"""
Show (saved setlist / running order) model for Loop Station. No Tk.

A Show is an ordered list of ShowEntry items: songs (an audio file path)
and dividers ("Act 1"). Cue data (vamps, holds, notes, tempo/key) still
belongs to the SONG and lives in loop_data.json by audio fingerprint; a
show only stores the running order and per-entry flags:

- ``cut``   -- the entry is skipped by GO (still listed, greyed out)
- ``segue`` -- when this song ends naturally, the next playable entry
  starts automatically (like an automatic GO)

The same file may appear in several entries (a reprise).

Two kinds of show:
- Explicit show file (``*.loopshow``, ``file_path`` set): saved only when
  the user saves (``dirty`` tracks unsaved edits).
- Folder-derived show (``folder`` set, ``file_path`` None): built from the
  audio files of a folder and remembered automatically in the app data dir
  under ``shows/<hash of folder>.loopshow`` (see ``folder_show_path``).
  Reopening the folder restores that order; new files in the folder are
  appended and deleted ones stay listed as missing.

File format (JSON, UTF-8)::

    {"format": "loopstation-show", "version": 1, "name": "...",
     "relative_to": "file" | "folder", "source_folder": str | null,
     "entries": [{"id", "kind": "song"|"divider", "title", "cut", "segue",
                  "rel_path": "sub/Song.wav" | null, "abs_path": str | null}],
     "_unreadable": [raw entries that could not be parsed, kept verbatim]}

``rel_path`` uses "/" separators and is relative to the show file's folder
(explicit) or to ``source_folder`` (autosave). On load the relative path is
tried first, then ``abs_path``. A relative path is impossible across drives
(Windows ``C:`` vs ``D:`` or a UNC share): then only ``abs_path`` is stored.

Thread safety: edits take the show's lock and replace ``entries`` with a new
list (never mutate it in place), so other threads may iterate a snapshot.
"""

import os
import sys
import json
import time
import uuid
import hashlib
import logging
import threading
import unicodedata
from typing import Any, Dict, Iterable, List, Optional

from config import SUPPORTED_FORMATS
from utils.fileio import read_bytes_with_retry, write_text_atomic, backup_copy

logger = logging.getLogger("LoopStation.Show")

SHOW_FORMAT = "loopstation-show"
SHOW_VERSION = 1
SHOW_EXTENSION = ".loopshow"
SHOWS_SUBDIR = "shows"
UNREADABLE_KEY = "_unreadable"
KINDS = ("song", "divider")

# How long an "is the file there?" answer is reused (seconds). Keeps
# standby()/snapshots from stat()ing a slow network share many times a second.
MISSING_CACHE_S = 2.0


class ShowFileError(ValueError):
    """A show file could not be read or is not a show."""


# ----------------------------------------------------------------- helpers --

def is_audio_file(path: str) -> bool:
    """True for a visible file with a supported audio extension (no stat)."""
    name = os.path.basename(path or "")
    return bool(name) and not name.startswith('.') and name.lower().endswith(SUPPORTED_FORMATS)


def norm_path(path: str) -> str:
    """Comparable form of a path: absolute, normcase'd, Unicode NFC."""
    if not path:
        return ""
    return unicodedata.normalize("NFC", os.path.normcase(os.path.abspath(path)))


def _folder_key(folder: str) -> str:
    key = norm_path(folder).rstrip("/\\") or norm_path(folder)
    if sys.platform == "darwin" or os.name == "nt":
        key = key.casefold()   # default file systems are case-insensitive
    return key


def folder_show_path(folder: str, data_dir: Optional[str] = None) -> str:
    """Autosave file of the folder-derived show for ``folder``."""
    if data_dir is None:
        from utils.paths import get_data_dir
        data_dir = get_data_dir()
    digest = hashlib.sha1(_folder_key(folder).encode("utf-8")).hexdigest()[:16]
    return os.path.join(data_dir, SHOWS_SUBDIR, digest + SHOW_EXTENSION)


