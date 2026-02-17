"""
Main Application Window for Loop Station.

Layout:
- Header with song title
- Waveform (fixed height)
- Bottom deck (scrollable):
  - Transport (always visible)
  - Cue Sheet (collapsible) — unified timeline of cues + vamps
  - Vamp Controls (collapsible) — SET IN/OUT, EXIT, FADE performance controls
  - Auto Loop Finder (collapsible)
- Status bar
"""

import os
import sys
import time
import logging
import webbrowser
import queue  # <--- FIXED: Added for thread safety
import threading
import tkinter as tk
import customtkinter as ctk

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import (
    WINDOW_TITLE, WINDOW_WIDTH, WINDOW_HEIGHT,
    WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT,
    COLOR_BG_DARK, COLOR_BG_MEDIUM, COLOR_BG_LIGHT,
    COLOR_TEXT, COLOR_TEXT_DIM,
    COLOR_BTN_PRIMARY, COLOR_BTN_SUCCESS, COLOR_BTN_DANGER,
    COLOR_BTN_WARNING, COLOR_BTN_DISABLED, COLOR_BTN_TEXT,
    SIDEBAR_WIDTH, RIGHT_SIDEBAR_WIDTH, PADDING_SMALL, PADDING_MEDIUM, PADDING_LARGE,
    FADE_EXIT_DURATION_MS, COLOR_BTN_SKIP, COLOR_BTN_WARNING, get_asset_path
)
from backend import StateManager, PlaybackState
from .waveform import WaveformWidget
from .transport import TransportControls
from .loop_controls import LoopControls
from .library import LibrarySidebar
from .detector_panel import DetectorPanel
from .cue_sheet import CueSheetPanel
from .vamp_settings import VampSettingsPanel
from .vamp_modal import VampModal
from .notes_sidebar import NotesSidebar

# Tooltip utility
from utils.tooltip import ToolTip

# Web server (Phase 2)
from backend.web_server import CueWebServer, SharedCueState, get_local_ip

logger = logging.getLogger("LoopStation.App")


class CollapsibleSection(ctk.CTkFrame):
    """A section with a clickable header that collapses/expands."""
    def __init__(self, parent, title, initially_open=False, **kwargs):
        super().__init__(parent, fg_color="transparent", **kwargs)
        
        self._is_open = initially_open
        self._title = title
        
        self.header = ctk.CTkFrame(self, fg_color="#222222", corner_radius=4, height=30)
        self.header.pack(fill="x", pady=(0, 2))
        self.header.pack_propagate(False)
        
        self.toggle_label = ctk.CTkLabel(
            self.header,
            text=f"{'▼' if self._is_open else '▶'}  {title}",
            font=("Segoe UI", 11, "bold"),
            text_color=COLOR_TEXT_DIM,
            cursor="hand2"
        )
        self.toggle_label.pack(side="left", padx=8, pady=2)
        self.toggle_label.bind("<Button-1>", lambda e: self.toggle())
        self.header.bind("<Button-1>", lambda e: self.toggle())
        
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        if self._is_open:
            self.content.pack(fill="x", pady=(0, PADDING_SMALL))
    
    def toggle(self):
        if self._is_open:
            self.content.pack_forget()
            self._is_open = False
        else:
            self.content.pack(fill="x", pady=(0, PADDING_SMALL))
            self._is_open = True
        arrow = "▼" if self._is_open else "▶"
        self.toggle_label.configure(text=f"{arrow}  {self._title}")

class LoadingOverlay(ctk.CTkFrame):
    """A simple overlay to block interaction during loading."""
    def __init__(self, parent, **kwargs):
        super().__init__(parent, fg_color="transparent", **kwargs)
        
        # Semi-opaque background look (solid dark color)
        self.bg = ctk.CTkFrame(self, fg_color="#000000", corner_radius=0)
        self.bg.place(relx=0, rely=0, relwidth=1, relheight=1)
        
        # Loading Spinner/Text
        self.msg = ctk.CTkLabel(
            self, 
            text="⌛ Loading Audio...", 
            font=("Segoe UI", 24, "bold"),
            text_color="#ffffff"
        )
        self.msg.place(relx=0.5, rely=0.5, anchor="center")
        
        self.sub_msg = ctk.CTkLabel(
            self,
            text="Analyzing waveform & loops",
            font=("Segoe UI", 14),
            text_color="#aaaaaa"
        )
        self.sub_msg.place(relx=0.5, rely=0.55, anchor="center")

