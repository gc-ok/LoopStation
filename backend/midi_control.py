"""
MIDI control for Loop Station (docs/audio-engine-api.md section 3).

A ``MidiController`` polls one MIDI input port on a daemon thread, turns
each message into a ``message_key`` and either records it as a new mapping
(learn mode) or dispatches the mapped action through ``on_action``.

Message keys (<ch> is the MIDI channel 1-16, as printed on hardware):
    "note_on:<ch>:<note>"   note_on with velocity > 0 (velocity 0 is a note-off: ignored)
    "cc:<ch>:<controller>"  control_change crossing from < 64 to >= 64 (switch
                            pressed). Edge-triggered per (channel, controller): a
                            pedal that streams 100, 110, 127 fires once; it must
                            drop below 64 - or go quiet for CC_REARM_S - before it
                            can fire again (pedals that only ever send 127).
    "pc:<ch>:<program>"     program_change
Learn records these channel-specific keys. Older channel-less keys
("note_on:<note>", "cc:<controller>", "pc:<program>") stay valid and match
the message on any channel.

Port loss: an exception from the port, or the port missing from the input
list on two consecutive checks (one every RECONNECT_INTERVAL_S; a listing
that fails is not counted), closes the port, reports through ``on_status``
and re-opens the same port BY NAME every RECONNECT_INTERVAL_S until it is
back (``on_status`` again). Windows numbers port names ("Pedal 1"); when
the exact name is gone, a unique port with the same base name is used.

Threading: open()/close() are serialised by one lock, and every polling
thread only touches the port it was started with (or re-opened itself).
A thread superseded by a later open()/close() exits and never re-opens.

Backend: ``mido`` with ``mido.backends.rtmidi`` when python-rtmidi is
importable, else ``mido.backends.pygame`` (no compiler needed). The backend
is selected lazily, so importing this module never touches a MIDI device.
Tests inject a fake port factory / input list and call ``handle_message``.

Mappings persist in the user preferences under "midi":
    {"mappings": {message_key: action}, "port": port_name | None}
"""

import logging
import re
import threading
import time
from typing import Callable, Dict, List, Optional

from utils import preferences

logger = logging.getLogger("LoopStation.MIDI")

ACTIONS = ["go", "play_pause", "stop", "exit_vamp", "fade_exit", "skip_vamp",
           "next_cue", "prev_cue", "add_marker", "toggle_live"]

PREFS_KEY = "midi"
POLL_INTERVAL_S = 0.005
CC_THRESHOLD = 64
RECONNECT_INTERVAL_S = 2.0
# A CC that stays >= 64 but is silent this long re-arms: the next >= 64
# value fires again (pedals that send 127 on every press and never 0).
CC_REARM_S = 0.3
# The port must be missing from this many consecutive listings to count
# as unplugged (a single empty / failed listing is not enough).
MISSING_CHECKS_TO_DROP = 2

_backend_ready = False


def _ensure_backend() -> bool:
    """Select the mido backend once (rtmidi if importable, else pygame)."""
    global _backend_ready
    if _backend_ready:
        return True
    try:
        import mido
    except Exception as e:
        logger.warning(f"mido not available: {e}")
        return False
    try:
        import importlib.util
        if importlib.util.find_spec("rtmidi") is None:
            raise ImportError("python-rtmidi not installed")
        mido.set_backend("mido.backends.rtmidi")
    except Exception:
        try:
            mido.set_backend("mido.backends.pygame")
        except Exception as e:
            logger.warning(f"Could not select a mido backend: {e}")
            return False
    _backend_ready = True
    return True


def _channel(msg) -> int:
    """1-based MIDI channel of ``msg`` (mido counts 0-15)."""
    try:
        return int(getattr(msg, "channel", 0)) + 1
    except (TypeError, ValueError):
        return 1


def message_key(msg) -> Optional[str]:
    """Map a mido message to a channel-specific mapping key, or None if it is not a trigger."""
    mtype = getattr(msg, "type", None)
    if mtype == "note_on":
        if getattr(msg, "velocity", 0) <= 0:
            return None
        return f"note_on:{_channel(msg)}:{int(msg.note)}"
    if mtype == "control_change":
        if getattr(msg, "value", 0) < CC_THRESHOLD:
            return None
        return f"cc:{_channel(msg)}:{int(msg.control)}"
    if mtype == "program_change":
        return f"pc:{_channel(msg)}:{int(msg.program)}"
    return None


