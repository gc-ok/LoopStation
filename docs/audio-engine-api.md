# Loop Station internal API contract (v2)

This document is the contract between `backend/audio_engine.py` (the mixer),
`backend/state_manager.py` (show logic), the control modules
(`backend/midi_control.py`, `backend/osc_output.py`), `backend/web_server.py`
and the frontend. Workers implementing different modules code against this
file; keep it updated when a signature changes.

Times are always **source seconds** (the time axis of the original audio file)
unless stated otherwise. Tempo changes never move cue times.

---

## 1. AudioEngine (`backend/audio_engine.py`)

Owns playback through a `sounddevice.OutputStream` callback. All mixing,
looping, holds, skips and routing happen inside the callback on a pure
`MixerCore` so they are sample-accurate and testable without a device.

```python
class StemInfo(NamedTuple):
    name: str          # "mix" is the primary track; "click" is special
    gain_db: float
    muted: bool
    solo: bool

class AudioEngine:
    def __init__(self, ffmpeg_path="ffmpeg", device=None, sample_rate=44100,
                 block_size=512, stream_factory=None)
    # stream_factory(callback, channels, sample_rate, block_size[, finished_callback=])
    # -> object with start()/stop()/close() (and optionally `active`); tests pass
    # a fake that pulls callback() itself. If the factory accepts a
    # `finished_callback` keyword (or **kwargs) it is given one and must call it
    # when the stream stops (like sounddevice).

    # ---- events (fired on a worker thread, never inside the audio callback)
    def on_event(self, callback: Callable[[str, dict], None]) -> None
    # event names and payloads:
    #   "loop_entered"   {loop_in, loop_out}
    #   "loop_cycle"     {cycle: int}            # 1-based, each wrap
    #   "loop_exited"    {position, reason: "cut"|"fade"|"count"|"seek"}
    #   "hold_reached"   {position}
    #   "hold_released"  {position}
    #   "skip_executed"  {from_pos, to_pos}
    #   "song_ended"     {}
    #   "render_started" {tempo_ratio, semitones}
    #   "render_done"    {tempo_ratio, semitones}
    #   "render_failed"  {message}
    #   "device_error"   {message}   # open failed, render failed, or the stream
    #                                # finished without us closing it (device lost)
    #   "device_restored" {name}     # restart_stream() succeeded (device name)
    #   "stopped"        {position, reason: "fade"}  # a transport (non-loop) fade
    #                                # exit reached silence; engine is stopped

    # ---- devices / routing
    def list_output_devices(self) -> list[dict]   # {index, name, channels, default: bool, hostapi}
    def set_output_device(self, index: int | None) -> None   # None = system default; restarts stream
    #   If the device cannot be opened, the previous device is reopened and
    #   RuntimeError(message) is raised (caller should not persist the choice).
    def get_output_device(self) -> dict | None
    def set_routing(self, mode: str) -> None     # "stereo" | "click_right" | "multichannel"
    #   If a working stream cannot be reopened with the new channel count (e.g.
    #   "multichannel" on a 2-channel device), the previous routing is restored,
    #   the stream reopened, and RuntimeError(message) is raised. With no working
    #   stream before the call the change is kept (nothing to protect).
    #   stereo:        mix (+click if unmuted) summed to outs 0,1
    #   click_right:   mix mono -> out 0, click -> out 1
    #   multichannel:  mix -> channel_map["mix"], click -> channel_map["click"]
    def set_channel_map(self, mix=(0, 1), click=(2, 3)) -> None
    #   set_channel_map raises/reverts the same way as set_routing.
    def get_routing(self) -> dict               # {mode, channel_map, output_channels}

    # ---- device health (for a watchdog in the state manager)
    frames_rendered: int                        # frames produced by the callback; monotonic,
                                                # never reset (seeks/loads/swaps included).
                                                # Not advancing while playing = callback dead.
    underflow_count: int                        # output underflows flagged in callback status
    def is_stream_active(self) -> bool          # stream open, started and not lost
    def restart_stream(self) -> bool
    #   Close and reopen the stream on the current device; if that fails and a
    #   specific device was selected, fall back to the system default (which then
    #   becomes the selection). Transport state lives in the core, so playback
    #   resumes at the same sample. Emits "device_restored" {name} on success,
    #   "device_error" on failure. Safe to call from any non-audio thread.

    # ---- loading
    def load_file(self, path: str, stem_paths: dict[str, str] | None = None) -> tuple[float, float]
    #   decodes `path` as stem "mix" and each stem_paths[name] (same rate/channels)
    #   to WAV temp files under <tempdir>/LoopStation, memmaps them.
    #   returns (duration_seconds, 1.0)   # second value kept for compatibility
    def get_stems(self) -> list[StemInfo]
    #   New component stems default muted; primary mix and click retain their
    #   defaults. Saved explicit stem settings override these defaults.
    def set_stem_gain(self, name: str, gain_db: float) -> None
    def set_stem_mute(self, name: str, muted: bool) -> None
    def set_stem_solo(self, name: str, solo: bool) -> None
    def get_raw_audio_data(self)                 # int16 memmap of the "mix" stem (frames, 2), source tempo
    song_length: float                           # source seconds
    current_file_path: str | None

    # ---- tempo / key (offline render via ffmpeg filters; hot-swapped when done)
    def set_tempo_pitch(self, tempo_ratio: float = 1.0, semitones: float = 0.0) -> None
    #   tempo_ratio 0.7..1.3 recommended, semitones -4..+4. "click" stem gets
    #   tempo only. Playback continues on the old buffers until "render_done".
    def get_tempo_pitch(self) -> tuple[float, float]
    #   Applied values; requests remain pending until a buffer swap. Failures
    #   leave these values unchanged. render_done is emitted on actual apply.
    def get_playback_rate(self) -> float          # source seconds / playback second
    def get_output_levels(self) -> dict          # peak (preclip), clipped_blocks

    # ---- transport (positions in source seconds)
    def play_transport(self, start_pos: float | None = None) -> None
    def pause_transport(self) -> None
    def unpause_transport(self) -> None
    def seek_transport(self, position: float) -> None     # also exits loop/hold
    def stop(self) -> None
    def toggle_play_pause(self) -> None
    def get_position(self) -> float
    is_playing: bool; is_paused: bool; mode: str          # "transport" | "loop"
    def is_transport_active(self) -> bool
    def is_loop_active(self) -> bool

    # ---- loops / vamps
    def set_loop_points(self, loop_in, loop_out, crossfade_ms=20) -> None   # builds body immediately
    def get_loop_points(self) -> tuple[float, float, int]
    def is_loop_ready(self) -> bool
    def arm_loop(self, loop_in, loop_out, crossfade_ms=20, count: int | None = None,
                 entry_fade_ms: int = 15) -> None
    #   The callback enters loop mode exactly when the playhead reaches loop_out
    #   (no monitor-thread timing). count=None loops until exit; count=N exits
    #   automatically after N additional passes ("loop_exited" reason "count").
    #   The state manager sends total_plays - 1. Bodies retain their full
    #   duration; crossfading never changes the repeat period.
    def disarm_loop(self) -> None
    def start_loop_mode(self, fade_in_ms=15, count: int | None = None) -> bool  # enter now (manual)
    #   Manual entry loops until exit unless count is given; a count from an
    #   earlier arm_loop is never inherited.
    def queue_loop_exit(self, mode: str = "cut", fade_ms: int | None = None) -> None
    #   "cut": at the next wrap continue the song from loop_out, seamless.
    #   "fade": start fading now over fade_ms, then stop (state becomes stopped).
    #     In a loop this ends with "loop_exited" reason "fade"; in transport it
    #     ends with "stopped" {position, reason: "fade"}. Paused/held transport
    #     stops at once ("stopped"); a stopped engine ignores it.
    def cancel_loop_exit(self) -> None
    def execute_loop_exit(self) -> None                     # exit immediately (compat)
    def execute_fade_exit(self, fade_ms=None) -> None       # alias of queue_loop_exit("fade")
    def get_loop_cycle_position(self) -> float
    def get_loop_cycle_count(self) -> int

    # ---- holds (caesura): pause exactly at a sample and wait for GO
    def hold_at(self, position: float) -> None              # arm (one at a time)
    def clear_hold(self) -> None
    def release_hold(self) -> None                          # resume from the held sample
    is_held: bool

    # ---- skips (cuts): executed sample-accurately in the callback
    def set_skip_regions(self, regions: list[tuple[float, float]]) -> None

    # ---- lifecycle
    def cleanup(self) -> None
    #   Reusable cleanup: release song/stream resources.
    def close(self) -> None
    #   Final shutdown: cleanup, terminate/join event/render workers; idempotent.
    def sweep_stale_temp_files(self) -> int
```

