"""
Main Application Window for Loop Station.

Layout:
- Header with song title, Live Mode toggle, Song / Settings dialogs
- Waveform (fixed height)
- Transport (always visible) with clock and bar:beat readout
- GO panel (big GO button, STANDBY line, HELD banner, rehearsal tools)
- Bottom deck (scrollable):
  - Cue Sheet (collapsible) — unified timeline of cues + vamps
  - Auto Loop Finder (collapsible)
- Status bar

Threading: the StateManager emits events from its monitor / loader threads.
Every event is pushed onto ``msg_queue`` and handled on the Tk thread by
``_check_msg_queue`` — never touch Tk from a callback directly.
"""

import gc
import os
import sys
import math
import time
import logging
import webbrowser
import queue
import threading
from collections import OrderedDict
import tkinter as tk
from tkinter import messagebox
import customtkinter as ctk

from utils.fonts import FONT_UI, FONT_MONO
from config import (
    COLOR_BORDER, COLOR_HOVER, COLOR_TEXT_DISABLED,
    WINDOW_TITLE, WINDOW_WIDTH, WINDOW_HEIGHT,
    WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT,
    COLOR_BG_DARK, COLOR_BG_MEDIUM, COLOR_BG_LIGHT,
    COLOR_TEXT, COLOR_TEXT_DIM,
    COLOR_BTN_PRIMARY, COLOR_BTN_SUCCESS, COLOR_BTN_DANGER,
    COLOR_BTN_WARNING, COLOR_BTN_DISABLED, COLOR_BTN_TEXT,
    SIDEBAR_WIDTH, RIGHT_SIDEBAR_WIDTH, PADDING_SMALL, PADDING_MEDIUM,
    COLOR_BTN_SKIP, WAVEFORM_TARGET_SAMPLES,
)
from utils.paths import get_asset_path
from backend import StateManager, PlaybackState
from backend.web_server import CueWebServer, SharedCueState, WebServerStartError
from utils.formatting import format_time
from utils.tooltip import ToolTip
from utils import preferences

from .widgets import CollapsibleSection, after_if_alive, call_optional, supported_kwargs
from .go_panel import GoPanel
from .settings_dialog import SettingsDialog, load_key_map, keys_display
from .song_settings import SongSettingsDialog
from .waveform import WaveformWidget, compute_envelope
from .show_sidebar import ShowSidebar
from .detector_panel import DetectorPanel
from .cue_sheet import CueSheetPanel
from .vamp_modal import VampModal
from .notes_sidebar import NotesSidebar

logger = logging.getLogger("LoopStation.App")

# Optional: drag-and-drop of files from Finder / Explorer (tkinterdnd2). The
# app works without it; _init_dnd() logs and disables the feature.
try:
    from tkinterdnd2 import TkinterDnD as _TkinterDnD
    _DND_BASES = (_TkinterDnD.DnDWrapper,)
except Exception as _dnd_error:          # not installed / broken install
    _TkinterDnD = None
    _DND_BASES = ()
    _DND_IMPORT_ERROR = _dnd_error
else:
    _DND_IMPORT_ERROR = None

# Waveform envelopes computed from preloaded songs (instant GO), newest last
ENVELOPE_CACHE_SIZE = 2
# A preloaded song loads fast: the overlay only appears if it takes longer
PRELOADED_OVERLAY_DELAY_MS = 350

class LoadingOverlay(ctk.CTkFrame):
    """A simple overlay to block interaction during loading."""
    def __init__(self, parent, **kwargs):
        super().__init__(parent, fg_color="transparent", **kwargs)

        # Semi-opaque background look (solid dark color)
        self.bg = ctk.CTkFrame(self, fg_color=COLOR_BG_DARK, corner_radius=0)
        self.bg.place(relx=0, rely=0, relwidth=1, relheight=1)

        # Loading Spinner/Text
        self.msg = ctk.CTkLabel(
            self,
            text="Loading audio…",
            font=(FONT_UI, 22, "bold"),
            text_color=COLOR_TEXT
        )
        self.msg.place(relx=0.5, rely=0.5, anchor="center")

        self.sub_msg = ctk.CTkLabel(
            self,
            text="Analyzing waveform & loops",
            font=(FONT_UI, 13),
            text_color=COLOR_TEXT_DIM
        )
        self.sub_msg.place(relx=0.5, rely=0.55, anchor="center")


