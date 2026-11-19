"""Feature-driven Surge XT patch parameter design (the "decision layer").

This module turns the objective acoustic features that analyze-tones.py extracts
from a stem (spectral centroid / brightness, attack-vs-sustain brightness
contrast, harmonic ratio, ADSR-ish envelope statistics) into concrete Surge XT
patch parameter values, so build-tones.py no longer just relabels a template
preset but actually shapes its filter and envelopes to the analyzed audio.

Unit semantics are taken from the Surge XT source (src/common/Parameter.cpp,
`Parameter::set_type` and the ATwoToTheBx display block):

  * filter cutoff / low cut (`ct_freq_audible`, `ct_freq_hpf`):
        Hz = 440 * 2 ** (value / 12)
        cutoff value range [-60, 70]  ->  13.75 Hz .. 25087.71 Hz
        lowcut  value range [-72, 15] ->   6.88 Hz ..  1046.5 Hz
  * envelope / LFO times (`ct_envtime`):
        seconds = 2 ** value,  value range [-8, 5]  ->  3.9 ms .. 32 s
  * sustain / resonance / percent params: plain 0 .. 1
  * filter env modulation (`ct_freq_mod`): semitones, range [-96, 96]

Every decision is returned with a human-readable reason so the choice can be
audited later (it is written into tone/manifest.json and the patch comment).
All thresholds live in CFG — do not scatter magic numbers.

Usage (from build-tones.py):

    from tone_design import design_patch_params
    design = design_patch_params(stem_features)
    # design["params"] = {"a_filter1_cutoff": 2.83, ...}
    # design["notes"]  = [{"param": ..., "value": ..., "reason": ...}, ...]
"""
import math

# --------------------------------------------------------------- configuration

CFG = {
    # --- filter cutoff -------------------------------------------------------
    # A lowpass at cutoff fc leaves a saw-ish source with a spectral centroid of
    # roughly fc / 1.6, so the target cutoff is the measured brightness scaled
    # by this ratio.
    "cutoff_ratio": 1.6,
    "cutoff_hz_min": 40.0,
    "cutoff_hz_max": 12000.0,
    # --- filter envelope ----------------------------------------------------
    # attack / sustain brightness contrast that starts to justify filter env mod
    "brightness_ratio_envmod": 1.25,
    "envmod_max_semitones": 36.0,
    "envmod_gain": 1.5,          # semitones per doubling of the contrast
    # --- resonance ----------------------------------------------------------
    "resonance_min": 0.05,
    "resonance_max": 0.45,
    "resonance_gain": 0.22,      # added per unit of harmonic_ratio
    # --- amp envelope clamps (seconds) --------------------------------------
    "attack_s_min": 0.0039, "attack_s_max": 4.0,
    "decay_s_min": 0.02, "decay_s_max": 8.0,
    "release_s_min": 0.03, "release_s_max": 8.0,
    # per-tendency envelopes: (sustain lo, sustain hi, decay lo, decay hi,
    #                          release lo, release hi)
    "env_sustained": (0.35, 0.95, 1.0, 8.0, 0.25, 6.0),
    "env_decaying": (0.15, 0.60, 0.05, 4.0, 0.05, 2.0),
    "env_percussive": (0.00, 0.20, 0.02, 1.0, 0.03, 0.8),
}

# Surge parameter bounds (Parameter.cpp): keep written values legal.
CUTOFF_VALUE_RANGE = (-60.0, 70.0)
LOWCUT_VALUE_RANGE = (-72.0, 15.0)
ENVTIME_VALUE_RANGE = (-8.0, 5.0)
ENVMOD_VALUE_RANGE = (-96.0, 96.0)
PERCENT_RANGE = (0.0, 1.0)


# --------------------------------------------------------------- unit helpers

def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def hz_to_cutoff_value(hz):
    """Hz -> Surge filter-cutoff natural value (440 * 2**(v/12) = Hz)."""
    v = 12.0 * math.log2(max(hz, 1e-3) / 440.0)
    return clamp(v, *CUTOFF_VALUE_RANGE)


def cutoff_value_to_hz(v):
    return 440.0 * 2.0 ** (v / 12.0)


def seconds_to_envtime_value(seconds):
    """Seconds -> Surge envelope-time natural value (2**v = seconds)."""
    v = math.log2(max(seconds, 1e-4))
    return clamp(v, *ENVTIME_VALUE_RANGE)


def envtime_value_to_seconds(v):
    return 2.0 ** v


# --------------------------------------------------------------- design rules

def _pick_tendency(env):
    return env.get("sustain_tendency") or "decaying"


def _note(param, value, reason):
    return {"param": param, "value": round(float(value), 6), "reason": reason}