Fades: loop entry fade-in (`entry_fade_ms`), fade exit, and a 10 ms declick on
every seek/skip/hold are applied inside the callback. The declick tail of the
old state wraps into the loop body when in a loop and fades to zero when it
runs into the song end.

Tempo swaps never build loop bodies in the audio callback: bodies for a
pending swap are prebuilt by `set_loop_points`/`arm_loop`/`set_tempo_pitch`;
if they are stale when the callback wants to swap, the swap is deferred and
rebuilt on a worker thread.

## 2. StateManager additions (`backend/state_manager.py`)

Existing API stays. New or changed:

```python
class LoopRegion:            # + repeat_count: int | None  (None = indefinite)
class HoldCue:               # id, time, name, tag_notes  (caesura; saved under "holds")
class SongSettings:          # tempo_ratio, semitones, bpm, beats_per_bar, downbeat_offset,
                             # stems: {name: {gain_db, muted, solo}}, routing_mode, osc_cue
                             # saved per song under "settings"

PlaybackState: STOPPED, PLAYING, PAUSED, HELD

# cue model
add_hold(time=None, name=None) -> HoldCue;  rename_hold(id, name);  delete_hold(id)
update_selected_loop(..., repeat_count=None)      # 0 or None = indefinite
get_timeline_items() -> [(time, "marker"|"vamp"|"hold", obj)]   # time-sorted
holds: list[HoldCue]

# GO / standby model (Stage|Tracks "Advance" semantics)
standby() -> dict      # {"action": "play"|"release_hold"|"exit_vamp"|"none", "label": str,
                       #  "next_item": obj|None, "next_type": str|None}
go(source="ui") -> bool          # performs standby()["action"]; returns False if ignored
                                 # ignores a second GO within GO_DEBOUNCE_MS (300)
go_log: list[dict]               # {ts, source, action, position, label}; also appended to
                                 # <data_dir>/go_log.jsonl ; event "go_fired" (entry)

# live mode
live_mode: bool;  set_live_mode(bool)   # when True: delete/rename/add/edit methods return
                                        # False and emit "blocked_in_live_mode" (what)

# tempo / key / stems / routing (persist into SongSettings, forward to engine)
set_tempo_pitch(tempo_ratio, semitones);  set_stem_gain/mute/solo(name, ...)
pending_tempo_pitch: tuple[float, float] | None   # request; settings retain applied values
set_playback_source(source) -> bool              # "Full mix" | "Stems"; click unchanged
get_playback_rate() -> float
countdown_to(target, position=None) -> float | None  # playback seconds; None beyond indefinite waits
set_routing(mode);  set_output_device(index)
set_song_tempo_map(bpm, beats_per_bar, downbeat_offset)
estimate_tempo() -> (bpm, downbeat_offset)      # librosa, background, event "tempo_estimated"
bar_beat_at(position) -> tuple[int, int, float] # (bar, beat, fraction) 1-based; (0,0,0) if no bpm

# stems discovery (done in load_song)
#   <song>.stems/<name>.wav|flac|mp3   or   "<song stem> - <name>.<ext>" beside the song
#   a stem whose name contains "click" is the click stem

# events (all emit copies)
"holds_changed"(holds), "hold_reached"(hold), "hold_released"(),
"standby_changed"(standby dict), "go_fired"(entry), "live_mode_changed"(bool),
"settings_changed"(SongSettings), "render_state"(state: "rendering"|"done"|"failed", msg),
"stems_changed"(list[StemInfo]), "tempo_estimated"(bpm, offset), "blocked_in_live_mode"(what),
"loop_cycle"(n, total|None)
"save_failed"(message), "save_succeeded"()
flush_save() -> bool           # dirty state retained on failure; retry without another edit
```

