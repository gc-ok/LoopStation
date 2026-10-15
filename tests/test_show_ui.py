"""
UI tests for the Show (saved setlist) sidebar, GO through the show, instant
GO (preloaded waveform envelope), show save / restore, Main / Backup
mirroring UI and the running-order export menu.

Reuses the headless app fixture of test_frontend_smoke (one app per module).
Skipped when no display is available.
"""
import os
import sys
import threading
import time
import types
import tkinter

import numpy as np
import pytest

from tests.test_frontend_smoke import _app, ui, pump  # noqa: F401
from helpers_audio import write_wav, SR


# ----------------------------------------------------------------- helpers --

def mod(name):
    return sys.modules[name]


def app_module(ui):
    return sys.modules[type(ui).__module__]


def make_song(folder, name, secs=2.0, freq=330):
    path = str(folder / name)
    t = np.arange(int(secs * SR)) / SR
    mono = (np.sin(2 * np.pi * freq * t) * 8000).astype(np.int16)
    write_wav(path, np.stack([mono, mono], axis=1))
    return path


def wait_until(ui, cond, seconds=5.0):
    end = time.time() + seconds
    while time.time() < end:
        pump(ui, 0.01)
        if cond():
            return True
    return cond()


def wait_loaded(ui, path):
    assert wait_until(ui, lambda: not ui._loading and ui.app_state.current_song_path == path)
    pump(ui, 0.1)


def invoke(menu, label):
    """Invoke the (enabled) menu entry whose label starts with ``label``."""
    labels = mod("frontend.show_sidebar").menu_labels(menu)
    for i, text in enumerate(labels):
        if text.startswith(label):
            assert menu.entrycget(i, "state") != "disabled", f"{label!r} is disabled"
            menu.invoke(i)
            return
    raise AssertionError(f"no menu entry {label!r} in {labels}")


def entry_state(menu, label):
    labels = mod("frontend.show_sidebar").menu_labels(menu)
    for i, text in enumerate(labels):
        if text.startswith(label):
            return str(menu.entrycget(i, "state"))
    raise AssertionError(f"no menu entry {label!r} in {labels}")


class Ev:
    def __init__(self, y_root=None, x_root=0, data=""):
        self.y_root = y_root
        self.x_root = x_root
        self.data = data


class Spy:
    """Wrap a bound method, recording the call arguments."""
    def __init__(self, monkeypatch, obj, name):
        self.calls = []
        original = getattr(obj, name)

        def wrapper(*args, **kwargs):
            self.calls.append((args, kwargs))
            return original(*args, **kwargs)
        monkeypatch.setattr(obj, name, wrapper)


@pytest.fixture
def show(ui, tmp_path):
    """An explicit show: [ACT 1 divider, Alpha, Bravo, Charlie] (not saved)."""
    st = ui.app_state
    original = st.current_song_path
    folder = tmp_path / "Show Songs"
    folder.mkdir()
    paths = [make_song(folder, f"{name}.wav", freq=f)
             for name, f in (("Alpha", 300), ("Bravo", 400), ("Charlie", 500))]
    st.new_show("Test show")
    ids = st.add_songs(paths)
    div = st.add_divider("Act 1", 0)
    pump(ui)
    yield types.SimpleNamespace(st=st, paths=paths, ids=ids, div=div, folder=folder)
    st.set_live_mode(False)
    st.stop()
    ui.library.cancel_drag()
    st.new_show()
    pump(ui)
    if original and st.current_song_path != original:
        ui._on_song_select(original)
        wait_loaded(ui, original)


# -------------------------------------------------------------- rendering --

def test_sidebar_renders_numbers_badges_cut_segue_missing(ui, show):
    st, lib = show.st, ui.library
    a, b, c = show.ids
    extra = make_song(show.folder, "Delta.wav")
    (d,) = st.add_songs([extra])
    st.set_entry_cut(b, True)
    st.set_entry_segue(a, True)
    pump(ui)
    os.remove(extra)
    st.show.invalidate_missing_cache()
    lib.refresh_from_state()
    pump(ui)
    assert lib.row_state(show.div) == {"kind": "divider", "title": "ACT 1"}
    # Numbers skip the divider and the cut song (like the printed running order)
    assert lib.row_state(a)["number"] == "1"
    assert lib.row_state(b)["number"] == "" and lib.row_state(b)["cut"]
    assert lib.row_state(c)["number"] == "2"
    assert lib.row_state(d)["number"] == "3"
    assert lib.row_state(d)["missing"] and lib.row_state(d)["locate"]
    assert "1 cut" in lib.count_label.cget("text") and "1 missing" in lib.count_label.cget("text")
    assert "Test show" in lib.name_label.cget("text") and "•" in lib.name_label.cget("text")
    # Nothing loaded from the show: the first playable song is next up
    assert st.next_up_entry_id == a
    assert lib.row_state(a)["next"] and not lib.row_state(c)["next"]
    # Load Alpha: it is current, the next playable (Charlie; Bravo is cut) gets NEXT
    lib.on_row_click(a)
    wait_loaded(ui, show.paths[0])
    assert lib.row_state(a)["current"] and not lib.row_state(a)["next"]
    assert lib.row_state(c)["next"] and not lib.row_state(b)["next"]
    assert "Charlie" in st.standby()["label"] or st.standby()["action"] == "play"
    seg = [w for w in lib._rows[a]["row"].winfo_children()
           if isinstance(w, mod("customtkinter").CTkLabel) and "segue" in w.cget("text")]
    assert seg


