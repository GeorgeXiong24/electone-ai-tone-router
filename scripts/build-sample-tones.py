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
    "max_shift_semitones": 3.0,
    # The pedal keyboard is the exception. Its 20 keys span C2..G3 while a bass
    # stem typically only plays A1..D#2, so the upper half of the pedal has no
    # source material at all. Pre-rendering those keys would mean stretching a
    # bass by a whole octave, which no longer sounds like a bass; instead the
    # pedal gets a modest window and the remaining keys are filled by the SFZ
    # engine transposing the nearest rendered key (see write_instrument).
    "max_shift_semitones_by_keyboard": {
        "脚": 5.0,
    },
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
    "fade_ms": 8.0,
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
    "loop_corr_min": 0.95,
    "loop_max_decay_db": 3.0,
    "loop_k_max": 40,
    "loop_max_len_ratio": 0.4,
    "loop_window_s": 0.2,
    "loop_margin_s": 0.25,
    "loop_xfade_ms": 20.0,
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


def refine_f0(seg, sr, f0_hz, cfg):
    """Return the true fundamental of a segment, correcting harmonic errors.

    pyin reports the strongest periodicity, which on a bright or noisy source
    is often a harmonic rather than the fundamental — a fifth above (ratio 1.5)
    and an octave above (ratio 2.0) are the common cases. Transposing from a
    wrong f0 makes every rendered key a fixed interval off, so the instrument
    plays wrong intervals even though every file and region is valid.

    The check is spectral: a true fundamental has energy at f0 and its
    harmonics, while a harmonic mistaken for f0 has little or no energy at
    f0/2 or f0/3. Walk down by the usual ratios and keep the lowest candidate
    that still has real energy at its own fundamental.
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

    ref = energy_at(f0_hz)
    if ref <= 0:
        return f0_hz

    # Candidate sub-harmonics, strongest first. A real fundamental must carry
    # a meaningful fraction of the energy its harmonic does.
    for ratio in (2.0, 3.0, 1.5):
        cand = f0_hz / ratio
        if cand < cfg["f0_min_hz"]:
            continue
        e = energy_at(cand)
        if e >= ref * cfg["subharmonic_energy_ratio"]:
            return cand
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


def pick_takes(notes):
    """Best take per (rounded midi, layer)."""
    best = {}
    for n in notes:
        key = (n["midi"], n["layer"])
        cur = best.get(key)
        if cur is None or n["score"] > cur["score"]:
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


def finalize_wav(seg, sr, cfg):
    """DC removal, 20 Hz high-pass, short fades at both ends."""
    seg = seg - np.mean(seg)
    sos = scipy.signal.butter(2, cfg["highpass_hz"] / (sr / 2), "highpass",
                              output="sos")
    seg = scipy.signal.sosfiltfilt(sos, seg) if len(seg) > 60 else seg
    fade = min(int(cfg["fade_ms"] * sr / 1000), len(seg) // 4)
    if fade > 1:
        seg[:fade] *= np.linspace(0, 1, fade)
        seg[-fade:] *= np.linspace(1, 0, fade)
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

    # velocity layers on loudness
    n_layers = 3 if len(notes) >= 12 else (2 if len(notes) >= 6 else 1)
    n_layers = min(n_layers, cfg["layers"])
    labels, centroids = kmeans1d([n["rms_db"] for n in notes], n_layers)
    for n, l in zip(notes, labels):
        n["layer"] = int(l)

    takes = pick_takes(notes)

    # coverage: keys reachable from some take within the shift cap, limited to
    # the keys this keyboard physically has.
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
    keys = sorted(keys)

    # Report the gap honestly: a keyboard whose source material sits below its
    # own range cannot be covered by transposition alone.
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

    for l in range(n_layers):
        for k in keys:
            best = None
            for t in takes:
                cents = 1200.0 * math.log2(librosa.midi_to_hz(k) / t["f0_hz"])
                if abs(cents) > max_cents:
                    continue
                rank = (t["layer"] != l, abs(cents), -t["score"])
                if best is None or rank < best[0]:
                    best = (rank, t, cents)
            if best is None:
                continue
            _, t, cents = best
            max_shift = max(max_shift, abs(cents))
            tid = id(t)
            if tid not in env_cache:
                env_cache[tid] = spectral_envelope(t["audio"], cfg)
            out, _ = render_note(t["audio"], librosa.midi_to_hz(k),
                                 t["f0_hz"], env_cache[tid], cfg)

            # Verify the rendered pitch before accepting it. A source whose f0
            # was detected a fifth or an octave off produces a note that is
            # musically wrong even though every file and region is valid, so
            # the check has to be on the audio, not on the metadata.
            err = verify_pitch(out, sr, librosa.midi_to_hz(k), cfg)
            if err is not None and abs(err) > cfg["max_pitch_error_cents"]:
                pitch_rejects += 1
                if pitch_reject_examples < 5:
                    pitch_reject_examples += 1
                    print(f"      reject k{k}: rendered {err:+.0f} cents off "
                          f"(source f0 {t['f0_hz']:.1f} Hz)")
                continue
            loop = find_loop(out, sr, librosa.midi_to_hz(k), cfg)
            if loop:
                looped += 1
            out = finalize_wav(out, sr, cfg)

            gain = 10.0 ** ((cfg["target_layer_rms_db"] + layer_gains_db[l]
                             - db(live_rms(out))) / 20)
            out *= gain
            layer_wavs[l].append({"key": k, "audio": out, "loop": loop})

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
            "centroids_db": centroids}


def vel_ranges(k):
    return [(1 + 127 * i // k, 127 * (i + 1) // k) for i in range(k)]


def ascii_keyboard(keyboard):
    """ASCII directory name for a keyboard.

    ARIA decodes the whole .sfz path with the system code page, so the parent
    directories have to be ASCII too — not just the filename.
    """
    return {"右手": "right", "左手": "left", "脚": "pedal"}.get(keyboard, "kb")


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
    # ARIA reads them with the system code page, so CJK would not resolve.
    slug = ascii_slug(f"{song} - {label}")
    sample_slug = slug
    plugin_dir = os.path.join(TONE_root, ascii_keyboard(keyboard), "sforzando")
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
        "ampeg_release=0.25",
        f"amp_veltrack={CFG['amp_veltrack']}",
        "",
    ]
    for l in range(built["layers"]):
        lo, hi = built["vel_ranges"][l]
        lines.append(f"<master> lovel={lo} hivel={hi}")
        for w in built["layer_wavs"][l]:
            region = (f"<region> sample={w['file']} pitch_keycenter={w['key']} "
                      f"lokey={w['key']} hikey={w['key']}")
            if w["loop"]:
                region += (f" loop_mode=loop_sustain loop_start={w['loop'][0]}"
                           f" loop_end={w['loop'][1]}")
            lines.append(region)
        lines.append("")

    # Keys the pre-rendered set does not reach are covered by letting the
    # engine transpose the nearest rendered key. A bass stem only plays A1..D#2
    # while the pedal spans C2..G3, so the top of the pedal has no source
    # material; stretching one sample a few semitones is far better than a dead
    # key, and it keeps the sample itself at a pitch where the instrument still
    # sounds like itself.
    #
    # Only keys with no rendered region at all are filled: emitting a second
    # region for a key that already has one would stack two voices on that key
    # (louder, and phasey where the two copies differ).
    covered = {w["key"] for wavs in built["layer_wavs"].values() for w in wavs}
    gaps = [k for k in range(key_lo, key_hi + 1) if k not in covered]
    if gaps:
        lines.append("// Keys below are filled by engine transposition from the "
                     "nearest rendered key.")
        for l in range(built["layers"]):
            lo, hi = built["vel_ranges"][l]
            lines.append(f"<master> lovel={lo} hivel={hi}")
            for k in gaps:
                src = min(covered, key=lambda c: abs(c - k))
                w = next(w for w in built["layer_wavs"][l] if w["key"] == src)
                region = (f"<region> sample={w['file']} pitch_keycenter={src} "
                          f"lokey={k} hikey={k}")
                if w["loop"]:
                    region += (f" loop_mode=loop_sustain loop_start={w['loop'][0]}"
                               f" loop_end={w['loop'][1]}")
                lines.append(region)
            lines.append("")
        print(f"    {len(gaps)} key(s) filled by engine transposition: "
              f"{gaps[0]}..{gaps[-1]}")

    with open(sfz_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return sfz_path, sample_dir


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
            sfz_path, sample_dir = write_instrument(
                song, label, role, keyboard, built, TONE_ROOT)
            print(f"    {os.path.relpath(sfz_path, TONE_ROOT)}")
            entries.append({
                "sfz": os.path.relpath(sfz_path, TONE_ROOT),
                "samples_dir": os.path.relpath(sample_dir, TONE_ROOT),
                "keyboard": keyboard, "instrument": label, "role": role,
                "notes_accepted": len(built["notes"]),
                "notes_rejected": built["reject_counts"],
                "layers": built["layers"],
                "layer_centroids_db": [round(c, 1) for c in built["centroids_db"]],
                "keys_covered": n_keys,
                "key_range": [built["keys"][0], built["keys"][-1]] if n_keys else [],
                "looped_samples": built["looped"],
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
        manifest_path = os.path.join(TONE_ROOT, "sample-instruments.json")
        manifest = {}
        if os.path.isfile(manifest_path):
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
        manifest[song] = {
            "generated": datetime.datetime.now().isoformat(timespec="seconds"),
            "plugin": "sforzando",
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
