"""Regression tests for the tone pipeline's measurement and design rules.

These do not need a GPU, a network, or a rendered song: they check the rules
that were wrong in ways no exception would have caught. Every case below is a
bug that shipped, plus the measurement that proved it.

Run directly (no pytest needed):

    python scripts\test_tone_rules.py
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _cache_env  # noqa: E402,F401
import tone_design as td  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.environ.get("ELECTONE_WORKSPACE") or os.path.join(ROOT, "workspace")

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  OK   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILURES.append(name)


def fundamental_hz(register_midi):
    return 440.0 * 2.0 ** ((float(register_midi) - 69.0) / 12.0)


# --------------------------------------------------------------- cutoff rules

def test_cutoff_never_below_audible_floor():
    """The bass cutoff must clear the fundamental by the measured margin.

    History: the design clamped the cutoff to +-3 octaves of a template that
    sits at -51 semitones (13.75 Hz). That produced -15 semitones = 185 Hz for a
    74 Hz fundamental, and *then* the "never below 1.5x fundamental" rule was
    applied to a value that was already below it, so nothing changed. The
    rendered peak was 0.0004 against 0.114 for the untouched template: the whole
    pedal keyboard was silent.
    """
    print("\n[cutoff] floor is relative to the stem's own register")
    template = {
        "a_filter1_type": 1.0,          # plain lowpass
        "a_filter1_cutoff": -50.984,    # the Fingered template's own cutoff
        "a_filter1_envmod": 14.4,
        "a_filter1_resonance": 0.255,
        "a_env1_attack": -3.8, "a_env1_decay": 0.5,
        "a_env1_sustain": 0.4, "a_env1_release": -2.0,
    }
    cases = [
        # (register_midi, brightness_hz) - low, dark instrument: the case that broke.
        (33.0, 85.0),     # bass on 《蒲公英》
        (38.1, 137.0),    # bass on 《女孩》
        (41.1, 110.0),    # bass on 《我走后》
    ]
    for reg, brightness in cases:
        feats = {"register_midi": reg, "brightness_hz": brightness,
                 "harmonic_ratio": 0.96, "onset_rate": 0.6}
        design = td.design_patch_params(feats, template=template)
        cutoff_hz = design["features"].get("cutoff_hz")
        fund = fundamental_hz(reg)
        check(f"register {reg} (fundamental {fund:.0f} Hz) has a designed cutoff",
              cutoff_hz is not None)
        if cutoff_hz is None:
            continue
        ratio = cutoff_hz / fund
        # 8x = 3 octaves. Measured on the real template: 111 Hz (1.5x) -> peak
        # 0.0000, 222 Hz -> 0.0004, 444 Hz -> 0.0043, 592 Hz (8x) -> 0.0323.
        check(f"register {reg}: cutoff {cutoff_hz:.0f} Hz is >= 7.9x fundamental",
              ratio >= 7.9, f"got {ratio:.2f}x")
        check(f"register {reg}: cutoff stays under the oscillator-noise ceiling",
              cutoff_hz <= fund * td.CFG["cutoff_ceiling_vs_fundamental"] * 1.001,
              f"got {cutoff_hz:.0f} Hz")


def test_bright_instrument_keeps_its_top_end():
    """A bright stem must not be dragged down by the bass floor.

    The floor is a *minimum*, not a target: applying it to a guitar whose
    measured centroid is 1807 Hz would turn a nylon guitar into a muffled sine.
    """
    print("\n[cutoff] a bright stem is not clamped down to the floor")
    template = {"a_filter1_type": 2.0, "a_filter1_cutoff": 18.0,
                "a_filter1_envmod": 10.0, "a_filter1_resonance": 0.08,
                "a_env1_attack": -3.8, "a_env1_decay": 0.5,
                "a_env1_sustain": 0.0, "a_env1_release": 0.5}
    feats = {"register_midi": 40.1, "brightness_hz": 1807.0,
             "harmonic_ratio": 0.88, "onset_rate": 2.1,
             "brightness_attack_hz": 2737.0, "brightness_sustain_hz": 1839.0}
    design = td.design_patch_params(feats, template=template)
    cutoff_hz = design["features"]["cutoff_hz"]
    fund = fundamental_hz(40.1)
    check(f"guitar cutoff {cutoff_hz:.0f} Hz is well above the floor",
          cutoff_hz > fund * 8.0, f"got {cutoff_hz / fund:.1f}x")
    check(f"guitar cutoff {cutoff_hz:.0f} Hz stays within the template's +-3 octaves",
          cutoff_hz <= 440.0 * 2.0 ** ((18.0 + 36.0) / 12.0) * 1.01)


def test_non_lowpass_keeps_template_cutoff():
    """A notch filter has no brightness to open: the template value must survive."""
    print("\n[cutoff] non-lowpass filter keeps its own cutoff")
    template = {"a_filter1_type": 8.0, "a_filter1_cutoff": -5.0}
    feats = {"register_midi": 59.9, "brightness_hz": 2371.0, "harmonic_ratio": 0.99}
    design = td.design_patch_params(feats, template=template)
    check("cutoff is not written for a notch filter",
          "a_filter1_cutoff" not in design["params"],
          f"got {design['params'].get('a_filter1_cutoff')}")
    check("resonance is not written for a notch filter",
          "a_filter1_resonance" not in design["params"])


def test_envtime_unit_roundtrip():
    """Surge envelope times are log2(seconds); the conversion must be exact."""
    print("\n[units] envtime round-trip")
    for seconds in (0.008, 0.0697, 1.463, 8.0):
        v = td.seconds_to_envtime_value(seconds)
        back = td.envtime_value_to_seconds(v)
        check(f"{seconds}s -> value {v:.4f} -> {back:.4f}s",
              abs(back - seconds) <= max(1e-3, seconds * 1e-3))


def test_cutoff_hz_roundtrip():
    """Cutoff natural value is 12*log2(Hz/440)."""
    print("\n[units] cutoff Hz round-trip")
    for hz in (55.0, 185.0, 592.0, 2217.0, 9946.0):
        v = td.hz_to_cutoff_value(hz)
        back = td.cutoff_value_to_hz(v)
        check(f"{hz} Hz -> value {v:.4f} -> {back:.1f} Hz",
              abs(1200.0 * math.log2(back / hz)) < 1.0)


# --------------------------------------------------- analyzer / sampler parity

def test_analysis_and_sampler_share_a_rate():
    """analyze-tones.py and build-sample-tones.py must agree on SR.

    They disagreed (22050 vs 44100) for the whole life of the project, which
    meant every absolute-Hz feature the analyst recorded described a signal
    band that stopped at 11 kHz while the sampler worked with 22 kHz.
    """
    print("\n[parity] analysis and sampling use the same sample rate")
    import importlib.util

    def load(path, name):
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(ROOT, "scripts", path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    # Both filenames contain a hyphen, so neither is importable by name; load
    # them from their paths instead.
    mod = load("analyze-tones.py", "analyze_tones_under_test")
    mod2 = load("build-sample-tones.py", "build_sample_tones_under_test")

    check(f"analyze-tones SR={mod.SR} == build-sample-tones SR={mod2.CFG['sr']}",
          mod.SR == mod2.CFG["sr"])


# ------------------------------------------------------------- eval thresholds

def test_eval_constants_are_documented_bounds():
    """Guard the eval's own thresholds against silent drift.

    Each of these encodes a measurement, so changing one without re-measuring
    turns a real defect back into a false pass (or a correct instrument into a
    false failure).
    """
    print("\n[eval] thresholds still match their measured basis")
    import tone_eval as te
    check("pitch tolerance is 50 cents", te.PITCH_TOLERANCE_CENTS == 50.0)
    check("stability IQR is 40 cents", te.PITCH_STABLE_IQR_CENTS == 40.0)
    check("minimum measurement window is 0.10 s", te.MIN_MEASURE_SECONDS == 0.10)
    check("null-test pass is -60 dBFS", te.NULL_TEST_PASS_DB == -60.0)


def main():
    test_cutoff_never_below_audible_floor()
    test_bright_instrument_keeps_its_top_end()
    test_non_lowpass_keeps_template_cutoff()
    test_envtime_unit_roundtrip()
    test_cutoff_hz_roundtrip()
    test_analysis_and_sampler_share_a_rate()
    test_eval_constants_are_documented_bounds()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
