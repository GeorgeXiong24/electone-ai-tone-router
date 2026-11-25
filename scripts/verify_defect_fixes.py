"""Verify every defect the user reported is actually fixed (evidence, not claims)."""
import base64
import json
import os
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cantabile_format as cf
import tone_common as tc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WS = os.path.join(ROOT, "workspace")
SONG = "韦礼安 - 女孩"
SLUG = "weilian-nvhai-1c28b2b9"

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


print("=" * 72)
print("DEFECT 1: exported song must reference the NEW sfizz instruments")
print("=" * 72)
path = os.path.join(WS, "cantabile", SONG + ".cantabileSong")
d = json.load(open(path, encoding="utf-8"))
sfizz_ids = []
for o in d.get("objects", []):
    if o.get("kind") != "Plugin":
        continue
    cls, st = cf.decode_plugin_data(o["pluginData"])
    if isinstance(st, bytes) and st[:4] != b"VST3":
        cls, st = cf.parse_abmp(zlib.decompress(st, -15))
    sfizz_ids.append((o.get("name", ""), cls))
sfizz_count = sum(1 for _n, c in sfizz_ids if c == cf.SFIZZ_CLASS_ID)
sfizz_real = sum(1 for _n, c in sfizz_ids if c.upper() == b"E8FAB71815ED46E38B5983101E12993F")
check("three keyboards use sfizz", sfizz_count >= 3, f"{sfizz_count} sfizz plugins")
check("sfizz uses the REAL class id", sfizz_real == sfizz_count,
      f"{sfizz_real}/{sfizz_count} carry E8FAB718...")
check("no invented class id c9bd8ea0", all(
    c != b"c9bd8ea0e2f24ea8b9b1e2b1c04fdcd8" for _n, c in sfizz_ids))

print()
print("=" * 72)
print("DEFECT 2: pedal range must reach C1 (MIDI 24) and never be silent")
print("=" * 72)
check("PEDAL_RANGE starts at C1 (24)", tc.PEDAL_RANGE[0] == 24,
      f"{tc.PEDAL_RANGE}")
check("pedal render covers 24..60", tc.PEDAL_RENDER_RANGE == (24, 60),
      f"{tc.PEDAL_RENDER_RANGE}")
pedal_sfz = os.path.join(WS, "tone", SLUG, "pedal", "sfizz", f"{SLUG}.sfz")
regions = [ln.strip() for ln in open(pedal_sfz, encoding="utf-8")
           if "<region>" in ln]
los = [int(r.split("lokey=")[1].split()[0]) for r in regions]
his = [int(r.split("hikey=")[1].split()[0]) for r in regions]
check("pedal samples exist below MIDI 36", min(los) <= 24, f"lokey min={min(los)}")
check("pedal regions cover full MIDI span", min(los) == 0 and max(his) == 127,
      f"lokey {min(los)}, hikey {max(his)}")
for kb in ("right", "left"):
    p = os.path.join(WS, "tone", SLUG, kb, "sfizz", f"{SLUG}.sfz")
    rs = [ln for ln in open(p, encoding="utf-8") if "<region>" in ln]
    los2 = [int(r.split("lokey=")[1].split()[0]) for r in rs]
    his2 = [int(r.split("hikey=")[1].split()[0]) for r in rs]
    check(f"{kb} regions cover full MIDI span",
          min(los2) == 0 and max(his2) == 127,
          f"lokey {min(los2)}, hikey {max(his2)}")

print()
print("=" * 72)
print("DEFECT 3b: keyboards must not all be piano (use bass/guitar stems)")
print("=" * 72)
man = json.load(open(os.path.join(WS, "tone", "manifest.json"), encoding="utf-8"))
files = man[SONG]["files"]
sfizz_entries = [f for f in files if f.get("plugin") == "sfizz"]
labels = {f["keyboard"]: f["instrument"] for f in sfizz_entries}
check("pedal uses the bass stem", labels.get("脚") == "贝斯",
      f"labels={labels}")
check("upper uses piano (Stage-3 prior)", labels.get("右手") == "钢琴")
check("manifest lists sfizz entries", len(sfizz_entries) == 3,
      f"{len(sfizz_entries)} entries")

print()
print("=" * 72)
print("DEFECT 3c: Vital class id + state must be real")
print("=" * 72)
check("Vital class id is VSTV+itavital",
      cf.VITAL_CLASS_ID == b"56535456697461766974616C00000000",
      cf.VITAL_CLASS_ID)
vref = os.path.join(WS, "_plugin_refs", "vital_preset_data.bin")
if os.path.isfile(vref):
    blob = open(vref, "rb").read()
    _c, comp = cf.parse_vst3_state(blob)
    js = cf.parse_vital_component_state(comp)
    check("Vital state wraps real JSON", bool(js) and '"author"' in js,
          f"{len(comp)} bytes")
    check("Vital state starts with VstW", comp[:4] == b"VstW", comp[:4])
sref = os.path.join(WS, "_plugin_refs", "sfizz_preset_data.bin")
if os.path.isfile(sref):
    blob = open(sref, "rb").read()
    _c, comp = cf.parse_vst3_state(blob)
    check("sfizz state is the measured 96-byte block", len(comp) == 96,
          f"{len(comp)} bytes")

print()
print("=" * 72)
print("DEFECT 3d: DSH plugin compiled into lib/")
print("=" * 72)
lib = os.path.join(ROOT, "electone-tone-studio", "lib")
check("lib/index.js exists", os.path.isfile(os.path.join(lib, "index.js")))
check("lib/python-bridge.js exists",
      os.path.isfile(os.path.join(lib, "python-bridge.js")))
check("package.json exports point at lib", json.load(
    open(os.path.join(ROOT, "electone-tone-studio", "package.json"),
         encoding="utf-8"))["exports"]["."] == "./lib/index.js")

print()
print("=" * 72)
n_pass = sum(1 for _n, ok, _d in results if ok)
print(f"RESULT: {n_pass}/{len(results)} checks pass")
print("=" * 72)
sys.exit(0 if n_pass == len(results) else 1)
