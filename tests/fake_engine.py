"""
In-memory stand-in for ``backend.audio_engine.AudioEngine``.

Implements the section-1 surface of docs/audio-engine-api.md with simple,
deterministic behaviour so StateManager can be tested without a device, a
decoder or wall-clock timing:

- The playhead only moves when a test calls ``advance(seconds)``.
- ``arm_loop`` / ``hold_at`` / ``set_skip_regions`` fire the contract events
  (``loop_entered``, ``loop_cycle``, ``loop_exited``, ``hold_reached``,
  ``skip_executed``, ``song_ended``) synchronously, on the calling thread,
  when the position crosses them.
- Every call is recorded in ``calls`` as ``(method, args, kwargs)`` and every
  fired event in ``events`` as ``(name, payload)``.

Device simulation: ``frames_rendered`` advances by one block on every read
while the transport runs and the stream is alive. ``unplug()`` kills the
stream (frames freeze), ``replug()`` makes the device available again and
``restart_stream()`` reopens it (``device_restored``) or fails
(``device_error``) like the real engine. ``fail_device_change = True`` makes
``set_routing`` / ``set_output_device`` revert and raise RuntimeError.

Cycle semantics: entering a loop fires ``loop_cycle`` 1; each later arrival
at ``loop_out`` either exits (count reached / cut queued) or fires the next
cycle number. ``count=N`` therefore plays the region N extra times.
"""
import os
import wave
from typing import Callable, List, NamedTuple, Optional, Tuple

import numpy as np


class StemInfo(NamedTuple):
    name: str
    gain_db: float
    muted: bool
    solo: bool


