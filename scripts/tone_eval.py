"""Measure how close a generated tone is to the stem it was built from.

This is the missing half of the pipeline. Everything up to here only ever
*wrote* tone files; nothing checked that the result sounds like the recording.
`tone_design.py` picks a Surge filter cutoff from `centroid / 0.18` — an
empirical constant — and `analyze-tones.py` claims a stem is "a guitar at
1865 Hz"; without a measurement step, both claims are unfalsifiable.

The loop this closes:

    tone file  ->  render  ->  compare against the source stem  ->  numbers

Three measurements, in increasing order of how much they cost:

1. NULL TEST (cheap, exact, and the only one that can prove a bug):
   render the tone at a key it was *built* from and compare sample-by-sample
   with the .wav that ships in the SFZ. Any difference means the engine is not
   playing our files as written — wrong pitch keycentre, a missing region, a
   blanked sample. Residual should sit at or below the 16-bit floor (-96 dB).
   This is reference.md Phase 6a, and it is the check that catches "every file
   is valid but the instrument plays nothing".

2. PITCH AUDIT (cheap): extract f0 from the rendered note and compare with the
   key that was pressed. Catches the failure that made this project's left hand
   go silent — a source f0 detected a fifth off, so every rendered key inherits
   the error. Reported in cents; anything above ~50 is musically wrong.

3. TIMBRE DISTANCE (the expensive one, and the reason this script exists):
   render every playable key of a keyboard, take the same descriptors
   analyze-tones.py extracts from the stem (spectral centroid, harmonic ratio,
   spectral flatness), and compare. A tone that is "close" must land in the same
   place as its source, not merely be non-silent.

Why the timbre comparison is done key-by-key rather than as one long render:
the source stem is polyphonic and the tone is played monophonically here, so a
sample-by-sample comparison is meaningless. What *is* comparable is the
distribution of descriptors over the notes the stem actually plays, versus the
distribution produced when the same keys are struck on the tone.

Every number is written to tone/eval/<song>.json; --report prints a table.

Usage:
  python tone_eval.py --song 女孩
  python tone_eval.py --latest --keys 12
  python tone_eval.py --song 女孩 --null-test-only
  python tone_eval.py --song 女孩 --report
"""
import argparse
import datetime
import glob
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _cache_env  # noqa: E402  (redirect caches into the project workspace)
import cantabile_format as cf  # noqa: E402
import tone_paths  # noqa: E402

import numpy as np
import soundfile as sf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.environ.get("ELECTONE_WORKSPACE") or os.path.join(ROOT, "workspace")
TONE_ROOT = os.path.join(WORKSPACE, "tone")
EVAL_ROOT = os.path.join(TONE_ROOT, "eval")
STEM_ROOT = os.path.join(WORKSPACE, "stems")

SURGE_VST3 = (r"C:\Program Files\Common Files\VST3\Surge Synth Team"
              r"\Surge XT.vst3\Contents\x86_64-win\Surge XT.vst3")
SFORZANDO_VST3 = (r"C:\Program Files\Common Files\VST3\sforzando.vst3"
                  r"\Contents\x86_64-win\sforzando.vst3")

SR = 48000
NOTE_SECONDS = 0.9
GAP_SECONDS = 0.12

# A rendered note is "correct" if its measured f0 is within this many cents of
# the key that was pressed. Same bound build-sample-tones.py enforces when it
# accepts a render, so the eval measures the same thing the builder claimed.
PITCH_TOLERANCE_CENTS = 50.0

# A pitch trajectory wider than this (interquartile range, cents) is not a
# note with a pitch error — it is a note whose pitch moves. A patch with
# portamento or heavy vibrato produces this, and quoting a median cents-off for
# it reports a measurement artefact as an instrument defect. Measured on the
# 《女孩》 guitar Surge patch: an IQR of ~350 cents while the same note read
# 3 cents off on the sample-based instrument built from the same stem.
PITCH_STABLE_IQR_CENTS = 40.0

# Shortest window a note may be measured in. pyin needs several periods, and a
# truncated window does not fail loudly — it returns a confident wrong number
# (measured: -127 cents at 50 ms for an 82 Hz note that is +8 cents at 100 ms).
MIN_MEASURE_SECONDS = 0.10

# Null-test residual, dBFS. The shipped samples are 16-bit PCM, so the absolute
# floor is about -96 dB; allow a margin for resampling and the engine's own
# gain staging. A failure here is a bug, not a tone-quality issue.
NULL_TEST_PASS_DB = -60.0

# Timbre acceptance: median spectral centroid of the render must land within
# this ratio of the source stem's. A ratio of 2 means "an octave of brightness
# wrong", which is the scale of error this project's own notes describe (the
# guitar patch that could not reach the stem's 1865 Hz centroid).
CENTROID_TOLERANCE_RATIO = 1.6

