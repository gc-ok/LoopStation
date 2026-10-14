"""
Audio Engine for Loop Station.

Playback runs through a ``sounddevice.OutputStream`` callback that pulls
float32 blocks from a pure ``MixerCore`` (``backend/mixer_core.py``). All
mixing, looping, holds, skips, fades and routing happen inside the callback,
sample-accurately, with no wall-clock timing anywhere.

Decoding
--------
Every input file (the primary "mix" and each optional stem) is decoded ONCE
by ffmpeg into a 16-bit stereo WAV at SAMPLE_RATE under
<tempdir>/LoopStation and memory-mapped at its 'data' chunk. Stems are padded
with silence so all buffers have the same length. Stale temp files from
earlier runs are swept when the engine starts.

Tempo / key
-----------
``set_tempo_pitch`` renders every stem offline with ffmpeg filters
(``asetrate`` + ``aresample`` for pitch, a chain of ``atempo`` for tempo) in a
background thread and hot-swaps the buffers inside the callback at the
equivalent source position. Loop points, holds and skips are kept in source
seconds, so cue times never move.

Events are delivered on a worker thread, never from the audio callback.

Device loss
-----------
The stream's ``finished_callback`` reports a stream that ended without us
closing it ("device_error"); output underflows are counted
(``underflow_count``) and ``frames_rendered`` lets a watchdog notice a
callback that stopped running. ``restart_stream()`` reopens the stream on the
current device (falling back to the system default) and playback resumes at
the same sample because all transport state lives in the core.

This module has NO UI dependencies and can be tested without a device by
passing ``stream_factory`` (see tests/helpers_audio.py).
"""

import gc
import os
import inspect
import uuid
import wave
import struct
import logging
import tempfile
import threading
import subprocess
import numpy as np

from config import (
    SAMPLE_RATE, CHANNELS, LOOP_CROSSFADE_MS, FADE_EXIT_DURATION_MS,
    TEMP_DIR_NAME, FFMPEG_DECODE_TIMEOUT_S,
    AUDIO_BLOCK_SIZE, DECLICK_MS, TEMPO_MIN, TEMPO_MAX, SEMITONES_MAX,
)
from .mixer_core import (
    MixerCore, StemInfo, build_loop_body, is_click_stem, ROUTING_MODES,
)

__all__ = [
    "AudioEngine", "MixerCore", "StemInfo", "build_loop_body", "is_click_stem",
    "ROUTING_MODES", "parse_wav_data_chunk", "sweep_stale_temp_files",
    "get_temp_dir", "atempo_chain", "build_tempo_pitch_filter", "TEMP_FILE_PREFIX",
]

try:
    import sounddevice as sd
except Exception:  # pragma: no cover - PortAudio missing on this machine
    sd = None

logger = logging.getLogger("LoopStation.AudioEngine")

# Suppress console window on Windows for subprocess calls
_SUBPROCESS_FLAGS = {}
if os.name == 'nt':
    _SUBPROCESS_FLAGS['creationflags'] = subprocess.CREATE_NO_WINDOW

# Prefix for every temp file this engine creates (used by the stale sweep).
TEMP_FILE_PREFIX = "loopstation_"

_STOP_EVENTS = object()


# =============================================================================
# MODULE-LEVEL HELPERS (pure functions, unit-testable without a device)
# =============================================================================