class LoopStationApp(ctk.CTk):
    def __init__(self, ffmpeg_path: str = "ffmpeg"):
        super().__init__()
        
        # --- FIX: THREAD SAFETY QUEUE ---
        self.msg_queue = queue.Queue()

        # --- FIX: WINDOWS TASKBAR ICON ---
        
        # 1. Separate this app from the generic "Python" taskbar group
        try:
            import ctypes
            myappid = 'gceducation.loopstation.pro.v1' # Arbitrary unique string
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
        except Exception:
            pass

        # 2. Load the actual .ico file for the window title bar
        try:
            # We use get_asset_path so it works inside the frozen exe
            icon_path = get_asset_path("logo.ico")
            self.iconbitmap(icon_path)
        except Exception as e:
            print(f"Could not load icon: {e}")


        self.restart_required = False
        self.title(WINDOW_TITLE)
        self.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.minsize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)
        self.resizable(True, True)
        self.configure(fg_color=COLOR_BG_DARK)
        
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        
        self.sidebar_visible = True
        self.sel_start = 0.0
        self.sel_end = 0.0
        self.ffmpeg_path = ffmpeg_path
        
        # Web server (Phase 2)
        self._shared_cue_state = SharedCueState()
        self._web_server = None

        # 1. Start with NO audio engine (Instant load)
        self.app_state = None 
        
        self._create_layout()
        self._create_widgets()
        
        # 2. CRITICAL FIX: Do NOT call _wire_callbacks() here!
        # It requires app_state to exist. We moved it to initialize_audio_system.
        
        self._bind_shortcuts()
        
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        
        # Start queue polling loop (Main Thread) — 16ms ≈ 60fps for responsive clicks
        self._last_ui_update = 0
        self.after(16, self._check_msg_queue)
        
        logger.info("LoopStationApp UI Shell initialized")

    def _check_msg_queue(self):
        """
        Poll the message queue for events from background threads.
        This runs strictly on the main thread to prevent macOS crashes.
        """
        try:
            while True:
                # Non-blocking get
                msg_type, args = self.msg_queue.get_nowait()
                
                # Dispatch to actual UI update methods
                if msg_type == 'position_update':
                    self._update_position(*args)
                elif msg_type == 'state_change':
                    self._update_state(*args)
                elif msg_type == 'loop_mode_enter':
                    self._update_loop_mode(True)
                elif msg_type == 'loop_mode_exit':
                    self._update_loop_mode(False)
                elif msg_type == 'song_loaded':
                    self._update_song_loaded(*args)
                elif msg_type == 'song_ended':
                    self.status_label.configure(text="Song ended")
                elif msg_type == 'loop_points_changed':
                    self._update_loop_points(*args)
                elif msg_type == 'loops_changed':
                    self._handle_loops_changed(*args)
                elif msg_type == 'markers_changed':
                    self._handle_markers_changed(*args)
                elif msg_type == 'detection_started':
                    self.detector.show_loading()
                elif msg_type == 'detection_complete':
                    self.detector.show_results(*args)
                elif msg_type == 'skips_changed':
                    self._update_skips_ui(*args)
                elif msg_type == 'cut_detection_complete':
                    self.detector.show_results(*args)
                elif msg_type == 'loop_skip_queued':
                    self._on_loop_skip_queued(*args)
                elif msg_type == 'loop_skip_cleared':
                    self._on_loop_skip_cleared()
                elif msg_type == 'hide_loading':
                    self.hide_loading()
                    
        except queue.Empty:
            pass
        
        # Schedule next check — 16ms ≈ 60fps
        self.after(16, self._check_msg_queue)

    def initialize_audio_system(self):
        """
        Initializes the audio engine and wires up callbacks.
        Called by main.py AFTER the splash screen is visible.
        """
        logger.info("Initializing Audio Backend...")
        
        # 0. Create the WaveformWidget NOW (deferred from __init__ to avoid
        #    matplotlib segfault on macOS ARM during splash screen rendering)
        if self.waveform is None:
            self.waveform = WaveformWidget(self.wave_container, on_seek=self._on_waveform_seek)
            self.waveform.on_selection_change = self._on_selection_change
            self.waveform.pack(fill="x")
        
        # 1. Create the heavy Audio Engine / State Manager now
        self.app_state = StateManager(ffmpeg_path=self.ffmpeg_path)
        
        # 2. NOW it is safe to wire callbacks because app_state exists
        self._wire_callbacks()
        
        # 3. Re-sync any UI elements if necessary
        self.detector.enable_find(False) 
        
        logger.info("Audio Backend Ready")

    def _create_layout(self):
        self.paned_window = tk.PanedWindow(
            self, orient=tk.HORIZONTAL, sashwidth=5, bg=COLOR_BG_DARK, bd=0
        )
        self.paned_window.pack(fill="both", expand=True)

        self.sidebar_frame = ctk.CTkFrame(
            self.paned_window, width=SIDEBAR_WIDTH,
            fg_color=COLOR_BG_MEDIUM, corner_radius=0
        )
        self.content_frame = ctk.CTkFrame(self.paned_window, fg_color="transparent")
        
        # Right sidebar for cue notes/details
        self.right_sidebar_frame = ctk.CTkFrame(
            self.paned_window, width=RIGHT_SIDEBAR_WIDTH,
            fg_color=COLOR_BG_MEDIUM, corner_radius=0
        )

        self.paned_window.add(self.sidebar_frame, minsize=200, stretch="never")
        self.paned_window.add(self.content_frame, minsize=500, stretch="always")
        self.paned_window.add(self.right_sidebar_frame, minsize=250, stretch="never")
        
        self.right_sidebar_visible = True
    
    def _create_widgets(self):
        """Create all UI widgets with new redesigned layout."""
        
        # =========================================================
        # SIDEBAR
        # =========================================================
        # =========================================================
        # SIDEBAR FOOTER: Website, Support, Feedback
        # =========================================================
        footer_frame = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        footer_frame.pack(side="bottom", fill="x", padx=PADDING_SMALL, pady=PADDING_MEDIUM)
        
        # Row 1: Support + Feedback buttons
        btn_row = ctk.CTkFrame(footer_frame, fg_color="transparent")
        btn_row.pack(fill="x", pady=(0, 6))
        
        btn_support = ctk.CTkButton(
            btn_row, text="Consider Supporting", width=0, height=26,
            font=("Segoe UI", 10, "bold"),
            fg_color="#6b3fa0", hover_color="#8255b8",
            text_color="#ffffff", corner_radius=6,
            command=lambda: webbrowser.open("https://ko-fi.com/gceducationanalytics")
        )
        btn_support.pack(side="left", expand=True, fill="x", padx=(0, 3))
        ToolTip(btn_support, "Opens ko-fi.com in your browser to support the developer")
        
        btn_feedback = ctk.CTkButton(
            btn_row, text="💬 Feedback", width=0, height=26,
            font=("Segoe UI", 10, "bold"),
            fg_color=COLOR_BG_LIGHT, hover_color="#555555",
            text_color=COLOR_TEXT, corner_radius=6,
            command=lambda: webbrowser.open("https://docs.google.com/forms/d/e/1FAIpQLSfR6c3-w6amZM0yICW4g3mId80S-NxKofN3dF7uVvinCxrOGA/viewform?usp=sharing&ouid=105646083626261044389")
        )
        btn_feedback.pack(side="left", expand=True, fill="x", padx=(3, 0))
        ToolTip(btn_feedback, "Opens feedback form in your browser — share bugs, ideas, or requests")
        
        # Row 2: Brand link
        self.lbl_footer = ctk.CTkLabel(
            footer_frame, text="© GC Education Analytics",
            font=("Segoe UI", 10), text_color=COLOR_TEXT_DIM, cursor="hand2"
        )
        self.lbl_footer.pack()
        self.lbl_footer.bind("<Button-1>", lambda e: webbrowser.open("https://www.gceducationanalytics.com"))
        ToolTip(self.lbl_footer, "Opens gceducationanalytics.com in your browser")
        
        self.library = LibrarySidebar(
            self.sidebar_frame, on_song_select=self._on_song_select
        )
        self.library.pack(fill="both", expand=True)

        # =========================================================
        # CONTENT AREA
        # =========================================================
        
        # Header (song title + sidebar toggle)
        header_frame = ctk.CTkFrame(self.content_frame, fg_color="transparent", height=50)
        header_frame.pack(fill="x", side="top", padx=PADDING_MEDIUM, pady=(PADDING_MEDIUM, 0))
        
        self.btn_toggle = ctk.CTkButton(
            header_frame, text="☰", width=40, height=30,
            fg_color="transparent", border_width=1, 
            border_color=COLOR_TEXT_DIM, text_color=COLOR_TEXT,
            hover_color=COLOR_BG_LIGHT, command=self._toggle_sidebar
        )
        self.btn_toggle.pack(side="left", padx=(0, 15))
        ToolTip(self.btn_toggle, "Toggle song library sidebar")

        self.title_label = ctk.CTkLabel(
            header_frame, text="No song loaded",
            font=("Segoe UI", 20, "bold"), text_color=COLOR_TEXT
        )
        self.title_label.pack(side="left")

        # Waveform container (fixed height) — actual WaveformWidget created later
        # to avoid matplotlib segfault on macOS ARM during splash screen
        self.wave_container = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.wave_container.pack(fill="x", padx=PADDING_MEDIUM, pady=(PADDING_SMALL, 0))
        
        self.waveform = None  # Created in initialize_audio_system()

        # =========================================================
        # NEW: TOP-LEVEL TRANSPORT (Always Visible)
        # =========================================================
        transport_frame = ctk.CTkFrame(
            self.content_frame,
            fg_color=COLOR_BG_MEDIUM,
            corner_radius=8,
            height=70
        )
        transport_frame.pack(fill="x", padx=PADDING_MEDIUM, pady=PADDING_SMALL)
        transport_frame.pack_propagate(False)
        
        # Play button
        self.btn_play = ctk.CTkButton(
            transport_frame, text="▶  PLAY", width=130, height=50,
            font=("Segoe UI", 16, "bold"),
            fg_color=COLOR_BTN_PRIMARY,
            text_color=COLOR_BTN_TEXT,
            command=self._on_play_pause_toggle
        )
        self.btn_play.pack(side="left", padx=(15, 8), pady=10)
        ToolTip(self.btn_play, "Play / Pause  (Space)")
        
        # Stop button
        self.btn_stop = ctk.CTkButton(
            transport_frame, text="⏹  STOP", width=110, height=50,
            font=("Segoe UI", 16, "bold"),
            fg_color=COLOR_BTN_DANGER,
            text_color="#ffffff",
            command=self._on_stop
        )
        self.btn_stop.pack(side="left", padx=8, pady=10)
        ToolTip(self.btn_stop, "Stop playback  (Esc)")
        
        # EXIT LOOP button (BIG, prominent, only enabled in loop mode)
        self.btn_exit_loop = ctk.CTkButton(
            transport_frame, text="⮑  EXIT LOOP", width=150, height=50,
            font=("Segoe UI", 16, "bold"),
            fg_color=COLOR_BTN_DISABLED,
            text_color="#888888",
            state="disabled",
            command=self._on_exit_loop
        )
        self.btn_exit_loop.pack(side="left", padx=8, pady=10)
        ToolTip(self.btn_exit_loop, "Exit the current loop at the next boundary  (E)")
        
        # Fade Exit button (smaller, next to EXIT)
        self.btn_fade_exit = ctk.CTkButton(
            transport_frame, text="🔉 FADE", width=90, height=50,
            font=("Segoe UI", 14),
            fg_color=COLOR_BTN_DISABLED,
            text_color="#888888",
            state="disabled",
            command=lambda: self._on_fade_exit(FADE_EXIT_DURATION_MS)
        )
        self.btn_fade_exit.pack(side="left", padx=8, pady=10)
        ToolTip(self.btn_fade_exit, "Fade out and exit the current loop  (F)")
        
        # Time display (right side)
        self.time_label = ctk.CTkLabel(
            transport_frame,
            text="0:00.00",
            font=("Consolas", 32, "bold"),
            text_color=COLOR_TEXT
        )
        self.time_label.pack(side="left", padx=20, pady=10)
        
        # Loop indicator (shows when in loop mode)
        self.loop_indicator = ctk.CTkLabel(
            transport_frame,
            text="",
            font=("Segoe UI", 11, "bold"),
            text_color="#66bb6a"
        )
        self.loop_indicator.pack(side="right", padx=15, pady=10)

        # Status bar (bottom of content area)
        self.status_label = ctk.CTkLabel(
            self.content_frame, text="Ready",
            font=("Segoe UI", 11), text_color=COLOR_TEXT_DIM
        )
        self.status_label.pack(side="bottom", anchor="w", padx=PADDING_MEDIUM, pady=(0, 5))

        # Bottom deck (scrollable area for sections)
        self.bottom_deck = ctk.CTkScrollableFrame(
            self.content_frame, fg_color=COLOR_BG_MEDIUM, 
            corner_radius=10, scrollbar_button_color=COLOR_BG_LIGHT,
        )
        self.bottom_deck.pack(fill="both", expand=True, padx=PADDING_MEDIUM, pady=PADDING_SMALL)

        # =========================================================
        # CUE SHEET (Expanded by default - main interface)
        # =========================================================
        self.cue_section = CollapsibleSection(
            self.bottom_deck, "📋 CUE SHEET", initially_open=True
        )
        self.cue_section.pack(fill="x", padx=PADDING_MEDIUM)

        self.cue_sheet = CueSheetPanel(
            self.cue_section.content,
            on_add_marker=self._on_add_marker,
            on_jump_to_marker=self._on_jump_to_marker,
            on_rename_marker=self._on_rename_marker,
            on_delete_marker=self._on_delete_marker,
            on_jump_next_marker=lambda: self.app_state.jump_to_next_marker(),
            on_jump_prev_marker=lambda: self.app_state.jump_to_prev_marker(),
            on_add_vamp=self._on_add_vamp_menu,
            on_select_vamp=self._on_select_vamp,
            on_jump_to_vamp=self._on_jump_to_vamp,
            on_open_vamp_settings=self._open_vamp_settings,
            on_rename_vamp=self._on_rename_vamp,
            on_delete_vamp=self._on_delete_vamp,
            on_toggle_skip=self._on_toggle_skip, # NEW
            on_delete_skip=self._on_delete_skip
        )
        self.cue_sheet.pack(fill="x")

        # =========================================================
        # AUTO LOOP FINDER (Collapsed by default)
        # =========================================================
        self.finder_section = CollapsibleSection(
            self.bottom_deck, "🔍 AUTO LOOP FINDER", initially_open=False
        )
        self.finder_section.pack(fill="x", padx=PADDING_MEDIUM, pady=(0, PADDING_MEDIUM))

        self.detector = DetectorPanel(
            self.finder_section.content,
            on_toggle_select=self._toggle_selection_mode,
            on_find=self._start_detection,
            on_preview=self._preview_candidate,
            on_use=self._use_candidate,
            on_mode_change=self._on_detector_mode_change
        )
        self.detector.pack(fill="x")
        
        # Loading overlay (shown during song load)
        self.loader = LoadingOverlay(self.content_frame)
        self.loop_controls = LoopControls(
            self.content_frame, # Note: This object doesn't seem attached to a parent frame in original, assuming usage via VampModal now
            on_set_in=self._on_set_in,
            on_set_out=self._on_set_out
        ) 
        
        # =========================================================
        # RIGHT SIDEBAR: Cue Notes & Details
        # =========================================================
        self.notes_sidebar = NotesSidebar(
            self.right_sidebar_frame,
            on_tag_note_save=self._on_item_tag_note_change,
            on_tag_remove=self._on_item_tag_remove,
        )
        self.notes_sidebar.pack(fill="both", expand=True)
        
        # Toggle button for right sidebar (add to header)
        self.btn_toggle_right = ctk.CTkButton(
            header_frame, text="📝", width=40, height=30,
            fg_color="transparent", border_width=1,
            border_color=COLOR_TEXT_DIM, text_color=COLOR_TEXT,
            hover_color=COLOR_BG_LIGHT, command=self._toggle_right_sidebar
        )
        self.btn_toggle_right.pack(side="right", padx=(15, 0))
        ToolTip(self.btn_toggle_right, "Toggle cue details sidebar  (N)")
        
        # Share button (Phase 2 - local network)
        self.btn_share = ctk.CTkButton(
            header_frame, text="📡 Share", width=80, height=30,
            font=("Segoe UI", 11),
            fg_color="transparent", border_width=1,
            border_color=COLOR_TEXT_DIM, text_color=COLOR_TEXT,
            hover_color=COLOR_BG_LIGHT, command=self._toggle_web_share
        )
        self.btn_share.pack(side="right", padx=(10, 0))
        ToolTip(self.btn_share, "Share cue monitor over local network for backstage devices")
        
        # Keyboard shortcuts reference button
        btn_hotkeys = ctk.CTkButton(
            header_frame, text="⌨", width=34, height=30,
            font=("Segoe UI", 14),
            fg_color="transparent", border_width=1,
            border_color=COLOR_TEXT_DIM, text_color=COLOR_TEXT,
            hover_color=COLOR_BG_LIGHT, command=self._show_hotkeys_modal
        )
        btn_hotkeys.pack(side="right", padx=(10, 0))
        ToolTip(btn_hotkeys, "View keyboard shortcuts")

    # Add these methods to LoopStationApp class

    def _on_play_pause_toggle(self):
        """Toggle between play and pause."""
        if self.app_state is None:
            return
        self.app_state.toggle_play_pause()

    def _on_play(self):
        """Start playback."""
        if self.app_state is None:
            return
        self.app_state.play()

    def _on_pause(self):
        """Pause playback."""
        if self.app_state is None:
            return
        self.app_state.pause()

    def _on_add_vamp_menu(self, choice):
        """Handle vamp creation from dropdown menu."""
        if choice == "Manual Entry":
            self._open_manual_vamp_dialog()
        elif choice == "Auto-Detect":
            self._start_auto_detect_workflow()

    def _open_manual_vamp_dialog(self):
        """Open modal for manual vamp creation."""
        if self.app_state is None:
            return
        
        current_pos = self.app_state.get_position()
        self.app_state.add_loop(
            start=current_pos,
            end=min(current_pos + 5.0, self.app_state.song_length),
            name=f"Vamp {len(self.app_state.loops) + 1}"
        )
        
        new_idx = len(self.app_state.loops) - 1
        self._open_vamp_settings(new_idx)

    def _start_auto_detect_workflow(self):
        """Start the auto-detect workflow."""
        if not self.waveform:
            return
        if not self.finder_section._is_open:
            self.finder_section.toggle()
        
        if self.cue_section._is_open:
            self.cue_section.toggle()
        
        self.waveform.set_selection_mode(True)
        self.detector.btn_select.configure(text="Cancel Selection", fg_color=COLOR_BTN_DANGER)
        self.status_label.configure(text="Drag on waveform to select a range for loop detection")

    def _on_jump_to_vamp(self, loop_idx):
        """Jump to vamp start and begin playing."""
        if self.app_state is None:
            return
        
        self.app_state.select_loop(loop_idx)
        
        if 0 <= loop_idx < len(self.app_state.loops):
            loop = self.app_state.loops[loop_idx]
            self.app_state.play_from(loop.start)

    def _format_time(self, seconds):
        """Format seconds as M:SS.ss"""
        minutes = int(seconds // 60)
        secs = seconds % 60
        return f"{minutes}:{secs:05.2f}"

    def _update_loop_mode(self, in_loop):
        """Update UI when entering/exiting loop mode."""
        if in_loop:
            self.status_label.configure(text="♻ Seamless loop active")
            self.btn_exit_loop.configure(
                state="normal",
                fg_color=COLOR_BTN_SUCCESS,
                text_color="#ffffff",
                text="⮑  EXIT LOOP"
            )
            self.btn_fade_exit.configure(
                state="normal",
                fg_color=COLOR_BTN_WARNING,
                text_color="#000000"
            )
            self.loop_indicator.configure(text="♻ LOOPING")
        else:
            self.status_label.configure(text="Exited loop")
            self.loop_indicator.configure(text="")
            # If still playing (transport resumed after loop exit), show SKIP VAMP
            if self.app_state and self.app_state.is_playing():
                has_active_loops = any(l.active for l in self.app_state.loops)
                if has_active_loops:
                    self.btn_exit_loop.configure(
                        state="normal",
                        fg_color=COLOR_BTN_PRIMARY,
                        text_color="#ffffff",
                        text="⏭  SKIP VAMP"
                    )
                    self.btn_fade_exit.configure(
                        state="normal",
                        fg_color=COLOR_BTN_WARNING,
                        text_color="#000000"
                    )
                    return
            # Otherwise disable
            self.btn_exit_loop.configure(
                state="disabled",
                fg_color=COLOR_BTN_DISABLED,
                text_color="#888888",
                text="⮑  EXIT LOOP"
            )
            self.btn_fade_exit.configure(
                state="disabled",
                fg_color=COLOR_BTN_DISABLED,
                text_color="#888888"
            )



    def show_loading(self):
        """Show the loading overlay on top of content."""
        self.loader.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.loader.lift() # Ensure it's on top
        self.update()      # Force UI update immediately

    def hide_loading(self):
        """Hide the loading overlay."""
        self.loader.place_forget()

    def _toggle_sidebar(self):
        if self.sidebar_visible:
            self.paned_window.forget(self.sidebar_frame)
            self.sidebar_visible = False
        else:
            # Re-add before content_frame (first pane)
            self.paned_window.add(self.sidebar_frame, before=self.content_frame, minsize=200, width=SIDEBAR_WIDTH)
            self.sidebar_visible = True
    
    def _toggle_right_sidebar(self):
        """Toggle the right notes/details sidebar."""
        if self.right_sidebar_visible:
            self.paned_window.forget(self.right_sidebar_frame)
            self.right_sidebar_visible = False
        else:
            self.paned_window.add(self.right_sidebar_frame, minsize=250, width=RIGHT_SIDEBAR_WIDTH)
            self.right_sidebar_visible = True
    
    def _on_item_tag_note_change(self, item_id, tag, note_text):
        """Handle tag note save from the sidebar."""
        if self.app_state:
            self.app_state.set_item_tag_note(item_id, tag, note_text)
    
    def _on_item_tag_remove(self, item_id, tag):
        """Handle tag removal from the sidebar."""
        if self.app_state:
            self.app_state.remove_item_tag(item_id, tag)
    
    # =========================================================================
    # WEB SHARING (Phase 2)
    # =========================================================================
    
    def _show_hotkeys_modal(self):
        """Show a modal with all keyboard shortcuts."""
        popup = ctk.CTkToplevel(self)
        popup.title("Keyboard Shortcuts")
        popup.geometry("360x520")
        popup.resizable(False, False)
        popup.configure(fg_color=COLOR_BG_DARK)
        popup.attributes("-topmost", True)
        popup.after(200, lambda: popup.attributes("-topmost", False))
        
        ctk.CTkLabel(
            popup, text="⌨  Keyboard Shortcuts",
            font=("Segoe UI", 16, "bold"), text_color=COLOR_TEXT
        ).pack(pady=(16, 12))
        
        # Scrollable frame for the shortcuts
        scroll = ctk.CTkScrollableFrame(
            popup, fg_color=COLOR_BG_MEDIUM, corner_radius=8,
            width=320, height=400
        )
        scroll.pack(padx=16, fill="both", expand=True)
        
        shortcuts = [
            ("Playback", [
                ("Space", "Play / Pause"),
                ("Escape", "Stop"),
                ("← / →", "Nudge ±0.1s"),
                ("Ctrl+← / →", "Nudge ±1.0s"),
                ("[ / ]", "Prev / Next marker"),
            ]),
            ("Looping", [
                ("I", "Set loop IN"),
                ("O", "Set loop OUT"),
                ("E", "Exit loop at boundary"),
                ("F", "Fade exit from loop"),
            ]),
            ("Cue Management", [
                ("M", "Add cue marker at playhead"),
                ("N", "Toggle cue details sidebar"),
                ("S", "Save all data"),
            ]),
        ]
        
        for section_title, keys in shortcuts:
            ctk.CTkLabel(
                scroll, text=section_title,
                font=("Segoe UI", 12, "bold"), text_color=COLOR_TEXT,
                anchor="w"
            ).pack(fill="x", padx=10, pady=(12, 4))
            
            for key, action in keys:
                row = ctk.CTkFrame(scroll, fg_color="transparent", height=28)
                row.pack(fill="x", padx=10, pady=1)
                row.pack_propagate(False)
                
                key_badge = ctk.CTkLabel(
                    row, text=key, width=100,
                    font=("Consolas", 11, "bold"), text_color="#58a6ff",
                    anchor="w"
                )
                key_badge.pack(side="left", padx=(0, 8))
                
                ctk.CTkLabel(
                    row, text=action,
                    font=("Segoe UI", 11), text_color=COLOR_TEXT_DIM,
                    anchor="w"
                ).pack(side="left", fill="x", expand=True)
        
        # Note about text fields
        ctk.CTkLabel(
            scroll, text="Shortcuts are paused while typing in text fields.",
            font=("Segoe UI", 10), text_color=COLOR_TEXT_DIM,
            wraplength=290
        ).pack(padx=10, pady=(16, 10))
        
        # Close button
        ctk.CTkButton(
            popup, text="Close", width=80, height=28,
            fg_color=COLOR_BG_LIGHT, text_color=COLOR_TEXT,
            command=popup.destroy
        ).pack(pady=(8, 12))

    def _toggle_web_share(self):
        """Start or stop the local network cue monitor."""
        if self._web_server and self._web_server.running:
            # Stop sharing
            self._web_server.stop()
            self._web_server = None
            self.btn_share.configure(
                text="📡 Share",
                fg_color="transparent",
                text_color=COLOR_TEXT
            )
            self.status_label.configure(text="Sharing stopped")
        else:
            # Start sharing
            try:
                self._web_server = CueWebServer(self._shared_cue_state, port=8080)
                url = self._web_server.start()
                self.btn_share.configure(
                    text="📡 LIVE",
                    fg_color=COLOR_BTN_SUCCESS,
                    text_color="#ffffff"
                )
                self.status_label.configure(text=f"Sharing at {url}")
                self._show_share_popup(url)
            except Exception as e:
                logger.error(f"Failed to start web server: {e}")
                self.status_label.configure(text=f"Share failed: {e}")
    
    def _show_share_popup(self, url):
        """Show a popup with the URL and QR code."""
        popup = ctk.CTkToplevel(self)
        popup.title("Cue Monitor - Share")
        popup.geometry("400x520")
        popup.resizable(False, False)
        popup.configure(fg_color=COLOR_BG_DARK)
        popup.attributes("-topmost", True)
        popup.after(200, lambda: popup.attributes("-topmost", False))
        
        ctk.CTkLabel(
            popup, text="📡  CUE MONITOR LIVE",
            font=("Segoe UI", 18, "bold"), text_color=COLOR_TEXT
        ).pack(pady=(20, 10))
        
        ctk.CTkLabel(
            popup, text="Open this URL on any device on the same network:",
            font=("Segoe UI", 12), text_color=COLOR_TEXT_DIM,
            wraplength=350
        ).pack(pady=(0, 10))
        
        # URL (copyable)
        url_frame = ctk.CTkFrame(popup, fg_color=COLOR_BG_MEDIUM, corner_radius=8)
        url_frame.pack(padx=20, pady=5, fill="x")
        
        url_label = ctk.CTkLabel(
            url_frame, text=url,
            font=("Consolas", 16, "bold"), text_color="#58a6ff",
            cursor="hand2"
        )
        url_label.pack(padx=15, pady=12)
        
        def copy_url():
            self.clipboard_clear()
            self.clipboard_append(url)
            copy_btn.configure(text="Copied!")
            popup.after(2000, lambda: copy_btn.configure(text="Copy URL"))
        
        copy_btn = ctk.CTkButton(
            popup, text="Copy URL", width=120, height=32,
            fg_color=COLOR_BTN_PRIMARY, text_color=COLOR_BTN_TEXT,
            command=copy_url
        )
        copy_btn.pack(pady=8)
        
        # QR Code
        try:
            import qrcode
            from PIL import Image, ImageTk
            
            qr = qrcode.QRCode(version=1, box_size=6, border=2)
            qr.add_data(url)
            qr.make(fit=True)
            qr_img = qr.make_image(fill_color="white", back_color="#0d1117")
            qr_img = qr_img.resize((200, 200), Image.NEAREST)
            
            # Convert to PhotoImage for Tkinter
            photo = ImageTk.PhotoImage(qr_img)
            
            qr_label = tk.Label(popup, image=photo, bg=COLOR_BG_DARK)
            qr_label.image = photo  # Keep reference
            qr_label.pack(pady=10)
            
            ctk.CTkLabel(
                popup, text="Scan with phone camera",
                font=("Segoe UI", 11), text_color=COLOR_TEXT_DIM
            ).pack()
        except ImportError:
            ctk.CTkLabel(
                popup, text="(Install 'qrcode' and 'pillow' for QR code)",
                font=("Segoe UI", 11), text_color=COLOR_TEXT_DIM
            ).pack(pady=20)
        
        # Close button
        ctk.CTkButton(
            popup, text="Close", width=100, height=30,
            fg_color=COLOR_BG_LIGHT, text_color=COLOR_TEXT,
            command=popup.destroy
        ).pack(pady=(15, 20))
    
    def _wire_callbacks(self):
        """
        Wire up StateManager events to push to the thread-safe queue.
        This prevents macOS crash EXC_BREAKPOINT by avoiding Tkinter calls in bg threads.
        """
        # Helper to simplify queue putting
        def q(key): return lambda *args: self.msg_queue.put((key, args))

        self.app_state.on('position_update', q('position_update'))
        self.app_state.on('state_change', q('state_change'))
        self.app_state.on('loop_mode_enter', q('loop_mode_enter'))
        self.app_state.on('loop_mode_exit', q('loop_mode_exit'))
        self.app_state.on('song_loaded', q('song_loaded'))
        self.app_state.on('song_ended', q('song_ended'))
        self.app_state.on('loop_points_changed', q('loop_points_changed'))
        self.app_state.on('loops_changed', q('loops_changed'))
        self.app_state.on('markers_changed', q('markers_changed'))
        self.app_state.on('detection_started', q('detection_started'))
        self.app_state.on('detection_complete', q('detection_complete'))
        self.app_state.on('skips_changed', q('skips_changed'))
        self.app_state.on('cut_detection_complete', q('cut_detection_complete'))
        self.app_state.on('loop_skip_queued', q('loop_skip_queued'))
        self.app_state.on('loop_skip_cleared', q('loop_skip_cleared'))
    
    def _bind_shortcuts(self):
        def _is_typing():
            """Check if focus is in a text input widget."""
            focused = self.focus_get()
            if focused is None:
                return False
            # Check Tk widget class names
            widget_class = focused.winfo_class()
            if widget_class in ('Text', 'Entry', 'TEntry', 'Spinbox'):
                return True
            # Check CTk widget types
            if isinstance(focused, (ctk.CTkTextbox, ctk.CTkEntry)):
                return True
            return False
        
        def _safe(fn):
            """Wrap callback so it's a no-op if app_state isn't ready or user is typing."""
            def wrapper(e):
                if _is_typing():
                    return
                if self.app_state is not None:
                    fn()
            return wrapper
        
        def _safe_key(fn):
            """Like _safe but for letter-key bindings (i, o, e, f, s, m, n)."""
            def wrapper(e):
                if _is_typing():
                    return
                if self.app_state is not None:
                    fn()
            return wrapper
        
        self.bind("<space>", _safe(lambda: self.app_state.toggle_play_pause()))
        self.bind("<Escape>", _safe(lambda: self.app_state.stop()))
        self.bind("i", _safe_key(lambda: self._on_set_in()))
        self.bind("o", _safe_key(lambda: self._on_set_out()))
        self.bind("e", _safe(lambda: self.app_state.queue_exit()))
        self.bind("f", _safe(lambda: self.app_state.queue_exit(fade_mode=True)))
        self.bind("s", _safe(lambda: self.app_state.save_loop()))
        self.bind("<Left>", _safe(lambda: self.app_state.nudge(-0.1)))
        
        self.bind("<Right>", _safe(lambda: self.app_state.nudge(0.1)))
        self.bind("<Control-Left>", _safe(lambda: self.app_state.nudge(-1.0)))
        self.bind("<Control-Right>", _safe(lambda: self.app_state.nudge(1.0)))
        self.bind("m", _safe_key(lambda: self._on_add_marker()))
        self.bind("<bracketright>", _safe(lambda: self.app_state.jump_to_next_marker()))
        self.bind("<bracketleft>", _safe(lambda: self.app_state.jump_to_prev_marker()))
        self.bind("n", lambda e: None if _is_typing() else self._toggle_right_sidebar())
        # Only steal focus when clicking empty background areas, not buttons/widgets
        # This prevents the root binding from interfering with CTkButton clicks

        #OLD CODE
        # self.bind("<Button-1>", self._on_background_click)

    # def _on_background_click(self, event):
        # """Only set focus to root when clicking on non-interactive areas.
        
        # The old binding (self.bind('<Button-1>', focus_set)) fired on
        # EVERY click including CTkButtons. Root bindings fire after widget
        # bindings in Tk's event propagation, which interfered with click
        # handling, especially on macOS where event timing is tighter.
        # """
        # widget = event.widget
        # widget_class = widget.winfo_class()
        
        # Don't steal focus from interactive widgets
        # interactive = {'Button', 'TButton', 'Canvas', 'Entry', 'Text', 
        #               'Checkbutton', 'Radiobutton', 'Scale', 'Spinbox',
        #               'Listbox', 'OptionMenu', 'TCombobox'}
        # if widget_class not in interactive:
        #    self.focus_set()
    
    # =========================================================================
    # UI -> State
    # =========================================================================
    
    def _on_song_select(self, path: str):
        """Handle song selection in a background thread."""
        self.show_loading()
        
        # Run the heavy loading in a separate thread so UI doesn't freeze
        threading.Thread(target=self._load_song_thread, args=(path,), daemon=True).start()

    def _load_song_thread(self, path):
        """
        The worker thread for loading.
        CRITICAL: Use msg_queue for UI updates to ensure thread safety on macOS.
        """
        success = self.app_state.load_song(path)
        
        # If loading failed, we must manually hide the loader via queue.
        # If it succeeded, the 'song_loaded' event will handle hiding it via queue.
        if not success:
            self.msg_queue.put(('hide_loading', ()))
    
    def _on_waveform_seek(self, position_frac: float):
        if self.app_state is None or self.waveform is None:
            return
        position = position_frac * self.app_state.song_length
        self.app_state.seek(position)
        self.waveform.update_playhead(position)
        
        # FIX: Update the label directly, don't use self.transport
        # OLD CODE: self.transport.set_time(position) 
        # NEW CODE:
        self.time_label.configure(text=self._format_time(position))
    
    def _on_stop(self):
        if self.app_state is None:
            return
        self.app_state.stop()
        if self.waveform:
            self.waveform.update_playhead(0)
        # self.transport.set_time(0)
    
    def _on_set_in(self):
        self.app_state.update_selected_loop(start=self.app_state.get_position())
    
    def _on_set_out(self):
        self.app_state.update_selected_loop(end=self.app_state.get_position())
    
    def _on_adjust_in(self, amount):
        self.app_state.adjust_loop_in(amount)
    
    def _on_adjust_out(self, amount):
        self.app_state.adjust_loop_out(amount)
    
    def _on_loop_points_changed(self, loop_in, loop_out):
        self.app_state.set_loop_points(loop_in, loop_out)
    
    def _on_exit_loop(self):
        if self.app_state.is_in_loop_mode():
            self.app_state.queue_exit(fade_mode=False)
            self.loop_controls.set_exit_waiting()
            self.status_label.configure(text="Exiting at loop boundary...")
        else:
            # Transport mode - skip the upcoming vamp
            self.app_state.queue_exit(fade_mode=False)
    
    def _on_fade_exit(self, fade_ms):
        if self.app_state.is_in_loop_mode():
            self.app_state.queue_exit(fade_mode=True, fade_ms=fade_ms)
            self.loop_controls.set_exit_waiting()
            self.status_label.configure(text=f"Fading out ({fade_ms}ms)...")
        else:
            # Transport mode - skip the upcoming vamp
            self.app_state.queue_exit(fade_mode=False)
    
    def _on_loop_skip_queued(self, loop_name):
        """Handle vamp skip queued from transport mode."""
        self.status_label.configure(text=f"⏭ Skipping vamp: {loop_name}")
        self.btn_exit_loop.configure(
            text="⏭  SKIPPING...",
            fg_color=COLOR_BTN_DISABLED,
            text_color="#aaaaaa",
            state="disabled"
        )
        self.btn_fade_exit.configure(
            state="disabled",
            fg_color=COLOR_BTN_DISABLED,
            text_color="#888888"
        )
    
    def _on_loop_skip_cleared(self):
        """Handle skip flag cleared - re-enable skip button if still playing."""
        if self.app_state and self.app_state.is_playing() and not self.app_state.is_in_loop_mode():
            has_active_loops = any(l.active for l in self.app_state.loops)
            if has_active_loops:
                self.btn_exit_loop.configure(
                    state="normal",
                    fg_color=COLOR_BTN_PRIMARY,
                    text_color="#ffffff",
                    text="⏭  SKIP VAMP"
                )
                self.btn_fade_exit.configure(
                    state="normal",
                    fg_color=COLOR_BTN_WARNING,
                    text_color="#000000"
                )
                self.status_label.configure(text="Playing")
    
    def _on_rename_loop(self, index, new_name):
        self.app_state.rename_loop(index, new_name)
    
    def _on_save(self):
        self.app_state.save_loop()
        self.status_label.configure(text="💾 Saved!")
    
    # --- Marker handlers ---
    
    def _on_add_marker(self):
        self.app_state.add_marker()
        self.status_label.configure(text="📍 Cue point added")
    
    def _on_jump_to_marker(self, marker_id):
        self.app_state.jump_to_marker(marker_id)
    
    def _on_rename_marker(self, marker_id, new_name):
        self.app_state.rename_marker(marker_id, new_name)
    
    def _on_delete_marker(self, marker_id):
        self.app_state.delete_marker(marker_id)
    
    # --- Cue sheet vamp handlers ---
    
    def _on_select_vamp(self, loop_idx):
        """Select a vamp from the cue sheet."""
        self.app_state.select_loop(loop_idx)
    
    def _on_rename_vamp(self, loop_idx, new_name):
        """Rename a vamp from the cue sheet."""
        self.app_state.rename_loop(loop_idx, new_name)
    
    def _on_delete_vamp(self, loop_idx):
        """Delete a vamp from the cue sheet."""
        # Select it first, then delete
        self.app_state.select_loop(loop_idx)
        self.app_state.delete_selected_loop()
    
    # --- Combined update for cue sheet ---
    
    def _on_loops_changed(self, loops, selected_index):
        # NOTE: Called via queue mechanism now, essentially wrapping _handle_loops_changed
        self._handle_loops_changed(loops, selected_index)
    
    def _update_state(self, state):
        """Update transport buttons based on playback state."""
        is_playing = state == PlaybackState.PLAYING
        
        if is_playing:
            self.btn_play.configure(text="⏸  PAUSE")
        else:
            self.btn_play.configure(text="▶  PLAY")
        
        # Push play/pause/stop to web server so remote clients update immediately
        if self._web_server and self._web_server.running:
            self._shared_cue_state.update(
                is_playing=is_playing,
                is_paused=(state == PlaybackState.PAUSED),
                is_looping=self.app_state.is_in_loop_mode() if self.app_state else False,
                song_duration=self.app_state.song_length if self.app_state else 0.0,
            )
        
        if state == PlaybackState.STOPPED:
            self.status_label.configure(text="Stopped")
            self.btn_exit_loop.configure(
                state="disabled",
                fg_color=COLOR_BTN_DISABLED,
                text_color="#888888",
                text="⮑  EXIT LOOP"
            )
            self.btn_fade_exit.configure(
                state="disabled",
                fg_color=COLOR_BTN_DISABLED,
                text_color="#888888"
            )
            self.loop_indicator.configure(text="")
        elif state == PlaybackState.PAUSED:
            self.status_label.configure(text="Paused")
        elif state == PlaybackState.PLAYING:
            self.status_label.configure(text="Playing")
            # Enable exit/skip buttons during playback if there are active loops
            # (they can be used to skip upcoming vamps in transport mode)
            if self.app_state and not self.app_state.is_in_loop_mode():
                has_active_loops = any(l.active for l in self.app_state.loops)
                if has_active_loops:
                    self.btn_exit_loop.configure(
                        state="normal",
                        fg_color=COLOR_BTN_PRIMARY,
                        text_color="#ffffff",
                        text="⏭  SKIP VAMP"
                    )
                    self.btn_fade_exit.configure(
                        state="normal",
                        fg_color=COLOR_BTN_WARNING,
                        text_color="#000000"
                    )

    def _update_loop_points(self, loop_in, loop_out):
        """Update waveform display with new loop points."""
        if self.waveform:
            self.waveform.update_loop_markers(loop_in, loop_out)
        # REMOVED: self.loop_controls.set_loop_points(loop_in, loop_out)

    def _handle_loops_changed(self, loops, selected_index):
        """Update UI when loops change."""
        if self.waveform:
            self.waveform.update_loops_display(loops, selected_index)
        self._refresh_cue_sheet()
        # Update notes sidebar timeline
        self.notes_sidebar.update_timeline(
            self.app_state.markers if self.app_state else [],
            loops
        )

    def _on_markers_changed(self, markers):
        self._handle_markers_changed(markers)
    
    def _handle_markers_changed(self, markers):
        if self.waveform:
            self.waveform.update_markers_display(markers)
        self._refresh_cue_sheet()
        # Update notes sidebar timeline
        self.notes_sidebar.update_timeline(
            markers,
            self.app_state.loops if self.app_state else []
        )
    
    # =========================================================================
    # State -> UI
    # =========================================================================
    
    def _on_position_update(self, position, is_loop_mode):
        self._update_position(position, is_loop_mode)
    
    def _update_position(self, position, is_loop_mode):
        """Update position display. Throttled to ~30fps to reduce UI load."""
        now = time.time()
        if now - self._last_ui_update < 0.033:  # 30fps cap
            return
        self._last_ui_update = now
        
        if self.waveform:
            self.waveform.update_playhead(position)
        actual_pos = self.app_state.get_position()
        self.time_label.configure(text=self._format_time(actual_pos))
        
        # Update cue sheet highlighting
        self.cue_sheet.update_position(actual_pos)
        
        # Update notes sidebar countdown and current cue
        self.notes_sidebar.update_position(actual_pos, is_playing=True)
        
        # Push to web server shared state (if sharing)
        if self._web_server and self._web_server.running:
            self._shared_cue_state.update_from_app(
                self.app_state, actual_pos,
                self.notes_sidebar._current_item,
                self.notes_sidebar._current_item_type,
                self.notes_sidebar._next_item,
                self.notes_sidebar._next_item_type,
            )
    
    def _on_state_change(self, state):
        self._update_state(state)
        
    def _on_loop_mode_enter(self):
        self._update_loop_mode(True)
    
    def _on_loop_mode_exit(self, exit_position):
        # We ignore exit_position because _update_loop_mode only needs a bool
        self._update_loop_mode(False)
    
    def _on_song_loaded(self, song_name, duration):
        self._update_song_loaded(song_name, duration)
    
    def _update_song_loaded(self, song_name, duration):
        display_name = os.path.splitext(song_name)[0]
        self.title_label.configure(text=display_name)
        self.library.set_current_song(song_name)
        raw_audio = self.app_state.get_raw_audio_for_waveform()
        if raw_audio is not None and self.waveform:
            self.waveform.load_waveform(raw_audio, duration)
        self._refresh_cue_sheet()
        # Refresh notes sidebar with new song's timeline
        self.notes_sidebar.update_timeline(
            self.app_state.markers,
            self.app_state.loops
        )
        self.status_label.configure(text=f"Loaded: {duration:.1f}s")
        self.hide_loading()
        # Immediately push fresh cue state to web server on song change
        if self._web_server and self._web_server.running:
            self._shared_cue_state.update_from_app(
                self.app_state, 0.0,
                self.notes_sidebar._current_item,
                self.notes_sidebar._current_item_type,
                self.notes_sidebar._next_item,
                self.notes_sidebar._next_item_type,
            )

    def _on_song_ended(self):
        self.status_label.configure(text="Song ended")
    
    def _on_loop_points_updated(self, loop_in, loop_out):
        self._update_loop_points(loop_in, loop_out)
        
    # =========================================================================
    # LIFECYCLE
    # =========================================================================
    
    def _on_close(self):
        """Handle application shutdown."""
        logger.info("Application closing")
        
        # 1. Stop web server if running
        if self._web_server and self._web_server.running:
            self._web_server.stop()
        
        # 2. Stop playback
        if self.app_state:
            self.app_state.stop()
            
            # 3. TRIGGER THE CLEANUP
            self.app_state.cleanup()
        
        # 4. Destroy window
        self.destroy()
        sys.exit(0)
    
    def run(self):
        logger.info("Starting application")
        self.mainloop()
        
    # =========================================================================
    # DETECTOR
    # =========================================================================

    def _toggle_selection_mode(self):
        if not self.waveform:
            return False
        current = self.waveform.selection_mode_active
        new_state = not current
        self.waveform.set_selection_mode(new_state)
        
        # Keep detector panel button in sync
        if new_state:
            self.detector.btn_select.configure(text="Cancel Selection", fg_color=COLOR_BTN_DANGER)
            self.status_label.configure(text="Drag on waveform to select a range for analysis")
        else:
            self.detector.btn_select.configure(text="① Select Range", fg_color=COLOR_BTN_PRIMARY)
            self.status_label.configure(text="Ready")
        
        return new_state

    def _on_selection_change(self, start_frac, end_frac):
        self.sel_start = start_frac * self.app_state.song_length
        self.sel_end = end_frac * self.app_state.song_length
        self.detector.enable_find(True)

    def _on_detector_mode_change(self, mode):
        """Handle switch between Loop and Cut finding."""
        if mode == "cut":
            self.detector.btn_find.configure(text="② Find Cuts", fg_color=COLOR_BTN_SKIP) # Use Theme Color
        else:
            self.detector.btn_find.configure(text="② Find Loops", fg_color=COLOR_BTN_WARNING) # Use Theme Color

    def _start_detection(self):
        """Route detection based on mode."""
        if self.detector.mode == "cut":
            # Call new backend method for smart cuts
            self.app_state.run_smart_cut_detection(self.sel_start, self.sel_end)
        else:
            # Existing loop detection
            self.app_state.run_loop_detection(self.sel_start, self.sel_end)

    def _on_cut_detection_complete(self, candidates):
        """Handle Cut candidates returning from backend."""
        self.detector.show_results(candidates)

    def _use_candidate(self, candidate):
        """Handle 'Use' button click from detector."""
        if self.detector.mode == "cut":
            # Add as a SKIP region
            self.app_state.add_skip(candidate.start, candidate.end)
            self.status_label.configure(text=f"Cut created: {candidate.duration:.2f}s removed")
        else:
            # Add as a LOOP region (Existing logic)
            self.app_state.add_loop(candidate.start, candidate.end)
            self.status_label.configure(text=f"Vamp created: {candidate.duration:.2f}s loop")
            
        # --- Full cleanup of the auto-detect workflow ---
        # 1. Exit selection mode on waveform
        if self.waveform and self.waveform.selection_mode_active:
            self.waveform.set_selection_mode(False)

        # 2. Reset the detector panel (button text + clear results)
        self.detector.reset()

        # 3. Collapse finder section, re-open cue section
        if self.finder_section._is_open:
            self.finder_section.toggle()
        if not self.cue_section._is_open:
            self.cue_section.toggle()

    def _preview_candidate(self, candidate):
        """Previewing a CUT is different - we play pre-roll then jump."""
        if self.detector.mode == "cut":
            # Play from 2 seconds before the cut
            preroll = max(0, candidate.start - 2.0)
            self.app_state.play_from(preroll)
            self.status_label.configure(text="Previewing cut transition...")
        else:
            # Preview Loop (Existing)
            self.app_state.set_loop_points(candidate.start, candidate.end)
            self.app_state.seek(candidate.start)
            if not self.app_state.is_playing():
                self.app_state.play()

    # =========================================================================
    # VAMP SETTINGS
    # =========================================================================

    def _open_vamp_settings(self, loop_idx=None):
        """Open the vamp settings modal."""
        if self.app_state is None:
            return
        
        if loop_idx is None:
            loop_idx = self.app_state.selected_loop_index
        
        if loop_idx < 0 or loop_idx >= len(self.app_state.loops):
            return
        
        self.app_state.select_loop(loop_idx)
        current_loop = self.app_state.loops[loop_idx]
        
        modal = VampModal(
            parent=self,
            loop=current_loop,
            state_manager=self.app_state,
            on_close=lambda: self._refresh_cue_sheet()
        )

    def _on_vamp_setting_change(self, key, value):
        """Callback when a slider moves."""
        if self.app_state is None or self.app_state.selected_loop_index < 0:
            return
            
        loop = self.app_state.loops[self.app_state.selected_loop_index]
        setattr(loop, key, value)
        
        if key == "crossfade_ms":
            self.app_state._sync_audio_engine(loop) 
            
        self.app_state.save_loop()

    # =========================================================================
    # SKIPS
    # =========================================================================

    def _on_skips_changed(self, skips):
        """Backend updated the list of skips."""
        self._update_skips_ui(skips)

    def _update_skips_ui(self, skips):
        if self.waveform:
            self.waveform.update_skips_display(skips)
        self._refresh_cue_sheet()

    def _refresh_cue_sheet(self):
        """Update cue sheet with all 3 types of data."""
        self.cue_sheet.update_data(
            self.app_state.markers,
            self.app_state.loops,
            self.app_state.selected_loop_index,
            getattr(self.app_state, 'skips', []) # Pass skips safely
        )

    def _on_toggle_skip(self, skip_id):
        self.app_state.toggle_skip_active(skip_id)

    def _on_delete_skip(self, skip_id):
        self.app_state.delete_skip(skip_id)



