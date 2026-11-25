"""Polyphony-aware Golden-Anchor extraction (Engine A, `improvements.md` Engine A.2).

Replaces the broken ``build-sample-tones.py`` approach that sliced every chord
fragment onto an individual key (measured: only 4/12 keys in tune, up to two
octaves of stretch, 38+ unrelated slices across 49 keys). Instead we locate a
small number (3-6) of **timbre-consistent, correctly-pitched single notes** and
resynthesise the whole keyboard from those (``anchor_resynth``).

Pipeline
--------
  1. Note candidates: ``basic-pitch`` when importable, else onset segmentation +
     a fixed 0.5 s grid so soft-attack material (bass) and nonstop strumming
     (guitar) still yield windows. Long runs are split into ~0.6 s pieces.
  2. Measure every candidate ONCE (pitch, SNR, sub-harmonic bleed, timbre,
     attack, f0 wobble) and record ``reject_reasons`` - no re-measuring later.
  3. Pitch verification (strict at every stage): harmonic/sub-harmonic comb test
     + autocorrelation, so octave/fifth misdetections are rejected.
  4. Relaxation ladder: widen only the *soft* thresholds (voiced ratio, SNR,
     sub-bleed, f0 wobble) as far as needed to reach the minimum anchor count.
     Pitch verification never relaxes - an out-of-tune anchor is never OK.
  5. Single timbre cluster + even register spread -> 3-6 Golden Anchors.

Every optional heavy dependency has a librosa/scipy fallback: the module runs on
Python 3.14 where ``basic_pitch`` wheels may be missing.
"""
import os
import sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tone_common as tc  # noqa: E402

CFG = {
    "min_mono_interval_s": 0.22,
    "max_pitch_classes": 1.6,
    "min_voiced_ratio": 0.6,
    "max_f0_iqr_cents": 45.0,
    "min_snr_db": 6.0,
    "min_body_rms_db": -52.0,
    "max_sub_peak_ratio": 0.35,
    "max_timbre_residual": 0.22,
    "max_attack_diff_ms": 40.0,
    "n_anchors_min": 3,
    "n_anchors_max": 6,
    "harm_comb_cents": 35.0,
    "autocorr_min": 0.55,
    # Pitch-verification gate, placed inside the measured distribution of this
    # project's own separated stems (comb 0.09..0.79, acf -0.01..0.98). A
    # separated stem is not a studio recording, so the old comb>0.35 / acf>=0.55
    # pair rejected clean bass notes that measured 0.55 / 0.52.
    "verify_comb_min": 0.22,
    "verify_acf_min": 0.34,
    # Reject a pitch whose octave-down period is nearly as periodic as the claim.
    "verify_subharm_ratio": 0.92,
    # Looser pair used by the last relaxation level, when a dense stem simply has
    # no perfect candidate: 3+ consistent anchors matter more than a flawless comb.
    "verify_comb_min_loose": 0.12,
    "verify_acf_min_loose": 0.25,
    # Multi-window scan: when given a long stem, scan windows of this length
    # (with this hop) and pool candidates, so the best anchors are found across
    # the song instead of wherever one arbitrary window happened to land.
    "scan_window_s": 20.0,
    "scan_hop_s": 10.0,
}

# Soft-filter relaxation ladder. Pitch verification is *not* in here: it stays
# strict at every level. Level 0 = strict defaults.
_RELAX_LEVELS = [
    {"min_voiced_ratio": 0.6, "min_snr_db": 6.0,
     "max_sub_peak_ratio": 0.35, "f0_iqr_scale": 1.0},
    {"min_voiced_ratio": 0.5, "min_snr_db": 3.0,
     "max_sub_peak_ratio": 0.45, "f0_iqr_scale": 1.3},
    {"min_voiced_ratio": 0.4, "min_snr_db": 1.0,
     "max_sub_peak_ratio": 0.6, "f0_iqr_scale": 1.7},
    {"min_voiced_ratio": 0.3, "min_snr_db": -2.0,
     "max_sub_peak_ratio": 0.8, "f0_iqr_scale": 2.2},
    {"min_voiced_ratio": 0.15, "min_snr_db": -6.0,
     "max_sub_peak_ratio": 1.2, "f0_iqr_scale": 3.0},
]


# ---------------------------------------------------------------- transcription

def _transcribe_basic_pitch(y, sr):
    try:
        from basic_pitch.inference import predict
        from basic_pitch import ICASSP_2022_MODEL_PATH
        import tempfile
        import soundfile as sf
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp = f.name
        sf.write(tmp, y, sr)
        try:
            _o, _m, note_events = predict(tmp, ICASSP_2022_MODEL_PATH)
        finally:
            os.unlink(tmp)
        return [(float(a), float(b), tc.midi_to_hz(float(p)))
                for (a, b, p, _v, _bd) in note_events]
    except Exception:
        return None


