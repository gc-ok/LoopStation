# Loop Station

A professional audio loop player with **mathematically-perfect seamless looping**.

## Features

- 🔄 **Seamless Looping** - No gaps, clicks, or glitches when looping
- 🎚️ **Precise Control** - Set loop in/out points with millisecond precision
- 👁️ **Visual Waveform** - See your audio and loop region
- 💾 **Save Loop Points** - Loop points are saved per song
- ⌨️ **Keyboard Shortcuts** - Fast workflow for musicians
- 🎨 **Modern UI** - Dark theme with intuitive controls

## Architecture Overview

```
loop_station/
├── main.py                 # Entry point
├── config.py               # All tunable constants
├── backend/                # Audio processing (no UI)
│   ├── audio_engine.py     # Core audio playback
│   └── state_manager.py    # State & coordination
├── frontend/               # UI components
│   ├── app.py              # Main window
│   ├── waveform.py         # Waveform display
│   ├── transport.py        # Play/pause/stop
│   ├── loop_controls.py    # Loop in/out controls
│   └── library.py          # Song library sidebar
├── utils/                  # Helper functions
│   └── formatting.py       # Time formatting
├── data/                   # Saved loop data
└── logs/                   # Application logs
```

## How It Works

### The Problem with Traditional Looping

Most audio players loop by seeking back to the start when reaching the end. This causes:
- Buffer underruns (gaps)
- Timing jitter (Python/OS scheduling)
- Clicks at the loop boundary

### Our Solution: "Slice, Process, and Pre-load"

Loop Station uses a two-mode architecture:

#### 1. Transport Mode
Uses `pygame.mixer.music` for streaming playback:
- Good for playing the whole song
- Good for scrubbing and seeking
- Good for finding loop points

#### 2. Loop Mode  
Uses `pygame.mixer.Sound` with pre-processed audio in RAM:
- Extracts exact samples from loop_in to loop_out
- Applies crossfade at the seam (eliminates clicks)
- Plays with `loops=-1` (infinite loop at SDL/C layer)
- **Python is removed from the timing-critical path**

```
         Transport Mode                    Loop Mode
    ┌─────────────────────┐         ┌─────────────────────┐
    │  Streaming from     │         │  Pre-sliced audio   │
    │  disk via pygame    │  ───►   │  in RAM, crossfaded │
    │  mixer.music        │         │  at boundaries      │
    └─────────────────────┘         └─────────────────────┘
    
    Good for seeking                 Good for seamless loops
```

## Installation

### Requirements

- Python 3.8+
- ffmpeg (must be in PATH or specify with `--ffmpeg`)

### Dependencies

```bash
pip install pygame numpy matplotlib customtkinter
```

### Optional (for advanced loop detection)

```bash
pip install librosa
```

### Run

```bash
python main.py
```

Or with debug logging:

```bash
python main.py --debug
```

## Usage

### Basic Workflow

1. Click 📁 to select a music folder
2. Click a song to load it
3. Press **Space** to play
4. Press **I** to set loop IN point
5. Press **O** to set loop OUT point
6. Watch as it loops seamlessly!
7. Press **E** to exit the loop
8. Press **S** to save loop points

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Space` | Play / Pause |
| `Escape` | Stop |
| `I` | Set loop IN at current position |
| `O` | Set loop OUT at current position |
| `E` | Exit loop (at next boundary) |
| `S` | Save loop points |
| `←` / `→` | Nudge position ±0.1s |
| `Ctrl+←` / `Ctrl+→` | Nudge position ±1.0s |

### Fine-Tuning Loop Points

Use the `+` / `-` buttons next to IN and OUT to adjust by 10ms increments.

Or type exact times in the entry fields (format: `M:SS.ms` or just seconds).

## Configuration

All tunable constants are in `config.py`. Key settings:

### Audio Timing (adjust if you hear issues)

```python
# Crossfade at loop boundary (ms)
LOOP_CROSSFADE_MS = 15

# Exit patch duration (ms) - bridges gap when exiting loop
EXIT_PATCH_DURATION_MS = 100

# Transport resume offset (ms) - when to start transport after exit
TRANSPORT_RESUME_OFFSET_MS = 80
```

### If you hear a gap when exiting loop:
1. Increase `EXIT_PATCH_DURATION_MS`
2. Or decrease `TRANSPORT_RESUME_OFFSET_MS`

### If you hear clicks at loop boundary:
1. Increase `LOOP_CROSSFADE_MS`

## Code Structure Explained

### Backend (No UI Dependencies)

**`audio_engine.py`** - Pure audio processing:
- Loads files via pygame and ffmpeg
- Generates seamless loop sounds with crossfading
- Manages transport/loop mode switching
- Can be tested without any UI

**`state_manager.py`** - Coordinates everything:
- Owns the AudioEngine
- Manages playback state
- Runs monitor thread for position updates
- Handles loop transitions
- Emits events for UI updates

### Frontend (UI Only)

**`app.py`** - Main window:
- Creates and arranges widgets
- Wires callbacks between UI and StateManager
- Routes events to appropriate UI updates

**`waveform.py`** - Waveform display:
- Renders audio waveform with matplotlib
- Shows loop region and markers
- Handles click-to-seek

**`transport.py`** - Transport controls:
- Play/pause/stop buttons
- Time display

**`loop_controls.py`** - Loop controls:
- Set IN/OUT buttons
- Adjustment buttons (+/- 10ms)
- Manual entry fields
- Exit loop button
- Save button

**`library.py`** - Song library:
- Folder browser
- Song list
- Highlights current song

### Event System

StateManager uses an event-driven architecture:

```python
# Register for events
state.on('position_update', lambda pos, loop: update_ui(pos))
state.on('loop_mode_enter', lambda: show_loop_indicator())

# Events are emitted automatically when state changes
```

Available events:
- `position_update` - Playback position changed
- `state_change` - Play/pause/stop state changed
- `loop_mode_enter` - Entered seamless loop mode
- `loop_mode_exit` - Exited loop mode
- `song_loaded` - New song loaded
- `song_ended` - Song finished playing
- `loop_points_changed` - Loop in/out points changed

## Debugging

### Enable Debug Logging

```bash
python main.py --debug
```

Logs are saved to `logs/loop_station_YYYYMMDD_HHMMSS.log`

### Log Format

```
HH:MM:SS.mmm [LEVEL] message
```

### Key Log Messages

```
>>> GENERATING SEAMLESS LOOP SOUND <<<     # Loop generation started
>>> LOOP SOUND READY (24.5ms) <<<          # Loop ready to use
>>> SWITCHING TO LOOP MODE <<<              # Transitioning to loop
♻ LOOP MODE: pos=143.2s cycle_pos=0.5s    # Currently looping
⮑ Exit boundary reached - executing exit   # Exiting loop
▶ Starting transport at 145.790s           # Resuming normal playback
```

### Common Issues

**"Loop sound not ready" warning:**
- Loop points were set but generation hasn't finished
- Wait a moment or check for errors in the log

**Gap when exiting loop:**
- Increase `EXIT_PATCH_DURATION_MS` in config.py
- Or decrease `TRANSPORT_RESUME_OFFSET_MS`

**Click at loop boundary:**
- Increase `LOOP_CROSSFADE_MS` in config.py

## Future Improvements

- [ ] Web-based UI with Tauri + React for better visuals
- [ ] Auto loop detection (find natural loop points)
- [ ] Multiple loop regions
- [ ] MIDI control support
- [ ] Audio effects (pitch shift, time stretch)

## License

MIT License - feel free to use and modify!