"""
Show sidebar for Loop Station: the running order (setlist) of the open show.

The sidebar is a view of ``StateManager.show_snapshot()`` plus the actions an
operator takes on it. Every change goes through StateManager (which enforces
the Live Mode rules); the sidebar re-renders from the 'show_changed' /
'current_entry_changed' / 'next_up_changed' events the app routes to it on
the Tk thread.

Rows:
- songs are numbered (cut songs and dividers get no number, same as the
  printed running order); the current song is filled, the next-up song has
  a green border and a NEXT badge (the GO panel's "next song" colour)
- dividers are section headers ("ACT 2")
- cut songs are struck through / dim with a CUT tag
- a segue shows "↪ segue" (the next song starts by itself)
- missing files are red with a "Locate…" button

Mouse:
- click: rehearsal = load that song; Live Mode = make it the next-up song
  (GO goes there next), it never loads
- press-drag-release reorders (insertion line; Esc cancels); locked in Live
- right-click (Windows: Button-3; macOS: Button-2 / Button-3 / Control-click)
  opens the row menu; the keyboard-friendly Move up / Move down are there too
- files / folders dropped from Finder / Explorer are added (tkinterdnd2,
  optional: see LoopStationApp)
"""

import os
import sys
import logging
import platform
import subprocess
import threading
import tkinter as tk
from typing import Callable, Dict, List, Optional

import customtkinter as ctk

from utils.fonts import FONT_UI
from config import (
    COLOR_BORDER, COLOR_HOVER,
    COLOR_BG_MEDIUM, COLOR_BG_LIGHT, COLOR_TEXT, COLOR_TEXT_DIM,
    COLOR_BTN_PRIMARY, SIDEBAR_WIDTH, SUPPORTED_FORMATS,
    PADDING_SMALL, PADDING_MEDIUM, COLOR_BTN_TEXT,
)
from utils import preferences
from utils.tooltip import ToolTip
from .go_panel import ACTION_COLORS

logger = logging.getLogger("LoopStation.ShowSidebar")

# The NEXT badge uses the GO panel's "GO: next song" colour so both agree
NEXT_COLOR = ACTION_COLORS["next_song"][0]
MISSING_COLOR = "#ff7b72"
SEGUE_COLOR = "#6db3ff"
INSERT_LINE_COLOR = "#58a6ff"
DRAG_THRESHOLD = 6           # pixels of mouse travel before a press becomes a drag

SHOW_EXTENSION = ".loopshow"
SHOW_FILETYPES = [("Loop Station show", "*" + SHOW_EXTENSION), ("All files", "*")]
AUDIO_FILETYPES = [("Audio files", " ".join("*" + ext for ext in SUPPORTED_FORMATS)),
                   ("All files", "*")]
RUNNING_ORDER_FILETYPES = [("Web page - print from the browser", "*.html"),
                           ("Spreadsheet (CSV)", "*.csv")]

EMPTY_SNAPSHOT = {
    "name": "", "file_path": None, "folder": None, "is_folder_derived": False,
    "dirty": False, "entries": [], "current_entry_id": None,
    "next_up_entry_id": None, "next_up_override": None,
}


def context_menu_sequences():
    """
    Mouse sequences that open a context menu. Windows / Linux: the right
    button is Button-3 (Button-2 is the middle button). macOS: the right
    button is Button-2 on Tk 8.6 and Button-3 on Tk 8.7+/9, and Control-click
    is the one-button-mouse / trackpad equivalent.
    """
    if sys.platform == "darwin":
        return ("<Button-2>", "<Button-3>", "<Control-Button-1>")
    return ("<Button-3>",)


def menu_labels(menu: tk.Menu) -> List[str]:
    """Labels of a tk.Menu's entries ('' for separators)."""
    labels = []
    end = menu.index("end")
    for i in range(0 if end is None else end + 1):
        try:
            labels.append(menu.entrycget(i, "label"))
        except tk.TclError:
            labels.append("")
    return labels


def expand_dropped_paths(paths) -> List[str]:
    """Dropped files and folders -> audio files (a folder adds its audio files, sorted)."""
    from backend.show import is_audio_file, list_audio_files
    out = []
    for p in paths:
        if not p:
            continue
        p = os.path.abspath(p)
        if os.path.isdir(p):
            out.extend(list_audio_files(p))
        elif is_audio_file(p):
            out.append(p)
    return out


