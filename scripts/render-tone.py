"""Render a generated tone file to audio, so a tone can be heard and measured.

The pipeline up to here only ever *wrote* patch files; nothing verified that the
patch sounds like the stem it was designed from. This script closes that loop:
it loads the tone into the real plugin (Surge XT / sforzando) through
pedalboard, plays a short MIDI phrase, and writes a wav.

It also measures the render against the source stem with the same features
analyze-tones.py uses (spectral centroid, harmonic ratio, envelope), so
"does this sound like the recording" becomes a number instead of an opinion.

Usage:
  python render-tone.py --tone <fxp|sfz> --out <wav> [--notes 60,64,67]
  python render-tone.py --song <title> --keyboard 右手 --out <dir>
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _cache_env  # noqa: E402

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.environ.get("ELECTONE_WORKSPACE") or os.path.join(ROOT, "workspace")

SURGE_VST3 = (r"C:\Program Files\Common Files\VST3\Surge Synth Team"
              r"\Surge XT.vst3\Contents\x86_64-win\Surge XT.vst3")
SFORZANDO_VST3 = (r"C:\Program Files\Common Files\VST3\sforzando.vst3"
                  r"\Contents\x86_64-win\sforzando.vst3")

SR = 48000


def load_plugin_for(tone_path):
    """Load the plugin that owns this tone file."""
    from pedalboard import load_plugin
    ext = os.path.splitext(tone_path)[1].lower()
    if ext == ".fxp":
        return load_plugin(SURGE_VST3), "surge-xt"
    if ext == ".sfz":
        return load_plugin(SFORZANDO_VST3), "sforzando"
    raise SystemExit(f"unsupported tone file: {tone_path}")


def load_patch(plugin, kind, tone_path):
    """Load the tone into the plugin.

    pedalboard cannot load a Surge patch through `raw_state` (silently ignored)
    or `preset_data` (accepted without error, plugin keeps its default sound),
    so the patch is applied parameter by parameter through surge_patch.py.
    sforzando has no such path — its ARIA state is a single opaque blob — so it
    is set through `preset_data` and the caller must check the render is not
    silent.
    """
    import cantabile_format as cf
    import surge_patch as sp

    if kind == "surge-xt":
        xml = sp.read_fxp_xml(tone_path)
        applied, missing = sp.apply_patch(plugin, xml)
        return {"applied": len(applied), "missing": len(missing)}

    state = cf.build_vst3_state(
        cf.sforzando_component_state(tone_path), cf.SFORZANDO_CLASS_ID)
    plugin.preset_data = state
    return {"applied": 0, "missing": 0}


def render(plugin, notes, note_s=1.2, gap_s=0.15, velocity=0.8):
    """Play a short phrase and return the rendered audio.

    pedalboard's VST3 instrument path takes MIDI as (message, timestamp)
    tuples, where the message is raw bytes and the timestamp is seconds from
    the start of the render.
    """
    messages = []
    t = 0.0
    for pitch in notes:
        pitch = int(pitch)
        vel = int(velocity * 127)
        messages.append((bytes([0x90, pitch, vel]), t))
        messages.append((bytes([0x80, pitch, 0]), t + note_s))
        t += note_s + gap_s
    total = t + 1.0
    audio = plugin.process(
        messages,
        duration=total,
        sample_rate=SR,
        num_channels=2,
        buffer_size=512,
        reset=True,
    )
    return audio


def features(y, sr):
    """The same descriptors analyze-tones.py uses, for comparison."""
    import librosa
    y = np.asarray(y, dtype=np.float32)
    if y.ndim > 1:
        y = y.mean(axis=0)
    if not np.any(y):
        return {"empty": True}
    hop = 512
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=hop)[0]
    rms_db = 20 * np.log10(rms + 1e-12)
    active = rms_db > -45
    if not active.any():
        return {"empty": True}
    cent = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=hop)[0]
    yh, yp = librosa.effects.hpss(y)
    eh = float(np.sqrt((yh ** 2).mean()))
    ep = float(np.sqrt((yp ** 2).mean()))
    return {
        "empty": False,
        "brightness_hz": round(float(np.median(cent[active])), 0),
        "harmonic_ratio": round(eh ** 2 / (eh ** 2 + ep ** 2 + 1e-12), 3),
        "rms_db": round(float(20 * np.log10(np.sqrt((y ** 2).mean()) + 1e-12)), 1),
        "activity": round(float(active.mean()), 3),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tone", default="", help="path to a .fxp or .sfz")
    ap.add_argument("--song", default="", help="render every tone of this song")
    ap.add_argument("--keyboard", default="", help="limit to one keyboard")
    ap.add_argument("--out", default="", help="output wav or directory")
    ap.add_argument("--notes", default="60,64,67,72",
                    help="comma-separated MIDI notes to play")
    ap.add_argument("--note-s", type=float, default=1.2)
    ap.add_argument("--compare", default="",
                    help="stem wav to compare the render against")
    args = ap.parse_args()

    notes = [int(x) for x in args.notes.split(",") if x.strip()]

    if args.tone:
        tones = [args.tone]
    else:
        manifest_path = os.path.join(WORKSPACE, "tone", "manifest.json")
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        key = next((k for k in manifest if args.song and args.song in k), None)
        if key is None:
            sys.exit(f"no song matching {args.song!r}")
        tones = []
        for entry in manifest[key]["files"]:
            if args.keyboard and entry["keyboard"] != args.keyboard:
                continue
            tones.append(os.path.join(WORKSPACE, "tone", entry["file"]))

    out_dir = args.out if args.out else os.path.join(WORKSPACE, "render")
    if len(tones) > 1 or not args.out.lower().endswith(".wav"):
        os.makedirs(out_dir, exist_ok=True)
    elif args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)

    import soundfile as sf

    for tone in tones:
        if not os.path.isfile(tone):
            print(f"  skip missing {tone}")
            continue
        name = os.path.splitext(os.path.basename(tone))[0]
        print(f"rendering {name} ...", flush=True)
        plugin, kind = load_plugin_for(tone)
        info = load_patch(plugin, kind, tone)
        audio = render(plugin, notes, note_s=args.note_s)
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak > 0:
            audio = audio / peak * 0.7
        if len(tones) == 1 and args.out.lower().endswith(".wav"):
            path = args.out
        else:
            path = os.path.join(out_dir, name + ".wav")
        sf.write(path, audio.T, SR)
        f = features(audio, SR)
        print(f"  -> {path}")
        if info.get("applied"):
            print(f"     params applied={info['applied']} "
                  f"unmapped={info['missing']}")
        if peak < 1e-6:
            print("     WARNING: render is silent - the patch did not load")
        print(f"     brightness={f.get('brightness_hz')}Hz "
              f"harmonic={f.get('harmonic_ratio')} "
              f"rms={f.get('rms_db')}dB peak={peak:.3f}")

    if args.compare and os.path.isfile(args.compare):
        y, sr = sf.read(args.compare, dtype="float32", always_2d=True)
        f = features(y.T, sr)
        print(f"\nsource stem {os.path.basename(args.compare)}:")
        print(f"  brightness={f.get('brightness_hz')}Hz "
              f"harmonic={f.get('harmonic_ratio')} rms={f.get('rms_db')}dB")


if __name__ == "__main__":
    main()
