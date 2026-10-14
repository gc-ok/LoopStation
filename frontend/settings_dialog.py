"""
App settings dialog (header gear button).

Sections:
- MIDI: input port, and per-action rows (current mapping, Learn, Clear).
  ``MidiController.learn()`` reports through ``on_learned`` on the MIDI
  thread; the app marshals that onto the Tk thread and calls
  ``on_midi_learned`` here.
- Keyboard: action -> key rows with "Set key" capture; persisted in user
  preferences under "keys" and applied to the app's bindings immediately.
- OSC output: enabled, host, port, scheme (loopstation / qlab), Send test.
- Foot pedals: how AirTurn / PageFlip style pedals are used (they type keys).

Also home of the key-map helpers the app uses (KEY_ACTIONS, load_key_map...).
"""

import logging
from typing import Callable, Dict, List, Optional

import tkinter as tk
import customtkinter as ctk

from config import (
    COLOR_TEXT, COLOR_TEXT_DIM,
    COLOR_BTN_PRIMARY, COLOR_BTN_SUCCESS, COLOR_BTN_TEXT,
)
from utils import preferences
from .widgets import ModalDialog, SECONDARY_BTN, after_if_alive

logger = logging.getLogger("LoopStation.Settings")

# =============================================================================
# KEY MAP
# =============================================================================

KEYS_PREF = "keys"

# (action, label, default keysyms). Keys not listed here (I, O, S, N, the
# arrow nudges) are fixed shortcuts.
KEY_ACTIONS = [
    ("go", "GO", ["Return", "KP_Enter"]),
    ("play_pause", "Play / Pause", ["space"]),
    ("stop", "Stop", ["Escape"]),
    ("exit_vamp", "Exit vamp at boundary", ["e"]),
    ("fade_exit", "Fade exit", ["f"]),
    ("add_marker", "Add cue marker", ["m"]),
    ("add_hold", "Add hold at playhead", ["h"]),
    ("next_cue", "Next marker", ["bracketright"]),
    ("prev_cue", "Previous marker", ["bracketleft"]),
    ("toggle_live", "Toggle Live Mode", ["l"]),
    ("seek_start", "Go to start", ["Home"]),
]
KEY_ACTION_LABELS = {a: label for a, label, _ in KEY_ACTIONS}
DEFAULT_KEY_MAP = {a: list(keys) for a, _, keys in KEY_ACTIONS}

KEY_NAMES = {
    "Return": "Enter", "KP_Enter": "Keypad Enter", "space": "Space",
    "Escape": "Esc", "bracketright": "]", "bracketleft": "[",
    "Prior": "Page Up", "Next": "Page Down", "Left": "←", "Right": "→",
    "Up": "↑", "Down": "↓",
}
MODIFIER_KEYSYMS = {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
                    "Meta_L", "Meta_R", "Super_L", "Super_R", "Caps_Lock", "Num_Lock",
                    "Win_L", "Win_R", "ISO_Level3_Shift"}

MIDI_ACTION_LABELS = {
    "go": "GO", "play_pause": "Play / Pause", "stop": "Stop",
    "exit_vamp": "Exit vamp", "fade_exit": "Fade exit", "skip_vamp": "Skip next vamp",
    "next_cue": "Next cue", "prev_cue": "Previous cue", "add_marker": "Add marker",
    "toggle_live": "Toggle Live Mode",
}


def key_display(keysym: str) -> str:
    if keysym in KEY_NAMES:
        return KEY_NAMES[keysym]
    return keysym.upper() if len(keysym) == 1 else keysym


def keys_display(keys: List[str]) -> str:
    return " / ".join(key_display(k) for k in keys) if keys else "—"


def normalize_keysym(keysym: str) -> str:
    """Letters are stored lower-case (both cases get bound)."""
    return keysym.lower() if len(keysym) == 1 else keysym


def load_key_map() -> Dict[str, List[str]]:
    """Defaults overlaid with the user's "keys" preferences (str or list values)."""
    result = {a: list(k) for a, k in DEFAULT_KEY_MAP.items()}
    try:
        stored = preferences.load_preferences().get(KEYS_PREF)
    except Exception:
        stored = None
    if isinstance(stored, dict):
        for action, keys in stored.items():
            if action not in result:
                continue
            if isinstance(keys, str):
                keys = [keys] if keys else []
            if isinstance(keys, list):
                result[action] = [normalize_keysym(str(k)) for k in keys if k]
    return result


