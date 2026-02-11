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
import queue

# --- BYPASS SSL CERTIFICATE CHECK (For macOS) ---
if hasattr(ssl, '_create_unverified_context'):
    ssl._create_default_https_context = ssl._create_unverified_context

# --- CONFIGURATION ---
# Windows: BtbN builds contain BOTH ffmpeg and ffprobe in one zip
WIN_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl-shared.zip"

# Mac: Evermeet distributes them separately. We use the "getrelease" permalink to always get the latest.
MAC_FFMPEG_URL = "https://evermeet.cx/ffmpeg/getrelease/zip"
MAC_FFPROBE_URL = "https://evermeet.cx/ffprobe/getrelease/zip"

def get_base_path():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def is_ffmpeg_installed():
    base_path = get_base_path()
    
    # Check for BOTH files
    if platform.system() == "Windows":
        files = ["ffmpeg.exe", "ffprobe.exe"]
    else:
        files = ["ffmpeg", "ffprobe"]
        
    # 1. Check local folder (Must have both)
    if all(os.path.exists(os.path.join(base_path, f)) for f in files):
        return True
        
    # 2. Check system PATH (Fallback)
    # On Mac/Linux we accept system installs if both are present
    return (shutil.which("ffmpeg") is not None) and (shutil.which("ffprobe") is not None)

def download_ffmpeg(parent_window):
    """Downloads FFmpeg (and FFprobe on Mac) using a thread-safe queue."""
    base_path = get_base_path()
    system = platform.system()
    
    # Define what to download
    # Format: List of (URL, Description)
    downloads = []
    if system == "Windows":
        downloads.append((WIN_URL, "Audio Engine (FFmpeg & FFprobe)"))
    else:
        downloads.append((MAC_FFMPEG_URL, "Audio Engine (FFmpeg)"))
        downloads.append((MAC_FFPROBE_URL, "Audio Engine (FFprobe)"))

    # 1. Setup UI
    popup = tk.Toplevel(parent_window)
    popup.title("Downloading Components")
    popup.geometry("350x180") # Taller for multiple files
    popup.resizable(False, False)
    
    try:
        x = parent_window.winfo_x() + (parent_window.winfo_width() // 2) - 175
        y = parent_window.winfo_y() + (parent_window.winfo_height() // 2) - 90
        popup.geometry(f"+{x}+{y}")
    except:
        pass
    
    lbl_title = tk.Label(popup, text="Setting up Loop Station...", font=("Segoe UI", 10, "bold"))
    lbl_title.pack(pady=(20, 5))
    
    lbl_status = tk.Label(popup, text="Initializing...", fg="gray", font=("Segoe UI", 9))
    lbl_status.pack()
    
    progress = ttk.Progressbar(popup, length=280, mode='determinate')
    progress.pack(pady=10)

    # 2. Thread Communication
    msg_queue = queue.Queue()
    result = {"success": False}

    def _worker():
        try:
            total_items = len(downloads)
            
            for index, (url, desc) in enumerate(downloads):
                zip_name = f"temp_download_{index}.zip"
                dest_path = os.path.join(base_path, zip_name)
                
                # Update UI for current file
                msg_queue.put(("status", f"Downloading {desc}..."))
                msg_queue.put(("progress", 0))

                # Download hook
                def report(block_num, block_size, total_size):
                    if total_size > 0:
                        percent = int((block_num * block_size * 100) / total_size)
                        msg_queue.put(("progress", percent))

                urllib.request.urlretrieve(url, dest_path, report)
                
                msg_queue.put(("status", f"Extracting {desc}..."))
                
                # Extract
                with zipfile.ZipFile(dest_path, 'r') as zip_ref:
                    for file_info in zip_ref.infolist():
                        filename = os.path.basename(file_info.filename)
                        
                        # Files we want to keep
                        target_files = ["ffmpeg.exe", "ffprobe.exe"] if system == "Windows" else ["ffmpeg", "ffprobe"]
                        
                        if filename.lower() in target_files:
                            source = zip_ref.open(file_info)
                            target = open(os.path.join(base_path, filename), "wb")
                            with source, target:
                                shutil.copyfileobj(source, target)
                            
                            # Executable permissions (Mac/Linux)
                            if system != "Windows":
                                os.chmod(os.path.join(base_path, filename), 0o755)

                # Cleanup zip
                if os.path.exists(dest_path):
                    os.remove(dest_path)

            msg_queue.put(("done", True))
            
        except Exception as e:
            msg_queue.put(("error", str(e)))

    # 3. Start Thread
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    
    # 4. Main Thread Checker
    def check_queue():
        try:
            while True:
                msg_type, data = msg_queue.get_nowait()
                
                if msg_type == "progress":
                    progress['value'] = data
                elif msg_type == "status":
                    lbl_status.config(text=data)
                elif msg_type == "error":
                    popup.destroy()
                    messagebox.showerror("Download Error", f"Failed to download.\nError: {data}")
                    return 
                elif msg_type == "done":
                    result["success"] = True
                    popup.destroy()
                    return 
        except queue.Empty:
            pass
        
        popup.after(100, check_queue)

    popup.after(100, check_queue)
    parent_window.wait_window(popup)
    
    return result["success"]

def check_startup(root):
    if is_ffmpeg_installed():
        return True
    
    ans = messagebox.askyesno(
        "Missing Components", 
        "Loop Station needs audio drivers (FFmpeg) to work.\n\n"
        "Download and install them automatically?\n"
        "(This will download ~80MB)"
    )
    
    if ans:
        return download_ffmpeg(root)
    return False
