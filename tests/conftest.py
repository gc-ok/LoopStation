"""Shared pytest setup: project root on sys.path, headless audio."""
import os
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


def pytest_collection_modifyitems(config, items):
    import pytest
    for item in items:
        if GUI_FIXTURES & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.gui)
