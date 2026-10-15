"""
State Manager for Loop Station.

Acts as the controller layer between the UI and audio engine.
Manages playback state, keeps the engine armed, and emits events.

This follows an event-driven architecture:
- UI registers callbacks for events it cares about
- StateManager emits events when state changes
- UI updates in response to events

Data model (see docs/audio-engine-api.md section 2):
- Each song has named LoopRegions (vamps), Markers (cue points), HoldCues
  (caesuras), SkipRegions and a SongSettings block (tempo/key/stems/routing).
- Everything is saved per song fingerprint in loop_data.json.

Engine model (contract section 1):
- All timing-critical work (loop entry, wraps, holds, skips, fades) happens
  inside the engine. The monitor thread only emits ``position_update``
  (~30 Hz), keeps the engine armed with the nearest upcoming vamp / hold and
  the active skip regions, and recomputes the GO standby label.
- Engine events (``loop_entered``, ``loop_cycle``, ``loop_exited``,
  ``hold_reached``, ``hold_released``, ``song_ended``, ``render_*``) drive
  state transitions and are translated into StateManager events.

Threading notes:
- Lists (loops, markers, holds, skips) are replaced, never mutated in place,
  so other threads can snapshot them. Events receive copies.
- Engine events arrive on an engine worker thread; MIDI actions on the MIDI
  polling thread. Both only call StateManager methods, which emit events the
  UI consumes through its own queue.
"""

import os
import json
import inspect
import math
import time
import logging
import threading
import uuid
import hashlib
import unicodedata
from contextlib import contextmanager, nullcontext
from enum import Enum, auto
from typing import Callable, Optional, Dict, List, Any, Tuple

import numpy as np

from config import (
    DATA_DIR, LOOP_DATA_FILE, SAMPLE_RATE,
    UI_UPDATE_INTERVAL, UI_EMIT_INTERVAL,
    LOOP_SWITCH_EARLY_MS, SAVE_DEBOUNCE_MS,
    DEFAULT_VAMP_NAME, DEFAULT_MARKER_NAME,
    FADE_EXIT_DURATION_MS, LOOP_CROSSFADE_MS,
    GO_DEBOUNCE_MS, MIN_LOOP_DURATION,
)
from utils.formatting import format_time as _format_time
from utils import preferences
from utils.fileio import (
    read_bytes_with_retry, write_text_atomic, backup_copy,
    replace_with_retry, unique_backup_path,
)
from .loop_detector import LoopDetector
from .midi_control import MidiController
from .osc_output import OscOutput
from .show import Show, ShowFileError, norm_path

logger = logging.getLogger("LoopStation.StateManager")

# How far past a skipped vamp's end the playhead must travel before the vamp
# is re-armed (seconds). Covers the transport resume offset after an exit.
SKIP_FLAG_RELEASE_S = 2.0

# A cue whose time is within this of the playhead counts as "reached", not
# "upcoming" (seconds). Keeps a released hold from re-arming itself.
CUE_EPSILON_S = 0.005
# How far past an armed hold the playhead may read (one audio block plus
# slack) while the engine has not yet reported reaching it.
HOLD_PENDING_S = 0.05

# Device-loss watchdog: while PLAYING, the engine's frames_rendered counter
# must advance at least this often, or the output is considered lost.
DEVICE_STALL_S = 0.3
# While the device is lost, restart_stream() is retried this often.
DEVICE_RETRY_S = 2.0
DEVICE_LOST_LABEL = "Audio device lost - check interface"

# A GO / exit within this long after a vamp ended (on its own count, or by a
# cut / fade exit) is treated as a late or duplicate "exit" press and ignored
# (not "skip the next vamp").
LATE_EXIT_GRACE_S = 1.5

# MIDI toggle_live: leaving Live Mode needs a second press within this window.
LIVE_EXIT_CONFIRM_S = 2.0

DEFAULT_HOLD_NAME = "Hold"
STEM_EXTENSIONS = (".wav", ".flac", ".mp3")
ROUTING_MODES = ("stereo", "click_right", "multichannel")

# Seconds of audio handed to librosa for tempo estimation
TEMPO_ESTIMATE_SECONDS = 60.0


def _compute_tempo(samples, sr: int) -> Tuple[float, float]:
    """librosa beat tracking on int16 (frames, channels) samples -> (bpm, first_beat_s)."""
    import librosa  # heavy import; only when actually estimating
    y = np.asarray(samples, dtype=np.float32) / 32768.0
    if y.ndim == 2:
        y = y.mean(axis=1)
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units='time')
    bpm = float(np.atleast_1d(tempo)[0])
    offset = float(beats[0]) if len(beats) else 0.0
    return bpm, offset


# Song-entry key holding saved items that could not be parsed. They are kept
# verbatim (and carried through later saves) instead of being overwritten.
UNREADABLE_KEY = '_unreadable'
# Song-entry key holding edits made while the data file could not be read
# (see _retry_blocked_read); kept so they are not lost.
SESSION_COPY_KEY = '_unsaved_session_copy'
PRESERVED_ENTRY_KEYS = (UNREADABLE_KEY, SESSION_COPY_KEY)

# Attempts (with short sleeps) to read the loop data file when it is
# locked by another process (antivirus, cloud sync) before giving up.
LOAD_READ_ATTEMPTS = 8

# Characters an OSC cue number must not contain (it becomes part of an OSC
# address). Fallback for backend.osc_output.sanitize_osc_cue.
_OSC_CUE_BAD_CHARS = frozenset('/*?[]{}#,')


def _sanitize_osc(value) -> str:
    """Clean an OSC cue number with osc_output.sanitize_osc_cue (lazy import)."""
    text = str(value or "")
    try:
        from .osc_output import sanitize_osc_cue
    except ImportError:
        sanitize_osc_cue = None
    if sanitize_osc_cue is not None:
        return str(sanitize_osc_cue(text) or "")
    return "".join(ch for ch in text.strip()
                   if ch not in _OSC_CUE_BAD_CHARS and not ch.isspace())


def _finite(value, what: str = "value") -> float:
    """float(value), raising ValueError for nan / inf / non-numbers."""
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{what} is not a finite number: {value!r}")
    return result


def _int_or(value, default: int) -> int:
    """int(value) for a finite number, else ``default`` (lenient load of optional fields)."""
    try:
        return int(_finite(value))
    except (TypeError, ValueError):
        return default


def _migrate_tag_notes(obj, data: dict) -> None:
    """Fill obj.tag_notes from 'tag_notes' or the legacy separate tags/notes fields."""
    if 'tag_notes' in data:
        notes = data['tag_notes']
        obj.tag_notes = dict(notes) if isinstance(notes, dict) else {}
        return
    old_tags = data.get('tags', [])
    if not isinstance(old_tags, list):
        old_tags = []
    old_notes = data.get('notes', '')
    obj.tag_notes = {}
    for tag in old_tags:
        obj.tag_notes[tag] = ''
    if old_notes and old_tags:
        obj.tag_notes[old_tags[0]] = old_notes
    elif old_notes:
        obj.tag_notes['Other'] = old_notes


class SkipRegion:
    """
    A defined section of the song to skip over during playback.
    """
    def __init__(self, start, end, name="Skip", method="cut"):
        self.id = str(uuid.uuid4())
        self.start = start
        self.end = end
        self.name = name
        self.active = True
        self.method = method  # "cut" (instant) or "fade" (reserved; engine cuts)
        self.fade_ms = 500    # Only used if method="fade"

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'start': self.start,
            'end': self.end,
            'active': self.active,
            'method': self.method,
            'fade_ms': self.fade_ms
        }

    @classmethod
    def from_dict(cls, data):
        start = _finite(data['start'], "skip start")
        end = _finite(data['end'], "skip end")
        if end <= start:
            raise ValueError(f"skip end {end} is not after start {start}")
        skip = cls(start, end, name=data.get('name', 'Skip'))
        skip.id = data.get('id', str(uuid.uuid4()))
        skip.active = bool(data.get('active', True))
        skip.method = data.get('method', 'cut')
        skip.fade_ms = _int_or(data.get('fade_ms', 500), 500)
        return skip


class PlaybackState(Enum):
    """Playback state enumeration."""
    STOPPED = auto()
    PLAYING = auto()
    PAUSED = auto()
    HELD = auto()       # parked on a hold cue (caesura), waiting for GO


def _long_enough(start: float, end: float) -> bool:
    """A vamp must be finite and at least MIN_LOOP_DURATION long (the core enforces it too)."""
    return (math.isfinite(start) and math.isfinite(end)
            and end - start >= MIN_LOOP_DURATION - 1e-9)


def _engine_count(repeat_count):
    """Total plays (user-facing) -> extra wraps (engine). None = until GO."""
    if not repeat_count or repeat_count < 2:
        return None
    return int(repeat_count) - 1


class LoopRegion:
    """
    A named loop region (vamp) within a song.

    ``start`` and ``end`` are backed by a single tuple so that a two-point
    edit (``set_range``) is atomic for readers on other threads.

    Attributes:
        id: Unique identifier
        name: Human-readable name (e.g., "Vamp - Scene 3 transition")
        start: Start time in seconds
        end: End time in seconds
        active: Whether this loop will trigger during playback
        repeat_count: None = loop until GO/exit; N (>= 2) = the section is
            heard N times in total, then playback continues on its own
            (theater "vamp x2"). The engine's count is N - 1 extra wraps.
        osc_cue: optional OSC cue id fired when the vamp is entered
    """
    def __init__(self, start, end, name=None):
        self.id = str(uuid.uuid4())
        self._range: Tuple[float, float] = (float(start), float(end))
        self.name = name or DEFAULT_VAMP_NAME
        self.active = True
        # --- Advanced Settings ---
        # 1. "Smooth Entry" (Fade-in from transport)
        self.entry_fade_ms = 15
        # 2. "Smooth Loop Seam" (Crossfade at loop point)
        self.crossfade_ms = LOOP_CROSSFADE_MS
        # 3. "Rhythm Correction" (kept for compatibility; the engine is sample-accurate)
        self.early_switch_ms = LOOP_SWITCH_EARLY_MS
        # 4. "Fade Exit" (Duration when fading out)
        self.exit_fade_ms = FADE_EXIT_DURATION_MS
        # 5. Repeat count (None = indefinite)
        self.repeat_count: Optional[int] = None
        # --- Cue Notes & Tags ---
        # Maps tag name -> notes text for that tag
        self.tag_notes = {}
        self.osc_cue = ""

    @property
    def start(self) -> float:
        return self._range[0]

    @start.setter
    def start(self, value):
        self._range = (float(value), self._range[1])

    @property
    def end(self) -> float:
        return self._range[1]

    @end.setter
    def end(self, value):
        self._range = (self._range[0], float(value))

    def set_range(self, start, end) -> None:
        """Set both points in one atomic assignment."""
        self._range = (float(start), float(end))

    @property
    def tags(self):
        """Convenience: list of active tags."""
        return list(self.tag_notes.keys())

    def to_dict(self):
        """Serialize for JSON storage."""
        return {
            'id': self.id,
            'name': self.name,
            'start': self.start,
            'end': self.end,
            'active': self.active,
            'entry_fade_ms': self.entry_fade_ms,
            'crossfade_ms': self.crossfade_ms,
            'early_switch_ms': self.early_switch_ms,
            'exit_fade_ms': self.exit_fade_ms,
            'repeat_count': self.repeat_count,
            'tag_notes': dict(self.tag_notes),   # copy: the UI edits in place
            'osc_cue': self.osc_cue,
        }

    @classmethod
    def from_dict(cls, data):
        """Deserialize from JSON storage."""
        start = _finite(data['start'], "vamp start")
        end = _finite(data['end'], "vamp end")
        if end <= start:
            raise ValueError(f"vamp end {end} is not after start {start}")
        loop = cls(start, end, name=data.get('name', DEFAULT_VAMP_NAME))
        loop.id = data.get('id', str(uuid.uuid4()))
        loop.active = bool(data.get('active', True))
        loop.entry_fade_ms = max(0, _int_or(data.get('entry_fade_ms', 15), 15))
        loop.crossfade_ms = max(0, _int_or(data.get('crossfade_ms', LOOP_CROSSFADE_MS), LOOP_CROSSFADE_MS))
        loop.early_switch_ms = max(0, _int_or(data.get('early_switch_ms', LOOP_SWITCH_EARLY_MS),
                                              LOOP_SWITCH_EARLY_MS))
        loop.exit_fade_ms = max(0, _int_or(data.get('exit_fade_ms', FADE_EXIT_DURATION_MS),
                                           FADE_EXIT_DURATION_MS))
        count = _int_or(data.get('repeat_count'), 0)
        loop.repeat_count = count if count >= 2 else None
        loop.osc_cue = _sanitize_osc(data.get('osc_cue'))
        _migrate_tag_notes(loop, data)
        return loop


class Marker:
    """
    A named timestamp / cue point within a song.
    Used for quick navigation during rehearsals.

    Attributes:
        id: Unique identifier
        name: Human-readable name (e.g., "Verse 2", "Dialogue starts")
        time: Position in seconds
        color: Optional color for display (hex string)
        tag_notes: Dict mapping tag names to their notes text
        osc_cue: optional OSC cue id fired when the playhead passes the marker
    """
    def __init__(self, time_pos, name=None, color=None):
        self.id = str(uuid.uuid4())
        self.name = name or DEFAULT_MARKER_NAME
        self.time = time_pos
        self.color = color  # None = use default COLOR_MARKER
        self.tag_notes = {}  # {"Director": "notes...", "Tech": "notes..."}
        self.osc_cue = ""

    @property
    def tags(self):
        """Convenience: list of active tags."""
        return list(self.tag_notes.keys())

    def to_dict(self):
        """Serialize for JSON storage."""
        return {
            'id': self.id,
            'name': self.name,
            'time': self.time,
            'color': self.color,
            'tag_notes': dict(self.tag_notes),   # copy: the UI edits in place
            'osc_cue': self.osc_cue,
        }

    @classmethod
    def from_dict(cls, data):
        """Deserialize from JSON storage."""
        marker = cls(_finite(data['time'], "marker time"),
                     name=data.get('name', DEFAULT_MARKER_NAME), color=data.get('color'))
        marker.id = data.get('id', str(uuid.uuid4()))
        marker.osc_cue = _sanitize_osc(data.get('osc_cue'))
        _migrate_tag_notes(marker, data)
        return marker


class HoldCue:
    """
    A caesura: playback parks exactly at ``time`` and waits for GO.

    Attributes:
        id, time, name, tag_notes, osc_cue (fired when the hold is reached)
    """
    def __init__(self, time_pos, name=None):
        self.id = str(uuid.uuid4())
        self.time = float(time_pos)
        self.name = name or DEFAULT_HOLD_NAME
        self.tag_notes: Dict[str, str] = {}
        self.osc_cue = ""

    @property
    def tags(self):
        return list(self.tag_notes.keys())

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'time': self.time,
            'tag_notes': dict(self.tag_notes),   # copy: the UI edits in place
            'osc_cue': self.osc_cue,
        }

    @classmethod
    def from_dict(cls, data):
        hold = cls(_finite(data['time'], "hold time"), name=data.get('name', DEFAULT_HOLD_NAME))
        hold.id = data.get('id', str(uuid.uuid4()))
        hold.osc_cue = _sanitize_osc(data.get('osc_cue'))
        _migrate_tag_notes(hold, data)
        return hold

    def __repr__(self):
        return f"HoldCue({self.name!r} @ {self.time:.3f})"


class SongSettings:
    """
    Per-song playback settings, saved under "settings".

    stems: {name: {"gain_db": float, "muted": bool, "solo": bool}}
    """
    FIELDS = ('tempo_ratio', 'semitones', 'bpm', 'beats_per_bar',
              'downbeat_offset', 'stems', 'routing_mode', 'osc_cue')

    def __init__(self, tempo_ratio=1.0, semitones=0.0, bpm=0.0, beats_per_bar=4,
                 downbeat_offset=0.0, stems=None, routing_mode="stereo", osc_cue=""):
        self.tempo_ratio = float(tempo_ratio)
        self.semitones = float(semitones)
        self.bpm = float(bpm)
        self.beats_per_bar = int(beats_per_bar)
        self.downbeat_offset = float(downbeat_offset)
        self.stems: Dict[str, Dict[str, Any]] = {}
        for name, cfg in (stems or {}).items():
            cfg = cfg or {}
            self.stems[str(name)] = {
                'gain_db': _finite(cfg.get('gain_db', 0.0), "stem gain"),
                'muted': bool(cfg.get('muted', False)),
                'solo': bool(cfg.get('solo', False)),
            }
        self.routing_mode = routing_mode if routing_mode in ROUTING_MODES else "stereo"
        self.osc_cue = _sanitize_osc(osc_cue)

    def to_dict(self) -> dict:
        return {
            'tempo_ratio': self.tempo_ratio,
            'semitones': self.semitones,
            'bpm': self.bpm,
            'beats_per_bar': self.beats_per_bar,
            'downbeat_offset': self.downbeat_offset,
            'stems': {k: dict(v) for k, v in self.stems.items()},
            'routing_mode': self.routing_mode,
            'osc_cue': self.osc_cue,
        }

    @classmethod
    def from_dict(cls, data, problems: Optional[List[str]] = None) -> "SongSettings":
        """
        Build from saved data. Each field is validated on its own: an
        invalid one (null / nan / wrong type) falls back to its default and
        is reported in ``problems`` instead of discarding the whole block.
        """
        if not isinstance(data, dict):
            if data is not None and problems is not None:
                problems.append("settings: not an object")
            return cls()
        kwargs: Dict[str, Any] = {}
        for key in cls.FIELDS:
            if key not in data:
                continue
            value = data[key]
            try:
                if key in ('tempo_ratio', 'semitones', 'bpm', 'downbeat_offset'):
                    value = _finite(value, key)
                    if key == 'tempo_ratio' and value <= 0:
                        raise ValueError("tempo_ratio must be positive")
                    if key == 'bpm':
                        value = max(0.0, value)
                elif key == 'beats_per_bar':
                    value = max(1, int(_finite(value, key)))
                elif key == 'stems':
                    value = {name: cfg for name, cfg in (value or {}).items()}
                cls(**{key: value})   # validates the field in isolation
            except (TypeError, ValueError, AttributeError) as e:
                logger.warning(f"Ignoring invalid song setting {key}={data[key]!r}: {e}")
                if problems is not None:
                    problems.append(f"settings.{key}: {data[key]!r}")
                continue
            kwargs[key] = value
        return cls(**kwargs)

    def copy(self) -> "SongSettings":
        return SongSettings.from_dict(self.to_dict())

    def stem(self, name: str) -> Dict[str, Any]:
        """Settings entry for a stem (created with defaults if missing)."""
        if name not in self.stems:
            self.stems[name] = {'gain_db': 0.0, 'muted': False, 'solo': False}
        return self.stems[name]

    def __eq__(self, other):
        return isinstance(other, SongSettings) and self.to_dict() == other.to_dict()

    def __repr__(self):
        return f"SongSettings({self.to_dict()})"


