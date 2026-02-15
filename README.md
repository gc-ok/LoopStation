# Loop Station

Professional audio loop player with **seamless looping** — built for live theater rehearsals, music directors, and stage managers.

## Features

### Core Audio
- **Seamless Looping** — No gaps, clicks, or glitches. Audio is pre-processed in RAM so loops are mathematically perfect
- **Precise Control** — Set loop in/out points with millisecond accuracy, fine-tune with keyboard nudge controls
- **Named Vamps** — Save multiple loop regions per song with individual crossfade, entry, and exit settings
- **Skip Regions** — Mark sections of audio to automatically skip during playback
- **Auto Loop Detection** — Finds natural loop points using built-in audio analysis
- **Visual Waveform** — See your audio, loop regions, markers, and playhead in real time

### Cue Management
- **Cue Points & Markers** — Drop named cue markers anywhere in the timeline for instant navigation
- **Per-Tag Notes** — Annotate each cue with role-specific notes for Director, Tech, Lighting, Sound, Stage, Costumes, and Props. Each department gets its own notes on every cue
- **Live Cue Sidebar** — Real-time display of current cue, next cue, countdown timer, and all tag notes
- **Unified Cue Sheet** — Single timeline view of all markers, vamps, and skip regions sorted chronologically

### Backstage Sharing
- **Local Network Monitor** — One click to share a live cue display to any phone or tablet on the same WiFi
- **Mobile Optimized** — Dark-themed web page with large countdown timer, current/next cue, and all tag notes. Designed for backstage readability
- **QR Code Access** — Scan with your phone camera to open the monitor instantly. No app install needed on the viewing device
- **Screen Wake Lock** — Keeps phone screens on so crew doesn't miss cues

### Workflow
- **Full Keyboard Control** — Every action has a shortcut. Shortcuts automatically pause when typing in notes
- **Song Library** — Browse and load songs from any folder
- **Persistent Storage** — All loop points, markers, notes, and settings are saved per song automatically
- **Multiple Themes** — Choose from several color themes

## Getting Started

1. Open Loop Station
2. Click 📁 in the library sidebar to select a folder containing your audio files
3. Click a song to load it
4. Press **Space** to play
5. Press **I** to set loop IN, **O** to set loop OUT — seamless looping starts automatically
6. Press **M** to add cue markers, **N** to open the notes sidebar

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Space` | Play / Pause |
| `Escape` | Stop |
| `I` | Set loop IN at current position |
| `O` | Set loop OUT at current position |
| `E` | Exit loop at next boundary |
| `F` | Fade exit from loop |
| `S` | Save all data |
| `M` | Add cue marker at playhead |
| `N` | Toggle cue details sidebar |
| `←` / `→` | Nudge position ±0.1s |
| `Ctrl+←` / `Ctrl+→` | Nudge position ±1.0s |
| `[` / `]` | Jump to previous / next marker |

All keyboard shortcuts are automatically suppressed when you're typing in a text field.

## Using Cue Notes

The cue details sidebar (press **N**) has three sections:

**NOW** shows the current cue you're inside, with all its tagged notes.

**UP NEXT** shows the next cue with a large countdown timer that changes color as it approaches: green when comfortable, yellow under 15 seconds, red under 5 seconds.

**EDIT TAGS & NOTES** lets you add role-specific annotations. Select "+ Add Tag" to add a department (Director, Tech, Lighting, etc.), then click Edit to write notes for that tag. Each tag has its own Save button, so different departments' notes are managed independently.

## Sharing to Backstage Devices

Click **📡 Share** in the header bar. A popup shows the URL and QR code — anyone on the same WiFi network can scan or type the URL on their phone, tablet, or laptop to see the live cue monitor. No app download required on their end.

The monitor shows the current cue, next cue, countdown, and all tag notes in a mobile-friendly dark layout. Click the button again to stop sharing.

## Audio Fine-Tuning

If you hear a **gap when exiting a loop**, open the vamp settings (gear icon on the vamp row) and increase the exit fade duration. If you hear **clicks at the loop boundary**, increase the crossfade setting. Each vamp can have its own tuning.

## System Requirements

- **macOS** 12 (Monterey) or later
- **Windows** 10 or later
- Audio files: MP3, WAV, FLAC, OGG, M4A, AAC, WMA, AIFF

## Support & Feedback

- **Website:** [gceducationanalytics.com](https://www.gceducationanalytics.com)
- **Support the Developer:** [gceducationanalytics.com/support](https://www.gceducationanalytics.com/support)
- **Send Feedback:** [gceducationanalytics.com/feedback](https://www.gceducationanalytics.com/feedback)

## Legal

Loop Station is proprietary software. © GC Education Analytics. All rights reserved.

This software uses FFmpeg (http://ffmpeg.org) licensed under the LGPLv2.1 for audio format conversion. FFmpeg binaries are included unmodified. See THIRD_PARTY_LICENSES.txt included with this application for complete third-party license information.
