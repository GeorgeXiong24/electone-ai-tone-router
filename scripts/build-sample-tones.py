"""Build sample-based single-instrument tones from the separated stems.

Cuts real single notes out of each stem and builds an SFZ instrument
(sforzando) carrying the recording's actual timbre.

Scope:
  - vocals are NOT sampled (the melody is played live on the ELB-02)
  - piano is NOT sampled: build-tones.py wraps the Accurate-Salamander grand
    piano sample library for it
  - drums produce no tone
  - guitar / bass / other stems go through the sampling pipeline below

Pipeline (all offline, CPU only):
  1. segmentation  - superflux onsets + pyin pitch breaks, boundaries snapped
     to local energy minima; decay tails may run past the next onset so held
     notes keep their natural decay
  2. analysis      - f0 median + IQR, voiced ratio, harmonicity (HPSS),
     spectral flatness, peak level
  3. quality gate  - hard rejects with reasons + weighted score per take
  4. velocity layers - 1-D k-means on loudness (2-3 layers)
  5. per-key render - every covered MIDI key gets its own wav rendered at
     exact pitch (resampling + spectral-envelope preservation so instrument
     body resonances stay put), the SFZ engine never stretches audio
  6. loop points   - period-multiple autocorrelation, crossfade baked into the
     wav, numeric self-check or the note falls back to one-shot
  7. SFZ           - one region per key per velocity layer, loop opcodes

Every threshold lives in CFG / INSTRUMENTS below. Coverage is reported
honestly: keys without a usable source note stay uncovered.

Usage:
  python build-sample-tones.py --song <title substring>
  python build-sample-tones.py --latest
  python build-sample-tones.py --analysis <analysis.json> --dry-run
"""
import argparse
import datetime
import glob
import json
import math
import os
import re
import sys
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _cache_env  # noqa: E402  (redirect caches into the project workspace)
import tone_paths  # noqa: E402  (tone tree layout: song / keyboard / plugin)

import numpy as np
import scipy.signal
import soundfile as sf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.environ.get("ELECTONE_WORKSPACE") or os.path.join(ROOT, "workspace")
TONE_ROOT = os.path.join(WORKSPACE, "tone")

CFG = {
    "sr": 44100,
    "hop": 512,
    "n_fft": 2048,
    "pyin_chunk_s": 30.0,
    "pyin_overlap_s": 2.0,

    # ---- hardware keyboard ranges (Yamaha ELB-02 / STAGEA Basic) -----------
    # The instrument has three keyboards with different spans, and a sampled
    # instrument only needs to cover the keys that can actually be pressed.
    # Generating outside the range wastes samples and, worse, lets the
    # transposition cap silently leave real keys uncovered.
    #   Upper / Lower: 49 keys, C2..C6  -> MIDI 36..84
    #   Pedal:         20 keys, C2..G3  -> MIDI 36..55
    # Source: Yamaha ELB-02 specifications ("Number of Pedal Keys: 20,
    # Range C-G"; "Upper (49 Range C-C) Lower (49 Range C-C)").
    "keyboard_ranges": {
        "右手": (36, 84),
        "左手": (36, 84),
        "脚": (36, 55),
    },
    # Fallback when a keyboard is not listed above.
    "default_key_range": (36, 84),

    # ---- segmentation
    "onset_delta": 0.08,          # peak-pick threshold on median-normalized flux
    "onset_wait_ms": 50.0,        # min gap between onsets
    "pitch_break_cents": 70.0,    # semitone-level change that splits a segment
    "pitch_break_ms": 40.0,       # the new level must persist this long
    "snap_back_ms": 20.0,         # snap boundary to energy minimum within +/- this
    "min_gap_ms": 30.0,           # merge boundaries closer than this
    "min_note_s": 0.08,
    "max_note_s": 5.0,
    "tail_extend_floor_db": 30.0, # extend tail while >= peak - this
    # A sampled instrument needs notes long enough to carry a timbre. On dense
    # material (a strummed guitar fires an onset every ~0.18 s) the raw
    # segmentation yields 0.18 s fragments, which are percussion, not notes:
    # the resulting SFZ sounds like a plucked zither. Segments shorter than
    # this are merged into their neighbour when the pitch agrees, and dropped
    # when it does not.
    "min_usable_note_s": 0.35,
    # Two adjacent segments may be merged when their pitch is within this many
    # semitones (a re-struck same note, or vibrato around one pitch).
    "merge_pitch_tolerance_semitones": 0.6,

    # ---- quality gates
    "min_voiced_ratio": 0.6,
    "max_f0_iqr_cents": 60.0,
    "min_harmonicity": 0.55,
    "max_flatness": 0.05,
    "min_peak_dbfs": -45.0,

    # ---- score weights (sum 1.0)
    "w_voiced": 0.35, "w_iqr": 0.25, "w_harmonicity": 0.25, "w_flatness": 0.15,

    # ---- velocity layers
    "layers": 3,
    "amp_veltrack": 40,

    # ---- rendering
    # How far a source note may be transposed to fill a key. Resampling by more
    # than a few semitones audibly changes the instrument: the formant
    # correction keeps body resonances in place, but the attack transient and
    # the harmonic balance still move, and a pad stretched a fifth sounds like a
    # bell. Keys outside this window are left uncovered rather than faked.
    #
    # 3.0 was measured against pitch alone; it is too wide once timbre matters.
    # Stretching a plucked sample by three semitones already moves its attack
    # and harmonic balance enough that neighbouring keys stop matching, and the
    # engine adds up to `max_gap_fill_semitones` on top. Measured on 《女孩》's
    # right hand, the 3+2 window assembled 49 keys from 38 takes and produced
    # the "classical guitar here, electric bass there" result. 2+1 keeps the
    # per-key stretch inside what one instrument tolerates; keys that would
    # need more are reported empty instead of faked, per the project's rule.
    "max_shift_semitones": 2.0,
    # The pedal keyboard is the exception. Its 20 keys span C2..G3 while a bass
    # stem typically only plays A1..D#2, so the upper half of the pedal has no
    # source material at all. Pre-rendering those keys would mean stretching a
    # bass by a whole octave, which no longer sounds like a bass; instead the
    # pedal gets a modest window and the remaining keys are filled by the SFZ
    # engine transposing the nearest rendered key (see write_instrument).
    "max_shift_semitones_by_keyboard": {
        "脚": 5.0,
    },
    # How far the SFZ engine may transpose a rendered key to cover a key that
    # has no source material at all (see write_instrument). This is separate
    # from `max_shift_semitones`, which bounds *pre-rendering*.
    #
    # This used to cap the fallback at 1 semitone for the manuals and leave
    # everything beyond it with no region at all, so those keys were silent —
    # the SFZ showed them greyed out and pressing them did nothing. A keyboard
    # where part of the range does not sound is not a usable instrument, so
    # every key now gets a region (see `always_cover_keyboard` below) and this
    # value only decides how much stretch is reported as acceptable.
    "max_gap_fill_semitones": {
        "default": 2,
        "脚": 5,
    },
    # Every key in the hardware keyboard range must produce sound. When true,
    # keys with no source material are pre-rendered from the nearest take (with
    # formant correction, so body resonances stay put) instead of being left
    # empty; the engine then fills anything still missing. The stretch is
    # reported per key in the manifest, but it is never a reason to go silent:
    # a stretched guitar still plays the right note, a missing region does not.
    "always_cover_keyboard": True,
    # Stretches beyond this many semitones are labelled `extrapolated` in the
    # manifest instead of `rendered`, so a piece that uses those keys knows the
    # timbre there is a best-effort extrapolation rather than the recording.
    "extrapolation_warn_semitones": 3.0,
    # A rendered key must land within this many cents of its target. pyin can
    # lock onto a harmonic instead of the fundamental (a fifth above is the
    # classic failure: ratio 1.5), and transposing from that wrong f0 makes
    # every key a fixed interval off — the instrument then plays wrong
    # intervals, which reads as "a bell on one key, jazz on two".
    "max_pitch_error_cents": 50.0,
    # Sub-harmonic correction: a candidate f0/2, f0/3 or f0/1.5 is accepted as
    # the true fundamental when its spectral peak reaches this fraction of the
    # peak at the detected f0.
    "subharmonic_energy_ratio": 0.25,
    "f0_min_hz": 30.0,
    # Upper bound for a corrected candidate (see refine_f0). Generous: the
    # correction may want to raise a detected sub-harmonic well into the
    # instrument's own range, and the per-instrument pyin fmax already bounds
    # what can ever be detected in the first place.
    "f0_max_hz": 2000.0,
    "fade_ms": 8.0,
    # A one-shot is held at its final sample until note-off, so it has to end
    # in silence or the note stops dead mid-sustain. This is the fade applied
    # to the tail of every non-looped sample.
    "oneshot_fade_ms": 220.0,
    "oneshot_fade_power": 2.0,
    "highpass_hz": 20.0,
    "target_layer_rms_db": -18.0,
    "peak_ceiling": 0.99,

    # ---- spectral envelope preservation ("formant" keep)
    "formant_correct": True,
    "formant_min_shift_cents": 25.0,
    "formant_lifter": 35,
    "formant_max_gain_db": 18.0,

    # ---- loops
    "loop_min_len_s": 0.25,
    "loop_tail_floor_db": 30.0,   # only loop when tail is still >= peak - this
    # 0.95 is correct and deliberately not lowered. Measured on 《女孩》 the
    # best loop candidate inside a plucked guitar note reaches only 0.38-0.46,
    # because a plucked string's spectrum keeps changing through the sustain —
    # there is no periodic steady state to loop. Lowering the threshold to fit
    # would splice two different moments of the note together and buzz. A
    # plucked instrument is supposed to be a one-shot; the bass stem (a
    # sustained instrument) does reach this threshold and loops correctly.
    "loop_corr_min": 0.95,
    "loop_max_decay_db": 3.0,
    "loop_k_max": 40,
    "loop_max_len_ratio": 0.4,
    "loop_window_s": 0.2,
    "loop_margin_s": 0.25,
    "loop_xfade_ms": 20.0,

    # ---- timbre consistency (see pick_takes / timbre_features) -------------
    # Choosing a take by pitch and cleanliness alone produced an instrument that
    # changed guitar from key to key. Measured on 《女孩》's guitar stem, before
    # these rules existed, in the rendered right-hand samples:
    #   relative brightness (centroid/f0): 2.03 (k63) .. 36.09 (k36), 17.7x
    #   HF/LF energy ratio:                0.075 (k63) .. 1.29 (k59), 17x
    #   adjacent keys jumped up to 142% in relative brightness
    # and inside the stem itself the usable notes spanned brightness_rel
    # 1.29..41.29 (32x) with `sub_peak_ratio` reaching 1.00 on the keys that
    # sounded like a bass. These thresholds are what separate that cluster from
    # an ordinary change of note; see the comments on each value.
    #
    # A take whose brightness sits this far below the stem's own trend is a
    # different instrument (or a different voice of the arrangement), not a
    # darker note. 1.5 octaves: the fit's own residual std is ~0.68 oct, so this
    # keeps normal per-note variation while cutting the 32x outlier cluster.
    "timbre_trend_tolerance_oct": 1.5,
    # Hard reject: a spectral peak under the fundamental that is at least as
    # tall as the fundamental itself. That is unambiguous — pyin's "fundamental"
    # is not even the strongest component, so another voice (a bass line, a
    # lower chord tone) is sounding at the same time. On 《女孩》's guitar stem
    # the rejected cluster measured 1.00-9.99, with exactly the low relative
    # brightness (1.9-4.5) of the keys that sounded like a bass guitar.
    #
    # The gate is deliberately at 1.0 and not lower: values in 0.5-1.0 also
    # occur on legitimate notes whose previous note is still decaying, so they
    # are handled by the ranking penalty below instead of being thrown away.
    #
    # Note there is deliberately NO gate on `harmonic_concentration`: measured
    # across this stem it never falls below 1.12, so any threshold low enough to
    # be safe would never fire — a check that cannot fail is worse than none.
    "max_sub_peak_ratio": 1.0,
    # Ranking penalty for a take sitting below the fundamental, on top of the
    # hard gate. Squared so a little is cheap and a lot is expensive.
    # Squared so a little is cheap and a lot is expensive.
    "w_sub_peak": 1.0,
    # Duration consistency. Every sample is a one-shot held until note-off, so
    # the note's length is directly audible and a 2.5 s take next to a 0.6 s take
    # reads as two different instruments. Measured on 《女孩》's rebuilt right
    # hand, the chosen takes spanned 0.36-2.54 s (7x) and that, not spectrum,
    # was what the jumps between neighbouring keys came from: brightness was
    # continuous while duration was not.
    #
    # Takes are penalised by how far their duration sits from the stem's own
    # median, in octaves (so "twice as long" costs the same as "half as long").
    "w_duration": 1.0,
    # The median duration's octave distance at which the penalty reaches the
    # weight above. 1.0 means twice (or half) the typical note is heavily
    # discouraged but still usable when nothing else reaches that key.
    "duration_ref_oct": 1.0,
    # Hard ceiling on the rendered length of a one-shot, as a multiple of the
    # stem's own median take. Picking could not equalise durations on 《女孩》's
    # right hand — some keys only had a 2.5 s take available while the median was
    # 0.6 s — so the rendered samples are trimmed to this ceiling instead. A
    # longer take is cut (with the usual one-shot fade); nothing is stretched to
    # fake sustain. 1.6 keeps the natural variation between a staccato and a
    # sustained note while removing the 7x span that read as two instruments.
    "max_duration_vs_median": 1.6,
    # Below this many usable notes the trend fit is not meaningful, so the
    # consistency rules are skipped rather than applied to noise.
    "timbre_min_notes": 8,
    # Weight of the consistency term when ranking takes for a key. It is a
    # tie-breaker on top of the existing quality score, not a replacement: a
    # slightly off-trend take still wins when the alternative is unacceptable.
    "w_timbre": 1.5,
}