def legacy_key(key: Optional[str]) -> Optional[str]:
    """The channel-less form of ``key`` ("cc:1:64" -> "cc:64"); old mappings use it."""
    if not key:
        return None
    parts = key.split(":")
    if len(parts) == 3:
        return f"{parts[0]}:{parts[2]}"
    return key


def describe_key(key: Optional[str]) -> str:
    """Human-readable mapping key for the UI: "Note 60 (ch 1)", "CC 64 (any ch)"."""
    if not key:
        return ""
    names = {"note_on": "Note", "cc": "CC", "pc": "Program"}
    parts = key.split(":")
    kind = names.get(parts[0])
    if kind is None or len(parts) not in (2, 3):
        return key
    if len(parts) == 2:
        return f"{kind} {parts[1]} (any ch)"
    return f"{kind} {parts[2]} (ch {parts[1]})"


_PORT_INDEX = re.compile(r"\s+\d+$")


def _match_port(name: str, names: List[str]) -> Optional[str]:
    """
    ``name`` if it is listed, else the single listed port with the same base
    name once a trailing Windows index is stripped ("Pedal 1" -> "Pedal 2"
    after re-plugging into another USB port). None if absent or ambiguous.
    """
    if name in names:
        return name
    base = _PORT_INDEX.sub("", name)
    same = [n for n in names if _PORT_INDEX.sub("", n) == base]
    return same[0] if len(same) == 1 else None


class _Session:
    """One open() call: its port, name and stop flag. The poll thread owns it."""

    def __init__(self, name: str, port):
        self.name = name
        self.port = port
        self.stop = threading.Event()