def test_click_loads_outside_live_and_sets_next_up_in_live(ui, show, monkeypatch):
    st, lib = show.st, ui.library
    a, b, c = show.ids
    # A press + release without movement is a click: rehearsal loads the song
    lib._on_press(Ev(y_root=100), b)
    lib._on_release(Ev(y_root=100))
    wait_loaded(ui, show.paths[1])
    assert st.current_entry_id == b
    # Live Mode: a click picks the next-up song and never loads
    st.set_live_mode(True)
    pump(ui)
    selected = []
    monkeypatch.setattr(ui, "_on_song_select", lambda *a, **k: selected.append(a))
    lib.on_row_click(a)
    pump(ui)
    assert selected == []
    assert st.next_up_override == a and st.next_up_entry_id == a
    assert ui.status_label.cget("text") == "Next up: Alpha — press GO"
    assert lib.row_state(a)["next"] and not lib.row_state(c)["next"]
    # The current song cannot be "next"; a divider click does nothing
    lib.on_row_click(b)
    lib.on_row_click(show.div)
    assert st.next_up_override == a
    # Context menu "Set as next" works in Live Mode too
    menu = lib.build_context_menu(c)
    assert entry_state(menu, "Mark as cut") == "disabled"
    invoke(menu, "Set as next")
    pump(ui)
    assert st.next_up_entry_id == c and lib.row_state(c)["next"]


# ---------------------------------------------------------- context menu --

def test_context_menu_actions_call_state(ui, show, monkeypatch):
    st, lib = show.st, ui.library
    a, b, c = show.ids
    spies = {n: Spy(monkeypatch, st, n) for n in (
        "set_entry_cut", "set_entry_segue", "rename_entry", "duplicate_entry",
        "add_divider", "move_entry", "remove_entry", "relink_entry", "set_next_up")}
    monkeypatch.setattr(lib, "_ask_text", lambda title, text, initial="": "Opening Number"
                        if title == "Rename" else "ACT 2")

    invoke(lib.build_context_menu(b), "Mark as cut")
    assert spies["set_entry_cut"].calls[-1][0] == (b, True)
    pump(ui)
    invoke(lib.build_context_menu(b), "Restore")
    assert spies["set_entry_cut"].calls[-1][0] == (b, False)
    invoke(lib.build_context_menu(a), "Segue into next: turn on")
    assert spies["set_entry_segue"].calls[-1][0] == (a, True)
    invoke(lib.build_context_menu(a), "Rename")
    assert spies["rename_entry"].calls[-1][0] == (a, "Opening Number")
    pump(ui)
    assert lib.row_state(a)["title"] == "Opening Number"
    invoke(lib.build_context_menu(c), "Duplicate")
    assert spies["duplicate_entry"].calls[-1][0] == (c,)
    pump(ui)
    assert len(st.show.entries) == 5
    invoke(lib.build_context_menu(c), "Insert divider above")
    assert spies["add_divider"].calls[-1][0] == ("ACT 2", 3)
    pump(ui)
    invoke(lib.build_context_menu(a), "Move down")
    assert spies["move_entry"].calls[-1][0] == (a, 2)
    pump(ui)
    invoke(lib.build_context_menu(a), "Move up")
    assert spies["move_entry"].calls[-1][0] == (a, 1)
    pump(ui)
    assert entry_state(lib.build_context_menu(show.div), "Move up") == "disabled"
    invoke(lib.build_context_menu(b), "Set as next")
    assert spies["set_next_up"].calls[-1][0] == (b,)
    pump(ui)
    invoke(lib.build_context_menu(b), "Back to running order")
    assert spies["set_next_up"].calls[-1][0] == (None,)
    invoke(lib.build_context_menu(b), "Remove from show")
    assert spies["remove_entry"].calls[-1][0] == (b,)
    pump(ui)
    assert st.show.get(b) is None and lib.row_state(b) == {}

    # A missing file offers Locate file… (also the row's button)
    pump(ui)
    os.rename(show.paths[2], str(show.folder / "moved.wav"))
    try:
        st.show.invalidate_missing_cache()
        lib.refresh_from_state()
        pump(ui)
        new_path = str(show.folder / "moved.wav")
        monkeypatch.setattr(lib, "_ask_open_paths", lambda *a, **k: [new_path])
        invoke(lib.build_context_menu(c), "Locate file")
        assert spies["relink_entry"].calls[-1][0] == (c, new_path)
        pump(ui)
        assert not lib.row_state(c)["missing"]
    finally:
        if os.path.exists(str(show.folder / "moved.wav")):
            os.rename(str(show.folder / "moved.wav"), show.paths[2])


