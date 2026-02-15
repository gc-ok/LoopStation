import os
import sys
import platform
import zipfile
import gzip
import urllib.request
import ssl
import tkinter as tk
from tkinter import messagebox, ttk
import threading
import shutil
import queue
import time

# --- BYPASS SSL CERTIFICATE CHECK (For macOS) ---
if hasattr(ssl, '_create_unverified_context'):
    ssl._create_default_https_context = ssl._create_unverified_context

# --- CONFIGURATION ---
# IMPORTANT: We MUST use LGPL builds for commercial distribution.
# Loop Station only needs decoding (no x264/x265), so LGPL is fully sufficient.

# Windows: BtbN LGPL build (no GPL codecs like x264/x265)
WIN_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-lgpl.zip"

# Mac: evermeet.cx builds are GPL (include x264/x265) — but this fallback
# downloader only runs if the bundled FFmpeg is missing (e.g. dev/source installs).
# For distributed .app builds, LGPL FFmpeg is compiled from source in CI.
MAC_FFMPEG_URL = "https://evermeet.cx/ffmpeg/getrelease/zip"
MAC_FFPROBE_URL = "https://evermeet.cx/ffprobe/getrelease/zip"

# Timeout for each download attempt (seconds)
DOWNLOAD_TIMEOUT = 60
# Number of retry attempts
MAX_RETRIES = 3

def get_base_path():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def is_ffmpeg_installed():
    # Check for BOTH files
    if platform.system() == "Windows":
        files = ["ffmpeg.exe", "ffprobe.exe"]
    else:
        files = ["ffmpeg", "ffprobe"]
    
    # 1. Check PyInstaller bundle (sys._MEIPASS) — this is where --add-binary lands
    #    On macOS .app: Contents/Resources/
    #    On Windows --onedir: same as exe dir 
    if getattr(sys, '_MEIPASS', None):
        if all(os.path.exists(os.path.join(sys._MEIPASS, f)) for f in files):
            return True
    
    # 2. Check local folder next to executable/script
    base_path = get_base_path()
    if all(os.path.exists(os.path.join(base_path, f)) for f in files):
        return True
        
    # 3. Check system PATH (Fallback)
    return (shutil.which("ffmpeg") is not None) and (shutil.which("ffprobe") is not None)


def _download_with_retry(url, dest_path, progress_callback, max_retries=MAX_RETRIES, timeout=DOWNLOAD_TIMEOUT):
    """
    Download a file with retry logic and timeout.
    
    Args:
        url: URL to download
        dest_path: Local path to save to
        progress_callback: function(percent_int) called with 0-100
        max_retries: Number of attempts
        timeout: Seconds before giving up per attempt
    
    Raises:
        Exception on final failure
    """
    last_error = None
    
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                wait = min(2 ** attempt, 10)  # exponential backoff, max 10s
                time.sleep(wait)
            
            # Build request with timeout
            req = urllib.request.Request(url, headers={
                'User-Agent': 'LoopStation/1.0'
            })
            
            response = urllib.request.urlopen(req, timeout=timeout)
            total_size = int(response.headers.get('Content-Length', 0))
            
            downloaded = 0
            block_size = 65536  # 64KB chunks (faster than default 8KB)
            
            with open(dest_path, 'wb') as f:
                while True:
                    chunk = response.read(block_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        progress_callback(int(downloaded * 100 / total_size))
            
            return  # Success
            
        except Exception as e:
            last_error = e
            # Clean up partial download
            if os.path.exists(dest_path):
                try:
                    os.remove(dest_path)
                except:
                    pass
    
    raise Exception(f"Download failed after {max_retries} attempts: {last_error}")


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
    popup.geometry("400x200")
    popup.resizable(False, False)
    
    try:
        x = parent_window.winfo_x() + (parent_window.winfo_width() // 2) - 200
        y = parent_window.winfo_y() + (parent_window.winfo_height() // 2) - 100
        popup.geometry(f"+{x}+{y}")
    except:
        pass
    
    lbl_title = tk.Label(popup, text="Setting up Loop Station...", font=("Segoe UI", 10, "bold"))
    lbl_title.pack(pady=(20, 5))
    
    lbl_status = tk.Label(popup, text="Initializing...", fg="gray", font=("Segoe UI", 9))
    lbl_status.pack()
    
    # Show which file we're on (e.g. "File 1 of 2")
    lbl_file_count = tk.Label(popup, text="", fg="gray", font=("Segoe UI", 8))
    lbl_file_count.pack()
    
    progress = ttk.Progressbar(popup, length=340, mode='determinate')
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
                msg_queue.put(("file_count", f"File {index + 1} of {total_items}"))
                msg_queue.put(("progress", 0))

                # Download with retry and timeout
                def report_progress(percent):
                    msg_queue.put(("progress", min(percent, 100)))
                
                _download_with_retry(url, dest_path, report_progress)
                
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
            
            # --- VERIFY both files exist ---
            if system == "Windows":
                required = ["ffmpeg.exe", "ffprobe.exe"]
            else:
                required = ["ffmpeg", "ffprobe"]
            
            missing = [f for f in required if not os.path.exists(os.path.join(base_path, f))]
            if missing:
                msg_queue.put(("error", f"Download completed but missing files: {', '.join(missing)}"))
                return

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
                elif msg_type == "file_count":
                    lbl_file_count.config(text=data)
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
        
        popup.after(50, check_queue)

    popup.after(50, check_queue)
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
