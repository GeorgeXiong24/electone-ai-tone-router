"""Phase 0 proof-of-concept benchmark (`improvements.md` Section 5) - the GATE.

Compares, on the existing *韦礼安 - 女孩* stems:

    Baseline      : the current sforzando chord-slicer per-key samples
    Candidate A   : De-Reverb + Harmonic-Isolated Golden-Anchor resynthesis
                    (anchor_extract + anchor_resynth)  [Engine A]
    Candidate B   : Stem-wavetable + attack-transient Vital (+ optimizer)
                    (vital_synth + tone_optimizer)      [Engine B]
    Candidate C   : Surge XT String / wavetable (+ optimizer)

and writes, to ``workspace/poc/<song>/``:

    scorecard.json                  objective metrics + pass/fail vs GATES
    <cand>_<keyboard>_audition.wav  a real musical phrase per candidate, so the
                                    user can A/B listen and decide the route.

Phase-0 decision rule (Section 5):
  * If A or B passes on both bass.wav and guitar.wav -> 100% per-song engines.
  * If bass passes but nonstop strummed guitar fails even after masking +
    Vital/Surge -> enable the Minimal Dry Seed Escape Hatch (Section 6.2.C).

The audition renders each keyboard's phrase from the candidate's own per-key
samples (a sampler), so Baseline and Candidate A are compared with zero VST
dependency; Candidate B/C render through pedalboard when the synth loads.
"""
import argparse
import glob
import json
import os
import sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _cache_env  # noqa: E402
import tone_common as tc  # noqa: E402
import anchor_extract as ae  # noqa: E402
import anchor_resynth as ar  # noqa: E402
import disk_budget as db  # noqa: E402

SR = tc.SR


# ---------------------------------------------------------------- sampler

def load_key_samples(samples_dir):
    """Map MIDI key -> float array from `kNNN[_l0].wav` files in a directory."""
    out = {}
    for p in glob.glob(os.path.join(samples_dir, "k*.wav")):
        base = os.path.basename(p)
        digits = "".join(ch for ch in base if ch.isdigit())
        if len(digits) < 3:
            continue
        key = int(digits[:3])
        # Prefer the first velocity layer when several exist.
        if key in out and "_l0" not in base:
            continue
        y, _ = tc.load_audio(p, sr=SR, mono=True)
        out[key] = y
    return out


def assemble_phrase(key_samples, events, sr=SR):
    """Render a MIDI phrase by placing per-key samples (a simple sampler).

    `events` = [(start_s, dur_s, midi, velocity)]. Missing keys fall back to the
    nearest available key (pitch-shifted by resampling), so a phrase is always
    renderable. Returns a 1-D float array.
    """
    if not key_samples:
        return np.zeros(int(3 * sr))
    total_s = max(e[0] + e[1] for e in events) + 2.5
    out = np.zeros(int(total_s * sr))
    keys = sorted(key_samples)
    for (start, dur, midi, vel) in events:
        if midi in key_samples:
            y = key_samples[midi]
            shift = 0.0
        else:
            near = min(keys, key=lambda k: abs(k - midi))
            y = key_samples[near]
            shift = 2.0 ** ((midi - near) / 12.0)   # resample ratio
        if shift != 1.0 and shift > 0:
            n_out = max(8, int(len(y) / shift))
            x_old = np.linspace(0, 1, len(y))
            y = np.interp(np.linspace(0, 1, n_out), x_old, y)
        # Place the note with a smooth 250 ms release (never an abrupt cut).
        max_len = int((dur + 0.9) * sr)
        n = min(len(y), max_len)
        seg = y[:n].copy()
        rel = int(0.25 * sr)
        if rel > 4 and n > rel:
            seg[-rel:] *= np.sqrt(np.linspace(1, 0, rel))
        seg *= (vel / 127.0)
        i0 = int(start * sr)
        i1 = min(i0 + len(seg), len(out))
        out[i0:i1] += seg[:i1 - i0]
    peak = np.abs(out).max() + 1e-9
    return out / peak * tc.from_db(-1.0)


# ---------------------------------------------------------------- metrics

