"""Generate single-instrument tone files from analyze-tones.py output.

For each analyzed stem a tone file is written per assigned keyboard:

  workspace/tone/<右手|左手|脚>/<plugin>/<song> - <instrument>(<role>).<ext>

Sound sources:
  piano  -> sforzando SFZ wrapping the Accurate-Salamander grand piano. Only
            the .sfz config lives in the project; the samples stay in
            <SAMPLE_LIBRARIES> and are referenced by absolute path.
  others -> Surge XT patches derived from a factory (or local) single-instrument
            preset, with the patch <meta> rewritten to describe the analyzed
            stem. The oscillator/filter design of the source preset is kept.

Surge tones are written as `.fxp` — Surge XT's native patch format, loadable by
dragging it onto the Surge XT window. With `--vstpreset`, a matching VST3 preset
is written too for hosts (Cantabile) that load presets through their own
mechanism.

Hard rules:
  - one file always contains exactly ONE instrument (no layering, no fusion)
  - nothing is ever written into a plugin's default/user patch folder; every
    output path must sit inside the project workspace
  - drums get no tone file (rhythm is handled by the ELB-02 itself)

Usage:
  python build-tones.py --song <title substring>   # uses workspace/analysis/<song>
  python build-tones.py --latest
  python build-tones.py --analysis <analysis.json>
  python build-tones.py --latest --dry-run
  python build-tones.py --latest --vstpreset
"""
import argparse
import glob
import json
import os
import re
import shutil
import struct
import sys
import datetime
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.environ.get("ELECTONE_WORKSPACE") or os.path.join(ROOT, "workspace")
TONE_ROOT = os.path.join(WORKSPACE, "tone")

FACTORY = os.environ.get("SURGE_FACTORY") or r"C:\ProgramData\Surge XT\patches_factory"
LOCAL_PATCHES = os.path.join(os.path.expanduser("~"), "Documents", "Surge XT", "Patches")
SAMPLE_LIBRARIES = os.environ.get("SAMPLE_LIBRARIES") or r"D:\SampleLibraries"

# sfz_daw is the full-quality variant (sfz_live trades velocity layers for
# lower CPU); tone quality is the priority here.
PIANO_SFZ = os.path.join(
    SAMPLE_LIBRARIES, "AccurateSalamanderGrandPianoV6.2e_48khz24bit",
    "sfz_daw", "Accurate-SalamanderGrandPiano_flat.Recommended.sfz")

# instrument label (from analysis) -> tone template
#   kind "surge": copy an .fxp and rewrite its meta
#   kind "sfz":   build an .sfz config referencing the sample library
#   name28:       ASCII name shown in the VST program-name field
#   zh/en:        display names
TEMPLATES = {
    "人声": {
        "kind": "surge", "plugin": "surge-xt", "name28": "Vocal Lead",
        "source": ["Leads/Vocal Lead.fxp"],
    },
    "吉他": {
        "kind": "surge", "plugin": "surge-xt", "name28": "Nylon Guitar",
        "source": [],  # resolved below: local Electone patch first
        "local_source": "Electone Classical Guitar.fxp",
        "fallback": ["Plucks/Guitar.fxp"],
    },
    "钢琴": {
        "kind": "sfz", "plugin": "sforzando", "name28": "Grand Piano",
    },
    "贝斯": {
        "kind": "surge", "plugin": "surge-xt", "name28": "Fingered Bass",
        "source": ["Basses/Fingered.fxp"],
    },
    "其他/混合": {
        "kind": "surge", "plugin": "surge-xt", "name28": "String Ensemble",
        "source": ["Pads/Subtle Comb Strings.fxp"],
    },
}

KEYBOARD_CODE = {"右手": "RH", "左手": "LH", "脚": "FT"}

# Surge XT's VST3 class id (as recorded in a real .vstpreset written by the
# installed plugin) and the private-data marker Surge appends to its component
# state. Both are verified byte-for-byte against a reference file produced by
# the actual Surge XT VST3 instance.
SURGE_CLASS_ID = b"ABCDEF019182FAEB566D624153675854"
SURGE_STATE_SUFFIX = b"\x00" * 16 + b"JUCEPrivateData"


# ---------------------------------------------------------------- fxp io

