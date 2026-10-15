"""
Regression tests for the frontend review fixes (Song Settings bound to its
song, Live Mode bypasses, vamp modal IN/OUT, cue sheet in-place updates,
position throttle, show keys in dialogs, close confirmation, library...).

Reuses the headless app fixture of test_frontend_smoke (one app per module).
Skipped when no display is available.
"""
import os
import sys
import time

import numpy as np
import pytest

from tests.test_frontend_smoke import _app, ui, pump, send_key, _Key  # noqa: F401
from helpers_audio import write_wav, SR


def mod(name):
    """A frontend module as currently loaded (the fixture re-imports them)."""
    return sys.modules[name]


def app_module(ui):
    return sys.modules[type(ui).__module__]


def make_song(tmp_path, name, secs=8.0, freq=330):
    path = str(tmp_path / name)
    t = np.arange(int(secs * SR)) / SR
    mono = (np.sin(2 * np.pi * freq * t) * 8000).astype(np.int16)
    write_wav(path, np.stack([mono, mono], axis=1))
    return path


def load(ui, path):
    ui._on_song_select(path)
    for _ in range(500):
        pump(ui, 0.01)
        if not ui._loading:
            break
    pump(ui, 0.2)
    assert ui.app_state.current_song_path == path


@pytest.fixture
def other_song(ui, tmp_path):
    """Load a second song for the test, then go back to the original one."""
    original = ui.app_state.current_song_path
    path = make_song(tmp_path, "Other Song.wav")
    yield path
    st = ui.app_state
    st.set_live_mode(False)
    st.stop()
    if st.current_song_path != original:
        load(ui, original)


def vamp_modal(ui):
    return ui._vamp_modal


# ---------------------------------------------------------------- finding 1

def test_song_settings_closes_on_song_change_and_never_saves_onto_it(ui, other_song):
    st = ui.app_state
    ui._open_song_dialog()
    pump(ui, 0.3)
    dlg = ui._song_dialog
    dlg._set_entry(dlg.entry_bpm, "95")
    load(ui, other_song)
    assert ui._song_dialog is None and dlg._closed
    assert st.settings.bpm == 0.0
    # A dialog that is (somehow) still bound to the old song refuses to save
    ui._open_song_dialog()
    pump(ui, 0.3)
    dlg = ui._song_dialog
    dlg._song_path = "/elsewhere/previous.wav"
    dlg._set_entry(dlg.entry_bpm, "95")
    dlg._save_tempo_map()
    dlg._save_osc()
    assert st.settings.bpm == 0.0
    assert "Another song" in dlg.lbl_map.cget("text")
    dlg.close()
    pump(ui)


# ---------------------------------------------------------------- finding 2

@pytest.mark.parametrize("field,value", [("offset", "nan"), ("offset", "inf"),
                                         ("bpm", "nan"), ("bpm", "-inf")])
def test_song_settings_rejects_non_finite_tempo_map(ui, field, value):
    st = ui.app_state
    ui._open_song_dialog()
    pump(ui, 0.3)
    dlg = ui._song_dialog
    dlg._set_entry(dlg.entry_bpm, "120")
    dlg._set_entry(dlg.entry_offset, "0.5")
    dlg._set_entry(dlg.entry_bpm if field == "bpm" else dlg.entry_offset, value)
    dlg._save_tempo_map()
    assert st.settings.bpm == 0.0 and st.settings.downbeat_offset == 0.0
    assert "BPM 0-400" in dlg.lbl_map.cget("text")
    dlg.close()
    pump(ui)


def test_bar_beat_failure_does_not_stop_position_update(ui, monkeypatch):
    st = ui.app_state
    st.set_song_tempo_map(120.0, 4, 0.0)
    pump(ui)
    assert ui._bar_label_visible

    def boom(_pos):
        raise OverflowError("cannot convert float infinity to integer")
    monkeypatch.setattr(st, "bar_beat_at", boom)
    ui._last_ui_update = -1e9
    ui._update_position(3.0, False)
    assert ui.cue_sheet._current_position == 3.0
    assert ui.notes_sidebar._current_position == 3.0
    assert ui.bar_label.cget("text") == ""


