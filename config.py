"""
Configuration constants for Loop Station.

All tunable parameters in one place for easy adjustment and debugging.
Modify these values to fine-tune audio behavior, UI appearance, and performance.
"""

import sys
import os
import customtkinter as ctk
from themes import THEMES, DEFAULT_THEME
from utils.preferences import get_theme_preference

# =============================================================================
# PATHS
# =============================================================================

# HELPER: Detect if we are running as a compiled exe or a script
def get_base_path():
    if getattr(sys, 'frozen', False):
        # We are running as an exe - use the folder the exe is sitting in
        return os.path.dirname(sys.executable)
    else:
        # We are running as a script - use the script's folder
        return os.path.dirname(os.path.abspath(__file__))

# ROOT DIR
BASE_DIR = get_base_path()

# ASSET DIR (For internal read-only files like logo.png)
# If using --onefile, internal assets are in sys._MEIPASS
def get_asset_path(relative_path):
    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative_path)

# Data directory for saved loop points (uses BASE_DIR so it works in frozen builds)
DATA_DIR = os.path.join(BASE_DIR, "data")
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

LOOP_DATA_FILE = os.path.join(DATA_DIR, "loop_data.json")

# Show data file (future cue sheet support)
SHOW_DATA_FILE = os.path.join(DATA_DIR, "show_data.json")

# =============================================================================
# AUDIO ENGINE SETTINGS
# =============================================================================

# Sample rate for audio processing (Hz)
SAMPLE_RATE = 44100

# Number of audio channels (2 = stereo)
CHANNELS = 2

# Pygame mixer buffer size (lower = less latency, but more CPU)
# 512 is good for low latency, 1024 is safer for older machines
MIXER_BUFFER_SIZE = 1024

# =============================================================================
# LOOP CROSSFADE SETTINGS  
# =============================================================================

# Crossfade duration at loop boundary (milliseconds)
# This blends the end of the loop into the beginning to prevent clicks
# TUNABLE: Increase if you hear clicks at loop point, decrease for tighter loops
LOOP_CROSSFADE_MS = 20

# For percussive/rhythmic content, use shorter crossfade:
# LOOP_CROSSFADE_MS = 20

# For ambient/sustained content, longer is smoother:
# LOOP_CROSSFADE_MS = 100

# Minimum loop duration (seconds)
MIN_LOOP_DURATION = 0.5

# =============================================================================
# EXIT TRANSITION SETTINGS
# These control the smoothness of exiting loop mode back to transport
# =============================================================================

# Exit patch duration (milliseconds)
# This short audio clip bridges the gap while transport buffers
# TUNABLE: Increase if you hear gaps when exiting loop
EXIT_PATCH_DURATION_MS = 250

# Exit patch fade-in duration (milliseconds)
EXIT_PATCH_FADE_IN_MS = 0

# Exit patch fade-out duration (milliseconds)  
EXIT_PATCH_FADE_OUT_MS = 200

LOOP_ENTRY_FADE_IN_MS = 0

# Transport resume offset (milliseconds)
# How far after loop_out to start the transport
# Should be slightly less than EXIT_PATCH_DURATION for smooth overlap
# TUNABLE: Adjust if exit sounds off - lower = more overlap, higher = less overlap
TRANSPORT_RESUME_OFFSET_MS = 30

# =============================================================================
# FADE-OUT EXIT SETTINGS
# For theater vamping: fade the loop out instead of cutting to transport
# =============================================================================

# Default fade-out duration when using "fade exit" mode (milliseconds)
FADE_EXIT_DURATION_MS = 2000

# Minimum / maximum fade-out (UI slider bounds)
FADE_EXIT_MIN_MS = 500
FADE_EXIT_MAX_MS = 5000

# =============================================================================
# MONITOR THREAD SETTINGS
# =============================================================================

# How often to update the UI (seconds)
# 0.033 = ~30 FPS
UI_UPDATE_INTERVAL = 0.010

# How early to switch to loop mode before reaching loop end (milliseconds)
# Compensates for Python/pygame latency
# TUNABLE: Increase if loop starts late, decrease if it starts early
LOOP_SWITCH_EARLY_MS = 60

# Threshold for detecting loop boundary when exiting (milliseconds)
EXIT_BOUNDARY_THRESHOLD_MS = 30

# =============================================================================
# WAVEFORM SETTINGS
# =============================================================================

# Target samples for waveform display (lower = faster generation)
WAVEFORM_TARGET_SAMPLES = 2000
WAVEFORM_HEIGHT = 120  # Fixed pixel height for waveform display

# Waveform analysis sample rate
WAVEFORM_SAMPLE_RATE = 22050

# =============================================================================
# LOOP DETECTION SETTINGS
# =============================================================================

# Sample rate for loop analysis (lower = faster but less accurate)
ANALYSIS_SAMPLE_RATE = 22050

# Default minimum confidence for loop detection (0-100)
DEFAULT_MIN_CONFIDENCE = 50

# Default minimum/maximum loop duration for detection (seconds)
DEFAULT_MIN_LOOP_DURATION = 1.0
DEFAULT_MAX_LOOP_DURATION = 10.0

