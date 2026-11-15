"""
Regression tests for the second review (UI side): the following lock on
exit / fade / cue keys and MIDI, a GO-advance that skips the full GC and the
Tk-thread prefs write, theme-tinted cue-sheet rows, the welcome panel's
recent buttons and cached recent list, and the in-window startup cover.

Reuses the headless app fixture of test_frontend_smoke (one app per module).
"""
import gc
import sys
import threading

import numpy as np
import pytest

import config
from tests.test_frontend_smoke import _app, ui, pump  # noqa: F401
from helpers_audio import write_wav, SR
from utils import preferences


def mod(name):
    return sys.modules[name]


def app_module(ui):
    return sys.modules[type(ui).__module__]


def make_song(tmp_path, name, secs=6.0, freq=330):
    path = str(tmp_path / name)
    t = np.arange(int(secs * SR)) / SR
    mono = (np.sin(2 * np.pi * freq * t) * 8000).astype(np.int16)
    write_wav(path, np.stack([mono, mono], axis=1))
    return path


def wait_loaded(ui):
    for _ in range(500):
        pump(ui, 0.01)
        if not ui._loading:
            break
    pump(ui, 0.2)


@pytest.fixture
def other_song(ui, tmp_path):
    original = ui.app_state.current_song_path
    path = make_song(tmp_path, "Review Two.wav")
    yield path
    st = ui.app_state
    st.stop()
    if st.current_song_path != original:
        ui._on_song_select(original)
        wait_loaded(ui)


# ------------------------------------------------------ 1. following lock --

def test_following_backup_blocks_exit_fade_and_cue_keys(ui, monkeypatch):
    st = ui.app_state
    monkeypatch.setattr(st.mirror, "is_following", lambda: True)
    calls = []
    monkeypatch.setattr(st, "queue_exit", lambda *a, **k: calls.append("exit"))
    pos = st.audio.get_position()
    for action in ("exit_vamp", "fade_exit", "next_cue", "prev_cue"):
        ui._key_handlers[action]()
    for action in ("exit_vamp", "fade_exit", "skip_vamp", "next_cue", "prev_cue"):
        ui._on_midi_action(action)
    ui._on_seek_start()
    pump(ui)
    assert calls == []
    assert st.audio.get_position() == pos
    assert ui.status_label.cget("text") == ui.FOLLOWING_TEXT


# -------------------------------------------- 2 + 3. instant GO-advance --

def test_go_load_skips_full_gc_and_writes_last_song_off_tk_thread(ui, other_song,
                                                                  monkeypatch):
    collects, writes = [], []
    monkeypatch.setattr(app_module(ui).gc, "collect", lambda *a: collects.append(a))
    real_save = preferences.save_preferences

    def spy(prefs):
        writes.append((threading.current_thread() is threading.main_thread(), dict(prefs)))
        real_save(prefs)
    monkeypatch.setattr(preferences, "save_preferences", spy)

    ui._on_song_select(other_song, from_go=True)
    assert not gc.isenabled()           # paused for the load instead
    wait_loaded(ui)
    ui._flush_last_song()
    assert collects == []
    assert gc.isenabled()
    last = [w for w in writes if "last_song" in w[1]]
    assert last and all(not on_tk for on_tk, _ in last)
    assert last[-1][1]["last_song"] == other_song

    ui.app_state.stop()
    ui._on_song_select(other_song)      # a normal load still collects
    wait_loaded(ui)
    assert collects


# ------------------------------------------------ 4. cue sheet on themes --

def test_cue_sheet_rows_are_theme_tints_on_a_light_theme(ui, monkeypatch):
    saved = {k: getattr(config, k) for k in dir(config) if k.startswith("COLOR_")}
    monkeypatch.setattr(config, "get_theme_preference", lambda: "Paper White")
    row_style = mod("frontend.cue_sheet").row_style
    try:
        config.load_theme()

        def luma(c):
            return sum(int(c[i:i + 2], 16) for i in (1, 3, 5)) / (3 * 255)
        assert luma(config.COLOR_BG_MEDIUM) > 0.7
        cases = [("marker", True, False), ("vamp", True, True), ("vamp", False, True),
                 ("vamp", True, False), ("hold", True, False)]
        for kind, current, selected in cases:
            bg, border, width = row_style(kind, current, selected)
            assert luma(bg) > 0.6, (kind, current, selected, bg)
            assert border and width
        assert row_style("marker") == ("transparent", None, 0)
    finally:
        for k, v in saved.items():
            setattr(config, k, v)

    src = open(mod("frontend.cue_sheet").__file__, encoding="utf-8").read()
    for dark in ("#3a3a1a", "#2a4a2a", "#1a331a", "#2a2a1a", "#1a2e3a", "⚙️"):
        assert dark not in src


# ----------------------------------------------- 6 + 7. welcome / recent --

def test_welcome_recent_buttons_do_not_pile_up(ui, tmp_path):
    lib = ui.library
    try:
        lib.render({})
        for i in range(4):
            folder = tmp_path / f"show{i}"
            folder.mkdir()
            preferences.add_recent_show("folder", str(folder))
            lib.render({})
            shown = min(len(preferences.get_recent_shows()),
                        mod("frontend.show_sidebar").RECENT_ON_WELCOME)
            assert len(lib.welcome_buttons) == 3 + shown
            assert all(b.winfo_exists() for b in lib.welcome_buttons)
    finally:
        lib.refresh_from_state()


def test_recent_shows_read_only_after_a_prefs_save(ui, monkeypatch):
    lib = ui.library
    reads = []
    real = preferences.get_recent_shows
    monkeypatch.setattr(preferences, "get_recent_shows",
                        lambda: reads.append(1) or real())
    try:
        preferences.save_preferences({"touch": 1})
        for _ in range(5):
            lib.render({})
        lib.build_show_menu().destroy()
        assert len(reads) == 1
        preferences.save_preferences({"touch": 2})
        lib.render({})
        assert len(reads) == 2
    finally:
        lib.refresh_from_state()


def test_recent_menu_labels_have_no_colour_emoji(ui, tmp_path):
    lib = ui.library
    preferences.add_recent_show("folder", str(tmp_path))
    menu = lib.build_show_menu()
    try:
        recent = menu.nametowidget(menu.entrycget("Recent", "menu"))
        label = recent.entrycget(0, "label")
        assert label.endswith("·  folder")
        assert not any(ord(ch) > 0x1F000 for ch in label)
    finally:
        menu.destroy()


# ------------------------------------------------------ 5. startup cover --

def test_startup_cover_reports_steps_and_lifts_off(ui):
    cover = app_module(ui).StartupCover(ui, version="9.9")
    cover.place(relx=0, rely=0, relwidth=1, relheight=1)
    ui._startup_cover = cover
    assert ui.starting_up
    ui.set_startup_step("Starting the audio engine…", 0.4)
    assert cover.step.cget("text") == "Starting the audio engine…"
    assert abs(cover.bar.get() - 0.4) < 1e-6
    ui.finish_startup()
    assert not ui.starting_up
    assert not cover.winfo_exists()
    ui.finish_startup()                 # idempotent