# ---------------------------------------------------------------- finding 3

def test_vamp_save_applies_typed_in_out_without_return(ui):
    st = ui.app_state
    st.add_loop(2.0, 4.0, name="V1")
    pump(ui, 0.2)
    ui._open_vamp_settings(0)
    pump(ui, 0.3)
    m = vamp_modal(ui)
    m.entry_in.delete(0, "end")
    m.entry_in.insert(0, "1.000")
    m.entry_out.delete(0, "end")
    m.entry_out.insert(0, "5.000")
    m._on_save_click()
    pump(ui, 0.2)
    assert (st.loops[0].start, st.loops[0].end) == (1.0, 5.0)
    assert ui._vamp_modal is None
    st.select_loop(0)
    st.delete_selected_loop()


@pytest.mark.parametrize("in_text,out_text,message", [
    ("abc", "4", "numbers"),
    ("4", "2", "before OUT"),
    ("1", "999", "past the end"),
    ("2", "2.1", "at least"),
    ("nan", "4", "numbers"),
])
def test_vamp_invalid_in_out_shows_message_and_keeps_modal_open(ui, in_text, out_text, message):
    st = ui.app_state
    st.add_loop(2.0, 4.0, name="V1")
    pump(ui, 0.2)
    ui._open_vamp_settings(0)
    pump(ui, 0.3)
    m = vamp_modal(ui)
    m.entry_in.delete(0, "end")
    m.entry_in.insert(0, in_text)
    m.entry_out.delete(0, "end")
    m.entry_out.insert(0, out_text)
    assert m._on_manual_change() is False          # Return / FocusOut path
    assert message in m.lbl_range_error.cget("text")
    assert m.entry_in.get() == in_text              # not silently reverted
    m._on_save_click()
    pump(ui, 0.1)
    assert ui._vamp_modal is m and not m._closed    # Save refused
    assert (st.loops[0].start, st.loops[0].end) == (2.0, 4.0)
    m._on_cancel_click()
    pump(ui, 0.1)
    st.select_loop(0)
    st.delete_selected_loop()


# ---------------------------------------------------------------- finding 4

def test_live_mode_blocks_goto_bar_and_jumps(ui):
    st = ui.app_state
    st.set_song_tempo_map(120.0, 4, 0.0)
    hold = st.add_hold(6.0)
    st.add_loop(7.0, 9.0, name="V")
    st.seek(1.0)
    pump(ui)
    st.set_live_mode(True)
    pump(ui)
    gp = ui.go_panel
    assert gp.entry_bar.cget("state") == "disabled"
    assert gp.btn_goto.cget("state") == "disabled"
    gp.entry_bar.configure(state="normal")
    gp.entry_bar.insert(0, "3")
    gp._on_goto_click()                              # Return in the entry
    ui._on_goto_bar(3)                               # and the callback itself
    ui._on_jump_hold(hold.id)
    ui._on_jump_to_vamp(0)
    pump(ui)
    assert st.get_position() == pytest.approx(1.0)
    assert not st.is_playing()
    st.set_live_mode(False)
    pump(ui)
    assert gp.entry_bar.cget("state") == "normal"
    ui._on_goto_bar(3)
    assert st.get_position() == pytest.approx(4.0)
    gp.entry_bar.delete(0, "end")
    st.select_loop(0)
    st.delete_selected_loop()


