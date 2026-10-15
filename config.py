"""
Configuration constants for Loop Station.

All tunable parameters in one place for easy adjustment and debugging.
Modify these values to fine-tune audio behavior, UI appearance, and performance.
"""

import os
import customtkinter as ctk
from themes import THEMES, DEFAULT_THEME
from utils.preferences import get_theme_preference
from utils.paths import get_base_path, get_data_dir

# =============================================================================
# PATHS
# =============================================================================
# All path logic lives in utils/paths.py (get_base_path, get_asset_path,
# get_data_dir, get_log_dir). Import from there directly.

# ROOT DIR (executable folder when frozen, project root when run from source)
BASE_DIR = get_base_path()

# Writable per-user data directory (platform-appropriate; see utils/paths.py)
DATA_DIR = get_data_dir()

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

# Name of the per-user temp subdirectory (under tempfile.gettempdir()) that
# holds decoded audio. Stale files from earlier runs are swept on startup.
TEMP_DIR_NAME = "LoopStation"

# Maximum time to wait for ffmpeg to decode one file (seconds)
FFMPEG_DECODE_TIMEOUT_S = 300

# sounddevice output block size in frames (lower = less latency, more CPU).
# 512 frames = 11.6 ms at 44.1 kHz.
AUDIO_BLOCK_SIZE = 512

# Declick crossfade applied inside the audio callback on every seek, skip,
# hold, pause and tempo hot-swap (milliseconds).
DECLICK_MS = 10

# Tempo ratio bounds for set_tempo_pitch (1.0 = original speed)
TEMPO_MIN = 0.5
TEMPO_MAX = 2.0

# Largest key change in either direction (semitones)
SEMITONES_MAX = 12

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

# Monitor thread scan interval (seconds). The monitor polls the playback
# position this often to catch loop boundaries and skip regions. This is
# NOT the UI refresh rate (see UI_EMIT_INTERVAL).
UI_UPDATE_INTERVAL = 0.010

# How often the monitor emits 'position_update' to the UI (seconds)
# 0.033 = ~30 FPS
UI_EMIT_INTERVAL = 0.033

# How early to switch to loop mode before reaching loop end (milliseconds)
# Compensates for Python/pygame latency. Default for a vamp's per-loop
# early_switch_ms setting.
# TUNABLE: Increase if loop starts late, decrease if it starts early
LOOP_SWITCH_EARLY_MS = 60

# Threshold for detecting loop boundary when exiting (milliseconds)
EXIT_BOUNDARY_THRESHOLD_MS = 30

# If a monitor tick lands late and the playhead is already past a vamp's end,
# the vamp is still entered as long as we are within this window (milliseconds).
LOOP_ENTRY_GRACE_MS = 250

# Loop/marker edits are written to disk this long after the last change (ms).
# stop() and cleanup() flush immediately.
SAVE_DEBOUNCE_MS = 500

# GO button / pedal debounce: a second GO within this window is ignored (ms)
GO_DEBOUNCE_MS = 300

# OSC output defaults (user preferences "osc" override these at runtime)
OSC_DEFAULT_ENABLED = False
OSC_DEFAULT_HOST = "127.0.0.1"
OSC_DEFAULT_PORT = 53000
OSC_DEFAULT_SCHEME = "loopstation"   # "loopstation" | "qlab"

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
COLOR_VAMP = "#66bb6a"        # Vamp text / icons (cue sheet, cue details)
COLOR_LOOP_GHOST = "#444444"
COLOR_LOOP_GHOST_ALPHA = 0.2
COLOR_BTN_PRIMARY = ""
COLOR_BTN_SUCCESS = ""
COLOR_BTN_WARNING = ""
COLOR_BTN_DANGER = ""
COLOR_BTN_DISABLED = ""
COLOR_TEXT = ""
COLOR_TEXT_DIM = ""
COLOR_TEXT_DISABLED = ""
COLOR_BORDER = ""            # Hairline borders / dividers
COLOR_HOVER = ""             # Hover fill for neutral buttons
COLOR_SKIP_REGION = ""       # For the waveform box
COLOR_SKIP_CANDIDATE = ""    # For text highlighting
COLOR_BTN_SKIP = ""          # For buttons

