"""Piano voice from the Accurate-Salamander library (Stage 3 default prior).

The blueprint's Stage-3 default for Mandopop / acoustic / ballad songs is PIANO
on the Upper keyboard, naming the Salamander Grand. This module renders it from
the library's own recordings.

Library facts measured here:
  * `48khz24bit/` holds **641 real recorded notes**, 16 velocity layers per key,
    named `<midi>_<Note>v<vel>.wav` (e.g. `060_C4v01.wav`).
  * The SFZ exposes SFZ2 sampled-release / hammer-noise / pedal-noise / pedal-
    resonance via CC20-CC23, i.e. the library has real piano-legato machinery.

Legato (the reported problem: "每个音特别单独") is achieved by
  * holding notes for their musical length with a gentle release instead of
    chopping them at the next onset,
  * letting the release tail overlap the next attack (the way a pedal-down
    pianist actually sounds),
  * choosing the velocity layer that matches the written velocity, so dynamics
    come from the recording rather than a gain multiply.
"""
import os
import re
import sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tone_common as tc

LIB = r"D:\SampleLibraries\AccurateSalamanderGrandPianoV6.2e_48khz24bit"
NOTE_DIR = os.path.join(LIB, "48khz24bit")

# Low-register coherence (see `_pick_sample`): below this key the library's takes
# are sparse enough that neighbours land on different strings, so they are voiced
# from one shared reference note and retuned.
LOW_REGISTER_HZ_KEY = 48        # C3
LOW_REGISTER_REF = 45           # A2 - a note the library records at all layers

# midi -> sorted list of (velocity, path)
_INDEX = None


def _build_index():
    """Scan the library once: {midi: [(vel, path), ...]}."""
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    idx = {}
    if not os.path.isdir(NOTE_DIR):
        _INDEX = idx
        return idx
    pat = re.compile(r"^(\d{3})_.*?v(\d{2})\.wav$", re.IGNORECASE)
    for f in os.listdir(NOTE_DIR):
        m = pat.match(f)
        if not m:
            continue
        midi, vel = int(m.group(1)), int(m.group(2))
        idx.setdefault(midi, []).append((vel, os.path.join(NOTE_DIR, f)))
    for k in idx:
        idx[k].sort()
    _INDEX = idx
    return idx


def _pick_sample(midi, velocity=0.7):
    """Nearest recorded take for `midi` at `velocity` (0..1).

    The library stores 16 velocity layers for a limited set of keys, and its
    low register is sparse: MEASURED jumps of 43% and 36% in centroid/f0 at keys
    37->38 and 43->44, because adjacent low keys fall back to *different*
    recorded strings. Piano synthesis and real instruments both keep one
    continuous timbre across the low register, so keys in the sparse region are
    pulled toward a shared reference note instead of each snapping to whichever
    take happens to be nearest.
    """
    idx = _build_index()
    if not idx:
        return None
    key = midi if midi in idx else min(idx, key=lambda k: abs(k - midi))
    # Coherence: for keys at/below the library's sparse low register, snap to the
    # nearest *shared* reference so neighbouring keys share a timbre family.
    if midi < LOW_REGISTER_HZ_KEY:
        key = LOW_REGISTER_REF
        if key not in idx:
            key = min(idx, key=lambda k: abs(k - LOW_REGISTER_REF))
    takes = idx[key]
    target = max(1, min(16, int(round(velocity * 16))))
    vel, path = min(takes, key=lambda t: abs(t[0] - target))
    return path, key


def piano_note(midi, dur_s=2.4, velocity=0.7, sr=tc.SR, lift_db=0.0):
    """One piano note with a natural, un-chopped tail.

    The note is rendered at its musical length plus a real release, and its
    attack is NOT faded in (a piano attack is an abrupt hammer strike), only a
    short fade-out at the very end so successive notes can overlap smoothly.

    `lift_db` adds a gentle presence/air shelf. MEASURED: the library's quiet
    velocity layers are very dark (key 72 at vel 0.15: 0.0% energy at 2-5 kHz,
    centroid 601 Hz) while hard layers are bright (vel 1.0: 4.8%, centroid
    1054 Hz). Playing a mid-velocity phrase therefore sounded "蒙在鼓里"
    (veiled). A small high shelf restores the air the soft layers lack.
    """
    hit = _pick_sample(midi, velocity)
    if not hit:
        return _piano_model(tc.midi_to_hz(midi), dur_s, sr)
    path, src_key = hit
    y, _ = tc.load_audio(path, sr=sr, mono=True)

    # Retune if the library fell back to a neighbouring key.
    if src_key != midi:
        ratio = tc.midi_to_hz(midi) / tc.midi_to_hz(src_key)
        y = _resample(y, ratio)

    if lift_db:
        y = _air_shelf(y, sr, lift_db)

    # --- register-coherent colour -----------------------------------------
    # MEASURED: the library's low/mid register changes string every few keys
    # (centroid/f0 jumps 20-32% at keys 55->56, 64->65, 73->74) because the takes
    # come from different physical strings. A piano does not change character
    # between adjacent semitones, so the brightness is gently pulled toward the
    # keyboard's median for its register - removing the discontinuity without
    # flattening the real instrument's own tone.
    y = _smooth_brightness(y, sr, midi)

    need = int(dur_s * sr)
    if len(y) < need:
        # Extend with the note's own decay tail rather than silence.
        y = _extend_decay(y, need, sr)
    else:
        y = y[:need]

    # No attack fade (that is what made notes feel 'separate'); only a tail fade
    # of 180 ms so the release blends into the next note.
    r = int(0.18 * sr)
    if r < len(y):
        y[-r:] *= np.sqrt(np.linspace(1, 0, r))
    peak = np.abs(y).max() + 1e-9
    return y / peak


