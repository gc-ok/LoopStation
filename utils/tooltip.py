"""
Tooltip utility for Loop Station.

Provides hover-over tooltips for any widget. Styled to match the dark theme.
Cross-platform safe: handles widget destruction mid-hover gracefully.

Usage:
    from utils.tooltip import ToolTip
    ToolTip(some_button, "Play / Pause (Space)")
"""

import sys
import tkinter as tk


# Cross-platform font: Segoe UI (Windows), SF Pro (macOS), sans-serif (Linux)
if sys.platform == "darwin":
    _TIP_FONT = ("SF Pro Text", 11)
elif sys.platform == "win32":
    _TIP_FONT = ("Segoe UI", 10)
else:
    _TIP_FONT = ("Sans", 10)


class ToolTip:
    """
    Lightweight tooltip that appears on hover after a short delay.
    Automatically positions itself near the widget, avoiding screen edges.
    
    Safe with dynamically destroyed widgets (e.g. cue sheet row rebuilds).
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
        # FIX: Wrap in try/except because some CTk widgets (like SegmentedButton) 
        # raise NotImplementedError for bind().
        try:
            widget.bind("<Enter>", self._on_enter, add="+")
            widget.bind("<Leave>", self._on_leave, add="+")
            widget.bind("<ButtonPress>", self._on_leave, add="+")
        except NotImplementedError:
            # Widget doesn't support binding. Fail gracefully so app doesn't crash.
            # (The tooltip just won't show for this specific widget)
            print(f"Warning: Tooltip not supported on {widget.__class__.__name__}")
            return
        except AttributeError:
             # Some widgets might not even have a bind method
            return

    def update_text(self, new_text):
        """Update the tooltip text dynamically."""
        self.text = new_text

    def _on_enter(self, event=None):
        self._cancel()
        try:
            self._after_id = self.widget.after(self.delay, self._show)
        except tk.TclError:
            pass  # Widget was destroyed between event dispatch and handler

    def _on_leave(self, event=None):
        self._cancel()
        self._hide()

    def _cancel(self):
        if self._after_id:
            try:
                self.widget.after_cancel(self._after_id)
            except tk.TclError:
                pass  # Widget already destroyed — safe to ignore
            self._after_id = None

    def _show(self):
        if self._tip_window or not self.text:
            return

        # Guard: make sure the widget still exists
        try:
            if not self.widget.winfo_exists():
                return
        except tk.TclError:
            return

        # Dismiss any other active tooltip
        if ToolTip._active_tip and ToolTip._active_tip is not self:
            ToolTip._active_tip._hide()
        ToolTip._active_tip = self

        # Position: below the widget, slightly right
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        except tk.TclError:
            return  # Widget destroyed during positioning

        # Create borderless top-level window
        tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)  # No window decorations

        # Platform-specific topmost handling
        if sys.platform == "darwin":
            # macOS: set window level to floating
            try:
                tw.wm_attributes("-topmost", True)
                # On macOS, tooltips sometimes appear behind the app.
                # Lift after a short delay to ensure visibility.
                tw.lift()
            except tk.TclError:
                pass
        else:
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
            font=_TIP_FONT,
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
            try:
                self._tip_window.destroy()
            except tk.TclError:
                pass  # Already gone
            self._tip_window = None
        if ToolTip._active_tip is self:
            ToolTip._active_tip = None
