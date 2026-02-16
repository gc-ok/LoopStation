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
    COLOR_MARKER, COLOR_LOOP_REGION, COLOR_LOOP_IN,
    PADDING_SMALL, PADDING_MEDIUM, COLOR_BTN_TEXT,
    COLOR_SKIP_REGION, COLOR_SKIP_CANDIDATE
)
from utils.tooltip import ToolTip

logger = logging.getLogger("LoopStation.CueSheet")


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
        
        # State
        self._markers = []
        self._loops = []
        self._selected_loop_index = -1
        self._item_widgets = []
        self._item_metadata = []  # (item_type, data, ref_id) per row
        self._current_position = 0.0
        
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
        ToolTip(self.btn_prev, "Jump to previous cue or vamp  ( [ )")
        
        self.btn_next = ctk.CTkButton(
            toolbar, text="⏭", width=30, height=24,
            fg_color=COLOR_BG_LIGHT,
            text_color=COLOR_TEXT,
            command=self._on_next
        )
        self.btn_next.pack(side="left", padx=(0, 10))
        ToolTip(self.btn_next, "Jump to next cue or vamp  ( ] )")
        
        # Add Cue button
        self.btn_add_cue = ctk.CTkButton(
            toolbar, text="+ 📍 Cue", width=80, height=24,
            font=("Segoe UI", 10, "bold"),
            fg_color=COLOR_MARKER,
            text_color="#000000",
            hover_color="#cccc22",
            command=self._on_add_cue
        )
        self.btn_add_cue.pack(side="left", padx=(0, 5))
        ToolTip(self.btn_add_cue, "Add a cue marker at the current playhead position  (M)")
        
        # Add Vamp dropdown menu
        self.vamp_menu_var = tk.StringVar(value="+ 🔁 Vamp")
        self.btn_add_vamp = ctk.CTkOptionMenu(
            toolbar,
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
        
        # Hint text
        ctk.CTkLabel(
            toolbar, text="M = add cue at playhead",
            font=("Segoe UI", 9), text_color=COLOR_TEXT_DIM
        ).pack(side="right", padx=5)
        
        # Scrollable list
        self.item_list = ctk.CTkScrollableFrame(
            self, height=120, fg_color=COLOR_BG_MEDIUM,
            corner_radius=4,
            scrollbar_button_color=COLOR_BG_LIGHT
        )
        self.item_list.pack(fill="x")
    
    def update_data(self, markers, loops, selected_loop_index, skips=None):
        """
        Update the cue sheet with current markers and loops.
        Merges both into a single time-sorted list and rebuilds the UI.
        """
        self._markers = markers or []
        self._loops = loops or []
        self._skips = skips or [] # Store skips
        self._selected_loop_index = selected_loop_index
        self._rebuild_list()
    
    def update_position(self, position):
        """Update current playback position for highlighting.
        
        PERFORMANCE FIX: Instead of destroying and recreating ALL widgets
        (which causes missed clicks because buttons get destroyed mid-click),
        we only update the visual styling when the highlighted set changes.
        """
        self._current_position = position
        new_current = self._get_current_item_index()
        if not hasattr(self, '_last_current_items') or self._last_current_items != new_current:
            old_current = getattr(self, '_last_current_items', set())
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
                self._rebuild_list()
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
                        
                elif item_type == 'skip':
                    if is_current:
                        row.configure(border_width=1, border_color=COLOR_SKIP_REGION)
                    else:
                        row.configure(border_width=0)
            except Exception:
                # Widget was destroyed or invalid — fall back to full rebuild
                self._rebuild_list()
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
        
        # Toggle Active Switch
        switch = ctk.CTkSwitch(row, text="", width=40, height=20,
                               progress_color=COLOR_SKIP_REGION, # Use theme color for the switch
                               command=lambda s=skip.id: self.on_toggle_skip(s) if self.on_toggle_skip else None)
        if skip.active: switch.select()
        else: switch.deselect()
        switch.pack(side="right", padx=5)
        ToolTip(switch, "Enable or disable this cut region")
        
        # Delete
        btn_del_s = ctk.CTkButton(row, text="✕", width=25, height=20, fg_color="transparent", 
                      hover_color="#441111", text_color=COLOR_BTN_DANGER,
                      command=lambda s=skip.id: self.on_delete_skip(s) if self.on_delete_skip else None
                      )
        btn_del_s.pack(side="right", padx=2)
        ToolTip(btn_del_s, "Delete this cut region")
                      
        return row

    def _get_current_item_index(self):
        """
        Get the set of item indices that should be highlighted.
        
        Rules:
        - Cue (marker): highlighted from its time until the next cue or vamp starts
        - Vamp: highlighted while position is within vamp range (start <= pos <= end)
        - Skip: highlighted while position is within skip range
        - Multiple items can be highlighted simultaneously (e.g., cue inside vamp)
        
        Returns a set of indices into the unified sorted items list.
        """
        items = []
        for marker in self._markers: items.append((marker.time, 'marker', marker, None))
        for i, loop in enumerate(self._loops): items.append((loop.start, 'vamp', loop, i))
        for skip in self._skips: items.append((skip.start, 'skip', skip, skip.id))
        items.sort(key=lambda x: x[0])

        highlighted = set()
        
        for idx, (sort_time, item_type, data, ref_id) in enumerate(items):
            if item_type == 'marker':
                # Cue is highlighted from its time until the next cue or vamp start
                if self._current_position < data.time:
                    continue  # Haven't reached this cue yet
                
                # Find the next cue or vamp start time after this marker
                next_boundary = None
                for next_idx in range(idx + 1, len(items)):
                    next_item_type = items[next_idx][1]
                    if next_item_type in ('marker', 'vamp'):
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

        # Build unified list of (sort_time, type, data, ref_id)
        # ref_id is loop_index for vamps, or object.id for skips
        items = []
        
        # Add Markers
        for marker in self._markers:
            items.append((marker.time, 'marker', marker, None))
            
        # Add Vamps (Loops)
        for i, loop in enumerate(self._loops):
            items.append((loop.start, 'vamp', loop, i))
            
        # Add Skips (Cuts) - NEW
        for skip in self._skips:
            items.append((skip.start, 'skip', skip, skip.id))

        # Sort by time
        items.sort(key=lambda x: x[0])

        if not items:
            lbl = ctk.CTkLabel(
                self.item_list,
                text="No cues, vamps, or cuts yet. Press M to add a cue point, or use + buttons above.",
                text_color=COLOR_TEXT_DIM, font=("Segoe UI", 10),
                wraplength=400
            )
            lbl.pack(pady=8)
            self._item_widgets.append(lbl)
            return

        # Get current items for highlighting (set of indices)
        current_items = self._get_current_item_index()

        for idx, (sort_time, item_type, data, ref_id) in enumerate(items):
            # Check if this item should be highlighted
            is_current = idx in current_items
            
            if item_type == 'marker':
                row = self._create_marker_row(data, is_current)
            elif item_type == 'vamp':
                is_selected = (ref_id == self._selected_loop_index)
                row = self._create_vamp_row(data, ref_id, is_selected, is_current)
            elif item_type == 'skip':
                row = self._create_skip_row(data, is_current) # NEW
            
            self._item_widgets.append(row)
            self._item_metadata.append((item_type, data, ref_id))

    def _format_time(self, seconds):
        """Format seconds as M:SS.ss"""
        minutes = int(seconds // 60)
        secs = seconds % 60
        return f"{minutes}:{secs:05.2f}"
    
    def _create_marker_row(self, marker, is_current=False):
        """Create a row widget for a cue point marker."""
        # Highlight if current
        bg = "#3a3a1a" if is_current else "transparent"
        border_color = COLOR_MARKER if is_current else None
        
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
        
        # Type icon - brighter if current
        icon_color = "#ffff00" if is_current else COLOR_MARKER
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
        
        # Rename
        btn_ren_m = ctk.CTkButton(
            row, text="✏", width=24, height=20,
            fg_color="transparent", hover_color=COLOR_BG_LIGHT,
            text_color=COLOR_TEXT_DIM,
            command=lambda mid=marker.id, mn=marker.name: self._on_rename_marker(mid, mn)
        )
        btn_ren_m.pack(side="right", padx=1)
        ToolTip(btn_ren_m, "Rename this cue")
        
        # Delete
        btn_del_m = ctk.CTkButton(
            row, text="✕", width=24, height=20,
            fg_color="transparent", hover_color="#442222",
            text_color="#aa4444",
            command=lambda mid=marker.id: self._on_delete_marker(mid)
        )
        btn_del_m.pack(side="right", padx=1)
        ToolTip(btn_del_m, "Delete this cue")
        
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
        icon_color = "#ffff00" if is_current else COLOR_MARKER
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
        ).pack(side="left", padx=(0, 6))
        
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
        
        # Selected indicator
        if is_selected and is_current:
            ctk.CTkLabel(
                row, text="⬤",  # Solid circle for both
                font=("Segoe UI", 10),
                text_color="#66ff66",
                width=16
            ).pack(side="right", padx=(0, 2))
        elif is_selected:
            ctk.CTkLabel(
                row, text="▶",
                font=("Segoe UI", 10),
                text_color="#66bb6a",
                width=16
            ).pack(side="right", padx=(0, 2))
        elif is_current:
            ctk.CTkLabel(
                row, text="▶",
                font=("Segoe UI", 10),
                text_color="#ffff00",
                width=16
            ).pack(side="right", padx=(0, 2))
        
        # ACTION BUTTONS (right side)
        
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
            command=lambda idx=loop_idx, ln=loop.name: self._on_rename_vamp_row(idx, ln)
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
        
        # Play/Jump button
        btn_play = ctk.CTkButton(
            row, text="▶", width=28, height=22,
            fg_color=COLOR_BTN_SUCCESS if is_selected else COLOR_BG_LIGHT,
            hover_color=COLOR_BTN_SUCCESS,
            text_color="#ffffff" if is_selected else COLOR_TEXT,
            command=lambda idx=loop_idx: self._on_jump_to_vamp_row(idx)
        )
        btn_play.pack(side="right", padx=2)
        ToolTip(btn_play, "Jump to this vamp and start playing")
        
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
    
    def _on_rename_marker(self, marker_id, current_name):
        dialog = ctk.CTkInputDialog(
            text="Rename cue point:",
            title="Rename Cue"
        )
        new_name = dialog.get_input()
        if new_name and new_name.strip() and self.on_rename_marker:
            self.on_rename_marker(marker_id, new_name.strip())
    
    def _on_delete_marker(self, marker_id):
        if self.on_delete_marker:
            self.on_delete_marker(marker_id)
    
    def _on_prev(self):
        """Jump to previous item (marker or vamp) in timeline."""
        # Build unified sorted list
        items = []
        for marker in self._markers:
            items.append((marker.time, 'marker', marker.id, None))
        for i, loop in enumerate(self._loops):
            items.append((loop.start, 'vamp', None, i))
        
        items.sort(key=lambda x: x[0])
        
        if not items:
            return
        
        # Find previous item before current position
        for i in range(len(items) - 1, -1, -1):
            time, item_type, marker_id, loop_idx = items[i]
            if time < self._current_position - 0.5:  # Must be at least 0.5s back
                if item_type == 'marker':
                    self.on_jump_to_marker(marker_id)
                else:
                    self.on_jump_to_vamp(loop_idx)
                return

    def _on_next(self):
        """Jump to next item (marker or vamp) in timeline."""
        # Build unified sorted list
        items = []
        for marker in self._markers:
            items.append((marker.time, 'marker', marker.id, None))
        for i, loop in enumerate(self._loops):
            items.append((loop.start, 'vamp', None, i))
        
        items.sort(key=lambda x: x[0])
        
        if not items:
            return
        
        # Find next item after current position
        for time, item_type, marker_id, loop_idx in items:
            if time > self._current_position + 0.1:  # Small buffer
                if item_type == 'marker':
                    self.on_jump_to_marker(marker_id)
                else:
                    self.on_jump_to_vamp(loop_idx)
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
    
    def _on_rename_vamp_row(self, loop_idx, current_name):
        """Rename a vamp (clicking ✏ button)."""
        dialog = ctk.CTkInputDialog(
            text="Rename vamp:",
            title="Rename Vamp"
        )
        new_name = dialog.get_input()
        if new_name and new_name.strip() and self.on_rename_vamp:
            self.on_rename_vamp(loop_idx, new_name.strip())
    
    def _on_delete_vamp_row(self, loop_idx):
        """Delete a vamp (clicking ✕ button)."""
        if self.on_delete_vamp:
            self.on_delete_vamp(loop_idx)
