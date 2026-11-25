"""Verify plugin serialization against the REAL captured states (scheme A)."""
import os
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cantabile_format as cf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sfz = os.path.join(ROOT, "workspace", "tone", "weilian-nvhai-1c28b2b9",
                   "right", "sfizz", "weilian-nvhai-1c28b2b9.sfz")
vital = os.path.join(ROOT, "workspace", "tone", "weilian-nvhai-1c28b2b9",
                     "synth", "guitar.vital")


def decode(b64):
    raw = cf.decode_plugin_data(b64)
    if isinstance(raw, tuple):
        cls, st = raw
        if isinstance(st, bytes) and st[:4] != b"VST3":
            cls, st = cf.parse_abmp(zlib.decompress(st, -15))
        return cls, cf.parse_vst3_state(st)[1]
    return None, None


print("=" * 66)
print("sfizz")
print("=" * 66)
b = cf.sfizz_plugin_data_from_sfz(sfz)
cls, comp = decode(b)
print(f"  class id : {cls!r}")
print(f"  is real  : {cls == cf.SFIZZ_CLASS_ID}  (expected E8FAB718...)")
print(f"  state    : {len(comp)} bytes (real reference is 96)")
print(f"  head     : {comp[:32]!r}")

print()
print("=" * 66)
print("Vital")
print("=" * 66)
if os.path.isfile(vital):
    b2 = cf.vital_plugin_data_from_preset(vital)
    cls2, comp2 = decode(b2)
    print(f"  class id : {cls2!r}")
    print(f"  is real  : {cls2 == cf.VITAL_CLASS_ID}  (expected VSTV+itavital)")
    print(f"  state    : {len(comp2)} bytes")
    print(f"  head     : {comp2[:40]!r}")
    js = cf.parse_vital_component_state(comp2)
    print(f"  json     : {'recovered ' + str(len(js)) + ' chars' if js else 'not found'}")
else:
    print(f"  no .vital at {vital}")

print()
print("=" * 66)
print("sforzando (legacy, unchanged)")
print("=" * 66)
b3 = cf.sforzando_plugin_data_from_sfz(sfz)
cls3, comp3 = decode(b3)
print(f"  class id : {cls3!r}  is real: {cls3 == cf.SFORZANDO_CLASS_ID}")
print(f"  state    : {len(comp3)} bytes")
