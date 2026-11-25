"""Unified MIR + polyphony + vocal-contour + LTAS-masking analysis (`improvements.md`
Stage 1 / Phase 1). Writes ``workspace/analysis/<song>/song_profile.json`` and
``analysis.md``.

Replaces the ad-hoc scattering of analysis across the pipeline with one profile
that the DSH layer and ``melody_orchestrator`` read. Deterministic, no LLM.

Per active stem (bass/guitar/piano/other, plus structural-only vocals):
    activity, rms_db, dumped_segments, usable
    polyphony : avg_simultaneous_notes, monophonic_ratio, clean_mono_note_count,
                clean_windows[] (t0, dur, f0, snr)
    timbre    : register_midi, f0_range_midi, centroid_hz, rolloff85_hz,
                harmonic_ratio, flatness, octave_band_db (8), mfcc_mean,
                rt60_estimate_s
    envelope  : attack_ms, decay_ms, sustain_level, release_ms, sustain_tendency
vocal_melody_profile (vocals active): active_share, register_midi, range_midi,
    mean_note_dur_s, legato_ratio, onset_rate, vibrato_cents
backing_ltas : 1/3-octave profile of non-vocal melodic stems +
    open_spectral_pockets_hz (bands >= 6 dB below peak for a clean RH lead).

Run on Python 3.14 with librosa/scipy only (basic-pitch optional).
"""
import argparse
import datetime
import glob
import json
import os
import sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _cache_env  # noqa: E402
import tone_common as tc  # noqa: E402

SR = tc.SR
ACTIVE_DB = -45.0
DERIVED = {"instrumental"}
OPEN_POCKET_DB = 6.0          # a pocket must sit this far below the LTAS peak


# ---------------------------------------------------------------- per-stem

def _envelope(y, sr, onsets):
    import librosa
    hop = tc.HOP
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=hop)[0]
    if len(onsets) < 3:
        return None
    pre = int(0.15 * sr / hop); post = int(1.5 * sr / hop)
    segs = []
    for t0 in onsets:
        i0 = int(t0 * sr / hop); a, b = i0 - pre, i0 + post
        if a < 0 or b > len(rms):
            continue
        seg = rms[a:b]; pk = seg.max()
        if pk <= 1e-6:
            continue
        segs.append(seg / pk)
    if len(segs) < 3:
        return None
    avg = np.mean(np.vstack(segs), axis=0)
    peak_i = int(np.argmax(avg[:max(1, int(0.5 * sr / hop))]))
    peak = float(avg[peak_i])
    i10 = next((i for i in range(peak_i + 1) if avg[i] >= 0.1 * peak), 0)
    i90 = next((i for i in range(i10, len(avg)) if avg[i] >= 0.9 * peak), i10)
    attack_ms = (i90 - i10) * hop / sr * 1000.0
    dec_i = next((i for i in range(peak_i + 1, len(avg)) if avg[i] <= 0.7 * peak), None)
    decay_ms = ((dec_i - peak_i) if dec_i is not None else (len(avg) - peak_i)) * hop / sr * 1000.0
    s0 = peak_i + int(0.5 * sr / hop); s1 = min(peak_i + int(1.2 * sr / hop), len(avg))
    sustain = float(np.median(avg[s0:s1]) / peak) if s1 - s0 >= 3 else float(avg[-1] / peak)
    tendency = ("percussive" if sustain < 0.2 else
                "decaying" if sustain < 0.55 else "sustained")
    return {"attack_ms": round(attack_ms, 1), "decay_ms": round(decay_ms, 1),
            "sustain_level": round(sustain, 3), "release_ms": None,
            "sustain_tendency": tendency}


def _polyphony(y, sr, onsets):
    """avg_simultaneous_notes + monophonic_ratio + clean_mono_note_count/windows."""
    import librosa
    y_h, _ = librosa.effects.hpss(y)
    chroma = librosa.feature.chroma_cqt(y=y_h, sr=sr, hop_length=tc.HOP)
    with np.errstate(invalid="ignore"):
        mx = chroma.max(axis=0)
        classes = ((chroma >= 0.5 * mx) & (mx > 0.1)).sum(axis=0)
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=tc.HOP)[0]
    active = 20 * np.log10(rms + 1e-12) > ACTIVE_DB
    avg_notes = float(classes[active].mean()) if active.any() else 0.0
    mono_ratio = float((classes[active] <= 1).mean()) if active.any() else 0.0

    # Clean monophonic windows: 0.5 s windows with one pitch class, high SNR.
    clean = []
    for i in range(0, max(1, len(rms) - int(0.5 * sr / tc.HOP)), int(0.5 * sr / tc.HOP)):
        seg = classes[i:i + int(0.5 * sr / tc.HOP)]
        if len(seg) < 4 or float(seg.mean()) > 1.3:
            continue
        s0 = i * tc.HOP; s1 = s0 + int(0.5 * sr)
        f0 = tc.estimate_f0(y[s0:s1], sr)
        if f0:
            clean.append({"t0": round(s0 / sr, 2), "dur": 0.5,
                          "f0": round(f0, 1)})
    return {"avg_simultaneous_notes": round(avg_notes, 2),
            "monophonic_ratio": round(mono_ratio, 3),
            "clean_mono_note_count": len(clean),
            "clean_windows": clean[:40]}


