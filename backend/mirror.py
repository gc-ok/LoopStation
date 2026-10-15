"""
Main / backup mirroring over the LAN.

Industry practice (QLab main + backup): two machines play the same show, both
feed the console, and the A2 switches to the backup if the main dies. Here
the MAIN machine sends every discrete show action as it happens plus a state
heartbeat; each BACKUP applies the actions to its own StateManager and keeps
its playhead within a few tens of milliseconds of the main.

Attach: ``StateManager.mirror`` is a MirrorLink, constructed idle.

Public API (used by the UI):
    link.start(role, peer_host=None, port=DEFAULT_PORT, pin="") -> bool
        role "main" (TCP server on all IPv4 interfaces) or "backup" (connects
        to peer_host:port with auto-reconnect). Saves role/peer_host/port/pin
        to preferences key "mirror". Returns False on bad arguments / when
        the main cannot listen (status message says why).
    link.stop()                 -> idle (role "off"); never touches playback
    link.take_over()            -> backup stops following and runs on its own
    link.status() -> dict       {role: "off"|"main"|"backup", connected: bool,
                                 peer: str|None, peers: int, following: bool,
                                 drift_ms: float|None, last_seen_s: float|None,
                                 message: str, port: int|None,
                                 show_match: "same"|"files"|"differs"|None}
    link.on_status              callable(status_dict) or None, called on the
                                mirror's notifier thread whenever the status
                                changes (drift / last_seen at most 1/s)
    link.on_loaded              callable(path) or None: the backup loaded a
                                song because the main did (applier thread;
                                return quickly -- e.g. queue the waveform)
    link.bound_port             the port the main actually listens on
    load_settings() / save_settings(**kw)   preferences "mirror" (module level)
    link.auto_start()           start the saved role if "auto_start" is True

Protocol (newline-delimited UTF-8 JSON objects, each with "type"; lines are
capped at MAX_LINE bytes, oversized / malformed / unknown messages ignored):
    backup -> main  hello {proto, pin, client}       first line
    main -> backup  welcome {proto, show, live_mode, pin_set}
                    reject {reason: "bad_pin"|"proto"|"full"}
    main -> backup  act {kind, seq, t, ...}          discrete action (never dropped;
                    a backup whose queue overflows is disconnected)
                    hb {seq, t, entry_id, name, state, pos, mode, held, exiting,
                        previewing, rate, live, cues}  every HEARTBEAT_S (latest only)
                    show {show}                       show identity changed
    backup -> main  ping {t}   ->   main -> backup pong {t}   (RTT)

Act kinds: go (info: action, position), play, play_from (position), pause
(position), stop, seek (position), queue_exit (fade, fade_ms), release_hold,
live_mode (enabled), next_up (entry_id, file), tempo (tempo_ratio, semitones),
load (entry_id, file).

Threads: main = accept thread + per-backup reader and writer + heartbeat
thread; backup = link thread (connect/read) + tick thread (ping / main-lost)
+ ONE applier thread that makes every StateManager call, in arrival order
(StateManager is used from several threads already: Tk, song loading, the
monitor and the engine's event thread; the applier is one more serial caller,
like the UI's load thread). Nothing here runs on the Tk thread or the audio
callback, and the main's hook (state._mirror_sink) only appends to bounded
per-backup queues.

Mirrored actions are applied inside ``state._mirror_quiet()``, which skips
the Live Mode locks for the internal entry points used here and stops the
backup from re-emitting them.
"""

import hmac
import json
import logging
import math
import os
import queue
import socket
import threading
import time
import unicodedata
import hashlib
from collections import deque
from typing import Any, Callable, Dict, List, Optional

from utils import preferences

logger = logging.getLogger("LoopStation.Mirror")

PROTO = 1
DEFAULT_PORT = 53100
PREF_KEY = "mirror"
MAX_LINE = 64 * 1024            # bytes per JSON line
MAX_PEERS = 8                   # backups per main
MAX_ACTION_QUEUE = 512          # pending discrete actions per backup
MAX_SHOW_ENTRIES = 300          # entries in the show identity
HANDSHAKE_TIMEOUT_S = 3.0
CONNECT_TIMEOUT_S = 2.0
SOCKET_TIMEOUT_S = 2.0          # per send / recv wait on the main's peer sockets
PEER_TIMEOUT_S = 5.0            # main drops a backup silent this long (no pings)
RECONNECT_DELAYS = (0.5, 1.0, 2.0)
ROLES = ("off", "main", "backup")

# Not notified on every change (they change constantly)
_NOISY_KEYS = ("drift_ms", "last_seen_s")


# ------------------------------------------------------------ preferences --

def _default_settings() -> dict:
    return {"role": "off", "peer_host": "", "port": DEFAULT_PORT, "pin": "", "auto_start": False}


def load_settings() -> dict:
    """Saved mirror settings (validated; defaults for anything missing / bad)."""
    out = _default_settings()
    try:
        saved = preferences.load_preferences().get(PREF_KEY)
    except Exception:
        saved = None
    if not isinstance(saved, dict):
        return out
    if saved.get("role") in ROLES:
        out["role"] = saved["role"]
    if isinstance(saved.get("peer_host"), str):
        out["peer_host"] = saved["peer_host"][:255]
    port = saved.get("port")
    if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
        out["port"] = port
    if isinstance(saved.get("pin"), str):
        out["pin"] = saved["pin"][:64]
    out["auto_start"] = saved.get("auto_start") is True
    return out


def save_settings(**changes) -> None:
    """Merge ``changes`` (role, peer_host, port, pin, auto_start) into preferences."""
    allowed = {k: v for k, v in changes.items() if k in _default_settings()}
    if allowed:
        preferences.save_preferences({PREF_KEY: allowed})


# ---------------------------------------------------------------- helpers --