def list_audio_files(folder: str) -> List[str]:
    """Audio files directly in ``folder`` (absolute), sorted like the library (casefold)."""
    try:
        names = os.listdir(folder)
    except OSError as e:
        logger.error(f"Cannot list folder {folder!r}: {e}")
        return []
    names = [n for n in names if is_audio_file(n) and os.path.isfile(os.path.join(folder, n))]
    names.sort(key=lambda n: (n.casefold(), n))
    return [os.path.join(folder, n) for n in names]


def _relative(path: str, base_dir: Optional[str]) -> Optional[str]:
    """``path`` relative to ``base_dir`` with "/" separators, or None (other drive)."""
    if not base_dir or not path:
        return None
    try:
        a, b = os.path.abspath(path), os.path.abspath(base_dir)
        if os.path.splitdrive(a)[0].lower() != os.path.splitdrive(b)[0].lower():
            return None
        rel = os.path.relpath(a, b)
    except ValueError:   # Windows: different drives / UNC share
        return None
    return rel.replace(os.sep, "/")


def _resolve(rel: Optional[str], abs_path: Optional[str], base_dir: Optional[str]) -> Optional[str]:
    """Runtime path: relative (if the file is there), else absolute, else the best guess."""
    candidate = None
    if rel and base_dir:
        parts = [p for p in rel.replace("\\", "/").split("/") if p]
        if parts:
            candidate = os.path.normpath(os.path.join(base_dir, *parts))
            if os.path.isfile(candidate):
                return candidate
    if abs_path and os.path.isfile(abs_path):
        return abs_path
    return candidate or abs_path


def _stem(path: Optional[str]) -> str:
    return os.path.splitext(os.path.basename(path or ""))[0]


# ------------------------------------------------------------------- entry --