def test_right_click_bindings_per_platform(ui, show):
    seqs = mod("frontend.show_sidebar").context_menu_sequences()
    if sys.platform == "darwin":
        assert set(seqs) == {"<Button-2>", "<Button-3>", "<Control-Button-1>"}
    else:
        assert seqs == ("<Button-3>",)
    row = ui.library._rows[show.ids[0]]["row"]
    bound = " ".join(row._canvas.bind())
    for seq in seqs:
        assert seq.strip("<>").replace("Button-", "Button-") in bound


def test_edits_blocked_in_live_mode(ui, show, monkeypatch):
    st, lib = show.st, ui.library
    a, b, c = show.ids
    st.set_live_mode(True)
    pump(ui)
    before = [e.id for e in st.show.entries]
    menu = lib.build_context_menu(a)
    for label in ("Rename", "Duplicate", "Mark as cut", "Segue", "Insert divider",
                  "Move up", "Move down", "Remove from show"):
        assert entry_state(menu, label) == "disabled", label
    show_menu = lib.build_show_menu()
    for label in ("New Show", "Open Show", "Open Folder", "Recent", "Add songs"):
        assert entry_state(show_menu, label) == "disabled", label
    for label in ("Save", "Save As", "Print running order"):
        assert entry_state(show_menu, label) == "normal", label
    assert lib.toggle_cut(a) is False
    assert lib.move_down(a) is False
    assert lib.remove_entry(a) is False
    assert lib.add_paths(show.paths) == []
    pump(ui)
    assert "Live Mode" in ui.status_label.cget("text")
    assert "show order" in ui.status_label.cget("text") or "locked" in ui.status_label.cget("text")
    # Backend blocks too (direct calls)
    assert st.move_entry(a, 3) is False
    assert [e.id for e in st.show.entries] == before
    assert not any(e.cut for e in st.show.entries)
    assert str(lib.btn_add.cget("state")) == "disabled"


# -------------------------------------------------------------- dragging --

def test_drag_reorder_calls_move_entry_with_final_index(ui, show, monkeypatch):
    st, lib = show.st, ui.library
    a, b, c = show.ids                 # order: [div, a, b, c]
    monkeypatch.setattr(lib, "_row_bounds", lambda: [(0, 30), (30, 70), (70, 110), (110, 150)])
    moves = Spy(monkeypatch, st, "move_entry")
    # Drag Alpha (index 1) below Charlie: insert before 4 -> final index 3
    lib._on_press(Ev(y_root=50), a)
    lib._on_motion(Ev(y_root=52))      # under the threshold: still a click
    assert not lib.dragging
    lib._on_motion(Ev(y_root=145))
    assert lib.dragging and lib._drag["index"] == 4
    lib._on_release(Ev(y_root=145))
    assert moves.calls[-1][0] == (a, 3)
    pump(ui)
    assert [e.id for e in st.show.entries] == [show.div, b, c, a]
    # Drag Charlie (index 2) above the divider: insert before 0 -> index 0
    lib._on_press(Ev(y_root=90), c)
    lib._on_motion(Ev(y_root=5))
    lib._on_release(Ev(y_root=5))
    assert moves.calls[-1][0] == (c, 0)
    pump(ui)
    assert [e.id for e in st.show.entries] == [c, show.div, b, a]
    # Dropping onto its own place does nothing
    n = len(moves.calls)
    lib._on_press(Ev(y_root=10), c)
    lib._on_motion(Ev(y_root=25))
    lib._on_release(Ev(y_root=25))
    assert len(moves.calls) == n
    # Esc (the stop key) cancels a drag and does not stop playback
    stops = Spy(monkeypatch, st, "stop")
    lib._on_press(Ev(y_root=10), c)
    lib._on_motion(Ev(y_root=140))
    ui._on_stop()
    assert stops.calls == [] and not lib.dragging
    lib._on_release(Ev(y_root=140))
    assert len(moves.calls) == n
    assert ui.status_label.cget("text") == "Move cancelled"
    ui._on_stop()
    assert len(stops.calls) == 1