def test_settings_dialog_locks_controls_in_live_mode(ui):
    st = ui.app_state
    ui._open_settings_dialog()
    pump(ui, 0.3)
    dlg = ui._settings_dialog
    dlg._start_capture("go")                         # capture in progress...
    st.set_live_mode(True)
    pump(ui)
    assert dlg._capture_action is None               # ...is cancelled
    for row in dlg._key_rows.values():
        assert row["button"].cget("state") == "disabled"
    assert dlg.btn_osc_save.cget("state") == "disabled"
    assert dlg.btn_osc_test.cget("state") == "disabled"
    if dlg.midi is not None:
        assert dlg.port_menu.cget("state") == "disabled"
        for row in dlg._midi_rows.values():
            assert row["learn"].cget("state") == "disabled"
        dlg._learn_midi("go")
        assert not dlg.midi.learning
    dlg._start_capture("go")
    send_key(ui, dlg, "x")
    assert ui._key_map["go"] == ["Return", "KP_Enter"]
    assert dlg._osc_save() is False
    st.set_live_mode(False)
    pump(ui)
    assert dlg.btn_osc_save.cget("state") == "normal"
    dlg.close()
    pump(ui)


def test_vamp_settings_not_opened_in_live_and_closed_when_live_starts(ui):
    st = ui.app_state
    st.add_loop(2.0, 4.0, name="V1")
    pump(ui, 0.2)
    original_xfade = st.loops[0].crossfade_ms
    st.set_live_mode(True)
    pump(ui)
    ui._open_vamp_settings(0)
    pump(ui, 0.2)
    assert ui._vamp_modal is None
    st.set_live_mode(False)
    pump(ui)

    # MIDI toggles Live on while the editor is open: the editor is cancelled
    # (restoring the vamp) before Live locks vamp edits.
    ui._open_vamp_settings(0)
    pump(ui, 0.3)
    m = vamp_modal(ui)
    assert m._apply(crossfade_ms=original_xfade + 100)
    ui.msg_queue.put(("midi_action", ("toggle_live",)))
    pump(ui, 0.3)
    assert st.live_mode and m._closed and ui._vamp_modal is None
    assert st.loops[0].crossfade_ms == original_xfade

    # Live turned on some other way: the editor closes without errors
    st.set_live_mode(False)
    pump(ui)
    ui._open_vamp_settings(0)
    pump(ui, 0.3)
    m = vamp_modal(ui)
    st.set_live_mode(True)
    pump(ui, 0.2)
    assert m._closed and ui._vamp_modal is None
    st.set_live_mode(False)
    pump(ui)
    st.select_loop(0)
    st.delete_selected_loop()


# ---------------------------------------------------------------- finding 5

def test_refused_waveform_click_does_not_move_playhead(ui):
    st = ui.app_state
    st.seek(1.0)
    pump(ui, 0.2)
    st.set_live_mode(True)
    pump(ui)
    wf = ui.waveform

    class Ev:
        inaxes = wf.ax
        xdata = 0.8
    wf._on_click(Ev())
    pump(ui, 0.2)
    assert st.get_position() == pytest.approx(1.0)
    assert wf._playhead_frac * st.song_length == pytest.approx(1.0, abs=0.01)
    st.set_live_mode(False)
    pump(ui)
    wf._on_click(Ev())
    pump(ui, 0.2)
    assert wf._playhead_frac == pytest.approx(0.8, abs=0.01)


# ---------------------------------------------------------------- finding 6

def test_song_load_clears_selection_and_detector(ui, other_song):
    wf = ui.waveform
    ui._toggle_selection_mode()

    class Ev:
        inaxes = wf.ax
        xdata = 0.25
    wf._on_click(Ev())
    Ev.xdata = 0.5
    wf._on_drag(Ev())
    wf._on_release(Ev())
    assert wf.get_selection() is not None and ui.sel_end > 0

    class Cand:
        start, end, duration, confidence = 3.0, 6.0, 3.0, 90
    ui.detector.show_results([Cand()])
    assert ui.detector._result_buttons
    load(ui, other_song)
    assert wf.get_selection() is None
    assert (ui.sel_start, ui.sel_end) == (0.0, 0.0)
    assert not wf.selection_mode_active
    assert not ui.detector._result_buttons
    assert ui.detector.btn_find.cget("state") == "disabled"


