import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
SONG = "韦礼安 - 女孩"


def run(script, *args):
    cmd = [PY, os.path.join(ROOT, "scripts", script), *args]
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


print("=" * 62)
print("FINAL ACCEPTANCE — all phases")
print("=" * 62)

# Phase 1: storage budget
bpath = os.path.join(ROOT, "workspace", "_final_budget.json")
run("disk_budget.py", "--song", SONG, "--soft", "--json", bpath)
b = json.load(open(bpath, encoding="utf-8"))
print(f"\n[Phase 1] storage")
print(f"  total   : {b['total_mb']:>7.1f} MB   (budget {b['budget_mb']:.0f} MB)")
print(f"  level   : {b['level']}   ok={b['ok']}")
for k, v in b["per_stage_mb"].items():
    print(f"    {k:10s} {v:>7.1f} MB")

# Phase 4: evaluation
epath = os.path.join(ROOT, "workspace", "_final_eval.json")
run("eval_tones.py", "--song", SONG, "--json", epath)
e = json.load(open(epath, encoding="utf-8"))
print(f"\n[Phase 4] objective evaluation")
for kb, v in e["keyboards"].items():
    print(f"  {kb:4s}  {v['passed']}/{v['total']} checks pass")
    for name, c in v["checks"].items():
        mark = "PASS" if c.get("pass") else "FAIL"
        if name == "pitch":
            detail = f"{c.get('pitch_pass_fraction', 0) * 100:.0f}% within 25c"
        elif name == "coherence":
            detail = f"median jump {c.get('median_jump')}"
        elif name == "spectrum":
            detail = f"{c.get('octave_band_err_db')} dB"
        elif name == "chord":
            detail = ("n/a" if not c.get("applicable")
                      else f"peak {c.get('peak')}")
        else:
            detail = f"corr {c.get('min_correlation')}"
        print(f"        [{mark}] {name:10s} {detail}")
print(f"  ALL_PASS = {e['all_pass']}")
print(f"  disk_ok  = {e['disk_ok']}")

# Phase 5: plugin selftest
print(f"\n[Phase 5] DSH plugin selftest")
st = subprocess.run(["node", os.path.join(ROOT, "electone-tone-studio", "test",
                                          "selftest.mjs")],
                     cwd=ROOT, capture_output=True, text=True,
                     encoding="utf-8", errors="replace")
last = [ln for ln in st.stdout.strip().splitlines() if "selftest" in ln]
print(f"  {last[-1] if last else 'no output'}")

# Phase 3 + 6: cantabile export
print(f"\n[Phase 3/6] Cantabile export")
cpath = os.path.join(ROOT, "workspace", "_final_cant.json")
r = run("build-cantabile.py", "--song", SONG, "--verify")
try:
    c = json.loads(r.stdout[r.stdout.index("{"):])
except Exception:
    c = {}
print(f"  ok       : {c.get('ok')}")
print(f"  plugins  : {c.get('plugins')}")
print(f"  problems : {c.get('problems')}")
print(f"  path     : {os.path.basename(c.get('path', '')).encode('ascii', 'replace').decode()}")

print("\n" + "=" * 62)
ok = (b["ok"] and e["all_pass"] and c.get("ok")
      and any("selftest PASS" in ln for ln in st.stdout.splitlines()))
print("RESULT:", "ALL PHASES PASS" if ok else "SOME CHECKS FAILED")
print("=" * 62)
sys.exit(0 if ok else 1)
