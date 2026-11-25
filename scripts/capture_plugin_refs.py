"""Capture REAL reference states for sfizz and Vital (scheme A, plugin-driven).

Everything here is read from the live plugins via pedalboard, so no format is
invented. Outputs, under `workspace/_plugin_refs/`:

  vital_preset_data.bin   exact `preset_data` bytes Vital emits (a VST3 state)
  vital_raw_state.bin     full host state (for reference only)
  sfizz_preset_data.bin   what sfizz accepts/emits
  report.txt              human-readable findings

The critical finding already confirmed: Vital's class id is
`56535456697461766974616C00000000` and its state is
`VstW` / `CcnK` / `FBCh` / `Vita` + JUCEPrivateData.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _cache_env  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "workspace", "_plugin_refs")
os.makedirs(OUT, exist_ok=True)
REPORT = []


def log(line=""):
    print(line)
    REPORT.append(line)


SFIZZ = (r"C:\Program Files\Common Files\VST3\sfizz.vst3"
         r"\Contents\x86_64-win\sfizz.vst3")
VITAL = r"C:\Program Files\Common Files\VST3\Vital.vst3"


def strings(data, limit=10, min_len=6):
    return [s for s in re.findall(rb"[\x20-\x7e]{%d,}" % min_len, data)][:limit]


from pedalboard import load_plugin    # noqa: E402

log("=" * 70)
log("VITAL")
log("=" * 70)
pl = load_plugin(VITAL)
log(f"identifier = {getattr(pl, 'identifier', None)!r}")
pd = pl.preset_data
assert isinstance(pd, (bytes, bytearray)), f"preset_data type {type(pd)}"
open(os.path.join(OUT, "vital_preset_data.bin"), "wb").write(bytes(pd))
log(f"preset_data = {len(pd)} bytes")
log(f"  head = {bytes(pd)[:64]!r}")
log(f"  class id inside state: {bytes(pd)[8:40]!r}")
for s in strings(bytes(pd)[:2048]):
    log(f"  str: {s[:100]!r}")
try:
    rs = pl.raw_state
    open(os.path.join(OUT, "vital_raw_state.bin"), "wb").write(rs)
    log(f"raw_state = {len(rs)} bytes, head={rs[:32]!r}")
except Exception as exc:      # noqa: BLE001
    log(f"raw_state unavailable: {exc}")

# The JSON inside preset_data is the .vital preset itself; extract it so a real
# template can be built from measured schema rather than invented keys.
m = re.search(rb'\{"author".*\}', bytes(pd), re.S)
if m:
    json_bytes = m.group(0)
    p = os.path.join(OUT, "vital_inner.json")
    open(p, "wb").write(json_bytes)
    log(f"inner JSON extracted: {len(json_bytes)} bytes -> {p}")
    import json
    try:
        d = json.loads(json_bytes.decode("utf-8", "replace"))
        log(f"  top-level keys: {sorted(d.keys())}")
        for k in ("settings", "oscillator", "filter", "env", "sample"):
            if k in d:
                log(f"  {k}: {type(d[k]).__name__} "
                    f"{list(d[k].keys())[:12] if isinstance(d[k], dict) else len(d[k])}")
    except Exception as exc:      # noqa: BLE001
        log(f"  JSON parse failed: {exc}")

log("")
log("=" * 70)
log("SFIZZ")
log("=" * 70)
sp = load_plugin(SFIZZ, plugin_name="sfizz")
log(f"identifier = {getattr(sp, 'identifier', None)!r}")
log(f"name = {sp.name!r}  manuf = {sp.manufacturer_name!r}  ver = {sp.version!r}")
for attr in ("preset_data", "raw_state"):
    try:
        data = getattr(sp, attr)
        if isinstance(data, (bytes, bytearray)) and data:
            open(os.path.join(OUT, f"sfizz_{attr}.bin"), "wb").write(bytes(data))
            log(f"{attr} = {len(data)} bytes head={bytes(data)[:48]!r}")
            for s in strings(bytes(data)[:1024]):
                log(f"  str: {s[:100]!r}")
        else:
            log(f"{attr} = {type(data)} {data!r}")
    except Exception as exc:      # noqa: BLE001
        log(f"{attr} error: {exc}")

with open(os.path.join(OUT, "report.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(REPORT))
log(f"\nwrote {OUT}")