def design_patch_params(feats):
    """Map analyzed stem features to Surge XT scene-A patch parameters.

    `feats` is one stem's entry from analysis.json. Returns a dict:
        params: {surge param name: natural value}
        notes:  [{param, value, reason}]   audit trail
        features: the feature subset the design used
    Missing features simply skip the dependent decisions (the template value
    stays), so old analysis.json files keep working.
    """
    params = {}
    notes = []
    used = {}

    env = feats.get("envelope") or {}
    brightness = feats.get("brightness_hz")
    harmonic_ratio = feats.get("harmonic_ratio")

    # ---- 1. amplitude envelope (a_env1_*) ---------------------------------
    tendency = _pick_tendency(env)
    sus_lo, sus_hi, dec_lo, dec_hi, rel_lo, rel_hi = CFG[f"env_{tendency}"]

    if env.get("attack_ms") is not None:
        atk_s = clamp(env["attack_ms"] / 1000.0,
                      CFG["attack_s_min"], CFG["attack_s_max"])
        v = seconds_to_envtime_value(atk_s)
        params["a_env1_attack"] = v
        used["attack_ms"] = env["attack_ms"]
        notes.append(_note(
            "a_env1_attack", v,
            f"起音 {env['attack_ms']:.0f}ms → {atk_s * 1000:.0f}ms（{atk_s:.4f}s，"
            f"Surge envtime=log2(秒)）"))

    if env.get("decay_ms") is not None:
        dec_s = clamp(env["decay_ms"] / 1000.0, dec_lo, dec_hi)
        v = seconds_to_envtime_value(dec_s)
        params["a_env1_decay"] = v
        used["decay_ms"] = env["decay_ms"]
        notes.append(_note(
            "a_env1_decay", v,
            f"衰减 {env['decay_ms']:.0f}ms，倾向「{tendency}」→ {dec_s * 1000:.0f}ms"))

    if env.get("sustain_level") is not None:
        sus = clamp(env["sustain_level"], sus_lo, sus_hi)
        params["a_env1_sustain"] = clamp(sus, *PERCENT_RANGE)
        used["sustain_level"] = env["sustain_level"]
        notes.append(_note(
            "a_env1_sustain", sus,
            f"持续电平 {env['sustain_level']:.2f}（相对峰值），倾向「{tendency}」→ {sus:.2f}"))

    if env.get("release_ms") is not None:
        rel_s = clamp(env["release_ms"] / 1000.0, rel_lo, rel_hi)
        v = seconds_to_envtime_value(rel_s)
        params["a_env1_release"] = v
        used["release_ms"] = env["release_ms"]
        notes.append(_note(
            "a_env1_release", v,
            f"释放 {env['release_ms']:.0f}ms → {rel_s * 1000:.0f}ms"))

    # ---- 2. filter envelope (a_env2_*) ------------------------------------
    # Only meaningful together with filter env modulation below; mirrored from
    # the amp envelope but with a shorter, gentler contour.
    if "a_env1_attack" in params:
        params["a_env2_attack"] = params["a_env1_attack"]
        notes.append(_note("a_env2_attack", params["a_env1_attack"],
                           "滤波包络跟随放大包络的起音"))
    if env.get("decay_ms") is not None:
        dec_s = clamp(env["decay_ms"] / 1000.0, CFG["decay_s_min"], 1.0)
        v = seconds_to_envtime_value(dec_s)
        params["a_env2_decay"] = v
        notes.append(_note("a_env2_decay", v,
                           f"滤波包络衰减取 {dec_s * 1000:.0f}ms（上限 1s）"))
    if tendency == "percussive":
        params["a_env2_sustain"] = 0.0
        notes.append(_note("a_env2_sustain", 0.0, "打击性音色：滤波包络无持续段"))

    # ---- 3. filter cutoff (a_filter1_cutoff) ------------------------------
    if brightness:
        fc = clamp(float(brightness) * CFG["cutoff_ratio"],
                   CFG["cutoff_hz_min"], CFG["cutoff_hz_max"])
        v = hz_to_cutoff_value(fc)
        params["a_filter1_cutoff"] = v
        used["brightness_hz"] = brightness
        notes.append(_note(
            "a_filter1_cutoff", v,
            f"亮度（谱质心）{float(brightness):.0f}Hz × {CFG['cutoff_ratio']} "
            f"→ 低通 {fc:.0f}Hz（{12 * math.log2(fc / 440):.2f} 半音 @A4）"))

    # ---- 4. filter env modulation (a_filter1_envmod) ----------------------
    atk_b = feats.get("brightness_attack_hz")
    sus_b = feats.get("brightness_sustain_hz")
    if atk_b and sus_b and sus_b > 0:
        contrast = float(atk_b) / float(sus_b)
        used["brightness_contrast"] = round(contrast, 3)
        if contrast >= CFG["brightness_ratio_envmod"]:
            semis = clamp(12.0 * math.log2(contrast) * CFG["envmod_gain"],
                          0.0, CFG["envmod_max_semitones"])
        else:
            semis = 0.0
        params["a_filter1_envmod"] = clamp(semis, *ENVMOD_VALUE_RANGE)
        notes.append(_note(
            "a_filter1_envmod", semis,
            f"起音亮度 {float(atk_b):.0f}Hz / 持续亮度 {float(sus_b):.0f}Hz "
            f"= {contrast:.2f}× → 滤波包络 +{semis:.1f} 半音"))

    # ---- 5. resonance (a_filter1_resonance) -------------------------------
    if harmonic_ratio is not None:
        target = (CFG["resonance_min"]
                  + CFG["resonance_gain"] * float(harmonic_ratio))
        target = clamp(target, CFG["resonance_min"], CFG["resonance_max"])
        params["a_filter1_resonance"] = clamp(target, *PERCENT_RANGE)
        used["harmonic_ratio"] = harmonic_ratio
        notes.append(_note(
            "a_filter1_resonance", target,
            f"谐波占比 {float(harmonic_ratio):.2f} → 共振 {target:.2f}"))

    return {"params": params, "notes": notes, "features": used}


def describe_design(design):
    """Short multi-line summary for patch comments / console output."""
    return [f"{n['param']}={n['value']:.3f}（{n['reason']}）" for n in design["notes"]]
