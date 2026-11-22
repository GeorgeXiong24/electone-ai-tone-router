"""Keep only the N most prominent stems and fold the rest into `other`.

A 53-way model emits every leaf source it knows (triangle, wind-chimes,
harpsichord...). For routing to an Electone, a dozen audible parts is what
matters; the long tail is near-silent and only adds clutter.

Selection is by plain loudness, after discarding anything below --min-db
(default -45 dB, i.e. inaudible). Distinct timbres within one family are kept
on purpose: a guitar, an acoustic guitar and an electric guitar are genuinely
different sounds, so no per-family de-duplication is applied.

The discarded stems are summed into `other` rather than deleted, so no audio is
lost and the reconstruction still matches the mix.

Usage:
  python prune-stems.py --stems <dir> --top 15 [--min-db -45] [--dry-run]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _cache_env  # noqa: E402  (redirect caches into the project workspace)

import numpy as np
import soundfile as sf

# Stems that are always kept when present, regardless of level order.
PROTECTED = {"other", "instrumental"}


def db(v):
    return 20 * np.log10(v + 1e-12)


def energy(path):
    x, _ = sf.read(path, dtype="float32", always_2d=True)
    x = x.mean(axis=1)
    if len(x) == 0:
        return 0.0, 0
    return float(np.sqrt((x ** 2).mean())), len(x)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stems", required=True)
    ap.add_argument("--top", type=int, required=True)
    ap.add_argument("--min-db", type=float, default=-45.0,
                    help="ignore stems quieter than this (default -45 dB)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    names = sorted(os.path.splitext(f)[0] for f in os.listdir(args.stems)
                   if f.lower().endswith(".wav"))
    if not names:
        print("SKIP: no wav files")
        return 0

    levels = {}
    for n in names:
        rms, _ = energy(os.path.join(args.stems, n + ".wav"))
        levels[n] = db(rms)

    by_level = sorted(names, key=lambda n: -levels[n])

    # Stems quieter than this are inaudible in practice; keeping a -80 dB
    # `wind-chimes` because it "represents" a family would be pointless.
    audible = [n for n in by_level if levels[n] > args.min_db]

    # Plain loudness order. A guitar, an acoustic guitar and an electric guitar
    # are genuinely different timbres, so family de-duplication is not applied:
    # the operator asked for the N most prominent parts, and those are the ones.
    kept = [n for n in audible if n not in PROTECTED][: args.top]

    kept += [n for n in names if n in PROTECTED]
    dropped = [n for n in names if n not in kept]

    print("  prune          : keep top %d of %d stem(s)" % (args.top, len(names)))
    print("  kept           : " + (", ".join(
        "%s(%.0fdB)" % (n, levels[n]) for n in kept)))
    if dropped:
        print("  folded to other: %d stem(s)  [%s]" % (len(dropped), ", ".join(dropped[:12]) +
              (" ..." if len(dropped) > 12 else "")))

    if args.dry_run or not dropped:
        return 0

    # 3) fold dropped stems into `other` so nothing is lost
    base = None
    base_len = 0
    sr = None
    for n in dropped:
        x, this_sr = sf.read(os.path.join(args.stems, n + ".wav"),
                             dtype="float32", always_2d=True)
        if len(x) == 0:
            continue
        if sr is None:
            sr = this_sr
        elif this_sr != sr:
            # Mixing different rates would change pitch; refuse rather than
            # silently writing a wrong-tempo `other`.
            print("  SKIP folding: sample rate mismatch (%d vs %d)" % (this_sr, sr))
            return 0
        if base is None:
            base = np.zeros(len(x), dtype=np.float32)
            base_len = len(x)
        base[:len(x)] += x.mean(axis=1)

    if base is None:
        return 0

    other_path = os.path.join(args.stems, "other.wav")
    if os.path.exists(other_path):
        ox, other_sr = sf.read(other_path, dtype="float32", always_2d=True)
        if other_sr != sr:
            print("  SKIP folding: `other` has sample rate %d, stems are %d"
                  % (other_sr, sr))
            return 0
        ox = ox.mean(axis=1)
        m = min(len(ox), base_len)
        merged = np.zeros(base_len, dtype=np.float32)
        merged[:m] = ox[:m] + base[:m]
        merged[m:] = base[m:]
        base = merged
    sf.write(other_path, base, sr, subtype="FLOAT")

    for n in dropped:
        os.remove(os.path.join(args.stems, n + ".wav"))

    print("  -> %d stem(s) remain (+ other absorbs %d)" % (len(kept), len(dropped)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
