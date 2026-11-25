"""Extract sfizz's REAL host-side state from the Cantabile reference song (scheme A).

The user saved `Documents\\Cantabile\\sfizz_ref.cantabileSong` with sfizz loaded
and an .sfz selected. Unlike the plugin's own `preset_data` (measured: a 96-byte
binary settings block with no strings), a host-saved state is what Cantabile
actually writes back - so this is the bytes that, when restored, must make sfizz
play our instrument.
"""
import base64
import json
import os
import re
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cantabile_format as cf

REF = os.path.join(os.path.expanduser("~"), "Documents", "Cantabile",
                   "sfizz_ref.cantabileSong")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "workspace", "_plugin_refs")
os.makedirs(OUT, exist_ok=True)

print(f"reference: {REF}  ({os.path.getsize(REF)} bytes)")
d = json.load(open(REF, encoding="utf-8"))
print(f"objects: {len(d.get('objects', []))}")

for o in d.get("objects", []):
    if o.get("kind") != "Plugin":
        continue
    print("\n" + "=" * 70)
    print(f"plugin {o.get('name')!r}")
    print("=" * 70)
    print(f"  pluginId  : {o.get('pluginId')}")
    print(f"  pluginPath: {o.get('pluginPath')}")
    b64 = o.get("pluginData") or ""
    raw = base64.b64decode(b64)
    print(f"  pluginData: {len(raw)} bytes  head={raw[:8]!r}")
    try:
        abmp = zlib.decompress(raw, -15)
    except Exception as exc:
        print(f"  not raw-deflate: {exc}")
        continue
    print(f"  aBMP      : {len(abmp)} bytes head={abmp[:28]!r}")
    cls, state = cf.parse_abmp(abmp)
    print(f"  class id  : {cls!r}")
    print(f"  known as  : {'sfizz REAL' if cls == cf.SFIZZ_CLASS_ID else 'OTHER'}")
    _cls, comp = cf.parse_vst3_state(state)
    print(f"  comp state: {len(comp)} bytes")
    print(f"    head    : {comp[:80]!r}")
    # Every printable string in the whole blob - an .sfz path must be in here.
    strs = [s for s in re.findall(rb"[\x20-\x7e]{5,}", comp)]
    print(f"  strings ({len(strs)}):")
    for s in strs[:20]:
        print(f"    {s[:120]!r}")
    # Save for the serializer.
    tag = "sfizz_host_state.bin"
    open(os.path.join(OUT, tag), "wb").write(comp)
    open(os.path.join(OUT, "sfizz_host_preset_data.bin"), "wb").write(raw)
    print(f"\n  wrote {tag} ({len(comp)} bytes)")
    print(f"  wrote sfizz_host_preset_data.bin ({len(raw)} bytes + deflate)")
