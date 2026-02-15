"""
State Manager for Loop Station.

Acts as the controller layer between the UI and audio engine.
Manages playback state, monitors playback position, and emits events.

This follows an event-driven architecture:
- UI registers callbacks for events it cares about
- StateManager emits events when state changes
- UI updates in response to events

This decouples the UI from the audio engine, making both easier to test and modify.

Data Model (designed for future cue sheet / show flow support):
- Each song can have multiple named LoopRegions (vamps)
- Each song can have multiple named Markers (cue points)
- Data is saved in a structure that can be wrapped in a show/cue-list later
"""

import os
import sys
import json
import time
import logging
import threading
import uuid
import hashlib
from enum import Enum, auto
from typing import Callable, Optional, Dict, List, Any
from .loop_detector import LoopDetector

# Add parent directory to path for config import
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import (
    DATA_DIR, LOOP_DATA_FILE,
    UI_UPDATE_INTERVAL,
    LOOP_SWITCH_EARLY_MS, EXIT_BOUNDARY_THRESHOLD_MS,
    DEFAULT_VAMP_NAME, DEFAULT_MARKER_NAME,
    FADE_EXIT_DURATION_MS,
    LOOP_CROSSFADE_MS, LOOP_SWITCH_EARLY_MS, FADE_EXIT_DURATION_MS
)
from .audio_engine import AudioEngine

logger = logging.getLogger("LoopStation.StateManager")


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
        self.method = method # "cut" (instant) or "fade" (dip volume)
        self.fade_ms = 500   # Only used if method="fade"

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
        skip = cls(data['start'], data['end'], name=data.get('name', 'Skip'))
        skip.id = data.get('id', str(uuid.uuid4()))
        skip.active = data.get('active', True)
        skip.method = data.get('method', 'cut')
        skip.fade_ms = data.get('fade_ms', 500)
        return skip

class PlaybackState(Enum):
    """Playback state enumeration."""
    STOPPED = auto()
    PLAYING = auto()
    PAUSED = auto()


