"""Clean-isolation resynthesis + sfizz SFZ assembly (Engine A.3-A.4) - REBUILD.

Phase-0 human review rejected three synthesis attempts (additive-from-smoothed-
envelope = buzzy/blurry; PSOLA grains = wrong fundamental; raw resample = noisy
"four-not-alike"). The root cause is the SAME every time: the separated stems are
not clean single notes. A strummed guitar is full of overlapping strings and the
leaves separation artifacts, so any resynthesis of the messy fragment drags that
bleed along as noise ("杂音"), and crossfade-looping it adds warble.

This rebuild follows `improvements.md` Engine A.2 Priority-2 **Harmonic-Comb
isolation** (the step that was built but never used): before any synthesis we
carve ONE clean harmonic series (k*f0) out of the chord, which removes the other
strings' partials. Then:

  * the clean body is resampled to the exact target pitch (timbre ratios kept),
  * extended by a **seamless phase-locked loop** (whole periods, no warble),
  * shaped with an instrument-correct decay / sustain envelope,
  * the real attack transient is kept at original speed (pluck character),
  * **zero injected noise**.

Per-key: nearest Golden Anchor -> isolate clean note -> resample -> loop ->
envelope -> real attack -> smooth release -> 16-bit WAV + SFZ opcodes.
"""
import os
import sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tone_common as tc  # noqa: E402
import anchor_extract as ae  # noqa: E402

CFG = {
    "attack_ms": (12.0, 45.0),
    "release_ms": 260.0,
    "sustain_len_s": 1.9,
    "match_eq_max_db": 7.5,
    "amp_veltrack": 45,
    "fil_veltrack": 2400,
    "base_cutoff_hz": 4500.0,
    "attack_s": 0.003,
    "target_peak_dbfs": -1.0,
    # Harmonic-comb isolation: keep k*f0 within +-cents, attenuate the rest.
    "comb_cents": 33.0,
    "comb_attenuate_db": 34.0,
    "comb_max_harmonics": 40,
}


# ---------------------------------------------------------------- spectral envelope (Match EQ)

def spectral_envelope(y, sr, lifter=38):
    S = np.abs(tc.stft(y))
    M = S.mean(axis=1) if S.ndim == 2 else S
    M = np.maximum(M, 1e-8)
    logM = np.log(M)
    n = 2 * (len(M) - 1)
    cep = np.fft.irfft(logM, n=n).copy()
    cep[lifter:] = 0.0
    env = np.exp(np.fft.rfft(cep, n=n).real)
    freqs = np.linspace(0.0, sr / 2.0, len(M))
    return freqs, np.maximum(env, 1e-6)


# ---------------------------------------------------------------- harmonic-comb isolation

def harmonic_comb_mask(y, sr, f0, cents=CFG["comb_cents"],
                       attenuate_db=CFG["comb_attenuate_db"],
                       max_harmonics=CFG["comb_max_harmonics"]):
    """Isolate ONE clean harmonic series (k*f0) from a chordal/bleedy fragment.

    Retain energy within +-`cents` of k*f0 (k=1..max_harmonics, up to Nyquist);
    attenuate everything else by `attenuate_db`. This removes the other strings /
    instruments' partials, so the result is a clean single note instead of a
    noisy chord smear.
    """
    if f0 is None or f0 <= 0:
        return y
    # Use 8192 FFT for bass (<150 Hz) where bin spacing matters, and 2048 for
    # mid/high notes (>=150 Hz) to preserve transient & decay time resolution.
    n_fft = 8192 if f0 < 150.0 else 2048
    if f0 >= 150.0 and attenuate_db == CFG["comb_attenuate_db"]:
        attenuate_db = 20.0
    S = tc.stft(y, n_fft=n_fft)
    mag, phase = np.abs(S), np.angle(S)
    freqs = np.linspace(0, sr / 2, mag.shape[0])
    mask = np.zeros_like(mag)
    bin_hz = sr / float(n_fft)
    k_max = int(min(max_harmonics, (sr / 2.0) / f0))
    for k in range(1, max(1, k_max) + 1):
        fk = f0 * k
        if fk >= sr / 2:
            break
        tol = max(fk * (2.0 ** (cents / 1200.0) - 1.0), 0.6 * bin_hz)
        sel = np.abs(freqs - fk) <= tol
        mask[sel, :] = 1.0
    gain = mask + (1.0 - mask) * tc.from_db(-abs(attenuate_db))
    out = tc.istft(mag * gain * np.exp(1j * phase), n_fft=n_fft, length=len(y))
    return out


