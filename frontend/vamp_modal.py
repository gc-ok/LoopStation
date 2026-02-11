"""
Unified Vamp Settings Modal - All vamp configuration in one place!

This modal provides:
- Name editing
- Loop range (IN/OUT) with SET buttons and adjustments
- Advanced timing sliders (collapsible)
- Delete and Save actions
"""

import logging
import tkinter as tk
from typing import Callable, Optional

import customtkinter as ctk

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import (
    COLOR_BG_DARK, COLOR_BG_MEDIUM, COLOR_BG_LIGHT,
    COLOR_BTN_PRIMARY, COLOR_BTN_SUCCESS, COLOR_BTN_DANGER,
    COLOR_BTN_DISABLED, COLOR_TEXT, COLOR_TEXT_DIM,
    COLOR_LOOP_IN, COLOR_LOOP_OUT, COLOR_BTN_TEXT,
    PADDING_SMALL, PADDING_MEDIUM, PADDING_LARGE,
    LOOP_CROSSFADE_MS, LOOP_SWITCH_EARLY_MS, FADE_EXIT_DURATION_MS,
)

logger = logging.getLogger("LoopStation.VampModal")


class CollapsibleFrame(ctk.CTkFrame):
    """A simple collapsible frame for the advanced settings section."""
    
    def __init__(self, parent, title, **kwargs):
        super().__init__(parent, fg_color="transparent", **kwargs)
        
        self._is_open = False
        self._title = title
        
        # Header (clickable)
        self.header = ctk.CTkFrame(self, fg_color=COLOR_BG_LIGHT, corner_radius=4, height=30)
        self.header.pack(fill="x", pady=(0, 2))
        self.header.pack_propagate(False)
        
        self.toggle_label = ctk.CTkLabel(
            self.header,
            text=f"▶  {title}",
            font=("Segoe UI", 11, "bold"),
            text_color=COLOR_TEXT,
            cursor="hand2"
        )
        self.toggle_label.pack(side="left", padx=8, pady=2)
        self.toggle_label.bind("<Button-1>", lambda e: self.toggle())
        self.header.bind("<Button-1>", lambda e: self.toggle())
        
        # Content (hidden by default)
        self.content = ctk.CTkFrame(self, fg_color="transparent")
    
    def toggle(self):
        """Toggle the collapsed/expanded state."""
        if self._is_open:
            self.content.pack_forget()
            self._is_open = False
            self.toggle_label.configure(text=f"▶  {self._title}")
        else:
            self.content.pack(fill="x", pady=(0, PADDING_SMALL))
            self._is_open = True
            self.toggle_label.configure(text=f"▼  {self._title}")


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
        self.state = state_manager
        self.on_close = on_close
        
        # Window setup
        self.title(f"Vamp Settings: {loop.name}")
        self.geometry("520x680")
        self.resizable(False, False)
        
        # Make modal
        self.transient(parent)
        self.grab_set()
        
        # Store slider references
        self.sliders = {}
        self.labels = {}
        
        self._create_widgets()
        self._load_values()
        
        # Center on parent
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() // 2) - (520 // 2)
        y = parent.winfo_y() + (parent.winfo_height() // 2) - (680 // 2)
        self.geometry(f"+{x}+{y}")
    
    def _create_widgets(self):
        """Create all modal widgets."""
        # Main container
        main = ctk.CTkFrame(self, fg_color=COLOR_BG_MEDIUM, corner_radius=0)
        main.pack(fill="both", expand=True, padx=0, pady=0)
        
        # Scrollable content area
        scroll_frame = ctk.CTkScrollableFrame(
            main,
            fg_color="transparent",
            scrollbar_button_color=COLOR_BG_LIGHT
        )
        scroll_frame.pack(fill="both", expand=True, padx=PADDING_LARGE, pady=PADDING_LARGE)
        
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
        # SECTION 3: Advanced Settings (Collapsible)
        # =====================================================================
        self.advanced_section = CollapsibleFrame(scroll_frame, "🛠️ Advanced Timing")
        self.advanced_section.pack(fill="x", pady=(0, PADDING_LARGE))
        
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
            -200, 200, "ms", "early_switch_ms",
            "Positive = switch early. Negative = switch late.",
            default=LOOP_SWITCH_EARLY_MS
        )
        
        self._add_slider(
            self.advanced_section.content,
            "Fade Exit Duration",
            100, 10000, "ms", "exit_fade_ms",
            "Duration when fading out of loop.",
            default=FADE_EXIT_DURATION_MS
        )
        
        # =====================================================================
        # SECTION 4: Actions
        # =====================================================================
        actions = ctk.CTkFrame(main, fg_color="transparent")
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
    
    def _on_slider_change(self, value, label, unit, attr_name):
        """Handle slider value change."""
        val = int(value)
        label.configure(text=f"{val} {unit}")
        
        # Update loop object
        setattr(self.loop, attr_name, val)
        
        # If crossfade changed, regenerate audio
        if attr_name == "crossfade_ms":
            self.state.audio.set_loop_points(
                self.loop.start,
                self.loop.end,
                crossfade_ms=val
            )
    
    def _load_values(self):
        """Load current loop values into the UI."""
        # Name
        self.entry_name.delete(0, "end")
        self.entry_name.insert(0, self.loop.name)
        
        # Range
        self._update_range_display()
        
        # Sliders
        self.sliders["entry_fade_ms"].set(self.loop.entry_fade_ms)
        self.labels["entry_fade_ms"].configure(text=f"{int(self.loop.entry_fade_ms)} ms")
        
        self.sliders["crossfade_ms"].set(self.loop.crossfade_ms)
        self.labels["crossfade_ms"].configure(text=f"{int(self.loop.crossfade_ms)} ms")
        
        self.sliders["early_switch_ms"].set(self.loop.early_switch_ms)
        self.labels["early_switch_ms"].configure(text=f"{int(self.loop.early_switch_ms)} ms")
        
        self.sliders["exit_fade_ms"].set(self.loop.exit_fade_ms)
        self.labels["exit_fade_ms"].configure(text=f"{int(self.loop.exit_fade_ms)} ms")
    
    def _update_range_display(self):
        """Update the IN/OUT entry fields and duration label."""
        self.entry_in.delete(0, "end")
        self.entry_in.insert(0, f"{self.loop.start:.3f}")
        
        self.entry_out.delete(0, "end")
        self.entry_out.insert(0, f"{self.loop.end:.3f}")
        
        duration = self.loop.end - self.loop.start
        self.lbl_duration.configure(text=f"Duration: {duration:.3f}s")
    
    def _adjust_in(self, amount):
        """Adjust IN point by amount."""
        new_val = max(0, self.loop.start + amount)
        if new_val < self.loop.end:
            self.loop.start = new_val
            self.state.update_selected_loop(start=new_val)
            self._update_range_display()
    
    def _adjust_out(self, amount):
        """Adjust OUT point by amount."""
        new_val = min(self.state.song_length, self.loop.end + amount)
        if new_val > self.loop.start:
            self.loop.end = new_val
            self.state.update_selected_loop(end=new_val)
            self._update_range_display()
    
    def _set_in_current(self):
        """Set IN to current playback position."""
        pos = self.state.get_position()
        if pos < self.loop.end:
            self.loop.start = pos
            self.state.update_selected_loop(start=pos)
            self._update_range_display()
    
    def _set_out_current(self):
        """Set OUT to current playback position."""
        pos = self.state.get_position()
        if pos > self.loop.start:
            self.loop.end = pos
            self.state.update_selected_loop(end=pos)
            self._update_range_display()
    
    def _on_manual_change(self, event=None):
        """Handle manual entry of IN/OUT times."""
        try:
            in_text = self.entry_in.get().strip()
            out_text = self.entry_out.get().strip()
            
            in_val = float(in_text)
            out_val = float(out_text)
            
            if 0 <= in_val < out_val <= self.state.song_length:
                self.loop.start = in_val
                self.loop.end = out_val
                self.state.set_loop_points(in_val, out_val)
                self._update_range_display()
        except ValueError:
            # Invalid input, revert to current values
            self._update_range_display()
    
    def _on_save_click(self):
        """Save changes and close."""
        # Update name
        new_name = self.entry_name.get().strip()
        if new_name:
            self.loop.name = new_name
        
        # Save to disk
        self.state.save_loop()
        
        # Close modal
        if self.on_close:
            self.on_close()
        self.destroy()
    
    def _on_delete_click(self):
        """Delete this vamp."""
        # Confirm deletion
        from tkinter import messagebox
        if messagebox.askyesno("Delete Vamp", f"Delete '{self.loop.name}'?"):
            self.state.delete_selected_loop()
            if self.on_close:
                self.on_close()
            self.destroy()
    
    def _on_cancel_click(self):
        """Cancel and close without saving."""
        # Reload original values (in case sliders changed things)
        # Note: Slider changes are applied immediately, so we'd need to
        # cache original values if we want true cancel functionality
        if self.on_close:
            self.on_close()
        self.destroy()