# ---------------------------------------------------------------- finding 7

def test_cue_sheet_updates_rows_in_place(ui):
    st = ui.app_state
    cs = ui.cue_sheet
    for i in range(1, 9):
        st.add_marker(i * 1.0, f"Cue {i}")
    st.add_hold(4.5)
    st.add_loop(5.2, 7.0, name="V")
    pump(ui, 0.3)
    keys_before = list(cs._row_order)
    rows_before = {k: cs._rows_by_key[k][0] for k in keys_before}
    assert len(keys_before) == 10

    # Insert one cue in the middle: only one new row, order still by time
    st.add_marker(2.5, "Inserted")
    pump(ui, 0.3)
    assert len(cs._row_order) == 11
    reused = [k for k in keys_before if cs._rows_by_key[k][0] is rows_before[k]]
    assert len(reused) >= 9
    packed = [w for w in cs.item_list.pack_slaves()]
    assert packed == cs._item_widgets
    times = [m[1].time if m[0] != 'vamp' else m[1].start for m in cs._item_metadata]
    assert times == sorted(times)

    # Selection only changes the vamp row
    rows_now = {k: cs._rows_by_key[k][0] for k in cs._row_order}
    st.select_loop(-1) if hasattr(st, "select_loop") else None
    st.select_loop(0)
    pump(ui, 0.3)
    changed = [k for k in cs._row_order if cs._rows_by_key[k][0] is not rows_now.get(k)]
    assert all(k[0] == 'vamp' for k in changed)

    # Removing an item destroys just its row
    victim = next(m for m in st.markers if m.name == "Inserted")
    st.delete_marker(victim.id)
    pump(ui, 0.3)
    assert ('marker', victim.id) not in cs._rows_by_key
    assert len(cs._row_order) == 10
    st.select_loop(0)
    st.delete_selected_loop()


def test_cue_sheet_single_change_is_fast_with_many_items(ui):
    st = ui.app_state
    cs = ui.cue_sheet
    for i in range(150):
        st.add_marker(0.05 + i * 0.07, f"C{i}")
    for i in range(30):
        st.add_hold(0.07 + i * 0.37)
    pump(ui, 0.5)
    cs._rebuild_list()
    st.add_marker(5.555, "one more")
    cs.update_data(st.markers, st.loops, st.selected_loop_index, st.skips,
                   holds=list(st.holds))
    t0 = time.perf_counter()
    cs._rebuild_list()
    ui.update_idletasks()
    elapsed = time.perf_counter() - t0
    assert len(cs._row_order) == 181
    assert elapsed < 0.25, elapsed


# ---------------------------------------------------------------- finding 8

def test_position_throttle_delivers_latest_update(ui):
    ui._last_ui_update = -1e9
    ui._update_position(2.0, False)
    ui._update_position(2.1, False)                  # inside the 33 ms slot
    assert ui.time_label.cget("text") == mod("utils.formatting").format_time(2.0)
    pump(ui, 0.15)
    assert ui.time_label.cget("text") == mod("utils.formatting").format_time(2.1)
    assert ui.cue_sheet._current_position == 2.1


def test_throttle_and_countdown_use_monotonic_clock():
    import inspect
    app_src = inspect.getsource(mod("frontend.app").LoopStationApp._update_position)
    side_src = inspect.getsource(mod("frontend.notes_sidebar").NotesSidebar.update_position)
    assert "time.monotonic()" in app_src and "time.time()" not in app_src
    assert "time.monotonic()" in side_src and "time.time()" not in side_src


# ---------------------------------------------------------------- finding 9