def test_drag_is_locked_in_live_mode(ui, show, monkeypatch):
    st, lib = show.st, ui.library
    a = show.ids[0]
    st.set_live_mode(True)
    pump(ui)
    monkeypatch.setattr(lib, "_row_bounds", lambda: [(0, 30), (30, 70), (70, 110), (110, 150)])
    moves = Spy(monkeypatch, st, "move_entry")
    lib._on_press(Ev(y_root=50), a)
    lib._on_motion(Ev(y_root=145))
    lib._on_release(Ev(y_root=145))
    pump(ui)
    assert moves.calls == []
    assert st.next_up_override is None        # the aborted drag is not a click either
    assert "show order" in ui.status_label.cget("text")


# ----------------------------------------------------------- add / drop --

def test_dnd_drop_parses_tcl_list_with_spaces_and_braces(ui, show, monkeypatch, tmp_path):
    st, lib = show.st, ui.library
    drop = tmp_path / "Drop Zone"
    drop.mkdir()
    spaced = make_song(drop, "My Song (live).wav")
    braced = make_song(drop, "odd {name}.wav")
    sub = drop / "Act Two Folder"
    sub.mkdir()
    in_sub_b = make_song(sub, "b second.wav")
    in_sub_a = make_song(sub, "A first.wav")
    (drop / "readme.txt").write_text("x", encoding="utf-8")
    data = tkinter._join([spaced, braced, str(sub), str(drop / "readme.txt")])
    assert "{" in data
    paths = lib.parse_drop_data(data)
    assert paths == [spaced, braced, str(sub), str(drop / "readme.txt")]

    adds = Spy(monkeypatch, st, "add_songs")
    lib._selected_id = show.ids[0]            # insert after Alpha
    assert lib._on_dnd_drop(Ev(data=data)) == "copy"
    files, index = adds.calls[-1][0]
    assert files == [spaced, braced, in_sub_a, in_sub_b]   # folder: sorted audio only
    assert index == 2
    pump(ui)
    titles = [e.title for e in st.show.entries]
    assert titles[:6] == ["Act 1", "Alpha", "My Song (live)", "odd {name}", "A first", "b second"]
    # Live Mode refuses drops
    st.set_live_mode(True)
    pump(ui)
    n = len(st.show.entries)
    assert lib._on_dnd_drop(Ev(data=data)) == "refuse_drop"
    assert len(st.show.entries) == n


def test_add_songs_button_inserts_after_selection(ui, show, monkeypatch, tmp_path):
    st, lib = show.st, ui.library
    new = make_song(tmp_path, "Encore.wav")
    asked = []
    monkeypatch.setattr(lib, "_ask_open_paths",
                        lambda title, filetypes, multiple=False, initialdir=None:
                        asked.append((filetypes, multiple)) or [new])
    lib._selected_id = show.ids[1]
    ids = lib.add_songs_dialog()
    assert asked and asked[0][1] is True
    assert any("*.wav" in pattern for _, pattern in asked[0][0])
    pump(ui)
    assert [e.id for e in st.show.entries].index(ids[0]) == 3


def test_dnd_is_optional(ui):
    # The app runs with or without tkdnd; when it loaded, the sidebar is a drop target
    assert isinstance(ui._dnd_available, bool)
    assert ui.library._dnd_enabled == ui._dnd_available


# ------------------------------------------------------------------- GO --

def test_go_at_song_end_loads_next_entry_once_and_plays(ui, show, monkeypatch):
    st, lib = show.st, ui.library
    a, b, c = show.ids
    lib.on_row_click(a)
    wait_loaded(ui, show.paths[0])
    st.set_entry_cut(b, True)                  # GO skips the cut song
    pump(ui)
    selects = Spy(monkeypatch, ui, "_on_song_select")
    loads = Spy(monkeypatch, st, "load_song")
    st.set_live_mode(True)                     # GO advances even in Live Mode
    st.play()
    st.audio.advance(5.0)                      # runs off the end
    pump(ui)
    assert st.standby()["action"] == "next_song"
    assert st.standby()["label"] == "GO: next song - Charlie"
    assert lib.row_state(c)["next"]
    st._last_go_time = -1e9
    assert st.go() is True
    wait_loaded(ui, show.paths[2])
    pump(ui, 0.2)
    assert len(selects.calls) == 1             # go_next_song ignored, go_next_entry used
    assert selects.calls[0][1].get("entry_id") == c
    assert len(loads.calls) == 1
    assert st.state.name == "PLAYING"
    assert st.current_entry_id == c and lib.row_state(c)["current"]
    st.audio.advance(5.0)                      # last song ends
    pump(ui)
    assert st.standby()["action"] == "none"
    assert st.standby()["label"].startswith("End of show")


