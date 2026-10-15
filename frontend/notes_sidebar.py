"""
Notes Sidebar for Loop Station.

A right-side panel that shows:
- Current cue/vamp details with per-tag notes
- Next cue/vamp preview with countdown timer
- Editable tag cards: each tag has its own notes with Save/Edit toggle
- Hold cues (caesuras) count as cues; acks from crew devices are shown
  under NOW / UP NEXT; live mode makes the notes read-only

This gives rehearsal directors at-a-glance information about
where they are in the show and what's coming up next.
"""

import logging
import time
import tkinter as tk
from typing import Callable, Optional

import customtkinter as ctk

from utils.fonts import FONT_UI, FONT_MONO
from config import (
    COLOR_BORDER, COLOR_BTN_DANGER, COLOR_HOVER, COLOR_TEXT_DISABLED, COLOR_VAMP, tint,
    COLOR_BG_MEDIUM, COLOR_BG_LIGHT,
    COLOR_TEXT, COLOR_TEXT_DIM, COLOR_MARKER,
    COLOR_BTN_SUCCESS,
    PADDING_SMALL, PADDING_MEDIUM,
    AVAILABLE_TAGS, TAG_COLORS,
    RIGHT_SIDEBAR_WIDTH,
)
from utils.formatting import format_time
from utils.tooltip import ToolTip

logger = logging.getLogger("LoopStation.NotesSidebar")

# Hold cues: same colour as the cue sheet / waveform hold markers.
COLOR_HOLD = "#5ec8f2"
COLOR_ACK = "#7ddc8a"
COLOR_LIVE = "#ff9f43"

TYPE_ICONS = {'marker': "◆", 'vamp': "↻", 'hold': "⏸"}
TYPE_LABELS = {'marker': "Cue", 'vamp': "Vamp", 'hold': "Hold"}


def item_start_time(item_type, obj):
    """Start time of a timeline item (markers/holds: .time, vamps: .start)."""
    return obj.start if item_type == 'vamp' else obj.time


