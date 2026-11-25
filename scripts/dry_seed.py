"""Clean dry-seed tone engine (Hybrid route, `improvements.md` §6.2.C + Engine C).

Phase-0 review: per-song extraction from strummed / messy separated stems kept
producing a noisy "four-not-alike" tone because the source fragments carry chord
bleed and separation artifacts. The improvements' sanctioned fix for dense
polyphonic strummed parts is a **dry neutral seed, heavily reshaped per-song**.

Instead of hunting an external recording (network search is unreliable here),
the seed is a **clean physically-modelled instrument** - exactly Engine C
(Surge XT `String` = Karplus-Strong). Karplus-Strong is a real plucked-string
model: a noise burst in a damped delay line. It is 100% bleed-free, recognisably
a plucked string, in-tune by construction, and we fully control its colour.

  * Guitar seed : Karplus-Strong plucked string + acoustic body resonances.
  * Bass seed   : warm Karplus-Strong / lowpass-subtractive electric bass.

Each is then reshaped per-song: a Match EQ fits the seed's octave-band LTAS to
that song's stem, and the envelope/space is matched - so the tone is a clean,
recognisable instrument that *blends with the song* (the hybrid promise).
"""
import os
import sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tone_common as tc  # noqa: E402

CFG = {
    "target_peak_dbfs": -1.0,
    "match_eq_max_db": 9.0,
    # Karplus-Strong damping (0.99x = long ring). Lower = faster decay.
    "guitar_damping": 0.9965,
    "bass_damping": 0.9985,
    "guitar_brightness": 0.42,   # excitation lowpass (0..1): steel-ish brightness
    "bass_brightness": 0.22,
}


# ---------------------------------------------------------------- Karplus-Strong

def karplus_strong(f0, dur_s, sr=tc.SR, damping=0.9965, brightness=0.42,
                   seed=0):
    """Clean plucked-string tone at `f0` (no bleed, exact pitch).

    Period-wise Karplus-Strong: a filtered noise burst in a damped averaging
    delay line. Vectorised per period (fast). This is a real physical string
    model -> recognisably a plucked string, not synthetic buzz.
    """
    f0 = max(20.0, float(f0))
    P = max(2, int(round(sr / f0)))
    n = int(dur_s * sr)
    n_periods = n // P + 2
    rng = np.random.default_rng(1234 + int(seed))
    buf = rng.standard_normal(P)
    # Excitation colour (steel vs nylon/warm): lowpass the noise burst.
    b = float(np.clip(brightness, 0.02, 0.99))
    if b < 0.98:
        from scipy.signal import butter, sosfilt
        sos = butter(2, b, "low", output="sos")
        buf = sosfilt(sos, buf)
    buf = buf / (np.abs(buf).max() + 1e-9)
    out = np.empty((n_periods + 1) * P, dtype=np.float64)
    out[:P] = buf
    d = float(damping)
    for k in range(1, n_periods + 1):
        buf = d * 0.5 * (buf + np.roll(buf, -1))
        out[k * P:(k + 1) * P] = buf
    return out[:n]


def _body_resonances(y, sr, peaks):
    """Apply a few body resonances (Hz, dB, Q) so the string sounds like a body."""
    from scipy.signal import iirpeak, lfilter
    out = y
    for (f, gain_db, q) in peaks:
        if f >= sr / 2:
            continue
        b, a = iirpeak(f / (sr / 2), q)
        band = lfilter(b, a, out)
        out = out + (tc.from_db(gain_db) - 1.0) * band
    return out


def _subtle_space(y, sr, wet=0.16, rt60=0.6):
    """Short decaying-noise reverb -> connects successive notes (legato feel).

    The v3 guitar was 'grainy / every note isolated'; a little room tail lets
    each note ring into the next instead of stopping dead.
    """
    import scipy.signal
    tail_len = max(64, int(rt60 * sr))
    rng = np.random.default_rng(7)
    tail = rng.standard_normal(tail_len) * np.exp(-np.linspace(0, 6.0, tail_len))
    pre = int(0.008 * sr)
    tail[:pre] = 0.0
    tail /= (np.abs(tail).max() + 1e-9)
    wet_sig = scipy.signal.fftconvolve(y, tail, mode="full")[:len(y)]
    if np.abs(wet_sig).max() > 1e-9:
        wet_sig = wet_sig / np.abs(wet_sig).max() * np.abs(y).max()
    return (1.0 - wet) * y + wet * wet_sig