def test_go_next_song_still_works_for_an_empty_show(ui, show, monkeypatch):
    st = show.st
    st.new_show()
    pump(ui)
    selects = []
    monkeypatch.setattr(ui, "_on_song_select", lambda *a, **k: selects.append((a, k)))
    ui._on_go_next_song(show.paths[0])
    assert selects == [((show.paths[0],), {"from_go": True})]
    assert st.next_song_provider is None


# ---------------------------------------------------------- instant GO --

def test_preloaded_envelope_is_used_on_load(ui, show, monkeypatch):
    st, lib = show.st, ui.library
    a, b, c = show.ids
    lib.on_row_click(a)
    wait_loaded(ui, show.paths[0])
    # The next-up song (Bravo) was preloaded -> its envelope is computed off the Tk thread
    assert wait_until(ui, lambda: ui._has_cached_envelope(show.paths[1]))
    calls = []
    real = app_module(ui).compute_envelope

    def counting(*a, **k):
        # Preload envelopes (of the NEXT next-up song) run on their own worker
        if threading.current_thread().name != "preload-envelope":
            calls.append(1)
        return real(*a, **k)
    monkeypatch.setattr(app_module(ui), "compute_envelope", counting)
    ui._on_song_select(show.paths[1], entry_id=b)
    assert ui._loading
    assert not ui.loader.winfo_manager()        # no overlay flash for a preloaded song
    wait_loaded(ui, show.paths[1])
    assert calls == []
    assert not ui.loader.winfo_manager()
    assert ui._overlay_after is None
    # Not preloaded -> computed as before (and the overlay shows)
    ui._on_song_select(show.paths[0], entry_id=a)
    assert ui.loader.winfo_manager() == "place"
    wait_loaded(ui, show.paths[0])
    assert calls == [1]
    # The cache keeps at most two songs
    for i in range(3):
        key = ui._envelope_key(f"/x/{i}.wav")
        with ui._envelope_lock:
            ui._envelope_cache[key] = (1, np.zeros(1, dtype=np.float32))
            while len(ui._envelope_cache) > app_module(ui).ENVELOPE_CACHE_SIZE:
                ui._envelope_cache.popitem(last=False)
    assert len(ui._envelope_cache) == 2


def test_cached_envelope_ignored_when_length_differs(ui):
    key_path = "/nowhere/song.wav"
    with ui._envelope_lock:
        ui._envelope_cache[ui._envelope_key(key_path)] = (10, np.ones(5, dtype=np.float32))
    try:
        assert ui._cached_envelope(key_path, np.zeros((10, 2), dtype=np.int16)) is not None
        assert ui._cached_envelope(key_path, np.zeros((11, 2), dtype=np.int16)) is None
        assert ui._cached_envelope(key_path, None) is None
    finally:
        with ui._envelope_lock:
            ui._envelope_cache.pop(ui._envelope_key(key_path), None)


# ------------------------------------------------------------ save / close --

def test_save_on_unsaved_show_asks_for_a_file_name(ui, show, monkeypatch, tmp_path):
    st, lib = show.st, ui.library
    asked = []
    target = str(tmp_path / "Spring Musical")
    monkeypatch.setattr(lib, "_ask_save_path",
                        lambda title, filetypes, ext, initialfile, initialdir=None:
                        asked.append((filetypes, ext, initialfile)) or target)
    invoke(lib.build_show_menu(), "Save")
    assert asked and asked[0][1] == ".loopshow" and asked[0][2] == "Test show.loopshow"
    assert os.path.isfile(target + ".loopshow")
    pump(ui)
    snap = st.show_snapshot()
    assert snap["file_path"] == os.path.abspath(target + ".loopshow") and not snap["dirty"]
    assert "•" not in lib.name_label.cget("text")
    assert "Spring Musical.loopshow" in lib.folder_label.cget("text")
    # Now it has a file: Save writes without asking
    st.rename_entry(show.ids[0], "Overture")
    pump(ui)
    assert "•" in lib.name_label.cget("text")
    asked.clear()
    assert lib.save_show() is True and asked == []
    # Save is allowed in Live Mode
    st.set_live_mode(True)
    st.rename_entry(show.ids[0], "blocked")      # (blocked: still "Overture")
    assert lib.save_show() is True