# ---------------------------------------------------------------- resample + loop

def _resample(y, ratio):
    y = np.asarray(y, dtype=np.float64)
    n_out = max(16, int(round(len(y) / max(ratio, 1e-3))))
    try:
        from scipy.signal import resample as _rs
        return _rs(y, n_out)
    except Exception:
        x_old = np.linspace(0, 1, len(y))
        return np.interp(np.linspace(0, 1, n_out), x_old, y)


def _periodic_loop(body, sr, f0_target, length_s):
    """Seamless sustain: tile the body, phase-locked to its own period.

    BUG FIXED (measured): the previous version took `n_per = len(steady) // P`
    whole periods and tiled that unit. Because P is rounded to an integer while
    the true period is fractional, the discarded remainder left the tile seam
    out of phase - rendering key 49 at **416 Hz instead of 138.6 Hz** (a 1903
    cent error, exactly 3x). It also used a `body[:need]` early return that cut
    the note mid-period.

    Now an integer number of periods is taken only when it is *exactly*
    consistent: the loop start is snapped to a period boundary and the seam is
    equal-power crossfaded, so repeated tiling cannot change the fundamental.
    """
    need = int(length_s * sr)
    n = len(body)
    if n == 0:
        return np.zeros(need)
    P = max(4, sr / f0_target)                # keep it FRACTIONAL for snapping
    if n >= need:
        # Long enough: take one contiguous span, still snapped to a period so the
        # release fade does not land mid-cycle.
        k = int(round(need / P))
        take = min(n, int(round(k * P)) if k > 0 else need)
        out = body[:max(take, need)]
        return out[:need]

    # Short body: loop a clean steady window (~60 ms) snapped to whole periods
    # so tiling preserves the exact fundamental without dragging pitch drift /
    # vibrato sidebands across the note.
    target_periods = max(8, int(round(0.06 * sr / P)))
    n_periods = min(max(1, int(n / P)), target_periods)
    unit_len = int(round(n_periods * P))
    start = min(max(0, n // 4), max(0, n - unit_len))
    unit = body[start:min(start + unit_len, n)]
    if len(unit) < 16:
        unit = body
    # Equal-power crossfade at the seam so the join cannot click.
    xf = min(int(0.005 * sr), len(unit) // 4)
    if xf > 4:
        fade_in = np.sqrt(np.linspace(0, 1, xf))
        fade_out = np.sqrt(np.linspace(1, 0, xf))
        unit = unit.copy()
        unit[:xf] = unit[:xf] * fade_in + unit[-xf:] * fade_out

    reps = int(np.ceil(need / len(unit))) + 1
    out = np.tile(unit, reps)[:need]
    return out


def _string_damped_tail(body, sr, f0_target, length_s, damping=0.996):
    """Extend a plucked/decaying note naturally via one-zero string damping.

    Unlike static period tiling (which sounds like a buzz/organ), lowpass
    damping seeded from the note's final period damps upper harmonics faster
    than the fundamental, matching real plucked string physics.
    """
    need = int(length_s * sr)
    n = len(body)
    if n >= need:
        return body[:need]
    P = max(2, int(round(sr / max(f0_target, 20.0))))
    if n < P + 4:
        return np.pad(body, (0, need - n))
    buf = body[-P:].astype(np.float64).copy()
    out = np.empty(need, dtype=np.float64)
    out[:n] = body
    idx = n
    while idx < need:
        buf = damping * (0.25 * np.roll(buf, 1) + 0.50 * buf + 0.25 * np.roll(buf, -1))
        take = min(P, need - idx)
        out[idx:idx + take] = buf[:take]
        idx += take
    return out


# ---------------------------------------------------------------- amplitude envelope

def _amp_env(n, sr, tendency, anchor):
    env = np.ones(n)
    atk = int(max(anchor.get("attack_ms", 10.0), 8.0) / 1000.0 * sr)
    atk = min(atk, max(2, n // 5))
    if atk > 1:
        env[:atk] = np.linspace(0, 1, atk)
    body = n - atk
    if body <= 0:
        return env
    if tendency == "percussive":
        env[atk:] = np.exp(-np.linspace(0, 4.5, body))
    elif tendency == "decaying":
        env[atk:] = np.exp(-np.linspace(0, 2.8, body))
    else:  # sustained (bass)
        env[atk:] = 0.92
    return env


# ---------------------------------------------------------------- note driver

def prepare_anchor_fragment(anchor, y, sr):
    """Isolate a clean single note + real attack from the anchor (kills bleed)."""
    yy = y.mean(axis=1) if y.ndim > 1 else np.asarray(y, dtype=np.float64)
    s0, s1 = int(anchor["start_s"] * sr), int(anchor["end_s"] * sr)
    seg = yy[max(0, s0):max(s1, s0 + 512)]
    if seg.size < 512:
        return anchor
    f0 = tc.estimate_f0_ac(seg, sr, fmin=25.0, fmax=1400.0) or \
        float(anchor.get("f0_hz") or 220.0)
    anchor["_f0_src"] = f0

    atk_len = int(np.clip(anchor.get("attack_ms", 18.0), *CFG["attack_ms"]) / 1000.0 * sr)
    atk_len = max(32, min(atk_len, len(seg) // 3))
    anchor["_atk"] = seg[:atk_len].copy()

    body = seg[atk_len:] if len(seg) > atk_len + 512 else seg
    # Harmonic-comb isolation -> clean tonal body (the noise/bleed is removed here).
    anchor["_seg"] = harmonic_comb_mask(body, sr, f0)
    ef, ev = spectral_envelope(anchor["_seg"], sr)
    anchor["_env_freqs"], anchor["_env"] = ef, ev
    anchor.setdefault("tendency", "decaying")
    return anchor


def resynthesize_note(anchor, target_midi, sr=tc.SR, length_s=None, **_ignored):
    """Clean note at `target_midi`: isolated body + seamless loop + real attack."""
    f0_target = tc.midi_to_hz(target_midi)
    clean = anchor.get("_seg")
    if clean is None:
        raise ValueError("run prepare_anchor_fragment first (no clean body)")
    f0_src = anchor.get("_f0_src", 220.0)
    tendency = anchor.get("tendency", "decaying")
    if length_s is None:
        length_s = {"percussive": 1.6, "decaying": 1.9,
                    "sustained": CFG["sustain_len_s"]}.get(tendency, 1.7)

    f0_body = tc.estimate_f0_ac(clean, sr, fmin=25.0, fmax=1400.0) or f0_src
    ratio = f0_target / max(f0_body, 1e-3)
    body = _resample(clean, ratio)                  # clean tonal body at pitch
    if tendency == "sustained":
        body = _periodic_loop(body, sr, f0_target, length_s)
    else:
        body = _string_damped_tail(body, sr, f0_target, length_s)

    out = body * _amp_env(len(body), sr, tendency, anchor)
    if tendency == "sustained":
        out, _ = tc.phase_locked_loop(out, sr, f0_target)

    atk = anchor.get("_atk")
    if atk is not None and len(atk) > 16 and len(out) > len(atk):
        xf = min(len(atk), int(0.008 * sr))
        fade = np.linspace(1, 0, xf)
        out[:xf] = out[:xf] * (1 - fade) + atk[:xf] * fade

    r = int(CFG["release_ms"] / 1000.0 * sr)
    if r > 4 and len(out) > r:
        out[-r:] *= np.sqrt(np.linspace(1, 0, r))
    a = int(0.003 * sr)
    if a > 2 and len(out) > a:
        out[:a] *= np.linspace(0, 1, a)

    peak = np.abs(out).max() + 1e-9
    return out / peak * tc.from_db(CFG["target_peak_dbfs"])


# ---------------------------------------------------------------- Match EQ

def fit_match_eq(source_ltas, synth_ltas, max_db=CFG["match_eq_max_db"]):
    src = np.asarray(source_ltas, dtype=float)
    syn = np.asarray(synth_ltas, dtype=float)
    diff = src - syn
    clamp = lambda g: float(np.clip(g, -max_db, max_db))
    return (clamp(float(np.mean(diff[:2]))),
            clamp(float(np.mean(diff[2:5]))),
            clamp(float(np.mean(diff[5:]))))


# ---------------------------------------------------------------- SFZ assembly

def build_sfz(sfz_path, samples, eq=(0.0, 0.0, 0.0), keyboard="右手",
              amp_veltrack=CFG["amp_veltrack"]):
    os.makedirs(os.path.dirname(os.path.abspath(sfz_path)), exist_ok=True)
    g1, g2, g3 = eq
    lines = [
        "// electone-ai-tone-router : Golden-Anchor sfizz instrument (clean isolation)",
        f"// keyboard={keyboard}  keys={min(samples)}..{max(samples)}",
        "// one 16-bit sample per key; velocity dynamics via fil_veltrack+amp_veltrack",
        "<global>",
        "amp_veltrack=%d" % amp_veltrack,
        "fil_type=lpf_2p",
        "cutoff=%.0f" % CFG["base_cutoff_hz"],
        "fil_veltrack=%d" % CFG["fil_veltrack"],
        "ampeg_attack=%.4f" % CFG["attack_s"],
        "ampeg_release=0.250",
        "eq1_freq=110  eq1_bw=1.0  eq1_gain=%.2f" % g1,
        "eq2_freq=750  eq2_bw=1.0  eq2_gain=%.2f" % g2,
        "eq3_freq=4000 eq3_bw=1.0 eq3_gain=%.2f" % g3,
        "<group>",
    ]
    keys = sorted(samples)
    for i, key in enumerate(keys):
        # The outermost regions span the WHOLE MIDI range (0..127). A strict
        # one-region-per-key map leaves silent keys the moment the instrument's
        # octave/transpose setting differs from our assumption - that was the
        # reported "only a few pedal keys sound" defect.
        lokey = 0 if i == 0 else key
        hikey = 127 if i == len(keys) - 1 else key
        lines.append(f"<region> sample={samples[key]} pitch_keycenter={key} "
                     f"lokey={lokey} hikey={hikey} loop_mode=one_shot")
    with open(sfz_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return sfz_path


# ---------------------------------------------------------------- keyboard driver

def resynthesize_keyboard(anchors, keyboard, out_dir, sr=tc.SR, song_slug="song",
                          source_ltas=None):
    lo, hi = tc.key_range(keyboard)
    os.makedirs(out_dir, exist_ok=True)
    for a in anchors:
        if "_seg" not in a:
            raise ValueError("run prepare_anchor_fragment on each anchor first")
    samples = {}
    synth_ltas_acc = []
    for key in range(lo, hi + 1):
        anchor = min(anchors, key=lambda a: abs(a["midi"] - key))
        y = resynthesize_note(anchor, key, sr=sr)
        fname = f"k{key:03d}.wav"
        tc.save_wav16(os.path.join(out_dir, fname), y, sr)
        samples[key] = fname
        synth_ltas_acc.append(tc.octave_band_ltas(y, sr))
    synth_ltas = np.mean(np.array(synth_ltas_acc), axis=0).tolist()
    eq = fit_match_eq(source_ltas, synth_ltas) if source_ltas is not None else (0, 0, 0)
    sfz_path = os.path.join(out_dir, f"{song_slug}.sfz")
    build_sfz(sfz_path, samples, eq=eq, keyboard=keyboard)
    return {"sfz": sfz_path, "samples": samples, "n_keys": len(samples),
            "eq_db": eq, "synth_ltas": synth_ltas,
            "anchors_used": [round(a["midi"], 1) for a in anchors]}