def _broad_body(y, sr, lo_gain, mid_gain, hi_gain, split_lo=220.0, split_hi=2200.0):
    """Smooth broad body shape (no sharp resonances -> sounds natural, not 'electronic')."""
    from scipy.signal import butter, sosfilt
    lo = sosfilt(butter(2, split_lo / (sr / 2), "low", output="sos"), y)
    hi = sosfilt(butter(2, split_hi / (sr / 2), "high", output="sos"), y)
    mid = sosfilt(butter(2, [split_lo / (sr / 2), split_hi / (sr / 2)],
                         "band", output="sos"), y)
    return lo * lo_gain + mid * mid_gain + hi * hi_gain


def _inharmonic_stretch(y, sr, f0, stretch=0.0004):
    """Slightly stretch the harmonic series (real strings are inharmonic), which
    removes the 'perfectly electronic' quality of a pure KS string."""
    S = tc.stft(y)
    mag, phase = np.abs(S), np.angle(S)
    freqs = np.linspace(0, sr / 2, mag.shape[0])
    # Warp frequency axis: f' = f*(1+stretch*f) so upper harmonics drift sharp.
    warped = freqs * (1.0 + stretch * freqs / 1000.0)
    out = np.empty_like(mag)
    for c in range(mag.shape[1]):
        out[:, c] = np.interp(freqs, warped, mag[:, c], left=0.0, right=0.0)
    return tc.istft(out * np.exp(1j * phase), length=len(y))


def guitar_note(f0, dur_s=1.8, sr=tc.SR):
    # Softer, warmer excitation (less harsh/synthetic pluck).
    y = karplus_strong(f0, dur_s, sr, damping=0.9992, brightness=0.26)
    # Smooth broad body (warm low-mid, gentle top rolloff) - NOT sharp peaks,
    # which were what made it read as 'electronic'.
    y = _broad_body(y, sr, lo_gain=1.18, mid_gain=1.05, hi_gain=0.72)
    # Slight inharmonicity -> natural string, not a perfect synth.
    y = _inharmonic_stretch(y, sr, f0, stretch=0.0005)
    n = len(y)
    env = np.exp(-np.linspace(0, 1.5, n))
    atk = int(0.014 * sr)
    env[:atk] *= np.linspace(0, 1, atk)
    rel = int(0.30 * sr)
    if rel < n:
        env[-rel:] *= np.sqrt(np.linspace(1, 0, rel))
    y = y * env
    y = _subtle_space(y, sr, wet=0.08, rt60=0.5)     # lighter, less 'processed'
    peak = np.abs(y).max() + 1e-9
    return y / peak * tc.from_db(CFG["target_peak_dbfs"])


def bass_note(f0, dur_s=1.9, sr=tc.SR):
    y = karplus_strong(f0, dur_s, sr, damping=0.9990, brightness=0.30)
    # Warm low-mid body (bass presence), smooth (no sub boost).
    y = _broad_body(y, sr, lo_gain=1.05, mid_gain=1.20, hi_gain=0.60,
                    split_lo=180.0, split_hi=1600.0)
    from scipy.signal import butter, sosfilt
    # Kill sub-rumble hard: high-pass at 55 Hz AND subtract a low shelf below 130 Hz.
    y = sosfilt(butter(2, 55.0 / (sr / 2), "high", output="sos"), y)
    low = sosfilt(butter(2, 130.0 / (sr / 2), "low", output="sos"), y)
    y = y - 0.50 * low                      # tame the "低音炮" boom
    # Lift low-mid (150-500 Hz) so it reads as bass, not sub.
    band = sosfilt(butter(2, [150.0 / (sr / 2), 500.0 / (sr / 2)],
                          "band", output="sos"), y)
    y = y + 0.35 * band
    n = len(y)
    atk = int(0.015 * sr)
    env = np.ones(n)
    env[:atk] = np.linspace(0, 1, atk)
    env *= np.concatenate([np.ones(int(0.45 * n)),
                           np.linspace(1.0, 0.20, n - int(0.45 * n))])
    rel = int(0.30 * sr)
    if rel < n:
        env[-rel:] *= np.sqrt(np.linspace(1, 0, rel))
    y = y * env
    peak = np.abs(y).max() + 1e-9
    return y / peak * tc.from_db(CFG["target_peak_dbfs"])


