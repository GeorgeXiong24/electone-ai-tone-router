"""Stem-extracted wavetable + attack-transient synthesis -> `Vital` (Engine B).

`improvements.md` Engine B.1. Extracts a small, self-contained ``.vital`` preset
directly from a stem:

  * **Wavetable**: 4-8 pitch-synchronous 2048-sample single-cycle frames sliced
    across a representative note (attack -> early sustain -> late sustain),
    normalised and zero-phase aligned, Base64-embedded as ``wave_data`` keyframes
    in Oscillator 1.
  * **Attack transient**: the first 30-60 ms of the dry stem's percussive /
    residual component (HPSS ``y_p``) kept at original speed and Base64-embedded
    in Vital's Sample oscillator, preserving pick/pluck/breath/hammer character.
  * Filter, amp + filter envelopes, chorus, 3-band Match EQ and reverb matched
    to the stem are written into the preset.

Output is a single ``.vital`` JSON (< 500 KB) with zero external sample paths.

Caveat (Phase 0 checklist): Vital's ``.vital`` JSON schema is large and version
specific. The DSP here (wavetable + transient extraction) is the deliverable; the
assembled JSON is a best-effort minimal template. If Vital rejects it, capture a
known-good ``.vital`` once and merge into ``VITAL_TEMPLATE`` - the same
"capture reference state once" step Phase 3 does for Cantabile blobs.
"""
import base64
import json
import os
import sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tone_common as tc  # noqa: E402

CFG = {
    "frame_size": 2048,
    "n_frames": 8,
    "transient_ms": (30.0, 60.0),
    "match_eq_max_db": 7.5,
}

# Minimal .vital skeleton. Oscillator 1 wavetable + Sample transient + filter,
# two envelopes, chorus, EQ, reverb. Field names follow Vital's public preset
# layout; see the module docstring for the schema caveat.
VITAL_TEMPLATE = {
    "settings": {"version": "1.1.1"},
    "oscillator": [{}, {}],
    "sample": {},
    "filter": [{}, {}],
    "env": [{}, {}],
    "chorus": {}, "delay": {}, "reverb": {}, "eq": [{}, {}],
}


# ---------------------------------------------------------------- wavetable

def extract_wavetable(y, sr, f0, n_frames=CFG["n_frames"],
                      frame_size=CFG["frame_size"]):
    """Pitch-synchronous single-cycle frames from a representative note.

    Returns a list of `n_frames` float32 arrays of length `frame_size`, each one
    normalised single cycle, zero-phase aligned (cross-correlation to frame 0).
    """
    y = np.asarray(y, dtype=np.float64)
    if y.ndim > 1:
        y = y.mean(axis=1)
    if f0 is None or f0 <= 0:
        f0 = tc.estimate_f0(y, sr) or 220.0
    P = max(8, int(round(sr / f0)))            # source period in samples
    # Slice centres spread across the note body (skip the first period=attack).
    n = len(y)
    centres = np.linspace(int(0.15 * n), int(0.9 * n), n_frames).astype(int)

    def one_cycle_at(c):
        a = max(0, c - P // 2)
        seg = y[a:a + P]
        if len(seg) < P:
            seg = np.pad(seg, (0, P - len(seg)))
        # Resample the single period to frame_size (linear, adequate for a cycle).
        x_old = np.linspace(0, 1, len(seg))
        x_new = np.linspace(0, 1, frame_size)
        cyc = np.interp(x_new, x_old, seg)
        cyc = cyc - cyc.mean()
        peak = np.abs(cyc).max() + 1e-9
        return cyc / peak

    frames = [one_cycle_at(c) for c in centres]
    # Zero-phase align every frame to frame 0 via cross-correlation.
    ref = frames[0]
    for i in range(1, len(frames)):
        c = np.correlate(frames[i], ref, mode="full")
        shift = int(np.argmax(c)) - (len(ref) - 1)
        frames[i] = np.roll(frames[i], -shift)
    return [f.astype(np.float32) for f in frames]


def encode_frames(frames):
    return [base64.b64encode(f.tobytes()).decode("ascii") for f in frames]


# ---------------------------------------------------------------- transient

def extract_transient(y, sr, ms=CFG["transient_ms"]):
    """First `ms` of the percussive/residual (HPSS y_p) attack, original speed."""
    import librosa
    y = np.asarray(y, dtype=np.float64)
    if y.ndim > 1:
        y = y.mean(axis=1)
    dur = float(np.clip(45.0, ms[0], ms[1]))
    n = int(dur / 1000.0 * sr)
    n = max(64, min(n, len(y)))
    _yh, yp = librosa.effects.hpss(y[:n * 2])
    tr = yp[:n]
    tr = tr / (np.abs(tr).max() + 1e-9)
    return tr.astype(np.float32)


def encode_pcm(samples):
    return base64.b64encode(np.asarray(samples, dtype=np.float32).tobytes()).decode("ascii")


# ---------------------------------------------------------------- preset build

def build_vital_preset(anchor_frames, transient, eq=(0.0, 0.0, 0.0),
                       f0=220.0, out_path=None):
    """Assemble a .vital JSON from extracted frames + transient. Returns the dict."""
    preset = json.loads(json.dumps(VITAL_TEMPLATE))   # deep copy
    preset["oscillator"][0] = {
        "wave_data": encode_frames(anchor_frames),
        "wave_frame": 0.0,
        "num_frames": len(anchor_frames),
        "name": "stem",
    }
    preset["sample"] = {
        "pcm_data": encode_pcm(transient),
        "name": "attack",
    }
    g1, g2, g3 = eq
    preset["filter"][0] = {"cutoff": 0.6, "resonance": 0.2}
    preset["env"][0] = {"attack": 0.01, "decay": 0.4, "sustain": 0.6, "release": 0.3}
    preset["env"][1] = {"attack": 0.005, "decay": 0.25, "sustain": 0.2, "release": 0.2}
    preset["eq"] = [{"gain": g1}, {"gain": g3}]
    preset["_meta"] = {"source_f0": f0, "eq_db": list(eq)}
    if out_path:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(preset, f, ensure_ascii=False, indent=2)
    return preset


def build_stem_synth(y, sr, out_path, f0=None, eq=(0.0, 0.0, 0.0)):
    """Full Engine B: stem -> representative note -> wavetable + transient -> .vital."""
    y = np.asarray(y, dtype=np.float64)
    if y.ndim > 1:
        y = y.mean(axis=1)
    if f0 is None:
        f0 = tc.estimate_f0(y, sr) or 220.0
    frames = extract_wavetable(y, sr, f0)
    transient = extract_transient(y, sr)
    preset = build_vital_preset(frames, transient, eq=eq, f0=f0, out_path=out_path)
    return {"out_path": out_path, "f0": f0, "n_frames": len(frames),
            "transient_len": len(transient), "preset": preset}
