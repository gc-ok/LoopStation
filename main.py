#!/usr/bin/env python3
"""
Loop Station - Audio Loop Player with Seamless Looping

A professional audio loop player that provides mathematically-perfect
seamless loops by pre-processing audio in RAM.

Usage:
    python main.py [--ffmpeg PATH] [--debug]
"""

import os
import sys

# Windowed frozen builds (PyInstaller --windowed, pythonw.exe) have no
# console: sys.stdout / sys.stderr are None and any library that writes to
# them would crash. Point them at the null device instead.
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8')

# Must be done BEFORE importing pygame (which happens in backend imports)
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "1"

import argparse
import ctypes
import logging
import threading
from logging.handlers import RotatingFileHandler
import shutil

try:
    import ctypes.wintypes  # Windows work-area lookup; harmless elsewhere
except Exception:  # pragma: no cover
    pass
import tkinter as tk
from tkinter import ttk

from utils.fonts import FONT_UI
from utils.paths import get_base_path, get_asset_path, get_log_dir
from frontend import __version__

# Third-party packages the app cannot run without (checked before they are
# imported so a missing one produces a readable message, not a traceback).
REQUIRED_PACKAGES = ('pygame', 'numpy', 'matplotlib', 'customtkinter', 'PIL')


# =============================================================================
# HELPER: CENTER WINDOW & HIGH DPI FIX
# =============================================================================
def make_dpi_aware():
    """Fixes blurry UI and incorrect positioning on Windows."""
    if os.name != 'nt':
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def get_work_area(window):
    """
    Usable screen area in physical pixels (excludes the Windows taskbar).
    Falls back to the full screen minus a margin elsewhere.
    """
    if os.name == 'nt':
        try:
            rect = ctypes.wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
                return rect.right - rect.left, rect.bottom - rect.top
        except Exception:
            pass
    return window.winfo_screenwidth(), window.winfo_screenheight() - 80


def center_window(window, width, height):
    """
    Size a window to at most the usable screen area and centre it.

    `width`/`height` are the desired size in the window's own geometry units
    (customtkinter windows scale geometry() by the DPI factor, plain Tk
    windows do not), so on a high-DPI laptop the default 1400x800 would
    otherwise overflow the screen and hide the bottom and right edges.
    """
    window.update_idletasks()
    work_w, work_h = get_work_area(window)

    scaling = 1.0
    if hasattr(window, "_apply_geometry_scaling"):  # customtkinter window
        try:
            from customtkinter import ScalingTracker
            scaling = float(ScalingTracker.get_window_scaling(window)) or 1.0
        except Exception:
            scaling = 1.0

    title_bar = 40  # physical pixels reserved for the OS title bar
    max_w = int(work_w / scaling) - 16
    max_h = int((work_h - title_bar) / scaling)
    width = max(300, min(width, max_w))
    height = max(200, min(height, max_h))

    # Position is in physical pixels for both window types.
    x = max(0, int((work_w - width * scaling) / 2))
    y = max(0, int((work_h - title_bar - height * scaling) / 2))
    window.geometry(f"{width}x{height}+{x}+{y}")


def show_fatal_error(title, message):
    """
    Report a startup error with a message box and on stderr.

    Error path ONLY: a throw-away tk.Tk() root is fine here because we exit
    right after. The success path must never create a plain tk.Tk() before
    ctk.CTk() — on macOS that corrupts the Tcl/Tk interpreter and the frozen
    app crashes with SIGSEGV.
    """
    print(f"{title}: {message}", file=sys.stderr)
    try:
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message, parent=root)
        root.destroy()
    except Exception:
        pass


