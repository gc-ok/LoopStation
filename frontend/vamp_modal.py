"""
Unified Vamp Settings Modal - All vamp configuration in one place!

This modal provides:
- Name editing
- Loop range (IN/OUT) with SET buttons and adjustments
- Repeat: loop until GO, or play the section N times in total
- Advanced timing sliders (collapsible)
- Delete and Save actions

All edits go through StateManager.update_selected_loop(); the modal never
mutates the LoopRegion itself. Slider motion is debounced so the engine is
only re-armed once the user stops dragging. Cancel restores the values the
vamp had when the modal was opened.

Layout: the Save / Cancel / Delete bar is packed at the bottom of the
Toplevel, outside the scrollable content, so it is always visible. The
window is sized to its content, capped to the screen work area (minus a
taskbar allowance) and clamped so it never extends past the screen edges;
when the content does not fit, the area above the button bar scrolls.
"""

import logging
import sys
import tkinter as tk
from typing import Callable, Optional, Tuple

import customtkinter as ctk

from config import (
    COLOR_BG_DARK, COLOR_BG_MEDIUM, COLOR_BG_LIGHT,
    COLOR_BTN_SUCCESS, COLOR_BTN_DANGER,
    COLOR_TEXT, COLOR_TEXT_DIM,
    COLOR_LOOP_IN, COLOR_LOOP_OUT, COLOR_BTN_TEXT,
    PADDING_MEDIUM, PADDING_LARGE,
    LOOP_CROSSFADE_MS, LOOP_SWITCH_EARLY_MS, FADE_EXIT_DURATION_MS,
)
from frontend.widgets import CollapsibleSection

logger = logging.getLogger("LoopStation.VampModal")

# Delay between the last slider motion event and the push to the backend.
SLIDER_DEBOUNCE_MS = 150

# Per-vamp fields the modal edits (all accepted by update_selected_loop).
EDITABLE_FIELDS = ("start", "end", "entry_fade_ms", "crossfade_ms",
                   "early_switch_ms", "exit_fade_ms", "name", "repeat_count")

# Repeat control: "Until GO" (repeat_count 0) or "Count" (heard N times total).
REPEAT_UNTIL_GO = "Until GO"
REPEAT_COUNT = "Count"
REPEAT_MIN = 2
REPEAT_MAX = 16


def normalize_repeat(value) -> int:
    """LoopRegion.repeat_count -> 0 (until GO) or N in [2, REPEAT_MAX]."""
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if n < REPEAT_MIN:
        return 0
    return n

# Sliders in the Advanced Timing section (all debounced through _apply).
SLIDER_FIELDS = ("entry_fade_ms", "crossfade_ms", "early_switch_ms", "exit_fade_ms")

# Window width in unscaled (customtkinter) units: wide enough that the
# IN/OUT rows (label + entry + -/+ + SET) never get clipped.
MODAL_WIDTH = 520
# Smallest useful content height (unscaled); below this the modal is pointless.
MODAL_MIN_HEIGHT = 260
# Physical pixels kept free at the bottom of the screen for the taskbar.
TASKBAR_ALLOWANCE = 80
# Title-bar height estimate (unscaled); the WM adds this to the client area.
TITLEBAR_ESTIMATE = 32


