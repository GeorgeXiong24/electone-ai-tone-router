"""Audit the reported defects before fixing them (evidence first, then repair)."""
import json
import base64
import os
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cantabile_format as cf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WS = os.path.join(ROOT, "workspace")
SONG = "韦礼安 - 女孩"

print("=" * 70)
print("CLAIM 1: the exported song references OLD sforzando slices")
print("=" * 70)
song_path = os.path.join(WS, "cantabile", SONG + ".cantabileSong")
d = json.load(open(song_path, encoding="utf-8"))
for o in d.get("objects", []):
    if o.get("kind") != "Plugin":
        continue
    name = o.get("name", "")
    cls, st = cf.decode_plugin_data(o["pluginData"])
    if isinstance(st, bytes) and st[:4] != b"VST3":
        cls, st = cf.parse_abmp(zlib.decompress(st, -15))
    _cls2, comp = cf.parse_vst3_state(st)
    try:
        if cls == cf.SFORZANDO_CLASS_ID:
            ref = cf.parse_sforzando_component_state(comp)[0]
            plugin = "sforzando"
        elif cls == cf.SFIZZ_CLASS_ID:
            ref = cf.parse_sfizz_component_state(comp)
            plugin = "sfizz"
        else:
            ref = f"<unknown {cls!r}>"
            plugin = "?"
    except Exception as exc:
        ref = f"<parse error {exc}>"
        plugin = "?"
    print(f"  {name[:34]:36s} {plugin:10s} -> {ref}")
    for part in ("/sforzando/", "/sfizz/"):
        if part in ref.replace("\\", "/"):
            print(f"      ^ lives under {part}")

print()
print("=" * 70)
print("CLAIM 2: pedal range is hardcoded to 36..55, ELB-02 pedal is 24..43")
print("=" * 70)
import tone_common as tc
print(f"  tone_common.PEDAL_RANGE = {tc.PEDAL_RANGE}")
pedal_sfz = os.path.join(WS, "tone", "weilian-nvhai-1c28b2b9", "pedal",
                         "sfizz", "weilian-nvhai-1c28b2b9.sfz")
if os.path.isfile(pedal_sfz):
    regs = [ln for ln in open(pedal_sfz, encoding="utf-8") if "<region>" in ln]
    print(f"  new sfizz pedal sfz has {len(regs)} regions; first/last:")
    print(f"    {regs[0].strip()[:80]}")
    print(f"    {regs[-1].strip()[:80]}")
old_pedal = os.path.join(WS, "tone", "weilian-nvhai-1c28b2b9", "pedal",
                         "sforzando", "tone-c9d0138c.sfz")
if os.path.isfile(old_pedal):
    print(f"  OLD sforzando pedal sfz exists: {os.path.basename(old_pedal)}")

print()
print("=" * 70)
print("CLAIM 3a: sample-instruments.json / manifest.json still list OLD tones")
print("=" * 70)
for fn in ("manifest.json", "sample-instruments.json"):
    p = os.path.join(WS, "tone", fn)
    if not os.path.isfile(p):
        print(f"  {fn}: missing")
        continue
    data = json.load(open(p, encoding="utf-8"))
    entry = data.get(SONG) or {}
    files = entry.get("files", [])
    print(f"  {fn}: {len(files)} tone file(s) for this song")
    for f in files[:6]:
        print(f"    {f.get('keyboard','?'):4s} {f.get('plugin','?'):10s} "
              f"{f.get('instrument','?'):8s} {f.get('file','')}")

print()
print("=" * 70)
print("CLAIM 3b: all three keyboards rendered as piano?")
print("=" * 70)
import build_song_tone as bst
import inspect
src = inspect.getsource(bst.build_song_instruments)
print("  build_song_instruments specs:")
for line in src.splitlines():
    if "右手" in line or "左手" in line or "脚" in line:
        print(f"    {line.strip()}")

print()
print("=" * 70)
print("CLAIM 3c: sfizz / Vital class ids are invented")
print("=" * 70)
print(f"  cantabile_format.SFIZZ_CLASS_ID = {cf.SFIZZ_CLASS_ID!r}")
print(f"  cantabile_format.VITAL_CLASS_ID = {cf.VITAL_CLASS_ID!r}")
for p in (r"C:\Program Files\Common Files\VST3\sfizz.vst3\Contents\moduleinfo.json",
          r"C:\Program Files\Common Files\VST3\Vital.vst3\Contents\moduleinfo.json"):
    print(f"  {os.path.basename(os.path.dirname(os.path.dirname(p)))} "
          f"moduleinfo.json exists: {os.path.isfile(p)}")

print()
print("=" * 70)
print("CLAIM 3d: electone-tone-studio/lib missing")
print("=" * 70)
pkg = os.path.join(ROOT, "electone-tone-studio", "package.json")
p = json.load(open(pkg, encoding="utf-8"))
print(f"  package.json main/exports -> {p.get('exports', {}).get('.')}")
print(f"  lib/ exists: {os.path.isdir(os.path.join(ROOT, 'electone-tone-studio', 'lib'))}")