def measure_keyboard(key_samples, keyboard, source_ltas=None, anchor_midi=None):
    """Objective scorecard metrics for a set of per-key samples.

    `anchor_midi` (median pitch of the source's Golden Anchors) makes the
    octave-band comparison register-fair: a stem played below the manual's range
    (e.g. this song's bass at G1-C2 vs the pedal's C2-G3) would otherwise be
    compared across octaves and inflate the spectral error.
    """
    lo, hi = tc.key_range(keyboard)
    keys = [k for k in range(lo, hi + 1) if k in key_samples]
    if not keys:
        return {"error": "no key samples"}

    # Pitch accuracy (autocorrelation ground truth, octave-robust).
    errs = {}
    for k in keys:
        f0 = tc.estimate_f0_ac(key_samples[k], SR,
                               fmin=tc.midi_to_hz(k) / 2, fmax=tc.midi_to_hz(k) * 2)
        errs[k] = tc.cents_between(tc.midi_to_hz(k), f0) if f0 else float("nan")
    finite = [e for e in errs.values() if np.isfinite(e)]
    within = [e for e in finite if abs(e) <= tc.GATES["pitch_cents_max"]]
    pitch_frac = len(within) / max(len(keys), 1)
    octave_err = sum(1 for e in finite
                     if abs(abs(e) - 1200) < tc.GATES["octave_error_cents"])
    fifth_err = sum(1 for e in finite
                    if abs(abs(e) - 702) < tc.GATES["fifth_error_cents"])

    # Key-to-key timbre coherence: relative jump in centroid/f0 across adjacent keys.
    ratios = []
    for k in keys:
        c = tc.spectral_centroid_hz(key_samples[k], SR)
        f0 = tc.midi_to_hz(k)
        ratios.append(c / f0 if f0 > 0 else np.nan)
    jumps = []
    for a, b in zip(ratios[:-1], ratios[1:]):
        if np.isfinite(a) and np.isfinite(b) and a > 0:
            jumps.append(abs(b - a) / a)
    timbre_jump = float(np.max(jumps)) if jumps else 0.0

    # Octave-band spectral error vs source stem (register-fair subset).
    octave_err_db = None
    if source_ltas is not None:
        if anchor_midi is not None:
            cmp_keys = [k for k in keys if abs(k - anchor_midi) <= 7] or keys
        else:
            cmp_keys = keys
        synth = np.mean([tc.octave_band_ltas(key_samples[k], SR) for k in cmp_keys],
                        axis=0)
        band_err = np.abs(np.asarray(source_ltas) - synth)[1:7]   # 125 Hz..4 kHz
        octave_err_db = float(np.median(band_err))

    # Silent keys.
    silent = sum(1 for k in keys
                 if 20 * np.log10(np.abs(key_samples[k]).max() + 1e-9)
                 < tc.GATES["silent_key_peak_dbfs"])

    return {
        "n_keys": len(keys),
        "pitch_pass_fraction": round(pitch_frac, 3),
        "pitch_median_cents": round(float(np.median(finite)), 1) if finite else None,
        "octave_misdetections": octave_err,
        "fifth_misdetections": fifth_err,
        "timbre_jump_max": round(timbre_jump, 3),
        "octave_band_err_db": round(octave_err_db, 2) if octave_err_db else None,
        "silent_keys": silent,
    }


def gate_pass(m):
    if "error" in m:
        return False, ["no data"]
    fails = []
    if m["pitch_pass_fraction"] < tc.GATES["pitch_pass_fraction"]:
        fails.append(f"pitch {m['pitch_pass_fraction']:.0%} < "
                     f"{tc.GATES['pitch_pass_fraction']:.0%}")
    if m["octave_misdetections"] or m["fifth_misdetections"]:
        fails.append("octave/fifth misdetection")
    if m["timbre_jump_max"] > tc.GATES["timbre_jump_max"]:
        fails.append(f"timbre jump {m['timbre_jump_max']:.0%} > "
                     f"{tc.GATES['timbre_jump_max']:.0%}")
    if m.get("octave_band_err_db") is not None and \
            m["octave_band_err_db"] > tc.GATES["octave_band_err_db"]:
        fails.append(f"octave-band err {m['octave_band_err_db']:.1f} dB")
    if m["silent_keys"] > 0:
        fails.append(f"{m['silent_keys']} silent keys")
    return (not fails), fails