class FakeEngine:
    def __init__(self, ffmpeg_path="ffmpeg", device=None, sample_rate=44100,
                 block_size=512, stream_factory=None, default_duration=10.0):
        self.ffmpeg_path = ffmpeg_path
        self.sample_rate = sample_rate
        self.SAMPLE_RATE = sample_rate
        self.block_size = block_size
        self.default_duration = default_duration

        self.calls: List[Tuple[str, tuple, dict]] = []
        self.events: List[Tuple[str, dict]] = []
        self._listeners: List[Callable[[str, dict], None]] = []

        self.song_length = 0.0
        self.current_file_path: Optional[str] = None
        self._raw = None
        self._stems: List[dict] = []

        self._pos = 0.0
        self.is_playing = False
        self.is_paused = False
        self.is_held = False
        self.mode = "transport"

        self._loop = (0.0, 0.0, 20)
        self._armed: Optional[dict] = None
        self._count: Optional[int] = None
        self._cycle = 0
        self._exit: Optional[tuple] = None   # ("cut",) | ("fade", seconds_left)

        self._hold: Optional[float] = None
        self._skips: List[Tuple[float, float]] = []

        self._tempo = (1.0, 0.0)
        self._routing = "stereo"
        self._channel_map = {"mix": (0, 1), "click": (2, 3)}
        self._device = device

        # Device / stream simulation
        self._frames = 0
        self.underflow_count = 0
        self.device_present = True
        self._stream_alive = True
        self.restart_ok_names = None      # None = any present device restarts
        self.fail_device_change = False

    # ------------------------------------------------------------ helpers --

    def _rec(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def calls_to(self, name):
        return [c for c in self.calls if c[0] == name]

    def event_names(self):
        return [n for n, _ in self.events]

    def _fire(self, name, payload):
        payload = dict(payload)
        self.events.append((name, payload))
        for cb in list(self._listeners):
            cb(name, dict(payload))

    def on_event(self, callback):
        self._listeners.append(callback)

    # ------------------------------------------------------------ devices --

    @property
    def frames_rendered(self):
        if self._stream_alive and self.is_playing and not self.is_paused and not self.is_held:
            self._frames += self.block_size
        return self._frames

    def is_stream_active(self):
        return self._stream_alive

    def unplug(self):
        """The interface disappears: the stream dies, frames stop advancing."""
        self.device_present = False
        self._stream_alive = False

    def replug(self):
        self.device_present = True

    def restart_stream(self):
        self._rec("restart_stream")
        if not self.device_present:
            self._stream_alive = False
            self._fire("device_error", {"message": "No usable output device"})
            return False
        self._stream_alive = True
        self._fire("device_restored", {"name": "Fake Output"})
        return True

    def list_output_devices(self):
        return [{"index": 0, "name": "Fake Output", "channels": 2,
                 "default": True, "hostapi": "fake"}]

    def set_output_device(self, index):
        self._rec("set_output_device", index)
        if self.fail_device_change:
            self._fire("device_error", {"message": f"cannot open device {index}"})
            raise RuntimeError(f"cannot open output device {index}; kept the previous one")
        self._device = index

    def get_output_device(self):
        if self._device is None:
            return None
        return {"index": self._device, "name": "Fake Output", "channels": 2,
                "default": True, "hostapi": "fake"}

    def set_routing(self, mode):
        self._rec("set_routing", mode)
        if mode not in ("stereo", "click_right", "multichannel"):
            raise ValueError(f"bad routing mode {mode!r}")
        if self.fail_device_change:
            self._fire("device_error", {"message": f"device cannot do {mode}"})
            raise RuntimeError(f"output device cannot open for {mode!r}; routing reverted")
        self._routing = mode

    def set_channel_map(self, mix=(0, 1), click=(2, 3)):
        self._rec("set_channel_map", mix, click)
        self._channel_map = {"mix": tuple(mix), "click": tuple(click)}

    def get_routing(self):
        return {"mode": self._routing, "channel_map": dict(self._channel_map),
                "output_channels": 2}

    # ------------------------------------------------------------ loading --

    def load_file(self, path, stem_paths=None):
        self._rec("load_file", path, stem_paths)
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        self.stop()
        if path.lower().endswith(".wav"):
            try:
                with wave.open(path, "rb") as wf:
                    channels = wf.getnchannels()
                    rate = wf.getframerate()
                    frames = wf.getnframes()
                    raw = wf.readframes(frames)
            except Exception as e:
                raise ValueError(f"cannot decode {path}: {e}")
            data = np.frombuffer(raw, dtype=np.int16).reshape(-1, channels)
            if channels == 1:
                data = np.repeat(data, 2, axis=1)
            self._raw = data
            self.sample_rate = self.SAMPLE_RATE = rate
            self.song_length = frames / float(rate)
        else:
            raise ValueError(f"cannot decode {path}")
        self.current_file_path = path
        self._stems = [{"name": "mix", "gain_db": 0.0, "muted": False, "solo": False}]
        for name in (stem_paths or {}):
            self._stems.append({"name": name, "gain_db": 0.0,
                                "muted": 'click' not in name.lower(), "solo": False})
        self._tempo = (1.0, 0.0)
        self._armed = None
        self._hold = None
        self._skips = []
        self._pos = 0.0
        return self.song_length, 1.0

    def get_stems(self):
        return [StemInfo(s["name"], s["gain_db"], s["muted"], s["solo"]) for s in self._stems]

    def _stem(self, name):
        for s in self._stems:
            if s["name"] == name:
                return s
        raise KeyError(name)

    def set_stem_gain(self, name, gain_db):
        self._rec("set_stem_gain", name, gain_db)
        self._stem(name)["gain_db"] = float(gain_db)

    def set_stem_mute(self, name, muted):
        self._rec("set_stem_mute", name, muted)
        self._stem(name)["muted"] = bool(muted)

    def set_stem_solo(self, name, solo):
        self._rec("set_stem_solo", name, solo)
        self._stem(name)["solo"] = bool(solo)

    def get_raw_audio_data(self):
        return self._raw

    # -------------------------------------------------------- tempo / key --

    def set_tempo_pitch(self, tempo_ratio=1.0, semitones=0.0):
        self._rec("set_tempo_pitch", tempo_ratio, semitones)
        self._tempo = (float(tempo_ratio), float(semitones))
        self._fire("render_started", {"tempo_ratio": tempo_ratio, "semitones": semitones})
        self._fire("render_done", {"tempo_ratio": tempo_ratio, "semitones": semitones})

    def get_tempo_pitch(self):
        return self._tempo

    # ---------------------------------------------------------- transport --

    def play_transport(self, start_pos=None):
        self._rec("play_transport", start_pos)
        if start_pos is not None:
            self._pos = float(start_pos)
        self.is_playing = True
        self.is_paused = False
        self.is_held = False
        self.mode = "transport"
        self._exit = None

    def pause_transport(self):
        self._rec("pause_transport")
        if self.is_playing:
            self.is_paused = True

    def unpause_transport(self):
        self._rec("unpause_transport")
        if self.is_playing:
            self.is_paused = False

    def seek_transport(self, position):
        self._rec("seek_transport", position)
        was_loop = self.mode == "loop"
        self.mode = "transport"
        self._exit = None
        self.is_held = False
        self._pos = float(position)
        if was_loop:
            self._fire("loop_exited", {"position": self._pos, "reason": "seek"})

    def stop(self):
        self._rec("stop")
        self.is_playing = False
        self.is_paused = False
        self.is_held = False
        self.mode = "transport"
        self._exit = None

    def toggle_play_pause(self):
        self._rec("toggle_play_pause")
        if self.is_playing:
            self.is_paused = not self.is_paused
        else:
            self.play_transport()

    def get_position(self):
        return self._pos

    def is_transport_active(self):
        return self.is_playing and self.mode == "transport"

    def is_loop_active(self):
        return self.is_playing and self.mode == "loop"

    # -------------------------------------------------------------- loops --

    def set_loop_points(self, loop_in, loop_out, crossfade_ms=20):
        self._rec("set_loop_points", loop_in, loop_out, crossfade_ms=crossfade_ms)
        self._loop = (float(loop_in), float(loop_out), int(crossfade_ms))

    def get_loop_points(self):
        return self._loop

    def is_loop_ready(self):
        return True

    def arm_loop(self, loop_in, loop_out, crossfade_ms=20, count=None, entry_fade_ms=15):
        self._rec("arm_loop", loop_in, loop_out, crossfade_ms=crossfade_ms,
                  count=count, entry_fade_ms=entry_fade_ms)
        self._armed = {"start": float(loop_in), "end": float(loop_out),
                       "cf": int(crossfade_ms), "count": count, "entry": entry_fade_ms}
        self._loop = (float(loop_in), float(loop_out), int(crossfade_ms))

    def disarm_loop(self):
        self._rec("disarm_loop")
        self._armed = None

    @property
    def armed_loop(self):
        return self._armed

    @property
    def armed_hold(self):
        return self._hold

    @property
    def skip_regions(self):
        return list(self._skips)

    def start_loop_mode(self, fade_in_ms=15):
        self._rec("start_loop_mode", fade_in_ms)
        lin, lout, _ = self._loop
        if lout <= lin:
            return False
        self._enter_loop(lin, lout, self._armed["count"] if self._armed else None)
        return True

    def _enter_loop(self, lin, lout, count):
        self.mode = "loop"
        self._count = count
        self._cycle = 1
        self._exit = None
        self._armed = None
        self._pos = lin
        self._fire("loop_entered", {"loop_in": lin, "loop_out": lout})
        self._fire("loop_cycle", {"cycle": 1})

    def queue_loop_exit(self, mode="cut", fade_ms=None):
        self._rec("queue_loop_exit", mode, fade_ms=fade_ms)
        if self.mode != "loop":
            return
        if mode == "fade":
            self._exit = ("fade", (fade_ms if fade_ms is not None else 2000) / 1000.0)
        else:
            self._exit = ("cut",)

    def cancel_loop_exit(self):
        self._rec("cancel_loop_exit")
        self._exit = None

    def execute_loop_exit(self):
        self._rec("execute_loop_exit")
        if self.mode == "loop":
            self._leave_loop("cut", self._loop[1])

    def execute_fade_exit(self, fade_ms=None):
        self.queue_loop_exit("fade", fade_ms)

    def get_loop_cycle_position(self):
        if self.mode != "loop":
            return 0.0
        return self._pos - self._loop[0]

    def get_loop_cycle_count(self):
        return self._cycle if self.mode == "loop" else 0

    def _leave_loop(self, reason, position):
        self.mode = "transport"
        self._exit = None
        self._pos = float(position)
        if reason == "fade":
            self.is_playing = False
        self._fire("loop_exited", {"position": self._pos, "reason": reason})

    # -------------------------------------------------------------- holds --

    def hold_at(self, position):
        self._rec("hold_at", position)
        self._hold = float(position)

    def clear_hold(self):
        self._rec("clear_hold")
        self._hold = None

    def release_hold(self):
        self._rec("release_hold")
        if self.is_held:
            self.is_held = False
            self._fire("hold_released", {"position": self._pos})

    # -------------------------------------------------------------- skips --

    def set_skip_regions(self, regions):
        self._rec("set_skip_regions", list(regions))
        self._skips = sorted((float(s), float(e)) for s, e in regions if e > s)

    # ---------------------------------------------------------- lifecycle --

    def cleanup(self):
        self._rec("cleanup")
        self.stop()
        self._raw = None

    def sweep_stale_temp_files(self):
        return 0

    # --------------------------------------------------------- simulation --

    def advance(self, seconds):
        """Move the playhead ``seconds`` forward, firing events as boundaries are crossed."""
        remaining = float(seconds)
        guard = 0
        while remaining > 1e-9 and self.is_playing and not self.is_paused and not self.is_held:
            guard += 1
            if guard > 10000:
                raise RuntimeError("FakeEngine.advance did not converge")
            remaining -= self._step(remaining)
        return self._pos

    def _step(self, remaining):
        pos = self._pos
        events = []
        if self.mode == "loop":
            lin, lout, _ = self._loop
            events.append((max(0.0, lout - pos), "wrap", None))
            if self._exit and self._exit[0] == "fade":
                events.append((max(0.0, self._exit[1]), "fade_done", None))
        else:
            if self.song_length > 0:
                events.append((max(0.0, self.song_length - pos), "end", None))
            if self._armed is not None and self._armed["end"] > pos:
                events.append((self._armed["end"] - pos, "loop_in", None))
            if self._hold is not None and self._hold > pos:
                events.append((self._hold - pos, "hold", None))
            for s, e in self._skips:
                if s > pos:
                    events.append((s - pos, "skip", (s, e)))
                elif s <= pos < e:
                    events.append((0.0, "skip", (s, e)))
        events.sort(key=lambda t: t[0])

        if not events or events[0][0] > remaining:
            self._pos = pos + remaining
            if self._exit and self._exit[0] == "fade":
                self._exit = ("fade", self._exit[1] - remaining)
            return remaining

        dist, kind, data = events[0]
        self._pos = pos + dist
        if self._exit and self._exit[0] == "fade":
            self._exit = ("fade", self._exit[1] - dist)

        if kind == "end":
            self.is_playing = False
            self._fire("song_ended", {})
        elif kind == "loop_in":
            armed = self._armed
            self._loop = (armed["start"], armed["end"], armed["cf"])
            self._enter_loop(armed["start"], armed["end"], armed["count"])
        elif kind == "hold":
            self.is_held = True
            self._hold = None
            self._fire("hold_reached", {"position": self._pos})
        elif kind == "skip":
            s, e = data
            self._pos = e
            self._fire("skip_executed", {"from_pos": s, "to_pos": e})
        elif kind == "wrap":
            lin, lout, _ = self._loop
            if self._exit and self._exit[0] == "cut":
                self._leave_loop("cut", lout)
            elif self._count and self._cycle >= self._count:
                self._leave_loop("count", lout)
            else:
                self._cycle += 1
                self._pos = lin
                self._fire("loop_cycle", {"cycle": self._cycle})
        elif kind == "fade_done":
            self._leave_loop("fade", self._pos)
        return dist