def _timbre(y, sr):
    import librosa
    y_h, _ = librosa.effects.hpss(y)
    cent = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=tc.HOP)[0]
    roll = librosa.feature.spectral_rolloff(y=y, sr=sr, hop_length=tc.HOP,
                                            roll_percent=0.85)[0]
    flat = librosa.feature.spectral_flatness(y=y, hop_length=tc.HOP)[0]
    e_h = float(np.sqrt((y_h ** 2).mean()))
    e_p = float(np.sqrt(((y - y_h) ** 2).mean()))
    f0, voiced, _ = librosa.pyin(y_h, fmin=55.0, fmax=1200.0, sr=sr,
                                 frame_length=4096, hop_length=tc.HOP)
    reg = None; rng = None
    if f0 is not None and np.any(voiced):
        vals = f0[voiced & np.isfinite(f0)]
        if vals.size:
            reg = float(librosa.hz_to_midi(np.median(vals)))
            lo, hi = np.percentile(vals, [10, 90])
            rng = [round(float(librosa.hz_to_midi(lo)), 1),
                   round(float(librosa.hz_to_midi(hi)), 1)]
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=5)
    return {
        "register_midi": round(reg, 1) if reg else None,
        "f0_range_midi": rng,
        "centroid_hz": round(float(np.median(cent[cent > 0])), 0) if (cent > 0).any() else None,
        "rolloff85_hz": round(float(np.median(roll[roll > 0])), 0) if (roll > 0).any() else None,
        "harmonic_ratio": round(e_h ** 2 / (e_h ** 2 + e_p ** 2 + 1e-12), 3),
        "flatness": round(float(np.median(flat)), 5),
        "octave_band_db": [round(v, 1) for v in tc.octave_band_ltas(y, sr)],
        "mfcc_mean": [round(float(v), 2) for v in np.median(mfcc, axis=1)],
        "rt60_estimate_s": None,
    }


def analyze_stem(path):
    y, sr = tc.load_audio(path, sr=SR, mono=True)
    if len(y) == 0:
        return {"empty": True}
    import librosa
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=tc.HOP)[0]
    active = 20 * np.log10(rms + 1e-12) > ACTIVE_DB
    if not active.any():
        return {"empty": True, "duration": round(len(y) / sr, 1)}
    onsets = librosa.onset.onset_detect(y=y, sr=sr, units="time", hop_length=tc.HOP)
    feats = {
        "empty": False,
        "duration": round(len(y) / sr, 1),
        "activity": round(float(active.mean()), 3),
        "rms_db": round(float(20 * np.log10(np.sqrt((y ** 2).mean()) + 1e-12)), 1),
    }
    feats["usable"] = feats["activity"] >= 0.05
    feats["polyphony"] = _polyphony(y, sr, onsets)
    feats["timbre"] = _timbre(y, sr)
    feats["envelope"] = _envelope(y, sr, onsets)
    return feats


def vocal_profile(path):
    """Structural contour of vocals.wav ONLY (never sampled)."""
    y, sr = tc.load_audio(path, sr=SR, mono=True)
    import librosa
    y_h, _ = librosa.effects.hpss(y)
    f0, voiced, _ = librosa.pyin(y_h, fmin=65.0, fmax=1100.0, sr=sr,
                                 frame_length=4096, hop_length=tc.HOP)
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=tc.HOP)[0]
    active = 20 * np.log10(rms + 1e-12) > ACTIVE_DB
    active_share = float(active.mean())
    if f0 is None or not np.any(voiced):
        return {"active_share": round(active_share, 3)}
    vals = f0[voiced & np.isfinite(f0)]
    midi = librosa.hz_to_midi(vals)
    # Note durations + legato from the voiced mask runs.
    runs, cur = [], 0
    for vv in voiced:
        if vv:
            cur += 1
        elif cur:
            runs.append(cur * tc.HOP / sr); cur = 0
    if cur:
        runs.append(cur * tc.HOP / sr)
    mean_dur = float(np.mean(runs)) if runs else None
    # Legato: share of adjacent voiced frames with small pitch step.
    steps = np.abs(np.diff(midi))
    legato = float((steps < 1.0).mean()) if steps.size else 0.0
    # Vibrato: median absolute f0 wobble in voiced runs (cents).
    vib = float(np.median(np.abs(np.diff(midi)))) * 100 if steps.size else 0.0
    onsets = librosa.onset.onset_detect(y=y, sr=sr, units="time", hop_length=tc.HOP)
    return {
        "active_share": round(active_share, 3),
        "register_midi": round(float(np.median(midi)), 1),
        "range_midi": [round(float(np.percentile(midi, 10)), 1),
                       round(float(np.percentile(midi, 90)), 1)],
        "mean_note_dur_s": round(mean_dur, 2) if mean_dur else None,
        "legato_ratio": round(legato, 3),
        "onset_rate": round(len(onsets) / max(len(y) / sr, 1e-9), 2),
        "vibrato_cents": round(vib, 1),
    }


