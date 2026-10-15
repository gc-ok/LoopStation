"""
Pure, device-free mixing core for Loop Station.

``MixerCore`` owns the sample-accurate transport. It holds every stem as an
int16 ``(frames, 2)`` array (normally a memmap of a decoded WAV) plus float
gain state, and ``render(frames)`` produces one float32 block of output.
Everything that has to be sample-accurate happens inside ``render``:

* skip regions (jump at the exact region start),
* armed loop entry (switch to the prebuilt loop body so that body sample 0
  lands exactly on ``loop_out``),
* loop cycling with cycle counting and count-based auto exit,
* queued "cut" exits (continue the song seamlessly from the loop end),
* fade exits (linear gain ramp, then stopped),
* holds (stop exactly at a sample, hold the state, silence until release),
* manual-entry fade-in, pause/stop, end of song,
* a short declick crossfade on every discontinuity (loop edits included),
* gain/mute/solo ramps over the declick length,
* routing (stereo / click_right / multichannel), applied last.

All positions handed to and from the core are **source seconds** (the time
axis of the original file). Internally the core works in frames of the
*active* buffers, which are shorter or longer than the source when a tempo
render is in use; ``_scale`` converts between the two.

Events are appended to ``self.events`` (a ``queue.Queue``) from inside
``render`` and from the control methods. They are never delivered from here;
``AudioEngine`` drains the queue on a worker thread so user callbacks never
run on the audio thread.

The core is thread-safe: ``render`` and every control method take ``lock``.
Loop bodies are built outside the lock so the audio callback is never
starved by a long numpy job, and they are NEVER built inside ``render``:
a pending tempo swap carries prebuilt bodies for its own buffers; if those
are stale when the callback wants to apply the swap, the swap is deferred
and the bodies are rebuilt on a long-lived worker thread (``render`` only
sets an Event; it never starts a thread).

``_buf_gen`` is bumped every time the active buffers change; code that
builds bodies outside the lock rechecks it before installing them.
"""

import logging
import math
import queue
import threading
from typing import NamedTuple

import numpy as np

try:
    from config import MIN_LOOP_DURATION
except ImportError:  # pragma: no cover - core used without the app config
    MIN_LOOP_DURATION = 0.5

logger = logging.getLogger("LoopStation.MixerCore")

ROUTING_MODES = ("stereo", "click_right", "multichannel")

_INT16_SCALE = np.float32(1.0 / 32768.0)

# Safety valve: at most this many zero-length events are handled per block
# before the core gives up on the block (prevents a spin if state is bad).
_MAX_EVENTS_PER_BLOCK = 64

# How often a body build is retried when the buffers or the loop points
# change while it runs (each retry means another thread won a race).
_MAX_BUILD_RETRIES = 20


class StemInfo(NamedTuple):
    name: str          # "mix" is the primary track; "click" is special
    gain_db: float
    muted: bool
    solo: bool


def is_click_stem(name):
    """A stem whose name contains "click" is the click stem."""
    return "click" in str(name).lower()


def db_to_linear(gain_db):
    return float(10.0 ** (float(gain_db) / 20.0))


