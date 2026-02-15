"""
Notes Sidebar for Loop Station.

A right-side panel that shows:
- Current cue/vamp details (name, type, tags, notes)
- Next cue/vamp preview with countdown timer
- Editable notes and tags for the current item

This gives rehearsal directors at-a-glance information about
where they are in the show and what's coming up next.
"""

import logging
import time
import tkinter as tk
from typing import Callable, Optional, List, Tuple

import customtkinter as ctk

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import (
    COLOR_BG_DARK, COLOR_BG_MEDIUM, COLOR_BG_LIGHT,
    COLOR_TEXT, COLOR_TEXT_DIM, COLOR_MARKER, COLOR_LOOP_REGION,
    COLOR_BTN_PRIMARY, COLOR_BTN_SUCCESS, COLOR_BTN_DANGER,
    PADDING_SMALL, PADDING_MEDIUM, PADDING_LARGE,
    AVAILABLE_TAGS, TAG_COLORS, COLOR_BTN_TEXT,
    RIGHT_SIDEBAR_WIDTH,
)

logger = logging.getLogger("LoopStation.NotesSidebar")


class NotesSidebar(ctk.CTkFrame):
    """
    Right sidebar displaying current/next cue details, notes, and tags.
    
    Receives position updates to compute which cue is current and
    what the countdown to the next cue is.
    """
    
    def __init__(
        self,
        parent: tk.Widget,
        on_notes_change: Optional[Callable[[str, str], None]] = None,  # (item_id, notes_text)
        on_tags_change: Optional[Callable[[str, list], None]] = None,  # (item_id, tags_list)
        **kwargs
    ):
        super().__init__(parent, fg_color=COLOR_BG_MEDIUM, corner_radius=0, **kwargs)
        
        self.on_notes_change = on_notes_change
        self.on_tags_change = on_tags_change
        
        # State
        self._timeline_items = []       # Sorted list of (time, type, object)
        self._current_position = 0.0
        self._current_item = None       # Currently active item object
        self._next_item = None          # Next upcoming item object
        self._current_item_type = None  # 'marker' or 'vamp'
        self._next_item_type = None
        self._is_playing = False
        self._last_countdown_update = 0
        
        # Debounce for notes saving
        self._notes_save_timer = None
        
        self._create_widgets()
    
    def _create_widgets(self):
        """Build the sidebar UI."""
        
        # Title bar
        title_frame = ctk.CTkFrame(self, fg_color="#1a1a2e", corner_radius=0, height=40)
        title_frame.pack(fill="x")
        title_frame.pack_propagate(False)
        
        ctk.CTkLabel(
            title_frame, text="CUE DETAILS",
            font=("Segoe UI", 13, "bold"),
            text_color=COLOR_TEXT
        ).pack(side="left", padx=PADDING_MEDIUM, pady=8)
        
        # Scrollable content
        self.scroll = ctk.CTkScrollableFrame(
            self, fg_color="transparent",
            scrollbar_button_color=COLOR_BG_LIGHT
        )
        self.scroll.pack(fill="both", expand=True, padx=0, pady=0)
        
        # =====================================================
        # SECTION 1: CURRENT CUE
        # =====================================================
        self._create_section_header(self.scroll, "NOW")
        
        self.current_frame = ctk.CTkFrame(
            self.scroll, fg_color="#1e2a1e", corner_radius=8
        )
        self.current_frame.pack(fill="x", padx=PADDING_SMALL, pady=(0, PADDING_MEDIUM))
        
        # Current cue icon + name
        self.current_header = ctk.CTkFrame(self.current_frame, fg_color="transparent")
        self.current_header.pack(fill="x", padx=PADDING_SMALL, pady=(PADDING_SMALL, 0))
        
        self.current_icon = ctk.CTkLabel(
            self.current_header, text="--",
            font=("Segoe UI", 16), width=30
        )
        self.current_icon.pack(side="left")
        
        self.current_name = ctk.CTkLabel(
            self.current_header, text="No active cue",
            font=("Segoe UI", 15, "bold"),
            text_color=COLOR_TEXT,
            wraplength=RIGHT_SIDEBAR_WIDTH - 80
        )
        self.current_name.pack(side="left", padx=5, fill="x", expand=True)
        
        # Current cue time
        self.current_time = ctk.CTkLabel(
            self.current_frame, text="",
            font=("Consolas", 11),
            text_color=COLOR_TEXT_DIM
        )
        self.current_time.pack(anchor="w", padx=PADDING_MEDIUM, pady=(2, 0))
        
        # Current tags display
        self.current_tags_frame = ctk.CTkFrame(self.current_frame, fg_color="transparent")
        self.current_tags_frame.pack(fill="x", padx=PADDING_SMALL, pady=(4, 0))
        
        # Current notes display (read-only view)
        self.current_notes_label = ctk.CTkLabel(
            self.current_frame, text="",
            font=("Segoe UI", 11),
            text_color="#bbddbb",
            wraplength=RIGHT_SIDEBAR_WIDTH - 40,
            justify="left",
            anchor="w"
        )
        self.current_notes_label.pack(
            fill="x", padx=PADDING_MEDIUM, pady=(4, PADDING_SMALL)
        )
        
        # =====================================================
        # SECTION 2: NEXT CUE (with countdown)
        # =====================================================
        self._create_section_header(self.scroll, "UP NEXT")
        
        self.next_frame = ctk.CTkFrame(
            self.scroll, fg_color="#2a2a1e", corner_radius=8
        )
        self.next_frame.pack(fill="x", padx=PADDING_SMALL, pady=(0, PADDING_MEDIUM))
        
        # Countdown timer (big, prominent)
        self.countdown_label = ctk.CTkLabel(
            self.next_frame, text="--:--",
            font=("Consolas", 36, "bold"),
            text_color="#ffcc00"
        )
        self.countdown_label.pack(pady=(PADDING_SMALL, 2))
        
        ctk.CTkLabel(
            self.next_frame, text="until next cue",
            font=("Segoe UI", 9),
            text_color=COLOR_TEXT_DIM
        ).pack(pady=(0, 4))
        
        # Next cue name + icon
        self.next_header = ctk.CTkFrame(self.next_frame, fg_color="transparent")
        self.next_header.pack(fill="x", padx=PADDING_SMALL, pady=(0, 2))
        
        self.next_icon = ctk.CTkLabel(
            self.next_header, text="",
            font=("Segoe UI", 14), width=30
        )
        self.next_icon.pack(side="left")
        
        self.next_name = ctk.CTkLabel(
            self.next_header, text="--",
            font=("Segoe UI", 13, "bold"),
            text_color=COLOR_TEXT,
            wraplength=RIGHT_SIDEBAR_WIDTH - 80
        )
        self.next_name.pack(side="left", padx=5, fill="x", expand=True)
        
        # Next cue time
        self.next_time = ctk.CTkLabel(
            self.next_frame, text="",
            font=("Consolas", 10),
            text_color=COLOR_TEXT_DIM
        )
        self.next_time.pack(anchor="w", padx=PADDING_MEDIUM, pady=(0, 2))
        
        # Next tags
        self.next_tags_frame = ctk.CTkFrame(self.next_frame, fg_color="transparent")
        self.next_tags_frame.pack(fill="x", padx=PADDING_SMALL, pady=(0, 2))
        
        # Next notes preview
        self.next_notes_label = ctk.CTkLabel(
            self.next_frame, text="",
            font=("Segoe UI", 10),
            text_color="#bbbb88",
            wraplength=RIGHT_SIDEBAR_WIDTH - 40,
            justify="left",
            anchor="w"
        )
        self.next_notes_label.pack(
            fill="x", padx=PADDING_MEDIUM, pady=(0, PADDING_SMALL)
        )
        
        # =====================================================
        # SECTION 3: EDIT NOTES & TAGS (for current item)
        # =====================================================
        self._create_section_header(self.scroll, "EDIT NOTES & TAGS")
        
        self.edit_frame = ctk.CTkFrame(
            self.scroll, fg_color=COLOR_BG_DARK, corner_radius=8
        )
        self.edit_frame.pack(fill="x", padx=PADDING_SMALL, pady=(0, PADDING_MEDIUM))
        
        # Editing target label
        self.edit_target_label = ctk.CTkLabel(
            self.edit_frame, text="Select a cue to edit",
            font=("Segoe UI", 11, "bold"),
            text_color=COLOR_TEXT_DIM
        )
        self.edit_target_label.pack(anchor="w", padx=PADDING_MEDIUM, pady=(PADDING_SMALL, 4))
        
        # Tags toggle buttons
        ctk.CTkLabel(
            self.edit_frame, text="Tags:",
            font=("Segoe UI", 10, "bold"),
            text_color=COLOR_TEXT_DIM
        ).pack(anchor="w", padx=PADDING_MEDIUM, pady=(4, 2))
        
        self.tags_button_frame = ctk.CTkFrame(self.edit_frame, fg_color="transparent")
        self.tags_button_frame.pack(fill="x", padx=PADDING_SMALL, pady=(0, 6))
        
        self._tag_buttons = {}
        self._tag_states = {}
        
        # Create tag toggle buttons in a wrapping grid
        for i, tag in enumerate(AVAILABLE_TAGS):
            color = TAG_COLORS.get(tag, "#555555")
            self._tag_states[tag] = False
            
            btn = ctk.CTkButton(
                self.tags_button_frame,
                text=tag,
                width=70,
                height=24,
                font=("Segoe UI", 9),
                fg_color="#333333",
                hover_color=color,
                text_color=COLOR_TEXT_DIM,
                corner_radius=12,
                command=lambda t=tag: self._toggle_tag(t)
            )
            row = i // 4
            col = i % 4
            btn.grid(row=row, column=col, padx=2, pady=2, sticky="ew")
            self._tag_buttons[tag] = btn
        
        # Make columns expand evenly
        for c in range(4):
            self.tags_button_frame.grid_columnconfigure(c, weight=1)
        
        # Notes text area
        ctk.CTkLabel(
            self.edit_frame, text="Notes:",
            font=("Segoe UI", 10, "bold"),
            text_color=COLOR_TEXT_DIM
        ).pack(anchor="w", padx=PADDING_MEDIUM, pady=(6, 2))
        
        self.notes_textbox = ctk.CTkTextbox(
            self.edit_frame,
            height=100,
            font=("Segoe UI", 11),
            fg_color=COLOR_BG_MEDIUM,
            text_color=COLOR_TEXT,
            corner_radius=6,
            wrap="word"
        )
        self.notes_textbox.pack(
            fill="x", padx=PADDING_SMALL, pady=(0, PADDING_SMALL)
        )
        
        # Bind text changes (with debounce)
        self.notes_textbox.bind("<KeyRelease>", self._on_notes_key)
    
    # =========================================================================
    # SECTION HEADER HELPER
    # =========================================================================
    
    def _create_section_header(self, parent, text):
        """Create a small section header label."""
        ctk.CTkLabel(
            parent, text=text,
            font=("Segoe UI", 10, "bold"),
            text_color=COLOR_TEXT_DIM
        ).pack(anchor="w", padx=PADDING_MEDIUM, pady=(PADDING_MEDIUM, 4))
    
    # =========================================================================
    # DATA UPDATES
    # =========================================================================
    
    def update_timeline(self, markers, loops):
        """
        Rebuild the internal timeline from current markers and loops.
        Called when markers_changed or loops_changed fires.
        """
        items = []
        for marker in (markers or []):
            items.append((marker.time, 'marker', marker))
        for loop in (loops or []):
            items.append((loop.start, 'vamp', loop))
        items.sort(key=lambda x: x[0])
        self._timeline_items = items
        
        # Re-evaluate current/next based on stored position
        self._evaluate_cues(self._current_position)
    
    def update_position(self, position, is_playing=True):
        """
        Update current playback position.
        Called from the position_update event (throttled in app.py).
        """
        self._current_position = position
        self._is_playing = is_playing
        
        # Throttle full re-evaluation to ~4fps for the countdown
        now = time.time()
        if now - self._last_countdown_update < 0.25:
            # Just update countdown number without full rebuild
            self._update_countdown_only(position)
            return
        
        self._last_countdown_update = now
        self._evaluate_cues(position)
    
    def _evaluate_cues(self, position):
        """
        Determine which cue is current and which is next.
        Updates both display sections.
        """
        if not self._timeline_items:
            self._set_no_cue()
            return
        
        current = None
        current_type = None
        next_item = None
        next_type = None
        
        for i, (t, item_type, obj) in enumerate(self._timeline_items):
            if item_type == 'marker':
                item_time = obj.time
            else:
                item_time = obj.start
            
            if item_time <= position:
                # This is a candidate for "current"
                # For vamps, also check if we're still within the range
                if item_type == 'vamp' and position > obj.end:
                    continue  # We've passed this vamp entirely
                current = obj
                current_type = item_type
                
                # Next is the item after this one (if any)
                if i + 1 < len(self._timeline_items):
                    next_item = self._timeline_items[i + 1][2]
                    next_type = self._timeline_items[i + 1][1]
                else:
                    next_item = None
                    next_type = None
            elif current is None and item_time > position:
                # We're before the first cue — show first cue as "next"
                next_item = obj
                next_type = item_type
                break
        
        # Update displays
        old_id = self._current_item.id if self._current_item else None
        
        self._current_item = current
        self._current_item_type = current_type
        self._next_item = next_item
        self._next_item_type = next_type
        
        self._refresh_current_display()
        self._refresh_next_display(position)
        
        # If current item changed, update the edit section
        new_id = current.id if current else None
        if new_id != old_id:
            self._refresh_edit_section()
    
    def _update_countdown_only(self, position):
        """Fast path: just update the countdown number."""
        if self._next_item:
            if self._next_item_type == 'marker':
                next_time = self._next_item.time
            else:
                next_time = self._next_item.start
            
            remaining = max(0, next_time - position)
            self.countdown_label.configure(text=self._format_countdown(remaining))
            
            # Color based on urgency
            if remaining < 5:
                self.countdown_label.configure(text_color="#ff4444")
            elif remaining < 15:
                self.countdown_label.configure(text_color="#ffcc00")
            else:
                self.countdown_label.configure(text_color="#88cc88")
    
    # =========================================================================
    # DISPLAY REFRESH
    # =========================================================================
    
    def _set_no_cue(self):
        """Reset to empty state."""
        self.current_icon.configure(text="--")
        self.current_name.configure(text="No active cue")
        self.current_time.configure(text="")
        self.current_notes_label.configure(text="")
        self._clear_tags_display(self.current_tags_frame)
        
        self.countdown_label.configure(text="--:--")
        self.next_icon.configure(text="")
        self.next_name.configure(text="--")
        self.next_time.configure(text="")
        self.next_notes_label.configure(text="")
        self._clear_tags_display(self.next_tags_frame)
    
    def _refresh_current_display(self):
        """Update the 'NOW' section with current item details."""
        item = self._current_item
        item_type = self._current_item_type
        
        if item is None:
            self.current_icon.configure(text="--")
            self.current_name.configure(text="No active cue")
            self.current_time.configure(text="")
            self.current_notes_label.configure(text="")
            self.current_frame.configure(fg_color="#1a1a1a")
            self._clear_tags_display(self.current_tags_frame)
            return
        
        if item_type == 'marker':
            self.current_icon.configure(text="📍")
            self.current_time.configure(
                text=f"at {self._format_time(item.time)}",
                text_color=COLOR_MARKER
            )
            self.current_frame.configure(fg_color="#2a2a1e")
        else:
            self.current_icon.configure(text="🔁")
            self.current_time.configure(
                text=f"{self._format_time(item.start)} → {self._format_time(item.end)}",
                text_color="#66bb6a"
            )
            self.current_frame.configure(fg_color="#1e2a1e")
        
        self.current_name.configure(text=item.name)
        self.current_notes_label.configure(
            text=item.notes if item.notes else "(no notes)"
        )
        
        # Show tags
        self._render_tags(self.current_tags_frame, getattr(item, 'tags', []))
    
    def _refresh_next_display(self, position):
        """Update the 'UP NEXT' section."""
        item = self._next_item
        item_type = self._next_item_type
        
        if item is None:
            self.countdown_label.configure(text="--:--", text_color="#555555")
            self.next_icon.configure(text="")
            self.next_name.configure(text="End of song")
            self.next_time.configure(text="")
            self.next_notes_label.configure(text="")
            self._clear_tags_display(self.next_tags_frame)
            return
        
        if item_type == 'marker':
            self.next_icon.configure(text="📍")
            next_time = item.time
            self.next_time.configure(
                text=f"at {self._format_time(next_time)}",
                text_color=COLOR_MARKER
            )
        else:
            self.next_icon.configure(text="🔁")
            next_time = item.start
            self.next_time.configure(
                text=f"{self._format_time(item.start)} → {self._format_time(item.end)}",
                text_color="#558855"
            )
        
        self.next_name.configure(text=item.name)
        self.next_notes_label.configure(
            text=item.notes if item.notes else ""
        )
        
        # Countdown
        remaining = max(0, next_time - position)
        self.countdown_label.configure(text=self._format_countdown(remaining))
        
        if remaining < 5:
            self.countdown_label.configure(text_color="#ff4444")
        elif remaining < 15:
            self.countdown_label.configure(text_color="#ffcc00")
        else:
            self.countdown_label.configure(text_color="#88cc88")
        
        # Tags
        self._render_tags(self.next_tags_frame, getattr(item, 'tags', []))
    
    def _refresh_edit_section(self):
        """Update the edit section to reflect the current item."""
        item = self._current_item
        
        if item is None:
            self.edit_target_label.configure(text="Select a cue to edit")
            self.notes_textbox.delete("0.0", "end")
            for tag in AVAILABLE_TAGS:
                self._set_tag_button_state(tag, False)
            return
        
        type_str = "Cue" if self._current_item_type == 'marker' else "Vamp"
        self.edit_target_label.configure(text=f"Editing: {type_str} — {item.name}")
        
        # Update notes textbox (without triggering save)
        self.notes_textbox.delete("0.0", "end")
        if item.notes:
            self.notes_textbox.insert("0.0", item.notes)
        
        # Update tag buttons
        item_tags = getattr(item, 'tags', [])
        for tag in AVAILABLE_TAGS:
            self._set_tag_button_state(tag, tag in item_tags)
    
    # =========================================================================
    # TAG RENDERING
    # =========================================================================
    
    def _render_tags(self, frame, tags):
        """Render tag badges in a frame."""
        self._clear_tags_display(frame)
        
        if not tags:
            return
        
        for tag in tags:
            color = TAG_COLORS.get(tag, "#555555")
            badge = ctk.CTkLabel(
                frame, text=tag,
                font=("Segoe UI", 9, "bold"),
                fg_color=color,
                corner_radius=10,
                text_color="#ffffff",
                width=60, height=20
            )
            badge.pack(side="left", padx=2, pady=2)
    
    def _clear_tags_display(self, frame):
        """Remove all tag badges from a frame."""
        for w in frame.winfo_children():
            w.destroy()
    
    # =========================================================================
    # TAG EDITING
    # =========================================================================
    
    def _toggle_tag(self, tag):
        """Toggle a tag on/off for the current item."""
        if self._current_item is None:
            return
        
        current_tags = list(getattr(self._current_item, 'tags', []))
        
        if tag in current_tags:
            current_tags.remove(tag)
            self._set_tag_button_state(tag, False)
        else:
            current_tags.append(tag)
            self._set_tag_button_state(tag, True)
        
        self._current_item.tags = current_tags
        
        # Update displays immediately
        self._render_tags(self.current_tags_frame, current_tags)
        
        # Notify backend
        if self.on_tags_change:
            self.on_tags_change(self._current_item.id, current_tags)
    
    def _set_tag_button_state(self, tag, active):
        """Update a tag button's visual state."""
        self._tag_states[tag] = active
        btn = self._tag_buttons.get(tag)
        if not btn:
            return
        
        color = TAG_COLORS.get(tag, "#555555")
        if active:
            btn.configure(
                fg_color=color,
                text_color="#ffffff",
                font=("Segoe UI", 9, "bold")
            )
        else:
            btn.configure(
                fg_color="#333333",
                text_color=COLOR_TEXT_DIM,
                font=("Segoe UI", 9)
            )
    
    # =========================================================================
    # NOTES EDITING
    # =========================================================================
    
    def _on_notes_key(self, event=None):
        """Handle keypress in notes textbox with debounce."""
        if self._current_item is None:
            return
        
        # Cancel previous timer
        if self._notes_save_timer is not None:
            try:
                self.after_cancel(self._notes_save_timer)
            except Exception:
                pass
        
        # Set new timer (save after 800ms of no typing)
        self._notes_save_timer = self.after(800, self._save_notes)
    
    def _save_notes(self):
        """Actually save the notes content."""
        if self._current_item is None:
            return
        
        text = self.notes_textbox.get("0.0", "end").strip()
        self._current_item.notes = text
        
        # Update the read-only display
        self.current_notes_label.configure(
            text=text if text else "(no notes)"
        )
        
        # Notify backend
        if self.on_notes_change:
            self.on_notes_change(self._current_item.id, text)
    
    # =========================================================================
    # FORMATTING
    # =========================================================================
    
    def _format_time(self, seconds):
        """Format seconds as M:SS.ss"""
        minutes = int(seconds // 60)
        secs = seconds % 60
        return f"{minutes}:{secs:05.2f}"
    
    def _format_countdown(self, seconds):
        """Format countdown as M:SS or SS.s depending on size."""
        if seconds <= 0:
            return "NOW"
        
        if seconds < 60:
            return f"{seconds:.1f}s"
        
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}:{secs:02d}"