def _num(value, lo: Optional[float] = None, hi: Optional[float] = None) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    if not math.isfinite(f):
        return None
    if (lo is not None and f < lo) or (hi is not None and f > hi):
        return None
    return f


def _str(value, limit: int = 1024) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        return None
    return value


def _name_key(name: Optional[str]) -> str:
    """File-name comparison key: NFC (macOS hands out NFD), case-insensitive."""
    return unicodedata.normalize("NFC", name or "").casefold()


def _reject_constant(_name):
    raise ValueError("non-finite number")


def encode(msg: dict) -> bytes:
    return (json.dumps(msg, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def decode(line: bytes) -> Optional[dict]:
    """A message dict with a str "type", or None for anything malformed."""
    if len(line) > MAX_LINE:
        return None
    try:
        msg = json.loads(line.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        return None
    return msg


class LineReader:
    """Splits a byte stream into lines; a line longer than ``cap`` is dropped whole."""

    def __init__(self, cap: int = MAX_LINE):
        self.cap = cap
        self.buf = bytearray()
        self.discarding = False
        self.dropped = 0

    def feed(self, data: bytes) -> List[bytes]:
        lines = []
        self.buf.extend(data)
        while True:
            idx = self.buf.find(b"\n")
            if idx < 0:
                break
            line = bytes(self.buf[:idx])
            del self.buf[:idx + 1]
            if self.discarding:
                self.discarding = False
                continue
            if len(line) > self.cap:
                self.dropped += 1
                continue
            if line.strip():
                lines.append(line)
        if len(self.buf) > self.cap:
            self.buf.clear()
            self.discarding = True
            self.dropped += 1
        return lines


def _tune_socket(sock: socket.socket) -> None:
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        pass


def _close_socket(sock: Optional[socket.socket]) -> None:
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def show_identity(snapshot: dict) -> dict:
    """{"name", "entries": [{"id", "kind", "file"}], "truncated"} -- no paths."""
    entries = []
    for e in (snapshot.get("entries") or [])[:MAX_SHOW_ENTRIES]:
        path = e.get("path") or ""
        entries.append({"id": str(e.get("id") or "")[:64], "kind": e.get("kind"),
                        "file": os.path.basename(path)[:255] if path else ""})
    return {"name": str(snapshot.get("name") or "")[:255], "entries": entries,
            "truncated": len(snapshot.get("entries") or []) > MAX_SHOW_ENTRIES}


def compare_shows(main_id: Optional[dict], local_id: dict) -> Optional[str]:
    """"same" (ids + files), "files" (same song files in order), "differs", None (unknown)."""
    if not isinstance(main_id, dict) or not isinstance(main_id.get("entries"), list):
        return None
    try:
        a = [(str(e.get("id")), e.get("kind"), _name_key(e.get("file")))
             for e in main_id["entries"] if isinstance(e, dict)]
    except Exception:
        return None
    b = [(e["id"], e["kind"], _name_key(e["file"])) for e in local_id["entries"]]
    if a == b:
        return "same"
    songs_a = [f for _, k, f in a if k == "song"]
    songs_b = [f for _, k, f in b if k == "song"]
    return "files" if songs_a == songs_b else "differs"


def cue_signature(state) -> str:
    """Short hash of the loaded song's vamps / holds / skips / tempo (detect differing cue data)."""
    try:
        data = {
            "loops": [[round(lp.start, 3), round(lp.end, 3), bool(lp.active),
                       getattr(lp, "repeat_count", None)] for lp in list(state.loops)],
            "holds": [round(h.time, 3) for h in list(state.holds)],
            "skips": [[round(s.start, 3), round(s.end, 3), bool(s.active)] for s in list(state.skips)],
            "tempo": [round(state.settings.tempo_ratio, 4), round(state.settings.semitones, 3)],
        }
        return hashlib.md5(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    except Exception:
        return ""


# ------------------------------------------------------------------ peers --

class _Peer:
    """One connected backup, seen from the main."""

    def __init__(self, link: "MirrorLink", sock: socket.socket, addr):
        self.link = link
        self.sock = sock
        self.addr = f"{addr[0]}:{addr[1]}" if addr else "?"
        self.host = addr[0] if addr else "?"
        self.cond = threading.Condition()
        self.actions: deque = deque()
        self.hb: Optional[bytes] = None
        self.alive = True
        self.last_rx = link._clock()

    def push_action(self, data: bytes, front: bool = False) -> bool:
        with self.cond:
            if not self.alive:
                return False
            if len(self.actions) >= MAX_ACTION_QUEUE:
                return False
            if front:
                self.actions.appendleft(data)
            else:
                self.actions.append(data)
            self.cond.notify()
            return True

    def push_hb(self, data: bytes) -> None:
        with self.cond:
            if self.alive:
                self.hb = data          # an unsent older heartbeat is dropped
                self.cond.notify()

    def close(self) -> None:
        with self.cond:
            if not self.alive:
                return
            self.alive = False
            self.cond.notify_all()
        _close_socket(self.sock)

    def writer(self) -> None:
        while True:
            with self.cond:
                while self.alive and not self.actions and self.hb is None:
                    self.cond.wait(0.5)
                if not self.alive:
                    return
                data = b"".join(self.actions) + (self.hb or b"")
                self.actions.clear()
                self.hb = None
            try:
                self.sock.sendall(data)
            except OSError as e:     # timeout, reset (WSAECONNRESET), broken pipe
                self.link._drop_peer(self, f"send failed: {e}")
                return


# ------------------------------------------------------------------- link --

class MirrorLink:
    def __init__(self, state, clock: Callable[[], float] = time.monotonic,
                 heartbeat_s: float = 0.2, lost_s: float = 1.0, ping_s: float = 1.0,
                 guard_s: float = 1.0, drift_threshold_ms: float = 40.0,
                 confirm: int = 2, tick_s: float = 0.05):
        self.state = state
        self._clock = clock
        self.heartbeat_s = heartbeat_s
        self.lost_s = lost_s
        self.ping_s = ping_s
        self.guard_s = guard_s
        self.drift_threshold_ms = drift_threshold_ms
        self.confirm = max(1, int(confirm))
        self.tick_s = tick_s

        self.on_status: Optional[Callable[[dict], None]] = None
        self.on_loaded: Optional[Callable[[str], None]] = None

        self._lock = threading.RLock()
        self._gen = 0
        self._stop_event = threading.Event()
        self._stop_event.set()
        self._threads: List[threading.Thread] = []
        self.role = "off"
        self.pin = ""
        self.port: Optional[int] = None
        self.peer_host: Optional[str] = None
        self.bound_port: Optional[int] = None

        # main
        self._server: Optional[socket.socket] = None
        self._peers: List[_Peer] = []
        self._seq = 0
        self._hooked = False

        # backup
        self._sock: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self.following = False
        self.connected = False
        self._ever_connected = False
        self._rejected: Optional[str] = None
        self._last_rx: Optional[float] = None
        self._last_ping = -1e9
        self._rtt: Optional[float] = None
        self._show_match: Optional[str] = None
        self._main_show: Optional[dict] = None
        self._apply_cond = threading.Condition()
        self._actions: deque = deque()
        self._hb: Optional[dict] = None
        self._last_action_t = -1e9
        self._mismatch_kind: Optional[str] = None
        self._mismatch_count = 0
        self._failed_load: Any = None
        self._missing: Optional[str] = None
        self._cues_differ: Optional[str] = None
        self._pending_release_until = -1e9
        self.drift_ms: Optional[float] = None
        self._took_over = False

        # status notification
        self._status: Dict[str, Any] = {}
        self._last_noisy_notify = -1e9
        self._notify_q: "queue.Queue" = queue.Queue()
        self._notifier: Optional[threading.Thread] = None
        self._error: Optional[str] = None

    # ================================================================ API --

    def start(self, role: str, peer_host: Optional[str] = None, port: int = DEFAULT_PORT,
              pin: str = "") -> bool:
        """Start as "main" or "backup" (stopping any current role first)."""
        self.stop()
        pin = pin if isinstance(pin, str) else ""
        if role not in ("main", "backup"):
            self._error = f"Unknown mirror role {role!r}"
            self._update_status()
            return False
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535 \
                or (role == "backup" and port == 0):
            self._error = f"Invalid port {port!r}"
            self._update_status()
            return False
        host = (peer_host or "").strip()
        if role == "backup" and not host:
            self._error = "Enter the main computer's address"
            self._update_status()
            return False
        try:
            save_settings(role=role, peer_host=host if role == "backup" else
                          load_settings()["peer_host"], port=port or DEFAULT_PORT, pin=pin)
        except Exception as e:
            logger.warning(f"Could not save mirror settings: {e}")
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._stop_event = threading.Event()
            self._error = None
            self.pin = pin
            self.port = port
            self.peer_host = host or None
            self._threads = []
        if role == "main":
            return self._start_main(gen, port)
        return self._start_backup(gen, host, port)

    def stop(self) -> None:
        """Stop mirroring (idle). Never stops or changes playback."""
        with self._lock:
            was = self.role
            self._gen += 1
            self._stop_event.set()
            server, self._server = self._server, None
            peers, self._peers = list(self._peers), []
            sock, self._sock = self._sock, None
            self.role = "off"
            self.following = False
            self.connected = False
            self.bound_port = None
            self._reset_backup_runtime()
            threads, self._threads = self._threads, []
        self._unhook()
        self._unhook_backup()
        _close_socket(server)
        for p in peers:
            p.close()
        _close_socket(sock)
        with self._apply_cond:
            self._actions.clear()
            self._hb = None
            self._apply_cond.notify_all()
        self._join(threads)
        if was != "off":
            logger.info(f"Mirror stopped (was {was})")
        self._update_status()

    def take_over(self) -> bool:
        """Backup: stop following the main and act independently (playback untouched)."""
        with self._lock:
            if self.role != "backup":
                return False
            self._gen += 1             # the link / tick threads exit
            self._stop_event.set()
            sock, self._sock = self._sock, None
            self.following = False
            self.connected = False
            self._took_over = True
            threads, self._threads = self._threads, []
        self._unhook_backup()
        _close_socket(sock)
        with self._apply_cond:
            self._actions.clear()
            self._hb = None
            self._apply_cond.notify_all()
        self._join(threads)
        logger.warning("Mirror: backup took over")
        self._update_status()
        return True

    @staticmethod
    def _join(threads, total_s: float = 1.0) -> None:
        """Join worker threads, never waiting more than ``total_s`` overall."""
        me = threading.current_thread()
        deadline = time.monotonic() + total_s
        for t in threads:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            if t is not me and t.is_alive():
                t.join(timeout=left)

    def _unhook_backup(self) -> None:
        try:
            self.state.off("hold_reached", self._on_backup_hold_reached)
        except Exception:
            pass

    def auto_start(self) -> bool:
        """Start the saved role when preferences say "auto_start": true."""
        s = load_settings()
        if not s["auto_start"] or s["role"] == "off":
            return False
        return self.start(s["role"], s["peer_host"], s["port"], s["pin"])

    def is_following(self) -> bool:
        """Backup following a connected main (local auto-advance is suppressed)."""
        return self.role == "backup" and self.following and self.connected

    def status(self) -> dict:
        with self._lock:
            return self._build_status()

    def cleanup(self) -> None:
        self.stop()
        self._notify_q.put(None)

    # ============================================================ status --

    def _build_status(self) -> dict:
        now = self._clock()
        role = self.role
        peers = len(self._peers) if role == "main" else (1 if self.connected else 0)
        if role == "main":
            peer = ", ".join(p.addr for p in self._peers) or None
            connected = bool(self._peers)
        else:
            peer = f"{self.peer_host}:{self.port}" if role == "backup" and self.peer_host else None
            connected = self.connected
        last_seen = None
        if role == "backup" and self._last_rx is not None:
            last_seen = round(max(0.0, now - self._last_rx), 1)
        return {
            "role": role, "connected": connected, "peer": peer, "peers": peers,
            "following": bool(self.following) if role == "backup" else False,
            "drift_ms": None if self.drift_ms is None else round(self.drift_ms, 1),
            "last_seen_s": last_seen, "message": self._message(),
            "port": self.bound_port if role == "main" else self.port,
            "show_match": self._show_match if role == "backup" else None,
        }

    def _message(self) -> str:
        if self.role == "off":
            return self._error or "Mirroring off"
        if self.role == "main":
            if self._error:
                return self._error
            n = len(self._peers)
            msg = f"Main: listening on port {self.bound_port} - {n} backup{'s' if n != 1 else ''} connected"
            if not self.pin:
                msg += " | no PIN set (any computer on the network can follow)"
            return msg
        # backup
        if self._took_over:
            base = "Took over - running independently"
        elif self._rejected:
            base = self._rejected
        elif self.connected:
            base = "Following main"
        elif self._ever_connected:
            base = "MAIN LOST - backup keeps playing; reconnecting"
        else:
            base = f"Connecting to main {self.peer_host}:{self.port}..."
        extras = []
        if self.following and self._show_match == "differs":
            extras.append("show differs from main (following by file name)")
        if self.following and self._missing:
            extras.append(f"missing on backup: {self._missing}")
        if self.following and self._cues_differ:
            extras.append(f"cue data differs from main for {self._cues_differ}")
        if not self.pin and not self._took_over:
            extras.append("no PIN set")
        return " | ".join([base] + extras)

    def _update_status(self) -> None:
        with self._lock:
            status = self._build_status()
            now = self._clock()
            old = self._status
            quiet_old = {k: v for k, v in old.items() if k not in _NOISY_KEYS}
            quiet_new = {k: v for k, v in status.items() if k not in _NOISY_KEYS}
            if quiet_old == quiet_new:
                if all(old.get(k) == status.get(k) for k in _NOISY_KEYS):
                    return
                if now - self._last_noisy_notify < 1.0:
                    return
            self._status = status
            self._last_noisy_notify = now
        if self.on_status is None:
            return
        self._ensure_notifier()
        self._notify_q.put(dict(status))

    def _ensure_notifier(self) -> None:
        with self._lock:
            if self._notifier is not None and self._notifier.is_alive():
                return
            self._notifier = threading.Thread(target=self._notify_loop, name="mirror-status",
                                              daemon=True)
            self._notifier.start()

    def _notify_loop(self) -> None:
        while True:
            item = self._notify_q.get()
            if item is None:
                return
            cb = self.on_status
            if cb is None:
                continue
            try:
                cb(item)
            except Exception:
                logger.exception("mirror on_status callback failed")

    def _spawn(self, target, name: str, *args) -> threading.Thread:
        t = threading.Thread(target=target, args=args, name=name, daemon=True)
        with self._lock:
            self._threads = [x for x in self._threads if x.is_alive()]
            self._threads.append(t)
        t.start()
        return t

    def _alive(self, gen: int) -> bool:
        return gen == self._gen and not self._stop_event.is_set()

    # ============================================================== MAIN --

    def _start_main(self, gen: int, port: int) -> bool:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name == "nt":
                # Windows: SO_REUSEADDR would let a second server share the
                # port; ask for an exclusive bind instead.
                excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
                if excl is not None:
                    server.setsockopt(socket.SOL_SOCKET, excl, 1)
            else:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("0.0.0.0", port))
            server.listen(MAX_PEERS)
            server.settimeout(0.1)
        except OSError as e:
            _close_socket(server)
            msg = f"Could not listen on port {port}: {e.strerror or e}"
            logger.error(f"Mirror main: {msg}")
            with self._lock:
                self._error = msg
                self.role = "off"
            self._update_status()
            return False
        with self._lock:
            self._server = server
            self.bound_port = server.getsockname()[1]
            self.role = "main"
            self._seq = 0
        self._hook()
        self._spawn(self._accept_loop, "mirror-accept", gen, server)
        self._spawn(self._heartbeat_loop, "mirror-heartbeat", gen)
        logger.info(f"Mirror main listening on port {self.bound_port}")
        self._update_status()
        return True

    def _hook(self) -> None:
        self.state._mirror_sink = self._on_local_action
        try:
            self.state.on("show_changed", self._on_show_changed)
        except Exception:
            pass
        self._hooked = True

    def _unhook(self) -> None:
        if not self._hooked:
            return
        if getattr(self.state, "_mirror_sink", None) == self._on_local_action:
            self.state._mirror_sink = None
        try:
            self.state.off("show_changed", self._on_show_changed)
        except Exception:
            pass
        self._hooked = False

    def _accept_loop(self, gen: int, server: socket.socket) -> None:
        while self._alive(gen):
            try:
                sock, addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._alive(gen):
                    time.sleep(0.1)
                    continue
                return
            if not self._alive(gen):
                _close_socket(sock)
                return
            self._spawn(self._peer_reader, "mirror-peer", gen, sock, addr)

    def _peer_reader(self, gen: int, sock: socket.socket, addr) -> None:
        _tune_socket(sock)
        peer = _Peer(self, sock, addr)
        reader = LineReader()
        try:
            sock.settimeout(HANDSHAKE_TIMEOUT_S)
            hello = None
            deadline = self._clock() + HANDSHAKE_TIMEOUT_S
            while hello is None and self._clock() < deadline and self._alive(gen):
                data = sock.recv(65536)
                if not data:
                    raise ConnectionError("closed during handshake")
                for line in reader.feed(data):
                    msg = decode(line)
                    if msg is not None and msg.get("type") == "hello":
                        hello = msg
                        break
            if hello is None:
                raise ConnectionError("no hello")
            reason = None
            if hello.get("proto") != PROTO:
                reason = "proto"
            else:
                pin = hello.get("pin")
                pin = pin if isinstance(pin, str) and len(pin) <= 256 else "\x00invalid"
                if not hmac.compare_digest(pin.encode("utf-8"), self.pin.encode("utf-8")):
                    reason = "bad_pin"
            with self._lock:
                if reason is None and len(self._peers) >= MAX_PEERS:
                    reason = "full"
            if reason is not None:
                logger.warning(f"Mirror main: rejected {peer.addr} ({reason})")
                time.sleep(0.2)   # slow down PIN guessing a little
                try:
                    sock.sendall(encode({"type": "reject", "reason": reason}))
                except OSError:
                    pass
                _close_socket(sock)
                return
            welcome = {"type": "welcome", "proto": PROTO,
                       "show": show_identity(self.state.show_snapshot()),
                       "live_mode": bool(self.state.live_mode), "pin_set": bool(self.pin)}
            sock.sendall(encode(welcome))
            sock.settimeout(SOCKET_TIMEOUT_S)
        except (OSError, ConnectionError, ValueError) as e:
            logger.info(f"Mirror main: handshake with {peer.addr} failed: {e}")
            _close_socket(sock)
            return
        with self._lock:
            if not self._alive(gen):
                _close_socket(sock)
                return
            self._peers.append(peer)
        logger.info(f"Mirror main: backup {peer.addr} connected")
        self._spawn(peer.writer, "mirror-peer-writer")
        peer.push_hb(self._heartbeat_bytes())
        self._update_status()
        try:
            while peer.alive and self._alive(gen):
                try:
                    data = sock.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    break
                peer.last_rx = self._clock()
                for line in reader.feed(data):
                    msg = decode(line)
                    if msg is not None and msg.get("type") == "ping":
                        t = _num(msg.get("t"))
                        if t is not None:
                            if not peer.push_action(encode({"type": "pong", "t": t}), front=True):
                                break
        except OSError as e:   # ConnectionResetError covers WSAECONNRESET
            logger.info(f"Mirror main: backup {peer.addr} read error: {e}")
        self._drop_peer(peer, "disconnected")

    def _drop_peer(self, peer: _Peer, reason: str) -> None:
        with self._lock:
            known = peer in self._peers
            if known:
                self._peers.remove(peer)
        peer.close()
        if known:
            logger.info(f"Mirror main: backup {peer.addr} {reason}")
            self._update_status()

    def _broadcast(self, msg: dict) -> None:
        data = encode(msg)
        if len(data) > MAX_LINE:
            logger.error(f"Mirror: message {msg.get('type')}/{msg.get('kind')} too large; not sent")
            return
        with self._lock:
            peers = list(self._peers)
        for p in peers:
            if not p.push_action(data):
                self._drop_peer(p, "too slow (action queue full); disconnected")

    def _on_local_action(self, kind: str, data: dict) -> None:
        """StateManager hook (any thread): queue a discrete action for every backup."""
        if self.role != "main":
            return
        with self._lock:
            self._seq += 1
            seq = self._seq
            has_peers = bool(self._peers)
        if not has_peers:
            return
        msg = {"type": "act", "kind": kind, "seq": seq, "t": self._clock()}
        for k, v in data.items():
            if isinstance(v, float) and not math.isfinite(v):
                v = None
            msg[k] = v
        self._broadcast(msg)

    def _on_show_changed(self, snapshot) -> None:
        if self.role != "main" or not isinstance(snapshot, dict):
            return
        self._broadcast({"type": "show", "show": show_identity(snapshot)})

    def _heartbeat_msg(self) -> dict:
        st = self.state
        audio = st.audio
        try:
            pos = float(audio.get_position())
        except Exception:
            pos = 0.0
        try:
            rate = float(st.get_playback_rate())
        except Exception:
            rate = 1.0
        state_name = getattr(st.state, "name", str(st.state))
        with self._lock:
            self._seq += 1
            seq = self._seq
        return {
            "type": "hb", "seq": seq, "t": self._clock(),
            "entry_id": st.current_entry_id, "name": st.current_song_name or "",
            "state": state_name, "pos": pos if math.isfinite(pos) else 0.0,
            "mode": getattr(audio, "mode", "transport"),
            "loop_id": getattr(st, "_looping_loop_id", None),
            "held": state_name == "HELD",
            "exiting": bool(st.exit_queue_active or st.exit_fade_mode),
            "previewing": bool(getattr(st, "_previewing", False)),
            "rate": rate if math.isfinite(rate) and rate > 0 else 1.0,
            "live": bool(st.live_mode),
            "cues": cue_signature(st) if st.current_song_name else "",
        }

    def _heartbeat_bytes(self) -> bytes:
        return encode(self._heartbeat_msg())

    def _heartbeat_loop(self, gen: int) -> None:
        stop = self._stop_event
        while self._alive(gen):
            with self._lock:
                peers = list(self._peers)
            if peers:
                try:
                    data = self._heartbeat_bytes()
                except Exception:
                    logger.exception("Mirror heartbeat failed")
                    data = None
                now = self._clock()
                for p in peers:
                    if now - p.last_rx > PEER_TIMEOUT_S:
                        self._drop_peer(p, "silent (timeout)")
                    elif data is not None:
                        p.push_hb(data)
            stop.wait(self.heartbeat_s)

    # ============================================================ BACKUP --

    def _reset_backup_runtime(self) -> None:
        self._ever_connected = False
        self._rejected = None
        self._last_rx = None
        self._rtt = None
        self._show_match = None
        self._main_show = None
        self._mismatch_kind = None
        self._mismatch_count = 0
        self._failed_load = None
        self._missing = None
        self._cues_differ = None
        self.drift_ms = None
        self._took_over = False

    def _start_backup(self, gen: int, host: str, port: int) -> bool:
        with self._lock:
            self.role = "backup"
            self.following = True
            self.connected = False
            self._reset_backup_runtime()
        try:
            self.state.on("hold_reached", self._on_backup_hold_reached)
        except Exception:
            pass
        self._spawn(self._applier_loop, "mirror-applier", gen)
        self._spawn(self._link_loop, "mirror-link", gen, host, port)
        self._spawn(self._tick_loop, "mirror-tick", gen)
        logger.info(f"Mirror backup following {host}:{port}")
        self._update_status()
        return True

    def _send(self, msg: dict) -> bool:
        sock = self._sock
        if sock is None:
            return False
        try:
            with self._send_lock:
                sock.sendall(encode(msg))
            return True
        except OSError:
            return False

    def _link_loop(self, gen: int, host: str, port: int) -> None:
        attempt = 0
        stop = self._stop_event
        while self._alive(gen):
            sock = None
            try:
                infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
                if not infos:
                    raise OSError("address not found")
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(CONNECT_TIMEOUT_S)
                sock.connect(infos[0][4])
                _tune_socket(sock)
                sock.settimeout(max(0.05, self.tick_s))
            except OSError as e:
                _close_socket(sock)
                logger.debug(f"Mirror backup: connect failed: {e}")
                delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
                attempt += 1
                stop.wait(delay)
                continue
            with self._lock:
                if not self._alive(gen):
                    _close_socket(sock)
                    return
                self._sock = sock
            self._send({"type": "hello", "proto": PROTO, "pin": self.pin,
                        "client": "LoopStation"})
            welcomed = self._read_main(gen, sock)
            with self._lock:
                if self._sock is sock:
                    self._sock = None
                was_connected = self.connected
                self.connected = False
                self.drift_ms = None
            _close_socket(sock)
            if was_connected:
                logger.warning("Mirror backup: MAIN LOST (playback continues)")
            self._update_status()
            if self._rejected:
                return
            if welcomed:
                attempt = 0
            delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
            attempt += 1
            stop.wait(delay)

    def _read_main(self, gen: int, sock: socket.socket) -> bool:
        reader = LineReader()
        welcomed = False
        started = self._clock()
        while self._alive(gen):
            try:
                data = sock.recv(65536)
            except socket.timeout:
                if not welcomed and self._clock() - started > HANDSHAKE_TIMEOUT_S:
                    return False
                continue
            except OSError:      # reset / aborted (WSAECONNRESET) / closed by tick
                return welcomed
            if not data:
                return welcomed
            now = self._clock()
            for line in reader.feed(data):
                msg = decode(line)
                if msg is None:
                    continue
                kind = msg["type"]
                if kind == "reject":
                    reason = msg.get("reason")
                    text = {"bad_pin": "Main rejected the PIN",
                            "proto": "Main runs an incompatible LoopStation version",
                            "full": "Main has too many backups"}.get(reason, "Main refused the connection")
                    with self._lock:
                        self._rejected = text
                    logger.error(f"Mirror backup: {text}")
                    return False
                if not welcomed:
                    if kind != "welcome" or msg.get("proto") != PROTO:
                        continue
                    welcomed = True
                    with self._lock:
                        self.connected = True
                        self._ever_connected = True
                        self._last_rx = now
                        self._last_ping = -1e9
                        self._mismatch_kind, self._mismatch_count = None, 0
                    self._set_main_show(msg.get("show"))
                    if msg.get("live_mode") in (True, False):
                        self._enqueue_action({"kind": "live_mode", "enabled": msg["live_mode"]},
                                             guard=False)
                    logger.info("Mirror backup: connected to main")
                    self._update_status()
                    continue
                if kind in ("hb", "act", "show"):
                    self._last_rx = now     # pongs alone do not prove the main is working
                if kind == "hb":
                    self._on_heartbeat(msg, now)
                elif kind == "act":
                    self._enqueue_action(msg)
                elif kind == "show":
                    self._set_main_show(msg.get("show"))
                    self._update_status()
                elif kind == "pong":
                    t = _num(msg.get("t"))
                    if t is not None and t <= now:
                        rtt = now - t
                        self._rtt = rtt if self._rtt is None else 0.7 * self._rtt + 0.3 * rtt
        return welcomed

    def _tick_loop(self, gen: int) -> None:
        stop = self._stop_event
        while self._alive(gen):
            now = self._clock()
            if self.connected:
                if now - self._last_ping >= self.ping_s:
                    self._last_ping = now
                    self._send({"type": "ping", "t": now})
                if self._last_rx is not None and now - self._last_rx > self.lost_s:
                    logger.warning("Mirror backup: no heartbeat from main; MAIN LOST")
                    with self._lock:
                        sock = self._sock
                        self.connected = False
                        self.drift_ms = None
                    _close_socket(sock)   # the link loop reconnects
            self._update_status()
            stop.wait(self.tick_s)

    def _set_main_show(self, ident) -> None:
        if not isinstance(ident, dict):
            return
        self._main_show = ident
        try:
            local = show_identity(self.state.show_snapshot())
        except Exception:
            return
        self._show_match = compare_shows(ident, local)

    # ------------------------------------------------------- receiving --

    def _on_heartbeat(self, msg: dict, now: float) -> None:
        hb = {
            "entry_id": _str(msg.get("entry_id"), 64),
            "name": _str(msg.get("name"), 255) or "",
            "state": msg.get("state") if msg.get("state") in ("STOPPED", "PLAYING", "PAUSED", "HELD")
            else None,
            "pos": _num(msg.get("pos"), 0.0, 1e7),
            "mode": msg.get("mode") if msg.get("mode") in ("loop", "transport") else None,
            "held": msg.get("held") is True,
            "exiting": msg.get("exiting") is True,
            "previewing": msg.get("previewing") is True,
            "rate": _num(msg.get("rate"), 0.01, 10.0) or 1.0,
            "cues": _str(msg.get("cues"), 64) or "",
            "recv_t": now,
        }
        if hb["state"] is None or hb["pos"] is None or hb["mode"] is None:
            return
        with self._apply_cond:
            self._hb = hb
            self._apply_cond.notify()

    def _enqueue_action(self, msg: dict, guard: bool = True) -> None:
        if not self.following:
            return
        kind = msg.get("kind")
        if not isinstance(kind, str):
            return
        with self._apply_cond:
            self._actions.append((dict(msg), guard, self._clock()))
            self._hb = None                 # an older heartbeat is stale now
            self._apply_cond.notify()

    # --------------------------------------------------------- applying --

    def _applier_loop(self, gen: int) -> None:
        while gen == self._gen:
            with self._apply_cond:
                while gen == self._gen and not self._actions and self._hb is None:
                    self._apply_cond.wait(0.5)
                if gen != self._gen:
                    return
                if self._actions:
                    item, hb = self._actions.popleft(), None
                else:
                    item, hb = None, self._hb
                    self._hb = None
            try:
                if item is not None:
                    msg, guard, recv_t = item
                    self._apply_action(msg, recv_t)
                    if guard:
                        self._last_action_t = self._clock()
                    self._mismatch_kind, self._mismatch_count = None, 0
                elif hb is not None and self.following and self.connected:
                    self._apply_heartbeat(hb)
            except Exception:
                logger.exception("Mirror backup: applying a mirrored action failed")
            self._update_status()

    def _quiet(self):
        return self.state._mirror_quiet()

    def _owd(self) -> float:
        return (self._rtt or 0.0) / 2.0

    def _apply_action(self, msg: dict, recv_t: float) -> None:
        st = self.state
        kind = msg["kind"]
        with self._quiet():
            if kind == "go":
                return                         # informational; guard time recorded
            if kind == "play":
                if st.current_song_path and st.state.name != "PLAYING":
                    st.play()
            elif kind == "play_from":
                pos = _num(msg.get("position"), 0.0, 1e7)
                if pos is not None and st.current_song_path:
                    st._play_from(pos + self._owd() * self._rate())
            elif kind == "pause":
                if st.state.name == "PLAYING":
                    st.pause()
                pos = _num(msg.get("position"), 0.0, 1e7)
                if pos is not None and st.current_song_path and st.audio.mode == "transport" \
                        and st.state.name == "PAUSED" \
                        and abs(st.audio.get_position() - pos) * 1000.0 > self.drift_threshold_ms:
                    st.seek(pos)
            elif kind == "stop":
                st.stop()
            elif kind == "seek":
                pos = _num(msg.get("position"), 0.0, 1e7)
                if pos is not None and st.current_song_path:
                    if st.state.name == "PLAYING":
                        pos += (self._owd() + self._clock() - recv_t) * self._rate()
                    st.seek(pos)
            elif kind == "queue_exit":
                fade = msg.get("fade") is True
                fade_ms = _num(msg.get("fade_ms"), 0.0, 600000.0)
                st.queue_exit(fade_mode=fade, fade_ms=int(fade_ms) if fade and fade_ms is not None else None)
            elif kind == "release_hold":
                if st.state.name == "HELD":
                    st._release_hold()
                else:
                    # The backup may be a few ms short of the hold: release it on arrival
                    self._pending_release_until = self._clock() + max(1.0, self.guard_s)
            elif kind == "live_mode":
                if msg.get("enabled") in (True, False):
                    st.set_live_mode(msg["enabled"])
            elif kind == "next_up":
                entry_id = _str(msg.get("entry_id"), 64)
                if entry_id is None:
                    st.set_next_up(None)
                else:
                    target = self._map_entry(entry_id, _str(msg.get("file"), 255) or "")
                    if target is not None and target[0] is not None:
                        st.set_next_up(target[0])
            elif kind == "tempo":
                ratio = _num(msg.get("tempo_ratio"), 0.25, 4.0)
                semis = _num(msg.get("semitones"), -24.0, 24.0)
                if ratio is not None and semis is not None and st.current_song_path:
                    st._mirror_set_tempo_pitch(ratio, semis)
            elif kind == "load":
                self._load(_str(msg.get("entry_id"), 64), _str(msg.get("file"), 255) or "",
                           force_stop=True)
            # unknown kinds are ignored

    def _rate(self) -> float:
        try:
            return max(0.01, float(self.state.get_playback_rate()))
        except Exception:
            return 1.0

    def _on_backup_hold_reached(self, _hold=None) -> None:
        if self.role != "backup" or not self.following:
            return
        if self._clock() <= self._pending_release_until:
            self._pending_release_until = -1e9
            self._enqueue_action({"kind": "release_hold"})

    # ----------------------------------------------------- entry mapping --

    def _map_entry(self, entry_id: Optional[str], file: str):
        """(local entry id | None, path) for the main's entry, or None if not found."""
        snap = self.state.show_snapshot()
        entries = snap.get("entries") or []
        want = _name_key(file)

        def _name(e):
            return _name_key(os.path.basename(e.get("path") or ""))

        songs = [e for e in entries if e.get("kind") == "song" and not e.get("missing")]
        if entry_id:
            for e in songs:
                if e.get("id") == entry_id and (not want or _name(e) == want):
                    return e["id"], e["path"]
        if not want:
            return None
        matches = [e for e in songs if _name(e) == want]
        if matches:
            cur = snap.get("current_entry_id")
            ids = [e.get("id") for e in entries]
            cur_index = ids.index(cur) if cur in ids else -1
            after = [e for e in matches if ids.index(e["id"]) > cur_index]
            chosen = (after or matches)[0]
            return chosen["id"], chosen["path"]
        st = self.state
        if st.current_song_path and _name_key(os.path.basename(st.current_song_path)) == want:
            return None, st.current_song_path
        folders = []
        if st.current_song_path:
            folders.append(os.path.dirname(st.current_song_path))
        for key in ("folder", "file_path"):
            value = snap.get(key)
            if value:
                folders.append(value if key == "folder" else os.path.dirname(value))
        for folder in folders:
            try:
                for name in os.listdir(folder):
                    if _name_key(name) == want:
                        path = os.path.join(folder, name)
                        if os.path.isfile(path):
                            return None, path
            except OSError:
                continue
        return None

    def _on_same_song(self, entry_id: Optional[str], name: str) -> bool:
        st = self.state
        if _name_key(st.current_song_name) != _name_key(name):
            return False
        if entry_id and self._show_match == "same":
            snap_ids = {e.get("id") for e in st.show_snapshot().get("entries") or []}
            if entry_id in snap_ids and st.current_entry_id != entry_id:
                return False     # a reprise of the same file: another entry
        return True

    def _load(self, entry_id: Optional[str], file: str, force_stop: bool = False) -> bool:
        st = self.state
        if not file:
            if force_stop:
                st.stop()
            return False
        target = self._map_entry(entry_id, file)
        if target is None:
            self._missing = file
            self._failed_load = (entry_id, _name_key(file))
            logger.error(f"Mirror backup: main loaded '{file}', which is not on this computer")
            st.stop()
            return False
        local_id, path = target
        if self._on_same_song(entry_id, file) and (local_id is None or local_id == st.current_entry_id) \
                and st.current_song_path and os.path.normcase(path) == os.path.normcase(st.current_song_path):
            # Already on it (e.g. we realigned first): just mirror the stop
            if force_stop:
                st.stop()
            self._missing = None
            return True
        ok = st._mirror_load(local_id, path)
        if ok:
            self._missing = None
            self._failed_load = None
            cb = self.on_loaded
            if cb is not None:
                try:
                    cb(path)
                except Exception:
                    logger.exception("mirror on_loaded callback failed")
        else:
            self._missing = file
            self._failed_load = (entry_id, _name_key(file))
        return ok

    # ---------------------------------------------------- reconciliation --

    def _mismatch(self, kind: str) -> bool:
        """Count consecutive heartbeats asking for ``kind``; True when confirmed."""
        if self._mismatch_kind == kind:
            self._mismatch_count += 1
        else:
            self._mismatch_kind, self._mismatch_count = kind, 1
        if self._mismatch_count >= self.confirm:
            self._mismatch_kind, self._mismatch_count = None, 0
            return True
        return False

    def _apply_heartbeat(self, hb: dict) -> None:
        st = self.state
        now = self._clock()
        if not hb["name"]:
            self.drift_ms = None
            return
        # 1. Same song as the main?
        if not self._on_same_song(hb["entry_id"], hb["name"]):
            self.drift_ms = None
            key = (hb["entry_id"], _name_key(hb["name"]))
            if key == self._failed_load:
                return
            with self._quiet():
                self._load(hb["entry_id"], hb["name"])
            self._mismatch_kind, self._mismatch_count = None, 0
            return
        self._cues_differ = None
        if hb["cues"]:
            mine = cue_signature(st)
            if mine and mine != hb["cues"]:
                self._cues_differ = hb["name"]
        # 2. Never correct right after a discrete action / GO
        if now - self._last_action_t < self.guard_s:
            self._mismatch_kind, self._mismatch_count = None, 0
            return
        m_state, b_state = hb["state"], st.state.name
        audio = st.audio
        main_transport = (m_state == "PLAYING" and hb["mode"] == "transport" and not hb["held"]
                          and not hb["exiting"] and not hb["previewing"])
        backup_transport = (b_state == "PLAYING" and getattr(audio, "mode", "") == "transport"
                            and not st.exit_queue_active and not st.exit_fade_mode
                            and not getattr(st, "_previewing", False))
        est = hb["pos"]
        if m_state == "PLAYING":
            est += (self._owd() + now - hb["recv_t"]) * hb["rate"]

        if main_transport and backup_transport:
            drift = (audio.get_position() - est) * 1000.0
            self.drift_ms = drift
            if abs(drift) > self.drift_threshold_ms:
                if self._mismatch("drift"):
                    logger.info(f"Mirror backup: drift {drift:+.0f} ms; seeking to {est:.3f}s")
                    with self._quiet():
                        st.seek(est)
                    self.drift_ms = 0.0
            else:
                self._mismatch_kind, self._mismatch_count = None, 0
            return
        self.drift_ms = None

        with self._quiet():
            if m_state == "PLAYING" and not hb["previewing"]:
                if b_state in ("STOPPED", "PAUSED") and main_transport:
                    if self._mismatch("start"):
                        logger.info(f"Mirror backup: realign - start at {est:.3f}s")
                        st.seek(est)
                        st.play()
                    return
                if b_state == "HELD" and not hb["held"]:
                    if self._mismatch("release"):
                        st._release_hold()
                    return
            elif m_state == "PAUSED":
                if b_state in ("PLAYING", "HELD"):
                    if self._mismatch("pause"):
                        if b_state == "PLAYING":
                            st.pause()
                        if getattr(audio, "mode", "") == "transport":
                            st.seek(hb["pos"])
                    return
                if b_state in ("STOPPED", "PAUSED") and getattr(audio, "mode", "") == "transport" \
                        and abs(audio.get_position() - hb["pos"]) * 1000.0 > self.drift_threshold_ms:
                    if self._mismatch("cue"):
                        st.seek(hb["pos"])
                    return
            elif m_state == "STOPPED":
                if b_state in ("PLAYING", "PAUSED", "HELD"):
                    if self._mismatch("stop"):
                        st.stop()
                    return
        self._mismatch_kind, self._mismatch_count = None, 0
