"""
UI polish: platform fonts, theme-derived surfaces, the HELD GO pulse.

Reuses the headless app fixture of test_frontend_smoke (one app per module).
"""
import sys

import pytest

import config
from tests.test_frontend_smoke import _app, ui, pump  # noqa: F401
from utils import fonts


def test_fonts_are_platform_families():
    if sys.platform == "darwin":
        # "Segoe UI" / "Consolas" don't exist on macOS: Tk fell back to a
        # proportional font and the clock changed width as it ran
        assert fonts.FONT_MONO == "Menlo"
    elif sys.platform == "win32":
        assert (fonts.FONT_UI, fonts.FONT_MONO) == ("Segoe UI", "Consolas")
    assert fonts.FONT_UI and fonts.FONT_MONO


def test_mono_font_has_fixed_width_digits(ui):
    import tkinter.font as tkfont
    f = tkfont.Font(root=ui, family=fonts.FONT_MONO, size=32, weight="bold")
    assert f.measure("0:00.00") == f.measure("1:11.11")


def test_mix_and_tint():
    assert config.mix("#000000", "#ffffff", 0) == "#000000"
    assert config.mix("#000000", "#ffffff", 1) == "#ffffff"
    assert config.mix("#000000", "#ffffff", 0.5) == "#808080"
    assert config.tint("#ff0000", 0) == config.COLOR_BG_MEDIUM


@pytest.mark.parametrize("theme", ["Midnight Blue", "Paper White"])
def test_surfaces_step_away_from_panel(monkeypatch, theme):
    saved = {k: getattr(config, k) for k in dir(config) if k.startswith("COLOR_")}
    monkeypatch.setattr(config, "get_theme_preference", lambda: theme)
    try:
        config.load_theme()
        # Raised controls, borders and hovers are each further from the panel
        def dist(c):
            a = [int(config.COLOR_BG_MEDIUM[i:i + 2], 16) for i in (1, 3, 5)]
            b = [int(c[i:i + 2], 16) for i in (1, 3, 5)]
            return sum(abs(x - y) for x, y in zip(a, b))
        assert 0 < dist(config.COLOR_BG_LIGHT) < dist(config.COLOR_BORDER) \
            < dist(config.COLOR_HOVER)
        assert config.COLOR_BTN_DISABLED != config.COLOR_BG_MEDIUM
    finally:
        for k, v in saved.items():
            setattr(config, k, v)


def test_held_go_pulses_and_stops(ui):
    gp = ui.go_panel
    gp.set_held(None, held=True)
    try:
        assert gp._pulse_after is not None
        first = gp.btn_go.cget("border_color")
        pump(ui, 0.3)
        assert gp.btn_go.cget("border_color") != first     # it breathes
    finally:
        gp.set_held(held=False)
    assert gp._pulse_after is None
    assert gp.btn_go.cget("border_width") == 0


# ------------------------------------------------- show sidebar empty states --

def _render(lib, **snap):
    lib.render(snap)
    lib.update_idletasks()


def test_no_show_offers_get_started_choices(ui):
    lib = ui.library
    try:
        _render(lib)                                         # nothing open
        assert lib.name_label.cget("text") == "No show open"
        assert lib.welcome.winfo_manager() and not lib.empty_label.winfo_manager()
        labels = [b.cget("text") for b in lib.welcome_buttons]
        assert labels[:3] == ["Open a music folder…", "Open a saved show…", "New empty show"]

        _render(lib, name="Spring", dirty=True)              # new, empty show
        assert not lib.welcome.winfo_manager() and lib.empty_label.winfo_manager()
        assert "No songs in this show yet" in lib.empty_label.cget("text")
    finally:
        lib.refresh_from_state()


def test_welcome_choices_locked_in_live_mode(ui):
    lib = ui.library
    try:
        _render(lib)
        lib.set_locked(True)
        assert all(b.cget("state") == "disabled" for b in lib.welcome_buttons)
        lib.set_locked(False)
        assert all(b.cget("state") == "normal" for b in lib.welcome_buttons)
    finally:
        lib.set_locked(False)
        lib.refresh_from_state()