# ---------------------------------------------------------------- phrases

def phrase_for(keyboard):
    lo, hi = tc.key_range(keyboard)
    ev = []
    if keyboard in ("脚", "pedal"):
        root = 36
        line = [0, 0, 5, 5, 3, 3, 7, 7]        # C G A E-ish walking
        t = 0.0
        for s in line:
            ev.append((t, 0.55, root + s, 92)); t += 0.55
    else:
        # Clear guitar-style phrase: a simple melody line + a few open chords.
        # No low octave roots (they read as muddy bass) and smooth velocities so
        # nothing spikes as an "accent".
        melody = [72, 74, 76, 74, 72, 69, 71, 72]      # C5 D5 E5 D5 C5 A4 B4 C5
        t = 0.0
        for m in melody:
            ev.append((t, 0.55, m, 88)); t += 0.55
        t = 0.0
        for triad in ([60, 64, 67], [59, 62, 67], [57, 60, 64], [55, 60, 65]):
            for n in triad:
                ev.append((t, 1.1, n, 74))
            t += 1.15
    return ev


# ---------------------------------------------------------------- candidates

def run_baseline(song, keyboard, out_dir):
    """Measure + audition the current (old) sforzando per-key samples."""
    slug = None
    import tone_paths
    slug = tone_paths.song_slug(song)
    kb_dir = tone_paths.ascii_keyboard(keyboard)
    cands = glob.glob(os.path.join(tc.WORKSPACE, "tone", slug, kb_dir,
                                   "sforzando", "samples", "*"))
    cands = [c for c in cands if os.path.isdir(c)]
    if not cands:
        return {"error": "no baseline samples"}
    samples = load_key_samples(cands[0])
    return samples


