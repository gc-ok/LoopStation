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
import math
from typing import Callable, Optional

import tkinter as tk
import customtkinter as ctk

from utils.fonts import FONT_UI, FONT_MONO
from config import (
    COLOR_BG_LIGHT, COLOR_BORDER, mix, COLOR_HOVER, COLOR_TEXT_DISABLED,
    COLOR_BG_MEDIUM, COLOR_TEXT, COLOR_TEXT_DIM,
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
    "next_song": (COLOR_BTN_SUCCESS, "#4cd38a"),
    "none": (COLOR_BTN_DISABLED, COLOR_TEXT_DIM),
}
HELD_BG = "#6b4200"
DEVICE_LOST_BG = "#5c0f0f"
DEVICE_LOST_BANNER = "#d63031"
DEVICE_LOST_TEXT = "⚠  AUDIO DEVICE LOST — reconnecting…"
LIVE_BORDER = "#d63031"
FLASH_MS = 1200
# HELD: the GO border breathes so the waiting cue is hard to miss
PULSE_MS = 60
PULSE_STEPS = 20          # one breath = PULSE_MS * PULSE_STEPS (1.2 s)
HELD_GO = "#f0a020"

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
        self._pulse_after = None
        self._pulse_phase = 0
        self._live = False
        self._device_lost = False
        self._previewing = False
        self.go_key_hint = "Return"

        # --- Device-lost banner (packed above everything only when shown)
        self.lbl_device = ctk.CTkLabel(
            self, text=DEVICE_LOST_TEXT, fg_color=DEVICE_LOST_BANNER, corner_radius=6,
            text_color="#ffffff", font=(FONT_UI, 18, "bold"), height=40,
        )

        # --- GO button (left)
        self.btn_go = ctk.CTkButton(
            self, text="GO", width=150, height=64,
            font=(FONT_UI, 28, "bold"), corner_radius=10,
            fg_color=ACTION_COLORS["none"][0], hover_color="#2a9a5e",
            text_color="#ffffff", command=self._on_go_click,
        )
        self.btn_go.pack(side="left", padx=(12, 12), pady=8)
        self._go_tip = ToolTip(self.btn_go, "GO: perform the STANDBY action  (Return)")

        # --- Rehearsal tools (right)
        tools = ctk.CTkFrame(self, fg_color="transparent")
        tools.pack(side="right", padx=(6, 12), pady=6)

        self.btn_loop_sel = ctk.CTkButton(
            tools, text="↻ Loop selection", width=140, height=28,
            font=(FONT_UI, 11), fg_color=COLOR_BG_LIGHT, border_width=1,
            border_color=COLOR_BORDER, text_color=COLOR_TEXT,
            hover_color=COLOR_HOVER, command=self._on_loop_sel_click,
        )
        self.btn_loop_sel.pack(side="top", anchor="e", pady=(0, 4))
        ToolTip(self.btn_loop_sel,
                "Rehearsal: loop the waveform selection (press again to stop)")

        self.goto_frame = ctk.CTkFrame(tools, fg_color="transparent")
        ctk.CTkLabel(self.goto_frame, text="Go to bar", font=(FONT_UI, 11),
                     text_color=COLOR_TEXT_DIM).pack(side="left", padx=(0, 4))
        self.entry_bar = ctk.CTkEntry(self.goto_frame, width=52, height=26,
                                      font=(FONT_MONO, 12), justify="center")
        self.entry_bar.pack(side="left")
        self.entry_bar.bind("<Return>", lambda _e: self._on_goto_click())
        self.entry_bar.bind("<KP_Enter>", lambda _e: self._on_goto_click())
        self.btn_goto = ctk.CTkButton(
            self.goto_frame, text="Go", width=36, height=26, font=(FONT_UI, 11),
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
        self.lbl_caption = ctk.CTkLabel(top, text="STANDBY", font=(FONT_UI, 10, "bold"),
                                        text_color=COLOR_TEXT_DIM)
        self.lbl_caption.pack(side="left", padx=(0, 8))
        self.lbl_pass = ctk.CTkLabel(top, text="", font=(FONT_UI, 12, "bold"),
                                     text_color="#66bb6a")
        self.lbl_pass.pack(side="left")
        self.lbl_flash = ctk.CTkLabel(top, text="", font=(FONT_UI, 11, "bold"),
                                      text_color="#ff7b72")
        self.lbl_flash.pack(side="right", padx=(0, 6))

        self.lbl_standby = ctk.CTkLabel(mid, text="No song loaded", anchor="w",
                                        font=(FONT_UI, 18, "bold"),
                                        text_color=COLOR_TEXT_DIM)
        self.lbl_standby.pack(fill="x")
        self.lbl_next = ctk.CTkLabel(mid, text="", anchor="w", font=(FONT_UI, 11),
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
        if self._live:
            # Seeking is locked in Live Mode (Return in the entry included)
            self.flash("locked (Live Mode)", "#ff7b72")
            return "break"
        text = self.entry_bar.get().strip()
        try:
            bar = int(float(text))      # "inf" -> OverflowError, "nan" -> ValueError
        except (ValueError, OverflowError):
            self.flash("Enter a bar number", "#ff7b72")
            return "break"
        if self.on_goto_bar:
            self.on_goto_bar(bar)
        return "break"

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
        # Go to bar seeks: locked while live
        state = "disabled" if self._live else "normal"
        for w in (self.entry_bar, self.btn_goto):
            try:
                w.configure(state=state)
            except (tk.TclError, ValueError):
                pass
        if self._live:
            # A focused (now disabled) entry would swallow Return: hand the
            # keyboard back to the window so GO keeps working.
            try:
                focused = self.focus_get()
                if focused is not None and str(focused).startswith(str(self.entry_bar)):
                    self.winfo_toplevel().focus_set()
            except (KeyError, tk.TclError):
                pass

    def _update_loop_sel_state(self):
        locked = self._live and not self._previewing
        self.btn_loop_sel.configure(
            state="disabled" if locked else "normal",
            text_color_disabled=COLOR_TEXT_DISABLED, border_color=COLOR_BORDER,
            text="Loop selection (locked)" if locked else (
                "⏹ Stop loop" if self._previewing else "↻ Loop selection"))

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
        # Text comes from _update_loop_sel_state (keeps "🔒" while live)
        self._update_loop_sel_state()
        self.btn_loop_sel.configure(
            fg_color=COLOR_BTN_PRIMARY if previewing else COLOR_BG_LIGHT,
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
            self.btn_go.configure(fg_color=HELD_GO, hover_color="#ffb840",
                                  text_color="#000000", text="GO ▶")
            self._set_pulse(True)
            return
        self._set_pulse(False)
        btn_color, text_color = ACTION_COLORS.get(self._action, ACTION_COLORS["none"])
        self.configure(fg_color=DEVICE_LOST_BG if self._device_lost else COLOR_BG_MEDIUM)
        self.lbl_caption.configure(text="STANDBY", text_color=COLOR_TEXT_DIM)
        if self._device_lost:
            text_color = "#ffd6d6"      # readable on the dark red panel
        self.lbl_standby.configure(text=self._standby_label or "—", text_color=text_color)
        self.lbl_next.configure(text=self._next_text, text_color=COLOR_TEXT_DIM)
        dark_text = self._action == "release_hold"
        idle = self._action not in ACTION_COLORS or self._action == "none"
        self.btn_go.configure(fg_color=btn_color, hover_color=btn_color,
                              text_color=(COLOR_TEXT_DISABLED if idle else
                                          "#000000" if dark_text else "#ffffff"),
                              text="GO")

    # ------------------------------------------------------------------ pulse

    def _set_pulse(self, on: bool):
        """Start / stop the HELD breathing border on the GO button."""
        if on:
            if self._pulse_after is None:
                self._pulse_phase = 0
                self._pulse_tick()
            return
        if self._pulse_after is not None:
            try:
                self.after_cancel(self._pulse_after)
            except Exception:
                pass
            self._pulse_after = None
            try:
                self.btn_go.configure(border_width=0)
            except tk.TclError:
                pass

    def _pulse_tick(self):
        t = (1 - math.cos(2 * math.pi * self._pulse_phase / PULSE_STEPS)) / 2
        self._pulse_phase = (self._pulse_phase + 1) % PULSE_STEPS
        try:
            self.btn_go.configure(border_width=3,
                                  border_color=mix(HELD_GO, "#ffffff", 0.75 * t))
            self._pulse_after = self.after(PULSE_MS, self._pulse_tick)
        except tk.TclError:          # destroyed mid-animation
            self._pulse_after = None

    def destroy(self):
        self._set_pulse(False)
        super().destroy()