# Maximum number of loop candidates to return
MAX_LOOP_CANDIDATES = 10

# =============================================================================
# UI SETTINGS - WINDOW
# =============================================================================

WINDOW_TITLE = "Loop Station"
WINDOW_WIDTH = 1400
WINDOW_HEIGHT = 800
WINDOW_MIN_WIDTH = 800
WINDOW_MIN_HEIGHT = 500

# =============================================================================
# UI SETTINGS - COLORS (DYNAMIC LOADING)
# =============================================================================

# Global variables to hold current theme colors
COLOR_BG_DARK = ""
COLOR_BG_MEDIUM = ""
COLOR_BG_LIGHT = ""
COLOR_WAVEFORM = ""
COLOR_WAVEFORM_BG = ""
COLOR_PLAYHEAD = "#ff3333"
COLOR_LOOP_REGION = "#2d5a3d"
COLOR_LOOP_IN = "#00ff00"
COLOR_LOOP_OUT = "#ffa500"
COLOR_MARKER = "#e8e82e"
COLOR_LOOP_GHOST = "#444444"
COLOR_LOOP_GHOST_ALPHA = 0.2
COLOR_BTN_PRIMARY = ""
COLOR_BTN_SUCCESS = ""
COLOR_BTN_WARNING = ""
COLOR_BTN_DANGER = ""
COLOR_BTN_DISABLED = "#3a4a3b"
COLOR_TEXT = ""
COLOR_TEXT_DIM = ""
COLOR_SKIP_REGION = ""       # For the waveform box
COLOR_SKIP_CANDIDATE = ""    # For text highlighting
COLOR_BTN_SKIP = ""          # For buttons

def load_theme():
    """Loads the user's theme preference and updates global color variables."""
    global COLOR_BG_DARK, COLOR_BG_MEDIUM, COLOR_BG_LIGHT, COLOR_WAVEFORM, \
           COLOR_WAVEFORM_BG, COLOR_BTN_PRIMARY, COLOR_BTN_SUCCESS, \
           COLOR_BTN_WARNING, COLOR_BTN_DANGER, COLOR_TEXT, COLOR_TEXT_DIM, \
           COLOR_BTN_TEXT, COLOR_SKIP_REGION, COLOR_SKIP_CANDIDATE, COLOR_BTN_SKIP

    # 1. Load User Preference
    _user_theme = get_theme_preference()
    if not _user_theme or _user_theme not in THEMES:
        _user_theme = DEFAULT_THEME

    # 2. Get the Palette
    _palette = THEMES[_user_theme]

    # 3. Apply Colors
    COLOR_BG_DARK = _palette["bg_primary"]
    COLOR_BG_MEDIUM = _palette["bg_secondary"]
    COLOR_BG_LIGHT = _palette["bg_secondary"]
    COLOR_WAVEFORM = _palette["waveform_fg"]
    COLOR_WAVEFORM_BG = _palette["waveform_bg"]
    COLOR_BTN_PRIMARY = _palette["fg_primary"]
    COLOR_BTN_TEXT = _palette.get("btn_text", "#ffffff")
    COLOR_BTN_SUCCESS = _palette.get("btn_success", "#2cc985")
    COLOR_BTN_WARNING = _palette["accent_warn"]
    COLOR_BTN_DANGER = _palette.get("btn_danger", "#d63031")
    COLOR_TEXT = _palette["text_main"]
    COLOR_TEXT_DIM = _palette["text_dim"]
    COLOR_SKIP_REGION = _palette.get("skip_region", "#882222")
    COLOR_SKIP_CANDIDATE = _palette.get("skip_candidate", "#ff5555")
    COLOR_BTN_SKIP = _palette.get("btn_skip", "#d63031")
    
    # Set the appearance mode based on background brightness for CTk components
    # This is a simple heuristic; you might need to manually flag themes as 'light' or 'dark'
    bg_brightness = int(COLOR_BG_DARK[1:3], 16) + int(COLOR_BG_DARK[3:5], 16) + int(COLOR_BG_DARK[5:7], 16)
    if bg_brightness > 382: # (255*3)/2
        ctk.set_appearance_mode("light")
    else:
        ctk.set_appearance_mode("dark")


# Load the theme immediately when config is imported
load_theme()


# =============================================================================
# UI SETTINGS - LAYOUT
# =============================================================================

SIDEBAR_WIDTH = 280
SIDEBAR_MIN_WIDTH = 250

# Button sizes
BTN_HEIGHT = 36
BTN_FONT_SIZE = 13

# Spacing
PADDING_SMALL = 5
PADDING_MEDIUM = 10
PADDING_LARGE = 20

# =============================================================================
# FILE SETTINGS
# =============================================================================

# Supported audio formats
SUPPORTED_FORMATS = ('.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac')

# =============================================================================
# DEFAULT VAMP/MARKER NAMES
# =============================================================================

DEFAULT_VAMP_NAME = "Vamp"
DEFAULT_MARKER_NAME = "Cue"
