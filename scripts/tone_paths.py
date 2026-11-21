"""Layout of the tone tree: one directory per song, then keyboard, then plugin.

    <workspace>/tone/<song-slug>/<right|left|pedal>/<surge-xt|sforzando>/

Why the song directory is a pinyin slug and not the song title
--------------------------------------------------------------
sforzando's ARIA engine decodes the whole .sfz path with the system code page
(GBK on a Chinese Windows), not UTF-8. A CJK segment anywhere in the path turns
into mojibake and the instrument silently loads nothing — this is the same
constraint that already forces `right`/`left`/`pedal` instead of `右手`/`左手`/`脚`
and ASCII .sfz filenames. Every song title in this project is Chinese, so the
song directory has to be ASCII too. The readable title is kept in the manifest,
in `song.txt` inside the directory, and in the SFZ comments.

The slug is a pure function of the title, so a rebuild lands in the same
directory instead of creating a second one.
"""
import hashlib
import os
import re

# Keyboard -> ASCII directory. ARIA decodes the whole path, so these cannot be
# the Chinese names even though the manifest and reports use them.
KEYBOARD_DIRS = {"右手": "right", "左手": "left", "脚": "pedal"}

# Directory name -> keyboard, for reading a tree back.
DIR_KEYBOARDS = {v: k for k, v in KEYBOARD_DIRS.items()}

# Plugin directory names, matching the manifest's `plugin` field.
PLUGIN_DIRS = {"surge-xt": "surge-xt", "sforzando": "sforzando"}

# Fallback when pypinyin is unavailable: a stable hash-only slug. It is uglier
# but still deterministic and still ASCII, so the pipeline keeps working.
_HASH_ONLY = "song"


def ascii_keyboard(keyboard):
    """ASCII directory name for a keyboard (right / left / pedal)."""
    return KEYBOARD_DIRS.get(keyboard, "kb")


def song_slug(title, max_len=48):
    """ASCII directory name for a song title.

    Pinyin is used because it stays readable and stable: `小咪 - 我走后` becomes
    `xiaomi-wozouhou`. A short hash of the original title is appended so two
    different titles that transliterate to the same pinyin cannot collide.
    """
    text = (title or "").strip()
    if not text:
        return _HASH_ONLY

    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]

    try:
        from pypinyin import lazy_pinyin
        parts = lazy_pinyin(text)
    except ImportError:  # pragma: no cover - pypinyin is installed by setup-env
        parts = [text]

    joined = "".join(parts)
    # Keep ASCII letters/digits, collapse every other run to a single dash.
    slug = re.sub(r"[^A-Za-z0-9]+", "-", joined).strip("-").lower()
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        slug = _HASH_ONLY
    return f"{slug[:max_len].strip('-')}-{digest}"


def song_dir(tone_root, title):
    """Absolute path of a song's tone directory."""
    return os.path.join(tone_root, song_slug(title))


def plugin_dir(tone_root, title, keyboard, plugin):
    """Absolute path of `<tone>/<song>/<keyboard>/<plugin>`."""
    return os.path.join(tone_root, song_slug(title),
                        ascii_keyboard(keyboard), PLUGIN_DIRS.get(plugin, plugin))


def rel_to_song(tone_root, title, path):
    """Path relative to the song directory (what the manifest stores)."""
    return os.path.relpath(path, song_dir(tone_root, title))


def abs_from_song(tone_root, title, rel):
    """Resolve a manifest path (relative to the song directory)."""
    return os.path.join(song_dir(tone_root, title), rel)


def write_song_title(tone_root, title):
    """Drop a `song.txt` with the readable title into the song directory.

    The directory name is pinyin, so without this the tree is hard to read for
    anyone who does not speak the transliteration.
    """
    d = song_dir(tone_root, title)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "song.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(title + "\n")
    return path
