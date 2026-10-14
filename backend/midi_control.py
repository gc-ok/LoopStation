"""
MIDI control for Loop Station (docs/audio-engine-api.md section 3).

A ``MidiController`` polls one MIDI input port on a daemon thread, turns
each message into a ``message_key`` and either records it as a new mapping
(learn mode) or dispatches the mapped action through ``on_action``.

Message keys:
    "note_on:<note>"   note_on with velocity > 0 (velocity 0 is a note-off: ignored)
    "cc:<controller>"  control_change crossing from < 64 to >= 64 (switch
                       pressed). Edge-triggered per (channel, controller): a
                       pedal that streams 100, 110, 127 fires once; it must
                       drop below 64 before it can fire again.
    "pc:<program>"     program_change

Port loss: an exception from the port, or the port disappearing from the
input list (checked every RECONNECT_INTERVAL_S), closes the port, reports
through ``on_status`` and re-opens the same port BY NAME every
RECONNECT_INTERVAL_S until it is back (``on_status`` again).

Backend: ``mido`` with ``mido.backends.rtmidi`` when python-rtmidi is
importable, else ``mido.backends.pygame`` (no compiler needed). The backend
is selected lazily, so importing this module never touches a MIDI device.
Tests inject a fake port factory / input list and call ``handle_message``.

Mappings persist in the user preferences under "midi":
    {"mappings": {message_key: action}, "port": port_name | None}
"""

import logging
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


def message_key(msg) -> Optional[str]:
    """Map a mido message to a mapping key, or None if it is not a trigger."""
    mtype = getattr(msg, "type", None)
    if mtype == "note_on":
        if getattr(msg, "velocity", 0) <= 0:
            return None
        return f"note_on:{int(msg.note)}"
    if mtype == "control_change":
        if getattr(msg, "value", 0) < CC_THRESHOLD:
            return None
        return f"cc:{int(msg.control)}"
    if mtype == "program_change":
        return f"pc:{int(msg.program)}"
    return None


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
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._learn_action: Optional[str] = None
        self._learn_deadline: float = 0.0
        self.last_error: Optional[str] = None
        # Last CC value seen per (channel, controller), for edge triggering
        self._cc_high: Dict[tuple, bool] = {}

    # ------------------------------------------------------------- ports --

    def list_inputs(self) -> List[str]:
        if self._list_inputs_fn is not None:
            try:
                return list(self._list_inputs_fn())
            except Exception as e:
                logger.warning(f"MIDI input listing failed: {e}")
                return []
        if not _ensure_backend():
            return []
        try:
            import mido
            return list(mido.get_input_names())
        except Exception as e:
            logger.warning(f"MIDI input listing failed: {e}")
            return []

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
        re-opens it by name when it comes back.
        """
        self.close()
        if port_name is None:
            names = self.list_inputs()
            if not names:
                self.last_error = "no MIDI inputs found"
                return False
            port_name = names[0]
        try:
            port = self._open_port(port_name)
        except Exception as e:
            self.last_error = str(e)
            logger.warning(f"Could not open MIDI input '{port_name}': {e}")
            return False
        with self._lock:
            self._port = port
            self.port_name = port_name
            self._cc_high = {}
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._poll, args=(port_name, self._stop),
                                            name="midi-poll", daemon=True)
            self._thread.start()
        self.last_error = None
        logger.info(f"MIDI input open: {port_name}")
        self._set_status(f"MIDI: {port_name}")
        return True

    def close(self) -> None:
        with self._lock:
            port, thread = self._port, self._thread
            self._port = None
            self._thread = None
            self._stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
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

    def _lose_port(self, port, reason: str) -> None:
        """(Poll thread) drop a dead port; the loop then tries to reconnect."""
        self.last_error = reason
        with self._lock:
            if self._port is port:
                self._port = None
        try:
            port.close()
        except Exception:
            pass
        logger.error(f"MIDI input '{self.port_name}' lost: {reason}")
        self._set_status(f"MIDI '{self.port_name}' lost - reconnecting...")

    def _poll(self, name: str, stop_event: threading.Event) -> None:
        next_check = time.monotonic() + RECONNECT_INTERVAL_S
        while not stop_event.is_set():
            port = self._port
            now = time.monotonic()
            if port is None:
                # Lost: re-open by name every RECONNECT_INTERVAL_S
                if now >= next_check:
                    next_check = now + RECONNECT_INTERVAL_S
                    if name in self.list_inputs():
                        try:
                            new_port = self._open_port(name)
                        except Exception as e:
                            self.last_error = str(e)
                        else:
                            with self._lock:
                                if stop_event.is_set():
                                    new_port.close()
                                    return
                                self._port = new_port
                                self._cc_high = {}
                            self.last_error = None
                            logger.info(f"MIDI input '{name}' reconnected")
                            self._set_status(f"MIDI '{name}' reconnected")
                stop_event.wait(min(0.1, RECONNECT_INTERVAL_S))
                continue
            try:
                for msg in port.iter_pending():
                    self.handle_message(msg)
            except Exception as e:
                self._lose_port(port, str(e))
                next_check = time.monotonic() + RECONNECT_INTERVAL_S
                continue
            if now >= next_check:
                next_check = now + RECONNECT_INTERVAL_S
                # Some backends never raise when a device is unplugged; the
                # port just goes quiet. Check it is still listed.
                if name not in self.list_inputs():
                    self._lose_port(port, "device disconnected")
                    continue
            time.sleep(POLL_INTERVAL_S)

    # ---------------------------------------------------------- messages --

    def handle_message(self, msg) -> Optional[str]:
        """
        Process one mido message. Returns the action dispatched (or learned),
        or None if the message was ignored.
        """
        key = message_key(msg)
        if getattr(msg, "type", None) == "control_change":
            # Edge trigger: fire only on a < 64 -> >= 64 transition
            cc_id = (int(getattr(msg, "channel", 0)), int(msg.control))
            high = key is not None
            with self._lock:
                was_high = self._cc_high.get(cc_id, False)
                self._cc_high[cc_id] = high
            if not high or was_high:
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

        action = self.mappings.get(key)
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