def test_close_with_dirty_show_asks_save_dont_save_cancel(ui, show, monkeypatch, tmp_path):
    st, lib = show.st, ui.library
    closed, answers, asked = [], [], []
    monkeypatch.setattr(ui, "_on_close", lambda: closed.append(1))
    monkeypatch.setattr(lib, "_ask_yes_no_cancel",
                        lambda title, msg: asked.append(msg) or answers.pop(0))
    monkeypatch.setattr(lib, "_ask_save_path", lambda *a, **k: str(tmp_path / "close.loopshow"))
    answers[:] = [None]                          # Cancel: stays open
    ui._on_close_request()
    assert closed == [] and len(asked) == 1 and "Test show" in asked[0]
    answers[:] = [False]                         # Don't save
    ui._on_close_request()
    assert closed == [1] and not os.path.exists(str(tmp_path / "close.loopshow"))
    answers[:] = [True]                          # Save (Save As for a new show)
    ui._on_close_request()
    assert closed == [1, 1] and os.path.isfile(str(tmp_path / "close.loopshow"))
    # Saved / clean: no question at all
    asked.clear()
    ui._on_close_request()
    assert asked == [] and closed == [1, 1, 1]
    # A folder show autosaves: no question either
    st.open_folder(str(show.folder))
    st.move_entry(st.show.entries[0].id, 2)
    ui._on_close_request()
    assert asked == []


# --------------------------------------------------------- restore last --

def test_last_show_restore_and_old_pref_migration(ui, show, monkeypatch, tmp_path):
    st, lib = show.st, ui.library
    prefs = mod("utils.preferences")
    selected = []
    monkeypatch.setattr(ui, "_on_song_select", lambda *a, **k: selected.append(a))
    show_file = str(tmp_path / "Restore.loopshow")
    assert st.save_show(show_file)
    st.new_show()
    prefs.set_last_show("file", show_file)
    prefs.save_preferences({ui.PREF_LAST_SONG: show.paths[1]})
    ui._restore_session()
    pump(ui, 0.4)
    assert st.show_snapshot()["file_path"] == os.path.abspath(show_file)
    assert len(lib.song_buttons) == 4 and "Restore.loopshow" in lib.folder_label.cget("text")
    assert selected == [(show.paths[1],)]
    # Old preferences: only the library folder -> opened as a folder show
    st.new_show()
    prefs.save_preferences({"show": {"last_open": None},
                            ui.PREF_LIBRARY_FOLDER: str(show.folder),
                            ui.PREF_LAST_SONG: None})
    ui._restore_session()
    pump(ui)
    assert os.path.samefile(lib.current_folder, str(show.folder))
    assert sorted(lib.songs) == ["Alpha.wav", "Bravo.wav", "Charlie.wav"]
    last = prefs.get_last_show()
    assert last["kind"] == "folder" and os.path.samefile(last["path"], str(show.folder))
    # A last show that vanished leaves the app empty but working
    st.new_show()
    prefs.set_last_show("file", str(tmp_path / "gone.loopshow"))
    ui._restore_session()
    pump(ui)
    assert st.show_snapshot()["entries"] == []
    prefs.save_preferences({ui.PREF_LIBRARY_FOLDER: None})


def test_open_show_and_recent_menu(ui, show, monkeypatch, tmp_path):
    st, lib = show.st, ui.library
    prefs = mod("utils.preferences")
    show_file = str(tmp_path / "Recent One.loopshow")
    assert st.save_show(show_file)
    st.new_show()
    pump(ui)
    monkeypatch.setattr(lib, "_ask_open_paths", lambda *a, **k: [show_file])
    invoke(lib.build_show_menu(), "Open Show")
    pump(ui)
    assert st.show_snapshot()["file_path"] == os.path.abspath(show_file)
    assert prefs.get_recent_shows()[0]["path"] == os.path.abspath(show_file)
    recent = lib.build_show_menu()
    idx = mod("frontend.show_sidebar").menu_labels(recent).index("Recent")
    sub = recent.nametowidget(recent.entrycget(idx, "menu"))
    assert any("Recent One" in label for label in mod("frontend.show_sidebar").menu_labels(sub))
    # New Show (asks for a name; the clean show needs no save question)
    monkeypatch.setattr(lib, "_ask_text", lambda *a, **k: "Fall Revue")
    monkeypatch.setattr(lib, "_ask_yes_no_cancel", lambda *a: pytest.fail("asked to save"))
    invoke(lib.build_show_menu(), "New Show")
    pump(ui)
    assert st.show_snapshot()["name"] == "Fall Revue" and st.show_snapshot()["entries"] == []