class ShowSidebar(ctk.CTkFrame):
    """The show (running order) sidebar. ``app`` is the LoopStationApp."""

    ROW_BATCH = 60          # rows created per after() slice
    ROW_HEIGHT = 40
    DIVIDER_HEIGHT = 30

    def __init__(self, parent: tk.Widget, app=None,
                 on_song_select: Optional[Callable[[str], None]] = None, **kwargs):
        super().__init__(parent, width=SIDEBAR_WIDTH, fg_color=COLOR_BG_MEDIUM, **kwargs)
        self.app = app
        self.on_song_select = on_song_select       # fallback when there is no app
        self.on_locked_click: Optional[Callable[[str], None]] = None

        self.snapshot: dict = dict(EMPTY_SNAPSHOT)
        self.song_buttons: List[ctk.CTkFrame] = []   # one frame per entry, in order
        self._rows: Dict[str, dict] = {}             # entry id -> row widgets
        self._row_order: List[str] = []              # entry ids with a created row
        self._numbers: Dict[str, int] = {}
        self._structure_key = None
        self._selected_id: Optional[str] = None

        # While False (a song is loading) clicks are ignored. Live Mode lock:
        # clicks pick the next-up song instead of loading; edits are locked.
        self._enabled = True
        self._locked = False
        self._following = False     # backup following a main: no loading here

        self._load_generation = 0
        self._batch_after = None
        self._press = None          # {"id", "y"} between ButtonPress-1 and release
        self._drag = None           # {"id", "index"} while dragging a row
        self._insert_line = None
        self._dnd_enabled = False
        self._picker_thread = None
        self._picker_result = None

        self._font_title = ctk.CTkFont(family=FONT_UI, size=13)
        self._font_cut = ctk.CTkFont(family=FONT_UI, size=13, overstrike=True)

        self._create_widgets()
        self.render(self.snapshot)
        logger.debug("ShowSidebar initialized")

    # =========================================================================
    # WIDGETS
    # =========================================================================

    def _create_widgets(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=PADDING_MEDIUM, pady=(PADDING_MEDIUM, 0))

        self.name_label = ctk.CTkLabel(
            header, text="No show", font=(FONT_UI, 16, "bold"),
            text_color=COLOR_TEXT, anchor="w", justify="left",
            wraplength=SIDEBAR_WIDTH - 30)
        self.name_label.pack(fill="x")

        self.folder_label = ctk.CTkLabel(
            header, text="Open a folder or a show", font=(FONT_UI, 10),
            text_color=COLOR_TEXT_DIM, anchor="w", justify="left",
            wraplength=SIDEBAR_WIDTH - 30)
        self.folder_label.pack(fill="x")

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(fill="x", padx=PADDING_MEDIUM, pady=(PADDING_SMALL, 0))
        self.btn_menu = ctk.CTkButton(
            buttons, text="Show ▾", width=78, height=30, font=(FONT_UI, 12, "bold"),
            fg_color=COLOR_BG_LIGHT, hover_color=COLOR_HOVER, text_color=COLOR_TEXT,
            border_width=1, border_color=COLOR_BORDER,
            command=self._post_show_menu)
        self.btn_menu.pack(side="left")
        ToolTip(self.btn_menu, "New / Open / Save show, recent shows, print the running order")
        self.btn_save = ctk.CTkButton(
            buttons, text="Save", width=52, height=30, font=(FONT_UI, 12),
            fg_color=COLOR_BG_LIGHT, hover_color=COLOR_HOVER, text_color=COLOR_TEXT,
            border_width=1, border_color=COLOR_BORDER,
            command=self.save_show)
        self.btn_save.pack(side="left", padx=(4, 0))
        ToolTip(self.btn_save, "Save the show (also allowed in Live Mode)")
        self.btn_add = ctk.CTkButton(
            buttons, text="+ Add songs", width=100, height=30, font=(FONT_UI, 12, "bold"),
            fg_color=COLOR_BTN_PRIMARY, text_color=COLOR_BTN_TEXT,
            command=self.add_songs_dialog)
        self.btn_add.pack(side="right")
        ToolTip(self.btn_add, "Add audio files after the selected song (or drag files here)")
        # Compat name: the folder button of the old library
        self.btn_browse = self.btn_menu

        self._create_theme_selector()

        self.song_list_frame = ctk.CTkScrollableFrame(
            self, fg_color="transparent", scrollbar_button_color=COLOR_BG_LIGHT)
        self.song_list_frame.pack(fill="both", expand=True, padx=PADDING_SMALL, pady=PADDING_SMALL)

        self.empty_label = ctk.CTkLabel(
            self.song_list_frame, font=(FONT_UI, 12), text_color=COLOR_TEXT_DIM,
            justify="center", wraplength=SIDEBAR_WIDTH - 80,
            text="This show is empty.\n\nOpen a folder or show from Show ▾, "
                 "click + Add songs, or drag audio files here.")

        self.count_label = ctk.CTkLabel(self, text="0 songs", font=(FONT_UI, 10),
                                        text_color=COLOR_TEXT_DIM)
        self.count_label.pack(pady=PADDING_SMALL)

    def _create_theme_selector(self):
        from themes import THEMES, DEFAULT_THEME
        from utils.preferences import set_theme_preference, get_theme_preference

        def change_theme(new_theme):
            app_instance = self.winfo_toplevel()
            confirm = getattr(app_instance, 'confirm_interrupt_show', None)
            if (confirm is not None and not confirm("restart Loop Station to change the theme")) \
                    or not self.confirm_unsaved_show("restart Loop Station"):
                previous = get_theme_preference()
                self.theme_menu.set(previous if previous in THEMES else DEFAULT_THEME)
                return
            set_theme_preference(new_theme)
            if hasattr(app_instance, 'request_restart'):
                app_instance.request_restart()
            else:
                logger.warning("Top-level window cannot restart; theme applies on next launch")

        theme_frame = ctk.CTkFrame(self, fg_color="transparent")
        theme_frame.pack(fill="x", padx=PADDING_MEDIUM, pady=(PADDING_SMALL, 0))
        ctk.CTkLabel(theme_frame, text="Theme", font=(FONT_UI, 11),
                     text_color=COLOR_TEXT_DIM).pack(side="left")
        self.theme_menu = ctk.CTkOptionMenu(
            theme_frame, values=sorted(THEMES.keys()), width=120, height=24,
            font=(FONT_UI, 10), command=change_theme,
            fg_color=COLOR_BG_LIGHT, button_color=COLOR_BG_LIGHT,
            button_hover_color=COLOR_HOVER, text_color=COLOR_TEXT,
            dropdown_fg_color=COLOR_BG_LIGHT, dropdown_text_color=COLOR_TEXT,
            dropdown_hover_color=COLOR_HOVER)
        current_theme = get_theme_preference()
        self.theme_menu.set(current_theme if current_theme in THEMES else DEFAULT_THEME)
        self.theme_menu.pack(side="right")
        ToolTip(self.theme_menu, "Change color theme (restarts app)")

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _state(self):
        return getattr(self.app, 'app_state', None) if self.app is not None else None

    def _status(self, text: str, color: Optional[str] = None):
        label = getattr(self.app, 'status_label', None)
        if label is None:
            return
        try:
            label.configure(text=text, text_color=color or COLOR_TEXT)
        except tk.TclError:
            pass

    def _report_blocked(self, what: str):
        if self.on_locked_click is not None:
            self.on_locked_click(what)

    def _locked_out(self, what: str) -> bool:
        """True (and the Live Mode message) when an edit is locked."""
        if self._locked:
            self._report_blocked(what)
            return True
        return False

    @property
    def entries(self) -> List[dict]:
        return list(self.snapshot.get("entries") or [])

    def _entry(self, entry_id) -> Optional[dict]:
        for e in self.snapshot.get("entries") or []:
            if e.get("id") == entry_id:
                return e
        return None

    def _index_of(self, entry_id) -> int:
        for i, e in enumerate(self.snapshot.get("entries") or []):
            if e.get("id") == entry_id:
                return i
        return -1

    @property
    def current_entry_id(self):
        return self.snapshot.get("current_entry_id")

    @property
    def next_up_entry_id(self):
        return self.snapshot.get("next_up_entry_id")

    # Old LibrarySidebar names (tests, other code)
    @property
    def current_folder(self) -> str:
        return self.snapshot.get("folder") or ""

    @property
    def songs(self) -> List[str]:
        return [os.path.basename(e["path"]) for e in self.entries
                if e.get("kind") == "song" and e.get("path")]

    @staticmethod
    def folder_display_name(folder_path: str) -> str:
        """Last path component; the macOS picker returns paths ending in "/"."""
        trimmed = folder_path.rstrip("/\\" + os.sep)
        return os.path.basename(trimmed) or folder_path

    def _default_dir(self) -> str:
        snap = self.snapshot
        for candidate in (snap.get("folder"),
                          os.path.dirname(snap.get("file_path") or "")):
            if candidate and os.path.isdir(candidate):
                return candidate
        for e in self.entries:
            d = os.path.dirname(e.get("path") or "")
            if d and os.path.isdir(d):
                return d
        return os.path.expanduser("~")

    def _insert_index(self) -> Optional[int]:
        """Where new songs go: after the selected row, else after the current one, else the end."""
        for entry_id in (self._selected_id, self.current_entry_id):
            i = self._index_of(entry_id)
            if i >= 0:
                return i + 1
        return None

    # ---- dialogs (overridable in tests) --------------------------------------

    def _ask_text(self, title: str, text: str, initial: str = "") -> Optional[str]:
        from .widgets import NamePrompt
        return NamePrompt(self.winfo_toplevel(), title, text, initial=initial).get_input()

    def _ask_yes_no_cancel(self, title: str, message: str) -> Optional[bool]:
        from tkinter import messagebox
        try:
            return messagebox.askyesnocancel(title, message, parent=self.winfo_toplevel())
        except tk.TclError:
            return None

    def _ask_open_paths(self, title, filetypes, multiple=False, initialdir=None):
        """File-open dialog: a list of paths (empty when cancelled)."""
        from tkinter import filedialog
        # parent= is REQUIRED on Windows: without it Tk flashes a stray root window
        kwargs = dict(title=title, filetypes=filetypes, parent=self.winfo_toplevel(),
                      initialdir=initialdir or self._default_dir())
        if multiple:
            result = filedialog.askopenfilenames(**kwargs)
            if isinstance(result, str):
                result = self.tk.splitlist(result) if result else ()
            return [p for p in result if p]
        result = filedialog.askopenfilename(**kwargs)
        return [result] if result else []

    def _ask_save_path(self, title, filetypes, defaultextension, initialfile, initialdir=None):
        from tkinter import filedialog
        return filedialog.asksaveasfilename(
            title=title, filetypes=filetypes, defaultextension=defaultextension,
            initialfile=initialfile, initialdir=initialdir or self._default_dir(),
            parent=self.winfo_toplevel()) or None

    # =========================================================================
    # ENABLE / LOCK
    # =========================================================================

    def set_enabled(self, enabled: bool):
        """False while a song is loading: clicks are ignored."""
        self._enabled = bool(enabled)
        self._apply_enabled()

    def set_locked(self, locked: bool):
        """Live Mode: clicks choose the next-up song; edits and opening shows are locked."""
        self._locked = bool(locked)
        if self._locked:
            self.cancel_drag()
        self._apply_enabled()

    def set_following(self, following: bool):
        """Backup following the main: rows do not load songs (it would race the main)."""
        self._following = bool(following)

    @property
    def accepting_clicks(self) -> bool:
        """True when a click loads a song (not loading, not in Live Mode)."""
        return self._enabled and not self._locked

    def _apply_enabled(self):
        editable = "normal" if self.accepting_clicks else "disabled"
        for widget in (self.btn_add, self.theme_menu):
            try:
                widget.configure(state=editable)
            except (tk.TclError, ValueError):
                pass
        try:
            self.btn_menu.configure(state="normal" if self._enabled else "disabled")
        except (tk.TclError, ValueError):
            pass
        cursor = "hand2" if self._enabled else "watch"
        for row in self.song_buttons:
            try:
                row.configure(cursor=cursor)
            except tk.TclError:
                pass

    # =========================================================================
    # RENDERING
    # =========================================================================

    @staticmethod
    def _structure(entries) -> tuple:
        return tuple((e.get("id"), e.get("kind"), e.get("title"), bool(e.get("cut")),
                      bool(e.get("segue")), bool(e.get("missing")), e.get("path"))
                     for e in entries)

    @staticmethod
    def compute_numbers(entries) -> Dict[str, int]:
        """Running-order numbers: songs that are not cut (dividers / cut songs get none)."""
        numbers, n = {}, 0
        for e in entries:
            if e.get("kind") == "song" and not e.get("cut"):
                n += 1
                numbers[e["id"]] = n
        return numbers

    def render(self, snapshot: Optional[dict]):
        """Show a show_snapshot(); rows are rebuilt only when the list itself changed."""
        snap = dict(EMPTY_SNAPSHOT)
        snap.update(snapshot or {})
        self.snapshot = snap
        if self._selected_id is not None and self._entry(self._selected_id) is None:
            self._selected_id = None
        self._update_header()
        key = self._structure(snap["entries"])
        if key != self._structure_key:
            self._structure_key = key
            self._rebuild_rows()
        else:
            self._apply_highlight()

    def refresh_from_state(self):
        st = self._state()
        if st is not None:
            self.render(st.show_snapshot())

    def set_current_entry(self, entry_id):
        self.snapshot["current_entry_id"] = entry_id
        self._apply_highlight()

    def set_next_up_entry(self, entry_id):
        self.snapshot["next_up_entry_id"] = entry_id
        self._apply_highlight()

    def _update_header(self):
        snap = self.snapshot
        entries = snap["entries"]
        name = snap.get("name") or "Untitled show"
        if not entries and not snap.get("file_path") and not snap.get("folder") \
                and not snap.get("dirty"):
            title = "No show"
        else:
            title = name + ("  •" if snap.get("dirty") else "")
        self.name_label.configure(text=title)
        if snap.get("file_path"):
            sub = f"{os.path.basename(snap['file_path'])}"
        elif snap.get("folder"):
            sub = f"Folder · {self.folder_display_name(snap['folder'])}  (order remembered)"
        elif entries:
            sub = "Not saved yet - Show ▾ › Save As…"
        else:
            sub = "Open a folder or a show"
        self.folder_label.configure(text=sub)
        songs = [e for e in entries if e.get("kind") == "song"]
        cut = sum(1 for e in songs if e.get("cut"))
        missing = sum(1 for e in songs if e.get("missing"))
        text = f"{len(songs)} song" + ("" if len(songs) == 1 else "s")
        if cut:
            text += f" · {cut} cut"
        if missing:
            text += f" · {missing} missing"
        self.count_label.configure(text=text, text_color=MISSING_COLOR if missing else COLOR_TEXT_DIM)

    def _clear_rows(self):
        self._load_generation += 1
        if self._batch_after is not None:
            try:
                self.after_cancel(self._batch_after)
            except (tk.TclError, ValueError):
                pass
            self._batch_after = None
        self.cancel_drag()
        for row in self.song_buttons:
            try:
                row.destroy()
            except tk.TclError:
                pass
        self.song_buttons.clear()
        self._rows.clear()
        self._row_order.clear()

    def _rebuild_rows(self):
        self._clear_rows()
        entries = self.snapshot["entries"]
        self._numbers = self.compute_numbers(entries)
        if entries:
            self.empty_label.pack_forget()
        else:
            self.empty_label.pack(fill="x", pady=30)
        self._create_rows_batch(self._load_generation, 0)

    @property
    def rows_pending(self) -> bool:
        """True while rows are still being created (big shows load in slices)."""
        return len(self.song_buttons) < len(self.snapshot["entries"])

    def _create_rows_batch(self, generation, start):
        self._batch_after = None
        if generation != self._load_generation:
            return
        entries = self.snapshot["entries"]
        end = min(len(entries), start + self.ROW_BATCH)
        for e in entries[start:end]:
            self._create_row(e)
        if end < len(entries):
            try:
                self._batch_after = self.after(1, lambda: self._create_rows_batch(generation, end))
            except tk.TclError:
                self._batch_after = None

    def _bind_row(self, widget, entry_id):
        widget.bind("<ButtonPress-1>", lambda e, i=entry_id: self._on_press(e, i))
        widget.bind("<B1-Motion>", self._on_motion)
        widget.bind("<ButtonRelease-1>", self._on_release)
        for seq in context_menu_sequences():
            widget.bind(seq, lambda e, i=entry_id: self._on_context_menu(e, i))

    def _create_row(self, e: dict):
        entry_id = e["id"]
        cursor = "hand2" if self._enabled else "watch"
        info = {"kind": e.get("kind")}
        if e.get("kind") == "divider":
            row = ctk.CTkFrame(self.song_list_frame, fg_color="transparent",
                               height=self.DIVIDER_HEIGHT, corner_radius=0, cursor=cursor)
            row.pack(fill="x", pady=(8, 1))
            row.pack_propagate(False)
            title = ctk.CTkLabel(row, text=(e.get("title") or "").upper(), anchor="w",
                                 font=(FONT_UI, 12, "bold"), text_color=COLOR_TEXT_DIM)
            title.pack(side="left", fill="x", expand=True, padx=(6, 4))
            rule = ctk.CTkFrame(row, height=2, fg_color=COLOR_TEXT_DIM, corner_radius=0)
            rule.place(relx=0, rely=1.0, relwidth=1.0, anchor="sw")
            widgets = [row, title]
            info.update(row=row, title=title, number=None, next=None)
        else:
            cut, missing = bool(e.get("cut")), bool(e.get("missing"))
            row = ctk.CTkFrame(self.song_list_frame, fg_color="transparent",
                               height=self.ROW_HEIGHT, corner_radius=6, border_width=0,
                               cursor=cursor)
            row.pack(fill="x", pady=1)
            row.pack_propagate(False)
            num = self._numbers.get(entry_id)
            number = ctk.CTkLabel(row, text="" if num is None else str(num), width=30,
                                  anchor="e", font=(FONT_UI, 15, "bold"),
                                  text_color=COLOR_TEXT_DIM if cut else COLOR_TEXT)
            number.pack(side="left", padx=(4, 6))
            widgets = [row, number]
            # Right-hand tags (packed right-to-left)
            next_badge = ctk.CTkLabel(row, text=" NEXT ", font=(FONT_UI, 11, "bold"),
                                      fg_color=NEXT_COLOR, text_color="#ffffff",
                                      corner_radius=4, height=22)
            widgets.append(next_badge)
            if missing:
                btn = ctk.CTkButton(row, text="Locate…", width=64, height=24,
                                    font=(FONT_UI, 11, "bold"), fg_color=MISSING_COLOR,
                                    text_color="#ffffff", hover_color="#d65a52",
                                    command=lambda i=entry_id: self.locate_entry(i))
                btn.pack(side="right", padx=(2, 6))
                info["locate"] = btn
            if cut:
                tag = ctk.CTkLabel(row, text=" CUT ", font=(FONT_UI, 10, "bold"),
                                   text_color=COLOR_TEXT_DIM, fg_color=COLOR_BG_LIGHT,
                                   corner_radius=4, height=20)
                tag.pack(side="right", padx=(2, 6))
                widgets.append(tag)
            if e.get("segue"):
                seg = ctk.CTkLabel(row, text="↪ segue", font=(FONT_UI, 11, "bold"),
                                   text_color=SEGUE_COLOR)
                seg.pack(side="right", padx=(2, 6))
                widgets.append(seg)
            title = ctk.CTkLabel(
                row, text=e.get("title") or "", anchor="w",
                font=self._font_cut if cut else self._font_title,
                text_color=MISSING_COLOR if missing else (COLOR_TEXT_DIM if cut else COLOR_TEXT))
            title.pack(side="left", fill="x", expand=True, padx=(0, 4))
            widgets.append(title)
            info.update(row=row, title=title, number=number, next=next_badge,
                        cut=cut, missing=missing)
            tip = e.get("title") or ""
            if e.get("path"):
                tip += "\n" + e["path"] + ("\n(file not found)" if missing else "")
            ToolTip(row, tip)
            row.bind("<Enter>", lambda _e, i=entry_id: self._on_row_hover(i, True), add="+")
            row.bind("<Leave>", lambda _e, i=entry_id: self._on_row_hover(i, False), add="+")
        for w in widgets:
            self._bind_row(w, entry_id)
        info["visible_next"] = False
        self._rows[entry_id] = info
        self._row_order.append(entry_id)
        self.song_buttons.append(row)
        self._style_row(entry_id)

    def _style_row(self, entry_id):
        info = self._rows.get(entry_id)
        if info is None or info["kind"] != "song":
            return
        current = entry_id == self.current_entry_id
        is_next = entry_id == self.next_up_entry_id and not current
        row, title, number = info["row"], info["title"], info["number"]
        try:
            if current:
                row.configure(fg_color=COLOR_BTN_PRIMARY)
                title.configure(text_color="#ffffff")
                number.configure(text_color="#ffffff")
            else:
                row.configure(fg_color="transparent")
                title.configure(text_color=MISSING_COLOR if info["missing"]
                                else (COLOR_TEXT_DIM if info["cut"] else COLOR_TEXT))
                number.configure(text_color=COLOR_TEXT_DIM if info["cut"] else COLOR_TEXT)
            row.configure(border_width=2 if is_next else 0, border_color=NEXT_COLOR)
            if is_next and not info["visible_next"]:
                info["next"].pack(side="right", padx=(2, 6), before=title)
            elif not is_next and info["visible_next"]:
                info["next"].pack_forget()
            info["visible_next"] = is_next
        except tk.TclError:
            pass

    def _apply_highlight(self):
        for entry_id in self._row_order:
            self._style_row(entry_id)

    def _on_row_hover(self, entry_id, entering: bool):
        if entry_id == self.current_entry_id or self._drag is not None:
            return
        info = self._rows.get(entry_id)
        if info is None:
            return
        try:
            info["row"].configure(fg_color=COLOR_BG_LIGHT if entering else "transparent")
        except tk.TclError:
            pass

    def row_state(self, entry_id) -> dict:
        """What a row shows (tests / diagnostics)."""
        info = self._rows.get(entry_id)
        if info is None:
            return {}
        out = {"kind": info["kind"], "title": info["title"].cget("text")}
        if info["kind"] == "song":
            out.update(number=info["number"].cget("text"), next=info["visible_next"],
                       current=entry_id == self.current_entry_id,
                       cut=info["cut"], missing=info["missing"],
                       locate="locate" in info)
        return out

    # =========================================================================
    # CLICK / DRAG
    # =========================================================================

    def _on_press(self, event, entry_id):
        self._press = {"id": entry_id, "y": getattr(event, "y_root", 0), "warned": False}
        self._drag = None

    def _on_motion(self, event):
        p = self._press
        if p is None:
            return
        y = getattr(event, "y_root", 0)
        if self._drag is None:
            if abs(y - p["y"]) < DRAG_THRESHOLD:
                return
            if self._locked:
                if not p["warned"]:
                    p["warned"] = True
                    self._report_blocked("move_entry")
                return
            if not self._enabled:
                return
            self._drag = {"id": p["id"], "index": None}
        index = self._drop_index_at(y)
        self._drag["index"] = index
        self._show_insert_line(index)

    def _on_release(self, event=None):
        p, self._press = self._press, None
        drag, self._drag = self._drag, None
        self._hide_insert_line()
        if p is None:
            return
        if drag is not None:
            if drag["index"] is not None:
                self._finish_drag(drag["id"], drag["index"])
            return
        if p["warned"]:
            return
        self.on_row_click(p["id"])

    def cancel_drag(self) -> bool:
        """Esc during a drag: drop it. True if a drag was in progress."""
        was = self._drag is not None
        self._press = None
        self._drag = None
        self._hide_insert_line()
        if was:
            self._status("Move cancelled")
        return was

    @property
    def dragging(self) -> bool:
        return self._drag is not None

    def _row_bounds(self) -> List[tuple]:
        """(top, bottom) screen y of each created row, in show order ([] if not on screen)."""
        bounds = []
        for row in self.song_buttons:
            try:
                if not row.winfo_viewable():
                    return []
                top = row.winfo_rooty()
                bounds.append((top, top + row.winfo_height()))
            except tk.TclError:
                return []
        return bounds

    def _drop_index_at(self, y_root) -> Optional[int]:
        """Insert-before index for a pointer at ``y_root`` (len = the end; None = unknown)."""
        if y_root is None:
            return None
        bounds = self._row_bounds()
        if not bounds:
            return None
        for i, (top, bottom) in enumerate(bounds):
            if y_root < (top + bottom) / 2.0:
                return i
        return len(bounds)

    def _finish_drag(self, entry_id, insert_before: int):
        """Dropped before row ``insert_before``: move_entry takes the FINAL index."""
        src = self._index_of(entry_id)
        if src < 0:
            return False
        new_index = insert_before - 1 if insert_before > src else insert_before
        if new_index == src:
            return False
        if self._locked_out("move_entry"):
            return False
        st = self._state()
        return bool(st is not None and st.move_entry(entry_id, new_index))

    def _show_insert_line(self, index):
        if index is None or not self.song_buttons:
            self._hide_insert_line()
            return
        try:
            if index < len(self.song_buttons):
                y = self.song_buttons[index].winfo_y() - 2
            else:
                last = self.song_buttons[-1]
                y = last.winfo_y() + last.winfo_height()
            if self._insert_line is None:
                self._insert_line = tk.Frame(self.song_list_frame, height=3,
                                             bg=INSERT_LINE_COLOR, bd=0, highlightthickness=0)
            self._insert_line.place(x=0, y=max(0, y), relwidth=1.0)
            self._insert_line.lift()
        except tk.TclError:
            pass

    def _hide_insert_line(self):
        if self._insert_line is not None:
            try:
                self._insert_line.place_forget()
            except tk.TclError:
                pass

    def on_row_click(self, entry_id):
        """Rehearsal: load the song. Live Mode: make it the next-up song (never loads)."""
        e = self._entry(entry_id)
        if e is None:
            return
        self._selected_id = entry_id
        if self._locked:
            if e.get("kind") == "song":
                self.set_next(entry_id)
            return
        if not self._enabled or e.get("kind") != "song":
            return
        if self._following:
            self._status(getattr(self.app, "FOLLOWING_TEXT", "Following main"), "#f0a020")
            return
        if e.get("missing"):
            self._status(f"File not found: {e.get('title')} - use Locate… to find it",
                         MISSING_COLOR)
            return
        self.load_entry(entry_id)

    def load_entry(self, entry_id):
        e = self._entry(entry_id)
        if e is None or e.get("kind") != "song":
            return
        loader = getattr(self.app, 'load_show_entry', None)
        if loader is not None:
            loader(entry_id)
        elif self.on_song_select is not None:
            self.on_song_select(e["path"])

    # =========================================================================
    # ENTRY ACTIONS (context menu)
    # =========================================================================

    def set_next(self, entry_id) -> bool:
        """Make ``entry_id`` the song GO goes to next. Allowed in Live Mode."""
        st, e = self._state(), self._entry(entry_id)
        if st is None or e is None:
            return False
        if e.get("kind") != "song":
            return False
        if e.get("missing"):
            self._status(f"Cannot set next: the file of {e.get('title')} is missing", MISSING_COLOR)
            return False
        if entry_id == self.current_entry_id:
            self._status(f"{e.get('title')} is the current song - Play replays it")
            return False
        if st.set_next_up(entry_id):
            self._status(f"Next up: {e.get('title')} — press GO", NEXT_COLOR)
            return True
        return False

    def clear_next(self) -> bool:
        st = self._state()
        if st is None:
            return False
        ok = st.set_next_up(None)
        if ok:
            self._status("Next up: back to the running order")
        return ok

    def rename_entry(self, entry_id):
        e = self._entry(entry_id)
        if e is None or self._locked_out("rename_entry"):
            return False
        text = self._ask_text("Rename", "New title (leave empty to use the file name):",
                              initial=e.get("title") or "")
        if text is None:
            return False
        return bool(self._state().rename_entry(entry_id, text))

    def duplicate_entry(self, entry_id):
        if self._locked_out("duplicate_entry"):
            return None
        new_id = self._state().duplicate_entry(entry_id)
        if new_id:
            self._selected_id = new_id
            self._status("Reprise added below")
        return new_id

    def toggle_cut(self, entry_id):
        e = self._entry(entry_id)
        if e is None or self._locked_out("set_entry_cut"):
            return False
        return bool(self._state().set_entry_cut(entry_id, not e.get("cut")))

    def toggle_segue(self, entry_id):
        e = self._entry(entry_id)
        if e is None or self._locked_out("set_entry_segue"):
            return False
        return bool(self._state().set_entry_segue(entry_id, not e.get("segue")))

    def insert_divider_above(self, entry_id):
        if self._locked_out("add_divider"):
            return None
        index = self._index_of(entry_id)
        title = self._ask_text("Insert divider", "Section title (for example ACT 2):",
                               initial="ACT 2")
        if title is None:
            return None
        return self._state().add_divider(title, max(0, index) if index >= 0 else None)

    def add_divider_dialog(self):
        if self._locked_out("add_divider"):
            return None
        title = self._ask_text("Add divider", "Section title (for example ACT 1):", initial="ACT 1")
        if title is None:
            return None
        return self._state().add_divider(title, self._insert_index())

    def move_up(self, entry_id):
        return self._move_by(entry_id, -1)

    def move_down(self, entry_id):
        return self._move_by(entry_id, +1)

    def _move_by(self, entry_id, delta):
        if self._locked_out("move_entry"):
            return False
        i = self._index_of(entry_id)
        if i < 0:
            return False
        target = i + delta
        if not (0 <= target < len(self.entries)):
            return False
        return bool(self._state().move_entry(entry_id, target))

    def remove_entry(self, entry_id):
        if self._locked_out("remove_entry"):
            return False
        return bool(self._state().remove_entry(entry_id))

    def locate_entry(self, entry_id):
        """Missing file: pick it again (other missing files in that folder are relinked too)."""
        e = self._entry(entry_id)
        if e is None or self._locked_out("relink_entry"):
            return []
        old_dir = os.path.dirname(e.get("path") or "")
        paths = self._ask_open_paths(f"Locate “{e.get('title')}”", AUDIO_FILETYPES,
                                     initialdir=old_dir if os.path.isdir(old_dir) else None)
        if not paths:
            return []
        ids = self._state().relink_entry(entry_id, paths[0]) or []
        if ids:
            extra = len(ids) - 1
            self._status(f"Relinked {e.get('title')}" + (f" (+{extra} more found)" if extra > 0 else ""))
        else:
            self._status("That file could not be used", MISSING_COLOR)
        return ids

    # ---- context menu ---------------------------------------------------------

    def build_context_menu(self, entry_id) -> Optional[tk.Menu]:
        e = self._entry(entry_id)
        if e is None:
            return None
        menu = tk.Menu(self, tearoff=0)
        edit = "disabled" if self._locked else "normal"
        song = e.get("kind") == "song"
        if song:
            can_next = not e.get("missing") and entry_id != self.current_entry_id
            menu.add_command(label="Set as next", command=lambda: self.set_next(entry_id),
                             state="normal" if can_next else "disabled")
            st = self._state()
            if getattr(st, "next_up_override", None):
                menu.add_command(label="Back to running order (clear next)",
                                 command=self.clear_next)
            menu.add_separator()
        menu.add_command(label="Rename…", command=lambda: self.rename_entry(entry_id), state=edit)
        if song:
            menu.add_command(label="Duplicate (reprise)",
                             command=lambda: self.duplicate_entry(entry_id), state=edit)
            menu.add_command(label="Restore (un-cut)" if e.get("cut") else "Mark as cut",
                             command=lambda: self.toggle_cut(entry_id), state=edit)
            menu.add_command(label="Segue into next: turn off" if e.get("segue")
                             else "Segue into next: turn on",
                             command=lambda: self.toggle_segue(entry_id), state=edit)
        menu.add_command(label="Insert divider above…",
                         command=lambda: self.insert_divider_above(entry_id), state=edit)
        i = self._index_of(entry_id)
        menu.add_command(label="Move up", command=lambda: self.move_up(entry_id),
                         state=edit if i > 0 else "disabled")
        menu.add_command(label="Move down", command=lambda: self.move_down(entry_id),
                         state=edit if 0 <= i < len(self.entries) - 1 else "disabled")
        if song and e.get("missing"):
            menu.add_command(label="Locate file…", command=lambda: self.locate_entry(entry_id),
                             state=edit)
        menu.add_separator()
        menu.add_command(label="Remove from show", command=lambda: self.remove_entry(entry_id),
                         state=edit)
        return menu

    def _on_context_menu(self, event, entry_id):
        self._press = None          # a Control-click must not also count as a click
        self._selected_id = entry_id
        menu = self.build_context_menu(entry_id)
        if menu is None:
            return "break"
        self._popup(menu, event)
        return "break"

    def _popup(self, menu, event=None):
        try:
            x = getattr(event, "x_root", None)
            y = getattr(event, "y_root", None)
            if x is None or y is None:
                x, y = self.winfo_pointerxy()
            menu.tk_popup(x, y)
        except tk.TclError:
            pass
        finally:
            try:
                menu.grab_release()
            except tk.TclError:
                pass

    # =========================================================================
    # SHOW MENU (header)
    # =========================================================================

    def build_show_menu(self) -> tk.Menu:
        live = "disabled" if self._locked else "normal"
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="New Show", command=self.new_show, state=live)
        menu.add_command(label="Open Show…", command=self.open_show_dialog, state=live)
        menu.add_command(label="Open Folder…", command=self.open_folder_dialog, state=live)
        recent = tk.Menu(menu, tearoff=0)
        refs = preferences.get_recent_shows()
        for ref in refs:
            icon = "📄" if ref["kind"] == "file" else "📁"
            name = (os.path.splitext(os.path.basename(ref["path"]))[0] if ref["kind"] == "file"
                    else self.folder_display_name(ref["path"]))
            recent.add_command(label=f"{icon} {name}", command=lambda r=ref: self.open_recent(r))
        if not refs:
            recent.add_command(label="(none yet)", state="disabled")
        menu.add_cascade(label="Recent", menu=recent, state=live)
        menu.add_separator()
        menu.add_command(label="Save", command=self.save_show)
        menu.add_command(label="Save As…", command=self.save_show_as)
        menu.add_separator()
        menu.add_command(label="Add songs…", command=self.add_songs_dialog, state=live)
        menu.add_command(label="Add divider…", command=self.add_divider_dialog, state=live)
        menu.add_separator()
        menu.add_command(label="Print running order…", command=self.print_running_order)
        return menu

    def _post_show_menu(self):
        menu = self.build_show_menu()
        try:
            x = self.btn_menu.winfo_rootx()
            y = self.btn_menu.winfo_rooty() + self.btn_menu.winfo_height()
        except tk.TclError:
            x = y = None

        class _At:
            x_root, y_root = x, y
        self._popup(menu, _At if x is not None else None)

    # ---- unsaved changes --------------------------------------------------------

    def has_unsaved_show(self) -> bool:
        """An explicit (or never saved, non-empty) show with unsaved edits."""
        st = self._state()
        if st is None:
            return False
        snap = st.show_snapshot()
        if not snap.get("dirty") or snap.get("is_folder_derived"):
            return False
        return bool(snap.get("file_path") or snap.get("entries"))

    def confirm_unsaved_show(self, what: str = "close this show") -> bool:
        """Save / Don't save / Cancel for a dirty show. True = go ahead."""
        if not self.has_unsaved_show():
            return True
        name = self._state().show_snapshot().get("name") or "Untitled show"
        answer = self._ask_yes_no_cancel(
            "Save the show?",
            f"The show “{name}” has unsaved changes.\n\nSave them before you {what}?")
        if answer is None:
            return False
        if answer is False:
            return True
        return self.save_show()

    # ---- open / new -----------------------------------------------------------

    def new_show(self):
        if self._locked_out("new_show") or not self.confirm_unsaved_show("start a new show"):
            return False
        name = self._ask_text("New show", "Show name:", initial="Untitled show")
        if name is None:
            return False
        ok = bool(self._state().new_show(name.strip() or "Untitled show"))
        if ok:
            self._selected_id = None
        return ok

    def open_show_dialog(self):
        if self._locked_out("open_show") or not self.confirm_unsaved_show("open another show"):
            return False
        paths = self._ask_open_paths("Open show", SHOW_FILETYPES)
        if not paths:
            return False
        return self.open_show(paths[0])

    def open_show(self, path) -> bool:
        st = self._state()
        if st is None or self._locked_out("open_show"):
            return False
        ok = bool(st.open_show(path))
        if ok:
            self._selected_id = None
            self.refresh_from_state()
            self._status(f"Opened show: {self.snapshot.get('name')}")
        return ok

    def open_folder(self, folder) -> bool:
        """The folder becomes a show whose order is remembered automatically."""
        st = self._state()
        if st is None or self._locked_out("open_folder"):
            return False
        if not self.confirm_unsaved_show("open another show"):
            return False
        if not folder or not os.path.isdir(folder):
            self._status(f"Not a folder: {folder}", MISSING_COLOR)
            return False
        ok = bool(st.open_folder(folder))
        if ok:
            self._selected_id = None
            self.refresh_from_state()
        return ok

    # Old LibrarySidebar name
    load_folder = open_folder

    def open_recent(self, ref) -> bool:
        kind, path = ref.get("kind"), ref.get("path")
        exists = os.path.isfile(path) if kind == "file" else os.path.isdir(path)
        if not exists:
            self._status(f"Not found any more: {path}", MISSING_COLOR)
            preferences.remove_recent_show(kind, path)
            return False
        if kind == "file":
            if self._locked_out("open_show") or not self.confirm_unsaved_show("open another show"):
                return False
            return self.open_show(path)
        return self.open_folder(path)

    def open_folder_dialog(self):
        if self._locked_out("open_folder"):
            return
        if platform.system() == "Darwin":
            # The native Tk folder dialog can hang the main thread on macOS, so
            # the AppleScript chooser runs in a worker thread and is polled.
            self.btn_menu.configure(state="disabled")
            self._picker_result = None
            self._picker_thread = threading.Thread(target=self._worker_browse_mac, daemon=True)
            self._picker_thread.start()
            self.after(100, self._check_picker_thread)
            return
        from tkinter import filedialog
        folder = filedialog.askdirectory(title="Open a folder of songs as a show",
                                         initialdir=self._default_dir(),
                                         parent=self.winfo_toplevel())
        if folder:
            self.open_folder(folder)

    def _worker_browse_mac(self):
        folder = None
        try:
            script = 'return POSIX path of (choose folder with prompt "Open a folder of songs as a show")'
            result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True)
            if result.returncode == 0:
                folder = result.stdout.strip()
        except Exception as e:
            logger.error(f"macOS picker failed: {e}")
        self._picker_result = folder or "CANCELLED"

    def _check_picker_thread(self):
        if self._picker_thread and self._picker_thread.is_alive():
            self.after(100, self._check_picker_thread)
            return
        result = self._picker_result
        self._picker_thread = None
        self._apply_enabled()
        if result and result != "CANCELLED":
            self.open_folder(result)

    # ---- save ------------------------------------------------------------------

    def save_show(self) -> bool:
        """Save; a folder-derived / never-saved show asks for a file name (Save As)."""
        st = self._state()
        if st is None:
            return False
        if st.save_show():
            self._status(f"✓ Show saved: {os.path.basename(st.show_snapshot().get('file_path') or '')}")
            return True
        if st.show_snapshot().get("file_path") is None:
            return self.save_show_as()
        return False        # the failure arrives as 'show_save_failed'

    def save_show_as(self) -> bool:
        st = self._state()
        if st is None:
            return False
        snap = st.show_snapshot()
        name = snap.get("name") or "Untitled show"
        path = self._ask_save_path("Save show as", SHOW_FILETYPES, SHOW_EXTENSION,
                                   initialfile=name + SHOW_EXTENSION)
        if not path:
            return False
        if not os.path.splitext(path)[1]:
            path += SHOW_EXTENSION
        if st.save_show(path):
            self._status(f"✓ Show saved: {os.path.basename(path)}")
            return True
        return False

    # ---- add songs ----------------------------------------------------------------

    def add_songs_dialog(self):
        if self._locked_out("add_songs"):
            return []
        paths = self._ask_open_paths("Add songs to the show", AUDIO_FILETYPES, multiple=True)
        if not paths:
            return []
        return self.add_paths(paths, self._insert_index())

    def add_paths(self, paths, index=None) -> List[str]:
        """Add audio files / folders (expanded) at ``index`` (None = end)."""
        if self._locked_out("add_songs"):
            return []
        files = expand_dropped_paths(paths)
        if not files:
            self._status("No audio files to add", MISSING_COLOR)
            return []
        ids = self._state().add_songs(files, index) or []
        if ids:
            self._selected_id = ids[-1]
            self._status(f"Added {len(ids)} song" + ("" if len(ids) == 1 else "s"))
        return ids

    # ---- drag and drop from Finder / Explorer ----------------------------------------

    def enable_dnd(self) -> bool:
        """Register the sidebar as a file drop target (needs tkdnd loaded on the root)."""
        try:
            from tkinterdnd2 import DND_FILES
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_dnd_drop)
            self.dnd_bind("<<DropPosition>>", self._on_dnd_position)
            self.dnd_bind("<<DropLeave>>", self._on_dnd_leave)
        except Exception as e:
            logger.info(f"File drag-and-drop unavailable: {e}")
            self._dnd_enabled = False
            return False
        self._dnd_enabled = True
        return True

    def parse_drop_data(self, data) -> List[str]:
        """tkdnd's file list (Tcl list: paths with spaces come in braces)."""
        if not data:
            return []
        if isinstance(data, (list, tuple)):
            return [str(p) for p in data if p]
        return [p for p in self.tk.splitlist(data) if p]

    def _on_dnd_position(self, event):
        if self._locked:
            return "refuse_drop"
        self._show_insert_line(self._drop_index_at(getattr(event, "y_root", None)))
        return "copy"

    def _on_dnd_leave(self, _event=None):
        self._hide_insert_line()
        return "copy"

    def _on_dnd_drop(self, event):
        self._hide_insert_line()
        if self._locked_out("add_songs"):
            return "refuse_drop"
        paths = self.parse_drop_data(getattr(event, "data", ""))
        index = self._drop_index_at(getattr(event, "y_root", None))
        if index is None:
            index = self._insert_index()
        self.add_paths(paths, index)
        return "copy"

    # ---- running order export ------------------------------------------------------

    def _running_order_module(self):
        try:
            from backend import running_order
            return running_order
        except Exception as e:
            logger.info(f"Running-order export unavailable: {e}")
            return None

    def print_running_order(self):
        st = self._state()
        if st is None:
            return False
        module = self._running_order_module()
        if module is None or not hasattr(module, "export_running_order"):
            self._status("Printing the running order is not available in this version",
                         MISSING_COLOR)
            return False
        name = self.snapshot.get("name") or "Show"
        path = self._ask_save_path("Running order", RUNNING_ORDER_FILETYPES, ".html",
                                   initialfile=f"{name} - running order.html")
        if not path:
            return False
        fmt = "csv" if path.lower().endswith(".csv") else "html"
        if not os.path.splitext(path)[1]:
            path += ".html"
        self._status("Writing the running order…")
        queue_ = getattr(self.app, "msg_queue", None)

        def _work():
            error, written = None, path
            try:
                written = module.export_running_order(st, path, fmt) or path
                if fmt == "html" and hasattr(module, "open_in_browser"):
                    module.open_in_browser(written)
            except Exception as e:
                logger.exception("Running-order export failed")
                error = str(e) or type(e).__name__
            if queue_ is not None:
                queue_.put(('running_order_done', (written, fmt, error)))

        threading.Thread(target=_work, name="running-order", daemon=True).start()
        return True

    def on_running_order_done(self, path, fmt, error):
        if error:
            self._status(f"Could not write the running order: {error}", MISSING_COLOR)
        elif fmt == "html":
            self._status(f"Running order opened in your browser - print it from there "
                         f"({os.path.basename(path)})")
        else:
            self._status(f"Running order saved: {os.path.basename(path)}")