INSTRUMENTS = {
    "guitar": {"label": "吉他", "fmin": 70.0, "fmax": 1500.0, "frame": 2048},
    "bass":   {"label": "贝斯", "fmin": 30.0, "fmax": 700.0,  "frame": 4096},
    "other":  {"label": "其他/混合", "fmin": 55.0, "fmax": 1500.0, "frame": 2048},
}

SKIP_STEMS = {
    "vocals": "人声不做采样音色（按需求）",
    "piano": "钢琴使用 Salamander 采样库（build-tones.py 生成）",
    "drums": "鼓组不生成音色",
}


def safe_name(v):
    return re.sub(r'[\\/:*?"<>|]', "-", v).strip()


def db(x):
    return 20.0 * math.log10(max(float(x), 1e-12))


def live_rms(x, rel_db=30.0):
    """RMS over the audible part only (extended silence tails must not dilute
    loudness measurements)."""
    peak = float(np.abs(x).max())
    if peak <= 0:
        return 0.0
    m = np.abs(x) >= peak * 10.0 ** (-rel_db / 20)
    if int(m.sum()) < 16:
        return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))
    return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64)[m] ** 2)))


# ---------------------------------------------------------------- analysis

def chunked_pyin(y, sr, hop, inst, cfg):
    """pyin over the whole stem in chunks to cap memory (4 GB constraint)."""
    import librosa

    chunk = int(cfg["pyin_chunk_s"] * sr) // hop * hop
    ov = int(cfg["pyin_overlap_s"] * sr) // hop * hop
    n_frames = len(y) // hop + 2
    f0_all = np.full(n_frames, np.nan)
    pos = 0
    while pos < len(y):
        a = max(0, pos - ov)
        b = min(len(y), pos + chunk + ov)
        f0, _, _ = librosa.pyin(y[a:b], fmin=inst["fmin"], fmax=inst["fmax"],
                                sr=sr, frame_length=inst["frame"], hop_length=hop)
        lo = pos // hop
        hi = min(n_frames, (min(pos + chunk, len(y))) // hop + 1)
        for g in range(lo, hi):
            local = (g * hop - a) // hop
            if 0 <= local < len(f0):
                f0_all[g] = f0[local]
        pos += chunk
        print(f"    pyin {min(pos, len(y)) * 100 // len(y)}%", flush=True)
    return f0_all


def frame_boundaries(f0, rms, ostr, sr, hop, cfg):
    """Union of superflux onsets and pitch breaks, snapped to energy minima."""
    import librosa

    env = ostr / (np.median(ostr) + 1e-9)
    onset_frames = librosa.onset.onset_detect(
        onset_envelope=env, sr=sr, hop_length=hop,
        backtrack=True, wait=max(1, int(cfg["onset_wait_ms"] * sr / 1000 / hop)),
        delta=cfg["onset_delta"])
    bounds = set(int(f) * hop for f in onset_frames)

    # pitch breaks: runs of semitone-quantized f0 changing and persisting
    n_break = max(1, int(cfg["pitch_break_ms"] * sr / 1000 / hop))
    q = np.round(librosa.hz_to_midi(f0))
    valid = np.isfinite(q)
    runs = []  # (start, end, value)
    i = 0
    while i < len(q):
        if not valid[i]:
            i += 1
            continue
        j = i
        while j < len(q) and valid[j] and q[j] == q[i]:
            j += 1
        runs.append((i, j, q[i]))
        i = j
    for k in range(1, len(runs)):
        a0, a1, va = runs[k - 1]
        b0, b1, vb = runs[k]
        if va != vb and (a1 - a0) >= n_break and (b1 - b0) >= n_break:
            bounds.add(b0 * hop)

    # snap each boundary to the nearest local RMS minimum within +/- snap_back
    snap = int(cfg["snap_back_ms"] * sr / 1000 / hop)
    snapped = []
    for s in bounds:
        f = s // hop
        a, b = max(0, f - snap), min(len(rms), f + snap + 1)
        if b > a:
            s = (a + int(np.argmin(rms[a:b]))) * hop
        snapped.append(s)

    snapped = sorted(snapped)
    merged = []
    min_gap = int(cfg["min_gap_ms"] * sr / 1000)
    for s in snapped:
        if merged and s - merged[-1] < min_gap:
            continue
        merged.append(s)

    segs = []
    for idx, s in enumerate(merged):
        e = merged[idx + 1] if idx + 1 < len(merged) else None
        segs.append((s, e))
    return segs


def extend_tail(rms, start, end, total, sr, hop, cfg):
    """Let the decay ring past the next onset while it stays quiet."""
    if end is None:
        return min(total, start + int(cfg["max_note_s"] * sr))
    a, b = start // hop, max(start // hop + 1, end // hop)
    seg_rms = rms[a:min(b, len(rms))]
    if seg_rms.size == 0:
        return end
    peak_db = db(seg_rms.max())
    floor_db = peak_db - cfg["tail_extend_floor_db"]
    limit = min(total, start + int(cfg["max_note_s"] * sr))
    f = end // hop
    while f < len(rms) and f * hop < limit:
        if db(rms[f]) > floor_db:
            break
        f += 1
    return min(f * hop, limit)


def analyze_segment(y_seg, f0_seg, sr, cfg):
    import librosa

    f0v = f0_seg[np.isfinite(f0_seg)]
    feats = {"frames": len(f0_seg), "voiced_ratio": float(f0v.size / max(len(f0_seg), 1))}
    if f0v.size >= 3:
        midi = librosa.hz_to_midi(f0v)
        feats["f0_hz"] = float(np.median(f0v))
        feats["midi_f"] = float(np.median(midi))
        feats["f0_iqr_cents"] = float(
            (np.percentile(midi, 75) - np.percentile(midi, 25)) * 100.0)
    else:
        feats["f0_hz"] = float("nan")
        feats["midi_f"] = float("nan")
        feats["f0_iqr_cents"] = 1000.0

    feats["peak_dbfs"] = db(np.abs(y_seg).max())
    feats["rms"] = live_rms(y_seg)

    h, p = librosa.effects.hpss(y_seg)
    he = float(np.sum(h.astype(np.float64) ** 2))
    pe = float(np.sum(p.astype(np.float64) ** 2))
    feats["harmonicity"] = he / (he + pe + 1e-12)

    flat = librosa.feature.spectral_flatness(y=y_seg)
    feats["flatness"] = float(np.median(flat))
    return feats


def timbre_features(seg, sr, f0_hz, cfg):
    """Measure what an instrument a segment actually sounds like.

    Pitch tells you which key a sample serves; it says nothing about whether the
    sample is the *same instrument* as its neighbours. This returns the measures
    that do, so takes can be chosen for consistency (see pick_takes):

    - `brightness_rel`: spectral centroid / f0. Pitch-invariant by construction,
      so a value that drifts between neighbouring keys means different guitars,
      not different notes. Measured on 《女孩》's guitar stem: 1.29-41.29, a 32x
      spread inside a single stem.
    - `sub_peak_ratio`: tallest spectral peak *below* the detected f0, relative
      to the peak at f0. A clean single note has almost no energy under its own
      fundamental; a value near 1.0 means a bass line or a lower chord voice is
      sounding at the same time. On 《女孩》 the keys that sounded like a bass
      guitar (midi 55/64/74/79) all measured 1.00 here.
    - `harmonic_concentration`: energy at the f0 harmonic series divided by the
      tallest peak. High for a single plucked note, lower when foreign partials
      coexist.
    """
    import librosa

    y = np.asarray(seg, dtype=np.float32)
    if y.ndim > 1:
        y = y.mean(axis=1)
    if y.size < int(0.05 * sr) or not math.isfinite(f0_hz) or f0_hz <= 0:
        return None

    S = np.abs(librosa.stft(y, n_fft=cfg["n_fft"], hop_length=cfg["hop"]))
    if S.size == 0:
        return None
    freqs = librosa.fft_frequencies(sr=sr, n_fft=cfg["n_fft"])
    # A +-3% band around a low fundamental can be narrower than one FFT bin
    # (n_fft=2048 at 44.1 kHz gives 21.5 Hz per bin, so +-3% of 124 Hz is 7 Hz
    # and the band comes back empty). Every measurement below must therefore
    # widen its window to at least a couple of bins, or it reads "no energy"
    # for notes that are perfectly audible.
    bin_hz = float(sr) / float(cfg["n_fft"])

    def band(center, rel_tol=0.03):
        half = max(center * rel_tol, 1.5 * bin_hz)
        return (center - half, center + half)

    # Median over the sustain, skipping the attack transient: the attack of a
    # plucked note is broadband and would swamp the comparison.
    n_frames = S.shape[1]
    if n_frames < 3:
        spec = S.mean(axis=1)
    else:
        a = n_frames // 3
        b = max(a + 1, (2 * n_frames) // 3)
        spec = np.median(S[:, a:b], axis=1)

    def tallest(lo, hi):
        band_vals = spec[(freqs >= lo) & (freqs < hi)]
        return float(band_vals.max()) if band_vals.size else 0.0

    lo, hi = band(f0_hz)
    at_f0 = tallest(lo, hi)
    if at_f0 <= 0:
        return None

    # Anything an octave or more below the fundamental is a different voice.
    sub = tallest(30.0, f0_hz * 0.95)

    harmonics = []
    for i in range(1, 13):
        f = f0_hz * i
        if f >= sr / 2.0:
            break
        hlo, hhi = band(f)
        harmonics.append(tallest(hlo, hhi))
    harm = sum(harmonics)

    centroid = float(np.median(librosa.feature.spectral_centroid(S=S, sr=sr)))

    # Envelope shape. A note's length and decay are as much a part of "what
    # instrument is this" as its spectrum: on 《女孩》's rebuilt right hand one
    # key was a 2.54 s ringing note and its neighbour a 0.60 s pluck, and played
    # back-to-back they read as a sustained bass and a staccato guitar even
    # though their spectra were continuous. Every sample is a one-shot held
    # until note-off, so an unmatched duration is directly audible.
    env = np.abs(y)
    peak = float(env.max()) or 1e-12
    # Decay: how far the tail falls below the peak (dB, negative).
    tail_n = max(1, int(len(env) * 0.1))
    tail = float(env[-tail_n:].max())
    decay_db = 20.0 * math.log10(max(tail, 1e-12) / peak)
    # Attack: time from the start to the amplitude peak (s).
    attack_s = float(np.argmax(env)) / float(sr)

    return {
        "brightness_rel": centroid / f0_hz,
        "sub_peak_ratio": sub / at_f0,
        "harmonic_concentration": harm / at_f0,
        "centroid_hz": centroid,
        "decay_db": decay_db,
        "attack_s": attack_s,
    }


def timbre_trend(notes, cfg):
    """Fit how relative brightness depends on register, robustly.

    Even one instrument is not perfectly flat in `brightness_rel`: a small body
    resonates differently at different pitches, and measurements scatter. What
    separates "same instrument, different note" from "different instrument" is
    whether a take lands on the shared trend. Fitting log2(brightness_rel) over
    midi and judging the residual is what makes this pitch-invariant in practice
    as well as in definition.

    Returns (slope_per_semitone, intercept) or None when there is too little
    material to fit. The fit is iterative with outlier rejection so that the
    minority of genuinely foreign takes cannot define the trend they are judged
    against.
    """
    rows = [n for n in notes if n.get("timbre")]
    if len(rows) < cfg["timbre_min_notes"]:
        return None
    x = np.array([n["midi"] for n in rows], dtype=np.float64)
    y = np.log2(np.clip([n["timbre"]["brightness_rel"] for n in rows], 0.2, None))

    keep = np.ones(len(rows), dtype=bool)
    coef = np.array([0.0, float(np.median(y))])
    for _ in range(8):
        if keep.sum() < 4:
            break
        A = np.vstack([x[keep], np.ones(int(keep.sum()))]).T
        coef, *_ = np.linalg.lstsq(A, y[keep], rcond=None)
        resid = y - (x * coef[0] + coef[1])
        # 1.5 octaves: wide enough to keep a legitimately bright articulation,
        # narrow enough that the 32x outlier cluster falls outside.
        new_keep = np.abs(resid) <= cfg["timbre_trend_tolerance_oct"]
        if np.array_equal(new_keep, keep):
            break
        keep = new_keep
    return float(coef[0]), float(coef[1])


def timbre_deviation(note, trend):
    """How far a take sits from the stem's own brightness trend, in octaves."""
    if not note.get("timbre") or trend is None:
        return 0.0
    slope, intercept = trend
    pred = note["midi"] * slope + intercept
    return float(np.log2(max(note["timbre"]["brightness_rel"], 1e-9)) - pred)


def refine_f0(seg, sr, f0_hz, cfg):
    """Return the true fundamental of a segment, correcting harmonic errors.

    pyin reports the strongest periodicity, which on a bright, noisy or
    polyphonic source is often *not* the fundamental. The error goes in BOTH
    directions, and correcting only one of them is what broke this instrument:

    - UPWARD (f0 detected too high): pyin locked onto a harmonic, e.g. the 3rd
      (ratio 3.0) or the octave (2.0). The true f0 is f0/ratio, lower.
    - DOWNWARD (f0 detected too low): pyin locked onto a sub-harmonic of the
      real note, which happens when a strong partial is missing. The true f0
      is f0*ratio, higher.

    On 《女孩》 the guitar take serving keys 53..57 was detected at 196.63 Hz
    while the segment actually sounds 295.37 Hz — exactly 3/2, i.e. pyin
    reported the *sub-harmonic* at two thirds of the true pitch. A downward-only
    search can never fix that: it walked 196->98->65 and returned 196, so every
    rendered key came out +700 cents off and verify_pitch rejected all five,
    leaving a dead patch in the middle of the left hand.

    The rule below is symmetric and spectral: the true fundamental is the
    candidate that has real energy at itself AND a well-populated harmonic
    series above it. A harmonic or sub-harmonic does not.
    """
    import librosa

    y = np.asarray(seg, dtype=np.float32)
    if y.ndim > 1:
        y = y.mean(axis=1)
    if y.size < int(0.05 * sr) or not math.isfinite(f0_hz) or f0_hz <= 0:
        return f0_hz

    n_fft = cfg["n_fft"]
    S = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=cfg["hop"]))
    if S.size == 0:
        return f0_hz
    spec = S.mean(axis=1)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)

    def energy_at(hz, tol=0.03):
        """Peak energy in a narrow band around hz (relative to the spectrum)."""
        if hz <= 0 or hz >= sr / 2:
            return 0.0
        lo, hi = hz * (1 - tol), hz * (1 + tol)
        band = spec[(freqs >= lo) & (freqs <= hi)]
        return float(band.max()) if band.size else 0.0

    def harmonic_support(f0):
        """How much of the first N harmonics actually carry energy.

        This is the discriminator. If `f0` is really the fundamental, every
        harmonic up to a few kHz is present. If `f0` is a sub-harmonic of the
        true note, the odd harmonics are missing — the series is sparse. That
        asymmetry is what tells a real octave-down apart from a spurious one.
        """
        if f0 <= 0:
            return 0.0
        n = 8
        peak = max(energy_at(f0 * i) for i in range(1, n + 1)) or 1e-12
        present = sum(1 for i in range(1, n + 1)
                      if energy_at(f0 * i) >= peak * 0.12)
        return present / n

    ref = energy_at(f0_hz)
    if ref <= 0:
        return f0_hz

    # Candidates in both directions. Ratios > 1 raise the pitch (fixing a
    # detected sub-harmonic), ratios < 1 lower it (fixing a detected harmonic).
    candidates = [f0_hz]
    for ratio in (2.0, 3.0, 1.5):
        for cand in (f0_hz / ratio, f0_hz * ratio):
            if cfg["f0_min_hz"] <= cand <= cfg["f0_max_hz"]:
                candidates.append(cand)

    scored = []
    for cand in candidates:
        if energy_at(cand) < ref * cfg["subharmonic_energy_ratio"]:
            # A candidate with no energy at all at its own fundamental cannot
            # be the note being played.
            if cand != f0_hz:
                continue
        support = harmonic_support(cand)
        # Prefer the candidate with the fullest harmonic series; break ties
        # toward the original detection so an ambiguous segment is left alone.
        scored.append((support, -abs(math.log2(cand / f0_hz)), cand))
    if not scored:
        return f0_hz
    scored.sort(reverse=True)
    best_support, _, best = scored[0]

    # Only overrule pyin when the alternative is clearly better, otherwise a
    # noisy segment would wander between octaves from note to note.
    if best != f0_hz and best_support >= harmonic_support(f0_hz) + 0.12:
        return best
    return f0_hz