def read_fxp(path):
    """Return (name28, xml_bytes, extra_bytes) from a Surge .fxp."""
    b = open(path, "rb").read()
    if b[:4] != b"CcnK" or b[8:12] != b"FPCh":
        raise ValueError(f"not a Surge fxp: {path}")
    name = b[28:56]
    chunk_size = struct.unpack(">i", b[56:60])[0]
    chunk = b[60:60 + chunk_size]
    if chunk[:4] != b"sub3":
        raise ValueError(f"unexpected fxp chunk tag: {path}")
    xml_len, extra_len = struct.unpack("<ii", chunk[4:12])
    xml = chunk[32:32 + xml_len]
    extra = chunk[32 + xml_len:32 + xml_len + extra_len]
    return name, xml, extra


def write_fxp(path, name28, xml, extra):
    """Rebuild a Surge .fxp around the given chunk payload."""
    name = name28.encode("ascii", "replace")[:28].ljust(28, b"\x00")
    chunk = (b"sub3" + struct.pack("<ii", len(xml), len(extra))
             + b"\x00" * 20 + xml + extra)
    head = (b"CcnK" + struct.pack(">i", 0) + b"FPCh" + struct.pack(">i", 1)
            + b"cjs3" + struct.pack(">ii", 1, 1) + name
            + struct.pack(">i", len(chunk)))
    with open(path, "wb") as f:
        f.write(head + chunk)


META_RE = re.compile(rb"<meta[^>]*(?:/>|>.*?</meta>)", re.S)


# ---------------------------------------------------------------- vstpreset io

def make_component_state(xml, extra):
    """Surge's VST3 component state: the .fxp chunk plus its private marker."""
    chunk = (b"sub3" + struct.pack("<ii", len(xml), len(extra))
             + b"\x00" * 20 + xml + extra)
    return chunk + SURGE_STATE_SUFFIX


def build_vstpreset(component_state):
    """Assemble a standard VST3 preset file (Vst::PresetFile layout).

    Header: 'VST3' + int32 version + 32-char class id + uint64 list offset.
    Body:   component state, then the chunk list ('List', count, entries of
            4-char id + uint64 offset + uint64 size) — as written by the
            Steinberg SDK and verified against a real Surge XT .vstpreset.
    """
    list_offset = 48 + len(component_state)
    chunk_list = (b"List" + struct.pack("<i", 2)
                  + b"Comp" + struct.pack("<QQ", 48, len(component_state))
                  + b"Cont" + struct.pack("<QQ", list_offset, 0))
    header = (b"VST3" + struct.pack("<i", 1) + SURGE_CLASS_ID
              + struct.pack("<Q", list_offset))
    return header + component_state + chunk_list


def check_vstpreset(path):
    """Parse a .vstpreset back and return its component state bytes."""
    b = open(path, "rb").read()
    if b[:4] != b"VST3" or struct.unpack("<i", b[4:8])[0] != 1:
        raise ValueError(f"bad vstpreset header: {path}")
    if b[8:40] != SURGE_CLASS_ID:
        raise ValueError(f"class id mismatch: {path}")
    list_offset = struct.unpack("<Q", b[40:48])[0]
    if b[list_offset:list_offset + 4] != b"List":
        raise ValueError(f"bad chunk list: {path}")
    count = struct.unpack("<i", b[list_offset + 4:list_offset + 8])[0]
    off = list_offset + 8
    entries = []
    for _ in range(count):
        eid = b[off:off + 4]
        eoff, esize = struct.unpack("<QQ", b[off + 4:off + 20])
        entries.append((eid, eoff, esize))
        off += 20
    comp = next(e for e in entries if e[0] == b"Comp")
    state = b[comp[1]:comp[1] + comp[2]]
    if not state.startswith(b"sub3"):
        raise ValueError(f"component state is not a Surge chunk: {path}")
    if off != len(b):
        raise ValueError(f"chunk list overruns file: {path}")
    return state


def xml_escape(v):
    return (v.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))





def rewrite_meta(xml, name, category, comment):
    meta = (f'<meta name="{xml_escape(name)}" category="{xml_escape(category)}"'
            f' comment="{xml_escape(comment)}" author="electone-ai-tone-router"'
            f' license="see source preset"><tags /></meta>').encode("utf-8")
    out, n = META_RE.subn(lambda m: meta, xml, count=1)
    if n != 1:
        raise ValueError("no <meta> element found in preset XML")
    return out


# ---------------------------------------------------------------- sfz io

SAMPLE_RE = re.compile(r"sample=(\S+)")


