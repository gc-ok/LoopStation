# Loop Station

Professional audio loop player with **seamless looping** — built for live theater rehearsals, music directors, and stage managers.

See [Known Issues and Performance Readiness](#known-issues-and-performance-readiness) for current release limitations.

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

### Show Control
- **GO Button** — One big GO (Enter / keypad Enter) performs whatever the STANDBY line shows: play, release a hold, or exit the current vamp. A double press within 300 ms is ignored (and says so)
- **Hold Cues (Caesuras)** — Drop a hold (**H**) and playback stops exactly there until GO. The GO panel turns amber with a HELD banner
- **Vamp Pass Counter** — "Pass 2 of 3" while a counted vamp repeats
- **Live Mode** — **L** or the header button locks song switching, cue/vamp/hold editing, theme and tempo changes during a show (red LIVE badge); blocked actions are reported in the status line
- **GO Log** — Every GO is logged with time and source (shown in the cue sheet footer and on the backstage monitor)
- **MIDI Control & Learn** — Map any MIDI note / CC / program change to GO, play/pause, stop, exit/fade/skip vamp, next/previous cue, add marker or Live Mode (⚙ Settings → Learn)
- **Foot Pedals & Custom Keys** — Page-turner pedals (AirTurn, PageFlip) work as keys; remap GO and other actions in ⚙ Settings
- **OSC Output** — Sends GO, cue, vamp and hold events to QLab, lighting or show-control software (loopstation or QLab address scheme, with a "Send test" button)

### Rehearsal Tools
- **Tempo & Key** — Slow a song down or speed it up (50–150 %) and transpose ±6 semitones without moving your cues (🎵 Song)
- **Tempo Map** — Enter BPM / meter, detect it automatically or tap it; adds a "Bar 12 · 3" readout next to the clock, a bar grid on the waveform and **Go to bar**
- **Loop Selection** — Loop any passage you drag on the waveform for drilling, without creating a vamp
- **Stems & Click** — Per-stem mute / solo / gain for songs with stems (`<song>.stems/` folder), click routed to the right channel or to separate outputs, and output-device selection

### Backstage Sharing
- **Local Network Monitor** — One click to share a live cue display to any phone or tablet on the same WiFi
- **Mobile Optimized** — Dark-themed web page with large countdown timer, current/next cue, and all tag notes. Designed for backstage readability
- **QR Code Access** — Scan with your phone camera to open the monitor instantly. No app install needed on the viewing device
- **Screen Wake Lock** — Keeps phone screens on so crew doesn't miss cues
- **Department Acks** — Crew tap ACK on their phones; acknowledgements appear on the desktop notes sidebar

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
| `Enter` / keypad `Enter` | GO (perform the STANDBY action) |
| `L` | Toggle Live Mode |
| `H` | Add hold at playhead |
| `Home` | Go to start |
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

All keyboard shortcuts are automatically suppressed when you're typing in a text field. GO, Live Mode, hold, play/stop, exit/fade, marker and Home keys can be remapped (for example to a foot pedal) in **⚙ Settings**.

## Running a Show

1. Load the song and press **L** to enter Live Mode (the header shows a red **LIVE** badge).
2. Watch the **STANDBY** line: it always says what GO will do next.
3. Press **GO** (Enter, a foot pedal or a mapped MIDI button) on each cue. At a hold the panel turns amber — GO releases it. In a vamp, GO exits at the end of the current pass.
4. Press **L** again after the show to unlock editing.

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

## Known Issues and Performance Readiness

All eleven findings from the October 14, 2026 review have been corrected in the working tree. The changes are covered by regression tests; packaged builds and physical show equipment still need release validation.

| ID | Correction |
|----|------------|
| LS-01 | Loop bodies retain the full selected duration. The seam blends the loop head with a short continuation after the loop end, or an extended final sample at EOF, without shortening the musical period. |
| LS-02 | The Mac FFmpeg build enables `atempo` and `asetrate` alongside `aresample`. CI selects the exact bundled FFmpeg for the audio integration tests and runs the phone-monitor JavaScript regressions with Node. |
| LS-03 | Failed saves remain pending for retry and emit a visible error. Save confirms a successful write; recovery clears the error only after persistence succeeds. |
| LS-04 | The phone monitor stops its clock and prominently reports stale/disconnected updates. Requests time out, and polling recovers after reconnection. A frozen desktop update timestamp is also detected. |
| LS-05 | Component stems start muted; the full mix and additional click retain their defaults. Song settings offers **Full mix / Stems / Custom** selection and output level/clipping feedback. Explicit saved stem choices are preserved. |
| LS-06 | Vamp entry occurs at the exact loop end. Holds in the former crossfade window, including one exactly at the boundary, fire before looping and GO resumes correctly. |
| LS-07 | Desktop and phone countdowns use the applied playback rate. Counted vamps add their remaining passes; timing beyond an unreleased hold or indefinite vamp is shown as unknown. Source-position clocks remain on the original track timeline. |
| LS-08 | Paused/held/stopped state overrides loop mode in the phone monitor. Pausing a vamp freezes its countdown and shows **Paused**. |
| LS-09 | Tempo/key requests are tracked separately from applied settings. Completion is reported after the actual buffer swap, and failed requests retain and save the settings of the audio still playing. |
| LS-10 | Final desktop shutdown/theme restart closes the audio engine and OSC sender and joins their workers. Reusable audio cleanup remains separate from final close. |
| LS-11 | Live Mode locks tempo-map and song OSC fields/buttons and rejects edits at the model boundary, including per-item notes and OSC mappings. Live playback controls remain available. |

**Stem playback:** when replacing the primary full mix with instrument stems, select **Stems** in Song settings. The click is managed independently. Manually combining sources is displayed as **Custom**; lower gains if the clipping indicator appears.

Regression coverage is in [release regressions](tests/test_release_regressions.py), [audio engine tests](tests/test_audio_engine.py), [integration tests](tests/test_integration.py), [frontend smoke tests](tests/test_frontend_smoke.py), and the [monitor JavaScript checks](tests/monitor_regression.js). See the [review history and resolution details](docs/performance-readiness-review.md).

**Verification:** all **241 test cases passed** on Windows with Python 3.13.6, including the Node monitor checks; pyflakes passed. Preferences, cue data, and audio caches were isolated from user data during testing.

Release validation still includes the packaged Mac app, signing/notarization, real audio and MIDI devices, audible music quality, long show runs, and real phone/browser behavior. Automated checks use synthetic audio and device-free streams.

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