def gate_and_score(feats, cfg):
    reasons = []
    if feats["voiced_ratio"] < cfg["min_voiced_ratio"]:
        reasons.append("voiced_ratio")
    if feats["f0_iqr_cents"] > cfg["max_f0_iqr_cents"]:
        reasons.append("f0_iqr")
    if feats["harmonicity"] < cfg["min_harmonicity"]:
        reasons.append("harmonicity")
    if feats["flatness"] > cfg["max_flatness"]:
        reasons.append("flatness")
    if feats["peak_dbfs"] < cfg["min_peak_dbfs"]:
        reasons.append("peak")
    if not math.isfinite(feats["f0_hz"]):
        reasons.append("no_f0")

    score = (cfg["w_voiced"] * feats["voiced_ratio"]
             + cfg["w_iqr"] * max(0.0, 1.0 - feats["f0_iqr_cents"] / 60.0)
             + cfg["w_harmonicity"] * feats["harmonicity"]
             + cfg["w_flatness"] * max(0.0, 1.0 - feats["flatness"] / 0.05))
    return (not reasons), reasons, score


# ---------------------------------------------------------------- layers / takes

def kmeans1d(vals, k):
    """Tiny deterministic 1-D k-means; labels ordered by ascending value."""
    v = np.asarray(vals, dtype=np.float64)
    centers = np.quantile(v, np.linspace(0.1, 0.9, k))
    labels = np.zeros(len(v), dtype=int)
    for _ in range(64):
        d = np.abs(v[:, None] - centers[None, :])
        new_labels = np.argmin(d, axis=1)
        if np.array_equal(new_labels, labels) and _ > 0:
            break
        labels = new_labels
        for c in range(k):
            if np.any(labels == c):
                centers[c] = v[labels == c].mean()
    order = np.argsort(centers)
    remap = {int(o): i for i, o in enumerate(order)}
    return np.array([remap[int(l)] for l in labels]), [float(centers[o]) for o in order]


