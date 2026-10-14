"""
Filesystem locations for Loop Station.

Single source of truth for "where is the executable", "where are bundled
assets", and "where may we write user data". Every other module should
import from here instead of re-deriving these paths.

Rules:
- Running from source: data lives in <repo>/data so developers can see it.
- Frozen (PyInstaller) on macOS: the .app bundle is read-only (and may be
  translocated), so data goes to ~/Library/Application Support/LoopStation.
- Frozen on Windows: the install folder is usually read-only (Program
  Files), so data goes to %LOCALAPPDATA%\\LoopStation.
- Frozen elsewhere (Linux): ~/.local/share/LoopStation.
"""

import os
import sys

APP_NAME = "LoopStation"


def is_frozen() -> bool:
    """True when running inside a PyInstaller bundle."""
    return bool(getattr(sys, 'frozen', False))


def get_base_path() -> str:
    """Directory containing the executable (frozen) or the project root (source)."""
    if is_frozen():
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_asset_path(relative_path: str) -> str:
    """Path to a read-only bundled asset such as logo.png."""
    if is_frozen():
        base = getattr(sys, '_MEIPASS', get_base_path())
    else:
        base = get_base_path()
    return os.path.join(base, relative_path)


def get_data_dir() -> str:
    """Writable per-user data directory. Created on first call."""
    if not is_frozen():
        path = os.path.join(get_base_path(), "data")
    elif sys.platform == 'darwin':
        path = os.path.join(os.path.expanduser("~"), "Library",
                            "Application Support", APP_NAME)
    elif os.name == 'nt':
        root = os.environ.get('LOCALAPPDATA') or os.path.expanduser("~")
        path = os.path.join(root, APP_NAME)
    else:
        root = os.environ.get('XDG_DATA_HOME') or os.path.join(
            os.path.expanduser("~"), ".local", "share")
        path = os.path.join(root, APP_NAME)

    os.makedirs(path, exist_ok=True)
    _migrate_legacy_data(path)
    return path


_migration_checked = False


def _migrate_legacy_data(new_dir: str) -> None:
    """
    One-time copy of data from the old location (<exe dir>/data) used by
    earlier frozen Windows builds, so existing users keep their vamps and
    cue notes after upgrading. Runs once per process; never overwrites.
    """
    global _migration_checked
    if _migration_checked or not is_frozen() or os.name != 'nt':
        _migration_checked = True
        return
    _migration_checked = True

    old_dir = os.path.join(get_base_path(), "data")
    if not os.path.isdir(old_dir) or os.path.normcase(old_dir) == os.path.normcase(new_dir):
        return

    import shutil
    for name in ("loop_data.json", "user_preferences.json", "show_data.json"):
        src = os.path.join(old_dir, name)
        dst = os.path.join(new_dir, name)
        if os.path.isfile(src) and not os.path.exists(dst):
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass


def get_log_dir() -> str:
    """Writable directory for log files. Created on first call."""
    path = os.path.join(get_data_dir(), "logs")
    os.makedirs(path, exist_ok=True)
    return path
