"""
GO panel: the show-control strip under the transport.

- A large GO button (Return / keypad Enter by default; configurable) that
  performs StateManager.standby()["action"].
- The STANDBY line (what GO will do) coloured by action, plus the next cue.
- HELD state shown loudly (panel turns amber, banner text).
- Vamp pass counter ("Pass 2 of 3") from loop_cycle events.
- A short flash when a GO is ignored (double press within the debounce).
- A loud red "AUDIO DEVICE LOST" banner while the output device is gone
  (set_device_lost), cleared when the device is back.
- Rehearsal tools on the right: "Loop selection" and "Go to bar".

The panel is a pure view: the app calls its setters on the Tk thread.
"""

import logging
from typing import Callable, Optional

import tkinter as tk
import customtkinter as ctk

from config import (
    COLOR_BG_MEDIUM, COLOR_BG_DARK, COLOR_TEXT, COLOR_TEXT_DIM,
    COLOR_BTN_SUCCESS, COLOR_BTN_PRIMARY, COLOR_BTN_DISABLED,
)
from utils.formatting import format_time
from utils.tooltip import ToolTip

logger = logging.getLogger("LoopStation.GoPanel")

# Colour per standby action: (GO button fill, standby text colour)
ACTION_COLORS = {
    "play": (COLOR_BTN_SUCCESS, "#4cd38a"),
    "release_hold": ("#f0a020", "#ffc04d"),
    "exit_vamp": ("#2f80d0", "#6db3ff"),
    "none": (COLOR_BTN_DISABLED, COLOR_TEXT_DIM),
}
HELD_BG = "#6b4200"
DEVICE_LOST_BG = "#5c0f0f"
DEVICE_LOST_BANNER = "#d63031"
DEVICE_LOST_TEXT = "⚠  AUDIO DEVICE LOST — reconnecting…"
LIVE_BORDER = "#d63031"
FLASH_MS = 1200

TYPE_LABELS = {"marker": "Cue", "vamp": "Vamp", "hold": "Hold", "loop": "Vamp"}


def item_time(item) -> Optional[float]:
    """Start time of a marker / vamp / hold object (None if unknown)."""
    for attr in ("time", "start"):
        value = getattr(item, attr, None)
        if isinstance(value, (int, float)):
            return float(value)
    return None


