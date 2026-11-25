"""Find real sfizz / Vital plugin states inside any Cantabile song (scheme A).

Scheme A needs a song Cantabile itself wrote with the plugin loaded. This scans
every candidate song file and reports which plugins it contains, so the real
class id and component-state format can be read instead of invented.
"""
import base64
import json
import os
import re
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cantabile_format as cf

CANDIDATES = [
    os.path.join(os.path.expanduser("~"), "Documents", "Cantabile",
                 "song1.cantabileSong"),
]
# Also any song under the workspace.
WS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "workspace")
for root, _d, files in os.walk(WS):
    for f in files:
        if f.endswith(".cantabileSong"):
            CANDIDATES.append(os.path.join(root, f))

KNOWN_IDS = {
    cf.SURGE_CLASS_ID: "surge-xt",
    cf.SFORZANDO_CLASS_ID: "sforzando",
    cf.SFIZZ_CLASS_ID: "sfizz(claimed)",
    cf.VITAL_CLASS_ID: "Vital(claimed)",
}


def scan(path):
    print(f"\n=== {path}  ({os.path.getsize(path)} bytes) ===")
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    objs = d.get("objects", [])
    plugins = [o for o in objs if o.get("kind") == "Plugin"]
    print(f"  {len(objs)} objects, {len(plugins)} plugins")
    for o in plugins:
        name = o.get("name", "")
        pid = o.get("pluginId", "")
        ppath = o.get("pluginPath", "")
        print(f"\n  plugin {name[:40]!r}")
        print(f"    pluginId  : {pid}")
        print(f"    pluginPath: {ppath[:100]}")
        blob_b64 = o.get("pluginData")
        if not blob_b64:
            print("    no pluginData")
            continue
        raw = base64.b64decode(blob_b64)
        print(f"    pluginData: {len(raw)} bytes head={raw[:8]!r}")
        # Cantabile stores raw-deflate(aBMP)
        try:
            abmp = zlib.decompress(raw, -15)
        except Exception as exc:
            print(f"    not raw-deflate: {exc}")
            continue
        print(f"    aBMP-ish: {len(abmp)} bytes head={abmp[:24]!r}")
        if abmp[:4] == b"aBMP":
            try:
                class_id, state = cf.parse_abmp(abmp)
                print(f"    class_id  : {class_id!r}")
                print(f"      known as: {KNOWN_IDS.get(class_id, 'UNKNOWN')}")
                print(f"    state     : {len(state)} bytes head={state[:48]!r}")
                # Show printable strings in the state (SFZ / JSON paths).
                strs = re.findall(rb"[\x20-\x7e]{6,}", state)
                for s in strs[:6]:
                    print(f"      str: {s[:110]!r}")
            except Exception as exc:
                print(f"    parse_abmp failed: {exc}")


for p in dict.fromkeys(CANDIDATES):
    if os.path.isfile(p):
        try:
            scan(p)
        except Exception as exc:      # noqa: BLE001
            print(f"\n=== {p} ===\n  error: {exc}")
