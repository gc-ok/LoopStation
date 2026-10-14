"""
User preferences (theme, etc.) stored as JSON in the per-user data directory.
"""

import os
import json
import logging

from utils.paths import get_data_dir

logger = logging.getLogger("LoopStation.Preferences")

# Path to the preferences file
PREFS_FILE = os.path.join(get_data_dir(), "user_preferences.json")


def load_preferences() -> dict:
    """Load user preferences from JSON. Returns {} if missing or unreadable."""
    if not os.path.exists(PREFS_FILE):
        return {}

    try:
        with open(PREFS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as e:
        logger.warning(f"Error loading preferences: {e}")
        return {}


def save_preferences(prefs: dict) -> None:
    """Merge `prefs` into the stored preferences and write atomically."""
    try:
        current = load_preferences()
        current.update(prefs)

        tmp_path = PREFS_FILE + ".tmp"
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(current, f, indent=2)
        os.replace(tmp_path, PREFS_FILE)
    except OSError as e:
        logger.error(f"Error saving preferences: {e}")


def get_theme_preference():
    """Get the name of the saved theme, or None."""
    return load_preferences().get("theme")


def set_theme_preference(theme_name: str) -> None:
    """Save the theme preference."""
    save_preferences({"theme": theme_name})