def test_vamp_modal_forwards_show_keys(ui, monkeypatch):
    st = ui.app_state
    st.add_loop(2.0, 4.0, name="V1")
    pump(ui, 0.2)
    ui._open_vamp_settings(0)
    pump(ui, 0.3)
    m = vamp_modal(ui)
    calls = []
    monkeypatch.setattr(st, "go", lambda source="ui": calls.append(source) or True)
    m.focus_force()
    pump(ui, 0.1)
    if ui.focus_get() is not None and ui.focus_get() is not m:
        monkeypatch.setattr(ui, "focus_get", lambda: m)
    send_key(ui, m, "Return")
    pump(ui, 0.1)
    assert calls == ["key"]
    # typing in the name entry: Return is not GO
    monkeypatch.setattr(ui, "focus_get", lambda: m.entry_name)
    assert m._forward_show_key(_Key("Return")) is None
    assert calls == ["key"]
    m._on_cancel_click()
    pump(ui)
    st.select_loop(0)
    st.delete_selected_loop()


def test_rename_prompt_forwards_show_keys_and_returns_name(ui, monkeypatch):
    st = ui.app_state
    marker = st.add_marker(1.0, "Old")
    pump(ui, 0.3)
    calls = []
    monkeypatch.setattr(st, "go", lambda source="ui": calls.append(source) or True)
    NamePrompt = mod("frontend.widgets").NamePrompt
    seen = []

    def drive():
        prompts = [w for w in ui.winfo_children() if isinstance(w, NamePrompt)]
        dlg = prompts[-1]
        seen.append(dlg.entry.get())
        # focus outside the entry: GO reaches the app
        monkeypatch.setattr(ui, "focus_get", lambda: dlg)
        assert dlg._forward_show_key(_Key("Return")) == "break"
        # typing in the entry: Return is the entry's (OK), not GO
        monkeypatch.setattr(ui, "focus_get", lambda: dlg.entry)
        assert dlg._forward_show_key(_Key("Return")) is None
        dlg.entry.delete(0, "end")
        dlg.entry.insert(0, "  New name  ")
        dlg._on_ok()
    ui.after(300, drive)
    ui.cue_sheet._on_rename_marker(marker.id, marker.name)
    pump(ui, 0.2)
    assert seen == ["Old"]
    assert calls == ["key"]
    assert next(m for m in st.markers if m.id == marker.id).name == "New name"


def test_toggle_live_key_uses_press_twice_confirm(ui, monkeypatch):
    st = ui.app_state
    asked = []
    monkeypatch.setattr(app_module(ui).messagebox, "askyesno",
                        lambda *a, **k: asked.append(1) or True)
    key = ui._key_handlers["toggle_live"]
    key()
    pump(ui)
    assert st.live_mode
    key()                                            # first press only arms
    pump(ui)
    assert st.live_mode and "again" in ui.status_label.cget("text")
    key()                                            # second press leaves
    pump(ui)
    assert not st.live_mode and asked == []


def test_live_blocks_cue_sheet_click_jumps_but_not_next_prev(ui):
    st = ui.app_state
    m1 = st.add_marker(2.0, "A")
    st.add_marker(5.0, "B")
    st.seek(1.0)
    pump(ui, 0.3)
    st.set_live_mode(True)
    pump(ui)
    ui.cue_sheet._on_jump_marker(m1.id)              # row click: locked
    pump(ui)
    assert st.get_position() == pytest.approx(1.0)
    assert "locked" in ui.status_label.cget("text")
    ui.cue_sheet._on_next()                          # toolbar ⏭ = next-cue key
    pump(ui)
    assert st.get_position() == pytest.approx(2.0)
    st.set_live_mode(False)
    pump(ui)


# --------------------------------------------------------------- finding 10

