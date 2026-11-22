"""Second-stage refinement: split already-separated stems into finer timbres.

The 6-stem model tells us which instrument families are actually present. This
script then applies a specialist model only to the stems that carry audio, so
absent instruments cost nothing.

Currently drums is the only stem with a usable specialist (DrumSep ->
kick/snare/toms/hh/ride/crash). There is no freely downloadable piano/keys
model in audio-separator's registry, so `piano` stays a single stem.

Usage:
  python refine-stems.py --stems <6-stem dir> [--only drums] [--dry-run]
  python refine-stems.py --latest          # newest stem folder
  python refine-stems.py --song <title>    # pick by title substring

Stems are written next to the parent stems in `<song>/refined/`.
"""
import argparse
import glob
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _cache_env  # noqa: E402  (redirect caches into the project workspace)

import numpy as np
import soundfile as sf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["PATH"] = os.path.join(ROOT, "tools") + os.pathsep + os.environ.get("PATH", "")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MODELS = os.path.join(ROOT, "workspace", "models", "audio-separator")

# stem name -> (model filename, human label)
# Only models whose output actually adds distinct timbres are listed.
# - DrumSep: drums -> kick/snare/toms/hh/ride/crash (verified working).
# - The karaoke model only emits vocals/instrumental (a duplicate of the parent
#   stem), so it is not used. There is no downloadable piano/keys model either,
#   which is why `piano` stays a single stem.
SPECIALISTS = {
    "drums": ("MDX23C-DrumSep-aufr33-jarredou.ckpt",
              "DrumSep -> kick/snare/toms/hh/ride/crash"),
}

# A stem quieter than this (median over 10s windows) is treated as absent.
ABSENT_DB = -45.0


def db(v):
    return 20 * np.log10(v + 1e-12)


def activity(path, sr_hint=None):
    """Return (rms_db, active_fraction) for a stem."""
    x, sr = sf.read(path, dtype="float32", always_2d=True)
    mono = x.mean(axis=1)
    if len(mono) == 0:
        return -120.0, 0.0
    rms = db(np.sqrt((mono ** 2).mean()))
    hop = max(1, int(sr * 0.1))
    env = np.array([np.abs(mono[i:i + hop]).max() for i in range(0, len(mono) - hop, hop)])
    if env.max() <= 0:
        return rms, 0.0
    return rms, float((env > env.max() * 0.05).mean())


def list_stems(stem_dir):
    out = {}
    for p in sorted(glob.glob(os.path.join(stem_dir, "*.wav"))):
        name = os.path.splitext(os.path.basename(p))[0]
        out[name] = p
    return out


def split_name(filename):
    """Extract the source id from '<stem>_(<id>)_<model>.wav'."""
    base = os.path.basename(filename)
    if "_(" in base:
        return base.split("_(")[1].split(")")[0]
    return base


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stems", default="", help="folder with 6-stem output")
    ap.add_argument("--latest", action="store_true",
                    help="use the newest stem folder (avoids passing CJK paths on the CLI)")
    ap.add_argument("--song", default="",
                    help="substring of the song folder name to pick (e.g. a title word)")
    ap.add_argument("--only", default="", help="comma list of stems to refine")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-db", type=float, default=ABSENT_DB)
    args = ap.parse_args()

    stem_dir = args.stems
    if args.latest or args.song or not stem_dir:
        root = os.path.join(ROOT, "workspace", "stems")
        cands = [d for d in glob.glob(os.path.join(root, "*"))
                 if os.path.isdir(d) and os.path.basename(d) not in ("refined", ".staging")
                 and glob.glob(os.path.join(d, "*.wav"))]
        if args.song:
            # Compare by unicode escaping so a CJK query survives a console
            # that cannot represent it.
            key = args.song
            cands = [d for d in cands
                     if key in os.path.basename(d)
                     or key.encode("unicode_escape").decode() in os.path.basename(d).encode("unicode_escape").decode()]
        if not cands:
            print("SKIP: no matching stem folder under %s" % root)
            return 0
        stem_dir = max(cands, key=os.path.getmtime) if not args.song else cands[0]
        print("using stem folder: %s" % os.path.basename(stem_dir))

    stems = list_stems(stem_dir)
    if not stems:
        print("SKIP: no stems in %r" % stem_dir)
        return 0

    print("=== present stems ===")
    present = {}
    for name, path in stems.items():
        if name in ("instrumental", "other"):
            continue
        rms, act = activity(path)
        ok = rms > args.min_db
        present[name] = (path, rms, act, ok)
        print("  %-12s RMS %7.1f dB  active %5.1f%%  %s"
              % (name, rms, act * 100, "present" if ok else "absent (skip)"))

    wanted = [s.strip() for s in args.only.split(",") if s.strip()]
    targets = []
    for name, (path, rms, act, ok) in present.items():
        if not ok:
            continue
        if wanted and name not in wanted:
            continue
        if name not in SPECIALISTS:
            continue
        targets.append(name)

    if not targets:
        print("\nno stem needs refinement (or no specialist for it)")
        return 0

    out_dir = os.path.join(stem_dir, "refined")
    print("\n=== refine plan ===")
    for t in targets:
        print("  %-12s -> %s" % (t, SPECIALISTS[t][1]))
    print("  output: %s" % out_dir)

    if args.dry_run:
        return 0

    from audio_separator.separator import Separator
    from progress import Heartbeat, stage

    stage(2, 3, "二级细分 / refining stems")

    os.makedirs(out_dir, exist_ok=True)
    total = 0
    written = {}
    n_targets = len(targets)
    for idx, t in enumerate(targets, 1):
        path = present[t][0]
        model_file, label = SPECIALISTS[t]
        print("\n  [%d/%d] %s -> %s" % (idx, n_targets, t, label), flush=True)
        try:
            sep = Separator(
                model_file_dir=MODELS,
                output_dir=out_dir,
                output_format="WAV",
            )
            with Heartbeat("loading model %s" % model_file[:40]):
                sep.load_model(model_filename=model_file)
            with Heartbeat("separating %s" % t):
                files = sep.separate(path)
        except Exception as e:
            # A missing or unverifiable model must not throw away stems that
            # already succeeded for other instruments.
            print("  SKIPPED %s: %s" % (t, str(e).splitlines()[0][:150]))
            continue

        for f in files:
            src = f if os.path.isabs(f) else os.path.join(out_dir, f)
            if not os.path.exists(src):
                continue
            sub = split_name(src)
            dst = os.path.join(out_dir, "%s.wav" % sub)
            shutil.move(src, dst)
            rms, act = activity(dst)
            flag = "" if rms > -45 else "   <-- near-silent"
            print("  %-10s RMS %7.1f dB  active %5.1f%%%s" % (sub, rms, act * 100, flag))
            written[sub] = dst
            total += 1

    print("\ndone: %d refined stem(s) in %s" % (total, out_dir))
    for k in sorted(written):
        print("  %s" % os.path.basename(written[k]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
