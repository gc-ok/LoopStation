"""
User preferences (theme, etc.) stored as JSON in the per-user data directory.

Writes are serialised by a module lock (the UI, MIDI and web threads all
save), go through a unique temp file + os.replace(), and never overwrite a
preferences file that exists but could not be read: an unparsable file is
first copied to user_preferences.json.corrupt-<stamp>.
"""

import os
import json
import logging
import threading

from utils.paths import get_data_dir
from utils.fileio import read_bytes_with_retry, write_text_atomic, backup_copy

logger = logging.getLogger("LoopStation.Preferences")

# Path to the preferences file
PREFS_FILE = os.path.join(get_data_dir(), "user_preferences.json")

# Serialises read-modify-write cycles across threads
_lock = threading.RLock()


def _read_prefs():
    """
    (data, status, raw) with status "ok" | "missing" | "corrupt" | "unreadable";
    ``raw`` is the file's bytes when "corrupt" (for the backup), else None.
    """
    try:
        raw = read_bytes_with_retry(PREFS_FILE)
    except FileNotFoundError:
        return {}, "missing", None
    except OSError as e:
        logger.warning(f"Error reading preferences: {e}")
        return {}, "unreadable", None
    try:
        data = json.loads(raw.decode('utf-8-sig'))
    except ValueError as e:   # JSONDecodeError and UnicodeDecodeError
        logger.warning(f"Preferences file is not valid JSON: {e}")
        return {}, "corrupt", raw
    if not isinstance(data, dict):
        logger.warning("Preferences file does not contain an object")
        return {}, "corrupt", raw
    return data, "ok", None


def load_preferences() -> dict:
    """Load user preferences from JSON. Returns {} if missing or unreadable."""
    with _lock:
        data, _, _ = _read_prefs()
        return data


def _merge(current: dict, prefs: dict) -> None:
    """
    Merge ``prefs`` into ``current``. A dict value is merged one level deep
    (saving {"audio": {"buffer": 256}} keeps "audio"."output_device"); values
    below that are replaced wholesale (e.g. the MIDI mapping table).
    """
    for key, value in prefs.items():
        old = current.get(key)
        if isinstance(value, dict) and isinstance(old, dict):
            merged = dict(old)
            merged.update(value)
            current[key] = merged
        else:
            current[key] = value


def save_preferences(prefs: dict) -> None:
    """Merge `prefs` into the stored preferences and write atomically."""
    with _lock:
        try:
            current, status, raw = _read_prefs()
            if status == "unreadable":
                # Exists but locked/unreadable: writing now would wipe every
                # other preference. Skip this save; the next one retries.
                logger.error("Preferences not saved: the existing file could not be read")
                return
            if status == "corrupt":
                backup = backup_copy(raw, PREFS_FILE, "corrupt")
                if backup is None:
                    logger.error("Preferences not saved: could not back up the unreadable file")
                    return
                logger.warning(f"Unreadable preferences backed up to {os.path.basename(backup)}")
            _merge(current, prefs)
            write_text_atomic(PREFS_FILE, json.dumps(current, indent=2))
        except (OSError, TypeError, ValueError) as e:
            logger.error(f"Error saving preferences: {e}")


def get_theme_preference():
    """Get the name of the saved theme, or None."""
    return load_preferences().get("theme")


def set_theme_preference(theme_name: str) -> None:
    """Save the theme preference."""
    save_preferences({"theme": theme_name})


# ----------------------------------------------------------------- shows --
# Stored under "show": {"recent": [{"kind": "file"|"folder", "path": str}, ...],
#                       "last_open": {"kind": ..., "path": ...} | None}

MAX_RECENT_SHOWS = 8
SHOW_KINDS = ("file", "folder")


def _show_prefs() -> dict:
    value = load_preferences().get("show")
    return value if isinstance(value, dict) else {}


def _valid_show_ref(item) -> bool:
    return (isinstance(item, dict) and item.get("kind") in SHOW_KINDS
            and isinstance(item.get("path"), str) and bool(item.get("path")))


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def get_recent_shows() -> list:
    """Recently opened shows, newest first: [{"kind": "file"|"folder", "path": str}]."""
    recent = _show_prefs().get("recent")
    if not isinstance(recent, list):
        return []
    return [dict(item) for item in recent if _valid_show_ref(item)][:MAX_RECENT_SHOWS]


def add_recent_show(kind: str, path: str) -> None:
    """Put a show file / folder at the top of the recent list (max MAX_RECENT_SHOWS)."""
    if kind not in SHOW_KINDS or not path:
        return
    with _lock:
        path = os.path.abspath(path)
        recent = [r for r in get_recent_shows()
                  if not (r["kind"] == kind and _same_path(r["path"], path))]
        recent.insert(0, {"kind": kind, "path": path})
        save_preferences({"show": {"recent": recent[:MAX_RECENT_SHOWS]}})


def remove_recent_show(kind: str, path: str) -> None:
    with _lock:
        recent = [r for r in get_recent_shows()
                  if not (r["kind"] == kind and _same_path(r["path"], path))]
        save_preferences({"show": {"recent": recent}})


def get_last_show():
    """The show open when the app last ran: {"kind", "path"} or None."""
    last = _show_prefs().get("last_open")
    return dict(last) if _valid_show_ref(last) else None


def set_last_show(kind, path) -> None:
    """Remember the open show (kind "file"|"folder"); kind None clears it."""
    if kind is None or not path:
        save_preferences({"show": {"last_open": None}})
    elif kind in SHOW_KINDS:
        save_preferences({"show": {"last_open": {"kind": kind, "path": os.path.abspath(path)}}})