def test_close_and_theme_restart_ask_while_show_runs(ui, monkeypatch):
    st = ui.app_state
    asked = []
    monkeypatch.setattr(app_module(ui).messagebox, "askyesno",
                        lambda *a, **k: asked.append(a) or False)
    st.play()
    pump(ui)
    ui._on_close_request()
    assert ui.winfo_exists() and not ui._closing and ui.app_state is not None
    assert len(asked) == 1
    assert ui.confirm_interrupt_show("restart") is False
    st.stop()
    st.set_live_mode(True)
    pump(ui)
    assert ui.confirm_interrupt_show("restart") is False
    st.set_live_mode(False)
    pump(ui)
    asked.clear()
    assert ui.confirm_interrupt_show("restart") is True and asked == []


def test_close_handlers_registered_for_both_platforms(ui):
    assert ui.protocol("WM_DELETE_WINDOW")           # a Tcl command name
    assert "_on_close_request" in ui.protocol("WM_DELETE_WINDOW")
    if sys.platform == "darwin":
        assert ui.tk.call("info", "commands", "::tk::mac::Quit")


def test_library_folder_and_last_song_are_remembered_and_restored(ui, tmp_path, monkeypatch):
    # The library became the show sidebar: an opened folder is remembered as
    # the last show (utils.preferences "show"), the last song as before.
    prefs = mod("utils.preferences")
    st = ui.app_state
    lib = tmp_path / "lib"
    lib.mkdir()
    song = make_song(lib, "Remembered.wav", secs=2.0)
    ui.library.load_folder(str(lib))
    try:
        last = prefs.get_last_show()
        assert last["kind"] == "folder" and os.path.samefile(last["path"], str(lib))
        stored = prefs.load_preferences()
        assert stored[ui.PREF_LAST_SONG] == st.current_song_path

        selected, shared = [], []
        monkeypatch.setattr(ui, "_on_song_select", lambda p: selected.append(p))
        monkeypatch.setattr(ui, "_start_web_share", lambda show_popup=True: shared.append(show_popup))
        prefs.save_preferences({ui.PREF_LAST_SONG: song,
                                ui.PREF_RESTART_STATE: {"sharing": True}})
        st.new_show()                                  # forgets the last show...
        prefs.set_last_show("folder", str(lib))        # ...as a fresh start would see it
        pump(ui)
        assert ui.library.current_folder == ""
        ui._restore_session()
        pump(ui, 0.6)
        assert os.path.samefile(ui.library.current_folder, str(lib))
        assert selected == [song]
        assert shared == [False]
        assert not prefs.load_preferences().get(ui.PREF_RESTART_STATE)   # one-shot
    finally:
        st.new_show()
        pump(ui)


def test_restart_remembers_sharing_state(ui, monkeypatch):
    prefs = mod("utils.preferences")
    destroyed = []
    monkeypatch.setattr(ui, "_shutdown_backend", lambda: None)
    monkeypatch.setattr(ui, "destroy", lambda: destroyed.append(1))

    class FakeServer:
        running = True
        def stop(self):
            pass
    monkeypatch.setattr(ui, "_web_server", FakeServer())
    ui.request_restart()
    try:
        assert destroyed == [1]
        assert prefs.load_preferences()[ui.PREF_RESTART_STATE] == {"sharing": True}
    finally:
        ui._closing = False
        ui.restart_required = False
        prefs.save_preferences({ui.PREF_RESTART_STATE: None})


# --------------------------------------------------------------- finding 11

