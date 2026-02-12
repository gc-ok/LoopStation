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
    SIDEBAR_WIDTH, PADDING_SMALL, PADDING_MEDIUM, PADDING_LARGE,
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

        self.paned_window.add(self.sidebar_frame, minsize=200, stretch="never")
        self.paned_window.add(self.content_frame, minsize=500, stretch="always")
    
    def _create_widgets(self):
        """Create all UI widgets with new redesigned layout."""
        
        # =========================================================
        # SIDEBAR
        # =========================================================
        self.lbl_footer = ctk.CTkLabel(
            self.sidebar_frame, text="Made by GC Education Analytics",
            font=("Segoe UI", 11), text_color=COLOR_TEXT_DIM, cursor="hand2"
        )
        self.lbl_footer.pack(side="bottom", pady=PADDING_MEDIUM)
        self.lbl_footer.bind("<Button-1>", lambda e: webbrowser.open("https://www.gceducationanalytics.com"))
        
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

        self.title_label = ctk.CTkLabel(
            header_frame, text="No song loaded",
            font=("Segoe UI", 20, "bold"), text_color=COLOR_TEXT
        )
        self.title_label.pack(side="left")

        # Waveform (fixed height)
        wave_container = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        wave_container.pack(fill="x", padx=PADDING_MEDIUM, pady=(PADDING_SMALL, 0))
        
        self.waveform = WaveformWidget(wave_container, on_seek=self._on_waveform_seek)
        self.waveform.on_selection_change = self._on_selection_change
        self.waveform.pack(fill="x")

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
        
        # Stop button
        self.btn_stop = ctk.CTkButton(
            transport_frame, text="⏹  STOP", width=110, height=50,
            font=("Segoe UI", 16, "bold"),
            fg_color=COLOR_BTN_DANGER,
            text_color="#ffffff",
            command=self._on_stop
        )
        self.btn_stop.pack(side="left", padx=8, pady=10)
        
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
            self.paned_window.add(self.sidebar_frame, before=self.content_frame, minsize=200, width=SIDEBAR_WIDTH)
            self.sidebar_visible = True

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
    
    def _bind_shortcuts(self):
        def _safe(fn):
            """Wrap callback so it's a no-op if app_state isn't ready."""
            def wrapper(e):
                if self.app_state is not None:
                    fn()
            return wrapper
        
        self.bind("<space>", _safe(lambda: self.app_state.toggle_play_pause()))
        self.bind("<Escape>", _safe(lambda: self.app_state.stop()))
        self.bind("i", lambda e: self._on_set_in() if self.app_state else None)
        self.bind("o", lambda e: self._on_set_out() if self.app_state else None)
        self.bind("e", _safe(lambda: self.app_state.queue_exit()))
        self.bind("f", _safe(lambda: self.app_state.queue_exit(fade_mode=True)))
        self.bind("s", _safe(lambda: self.app_state.save_loop()))
        self.bind("<Left>", _safe(lambda: self.app_state.nudge(-0.1)))
        self.bind("<Right>", _safe(lambda: self.app_state.nudge(0.1)))
        self.bind("<Control-Left>", _safe(lambda: self.app_state.nudge(-1.0)))
        self.bind("<Control-Right>", _safe(lambda: self.app_state.nudge(1.0)))
        self.bind("m", lambda e: self._on_add_marker() if self.app_state else None)
        self.bind("<bracketright>", _safe(lambda: self.app_state.jump_to_next_marker()))
        self.bind("<bracketleft>", _safe(lambda: self.app_state.jump_to_prev_marker()))
        self.bind("<Button-1>", lambda e: self.focus_set())
    
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
        position = position_frac * self.app_state.song_length
        self.app_state.seek(position)
        self.waveform.update_playhead(position)
        
        # FIX: Update the label directly, don't use self.transport
        # OLD CODE: self.transport.set_time(position) 
        # NEW CODE:
        self.time_label.configure(text=self._format_time(position))
    
    def _on_stop(self):
        self.app_state.stop()
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
        self.app_state.queue_exit(fade_mode=False)
        self.loop_controls.set_exit_waiting()
        self.status_label.configure(text="Exiting at loop boundary...")
    
    def _on_fade_exit(self, fade_ms):
        self.app_state.queue_exit(fade_mode=True, fade_ms=fade_ms)
        self.loop_controls.set_exit_waiting()
        self.status_label.configure(text=f"Fading out ({fade_ms}ms)...")
    
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
        
        # REMOVED: self.transport.set_playing(is_playing)
        
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

    def _update_loop_points(self, loop_in, loop_out):
        """Update waveform display with new loop points."""
        self.waveform.update_loop_markers(loop_in, loop_out)
        # REMOVED: self.loop_controls.set_loop_points(loop_in, loop_out)

    def _handle_loops_changed(self, loops, selected_index):
        """Update UI when loops change."""
        self.waveform.update_loops_display(loops, selected_index)
        # REMOVED: self.loop_controls.update_loop_status(loops, selected_index)
        self._refresh_cue_sheet()

    def _on_markers_changed(self, markers):
        self._handle_markers_changed(markers)
    
    def _handle_markers_changed(self, markers):
        self.waveform.update_markers_display(markers)
        self._refresh_cue_sheet()
    
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
        
        self.waveform.update_playhead(position)
        actual_pos = self.app_state.get_position()
        self.time_label.configure(text=self._format_time(actual_pos))
        
        # Update cue sheet highlighting
        self.cue_sheet.update_position(actual_pos)
    
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
        if raw_audio is not None:
            self.waveform.load_waveform(raw_audio, duration)
        self._refresh_cue_sheet()
        self.status_label.configure(text=f"Loaded: {duration:.1f}s")
        self.hide_loading()

    def _on_song_ended(self):
        self.status_label.configure(text="Song ended")
    
    def _on_loop_points_updated(self, loop_in, loop_out):
        self._update_loop_points(loop_in, loop_out)
        
    # =========================================================================
    # LIFECYCLE
    # =========================================================================
    
    def _on_close(self):
        logger.info("Application closing")
        self.app_state.stop()
        self.destroy()
    
    def run(self):
        logger.info("Starting application")
        self.mainloop()
        
    # =========================================================================
    # DETECTOR
    # =========================================================================

    def _toggle_selection_mode(self):
        current = self.waveform.selection_mode_active
        new_state = not current
        self.waveform.set_selection_mode(new_state)
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
            self.app_state.set_loop_points(candidate.start, candidate.end)
            
        if self.waveform.selection_mode_active:
            self._toggle_selection_mode()

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
