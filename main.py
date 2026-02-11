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
import sys

# PIL is required for handling the logo image
from PIL import Image, ImageTk

# Ensure we can import from our package
sys.path.insert(0, os.path.dirname(__file__))

from config import WINDOW_WIDTH, WINDOW_HEIGHT, get_asset_path

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
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
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
    import shutil
    if shutil.which("ffmpeg"): return "ffmpeg"
    common = [r"C:\ffmpeg\bin\ffmpeg.exe", os.path.expanduser("~/ffmpeg/bin/ffmpeg")]
    for p in common:
        if os.path.isfile(p): return p
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

    parser = argparse.ArgumentParser()
    parser.add_argument("--ffmpeg", default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    check_dependencies()
    logger = setup_logging(debug=args.debug)
    logger.info("Loop Station Starting")
    
    ffmpeg_path = args.ffmpeg or find_ffmpeg()
    logger.info(f"Using ffmpeg: {ffmpeg_path}")

    import config # Import config to reload theme

    # --- APP LIFECYCLE LOOP ---
    first_run = True
    while True:
        try:
            # 1. CLEAN SLATE (The Fix)
            # If this isn't the first run, wipe the old UI code from memory
            if not first_run:
                reset_python_imports()

            # 2. RELOAD THEME & IMPORT APP
            config.load_theme()
            
            # Re-import the app class AFTER clearing the cache
            # This forces it to read the new colors from config
            from frontend.app import LoopStationApp 

            # 3. CREATE APP SHELL
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

            # 4. RUN APP
            app.run()
            
            # 5. CHECK FOR RESTART
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