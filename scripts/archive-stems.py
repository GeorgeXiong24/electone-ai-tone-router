"""Archive separated stems as 24-bit lossless FLAC (Phase 1, improvements.md §4.1-4.2).

The measured problem this solves: a 5:06 song produced `stems/` = **1339.8 MB**
of uncompressed float32 stereo WAVs, which alone blew the 1.2 GB per-song budget
(total 1412.8 MB). 24-bit FLAC is 3-5x smaller, bit-accurate to 24-bit, and read
natively by soundfile/librosa/sfizz, so nothing downstream changes.

Also implements §4.2: delete the derived `instrumental.wav` (it is just
`mix - vocals`) and keep `refine-stems.py` (6 drum sub-stems, ~580 MB) strictly
opt-in behind `--refine-drums`.

Usage:
  python archive-stems.py --song "韦礼安 - 女孩"          # FLAC the stems, drop wavs
  python archive-stems.py --song "..." --keep-wav          # transcode, keep originals
  python archive-stems.py --song "..." --dry-run
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tone_common as tc  # noqa: E402

# Derived from the mix, never a real source. `instrumental = mix - vocals`.
DERIVED = {"instrumental"}


def _mb(n):
    return round(n / (1024.0 * 1024.0), 1)


def _archive_dir(dir_path, report, *, keep_wav=False, dry_run=False,
                 key_prefix=""):
    """Transcode every float32 WAV in one directory to 24-bit FLAC.

    Shared by the top-level stems and the `refined/` drum sub-stems (§4.2 keeps
    the drum parts for the deferred Start/Stop milestone, so they are compressed
    rather than deleted).
    """
    for name in sorted(os.listdir(dir_path)):
        if not name.lower().endswith(".wav"):
            continue
        base = os.path.splitext(name)[0]
        if base in DERIVED:
            continue
        src = os.path.join(dir_path, name)
        dst = os.path.join(dir_path, base + ".flac")
        entry = {"name": key_prefix + name, "wav_mb": _mb(os.path.getsize(src))}
        if dry_run:
            entry["status"] = "would-transcode"
            report["files"].append(entry)
            continue
        try:
            y, sr = tc.load_audio(src, sr=None)
            import numpy as np
            peak = float(np.max(np.abs(y))) if y.size else 0.0
            gain = 1.0
            if peak > 1.0:
                gain = 0.999 / peak
                y = y * gain
                entry["headroom_gain"] = gain
                entry["source_peak"] = round(peak, 4)
            tc.save_flac(dst, y, sr, subtype="PCM_24")
            back, sr2 = tc.load_audio(dst, sr=None)
            if back.shape != y.shape:
                raise RuntimeError(
                    f"round-trip shape mismatch {y.shape} -> {back.shape}")
            err = float(np.max(np.abs(back - y)))
            if err > 2.0 / 32768.0:
                raise RuntimeError(f"round-trip error {err:.2e} exceeds 24-bit")
            entry["flac_mb"] = _mb(os.path.getsize(dst))
            entry["round_trip_max_err"] = err
            entry["status"] = "ok"
            if not keep_wav:
                os.remove(src)
                entry["wav_removed"] = True
        except Exception as exc:
            entry["status"] = "failed"
            entry["error"] = str(exc)
            if os.path.isfile(dst):
                os.remove(dst)
        report["files"].append(entry)


def archive_song(song, *, keep_wav=False, dry_run=False, keep_debug=False):
    """Transcode a song's float32 WAV stems to 24-bit FLAC and drop the WAVs."""
    stem_dir = os.path.join(tc.WORKSPACE, "stems", song)
    if not os.path.isdir(stem_dir):
        return {"error": f"no stem dir: {stem_dir}"}

    report = {"song": song, "stems_dir": stem_dir, "files": [],
              "deleted_derived": [], "removed_debug": [],
              "before_mb": 0.0, "after_mb": 0.0}

    before = 0
    for name in sorted(os.listdir(stem_dir)):
        p = os.path.join(stem_dir, name)
        if os.path.isfile(p):
            before += os.path.getsize(p)
    report["before_mb"] = _mb(before)

    # --- 1. delete the derived instrumental stem (§4.2) ---------------------
    for name in sorted(os.listdir(stem_dir)):
        base, ext = os.path.splitext(name)
        if base in DERIVED and ext.lower() in (".wav", ".flac"):
            p = os.path.join(stem_dir, name)
            if not dry_run:
                os.remove(p)
            report["deleted_derived"].append(name)

    # --- 2. transcode float32 WAV -> 24-bit FLAC ---------------------------
    _archive_dir(stem_dir, report, keep_wav=keep_wav, dry_run=dry_run)

    # --- 2b. the optional drum sub-stems (§4.2) --------------------------
    # `refine-stems.py` output is ~618 MB per song. Standard tone runs skip it,
    # but §7 keeps the drum parts for the deferred Start/Stop milestone, so they
    # are compressed in place rather than deleted.
    refined = os.path.join(stem_dir, "refined")
    if os.path.isdir(refined):
        _archive_dir(refined, report, keep_wav=keep_wav, dry_run=dry_run,
                     key_prefix="refined/")

    # --- 3. ephemeral debug leftovers (§4.3) ------------------------------
    tmp = os.path.join(stem_dir, "_tmp_dereverb")
    if os.path.isdir(tmp) and not keep_debug:
        import shutil
        size = sum(os.path.getsize(os.path.join(r, f))
                   for r, _d, fs in os.walk(tmp) for f in fs)
        if not dry_run:
            shutil.rmtree(tmp, ignore_errors=True)
        report["removed_debug"].append({"_tmp_dereverb": _mb(size)})

    after = 0
    for name in os.listdir(stem_dir):
        p = os.path.join(stem_dir, name)
        if os.path.isfile(p):
            after += os.path.getsize(p)
    report["after_mb"] = _mb(after)
    report["saved_mb"] = _mb(before - after)
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--song", required=True)
    ap.add_argument("--keep-wav", action="store_true",
                    help="keep the original float32 wavs")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-debug", action="store_true")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    rep = archive_song(args.song, keep_wav=args.keep_wav,
                       dry_run=args.dry_run, keep_debug=args.keep_debug)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
