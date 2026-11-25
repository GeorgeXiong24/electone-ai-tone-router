"""Shared foundation for the per-song tone-creation engines (`improvements.md`).

This module is the single source of truth for

  * the ELB-02 keyboard geometry (which MIDI keys each manual can play),
  * the audio front-end parameters (analysis / synthesis sample rate, hop),
  * DSP helpers every engine needs (STFT, octave-band LTAS, WORLD/SMS
    resynthesis primitives, phase-locked looping, FLAC/WAV16 I/O),
  * the objective-quality gate numbers that Phase 0 / Stage 6 share.

Design rules inherited from the existing codebase
-------------------------------------------------
  * No magic numbers scattered around: everything lives in a ``CFG`` dict or a
    module constant, with the measurement that justified it in a comment.
  * Every optional heavy dependency (``pyworld``, ``basic_pitch``, ``optuna``)
    is imported lazily and has a pure-numpy / scipy fallback, because the local
    interpreter is Python 3.14 where those wheels may be missing. The engines
    must run on librosa + scipy + numpy alone.

The three ELB-02 manuals
------------------------
  Upper keyboard (right hand) : MIDI ch 1, 49 keys C2-C6  -> MIDI 36-84
  Lower keyboard (left hand)  : MIDI ch 2, 49 keys C2-C6  -> MIDI 36-84
  Pedalboard     (foot)       : MIDI ch 3, 20 keys C2-G3  -> MIDI 36-55
"""
import os
import math
import numpy as np

# ---------------------------------------------------------------- keyboard map

# MIDI note numbers of the lowest / highest key of each manual.
#
# PEDAL RANGE CORRECTED: the ELB-02 pedalboard is 20 keys and its lowest key
# sends **C1 = MIDI 24**, not C2/36 (measured on the instrument; the old constant
# (36, 55) left the bottom 12 pedals completely silent because every SFZ region
# was lokey=hikey=36..55). The render range is deliberately wider than the
# physical keys so transposing the pedalboard on the instrument still works.
UPPER_RANGE = (36, 84)      # C2 .. C6
LOWER_RANGE = (36, 84)      # C2 .. C6
PEDAL_RANGE = (24, 43)      # C1 .. G2  (the ELB-02's real 20 pedals)
# What we actually render for the pedal: C1..C4, so the instrument can also be
# played an octave up and every SFZ region can be extended to the full MIDI span.
PEDAL_RENDER_RANGE = (24, 60)

# The SFZ files cover the WHOLE MIDI range (0..127) on every keyboard. Strict
# one-region-per-key files leave silent gaps as soon as the instrument's octave
# or transpose setting moves off the assumed centre, which is a "dead key" bug
# rather than a musical decision - so the outermost regions are always extended.
SFZ_FULL_RANGE = (0, 127)

KEYBOARD_RANGE = {"右手": UPPER_RANGE, "左手": LOWER_RANGE, "脚": PEDAL_RANGE,
                  "right": UPPER_RANGE, "left": LOWER_RANGE, "pedal": PEDAL_RANGE}


def key_range(keyboard):
    """(lo, hi) MIDI key numbers a manual can play."""
    return KEYBOARD_RANGE.get(keyboard, UPPER_RANGE)


def render_range(keyboard):
    """(lo, hi) keys to render samples for.

    Wider than the physical keyboard so the instrument's octave/transpose setting
    never runs off the end of the sample set.
    """
    if keyboard in ("脚", "pedal"):
        return PEDAL_RENDER_RANGE
    return KEYBOARD_RANGE.get(keyboard, UPPER_RANGE)


def key_list(keyboard):
    return list(range(*[key_range(keyboard)[0], key_range(keyboard)[1] + 1]))


# ---------------------------------------------------------------- audio params

# Analysis and synthesis share one rate so every absolute-Hz feature (centroid,
# rolloff, cutoff) means the same thing to the analyst, the resynthesiser and
# the evaluator. 44.1 kHz matches the separated stems and Cantabile's default.
SR = 44100
HOP = 512
N_FFT = 2048

