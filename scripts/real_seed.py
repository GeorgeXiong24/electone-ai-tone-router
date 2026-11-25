"""Real recorded-sample tone engine (the reliable fix for 'electronic'/rumble).

After several synthesis attempts kept reading as 'electronic' (a modelled string
always does) with a stubborn low-end, the fix is a **real recorded instrument**:
clean dry CC-licensed samples (Karoryfer "Black & Green Guitars" = Gretsch/Hofner
electric guitar; "Black & Blue Basses" = dark electric bass), stored once in
``D:\SampleLibraries\{guitar,bass}_seed`` and reshaped per-song.

Each keyboard key picks the nearest real recorded note and resamples it to the
exact pitch (a real recording -> sounds real, not electronic), then a per-song
Match EQ blends it to the target stem. This is `improvements.md` §6.2.C dry seed.
"""
import os
import re
import sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tone_common as tc  # noqa: E402

SEED_DIRS = {
    # guitar: the MELLOW Hofner ("black") from Black & Green Guitars.
    #
    # MEASURED: the bright "green" Gretsch take is a spiky twang with a 5516 Hz
    # spectral centroid and 20-28% of its energy above 4 kHz, which is exactly
    # the reported "沙沙声 + 电子乐" character (hiss/buzz). The "black" Hofner
    # take is a mellow jazz guitar: centroid 628 Hz, 2.8% above 4 kHz, spectral
    # flatness 0.00000. Choosing the warm seed removes the harshness instead of
    # trying to EQ it away.
    "guitar": r"D:\SampleLibraries\guitar_alt",
    "bass": r"D:\SampleLibraries\bass_seed",
}

# Only the mellow "black" takes are seeds; the bright "green" ones are reference
# material, not part of the instrument.
SEED_FILE_FILTER = {"guitar": "black_mf_"}

# Per-instrument octave transposition.
#
# The bass on 《女孩》 sits at G1-C2 (MIDI 33-36), but the ELB-02 pedal's LOWEST
# key is C2 (MIDI 36), so the part physically cannot be played where it was
# recorded - the bottom octave simply does not exist on the instrument. It has
# to be transposed UP an octave to be playable at all, and the review also
# reported the low octave as a "subwoofer rumble" that was unpleasant. Rendering
# the bass one octave up fixes both: the whole part lands inside C2-G3 and reads
# as a clean bass line instead of sub-bass.
OCTAVE_SHIFT = {"guitar": 0, "bass": +12}

_NAME_TO_SEMI = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}


def note_to_midi(name):
    """'a3' -> 57, 'bb2' -> 46, 'db4' -> 61 (note letter, optional flat, octave)."""
    m = re.search(r"([a-g])(b?)(\d)", name.lower())
    if not m:
        return None
    letter, flat, octv = m.group(1), m.group(2), m.group(3)
    semi = _NAME_TO_SEMI[letter]
    if flat:
        semi -= 1
    return (int(octv) + 1) * 12 + semi


def _harmonic_series_score(y, sr, f0):
    """Fraction of spectral energy falling on the k*f0 series (k=1..20)."""
    S = np.abs(tc.stft(y)) ** 2
    power = S.mean(axis=1)
    freqs = np.linspace(0, sr / 2, len(power))
    total = float(power.sum()) + 1e-12
    acc = 0.0
    for k in range(1, 21):
        fk = f0 * k
        if fk >= sr / 2:
            break
        tol = fk * (2 ** (35.0 / 1200.0) - 1.0)
        sel = np.abs(freqs - fk) <= max(tol, 3.0)
        acc += float(power[sel].sum()) if sel.any() else 0.0
    return acc / total