def mix(a: str, b: str, t: float) -> str:
    """Blend hex colour a toward b by t (0 = a, 1 = b)."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def tint(color: str, amount: float = 0.18) -> str:
    """A panel-coloured surface washed with color (state cards, selected rows).

    Works on light and dark themes alike, unlike a fixed dark hex."""
    return mix(COLOR_BG_MEDIUM, color, amount)


def load_theme():
    """Loads the user's theme preference and updates global color variables."""
    global COLOR_BG_DARK, COLOR_BG_MEDIUM, COLOR_BG_LIGHT, COLOR_WAVEFORM, \
           COLOR_WAVEFORM_BG, COLOR_BTN_PRIMARY, COLOR_BTN_SUCCESS, \
           COLOR_BTN_WARNING, COLOR_BTN_DANGER, COLOR_TEXT, COLOR_TEXT_DIM, \
           COLOR_BTN_TEXT, COLOR_SKIP_REGION, COLOR_SKIP_CANDIDATE, COLOR_BTN_SKIP, \
           COLOR_BTN_DISABLED, COLOR_TEXT_DISABLED, COLOR_BORDER, COLOR_HOVER

    # 1. Load User Preference
    _user_theme = get_theme_preference()
    if not _user_theme or _user_theme not in THEMES:
        _user_theme = DEFAULT_THEME

    # 2. Get the Palette
    _palette = THEMES[_user_theme]

    # 3. Apply Colors
    COLOR_BG_DARK = _palette["bg_primary"]
    COLOR_BG_MEDIUM = _palette["bg_secondary"]
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

    # 4. Derived surfaces: window < panel < raised control, plus hairlines.
    #    Blending toward the text colour works for light and dark themes.
    COLOR_BG_LIGHT = mix(COLOR_BG_MEDIUM, COLOR_TEXT, 0.07)
    COLOR_BORDER = mix(COLOR_BG_MEDIUM, COLOR_TEXT, 0.14)
    COLOR_HOVER = mix(COLOR_BG_MEDIUM, COLOR_TEXT, 0.16)
    COLOR_BTN_DISABLED = mix(COLOR_BG_MEDIUM, COLOR_TEXT, 0.09)
    COLOR_TEXT_DISABLED = mix(COLOR_BG_MEDIUM, COLOR_TEXT_DIM, 0.75)

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

# Right sidebar (Notes / Cue Details)
RIGHT_SIDEBAR_WIDTH = 320
RIGHT_SIDEBAR_MIN_WIDTH = 280

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

# Supported audio formats. Everything is decoded by ffmpeg to PCM before
# playback, so any format ffmpeg can read works; this list is what the
# library browser shows.
SUPPORTED_FORMATS = ('.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac',
                     '.wma', '.aiff', '.aif', '.opus')

# =============================================================================
# DEFAULT VAMP/MARKER NAMES
# =============================================================================

DEFAULT_VAMP_NAME = "Vamp"
DEFAULT_MARKER_NAME = "Cue"

# =============================================================================
# CUE NOTES / TAGS
# =============================================================================

# Available tag categories for cues and vamps
AVAILABLE_TAGS = ["Director", "Tech", "Lighting", "Sound", "Stage", "Costumes", "Props", "Other"]

# Tag colors for display
TAG_COLORS = {
    "Director":  "#e74c3c",  # Red
    "Tech":      "#3498db",  # Blue
    "Lighting":  "#f1c40f",  # Yellow
    "Sound":     "#2ecc71",  # Green
    "Stage":     "#9b59b6",  # Purple
    "Costumes":  "#e67e22",  # Orange
    "Props":     "#1abc9c",  # Teal
    "Other":     "#95a5a6",  # Gray
}