# =============================================================================
# BOOTSTRAP SPLASH SCREEN
# =============================================================================
class BootstrapSplash(tk.Toplevel):
    """Compact startup card: app name, thin progress bar, current step."""

    STEPS = [
        ("Initializing Audio Engine...", 50),
        ("Loading User Interface...", 90),
        ("Ready!", 100),
    ]
    CARD_WIDTH = 480
    CARD_HEIGHT = 280

    def __init__(self, parent, width=None, height=None):
        # width / height are the main window's size; the card is fixed-size
        super().__init__(parent)
        self.parent = parent
        self._current_progress = 0
        self._target_progress = 0
        self._step_index = 0

        import config
        bg = config.COLOR_BG_MEDIUM
        accent = config.COLOR_BTN_PRIMARY
        text = config.COLOR_TEXT
        dim = config.COLOR_TEXT_DIM
        faint = config.mix(bg, dim, 0.6)
        track = config.COLOR_BG_LIGHT

        self.overrideredirect(True)
        self.configure(bg=bg, highlightthickness=1,
                       highlightbackground=config.COLOR_BORDER,
                       highlightcolor=config.COLOR_BORDER)
        center_window(self, self.CARD_WIDTH, self.CARD_HEIGHT)

        # Accent strip along the top edge
        tk.Frame(self, bg=accent, height=3).place(relx=0, rely=0, relwidth=1)

        tk.Label(self, text="Loop Station", font=(FONT_UI, 30, "bold"),
                 bg=bg, fg=text).place(relx=0.5, rely=0.30, anchor="center")
        tk.Label(self, text="Seamless vamps for live theater", font=(FONT_UI, 12),
                 bg=bg, fg=dim).place(relx=0.5, rely=0.45, anchor="center")

        style = ttk.Style(self)
        style.theme_use('default')
        style.configure("Splash.Horizontal.TProgressbar", background=accent,
                        troughcolor=track, bordercolor=track,
                        lightcolor=accent, darkcolor=accent, thickness=4)
        self.progress = ttk.Progressbar(self, style="Splash.Horizontal.TProgressbar",
                                        mode='determinate', maximum=100,
                                        value=0, length=320)
        self.progress.place(relx=0.5, rely=0.64, anchor="center")

        self.status = tk.Label(self, text="Starting up...", font=(FONT_UI, 11),
                               bg=bg, fg=dim)
        self.status.place(relx=0.5, rely=0.74, anchor="center")

        # Footer: maker's mark (dark themes only: the logo is white) + version
        self._add_logo(bg, dark=self._is_dark(bg))
        tk.Label(self, text=f"v{__version__}", font=(FONT_UI, 9),
                 bg=bg, fg=faint).place(relx=0.96, rely=0.93, anchor="se")

    @staticmethod
    def _is_dark(color):
        return sum(int(color[i:i + 2], 16) for i in (1, 3, 5)) < 382

    def _add_logo(self, bg, dark):
        if not dark:
            return
        try:
            from PIL import Image, ImageTk
            logo_path = get_asset_path("logo.png")
            if not os.path.exists(logo_path):
                return
            image = Image.open(logo_path).convert("RGBA")
            box = image.getbbox()           # trim the transparent padding
            if box:
                image = image.crop(box)
            target_h = 22
            ratio = target_h / image.height
            image = image.resize((max(1, int(image.width * ratio)), target_h),
                                 Image.Resampling.LANCZOS)
            # Fade the white mark so it sits quietly in the footer
            alpha = image.getchannel("A").point(lambda a: int(a * 0.45))
            image.putalpha(alpha)
            self.logo_img = ImageTk.PhotoImage(image)
            tk.Label(self, image=self.logo_img, bg=bg).place(
                relx=0.04, rely=0.93, anchor="sw")
        except Exception as e:
            logging.getLogger("LoopStation").warning(f"Could not load logo: {e}")

    def update_status(self, text, step=None):
        """Update status text and advance progress to the next step target."""
        self.status.config(text=text)

        if step is not None and step < len(self.STEPS):
            self._step_index = step
            _, target = self.STEPS[step]
            self._target_progress = target
        else:
            # Auto-advance to next step
            if self._step_index < len(self.STEPS):
                _, target = self.STEPS[self._step_index]
                self._target_progress = target
                self._step_index += 1

        # Animate progress to target
        self._animate_to_target()

    def _animate_to_target(self):
        """Smoothly animate the progress bar to the target value."""
        if not self.winfo_exists():
            return
        if self._current_progress < self._target_progress:
            # Ease out: big steps first, then small ones
            step_size = max(1, (self._target_progress - self._current_progress) // 6)
            self._current_progress = min(self._current_progress + step_size, self._target_progress)

            self.progress['value'] = self._current_progress
            self.update()

            if self._current_progress < self._target_progress:
                self.after(16, self._animate_to_target)

    def finish(self):
        """Complete the progress bar and close."""
        self._target_progress = 100
        self._current_progress = 100
        self.progress['value'] = 100
        self.status.config(text="Ready")
        self.update()
        self.destroy()


# =============================================================================
# UTILS
# =============================================================================

def setup_logging(debug: bool = False) -> logging.Logger:
    log_file = os.path.join(get_log_dir(), "loop_station.log")

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    ))

    handlers = [file_handler]
    if sys.stdout is not None and getattr(sys.stdout, "name", None) != os.devnull:
        handlers.insert(0, logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers
    )
    # Third-party libraries are very chatty at DEBUG (font matching, PNG
    # chunks, HTTP requests); keep the log focused on Loop Station itself.
    for noisy in ("matplotlib", "PIL", "werkzeug", "numba", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger("LoopStation")


def install_crash_handlers(logger: logging.Logger) -> None:
    """
    Route uncaught exceptions (main thread and worker threads) to the log
    file: in a windowed build there is no console, so they would otherwise
    vanish. Tk callback errors are routed by install_tk_crash_handler().
    """
    def _excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logger.critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    def _thread_excepthook(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else "?"
        logger.critical(f"Uncaught exception in thread {name}",
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook


def install_tk_crash_handler(root, logger: logging.Logger) -> None:
    """Log exceptions raised inside Tk callbacks (button handlers, after() jobs)."""
    def _report_callback_exception(exc_type, exc, tb):
        logger.error("Exception in Tk callback", exc_info=(exc_type, exc, tb))
    root.report_callback_exception = _report_callback_exception


def check_dependencies():
    """Return the list of required packages that cannot be imported."""
    missing = []
    for dep in REQUIRED_PACKAGES:
        try:
            __import__(dep)
        except ImportError:
            missing.append(dep)
    return missing


def _make_executable(path):
    """ffmpeg extracted by PyInstaller may have lost its x bit."""
    if os.name != 'nt':
        try:
            os.chmod(path, 0o755)
        except OSError:
            pass


def find_ffmpeg(explicit=None):
    """
    Locate the ffmpeg binary. Returns an absolute path (or the name resolved
    via PATH) or None if not found.

    Search order:
      1. --ffmpeg PATH given on the command line
      2. sys._MEIPASS (PyInstaller --add-binary; macOS .app Resources)
      3. next to the executable / project root
      4. system PATH
    """
    binary_name = "ffmpeg.exe" if os.name == 'nt' else "ffmpeg"

    if explicit:
        if os.path.isfile(explicit):
            return os.path.abspath(explicit)
        return shutil.which(explicit)

    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        candidate = os.path.join(meipass, binary_name)
        if os.path.isfile(candidate):
            _make_executable(candidate)
            return candidate

    candidate = os.path.join(get_base_path(), binary_name)
    if os.path.isfile(candidate):
        _make_executable(candidate)
        return candidate

    return shutil.which("ffmpeg")


def ffmpeg_install_hint():
    if sys.platform == 'darwin':
        how = "Install it with Homebrew:\n    brew install ffmpeg"
    elif os.name == 'nt':
        how = ("Install it with winget or Chocolatey:\n"
               "    winget install Gyan.FFmpeg\n"
               "    choco install ffmpeg\n"
               "or download a build from https://ffmpeg.org/download.html and "
               "place ffmpeg.exe next to Loop Station.")
    else:
        how = "Install it with your package manager, e.g.:\n    sudo apt install ffmpeg"
    return (
        "FFmpeg is required to decode audio files but was not found.\n\n"
        f"{how}\n\n"
        "You can also start Loop Station with:  --ffmpeg <path to ffmpeg>"
    )


def reset_python_imports():
    """
    Force Python to forget all 'frontend' modules so a restart re-reads the
    new color values from config.py instead of using the cached ones.
    """
    modules_to_reset = [name for name in sys.modules if name.startswith("frontend")]
    for module_name in modules_to_reset:
        del sys.modules[module_name]
    logging.getLogger("LoopStation").info(f"Cache cleared for {len(modules_to_reset)} UI modules.")


# =============================================================================
# MAIN
# =============================================================================

def main():
    make_dpi_aware()

    parser = argparse.ArgumentParser(description="Loop Station")
    parser.add_argument("--ffmpeg", default=None, help="path to the ffmpeg executable")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument("--version", action="version", version=f"Loop Station {__version__}")
    args = parser.parse_args()

    logger = setup_logging(debug=args.debug)
    install_crash_handlers(logger)
    logger.info(f"Loop Station {__version__} starting")

    # 1. Dependencies (before anything imports them)
    missing = check_dependencies()
    if missing:
        msg = ("Missing Python packages: " + ", ".join(missing) +
               "\n\nInstall them with:\n    pip install -r requirements.txt")
        logger.error(msg)
        show_fatal_error("Loop Station - Missing dependencies", msg)
        sys.exit(1)

    # 2. FFmpeg
    ffmpeg_path = find_ffmpeg(args.ffmpeg)
    if not ffmpeg_path:
        if args.ffmpeg:
            msg = f"The ffmpeg given with --ffmpeg was not found:\n{args.ffmpeg}"
        elif getattr(sys, 'frozen', False):
            msg = ("FFmpeg is missing from this Loop Station build (it should be "
                   "bundled). Please reinstall the application.")
        else:
            msg = ffmpeg_install_hint()
        logger.error(msg)
        show_fatal_error("Loop Station - FFmpeg not found", msg)
        sys.exit(1)
    logger.info(f"Using ffmpeg: {ffmpeg_path}")

    # 3. Heavy imports only now that the checks passed
    import config
    from config import WINDOW_WIDTH, WINDOW_HEIGHT

    # 4. APP LIFECYCLE LOOP. A theme change asks for a restart: the app
    #    shuts its backend down (request_restart), mainloop returns, and a
    #    fresh app is created with the new colours. The previous AudioEngine
    #    is always released before the next one is created.
    first_run = True
    try:
        while True:
            if not first_run:
                reset_python_imports()

            config.load_theme()
            from frontend.app import LoopStationApp  # late import: fresh config colours

            app = LoopStationApp(ffmpeg_path=ffmpeg_path)
            install_tk_crash_handler(app, logger)
            center_window(app, WINDOW_WIDTH, WINDOW_HEIGHT)

            if first_run:
                app.withdraw()
                splash = BootstrapSplash(app, WINDOW_WIDTH, WINDOW_HEIGHT)
                splash.update()

                splash.update_status("Initializing Audio Engine...", 0)
                app.initialize_audio_system()

                splash.update_status("Loading User Interface...", 1)
                app.update_idletasks()

                splash.finish()
                app.deiconify()
                first_run = False
            else:
                app.initialize_audio_system()

            app.run()

            if not app.restart_required:
                break
            logger.info("Theme change detected. Reloading application...")

    except Exception as e:
        logger.exception("Critical error")
        show_fatal_error(
            "Loop Station - Error",
            f"Loop Station hit an unexpected error and has to close:\n\n{e}\n\n"
            f"Details are in the log folder:\n{get_log_dir()}",
        )
    finally:
        logger.info("Loop Station exiting")


if __name__ == "__main__":
    main()
