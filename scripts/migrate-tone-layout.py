"""One-shot migration: tone/<keyboard>/<plugin>/ -> tone/<song>/<keyboard>/<plugin>/.

The old tree had no song level, so a song's files were only identifiable by the
filename (Surge patches) or by the manifest (the ASCII-slugged .sfz files).
This script reads both manifests, moves every file to its song directory, and
rewrites the manifest paths to be relative to that directory.

Safe to re-run: files already in place are left alone, and the manifest is
rewritten from the actual on-disk location.
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tone_paths as tp  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.environ.get("ELECTONE_WORKSPACE") or os.path.join(ROOT, "workspace")
TONE_ROOT = os.path.join(WORKSPACE, "tone")

MANIFEST = os.path.join(TONE_ROOT, "manifest.json")
SAMPLES = os.path.join(TONE_ROOT, "sample-instruments.json")


def load(path):
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def move(old_abs, new_abs):
    """Move a file, creating parents. Returns True when something moved."""
    if os.path.abspath(old_abs) == os.path.abspath(new_abs):
        return False
    if not os.path.isfile(old_abs):
        return False
    os.makedirs(os.path.dirname(new_abs), exist_ok=True)
    if os.path.isfile(new_abs):
        # Same content already migrated; drop the stale copy.
        os.remove(old_abs)
        return False
    shutil.move(old_abs, new_abs)
    return True


def main():
    manifest = load(MANIFEST)
    samples = load(SAMPLES)
    moved = 0
    missing = []

    # --- Surge patches + piano .sfz (manifest.json) -------------------------
    for song, entry in manifest.items():
        for f in entry.get("files", []):
            old_rel = f.get("file", "")
            if not old_rel:
                continue
            old_abs = os.path.join(TONE_ROOT, old_rel)
            new_abs = tp.abs_from_song(TONE_ROOT, song, old_rel)
            if move(old_abs, new_abs):
                moved += 1
            if not os.path.isfile(new_abs):
                missing.append(old_rel)
            f["file"] = tp.rel_to_song(TONE_ROOT, song, new_abs)

            vst = f.get("vstpreset")
            if vst:
                old_vst = os.path.join(TONE_ROOT, vst)
                new_vst = tp.abs_from_song(TONE_ROOT, song, vst)
                if move(old_vst, new_vst):
                    moved += 1
                if not os.path.isfile(new_vst):
                    missing.append(vst)
                f["vstpreset"] = tp.rel_to_song(TONE_ROOT, song, new_vst)

        entry["tone_dir"] = tp.song_slug(song)
        tp.write_song_title(TONE_ROOT, song)

    # --- sampled instruments (sample-instruments.json) ----------------------
    for song, entry in samples.items():
        for f in entry.get("files", []):
            sfz = f.get("sfz", "")
            if sfz:
                old_abs = os.path.join(TONE_ROOT, sfz)
                new_abs = tp.abs_from_song(TONE_ROOT, song, sfz)
                if move(old_abs, new_abs):
                    moved += 1
                if not os.path.isfile(new_abs):
                    missing.append(sfz)
                f["sfz"] = tp.rel_to_song(TONE_ROOT, song, new_abs)

            sdir = f.get("samples_dir", "")
            if sdir:
                old_dir = os.path.join(TONE_ROOT, sdir)
                new_dir = tp.abs_from_song(TONE_ROOT, song, sdir)
                if os.path.isdir(old_dir) and os.path.abspath(old_dir) != os.path.abspath(new_dir):
                    os.makedirs(os.path.dirname(new_dir), exist_ok=True)
                    if os.path.isdir(new_dir):
                        # Merge: move the wavs, then drop the empty source.
                        for name in os.listdir(old_dir):
                            src = os.path.join(old_dir, name)
                            dst = os.path.join(new_dir, name)
                            if os.path.isfile(src) and not os.path.isfile(dst):
                                shutil.move(src, dst)
                                moved += 1
                        shutil.rmtree(old_dir, ignore_errors=True)
                    else:
                        shutil.move(old_dir, new_dir)
                        moved += 1
                if not os.path.isdir(new_dir):
                    missing.append(sdir)
                f["samples_dir"] = tp.rel_to_song(TONE_ROOT, song, new_dir)

        entry["tone_dir"] = tp.song_slug(song)
        tp.write_song_title(TONE_ROOT, song)

    save(MANIFEST, manifest)
    save(SAMPLES, samples)

    print(f"moved {moved} file(s)")
    if missing:
        print(f"MISSING {len(missing)}:")
        for m in missing:
            print("  " + m)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