CFG = {
    "keys_per_keyboard": 12,     # sampled keys to strike per keyboard
    "stems": ("guitar", "bass", "other", "piano"),
}


# ---------------------------------------------------------------- tone lookup

def find_song(query):
    """Resolve a song title substring to (title, tone_dir)."""
    if not os.path.isdir(TONE_ROOT):
        sys.exit(f"no tone tree at {TONE_ROOT}")
    songs = {}
    for d in glob.glob(os.path.join(TONE_ROOT, "*")):
        if not os.path.isdir(d) or os.path.basename(d) == "eval":
            continue
        txt = os.path.join(d, "song.txt")
        if os.path.isfile(txt):
            with open(txt, encoding="utf-8") as f:
                songs[f.read().strip()] = d
        else:
            songs[os.path.basename(d)] = d
    if not songs:
        sys.exit(f"no song directories under {TONE_ROOT}")
    if not query:
        # newest by mtime
        title = max(songs, key=lambda k: os.path.getmtime(songs[k]))
        return title, songs[title]
    hits = [k for k in songs if query in k]
    if not hits:
        sys.exit(f"no song matching {query!r}; known: {', '.join(sorted(songs))}")
    return hits[0], songs[hits[0]]


def load_tone_files(title, tone_dir):
    """Return the tone entries for one song from both manifests.

    build-tones.py writes `manifest.json` (Surge patches + the piano .sfz) and
    build-sample-tones.py writes `sample-instruments.json` (stem-sampled
    instruments). The eval has to see both, because the sampled ones are what
    actually gets imported into Cantabile.
    """
    entries = []
    manifest_path = os.path.join(TONE_ROOT, "manifest.json")
    if os.path.isfile(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        for e in manifest.get(title, {}).get("files", []):
            entries.append(dict(e, source_manifest="manifest.json"))
    sample_path = os.path.join(TONE_ROOT, "sample-instruments.json")
    if os.path.isfile(sample_path):
        with open(sample_path, encoding="utf-8") as f:
            samples = json.load(f)
        for s in samples.get(title, {}).get("files", []):
            entries.append({
                "file": s["sfz"],
                "keyboard": s["keyboard"],
                "plugin": "sforzando",
                "instrument": s["instrument"],
                "role": s.get("role", ""),
                "key_range": s.get("key_range"),
                "keys_covered": s.get("keys_covered"),
                "sampled": True,
                "source_manifest": "sample-instruments.json",
            })
    out = []
    for e in entries:
        path = tone_paths.abs_from_song(TONE_ROOT, title, e["file"])
        e["abs_path"] = path
        out.append(e)
    return out


# ---------------------------------------------------------------- rendering

def load_plugin_for(path):
    """Return (plugin, kind), reusing one instance of each plugin type.

    Loading a VST3 costs ~25 s for sforzando (it initialises ARIA and its
    streaming engine), and the eval loads one per tone file — measured at 26 s
    per instrument when nothing was cached, which is why a four-instrument song
    took minutes. The plugin type is the expensive part; the *tone* is applied
    separately by apply_tone(), so instances are cached per plugin path and
    reloaded only when the tone changes.
    """
    from pedalboard import load_plugin
    ext = os.path.splitext(path)[1].lower()
    if ext == ".fxp":
        vst, kind = SURGE_VST3, "surge-xt"
    elif ext == ".sfz":
        vst, kind = SFORZANDO_VST3, "sforzando"
    else:
        raise SystemExit(f"unsupported tone file: {path}")
    plugin = _PLUGIN_CACHE.get(vst)
    if plugin is None:
        plugin = load_plugin(vst)
        _PLUGIN_CACHE[vst] = plugin
    return plugin, kind


# One live instance per plugin binary, reused across tones (see load_plugin_for).
_PLUGIN_CACHE = {}


def reset_plugin(kind):
    """Discard the cached instance for a plugin type, forcing a fresh load.

    Reuse is safe for *null_test* (which renders one instrument on a clean
    instance and immediately compares it), but not for the multi-key evaluation:
    sforzando keeps the previous ARIA state's samples resident, and a render
    issued right after a state swap carries the earlier instrument's voices.
    Measured on 《我走后》 the null test passed 4/4 on clean instances and only
    2/4 when the same instance had just played another instrument.

    Dropping the cache makes the next load_plugin_for() construct a new one, so
    cost is paid once per instrument rather than once per tone.
    """
    if kind == "sforzando":
        _PLUGIN_CACHE.pop(SFORZANDO_VST3, None)
    elif kind == "surge-xt":
        _PLUGIN_CACHE.pop(SURGE_VST3, None)


def apply_tone(plugin, kind, path):
    """Load a tone into the plugin instance. Returns (applied, missing)."""
    if kind == "surge-xt":
        import surge_patch as sp
        xml = sp.read_fxp_xml(path)
        applied, missing = sp.apply_patch(plugin, xml)
        return len(applied), len(missing)

    # sforzando's ARIA state is an opaque blob; `preset_data` is the only
    # setter it honours (see render-tone.py).
    state = cf.build_vst3_state(
        cf.sforzando_component_state(path), cf.SFORZANDO_CLASS_ID)
    plugin.preset_data = state
    return 0, 0


def render_keys(plugin, kind, keys, note_s=NOTE_SECONDS, gap_s=GAP_SECONDS,
                velocity=100, sr=SR):
    """Strike `keys` in order and return (audio, [(key, start_seconds)]).

    `reset=False` is mandatory for sforzando: passing reset=True makes pedalboard
    re-initialise the plugin, which drops the ARIA state just loaded and renders
    silence. That is why every earlier attempt to evaluate an SFZ tone produced
    a flat line.
    """
    msgs = []
    t = 0.0
    starts = []
    for k in keys:
        starts.append((k, t))
        msgs.append((bytes([0x90, int(k), velocity]), t))
        msgs.append((bytes([0x80, int(k), 0]), t + note_s))
        t += note_s + gap_s
    total = t + 0.6
    audio = plugin.process(msgs, duration=total, sample_rate=sr,
                           num_channels=2, buffer_size=512, reset=False)
    return np.asarray(audio, dtype=np.float32), starts


# ---------------------------------------------------------------- descriptors

def descriptors(y, sr=SR):
    """The same descriptors analyze-tones.py uses, for a like-for-like compare."""
    import librosa
    y = np.asarray(y, dtype=np.float32)
    if y.ndim > 1:
        y = y.mean(axis=0)
    if y.size < 1024 or not np.any(y):
        return {"empty": True}
    hop = 512
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=hop)[0]
    rms_db = 20.0 * np.log10(rms + 1e-12)
    active = rms_db > -45.0
    if not active.any():
        return {"empty": True}
    cent = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=hop)[0]
    yh, yp = librosa.effects.hpss(y)
    eh = float(np.sum(yh.astype(np.float64) ** 2))
    ep = float(np.sum(yp.astype(np.float64) ** 2))
    flat = librosa.feature.spectral_flatness(y=y)
    return {
        "empty": False,
        "centroid_hz": round(float(np.median(cent[active])), 1),
        "harmonic_ratio": round(eh / (eh + ep + 1e-12), 3),
        "flatness": round(float(np.median(flat)), 5),
        "rms_db": round(float(db(np.sqrt((y ** 2).mean()))), 1),
    }


