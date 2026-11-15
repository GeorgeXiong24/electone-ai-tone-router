# Prerequisites: Yamaha ELB-02 as a MIDI Controller with a Windows PC

This document describes how to use a **Yamaha ELB-02 (STAGEA Basic)** as the keyboard/controller while a Windows PC generates the sound.

> Model note: this guide is written for the ELB-02. Other Electone models (ELS-02 series, ELC-02, ELB-01, etc.) may have different connectors, voice sets, and MIDI behavior.

## 1. Scope and limitations

- The ELB-02 sends MIDI over **USB TO HOST**; it does not carry audio.
- Custom/external voices **cannot be installed into the ELB-02**. The PC is the sound source; the instrument is the controller and, optionally, the amplifier.
- The instrument's built-in voices must be muted, otherwise they layer with the PC sound.
- There is no HDMI, no user sample memory, and no official voice-loading function.

## 2. Hardware

- **USB 2.0 A–B cable**, under 3 m. USB 3.0 A–B cables are not supported by the instrument.
- **3.5 mm stereo cable**: PC headphone/interface output → ELB-02 **AUX IN** (stereo mini jack).
- Optional: **USB sustain pedal** (the ELB-02 has no sustain pedal input).
- Optional: **USB audio interface** for lower latency and better audio quality.
- Optional: ground-loop isolator if hum is present.

## 3. Software (Windows 10/11, 64-bit)

- **Cantabile Lite** — VST host.
- **ASIO4ALL** — only if using onboard audio; otherwise use the audio interface's ASIO driver.
- **VST3 instruments**, for example:
  - Sforzando (SFZ player)
  - Decent Sampler (`.dspreset` / `.dslibrary` player)
  - Surge XT (synthesizer)
- **Sample libraries** in SFZ or Decent Sampler format (piano, strings, bass, etc.).
- Optional: **MIDI-OX** for diagnostics, **ReaEQ** or **TDR Nova** for EQ.

Install 64-bit VST3 plugins to:

```text
C:\Program Files\Common Files\VST3
```

The ELB-02 uses the operating system's standard USB-MIDI driver on Windows/macOS; installing a Yamaha driver is not required unless the connection is unstable.

## 4. Configure Cantabile Lite

1. `Tools → Options → Audio Engine`
   - Audio Driver: `ASIO`
   - Device: `ASIO4ALL v2` (or the audio interface)
   - Sample Rate: `48000`
   - Buffer Size: `256` (lower to 128 if stable)
2. `Tools → Options → Audio Ports`
   - Confirm Main Speakers are mapped to output channels 1/2.
3. `Tools → Options → Plugin Options`
   - Add `C:\Program Files\Common Files\VST3`.
4. `Tools → Scan Plugin Folders` → `Full`.
5. Insert an instrument (`Insert → Plugin...`) and load a sample library.
6. Connect the ELB-02 with the USB cable.
7. `Tools → Options → MIDI Ports`
   - Create a port named `ELB-02`.
   - Assign the physical ELB-02 MIDI input.
   - `Enabled`: on; `Default Port`: off; `Enable Loopback`: off; `Route MIDI Clock`: off.
8. Create one MIDI route per instrument:
   - Upper keyboard → channel `1`
   - Lower keyboard → channel `2`
   - Pedalboard → channel `3`
9. Use **one instrument instance per keyboard** to avoid same-note conflicts.
10. Save the project (`File → Save As`).

## 5. Electone settings

- Set the volume of every voice to 0.
- Do not run MDR playback or recording while sending MIDI.

## 6. Audio path

- In Windows, select the output jack or audio interface connected to the instrument's **AUX IN**.
- With ASIO, Windows' default output setting does not affect Cantabile; select the device in the ASIO control panel instead.

## 7. Test checklist

- Upper, lower, and pedal keyboards each trigger only their own instrument.
- The same pitch played on two keyboards does not cut off.
- No built-in ELB-02 voice is audible.
- No dropouts at 128–256 samples.
- The project and the `CTRL` registration are backed up.

## 8. Known limitations

- USB TO HOST carries MIDI only: no audio and no file transfer.
- No user sample/voice memory; external voices remain on the PC.
- The 10-note polyphony limit applies to the internal tone generator only.
- USB TO HOST and the 5-pin MIDI terminals are mutually exclusive.
- A USB wireless LAN adapter disables both MIDI and USB TO HOST.
- MDR playback/recording blocks MIDI transmission.

## 9. Troubleshooting

| Symptom | What to check |
| --- | --- |
| No sound | Audio driver/device, plugin loaded, routing, output level |
| Electone's own voice is audible | Silent registration not applied |
| Same note cuts off | Both keyboards feed one instance; use separate instances |
| Muffled sound | Disable plugin effects, use a close mic, add a gentle high-shelf EQ |
| High latency | Use ASIO, 128–256 samples, avoid Bluetooth audio |
| Windows music does not reach the instrument | Output device, cable, or Cantabile's exclusive mode |
