"""
Smoke test of the desktop UI: builds the real LoopStationApp (withdrawn)
on a FakeEngine-backed StateManager and exercises GO, Live Mode, both
settings dialogs and the dispatch of every phase-2 event.

Skipped when no display is available.
"""
import logging
import sys
import time

import numpy as np
import pytest

from fake_engine import FakeEngine
from helpers_audio import write_wav, SR


def _fresh_frontend():
    """Load the theme colours, then (re)import the frontend so it sees them."""
    import config
    config.load_theme()
    for name in [n for n in sys.modules if n == "frontend" or n.startswith("frontend.")]:
        del sys.modules[name]
    import frontend.app as app_module
    return app_module


def pump(app, seconds=0.2):
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        time.sleep(0.005)


@pytest.fixture(scope="module")
def _app(tmp_path_factory):
    """
    ONE app (one Tcl interpreter) for the whole module: creating and
    destroying many Tk interpreters in a long pytest process occasionally
    fails on Windows ("Can't find a usable init.tcl"), so the window is
    built once and reset between tests by the ``ui`` fixture.
    """
    tmp = tmp_path_factory.mktemp("frontend_smoke")
    mp = pytest.MonkeyPatch()
    from utils import preferences
    mp.setattr(preferences, "PREFS_FILE", str(tmp / "prefs.json"))

    app_module = _fresh_frontend()
    from backend.state_manager import StateManager

    def make_state(ffmpeg_path="ffmpeg", **_kw):
        return StateManager(engine=FakeEngine(), data_file=str(tmp / "loop_data.json"))

    mp.setattr(app_module, "StateManager", make_state)
    errors = []
    mp.setattr(app_module.messagebox, "showerror", lambda *a, **k: errors.append(a))
    mp.setattr(app_module.messagebox, "showwarning", lambda *a, **k: errors.append(a))

    app = None
    last_error = None
    for _attempt in range(3):
        try:
            app = app_module.LoopStationApp(ffmpeg_path="ffmpeg")
            break
        except Exception as e:  # no display / transient Tcl init failure
            last_error = e
            time.sleep(0.2)
    if app is None:
        mp.undo()
        pytest.skip(f"no display: {last_error}")

    app.withdraw()
    app.tk_errors = []
    app.report_callback_exception = lambda *exc: app.tk_errors.append(exc)
    app.initialize_audio_system()

    song = str(tmp / "Smoke Song.wav")
    t = np.arange(int(12 * SR)) / SR
    mono = (np.sin(2 * np.pi * 220 * t) * 8000).astype(np.int16)
    write_wav(song, np.stack([mono, mono], axis=1))
    app._on_song_select(song)
    for _ in range(500):
        pump(app, 0.01)
        if not app._loading:
            break
    pump(app, 0.2)
    assert app.app_state.current_song_path == song

    app.shown_errors = errors
    yield app
    try:
        app._on_close()
    except Exception:
        pass
    mp.undo()


@pytest.fixture
def ui(_app):
    """The shared app, reset to a clean, stopped, unlocked song."""
    st = _app.app_state
    st.set_live_mode(False)
    st.stop()
    for m in list(st.markers):
        st.delete_marker(m.id)
    for h in list(st.holds):
        st.delete_hold(h.id)
    st.set_song_tempo_map(0.0, 4, 0.0)
    st.go_log.clear()
    st._last_go_time = -1e9          # forget the GO debounce of the last test
    for dlg in (_app._settings_dialog, _app._song_dialog):
        if dlg is not None:
            dlg.close()
    _app.__dict__.pop("_confirm_leave_live", None)
    if st.device_lost:
        st.device_lost = False
    _app._on_device_restored("reset")
    _app.shown_errors.clear()
    _app.tk_errors.clear()
    _app._shown_device_errors.clear()
    pump(_app, 0.1)
    return _app


def _no_handler_errors(caplog, app=None):
    if app is not None:
        assert not app.tk_errors, app.tk_errors
    bad = [r for r in caplog.records if "Error handling" in r.getMessage()
           or r.levelno >= logging.ERROR and r.name.startswith("LoopStation.App")]
    assert not bad, [r.getMessage() for r in bad]


def test_go_plays_and_logs(ui, caplog):
    st = ui.app_state
    assert st.standby()["action"] == "play"
    ui._on_go("ui")
    pump(ui)
    assert st.is_playing()
    assert len(st.go_log) == 1
    assert "GO" in ui.status_label.cget("text")
    # A second press right away is ignored and flashes a message
    ui._on_go("ui")
    assert len(st.go_log) == 1
    assert ui.go_panel.lbl_flash.cget("text")
    _no_handler_errors(caplog, ui)


