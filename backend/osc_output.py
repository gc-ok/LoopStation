"""
OSC output for Loop Station (docs/audio-engine-api.md section 4).

Sends show events over UDP using python-osc. Two address schemes:

- "loopstation": ``/loopstation/<name> <args...>`` for every event and
  ``/loopstation/cue <osc_cue>`` for fired cues.
- "qlab": only fired cues are sent, as ``/cue/<osc_cue>/start`` (events are
  swallowed, since QLab would not understand them).

Cue numbers go through ``sanitize_osc_cue`` (no OSC pattern characters or
whitespace), so a cue typed as "1/2" or "*" can never address other cues.
UDP gives no delivery receipt: a successful send only means "sent".

Sending never blocks or raises into the caller: send_event / send_cue only
append to a bounded queue (oldest dropped when full) that a daemon worker
drains. The worker resolves the host once per configuration (with a short
timeout) and sends to the resolved address; a bad host, a closed socket or a
serialisation problem is logged, kept in ``last_error`` and dropped.
``flush()`` waits for the queue to drain. Settings persist in the user
preferences under the "osc" key.
"""

import collections
import logging
import socket
import threading
import time
from typing import Any, Callable, Optional, Tuple

from config import (
    OSC_DEFAULT_ENABLED, OSC_DEFAULT_HOST, OSC_DEFAULT_PORT, OSC_DEFAULT_SCHEME,
)
from utils import preferences

try:
    from pythonosc.udp_client import SimpleUDPClient
except Exception:  # pragma: no cover - python-osc missing
    SimpleUDPClient = None

logger = logging.getLogger("LoopStation.OSC")

SCHEMES = ("loopstation", "qlab")
PREFS_KEY = "osc"

# Outgoing messages wait here for the worker thread; when full the OLDEST
# message is dropped (a late show event is worth less than a fresh one).
QUEUE_MAX = 256
# Host name resolution runs on the worker and gives up after this long.
RESOLVE_TIMEOUT_S = 1.0
# After a failed resolution, wait this long before trying again.
RESOLVE_RETRY_S = 5.0


# Characters with a meaning in OSC address patterns (and the path
# separator): a cue number containing one would address other cues.
_OSC_UNSAFE = set("/*?[]{}#,")


def sanitize_osc_cue(value) -> str:
    """
    A cue number that is safe inside ``/cue/<cue>/start``: ``str(value)``
    without surrounding whitespace, with every OSC pattern character
    (``/ * ? [ ] { } # ,``), whitespace and control character removed.
    "" for None (or when nothing usable is left).
    """
    if value is None:
        return ""
    return "".join(ch for ch in str(value).strip()
                   if ch not in _OSC_UNSAFE and not ch.isspace()
                   and ord(ch) >= 0x20 and ord(ch) != 0x7f)


def _resolve(host: str, port: int, timeout: float = RESOLVE_TIMEOUT_S) -> str:
    """
    Resolve ``host`` to an IP address string, giving up after ``timeout``.
    socket.getaddrinfo has no timeout of its own, so it runs on a helper
    thread that is abandoned (daemon) if it hangs. IPv4 is preferred: the
    client socket is IPv4-capable everywhere, and "localhost" often lists
    ::1 first while QLab / consoles usually listen on IPv4 only.
    """
    result: dict = {}

    def _lookup():
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
            v4 = [i for i in infos if i[0] == socket.AF_INET]
            result["ip"] = (v4 or infos)[0][4][0]
        except Exception as e:
            result["error"] = e

    t = threading.Thread(target=_lookup, name="osc-resolve", daemon=True)
    t.start()
    t.join(timeout)
    if "ip" in result:
        return result["ip"]
    if "error" in result:
        raise OSError(f"cannot resolve {host!r}: {result['error']}")
    raise OSError(f"resolving {host!r} timed out")


def _osc_arg(value: Any):
    """Coerce a Python value to something python-osc can encode."""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float, str, bytes)):
        return value
    if value is None:
        return ""
    return str(value)


