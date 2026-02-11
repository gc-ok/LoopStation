"""
Loop Controls Widget for Loop Station.

Contains:
- Vamp (loop) navigation with names
- Rename vamp functionality
- Set In/Out buttons
- Loop point adjustment (+/- buttons)
- Loop point entry fields
- Exit Loop button (with fade-out option)
- Save button
- Delete loop button
"""

import logging
import tkinter as tk
from typing import Callable, Optional

import customtkinter as ctk

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import (
    COLOR_BTN_PRIMARY, COLOR_BTN_SUCCESS, COLOR_BTN_WARNING, 
    COLOR_BTN_DISABLED, COLOR_BTN_DANGER, COLOR_LOOP_IN, COLOR_LOOP_OUT,
    COLOR_TEXT, COLOR_TEXT_DIM, COLOR_BG_LIGHT, COLOR_BG_MEDIUM,
    BTN_HEIGHT, BTN_FONT_SIZE, PADDING_SMALL, PADDING_MEDIUM,
    FADE_EXIT_DURATION_MS, FADE_EXIT_MIN_MS, FADE_EXIT_MAX_MS, COLOR_BTN_TEXT
)

logger = logging.getLogger("LoopStation.LoopControls")


class LoopControls(ctk.CTkFrame):
    """
    Loop control panel with named vamp management, in/out settings, and fade exit.
    """
    
    def __init__(
        self,
        parent: tk.Widget,
        on_set_in: Optional[Callable] = None,
        on_set_out: Optional[Callable] = None,
        on_adjust_in: Optional[Callable[[float], None]] = None,
        on_adjust_out: Optional[Callable[[float], None]] = None,
        on_loop_points_changed: Optional[Callable[[float, float], None]] = None,
        on_exit_loop: Optional[Callable] = None,
        on_fade_exit: Optional[Callable[[int], None]] = None,
        on_save: Optional[Callable] = None,
        on_add_loop: Optional[Callable] = None,
        on_next_loop: Optional[Callable] = None,
        on_prev_loop: Optional[Callable] = None,
        on_rename_loop: Optional[Callable[[int, str], None]] = None,
        on_delete_loop: Optional[Callable] = None,
        on_settings=None,
        **kwargs
    ):
        super().__init__(parent, fg_color="transparent", **kwargs)
        
        self.on_set_in = on_set_in
        self.on_settings = on_settings
        self.on_set_out = on_set_out
        self.on_adjust_in = on_adjust_in
        self.on_adjust_out = on_adjust_out
        self.on_loop_points_changed = on_loop_points_changed
        self.on_exit_loop = on_exit_loop
        self.on_fade_exit = on_fade_exit
        self.on_save = on_save
        self.on_add_loop = on_add_loop
        self.on_next_loop = on_next_loop
        self.on_prev_loop = on_prev_loop
        self.on_rename_loop = on_rename_loop
        self.on_delete_loop = on_delete_loop
        
        self._loop_in = 0.0
        self._loop_out = 0.0
        self._current_loops = []
        self._current_selected = -1
        
        self._create_widgets()
        logger.debug("LoopControls initialized")

    def _create_widgets(self):
        """Create all loop control widgets."""
        
        # =========================================================
        # 1. VAMP MANAGEMENT ROW (Name + Navigation)
        # =========================================================
        mgmt_row = ctk.CTkFrame(self, fg_color="transparent")
        mgmt_row.pack(fill="x", pady=(0, PADDING_MEDIUM))
        
        # Label
        ctk.CTkLabel(
            mgmt_row, 
            text="🎵 Vamp:", 
            font=("Segoe UI", 11, "bold"),
            text_color=COLOR_TEXT_DIM
        ).pack(side="left")
        
        # Prev Button
        self.btn_prev_loop = ctk.CTkButton(
            mgmt_row, text="◀", width=30, height=24, 
            fg_color=COLOR_BG_LIGHT, command=self._prev_loop
        )
        self.btn_prev_loop.pack(side="left", padx=(10, 3))
        
        # Vamp Name Display (clickable to rename)
        self.lbl_vamp_name = ctk.CTkLabel(
            mgmt_row, 
            text="(no vamp)", 
            font=("Segoe UI", 12, "bold"),
            text_color=COLOR_TEXT,
            width=150,
            cursor="hand2"
        )
        self.lbl_vamp_name.pack(side="left", padx=5)
        self.lbl_vamp_name.bind("<Button-1>", lambda e: self._rename_current_loop())

        # Next Button
        self.btn_next_loop = ctk.CTkButton(
            mgmt_row, text="▶", width=30, height=24, 
            fg_color=COLOR_BG_LIGHT, command=self._next_loop
        )
        self.btn_next_loop.pack(side="left", padx=3)
        
        # Index Display (e.g. "1 / 3")
        self.lbl_loop_index = ctk.CTkLabel(
            mgmt_row, 
            text="", 
            font=("Consolas", 10),
            text_color=COLOR_TEXT_DIM,
            width=45
        )
        self.lbl_loop_index.pack(side="left", padx=5)
        
        # Add New Button
        self.btn_add_loop = ctk.CTkButton(
            mgmt_row, text="+ NEW", width=65, height=24, 
            font=("Segoe UI", 10, "bold"),
            fg_color="#555555", hover_color="#666666",
            command=self._add_loop
        )
        self.btn_add_loop.pack(side="left", padx=(10, 3))
        
        # Delete Button
        self.btn_delete_loop = ctk.CTkButton(
            mgmt_row, text="🗑", width=30, height=24, 
            fg_color=COLOR_BTN_DANGER, hover_color="#ff4444",
            command=self._delete_loop
        )
        self.btn_delete_loop.pack(side="left", padx=3)

        # Add next to the "Vamp Name" or "Delete" button
        self.btn_settings = ctk.CTkButton(
            mgmt_row, text="⚙️", width=30, height=24,
            fg_color=COLOR_BG_LIGHT, text_color=COLOR_TEXT,
            command=lambda: self.on_settings() if self.on_settings else None
        )
        self.btn_settings.pack(side="left", padx=3)

        # =========================================================
        # 2. IN/OUT CONTROL ROW
        # =========================================================
        top_row = ctk.CTkFrame(self, fg_color="transparent")
        top_row.pack(fill="x", pady=(0, PADDING_MEDIUM))
        
        # --- Loop IN section ---
        in_frame = ctk.CTkFrame(top_row, fg_color="transparent")
        in_frame.pack(side="left", padx=(0, PADDING_MEDIUM))
        
        self.btn_set_in = ctk.CTkButton(
            in_frame, text="⬇ SET IN", width=90, height=BTN_HEIGHT,
            font=("Segoe UI", BTN_FONT_SIZE),
            fg_color=COLOR_LOOP_IN,
            text_color="#000000",
            command=self._on_set_in_click
        )
        self.btn_set_in.pack(side="left", padx=(0, PADDING_SMALL))
        
        in_adj_frame = ctk.CTkFrame(in_frame, fg_color="transparent")
        in_adj_frame.pack(side="left")
        
        ctk.CTkButton(
            in_adj_frame, text="−", width=30, height=28,
            command=lambda: self._on_adjust_in(-0.01)
        ).pack(side="left", padx=1)
        
        ctk.CTkButton(
            in_adj_frame, text="+", width=30, height=28,
            command=lambda: self._on_adjust_in(0.01)
        ).pack(side="left", padx=1)
        
        # --- Loop OUT section ---
        out_frame = ctk.CTkFrame(top_row, fg_color="transparent")
        out_frame.pack(side="left", padx=(0, PADDING_MEDIUM))
        
        self.btn_set_out = ctk.CTkButton(
            out_frame, text="⬇ SET OUT", width=90, height=BTN_HEIGHT,
            font=("Segoe UI", BTN_FONT_SIZE),
            fg_color=COLOR_LOOP_OUT,
            text_color="#000000",
            command=self._on_set_out_click
        )
        self.btn_set_out.pack(side="left", padx=(0, PADDING_SMALL))
        
        out_adj_frame = ctk.CTkFrame(out_frame, fg_color="transparent")
        out_adj_frame.pack(side="left")
        
        ctk.CTkButton(
            out_adj_frame, text="−", width=30, height=28,
            command=lambda: self._on_adjust_out(-0.01)
        ).pack(side="left", padx=1)
        
        ctk.CTkButton(
            out_adj_frame, text="+", width=30, height=28,
            command=lambda: self._on_adjust_out(0.01)
        ).pack(side="left", padx=1)
        
        # --- EXIT section (two buttons: hard exit + fade exit) ---
        exit_frame = ctk.CTkFrame(top_row, fg_color="transparent")
        exit_frame.pack(side="left", padx=(PADDING_MEDIUM, 0))
        
        self.btn_exit = ctk.CTkButton(
            exit_frame, text="⮑ EXIT", width=80, height=BTN_HEIGHT,
            font=("Segoe UI", BTN_FONT_SIZE, "bold"),
            fg_color=COLOR_BTN_DISABLED, state="disabled",
            command=self._on_exit_click
        )
        self.btn_exit.pack(side="left", padx=(0, 3))
        
        self.btn_fade_exit = ctk.CTkButton(
            exit_frame, text="🔉 FADE", width=75, height=BTN_HEIGHT,
            font=("Segoe UI", BTN_FONT_SIZE - 1),
            fg_color=COLOR_BTN_DISABLED, state="disabled",
            command=self._on_fade_exit_click
        )
        self.btn_fade_exit.pack(side="left")

        # =========================================================
        # 3. BOTTOM ROW: Entry fields and save
        # =========================================================
        bottom_row = ctk.CTkFrame(self, fg_color="transparent")
        bottom_row.pack(fill="x")
        
        # Loop IN entry
        in_entry_frame = ctk.CTkFrame(bottom_row, fg_color="transparent")
        in_entry_frame.pack(side="left", padx=(0, PADDING_MEDIUM))
        
        ctk.CTkLabel(
            in_entry_frame, text="IN:", 
            font=("Segoe UI", 11), text_color=COLOR_LOOP_IN
        ).pack(side="left", padx=(0, PADDING_SMALL))
        
        self.entry_in = ctk.CTkEntry(
            in_entry_frame, width=80, height=28, font=("Consolas", 11)
        )
        self.entry_in.pack(side="left")
        self.entry_in.bind("<Return>", self._on_entry_change)
        self.entry_in.bind("<FocusOut>", self._on_entry_change)
        
        # Loop OUT entry
        out_entry_frame = ctk.CTkFrame(bottom_row, fg_color="transparent")
        out_entry_frame.pack(side="left", padx=(0, PADDING_MEDIUM))
        
        ctk.CTkLabel(
            out_entry_frame, text="OUT:",
            font=("Segoe UI", 11), text_color=COLOR_LOOP_OUT
        ).pack(side="left", padx=(0, PADDING_SMALL))
        
        self.entry_out = ctk.CTkEntry(
            out_entry_frame, width=80, height=28, font=("Consolas", 11)
        )
        self.entry_out.pack(side="left")
        self.entry_out.bind("<Return>", self._on_entry_change)
        self.entry_out.bind("<FocusOut>", self._on_entry_change)
        
        # Duration label
        self.duration_label = ctk.CTkLabel(
            bottom_row, text="Duration: 0.000s",
            font=("Segoe UI", 11), text_color=COLOR_TEXT_DIM
        )
        self.duration_label.pack(side="left", padx=PADDING_MEDIUM)
        
        # Save button
        self.btn_save = ctk.CTkButton(
            bottom_row, text="💾 SAVE", width=80, height=28,
            font=("Segoe UI", BTN_FONT_SIZE - 1),
            fg_color=COLOR_BTN_PRIMARY,
            text_color=COLOR_BTN_TEXT,
            command=self._on_save_click
        )
        self.btn_save.pack(side="left", padx=(PADDING_MEDIUM, 0))

    # =========================================================================
    # VAMP MANAGEMENT HANDLERS
    # =========================================================================
    
    def update_loop_status(self, loops, selected_index):
        """Called from App when loops change."""
        self._current_loops = loops
        self._current_selected = selected_index
        total = len(loops)
        
        if total == 0:
            self.lbl_loop_index.configure(text="")
            self.lbl_vamp_name.configure(text="(no vamp)")
            return
            
        current = selected_index + 1
        self.lbl_loop_index.configure(text=f"{current}/{total}")
        
        # Show vamp name
        loop = loops[selected_index]
        display_name = loop.name[:20] + "…" if len(loop.name) > 20 else loop.name
        self.lbl_vamp_name.configure(text=display_name)
        
        # Update fields
        self.set_loop_points(loop.start, loop.end)

    def _add_loop(self):
        if self.on_add_loop:
            self.on_add_loop()
        
    def _next_loop(self):
        if self.on_next_loop:
            self.on_next_loop()

    def _prev_loop(self):
        if self.on_prev_loop:
            self.on_prev_loop()
    
    def _delete_loop(self):
        if self.on_delete_loop:
            self.on_delete_loop()
    
    def _rename_current_loop(self):
        """Open a dialog to rename the current vamp."""
        if self._current_selected < 0 or not self._current_loops:
            return
        
        current_name = self._current_loops[self._current_selected].name
        
        dialog = ctk.CTkInputDialog(
            text=f"Rename vamp:",
            title="Rename Vamp"
        )
        new_name = dialog.get_input()
        
        if new_name and new_name.strip() and self.on_rename_loop:
            self.on_rename_loop(self._current_selected, new_name.strip())
    
    # =========================================================================
    # EVENT HANDLERS
    # =========================================================================
    
    def _on_set_in_click(self):
        if self.on_set_in:
            self.on_set_in()
    
    def _on_set_out_click(self):
        if self.on_set_out:
            self.on_set_out()
    
    def _on_adjust_in(self, amount: float):
        if self.on_adjust_in:
            self.on_adjust_in(amount)
    
    def _on_adjust_out(self, amount: float):
        if self.on_adjust_out:
            self.on_adjust_out(amount)
    
    def _on_exit_click(self):
        if self.on_exit_loop:
            self.on_exit_loop()
    
    def _on_fade_exit_click(self):
        """Trigger fade-out exit with configured duration."""
        if self.on_fade_exit:
            self.on_fade_exit(FADE_EXIT_DURATION_MS)
    
    def _on_save_click(self):
        if self.on_save:
            self.on_save()
    
    def _on_entry_change(self, event=None):
        """Handle manual entry of loop times."""
        try:
            in_text = self.entry_in.get().strip()
            out_text = self.entry_out.get().strip()
            
            in_val = self._parse_time(in_text)
            out_val = self._parse_time(out_text)
            
            if in_val is not None and out_val is not None:
                if out_val > in_val and self.on_loop_points_changed:
                    self.on_loop_points_changed(in_val, out_val)
        except Exception as e:
            logger.warning(f"Error parsing loop time: {e}")
    
    def _parse_time(self, text: str) -> Optional[float]:
        """Parse time string (M:SS.ms or just seconds) to float."""
        try:
            if ':' in text:
                parts = text.split(':')
                minutes = int(parts[0])
                seconds = float(parts[1])
                return minutes * 60 + seconds
            else:
                return float(text)
        except:
            return None
    
    # =========================================================================
    # PUBLIC METHODS
    # =========================================================================
    
    def set_loop_points(self, loop_in: float, loop_out: float):
        """Update the displayed loop points."""
        self._loop_in = loop_in
        self._loop_out = loop_out
        
        self.entry_in.delete(0, "end")
        self.entry_in.insert(0, f"{loop_in:.3f}")
        
        self.entry_out.delete(0, "end")
        self.entry_out.insert(0, f"{loop_out:.3f}")
        
        duration = loop_out - loop_in
        self.duration_label.configure(text=f"Duration: {duration:.3f}s")
    
    def set_exit_enabled(self, enabled: bool, active: bool = False):
        """Enable/disable exit buttons."""
        if enabled:
            color = COLOR_BTN_SUCCESS if active else COLOR_BTN_SUCCESS
            self.btn_exit.configure(state="normal", fg_color=color, text="⮑ EXIT")
            self.btn_fade_exit.configure(state="normal", fg_color=COLOR_BTN_WARNING)
        else:
            self.btn_exit.configure(state="disabled", fg_color=COLOR_BTN_DISABLED, text="⮑ EXIT")
            self.btn_fade_exit.configure(state="disabled", fg_color=COLOR_BTN_DISABLED)
    
    def set_exit_waiting(self):
        """Set exit button to waiting state."""
        self.btn_exit.configure(text="⌛ Exiting...", fg_color=COLOR_BTN_WARNING, state="disabled")
        self.btn_fade_exit.configure(state="disabled")
    
    def reset(self):
        """Reset to initial state."""
        self.set_loop_points(0, 0)
        self.set_exit_enabled(False)