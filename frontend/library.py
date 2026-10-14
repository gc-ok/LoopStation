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

from config import (
    COLOR_BG_MEDIUM, COLOR_BG_LIGHT, COLOR_TEXT, COLOR_TEXT_DIM,
    COLOR_BTN_PRIMARY, SIDEBAR_WIDTH, SUPPORTED_FORMATS,
    PADDING_SMALL, PADDING_MEDIUM, COLOR_BTN_TEXT,
)
from utils.tooltip import ToolTip

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
        self.song_buttons: List[ctk.CTkFrame] = []
        self._song_labels: List[ctk.CTkLabel] = []
        self._tooltips: List[ToolTip] = []

        # While False (a song is loading) clicks are ignored and the
        # browse / theme controls are disabled.
        self._enabled = True
        # Live Mode lock: song switching and theme change are disabled.
        self._locked = False
        self.on_locked_click: Optional[Callable[[str], None]] = None

        # Threading state for the macOS folder picker
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
        ToolTip(self.btn_browse, "Browse for a folder of music files")

        # --- Theme Selector ---
        from themes import THEMES, DEFAULT_THEME
        from utils.preferences import set_theme_preference, get_theme_preference

        def change_theme(new_theme):
            set_theme_preference(new_theme)
            app_instance = self.winfo_toplevel()
            if hasattr(app_instance, 'request_restart'):
                # Shuts the backend down cleanly, then closes the window so
                # main.py's lifecycle loop re-creates the app with the new theme.
                app_instance.request_restart()
            else:
                logger.warning("Top-level window cannot restart; theme applies on next launch")

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
        current_theme = get_theme_preference()
        if current_theme not in THEMES:
            current_theme = DEFAULT_THEME
        self.theme_menu.set(current_theme)
        self.theme_menu.pack(side="right")
        ToolTip(self.theme_menu, "Change color theme (restarts app)")

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

    # =========================================================================
    # ENABLE / DISABLE (while a song is loading)
    # =========================================================================

    def set_enabled(self, enabled: bool):
        """Enable or disable the sidebar. Disabled: song clicks are ignored."""
        self._enabled = bool(enabled)
        self._apply_enabled()

    def set_locked(self, locked: bool):
        """Live Mode lock: like set_enabled(False) but independent of loading."""
        self._locked = bool(locked)
        self._apply_enabled()

    @property
    def accepting_clicks(self) -> bool:
        return self._enabled and not self._locked

    def _apply_enabled(self):
        active = self.accepting_clicks
        state = "normal" if active else "disabled"
        self.btn_browse.configure(state=state)
        self.theme_menu.configure(state=state)
        if self._locked:
            cursor = "X_cursor"
        else:
            cursor = "hand2" if self._enabled else "watch"
        for row in self.song_buttons:
            try:
                row.configure(cursor=cursor)
            except tk.TclError:
                pass

    # =========================================================================
    # FOLDER PICKER
    # =========================================================================

    def _browse_folder(self):
        """Open the folder picker."""
        if platform.system() == "Darwin":
            # The native Tk dialog can hang the main thread on macOS, so the
            # AppleScript chooser runs in a worker thread and is polled.
            self.btn_browse.configure(state="disabled")
            self._picker_result = None
            self._picker_thread = threading.Thread(target=self._worker_browse_mac, daemon=True)
            self._picker_thread.start()
            self.after(100, self._check_picker_thread)
            return

        # Windows / Linux: the Tk dialog is modal and must run on the main
        # thread anyway, so call it directly.
        # parent= is REQUIRED on Windows; without it tkinter creates a
        # transient root window that flashes visibly on screen.
        from tkinter import filedialog
        folder = filedialog.askdirectory(
            title="Select Music Folder",
            initialdir=self.current_folder or os.path.expanduser("~"),
            parent=self.winfo_toplevel()
        )
        if folder:
            self.load_folder(folder)

    def _worker_browse_mac(self):
        """Run the AppleScript folder chooser off the main thread."""
        folder = None
        try:
            # "choose folder" is a standard addition; no System Events needed.
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

        self._picker_result = folder or "CANCELLED"

    def _check_picker_thread(self):
        """Poll for the macOS picker thread result."""
        if self._picker_thread and self._picker_thread.is_alive():
            self.after(100, self._check_picker_thread)
            return

        result = self._picker_result
        self._picker_thread = None
        if self.accepting_clicks:
            self.btn_browse.configure(state="normal")

        if result and result != "CANCELLED":
            self.load_folder(result)

    # =========================================================================
    # SONG LIST
    # =========================================================================

    def load_folder(self, folder_path: str):
        """Load songs from a folder."""
        if not os.path.isdir(folder_path):
            logger.warning(f"Not a valid folder: {folder_path}")
            return

        self.current_folder = folder_path
        self.folder_label.configure(text=os.path.basename(folder_path))

        # Clear existing rows
        for btn in self.song_buttons:
            btn.destroy()
        self.song_buttons.clear()
        self._song_labels.clear()
        self._tooltips.clear()

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
            row._selected = False

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

            # One hover handler per row; it checks the row's _selected flag.
            # add="+" so the tooltip's own <Enter>/<Leave> bindings survive.
            row.bind("<Enter>", lambda e, r=row: self._on_row_hover(r, True), add="+")
            row.bind("<Leave>", lambda e, r=row: self._on_row_hover(r, False), add="+")

            self.song_buttons.append(row)
            self._song_labels.append(lbl)

            # Tooltip shows the full name for long titles
            self._tooltips.append(ToolTip(row, display_name))

        self.count_label.configure(text=f"{len(self.songs)} songs")
        logger.info(f"Loaded {len(self.songs)} songs from {folder_path}")

    def _on_row_hover(self, row, entering: bool):
        """Highlight a row on hover unless it is the selected song."""
        if getattr(row, '_selected', False):
            return
        try:
            row.configure(fg_color=COLOR_BG_LIGHT if entering else "transparent")
        except tk.TclError:
            pass  # row destroyed by a folder reload mid-hover

    def _truncate_name(self, name: str, max_length: int = 35) -> str:
        """Truncate filename for display."""
        name_no_ext = os.path.splitext(name)[0]
        if len(name_no_ext) > max_length:
            return name_no_ext[:max_length-3] + "..."
        return name_no_ext

    def _on_song_click(self, filename: str):
        """Handle song row click."""
        if self._locked and self.on_locked_click:
            self.on_locked_click("load_song")
            return
        if not self.accepting_clicks:
            return
        full_path = os.path.join(self.current_folder, filename)
        if self.on_song_select:
            self.on_song_select(full_path)

    def set_current_song(self, filename: str):
        """Highlight the currently loaded song."""
        self.current_song = filename

        for i, song in enumerate(self.songs):
            if i >= len(self.song_buttons) or i >= len(self._song_labels):
                break
            row = self.song_buttons[i]
            row._selected = (song == filename)
            if row._selected:
                row.configure(fg_color=COLOR_BTN_PRIMARY)
                self._song_labels[i].configure(text_color="#ffffff")
            else:
                row.configure(fg_color="transparent")
                self._song_labels[i].configure(text_color=COLOR_TEXT)