class LoopStationApp(ctk.CTk, *_DND_BASES):
    def __init__(self, ffmpeg_path: str = "ffmpeg"):
        super().__init__()
        self._dnd_available = self._init_dnd()

        # Thread-safe event queue (background threads -> Tk thread)
        self.msg_queue = queue.Queue()
        self._closing = False
        self._pending_cue_refresh = False

        # Song loading guard: True from click until the worker reports back
        self._loading = False
        self._autoplay_path = None      # GO-advanced song: play once loaded
        self._overlay_after = None      # delayed overlay (preloaded song)

        # path (normcase'd) -> (frames, envelope) from preloaded songs
        self._envelope_cache = OrderedDict()
        self._envelope_lock = threading.Lock()
        self._envelope_jobs = set()
        # Main / backup mirroring header state (status dict, flash phase)
        self._mirror_status = None
        self._mirror_flash_after = None
        self._mirror_flash_on = False
        self._shown_show_errors = set()

        # Data-load problems already shown to the user (show each once)
        self._shown_load_errors = set()
        # Audio device errors already shown (each message once)
        self._shown_device_errors = set()
        # True between 'device_lost' and 'device_restored' (GO-panel banner)
        self._device_lost_ui = False

        # Configurable key map (preferences "keys") and what is bound now
        self._key_map = load_key_map()
        self._bound_key_seqs = []

        # Open dialogs (None when closed) and the last acks shown
        self._settings_dialog = None
        self._song_dialog = None
        self._last_acks = None
        self._held = False
        # Single-instance pop-ups and the open vamp modal (None when closed)
        self._hotkeys_popup = None
        self._share_popup = None
        self._vamp_modal = None
        # Exit-button mode last applied (re-applied when the transport resizes)
        self._exit_mode = 'disabled'

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
            logger.debug(f"Could not load icon: {e}")

        self.restart_required = False
        self.title(WINDOW_TITLE)
        self.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.minsize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)
        self.resizable(True, True)
        self.configure(fg_color=COLOR_BG_DARK)

        # Appearance mode (light / dark) follows the theme: config.load_theme()
        ctk.set_default_color_theme("blue")

        self.sidebar_visible = True
        self.sel_start = 0.0
        self.sel_end = 0.0
        self.ffmpeg_path = ffmpeg_path

        # Web server (local network cue monitor)
        self._shared_cue_state = SharedCueState()
        self._web_server = None

        # Start with NO audio engine (instant window); see initialize_audio_system
        self.app_state = None

        self._create_layout()
        self._create_widgets()
        self._bind_shortcuts()

        # Window close button / Alt+F4 (Windows) and Cmd+Q / the app menu's
        # Quit (macOS: Tk calls tk::mac::Quit instead of WM_DELETE_WINDOW)
        # both go through the confirmation, then _on_close.
        self.protocol("WM_DELETE_WINDOW", self._on_close_request)
        if sys.platform == "darwin":
            try:
                self.createcommand('tk::mac::Quit', self._on_close_request)
            except tk.TclError:
                pass

        # Event dispatch table for _check_msg_queue
        self._msg_handlers = {
            'position_update': self._update_position,
            'state_change': self._update_state,
            'loop_mode_enter': lambda: self._update_loop_mode(True),
            'loop_mode_exit': lambda exit_position=None: self._update_loop_mode(False),
            'song_loaded': self._update_song_loaded,
            'song_ended': lambda: self.status_label.configure(text="Song ended"),
            'loop_points_changed': self._update_loop_points,
            'loops_changed': self._handle_loops_changed,
            'markers_changed': self._handle_markers_changed,
            'detection_started': lambda: self.detector.show_loading(),
            'detection_complete': lambda candidates: self.detector.show_results(candidates),
            'cut_detection_complete': lambda candidates: self.detector.show_results(candidates),
            'skips_changed': self._update_skips_ui,
            'loop_skip_queued': self._on_loop_skip_queued,
            'loop_skip_cleared': self._on_loop_skip_cleared,
            'data_load_failed': self._on_data_load_failed,
            'save_failed': self._on_save_failed,
            'save_succeeded': self._on_save_succeeded,
            'preview_changed': self._on_preview_changed,
            # Show control (GO / holds / live mode)
            'standby_changed': self._on_standby_changed,
            'go_fired': self._on_go_fired,
            'go_next_song': self._on_go_next_song,
            'go_next_entry': self._on_go_next_entry,
            # Show (running order)
            'show_changed': self._on_show_changed,
            'current_entry_changed': self._on_current_entry_changed,
            'next_up_changed': self._on_next_up_changed,
            'show_save_failed': self._on_show_save_failed,
            'preload_ready': self._on_preload_ready,
            'preload_failed': self._on_preload_failed,
            'running_order_done': lambda *a: self.library.on_running_order_done(*a),
            'mirror_status': self._on_mirror_status,
            'mirror_loaded': self._on_mirror_loaded,
            'hold_reached': self._on_hold_reached,
            'hold_released': self._on_hold_released,
            'holds_changed': self._handle_holds_changed,
            'loop_cycle': self._on_loop_cycle,
            'live_mode_changed': self._on_live_mode_changed,
            'blocked_in_live_mode': self._on_blocked_in_live_mode,
            # Song settings / engine
            'settings_changed': self._on_settings_changed,
            'stems_changed': self._on_stems_changed,
            'render_state': self._on_render_state,
            'tempo_estimated': self._on_tempo_estimated,
            'device_error': self._on_device_error,
            'device_lost': self._on_device_lost,
            'device_restored': self._on_device_restored,
            'loop_ready': lambda: None,
            # Control surfaces (MIDI thread -> queue -> here)
            'midi_action': self._on_midi_action,
            'midi_learned': self._on_midi_learned,
            'midi_status': self._on_midi_status,
            # Internal messages from the song-load worker thread
            'waveform_ready': self._on_waveform_ready,
            'load_failed': self._on_load_failed,
            'load_finished': self._on_load_finished,
        }

        # Start queue polling loop (Main Thread) — 16ms ≈ 60fps for responsive clicks
        self._last_ui_update = -1e9
        # Throttled position updates: the newest one waiting for its slot
        self._pending_position = None
        self._position_after = None
        self.after(16, self._check_msg_queue)

        logger.info("LoopStationApp UI Shell initialized")

    def _check_msg_queue(self):
        """
        Poll the message queue for events from background threads.
        Runs strictly on the main thread. A failing handler is logged and
        never stops the pump.
        """
        try:
            while True:
                try:
                    msg_type, args = self.msg_queue.get_nowait()
                except queue.Empty:
                    break
                handler = self._msg_handlers.get(msg_type)
                if handler is None:
                    logger.warning(f"Unhandled event: {msg_type}")
                    continue
                try:
                    handler(*args)
                except Exception:
                    logger.exception(f"Error handling '{msg_type}' event")
        finally:
            if not self._closing:
                try:
                    self.after(16, self._check_msg_queue)
                except tk.TclError:
                    pass  # window already destroyed

    def _init_dnd(self) -> bool:
        """Load tkdnd into this interpreter (optional; never fatal)."""
        if _TkinterDnD is None:
            logger.info(f"File drag-and-drop disabled (tkinterdnd2 not available: {_DND_IMPORT_ERROR})")
            return False
        try:
            self.TkdndVersion = _TkinterDnD._require(self)
            return True
        except Exception as e:
            logger.info(f"File drag-and-drop disabled (tkdnd failed to load: {e})")
            return False

    def initialize_audio_system(self):
        """
        Initializes the audio engine and wires up callbacks.
        Called by main.py AFTER the splash screen is visible.
        """
        if self.app_state is not None:
            logger.warning("initialize_audio_system called twice; ignoring")
            return

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

        # 4. A corrupt data file is detected during construction, before any
        #    callback exists: surface it once the window is visible.
        if self.app_state.last_load_error:
            self.msg_queue.put(('data_load_failed', (self.app_state.last_load_error,)))

        # 5. Library folder / last song (and sharing after a theme restart)
        self._restore_session()

        logger.info("Audio Backend Ready")

    # Preference keys for the session restored at startup / after a restart
    PREF_LIBRARY_FOLDER = "library_folder"
    PREF_LAST_SONG = "last_song"
    PREF_RESTART_STATE = "restart_state"

    def _restore_session(self):
        """
        Reopen the last show (file or folder) and the last song; re-share
        after a restart. Old preferences (before shows existed) only had the
        library folder: that folder is opened as a folder show (which then
        becomes the remembered last show).
        """
        try:
            prefs = preferences.load_preferences()
        except Exception:
            prefs = {}
        try:
            self._restore_last_show(prefs)
        except Exception:
            logger.exception("Could not reopen the last show")
        song = prefs.get(self.PREF_LAST_SONG)
        if isinstance(song, str) and song and os.path.isfile(song):
            after_if_alive(self, 150, lambda: self._on_song_select(song))
        restart = prefs.get(self.PREF_RESTART_STATE)
        if isinstance(restart, dict) and restart:
            # One-shot: only the restart that wrote it restores it
            try:
                preferences.save_preferences({self.PREF_RESTART_STATE: None})
            except Exception:
                pass
            if restart.get("sharing"):
                after_if_alive(self, 400, lambda: self._start_web_share(show_popup=False))
        # Main / backup: start the saved role if "Auto-start on launch" is on
        after_if_alive(self, 600, self._mirror_auto_start)

    def _restore_last_show(self, prefs) -> bool:
        st = self.app_state
        last = preferences.get_last_show()
        if last is not None:
            kind, path = last["kind"], last["path"]
            if kind == "file" and os.path.isfile(path):
                ok = st.open_show(path)
            elif kind == "folder" and os.path.isdir(path):
                ok = st.open_folder(path)
            else:
                logger.warning(f"Last show not found: {path}")
                ok = False
        else:
            folder = prefs.get(self.PREF_LIBRARY_FOLDER)     # pre-show preferences
            ok = bool(isinstance(folder, str) and folder and os.path.isdir(folder)
                      and st.open_folder(folder))
        self.library.render(st.show_snapshot())
        return bool(ok)

    def _on_go_next_song(self, path):
        """
        GO at the end of a song (path form). With a show, 'go_next_entry'
        follows right after and loads the entry -- handle only that one.
        """
        st = self.app_state
        if st is not None and st.show.entries:
            return
        self._on_song_select(path, from_go=True)

    def _on_go_next_entry(self, entry_id):
        """GO at the end of a song: load the next show entry and start it."""
        st = self.app_state
        if st is None:
            return
        entry = st.show.get(entry_id)
        if entry is None or entry.kind != "song":
            return
        self._on_song_select(entry.path, from_go=True, entry_id=entry_id)

    def load_show_entry(self, entry_id):
        """Sidebar click (rehearsal): load a show entry through the loading flow."""
        st = self.app_state
        if st is None:
            return
        entry = st.show.get(entry_id)
        if entry is None or entry.kind != "song":
            return
        self._on_song_select(entry.path, entry_id=entry_id)

    # =========================================================================
    # SHOW EVENTS
    # =========================================================================

    def _on_show_changed(self, snapshot):
        self.library.render(snapshot)

    def _on_current_entry_changed(self, entry_id):
        self.library.set_current_entry(entry_id)

    def _on_next_up_changed(self, entry_id):
        self.library.set_next_up_entry(entry_id)

    def _on_show_save_failed(self, message):
        self.status_label.configure(text=f"⚠ {message}", text_color="#ff7b72")
        if message in self._shown_show_errors:
            return
        self._shown_show_errors.add(message)
        try:
            messagebox.showerror("Loop Station - Show not saved", message, parent=self)
        except tk.TclError:
            pass

    @staticmethod
    def _envelope_key(path):
        return os.path.normcase(os.path.abspath(path)) if path else ""

    def _on_preload_ready(self, path):
        """Next-up song pre-decoded: compute its waveform envelope now (worker)."""
        st = self.app_state
        key = self._envelope_key(path)
        if st is None or not key:
            return
        with self._envelope_lock:
            if key in self._envelope_cache or key in self._envelope_jobs:
                return
            self._envelope_jobs.add(key)
        threading.Thread(target=self._envelope_worker, args=(st, path, key),
                         name="preload-envelope", daemon=True).start()

    def _envelope_worker(self, st, path, key):
        try:
            audio = st.get_preloaded_audio(path)
            if audio is None:
                return
            frames = int(audio.shape[0])
            envelope = compute_envelope(audio, WAVEFORM_TARGET_SAMPLES)
            del audio   # drop the memmap (Windows cannot delete a mapped file)
            with self._envelope_lock:
                self._envelope_cache[key] = (frames, envelope)
                self._envelope_cache.move_to_end(key)
                while len(self._envelope_cache) > ENVELOPE_CACHE_SIZE:
                    self._envelope_cache.popitem(last=False)
        except Exception:
            logger.exception("Envelope of the preloaded song failed")
        finally:
            with self._envelope_lock:
                self._envelope_jobs.discard(key)

    def _has_cached_envelope(self, path) -> bool:
        with self._envelope_lock:
            return self._envelope_key(path) in self._envelope_cache

    def _cached_envelope(self, path, raw_audio):
        """The preloaded envelope for ``path`` if it matches the loaded audio's length."""
        if raw_audio is None:
            return None
        with self._envelope_lock:
            hit = self._envelope_cache.get(self._envelope_key(path))
        if hit is None or hit[0] != int(raw_audio.shape[0]):
            return None
        return hit[1]

    def _on_preload_failed(self, path, message=""):
        logger.info(f"Preload of {os.path.basename(path or '')} failed ({message}); "
                    "it will load normally")

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
        """Create all UI widgets."""

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
            font=(FONT_UI, 10, "bold"),
            fg_color="#6b3fa0", hover_color="#8255b8",
            text_color="#ffffff", corner_radius=6,
            command=lambda: webbrowser.open("https://ko-fi.com/gceducationanalytics")
        )
        btn_support.pack(side="left", expand=True, fill="x", padx=(0, 3))
        ToolTip(btn_support, "Opens ko-fi.com in your browser to support the developer")

        btn_feedback = ctk.CTkButton(
            btn_row, text="Feedback", width=0, height=26,
            font=(FONT_UI, 10, "bold"),
            fg_color=COLOR_BG_LIGHT, hover_color=COLOR_HOVER,
            text_color=COLOR_TEXT, corner_radius=6,
            command=lambda: webbrowser.open("https://docs.google.com/forms/d/e/1FAIpQLSfR6c3-w6amZM0yICW4g3mId80S-NxKofN3dF7uVvinCxrOGA/viewform?usp=sharing&ouid=105646083626261044389")
        )
        btn_feedback.pack(side="left", expand=True, fill="x", padx=(3, 0))
        ToolTip(btn_feedback, "Opens feedback form in your browser — share bugs, ideas, or requests")

        # Row 2: Brand link
        self.lbl_footer = ctk.CTkLabel(
            footer_frame, text="© GC Education Analytics",
            font=(FONT_UI, 10), text_color=COLOR_TEXT_DIM, cursor="hand2"
        )
        self.lbl_footer.pack()
        self.lbl_footer.bind("<Button-1>", lambda e: webbrowser.open("https://www.gceducationanalytics.com"))
        ToolTip(self.lbl_footer, "Opens gceducationanalytics.com in your browser")

        self.library = ShowSidebar(self.sidebar_frame, app=self)
        self.library.on_locked_click = self._on_blocked_in_live_mode
        self.library.pack(fill="both", expand=True)
        if self._dnd_available:
            self.library.enable_dnd()

        # =========================================================
        # CONTENT AREA
        # =========================================================

        # Header (song title + sidebar toggle)
        header_frame = ctk.CTkFrame(self.content_frame, fg_color="transparent", height=50)
        header_frame.pack(fill="x", side="top", padx=PADDING_MEDIUM, pady=(PADDING_MEDIUM, 0))

        self.btn_toggle = ctk.CTkButton(
            header_frame, text="◧", width=40, height=30,
            fg_color=COLOR_BG_LIGHT, border_width=1,
            border_color=COLOR_BORDER, text_color=COLOR_TEXT,
            hover_color=COLOR_HOVER, command=self._toggle_sidebar
        )
        self.btn_toggle.pack(side="left", padx=(0, 15))
        ToolTip(self.btn_toggle, "Toggle the show (running order) sidebar")

        self.title_label = ctk.CTkLabel(
            header_frame, text="No song loaded",
            font=(FONT_UI, 20, "bold"), text_color=COLOR_TEXT
        )
        self.title_label.pack(side="left")

        # Waveform container (fixed height) — actual WaveformWidget created later
        # to avoid matplotlib segfault on macOS ARM during splash screen
        self.wave_container = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.wave_container.pack(fill="x", padx=PADDING_MEDIUM, pady=(PADDING_SMALL, 0))

        self.waveform = None  # Created in initialize_audio_system()

        # =========================================================
        # TOP-LEVEL TRANSPORT (Always Visible)
        # =========================================================
        transport_frame = ctk.CTkFrame(
            self.content_frame,
            fg_color=COLOR_BG_MEDIUM,
            corner_radius=8,
            height=70
        )
        transport_frame.pack(fill="x", padx=PADDING_MEDIUM, pady=PADDING_SMALL)
        transport_frame.pack_propagate(False)

        # Time display and loop indicator are packed FIRST (from the right)
        # so Tk gives them their space before the buttons; the clock is the
        # one thing an operator must always be able to read, even when the
        # window is narrow.
        self.time_label = ctk.CTkLabel(
            transport_frame,
            text="0:00.00",
            font=(FONT_MONO, 32, "bold"),
            text_color=COLOR_TEXT
        )
        self.time_label.pack(side="right", padx=(10, 20), pady=10)

        # Bar:beat readout (only while the song has a BPM); packed with
        # before=loop_indicator so it sits between the clock and the indicator.
        self.bar_label = ctk.CTkLabel(
            transport_frame, text="", font=(FONT_MONO, 18, "bold"),
            text_color="#58a6ff"
        )
        self._bar_label_visible = False

        self.loop_indicator = ctk.CTkLabel(
            transport_frame,
            text="",
            font=(FONT_UI, 11, "bold"),
            text_color="#66bb6a"
        )
        self.loop_indicator.pack(side="right", padx=5, pady=10)
        ToolTip(self.bar_label, "Bar · beat from the song's tempo map (Song settings)")

        # Play button
        self.btn_play = ctk.CTkButton(
            transport_frame, text="▶  PLAY", width=130, height=50,
            font=(FONT_UI, 16, "bold"),
            fg_color=COLOR_BTN_PRIMARY,
            text_color=COLOR_BTN_TEXT,
            command=self._on_play_pause_toggle
        )
        self.btn_play.pack(side="left", padx=(15, 8), pady=10)
        self._tip_play = ToolTip(self.btn_play, "Play / Pause  (Space)")

        # Stop button
        self.btn_stop = ctk.CTkButton(
            transport_frame, text="⏹  STOP", width=110, height=50,
            font=(FONT_UI, 16, "bold"),
            fg_color=COLOR_BTN_DANGER,
            text_color="#ffffff",
            command=self._on_stop
        )
        self.btn_stop.pack(side="left", padx=8, pady=10)
        self._tip_stop = ToolTip(self.btn_stop, "Stop playback  (Esc)")

        # EXIT LOOP button (BIG, prominent, only enabled in loop mode)
        self.btn_exit_loop = ctk.CTkButton(
            transport_frame, text="⮑  EXIT LOOP", width=150, height=50,
            font=(FONT_UI, 16, "bold"),
            fg_color=COLOR_BTN_DISABLED,
            text_color=COLOR_TEXT_DISABLED,
            state="disabled",
            command=self._on_exit_loop
        )
        self.btn_exit_loop.pack(side="left", padx=8, pady=10)
        self._tip_exit = ToolTip(self.btn_exit_loop, "Exit the current loop at the next boundary  (E)")

        # Fade Exit button (smaller, next to EXIT)
        self.btn_fade_exit = ctk.CTkButton(
            transport_frame, text="FADE", width=90, height=50,
            font=(FONT_UI, 14),
            fg_color=COLOR_BTN_DISABLED,
            text_color=COLOR_TEXT_DISABLED,
            state="disabled",
            command=lambda: self._on_fade_exit(None)  # None = the vamp's own exit fade
        )
        self.btn_fade_exit.pack(side="left", padx=8, pady=10)
        self._tip_fade = ToolTip(self.btn_fade_exit, "Fade out and exit the current loop  (F)")

        # Narrow windows: switch the transport buttons to compact labels so
        # none of them (or the clock) gets clipped.
        self._transport_compact = None
        self._transport_frame = transport_frame
        transport_frame.bind("<Configure>", self._on_transport_resize, add="+")

        # =========================================================
        # GO PANEL (directly under the transport)
        # =========================================================
        self.go_panel = GoPanel(
            self.content_frame,
            on_go=lambda: self._on_go("ui"),
            on_loop_selection=self._on_loop_selection,
            on_goto_bar=self._on_goto_bar,
        )
        self.go_panel.pack(fill="x", padx=PADDING_MEDIUM, pady=(0, PADDING_SMALL))
        self.go_panel.set_go_keys(keys_display(self._key_map.get("go", [])))

        # Status bar (bottom of content area)
        self.status_label = ctk.CTkLabel(
            self.content_frame, text="Ready",
            font=(FONT_UI, 11), text_color=COLOR_TEXT_DIM
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
            self.bottom_deck, "CUE SHEET", initially_open=True
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
            on_toggle_skip=self._on_toggle_skip,
            on_delete_skip=self._on_delete_skip,
            # Hold callbacks: passed only once CueSheetPanel accepts them
            **supported_kwargs(CueSheetPanel.__init__, dict(
                on_add_hold=self._on_add_hold,
                on_rename_hold=self._on_rename_hold,
                on_delete_hold=self._on_delete_hold,
                on_jump_hold=self._on_jump_hold,
            ))
        )
        self.cue_sheet.pack(fill="x")

        # =========================================================
        # AUTO LOOP FINDER (Collapsed by default)
        # =========================================================
        self.finder_section = CollapsibleSection(
            self.bottom_deck, "AUTO LOOP FINDER", initially_open=False
        )
        self.finder_section.pack(fill="x", padx=PADDING_MEDIUM, pady=(0, PADDING_MEDIUM))

        # Collapsing the finder ends any candidate preview. CollapsibleSection's
        # header lambdas look up ``toggle`` on the instance, so wrapping it
        # here keeps the shared widget's API untouched.
        _section_toggle = self.finder_section.toggle
        def _finder_toggle():
            _section_toggle()
            if not self.finder_section._is_open:
                self._stop_preview()
        self.finder_section.toggle = _finder_toggle

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
            header_frame, text="◨", width=40, height=30,
            fg_color=COLOR_BG_LIGHT, border_width=1,
            border_color=COLOR_BORDER, text_color=COLOR_TEXT,
            hover_color=COLOR_HOVER, command=self._toggle_right_sidebar
        )
        self.btn_toggle_right.pack(side="right", padx=(15, 0))
        ToolTip(self.btn_toggle_right, "Toggle cue details sidebar  (N)")

        # Share button (local network)
        self.btn_share = ctk.CTkButton(
            header_frame, text="Share", width=80, height=30,
            font=(FONT_UI, 11),
            fg_color=COLOR_BG_LIGHT, border_width=1,
            border_color=COLOR_BORDER, text_color=COLOR_TEXT,
            hover_color=COLOR_HOVER, command=self._toggle_web_share
        )
        self.btn_share.pack(side="right", padx=(10, 0))
        ToolTip(self.btn_share, "Share cue monitor over local network for backstage devices")

        # Keyboard shortcuts reference button
        btn_hotkeys = ctk.CTkButton(
            header_frame, text="?", width=34, height=30,
            font=(FONT_UI, 14),
            fg_color=COLOR_BG_LIGHT, border_width=1,
            border_color=COLOR_BORDER, text_color=COLOR_TEXT,
            hover_color=COLOR_HOVER, command=self._show_hotkeys_modal
        )
        btn_hotkeys.pack(side="right", padx=(10, 0))
        ToolTip(btn_hotkeys, "View keyboard shortcuts")

        # App settings (MIDI, keys, OSC)
        self.btn_settings = ctk.CTkButton(
            header_frame, text="⚙", width=34, height=30,
            font=(FONT_UI, 14),
            fg_color=COLOR_BG_LIGHT, border_width=1,
            border_color=COLOR_BORDER, text_color=COLOR_TEXT,
            hover_color=COLOR_HOVER, command=self._open_settings_dialog
        )
        self.btn_settings.pack(side="right", padx=(10, 0))
        ToolTip(self.btn_settings, "Settings: MIDI, keyboard / foot pedal keys, OSC output")

        # Per-song settings (tempo / key / tempo map / stems / output)
        self.btn_song = ctk.CTkButton(
            header_frame, text="Song", width=80, height=30,
            font=(FONT_UI, 11),
            fg_color=COLOR_BG_LIGHT, border_width=1,
            border_color=COLOR_BORDER, text_color=COLOR_TEXT,
            hover_color=COLOR_HOVER, command=self._open_song_dialog
        )
        self.btn_song.pack(side="right", padx=(10, 0))
        ToolTip(self.btn_song, "Song settings: tempo, key, BPM, stems, output")

        # Live Mode toggle; turns into a red LIVE badge while on
        self.btn_live = ctk.CTkButton(
            header_frame, text="Live Mode", width=110, height=30,
            font=(FONT_UI, 11, "bold"),
            fg_color=COLOR_BG_LIGHT, border_width=1,
            border_color=COLOR_BORDER, text_color=COLOR_TEXT,
            hover_color=COLOR_HOVER, command=self._toggle_live_mode
        )
        self.btn_live.pack(side="right", padx=(10, 0))
        self._tip_live = ToolTip(self.btn_live, "Live Mode locks editing during a show  (L)")

        # Main / backup mirroring: status badge and the backup's TAKE OVER
        # button (both hidden until mirroring runs; see _on_mirror_status)
        self.lbl_mirror = ctk.CTkLabel(
            header_frame, text="", font=(FONT_UI, 12, "bold"),
            text_color="#ffffff", fg_color=COLOR_BG_LIGHT, corner_radius=6, height=30)
        self.btn_take_over = ctk.CTkButton(
            header_frame, text="TAKE OVER", width=130, height=34,
            font=(FONT_UI, 15, "bold"), fg_color="#e67e22", hover_color="#cf6d17",
            text_color="#ffffff", command=self._on_take_over)
        ToolTip(self.btn_take_over, "Stop following the main machine: this machine runs the show")
        self._mirror_widgets_packed = False

        # Re-pack the title last so the header buttons (Live Mode above all)
        # get their space first; a long title is clipped instead.
        self.title_label.pack_forget()
        self.title_label.configure(anchor="w")
        self.title_label.pack(side="left", fill="x", expand=True)
        self._header_compact = False
        header_frame.bind("<Configure>", self._on_header_resize, add="+")

    # =========================================================================
    # TRANSPORT
    # =========================================================================

    def _on_play_pause_toggle(self):
        """Toggle between play and pause. During a candidate preview, stop it."""
        if self.app_state is None or self._blocked_by_following():
            return
        if self.app_state.is_previewing():
            self.app_state.stop_preview()
            return
        self.app_state.toggle_play_pause()

    def _on_stop(self):
        if self.app_state is None:
            return
        if self.library.cancel_drag():
            return      # Esc while dragging a show row only cancels the drag
        if self._blocked_by_following():
            return
        self.app_state.stop()
        if self.waveform:
            self.waveform.update_playhead(0)

    def _on_waveform_seek(self, position_frac: float) -> bool:
        """Waveform click. Returns False when the seek was refused (the
        waveform then leaves its playhead where the engine is)."""
        if self.app_state is None or self.waveform is None:
            return False
        position = position_frac * self.app_state.song_length
        if not self.app_state.user_seek(position):
            return False    # Live Mode: free seeking is locked
        self.waveform.update_playhead(position)
        self.time_label.configure(text=format_time(position))
        self._update_bar_beat(position)
        return True

    def _on_set_in(self):
        self.app_state.update_selected_loop(start=self.app_state.get_position())

    def _on_set_out(self):
        self.app_state.update_selected_loop(end=self.app_state.get_position())

    def _on_exit_loop(self):
        """EXIT LOOP (in loop mode) / SKIP VAMP (in transport mode)."""
        if self.app_state is None:
            return
        in_loop = self.app_state.is_in_loop_mode()
        self.app_state.queue_exit(fade_mode=False)
        if in_loop:
            self._set_exit_buttons('waiting')
            self.status_label.configure(text="Exiting at loop boundary...")

    def _on_fade_exit(self, fade_ms=None):
        """Fade out of the loop (fade_ms=None uses the vamp's own exit fade)."""
        if self.app_state is None:
            return
        if self.app_state.is_in_loop_mode():
            self.app_state.queue_exit(fade_mode=True, fade_ms=fade_ms)
            self._set_exit_buttons('waiting')
            label = f"Fading out ({fade_ms}ms)..." if fade_ms else "Fading out..."
            self.status_label.configure(text=label)
        else:
            # Transport mode - skip the upcoming vamp
            self.app_state.queue_exit(fade_mode=False)

    # =========================================================================
    # EXIT / FADE BUTTON STYLING
    # =========================================================================

    def _set_exit_buttons(self, mode: str):
        """
        Style the EXIT LOOP and FADE buttons for a UI mode:
        'loop' (exit active), 'skip' (skip upcoming vamp), 'waiting' (exit
        queued), 'skipping' (skip queued) or 'disabled'.
        """
        self._exit_mode = mode
        fade_on = dict(state="normal", fg_color=COLOR_BTN_WARNING, text_color="#000000")
        fade_off = dict(state="disabled", fg_color=COLOR_BTN_DISABLED, text_color=COLOR_TEXT_DISABLED)
        styles = {
            'loop':     (dict(state="normal", fg_color=COLOR_BTN_SUCCESS, text_color="#ffffff"), fade_on),
            'skip':     (dict(state="normal", fg_color=COLOR_BTN_PRIMARY, text_color="#ffffff"), fade_on),
            'waiting':  (dict(state="disabled", fg_color=COLOR_BTN_WARNING, text_color="#000000"), fade_off),
            'skipping': (dict(state="disabled", fg_color=COLOR_BTN_DISABLED, text_color="#aaaaaa"), fade_off),
            'disabled': (dict(state="disabled", fg_color=COLOR_BTN_DISABLED, text_color=COLOR_TEXT_DISABLED), fade_off),
        }
        exit_style, fade_style = styles[mode]
        # Label follows the transport's compact / full layout
        texts = self._EXIT_TEXT_COMPACT if self._transport_compact else self._EXIT_TEXT_FULL
        exit_style = dict(exit_style, text=texts[mode])
        self.btn_exit_loop.configure(**exit_style)
        self.btn_fade_exit.configure(**fade_style)

    _EXIT_TEXT_FULL = {'loop': "⮑  EXIT LOOP", 'skip': "⏭  SKIP VAMP", 'waiting': "⌛  EXITING...",
                       'skipping': "⏭  SKIPPING...", 'disabled': "⮑  EXIT LOOP"}
    _EXIT_TEXT_COMPACT = {'loop': "⮑ EXIT", 'skip': "⏭ SKIP", 'waiting': "⌛ ...",
                          'skipping': "⏭ ...", 'disabled': "⮑ EXIT"}

    def _refresh_exit_buttons(self):
        """Derive the EXIT/FADE button mode from the current backend state."""
        s = self.app_state
        if s is None:
            mode = 'disabled'
        elif s.is_in_loop_mode():
            mode = 'waiting' if s.exit_queue_active else 'loop'
        elif s.is_playing() and any(l.active for l in s.loops):
            mode = 'skip'
        else:
            mode = 'disabled'
        self._set_exit_buttons(mode)

    def _update_loop_mode(self, in_loop):
        """Update UI when entering/exiting loop mode."""
        if in_loop:
            if self.app_state and self.app_state.is_previewing():
                self.status_label.configure(text="♻ Previewing candidate loop (Space/Esc to stop)")
            else:
                self.status_label.configure(text="♻ Seamless loop active")
            self.loop_indicator.configure(text="♻ LOOPING")
            total = None
            try:
                loop = self.app_state._current_loop() if self.app_state else None
                total = getattr(loop, 'repeat_count', None) or None
            except Exception:
                total = None
            self.go_panel.set_pass(1, total)
        else:
            self.status_label.configure(text="Exited loop")
            self.loop_indicator.configure(text="")
            self.go_panel.set_pass(None)
        self._refresh_exit_buttons()

    def _on_loop_skip_queued(self, loop_name):
        """Handle vamp skip queued from transport mode."""
        self.status_label.configure(text=f"⏭ Skipping vamp: {loop_name}")
        self._set_exit_buttons('skipping')

    def _on_loop_skip_cleared(self):
        """Skip flag / queued exit cleared (executed or cancelled by a seek)."""
        self._refresh_exit_buttons()
        if self.app_state is None:
            return
        if self.app_state.is_in_loop_mode():
            self.status_label.configure(text="♻ Seamless loop active")
        elif self.app_state.is_playing():
            self.status_label.configure(text="Playing")
        elif self.app_state.state == PlaybackState.PAUSED:
            self.status_label.configure(text="Paused")

    # =========================================================================
    # VAMP MENU
    # =========================================================================

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

        if self.app_state.live_mode:
            self._on_blocked_in_live_mode("add_loop")
            return
        current_pos = self.app_state.get_position()
        created = self.app_state.add_loop(
            start=current_pos,
            end=min(current_pos + 5.0, self.app_state.song_length),
            name=f"Vamp {len(self.app_state.loops) + 1}"
        )
        if created is None:
            # Playhead at the very end: no room for a vamp.
            self.status_label.configure(text="Cannot add a vamp at the end of the song")
            return

        self._open_vamp_settings(self.app_state.selected_loop_index)

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
        if self.app_state.live_mode:
            # Jumping moves playback: locked like every other free seek
            self._on_blocked_in_live_mode("seek")
            return

        self.app_state.select_loop(loop_idx)

        if 0 <= loop_idx < len(self.app_state.loops):
            loop = self.app_state.loops[loop_idx]
            self.app_state.play_from(loop.start)

    # =========================================================================
    # LOADING OVERLAY / SIDEBARS
    # =========================================================================

    def show_loading(self):
        """Show the loading overlay on top of content."""
        self.loader.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.loader.lift() # Ensure it's on top

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
    # HOTKEYS MODAL
    # =========================================================================

    def _show_hotkeys_modal(self):
        """Show a modal with all keyboard shortcuts."""
        def k(action):
            return keys_display(self._key_map.get(action, []))

        if self._lift_existing(self._hotkeys_popup):
            return
        popup = ctk.CTkToplevel(self)
        self._hotkeys_popup = popup
        popup.title("Keyboard Shortcuts")
        popup.geometry("380x560")
        popup.resizable(False, False)
        popup.configure(fg_color=COLOR_BG_DARK)
        popup.attributes("-topmost", True)
        after_if_alive(popup, 200, lambda: popup.attributes("-topmost", False))

        ctk.CTkLabel(
            popup, text="⌨  Keyboard Shortcuts",
            font=(FONT_UI, 16, "bold"), text_color=COLOR_TEXT
        ).pack(pady=(16, 12))

        # Scrollable frame for the shortcuts
        scroll = ctk.CTkScrollableFrame(
            popup, fg_color=COLOR_BG_MEDIUM, corner_radius=8,
            width=320, height=400
        )
        scroll.pack(padx=16, fill="both", expand=True)

        shortcuts = [
            ("Show control", [
                (k("go"), "GO (perform STANDBY)"),
                (k("toggle_live"), "Toggle Live Mode"),
                (k("add_hold"), "Add hold at playhead"),
            ]),
            ("Playback", [
                (k("play_pause"), "Play / Pause"),
                (k("stop"), "Stop"),
                (k("seek_start"), "Go to start"),
                ("← / →", "Nudge ±0.1s"),
                ("Ctrl+← / →", "Nudge ±1.0s"),
                (k("prev_cue") + " / " + k("next_cue"), "Prev / Next marker"),
            ]),
            ("Looping", [
                ("I", "Set loop IN"),
                ("O", "Set loop OUT"),
                (k("exit_vamp"), "Exit loop at boundary"),
                (k("fade_exit"), "Fade exit from loop"),
            ]),
            ("Cue Management", [
                (k("add_marker"), "Add cue marker at playhead"),
                ("N", "Toggle cue details sidebar"),
                ("S", "Save all data"),
            ]),
        ]

        for section_title, keys in shortcuts:
            ctk.CTkLabel(
                scroll, text=section_title,
                font=(FONT_UI, 12, "bold"), text_color=COLOR_TEXT,
                anchor="w"
            ).pack(fill="x", padx=10, pady=(12, 4))

            for key, action in keys:
                row = ctk.CTkFrame(scroll, fg_color="transparent", height=28)
                row.pack(fill="x", padx=10, pady=1)
                row.pack_propagate(False)

                key_badge = ctk.CTkLabel(
                    row, text=key, width=130,
                    font=(FONT_MONO, 11, "bold"), text_color="#58a6ff",
                    anchor="w"
                )
                key_badge.pack(side="left", padx=(0, 8))

                ctk.CTkLabel(
                    row, text=action,
                    font=(FONT_UI, 11), text_color=COLOR_TEXT_DIM,
                    anchor="w"
                ).pack(side="left", fill="x", expand=True)

        # Note about text fields
        ctk.CTkLabel(
            scroll, text="Shortcuts are paused while typing in text fields. "
                         "Change keys (and foot pedals) in ⚙ Settings.",
            font=(FONT_UI, 10), text_color=COLOR_TEXT_DIM,
            wraplength=290
        ).pack(padx=10, pady=(16, 10))

        # Close button
        ctk.CTkButton(
            popup, text="Close", width=80, height=28,
            fg_color=COLOR_BG_LIGHT, text_color=COLOR_TEXT,
            command=popup.destroy
        ).pack(pady=(8, 12))

    # =========================================================================
    # WEB SHARING (local network cue monitor)
    # =========================================================================

    @staticmethod
    def _lift_existing(popup) -> bool:
        """Raise an already-open pop-up; False if there is none (or it is gone)."""
        try:
            if popup is not None and popup.winfo_exists():
                popup.deiconify()
                popup.lift()
                popup.focus_force()
                return True
        except tk.TclError:
            pass
        return False

    def _close_share_popup(self):
        popup, self._share_popup = self._share_popup, None
        try:
            if popup is not None and popup.winfo_exists():
                popup.destroy()
        except tk.TclError:
            pass

    def _toggle_web_share(self):
        """Start or stop the local network cue monitor."""
        if self._web_server and self._web_server.running:
            # Stop sharing
            self._web_server.stop()
            self._web_server = None
            self._close_share_popup()       # its URL no longer works
            self.btn_share.configure(
                text="↗" if self._header_compact else "Share",
                fg_color=COLOR_BG_LIGHT,
                text_color=COLOR_TEXT
            )
            self.status_label.configure(text="Sharing stopped")
            return
        self._start_web_share(show_popup=True)

    def _start_web_share(self, show_popup=True):
        """Start the cue monitor server (the share popup shows the URL)."""
        if self._closing or (self._web_server and self._web_server.running):
            return
        server = CueWebServer(self._shared_cue_state, port=8080)
        try:
            url = server.start()
        except WebServerStartError as e:
            logger.error(f"Failed to start web server: {e}")
            self.status_label.configure(text=f"Share failed: {e}")
            messagebox.showerror("Cue Monitor", f"Could not start sharing:\n\n{e}", parent=self)
            return
        except Exception as e:
            logger.exception("Failed to start web server")
            self.status_label.configure(text=f"Share failed: {e}")
            return

        self._web_server = server
        self.btn_share.configure(
            text="●" if self._header_compact else "● Sharing",
            fg_color=COLOR_BTN_SUCCESS,
            text_color="#ffffff"
        )
        self.status_label.configure(text=f"Sharing at {url}")
        if show_popup:
            self._show_share_popup(url, server.get_urls()[1:])

    def _show_share_popup(self, url, other_urls=()):
        """Show a popup with the URL and QR code (one at a time)."""
        self._close_share_popup()
        popup = ctk.CTkToplevel(self)
        self._share_popup = popup
        popup.title("Cue Monitor - Share")
        popup.geometry("420x560" if other_urls else "420x520")
        popup.resizable(False, False)
        popup.configure(fg_color=COLOR_BG_DARK)
        popup.attributes("-topmost", True)
        after_if_alive(popup, 200, lambda: popup.attributes("-topmost", False))

        ctk.CTkLabel(
            popup, text="Cue monitor is live",
            font=(FONT_UI, 18, "bold"), text_color=COLOR_TEXT
        ).pack(pady=(20, 10))

        ctk.CTkLabel(
            popup, text="Open this URL on any device on the same network:",
            font=(FONT_UI, 12), text_color=COLOR_TEXT_DIM,
            wraplength=370
        ).pack(pady=(0, 10))

        # URL (copyable)
        url_frame = ctk.CTkFrame(popup, fg_color=COLOR_BG_MEDIUM, corner_radius=8)
        url_frame.pack(padx=20, pady=5, fill="x")

        url_label = ctk.CTkLabel(
            url_frame, text=url,
            font=(FONT_MONO, 13, "bold"), text_color="#58a6ff",
            cursor="hand2", wraplength=360
        )
        url_label.pack(padx=15, pady=12)

        def copy_url():
            self.clipboard_clear()
            self.clipboard_append(url)
            copy_btn.configure(text="Copied!")
            after_if_alive(popup, 2000, lambda: copy_btn.configure(text="Copy URL"))

        copy_btn = ctk.CTkButton(
            popup, text="Copy URL", width=120, height=32,
            fg_color=COLOR_BTN_PRIMARY, text_color=COLOR_BTN_TEXT,
            command=copy_url
        )
        copy_btn.pack(pady=8)

        server = self._web_server
        if server is not None and hasattr(server, "revoke_token"):
            def new_link():
                # Old links / QR codes stop working; phones must rescan
                new_url = server.revoke_token()
                if new_url and server is self._web_server:
                    self.status_label.configure(text=f"Sharing at {new_url}")
                    self._show_share_popup(new_url, server.get_urls()[1:])

            ctk.CTkButton(
                popup, text="New share link", width=120, height=26,
                fg_color=COLOR_BG_LIGHT, border_width=1, border_color=COLOR_BORDER,
                text_color=COLOR_TEXT, hover_color=COLOR_HOVER, command=new_link
            ).pack(pady=(0, 4))

        if other_urls:
            ctk.CTkLabel(
                popup, text="Other addresses (if the one above does not work):",
                font=(FONT_UI, 10), text_color=COLOR_TEXT_DIM
            ).pack(pady=(4, 0))
            ctk.CTkLabel(
                popup, text="\n".join(other_urls),
                font=(FONT_MONO, 10), text_color=COLOR_TEXT_DIM,
                wraplength=370, justify="center"
            ).pack(pady=(0, 4))

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
                font=(FONT_UI, 11), text_color=COLOR_TEXT_DIM
            ).pack()
        except ImportError:
            ctk.CTkLabel(
                popup, text="(Install 'qrcode' and 'pillow' for QR code)",
                font=(FONT_UI, 11), text_color=COLOR_TEXT_DIM
            ).pack(pady=20)

        # Close button
        ctk.CTkButton(
            popup, text="Close", width=100, height=30,
            fg_color=COLOR_BG_LIGHT, text_color=COLOR_TEXT,
            command=popup.destroy
        ).pack(pady=(15, 20))

    # =========================================================================
    # EVENT WIRING / SHORTCUTS
    # =========================================================================

    def _wire_callbacks(self):
        """
        Wire StateManager events to the thread-safe queue. Handlers run on
        the Tk thread in _check_msg_queue (avoids macOS EXC_BREAKPOINT from
        Tkinter calls in background threads).
        """
        def q(key):
            return lambda *args: self.msg_queue.put((key, args))

        for event in (
            'position_update', 'state_change',
            'loop_mode_enter', 'loop_mode_exit', 'loop_cycle',
            'song_loaded', 'song_ended',
            'loop_points_changed', 'loops_changed', 'markers_changed', 'skips_changed',
            'holds_changed',
            'detection_started', 'detection_complete', 'cut_detection_complete',
            'loop_skip_queued', 'loop_skip_cleared',
            'data_load_failed', 'save_failed', 'save_succeeded', 'preview_changed',
            'standby_changed', 'go_fired', 'go_next_song',
            'hold_reached', 'hold_released',
            'live_mode_changed', 'blocked_in_live_mode',
            'settings_changed', 'stems_changed', 'render_state',
            'tempo_estimated', 'device_error', 'device_lost', 'device_restored',
            'go_next_entry', 'show_changed', 'current_entry_changed', 'next_up_changed',
            'show_save_failed', 'preload_ready', 'preload_failed',
        ):
            self.app_state.on(event, q(event))
        # GO at the end of a song advances through the SHOW (go_next_entry).
        # There is no separate library order any more, so an empty show has
        # no next song (next_song_provider stays None: "End of song list").
        self.app_state.next_song_provider = None
        self.library.render(self.app_state.show_snapshot())

        self._attach_mirror()

        # MIDI: actions and learn results arrive on the MIDI polling thread;
        # route both through the queue so perform_action runs on the Tk thread.
        midi = getattr(self.app_state, 'midi', None)
        if midi is not None:
            midi.on_action = q('midi_action')
            midi.on_learned = q('midi_learned')
            midi.on_status = q('midi_status')
            self._open_saved_midi_port(midi)

        self._restore_output_device()

        # Initial GO panel / live state
        self._on_standby_changed(self.app_state.standby())
        call_optional(self.cue_sheet, 'update_go_log', list(self.app_state.go_log[-20:]))
        self._on_live_mode_changed(self.app_state.live_mode)

        # Acks from backstage devices (polled on the Tk thread)
        self.after(1000, self._poll_acks)

    def _open_saved_midi_port(self, midi):
        """Re-open the MIDI input remembered in preferences (worker thread)."""
        try:
            port = (preferences.load_preferences().get("midi") or {}).get("port")
        except Exception:
            port = None
        if not port:
            return

        def _open():
            # restore_port: skipped if the user already chose a port in Settings
            restore = getattr(midi, "restore_port", None)
            if (restore(port) if restore is not None else midi.open(port)):
                logger.info(f"MIDI input re-opened: {port}")
            else:
                logger.warning(f"Saved MIDI input '{port}' not available: {midi.last_error}")
        threading.Thread(target=_open, name="midi-open", daemon=True).start()

    def _restore_output_device(self):
        """Apply the output device saved by Song settings (if it still exists)."""
        if self.app_state is not None:
            self.app_state.restore_output_device()

    # ---- keyboard ---------------------------------------------------------

    def _is_typing(self):
        """True if focus is in a text input widget (shortcuts are paused)."""
        try:
            focused = self.focus_get()
        except (KeyError, tk.TclError):
            return False
        if focused is None:
            return False
        if focused.winfo_class() in ('Text', 'Entry', 'TEntry', 'Spinbox'):
            return True
        return isinstance(focused, (ctk.CTkTextbox, ctk.CTkEntry))

    def _safe(self, fn):
        """Wrap a key handler: no-op while typing, while a song is loading
        (the overlay is up) or before the backend exists."""
        def wrapper(e):
            if self._is_typing():
                return None
            if self._loading:
                return "break"
            if self.app_state is not None:
                fn()
            return "break"
        return wrapper

    def _bind_shortcuts(self):
        safe = self._safe
        # Fixed shortcuts (not in the configurable key map); letters are
        # bound in both cases so Caps Lock / Shift do not disable them.
        fixed = [
            ("i", safe(self._on_set_in)),
            ("o", safe(self._on_set_out)),
            ("s", safe(self._on_save)),
            ("n", lambda e: None if self._is_typing() else self._toggle_right_sidebar()),
        ]
        self._fixed_bindings = [(seq, handler) for key, handler in fixed
                                for seq in self._key_sequences(key)]
        self._fixed_bindings += [
            ("<Left>", safe(lambda: self.app_state.nudge(-0.1))),
            ("<Right>", safe(lambda: self.app_state.nudge(0.1))),
            ("<Control-Left>", safe(lambda: self.app_state.nudge(-1.0))),
            ("<Control-Right>", safe(lambda: self.app_state.nudge(1.0))),
        ]
        # Configurable actions (settings_dialog.KEY_ACTIONS)
        self._key_handlers = {
            "go": lambda: self._on_go("key"),
            "play_pause": self._on_play_pause_toggle,
            "stop": self._on_stop,
            "exit_vamp": self._on_exit_loop,
            "fade_exit": lambda: self._on_fade_exit(None),
            "add_marker": self._on_add_marker,
            "add_hold": lambda: self._on_add_hold(),
            "next_cue": lambda: self.app_state.jump_to_next_marker(),
            "prev_cue": lambda: self.app_state.jump_to_prev_marker(),
            "toggle_live": self._on_toggle_live_key,
            "seek_start": self._on_seek_start,
        }
        self.apply_key_map(self._key_map)

    SHOW_KEY_ACTIONS = ("go", "play_pause", "stop")

    def handle_show_key(self, event) -> bool:
        """
        GO / play-pause / stop pressed while a dialog has focus (dialogs
        forward their key presses here). Ignored while typing in an entry.
        Returns True if the key was handled.
        """
        if self.app_state is None or self._is_typing():
            return False
        if self._loading:
            return False        # loading overlay up: transport keys are paused
        keysym = getattr(event, "keysym", "")
        for action in self.SHOW_KEY_ACTIONS:
            keys = self._key_map.get(action, [])
            if keysym in keys or (len(keysym) == 1 and keysym.lower() in
                                  [k.lower() for k in keys if len(k) == 1]):
                fn = self._key_handlers.get(action)
                if fn is not None:
                    fn()
                    return True
        return False

    @staticmethod
    def _key_sequences(keysym):
        """Tk event sequences for a stored keysym (letters bind both cases)."""
        if len(keysym) == 1 and keysym.isalpha():
            return [f"<KeyPress-{keysym.lower()}>", f"<KeyPress-{keysym.upper()}>"]
        return [f"<KeyPress-{keysym}>"]

    def apply_key_map(self, key_map):
        """(Re)bind the configurable keys; a mapped key overrides a fixed one."""
        for seq in self._bound_key_seqs:
            try:
                self.unbind(seq)
            except tk.TclError:
                pass
        self._bound_key_seqs = []
        for seq, handler in self._fixed_bindings:
            self.bind(seq, handler)
        for action, keys in key_map.items():
            fn = self._key_handlers.get(action)
            if fn is None:
                continue
            handler = self._safe(fn)
            for keysym in keys:
                for seq in self._key_sequences(keysym):
                    try:
                        self.bind(seq, handler)
                        self._bound_key_seqs.append(seq)
                    except tk.TclError as e:
                        logger.warning(f"Cannot bind key {keysym!r} for {action}: {e}")
        self._key_map = {a: list(k) for a, k in key_map.items()}
        self.go_panel.set_go_keys(keys_display(self._key_map.get("go", [])))
        self._refresh_key_hints()

    def _key_hint(self, action) -> str:
        return keys_display(self._key_map.get(action, []))

    def _refresh_key_hints(self):
        """Tooltips / hints show the keys currently mapped (not the defaults)."""
        k = self._key_hint
        for tip, text in (
            (getattr(self, "_tip_play", None), f"Play / Pause  ({k('play_pause')})"),
            (getattr(self, "_tip_stop", None), f"Stop playback  ({k('stop')})"),
            (getattr(self, "_tip_exit", None),
             f"Exit the current loop at the next boundary  ({k('exit_vamp')})"),
            (getattr(self, "_tip_fade", None),
             f"Fade out and exit the current loop  ({k('fade_exit')})"),
            (getattr(self, "_tip_live", None),
             f"Live Mode locks editing during a show  ({k('toggle_live')})"),
        ):
            if tip is not None:
                tip.text = text
        call_optional(self.cue_sheet, 'set_key_hints',
                      {action: k(action) for action in self._key_map})

    def _on_save(self):
        if not self.app_state.current_song_id:
            self.status_label.configure(text="No song data to save")
            return
        self.app_state.save_loop()
        if self.app_state.flush_save():
            self.status_label.configure(text="✓ Saved", text_color=COLOR_TEXT)
        else:
            self._on_save_failed(self.app_state.last_save_error)

    def _on_save_failed(self, message):
        self._save_failed_ui = True
        self.status_label.configure(text=f"SAVE FAILED — changes pending: {message}",
                                    text_color="#ff7b72")

    def _on_save_succeeded(self):
        if getattr(self, '_save_failed_ui', False):
            self._save_failed_ui = False
            self.status_label.configure(text="✓ Pending changes saved", text_color=COLOR_TEXT)

    # =========================================================================
    # SONG LOADING
    # =========================================================================

    def _on_song_select(self, path: str, from_go: bool = False, entry_id=None):
        """
        Load a song in a background thread. Ignored while a load is running.
        ``from_go``: GO advanced to this song -- allowed in Live Mode, and it
        starts playing once loaded. ``entry_id``: the show entry to load
        (StateManager.load_entry), so a reprise row becomes the current one.
        """
        if self.app_state is None or self._loading:
            return
        self._autoplay_path = path if from_go else None
        # Song Settings belongs to the current song: close it now (pending
        # gain changes are flushed to the song they were made for).
        self._close_song_dialog()
        self._loading = True
        self.library.set_enabled(False)
        if self._has_cached_envelope(path):
            # Preloaded (instant GO): the load is quick, so the overlay only
            # appears if it unexpectedly takes longer. _loading still guards.
            self._overlay_after = after_if_alive(
                self, PRELOADED_OVERLAY_DELAY_MS,
                lambda: self.show_loading() if self._loading else None)
        else:
            self.show_loading()

        # Run the heavy loading in a separate thread so UI doesn't freeze
        # Free unreachable Tk objects (images, widgets) HERE on the Tk thread:
        # the decode allocates heavily, and a collection triggered on the
        # worker would run their __del__ Tcl calls on the wrong thread.
        gc.collect()
        threading.Thread(target=self._load_song_thread, args=(path, from_go, entry_id),
                         daemon=True).start()

    def _load_song_thread(self, path, from_go=False, entry_id=None):
        """
        Worker thread: decode the song, then compute the waveform envelope
        from the int16 memmap in chunks (or reuse the one computed when the
        song was preloaded). All UI updates go through msg_queue.
        """
        try:
            if entry_id is not None:
                success = self.app_state.load_entry(entry_id, from_go=from_go)
            else:
                success = self.app_state.load_song(path, from_go=from_go)
            if success:
                try:
                    raw_audio = self.app_state.get_raw_audio_for_waveform()
                    envelope = self._cached_envelope(path, raw_audio)
                    if envelope is None:
                        envelope = compute_envelope(raw_audio, WAVEFORM_TARGET_SAMPLES)
                    raw_audio = None
                    self.msg_queue.put(('waveform_ready', (path, envelope)))
                except Exception:
                    logger.exception("Waveform envelope computation failed")
            else:
                self.msg_queue.put(('load_failed', (path,)))
        except Exception:
            logger.exception(f"Unexpected error loading {path}")
            self.msg_queue.put(('load_failed', (path,)))
        finally:
            self.msg_queue.put(('load_finished', ()))

    def _close_song_dialog(self):
        if self._song_dialog is not None:
            try:
                self._song_dialog.close()
            except Exception:
                logger.exception("Closing Song Settings failed")
            self._song_dialog = None

    def _update_song_loaded(self, song_name, duration):
        # A dialog still bound to the previous song must not edit this one
        self._close_song_dialog()
        display_name = os.path.splitext(song_name)[0]
        self.title_label.configure(text=display_name)
        self.time_label.configure(text=format_time(0))
        # The previous song's selection / detector results are meaningless
        # here ("Use" would create a vamp at the old song's times).
        self.sel_start = self.sel_end = 0.0
        if self.waveform is not None and self.waveform.selection_mode_active:
            self._toggle_selection_mode()
        self.detector.reset()
        if self.waveform:
            # Envelope arrives separately ('waveform_ready'); reset now so
            # the loop/marker events that follow are placed on the new duration.
            self.waveform.begin_song(duration)
        path = getattr(self.app_state, 'current_song_path', None) if self.app_state else None
        if path:
            try:
                preferences.save_preferences({self.PREF_LAST_SONG: path})
            except Exception:
                logger.debug("Could not remember the last song", exc_info=True)
        self._refresh_cue_sheet()
        # Refresh notes sidebar with new song's timeline
        self._refresh_notes_timeline()
        self.status_label.configure(text=f"Loaded: {duration:.1f}s")
        self._refresh_exit_buttons()
        self._held = False
        self.go_panel.set_held(held=False)
        self.go_panel.set_pass(None)
        self._apply_tempo_map_ui()
        if self.waveform:
            call_optional(self.waveform, 'update_holds_display', list(self.app_state.holds))
        self._on_standby_changed(self.app_state.standby())
        # Immediately push fresh cue state to web server on song change
        self._push_cue_state(0.0)

    def _on_waveform_ready(self, path, envelope):
        """Plot the envelope computed by the loader thread (if still current)."""
        if self.app_state is None or self.waveform is None:
            return
        if path != self.app_state.current_song_path:
            return  # a newer song replaced this one
        self.waveform.load_waveform(envelope, self.app_state.song_length)

    def _on_load_failed(self, path):
        self._autoplay_path = None
        name = os.path.basename(path)
        self.status_label.configure(text=f"Could not load: {name}")
        messagebox.showerror(
            "Loop Station",
            f"Could not load \"{name}\".\n\nThe file may be corrupt or in an unsupported format. "
            "See the log for details.",
            parent=self
        )

    def _on_load_finished(self):
        self._loading = False
        if self._overlay_after is not None:
            try:
                self.after_cancel(self._overlay_after)
            except (tk.TclError, ValueError):
                pass
            self._overlay_after = None
        self.library.set_enabled(True)
        self.hide_loading()
        autoplay, self._autoplay_path = getattr(self, '_autoplay_path', None), None
        if (autoplay and self.app_state is not None
                and self.app_state.current_song_path == autoplay):
            self.app_state.play_after_go_load()

    def _on_data_load_failed(self, message):
        """Saved vamp/cue data could not be read; tell the user once."""
        if message in self._shown_load_errors:
            return
        if not self.winfo_viewable():
            # Window still withdrawn (splash) — try again shortly
            self.after(500, lambda: self._on_data_load_failed(message))
            return
        self._shown_load_errors.add(message)
        messagebox.showwarning("Loop Station - Saved data problem", message, parent=self)

    # =========================================================================
    # MARKER / VAMP / SKIP HANDLERS (UI -> State)
    # =========================================================================

    def _on_add_marker(self):
        if self.app_state.add_marker() is not None:
            self.status_label.configure(text="Cue point added")

    def _on_jump_to_marker(self, marker_id):
        """Cue-sheet click on a cue: a free jump, locked in Live Mode
        (next / prev cue keys and MIDI stay allowed)."""
        if self.app_state.live_mode:
            self._on_blocked_in_live_mode("seek")
            return
        self.app_state.jump_to_marker(marker_id)

    def _on_rename_marker(self, marker_id, new_name):
        self.app_state.rename_marker(marker_id, new_name)

    def _on_delete_marker(self, marker_id):
        self.app_state.delete_marker(marker_id)

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

    def _on_toggle_skip(self, skip_id):
        self.app_state.toggle_skip_active(skip_id)

    def _on_delete_skip(self, skip_id):
        self.app_state.delete_skip(skip_id)

    # =========================================================================
    # STATE -> UI
    # =========================================================================

    def _update_state(self, state):
        """Update transport buttons based on playback state."""
        is_playing = state == PlaybackState.PLAYING

        self._play_shows_pause = is_playing
        self.btn_play.configure(text=self._play_text())

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
            self.loop_indicator.configure(text="")
            self.go_panel.set_pass(None)
            if self._held:
                self._held = False
                self.go_panel.set_held(held=False)
        elif state == PlaybackState.PAUSED:
            self.status_label.configure(
                text="⚠ AUDIO DEVICE LOST — check the interface" if self._device_lost_ui else "Paused")
        elif state == PlaybackState.PLAYING:
            self.status_label.configure(text="Playing")
        elif state == getattr(PlaybackState, 'HELD', None):
            self.status_label.configure(text="⏸ HELD — press GO to continue")
        self._refresh_exit_buttons()
        self._push_cue_state(self._current_position())

    def _update_loop_points(self, loop_in, loop_out):
        """Update waveform display with new loop points."""
        if self.waveform:
            self.waveform.update_loop_markers(loop_in, loop_out)

    # Transport bar: full labels need ~700 px of logical width; below that
    # fall back to icon-style labels. Widths are in CTk logical pixels.
    _TRANSPORT_FULL = (("▶  PLAY", 130), ("⏹  STOP", 110), ("⮑  EXIT LOOP", 150), ("FADE", 90))
    _TRANSPORT_COMPACT = (("▶", 60), ("⏹", 60), ("⮑ EXIT", 90), ("FADE", 60))
    _TRANSPORT_COMPACT_BELOW = 700
    _play_shows_pause = False

    def _play_text(self) -> str:
        """PLAY / PAUSE label for the current playback state and layout."""
        if self._play_shows_pause:
            return "⏸" if self._transport_compact else "⏸  PAUSE"
        return "▶" if self._transport_compact else "▶  PLAY"

    def _on_transport_resize(self, event):
        try:
            width = int(event.width / float(self._get_window_scaling()))
        except Exception:
            width = event.width
        compact = width < self._TRANSPORT_COMPACT_BELOW
        if compact == self._transport_compact:
            return
        self._transport_compact = compact
        labels = self._TRANSPORT_COMPACT if compact else self._TRANSPORT_FULL
        buttons = (self.btn_play, self.btn_stop, self.btn_exit_loop, self.btn_fade_exit)
        for btn, (text, width_px) in zip(buttons, labels):
            btn.configure(text=text, width=width_px)
        # Labels that depend on state (PAUSE, SKIP VAMP, EXITING...)
        self.btn_play.configure(text=self._play_text())
        self._set_exit_buttons(self._exit_mode)

    def _handle_loops_changed(self, loops, selected_index):
        """Update UI when loops change."""
        if self.waveform:
            self.waveform.update_loops_display(loops, selected_index)
        self._refresh_cue_sheet()
        self._refresh_notes_timeline()
        self._refresh_exit_buttons()

    def _handle_markers_changed(self, markers):
        if self.waveform:
            self.waveform.update_markers_display(markers)
        self._refresh_cue_sheet()
        self._refresh_notes_timeline()

    def _update_skips_ui(self, skips):
        if self.waveform:
            self.waveform.update_skips_display(skips)
        self._refresh_cue_sheet()

    POSITION_UI_INTERVAL = 0.033    # ~30 fps cap for the position displays

    def _update_position(self, position, is_loop_mode):
        """
        Update position display. Throttled to ~30fps to reduce UI load; an
        update that arrives inside the interval is not dropped but delivered
        (the newest one) by a trailing after(), so a final paused seek
        always shows.
        """
        now = time.monotonic()
        self._pending_position = (position, is_loop_mode)
        wait = self.POSITION_UI_INTERVAL - (now - self._last_ui_update)
        if wait > 0:
            if self._position_after is None:
                try:
                    self._position_after = self.after(
                        max(1, int(wait * 1000) + 1), self._flush_position)
                except tk.TclError:
                    self._position_after = None
            return
        self._apply_position(position, is_loop_mode)

    def _flush_position(self):
        """Trailing edge of the position throttle."""
        self._position_after = None
        pending = self._pending_position
        if pending is None or self._closing:
            return
        try:
            self._apply_position(*pending)
        except Exception:
            logger.exception("Error handling 'position_update' event")

    def _apply_position(self, position, is_loop_mode):
        self._last_ui_update = time.monotonic()
        self._pending_position = None
        if self._position_after is not None:
            try:
                self.after_cancel(self._position_after)
            except (tk.TclError, ValueError):
                pass
            self._position_after = None

        # One emitted position for every consumer (waveform, label, cue
        # sheet, notes sidebar, web monitor) so they can never disagree.
        if self.waveform:
            self.waveform.update_playhead(position)
        self.time_label.configure(text=format_time(position))
        self._update_bar_beat(position)
        self.cue_sheet.update_position(position)

        is_playing = self.app_state.is_playing() if self.app_state else False
        if self.app_state:
            self.notes_sidebar.set_playback_timing(
                self.app_state.get_playback_rate(), self.app_state.countdown_to)
        self.notes_sidebar.update_position(position, is_playing=is_playing)
        if self._song_dialog is not None:
            levels = getattr(self.app_state.audio, 'get_output_levels', lambda: {})()
            self._song_dialog.update_output_levels(levels)

        self._push_cue_state(position)

    def _push_cue_state(self, position):
        """Push the current cue state to the web monitor (if sharing)."""
        if self._web_server and self._web_server.running:
            self._shared_cue_state.update_from_app(
                self.app_state, position,
                self.notes_sidebar._current_item,
                self.notes_sidebar._current_item_type,
                self.notes_sidebar._next_item,
                self.notes_sidebar._next_item_type,
            )

    def _refresh_cue_sheet(self):
        """Schedule an update to the cue sheet rather than doing it instantly."""
        if not self._pending_cue_refresh:
            self._pending_cue_refresh = True
            self.after(50, self._execute_cue_refresh)

    def _execute_cue_refresh(self):
        """Perform the actual destruction and recreation of widgets once."""
        self._pending_cue_refresh = False

        if self.app_state is not None:
            call_optional(
                self.cue_sheet, 'update_data',
                self.app_state.markers,
                self.app_state.loops,
                self.app_state.selected_loop_index,
                self.app_state.skips,
                holds=list(self.app_state.holds),
            )

    # =========================================================================
    # LIFECYCLE
    # =========================================================================

    def _refresh_notes_timeline(self):
        """Give the notes sidebar the current markers, vamps and holds."""
        s = self.app_state
        call_optional(
            self.notes_sidebar, 'update_timeline',
            s.markers if s else [], s.loops if s else [],
            holds=list(s.holds) if s else [],
        )

    def _current_position(self) -> float:
        try:
            return float(self.app_state.get_position()) if self.app_state else 0.0
        except Exception:
            return 0.0

    # =========================================================================
    # GO / STANDBY / HOLDS
    # =========================================================================

    def _on_go(self, source="ui"):
        """GO button / key: perform the standby action; flash if ignored."""
        if self.app_state is None or self._blocked_by_following():
            return
        standby = self.app_state.standby()
        if self.app_state.go(source):
            self.go_panel.flash("GO ✓", "#4cd38a", 600)
            return
        if standby.get("action", "none") != "none":
            self.go_panel.flash("ignored (double press)")
        else:
            self.go_panel.flash("nothing to GO", COLOR_TEXT_DIM)

    def _on_standby_changed(self, standby):
        self.go_panel.set_standby(standby)
        self._push_cue_state(self._current_position())

    def _on_go_fired(self, entry):
        action = entry.get("action", "")
        label = entry.get("label", "")
        self.status_label.configure(text=f"GO ({entry.get('source', '')}): {label or action}")
        if self.app_state is not None:
            call_optional(self.cue_sheet, 'update_go_log', list(self.app_state.go_log[-20:]))
        self._push_cue_state(self._current_position())

    def _on_hold_reached(self, hold):
        self._held = True
        self.go_panel.set_held(hold, held=True)
        name = getattr(hold, "name", None) or "hold"
        self.status_label.configure(text=f"⏸ HELD at {name} — press GO to continue")
        self._push_cue_state(self._current_position())

    def _on_hold_released(self):
        self._held = False
        self.go_panel.set_held(held=False)
        self.status_label.configure(text="Hold released")
        self._push_cue_state(self._current_position())

    def _handle_holds_changed(self, holds):
        if self.waveform:
            call_optional(self.waveform, 'update_holds_display', list(holds))
        self._refresh_cue_sheet()
        self._refresh_notes_timeline()
        self._push_cue_state(self._current_position())

    def _on_loop_cycle(self, n, total=None):
        self.go_panel.set_pass(n, total)

    def _on_add_hold(self, time_pos=None):
        if self.app_state is None:
            return
        hold = self.app_state.add_hold(time_pos)
        if hold is not None:
            self.status_label.configure(text=f"⏸ Hold added at {format_time(hold.time)}")

    def _on_rename_hold(self, hold_id, name):
        if self.app_state is not None:
            self.app_state.rename_hold(hold_id, name)

    def _on_delete_hold(self, hold_id):
        if self.app_state is not None:
            self.app_state.delete_hold(hold_id)

    def _on_jump_hold(self, hold_id):
        """Seek to a hold (a hair before it, so GO / play stops on it)."""
        if self.app_state is None:
            return
        hold = next((h for h in self.app_state.holds if h.id == hold_id), None)
        if hold is None:
            return
        target = max(0.0, hold.time - 0.5)
        if not self.app_state.user_seek(target):
            return      # Live Mode: jumping is locked
        if self.waveform:
            self.waveform.update_playhead(target)
        self.time_label.configure(text=format_time(target))
        self._update_bar_beat(target)

    # =========================================================================
    # MAIN / BACKUP MIRRORING (backend/mirror.py; optional)
    # =========================================================================

    MIRROR_GREEN = "#1f8f4e"
    MIRROR_RED = "#d63031"
    MIRROR_AMBER = "#b8730a"

    @staticmethod
    def mirror_badge(status):
        """(badge text, colour, flashing, show TAKE OVER) for a mirror status dict."""
        status = status or {}
        role = status.get("role")
        connected = bool(status.get("connected"))
        if role == "main":
            peer = status.get("peer")
            if isinstance(status.get("peers"), int):
                n = status["peers"]
            elif isinstance(peer, (list, tuple)):
                n = len(peer)
            else:
                n = 1 if connected else 0
            text = f"MAIN · {n} backup" + ("" if n == 1 else "s") if n else "MAIN · no backup"
            return text, LoopStationApp.MIRROR_GREEN if n else LoopStationApp.MIRROR_AMBER, False, False
        if role == "backup":
            if not status.get("following", True):
                return "BACKUP · on its own", LoopStationApp.MIRROR_AMBER, False, False
            if connected:
                return "BACKUP · following", LoopStationApp.MIRROR_GREEN, False, True
            message = str(status.get("message") or "")
            if message.startswith("Connecting") or "reject" in message.lower() \
                    or "PIN" in message[:40]:
                # Never connected yet (or refused): nothing was lost
                return "BACKUP · connecting…", LoopStationApp.MIRROR_AMBER, False, True
            return "BACKUP · MAIN LOST", LoopStationApp.MIRROR_RED, True, True
        return None, None, False, False

    def _attach_mirror(self):
        """Route the mirror link's status callback (worker thread) through msg_queue."""
        mirror = getattr(self.app_state, 'mirror', None) if self.app_state else None
        if mirror is None:
            return False
        try:
            mirror.on_status = lambda *args: self.msg_queue.put(('mirror_status', args))
            # The backup loaded a song because the main did (applier thread):
            # the waveform is computed here like a normal load. Return fast.
            mirror.on_loaded = lambda *args: self.msg_queue.put(('mirror_loaded', args))
            self._on_mirror_status(mirror.status())
        except Exception:
            logger.exception("Could not attach to the mirror link")
            return False
        return True

    def _mirror_auto_start(self):
        """Start the saved main / backup role when "Auto-start on launch" is on."""
        mirror = getattr(self.app_state, 'mirror', None) if self.app_state else None
        if mirror is None or not hasattr(mirror, 'auto_start'):
            return False
        try:
            return bool(mirror.auto_start())
        except Exception:
            logger.exception("Mirror auto-start failed")
            return False

    def _on_mirror_loaded(self, path):
        """Backup followed the main to ``path``: build its waveform (worker thread)."""
        st = self.app_state
        if st is None or path != st.current_song_path:
            return

        def _work():
            try:
                raw = st.get_raw_audio_for_waveform()
                envelope = self._cached_envelope(path, raw)
                if envelope is None:
                    envelope = compute_envelope(raw, WAVEFORM_TARGET_SAMPLES)
                raw = None
                self.msg_queue.put(('waveform_ready', (path, envelope)))
            except Exception:
                logger.exception("Waveform for the mirrored song failed")
        threading.Thread(target=_work, name="mirror-envelope", daemon=True).start()

    def mirror_following(self) -> bool:
        """True while this machine is a backup following a connected main."""
        mirror = getattr(self.app_state, 'mirror', None) if self.app_state else None
        if mirror is None:
            return False
        try:
            return bool(mirror.is_following())
        except Exception:
            status = self._mirror_status or {}
            return status.get("role") == "backup" and bool(status.get("following")) \
                and bool(status.get("connected"))

    FOLLOWING_TEXT = "Following main — TAKE OVER to control"

    def _blocked_by_following(self) -> bool:
        """Backup following: its own transport would race the mirrored actions."""
        if not self.mirror_following():
            return False
        self.status_label.configure(text=self.FOLLOWING_TEXT, text_color="#f0a020")
        self.go_panel.flash("following main", "#f0a020")
        return True

    def _apply_following_lock(self, following):
        state = "disabled" if following else "normal"
        for btn in (self.btn_play, self.btn_stop, getattr(self.go_panel, 'btn_go', None)):
            if btn is None:
                continue
            try:
                btn.configure(state=state)
            except (tk.TclError, ValueError):
                pass
        self.library.set_following(following)

    def _on_mirror_status(self, status):
        """Mirror status (routed from the mirror's worker thread)."""
        self._mirror_status = dict(status or {})
        st = self._mirror_status
        self._apply_following_lock(st.get("role") == "backup" and bool(st.get("following"))
                                   and bool(st.get("connected")))
        text, color, flashing, take_over = self.mirror_badge(self._mirror_status)
        if text is None:
            if self._mirror_widgets_packed:
                self.lbl_mirror.pack_forget()
                self.btn_take_over.pack_forget()
                self._mirror_widgets_packed = False
        else:
            if not self._mirror_widgets_packed:
                self.lbl_mirror.pack(side="right", padx=(10, 0), before=self.btn_live)
                self._mirror_widgets_packed = True
            self.lbl_mirror.configure(text=f"  {text}  ", fg_color=color)
            if take_over and not self.btn_take_over.winfo_manager():
                self.btn_take_over.pack(side="right", padx=(10, 0), before=self.lbl_mirror)
            elif not take_over and self.btn_take_over.winfo_manager():
                self.btn_take_over.pack_forget()
        self._set_mirror_flash(flashing)
        dlg = self._settings_dialog
        if dlg is not None:
            call_optional(dlg, 'on_mirror_status', self._mirror_status)

    def _set_mirror_flash(self, on):
        if not on:
            if self._mirror_flash_after is not None:
                try:
                    self.after_cancel(self._mirror_flash_after)
                except (tk.TclError, ValueError):
                    pass
                self._mirror_flash_after = None
            return
        if self._mirror_flash_after is None:
            self._mirror_flash_tick()

    def _mirror_flash_tick(self):
        self._mirror_flash_after = None
        text, color, flashing, _ = self.mirror_badge(self._mirror_status)
        if not flashing or self._closing:
            return
        self._mirror_flash_on = not self._mirror_flash_on
        try:
            self.lbl_mirror.configure(fg_color=color if self._mirror_flash_on else "#5c0f0f")
            self._mirror_flash_after = self.after(500, self._mirror_flash_tick)
        except tk.TclError:
            pass

    def _on_take_over(self):
        """Backup: stop following the main. Always allowed (also in Live Mode)."""
        mirror = getattr(self.app_state, 'mirror', None) if self.app_state else None
        if mirror is None:
            return False
        status = self._mirror_status or {}
        if status.get("connected"):
            # The main is still there: a stray click must not split the show
            try:
                if not messagebox.askyesno(
                        "Take over?", "The main machine is still connected.\n\n"
                        "Take over and run the show from this machine?",
                        icon=messagebox.WARNING, default=messagebox.NO, parent=self):
                    return False
            except tk.TclError:
                return False
        try:
            mirror.take_over()
        except Exception as e:
            logger.exception("take_over failed")
            self.status_label.configure(text=f"TAKE OVER failed: {e}", text_color="#ff7b72")
            return False
        self.status_label.configure(text="TAKEN OVER - this machine runs the show now",
                                    text_color="#f0a020")
        try:
            self._on_mirror_status(mirror.status())
        except Exception:
            logger.debug("mirror.status() failed after take_over", exc_info=True)
        return True

    # =========================================================================
    # LIVE MODE
    # =========================================================================

    def _toggle_live_mode(self):
        """Header button. Entering is one click; leaving asks first."""
        if self.app_state is None:
            return
        if not self.app_state.live_mode:
            self._prepare_enter_live()
            self.app_state.set_live_mode(True)
            return
        if self._confirm_leave_live():
            self.app_state.set_live_mode(False)
        else:
            self.status_label.configure(text="Still in Live Mode")

    def _on_toggle_live_key(self):
        """
        Toggle-live key. Leaving uses the same non-modal press-twice
        confirmation as MIDI (no Yes/No box that would swallow the next
        GO / Return press mid-show).
        """
        if self.app_state is None:
            return
        if not self.app_state.live_mode:
            self._prepare_enter_live()
        self.app_state.toggle_live_from_control()

    def _prepare_enter_live(self):
        """Commit open note editors and cancel an open vamp editor (its
        Cancel cannot restore once Live Mode locks vamp edits)."""
        self.notes_sidebar._commit_open_editors()
        modal = self._vamp_modal
        if modal is not None:
            try:
                modal._on_cancel_click()
            except Exception:
                logger.exception("Closing the vamp editor failed")
            self._vamp_modal = None

    def _confirm_leave_live(self) -> bool:
        """Yes/No, default No, so a stray L during the show changes nothing."""
        try:
            return bool(messagebox.askyesno(
                "Leave Live Mode?",
                "Leave Live Mode?\n\nEditing, seeking and previews will be unlocked.",
                icon=messagebox.WARNING, default=messagebox.NO, parent=self))
        except tk.TclError:
            return False

    def _on_live_mode_changed(self, enabled):
        enabled = bool(enabled)
        if enabled:
            self.btn_live.configure(text=self._live_button_text(True), fg_color=COLOR_BTN_DANGER,
                                    hover_color="#b52a2a", border_color=COLOR_BTN_DANGER,
                                    text_color="#ffffff")
            self.status_label.configure(
                text=f"Live Mode on — editing is locked ({self._key_hint('toggle_live')} to unlock)")
        else:
            self.btn_live.configure(text=self._live_button_text(False), fg_color=COLOR_BG_LIGHT,
                                    hover_color=COLOR_HOVER, border_color=COLOR_BORDER,
                                    text_color=COLOR_TEXT)
            self.status_label.configure(text="Live Mode off — editing unlocked")
        self.library.set_locked(enabled)
        self.go_panel.set_live(enabled)
        call_optional(self.detector, 'set_live_mode', enabled)
        if enabled and self.waveform is not None and self.waveform.selection_mode_active:
            self._toggle_selection_mode()
        call_optional(self.cue_sheet, 'set_live_mode', enabled)
        call_optional(self.notes_sidebar, 'set_live_mode', enabled)
        for dlg in (self._settings_dialog, self._song_dialog):
            if dlg is not None:
                dlg.set_live_mode(enabled)
        if enabled and self._vamp_modal is not None:
            # Live came on from elsewhere: vamp edits are locked now, so
            # close the editor (edits already applied stay, pending ones drop).
            modal, self._vamp_modal = self._vamp_modal, None
            try:
                modal.close_for_live()
            except Exception:
                logger.exception("Closing the vamp editor failed")
        self._push_cue_state(self._current_position())

    # Header: below this logical width the Live / Song / Share buttons use
    # short labels so none of them is pushed out of the window.
    _HEADER_COMPACT_BELOW = 700

    def _live_button_text(self, live):
        if live:
            return "● LIVE"
        return "Live" if self._header_compact else "Live Mode"

    def _on_header_resize(self, event):
        try:
            width = int(event.width / float(self._get_window_scaling()))
        except Exception:
            width = event.width
        compact = width < self._HEADER_COMPACT_BELOW
        if compact == self._header_compact:
            return
        self._header_compact = compact
        live = bool(self.app_state and self.app_state.live_mode)
        self.btn_live.configure(text=self._live_button_text(live), width=76 if compact else 110)
        self.btn_song.configure(text="♪" if compact else "Song", width=40 if compact else 80)
        sharing = bool(self._web_server and self._web_server.running)
        if compact:
            self.btn_share.configure(text="●" if sharing else "↗", width=40)
        else:
            self.btn_share.configure(text="● Sharing" if sharing else "Share", width=80)

    _BLOCKED_NAMES = {
        "load_song": "switching songs", "add_hold": "adding a hold",
        "add_marker": "adding a cue", "add_loop": "adding a vamp",
        "set_tempo_pitch": "changing tempo/key",
        "update_selected_loop": "editing a vamp",
        "seek": "seeking", "nudge": "nudging", "seek_start": "jumping to the start",
        "preview": "previewing", "preview_loop": "looping a selection",
        # Show (running order): "Order locked in Live Mode"
        "move_entry": "changing the show order", "add_songs": "adding songs to the show",
        "add_divider": "adding a divider", "remove_entry": "removing a song from the show",
        "duplicate_entry": "duplicating a song", "rename_entry": "renaming a show entry",
        "set_entry_cut": "cutting a song", "set_entry_segue": "changing a segue",
        "relink_entry": "relinking a file", "open_show": "opening another show",
        "open_folder": "opening another show", "new_show": "starting a new show",
        "mirror_settings": "changing main / backup settings",
    }

    def _on_blocked_in_live_mode(self, what):
        if what == "leave_live_confirm":
            self.status_label.configure(
                text="Press toggle-live again within 2 s to leave Live Mode")
            self.go_panel.flash("press again to leave Live", "#f0a020")
            return
        nice = self._BLOCKED_NAMES.get(what, str(what).replace("_", " "))
        self.status_label.configure(
            text=f"Live Mode: {nice} is locked (press {self._key_hint('toggle_live')} to unlock)")
        self.go_panel.flash("locked (Live Mode)", "#ff7b72")

    # =========================================================================
    # SONG SETTINGS / ENGINE EVENTS
    # =========================================================================

    def _apply_tempo_map_ui(self):
        """Bar:beat readout, Go-to-bar and the waveform grid follow the BPM."""
        s = self.app_state.settings if self.app_state else None
        has_bpm = bool(s and s.bpm > 0 and self.app_state.current_song_path)
        if has_bpm and not self._bar_label_visible:
            self.bar_label.pack(side="right", padx=(5, 0), pady=10, before=self.loop_indicator)
            self._bar_label_visible = True
        elif not has_bpm and self._bar_label_visible:
            self.bar_label.pack_forget()
            self._bar_label_visible = False
        self.go_panel.set_tempo_available(has_bpm)
        if self.waveform and s is not None:
            call_optional(self.waveform, 'set_tempo_map',
                          s.bpm if has_bpm else 0.0, s.beats_per_bar, s.downbeat_offset)
        self._update_bar_beat(self._current_position())

    def _update_bar_beat(self, position):
        """Bar:beat readout. Never raises: a bad tempo map must not stop
        the rest of the position update (cue sheet, sidebar, web monitor)."""
        if not self._bar_label_visible or self.app_state is None:
            return
        try:
            bar, beat, _frac = self.app_state.bar_beat_at(position)
            bar, beat = int(bar), int(beat)
        except Exception:
            logger.debug("bar_beat_at failed", exc_info=True)
            self.bar_label.configure(text="")
            return
        if bar < 1:
            self.bar_label.configure(text="Pickup")
        else:
            self.bar_label.configure(text=f"Bar {bar} · {beat}")

    def _on_settings_changed(self, settings):
        self._apply_tempo_map_ui()
        if self._song_dialog is not None:
            self._song_dialog.on_settings_changed(settings)

    def _on_stems_changed(self, stems):
        if self._song_dialog is not None:
            self._song_dialog.on_stems_changed(list(stems))

    def _on_render_state(self, state, message=""):
        if state == "rendering":
            self.status_label.configure(text="⏳ Rendering tempo/key…")
        elif state == "done":
            self.status_label.configure(text="✓ Tempo/key render done")
        else:
            self.status_label.configure(text=f"✗ Tempo/key render failed: {message}")
        if self._song_dialog is not None:
            self._song_dialog.on_render_state(state, message)

    def _on_tempo_estimated(self, bpm, offset):
        if bpm:
            self.status_label.configure(text=f"Tempo detected: ≈{float(bpm):.1f} BPM")
        if self._song_dialog is not None:
            self._song_dialog.on_tempo_estimated(bpm, offset)

    def _on_device_error(self, message):
        self.status_label.configure(text=f"⚠ Audio device: {message}")
        if self._device_lost_ui:
            return      # the GO-panel banner is showing; no dialog mid-show
        if self._song_dialog is not None:
            call_optional(self._song_dialog, 'on_device_error', message)
        if message in self._shown_device_errors:
            return
        st = self.app_state
        if st is not None and st.state != PlaybackState.STOPPED:
            return      # never pop a modal box while the show is running
        self._shown_device_errors.add(message)
        try:
            messagebox.showerror("Loop Station - Audio device", str(message), parent=self)
        except tk.TclError:
            pass

    def _on_device_lost(self, message):
        self._device_lost_ui = True
        self.go_panel.set_device_lost(True)
        self.status_label.configure(text=f"⚠ AUDIO DEVICE LOST: {message}")

    def _on_device_restored(self, name):
        self._device_lost_ui = False
        self.go_panel.set_device_lost(False)
        self.status_label.configure(text=f"✓ Audio device back: {name}")

    def _on_midi_status(self, text):
        self.status_label.configure(text=f"MIDI · {text}")
        if self._settings_dialog is not None:
            call_optional(self._settings_dialog, 'on_midi_status', text)

    # =========================================================================
    # CONTROL SURFACES
    # =========================================================================

    def _on_midi_action(self, action):
        if self.app_state is not None:
            if self._loading:
                logger.info(f"MIDI {action!r} ignored while a song is loading")
                return
            if action in ('go', 'play_pause', 'stop') and self._blocked_by_following():
                return      # backup following the main: its transport is the main's
            if action == 'toggle_live' and not self.app_state.live_mode:
                self._prepare_enter_live()
            self.app_state.perform_action(action, source="midi")

    def _on_midi_learned(self, action, key):
        if self._settings_dialog is not None:
            self._settings_dialog.on_midi_learned(action, key)
        else:
            midi = getattr(self.app_state, 'midi', None)
            if midi is not None:
                midi.save_mappings()

    # =========================================================================
    # DIALOGS
    # =========================================================================

    def _open_settings_dialog(self):
        if self.app_state is None:
            return
        if self._settings_dialog is not None:
            self._settings_dialog.lift()
            return

        def _closed():
            self._settings_dialog = None
        self._settings_dialog = SettingsDialog(
            self, self.app_state, self._key_map,
            on_keys_changed=self.apply_key_map, on_close=_closed,
        )

    def _open_song_dialog(self):
        if self.app_state is None:
            return
        if not self.app_state.current_song_path:
            self.status_label.configure(text="Load a song first to change its settings")
            return
        if self._song_dialog is not None:
            self._song_dialog.lift()
            return

        def _closed():
            self._song_dialog = None
        self._song_dialog = SongSettingsDialog(self, self.app_state, on_close=_closed)

    # =========================================================================
    # REHEARSAL TOOLS
    # =========================================================================

    def _waveform_selection(self):
        """(start, end) in seconds of the waveform selection, or None."""
        sel = call_optional(self.waveform, 'get_selection') if self.waveform else None
        if sel and sel[1] > sel[0]:
            return float(sel[0]), float(sel[1])
        if self.sel_end > self.sel_start:
            return self.sel_start, self.sel_end
        return None

    def _on_loop_selection(self):
        if self.app_state is None or not self.app_state.current_song_path:
            return
        if self.app_state.is_previewing():
            self.app_state.stop_preview()
            return
        sel = self._waveform_selection()
        if sel is None or sel[1] - sel[0] < 0.2:
            if self.waveform and not self.waveform.selection_mode_active:
                self._toggle_selection_mode()
            self.status_label.configure(
                text="Drag on the waveform to select a passage, then press Loop selection")
            return
        if not self.app_state.preview_loop(sel[0], sel[1]):
            return      # Live Mode
        self.status_label.configure(
            text=f"Looping {format_time(sel[0])}–{format_time(sel[1])} (press again to stop)")

    def _on_goto_bar(self, bar):
        s = self.app_state.settings if self.app_state else None
        if s is None or s.bpm <= 0:
            return
        bar = max(1, int(bar))
        pos = s.downbeat_offset + (bar - 1) * max(1, s.beats_per_bar) * 60.0 / s.bpm
        if not math.isfinite(pos):
            self.go_panel.flash("Tempo map is invalid", "#ff7b72")
            return
        pos = max(0.0, min(pos, self.app_state.song_length))
        if not self.app_state.user_seek(pos):
            return      # Live Mode: seeking is locked
        if self.waveform:
            self.waveform.update_playhead(pos)
        self.time_label.configure(text=format_time(pos))
        self._update_bar_beat(pos)
        self.status_label.configure(text=f"Bar {bar} ({format_time(pos)})")
        self.focus_set()  # give keys (GO) back to the window

    def _on_seek_start(self):
        if self.app_state is None:
            return
        if not self.app_state.seek_to_start():
            return      # Live Mode
        if self.waveform:
            self.waveform.update_playhead(0.0)
        self.time_label.configure(text=format_time(0.0))
        self._update_bar_beat(0.0)

    # =========================================================================
    # ACKS (backstage devices -> notes sidebar)
    # =========================================================================

    def _poll_acks(self):
        if self._closing:
            return
        try:
            if self._web_server and self._web_server.running:
                snap = self._shared_cue_state.acks_snapshot()
                if snap != self._last_acks:
                    self._last_acks = snap
                    call_optional(self.notes_sidebar, 'update_acks', snap)
        except Exception:
            logger.exception("Ack poll failed")
        try:
            self.after(1000, self._poll_acks)
        except tk.TclError:
            pass

    def _shutdown_backend(self):
        """Stop sharing, stop playback, flush saves and release the engine."""
        if self._web_server:
            # stop() is safe when the serve thread already died; it still
            # closes the listening socket.
            try:
                self._web_server.stop()
            except Exception:
                logger.exception("Error stopping web server")
        self._web_server = None

        if self.app_state is not None:
            try:
                self.app_state.stop()
            except Exception:
                logger.exception("Error stopping playback")
            try:
                self.app_state.flush_save()
            except Exception:
                logger.exception("Error flushing save")
            try:
                self.app_state.cleanup()
            except Exception:
                logger.exception("Error cleaning up backend")
            self.app_state = None

    def _show_is_running(self) -> bool:
        """True while playing / held / in Live Mode (closing would cut the show)."""
        st = self.app_state
        if st is None:
            return False
        return bool(st.live_mode) or st.state not in (PlaybackState.STOPPED, PlaybackState.PAUSED)

    def confirm_interrupt_show(self, what="quit Loop Station") -> bool:
        """Ask before an action that ends playback while the show runs (default No)."""
        if not self._show_is_running():
            return True
        live = bool(self.app_state and self.app_state.live_mode)
        detail = ("Live Mode is on and playback will stop." if live
                  else "Playback will stop.")
        try:
            return bool(messagebox.askyesno(
                "Loop Station", f"Really {what}?\n\n{detail}",
                icon=messagebox.WARNING, default=messagebox.NO, parent=self))
        except tk.TclError:
            return False

    def _on_close_request(self):
        """Window close / Cmd+Q: confirm while the show runs, then close."""
        if self._closing:
            return
        if not self.confirm_interrupt_show("quit Loop Station"):
            self.status_label.configure(text="Quit cancelled")
            return
        if not self.library.confirm_unsaved_show("quit Loop Station"):
            self.status_label.configure(text="Quit cancelled")
            return
        self._on_close()

    def _on_close(self):
        """Handle application shutdown (window close button)."""
        if self._closing:
            return
        if self.app_state is not None:
            self.notes_sidebar._commit_open_editors()
            if self._song_dialog is not None:
                self._song_dialog._flush_gain()
        if self.app_state is not None and not self.app_state.flush_save():
            self.restart_required = False
            self._on_save_failed(self.app_state.last_save_error)
            messagebox.showerror("Changes could not be saved",
                "Loop Station is staying open so you can retry saving.\n\n"
                + str(self.app_state.last_save_error), parent=self)
            return
        logger.info("Application closing")
        self._closing = True
        if self._position_after is not None:
            try:
                self.after_cancel(self._position_after)
            except (tk.TclError, ValueError):
                pass
            self._position_after = None
        if self.restart_required:
            # Theme restart: the new window turns sharing back on
            sharing = bool(self._web_server and self._web_server.running)
            try:
                preferences.save_preferences({self.PREF_RESTART_STATE: {"sharing": sharing}})
            except Exception:
                logger.debug("Could not remember the sharing state", exc_info=True)
        for dlg in (self._settings_dialog, self._song_dialog):
            if dlg is not None:
                try:
                    dlg.close()
                except Exception:
                    pass
        if self._mirror_flash_after is not None:
            try:
                self.after_cancel(self._mirror_flash_after)
            except (tk.TclError, ValueError):
                pass
            self._mirror_flash_after = None
        self._shutdown_backend()
        self.destroy()

    def request_restart(self):
        """
        Shut down exactly like _on_close, then flag main.py's lifecycle loop
        to create a fresh app (used by the theme selector).
        """
        logger.info("Restart requested")
        self.restart_required = True
        self._on_close()

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
        if self.app_state is None:
            return
        self.sel_start = start_frac * self.app_state.song_length
        self.sel_end = end_frac * self.app_state.song_length
        self.detector.enable_find(True)

    def _on_detector_mode_change(self, mode):
        """Handle switch between Loop and Cut finding."""
        self._stop_preview()
        if mode == "cut":
            self.detector.btn_find.configure(text="② Find Cuts", fg_color=COLOR_BTN_SKIP)
        else:
            self.detector.btn_find.configure(text="② Find Loops", fg_color=COLOR_BTN_WARNING)

    def _start_detection(self):
        """Route detection based on mode."""
        if self.app_state is None:
            return
        if self.detector.mode == "cut":
            self.app_state.run_smart_cut_detection(self.sel_start, self.sel_end)
        else:
            self.app_state.run_loop_detection(self.sel_start, self.sel_end)

    def _use_candidate(self, candidate):
        """Handle 'Use' button click from detector."""
        if self.app_state is None:
            return
        self._stop_preview()

        if self.detector.mode == "cut":
            # Add as a SKIP region
            self.app_state.add_skip(candidate.start, candidate.end)
            self.status_label.configure(text=f"Cut created: {candidate.duration:.2f}s removed")
        else:
            # Add as a new named vamp (the menu entry is "Add Vamp -> Auto-Detect")
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
        """
        Audition a candidate. Loops are previewed through preview_loop(),
        which never touches the vamp list; cuts play 2 s of pre-roll so the
        skip can be heard.
        """
        if self.app_state is None:
            return
        if self.detector.mode == "cut":
            self._stop_preview()
            preroll = max(0, candidate.start - 2.0)
            if self.app_state.preview_from(preroll):
                self.status_label.configure(text="Previewing cut transition...")
        else:
            # preview_loop() stops any earlier preview itself
            self.app_state.preview_loop(candidate.start, candidate.end)

    def _stop_preview(self):
        if self.app_state is not None and self.app_state.is_previewing():
            self.app_state.stop_preview()

    def _on_preview_changed(self, is_previewing):
        self.go_panel.set_previewing(is_previewing)
        if is_previewing:
            self.status_label.configure(text="▶ Previewing candidate loop (Space/Esc to stop)")
        self._refresh_exit_buttons()

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
        if self.app_state.live_mode:
            self._on_blocked_in_live_mode("update_selected_loop")
            return
        if self._lift_existing(self._vamp_modal):
            return

        self.app_state.select_loop(loop_idx)
        current_loop = self.app_state.loops[loop_idx]

        def _closed():
            if self._vamp_modal is modal:
                self._vamp_modal = None
            self._refresh_cue_sheet()

        modal = VampModal(
            parent=self,
            loop=current_loop,
            state_manager=self.app_state,
            on_close=_closed
        )
        self._vamp_modal = modal
        # CTkToplevel can come up withdrawn after heavy redraws; make sure
        # the modal is visible and on top once it has sized itself.
        def _show():
            try:
                if modal.winfo_exists() and modal.state() == "withdrawn":
                    modal.deiconify()
                modal.lift()
            except tk.TclError:
                pass
        self.after(250, _show)