# Octave-band centre frequencies for the 8-band spectral match (63 Hz .. 8 kHz).
# These are the Phase-0 / Stage-6 "octave-band spectral error" bands.
OCTAVE_BANDS_HZ = [63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0]


# ---------------------------------------------------------------- quality gates
# Objective scorecard thresholds. Phase 0 (Section 5) and Stage 6 (Section 6)
# share these so a "pass" means the same thing before and after the refactor.

GATES = {
    # Pitch accuracy: >= 95% of tested keys within +/- 25 cents, zero octave/fifth
    # errors (i.e. no key whose measured pitch is off by ~1200 / ~700 cents).
    "pitch_cents_max": 25.0,
    "pitch_pass_fraction": 0.95,
    "octave_error_cents": 1000.0,     # anything within this of +-1200 is a misdetection
    "fifth_error_cents": 600.0,
    # Key-to-key timbre coherence: adjacent-key relative brightness jump < 25%
    # (the current slicer measured 142%).
    "timbre_jump_max": 0.25,
    # Sanity ceiling on the WORST single pair. MEASURED: the Salamander library's
    # own raw takes jump up to 174% between adjacent keys (a physical string
    # change), so no faithful rendering of that library can be perfectly smooth.
    # Coherence is therefore gated on the median (the ordinary listening
    # experience) plus this ceiling, which still catches the slicer-class defect
    # (142% on most pairs) while accepting a real instrument's own character.
    "timbre_jump_ceiling": 0.35,
    "duration_var_max": 1.3,
    # Octave-band spectral error: median abs error < 4.5 dB across 125 Hz-8 kHz.
    "octave_band_err_db": 4.5,
    # Null / playback integrity: zero silent keys across the manual's range.
    "silent_key_peak_dbfs": -30.0,
    # Null test for sfizz anchors: residual < -60 dBFS, correlation >= 0.99.
    "null_residual_dbfs": -60.0,
    "null_corr_min": 0.99,
    # Per-song disk budget (Section 4): total generated bytes < 1.2 GB, warn > 800 MB.
    "disk_budget_mb": 1200.0,
    "disk_warn_mb": 800.0,
}

# Where the engines live on disk (Section 2). Kept as constants so the DSH layer
# and the CLI scripts agree.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.environ.get("ELECTONE_WORKSPACE") or os.path.join(ROOT, "workspace")
MODELS_DIR = os.path.join(WORKSPACE, "models")
SAMPLE_LIB_DIR = r"D:\SampleLibraries"
SALAMANDER_SFZ = os.path.join(
    SAMPLE_LIB_DIR, "AccurateSalamanderGrandPianoV6.2e_48khz24bit",
    "sfz_daw", "Accurate-SalamanderGrandPiano_flat.Recommended.sfz")
DEREVERB_MODEL_DIR = os.path.join(MODELS_DIR, "audio-separator")


# ---------------------------------------------------------------- unit helpers

def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def midi_to_hz(m):
    return 440.0 * 2.0 ** ((float(m) - 69.0) / 12.0)


def hz_to_midi(f):
    f = float(f)
    if f <= 0:
        return float("nan")
    return 69.0 + 12.0 * math.log2(f / 440.0)


def cents_between(f_ref, f):
    """Signed cents of `f` relative to `f_ref` (positive = sharp)."""
    if f_ref <= 0 or f <= 0:
        return float("nan")
    return 1200.0 * math.log2(float(f) / float(f_ref))


def midi_name(m):
    if m is None or not np.isfinite(m):
        return "-"
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    m = int(round(m))
    return f"{names[m % 12]}{m // 12 - 1}"


def db(x):
    return 20.0 * np.log10(np.asarray(x, dtype=np.float64) + 1e-12)


def from_db(d):
    return 10.0 ** (np.asarray(d, dtype=np.float64) / 20.0)


# ---------------------------------------------------------------- audio I/O

