"""
Library Sidebar Widget for Loop Station.

Displays a list of audio files in a selected folder.
Allows browsing and loading songs.
"""

import os
import logging
import tkinter as tk
import platform
import subprocess
import threading
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
        
        # Threading state for folder picker
        self._picker_thread = None
        self._picker_result = None
        
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
        self.theme_menu.set("Select Theme")
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
        """Start the folder picker process."""
        self.btn_browse.configure(state="disabled")
        
        # Reset result
        self._picker_result = None
        
        # Start worker thread
        self._picker_thread = threading.Thread(target=self._worker_browse, daemon=True)
        self._picker_thread.start()
        
        # Start polling for result on Main Thread
        self.after(100, self._check_picker_thread)

    def _worker_browse(self):
        """Run the actual OS dialog in a background thread to prevent Main Thread freeze."""
        folder = None
        
        # 1. macOS Robust Fix (AppleScript)
        if platform.system() == "Darwin":
            try:
                # SIMPLIFIED SCRIPT: Removed "System Events" dependency to reduce permission friction.
                # "choose folder" is a standard addition that usually works standalone.
                script = 'return POSIX path of (choose folder with prompt "Select Music Folder")'
                result = subprocess.run(
                    ['osascript', '-e', script], 
                    capture_output=True, 
                    text=True
                )
                if result.returncode == 0:
                    folder = result.stdout.strip()
            except Exception as e:
                logger.error(f"macOS picker failed: {e}")
        
        # 2. Windows / Linux (Standard Tkinter)
        # Note: Tkinter filedialog must run on main thread usually, but since 
        # we are avoiding it on Mac, we only use this path for Win/Linux.
        # On Windows, filedialog is generally thread-safe enough or requires main thread.
        # For safety across all OSs in this hybrid approach:
        else:
            # We cannot run tkinter widget calls in a thread. 
            # So for non-Mac, we signal a special flag to run it on main thread.
            self._picker_result = "RUN_ON_MAIN"
            return

        # Store result
        if folder:
            self._picker_result = folder
        else:
            self._picker_result = "CANCELLED"

    def _check_picker_thread(self):
        """Poll for the picker thread result."""
        # Case 1: Thread is still running
        if self._picker_thread and self._picker_thread.is_alive():
            self.after(100, self._check_picker_thread)
            return

        # Case 2: Thread finished
        result = self._picker_result
        self.btn_browse.configure(state="normal")
        
        if result == "RUN_ON_MAIN":
            # Fallback for Windows/Linux: Run standard dialog on main thread.
            # parent= is REQUIRED on Windows — without it, tkinter creates a
            # transient root window that flashes visibly on screen.
            from tkinter import filedialog
            folder = filedialog.askdirectory(
                title="Select Music Folder",
                initialdir=self.current_folder or os.path.expanduser("~"),
                parent=self.winfo_toplevel()
            )
            if folder:
                self.load_folder(folder)
                
        elif result and result != "CANCELLED":
            # macOS result
            self.load_folder(result)
    
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
        self._song_labels = []
        
        # Find audio files (skip hidden/metadata files)
        self.songs = []
        try:
            for filename in sorted(os.listdir(folder_path)):
                # Skip hidden files (Unix-style dot files, macOS resource forks)
                # macOS creates "._SongName.mp3" AppleDouble metadata files that
                # match audio extensions but aren't playable audio.
                if filename.startswith('.'):
                    continue
                if filename.lower().endswith(SUPPORTED_FORMATS):
                    self.songs.append(filename)
        except Exception as e:
            logger.error(f"Error reading folder: {e}")
            return
        
        # Create rows for each song
        for song in self.songs:
            display_name = os.path.splitext(song)[0]
            
            row = ctk.CTkFrame(
                self.song_list_frame, fg_color="transparent",
                height=32, corner_radius=4, cursor="hand2"
            )
            row.pack(fill="x", pady=1)
            row.pack_propagate(False)
            
            lbl = ctk.CTkLabel(
                row, text=display_name,
                anchor="w",
                font=("Segoe UI", 11),
                text_color=COLOR_TEXT,
            )
            lbl.pack(side="left", fill="x", expand=True, padx=(10, 5))
            
            # Click handlers on both frame and label
            row.bind("<Button-1>", lambda e, s=song: self._on_song_click(s))
            lbl.bind("<Button-1>", lambda e, s=song: self._on_song_click(s))
            
            # Hover effects
            row.bind("<Enter>", lambda e, r=row: r.configure(fg_color=COLOR_BG_LIGHT))
            row.bind("<Leave>", lambda e, r=row: r.configure(fg_color="transparent"))
            
            self.song_buttons.append(row)
            self._song_labels = getattr(self, '_song_labels', [])
            self._song_labels.append(lbl)
            
            # Tooltip on hover for long names
            self._add_tooltip(row, display_name)
        
        self.count_label.configure(text=f"{len(self.songs)} songs")
        logger.info(f"Loaded {len(self.songs)} songs from {folder_path}")
    
    def _truncate_name(self, name: str, max_length: int = 35) -> str:
        """Truncate filename for display."""
        name_no_ext = os.path.splitext(name)[0]
        if len(name_no_ext) > max_length:
            return name_no_ext[:max_length-3] + "..."
        return name_no_ext
    
    def _add_tooltip(self, widget, text):
        """Add a hover tooltip to a widget. Shows full text after a short delay."""
        tip_window = [None]  # Use list for mutability in closures
        after_id = [None]
        
        def show(event):
            def _create():
                if tip_window[0]:
                    return
                x = widget.winfo_rootx() + widget.winfo_width() + 5
                y = widget.winfo_rooty()
                tw = tk.Toplevel(widget)
                tw.wm_overrideredirect(True)
                tw.wm_geometry(f"+{x}+{y}")
                # Platform-safe: skip attributes that may not work everywhere
                try:
                    tw.attributes("-topmost", True)
                except Exception:
                    pass
                label = tk.Label(
                    tw, text=text, justify="left",
                    background="#333333", foreground="#ffffff",
                    relief="solid", borderwidth=1,
                    font=("Segoe UI", 10),
                    padx=6, pady=3
                )
                label.pack()
                tip_window[0] = tw
            after_id[0] = widget.after(500, _create)
        
        def hide(event):
            if after_id[0]:
                widget.after_cancel(after_id[0])
                after_id[0] = None
            tw = tip_window[0]
            if tw:
                tw.destroy()
                tip_window[0] = None
        
        widget.bind("<Enter>", show, add="+")
        widget.bind("<Leave>", hide, add="+")
    
    def _on_song_click(self, filename: str):
        """Handle song button click."""
        full_path = os.path.join(self.current_folder, filename)
        if self.on_song_select:
            self.on_song_select(full_path)
    
    def set_current_song(self, filename: str):
        """Highlight the currently loaded song."""
        self.current_song = filename
        labels = getattr(self, '_song_labels', [])
        
        for i, song in enumerate(self.songs):
            if i >= len(self.song_buttons) or i >= len(labels):
                break
            if song == filename:
                self.song_buttons[i].configure(fg_color=COLOR_BTN_PRIMARY)
                labels[i].configure(text_color="#ffffff")
                # Disable hover color change for selected item
                self.song_buttons[i].bind("<Enter>", lambda e: None)
                self.song_buttons[i].bind("<Leave>", lambda e: None)
            else:
                self.song_buttons[i].configure(fg_color="transparent")
                labels[i].configure(text_color=COLOR_TEXT)
                # Re-enable hover
                row = self.song_buttons[i]
                row.bind("<Enter>", lambda e, r=row: r.configure(fg_color=COLOR_BG_LIGHT))
                row.bind("<Leave>", lambda e, r=row: r.configure(fg_color="transparent"))
