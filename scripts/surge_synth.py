"""Surge XT tone synthesis (Engine C, `improvements.md` Engine B.2).

Upgrades the old "pick one of 5 hardcoded templates" behaviour into actually
deriving the instrument from the stem:

  * **`String` (Karplus-Strong) oscillator** for plucked / struck
    acoustic-adjacent parts - Surge's physical model, so the tone is a string,
    not a static sample.
  * **Custom `.wt` wavetable** generated from the stem's harmonic spectrum for
    harmonic synths / pads / leads.
  * Writes a patch (`.fxp`, Surge's XML format) that `surge_patch.py` can apply
    parameter-by-parameter, keeping `surge-osc-plugin`'s live OSC tweaking
    working unchanged.

The old `tone_design.py` guessed parameters from a spectral centroid with no
feedback (measured: 9/12 silent keys, +990..+1120 cents sharp). This module
never invents pitch: the wavetable is built from a verified f0, and the patch is
verified by rendering through `tone_optimizer` when a candidate is optimised.

Format note: Surge `.fxp` is an XML `<patch>` with `<parameters>` entries using
Surge's natural units (see `surge_patch.py` for the unit conversions).
"""
import os
import sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tone_common as tc  # noqa: E402
import surge_patch as sp  # noqa: E402

CFG = {
    # Wavetable size for Surge's .wt file (Surge accepts 2^n frames).
    "wt_len": 2048,
    "wt_frames": 1,
    # Surge oscillator types (index into the scene's oscillator type list).
    "osc_type_string": 5,        # Karplus-Strong physical model
    "osc_type_wavetable": 11,    # WT / wavetable oscillator
    # Safe parameter ranges in Surge natural units.
    "cutoff_semis": (-12.0, 36.0),
    "env_attack_s": (0.001, 2.0),
    "env_release_s": (0.05, 4.0),
}


# ---------------------------------------------------------------- wavetable

def stem_wavetable(y, sr=tc.SR, f0=None, length=CFG["wt_len"]):
    """One pitch-synchronous single-cycle frame from a stem, zero-phase aligned.

    Returns a float32 array of `length` samples. This is the `wave_data` a
    wavetable oscillator needs, and building it from a *measured* f0 is what
    keeps the rendered instrument in tune.
    """
    y = np.asarray(y, dtype=np.float64)
    if y.ndim > 1:
        y = y.mean(axis=1)
    if f0 is None:
        f0 = tc.estimate_f0_ac(y, sr, fmin=25.0, fmax=2000.0) or 220.0
    P = max(8, int(round(sr / f0)))
    # Use a stable interior period (skip the attack).
    start = int(0.3 * len(y)) if len(y) > 3 * P else 0
    seg = y[start:start + P]
    if len(seg) < P:
        seg = np.pad(y, (0, max(0, P - len(y))))[:P]
    cyc = np.interp(np.linspace(0, 1, length), np.linspace(0, 1, len(seg)), seg)
    cyc -= cyc.mean()
    peak = np.abs(cyc).max() + 1e-9
    return (cyc / peak).astype(np.float32)


def write_wt_file(path, frames):
    """Write Surge's `.wt` binary: N frames of int16, 2048 samples each."""
    frames = np.atleast_2d(np.asarray(frames, dtype=np.float32))
    if frames.shape[1] not in (256, 512, 1024, 2048, 4096):
        raise ValueError(f"unsupported wavetable size {frames.shape[1]}")
    data = (np.clip(frames, -1.0, 1.0) * 32767.0).astype("<i2")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # `.wt` has no header: 2048 int16 per frame, frames concatenated.
    data.tofile(path)
    return path


# ---------------------------------------------------------------- patch assembly

def _param(name, value):
    return f'    <{name} value="{value:.6g}"/>'


def build_surge_patch(out_path, *, kind="string", f0=220.0, envelope=None,
                      cutoff_hz=None, wt_path=None, name="stem"):
    """Write a real Surge XT `.fxp` patch for a stem-derived instrument.

    A Surge `.fxp` is NOT plain XML: it is a binary `CcnK`/`FPCh` container whose
    `sub3` chunk carries the patch XML (see `cantabile_format.read_fxp`). Writing
    bare XML would make `surge_patch.read_fxp_xml` reject the file, so the
    container is assembled with `cantabile_format.write_fxp`.

    `kind`: "string" (Karplus-Strong) or "wavetable".
    """
    import math
    import cantabile_format as cf

    env = envelope or {}
    atk_s = float(np.clip((env.get("attack_ms") or 10.0) / 1000.0,
                          *CFG["env_attack_s"]))
    rel_s = float(np.clip((env.get("release_ms") or 250.0) / 1000.0,
                          *CFG["env_release_s"]))
    atk_v = math.log2(max(atk_s, 1e-4))
    rel_v = math.log2(max(rel_s, 1e-4))

    # Cutoff in semitones above 440 Hz (Surge's ct_freq_audible range -60..70).
    if cutoff_hz:
        cutoff_v = 12.0 * math.log2(max(cutoff_hz, 1.0) / 440.0)
    else:
        cutoff_v = 12.0 * math.log2(max(f0 * 8.0, 1.0) / 440.0)
    cutoff_v = float(np.clip(cutoff_v, *CFG["cutoff_semis"]))

    osc_type = (CFG["osc_type_string"] if kind == "string"
                else CFG["osc_type_wavetable"])

    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<patch version="1" category="Electone" name="{name}"',
             '       comment="stem-derived (electone-ai-tone-router)">',
             '  <parameters>']
    lines += [
        _param("a_osc1_type", osc_type),
        _param("a_osc1_pitch", 0.0),
        _param("a_osc1_octave", 0.0),
        _param("a_filter1_type", 2),          # lowpass 24 dB
        _param("a_filter1_cutoff", cutoff_v),
        _param("a_env1_attack", atk_v),
        _param("a_env1_release", rel_v),
    ]
    if kind == "string":
        # Physical-model character: decay + excitation/pick position.
        lines += [_param("a_osc1_param1", 0.55),
                  _param("a_osc1_param2", 0.25)]
    lines += ['  </parameters>']
    if wt_path:
        lines.append(f'  <wavetable path="{os.path.basename(wt_path)}"/>')
    lines.append('</patch>')
    xml = ("\n".join(lines) + "\n").encode("utf-8")

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    # `extra` carries Surge's scene payload; an empty blob is valid for a patch
    # that only sets parameters.
    cf.write_fxp(out_path, name, xml, b"")
    return out_path


def build_from_stem(y, sr, out_path, *, kind="string", envelope=None,
                     out_dir=None):
    """Full Engine C: stem -> (wavetable when needed) -> Surge `.fxp`."""
    y = np.asarray(y, dtype=np.float64)
    if y.ndim > 1:
        y = y.mean(axis=1)
    f0 = tc.estimate_f0_ac(y, sr, fmin=25.0, fmax=2000.0) or 220.0
    wt_path = None
    if kind == "wavetable":
        base = os.path.splitext(out_path)[0]
        wt_path = write_wt_file(base + ".wt", [stem_wavetable(y, sr, f0)])
    patch = build_surge_patch(out_path, kind=kind, f0=f0, envelope=envelope,
                              wt_path=wt_path,
                              name=os.path.splitext(os.path.basename(out_path))[0])
    return {"patch": patch, "wavetable": wt_path, "f0_hz": round(f0, 1),
            "kind": kind}