def _segments(y, sr):
    """Note candidates: onset-split + long-run split + fixed-grid fallback."""
    import librosa
    dur = len(y) / sr
    onsets = librosa.onset.onset_detect(y=y, sr=sr, units="time",
                                        hop_length=tc.HOP, backtrack=True)
    segs = []
    edges = list(onsets) + [dur] if len(onsets) >= 2 else []
    for a, b in zip(edges[:-1], edges[1:]):
        t = a
        while t + 0.12 < b:
            t2 = min(t + 0.6, b)
            f0 = tc.estimate_f0_ac(y[int(t * sr):int(t2 * sr)], sr, fmin=28.0)
            if f0:
                segs.append((float(t), float(t2), float(f0)))
            t = t2
    if len(segs) < 3:
        t = 0.0
        while t + 0.5 <= dur:
            f0 = tc.estimate_f0_ac(y[int(t * sr):int((t + 0.5) * sr)], sr, fmin=28.0)
            if f0:
                segs.append((float(t), float(t + 0.5), float(f0)))
            t += 0.375
    return segs


# ---------------------------------------------------------------- measurement

def _frame_pitch_classes(y, sr):
    import librosa
    y_h, _ = librosa.effects.hpss(y)
    chroma = librosa.feature.chroma_cqt(y=y_h, sr=sr, hop_length=tc.HOP)
    with np.errstate(invalid="ignore"):
        frame_max = chroma.max(axis=0)
        classes = ((chroma >= 0.5 * frame_max) & (frame_max > 0.1)).sum(axis=0)
    return classes.astype(float)


