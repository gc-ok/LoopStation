"""
Platform font families for the UI.

Tk silently substitutes the system font for a family that isn't installed,
so "Segoe UI" / "Consolas" on macOS gave proportional digits (the clock
changed width as it ran). Use these instead of naming a family:

    from utils.fonts import FONT_UI, FONT_MONO
    label = ctk.CTkLabel(parent, font=(FONT_UI, 14, "bold"))

FONT_MONO is for anything that must not jitter: clocks, timecodes, counters.
"""

import sys

if sys.platform == "darwin":
    # The macOS system font (SF Pro) under the name Tk reports for it
    FONT_UI = ".AppleSystemUIFont"
    FONT_MONO = "Menlo"
elif sys.platform == "win32":
    FONT_UI = "Segoe UI"
    FONT_MONO = "Consolas"
else:
    FONT_UI = "DejaVu Sans"
    FONT_MONO = "DejaVu Sans Mono"