# --------------------------------------------------------- running order --

def test_print_running_order_menu_uses_exporter(ui, show, monkeypatch, tmp_path):
    lib = ui.library
    calls = []

    def export(state, path, fmt="html"):
        calls.append(("export", path, fmt, threading.current_thread() is threading.main_thread()))
        with open(path, "w", encoding="utf-8") as f:
            f.write("x")
        return path
    fake = types.SimpleNamespace(export_running_order=export,
                                 open_in_browser=lambda path: calls.append(("open", path)))
    monkeypatch.setattr(lib, "_running_order_module", lambda: fake)
    target = str(tmp_path / "order.html")
    monkeypatch.setattr(lib, "_ask_save_path", lambda *a, **k: target)
    invoke(lib.build_show_menu(), "Print running order")
    assert wait_until(ui, lambda: "browser" in ui.status_label.cget("text"))
    assert calls == [("export", target, "html", False), ("open", target)]
    # CSV: saved, not opened
    calls.clear()
    target = str(tmp_path / "order.csv")
    invoke(lib.build_show_menu(), "Print running order")
    assert wait_until(ui, lambda: "saved" in ui.status_label.cget("text"))
    assert calls == [("export", target, "csv", False)]
    # Exporter failure is reported
    fake.export_running_order = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    invoke(lib.build_show_menu(), "Print running order")
    assert wait_until(ui, lambda: "disk full" in ui.status_label.cget("text"))
    # Not built in: "not available"
    monkeypatch.setattr(lib, "_running_order_module", lambda: None)
    invoke(lib.build_show_menu(), "Print running order")
    assert "not available" in ui.status_label.cget("text")


# ------------------------------------------------------------- mirroring --

class FakeMirror:
    def __init__(self):
        self.calls = []
        self.on_status = None
        self._status = {"role": None, "connected": False, "peer": None, "following": False,
                        "drift_ms": None, "last_seen_s": None, "message": ""}

    def start(self, role, peer_host=None, port=53100, pin=""):
        self.calls.append(("start", role, peer_host, port, pin))
        self._status.update(role=role, connected=False, following=role == "backup",
                            peer=peer_host)

    def stop(self):
        self.calls.append(("stop",))
        self._status.update(role=None, connected=False, following=False)

    def take_over(self):
        self.calls.append(("take_over",))
        self._status.update(following=False)

    def status(self):
        return dict(self._status)


@pytest.fixture
def fake_mirror(ui, monkeypatch):
    mirror = FakeMirror()
    monkeypatch.setattr(ui.app_state, "mirror", mirror, raising=False)
    yield mirror
    if ui._settings_dialog is not None:
        ui._settings_dialog.close()
    ui._on_mirror_status({"role": None})
    pump(ui)


def test_mirror_settings_start_backup_and_lock_in_live(ui, fake_mirror):
    st = ui.app_state
    ui._open_settings_dialog()
    pump(ui, 0.2)
    dlg = ui._settings_dialog
    assert dlg.mirror is fake_mirror
    dlg.mirror_role.set("Main")
    dlg._mirror_role_changed()
    assert "IP" in dlg.lbl_mirror_ips.cget("text")
    dlg.mirror_role.set("Backup")
    dlg._mirror_role_changed()
    assert dlg._mirror_start() is False                     # no main IP yet
    dlg.mirror_host.insert(0, "10.0.0.5")
    dlg.mirror_pin.insert(0, "1234")
    assert dlg._mirror_start() is True
    assert fake_mirror.calls[-1] == ("start", "backup", "10.0.0.5", 53100, "1234")
    pump(ui)
    assert "BACKUP" in ui.lbl_mirror.cget("text")
    # Live Mode locks the section (TAKE OVER stays allowed, see below)
    st.set_live_mode(True)
    pump(ui)
    n = len(fake_mirror.calls)
    assert dlg._mirror_start() is False and dlg._mirror_stop() is False
    assert len(fake_mirror.calls) == n
    assert str(dlg.btn_mirror_start.cget("state")) == "disabled"
    st.set_live_mode(False)
    pump(ui)
    assert str(dlg.btn_mirror_start.cget("state")) == "normal"
    assert dlg._mirror_stop() is True and fake_mirror.calls[-1] == ("stop",)
    saved = mod("utils.preferences").load_preferences().get("mirror", {})
    assert saved.get("role") == "backup" and saved.get("peer_host") == "10.0.0.5"
    assert saved.get("port") == 53100 and saved.get("auto_start") is False
    # start() refusing (port busy) shows the link's message
    fake_mirror.start = lambda *a, **k: fake_mirror._status.update(
        role="off", message="Could not listen on port 53100: busy") or False
    dlg.mirror_role.set("Main")
    dlg._mirror_role_changed()
    assert dlg._mirror_start() is False
    assert "Could not listen" in dlg.lbl_mirror.cget("text")
    mod("utils.preferences").save_preferences({"mirror": {"role": "off", "auto_start": False}})


