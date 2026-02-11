"""
Marker Panel Widget for Loop Station.

Manages named cue points for quick navigation during rehearsals.
Note: The section header is provided by CollapsibleSection in app.py.
"""

import logging
import tkinter as tk
from typing import Callable, Optional, List

import customtkinter as ctk

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import (
    COLOR_BTN_PRIMARY, COLOR_BTN_DANGER, COLOR_BTN_SUCCESS,
    COLOR_TEXT, COLOR_TEXT_DIM, COLOR_BG_LIGHT, COLOR_BG_MEDIUM,
    COLOR_MARKER, PADDING_SMALL, PADDING_MEDIUM,
)

logger = logging.getLogger("LoopStation.MarkerPanel")


class MarkerPanel(ctk.CTkFrame):
    """
    Panel for managing named markers / cue points.
    """
    
    def __init__(
        self,
        parent: tk.Widget,
        on_add_marker: Optional[Callable] = None,
        on_jump_to_marker: Optional[Callable[[str], None]] = None,
        on_rename_marker: Optional[Callable[[str, str], None]] = None,
        on_delete_marker: Optional[Callable[[str], None]] = None,
        on_jump_next: Optional[Callable] = None,
        on_jump_prev: Optional[Callable] = None,
        **kwargs
    ):
        super().__init__(parent, fg_color="transparent", **kwargs)
        
        self.on_add_marker = on_add_marker
        self.on_jump_to_marker = on_jump_to_marker
        self.on_rename_marker = on_rename_marker
        self.on_delete_marker = on_delete_marker
        self.on_jump_next = on_jump_next
        self.on_jump_prev = on_jump_prev
        
        self._marker_widgets = []
        
        self._create_widgets()
    
    def _create_widgets(self):
        # Toolbar row (nav + add)
        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.pack(fill="x", pady=(0, PADDING_SMALL))
        
        self.btn_prev = ctk.CTkButton(
            toolbar, text="⏮", width=30, height=24,
            fg_color=COLOR_BG_LIGHT, command=self._on_prev
        )
        self.btn_prev.pack(side="left", padx=(0, 3))
        
        self.btn_next = ctk.CTkButton(
            toolbar, text="⏭", width=30, height=24,
            fg_color=COLOR_BG_LIGHT, command=self._on_next
        )
        self.btn_next.pack(side="left", padx=3)
        
        self.btn_add = ctk.CTkButton(
            toolbar, text="+ ADD CUE", width=85, height=24,
            font=("Segoe UI", 10, "bold"),
            fg_color=COLOR_MARKER, text_color="#000000",
            hover_color="#cccc22",
            command=self._on_add
        )
        self.btn_add.pack(side="right")
        
        ctk.CTkLabel(
            toolbar, text="Press M to add at playhead",
            font=("Segoe UI", 9), text_color=COLOR_TEXT_DIM
        ).pack(side="right", padx=10)
        
        # Marker list (scrollable within section)
        self.marker_list = ctk.CTkScrollableFrame(
            self, height=70, fg_color=COLOR_BG_MEDIUM,
            corner_radius=4,
            scrollbar_button_color=COLOR_BG_LIGHT
        )
        self.marker_list.pack(fill="x")
    
    def update_markers(self, markers):
        """Update the displayed marker list."""
        for widget in self._marker_widgets:
            widget.destroy()
        self._marker_widgets.clear()
        
        if not markers:
            lbl = ctk.CTkLabel(
                self.marker_list, 
                text="No cue points yet.",
                text_color=COLOR_TEXT_DIM, font=("Segoe UI", 10)
            )
            lbl.pack(pady=5)
            self._marker_widgets.append(lbl)
            return
        
        for marker in markers:
            row = ctk.CTkFrame(self.marker_list, fg_color="transparent")
            row.pack(fill="x", pady=1)
            self._marker_widgets.append(row)
            
            # Time badge
            minutes = int(marker.time // 60)
            secs = marker.time % 60
            time_str = f"{minutes}:{secs:05.2f}"
            
            time_lbl = ctk.CTkLabel(
                row, text=time_str,
                font=("Consolas", 10),
                text_color=COLOR_MARKER,
                width=60
            )
            time_lbl.pack(side="left", padx=(5, 5))
            
            # Name (clickable to jump)
            name_btn = ctk.CTkButton(
                row, 
                text=marker.name,
                anchor="w",
                height=22,
                font=("Segoe UI", 11),
                fg_color="transparent",
                hover_color=COLOR_BG_LIGHT,
                text_color=COLOR_TEXT,
                command=lambda mid=marker.id: self._on_jump(mid)
            )
            name_btn.pack(side="left", fill="x", expand=True, padx=2)
            
            # Rename
            rename_btn = ctk.CTkButton(
                row, text="✏", width=25, height=20,
                fg_color="transparent", hover_color=COLOR_BG_LIGHT,
                text_color=COLOR_TEXT_DIM,
                command=lambda mid=marker.id, mname=marker.name: self._on_rename(mid, mname)
            )
            rename_btn.pack(side="right", padx=1)
            
            # Delete
            del_btn = ctk.CTkButton(
                row, text="✕", width=25, height=20,
                fg_color="transparent", hover_color="#442222",
                text_color="#aa4444",
                command=lambda mid=marker.id: self._on_delete(mid)
            )
            del_btn.pack(side="right", padx=1)
    
    def _on_add(self):
        if self.on_add_marker:
            self.on_add_marker()
    
    def _on_jump(self, marker_id):
        if self.on_jump_to_marker:
            self.on_jump_to_marker(marker_id)
    
    def _on_rename(self, marker_id, current_name):
        dialog = ctk.CTkInputDialog(
            text="Rename cue point:",
            title="Rename Cue"
        )
        new_name = dialog.get_input()
        if new_name and new_name.strip() and self.on_rename_marker:
            self.on_rename_marker(marker_id, new_name.strip())
    
    def _on_delete(self, marker_id):
        if self.on_delete_marker:
            self.on_delete_marker(marker_id)
    
    def _on_next(self):
        if self.on_jump_next:
            self.on_jump_next()
    
    def _on_prev(self):
        if self.on_jump_prev:
            self.on_jump_prev()