def test_library_label_sort_errors_and_batching(ui, tmp_path, monkeypatch):
    # Folder shows: label, casefold order, hidden / non-audio files skipped,
    # big folders render in slices, an unreadable folder gives an empty show.
    lib = ui.library
    st = ui.app_state
    folder = tmp_path / "My Songs"
    folder.mkdir()
    for name in ("beta.wav", "Alpha.wav", "alpha2.wav", "Zed.wav", "._junk.wav", "notes.txt"):
        (folder / name).write_bytes(b"")
    try:
        lib.load_folder(str(folder) + "/")              # macOS picker style
        assert "My Songs" in lib.folder_label.cget("text")
        assert lib.songs == ["Alpha.wav", "alpha2.wav", "beta.wav", "Zed.wav"]

        many = tmp_path / "many"
        many.mkdir()
        for i in range(400):
            (many / f"Song {i:04d}.mp3").write_bytes(b"")
        t0 = time.perf_counter()
        lib.load_folder(str(many))
        assert time.perf_counter() - t0 < 2.0
        assert lib.count_label.cget("text") == "400 songs"
        assert lib.rows_pending                          # rows still being added
        for _ in range(400):
            pump(ui, 0.02)
            if not lib.rows_pending:
                break
        assert len(lib.song_buttons) == 400

        unreadable = tmp_path / "locked"
        unreadable.mkdir()

        def fail(_path):
            raise PermissionError("denied")
        monkeypatch.setattr(mod("backend.show").os, "listdir", fail)
        lib.load_folder(str(unreadable))
        monkeypatch.undo()
        assert lib.songs == [] and lib.song_buttons == []
        assert lib.count_label.cget("text") == "0 songs"
    finally:
        st.new_show()
        pump(ui)


# --------------------------------------------------------------- finding 12

@pytest.mark.parametrize("text", ["inf", "-inf", "nan", "1e400", "x"])
def test_goto_bar_rejects_non_finite_input(ui, text):
    st = ui.app_state
    st.set_song_tempo_map(120.0, 4, 0.0)
    pump(ui)
    called = []
    gp = ui.go_panel
    original = gp.on_goto_bar
    gp.on_goto_bar = lambda bar: called.append(bar)
    try:
        gp.entry_bar.delete(0, "end")
        gp.entry_bar.insert(0, text)
        gp._on_goto_click()
    finally:
        gp.on_goto_bar = original
        gp.entry_bar.delete(0, "end")
    assert called == []
    assert gp.lbl_flash.cget("text") == "Enter a bar number"


# --------------------------------------------------------------- finding 13

def test_compact_transport_labels_survive_state_changes(ui):
    st = ui.app_state

    class Ev:
        width = 500
    ui._transport_compact = None
    ui._on_transport_resize(Ev())
    assert ui._transport_compact
    st.play()
    pump(ui)
    assert ui.btn_play.cget("text") == "⏸"
    ui._set_exit_buttons('skip')
    assert ui.btn_exit_loop.cget("text") == "⏭ SKIP"
    Ev.width = 2000
    ui._on_transport_resize(Ev())
    assert ui.btn_play.cget("text") == "⏸  PAUSE"
    assert ui.btn_exit_loop.cget("text") == "⏭  SKIP VAMP"
    st.stop()
    pump(ui)
    assert ui.btn_play.cget("text") == "▶  PLAY"


def test_loop_selection_lock_label_kept_when_preview_ends(ui):
    gp = ui.go_panel
    gp.set_live(True)
    gp.set_previewing(True)
    assert gp.btn_loop_sel.cget("text") == "⏹ Stop loop"
    gp.set_previewing(False)
    assert gp.btn_loop_sel.cget("text") == "Loop selection (locked)"
    gp.set_live(False)
    assert gp.btn_loop_sel.cget("text") == "↻ Loop selection"


# --------------------------------------------------------------- finding 14

def test_fixed_shortcuts_bound_in_both_cases(ui):
    bound = set(ui.bind())
    for letter in "iosn":
        assert {letter, f"<Key-{letter}>"} & bound
        assert {letter.upper(), f"<Key-{letter.upper()}>"} & bound


# --------------------------------------------------------------- finding 15