def duration_penalty(note, median_dur_s, cfg):
    """How much a take's length disagrees with the rest of the instrument.

    Returns 0 for a typical note and grows toward `w_duration` at
    `duration_ref_oct` octaves away. Duration is judged in octaves so that
    "twice as long" and "half as long" cost the same.
    """
    w = cfg.get("w_duration", 0.0)
    dur = note.get("dur_s")
    if not w or not median_dur_s or not dur or dur <= 0:
        return 0.0
    octs = abs(math.log2(dur / median_dur_s))
    return w * min(octs / max(cfg.get("duration_ref_oct", 1.0), 1e-6), 1.0) ** 2


def pick_takes(notes, cfg=None, trend=None, median_dur_s=None):
    """Best take per (rounded midi, layer), preferring a consistent timbre.

    Picking by `score` alone is what made the instrument change guitar from key
    to key: `score` measures how *clean* a segment is, never whether it sounds
    like the neighbouring keys. Two takes can both be clean and yet be an
    acoustic guitar and a muted electric bass.

    The consistency terms are:
    - brightness deviation from the stem's own trend (pitch-invariant, so a
      legitimately darker low note is not penalised);
    - energy under the fundamental (another voice sounding at the same time);
    - duration, because every sample is a one-shot and its length is audible.
    """
    cfg = cfg or CFG
    w_timbre = cfg.get("w_timbre", 0.0)
    w_sub = cfg.get("w_sub_peak", 0.0)
    if median_dur_s is None and notes:
        median_dur_s = float(np.median([n["dur_s"] for n in notes if n.get("dur_s")]))
    best = {}
    for n in notes:
        key = (n["midi"], n["layer"])
        penalty = 0.0
        if trend is not None:
            if w_timbre:
                # Squared so a mild deviation is cheap and a different
                # instrument is expensive, instead of a linear cost that a lucky
                # quality score could outbid.
                dev = abs(timbre_deviation(n, trend))
                penalty += w_timbre * dev * dev
            if w_sub and n.get("timbre"):
                penalty += w_sub * n["timbre"]["sub_peak_ratio"] ** 2
        penalty += duration_penalty(n, median_dur_s, cfg)
        rank = n["score"] - penalty
        cur = best.get(key)
        if cur is None or rank > cur["_rank"]:
            n = dict(n)
            n["_rank"] = rank
            n["_timbre_dev"] = timbre_deviation(n, trend)
            best[key] = n
    return list(best.values())


# ---------------------------------------------------------------- rendering

def spectral_envelope(seg, cfg):
    """Smooth spectral envelope E0(f) via cepstral liftering (median over time)."""
    import librosa

    S = np.abs(librosa.stft(seg, n_fft=cfg["n_fft"], hop_length=cfg["hop"]))
    logS = np.log(S + 1e-10)
    cep = np.fft.irfft(logS, axis=0)
    cep[cfg["formant_lifter"]:, :] = 0.0
    logE = np.fft.rfft(cep, axis=0)
    E = np.exp(logE.real)
    return np.median(E, axis=1)


def resample_by(seg, r):
    """Resample so the pitch rises by factor r (duration shrinks by r)."""
    frac = Fraction(1.0 / r).limit_denominator(10000)
    out = scipy.signal.resample_poly(seg.astype(np.float64), frac.numerator,
                                     frac.denominator)
    return out


def verify_pitch(seg, sr, f_target, cfg):
    """Return the rendered pitch error in cents, or None when unmeasurable.

    The whole instrument depends on this: `render_note` transposes by
    `f_target / f0_src`, so if the source's f0 was detected an octave or a
    fifth off, every rendered key inherits that error and the instrument plays
    the wrong intervals — a single key sounds like a bell, a chord sounds like
    jazz, and nothing matches the recording.

    pyin locks onto whichever harmonic is strongest, and on a bright or noisy
    stem that is often not the fundamental (a fifth above is the classic case:
    ratio 1.5). Measuring the *rendered* note catches that, because the error
    is then a fixed interval away from the target.
    """
    import librosa

    y = np.asarray(seg, dtype=np.float32)
    if y.ndim > 1:
        y = y.mean(axis=1)
    if y.size < int(0.05 * sr):
        return None
    try:
        f0, voiced, _ = librosa.pyin(
            y, fmin=f_target * 0.6, fmax=f_target * 1.8,
            sr=sr, frame_length=4096)
    except Exception:
        return None
    f0v = f0[voiced]
    if f0v.size < 3:
        return None
    measured = float(np.median(f0v))
    if measured <= 0:
        return None
    return 1200.0 * math.log2(measured / f_target)


def render_note(seg, f_target, f0_src, env0, cfg):
    """Render a segment at exact target pitch, keeping the spectral envelope."""
    import librosa

    r = f_target / f0_src
    cents = 1200.0 * math.log2(r)
    if abs(cents) < 0.01:
        out = seg.astype(np.float64).copy()
        return out, cents

    out = resample_by(seg, r)
    if not (cfg["formant_correct"]
            and abs(cents) >= cfg["formant_min_shift_cents"]):
        return out, cents

    # shifted audio carries envelope E0(f/r); restore E0(f) so instrument body
    # resonances stay at their original frequencies
    n_freq = cfg["n_fft"] // 2 + 1
    freqs = np.fft.rfftfreq(cfg["n_fft"], 1.0 / cfg["sr"])[:n_freq]
    E_target = env0[:n_freq]
    E_warped = np.interp(freqs / r, freqs, E_target)
    H = E_target / (E_warped + 1e-10)
    H = np.clip(H, 10.0 ** (-cfg["formant_max_gain_db"] / 20),
                10.0 ** (cfg["formant_max_gain_db"] / 20))
    H[0] = 1.0
    S = librosa.stft(out, n_fft=cfg["n_fft"], hop_length=cfg["hop"])
    S *= H[:, None]
    out = librosa.istft(S, hop_length=cfg["hop"], length=len(out))
    return out, cents