def test_mirror_header_badge_and_take_over(ui, fake_mirror, monkeypatch):
    st = ui.app_state
    assert ui._attach_mirror() is True
    # Status arrives on a worker thread -> routed through msg_queue
    t = threading.Thread(target=fake_mirror.on_status,
                         args=({"role": "main", "connected": True, "peer": ["10.0.0.9"]},))
    t.start()
    t.join()
    pump(ui)
    assert "MAIN · 1 backup" in ui.lbl_mirror.cget("text")
    assert not ui.btn_take_over.winfo_manager()
    ui._on_mirror_status({"role": "backup", "connected": True, "following": True})
    assert "BACKUP · following" in ui.lbl_mirror.cget("text")
    assert ui.btn_take_over.winfo_manager() == "pack"
    assert ui._mirror_flash_after is None
    ui._on_mirror_status({"role": "backup", "connected": False, "following": True})
    assert "MAIN LOST" in ui.lbl_mirror.cget("text")
    assert ui._mirror_flash_after is not None              # flashing red
    assert ui.btn_take_over.winfo_manager() == "pack"
    # TAKE OVER is allowed in Live Mode; main lost -> no question
    fake_mirror._status.update(role="backup", connected=False, following=True)
    st.set_live_mode(True)
    asked = []
    monkeypatch.setattr(app_module(ui).messagebox, "askyesno", lambda *a, **k: asked.append(1) or False)
    assert ui._on_take_over() is True
    assert fake_mirror.calls[-1] == ("take_over",) and asked == []
    assert not ui.btn_take_over.winfo_manager() and ui._mirror_flash_after is None
    # Main still connected: asks first (default No)
    ui._on_mirror_status({"role": "backup", "connected": True, "following": True})
    n = len(fake_mirror.calls)
    assert ui._on_take_over() is False and asked == [1] and len(fake_mirror.calls) == n
    st.set_live_mode(False)
    ui._on_mirror_status({"role": None})
    assert not ui.lbl_mirror.winfo_manager()


def test_backup_following_locks_transport_until_take_over(ui, fake_mirror, monkeypatch):
    st = ui.app_state
    fake_mirror.is_following = lambda: fake_mirror._status.get("following") and \
        fake_mirror._status.get("connected")
    ui._attach_mirror()
    fake_mirror._status.update(role="backup", connected=True, following=True)
    ui._on_mirror_status(fake_mirror.status())
    assert str(ui.btn_play.cget("state")) == "disabled"
    assert str(ui.go_panel.btn_go.cget("state")) == "disabled"
    gos = Spy(monkeypatch, st, "go")
    ui._on_go("key")
    ui._on_play_pause_toggle()
    ui._on_midi_action("go")
    assert gos.calls == [] and not st.is_playing()
    assert "TAKE OVER" in ui.status_label.cget("text")
    # The backup loaded a song because the main did: waveform is rebuilt
    ready = []
    monkeypatch.setitem(ui._msg_handlers, "waveform_ready", lambda p, env: ready.append(p))
    fake_mirror.on_loaded(st.current_song_path)
    assert wait_until(ui, lambda: ready == [st.current_song_path])
    # After TAKE OVER the transport is back
    monkeypatch.setattr(app_module(ui).messagebox, "askyesno", lambda *a, **k: True)
    assert ui._on_take_over() is True
    assert str(ui.btn_play.cget("state")) == "normal"
    ui._on_go("ui")
    assert len(gos.calls) == 1
    st.stop()


def test_settings_without_mirror_says_not_available(ui, monkeypatch):
    monkeypatch.setattr(ui.app_state, "mirror", None, raising=False)
    ui._open_settings_dialog()
    pump(ui, 0.2)
    dlg = ui._settings_dialog
    try:
        assert dlg.mirror is None
        assert ui._attach_mirror() is False
    finally:
        dlg.close()
