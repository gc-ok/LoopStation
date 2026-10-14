"""Shared helpers for the backend tests (synthetic audio, ffmpeg lookup, waiting)."""
import os
import shutil
import time
import wave

import numpy as np

SR = 44100
CHANNELS = 2


def find_ffmpeg():
    """LOOPSTATION_FFMPEG env var, then PATH, then imageio_ffmpeg's bundled binary, else None."""
    env = os.environ.get("LOOPSTATION_FFMPEG")
    if env and os.path.isfile(env):
        return env
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    return None


def make_signal(seconds, sr=SR, channels=CHANNELS, freq=440.0, amp=12000):
    """int16 (frames, channels) test signal; channels differ so mismatches are caught."""
    n = int(round(seconds * sr))
    t = np.arange(n) / sr
    left = np.sin(2 * np.pi * freq * t) * amp
    right = np.sin(2 * np.pi * (freq * 1.5) * t) * (amp * 0.5)
    cols = [left, right][:channels]
    data = np.stack(cols, axis=1) if channels > 1 else left[:, None]
    return np.clip(data, -32768, 32767).astype(np.int16)


def write_wav(path, data, sr=SR):
    """Write an int16 (frames, channels) array as a PCM WAV with the stdlib."""
    data = np.ascontiguousarray(data, dtype=np.int16)
    channels = data.shape[1] if data.ndim > 1 else 1
    with wave.open(str(path), 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(data.tobytes())
    return str(path)


def read_wav(path):
    """Read a 16-bit WAV back as (frames, channels) int16."""
    with wave.open(str(path), 'rb') as wf:
        channels = wf.getnchannels()
        raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype=np.int16).reshape(-1, channels)


def wait_until(predicate, timeout=2.0, interval=0.01):
    """Poll ``predicate`` until it is truthy or ``timeout`` seconds pass."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class FakeStream:
    """
    Stand-in for sounddevice.OutputStream.

    ``realtime=True`` pulls the callback from a thread at real-time pace;
    otherwise nothing runs until the test calls ``pump(frames)``, which
    renders synchronously on the calling thread (deterministic tests).

    Like sounddevice, ``finished_callback`` is called whenever the stream
    becomes inactive (stop/close, or ``fail()`` which simulates a lost
    device).
    """

    def __init__(self, callback, channels, sample_rate, block_size, realtime=False,
                 finished_callback=None):
        self.callback = callback
        self.finished_callback = finished_callback
        self.failed = False
        self.channels = channels
        self.sample_rate = sample_rate
        self.block_size = block_size
        self.realtime = realtime
        self.latency = 0.0
        self.started = False
        self.closed = False
        self.thread_ident = None
        self._stop = None
        self._thread = None

    def start(self):
        self.started = True
        if self.realtime:
            import threading
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self):
        period = self.block_size / self.sample_rate
        nxt = time.monotonic()
        while not self._stop.is_set():
            self._call(self.block_size)
            nxt += period
            delay = nxt - time.monotonic()
            if delay > 0:
                time.sleep(delay)

    @property
    def active(self):
        return self.started and not self.closed and not self.failed

    def _call(self, frames, status=None):
        import threading
        self.thread_ident = threading.get_ident()
        out = np.zeros((frames, self.channels), dtype=np.float32)
        self.callback(out, frames, None, status)
        return out

    def pump(self, frames, status=None):
        """Render ``frames`` frames in block_size chunks; returns (frames, channels).
        A lost (``fail()``-ed) stream renders nothing and returns silence."""
        chunks = []
        left = int(frames)
        while left > 0:
            n = min(self.block_size, left)
            if self.failed:
                chunks.append(np.zeros((n, self.channels), dtype=np.float32))
            else:
                chunks.append(self._call(n, status))
            left -= n
        if not chunks:
            return np.zeros((0, self.channels), dtype=np.float32)
        return np.concatenate(chunks)

    def _halt(self):
        if self._stop is not None:
            self._stop.set()
            import threading
            if self._thread is not threading.current_thread():
                self._thread.join(timeout=2.0)
        was_running = self.started and not self.failed
        self.started = False
        return was_running

    def stop(self):
        if self._halt() and self.finished_callback is not None:
            self.finished_callback()

    def close(self):
        self.stop()
        self.closed = True

    def fail(self):
        """Simulate the device disappearing: the stream finishes on its own."""
        was_running = self.started and not self.failed
        self._halt()
        self.failed = True
        if was_running and self.finished_callback is not None:
            self.finished_callback()


def fake_stream_factory(realtime=False, holder=None):
    """Factory for AudioEngine(stream_factory=...); appends streams to ``holder``."""
    def factory(callback, channels, sample_rate, block_size, finished_callback=None):
        s = FakeStream(callback, channels, sample_rate, block_size, realtime=realtime,
                       finished_callback=finished_callback)
        if holder is not None:
            holder.append(s)
        return s
    return factory
