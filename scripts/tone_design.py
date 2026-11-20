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
    # The absolute spectral centroid is not a timbre: it scales with pitch, so a
    # bass at 90 Hz and a guitar at 1865 Hz are not "dark" and "bright"
    # instruments, they are low and high ones. What distinguishes timbres is the
    # centroid *relative to the fundamental* — measured on the stems:
    #   bass 1.59x, piano 7.31x, guitar 18.92x
    # A lowpass at cutoff fc renders a centroid of roughly fc * 0.18 (measured
    # by sweeping a real Surge patch), so the cutoff that reproduces a stem's
    # relative brightness is
    #   fc = fundamental * relative_brightness / 0.18
    "centroid_to_cutoff": 1.0 / 0.18,
    "cutoff_hz_min": 40.0,
    "cutoff_hz_max": 12000.0,
    # Fallback when the stem has no usable fundamental: treat the measured
    # centroid as if it belonged to a mid-register note.
    "fallback_fundamental_hz": 220.0,
    # A lowpass below the note's fundamental removes the note itself: the bass
    # patch designed at 65 Hz rendered at -50 dB with a 21 Hz centroid, because
    # the fundamental (41-98 Hz for E1-G2) sat above the cutoff. Never design a
    # cutoff below this multiple of the stem's own register.
    "cutoff_floor_vs_fundamental": 1.5,
    # How far the designed cutoff may move away from the template's own cutoff,
    # in octaves. The template was voiced by a sound designer, so the
    # measurement only gets to nudge it — but the nudge has to be wide enough to
    # matter: with the calibrated centroid->cutoff ratio the design lands
    # several octaves away from a template voiced for a different instrument,
    # and clamping to 1.5 octaves left every patch at the clamp boundary.
    "cutoff_max_shift_octaves": 3.0,
    # A cutoff this far above the fundamental lets the oscillator's own noise
    # and aliasing through. Measured on the guitar patch (fundamental 74 Hz):
    #   cutoff  4435 Hz -> centroid  662 Hz, harmonic 0.971
    #   cutoff  7040 Hz -> centroid  952 Hz, harmonic 0.884
    #   cutoff 11175 Hz -> centroid 1321 Hz, harmonic 0.774
    # so the useful ceiling is where harmonicity is still above ~0.85.
    # Note the oscillator cannot reach the stem's 1865 Hz centroid at all: a
    # filter removes content, it cannot add harmonics the oscillator lacks.
    # This cap keeps the patch musical instead of chasing an unreachable target.
    "cutoff_ceiling_vs_fundamental": 120.0,
    # --- filter envelope ----------------------------------------------------
    # attack / sustain brightness contrast that starts to justify filter env mod
    "brightness_ratio_envmod": 1.25,
    "envmod_max_semitones": 36.0,
    "envmod_gain": 1.5,          # semitones per doubling of the contrast
    # The template's own env-mod is kept unless the measurement clearly
    # disagrees; overwriting it with 0 removes the pluck/attack character that
    # makes a piano or a guitar sound struck rather than blown.
    "envmod_keep_ratio": 0.5,    # keep template envmod when >= this × designed
    # --- resonance ----------------------------------------------------------
    "resonance_min": 0.05,
    "resonance_max": 0.45,
    "resonance_gain": 0.22,      # added per unit of harmonic_ratio
    # --- amp envelope clamps (seconds) --------------------------------------
    "attack_s_min": 0.0039, "attack_s_max": 4.0,
    "decay_s_min": 0.02, "decay_s_max": 8.0,
    "release_s_min": 0.03, "release_s_max": 8.0,
    # A measured attack is a lower bound on the real attack: onset detection
    # fires after the rise has begun, so the averaged profile under-reports it.
    # Never design an attack faster than this, or every struck instrument gets
    # an instant-on envelope and loses its transient.
    "attack_floor_ms": 8.0,
    # per-tendency envelopes: (sustain lo, sustain hi, decay lo, decay hi,
    #                          release lo, release hi)
    "env_sustained": (0.35, 0.95, 1.0, 8.0, 0.25, 6.0),
    "env_decaying": (0.15, 0.60, 0.05, 4.0, 0.05, 2.0),
    "env_percussive": (0.00, 0.20, 0.02, 1.0, 0.03, 0.8),
    # --- sustain reliability -------------------------------------------------
    # The onset-aligned average envelope measures the level 0.5-1.2 s after an
    # onset. On dense material that window is already full of the *next* notes,
    # so the number describes note density, not how a single note decays: a
    # plucked guitar in a busy mix measures 0.91 while the instrument itself
    # decays to nothing. When the onset rate is high enough that a new note
    # lands inside the sustain window, the measurement is not evidence and the
    # template's own sustain wins.
    "sustain_window_s": 1.2,
    "sustain_max_onset_rate": 0.8,   # notes/s above which sustain is unreliable
    # Even when the measurement is trusted, it may not raise a template's
    # sustain by more than this: a plucked/struck patch must stay plucked.
    "sustain_max_raise": 0.25,
}

