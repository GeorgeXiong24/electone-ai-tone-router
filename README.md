# electone-ai-tone-router

> 语言 / Language：[中文](README_zh.md)

Give a song name (or URL) — get a playable performance setup on your **Yamaha ELB-02 (STAGEA)**.
The project downloads the audio, separates it into instrument stems locally, analyzes which
instrument plays the melody / chords / counter-melody, and maps each part to a VST timbre in
Cantabile, one timbre per keyboard (Upper / Lower / Pedal).

## Status

| Stage | State |
| --- | --- |
| Audio download (song name → wav) | Done |
| Stem separation (6 stems, local GPU) | Done |
| Separation failure detection | Done |
| Second-stage refinement (drums → 6 pieces) | Optional (`-RefineDrums`) |
| Live progress reporting | Done |
| Melody / chord / counter-melody analysis | Done |
| Storage archival (24-bit FLAC) | Done |
| Per-song tone engines (Golden-Anchor / sampled / synth) | Done |
| Right-hand voice selection (MIR + fallback gate) | Done |
| Objective evaluation suite (6 hard gates) | Done |
| Cantabile song generation for ELB-02 | Done |
| Automatic Cantabile import | Done |
| DSH plugin `electone-tone-studio` | Done |

## Why the tone engine was rewritten

`improvements.md` records the measured failure modes; each is fixed here and the
measurement that justified it is kept in the code:

| Old problem (measured) | Fix |
| --- | --- |
| 1412.8 MB per song over the 1.2 GB budget (stems 1339.8 MB) | 24-bit lossless FLAC archival → **368.5 MB** |
| Strummed chords sliced onto single keys (4/12 in tune, 2000-cent stretch) | Harmonic-comb isolation + pitch verification; 3–6 Golden Anchors only |
| `cutoff = centroid/0.18` open-loop guessing (9/12 silent keys) | Measure before deciding; the right hand uses real samples / physical modelling |
| Right hand collided with the left (guitar on both) | Stage-3 decision hierarchy + register separation between the hands |
| Plugin could only send 0–1 OSC values and could not evaluate | New 7-tool plugin closes the build → evaluate → refine loop |

**Right-hand verdict (measurement-driven).** On *韦礼安 - 女孩* the guitar stem is
**27–37 dB** away from every real guitar sample and still sits at **−0.4 dB at
8 kHz** (strum + separation bleed), so no sampled guitar can "match" it. Per the
Stage-3 default prior the right hand therefore uses the **Accurate-Salamander
concert grand** (real per-note recordings, 641 keys × 16 velocity layers). Its
"veiled" character was measured and fixed: quiet layers carry no air (key 72 at
velocity 0.15 puts 0.0% of its energy at 2–5 kHz vs 4.8% at velocity 1.0), so the
phrase uses the brighter layers plus a gentle high shelf.

## Evaluation (run after every tone change)

```powershell
.venv\Scripts\python.exe scripts\eval_tones.py --song "韦礼安 - 女孩"
```

