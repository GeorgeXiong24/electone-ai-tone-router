"""Detect separation failure segments in a stem folder.

Two failure modes are caught:

  dumped        the model copied the whole mix into `other` instead of
                separating it (other ~= mix in both level and waveform)
  silent-melodic  the mix is loud but every melodic stem is near-silent

Usage:
  python check-stems.py --stems <stem dir> --mix <original wav> [--window 10]

Exit code is 0 even when failures are found; the report is the output.
"""
import argparse
import os
import sys

import numpy as np
import soundfile as sf

MELODIC = ["vocals", "drums", "bass", "guitar", "piano"]
ALL_STEMS = MELODIC + ["other"]

LEVEL_TOL_DB = 3.0
CORR_MIN = 0.80
SILENT_GAP_DB = 20.0
MIX_ACTIVE_DB = -45.0


def db(v):
    return 20.0 * np.log10(v + 1e-12)


def load_mono(path):
    x, sr = sf.read(path, dtype="float32", always_2d=True)
    return x.mean(axis=1), sr


def merge_segments(starts, window):
    if not starts:
        return []
    out = []
    start = prev = starts[0]
    for t in starts[1:]:
        if abs(t - (prev + window)) < 1e-6:
            prev = t
        else:
            out.append((start, prev + window))
            start = prev = t
    out.append((start, prev + window))
    return out


def fmt_time(sec):
    return "%d:%04.1f" % (int(sec // 60), sec % 60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stems", required=True)
    ap.add_argument("--mix", required=True)
    ap.add_argument("--window", type=float, default=10.0)
    ap.add_argument("--max-lines", type=int, default=15)
    args = ap.parse_args()

    missing = [n for n in ALL_STEMS if not os.path.exists(os.path.join(args.stems, n + ".wav"))]
    if missing:
        print("SKIP: missing stems: " + ", ".join(missing))
        return 0
    if not os.path.exists(args.mix):
        print("SKIP: mix not found: " + args.mix)
        return 0

    mix, sr = load_mono(args.mix)
    stems = {}
    for n in ALL_STEMS:
        x, _ = load_mono(os.path.join(args.stems, n + ".wav"))
        stems[n] = x

    total_len = min([len(mix)] + [len(v) for v in stems.values()])
    win = int(args.window * sr)
    if win <= 0 or total_len < win:
        print("SKIP: audio shorter than one window")
        return 0

    n_win = total_len // win
    dumped = []
    silent = []
    rows = []

    for i in range(n_win):
        s, e = i * win, (i + 1) * win
        m = mix[s:e]
        o = stems["other"][s:e]
        mix_rms = db(np.sqrt((m ** 2).mean()))
        other_rms = db(np.sqrt((o ** 2).mean()))
        if o.std() > 1e-7 and m.std() > 1e-7:
            corr = float(np.corrcoef(o, m)[0, 1])
        else:
            corr = float("nan")
        mel_max = max(db(np.sqrt((stems[n][s:e] ** 2).mean())) for n in MELODIC)

        is_dumped = (
            corr == corr
            and corr >= CORR_MIN
            and abs(other_rms - mix_rms) <= LEVEL_TOL_DB
            and mix_rms > MIX_ACTIVE_DB
        )
        is_silent = mix_rms > MIX_ACTIVE_DB and mel_max < mix_rms - SILENT_GAP_DB

        if is_dumped:
            dumped.append(i * args.window)
        if is_silent:
            silent.append(i * args.window)
        if is_dumped or is_silent:
            rows.append((i * args.window, mix_rms, other_rms, corr, mel_max,
                         "dumped" if is_dumped else "silent-melodic"))

    # whole-file coherence
    summed = sum(stems[n][:total_len] for n in ALL_STEMS)
    resid = db(np.sqrt(((summed - mix[:total_len]) ** 2).mean()))
    corr_all = float(np.corrcoef(summed, mix[:total_len])[0, 1])

    print("")
    print("=== separation failure scan (window %.1fs) ===" % args.window)
    print("  windows        : %d  (%.1fs of audio)" % (n_win, n_win * args.window))
    print("  sum(6)/mix     : corr %.6f  residual %.1f dB" % (corr_all, resid))

    dumped_seg = merge_segments(dumped, args.window)
    silent_seg = merge_segments(silent, args.window)

    if not dumped_seg and not silent_seg:
        print("  result         : OK - no failure segment detected")
    else:
        print("  result         : FAILURES DETECTED")
        if dumped_seg:
            print("  dumped         : %d window(s), %d segment(s)" % (len(dumped), len(dumped_seg)))
            for a, b in dumped_seg:
                print("                   %s - %s  (model put the whole mix into `other`)" % (fmt_time(a), fmt_time(b)))
        if silent_seg:
            print("  silent-melodic : %d window(s), %d segment(s)" % (len(silent), len(silent_seg)))
            for a, b in silent_seg:
                print("                   %s - %s  (mix is loud, all melodic stems near-silent)" % (fmt_time(a), fmt_time(b)))

        print("")
        print("  %-9s %8s %8s %7s %9s  %s" % ("start", "mix", "other", "corr", "mel-max", "flag"))
        for t, mr, orr, c, mm, flag in rows[: args.max_lines]:
            cs = "%7.3f" % c if c == c else "    n/a"
            print("  %-9s %7.1f %8.1f %s %9.1f  %s" % (fmt_time(t), mr, orr, cs, mm, flag))
        if len(rows) > args.max_lines:
            print("  ... %d more window(s)" % (len(rows) - args.max_lines))

    # per-stem usability
    print("")
    print("  stem usability (10s windows above -40 dB):")
    for n in ALL_STEMS:
        x = stems[n]
        seg = sr * 10
        if len(x) < seg:
            continue
        vals = np.array([db(np.sqrt((x[j:j + seg] ** 2).mean())) for j in range(0, len(x) - seg, seg)])
        if len(vals) == 0:
            continue
        usable = int((vals > -40).sum())
        pct = 100.0 * usable / len(vals)
        note = ""
        if pct < 5:
            note = "  <-- EMPTY (instrument likely absent or failed)"
        elif pct < 50:
            note = "  <-- SPARSE"
        print("    %-10s %3d/%-3d (%5.1f%%)  max %6.1f dB%s" % (n, usable, len(vals), pct, vals.max(), note))

    print("")
    print("  note: `other` is the residual bucket (mix minus the other five stems),")
    print("        not an instrument track. Do not use it as 'the piano part'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