def get_temp_dir():
    """Return (and create) the dedicated temp directory for decoded audio."""
    path = os.path.join(tempfile.gettempdir(), TEMP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def sweep_stale_temp_files(temp_dir, keep_pid=None):
    """
    Delete leftover decoded-audio files from previous runs.

    Files that are still locked (another running instance, or a memmap that
    could not be released last time) are skipped silently and retried on the
    next start. Files belonging to ``keep_pid`` are never touched.

    Returns:
        Number of files removed.
    """
    removed = 0
    try:
        names = os.listdir(temp_dir)
    except OSError:
        return 0

    own_prefix = f"{TEMP_FILE_PREFIX}{keep_pid}_" if keep_pid is not None else None
    for name in names:
        if not name.startswith(TEMP_FILE_PREFIX):
            continue
        if own_prefix and name.startswith(own_prefix):
            continue
        full = os.path.join(temp_dir, name)
        try:
            os.unlink(full)
            removed += 1
        except OSError as e:
            logger.debug(f"Stale temp file still in use, skipping: {name} ({e})")
    if removed:
        logger.info(f"Swept {removed} stale temp file(s) from {temp_dir}")
    return removed


def parse_wav_data_chunk(path):
    """
    Walk the RIFF chunk list of a WAV file and locate the PCM payload.

    Does NOT assume a 44-byte header: ffmpeg frequently writes a LIST/INFO
    chunk before 'data', and WAVE_FORMAT_EXTENSIBLE headers are longer.

    Returns:
        dict with keys data_offset, data_size, channels, sample_rate,
        bits_per_sample, format_tag, frames.

    Raises:
        ValueError if the file is not a RIFF/WAVE file or lacks fmt/data chunks.
    """
    file_size = os.path.getsize(path)
    fmt = None
    data = None

    with open(path, 'rb') as f:
        header = f.read(12)
        if len(header) < 12 or header[:4] not in (b'RIFF', b'RF64') or header[8:12] != b'WAVE':
            raise ValueError(f"Not a RIFF/WAVE file: {path}")

        pos = 12
        while pos + 8 <= file_size:
            f.seek(pos)
            chunk_header = f.read(8)
            if len(chunk_header) < 8:
                break
            chunk_id = chunk_header[:4]
            chunk_size = struct.unpack('<I', chunk_header[4:8])[0]
            body = pos + 8

            if chunk_id == b'fmt ':
                raw = f.read(min(chunk_size, 40))
                if len(raw) < 16:
                    raise ValueError("Truncated 'fmt ' chunk")
                format_tag, channels, sample_rate, _, _, bits = struct.unpack('<HHIIHH', raw[:16])
                if format_tag == 0xFFFE and len(raw) >= 26:
                    # WAVE_FORMAT_EXTENSIBLE: real format tag is the first
                    # two bytes of the SubFormat GUID.
                    format_tag = struct.unpack('<H', raw[24:26])[0]
                fmt = {
                    'format_tag': format_tag,
                    'channels': channels,
                    'sample_rate': sample_rate,
                    'bits_per_sample': bits,
                }
            elif chunk_id == b'data':
                # ffmpeg writes 0xFFFFFFFF when it could not seek back to patch
                # the size; RF64 files store the real size elsewhere. Either
                # way the payload runs to the end of the file.
                if chunk_size == 0xFFFFFFFF or body + chunk_size > file_size:
                    chunk_size = file_size - body
                data = (body, chunk_size)
                break

            pos = body + chunk_size + (chunk_size & 1)  # chunks are word-aligned

    if fmt is None:
        raise ValueError("WAV file has no 'fmt ' chunk")
    if data is None:
        raise ValueError("WAV file has no 'data' chunk")

    bytes_per_frame = max(1, fmt['channels'] * (fmt['bits_per_sample'] // 8))
    result = dict(fmt)
    result['data_offset'] = data[0]
    result['data_size'] = data[1]
    result['frames'] = data[1] // bytes_per_frame
    return result


def atempo_chain(factor):
    """Split a speed factor into ffmpeg ``atempo`` factors, each in [0.5, 2]."""
    factor = float(factor)
    if factor <= 0:
        raise ValueError("tempo factor must be positive")
    chain = []
    while factor > 2.0:
        chain.append(2.0)
        factor /= 2.0
    while factor < 0.5:
        chain.append(0.5)
        factor /= 0.5
    if abs(factor - 1.0) > 1e-6:
        chain.append(factor)
    return chain


def build_tempo_pitch_filter(tempo_ratio, semitones, sample_rate=SAMPLE_RATE):
    """
    ffmpeg ``-af`` string for a tempo/pitch render, or None for identity.

    Pitch: ``asetrate=SR*2^(semi/12),aresample=SR`` (changes speed by the
    same factor), then ``atempo`` factors so that the final playback speed
    equals ``tempo_ratio``.
    """
    parts = []
    speed = float(tempo_ratio)
    if abs(float(semitones)) > 1e-9:
        pitch = 2.0 ** (float(semitones) / 12.0)
        new_rate = int(round(sample_rate * pitch))
        actual_pitch = new_rate / sample_rate
        parts.append(f"asetrate={new_rate}")
        parts.append(f"aresample={int(sample_rate)}")
        speed = speed / actual_pitch
    for f in atempo_chain(speed):
        parts.append(f"atempo={f:.6f}")
    return ",".join(parts) if parts else None


# =============================================================================
# ENGINE
# =============================================================================

class AudioEngine:
    """
    Device-facing wrapper around ``MixerCore``.

    Usage:
        engine = AudioEngine(ffmpeg_path="ffmpeg")
        engine.on_event(lambda name, payload: ...)
        engine.load_file("song.mp3", {"click": "song.stems/click.wav"})
        engine.arm_loop(10.0, 20.0)       # vamp entered exactly at 20.0 s
        engine.play_transport(0.0)
        engine.queue_loop_exit("cut")     # leave at the next wrap
    """

    def __init__(self, ffmpeg_path="ffmpeg", device=None, sample_rate=SAMPLE_RATE,
                 block_size=AUDIO_BLOCK_SIZE, stream_factory=None):
        self.ffmpeg_path = ffmpeg_path
        self.SAMPLE_RATE = int(sample_rate)
        self.block_size = int(block_size)
        self._stream_factory = stream_factory or self._default_stream_factory
        self._device = device

        self.core = MixerCore(self.SAMPLE_RATE, DECLICK_MS)
        self.lock = self.core.lock

        # Dedicated temp directory; sweep leftovers from earlier runs
        self.temp_dir = get_temp_dir()
        sweep_stale_temp_files(self.temp_dir, keep_pid=os.getpid())

        # Audio data
        self.current_file_path = None
        self.raw_audio_data = None      # "mix" stem int16 (frames, 2) memmap
        self.song_length = 0.0
        self._stem_sources = {}         # name -> {"path": wav path, "data": memmap}
        self._temp_paths = []           # decoded/padded files we own
        self._render_paths = []         # rendered files in use (or retiring)
        self._retired_paths = []        # rendered files no longer wanted

        # Tempo / key
        self._tempo_pitch = (1.0, 0.0)
        self._render_gen = 0
        self._render_thread = None
        self._closed = False
        self._render_lock = threading.RLock()

        # Events
        self._callbacks = []
        self._event_thread = threading.Thread(
            target=self._event_loop, name="AudioEngineEvents", daemon=True)
        self._event_thread.start()

        # Stream
        self._stream = None
        self._stream_channels = 0
        self._stream_device = None
        self._latency_frames = 0
        self._callback_error_logged = False
        self._stream_lock = threading.RLock()   # open/close/restart (never the core lock)
        self._stream_token = 0      # identifies the current stream for finished_callback
        self._stream_lost = False   # the current stream finished on its own
        self._underflows = 0
        self._last_stream_error = None
        self._open_stream()

        logger.info("AudioEngine initialized")

    # =========================================================================
    # EVENTS
    # =========================================================================

    def on_event(self, callback):
        """Register ``callback(name, payload)``; called on a worker thread."""
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def remove_event_callback(self, callback):
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def _emit(self, _event, /, **payload):
        self.core.events.put((_event, payload))

    def _event_loop(self):
        q = self.core.events
        while True:
            item = q.get()
            if item is _STOP_EVENTS:
                break
            name, payload = item
            for cb in list(self._callbacks):
                try:
                    cb(name, dict(payload))
                except Exception:
                    logger.exception(f"Event callback failed for {name}")

    # =========================================================================
    # DEVICES / STREAM
    # =========================================================================

    @staticmethod
    def _device_entry(index, info, default_index):
        hostapi = info.get('hostapi')
        hostapi_name = None
        if sd is not None and hostapi is not None:
            try:
                hostapi_name = sd.query_hostapis(hostapi)['name']
            except Exception:
                hostapi_name = None
        return {
            'index': index,
            'name': info.get('name'),
            'channels': int(info.get('max_output_channels', 0)),
            'default': index == default_index,
            'hostapi': hostapi_name,
        }

    def _preferred_default_device(self):
        """WASAPI default output if present, else PortAudio's default."""
        if sd is None:
            return None
        try:
            for api in sd.query_hostapis():
                if 'wasapi' in str(api.get('name', '')).lower():
                    idx = api.get('default_output_device', -1)
                    if idx is not None and idx >= 0:
                        return int(idx)
        except Exception:
            pass
        try:
            dev = sd.default.device
            out = dev[1] if isinstance(dev, (list, tuple)) else dev
            if out is not None and int(out) >= 0:
                return int(out)
        except Exception:
            pass
        return None

    def list_output_devices(self):
        if sd is None:
            return []
        default_index = self._preferred_default_device()
        out = []
        try:
            devices = sd.query_devices()
        except Exception as e:
            logger.warning(f"query_devices failed: {e}")
            return []
        for i, info in enumerate(devices):
            if int(info.get('max_output_channels', 0)) <= 0:
                continue
            out.append(self._device_entry(i, info, default_index))
        return out

    def get_output_device(self):
        if self._stream is None or sd is None:
            return None
        idx = self._stream_device
        if idx is None:
            idx = self._preferred_default_device()
        if idx is None:
            return None
        try:
            info = sd.query_devices(idx)
        except Exception:
            return None
        return self._device_entry(idx, info, self._preferred_default_device())

    def set_output_device(self, index):
        """
        Switch output device (None = system default); restarts the stream.
        If the new device cannot be opened the previous device is reopened
        and RuntimeError is raised.
        """
        with self._stream_lock:
            previous = self._device
            self._device = None if index is None else int(index)
            if self._restart_stream(emit_error=False):
                return
            message = self._last_stream_error or "could not open output device"
            # Keep a working device rather than a broken one.
            self._device = previous
            self._restart_stream()
            raise RuntimeError(f"Output device {index!r} unavailable: {message}")

    def _default_stream_factory(self, callback, channels, sample_rate, block_size,
                                finished_callback=None):
        if sd is None:
            raise RuntimeError("sounddevice/PortAudio is not available")
        device = self._device if self._device is not None else self._preferred_default_device()
        attempts = [device]
        if self._device is None and device is not None:
            attempts.append(None)   # PortAudio default as a fallback
        last_err = None
        for dev in attempts:
            kwargs = dict(device=dev, channels=channels, samplerate=sample_rate,
                          blocksize=block_size, dtype='float32', callback=callback)
            if finished_callback is not None:
                kwargs['finished_callback'] = finished_callback
            try:
                if dev is not None and self._is_wasapi(dev) and hasattr(sd, 'WasapiSettings'):
                    try:
                        kwargs['extra_settings'] = sd.WasapiSettings(auto_convert=True)
                    except TypeError:
                        pass
                stream = sd.OutputStream(**kwargs)
                self._stream_device = dev
                return stream
            except Exception as e:
                last_err = e
                logger.warning(f"Could not open output device {dev!r}: {e}")
        raise RuntimeError(f"No usable output device: {last_err}")

    @staticmethod
    def _is_wasapi(device):
        try:
            info = sd.query_devices(device)
            return 'wasapi' in str(sd.query_hostapis(info['hostapi'])['name']).lower()
        except Exception:
            return False

    def _factory_accepts_finished_callback(self):
        try:
            params = inspect.signature(self._stream_factory).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(p.name == 'finished_callback' or p.kind == p.VAR_KEYWORD for p in params)

    def _make_finished_callback(self, token):
        def finished():
            self._on_stream_finished(token)
        return finished

    def _on_stream_finished(self, token):
        """The stream ended (PortAudio thread). Closed by us, or a lost device?"""
        if token != self._stream_token or self._stream is None or self._stream_lost:
            return  # we closed it ourselves (or it was already reported)
        self._stream_lost = True
        logger.error("Audio output stream stopped unexpectedly (device lost?)")
        self._emit("device_error", message="audio output stream stopped unexpectedly "
                                           "(device disconnected?)")

    def _open_stream(self, emit_error=True):
        channels = self.core.output_channels
        with self._stream_lock:
            if self._closed:
                return False
            self._stream_token += 1
            token = self._stream_token
            try:
                if self._factory_accepts_finished_callback():
                    stream = self._stream_factory(
                        self._callback, channels, self.SAMPLE_RATE, self.block_size,
                        finished_callback=self._make_finished_callback(token))
                else:
                    stream = self._stream_factory(
                        self._callback, channels, self.SAMPLE_RATE, self.block_size)
                self._stream = stream
                self._stream_lost = False
                stream.start()
            except Exception as e:
                logger.error(f"Audio output unavailable: {e}")
                failed, self._stream = self._stream, None
                self._stream_token += 1     # ignore finished_callback of the failed stream
                if failed is not None:
                    try:
                        failed.close()
                    except Exception:
                        pass
                self._stream_channels = 0
                self._latency_frames = 0
                self._last_stream_error = str(e)
                if emit_error:
                    self._emit("device_error", message=str(e))
                return False
            self._stream_channels = channels
            self._last_stream_error = None
            self._callback_error_logged = False
            latency = getattr(stream, 'latency', 0.0)
            try:
                if isinstance(latency, (tuple, list)):
                    latency = latency[-1]
                self._latency_frames = max(0, int(float(latency) * self.SAMPLE_RATE))
            except Exception:
                self._latency_frames = 0
        logger.info(f"Output stream open: {channels} ch, {self.SAMPLE_RATE} Hz, "
                    f"block {self.block_size}, latency {self._latency_frames} frames")
        return True

    def _close_stream(self):
        with self._stream_lock:
            self._stream_token += 1      # its finished_callback is now ours, not a loss
            stream, self._stream = self._stream, None
            self._stream_channels = 0
            self._stream_lost = False
            if stream is None:
                return
            for meth in ('stop', 'close'):
                try:
                    getattr(stream, meth)()
                except Exception as e:
                    logger.debug(f"stream.{meth}() failed: {e}")

    def _restart_stream(self, emit_error=True):
        with self._stream_lock:
            self._close_stream()
            return self._open_stream(emit_error=emit_error)

    def _ensure_stream(self, emit_error=True):
        """Open/reopen the stream if missing or the channel count changed."""
        with self._stream_lock:
            if self._stream is None:
                return self._open_stream(emit_error=emit_error)
            if self._stream_channels != self.core.output_channels:
                return self._restart_stream(emit_error=emit_error)
            return True

    def restart_stream(self):
        """
        Reopen the output stream on the current device, falling back to the
        system default (which then becomes the selected device). The core is
        untouched, so playback resumes at the same sample. Emits
        "device_restored" {name} on success, "device_error" on failure.
        """
        with self._stream_lock:
            ok = self._restart_stream(emit_error=False)
            if not ok and self._device is not None:
                logger.warning(f"Output device {self._device!r} unavailable; "
                               "falling back to the system default")
                preferred = self._device
                self._device = None
                ok = self._restart_stream(emit_error=False)
                if not ok:
                    self._device = preferred
            if not ok:
                self._emit("device_error",
                           message=self._last_stream_error or "no usable output device")
                return False
            info = self.get_output_device()
            name = (info or {}).get('name') or "system default"
        logger.info(f"Output stream restored on {name!r}")
        self._emit("device_restored", name=name)
        return True

    def is_stream_active(self):
        """True while an output stream is open, started and not lost."""
        stream = self._stream
        if stream is None or self._stream_lost:
            return False
        try:
            return bool(getattr(stream, 'active', True))
        except Exception:
            return False

    @property
    def frames_rendered(self):
        """Frames rendered by the callback so far (monotonic; watchdog)."""
        return self.core.frames_rendered

    @property
    def underflow_count(self):
        """Output underflows reported in the stream callback status."""
        return self._underflows

    def _callback(self, outdata, frames, time_info, status):
        if status is not None and getattr(status, 'output_underflow', False):
            self._underflows += 1
        try:
            outdata[:] = self.core.render(frames)
        except Exception as e:
            outdata.fill(0)
            if not self._callback_error_logged:
                self._callback_error_logged = True
                logger.exception("Render failed inside the audio callback")
                self._emit("device_error", message=f"render failed: {e}")

    # ---- routing

    def _change_routing(self, apply, restore, what):
        """
        Apply a routing change; if a working stream cannot be reopened with
        the new channel count, restore the previous routing, reopen, and
        raise RuntimeError. (Without a working stream before the change
        there is nothing to protect: the change is kept.)
        """
        with self._stream_lock:
            had_stream = self._stream is not None and not self._stream_lost
            apply()
            if self._ensure_stream(emit_error=not had_stream) or not had_stream:
                return
            message = self._last_stream_error or "could not open output stream"
            restore()
            self._ensure_stream()
            raise RuntimeError(f"{what} not supported by the output device: {message}")

    def set_routing(self, mode):
        """Raises RuntimeError (routing reverted) if the device cannot do it."""
        previous = self.core.get_routing()["mode"]
        self._change_routing(lambda: self.core.set_routing(mode),
                             lambda: self.core.set_routing(previous),
                             f"Routing {mode!r}")

    def set_channel_map(self, mix=(0, 1), click=(2, 3)):
        """Raises RuntimeError (map reverted) if the device cannot do it."""
        previous = self.core.get_routing()["channel_map"]
        self._change_routing(lambda: self.core.set_channel_map(mix, click),
                             lambda: self.core.set_channel_map(previous["mix"], previous["click"]),
                             "Channel map")

    def get_routing(self):
        return self.core.get_routing()

    # =========================================================================
    # FILE LOADING
    # =========================================================================

    def load_file(self, path, stem_paths=None):
        """
        Decode ``path`` (stem "mix") and each ``stem_paths[name]`` to WAV
        temp files, memmap them and hand them to the mixer.

        Returns:
            (duration_seconds, 1.0)   # second value kept for compatibility
        """
        if self._closed:
            raise RuntimeError("Audio engine is closed")
        logger.info(f"=== LOADING FILE: {os.path.basename(path)} ===")
        self.stop()
        self._release_audio()
        self.current_file_path = path

        decoded = {"mix": self._decode(path)}
        for name, stem_path in (stem_paths or {}).items():
            if name == "mix" or not stem_path:
                continue
            try:
                decoded[name] = self._decode(stem_path)
            except Exception as e:
                logger.error(f"Stem {name!r} could not be decoded, skipping: {e}")

        infos = {name: parse_wav_data_chunk(p) for name, p in decoded.items()}
        target = max(info['frames'] for info in infos.values())
        if target <= 0:
            raise ValueError("Decoded audio is empty")
        for name, info in infos.items():
            if info['frames'] < target:
                decoded[name] = self._pad_wav(decoded[name], info, target)
                infos[name] = parse_wav_data_chunk(decoded[name])

        buffers = {}
        for name, p in decoded.items():
            buffers[name] = self._map_wav(p, infos[name])
            self._stem_sources[name] = {"path": p, "data": buffers[name]}

        with self._render_lock:
            if self._closed:
                buffers.clear()
                self._release_audio_locked()
                raise RuntimeError("Audio engine closed while the song was loading")
            self.core.set_stems(buffers)
            self.raw_audio_data = buffers["mix"]
            self.song_length = target / self.SAMPLE_RATE
            self._tempo_pitch = (1.0, 0.0)
            self._ensure_stream()
        logger.info(f"Song duration: {self.song_length:.3f}s, stems: {list(buffers)}")
        return self.song_length, 1.0

    def _new_temp_path(self, suffix=".wav"):
        return os.path.join(self.temp_dir, f"{TEMP_FILE_PREFIX}{os.getpid()}_{uuid.uuid4().hex}{suffix}")

    def _run_ffmpeg(self, args, out_path, what):
        """Run ffmpeg; returns None if the binary is missing, raises on failure."""
        cmd = [self.ffmpeg_path, '-nostdin', '-v', 'error', '-y'] + args
        try:
            proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                  timeout=FFMPEG_DECODE_TIMEOUT_S, **_SUBPROCESS_FLAGS)
        except FileNotFoundError:
            logger.error(f"ffmpeg not found at '{self.ffmpeg_path}'")
            self._remove_files(out_path)
            return None
        except subprocess.TimeoutExpired:
            self._remove_files(out_path)
            raise RuntimeError(f"ffmpeg timed out after {FFMPEG_DECODE_TIMEOUT_S}s ({what})")
        except OSError as e:
            logger.error(f"ffmpeg could not be run: {e}")
            self._remove_files(out_path)
            return None
        stderr = proc.stderr.decode('utf-8', errors='replace').strip()
        if proc.returncode != 0:
            self._remove_files(out_path)
            logger.error(f"ffmpeg failed (rc={proc.returncode}) {what}:\n{stderr}")
            raise RuntimeError(f"ffmpeg failed (rc={proc.returncode}) {what}: {stderr}")
        if stderr:
            logger.warning(f"ffmpeg warnings: {stderr}")
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            self._remove_files(out_path)
            raise RuntimeError(f"ffmpeg produced no output ({what})")
        return out_path

    def _decode(self, path):
        """Decode ``path`` to a temp WAV; returns the WAV path."""
        out = self._new_temp_path(".wav")
        args = ['-i', path, '-vn', '-f', 'wav', '-acodec', 'pcm_s16le',
                '-ar', str(self.SAMPLE_RATE), '-ac', str(CHANNELS), out]
        result = self._run_ffmpeg(args, out, f"decoding {os.path.basename(path)}")
        if result is not None:
            self._temp_paths.append(result)
            return result
        if self._is_native_pcm_wav(path):
            logger.warning("ffmpeg unavailable; using the source WAV directly")
            return path
        raise RuntimeError(f"ffmpeg is unavailable and {path!r} is not a native PCM WAV")

    def _pad_wav(self, src_path, info, target_frames):
        """Copy a WAV, appending silence so it has ``target_frames`` frames."""
        out = self._new_temp_path(".wav")
        frame_bytes = info['channels'] * (info['bits_per_sample'] // 8)
        with open(src_path, 'rb') as f, wave.open(out, 'wb') as wf:
            wf.setnchannels(info['channels'])
            wf.setsampwidth(info['bits_per_sample'] // 8)
            wf.setframerate(info['sample_rate'])
            f.seek(info['data_offset'])
            remaining = info['frames'] * frame_bytes
            while remaining > 0:
                chunk = f.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                wf.writeframes(chunk)
                remaining -= len(chunk)
            pad = (target_frames - info['frames']) * frame_bytes
            zeros = bytes(1 << 20)
            while pad > 0:
                n = min(pad, len(zeros))
                wf.writeframes(zeros[:n])
                pad -= n
        self._temp_paths.append(out)
        return out

    @staticmethod
    def _remove_files(*paths):
        for p in paths:
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def _is_native_pcm_wav(self, path):
        """True if ``path`` is already 16-bit PCM at SAMPLE_RATE/CHANNELS."""
        try:
            info = parse_wav_data_chunk(path)
        except (OSError, ValueError):
            return False
        return (info['format_tag'] == 1 and info['bits_per_sample'] == 16
                and info['channels'] == CHANNELS and info['sample_rate'] == self.SAMPLE_RATE
                and info['frames'] > 0)

    def _map_wav(self, wav_path, info=None):
        """Memory-map the PCM payload of a 16-bit WAV at its data chunk."""
        if info is None:
            info = parse_wav_data_chunk(wav_path)
        if info['bits_per_sample'] != 16 or info['channels'] != CHANNELS \
                or info['sample_rate'] != self.SAMPLE_RATE:
            raise ValueError(
                f"Unexpected WAV format: {info['bits_per_sample']}-bit, "
                f"{info['channels']}ch, {info['sample_rate']}Hz")
        frames = info['frames']
        if frames == 0:
            raise ValueError("Decoded audio is empty")
        mm = np.memmap(wav_path, dtype=np.int16, mode='r',
                       offset=info['data_offset'], shape=(frames, CHANNELS))
        logger.debug(f"Memory map: {frames} frames at offset {info['data_offset']} "
                     f"of {os.path.basename(wav_path)}")
        return mm

    def _release_audio(self):
        """Drop buffers and delete our temp files (stream stays open)."""
        with self._render_lock:
            self._release_audio_locked()

    def _release_audio_locked(self):
        self._render_gen += 1
        self.core.clear_stems()
        self.raw_audio_data = None
        self._stem_sources = {}
        self.song_length = 0.0
        self._tempo_pitch = (1.0, 0.0)
        paths = self._temp_paths + self._render_paths + self._retired_paths
        self._temp_paths, self._render_paths, self._retired_paths = [], [], []
        # Memmaps reachable only through reference cycles would keep the
        # files locked on Windows; collect them before unlinking.
        gc.collect()
        for p in paths:
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                    logger.debug(f"Deleted temp file: {p}")
                except OSError as e:
                    logger.warning(f"Could not delete temp file {p} ({e}); "
                                   "it will be swept on next start")

    def cleanup(self):
        """Stop playback, release audio, delete temp files and close the stream."""
        logger.info("Cleaning up AudioEngine resources...")
        self.stop()
        self._release_audio()
        self._close_stream()

    def close(self):
        """Final, idempotent shutdown of playback and owned workers."""
        with self._render_lock:
            if self._closed:
                return
            self._closed = True
            self.cleanup()
            self.core.events.put(_STOP_EVENTS)
            self._callbacks.clear()
        for thread in (self._render_thread, self._event_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2.0)

    def sweep_stale_temp_files(self):
        return sweep_stale_temp_files(self.temp_dir, keep_pid=os.getpid())

    def get_raw_audio_data(self):
        """int16 memmap of the "mix" stem (frames, 2), source tempo."""
        return self.raw_audio_data

    # ---- stems

    def get_stems(self):
        return self.core.get_stems()

    def set_stem_gain(self, name, gain_db):
        self.core.set_stem_gain(name, gain_db)

    def set_stem_mute(self, name, muted):
        self.core.set_stem_mute(name, muted)

    def set_stem_solo(self, name, solo):
        self.core.set_stem_solo(name, solo)

    # =========================================================================
    # TEMPO / KEY
    # =========================================================================

    def set_tempo_pitch(self, tempo_ratio=1.0, semitones=0.0):
        with self._render_lock:
            self._request_tempo_pitch(tempo_ratio, semitones)

    def _request_tempo_pitch(self, tempo_ratio, semitones):
        tempo_ratio = max(TEMPO_MIN, min(TEMPO_MAX, float(tempo_ratio)))
        semitones = max(-SEMITONES_MAX, min(SEMITONES_MAX, float(semitones)))
        if self._closed or not self._stem_sources:
            raise RuntimeError("No open song to render")
        with self._render_lock:
            self._render_gen += 1
            gen = self._render_gen
            # Cancel a previously rendered swap that has not reached the
            # callback yet. Only the latest request may become active.
            with self.core.lock:
                self.core._pending_swap = None
        self._emit("render_started", tempo_ratio=tempo_ratio, semitones=semitones)
        if abs(tempo_ratio - 1.0) < 1e-9 and abs(semitones) < 1e-9:
            self.core.swap_buffers(self.core.get_stem_buffers(), 1.0, 0.0)
            self._retire_render_paths([])
            self._apply_swap_if_no_stream()
            return
        self._render_thread = threading.Thread(
            target=self._render_worker, args=(gen, tempo_ratio, semitones),
            name="TempoRender", daemon=True)
        self._render_thread.start()

    def get_tempo_pitch(self):
        """Applied settings, never the settings of an unfinished render."""
        with self.core.lock:
            return self.core.tempo_ratio, self.core.semitones

    def get_playback_rate(self):
        """Source seconds per playback second for the active buffers."""
        with self.core.lock:
            return self.core._scale()

    def get_output_levels(self):
        return {"peak": self.core.output_peak, "clipped_blocks": self.core.clipped_blocks}

    def _apply_swap_if_no_stream(self):
        if self._stream is None:
            self.core.apply_pending_swap()

    def _retire_render_paths(self, keep):
        """Mark rendered files not in ``keep`` for deletion (best effort)."""
        retire = [p for p in self._render_paths if p not in keep]
        self._render_paths = list(keep)
        self._retired_paths.extend(retire)
        still = []
        for p in self._retired_paths:
            try:
                if os.path.exists(p):
                    os.unlink(p)
            except OSError:
                still.append(p)    # memmap still alive; swept later
        self._retired_paths = still

    def _render_worker(self, gen, tempo_ratio, semitones):
        new_paths = []
        try:
            buffers = {}
            for name, src in list(self._stem_sources.items()):
                semi = 0.0 if is_click_stem(name) else semitones
                filt = build_tempo_pitch_filter(tempo_ratio, semi, self.SAMPLE_RATE)
                if filt is None:
                    buffers[name] = src["data"]
                    continue
                out = self._new_temp_path(".wav")
                args = ['-i', src["path"], '-vn', '-af', filt, '-f', 'wav',
                        '-acodec', 'pcm_s16le', '-ar', str(self.SAMPLE_RATE),
                        '-ac', str(CHANNELS), out]
                result = self._run_ffmpeg(args, out, f"rendering {name}")
                if result is None:
                    raise RuntimeError(f"ffmpeg not found at '{self.ffmpeg_path}'")
                if gen != self._render_gen:
                    self._remove_files(out)
                    return
                new_paths.append(out)
                buffers[name] = self._map_wav(out)
            # ffmpeg's resampler may round lengths differently per stem
            n = min(len(b) for b in buffers.values())
            if n <= 0:
                raise RuntimeError("render produced empty audio")
            buffers = {k: (b if len(b) == n else b[:n]) for k, b in buffers.items()}
            if gen != self._render_gen:
                self._remove_files(*new_paths)
                return
            with self._render_lock:
                if gen != self._render_gen or self._closed:
                    self._remove_files(*new_paths)
                    return
                self.core.swap_buffers(buffers, tempo_ratio, semitones)
                self._retire_render_paths(new_paths)
                self._apply_swap_if_no_stream()
        except Exception as e:
            logger.error(f"Tempo/pitch render failed: {e}")
            self._remove_files(*new_paths)
            if gen == self._render_gen:
                self._emit("render_failed", message=str(e))

    # =========================================================================
    # TRANSPORT
    # =========================================================================

    def play_transport(self, start_pos=None):
        self.core.play(start_pos)

    def pause_transport(self):
        self.core.pause()

    def unpause_transport(self):
        self.core.unpause()

    def seek_transport(self, position):
        self.core.seek(position)

    def stop(self):
        self.core.stop()

    def toggle_play_pause(self):
        self.core.toggle_play_pause()

    def get_position(self):
        return self.core.get_position(latency_frames=self._latency_frames)

    @property
    def is_playing(self):
        return self.core.is_playing

    @property
    def is_paused(self):
        return self.core.is_paused

    @property
    def is_held(self):
        return self.core.is_held

    @property
    def mode(self):
        return self.core.mode

    def is_transport_active(self):
        c = self.core
        return c.mode == "transport" and c.is_playing and not c.is_paused and not c.is_held

    def is_loop_active(self):
        return self.core.mode == "loop" and self.core.is_playing

    # ---- compatibility attributes used by StateManager

    @property
    def loop_in(self):
        return self.core.loop_in

    @property
    def loop_out(self):
        return self.core.loop_out

    @property
    def loop_duration(self):
        return self.core.loop_duration

    @property
    def transport_offset(self):
        """Current transport position (old engines exposed the start offset)."""
        return self.core.get_position()

    @transport_offset.setter
    def transport_offset(self, value):
        self.core.seek(value)

    # =========================================================================
    # LOOPS / VAMPS
    # =========================================================================

    def set_loop_points(self, loop_in, loop_out, crossfade_ms=LOOP_CROSSFADE_MS):
        self.core.set_loop_points(loop_in, loop_out, crossfade_ms)

    def get_loop_points(self):
        return self.core.get_loop_points()

    def is_loop_ready(self):
        return self.core.is_loop_ready()

    def arm_loop(self, loop_in, loop_out, crossfade_ms=LOOP_CROSSFADE_MS, count=None,
                 entry_fade_ms=15):
        self.core.arm_loop(loop_in, loop_out, crossfade_ms, count, entry_fade_ms)

    def disarm_loop(self):
        self.core.disarm_loop()

    def start_loop_mode(self, fade_in_ms=15, count=None):
        """Enter the loop now; loops until exit unless ``count`` is given."""
        return self.core.enter_loop(fade_in_ms, count)

    def queue_loop_exit(self, mode="cut", fade_ms=None):
        if mode == "fade" and fade_ms is None:
            fade_ms = FADE_EXIT_DURATION_MS
        self.core.queue_loop_exit(mode, fade_ms)

    def cancel_loop_exit(self):
        self.core.cancel_loop_exit()

    def execute_loop_exit(self):
        """Exit immediately, continuing from loop_out (compat)."""
        self.core.exit_loop_now()

    def execute_fade_exit(self, fade_ms=None):
        self.queue_loop_exit("fade", fade_ms)

    def is_fading_out(self):
        return self.core.is_fading_out()

    def finish_fade_exit(self):
        """No-op: the callback finalises a fade exit itself (compat)."""
        return None

    def get_loop_cycle_position(self):
        return self.core.get_loop_cycle_position()

    def get_loop_cycle_count(self):
        return self.core.get_loop_cycle_count()

    # =========================================================================
    # HOLDS / SKIPS
    # =========================================================================

    def hold_at(self, position):
        self.core.hold_at(position)

    def clear_hold(self):
        self.core.clear_hold()

    def release_hold(self):
        self.core.release_hold()

    def set_skip_regions(self, regions):
        self.core.set_skip_regions(regions)

    def perform_skip(self, target_pos):
        """Manual skip: a declicked seek that also reports ``skip_executed``."""
        from_pos = self.core.get_position()
        self.core.seek(target_pos)
        self._emit("skip_executed", from_pos=from_pos, to_pos=self.core.get_position())
