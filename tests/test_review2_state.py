"""
Regression tests for the second review (backend side): a backup following
its main refuses every local transport action, while the mirror's own
applier still drives it; the preferences change token.
"""
import os

import pytest

from backend.state_manager import StateManager, FOLLOW_LOCKED_ACTIONS
from utils import preferences

from fake_engine import FakeEngine
from helpers_audio import make_signal, write_wav


@pytest.fixture(autouse=True)
def prefs_file(tmp_path, monkeypatch):
    path = str(tmp_path / "prefs" / "user_preferences.json")
    os.makedirs(os.path.dirname(path))
    monkeypatch.setattr(preferences, "PREFS_FILE", path)
    return path


@pytest.fixture
def loaded(tmp_path, monkeypatch):
    monkeypatch.setattr(StateManager, "_start_monitor", lambda self: None)
    sm = StateManager(ffmpeg_path="no-ffmpeg", data_file=str(tmp_path / "loop_data.json"),
                      engine=FakeEngine())
    song = write_wav(tmp_path / "song.wav", make_signal(10.0, sr=8000), sr=8000)
    assert sm.load_song(song)
    sm.add_marker(3.0, name="A")
    sm.add_marker(6.0, name="B")
    yield sm
    sm.stop()
    sm.cleanup()


@pytest.fixture
def following(loaded, monkeypatch):
    monkeypatch.setattr(loaded.mirror, "is_following", lambda: True)
    blocked = []
    loaded.on('blocked_following', lambda what: blocked.append(what))
    return loaded, blocked


def test_following_backup_refuses_local_transport(following):
    sm, blocked = following
    pos = sm.audio.get_position()
    marker = sm.markers[0]
    assert sm.jump_to_next_marker() is False
    assert sm.jump_to_prev_marker() is False
    assert sm.jump_to_marker(marker.id) is False
    assert sm.user_seek(5.0) is False
    assert sm.nudge(1.0) is False
    assert sm.seek_to_start() is False
    assert sm.play_from(2.0) is False
    assert sm.preview_from(2.0) is False
    assert sm.go("key") is False
    assert sm.queue_exit(fade_mode=True) is False
    assert sm.audio.get_position() == pos
    assert len(blocked) == 10


def test_following_backup_refuses_control_surface_actions(following):
    sm, blocked = following
    calls = []
    sm.queue_exit = lambda *a, **k: calls.append("exit")
    for action in sorted(FOLLOW_LOCKED_ACTIONS):
        sm.perform_action(action, source="midi")
    assert calls == []
    assert sorted(blocked) == sorted(FOLLOW_LOCKED_ACTIONS)
    sm.perform_action("add_marker", source="midi")       # not transport: allowed
    assert len(sm.markers) == 3


def test_mirror_applier_still_drives_a_following_backup(following):
    sm, blocked = following
    with sm._mirror_quiet():
        assert sm.jump_to_next_marker() is True
    sm.mirror._apply_action({"kind": "queue_exit", "fade": False}, 0.0)
    assert blocked == []


def test_main_or_unfollowing_backup_is_not_blocked(loaded):
    blocked = []
    loaded.on('blocked_following', lambda what: blocked.append(what))
    assert loaded.jump_to_next_marker() is True
    assert loaded.user_seek(1.0) is True
    assert blocked == []


def test_preferences_change_token_moves_on_every_save():
    first = preferences.change_token()
    preferences.save_preferences({"x": 1})
    second = preferences.change_token()
    assert second != first
    preferences.add_recent_show("folder", os.getcwd())
    assert preferences.change_token() != second
