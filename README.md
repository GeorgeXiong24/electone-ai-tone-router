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
| Second-stage refinement (drums → 6 pieces) | Done |
| Live progress reporting | Done |
| Melody / chord / counter-melody analysis | Done |
| Instrument → VST timbre mapping (sforzando / Surge XT / Decent Sampler) | Done |
| Cantabile song generation for ELB-02 | Done |
| Automatic Cantabile import | Done |

## How it works

1. **Download** — `scripts/download-audio.ps1` resolves a song name via YouTube search (or takes
   a URL directly) and saves a 44.1 kHz / 16-bit stereo wav to the workspace. The video title is
   collapsed to a short `artist - song` name (e.g. `周杰倫 - 晴天`).
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

Read this before building anything on top of the stems. Measured on three reference songs
(韋禮安《女孩》, 周杰倫《晴天》, 周杰倫《不能說的祕密》):

- **`other` is not an instrument.** It is the residual bucket — by definition
  `other = mix − (vocals + drums + bass + guitar + piano)`. It holds brass, pads, strings and
  whatever else the model could not assign. Never treat `other.wav` as "the piano part" or any
  single timbre.
- **But the residual bucket holds real instruments, not garbage.** It cannot be used as one
  timbre, yet it should not be thrown away either: measure what is in it, then pick a matching
  patch. On 《蒲公英的約定》 the `other` stem is dominated by **bowed strings** — median attack
  rise 93 ms (piano 70 ms, vocals 186 ms), longest continuous run **71 s**, spectral flatness
  0.00086, onset rate 0.31/s. A piano cannot satisfy those numbers.
  **Restoring strings with a pad preset yields a metallic fake**, so use a string patch
  (`Violini Poly`). Judge with all three: attack time, longest sustained run, spectral flatness.
- **`piano` may be empty even when the song has piano.** On 《晴天》 the piano stem was silent for
  the whole track (0 of 31 ten-second windows above −40 dB) while a solo piano intro sat in
  `other`. On 《不能說的祕密》 the same model produced usable piano (28 of 33 windows).
  Accuracy is song-dependent, so check before relying on a stem.
- **Sparse intros can fail entirely.** On all three songs the opening section showed
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
- ~5 GB free disk (venv + model weights)

## Setup

```powershell
.\scripts\setup-env.ps1
```

Creates `.venv`, installs CUDA PyTorch and `bs-roformer-infer`. The separation model
(~667 MB) is downloaded automatically on first run into `<workspace>\models\`.

It also installs `audio-separator`, used only for the optional refinement stage. That package
pins some older dependencies which would break `bs-roformer`, so it is installed with
`--no-deps` and the needed pieces are added explicitly — do not "fix" this by installing it
normally.

`tools/` must contain `yt-dlp.exe`, `ffmpeg.exe`, `ffprobe.exe` (binaries are not committed;
run scripts look them up there).

## Usage

### One click

Double-click `download-audio.cmd`, type a song name (or URL) and a workspace folder.
Download and separation run back to back.

### Download

```powershell
.\scripts\download-audio.ps1 "女孩 - 韦礼安"
.\scripts\download-audio.ps1 "https://www.youtube.com/watch?v=..."
.\scripts\download-audio.ps1 "女孩 - 韦礼安" -CookiesFromBrowser chrome
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
.\.venv\Scripts\python.exe .\scripts\refine-stems.py --stems "workspace\stems\周杰倫 - 晴天"
.\.venv\Scripts\python.exe .\scripts\refine-stems.py --latest --dry-run
```

Runs specialist models on the stems that actually carry audio and writes the results to
`<song>\refined\`. On 《不能說的祕密》 this yields 7 base stems + 6 drum pieces, of which 8 are
above the audibility floor. A specialist that cannot be downloaded is skipped with a message;
it never discards stems produced by the others.

Preferred over the earlier "prune to N" approach, which only renamed a loudness cutoff and could
not add timbres.

### Quality check

```powershell
.\.venv\Scripts\python.exe .\scripts\check-stems.py --stems "workspace\stems\周杰倫 - 晴天" --mix "workspace\audio\周杰倫 - 晴天.wav"
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
.\.venv\Scripts\python.exe .\scripts\analyze-tones.py --song 不能說的祕密
.\.venv\Scripts\python.exe .\scripts\build-tones.py  --song 不能說的祕密
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
  Measured on 《蒲公英的約定》: melody sits in B3–C5 (median D4), 76% voiced, 97.9% stepwise.
  So **one piano can serve both hands** — melody on the right, accompaniment on the left,
  which is how the part is actually played. After high-passing, check the high-band energy
  share (<5% means no melody), otherwise bass harmonics get mistaken for a melody.
