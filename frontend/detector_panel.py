"""
Auto Loop Finder Panel for Loop Station.

Provides UI for the loop detection feature:
- Toggle selection mode on waveform
- Trigger loop analysis
- Display and interact with results
"""

import customtkinter as ctk

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import (
    COLOR_BTN_PRIMARY, COLOR_BTN_WARNING, COLOR_BTN_SUCCESS,
    COLOR_BTN_DANGER, COLOR_BG_MEDIUM,
)
from utils.tooltip import ToolTip


class DetectorPanel(ctk.CTkFrame):
    def __init__(self, parent, on_toggle_select, on_find, on_preview, on_use, on_mode_change=None):
        super().__init__(parent, fg_color="transparent")
        
        self.on_toggle_select = on_toggle_select
        self.on_find = on_find
        self.on_preview = on_preview
        self.on_use = on_use
        self.on_mode_change = on_mode_change # New callback
        self.mode = "loop" # "loop" or "cut"
        
        self._create_widgets()

    def _create_widgets(self):
        # Header
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", pady=5)
        
        # Mode Switcher (New)
        self.btn_mode = ctk.CTkSegmentedButton(
            head, values=["Find Loops", "Find Cuts"],
            command=self._on_mode_switch,
            width=140
        )
        self.btn_mode.set("Find Loops")
        self.btn_mode.pack(side="left", padx=(0, 10))
        ToolTip(self.btn_mode, "Switch between finding loop points and finding sections to cut")
        
        # Selection Button
        self.btn_select = ctk.CTkButton(
            head, text="① Select Range", width=100, 
            command=self._toggle_select, fg_color=COLOR_BTN_PRIMARY
        )
        self.btn_select.pack(side="left", padx=5)
        ToolTip(self.btn_select, "Click then drag on the waveform to select a range to analyze")
        
        # Find/Analyze Button
        self.btn_find = ctk.CTkButton(
            head, text="② Analyze", width=100, 
            command=self.on_find, state="disabled", fg_color=COLOR_BTN_WARNING
        )
        self.btn_find.pack(side="left", padx=5)
        ToolTip(self.btn_find, "Analyze the selected range for loop points or cut candidates")
        
        # Status Label
        self.status_lbl = ctk.CTkLabel(head, text="", text_color="gray")
        self.status_lbl.pack(side="left", padx=10)

        # Results List Frame
        self.results_frame = ctk.CTkScrollableFrame(self, height=100, fg_color=COLOR_BG_MEDIUM)
        self.results_frame.pack(fill="x", expand=True)

    def _on_mode_switch(self, value):
        self.mode = "loop" if value == "Find Loops" else "cut"
        if self.on_mode_change:
            self.on_mode_change(self.mode)

    def _toggle_select(self):
        is_active = self.on_toggle_select()
        if is_active:
            self.btn_select.configure(text="Cancel Selection", fg_color=COLOR_BTN_DANGER)
        else:
            self.btn_select.configure(text="① Select Range", fg_color=COLOR_BTN_PRIMARY)

    def enable_find(self, enabled=True):
        self.btn_find.configure(state="normal" if enabled else "disabled")
        if enabled:
            self.status_lbl.configure(text="Range selected.")

    def show_loading(self):
        self.status_lbl.configure(text="Analyzing audio...")
        self.btn_find.configure(state="disabled")

    def reset(self):
        """Fully reset the detector panel to its initial state.
        
        Called after 'Use' to clean up the workflow so the user
        returns to normal mode without any stale UI.
        """
        # Reset button text
        self.btn_select.configure(text="① Select Range", fg_color=COLOR_BTN_PRIMARY)
        self.btn_find.configure(state="disabled")
        self.status_lbl.configure(text="")
        
        # Clear results
        for widget in self.results_frame.winfo_children():
            widget.destroy()

    def show_results(self, candidates):
        self.status_lbl.configure(text=f"Found {len(candidates)} loops")
        self.btn_find.configure(state="normal")
        
        # Clear old
        for widget in self.results_frame.winfo_children():
            widget.destroy()

        if not candidates:
            ctk.CTkLabel(self.results_frame, text="No loops found in selection").pack()
            return

        for c in candidates:
            row = ctk.CTkFrame(self.results_frame, fg_color="transparent")
            row.pack(fill="x", pady=2)
            
            txt = f"{c.confidence}% | {c.duration:.2f}s"
            ctk.CTkLabel(row, text=txt, font=("Consolas", 11)).pack(side="left", padx=5)
            
            btn_use = ctk.CTkButton(
                row, text="Use", width=40, height=20, fg_color=COLOR_BTN_SUCCESS,
                command=lambda x=c: self.on_use(x)
            )
            btn_use.pack(side="right", padx=2)
            ToolTip(btn_use, "Create a vamp/cut from this candidate and return to cue sheet")
            
            btn_preview = ctk.CTkButton(
                row, text="▶", width=30, height=20, fg_color=COLOR_BTN_PRIMARY,
                command=lambda x=c: self.on_preview(x)
            )
            btn_preview.pack(side="right", padx=2)
            ToolTip(btn_preview, "Preview this loop candidate")