class LoopRegion:
    """
    A named loop region (vamp) within a song.
    
    Attributes:
        id: Unique identifier
        name: Human-readable name (e.g., "Vamp - Scene 3 transition")
        start: Start time in seconds
        end: End time in seconds
        active: Whether this loop will trigger during playback
    """
    def __init__(self, start, end, name=None):
        self.id = str(uuid.uuid4())
        self.start = start
        self.end = end
        self.name = name or DEFAULT_VAMP_NAME
        self.active = True
        # --- NEW: Advanced Settings ---
        # 1. "Smooth Entry" (Fade-in from transport)
        self.entry_fade_ms = 15  
        
        # 2. "Smooth Loop Seam" (Crossfade at loop point)
        self.crossfade_ms = LOOP_CROSSFADE_MS
        
        # 3. "Rhythm Correction" (Early switch offset)
        self.early_switch_ms = LOOP_SWITCH_EARLY_MS
        
        # 4. "Fade Exit" (Duration when fading out)
        self.exit_fade_ms = FADE_EXIT_DURATION_MS
        
        # --- Cue Notes & Tags ---
        # Maps tag name -> notes text for that tag
        # e.g. {"Director": "Cross SL after dialogue", "Lighting": "Fade to blue"}
        self.tag_notes = {}

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
            'tag_notes': self.tag_notes,
        }
    
    @classmethod
    def from_dict(cls, data):
        """Deserialize from JSON storage."""
        loop = cls(data['start'], data['end'], name=data.get('name', DEFAULT_VAMP_NAME))
        loop.id = data.get('id', str(uuid.uuid4()))
        loop.active = data.get('active', True)
        # Load new settings (with fallbacks for old files)
        loop.entry_fade_ms = data.get('entry_fade_ms', 15)
        loop.crossfade_ms = data.get('crossfade_ms', LOOP_CROSSFADE_MS)
        loop.early_switch_ms = data.get('early_switch_ms', LOOP_SWITCH_EARLY_MS)
        loop.exit_fade_ms = data.get('exit_fade_ms', FADE_EXIT_DURATION_MS)
        # Tag notes: new format or migrate from old
        if 'tag_notes' in data:
            loop.tag_notes = data['tag_notes']
        else:
            # Migrate from old separate notes + tags fields
            old_tags = data.get('tags', [])
            old_notes = data.get('notes', '')
            loop.tag_notes = {}
            for tag in old_tags:
                loop.tag_notes[tag] = ''
            if old_notes and old_tags:
                loop.tag_notes[old_tags[0]] = old_notes
            elif old_notes:
                loop.tag_notes['Other'] = old_notes
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
    """
    def __init__(self, time_pos, name=None, color=None):
        self.id = str(uuid.uuid4())
        self.name = name or DEFAULT_MARKER_NAME
        self.time = time_pos
        self.color = color  # None = use default COLOR_MARKER
        self.tag_notes = {}  # {"Director": "notes...", "Tech": "notes..."}
    
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
            'tag_notes': self.tag_notes,
        }
    
    @classmethod
    def from_dict(cls, data):
        """Deserialize from JSON storage."""
        marker = cls(data['time'], name=data.get('name', DEFAULT_MARKER_NAME), color=data.get('color'))
        marker.id = data.get('id', str(uuid.uuid4()))
        # Tag notes: new format or migrate from old
        if 'tag_notes' in data:
            marker.tag_notes = data['tag_notes']
        else:
            old_tags = data.get('tags', [])
            old_notes = data.get('notes', '')
            marker.tag_notes = {}
            for tag in old_tags:
                marker.tag_notes[tag] = ''
            if old_notes and old_tags:
                marker.tag_notes[old_tags[0]] = old_notes
            elif old_notes:
                marker.tag_notes['Other'] = old_notes
        return marker


class StateManager:
    """
    Manages application state and coordinates between UI and audio engine.
    
    Responsibilities:
    - Owns the AudioEngine instance
    - Manages playback state
    - Runs the monitor thread
    - Handles loop mode transitions
    - Manages named vamps (loops) and markers (cue points)
    - Persists loop/marker data to disk
    - Emits callbacks for UI updates
    
    Event System:
    - Register callbacks with: state.on('event_name', callback_function)
    - Events are emitted automatically when state changes
    
    Available Events:
    - 'position_update': (position: float, is_loop_mode: bool)
    - 'state_change': (state: PlaybackState)
    - 'loop_mode_enter': ()
    - 'loop_mode_exit': (exit_position: float)
    - 'loop_ready': ()
    - 'song_loaded': (song_name: str, duration: float)
    - 'song_ended': ()
    - 'loop_points_changed': (loop_in: float, loop_out: float)
    - 'loops_changed': (loops_list, selected_index)
    - 'markers_changed': (markers_list)
    - 'detection_started': ()
    - 'detection_complete': (candidates_list)
    """
    
    def __init__(self, ffmpeg_path: str = "ffmpeg"):
        """
        Initialize the state manager.
        
        Args:
            ffmpeg_path: Path to ffmpeg executable
        """
        self.ffmpeg_path = ffmpeg_path
        
        # Audio engine
        self.audio = AudioEngine(ffmpeg_path=ffmpeg_path)
        
        # Named vamps (loop regions)
        self.loops: List[LoopRegion] = [] 
        self.selected_loop_index: int = -1
        
        # Named markers (cue points)
        self.markers: List[Marker] = []
        
        # Fix for the "Loop Exit" bug:
        # Instead of self.loop_enabled = False, we track a specific loop ID to skip
        self.temp_skip_loop_id: Optional[str] = None

        # Current song
        self.current_song_path: str = ""
        self.current_song_name: str = ""
        self.song_length: float = 0.0
        self.sync_ratio: float = 1.0
        
        # Playback state
        self.state = PlaybackState.STOPPED
        
        # Loop configuration (backward compat - tracks selected loop)
        self.loop_start: float = 0.0
        self.loop_end: float = 0.0
        self.loop_enabled: bool = True
        
        # Exit queue for smooth loop exit
        self.exit_queue_active: bool = False
        self.exit_fade_mode: bool = False  # True = fade out, False = cut to transport
        self.exit_fade_ms: int = FADE_EXIT_DURATION_MS
        self._prev_cycle_pos: float = 0.0
        
        # Monitor thread
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_stop: bool = False
        
        # Callbacks for UI updates (event-driven architecture)
        self._callbacks: Dict[str, List[Callable]] = {
            'position_update': [],      # (position, is_loop_mode)
            'state_change': [],         # (PlaybackState)
            'loop_mode_enter': [],      # ()
            'loop_mode_exit': [],       # (exit_position)
            'loop_ready': [],           # ()
            'song_loaded': [],          # (song_name, duration)
            'song_ended': [],           # ()
            'loop_points_changed': [],  # (loop_in, loop_out)
            'detection_complete': [],   # (candidates list)
            'detection_started': [],    # ()
            'loops_changed': [],        # (loops_list, selected_index)
            'markers_changed': [],      # (markers_list)
            'skips_changed': [],          # (skips_list)
            'cut_detection_complete': [], # (candidates_list)
            'loop_skip_queued': [],       # (loop_name) - vamp skip from transport
            'loop_skip_cleared': [],      # () - skip flag cleared, loop re-armed
        }
        self.skips: List[SkipRegion] = []
        self._last_skip_time = 0.0


        # Load saved data
        self.loop_data = self._load_loop_data()
        
        logger.info("StateManager initialized")
    
    # =========================================================================
    # EVENT SYSTEM
    # =========================================================================
    
    def on(self, event: str, callback: Callable) -> None:
        """
        Register a callback for an event.
        
        Args:
            event: Event name (see class docstring for available events)
            callback: Function to call when event occurs
        """
        if event in self._callbacks:
            self._callbacks[event].append(callback)
        else:
            logger.warning(f"Unknown event: {event}. Available: {list(self._callbacks.keys())}")
    
    def off(self, event: str, callback: Callable) -> None:
        """
        Unregister a callback for an event.
        
        Args:
            event: Event name
            callback: Function to remove
        """
        if event in self._callbacks and callback in self._callbacks[event]:
            self._callbacks[event].remove(callback)
    
    def _emit(self, event: str, *args) -> None:
        """Emit an event to all registered callbacks."""
        for callback in self._callbacks.get(event, []):
            try:
                callback(*args)
            except Exception as e:
                logger.error(f"Error in callback for {event}: {e}")
    
    # =========================================================================
    # SONG LOADING
    # =========================================================================
    
    def load_song(self, path: str) -> bool:
        """Load a song and its associated loop data."""
        logger.info(f"=== UI: Loading song: {os.path.basename(path)} ===")
        
        self.stop()
        
        try:
            self.song_length, self.sync_ratio = self.audio.load_file(path)
            self.current_song_path = path
            self.current_song_name = os.path.basename(path)
            
            # GENERATE UNIQUE ID
            self.current_song_id = self._get_file_fingerprint(path)
            logger.info(f"Song ID: {self.current_song_id}")

            # Clear previous state
            self.loops.clear()
            self.markers.clear()
            self.selected_loop_index = -1
            self.temp_skip_loop_id = None
            
            # STRICT LOAD: Only look for the unique ID
            if self.current_song_id and self.current_song_id in self.loop_data:
                logger.info("Found saved data for this audio file.")
                saved = self.loop_data[self.current_song_id]
                self._load_song_data(saved)
            else:
                logger.info("No saved data found for this specific audio file.")
                # Default to whole song
                self.loop_start = 0.0
                self.loop_end = self.song_length
            
            # Emit updates to UI
            self.loop_enabled = True
            self._emit('song_loaded', self.current_song_name, self.song_length)
            self._emit('loop_points_changed', self.loop_start, self.loop_end)
            self._emit('markers_changed', self.markers)
            self._emit_loops_update()
            
            return True
            
        except Exception as e:
            logger.error(f"Error loading song: {e}")
            return False

    def _load_song_data(self, saved):
        """
        Load saved song data (loops, markers) from the data dict.
        Handles both old format (just start/end) and new format (loops + markers).
        
        Args:
            saved: Dict from loop_data.json for this song
        """
        # New format: has 'loops' key
        if 'loops' in saved:
            for loop_data in saved['loops']:
                loop = LoopRegion.from_dict(loop_data)
                self.loops.append(loop)
            
            if self.loops:
                self.selected_loop_index = 0
                self.loop_start = self.loops[0].start
                self.loop_end = self.loops[0].end
                self.audio.set_loop_points(self.loop_start, self.loop_end)
            
            # Load markers
            for marker_data in saved.get('markers', []):
                marker = Marker.from_dict(marker_data)
                self.markers.append(marker)
            
            # Load Skips
            self.skips.clear()
            if 'skips' in saved:
                for skip_data in saved['skips']:
                    self.skips.append(SkipRegion.from_dict(skip_data))
            logger.info(f"Loaded {len(self.loops)} vamps, {len(self.markers)} markers, {len(self.skips)} skips")
        
        # Old format: just 'start' and 'end'
        elif 'start' in saved and 'end' in saved:
            self.loop_start = saved['start']
            self.loop_end = saved['end']
            logger.info(f"Loaded legacy loop points: IN={self.loop_start:.3f}s OUT={self.loop_end:.3f}s")
            
            new_loop = LoopRegion(self.loop_start, self.loop_end, name="Vamp 1")
            self.loops.append(new_loop)
            self.selected_loop_index = 0
            self.audio.set_loop_points(self.loop_start, self.loop_end)
    
    def get_raw_audio_for_waveform(self):
        """Get raw audio data for waveform generation."""
        return self.audio.get_raw_audio_data()
    
    # =========================================================================
    # PLAYBACK CONTROLS
    # =========================================================================
    
    def play(self) -> None:
        """Start or resume playback."""
        if not self.current_song_path:
            logger.warning("No song loaded")
            return
        
        logger.info(f"UI: Toggle play -> PLAY (was {self.state.name.lower()})")
        
        if self.state == PlaybackState.PAUSED:
            self.audio.toggle_play_pause()
        else:
            self.audio.play_transport(0.0)
        
        self.state = PlaybackState.PLAYING
        self._monitor_stop = False
        self._start_monitor()
        self._emit('state_change', self.state)

    def play_from(self, position: float) -> None:
        """
        Start playback from a specific timestamp.
        Used for previewing loop candidates and jumping to markers.
        """
        self.stop()
        self.audio.play_transport(start_pos=position)
        self.state = PlaybackState.PLAYING
        self._monitor_stop = False
        self._start_monitor()
        self._emit('state_change', self.state)

    def pause(self) -> None:
        """Pause playback."""
        if self.state == PlaybackState.PLAYING:
            logger.info("UI: PAUSE")
            self.audio.toggle_play_pause()
            self.state = PlaybackState.PAUSED
            self._emit('state_change', self.state)
    
    def toggle_play_pause(self) -> None:
        """Toggle between play and pause."""
        if self.state == PlaybackState.PLAYING:
            self.pause()
        else:
            self.play()
    
    def stop(self) -> None:
        """Stop playback."""
        logger.info("UI: STOP button pressed")
        self._monitor_stop = True
        self.exit_queue_active = False
        self.exit_fade_mode = False
        self.temp_skip_loop_id = None
        self._prev_cycle_pos = 0.0
        self.audio.stop()
        self.state = PlaybackState.STOPPED
        self._emit('state_change', self.state)
    
    def seek(self, position: float) -> None:
        """
        Seek to a position.
        
        Args:
            position: Time in seconds
        """
        position = max(0, min(position, self.song_length - 0.01))
        self.temp_skip_loop_id = None
        self.audio.seek_transport(position)
        
        if self.state == PlaybackState.STOPPED:
            self.state = PlaybackState.PAUSED
            self._emit('state_change', self.state)
    
    def nudge(self, amount: float) -> None:
        """
        Nudge position by a small amount.
        
        Args:
            amount: Seconds to nudge (positive or negative)
        """
        pos = self.audio.get_position()
        self.seek(pos + amount)
    
    # =========================================================================
    # LOOP CONTROLS
    # =========================================================================
    
    def set_loop_in(self, time_val: Optional[float] = None) -> None:
        """
        Set loop in point.
        
        Args:
            time_val: Time in seconds (current position if None)
        """
        if time_val is None:
            time_val = self.audio.get_position()
        
        self.loop_start = time_val
        
        if self.loop_end > self.loop_start:
            self.audio.set_loop_points(self.loop_start, self.loop_end)
            if self.loops and 0 <= self.selected_loop_index < len(self.loops):
                self.loops[self.selected_loop_index].start = self.loop_start
            elif not self.loops:
                self.loops.append(LoopRegion(self.loop_start, self.loop_end))
                self.selected_loop_index = 0
            self._emit('loop_points_changed', self.loop_start, self.loop_end)
    
    def set_loop_out(self, time_val: Optional[float] = None) -> None:
        """
        Set loop out point.
        
        Args:
            time_val: Time in seconds (current position if None)
        """
        if time_val is None:
            time_val = self.audio.get_position()
        
        self.loop_end = time_val
        
        if self.loop_end > self.loop_start:
            self.audio.set_loop_points(self.loop_start, self.loop_end)
            if self.loops and 0 <= self.selected_loop_index < len(self.loops):
                self.loops[self.selected_loop_index].end = self.loop_end
            elif not self.loops:
                self.loops.append(LoopRegion(self.loop_start, self.loop_end))
                self.selected_loop_index = 0
            self._emit('loop_points_changed', self.loop_start, self.loop_end)
    
    def set_loop_points(self, start: float, end: float) -> None:
        """
        Set both loop points at once.
        
        Args:
            start: Start time in seconds
            end: End time in seconds
        """
        self.loop_start = start
        self.loop_end = end
        
        if end > start:
            self.audio.set_loop_points(start, end)
            
            if self.loops and 0 <= self.selected_loop_index < len(self.loops):
                self.loops[self.selected_loop_index].start = start
                self.loops[self.selected_loop_index].end = end
            elif not self.loops:
                new_loop = LoopRegion(start, end)
                self.loops.append(new_loop)
                self.selected_loop_index = 0
            
            self._emit('loop_points_changed', start, end)
            self._emit_loops_update()
    
    def adjust_loop_in(self, amount: float) -> None:
        """Adjust loop in point by amount in seconds."""
        new_val = max(0, min(self.loop_start + amount, self.song_length))
        self.set_loop_in(new_val)
    
    def adjust_loop_out(self, amount: float) -> None:
        """Adjust loop out point by amount in seconds."""
        new_val = max(0, min(self.loop_end + amount, self.song_length))
        self.set_loop_out(new_val)
    
    def save_loop(self) -> None:
        """Save current loops and markers to disk using unique file ID."""
        if not self.current_song_id: return
        
        self.loop_data[self.current_song_id] = {
            'last_known_name': self.current_song_name,
            'last_known_path': self.current_song_path,
            'loops': [loop.to_dict() for loop in self.loops],
            'markers': [marker.to_dict() for marker in self.markers],
            'skips': [skip.to_dict() for skip in self.skips], # Add this
        }
        self._save_loop_data()
        logger.info(f"Saved data for {self.current_song_name} (ID: {self.current_song_id})")

    def queue_exit(self, fade_mode=False, fade_ms=None):
        """
        Exit the current loop at the next boundary, or skip the upcoming loop
        if still in transport mode.
        
        Args:
            fade_mode: If True, fade out instead of cutting to transport
            fade_ms: Fade duration in ms (only used if fade_mode=True)
        """
        if self.audio.mode == "loop":
            # Already in loop mode - queue exit at next boundary
            current_engine_in = self.audio.loop_in
            
            for loop in self.loops:
                if abs(loop.start - current_engine_in) < 0.01:
                    self.temp_skip_loop_id = loop.id
                    break
            
            self.exit_fade_mode = fade_mode
            if fade_ms is not None:
                self.exit_fade_ms = fade_ms
            
            mode_str = f"FADE ({self.exit_fade_ms}ms)" if fade_mode else "CUT"
            logger.info(f"UI: EXIT LOOP queued ({mode_str}) - Skipping loop {self.temp_skip_loop_id}")
            self.exit_queue_active = True
            self._prev_cycle_pos = self.audio.get_loop_cycle_position()
        elif self.state == PlaybackState.PLAYING:
            # In transport mode - skip the next upcoming (or current) active loop
            pos = self.audio.get_position()
            target_loop = None
            
            # Find the nearest active loop we're inside or approaching
            # (loops may not be sorted, so check all and pick closest)
            for loop in self.loops:
                if not loop.active:
                    continue
                if loop.id == self.temp_skip_loop_id:
                    continue
                # Must be a loop we haven't passed yet
                if pos < loop.end:
                    if target_loop is None or loop.start < target_loop.start:
                        target_loop = loop
            
            if target_loop:
                self.temp_skip_loop_id = target_loop.id
                logger.info(f"UI: SKIP VAMP queued - Skipping loop '{target_loop.name}' (id={target_loop.id})")
                self._emit('loop_skip_queued', target_loop.name)
    
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
        """
        Add a named marker / cue point at the given time.
        
        Args:
            time_pos: Position in seconds (current position if None)
            name: Name for the marker (auto-generated if None)
        """
        if time_pos is None:
            time_pos = self.audio.get_position()
        
        if name is None:
            # Auto-generate name: "Cue 1", "Cue 2", etc.
            existing_count = len(self.markers)
            name = f"{DEFAULT_MARKER_NAME} {existing_count + 1}"
        
        marker = Marker(time_pos, name=name)
        self.markers.append(marker)
        
        # Keep markers sorted by time
        self.markers.sort(key=lambda m: m.time)
        
        self._emit('markers_changed', self.markers)
        logger.info(f"Added marker '{name}' at {time_pos:.3f}s")
        self.save_loop()
        return marker
    
    def rename_marker(self, marker_id, new_name):
        """Rename a marker by ID."""
        for marker in self.markers:
            if marker.id == marker_id:
                marker.name = new_name
                self._emit('markers_changed', self.markers)
                self.save_loop()
                return True
        return False
    
    def delete_marker(self, marker_id):
        """Delete a marker by ID."""
        self.markers = [m for m in self.markers if m.id != marker_id]
        self._emit('markers_changed', self.markers)
        self.save_loop()
    
    def jump_to_marker(self, marker_id):
        """Seek to a marker's position and start playing."""
        for marker in self.markers:
            if marker.id == marker_id:
                logger.info(f"Jumping to marker '{marker.name}' at {marker.time:.3f}s")
                self.play_from(marker.time)
                return True
        return False
    
    def jump_to_next_marker(self):
        """Jump to the next marker after current position."""
        pos = self.audio.get_position()
        for marker in self.markers:
            if marker.time > pos + 0.1:  # Small buffer to avoid re-triggering same marker
                self.play_from(marker.time)
                return True
        return False
    
    def jump_to_prev_marker(self):
        """Jump to the previous marker before current position."""
        pos = self.audio.get_position()
        prev_marker = None
        for marker in self.markers:
            if marker.time < pos - 0.5:  # Must be at least 0.5s back
                prev_marker = marker
            else:
                break
        
        if prev_marker:
            self.play_from(prev_marker.time)
            return True
        return False
    
    # =========================================================================
    # CUE NOTES & TAGS
    # =========================================================================
    
    def set_item_tag_note(self, item_id, tag, note_text):
        """
        Set (or create) a tag with its notes on a marker or loop region.
        
        Args:
            item_id: UUID string of the marker or loop
            tag: Tag name (e.g. "Director")
            note_text: Notes content for this tag
        """
        for marker in self.markers:
            if marker.id == item_id:
                marker.tag_notes[tag] = note_text
                self._emit('markers_changed', self.markers)
                self.save_loop()
                return True
        
        for loop in self.loops:
            if loop.id == item_id:
                loop.tag_notes[tag] = note_text
                self._emit_loops_update()
                self.save_loop()
                return True
        
        return False
    
    def remove_item_tag(self, item_id, tag):
        """
        Remove a tag (and its notes) from a marker or loop region.
        
        Args:
            item_id: UUID string of the marker or loop
            tag: Tag name to remove
        """
        for marker in self.markers:
            if marker.id == item_id:
                marker.tag_notes.pop(tag, None)
                self._emit('markers_changed', self.markers)
                self.save_loop()
                return True
        
        for loop in self.loops:
            if loop.id == item_id:
                loop.tag_notes.pop(tag, None)
                self._emit_loops_update()
                self.save_loop()
                return True
        
        return False
    
    def get_timeline_items(self):
        """
        Get all markers and vamps as a unified, time-sorted list.
        Returns list of tuples: (time, type_str, object)
        where type_str is 'marker' or 'vamp'.
        """
        items = []
        for marker in self.markers:
            items.append((marker.time, 'marker', marker))
        for loop in self.loops:
            items.append((loop.start, 'vamp', loop))
        items.sort(key=lambda x: x[0])
        return items
    
    # =========================================================================
    # POSITION AND STATE QUERIES
    # =========================================================================
    
    def get_position(self) -> float:
        """Get current playback position in seconds."""
        return self.audio.get_position()
    
    def get_visual_position(self) -> float:
        """Get position adjusted for waveform sync."""
        pos = self.audio.get_position()
        return pos / self.sync_ratio if self.sync_ratio else pos
    
    def is_playing(self) -> bool:
        """Check if currently playing (not stopped or paused)."""
        return self.state == PlaybackState.PLAYING
    
    def is_in_loop_region(self) -> bool:
        pos = self.audio.get_position()
        for loop in self.loops:
            if loop.active and loop.start <= pos < loop.end:
                return True
        return False
    
    # --- SKIP MANAGEMENT ---

    def add_skip(self, start, end, method="cut"):
        """Add a new skip region."""
        if end <= start: return
        new_skip = SkipRegion(start, end, method=method)
        self.skips.append(new_skip)
        # Keep sorted by start time
        self.skips.sort(key=lambda x: x.start)
        self.save_loop()
        self._emit('skips_changed', self.skips)
        return new_skip

    def delete_skip(self, skip_id):
        self.skips = [s for s in self.skips if s.id != skip_id]
        self.save_loop()
        self._emit('skips_changed', self.skips)

    def toggle_skip_active(self, skip_id):
        for s in self.skips:
            if s.id == skip_id:
                s.active = not s.active
                self.save_loop()
                self._emit('skips_changed', self.skips)
                break

    def run_smart_cut_detection(self, start_time, end_time):
        """Run analysis to find beat-aligned cuts."""
        if self.audio.raw_audio_data is None: return

        self._emit('detection_started')
        
        def _worker():
            try:
                detector = LoopDetector(self.audio.raw_audio_data, self.audio.SAMPLE_RATE)
                # Call the NEW method we added to LoopDetector
                candidates = detector.find_smart_cuts(start_time, end_time)
                # Emit a specific event for cut candidates
                self._emit('cut_detection_complete', candidates) 
            except Exception as e:
                logger.error(f"Cut detection failed: {e}")
                self._emit('cut_detection_complete', [])

        threading.Thread(target=_worker, daemon=True).start()

    # =========================================================================
    # MONITOR THREAD
    # =========================================================================
    
    def _start_monitor(self) -> None:
        """Start the monitor thread if not already running."""
        if self._monitor_thread is None or not self._monitor_thread.is_alive():
            self._monitor_thread = threading.Thread(target=self._monitor, daemon=True)
            self._monitor_thread.start()
    
    def _monitor(self) -> None:
        """
        Monitor thread that watches playback and handles transitions.
        
        This runs in a background thread and:
        1. Emits position updates for the UI
        2. Scans self.loops to find the active loop region
        3. Detects when to switch to loop mode
        4. Handles the exit queue (temp_skip_loop_id) logic
        """
        logger.debug("Monitor thread started")
        last_update = 0
        last_pos_log = 0
        
        while not self._monitor_stop:
            now = time.time()
            
            # =========================================================
            # 1. LOOP MODE (Currently looping)
            # =========================================================
            if self.audio.mode == "loop":
                if now - last_pos_log > 2.0:
                    last_pos_log = now
                if self.audio.is_loop_active():
                    # Emit position updates
                    if now - last_update > UI_UPDATE_INTERVAL:
                        pos = self.audio.get_position()
                        visual_pos = pos / self.sync_ratio if self.sync_ratio else pos
                        self._emit('position_update', visual_pos, True)
                        
                        if now - last_pos_log > 1.0:
                            cycle_pos = self.audio.get_loop_cycle_position()
                            logger.debug(f"[LOOP] LOOP MODE: pos={pos:.3f}s cycle_pos={cycle_pos:.3f}s")
                            last_pos_log = now
                        
                        last_update = now
                    
                    # Handle exit queue
                    if self.exit_queue_active:
                        self._check_exit_boundary()
                else:
                    # Loop channel stopped unexpectedly (could be fade exit completing)
                    if self.exit_fade_mode:
                        logger.info("Fade exit completed")
                        self.exit_fade_mode = False
                        self.audio.mode = "transport"
                        self.audio.is_playing = False
                        self.state = PlaybackState.STOPPED
                        self._emit('state_change', self.state)
                        self._emit('loop_mode_exit', self.loop_end)
                        break
                    else:
                        logger.warning("Loop channel stopped unexpectedly")
                        self.stop()
                        self._emit('song_ended')
                        break
                
                time.sleep(0.01)
                continue
            
            # =========================================================
            # 2. TRANSPORT MODE (Normal playback)
            # =========================================================
            if self.state == PlaybackState.PLAYING:
                if self.audio.is_transport_active():
                    pos = self.audio.get_position()
                    
                    if now - last_pos_log > 2.0:
                        for idx, lp in enumerate(self.loops):
                            sel = " [SELECTED]" if idx == self.selected_loop_index else ""
                            skip = " [SKIPPED]" if lp.id == self.temp_skip_loop_id else ""
                    
                    # --- NEW: SKIP LOGIC ---
                    # Check cooldown to prevent skip loops (2 seconds buffer)
                    if now - self._last_skip_time > 2.0:
                        for skip in self.skips:
                            if not skip.active: 
                                continue
                                
                            # If we are INSIDE a skip region
                            if skip.start <= pos < skip.end:
                                logger.info(f"Entered Skip Region '{skip.name}' ({skip.start:.2f}-{skip.end:.2f})")
                                
                                # Execute Jump
                                fade_ms = skip.fade_ms if skip.method == "fade" else 0
                                self.audio.perform_skip(skip.end, fade_out_ms=fade_ms)
                                
                                # Update internal state so we don't glitch UI
                                self.audio.transport_offset = skip.end
                                self._last_skip_time = now
                                break

                    # Emit position updates
                    if now - last_update > UI_UPDATE_INTERVAL:
                        visual_pos = pos / self.sync_ratio if self.sync_ratio else pos
                        self._emit('position_update', visual_pos, False)
                        
                        if now - last_pos_log > 1.0:
                            logger.debug(f"[TRANSPORT] pos={pos:.3f}s / {self.song_length:.3f}s")
                            last_pos_log = now
                        
                        last_update = now
                    
                    # --- RESET SKIP FLAG LOGIC ---
                    if self.temp_skip_loop_id:
                        skipped_loop = next((l for l in self.loops if l.id == self.temp_skip_loop_id), None)
                        if skipped_loop:
                            if pos > skipped_loop.end + 2.0 or pos < skipped_loop.start:
                                self.temp_skip_loop_id = None
                                logger.debug("Cleared skip loop flag - loop re-armed")
                                self._emit('loop_skip_cleared')
                    
                    # --- SCAN FOR LOOPS ---
                    target_loop = None
                    
                    for loop in self.loops:
                        if not loop.active:
                            continue
                        if loop.id == self.temp_skip_loop_id:
                            continue
                        if loop.start <= pos < loop.end:
                            target_loop = loop
                            break
                    
                    if target_loop:
                        # SYNC ENGINE
                        if abs(self.audio.loop_in - target_loop.start) > 0.001:
                            logger.debug(f"Monitor: Syncing engine to loop {target_loop.start:.2f}")
                            self.audio.set_loop_points(target_loop.start, target_loop.end, 
                                                    crossfade_ms=target_loop.crossfade_ms)
                        
                        # CRITICAL FIX: Calculate exact distance to boundary
                        distance_to_boundary = target_loop.end - pos
                        
                        # Convert to milliseconds
                        distance_ms = distance_to_boundary * 1000
                        
                        # Account for monitor thread delay (UI_UPDATE_INTERVAL)
                        # We need to switch BEFORE the next check
                        safety_margin = (UI_UPDATE_INTERVAL * 1000) + 10  # +10ms for processing
                        
                        # Dynamic threshold: switch when we're within one monitor cycle
                        if distance_ms <= safety_margin:
                            logger.info(f"[ENTRY] Switching to loop (distance={distance_ms:.1f}ms)")
                            self._switch_to_loop_mode()
                    
                else:
                    # Song ended
                    logger.info("Song ended naturally")
                    self.stop()
                    self._emit('song_ended')
                    break
            
            time.sleep(0.01)
        
        logger.debug("Monitor thread exiting")

    def _switch_to_loop_mode(self) -> None:
        """
        Transition from transport playback to the seamless loop engine.
        Called by the monitor thread when the playhead reaches a loop boundary.
        """
        pos = self.audio.get_position()
        logger.info(f">>> SWITCHING TO LOOP MODE at pos={pos:.3f}s (l_end={self.audio.loop_out:.3f}s) <<<")
        
        # 1. Get custom settings for the current loop
        # We need to find which loop we are actually entering to get its specific fade setting
        current_loop_region = None
        if 0 <= self.selected_loop_index < len(self.loops):
            # Optimistic check: are we entering the selected loop?
            current_loop_region = self.loops[self.selected_loop_index]
        
        # Fallback: If for some reason we aren't in the selected loop, find the right one
        # (This handles edge cases where user might have changed selection while playing)
        if not current_loop_region or abs(current_loop_region.start - self.audio.loop_in) > 0.1:
            for loop in self.loops:
                if abs(loop.start - self.audio.loop_in) < 0.1:
                    current_loop_region = loop
                    break
        
        # 2. Determine Entry Fade Duration
        # Default to 15ms if we can't find the loop object, otherwise use user preference
        entry_fade = current_loop_region.entry_fade_ms if current_loop_region else 15

        # 3. Execute Switch
        if self.audio.is_loop_ready():
            # Pass the custom fade-in duration to the engine
            success = self.audio.start_loop_mode(fade_in_ms=entry_fade)
            
            if success:
                self._emit('loop_mode_enter')
        else:
            # Fail-safe: If RAM loop isn't ready, seek transport back to start
            # This causes a gap/click, but keeps the rhythm going
            logger.warning(f"Loop not ready, falling back to transport seek at pos={pos:.3f}s")
            self.audio.play_transport(self.audio.loop_in)

    def _check_exit_boundary(self) -> None:
        """Check if we should execute loop exit."""
        cycle_pos = self.audio.get_loop_cycle_position()
        time_to_boundary = self.audio.loop_duration - cycle_pos
        
        # Detect wrap-around: previous was near end, current is near start
        wrapped = (
            self._prev_cycle_pos > self.audio.loop_duration * 0.8 and 
            cycle_pos < self.audio.loop_duration * 0.2
        )
        
        # Log approach to boundary
        if time_to_boundary < 0.2:
            logger.debug(f"[EXIT-WAIT] Exit queued - time to boundary: {time_to_boundary*1000:.1f}ms (prev={self._prev_cycle_pos:.3f}s)")
        
        # Exit when near boundary or wrapped
        threshold = EXIT_BOUNDARY_THRESHOLD_MS / 1000.0
        if time_to_boundary < threshold or wrapped:
            if wrapped:
                logger.info("⮑ Exit boundary detected via wrap-around")
            else:
                logger.info("⮑ Exit boundary reached - executing exit")
            
            # Choose exit mode
            if self.exit_fade_mode:
                self.audio.execute_fade_exit(self.exit_fade_ms)
            else:
                self.audio.execute_loop_exit()
            
            self.exit_queue_active = False
            self.loop_enabled = False
            self._prev_cycle_pos = 0
            
            if not self.exit_fade_mode:
                self._emit('loop_mode_exit', self.loop_end)
            # For fade mode, the monitor loop detects when the channel stops
        else:
            self._prev_cycle_pos = cycle_pos
    
    # =========================================================================
    # DATA PERSISTENCE
    # =========================================================================
    def cleanup(self):
        """Pass the cleanup signal down to the audio engine."""
        if self.audio:
            self.audio.cleanup()
            
    def _load_loop_data(self) -> dict:
        """Load saved loop data from disk."""
        if not os.path.exists(DATA_DIR):
            os.makedirs(DATA_DIR, exist_ok=True)
        
        if os.path.exists(LOOP_DATA_FILE):
            try:
                with open(LOOP_DATA_FILE, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load loop data: {e}")
        return {}
    
    def _save_loop_data(self) -> None:
        """Save loop data to disk."""
        try:
            if not os.path.exists(DATA_DIR):
                os.makedirs(DATA_DIR, exist_ok=True)
            
            with open(LOOP_DATA_FILE, 'w') as f:
                json.dump(self.loop_data, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save loop data: {e}")
    
    # =========================================================================
    # UTILITY
    # =========================================================================
    
    @staticmethod
    def format_time(seconds: float) -> str:
        """Format seconds as M:SS.ms string."""
        return f"{int(seconds // 60)}:{seconds % 60:05.2f}"
    
    # =========================================================================
    # LOOP DETECTOR
    # =========================================================================

    def run_loop_detection(self, start_time, end_time):
        """Run loop detection in a background thread."""
        if self.audio.raw_audio_data is None:
            return

        self._emit('detection_started')
        
        def _worker():
            try:
                detector = LoopDetector(self.audio.raw_audio_data, self.audio.SAMPLE_RATE)
                candidates = detector.find_loops(start_time, end_time)
                self._emit('detection_complete', candidates)
            except Exception as e:
                logger.error(f"Detection failed: {e}")
                self._emit('detection_complete', [])

        threading.Thread(target=_worker, daemon=True).start()

    # =========================================================================
    # MULTI-LOOP (VAMP) MANAGEMENT
    # =========================================================================

    def add_loop(self, start=None, end=None, name=None):
        """
        Create a new named loop region (vamp).
        
        Args:
            start: Start time (current position if None)
            end: End time (start + 5s if None)
            name: Name for the vamp (auto-generated if None)
        """
        if start is None:
            start = self.audio.get_position()
        if end is None:
            end = min(start + 5.0, self.song_length)
        if name is None:
            name = f"{DEFAULT_VAMP_NAME} {len(self.loops) + 1}"
        
        new_loop = LoopRegion(start, end, name=name)
        self.loops.append(new_loop)
        self.selected_loop_index = len(self.loops) - 1
        
        self._emit_loops_update()
        self._sync_audio_engine(new_loop)
        self.save_loop()

    def select_loop(self, index):
        """Select a specific loop for editing."""
        if 0 <= index < len(self.loops):
            self.selected_loop_index = index
            loop = self.loops[index]
            self._sync_audio_engine(loop)
            self._emit_loops_update()

    def update_selected_loop(self, start=None, end=None):
        """Update the currently selected loop points."""
        if self.selected_loop_index < 0 or not self.loops:
            if start is not None:
                self.add_loop(start, end)
            return

        loop = self.loops[self.selected_loop_index]
        
        if start is not None:
            loop.start = start
        if end is not None:
            loop.end = end
        
        if loop.start >= loop.end:
            return 

        self._sync_audio_engine(loop)
        self._emit_loops_update()
        self.save_loop()

    def rename_loop(self, index, new_name):
        """Rename a loop/vamp by index."""
        if 0 <= index < len(self.loops):
            self.loops[index].name = new_name
            self._emit_loops_update()
            logger.info(f"Renamed loop {index} to '{new_name}'")
            self.save_loop()

    def delete_selected_loop(self):
        """Delete the currently selected loop."""
        if 0 <= self.selected_loop_index < len(self.loops):
            deleted = self.loops.pop(self.selected_loop_index)
            logger.info(f"Deleted loop '{deleted.name}'")
            self.selected_loop_index = max(0, len(self.loops) - 1)
            self._emit_loops_update()
            
            if not self.loops:
                self.audio.set_loop_points(0, 0)
            self.save_loop()

    def _sync_audio_engine(self, loop: LoopRegion):
        """Helper to tell AudioEngine about the current target loop."""
        self.loop_start = loop.start
        self.loop_end = loop.end
        self.audio.set_loop_points(loop.start, loop.end, crossfade_ms=loop.crossfade_ms)
        self._emit('loop_points_changed', loop.start, loop.end)

    def _emit_loops_update(self):
        """Notify UI about loop changes."""
        self._emit('loops_changed', self.loops, self.selected_loop_index)
        
        if 0 <= self.selected_loop_index < len(self.loops):
            l = self.loops[self.selected_loop_index]
            self._emit('loop_points_changed', l.start, l.end)

    def _get_file_fingerprint(self, path: str) -> str:
        """
        Generate a unique ID based on file content.
        Reads start/middle/end chunks so it's fast even for large files.
        """
        if not os.path.exists(path):
            return None
            
        try:
            file_size = os.path.getsize(path)
            hasher = hashlib.md5()
            
            with open(path, 'rb') as f:
                # 1. Add file size to hash (fastest unique check)
                hasher.update(str(file_size).encode('utf-8'))
                
                # 2. Read first 4KB (Header)
                hasher.update(f.read(4096))
                
                # 3. Read middle 4KB (if file is big enough)
                if file_size > 8192:
                    f.seek(file_size // 2)
                    hasher.update(f.read(4096))
                    
                # 4. Read last 4KB (Footer)
                if file_size > 12288:
                    f.seek(-4096, 2)
                    hasher.update(f.read(4096))
                    
            return hasher.hexdigest()
        except Exception as e:
            logger.error(f"Error generating fingerprint: {e}")

            return None
