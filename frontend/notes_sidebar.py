"""
Notes Sidebar for Loop Station.

A right-side panel that shows:
- Current cue/vamp details with per-tag notes
- Next cue/vamp preview with countdown timer
- Editable tag cards: each tag has its own notes with Save/Edit toggle

This gives rehearsal directors at-a-glance information about
where they are in the show and what's coming up next.
"""

import logging
import time
import tkinter as tk
from typing import Callable, Optional, List

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
    Right sidebar displaying current/next cue details with per-tag notes.
    """
    
    def __init__(
        self,
        parent: tk.Widget,
        on_tag_note_save: Optional[Callable[[str, str, str], None]] = None,  # (item_id, tag, text)
        on_tag_remove: Optional[Callable[[str, str], None]] = None,          # (item_id, tag)
        **kwargs
    ):
        super().__init__(parent, fg_color=COLOR_BG_MEDIUM, corner_radius=0, **kwargs)
        
        self.on_tag_note_save = on_tag_note_save
        self.on_tag_remove = on_tag_remove
        
        # State
        self._timeline_items = []
        self._current_position = 0.0
        self._current_item = None
        self._next_item = None
        self._current_item_type = None
        self._next_item_type = None
        self._last_countdown_update = 0
        
        # Tag card widgets (managed dynamically)
        self._tag_cards = []
        
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
        
        # ==========================================================
        # SECTION 1: NOW
        # ==========================================================
        self._section_header(self.scroll, "NOW")
        
        self.current_frame = ctk.CTkFrame(
            self.scroll, fg_color="#1e2a1e", corner_radius=8
        )
        self.current_frame.pack(fill="x", padx=PADDING_SMALL, pady=(0, PADDING_MEDIUM))
        
        self.current_header = ctk.CTkFrame(self.current_frame, fg_color="transparent")
        self.current_header.pack(fill="x", padx=PADDING_SMALL, pady=(PADDING_SMALL, 0))
        
        self.current_icon = ctk.CTkLabel(
            self.current_header, text="--", font=("Segoe UI", 16), width=30
        )
        self.current_icon.pack(side="left")
        
        self.current_name = ctk.CTkLabel(
            self.current_header, text="No active cue",
            font=("Segoe UI", 15, "bold"), text_color=COLOR_TEXT,
            wraplength=RIGHT_SIDEBAR_WIDTH - 80
        )
        self.current_name.pack(side="left", padx=5, fill="x", expand=True)
        
        self.current_time = ctk.CTkLabel(
            self.current_frame, text="",
            font=("Consolas", 11), text_color=COLOR_TEXT_DIM
        )
        self.current_time.pack(anchor="w", padx=PADDING_MEDIUM, pady=(2, 0))
        
        # Container for per-tag note summaries in NOW section
        self.current_tags_notes = ctk.CTkFrame(self.current_frame, fg_color="transparent")
        self.current_tags_notes.pack(fill="x", padx=PADDING_SMALL, pady=(4, PADDING_SMALL))
        
        # ==========================================================
        # SECTION 2: UP NEXT (with countdown)
        # ==========================================================
        self._section_header(self.scroll, "UP NEXT")
        
        self.next_frame = ctk.CTkFrame(
            self.scroll, fg_color="#2a2a1e", corner_radius=8
        )
        self.next_frame.pack(fill="x", padx=PADDING_SMALL, pady=(0, PADDING_MEDIUM))
        
        # Big countdown
        self.countdown_label = ctk.CTkLabel(
            self.next_frame, text="--:--",
            font=("Consolas", 36, "bold"), text_color="#ffcc00"
        )
        self.countdown_label.pack(pady=(PADDING_SMALL, 2))
        
        ctk.CTkLabel(
            self.next_frame, text="until next cue",
            font=("Segoe UI", 9), text_color=COLOR_TEXT_DIM
        ).pack(pady=(0, 4))
        
        self.next_header = ctk.CTkFrame(self.next_frame, fg_color="transparent")
        self.next_header.pack(fill="x", padx=PADDING_SMALL, pady=(0, 2))
        
        self.next_icon = ctk.CTkLabel(
            self.next_header, text="", font=("Segoe UI", 14), width=30
        )
        self.next_icon.pack(side="left")
        
        self.next_name = ctk.CTkLabel(
            self.next_header, text="--",
            font=("Segoe UI", 13, "bold"), text_color=COLOR_TEXT,
            wraplength=RIGHT_SIDEBAR_WIDTH - 80
        )
        self.next_name.pack(side="left", padx=5, fill="x", expand=True)
        
        self.next_time = ctk.CTkLabel(
            self.next_frame, text="",
            font=("Consolas", 10), text_color=COLOR_TEXT_DIM
        )
        self.next_time.pack(anchor="w", padx=PADDING_MEDIUM, pady=(0, 2))
        
        # Container for per-tag note summaries in NEXT section
        self.next_tags_notes = ctk.CTkFrame(self.next_frame, fg_color="transparent")
        self.next_tags_notes.pack(fill="x", padx=PADDING_SMALL, pady=(0, PADDING_SMALL))
        
        # ==========================================================
        # SECTION 3: EDIT TAGS & NOTES
        # ==========================================================
        self._section_header(self.scroll, "EDIT TAGS & NOTES")
        
        self.edit_frame = ctk.CTkFrame(
            self.scroll, fg_color=COLOR_BG_DARK, corner_radius=8
        )
        self.edit_frame.pack(fill="x", padx=PADDING_SMALL, pady=(0, PADDING_MEDIUM))
        
        # Editing target
        self.edit_target_label = ctk.CTkLabel(
            self.edit_frame, text="Select a cue to edit",
            font=("Segoe UI", 11, "bold"), text_color=COLOR_TEXT_DIM
        )
        self.edit_target_label.pack(anchor="w", padx=PADDING_MEDIUM, pady=(PADDING_SMALL, 4))
        
        # "Add Tag" dropdown
        add_tag_frame = ctk.CTkFrame(self.edit_frame, fg_color="transparent")
        add_tag_frame.pack(fill="x", padx=PADDING_SMALL, pady=(0, PADDING_SMALL))
        
        self.add_tag_var = tk.StringVar(value="+ Add Tag")
        self.add_tag_menu = ctk.CTkOptionMenu(
            add_tag_frame,
            values=AVAILABLE_TAGS,
            width=140, height=28,
            font=("Segoe UI", 11, "bold"),
            command=self._on_add_tag,
            fg_color="#336633",
            button_color="#336633",
            text_color="#ffffff",
            dropdown_fg_color="#336633",
            dropdown_text_color="#ffffff",
            dropdown_hover_color="#448844",
            variable=self.add_tag_var
        )
        self.add_tag_menu.pack(side="left")
        
        # Container for tag cards
        self.cards_frame = ctk.CTkFrame(self.edit_frame, fg_color="transparent")
        self.cards_frame.pack(fill="x", padx=PADDING_SMALL, pady=(0, PADDING_SMALL))
    
    # =========================================================================
    # HELPERS
    # =========================================================================
    
    def _section_header(self, parent, text):
        ctk.CTkLabel(
            parent, text=text,
            font=("Segoe UI", 10, "bold"), text_color=COLOR_TEXT_DIM
        ).pack(anchor="w", padx=PADDING_MEDIUM, pady=(PADDING_MEDIUM, 4))
    
    # =========================================================================
    # TIMELINE / POSITION UPDATES
    # =========================================================================
    
    def update_timeline(self, markers, loops):
        """Rebuild internal timeline from current markers and loops."""
        items = []
        for marker in (markers or []):
            items.append((marker.time, 'marker', marker))
        for loop in (loops or []):
            items.append((loop.start, 'vamp', loop))
        items.sort(key=lambda x: x[0])
        self._timeline_items = items
        self._evaluate_cues(self._current_position)
    
    def update_position(self, position, is_playing=True):
        """Update current playback position."""
        self._current_position = position
        
        now = time.time()
        if now - self._last_countdown_update < 0.25:
            self._update_countdown_only(position)
            return
        
        self._last_countdown_update = now
        self._evaluate_cues(position)
    
    def _evaluate_cues(self, position):
        """Determine current and next cue, update displays."""
        if not self._timeline_items:
            self._set_empty()
            return
        
        current = None
        current_type = None
        next_item = None
        next_type = None
        
        # Pass 1: Find CURRENT item (last item we're inside/past)
        for i, (t, item_type, obj) in enumerate(self._timeline_items):
            item_time = obj.time if item_type == 'marker' else obj.start
            
            if item_time <= position:
                if item_type == 'vamp' and position > obj.end:
                    continue  # We've passed this vamp entirely
                current = obj
                current_type = item_type
        
        # Pass 2: Find NEXT item (first item whose start is after current position)
        for i, (t, item_type, obj) in enumerate(self._timeline_items):
            if item_type == 'marker':
                item_time = obj.time
            else:
                item_time = obj.start
            
            if item_time > position:
                next_item = obj
                next_type = item_type
                break
            
            # Also catch vamps we're before the end of but already past start
            if item_type == 'vamp' and position <= obj.end and obj is not current:
                next_item = obj
                next_type = item_type
                break
        
        old_id = self._current_item.id if self._current_item else None
        self._current_item = current
        self._current_item_type = current_type
        self._next_item = next_item
        self._next_item_type = next_type
        
        self._refresh_current(position)
        self._refresh_next(position)
        
        new_id = current.id if current else None
        if new_id != old_id:
            self._rebuild_tag_cards()
    
    def _update_countdown_only(self, position):
        """Fast path: just update countdown number."""
        if self._next_item:
            next_time = self._next_item.time if self._next_item_type == 'marker' else self._next_item.start
            remaining = max(0, next_time - position)
            self.countdown_label.configure(text=self._fmt_countdown(remaining))
            if remaining < 5:
                self.countdown_label.configure(text_color="#ff4444")
            elif remaining < 15:
                self.countdown_label.configure(text_color="#ffcc00")
            else:
                self.countdown_label.configure(text_color="#88cc88")
    
    # =========================================================================
    # DISPLAY: NOW SECTION
    # =========================================================================
    
    def _set_empty(self):
        self.current_icon.configure(text="--")
        self.current_name.configure(text="No active cue")
        self.current_time.configure(text="")
        self._clear_children(self.current_tags_notes)
        self.countdown_label.configure(text="--:--", text_color="#555555")
        self.next_icon.configure(text="")
        self.next_name.configure(text="--")
        self.next_time.configure(text="")
        self._clear_children(self.next_tags_notes)
    
    def _refresh_current(self, position):
        item = self._current_item
        itype = self._current_item_type
        
        if item is None:
            self.current_icon.configure(text="--")
            self.current_name.configure(text="No active cue")
            self.current_time.configure(text="")
            self.current_frame.configure(fg_color="#1a1a1a")
            self._clear_children(self.current_tags_notes)
            return
        
        if itype == 'marker':
            self.current_icon.configure(text="📍")
            self.current_time.configure(
                text=f"at {self._fmt(item.time)}", text_color=COLOR_MARKER
            )
            self.current_frame.configure(fg_color="#2a2a1e")
        else:
            self.current_icon.configure(text="🔁")
            self.current_time.configure(
                text=f"{self._fmt(item.start)} → {self._fmt(item.end)}", text_color="#66bb6a"
            )
            self.current_frame.configure(fg_color="#1e2a1e")
        
        self.current_name.configure(text=item.name)
        self._render_tag_notes_summary(self.current_tags_notes, item.tag_notes)
    
    # =========================================================================
    # DISPLAY: NEXT SECTION
    # =========================================================================
    
    def _refresh_next(self, position):
        item = self._next_item
        itype = self._next_item_type
        
        if item is None:
            self.countdown_label.configure(text="--:--", text_color="#555555")
            self.next_icon.configure(text="")
            self.next_name.configure(text="End of song")
            self.next_time.configure(text="")
            self._clear_children(self.next_tags_notes)
            return
        
        if itype == 'marker':
            self.next_icon.configure(text="📍")
            next_time = item.time
            self.next_time.configure(
                text=f"at {self._fmt(next_time)}", text_color=COLOR_MARKER
            )
        else:
            self.next_icon.configure(text="🔁")
            next_time = item.start
            self.next_time.configure(
                text=f"{self._fmt(item.start)} → {self._fmt(item.end)}", text_color="#558855"
            )
        
        self.next_name.configure(text=item.name)
        
        remaining = max(0, next_time - position)
        self.countdown_label.configure(text=self._fmt_countdown(remaining))
        if remaining < 5:
            self.countdown_label.configure(text_color="#ff4444")
        elif remaining < 15:
            self.countdown_label.configure(text_color="#ffcc00")
        else:
            self.countdown_label.configure(text_color="#88cc88")
        
        self._render_tag_notes_summary(self.next_tags_notes, item.tag_notes)
    
    def _render_tag_notes_summary(self, parent, tag_notes):
        """Render read-only tag+notes summary in NOW or NEXT section."""
        self._clear_children(parent)
        
        if not tag_notes:
            ctk.CTkLabel(
                parent, text="(no tags)", font=("Segoe UI", 10),
                text_color=COLOR_TEXT_DIM
            ).pack(anchor="w", padx=4, pady=2)
            return
        
        for tag, notes in tag_notes.items():
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x", pady=1)
            
            color = TAG_COLORS.get(tag, "#555555")
            ctk.CTkLabel(
                row, text=f" {tag} ",
                font=("Segoe UI", 9, "bold"),
                fg_color=color, corner_radius=8,
                text_color="#ffffff", height=18
            ).pack(side="left", padx=(4, 6), pady=1)
            
            display = notes.strip().replace('\n', ' ')
            if len(display) > 60:
                display = display[:57] + "..."
            
            ctk.CTkLabel(
                row, text=display if display else "(empty)",
                font=("Segoe UI", 10),
                text_color=COLOR_TEXT if display else COLOR_TEXT_DIM,
                anchor="w"
            ).pack(side="left", fill="x", expand=True, padx=(0, 4))
    
    # =========================================================================
    # EDIT SECTION: TAG CARDS
    # =========================================================================
    
    def _on_add_tag(self, tag_name):
        """Handle tag selection from the Add Tag dropdown."""
        if self._current_item is None or tag_name == "(all tags added)":
            self.add_tag_var.set("+ Add Tag")
            return
        
        # Don't add duplicate
        if tag_name in self._current_item.tag_notes:
            self.add_tag_var.set("+ Add Tag")
            return
        
        # Add tag with empty notes
        self._current_item.tag_notes[tag_name] = ""
        
        # Save to backend
        if self.on_tag_note_save:
            self.on_tag_note_save(self._current_item.id, tag_name, "")
        
        # Reset dropdown and rebuild
        self.add_tag_var.set("+ Add Tag")
        self._rebuild_tag_cards()
        self._refresh_current(self._current_position)
    
    def _rebuild_tag_cards(self):
        """Rebuild all tag editing cards for the current item."""
        # Destroy old cards
        for card in self._tag_cards:
            card.destroy()
        self._tag_cards.clear()
        
        item = self._current_item
        
        if item is None:
            self.edit_target_label.configure(text="Select a cue to edit")
            return
        
        type_str = "Cue" if self._current_item_type == 'marker' else "Vamp"
        self.edit_target_label.configure(text=f"Editing: {type_str} — {item.name}")
        
        # Update dropdown to only show tags not yet added
        existing = set(item.tag_notes.keys())
        available = [t for t in AVAILABLE_TAGS if t not in existing]
        self.add_tag_menu.configure(values=available if available else ["(all tags added)"])
        self.add_tag_var.set("+ Add Tag")
        
        # Build a card for each tag
        for tag, notes in item.tag_notes.items():
            card = self._create_tag_card(tag, notes)
            self._tag_cards.append(card)
    
    def _create_tag_card(self, tag, notes):
        """
        Create an editable card for a single tag.
        Starts in VIEW mode (label). Edit button toggles to EDIT mode (textbox).
        Save button persists and returns to VIEW mode.
        """
        color = TAG_COLORS.get(tag, "#555555")
        
        card = ctk.CTkFrame(self.cards_frame, fg_color="#1a1a1a", corner_radius=6)
        card.pack(fill="x", pady=3)
        
        # ---- Header row: colored tag badge + Edit/Save + Delete ----
        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=4, pady=(4, 2))
        
        ctk.CTkLabel(
            header, text=f" {tag} ",
            font=("Segoe UI", 10, "bold"),
            fg_color=color, corner_radius=8,
            text_color="#ffffff", height=22
        ).pack(side="left", padx=(2, 6))
        
        # Delete tag button
        btn_delete = ctk.CTkButton(
            header, text="✕", width=26, height=22,
            fg_color="transparent", hover_color="#442222",
            text_color="#aa4444", font=("Segoe UI", 11),
            command=lambda: self._remove_tag(tag, card)
        )
        btn_delete.pack(side="right", padx=2)
        
        # Edit / Save toggle button
        btn_edit = ctk.CTkButton(
            header, text="Edit", width=50, height=22,
            fg_color=COLOR_BG_LIGHT, hover_color="#555555",
            text_color=COLOR_TEXT, font=("Segoe UI", 10),
            command=None  # Set below
        )
        btn_edit.pack(side="right", padx=2)
        
        # ---- VIEW mode: label showing saved notes ----
        view_label = ctk.CTkLabel(
            card, text=notes if notes else "(click Edit to add notes)",
            font=("Segoe UI", 11),
            text_color=COLOR_TEXT if notes else COLOR_TEXT_DIM,
            wraplength=RIGHT_SIDEBAR_WIDTH - 60,
            justify="left", anchor="w"
        )
        view_label.pack(fill="x", padx=PADDING_MEDIUM, pady=(0, PADDING_SMALL))
        
        # ---- EDIT mode: textbox container (hidden initially) ----
        edit_container = ctk.CTkFrame(card, fg_color="transparent")
        
        textbox = ctk.CTkTextbox(
            edit_container, height=70,
            font=("Segoe UI", 11),
            fg_color=COLOR_BG_MEDIUM, text_color=COLOR_TEXT,
            corner_radius=4, wrap="word"
        )
        textbox.pack(fill="x", padx=2, pady=(0, 4))
        textbox.insert("0.0", notes)
        
        # ---- Toggle logic ----
        card._editing = False
        
        def toggle_edit():
            if card._editing:
                # === SAVE ===
                new_text = textbox.get("0.0", "end").strip()
                if self._current_item:
                    self._current_item.tag_notes[tag] = new_text
                    if self.on_tag_note_save:
                        self.on_tag_note_save(self._current_item.id, tag, new_text)
                
                view_label.configure(
                    text=new_text if new_text else "(click Edit to add notes)",
                    text_color=COLOR_TEXT if new_text else COLOR_TEXT_DIM
                )
                edit_container.pack_forget()
                view_label.pack(fill="x", padx=PADDING_MEDIUM, pady=(0, PADDING_SMALL))
                btn_edit.configure(text="Edit", fg_color=COLOR_BG_LIGHT, text_color=COLOR_TEXT)
                card._editing = False
                
                # Refresh NOW display to show updated notes
                self._refresh_current(self._current_position)
            else:
                # === EDIT ===
                textbox.delete("0.0", "end")
                current_notes = ""
                if self._current_item:
                    current_notes = self._current_item.tag_notes.get(tag, "")
                textbox.insert("0.0", current_notes)
                
                view_label.pack_forget()
                edit_container.pack(fill="x", padx=PADDING_SMALL, pady=(0, PADDING_SMALL))
                btn_edit.configure(text="Save", fg_color=COLOR_BTN_SUCCESS, text_color="#ffffff")
                card._editing = True
                textbox.focus_set()
        
        btn_edit.configure(command=toggle_edit)
        
        return card
    
    def _remove_tag(self, tag, card_widget):
        """Remove a tag from the current item."""
        if self._current_item is None:
            return
        
        self._current_item.tag_notes.pop(tag, None)
        
        if self.on_tag_remove:
            self.on_tag_remove(self._current_item.id, tag)
        
        card_widget.destroy()
        if card_widget in self._tag_cards:
            self._tag_cards.remove(card_widget)
        
        # Update dropdown and NOW display
        existing = set(self._current_item.tag_notes.keys())
        available = [t for t in AVAILABLE_TAGS if t not in existing]
        self.add_tag_menu.configure(values=available if available else ["(all tags added)"])
        self.add_tag_var.set("+ Add Tag")
        
        self._refresh_current(self._current_position)
    
    # =========================================================================
    # UTILITIES
    # =========================================================================
    
    def _clear_children(self, frame):
        for w in frame.winfo_children():
            w.destroy()
    
    def _fmt(self, seconds):
        minutes = int(seconds // 60)
        secs = seconds % 60
        return f"{minutes}:{secs:05.2f}"
    
    def _fmt_countdown(self, seconds):
        if seconds <= 0:
            return "NOW"
        if seconds < 60:
            return f"{seconds:.1f}s"
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}:{secs:02d}"