def build_sfz(template, out_path, header_lines):
    """Copy an SFZ config, rewriting sample= paths to absolute ones."""
    src_dir = os.path.dirname(os.path.abspath(template))
    text = open(template, encoding="utf-8", errors="replace").read()

    def fix(m):
        val = m.group(1)
        if os.path.isabs(val) or re.match(r"^[A-Za-z]:", val):
            return m.group(0)
        return "sample=" + os.path.normpath(os.path.join(src_dir, val))

    text = SAMPLE_RE.sub(fix, text)
    header = "".join(f"// {line}\n" for line in header_lines)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + "\n" + text)
    return [m.group(1) for m in SAMPLE_RE.finditer(text)]


# ---------------------------------------------------------------- naming

def resolve_template(instrument):
    t = TEMPLATES.get(instrument)
    if not t:
        return None
    if t["kind"] == "sfz":
        return dict(t, resolved=PIANO_SFZ)
    for rel in t.get("source", []):
        p = os.path.join(FACTORY, rel)
        if os.path.isfile(p):
            return dict(t, resolved=p, source_label=f"Surge factory {rel}")
    local = t.get("local_source")
    if local:
        p = os.path.join(LOCAL_PATCHES, local)
        if os.path.isfile(p):
            return dict(t, resolved=p,
                        source_label=f"local {os.path.join('Documents', 'Surge XT', 'Patches', local)}")
    for rel in t.get("fallback", []):
        p = os.path.join(FACTORY, rel)
        if os.path.isfile(p):
            return dict(t, resolved=p, source_label=f"Surge factory {rel}")
    return None


def safe_name(v):
    """Strip characters that are invalid in Windows filenames."""
    return re.sub(r'[\\/:*?"<>|]', "-", v).strip()


def role_for_keyboard(stem, keyboard):
    """Most relevant role of this stem that maps to the given keyboard."""
    rule = {"右手": ["主旋律"], "左手": ["副旋律", "和弦", "伴奏", "和声铺底"],
            "脚": ["低音线条"]}
    roles = stem.get("roles", {})
    best, best_share = None, -1.0
    for role, share in roles.items():
        if role in rule.get(keyboard, []) and share > best_share:
            best, best_share = role, share
    return best or stem.get("primary_role", "")


# ---------------------------------------------------------------- build

def build_song(song, stems, dry_run=False, vstpreset=False):
    entries = []
    for stem_name, stem in stems.items():
        if stem.get("empty") or stem_name == "drums":
            continue
        instrument = stem_name if stem_name in TEMPLATES else None
        # analysis labels stems in Chinese in the report; the JSON keeps the
        # wav basename, so map through the known set
        label = {"vocals": "人声", "guitar": "吉他", "piano": "钢琴",
                 "bass": "贝斯", "other": "其他/混合"}.get(stem_name)
        if label is None:
            print(f"  skip {stem_name}: no tone template")
            continue
        tpl = resolve_template(label)
        if tpl is None:
            print(f"  skip {label}: template not found")
            continue

        for keyboard in stem.get("keyboards", []):
            role = role_for_keyboard(stem, keyboard)
            plugin_dir = os.path.join(TONE_ROOT, keyboard, tpl["plugin"])
            ext = "sfz" if tpl["kind"] == "sfz" else "fxp"
            fname = safe_name(f"{song} - {label}({role}).{ext}")
            out_path = os.path.join(plugin_dir, fname)

            # hard rule: outputs must stay inside the project workspace
            if not os.path.abspath(out_path).startswith(
                    os.path.abspath(TONE_ROOT) + os.sep):
                raise RuntimeError(f"refusing to write outside tone dir: {out_path}")

            comment = (f"曲目 {song} | 音色 {label} | 角色 {role} | "
                       f"键盘 {keyboard} | "
                       f"特点：亮度 {stem.get('brightness_hz', 0):.0f}Hz，"
                       f"音区 {stem.get('register_midi', 0)}MIDI，"
                       f"谐波占比 {stem.get('harmonic_ratio', 0):.2f} | "
                       f"源：{tpl.get('source_label', 'sample library')} | "
                       f"单乐器音色，由 analyze-tones.py 分析生成")
            print(f"  {keyboard}/{tpl['plugin']}/{fname}")

            if dry_run:
                entries.append({"file": os.path.relpath(out_path, TONE_ROOT),
                                "keyboard": keyboard, "plugin": tpl["plugin"],
                                "instrument": label, "role": role, "dry_run": True})
                continue

            os.makedirs(plugin_dir, exist_ok=True)
            if tpl["kind"] == "sfz":
                samples = build_sfz(
                    tpl["resolved"], out_path,
                    [f"{song} - {label} ({role}) / {keyboard}",
                     "Single-instrument tone file generated by scripts/build-tones.py.",
                     f"Samples: {os.path.dirname(PIANO_SFZ)} (Accurate-Salamander, CC-BY).",
                     "Sample paths below are absolute; edit if the library moves."])
                missing = [s for s in samples if not os.path.isfile(s)]
                if missing:
                    raise RuntimeError(f"{len(missing)} sample paths missing, "
                                       f"e.g. {missing[0]}")
                entries.append({
                    "file": os.path.relpath(out_path, TONE_ROOT),
                    "keyboard": keyboard, "plugin": tpl["plugin"],
                    "instrument": label, "role": role,
                    "source": tpl["resolved"], "samples": len(samples),
                })
            else:
                _, xml, extra = read_fxp(tpl["resolved"])
                meta_name = f"{song} · {label}（{role}·{keyboard}）"
                xml = rewrite_meta(xml, meta_name, keyboard, comment)
                write_fxp(out_path, tpl["name28"], xml, extra)
                # verify the .fxp round-trip
                chk_name, chk_xml, chk_extra = read_fxp(out_path)
                ET.fromstring(chk_xml.decode("utf-8"))

                # VST3 preset for host-level loading (Cantabile etc.)
                vst_path = None
                if vstpreset:
                    vst_path = os.path.splitext(out_path)[0] + ".vstpreset"
                    state = make_component_state(xml, extra)
                    with open(vst_path, "wb") as f:
                        f.write(build_vstpreset(state))
                    # verify the .vstpreset parses and carries our patch
                    chk_state = check_vstpreset(vst_path)
                    if chk_state != state:
                        raise RuntimeError(f"vstpreset round-trip mismatch: {vst_path}")

                entry = {
                    "file": os.path.relpath(out_path, TONE_ROOT),
                    "keyboard": keyboard, "plugin": tpl["plugin"],
                    "instrument": label, "role": role,
                    "source": tpl["resolved"],
                    "bytes": os.path.getsize(out_path),
                }
                if vst_path:
                    entry["vstpreset"] = os.path.relpath(vst_path, TONE_ROOT)
                entries.append(entry)
    return entries