class StateManager:
    """
    Manages application state and coordinates between UI and audio engine.

    Responsibilities:
    - Owns the AudioEngine instance (or an injected engine), the OSC output
      and the MIDI controller
    - Manages playback state (STOPPED / PLAYING / PAUSED / HELD)
    - Runs the monitor thread (position updates, engine arming, standby)
    - Translates engine events into state transitions and UI events
    - Manages named vamps, markers, holds and skips; per-song settings
    - GO / standby model, GO log, live mode
    - Persists song data to disk

    Event System:
    - Register callbacks with: state.on('event_name', callback_function)
    - Events are emitted automatically when state changes
    - List payloads are COPIES; mutate state through StateManager methods.

    Available Events:
    - 'position_update': (position: float, is_loop_mode: bool)
    - 'state_change': (state: PlaybackState)
    - 'loop_mode_enter': ()
    - 'loop_mode_exit': (exit_position: float)  -- end of the loop that was exited
    - 'loop_cycle': (n: int, total: int | None)
    - 'loop_ready': ()
    - 'song_loaded': (song_name: str, duration: float)
    - 'song_ended': ()
    - 'loop_points_changed': (loop_in: float, loop_out: float)
    - 'loops_changed': (loops_list, selected_index)
    - 'markers_changed': (markers_list)
    - 'holds_changed': (holds_list)
    - 'skips_changed': (skips_list)
    - 'detection_started': ()
    - 'detection_complete': (candidates_list)
    - 'cut_detection_complete': (candidates_list)
    - 'loop_skip_queued': (loop_name)
    - 'loop_skip_cleared': ()  -- skip flag / exit queue cleared, label can reset
    - 'data_load_failed': (message: str)  -- saved data could not be read
    - 'preview_changed': (is_previewing: bool)
    - 'hold_reached': (hold: HoldCue)
    - 'hold_released': ()
    - 'standby_changed': (standby: dict)
    - 'go_fired': (entry: dict)
    - 'go_next_song': (path: str)  -- GO at the end of a song: the UI loads
        ``path`` with load_song(path, from_go=True), then calls play_after_go_load()
    - 'go_next_entry': (entry_id: str)  -- emitted right after 'go_next_song'
        when the target came from the show; a show-aware UI handles THIS one
        (load_entry(entry_id, from_go=True), then play_after_go_load()) and
        ignores 'go_next_song'. Never emitted for next_song_provider targets.
    - 'show_changed': (snapshot: dict)  -- see show_snapshot()
    - 'current_entry_changed': (entry_id: str | None)
    - 'next_up_changed': (entry_id: str | None)  -- the EFFECTIVE next-up
        entry (operator choice, else computed); None = end of show
    - 'show_save_failed': (message: str)
    - 'preload_ready': (path: str)   -- engine pre-decoded/rendered ``path``
    - 'preload_failed': (path: str, message: str)
    - 'live_mode_changed': (enabled: bool)
    - 'settings_changed': (settings: SongSettings)   -- a copy
    - 'render_state': (state: "rendering"|"done"|"failed", message: str)
    - 'stems_changed': (stems: list[StemInfo])
    - 'tempo_estimated': (bpm: float, downbeat_offset: float)
    - 'blocked_in_live_mode': (what: str)
        what is a method name, or "leave_live_confirm" when a MIDI
        toggle_live needs a second press to leave Live Mode
    - 'device_error': (message: str)
    - 'device_lost': (message: str)   -- emitted (once) just before the
        matching 'device_error' when the output device stopped while playing
    - 'device_restored': (name: str)  -- output is running again
    """

    EVENTS = (
        'position_update', 'state_change', 'loop_mode_enter', 'loop_mode_exit',
        'loop_cycle', 'loop_ready', 'song_loaded', 'song_ended',
        'loop_points_changed', 'detection_complete', 'detection_started',
        'loops_changed', 'markers_changed', 'holds_changed', 'skips_changed',
        'cut_detection_complete', 'loop_skip_queued', 'loop_skip_cleared',
        'data_load_failed', 'save_failed', 'save_succeeded', 'preview_changed',
        'hold_reached', 'hold_released', 'standby_changed', 'go_fired',
        'go_next_song', 'live_mode_changed', 'settings_changed', 'render_state', 'stems_changed',
        'tempo_estimated', 'blocked_in_live_mode', 'device_error',
        'device_lost', 'device_restored',
        'go_next_entry', 'show_changed', 'current_entry_changed', 'next_up_changed',
        'show_save_failed', 'preload_ready', 'preload_failed',
    )

    def __init__(self, ffmpeg_path: str = "ffmpeg", data_file: Optional[str] = None,
                 engine=None):
        """
        Initialize the state manager.

        Args:
            ffmpeg_path: Path to ffmpeg executable
            data_file: Override for the loop data JSON path (tests)
            engine: An object implementing the AudioEngine contract; None
                    constructs the real AudioEngine.
        """
        self.ffmpeg_path = ffmpeg_path
        self.data_file = data_file or LOOP_DATA_FILE
        self.data_dir = os.path.dirname(self.data_file) or DATA_DIR
        self.go_log_file = os.path.join(self.data_dir, "go_log.jsonl")

        # Audio engine (lazy import so a broken/absent engine module only
        # matters when the real engine is actually requested)
        if engine is None:
            from .audio_engine import AudioEngine
            engine = AudioEngine(ffmpeg_path=ffmpeg_path)
        self.audio = engine

        # Named vamps (loop regions)
        self.loops: List[LoopRegion] = []
        self.selected_loop_index: int = -1

        # Named markers (cue points)
        self.markers: List[Marker] = []

        # Hold cues (caesuras)
        self.holds: List[HoldCue] = []

        # Skip regions
        self.skips: List[SkipRegion] = []

        # Per-song settings and the engine's stems
        self.settings: SongSettings = SongSettings()
        self.stems: list = []

        # A specific loop ID to skip (set after exiting it, or by "skip vamp")
        self.temp_skip_loop_id: Optional[str] = None

        # Current song
        self.current_song_path: str = ""
        self.current_song_name: str = ""
        self.current_song_id: Optional[str] = None
        self.song_length: float = 0.0
        self.sync_ratio: float = 1.0  # always 1.0; kept for API compatibility

        # Playback state
        self.state = PlaybackState.STOPPED

        # Loop configuration (backward compat - tracks selected loop)
        self.loop_start: float = 0.0
        self.loop_end: float = 0.0

        # Exit queue bookkeeping (the engine performs the exit)
        self.exit_queue_active: bool = False
        self.exit_fade_mode: bool = False  # True = fade out, False = cut to transport
        self.exit_fade_ms: int = FADE_EXIT_DURATION_MS
        self._prev_cycle_pos: float = 0.0   # compat; no longer used for timing

        # Which vamp's points the engine currently holds (armed by the monitor
        # or pushed while stopped). None = engine points belong to no vamp.
        self._armed_loop_id: Optional[str] = None
        # Vamp the engine is currently looping (None in transport / preview)
        self._looping_loop_id: Optional[str] = None

        # Arming cache: what the engine was last told (None = nothing armed)
        self._arm_lock = threading.RLock()
        self._armed_loop_key: Any = None
        self._armed_hold_key: Any = None
        self._armed_hold_id: Optional[str] = None
        self._armed_skips: Any = None

        # Marker crossing detection for OSC cue_reached
        self._last_cue_pos: Optional[float] = None

        # Preview (audition a region without touching the vamp list)
        self._previewing: bool = False
        self._preview_region: Optional[Tuple[float, float]] = None

        # Monitor thread: per-run stop event + generation counter
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_stop_event = threading.Event()
        self._monitor_generation: int = 0

        # Song loading: serialised, with a generation so stale loads drop out
        self._load_lock = threading.RLock()
        self._load_gen_lock = threading.Lock()
        self._load_generation: int = 0

        # Detection workers: generation counter so stale results are dropped
        self._detection_generation: int = 0

        # Debounced persistence
        self._save_lock = threading.RLock()
        self._save_timer: Optional[threading.Timer] = None
        self._save_dirty: bool = False
        self.last_save_error: Optional[str] = None
        self.pending_tempo_pitch: Optional[Tuple[float, float]] = None
        # Tempo/key the song was saved with while its load-time render is
        # still pending: saves keep persisting these, not the engine's 1.0/0.
        self._saved_tempo_pitch: Optional[Tuple[float, float]] = None
        # Set when the data file exists but could not be read (locked, or
        # corrupt and impossible to back up): nothing is written until a
        # later read succeeds, so the file is never overwritten blindly.
        self._data_read_blocked: Optional[str] = None
        # Songs saved during this session (merged back after a blocked read)
        self._session_song_ids: set = set()
        # Songs whose disk entry turned up after edits were made on an empty
        # load: saves go to the entry's SESSION_COPY_KEY until it is reloaded.
        self._conflicted_song_ids: set = set()

        # GO / standby / live mode
        self.live_mode: bool = False
        self.go_log: List[dict] = []
        self._go_lock = threading.Lock()
        self._last_go_time: float = -1e9
        self._last_standby: Optional[dict] = None
        self._standby_lock = threading.Lock()
        self._live_exit_armed_at: float = -1e9   # MIDI two-press confirmation

        # Late exit after a vamp ended (its count ran out, or a cut / fade
        # exit completed) -- monotonic time; see LATE_EXIT_GRACE_S
        self._count_exit_time: float = -1e9

        # True after the song played to its end or a fade exit stopped it:
        # GO advances to the next song (never restarts this one from 0:00);
        # Play replays it.
        self._ended_at_rest: bool = False

        # () -> path of the song after the current one in the running order,
        # or None at the end of the list. Set by the UI (library order).
        self.next_song_provider: Optional[Callable[[], Optional[str]]] = None

        # Show (running order). Always a Show; empty = fall back to
        # next_song_provider. See the SHOW section below.
        self._show_lock = threading.RLock()
        self.show: Show = Show()
        self.current_entry_id: Optional[str] = None
        self.next_up_override: Optional[str] = None   # operator's explicit choice
        self._show_anchor_index: Optional[int] = None  # where a removed current entry was
        self._pending_go_entry_id: Optional[str] = None
        self._last_next_up: Any = object()             # dedupe 'next_up_changed'
        self._show_save_timer: Optional[threading.Timer] = None
        # Preload worker (one request at a time; stale requests drop out)
        self._preload_lock = threading.Lock()
        self._preload_generation: int = 0
        self._preload_requested: Any = None
        self._preload_thread: Optional[threading.Thread] = None

        # Device-loss watchdog (see _watchdog_tick)
        self._clock = time.monotonic             # tests substitute a fake clock
        self._device_lock = threading.Lock()
        self.device_lost: bool = False
        self._device_loss_pending: Optional[str] = None
        self._device_retry_at: float = 0.0
        self._device_change_active: bool = False  # set_routing / set_output_device running
        self._wd_frames: Optional[int] = None
        self._wd_changed_at: float = 0.0

        # Last persistence/load problem (None if fine); also emitted as
        # 'data_load_failed'. Set during __init__ before callbacks exist, so
        # the UI should inspect it after construction.
        self.last_load_error: Optional[str] = None

        # Callbacks for UI updates (event-driven architecture)
        self._callbacks: Dict[str, List[Callable]] = {name: [] for name in self.EVENTS}

        # Main/backup mirroring (backend/mirror.py). The link is idle until
        # mirror.start(); as main it sets _mirror_sink to receive discrete
        # actions (see _mirror_emit). _mirror_quiet() suppresses emits for
        # internal calls and for actions a backup applies from its main.
        self._mirror_sink: Optional[Callable[[str, dict], None]] = None
        self._mirror_tls = threading.local()
        from .mirror import MirrorLink
        self.mirror = MirrorLink(self)

        # Control surfaces
        self.osc = OscOutput()
        self.osc.load_settings()
        self.midi = MidiController(on_action=self._on_control_action)
        self.midi.load_from_preferences()

        # Engine events
        try:
            self.audio.on_event(self._on_engine_event)
        except Exception as e:
            logger.error(f"Engine does not support on_event: {e}")

        # Load saved data
        self.loop_data: Dict[str, Any] = self._load_loop_data()

        logger.info("StateManager initialized")

    # =========================================================================
    # EVENT SYSTEM
    # =========================================================================

    def on(self, event: str, callback: Callable) -> None:
        """Register a callback for an event."""
        if event in self._callbacks:
            self._callbacks[event].append(callback)
        else:
            logger.warning(f"Unknown event: {event}. Available: {list(self._callbacks.keys())}")

    def off(self, event: str, callback: Callable) -> None:
        """Unregister a callback for an event."""
        if event in self._callbacks and callback in self._callbacks[event]:
            self._callbacks[event].remove(callback)

    def _emit(self, event: str, *args) -> None:
        """Emit an event to all registered callbacks."""
        for callback in list(self._callbacks.get(event, [])):
            try:
                callback(*args)
            except Exception as e:
                logger.error(f"Error in callback for {event}: {e}")

    def _emit_loops_update(self) -> None:
        """Notify UI about loop changes (loops_changed + loop_points_changed, once each)."""
        self._emit('loops_changed', list(self.loops), self.selected_loop_index)
        loop = self._selected_loop()
        if loop is not None:
            self._emit('loop_points_changed', loop.start, loop.end)
        else:
            self._emit('loop_points_changed', self.loop_start, self.loop_end)
        self._refresh_standby()

    def _emit_markers_update(self) -> None:
        self._emit('markers_changed', list(self.markers))
        self._refresh_standby()

    def _emit_holds_update(self) -> None:
        self._emit('holds_changed', list(self.holds))
        self._refresh_standby()

    def _emit_skips_update(self) -> None:
        self._emit('skips_changed', list(self.skips))

    def _emit_settings_update(self) -> None:
        self._emit('settings_changed', self.settings.copy())

    def _set_state(self, state: PlaybackState) -> None:
        self.state = state
        self._emit('state_change', state)
        self._refresh_standby()

    # =========================================================================
    # MIRRORING HOOKS (backend/mirror.py; internal -- never call from the UI)
    # =========================================================================

    def _mirror_emit(self, kind: str, **data) -> None:
        """Hand a discrete action to the mirror main (non-blocking), unless quiet."""
        sink = self._mirror_sink
        if sink is None or getattr(self._mirror_tls, 'quiet', 0):
            return
        try:
            sink(kind, data)
        except Exception:
            logger.exception(f"Mirror hook failed for {kind}")

    @contextmanager
    def _mirror_quiet(self):
        """Calls inside this block (this thread) are not sent to mirror backups."""
        tls = self._mirror_tls
        tls.quiet = getattr(tls, 'quiet', 0) + 1
        try:
            yield
        finally:
            tls.quiet -= 1

    def _mirror_load(self, entry_id: Optional[str], path: str) -> bool:
        """Backup: load what the main loaded (the GO path, so Live Mode allows it)."""
        with self._mirror_quiet():
            if entry_id:
                return self.load_entry(entry_id, from_go=True)
            return self.load_song(path, from_go=True)

    def _mirror_set_tempo_pitch(self, tempo_ratio: float, semitones: float) -> bool:
        """Backup: apply the main's tempo/key change (bypasses the Live Mode lock)."""
        with self._mirror_quiet():
            return self._set_tempo_pitch(tempo_ratio, semitones)

    # =========================================================================
    # LIVE MODE
    # =========================================================================

    def set_live_mode(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.live_mode:
            return
        self.live_mode = enabled
        logger.info(f"Live mode {'ON' if enabled else 'OFF'}")
        self._mirror_emit("live_mode", enabled=enabled)
        self._emit('live_mode_changed', enabled)
        self._refresh_standby()

    def toggle_live_from_control(self) -> bool:
        """
        toggle_live from a control surface (MIDI): entering Live Mode takes
        one press; leaving needs a second press within LIVE_EXIT_CONFIRM_S
        (the first emits 'blocked_in_live_mode'("leave_live_confirm")).
        Returns the new live state.
        """
        if not self.live_mode:
            self.set_live_mode(True)
            return True
        now = self._clock()
        if now - self._live_exit_armed_at <= LIVE_EXIT_CONFIRM_S:
            self._live_exit_armed_at = -1e9
            self.set_live_mode(False)
            return False
        self._live_exit_armed_at = now
        logger.info("Live mode: press toggle_live again to leave")
        self._emit('blocked_in_live_mode', "leave_live_confirm")
        return True

    def _blocked(self, what: str) -> bool:
        """True (and emits 'blocked_in_live_mode') when live mode forbids ``what``."""
        if self.live_mode:
            logger.info(f"Blocked in live mode: {what}")
            self._emit('blocked_in_live_mode', what)
            return True
        return False

    # =========================================================================
    # SONG LOADING
    # =========================================================================

    @staticmethod
    def discover_stems(path: str) -> Dict[str, str]:
        """
        Find stem files for ``path``:
          <song>.stems/<name>.wav|flac|mp3   or   "<song stem> - <name>.<ext>" beside it.
        A stem whose name contains "click" is returned under the key "click".
        """
        found: Dict[str, str] = {}
        base, _ = os.path.splitext(path)
        folder = os.path.dirname(path)
        song_stem = os.path.basename(base)

        def _add(name: str, file_path: str) -> None:
            name = name.strip()
            if not name:
                return
            key = "click" if "click" in name.lower() else name
            if key == "mix":
                key = f"{name}_stem"
            if key not in found:
                found[key] = file_path

        stems_dir = base + ".stems"
        if os.path.isdir(stems_dir):
            for entry in sorted(os.listdir(stems_dir)):
                name, ext = os.path.splitext(entry)
                if ext.lower() in STEM_EXTENSIONS:
                    _add(name, os.path.join(stems_dir, entry))

        prefix = song_stem + " - "
        if os.path.isdir(folder):
            for entry in sorted(os.listdir(folder)):
                name, ext = os.path.splitext(entry)
                if ext.lower() in STEM_EXTENSIONS and name.startswith(prefix) and len(name) > len(prefix):
                    _add(name[len(prefix):], os.path.join(folder, entry))
        return found

    def load_song(self, path: str, from_go: bool = False) -> bool:
        """
        Load a song and its associated loop data.

        Safe to call from several threads: loads are serialised and a load
        that was superseded by a newer call drops its events and returns False.
        Returns False (and emits 'blocked_in_live_mode') in live mode, except
        for the GO-driven advance to the next song (``from_go``).
        """
        if not from_go and self._blocked("load_song"):
            return False
        logger.info(f"=== UI: Loading song: {os.path.basename(path)} ===")

        # Claim a generation first so a load requested while an earlier one
        # is still decoding makes the earlier one stale.
        with self._load_gen_lock:
            self._load_generation += 1
            generation = self._load_generation

        with self._load_lock:
            if generation != self._load_generation:
                logger.info("Load superseded before it started; dropping")
                return False

            self.flush_save()
            with self._mirror_quiet():   # the mirrored "load" stops the backup
                self.stop()
            self._detection_generation += 1  # cancel in-flight detection

            try:
                stem_paths = self.discover_stems(path)
            except Exception as e:
                logger.warning(f"Stem discovery failed: {e}")
                stem_paths = {}

            song_id = self._get_file_fingerprint(path)
            tempo_pitch = self._saved_tempo_pitch_for(song_id)
            try:
                song_length, sync_ratio = self._engine_load_file(path, stem_paths or None, tempo_pitch)
            except Exception as e:
                logger.error(f"Error loading song: {e}")
                # The engine has already released the previous song, so
                # reset to the no-song state and tell the UI to clear.
                self._reset_to_no_song()
                return False

            if generation != self._load_generation:
                logger.info("Load superseded by a newer request; dropping")
                return False

            self.song_length = song_length
            self.sync_ratio = sync_ratio
            self.current_song_path = path
            self.current_song_name = os.path.basename(path)

            # UNIQUE ID (content fingerprint, computed before the load)
            self.current_song_id = song_id
            if self.current_song_id is None:
                msg = (f"Could not fingerprint '{self.current_song_name}'; "
                       "vamps and markers for this song cannot be saved")
                logger.error(msg)
                self.last_load_error = msg
                self._emit('data_load_failed', msg)
            else:
                logger.info(f"Song ID: {self.current_song_id}")

            # Clear previous state (new lists, never in-place)
            self._clear_song_lists()
            self.loop_end = self.song_length

            if self._data_read_blocked:
                self._retry_blocked_read()   # the file may be readable now
            if self.current_song_id:
                # Reloading shows the disk entry again: saves may replace it
                self._conflicted_song_ids.discard(self.current_song_id)
                if self.current_song_id not in self.loop_data:
                    self._adopt_moved_entry(path, self.current_song_id)

            saved = self.loop_data.get(self.current_song_id) if self.current_song_id else None
            if saved is not None:
                logger.info("Found saved data for this audio file.")
                try:
                    problems = self._load_song_data(saved)
                except Exception as e:
                    msg = (f"Saved data for '{self.current_song_name}' is unreadable ({e}); "
                           "starting empty (the original entry is kept in the data file)")
                    logger.error(msg)
                    self._clear_song_lists()
                    self.loop_end = self.song_length
                    self._keep_unreadable(self.current_song_id, {'entry': [saved]}, replace_entry=True)
                    self.last_load_error = msg
                    self._emit('data_load_failed', msg)
                else:
                    if problems:
                        msg = (f"{len(problems)} saved item(s) for '{self.current_song_name}' could not "
                               f"be read and were skipped ({'; '.join(problems[:3])}"
                               f"{'; ...' if len(problems) > 3 else ''}). "
                               "The originals are kept in the data file.")
                        logger.error(msg)
                        self.last_load_error = msg
                        self._emit('data_load_failed', msg)
            else:
                logger.info("No saved data found for this specific audio file.")

            self._apply_settings_to_engine()
            self.stems = self._engine_stems()

            selected = self._selected_loop()
            if selected is not None:
                self._push_loop_to_engine(selected)

            # Emit updates to UI
            self._emit('song_loaded', self.current_song_name, self.song_length)
            self._emit_loops_update()
            self._emit_markers_update()
            self._emit_holds_update()
            self._emit_skips_update()
            self._emit_settings_update()
            self._emit('stems_changed', list(self.stems))
            self.osc.send_event("song_loaded", name=self.current_song_name,
                                duration=self.song_length)
            self._after_song_loaded(path)
            return True

    def _engine_load_file(self, path: str, stem_paths, tempo_pitch: Tuple[float, float]):
        """
        audio.load_file(), passing the song's saved ``tempo_pitch`` when the
        engine supports it (a matching preload is then installed already
        rendered). Older engines get the two-argument call.
        """
        load = self.audio.load_file
        try:
            params = inspect.signature(load).parameters
            supported = 'tempo_pitch' in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        except (TypeError, ValueError):
            supported = False
        if supported:
            return load(path, stem_paths, tempo_pitch=tempo_pitch)
        return load(path, stem_paths)

    def _saved_tempo_pitch_for(self, song_id: Optional[str]) -> Tuple[float, float]:
        """The (tempo_ratio, semitones) saved in loop_data for a fingerprint ((1.0, 0.0) if none)."""
        entry = self.loop_data.get(song_id) if song_id else None
        settings = entry.get('settings') if isinstance(entry, dict) else None
        if not isinstance(settings, dict):
            return (1.0, 0.0)
        try:
            ratio = _finite(settings.get('tempo_ratio', 1.0), "tempo_ratio")
            semis = _finite(settings.get('semitones', 0.0), "semitones")
        except (TypeError, ValueError):
            return (1.0, 0.0)
        if ratio <= 0:
            return (1.0, 0.0)
        return (max(0.5, min(2.0, ratio)), max(-12.0, min(12.0, semis)))

    def _clear_song_lists(self) -> None:
        self.loops = []
        self.markers = []
        self.holds = []
        self.skips = []
        self.settings = SongSettings()
        self.pending_tempo_pitch = None
        self._saved_tempo_pitch = None
        self._ended_at_rest = False
        self.selected_loop_index = -1
        self.temp_skip_loop_id = None
        self._armed_loop_id = None
        self._looping_loop_id = None
        self._last_cue_pos = None
        self.loop_start = 0.0
        self.loop_end = 0.0
        self._invalidate_arm()

    def _reset_to_no_song(self) -> None:
        """Clear song metadata and lists after a failed load, and notify the UI."""
        self.current_song_path = ""
        self.current_song_name = ""
        self.current_song_id = None
        self.song_length = 0.0
        self.sync_ratio = 1.0
        self._clear_song_lists()
        self.stems = []
        self._emit('song_loaded', "", 0.0)
        self._emit_loops_update()
        self._emit_markers_update()
        self._emit_holds_update()
        self._emit_skips_update()
        self._emit_settings_update()
        self._emit('stems_changed', [])

    def _load_song_data(self, saved: dict) -> List[str]:
        """
        Load saved song data (loops, markers, holds, skips, settings).
        Handles both old format (just start/end) and new format (loops + markers).

        Every item is parsed on its own: a malformed vamp / marker / hold /
        skip (missing time, null or nan values) is skipped and kept verbatim
        under UNREADABLE_KEY so the next save does not lose it; an invalid
        setting falls back to its default. Returns the problems found (empty
        when clean). Raises only when the entry as a whole is unusable.
        """
        if not isinstance(saved, dict):
            raise ValueError("song entry is not an object")

        problems: List[str] = []
        unreadable: Dict[str, list] = {}

        def _parse(kind: str, factory) -> list:
            raw_items = saved.get(kind)
            if raw_items is None:
                return []
            if not isinstance(raw_items, list):
                problems.append(f"{kind}: not a list")
                unreadable.setdefault(kind, []).append(raw_items)
                return []
            items = []
            for index, raw in enumerate(raw_items):
                try:
                    if not isinstance(raw, dict):
                        raise ValueError("not an object")
                    items.append(factory(raw))
                except Exception as e:
                    logger.warning(f"Skipping unreadable saved {kind}[{index}] {raw!r}: {e}")
                    problems.append(f"{kind}[{index}]: {e}")
                    unreadable.setdefault(kind, []).append(raw)
            return items

        if 'loops' in saved:
            loops = _parse('loops', LoopRegion.from_dict)
            markers = sorted(_parse('markers', Marker.from_dict), key=lambda m: m.time)
            holds = sorted(_parse('holds', HoldCue.from_dict), key=lambda h: h.time)
            skips = sorted(_parse('skips', SkipRegion.from_dict), key=lambda x: x.start)
            settings = SongSettings.from_dict(saved.get('settings'), problems)
            self.loops = loops
            self.markers = markers
            self.holds = holds
            self.skips = skips
            self.settings = settings
            if self.loops:
                self.selected_loop_index = 0
                self.loop_start = self.loops[0].start
                self.loop_end = self.loops[0].end
            logger.info(f"Loaded {len(self.loops)} vamps, {len(self.markers)} markers, "
                        f"{len(self.holds)} holds, {len(self.skips)} skips")

        elif 'start' in saved and 'end' in saved:
            start = _finite(saved['start'], "start")
            end = _finite(saved['end'], "end")
            if end <= start:
                raise ValueError(f"legacy loop end {end} is not after start {start}")
            logger.info(f"Loaded legacy loop points: IN={start:.3f}s OUT={end:.3f}s")
            self.loops = [LoopRegion(start, end, name="Vamp 1")]
            self.selected_loop_index = 0
            self.loop_start = start
            self.loop_end = end

        if unreadable and self.current_song_id:
            self._keep_unreadable(self.current_song_id, unreadable)
        return problems

    def _keep_unreadable(self, song_id: str, items: Dict[str, list],
                         replace_entry: bool = False) -> None:
        """
        Record raw items that failed to load under the song entry's
        UNREADABLE_KEY (save_loop carries the key forward), so a later save
        never silently discards them. ``replace_entry``: the entry itself
        was unusable; keep only the preserved copy.
        """
        with self._save_lock:
            entry = self.loop_data.get(song_id)
            if replace_entry or not isinstance(entry, dict):
                old = entry if isinstance(entry, dict) else {}
                entry = {k: old[k] for k in PRESERVED_ENTRY_KEYS if k in old}
            else:
                entry = dict(entry)
            kept = entry.get(UNREADABLE_KEY)
            kept = {k: list(v) for k, v in kept.items() if isinstance(v, list)} \
                if isinstance(kept, dict) else {}
            for kind, raws in items.items():
                bucket = kept.setdefault(kind, [])
                for raw in raws:
                    if raw not in bucket:
                        bucket.append(raw)
            entry[UNREADABLE_KEY] = kept
            self.loop_data[song_id] = entry

    def _engine_stems(self) -> list:
        try:
            return list(self.audio.get_stems())
        except Exception as e:
            logger.warning(f"get_stems failed: {e}")
            return []

    def _apply_settings_to_engine(self) -> None:
        """Push the current SongSettings (tempo/key, stems, routing) to the engine."""
        s = self.settings
        audio = self.audio
        try:
            current = tuple(audio.get_tempo_pitch()) \
                if (s.tempo_ratio != 1.0 or s.semitones != 0.0) else None
            if current is not None and abs(current[0] - s.tempo_ratio) < 1e-6 \
                    and abs(current[1] - s.semitones) < 1e-6:
                # The engine installed a preload rendered at the saved
                # tempo/key: no load-time render needed.
                logger.info("Saved tempo/key already rendered (preload); no render")
            elif current is not None:
                requested = (s.tempo_ratio, s.semitones)
                s.tempo_ratio, s.semitones = current
                self.pending_tempo_pitch = requested
                # Until the render finishes, saves keep the song's saved values
                self._saved_tempo_pitch = requested
                audio.set_tempo_pitch(*requested)
        except Exception as e:
            logger.error(f"set_tempo_pitch failed: {e}")
            self._ev_render_failed({'message': str(e)})
        names = {st.name for st in self._engine_stems()}
        for name, cfg in s.stems.items():
            if name not in names:
                continue
            try:
                audio.set_stem_gain(name, cfg.get('gain_db', 0.0))
                audio.set_stem_mute(name, cfg.get('muted', False))
                audio.set_stem_solo(name, cfg.get('solo', False))
            except Exception as e:
                logger.error(f"stem settings for '{name}' failed: {e}")
        try:
            audio.set_routing(s.routing_mode)
        except Exception as e:
            logger.error(f"set_routing({s.routing_mode}) failed: {e}")

    def get_raw_audio_for_waveform(self):
        """Get raw audio data for waveform generation."""
        return self.audio.get_raw_audio_data()

    # =========================================================================
    # PLAYBACK CONTROLS
    # =========================================================================

    def play(self) -> None:
        """Start or resume playback (releases a hold when HELD)."""
        if not self.current_song_path:
            logger.warning("No song loaded")
            return

        logger.info(f"UI: Toggle play -> PLAY (was {self.state.name.lower()})")

        if self.device_lost and not self._try_restore_device():
            # Still no interface: stay in the alert state instead of
            # "playing" into a dead stream (the monitor keeps retrying).
            logger.warning("Play ignored: audio device still lost")
            return

        self._ended_at_rest = False
        if self.state == PlaybackState.HELD:
            self._release_hold()
            return
        if self.state == PlaybackState.PAUSED and self.audio.is_playing:
            self.audio.unpause_transport()
            if getattr(self.audio, 'is_held', False):
                # The engine reached a hold just as we paused (its
                # hold_reached was ignored while PAUSED): resume onto the
                # hold, so the next GO releases it.
                self._enter_held()
                self._mirror_emit("play", position=self.audio.get_position())
                return
        elif self.state == PlaybackState.PAUSED:
            # Paused by a seek while stopped: the engine is idle, start it here
            pos = self.audio.get_position()
            self._last_cue_pos = pos
            self.audio.play_transport(pos)
        else:
            self._last_cue_pos = 0.0
            self.audio.play_transport(0.0)

        self.state = PlaybackState.PLAYING
        self._invalidate_arm()
        self._arm_engine(self.audio.get_position())
        self._start_monitor()
        self._mirror_emit("play", position=self.audio.get_position())
        self._emit('state_change', self.state)
        self._refresh_standby()

    def play_from(self, position: float) -> bool:
        """
        Start playback from a specific timestamp (a user jump, e.g. to a
        vamp's start). Returns False (and emits 'blocked_in_live_mode') in
        live mode.
        """
        if self._blocked("play_from"):
            return False
        self._play_from(position)
        return True

    def _play_from(self, position: float) -> None:
        """
        Start playback from a specific timestamp (no Live Mode check).
        Used for previewing loop candidates and jumping to markers.
        """
        with self._mirror_quiet():
            self.stop()
        self._ended_at_rest = False
        self._last_cue_pos = position
        self.audio.play_transport(start_pos=position)
        self.state = PlaybackState.PLAYING
        self._invalidate_arm()
        self._arm_engine(position)
        self._start_monitor()
        self._mirror_emit("play_from", position=float(position))
        self._emit('state_change', self.state)
        self._refresh_standby()

    def pause(self) -> None:
        """Pause playback."""
        if self.state == PlaybackState.PLAYING:
            logger.info("UI: PAUSE")
            # Flip state first so a monitor tick between the two calls does
            # not see PLAYING + inactive transport and declare the song ended.
            self.state = PlaybackState.PAUSED
            self.audio.pause_transport()
            self._mirror_emit("pause", position=self.audio.get_position())
            self._emit('state_change', self.state)
            self._refresh_standby()

    def toggle_play_pause(self) -> None:
        """Toggle between play and pause (releases a hold when HELD)."""
        if self.state == PlaybackState.PLAYING:
            self.pause()
        else:
            self.play()

    def stop(self) -> None:
        """Stop playback (also ends a preview and flushes pending saves)."""
        logger.info("UI: STOP")
        self._monitor_stop_event.set()
        self._wd_frames = None
        self._reset_transient_flags()
        self.audio.stop()
        if self._previewing:
            self._end_preview(restore=True)
        self.state = PlaybackState.STOPPED
        self._invalidate_arm()
        self.flush_save()
        self._mirror_emit("stop")
        self._emit('state_change', self.state)
        self._refresh_standby()

    def _reset_transient_flags(self) -> None:
        self.exit_queue_active = False
        self.exit_fade_mode = False
        self.temp_skip_loop_id = None
        self._prev_cycle_pos = 0.0
        self._looping_loop_id = None

    def seek(self, position: float) -> None:
        """
        Seek to a position.

        A backwards seek (or a seek far past a skipped vamp) re-arms the vamp
        that was being skipped; a pending exit queue is always cancelled.
        Seeking while HELD leaves the hold and continues playing.

        Args:
            position: Time in seconds
        """
        position = max(0.0, min(position, self.song_length - 0.01))
        previous_pos = self.audio.get_position()
        cleared = False
        self._ended_at_rest = False

        if self.temp_skip_loop_id:
            skipped = self._find_loop(self.temp_skip_loop_id)
            if (skipped is None or position < previous_pos
                    or position > skipped.end + SKIP_FLAG_RELEASE_S):
                self.temp_skip_loop_id = None
                cleared = True
                logger.debug("Seek cleared skip-loop flag")

        if self.exit_queue_active or self.exit_fade_mode:
            self.exit_queue_active = False
            self.exit_fade_mode = False
            cleared = True
            logger.debug("Seek cancelled queued exit")
        self._prev_cycle_pos = 0.0

        was_held = self.state == PlaybackState.HELD
        self._last_cue_pos = position
        # The engine exits loop/hold itself and reports 'loop_exited' (seek)
        self.audio.seek_transport(position)
        self._looping_loop_id = None
        self._mirror_emit("seek", position=float(position))

        if cleared:
            self._emit('loop_skip_cleared')

        if self.state == PlaybackState.STOPPED:
            self.state = PlaybackState.PAUSED
            self._emit('state_change', self.state)
        elif was_held:
            self.state = PlaybackState.PLAYING
            self._emit('state_change', self.state)

        self._invalidate_arm()
        self._arm_engine(position)

        if self.state != PlaybackState.PLAYING:
            # The monitor only emits positions while playing; keep the
            # clock, playhead and cue displays in step with a paused seek.
            self._emit('position_update', position, False)
        self._refresh_standby()

    def nudge(self, amount: float) -> bool:
        """Nudge position by a small amount (seconds). Blocked in live mode."""
        if self._blocked("nudge"):
            return False
        pos = self.audio.get_position()
        self.seek(pos + amount)
        return True

    def user_seek(self, position: float) -> bool:
        """A free seek from the UI (waveform click). Blocked in live mode."""
        if self._blocked("seek"):
            return False
        self.seek(position)
        return True

    def seek_to_start(self) -> bool:
        """Home: seek to the top of the song. Blocked in live mode."""
        if self._blocked("seek_start"):
            return False
        self.seek(0.0)
        return True

    def preview_from(self, position: float) -> bool:
        """Audition from ``position`` (detector cut preview). Blocked in live mode."""
        if self._blocked("preview"):
            return False
        self._play_from(position)
        return True

    # =========================================================================
    # GO / STANDBY
    # =========================================================================

    def _trigger_time(self, kind: str, obj) -> float:
        """The time at which a cue 'happens' (vamp = its end, where looping begins)."""
        return obj.end if kind == 'vamp' else obj.time

    def _next_item(self, pos: float):
        """(obj, type) of the nearest cue strictly ahead of ``pos``, or (None, None)."""
        best = None
        for t, kind, obj in self.get_timeline_items():
            if kind == 'vamp' and (not obj.active or obj.id == self.temp_skip_loop_id):
                continue
            when = self._trigger_time(kind, obj)
            if when <= pos + CUE_EPSILON_S or self._in_active_skip(when):
                continue
            if best is None or when < best[0]:
                best = (when, obj, kind)
        if best is None:
            return None, None
        return best[1], best[2]

    def _in_active_skip(self, when: float) -> bool:
        """True if playback jumps over ``when`` (it lies inside an active skip region)."""
        for skip in self.skips:
            if skip.active and skip.start <= when < skip.end:
                return True
        return False

    def _current_hold(self) -> Optional[HoldCue]:
        hold = self._find_hold(self._armed_hold_id)
        if hold is not None:
            return hold
        pos = self.audio.get_position()
        for h in self.holds:
            if abs(h.time - pos) <= 0.05:
                return h
        return None

    def _current_loop(self) -> Optional[LoopRegion]:
        return self._find_loop(self._looping_loop_id) or self._find_loop(self._armed_loop_id)

    def standby(self) -> dict:
        """
        What GO would do right now (Stage|Tracks "Advance" semantics):
        {"action": "play"|"release_hold"|"exit_vamp"|"none", "label": str,
         "next_item": obj|None, "next_type": str|None}
        """
        if not self.current_song_path:
            return {"action": "none", "label": "No song loaded",
                    "next_item": None, "next_type": None}

        state = self.state
        pos = self.audio.get_position()
        next_item, next_type = self._next_item(pos)

        if self.device_lost:
            return {"action": "none", "label": DEVICE_LOST_LABEL,
                    "next_item": next_item, "next_type": next_type}

        if state == PlaybackState.HELD:
            hold = self._current_hold()
            name = hold.name if hold else "hold"
            return {"action": "release_hold", "label": f"GO: release {name}",
                    "next_item": hold, "next_type": "hold"}

        # PAUSED resolves to "resume" before anything else: GO while paused
        # inside a vamp must restart the audio, not queue an exit.
        if state == PlaybackState.PAUSED:
            return {"action": "play", "label": "GO: resume",
                    "next_item": next_item, "next_type": next_type}

        if self.audio.mode == "loop":
            if self._previewing:
                return {"action": "exit_vamp", "label": "GO: exit preview",
                        "next_item": None, "next_type": None}
            loop = self._current_loop()
            name = loop.name if loop else "vamp"
            if self.exit_queue_active:
                return {"action": "none", "label": f"Exiting {name}...",
                        "next_item": loop, "next_type": "vamp"}
            return {"action": "exit_vamp", "label": f"GO: exit {name}",
                    "next_item": loop, "next_type": "vamp"}

        if state == PlaybackState.STOPPED:
            if self._ended_at_rest:
                # Song ended / faded out: GO moves on to the next song (it
                # never restarts this one from 0:00); Play replays it.
                target = self._next_song_target()
                if target is not None:
                    return {"action": "next_song", "label": f"GO: next song - {target[2]}",
                            "next_item": None, "next_type": None}
                end = "End of show" if self._show_active() else "End of song list"
                return {"action": "none", "label": f"{end} - Play replays this song",
                        "next_item": None, "next_type": None}
            return {"action": "play", "label": "GO: play",
                    "next_item": next_item, "next_type": next_type}

        # PLAYING in transport
        if next_item is not None:
            label = f"Next: {next_item.name}"
        else:
            label = "Playing"
        return {"action": "none", "label": label,
                "next_item": next_item, "next_type": next_type}

    def _next_song_target(self) -> Optional[Tuple[str, Optional[str], str]]:
        """
        Where GO at the end of a song goes: (path, entry_id, title), or None.
        With a non-empty show it is the next-up entry (entry_id set); with an
        empty show it is next_song_provider's path (entry_id None).
        """
        if self._show_active():
            entry = self.show.get(self.next_up_entry_id)
            if entry is None:
                return None
            return entry.path, entry.id, entry.title
        path = self._next_song_path()
        if not path:
            return None
        return path, None, os.path.splitext(os.path.basename(path))[0]

    def _next_song_path(self) -> Optional[str]:
        """next_song_provider's song after this one (None = last song / no provider)."""
        provider = self.next_song_provider
        if provider is None:
            return None
        try:
            return provider() or None
        except Exception as e:
            logger.error(f"next_song_provider failed: {e}")
            return None

    def play_after_go_load(self) -> None:
        """Start the song that a 'go_next_song' GO just loaded (sends its OSC cue)."""
        self.play()
        if self.settings.osc_cue:
            self.osc.send_cue(self.settings.osc_cue)

    def refresh_standby(self) -> None:
        """Recompute GO's standby (e.g. after the song running order changed)."""
        self._refresh_standby()

    def _refresh_standby(self) -> None:
        """Recompute standby(); emit 'standby_changed' when it differs from last time."""
        try:
            current = self.standby()
        except Exception as e:
            logger.error(f"standby() failed: {e}")
            return
        with self._standby_lock:
            if current == self._last_standby:
                return
            self._last_standby = current
        self._emit('standby_changed', dict(current))

    def go(self, source: str = "ui") -> bool:
        """
        Perform the standby action. Returns False if nothing was done
        (no action, or a second GO within GO_DEBOUNCE_MS). ``source`` is
        recorded in the GO log ("ui", "midi", "web", "segue", ...).
        """
        next_entry_id = None
        now = time.monotonic()
        with self._go_lock:
            if (now - self._last_go_time) * 1000.0 < GO_DEBOUNCE_MS:
                logger.info(f"GO ({source}) ignored: debounce")
                return False
            self._last_go_time = now

        sb = self.standby()
        action = sb["action"]
        position = self.audio.get_position()
        if action == "none":
            logger.info(f"GO ({source}): nothing to do ({sb['label']})")
            return False

        logger.info(f"GO ({source}): {action} at {position:.3f}s")
        if action == "release_hold":
            self._release_hold()
        elif action == "exit_vamp":
            self.queue_exit(fade_mode=False)
        elif action == "play":
            self.play()
            if self.settings.osc_cue:
                self.osc.send_cue(self.settings.osc_cue)
        elif action == "next_song":
            # The UI owns loading (overlay, waveform); it calls
            # load_song(path, from_go=True) / load_entry(id, from_go=True)
            # then play_after_go_load().
            target = self._next_song_target()
            if target is None:
                return False
            next_entry_id = target[1]
            self._request_next_song(target)

        entry = {"ts": time.time(), "source": source, "action": action,
                 "position": position, "label": sb["label"]}
        if action == "next_song" and next_entry_id:
            entry["entry_id"] = next_entry_id
        self.go_log.append(entry)
        self._append_go_log(entry)
        self._mirror_emit("go", action=action, position=position, entry_id=next_entry_id)
        self._emit('go_fired', dict(entry))
        self.osc.send_event("go", action=action, position=position,
                            source=source, label=sb["label"])
        self._refresh_standby()
        return True

    def _append_go_log(self, entry: dict) -> None:
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(self.go_log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.error(f"Could not append GO log: {e}")

    def _release_hold(self) -> None:
        """Resume from a hold (engine resumes from the held sample)."""
        if self.state != PlaybackState.HELD:
            return
        self._mirror_emit("release_hold")
        self.audio.release_hold()
        if getattr(self.audio, 'is_paused', False):
            # Paused while parked (or the pause raced the hold): resume the
            # engine too, or we would report PLAYING over silence.
            self.audio.unpause_transport()
        if self.state == PlaybackState.HELD:   # engine event may be async
            self._set_state(PlaybackState.PLAYING)

    # =========================================================================
    # LOOP CONTROLS (all edits route through update_selected_loop)
    # =========================================================================

    def _selected_loop(self) -> Optional[LoopRegion]:
        if 0 <= self.selected_loop_index < len(self.loops):
            return self.loops[self.selected_loop_index]
        return None

    def _find_loop(self, loop_id: Optional[str]) -> Optional[LoopRegion]:
        if loop_id is None:
            return None
        for loop in self.loops:
            if loop.id == loop_id:
                return loop
        return None

    def _find_hold(self, hold_id: Optional[str]) -> Optional[HoldCue]:
        if hold_id is None:
            return None
        for hold in self.holds:
            if hold.id == hold_id:
                return hold
        return None

    def _push_loop_to_engine(self, loop: LoopRegion) -> bool:
        """
        Push a vamp's points to the engine if the UI is allowed to (rule: only
        when stopped, or when it is the vamp the monitor armed / is looping).
        Returns True if pushed. While playing, the monitor re-arms the engine
        on its next tick when the armed vamp's fields changed.
        """
        if self._previewing:
            return False
        if self.state == PlaybackState.STOPPED or loop.id == self._armed_loop_id:
            self.audio.set_loop_points(loop.start, loop.end, crossfade_ms=loop.crossfade_ms)
            self._armed_loop_id = loop.id
            return True
        logger.debug(f"Not pushing loop '{loop.name}' to engine while playing (monitor owns it)")
        return False

    def _clamp_time(self, value: float) -> float:
        value = _finite(value, "time")   # nan / inf would poison sorting and the engine
        if self.song_length > 0:
            return max(0.0, min(value, self.song_length))
        return max(0.0, value)

    def update_selected_loop(self, start=None, end=None, crossfade_ms=None,
                             early_switch_ms=None, exit_fade_ms=None, name=None,
                             entry_fade_ms=None, repeat_count=None) -> bool:
        """
        Update fields of the currently selected vamp.

        Single entry point for all vamp edits: updates the fields, pushes to
        the engine when allowed (see _push_loop_to_engine), emits
        'loops_changed' and 'loop_points_changed' once each, and schedules a
        debounced save.

        If no vamp is selected and both start and end are given, a new vamp
        is created instead.

        Args:
            start, end: new points in seconds (None = unchanged)
            crossfade_ms, early_switch_ms, exit_fade_ms, entry_fade_ms:
                per-vamp settings
            name: new name
            repeat_count: 0 or 1 = indefinite, N >= 2 = play the section N
                times in total (None = unchanged)

        Returns:
            True if the loop was updated/created, False if rejected.
        """
        if self._blocked("update_selected_loop"):
            return False
        loop = self._selected_loop()
        if loop is None:
            if start is not None and end is not None:
                return self.add_loop(start, end, name=name) is not None
            logger.warning("update_selected_loop: no vamp selected")
            return False

        new_start = loop.start if start is None else self._clamp_time(start)
        new_end = loop.end if end is None else self._clamp_time(end)
        if not _long_enough(new_start, new_end):
            logger.warning(f"Rejected loop points IN={new_start:.3f} OUT={new_end:.3f} "
                           f"(minimum {MIN_LOOP_DURATION}s)")
            return False

        points_changed = (new_start != loop.start) or (new_end != loop.end)
        engine_dirty = points_changed
        loop.set_range(new_start, new_end)

        if crossfade_ms is not None:
            value = max(0, int(crossfade_ms))
            if value != loop.crossfade_ms:
                loop.crossfade_ms = value
                engine_dirty = True
        if early_switch_ms is not None:
            loop.early_switch_ms = max(0, int(early_switch_ms))
        if exit_fade_ms is not None:
            loop.exit_fade_ms = max(0, int(exit_fade_ms))
        if entry_fade_ms is not None:
            loop.entry_fade_ms = max(0, int(entry_fade_ms))
        if repeat_count is not None:
            count = max(0, int(repeat_count))
            loop.repeat_count = count if count >= 2 else None
        if name is not None:
            loop.name = str(name)

        self.loop_start = loop.start
        self.loop_end = loop.end

        if engine_dirty:
            self._push_loop_to_engine(loop)

        self._emit_loops_update()
        self.save_loop()
        return True

    def set_loop_in(self, time_val: Optional[float] = None) -> None:
        """Set loop in point (current position if None)."""
        if time_val is None:
            time_val = self.audio.get_position()
        if self._selected_loop() is None:
            end = self.loop_end if self.loop_end > time_val else self.song_length
            self.add_loop(time_val, end)
            return
        self.update_selected_loop(start=time_val)

    def set_loop_out(self, time_val: Optional[float] = None) -> None:
        """Set loop out point (current position if None)."""
        if time_val is None:
            time_val = self.audio.get_position()
        if self._selected_loop() is None:
            self.add_loop(min(self.loop_start, time_val), time_val)
            return
        self.update_selected_loop(end=time_val)

    def set_loop_points(self, start: float, end: float) -> None:
        """Set both loop points at once (creates a vamp if none is selected)."""
        self.update_selected_loop(start=start, end=end)

    def adjust_loop_in(self, amount: float) -> None:
        """Adjust loop in point by amount in seconds."""
        self.update_selected_loop(start=self.loop_start + amount)

    def adjust_loop_out(self, amount: float) -> None:
        """Adjust loop out point by amount in seconds."""
        self.update_selected_loop(end=self.loop_end + amount)

    # ---- Preview (audition a region without touching the vamp list) ----

    def preview_loop(self, start: float, end: float) -> bool:
        """
        Audition a region as a loop without modifying any vamp.

        Arms the engine with [start, end], plays from ``start`` and lets the
        engine enter loop mode at ``end``. The monitor arms no other vamp
        while previewing. Exiting the loop (queue_exit) or calling
        stop_preview() ends the preview and restores the selected vamp's
        engine points. Returns False (and emits 'blocked_in_live_mode') in
        live mode, or when there is nothing to preview.
        """
        if self._blocked("preview_loop"):
            return False
        if not self.current_song_path:
            logger.warning("preview_loop: no song loaded")
            return False
        start = self._clamp_time(start)
        end = self._clamp_time(end)
        if not _long_enough(start, end):
            logger.warning(f"preview_loop: invalid region {start:.3f}-{end:.3f}")
            return False

        # stop() would restore the selected vamp's points; skip that since we
        # override them right away.
        self._previewing = False
        self._preview_region = None
        self.stop()

        selected = self._selected_loop()
        crossfade = selected.crossfade_ms if selected else LOOP_CROSSFADE_MS

        self._previewing = True
        self._preview_region = (start, end)
        self._armed_loop_id = None
        self.audio.set_loop_points(start, end, crossfade_ms=crossfade)
        logger.info(f"Previewing region {start:.3f}-{end:.3f}s")

        self._last_cue_pos = start
        self.audio.play_transport(start_pos=start)
        self.state = PlaybackState.PLAYING
        self._invalidate_arm()
        self._arm_engine(start)
        self._start_monitor()
        self._emit('state_change', self.state)
        self._emit('preview_changed', True)
        self._refresh_standby()
        return True

    def stop_preview(self) -> None:
        """Stop a preview and restore the selected vamp's engine points."""
        if not self._previewing:
            return
        self.stop()

    def is_previewing(self) -> bool:
        return self._previewing

    def _end_preview(self, restore: bool) -> None:
        if not self._previewing:
            return
        self._previewing = False
        self._preview_region = None
        self._invalidate_arm()
        if restore:
            selected = self._selected_loop()
            if selected is not None:
                self.audio.set_loop_points(selected.start, selected.end,
                                           crossfade_ms=selected.crossfade_ms)
                self._armed_loop_id = selected.id
        self._emit('preview_changed', False)

    # ---- Exit queue ----

    def queue_exit(self, fade_mode=False, fade_ms=None):
        """
        Exit the current loop at the next boundary (the engine performs the
        exit), or skip the upcoming loop if still in transport mode.

        Args:
            fade_mode: If True, fade out instead of cutting to transport
            fade_ms: Fade duration in ms (defaults to the vamp's exit_fade_ms)
        """
        if self.audio.mode != "loop" and self._clock() - self._count_exit_time < LATE_EXIT_GRACE_S:
            # The vamp ended on its own count a moment ago: this press was
            # meant for that vamp, not "skip the next one".
            logger.info("Exit ignored: vamp just ended on its count")
            return
        if self.audio.mode == "loop":
            current_loop = self._current_loop()
            if current_loop is None and not self._previewing:
                engine_in, _, _ = self.audio.get_loop_points()
                for loop in self.loops:
                    if abs(loop.start - engine_in) < 0.01:
                        current_loop = loop
                        break

            if current_loop is not None and not self._previewing:
                self.temp_skip_loop_id = current_loop.id

            self.exit_fade_mode = bool(fade_mode)
            if fade_mode:
                if fade_ms is not None:
                    self.exit_fade_ms = int(fade_ms)
                elif current_loop is not None:
                    self.exit_fade_ms = int(current_loop.exit_fade_ms)
                else:
                    self.exit_fade_ms = FADE_EXIT_DURATION_MS

            mode_str = f"FADE ({self.exit_fade_ms}ms)" if fade_mode else "CUT"
            logger.info(f"UI: EXIT LOOP queued ({mode_str}) - skipping loop {self.temp_skip_loop_id}")
            self.exit_queue_active = True
            if fade_mode:
                if self.audio.mode == "loop":
                    self.audio.queue_loop_exit("fade", self.exit_fade_ms)
                else:   # the loop ended meanwhile; a transport fade would stop the show
                    self.exit_queue_active = False
                    self.exit_fade_mode = False
                    logger.info("Fade exit ignored: no longer in loop mode")
            else:
                self.audio.queue_loop_exit("cut")
            if self.exit_queue_active:
                self._mirror_emit("queue_exit", fade=bool(fade_mode),
                                  fade_ms=int(self.exit_fade_ms) if fade_mode else None)
            self._refresh_standby()

        elif self.state in (PlaybackState.PLAYING, PlaybackState.HELD) and not self._previewing:
            # In transport mode - skip the next upcoming (or current) active loop
            pos = self.audio.get_position()
            target_loop = None
            for loop in self.loops:
                if not loop.active or loop.id == self.temp_skip_loop_id:
                    continue
                if pos < loop.end:
                    if target_loop is None or loop.end < target_loop.end:
                        target_loop = loop

            if target_loop:
                self.temp_skip_loop_id = target_loop.id
                logger.info(f"UI: SKIP VAMP queued - Skipping loop '{target_loop.name}' (id={target_loop.id})")
                self._arm_engine(pos)   # disarm it right away
                self._mirror_emit("queue_exit", fade=False, fade_ms=None)
                self._emit('loop_skip_queued', target_loop.name)
                self._refresh_standby()

    def is_loop_ready(self) -> bool:
        """Check if seamless loop sound is ready."""
        return self.audio.is_loop_ready()

    def is_in_loop_mode(self) -> bool:
        """Check if currently in loop mode."""
        return self.audio.mode == "loop"

    # =========================================================================
    # MARKER (CUE POINT) MANAGEMENT
    # =========================================================================

    def add_marker(self, time_pos=None, name=None):
        """Add a named marker / cue point at the given time (current position if None)."""
        if self._blocked("add_marker"):
            return None
        if time_pos is None:
            time_pos = self.audio.get_position()
        time_pos = _finite(time_pos, "marker time")
        if name is None:
            name = f"{DEFAULT_MARKER_NAME} {len(self.markers) + 1}"

        marker = Marker(time_pos, name=name)
        self.markers = sorted(self.markers + [marker], key=lambda m: m.time)

        self._emit_markers_update()
        logger.info(f"Added marker '{name}' at {time_pos:.3f}s")
        self.save_loop()
        return marker

    def rename_marker(self, marker_id, new_name):
        """Rename a marker by ID."""
        if self._blocked("rename_marker"):
            return False
        for marker in self.markers:
            if marker.id == marker_id:
                marker.name = new_name
                self._emit_markers_update()
                self.save_loop()
                return True
        return False

    def delete_marker(self, marker_id):
        """Delete a marker by ID."""
        if self._blocked("delete_marker"):
            return False
        self.markers = [m for m in self.markers if m.id != marker_id]
        self._emit_markers_update()
        self.save_loop()
        return True

    def jump_to_marker(self, marker_id):
        """
        Seek to a marker's position and start playing (cue-sheet click: an
        arbitrary jump). Blocked in live mode; next/prev cue stay allowed.
        """
        if self._blocked("jump_to_marker"):
            return False
        for marker in self.markers:
            if marker.id == marker_id:
                logger.info(f"Jumping to marker '{marker.name}' at {marker.time:.3f}s")
                self._play_from(marker.time)
                return True
        return False

    def jump_to_next_marker(self):
        """
        Jump to the next marker after current position. Allowed in
        live mode (key / MIDI next_cue walk the cue list in order).
        """
        pos = self.audio.get_position()
        for marker in sorted(self.markers, key=lambda m: m.time):
            if marker.time > pos + 0.1:
                self._play_from(marker.time)
                return True
        return False

    def jump_to_prev_marker(self):
        """
        Jump to the previous marker before current position. Allowed in
        live mode (key / MIDI prev_cue walk the cue list in order).
        """
        pos = self.audio.get_position()
        prev_marker = None
        for marker in sorted(self.markers, key=lambda m: m.time):
            if marker.time < pos - 0.5:
                prev_marker = marker
            else:
                break
        if prev_marker:
            self._play_from(prev_marker.time)
            return True
        return False

    # =========================================================================
    # HOLD CUES (CAESURAS)
    # =========================================================================

    def add_hold(self, time=None, name=None) -> Optional[HoldCue]:
        """Add a hold cue at ``time`` (current position if None)."""
        if self._blocked("add_hold"):
            return None
        if time is None:
            time = self.audio.get_position()
        if name is None:
            name = f"{DEFAULT_HOLD_NAME} {len(self.holds) + 1}"
        hold = HoldCue(self._clamp_time(time), name=name)
        self.holds = sorted(self.holds + [hold], key=lambda h: h.time)
        logger.info(f"Added hold '{name}' at {hold.time:.3f}s")
        self._emit_holds_update()
        self.save_loop()
        return hold

    def rename_hold(self, hold_id, new_name) -> bool:
        if self._blocked("rename_hold"):
            return False
        hold = self._find_hold(hold_id)
        if hold is None:
            return False
        hold.name = str(new_name)
        self._emit_holds_update()
        self.save_loop()
        return True

    def move_hold(self, hold_id, new_time) -> bool:
        """Change a hold's time."""
        if self._blocked("move_hold"):
            return False
        hold = self._find_hold(hold_id)
        if hold is None:
            return False
        hold.time = self._clamp_time(new_time)
        self.holds = sorted(self.holds, key=lambda h: h.time)
        self._emit_holds_update()
        self.save_loop()
        return True

    def delete_hold(self, hold_id) -> bool:
        if self._blocked("delete_hold"):
            return False
        before = len(self.holds)
        self.holds = [h for h in self.holds if h.id != hold_id]
        if len(self.holds) == before:
            return False
        if self._armed_hold_id == hold_id and self.state != PlaybackState.HELD:
            self._armed_hold_id = None
        self._emit_holds_update()
        self.save_loop()
        return True

    # =========================================================================
    # CUE NOTES & TAGS
    # =========================================================================

    def _find_item(self, item_id):
        """(obj, kind) for a marker / loop / hold id, or (None, None)."""
        for marker in self.markers:
            if marker.id == item_id:
                return marker, 'marker'
        for loop in self.loops:
            if loop.id == item_id:
                return loop, 'vamp'
        for hold in self.holds:
            if hold.id == item_id:
                return hold, 'hold'
        return None, None

    def _emit_item_update(self, kind: str) -> None:
        if kind == 'marker':
            self._emit_markers_update()
        elif kind == 'vamp':
            self._emit_loops_update()
        elif kind == 'hold':
            self._emit_holds_update()

    def set_item_tag_note(self, item_id, tag, note_text):
        """Set (or create) a tag with its notes on a marker, loop region or hold."""
        if self._blocked("set_item_tag_note"):
            return False
        obj, kind = self._find_item(item_id)
        if obj is None:
            return False
        obj.tag_notes[tag] = note_text
        self._emit_item_update(kind)
        self.save_loop()
        return True

    def remove_item_tag(self, item_id, tag):
        """Remove a tag (and its notes) from a marker, loop region or hold."""
        if self._blocked("remove_item_tag"):
            return False
        obj, kind = self._find_item(item_id)
        if obj is None:
            return False
        obj.tag_notes.pop(tag, None)
        self._emit_item_update(kind)
        self.save_loop()
        return True

    def set_item_osc_cue(self, item_id, osc_cue) -> bool:
        """Set the OSC cue id fired when a marker/vamp/hold is reached."""
        if self._blocked("set_item_osc_cue"):
            return False
        obj, kind = self._find_item(item_id)
        if obj is None:
            return False
        obj.osc_cue = _sanitize_osc(osc_cue)
        self._emit_item_update(kind)
        self.save_loop()
        return True

    def get_timeline_items(self):
        """
        Get all markers, vamps and holds as a unified, time-sorted list.
        Returns list of tuples: (time, type_str, object) with type_str in
        "marker" | "vamp" | "hold".
        """
        items = []
        for marker in self.markers:
            items.append((marker.time, 'marker', marker))
        for loop in self.loops:
            items.append((loop.start, 'vamp', loop))
        for hold in self.holds:
            items.append((hold.time, 'hold', hold))
        items.sort(key=lambda x: x[0])
        return items

    # =========================================================================
    # POSITION AND STATE QUERIES
    # =========================================================================

    def get_position(self) -> float:
        """Get current playback position in seconds."""
        return self.audio.get_position()

    def get_visual_position(self) -> float:
        """Position for the waveform (identical to get_position; sync_ratio is 1.0)."""
        return self.audio.get_position()

    def is_playing(self) -> bool:
        """Check if currently playing (not stopped, paused or held)."""
        return self.state == PlaybackState.PLAYING

    def is_held(self) -> bool:
        return self.state == PlaybackState.HELD

    def is_in_loop_region(self) -> bool:
        pos = self.audio.get_position()
        for loop in self.loops:
            if loop.active and loop.start <= pos < loop.end:
                return True
        return False

    # =========================================================================
    # SKIP MANAGEMENT
    # =========================================================================

    def add_skip(self, start, end, method="cut"):
        """Add a new skip region."""
        if self._blocked("add_skip"):
            return None
        start, end = _finite(start, "skip start"), _finite(end, "skip end")
        if end <= start:
            return None
        new_skip = SkipRegion(start, end, method=method)
        self.skips = sorted(self.skips + [new_skip], key=lambda x: x.start)
        self.save_loop()
        self._emit_skips_update()
        return new_skip

    def delete_skip(self, skip_id):
        if self._blocked("delete_skip"):
            return False
        self.skips = [s for s in self.skips if s.id != skip_id]
        self.save_loop()
        self._emit_skips_update()
        return True

    def toggle_skip_active(self, skip_id):
        if self._blocked("toggle_skip_active"):
            return False
        for s in self.skips:
            if s.id == skip_id:
                s.active = not s.active
                self.save_loop()
                self._emit_skips_update()
                return True
        return False

    # =========================================================================
    # TEMPO / KEY / STEMS / ROUTING / DEVICE
    # =========================================================================

    def set_tempo_pitch(self, tempo_ratio: float = 1.0, semitones: float = 0.0) -> bool:
        """Request a render; applied values are persisted only on completion."""
        if self._blocked("set_tempo_pitch"):
            return False
        return self._set_tempo_pitch(tempo_ratio, semitones)

    def _set_tempo_pitch(self, tempo_ratio: float, semitones: float) -> bool:
        tempo_ratio = max(0.5, min(2.0, float(tempo_ratio)))
        semitones = max(-12.0, min(12.0, float(semitones)))
        self.pending_tempo_pitch = (tempo_ratio, semitones)
        self._mirror_emit("tempo", tempo_ratio=tempo_ratio, semitones=semitones)
        try:
            self.audio.set_tempo_pitch(tempo_ratio, semitones)
        except Exception as e:
            logger.error(f"set_tempo_pitch failed: {e}")
            self._ev_render_failed({'message': str(e)})
            return False
        return True

    def get_tempo_pitch(self) -> Tuple[float, float]:
        return self.audio.get_tempo_pitch()

    def get_playback_rate(self) -> float:
        getter = getattr(self.audio, 'get_playback_rate', None)
        return max(0.01, float(getter() if getter else self.get_tempo_pitch()[0]))

    def countdown_to(self, target: float, position: Optional[float] = None) -> Optional[float]:
        """Playback seconds until a cue; unknown beyond a hold or active vamp."""
        pos = self.audio.get_position() if position is None else position
        if target <= pos:
            return 0.0
        # Holds / vamps inside an active skip region are jumped over
        if self.is_held() or any(pos < h.time < target and not self._in_active_skip(h.time)
                                 for h in self.holds):
            return None
        remaining = target - pos
        for skip in self.skips:
            if skip.active and skip.end > skip.start:
                remaining -= max(0.0, min(skip.end, target) - max(skip.start, pos))
        remaining = max(0.0, remaining)
        current = self._current_loop() if self.is_in_loop_mode() else None
        for loop in self.loops:
            if not loop.active or loop.id == self.temp_skip_loop_id or not pos < loop.end < target:
                continue
            if self._in_active_skip(loop.end):
                continue
            if loop.repeat_count is None:
                return None
            extra = loop.repeat_count - 1
            if current is not None and current.id == loop.id:
                extra -= self.audio.get_loop_cycle_count()
            remaining += max(0, extra) * (loop.end - loop.start)
        return remaining / self.get_playback_rate()

    def _after_stem_change(self) -> None:
        self.stems = self._engine_stems()
        self._emit('stems_changed', list(self.stems))
        self._emit_settings_update()
        self.save_loop()

    def set_playback_source(self, source: str) -> bool:
        if source not in ('Full mix', 'Stems') or self._blocked('set_playback_source'):
            return False
        stems = self._engine_stems()
        components = [st for st in stems if st.name != 'mix' and 'click' not in st.name.lower()]
        if not components:
            return False
        with getattr(self.audio, 'lock', None) or nullcontext():
            music = [st for st in stems if 'click' not in st.name.lower()]
            # Mute the outgoing source before enabling the incoming source.
            music.sort(key=lambda st: (st.name == 'mix') != (source == 'Stems'))
            for st in music:
                muted = (source == 'Stems') if st.name == 'mix' else (source == 'Full mix')
                self.audio.set_stem_mute(st.name, muted)
                self.audio.set_stem_solo(st.name, False)
                self.settings.stem(st.name).update(muted=muted, solo=False)
        self._after_stem_change()
        return True

    def set_stem_gain(self, name: str, gain_db: float) -> bool:
        self.settings.stem(name)['gain_db'] = float(gain_db)
        try:
            self.audio.set_stem_gain(name, float(gain_db))
        except Exception as e:
            logger.error(f"set_stem_gain({name}) failed: {e}")
            return False
        finally:
            self._after_stem_change()
        return True

    def set_stem_mute(self, name: str, muted: bool) -> bool:
        self.settings.stem(name)['muted'] = bool(muted)
        try:
            self.audio.set_stem_mute(name, bool(muted))
        except Exception as e:
            logger.error(f"set_stem_mute({name}) failed: {e}")
            return False
        finally:
            self._after_stem_change()
        return True

    def set_stem_solo(self, name: str, solo: bool) -> bool:
        self.settings.stem(name)['solo'] = bool(solo)
        try:
            self.audio.set_stem_solo(name, bool(solo))
        except Exception as e:
            logger.error(f"set_stem_solo({name}) failed: {e}")
            return False
        finally:
            self._after_stem_change()
        return True

    def set_routing(self, mode: str) -> bool:
        """
        Change the output routing. If the engine cannot open the device for
        ``mode`` (it raises after reverting), the setting is NOT saved,
        'device_error' is emitted and False returned.
        """
        if mode not in ROUTING_MODES:
            logger.warning(f"Unknown routing mode {mode!r}")
            return False
        self._device_change_active = True
        try:
            self.audio.set_routing(mode)
        except Exception as e:
            logger.error(f"set_routing({mode}) failed: {e}")
            self._emit('device_error', str(e))
            self._emit_settings_update()      # UI reverts its selector
            return False
        finally:
            self._device_change_active = False
            self._wd_frames = None
        self.settings.routing_mode = mode
        self._emit_settings_update()
        self.save_loop()
        return True

    def get_routing(self) -> dict:
        try:
            return dict(self.audio.get_routing())
        except Exception:
            return {"mode": self.settings.routing_mode, "channel_map": {}, "output_channels": 2}

    def list_output_devices(self) -> list:
        try:
            return list(self.audio.list_output_devices())
        except Exception as e:
            logger.error(f"list_output_devices failed: {e}")
            return []

    def set_output_device(self, index: Optional[int]) -> bool:
        """
        Switch the engine's output device (None = system default); remembered
        in preferences. If the engine cannot open it (it raises after
        reverting), nothing is saved, 'device_error' is emitted and False
        returned.
        """
        self._device_change_active = True
        try:
            self.audio.set_output_device(index)
        except Exception as e:
            logger.error(f"set_output_device({index}) failed: {e}")
            self._emit('device_error', str(e))
            return False
        finally:
            self._device_change_active = False
            self._wd_frames = None
        # Remember the device NAME too: Windows renumbers devices when
        # interfaces are plugged in or out, so an index alone is fragile.
        name = None
        if index is not None:
            name = next((d.get("name") for d in self.list_output_devices()
                         if d.get("index") == index), None)
        try:
            preferences.save_preferences({"audio": {"output_device": index,
                                                    "output_device_name": name}})
        except Exception as e:
            logger.warning(f"Could not save output device preference: {e}")
        return True

    def restore_output_device(self) -> Optional[int]:
        """
        Re-apply the saved output device at startup. Matches by name first
        (indices shift when devices change), then by index; falls back to
        the system default. Returns the index applied, or None.
        """
        try:
            saved = preferences.load_preferences().get("audio") or {}
        except Exception:
            return None
        name, index = saved.get("output_device_name"), saved.get("output_device")
        devices = self.list_output_devices()
        match = None
        if name:
            match = next((d for d in devices if d.get("name") == name), None)
        if match is None and index is not None and not name:
            match = next((d for d in devices if d.get("index") == index), None)
        if match is None:
            if name or index is not None:
                logger.info(f"Saved output device {name or index!r} not present; using the default")
            return None
        try:
            self.audio.set_output_device(match["index"])
        except Exception as e:
            logger.warning(f"Could not restore output device {match.get('name')}: {e}")
            return None
        return match["index"]

    def set_song_osc_cue(self, osc_cue: str) -> bool:
        if self._blocked("set_song_osc_cue"):
            return False
        self.settings.osc_cue = _sanitize_osc(osc_cue)
        self._emit_settings_update()
        self.save_loop()
        return True

    def configure_osc(self, enabled: bool, host: str, port: int, scheme: str = "loopstation") -> dict:
        """Configure and persist OSC output."""
        self.osc.configure(enabled, host, port, scheme)
        return self.osc.save_settings()

    # ---- tempo map ----

    def set_song_tempo_map(self, bpm: float, beats_per_bar: int = 4,
                           downbeat_offset: float = 0.0) -> bool:
        """
        Set the song's tempo map. Raises ValueError for values that are not
        numbers or not finite (nan / inf); nothing is changed then.
        """
        if self._blocked("set_song_tempo_map"):
            return False
        # Validate everything before touching the settings (no partial update)
        bpm = max(0.0, _finite(bpm, "bpm"))
        beats_per_bar = max(1, int(_finite(beats_per_bar, "beats_per_bar")))
        downbeat_offset = _finite(downbeat_offset, "downbeat_offset")
        self.settings.bpm = bpm
        self.settings.beats_per_bar = beats_per_bar
        self.settings.downbeat_offset = downbeat_offset
        self._emit_settings_update()
        self.save_loop()
        return True

    def bar_beat_at(self, position: float) -> Tuple[int, int, float]:
        """
        (bar, beat, fraction), 1-based, for a source-time position using the
        song's tempo map. (0, 0, 0.0) when no BPM is set.
        """
        s = self.settings
        if not (s.bpm > 0 and math.isfinite(s.bpm) and math.isfinite(s.downbeat_offset)):
            return (0, 0, 0.0)
        bpb = max(1, int(s.beats_per_bar))
        total_beats = (float(position) - s.downbeat_offset) * s.bpm / 60.0
        if not math.isfinite(total_beats):
            return (0, 0, 0.0)
        # A position exactly on a beat must read as that beat even when the
        # float product lands a hair below it (4.1 s @120, offset 0.1 -> 7.9999...)
        whole = int(np.floor(total_beats + 1e-6))
        fraction = max(0.0, total_beats - whole)
        bar = whole // bpb + 1
        beat = whole % bpb + 1
        return (int(bar), int(beat), float(fraction))

    def estimate_tempo(self, wait: bool = False):
        """
        Estimate BPM and first-downbeat offset with librosa on a worker thread.
        Emits 'tempo_estimated'(bpm, downbeat_offset). With wait=True the
        result tuple is returned (None if nothing could be analysed).
        """
        raw = self.audio.get_raw_audio_data()
        if raw is None or len(raw) == 0:
            logger.warning("estimate_tempo: no audio loaded")
            return None
        sr = int(getattr(self.audio, 'sample_rate', None)
                 or getattr(self.audio, 'SAMPLE_RATE', None) or SAMPLE_RATE)
        frames = int(min(len(raw), TEMPO_ESTIMATE_SECONDS * sr))
        samples = np.array(raw[:frames])
        load_gen = self._load_generation
        result: Dict[str, Any] = {}

        def _worker():
            try:
                bpm, offset = _compute_tempo(samples, sr)
            except Exception as e:
                logger.error(f"Tempo estimation failed: {e}")
                return
            if load_gen != self._load_generation:
                logger.info("Discarding stale tempo estimate")
                return
            result['value'] = (bpm, offset)
            self._emit('tempo_estimated', bpm, offset)

        thread = threading.Thread(target=_worker, name="tempo-estimate", daemon=True)
        thread.start()
        if wait:
            thread.join()
            return result.get('value')
        return None

    # =========================================================================
    # DETECTION (loop candidates / smart cuts share one worker)
    # =========================================================================

    def run_loop_detection(self, start_time, end_time):
        """Run loop detection in a background thread; emits 'detection_complete'."""
        self._run_detection('loops', start_time, end_time)

    def run_smart_cut_detection(self, start_time, end_time):
        """Run beat-aligned cut analysis in a background thread; emits 'cut_detection_complete'."""
        self._run_detection('cuts', start_time, end_time)

    def cancel_detection(self) -> None:
        """Drop the results of any detection currently running."""
        self._detection_generation += 1

    def _run_detection(self, kind, start_time, end_time):
        raw = self.audio.get_raw_audio_data()
        if raw is None:
            logger.warning("Detection requested with no audio loaded")
            return
        sr = int(getattr(self.audio, 'sample_rate', None)
                 or getattr(self.audio, 'SAMPLE_RATE', None) or SAMPLE_RATE)

        self._detection_generation += 1
        detection_gen = self._detection_generation
        load_gen = self._load_generation
        event = 'detection_complete' if kind == 'loops' else 'cut_detection_complete'

        self._emit('detection_started')

        def _worker():
            results = []
            try:
                detector = LoopDetector(raw, sr)
                if kind == 'loops':
                    results = detector.find_loops(start_time, end_time)
                else:
                    results = detector.find_smart_cuts(start_time, end_time)
            except Exception as e:
                logger.error(f"Detection ({kind}) failed: {e}")
                results = []

            if detection_gen != self._detection_generation or load_gen != self._load_generation:
                logger.info(f"Discarding stale {kind} detection result")
                return
            self._emit(event, results)

        threading.Thread(target=_worker, name=f"detect-{kind}", daemon=True).start()

    # =========================================================================
    # SHOW (running order / setlist)
    # =========================================================================
    #
    # self.show is always a Show (backend/show.py); empty by default, and an
    # empty show means GO at the end of a song falls back to
    # next_song_provider (library order). Cue data stays per song in
    # loop_data.json; the show only holds the order and cut/segue flags.
    #
    # Live Mode: opening/creating shows and every order edit are blocked
    # (_blocked); set_next_up, GO advance, segue and saving are allowed.

    def _show_active(self) -> bool:
        return len(self.show.entries) > 0

    @property
    def next_up_entry_id(self) -> Optional[str]:
        """
        The entry GO advances to at the end of the current song: the
        operator's set_next_up() choice if still valid, else the next
        playable entry after the current one (from the start when no show
        entry is loaded). None at the end of the show.
        """
        with self._show_lock:
            show = self.show
            chosen = show.get(self.next_up_override)
            if chosen is not None and chosen.kind == "song" and not show.is_missing(chosen) \
                    and chosen.id != self.current_entry_id:
                return chosen.id
            if show.get(self.current_entry_id) is not None:
                entry = show.next_playable_after(self.current_entry_id)
            else:
                entry = show.next_playable_from(self._show_anchor_index or 0)
            return entry.id if entry is not None else None

    def show_snapshot(self) -> dict:
        """
        Plain-data copy of the show for the UI / mirroring (never live objects):
        {"name", "file_path", "folder", "is_folder_derived", "dirty",
         "entries": [{"id", "kind", "path", "title", "cut", "segue",
                      "index", "missing", "playable"}],
         "current_entry_id", "next_up_entry_id", "next_up_override"}
        """
        with self._show_lock:
            show = self.show
            return {
                "name": show.name, "file_path": show.file_path, "folder": show.folder,
                "is_folder_derived": show.is_folder_derived, "dirty": show.dirty,
                "entries": show.snapshot(),
                "current_entry_id": self.current_entry_id,
                "next_up_entry_id": self.next_up_entry_id,
                "next_up_override": self.next_up_override,
            }

    def _show_dir(self) -> str:
        """App data dir holding folder-derived show autosaves (next to loop_data.json)."""
        return self.data_dir

    def _install_show(self, show: Show) -> None:
        """Make ``show`` current: re-match the loaded song, reset next-up, notify."""
        self.flush_show_autosave()
        with self._show_lock:
            self.show = show
            self.next_up_override = None
            self._show_anchor_index = None
            self._pending_go_entry_id = None
            entry = show.find_by_path(self.current_song_path) if self.current_song_path else None
            self.current_entry_id = entry.id if entry else None
        with self._preload_lock:
            self._preload_requested = None
        self._emit('current_entry_changed', self.current_entry_id)
        self._after_show_change(edited=False)

    def open_show(self, path: str) -> bool:
        """
        Open a ``.loopshow`` file. Blocked in Live Mode. On failure emits
        'data_load_failed' (message) and keeps the current show.
        Unreadable entries are reported the same way (and kept in the file).
        """
        if self._blocked("open_show"):
            return False
        try:
            show = Show.load(path)
        except (OSError, ShowFileError) as e:
            msg = f"Could not open show '{os.path.basename(path)}': {e}"
            logger.error(msg)
            self.last_load_error = msg
            self._emit('data_load_failed', msg)
            return False
        if show.load_problems:
            msg = (f"{len(show.load_problems)} entry(ies) of show '{show.name}' could not be read "
                   "and were skipped (they are kept in the file)")
            logger.error(msg)
            self.last_load_error = msg
            self._emit('data_load_failed', msg)
        self._install_show(show)
        preferences.add_recent_show("file", show.file_path)
        preferences.set_last_show("file", show.file_path)
        return True

    def open_folder(self, folder: str) -> bool:
        """
        Folder-derived show for ``folder`` (its remembered order, with new
        files appended and deleted ones shown missing). Blocked in Live Mode.
        """
        if self._blocked("open_folder"):
            return False
        if not folder or not os.path.isdir(folder):
            logger.error(f"open_folder: not a folder: {folder!r}")
            return False
        show = Show.from_folder(folder, data_dir=self._show_dir())
        self._install_show(show)
        if show.dirty:
            self._schedule_show_autosave()
        preferences.add_recent_show("folder", show.folder)
        preferences.set_last_show("folder", show.folder)
        return True

    def new_show(self, name: str = "Untitled show") -> bool:
        """Start an empty, unsaved show. Blocked in Live Mode."""
        if self._blocked("new_show"):
            return False
        self._install_show(Show(name=name or "Untitled show"))
        preferences.set_last_show(None, None)
        return True

    def save_show(self, path: Optional[str] = None) -> bool:
        """
        Save the show to ``path`` (Save As) or its own file. A folder-derived
        or new show without ``path`` returns False (the UI should ask for a
        file name). Allowed in Live Mode. Failure emits 'show_save_failed'.
        """
        with self._show_lock:
            show = self.show
            if path is None and show.file_path is None:
                return False
            try:
                saved = show.save(path)
            except (OSError, ValueError) as e:
                msg = f"Could not save show: {e}"
                logger.error(msg)
                self._emit('show_save_failed', msg)
                return False
        self._cancel_show_autosave()
        preferences.add_recent_show("file", saved)
        preferences.set_last_show("file", saved)
        self._emit('show_changed', self.show_snapshot())
        return True

    # ------------------------------------------------------------ editing --

    def _show_edit(self, what: str, func, changed=bool):
        """Run a Show edit unless Live Mode blocks it; notify when ``changed(result)``."""
        if self._blocked(what):
            return None
        with self._show_lock:
            result = func(self.show)
        if changed(result):
            self._after_show_change(edited=True)
        return result

    def add_songs(self, paths, index: Optional[int] = None) -> List[str]:
        """Insert audio files (non-audio / missing ones ignored). Returns the new entry ids."""
        return self._show_edit("add_songs", lambda s: s.add_songs(list(paths or []), index)) or []

    def add_divider(self, title: str = "", index: Optional[int] = None) -> Optional[str]:
        return self._show_edit("add_divider", lambda s: s.add_divider(title, index))

    def move_entry(self, entry_id: str, new_index: int) -> bool:
        return bool(self._show_edit("move_entry", lambda s: s.move(entry_id, new_index)))

    def remove_entry(self, entry_id: str) -> bool:
        def _remove(show):
            index = show.index_of(entry_id)
            ok = show.remove(entry_id)
            if ok and entry_id == self.current_entry_id:
                # Keep GO's place: the next-up is what followed the removed entry
                self.current_entry_id = None
                self._show_anchor_index = index
                self._emit('current_entry_changed', None)
            return ok
        return bool(self._show_edit("remove_entry", _remove))

    def duplicate_entry(self, entry_id: str) -> Optional[str]:
        return self._show_edit("duplicate_entry", lambda s: s.duplicate(entry_id))

    def rename_entry(self, entry_id: str, title: str) -> bool:
        return bool(self._show_edit("rename_entry", lambda s: s.rename(entry_id, title)))

    def set_entry_cut(self, entry_id: str, cut: bool) -> bool:
        return bool(self._show_edit("set_entry_cut", lambda s: s.set_cut(entry_id, cut)))

    def set_entry_segue(self, entry_id: str, segue: bool) -> bool:
        return bool(self._show_edit("set_entry_segue", lambda s: s.set_segue(entry_id, segue)))

    def relink_entry(self, entry_id: str, new_path: str) -> List[str]:
        """Relink a (missing) entry; other missing files found in that folder too. Returns relinked ids."""
        return self._show_edit("relink_entry", lambda s: s.relink(entry_id, new_path)) or []

    def _after_show_change(self, edited: bool) -> None:
        """Validate ids, autosave a folder-derived show, emit 'show_changed', update next-up."""
        with self._show_lock:
            show = self.show
            if self.next_up_override and show.get(self.next_up_override) is None:
                self.next_up_override = None
            if self.current_entry_id and show.get(self.current_entry_id) is None:
                self.current_entry_id = None
            if self.current_entry_id:
                self._show_anchor_index = None
        if edited and show.is_folder_derived and show.dirty:
            self._schedule_show_autosave()
        self._emit('show_changed', self.show_snapshot())
        self._notify_next_up()

    def _notify_next_up(self, force: bool = False) -> None:
        """Emit 'next_up_changed' if the effective next-up changed; refresh standby; preload."""
        next_id = self.next_up_entry_id
        if force or next_id != self._last_next_up:
            self._last_next_up = next_id
            self._emit('next_up_changed', next_id)
        self._refresh_standby()
        self._schedule_preload()

    # ----------------------------------------------------- show autosave --

    def _schedule_show_autosave(self) -> None:
        with self._show_lock:
            if self._show_save_timer is not None:
                self._show_save_timer.cancel()
            timer = threading.Timer(SAVE_DEBOUNCE_MS / 1000.0, self.flush_show_autosave)
            timer.daemon = True
            self._show_save_timer = timer
            timer.start()

    def _cancel_show_autosave(self) -> None:
        with self._show_lock:
            if self._show_save_timer is not None:
                self._show_save_timer.cancel()
                self._show_save_timer = None

    def flush_show_autosave(self) -> bool:
        """Write a pending folder-derived show autosave now. Never raises."""
        with self._show_lock:
            self._cancel_show_autosave()
            show = self.show
            if not (show.is_folder_derived and show.dirty):
                return True
            try:
                show.autosave(self._show_dir())
                return True
            except OSError as e:
                msg = f"Could not remember the show order: {e}"
                logger.error(msg)
                self._emit('show_save_failed', msg)
                return False

    # ---------------------------------------------- next-up / loading ----

    def set_next_up(self, entry_id: Optional[str]) -> bool:
        """
        Choose the song GO goes to next (going off-script). ALLOWED in Live
        Mode. ``entry_id`` must be a song entry whose file exists (a cut song
        may be chosen explicitly); None returns to the computed next.
        Emits 'next_up_changed' and refreshes standby.
        """
        with self._show_lock:
            if entry_id is not None:
                entry = self.show.get(entry_id)
                if entry is None or entry.kind != "song" or self.show.is_missing(entry):
                    return False
            self.next_up_override = entry_id
            entry = self.show.get(entry_id)
            file = os.path.basename(entry.path) if entry is not None and entry.path else ""
        self._mirror_emit("next_up", entry_id=entry_id, file=file)
        self._notify_next_up(force=True)
        return True

    def load_entry(self, entry_id: str, from_go: bool = False) -> bool:
        """
        Load the song of a show entry (via load_song: the Live Mode load lock
        applies unless ``from_go``) and make it the current entry.
        """
        with self._show_lock:
            entry = self.show.get(entry_id)
            if entry is None or entry.kind != "song":
                return False
            path = entry.path
        if not from_go and self.live_mode:
            return self.load_song(path, from_go=False)   # emits the block
        with self._show_lock:
            self._pending_go_entry_id = entry_id
        return self.load_song(path, from_go=from_go)

    def _request_next_song(self, target: Tuple[str, Optional[str], str]) -> None:
        """GO / segue to ``target``: emit 'go_next_song' (path) and, for a show entry, 'go_next_entry'."""
        path, entry_id, _ = target
        if entry_id:
            with self._show_lock:
                self._pending_go_entry_id = entry_id
        self._emit('go_next_song', path)
        if entry_id:
            self._emit('go_next_entry', entry_id)

    def _after_song_loaded(self, path: str) -> None:
        """load_song succeeded: set the current entry, then update next-up and preload."""
        with self._show_lock:
            show = self.show
            entry = show.get(self._pending_go_entry_id)
            if entry is None or norm_path(entry.path) != norm_path(path):
                entry = show.get(self.current_entry_id)
                if entry is None or norm_path(entry.path) != norm_path(path):
                    # Manual load: reloading the current entry keeps it;
                    # otherwise the first match after it, else the first.
                    entry = show.find_by_path(path, after_id=self.current_entry_id)
            self._pending_go_entry_id = None
            new_id = entry.id if entry is not None else None
            changed = new_id != self.current_entry_id
            self.current_entry_id = new_id
            if new_id is not None:
                self._show_anchor_index = None
            if self.next_up_override and self.next_up_override == new_id:
                self.next_up_override = None   # the operator's choice was taken
        with self._preload_lock:
            self._preload_requested = None   # the engine consumed / dropped its preload
        self._mirror_emit("load", entry_id=new_id, file=os.path.basename(path))
        if changed:
            self._emit('current_entry_changed', new_id)
        self._notify_next_up()

    def _maybe_segue(self) -> None:
        """Song ended naturally: if the current entry segues, advance like GO (source "segue")."""
        with self._show_lock:
            entry = self.show.get(self.current_entry_id)
            if entry is None or not entry.segue:
                return
        mirror = getattr(self, 'mirror', None)
        if mirror is not None and mirror.is_following():
            logger.info("Segue left to the mirror main (backup is following)")
            return
        target = self._next_song_target()
        if target is None or not target[1]:
            return
        with self._go_lock:
            self._last_go_time = -1e9   # a segue is never debounced
        logger.info(f"Segue: {entry.title} -> {target[2]}")
        self.go(source="segue")

    # ------------------------------------------------------------ preload --

    def _schedule_preload(self) -> None:
        """Ask the engine (on a worker thread) to preload the next-up entry's song."""
        audio = self.audio
        if getattr(audio, 'preload_file', None) is None:
            return
        with self._show_lock:
            entry = self.show.get(self.next_up_entry_id) if self._show_active() else None
            path = entry.path if entry is not None else None
        with self._preload_lock:
            if path == self._preload_requested:
                return
            self._preload_requested = path
            self._preload_generation += 1
            generation = self._preload_generation
        if path is None:
            cancel = getattr(audio, 'cancel_preload', None)
            if cancel is not None:
                threading.Thread(target=self._safe_call, args=(cancel,),
                                 name="preload-cancel", daemon=True).start()
            return
        thread = threading.Thread(target=self._preload_worker, args=(generation, path),
                                  name="preload-request", daemon=True)
        self._preload_thread = thread
        thread.start()

    @staticmethod
    def _safe_call(func, *args) -> None:
        try:
            func(*args)
        except Exception as e:
            logger.warning(f"{getattr(func, '__name__', func)} failed: {e}")

    def _preload_worker(self, generation: int, path: str) -> None:
        """Gather stems + the song's saved tempo/key (file I/O), then call preload_file."""
        try:
            stem_paths = self.discover_stems(path)
        except Exception as e:
            logger.warning(f"Stem discovery for preload failed: {e}")
            stem_paths = {}
        tempo_pitch = self._saved_tempo_pitch_for(self._get_file_fingerprint(path))
        with self._preload_lock:
            if generation != self._preload_generation:
                return   # superseded
        try:
            self.audio.preload_file(path, stem_paths or None, tempo_pitch)
        except Exception as e:
            logger.warning(f"preload_file failed for {os.path.basename(path)}: {e}")
            self._emit('preload_failed', path, str(e))

    def get_preloaded_audio(self, path: str):
        """The engine's preloaded mix (int16 memmap (frames, 2)) for ``path``, or None."""
        getter = getattr(self.audio, 'get_preloaded_audio', None)
        if getter is None:
            return None
        try:
            return getter(path)
        except Exception as e:
            logger.warning(f"get_preloaded_audio failed: {e}")
            return None

    # =========================================================================
    # ENGINE EVENTS
    # =========================================================================

    def _on_engine_event(self, name: str, payload: dict) -> None:
        """Engine callback (worker thread): translate into state transitions."""
        handler = getattr(self, f"_ev_{name}", None)
        if handler is None:
            logger.debug(f"Unhandled engine event {name}: {payload}")
            return
        try:
            handler(payload or {})
        except Exception:
            logger.exception(f"Engine event handler for {name} crashed")

    def _ev_loop_entered(self, payload: dict) -> None:
        if self.state == PlaybackState.STOPPED:
            return  # stale event delivered after stop()
        self.exit_queue_active = False
        self.exit_fade_mode = False
        self._prev_cycle_pos = 0.0
        loop = None
        if not self._previewing:
            loop = self._find_loop(self._armed_loop_id)
            self._looping_loop_id = loop.id if loop else None
        logger.info(f">>> LOOP MODE: {loop.name if loop else 'preview'} "
                    f"{payload.get('loop_in', 0):.3f}-{payload.get('loop_out', 0):.3f} <<<")
        self._emit('loop_mode_enter')
        if loop is not None:
            self.osc.send_event("vamp_enter", id=loop.id, name=loop.name)
            if loop.osc_cue:
                self.osc.send_cue(loop.osc_cue)
        self._refresh_standby()

    def _ev_loop_cycle(self, payload: dict) -> None:
        if self.state == PlaybackState.STOPPED:
            return  # stale event delivered after stop()
        # Engine cycle n = n-th wrap, so the section is now on pass n + 1.
        n = int(payload.get('cycle', 0))
        loop = self._find_loop(self._looping_loop_id)
        total = loop.repeat_count if loop is not None else None
        self._emit('loop_cycle', n + 1, total)

    def _ev_loop_exited(self, payload: dict) -> None:
        reason = payload.get('reason', 'cut')
        position = float(payload.get('position', self.audio.get_position()))
        loop = self._find_loop(self._looping_loop_id)
        if loop is not None:
            exited_end = loop.end
        elif self._preview_region is not None:
            exited_end = self._preview_region[1]
        else:
            exited_end = position
        logger.info(f"Loop exited ({reason}) at {position:.3f}s")

        self.exit_queue_active = False
        self.exit_fade_mode = False
        self._prev_cycle_pos = 0.0
        self._looping_loop_id = None
        if loop is not None and reason in ("cut", "count", "fade"):
            # Do not re-enter the vamp we just left until well past its end
            self.temp_skip_loop_id = loop.id
        if reason in ("count", "cut", "fade"):
            # A second exit press arriving just after (double tap, key +
            # pedal) must not skip the NEXT vamp; see queue_exit.
            self._count_exit_time = self._clock()
        self._invalidate_arm()

        if reason == "fade":
            logger.info("Fade exit completed")
            self._monitor_stop_event.set()
            self.temp_skip_loop_id = None
            self.state = PlaybackState.STOPPED
            self._ended_at_rest = not self._previewing   # GO must not restart from 0:00
            if self._previewing:
                self._end_preview(restore=True)
            self.flush_save()
            self._emit('state_change', self.state)
        elif reason != "seek" and self._previewing:
            self._end_preview(restore=True)

        self._emit('loop_mode_exit', exited_end)
        if loop is not None:
            self.osc.send_event("vamp_exit", id=loop.id, name=loop.name, reason=reason)
        self._refresh_standby()

    def _ev_hold_reached(self, payload: dict) -> None:
        if self.state == PlaybackState.STOPPED:
            return  # stale event delivered after stop()
        if self.state == PlaybackState.PAUSED:
            # Paused just before this (queued) event was handled: stay
            # PAUSED; play() notices the parked engine and enters HELD then.
            logger.info("HOLD reached while paused; will park on it when resumed")
            return
        self._enter_held(payload.get('position'))

    def _enter_held(self, position=None) -> None:
        """Switch to HELD on the armed hold (or the one at ``position``) and announce it."""
        hold = self._find_hold(self._armed_hold_id)
        if hold is None:
            pos = float(self.audio.get_position() if position is None else position)
            hold = next((h for h in self.holds if abs(h.time - pos) <= 0.05), None)
        name = hold.name if hold else "hold"
        logger.info(f"HOLD reached: {name}")
        self.state = PlaybackState.HELD
        self._emit('state_change', self.state)
        self._emit('hold_reached', hold)
        if hold is not None:
            self.osc.send_event("hold_reached", id=hold.id, name=hold.name)
            self.osc.send_event("cue_reached", id=hold.id, name=hold.name, type="hold")
            if hold.osc_cue:
                self.osc.send_cue(hold.osc_cue)
        self._refresh_standby()

    def _ev_hold_released(self, payload: dict) -> None:
        logger.info("HOLD released")
        self._armed_hold_id = None
        self._armed_hold_key = None
        if self.state == PlaybackState.HELD:
            self.state = PlaybackState.PLAYING
            self._emit('state_change', self.state)
        self._emit('hold_released')
        self._refresh_standby()

    def _ev_skip_executed(self, payload: dict) -> None:
        logger.info(f"Skip executed {payload.get('from_pos', 0):.3f} -> {payload.get('to_pos', 0):.3f}")
        self._last_cue_pos = float(payload.get('to_pos', self.audio.get_position()))

    def _ev_song_ended(self, payload: dict) -> None:
        if self.state == PlaybackState.STOPPED:
            return  # stale event delivered after stop()
        logger.info("Song ended")
        with self._mirror_quiet():   # each machine's own engine ends its song
            self.stop()
        self._ended_at_rest = True   # GO must not restart from 0:00
        self._refresh_standby()
        self._emit('song_ended')
        self._maybe_segue()

    def _ev_preload_ready(self, payload: dict) -> None:
        path = str(payload.get('path') or "")
        logger.info(f"Preload ready: {os.path.basename(path)}")
        self._emit('preload_ready', path)

    def _ev_preload_failed(self, payload: dict) -> None:
        path = str(payload.get('path') or "")
        message = str(payload.get('error') or payload.get('message') or "")
        logger.warning(f"Preload failed for {os.path.basename(path)}: {message}")
        self._emit('preload_failed', path, message)

    def _ev_render_started(self, payload: dict) -> None:
        self._emit('render_state', "rendering",
                   f"tempo {payload.get('tempo_ratio', 1.0)} / {payload.get('semitones', 0.0)} st")

    def _ev_render_done(self, payload: dict) -> None:
        self.pending_tempo_pitch = None
        self._saved_tempo_pitch = None
        self.settings.tempo_ratio, self.settings.semitones = self.audio.get_tempo_pitch()
        self._emit_settings_update()
        self.save_loop()
        self._emit('render_state', "done", "")

    def _ev_render_failed(self, payload: dict) -> None:
        self.pending_tempo_pitch = None
        self._saved_tempo_pitch = None
        self.settings.tempo_ratio, self.settings.semitones = self.audio.get_tempo_pitch()
        self._emit_settings_update()
        self.save_loop()
        self._emit('render_state', "failed", str(payload.get('message', '')))

    def _ev_stopped(self, payload: dict) -> None:
        """The engine stopped by itself (e.g. a transport fade finished)."""
        if self.state != PlaybackState.PLAYING:
            return
        logger.info(f"Engine stopped ({payload.get('reason', '?')})")
        self._monitor_stop_event.set()
        self._reset_transient_flags()
        self._ended_at_rest = not self._previewing   # faded out: GO must not restart
        if self._previewing:
            self._end_preview(restore=True)
        self._invalidate_arm()
        self.flush_save()
        self._set_state(PlaybackState.STOPPED)
        if payload.get('reason') == "error":
            # The engine's safety guard stopped playback: tell the operator
            self._emit('device_error', str(payload.get('message')
                                           or "Playback stopped: audio engine error (see log)"))

    def _ev_device_error(self, payload: dict) -> None:
        msg = str(payload.get('message', ''))
        logger.error(f"Audio device error: {msg}")
        if self.device_lost or self._device_change_active:
            # Already in the alert state (reported once), or a routing /
            # device change is running: it reports its own failure.
            return
        if (self.state == PlaybackState.PLAYING and not self._device_change_active
                and not msg.startswith("render failed")):
            # Device loss while playing: the monitor thread restarts the
            # stream (never on the engine's event thread).
            self._device_loss_pending = msg or "audio device error"
            return
        self._emit('device_error', msg)

    def _ev_device_restored(self, payload: dict) -> None:
        self._mark_device_restored(str(payload.get('name') or ''))

    # =========================================================================
    # DEVICE-LOSS WATCHDOG
    # =========================================================================

    def _frames_rendered(self) -> Optional[int]:
        value = getattr(self.audio, 'frames_rendered', None)
        if callable(value):
            try:
                value = value()
            except Exception:
                return None
        return value if isinstance(value, int) else None

    def _device_name(self) -> str:
        try:
            dev = self.audio.get_output_device()
        except Exception:
            dev = None
        if isinstance(dev, dict) and dev.get('name'):
            return str(dev['name'])
        return "default output"

    def _watchdog_tick(self) -> None:
        """
        Called from every monitor tick. While PLAYING the engine's
        frames_rendered must keep advancing; a stall of DEVICE_STALL_S (or a
        device_error event) starts device-loss handling. While lost, retry
        restart_stream() every DEVICE_RETRY_S.
        """
        now = self._clock()
        pending, self._device_loss_pending = self._device_loss_pending, None
        if self.device_lost:
            if now >= self._device_retry_at:
                self._try_restore_device()
            return
        if pending is not None:
            if self.state == PlaybackState.PLAYING:
                self._device_loss(pending)
            else:
                self._emit('device_error', pending)
            return
        audio = self.audio
        running = (self.state == PlaybackState.PLAYING and not self._device_change_active
                   and bool(getattr(audio, 'is_playing', False))
                   and not getattr(audio, 'is_paused', False)
                   and not getattr(audio, 'is_held', False))
        frames = self._frames_rendered() if running else None
        if frames is None:
            self._wd_frames = None
            return
        if self._wd_frames is None or frames != self._wd_frames:
            self._wd_frames = frames
            self._wd_changed_at = now
            return
        if now - self._wd_changed_at >= DEVICE_STALL_S:
            self._device_loss("Audio output stopped (the device may have been unplugged)")

    def _device_loss(self, message: str) -> None:
        with self._device_lock:
            if self.device_lost:
                return
            self.device_lost = True
        logger.error(f"AUDIO DEVICE LOST: {message}")
        self._emit('device_lost', message)
        self._emit('device_error', message)
        self._try_restore_device()

    def _try_restore_device(self) -> bool:
        """One restart_stream() attempt; on failure park in PAUSED and retry later."""
        restart = getattr(self.audio, 'restart_stream', None)
        ok = False
        if callable(restart):
            try:
                ok = bool(restart())
            except Exception as e:
                logger.error(f"restart_stream failed: {e}")
        if ok:
            self._mark_device_restored(self._device_name())
            return True
        self._device_retry_at = self._clock() + DEVICE_RETRY_S
        if self.state == PlaybackState.PLAYING:
            # Alert state: keep the monitor alive (PAUSED) so it keeps retrying
            self.state = PlaybackState.PAUSED
            try:
                self.audio.pause_transport()
            except Exception:
                pass
            self._emit('state_change', self.state)
        self._refresh_standby()
        return False

    def _mark_device_restored(self, name: str) -> None:
        with self._device_lock:
            if not self.device_lost:
                return
            self.device_lost = False
        self._wd_frames = None
        logger.info(f"Audio device restored: {name}")
        self._emit('device_restored', name or self._device_name())
        self._refresh_standby()

    # =========================================================================
    # CONTROL SURFACES (MIDI actions)
    # =========================================================================

    def perform_action(self, action: str, source: str = "midi") -> None:
        """Perform a named control action (see midi_control.ACTIONS)."""
        if action == "go":
            self.go(source)
        elif action == "play_pause":
            self.toggle_play_pause()
        elif action == "stop":
            self.stop()
        elif action == "exit_vamp":
            self.queue_exit(fade_mode=False)
        elif action == "fade_exit":
            self.queue_exit(fade_mode=True)
        elif action == "skip_vamp":
            if self.audio.mode != "loop":
                self.queue_exit(fade_mode=False)
        elif action == "next_cue":
            self.jump_to_next_marker()
        elif action == "prev_cue":
            self.jump_to_prev_marker()
        elif action == "add_marker":
            self.add_marker()
        elif action == "toggle_live":
            if source == "midi":
                self.toggle_live_from_control()
            else:
                self.set_live_mode(not self.live_mode)
        else:
            logger.warning(f"Unknown control action {action!r}")

    def _on_control_action(self, action: str) -> None:
        try:
            self.perform_action(action, source="midi")
        except Exception:
            logger.exception(f"Control action {action!r} failed")

    # =========================================================================
    # MONITOR THREAD
    # =========================================================================

    def _start_monitor(self) -> None:
        """
        Always start a fresh monitor run: signal the previous run to stop,
        join it (unless we ARE it), then spawn a new thread with a new
        generation number.
        """
        old_thread = self._monitor_thread
        self._monitor_stop_event.set()
        if old_thread is not None and old_thread.is_alive() \
                and old_thread is not threading.current_thread():
            old_thread.join(timeout=1.0)
            if old_thread.is_alive():
                logger.warning("Previous monitor thread did not exit in time")

        self._wd_frames = None   # fresh watchdog baseline for the new run
        self._monitor_generation += 1
        generation = self._monitor_generation
        stop_event = threading.Event()
        self._monitor_stop_event = stop_event
        thread = threading.Thread(
            target=self._monitor, args=(stop_event, generation),
            name=f"monitor-{generation}", daemon=True
        )
        self._monitor_thread = thread
        thread.start()

    def _monitor(self, stop_event: threading.Event, generation: int) -> None:
        """
        Monitor thread: every UI_UPDATE_INTERVAL seconds it
        1. emits 'position_update' for the UI (throttled to UI_EMIT_INTERVAL)
        2. keeps the engine armed (next vamp, next hold, skip regions)
        3. recomputes the standby label and emits 'standby_changed' on change
        Timing-critical work (entries, wraps, holds, skips) is the engine's.
        """
        logger.debug(f"Monitor thread started (gen={generation})")
        last_emit = 0.0
        try:
            while (not stop_event.is_set() and generation == self._monitor_generation
                   and self.state != PlaybackState.STOPPED):
                now = time.monotonic()
                emit = now - last_emit >= UI_EMIT_INTERVAL
                self._monitor_tick(emit)
                if emit:
                    last_emit = now
                time.sleep(UI_UPDATE_INTERVAL)
        except Exception:
            logger.exception("Monitor thread crashed")
        logger.debug(f"Monitor thread exiting (gen={generation})")

    def _monitor_tick(self, emit: bool = True) -> None:
        """One monitor iteration (also callable from tests)."""
        audio = self.audio
        pos = audio.get_position()
        if self.state == PlaybackState.STOPPED:
            return
        self._watchdog_tick()
        if self.state == PlaybackState.STOPPED:
            return
        if emit and self.state in (PlaybackState.PLAYING, PlaybackState.HELD):
            self._emit('position_update', pos, audio.mode == "loop")
        if self.state == PlaybackState.PLAYING:
            self._release_skip_flag(pos, self.loops)
            self._check_markers_crossed(pos)
        self._arm_engine(pos)
        if emit:
            self._refresh_standby()

    def _release_skip_flag(self, pos: float, loops: List[LoopRegion]) -> None:
        """Re-arm a skipped vamp once the playhead is well past its end."""
        if not self.temp_skip_loop_id:
            return
        skipped = next((l for l in loops if l.id == self.temp_skip_loop_id), None)
        if skipped is None or pos > skipped.end + SKIP_FLAG_RELEASE_S:
            self.temp_skip_loop_id = None
            logger.debug("Cleared skip loop flag - loop re-armed")
            self._emit('loop_skip_cleared')

    def _check_markers_crossed(self, pos: float) -> None:
        """OSC cue_reached for markers the playhead passed since the last tick."""
        last = self._last_cue_pos
        self._last_cue_pos = pos
        if last is None or pos <= last or pos - last > 1.0:
            return
        for marker in self.markers:
            if last < marker.time <= pos:
                self.osc.send_event("cue_reached", id=marker.id, name=marker.name, type="marker")
                if marker.osc_cue:
                    self.osc.send_cue(marker.osc_cue)

    def _invalidate_arm(self) -> None:
        """Forget what the engine was told; the next _arm_engine re-sends everything."""
        with self._arm_lock:
            self._armed_loop_key = "?"
            self._armed_hold_key = "?"
            self._armed_skips = "?"

    def _next_vamp(self, pos: float, loops: List[LoopRegion]) -> Optional[LoopRegion]:
        """Active, non-skipped vamp whose end is nearest ahead of ``pos``."""
        target = None
        for loop in loops:
            if not loop.active or loop.id == self.temp_skip_loop_id:
                continue
            pending_boundary = loop.id == self._armed_loop_id and abs(loop.end - pos) <= CUE_EPSILON_S
            if (loop.end > pos + CUE_EPSILON_S or pending_boundary) and (target is None or loop.end < target.end):
                target = loop
        return target

    def _arm_engine(self, pos: float) -> None:
        """
        Keep the engine armed for what comes next: nearest upcoming vamp
        (arm_loop), nearest upcoming hold (hold_at), active skips
        (set_skip_regions). Only calls the engine when something changed.
        """
        audio = self.audio
        with self._arm_lock:
            # ---- vamp (left alone while the engine is looping)
            if audio.mode != "loop":
                if self._previewing:
                    region = self._preview_region
                    selected = self._selected_loop()
                    cf = selected.crossfade_ms if selected else LOOP_CROSSFADE_MS
                    key = None
                    if region is not None and region[1] > pos + CUE_EPSILON_S:
                        key = ("preview", region[0], region[1], cf, None, 15)
                else:
                    target = self._next_vamp(pos, self.loops)
                    key = None
                    if target is not None:
                        key = (target.id, target.start, target.end, target.crossfade_ms,
                               _engine_count(target.repeat_count), target.entry_fade_ms)
                if key != self._armed_loop_key:
                    if key is None:
                        if self._armed_loop_key is not None:
                            audio.disarm_loop()
                            self._armed_loop_id = None
                    else:
                        logger.debug(f"Arming vamp {key[0]} {key[1]:.3f}-{key[2]:.3f} "
                                     f"(cf={key[3]} count={key[4]} entry={key[5]})")
                        audio.arm_loop(key[1], key[2], crossfade_ms=key[3],
                                       count=key[4], entry_fade_ms=key[5])
                        self._armed_loop_id = None if key[0] == "preview" else key[0]
                    self._armed_loop_key = key

            # ---- hold (left alone while parked on one)
            if not audio.is_held and self.state != PlaybackState.HELD:
                nxt = None
                for hold in self.holds:
                    if hold.time > pos + CUE_EPSILON_S and (nxt is None or hold.time < nxt.time):
                        nxt = hold
                key = (nxt.id, nxt.time) if nxt is not None else None
                # The playhead can sit exactly on the armed hold's sample
                # between audio blocks, before the engine fires it. Keep it
                # armed until the engine reports it (or it is clearly passed).
                armed = self._armed_hold_key
                if (isinstance(armed, tuple) and armed != key
                        and any(h.id == armed[0] for h in self.holds)
                        and armed[1] >= pos - HOLD_PENDING_S):
                    key = armed
                if key != self._armed_hold_key:
                    if key is None:
                        if self._armed_hold_key is not None:
                            audio.clear_hold()
                    else:
                        audio.hold_at(key[1])
                    self._armed_hold_key = key
                    self._armed_hold_id = key[0] if key else None

            # ---- skips
            regions = tuple((s.start, s.end) for s in self.skips if s.active and s.end > s.start)
            if regions != self._armed_skips:
                audio.set_skip_regions(list(regions))
                self._armed_skips = regions

    # =========================================================================
    # DATA PERSISTENCE
    # =========================================================================

    def cleanup(self):
        """Flush pending saves, stop the monitor, close MIDI and release the audio engine."""
        try:
            self.mirror.cleanup()
        except Exception:
            logger.exception("Mirror cleanup failed")
        self._monitor_stop_event.set()
        self.cancel_detection()
        thread = self._monitor_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        try:
            self.midi.close()
        except Exception:
            pass
        if self.audio:
            close = getattr(self.audio, 'close', self.audio.cleanup)
            close()
        self.osc.close()
        self.flush_save()
        self.flush_show_autosave()
        with self._preload_lock:
            self._preload_generation += 1   # drop any queued preload

    def _read_data_file(self):
        """
        Read and parse the data file -> (status, value, raw):
          "ok", dict, bytes | "missing", {}, None |
          "unreadable", OSError, None (locked / no permission, after retries) |
          "corrupt", error, bytes (not UTF-8 JSON, or not an object)
        """
        try:
            raw = read_bytes_with_retry(self.data_file, attempts=LOAD_READ_ATTEMPTS)
        except FileNotFoundError:
            return "missing", {}, None
        except OSError as e:
            return "unreadable", e, None
        try:
            data = json.loads(raw.decode('utf-8-sig'))
        except ValueError as e:   # JSONDecodeError and UnicodeDecodeError
            return "corrupt", e, raw
        if not isinstance(data, dict):
            return "corrupt", ValueError("top-level JSON value is not an object"), raw
        return "ok", data, raw

    def _report_load_problem(self, msg: str) -> None:
        logger.error(msg)
        self.last_load_error = msg
        self._emit('data_load_failed', msg)

    def _load_loop_data(self) -> dict:
        """
        Load saved loop data from disk.

        A corrupt file (not JSON / not an object) is renamed to
        loop_data.json.corrupt-<timestamp>; if it cannot be moved, a copy is
        written under that name instead, and if even that fails the file is
        left alone and saving is disabled. A file that exists but cannot be
        read (locked by antivirus / cloud sync, permissions) is retried
        briefly; then saving is disabled until a later read succeeds, so the
        other songs' data is never overwritten. Problems are logged, stored
        in ``last_load_error`` and emitted as 'data_load_failed'; an empty
        dict is returned.
        """
        data_dir = self.data_dir
        try:
            os.makedirs(data_dir, exist_ok=True)
        except OSError as e:
            logger.error(f"Could not create data directory {data_dir}: {e}")

        status, value, raw = self._read_data_file()
        if status in ("ok", "missing"):
            return value
        if status == "unreadable":
            self._data_read_blocked = str(value)
            self._report_load_problem(
                f"Saved loop data could not be read ({value}). Nothing will be saved until it "
                "can be read, so the existing file is not overwritten. Close programs that may "
                "be using it (antivirus, cloud sync) and load the song again.")
            return {}
        self._quarantine_corrupt(value, raw)
        return {}

    def _quarantine_corrupt(self, error, raw: bytes) -> None:
        """Move a corrupt data file aside (or back it up); block saving if neither works."""
        corrupt_path = unique_backup_path(self.data_file, "corrupt")
        try:
            replace_with_retry(self.data_file, corrupt_path)
            where = f"moved to {os.path.basename(corrupt_path)}"
        except OSError as move_err:
            backup = backup_copy(raw, self.data_file, "corrupt")
            if backup is not None:
                where = (f"could not be moved aside ({move_err}); a copy was saved as "
                         f"{os.path.basename(backup)}")
            else:
                self._data_read_blocked = f"corrupt file could not be backed up ({move_err})"
                where = ("could not be moved aside or backed up, so it will not be "
                         "overwritten and nothing will be saved")
        self._report_load_problem(
            f"Saved loop data is unreadable ({error}); file {where}. Starting with no saved vamps.")

    def _retry_blocked_read(self) -> bool:
        """
        While saving is disabled after a failed read, try reading the file
        again. On success the disk data becomes the base: songs saved this
        session that are not on disk are added; for songs that ARE on disk
        the disk entry wins and the session's version is kept under
        SESSION_COPY_KEY (the song was shown empty, so its edits must not
        replace the real cues). Returns True when saving is possible again.
        """
        if not self._data_read_blocked:
            return True
        status, value, raw = self._read_data_file()
        if status == "unreadable":
            return False
        if status == "corrupt":
            self._data_read_blocked = None
            self._quarantine_corrupt(value, raw)
            if self._data_read_blocked:
                return False
            value = {}
        conflicted = []
        with self._save_lock:
            merged = dict(value)
            for song_id in self._session_song_ids:
                entry = self.loop_data.get(song_id)
                if entry is None:
                    continue
                if song_id in merged and isinstance(merged[song_id], dict):
                    disk_entry = dict(merged[song_id])
                    disk_entry[SESSION_COPY_KEY] = entry
                    merged[song_id] = disk_entry
                    self._conflicted_song_ids.add(song_id)
                    conflicted.append(song_id)
                else:
                    merged[song_id] = entry
            self.loop_data = merged
            self._data_read_blocked = None
            if self._session_song_ids:
                self._save_dirty = True
        logger.info("Saved loop data is readable again; saving resumed")
        if self.current_song_id in conflicted:
            self._report_load_problem(
                f"Saved cues for '{self.current_song_name}' can be read again. Load the song "
                "again to see them; changes made since are kept as a separate copy.")
        return True

    def _adopt_moved_entry(self, path: str, new_id: str) -> bool:
        """
        No entry for this fingerprint: the file may have been re-exported or
        moved. Adopt (and migrate to ``new_id``) the single entry whose
        last_known_path is this path, or else whose last_known_name is this
        file name and whose saved duration matches -- but only if that
        entry's old file no longer exists unchanged. Ambiguous -> no match.
        """
        def _norm(p):
            # macOS hands out NFD file names, Windows NFC; case-insensitive on Windows
            p = unicodedata.normalize('NFC', str(p))
            return os.path.normcase(os.path.normpath(os.path.abspath(p)))

        def _duration_close(entry, tolerance):
            """True/False when the entry has a saved duration, None when it has none."""
            saved = entry.get('duration')
            if saved is None:
                return None
            try:
                return abs(float(saved) - self.song_length) <= tolerance
            except (TypeError, ValueError):
                return False

        def _orphaned(old_id, entry):
            old_path = entry.get('last_known_path')
            if not old_path or not os.path.exists(old_path):
                return True
            return self._get_file_fingerprint(old_path) != old_id

        with self._save_lock:
            entries = [(k, v) for k, v in self.loop_data.items()
                       if k != new_id and isinstance(v, dict)]
            target = _norm(path)
            name = os.path.basename(path)
            by_path = [(k, v) for k, v in entries
                       if v.get('last_known_path') and _norm(v['last_known_path']) == target
                       and _duration_close(v, max(1.0, 0.05 * self.song_length)) is not False]
            how = "path"
            candidates = by_path
            if not by_path:
                how = "name and duration"
                candidates = [(k, v) for k, v in entries
                              if v.get('last_known_name') == name and _duration_close(v, 0.5)]
            if len(candidates) != 1:
                if len(candidates) > 1:
                    logger.warning(f"{len(candidates)} saved entries match '{name}' by {how}; "
                                   "not adopting any (ambiguous)")
                return False
            old_id, entry = candidates[0]
            if not _orphaned(old_id, entry):
                return False
            self.loop_data[new_id] = self.loop_data.pop(old_id)
            self._session_song_ids.add(new_id)
            self._save_dirty = True
        logger.warning(f"Audio file '{name}' changed (fingerprint {old_id} -> {new_id}); "
                       f"migrated its saved cues (matched by {how})")
        self._schedule_save()
        return True

    def save_loop(self, immediate: bool = False) -> None:
        """
        Record the current song's vamps/markers/holds/skips/settings and schedule a write.

        Writes are debounced (SAVE_DEBOUNCE_MS after the last change);
        stop() and cleanup() flush immediately. No-op before a song is loaded
        or when the song could not be fingerprinted.
        """
        if not self.current_song_id:
            return

        with self._save_lock:
            song_id = self.current_song_id
            try:
                settings = self.settings.to_dict()
                if self.pending_tempo_pitch is not None and self._saved_tempo_pitch is not None:
                    # Load-time render still running: the engine reports 1.0/0
                    # meanwhile; keep the song's saved tempo/key on disk.
                    settings['tempo_ratio'], settings['semitones'] = self._saved_tempo_pitch
                entry = {
                    'last_known_name': self.current_song_name,
                    'last_known_path': self.current_song_path,
                    'duration': self.song_length,
                    'loops': [loop.to_dict() for loop in self.loops],
                    'markers': [marker.to_dict() for marker in self.markers],
                    'holds': [hold.to_dict() for hold in self.holds],
                    'skips': [skip.to_dict() for skip in self.skips],
                    'settings': settings,
                }
            except Exception as e:   # e.g. a dict edited on another thread mid-copy
                logger.exception("Could not snapshot song data for saving")
                self.last_save_error = str(e)
                self._emit('save_failed', str(e))
                self._schedule_save()   # retry shortly with a fresh snapshot
                return
            previous = self.loop_data.get(song_id)
            if isinstance(previous, dict):
                for key in PRESERVED_ENTRY_KEYS:
                    if key in previous:
                        entry[key] = previous[key]
            if song_id in self._conflicted_song_ids and isinstance(previous, dict):
                # Disk entry reappeared after this song was shown empty:
                # keep it, store this session's version beside it.
                kept = dict(previous)
                entry.pop(SESSION_COPY_KEY, None)
                kept[SESSION_COPY_KEY] = entry
                entry = kept
            self.loop_data[song_id] = entry
            self._session_song_ids.add(song_id)
            self._save_dirty = True

        if immediate:
            self.flush_save()
        else:
            self._schedule_save()

    def _schedule_save(self) -> None:
        with self._save_lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
            timer = threading.Timer(SAVE_DEBOUNCE_MS / 1000.0, self.flush_save)
            timer.daemon = True
            self._save_timer = timer
            timer.start()

    def flush_save(self) -> bool:
        """Write pending loop data to disk now (no-op if nothing changed). Never raises."""
        with self._save_lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
                self._save_timer = None
            if not self._save_dirty:
                return self.last_save_error is None
            try:
                ok = self._save_loop_data()
            except Exception as e:   # never let a save failure escape into stop()/load_song()
                logger.exception("Unexpected error while saving loop data")
                self.last_save_error = str(e)
                self._emit('save_failed', str(e))
                ok = False
            if ok:
                self._save_dirty = False
                self.last_save_error = None
                self._emit('save_succeeded')
                return True
            return False

    def _save_loop_data(self) -> bool:
        """Atomically write loop data: unique temp file in the same directory, then os.replace()."""
        with self._save_lock:
            if self._data_read_blocked and not self._retry_blocked_read():
                msg = (f"Not saved: the existing loop data file could not be read "
                       f"({self._data_read_blocked}), so it is not overwritten")
                logger.error(msg)
                self.last_save_error = msg
                self._emit('save_failed', msg)
                return False
            try:
                payload = json.dumps(self.loop_data, indent=2)
            except Exception as e:   # TypeError/ValueError, or RuntimeError from a concurrent edit
                logger.error(f"Could not serialise loop data: {e}")
                self.last_save_error = str(e)
                self._emit('save_failed', str(e))
                return False

            try:
                write_text_atomic(self.data_file, payload)
                logger.info(f"Saved loop data ({len(self.loop_data)} songs) to {self.data_file}")
                return True
            except Exception as e:
                logger.error(f"Could not save loop data: {e}")
                self.last_save_error = str(e)
                self._emit('save_failed', str(e))
                return False

    # =========================================================================
    # UTILITY
    # =========================================================================

    format_time = staticmethod(_format_time)

    # =========================================================================
    # MULTI-LOOP (VAMP) MANAGEMENT
    # =========================================================================

    def add_loop(self, start=None, end=None, name=None) -> Optional[LoopRegion]:
        """
        Create a new named loop region (vamp) and select it.

        Args:
            start: Start time (current position if None)
            end: End time (start + 5s if None)
            name: Name for the vamp (auto-generated if None)

        Returns:
            The new LoopRegion, or None if the region is empty/inverted
            (or live mode is on).
        """
        if self._blocked("add_loop"):
            return None
        if start is None:
            start = self.audio.get_position()
        if end is None:
            end = min(start + 5.0, self.song_length) if self.song_length > 0 else start + 5.0
        start = self._clamp_time(start)
        end = self._clamp_time(end)
        if not _long_enough(start, end):
            logger.warning(f"add_loop rejected region {start:.3f}-{end:.3f} "
                           f"(minimum {MIN_LOOP_DURATION}s)")
            return None
        if name is None:
            name = f"{DEFAULT_VAMP_NAME} {len(self.loops) + 1}"

        new_loop = LoopRegion(start, end, name=name)
        self.loops = self.loops + [new_loop]
        self.selected_loop_index = len(self.loops) - 1
        self.loop_start = new_loop.start
        self.loop_end = new_loop.end

        self._push_loop_to_engine(new_loop)
        self._emit_loops_update()
        self.save_loop()
        return new_loop

    def select_loop(self, index):
        """Select a specific loop for editing."""
        if 0 <= index < len(self.loops):
            self.selected_loop_index = index
            loop = self.loops[index]
            self.loop_start = loop.start
            self.loop_end = loop.end
            self._push_loop_to_engine(loop)
            self._emit_loops_update()

    def rename_loop(self, index, new_name):
        """Rename a loop/vamp by index."""
        if self._blocked("rename_loop"):
            return False
        if 0 <= index < len(self.loops):
            self.loops[index].name = new_name
            self._emit_loops_update()
            logger.info(f"Renamed loop {index} to '{new_name}'")
            self.save_loop()
            return True
        return False

    def set_loop_active(self, index, active: bool) -> bool:
        """Enable/disable a vamp without deleting it."""
        if self._blocked("set_loop_active"):
            return False
        if 0 <= index < len(self.loops):
            self.loops[index].active = bool(active)
            self._emit_loops_update()
            self.save_loop()
            return True
        return False

    def delete_selected_loop(self):
        """Delete the currently selected loop."""
        if self._blocked("delete_selected_loop"):
            return False
        if 0 <= self.selected_loop_index < len(self.loops):
            deleted = self.loops[self.selected_loop_index]
            self.loops = [l for l in self.loops if l.id != deleted.id]
            logger.info(f"Deleted loop '{deleted.name}'")
            if deleted.id == self._armed_loop_id:
                self._armed_loop_id = None
            if deleted.id == self.temp_skip_loop_id:
                self.temp_skip_loop_id = None
            if deleted.id == self._looping_loop_id:
                self._looping_loop_id = None

            self.selected_loop_index = min(self.selected_loop_index, len(self.loops) - 1)
            selected = self._selected_loop()
            if selected is not None:
                self.loop_start = selected.start
                self.loop_end = selected.end
                self._push_loop_to_engine(selected)
            elif self.state == PlaybackState.STOPPED:
                self.audio.set_loop_points(0, 0)

            self._emit_loops_update()
            self.save_loop()
            return True
        return False

    def _get_file_fingerprint(self, path: str) -> Optional[str]:
        """
        Generate a unique ID based on file content.
        Reads start/middle/end chunks so it's fast even for large files.
        Returns None (after logging) if the file cannot be read.
        """
        if not os.path.exists(path):
            logger.error(f"Cannot fingerprint missing file: {path}")
            return None

        try:
            file_size = os.path.getsize(path)
            hasher = hashlib.md5()
            with open(path, 'rb') as f:
                hasher.update(str(file_size).encode('utf-8'))
                hasher.update(f.read(4096))
                if file_size > 8192:
                    f.seek(file_size // 2)
                    hasher.update(f.read(4096))
                if file_size > 12288:
                    f.seek(-4096, 2)
                    hasher.update(f.read(4096))
            return hasher.hexdigest()
        except Exception as e:
            logger.error(f"Error generating fingerprint for {path}: {e}")
            return None
