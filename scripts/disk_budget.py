"""Per-song disk budget gate (`improvements.md` Section 4.5).

Computes the total bytes of **every** directory a single song touches and hard-
asserts the sum stays under 1.2 GB (warn above 800 MB, fail above 1200 MB). The
budget exists because the current pipeline overran it (measured 1,412.8 MB for
*韦礼安 - 女孩*: 1,339.8 MB of it was seven uncompressed float32 stereo WAVs plus
six refined drum WAVs). With 24-bit FLAC archival and no `refine-stems.py` by
default (Section 4.1-4.2) a typical 5-minute song lands in the 200-350 MB band.

Stages counted (all under the workspace):
    audio/          <song>.wav|flac
    stems/          <song>/  (incl. any refined/ sub-stems)
    analysis/       <song>/
    tone/           <song-slug>/   (+ tone/eval clips for this song)
    cantabile/      <song>*        (.cantabileSong + reports)

Usage:
    python disk_budget.py --song "韦礼安 - 女孩"           # report + gate
    python disk_budget.py --song ... --json out.json       # machine-readable
    from disk_budget import check_song_budget              # in the pipeline
"""
import argparse
import datetime
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tone_common as tc  # noqa: E402
import tone_paths  # noqa: E402

WARN_MB = tc.GATES["disk_warn_mb"]
BUDGET_MB = tc.GATES["disk_budget_mb"]


def _dir_bytes(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _match_bytes(root, pattern):
    total = 0
    for p in glob.glob(os.path.join(root, pattern)):
        if os.path.isdir(p):
            total += _dir_bytes(p)
        else:
            total += os.path.getsize(p)
    return total


def measure_song_bytes(song, workspace=tc.WORKSPACE):
    """Per-stage byte breakdown for one song (dict stage -> bytes)."""
    slug = tone_paths.song_slug(song)
    stages = {
        "audio": _match_bytes(os.path.join(workspace, "audio"), song + ".*"),
        "stems": _dir_bytes(os.path.join(workspace, "stems", song))
        if os.path.isdir(os.path.join(workspace, "stems", song)) else 0,
        "analysis": _dir_bytes(os.path.join(workspace, "analysis", song))
        if os.path.isdir(os.path.join(workspace, "analysis", song)) else 0,
        "tone": _dir_bytes(os.path.join(workspace, "tone", slug))
        if os.path.isdir(os.path.join(workspace, "tone", slug)) else 0,
    }
    # Eval clips and Cantabile artifacts that mention this song.
    eval_dir = os.path.join(workspace, "tone", "eval")
    stages["tone_eval"] = _match_bytes(eval_dir, "*" + song + "*") \
        if os.path.isdir(eval_dir) else 0
    cantabile = os.path.join(workspace, "cantabile")
    stages["cantabile"] = _match_bytes(cantabile, "*" + song + "*") \
        if os.path.isdir(cantabile) else 0
    return stages


def check_song_budget(song, workspace=tc.WORKSPACE):
    """Return (ok, report_dict). `ok` is False when over the 1.2 GB budget."""
    stages = measure_song_bytes(song, workspace)
    total = sum(stages.values())
    total_mb = total / (1024.0 * 1024.0)
    per_stage_mb = {k: round(v / (1024.0 * 1024.0), 1) for k, v in stages.items()}
    ok = total_mb < BUDGET_MB
    level = ("ok" if total_mb < WARN_MB
             else "warn" if total_mb < BUDGET_MB else "over")
    report = {
        "song": song,
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "total_mb": round(total_mb, 1),
        "per_stage_mb": per_stage_mb,
        "warn_mb": WARN_MB, "budget_mb": BUDGET_MB,
        "level": level, "ok": ok,
    }
    return ok, report


def format_report(report):
    lines = [f"## 磁盘预算 — {report['song']}",
             f"总计 **{report['total_mb']:.1f} MB**"
             f"（预算 {report['budget_mb']:.0f} MB，"
             f"{'达标' if report['ok'] else '超预算！'} [{report['level']}]）", "",
             "| 阶段 | 大小 MB |", "| --- | --- |"]
    for k, v in report["per_stage_mb"].items():
        lines.append(f"| {k} | {v:.1f} |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--song", required=True, help="song title")
    ap.add_argument("--json", default="", help="write machine-readable report")
    ap.add_argument("--soft", action="store_true",
                    help="report only; never exit non-zero on overrun")
    args = ap.parse_args()
    ok, report = check_song_budget(args.song)
    print(format_report(report))
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\nwrote {args.json}")
    if not ok and not args.soft:
        sys.exit(1)


if __name__ == "__main__":
    main()