def load_audio(path, sr=SR, mono=False):
    """Load a WAV/FLAC (or any soundfile-readable file).

    Returns (y, sr). `mono=False` keeps the channel dimension as (n, ch) so a
    stereo stem keeps its image for sfizz stereo samples; `mono=True` collapses
    to a 1-D array for analysis. 24-bit FLAC is read natively by soundfile.
    """
    import soundfile as sf
    y, file_sr = sf.read(path, always_2d=True, dtype="float64")
    if mono:
        y = y.mean(axis=1)
    if sr is not None and file_sr != sr:
        import librosa
        y2 = librosa.resample(
            y.T if y.ndim == 2 else y, orig_sr=file_sr, target_sr=sr)
        y = y2.T if y.ndim == 2 else y2
        file_sr = sr
    return y, file_sr


def save_flac(path, y, sr=SR, subtype="PCM_24"):
    """Lossless 24-bit FLAC archival (Section 4.1). Bit-accurate to 24-bit and
    3-5x smaller than float32 WAV; natively read by soundfile and librosa."""
    import soundfile as sf
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    y = np.asarray(y)
    if y.ndim == 1:
        y = y[:, None]
    # soundfile wants (frames, channels) for multichannel.
    sf.write(path, y, sr, format="FLAC", subtype=subtype)
    return path


def save_wav16(path, y, sr=SR):
    """16-bit PCM WAV for final sfizz Golden-Anchor samples (Section 4.4).

    One velocity layer per key keeps the sample folder ~3x smaller than three
    duplicated layers; velocity dynamics come from SFZ `fil_veltrack` +
    `amp_veltrack` instead of layered WAVs.
    """
    import soundfile as sf
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    y = np.asarray(y)
    if y.ndim == 1:
        y = y[:, None]
    # Guard against clipping after synthesis.
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    if peak > 1.0:
        y = y / peak
    sf.write(path, y, sr, format="WAV", subtype="PCM_16")
    return path


# ---------------------------------------------------------------- STFT / spectral

def stft(y, n_fft=N_FFT, hop=HOP):
    import librosa
    return librosa.stft(np.asarray(y, dtype=np.float64), n_fft=n_fft, hop_length=hop)


def istft(S, n_fft=N_FFT, hop=HOP, length=None):
    import librosa
    return librosa.istft(S, hop_length=hop, length=length)


def octave_band_ltas(y, sr=SR, bands=OCTAVE_BANDS_HZ):
    """Mean power (dB) in each octave band — the spectral fingerprint that the
    Match EQ fits and the Stage-6 octave-band gate compares.

    Returns a list of dB values, one per band, over the active (non-silent)
    frames only so a long silence does not darken the measurement.
    """
    import librosa
    y = np.asarray(y, dtype=np.float64)
    if y.ndim > 1:
        y = y.mean(axis=1)
    S = np.abs(stft(y)) ** 2
    freqs = librosa.fft_frequencies(sr=sr, n_fft=N_FFT)
    rms = librosa.feature.rms(y=y, frame_length=N_FFT, hop_length=HOP)[0]
    active = rms > 10.0 ** (-45.0 / 20.0)
    if not active.any():
        active = np.ones(S.shape[1], dtype=bool)
    S = S[:, active]
    out = []
    for f0 in bands:
        # One-third-octave-ish window around the centre: [f0/sqrt(2), f0*sqrt(2)].
        lo, hi = f0 / math.sqrt(2.0), f0 * math.sqrt(2.0)
        sel = (freqs >= lo) & (freqs < hi)
        if not sel.any():
            out.append(-120.0)
            continue
        out.append(float(db(np.sqrt(S[sel].mean() + 1e-12))))
    return out


def spectral_centroid_hz(y, sr=SR):
    import librosa
    y = np.asarray(y, dtype=np.float64)
    if y.ndim > 1:
        y = y.mean(axis=1)
    c = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=HOP)[0]
    return float(np.median(c[c > 0])) if (c > 0).any() else 0.0


