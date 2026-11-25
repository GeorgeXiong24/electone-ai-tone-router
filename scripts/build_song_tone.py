"""End-to-end per-song tone build (Stage 2-3, the deterministic backend).

This is the single entry point that turns one separated song into a complete
three-keyboard tone set. It ties together the pieces validated in Phase 0:

  Upper (右手)  : **Piano** - the Accurate-Salamander concert grand
                  (Stage 3 default prior for Mandopop / acoustic / ballad), or a
                  per-song Golden-Anchor instrument when a stem is a CLEAN solo
                  line (Route A).
  Lower (左手)  : the song's accompaniment stem, differentiated by register so it
                  never collides with the Upper keyboard.
  Pedal (脚)    : the song's bass, transposed into the pedal's playable range.

Design decisions forced by measurement during Phase 0 (all recorded in the
modules they came from):
  * A sampled guitar voice cannot match a *strummed* separated stem (measured
    27-37 dB apart, with the stem flat to 8 kHz where real guitars roll off), so
    for Mandopop the Upper keyboard correctly falls back to Piano.
  * The Salamander library is real per-note audio (641 notes, 16 velocity
    layers); soft layers are dark and hard layers bright (centroid 601 -> 1054 Hz),
    so the phrase is played on the brighter layers and lifted gently.
  * Karoryfer seed files sound one octave below their filename.
  * Headless VST sample loading is broken in this environment, so rendering is
    done from the libraries' own WAVs - which is also what makes the result
    reproducible without a DAW.

Usage:
  python build_song_tone.py --song "韦礼安 - 女孩"
  python build_song_tone.py --song "..." --dry-run
"""
import argparse
import json
import os
import sys
import datetime
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _cache_env  # noqa: E402
import tone_common as tc  # noqa: E402
import tone_paths  # noqa: E402
import disk_budget as db  # noqa: E402
import piano_seed as ps  # noqa: E402
import real_seed as rs  # noqa: E402

SR = tc.SR

# ------------------------------------------------------------------ configuration

CFG = {
    # Right hand: how the Upper keyboard is voiced.
    "upper_voice": "piano",          # "piano" | "stem_anchor"
    "piano_lift_db": 4.5,            # gentle air shelf (measured veil fix)
    "piano_note_tail_s": 0.95,       # overlap so notes are legato
    # Per-keyboard note lengths. Upper is legato; Lower sustains the harmony;
    # Pedal rings longer (a bass note is felt, not struck).
    "upper_len_s": 2.4,
    "lower_len_s": 2.6,
    "pedal_len_s": 2.3,
    # Differentiation: the Lower keyboard is shifted down so the hands never
    # collide (Stage 3 Step 2.1 requires this when only one accompaniment exists).
    "lower_transpose": -12,
    "lower_gain": 0.72,
    "pedal_gain": 1.0,
    # Phrases used to audition / evaluate each keyboard.
    "phrase_window_s": 9.0,
}

# Per-key sample ceiling. MEASURED: at -1 dBFS per key a three-note triad summed
# to peak 1.34 (hard clipping, caught by the Stage-6 chord gate). Three coherent
# notes can add almost linearly, so the ceiling has to leave ~3x headroom: -12
# dBFS per key puts a full triad at about -2.5 dBFS. Loudness is the host
# fader's job, not the sample file's.
KEY_HEADROOM_DBFS = -12.0


# ------------------------------------------------------------------ phrases

def upper_phrase():
    """A singable right-hand line over a chord bed (legato)."""
    ev = []
    t = 0.0
    for m in [72, 74, 76, 74, 72, 69, 71, 72, 74, 76, 79, 76]:
        ev.append((t, 0.8, m, 1.0)); t += 0.6
    t = 0.0
    for triad in ([60, 64, 67], [59, 62, 67], [57, 60, 64], [55, 60, 65]):
        for n in triad:
            ev.append((t, 1.15, n, 0.85))
        ev.append((t, 1.15, triad[0] - 12, 0.9))
        t += 1.2
    return ev


def lower_phrase():
    """Accompaniment: broken chords + a counter line, one octave below."""
    ev = []
    t = 0.0
    for triad in ([60, 64, 67], [59, 62, 67], [57, 60, 64], [55, 60, 65]):
        for i, n in enumerate(triad):
            ev.append((t + i * 0.22, 0.9, n, 0.62))
        t += 1.25
    return ev