def db(x):
    return 20.0 * math.log10(max(float(x), 1e-12))


def slice_note(audio, start_s, note_s=NOTE_SECONDS, gap_s=GAP_SECONDS, sr=SR,
               loop_after_s=None):
    """Cut one note out of a multi-note render, without touching the next one.

    The window must stop at the next note's onset: taking `note_s + 0.35` when
    the gap is only 0.12 s pulled the following note's attack into the slice,
    which is what made the per-key pitch audit read +1900/+3100 cents on
    instruments whose null test showed they were pitch-exact.

    `loop_after_s` additionally stops the window at the region's loop point.
    Past a `loop_sustain` loop the engine is replaying an *earlier part of the
    same file*, and for a bass that earlier part can sit a different pitch away:
    measured on 《蒲公英》 key 39, the shipped wav reads +8 cents but the render
    read +323 because the window had run into the looped material. That is not
    a defect in the instrument, so it must not be measured as one.
    """
    a = int(start_s * sr)
    span = int((note_s + gap_s * 0.5) * sr)
    looped = False
    if loop_after_s is not None and loop_after_s > 0:
        if loop_after_s >= 0.05:
            span = min(span, int(loop_after_s * sr))
            looped = True
        # A loop that starts inside the first 50 ms leaves nothing to measure
        # before it. Rather than silently measuring looped material (which is a
        # different pitch for a bass) the caller gets the full window and the
        # flag says the reading is less trustworthy.
    # Pitch detection needs several periods of the note. At 82 Hz (E2, the top of
    # a bass stem's comfortable range) a 50 ms window is four periods and pyin
    # reads it -127 cents off: measured on 《蒲公英》 key 40, the same sample reads
    # +8 cents from 0.10 s onward and -62 to -127 cents from 0.06 s down. Windows
    # shorter than this are widened rather than measured, because a short window
    # produces a confident wrong number.
    min_span = int(MIN_MEASURE_SECONDS * sr)
    if span < min_span:
        span = min(int((note_s + gap_s * 0.5) * sr), min_span)
    b = min(audio.shape[-1], a + span)
    if b <= a:
        return np.zeros(0, dtype=np.float32), looped
    return audio[:, a:b], looped