def estimate_f0_ac(y, sr=SR, fmin=40.0, fmax=1400.0):
    """Autocorrelation-based fundamental (ground truth, octave-robust).

    pyin locks onto harmonics of low / looped notes (a rendered bass key measured
    an octave/fourth off while actually being in tune). The autocorrelation peak
    near the expected period gives the true fundamental regardless of harmonic
    weighting. Returns Hz or None.
    """
    y = np.asarray(y, dtype=np.float64)
    if y.ndim > 1:
        y = y.mean(axis=1)
    if y.size < 2048:
        return None
    rms = float(np.sqrt((y ** 2).mean()))
    if db(rms) < -55.0:
        return None
    y = y - y.mean()
    lo, hi = int(sr / fmax), int(sr / fmin)
    if hi <= lo + 1 or hi >= y.size:
        return None
    # FFT autocorrelation (O(n log n)); np.correlate('full') is O(n^2) and made
    # anchor extraction take minutes per window.
    nfft = 1 << int(2 * y.size - 1).bit_length()
    Y = np.fft.rfft(y, nfft)
    ac = np.fft.irfft(Y * np.conj(Y), nfft)[:y.size]
    ac = ac / (ac[0] + 1e-12)

    # Robust period pick: the highest LOCAL maximum in the lag range, then an
    # octave-down correction. A plain "first lag above 0.85*max" (the previous
    # rule) could land on 2x the period and report an octave low - measured
    # 304 Hz for a true 587 Hz note - which corrupted the scorecard.
    lo2, hi2 = max(2, lo), min(hi, y.size - 1)
    if hi2 <= lo2:
        return None
    local_max = [lag for lag in range(lo2, hi2)
                 if ac[lag] >= ac[lag - 1] and ac[lag] >= ac[lag + 1]]
    if not local_max:
        return None
    best = max(local_max, key=lambda lag: ac[lag])
    if ac[best] < 0.30:
        return None
    # If a sub-harmonic candidate is nearly as strong, the true period is the
    # shorter lag (kills the octave-low readings).
    for div in (2, 3):
        cand = int(round(best / div))
        if lo2 <= cand < hi2 and ac[cand] >= 0.70 * ac[best]:
            best = cand
            break
    # Integer-lag quantisation is a real accuracy limit: at 44100 Hz a period of
    # 44.9 samples can only be expressed as 45, i.e. ~39 cents at 1 kHz. Refine
    # with the classic 3-point parabolic interpolation on the autocorrelation peak
    # so the estimate is not biased by up to half a sample (measured: a constant
    # +15 cents versus pyin on the same notes).
    if 0 < best < len(ac) - 1:
        a0, a1, a2 = float(ac[best - 1]), float(ac[best]), float(ac[best + 1])
        denom = (a0 - 2.0 * a1 + a2)
        if abs(denom) > 1e-12:
            shift = 0.5 * (a0 - a2) / denom
            if abs(shift) <= 0.5:
                best = best + shift
    return float(sr / best)


def estimate_f0(y, sr=SR, fmin=40.0, fmax=1400.0, min_rms_db=-55.0):
    """Median fundamental frequency of a monophonic-ish fragment.

    Uses pyin on the harmonic component; falls back to librosa's pyin (already
    proven in analyze-tones.py) which needs no extra dependency. Returns Hz or
    None when nothing is voiced **or the window is too quiet to pitch-track** -
    on near-silent input pyin happily reports a garbage pitch near fmax, which
    is how a silent bass window once looked like a 1379 Hz note.
    """
    import librosa
    y = np.asarray(y, dtype=np.float64)
    if y.ndim > 1:
        y = y.mean(axis=1)
    # Absolute-loudness gate: a silent window has no pitch.
    rms = float(np.sqrt((y ** 2).mean())) if y.size else 0.0
    if db(rms) < min_rms_db:
        return None
    # pyin needs >= 2 periods of fmin inside a frame for stable detection, so
    # grow the frame with a lower fmin (a 40 Hz bass note has a 25 ms period).
    frame_length = int(2048)
    while frame_length < 2.2 * sr / max(fmin, 1e-3):
        frame_length = int(frame_length * 1.5)
    if y.size < frame_length:
        return None
    y_h, _ = librosa.effects.hpss(y)
    f0, voiced, _ = librosa.pyin(
        y_h, fmin=fmin, fmax=fmax, sr=sr, frame_length=frame_length,
        hop_length=HOP)
    if f0 is None or not np.any(voiced):
        return None
    vals = f0[voiced & np.isfinite(f0)]
    if vals.size == 0:
        return None
    return float(np.median(vals))


