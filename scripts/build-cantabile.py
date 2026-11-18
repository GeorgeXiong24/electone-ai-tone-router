"""Build a Cantabile song file from the generated tone files.

Reads workspace/tone/manifest.json (written by build-tones.py) and produces a
.cantabileSong that Cantabile 4 can open directly:

  - one Surge XT plugin instance per tone file
  - right hand  -> MIDI channel 1
  - left hand   -> MIDI channel 2
  - foot        -> MIDI channel 3
  - every instance routed to Main Speakers

The song file is written inside the project workspace (workspace/cantabile/),
never into Cantabile's own user folders.

Only Surge XT tones are imported: sforzando/SFZ tones are skipped because the
sampled SFZ instruments do not reproduce the source timbre well enough to be
usable, and vocal tones are skipped because the melody is played live.

Usage:
  python build-cantabile.py --song <title substring>
  python build-cantabile.py --latest
  python build-cantabile.py --latest --dry-run
  python build-cantabile.py --self-test
"""
import argparse
import copy
import datetime
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cantabile_format as cf  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.environ.get("ELECTONE_WORKSPACE") or os.path.join(ROOT, "workspace")
TONE_ROOT = os.path.join(WORKSPACE, "tone")
SONG_ROOT = os.path.join(WORKSPACE, "cantabile")

SURGE_VST3 = (r"C:\Program Files\Common Files\VST3\Surge Synth Team"
              r"\Surge XT.vst3\Contents\x86_64-win\Surge XT.vst3")

# keyboard -> (MIDI channel, wiring column)
KEYBOARD_LAYOUT = {
    "右手": (1, 0),
    "左手": (2, 1),
    "脚": (3, 2),
}

# Stems that never become a Cantabile plugin.
SKIP_INSTRUMENTS = {
    "人声": "人声不做音色（旋律现场弹奏）",
    "鼓组": "鼓组不生成音色（节奏由 ELB-02 自带 rhythm 处理）",
}

# A minimal Cantabile 4.0.4360 song skeleton. Every field below was copied
# from a song file written by the real application; unknown fields are not
# invented, so Cantabile always sees a structure it produced itself.
SONG_TEMPLATE = {
    "metronome": {
        "timeSignature": "4/4", "tempo": 97, "masterTransportId": "metronome",
        "soundsGain": 1, "soundsMeasureBeatBalance": 0, "soundsEnabled": False,
        "measureSoundsEnabled": True,
        "stateManager": {
            "behaviour": 3, "indexedBehaviour": None, "externalBehaviour": 0,
            "externalIndexedBehaviour": None, "nonLinkedBehaviour": 0,
            "nonLinkedIndexedBehaviour": None, "resetBehaviour": 0,
            "resetIndexedBehaviour": None, "states": {}, "resetState": None,
        },
    },
    "onscreenKeyboardState": {
        "activeOctaveA": 6, "activeOctaveB": 7,
        "stateManager": {
            "behaviour": 3, "indexedBehaviour": None, "externalBehaviour": 0,
            "externalIndexedBehaviour": None, "nonLinkedBehaviour": 0,
            "nonLinkedIndexedBehaviour": None, "resetBehaviour": 0,
            "resetIndexedBehaviour": None, "states": {}, "resetState": None,
        },
    },
    "documents": {},
    "showNotes": [],
    "savedByBuild": 4360,
    "currentState": None,
    "bindings4": [],
    "bindingGroups": [{
        "name": "General", "expanded": True, "enabled": True,
        "slotColor": "Default", "userNotes": None,
        "stateManager": {
            "behaviour": 0, "indexedBehaviour": None, "externalBehaviour": 0,
            "externalIndexedBehaviour": None, "nonLinkedBehaviour": 0,
            "nonLinkedIndexedBehaviour": None, "resetBehaviour": 0,
            "resetIndexedBehaviour": None, "states": {}, "resetState": None,
        },
        "uniqueID": 1,
    }],
    "viewState": {
        "currentTabSet": {
            "name": "", "splitMode": "None", "splitPosition": 0,
            "liveModeSet": False,
            "panels": [{"id": "main", "tabs": ["song.Wiring"],
                        "activeTabId": "#0"}],
            "activePanel": "main", "pluginEditors": [],
        },
    },
    "states": [],
    "viewAsRoutingDiagram": True,
    "showPortsInRoutingDiagram": True,
    "useSharedWiringLayout": False,
    "comments": None,
    "autoShowComments": False,
    "savedByVersion": "4.0.4360",
    "modifiedBehaviour": "SignificantChanges",
}


def state_manager(behaviour):
    return {
        "behaviour": behaviour, "indexedBehaviour": None,
        "externalBehaviour": 0, "externalIndexedBehaviour": None,
        "nonLinkedBehaviour": 0, "nonLinkedIndexedBehaviour": None,
        "resetBehaviour": 0, "resetIndexedBehaviour": None,
        "states": {}, "resetState": None,
    }


