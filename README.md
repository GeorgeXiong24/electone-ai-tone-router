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
| Melody / chord / counter-melody analysis | Planned |
| Instrument → VST timbre mapping (sforzando / Surge XT / Decent Sampler) | Planned |
| Cantabile rack generation for ELB-02 | Planned |

## How it works

1. **Download** — `scripts/download-audio.ps1` resolves a song name via YouTube search (or takes
   a URL directly) and saves a 44.1 kHz / 16-bit stereo wav to the workspace. The video title is
   collapsed to a short `artist - song` name (e.g. `周杰倫 - 晴天`).
2. **Separate** — `scripts/separate-stems.ps1` runs BS-RoFormer-SW locally on the GPU and splits
   the mix into `vocals`, `drums`, `bass`, `guitar`, `piano`, `other` (+ `instrumental`).
   Output is 32-bit float wav; each stem gets a usability report afterwards.
3. **Check** — `scripts/check-stems.py` scans the stems window by window and reports separation
   failures (see [Separation caveats](#separation-caveats)).
4. **Analyze** (planned) — determine which instrument carries the main melody, which play
   chords / counter-melody, and which is the bass line.
5. **Map & configure** (planned) — pick a matching VST timbre per part and generate a Cantabile
   setup where Upper keyboard = melody, Lower keyboard = chords/counter-melody, Pedal = bass.
6. **Play** — the ELB-02 acts as a MIDI controller, the PC produces the sound and routes it
   back through the instrument's AUX IN. See [prerequisites.md](prerequisites.md) /
   [prerequisites_zh.md](prerequisites_zh.md) for hardware setup.

Stems are instrument tracks, not musical roles: "melody vs. chords" is decided in the analysis
stage, not by the separator.

## Separation caveats

Read this before building anything on top of the stems. Measured on three reference songs
(韋禮安《女孩》, 周杰倫《晴天》, 周杰倫《不能說的祕密》):

- **`other` is not an instrument.** It is the residual bucket — by definition
  `other = mix − (vocals + drums + bass + guitar + piano)`. It holds brass, pads, strings and
  whatever else the model could not assign. Never treat `other.wav` as "the piano part" or any
  single timbre.
- **`piano` may be empty even when the song has piano.** On 《晴天》 the piano stem was silent for
  the whole track (0 of 31 ten-second windows above −40 dB) while a solo piano intro sat in
  `other`. On 《不能說的祕密》 the same model produced usable piano (28 of 33 windows).
  Accuracy is song-dependent, so check before relying on a stem.
- **Sparse intros can fail entirely.** On all three songs the opening section showed
  `corr(other, mix) ≈ 1.000`, i.e. the model copied the mix into `other` instead of separating.
  `check-stems.py` reports these as `dumped` / `silent-melodic` segments with timestamps.
- **Vocals, drums, bass and guitar are the reliable stems.** Those four routed correctly in a
  6×6 round-trip label test and carry consistent level across songs.

The stems always sum back to the mix (`corr ≈ 0.9998`, residual ≈ −55 dB), so nothing is lost —
but "present in the output" does not mean "correctly separated".

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
```

- Model: BS-RoFormer-SW, 6 stems, 32-bit float wav, GPU-accelerated.
- Results: `<workspace>\stems\<song>\{vocals,drums,bass,guitar,piano,other}.wav` (+ `instrumental`).
- A quality report runs automatically after separation, including failure segments.

### Quality check

```powershell
.\.venv\Scripts\python.exe .\scripts\check-stems.py --stems "workspace\stems\周杰倫 - 晴天" --mix "workspace\audio\周杰倫 - 晴天.wav"
```

Flags two failure modes per 10-second window:

- `dumped` — the model copied the whole mix into `other` instead of separating it
- `silent-melodic` — the mix is loud but every melodic stem is near-silent

It also prints per-stem usability (`EMPTY` / `SPARSE`), so an empty `piano.wav` is reported
instead of silently passing. Runs from the separation script; see
[Separation caveats](#separation-caveats) for what these numbers mean.

### Workspace

All stages share one workspace root, resolved in this order:

1. `-Workspace` parameter
2. `ELECTONE_WORKSPACE` environment variable
3. `config.json` in the project root (saved when you confirm a path interactively)
4. Default: `<project>\workspace`

Layout: `audio\` (downloads), `stems\` (separation output), `analysis\` (planned),
`models\` (model cache). `config.json` holds machine-local paths and is gitignored.

## Open-source resources

| Resource | Role | License |
| --- | --- | --- |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Audio download | Unlicense |
| [FFmpeg](https://ffmpeg.org/) (gyan.dev build) | Audio conversion | LGPL/GPL |
| [PyTorch](https://pytorch.org/) | Model runtime (CUDA) | BSD-3 |
| [BS-RoFormer](https://github.com/lucidrains/BS-RoFormer) (lucidrains) | Separation architecture | MIT |
| [bs-roformer-infer](https://github.com/openmirlab/bs-roformer-infer) (openmirlab) | Inference CLI / model registry | MIT |
| BS-RoFormer-SW checkpoint (jarredou, mirrored by enerjazzer) | 6-stem separation weights | not stated — personal use fine, verify before commercial use |
| [Surge XT](https://surge-synthesizer.github.io/) (planned) | Free synthesizer | GPLv3 |
| sforzando / Decent Sampler (planned) | SFZ / DSP sample players | freeware |
| [Cantabile](https://www.cantabilesoftware.com/) (planned) | VST host (Lite is free) | commercial |

## Notes & limitations

- Separation quality is bounded by the source audio: prefer official or high-bitrate sources;
  live recordings and low-bitrate rips degrade results noticeably.
- Dense mixes (orchestra, layered J-pop) separate worse than sparse ones; unassigned material
  ends up in `other`.
- Piano separation is the weakest branch of the model and varies by song — see
  [Separation caveats](#separation-caveats).
- Downloading from video platforms may violate their terms of service — for personal use only.
- The ELB-02 cannot load custom voices; the PC is the sound source (see prerequisites docs).

## Roadmap

- Melody / chord / counter-melody role detection from stems
- Instrument → VST timbre mapping table (SFZ libraries, Surge XT presets)
- Automatic Cantabile rack/song generation (Performer triggers / Network Server)
- Live MIDI routing ELB-02 (Upper/Lower/Pedal) → Cantabile
- Fallback for segments where separation fails (currently reported, not repaired)

## Related docs

- [prerequisites.md](prerequisites.md) — ELB-02 as MIDI controller with a Windows PC
- [prerequisites_zh.md](prerequisites_zh.md) — 中文版