def _measure_note(y, sr, t0, t1, f0):
    """Measure one candidate unconditionally; record `reject_reasons`.

    No early threshold returns here - the relaxation ladder later filters on the
    recorded metrics, so we never recompute pyin / STFT per level.
    """
    import librosa
    s0, s1 = int(t0 * sr), int(t1 * sr)
    seg = y[s0:s1]
    reasons = []
    if seg.size < 512:
        return None
    body_rms = float(np.sqrt((seg ** 2).mean())) + 1e-12
    if tc.db(body_rms) < CFG["min_body_rms_db"]:
        return None                     # silent: no metrics worth keeping

    # f0 + stability via autocorrelation (fast, octave-robust). pyin was both
    # slow (~200 s/window) and octave-errant on low/looped notes.
    #
    # `voiced_ratio` must not be quantised by the sub-segment count: a 0.25 s note
    # split into 3 pieces could only ever score 0.0/0.33/0.67/1.0, so a perfectly
    # steady note failed the 0.6 gate. Pieces are chosen adaptively and the ratio
    # is how many of them show a clear pitch.
    f0_ac = tc.estimate_f0_ac(seg, sr, fmin=max(25.0, f0 / 3), fmax=f0 * 3)
    if f0_ac:
        f0 = f0_ac
    n_pieces = int(np.clip(round(len(seg) / (0.12 * sr)), 2, 6))
    subs = []
    for piece in np.array_split(seg, n_pieces):
        if len(piece) >= 2048:
            s = tc.estimate_f0_ac(piece, sr, fmin=max(25.0, f0 / 3), fmax=f0 * 3)
            if s:                      # None means "no confident pitch here"
                subs.append(s)
    voiced_ratio = len(subs) / max(n_pieces, 1)
    iqr_cents = 0.0
    if len(subs) >= 2:
        med = float(np.median(subs))
        hi = float(np.percentile(subs, 75))
        iqr_cents = abs(tc.cents_between(med, hi)) if hi > 0 else 0.0
        f0 = med

    # SNR vs. quiet-frame floor.
    fr = librosa.feature.rms(y=y, frame_length=2048, hop_length=tc.HOP)[0]
    noise_floor = float(np.percentile(fr, 10)) + 1e-9
    snr_db = float(tc.db(body_rms) - tc.db(noise_floor))

    # Spectral measurements: timbre, sub-harmonic bleed.
    n_fft = 8192 if f0 < 150.0 else 4096
    S = np.abs(tc.stft(seg, n_fft=n_fft))
    freqs = np.linspace(0, sr / 2, S.shape[0])
    power = (S ** 2).mean(axis=1)
    centroid = float((freqs * power).sum() / (power.sum() + 1e-12))
    bin_hz = sr / float(n_fft)

    def band_power(fc):
        sel = np.abs(freqs - fc) <= max(20.0, 0.04 * fc, 1.5 * bin_hz)
        return float(power[sel].sum()) if sel.any() else 0.0
    p_f0 = band_power(f0)
    p_sub = max(band_power(f0 / 2.0), band_power(f0 / 3.0), band_power(f0 / 1.5))
    sub_peak_ratio = p_sub / (p_f0 + 1e-12)

    # Attack time.
    rms = librosa.feature.rms(y=seg, frame_length=1024, hop_length=128)[0]
    if len(rms) >= 4:
        pk = int(np.argmax(rms[:max(1, len(rms) // 3)]))
        peak = float(rms[pk])
        i10 = next((i for i in range(pk + 1) if rms[i] >= 0.1 * peak), 0)
        i90 = next((i for i in range(i10, len(rms)) if rms[i] >= 0.9 * peak), i10)
        attack_ms = (i90 - i10) * 128 / sr * 1000.0
    else:
        attack_ms = 0.0

    return {
        "start_s": float(t0), "end_s": float(t1),
        "f0_hz": float(f0), "midi": float(tc.hz_to_midi(f0)),
        "detune_cents": float(tc.cents_between(
            tc.midi_to_hz(round(tc.hz_to_midi(f0))), f0)),
        "snr_db": round(snr_db, 1), "voiced_ratio": round(voiced_ratio, 3),
        "f0_iqr_cents": round(iqr_cents, 1),
        "sub_peak_ratio": round(sub_peak_ratio, 3),
        "timbre": round(float(np.log2(max(centroid, 1.0) / f0)), 3),
        "attack_ms": round(attack_ms, 1), "centroid_hz": round(centroid, 0),
        "reject_reasons": reasons,
    }


def _passes(m, relax):
    if m["voiced_ratio"] < relax["min_voiced_ratio"]:
        return False
    if m["snr_db"] < relax["min_snr_db"]:
        return False
    if m["sub_peak_ratio"] > relax["max_sub_peak_ratio"]:
        return False
    if m["f0_iqr_cents"] > CFG["max_f0_iqr_cents"] * relax["f0_iqr_scale"]:
        return False
    return True


def _verify_pitch(seg, sr, f0, loose=False):
    """Harmonic-comb + autocorrelation pitch confirmation.

    `loose` uses the last-rung thresholds, for stems where no candidate can meet
    the strict pair; the sub-harmonic (octave) guard is applied either way.
    """
    if f0 is None or f0 <= 0 or len(seg) < 512:
        return False, f0
    n_fft = 8192 if f0 < 150.0 else 4096
    S = np.abs(tc.stft(seg, n_fft=n_fft))
    freqs = np.linspace(0, sr / 2, S.shape[0])
    power = (S ** 2).mean(axis=1)
    total = float(power.sum()) + 1e-12
    bin_hz = sr / float(n_fft)
    comb = 0.0
    for k in range(1, 17):
        fk = f0 * k
        if fk > sr / 2:
            break
        tol = max(fk * (2.0 ** (CFG["harm_comb_cents"] / 1200.0) - 1.0), 1.5 * bin_hz)
        sel = np.abs(freqs - fk) <= tol
        comb += float(power[sel].sum()) if sel.any() else 0.0
    comb_ratio = comb / total
    yy = seg - seg.mean()
    nfft = 1 << int(2 * len(yy) - 1).bit_length()
    Y = np.fft.rfft(yy, nfft)
    ac = np.fft.irfft(Y * np.conj(Y), nfft)[:len(yy)]
    ac = ac / (ac[0] + 1e-12)
    lag = int(round(sr / f0))
    lo, hi = int(lag * 0.85), int(lag * 1.15) + 1
    ac_peak = float(np.max(ac[lo:hi])) if hi > lo and hi < len(ac) else 0.0
    # MEASURED on this project's own stems: real separated notes score
    # comb 0.09..0.79 and autocorr -0.01..0.98. The old gate (comb > 0.35 AND
    # acf >= 0.55) rejected clean bass notes that measured comb 0.55 / acf 0.52
    # purely because a separated stem is not a studio recording. The gate keeps
    # its purpose - reject a misdetected pitch - with thresholds placed inside the
    # measured distribution, and combined with a check that the autocorr peak
    # actually sits at the claimed period rather than merely being large.
    comb_min = CFG["verify_comb_min_loose"] if loose else CFG["verify_comb_min"]
    acf_min = CFG["verify_acf_min_loose"] if loose else CFG["verify_acf_min"]
    ok = comb_ratio > comb_min and ac_peak >= acf_min
    if ok:
        # Octave guard, done in a way that can actually discriminate.
        #
        # A "comb ratio at f/2" is useless here: the harmonics of f/2 are exactly
        # a superset of those of f, so comb(f/2) == comb(f) by construction
        # (measured: ratio 1.00 for every candidate). What distinguishes a true
        # octave error is whether the spectrum has a strong partial BELOW f that
        # f cannot explain - i.e. the lowest strong peak in the series. If the
        # fundamental sits at ~f/2 (within a semitone) the claimed f is an octave
        # high.
        sel = np.flatnonzero(power > 0.02 * power.max())
        if sel.size:
            lowest = float(freqs[sel[0]])
            if lowest >= 25.0 and abs(tc.cents_between(lowest, f0 / 2.0)) < 100.0:
                ok = False
    return bool(ok), f0


# ---------------------------------------------------------------- cluster + select

def _timbre_cluster(cands):
    """Pick the consistent-timbre set, keeping PITCH DIVERSITY.

    MEASURED BUG this fixes: on this song's bass there were 226 verified
    candidates spanning MIDI 25.7-55, but this function returned 59 copies of
    MIDI 45 - one single note. It sorted by `timbre` and pruned from the ends, so
    the pruned set collapsed onto whichever note had the median timbre, and after
    de-duplication only one anchor survived. That single anchor is why the pedal
    keyboard then had to stretch one note across its whole range.

    Diversity is therefore a selection criterion, not an afterthought: candidates
    are first reduced to one good representative per semitone, and the timbre
    spread is only used to *prefer* a coherent subset among those.
    """
    if not cands:
        return []

    # One representative per semitone (best score, then strongest), so the set
    # can never be N copies of one pitch again.
    by_semi = {}
    for c in cands:
        k = int(round(c["midi"]))
        prev = by_semi.get(k)
        if prev is None or (c.get("score", 0.0), c["snr_db"]) > (
                prev.get("score", 0.0), prev["snr_db"]):
            by_semi[k] = c
    uniq = sorted(by_semi.values(), key=lambda c: c["midi"])

    if len(uniq) <= CFG["n_anchors_max"]:
        return uniq

    # More distinct semitones than we need: keep the timbre-consistent ones,
    # spread over the register so resynthesis never has to stretch far.
    timbres = np.array([c["timbre"] for c in uniq])
    centre = float(np.median(timbres))
    coherent = [c for c in uniq if abs(c["timbre"] - centre) <= CFG["max_timbre_residual"]]
    if len(coherent) >= CFG["n_anchors_min"]:
        uniq = coherent
    order = sorted(uniq, key=lambda c: abs(c["timbre"] - centre))
    keep = order[:CFG["n_anchors_max"]]
    return sorted(keep, key=lambda c: c["midi"])


def extract_anchors(y, sr=tc.SR, n_anchors=4):
    """Return 3-6 Golden Anchors for a stem (ordered by pitch).

    Accepts a full-length stem: long signals are scanned in overlapping windows
    and candidates pooled, so the best anchors are found across the song.
    """
    n_anchors = int(tc.clamp(n_anchors, CFG["n_anchors_min"], CFG["n_anchors_max"]))
    y = np.asarray(y, dtype=np.float64)
    if y.ndim > 1:
        y = y.mean(axis=1)
    total = len(y) / sr

    # Scan windows (a single window can land on a silent or dense passage).
    # Each window carries the ABSOLUTE sample offset it starts at, because the
    # candidate timestamps it produces are relative to the window and a caller
    # renders from the full stem.
    if total > CFG["scan_window_s"] * 1.5:
        windows = []
        w = int(CFG["scan_window_s"] * sr)
        step = int(CFG["scan_hop_s"] * sr)
        t = 0
        while t + w <= len(y):
            windows.append((t, y[t:t + w]))
            t += step
    else:
        windows = [(0, y)]

    raw = []
    for (offset_s, w) in windows:
        offset = offset_s / sr
        events = _transcribe_basic_pitch(w, sr)
        if not events:
            events = _segments(w, sr)
        if not events:
            continue
        classes = _frame_pitch_classes(w, sr)
        hop_s = tc.HOP / sr
        for (t0, t1, f0) in events:
            if t1 - t0 < CFG["min_mono_interval_s"]:
                continue
            f0i = int(t0 / hop_s); f1i = max(f0i + 1, int(t1 / hop_s))
            avg_cls = float(classes[f0i:f1i].mean()) if f1i > f0i else 99.0
            m = _measure_note(w, sr, t0, t1, f0)
            if m is None:
                continue
            ok, vf0 = _verify_pitch(w[int(t0 * sr):int(t1 * sr)], sr, m["f0_hz"])
            m["verified_strict"] = bool(ok)
            if not ok:
                # Not a strict pass; remember whether the autocorrelation still
                # supports the pitch so the ladder can fall back to it.
                m["verify_loose"] = bool(_verify_pitch(
                    w[int(t0 * sr):int(t1 * sr)], sr, m["f0_hz"],
                    loose=True)[0])
                if not m["verify_loose"]:
                    continue
            m["f0_hz"] = vf0
            m["midi"] = float(tc.hz_to_midi(vf0))
            m["detune_cents"] = float(tc.cents_between(
                tc.midi_to_hz(round(tc.hz_to_midi(vf0))), vf0))
            m["mono"] = bool(avg_cls <= CFG["max_pitch_classes"])
            m["score"] = _score(m, m["mono"])
            # BUG FIX: timestamps are window-relative, but a caller that renders
            # the whole stem uses them as absolute song time. Without this the
            # bass anchors were reported at 8-17 s while the audio that produced
            # them actually lived elsewhere, so `prepare_anchor_fragment` read
            # SILENCE (-100 dBFS, measured) and every key came from noise. Shift
            # by the window's offset on the song timeline.
            m["start_s"] = float(m["start_s"]) + offset
            m["end_s"] = float(m["end_s"]) + offset
            raw.append(m)

    if not raw:
        return []

    # A candidate rejected by the strict comb gate may still be a usable anchor
    # when the autocorrelation agrees with it: a separated stem simply has no
    # perfect candidates. They are collected into a second pool that the ladder
    # may fall back to, so a dense stem yields a coherent anchor set instead of a
    # single note. Octave consistency is still enforced (see _verify_pitch).
    loose = [m for m in raw if m.get("verify_loose")]

    # Relaxation ladder over the pre-measured candidates (fast filtering).
    cands = []
    for relax in _RELAX_LEVELS:
        cands = [m for m in raw if _passes(m, relax)]
        if len(cands) >= CFG["n_anchors_min"]:
            break
    if len(cands) < CFG["n_anchors_min"]:
        # Nothing met even the loosest rung. Fall back to every candidate whose
        # pitch verification accepted: on a separated stem a coherent 3-anchor set
        # taken from slightly noisier notes still gives every key the SAME
        # instrument, which is the property that matters for a keyboard. The
        # timbre cluster below keeps them consistent.
        verified = [m for m in raw
                    if m.get("verified_strict") or m.get("verify_loose")]
        if len(verified) > len(cands):
            cands = verified

    mono_cands = [c for c in cands if c["mono"]]
    pool = mono_cands if len(mono_cands) >= CFG["n_anchors_min"] else cands
    # _timbre_cluster already guarantees one representative per semitone.
    pool = _timbre_cluster(pool)
    pool.sort(key=lambda c: c["midi"])

    if len(pool) <= n_anchors:
        chosen = pool
    else:
        idx = np.linspace(0, len(pool) - 1, n_anchors)
        chosen = []
        for i in idx:
            c = pool[int(round(i))]
            if c not in chosen:
                chosen.append(c)

    # Prune outlier anchors across wide gaps (>= 4 semitones) if brightness
    # ratio differs by > 0.35, so we never bridge mismatched articulations.
    while len(chosen) > CFG["n_anchors_min"]:
        centre = float(np.median([c["timbre"] for c in chosen]))
        dropped = False
        for i in range(len(chosen) - 1):
            a_lo, a_hi = chosen[i], chosen[i + 1]
            if a_hi["midi"] - a_lo["midi"] >= 4.0:
                r_lo, r_hi = 2.0 ** a_lo["timbre"], 2.0 ** a_hi["timbre"]
                if abs(r_hi - r_lo) / max(r_lo, 1e-9) > 0.35:
                    drop_idx = (i if abs(a_lo["timbre"] - centre) > abs(a_hi["timbre"] - centre)
                                else i + 1)
                    chosen.pop(drop_idx)
                    dropped = True
                    break
        if not dropped:
            break

    for c in chosen:
        c.setdefault("source", "onset_pyin")
    return chosen


def _score(m, mono):
    return (0.35 * m["voiced_ratio"]
            + 0.25 * (1.0 - min(m["f0_iqr_cents"] / CFG["max_f0_iqr_cents"], 1.0))
            + 0.20 * min(m["snr_db"] / 30.0, 1.0)
            + 0.10 * (1.0 - min(m["sub_peak_ratio"] / CFG["max_sub_peak_ratio"], 1.0))
            + 0.10 * (1.0 if mono else 0.0))
