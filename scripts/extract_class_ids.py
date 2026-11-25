"""Extract REAL VST3 class ids (and state formats) from the installed binaries.

The previous attempt invented `c9bd8ea0...` (sfizz) and `1c4e04ee...` (Vital).
The user verified Vital's real id is `VSTV` + `itavital` (56535456697461766974616C
00000000). This reads the actual files so the ids cannot be guessed again.
"""
import os
import re
import struct

SFIZZ = (r"C:\Program Files\Common Files\VST3\sfizz.vst3"
         r"\Contents\x86_64-win\sfizz.vst3")
VITAL_DIR = r"C:\Program Files\Common Files\VST3"
# Vital ships as a SINGLE-FILE bundle (improvements.md §2), not a directory.
VITAL = os.path.join(VITAL_DIR, "Vital.vst3")
SURGE = (r"C:\Program Files\Common Files\VST3\Surge Synth Team"
         r"\Surge XT.vst3\Contents\x86_64-win\Surge XT.vst3")

# VST3 class ids are 16-byte ASCII blobs. Known real ones (verified):
KNOWN = {
    b"VSTV" + b"itavital".ljust(12, b"\x00"): "Vital (verified by user)",
    b"abcdef019182faeb566d624153675854": "Surge XT (existing constant)",
    b"5c5ca79682fc437ab6539ba204bab349": "sforzando (existing constant)",
}


def find_class_ids(path, limit=12):
    """Scan a binary for candidate 16-byte VST3 class ids."""
    with open(path, "rb") as f:
        blob = f.read()
    found = []
    seen = set()
    for m in re.finditer(rb"[A-Za-z0-9_]{16}", blob):
        cand = m.group(0)
        if cand in seen:
            continue
        # Real ids are printable ASCII; skip obvious noise.
        if not all(32 <= c < 127 for c in cand):
            continue
        seen.add(cand)
        found.append((m.start(), cand))
    return found[:limit]


def strings(path, needle, width=48):
    with open(path, "rb") as f:
        blob = f.read()
    out = []
    for m in re.finditer(re.escape(needle), blob):
        s = blob[max(0, m.start() - 8):m.start() + width]
        try:
            txt = s.decode("ascii", "replace")
        except Exception:
            continue
        if txt.isprintable() or " " in txt:
            out.append((m.start(), txt))
    return out


print("=" * 70)
print("Vital")
print("=" * 70)
vital = VITAL if os.path.isfile(VITAL) else None
print(f"binary: {vital}")
if vital:
    hits = strings(vital, b"Vital")
    print(f"  'Vital' occurrences: {len(hits)} (first few shown)")
    for off, txt in hits[:6]:
        print(f"    0x{off:08x} {txt!r}")
    ids = [(o, c) for o, c in find_class_ids(vital, 40)
           if b"VST" == c[:3] or b"itavital" in c]
    print(f"  VST-like ids found: {len(ids)}")
    for o, c in ids[:8]:
        print(f"    0x{o:08x} {c!r}  {KNOWN.get(c, '')}")
    # The user reported the real id as 'VSTV' + 'itavital' padded.
    probe = strings(vital, b"itavital", 32)
    print(f"  'itavital' hits: {len(probe)}")
    for off, txt in probe[:4]:
        print(f"    0x{off:08x} {txt!r}")

print()
print("=" * 70)
print("sfizz")
print("=" * 70)
print(f"binary: {SFIZZ} exists={os.path.isfile(SFIZZ)}")
if os.path.isfile(SFIZZ):
    ids = find_class_ids(SFIZZ, 60)
    print(f"  candidate 16-byte tokens: {len(ids)}")
    for o, c in ids[:24]:
        mark = KNOWN.get(c, "")
        if mark or b"sfizz" in c.lower() or c.startswith(b"VST"):
            print(f"    0x{o:08x} {c!r}  {mark}")
    hits = strings(SFIZZ, b"sfizz")
    print(f"  'sfizz' string hits: {len(hits)}")
    for off, txt in hits[:6]:
        print(f"    0x{off:08x} {txt!r}")
