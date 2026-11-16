"""Detect separation failures in a stem folder, for any number of stems.

The stem set is discovered from the folder, so 2-stem karaoke models, the
6-stem instrument model and the 53-stem mega model are all handled the same
way. `instrumental` is treated as derived (it is `mix - vocals`) and excluded
from the reconstruction sum so it is not counted twice.

Failure modes reported per window:

  dumped        the catch-all `other` stem is a copy of the mix (only checked
                when the model has an `other` bucket)
  energy-loss   the stems do not reconstruct the mix: summed level is far
                below the mix, i.e. the model dropped content
  all-silent    the mix is loud but every stem is near-silent

Usage:
  python check-stems.py --stems <stem dir> --mix <original wav> [--window 10]

Exit code is 0 even when failures are found; the report is the output.
"""
import argparse
import os
import sys

import numpy as np
import soundfile as sf

# Stems derived from the mix rather than predicted by the model.
DERIVED = {"instrumental"}

LEVEL_TOL_DB = 3.0
CORR_MIN = 0.80
SILENT_GAP_DB = 20.0
ENERGY_LOSS_DB = 10.0
MIX_ACTIVE_DB = -45.0
USABLE_DB = -40.0


def db(v):
    return 20.0 * np.log10(v + 1e-12)


def rms_db(x):
    if len(x) == 0:
        return float("-inf")
    return db(np.sqrt((x ** 2).mean()))


class StemReader:
    """Windowed reader so 50+ stems never sit in memory at once."""

    def __init__(self, path):
        self.f = sf.SoundFile(path)
        self.sr = self.f.samplerate
        self.frames = len(self.f)

    def read(self, start, frames):
        self.f.seek(start)
        x = self.f.read(frames, dtype="float32", always_2d=True)
        if len(x) == 0:
            return np.zeros(0, dtype=np.float32)
        return x.mean(axis=1)

    def close(self):
        self.f.close()