class ShowEntry:
    """
    One row of a show.

    Attributes:
        id: uuid hex string (stable across saves)
        kind: "song" | "divider"
        path: absolute audio path at runtime (None for a divider)
        title: display name (default: the file stem; e.g. "Act 1" for a divider)
        cut: skipped by GO
        segue: when this song ends naturally, the next playable entry starts
    """
    __slots__ = ("id", "kind", "path", "title", "cut", "segue")

    def __init__(self, kind: str = "song", path: Optional[str] = None,
                 title: Optional[str] = None, cut: bool = False, segue: bool = False,
                 entry_id: Optional[str] = None):
        if kind not in KINDS:
            raise ValueError(f"unknown entry kind {kind!r}")
        self.id = entry_id or uuid.uuid4().hex
        self.kind = kind
        self.path = os.path.abspath(path) if (kind == "song" and path) else None
        if kind == "song" and not self.path:
            raise ValueError("a song entry needs a path")
        title = (title or "").strip()
        self.title = title or (_stem(self.path) if kind == "song" else "Divider")
        self.cut = bool(cut)
        self.segue = bool(segue)

    def copy(self, new_id: bool = False) -> "ShowEntry":
        return ShowEntry(self.kind, self.path, self.title, self.cut, self.segue,
                         entry_id=None if new_id else self.id)

    def to_dict(self, base_dir: Optional[str] = None) -> dict:
        """Serialized form for the file (paths relative to ``base_dir`` + absolute)."""
        return {
            "id": self.id, "kind": self.kind, "title": self.title,
            "cut": self.cut, "segue": self.segue,
            "rel_path": _relative(self.path, base_dir) if self.path else None,
            "abs_path": self.path,
        }

    @classmethod
    def from_dict(cls, data: dict, base_dir: Optional[str] = None) -> "ShowEntry":
        if not isinstance(data, dict):
            raise ValueError("entry is not an object")
        kind = data.get("kind", "song")
        if kind not in KINDS:
            raise ValueError(f"unknown entry kind {kind!r}")
        title = data.get("title")
        if title is not None and not isinstance(title, str):
            raise ValueError("title is not a string")
        entry_id = data.get("id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            entry_id = None
        path = None
        if kind == "song":
            rel, abs_path = data.get("rel_path"), data.get("abs_path")
            rel = rel if isinstance(rel, str) and rel.strip() else None
            abs_path = abs_path if isinstance(abs_path, str) and abs_path.strip() else None
            if rel is None and abs_path is None:
                raise ValueError("song entry has no path")
            path = _resolve(rel, abs_path, base_dir)
        return cls(kind, path, title, bool(data.get("cut", False)),
                   bool(data.get("segue", False)), entry_id=entry_id)

    def snapshot(self) -> dict:
        """Plain dict for the UI (no file-system checks)."""
        return {"id": self.id, "kind": self.kind, "path": self.path, "title": self.title,
                "cut": self.cut, "segue": self.segue}

    def __repr__(self):
        return f"ShowEntry({self.kind}, {self.title!r}, cut={self.cut}, segue={self.segue})"


# -------------------------------------------------------------------- show --

class Show:
    """
    An ordered running order of songs and dividers.

    Attributes:
        name: display name
        file_path: the explicit ``.loopshow`` file (None = unsaved / folder-derived)
        folder: source folder of a folder-derived show (None otherwise)
        entries: list[ShowEntry] -- replaced on every edit, never mutated in place
        dirty: unsaved edits exist
        unreadable: raw entries from the file that could not be parsed; they
            are written back unchanged on save
    """

    def __init__(self, name: str = "Untitled show", entries: Optional[Iterable[ShowEntry]] = None):
        self.name = name
        self.file_path: Optional[str] = None
        self.folder: Optional[str] = None
        self.entries: List[ShowEntry] = list(entries or [])
        self.dirty = False
        self.unreadable: List[Any] = []
        self.load_problems: List[str] = []
        self._lock = threading.RLock()
        self._missing_cache: Dict[str, tuple] = {}

    # ------------------------------------------------------------ queries --

    @property
    def is_folder_derived(self) -> bool:
        return self.folder is not None and self.file_path is None

    def __len__(self):
        return len(self.entries)

    def get(self, entry_id: Optional[str]) -> Optional[ShowEntry]:
        if not entry_id:
            return None
        return next((e for e in self.entries if e.id == entry_id), None)

    def index_of(self, entry_id: Optional[str]) -> int:
        """Index of the entry, or -1."""
        for i, e in enumerate(self.entries):
            if e.id == entry_id:
                return i
        return -1

    def is_missing(self, entry: ShowEntry) -> bool:
        """A song entry whose file is not there (cached for MISSING_CACHE_S)."""
        if entry is None or entry.kind != "song":
            return False
        now = time.monotonic()
        hit = self._missing_cache.get(entry.path)
        if hit is not None and now - hit[0] < MISSING_CACHE_S:
            return hit[1]
        missing = not os.path.isfile(entry.path)
        self._missing_cache[entry.path] = (now, missing)
        return missing

    def invalidate_missing_cache(self) -> None:
        self._missing_cache = {}

    def playable(self, entry: Optional[ShowEntry]) -> bool:
        """A song that GO may advance to: not a divider, not cut, not missing."""
        return entry is not None and entry.kind == "song" and not entry.cut \
            and not self.is_missing(entry)

    def next_playable_from(self, index: int) -> Optional[ShowEntry]:
        """First playable entry at ``index`` or later."""
        entries = self.entries
        for e in entries[max(0, index):]:
            if self.playable(e):
                return e
        return None

    def next_playable_after(self, entry_id: Optional[str]) -> Optional[ShowEntry]:
        """
        The first playable entry after ``entry_id``. None/unknown id = from
        the start of the show. None at the end of the show.
        """
        i = self.index_of(entry_id)
        return self.next_playable_from(i + 1 if i >= 0 else 0)

    def find_by_path(self, path: str, after_id: Optional[str] = None) -> Optional[ShowEntry]:
        """Song entry for ``path``: the first match after ``after_id``, else the first match."""
        target = norm_path(path)
        if not target:
            return None
        entries = self.entries
        matches = [i for i, e in enumerate(entries) if e.kind == "song" and norm_path(e.path) == target]
        if not matches:
            return None
        start = self.index_of(after_id)
        for i in matches:
            if i > start >= 0:
                return entries[i]
        return entries[matches[0]]

    def snapshot(self) -> List[dict]:
        """Plain dicts (with "index", "missing", "playable") for the UI."""
        out = []
        for i, e in enumerate(self.entries):
            d = e.snapshot()
            d["index"] = i
            d["missing"] = self.is_missing(e)
            d["playable"] = self.playable(e)
            out.append(d)
        return out

    # ------------------------------------------------------------ editing --

    def _insert(self, new: List[ShowEntry], index: Optional[int]) -> None:
        entries = list(self.entries)
        if index is None or index > len(entries):
            index = len(entries)
        index = max(0, int(index))
        entries[index:index] = new
        self.entries = entries
        self.dirty = True

    def add_songs(self, paths: Iterable[str], index: Optional[int] = None) -> List[str]:
        """Insert existing audio files (others are ignored). Returns the new entry ids."""
        with self._lock:
            new = []
            for p in paths or []:
                if not isinstance(p, str) or not is_audio_file(p) or not os.path.isfile(p):
                    logger.info(f"Not adding {p!r} to the show (not an audio file / missing)")
                    continue
                new.append(ShowEntry("song", p))
            if new:
                self._insert(new, index)
            return [e.id for e in new]

    def add_divider(self, title: str = "", index: Optional[int] = None) -> str:
        with self._lock:
            entry = ShowEntry("divider", None, title or "Divider")
            self._insert([entry], index)
            return entry.id

    def move(self, entry_id: str, new_index: int) -> bool:
        """Move an entry so it ends up at ``new_index`` (clamped)."""
        with self._lock:
            i = self.index_of(entry_id)
            if i < 0:
                return False
            entries = list(self.entries)
            entry = entries.pop(i)
            new_index = max(0, min(int(new_index), len(entries)))
            if new_index == i:
                return False
            entries.insert(new_index, entry)
            self.entries = entries
            self.dirty = True
            return True

    def remove(self, entry_id: str) -> bool:
        with self._lock:
            i = self.index_of(entry_id)
            if i < 0:
                return False
            entries = list(self.entries)
            del entries[i]
            self.entries = entries
            self.dirty = True
            return True

    def duplicate(self, entry_id: str) -> Optional[str]:
        """Copy an entry (new id) right after it (a reprise). Returns the new id."""
        with self._lock:
            i = self.index_of(entry_id)
            if i < 0:
                return None
            copy = self.entries[i].copy(new_id=True)
            self._insert([copy], i + 1)
            return copy.id

    def _set(self, entry_id: str, attr: str, value) -> bool:
        with self._lock:
            entry = self.get(entry_id)
            if entry is None:
                return False
            if getattr(entry, attr) == value:
                return True
            setattr(entry, attr, value)
            self.entries = list(self.entries)
            self.dirty = True
            return True

    def rename(self, entry_id: str, title: str) -> bool:
        """Set the display title; empty restores the default (file stem / "Divider")."""
        entry = self.get(entry_id)
        if entry is None:
            return False
        title = (title or "").strip() or (_stem(entry.path) if entry.kind == "song" else "Divider")
        return self._set(entry_id, "title", title)

    def set_cut(self, entry_id: str, cut: bool) -> bool:
        entry = self.get(entry_id)
        if entry is None or entry.kind != "song":
            return False
        return self._set(entry_id, "cut", bool(cut))

    def set_segue(self, entry_id: str, segue: bool) -> bool:
        entry = self.get(entry_id)
        if entry is None or entry.kind != "song":
            return False
        return self._set(entry_id, "segue", bool(segue))

    def relink(self, entry_id: str, new_path: str) -> List[str]:
        """
        Point a song entry at ``new_path`` (an existing audio file). Other
        MISSING song entries whose file name exists in the new file's folder
        are relinked too. Returns the ids of all relinked entries ([] = failed).
        A title that was the old default (file stem) follows the new file.
        """
        with self._lock:
            entry = self.get(entry_id)
            if entry is None or entry.kind != "song" or not new_path \
                    or not is_audio_file(new_path) or not os.path.isfile(new_path):
                return []
            self.invalidate_missing_cache()
            folder = os.path.dirname(os.path.abspath(new_path))
            changed = [entry_id]
            others = [e for e in self.entries
                      if e.id != entry_id and e.kind == "song" and self.is_missing(e)]
            self._relink_one(entry, new_path)
            for e in others:
                candidate = os.path.join(folder, os.path.basename(e.path))
                if os.path.isfile(candidate):
                    self._relink_one(e, candidate)
                    changed.append(e.id)
            self.entries = list(self.entries)
            self.dirty = True
            self.invalidate_missing_cache()
            return changed

    @staticmethod
    def _relink_one(entry: ShowEntry, new_path: str) -> None:
        if entry.title == _stem(entry.path):
            entry.title = _stem(new_path)
        entry.path = os.path.abspath(new_path)

    # ------------------------------------------------------- serialization --

    def to_dict(self, base_dir: Optional[str], relative_to: str = "file") -> dict:
        with self._lock:
            data = {
                "format": SHOW_FORMAT, "version": SHOW_VERSION, "name": self.name,
                "relative_to": relative_to, "source_folder": self.folder,
                "entries": [e.to_dict(base_dir) for e in self.entries],
            }
            if self.unreadable:
                data[UNREADABLE_KEY] = list(self.unreadable)
            return data

    def _apply_dict(self, data: Any, base_dir: Optional[str]) -> None:
        """Fill this show from parsed file data (malformed entries are kept raw)."""
        if not isinstance(data, dict):
            raise ShowFileError("not a show file (no JSON object)")
        if data.get("format") != SHOW_FORMAT:
            raise ShowFileError(f"not a show file (format {data.get('format')!r})")
        version = data.get("version")
        if isinstance(version, (int, float)) and version > SHOW_VERSION:
            logger.warning(f"Show file version {version} is newer than {SHOW_VERSION}; "
                           "reading what is understood")
        name = data.get("name")
        if isinstance(name, str) and name.strip():
            self.name = name
        kept = data.get(UNREADABLE_KEY)
        unreadable = list(kept) if isinstance(kept, list) else []
        problems = []
        raw_entries = data.get("entries", [])
        if not isinstance(raw_entries, list):
            problems.append("entries: not a list")
            unreadable.append(raw_entries)
            raw_entries = []
        entries, seen = [], set()
        for i, raw in enumerate(raw_entries):
            try:
                entry = ShowEntry.from_dict(raw, base_dir)
            except Exception as e:
                logger.warning(f"Skipping unreadable show entry [{i}] {raw!r}: {e}")
                problems.append(f"entry {i}: {e}")
                if raw not in unreadable:
                    unreadable.append(raw)
                continue
            if entry.id in seen:
                entry.id = uuid.uuid4().hex
            seen.add(entry.id)
            entries.append(entry)
        self.entries = entries
        self.unreadable = unreadable
        self.load_problems = problems

    @staticmethod
    def _read_json(path: str) -> Any:
        """Parsed JSON of ``path``. Raises FileNotFoundError, OSError or ShowFileError."""
        raw = read_bytes_with_retry(path)
        try:
            return json.loads(raw.decode("utf-8-sig"))
        except ValueError as e:
            err = ShowFileError(f"show file is not valid JSON: {e}")
            err.raw = raw
            raise err

    @classmethod
    def load(cls, path: str) -> "Show":
        """
        Open an explicit show file. Raises ShowFileError (bad content) or
        OSError (cannot read). Unparsable entries are skipped (see
        ``load_problems``) and kept raw in ``unreadable`` for the next save.
        """
        path = os.path.abspath(path)
        data = cls._read_json(path)
        show = cls(name=_stem(path))
        relative_to = data.get("relative_to") if isinstance(data, dict) else None
        base = os.path.dirname(path)
        if relative_to == "folder" and isinstance(data.get("source_folder"), str):
            base = data["source_folder"]
        show._apply_dict(data, base)
        show.file_path = path
        show.dirty = False
        return show

    def save(self, path: Optional[str] = None) -> str:
        """
        Write the show to ``path`` (default: its ``file_path``) atomically,
        with song paths relative to that file's folder. The show then is an
        explicit show file (no longer folder-derived). Raises ValueError when
        there is no path, OSError when the write fails. Returns the path.
        """
        with self._lock:
            target = path or self.file_path
            if not target:
                raise ValueError("show has no file path; pass one (Save As)")
            target = os.path.abspath(target)
            if not target.lower().endswith(SHOW_EXTENSION):
                target += SHOW_EXTENSION
            data = self.to_dict(os.path.dirname(target), relative_to="file")
            data["source_folder"] = None
            write_text_atomic(target, json.dumps(data, indent=2, ensure_ascii=False))
            if path and (self.file_path is None or norm_path(target) != norm_path(self.file_path)):
                self.name = _stem(target)
            self.file_path = target
            self.folder = None
            self.dirty = False
            return target

    # --------------------------------------------------- folder-derived ---

    @classmethod
    def from_folder(cls, folder: str, data_dir: Optional[str] = None,
                    restore: bool = True) -> "Show":
        """
        Folder-derived show: the folder's audio files sorted casefold. With
        ``restore`` (default) a previously autosaved order for this folder is
        restored; files added to the folder since are appended (sorted) and
        deleted ones stay as missing entries. A corrupt autosave is backed up
        (``.corrupt-<stamp>``) and the plain folder order is used.
        ``dirty`` is True when the result differs from the autosave.
        """
        folder = os.path.abspath(folder)
        show = cls(name=os.path.basename(folder.rstrip("/\\")) or folder)
        show.folder = folder
        files = list_audio_files(folder)
        restored = False
        if restore:
            auto = folder_show_path(folder, data_dir)
            try:
                data = cls._read_json(auto)
                show._apply_dict(data, folder)
                if isinstance(data, dict) and isinstance(data.get("name"), str) and data["name"].strip():
                    show.name = data["name"]
                restored = True
            except FileNotFoundError:
                pass
            except ShowFileError as e:
                logger.error(f"Saved order for {folder!r} is unreadable ({e}); using folder order")
                raw = getattr(e, "raw", None)
                if raw is not None and backup_copy(raw, auto, "corrupt") is None:
                    logger.error("Could not back up the unreadable show autosave")
                show.entries, show.unreadable = [], []
            except OSError as e:
                logger.error(f"Saved order for {folder!r} could not be read ({e}); using folder order")
                show.entries, show.unreadable = [], []
        known = {norm_path(e.path) for e in show.entries if e.kind == "song"}
        added = [ShowEntry("song", p) for p in files if norm_path(p) not in known]
        if added:
            show.entries = show.entries + added
        show.dirty = (not restored) or bool(added)
        if not show.entries and not restored:
            show.dirty = False   # empty folder: nothing worth remembering
        show.file_path = None
        return show

    def autosave(self, data_dir: Optional[str] = None) -> Optional[str]:
        """
        Remember a folder-derived show's order in the app data dir. No-op
        (returns None) for other shows. Raises OSError when the write fails.
        """
        with self._lock:
            if not self.is_folder_derived:
                return None
            target = folder_show_path(self.folder, data_dir)
            data = self.to_dict(self.folder, relative_to="folder")
            write_text_atomic(target, json.dumps(data, indent=2, ensure_ascii=False))
            self.dirty = False
            return target