class VampModal(ctk.CTkToplevel):
    """
    Complete vamp configuration modal.

    Provides all settings in one unified interface:
    - Vamp name
    - Loop range (IN/OUT points)
    - Advanced timing settings (collapsible)
    - Delete/Save actions
    """

    def __init__(
        self,
        parent: tk.Widget,
        loop,
        state_manager,
        on_close: Optional[Callable] = None
    ):
        super().__init__(parent)

        self.loop = loop
        # NOTE: must not be called ``self.state`` - that would shadow
        # tkinter's Toplevel.state(), which customtkinter calls internally.
        self.app_state = state_manager
        self.on_close = on_close
        self._closed = False

        # Snapshot so Cancel can restore exactly what the vamp had on open.
        self._original = {field: self._field_value(field) for field in EDITABLE_FIELDS}
        # Count shown by the stepper (kept while "Until GO" is selected).
        self._repeat_n = normalize_repeat(getattr(loop, "repeat_count", None)) or REPEAT_MIN

        # Debounced slider state: attr -> pending value, plus the after() id.
        self._pending_slider_values = {}
        self._slider_after_id = None

        # Window setup
        self.title(f"Vamp Settings: {loop.name}")
        self.resizable(False, True)
        self.minsize(MODAL_WIDTH, MODAL_MIN_HEIGHT)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel_click)

        # Make modal
        self.transient(parent)
        self.grab_set()

        # Store slider references
        self.sliders = {}
        self.labels = {}

        self._create_widgets()
        self._load_values()

        # Keyboard: Enter in the name field saves, Escape cancels anywhere.
        self.entry_name.bind("<Return>", lambda _e: self._on_save_click())
        self.bind("<Escape>", lambda _e: self._on_cancel_click())

        # Size to content (capped to the screen), centre on parent, clamp.
        self._fit_to_screen(center_on=parent)
        self.entry_name.focus_set()

    # =========================================================================
    # WIDGETS
    # =========================================================================

    def _create_widgets(self):
        """Create all modal widgets."""
        # Main container
        main = ctk.CTkFrame(self, fg_color=COLOR_BG_MEDIUM, corner_radius=0)
        main.pack(fill="both", expand=True, padx=0, pady=0)

        # =====================================================================
        # Fixed action bar (packed FIRST, side=bottom, so it is always visible
        # regardless of how tall the scrollable content is).
        # =====================================================================
        self._actions = ctk.CTkFrame(main, fg_color=COLOR_BG_DARK, corner_radius=0)
        self._actions.pack(side="bottom", fill="x")

        actions = ctk.CTkFrame(self._actions, fg_color="transparent")
        actions.pack(fill="x", pady=PADDING_MEDIUM, padx=PADDING_LARGE)

        # Delete button (left)
        ctk.CTkButton(
            actions, text="🗑 Delete Vamp", width=130, height=40,
            font=("Segoe UI", 12, "bold"),
            fg_color=COLOR_BTN_DANGER,
            text_color="#ffffff",
            command=self._on_delete_click
        ).pack(side="left")

        # Cancel button (right)
        ctk.CTkButton(
            actions, text="Cancel", width=100, height=40,
            font=("Segoe UI", 12),
            fg_color=COLOR_BG_LIGHT,
            text_color=COLOR_TEXT,
            command=self._on_cancel_click
        ).pack(side="right", padx=5)

        # Save button (right)
        ctk.CTkButton(
            actions, text="💾 Save", width=120, height=40,
            font=("Segoe UI", 12, "bold"),
            fg_color=COLOR_BTN_SUCCESS,
            text_color=COLOR_BTN_TEXT,
            command=self._on_save_click
        ).pack(side="right")

        # =====================================================================
        # Scrollable content area (everything above the action bar)
        # =====================================================================
        self._scroll = ctk.CTkScrollableFrame(
            main,
            fg_color="transparent",
            scrollbar_button_color="#5a5a5a",        # visible against the panel bg
            scrollbar_button_hover_color="#7a7a7a",
        )
        self._scroll.pack(side="top", fill="both", expand=True,
                          padx=(PADDING_LARGE, PADDING_MEDIUM), pady=PADDING_LARGE)
        scroll_frame = self._scroll

        # =====================================================================
        # SECTION 1: Vamp Name
        # =====================================================================
        ctk.CTkLabel(
            scroll_frame, text="📝 Vamp Name",
            font=("Segoe UI", 13, "bold"),
            text_color=COLOR_TEXT
        ).pack(anchor="w", pady=(0, 5))

        self.entry_name = ctk.CTkEntry(
            scroll_frame,
            height=40,
            font=("Segoe UI", 13),
            fg_color=COLOR_BG_DARK,
            border_color=COLOR_BG_LIGHT
        )
        self.entry_name.pack(fill="x", pady=(0, PADDING_LARGE))

        # =====================================================================
        # SECTION 2: Loop Range
        # =====================================================================
        ctk.CTkLabel(
            scroll_frame, text="🎵 Loop Range",
            font=("Segoe UI", 13, "bold"),
            text_color=COLOR_TEXT
        ).pack(anchor="w", pady=(0, 5))

        range_frame = ctk.CTkFrame(scroll_frame, fg_color=COLOR_BG_DARK, corner_radius=6)
        range_frame.pack(fill="x", pady=(0, PADDING_LARGE), padx=0, ipady=10)

        # IN row
        in_row = ctk.CTkFrame(range_frame, fg_color="transparent")
        in_row.pack(fill="x", pady=5, padx=PADDING_MEDIUM)

        ctk.CTkLabel(
            in_row, text="IN:",
            font=("Segoe UI", 11, "bold"),
            text_color=COLOR_LOOP_IN,
            width=35
        ).pack(side="left")

        self.entry_in = ctk.CTkEntry(
            in_row, width=110,
            font=("Consolas", 12),
            fg_color=COLOR_BG_MEDIUM
        )
        self.entry_in.pack(side="left", padx=5)
        self.entry_in.bind("<Return>", self._on_manual_change)
        self.entry_in.bind("<FocusOut>", self._on_manual_change)

        ctk.CTkButton(
            in_row, text="−", width=35, height=28,
            fg_color=COLOR_BG_LIGHT,
            command=lambda: self._adjust_in(-0.01)
        ).pack(side="left", padx=2)

        ctk.CTkButton(
            in_row, text="+", width=35, height=28,
            fg_color=COLOR_BG_LIGHT,
            command=lambda: self._adjust_in(0.01)
        ).pack(side="left", padx=2)

        ctk.CTkButton(
            in_row, text="⬇ SET", width=70, height=28,
            fg_color=COLOR_LOOP_IN,
            text_color="#000000",
            font=("Segoe UI", 10, "bold"),
            command=self._set_in_current
        ).pack(side="left", padx=5)

        # OUT row
        out_row = ctk.CTkFrame(range_frame, fg_color="transparent")
        out_row.pack(fill="x", pady=5, padx=PADDING_MEDIUM)

        ctk.CTkLabel(
            out_row, text="OUT:",
            font=("Segoe UI", 11, "bold"),
            text_color=COLOR_LOOP_OUT,
            width=35
        ).pack(side="left")

        self.entry_out = ctk.CTkEntry(
            out_row, width=110,
            font=("Consolas", 12),
            fg_color=COLOR_BG_MEDIUM
        )
        self.entry_out.pack(side="left", padx=5)
        self.entry_out.bind("<Return>", self._on_manual_change)
        self.entry_out.bind("<FocusOut>", self._on_manual_change)

        ctk.CTkButton(
            out_row, text="−", width=35, height=28,
            fg_color=COLOR_BG_LIGHT,
            command=lambda: self._adjust_out(-0.01)
        ).pack(side="left", padx=2)

        ctk.CTkButton(
            out_row, text="+", width=35, height=28,
            fg_color=COLOR_BG_LIGHT,
            command=lambda: self._adjust_out(0.01)
        ).pack(side="left", padx=2)

        ctk.CTkButton(
            out_row, text="⬇ SET", width=70, height=28,
            fg_color=COLOR_LOOP_OUT,
            text_color="#000000",
            font=("Segoe UI", 10, "bold"),
            command=self._set_out_current
        ).pack(side="left", padx=5)

        # Duration display
        self.lbl_duration = ctk.CTkLabel(
            range_frame,
            text="Duration: 0.000s",
            font=("Consolas", 11),
            text_color=COLOR_TEXT_DIM
        )
        self.lbl_duration.pack(anchor="e", pady=(5, 5), padx=PADDING_MEDIUM)

        # =====================================================================
        # SECTION 3: Repeat
        # =====================================================================
        self._create_repeat_section(scroll_frame)

        # =====================================================================
        # SECTION 4: Advanced Settings (Collapsible)
        # =====================================================================
        self.advanced_section = CollapsibleSection(scroll_frame, "🛠️ Advanced Timing")
        self.advanced_section.pack(fill="x", pady=(0, PADDING_MEDIUM))

        # Grow/shrink the window (within the screen) when the section toggles.
        section_toggle = self.advanced_section.toggle

        def _toggle_and_refit():
            section_toggle()
            self.after_idle(self._fit_to_screen)

        self.advanced_section.toggle = _toggle_and_refit

        # Add sliders to the collapsible content
        self._add_slider(
            self.advanced_section.content,
            "Smooth Entry (Fade-In)",
            0, 500, "ms", "entry_fade_ms",
            "Fade-in duration when entering loop mode.",
            default=15
        )

        self._add_slider(
            self.advanced_section.content,
            "Loop Seam Crossfade",
            0, 2000, "ms", "crossfade_ms",
            "Blends end of loop back into start.",
            default=LOOP_CROSSFADE_MS
        )

        self._add_slider(
            self.advanced_section.content,
            "Timing Offset (Early Switch)",
            0, 200, "ms", "early_switch_ms",
            "How far before the OUT point to switch into the loop (0 = exactly at OUT).",
            default=LOOP_SWITCH_EARLY_MS
        )

        self._add_slider(
            self.advanced_section.content,
            "Fade Exit Duration",
            100, 10000, "ms", "exit_fade_ms",
            "Duration when fading out of loop.",
            default=FADE_EXIT_DURATION_MS
        )

    def _create_repeat_section(self, parent):
        """Repeat mode: until GO, or a stepper for the total number of plays."""
        ctk.CTkLabel(
            parent, text="🔂 Repeat",
            font=("Segoe UI", 13, "bold"),
            text_color=COLOR_TEXT
        ).pack(anchor="w", pady=(0, 5))

        box = ctk.CTkFrame(parent, fg_color=COLOR_BG_DARK, corner_radius=6)
        box.pack(fill="x", pady=(0, PADDING_LARGE))

        row = ctk.CTkFrame(box, fg_color="transparent", height=36)
        row.pack(fill="x", padx=PADDING_MEDIUM, pady=(8, 2))
        row.pack_propagate(False)

        self.repeat_mode = ctk.CTkSegmentedButton(
            row, values=[REPEAT_UNTIL_GO, REPEAT_COUNT],
            width=180, height=30,
            font=("Segoe UI", 11, "bold"),
            command=self._on_repeat_mode
        )
        self.repeat_mode.pack(side="left")

        # Stepper (shown only for "Count")
        self._repeat_stepper = ctk.CTkFrame(row, fg_color="transparent")
        self._btn_repeat_minus = ctk.CTkButton(
            self._repeat_stepper, text="−", width=32, height=28,
            fg_color=COLOR_BG_LIGHT,
            command=lambda: self._step_repeat(-1)
        )
        self._btn_repeat_minus.pack(side="left", padx=(14, 2))
        self.lbl_repeat_n = ctk.CTkLabel(
            self._repeat_stepper, text=str(self._repeat_n), width=34,
            font=("Consolas", 14, "bold"), text_color="#88aaff"
        )
        self.lbl_repeat_n.pack(side="left")
        self._btn_repeat_plus = ctk.CTkButton(
            self._repeat_stepper, text="+", width=32, height=28,
            fg_color=COLOR_BG_LIGHT,
            command=lambda: self._step_repeat(1)
        )
        self._btn_repeat_plus.pack(side="left", padx=(2, 8))
        self.lbl_repeat_desc = ctk.CTkLabel(
            self._repeat_stepper, text="", font=("Segoe UI", 11),
            text_color=COLOR_TEXT
        )
        self.lbl_repeat_desc.pack(side="left")

        self.lbl_repeat_hint = ctk.CTkLabel(
            box, text="", font=("Segoe UI", 9), text_color=COLOR_TEXT_DIM,
            wraplength=440, justify="left", anchor="w"
        )
        self.lbl_repeat_hint.pack(fill="x", padx=PADDING_MEDIUM, pady=(0, 8))

    def _show_repeat(self, count: int):
        """Reflect ``count`` (0 = until GO) in the repeat widgets (no backend push)."""
        if count >= REPEAT_MIN:
            self._repeat_n = count
            self.repeat_mode.set(REPEAT_COUNT)
            self.lbl_repeat_n.configure(text=str(count))
            self.lbl_repeat_desc.configure(text=f"Play {count} times total")
            self._btn_repeat_minus.configure(state="normal" if count > REPEAT_MIN else "disabled")
            self._btn_repeat_plus.configure(state="normal" if count < REPEAT_MAX else "disabled")
            self._repeat_stepper.pack(side="left")
            self.lbl_repeat_hint.configure(
                text=f"The section is heard {count} times, then playback carries on. "
                     "GO still exits early.")
        else:
            self.repeat_mode.set(REPEAT_UNTIL_GO)
            self._repeat_stepper.pack_forget()
            self.lbl_repeat_hint.configure(
                text="The section loops until you press GO (or exit the vamp).")

    def _on_repeat_mode(self, choice):
        count = self._repeat_n if choice == REPEAT_COUNT else 0
        self._show_repeat(count)
        self._schedule_push("repeat_count", count)

    def _step_repeat(self, delta: int):
        count = max(REPEAT_MIN, min(REPEAT_MAX, self._repeat_n + delta))
        if count == self._repeat_n and self.repeat_mode.get() == REPEAT_COUNT:
            return
        self._show_repeat(count)
        self._schedule_push("repeat_count", count)

    def _add_slider(self, parent, label_text, min_val, max_val, unit, attr_name, tooltip, default=0):
        """Add a slider control with label."""
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.pack(fill="x", pady=8)

        # Label row
        lbl_frame = ctk.CTkFrame(frame, fg_color="transparent")
        lbl_frame.pack(fill="x")

        ctk.CTkLabel(
            lbl_frame, text=label_text,
            font=("Segoe UI", 11, "bold"),
            text_color=COLOR_TEXT
        ).pack(side="left")

        # Value label
        val_lbl = ctk.CTkLabel(
            lbl_frame,
            text=f"{int(default)} {unit}",
            font=("Consolas", 11),
            text_color="#88aaff"
        )
        val_lbl.pack(side="right")

        # Tooltip
        if tooltip:
            ctk.CTkLabel(
                frame,
                text=tooltip,
                font=("Segoe UI", 9),
                text_color=COLOR_TEXT_DIM,
                wraplength=380,
                justify="left"
            ).pack(fill="x", pady=(2, 5))

        # Slider
        slider = ctk.CTkSlider(
            frame,
            from_=min_val,
            to=max_val,
            number_of_steps=(max_val - min_val),
            command=lambda v: self._on_slider_change(v, val_lbl, unit, attr_name)
        )
        slider.set(default)
        slider.pack(fill="x", pady=(0, 5))

        # Store references
        self.sliders[attr_name] = slider
        self.labels[attr_name] = val_lbl

    # =========================================================================
    # WINDOW GEOMETRY
    # =========================================================================

    def _work_area(self, anchor: tk.Widget) -> Tuple[int, int, int, int]:
        """
        Usable screen rectangle (left, top, right, bottom) in the same pixel
        units as window geometry.

        Default: winfo_screenwidth/height minus a taskbar allowance. On
        Windows the work area of the monitor showing ``anchor`` is used
        instead when it can be queried: it excludes the real taskbar, works
        on multi-monitor setups, and - unlike Tk's screen metrics, which are
        cached before customtkinter switches the process to per-monitor DPI
        awareness - it is in the same coordinate space as the window.
        """
        fallback = (0, 0, self.winfo_screenwidth(),
                    self.winfo_screenheight() - TASKBAR_ALLOWANCE)
        if not sys.platform.startswith("win"):
            return fallback
        try:
            import ctypes
            from ctypes import wintypes

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD),
                            ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT),
                            ("dwFlags", wintypes.DWORD)]

            user32 = ctypes.windll.user32
            MONITOR_DEFAULTTONEAREST = 2
            hmon = user32.MonitorFromWindow(anchor.winfo_id(), MONITOR_DEFAULTTONEAREST)
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                r = info.rcWork
                if r.right > r.left and r.bottom > r.top:
                    return r.left, r.top, r.right, r.bottom
        except Exception:
            pass
        return fallback

    def _fit_to_screen(self, center_on: Optional[tk.Widget] = None):
        """
        Size the window to its content, capped to the screen work area, and
        position it (centred on ``center_on`` when given, otherwise keeping
        the current position) clamped inside the screen.

        All measurements are physical pixels; customtkinter scales the
        width/height part of geometry strings, so those are divided back by
        the window scaling before being handed to geometry().
        """
        if self._closed:
            return
        try:
            self.update_idletasks()
            scaling = float(ctk.ScalingTracker.get_window_scaling(self)) or 1.0

            # Content height: the scrollable frame's inner frame requests the
            # full height of its children; add its outer padding and the bar.
            content_h = self._scroll.winfo_reqheight() + round(2 * PADDING_LARGE * scaling)
            bar_h = self._actions.winfo_reqheight()
            desired_h = content_h + bar_h + round(4 * scaling)

            width = round(MODAL_WIDTH * scaling)
            titlebar_h = round(TITLEBAR_ESTIMATE * scaling)
            left, top, right, bottom = self._work_area(center_on or self)
            max_client_h = (bottom - top) - titlebar_h
            min_client_h = round(MODAL_MIN_HEIGHT * scaling)
            height = max(min(desired_h, max_client_h), min(min_client_h, max_client_h))

            if center_on is not None:
                px = center_on.winfo_rootx() + (center_on.winfo_width() - width) // 2
                py = center_on.winfo_rooty() + (center_on.winfo_height() - height) // 2
            else:
                px, py = self.winfo_x(), self.winfo_y()

            # Clamp so the whole window (including title bar) stays on screen.
            px = max(left, min(px, right - width))
            py = max(top, min(py, bottom - height - titlebar_h))

            self.geometry(f"{round(width / scaling)}x{round(height / scaling)}+{px}+{py}")
        except tk.TclError:
            pass  # window already destroyed

    # =========================================================================
    # SLIDERS (debounced)
    # =========================================================================

    def _on_slider_change(self, value, label, unit, attr_name):
        """Slider motion: update the label now, push to the backend after a pause."""
        val = int(value)
        label.configure(text=f"{val} {unit}")

        self._schedule_push(attr_name, val)

    def _schedule_push(self, field, value):
        """Queue ``field=value`` for the debounced update_selected_loop call."""
        self._pending_slider_values[field] = value
        if self._slider_after_id is not None:
            self.after_cancel(self._slider_after_id)
        self._slider_after_id = self.after(SLIDER_DEBOUNCE_MS, self._flush_slider_values)

    def _flush_slider_values(self):
        """Push any pending slider values to the backend in one call."""
        self._slider_after_id = None
        if self._closed or not self.winfo_exists():
            return
        pending = self._pending_slider_values
        self._pending_slider_values = {}
        if not pending:
            return
        self._apply(**pending)

    def _cancel_pending_slider(self):
        """Drop any scheduled (not yet applied) slider push."""
        if self._slider_after_id is not None:
            try:
                self.after_cancel(self._slider_after_id)
            except Exception:
                pass
            self._slider_after_id = None
        self._pending_slider_values = {}

    # =========================================================================
    # BACKEND ACCESS
    # =========================================================================

    def _ensure_selected(self) -> bool:
        """
        Make sure the backend's selected vamp is the one this modal edits.

        The selection can move while the modal is open (e.g. the user clicks
        another row, or a vamp is deleted), so compare ids rather than trust
        selected_loop_index. Returns False if this vamp no longer exists.
        """
        loops = self.app_state.loops
        for idx, loop in enumerate(loops):
            if loop.id == self.loop.id:
                if idx != self.app_state.selected_loop_index:
                    self.app_state.select_loop(idx)
                return True
        logger.warning(f"Vamp '{self.loop.name}' no longer exists; edit ignored")
        return False

    def _field_value(self, field):
        """Current value of an editable field (repeat_count normalised to 0/N)."""
        value = getattr(self.loop, field, None)
        if field == "repeat_count":
            return normalize_repeat(value)
        return value

    def _apply(self, **fields) -> bool:
        """Apply field changes to this modal's vamp via update_selected_loop."""
        if not self._ensure_selected():
            return False
        ok = self.app_state.update_selected_loop(**fields)
        if not ok:
            logger.debug(f"update_selected_loop rejected {fields}")
        return ok

    # =========================================================================
    # DISPLAY
    # =========================================================================

    def _load_values(self):
        """Load current loop values into the UI."""
        # Name
        self.entry_name.delete(0, "end")
        self.entry_name.insert(0, self.loop.name)

        # Range
        self._update_range_display()

        # Sliders
        for attr in SLIDER_FIELDS:
            value = int(getattr(self.loop, attr))
            self.sliders[attr].set(value)
            self.labels[attr].configure(text=f"{value} ms")

        # Repeat
        self._show_repeat(normalize_repeat(getattr(self.loop, "repeat_count", None)))

    def _update_range_display(self):
        """Update the IN/OUT entry fields and duration label."""
        self.entry_in.delete(0, "end")
        self.entry_in.insert(0, f"{self.loop.start:.3f}")

        self.entry_out.delete(0, "end")
        self.entry_out.insert(0, f"{self.loop.end:.3f}")

        duration = self.loop.end - self.loop.start
        self.lbl_duration.configure(text=f"Duration: {duration:.3f}s")

    # =========================================================================
    # LOOP RANGE
    # =========================================================================

    def _adjust_in(self, amount):
        """Adjust IN point by amount."""
        new_val = max(0, self.loop.start + amount)
        if new_val < self.loop.end:
            self._apply(start=new_val)
        self._update_range_display()

    def _adjust_out(self, amount):
        """Adjust OUT point by amount."""
        new_val = min(self.app_state.song_length, self.loop.end + amount)
        if new_val > self.loop.start:
            self._apply(end=new_val)
        self._update_range_display()

    def _set_in_current(self):
        """Set IN to current playback position."""
        pos = self.app_state.get_position()
        if pos < self.loop.end:
            self._apply(start=pos)
        self._update_range_display()

    def _set_out_current(self):
        """Set OUT to current playback position."""
        pos = self.app_state.get_position()
        if pos > self.loop.start:
            self._apply(end=pos)
        self._update_range_display()

    def _on_manual_change(self, event=None):
        """Handle manual entry of IN/OUT times."""
        if self._closed:
            return
        try:
            in_val = float(self.entry_in.get().strip())
            out_val = float(self.entry_out.get().strip())
        except ValueError:
            # Invalid input, revert to current values
            self._update_range_display()
            return

        if 0 <= in_val < out_val <= self.app_state.song_length:
            if (in_val, out_val) != (self.loop.start, self.loop.end):
                self._apply(start=in_val, end=out_val)
        self._update_range_display()

    # =========================================================================
    # ACTIONS
    # =========================================================================

    def _close(self):
        """Tear down: cancel timers, notify the owner, destroy the window."""
        if self._closed:
            return
        self._closed = True
        self._cancel_pending_slider()
        if self.on_close:
            self.on_close()
        self.destroy()

    def _on_save_click(self):
        """Save changes and close."""
        if self._closed:
            return
        # Push anything still waiting on the slider debounce.
        if self._slider_after_id is not None:
            self.after_cancel(self._slider_after_id)
            self._slider_after_id = None
        pending = self._pending_slider_values
        self._pending_slider_values = {}

        new_name = self.entry_name.get().strip()
        if new_name and new_name != self.loop.name:
            pending["name"] = new_name

        if pending:
            self._apply(**pending)

        self._close()

    def _on_delete_click(self):
        """Delete this vamp."""
        if self._closed:
            return
        # Confirm deletion
        from tkinter import messagebox
        if messagebox.askyesno("Delete Vamp", f"Delete '{self.loop.name}'?", parent=self):
            self._cancel_pending_slider()
            if self._ensure_selected():
                self.app_state.delete_selected_loop()
            self._close()

    def _on_cancel_click(self):
        """Cancel: restore the values the vamp had when the modal opened."""
        if self._closed:
            return
        self._cancel_pending_slider()

        # Only touch the backend if something actually changed.
        changed = {
            field: value for field, value in self._original.items()
            if self._field_value(field) != value
        }
        if changed:
            logger.debug(f"Cancel: restoring {sorted(changed)}")
            self._apply(**changed)

        self._close()