def midi_route(source_port, target_name, channel, unique_id):
    """A MIDI route from the ELB-02 port to a plugin, filtered to one channel."""
    return {
        "kind": "midiRoute",
        "sourceChannelMask": 1 << (channel - 1),
        "keyRangeMin": 0, "keyRangeMax": 127, "transpose": 0,
        "velocityCurve": {
            "inputMin": 0, "inputMax": 127, "outputMin": 0, "outputMax": 127,
            "curveKind": "ExpandCompress", "curveAmount": 0,
        },
        "targetChannel": -1,
        "conditionOperator": "GreaterThanOrEqual", "conditionValue": 64,
        "notesOnly": False,
        "enableSourceProgramNumber": False, "sourceProgramNumber": 8421376,
        "ignoreSharedTranspose": False,
        "showKeyRange": False, "keyRangeColor": "Auto", "keyRangeText": "",
        "enableConditionCC": False, "conditionCC": 0,
        "midiFilter": {"items": []},
        "stateManager": state_manager(917),
        "wiringStartInset": 10, "wiringEndInset": 10,
        "enabled": True, "userNotes": None, "uniqueID": unique_id, "name": None,
        "sourcePort": source_port,
        "targetKind": "Plugin", "targetName": target_name,
        "targetPort": "MIDI In",
    }


def audio_route(unique_id):
    """Plugin output -> Main Speakers."""
    return {
        "kind": "audioRoute", "gainLevel": 1, "sendBalanceKind": -1,
        "stateManager": state_manager(13),
        "wiringStartInset": 10, "wiringEndInset": 10,
        "enabled": True, "userNotes": None, "uniqueID": unique_id, "name": None,
        "sourcePort": "Output Out",
        "targetKind": "OutputPortProvider", "targetName": "OutputPort",
        "targetPort": "Main Speakers",
    }


def surge_audio_ports():
    """Surge XT's bus layout: Sidechain in, Output + Scene A/B out."""
    def stereo_out(index, name):
        return {
            "kind": "output",
            "channels": [
                {"name": "Left (L)", "assignments": [{
                    "assignedChannel": f"pluginOutput\\#{index}",
                    "mixLevel": 1, "panPlacement": "LeftBalance",
                    "fadePlacement": "FrontBalance"}]},
                {"name": "Right (R)", "assignments": [{
                    "assignedChannel": f"pluginOutput\\#{index + 1}",
                    "mixLevel": 1, "panPlacement": "RightBalance",
                    "fadePlacement": "FrontBalance"}]},
            ],
            "aliases": None, "enabled": True, "default": False,
            "showInMonitor": True, "includeInMasterIndicator": True,
            "enableLoopback": False, "name": name,
        }

    sidechain = {
        "kind": "input",
        "channels": [
            {"name": "Left (L)", "assignments": [{
                "assignedChannel": "#0", "mixLevel": 1,
                "panPlacement": "None", "fadePlacement": "None"}]},
            {"name": "Right (R)", "assignments": [{
                "assignedChannel": "#1", "mixLevel": 1,
                "panPlacement": "None", "fadePlacement": "None"}]},
        ],
        "aliases": None, "enabled": True, "default": False,
        "showInMonitor": True, "includeInMasterIndicator": True,
        "enableLoopback": False, "name": "Sidechain In",
    }
    return [sidechain, stereo_out(0, "Output Out"),
            stereo_out(2, "Scene A Out"), stereo_out(4, "Scene B Out")]


def surge_plugin_object(name, plugin_data, unique_id, x, y, route_id):
    # Cantabile records the plugin id as a dashed GUID in both `pluginPath`
    # (after the '>') and `pluginId`; the VST3 state header keeps it undashed.
    guid = cf.dashed_guid(cf.SURGE_CLASS_ID)
    return {
        "kind": "Plugin",
        "pluginPath": SURGE_VST3 + ">" + guid,
        "pluginId": guid,
        "selectedProgram": 0,
        "autoMapAudioBusses": True,
        "audioBusConfiguration": [
            {"arrangement": "11", "name": "Sidechain", "input": True,
             "active": True},
            {"arrangement": "11", "name": "Output", "input": False,
             "active": True},
            {"arrangement": "11", "name": "Scene A", "input": False,
             "active": True},
            {"arrangement": "11", "name": "Scene B", "input": False,
             "active": True},
        ],
        "pluginData": plugin_data,
        "uiSettings": {
            "guiEditorDocked": False,
            "guiEditorPlacement": "92.8,524,1612,1060.8,False:False,1",
            "guiEditorPlacementDetached": False,
            "GuiEditorOpen": False, "ParameterEditorOpen": False,
        },
        "blobs": {},
        "midiPassthrough": False,
        "trackParameterChanges": False,
        "shouldFlushTailSounds": False,
        "runMode": "Running",
        "midiPorts": [{
            "IsInput": True, "midiFilter": {"items": []},
            "pluginPortName": "MIDI In", "name": "MIDI In",
        }],
        "audioPorts": surge_audio_ports(),
        "gain": 1, "audioBypass": False, "wetDryBalance": 1,
        "panPosition": 0, "fadePosition": 0,
        "pluginInputChannelCount": 2, "pluginOutputChannelCount": 6,
        "switchPresetOnProgramChange": False,
        "userNotes": None,
        "stateManager": state_manager(115),
        "solo": False, "soloControl": True,
        "morphSets": {},
        "wiringPositionX": x, "wiringPositionY": y,
        "nextUniqueID": 1, "uniqueId": unique_id,
        "routes": [audio_route(route_id)],
        "name": name, "sendBalance": 0, "displayExpanded": True,
        "slotColor": "Default",
    }


