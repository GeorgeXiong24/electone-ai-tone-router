"""Apply a Surge XT .fxp patch to a live plugin instance, parameter by parameter.

Why this exists: pedalboard cannot load a Surge XT patch through `raw_state`
(the setter is silently ignored) or `preset_data` (it accepts the bytes without
error but the plugin keeps its default sound). The only path that actually
changes the sound is writing the individual VST3 parameters, which this module
does by mapping the patch XML's parameter tags onto Surge's VST3 parameter
names.

The mapping is mechanical:

    XML tag                     VST3 parameter
    <a_filter1_cutoff .../>  -> a_filter_1_cutoff
    <a_env1_attack .../>     -> a_amp_eg_attack
    <a_env2_attack .../>     -> a_filter_eg_attack
    <a_osc1_type .../>       -> a_osc_1_type

Surge's XML uses `filter1` / `env1` / `osc1`; its VST3 parameter names use
`filter_1` / `amp_eg` / `osc_1`. Everything else is identical.

Values are written in *natural* units, which is what the XML stores. Surge's
VST3 parameters are not uniformly natural though, so each parameter is written
in the unit its own range implies:

  * a parameter whose range is the natural range (cutoff 13.75..25087.71 Hz,
    resonance 0..100 %) takes the natural value directly;
  * a parameter whose range is 0..1 (envelope times, env-mod amounts) takes the
    value normalized against the natural bounds;
  * a stepped parameter (filter type, oscillator type) takes the option index
    divided by (count - 1).

`_write_param` decides which case applies from the parameter's own reported
range, so no per-parameter table has to be maintained.
"""
import re
import struct

# Natural bounds for the parameter families whose VST3 range is 0..1 but whose
# XML value is in natural units. Taken from Surge's Parameter.cpp.
_NATURAL_BOUNDS = {
    # envelope / LFO times: seconds = 2 ** value
    "envtime": (-8.0, 5.0),
    # filter env modulation: semitones
    "freq_mod": (-96.0, 96.0),
    # oscillator pitch: semitones
    "pitch": (-60.0, 60.0),
    # portamento: seconds = 2 ** value
    "portamento": (-8.0, 5.0),
    # filter cutoff: Hz = 440 * 2 ** (value / 12)
    "cutoff": (-60.0, 70.0),
    # highpass / lowcut: Hz = 440 * 2 ** (value / 12)
    "lowcut": (-72.0, 15.0),
}

# XML prefix -> VST3 prefix. Longest first so `env1` is not eaten by `env`.
_PREFIX_MAP = [
    ("a_filter1_", "a_filter_1_"),
    ("a_filter2_", "a_filter_2_"),
    ("a_osc1_", "a_osc_1_"),
    ("a_osc2_", "a_osc_2_"),
    ("a_osc3_", "a_osc_3_"),
    ("a_env1_", "a_amp_eg_"),
    ("a_env2_", "a_filter_eg_"),
    ("a_lfo1_", "a_lfo_1_"),
    ("a_lfo2_", "a_lfo_2_"),
    ("a_lfo3_", "a_lfo_3_"),
    ("a_lfo4_", "a_lfo_4_"),
    ("a_lfo5_", "a_lfo_5_"),
    ("a_lfo6_", "a_lfo_6_"),
    ("a_stepseq1_", "a_stepseq_1_"),
    ("a_stepseq2_", "a_stepseq_2_"),
]

# XML tag -> VST3 name, for the tags whose names differ beyond the prefix map.
_EXACT_MAP = {
    "a_volume": "a_volume",
    "a_pan": "a_pan",
    "a_width": "a_width",
    "a_pitch": "a_pitch",
    "a_octave": "a_octave",
    "a_portamento": "a_portamento",
    "a_play_mode": "a_play_mode",
    "a_fm_depth": "a_fm_depth",
    "a_fm_routing": "a_fm_routing",
    "a_osc_drift": "a_osc_drift",
    "a_noise_color": "a_noise_color",
    "a_noise_volume": "a_noise_volume",
    "a_feedback": "a_feedback",
    "a_highpass": "a_highpass",
    "a_vca_gain": "a_vca_gain",
    "a_velocity_vca_gain": "a_velocity_vca_gain",
    "a_filter_configuration": "a_filter_configuration",
    "a_filter_balance": "a_filter_balance",
    "a_link_resonance": "a_link_resonance",
    "a_waveshaper_type": "a_waveshaper_type",
    "a_waveshaper_drive": "a_waveshaper_drive",
    "a_pre_filter_gain": "a_pre_filter_gain",
}

# Tags that are discrete in VST3: the XML stores the option index directly.
_DISCRETE_TAGS = {
    "a_filter1_type", "a_filter2_type",
    "a_osc1_type", "a_osc2_type", "a_osc3_type",
    "a_filter1_subtype", "a_filter2_subtype",
    "a_play_mode", "a_fm_routing", "a_waveshaper_type",
    "a_filter_configuration",
}

# Tags whose VST3 parameter reports a natural range but still expects a
# normalized position in raw_value. Surge's cutoff is one: it reports
# 13.75..25087.71 Hz yet raw_value is the position across that range.
_NORMALIZED_TAGS = {
    "a_filter1_cutoff", "a_filter2_cutoff",
    "a_highpass",
}

PARAM_RE = re.compile(rb'<(a_[a-z0-9_]+)\b[^>]*?value="([^"]*)"')