class OscOutput:
    """
    Fire-and-forget OSC sender. ``send_event`` / ``send_cue`` only enqueue
    (O(1), never block, never raise); a daemon worker resolves the host once
    per configuration and does the actual UDP sends.
    """

    def __init__(self, client_factory: Optional[Callable[[str, int], Any]] = None,
                 queue_max: int = QUEUE_MAX):
        self.enabled: bool = OSC_DEFAULT_ENABLED
        self.host: str = OSC_DEFAULT_HOST
        self.port: int = OSC_DEFAULT_PORT
        self.scheme: str = OSC_DEFAULT_SCHEME
        self._client = None
        self._client_factory = client_factory
        self._lock = threading.Lock()
        self.last_error: Optional[str] = None

        self._queue: "collections.deque" = collections.deque()
        self._queue_max = max(1, int(queue_max))
        self._cond = threading.Condition(self._lock)
        self._worker: Optional[threading.Thread] = None
        self._busy = False                 # worker is sending one message
        self._config_gen = 0               # bumped when host/port change
        self._resolved: Optional[Tuple[int, str]] = None   # (gen, ip)
        self._resolve_failed: Optional[Tuple[int, float]] = None  # (gen, when)
        self._drop_logged = False
        self.dropped = 0
        self._closed = False

    # ------------------------------------------------------------ config --

    def configure(self, enabled: bool, host: str, port: int, scheme: str = "loopstation") -> None:
        scheme = scheme if scheme in SCHEMES else OSC_DEFAULT_SCHEME
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = OSC_DEFAULT_PORT
        host = str(host or OSC_DEFAULT_HOST)
        with self._lock:
            if self._closed:
                return
            changed = (host != self.host or port != self.port)
            self.enabled = bool(enabled)
            self.host = host
            self.port = port
            self.scheme = scheme
            if changed:
                self._config_gen += 1
                self._client = None
                self._resolved = None
                self._resolve_failed = None
            if self.enabled:
                self._ensure_worker()
                # Resolve the host now (on the worker), not on the first cue
                self._queue.append(None)
                self._cond.notify()
        logger.info(f"OSC configured: enabled={self.enabled} {self.host}:{self.port} scheme={self.scheme}")

    def settings(self) -> dict:
        return {"enabled": self.enabled, "host": self.host, "port": self.port,
                "scheme": self.scheme}

    def load_settings(self) -> dict:
        """Apply the "osc" block of the user preferences (if any)."""
        data = preferences.load_preferences().get(PREFS_KEY)
        if isinstance(data, dict):
            self.configure(
                data.get("enabled", OSC_DEFAULT_ENABLED),
                data.get("host", OSC_DEFAULT_HOST),
                data.get("port", OSC_DEFAULT_PORT),
                data.get("scheme", OSC_DEFAULT_SCHEME),
            )
        return self.settings()

    def save_settings(self) -> dict:
        data = self.settings()
        try:
            preferences.save_preferences({PREFS_KEY: data})
        except Exception as e:  # never raise into callers
            logger.error(f"Could not save OSC settings: {e}")
        return data

    # ------------------------------------------------------------- sending --

    def _ensure_worker(self) -> None:
        """Start the sender thread if needed (caller holds self._lock)."""
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._run, name="osc-send", daemon=True)
            self._worker.start()

    def _send(self, address: str, args: list) -> bool:
        """Queue one message. True if queued; never blocks on the network."""
        if not self.enabled:
            return False
        if self._client_factory is None and SimpleUDPClient is None:
            self.last_error = "python-osc is not installed"
            return False
        payload = [_osc_arg(a) for a in args]
        with self._cond:
            if self._closed:
                return False
            if len(self._queue) >= self._queue_max:
                self._queue.popleft()
                self.dropped += 1
                if not self._drop_logged:
                    self._drop_logged = True
                    logger.warning(f"OSC queue full ({self._queue_max}); dropping oldest messages")
            self._queue.append((address, payload))
            self._ensure_worker()
            self._cond.notify()
        return True

    def flush(self, timeout: float = 2.0) -> bool:
        """Wait until every queued message was handled (tests / settings 'Test')."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._queue or self._busy:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(min(left, 0.05))
        return True

    # ---- worker side ----

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._closed:
                    self._cond.wait(1.0)
                if self._closed:
                    return
                item = self._queue.popleft()
                self._busy = True
            try:
                if item is None:
                    self._prepare()
                else:
                    self._deliver(*item)
            except Exception:  # pragma: no cover - _deliver catches everything
                logger.exception("OSC worker error")
            finally:
                with self._cond:
                    self._busy = False
                    self._cond.notify_all()

    def close(self) -> None:
        """Stop accepting messages, discard pending sends, and join the worker."""
        with self._cond:
            self._closed = True
            self.enabled = False
            self._queue.clear()
            self._cond.notify_all()
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2.0)

    def _prepare(self) -> None:
        """(Worker) resolve the configured host ahead of the first send."""
        try:
            self._get_client()
        except Exception as e:
            self.last_error = str(e)
            logger.warning(f"OSC host not ready: {e}")

    def _get_client(self):
        """(Worker) the UDP client for the current host, resolving it once."""
        with self._lock:
            gen, host, port = self._config_gen, self.host, self.port
            if self._client is not None and self._resolved and self._resolved[0] == gen:
                return self._client
            failed = self._resolve_failed
            resolved = self._resolved if self._resolved and self._resolved[0] == gen else None
        if resolved is None:
            if failed and failed[0] == gen and time.monotonic() - failed[1] < RESOLVE_RETRY_S:
                raise OSError(self.last_error or f"cannot resolve {host!r}")
            try:
                ip = _resolve(host, port)
            except Exception:
                with self._lock:
                    if gen == self._config_gen:
                        self._resolve_failed = (gen, time.monotonic())
                raise
            resolved = (gen, ip)
        factory = self._client_factory or SimpleUDPClient
        if factory is None:
            raise RuntimeError("python-osc is not installed")
        client = factory(resolved[1], port)
        with self._lock:
            if gen == self._config_gen:
                self._resolved = resolved
                self._resolve_failed = None
                self._client = client
        return client

    def _deliver(self, address: str, args: list) -> None:
        try:
            client = self._get_client()
            client.send_message(address, args)
            self.last_error = None
        except Exception as e:
            self.last_error = str(e)
            logger.warning(f"OSC send failed ({address}): {e}")
            with self._lock:
                self._client = None  # rebuild on the next attempt

    def send_event(self, event: str, /, **kw) -> bool:
        """loopstation scheme: /loopstation/<name> with kw values as args (in order)."""
        if self.scheme != "loopstation":
            return False
        return self._send(f"/loopstation/{event}", list(kw.values()))

    def send_cue(self, osc_cue: str) -> bool:
        """
        qlab: /cue/<osc_cue>/start ; loopstation: /loopstation/cue <osc_cue>.
        The cue number is passed through sanitize_osc_cue first; nothing is
        sent when it is empty afterwards.
        """
        osc_cue = sanitize_osc_cue(osc_cue)
        if not osc_cue:
            return False
        if self.scheme == "qlab":
            return self._send(f"/cue/{osc_cue}/start", [])
        return self._send("/loopstation/cue", [osc_cue])
