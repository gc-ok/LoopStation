"""
Transport Controls Widget for Loop Station.

Contains play/pause, stop buttons and time display.
"""

import logging
import tkinter as tk
from typing import Callable, Optional

import customtkinter as ctk

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import (
    COLOR_BTN_PRIMARY, COLOR_BTN_DANGER, COLOR_BTN_DISABLED,
    COLOR_TEXT, BTN_HEIGHT, BTN_FONT_SIZE, COLOR_BTN_TEXT
)

logger = logging.getLogger("LoopStation.Transport")


class TransportControls(ctk.CTkFrame):
    """
    Transport control panel with play/pause, stop, and time display.
    """
    
    def __init__(
        self, 
        parent: tk.Widget,
        on_play: Optional[Callable] = None,
        on_pause: Optional[Callable] = None,
        on_stop: Optional[Callable] = None,
        **kwargs
    ):
        super().__init__(parent, fg_color="transparent", **kwargs)
        
        self.on_play = on_play
        self.on_pause = on_pause
        self.on_stop = on_stop
        
        self._is_playing = False
        
        self._create_widgets()
        logger.debug("TransportControls initialized")
    
    def _create_widgets(self):
        """Create the transport control widgets."""
        # Play/Pause button
        self.btn_play = ctk.CTkButton(
            self,
            text="▶  PLAY",
            width=120,
            height=BTN_HEIGHT,
            font=("Segoe UI", BTN_FONT_SIZE, "bold"),
            fg_color=COLOR_BTN_PRIMARY,
            text_color=COLOR_BTN_TEXT,
            command=self._on_play_click
        )
        self.btn_play.pack(side="left", padx=(0, 10))
        
        # Stop button
        self.btn_stop = ctk.CTkButton(
            self,
            text="⏹  STOP",
            width=100,
            height=BTN_HEIGHT,
            font=("Segoe UI", BTN_FONT_SIZE),
            fg_color=COLOR_BTN_DANGER,
            text_color="#ffffff",
            command=self._on_stop_click
        )
        self.btn_stop.pack(side="left", padx=(0, 20))
        
        # Time display
        self.time_label = ctk.CTkLabel(
            self,
            text="0:00.00",
            font=("Consolas", 24, "bold"),
            text_color=COLOR_TEXT
        )
        self.time_label.pack(side="left", padx=10)
    
    def _on_play_click(self):
        """Handle play button click."""
        if self._is_playing:
            if self.on_pause:
                self.on_pause()
        else:
            if self.on_play:
                self.on_play()
    
    def _on_stop_click(self):
        """Handle stop button click."""
        if self.on_stop:
            self.on_stop()
    
    def set_playing(self, is_playing: bool):
        """Update the play button state."""
        self._is_playing = is_playing
        if is_playing:
            self.btn_play.configure(text="⏸  PAUSE")
        else:
            self.btn_play.configure(text="▶  PLAY")
    
    def set_time(self, seconds: float):
        """Update the time display."""
        minutes = int(seconds // 60)
        secs = seconds % 60
        self.time_label.configure(text=f"{minutes}:{secs:05.2f}")
    
    def reset(self):
        """Reset to initial state."""
        self.set_playing(False)
        self.set_time(0)