def save_key_map(key_map: Dict[str, List[str]]) -> None:
    preferences.save_preferences({KEYS_PREF: {a: list(k) for a, k in key_map.items()}})


def assign_key(key_map: Dict[str, List[str]], action: str, keysym: str) -> Dict[str, List[str]]:
    """Return a copy where ``action`` uses only ``keysym`` (removed from other actions)."""
    keysym = normalize_keysym(keysym)
    new = {a: [k for k in keys if k != keysym] for a, keys in key_map.items()}
    new[action] = [keysym]
    return new


# =============================================================================
# DIALOG
# =============================================================================

class SettingsDialog(ModalDialog):
    WIDTH = 600

    def __init__(self, parent, state_manager, key_map: Dict[str, List[str]],
                 on_keys_changed: Callable[[Dict[str, List[str]]], None],
                 on_close: Optional[Callable] = None):
        super().__init__(parent, "Loop Station Settings", on_close=on_close)
        self.app_state = state_manager
        self.midi = getattr(state_manager, "midi", None)
        self.key_map = {a: list(k) for a, k in key_map.items()}
        self.on_keys_changed = on_keys_changed
        self._live = bool(getattr(state_manager, "live_mode", False))
        self._capture_action: Optional[str] = None
        self._midi_rows: Dict[str, dict] = {}
        self._key_rows: Dict[str, dict] = {}
        self._destructive: List[ctk.CTkButton] = []

        self._build_midi()
        self._build_keys()
        self._build_osc()
        self._build_pedal_hint()

        self.lbl_status = ctk.CTkLabel(self.actions, text="", font=("Segoe UI", 11),
                                       text_color=COLOR_TEXT_DIM, anchor="w")
        self.lbl_status.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(self.actions, text="Close", width=100, height=36,
                      **SECONDARY_BTN, text_color=COLOR_TEXT,
                      command=self.close).pack(side="right")

        self.bind("<Escape>", self._on_escape)
        self.bind("<KeyPress>", self._on_keypress, add="+")
        self.set_live_mode(self._live)
        self.finish_layout()

    # ----------------------------------------------------------------- MIDI

    def _build_midi(self):
        box = self.section("🎹 MIDI control")
        if self.midi is None:
            ctk.CTkLabel(box, text="MIDI is not available.", text_color=COLOR_TEXT_DIM).pack(anchor="w")
            return
        row = ctk.CTkFrame(box, fg_color="transparent")
        row.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(row, text="Input port", width=90, anchor="w",
                     text_color=COLOR_TEXT).pack(side="left")
        self.port_menu = ctk.CTkOptionMenu(row, values=["(none)"], width=300,
                                           command=self._on_port_select)
        self.port_menu.pack(side="left", padx=(0, 6))
        ctk.CTkButton(row, text="↻", width=32, height=28, **SECONDARY_BTN,
                      text_color=COLOR_TEXT, command=self._refresh_ports).pack(side="left")
        self.lbl_port = ctk.CTkLabel(box, text="", font=("Segoe UI", 10),
                                     text_color=COLOR_TEXT_DIM, anchor="w")
        self.lbl_port.pack(fill="x")
        self._refresh_ports()

        from backend.midi_control import ACTIONS
        for action in ACTIONS:
            r = ctk.CTkFrame(box, fg_color="transparent", height=30)
            r.pack(fill="x", pady=1)
            ctk.CTkLabel(r, text=MIDI_ACTION_LABELS.get(action, action), width=150,
                         anchor="w", text_color=COLOR_TEXT).pack(side="left")
            lbl = ctk.CTkLabel(r, text="", width=170, anchor="w",
                               font=("Consolas", 11), text_color="#58a6ff")
            lbl.pack(side="left")
            btn_clear = ctk.CTkButton(r, text="Clear", width=60, height=26,
                                      **SECONDARY_BTN, text_color=COLOR_TEXT,
                                      command=lambda a=action: self._clear_midi(a))
            btn_clear.pack(side="right")
            btn_learn = ctk.CTkButton(r, text="Learn", width=70, height=26,
                                      fg_color=COLOR_BTN_PRIMARY, text_color=COLOR_BTN_TEXT,
                                      command=lambda a=action: self._learn_midi(a))
            btn_learn.pack(side="right", padx=(0, 6))
            self._destructive.append(btn_clear)
            self._midi_rows[action] = {"label": lbl, "learn": btn_learn}
        self._refresh_midi_rows()

    def _refresh_ports(self):
        names = []
        try:
            names = list(self.midi.list_inputs())
        except Exception as e:
            logger.warning(f"MIDI listing failed: {e}")
        self.port_menu.configure(values=["(none)"] + names)
        current = self.midi.port_name
        self.port_menu.set(current if current else "(none)")
        if current and not self.midi.is_open:
            self.lbl_port.configure(text=f"{current} disconnected - reconnecting...")
        elif not names:
            self.lbl_port.configure(text="No MIDI inputs found (connect a device and press ↻).")
        elif current:
            self.lbl_port.configure(text=f"Listening on {current}")
        else:
            self.lbl_port.configure(text="Choose an input port to enable MIDI control.")

    def _on_port_select(self, name):
        if name == "(none)":
            self.midi.close()
            self.lbl_port.configure(text="MIDI input closed.")
        elif self.midi.open(name):
            self.lbl_port.configure(text=f"Listening on {name}")
        else:
            self.lbl_port.configure(text=f"Could not open {name}: {self.midi.last_error}")
            self.port_menu.set("(none)")
        self.midi.save_mappings()

    def on_midi_status(self, text):
        """MIDI port lost / reconnected (from the app, Tk thread)."""
        if self.midi is None or self._closed:
            return
        try:
            self.lbl_port.configure(text=text)
        except tk.TclError:
            pass

    def _refresh_midi_rows(self):
        learning = self.midi.learning if self.midi else None
        for action, row in self._midi_rows.items():
            if action == learning:
                row["label"].configure(text="Waiting for MIDI…", text_color="#f0a020")
                row["learn"].configure(text="Cancel")
            else:
                key = self.midi.key_for(action)
                row["label"].configure(text=key or "—",
                                       text_color="#58a6ff" if key else COLOR_TEXT_DIM)
                row["learn"].configure(text="Learn")

    def _learn_midi(self, action):
        if self.midi.learning == action:
            self.midi.cancel_learn()
            self._refresh_midi_rows()
            return
        try:
            self.midi.learn(action)
        except ValueError as e:
            self.lbl_status.configure(text=str(e))
            return
        if not self.midi.is_open:
            self.lbl_status.configure(text="Tip: choose a MIDI input port first.")
        self._refresh_midi_rows()
        # learn() times out after 10 s without a callback; reset the row then.
        after_if_alive(self, 10_300, self._refresh_midi_rows)

    def on_midi_learned(self, action, key):
        """Called by the app on the Tk thread when MidiController learned a key."""
        if self._closed:
            return
        self.midi.save_mappings()
        self._refresh_midi_rows()
        self.lbl_status.configure(text=f"MIDI {key} → {MIDI_ACTION_LABELS.get(action, action)}")

    def _clear_midi(self, action):
        if self._live:
            return
        self.midi.clear_mapping(action)
        self.midi.save_mappings()
        self._refresh_midi_rows()

    # ------------------------------------------------------------- Keyboard

    def _build_keys(self):
        box = self.section("⌨ Keyboard / foot pedal keys")
        for action, label, _defaults in KEY_ACTIONS:
            r = ctk.CTkFrame(box, fg_color="transparent", height=30)
            r.pack(fill="x", pady=1)
            ctk.CTkLabel(r, text=label, width=170, anchor="w",
                         text_color=COLOR_TEXT).pack(side="left")
            lbl = ctk.CTkLabel(r, text="", width=170, anchor="w",
                               font=("Consolas", 11, "bold"), text_color="#58a6ff")
            lbl.pack(side="left")
            btn = ctk.CTkButton(r, text="Set key", width=90, height=26,
                                fg_color=COLOR_BTN_PRIMARY, text_color=COLOR_BTN_TEXT,
                                command=lambda a=action: self._start_capture(a))
            btn.pack(side="right")
            self._key_rows[action] = {"label": lbl, "button": btn}
        bottom = ctk.CTkFrame(box, fg_color="transparent")
        bottom.pack(fill="x", pady=(6, 0))
        self.btn_reset_keys = ctk.CTkButton(bottom, text="Reset to defaults", width=140, height=26,
                                            **SECONDARY_BTN, text_color=COLOR_TEXT,
                                            command=self._reset_keys)
        self.btn_reset_keys.pack(side="right")
        self._destructive.append(self.btn_reset_keys)
        ctk.CTkLabel(bottom, text="Fixed: I / O set loop, S save, N notes, ← → nudge.",
                     font=("Segoe UI", 10), text_color=COLOR_TEXT_DIM).pack(side="left")
        self._refresh_key_rows()

    def _refresh_key_rows(self):
        for action, row in self._key_rows.items():
            if action == self._capture_action:
                row["label"].configure(text="Press a key… (Esc cancels)", text_color="#f0a020")
                row["button"].configure(text="Cancel")
            else:
                row["label"].configure(text=keys_display(self.key_map.get(action, [])),
                                       text_color="#58a6ff")
                row["button"].configure(text="Set key")

    def _start_capture(self, action):
        self._capture_action = None if self._capture_action == action else action
        self._refresh_key_rows()
        self.focus_set()

    def _on_keypress(self, event):
        action = self._capture_action
        if action is None or event.keysym in MODIFIER_KEYSYMS:
            return None
        self._capture_action = None
        self.key_map = assign_key(self.key_map, action, event.keysym)
        self._apply_keys()
        self.lbl_status.configure(
            text=f"{KEY_ACTION_LABELS[action]} = {key_display(normalize_keysym(event.keysym))}")
        return "break"

    def _on_escape(self, _event=None):
        if self._capture_action is not None:
            self._capture_action = None
            self._refresh_key_rows()
            return "break"
        self.close()
        return "break"

    def _reset_keys(self):
        if self._live:
            return
        self.key_map = {a: list(k) for a, k in DEFAULT_KEY_MAP.items()}
        self._apply_keys()
        self.lbl_status.configure(text="Keys reset to defaults")

    def _apply_keys(self):
        self._refresh_key_rows()
        try:
            save_key_map(self.key_map)
        except Exception as e:
            logger.error(f"Could not save key map: {e}")
        if self.on_keys_changed:
            self.on_keys_changed(dict(self.key_map))

    # ------------------------------------------------------------------ OSC

    def _build_osc(self):
        box = self.section("📤 OSC output (QLab / lighting / show control)")
        osc = getattr(self.app_state, "osc", None)
        cfg = osc.settings() if osc is not None else {
            "enabled": False, "host": "127.0.0.1", "port": 53000, "scheme": "loopstation"}

        r1 = ctk.CTkFrame(box, fg_color="transparent")
        r1.pack(fill="x", pady=2)
        self.osc_enabled = ctk.CTkSwitch(r1, text="Send OSC", text_color=COLOR_TEXT)
        if cfg.get("enabled"):
            self.osc_enabled.select()
        self.osc_enabled.pack(side="left")
        self.osc_scheme = ctk.CTkSegmentedButton(r1, values=["loopstation", "qlab"])
        self.osc_scheme.set(cfg.get("scheme", "loopstation"))
        self.osc_scheme.pack(side="right")

        r2 = ctk.CTkFrame(box, fg_color="transparent")
        r2.pack(fill="x", pady=2)
        ctk.CTkLabel(r2, text="Host", width=40, anchor="w", text_color=COLOR_TEXT).pack(side="left")
        self.osc_host = ctk.CTkEntry(r2, width=170)
        self.osc_host.insert(0, str(cfg.get("host", "")))
        self.osc_host.pack(side="left", padx=(0, 10))
        ctk.CTkLabel(r2, text="Port", width=36, anchor="w", text_color=COLOR_TEXT).pack(side="left")
        self.osc_port = ctk.CTkEntry(r2, width=80)
        self.osc_port.insert(0, str(cfg.get("port", "")))
        self.osc_port.pack(side="left")
        ctk.CTkButton(r2, text="Send test", width=90, height=28, **SECONDARY_BTN,
                      text_color=COLOR_TEXT, command=self._osc_test).pack(side="right")
        ctk.CTkButton(r2, text="Save", width=70, height=28, fg_color=COLOR_BTN_SUCCESS,
                      text_color=COLOR_BTN_TEXT, command=self._osc_save).pack(side="right", padx=6)
        self.lbl_osc = ctk.CTkLabel(box, text="QLab scheme sends /cue/<osc cue>/start; "
                                    "loopstation sends /loopstation/<event>.",
                                    font=("Segoe UI", 10), text_color=COLOR_TEXT_DIM,
                                    anchor="w", wraplength=520, justify="left")
        self.lbl_osc.pack(fill="x")

    def _osc_values(self):
        try:
            port = int(self.osc_port.get().strip())
        except ValueError:
            raise ValueError("Port must be a number")
        if not (1 <= port <= 65535):
            raise ValueError("Port must be 1-65535")
        host = self.osc_host.get().strip() or "127.0.0.1"
        return bool(self.osc_enabled.get()), host, port, self.osc_scheme.get() or "loopstation"

    def _osc_save(self) -> bool:
        try:
            enabled, host, port, scheme = self._osc_values()
        except ValueError as e:
            self.lbl_osc.configure(text=str(e), text_color="#ff7b72")
            return False
        self.app_state.configure_osc(enabled, host, port, scheme)
        self.lbl_osc.configure(text=f"Saved: {'on' if enabled else 'off'} → {host}:{port} ({scheme})",
                               text_color=COLOR_TEXT_DIM)
        return True

    def _osc_test(self):
        if not self._osc_save():
            return
        osc = self.app_state.osc
        if not osc.enabled:
            self.lbl_osc.configure(text="Turn on \"Send OSC\" to send a test message.",
                                   text_color="#f0a020")
            return
        if not osc.send_event("test", source="settings"):
            self.lbl_osc.configure(text=f"Send failed: {osc.last_error or 'unknown error'}",
                                   text_color="#ff7b72")
            return
        self.lbl_osc.configure(text="Sending test...", text_color=COLOR_TEXT_DIM)
        # Sends happen on the OSC worker; poll for the result instead of
        # blocking the Tk thread (GO must stay live while this dialog is open).
        self._osc_test_poll(osc, 0)

    def _osc_test_poll(self, osc, tries):
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        if not osc.flush(timeout=0) and tries < 30:
            self.after(50, lambda: self._osc_test_poll(osc, tries + 1))
            return
        if osc.last_error:
            self.lbl_osc.configure(text=f"Send failed: {osc.last_error}", text_color="#ff7b72")
        else:
            self.lbl_osc.configure(text=f"Test sent to {osc.host}:{osc.port}", text_color="#4cd38a")

    # ---------------------------------------------------------- pedal hint

    def _build_pedal_hint(self):
        box = self.section("🦶 Foot pedals")
        ctk.CTkLabel(
            box, wraplength=520, justify="left", anchor="w", font=("Segoe UI", 11),
            text_color=COLOR_TEXT_DIM,
            text=("Bluetooth page-turner pedals (AirTurn, PageFlip and similar) act as a "
                  "keyboard. Put the pedal in a keyboard mode, then use \"Set key\" above "
                  "and press the pedal to map it - for example GO to the pedal's "
                  "Page Down or Enter key. MIDI pedals are mapped with Learn."),
        ).pack(fill="x")

    # ------------------------------------------------------------ live mode

    def set_live_mode(self, live: bool):
        self._live = bool(live)
        state = "disabled" if self._live else "normal"
        for btn in self._destructive:
            try:
                btn.configure(state=state)
            except tk.TclError:
                pass
        if self._live:
            self.lbl_status.configure(text="🔒 Live Mode: clearing / resetting is locked")
        else:
            self.lbl_status.configure(text="")

    def close(self):
        if self.midi is not None and self.midi.learning:
            self.midi.cancel_learn()
        super().close()
