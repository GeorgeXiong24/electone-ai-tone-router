"""Right-hand main-melody selection (`improvements.md` Stage 3).

Implements the requirement: *never use the singer's vocal timbre; analyse the
main melody and pick/craft a blending voice; default strongly to Piano on most
songs and fall back to Piano whenever blending fails.*

Consumes ``song_profile.json`` (written by ``analyze_song.py``) and produces a
right-hand instrument decision + a 2-sentence rationale the DSH layer shows to
the user (who can override in one phrase). Pure decision logic - no DSP.

Step 1 - four explicit MIR decision features
--------------------------------------------
  has_instrumental_lead : a non-vocal stem already carries a high-confidence
      monophonic top line (voiced_ratio > 0.5, stepwise > 0.85, high_band_share
      > 0.15) while vocals are inactive (vocal_active_share < 0.15).
  vocal_sustain_index   : from vocals.wav (structural only, NEVER sampled):
      mean_note_dur_s + legato_ratio. Short/syllabic -> struck/plucked clarity
      (Piano); long/sustained -> piano decays out, a sustained lead blends better.
  backing_world         : acoustic band vs piano/orchestral ballad vs electronic.
  open_spectral_pockets : backing LTAS bands >= 6 dB below peak where a right-hand
      lead sits cleanly without masking the Lower keyboard.

Step 2 - orchestration prior + decision hierarchy (in order)
-----------------------------------------------------------
  1. Instrumental / clear solo lead      -> Upper = that instrument (Route A/B).
  2. Vocal pop / acoustic / ballad (DEFAULT) -> Piano (Salamander sfizz) with
     Match EQ + warmth tuned to open_spectral_pockets_hz.
  3. Sustained legato vocal over sparse backing, or electronic/synth mix -> DSH
     may craft a blending lead (Vital / Surge) tuned into the open pockets.
  4. Hard quality fallback gate: any custom lead must pass render_and_eval +
     mix-blend; on failure -> Piano.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CFG = {
    "has_lead_voiced_ratio": 0.5,
    "has_lead_stepwise": 0.85,
    "has_lead_high_band": 0.15,
    "vocal_inactive_share": 0.15,
    # vocal_sustain_index thresholds (mean_note_dur_s / legato_ratio).
    "syllabic_note_dur_s": 0.55,
    "sustained_note_dur_s": 0.65,
    "sustained_legato": 0.65,
    # Default prior strength: how readily we reach for Piano.
    "piano_default": True,
}


def _vocal_profile(profile):
    v = profile.get("vocal_melody_profile") or {}
    return {
        "active_share": float(v.get("active_share", 0.0)),
        "mean_note_dur_s": v.get("mean_note_dur_s"),
        "legato_ratio": v.get("legato_ratio"),
        "register_midi": v.get("register_midi"),
    }


def _has_instrumental_lead(profile):
    v = _vocal_profile(profile)
    if v["active_share"] >= CFG["vocal_inactive_share"]:
        return False, None
    best = None
    for name, s in (profile.get("stems") or {}).items():
        if name in ("vocals", "drums") or s.get("unusable"):
            continue
        mel = s.get("melody") or s.get("polyphony") or {}
        voiced = float(mel.get("voiced_ratio", s.get("voiced_ratio", 0)))
        step = float(mel.get("step_lt2_semitones", 0))
        hb = float(mel.get("high_band_share", 0))
        if (voiced > CFG["has_lead_voiced_ratio"]
                and step > CFG["has_lead_stepwise"]
                and hb > CFG["has_lead_high_band"]):
            score = voiced * step * hb
            if best is None or score > best[0]:
                best = (score, name)
    return (best is not None), (best[1] if best else None)


def _vocal_sustain_index(v):
    dur = v.get("mean_note_dur_s")
    leg = v.get("legato_ratio") or 0.0
    if dur is None:
        return "unknown"
    if dur < CFG["syllabic_note_dur_s"]:
        return "syllabic"
    if dur >= CFG["sustained_note_dur_s"] and leg >= CFG["sustained_legato"]:
        return "sustained"
    return "mixed"


def _backing_world(profile):
    """Classify the mix: acoustic / ballad / electronic."""
    synth = 0.0
    acoustic = 0.0
    for name, s in (profile.get("stems") or {}).items():
        if name in ("vocals", "drums") or s.get("unusable"):
            continue
        sus = (s.get("envelope") or {}).get("sustain_tendency", "")
        harm = float(s.get("harmonic_ratio", 0.5))
        if sus == "sustained" and harm > 0.7:
            synth += 1.0
        elif name in ("guitar", "piano", "bass"):
            acoustic += 1.0
    if synth > acoustic:
        return "electronic"
    if acoustic >= 2:
        return "acoustic_band"
    return "ballad"


def recommend_right_hand(profile):
    """Return the right-hand decision dict for a song_profile.

    Keys: instrument, engine, rationale (2 sentences), route, fallback_triggered,
          features (the four MIR decision features), open_spectral_pockets_hz.
    """
    v = _vocal_profile(profile)
    lead, lead_stem = _has_instrumental_lead(profile)
    vsi = _vocal_sustain_index(v)
    world = _backing_world(profile)
    pockets = profile.get("backing_ltas", {}).get("open_spectral_pockets_hz", [])

    features = {
        "has_instrumental_lead": lead,
        "instrumental_lead_stem": lead_stem,
        "vocal_sustain_index": vsi,
        "backing_world": world,
        "open_spectral_pockets_hz": pockets,
        "vocal_active_share": v["active_share"],
    }

    # Decision hierarchy (Section 6 Stage 3, Step 2).
    if lead and v["active_share"] < CFG["vocal_inactive_share"]:
        return {
            "instrument": lead_stem, "engine": "sfizz_anchor", "route": "A",
            "rationale": (
                f"歌曲是器乐/该轨已带清晰单音主旋律（{lead_stem}），右手直接用它的"
                f"逐曲定制音色最贴合原曲。左手换另一伴奏轨并做音区/亮度区分，避免双手相撞。"),
            "fallback_triggered": False, "features": features,
        }

    if vsi == "sustained" and world in ("electronic", "ballad"):
        return {
            "instrument": "blending_lead", "engine": "vital_wavetable", "route": "C",
            "rationale": (
                "人声长音连贯、伴奏偏电子/抒情，钢琴长音会衰减掉，"
                "因此尝试合成一支贴合空闲频段的融合主音（Vital/Surge）。"),
            "fallback_triggered": False, "features": features,
        }

    # Default prior: vocal pop / mandopop / acoustic / ballad -> Piano.
    return {
        "instrument": "piano", "engine": "piano_sfizz", "route": "B",
        "rationale": (
            "这是流行/原声乐队/抒情曲，按默认先验右手用钢琴（Accurate-Salamander，"
            "逐曲 Match EQ + 亮度/空间），能干净贴合伴奏不遮蔽。"
            "若试听融合不佳会自动回退到钢琴。"),
        "fallback_triggered": False, "features": features,
    }


def apply_quality_fallback(reco, eval_result):
    """Hard quality gate (Stage 3 Step 4): any custom lead that fails render_and_eval
    / mix-blend automatically falls back to Piano."""
    if reco["engine"] == "piano_sfizz":
        return reco, False
    fails = eval_result.get("fails") or []
    if not eval_result.get("pass", True) and fails:
        reco = dict(reco)
        reco["instrument"] = "piano"
        reco["engine"] = "piano_sfizz"
        reco["route"] = "B"
        reco["fallback_triggered"] = True
        reco["rationale"] = (
            "自定义融合主音未通过质量门（" + "、".join(fails) + "），"
            "已自动回退到钢琴。")
        return reco, True
    return reco, False