# Surge filter types that are a plain lowpass and therefore safe to retune from
# a spectral centroid. Everything else (notch, comb, allpass, bandpass, ...)
# must keep the template's cutoff: writing a "brightness" value into a notch
# filter moves the notch, it does not open the sound up.
LOWPASS_FILTER_TYPES = {1, 2, 3}

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


def design_patch_params(feats, template=None):
    """Map analyzed stem features to Surge XT scene-A patch parameters.

    `feats` is one stem's entry from analysis.json. `template` is the source
    preset's own parameter values (natural units, read from the .fxp); when
    given, the design is constrained by what the template already does:

      * the cutoff may only move `cutoff_max_shift_octaves` away from it, so a
        dark measurement cannot turn a bright patch into a muffled sine;
      * a non-lowpass filter keeps its cutoff entirely (a notch has no
        "brightness" to open up);
      * the template's filter env-mod is kept when it is at least
        `envmod_keep_ratio` of the designed value, preserving the pluck /
        attack character that makes struck instruments sound struck.

    Returns a dict:
        params: {surge param name: natural value}
        notes:  [{param, value, reason}]   audit trail
        features: the feature subset the design used
    Missing features simply skip the dependent decisions (the template value
    stays), so old analysis.json files keep working.
    """
    params = {}
    notes = []
    used = {}
    tpl = template or {}

    env = feats.get("envelope") or {}
    brightness = feats.get("brightness_hz")
    harmonic_ratio = feats.get("harmonic_ratio")

    # ---- 1. amplitude envelope (a_env1_*) ---------------------------------
    tendency = _pick_tendency(env)
    sus_lo, sus_hi, dec_lo, dec_hi, rel_lo, rel_hi = CFG[f"env_{tendency}"]

    if env.get("attack_ms") is not None:
        atk_s = clamp(env["attack_ms"] / 1000.0,
                      CFG["attack_floor_ms"] / 1000.0, CFG["attack_s_max"])
        v = seconds_to_envtime_value(atk_s)
        params["a_env1_attack"] = v
        used["attack_ms"] = env["attack_ms"]
        notes.append(_note(
            "a_env1_attack", v,
            f"起音 {env['attack_ms']:.0f}ms → {atk_s * 1000:.0f}ms"
            f"（下限 {CFG['attack_floor_ms']:.0f}ms，Surge envtime=log2(秒)）"))

    if env.get("decay_ms") is not None:
        dec_s = clamp(env["decay_ms"] / 1000.0, dec_lo, dec_hi)
        reason = f"衰减 {env['decay_ms']:.0f}ms，倾向「{tendency}」→ {dec_s * 1000:.0f}ms"
        # The tendency range is a sanity band, not evidence. When the measured
        # decay is clearly shorter than the band's floor, the measurement wins:
        # stretching a 255 ms piano decay to 1 s turns a struck note into a
        # held one. Only the global bounds still apply.
        measured = env["decay_ms"] / 1000.0
        if measured < dec_lo:
            dec_s = clamp(measured, CFG["decay_s_min"], CFG["decay_s_max"])
            reason = (f"衰减 {env['decay_ms']:.0f}ms 短于「{tendency}」下限 "
                      f"{dec_lo * 1000:.0f}ms，按实测 → {dec_s * 1000:.0f}ms")
        v = seconds_to_envtime_value(dec_s)
        params["a_env1_decay"] = v
        used["decay_ms"] = env["decay_ms"]
        notes.append(_note("a_env1_decay", v, reason))

    if env.get("sustain_level") is not None:
        sus = clamp(env["sustain_level"], sus_lo, sus_hi)
        reason = (f"持续电平 {env['sustain_level']:.2f}（相对峰值），"
                  f"倾向「{tendency}」→ {sus:.2f}")
        onset_rate = feats.get("onset_rate") or 0.0
        tpl_sus = tpl.get("a_env1_sustain")
        if onset_rate > CFG["sustain_max_onset_rate"]:
            # A new note lands inside the sustain window, so the number is
            # note density, not decay. Keep the template's own sustain.
            if tpl_sus is not None:
                sus = tpl_sus
                reason = (f"持续电平 {env['sustain_level']:.2f} 不可信"
                          f"（音符密度 {onset_rate:.1f}/s，持续窗内已有下一个音）"
                          f"→ 保留模板值 {tpl_sus:.2f}")
            else:
                reason += f"（音符密度 {onset_rate:.1f}/s，该值可能偏高）"
        elif tpl_sus is not None and sus > tpl_sus + CFG["sustain_max_raise"]:
            # Trusted measurement, but a plucked/struck patch must stay plucked:
            # raising sustain past this turns a guitar into an organ.
            capped = tpl_sus + CFG["sustain_max_raise"]
            reason += (f"；模板 {tpl_sus:.2f}，最多上调 "
                       f"{CFG['sustain_max_raise']:.2f} → {capped:.2f}")
            sus = capped
        params["a_env1_sustain"] = clamp(sus, *PERCENT_RANGE)
        used["sustain_level"] = env["sustain_level"]
        notes.append(_note("a_env1_sustain", sus, reason))

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
    # Only a lowpass has a cutoff that means "how bright". A notch / comb /
    # allpass template must keep its own cutoff: writing a brightness value
    # into it moves the notch and mangles the timbre instead of opening it.
    filter_type = tpl.get("a_filter1_type")
    tpl_cutoff = tpl.get("a_filter1_cutoff")
    if brightness and (filter_type is None or int(filter_type) in LOWPASS_FILTER_TYPES):
        # Relative brightness (centroid / fundamental) is the timbre; the
        # absolute centroid only says how high the part is played.
        reg = feats.get("register_midi")
        if reg is not None:
            fundamental = 440.0 * 2.0 ** ((float(reg) - 69.0) / 12.0)
            basis = f"基频 {fundamental:.0f}Hz"
        else:
            fundamental = CFG["fallback_fundamental_hz"]
            basis = f"无基频，按 {fundamental:.0f}Hz 计"
        relative = float(brightness) / fundamental
        fc = clamp(fundamental * relative / 0.18,
                   CFG["cutoff_hz_min"], CFG["cutoff_hz_max"])
        reason = (f"相对亮度 {relative:.2f}×（谱质心 {float(brightness):.0f}Hz / "
                  f"{basis}）÷ 0.18 → 低通 {fc:.0f}Hz"
                  f"（{12 * math.log2(fc / 440):.2f} 半音 @A4）")
        # Never put the cutoff under the note's own fundamental: a lowpass below
        # it removes the note instead of shaping it.
        floor_hz = fundamental * CFG["cutoff_floor_vs_fundamental"]
        if fc < floor_hz:
            reason += (f"；下限 {CFG['cutoff_floor_vs_fundamental']:.1f}× 基频 "
                       f"→ {floor_hz:.0f}Hz")
            fc = floor_hz
        # And never open it so far that the oscillator's noise dominates.
        ceil_hz = fundamental * CFG["cutoff_ceiling_vs_fundamental"]
        if fc > ceil_hz:
            reason += (f"；上限 {CFG['cutoff_ceiling_vs_fundamental']:.0f}× 基频 "
                       f"→ {ceil_hz:.0f}Hz（再高会放出振荡器噪声）")
            fc = ceil_hz
        v = hz_to_cutoff_value(fc)
        if tpl_cutoff is not None:
            lo = tpl_cutoff - CFG["cutoff_max_shift_octaves"] * 12.0
            hi = tpl_cutoff + CFG["cutoff_max_shift_octaves"] * 12.0
            clamped = clamp(v, lo, hi)
            if abs(clamped - v) > 1e-9:
                reason += (f"；模板 cutoff {tpl_cutoff:.1f} 半音，限制在 ±"
                           f"{CFG['cutoff_max_shift_octaves']:.1f} 八度内 → {clamped:.1f}")
                v = clamped
        params["a_filter1_cutoff"] = v
        used["brightness_hz"] = brightness
        notes.append(_note("a_filter1_cutoff", v, reason))
    elif brightness and filter_type is not None:
        notes.append(_note(
            "a_filter1_cutoff", tpl_cutoff if tpl_cutoff is not None else 0.0,
            f"模板滤波器类型 {int(filter_type)} 不是低通（notch/comb/allpass 等），"
            f"cutoff 不表示亮度，保留模板值"))

    # ---- 4. filter env modulation (a_filter1_envmod) ----------------------
    atk_b = feats.get("brightness_attack_hz")
    sus_b = feats.get("brightness_sustain_hz")
    tpl_envmod = tpl.get("a_filter1_envmod")
    if atk_b and sus_b and sus_b > 0:
        contrast = float(atk_b) / float(sus_b)
        used["brightness_contrast"] = round(contrast, 3)
        if contrast >= CFG["brightness_ratio_envmod"]:
            semis = clamp(12.0 * math.log2(contrast) * CFG["envmod_gain"],
                          0.0, CFG["envmod_max_semitones"])
        else:
            semis = 0.0
        reason = (f"起音亮度 {float(atk_b):.0f}Hz / 持续亮度 {float(sus_b):.0f}Hz "
                  f"= {contrast:.2f}× → 滤波包络 +{semis:.1f} 半音")
        # The template's env-mod is the sound designer's attack character. A
        # measured contrast of ~1.1-1.2× is normal for any sustained note and
        # does not justify deleting it — doing so is what turned the electric
        # piano into a flute.
        if (tpl_envmod is not None and abs(tpl_envmod) > 0
                and abs(tpl_envmod) >= CFG["envmod_keep_ratio"] * abs(semis)):
            semis = tpl_envmod
            reason += f"；模板已有 {tpl_envmod:.1f} 半音，保留（起音特征）"
        params["a_filter1_envmod"] = clamp(semis, *ENVMOD_VALUE_RANGE)
        notes.append(_note("a_filter1_envmod", semis, reason))

    # ---- 5. resonance (a_filter1_resonance) -------------------------------
    # Resonance only makes sense on a filter that has a cutoff peak. On a notch
    # or comb it just adds a whistle, so the template value stays.
    if harmonic_ratio is not None and (
            filter_type is None or int(filter_type) in LOWPASS_FILTER_TYPES):
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