class MidiController:
    def __init__(self, on_action: Callable[[str], None],
                 port_factory: Optional[Callable[[str], object]] = None,
                 list_inputs_fn: Optional[Callable[[], List[str]]] = None):
        """
        Args:
            on_action: called with an action name (see ACTIONS) on the polling thread
            port_factory: name -> object with iter_pending() and close() (tests)
            list_inputs_fn: () -> list of port names (tests)
        """
        self.on_action = on_action
        self.on_learned: Optional[Callable[[str, str], None]] = None
        # Status text for the UI ("MIDI 'Pedal' lost - reconnecting...").
        # Called on the polling thread; the app marshals it to Tk.
        self.on_status: Optional[Callable[[str], None]] = None
        self.status: str = ""
        self.mappings: Dict[str, str] = {}
        self.port_name: Optional[str] = None

        self._port_factory = port_factory
        self._list_inputs_fn = list_inputs_fn
        self._port = None
        self._thread: Optional[threading.Thread] = None
        self._session: Optional[_Session] = None
        # Serialises open()/close() end to end (re-entrant: on_action may
        # call close() on the poll thread). _lock guards the shared fields.
        self._open_lock = threading.RLock()
        self._lock = threading.RLock()
        # True once open()/close() was called; restore_port() then backs off
        self._user_selected = False
        self._clock = time.monotonic
        self._learn_action: Optional[str] = None
        self._learn_deadline: float = 0.0
        self.last_error: Optional[str] = None
        # (channel, controller) -> (was >= 64, time of last value), for edge triggering
        self._cc_high: Dict[tuple, tuple] = {}

    # ------------------------------------------------------------- ports --

    def list_inputs(self) -> List[str]:
        names = self._list_inputs_checked()
        return names if names is not None else []

    def _list_inputs_checked(self) -> Optional[List[str]]:
        """Port names, or None when the listing itself failed (not "no ports")."""
        if self._list_inputs_fn is not None:
            try:
                return list(self._list_inputs_fn())
            except Exception as e:
                logger.warning(f"MIDI input listing failed: {e}")
                return None
        if not _ensure_backend():
            return None
        try:
            import mido
            return list(mido.get_input_names())
        except Exception as e:
            logger.warning(f"MIDI input listing failed: {e}")
            return None

    def _open_port(self, name: str):
        if self._port_factory is not None:
            return self._port_factory(name)
        if not _ensure_backend():
            raise RuntimeError("no MIDI backend available")
        import mido
        return mido.open_input(name)

    def open(self, port_name: Optional[str]) -> bool:
        """
        Open ``port_name`` (None = first available input) and start polling.
        The polling thread keeps running if the port is lost later and
        re-opens it by name when it comes back. Concurrent open()/close()
        calls are serialised; the last one wins.
        """
        with self._open_lock:
            self._user_selected = True
            return self._open_locked(port_name)

    def restore_port(self, port_name: Optional[str]) -> bool:
        """
        Re-open the port remembered in preferences at startup, unless the
        user already chose a port (or "(none)") through open()/close().
        Safe to call from a worker thread while Settings is open.
        """
        if not port_name:
            return False
        with self._open_lock:
            if self._user_selected:
                logger.info(f"MIDI: not restoring '{port_name}' (port chosen meanwhile)")
                return False
            return self._open_locked(port_name)

    def _open_locked(self, port_name: Optional[str]) -> bool:
        self._close_locked()
        names = None
        if port_name is None:
            names = self.list_inputs()
            if not names:
                self.last_error = "no MIDI inputs found"
                return False
            port_name = names[0]
        else:
            listed = self._list_inputs_checked()
            if listed is not None and port_name not in listed:
                # Windows renumbers ports ("Pedal 1" -> "Pedal 2")
                port_name = _match_port(port_name, listed) or port_name
        try:
            port = self._open_port(port_name)
        except Exception as e:
            self.last_error = str(e)
            logger.warning(f"Could not open MIDI input '{port_name}': {e}")
            return False
        session = _Session(port_name, port)
        with self._lock:
            self._session = session
            self._port = port
            self.port_name = port_name
            self._cc_high = {}
            self._thread = threading.Thread(target=self._poll, args=(session,),
                                            name="midi-poll", daemon=True)
            self._thread.start()
        self.last_error = None
        logger.info(f"MIDI input open: {port_name}")
        self._set_status(f"MIDI: {port_name}")
        return True

    def close(self) -> None:
        with self._open_lock:
            self._user_selected = True
            self._close_locked()

    def _close_locked(self) -> None:
        with self._lock:
            session, thread = self._session, self._thread
            self._session = None
            self._port = None
            self._thread = None
            if session is not None:
                session.stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        if session is not None:
            # The thread only touches session.port, and never after stop is
            # set and it holds _lock, so closing it here cannot race a reopen.
            with self._lock:
                port, session.port = session.port, None
            if port is not None:
                try:
                    port.close()
                except Exception:
                    pass
        self.port_name = None

    @property
    def is_open(self) -> bool:
        return self._port is not None

    def _set_status(self, text: str) -> None:
        self.status = text
        cb = self.on_status
        if cb is None:
            return
        try:
            cb(text)
        except Exception as e:
            logger.error(f"on_status callback failed: {e}")

    def _lose_port(self, session: _Session, reason: str) -> None:
        """(Poll thread) drop a dead port; the loop then tries to reconnect."""
        with self._lock:
            port, session.port = session.port, None
            current = self._session is session
            if current:
                self._port = None
        if port is not None:
            try:
                port.close()
            except Exception:
                pass
        if not current or session.stop.is_set():
            return
        self.last_error = reason
        logger.error(f"MIDI input '{session.name}' lost: {reason}")
        self._set_status(f"MIDI '{session.name}' lost - reconnecting...")

    def _poll(self, session: _Session) -> None:
        stop_event = session.stop
        next_check = time.monotonic() + RECONNECT_INTERVAL_S
        misses = 0
        while not stop_event.is_set():
            port = session.port
            now = time.monotonic()
            if port is None:
                # Lost: re-open by name every RECONNECT_INTERVAL_S
                if now >= next_check:
                    next_check = now + RECONNECT_INTERVAL_S
                    names = self._list_inputs_checked()
                    match = _match_port(session.name, names) if names else None
                    if match is not None and not stop_event.is_set():
                        try:
                            new_port = self._open_port(match)
                        except Exception as e:
                            self.last_error = str(e)
                        else:
                            with self._lock:
                                if stop_event.is_set() or self._session is not session:
                                    # Superseded by open()/close(): never reopen
                                    try:
                                        new_port.close()
                                    except Exception:
                                        pass
                                    return
                                session.port = new_port
                                session.name = match
                                self._port = new_port
                                self.port_name = match
                                self._cc_high = {}
                            misses = 0
                            self.last_error = None
                            logger.info(f"MIDI input '{match}' reconnected")
                            self._set_status(f"MIDI '{session.name}' reconnected")
                stop_event.wait(min(0.1, RECONNECT_INTERVAL_S))
                continue
            try:
                for msg in port.iter_pending():
                    if stop_event.is_set():
                        break
                    self.handle_message(msg)
            except Exception as e:
                self._lose_port(session, str(e))
                next_check = time.monotonic() + RECONNECT_INTERVAL_S
                continue
            if now >= next_check:
                next_check = now + RECONNECT_INTERVAL_S
                # Some backends never raise when a device is unplugged; the
                # port just goes quiet. Check it is still listed - a failed
                # listing (None) proves nothing, and one miss can be a
                # CoreMIDI / WinMM hiccup, so it takes two in a row.
                names = self._list_inputs_checked()
                if names is not None:
                    if _match_port(session.name, names) is None:
                        misses += 1
                        if misses >= MISSING_CHECKS_TO_DROP:
                            misses = 0
                            self._lose_port(session, "device disconnected")
                            continue
                    else:
                        misses = 0
            time.sleep(POLL_INTERVAL_S)

    # ---------------------------------------------------------- messages --

    def handle_message(self, msg) -> Optional[str]:
        """
        Process one mido message. Returns the action dispatched (or learned),
        or None if the message was ignored.
        """
        key = message_key(msg)
        if getattr(msg, "type", None) == "control_change":
            # Edge trigger: fire only on a < 64 -> >= 64 transition, or on a
            # >= 64 value after CC_REARM_S of silence (127-only pedals).
            cc_id = (_channel(msg), int(msg.control))
            high = key is not None
            now = self._clock()
            with self._lock:
                was_high, last = self._cc_high.get(cc_id, (False, 0.0))
                self._cc_high[cc_id] = (high, now)
            if not high or (was_high and now - last < CC_REARM_S):
                return None
        if key is None:
            return None

        with self._lock:
            learn_action = self._learn_action
            if learn_action is not None and time.monotonic() > self._learn_deadline:
                self._learn_action = None
                learn_action = None
            if learn_action is not None:
                self._learn_action = None
                new = {k: v for k, v in self.mappings.items() if v != learn_action}
                new[key] = learn_action
                self.mappings = new
        if learn_action is not None:
            logger.info(f"MIDI learned {key} -> {learn_action}")
            self._notify_learned(learn_action, key)
            return learn_action

        mappings = self.mappings
        action = mappings.get(key)
        if action is None:
            action = mappings.get(legacy_key(key))   # channel-less: any channel
        if action is None:
            return None
        try:
            self.on_action(action)
        except Exception as e:
            logger.error(f"MIDI action '{action}' failed: {e}")
        return action

    def _notify_learned(self, action: str, key: str) -> None:
        cb = self.on_learned
        if cb is None:
            return
        try:
            cb(action, key)
        except Exception as e:
            logger.error(f"on_learned callback failed: {e}")

    # ------------------------------------------------------------- learn --

    def learn(self, action: str, timeout_s: float = 10.0) -> None:
        """The next trigger message maps to ``action`` (replacing its old key)."""
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        with self._lock:
            self._learn_action = action
            self._learn_deadline = time.monotonic() + float(timeout_s)

    def cancel_learn(self) -> None:
        with self._lock:
            self._learn_action = None

    @property
    def learning(self) -> Optional[str]:
        with self._lock:
            if self._learn_action is not None and time.monotonic() > self._learn_deadline:
                self._learn_action = None
            return self._learn_action

    # ---------------------------------------------------------- mappings --

    def load_mappings(self, mappings: dict) -> None:
        clean = {}
        for key, action in (mappings or {}).items():
            if isinstance(key, str) and action in ACTIONS:
                clean[key] = action
        with self._lock:
            self.mappings = clean

    def clear_mapping(self, action: str) -> None:
        with self._lock:
            self.mappings = {k: v for k, v in self.mappings.items() if v != action}

    def save_mappings(self) -> dict:
        data = {"mappings": dict(self.mappings), "port": self.port_name}
        try:
            preferences.save_preferences({PREFS_KEY: data})
        except Exception as e:
            logger.error(f"Could not save MIDI mappings: {e}")
        return data

    def load_from_preferences(self) -> dict:
        """Load mappings (and the remembered port name) from user preferences."""
        data = preferences.load_preferences().get(PREFS_KEY)
        if not isinstance(data, dict):
            return {}
        self.load_mappings(data.get("mappings") or {})
        return data

    def key_for(self, action: str) -> Optional[str]:
        for key, mapped in self.mappings.items():
            if mapped == action:
                return key
        return None