def ack_devices(acks, cue_id):
    """Unique device names (first-ack order) that acknowledged ``cue_id``."""
    names = []
    for ack in (acks or {}).get(cue_id) or []:
        name = ack.get("device") if isinstance(ack, dict) else ack
        name = str(name or "").strip()
        if name and name not in names:
            names.append(name)
    return names


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
        self._last_countdown_update = -1e9
        self._acks = {}
        self._live_mode = False
        self._playback_rate = 1.0
        self._countdown_to = None

        # Tag card widgets (managed dynamically). The edit section always
        # shows the cards for ``_edit_item``; that may lag behind
        # ``_current_item`` while a card is open for editing, because we
        # never destroy a card the user is typing in.
        self._tag_cards = []
        self._edit_item = None
        self._edit_item_type = None
        self._cards_rebuild_pending = False

        self._create_widgets()
    
    def _create_widgets(self):
        """Build the sidebar UI."""
        
        # Title bar
        title_frame = ctk.CTkFrame(self, fg_color=COLOR_BG_MEDIUM, corner_radius=0, height=40)
        title_frame.pack(fill="x")
        title_frame.pack_propagate(False)
        
        ctk.CTkLabel(
            title_frame, text="CUE DETAILS",
            font=(FONT_UI, 13, "bold"),
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
            self.scroll, fg_color=COLOR_BG_LIGHT, corner_radius=8
        )
        self.current_frame.pack(fill="x", padx=PADDING_SMALL, pady=(0, PADDING_MEDIUM))
        
        self.current_header = ctk.CTkFrame(self.current_frame, fg_color="transparent")
        self.current_header.pack(fill="x", padx=PADDING_SMALL, pady=(PADDING_SMALL, 0))
        
        self.current_icon = ctk.CTkLabel(
            self.current_header, text="--", font=(FONT_UI, 16), width=30
        )
        self.current_icon.pack(side="left")
        
        self.current_name = ctk.CTkLabel(
            self.current_header, text="No active cue",
            font=(FONT_UI, 15, "bold"), text_color=COLOR_TEXT,
            wraplength=RIGHT_SIDEBAR_WIDTH - 80
        )
        self.current_name.pack(side="left", padx=5, fill="x", expand=True)
        
        self.current_time = ctk.CTkLabel(
            self.current_frame, text="",
            font=(FONT_MONO, 11), text_color=COLOR_TEXT_DIM
        )
        self.current_time.pack(anchor="w", padx=PADDING_MEDIUM, pady=(2, 0))
        
        # Container for per-tag note summaries in NOW section
        self.current_tags_notes = ctk.CTkFrame(self.current_frame, fg_color="transparent")
        self.current_tags_notes.pack(fill="x", padx=PADDING_SMALL, pady=(4, PADDING_SMALL))

        # "✓ Acked by: ..." (packed only when the shown cue has acks)
        self.current_acks = ctk.CTkLabel(
            self.current_frame, text="", font=(FONT_UI, 10, "bold"),
            text_color=COLOR_ACK, anchor="w", justify="left",
            wraplength=RIGHT_SIDEBAR_WIDTH - 50
        )
        
        # ==========================================================
        # SECTION 2: UP NEXT (with countdown)
        # ==========================================================
        self._section_header(self.scroll, "UP NEXT")
        
        self.next_frame = ctk.CTkFrame(
            self.scroll, fg_color=COLOR_BG_LIGHT, corner_radius=8
        )
        self.next_frame.pack(fill="x", padx=PADDING_SMALL, pady=(0, PADDING_MEDIUM))
        
        # Big countdown
        self.countdown_label = ctk.CTkLabel(
            self.next_frame, text="--:--",
            font=(FONT_MONO, 36, "bold"), text_color=COLOR_TEXT_DISABLED
        )
        self.countdown_label.pack(pady=(PADDING_SMALL, 2))
        
        ctk.CTkLabel(
            self.next_frame, text="until next cue",
            font=(FONT_UI, 9), text_color=COLOR_TEXT_DIM
        ).pack(pady=(0, 4))
        
        self.next_header = ctk.CTkFrame(self.next_frame, fg_color="transparent")
        self.next_header.pack(fill="x", padx=PADDING_SMALL, pady=(0, 2))
        
        self.next_icon = ctk.CTkLabel(
            self.next_header, text="", font=(FONT_UI, 14), width=30
        )
        self.next_icon.pack(side="left")
        
        self.next_name = ctk.CTkLabel(
            self.next_header, text="--",
            font=(FONT_UI, 13, "bold"), text_color=COLOR_TEXT,
            wraplength=RIGHT_SIDEBAR_WIDTH - 80
        )
        self.next_name.pack(side="left", padx=5, fill="x", expand=True)
        
        self.next_time = ctk.CTkLabel(
            self.next_frame, text="",
            font=(FONT_MONO, 10), text_color=COLOR_TEXT_DIM
        )
        self.next_time.pack(anchor="w", padx=PADDING_MEDIUM, pady=(0, 2))
        
        # Container for per-tag note summaries in NEXT section
        self.next_tags_notes = ctk.CTkFrame(self.next_frame, fg_color="transparent")
        self.next_tags_notes.pack(fill="x", padx=PADDING_SMALL, pady=(0, PADDING_SMALL))

        self.next_acks = ctk.CTkLabel(
            self.next_frame, text="", font=(FONT_UI, 10, "bold"),
            text_color=COLOR_ACK, anchor="w", justify="left",
            wraplength=RIGHT_SIDEBAR_WIDTH - 50
        )
        
        # ==========================================================
        # SECTION 3: EDIT TAGS & NOTES
        # ==========================================================
        self._section_header(self.scroll, "EDIT TAGS & NOTES")
        
        self.edit_frame = ctk.CTkFrame(
            self.scroll, fg_color=COLOR_BG_LIGHT, corner_radius=8
        )
        self.edit_frame.pack(fill="x", padx=PADDING_SMALL, pady=(0, PADDING_MEDIUM))
        
        # Editing target
        self.edit_target_label = ctk.CTkLabel(
            self.edit_frame, text="Select a cue to edit",
            font=(FONT_UI, 11, "bold"), text_color=COLOR_TEXT_DIM,
            anchor="w", justify="left", wraplength=RIGHT_SIDEBAR_WIDTH - 60
        )
        self.edit_target_label.pack(anchor="w", fill="x", padx=PADDING_MEDIUM,
                                    pady=(PADDING_SMALL, 4))
        
        # "Add Tag" dropdown
        add_tag_frame = ctk.CTkFrame(self.edit_frame, fg_color="transparent")
        add_tag_frame.pack(fill="x", padx=PADDING_SMALL, pady=(0, PADDING_SMALL))
        self._add_tag_frame = add_tag_frame

        # Shown instead of the Add Tag menu while live
        self.live_note = ctk.CTkLabel(
            self.edit_frame, text="LIVE · notes are read-only",
            font=(FONT_UI, 10, "bold"), text_color=COLOR_LIVE
        )
        
        self.add_tag_var = tk.StringVar(value="+ Add Tag")
        self.add_tag_menu = ctk.CTkOptionMenu(
            add_tag_frame,
            values=AVAILABLE_TAGS,
            width=140, height=28,
            font=(FONT_UI, 11, "bold"),
            command=self._on_add_tag,
            fg_color=COLOR_BORDER,
            button_color=COLOR_BORDER,
            button_hover_color=COLOR_HOVER,
            text_color=COLOR_TEXT,
            dropdown_fg_color=COLOR_BG_LIGHT,
            dropdown_text_color=COLOR_TEXT,
            dropdown_hover_color=COLOR_HOVER,
            variable=self.add_tag_var
        )
        self.add_tag_menu.pack(side="left")
        ToolTip(self.add_tag_menu, "Add a tag category (Lighting, Sound, etc.) to the current cue")
        
        # Container for tag cards
        self.cards_frame = ctk.CTkFrame(self.edit_frame, fg_color="transparent")
        self.cards_frame.pack(fill="x", padx=PADDING_SMALL, pady=(0, PADDING_SMALL))
    
    # =========================================================================
    # HELPERS
    # =========================================================================
    
    def _section_header(self, parent, text):
        ctk.CTkLabel(
            parent, text=text,
            font=(FONT_UI, 10, "bold"), text_color=COLOR_TEXT_DIM
        ).pack(anchor="w", padx=PADDING_MEDIUM, pady=(PADDING_MEDIUM, 4))
    
    # =========================================================================
    # TIMELINE / POSITION UPDATES
    # =========================================================================
    
    def update_timeline(self, markers, loops, holds=None):
        """Rebuild internal timeline from current markers, loops and holds."""
        items = []
        for marker in (markers or []):
            items.append((marker.time, 'marker', marker))
        for loop in (loops or []):
            items.append((loop.start, 'vamp', loop))
        for hold in (holds or []):
            items.append((hold.time, 'hold', hold))
        items.sort(key=lambda x: x[0])
        self._timeline_items = items
        self._evaluate_cues(self._current_position)
    
    def update_position(self, position, is_playing=True):
        """Update current playback position."""
        self._current_position = position
        
        now = time.monotonic()      # wall-clock jumps must not freeze the countdown
        if now - self._last_countdown_update < 0.25:
            self._update_countdown_only(position)
            return
        
        self._last_countdown_update = now
        self._evaluate_cues(position)

    def set_playback_timing(self, rate, countdown_to=None):
        self._playback_rate = max(0.01, float(rate))
        self._countdown_to = countdown_to

    def _remaining(self, next_time, position):
        if self._countdown_to is not None:
            return self._countdown_to(next_time, position)
        return max(0, next_time - position) / self._playback_rate
    
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
            item_time = item_start_time(item_type, obj)

            if item_time <= position:
                if item_type == 'vamp' and position > obj.end:
                    continue  # We've passed this vamp entirely
                current = obj
                current_type = item_type
        
        # Pass 2: Find NEXT item (first item whose start is after current position)
        for i, (t, item_type, obj) in enumerate(self._timeline_items):
            item_time = item_start_time(item_type, obj)

            if item_time > position:
                next_item = obj
                next_type = item_type
                break
            
            # Also catch vamps we're before the end of but already past start
            if item_type == 'vamp' and position <= obj.end and obj is not current:
                next_item = obj
                next_type = item_type
                break
        
        # Compare by object identity: the backend hands us the same objects
        # on every emission, and a reload that recreates them (same ids,
        # fresh objects) must refresh too.
        current_changed = current is not self._current_item
        next_changed = next_item is not self._next_item

        self._current_item = current
        self._current_item_type = current_type
        self._next_item = next_item
        self._next_item_type = next_type

        # Only rebuild the tag summary widgets when the item changes,
        # NOT on every position tick — this prevents the visible flash/flicker.
        if current_changed:
            self._refresh_current(position)

        if next_changed:
            self._refresh_next(position)

        # Update countdown (lightweight, no widget destruction)
        self._update_countdown_only(position)

        if current_changed:
            # Deferred automatically if a card is open for editing.
            self._rebuild_tag_cards()
    
    def _update_countdown_only(self, position):
        """Fast path: just update countdown number."""
        if self._next_item:
            next_time = item_start_time(self._next_item_type, self._next_item)
            remaining = self._remaining(next_time, position)
            if remaining is None:
                self.countdown_label.configure(text="Waiting for release", text_color="#ffcc00")
                return
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
        self._current_item = None
        self._current_item_type = None
        self._next_item = None
        self._next_item_type = None
        self.current_icon.configure(text="--")
        self.current_name.configure(text="No active cue")
        self.current_time.configure(text="")
        self._clear_children(self.current_tags_notes)
        self.countdown_label.configure(text="--:--", text_color=COLOR_TEXT_DISABLED)
        self.next_icon.configure(text="")
        self.next_name.configure(text="--")
        self.next_time.configure(text="")
        self._clear_children(self.next_tags_notes)
        self._refresh_acks()
        if self._edit_item is not None:
            self._rebuild_tag_cards()

    def _refresh_current(self, position):
        item = self._current_item
        itype = self._current_item_type
        
        if item is None:
            self.current_icon.configure(text="--")
            self.current_name.configure(text="No active cue")
            self.current_time.configure(text="")
            self.current_frame.configure(fg_color=COLOR_BG_LIGHT)
            self._clear_children(self.current_tags_notes)
            self._refresh_acks()
            return

        if itype == 'marker':
            self.current_icon.configure(text="◆", text_color=COLOR_MARKER)
            self.current_time.configure(
                text=f"at {self._fmt(item.time)}", text_color=COLOR_MARKER
            )
            self.current_frame.configure(fg_color=tint(COLOR_MARKER, 0.10))
        elif itype == 'hold':
            self.current_icon.configure(text="⏸", text_color=COLOR_HOLD)
            self.current_time.configure(
                text=f"Hold at {self._fmt(item.time)}", text_color=COLOR_HOLD
            )
            self.current_frame.configure(fg_color=tint(COLOR_HOLD, 0.12))
        else:
            self.current_icon.configure(text="↻", text_color=COLOR_VAMP)
            self.current_time.configure(
                text=f"{self._fmt(item.start)} → {self._fmt(item.end)}", text_color=COLOR_VAMP
            )
            self.current_frame.configure(fg_color=tint(COLOR_VAMP, 0.12))
        
        self.current_name.configure(text=item.name)
        self._render_tag_notes_summary(self.current_tags_notes, item.tag_notes)
        self._refresh_acks()
    
    # =========================================================================
    # DISPLAY: NEXT SECTION
    # =========================================================================
    
    def _refresh_next(self, position):
        item = self._next_item
        itype = self._next_item_type
        
        if item is None:
            self.countdown_label.configure(text="--:--", text_color=COLOR_TEXT_DISABLED)
            self.next_icon.configure(text="")
            self.next_name.configure(text="End of song")
            self.next_time.configure(text="")
            self._clear_children(self.next_tags_notes)
            self._refresh_acks()
            return

        if itype == 'marker':
            self.next_icon.configure(text="◆", text_color=COLOR_MARKER)
            next_time = item.time
            self.next_time.configure(
                text=f"at {self._fmt(next_time)}", text_color=COLOR_MARKER
            )
        elif itype == 'hold':
            self.next_icon.configure(text="⏸", text_color=COLOR_HOLD)
            next_time = item.time
            self.next_time.configure(
                text=f"Hold at {self._fmt(next_time)}", text_color=COLOR_HOLD
            )
        else:
            self.next_icon.configure(text="↻", text_color=COLOR_VAMP)
            next_time = item.start
            self.next_time.configure(
                text=f"{self._fmt(item.start)} → {self._fmt(item.end)}", text_color=COLOR_VAMP
            )
        
        self.next_name.configure(text=item.name)
        
        remaining = self._remaining(next_time, position)
        remaining = 0 if remaining is None else remaining
        self.countdown_label.configure(text=self._fmt_countdown(remaining))
        if remaining < 5:
            self.countdown_label.configure(text_color="#ff4444")
        elif remaining < 15:
            self.countdown_label.configure(text_color="#ffcc00")
        else:
            self.countdown_label.configure(text_color="#88cc88")
        
        self._render_tag_notes_summary(self.next_tags_notes, item.tag_notes)
        self._refresh_acks()

    # =========================================================================
    # ACKS
    # =========================================================================

    def update_acks(self, acks):
        """acks: {cue_id: [{"device": str, "ts": float}, ...]} from crew devices."""
        self._acks = dict(acks or {})
        self._refresh_acks()

    def _refresh_acks(self):
        """Show / hide the "✓ Acked by" line under the NOW and UP NEXT cards."""
        for label, item, anchor in (
            (self.current_acks, self._current_item, self.current_tags_notes),
            (self.next_acks, self._next_item, self.next_tags_notes),
        ):
            names = ack_devices(self._acks, item.id) if item is not None else []
            try:
                if names:
                    label.configure(text="✓ Acked by: " + ", ".join(names))
                    label.pack(anchor="w", fill="x", padx=PADDING_MEDIUM,
                               pady=(0, PADDING_SMALL), after=anchor)
                else:
                    label.pack_forget()
            except tk.TclError:
                pass

    # =========================================================================
    # LIVE MODE
    # =========================================================================

    def set_live_mode(self, enabled: bool):
        """Lock tag/notes editing (read-only cards, no Add Tag) while live."""
        enabled = bool(enabled)
        if enabled == self._live_mode:
            return
        if enabled:
            # Keep whatever is being typed right now.
            self._commit_open_editors()
        self._live_mode = enabled
        if enabled:
            self._add_tag_frame.pack_forget()
            self.live_note.pack(anchor="w", padx=PADDING_MEDIUM, pady=(0, PADDING_SMALL),
                                before=self.cards_frame)
        else:
            self.live_note.pack_forget()
            self._add_tag_frame.pack(fill="x", padx=PADDING_SMALL, pady=(0, PADDING_SMALL),
                                     before=self.cards_frame)
        self._rebuild_tag_cards(force=True)

    def _render_tag_notes_summary(self, parent, tag_notes):
        """Render read-only tag+notes summary in NOW or NEXT section."""
        self._clear_children(parent)
        
        if not tag_notes:
            ctk.CTkLabel(
                parent, text="(no tags)", font=(FONT_UI, 10),
                text_color=COLOR_TEXT_DIM
            ).pack(anchor="w", padx=4, pady=2)
            return
        
        for tag, notes in tag_notes.items():
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x", pady=1)
            
            color = TAG_COLORS.get(tag, "#555555")
            ctk.CTkLabel(
                row, text=f" {tag} ",
                font=(FONT_UI, 9, "bold"),
                fg_color=color, corner_radius=8,
                text_color="#ffffff", height=18
            ).pack(side="left", padx=(4, 6), pady=1)
            
            # Show the whole note, wrapped: a crew member reading this
            # backstage needs the full instruction, not a truncated one.
            display = notes.strip()

            ctk.CTkLabel(
                row, text=display if display else "(empty)",
                font=(FONT_UI, 10),
                text_color=COLOR_TEXT if display else COLOR_TEXT_DIM,
                anchor="w", justify="left",
                wraplength=RIGHT_SIDEBAR_WIDTH - 110
            ).pack(side="left", fill="x", expand=True, padx=(0, 4))
    
    # =========================================================================
    # EDIT SECTION: TAG CARDS
    # =========================================================================
    
    def _on_add_tag(self, tag_name):
        """Handle tag selection from the Add Tag dropdown."""
        # The dropdown belongs to the item the cards are showing, which is
        # ``_edit_item`` (not necessarily the playback-current item).
        item = self._edit_item
        self.add_tag_var.set("+ Add Tag")
        if item is None or tag_name == "(all tags added)" or self._live_mode:
            return

        # Don't add duplicate
        if tag_name in item.tag_notes:
            return

        # Anything being typed in another card is committed first so the
        # rebuild below cannot lose it.
        self._commit_open_editors()

        # The backend owns the item: it adds the tag, emits and saves.
        if self.on_tag_note_save:
            self.on_tag_note_save(item.id, tag_name, "")

        self._rebuild_tag_cards(force=True)
        if item is self._current_item:
            self._refresh_current(self._current_position)

    def _any_card_editing(self) -> bool:
        return any(getattr(card, "_editing", False) for card in self._tag_cards)

    def _commit_open_editors(self):
        """Save every card that is currently in edit mode."""
        for card in list(self._tag_cards):
            if getattr(card, "_editing", False):
                card._commit()

    def _on_card_edit_ended(self):
        """A card left edit mode; run any rebuild that was deferred meanwhile."""
        if self._cards_rebuild_pending and not self._any_card_editing():
            self._rebuild_tag_cards()

    def _rebuild_tag_cards(self, force: bool = False):
        """
        Rebuild all tag editing cards for the current item.

        If a card is open for editing the rebuild is deferred until that
        edit ends (see _on_card_edit_ended), so the user's text is never
        destroyed under them. ``force`` commits open editors first instead.
        """
        if self._any_card_editing():
            if not force:
                self._cards_rebuild_pending = True
                return
            self._commit_open_editors()
        self._cards_rebuild_pending = False

        # Destroy old cards
        for card in self._tag_cards:
            card.destroy()
        self._tag_cards.clear()

        item = self._current_item
        self._edit_item = item
        self._edit_item_type = self._current_item_type

        if item is None:
            self.edit_target_label.configure(text="Select a cue to edit")
            self.add_tag_menu.configure(values=AVAILABLE_TAGS)
            self.add_tag_var.set("+ Add Tag")
            return

        type_str = TYPE_LABELS.get(self._current_item_type, "Cue")
        verb = "Notes" if self._live_mode else "Editing"
        self.edit_target_label.configure(text=f"{verb}: {type_str} — {item.name}")

        self._refresh_add_tag_menu(item)

        # Build a card for each tag
        for tag, notes in item.tag_notes.items():
            card = self._create_tag_card(item, tag, notes)
            self._tag_cards.append(card)

    def _refresh_add_tag_menu(self, item):
        """Offer only the tags the item does not have yet."""
        existing = set(item.tag_notes.keys())
        available = [t for t in AVAILABLE_TAGS if t not in existing]
        self.add_tag_menu.configure(values=available if available else ["(all tags added)"])
        self.add_tag_var.set("+ Add Tag")

    def _create_tag_card(self, item, tag, notes):
        """
        Create an editable card for a single tag of ``item``.
        Starts in VIEW mode (label). Edit button toggles to EDIT mode (textbox).
        Save button (or focus leaving the textbox) persists and returns to
        VIEW mode. The card writes to ``item`` -- the object it was built
        for -- even if the playback-current cue has moved on since.
        """
        color = TAG_COLORS.get(tag, "#555555")

        card = ctk.CTkFrame(self.cards_frame, fg_color=COLOR_BG_MEDIUM, corner_radius=6)
        card.pack(fill="x", pady=3)
        
        # ---- Header row: colored tag badge + Edit/Save + Delete ----
        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=4, pady=(4, 2))
        
        ctk.CTkLabel(
            header, text=f" {tag} ",
            font=(FONT_UI, 10, "bold"),
            fg_color=color, corner_radius=8,
            text_color="#ffffff", height=22
        ).pack(side="left", padx=(2, 6))

        if self._live_mode:
            # Read-only card: no Edit / Delete while live.
            card._editing = False
            card._commit = lambda: None
            ctk.CTkLabel(
                card, text=notes if notes else "(no notes)",
                font=(FONT_UI, 11),
                text_color=COLOR_TEXT if notes else COLOR_TEXT_DIM,
                wraplength=RIGHT_SIDEBAR_WIDTH - 60,
                justify="left", anchor="w"
            ).pack(fill="x", padx=PADDING_MEDIUM, pady=(0, PADDING_SMALL))
            return card

        # Delete tag button
        btn_delete = ctk.CTkButton(
            header, text="✕", width=26, height=22,
            fg_color="transparent", hover_color=tint(COLOR_BTN_DANGER, 0.3),
            text_color="#aa4444", font=(FONT_UI, 11),
            command=lambda: self._remove_tag(item, tag, card)
        )
        btn_delete.pack(side="right", padx=2)
        
        # Edit / Save toggle button
        btn_edit = ctk.CTkButton(
            header, text="Edit", width=50, height=22,
            fg_color=COLOR_BORDER, hover_color=COLOR_HOVER,
            text_color=COLOR_TEXT, font=(FONT_UI, 10),
            command=None  # Set below
        )
        btn_edit.pack(side="right", padx=2)
        
        # ---- VIEW mode: label showing saved notes ----
        view_label = ctk.CTkLabel(
            card, text=notes if notes else "(click Edit to add notes)",
            font=(FONT_UI, 11),
            text_color=COLOR_TEXT if notes else COLOR_TEXT_DIM,
            wraplength=RIGHT_SIDEBAR_WIDTH - 60,
            justify="left", anchor="w"
        )
        view_label.pack(fill="x", padx=PADDING_MEDIUM, pady=(0, PADDING_SMALL))
        
        # ---- EDIT mode: textbox container (hidden initially) ----
        edit_container = ctk.CTkFrame(card, fg_color="transparent")
        
        textbox = ctk.CTkTextbox(
            edit_container, height=70,
            font=(FONT_UI, 11),
            fg_color=COLOR_BG_MEDIUM, text_color=COLOR_TEXT,
            corner_radius=4, wrap="word"
        )
        textbox.pack(fill="x", padx=2, pady=(0, 4))
        textbox.insert("0.0", notes)
        
        # ---- Toggle logic ----
        # ``item`` is captured here: Save always writes to the cue the card
        # was opened for, never to whatever is "current" at click time.
        card._editing = False

        def commit():
            """Leave edit mode, persisting the text to ``item``."""
            if not card._editing:
                return
            card._editing = False
            try:
                if not textbox.winfo_exists():
                    return
                new_text = textbox.get("0.0", "end").strip()
            except tk.TclError:
                # Card torn down while focus was leaving it; nothing to save.
                return

            if self.on_tag_note_save:
                self.on_tag_note_save(item.id, tag, new_text)

            try:
                view_label.configure(
                    text=new_text if new_text else "(click Edit to add notes)",
                    text_color=COLOR_TEXT if new_text else COLOR_TEXT_DIM
                )
                edit_container.pack_forget()
                view_label.pack(fill="x", padx=PADDING_MEDIUM, pady=(0, PADDING_SMALL))
                btn_edit.configure(text="Edit", fg_color=COLOR_BG_LIGHT, text_color=COLOR_TEXT)
            except tk.TclError:
                logger.debug("Tag card destroyed during commit", exc_info=True)

            # Refresh NOW display to show updated notes (if this is the current cue)
            if item is self._current_item:
                self._refresh_current(self._current_position)

            self._on_card_edit_ended()

        def begin_edit():
            if self._live_mode:
                return
            textbox.delete("0.0", "end")
            textbox.insert("0.0", item.tag_notes.get(tag, ""))

            view_label.pack_forget()
            edit_container.pack(fill="x", padx=PADDING_SMALL, pady=(0, PADDING_SMALL))
            btn_edit.configure(text="Save", fg_color=COLOR_BTN_SUCCESS, text_color="#ffffff")
            card._editing = True
            textbox.focus_set()

        def toggle_edit():
            if card._editing:
                commit()
            else:
                begin_edit()

        card._commit = commit
        btn_edit.configure(command=toggle_edit)
        # Save on focus-out so nothing is lost if the user clicks elsewhere.
        textbox.bind("<FocusOut>", lambda e: commit())

        return card

    def _remove_tag(self, item, tag, card_widget):
        """Remove ``tag`` from ``item`` (the cue the card was built for)."""
        if self._live_mode:
            return
        card_widget._editing = False  # discard any in-progress text

        # The backend owns the item: it removes the tag, emits and saves.
        if self.on_tag_remove:
            self.on_tag_remove(item.id, tag)

        card_widget.destroy()
        if card_widget in self._tag_cards:
            self._tag_cards.remove(card_widget)

        # Update dropdown and NOW display
        if item is self._edit_item:
            self._refresh_add_tag_menu(item)
        if item is self._current_item:
            self._refresh_current(self._current_position)

        self._on_card_edit_ended()
    
    # =========================================================================
    # UTILITIES
    # =========================================================================
    
    def _clear_children(self, frame):
        for w in frame.winfo_children():
            w.destroy()
    
    @staticmethod
    def _fmt(seconds):
        return format_time(seconds)
    
    def _fmt_countdown(self, seconds):
        if seconds <= 0:
            return "NOW"
        if seconds < 60:
            return f"{seconds:.1f}s"
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}:{secs:02d}"