def input_port_object(routes, unique_id, next_unique_id):
    return {
        "kind": "InputPortProvider",
        "transpose": 0, "inputGain": 1,
        "stateManager": state_manager(2),
        "userNotes": None,
        "portWiringPositions": {
            "MIDI.Main Keyboard": 65, "MIDI.Onscreen Keyboard": 90,
            "Audio.Main Microphone": 115, "MIDI.ELB-02": 40,
        },
        "wiringPositionX": 180, "wiringPositionY": 0,
        "nextUniqueID": next_unique_id, "uniqueId": unique_id,
        "routes": routes,
        "name": "InputPort", "sendBalance": 0, "displayExpanded": True,
        "slotColor": "Default",
    }


def output_port_object(unique_id):
    return {
        "kind": "OutputPortProvider",
        "outputGain": 1,
        "stateManager": state_manager(0),
        "userNotes": None,
        "portWiringPositions": {
            "MIDI.External Synth": 40, "MIDI.Onscreen Keyboard": 65,
            "Audio.Main Speakers": 90, "Audio.Metronome": 115,
        },
        "wiringPositionX": 180, "wiringPositionY": 0,
        "nextUniqueID": 1, "uniqueId": unique_id,
        "routes": [],
        "name": "OutputPort", "sendBalance": 0, "displayExpanded": True,
        "slotColor": "Default",
    }


# ---------------------------------------------------------------- selection

def collect_tones(song_entry):
    """Return the Surge tones to import, grouped by keyboard.

    Only Surge XT tones are used: SFZ/sforzando tones are skipped (the sampled
    instruments do not reproduce the source timbre) and vocal tones are
    skipped (the melody is played live).
    """
    picked = []
    skipped = []
    for entry in song_entry.get("files", []):
        rel = entry.get("file", "")
        instrument = entry.get("instrument", "")
        plugin = entry.get("plugin", "")
        keyboard = entry.get("keyboard", "")

        if instrument in SKIP_INSTRUMENTS:
            skipped.append((rel, SKIP_INSTRUMENTS[instrument]))
            continue
        if plugin != "surge-xt":
            skipped.append((rel, f"{plugin} 音色不导入（只用 Surge XT）"))
            continue
        if keyboard not in KEYBOARD_LAYOUT:
            skipped.append((rel, f"未知键盘 {keyboard}"))
            continue
        fxp = os.path.join(TONE_ROOT, rel)
        if not os.path.isfile(fxp):
            skipped.append((rel, "音色文件不存在"))
            continue
        picked.append({
            "keyboard": keyboard,
            "instrument": instrument,
            "role": entry.get("role", ""),
            "fxp": fxp,
            "rel": rel,
        })
    return picked, skipped


def plugin_name_for(tone):
    role = tone["role"] or "音色"
    return f"{tone['keyboard']} · {tone['instrument']}（{role}）"


# ---------------------------------------------------------------- build

