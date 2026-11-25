"""Reverse-engineer sfizz's 96-byte component state so an .sfz can be injected.

Measured layout (192-byte preset_data, Comp chunk at offset 48, size 96):

  05 00 00 00 00 00 00 00  -> ?
  01 00 00 00 00 00 00 00  -> ?
  00 40 00 00 00 00 00 00  -> ?
  00 20 00 01 00 00 00 00  -> ?
  3c 00 00 00 00 00 dc 43  -> ?
  00 00 00 02 00 00 00 01  -> ?
  00 00 00 0a 00 00 00 03  -> ?
  00 00 00 00 00 ...

This probes whether the payload embeds the .sfz path (it must, since that is the
only instrument identity sfizz has) and where.
"""
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = os.path.join(ROOT, "workspace", "_plugin_refs", "sfizz_preset_data.bin")

b = open(REF, "rb").read()
comp = b[48:48 + 96]
print(f"component state = {len(comp)} bytes\n")
for i in range(0, len(comp), 16):
    chunk = comp[i:i + 16]
    hexs = " ".join(f"{c:02x}" for c in chunk)
    asc = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
    print(f"  {i:3d}: {hexs:<48} |{asc}|")
print()
# Interpret as a sequence of little-endian int32s.
ints = struct.unpack("<24i", comp)
print("as int32:", ints)
# Any embedded string at all?
print(f"\nprintable strings: {[s for s in __import__('re').findall(rb'[ -~]{3,}', comp)]}")
# Does sfizz put the path AFTER the comp chunk (i.e. inside Cont or tail)?
print(f"tail after chunk list: {b[144 + 48:]!r}")
print(f"entire file hex:\n{b.hex()}")
