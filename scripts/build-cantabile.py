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
import tone_paths  # noqa: E402  (tone tree layout: song / keyboard / plugin)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.environ.get("ELECTONE_WORKSPACE") or os.path.join(ROOT, "workspace")
TONE_ROOT = os.path.join(WORKSPACE, "tone")
SONG_ROOT = os.path.join(WORKSPACE, "cantabile")

SURGE_VST3 = (r"C:\Program Files\Common Files\VST3\Surge Synth Team"
              r"\Surge XT.vst3\Contents\x86_64-win\Surge XT.vst3")
SFORZANDO_VST3 = (r"C:\Program Files\Common Files\VST3\sforzando.vst3"
                  r"\Contents\x86_64-win\sforzando.vst3")

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

# Plugin kinds that can be imported, with the VST3 path each one needs.
PLUGIN_VST3 = {
    "surge-xt": SURGE_VST3,
    "sforzando": SFORZANDO_VST3,
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


def audio_route(unique_id, source_port="Output Out"):
    """Plugin output -> Main Speakers."""
    return {
        "kind": "audioRoute", "gainLevel": 1, "sendBalanceKind": -1,
        "stateManager": state_manager(13),
        "wiringStartInset": 10, "wiringEndInset": 10,
        "enabled": True, "userNotes": None, "uniqueID": unique_id, "name": None,
        "sourcePort": source_port,
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


def sforzando_audio_ports():
    """sforzando's bus layout: one stereo output, no sidechain."""
    return [{
        "kind": "output",
        "channels": [
            {"name": "Left (L)", "assignments": [{
                "assignedChannel": "pluginOutput\\#0",
                "mixLevel": 1, "panPlacement": "LeftBalance",
                "fadePlacement": "FrontBalance"}]},
            {"name": "Right (R)", "assignments": [{
                "assignedChannel": "pluginOutput\\#1",
                "mixLevel": 1, "panPlacement": "RightBalance",
                "fadePlacement": "FrontBalance"}]},
        ],
        "aliases": None, "enabled": True, "default": False,
        "showInMonitor": True, "includeInMasterIndicator": True,
        "enableLoopback": False, "name": "Stereo Output Out",
    }]


def sforzando_plugin_object(name, plugin_data, unique_id, x, y, route_id):
    guid = cf.dashed_guid(cf.SFORZANDO_CLASS_ID)
    return {
        "kind": "Plugin",
        "pluginPath": SFORZANDO_VST3 + ">" + guid,
        "pluginId": guid,
        "selectedProgram": 0,
        "autoMapAudioBusses": True,
        "audioBusConfiguration": [
            {"arrangement": "11", "name": "Stereo Output", "input": False,
             "active": True},
        ],
        "pluginData": plugin_data,
        "uiSettings": {
            "guiEditorDocked": False,
            "guiEditorPlacement": "378.4,412.8,788.8,564,False:False,1",
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
        "audioPorts": sforzando_audio_ports(),
        "gain": 1, "audioBypass": False, "wetDryBalance": 1,
        "panPosition": 0, "fadePosition": 0,
        "pluginInputChannelCount": 0, "pluginOutputChannelCount": 2,
        "switchPresetOnProgramChange": False,
        "userNotes": None,
        "stateManager": state_manager(115),
        "solo": False, "soloControl": False,
        "morphSets": {},
        "wiringPositionX": x, "wiringPositionY": y,
        "nextUniqueID": 1, "uniqueId": unique_id,
        "routes": [audio_route(route_id, source_port="Stereo Output Out")],
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

def collect_tones(song_entry, song=""):
    """Return the tones to import, one per (keyboard, instrument).

    A sampled instrument is always preferred over a synthesizer patch for the
    same part. Surge XT is a synthesizer: its factory presets are oscillator
    designs, so using one to imitate a recorded guitar or bass produces a
    *different instrument* — a nylon-guitar preset reads as a plucked zither,
    a fingered-bass preset reads as a muted trumpet. The stems themselves are
    the only source that carries the recording's actual timbre, so when
    build-sample-tones.py has produced an .sfz for a part, that is what gets
    imported and the Surge patch for the same part is skipped.

    The piano is the exception in the other direction: it is always the
    Salamander grand (an .sfz), never a Surge electric piano.

    Vocal tones are skipped (the melody is played live).
    """
    # Group by (keyboard, instrument) so the sampled/synth choice is made per
    # part rather than per file.
    by_part = {}
    skipped = []
    for entry in song_entry.get("files", []):
        rel = entry.get("file", "")
        instrument = entry.get("instrument", "")
        plugin = entry.get("plugin", "")
        keyboard = entry.get("keyboard", "")

        if instrument in SKIP_INSTRUMENTS:
            skipped.append((rel, SKIP_INSTRUMENTS[instrument]))
            continue
        if plugin not in PLUGIN_VST3:
            skipped.append((rel, f"{plugin} 音色不导入（只支持 Surge XT / sforzando）"))
            continue
        if keyboard not in KEYBOARD_LAYOUT:
            skipped.append((rel, f"未知键盘 {keyboard}"))
            continue
        tone_file = tone_paths.abs_from_song(TONE_ROOT, song, rel)
        if not os.path.isfile(tone_file):
            skipped.append((rel, "音色文件不存在"))
            continue
        by_part.setdefault((keyboard, instrument), []).append({
            "keyboard": keyboard,
            "instrument": instrument,
            "role": entry.get("role", ""),
            "plugin": plugin,
            "tone_file": tone_file,
            "rel": rel,
        })

    picked = []
    for (keyboard, instrument), cands in by_part.items():
        sampled = [c for c in cands if c["plugin"] == "sforzando"]
        if sampled:
            picked.append(sampled[0])
            for c in cands:
                if c["plugin"] != "sforzando":
                    skipped.append((c["rel"],
                                    f"改用采样音色（{sampled[0]['rel']}）"))
        else:
            picked.append(cands[0])
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
        if tone["plugin"] == "sforzando":
            plugin_data = cf.sforzando_plugin_data_from_sfz(tone["tone_file"])
            obj = sforzando_plugin_object(
                name=name,
                plugin_data=plugin_data,
                unique_id=next_unique_id,
                x=330 + column * 300,
                y=140 + row * 150,
                route_id=next_route_id,
            )
        else:
            plugin_data = cf.surge_plugin_data_from_fxp(tone["tone_file"])
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
            "host": tone["plugin"], "tone": tone["rel"],
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
            if class_id == cf.SURGE_CLASS_ID:
                xml, _ = cf.parse_surge_component_state(
                    cf.parse_vst3_state(state)[1])
                if b"<patch" not in xml[:200]:
                    problems.append(f"{obj['name']}: state is not a Surge patch")
            elif class_id == cf.SFORZANDO_CLASS_ID:
                sfz, _ = cf.parse_sforzando_component_state(
                    cf.parse_vst3_state(state)[1])
                if not sfz.lower().endswith(".sfz"):
                    problems.append(f"{obj['name']}: state does not name an .sfz")
                elif not os.path.isfile(sfz):
                    problems.append(f"{obj['name']}: .sfz missing: {sfz}")
            else:
                problems.append(f"{obj['name']}: unknown class id {class_id!r}")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{obj['name']}: {exc}")
    return plugins, problems


# ---------------------------------------------------------------- main

def load_song_entry(song):
    """Return the tone manifest entry for `song`, with sampled tones merged in.

    Two scripts write tone metadata: build-tones.py writes `manifest.json`
    (Surge patches + the piano .sfz) and build-sample-tones.py writes
    `sample-instruments.json` (the stem-sampled instruments). Cantabile needs
    both, because the sampled instruments are the ones that actually carry the
    recording's timbre.
    """
    manifest_path = os.path.join(TONE_ROOT, "manifest.json")
    if not os.path.isfile(manifest_path):
        sys.exit(f"no manifest at {manifest_path} - run build-tones.py first")
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    if song not in manifest:
        sys.exit(f"no entry for {song!r} in {manifest_path}")
    entry = copy.deepcopy(manifest[song])
    entry.setdefault("files", [])

    sample_path = os.path.join(TONE_ROOT, "sample-instruments.json")
    if os.path.isfile(sample_path):
        with open(sample_path, encoding="utf-8") as f:
            samples = json.load(f)
        for s in samples.get(song, {}).get("files", []):
            entry["files"].append({
                "file": s["sfz"],
                "keyboard": s["keyboard"],
                "plugin": "sforzando",
                "instrument": s["instrument"],
                "role": s.get("role", ""),
                "sampled": True,
                "notes_accepted": s.get("notes_accepted"),
                "keys_covered": s.get("keys_covered"),
            })
    return entry


def prune_stale(entry, song):
    """Drop manifest entries whose tone files no longer exist on disk.

    Both manifests accumulate entries for songs whose tone directory was
    deleted or never rebuilt (《不能說的祕密》 has audio and a manifest entry but
    no stems, so its referenced .sfz/.fxp paths are all gone). Left in place,
    they make build-cantabile pick a song whose every file is missing, and the
    error surfaces much later as "no tones to import" with no explanation.
    """
    kept, dropped = [], []
    for e in entry.get("files", []):
        rel = e.get("file") or e.get("sfz") or ""
        if rel and os.path.isfile(tone_paths.abs_from_song(TONE_ROOT, song, rel)):
            kept.append(e)
        else:
            dropped.append(rel)
    entry["files"] = kept
    return entry, dropped


def pick_song(args):
    manifest_path = os.path.join(TONE_ROOT, "manifest.json")
    if not os.path.isfile(manifest_path):
        sys.exit(f"no manifest at {manifest_path} - run build-tones.py first")
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    # A manifest entry is not a promise that the files are still there. Only
    # consider songs that still have at least one tone file on disk.
    alive = {}
    for name in manifest:
        entry, _ = prune_stale(load_song_entry(name), name)
        if entry["files"]:
            alive[name] = manifest[name]
    if not alive:
        sys.exit(f"no song in {manifest_path} has any tone file on disk - "
                 f"run build-tones.py / build-sample-tones.py first")
    if args.song:
        matches = [k for k in alive if args.song in k]
        if not matches:
            sys.exit(f"no song with existing tone files matching {args.song!r}")
        song = matches[0]
    elif not alive:
        sys.exit(f"manifest is empty: {manifest_path}")
    else:
        song = max(alive, key=lambda k: alive[k].get("generated", ""))
    entry, dropped = prune_stale(load_song_entry(song), song)
    for rel in dropped:
        print(f"  stale: {rel} (file no longer exists, skipped)")
    return song, entry


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

    tones, skipped = collect_tones(entry, song)
    for rel, why in skipped:
        print(f"  skip {rel}: {why}")
    if not tones:
        sys.exit("no tones to import")

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