class GoPanel(ctk.CTkFrame):
    def __init__(self, parent, on_go: Callable[[], None],
                 on_loop_selection: Optional[Callable[[], None]] = None,
                 on_goto_bar: Optional[Callable[[int], None]] = None, **kwargs):
        super().__init__(parent, fg_color=COLOR_BG_MEDIUM, corner_radius=8,
                         border_width=0, border_color=LIVE_BORDER, **kwargs)
        self.on_go = on_go
        self.on_loop_selection = on_loop_selection
        self.on_goto_bar = on_goto_bar

        self._action = "none"
        self._held_name: Optional[str] = None
        self._standby_label = ""
        self._next_text = ""
        self._pass_text = ""
        self._flash_after = None
        self._live = False
        self._device_lost = False
        self._previewing = False
        self.go_key_hint = "Return"

        # --- Device-lost banner (packed above everything only when shown)
        self.lbl_device = ctk.CTkLabel(
            self, text=DEVICE_LOST_TEXT, fg_color=DEVICE_LOST_BANNER, corner_radius=6,
            text_color="#ffffff", font=("Segoe UI", 18, "bold"), height=40,
        )

        # --- GO button (left)
        self.btn_go = ctk.CTkButton(
            self, text="GO", width=150, height=64,
            font=("Segoe UI", 28, "bold"), corner_radius=10,
            fg_color=ACTION_COLORS["none"][0], hover_color="#2a9a5e",
            text_color="#ffffff", command=self._on_go_click,
        )
        self.btn_go.pack(side="left", padx=(12, 12), pady=8)
        self._go_tip = ToolTip(self.btn_go, "GO: perform the STANDBY action  (Return)")

        # --- Rehearsal tools (right)
        tools = ctk.CTkFrame(self, fg_color="transparent")
        tools.pack(side="right", padx=(6, 12), pady=6)

        self.btn_loop_sel = ctk.CTkButton(
            tools, text="🔁 Loop selection", width=140, height=28,
            font=("Segoe UI", 11), fg_color="transparent", border_width=1,
            border_color=COLOR_TEXT_DIM, text_color=COLOR_TEXT,
            hover_color=COLOR_BG_DARK, command=self._on_loop_sel_click,
        )
        self.btn_loop_sel.pack(side="top", anchor="e", pady=(0, 4))
        ToolTip(self.btn_loop_sel,
                "Rehearsal: loop the waveform selection (press again to stop)")

        self.goto_frame = ctk.CTkFrame(tools, fg_color="transparent")
        ctk.CTkLabel(self.goto_frame, text="Go to bar", font=("Segoe UI", 11),
                     text_color=COLOR_TEXT_DIM).pack(side="left", padx=(0, 4))
        self.entry_bar = ctk.CTkEntry(self.goto_frame, width=52, height=26,
                                      font=("Consolas", 12), justify="center")
        self.entry_bar.pack(side="left")
        self.entry_bar.bind("<Return>", lambda _e: self._on_goto_click())
        self.entry_bar.bind("<KP_Enter>", lambda _e: self._on_goto_click())
        self.btn_goto = ctk.CTkButton(
            self.goto_frame, text="Go", width=36, height=26, font=("Segoe UI", 11),
            fg_color=COLOR_BTN_PRIMARY, command=self._on_goto_click,
        )
        self.btn_goto.pack(side="left", padx=(4, 0))
        ToolTip(self.goto_frame, "Seek to the start of a bar (uses the song's BPM)")
        self._goto_visible = False

        # --- STANDBY text (middle)
        mid = ctk.CTkFrame(self, fg_color="transparent")
        mid.pack(side="left", fill="both", expand=True, pady=6)

        top = ctk.CTkFrame(mid, fg_color="transparent")
        top.pack(fill="x")
        self.lbl_caption = ctk.CTkLabel(top, text="STANDBY", font=("Segoe UI", 10, "bold"),
                                        text_color=COLOR_TEXT_DIM)
        self.lbl_caption.pack(side="left", padx=(0, 8))
        self.lbl_pass = ctk.CTkLabel(top, text="", font=("Segoe UI", 12, "bold"),
                                     text_color="#66bb6a")
        self.lbl_pass.pack(side="left")
        self.lbl_flash = ctk.CTkLabel(top, text="", font=("Segoe UI", 11, "bold"),
                                      text_color="#ff7b72")
        self.lbl_flash.pack(side="right", padx=(0, 6))

        self.lbl_standby = ctk.CTkLabel(mid, text="No song loaded", anchor="w",
                                        font=("Segoe UI", 18, "bold"),
                                        text_color=COLOR_TEXT_DIM)
        self.lbl_standby.pack(fill="x")
        self.lbl_next = ctk.CTkLabel(mid, text="", anchor="w", font=("Segoe UI", 11),
                                     text_color=COLOR_TEXT_DIM)
        self.lbl_next.pack(fill="x")

    # ------------------------------------------------------------------ input

    def _on_go_click(self):
        if self.on_go:
            self.on_go()

    def _on_loop_sel_click(self):
        if self.on_loop_selection:
            self.on_loop_selection()

    def _on_goto_click(self):
        text = self.entry_bar.get().strip()
        try:
            bar = int(float(text))
        except ValueError:
            self.flash("Enter a bar number", "#ff7b72")
            return
        if self.on_goto_bar:
            self.on_goto_bar(bar)

    # ---------------------------------------------------------------- setters

    def set_go_keys(self, keys_text: str):
        """Update the GO tooltip with the configured key names."""
        self.go_key_hint = keys_text
        try:
            self._go_tip.text = f"GO: perform the STANDBY action  ({keys_text})"
        except Exception:
            pass

    def set_standby(self, standby: Optional[dict]):
        """Show a standby() dict: label, colour by action, next cue."""
        standby = standby or {}
        self._action = standby.get("action") or "none"
        self._standby_label = standby.get("label") or ""
        item = standby.get("next_item")
        kind = standby.get("next_type")
        if item is not None and self._action != "release_hold":
            name = getattr(item, "name", "") or ""
            t = item_time(item)
            when = f"  @ {format_time(t)}" if t is not None else ""
            self._next_text = f"Next {TYPE_LABELS.get(kind, 'cue').lower()}: {name}{when}"
        else:
            self._next_text = ""
        self._render()

    def set_held(self, hold=None, held: bool = True):
        """Enter (held=True) or leave the loud HELD state."""
        if held:
            self._held_name = getattr(hold, "name", None) or "hold"
        else:
            self._held_name = None
        self._render()

    @property
    def is_held(self) -> bool:
        return self._held_name is not None

    def set_pass(self, n: Optional[int], total: Optional[int] = None):
        """Vamp pass counter; n=None clears it."""
        if n is None:
            self._pass_text = ""
        elif total:
            self._pass_text = f"♻ Pass {n} of {total}"
        else:
            self._pass_text = f"♻ Pass {n}"
        self.lbl_pass.configure(text=self._pass_text)

    def set_live(self, live: bool):
        """Live Mode: red border; "Loop selection" is a rehearsal tool, so it
        is disabled (unless a loop is running, so it can still be stopped)."""
        self._live = bool(live)
        self.configure(border_width=2 if self._live else 0)
        self._update_loop_sel_state()

    def _update_loop_sel_state(self):
        locked = self._live and not self._previewing
        self.btn_loop_sel.configure(
            state="disabled" if locked else "normal",
            text_color_disabled="#5a5a5a", border_color="#3a3a3a" if locked else COLOR_TEXT_DIM,
            text="🔒 Loop selection" if locked else (
                "⏹ Stop loop" if self._previewing else "🔁 Loop selection"))

    def set_device_lost(self, lost: bool):
        """Show / clear the loud AUDIO DEVICE LOST banner."""
        lost = bool(lost)
        if lost == self._device_lost:
            return
        self._device_lost = lost
        if lost:
            self.lbl_device.pack(side="top", fill="x", padx=8, pady=(8, 0), before=self.btn_go)
        else:
            self.lbl_device.pack_forget()
        self._render()

    @property
    def device_lost(self) -> bool:
        return self._device_lost

    def set_tempo_available(self, available: bool):
        """Show the Go-to-bar entry only when the song has a BPM."""
        if available and not self._goto_visible:
            self.goto_frame.pack(side="top", anchor="e")
            self._goto_visible = True
        elif not available and self._goto_visible:
            self.goto_frame.pack_forget()
            self._goto_visible = False

    def set_previewing(self, previewing: bool):
        self._previewing = bool(previewing)
        self._update_loop_sel_state()
        self.btn_loop_sel.configure(
            text="⏹ Stop loop" if previewing else "🔁 Loop selection",
            fg_color=COLOR_BTN_PRIMARY if previewing else "transparent",
        )

    def flash(self, text: str, color: str = "#ff7b72", ms: int = FLASH_MS):
        """Show a short message next to the STANDBY caption."""
        if self._flash_after is not None:
            try:
                self.after_cancel(self._flash_after)
            except Exception:
                pass
        self.lbl_flash.configure(text=text, text_color=color)

        def _clear():
            self._flash_after = None
            try:
                self.lbl_flash.configure(text="")
            except tk.TclError:
                pass
        try:
            self._flash_after = self.after(ms, _clear)
        except tk.TclError:
            self._flash_after = None

    # ----------------------------------------------------------------- render

    def _render(self):
        if self._held_name is not None:
            self.configure(fg_color=HELD_BG)
            self.lbl_caption.configure(text="⏸ HELD", text_color="#ffffff")
            self.lbl_standby.configure(text=f"HOLDING at {self._held_name}  —  press GO",
                                       text_color="#ffffff")
            self.lbl_next.configure(text=self._next_text, text_color="#f3d9a8")
            self.btn_go.configure(fg_color="#f0a020", hover_color="#ffb840",
                                  text_color="#000000", text="GO ▶")
            return
        btn_color, text_color = ACTION_COLORS.get(self._action, ACTION_COLORS["none"])
        self.configure(fg_color=DEVICE_LOST_BG if self._device_lost else COLOR_BG_MEDIUM)
        self.lbl_caption.configure(text="STANDBY", text_color=COLOR_TEXT_DIM)
        if self._device_lost:
            text_color = "#ffd6d6"      # readable on the dark red panel
        self.lbl_standby.configure(text=self._standby_label or "—", text_color=text_color)
        self.lbl_next.configure(text=self._next_text, text_color=COLOR_TEXT_DIM)
        dark_text = self._action == "release_hold"
        self.btn_go.configure(fg_color=btn_color, hover_color=btn_color,
                              text_color="#000000" if dark_text else "#ffffff",
                              text="GO")
