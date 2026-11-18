"""Analyze separated stems: instrument identity, timbre traits, musical role.

For every stem of a song this reports:
  - what the instrument is (cross-checked against the separator's label)
  - how it sounds (brightness, attack, sustain, register, harmonic content)
  - what it does in the arrangement: melody / counter-melody / chords /
    accompaniment / pad / bass line / rhythm, and which ELB-02 keyboard that
    maps to (right hand / left hand / foot)

Roles are decided on 5-second windows (monophonic melody shape vs. polyphonic
chords vs. onset-dense strumming), then aggregated per stem. A stem can carry
two roles (e.g. piano intro melody + chord comping); both are reported with
their time share.

The report feeds build-tones.py, which writes one single-instrument tone file
per (stem, keyboard) pair under workspace/tone/.

Usage:
  python analyze-tones.py --stems <song stem dir> [--out <analysis dir>]
  python analyze-tones.py --latest
  python analyze-tones.py --song <title substring>

Writes <out>/<song>/analysis.json and analysis.md (default out: workspace/analysis).
"""
import argparse
import datetime
import glob
import json
import os
import sys

import numpy as np
import soundfile as sf

import librosa

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.environ.get("ELECTONE_WORKSPACE") or os.path.join(ROOT, "workspace")

# Stems derived from the mix rather than predicted by the model.
DERIVED = {"instrumental"}

# A stem quieter than this (frame RMS) is treated as silent.
ACTIVE_DB = -45.0

WIN_SEC = 5.0
HOP_SEC = 2.5

ROLE_MELODY = "主旋律"
ROLE_COUNTER = "副旋律"
ROLE_CHORDS = "和弦"
ROLE_ACCOMP = "伴奏"
ROLE_PAD = "和声铺底"
ROLE_BASS = "低音线条"
ROLE_RHYTHM = "节奏"
ROLE_SPARSE = "稀疏/点缀"

KEYBOARD_OF_ROLE = {
    ROLE_MELODY: "右手",
    ROLE_COUNTER: "左手",
    ROLE_CHORDS: "左手",
    ROLE_ACCOMP: "左手",
    ROLE_PAD: "左手",
    ROLE_BASS: "脚",
    ROLE_RHYTHM: "",
    ROLE_SPARSE: "",
}

INSTRUMENT_ZH = {
    "vocals": "人声",
    "drums": "鼓组",
    "bass": "贝斯",
    "guitar": "吉他",
    "piano": "钢琴",
    "other": "其他/混合",
    "kick": "底鼓",
    "snare": "军鼓",
    "toms": "嗵鼓",
    "hh": "踩镲",
    "ride": "叮叮镲",
    "crash": "碎镲",
}

CHORD_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def db(v):
    return 20.0 * np.log10(v + 1e-12)


def midi_name(m):
    if not np.isfinite(m):
        return "-"
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    m = int(round(m))
    return f"{names[m % 12]}{m // 12 - 1}"


def label_of(stem):
    return INSTRUMENT_ZH.get(stem, stem)


# ---------------------------------------------------------------- features

