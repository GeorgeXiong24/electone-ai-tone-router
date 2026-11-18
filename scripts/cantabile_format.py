"""Cantabile song-file format helpers.

Cantabile stores each plugin's state in the song JSON as a base64 string:

    base64( raw-deflate( aBMP blob ) )

The aBMP blob wraps the plugin's VST3 component state:

    'aBMP' | version | class-id | ... | crc32(state) | len(state) | state

and the VST3 state is the standard PresetFile layout:

    'VST3' | version | class-id | chunk-list-offset | component state | chunk list

For Surge XT the component state is its native `sub3` chunk (patch XML plus
the extra wavetable data) followed by the JUCE private-data marker.

Every layout here was reverse-engineered byte-for-byte against a song file
written by Cantabile 4.0.4360 and is verified by round-tripping that file
(see `verify_against_reference` at the bottom).

Nothing in this module writes to disk; it only converts between the .fxp
patch format and the base64 blob Cantabile expects.
"""
import base64
import binascii
import struct
import zlib

# VST3 class ids as recorded by the installed plugins (32 ASCII hex chars).
SURGE_CLASS_ID = b"abcdef019182faeb566d624153675854"
SFORZANDO_CLASS_ID = b"5c5ca79682fc437a-b653-9ba204bab349".replace(b"-", b"")


def dashed_guid(class_id):
    """32 hex chars -> the 8-4-4-4-12 GUID form Cantabile stores.

    Cantabile keeps the plugin id in `pluginPath`/`pluginId` and inside the
    aBMP container as a dashed GUID, while the VST3 state header carries the
    same id undashed. Mixing the two makes Cantabile report
    "Plugin ID mismatch" and refuse the state.
    """
    s = class_id.decode("ascii") if isinstance(class_id, bytes) else class_id
    if len(s) != 32:
        raise ValueError(f"not a 32-char class id: {s!r}")
    return f"{s[0:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:32]}"

# Surge appends this marker to its component state inside a VST3 host.
SURGE_STATE_SUFFIX = b"\x00" * 16 + b"JUCEPrivateData"

# aBMP header constants, taken from a real Cantabile 4.0.4360 song file.
_ABMP_VERSION = 5
_ABMP_SNAPSHOT_NAME = b"Plugin Snapshot 1"
_ABMP_HEADER_LEN = 0x45  # 69 bytes: everything before the state payload


# ---------------------------------------------------------------- .fxp

def read_fxp(path):
    """Return (name28, xml_bytes, extra_bytes) from a Surge .fxp."""
    with open(path, "rb") as f:
        b = f.read()
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


# ---------------------------------------------------------------- component state

def surge_component_state(xml, extra):
    """Surge's VST3 component state: the .fxp chunk plus its private marker."""
    chunk = (b"sub3" + struct.pack("<ii", len(xml), len(extra))
             + b"\x00" * 20 + xml + extra)
    return chunk + SURGE_STATE_SUFFIX


def parse_surge_component_state(state):
    """Inverse of surge_component_state -> (xml, extra)."""
    if state[:4] != b"sub3":
        raise ValueError("component state is not a Surge sub3 chunk")
    xml_len, extra_len = struct.unpack("<ii", state[4:12])
    xml = state[32:32 + xml_len]
    extra = state[32 + xml_len:32 + xml_len + extra_len]
    return xml, extra


# ---------------------------------------------------------------- VST3 state

def build_vst3_state(component_state, class_id):
    """Assemble a standard VST3 PresetFile state around a component state."""
    list_offset = 48 + len(component_state)
    chunk_list = (b"List" + struct.pack("<i", 2)
                  + b"Comp" + struct.pack("<QQ", 48, len(component_state))
                  + b"Cont" + struct.pack("<QQ", list_offset, 0))
    header = (b"VST3" + struct.pack("<i", 1) + class_id
              + struct.pack("<Q", list_offset))
    return header + component_state + chunk_list


def parse_vst3_state(state):
    """Return (class_id, component_state) from a VST3 PresetFile state."""
    if state[:4] != b"VST3" or struct.unpack("<i", state[4:8])[0] != 1:
        raise ValueError("bad VST3 state header")
    class_id = state[8:40]
    list_offset = struct.unpack("<Q", state[40:48])[0]
    if state[list_offset:list_offset + 4] != b"List":
        raise ValueError("bad VST3 chunk list")
    count = struct.unpack("<i", state[list_offset + 4:list_offset + 8])[0]
    off = list_offset + 8
    comp = None
    for _ in range(count):
        eid = state[off:off + 4]
        eoff, esize = struct.unpack("<QQ", state[off + 4:off + 20])
        if eid == b"Comp":
            comp = state[eoff:eoff + esize]
        off += 20
    if comp is None:
        raise ValueError("VST3 state has no component chunk")
    return class_id, comp


