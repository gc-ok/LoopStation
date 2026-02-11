import os
import sys
import platform
import zipfile
import urllib.request
import ssl
import tkinter as tk
from tkinter import messagebox, ttk
import threading
import shutil

# --- FIX 1: BYPASS SSL CERTIFICATE CHECK (For macOS) ---
# This forces Python to trust the download link even without local certs
if hasattr(ssl, '_create_unverified_context'):
    ssl._create_default_https_context = ssl._create_unverified_context
# -------------------------------------------------------

# Stable download links
WIN_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl-shared.zip" 
MAC_URL = "https://evermeet.cx/ffmpeg/ffmpeg-113357-g41726c27e0.zip" 

def get_base_path():
    """Get the folder where the app is running."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def is_ffmpeg_installed():
    """Check if ffmpeg exists in the app directory or system path."""
    base_path = get_base_path()
    
    # 1. Check local folder
    local_exe = "ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg"
    if os.path.exists(os.path.join(base_path, local_exe)):
        return True
        
    # 2. Check system PATH
    return shutil.which("ffmpeg") is not None

def download_ffmpeg(parent_window):
    """Downloads and extracts FFmpeg with a progress bar."""
    base_path = get_base_path()
    system = platform.system()
    url = WIN_URL if system == "Windows" else MAC_URL
    zip_name = "ffmpeg_temp.zip"
    dest_path = os.path.join(base_path, zip_name)
    
    # UI Setup
    popup = tk.Toplevel(parent_window)
    popup.title("Downloading Components")
    popup.geometry("350x150")
    popup.resizable(False, False)
    
    # Center popup
    try:
        x = parent_window.winfo_x() + (parent_window.winfo_width() // 2) - 175
        y = parent_window.winfo_y() + (parent_window.winfo_height() // 2) - 75
        popup.geometry(f"+{x}+{y}")
    except:
        pass
    
    tk.Label(popup, text="Downloading Audio Engine (FFmpeg)...", font=("Segoe UI", 10, "bold")).pack(pady=(20, 10))
    progress = ttk.Progressbar(popup, length=280, mode='determinate')
    progress.pack(pady=5)
    status = tk.Label(popup, text="Connecting...", fg="gray", font=("Segoe UI", 9))
    status.pack()

    # Shared state for thread results
    result = {"success": False, "error": None}

    def _worker():
        try:
            # Download hook
            def report(block_num, block_size, total_size):
                if total_size > 0:
                    percent = int((block_num * block_size * 100) / total_size)
                    # Schedule UI update on main thread
                    popup.after(0, lambda: progress.config(value=percent))
                    popup.after(0, lambda: status.config(text=f"Downloading... {percent}%"))

            # Download
            urllib.request.urlretrieve(url, dest_path, report)
            
            popup.after(0, lambda: status.config(text="Extracting files..."))

            # Extract
            with zipfile.ZipFile(dest_path, 'r') as zip_ref:
                for file_info in zip_ref.infolist():
                    filename = os.path.basename(file_info.filename)
                    # We look for the executable files inside the zip
                    target_files = ["ffmpeg.exe", "ffprobe.exe"] if system == "Windows" else ["ffmpeg", "ffprobe"]
                    
                    if filename.lower() in target_files:
                        source = zip_ref.open(file_info)
                        target = open(os.path.join(base_path, filename), "wb")
                        with source, target:
                            shutil.copyfileobj(source, target)
                        
                        # Set permissions for Mac/Linux
                        if system != "Windows":
                            os.chmod(os.path.join(base_path, filename), 0o755)

            # Cleanup
            if os.path.exists(dest_path):
                os.remove(dest_path)
            
            result["success"] = True
            
        except Exception as e:
            result["error"] = str(e)
        finally:
            # FIX 2: Schedule the popup to close on the main thread
            popup.after(0, popup.destroy)

    # Start thread
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    
    # Wait for the window to be destroyed (this blocks the main flow until done)
    parent_window.wait_window(popup)
    
    # Check result AFTER the thread is dead and popup is gone
    if result["error"]:
        messagebox.showerror("Download Error", f"Failed to download components.\nError: {result['error']}")
        return False

    return result["success"]

def check_startup(root):
    if is_ffmpeg_installed():
        return True
    
    ans = messagebox.askyesno(
        "Missing Component", 
        "The audio engine (FFmpeg) is missing.\n\n"
        "Loop Station needs to download it (~80MB) to function.\n"
        "Download now?"
    )
    
    if ans:
        return download_ffmpeg(root)
    return False