def analyze_stem(path, quick=False):
    """Return a feature dict for one stem wav."""
    y, sr = librosa.load(path, sr=22050, mono=True)
    if len(y) == 0:
        return {"empty": True}

    hop = 512
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=hop)[0]
    rms_db = 20.0 * np.log10(rms + 1e-12)
    active = rms_db > ACTIVE_DB
    if not active.any():
        return {"empty": True, "duration": len(y) / sr}

    centroid = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=hop)[0]
    rolloff = librosa.feature.spectral_rolloff(y=y, sr=sr, hop_length=hop,
                                              roll_percent=0.85)[0]

    y_h, y_p = librosa.effects.hpss(y)
    e_h = float(np.sqrt((y_h ** 2).mean()))
    e_p = float(np.sqrt((y_p ** 2).mean()))
    harmonic_ratio = e_h ** 2 / (e_h ** 2 + e_p ** 2 + 1e-12)

    onsets = librosa.onset.onset_detect(y=y, sr=sr, units="time", hop_length=hop)
    duration = len(y) / sr
    onset_rate = float(len(onsets) / max(duration, 1e-9))
    feats = {
        "empty": False,
        "duration": round(duration, 1),
        "activity": round(float(active.mean()), 3),
        "rms_db": round(float(db(np.sqrt((y ** 2).mean()))), 1),
        "brightness_hz": round(float(np.median(centroid[active])), 0),
        "rolloff85_hz": round(float(np.median(rolloff[active])), 0),
        "harmonic_ratio": round(harmonic_ratio, 3),
        "percussive_ratio": round(1.0 - harmonic_ratio, 3),
        "onset_rate": round(onset_rate, 2),
    }

    if quick:
        # Percussive/refined stems: no pitch work needed.
        return feats

    # Pitch tracking on the harmonic component (better for polyphonic music).
    f0, voiced, _ = librosa.pyin(
        y_h, fmin=55.0, fmax=1200.0, sr=sr,
        frame_length=2048, hop_length=hop,
    )
    chroma = librosa.feature.chroma_cqt(y=y_h, sr=sr, hop_length=hop)

    feats["register_midi"] = None
    feats["f0_range"] = None
    feats["voiced_ratio"] = round(float(np.mean(voiced[active])) if active.any() else 0.0, 3)
    if voiced.any():
        f0_v = f0[voiced]
        lo, hi = np.percentile(f0_v, [10, 90])
        med = float(np.median(f0_v))
        feats["register_midi"] = round(float(librosa.hz_to_midi(med)), 1)
        feats["f0_range"] = [round(float(librosa.hz_to_midi(lo)), 1),
                             round(float(librosa.hz_to_midi(hi)), 1)]

    # Simultaneous pitch classes per frame -> polyphony hint.
    with np.errstate(invalid="ignore"):
        frame_max = chroma.max(axis=0)
        classes = ((chroma >= 0.5 * frame_max) & (frame_max > 0.1)).sum(axis=0)
    feats["avg_pitch_classes"] = round(float(classes[active].mean()), 2)

    # A piano stem carries both the accompaniment (low register) and often the
    # melody (high register). pyin follows the loudest fundamental, which is
    # almost always the bass note, so the melody is invisible to it. Track the
    # high-passed signal separately: that is the line a right hand would play.
    feats["melody"] = analyze_melody_register(y, sr, hop, active)

    feats["_frames"] = {
        "hop": hop, "sr": sr, "rms_db": rms_db, "voiced": voiced,
        "classes": classes, "f0": f0, "chroma": chroma, "onsets": onsets,
    }
    return feats


def analyze_melody_register(y, sr, hop, active, split_hz=262.0):
    """Track the melody line above `split_hz` (default C4).

    Returns a dict describing whether a usable monophonic line exists up
    there, or None when the stem has no high-register content.
    """
    import scipy.signal

    sos = scipy.signal.butter(4, split_hz / (sr / 2), "highpass", output="sos")
    y_hi = scipy.signal.sosfiltfilt(sos, y)

    # A high-passed bass stem still has harmonics up there, and pyin happily
    # locks onto them. Require the high band to carry real energy relative to
    # the whole stem before believing any of it.
    band_energy = float(np.sum(y_hi ** 2))
    total_energy = float(np.sum(y ** 2)) + 1e-12
    if band_energy / total_energy < 0.05:
        return None

    f0, voiced, _ = librosa.pyin(
        y_hi, fmin=split_hz * 0.75, fmax=2000.0, sr=sr,
        frame_length=2048, hop_length=hop,
    )
    if not voiced.any():
        return None

    f0_v = f0[voiced]
    midi = librosa.hz_to_midi(f0_v)
    # Step sizes: a melody moves mostly by small intervals, a chord
    # progression jumps around. This separates the two.
    steps = np.abs(np.diff(librosa.hz_to_midi(f0)))
    steps = steps[np.isfinite(steps)]
    smooth = float((steps < 2.0).mean()) if steps.size else 0.0

    return {
        "voiced_ratio": round(float(voiced.mean()), 3),
        "register_midi": round(float(np.median(midi)), 1),
        "range_midi": [round(float(np.percentile(midi, 10)), 1),
                       round(float(np.percentile(midi, 90)), 1)],
        "step_lt2_semitones": round(smooth, 3),
        "high_band_share": round(band_energy / total_energy, 3),
        "is_melodic": bool(voiced.mean() > 0.5 and smooth > 0.85),
    }


# ---------------------------------------------------------------- roles