def build_song(song, tones, dry_run=False):
    """Assemble the song dict. Returns (song_dict, summary)."""
    song_dict = copy.deepcopy(SONG_TEMPLATE)
    song_dict["comments"] = (
        f"{song} — 由 electone-ai-tone-router 生成。"
        f"右手=通道1，左手=通道2，脚=通道3。")

    objects = []
    midi_routes = []
    next_unique_id = 3          # 1 = InputPort, 2 = OutputPort
    next_route_id = 1
    next_midi_route_id = 1

    # Lay the plugins out in three columns, one per keyboard.
    column_counts = {}
    summary = []

    for tone in tones:
        keyboard = tone["keyboard"]
        channel, column = KEYBOARD_LAYOUT[keyboard]
        row = column_counts.get(keyboard, 0)
        column_counts[keyboard] = row + 1

        name = plugin_name_for(tone)
        plugin_data = cf.surge_plugin_data_from_fxp(tone["fxp"])

        obj = surge_plugin_object(
            name=name,
            plugin_data=plugin_data,
            unique_id=next_unique_id,
            x=330 + column * 300,
            y=140 + row * 150,
            route_id=next_route_id,
        )
        objects.append(obj)
        next_unique_id += 1
        next_route_id += 1

        midi_routes.append(midi_route(
            source_port="ELB-02",
            target_name=name,
            channel=channel,
            unique_id=next_midi_route_id,
        ))
        next_midi_route_id += 1

        summary.append({
            "plugin": name, "keyboard": keyboard, "channel": channel,
            "instrument": tone["instrument"], "role": tone["role"],
            "fxp": tone["rel"],
        })

    objects.append(input_port_object(
        midi_routes, unique_id=1, next_unique_id=next_midi_route_id))
    objects.append(output_port_object(unique_id=2))

    song_dict["objects"] = objects
    song_dict["nextUniqueID"] = next_unique_id
    return song_dict, summary


def write_song(song_dict, song, dry_run=False):
    os.makedirs(SONG_ROOT, exist_ok=True)
    safe = song.replace("/", "-").replace("\\", "-")
    path = os.path.join(SONG_ROOT, f"{safe}.cantabileSong")
    if dry_run:
        return path
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(song_dict, f, ensure_ascii=False, indent="\t")
    return path


def verify_song(path):
    """Re-open the written song and check every plugin blob decodes."""
    with open(path, encoding="utf-8") as f:
        song = json.load(f)
    problems = []
    plugins = 0
    for obj in song.get("objects", []):
        if obj.get("kind") != "Plugin":
            continue
        plugins += 1
        try:
            class_id, state = cf.decode_plugin_data(obj["pluginData"])
            if class_id != cf.SURGE_CLASS_ID:
                problems.append(f"{obj['name']}: class id mismatch")
                continue
            xml, _ = cf.parse_surge_component_state(
                cf.parse_vst3_state(state)[1])
            if b"<patch" not in xml[:200]:
                problems.append(f"{obj['name']}: state is not a Surge patch")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{obj['name']}: {exc}")
    return plugins, problems


# ---------------------------------------------------------------- main

def pick_song(args):
    manifest_path = os.path.join(TONE_ROOT, "manifest.json")
    if not os.path.isfile(manifest_path):
        sys.exit(f"no manifest at {manifest_path} - run build-tones.py first")
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    if args.song:
        matches = [k for k in manifest if args.song in k]
        if not matches:
            sys.exit(f"no song matching {args.song!r} in {manifest_path}")
        return matches[0], manifest[matches[0]]
    if not manifest:
        sys.exit(f"manifest is empty: {manifest_path}")
    return max(manifest, key=lambda k: manifest[k].get("generated", "")), \
        manifest[max(manifest, key=lambda k: manifest[k].get("generated", ""))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--song", default="", help="substring of the song name")
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true",
                    help="round-trip a real Cantabile song file and exit")
    args = ap.parse_args()

    if args.self_test:
        ref = os.path.join(os.path.expanduser("~"), "Documents", "Cantabile",
                           "song1.cantabileSong")
        if not os.path.isfile(ref):
            sys.exit(f"no reference song to test against: {ref}")
        results = cf.verify_against_reference(ref)
        for name, ok, detail in results:
            print(("OK   " if ok else "FAIL ") + name + "  " + detail)
        sys.exit(0 if all(ok for _, ok, _ in results) else 1)

    song, entry = pick_song(args)
    print(f"song: {song}")
    print(f"tone dir: {TONE_ROOT}")

    tones, skipped = collect_tones(entry)
    for rel, why in skipped:
        print(f"  skip {rel}: {why}")
    if not tones:
        sys.exit("no Surge tones to import")

    song_dict, summary = build_song(song, tones, dry_run=args.dry_run)
    path = write_song(song_dict, song, dry_run=args.dry_run)

    print()
    for item in summary:
        print(f"  ch{item['channel']}  {item['plugin']}")
    print()

    if args.dry_run:
        print(f"[dry-run] would write {path}")
        return

    plugins, problems = verify_song(path)
    print(f"wrote {path}")
    print(f"verified {plugins} plugin(s)")
    if problems:
        for p in problems:
            print(f"  PROBLEM {p}")
        sys.exit(1)

    # Record the song in the tone manifest so later steps can find it.
    manifest_path = os.path.join(TONE_ROOT, "manifest.json")
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    manifest[song]["cantabile"] = {
        "song_file": os.path.relpath(path, WORKSPACE),
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "plugins": summary,
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
