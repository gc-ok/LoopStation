"""
Tooltip utility for Loop Station.

Provides hover-over tooltips for any widget. Styled to match the dark theme.

Usage:
    from utils.tooltip import ToolTip
    ToolTip(some_button, "Play / Pause (Space)")
"""

import tkinter as tk


class ToolTip:
    """
    Lightweight tooltip that appears on hover after a short delay.
    Automatically positions itself near the widget, avoiding screen edges.
    """

    _active_tip = None  # Class-level: only one tooltip visible at a time

    def __init__(self, widget, text, delay=400):
        """
        Args:
            widget: The Tk/CTk widget to attach the tooltip to.
            text: The tooltip text to display.
            delay: Milliseconds before the tooltip appears (default 400ms).
        """
        self.widget = widget
        self.text = text
        self.delay = delay
        self._tip_window = None
        self._after_id = None

        # Bind hover events — use add="+" so we don't clobber existing bindings
        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        widget.bind("<ButtonPress>", self._on_leave, add="+")

    def update_text(self, new_text):
        """Update the tooltip text dynamically."""
        self.text = new_text

    def _on_enter(self, event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay, self._show)

    def _on_leave(self, event=None):
        self._cancel()
        self._hide()

    def _cancel(self):
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self):
        if self._tip_window or not self.text:
            return

        # Dismiss any other active tooltip
        if ToolTip._active_tip and ToolTip._active_tip is not self:
            ToolTip._active_tip._hide()
        ToolTip._active_tip = self

        # Position: below the widget, slightly right of cursor
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4

        # Create borderless top-level window
        tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)  # No window decorations
        tw.wm_attributes("-topmost", True)

        # Style the tooltip
        frame = tk.Frame(
            tw, background="#2a2a3a", borderwidth=1, relief="solid",
            highlightbackground="#555566", highlightthickness=1
        )
        frame.pack()

        label = tk.Label(
            frame,
            text=self.text,
            background="#2a2a3a",
            foreground="#e0e0e0",
            font=("Segoe UI", 10),
            padx=8,
            pady=4,
            justify="left",
            wraplength=300,
        )
        label.pack()

        # Ensure tooltip doesn't go off-screen
        tw.update_idletasks()
        tip_w = tw.winfo_reqwidth()
        tip_h = tw.winfo_reqheight()
        screen_w = self.widget.winfo_screenwidth()
        screen_h = self.widget.winfo_screenheight()

        if x + tip_w > screen_w - 10:
            x = screen_w - tip_w - 10
        if y + tip_h > screen_h - 10:
            # Show above the widget instead
            y = self.widget.winfo_rooty() - tip_h - 4

        tw.wm_geometry(f"+{x}+{y}")
        self._tip_window = tw

    def _hide(self):
        if self._tip_window:
            self._tip_window.destroy()
            self._tip_window = None
        if ToolTip._active_tip is self:
            ToolTip._active_tip = None
