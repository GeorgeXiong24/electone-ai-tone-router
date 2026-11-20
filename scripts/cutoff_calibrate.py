"""Calibrate a Surge XT patch's filter cutoff against a target brightness.

The design layer picks a cutoff from the stem's spectral centroid, but the
mapping from cutoff to rendered centroid is not a constant ratio: it depends on
the oscillator's own harmonic content and saturates once the cutoff passes the
oscillator's top harmonic. A fixed ratio therefore over-brightens dark patches
and under-brightens bright ones.

This module closes the loop the only reliable way: render the patch at a few
cutoffs, measure the resulting centroid, and pick the cutoff whose render is
closest to the target. The result is written back into the .fxp, so the file
that ships is the one that was actually measured.

Usage (from build-tones.py):

    from cutoff_calibrate import calibrate_cutoff
    new_value, report = calibrate_cutoff(fxp_path, target_centroid_hz)
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SURGE_VST3 = (r"C:\Program Files\Common Files\VST3\Surge Synth Team"
              r"\Surge XT.vst3\Contents\x86_64-win\Surge XT.vst3")

SR = 48000
# A single sustained note is enough to characterise the filter: the centroid of
# a held note is what the cutoff shapes.
PROBE_NOTE = 60
PROBE_SECONDS = 1.6


def _render_centroid(plugin, note=PROBE_NOTE):
    """Render one note and return its median spectral centroid in Hz."""
    import librosa
    import numpy as np

    msgs = [(bytes([0x90, note, 100]), 0.0),
            (bytes([0x80, note, 0]), PROBE_SECONDS)]
    audio = plugin.process(msgs, duration=PROBE_SECONDS + 0.8,
                           sample_rate=SR, num_channels=2, buffer_size=512)
    y = audio.mean(axis=0)
    if not np.any(y):
        return 0.0
    cent = librosa.feature.spectral_centroid(y=y, sr=SR)[0]
    return float(np.median(cent))


def _set_cutoff(plugin, natural_value):
    """Write a natural-unit cutoff into the plugin."""
    lo, hi = -60.0, 70.0
    plugin.parameters["a_filter_1_cutoff"].raw_value = (
        (natural_value - lo) / (hi - lo))


def calibrate_cutoff(fxp_path, target_hz, tolerance=0.15, steps=14):
    """Find the cutoff whose render best matches `target_hz`.

    A linear sweep, not a bisection: the measured centroid is *not* monotonic
    in cutoff at the bottom of the range (a fully closed filter still leaks
    enough to read ~80 Hz on a bass patch), so bisection can converge on the
    wrong branch. Sweeping and taking the closest probe is slower but correct.

    Returns (natural_cutoff, report) where report is a list of
    (natural, cutoff_hz, measured_hz) probes, for the manifest.
    """
    import surge_patch as sp
    from pedalboard import load_plugin

    xml = sp.read_fxp_xml(fxp_path)
    plugin = load_plugin(SURGE_VST3)
    sp.apply_patch(plugin, xml)

    lo, hi = -60.0, 70.0
    probes = []

    def probe(nat):
        _set_cutoff(plugin, nat)
        measured = _render_centroid(plugin)
        probes.append((round(nat, 2), round(440 * 2 ** (nat / 12), 1),
                       round(measured, 1)))
        return measured

    # Sweep the whole legal range: the useful part of the curve depends on the
    # oscillator, so there is no safe sub-range to assume.
    best_nat, best_err = lo, None
    for i in range(steps):
        nat = lo + (hi - lo) * i / (steps - 1)
        measured = probe(nat)
        err = abs(measured - target_hz)
        if best_err is None or err < best_err:
            best_nat, best_err = nat, err

    # One refinement pass around the winner, at a quarter of the sweep step.
    step = (hi - lo) / (steps - 1)
    for delta in (-step / 2, step / 2):
        nat = min(hi, max(lo, best_nat + delta))
        measured = probe(nat)
        err = abs(measured - target_hz)
        if err < best_err:
            best_nat, best_err = nat, err

    return best_nat, probes


def main():
    import argparse
    import json

    ap = argparse.ArgumentParser()
    ap.add_argument("--fxp", required=True)
    ap.add_argument("--target", type=float, required=True,
                    help="target spectral centroid in Hz")
    args = ap.parse_args()

    nat, probes = calibrate_cutoff(args.fxp, args.target)
    print(f"target {args.target:.0f} Hz -> cutoff {nat:.2f} "
          f"({440 * 2 ** (nat / 12):.0f} Hz)")
    for n, hz, m in probes:
        print(f"  cutoff {n:7.2f} ({hz:8.1f} Hz) -> centroid {m:8.1f} Hz")
    print(json.dumps({"cutoff": nat, "probes": probes}, ensure_ascii=False))


if __name__ == "__main__":
    main()