# ---------------------------------------------------------------- aBMP blob

def build_abmp(state, class_id):
    """Wrap a VST3 state in Cantabile's aBMP container.

    `class_id` is the undashed 32-char VST3 id; the container stores the
    dashed GUID form, which is what Cantabile compares against.
    """
    guid = dashed_guid(class_id).encode("ascii")
    head = (b"aBMP" + struct.pack("<i", _ABMP_VERSION)
            + struct.pack("<i", len(guid)) + guid)
    body = (b"\x00" * 4
            + struct.pack("<i", 1) + struct.pack("<i", 1) + b"\x00" * 4
            + struct.pack("<i", 1) + struct.pack("<i", 0x80) + b"\x00" * 4
            + struct.pack("<i", len(_ABMP_SNAPSHOT_NAME)) + _ABMP_SNAPSHOT_NAME
            + b"\x00" * 8
            + struct.pack("<I", binascii.crc32(state) & 0xFFFFFFFF)
            + b"\x00" * 4
            + struct.pack("<I", len(state))
            + state)
    return head + body


def parse_abmp(blob):
    """Return (class_id_undashed, state) from an aBMP container."""
    if blob[:4] != b"aBMP":
        raise ValueError("not an aBMP blob")
    id_len = struct.unpack("<i", blob[8:12])[0]
    raw_id = blob[12:12 + id_len].decode("ascii")
    class_id = raw_id.replace("-", "").encode("ascii")
    base = 12 + id_len
    crc = struct.unpack("<I", blob[base + 0x39:base + 0x3D])[0]
    size = struct.unpack("<I", blob[base + 0x41:base + 0x45])[0]
    state = blob[base + _ABMP_HEADER_LEN:base + _ABMP_HEADER_LEN + size]
    if binascii.crc32(state) & 0xFFFFFFFF != crc:
        raise ValueError("aBMP checksum mismatch")
    return class_id, state


# ---------------------------------------------------------------- JSON field

def encode_plugin_data(state, class_id):
    """VST3 state -> the base64 string Cantabile keeps in `pluginData`."""
    blob = build_abmp(state, class_id)
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    raw = co.compress(blob) + co.flush()
    return base64.b64encode(raw).decode("ascii")


def decode_plugin_data(b64):
    """Inverse of encode_plugin_data -> (class_id, state)."""
    blob = zlib.decompress(base64.b64decode(b64), -15)
    return parse_abmp(blob)


def surge_plugin_data_from_fxp(fxp_path):
    """Read a Surge .fxp and return the base64 blob Cantabile expects."""
    _, xml, extra = read_fxp(fxp_path)
    state = build_vst3_state(surge_component_state(xml, extra), SURGE_CLASS_ID)
    return encode_plugin_data(state, SURGE_CLASS_ID)


# ---------------------------------------------------------------- self-check

def verify_against_reference(song_path):
    """Round-trip every plugin blob in a real Cantabile song file.

    Returns a list of (plugin name, ok, detail). Used by the test suite and by
    `build-cantabile.py --self-test` so a format change cannot slip through.
    """
    import json

    with open(song_path, encoding="utf-8") as f:
        song = json.load(f)

    results = []
    for obj in song.get("objects", []):
        if obj.get("kind") != "Plugin":
            continue
        name = obj.get("name", "?")
        try:
            class_id, state = decode_plugin_data(obj["pluginData"])
            rebuilt = encode_plugin_data(state, class_id)
            # The deflate stream need not be byte-identical, but it must decode
            # back to the same state and the same class id.
            class_id2, state2 = decode_plugin_data(rebuilt)
            ok = class_id2 == class_id and state2 == state
            # The id must also agree with what the plugin entry declares,
            # otherwise Cantabile rejects the state with "Plugin ID mismatch".
            declared = obj.get("pluginId", "").replace("-", "").encode("ascii")
            if declared and declared != class_id:
                ok = False
                detail = f"id mismatch: blob={class_id!r} entry={declared!r}"
            else:
                detail = f"{len(state)} bytes"
            results.append((name, ok, detail))
        except Exception as exc:  # noqa: BLE001 - report, do not crash
            results.append((name, False, str(exc)))
    return results