def calibrate_seed_pitch(y, sr, nominal_midi):
    """Resolve a real recording's true octave against its filename.

    Seed filenames can be an octave off (measured: guitar_a3's dominant energy
    sits at ~108 Hz, not 220), and a filename-trusting resample ratio then
    shifts every rendered key wrong. Score the three octave candidates
    (f0/2, f0, f0*2) on how well their harmonic series explains the spectrum and
    keep the best - the filename only breaks ties.
    """
    f_nom = tc.midi_to_hz(nominal_midi)
    best_midi, best_score = nominal_midi, -1.0
    for cand in (nominal_midi - 12, nominal_midi, nominal_midi + 12):
        f = tc.midi_to_hz(cand)
        if f < 30 or f > sr / 2:
            continue
        score = _harmonic_series_score(y, sr, f)
        # Small prior toward the filename so a tie does not flip octaves.
        if cand == nominal_midi:
            score += 0.02
        if score > best_score:
            best_score, best_midi = score, cand
    return best_midi


def load_seeds(kind, calibrate=True):
    """Load recorded seed notes -> {midi: (y, sr)} from the seed folder.

    MEASURED: every Karoryfer seed file sounds exactly ONE OCTAVE below its
    filename (`guitar_a3.wav` = 110.6 Hz, not 220; `bass_a2.wav` = 55 Hz, not
    110; all 77 files within -1191..-1211 cents). That is the library's own
    convention, not a transcription error, so the correction is a fixed -12,
    not a fragile spectral guess. Without it every rendered key came out an
    octave wrong (and the earlier 'bass sounds low / make it an octave up'
    report is exactly this shift).
    """
    d = SEED_DIRS.get(kind)
    out = {}
    if not d or not os.path.isdir(d):
        return out
    for fn in os.listdir(d):
        if not fn.lower().endswith(".wav"):
            continue
        want = SEED_FILE_FILTER.get(kind)
        if want and want not in fn:
            continue
        midi = note_to_midi(fn)
        if midi is None:
            continue
        y, sr = tc.load_audio(os.path.join(d, fn), sr=tc.SR, mono=True)
        if y.size <= 512:
            continue
        if calibrate:
            midi = midi - 12          # library names the file one octave high
        if midi in out and np.abs(out[midi][0]).max() >= np.abs(y).max():
            continue
        out[midi] = (y, sr)
    return out

def _resample(y, ratio):
    n_out = max(16, int(round(len(y) / max(ratio, 1e-3))))
    try:
        from scipy.signal import resample as _rs
        return _rs(y, n_out)
    except Exception:
        x_old = np.linspace(0, 1, len(y))
        return np.interp(np.linspace(0, 1, n_out), x_old, y)


