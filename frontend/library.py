"""
Library Sidebar Widget for Loop Station.

Displays a list of audio files in a selected folder.
Allows browsing and loading songs.
"""

import os
import logging
import tkinter as tk
from typing import Callable, Optional, List

import customtkinter as ctk

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import (
    COLOR_BG_MEDIUM, COLOR_BG_LIGHT, COLOR_TEXT, COLOR_TEXT_DIM,
    COLOR_BTN_PRIMARY, SIDEBAR_WIDTH, SUPPORTED_FORMATS,
    PADDING_SMALL, PADDING_MEDIUM, COLOR_BTN_TEXT,
)

logger = logging.getLogger("LoopStation.Library")


class LibrarySidebar(ctk.CTkFrame):
    """
    Sidebar showing list of songs in a folder.
    """
    
    def __init__(
        self,
        parent: tk.Widget,
        on_song_select: Optional[Callable[[str], None]] = None,
        **kwargs
    ):
        super().__init__(parent, width=SIDEBAR_WIDTH, fg_color=COLOR_BG_MEDIUM, **kwargs)
        
        self.on_song_select = on_song_select
        
        self.current_folder: str = ""
        self.songs: List[str] = []
        self.current_song: str = ""
        self.song_buttons: List[ctk.CTkButton] = []
        
        self._create_widgets()
        logger.debug("LibrarySidebar initialized")
    
    def _create_widgets(self):
        """Create sidebar widgets."""
        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=PADDING_MEDIUM, pady=PADDING_MEDIUM)
        
        ctk.CTkLabel(
            header,
            text="🎵 Library",
            font=("Segoe UI", 16, "bold"),
            text_color=COLOR_TEXT
        ).pack(side="left")
        
        self.btn_browse = ctk.CTkButton(
            header,
            text="📁",
            width=36,
            height=28,
            font=("Segoe UI", 14),
            fg_color=COLOR_BTN_PRIMARY,
            text_color=COLOR_BTN_TEXT,
            command=self._browse_folder
        )
        self.btn_browse.pack(side="right")

        # --- NEW: Theme Selector ---
        from themes import THEMES
        from utils.preferences import set_theme_preference

        def change_theme(new_theme):
            set_theme_preference(new_theme)
            app_instance = self.winfo_toplevel()
            if hasattr(app_instance, 'restart_required'):
                 app_instance.restart_required = True
                 app_instance.destroy() # Close the app to trigger the reload loop

        theme_frame = ctk.CTkFrame(self, fg_color="transparent")
        theme_frame.pack(fill="x", padx=PADDING_MEDIUM, pady=(PADDING_MEDIUM, 0))

        ctk.CTkLabel(theme_frame, text="🎨 Theme:", font=("Segoe UI", 11), text_color=COLOR_TEXT_DIM).pack(side="left")

        # Get current theme name from config indirectly or defaults
        # For simplicity, we just show the dropdown
        theme_names = sorted(list(THEMES.keys()))
        
        self.theme_menu = ctk.CTkOptionMenu(
            theme_frame,
            values=theme_names,
            width=120,
            height=24,
            font=("Segoe UI", 10),
            command=change_theme,
            fg_color=COLOR_BTN_PRIMARY,
            button_color=COLOR_BTN_PRIMARY,
            text_color=COLOR_BTN_TEXT,
        )
        self.theme_menu.set("Select Theme") # Or set to current if you pass it in
        self.theme_menu.pack(side="right")
        
        # Folder path label
        self.folder_label = ctk.CTkLabel(
            self,
            text="No folder selected",
            font=("Segoe UI", 10),
            text_color=COLOR_TEXT_DIM,
            wraplength=SIDEBAR_WIDTH - 20
        )
        self.folder_label.pack(fill="x", padx=PADDING_MEDIUM, pady=(0, PADDING_SMALL))
        
        # Scrollable song list
        self.song_list_frame = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            scrollbar_button_color=COLOR_BG_LIGHT
        )
        self.song_list_frame.pack(fill="both", expand=True, padx=PADDING_SMALL, pady=PADDING_SMALL)
        
        # Song count label
        self.count_label = ctk.CTkLabel(
            self,
            text="0 songs",
            font=("Segoe UI", 10),
            text_color=COLOR_TEXT_DIM
        )
        self.count_label.pack(pady=PADDING_SMALL)
    
    def _browse_folder(self):
        """Open folder browser dialog."""
        from tkinter import filedialog
        
        folder = filedialog.askdirectory(
            title="Select Music Folder",
            initialdir=self.current_folder or os.path.expanduser("~")
        )
        
        if folder:
            self.load_folder(folder)
    
    def load_folder(self, folder_path: str):
        """Load songs from a folder."""
        if not os.path.isdir(folder_path):
            logger.warning(f"Not a valid folder: {folder_path}")
            return
        
        self.current_folder = folder_path
        self.folder_label.configure(text=os.path.basename(folder_path))
        
        # Clear existing buttons
        for btn in self.song_buttons:
            btn.destroy()
        self.song_buttons.clear()
        
        # Find audio files
        self.songs = []
        try:
            for filename in sorted(os.listdir(folder_path)):
                if filename.lower().endswith(SUPPORTED_FORMATS):
                    self.songs.append(filename)
        except Exception as e:
            logger.error(f"Error reading folder: {e}")
            return
        
        # Create buttons for each song
        for song in self.songs:
            btn = ctk.CTkButton(
                self.song_list_frame,
                text=self._truncate_name(song),
                anchor="w",
                height=32,
                font=("Segoe UI", 11),
                fg_color="transparent",
                hover_color=COLOR_BG_LIGHT,
                text_color=COLOR_TEXT,
                command=lambda s=song: self._on_song_click(s)
            )
            btn.pack(fill="x", pady=1)
            self.song_buttons.append(btn)
        
        self.count_label.configure(text=f"{len(self.songs)} songs")
        logger.info(f"Loaded {len(self.songs)} songs from {folder_path}")
    
    def _truncate_name(self, name: str, max_length: int = 35) -> str:
        """Truncate filename for display."""
        name_no_ext = os.path.splitext(name)[0]
        if len(name_no_ext) > max_length:
            return name_no_ext[:max_length-3] + "..."
        return name_no_ext
    
    def _on_song_click(self, filename: str):
        """Handle song button click."""
        full_path = os.path.join(self.current_folder, filename)
        if self.on_song_select:
            self.on_song_select(full_path)
    
    def set_current_song(self, filename: str):
        """Highlight the currently loaded song."""
        self.current_song = filename
        
        for i, song in enumerate(self.songs):
            if song == filename:
                self.song_buttons[i].configure(
                    fg_color=COLOR_BTN_PRIMARY,
                    text_color="#ffffff"
                )
            else:
                self.song_buttons[i].configure(
                    fg_color="transparent",
                    text_color=COLOR_TEXT
                )