def read_fxp_xml(path):
    """Return the patch XML bytes from a Surge .fxp."""
    with open(path, "rb") as f:
        b = f.read()
    if b[:4] != b"CcnK" or b[8:12] != b"FPCh":
        raise ValueError(f"not a Surge fxp: {path}")
    size = struct.unpack(">i", b[56:60])[0]
    chunk = b[60:60 + size]
    if chunk[:4] != b"sub3":
        raise ValueError(f"unexpected fxp chunk tag: {path}")
    xml_len, _ = struct.unpack("<ii", chunk[4:12])
    return chunk[32:32 + xml_len]


def vst3_name(xml_tag):
    """Map an XML parameter tag to Surge's VST3 parameter name."""
    if xml_tag in _EXACT_MAP:
        return _EXACT_MAP[xml_tag]
    for xml_prefix, vst_prefix in _PREFIX_MAP:
        if xml_tag.startswith(xml_prefix):
            return vst_prefix + xml_tag[len(xml_prefix):]
    return xml_tag


def patch_params(xml):
    """Return {xml_tag: float} for every parameter the patch sets."""
    out = {}
    for m in PARAM_RE.finditer(xml):
        tag = m.group(1).decode("ascii")
        try:
            out[tag] = float(m.group(2))
        except ValueError:
            continue
    return out


def apply_patch(plugin, xml, skip=()):
    """Write a patch's parameters into a loaded Surge XT plugin instance.

    Returns (applied, missing) where `applied` maps the XML tag to the value
    written and `missing` lists tags Surge has no matching parameter for.
    """
    params = plugin.parameters
    applied, missing = {}, []
    for tag, value in patch_params(xml).items():
        if tag in skip:
            continue
        name = vst3_name(tag)
        if name not in params:
            missing.append(tag)
            continue
        _write_param(params[name], tag, value)
        applied[tag] = value
    return applied, missing


def _write_param(param, tag, value):
    """Write one XML value into a VST3 parameter, in the unit it expects.

    Surge exposes most continuous parameters as *stepped* VST3 parameters (a
    cutoff has ~957 discrete positions, an envelope time ~957). For those,
    `raw_value` is the normalized position in 0..1, not the natural value, so
    the natural value has to be mapped through the parameter's own range.
    """
    text = str(param)

    # Stepped parameter with a string list: raw_value is a normalized position.
    if "valid string values" in text:
        nat_lo, nat_hi = _natural_bounds(tag)
        if nat_lo is None:
            nat_lo, nat_hi = _param_range(param)
        if nat_lo is None:
            return
        norm = (value - nat_lo) / (nat_hi - nat_lo) if nat_hi != nat_lo else 0.0
        param.raw_value = min(1.0, max(0.0, norm))
        return

    if tag in _DISCRETE_TAGS:
        n = _option_count(param)
        param.raw_value = 0.0 if n <= 1 else min(1.0, max(0.0, value / (n - 1)))
        return

    lo, hi = _param_range(param)
    if lo is None:
        param.raw_value = value
        return

    # Surge reports a natural range for cutoff (13.75..25087.71 Hz) but still
    # treats it as a stepped parameter, so raw_value is the normalized position
    # across that range, not the natural value. The mapping is logarithmic:
    #   natural = raw * 130 - 60,  Hz = 440 * 2 ** (natural / 12)
    # which is exactly (value - lo) / (hi - lo) with lo/hi in natural units.
    if tag in _NORMALIZED_TAGS:
        nat_lo, nat_hi = _NATURAL_BOUNDS["cutoff"]
        norm = (value - nat_lo) / (nat_hi - nat_lo) if nat_hi != nat_lo else 0.0
        param.raw_value = min(1.0, max(0.0, norm))
        return

    # A 0..1 range means the plugin wants a normalized position, not the
    # natural value the XML stores.
    if abs(lo) < 1e-9 and abs(hi - 1.0) < 1e-9:
        nat_lo, nat_hi = _natural_bounds(tag)
        if nat_lo is None:
            param.raw_value = min(1.0, max(0.0, value))
            return
        norm = (value - nat_lo) / (nat_hi - nat_lo)
        param.raw_value = min(1.0, max(0.0, norm))
        return

    # The parameter's range is already the natural range: write it directly.
    param.raw_value = value


def _param_range(param):
    """Return (lo, hi) of a parameter's range, or (None, None)."""
    m = re.search(r"range=\(([-\d.e+]+),\s*([-\d.e+]+)", str(param))
    if not m:
        return None, None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None, None


def _natural_bounds(tag):
    """Natural (XML-unit) bounds for a parameter family, or (None, None)."""
    if "env" in tag and ("attack" in tag or "decay" in tag
                         or "release" in tag or "sustain" in tag):
        # Sustain is a plain 0..1 level, not a time.
        if "sustain" in tag:
            return 0.0, 1.0
        return _NATURAL_BOUNDS["envtime"]
    if "envmod" in tag or "feg_mod" in tag:
        return _NATURAL_BOUNDS["freq_mod"]
    if "portamento" in tag:
        return _NATURAL_BOUNDS["portamento"]
    if "_pitch" in tag or tag.endswith("_pitch"):
        return _NATURAL_BOUNDS["pitch"]
    return None, None


def _option_count(param):
    """Number of discrete options a stepped parameter exposes."""
    text = str(param)
    m = re.search(r"\((\d+) valid string values\)", text)
    if m:
        return int(m.group(1))
    m = re.search(r"\((\d+) valid values\)", text)
    if m:
        return int(m.group(1))
    return 0
