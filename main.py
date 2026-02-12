#!/usr/bin/env python3
"""
Loop Station - Audio Loop Player with Seamless Looping

A professional audio loop player that provides mathematically-perfect
seamless loops by pre-processing audio in RAM.

Usage:
    python main.py [--ffmpeg PATH]
"""

import os
import sys

# Must be done BEFORE importing pygame (which happens in backend imports)
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "1"

import logging
import argparse
import time
from datetime import datetime
import tkinter as tk
from tkinter import ttk
import ctypes
import shutil # Needed for which

from utils.ffmpeg_downloader import check_startup

# PIL is required for handling the logo image
from PIL import Image, ImageTk

# Ensure we can import from our package
sys.path.insert(0, os.path.dirname(__file__))

from config import WINDOW_WIDTH, WINDOW_HEIGHT, get_asset_path

# =============================================================================
# HELPER: DETECT BASE PATH
# =============================================================================
def get_base_path():
    """Returns the directory where the executable or script is running."""
    if getattr(sys, 'frozen', False):
        # Running as compiled app/exe
        return os.path.dirname(sys.executable)
    else:
        # Running as script
        return os.path.dirname(os.path.abspath(__file__))

# =============================================================================
# HELPER: CENTER WINDOW & HIGH DPI FIX
# =============================================================================
def make_dpi_aware():
    """Fixes blurry UI and incorrect positioning on Windows."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

def center_window(window, width, height):
    """Centers a tkinter window on the screen reliably."""
    window.update_idletasks()
    screen_width = window.winfo_screenwidth()
    screen_height = window.winfo_screenheight()
    x = int((screen_width / 2) - (width / 2))
    y = int((screen_height / 2) - (height / 2))
    window.geometry(f"{width}x{height}+{x}+{y}")

# =============================================================================
# BOOTSTRAP SPLASH SCREEN (Fixed Spacing & Sizes)
# =============================================================================
class BootstrapSplash(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        
        # 1. BORDERLESS
        self.overrideredirect(True)
        
        # 2. STYLE
        bg_color = "#111111"
        self.configure(bg=bg_color)
        
        # 3. CENTER
        center_window(self, WINDOW_WIDTH, WINDOW_HEIGHT)
        
        # --- CONTENT LAYOUT ---
        
        # Defaults (if no logo found)
        text_y_start = 0.40
        
        # A. LOGO (Moved Up & Allowed to be Bigger)
        try:
            logo_path = get_asset_path("logo.png")
            
            if os.path.exists(logo_path):
                # Load Image
                original = Image.open(logo_path)
                
                # --- SMART RESIZE (Keep your preferred size) ---
                target_width = 500   
                target_height = 250  
                
                # Calculate ratio
                ratio = min(target_width / original.width, target_height / original.height)
                new_width = int(original.width * ratio)
                new_height = int(original.height * ratio)
                
                resized = original.resize((new_width, new_height), Image.Resampling.LANCZOS)
                self.logo_img = ImageTk.PhotoImage(resized)
                
                # Display Logo HIGHER (0.25)
                logo_lbl = tk.Label(self, image=self.logo_img, bg=bg_color)
                logo_lbl.place(relx=0.5, rely=0.25, anchor="center")
                
                # Push text DOWN significantly to avoid overlap
                text_y_start = 0.55
                
        except Exception as e:
            print(f"Could not load logo: {e}")

        # B. TEXT (Fixed Overlap)
        # Title
        tk.Label(self, text="LOOP STATION", font=("Segoe UI", 36, "bold"),
                 bg=bg_color, fg="#ffffff").place(relx=0.5, rely=text_y_start, anchor="center")
        
        # Subtitle - Increased spacing from 0.06 to 0.10 to prevent overlap
        tk.Label(self, text="Professional Audio Looper", font=("Segoe UI", 12),
                 bg=bg_color, fg="#3b8ed0").place(relx=0.5, rely=text_y_start + 0.10, anchor="center")
        
        # C. LOADING BAR (Pushed down slightly)
        style = ttk.Style()
        style.theme_use('default')
        style.configure("Horizontal.TProgressbar", background="#3b8ed0", 
                        troughcolor="#111111", bordercolor="#111111", thickness=6)
        
        self.progress = ttk.Progressbar(self, style="Horizontal.TProgressbar", 
                                        mode='indeterminate', length=400)
        self.progress.place(relx=0.5, rely=0.82, anchor="center")
        self.progress.start(15)
        
        # D. STATUS (Added width to fix ghosting text artifact)
        self.status = tk.Label(self, text="Initializing...", font=("Consolas", 10),
                               bg=bg_color, fg="#666666", width=60)
        self.status.place(relx=0.5, rely=0.88, anchor="center")
        
        # E. FOOTER
        tk.Label(self, text="v1.0.0", font=("Segoe UI", 8),
                 bg=bg_color, fg="#333333").place(relx=0.98, rely=0.98, anchor="se")

    def update_status(self, text):
        self.status.config(text=text)
        self.update()

    def finish(self):
        self.progress.stop()
        self.destroy()

# =============================================================================
# UTILS
# =============================================================================

def setup_logging(debug: bool = False) -> logging.Logger:
    log_dir = os.path.join(get_base_path(), "logs") # Use base path for logs too
    os.makedirs(log_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(log_dir, f"loop_station_{timestamp}.log")
    
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(log_file)]
    )
    return logging.getLogger("LoopStation")

def find_ffmpeg() -> str:
    """
    Robustly find FFmpeg.
    PRIORITY 1: Check sys._MEIPASS (PyInstaller bundles --add-binary files here).
    PRIORITY 2: Check the local folder (next to the .exe or script).
    PRIORITY 3: Check global system PATH.
    """
    binary_name = "ffmpeg.exe" if os.name == 'nt' else "ffmpeg"
    
    # 1. Check PyInstaller bundle directory (CRITICAL for macOS .app)
    # On macOS: LoopStation.app/Contents/Resources/ (sys._MEIPASS)
    # On Windows --onefile: temp _MEIPASS dir
    # On Windows --onedir: same as exe dir
    if getattr(sys, '_MEIPASS', None):
        meipass_binary = os.path.join(sys._MEIPASS, binary_name)
        if os.path.isfile(meipass_binary):
            if os.name != 'nt':
                try:
                    os.chmod(meipass_binary, 0o755)
                except:
                    pass
            return meipass_binary
    
    # 2. Check Local Folder (next to executable or script)
    base_path = get_base_path()
    local_binary = os.path.join(base_path, binary_name)
    
    if os.path.isfile(local_binary):
        if os.name != 'nt':
            try:
                os.chmod(local_binary, 0o755)
            except:
                pass
        return local_binary

    # 3. Check Global PATH
    if shutil.which("ffmpeg"): 
        return "ffmpeg"
        
    # 4. Fallback (likely to fail if not found above)
    return "ffmpeg"

def check_dependencies():
    missing = []
    for dep in ['pygame', 'numpy', 'matplotlib', 'customtkinter', 'PIL']:
        try:
            __import__(dep)
        except ImportError:
            missing.append(dep)
    if missing:
        print(f"Missing: {', '.join(missing)}")
        sys.exit(1)

def smart_sleep(root, seconds):
    end_time = time.time() + seconds
    while time.time() < end_time:
        root.update()
        time.sleep(0.01)


# =============================================================================
# MAIN
# =============================================================================

def reset_python_imports():
    """
    Force Python to forget all 'frontend' modules.
    This ensures that when we reload the app, it re-reads the new 
    color values from config.py instead of using the cached old ones.
    """
    # Create a list of modules to remove to avoid changing dictionary while iterating
    modules_to_reset = [name for name in sys.modules.keys() if name.startswith("frontend")]
    
    for module_name in modules_to_reset:
        if module_name in sys.modules:
            del sys.modules[module_name]
    
    print(f"Cache cleared for {len(modules_to_reset)} UI modules.")


def main():
    make_dpi_aware()

    # 1. SETUP & LOGGING (Must happen first!)
    parser = argparse.ArgumentParser()
    parser.add_argument("--ffmpeg", default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    check_dependencies()
    logger = setup_logging(debug=args.debug)
    logger.info("Loop Station Starting")
    
    # 2. DETERMINE PATHS (Initial check)
    ffmpeg_path = args.ffmpeg or find_ffmpeg()
    
    # Import config after logging setup just in case
    import config 

    # 3. AUTO-DOWNLOAD CHECK
    # We create a temporary hidden root window just for the download popup.
    try:
        # Create a dummy window for the downloader to parent to
        dummy_root = tk.Tk()
        dummy_root.withdraw() # Hide it
        
        # This will download to get_base_path() if user accepts
        if not check_startup(dummy_root):
            # Only exit if ffmpeg is TRULY missing and they declined download
            if not shutil.which(ffmpeg_path) and not os.path.exists(ffmpeg_path):
                logger.error("FFmpeg missing and download declined. Exiting.")
                dummy_root.destroy()
                sys.exit(1)
            
        dummy_root.destroy() # Cleanup dummy window
        
        # RE-DETECT PATH after potential download
        # This is the critical step to pick up the file we just downloaded
        ffmpeg_path = args.ffmpeg or find_ffmpeg()
        logger.info(f"Using ffmpeg: {ffmpeg_path}")
        
    except Exception as e:
        logger.exception(f"Startup check failed: {e}")
        sys.exit(1)

    # 4. APP LIFECYCLE LOOP (Now safe to start)
    first_run = True
    while True:
        try:
            if not first_run:
                reset_python_imports()

            config.load_theme()
            
            # Late import to ensure it uses fresh config
            from frontend.app import LoopStationApp 

            # Create the REAL app
            # Pass the ABSOLUTE PATH to ffmpeg we found
            app = LoopStationApp(ffmpeg_path=ffmpeg_path)
            
            if first_run:
                app.withdraw()
                center_window(app, WINDOW_WIDTH, WINDOW_HEIGHT)

                splash = BootstrapSplash(app)
                splash.update()
                
                splash.update_status("Initializing Audio Engine...")
                smart_sleep(app, 0.1)
                app.initialize_audio_system()
                
                splash.update_status("Loading User Library...")
                smart_sleep(app, 0.4)
                
                splash.update_status("Ready!")
                smart_sleep(app, 0.4)

                splash.finish()
                app.deiconify()
                first_run = False 
            else:
                center_window(app, WINDOW_WIDTH, WINDOW_HEIGHT)
                app.initialize_audio_system()

            app.run()
            
            if not app.restart_required:
                break 
                
            logger.info("Theme change detected. Reloading application...")

        except Exception as e:
            logger.exception(f"Critical Error: {e}")
            break 
        finally:
            if not first_run and ('app' in locals() and not app.restart_required):
                 logger.info("Loop Station Exiting")

if __name__ == "__main__":
    main()