def test_shortcuts_and_share_popups_are_single_instance(ui, monkeypatch):
    ui._show_hotkeys_modal()
    ui._show_hotkeys_modal()
    pump(ui, 0.2)
    ctk = mod("customtkinter")
    tops = [w for w in ui.winfo_children()
            if isinstance(w, ctk.CTkToplevel) and w.title() == "Keyboard Shortcuts"]
    assert len(tops) == 1
    tops[0].destroy()

    ui._show_share_popup("http://127.0.0.1:8080/?t=x")
    ui._show_share_popup("http://127.0.0.1:8080/?t=x")
    pump(ui, 0.2)
    shares = [w for w in ui.winfo_children()
              if isinstance(w, ctk.CTkToplevel) and w.title() == "Cue Monitor - Share"]
    assert len(shares) == 1
    ui._close_share_popup()
    pump(ui)


# --------------------------------------------------------------- finding 16

def test_hints_follow_the_key_map(ui):
    keys = dict(ui._key_map)
    try:
        new = dict(keys, add_marker=["F5"], prev_cue=["comma"], stop=["F2"])
        ui.apply_key_map(new)
        cs = ui.cue_sheet
        assert cs._hint_label.cget("text") == "F5 = add cue at playhead"
        assert "(F5)" in cs._tip_add_cue.text
        assert "comma" in cs._tip_prev.text
        assert "(F2)" in ui._tip_stop.text
    finally:
        ui.apply_key_map(keys)
    assert ui.cue_sheet._hint_label.cget("text") == "M = add cue at playhead"


# --------------------------------------------------------------- finding 17

def test_shortcuts_ignored_while_loading(ui, monkeypatch):
    st = ui.app_state
    calls = []
    monkeypatch.setattr(st, "go", lambda source="ui": calls.append("go") or True)
    monkeypatch.setattr(st, "toggle_play_pause", lambda: calls.append("pp"))
    monkeypatch.setattr(ui, "_is_typing", lambda: False)
    ui._loading = True
    try:
        assert ui._safe(lambda: calls.append("x"))(None) == "break"
        assert ui.handle_show_key(_Key("Return")) is False
        ui.msg_queue.put(("midi_action", ("go",)))
        pump(ui, 0.1)
    finally:
        ui._loading = False
    assert calls == []
    assert ui.handle_show_key(_Key("space")) is True
    assert calls == ["pp"]


# --------------------------------------------------------------- finding 18

def test_waveform_height_follows_widget_scaling(ui, monkeypatch):
    wmod = mod("frontend.waveform")
    tk = mod("tkinter")
    monkeypatch.setattr(wmod, "_widget_scaling", lambda _w: 2.0)
    holder = tk.Frame(ui)
    try:
        wf = wmod.WaveformWidget(holder)
        assert int(wf.container.cget("height")) == 2 * wmod.WAVEFORM_HEIGHT
    finally:
        holder.destroy()
    assert wmod._widget_scaling.__name__ == "<lambda>"


def test_go_at_song_end_loads_and_starts_next_library_song(ui, tmp_path):
    st = ui.app_state
    original_song, original_folder = st.current_song_path, ui.library.current_folder
    lib = tmp_path / "setlist"
    lib.mkdir()
    first = make_song(lib, "01 Opening.wav", secs=2.0)
    second = make_song(lib, "02 Second.wav", secs=2.0, freq=440)
    ui.library.load_folder(str(lib))
    try:
        load(ui, first)
        st.set_live_mode(True)                   # GO advances even in Live Mode
        st.play()
        st.audio.advance(5.0)                    # runs off the end
        pump(ui)
        assert st.standby()["action"] == "next_song"
        st._last_go_time = -1e9
        assert st.go() is True
        for _ in range(500):
            pump(ui, 0.01)
            if not ui._loading and st.current_song_path == second:
                break
        pump(ui, 0.2)
        assert st.current_song_path == second
        assert st.state.name == "PLAYING"
        st.audio.advance(5.0)                    # last song in the list ends
        pump(ui)
        assert st.standby()["action"] == "none"
    finally:
        st.set_live_mode(False)
        st.stop()
        if original_folder:
            ui.library.load_folder(original_folder)
        if original_song and st.current_song_path != original_song:
            load(ui, original_song)
