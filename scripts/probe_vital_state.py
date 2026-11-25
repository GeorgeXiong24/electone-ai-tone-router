"""Read the REAL Vital component state and class id via pedalboard (scheme A).

The previous constants (VITAL_CLASS_ID = b"1c4e04ee...") were invented. This uses
the live plugin, exactly as the user did: load Vital, save a preset, read
`preset_data` / `raw_state`, and dump the bytes so the real container can be
reproduced byte-for-byte.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _cache_env  # noqa: E402

VITAL = r"C:\Program Files\Common Files\VST3\Vital.vst3"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "workspace", "_vital_probe")
os.makedirs(OUT, exist_ok=True)


def dump(tag, data, limit=96):
    if data is None:
        print(f"  {tag}: None")
        return
    print(f"  {tag}: {len(data)} bytes")
    print(f"    head: {data[:limit]!r}")
    # printable strings
    import re
    strs = [s for s in re.findall(rb"[\x20-\x7e]{6,}", data[:4096])][:8]
    for s in strs:
        print(f"    str : {s[:100]!r}")


from pedalboard import load_plugin      # noqa: E402
print(f"loading {VITAL}")
pl = load_plugin(VITAL)
print(f"  name={pl.name!r} manuf={pl.manufacturer_name!r} ver={pl.version!r}")
print(f"  identifier={getattr(pl, 'identifier', None)!r}")

print("\n=== preset_data (a .vital JSON) ===")
pd = pl.preset_data
if isinstance(pd, (bytes, bytearray)):
    dump("preset_data", bytes(pd))
    open(os.path.join(OUT, "preset_data.bin"), "wb").write(bytes(pd))
elif isinstance(pd, str):
    print(f"  preset_data is str, {len(pd)} chars")
    print(f"    head: {pd[:300]!r}")
    open(os.path.join(OUT, "preset_data.json"), "w", encoding="utf-8").write(pd)
else:
    print(f"  preset_data type {type(pd)}")

print("\n=== raw_state (the VST3 component state) ===")
try:
    rs = pl.raw_state
    dump("raw_state", rs, 128)
    if rs:
        open(os.path.join(OUT, "raw_state.bin"), "wb").write(rs)
except Exception as exc:      # noqa: BLE001
    print(f"  raw_state error: {exc}")

print("\n=== after a preset save (does state carry the VirtualData?) ===")
# Ask the plugin to save its state the way a host would.
for meth in ("save_preset", "get_state"):
    if hasattr(pl, meth):
        print(f"  has {meth}")
print("  plugin methods:", [m for m in dir(pl) if not m.startswith("_")][:40])

print(f"\nwrote probes under {OUT}")
