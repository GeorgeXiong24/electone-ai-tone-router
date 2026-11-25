"""Read the REAL VST3 plugin descriptors straight out of the binaries.

VST3 embeds a plugin descriptor (class id + name + category) in an ASCII string
table. This dumps the strings around the factory metadata so the true class id is
read rather than guessed.
"""
import os
import re
import sys

TARGETS = {
    "sfizz": r"C:\Program Files\Common Files\VST3\sfizz.vst3"
             r"\Contents\x86_64-win\sfizz.vst3",
    "Vital": r"C:\Program Files\Common Files\VST3\Vital.vst3",
    "Surge XT": r"C:\Program Files\Common Files\VST3\Surge Synth Team"
                r"\Surge XT.vst3\Contents\x86_64-win\Surge XT.vst3",
    "sforzando": r"C:\Program Files\Common Files\VST3\sforzando.vst3"
                 r"\Contents\x86_64-win\sforzando.vst3",
}

# Known-good values, to validate the extraction against ground truth.
EXPECT = {
    "Surge XT": b"abcdef019182faeb566d624153675854",
    "sforzando": b"5c5ca79682fc437ab6539ba204bab349",
}


def ascii_table(path, min_len=4):
    """All printable ASCII runs with their offsets."""
    with open(path, "rb") as f:
        blob = f.read()
    out = []
    for m in re.finditer(rb"[\x20-\x7e]{%d,}" % min_len, blob):
        out.append((m.start(), m.group(0)))
    return blob, out


def find_descriptors(path, label):
    print(f"\n{'=' * 68}\n{label}\n{'=' * 68}")
    if not os.path.isfile(path):
        print(f"  missing: {path}")
        return None
    blob, table = ascii_table(path)
    print(f"  {len(blob)} bytes, {len(table)} ascii runs")

    # A VST3 descriptor names a Processor/Controller class; the class id sits in
    # the same string table. Look for the 'Audio Module Class' / 'Component'
    # markers and dump the neighbouring runs.
    markers = [b"Audio Module Class", b"Component Controller Class",
               b"VST 3", b"Factory"]
    for mk in markers:
        idxs = [i for i, (_o, s) in enumerate(table) if mk in s]
        if idxs:
            print(f"  marker {mk!r}: {len(idxs)} run(s)")
    # Print runs that look like a 16-char id next to a name run.
    for i, (off, s) in enumerate(table):
        if len(s) == 16 and re.fullmatch(rb"[A-Za-z0-9]{16}", s):
            nxt = table[i + 1][1] if i + 1 < len(table) else b""
            prv = table[i - 1][1] if i > 0 else b""
            if any(k in nxt + prv for k in (b"VST", b"Class", b"Vital",
                                             b"sfizz", b"Sfizz")):
                print(f"    0x{off:08x} id={s!r} prev={prv[:24]!r} next={nxt[:24]!r}")
    # Validate against known ground truth.
    if label in EXPECT:
        print(f"  expected id {EXPECT[label]!r} present: {EXPECT[label] in blob}")
    return blob


for label, path in TARGETS.items():
    find_descriptors(path, label)
