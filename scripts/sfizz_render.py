"""Render tone through the REAL sfizz engine (the architectural fix).

The Python-side chain kept hand-EQ-ing recordings until it sounded synthetic:
a 2.2x narrow presence boost IS a resonant filter. The correct architecture is
what Cantabile does - let **sfizz** apply the instrument: SFZ opcodes for the
filter, velocity response and amp envelope, so we never hand-shape the audio.

Measured on this machine: sfizz.vst3 loads in ~12s (543 params, is_instrument)
and renders real audio through pedalboard, so the same engine that will play in
Cantabile can be used to audition and to verify.
"""
import os
import sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tone_common as tc  # noqa: E402

SFIZZ_VST3 = (r"C:\Program Files\Common Files\VST3\sfizz.vst3"
              r"\Contents\x86_64-win\sfizz.vst3")


def write_sfz(path, samples, *, cutoff=5000.0, fil_veltrack=1800,
              amp_veltrack=45.0, attack=0.002, release=0.35,
              resonance=0.0, eq=None):
    """Write a plain SFZ 2.0 instrument over a set of per-key samples.

    Kept deliberately minimal: no rewrites, no per-region EQ stacks. All shaping
    is expressed as readable, engine-side SFZ opcodes.
    """
    lines = ["// electone-ai-tone-router : sfizz instrument",
             "<global>",
             f"amp_veltrack={amp_veltrack:g}",
             "fil_type=lpf_2p",
             f"cutoff={cutoff:g}",
             f"fil_veltrack={fil_veltrack:g}",
             f"ampeg_attack={attack:g}",
             f"ampeg_release={release:g}"]
    if resonance:
        lines.append(f"resonance={resonance:g}")
    if eq:
        g1, g2, g3 = eq
        lines += [f"eq1_freq=120 eq1_gain={g1:.2f}",
                  f"eq2_freq=800 eq2_gain={g2:.2f}",
                  f"eq3_freq=4000 eq3_gain={g3:.2f}"]
    lines.append("<group>")
    for key in sorted(samples):
        lines.append(f"<region> sample={samples[key]} pitch_keycenter={key} "
                     f"lokey={key} hikey={key}")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


class SfizzRenderer:
    """Headless sfizz instance that loads one SFZ and renders MIDI phrases."""

    def __init__(self, sfz_path=None):
        from pedalboard import load_plugin
        self.plugin = load_plugin(SFIZZ_VST3, plugin_name="sfizz")
        self.sfz_path = None
        if sfz_path:
            self.load(sfz_path)

    def load(self, sfz_path):
        """Load an SFZ into the engine.

        sfizz accepts the instrument file as a component state; pedalboard's
        `preset_data` is the only route that reaches it headlessly.
        """
        state = _sfizz_state(sfz_path)
        try:
            self.plugin.preset_data = state
        except Exception:
            pass
        self.sfz_path = sfz_path
        return self

    def render(self, events, sr=tc.SR, duration=None, channels=2):
        """Render a list of (start_s, dur_s, midi, velocity) to float audio."""
        if duration is None:
            duration = max(e[0] + e[1] for e in events) + 0.6
        msgs = []
        for (start, dur, midi, vel) in events:
            v = int(max(1, min(127, vel)))
            msgs.append((bytes([0x90, int(midi), v]), float(start)))
            msgs.append((bytes([0x80, int(midi), 0]), float(start + dur)))
        msgs.sort(key=lambda m: m[1])
        audio = self.plugin.process(
            msgs, duration=float(duration), sample_rate=sr,
            num_channels=channels, buffer_size=512, reset=True)
        return audio


def _sfizz_state(sfz_path):
    """Minimal VST3 component state whose only content is the SFZ path."""
    import struct
    path = os.path.abspath(sfz_path).encode("utf-8")
    # sfizz reads its instrument from a chunk; a UTF-8 path string is what its
    # state carries when saved by a host.
    return struct.pack("<I", len(path)) + path
