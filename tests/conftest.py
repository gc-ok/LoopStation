"""Shared pytest setup: project root on sys.path, headless audio."""
import inspect
import os
import re
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

# Tests that build the real Tk app (the _app fixture of test_frontend_smoke)
# are marked "gui" automatically, so CI can run them in their own step with
# `-m gui` / `-m "not gui"` and a new UI test module needs no list update.
GUI_FIXTURES = {"_app", "ui"}


def pytest_configure(config):
    config.addinivalue_line("markers", "gui: opens real Tk windows (auto-applied)")
    config.addinivalue_line(
        "markers", "mac_ci_hang: hangs on the GitHub macOS runner's Tk (run by hand on a Mac)")


# GUI tests whose body opens a modal dialog (grab_set) hang inside Tk's
# update() on the GitHub macOS runner, so they are marked mac_ci_hang
# automatically: CI runs them on macOS in a report-only step, and a new test
# that opens a dialog can't stall the blocking GUI step again.
MODAL_OPENERS = re.compile(
    r"_open_(settings|song)_dialog|_open_vamp_settings|_open_manual_vamp_dialog"
    r"|NamePrompt|VampModal")


def _opens_modal(item):
    fn = getattr(item, "function", None)
    if fn is None:
        return False
    try:
        return bool(MODAL_OPENERS.search(inspect.getsource(fn)))
    except (OSError, TypeError):
        return False


def pytest_collection_modifyitems(config, items):
    import pytest
    for item in items:
        if GUI_FIXTURES & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.gui)
            if _opens_modal(item):
                item.add_marker(pytest.mark.mac_ci_hang)
