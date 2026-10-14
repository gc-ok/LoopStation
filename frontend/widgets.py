"""
Small reusable widgets shared by the Loop Station frontend.

- CollapsibleSection: a frame with a clickable header that shows/hides its
  ``content`` frame.
- after_if_alive(): schedule a Tk callback that is skipped if the widget has
  been destroyed in the meantime (safe for pop-ups the user may close early).
"""

import tkinter as tk
import customtkinter as ctk

from config import COLOR_TEXT_DIM, PADDING_SMALL


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


def after_if_alive(widget, delay_ms, callback):
    """
    Like ``widget.after(delay_ms, callback)`` but the callback is skipped when
    the widget no longer exists (for example a pop-up the user already closed).
    Returns the Tk after-id, or None if the widget is already gone.
    """
    def _run():
        try:
            if widget.winfo_exists():
                callback()
        except tk.TclError:
            pass

    try:
        return widget.after(delay_ms, _run)
    except tk.TclError:
        return None


# =============================================================================
# Optional-API helpers (sibling widgets gain methods over time)
# =============================================================================

def call_optional(obj, name, *args, **kwargs):
    """
    Call ``obj.name(*args, **kwargs)`` if that method exists. Keyword
    arguments the method does not accept are dropped, so a caller can pass
    newer arguments to an older widget. Returns the result, or None.
    """
    fn = getattr(obj, name, None) if obj is not None else None
    if not callable(fn):
        return None
    return fn(*args, **supported_kwargs(fn, kwargs))


def supported_kwargs(fn, kwargs):
    """The subset of ``kwargs`` that ``fn`` names as explicit parameters."""
    if not kwargs:
        return {}
    import inspect
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return dict(kwargs)
    return {k: v for k, v in kwargs.items()
            if k in params and params[k].kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)}


# =============================================================================
# Modal dialog base: fixed bottom action bar + scrollable content, sized to
# its content and clamped to the screen work area; Escape closes.
# =============================================================================

def screen_work_area(anchor):
    """
    Usable screen rectangle (left, top, right, bottom) in window-geometry
    pixels for the monitor showing ``anchor`` (Windows work area when it can
    be queried, otherwise the screen minus a taskbar allowance).
    """
    import sys
    fallback = (0, 0, anchor.winfo_screenwidth(), anchor.winfo_screenheight() - 80)
    if not sys.platform.startswith("win"):
        return fallback
    try:
        import ctypes
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        user32 = ctypes.windll.user32
        hmon = user32.MonitorFromWindow(anchor.winfo_id(), 2)  # DEFAULTTONEAREST
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcWork
            if r.right > r.left and r.bottom > r.top:
                return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return fallback


class ModalDialog(ctk.CTkToplevel):
    """
    Base for Loop Station dialogs (same layout rules as VampModal).

    Subclasses build their content into ``self.body`` (a scrollable frame)
    and their buttons into ``self.actions`` (always visible at the bottom),
    then call ``self.finish_layout()``. ``close()`` runs ``on_close`` once.
    """
    WIDTH = 560
    MIN_HEIGHT = 300
    TITLEBAR = 32

    def __init__(self, parent, title, on_close=None, modal=True):
        super().__init__(parent)
        self._parent = parent
        self._on_close_cb = on_close
        self._closed = False
        self.title(title)
        self.resizable(False, True)
        self.minsize(self.WIDTH, self.MIN_HEIGHT)
        self.protocol("WM_DELETE_WINDOW", self.close)
        try:
            from config import COLOR_BG_MEDIUM, COLOR_BG_DARK
        except Exception:
            COLOR_BG_MEDIUM = COLOR_BG_DARK = None
        self.transient(parent)
        if modal:
            try:
                self.grab_set()
            except tk.TclError:
                pass

        main = ctk.CTkFrame(self, fg_color=COLOR_BG_MEDIUM, corner_radius=0)
        main.pack(fill="both", expand=True)
        self._action_bar = ctk.CTkFrame(main, fg_color=COLOR_BG_DARK, corner_radius=0)
        self._action_bar.pack(side="bottom", fill="x")
        self.actions = ctk.CTkFrame(self._action_bar, fg_color="transparent")
        self.actions.pack(fill="x", pady=10, padx=20)

        self.body = ctk.CTkScrollableFrame(
            main, fg_color="transparent",
            scrollbar_button_color="#5a5a5a", scrollbar_button_hover_color="#7a7a7a",
        )
        self.body.pack(side="top", fill="both", expand=True, padx=(20, 10), pady=16)
        self.bind("<Escape>", lambda _e: self.close())
        # GO / play-pause / stop keep working while a dialog has focus.
        # Bound first so it runs before a subclass's own <KeyPress> handler.
        self.bind("<KeyPress>", self._forward_show_key, add="+")

    def _forward_show_key(self, event):
        """Hand show keys (GO, play/pause, stop) to the app unless typing."""
        if getattr(self, "_capture_action", None) is not None:
            return None     # the settings dialog is capturing a new key
        handler = getattr(self._parent, "handle_show_key", None)
        if handler is None:
            return None
        try:
            if handler(event):
                return "break"
        except Exception:
            import logging
            logging.getLogger("LoopStation.Widgets").exception("show key forwarding failed")
        return None

    def finish_layout(self):
        self.fit_to_screen(center_on=self._parent)
        try:
            self.lift()
            self.focus_force()
        except tk.TclError:
            pass

    def fit_to_screen(self, center_on=None):
        """Size to content (capped to the work area), centre, clamp on screen."""
        if self._closed:
            return
        try:
            self.update_idletasks()
            scaling = float(ctk.ScalingTracker.get_window_scaling(self)) or 1.0
            content_h = self.body.winfo_reqheight() + round(32 * scaling)
            desired_h = content_h + self._action_bar.winfo_reqheight() + round(4 * scaling)
            width = round(self.WIDTH * scaling)
            titlebar_h = round(self.TITLEBAR * scaling)
            left, top, right, bottom = screen_work_area(center_on or self)
            max_h = (bottom - top) - titlebar_h
            height = max(min(desired_h, max_h), min(round(self.MIN_HEIGHT * scaling), max_h))
            if center_on is not None:
                px = center_on.winfo_rootx() + (center_on.winfo_width() - width) // 2
                py = center_on.winfo_rooty() + (center_on.winfo_height() - height) // 2
            else:
                px, py = self.winfo_x(), self.winfo_y()
            px = max(left, min(px, right - width))
            py = max(top, min(py, bottom - height - titlebar_h))
            self.geometry(f"{round(width / scaling)}x{round(height / scaling)}+{px}+{py}")
        except tk.TclError:
            pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.grab_release()
        except tk.TclError:
            pass
        cb = self._on_close_cb
        try:
            self.destroy()
        except tk.TclError:
            pass
        if cb is not None:
            try:
                cb()
            except Exception:
                import logging
                logging.getLogger("LoopStation.Widgets").exception("on_close failed")

    def section(self, title):
        """A bold section heading in the body; returns a content frame under it."""
        try:
            from config import COLOR_TEXT
        except Exception:
            COLOR_TEXT = None
        ctk.CTkLabel(self.body, text=title, font=("Segoe UI", 13, "bold"),
                     text_color=COLOR_TEXT, anchor="w").pack(fill="x", pady=(12, 4))
        frame = ctk.CTkFrame(self.body, fg_color="transparent")
        frame.pack(fill="x")
        return frame


# Outlined style for secondary buttons (visible on every theme, where the
# "light" background can equal the panel colour).
SECONDARY_BTN = dict(fg_color="transparent", border_width=1,
                     border_color="#5a5a5a", hover_color="#3a3a3a")