# ---------------------------------------------------------------- per-song reshape

def match_eq_to_source(y, source_ltas, max_db=CFG["match_eq_max_db"]):
    """Fit the clean seed's octave-band LTAS to the song's stem (per-song voice)."""
    if source_ltas is None:
        return y
    seed_ltas = tc.octave_band_ltas(y, tc.SR)
    diff = np.clip(np.asarray(source_ltas, float) - np.asarray(seed_ltas, float),
                   -max_db, max_db)
    # Smooth 3-band correction applied as a gentle spectral tilt via EQ gains.
    g1 = float(np.mean(diff[:2]))
    g2 = float(np.mean(diff[2:5]))
    g3 = float(np.mean(diff[5:]))
    from scipy.signal import butter, sosfilt
    # Low band.
    sos_lo = butter(2, 250 / (tc.SR / 2), "low", output="sos")
    lo = sosfilt(sos_lo, y) * tc.from_db(g1)
    sos_hi = butter(2, 2000 / (tc.SR / 2), "high", output="sos")
    hi = sosfilt(sos_hi, y) * tc.from_db(g3)
    sos_mid = butter(2, [250 / (tc.SR / 2), 2000 / (tc.SR / 2)], "band", output="sos")
    mid = sosfilt(sos_mid, y) * tc.from_db(g2)
    return lo + mid + hi


# ---------------------------------------------------------------- keyboard driver

def build_dry_seed_keyboard(keyboard, kind, out_dir, sr=tc.SR,
                            source_ltas=None, song_slug="dryseed"):
    """Render a full keyboard from the clean dry seed, reshaped to the song."""
    lo, hi = tc.key_range(keyboard)
    os.makedirs(out_dir, exist_ok=True)
    note_fn = guitar_note if kind == "guitar" else bass_note
    samples = {}
    ltas_acc = []
    for key in range(lo, hi + 1):
        y = note_fn(tc.midi_to_hz(key), sr=sr)
        y = match_eq_to_source(y, source_ltas)
        peak = np.abs(y).max() + 1e-9
        y = y / peak * tc.from_db(CFG["target_peak_dbfs"])
        fname = f"k{key:03d}.wav"
        tc.save_wav16(os.path.join(out_dir, fname), y, sr)
        samples[key] = fname
        ltas_acc.append(tc.octave_band_ltas(y, sr))
    synth_ltas = np.mean(np.array(ltas_acc), axis=0).tolist()
    sfz_path = os.path.join(out_dir, f"{song_slug}.sfz")
    _build_sfz(sfz_path, samples, keyboard)
    return {"sfz": sfz_path, "samples": samples, "n_keys": len(samples),
            "synth_ltas": synth_ltas, "kind": kind}


def _build_sfz(sfz_path, samples, keyboard):
    os.makedirs(os.path.dirname(os.path.abspath(sfz_path)), exist_ok=True)
    lines = [
        "// electone-ai-tone-router : clean dry-seed instrument (Karplus-Strong)",
        f"// keyboard={keyboard} keys={min(samples)}..{max(samples)}",
        "<global>",
        "amp_veltrack=45", "fil_type=lpf_2p", "cutoff=4500",
        "fil_veltrack=2400", "ampeg_attack=0.003", "ampeg_release=0.250",
        "<group>",
    ]
    for key in sorted(samples):
        lines.append(f"<region> sample={samples[key]} pitch_keycenter={key} "
                     f"lokey={key} hikey={key} loop_mode=one_shot")
    with open(sfz_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return sfz_path