def test_live_mode_blocks_delete(ui, caplog):
    st = ui.app_state
    marker = st.add_marker(2.0, "Cue A")
    pump(ui)
    ui._toggle_live_mode()
    pump(ui)
    assert st.live_mode
    assert "LIVE" in ui.btn_live.cget("text")
    assert not ui.library.accepting_clicks
    assert st.delete_marker(marker.id) is False
    pump(ui)
    assert any(m.id == marker.id for m in st.markers)
    assert "Live Mode" in ui.status_label.cget("text")
    # H (add hold) is blocked too
    ui._on_add_hold()
    pump(ui)
    assert st.holds == []
    ui._confirm_leave_live = lambda: True
    ui._toggle_live_mode()
    pump(ui)
    assert not st.live_mode
    assert ui.library.accepting_clicks
    _no_handler_errors(caplog, ui)


def test_dialogs_open_and_close(ui, caplog):
    ui._open_settings_dialog()
    pump(ui, 0.3)
    dlg = ui._settings_dialog
    assert dlg is not None
    dlg._start_capture("go")

    class _Ev:
        keysym = "Next"
    dlg._on_keypress(_Ev())
    assert ui._key_map["go"] == ["Next"]
    assert "<KeyPress-Next>" in ui._bound_key_seqs
    dlg._reset_keys()
    assert ui._key_map["go"] == ["Return", "KP_Enter"]
    dlg.close()
    pump(ui)
    assert ui._settings_dialog is None

    ui._open_song_dialog()
    pump(ui, 0.3)
    sdlg = ui._song_dialog
    assert sdlg is not None
    sdlg.entry_bpm.delete(0, "end")
    sdlg.entry_bpm.insert(0, "120")
    sdlg._save_tempo_map()
    pump(ui)
    assert ui.app_state.settings.bpm == 120.0
    assert ui._bar_label_visible
    sdlg._apply_tempo_key()
    pump(ui)
    sdlg.close()
    pump(ui)
    assert ui._song_dialog is None
    _no_handler_errors(caplog, ui)


def test_go_to_bar_and_home(ui):
    st = ui.app_state
    st.set_song_tempo_map(120.0, 4, 0.0)
    pump(ui)
    ui._on_goto_bar(3)      # bar 3 at 120 BPM 4/4 starts at 4.0 s
    assert st.get_position() == pytest.approx(4.0, abs=1e-6)
    assert ui.bar_label.cget("text").startswith("Bar 3")
    ui._on_seek_start()
    assert st.get_position() == pytest.approx(0.0, abs=1e-6)


def test_every_new_event_dispatches(ui, caplog):
    st = ui.app_state
    hold = st.add_hold(5.0, "Fermata")
    ui._open_song_dialog()
    ui._open_settings_dialog()
    pump(ui, 0.3)
    entry = {"ts": time.time(), "source": "ui", "action": "play", "position": 0.0,
             "label": "GO: play"}
    msgs = [
        ("standby_changed", (st.standby(),)),
        ("go_fired", (entry,)),
        ("hold_reached", (hold,)),
        ("loop_cycle", (2, 3)),
        ("hold_released", ()),
        ("holds_changed", (list(st.holds),)),
        ("loop_cycle", (4, None)),
        ("live_mode_changed", (True,)),
        ("blocked_in_live_mode", ("delete_marker",)),
        ("live_mode_changed", (False,)),
        ("settings_changed", (st.settings.copy(),)),
        ("stems_changed", (list(st.stems),)),
        ("render_state", ("rendering", "")),
        ("render_state", ("done", "")),
        ("render_state", ("failed", "boom")),
        ("tempo_estimated", (118.5, 0.25)),
        ("device_error", ("device vanished",)),
        ("device_error", ("device vanished",)),
        ("loop_ready", ()),
        ("midi_action", ("toggle_live",)),
        ("midi_learned", ("go", "note_on:60")),
        ("midi_action", ("toggle_live",)),     # leaving live: first press only arms
        ("midi_action", ("toggle_live",)),     # second press within 2 s leaves
    ]
    for msg in msgs:
        ui.msg_queue.put(msg)
    pump(ui, 0.4)
    assert not st.live_mode
    assert len(ui.shown_errors) == 1          # device error shown once
    assert ui._song_dialog.entry_bpm.get() == "118.5"
    assert ui.go_panel.lbl_pass.cget("text") == "♻ Pass 4"
    ui._song_dialog.close()
    ui._settings_dialog.close()
    pump(ui)
    _no_handler_errors(caplog, ui)


class _Key:
    def __init__(self, keysym):
        self.keysym = keysym


