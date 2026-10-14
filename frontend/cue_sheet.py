"""
Cue Sheet Panel for Loop Station.

Unified timeline view that displays BOTH markers (cue points) and
vamps (loop regions) in a single time-ordered list. This gives the
rehearsal director an at-a-glance view of what's coming up next.

REDESIGNED: Includes inline action buttons for vamps:
- ▶ Jump to and play
- ⚙️ Open settings modal
- ✏ Rename
- ✕ Delete

Hold cues (⏸ caesuras) are a row type of their own. In live mode every
add / rename / delete / toggle control is hidden (jump and play stay), and
a compact collapsible "GO log" footer lists the most recent GO presses.
"""

import logging
import time
import tkinter as tk
from typing import Callable, Optional

import customtkinter as ctk

from config import (
    COLOR_BTN_DANGER, COLOR_BTN_SUCCESS,
    COLOR_TEXT, COLOR_TEXT_DIM, COLOR_BG_LIGHT, COLOR_BG_MEDIUM,
    COLOR_MARKER, COLOR_LOOP_REGION,
    PADDING_SMALL,
    COLOR_SKIP_REGION, COLOR_SKIP_CANDIDATE
)
from utils.formatting import format_time
from utils.tooltip import ToolTip

logger = logging.getLogger("LoopStation.CueSheet")

# Hold cues: same colour as the waveform's hold lines.
COLOR_HOLD = "#5ec8f2"
COLOR_HOLD_CURRENT_BG = "#1a2e3a"
COLOR_LIVE = "#ff9f43"

# GO log footer: newest first, at most this many rows.
GO_LOG_MAX_ROWS = 20
GO_ACTION_LABELS = {
    "play": "PLAY",
    "release_hold": "RELEASE",
    "exit_vamp": "EXIT VAMP",
    "none": "—",
}


def repeat_badge(repeat_count) -> str:
    """'×N' for a vamp heard N (>= 2) times in total, '∞' for until-GO."""
    try:
        n = int(repeat_count or 0)
    except (TypeError, ValueError):
        n = 0
    return f"×{n}" if n >= 2 else "∞"


def format_go_entry(entry) -> tuple:
    """(HH:MM:SS, ACTION, label) for a go_log entry dict."""
    ts = entry.get("ts")
    try:
        clock = time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OverflowError, OSError):
        clock = "--:--:--"
    action = str(entry.get("action") or "")
    label = str(entry.get("label") or "")
    # Standby labels read "GO: release Hold 1"; the action column already says GO.
    if label.startswith("GO: "):
        label = label[4:]
    return clock, GO_ACTION_LABELS.get(action, action.upper()), label