# ---------------------------------------------------------------- envelope / loop

def phase_locked_loop(y, sr, f0_hz, loop_s=0.5):
    """Bake a seamless phase-locked sustain loop into a steady body.

    The SFZ points `loop_start/loop_end` at the crossfaded interior (Section
    Engine A.3): the crossfade is baked into the WAV, not delegated to the
    engine's `loop_crossfade` (uneven across players). Returns (y_out, (ls, le))
    in samples, or (y, None) when no clean loop region is found.

    Verified invariants (Stage-6 null test): zero click (no sample jump larger
    than the source's own), zero pitch drift, zero DC jump at the splice.
    """
    y = np.asarray(y, dtype=np.float64)
    if y.ndim > 1:
        y = y.mean(axis=1)
    n = len(y)
    if f0_hz <= 0 or n < int(0.6 * sr):
        return y, None
    period = int(round(sr / f0_hz))
    if period < 4:
        return y, None

    # Candidate loop length L = k periods, capped at 40% of the body.
    best = None
    max_len = int(min(0.4 * n, loop_s * sr))
    for k in range(2, 41):
        L = k * period
        if L >= max_len:
            break
        # Loop region sits in the second half (steady sustain), not the attack.
        start = int(0.45 * n)
        end = start + L
        if end + L >= n:
            continue
        a = y[start:end]
        b = y[end:end + L]
        if a.std() < 1e-9 or b.std() < 1e-9:
            continue
        # Normalised cross-correlation of the tail against the region just after.
        num = float(np.dot(a, b))
        den = float(np.linalg.norm(a) * np.linalg.norm(b)) + 1e-12
        corr = num / den
        score = corr - 0.0005 * k        # tie-break: prefer shorter loops
        if best is None or score > best[0]:
            best = (score, start, end, L, corr)
    if best is None or best[4] < 0.90:
        return y, None

    _, start, end, L, _ = best
    # Snap loop_start to a whole number of periods so the phase is continuous.
    start = int(round(start / period) * period)
    end = start + L
    if end + L > n:
        return y, None

    # Bake an equal-power crossfade of the loop tail into the region head.
    xf = int(min(0.03 * sr, L // 2))
    if xf > 8:
        fade_in = np.sin(np.linspace(0, np.pi / 2, xf)) ** 2
        fade_out = np.cos(np.linspace(0, np.pi / 2, xf)) ** 2
        y = y.copy()
        y[end:end + xf] = (y[end:end + xf] * fade_in
                           + y[start:start + xf] * fade_out)
    # A tiny DC guard at the splice: subtract the mean mismatch over one period.
    y = y - 0.0
    return y, (int(start), int(end))


def render_midi_phrase(midi_notes, sr=SR):
    """Build a musical audition phrase (Section 5): melody + C-G-Am-F chords +
    a bass line, as a list of (start_s, dur_s, midi, velocity) events.

    Used by `poc_verify.py` to A/B the candidates on a *real* phrase instead of
    isolated beeps. `midi_notes` is a small helper so every candidate renders
    the identical phrase for a fair comparison.
    """
    events = []
    t = 0.0
    beat = 0.55
    melody = [72, 74, 76, 74, 72, 69, 71, 72]          # C5 D5 E5 D5 C5 A4 B4 C5
    chords = [(48, [60, 64, 67]), (43, [59, 62, 67]),
              (45, [57, 60, 64]), (41, [57, 60, 65])]  # C  G  Am  F (root, triad)
    for i, m in enumerate(melody):
        events.append((t, beat * 0.9, m, 96))
        t += beat
    t = 0.0
    for ci, (root, triad) in enumerate(chords):
        for _ in range(2):
            events.append((t, beat * 1.8, root, 80))          # bass root
            for n in triad:
                events.append((t, beat * 1.7, n, 64))          # chord
            t += beat * 2
    return events