def build_loop_body(audio, start_sample, end_sample, crossfade_samples):
    """
    Build the seamless loop body for audio[start:end].

    Keep exactly ``end - start`` frames so musical periods never shrink.
    Blend the first ``cf`` frames with the continuation after ``end``;
    this joins the preceding loop tail to the head without advancing the
    head's time axis. At EOF, extend the final sample for that short fade.
    Body sample ``i`` corresponds to song sample ``start + i``.

    Args:
        audio: int16 array of shape (n, channels) or (n,)
        start_sample, end_sample: loop region [start, end)
        crossfade_samples: requested crossfade length (clamped to a third of
            the region)

    Returns:
        (body_int16, crossfade_samples_used)
    """
    n = end_sample - start_sample
    if n <= 0:
        raise ValueError("Loop region is empty")

    cf = max(0, min(int(crossfade_samples), n // 3))
    if cf == 0:
        body = np.array(audio[start_sample:end_sample], dtype=np.int16)
        return body, 0

    body = np.array(audio[start_sample:end_sample], dtype=np.float32)
    fade_out = np.linspace(1.0, 0.0, cf, dtype=np.float32)
    fade_in = 1.0 - fade_out
    if body.ndim > 1:
        fade_out = fade_out[:, None]
        fade_in = fade_in[:, None]

    tail = np.empty_like(body[:cf])
    tail[:] = audio[end_sample - 1]
    available = min(cf, len(audio) - end_sample)
    if available > 0:
        tail[:available] = audio[end_sample:end_sample + available]
    head = np.asarray(audio[start_sample:start_sample + cf], dtype=np.float32)
    body[:cf] = tail * fade_out + head * fade_in

    body = np.rint(np.clip(body, -32768, 32767)).astype(np.int16)
    return body, cf


class _Stem:
    __slots__ = ("name", "source", "active", "gain_db", "gain_lin",
                 "muted", "solo", "body", "is_click",
                 "g_cur", "g_target", "g_step", "g_left")

    def __init__(self, name, data):
        self.name = name
        self.source = data      # int16 (frames, 2), original tempo
        self.active = data      # buffer currently played (source or render)
        self.gain_db = 0.0
        self.gain_lin = 1.0
        self.muted = False
        self.solo = False
        self.body = None        # int16 loop body built from ``active``
        self.is_click = is_click_stem(name)
        # Applied gain (linear, mute/solo included) ramps toward the target
        # over the declick length so gain/mute/solo changes never click.
        self.g_cur = None       # None until the first _update_gains
        self.g_target = 0.0
        self.g_step = 0.0
        self.g_left = 0         # frames left in the current ramp


class _PendingSwap:
    """A scheduled tempo/pitch hot-swap with bodies prebuilt for its buffers."""
    __slots__ = ("buffers", "bodies", "loop_key", "tempo", "semitones", "n")

    def __init__(self, buffers, bodies, loop_key, tempo, semitones):
        self.buffers = buffers      # {name: int16 (frames, 2)}
        self.bodies = bodies        # _build_bodies() result for ``buffers`` or None
        self.loop_key = loop_key    # (in, out, cf_ms) the bodies were built for
        self.tempo = tempo
        self.semitones = semitones
        self.n = len(next(iter(buffers.values())))


class MixerCore:
    """Sample-accurate transport + mixer. See the module docstring."""

    def __init__(self, sample_rate=44100, declick_ms=10.0):
        self.sample_rate = int(sample_rate)
        self.declick_frames = max(0, int(round(float(declick_ms) / 1000.0 * self.sample_rate)))
        self.lock = threading.RLock()
        self.events = queue.Queue()

        self._stems = {}            # name -> _Stem, insertion ordered
        self._n_src = 0             # frames of the source buffers
        self._n_active = 0          # frames of the active buffers
        self._tempo_ratio = 1.0
        self._semitones = 0.0

        # routing
        self._routing = "stereo"
        self._channel_map = {"mix": (0, 1), "click": (2, 3)}

        # transport (active frames)
        self._pos = 0
        self._playing = False
        self._paused = False
        self._held = False
        self._frames_since_jump = 0

        # loop (points in source seconds, frames derived from them)
        self._loop_in = 0.0
        self._loop_out = 0.0
        self._crossfade_ms = 20
        self._loop_ready = False
        self._installed_key = None  # (in, out, cf_ms) of the bodies in use
        self._loop_in_f = 0
        self._loop_out_f = 0
        self._cf = 0
        self._body_len = 0
        self._in_loop = False
        self._loop_pos = 0
        self._cycles = 0            # completed passes since entry
        self._automatic_entry = False
        self._armed = False
        self._loop_count = None
        self._entry_fade_ms = 15
        self._exit_queued = None    # None | "cut" | "fade"
        self._entry_min_frame = 0   # suppress re-entry right after an exit

        # fades (frames)
        self._fadein_total = 0
        self._fadein_done = 0
        self._fadeout_total = 0
        self._fadeout_done = 0

        # hold
        self._hold_src = None
        self._hold_f = None

        # skips: source seconds and active frames (sorted by start)
        self._skips_src = []
        self._skips_f = []

        # declick tail (output of the pre-discontinuity state, faded out
        # under the new state which fades in)
        self._tail_mix = None
        self._tail_click = None
        self._tail_idx = 0
        n = self.declick_frames
        self._ramp_in = (np.arange(1, n + 1, dtype=np.float32) / max(1, n))[:, None] if n else None

        # pending tempo/pitch hot-swap (_PendingSwap), applied at the next
        # render once its bodies match the current loop points
        self._pending_swap = None
        self._buf_gen = 0           # bumped whenever the active buffers change
        self._rebuild_running = False
        # Long-lived worker that rebuilds stale pending-swap bodies. Started
        # from swap_buffers (never from render); render only sets the event.
        self._rebuild_event = threading.Event()
        self._rebuild_thread = None
        self._shutdown = False

        # monotonic count of frames rendered (never reset; device watchdog)
        self._frames_rendered = 0
        self.output_peak = 0.0
        self.clipped_blocks = 0

    # ------------------------------------------------------------ helpers --

    def _emit(self, _event, /, **payload):
        self.events.put((_event, payload))

    def _scale(self):
        """Source frames per active frame (1.0 when no render is active)."""
        if self._n_active <= 0 or self._n_src <= 0:
            return 1.0
        return self._n_src / self._n_active

    def _to_frame(self, src_sec):
        return int(round(float(src_sec) * self.sample_rate / self._scale()))

    def _to_sec(self, frame):
        return float(frame) * self._scale() / self.sample_rate

    def _active(self):
        return bool(self._stems) and self._playing and not self._paused and not self._held

    def _env_value(self):
        """Current scalar gain of the entry/exit fade envelopes."""
        g = 1.0
        if self._fadein_total and self._fadein_done < self._fadein_total:
            g *= self._fadein_done / self._fadein_total
        if self._fadeout_total:
            g *= max(0.0, 1.0 - self._fadeout_done / self._fadeout_total)
        return g

    # -------------------------------------------------------------- stems --

    @staticmethod
    def _check_buffers(buffers, what):
        lengths = {len(a) for a in buffers.values()}
        if len(lengths) != 1:
            raise ValueError(f"{what} must have equal length, got {sorted(lengths)}")
        n = lengths.pop()
        if n <= 0:
            raise ValueError(f"{what} are empty")
        for name, a in buffers.items():
            if a.ndim != 2 or a.shape[1] != 2 or a.dtype != np.int16:
                raise ValueError(f"Stem {name!r} must be int16 (frames, 2), got {a.dtype} {a.shape}")
        return n

    def set_stems(self, stems, active=None, tempo_ratio=1.0, semitones=0.0):
        """
        Replace all stems. ``stems`` maps name -> int16 (frames, 2) array;
        every array must have the same length. Resets the transport.

        ``active`` (optional, same names, equal lengths) installs already
        rendered tempo/key buffers for ``tempo_ratio``/``semitones``: the
        result is the state a completed ``swap_buffers`` leaves at position
        0 (``source`` = ``stems``, ``active`` = renders, positions mapped
        through ``n_source / n_active``).
        """
        if not stems:
            self.clear_stems()
            return
        n = self._check_buffers(stems, "Stems")
        n_act = n
        if active is not None:
            if set(active) != set(stems):
                raise ValueError("set_stems: active stem names differ from the stems")
            n_act = self._check_buffers(active, "Active buffers")
        else:
            tempo_ratio, semitones = 1.0, 0.0
        with self.lock:
            old = self._stems
            new = {}
            for name, a in stems.items():
                s = _Stem(name, a)
                # The primary file is normally a complete mix. Components
                # are opt-in; an additional click remains audible/routable.
                if "mix" in stems and name != "mix" and not s.is_click:
                    s.muted = True
                prev = old.get(name)
                if prev is not None:
                    s.gain_db, s.gain_lin, s.muted, s.solo = prev.gain_db, prev.gain_lin, prev.muted, prev.solo
                if active is not None:
                    s.active = active[name]
                new[name] = s
            self._stems = new
            self._update_gains(snap=True)
            self._n_src = n
            self._n_active = n_act
            self._tempo_ratio = float(tempo_ratio)
            self._semitones = float(semitones)
            self._pending_swap = None
            self._buf_gen += 1
            self._reset_transport_state()

    def clear_stems(self):
        with self.lock:
            self._stems = {}
            self._n_src = 0
            self._n_active = 0
            self._tempo_ratio = 1.0
            self._semitones = 0.0
            self._pending_swap = None
            self._buf_gen += 1
            self._reset_transport_state()

    def _reset_transport_state(self):
        self._pos = 0
        self._playing = False
        self._paused = False
        self._held = False
        self._frames_since_jump = 0
        self._loop_ready = False
        self._installed_key = None
        self._in_loop = False
        self._loop_pos = 0
        self._cycles = 0
        self._automatic_entry = False
        self.output_peak = 0.0
        self.clipped_blocks = 0
        self._armed = False
        self._exit_queued = None
        self._entry_min_frame = 0
        self._body_len = 0
        self._fadein_total = self._fadein_done = 0
        self._fadeout_total = self._fadeout_done = 0
        self._hold_src = None
        self._hold_f = None
        self._skips_src = []
        self._skips_f = []
        self._tail_mix = self._tail_click = None
        self._tail_idx = 0

    def get_stems(self):
        with self.lock:
            return [StemInfo(s.name, s.gain_db, s.muted, s.solo) for s in self._stems.values()]

    def get_stem_buffers(self):
        """{name: source int16 array} of the loaded stems."""
        with self.lock:
            return {name: s.source for name, s in self._stems.items()}

    def set_stem_gain(self, name, gain_db):
        s = self._stems.get(name)
        if s is None:
            return
        gain_db = float(gain_db)
        s.gain_lin = db_to_linear(gain_db)
        s.gain_db = gain_db

    def set_stem_mute(self, name, muted):
        s = self._stems.get(name)
        if s is not None:
            s.muted = bool(muted)

    def set_stem_solo(self, name, solo):
        s = self._stems.get(name)
        if s is not None:
            s.solo = bool(solo)

    def _update_gains(self, snap=False):
        """
        Retarget each stem's applied gain from gain/mute/solo (lock held).
        A changed target starts a linear ramp over ``declick_frames`` that
        ``_read_span`` advances; with ``snap`` (or no declick, or nothing
        audible) the applied gain jumps to the target.
        """
        stems = self._stems.values()
        any_solo = any(s.solo for s in stems)
        n = self.declick_frames
        snap = snap or n <= 0 or not self._active()
        for s in stems:
            t = 0.0 if (s.muted or (any_solo and not s.solo)) else float(s.gain_lin)
            if snap or s.g_cur is None:
                s.g_cur, s.g_target, s.g_step, s.g_left = t, t, 0.0, 0
            elif t != s.g_target:
                s.g_target = t
                s.g_step = (t - s.g_cur) / n
                s.g_left = n

    @property
    def n_source_frames(self):
        return self._n_src

    @property
    def n_active_frames(self):
        return self._n_active

    @property
    def song_length(self):
        return self._n_src / self.sample_rate if self._n_src else 0.0

    @property
    def frames_rendered(self):
        """Total frames produced by ``render`` (monotonic; never reset)."""
        return self._frames_rendered

    # ------------------------------------------------------------ routing --

    def set_routing(self, mode):
        if mode not in ROUTING_MODES:
            raise ValueError(f"Unknown routing mode {mode!r}")
        with self.lock:
            self._routing = mode

    def set_channel_map(self, mix=(0, 1), click=(2, 3)):
        mix = tuple(int(c) for c in mix)
        click = tuple(int(c) for c in click)
        if not mix or not click or min(mix + click) < 0:
            raise ValueError("Channel map entries must be non-negative channel indices")
        with self.lock:
            self._channel_map = {"mix": mix, "click": click}

    def get_routing(self):
        with self.lock:
            return {"mode": self._routing,
                    "channel_map": dict(self._channel_map),
                    "output_channels": self.output_channels}

    @property
    def output_channels(self):
        if self._routing == "multichannel":
            top = max(self._channel_map["mix"] + self._channel_map["click"])
            return max(2, top + 1)
        return 2

    # ---------------------------------------------------------- transport --

    @property
    def is_playing(self):
        return self._playing

    @property
    def is_paused(self):
        return self._paused

    @property
    def is_held(self):
        return self._held

    @property
    def mode(self):
        return "loop" if self._in_loop else "transport"

    @property
    def frames_since_jump(self):
        return self._frames_since_jump

    @property
    def tempo_ratio(self):
        return self._tempo_ratio

    @property
    def semitones(self):
        return self._semitones

    def play(self, start_pos=None):
        """Start (or resume) transport playback, optionally from ``start_pos``."""
        with self.lock:
            if not self._stems:
                return
            if start_pos is not None:
                self._jump_to(self._clamp_frame(self._to_frame(start_pos)), reason="seek")
            elif self._in_loop and self._paused:
                self.unpause()
                return
            else:
                self._capture_tail()
                self._leave_hold_state()
                if self._pos >= self._n_active:
                    self._pos = 0
                    self._entry_min_frame = 0
            self._playing = True
            self._paused = False
            self._frames_since_jump = 0

    def pause(self):
        with self.lock:
            if self._playing and not self._paused and not self._held:
                self._capture_tail()
                self._paused = True

    def unpause(self):
        with self.lock:
            if self._playing and self._paused:
                self._capture_tail()
                self._paused = False
                self._frames_since_jump = 0

    def toggle_play_pause(self):
        with self.lock:
            if self._held:
                self.release_hold()
            elif self._playing and not self._paused:
                self.pause()
            elif self._playing and self._paused:
                self.unpause()
            else:
                self.play()

    def seek(self, position):
        """Seek (source seconds). Exits loop mode and any hold."""
        with self.lock:
            if not self._stems:
                return
            self._jump_to(self._clamp_frame(self._to_frame(position)), reason="seek")
            if not self._playing:
                # Old engine semantics: a seek while stopped leaves the
                # transport parked (paused) at the new position.
                self._playing = True
                self._paused = True

    def stop(self):
        with self.lock:
            self._capture_tail()
            self._playing = False
            self._paused = False
            self._held = False
            self._in_loop = False
            self._exit_queued = None
            self._fadein_total = self._fadein_done = 0
            self._fadeout_total = self._fadeout_done = 0
            self._pos = 0
            self._frames_since_jump = 0
            self._entry_min_frame = 0

    def _clamp_frame(self, frame):
        if self._n_active <= 0:
            return 0
        return max(0, min(int(frame), self._n_active - 1))

    def _jump_to(self, frame, reason):
        """Discontinuous move of the playhead (declicked)."""
        self._capture_tail()
        if self._in_loop:
            self._in_loop = False
            self._exit_queued = None
            self._emit("loop_exited", position=self._to_sec(frame), reason=reason)
        self._held = False
        self._pos = frame
        self._frames_since_jump = 0
        self._entry_min_frame = 0
        self._fadein_total = self._fadein_done = 0
        self._fadeout_total = self._fadeout_done = 0

    def _leave_hold_state(self):
        self._held = False

    def get_position(self, latency_frames=0):
        """Playhead in source seconds (see module docstring)."""
        with self.lock:
            if not self._stems:
                return 0.0
            comp = 0
            if latency_frames > 0 and self._active():
                comp = min(int(latency_frames), self._frames_since_jump)
            if self._in_loop and self._body_len > 0:
                lp = (self._loop_pos - comp) % self._body_len
                frame = self._loop_in_f + lp
            else:
                frame = max(0, self._pos - comp)
            return self._to_sec(frame)

    # -------------------------------------------------------------- loops --

    def _reported_loop(self):
        """
        Loop points to report: while looping, the points of the bodies that
        are actually playing (a set_loop_points still building keeps the
        old ones audible); otherwise the requested points.
        """
        if self._in_loop and self._installed_key is not None:
            return self._installed_key
        return self._loop_key()

    @property
    def loop_in(self):
        return self._reported_loop()[0]

    @property
    def loop_out(self):
        return self._reported_loop()[1]

    @property
    def crossfade_ms(self):
        return self._reported_loop()[2]

    @property
    def loop_duration(self):
        """Duration of one body cycle in source seconds."""
        with self.lock:
            return self._to_sec(self._body_len) if self._loop_ready else max(0.0, self._loop_out - self._loop_in)

    def is_loop_ready(self):
        return self._loop_ready

    def get_loop_points(self):
        with self.lock:
            return tuple(self._reported_loop())

    def _loop_frames_for(self, n_active, loop_in, loop_out, crossfade_ms):
        """Frame positions of a loop in a buffer of ``n_active`` frames."""
        scale = self._n_src / n_active if (n_active and self._n_src) else 1.0
        in_f = int(round(loop_in * self.sample_rate / scale))
        out_f = int(round(loop_out * self.sample_rate / scale))
        in_f = max(0, min(in_f, n_active - 1))
        out_f = max(in_f + 1, min(out_f, n_active))
        cf_req = int(round(crossfade_ms / 1000.0 * self.sample_rate))
        return in_f, out_f, cf_req

    def _build_bodies(self, buffers, loop_in, loop_out, crossfade_ms):
        """Build {name: body} for ``buffers`` (outside the lock)."""
        n_active = len(next(iter(buffers.values())))
        in_f, out_f, cf_req = self._loop_frames_for(n_active, loop_in, loop_out, crossfade_ms)
        bodies = {}
        cf_used = None
        for name, buf in buffers.items():
            body, cf = build_loop_body(buf, in_f, out_f, cf_req)
            bodies[name] = body
            cf_used = cf
        return bodies, in_f, out_f, cf_used

    def _loop_key(self):
        return (self._loop_in, self._loop_out, self._crossfade_ms)

    def _loop_too_short(self, loop_in, loop_out):
        """True for an empty loop or one shorter than MIN_LOOP_DURATION
        (after clamping to the song end), which the core rejects."""
        end = min(loop_out, self.song_length) if self._n_src else loop_out
        return end - loop_in < MIN_LOOP_DURATION - 1e-9

    def set_loop_points(self, loop_in, loop_out, crossfade_ms=20):
        """
        Store loop points (source seconds) and build the loop bodies now.

        Bodies are built outside the lock for the active buffers AND, when a
        tempo swap is pending, for the pending buffers (attached to the swap
        so ``render`` only installs prebuilt data). If the active buffers
        change while building (a swap applied by the callback), the build is
        retried against the new buffers.

        A loop with ``loop_out <= loop_in`` or shorter than
        ``MIN_LOOP_DURATION`` (``config``) is rejected: the points are stored
        but no loop is ready (and a running loop is left, reason "seek").
        Editing the loop that is playing keeps the playhead on the same song
        sample (or restarts at the new loop_in when it falls outside) and
        declicks the body change.
        """
        loop_in = float(loop_in)
        loop_out = float(loop_out)
        crossfade_ms = int(crossfade_ms)
        key = (loop_in, loop_out, crossfade_ms)
        with self.lock:
            changed = key != self._loop_key()
            self._loop_in, self._loop_out, self._crossfade_ms = key
            if changed:
                self._entry_min_frame = 0
            stems = self._stems
            if not stems or self._loop_too_short(loop_in, loop_out):
                if stems and loop_out > loop_in:
                    logger.warning(f"Loop {loop_in:.3f}-{loop_out:.3f} s is shorter than "
                                   f"{MIN_LOOP_DURATION} s; rejected")
                self._loop_ready = False
                self._installed_key = None
                self._body_len = 0
                for s in stems.values():
                    s.body = None
                if self._in_loop:
                    self._jump_to(self._clamp_frame(self._loop_song_frame()), reason="seek")
                return

        for _attempt in range(_MAX_BUILD_RETRIES):
            with self.lock:
                if self._stems is not stems or key != self._loop_key():
                    return  # superseded
                gen = self._buf_gen
                buffers = {name: s.active for name, s in stems.items()}
                pending = self._pending_swap

            built = self._build_bodies(buffers, *key)
            pending_built = None
            if pending is not None:
                pending_built = self._build_bodies(pending.buffers, *key)

            with self.lock:
                if self._stems is not stems or key != self._loop_key():
                    return  # superseded while building
                if self._buf_gen != gen:
                    continue  # buffers swapped while building: rebuild
                self._install_bodies(*built, key)
                if pending is not None and self._pending_swap is pending:
                    pending.bodies, pending.loop_key = pending_built, key
                return

        logger.error("set_loop_points: buffers kept changing; loop not ready")
        with self.lock:
            if self._stems is stems and key == self._loop_key():
                self._loop_ready = False
                self._installed_key = None
                self._body_len = 0
                if self._in_loop:
                    self._jump_to(self._clamp_frame(self._loop_song_frame()), reason="seek")

    def _install_bodies(self, bodies, in_f, out_f, cf, key):
        """
        Install freshly built bodies for the active buffers (lock held).
        In a running loop the old body's tail is captured first (declick)
        and the playhead keeps the same song sample; if that sample is not
        inside the new loop it restarts at the new loop_in.
        """
        body_len = len(next(iter(bodies.values())))
        remap = self._in_loop and self._body_len > 0 and body_len > 0
        if remap:
            self._capture_tail()
            song_f = self._loop_song_frame()
        for name, s in self._stems.items():
            s.body = bodies.get(name)
        if remap:
            lp = song_f - in_f
            if not 0 <= lp < body_len:
                lp = 0
                self._frames_since_jump = 0
            self._loop_pos = lp
        self._install_loop_frames(in_f, out_f, cf, body_len, key)

    def _install_loop_frames(self, in_f, out_f, cf, body_len, key):
        self._loop_in_f, self._loop_out_f, self._cf = in_f, out_f, cf
        self._body_len = body_len
        self._loop_ready = body_len > 0
        self._installed_key = key if body_len > 0 else None
        if self._in_loop:
            if body_len <= 0:
                self._jump_to(self._clamp_frame(self._loop_song_frame()), reason="seek")
            elif self._loop_pos > body_len:
                self._loop_pos %= body_len

    def _loop_song_frame(self):
        """Song frame equivalent to the current loop body position."""
        return self._loop_in_f + self._loop_pos

    def arm_loop(self, loop_in, loop_out, crossfade_ms=20, count=None, entry_fade_ms=15):
        """Arm: the renderer enters the loop when the playhead reaches loop_out."""
        if count is not None and int(count) <= 0:
            count = None
        with self.lock:
            same = (float(loop_in), float(loop_out), int(crossfade_ms)) == \
                   (self._loop_in, self._loop_out, self._crossfade_ms) and self._loop_ready
        if not same:
            self.set_loop_points(loop_in, loop_out, crossfade_ms)
        with self.lock:
            self._loop_count = None if count is None else int(count)
            # Stored for compatibility only: an armed entry is sample-
            # continuous (body sample 0 == song sample loop_out), so a fade-in
            # would only dip a signal that is already playing.
            self._entry_fade_ms = int(entry_fade_ms)
            if not self._in_loop:
                self._armed = True

    def disarm_loop(self):
        with self.lock:
            self._armed = False

    def enter_loop(self, fade_in_ms=15, count=None):
        """
        Enter loop mode now (manual). Returns False if no body is ready.
        Loops until exit unless ``count`` is given (a count from an earlier
        ``arm_loop`` is NOT inherited).
        """
        if count is not None and int(count) <= 0:
            count = None
        with self.lock:
            if not self._stems or not self._loop_ready or self._body_len <= 0:
                return False
            if self._in_loop:
                return True
            lo = self._loop_in_f
            hi = self._loop_out_f
            if self._active() and lo + self._cf <= self._pos <= hi:
                # Same samples as the song (past the blended head): no
                # discontinuity.
                self._loop_pos = self._pos - lo
            else:
                self._capture_tail()
                self._loop_pos = (self._pos - lo) % self._body_len
                self._frames_since_jump = 0
                fade = int(round(fade_in_ms / 1000.0 * self.sample_rate))
                self._fadein_total, self._fadein_done = max(0, fade), 0
            self._in_loop = True
            self._cycles = 0
            self._automatic_entry = False
            self._armed = False
            self._loop_count = None if count is None else int(count)
            self._exit_queued = None
            self._held = False
            self._playing = True
            self._paused = False
            self._emit("loop_entered", loop_in=self.loop_in, loop_out=self.loop_out)
            return True

    def queue_loop_exit(self, mode="cut", fade_ms=None):
        """
        "cut": leave the loop at the next wrap (no-op outside a loop).
        "fade": fade out over ``fade_ms`` then stop. In a loop this ends with
        "loop_exited" (reason "fade"); in transport with "stopped"
        (reason "fade"). A paused/held transport stops at once; a stopped
        engine ignores it.
        """
        with self.lock:
            if mode == "fade":
                if not self._in_loop:
                    if not self._playing or not self._stems:
                        return
                    if not self._active():
                        self._finish_fade()   # paused/held: nothing to fade
                        return
                ms = 2000 if fade_ms is None else max(0, int(fade_ms))
                self._fadeout_total = max(1, int(round(ms / 1000.0 * self.sample_rate)))
                self._fadeout_done = 0
                self._exit_queued = "fade"
            elif mode == "cut":
                if self._in_loop:
                    self._exit_queued = "cut"
            else:
                raise ValueError(f"Unknown exit mode {mode!r}")

    def cancel_loop_exit(self):
        with self.lock:
            if self._fadeout_total:
                self._capture_tail()   # resume full level through the declick
                self._fadeout_total = self._fadeout_done = 0
            self._exit_queued = None

    def is_fading_out(self):
        return self._fadeout_total > 0

    def exit_loop_now(self):
        """Leave the loop immediately, continuing from loop_out (declicked)."""
        with self.lock:
            if self._in_loop:
                self._leave_loop(self._loop_out_f, "cut", declick=True)

    def get_loop_cycle_position(self):
        with self.lock:
            if not self._in_loop:
                return 0.0
            return self._to_sec(self._loop_pos)

    def get_loop_cycle_count(self):
        return self._cycles if self._in_loop else 0

    @property
    def is_armed(self):
        return self._armed

    def _exit_pending(self):
        if self._exit_queued == "cut":
            return True
        # count=N: leave after N wraps (N "loop_cycle" events).
        return self._loop_count is not None and self._cycles >= self._loop_count

    def _leave_loop(self, frame, reason, declick):
        if declick:
            self._capture_tail()
            self._frames_since_jump = 0
        self._in_loop = False
        self._exit_queued = None
        self._armed = False
        self._pos = min(frame, self._n_active)
        # Block an immediate re-entry of the same loop (the entry point sits
        # at or before where we just landed).
        self._entry_min_frame = self._loop_out_f + 1
        self._emit("loop_exited", position=self._to_sec(self._pos), reason=reason)

    # -------------------------------------------------------------- holds --

    def hold_at(self, position):
        with self.lock:
            self._hold_src = float(position)
            self._hold_f = self._to_frame(position)

    def clear_hold(self):
        with self.lock:
            self._hold_src = None
            self._hold_f = None

    def release_hold(self):
        with self.lock:
            if not self._held:
                return
            self._capture_tail()
            self._held = False
            self._playing = True
            self._paused = False
            self._frames_since_jump = 0
            self._emit("hold_released", position=self._to_sec(self._pos))

    # -------------------------------------------------------------- skips --

    def set_skip_regions(self, regions):
        clean = []
        for start, end in regions or ():
            start, end = float(start), float(end)
            if end > start:
                clean.append((start, end))
        clean.sort()
        with self.lock:
            self._skips_src = clean
            self._recompute_skip_frames()

    def _recompute_skip_frames(self):
        n = self._n_active
        out = []
        for s, e in self._skips_src:
            sf = self._to_frame(s)
            ef = min(self._to_frame(e), n)
            if ef > sf:
                out.append((sf, ef))
        self._skips_f = out

    # -------------------------------------------------------- tempo swap --

    def swap_buffers(self, buffers, tempo_ratio, semitones):
        """
        Schedule a hot-swap of the active buffers (same stem names, equal
        lengths). Loop bodies for the new buffers are built here, outside
        the lock; the swap itself happens at the start of the next
        ``render``, which only installs the prebuilt data. If the loop points
        or the active buffers change while building, the build is retried.
        """
        buffers = dict(buffers)
        with self.lock:
            stems = self._stems
            if not stems:
                return
            names = set(stems)
        if set(buffers) != names:
            raise ValueError("swap_buffers: stem names differ from the loaded stems")
        lengths = {len(b) for b in buffers.values()}
        if len(lengths) != 1 or lengths == {0}:
            raise ValueError("swap_buffers: buffers must be non-empty and of equal length")
        tempo_ratio, semitones = float(tempo_ratio), float(semitones)
        self._ensure_rebuild_worker()

        for _attempt in range(_MAX_BUILD_RETRIES):
            with self.lock:
                if self._stems is not stems:
                    return
                gen = self._buf_gen
                loop_key = self._loop_key()
                want = self._loop_ready and loop_key[1] > loop_key[0]
            bodies = self._build_bodies(buffers, *loop_key) if want else None
            with self.lock:
                if self._stems is not stems:
                    return
                now_want = self._loop_ready and self._loop_out > self._loop_in
                if gen != self._buf_gen or loop_key != self._loop_key() or want != now_want:
                    continue  # raced with set_loop_points / another swap
                self._pending_swap = _PendingSwap(
                    buffers, bodies, loop_key if want else None, tempo_ratio, semitones)
                return

        # Persistent contention: schedule without bodies; render defers the
        # swap and a worker builds them.
        with self.lock:
            if self._stems is stems:
                self._pending_swap = _PendingSwap(buffers, None, None, tempo_ratio, semitones)
                self._kick_rebuild()

    def _pending_stale(self, p):
        """True if ``p``'s prebuilt bodies do not match the current loop."""
        if not self._loop_ready:
            return False
        return p.bodies is None or p.loop_key != self._loop_key()

    def _refresh_pending_bodies(self, worker=False):
        """Build bodies for the pending swap until they are current (off-lock)."""
        while True:
            with self.lock:
                p = self._pending_swap
                if p is None or not self._pending_stale(p):
                    if worker:
                        self._rebuild_running = False
                    return
                key = self._loop_key()
                buffers = p.buffers
            try:
                built = self._build_bodies(buffers, *key)
            except Exception:
                logger.exception("Pending-swap body build failed")
                with self.lock:
                    if worker:
                        self._rebuild_running = False
                return
            with self.lock:
                if self._pending_swap is p and key == self._loop_key():
                    p.bodies, p.loop_key = built, key

    def _ensure_rebuild_worker(self):
        """Start the long-lived body rebuild worker (never on the audio thread)."""
        with self.lock:
            t = self._rebuild_thread
            if self._shutdown or (t is not None and t.is_alive()):
                return
            self._rebuild_thread = threading.Thread(
                target=self._rebuild_loop, name="LoopBodyRebuild", daemon=True)
            self._rebuild_thread.start()

    def _rebuild_loop(self):
        while True:
            self._rebuild_event.wait()
            self._rebuild_event.clear()
            if self._shutdown:
                return
            self._refresh_pending_bodies(worker=True)

    def shutdown(self):
        """Stop the rebuild worker (idempotent; the core stays usable for
        rendering, but stale swaps then wait for apply_pending_swap)."""
        self._shutdown = True
        self._rebuild_event.set()
        t = self._rebuild_thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)

    def _kick_rebuild(self):
        """Wake the rebuild worker for a stale pending swap (lock held).
        Real-time safe: only sets an Event, never starts a thread."""
        if self._rebuild_running:
            return
        self._rebuild_running = True
        self._rebuild_event.set()

    def apply_pending_swap(self):
        """
        Apply a scheduled swap now (``render`` does this automatically).
        Not for the audio thread: stale bodies are rebuilt synchronously.
        """
        self._refresh_pending_bodies()
        with self.lock:
            p = self._pending_swap
            if p is not None and not self._pending_stale(p):
                self._apply_swap()

    def _apply_swap(self):
        """Install the pending swap's buffers and prebuilt bodies (lock held)."""
        p = self._pending_swap
        self._pending_swap = None
        new_n = p.n
        old_scale = self._scale()
        new_scale = self._n_src / new_n
        ratio = old_scale / new_scale
        self._capture_tail()
        old_song_f = self._loop_song_frame()
        old_out_f = self._loop_out_f
        # Move the playhead to the equivalent source position
        self._pos = int(round(self._pos * ratio))
        self._pos = max(0, min(self._pos, new_n))
        # Re-entry guard: keep blocking the loop just left, else rescale.
        emf = self._entry_min_frame
        blocks_loop = emf > 0 and emf == old_out_f + 1
        if emf > 0:
            self._entry_min_frame = int(math.ceil(emf * ratio))
        for name, s in self._stems.items():
            s.active = p.buffers[name]
        self._buf_gen += 1
        self._n_active = new_n
        self._tempo_ratio = p.tempo
        self._semitones = p.semitones
        self._frames_since_jump = 0
        self._recompute_skip_frames()
        if self._hold_src is not None:
            self._hold_f = self._to_frame(self._hold_src)
        if self._loop_ready and p.bodies is not None and p.loop_key == self._loop_key():
            body_map, in_f, out_f, cf = p.bodies
            old_len = self._body_len
            for name, s in self._stems.items():
                s.body = body_map.get(name)
            new_len = len(next(iter(body_map.values())))
            if self._in_loop and old_len > 0:
                if p.loop_key == self._installed_key:
                    self._loop_pos = int(round(self._loop_pos * new_len / old_len)) % new_len
                else:
                    # The playing bodies were for older points (a
                    # set_loop_points still building): keep the song sample.
                    lp = int(round(old_song_f * ratio)) - in_f
                    self._loop_pos = lp if 0 <= lp < new_len else 0
            self._install_loop_frames(in_f, out_f, cf, new_len, p.loop_key)
            if blocks_loop:
                self._entry_min_frame = out_f + 1
        else:
            # No matching bodies (only reachable when no loop is ready, since
            # render defers stale swaps): old bodies would be wrong now.
            for s in self._stems.values():
                s.body = None
            if self._loop_ready:
                self._loop_ready = False
                self._body_len = 0
                if self._in_loop:
                    self._in_loop = False
                    self._exit_queued = None
                    self._emit("loop_exited", position=self._to_sec(self._pos), reason="seek")
        self._emit("render_done", tempo_ratio=p.tempo, semitones=p.semitones)

    def prefetch_spans(self, frames):
        """
        [(buffer, start, end)] of active/pending buffer frames the callback
        may read within the next ``frames`` frames: the playhead (or, in a
        loop, the cut-exit continuation after loop_out; bodies are in RAM),
        the next skip target, and a pending swap's equivalent position. A
        non-real-time thread touches these memmap pages so the audio
        callback does not page-fault on them.
        """
        frames = max(0, int(frames))
        with self.lock:
            if not self._stems or not self._playing:
                return []
            if self._in_loop:
                starts = [self._loop_out_f]
                here = self._loop_song_frame()
            else:
                starts = [self._pos]
                here = self._pos
                nxt = next((e for s0, e in self._skips_f if s0 >= self._pos), None)
                if nxt is not None:
                    starts.append(nxt)
            spans = [(s.active, a, min(a + frames, self._n_active))
                     for s in self._stems.values() for a in starts]
            p = self._pending_swap
            if p is not None:
                a = int(round(here * self._scale() * p.n / self._n_src))
                spans += [(b, a, min(a + frames, p.n)) for b in p.buffers.values()]
        return [sp for sp in spans if sp[2] > sp[1] >= 0]

    # ---------------------------------------------------------- rendering --

    def render(self, frames, channels=None):
        """
        Render ``frames`` frames; returns float32 (frames, channels).
        ``channels`` defaults to ``output_channels``; the audio callback
        passes the stream's channel count so a routing change that has not
        reopened the stream yet is routed into the channels that exist.
        """
        frames = int(frames)
        with self.lock:
            self._frames_rendered += frames
            nch = self.output_channels if channels is None else max(1, int(channels))
            out = np.zeros((frames, nch), dtype=np.float32)
            guard = 0
            if self._pending_swap is not None:
                if self._pending_stale(self._pending_swap):
                    # Never build bodies here: defer one block, rebuild off-thread.
                    self._kick_rebuild()
                else:
                    # Events due exactly at the current frame belong to the
                    # old buffers; rounding the playhead could skip them.
                    guard = self._handle_due_events(guard)
                    self._apply_swap()
            self._update_gains()
            mix = np.zeros((frames, 2), dtype=np.float32)
            click = np.zeros((frames, 2), dtype=np.float32)
            i = 0
            while i < frames:
                n = frames - i
                if self._active():
                    dist, kind = self._next_event()
                    if dist <= 0:
                        guard += 1
                        if guard > _MAX_EVENTS_PER_BLOCK:
                            self._abort_playback()
                            continue
                        self._handle_event(kind)
                        continue
                    n = min(n, dist)
                    self._pull(mix, click, i, n)
                    guard = 0   # only consecutive zero-length events count
                self._blend_tail(mix, click, i, n)
                i += n
            self._route(mix, click, out)
        return out

    def _handle_due_events(self, guard):
        """Handle the zero-distance events at the playhead (lock held)."""
        while self._active():
            dist, kind = self._next_event()
            if dist > 0:
                break
            guard += 1
            if guard > _MAX_EVENTS_PER_BLOCK:
                break   # render's loop trips the guard
            self._handle_event(kind)
        return guard

    def _abort_playback(self):
        """
        Safety valve: zero-length events keep firing without any audio
        (state is bad). Stop and report it like a finished fade: a running
        loop ends with "loop_exited" (reason "seek"), then "stopped"
        (reason "error") so the state manager leaves PLAYING.
        """
        logger.error("Too many zero-length events in one block; stopping")
        self._capture_tail()
        was_loop = self._in_loop
        frame = self._clamp_frame(self._loop_song_frame() if was_loop else self._pos)
        self._playing = False
        self._paused = False
        self._held = False
        self._in_loop = False
        self._armed = False
        self._exit_queued = None
        self._fadein_total = self._fadein_done = 0
        self._fadeout_total = self._fadeout_done = 0
        self._pos = frame
        if was_loop:
            self._emit("loop_exited", position=self._to_sec(frame), reason="seek")
        self._emit("stopped", position=self._to_sec(frame), reason="error")

    def _next_event(self):
        best = None

        def cand(d, k):
            nonlocal best
            if best is None or d < best[0]:
                best = (d, k)

        if self._fadeout_total:
            cand(self._fadeout_total - self._fadeout_done, "fade_done")
        if self._in_loop:
            rem = self._body_len - self._loop_pos
            cand(rem, "wrap")
        else:
            if self._hold_f is not None and self._hold_f >= self._pos:
                cand(self._hold_f - self._pos, "hold")
            for s, _e in self._skips_f:
                if s >= self._pos:
                    cand(s - self._pos, "skip")
                    break
            if self._armed and self._loop_ready and self._body_len > 0:
                ef = self._loop_out_f
                if ef >= self._pos and ef >= self._entry_min_frame:
                    cand(ef - self._pos, "loop_entry")
            cand(self._n_active - self._pos, "song_end")
        return best

    def _handle_event(self, kind):
        if kind == "fade_done":
            self._finish_fade()
        elif kind == "song_end":
            self._playing = False
            self._paused = False
            self._emit("song_ended")
        elif kind == "hold":
            self._capture_tail()
            self._held = True
            self._hold_f = None
            self._hold_src = None
            self._emit("hold_reached", position=self._to_sec(self._pos))
        elif kind == "skip":
            for s, e in self._skips_f:
                if s >= self._pos:
                    break
            else:
                return
            from_pos = self._to_sec(self._pos)
            self._capture_tail()
            self._pos = e
            self._frames_since_jump = 0
            self._emit("skip_executed", from_pos=from_pos, to_pos=self._to_sec(e))
        elif kind == "loop_entry":
            self._in_loop = True
            self._loop_pos = 0
            self._cycles = 1  # the original transport pass just completed
            self._automatic_entry = True
            self._armed = False
            self._exit_queued = None
            self._emit("loop_entered", loop_in=self.loop_in, loop_out=self.loop_out)
            self._emit("loop_cycle", cycle=self._cycles)
        elif kind == "wrap":
            if self._exit_queued == "cut":
                self._leave_loop(self._loop_out_f, "cut", declick=False)
                return
            if self._automatic_entry and self._exit_pending():
                self._leave_loop(self._loop_out_f, "count", declick=False)
                return
            self._loop_pos = 0
            self._cycles += 1
            self._emit("loop_cycle", cycle=self._cycles)
            if not self._automatic_entry and self._exit_pending():
                self._leave_loop(self._loop_out_f, "count", declick=False)

    def _finish_fade(self):
        """A fade exit reached silence: stop and report it."""
        was_loop = self._in_loop
        self._fadeout_total = self._fadeout_done = 0
        self._fadein_total = self._fadein_done = 0
        self._exit_queued = None
        self._playing = False
        self._paused = False
        self._held = False
        if was_loop:
            self._in_loop = False
            self._armed = False
            self._pos = min(self._loop_out_f, self._n_active)
            self._entry_min_frame = self._loop_out_f + 1
            self._emit("loop_exited", position=self._to_sec(self._pos), reason="fade")
        else:
            self._emit("stopped", position=self._to_sec(self._pos), reason="fade")

    def _read_into(self, mix, click, i, n, advance=False):
        """Add up to ``n`` frames of the current state to the buses (no
        advance); returns the number of frames actually available."""
        if self._in_loop:
            p = self._loop_pos
            avail = self._body_len - p
        else:
            p = self._pos
            avail = self._n_active - p
        k = min(n, avail)
        if k > 0:
            self._read_span(mix, click, i, k, self._in_loop, p, advance)
        return max(0, k)

    def _read_span(self, mix, click, i, k, from_body, p, advance=False):
        """Mix ``k`` frames at ``p`` into the buses. ``advance`` moves the
        gain ramps on (playback); tails read at the current gain."""
        for s in self._stems.values():
            ramping = advance and s.g_left > 0
            if not ramping and not s.g_cur:
                continue
            buf = s.body if from_body else s.active
            if buf is None:
                continue
            bus = click if s.is_click else mix
            data = np.asarray(buf[p:p + k], dtype=np.float32)
            if ramping:
                m = min(k, s.g_left)
                g = (s.g_cur + s.g_step * np.arange(1, m + 1, dtype=np.float32)) * _INT16_SCALE
                data[:m] *= g[:, None]
                s.g_left -= m
                s.g_cur = s.g_target if s.g_left == 0 else s.g_cur + s.g_step * m
                if m < k:
                    data[m:] *= np.float32(s.g_cur) * _INT16_SCALE
            else:
                data *= np.float32(s.g_cur) * _INT16_SCALE
            bus[i:i + k] += data

    def _read_tail(self, mix, click, n):
        """
        Read the next ``n`` frames of the current state for a declick tail.
        In a loop the read wraps into the body (that is what would have
        played); near the song end the available frames fade to zero
        instead of stopping on a hard step.
        """
        if self._in_loop and self._body_len > 0:
            p = self._loop_pos % self._body_len
            i = 0
            while i < n:
                k = min(n - i, self._body_len - p)
                self._read_span(mix, click, i, k, True, p)
                i += k
                p = 0
            return
        k = self._read_into(mix, click, 0, n)
        if 0 < k < n:
            fade = (np.arange(k, 0, -1, dtype=np.float32) / np.float32(k + 1))[:, None]
            mix[:k] *= fade
            click[:k] *= fade

    def _pull(self, mix, click, i, n):
        self._read_into(mix, click, i, n, advance=True)
        env = self._envelope(n)
        if env is not None:
            mix[i:i + n] *= env
            click[i:i + n] *= env
        if self._in_loop:
            self._loop_pos += n
        else:
            self._pos += n
        self._frames_since_jump += n

    def _envelope(self, n):
        env = None
        if self._fadein_total and self._fadein_done < self._fadein_total:
            r = (self._fadein_done + np.arange(1, n + 1, dtype=np.float32)) / self._fadein_total
            np.minimum(r, 1.0, out=r)
            env = r[:, None]
            self._fadein_done += n
        if self._fadeout_total:
            r = 1.0 - (self._fadeout_done + np.arange(1, n + 1, dtype=np.float32)) / self._fadeout_total
            np.maximum(r, 0.0, out=r)
            env = r[:, None] if env is None else env * r[:, None]
            self._fadeout_done += n
        return env

    def _capture_tail(self):
        """Snapshot the next declick frames of the CURRENT state as a tail."""
        n = self.declick_frames
        if n <= 0:
            return
        tm = np.zeros((n, 2), dtype=np.float32)
        tc = np.zeros((n, 2), dtype=np.float32)
        if self._active():
            self._read_tail(tm, tc, n)
            g = self._env_value()
            if g != 1.0:
                tm *= g
                tc *= g
        if self._tail_mix is not None and self._tail_idx < n:
            # A jump during a declick: fold the remaining old tail in.
            rem = n - self._tail_idx
            r = self._ramp_in[:rem]
            tm[:rem] = tm[:rem] * r + self._tail_mix[self._tail_idx:] * (1.0 - r)
            tc[:rem] = tc[:rem] * r + self._tail_click[self._tail_idx:] * (1.0 - r)
        self._tail_mix, self._tail_click, self._tail_idx = tm, tc, 0

    def _blend_tail(self, mix, click, i, n):
        if self._tail_mix is None:
            return
        total = self.declick_frames
        k = min(n, total - self._tail_idx)
        if k > 0:
            a = self._tail_idx
            r = self._ramp_in[a:a + k]
            mix[i:i + k] = mix[i:i + k] * r + self._tail_mix[a:a + k] * (1.0 - r)
            click[i:i + k] = click[i:i + k] * r + self._tail_click[a:a + k] * (1.0 - r)
            self._tail_idx += k
        if self._tail_idx >= total:
            self._tail_mix = self._tail_click = None
            self._tail_idx = 0

    def _route(self, mix, click, out):
        outch = out.shape[1]
        mode = self._routing
        if outch < 2:
            out[:, 0] = (mix + click).mean(axis=1)
        elif mode == "stereo":
            out[:, 0:2] = mix + click
        elif mode == "click_right":
            out[:, 0] = mix.mean(axis=1)
            out[:, 1] = click.mean(axis=1)
        else:
            for bus, chans in ((mix, self._channel_map["mix"]), (click, self._channel_map["click"])):
                if len(chans) == 1:
                    if chans[0] < outch:
                        out[:, chans[0]] += bus.mean(axis=1)
                    continue
                for j, ch in enumerate(chans):
                    if ch < outch:
                        out[:, ch] += bus[:, j % 2]
        self.output_peak = float(np.max(np.abs(out))) if out.size else 0.0
        if self.output_peak > 1.0:
            self.clipped_blocks += 1
        np.clip(out, -1.0, 1.0, out=out)
