# FILE: frontend/splash.py
import tkinter as tk
from tkinter import ttk
import time

class SplashScreen(tk.Toplevel):
    def __init__(self, root, duration=3000):
        super().__init__(root)
        self.root = root
        self.duration = duration
        
        # Remove title bar and borders (makes it look like a floating image)
        self.overrideredirect(True)
        
        # Center the splash screen
        width = 600
        height = 350
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        x = (screen_width // 2) - (width // 2)
        y = (screen_height // 2) - (height // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")
        
        # Set Background Color (Match your app theme)
        bg_color = "#1a1a1a"
        self.configure(bg=bg_color)
        
        # --- CONTENT ---
        
        # 1. Main Title
        self.label_title = tk.Label(
            self, 
            text="LOOP STATION", 
            font=("Segoe UI", 42, "bold"),
            bg=bg_color, 
            fg="#ffffff"
        )
        self.label_title.place(relx=0.5, rely=0.35, anchor="center")
        
        # 2. Subtitle
        self.label_subtitle = tk.Label(
            self, 
            text="Professional Audio Looper", 
            font=("Segoe UI", 14),
            bg=bg_color, 
            fg="#3b8ed0"  # Your primary blue color
        )
        self.label_subtitle.place(relx=0.5, rely=0.5, anchor="center")
        
        # 3. Loading Bar (Indeterminate - bounces back and forth)
        self.style = ttk.Style()
        self.style.theme_use('default')
        self.style.configure("Horizontal.TProgressbar", background="#3b8ed0", troughcolor="#111111", bordercolor="#111111")
        
        self.progress = ttk.Progressbar(
            self, 
            style="Horizontal.TProgressbar", 
            mode='indeterminate', 
            length=400
        )
        self.progress.place(relx=0.5, rely=0.7, anchor="center")
        self.progress.start(10)
        
        # 4. Status Text
        self.label_status = tk.Label(
            self, 
            text="Initializing Audio Engine...", 
            font=("Consolas", 9),
            bg=bg_color, 
            fg="#666666"
        )
        self.label_status.place(relx=0.5, rely=0.85, anchor="center")

    def update_status(self, text):
        """Update the loading text."""
        self.label_status.config(text=text)
        self.update()

    def finish(self):
        """Destroy splash and show main window."""
        self.destroy()
        self.root.deiconify()  # Reveal the main app