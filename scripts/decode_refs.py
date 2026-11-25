"""Decode the captured sfizz / Vital states byte by byte."""
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = os.path.join(ROOT, "workspace", "_plugin_refs")

for name in ("sfizz_preset_data.bin", "vital_preset_data.bin"):
    p = os.path.join(REF, name)
    if not os.path.isfile(p):
        print(f"{name}: missing")
        continue
    b = open(p, "rb").read()
    print("=" * 70)
    print(f"{name}: {len(b)} bytes")
    print("=" * 70)
    print(f"  magic      : {b[:4]!r}")
    print(f"  version    : {struct.unpack('<i', b[4:8])[0]}")
    cid = b[8:40]
    print(f"  class id   : {cid!r}")
    off = struct.unpack("<Q", b[40:48])[0]
    print(f"  list offset: {off} (file len {len(b)})")
    print(f"  at offset  : {b[off:off + 4]!r}")
    if b[off:off + 4] == b"List":
        cnt = struct.unpack("<i", b[off + 4:off + 8])[0]
        print(f"  chunks     : {cnt}")
        pos = off + 8
        for i in range(cnt):
            eid = b[pos:pos + 4]
            eoff, esize = struct.unpack("<QQ", b[pos + 4:pos + 20])
            print(f"    [{i}] {eid!r} off={eoff} size={esize}")
            if eid == b"Comp":
                comp = b[eoff:eoff + esize]
                print(f"        comp head: {comp[:64]!r}")
                print(f"        comp len : {len(comp)}")
            pos += 20
    # sfizz payload inspection: after the chunk list there may be a path.
    tail = b[off + 8 + 20 * max(1, (len(b) - off - 8) // 20):]
    print(f"  tail guess : {b[off + 48:off + 48 + 200]!r}")
