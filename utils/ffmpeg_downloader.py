import os
import sys
import platform
import zipfile
import urllib.request
import tkinter as tk
from tkinter import messagebox, ttk
import threading
import shutil

# --- CONFIGURATION ---
# Stable download links (BtbN is a trusted source for Windows builds)
# We use "shared" builds which are often preferred for compatibility
WIN_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl-shared.zip" 
# Mac users usually use Homebrew, but we can grab a static binary for portability
MAC_URL = "https://evermeet.cx/ffmpeg/ffmpeg-113357-g41726c27e0.zip" 

def get_base_path():
    """Get the folder where the app is running (works for .exe and script)."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def is_ffmpeg_installed():
    """Check if ffmpeg exists in the app directory or system path."""
    base_path = get_base_path()
    
    # 1. Check local folder (Priority)
    local_exe = "ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg"
    if os.path.exists(os.path.join(base_path, local_exe)):
        return True
        
    # 2. Check system PATH (Fallback)
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
        pass # Fallback if parent not ready
    
    tk.Label(popup, text="Downloading Audio Engine (FFmpeg)...", font=("Segoe UI", 10, "bold")).pack(pady=(20, 10))
    progress = ttk.Progressbar(popup, length=280, mode='determinate')
    progress.pack(pady=5)
    status = tk.Label(popup, text="Connecting...", fg="gray", font=("Segoe UI", 9))
    status.pack()

    result = {"success": False}

    def _worker():
        try:
            # 1. Download hook to update progress
            def report(block_num, block_size, total_size):
                percent = int((block_num * block_size * 100) / total_size)
                progress['value'] = percent
                status.config(text=f"Downloading... {percent}%")
                popup.update_idletasks()

            urllib.request.urlretrieve(url, dest_path, report)
            
            status.config(text="Extracting files...")
            popup.update_idletasks()

            # 2. Extract
            with zipfile.ZipFile(dest_path, 'r') as zip_ref:
                for file_info in zip_ref.infolist():
                    # Flatten structure: Pull ffmpeg.exe out of subfolders
                    filename = os.path.basename(file_info.filename)
                    if filename.lower() in ["ffmpeg.exe", "ffmpeg", "ffprobe.exe", "ffprobe"]:
                        # Extract directly to base_path
                        source = zip_ref.open(file_info)
                        target = open(os.path.join(base_path, filename), "wb")
                        with source, target:
                            shutil.copyfileobj(source, target)
                        
                        # Mac/Linux needs executable permissions
                        if system != "Windows":
                            os.chmod(os.path.join(base_path, filename), 0o755)

            # 3. Cleanup
            os.remove(dest_path)
            result["success"] = True
            
        except Exception as e:
            print(f"Download Error: {e}")
            messagebox.showerror("Download Error", f"Failed to download components.\nError: {e}")
        finally:
            popup.destroy()

    # Start download in thread so UI doesn't freeze
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    
    # Wait for popup (makes it modal)
    parent_window.wait_window(popup)
    return result["success"]

def check_startup(root):
    """Main check function to be called from main.py."""
    if is_ffmpeg_installed():
        return True
    
    # Ask user
    ans = messagebox.askyesno(
        "Missing Component", 
        "The audio engine (FFmpeg) is missing.\n\n"
        "Loop Station needs to download it (~80MB) to function.\n"
        "Download now?"
    )
    
    if ans:
        return download_ffmpeg(root)
    return False