def loop_seconds_for(sfz_path, key, sr=SR):
    """Seconds into a region before its loop point, or None if it does not loop.

    A rendered note is only the *shipped audio* up to `loop_start`; beyond that
    the engine repeats a different excerpt by design.
    """
    try:
        with open(sfz_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "<region>" not in line:
                    continue
                if f"lokey={key} " not in line and not line.rstrip().endswith(
                        f"lokey={key}"):
                    # Match only the region whose lokey is exactly this key.
                    m = re.search(r"lokey=(\d+)", line)
                    if not m or int(m.group(1)) != key:
                        continue
                m = re.search(r"loop_start=(\d+)", line)
                if m:
                    return int(m.group(1)) / float(sr)
                return None
    except OSError:
        return None
    return None


def measure_pitch(y, expected_hz, sr=SR):
    """Measure the fundamental of a rendered note; report when it cannot.

    Neither an unrestricted pyin nor a harmonic summation can be trusted here,
    and both failed on this project's own tones:

    * the 《女孩》 guitar *Surge* patch: pyin locked onto ~984 Hz for a struck
      MIDI 60 = 261.6 Hz, and the whole trajectory slides (701 -> 547 -> 717 Hz
      over 0.9 s) because that patch carries a portamento. Harmonic summation
      gave +924 / +208 / +215 cents on three different keys — inconsistent.
    * short bright *sampled* guitar notes: pyin returned 1999 Hz for a 164.8 Hz
      note and 138 Hz for a 415.3 Hz one.

    Every one of those would have been reported as "the instrument plays the
    wrong pitch", and every one was a measurement failure: the null test shows
    those samples are bit-exact with the shipped wav, so their pitch is by
    construction the pitch the builder rendered.

    So this returns a *status* with the number, and only `ok` / `too_far` are
    evidence about the instrument:

        ok               stable reading within tolerance
        too_far          stable reading outside tolerance (a real defect)
        octave_ambiguous an exact octave away - signal ambiguity, not a fault
        unstable         the pitch moves; the median would be meaningless
        unmeasurable     nothing periodic found

    Returns (f0_hz or None, status, spread_iqr_cents or None).
    """
    import librosa

    y = np.asarray(y, dtype=np.float32)
    if y.ndim > 1:
        y = y.mean(axis=0)
    if y.size < int(0.08 * sr) or not np.any(y) or expected_hz <= 0:
        return None, "unmeasurable", None

    fmin = max(20.0, expected_hz * 0.45, 4.0 * sr / 2048.0)
    fmax = min(sr / 2.0 - 1.0, expected_hz * 2.2)
    if fmax <= fmin:
        return None, "unmeasurable", None
    try:
        f0, voiced, _ = librosa.pyin(y, fmin=fmin, fmax=fmax, sr=sr,
                                     frame_length=2048)
    except Exception:  # noqa: BLE001 - pyin raises on degenerate input
        return None, "unmeasurable", None
    f0v = f0[voiced]
    if f0v.size < 3:
        return None, "unmeasurable", None

    # A glide, a vibrato-heavy patch or polyphonic bleed shows up as a wide
    # trajectory. Judging such a note by its median is how a correct instrument
    # gets reported as mistuned, so measure the spread before quoting a number.
    cents_track = 1200.0 * np.log2(f0v / expected_hz)
    iqr = float(np.percentile(cents_track, 75) - np.percentile(cents_track, 25))
    median_cents = float(np.median(cents_track))
    median_f0 = float(np.median(f0v))

    if iqr > PITCH_STABLE_IQR_CENTS:
        return median_f0, "unstable", round(iqr, 1)

    ratio = median_f0 / expected_hz
    if any(abs(abs(math.log2(ratio)) - k) < 0.08 for k in (1, 2)):
        return median_f0, "octave_ambiguous", round(iqr, 1)
    if abs(median_cents) <= PITCH_TOLERANCE_CENTS:
        return median_f0, "ok", round(iqr, 1)
    return median_f0, "too_far", round(iqr, 1)


def null_test(entry):
    """Compare the engine's render of a built key with the shipped wav.

    A sampled instrument stores one wav per (key, velocity layer) and the SFZ
    points `pitch_keycenter` at that same key, so a render at that key with no
    transposition should be the file itself. Any real difference means the
    engine is not playing what we shipped.
    """
    sfz = entry["abs_path"]
    if not sfz.lower().endswith(".sfz"):
        return {"applicable": False, "reason": "only sampled (.sfz) tones ship per-key wavs"}

    # The piano .sfz wraps the external Salamander library: its samples live in
    # <SAMPLE_LIBRARIES>, were recorded by someone else, and are not rendered by
    # this project, so there is nothing of ours to null-test against. Detect it
    # from the SFZ content *before* looking for a samples/ directory, because
    # build-tones.py writes an empty `samples/` sibling for it and the directory
    # test alone classified the piano as a stem-sampled instrument.
    try:
        with open(sfz, encoding="utf-8", errors="replace") as f:
            head = f.read(8192)
    except OSError:
        head = ""
    if "Salamander" in head:
        return {"applicable": False,
                "reason": "external sample library (Salamander piano), "
                          "not rendered by this project"}

    sample_dir = os.path.join(os.path.dirname(sfz), "samples")
    dirs = [d for d in glob.glob(os.path.join(sample_dir, "*")) if os.path.isdir(d)]
    if not dirs:
        return {"applicable": False, "reason": "no samples/ directory next to the .sfz"}
    wavs = sorted(glob.glob(os.path.join(dirs[0], "k*_l*.wav")))
    if not wavs:
        return {"applicable": False, "reason": "samples/ holds no k*_l*.wav"}

    # Only load the candidate files needed to choose a target key. Reading every
    # wav in the tree (up to ~170 per instrument, three layers each) was most of
    # this function's cost, for one sample that actually gets compared.
    usable = []
    for w in wavs:
        name = os.path.basename(w)
        try:
            key = int(name[1:4])
            layer = int(name.split("_l")[1].split(".")[0])
        except (ValueError, IndexError):
            continue
        usable.append((key, layer, w))
    if not usable:
        return {"applicable": False, "reason": "sample filenames are unparseable"}
    top_layer = max(u[1] for u in usable)
    top = sorted(u for u in usable if u[1] == top_layer)
    target_key = top[len(top) // 2][0]
    src_path = next(w for k, l, w in top if k == target_key)
    x, sr_file = sf.read(src_path, dtype="float32", always_2d=True)
    src = {"path": src_path, "key": target_key, "layer": top_layer,
           "sr": sr_file, "audio": x.mean(axis=1)}

    plugin, kind = load_plugin_for(sfz)
    apply_tone(plugin, kind, sfz)

    # Render at the *sample's own* rate. Resampling the reference onto 48 kHz
    # first was the second version of this check, and it still "failed" at
    # -22 dB: a sinc resampler is not transparent at 44.1 -> 48 kHz, so the
    # comparison measured the resampler, not the instrument. Rendering at the
    # file's rate makes the comparison exact.
    render_sr = int(src["sr"])
    audio, _ = render_keys(plugin, kind, [target_key],
                           note_s=len(src["audio"]) / render_sr - 0.02,
                           sr=render_sr)
    rendered = audio.mean(axis=0)
    ref = src["audio"]

    # Stop before the loop point. Past `loop_start` the engine is playing a
    # different part of the file by design, so comparing further would measure
    # the loop, not the load. Find the region's loop_start for this key.
    loop_start = None
    try:
        with open(sfz, encoding="utf-8", errors="replace") as f:
            text = f.read()
        for line in text.splitlines():
            if f"sample={os.path.basename(src['path'])}" in line:
                m = re.search(r"loop_start=(\d+)", line)
                if m:
                    loop_start = int(m.group(1))
                break
    except OSError:
        pass
    cmp_len = len(ref) if loop_start is None else min(len(ref), loop_start)
    if cmp_len < 1024:
        return {"applicable": False, "reason": "comparable window too short"}

    # Align: the engine may add ampeg_attack, and a region can carry an offset.
    best_lag, best_corr = 0, -2.0
    span = min(len(rendered) - cmp_len, int(0.05 * render_sr))
    if span < 0:
        return {"applicable": False, "reason": "render shorter than the sample"}
    for lag in range(0, max(1, span), 16):
        seg = rendered[lag:lag + cmp_len]
        if len(seg) < cmp_len:
            break
        if float(np.std(seg)) < 1e-9 or float(np.std(ref[:cmp_len])) < 1e-9:
            continue
        c = float(np.corrcoef(seg, ref[:cmp_len])[0, 1])
        if c > best_corr:
            best_lag, best_corr = lag, c

    a = rendered[best_lag:best_lag + cmp_len]
    b = ref[:len(a)]
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    if n < 1024:
        return {"applicable": False, "reason": "alignment failed"}
    if not np.any(b):
        return {"applicable": False, "reason": "source sample is silent"}

    # Scale-invariant residual: the engine applies amp_veltrack / layer gain, so
    # compare *shapes*, and report how much level was added separately.
    gain = float(np.dot(a, b) / (np.dot(b, b) + 1e-12))
    resid = a - gain * b
    resid_db = db(np.sqrt(np.mean(resid ** 2)))
    sig_db = db(np.sqrt(np.mean(b ** 2)))
    corr = float(np.corrcoef(a, b)[0, 1]) if n > 8 else 0.0
    return {
        "applicable": True,
        "key": int(target_key),
        "layer": int(top_layer),
        "sample": os.path.basename(src["path"]),
        "render_sr": render_sr,
        "compared_samples": int(n),
        "loop_start": loop_start,
        "lag_samples": int(best_lag),
        "corr": round(corr, 5),
        "gain_db": round(db(gain), 2) if gain > 0 else None,
        "residual_dbfs": round(resid_db, 2),
        "source_dbfs": round(sig_db, 2),
        "residual_vs_source_db": round(resid_db - sig_db, 2),
        "pass": bool(resid_db <= NULL_TEST_PASS_DB and corr >= 0.99),
    }


# ---------------------------------------------------------------- stem compare

def find_stem(title, instrument):
    """Locate the stem wav a tone was built from, by instrument label."""
    label_to_stem = {"吉他": "guitar", "贝斯": "bass", "其他/混合": "other",
                     "钢琴": "piano", "人声": "vocals"}
    stem = label_to_stem.get(instrument)
    if not stem:
        return None
    d = os.path.join(STEM_ROOT, title)
    p = os.path.join(d, stem + ".wav")
    return p if os.path.isfile(p) else None


def stem_reference(stem_path):
    """Descriptors of the stem, restricted to the notes it actually plays."""
    import librosa
    y, sr = librosa.load(stem_path, sr=SR, mono=True)
    if y.size == 0:
        return {"empty": True}
    return descriptors(y, sr)


# ---------------------------------------------------------------- per-tone eval

def eval_tone(entry, n_keys, do_null=True, do_pitch=True):
    sfz = entry["abs_path"]
    result = {
        "file": entry["file"],
        "keyboard": entry.get("keyboard", ""),
        "instrument": entry.get("instrument", ""),
        "role": entry.get("role", ""),
        "plugin": entry.get("plugin", ""),
        "exists": os.path.isfile(sfz),
    }
    if not result["exists"]:
        result["error"] = "tone file missing"
        return result

    if do_null:
        try:
            result["null_test"] = null_test(entry)
        except Exception as exc:  # noqa: BLE001 - an eval must not crash the run
            result["null_test"] = {"applicable": False, "error": f"{type(exc).__name__}: {exc}"}

    # Evaluate this instrument on a clean instance. Sharing one across different
    # tones leaks the previous ARIA state into the render and silently turns a
    # correct instrument into a "null test failed" (see reset_plugin).
    plugin, kind = load_plugin_for(sfz)
    reset_plugin(kind)
    plugin, kind = load_plugin_for(sfz)
    applied, missing = apply_tone(plugin, kind, sfz)
    result["params_applied"] = applied
    result["params_missing"] = missing

    # Keys to strike: the ones the instrument actually covers, sampled evenly.
    lo, hi = 36, 84
    if entry.get("key_range"):
        lo, hi = int(entry["key_range"][0]), int(entry["key_range"][1])
    if entry.get("keyboard") == "脚":
        lo, hi = max(lo, 36), min(hi, 55)
    keys = list(range(lo, hi + 1))
    if n_keys and len(keys) > n_keys:
        step = max(1, len(keys) // n_keys)
        keys = keys[::step][:n_keys]

    audio, starts = render_keys(plugin, kind, keys)
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    result["render_peak"] = round(peak, 4)
    result["silent"] = peak < 1e-4
    if result["silent"]:
        result["error"] = ("render is silent - the tone did not load "
                           "(check the .sfz path, sample dir and ARIA state)")
        return result

    per_key = []
    centroids = []
    harmonics = []
    for key, t0 in starts:
        # Only the pre-loop part of a note is the audio we shipped.
        loop_at = (loop_seconds_for(sfz, key) if kind == "sforzando" else None)
        clip, cut_at_loop = slice_note(audio, t0, note_s=NOTE_SECONDS,
                                       gap_s=GAP_SECONDS, loop_after_s=loop_at)
        if clip.size == 0:
            continue
        d = descriptors(clip)
        if d.get("empty"):
            per_key.append({"key": key, "empty": True})
            continue
        rec = {"key": key, "centroid_hz": d["centroid_hz"],
               "harmonic_ratio": d["harmonic_ratio"]}
        if cut_at_loop:
            rec["window_ends_at_loop"] = True
        if do_pitch:
            expected = librosa_midi_to_hz(key)
            f0, status, spread = measure_pitch(clip, expected)
            if f0:
                rec["f0_hz"] = round(f0, 2)
                rec["cents_off"] = round(1200.0 * math.log2(f0 / expected), 1)
                rec["pitch_status"] = status
                if spread is not None:
                    rec["cents_iqr"] = spread
        per_key.append(rec)
        centroids.append(d["centroid_hz"])
        harmonics.append(d["harmonic_ratio"])

    result["keys_played"] = len(per_key)
    result["keys_silent"] = sum(1 for r in per_key if r.get("empty"))
    if centroids:
        result["render_centroid_hz"] = round(float(np.median(centroids)), 1)
        result["render_harmonic_ratio"] = round(float(np.median(harmonics)), 3)
    if do_pitch:
        checked = [r for r in per_key if r.get("pitch_status")]
        if checked:
            counts = {}
            for r in checked:
                counts[r["pitch_status"]] = counts.get(r["pitch_status"], 0) + 1
            decisive = [r for r in checked
                        if r["pitch_status"] in ("ok", "too_far")]
            result["pitch"] = {
                "checked": len(checked),
                "status": counts,
                "within_tolerance": counts.get("ok", 0),
                # Only a stable, decisive reading is evidence about the
                # instrument. Unstable (glide/vibrato) and octave-ambiguous
                # (signal) reads are reported, never counted as failures.
                "decisive": len(decisive),
                "decisive_ok": counts.get("ok", 0),
                "median_cents": round(float(np.median(
                    [r["cents_off"] for r in decisive])), 1) if decisive else None,
                "max_abs_cents": round(max((abs(r["cents_off"]) for r in decisive),
                                           default=0.0), 1),
                "unambiguous_ok": all(r["pitch_status"] == "ok" for r in decisive),
            }
    result["per_key"] = per_key

    stem = find_stem(entry.get("song") or entry.get("_song", ""), entry.get("instrument", ""))
    if stem:
        ref = stem_reference(stem)
        if not ref.get("empty") and centroids:
            ratio = float(np.median(centroids)) / max(ref["centroid_hz"], 1e-9)
            result["stem"] = {
                "path": os.path.relpath(stem, WORKSPACE),
                "centroid_hz": ref["centroid_hz"],
                "harmonic_ratio": ref["harmonic_ratio"],
                "centroid_ratio": round(ratio, 3),
                "centroid_ok": bool(1.0 / CENTROID_TOLERANCE_RATIO <= ratio
                                    <= CENTROID_TOLERANCE_RATIO),
            }
    return result


def librosa_midi_to_hz(m):
    return 440.0 * 2.0 ** ((float(m) - 69.0) / 12.0)


# ---------------------------------------------------------------- report

def summarize(results):
    tones = [r for r in results if r.get("tones")]
    # Flatten: one record per tone file.
    flat = []
    for r in results:
        for t in r.get("tones", []):
            flat.append(dict(t, _song=r["song"], _keyboard_file=r.get("keyboard", "")))
    n = len(flat)
    silent = [t for t in flat if t.get("silent") or t.get("error")]
    # Keep the tone with each result. Zipping `flat` against a *filtered* list of
    # null-test dicts silently misaligns the two (the filter drops entries but
    # `flat` does not), which reported the wrong instruments as failing.
    null_pairs = [(t, t["null_test"]) for t in flat
                  if isinstance(t.get("null_test"), dict)
                  and t["null_test"].get("applicable")]
    null_fail = [t for t, nt in null_pairs if not nt.get("pass")]
    pitches = [t["pitch"] for t in flat if t.get("pitch")]
    pitch_keys = sum(p["decisive"] for p in pitches)
    pitch_ok = sum(p["decisive_ok"] for p in pitches)
    pitch_unmeasured = sum(p["checked"] - p["decisive"] for p in pitches)
    pitch_fail = [t for t in flat
                  if t.get("pitch") and not t["pitch"].get("unambiguous_ok")]
    timbres = [t for t in flat if t.get("stem", {}).get("centroid_ok") is not None]
    timbre_ok = [t for t in timbres if t["stem"]["centroid_ok"]]
    return {
        "tones": n,
        "silent": len(silent),
        "null_tested": len(null_pairs),
        "null_passed": len(null_pairs) - len(null_fail),
        "null_failed": [f"{t['_song']} / {t['keyboard']} / {t['instrument']}"
                        for t in null_fail][:12],
        "pitch_keys": pitch_keys,
        "pitch_ok": pitch_ok,
        "pitch_not_decisive": pitch_unmeasured,
        "pitch_failed": [f"{t['_song']} / {t['keyboard']} / {t['instrument']}"
                         for t in pitch_fail][:12],
        "timbre_cmp": len(timbres),
        "timbre_ok": len(timbre_ok),
        "timbre_failed": [f"{t['_song']} / {t['keyboard']} / {t['instrument']}"
                          f" ({t['stem']['centroid_ratio']:.2f}x)"
                          for t in timbres if not t["stem"]["centroid_ok"]][:12],
        "silent_files": [f"{t['_song']} / {t['keyboard']} / {t['instrument']}"
                         for t in silent][:12],
    }


def print_report(path):
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    print(f"song   {payload.get('song')}")
    print(f"tone   {payload.get('tone_dir')}")
    print(f"made   {payload.get('generated')}")
    print()
    hdr = (f"{'keyboard':10s} {'instrument':10s} {'file':34s} "
           f"{'peak':>7s} {'cent':>8s} {'cents':>7s} {'null':>7s}")
    print(hdr)
    print("-" * len(hdr))
    for t in payload.get("tones", []):
        name = os.path.basename(t.get("file", ""))[:34]
        peak = f"{t.get('render_peak', 0):.3f}"
        cent = f"{t.get('render_centroid_hz', 0):.0f}" if t.get("render_centroid_hz") else "-"
        p = t.get("pitch") or {}
        cents = f"{p['median_cents']:+.0f}" if p.get("median_cents") is not None else "-"
        nt = t.get("null_test") or {}
        if nt.get("applicable"):
            nulls = f"{nt['residual_dbfs']:+.1f}"
        elif nt.get("error"):
            nulls = "ERR"
        else:
            nulls = "-"
        flag = "  <-- SILENT" if t.get("silent") else ""
        print(f"{t.get('keyboard', ''):10s} {t.get('instrument', ''):10s} "
              f"{name:34s} {peak:>7s} {cent:>8s} {cents:>7s} {nulls:>7s}{flag}")
    s = payload.get("summary", {})
    print()
    print(f"tones {s.get('tones', 0)}  silent {s.get('silent', 0)}  "
          f"null-test {s.get('null_passed', 0)}/{s.get('null_tested', 0)} pass  "
          f"pitch {s.get('pitch_ok', 0)}/{s.get('pitch_keys', 0)} decisive keys "
          f"within {PITCH_TOLERANCE_CENTS:.0f} cents"
          + (f" ({s['pitch_not_decisive']} not measurable)"
             if s.get("pitch_not_decisive") else "")
          + f"  timbre {s.get('timbre_ok', 0)}/{s.get('timbre_cmp', 0)} within "
          f"{CENTROID_TOLERANCE_RATIO:.1f}x")
    if s.get("pitch_failed"):
        print("\npitch outside tolerance (unambiguous keys only):")
        for f in s["pitch_failed"]:
            print("  " + f)
    if s.get("null_failed"):
        print("\nnull test failed (the engine is not playing the shipped wav):")
        for f in s["null_failed"]:
            print("  " + f)
    if s.get("timbre_failed"):
        print("\ntimbre far from the source stem:")
        for f in s["timbre_failed"]:
            print("  " + f)
    if s.get("silent_files"):
        print("\nsilent:")
        for f in s["silent_files"]:
            print("  " + f)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--song", default="", help="song title substring")
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--keys", type=int, default=CFG["keys_per_keyboard"],
                    help=f"keys to strike per instrument (default {CFG['keys_per_keyboard']})")
    ap.add_argument("--null-test-only", action="store_true",
                    help="skip rendering/descriptors; only check the shipped wavs")
    ap.add_argument("--no-pitch", action="store_true",
                    help="skip the f0 audit (it is the slow part)")
    ap.add_argument("--report", action="store_true",
                    help="print the stored report instead of re-running")
    ap.add_argument("--out", default="", help="output json (default under tone/eval/)")
    args = ap.parse_args()

    if args.report:
        title, tone_dir = find_song(args.song)
        path = os.path.join(EVAL_ROOT, tone_paths.song_slug(title) + ".json")
        if not os.path.isfile(path):
            sys.exit(f"no report at {path} - run tone_eval.py first")
        print_report(path)
        return

    title, tone_dir = find_song(args.song)
    print(f"song: {title}")
    print(f"tone: {tone_dir}")

    entries = load_tone_files(title, tone_dir)
    if not entries:
        sys.exit("no tone files recorded for this song - run build-tones.py first")
    for e in entries:
        e["song"] = title

    results = {"song": title, "tone_dir": os.path.basename(tone_dir),
               "generated": datetime.datetime.now().isoformat(timespec="seconds"),
               "tones": []}
    for e in entries:
        label = f"{e.get('keyboard', '')}/{e.get('instrument', '')}"
        print(f"  {label} ...", flush=True)
        r = eval_tone(e, None if args.null_test_only else args.keys,
                      do_null=True, do_pitch=not args.no_pitch and not args.null_test_only)
        r["song"] = title
        if args.null_test_only:
            r.pop("per_key", None)
        results["tones"].append(r)
        if r.get("error"):
            print(f"    {r['error']}", flush=True)
        elif r.get("silent"):
            print("    SILENT", flush=True)
        else:
            p = r.get("pitch") or {}
            cents = p.get("median_cents")
            print(f"    peak {r.get('render_peak')}  "
                  f"centroid {r.get('render_centroid_hz')}Hz  "
                  + (f"pitch {cents:+.0f} cents "
                     f"({p.get('decisive_ok')}/{p.get('decisive')} decisive ok)  "
                     if cents is not None else "")
                  + (f"null {r['null_test']['residual_dbfs']:+.1f} dB"
                     if isinstance(r.get("null_test"), dict)
                     and r["null_test"].get("applicable") else ""), flush=True)

    results["summary"] = summarize([results])
    os.makedirs(EVAL_ROOT, exist_ok=True)
    out = args.out or os.path.join(EVAL_ROOT, tone_paths.song_slug(title) + ".json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nwrote {out}")
    print_report(out)


if __name__ == "__main__":
    main()