def backing_ltas(stem_feats):
    """1/3-octave profile of non-vocal melodic stems + open spectral pockets."""
    bands = []
    for name, s in stem_feats.items():
        if name in ("vocals", "drums") or s.get("empty") or not s.get("usable"):
            continue
        ob = (s.get("timbre") or {}).get("octave_band_db")
        if ob:
            bands.append(ob)
    if not bands:
        return {"profile_db": [], "open_spectral_pockets_hz": []}
    profile = np.mean(np.array(bands), axis=0)
    peak = float(profile.max())
    pockets = [tc.OCTAVE_BANDS_HZ[i] for i in range(len(profile))
               if peak - profile[i] >= OPEN_POCKET_DB]
    return {"profile_db": [round(float(v), 1) for v in profile],
            "open_spectral_pockets_hz": pockets}


# ---------------------------------------------------------------- build

def build_song_profile(song, stem_dir):
    stems = {}
    seen = {}
    for ext in ("*.flac", "*.wav"):
        for p in sorted(glob.glob(os.path.join(stem_dir, ext))):
            name = os.path.splitext(os.path.basename(p))[0]
            if name in DERIVED or name in seen:
                continue
            seen[name] = p
    for name, p in sorted(seen.items()):
        stems[name] = analyze_stem(p)
    vpath = next((os.path.join(stem_dir, f"vocals{ext}")
                  for ext in (".flac", ".wav")
                  if os.path.isfile(os.path.join(stem_dir, f"vocals{ext}"))), None)
    vp = vocal_profile(vpath) if vpath else {}
    profile = {
        "song": song,
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "stems_dir": stem_dir,
        "stems": stems,
        "vocal_melody_profile": vp,
        "backing_ltas": backing_ltas(stems),
    }
    return profile


def build_report(profile):
    lines = [f"# 逐曲分析档案 — {profile['song']}", "",
             f"生成时间：{profile['generated']}　工具：`scripts/analyze_song.py`", "",
             "## 各轨", "", "| 轨 | 可用 | 同时音数 | 单音占比 | 干净单音数 | 音区 | 谐波占比 |",
             "| --- | --- | --- | --- | --- | --- | --- | |"]
    for name, s in profile["stems"].items():
        if s.get("empty"):
            lines.append(f"| {name} | — | — | — | — | — | — |")
            continue
        t = s.get("timbre") or {}; poly = s.get("polyphony") or {}
        lines.append(f"| {name} | {'是' if s.get('usable') else '否'} "
                     f"| {poly.get('avg_simultaneous_notes')} "
                     f"| {poly.get('monophonic_ratio')} "
                     f"| {poly.get('clean_mono_note_count')} "
                     f"| {t.get('register_midi')} | {t.get('harmonic_ratio')} |")
    vp = profile.get("vocal_melody_profile") or {}
    if vp:
        lines += ["", "## 人声结构（仅分析，不采样）", ""]
        lines.append(f"- 活跃占比 {vp.get('active_share')}，音区 {vp.get('register_midi')}，"
                     f"平均音长 {vp.get('mean_note_dur_s')}s，连音比 {vp.get('legato_ratio')}，"
                     f"颤音 {vp.get('vibrato_cents')} cents")
    ltas = profile.get("backing_ltas") or {}
    if ltas.get("open_spectral_pockets_hz"):
        lines += ["", f"## 右手空闲频段：{ltas['open_spectral_pockets_hz']} Hz"]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stems", default="", help="stem directory")
    ap.add_argument("--song", default="", help="song folder name substring")
    ap.add_argument("--out", default=os.path.join(tc.WORKSPACE, "analysis"))
    args = ap.parse_args()
    stem_dir = args.stems
    if not stem_dir:
        root = os.path.join(tc.WORKSPACE, "stems")
        cands = [d for d in glob.glob(os.path.join(root, "*")) if os.path.isdir(d)]
        if args.song:
            cands = [d for d in cands if args.song in os.path.basename(d)]
        stem_dir = max(cands, key=os.path.getmtime)
    song = os.path.basename(os.path.normpath(stem_dir))
    profile = build_song_profile(song, stem_dir)
    out_dir = os.path.join(args.out, song)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "song_profile.json"), "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "analysis.md"), "w", encoding="utf-8") as f:
        f.write(build_report(profile))
    print(f"song: {song}")
    print(f"wrote {os.path.join(out_dir, 'song_profile.json')}")


if __name__ == "__main__":
    main()
