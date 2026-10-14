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

# Must be done BEFORE importing pygame (which happens in backend imports)
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "1"

import argparse
import ctypes
import logging
from logging.handlers import RotatingFileHandler
import shutil

try:
    import ctypes.wintypes  # Windows work-area lookup; harmless elsewhere
except Exception:  # pragma: no cover
    pass
import tkinter as tk
from tkinter import ttk

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
    """Splash screen with determinate progress bar and step descriptions."""

    STEPS = [
        ("Initializing Audio Engine...", 50),
        ("Loading User Interface...", 90),
        ("Ready!", 100),
    ]

    def __init__(self, parent, width, height):
        super().__init__(parent)
        self.parent = parent
        self._current_progress = 0
        self._target_progress = 0
        self._step_index = 0

        # 1. BORDERLESS
        self.overrideredirect(True)

        # 2. STYLE
        bg_color = "#111111"
        self.configure(bg=bg_color)

        # 3. CENTER
        center_window(self, width, height)

        # --- CONTENT LAYOUT ---

        # Defaults (if no logo found)
        text_y_start = 0.40

        # A. LOGO
        try:
            from PIL import Image, ImageTk
            logo_path = get_asset_path("logo.png")

            if os.path.exists(logo_path):
                original = Image.open(logo_path)

                target_width = 500
                target_height = 250

                ratio = min(target_width / original.width, target_height / original.height)
                new_width = int(original.width * ratio)
                new_height = int(original.height * ratio)

                resized = original.resize((new_width, new_height), Image.Resampling.LANCZOS)
                self.logo_img = ImageTk.PhotoImage(resized)

                logo_lbl = tk.Label(self, image=self.logo_img, bg=bg_color)
                logo_lbl.place(relx=0.5, rely=0.25, anchor="center")

                text_y_start = 0.55

        except Exception as e:
            logging.getLogger("LoopStation").warning(f"Could not load logo: {e}")

        # B. TEXT
        tk.Label(self, text="LOOP STATION", font=("Segoe UI", 36, "bold"),
                 bg=bg_color, fg="#ffffff").place(relx=0.5, rely=text_y_start, anchor="center")

        tk.Label(self, text="Professional Audio Looper", font=("Segoe UI", 12),
                 bg=bg_color, fg="#3b8ed0").place(relx=0.5, rely=text_y_start + 0.10, anchor="center")

        # C. PROGRESS BAR (Determinate)
        style = ttk.Style()
        style.theme_use('default')
        style.configure("Horizontal.TProgressbar", background="#3b8ed0",
                        troughcolor="#222222", bordercolor="#111111", thickness=6)

        self.progress = ttk.Progressbar(self, style="Horizontal.TProgressbar",
                                        mode='determinate', maximum=100,
                                        value=0, length=400)
        self.progress.place(relx=0.5, rely=0.79, anchor="center")

        # D. STEP STATUS
        self.status = tk.Label(self, text="Starting up...", font=("Segoe UI", 11),
                               bg=bg_color, fg="#888888", width=50)
        self.status.place(relx=0.5, rely=0.85, anchor="center")

        # E. PERCENT LABEL
        self.percent_label = tk.Label(self, text="0%", font=("Consolas", 9),
                                      bg=bg_color, fg="#555555")
        self.percent_label.place(relx=0.5, rely=0.91, anchor="center")

        # F. FIRST-RUN HINT
        self.hint = tk.Label(self, text="This may take a moment on first launch",
                             font=("Segoe UI", 9), bg=bg_color, fg="#333333")
        self.hint.place(relx=0.5, rely=0.95, anchor="center")

        # G. FOOTER
        tk.Label(self, text=f"v{__version__}", font=("Segoe UI", 8),
                 bg=bg_color, fg="#333333").place(relx=0.98, rely=0.98, anchor="se")

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
            # Move in increments for smooth animation
            step_size = max(1, (self._target_progress - self._current_progress) // 8)
            self._current_progress = min(self._current_progress + step_size, self._target_progress)

            self.progress['value'] = self._current_progress
            self.percent_label.config(text=f"{self._current_progress}%")
            self.update()

            if self._current_progress < self._target_progress:
                self.after(30, self._animate_to_target)

    def finish(self):
        """Complete the progress bar and close."""
        self._target_progress = 100
        self._current_progress = 100
        self.progress['value'] = 100
        self.percent_label.config(text="100%")
        self.status.config(text="Ready!")
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

    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout), file_handler]
    )
    # Third-party libraries are very chatty at DEBUG (font matching, PNG
    # chunks, HTTP requests); keep the log focused on Loop Station itself.
    for noisy in ("matplotlib", "PIL", "werkzeug", "numba", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger("LoopStation")


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