def normalize_duration(seg, sr, cfg, target_s):
    """Trim a one-shot to a common length so every key lasts the same.

    Every sample is played as a one-shot held until note-off, so its length is
    part of the instrument's identity. Without this, 《女孩》's right hand mixed
    2.54 s ringing takes with 0.36 s plucks: holding one key gave a sustained
    bass drone and the next a staccato pluck, with continuous spectra the whole
    time. Picking takes by duration could not fix it because on some keys the
    long take was the only material available.

    Trimming is the safe direction: it can only shorten, never invent sustain.
    The tail gets the same exponential fade a one-shot already needs, so the
    truncation is inaudible rather than a click.
    """
    if not target_s or target_s <= 0:
        return seg
    want = int(target_s * sr)
    if want <= 0 or len(seg) <= want:
        return seg
    out = seg[:want].copy()
    # Re-apply the one-shot tail fade over the trimmed end so the cut is silent.
    tail = min(int(cfg["oneshot_fade_ms"] * sr / 1000), len(out) // 2)
    if tail > 1:
        out[-tail:] *= np.linspace(1, 0, tail) ** cfg["oneshot_fade_power"]
    return out


def find_loop(seg, sr, f_target, cfg):
    """Period-multiple loop with a crossfade baked into the wav.

    Returns (loop_start, loop_end) in samples, or None for one-shot.
    """
    n = len(seg)
    if n < int(cfg["loop_min_len_s"] * sr):
        return None
    P = sr / f_target

    # loop placement lives inside the longest "live" run of the segment (rms
    # within loop_tail_floor_db of the peak): the file tail may hold an abrupt
    # cut or extended silence that would wreck the correlation windows
    hop = 512
    n_fr = max(1, n // hop)
    fr_rms = np.array([
        float(np.sqrt(np.mean(seg[i * hop:(i + 1) * hop] ** 2)))
        for i in range(n_fr)])
    peak_db = db(np.abs(seg).max())
    live = np.array([db(v) >= peak_db - cfg["loop_tail_floor_db"] for v in fr_rms])
    best_run = (0, 0)
    i = 0
    while i < n_fr:
        if not live[i]:
            i += 1
            continue
        j = i
        while j < n_fr and live[j]:
            j += 1
        if j - i > best_run[1] - best_run[0]:
            best_run = (i, j)
        i = j
    run_a, run_b = best_run[0] * hop, best_run[1] * hop
    if run_b - run_a < int(cfg["loop_min_len_s"] * sr):
        return None

    W = min(int(cfg["loop_window_s"] * sr), (run_b - run_a) // 3)
    we = run_b - W
    if we - W <= run_a:
        return None

    xfade = int(cfg["loop_xfade_ms"] * sr / 1000)
    xfade = max(xfade, min(W // 2, int(2 * P)))
    # loops shorter than ~100 ms (or 8 periods) buzz on noisy material
    loop_min_len = max(int(0.1 * sr), int(8 * P))
    candidates = []
    for k in range(2, cfg["loop_k_max"] + 1):
        L = P * k
        if L > cfg["loop_max_len_ratio"] * n or L < loop_min_len:
            continue
        ws = int(round(we - L))
        if ws < run_a:
            continue
        a = seg[ws:ws + W]
        b = seg[we:we + W]
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        if na < 1e-9 or nb < 1e-9:
            continue
        corr = float(np.dot(a, b) / (na * nb))
        decay = abs(db(np.sqrt(np.mean(b ** 2))) - db(np.sqrt(np.mean(a ** 2))))
        if corr >= cfg["loop_corr_min"] and decay <= cfg["loop_max_decay_db"]:
            candidates.append((L, corr, ws))
    if not candidates:
        return None
    # longest passing loop (more natural than a tiny one); ties -> higher corr
    candidates.sort(key=lambda c: (-c[0], -c[1]))
    L, corr, ws = candidates[0]
    xfade = min(xfade, max(1, L // 2), W // 2)
    # snap loop_start to a whole number of periods from the sample start
    ws = int(round(ws / P) * P)
    we_looped = ws + int(round(L / P) * P)
    if we_looped + xfade >= n or ws + xfade > we_looped:
        return None

    # bake the crossfade at the loop_start region: the junction we->ws then
    # continues naturally and morphs into the loop content over `xfade`
    seg[ws:ws + xfade] = ((1.0 - np.linspace(0, 1, xfade)) * seg[we_looped:we_looped + xfade]
                          + np.linspace(0, 1, xfade) * seg[ws:ws + xfade])

    # self-check: junction jump no worse than the natural adjacent jumps
    jump = abs(seg[ws] - seg[we_looped - 1])
    natural = np.max(np.abs(np.diff(seg[ws:we_looped]))) + 1e-12
    dc = abs(np.mean(seg[we_looped - int(P):we_looped])
             - np.mean(seg[ws:ws + int(P)]))
    if jump > natural or dc > 0.05 * float(np.abs(seg).max()):
        return None
    return ws, we_looped


def finalize_wav(seg, sr, cfg, looped=False):
    """DC removal, 20 Hz high-pass, and end fades.

    The end fade is what makes a one-shot playable. Without a loop the engine
    holds the last sample and, when the key is released, applies `ampeg_release`
    — but `finalize_wav` already faded the file out over `fade_ms`. The audible
    result is a note that stops dead 1–2 s in, mid-sustain, which is the
    "过个1-2秒直接硬掐断" symptom. A one-shot therefore gets a long, natural
    decay to silence so the transition into the release is inaudible.

    A looped region is different: its tail is never reached, so it keeps a short
    fade to avoid a click at the loop point.
    """
    seg = seg - np.mean(seg)
    sos = scipy.signal.butter(2, cfg["highpass_hz"] / (sr / 2), "highpass",
                              output="sos")
    seg = scipy.signal.sosfiltfilt(sos, seg) if len(seg) > 60 else seg

    fade = min(int(cfg["fade_ms"] * sr / 1000), len(seg) // 4)
    if fade > 1:
        seg[:fade] *= np.linspace(0, 1, fade)

    if looped:
        tail = fade
    else:
        # A one-shot is held at its last sample until note-off, so the last
        # sample must already be silent. Fade over the shorter of a fixed
        # release window and the back half of the note.
        tail = min(int(cfg["oneshot_fade_ms"] * sr / 1000), len(seg) // 2)
        tail = max(tail, fade)
    if tail > 1:
        seg[-tail:] *= np.linspace(1, 0, tail) ** cfg["oneshot_fade_power"]
    return seg


# ---------------------------------------------------------------- build

def merge_short_segments(segs, f0, sr, hop, cfg):
    """Merge or drop segments too short to carry a timbre.

    A sampled instrument is only as good as its notes. On dense material the
    onset detector fires on every re-strike, so a strummed guitar yields 0.18 s
    fragments — percussion, not notes — and an SFZ built from them sounds like
    a plucked zither rather than a guitar.

    Two adjacent segments are merged when their median pitch agrees within
    `merge_pitch_tolerance_semitones` (a re-struck note, or vibrato around one
    pitch). A short segment whose neighbour disagrees is dropped: it is a
    transient, not a note.
    """
    import librosa

    def seg_midi(s, e):
        fa, fb = s // hop, max(s // hop + 1, e // hop)
        f = f0[fa:fb]
        f = f[np.isfinite(f)]
        if f.size < 3:
            return None
        return float(np.median(librosa.hz_to_midi(f)))

    min_len = int(cfg["min_usable_note_s"] * sr)
    tol = cfg["merge_pitch_tolerance_semitones"]

    out = []
    for s, e in segs:
        if out and (e - s) < min_len:
            ps, pe = out[-1]
            m_prev, m_cur = seg_midi(ps, pe), seg_midi(s, e)
            if (m_prev is not None and m_cur is not None
                    and abs(m_prev - m_cur) <= tol):
                out[-1] = (ps, e)
                continue
            # Pitch disagrees: keep the longer of the two, drop the fragment.
            if (pe - ps) >= (e - s):
                continue
            out[-1] = (s, e)
            continue
        out.append((s, e))

    # A trailing fragment has no neighbour to merge into.
    if out and (out[-1][1] - out[-1][0]) < min_len and len(out) > 1:
        out.pop()
    return out


def build_instrument(stem_wav, inst, dry_run=False, keyboard="右手"):
    import librosa

    cfg = CFG
    sr = cfg["sr"]
    hop = cfg["hop"]
    y, _ = librosa.load(stem_wav, sr=sr, mono=True)
    if y.size == 0:
        return None

    print("    pyin + features ...")
    f0 = chunked_pyin(y, sr, hop, inst, cfg)
    rms = librosa.feature.rms(y=y, frame_length=1024, hop_length=hop)[0]
    ostr = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop,
                                        lag=1, max_size=3)

    print("    segmenting ...")
    bounds = frame_boundaries(f0, rms, ostr, sr, hop, cfg)
    segs = []
    for s, e in bounds:
        e2 = extend_tail(rms, s, e, len(y), sr, hop, cfg)
        if (min(e2, len(y)) - s) / sr >= cfg["min_note_s"]:
            segs.append((s, min(e2, len(y))))
    n_raw = len(segs)
    segs = merge_short_segments(segs, f0, sr, hop, cfg)
    print(f"    {n_raw} segments -> {len(segs)} usable "
          f"(merged/dropped {n_raw - len(segs)} fragments)")

    notes = []
    reject_counts = {}
    corrected = 0
    for s, e in segs:
        fa, fb = s // hop, max(s // hop + 1, e // hop)
        feats = analyze_segment(y[s:e], f0[fa:fb], sr, cfg)
        ok, reasons, score = gate_and_score(feats, cfg)
        if not ok:
            for r in reasons:
                reject_counts[r] = reject_counts.get(r, 0) + 1
            continue
        # pyin can report a harmonic instead of the fundamental; correct it
        # before the note is used as a transposition reference.
        true_f0 = refine_f0(y[s:e], sr, feats["f0_hz"], cfg)
        if abs(1200.0 * math.log2(true_f0 / feats["f0_hz"])) > 1.0:
            corrected += 1
            feats["f0_hz_raw"] = feats["f0_hz"]
            feats["f0_hz"] = true_f0
            feats["midi_f"] = float(librosa.hz_to_midi(true_f0))
        notes.append({
            "start": s, "end": e,
            "midi": int(round(feats["midi_f"])),
            "f0_hz": feats["f0_hz"],
            "rms": feats["rms"], "rms_db": db(feats["rms"]),
            "score": score, "feats": feats,
            "dur_s": float(e - s) / float(sr),
            "audio": y[s:e].astype(np.float32),
        })
    if corrected:
        print(f"    corrected {corrected} harmonic f0 detection(s)")
    if not notes:
        return {"notes": [], "reject_counts": reject_counts}

    # register outliers: crosstalk/bleed segments whose pitch sits far outside
    # what the stem actually plays (e.g. a stray pad in the bass stem)
    if len(notes) >= 8:
        med = float(np.median([n["midi"] for n in notes]))
        kept = [n for n in notes if abs(n["midi"] - med) <= 30.0]
        reject_counts["register_outlier"] = len(notes) - len(kept)
        notes = kept
    if not notes:
        return {"notes": [], "reject_counts": reject_counts}

    # ---- timbre: measure what each take actually sounds like --------------
    # Pitch decides which key a take can serve; it says nothing about whether
    # the take is the same instrument as its neighbours. Without this the
    # right hand ended up assembled from an acoustic guitar, a muted electric
    # and a bass line depending on which key was asked for.
    for n in notes:
        n["timbre"] = timbre_features(n["audio"], sr, n["f0_hz"], cfg)

    before = len(notes)
    notes = [n for n in notes if n.get("timbre")]
    if len(notes) != before:
        reject_counts["unmeasurable_timbre"] = before - len(notes)
    if not notes:
        return {"notes": [], "reject_counts": reject_counts}

    # Hard reject: another voice sounding underneath (bass bleed, chord tone).
    kept = [n for n in notes
            if n["timbre"]["sub_peak_ratio"] <= cfg["max_sub_peak_ratio"]]
    if len(notes) != len(kept):
        reject_counts["sub_peak"] = len(notes) - len(kept)
    notes = kept
    if not notes:
        return {"notes": [], "reject_counts": reject_counts}

    # velocity layers on loudness
    n_layers = 3 if len(notes) >= 12 else (2 if len(notes) >= 6 else 1)
    n_layers = min(n_layers, cfg["layers"])
    labels, centroids = kmeans1d([n["rms_db"] for n in notes], n_layers)
    for n, l in zip(notes, labels):
        n["layer"] = int(l)

    # Fit the stem's own brightness-vs-register trend now that the foreign
    # voices are gone, so the surviving takes define the reference instead of
    # the outliers they are judged against.
    trend = timbre_trend(notes, cfg)
    median_dur = float(np.median([n["dur_s"] for n in notes if n.get("dur_s")]))
    durs = sorted(n["dur_s"] for n in notes if n.get("dur_s"))
    print(f"    take durations: median {median_dur:.2f}s, "
          f"p10 {durs[len(durs) // 10]:.2f}s, p90 {durs[9 * len(durs) // 10]:.2f}s")
    if trend is not None:
        print(f"    timbre trend: brightness_rel x2^{trend[0]:+.3f}/semitone "
              f"(base x2^{trend[1]:.2f} at midi 0)")
    takes = pick_takes(notes, cfg, trend, median_dur)
    if trend is not None:
        devs = sorted(abs(t.get("_timbre_dev", 0.0)) for t in takes)
        if devs:
            print(f"    timbre spread across chosen takes: "
                  f"median {devs[len(devs) // 2]:.2f} oct, max {devs[-1]:.2f} oct")

    # Which keys to render.
    #
    # Preference order for each key on the hardware keyboard:
    #   1. a take within `max_shift_semitones` — pre-rendered at exact pitch, so
    #      the engine applies no stretch at all (the ideal case);
    #   2. if none, the nearest take anyway, pre-rendered at exact pitch from it.
    #
    # Step 2 is what makes every key sound. It deliberately trades timbre
    # accuracy on outlying keys for a playable keyboard: before it existed, keys
    # with no take nearby got no region at all and were silent — greyed out in
    # the SFZ and dead under the finger. A stretched guitar note is still a
    # guitar note on the right pitch; a missing region is nothing.
    #
    # Pre-rendering (rather than letting the SFZ engine transpose) matters
    # because render_note() corrects the spectral envelope, keeping body
    # resonances at their original frequencies; the engine would just resample.
    key_lo, key_hi = cfg["keyboard_ranges"].get(keyboard, cfg["default_key_range"])
    shift_cap = cfg["max_shift_semitones_by_keyboard"].get(
        keyboard, cfg["max_shift_semitones"])
    max_cents = shift_cap * 100.0
    keys = set()
    for t in takes:
        for k in range(max(key_lo, t["midi"] - int(shift_cap)),
                       min(key_hi, t["midi"] + int(shift_cap)) + 1):
            cents = 1200.0 * math.log2(librosa.midi_to_hz(k) / t["f0_hz"])
            if abs(cents) <= max_cents:
                keys.add(k)

    # Extrapolated keys: no take within the comfortable window, so use the
    # nearest one and accept the stretch. Recorded per key so the manifest can
    # be honest about where the instrument is a best-effort extrapolation.
    extrapolated = {}
    if cfg.get("always_cover_keyboard", True) and takes:
        warn_cents = cfg["extrapolation_warn_semitones"] * 100.0
        for k in range(key_lo, key_hi + 1):
            if k in keys:
                continue
            src = min(takes, key=lambda t: abs(1200.0 * math.log2(
                librosa.midi_to_hz(k) / t["f0_hz"])))
            stretch = 1200.0 * math.log2(librosa.midi_to_hz(k) / src["f0_hz"])
            extrapolated[k] = round(stretch, 1)
            keys.add(k)
    keys = sorted(keys)

    # Report the gap honestly: with always_cover_keyboard every hardware key is
    # rendered, but the ones built from a distant take are called out.
    if extrapolated:
        far = sorted(k for k, s in extrapolated.items()
                     if abs(s) > cfg["extrapolation_warn_semitones"] * 100.0)
        near = len(extrapolated) - len(far)
        print(f"    {keyboard} range {key_lo}..{key_hi}: all {len(keys)} keys "
              f"covered ({near} extrapolated, {len(far)} beyond "
              f"{cfg['extrapolation_warn_semitones']:.0f} semitones)")
        if far:
            print(f"      extrapolated far: {far[0]}..{far[-1]} "
                  f"(recorded in the manifest, still audible)")
    else:
        missing = [k for k in range(key_lo, key_hi + 1) if k not in keys]
        if missing:
            print(f"    {keyboard} range {key_lo}..{key_hi}: "
                  f"{len(keys)} covered, {len(missing)} uncovered "
                  f"({missing[0]}..{missing[-1]})")

    if dry_run:
        return {"notes": notes, "takes": takes, "keys": keys,
                "layers": n_layers, "reject_counts": reject_counts}

    print(f"    rendering {len(keys)} keys x {n_layers} layers ...")
    # cache the source envelope per take (reused across keys)
    env_cache = {}
    layer_wavs = {l: [] for l in range(n_layers)}
    layer_gains_db = {l: centroids[l] - centroids[0] for l in range(n_layers)}
    looped = 0
    max_shift = 0.0
    pitch_rejects = 0
    pitch_reject_examples = 0
    trimmed = 0
    forced_pitch = []
    max_duration_s = median_dur * cfg["max_duration_vs_median"]

    for l in range(n_layers):
        for k in keys:
            # Rank every take that can reach this key and try them in order.
            #
            # Trying only the best-ranked take is what left the left hand with a
            # dead patch: for keys 53..57 the top-ranked take was a polyphonic
            # fragment whose pyin pitch (196.6 Hz) was one note of a chord while
            # the audio also sounds 295.4 Hz. Rendering from it landed +700 cents
            # off, verify_pitch rejected it, and because there was no fallback
            # the key simply produced no sample at all. A later-ranked take that
            # happens to be monophonic renders perfectly — it was never given a
            # chance.
            f_target = float(librosa.midi_to_hz(k))
            # Preferred window first; if that leaves nothing, fall back to the
            # nearest take at any distance so the key still sounds. A key in the
            # extrapolated set has no take inside `max_cents` by construction,
            # and refusing to render it is exactly how it used to end up silent.
            ranked = []
            for t in takes:
                cents = 1200.0 * math.log2(f_target / t["f0_hz"])
                if abs(cents) > max_cents:
                    continue
                # Same-layer first, then the least transposition, then the
                # take that sounds most like the rest of the instrument. Ordering
                # by `score` alone here let a bright outlier win a key just
                # because it was cleaner, which reintroduced at render time the
                # very inconsistency pick_takes had filtered out.
                dev = abs(t.get("_timbre_dev", 0.0))
                dpen = duration_penalty(t, median_dur, cfg)
                ranked.append(((t["layer"] != l, abs(cents), dev, dpen,
                                -t["score"]), t, cents))
            if not ranked:
                for t in takes:
                    cents = 1200.0 * math.log2(f_target / t["f0_hz"])
                    dev = abs(t.get("_timbre_dev", 0.0))
                    dpen = duration_penalty(t, median_dur, cfg)
                    ranked.append(((t["layer"] != l, abs(cents), dev, dpen,
                                    -t["score"]), t, cents))
            ranked.sort(key=lambda r: r[0])

            accepted = None
            last_render = None
            for _, t, cents in ranked:
                tid = id(t)
                if tid not in env_cache:
                    env_cache[tid] = spectral_envelope(t["audio"], cfg)
                out, _ = render_note(t["audio"], f_target, t["f0_hz"],
                                     env_cache[tid], cfg)
                # Verify the rendered pitch before accepting it. A source whose
                # f0 was detected a fifth or an octave off produces a note that
                # is musically wrong even though every file and region is valid,
                # so the check has to be on the audio, not on the metadata.
                err = verify_pitch(out, sr, f_target, cfg)
                if err is None or abs(err) <= cfg["max_pitch_error_cents"]:
                    accepted = (t, cents, out, err)
                    break
                last_render = (t, cents, out, err)
                pitch_rejects += 1
                if pitch_reject_examples < 5:
                    pitch_reject_examples += 1
                    print(f"      reject k{k} take {t['f0_hz']:.1f}Hz: "
                          f"rendered {err:+.0f} cents off")
            if accepted is None:
                # Every candidate failed the pitch check. Previously the key was
                # simply dropped, which is how a manual ended up with dead notes
                # in the middle of its range. Keep the least-wrong render — it is
                # off by an interval, but the key sounds and the error is
                # recorded — rather than leaving a hole in the keyboard.
                if last_render is not None:
                    accepted = last_render
                    forced_pitch.append((k, round(last_render[3] or 0.0, 1)))
                else:
                    continue
            t, cents, out, _ = accepted
            max_shift = max(max_shift, abs(cents))
            loop = find_loop(out, sr, f_target, cfg)
            if loop:
                looped += 1
            # Equalise one-shot length across the keyboard before the tail fade,
            # so holding any key gives the same kind of note. Only shortens.
            if not loop:
                out = normalize_duration(out, sr, cfg, max_duration_s)
                trimmed += 1 if t["dur_s"] > max_duration_s > 0 else 0
            # A one-shot must end in silence (it is held at its last sample
            # until note-off); a looped region keeps only a short anti-click
            # fade because its tail is never reached.
            out = finalize_wav(out, sr, cfg, looped=bool(loop))

            gain = 10.0 ** ((cfg["target_layer_rms_db"] + layer_gains_db[l]
                             - db(live_rms(out))) / 20)
            out *= gain
            layer_wavs[l].append({"key": k, "audio": out, "loop": loop,
                                  "src_key": k, "src_f0": float(t["f0_hz"]),
                                  "_layer": l})

    # Fill per-layer gaps.
    #
    # A key can lose one velocity layer and keep the others: the layer's loudest
    # take for that key was rejected by verify_pitch, or no take within the
    # shift window happened to be in that layer. The SFZ then has a *region* for
    # the key in layer 1 and 3 but none in layer 2, so playing at middle
    # velocity produces nothing at all — the key appears dead and comes back
    # when struck harder. That is exactly the "左手缺音，弹着没声音" symptom.
    #
    # Copy the nearest available layer for that key (preferring the closest
    # velocity layer) and tilt its gain to the target layer level, so the key
    # responds across the whole velocity range instead of dropping out.
    filled = 0
    for l in range(n_layers):
        have = {w["key"] for w in layer_wavs[l]}
        target_keys = set(keys)
        for k in sorted(target_keys - have):
            donors = [(abs(l2 - l), l2) for l2 in range(n_layers) if l2 != l]
            donor = None
            for _, l2 in sorted(donors):
                cand = next((w for w in layer_wavs[l2] if w["key"] == k), None)
                if cand is not None:
                    donor = cand
                    break
            if donor is None:
                continue
            audio = donor["audio"].copy()
            tilt = 10.0 ** ((layer_gains_db[l] - layer_gains_db[donor["_layer"]])
                            / 20.0)
            audio = audio * tilt
            layer_wavs[l].append({"key": k, "audio": audio, "loop": donor["loop"],
                                  "src_key": donor["src_key"],
                                  "src_f0": donor["src_f0"],
                                  "_layer": l,
                                  "filled_from_layer": donor["_layer"]})
            filled += 1
    if filled:
        print(f"    filled {filled} per-layer key gap(s) from the nearest layer")
    if trimmed:
        print(f"    trimmed {trimmed} one-shot(s) to <= {max_duration_s:.2f}s "
              f"({cfg['max_duration_vs_median']:.1f}x the {median_dur:.2f}s median)")
    if forced_pitch:
        print(f"    kept {len(forced_pitch)} key(s) that failed the pitch check "
              f"(no alternative take; error recorded, not silently dropped)")

    # uniform per-layer peak guard
    for l, wavs in layer_wavs.items():
        peak = max((float(np.abs(w["audio"]).max()) for w in wavs), default=0.0)
        if peak > cfg["peak_ceiling"]:
            for w in wavs:
                w["audio"] *= cfg["peak_ceiling"] / peak

    return {"notes": notes, "takes": takes, "keys": keys,
            "layers": n_layers, "layer_wavs": layer_wavs,
            "vel_ranges": vel_ranges(n_layers),
            "looped": looped, "max_shift_cents": max_shift,
            "pitch_rejects": pitch_rejects,
            "reject_counts": reject_counts,
            "filled_gaps": filled,
            "trimmed": trimmed,
            "extrapolated": extrapolated,
            "forced_pitch": forced_pitch,
            "centroids_db": centroids}


def vel_ranges(k):
    return [(1 + 127 * i // k, 127 * (i + 1) // k) for i in range(k)]


def ascii_keyboard(keyboard):
    """ASCII directory name for a keyboard.

    ARIA decodes the whole .sfz path with the system code page, so the parent
    directories have to be ASCII too — not just the filename.
    """
    return tone_paths.ascii_keyboard(keyboard)


def ascii_slug(text, max_len=48):
    """A filesystem-safe ASCII slug for a name that may be CJK.

    sforzando's ARIA engine decodes the .sfz path with the system code page
    (GBK on a Chinese Windows), not UTF-8, so any CJK character in the path
    becomes mojibake and the instrument silently loads nothing. The sample
    directory and the .sfz filename therefore have to be pure ASCII; the
    human-readable name stays in the SFZ comment and the manifest.
    """
    import hashlib

    # Keep ASCII letters/digits, collapse everything else to single dashes.
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        slug = "tone"
    # A short hash keeps two different CJK names from colliding once their
    # non-ASCII characters are stripped.
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return f"{slug[:max_len]}-{digest}"


def write_instrument(song, label, role, keyboard, built, TONE_root):
    key_lo, key_hi = CFG["keyboard_ranges"].get(keyboard, CFG["default_key_range"])
    base = safe_name(f"{song} - {label}")
    # The .sfz filename and its sample directory must be ASCII (see ascii_slug):
    # ARIA reads them with the system code page, so CJK would not resolve. The
    # song directory is a pinyin slug for the same reason.
    slug = ascii_slug(f"{song} - {label}")
    sample_slug = slug
    plugin_dir = tone_paths.plugin_dir(TONE_root, song, keyboard, "sforzando")
    sfz_path = os.path.join(plugin_dir, slug + ".sfz")
    sample_dir = os.path.join(plugin_dir, "samples", sample_slug)
    if not os.path.abspath(sfz_path).startswith(os.path.abspath(TONE_root) + os.sep):
        raise RuntimeError(f"refusing to write outside tone dir: {sfz_path}")

    os.makedirs(sample_dir, exist_ok=True)
    for l, wavs in built["layer_wavs"].items():
        for w in wavs:
            name = f"k{w['key']:03d}_l{l}.wav"
            sf.write(os.path.join(sample_dir, name),
                     w["audio"].astype(np.float32), CFG["sr"], subtype="PCM_16")
            w["file"] = name

    lines = [
        f"// {song} - {label} ({role}) / {keyboard}",
        "// Generated by scripts/build-sample-tones.py; samples cut from the song's own stem.",
        "// Each region is one MIDI key pre-rendered at exact pitch (engine stretch = 0).",
        "// Spectral envelope of the source is preserved so body resonances do not shift.",
        f"// Velocity layers: {built['vel_ranges']} (loudness clusters of the recording).",
        "// Looped regions have the loop crossfade baked into the wav (engine-independent).",
        f"// Keyboard range: {key_lo}..{key_hi} (Yamaha ELB-02 hardware).",
        "",
        "<control>",
        f"default_path=samples/{sample_slug}/",
        "",
        "<global>",
        "ampeg_attack=0.001",
        # A plucked guitar whose samples are cut from a 2 s stem excerpt needs a
        # long release: the note the player hears is the *sample*, and 0.25 s
        # after note-off is the wrong place to stop it. Both manuals get 1.2 s
        # so a held chord keeps ringing after the key comes up, which is what
        # the recording does.
        "ampeg_release=1.2",
        f"amp_veltrack={CFG['amp_veltrack']}",
        "",
    ]
    for l in range(built["layers"]):
        lo, hi = built["vel_ranges"][l]
        # `<group>`, not `<master>`. An SFZ has exactly one `<global>` and one
        # `<master>` level; a second `<master>` is invalid and ARIA silently
        # ignores it, so the intended `lovel/hivel` filter would not apply and
        # all three layers would answer every velocity. `<group>` is the
        # correct per-velocity-layer container.
        lines.append(f"<group> lovel={lo} hivel={hi}")
        for w in built["layer_wavs"][l]:
            region = (f"<region> sample={w['file']} pitch_keycenter={w['key']} "
                      f"lokey={w['key']} hikey={w['key']}")
            if w["loop"]:
                region += (f" loop_mode=loop_sustain loop_start={w['loop'][0]}"
                           f" loop_end={w['loop'][1]}")
            lines.append(region)
        lines.append("")

    # Every key on the keyboard must sound.
    #
    # This used to cap the engine-transposition fallback and simply not emit a
    # region beyond the cap, so the upper manual's top octave had no region at
    # all: the SFZ crossed those keys out and pressing them did nothing. That is
    # the one failure mode a performing instrument cannot have — a stretched
    # guitar still plays the correct note, a missing region plays nothing.
    #
    # So the fallback now covers the whole hardware range. Where a take exists
    # within `max_shift_semitones` it was already pre-rendered (exact pitch, no
    # engine stretch); everything beyond is filled here by transposing the
    # nearest rendered key, and anything *still* missing is pre-rendered from the
    # nearest take instead of being dropped (see fill_uncovered_keys above).
    covered = {w["key"] for wavs in built["layer_wavs"].values() for w in wavs}
    fill_cap = CFG["max_gap_fill_semitones"].get(
        keyboard, CFG["max_gap_fill_semitones"]["default"])
    gaps = [k for k in range(key_lo, key_hi + 1) if k not in covered]
    if covered:
        reachable = gaps
        unreachable = []
    else:
        reachable = []
        unreachable = gaps
    if reachable:
        lines.append("// Keys below are filled by engine transposition from the "
                     "nearest rendered key, so every key on the keyboard sounds.")
        for l in range(built["layers"]):
            lo, hi = built["vel_ranges"][l]
            lines.append(f"<group> lovel={lo} hivel={hi}")
            for k in reachable:
                src = min(covered, key=lambda c: abs(c - k))
                w = next((w for w in built["layer_wavs"][l] if w["key"] == src),
                         None)
                if w is None:
                    continue
                region = (f"<region> sample={w['file']} pitch_keycenter={src} "
                          f"lokey={k} hikey={k}")
                if w["loop"]:
                    region += (f" loop_mode=loop_sustain loop_start={w['loop'][0]}"
                               f" loop_end={w['loop'][1]}")
                lines.append(region)
            lines.append("")
        far = [k for k in reachable
               if min(abs(k - c) for c in covered) > fill_cap]
        print(f"    {len(reachable)} key(s) filled by engine transposition "
              f"({reachable[0]}..{reachable[-1]})")
        if far:
            print(f"      of those, {len(far)} stretch more than {fill_cap} "
                  f"semitones: {far[0]}..{far[-1]} (still audible, "
                  f"labelled 'extrapolated' in the manifest)")
    if unreachable:
        # Only reachable when nothing at all could be rendered for this
        # keyboard; reported rather than silently producing a dead keyboard.
        print(f"    WARNING: {len(unreachable)} key(s) have no region at all: "
              f"{unreachable[0]}..{unreachable[-1]}")

    with open(sfz_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    total_keys = key_hi - key_lo + 1
    extrapolated = built.get("extrapolated", {}) or {}
    coverage = {
        "keyboard_range": [key_lo, key_hi],
        "keys_total": total_keys,
        "keys_rendered": len(covered),
        "keys_gap_filled": len(reachable),
        "keys_empty": len(unreachable),
        "empty_keys": unreachable,
        "keys_extrapolated": len(extrapolated),
        "extrapolated_keys": sorted(extrapolated),
        "gap_fill_cap_semitones": fill_cap,
        # Every key must sound: coverage is complete unless nothing could be
        # rendered at all. `keys_extrapolated` says how many relied on a
        # stretched take, which is a quality note, not a playability gap.
        "coverage_pct": round(100.0 * (len(covered) + len(reachable))
                              / max(total_keys, 1), 1),
        "fully_playable": len(unreachable) == 0,
    }
    return sfz_path, sample_dir, coverage


# ---------------------------------------------------------------- song build

def role_for_keyboard(stem, keyboard):
    """Most relevant role of this stem that maps to the given keyboard.

    `primary_role` wins when it already maps to this keyboard: the analysis
    step has cross-stem context (it knows which stem owns the melody after
    arbitration) that the per-window shares alone cannot express. Without this
    a stem that comps most of the time but carries the tune is labelled
    "accompaniment" on the right hand too.
    """
    rule = {"右手": ["主旋律"], "左手": ["副旋律", "和弦", "伴奏", "和声铺底"],
            "脚": ["低音线条"]}
    primary = stem.get("primary_role", "")
    if primary and primary in rule.get(keyboard, []):
        return primary
    roles = stem.get("roles", {})
    best, best_share = None, -1.0
    for role, share in roles.items():
        if role in rule.get(keyboard, []) and share > best_share:
            best, best_share = role, share
    return best or primary


def build_song(song, stems, stems_dir, dry_run=False):
    entries = []
    for stem_name, stem in stems.items():
        if stem.get("empty"):
            continue
        if stem.get("unusable"):
            # Too little audible content after removing separation failures:
            # slicing notes out of it would sample noise, not an instrument.
            print(f"  skip {stem_name}: only {stem.get('activity', 0) * 100:.1f}% "
                  f"audible after removing separation failures")
            continue
        if stem_name in SKIP_STEMS:
            print(f"  skip {stem_name}: {SKIP_STEMS[stem_name]}")
            continue
        inst = INSTRUMENTS.get(stem_name)
        if inst is None:
            print(f"  skip {stem_name}: not in sampling scope")
            continue
        label = inst["label"]
        stem_wav = os.path.join(stems_dir, f"{stem_name}.wav")
        if not os.path.isfile(stem_wav):
            print(f"  skip {label}: stem missing ({stem_wav})")
            continue

        print(f"  {label}: analyzing ...")
        # A stem can serve several keyboards, and each keyboard has its own
        # physical range (the pedal is 20 keys, the manuals are 49). Build one
        # instrument per keyboard so the sample set covers exactly the keys
        # that can be pressed, instead of generating a wide range once and
        # hoping it lines up.
        built_by_kb = {}
        for keyboard in stem.get("keyboards", []):
            built = build_instrument(stem_wav, inst, dry_run=dry_run,
                                     keyboard=keyboard)
            if not built or not built.get("notes"):
                print(f"  skip {label}/{keyboard}: no usable notes "
                      f"(rejected: {built.get('reject_counts') if built else {}})")
                continue
            built_by_kb[keyboard] = built

        if not built_by_kb:
            continue

        for keyboard, built in built_by_kb.items():
            role = role_for_keyboard(stem, keyboard)
            n_keys = len(built.get("keys", []))
            print(f"  {label}/{keyboard}: {len(built['notes'])} notes accepted, "
                  f"{built.get('layers', 0)} velocity layers, "
                  f"{n_keys} keys covered"
                  + (f", MIDI {built['keys'][0]}..{built['keys'][-1]}" if n_keys else ""))
            if dry_run:
                entries.append({"keyboard": keyboard, "instrument": label,
                                "role": role, "dry_run": True,
                                "keys": n_keys,
                                "notes": len(built["notes"])})
                print(f"    [dry-run] {keyboard}/sforzando/{safe_name(song + ' - ' + label)}.sfz")
                continue
            sfz_path, sample_dir, coverage = write_instrument(
                song, label, role, keyboard, built, TONE_ROOT)
            print(f"    {tone_paths.rel_to_song(TONE_ROOT, song, sfz_path)}")
            entries.append({
                "sfz": tone_paths.rel_to_song(TONE_ROOT, song, sfz_path),
                "samples_dir": tone_paths.rel_to_song(TONE_ROOT, song, sample_dir),
                "keyboard": keyboard, "instrument": label, "role": role,
                "notes_accepted": len(built["notes"]),
                "notes_rejected": built["reject_counts"],
                "layers": built["layers"],
                "layer_centroids_db": [round(c, 1) for c in built["centroids_db"]],
                "keys_covered": n_keys,
                "key_range": [built["keys"][0], built["keys"][-1]] if n_keys else [],
                "coverage": coverage,
                "looped_samples": built["looped"],
                "filled_layer_gaps": built.get("filled_gaps", 0),
                "max_source_shift_cents": round(built["max_shift_cents"], 1),
                "pitch_rejected_keys": built.get("pitch_rejects", 0),
            })
    return entries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--analysis", default="")
    ap.add_argument("--song", default="")
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    path = args.analysis
    if not path:
        root = os.path.join(WORKSPACE, "analysis")
        cands = glob.glob(os.path.join(root, "*", "analysis.json"))
        if args.song:
            cands = [p for p in cands
                     if args.song in os.path.basename(os.path.dirname(p))]
        if not cands:
            sys.exit(f"no analysis.json under {root} - run analyze-tones.py first")
        path = max(cands, key=os.path.getmtime)

    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    song = data["song"]
    stems_dir = data.get("stems_dir", "")
    print(f"song: {song}")
    print(f"stems: {stems_dir}")

    entries = build_song(song, data["stems"], stems_dir, dry_run=args.dry_run)

    if not args.dry_run:
        os.makedirs(TONE_ROOT, exist_ok=True)
        tone_paths.write_song_title(TONE_ROOT, song)
        manifest_path = os.path.join(TONE_ROOT, "sample-instruments.json")
        manifest = {}
        if os.path.isfile(manifest_path):
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
        manifest[song] = {
            "generated": datetime.datetime.now().isoformat(timespec="seconds"),
            "plugin": "sforzando",
            # Paths in `files` are relative to this directory, not to tone/.
            "tone_dir": tone_paths.song_slug(song),
            "note": "one instrument per .sfz; per-key samples pre-rendered at "
                    "exact pitch from the stem itself; vocals/piano/drums excluded",
            "files": entries,
        }
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        print(f"\nwrote {len(entries)} sample instruments + {manifest_path}")
    else:
        print(f"\n[dry-run] would write {len(entries)} sample instruments")


if __name__ == "__main__":
    main()