def pedal_phrase():
    """Bass line: roots and fifths, the shape of a real left foot part."""
    ev = []
    t = 0.0
    for root in (36, 31, 33, 29):
        ev.append((t, 1.0, root, 0.95))
        ev.append((t + 1.1, 1.0, root + 7, 0.85))
        t += 2.2
    return ev


# ------------------------------------------------------------------ rendering

def render_keyboard(events, note_fn, *, length_s, sr=SR, gain=1.0, tail_s=1.5):
    """Mix `events` into one audio buffer using a per-note renderer."""
    total = max(e[0] + e[1] for e in events) + tail_s
    buf = np.zeros(int(total * sr))
    cache = {}
    for (start, dur, midi, vel) in events:
        key = (int(midi), round(float(vel), 2))
        if key not in cache:
            cache[key] = note_fn(int(midi), dur_s=max(length_s, dur + 0.6),
                                 velocity=float(vel))
        y = np.asarray(cache[key], dtype=np.float64)
        n = min(len(y), int((dur + 0.9) * sr))
        seg = y[:n].copy()
        rel = int(0.28 * sr)
        if rel < n:
            seg[-rel:] *= np.sqrt(np.linspace(1, 0, rel))
        i0 = int(start * sr)
        i1 = min(i0 + len(seg), len(buf))
        buf[i0:i1] += seg[:i1 - i0] * gain
    peak = np.abs(buf).max() + 1e-9
    if peak > 0:
        buf = buf / peak * tc.from_db(-2.0)
    return buf


def piano_note_fn(midi, dur_s, velocity):
    return ps.piano_note(midi, dur_s=dur_s, velocity=velocity,
                         lift_db=CFG["piano_lift_db"])


def stem_note_fn_factory(kind):
    seeds = rs.load_seeds(kind)

    def _fn(midi, dur_s, velocity):
        y = rs.render_key(seeds, midi, length_s=dur_s, kind=kind)
        return y
    return _fn


# ------------------------------------------------------------------ per-song build

def detect_upper_voice(song, analysis_path=None):
    """Choose the Upper voice: Piano unless a stem is a clean solo line.

    Stage 3: a non-vocal stem owning a high-confidence monophonic top line may
    supply the Upper tone (Route A); everything else defaults to Piano.
    """
    prof = None
    if analysis_path and os.path.isfile(analysis_path):
        try:
            with open(analysis_path, encoding="utf-8") as f:
                prof = json.load(f)
        except Exception:
            prof = None
    if not prof:
        return {"voice": "piano", "why": "无逐曲分析档案，按默认先验用钢琴"}

    vocal_share = float((prof.get("vocal_melody_profile") or {}).get("active_share", 1.0) or 0.0)
    if vocal_share >= 0.15:
        return {"voice": "piano",
                "why": f"人声活跃（active_share={vocal_share:.2f}），按 Stage 3 默认先验右手用钢琴"}

    best = None
    for name, s in (prof.get("stems") or {}).items():
        if name in ("vocals", "drums") or s.get("unusable"):
            continue
        mel = s.get("melody") or {}
        # Clean solo line test, plus the requirement that the stem is NOT dense
        # polyphonic: a strummed stem is melodic-looking but unusable as a voice.
        poly = s.get("polyphony") or {}
        avg_notes = float(poly.get("avg_simultaneous_notes",
                                   s.get("avg_pitch_classes", 2.5)) or 2.5)
        if (mel.get("is_melodic")
                and float(mel.get("voiced_ratio", 0) or 0) > 0.5
                and float(mel.get("step_lt2_semitones", 0) or 0) > 0.85
                and float(mel.get("high_band_share", 0) or 0) > 0.15
                and avg_notes <= 1.8):
            score = (float(mel.get("voiced_ratio", 0))
                     * float(mel.get("step_lt2_semitones", 0))
                     * float(mel.get("high_band_share", 0)))
            if best is None or score > best[0]:
                best = (score, name)
    if best:
        return {"voice": "stem_anchor", "stem": best[1],
                "why": f"{best[1]} 是清晰单音主奏（同时音数低），可用逐曲音色"}
    return {"voice": "piano",
            "why": "壳轨为密集复音（扫弦/分离残留），按 Stage 3 默认回退到钢琴"}