def window_roles(feats):
    """Role share per stem over 5s windows.

    Returns (shares dict, per-window records, chord guesses).
    """
    fr = feats["_frames"]
    hop, sr = fr["hop"], fr["sr"]
    rms_db, voiced, classes, f0 = fr["rms_db"], fr["voiced"], fr["classes"], fr["f0"]
    chroma = fr["chroma"]
    onsets = fr["onsets"]

    frames_per_win = int(WIN_SEC * sr / hop)
    frames_per_hop = int(HOP_SEC * sr / hop)
    n = len(rms_db)
    windows = []
    chords = []

    for start in range(0, max(n - frames_per_win // 2, 1), frames_per_hop):
        sl = slice(start, min(start + frames_per_win, n))
        act = rms_db[sl] > ACTIVE_DB
        act_n = int(act.sum())
        if act_n < frames_per_win * 0.2:
            continue
        v = voiced[sl][act] if act.any() else np.zeros(0)
        c = classes[sl][act] if act.any() else np.zeros(0)
        f = f0[sl][act] if act.any() else np.zeros(0)
        voiced_frac = float(v.mean()) if len(v) else 0.0
        avg_cls = float(c.mean()) if len(c) else 0.0
        t0, t1 = start * hop / sr, min(start + frames_per_win, n) * hop / sr
        onset_rate = float(((onsets >= t0) & (onsets < t1)).sum() / max(t1 - t0, 1e-9))

        # Rough chord guess from the window's mean chroma.
        ch = guess_chord(chroma[:, sl].mean(axis=1))
        if ch:
            chords.append(ch)

        med_midi = float(librosa.hz_to_midi(np.median(f[f > 0]))) if (f > 0).any() else 0.0

        if med_midi < 40 and avg_cls < 1.8 and voiced_frac > 0.4:
            role = ROLE_BASS
        elif avg_cls >= 2.3:
            if onset_rate < 0.5 and feats["harmonic_ratio"] > 0.55:
                role = ROLE_PAD
            elif onset_rate >= 2.0:
                role = ROLE_ACCOMP
            else:
                role = ROLE_CHORDS
        elif voiced_frac > 0.55 and avg_cls < 2.2 and med_midi >= 48:
            role = ROLE_MELODY
        elif onset_rate >= 0.8 and avg_cls >= 1.5:
            role = ROLE_ACCOMP
        elif voiced_frac > 0.35 and avg_cls < 2.2 and med_midi >= 48:
            role = ROLE_MELODY
        elif feats["harmonic_ratio"] > 0.7 and onset_rate < 1.0:
            role = ROLE_PAD
        else:
            role = ROLE_SPARSE
        windows.append(role)

    shares = {}
    for r in windows:
        shares[r] = shares.get(r, 0) + 1
    total = max(len(windows), 1)
    shares = {r: round(c / total, 3) for r, c in sorted(shares.items(),
                                                       key=lambda kv: -kv[1])}

    # Compress the chord sequence: drop repeats.
    seq, prev = [], None
    for ch in chords:
        if ch != prev:
            seq.append(ch)
            prev = ch
    return shares, windows, seq[:12]


def guess_chord(chroma_mean):
    best, best_score = None, -1.0
    for root in range(12):
        for third, quality in ((4, ""), (3, "m")):
            tones = [root, (root + third) % 12, (root + 7) % 12]
            score = chroma_mean[tones].mean() - (
                chroma_mean[[i for i in range(12) if i not in tones]].mean() + 1e-9)
            if score > best_score:
                best_score, best = score, f"{CHORD_NAMES[root]}{quality}"
    return best if best_score > 0.15 else None


# ---------------------------------------------------------------- roles (cross-stem)

def assign_roles(stems):
    """Finalize roles with cross-stem context (counter-melody needs a rival)."""
    melody_scores = {
        name: s["roles"].get(ROLE_MELODY, 0.0)
        for name, s in stems.items() if not s.get("empty")
    }
    top = max(melody_scores.values(), default=0.0)
    top_stems = [n for n, v in melody_scores.items() if v >= top - 1e-9 and v >= 0.25]

    for name, s in stems.items():
        if s.get("empty"):
            s["primary_role"], s["keyboards"] = "-", []
            continue
        shares = s["roles"]
        primary = max(shares, key=shares.get) if shares else ROLE_SPARSE
        if name == "drums":
            primary = ROLE_RHYTHM
        elif name == "bass" and shares.get(ROLE_BASS, 0) < 0.5:
            primary = ROLE_BASS  # the separator's bass stem is the bass line
        elif primary == ROLE_MELODY and name not in top_stems and len(top_stems) > 0:
            primary = ROLE_COUNTER

        keyboards = []
        for role, share in shares.items():
            kb = KEYBOARD_OF_ROLE.get(role, "")
            if kb and kb not in keyboards and (share >= 0.2 or role == primary):
                keyboards.append(kb)
        main_kb = KEYBOARD_OF_ROLE.get(primary, "")
        if main_kb:
            keyboards = [main_kb] + [k for k in keyboards if k != main_kb]
        if name == "bass" and "脚" not in keyboards:
            keyboards.insert(0, "脚")

        # A piano stem that carries a clean high-register line can serve both
        # hands: the melody on the right, the accompaniment on the left. This
        # is how the part is actually played, and it is the only way to get a
        # right-hand line when the vocal stem is excluded.
        mel = s.get("melody")
        if mel and mel.get("is_melodic") and "右手" not in keyboards:
            keyboards.insert(0, "右手")
            s["melody_keyboard"] = "右手"

        # `other` is the residual bucket: it holds whatever the separator could
        # not name. It is a real instrument layer (here: bowed strings), but it
        # is not a melody in its own right, so it must not claim the right hand
        # on its own. Keep it as a right-hand doubling layer only.
        if name == "other":
            primary = ROLE_PAD
            keyboards = ["右手"]

        s["primary_role"] = primary
        s["keyboards"] = keyboards
    return stems


# ---------------------------------------------------------------- chord seq filter

def compact(seq):
    out = []
    for ch in seq:
        if not out or out[-1] != ch:
            out.append(ch)
    return out


# ---------------------------------------------------------------- report

def describe(feats):
    if feats.get("empty"):
        return ["静音/无效轨"]
    b = feats["brightness_hz"]
    bright = "明亮" if b > 2200 else ("适中" if b > 900 else "偏暗/温暖")
    harm = feats["harmonic_ratio"]
    harm_txt = f"谐波占比 {harm:.2f}（{'音高清晰' if harm > 0.6 else '打击/噪声成分较多'}）"
    reg = feats.get("register_midi")
    reg_txt = f"音区 {midi_name(reg)} 附近" if reg else "音区未检出"
    rng = feats.get("f0_range")
    if rng:
        reg_txt += f"（约 {midi_name(rng[0])}–{midi_name(rng[1])}）"
    poly = feats.get("avg_pitch_classes")
    poly_txt = ""
    if poly is not None:
        poly_txt = f"同时音数约 {poly:.1f}（{'多声部' if poly >= 2.5 else '较单声部'}）"
    onset = feats["onset_rate"]
    onset_txt = f"音符密度 {onset:.1f} 个/秒（{'节奏型明显' if onset >= 2 else '偏持续'}）"
    out = [f"亮度 {b:.0f} Hz（{bright}）", harm_txt, reg_txt]
    if poly_txt:
        out.append(poly_txt)
    out.append(onset_txt)
    out.append(f"有声占比 {feats['activity'] * 100:.0f}%，响度 {feats['rms_db']:.1f} dB")
    return out


def build_report(song, stems, refined, chord_seq):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# 音色分析报告 — {song}",
        "",
        f"生成时间：{now}　工具：`scripts/analyze-tones.py`",
        "",
        "## 总览",
        "",
        "| 轨道 | 乐器 | 主要角色 | 分配键盘 | 角色占比 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, s in stems.items():
        if s.get("empty"):
            lines.append(f"| {name}.wav | {label_of(name)} | （空轨） | — | — |")
            continue
        shares = "、".join(f"{r} {v * 100:.0f}%" for r, v in list(s["roles"].items())[:3])
        kb = "、".join(s["keyboards"]) or "—（不映射键盘）"
        lines.append(f"| {name}.wav | {label_of(name)} | {s['primary_role']} | {kb} | {shares} |")

    if chord_seq:
        lines += ["", f"检测到的和弦走向（粗略，5s 窗口）：`{' – '.join(chord_seq)}`"]

    lines += ["", "## 各轨详情", ""]
    for name, s in stems.items():
        lines.append(f"### {name}.wav — {label_of(name)}")
        lines.append("")
        if s.get("empty"):
            lines.append("- 该轨几乎无有效信号（分离结果为空或极弱），不生成音色。")
            lines.append("")
            continue
        lines.append(f"- **主要角色**：{s['primary_role']}"
                     + (f"（次要角色：{'、'.join(r for r in s['roles'] if r != max(s['roles'], key=s['roles'].get))}）"
                        if len(s["roles"]) > 1 else ""))
        kb = "、".join(s["keyboards"]) or "不映射（节奏由琴自带 rhythm 处理）"
        lines.append(f"- **键盘分配**：{kb}")
        lines.append("- **音色特点**：")
        for t in describe(s):
            lines.append(f"  - {t}")
        if s.get("chords"):
            lines.append(f"- **窗口和弦采样**：`{' – '.join(s['chords'])}`")
        lines.append("")

    if refined:
        lines += ["## 鼓组细分（refined，仅分析不生成音色）", ""]
        lines.append("| 部件 | 响度 dB | 亮度 Hz | 音符密度 | 有声占比 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for name, s in refined.items():
            if s.get("empty"):
                lines.append(f"| {name} | 静音 | — | — | — |")
            else:
                lines.append(f"| {name} | {s['rms_db']} | {s['brightness_hz']:.0f} "
                             f"| {s['onset_rate']:.1f}/s | {s['activity'] * 100:.0f}% |")
        lines.append("")

    lines += [
        "## 键盘映射规则",
        "",
        "- 右手 = 主旋律；左手 = 副旋律 / 和弦 / 伴奏 / 和声铺底；脚 = 低音线条",
        "- 节奏（鼓组）不映射到三个键盘，由 ELB-02 自带 rhythm 处理",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- main

def pick_stem_dir(args):
    stem_dir = args.stems
    if args.latest or args.song or not stem_dir:
        root = os.path.join(WORKSPACE, "stems")
        cands = [d for d in glob.glob(os.path.join(root, "*"))
                 if os.path.isdir(d) and os.path.basename(d) not in ("refined", ".staging")
                 and glob.glob(os.path.join(d, "*.wav"))]
        if args.song:
            cands = [d for d in cands if args.song in os.path.basename(d)]
        if not cands:
            sys.exit(f"no stem folder found under {root}")
        stem_dir = max(cands, key=os.path.getmtime)
    return stem_dir


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stems", default="", help="folder with separated stems")
    ap.add_argument("--latest", action="store_true",
                    help="use the newest stem folder")
    ap.add_argument("--song", default="", help="substring of the song folder name")
    ap.add_argument("--out", default=os.path.join(WORKSPACE, "analysis"),
                    help="analysis output root")
    ap.add_argument("--exclude", default="",
                    help="comma-separated stems to ignore, e.g. vocals,other")
    args = ap.parse_args()

    excluded = {s.strip() for s in args.exclude.split(",") if s.strip()}

    stem_dir = pick_stem_dir(args)
    song = os.path.basename(os.path.normpath(stem_dir))
    print(f"song: {song}")
    print(f"stems: {stem_dir}")
    if excluded:
        print(f"excluded: {', '.join(sorted(excluded))}")

    stems, refined = {}, {}
    for p in sorted(glob.glob(os.path.join(stem_dir, "*.wav"))):
        name = os.path.splitext(os.path.basename(p))[0]
        if name in DERIVED:
            print(f"  skip derived {name}.wav")
            continue
        if name in excluded:
            print(f"  skip excluded {name}.wav")
            continue
        print(f"  analyzing {name}.wav ...", flush=True)
        feats = analyze_stem(p, quick=(name == "drums"))
        if not feats.get("empty") and "_frames" in feats:
            shares, _, chords = window_roles(feats)
            feats["roles"] = shares
            feats["chords"] = compact(chords)
        else:
            feats["roles"] = {}
            feats["chords"] = []
        feats.pop("_frames", None)
        stems[name] = feats

    refined_dir = os.path.join(stem_dir, "refined")
    if os.path.isdir(refined_dir):
        for p in sorted(glob.glob(os.path.join(refined_dir, "*.wav"))):
            name = os.path.splitext(os.path.basename(p))[0]
            print(f"  analyzing refined/{name}.wav ...", flush=True)
            refined[name] = analyze_stem(p, quick=True)

    stems = assign_roles(stems)
    chord_seq = []
    for name in ("piano", "guitar", "other", "vocals"):
        if name in stems and stems[name].get("chords"):
            chord_seq = stems[name]["chords"]
            break

    out_dir = os.path.join(args.out, song)
    os.makedirs(out_dir, exist_ok=True)
    payload = {
        "song": song,
        "stems_dir": stem_dir,
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "keyboard_rule": {
            "右手": ROLE_MELODY, "左手": [ROLE_COUNTER, ROLE_CHORDS, ROLE_ACCOMP, ROLE_PAD],
            "脚": ROLE_BASS,
        },
        "stems": stems,
        "refined_drums": refined,
        "chord_sequence": chord_seq,
    }
    json_path = os.path.join(out_dir, "analysis.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    md_path = os.path.join(out_dir, "analysis.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(build_report(song, stems, refined, chord_seq))

    print(f"\nwrote {json_path}")
    print(f"wrote {md_path}")
    print("\n== summary ==")
    for name, s in stems.items():
        if s.get("empty"):
            print(f"  {name:8s} (empty)")
        else:
            print(f"  {name:8s} {label_of(name):6s} role={s['primary_role']:5s} "
                  f"keyboards={'/'.join(s['keyboards']) or '-'}")


if __name__ == "__main__":
    main()