def discover(stem_dir):
    names = []
    for f in sorted(os.listdir(stem_dir)):
        if f.lower().endswith(".wav"):
            names.append(os.path.splitext(f)[0])
    return names


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
    ap.add_argument("--max-stems", type=int, default=80)
    args = ap.parse_args()

    if not os.path.isdir(args.stems):
        print("SKIP: stems folder not found: %s" % args.stems)
        return 0
    if not os.path.exists(args.mix):
        print("SKIP: mix not found: %s" % args.mix)
        return 0

    names = discover(args.stems)
    if not names:
        print("SKIP: no wav files in %s" % args.stems)
        return 0

    contributors = [n for n in names if n not in DERIVED]
    if not contributors:
        print("SKIP: only derived stems present")
        return 0

    mix, sr = sf.read(args.mix, dtype="float32", always_2d=True)
    mix = mix.mean(axis=1)

    readers = {}
    try:
        for n in contributors:
            r = StemReader(os.path.join(args.stems, n + ".wav"))
            if r.sr != sr:
                print("SKIP: sample rate mismatch in %s (%d vs %d)" % (n, r.sr, sr))
                return 0
            readers[n] = r

        total = min([len(mix)] + [r.frames for r in readers.values()])
        win = int(args.window * sr)
        if win <= 0 or total < win:
            print("SKIP: audio shorter than one window")
            return 0

        n_win = total // win
        has_other = "other" in readers

        dumped, energy_loss, all_silent = [], [], []
        rows = []
        levels = {n: np.empty(n_win, dtype=np.float64) for n in contributors}
        rel = {n: np.empty(n_win, dtype=np.float64) for n in contributors}
        mix_levels = np.empty(n_win, dtype=np.float64)
        recon_num = 0.0
        recon_den = 0.0
        mix_sq = 0.0

        for i in range(n_win):
            s, e = i * win, (i + 1) * win
            m = mix[s:e]
            mix_rms = rms_db(m)
            mix_levels[i] = mix_rms

            seg = {n: readers[n].read(s, win) for n in contributors}
            total_seg = None
            for n, x in seg.items():
                lv = rms_db(x)
                levels[n][i] = lv
                rel[n][i] = lv - mix_rms if np.isfinite(lv) and np.isfinite(mix_rms) else -999.0
                total_seg = x if total_seg is None else total_seg + x

            sum_rms = rms_db(total_seg)
            max_stem = max(levels[n][i] for n in contributors)
            active = mix_rms > MIX_ACTIVE_DB

            # reconstruction fidelity for the whole file (energy weighted)
            recon_num += float(((total_seg - m) ** 2).sum())
            recon_den += float((m ** 2).sum())
            mix_sq += float((m ** 2).sum())

            is_dumped = False
            other_rms = other_corr = float("nan")
            if has_other:
                other_rms = levels["other"][i]
                o = seg["other"]
                if o.std() > 1e-7 and m.std() > 1e-7:
                    other_corr = float(np.corrcoef(o, m)[0, 1])
                is_dumped = (
                    active
                    and other_corr == other_corr
                    and other_corr >= CORR_MIN
                    and abs(other_rms - mix_rms) <= LEVEL_TOL_DB
                )

            is_loss = active and (sum_rms < mix_rms - ENERGY_LOSS_DB)
            is_silent = active and max_stem < mix_rms - SILENT_GAP_DB

            if is_dumped:
                dumped.append(i * args.window)
            if is_loss:
                energy_loss.append(i * args.window)
            if is_silent:
                all_silent.append(i * args.window)
            if is_dumped or is_loss or is_silent:
                flag = "dumped" if is_dumped else ("energy-loss" if is_loss else "all-silent")
                rows.append((i * args.window, mix_rms, sum_rms, other_rms, other_corr, max_stem, flag))
    finally:
        for r in readers.values():
            r.close()

    resid_total = db(np.sqrt(recon_num / max(n_win * win, 1)))
    mix_total = db(np.sqrt(mix_sq / max(n_win * win, 1)))
    sum_gap = resid_total - mix_total  # rough: >0 means stems over-sum

    print("")
    print("=== separation failure scan (window %.1fs) ===" % args.window)
    print("  stems          : %d  (%s)" % (len(names), ", ".join(names)))
    print("  windows        : %d  (%.1fs of audio)" % (n_win, n_win * args.window))
    print("  recon vs mix   : mix %.1f dB, residual %.1f dB" % (mix_total, resid_total))
    if sum_gap > 3.0:
        print("  over-sum       : stems overlap (multi-label model), summing is not a")
        print("                   partition of the mix - this is expected, not a failure")

    dumped_seg = merge_segments(dumped, args.window)
    loss_seg = merge_segments(energy_loss, args.window)
    silent_seg = merge_segments(all_silent, args.window)

    if not (dumped_seg or loss_seg or silent_seg):
        print("  result         : OK - no failure segment detected")
    else:
        print("  result         : FAILURES DETECTED")
        if dumped_seg:
            print("  dumped         : %d window(s), %d segment(s)" % (len(dumped), len(dumped_seg)))
            for a, b in dumped_seg:
                print("                   %s - %s  (whole mix copied into `other`)" % (fmt_time(a), fmt_time(b)))
        if loss_seg:
            print("  energy-loss    : %d window(s), %d segment(s)" % (len(energy_loss), len(loss_seg)))
            for a, b in loss_seg:
                print("                   %s - %s  (stems sum far below the mix)" % (fmt_time(a), fmt_time(b)))
        if silent_seg:
            print("  all-silent     : %d window(s), %d segment(s)" % (len(all_silent), len(silent_seg)))
            for a, b in silent_seg:
                print("                   %s - %s  (mix is loud, every stem near-silent)" % (fmt_time(a), fmt_time(b)))

        print("")
        print("  %-9s %8s %9s %8s %7s %9s  %s"
              % ("start", "mix", "sum", "other", "corr", "max-stem", "flag"))
        for t, mr, srms, orr, oc, mm, flag in rows[: args.max_lines]:
            os_ = "%8.1f" % orr if orr == orr else "     n/a"
            cs = "%7.3f" % oc if oc == oc else "    n/a"
            print("  %-9s %7.1f %9.1f %s %s %9.1f  %s" % (fmt_time(t), mr, srms, os_, cs, mm, flag))
        if len(rows) > args.max_lines:
            print("  ... %d more window(s)" % (len(rows) - args.max_lines))

    # per-stem usability
    order = sorted(contributors, key=lambda n: -float(np.nanmax(levels[n])))
    print("")
    print("  stem usability (10s windows above %.0f dB abs; rel = level vs mix)" % USABLE_DB)
    for n in order[: args.max_stems]:
        vals = levels[n]
        usable = int((vals > USABLE_DB).sum())
        pct = 100.0 * usable / len(vals)
        med_rel = float(np.median(rel[n]))
        note = ""
        if pct < 5:
            note = "  <-- EMPTY"
        elif pct < 50:
            note = "  <-- SPARSE"
        print("    %-18s %3d/%-3d (%5.1f%%)  max %6.1f dB  median %+6.1f dB rel%s"
              % (n, usable, len(vals), pct, float(np.nanmax(vals)), med_rel, note))
    if len(order) > args.max_stems:
        print("    ... %d more stem(s)" % (len(order) - args.max_stems))

    print("")
    if has_other:
        print("  note: `other` is the residual bucket (mix minus the named stems),")
        print("        not an instrument track. Do not use it as 'the piano part'.")
    else:
        print("  note: this model has no `other` bucket; every stem is a named source.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
