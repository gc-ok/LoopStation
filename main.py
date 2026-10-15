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

from utils.paths import get_base_path, get_log_dir
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
            # Build everything hidden and show the window ONCE, fully drawn.
            # (A splash card used to flash for ~1 s before the main window,
            # and centring could map the half-built window for a frame.)
            app.withdraw()
            install_tk_crash_handler(app, logger)
            center_window(app, WINDOW_WIDTH, WINDOW_HEIGHT)
            app.initialize_audio_system()
            app.update_idletasks()
            app.deiconify()
            first_run = False

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