def _note_end(y, sr, floor_db=-46.0, rel_db=-40.0):
    """Sample index where the note has decayed into the noise floor.

    MEASURED: the Karoryfer recordings carry a -53 dB amp/room noise floor, and
    taking the full 2.4 s dragged that hiss into every note (the reported
    '沙沙声' background). A note is over once it falls to `rel_db` below its peak
    *and* into the noise floor; we stop there and fade out.
    """
    if y.size < 1024:
        return y.size
    hop = 256
    win = 1024
    n_frames = max(1, (y.size - win) // hop)
    rms = np.array([np.sqrt((y[i * hop:i * hop + win] ** 2).mean() + 1e-15)
                    for i in range(n_frames)])
    if rms.size == 0:
        return y.size
    dbv = 20.0 * np.log10(rms + 1e-12)
    peak = float(dbv.max())
    thr = max(peak + rel_db, floor_db)
    # Last frame above the threshold = end of the note body.
    above = np.flatnonzero(dbv > thr)
    if above.size == 0:
        return y.size
    end = int((above[-1] + 1) * hop + win)
    return int(min(y.size, max(end, int(0.12 * sr))))


def _fade_tail(y, sr, fade_ms=90.0):
    out = y.copy()
    f = int(fade_ms / 1000.0 * sr)
    if 1 < f < out.size:
        out[-f:] *= np.sqrt(np.linspace(1, 0, f))
    return out


def render_key(seeds, target_midi, length_s=2.0, sr=tc.SR, kind=None):
    """Nearest real note resampled to `target_midi`, trimmed to `length_s`.

    `kind` applies that instrument's octave transposition (bass +12): the note
    actually rendered is `target_midi + OCTAVE_SHIFT[kind]`, so a bass part
    written below the pedal's range is voiced an octave up where it is playable.

    The recording is first trimmed to its true end (killing the amp/room noise
    tail that caused the '沙沙声' background) and then faded out smoothly.
    """
    if not seeds:
        raise ValueError("no seed samples loaded")
    eff = int(target_midi) + int(OCTAVE_SHIFT.get(kind, 0))
    # Nearest recorded note. With a dense chromatic seed set this is <= 1 semitone,
    # so no audible resampling smear (a sparse set was what blurred the guitar).
    nearest = min(seeds, key=lambda k: abs(k - eff))
    y = seeds[nearest][0]
    # Gate the noise tail BEFORE resampling so the fade is in source time.
    y = _fade_tail(y[: _note_end(y, sr)], sr)
    ratio = tc.midi_to_hz(eff) / tc.midi_to_hz(nearest)
    out = _resample(y, ratio)

    # NORMALISE FIRST, then lengthen by looping the note's own body.
    #
    # The previous version zero-padded to `length_s` and then normalised, which
    # dropped the noise floor to -240 dB numerically, so the audible noise that
    # survived was a *-39.7 dB frame floor* in the finished audition against
    # -56.1 dB in the raw recording: the padding let the peak stage amplify
    # quiet tails by ~16 dB (measured). Normalising before lengthening keeps the
    # floor where the recording put it, and looping real audio instead of adding
    # digital silence means there is nothing for hiss to sit in.
    peak = np.abs(out).max() + 1e-9
    out = out / peak
    need = int(length_s * sr)
    if len(out) < need:
        body = out[max(0, len(out) // 5):]        # skip the attack when looping
        if body.size < 64:
            body = out
        xf = int(0.04 * sr)
        while len(out) < need:
            if 4 < xf < len(out) and len(body) > xf:
                fade = np.linspace(0, 1, xf)
                head = out[-xf:] * (1 - fade) + body[:xf] * fade
                out = np.concatenate([out[:-xf], head, body[xf:]])
            else:
                out = np.concatenate([out, body])
    else:
        out = out[:need]
    # --- NO SYNTHETIC EQ ------------------------------------------------
    # Earlier versions stacked a 2.2x presence boost (+ air shelf) on top of the
    # recording to make it "clearer". That IS a resonant filter and is exactly
    # what made a real guitar read as 电子音 (synthetic). Agreed direction: ship
    # the honest recording.
    #
    # Only a gentle de-click remains, because that removes a defect (edge
    # discontinuity measured at 18.7% of peak) rather than adding colour.
    out = _declick_edges(out, sr, ms=10.0)

    peak = np.abs(out).max() + 1e-9
    return out / peak * tc.from_db(-1.0)


# Retained as named knobs at unity so the shape of the chain is visible and a
# future decision to add colour is explicit rather than buried in arithmetic.
CFG_PRESENCE = 0.0       # DISABLED: caused the synthetic '电子音' character
CFG_AIR = 0.0            # DISABLED: same reason


def _declick_edges(y, sr, ms=10.0):
    """Equal-power fade at both edges to kill attack/splice discontinuities."""
    n = int(ms / 1000.0 * sr)
    y = np.asarray(y, dtype=np.float64).copy()
    if n > 2 and y.size > 2 * n:
        y[:n] *= np.linspace(0.0, 1.0, n) ** 0.5
        y[-n:] *= np.linspace(1.0, 0.0, n) ** 0.5
    return y


def match_eq_to_source(y, source_ltas, max_db=2.0):
    """Per-song spectral blend that is UNITY by construction.

    AGGRESSIVE EQ IS DISABLED: the previous version tilted up to +-9 dB per band
    and capped boosts at +3 dB, which was audible as synthetic coloration on a
    real recording. Agreed direction is "real samples, no aggressive EQ", so the
    clamp is now +-2 dB - a gentle blend, never a re-voice.

    The bands partition the signal exactly:
        low  = LPF(f_lo); rest = y - low
        mid  = LPF(f_hi) applied to rest;  high = rest - mid
    so low + mid + high == y at unity gain and the EQ cannot notch or hollow.
    """
    if source_ltas is None:
        return y
    sr = tc.SR
    seed_ltas = tc.octave_band_ltas(y, sr)
    diff = np.clip(np.asarray(source_ltas, float) - np.asarray(seed_ltas, float),
                   -max_db, max_db)
    g1 = float(np.mean(diff[:2]))
    g2 = float(np.mean(diff[2:5]))
    g3 = float(np.mean(diff[5:]))
    from scipy.signal import butter, sosfilt
    f_lo, f_hi = 250.0 / (sr / 2), 2000.0 / (sr / 2)
    low = sosfilt(butter(2, f_lo, "low", output="sos"), y)
    rest = y - low
    mid = sosfilt(butter(2, f_hi, "low", output="sos"), rest)
    high = rest - mid

    # Gentle +-2 dB clamp, and boosts capped at +1.5 dB so a per-song blend can
    # never lift the recording's noise floor (a +3 dB boost measurably raised
    # flatness 3.3x and was heard as '沙沙声').
    g1 = min(g1, 1.5); g2 = min(g2, 1.5); g3 = min(g3, 1.5)

    out = low * tc.from_db(g1) + mid * tc.from_db(g2) + high * tc.from_db(g3)
    return _soft_gate(out)


def _soft_gate(y, sr=None, floor_rel_db=-42.0, depth=0.85):
    """Attenuate the near-silent tail only (kills residual hiss, keeps the note).

    A expander-style gate: frames more than `floor_rel_db` below the loudest
    frame are faded toward zero by `depth`. This removes amp/room noise between
    notes without touching the note body or its natural decay.
    """
    y = np.asarray(y, dtype=np.float64)
    if y.size == 0:
        return y
    hop, win = 256, 1024
    n = max(1, (y.size - win) // hop)
    rms = np.array([np.sqrt((y[i * hop:i * hop + win] ** 2).mean() + 1e-15)
                    for i in range(n)])
    if rms.size == 0:
        return y
    dbv = 20.0 * np.log10(rms + 1e-12)
    peak = float(dbv.max())
    thr = peak + floor_rel_db
    # 2-smooth frame gain, then interpolate to sample rate.
    gain_f = np.clip((dbv - thr) / 6.0, 0.0, 1.0)      # soft knee over 6 dB
    gain_f = 1.0 - depth * (1.0 - gain_f)
    centres = (np.arange(n) * hop + win / 2.0)
    gains = np.interp(np.arange(y.size), centres, gain_f,
                      left=gain_f[0] if n else 1.0, right=gain_f[-1] if n else 1.0)
    return y * gains


def build_real_keyboard(kind, keyboard, out_dir, sr=tc.SR, source_ltas=None,
                        song_slug="realseed"):
    lo, hi = tc.key_range(keyboard)
    os.makedirs(out_dir, exist_ok=True)
    seeds = load_seeds(kind)
    samples = {}
    for key in range(lo, hi + 1):
        y = render_key(seeds, key, sr=sr, kind=kind)
        y = match_eq_to_source(y, source_ltas)
        peak = np.abs(y).max() + 1e-9
        y = y / peak * tc.from_db(-1.0)
        fname = f"k{key:03d}.wav"
        tc.save_wav16(os.path.join(out_dir, fname), y, sr)
        samples[key] = fname
    sfz = os.path.join(out_dir, f"{song_slug}.sfz")
    _sfz(sfz, samples, keyboard)
    return {"sfz": sfz, "samples": samples, "n_keys": len(samples), "kind": kind,
            "octave_shift": OCTAVE_SHIFT.get(kind, 0)}


def _sfz(path, samples, keyboard):
    lines = [f"// real recorded {keyboard} dry seed (Karoryfer, CC)", "<global>",
             "amp_veltrack=45", "fil_type=lpf_2p", "cutoff=5000",
             "fil_veltrack=2000", "ampeg_release=0.25", "<group>"]
    for key in sorted(samples):
        lines.append(f"<region> sample={samples[key]} pitch_keycenter={key} "
                     f"lokey={key} hikey={key} loop_mode=one_shot")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