- `--exclude vocals,other` drops stems from the analysis. Excluding vocals leaves the piano's
  high register as the right-hand melody.
- `build-tones.py` turns the analysis into **single-instrument** tone files under
  `workspace\tone\<右手|左手|脚>\<plugin>\` — one file never mixes instruments. Right hand gets the
  melody, left hand gets chords/counter-melody/accompaniment, the pedal gets the bass line;
  drums produce no tone file (the ELB-02 rhythm section covers them).
  When one stem feeds two keyboards, the filenames differ by role
  (e.g. `钢琴(主旋律)` vs `钢琴(和弦)`).
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

  Output goes to `workspace\tone\<右手|左手|脚>\sforzando\`; `tone\sample-instruments.json`
  records accepted/rejected note counts, velocity layers and keyboard coverage (keys
  without a usable source note are reported honestly, not stretched).
- Nothing is ever written into a plugin's default/user patch folder — the scripts refuse
  to write outside `workspace\tone\`. A `manifest.json` in that folder records what was produced.

### Generate & import a Cantabile song

```powershell
.\.venv\Scripts\python.exe .\scripts\build-cantabile.py --song 蒲公英
.\.venv\Scripts\python.exe .\scripts\import-cantabile.py --song 蒲公英
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
- **Transposition is capped at 3 semitones.** Resampling further audibly changes the instrument —
  the formant correction keeps body resonances in place, but the attack transient and harmonic
  balance still move, and a pad stretched a fifth sounds like a bell. Keys outside the window stay
  uncovered rather than faked.
- **Each keyboard is sampled for its own physical range.** The ELB-02 has three keyboards with
  different spans — Upper and Lower are 49 keys (C2–C6, MIDI 36–84), the Pedal is 20 keys
  (C2–G3, MIDI 36–55) — so `build-sample-tones.py` builds one instrument per keyboard and only
  renders keys that can actually be pressed. This matters most for the pedal: a bass stem
  typically plays A1–D#2, which is *below* the pedal's own range, so the upper half of the pedal
  has no source material at all. Those keys are filled by letting the SFZ engine transpose the
  nearest rendered key (`pitch_keycenter`), which keeps the sample at a pitch where it still
  sounds like a bass instead of stretching it an octave. The pedal therefore covers all 20 keys.
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
second-stage stems), `models\` (model weights), `tone\` (tone files), `cantabile\` (song files),
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
| `separate-stems.ps1` | 6-stem separation, then quality check |
| `check-stems.py` | per-window failure detection, per-stem usability |
| `refine-stems.py` | specialist second-stage split (drums → 6 pieces) |
| `analyze-tones.py` | per-stem instrument/timbre/role analysis → JSON + MD report |
| `tone_design.py` | feature-driven Surge XT parameter design (unit conversions + decision rules) |
| `build-tones.py` | single-instrument Surge XT tones (`.fxp`, optional `--vstpreset`) |
| `build-sample-tones.py` | cut single notes from each stem → sforzando SFZ instrument (`.sfz` + `.wav`) |
| `build-cantabile.py` | generate a Cantabile song (`.cantabileSong`) with per-keyboard MIDI channels |
| `import-cantabile.py` | build the song, then offer to open it in Cantabile |
| `cantabile_format.py` | Cantabile plugin-state container (aBMP / VST3 state) ↔ `.fxp` |
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