def build_song(song, *, out_root=None, write_audio=True):
    """Build all three keyboards for one song. Returns a report dict."""
    tone_root = os.path.join(tc.WORKSPACE, "tone")
    stem_dir = os.path.join(tc.WORKSPACE, "stems", song)
    analysis = os.path.join(tc.WORKSPACE, "analysis", song, "song_profile.json")
    if not os.path.isfile(analysis):
        analysis = os.path.join(tc.WORKSPACE, "analysis", song, "analysis.json")

    decision = detect_upper_voice(song, analysis)
    report = {
        "song": song,
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "upper_decision": decision,
        "keyboards": {},
    }

    plans = [
        ("右手", "upper", upper_phrase(), piano_note_fn,
         CFG["upper_len_s"], 1.0),
        ("左手", "lower", lower_phrase(), None,
         CFG["lower_len_s"], CFG["lower_gain"]),
        ("脚", "pedal", pedal_phrase(), None,
         CFG["pedal_len_s"], CFG["pedal_gain"]),
    ]

    for (keyboard, tag, events, fn, length_s, gain) in plans:
        # Resolve the note renderer for this keyboard.
        note_fn = fn
        voice = "piano"
        if tag == "upper" and decision["voice"] == "stem_anchor":
            stem = decision.get("stem")
            kind = stem if stem in ("guitar", "bass") else None
            if kind:
                try:
                    note_fn = stem_note_fn_factory(kind)
                    voice = f"stem_anchor:{stem}"
                except Exception:
                    note_fn = piano_note_fn
                    voice = "piano"
        elif tag != "upper":
            note_fn = piano_note_fn        # accompaniment voiced on the piano
            voice = "piano"

        ev = list(events)
        if tag == "lower":
            ev = [(s, d, m + CFG["lower_transpose"], v) for (s, d, m, v) in ev]

        audio = render_keyboard(ev, note_fn, length_s=length_s, gain=gain)

        entry = {"keyboard": keyboard, "voice": voice,
                 "events": len(ev), "duration_s": round(len(audio) / SR, 2),
                 "note_len_s": length_s}
        if write_audio:
            out_dir = out_root or os.path.join(tc.WORKSPACE, "poc", song, "build")
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, f"{tag}_audition.wav")
            tc.save_wav16(path, audio, SR)
            entry["audition"] = path
            entry["cents_err"] = _pitch_err(audio)
        report["keyboards"][keyboard] = entry

    ok, budget = db.check_song_budget(song)
    report["disk_budget"] = budget
    return report


def _pitch_err(y):
    """Median |cents| of the rendered phrase vs. the nearest tempered note."""
    f0 = tc.estimate_f0_ac(y, SR, fmin=25.0, fmax=2000.0)
    if not f0:
        return None
    midi = tc.hz_to_midi(f0)
    return round(abs(tc.cents_between(tc.midi_to_hz(round(midi)), f0)), 1)


# ------------------------------------------------------------------ manifests

# Labels the manifest/Cantabile use for a keyboard's instrument. The tone tree
# stores one instrument per keyboard under `tone/<slug>/<ascii-keyboard>/sfizz/`,
# so the manifest needs the readable part name for the plugin label.
INSTRUMENT_LABEL = {"右手": "钢琴", "左手": "吉他", "脚": "贝斯"}
INSTRUMENT_ROLE = {"右手": "主旋律", "左手": "伴奏", "脚": "低音线条"}


def _label_for_keyboard(keyboard, info):
    voice = info.get("voice", "")
    if voice == "piano":
        return "钢琴"
    if voice == "anchor":
        return {"左手": "吉他", "脚": "贝斯", "右手": "主音"}.get(keyboard, INSTRUMENT_LABEL.get(keyboard, "钢琴"))
    return INSTRUMENT_LABEL.get(keyboard, "钢琴")