def send_key(app, dlg, keysym):
    """
    Press ``keysym`` in ``dlg``. Tk only delivers synthetic key events to the
    window holding keyboard focus; when the test process cannot take focus
    (e.g. macOS, not the frontmost app) the event is dropped or lands in
    whichever window does hold focus. Generate a real event only when focus
    is inside ``dlg``; otherwise run the dialog's <KeyPress> handlers in
    their binding order instead.
    """
    focused = app.focus_get()
    if focused is not None and str(focused).startswith(str(dlg)):
        dlg.event_generate(f"<KeyPress-{keysym}>")
        return
    event = _Key(keysym)
    if dlg._forward_show_key(event) == "break":
        return
    if hasattr(dlg, "_on_keypress"):
        dlg._on_keypress(event)


def test_leaving_live_mode_asks_and_defaults_to_staying(ui, monkeypatch):
    st = ui.app_state
    asked = []
    monkeypatch.setattr(ui_module(ui).messagebox, "askyesno",
                        lambda *a, **k: asked.append(k) or False)
    ui._toggle_live_mode()                       # entering: no question
    pump(ui)
    assert st.live_mode and asked == []
    ui._toggle_live_mode()                       # leaving: asked, answer No
    pump(ui)
    assert st.live_mode and len(asked) == 1
    assert asked[0]["default"] == "no"
    monkeypatch.setattr(ui_module(ui).messagebox, "askyesno", lambda *a, **k: True)
    ui._toggle_live_mode()
    pump(ui)
    assert not st.live_mode


def ui_module(ui):
    return sys.modules[type(ui).__module__]


def test_live_mode_locks_rehearsal_tools(ui):
    st = ui.app_state
    st.play()
    pump(ui)
    pos = st.get_position()
    st.set_live_mode(True)
    pump(ui)
    assert ui.go_panel.btn_loop_sel.cget("state") == "disabled"
    assert ui.detector.btn_select.cget("state") == "disabled"
    assert ui.detector.btn_mode.cget("state") == "disabled"
    ui._on_waveform_seek(0.5)                    # click-seek blocked
    ui._on_seek_start()                          # Home blocked
    pump(ui)
    assert st.get_position() == pytest.approx(pos)
    assert "locked" in ui.status_label.cget("text")
    ui.sel_start, ui.sel_end = 1.0, 3.0
    ui._on_loop_selection()
    assert not st.is_previewing()
    st.set_live_mode(False)
    pump(ui)
    assert ui.go_panel.btn_loop_sel.cget("state") == "normal"
    assert ui.detector.btn_select.cget("state") == "normal"
    ui._on_waveform_seek(0.5)
    assert st.get_position() == pytest.approx(0.5 * st.song_length)
    st.stop()


def test_live_mode_locks_song_tempo_map_and_osc(ui):
    ui._open_song_dialog()
    pump(ui)
    dialog = ui._song_dialog
    st = ui.app_state
    st.set_song_tempo_map(120)
    st.set_song_osc_cue('12')
    dialog.entry_bpm.delete(0, 'end')
    dialog.entry_bpm.insert(0, '180')
    dialog.entry_osc.delete(0, 'end')
    dialog.entry_osc.insert(0, '99')
    st.set_live_mode(True)
    pump(ui)
    for widget in (dialog.entry_bpm, dialog.btn_save_map, dialog.entry_osc, dialog.btn_save_osc):
        assert widget.cget('state') == 'disabled'
    dialog._save_tempo_map()
    dialog._save_osc()
    assert st.settings.bpm == 120 and st.settings.osc_cue == '12'
    st.set_live_mode(False)
    pump(ui)
    assert dialog.btn_save_map.cget('state') == 'normal'
    dialog.close()


def test_save_failure_and_recovery_are_visible(ui, monkeypatch):
    import backend.state_manager as module

    def fail_replace(*args):
        raise PermissionError('injected full disk')

    with monkeypatch.context() as mp:
        mp.setattr(module.os, 'replace', fail_replace)
        ui._on_save()
        pump(ui)
        assert 'SAVE FAILED' in ui.status_label.cget('text')
        assert ui.app_state._save_dirty
    assert ui.app_state.flush_save()
    pump(ui)
    assert 'Pending changes saved' in ui.status_label.cget('text')


def test_song_dialog_source_choice_and_clipping_feedback(ui):
    st = ui.app_state
    st.audio._stems.append({'name': 'piano', 'gain_db': 0, 'muted': True, 'solo': False})
    st.stems = st.audio.get_stems()
    ui._open_song_dialog()
    pump(ui)
    dialog = ui._song_dialog
    assert dialog.source_menu.get() == 'Full mix'
    dialog._on_source('Stems')
    pump(ui)
    assert dialog.source_menu.get() == 'Stems'
    assert st.audio._stem('mix')['muted']
    dialog.update_output_levels({'peak': 1.2, 'clipped_blocks': 2})
    assert 'CLIPPING' in dialog.lbl_levels.cget('text')
    dialog.close()


