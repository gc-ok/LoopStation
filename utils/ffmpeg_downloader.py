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
import queue  # <--- NEW IMPORT

# --- BYPASS SSL CERTIFICATE CHECK (For macOS) ---
if hasattr(ssl, '_create_unverified_context'):
    ssl._create_default_https_context = ssl._create_unverified_context

# Stable download links
WIN_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl-shared.zip" 
MAC_URL = "https://evermeet.cx/ffmpeg/ffmpeg-113357-g41726c27e0.zip" 

def get_base_path():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def is_ffmpeg_installed():
    base_path = get_base_path()
    local_exe = "ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg"
    if os.path.exists(os.path.join(base_path, local_exe)):
        return True
    return shutil.which("ffmpeg") is not None

def download_ffmpeg(parent_window):
    """Downloads and extracts FFmpeg using a thread-safe queue."""
    base_path = get_base_path()
    system = platform.system()
    url = WIN_URL if system == "Windows" else MAC_URL
    zip_name = "ffmpeg_temp.zip"
    dest_path = os.path.join(base_path, zip_name)
    
    # 1. Setup UI
    popup = tk.Toplevel(parent_window)
    popup.title("Downloading Components")
    popup.geometry("350x150")
    popup.resizable(False, False)
    
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

    # 2. Setup Thread-Safe Communication
    msg_queue = queue.Queue()
    result = {"success": False}

    def _worker():
        try:
            def report(block_num, block_size, total_size):
                if total_size > 0:
                    percent = int((block_num * block_size * 100) / total_size)
                    # Don't touch UI here! Put in queue.
                    msg_queue.put(("progress", percent))

            # Download
            urllib.request.urlretrieve(url, dest_path, report)
            
            msg_queue.put(("status", "Extracting files..."))

            # Extract
            with zipfile.ZipFile(dest_path, 'r') as zip_ref:
                for file_info in zip_ref.infolist():
                    filename = os.path.basename(file_info.filename)
                    target_files = ["ffmpeg.exe", "ffprobe.exe"] if system == "Windows" else ["ffmpeg", "ffprobe"]
                    
                    if filename.lower() in target_files:
                        source = zip_ref.open(file_info)
                        target = open(os.path.join(base_path, filename), "wb")
                        with source, target:
                            shutil.copyfileobj(source, target)
                        
                        if system != "Windows":
                            os.chmod(os.path.join(base_path, filename), 0o755)

            if os.path.exists(dest_path):
                os.remove(dest_path)
            
            msg_queue.put(("done", True))
            
        except Exception as e:
            msg_queue.put(("error", str(e)))

    # 3. Start Thread
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    
    # 4. Process Queue (This runs on the MAIN thread)
    def check_queue():
        try:
            while True:
                # Get all available messages (don't block)
                msg_type, data = msg_queue.get_nowait()
                
                if msg_type == "progress":
                    progress['value'] = data
                    status.config(text=f"Downloading... {data}%")
                elif msg_type == "status":
                    status.config(text=data)
                elif msg_type == "error":
                    popup.destroy()
                    messagebox.showerror("Download Error", f"Failed to download.\nError: {data}")
                    return # Stop checking
                elif msg_type == "done":
                    result["success"] = True
                    popup.destroy()
                    return # Stop checking
                    
        except queue.Empty:
            pass
        
        # Check again in 100ms
        popup.after(100, check_queue)

    # Start the checker loop
    popup.after(100, check_queue)
    
    # Wait for window to close
    parent_window.wait_window(popup)
    
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