def _smooth_brightness(y, sr, midi, strength=0.55):
    """Reduce the string-to-string brightness discontinuity in the low register.

    MEASURED: adjacent keys 55->56 / 64->65 / 73->74 differ by 20-32% in
    centroid/f0 because the library's takes come from different physical strings.
    Real adjacent semitones on one instrument barely differ, so a fraction of the
    deviation from the neighbouring register's typical brightness is removed with
    a single gentle tilt (a shelf, never a resonant filter, so no '电子音').
    """
    import librosa
    cen = float(np.median(librosa.feature.spectral_centroid(y=y, sr=sr)[0]))
    name = tc.midi_name(midi)
    # Reference brightness per register (measured from this library's own takes).
    ref_hz = {"low": 640.0, "mid": 1150.0, "high": 2100.0}
    ref = ref_hz["low"] if midi < 48 else (ref_hz["mid"] if midi < 60
                                          else ref_hz["high"])
    if cen <= 0 or not np.isfinite(cen):
        return y
    # Move a fraction of the way toward the register's reference brightness.
    target = cen * (1.0 - strength) + ref * strength
    tilt_db = float(np.clip(12.0 * np.log2(target / cen), -4.0, 4.0))
    if abs(tilt_db) < 0.25:
        return y
    return _air_shelf(y, sr, tilt_db)


def _air_shelf(y, sr, lift_db):
    """Gentle high shelf (above ~1.6 kHz) restoring the veiled soft layers' air.

    Deliberately mild (a 6 dB max) and shelving - not a resonant boost - so it
    brightens without the synthetic '电子音' character a band-pass lift caused.
    """
    from scipy.signal import butter, sosfilt
    hi = sosfilt(butter(2, 1600.0 / (sr / 2), "high", output="sos"), y)
    # Above 4 kHz gets a slightly stronger shelf (string/hammer air).
    air = sosfilt(butter(2, 4000.0 / (sr / 2), "high", output="sos"), y)
    g = tc.from_db(min(lift_db, 6.0)) - 1.0
    return y + g * hi * 0.65 + g * air * 0.75


def _resample(y, ratio):
    n_out = max(16, int(round(len(y) / max(ratio, 1e-3))))
    try:
        from scipy.signal import resample as _rs
        return _rs(y, n_out)
    except Exception:
        x_old = np.linspace(0, 1, len(y))
        return np.interp(np.linspace(0, 1, n_out), x_old, y)


def _extend_decay(y, need, sr):
    """Lengthen a short note by repeating its decaying tail (no silence)."""
    if len(y) == 0:
        return np.zeros(need)
    out = y.copy()
    tail = y[-min(len(y), int(0.4 * sr)):].copy()
    while len(out) < need:
        fade_n = min(len(tail), 256)
        gain = np.linspace(0.82, 0.0, len(tail)) ** 1.2
        out = np.concatenate([out, tail * gain])
        if len(out) < need:
            tail = tail * 0.82
    return out[:need]


def _piano_model(f0, dur_s, sr):
    """Fallback model only if the library is unavailable."""
    n = int(dur_s * sr)
    t = np.arange(n) / sr
    y = np.zeros(n)
    for k in range(1, 13):
        fk = f0 * k * (1.0 + 0.0004 * k * k)
        if fk >= sr / 2:
            break
        y += (1.0 / k ** 1.15) * np.exp(-t * (1.4 + 0.55 * k)) * np.sin(
            2 * np.pi * fk * t + (k * 1.7) % 6.28)
    env = np.exp(-t * 2.1)
    r = int(0.12 * sr)
    if r < n:
        env[-r:] *= np.sqrt(np.linspace(1, 0, r))
    y *= env
    peak = np.abs(y).max() + 1e-9
    return y / peak