def test_failed_save_prevents_shutdown_and_theme_restart(ui, monkeypatch):
    import backend.state_manager as module

    def fail_replace(*args):
        raise PermissionError('injected disk failure')

    ui.app_state.add_marker(1, 'Unsaved')
    with monkeypatch.context() as mp:
        mp.setattr(module.os, 'replace', fail_replace)
        ui.request_restart()
        assert ui.app_state is not None and ui.winfo_exists()
        assert not ui.restart_required and not ui._closing
        assert ui.app_state._save_dirty and ui.shown_errors
    assert ui.app_state.flush_save()


def test_song_settings_is_modeless_and_forwards_go(ui, monkeypatch):
    st = ui.app_state
    ui._open_song_dialog()
    pump(ui, 0.3)
    dlg = ui._song_dialog
    assert dlg.grab_current() is None            # no modal grab
    calls = []
    monkeypatch.setattr(st, "go", lambda source="ui": calls.append(source) or True)
    dlg.focus_force()
    pump(ui, 0.1)
    send_key(ui, dlg, "Return")
    pump(ui, 0.1)
    assert calls == ["key"]
    # typing in an entry does not fire GO
    dlg.entry_bpm.focus_force()
    pump(ui, 0.1)
    if not ui._is_typing():                      # focus did not land (see send_key)
        monkeypatch.setattr(ui, "focus_get", lambda: dlg.entry_bpm)
    assert ui.handle_show_key(_Key("Return")) is False
    assert calls == ["key"]
    dlg.close()
    pump(ui)


def test_settings_dialog_forwards_show_keys(ui, monkeypatch):
    st = ui.app_state
    ui._open_settings_dialog()
    pump(ui, 0.3)
    dlg = ui._settings_dialog
    calls = []
    monkeypatch.setattr(st, "go", lambda source="ui": calls.append("go") or True)
    monkeypatch.setattr(st, "stop", lambda: calls.append("stop"))
    dlg.focus_force()
    pump(ui, 0.1)
    send_key(ui, dlg, "Return")
    pump(ui, 0.1)
    assert calls == ["go"]
    # while capturing a new key, the key is NOT forwarded
    dlg._start_capture("stop")
    send_key(ui, dlg, "F9")
    pump(ui, 0.1)
    assert calls == ["go"] and ui._key_map["stop"] == ["F9"]
    dlg._reset_keys()
    dlg.close()
    pump(ui)


def test_device_lost_banner_instead_of_messagebox(ui):
    st = ui.app_state
    st.play()
    pump(ui)
    fake = st.audio
    fake.unplug()                                # the monitor thread notices
    for _ in range(200):
        pump(ui, 0.02)
        if ui.go_panel.device_lost and st.state.name == "PAUSED":
            break
    pump(ui, 0.1)
    assert st.device_lost and st.state.name == "PAUSED"
    assert ui.go_panel.device_lost
    assert ui.go_panel.lbl_device.winfo_manager() == "pack"
    assert "AUDIO DEVICE LOST" in ui.go_panel.lbl_device.cget("text")
    assert ui.shown_errors == []                 # no messagebox mid-show
    assert ui.go_panel.lbl_standby.cget("text") == "Audio device lost - check interface"
    fake.replug()                                # retried within 2 s
    for _ in range(200):
        pump(ui, 0.02)
        if not ui.go_panel.device_lost:
            break
    assert not st.device_lost and not ui.go_panel.device_lost
    assert ui.go_panel.lbl_device.winfo_manager() == ""
    assert "back" in ui.status_label.cget("text")
    st.stop()


def test_failed_routing_reverts_selector_and_shows_message(ui):
    st = ui.app_state
    ui._open_song_dialog()
    pump(ui, 0.3)
    dlg = ui._song_dialog
    st.audio.fail_device_change = True
    try:
        dlg.routing.set("Multichannel")
        dlg._on_routing("Multichannel")
        pump(ui, 0.2)
        assert dlg.routing.get() == "Stereo"
        assert "routing reverted" in dlg.lbl_out.cget("text")
        assert st.settings.routing_mode == "stereo"
    finally:
        st.audio.fail_device_change = False
    dlg.close()
    pump(ui)


def test_midi_status_reaches_status_line_and_settings(ui):
    ui._open_settings_dialog()
    pump(ui, 0.3)
    ui.msg_queue.put(("midi_status", ("MIDI 'Pedal' lost - reconnecting...",)))
    pump(ui, 0.2)
    assert "lost" in ui.status_label.cget("text")
    assert ui._settings_dialog.lbl_port.cget("text") == "MIDI 'Pedal' lost - reconnecting..."
    ui._settings_dialog.close()
    pump(ui)