def _pick_scan_windows(y, sr, k=5, win_s=25):
    """Choose `k` windows, one per time-zone, that best admit clean anchors.

    Within each zone pick the window maximising loudness x monophony: a window
    with single-note lines (melody, arpeggios) yields clean, *diverse-pitch*
    anchors so each keyboard key is only a small shift from a nearby anchor.
    Spreading zones across the song captures the instrument across its register.
    """
    import librosa
    win = int(win_s * sr)
    if len(y) < win * 2:
        return [y]
    starts = list(range(0, len(y) - win + 1, win // 2))
    if not starts:
        return [y]
    zones = np.array_split(np.array(starts), k)
    picks = []
    for z in zones:
        best_i, best_score = None, -1e9
        for i in z:
            i = int(i)
            seg = y[i:i + win]
            rms = float(np.sqrt((seg ** 2).mean() + 1e-12))
            if 20 * np.log10(rms + 1e-12) < -45:
                continue
            y_h, _ = librosa.effects.hpss(seg)
            chroma = librosa.feature.chroma_cqt(y=y_h, sr=sr, hop_length=tc.HOP)
            with np.errstate(invalid="ignore"):
                mx = chroma.max(axis=0)
                classes = ((chroma >= 0.5 * mx) & (mx > 0.1)).sum(axis=0)
            avg_cls = float(classes.mean())
            score = rms * (2.5 - min(avg_cls, 2.5))   # loud x monophonic
            if score > best_score:
                best_score, best_i = score, i
        if best_i is not None:
            picks.append(best_i)
    if not picks:
        return [y]
    picks.sort()
    return [y[i:i + win] for i in picks]


def run_candidate_a(stem_path, keyboard, out_dir, source_ltas=None):
    """Build Golden-Anchor samples from a stem (Engine A)."""
    y, sr = tc.load_audio(stem_path, sr=SR, mono=True)
    # Anchor extraction only needs representative *melodic* windows (bounded for
    # speed on a 5-minute stem); the keyboard is resynthesised in full below.
    if len(y) / sr > 60:
        wins = _pick_scan_windows(y, sr, k=5, win_s=25)
        scan = np.concatenate(wins) if len(wins) > 1 else wins[0]
    else:
        scan = y
    anchors = ae.extract_anchors(scan, sr, n_anchors=6)
    if not anchors:
        return {"error": "no anchors extracted"}, []
    for a in anchors:
        ar.prepare_anchor_fragment(a, scan, sr)
    res = ar.resynthesize_keyboard(anchors, keyboard, os.path.join(out_dir, "samples"),
                                   sr=sr, song_slug="candA",
                                   source_ltas=source_ltas)
    samples = load_key_samples(os.path.join(out_dir, "samples"))
    return samples, anchors


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--song", default="韦礼安 - 女孩")
    ap.add_argument("--out", default="", help="poc output dir")
    args = ap.parse_args()

    song = args.song
    out_root = args.out or os.path.join(tc.WORKSPACE, "poc", song)
    os.makedirs(out_root, exist_ok=True)
    stem_dir = os.path.join(tc.WORKSPACE, "stems", song)

    scorecard = {"song": song, "candidates": {}}

    for stem, keyboard in [("bass", "脚"), ("guitar", "右手")]:
        stem_path = os.path.join(stem_dir, stem + ".wav")
        if not os.path.isfile(stem_path):
            continue
        y, _ = tc.load_audio(stem_path, sr=SR, mono=True)
        source_ltas = tc.octave_band_ltas(y, SR)
        # Source register for a fair octave-band comparison (a bass played below
        # the manual's range must not be compared across octaves).
        f0r = tc.estimate_f0(y, SR)
        src_midi = float(tc.hz_to_midi(f0r)) if f0r else None
        kb_out = os.path.join(out_root, stem)
        os.makedirs(kb_out, exist_ok=True)
        print(f"\n===== {stem} ({keyboard}) =====")

        # Baseline.
        base = run_baseline(song, keyboard, kb_out)
        if isinstance(base, dict) and "error" in base:
            print("  baseline:", base["error"])
        else:
            m = measure_keyboard(base, keyboard, source_ltas, anchor_midi=src_midi)
            ok, fails = gate_pass(m)
            scorecard["candidates"].setdefault(stem, {})["baseline"] = \
                {"metrics": m, "pass": ok, "fails": fails}
            w = assemble_phrase(base, phrase_for(keyboard))
            tc.save_wav16(os.path.join(kb_out, "baseline_audition.wav"), w, SR)
            print(f"  baseline : pass={ok} pitch={m['pitch_pass_fraction']:.0%} "
                  f"timbre={m['timbre_jump_max']:.0%} silent={m['silent_keys']}")

        # Candidate A (Golden-Anchor).
        result = run_candidate_a(stem_path, keyboard, kb_out, source_ltas)
        if isinstance(result, tuple):
            candA, anchors = result
        else:
            candA, anchors = result, []
        if isinstance(candA, dict) and "error" in candA:
            print("  candidateA:", candA["error"])
            scorecard["candidates"].setdefault(stem, {})["candidateA"] = \
                {"error": candA["error"], "pass": False}
        else:
            m = measure_keyboard(candA, keyboard, source_ltas,
                                 anchor_midi=float(np.median([a["midi"] for a in anchors]))
                                 if anchors else src_midi)
            ok, fails = gate_pass(m)
            scorecard["candidates"].setdefault(stem, {})["candidateA"] = \
                {"metrics": m, "pass": ok, "fails": fails,
                 "anchors": [round(a["midi"], 1) for a in anchors]}
            w = assemble_phrase(candA, phrase_for(keyboard))
            tc.save_wav16(os.path.join(kb_out, "candidateA_audition.wav"), w, SR)
            print(f"  candidateA: pass={ok} pitch={m['pitch_pass_fraction']:.0%} "
                  f"timbre={m['timbre_jump_max']:.0%} silent={m['silent_keys']} "
                  f"anchors={[round(a['midi'],1) for a in anchors]}")

    # Disk budget in the report.
    _ok, budget = db.check_song_budget(song)
    scorecard["disk_budget"] = budget

    sc_path = os.path.join(out_root, "scorecard.json")
    with open(sc_path, "w", encoding="utf-8") as f:
        json.dump(scorecard, f, ensure_ascii=False, indent=2)
    print(f"\nwrote {sc_path}")
    print("audition wavs in", out_root)


if __name__ == "__main__":
    main()
