import customtkinter as ctk
from config import (
    COLOR_BG_MEDIUM, COLOR_TEXT, COLOR_TEXT_DIM, 
    PADDING_MEDIUM, PADDING_SMALL,
    LOOP_CROSSFADE_MS, LOOP_SWITCH_EARLY_MS, FADE_EXIT_DURATION_MS
)

class VampSettingsPanel(ctk.CTkFrame):
    def __init__(self, parent, on_update_settings, **kwargs):
        super().__init__(parent, fg_color=COLOR_BG_MEDIUM, **kwargs)
        self.on_update_settings = on_update_settings
        
        # Store slider references for later updates
        self.sliders = {}
        self.labels = {}
        
        self._create_widgets()

    def _create_widgets(self):
        ctk.CTkLabel(
            self, text="🛠️  ADVANCED LOOP TWEAKS", 
            font=("Segoe UI", 12, "bold"), 
            text_color=COLOR_TEXT
        ).pack(pady=(10, 15))

        # 1. Entry Smoothing
        self._add_slider(
            "Smooth Entry (Fade-In Loop)", 
            0, 500, "ms", "entry_fade_ms", 
            "Fade-in duration when the loop starts. Higher = Softer entry.",
            default=15
        )

        # 2. Crossfade
        self._add_slider(
            "Smooth Loop Seam (Crossfade)", 
            0, 2000, "ms", "crossfade_ms", 
            "Blends the end of the loop back into the start.",
            default=LOOP_CROSSFADE_MS
        )

        # 3. Early Switch
        self._add_slider(
            "Rhythm Correction (Early Switch)", 
            -200, 200, "ms", "early_switch_ms", 
            "Positive = Switch early (tight timing).\nZero/Negative = Switch late (hear all audio).",
            default=LOOP_SWITCH_EARLY_MS
        )

        # 4. Fade Exit
        self._add_slider(
            "Fade Out Duration (Exit)", 
            100, 10000, "ms", "exit_fade_ms", 
            "Duration of the fade-out when exiting loop mode.",
            default=FADE_EXIT_DURATION_MS
        )

    def _add_slider(self, label_text, min_val, max_val, unit, attr_name, tooltip, default=0):
        """Create a slider with label."""
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.pack(fill="x", padx=15, pady=5)
        
        # Label Row
        lbl_frame = ctk.CTkFrame(frame, fg_color="transparent")
        lbl_frame.pack(fill="x")
        
        ctk.CTkLabel(
            lbl_frame, text=label_text, 
            font=("Segoe UI", 11, "bold"), 
            text_color=COLOR_TEXT
        ).pack(side="left")
        
        # Value label - initialize with default
        val_lbl = ctk.CTkLabel(
            lbl_frame, 
            text=f"{int(default)} {unit}", 
            font=("Consolas", 11), 
            text_color="#88aaff"
        )
        val_lbl.pack(side="right")
        
        # Tooltip (subtle hint below label)
        if tooltip:
            ctk.CTkLabel(
                frame, 
                text=tooltip, 
                font=("Segoe UI", 9), 
                text_color=COLOR_TEXT_DIM,
                wraplength=350,
                justify="left"
            ).pack(fill="x", pady=(0, 5))
        
        # Slider - set default value
        slider = ctk.CTkSlider(
            frame, 
            from_=min_val, 
            to=max_val, 
            number_of_steps=(max_val - min_val),
            command=lambda v: self._on_change(v, val_lbl, unit, attr_name)
        )
        slider.set(default)  # Set initial value
        slider.pack(fill="x", pady=(2, 10))
        
        # Store references
        self.sliders[attr_name] = slider
        self.labels[attr_name] = val_lbl

    def _on_change(self, value, label, unit, attr_name):
        """Handle slider change."""
        # Snap to integer
        val = int(value)
        label.configure(text=f"{val} {unit}")
        
        # Notify parent
        if self.on_update_settings:
            self.on_update_settings(attr_name, val)

    def load_settings(self, loop):
        """Update sliders to match the selected loop's settings."""
        settings = {
            "entry_fade_ms": loop.entry_fade_ms,
            "crossfade_ms": loop.crossfade_ms,
            "early_switch_ms": loop.early_switch_ms,
            "exit_fade_ms": loop.exit_fade_ms,
        }
        
        for attr_name, value in settings.items():
            self._update_slider(attr_name, value)

    def _update_slider(self, attr_name, value):
        """Update a specific slider and its label."""
        slider = self.sliders.get(attr_name)
        label = self.labels.get(attr_name)
        
        if slider and label:
            slider.set(value)
            label.configure(text=f"{int(value)} ms")