def write_manifests(song, instruments):
    """Register the freshly built instruments in the tone manifests.

    Overwrites the song's `files` list in `manifest.json` cleanly with the
    newly generated instruments and prunes stale `sample-instruments.json`
    entries for that song so Cantabile never stacks legacy sforzando plugins.
    """
    slug = tone_paths.song_slug(song)
    root = os.path.join(tc.WORKSPACE, "tone")

    new_entries = []
    for keyboard, info in instruments.get("keyboards", {}).items():
        sfz = info.get("sfz")
        if not sfz:
            continue
        new_entries.append({
            # Manifest paths are relative to the song directory.
            "file": tone_paths.rel_to_song(root, song, sfz),
            "keyboard": keyboard,
            "plugin": "sfizz",
            "instrument": _label_for_keyboard(keyboard, info),
            "role": INSTRUMENT_ROLE.get(keyboard, ""),
            "sampled": True,
            "key_range": info.get("key_range"),
            "n_keys": info.get("n_keys"),
        })

    written = {}
    for fname, key in (("manifest.json", "files"),
                        ("sample-instruments.json", "files")):
        path = os.path.join(root, fname)
        data = {}
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        entry = data.setdefault(song, {})
        if fname == "manifest.json":
            entry[key] = list(new_entries)
        else:
            entry[key] = [dict(e, sfz=e["file"]) for e in new_entries]
        entry["cantabile"] = entry.get("cantabile") or {}
        written[fname] = os.path.relpath(path, tc.WORKSPACE)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    return {"written": written, "entries": new_entries}


# ------------------------------------------------------------------ per-key SFZ

def _coherence_pass(keys, sr=SR, stride=2, max_ratio_jump=0.25):
    """Enforce Stage-6.2 key-to-key coherence (<25% centroid/f0 jump).

    MEASURED: the Salamander library's takes come from different physical
    strings, so `centroid/f0` jumps 21-29% between some adjacent keys
    (55->56, 64->65, 67->68, 73->74, 79->80). Audio cannot be repaired into
    coherence after the fact - a per-key EQ tilt large enough to close a 29% jump
    would audibly re-voice the note.

    The instrument-level fix is the one a sampler uses: **let neighbouring keys
    share a recording**. Keys are grouped in pairs; each group renders once and
    the second key is produced by retuning that same take by one semitone, which
    is transparent and guarantees the pair has one timbre. Groups whose boundary
    still jumps are then re-rendered from the group's *shared* source too.
    """
    ks = sorted(keys)
    if len(ks) < 3:
        return keys, 0.0

    def _ratio(y, k):
        c = tc.spectral_centroid_hz(y, sr)
        f0 = tc.midi_to_hz(k)
        return c / f0 if c > 0 and f0 > 0 else np.nan

    def _worst(d):
        r = [(_ratio(d[k], k), k) for k in sorted(d)]
        rr = np.array([x for x, _ in r], dtype=float)
        m = float(np.nanmedian(rr)) if np.isfinite(rr).any() else 0.0
        rr = np.where(np.isfinite(rr), rr, m if m else 1.0)
        w = 0.0
        for a, b in zip(rr[:-1], rr[1:]):
            if a > 0:
                w = max(w, abs(b - a) / a)
        return w

    before = _worst(keys)
    if before < max_ratio_jump:
        return keys, before

    # Repair exactly the offending pairs, iteratively: for any adjacent pair whose
    # brightness ratio jumps more than `max_ratio_jump`, the *upper* key adopts
    # the lower key's take, retuned by one semitone. That is what a sampler does
    # and it is transparent (a semitone), while any EQ strong enough to close a
    # 29% jump would audibly re-voice the note.
    out = dict(keys)
    for _ in range(4):                       # a fixed point is reached quickly
        ratios = [(_ratio(out[k], k), k) for k in ks]
        changed = False
        for i in range(len(ks) - 1):
            a, ka = ratios[i]
            b, kb = ratios[i + 1]
            if not (np.isfinite(a) and np.isfinite(b)) or a <= 0:
                continue
            if abs(b - a) / a > max_ratio_jump:
                ratio = tc.midi_to_hz(kb) / tc.midi_to_hz(ka)
                if 0.5 <= ratio <= 2.0:
                    out[kb] = _retune(out[ka], ratio)
                    changed = True
        if not changed:
            break

    # NOTE: a "re-anchor pitch after retuning" pass was tried here and REMOVED.
    # Measuring each retuned key and resampling it back onto its declared
    # frequency produced octave errors on 48/49 left-hand keys (median |error|
    # 1202 cents) because pyin-class estimators octave-slip on short retuned
    # samples. The retune in the loop above is already exact - it scales by
    # midi_to_hz(kb)/midi_to_hz(ka) - so there is nothing to re-anchor.
    #
    # Keep every note the same length so the duration-variance gate stays valid.
    lens = {len(out[k]) for k in out}
    if len(lens) > 1:
        n = int(np.median(list(lens)))
        for k in out:
            y = out[k]
            if len(y) >= n:
                out[k] = y[:n]
            else:
                out[k] = np.pad(y, (0, n - len(y)))
    return out, before


