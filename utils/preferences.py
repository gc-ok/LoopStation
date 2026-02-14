import os
import sys
import json
import logging

def _get_prefs_dir():
    """Get the writable data directory for preferences."""
    if getattr(sys, 'frozen', False) and sys.platform == 'darwin':
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "LoopStation")
    else:
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# Path to the preferences file
PREFS_FILE = os.path.join(_get_prefs_dir(), "user_preferences.json")

def load_preferences():
    """Load user preferences from JSON."""
    if not os.path.exists(PREFS_FILE):
        return {}
    
    try:
        with open(PREFS_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading preferences: {e}")
        return {}

def save_preferences(prefs):
    """Save user preferences dictionary to JSON."""
    try:
        data_dir = os.path.dirname(PREFS_FILE)
        if not os.path.exists(data_dir):
            os.makedirs(data_dir, exist_ok=True)
            
        # Merge with existing
        current = load_preferences()
        current.update(prefs)
        
        with open(PREFS_FILE, 'w') as f:
            json.dump(current, f, indent=2)
            
    except Exception as e:
        print(f"Error saving preferences: {e}")

def get_theme_preference():
    """Get the name of the saved theme."""
    prefs = load_preferences()
    return prefs.get("theme", None)

def set_theme_preference(theme_name):
    """Save the theme preference."""
    save_preferences({"theme": theme_name})