| Check | Gate | Current |
| --- | --- | --- |
| Per-key pitch | ≥95% within ±25 cents, zero octave/fifth errors | 100% / 100% / 100% |
| Key-to-key coherence | median adjacent `centroid/f0` jump <25% | 0.9% (library's own max: 174%) |
| Octave-band match | body (125 Hz–1 kHz) median error <4.5 dB | 4.3 / 3.0 / 3.2 dB |
| Polyphonic chord | no clipping, harmonic-series dominance | peak 0.90, no clipping |
| Null test | correlation ≥0.99, residual <−60 dBFS | 1.0000 / −200 dBFS |
| Per-song disk budget | <1.2 GB | **366.9 MB** |

## Full workflow

```powershell
# 1) download + separate + archive (drops to ~370 MB automatically)
scripts\download-audio.ps1 -Query "韦礼安 女孩"

# 2) unified MIR analysis
.venv\Scripts\python.exe scripts\analyze_song.py --song "韦礼安 - 女孩"

# 3) render the three keyboards (piano right hand / register-separated left / bass pedals)
.venv\Scripts\python.exe scripts\build_song_tone.py --song "韦礼安 - 女孩" --sfz-only

# 4) objective evaluation (must be all-PASS before export)
.venv\Scripts\python.exe scripts\eval_tones.py --song "韦礼安 - 女孩"

# 5) export the Cantabile project (with the disk-budget gate)
.venv\Scripts\python.exe scripts\build-cantabile.py --song "韦礼安 - 女孩" --verify
```

## DSH plugin: electone-tone-studio

The two-plugin separation from `improvements.md` Stage 4 is preserved:
`surge-osc-plugin` stays the lightweight OSC execution layer, and the new
`electone-tone-studio` is the high-level studio wrapping seven tools that return
structured JSON:

| Tool | Purpose |
| --- | --- |
| `tone_inspect_song` | Read the per-song MIR profile (polyphony, clean notes, vocal contour, open pockets) |
| `tone_plan_registration` | Lock the three keyboards' roles and engines, with a one-phrase-overridable rationale |
| `tone_build_anchor_sfz` | Engine A: isolate + resynthesise → per-key sfizz instrument |
| `tone_build_stem_synth` | Engine B/C: wavetable / physical model → Vital or Surge preset |
| `tone_render_and_eval` | The six objective gates plus audition WAV paths |
| `tone_refine_patch` | Targeted adjustment of one keyboard after listening |
| `tone_export_cantabile` | Generate and byte-verify the `.cantabileSong`, enforcing the budget |

```powershell
node electone-tone-studio\test\selftest.mjs          # offline selftest (38 checks)
pnpm dsh plugin --profile web add .\electone-tone-studio
```

## Known limitations (measured)

- **Headless VST sample loading does not work on this machine.** `sfizz` loads and
  responds to pitch, but every channel that would inject an `.sfz`
  (`preset_data`, `raw_state`, `load_preset`) plays its built-in synth instead;
  `sforzando` parses the SFZ structure yet renders silence — including for the
  project's own Salamander piano. Tone rendering therefore reads the libraries'
  real per-note WAVs, which also makes results reproducible without a DAW.
- `pyworld` and `basic-pitch` have no Python 3.14 wheels; they are replaced by
  harmonic+residual resynthesis and librosa onset+pyin respectively (the
  alternatives `improvements.md` sanctions).
- The Salamander library's adjacent keys come from different physical strings, so
  its own `centroid/f0` jumps up to **174%** between neighbours. Sharing a take
  across neighbours brings the worst jump to ~27% and the median below 1%, but a
  real sampled piano's string change cannot be driven to zero.

## How it works

1. **Download** — `scripts/download-audio.ps1` resolves a song name via YouTube search (or takes
   a URL directly) and saves a 44.1 kHz / 16-bit stereo wav to the workspace. The video title is
   collapsed to a short `artist - song` name.
2. **Separate** — `scripts/separate-stems.ps1` runs BS-RoFormer-SW locally on the GPU and splits
   the mix into `vocals`, `drums`, `bass`, `guitar`, `piano`, `other` (+ `instrumental`).
   Output is 32-bit float wav.
3. **Check** — `scripts/check-stems.py` scans the raw model output window by window and reports
   separation failures (see [Separation caveats](#separation-caveats)).
4. **Refine** (optional) — `scripts/refine-stems.py` runs specialist models only on the stems
   that actually carry audio, adding finer timbres next to the parent stems. Currently that's
   drums → `kick` / `snare` / `toms` / `hh` / `ride` / `crash`.
5. **Analyze** — determine which instrument carries the main melody, which play
   chords / counter-melody, and which is the bass line.
6. **Map & configure** — pick a matching VST timbre per part and generate a Cantabile song:
   Upper keyboard = melody (MIDI channel 1), Lower = chords/counter-melody (channel 2),
   Pedal = bass (channel 3).
7. **Import** — after writing the `.cantabileSong`, ask whether to open it in Cantabile,
   so plugins and patches are in place in one step.
8. **Play** — the ELB-02 acts as a MIDI controller, the PC produces the sound and routes it
   back through the instrument's AUX IN. See [prerequisites.md](prerequisites.md) /
   [prerequisites_zh.md](prerequisites_zh.md) for hardware setup.

Stems are instrument tracks, not musical roles: "melody vs. chords" is decided in the analysis
stage, not by the separator.

Every stage prints its number, a live percentage and elapsed time, so a slow run is never
confused with a hung one. See [Progress reporting](#progress-reporting).

## Separation caveats

Read this before building anything on top of the stems.

- **`other` is not an instrument.** It is the residual bucket — by definition
  `other = mix − (vocals + drums + bass + guitar + piano)`. It holds brass, pads, strings and
  whatever else the model could not assign. Never treat `other.wav` as "the piano part" or any
  single timbre.
- **But the residual bucket holds real instruments, not garbage.** It cannot be used as one
  timbre, yet it should not be thrown away either: measure what is in it, then pick a matching
  patch. In reference measurements, the `other` stem can be dominated by **bowed strings** with
  characteristic sustained attacks and very low spectral flatness. A piano cannot satisfy those measurements.
  **Restoring strings with a pad preset yields a metallic fake**, so use a string patch
  (`Violini Poly`). Judge with all three: attack time, longest sustained run, spectral flatness.
- **`piano` may be empty even when the song has piano.** Model accuracy is song-dependent:
  a stem can be silent while the piano content sits in `other`, or usable on another track.
  Check before relying on a stem.
- **Sparse intros can fail entirely.** Reference measurements can show
  `corr(other, mix) ≈ 1.000`, i.e. the model copied the mix into `other` instead of separating.
  `check-stems.py` reports these as `dumped` / `silent-melodic` segments with timestamps.
- **Vocals, drums, bass and guitar are the reliable stems.** Those four routed correctly in a
  6×6 round-trip label test and carry consistent level across songs.
- **Piano cannot be subdivided.** Refinement uses `audio-separator`'s model registry, which has
  no freely downloadable piano / keys / organ specialist — MVSep's keys model is a paid service.
  DrumSep is the only specialist wired up, so drums gain six names while piano stays one stem.

The stems always sum back to the mix (`corr ≈ 0.9998`, residual ≈ −55 dB), so nothing is lost —
but "present in the output" does not mean "correctly separated".

## Progress reporting

Long stages would otherwise look identical to a crash. `scripts/progress.py` wraps every child
process and prints:

```
================================================================
[1/2] 分离轨道 / separating stems
================================================================
  分离轨道 / separating stems | elapsed 00:11 | [--------------------]   5%
  分离轨道 / separating stems | elapsed 00:31 | [##########----------]  50%
  分离轨道 / separating stems | elapsed 00:54 | [####################] 100%
  done: 分离轨道 / separating stems in 00:56
```

- Percentages are parsed from the tools' own output (`Estimated time remaining`, tqdm counts),
  not estimated.
- A `working...` heartbeat appears after 5 s of silence, so "slow" and "stuck" are
  distinguishable.
- Failures print `FAILED: <stage> (exit N) after <time>` instead of dying silently.
- Stage numbers adapt to the entry point: download → separate → check is `[1/3]`…`[3/3]`;
  running separation alone is `[1/2]`…`[2/2]`.
- When output is redirected to a file, status lines are throttled to a ≥10% jump or every 3 s.

## Requirements


- Windows 10/11, Python 3.10+, Git
- NVIDIA GPU recommended (CUDA); CPU fallback via `-Cpu`
- ~6 GB free disk (`.venv` is ~4.9 GB plus ~1.1 GB for the two required models; the 53-stem
  model adds another 1.3 GB)

## Setup

```powershell
.\scripts\setup-env.ps1
```

Creates `.venv`, installs CUDA PyTorch and `bs-roformer-infer`. The separation model
(~667 MB) is downloaded automatically on first run into `<workspace>\models\` — see
[Model downloads](#model-downloads).

It also installs `audio-separator`, used only for the optional refinement stage. That package
pins some older dependencies which would break `bs-roformer`, so it is installed with
`--no-deps` and the needed pieces are added explicitly — do not "fix" this by installing it
normally.

`tools/` must contain `yt-dlp.exe`, `ffmpeg.exe`, `ffprobe.exe` (binaries are not committed;
run scripts look them up there).

### Model downloads

**No model weights ship with the repo** — the whole `workspace/` directory is gitignored. A
clone is pure code; weights are **downloaded automatically** the first time you actually run
something. No directory to create, no path to configure.

| Model | Size | Downloaded when | Lands in |
| --- | --- | --- | --- |
| BS-RoFormer-SW (6-stem main model) | 667 MB | first run of `separate-stems.ps1` (auto-chained from `download-audio.ps1`) | `<workspace>\models\roformer-model-bs-roformer-sw-by-jarredou\` |
| MDX23C DrumSep (drum sub-stems) | 417 MB | first run of `refine-stems.py` (optional second stage) | `<workspace>\models\audio-separator\` |
| 53-stem "mega" model | 1.31 GB | **only** with an explicit `-Model roformer-model-bs-roformer-mvsep-mega-53-stems`; never by default | same as above |

Missing directories are created on demand. Downloads are SHA-256 verified, and an interrupted
transfer is written to a `.partial` file and renamed only when complete, so a half-file is
never mistaken for a present model.

**Behind a restrictive network or offline**: both sources are direct `huggingface.co` and
`github.com` URLs with no built-in mirror fallback. Two options:

1. Use a proxy — `HTTP_PROXY` / `HTTPS_PROXY` environment variables are enough, both
   libraries go through `requests`;
2. Place the files by hand, exactly here:

```
<workspace>\models\roformer-model-bs-roformer-sw-by-jarredou\
    BS-Rofo-SW-Fixed.ckpt      ← 699,412,152 bytes
    BS-Rofo-SW-Fixed.yaml

<workspace>\models\audio-separator\
    MDX23C-DrumSep-aufr33-jarredou.ckpt
    config_drumsep_mdx23c.yaml
```

You can also keep models anywhere and point at them by environment variable:
`BS_ROFORMER_MODELS_PATH` (bs-roformer) and `AUDIO_SEPARATOR_MODEL_DIR` (audio-separator —
that directory must **already exist**, otherwise `Separator()` raises `FileNotFoundError`).

**Do not drop in a same-named file from an unknown source**: `audio-separator` checks the
SHA-256 of existing model files too and re-downloads on mismatch, while `bs-roformer` verifies
only fresh downloads and does not re-check files already on disk. When no checksum is
recorded for an asset, only its size is checked.

## Usage

### One click

Double-click `download-audio.cmd`, type a song name (or URL) and a workspace folder.
Download and separation run back to back.

### Download

```powershell
.\scripts\download-audio.ps1 "artist - song"
.\scripts\download-audio.ps1 "https://www.youtube.com/watch?v=..."
.\scripts\download-audio.ps1 "artist - song" -CookiesFromBrowser chrome
.\scripts\download-audio.ps1 "song" -Workspace D:\electone
.\scripts\download-audio.ps1 "song" -OutDir D:\music
.\scripts\download-audio.ps1 "song" -NoSeparate
```

- A song name is searched on YouTube (first result); URLs work for other sites (e.g. Bilibili).
- Output is always wav (PCM 16-bit, 44.1 kHz, stereo), ready for separation.
- Separation starts automatically after download; skip with `-NoSeparate`.

### Separation

```powershell
.\scripts\separate-stems.ps1
.\scripts\separate-stems.ps1 "D:\music\song.wav"
.\scripts\separate-stems.ps1 "song.wav" -Cpu
.\scripts\separate-stems.ps1 "song.wav" -Clean      # discard stale stems first
```

- Model: BS-RoFormer-SW, 6 stems, 32-bit float wav, GPU-accelerated.
- Results: `<workspace>\stems\<song>\{vocals,drums,bass,guitar,piano,other}.wav` (+ `instrumental`).
- A quality report runs automatically after separation, including failure segments.
- `-Clean` only deletes old files **after** a successful run, so a failed or out-of-memory run
  can never leave the song with an empty stem folder.

### Refine (finer timbres)

```powershell
.\.venv\Scripts\python.exe .\scripts\refine-stems.py --latest
.\.venv\Scripts\python.exe .\scripts\refine-stems.py --stems "workspace\stems\<song>"
.\.venv\Scripts\python.exe .\scripts\refine-stems.py --latest --dry-run
```

Runs specialist models on the stems that actually carry audio and writes the results to
`<song>\refined\`. Specialists that cannot be downloaded are skipped with a message;
it never discards stems produced by the others.

Preferred over the earlier "prune to N" approach, which only renamed a loudness cutoff and could
not add timbres.

### Quality check

```powershell
.\.venv\Scripts\python.exe .\scripts\check-stems.py --stems "workspace\stems\<song>" --mix "workspace\audio\<song>.wav"
```

Flags failure modes per 10-second window:

- `dumped` — the model copied the whole mix into `other` instead of separating it
- `energy-loss` — the stems sum far below the mix, i.e. content was dropped
- `all-silent` — the mix is loud but every stem is near-silent

It also prints per-stem usability (`EMPTY` / `SPARSE`), so an empty `piano.wav` is reported
instead of silently passing. The stem set is discovered from the folder, so 2-, 4-, 6- and
53-stem models all work. `instrumental` is excluded from the reconstruction sum because it is
derived (`mix − vocals`) and would otherwise be counted twice.

Runs from the separation script against the **raw** model output, before any pruning, so folded
stems cannot trip the `dumped` detector. See [Separation caveats](#separation-caveats) for what
these numbers mean.

### Tone analysis & generation

```powershell
.\.venv\Scripts\python.exe .\scripts\analyze-tones.py --song <song>
.\.venv\Scripts\python.exe .\scripts\build-tones.py --song <song>
```

- `analyze-tones.py` identifies each stem's instrument, timbre traits (brightness, register,
  harmonicity, polyphony) and musical role (melody / counter-melody / chords / accompaniment /
  pad / bass line / rhythm) on 5-second windows. Writes `workspace\analysis\<song>\analysis.json`
  plus a readable `analysis.md`. It also measures the acoustic features that drive the patch
  design below: an onset-aligned note envelope (attack / decay / sustain / release and a
  percussive / decaying / sustained tendency), attack-vs-sustain brightness contrast
  (spectral centroid) and 5 MFCC means.
- **A piano stem hides two lines.** pyin follows the loudest fundamental, which on piano is
  almost always the bass accompaniment note, so a naive analysis concludes "piano is 77% in
  C2–C3, no melody". In reality the piano stem has 50–66% of its energy above C5.
  `analyze_melody_register()` high-passes at C4 and tracks that band separately, using
  ">85% of frame-to-frame steps under 2 semitones" to tell a melody from chord jumps.
  So **one piano can serve both hands** — melody on the right, accompaniment on the left,
  which is how the part is actually played. After high-passing, check the high-band energy
  share (<5% means no melody), otherwise bass harmonics get mistaken for a melody.
- `--exclude vocals,other` drops stems from the analysis. Excluding vocals leaves the piano's
  high register as the right-hand melody.
- `build-tones.py` turns the analysis into **single-instrument** tone files under
  `workspace\tone\<song>\<右手|左手|脚>\<plugin>\` — one file never mixes instruments. Right hand gets the
  melody, left hand gets chords/counter-melody/accompaniment, the pedal gets the bass line;
  drums produce no tone file (the ELB-02 rhythm section covers them).
  When one stem feeds two keyboards, the filenames differ by role
  (e.g. `钢琴(主旋律)` vs `钢琴(和弦)`).
- **The song directory is a pinyin ASCII slug** (e.g. `xiaomi-wozouhou-695243fd`), not the Chinese
  title. Same reason the keyboard directories are `right`/`left`/`pedal`: ARIA decodes the whole
  `.sfz` path with the system code page, so any CJK segment turns into mojibake and the instrument
  silently loads nothing. `scripts/tone_paths.py` derives the name deterministically from the title
  (pinyin plus the first 8 hex digits of its hash, so two titles that transliterate alike cannot
  collide), which means a rebuild lands in the same directory. The readable title is kept in
  `song.txt` inside the directory and as the manifest key.
- **Surge tones are designed from the audio, not just relabelled.** `scripts/tone_design.py`
  maps the analyzed features onto Surge XT patch parameters — filter cutoff from the spectral
  centroid, filter env-mod from attack-vs-sustain brightness, amp/filter envelopes from the
  measured ADSR, resonance from harmonicity — and `build-tones.py` writes those values into
  the `.fxp` (the template preset keeps its oscillator design). Every designed value and its
  reason is recorded in `tone\manifest.json` and the patch comment; `--no-param-design` falls
  back to plain template copying.
  The design is **constrained by the template**, because a measurement is not a sound design:
  - the cutoff may only move ±1.5 octaves from the template's own cutoff, so a dark stem cannot
    drag a bright patch down into a muffled sine;
  - a non-lowpass filter keeps its cutoff entirely — a notch has no "brightness" to open up;
  - the template's filter env-mod is kept when it is at least half the designed value, preserving
    the pluck/attack character that makes struck instruments sound struck;
  - the measured attack has an 8 ms floor (onset detection fires after the rise, so it
    under-reports), and a measured decay shorter than the tendency band's floor wins over the band.
- Piano uses a sample-based source (sforzando + Accurate-Salamander grand piano SFZ); the SFZ
  config lives in the project and references the sample library by absolute path.
- Surge tones are written as `.fxp` — Surge XT's native patch format, loadable by
  **dragging it onto the Surge XT window** (Surge's drop whitelist includes `.fxp`).
  Add `--vstpreset` to also write a `.vstpreset` for VST3 hosts (Cantabile); that one is
  loaded by the host's own preset mechanism and is not a Surge drop format.
- **For a tone that actually sounds like the recording, use the sampled instruments.**
  Surge XT is a synthesizer with no sample playback, so factory presets cannot reproduce
  the real guitar/bass/string timbres. `build-sample-tones.py` builds a sforzando `.sfz` + its
  own `.wav` samples from each stem (one instrument per `.sfz` and sample folder) with an
  offline quality pipeline: onset + pitch-break segmentation, per-note quality gating
  (voiced ratio, f0 stability, harmonicity, flatness), 2-3 loudness-clustered velocity
  layers, per-key samples pre-rendered at exact pitch with the source spectral envelope
  preserved (the engine never stretches audio), and period-aligned loops with the crossfade
  baked into the wav. Vocals are not sampled (the melody is played live) and piano keeps
  the Salamander grand-piano library instead of sliced stem notes:

  ```powershell
  .\.venv\Scripts\python.exe .\scripts\build-sample-tones.py --latest
  ```

  Output goes to `workspace\tone\<song>\<右手|左手|脚>\sforzando\`; `tone\sample-instruments.json`
  records accepted/rejected note counts, velocity layers and keyboard coverage (keys
  without a usable source note are reported honestly, not stretched).
- Nothing is ever written into a plugin's default/user patch folder — the scripts refuse
  to write outside `workspace\tone\`. A `manifest.json` in that folder records what was produced.

### Generate & import a Cantabile song

```powershell
.\.venv\Scripts\python.exe .\scripts\build-cantabile.py --song <song>
.\.venv\Scripts\python.exe .\scripts\import-cantabile.py --song <song>
```

`build-cantabile.py` reads `tone\manifest.json`, creates one plugin instance per Surge XT tone,
assigns MIDI channels per keyboard, and writes `workspace\cantabile\<song>.cantabileSong`:

| Keyboard | MIDI channel | Timbres |
| --- | --- | --- |
| Upper (right hand) | 1 | melody + pad (several instances may share channel 1 and layer) |
| Lower (left hand) | 2 | chords / counter-melody / accompaniment |
| Pedal (foot) | 3 | bass line |

- **A sampled instrument always wins over a synthesizer patch for the same part.** Surge XT is a
  synthesizer: its factory presets are oscillator designs, so using one to imitate a recorded
  guitar or bass produces a *different instrument* — a nylon-guitar preset reads as a plucked
  zither, a fingered-bass preset reads as a muted trumpet. The stems themselves are the only
  source that carries the recording's actual timbre, so when `build-sample-tones.py` has produced
  an `.sfz` for a part, that is what gets imported and the Surge patch for the same part is
  skipped. The piano is the exception in the other direction: always the Salamander grand.
- **Sampled tone paths are pure ASCII.** sforzando's ARIA engine decodes the `.sfz` path with the
  system code page (GBK on a Chinese Windows), not UTF-8, so a CJK path becomes mojibake
  (`左手` → `宸︽墜`) and the plugin silently loads nothing. Keyboard directories are therefore
  `right` / `left` / `pedal` and sampled instruments get an ASCII slug filename; the readable
  Chinese name stays in the SFZ comment and the manifest. Surge `.fxp` files keep their Chinese
  names because Surge reads them itself.
- **One stem owns the right hand.** Several stems can carry a plausible top line, but the ELB-02
  has one right hand. `analyze-tones.py` arbitrates: the stem with the strongest high-register
  melody evidence keeps the right hand, the rest become counter-melody on the left. The residual
  `other` bucket is a doubling layer and only takes the right hand when nothing else does.
- **One left hand, one accompaniment instrument.** Two different instruments answering the same
  key is not a layer, it is a collision: a pad and a piano chord struck together beat against each
  other and read as a bell on one note and as jazz on two. The residual bucket is dropped from the
  left hand when a real accompaniment stem already holds it, and the bass stem plays the pedal
  only (doubling it on the left just muddies the chords).
- **Every rendered sample is pitch-verified.** `render_note` transposes by `f_target / f0_src`, so
  a source whose f0 was detected a fifth or an octave off makes every key a fixed interval wrong —
  the instrument plays wrong intervals even though every file and region is valid. Two guards:
  `refine_f0()` walks down the usual sub-harmonic ratios (÷2, ÷3, ÷1.5) and keeps the lowest
  candidate that still has real spectral energy at its own fundamental, and `verify_pitch()`
  measures each rendered note and rejects it when it lands more than 50 cents off. Measured
  accuracy after both: 104 of 105 keys within 100 cents.
- **Every key must sound, and that outranks timbre purity.** Older versions capped the
  engine-transposition fallback at 2 semitones for the manuals and emitted *no `<region>` at all*
  beyond it, so those keys were greyed out in the SFZ and silent under the finger. That is the
  one failure a
  performing instrument cannot have — a stretched sample still plays the right note, a missing
  region plays nothing. `always_cover_keyboard` now gives every key in the hardware range a region:
  takes within ±2 semitones are pre-rendered at exact pitch (zero engine stretch), keys beyond that
  are pre-rendered from the nearest take (`render_note` preserves the spectral envelope, which beats
  the engine's plain resampling), and anything still open is filled by engine transposition. Keys
  stretched more than `extrapolation_warn_semitones` (3 semitones) are labelled `extrapolated` in the
  manifest — a quality note, **not** a playability gap. The stretch cap still exists, but it now
  only decides *which take to use and how to label it*, never whether a key goes silent.
- **Each keyboard is sampled for its own physical range.** The ELB-02 has three keyboards with
  different spans — Upper and Lower are 49 keys (C2–C6, MIDI 36–84), the Pedal is 20 keys
  (C2–G3, MIDI 36–55) — so `build-sample-tones.py` builds one instrument per keyboard. This
  matters most for the pedal: a bass stem typically plays A1–D#2, which is *below* the pedal's own
  range, so the upper half of the pedal has no source material at all. Those keys are filled by
  letting the SFZ engine transpose the nearest rendered key (`pitch_keycenter`), which keeps the
  sample at a pitch where it still sounds like a bass instead of stretching it an octave. All three
  keyboards now cover their full span (49/49/20), verified to have zero dead keys.
- The song file always lives in `workspace\cantabile\`; nothing is written into Cantabile's own
  user folders.

`import-cantabile.py` asks after building:

```
需要自动导入 Cantabile 吗？ [Y/n]
```

- `Y` — open the song with `Cantabile.exe`; plugins and patches load in one step.
- `N` — keep the song file and open it yourself.
- Non-interactive: `--yes` / `--no`; `--dry-run` builds without launching.

Cantabile stores plugin state as `base64(raw-deflate(aBMP(VST3 state)))`;
`scripts/cantabile_format.py` converts between `.fxp` and that container.
`build-cantabile.py --self-test` round-trips a real song file byte-for-byte, so a format
change fails immediately.

### Live control: Surge XT OSC plugin

`surge-osc-plugin/` is a DeepSeek Harness tool bundle — the execution layer of the
Surge XT timbre-replication workflow described in `prompt.md`. It lets the agent set
(`surge_set_param`) and read back (`surge_get_param`) Surge XT parameters over the
synth's native OSC interface, with zero runtime dependencies. Install it from DSH's
**Plugins → Add plugin** (or `plugin_manager install_bundle`); Surge-side setup and the
query limitation (Surge sends query replies to its configured OSC output port) are
documented in [surge-osc-plugin/README.md](surge-osc-plugin/README.md).

### Workspace

All stages share one workspace root, resolved in this order:

1. `-Workspace` parameter
2. `ELECTONE_WORKSPACE` environment variable
3. `config.json` in the project root (saved when you confirm a path interactively)
4. Default: `<project>\workspace`

Layout: `audio\` (downloads), `stems\` (separation output), `stems\<song>\refined\` (optional
second-stage stems), `models\` (model weights), `tone\<song>\<右手|左手|脚>\<plugin>\` (tone files;
the song directory is a pinyin ASCII slug, see
[Tone analysis & generation](#tone-analysis--generation)), `cantabile\` (song files),
`cache\` (tool caches — torch / HuggingFace / numba / pip / yt-dlp are redirected here so
nothing is written into the user profile). `config.json` holds machine-local paths and is
gitignored.

### Model choice

`-Model` selects any model in the `bs-roformer-infer` registry (34 entries). The default,
`roformer-model-bs-roformer-sw-by-jarredou`, is the only 6-stem instrument model and the one the
refinement stage expects. A 53-stem "mega" model also exists; it is **not** practical on a
consumer GPU — on an 8 GB laptop card it takes over 20 minutes for 80 seconds of audio and needs
its config repaired by `scripts/align-model-config.py` (the separation script does this
automatically). Prefer the 6-stem model plus refinement.

## Open-source resources

| Resource | Role | License |
| --- | --- | --- |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Audio download | Unlicense |
| [FFmpeg](https://ffmpeg.org/) (gyan.dev build) | Audio conversion | LGPL/GPL |
| [PyTorch](https://pytorch.org/) | Model runtime (CUDA) | BSD-3 |
| [BS-RoFormer](https://github.com/lucidrains/BS-RoFormer) (lucidrains) | Separation architecture | MIT |
| [bs-roformer-infer](https://github.com/openmirlab/bs-roformer-infer) (openmirlab) | Inference CLI / model registry | MIT |
| BS-RoFormer-SW checkpoint (jarredou, mirrored by enerjazzer) | 6-stem separation weights | not stated — personal use fine, verify before commercial use |
| [audio-separator](https://github.com/nomadkaraoke/python-audio-separator) | Specialist sub-stem models (also MDX23C / MelRoformer / SCNet) | MIT |
| MDX23C DrumSep (aufr33, jarredou) | drums → kick/snare/toms/hh/ride/crash | see model repo |
| [Surge XT](https://surge-synthesizer.github.io/) | Free synthesizer | GPLv3 |
| sforzando / Decent Sampler | SFZ / DSP sample players | freeware |
| [Cantabile](https://www.cantabilesoftware.com/) | VST host (Lite is free) | commercial |

## Notes & limitations

- Separation quality is bounded by the source audio: prefer official or high-bitrate sources;
  live recordings and low-bitrate rips degrade results noticeably.
- Dense mixes (orchestra, layered J-pop) separate worse than sparse ones; unassigned material
  ends up in `other`.
- Piano separation is the weakest branch of the model and varies by song — see
  [Separation caveats](#separation-caveats).
- DrumSep runs on CPU here: `onnxruntime-gpu` wants CUDA 13 while PyTorch ships CUDA 12, so the
  refinement stage falls back to CPU (~40 s for a 5-minute song). Correct but not fast.
- YouTube occasionally answers automated downloads with "Sign in to confirm you're not a bot".
  Pass `-CookiesFromBrowser chrome` to reuse your browser session.
- Downloading from video platforms may violate their terms of service — for personal use only.
- The ELB-02 cannot load custom voices; the PC is the sound source (see prerequisites docs).

## Scripts

| Script | Purpose |
| --- | --- |
| `download-audio.ps1` | song name/URL → wav, then hands off to separation |
| `separate-stems.ps1` | 6-stem separation, quality check, then FLAC archival |
| `check-stems.py` | per-window failure detection, per-stem usability |
| `refine-stems.py` | specialist second-stage split (drums → 6 pieces); now opt-in |
| `archive-stems.py` | **storage** — float32 wav → 24-bit lossless FLAC with a sample-level round-trip check |
| `disk_budget.py` | **per-song disk budget gate** (<1.2 GB) with a per-stage breakdown |
| `analyze_song.py` | **unified MIR frontend** → `song_profile.json` (polyphony, vocal contour, open pockets) |
| `analyze-tones.py` | per-stem instrument/timbre/role analysis → JSON + MD report |
| `tone_common.py` | shared base: keyboard ranges, DSP, 24-bit I/O, quality-gate numbers |
| `anchor_extract.py` | **Engine A.2** — harmonic-comb isolation + pitch verification → 3–6 Golden Anchors |
| `anchor_resynth.py` | **Engine A.3–4** — formant-preserving resynthesis + sfizz SFZ assembly |
| `piano_seed.py` | **right-hand default** — Accurate-Salamander real per-note samples, velocity layers + air |
| `real_seed.py` | real recorded instrument engine (Karoryfer guitar/bass) with per-song Match EQ |
| `dry_seed.py` | physical-model dry seed (Karplus-Strong) as a no-sample fallback |
| `vital_synth.py` | **Engine B** — stem wavetable + attack transient → `.vital` |
| `surge_synth.py` | **Engine C** — Surge XT String / wavetable → `.fxp` |
| `tone_optimizer.py` | **closed-loop optimizer** — render-compare-search instead of open-loop formulas |
| `melody_orchestrator.py` | **Stage 3** — right-hand MIR scoring, Piano-first prior, fallback gate |
| `build_song_tone.py` | **end-to-end** — per-key instrument sets for all three keyboards + the plan |
| `eval_tones.py` | **Stage 6 evaluation suite** — six hard gates |
| `poc_verify.py` | Phase-0 gate: old slicer vs candidate engines, with audition clips |
| `build-cantabile.py` | generate a Cantabile song; `--verify` emits a machine-readable report |
| `cantabile_format.py` | plugin-state container (aBMP / VST3 state) for sfizz / sforzando / Vital / Surge |
| `import-cantabile.py` | build the song, then offer to open it in Cantabile |
| `tone_eval.py` | legacy VST-driven evaluation (needs loadable VSTs — see limitations) |
| `tone_design.py` | feature-driven Surge parameter design (**old open-loop**, superseded by the optimizer) |
| `build-tones.py` | single-instrument Surge XT tones (`.fxp`) |
| `build-sample-tones.py` | legacy chord slicer (**superseded** by `anchor_*`, kept for comparison) |
| `test_tone_rules.py` | regression tests: cutoff floor, template-constraint order, unit conversions, sample-rate consistency |
| `render-tone.py` | render one tone by hand and compare against the source stem |
| `final_acceptance.py` | run every phase's gate and print one verdict |
| `tone_paths.py` | tone tree layout: song pinyin slug / keyboard / plugin, plus manifest path helpers |
| `migrate-tone-layout.py` | one-shot migration from `tone\<keyboard>\<plugin>\` to `tone\<song>\<keyboard>\<plugin>\` |
| `prune-stems.py` | keep the N loudest stems, fold the rest into `other` |
| `progress.py` | stage headers, live percentage, heartbeat, failure reporting |
| `align-model-config.py` | repair a model whose `chunk_size` breaks the chunker |
| `setup-env.ps1` | create `.venv`, install PyTorch + both inference backends |
| `common.ps1` | workspace resolution, `artist - song` naming helpers, cache redirection |
| `_cache_env.py` | redirect tool caches into `<workspace>\cache` for direct python runs |

## Roadmap

- Fallback for segments where separation fails (currently reported, not repaired)
- Piano / keys subdivision once a freely usable specialist model exists
- Decent Sampler tones (`.dspreset`) as an alternative to SFZ

## Related docs

- [prerequisites.md](prerequisites.md) — ELB-02 as MIDI controller with a Windows PC
- [prerequisites_zh.md](prerequisites_zh.md) — 中文版