The monitor thread keeps emitting `position_update` (~30 Hz) and arms the
engine: next vamp (`arm_loop` with that vamp's crossfade/count/entry fade),
next hold (`hold_at`), skip regions (`set_skip_regions`). Engine events drive
state transitions; the monitor no longer does timing-critical work.

## 3. MIDI / key control (`backend/midi_control.py`)

```python
ACTIONS = ["go", "play_pause", "stop", "exit_vamp", "fade_exit", "skip_vamp",
           "next_cue", "prev_cue", "add_marker", "toggle_live"]
class MidiController:
    def __init__(self, on_action: Callable[[str], None])
    def list_inputs(self) -> list[str]
    def open(self, port_name: str | None) -> bool;  def close(self)
    def learn(self, action: str, timeout_s=10.0) -> None   # next message maps to action; event via on_learned
    on_learned: Callable[[str, str], None]                  # (action, message_key)
    mappings: dict[str, str]        # message_key -> action, e.g. "note_on:60" / "cc:64"
    def load_mappings(dict); def save_mappings() -> dict
```
Backend: `mido` with `mido.backends.pygame` (no compiler needed); rtmidi used if
importable. Polls on a daemon thread; never touches Tk. Mappings persist in
user preferences under "midi".

Foot pedals that act as keyboards (AirTurn, PageFlip) are handled by the
frontend key map (preferences "keys": action -> keysym), default: GO = Return
or Right, plus the README shortcuts.

## 4. OSC output (`backend/osc_output.py`)

```python
class OscOutput:
    def __init__(self); def configure(enabled, host, port, scheme="loopstation"|"qlab")
    def send_event(name, **kw)   # loopstation scheme: /loopstation/<name> with args
    def send_cue(osc_cue: str)   # qlab scheme: /cue/<osc_cue>/start ; loopstation: /loopstation/cue <osc_cue>
```
StateManager calls `send_event` for go, cue_reached(marker/hold id, name), vamp_enter,
vamp_exit, hold_reached, song_loaded, and `send_cue` when a fired cue has `osc_cue` set.
Settings persist in user preferences under "osc".

## 5. Web monitor (`backend/web_server.py`)

`SharedCueState.update_from_app(app_state, position, current_item, current_type,
next_item, next_type)` additionally reads `app_state.standby()`, `app_state.go_log[-20:]`,
`app_state.live_mode`, `app_state.holds`, and `app_state.state`.
The payload also contains `playback_rate`, playback-second `end_countdown`,
and `updated_at` (last desktop update). Cue countdowns use `countdown_to`;
an unknown arrival beyond a hold/indefinite vamp is `null`.
New: `/api/ack` (POST json {device, cue_id}) -> stores acks per cue_id in
`SharedCueState.acks`; `get_acks(cue_id) -> list[{device, ts}]`; desktop reads
them. Page: department filter (`?dept=` or in-page selector, localStorage),
GO log panel, HELD banner, ACK button, device name prompt (localStorage).

## 6. Frontend surfaces (phase 2)

GO panel (big GO button, STANDBY label, next cue, debounce feedback), Live
Mode toggle (locks destructive UI), hold cue rows/dialog, repeat-count field in
VampModal, Song settings (tempo %, key ±semitones, BPM/meter with "Detect",
stems mixer with mute/solo/gain and routing mode, output device), Settings
dialog (MIDI port + Learn per action, key map, OSC), bar:beat readout next to
the clock, waveform bar grid when BPM is set, GO log in cue sheet footer, acks
in the notes sidebar.