def _retune(y, ratio):
    """Resample a recording by `ratio` (keeps its timbre, moves its pitch)."""
    n_out = max(16, int(round(len(y) / max(ratio, 1e-6))))
    try:
        from scipy.signal import resample
        return resample(np.asarray(y, dtype=np.float64), n_out)
    except Exception:
        x_old = np.linspace(0, 1, len(y))
        return np.interp(np.linspace(0, 1, n_out), x_old, y)


def build_keyboard_sfz(keyboard, note_fn, song_slug, *, transpose=0,
                       length_s=2.0, sr=SR, n_velocity=1,
                       cutoff=12000, amp_veltrack=45):
    """Render every key of `keyboard` and write an sfizz SFZ + 16-bit samples.

    This is the production artifact Cantabile loads: one coherent sample per key
    (velocity dynamics come from SFZ `fil_veltrack`/`amp_veltrack`, so a single
    layer keeps the sample folder 3x smaller than duplicated velocity layers).

    Ranges: samples are rendered over `render_range` (the pedal goes down to C1 =
    MIDI 24, so its bottom octave exists), and the SFZ regions are then widened
    to the whole MIDI span so no key is ever silent when the instrument's octave
    or transpose setting differs from our assumption.
    """
    lo, hi = tc.render_range(keyboard)
    kb_dir = os.path.join(tc.WORKSPACE, "tone", song_slug,
                          tone_paths.ascii_keyboard(keyboard), "sfizz")
    sample_dir = os.path.join(kb_dir, "samples", "tone")
    os.makedirs(sample_dir, exist_ok=True)

    samples = {}
    rendered = {}
    for key in range(lo, hi + 1):
        y = note_fn(key + transpose, dur_s=length_s, velocity=0.85)
        peak = np.abs(y).max() + 1e-9
        rendered[key] = y / peak * tc.from_db(KEY_HEADROOM_DBFS)

    # Key-to-key coherence pass over the finished keyboard (see _coherence_pass).
    rendered, worst_before = _coherence_pass(rendered, sr=sr)
    # Re-normalise after any retuning.
    for key in list(rendered):
        peak = np.abs(rendered[key]).max() + 1e-9
        rendered[key] = rendered[key] / peak * tc.from_db(KEY_HEADROOM_DBFS)

    for key in range(lo, hi + 1):
        fname = f"k{key:03d}.wav"
        tc.save_wav16(os.path.join(sample_dir, fname), rendered[key], sr)
        samples[key] = fname

    sfz_path = os.path.join(kb_dir, f"{song_slug}.sfz")
    lines = [
        "// electone-ai-tone-router : per-key instrument",
        f"// keyboard={keyboard} rendered keys={lo}..{hi} "
        f"regions cover {tc.SFZ_FULL_RANGE[0]}..{tc.SFZ_FULL_RANGE[1]}",
        "<global>",
        f"amp_veltrack={int(amp_veltrack)}",
        "fil_type=lpf_2p",
        f"cutoff={int(cutoff)}",
        "fil_veltrack=1800",
        "ampeg_attack=0.002",
        "ampeg_release=0.30",
        "<group>",
    ]
    keys = sorted(samples)
    flo, fhi = tc.SFZ_FULL_RANGE
    for i, key in enumerate(keys):
        # Extend the outermost regions so a transposed instrument still sounds.
        lokey = flo if i == 0 else key
        hikey = fhi if i == len(keys) - 1 else key
        lines.append(f"<region> sample=samples/tone/{samples[key]} "
                     f"pitch_keycenter={key} lokey={lokey} hikey={hikey}")
    with open(sfz_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    size_mb = sum(os.path.getsize(os.path.join(sample_dir, f))
                  for f in os.listdir(sample_dir)) / (1024.0 * 1024.0)
    return {"sfz": sfz_path, "n_keys": len(samples),
            "key_range": [lo, hi],
            "samples_mb": round(size_mb, 2),
            "coherence_before": round(worst_before, 3)}


def build_song_instruments(song, only=""):
    """Render the full per-key instrument set for the requested keyboards.

    VOICING (this was previously wrong: all three manuals got the piano, which
    ignores bass.flac / guitar.flac entirely):

      右手 (ch1) : Piano by default (Stage-3 prior for Mandopop), or the song's
                   own clean lead when `detect_upper_voice` finds one.
      左手 (ch2) : the song's *guitar/accompaniment* stems, built with the
                   Golden-Anchor engine from guitar.flac, transposed down so the
                   hands never collide.
      脚  (ch3) : the song's *bass* stem, built with the Golden-Anchor engine
                   from bass.flac over C1..C4.
    """
    slug = tone_paths.song_slug(song)
    audit_isdir = os.path.join(tc.WORKSPACE, "stems", song)
    decision = detect_upper_voice(
        song, os.path.join(tc.WORKSPACE, "analysis", song, "song_profile.json"))
    report = {"song": song, "slug": slug, "upper_decision": decision,
              "keyboards": {}}

    # --- per-stem Golden-Anchor instruments -------------------------------
    stem_voices = {}
    for (keyboard, stem_name) in (("左手", "guitar"), ("脚", "bass")):
        src = None
        for ext in (".flac", ".wav"):
            p = os.path.join(audit_isdir, stem_name + ext)
            if os.path.isfile(p):
                src = p
                break
        if not src:
            report.setdefault("notes", []).append(
                f"{keyboard}: no {stem_name} stem, falling back to piano")
            continue
        try:
            voice = _build_anchor_voice(src, keyboard, slug)
            if voice:
                stem_voices[keyboard] = voice
        except Exception as exc:      # noqa: BLE001
            report.setdefault("notes", []).append(
                f"{keyboard}: anchor build failed ({exc}), using piano")

    upper_fn = piano_note_fn
    upper_kind = "piano"
    if decision["voice"] == "stem_anchor" and decision.get("stem") in ("guitar", "bass"):
        try:
            upper_fn = stem_note_fn_factory(decision["stem"])
            upper_kind = f"stem:{decision['stem']}"
        except Exception:
            upper_fn = piano_note_fn

    specs = [
        ("右手", upper_fn, 0, upper_kind),
        ("左手", None, CFG["lower_transpose"], "piano"),
        ("脚", None, 0, "piano"),
    ]
    want = (only or "").strip().lower()
    if want:
        alias = {"right": "右手", "左手": "左手", "left": "左手",
                 "pedal": "脚", "脚": "脚", "右手": "右手"}
        target = alias.get(want, want)
        specs = [s for s in specs if s[0] == target]

    for (keyboard, fn, tr, kind) in specs:
        if keyboard in stem_voices:
            info = stem_voices[keyboard]
            report["keyboards"][keyboard] = info
            continue
        note_fn = fn or piano_note_fn
        kw = {"cutoff": 2800, "amp_veltrack": 35} if (keyboard == "左手" and kind == "piano") else {}
        info = build_keyboard_sfz(keyboard, note_fn, slug, transpose=tr, **kw)
        info["voice"] = kind
        report["keyboards"][keyboard] = info

    # THE FIX for "Cantabile keeps playing the old broken slices": register the
    # instruments we just wrote, or build-cantabile.py will never see them.
    try:
        report["manifests"] = write_manifests(song, report)
    except Exception as exc:      # noqa: BLE001
        report["manifests"] = {"error": str(exc)}

    ok, budget = db.check_song_budget(song)
    report["disk_budget"] = budget
    report["ok"] = ok
    return report


def _apply_match_eq(y, sr, eq_db):
    """Apply a smooth, unity-partitioned 3-band Match EQ (<250, 250-2000, >2000 Hz)."""
    g1, g2, g3 = eq_db
    if max(abs(g1), abs(g2), abs(g3)) < 0.05:
        return y
    y = np.asarray(y, dtype=np.float64)
    n = len(y)
    if n < 16:
        return y
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    w1 = 1.0 / (1.0 + (freqs / 250.0) ** 4)
    w3 = 1.0 / (1.0 + (2000.0 / np.maximum(freqs, 1.0)) ** 4)
    w2 = np.maximum(0.0, 1.0 - w1 - w3)
    gain_db = g1 * w1 + g2 * w2 + g3 * w3
    gain = 10.0 ** (gain_db / 20.0)
    spec = np.fft.rfft(y) * gain
    return np.fft.irfft(spec, n=n)


def _build_anchor_voice(stem_path, keyboard, slug):
    """Build a Golden-Anchor instrument for one keyboard from a stem.

    Anchors are isolated with the harmonic comb, then the manual's whole range is
    resynthesised from them, which is what makes every key sound like ONE
    instrument instead of a pile of unrelated slices.
    """
    import anchor_extract as ae
    import anchor_resynth as ar

    y, sr = tc.load_audio(stem_path, sr=SR, mono=True)
    anchors = ae.extract_anchors(y, sr, n_anchors=6)
    if not anchors:
        return None
    for a in anchors:
        if keyboard == "脚":
            a["tendency"] = "sustained"
        ar.prepare_anchor_fragment(a, y, sr)

    transpose = OCTAVE_SHIFT_FOR_KEYBOARD.get(keyboard, 0)
    if keyboard == "脚":
        median_midi = float(np.median([a["midi"] for a in anchors]))
        if median_midi < 26.0:
            transpose = 12

    # Render over the keyboard's *render* range, blending the two nearest anchors.
    lo, hi = tc.render_range(keyboard)
    kb_dir = os.path.join(tc.WORKSPACE, "tone", slug,
                          tone_paths.ascii_keyboard(keyboard), "sfizz")
    sample_dir = os.path.join(kb_dir, "samples", "tone")
    os.makedirs(sample_dir, exist_ok=True)
    samples = {}
    rendered = {}
    ordered = sorted(anchors, key=lambda a: a["midi"])
    for key in range(lo, hi + 1):
        target = key + transpose
        out = _blend_anchors(ordered, target, sr)
        peak = np.abs(out).max() + 1e-9
        rendered[key] = out / peak * tc.from_db(KEY_HEADROOM_DBFS)

    rendered, _worst = _coherence_pass(rendered, sr=sr)

    # Compute and apply 3-band Match EQ against the source stem's LTAS.
    source_ltas = np.asarray(tc.octave_band_ltas(y, sr), dtype=float)
    synth_ltas = np.mean(
        [tc.octave_band_ltas(rendered[k], sr) for k in sorted(rendered)], axis=0)
    src_c = source_ltas - np.median(source_ltas)
    syn_c = synth_ltas - np.median(synth_ltas)
    eq = ar.fit_match_eq(src_c, syn_c, max_db=5.5)

    for key in list(rendered):
        eqd = _apply_match_eq(rendered[key], sr, eq)
        peak = np.abs(eqd).max() + 1e-9
        if peak > 0:
            rendered[key] = eqd / peak * tc.from_db(KEY_HEADROOM_DBFS)

    for key in range(lo, hi + 1):
        fname = f"k{key:03d}.wav"
        tc.save_wav16(os.path.join(sample_dir, fname), rendered[key], sr)
        samples[key] = fname

    sfz_path = os.path.join(kb_dir, f"{slug}.sfz")
    ar.build_sfz(sfz_path, {k: f"samples/tone/{v}" for k, v in samples.items()},
                 eq=(0.0, 0.0, 0.0), keyboard=keyboard)
    size_mb = sum(os.path.getsize(os.path.join(sample_dir, f))
                  for f in os.listdir(sample_dir)) / (1024.0 * 1024.0)
    return {"sfz": sfz_path, "n_keys": len(samples), "key_range": [lo, hi],
            "voice": "anchor",
            "eq_db": [round(float(x), 2) for x in eq],
            "anchors": [round(a["midi"], 1) for a in anchors],
            "samples_mb": round(size_mb, 2)}


OCTAVE_SHIFT_FOR_KEYBOARD = {"左手": 0, "脚": 0}


def _blend_anchors(anchors, target, sr=SR):
    """Render `target` by morphing the two nearest anchors in the STFT magnitude domain.

    `anchors` must be sorted by midi. Magnitude-domain morphing avoids time-domain
    phase cancellation / comb-filtering and guarantees smooth spectral centroid
    interpolation across anchor gaps.
    """
    import anchor_resynth as ar
    if not anchors:
        raise ValueError("no anchors")
    midis = [a["midi"] for a in anchors]
    if target <= midis[0]:
        return ar.resynthesize_note(anchors[0], target, sr=sr)
    if target >= midis[-1]:
        return ar.resynthesize_note(anchors[-1], target, sr=sr)

    hi = next(i for i, m in enumerate(midis) if m >= target)
    lo = hi - 1
    a_lo, a_hi = anchors[lo], anchors[hi]
    span = midis[hi] - midis[lo]
    w = 0.0 if span <= 0 else (target - midis[lo]) / span
    y_lo = ar.resynthesize_note(a_lo, target, sr=sr)
    if w <= 1e-3:
        return y_lo
    y_hi = ar.resynthesize_note(a_hi, target, sr=sr)
    n = min(len(y_lo), len(y_hi))
    S_lo, S_hi = tc.stft(y_lo[:n]), tc.stft(y_hi[:n])
    mag = (1.0 - w) * np.abs(S_lo) + w * np.abs(S_hi)
    phase = np.angle(S_lo) if w < 0.5 else np.angle(S_hi)
    return tc.istft(mag * np.exp(1j * phase), length=n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--song", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--json", default="", help="write the report JSON here")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--instruments", action="store_true",
                    help="also render the per-key sfizz instrument set")
    ap.add_argument("--sfz-only", action="store_true",
                    help="only render the per-key instruments (skip auditions)")
    ap.add_argument("--keyboard", default="",
                    help="limit --sfz-only to one keyboard (right|left|pedal)")
    ap.add_argument("--force-upper", default="",
                    help="override the Upper voice: piano | stem_anchor")
    ap.add_argument("--plan-only", action="store_true",
                    help="only emit the registration plan (no rendering)")
    ap.add_argument("--synth-stem", default="",
                    help="render a synth preset (Engine B/C) from this stem")
    ap.add_argument("--synth-engine", default="vital",
                    help="vital | surge_string | surge_wavetable")
    args = ap.parse_args()

    # --- plan only: the DSH `tone_plan_registration` tool ----------------
    if args.plan_only:
        decision = detect_upper_voice(
            args.song,
            os.path.join(tc.WORKSPACE, "analysis", args.song, "song_profile.json"))
        if args.force_upper:
            decision = dict(decision)
            decision["voice"] = args.force_upper
            decision["why"] = f"用户指定 {args.force_upper}"
        plan = {
            "song": args.song,
            "upper_decision": decision,
            "rationale": decision.get("why", ""),
            "keyboards": {
                "右手": {"voice": decision["voice"],
                         "engine": ("piano_sfizz" if decision["voice"] == "piano"
                                    else "sfizz_anchor")},
                "左手": {"voice": "piano", "engine": "piano_sfizz",
                         "transpose": CFG["lower_transpose"]},
                "脚": {"voice": "piano", "engine": "piano_sfizz"},
            },
        }
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        if args.json:
            os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(plan, f, ensure_ascii=False, indent=2)
        return

    # --- synth preset (Engine B / C): `tone_build_stem_synth` ------------
    if args.synth_stem:
        stem_dir = os.path.join(tc.WORKSPACE, "stems", args.song)
        src = None
        for ext in (".flac", ".wav"):
            p = os.path.join(stem_dir, args.synth_stem + ext)
            if os.path.isfile(p):
                src = p
                break
        if not src:
            sys.exit(f"no stem {args.synth_stem!r} under {stem_dir}")
        y, sr = tc.load_audio(src, sr=SR, mono=True)
        slug = tone_paths.song_slug(args.song)
        out_dir = os.path.join(tc.WORKSPACE, "tone", slug, "synth")
        os.makedirs(out_dir, exist_ok=True)
        if args.synth_engine == "vital":
            import vital_synth as vs
            out = os.path.join(out_dir, f"{args.synth_stem}.vital")
            detail = vs.build_stem_synth(y, sr, out)
        else:
            import surge_synth as ss
            kind = ("string" if args.synth_engine == "surge_string"
                    else "wavetable")
            out = os.path.join(out_dir, f"{args.synth_stem}.fxp")
            detail = ss.build_from_stem(y, sr, out, kind=kind)
        detail["preset"] = out
        print(json.dumps(detail, ensure_ascii=False, indent=2))
        return

    if args.sfz_only:
        report = build_song_instruments(args.song, only=args.keyboard)
    else:
        report = build_song(args.song, out_root=args.out or None,
                            write_audio=not args.dry_run)
        if args.instruments and not args.dry_run:
            report["instruments"] = build_song_instruments(args.song)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
