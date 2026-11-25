"""Decode the host-saved sfizz state so a new .sfz can be injected.

Measured structure of the 200-byte component state (from Cantabile's own song):

  05 00 00 00 00 00 00 00   header
  6f 00 00 00               uint32 length of the path (111)
  "D:/.../weilian-nvhai-1c28b2b9.sfz"   UTF-8, forward slashes
  ...                        trailing settings

So the instrument IS injectable: rewrite the path (and its length) and Cantabile
restores that .sfz.
"""
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = os.path.join(ROOT, "workspace", "_plugin_refs", "sfizz_host_state.bin")
comp = open(REF, "rb").read()

print(f"component state: {len(comp)} bytes")
print("\nbyte map:")
for i in range(0, len(comp), 16):
    ch = comp[i:i + 16]
    hexs = " ".join(f"{c:02x}" for c in ch)
    asc = "".join(chr(c) if 32 <= c < 127 else "." for c in ch)
    print(f"  {i:3d}: {hexs:<48} |{asc}|")

print("\ninterpretation:")
print(f"  [0:8]   header   = {comp[0:8]!r}")
plen = struct.unpack("<I", comp[8:12])[0]
print(f"  [8:12]  path len = {plen}")
path = comp[12:12 + plen].decode("utf-8")
print(f"  [12:..] path     = {path}")
rest = comp[12 + plen:]
print(f"  tail    ({len(rest)}B) = {rest!r}")
if len(rest) >= 4:
    print(f"  tail ints: {struct.unpack('<%di' % (len(rest) // 4), rest[:len(rest)//4*4])}")


def build_host_state(sfz_path: str) -> bytes:
    """Rebuild the component state for a different .sfz, same layout."""
    p = sfz_path.replace("\\", "/").encode("utf-8")
    return comp[0:8] + struct.pack("<I", len(p)) + p + rest


print("\n=== round-trip test ===")
test = "/tmp/other instrument.sfz"
rebuilt = build_host_state(test)
plen2 = struct.unpack("<I", rebuilt[8:12])[0]
back = rebuilt[12:12 + plen2].decode("utf-8")
print(f"  rebuilt {len(rebuilt)}B  path={back!r}")
print(f"  matches        : {back == test}")
print(f"  tail preserved : {rebuilt[12 + plen2:] == rest}")