# ---------------------------------------------------------------- main

def pick_analysis(args):
    if args.analysis:
        return args.analysis
    root = os.path.join(WORKSPACE, "analysis")
    cands = [p for p in glob.glob(os.path.join(root, "*", "analysis.json"))]
    if args.song:
        cands = [p for p in cands if args.song in os.path.basename(os.path.dirname(p))]
    if not cands:
        sys.exit(f"no analysis.json found under {root} - run analyze-tones.py first")
    return max(cands, key=os.path.getmtime)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--analysis", default="", help="path to analysis.json")
    ap.add_argument("--song", default="", help="substring of the song name")
    ap.add_argument("--latest", action="store_true",
                    help="use the newest analysis.json")
    ap.add_argument("--vstpreset", action="store_true",
                    help="also write .vstpreset files (for VST3 hosts such as Cantabile)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    analysis_path = pick_analysis(args)
    with open(analysis_path, encoding="utf-8") as f:
        data = json.load(f)
    song = data["song"]
    stems = data["stems"]
    print(f"song: {song}")
    print(f"analysis: {analysis_path}")
    print(f"tone dir: {TONE_ROOT}")

    entries = build_song(song, stems, dry_run=args.dry_run,
                         vstpreset=args.vstpreset)

    if not args.dry_run:
        os.makedirs(TONE_ROOT, exist_ok=True)
        manifest_path = os.path.join(TONE_ROOT, "manifest.json")
        manifest = {}
        if os.path.isfile(manifest_path):
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
        manifest[song] = {
            "generated": datetime.datetime.now().isoformat(timespec="seconds"),
            "analysis": os.path.relpath(analysis_path, WORKSPACE),
            "how_to_load": {
                "surge_xt": "Drag the .fxp file onto the Surge XT window "
                            "(Surge accepts .fxp by drag-and-drop).",
                "vstpreset": "Only with --vstpreset: VST3 hosts (Cantabile) load it "
                             "through their own preset browser.",
                "sforzando": "Open the piano .sfz file from sforzando's file menu.",
            },
            "files": entries,
        }
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        print(f"\nwrote {len(entries)} tone files + {manifest_path}")
    else:
        print(f"\n[dry-run] would write {len(entries)} tone files")


if __name__ == "__main__":
    main()