class CueSheetPanel(ctk.CTkFrame):
    """
    Unified cue sheet showing markers and vamps sorted by time.
    """
    
    def __init__(
        self,
        parent: tk.Widget,
        # Marker callbacks
        on_add_marker: Optional[Callable] = None,
        on_jump_to_marker: Optional[Callable[[str], None]] = None,
        on_rename_marker: Optional[Callable[[str, str], None]] = None,
        on_delete_marker: Optional[Callable[[str], None]] = None,
        on_jump_next_marker: Optional[Callable] = None,
        on_jump_prev_marker: Optional[Callable] = None,
        # Vamp callbacks
        on_add_vamp: Optional[Callable[[str], None]] = None,  # Takes choice string
        on_select_vamp: Optional[Callable[[int], None]] = None,
        on_jump_to_vamp: Optional[Callable[[int], None]] = None,
        on_open_vamp_settings: Optional[Callable[[int], None]] = None,
        on_rename_vamp: Optional[Callable[[int, str], None]] = None,
        on_delete_vamp: Optional[Callable[[int], None]] = None,
        on_toggle_skip=None,
        on_delete_skip=None,
        # Hold callbacks
        on_add_hold: Optional[Callable[[], None]] = None,
        on_rename_hold: Optional[Callable[[str, str], None]] = None,
        on_delete_hold: Optional[Callable[[str], None]] = None,
        on_jump_hold: Optional[Callable[[str], None]] = None,
        **kwargs
    ):
        super().__init__(parent, fg_color="transparent", **kwargs)
        
        # Marker callbacks
        self.on_add_marker = on_add_marker
        self.on_jump_to_marker = on_jump_to_marker
        self.on_rename_marker = on_rename_marker
        self.on_delete_marker = on_delete_marker
        self.on_jump_next_marker = on_jump_next_marker
        self.on_jump_prev_marker = on_jump_prev_marker
        
        # Vamp callbacks
        self.on_add_vamp = on_add_vamp
        self.on_select_vamp = on_select_vamp
        self.on_jump_to_vamp = on_jump_to_vamp
        self.on_open_vamp_settings = on_open_vamp_settings
        self.on_rename_vamp = on_rename_vamp
        self.on_delete_vamp = on_delete_vamp

        # Hold callbacks
        self.on_add_hold = on_add_hold
        self.on_rename_hold = on_rename_hold
        self.on_delete_hold = on_delete_hold
        self.on_jump_hold = on_jump_hold

        # State (the backend hands us list copies; we keep them as-is)
        self._markers = []
        self._loops = []
        self._skips = []
        self._holds = []
        self._selected_loop_index = -1
        self._live_mode = False
        self._go_log_sig = None
        self._go_log_open = False
        self._go_log_entries = []
        self._item_widgets = []
        self._item_metadata = []  # (item_type, data, ref_id) per row
        self._current_position = 0.0
        self._last_current_items = set()

        # A modal rename dialog keeps the Tk loop running, so backend events
        # can arrive while it is open. Rebuilding rows then would destroy the
        # row whose button is mid-callback, so rebuilds are deferred.
        self._rename_open = False
        self._rebuild_pending = False

        self.on_toggle_skip = on_toggle_skip
        self.on_delete_skip = on_delete_skip

        self._create_widgets()
    
    def _create_widgets(self):
        """Create the cue sheet UI."""
        # Toolbar
        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.pack(fill="x", pady=(0, PADDING_SMALL))
        
        # Nav buttons
        self.btn_prev = ctk.CTkButton(
            toolbar, text="⏮", width=30, height=24,
            fg_color=COLOR_BG_LIGHT,
            text_color=COLOR_TEXT,
            command=self._on_prev
        )
        self.btn_prev.pack(side="left", padx=(0, 3))
        ToolTip(self.btn_prev, "Jump to previous cue, hold or vamp  ( [ )")
        
        self.btn_next = ctk.CTkButton(
            toolbar, text="⏭", width=30, height=24,
            fg_color=COLOR_BG_LIGHT,
            text_color=COLOR_TEXT,
            command=self._on_next
        )
        self.btn_next.pack(side="left", padx=(0, 10))
        ToolTip(self.btn_next, "Jump to next cue, hold or vamp  ( ] )")

        # Editing tools live in their own frame so live mode can hide them
        # in one go (and restore them in the same place).
        self._edit_tools = ctk.CTkFrame(toolbar, fg_color="transparent")
        self._edit_tools.pack(side="left")

        # Add Cue button
        self.btn_add_cue = ctk.CTkButton(
            self._edit_tools, text="+ 📍 Cue", width=80, height=24,
            font=("Segoe UI", 10, "bold"),
            fg_color=COLOR_MARKER,
            text_color="#000000",
            hover_color="#cccc22",
            command=self._on_add_cue
        )
        self.btn_add_cue.pack(side="left", padx=(0, 5))
        ToolTip(self.btn_add_cue, "Add a cue marker at the current playhead position  (M)")

        # Add Hold button
        self.btn_add_hold = ctk.CTkButton(
            self._edit_tools, text="+ ⏸ Hold", width=80, height=24,
            font=("Segoe UI", 10, "bold"),
            fg_color=COLOR_HOLD,
            text_color="#000000",
            hover_color="#8fdaf7",
            command=self._on_add_hold
        )
        self.btn_add_hold.pack(side="left", padx=(0, 5))
        ToolTip(self.btn_add_hold, "Add a hold at the playhead: playback parks there until GO")
                
        # Add Vamp dropdown menu
        self.vamp_menu_var = tk.StringVar(value="+ 🔁 Vamp")
        self.btn_add_vamp = ctk.CTkOptionMenu(
            self._edit_tools,
            values=["Manual Entry", "Auto-Detect"],
            width=100,
            height=24,
            font=("Segoe UI", 10, "bold"),
            command=self._on_add_vamp_selection,
            fg_color="#336633",
            button_color="#336633",
            text_color="#ffffff",
            dropdown_fg_color="#336633",
            dropdown_text_color="#ffffff",
            dropdown_hover_color="#448844",
            variable=self.vamp_menu_var
        )
        self.btn_add_vamp.pack(side="left")
        ToolTip(self.btn_add_vamp, "Add a vamp (loop region) — choose manual or auto-detect")
        
        # Hint text (edit mode) / lock note (live mode) share the right edge
        self._hint_label = ctk.CTkLabel(
            toolbar, text="M = add cue at playhead",
            font=("Segoe UI", 9), text_color=COLOR_TEXT_DIM
        )
        self._hint_label.pack(side="right", padx=5)
        self._live_label = ctk.CTkLabel(
            toolbar, text="🔒 LIVE — editing locked",
            font=("Segoe UI", 10, "bold"), text_color=COLOR_LIVE
        )
                
        # Scrollable list
        self.item_list = ctk.CTkScrollableFrame(
            self, height=120, fg_color=COLOR_BG_MEDIUM,
            corner_radius=4,
            scrollbar_button_color=COLOR_BG_LIGHT
        )
        self.item_list.pack(fill="x")

        # GO log footer (collapsed by default)
        self._create_go_log_footer()

    def update_data(self, markers, loops, selected_loop_index, skips=None, holds=None):
        """
        Update the cue sheet with current markers, vamps, cuts and holds.
        Merges them into a single time-sorted list and rebuilds the UI.
        """
        self._markers = list(markers or [])
        self._loops = list(loops or [])
        self._skips = list(skips or [])
        self._holds = list(holds or [])
        self._selected_loop_index = selected_loop_index
        self._request_rebuild()

    def _build_items(self):
        """Unified, time-sorted (sort_time, type, data, ref_id) rows.

        ref_id is the loop index for vamps and the object id for skips and
        holds (None for markers).
        """
        items = []
        for marker in self._markers:
            items.append((marker.time, 'marker', marker, None))
        for i, loop in enumerate(self._loops):
            items.append((loop.start, 'vamp', loop, i))
        for hold in self._holds:
            items.append((hold.time, 'hold', hold, hold.id))
        for skip in self._skips:
            items.append((skip.start, 'skip', skip, skip.id))
        items.sort(key=lambda x: x[0])
        return items

    # =========================================================================
    # LIVE MODE
    # =========================================================================

    def set_live_mode(self, enabled: bool):
        """Hide every add/rename/delete/toggle control while live (jump/play stay)."""
        enabled = bool(enabled)
        if enabled == self._live_mode:
            return
        self._live_mode = enabled
        if enabled:
            self._edit_tools.pack_forget()
            self._hint_label.pack_forget()
            self._live_label.pack(side="right", padx=5)
        else:
            self._live_label.pack_forget()
            self._edit_tools.pack(side="left", after=self.btn_next)
            self._hint_label.pack(side="right", padx=5)
        self._request_rebuild()

    # =========================================================================
    # GO LOG FOOTER
    # =========================================================================

    def _create_go_log_footer(self):
        self._go_footer = ctk.CTkFrame(self, fg_color="transparent")
        self._go_footer.pack(fill="x", pady=(3, 0))

        self._go_header = ctk.CTkButton(
            self._go_footer, text="", anchor="w", height=20,
            font=("Segoe UI", 9, "bold"),
            fg_color="transparent", hover_color=COLOR_BG_LIGHT,
            text_color=COLOR_TEXT_DIM,
            command=self._toggle_go_log
        )
        self._go_header.pack(fill="x")
        ToolTip(self._go_header, "Show / hide the most recent GO presses")

        self._go_body = ctk.CTkFrame(self._go_footer, fg_color=COLOR_BG_MEDIUM, corner_radius=4)
        self._go_log_sig = ()   # forces the first update_go_log to build
        self.update_go_log([])

    def _refresh_go_header(self):
        arrow = "▼" if self._go_log_open else "▶"
        n = len(self._go_log_entries)
        text = f"{arrow}  GO log ({n})"
        if n and not self._go_log_open:
            clock, action, label = format_go_entry(self._go_log_entries[0])
            text += f"   ·   last: {clock}  {action}  {label}"
        self._go_header.configure(text=text)

    def _toggle_go_log(self):
        self._go_log_open = not self._go_log_open
        if self._go_log_open:
            self._go_body.pack(fill="x", pady=(1, 0))
        else:
            self._go_body.pack_forget()
        self._refresh_go_header()

    def update_go_log(self, entries):
        """Show the GO log (list of go_log dicts), newest first, max 20 rows."""
        recent = list(entries or [])[-GO_LOG_MAX_ROWS:][::-1]
        sig = tuple((e.get("ts"), e.get("action"), e.get("label"), e.get("source"))
                    for e in recent)
        if sig == self._go_log_sig and self._go_body.winfo_children():
            return
        self._go_log_sig = sig
        self._go_log_entries = recent

        for w in self._go_body.winfo_children():
            w.destroy()
        if not recent:
            ctk.CTkLabel(self._go_body, text="No GO presses yet.", height=18,
                         font=("Segoe UI", 9), text_color=COLOR_TEXT_DIM
                         ).pack(anchor="w", padx=8, pady=2)
        for entry in recent:
            clock, action, label = format_go_entry(entry)
            row = ctk.CTkFrame(self._go_body, fg_color="transparent")
            row.pack(fill="x", padx=6)
            ctk.CTkLabel(row, text=clock, font=("Consolas", 9), height=16,
                         text_color=COLOR_TEXT_DIM, width=60, anchor="w").pack(side="left")
            ctk.CTkLabel(row, text=action, font=("Segoe UI", 9, "bold"), height=16,
                         text_color=COLOR_BTN_SUCCESS, width=72, anchor="w").pack(side="left")
            source = str(entry.get("source") or "")
            ctk.CTkLabel(row, text=label, font=("Segoe UI", 9), height=16,
                         text_color=COLOR_TEXT, anchor="w").pack(side="left")
            if source:
                ctk.CTkLabel(row, text=f"({source})", font=("Segoe UI", 8), height=16,
                             text_color=COLOR_TEXT_DIM, anchor="w").pack(side="left", padx=6)
        self._refresh_go_header()

    def _request_rebuild(self):
        """Rebuild the rows now, or after the open rename dialog closes."""
        if self._rename_open:
            self._rebuild_pending = True
            return
        self._rebuild_pending = False
        self._rebuild_list()

    def update_position(self, position):
        """Update current playback position for highlighting.

        PERFORMANCE FIX: Instead of destroying and recreating ALL widgets
        (which causes missed clicks because buttons get destroyed mid-click),
        we only update the visual styling when the highlighted set changes.
        """
        self._current_position = position
        new_current = self._get_current_item_index()
        if self._last_current_items != new_current:
            old_current = self._last_current_items
            self._last_current_items = new_current

            # Update highlight in-place instead of full rebuild
            self._update_highlight(old_current, new_current)

    def _update_highlight(self, old_idx, new_idx):
        """Update just the highlight styling in-place without rebuilding widgets.

        Only touches rows whose highlight state actually changed,
        preventing the flash/flicker caused by destroy-and-recreate.
        """
        changed = old_idx.symmetric_difference(new_idx)

        for idx in changed:
            if idx >= len(self._item_widgets) or idx >= len(self._item_metadata):
                # Safety: if indices are out of range, do a full rebuild once
                logger.debug(f"Highlight index {idx} out of range; rebuilding cue sheet")
                self._request_rebuild()
                return
            
            row = self._item_widgets[idx]
            item_type, data, ref_id = self._item_metadata[idx]
            is_current = idx in new_idx
            
            try:
                if item_type == 'marker':
                    if is_current:
                        row.configure(fg_color="#3a3a1a", border_width=2, border_color=COLOR_MARKER)
                    else:
                        row.configure(fg_color="transparent", border_width=0)
                        
                elif item_type == 'vamp':
                    is_selected = (ref_id == self._selected_loop_index)
                    if is_selected and is_current:
                        row.configure(fg_color="#2a4a2a", border_width=2, border_color="#66ff66")
                    elif is_selected:
                        row.configure(fg_color="#1a331a", border_width=1, border_color=COLOR_LOOP_REGION)
                    elif is_current:
                        row.configure(fg_color="#2a2a1a", border_width=2, border_color="#ffff00")
                    else:
                        row.configure(fg_color="transparent", border_width=0)
                        
                elif item_type == 'hold':
                    if is_current:
                        row.configure(fg_color=COLOR_HOLD_CURRENT_BG, border_width=2,
                                      border_color=COLOR_HOLD)
                    else:
                        row.configure(fg_color="transparent", border_width=0)

                elif item_type == 'skip':
                    if is_current:
                        row.configure(border_width=1, border_color=COLOR_SKIP_REGION)
                    else:
                        row.configure(border_width=0)
            except Exception:
                # Widget was destroyed or invalid — fall back to full rebuild
                logger.debug("Cue sheet row vanished during highlight; rebuilding", exc_info=True)
                self._request_rebuild()
                return

    def _create_skip_row(self, skip, is_current=False):
        """Create a row for a Skip Region (Cut)."""
        # We use a dark background if active, or transparent if inactive.
        # Ideally, we would use a theme variable for the bg, but for now 
        # we will use a semi-transparent version logic or a hardcoded dark overlay 
        # to ensure readability against the text color.
        bg = "#331111" if skip.active else "transparent"
        
        border = COLOR_SKIP_REGION if is_current else None
        
        row = ctk.CTkFrame(self.item_list, fg_color=bg, height=30, corner_radius=4,
                           border_width=1 if is_current else 0, border_color=border)
        row.pack(fill="x", pady=1)
        
        # Icon
        ctk.CTkLabel(row, text="✂", width=24, text_color=COLOR_SKIP_CANDIDATE).pack(side="left", padx=4)
        
        # Time
        time_text = f"{self._format_time(skip.start)} → {self._format_time(skip.end)}"
        ctk.CTkLabel(row, text=time_text, font=("Consolas", 10), 
                     text_color=COLOR_SKIP_CANDIDATE, width=130).pack(side="left")
                     
        # Name
        name_lbl = ctk.CTkLabel(row, text=skip.name, font=("Segoe UI", 11, "italic"),
                                text_color=COLOR_SKIP_CANDIDATE if skip.active else COLOR_TEXT_DIM)
        name_lbl.pack(side="left", padx=10, fill="x", expand=True)
        
        if self._live_mode:
            # Locked: show the state, no toggle / delete.
            ctk.CTkLabel(row, text="ON" if skip.active else "OFF", width=40,
                         font=("Segoe UI", 9, "bold"),
                         text_color=COLOR_SKIP_CANDIDATE if skip.active else COLOR_TEXT_DIM
                         ).pack(side="right", padx=5)
            return row

        # Delete (rightmost, consistent with cue and vamp rows)
        btn_del_s = ctk.CTkButton(row, text="✕", width=25, height=20, fg_color="transparent",
                      hover_color="#441111", text_color=COLOR_BTN_DANGER,
                      command=lambda s=skip.id: self.on_delete_skip(s) if self.on_delete_skip else None
                      )
        btn_del_s.pack(side="right", padx=2)
        ToolTip(btn_del_s, "Delete this cut region")

        # Toggle Active Switch
        switch = ctk.CTkSwitch(row, text="", width=40, height=20,
                               progress_color=COLOR_SKIP_REGION, # Use theme color for the switch
                               command=lambda s=skip.id: self.on_toggle_skip(s) if self.on_toggle_skip else None)
        if skip.active: switch.select()
        else: switch.deselect()
        switch.pack(side="right", padx=5)
        ToolTip(switch, "Enable or disable this cut region")
                      
        return row

    def _get_current_item_index(self):
        """
        Get the set of item indices that should be highlighted.
        
        Rules:
        - Cue (marker) / hold: highlighted from its time until the next cue,
          hold or vamp starts
        - Vamp: highlighted while position is within vamp range (start <= pos <= end)
        - Skip: highlighted while position is within skip range
        - Multiple items can be highlighted simultaneously (e.g., cue inside vamp)
        
        Returns a set of indices into the unified sorted items list.
        """
        items = self._build_items()

        highlighted = set()

        for idx, (sort_time, item_type, data, ref_id) in enumerate(items):
            if item_type in ('marker', 'hold'):
                # Cue/hold is highlighted from its time until the next cue,
                # hold or vamp start
                if self._current_position < data.time:
                    continue  # Haven't reached this cue yet
                
                # Find the next cue or vamp start time after this marker
                next_boundary = None
                for next_idx in range(idx + 1, len(items)):
                    next_item_type = items[next_idx][1]
                    if next_item_type in ('marker', 'vamp', 'hold'):
                        next_boundary = items[next_idx][0]
                        break
                
                if next_boundary is not None:
                    if data.time <= self._current_position < next_boundary:
                        highlighted.add(idx)
                else:
                    # Last cue/vamp in the list - highlight from this cue onward
                    if self._current_position >= data.time:
                        highlighted.add(idx)
                        
            elif item_type == 'vamp':
                # Vamp: highlight while position is within range
                if data.start <= self._current_position <= data.end:
                    highlighted.add(idx)
                    
            elif item_type == 'skip':
                # Skip: highlight while position is within range
                if data.start <= self._current_position <= data.end:
                    highlighted.add(idx)
        
        return highlighted

    def _rebuild_list(self):
        """Rebuild the item list, sorted by time."""
        # Clear existing widgets
        for w in self._item_widgets:
            w.destroy()
        self._item_widgets.clear()
        self._item_metadata = []  # Track (item_type, data, ref_id) per row

        # Unified list of (sort_time, type, data, ref_id)
        items = self._build_items()

        if not items:
            lbl = ctk.CTkLabel(
                self.item_list,
                text=("No cues, holds, vamps, or cuts yet." if self._live_mode else
                      "No cues, holds, vamps, or cuts yet. Press M to add a cue point, "
                      "or use + buttons above."),
                text_color=COLOR_TEXT_DIM, font=("Segoe UI", 10),
                wraplength=400
            )
            lbl.pack(pady=8)
            self._item_widgets.append(lbl)
            return

        # Get current items for highlighting (set of indices)
        current_items = self._get_current_item_index()
        self._last_current_items = current_items

        for idx, (sort_time, item_type, data, ref_id) in enumerate(items):
            # Check if this item should be highlighted
            is_current = idx in current_items
            
            if item_type == 'marker':
                row = self._create_marker_row(data, is_current)
            elif item_type == 'vamp':
                is_selected = (ref_id == self._selected_loop_index)
                row = self._create_vamp_row(data, ref_id, is_selected, is_current)
            elif item_type == 'hold':
                row = self._create_hold_row(data, is_current)
            elif item_type == 'skip':
                row = self._create_skip_row(data, is_current)
            else:
                continue
                        
            self._item_widgets.append(row)
            self._item_metadata.append((item_type, data, ref_id))

    _format_time = staticmethod(format_time)

    def _create_marker_row(self, marker, is_current=False):
        """Create a row widget for a cue point marker."""
        # Highlight if current
        bg = "#3a3a1a" if is_current else "transparent"

        row = ctk.CTkFrame(
            self.item_list, 
            fg_color=bg, 
            height=28,
            corner_radius=4,
            border_width=2 if is_current else 0,
            border_color=COLOR_MARKER if is_current else None  # CHANGED: Don't use "transparent"
        )
        row.pack(fill="x", pady=1)
        row.pack_propagate(False)
        
        # Type icon
        ctk.CTkLabel(
            row, text="📍", width=24,
            font=("Segoe UI", 12)
        ).pack(side="left", padx=(4, 2))

        # Time - brighter if current
        time_color = "#ffff00" if is_current else COLOR_MARKER
        ctk.CTkLabel(
            row, text=self._format_time(marker.time),
            font=("Consolas", 10),
            text_color=time_color,
            width=55
        ).pack(side="left", padx=(0, 6))
        
        # Current indicator
        if is_current:
            ctk.CTkLabel(
                row, text="▶",
                font=("Segoe UI", 10),
                text_color="#ffff00",
                width=16
            ).pack(side="left", padx=(0, 4))
        
        # Name (clickable to jump)
        name_btn = ctk.CTkButton(
            row,
            text=marker.name,
            anchor="w",
            height=22,
            font=("Segoe UI", 11, "bold" if is_current else "normal"),
            fg_color="transparent",
            hover_color=COLOR_BG_LIGHT,
            text_color="#ffffff" if is_current else COLOR_TEXT,
            command=lambda mid=marker.id: self._on_jump_marker(mid)
        )
        name_btn.pack(side="left", fill="x", expand=True, padx=2)

        if self._live_mode:
            return row

        # Action buttons, packed right-to-left so Delete is always the
        # rightmost control on every row type (cues, vamps, cuts).
        btn_del_m = ctk.CTkButton(
            row, text="✕", width=24, height=20,
            fg_color="transparent", hover_color="#442222",
            text_color="#aa4444",
            command=lambda mid=marker.id: self._on_delete_marker(mid)
        )
        btn_del_m.pack(side="right", padx=1)
        ToolTip(btn_del_m, "Delete this cue")

        btn_ren_m = ctk.CTkButton(
            row, text="✏", width=24, height=20,
            fg_color="transparent", hover_color=COLOR_BG_LIGHT,
            text_color=COLOR_TEXT_DIM,
            command=lambda mid=marker.id, mn=marker.name: self._on_rename_marker(mid, mn)
        )
        btn_ren_m.pack(side="right", padx=1)
        ToolTip(btn_ren_m, "Rename this cue")

        return row

    def _create_vamp_row(self, loop, loop_idx, is_selected, is_current=False):
        """Create a row widget for a vamp (loop region) with inline action buttons."""
        # Highlight: selected OR current
        if is_selected and is_current:
            bg = "#2a4a2a"  # Both selected and current
            border_color = "#66ff66"
            border_width = 2
        elif is_selected:
            bg = "#1a331a"  # Just selected
            border_color = COLOR_LOOP_REGION
            border_width = 1
        elif is_current:
            bg = "#2a2a1a"  # Just current (yellowish)
            border_color = "#ffff00"
            border_width = 2
        else:
            bg = "transparent"
            border_color = None  # CHANGED: None instead of "transparent"
            border_width = 0
        
        row = ctk.CTkFrame(
            self.item_list, fg_color=bg, height=30,
            corner_radius=4,
            border_width=border_width,
            border_color=border_color  # Now either a real color or None
        )
        row.pack(fill="x", pady=1)
        row.pack_propagate(False)

        # Type icon
        ctk.CTkLabel(
            row, text="📍", width=24,
            font=("Segoe UI", 12)
        ).pack(side="left", padx=(4, 2))
        
        # Time range
        time_text = f"{self._format_time(loop.start)} → {self._format_time(loop.end)}"
        duration = loop.end - loop.start
        
        ctk.CTkLabel(
            row, text=time_text,
            font=("Consolas", 10),
            text_color="#66bb6a" if is_selected else "#558855",
            width=130
        ).pack(side="left", padx=(0, 4))
        
        # Duration badge
        ctk.CTkLabel(
            row, text=f"({duration:.1f}s)",
            font=("Consolas", 9),
            text_color=COLOR_TEXT_DIM,
            width=45
        ).pack(side="left", padx=(0, 4))

        # Repeat badge: xN = heard N times in total then continues; inf = until GO
        badge = repeat_badge(getattr(loop, 'repeat_count', None))
        counted = badge != "∞"
        badge_lbl = ctk.CTkLabel(
            row, text=badge, width=30, height=18,
            font=("Segoe UI", 10, "bold"),
            fg_color="#264026" if counted else "#2e2e2e",
            corner_radius=8,
            text_color="#aaffaa" if counted else COLOR_TEXT_DIM,
        )
        badge_lbl.pack(side="left", padx=(0, 6))
        ToolTip(badge_lbl, f"Plays {badge[1:]} times in total, then continues"
                if counted else "Loops until GO")

        # Status indicator, on the left like cue rows: ▶ = playhead is here,
        # ● = selected for editing, ⬤ = both. (Kept away from the action
        # buttons so it is not mistaken for a second Play button.)
        if is_selected and is_current:
            ctk.CTkLabel(row, text="⬤", font=("Segoe UI", 10),
                         text_color="#66ff66", width=16).pack(side="left", padx=(0, 4))
        elif is_current:
            ctk.CTkLabel(row, text="▶", font=("Segoe UI", 10),
                         text_color="#ffff00", width=16).pack(side="left", padx=(0, 4))
        elif is_selected:
            ctk.CTkLabel(row, text="●", font=("Segoe UI", 10),
                         text_color="#66bb6a", width=16).pack(side="left", padx=(0, 4))

        # Name (clickable to select this vamp)
        name_color = "#aaffaa" if is_selected else COLOR_TEXT
        name_btn = ctk.CTkButton(
            row,
            text=loop.name,
            anchor="w",
            height=22,
            font=("Segoe UI", 11, "bold" if is_selected else "normal"),
            fg_color="transparent",
            hover_color=COLOR_BG_LIGHT,
            text_color=name_color,
            command=lambda idx=loop_idx: self._on_select_vamp_row(idx)
        )
        name_btn.pack(side="left", fill="x", expand=True, padx=2)

        # ACTION BUTTONS (right side)
        if self._live_mode:
            self._create_vamp_play_button(row, loop_idx, is_selected)
            return row

        # Delete button
        btn_del = ctk.CTkButton(
            row, text="✕", width=28, height=22,
            fg_color="transparent",
            hover_color="#442222",
            text_color="#aa4444",
            command=lambda idx=loop_idx: self._on_delete_vamp_row(idx)
        )
        btn_del.pack(side="right", padx=2)
        ToolTip(btn_del, "Delete this vamp")
        
        # Rename button
        btn_ren = ctk.CTkButton(
            row, text="✏", width=28, height=22,
            fg_color="transparent",
            hover_color=COLOR_BG_LIGHT,
            text_color=COLOR_TEXT_DIM,
            command=lambda lid=loop.id: self._on_rename_vamp_row(lid)
        )
        btn_ren.pack(side="right", padx=2)
        ToolTip(btn_ren, "Rename this vamp")
        
        # Settings button (⚙️)
        btn_set = ctk.CTkButton(
            row, text="⚙️", width=28, height=22,
            fg_color=COLOR_BG_LIGHT,
            hover_color="#555555",
            text_color=COLOR_TEXT,
            command=lambda idx=loop_idx: self._on_open_settings_row(idx)
        )
        btn_set.pack(side="right", padx=2)
        ToolTip(btn_set, "Open vamp settings (crossfade, boundaries)")
        
        self._create_vamp_play_button(row, loop_idx, is_selected)
        return row

    def _create_vamp_play_button(self, row, loop_idx, is_selected):
        """Play/Jump button (kept in live mode)."""
        btn_play = ctk.CTkButton(
            row, text="▶", width=28, height=22,
            fg_color=COLOR_BTN_SUCCESS if is_selected else COLOR_BG_LIGHT,
            hover_color=COLOR_BTN_SUCCESS,
            text_color="#ffffff" if is_selected else COLOR_TEXT,
            command=lambda idx=loop_idx: self._on_jump_to_vamp_row(idx)
        )
        btn_play.pack(side="right", padx=2)
        ToolTip(btn_play, "Jump to this vamp and start playing")

    def _create_hold_row(self, hold, is_current=False):
        """Create a row widget for a hold cue (caesura)."""
        row = ctk.CTkFrame(
            self.item_list,
            fg_color=COLOR_HOLD_CURRENT_BG if is_current else "transparent",
            height=28,
            corner_radius=4,
            border_width=2 if is_current else 0,
            border_color=COLOR_HOLD if is_current else None
        )
        row.pack(fill="x", pady=1)
        row.pack_propagate(False)

        ctk.CTkLabel(
            row, text="⏸", width=24,
            font=("Segoe UI", 12, "bold"), text_color=COLOR_HOLD
        ).pack(side="left", padx=(4, 2))

        ctk.CTkLabel(
            row, text=self._format_time(hold.time),
            font=("Consolas", 10),
            text_color="#bfeaff" if is_current else COLOR_HOLD,
            width=55
        ).pack(side="left", padx=(0, 6))

        ctk.CTkLabel(
            row, text="HOLD", width=40, height=18,
            font=("Segoe UI", 9, "bold"),
            fg_color="#1d3a4a", corner_radius=8, text_color=COLOR_HOLD
        ).pack(side="left", padx=(0, 6))

        if is_current:
            ctk.CTkLabel(
                row, text="▶", font=("Segoe UI", 10),
                text_color=COLOR_HOLD, width=16
            ).pack(side="left", padx=(0, 4))

        name_btn = ctk.CTkButton(
            row, text=hold.name, anchor="w", height=22,
            font=("Segoe UI", 11, "bold" if is_current else "normal"),
            fg_color="transparent", hover_color=COLOR_BG_LIGHT,
            text_color="#ffffff" if is_current else COLOR_TEXT,
            command=lambda hid=hold.id: self._on_jump_hold(hid)
        )
        name_btn.pack(side="left", fill="x", expand=True, padx=2)
        ToolTip(name_btn, "Jump to this hold")

        if self._live_mode:
            return row

        btn_del = ctk.CTkButton(
            row, text="✕", width=24, height=20,
            fg_color="transparent", hover_color="#442222",
            text_color="#aa4444",
            command=lambda hid=hold.id: self._on_delete_hold(hid)
        )
        btn_del.pack(side="right", padx=1)
        ToolTip(btn_del, "Delete this hold")

        btn_ren = ctk.CTkButton(
            row, text="✏", width=24, height=20,
            fg_color="transparent", hover_color=COLOR_BG_LIGHT,
            text_color=COLOR_TEXT_DIM,
            command=lambda hid=hold.id: self._on_rename_hold(hid)
        )
        btn_ren.pack(side="right", padx=1)
        ToolTip(btn_ren, "Rename this hold")

        return row
    
    # =========================================================================
    # HANDLERS - CUE POINTS
    # =========================================================================
    
    def _on_add_cue(self):
        if self.on_add_marker:
            self.on_add_marker()
    
    def _on_jump_marker(self, marker_id):
        if self.on_jump_to_marker:
            self.on_jump_to_marker(marker_id)
    
    def _ask_name(self, text, title):
        """
        Show a modal name prompt. Row rebuilds are held off while it is open
        (the Tk loop keeps running underneath a modal dialog) and applied
        once it closes. Returns the stripped name, or None.
        """
        self._rename_open = True
        try:
            dialog = ctk.CTkInputDialog(text=text, title=title)
            new_name = dialog.get_input()
        finally:
            self._rename_open = False
        if self._rebuild_pending and self.winfo_exists():
            self._request_rebuild()
        if new_name and new_name.strip():
            return new_name.strip()
        return None

    def _on_rename_marker(self, marker_id, current_name):
        new_name = self._ask_name("Rename cue point:", "Rename Cue")
        if new_name and self.on_rename_marker:
            self.on_rename_marker(marker_id, new_name)
    
    def _on_delete_marker(self, marker_id):
        if self.on_delete_marker:
            self.on_delete_marker(marker_id)

    # =========================================================================
    # HANDLERS - HOLDS
    # =========================================================================

    def _on_add_hold(self):
        if self.on_add_hold:
            self.on_add_hold()

    def _on_jump_hold(self, hold_id):
        if self.on_jump_hold:
            self.on_jump_hold(hold_id)

    def _on_rename_hold(self, hold_id):
        new_name = self._ask_name("Rename hold:", "Rename Hold")
        if new_name and self.on_rename_hold:
            self.on_rename_hold(hold_id, new_name)

    def _on_delete_hold(self, hold_id):
        if self.on_delete_hold:
            self.on_delete_hold(hold_id)

    def _nav_items(self):
        """(time, type, ref) jump targets, time-sorted; ref = id, or index for vamps."""
        return [(t, kind, ref if kind == 'vamp' else data.id)
                for t, kind, data, ref in self._build_items() if kind != 'skip']

    def _jump_to_nav_item(self, kind, ref):
        if kind == 'marker':
            if self.on_jump_to_marker:
                self.on_jump_to_marker(ref)
        elif kind == 'hold':
            if self.on_jump_hold:
                self.on_jump_hold(ref)
        elif self.on_jump_to_vamp:
            self.on_jump_to_vamp(ref)
        
    def _on_prev(self):
        """Jump to previous item (cue, hold or vamp) in timeline."""
        for t, kind, ref in reversed(self._nav_items()):
            if t < self._current_position - 0.5:  # Must be at least 0.5s back
                self._jump_to_nav_item(kind, ref)
                return

    def _on_next(self):
        """Jump to next item (cue, hold or vamp) in timeline."""
        for t, kind, ref in self._nav_items():
            if t > self._current_position + 0.1:  # Small buffer
                self._jump_to_nav_item(kind, ref)
                return
    
    # =========================================================================
    # HANDLERS - VAMPS
    # =========================================================================
    
    def _on_add_vamp_selection(self, choice):
        """Handle vamp creation from dropdown menu."""
        if self.on_add_vamp:
            self.on_add_vamp(choice)
        # Reset dropdown to default text
        self.vamp_menu_var.set("+ 🔁 Vamp")
    
    def _on_select_vamp_row(self, loop_idx):
        """Select a vamp (clicking on name)."""
        if self.on_select_vamp:
            self.on_select_vamp(loop_idx)
    
    def _on_jump_to_vamp_row(self, loop_idx):
        """Jump to vamp start and play (clicking ▶ button)."""
        if self.on_jump_to_vamp:
            self.on_jump_to_vamp(loop_idx)
    
    def _on_open_settings_row(self, loop_idx):
        """Open vamp settings modal (clicking ⚙️ button)."""
        if self.on_open_vamp_settings:
            self.on_open_vamp_settings(loop_idx)
    
    def _on_rename_vamp_row(self, loop_id):
        """Rename a vamp (clicking ✏ button)."""
        new_name = self._ask_name("Rename vamp:", "Rename Vamp")
        if not new_name or not self.on_rename_vamp:
            return
        # The list may have changed while the dialog was open, so resolve
        # the index from the stable id only now.
        for idx, loop in enumerate(self._loops):
            if loop.id == loop_id:
                self.on_rename_vamp(idx, new_name)
                return
        logger.warning("Vamp to rename no longer exists; rename ignored")
    
    def _on_delete_vamp_row(self, loop_idx):
        """Delete a vamp (clicking ✕ button)."""
        if self.on_delete_vamp:
            self.on_delete_vamp(loop_idx)


# Interface name used by the app (same class).
CueSheet = CueSheetPanel
