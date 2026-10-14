"""Tests for backend/state_manager.py against the in-memory FakeEngine (tests/fake_engine.py)."""
import glob
import json
import os
import threading
import time

import pytest

from backend import state_manager
from backend.state_manager import (
    StateManager, PlaybackState, LoopRegion, HoldCue, SongSettings,
)
from utils import preferences

from fake_engine import FakeEngine
from helpers_audio import make_signal, write_wav, wait_until


# ---------------------------------------------------------------- fixtures --

class Recorder:
    """Collects emitted events as (name, args) tuples."""

    def __init__(self, sm, *events):
        self.items = []
        self._lock = threading.Lock()
        for name in events:
            sm.on(name, self._make(name))

    def _make(self, name):
        def cb(*args):
            with self._lock:
                self.items.append((name, args))
        return cb

    def names(self):
        with self._lock:
            return [n for n, _ in self.items]

    def count(self, name):
        return self.names().count(name)

    def args(self, name):
        with self._lock:
            return [a for n, a in self.items if n == name]


class FakeOscClient:
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.sent = []

    def send_message(self, address, value):
        self.sent.append((address, list(value)))


@pytest.fixture(autouse=True)
def prefs_file(tmp_path, monkeypatch):
    """Never touch the real user preferences."""
    path = str(tmp_path / "prefs" / "user_preferences.json")
    os.makedirs(os.path.dirname(path))
    monkeypatch.setattr(preferences, "PREFS_FILE", path)
    return path


@pytest.fixture
def data_file(tmp_path):
    return str(tmp_path / "data" / "loop_data.json")


@pytest.fixture
def song(tmp_path):
    return write_wav(tmp_path / "song.wav", make_signal(10.0, sr=8000), sr=8000)


@pytest.fixture
def song_b(tmp_path):
    return write_wav(tmp_path / "other.wav", make_signal(5.0, sr=8000, freq=330.0), sr=8000)


@pytest.fixture
def fake():
    return FakeEngine()


def _make_sm(data_file, fake, monkeypatch, threaded=False):
    if not threaded:
        monkeypatch.setattr(StateManager, "_start_monitor", lambda self: None)
    manager = StateManager(ffmpeg_path="no-ffmpeg", data_file=data_file, engine=fake)
    return manager


def _teardown(manager):
    manager.stop()
    manager.cleanup()
    thread = manager._monitor_thread
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=2.0)


@pytest.fixture
def sm(data_file, fake, monkeypatch):
    """StateManager with the monitor thread disabled: tests drive ticks themselves."""
    manager = _make_sm(data_file, fake, monkeypatch)
    yield manager
    _teardown(manager)


@pytest.fixture
def sm_threaded(data_file, fake, monkeypatch):
    manager = _make_sm(data_file, fake, monkeypatch, threaded=True)
    yield manager
    _teardown(manager)


@pytest.fixture
def loaded(sm, song):
    assert sm.load_song(song)
    return sm


def _tick(sm):
    sm._monitor_tick(emit=True)


# ------------------------------------------------------------ persistence --

def test_save_before_song_loaded_is_safe(sm):
    assert sm.current_song_id is None
    sm.save_loop()          # must not raise
    sm.flush_save()


def test_save_is_debounced_and_atomic(loaded, data_file):
    loaded.add_loop(0.5, 1.5, name="V1")
    assert not os.path.exists(data_file)           # debounced, not written yet
    assert wait_until(lambda: os.path.exists(data_file), timeout=2.0)
    assert glob.glob(data_file + ".tmp*") == []    # temp file replaced away

    with open(data_file, encoding="utf-8") as f:
        data = json.load(f)
    entry = data[loaded.current_song_id]
    assert entry["loops"][0]["name"] == "V1"
    assert entry["loops"][0]["start"] == 0.5
    assert entry["holds"] == []
    assert entry["settings"]["tempo_ratio"] == 1.0

    # stop() flushes immediately
    loaded.add_marker(2.0, name="Cue A")
    loaded.add_hold(3.0, name="Caesura")
    loaded.stop()
    with open(data_file, encoding="utf-8") as f:
        saved = json.load(f)[loaded.current_song_id]
    assert saved["markers"][0]["name"] == "Cue A"
    assert saved["holds"][0]["name"] == "Caesura" and saved["holds"][0]["time"] == 3.0


def test_corrupt_data_file_is_moved_aside(data_file, fake, monkeypatch):
    os.makedirs(os.path.dirname(data_file))
    with open(data_file, "w") as f:
        f.write("{this is not json")

    sm = _make_sm(data_file, fake, monkeypatch)
    try:
        assert sm.loop_data == {}
        assert sm.last_load_error and "unreadable" in sm.last_load_error
        assert not os.path.exists(data_file)
        corrupt = glob.glob(data_file + ".corrupt-*")
        assert len(corrupt) == 1
        with open(corrupt[0]) as f:
            assert f.read() == "{this is not json"
    finally:
        sm.cleanup()


def test_corrupt_song_entry_falls_back_to_empty(sm, song):
    rec = Recorder(sm, "data_load_failed", "song_loaded")
    fingerprint = sm._get_file_fingerprint(song)
    sm.loop_data[fingerprint] = {"loops": [{"bogus": 1}]}

    assert sm.load_song(song) is True
    assert sm.loops == [] and sm.markers == [] and sm.skips == [] and sm.holds == []
    assert rec.count("data_load_failed") == 1
    assert rec.count("song_loaded") == 1
    assert sm.last_load_error


def test_load_song_clears_skips_and_emits_copies(loaded, song_b):
    loaded.add_skip(1.0, 1.5)
    loaded.add_loop(0.5, 1.5)
    loaded.add_hold(2.0)
    rec = Recorder(loaded, "skips_changed", "loops_changed", "markers_changed", "holds_changed")

    assert loaded.load_song(song_b)
    assert loaded.skips == [] and loaded.loops == [] and loaded.holds == []
    assert rec.args("skips_changed")[-1] == ([],)
    assert rec.args("holds_changed")[-1] == ([],)

    # Event payloads are copies, not the live lists
    loaded.add_loop(0.2, 1.0)
    payload = rec.args("loops_changed")[-1][0]
    assert payload == loaded.loops and payload is not loaded.loops
    payload.clear()
    assert len(loaded.loops) == 1


def test_concurrent_loads_drop_stale_events(sm, song, song_b):
    rec = Recorder(sm, "song_loaded")
    results = {}

    def load(path, key):
        results[key] = sm.load_song(path)

    t1 = threading.Thread(target=load, args=(song, "a"))
    t2 = threading.Thread(target=load, args=(song_b, "b"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Exactly one load wins and only its song_loaded event is emitted
    assert sorted(results.values()) == [False, True]
    assert rec.count("song_loaded") == 1
    winner = os.path.basename(song if results["a"] else song_b)
    assert rec.args("song_loaded")[0][0] == winner
    assert sm.current_song_name == winner


def test_settings_and_holds_round_trip(loaded, song, song_b, fake):
    rec = Recorder(loaded, "settings_changed", "holds_changed")
    hold = loaded.add_hold(4.0, name="Breath")
    loaded.set_item_tag_note(hold.id, "Director", "wait for the laugh")
    loaded.set_song_tempo_map(120.0, 3, 0.25)
    loaded.set_tempo_pitch(0.9, -2.0)
    loaded.set_stem_gain("mix", -3.0)
    loaded.set_stem_mute("mix", True)
    loaded.set_routing("click_right")
    loaded.set_song_osc_cue("42")
    loop = loaded.add_loop(1.0, 2.0)
    loaded.update_selected_loop(repeat_count=3)
    assert rec.count("settings_changed") >= 5
    assert rec.args("settings_changed")[-1][0] == loaded.settings
    assert rec.args("settings_changed")[-1][0] is not loaded.settings   # copy
    assert fake.calls_to("set_tempo_pitch")[-1][1] == (0.9, -2.0)

    assert loaded.load_song(song_b)
    assert loaded.settings == SongSettings()
    fake.calls.clear()

    assert loaded.load_song(song)
    s = loaded.settings
    assert (s.tempo_ratio, s.semitones, s.bpm, s.beats_per_bar, s.downbeat_offset) == (0.9, -2.0, 120.0, 3, 0.25)
    assert s.stems["mix"] == {"gain_db": -3.0, "muted": True, "solo": False}
    assert s.routing_mode == "click_right" and s.osc_cue == "42"
    assert [h.name for h in loaded.holds] == ["Breath"]
    assert loaded.holds[0].tag_notes == {"Director": "wait for the laugh"}
    assert loaded.holds[0].id == hold.id
    assert loaded.loops[0].repeat_count == 3 and loaded.loops[0].id == loop.id

    # Settings were re-applied to the engine after the load
    assert fake.calls_to("set_tempo_pitch")[-1][1] == (0.9, -2.0)
    assert fake.calls_to("set_stem_gain")[-1][1] == ("mix", -3.0)
    assert fake.calls_to("set_stem_mute")[-1][1] == ("mix", True)
    assert fake.calls_to("set_routing")[-1][1] == ("click_right",)


def test_hold_cue_and_settings_serialisation():
    hold = HoldCue(2.5, name="H")
    hold.tag_notes["Tech"] = "spot 3"
    hold.osc_cue = "7"
    restored = HoldCue.from_dict(json.loads(json.dumps(hold.to_dict())))
    assert (restored.id, restored.time, restored.name, restored.tag_notes, restored.osc_cue) == \
        (hold.id, 2.5, "H", {"Tech": "spot 3"}, "7")
    assert HoldCue.from_dict({"time": 1.0, "tags": ["Sound"], "notes": "x"}).tag_notes == {"Sound": "x"}

    s = SongSettings(tempo_ratio=1.1, routing_mode="bogus", stems={"click": {"muted": True}})
    assert s.routing_mode == "stereo"
    assert s.stems == {"click": {"gain_db": 0.0, "muted": True, "solo": False}}
    assert SongSettings.from_dict(s.to_dict()) == s
    assert SongSettings.from_dict(None) == SongSettings()


# ----------------------------------------------- update_selected_loop API --

def test_update_selected_loop_contract(loaded):
    loop = loaded.add_loop(0.5, 1.5, name="V1")
    rec = Recorder(loaded, "loops_changed", "loop_points_changed")

    ok = loaded.update_selected_loop(start=0.6, end=1.8, crossfade_ms=40,
                                     early_switch_ms=80, exit_fade_ms=900, name="Renamed",
                                     repeat_count=2)
    assert ok
    assert (loop.start, loop.end) == (0.6, 1.8)
    assert loop.crossfade_ms == 40 and loop.early_switch_ms == 80 and loop.exit_fade_ms == 900
    assert loop.name == "Renamed" and loop.repeat_count == 2
    assert (loaded.loop_start, loaded.loop_end) == (0.6, 1.8)
    assert rec.count("loops_changed") == 1
    assert rec.count("loop_points_changed") == 1
    assert rec.args("loop_points_changed")[0] == (0.6, 1.8)
    # Pushed to the engine (playback is stopped) with the crossfade
    assert loaded.audio.get_loop_points() == (0.6, 1.8, 40)

    # repeat_count 0 means indefinite
    assert loaded.update_selected_loop(repeat_count=0)
    assert loop.repeat_count is None

    # Inverted edits are rejected without side effects
    assert loaded.update_selected_loop(start=1.9) is False
    assert loop.start == 0.6

    # With nothing selected, start+end creates a vamp
    loaded.loops = []
    loaded.selected_loop_index = -1
    assert loaded.update_selected_loop(start=2.0, end=2.5) is True
    assert len(loaded.loops) == 1 and loaded.loops[0].end == 2.5


def test_legacy_edit_paths_route_through_update(loaded):
    loaded.add_loop(1.0, 2.0)
    rec = Recorder(loaded, "loops_changed", "loop_points_changed")
    loaded.adjust_loop_in(-0.25)
    loaded.adjust_loop_out(0.5)
    loaded.set_loop_points(0.5, 1.5)
    assert (loaded.loops[0].start, loaded.loops[0].end) == (0.5, 1.5)
    assert rec.count("loops_changed") == 3
    assert rec.count("loop_points_changed") == 3
    assert loaded.audio.get_loop_points()[:2] == (0.5, 1.5)
    assert loaded.add_loop(2.0, 2.0) is None  # zero-length guard


def test_ui_does_not_push_engine_points_while_monitor_owns_them(loaded):
    first = loaded.add_loop(0.5, 1.5)
    second = loaded.add_loop(2.0, 2.8)
    assert loaded.audio.get_loop_points()[:2] == (2.0, 2.8)

    # Simulate: playing, monitor has armed `first`
    loaded.state = PlaybackState.PLAYING
    loaded._armed_loop_id = first.id

    loaded.select_loop(1)
    loaded.update_selected_loop(start=2.1)          # second: not armed -> no push
    assert loaded.audio.get_loop_points()[:2] == (2.0, 2.8)

    loaded.select_loop(0)
    loaded.update_selected_loop(end=1.4)            # first: armed -> pushed
    assert loaded.audio.get_loop_points()[:2] == (0.5, 1.4)

    loaded.state = PlaybackState.STOPPED
    loaded.select_loop(1)                            # stopped -> pushed
    assert loaded.audio.get_loop_points()[:2] == (2.1, 2.8)
    assert second.start == 2.1


# ---------------------------------------------------------- engine arming --

def test_play_arms_nearest_vamp_hold_and_skips(loaded, fake):
    later = loaded.add_loop(6.0, 7.0, name="Later")
    loop = loaded.add_loop(2.0, 4.0, name="Soon")
    loaded.update_selected_loop(crossfade_ms=30, entry_fade_ms=25, repeat_count=2)
    loaded.add_hold(5.0, name="H1")
    loaded.add_hold(8.0, name="H2")
    skip = loaded.add_skip(7.5, 8.0)
    loaded.add_skip(8.5, 9.0)
    loaded.toggle_skip_active(skip.id)      # inactive: must not be sent

    loaded.play()
    assert loaded.state == PlaybackState.PLAYING
    assert fake.armed_loop == {"start": 2.0, "end": 4.0, "cf": 30, "count": 1, "entry": 25}
    assert loaded._armed_loop_id == loop.id
    assert fake.armed_hold == 5.0
    assert fake.skip_regions == [(8.5, 9.0)]

    # Nothing is re-sent while nothing changes
    n_arm, n_hold, n_skip = (len(fake.calls_to(c)) for c in ("arm_loop", "hold_at", "set_skip_regions"))
    for _ in range(5):
        _tick(loaded)
    assert (len(fake.calls_to("arm_loop")), len(fake.calls_to("hold_at")),
            len(fake.calls_to("set_skip_regions"))) == (n_arm, n_hold, n_skip)

    # Editing the armed vamp re-arms with the new values on the next tick
    loaded.update_selected_loop(end=4.5)
    _tick(loaded)
    assert fake.armed_loop["end"] == 4.5 and len(fake.calls_to("arm_loop")) == n_arm + 1

    # Skip toggles reach the engine; a later vamp is armed once the first is passed
    loaded.toggle_skip_active(skip.id)
    _tick(loaded)
    assert fake.skip_regions == [(7.5, 8.0), (8.5, 9.0)]
    loaded.temp_skip_loop_id = loop.id
    _tick(loaded)
    assert fake.armed_loop["start"] == 6.0 and loaded._armed_loop_id == later.id


def test_vamp_entry_cycles_and_count_exit(loaded, fake):
    loop = loaded.add_loop(2.0, 3.0, name="V")
    loaded.update_selected_loop(repeat_count=3)     # heard 3 times = 2 wraps
    rec = Recorder(loaded, "loop_mode_enter", "loop_mode_exit", "loop_cycle",
                   "loop_skip_cleared", "position_update")

    loaded.play_from(2.5)
    fake.advance(0.5)                                # reaches loop_out -> loop mode
    assert loaded.is_in_loop_mode()
    assert rec.count("loop_mode_enter") == 1
    assert rec.args("loop_cycle") == [(2, 3)]
    assert loaded._looping_loop_id == loop.id
    _tick(loaded)
    assert rec.args("position_update")[-1] == (2.0, True)

    fake.advance(1.0)                                # second cycle
    assert rec.args("loop_cycle") == [(2, 3), (3, 3)]
    assert loaded.standby()["action"] == "exit_vamp"

    fake.advance(1.0)                                # count reached -> engine exits
    assert not loaded.is_in_loop_mode()
    assert rec.args("loop_mode_exit") == [(3.0,)]
    assert loaded.state == PlaybackState.PLAYING
    assert loaded.temp_skip_loop_id == loop.id       # not re-entered right away
    _tick(loaded)
    assert fake.armed_loop is None

    fake.advance(2.1)                                # well past: flag released, re-armable
    _tick(loaded)
    assert loaded.temp_skip_loop_id is None
    assert rec.count("loop_skip_cleared") == 1


def test_queue_exit_cut_resumes_transport(loaded, fake):
    loop = loaded.add_loop(0.5, 1.5)
    rec = Recorder(loaded, "loop_mode_enter", "loop_mode_exit", "song_ended", "standby_changed")
    loaded.play_from(1.3)
    fake.advance(0.3)
    assert rec.count("loop_mode_enter") == 1

    loaded.queue_exit(fade_mode=False)
    assert loaded.exit_queue_active and loaded.temp_skip_loop_id == loop.id
    assert fake.calls_to("queue_loop_exit")[-1][1] == ("cut",)
    assert loaded.standby()["action"] == "none"      # already exiting

    fake.advance(5.0)                                # wraps once more, then continues
    assert rec.args("loop_mode_exit") == [(1.5,)]
    assert loaded.state == PlaybackState.PLAYING
    assert fake.mode == "transport" and fake.get_position() > 1.5
    assert "song_ended" not in rec.names()
    assert not loaded.exit_queue_active


def test_queue_exit_fade_stops_without_song_ended(loaded, fake):
    loop = loaded.add_loop(0.5, 1.5, name="Vamp")
    loaded.update_selected_loop(exit_fade_ms=400)
    rec = Recorder(loaded, "loop_mode_enter", "loop_mode_exit", "state_change", "song_ended")

    loaded.play_from(1.3)
    fake.advance(0.3)
    assert loaded.is_in_loop_mode()

    loaded.queue_exit(fade_mode=True)                # fade_ms from the vamp (400)
    assert loaded.exit_fade_ms == 400
    assert loaded.temp_skip_loop_id == loop.id
    assert fake.calls_to("queue_loop_exit")[-1] == ("queue_loop_exit", ("fade",), {"fade_ms": 400})

    fake.advance(0.39)
    assert loaded.state == PlaybackState.PLAYING     # fade still running
    fake.advance(0.02)
    assert loaded.state == PlaybackState.STOPPED
    assert "song_ended" not in rec.names()
    assert rec.args("loop_mode_exit") == [(1.5,)]
    assert rec.args("state_change")[-1] == (PlaybackState.STOPPED,)
    assert loaded.temp_skip_loop_id is None
    assert fake.mode == "transport" and not fake.is_playing


def test_queue_exit_in_transport_skips_upcoming_vamp(loaded, fake):
    loop = loaded.add_loop(1.0, 2.0, name="Scene 2")
    rec = Recorder(loaded, "loop_skip_queued", "loop_skip_cleared")
    loaded.play_from(0.2)
    assert fake.armed_loop is not None
    loaded.queue_exit()
    assert loaded.temp_skip_loop_id == loop.id
    assert fake.armed_loop is None                   # disarmed immediately
    assert rec.args("loop_skip_queued") == [("Scene 2",)]

    # Forward seek that stays near the vamp keeps the skip ...
    loaded.seek(0.8)
    assert loaded.temp_skip_loop_id == loop.id
    assert fake.armed_loop is None
    # ... a backwards seek re-arms it
    loaded.seek(0.1)
    assert loaded.temp_skip_loop_id is None
    assert rec.count("loop_skip_cleared") == 1
    assert fake.armed_loop["end"] == 2.0


def test_seek_cancels_queued_exit(loaded):
    rec = Recorder(loaded, "loop_skip_cleared")
    loaded.exit_queue_active = True
    loaded.exit_fade_mode = True
    loaded._prev_cycle_pos = 0.7
    loaded.seek(1.0)
    assert not loaded.exit_queue_active and not loaded.exit_fade_mode
    assert loaded._prev_cycle_pos == 0.0
    assert rec.count("loop_skip_cleared") == 1
    assert loaded.state == PlaybackState.PAUSED


def test_seek_exits_loop_and_rearms(loaded, fake):
    loaded.add_loop(2.0, 3.0)
    rec = Recorder(loaded, "loop_mode_exit")
    loaded.play_from(2.9)
    fake.advance(0.2)
    assert loaded.is_in_loop_mode()
    loaded.seek(1.0)
    assert rec.args("loop_mode_exit") == [(3.0,)]
    assert not loaded.is_in_loop_mode()
    assert loaded.state == PlaybackState.PLAYING
    assert fake.armed_loop["start"] == 2.0           # no skip flag after a seek


def test_song_end_emits_song_ended(loaded, fake):
    rec = Recorder(loaded, "song_ended", "state_change")
    loaded.play_from(loaded.song_length - 0.3)
    fake.advance(0.5)
    assert rec.count("song_ended") == 1
    assert loaded.state == PlaybackState.STOPPED
    assert rec.args("state_change")[-1] == (PlaybackState.STOPPED,)


def test_skip_regions_execute_in_engine(loaded, fake):
    loaded.add_skip(1.0, 1.5)
    loaded.add_skip(1.5, 2.0)
    loaded.play_from(0.5)
    fake.advance(1.0)                                # 0.5 s played + both regions jumped
    assert fake.get_position() == pytest.approx(2.5)
    assert [n for n in fake.event_names() if n == "skip_executed"] == ["skip_executed"] * 2


def test_pause_resume_and_paused_seek(loaded, fake):
    rec = Recorder(loaded, "position_update", "song_ended")
    loaded.seek(1.0)
    assert loaded.state == PlaybackState.PAUSED
    assert rec.args("position_update")[-1] == (1.0, False)
    loaded.play()
    assert fake.is_playing and not fake.is_paused and fake.get_position() == 1.0
    loaded.pause()
    assert loaded.state == PlaybackState.PAUSED and fake.is_paused
    fake.advance(1.0)                                # nothing moves while paused
    assert fake.get_position() == 1.0
    loaded.toggle_play_pause()
    assert loaded.state == PlaybackState.PLAYING and not fake.is_paused
    assert rec.count("song_ended") == 0


# ------------------------------------------------------------------ holds --

def test_hold_flow_arm_reach_go_release(loaded, fake):
    hold = loaded.add_hold(3.0, name="Caesura")
    rec = Recorder(loaded, "hold_reached", "hold_released", "state_change", "standby_changed")
    loaded.play_from(2.5)
    assert fake.armed_hold == 3.0

    fake.advance(1.0)
    assert fake.get_position() == 3.0 and fake.is_held
    assert loaded.state == PlaybackState.HELD
    assert rec.args("hold_reached") == [(hold,)]
    assert not loaded.is_playing() and loaded.is_held()
    sb = loaded.standby()
    assert sb["action"] == "release_hold" and sb["next_item"] is hold and sb["next_type"] == "hold"
    assert "Caesura" in sb["label"]
    _tick(loaded)                                    # must not re-arm or clear the hold
    assert fake.calls_to("clear_hold") == []

    assert loaded.go() is True
    assert fake.calls_to("release_hold") and not fake.is_held
    assert loaded.state == PlaybackState.PLAYING
    assert rec.count("hold_released") == 1
    assert rec.args("state_change")[-2:] == [(PlaybackState.HELD,), (PlaybackState.PLAYING,)]

    fake.advance(0.5)                                # continues; the hold is not re-armed
    _tick(loaded)
    assert fake.get_position() == pytest.approx(3.5)
    assert fake.armed_hold is None
    assert loaded.standby()["action"] == "none"


def test_play_and_seek_release_a_hold(loaded, fake):
    loaded.add_hold(1.0)
    loaded.add_hold(2.0, name="Second")
    loaded.play_from(0.5)
    fake.advance(1.0)
    assert loaded.state == PlaybackState.HELD
    loaded.play()                                    # play/space while HELD releases
    assert loaded.state == PlaybackState.PLAYING and not fake.is_held
    _tick(loaded)
    assert fake.armed_hold == 2.0                    # next hold armed after release

    fake.advance(1.0)
    assert loaded.state == PlaybackState.HELD
    loaded.seek(0.2)                                 # seeking out of a hold keeps playing
    assert loaded.state == PlaybackState.PLAYING and not fake.is_held
    assert fake.armed_hold == 1.0


def test_hold_management_and_timeline(loaded):
    rec = Recorder(loaded, "holds_changed")
    b = loaded.add_hold(5.0, name="B")
    a = loaded.add_hold(1.0)
    assert [h.name for h in loaded.holds] == ["Hold 2", "B"]   # sorted by time
    assert loaded.rename_hold(a.id, "A") and a.name == "A"
    assert loaded.move_hold(b.id, 0.5) and [h.id for h in loaded.holds] == [b.id, a.id]
    loaded.add_marker(2.0, name="M")
    loaded.add_loop(3.0, 4.0, name="V")
    items = loaded.get_timeline_items()
    assert [(t, k, o.name) for t, k, o in items] == [
        (0.5, "hold", "B"), (1.0, "hold", "A"), (2.0, "marker", "M"), (3.0, "vamp", "V")]
    assert loaded.set_item_tag_note(a.id, "Stage", "hold the curtain")
    assert a.tag_notes == {"Stage": "hold the curtain"}
    assert loaded.remove_item_tag(a.id, "Stage") and a.tag_notes == {}
    assert loaded.delete_hold(b.id) and loaded.delete_hold("nope") is False
    assert [h.id for h in loaded.holds] == [a.id]
    payload = rec.args("holds_changed")[-1][0]
    assert payload == loaded.holds and payload is not loaded.holds


# --------------------------------------------------------------------- GO --

def test_go_in_each_state_with_debounce_and_log(loaded, fake, data_file):
    loop = loaded.add_loop(2.0, 3.0, name="V")
    loaded.add_marker(5.0, name="M")
    rec = Recorder(loaded, "go_fired")
    log_file = os.path.join(os.path.dirname(data_file), "go_log.jsonl")

    # Stopped -> play
    sb = loaded.standby()
    assert sb["action"] == "play" and sb["next_item"] is loop and sb["next_type"] == "vamp"
    assert loaded.go("ui") is True
    assert loaded.state == PlaybackState.PLAYING and fake.is_playing
    assert loaded.go("ui") is False                  # debounce
    assert len(loaded.go_log) == 1 and loaded.go_log[0]["action"] == "play"
    assert loaded.go_log[0]["source"] == "ui" and loaded.go_log[0]["position"] == 0.0

    # Playing in transport: nothing to do, nothing logged
    loaded._last_go_time = -1e9
    assert loaded.standby()["action"] == "none"
    assert loaded.go() is False
    assert len(loaded.go_log) == 1

    # Paused -> resume
    loaded.pause()
    loaded._last_go_time = -1e9
    assert loaded.standby()["label"] == "GO: resume"
    assert loaded.go("midi") is True
    assert loaded.state == PlaybackState.PLAYING and not fake.is_paused

    # In a vamp -> cut exit queued
    fake.advance(3.0)
    assert loaded.is_in_loop_mode()
    loaded._last_go_time = -1e9
    assert loaded.standby()["action"] == "exit_vamp"
    assert loaded.go("web") is True
    assert fake.calls_to("queue_loop_exit")[-1][1] == ("cut",)
    assert loaded.standby()["action"] == "none"

    assert [e["action"] for e in loaded.go_log] == ["play", "play", "exit_vamp"]
    assert [a[0]["source"] for a in rec.args("go_fired")] == ["ui", "midi", "web"]
    with open(log_file, encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert [e["action"] for e in lines] == ["play", "play", "exit_vamp"]
    assert lines[-1]["ts"] > 0 and "label" in lines[-1]


def test_standby_changed_emitted_only_on_change(loaded, fake):
    loaded.add_loop(2.0, 3.0, name="V")
    rec = Recorder(loaded, "standby_changed")
    loaded.play()
    assert rec.args("standby_changed")[-1][0]["label"] == "Next: V"
    n = rec.count("standby_changed")
    for _ in range(3):
        _tick(loaded)
    assert rec.count("standby_changed") == n          # unchanged -> not re-emitted
    fake.advance(3.0)
    assert rec.args("standby_changed")[-1][0]["action"] == "exit_vamp"
    loaded.stop()
    assert rec.args("standby_changed")[-1][0]["action"] == "play"
    payload = rec.args("standby_changed")[-1][0]
    assert set(payload) == {"action", "label", "next_item", "next_type"}


# -------------------------------------------------------------- live mode --

def test_live_mode_blocks_edits_but_not_playback(loaded, fake, song_b):
    loop = loaded.add_loop(1.0, 2.0, name="V")
    marker = loaded.add_marker(3.0)
    hold = loaded.add_hold(4.0)
    skip = loaded.add_skip(5.0, 6.0)
    rec = Recorder(loaded, "live_mode_changed", "blocked_in_live_mode", "loops_changed")
    loaded.set_live_mode(True)
    loaded.set_live_mode(True)
    assert loaded.live_mode and rec.args("live_mode_changed") == [(True,)]

    assert loaded.add_loop(7.0, 8.0) is None
    assert loaded.delete_selected_loop() is False
    assert loaded.rename_loop(0, "X") is False
    assert loaded.update_selected_loop(end=2.5) is False
    assert loaded.add_marker(3.5) is None
    assert loaded.rename_marker(marker.id, "Y") is False
    assert loaded.delete_marker(marker.id) is False
    assert loaded.add_hold(4.5) is None
    assert loaded.rename_hold(hold.id, "Z") is False
    assert loaded.delete_hold(hold.id) is False
    assert loaded.add_skip(8.0, 9.0) is None
    assert loaded.delete_skip(skip.id) is False
    assert loaded.toggle_skip_active(skip.id) is False
    assert loaded.load_song(song_b) is False
    assert loaded.set_tempo_pitch(1.2, 0) is False
    assert loop.name == "V" and loop.end == 2.0 and marker.name == "Cue 1"
    assert len(loaded.loops) == 1 and len(loaded.holds) == 1 and len(loaded.skips) == 1
    assert rec.count("blocked_in_live_mode") == 15
    assert ("blocked_in_live_mode", ("load_song",)) in rec.items
    assert rec.count("loops_changed") == 0

    # Playback and GO keep working
    assert loaded.go() is True and loaded.state == PlaybackState.PLAYING
    loaded.seek(1.5)
    loaded.pause()
    loaded.stop()
    assert loaded.state == PlaybackState.STOPPED

    loaded.set_live_mode(False)
    assert rec.args("live_mode_changed") == [(True,), (False,)]
    assert loaded.rename_loop(0, "X") is True


# -------------------------------------------------------------- tempo map --

def test_bar_beat_at_math(loaded):
    assert loaded.bar_beat_at(10.0) == (0, 0, 0.0)             # no BPM set
    loaded.set_song_tempo_map(120.0, 4, 1.0)
    assert loaded.bar_beat_at(1.0) == (1, 1, 0.0)
    assert loaded.bar_beat_at(1.5) == (1, 2, 0.0)
    assert loaded.bar_beat_at(3.0) == (2, 1, 0.0)
    bar, beat, frac = loaded.bar_beat_at(3.25)
    assert (bar, beat) == (2, 1) and frac == pytest.approx(0.5)
    assert loaded.bar_beat_at(4.75)[:2] == (2, 4)
    assert loaded.bar_beat_at(0.0) == (0, 3, 0.0)              # before the downbeat
    loaded.set_song_tempo_map(60.0, 3, 0.0)
    assert loaded.bar_beat_at(7.0) == (3, 2, 0.0)
    assert loaded.settings.beats_per_bar == 3


def test_estimate_tempo_runs_in_worker_and_emits(loaded, monkeypatch):
    seen = {}

    def fake_compute(samples, sr):
        seen["frames"] = len(samples)
        seen["sr"] = sr
        seen["thread"] = threading.current_thread().name
        return 123.0, 0.75

    monkeypatch.setattr(state_manager, "_compute_tempo", fake_compute)
    rec = Recorder(loaded, "tempo_estimated")
    assert loaded.estimate_tempo(wait=True) == (123.0, 0.75)
    assert rec.args("tempo_estimated") == [(123.0, 0.75)]
    assert seen["thread"] == "tempo-estimate" and seen["sr"] == 8000
    assert seen["frames"] == 8000 * 10                           # whole 10 s song (< 60 s)

    assert loaded.estimate_tempo() is None                     # background mode
    assert wait_until(lambda: rec.count("tempo_estimated") == 2, timeout=2.0)


def test_estimate_tempo_without_audio(sm):
    assert sm.estimate_tempo(wait=True) is None


# ------------------------------------------------------------------ stems --

def test_stems_discovery_and_load(sm, tmp_path, fake):
    song = write_wav(tmp_path / "act1.wav", make_signal(2.0, sr=8000), sr=8000)
    stems_dir = tmp_path / "act1.stems"
    stems_dir.mkdir()
    (stems_dir / "drums.wav").write_bytes(b"x")
    (stems_dir / "Click Track.flac").write_bytes(b"x")
    (stems_dir / "notes.txt").write_bytes(b"x")
    (tmp_path / "act1 - bass.mp3").write_bytes(b"x")
    (tmp_path / "act1 - .wav").write_bytes(b"x")
    (tmp_path / "act2 - keys.wav").write_bytes(b"x")

    found = StateManager.discover_stems(song)
    assert set(found) == {"drums", "click", "bass"}
    assert found["click"].endswith("Click Track.flac")
    assert found["bass"] == str(tmp_path / "act1 - bass.mp3")
    assert StateManager.discover_stems(str(tmp_path / "missing.wav")) == {}

    rec = Recorder(sm, "stems_changed")
    assert sm.load_song(song)
    assert fake.calls_to("load_file")[-1][1] == (song, found)
    assert [s.name for s in sm.stems] == ["mix", "click", "drums", "bass"]
    assert [s.name for s in rec.args("stems_changed")[-1][0]] == ["mix", "click", "drums", "bass"]

    assert sm.set_stem_solo("click", True)
    assert fake.calls_to("set_stem_solo")[-1][1] == ("click", True)
    assert sm.stems[1].solo is True
    assert sm.settings.stems["click"]["solo"] is True
    assert sm.set_stem_gain("nope", 1.0) is False              # unknown stem: engine refuses


def test_routing_and_output_device(loaded, fake, prefs_file):
    assert loaded.set_routing("multichannel") and loaded.get_routing()["mode"] == "multichannel"
    assert loaded.set_routing("bogus") is False
    assert loaded.list_output_devices()[0]["name"] == "Fake Output"
    assert loaded.set_output_device(0) and fake.get_output_device()["index"] == 0
    assert preferences.load_preferences()["audio"]["output_device"] == 0
    rec = Recorder(loaded, "render_state")
    loaded.set_tempo_pitch(1.1, 2)
    assert rec.args("render_state") == [("rendering", "tempo 1.1 / 2.0 st"), ("done", "")]
    assert loaded.get_tempo_pitch() == (1.1, 2.0)


# ------------------------------------------------------------------ preview --

def test_preview_loop_does_not_touch_vamps_and_restores_engine(loaded, fake):
    loop = loaded.add_loop(0.5, 1.5)
    rec = Recorder(loaded, "preview_changed", "loops_changed")
    before = list(loaded.loops)
    loops_changed_before = rec.count("loops_changed")

    loaded.preview_loop(2.0, 2.8)
    assert loaded.is_previewing()
    assert loaded.state == PlaybackState.PLAYING
    assert loaded.audio.get_loop_points()[:2] == (2.0, 2.8)
    assert fake.armed_loop["start"] == 2.0 and fake.armed_loop["count"] is None
    assert loaded.loops == before and (loop.start, loop.end) == (0.5, 1.5)
    assert loaded._armed_loop_id is None

    # The monitor must not arm a vamp while previewing
    _tick(loaded)
    assert loaded.audio.get_loop_points()[:2] == (2.0, 2.8)
    assert fake.mode == "transport"

    loaded.stop_preview()
    assert not loaded.is_previewing()
    assert loaded.state == PlaybackState.STOPPED
    assert loaded.audio.get_loop_points()[:2] == (0.5, 1.5)
    assert loaded._armed_loop_id == loop.id
    assert rec.args("preview_changed") == [(True,), (False,)]
    assert rec.count("loops_changed") == loops_changed_before
    loaded.stop_preview()  # idempotent


def test_preview_enters_loop_and_exit_restores(loaded, fake):
    loaded.add_loop(0.5, 1.0)
    rec = Recorder(loaded, "loop_mode_enter", "loop_mode_exit", "preview_changed")
    loaded.preview_loop(1.8, 2.6)
    fake.advance(0.8)
    assert rec.count("loop_mode_enter") == 1 and loaded.is_in_loop_mode()
    assert 1.8 <= loaded.get_position() < 2.6
    assert loaded.standby()["label"] == "GO: exit preview"

    loaded.queue_exit()
    assert loaded.temp_skip_loop_id is None          # previews never set the skip flag
    fake.advance(1.0)
    assert rec.args("loop_mode_exit") == [(2.6,)]
    assert not loaded.is_previewing() and loaded.state == PlaybackState.PLAYING
    assert loaded.audio.get_loop_points()[:2] == (0.5, 1.0)
    assert rec.args("preview_changed") == [(True,), (False,)]


# ------------------------------------------------------------- detection --

class _SlowDetector:
    delay = 0.3

    def __init__(self, raw, sr):
        pass

    def find_loops(self, start, end):
        time.sleep(self.delay)
        return ["loop-result"]

    def find_smart_cuts(self, start, end):
        time.sleep(self.delay)
        return ["cut-result"]


def test_stale_detection_results_are_dropped(loaded, monkeypatch):
    monkeypatch.setattr(state_manager, "LoopDetector", _SlowDetector)
    rec = Recorder(loaded, "detection_started", "detection_complete", "cut_detection_complete")

    loaded.run_loop_detection(0.0, 2.0)
    loaded.cancel_detection()
    time.sleep(0.6)
    assert rec.count("detection_complete") == 0

    loaded.run_smart_cut_detection(0.0, 2.0)
    assert wait_until(lambda: rec.count("cut_detection_complete") == 1, timeout=2.0)
    assert rec.args("cut_detection_complete") == [(["cut-result"],)]

    # A superseding run drops the earlier one's result
    loaded.run_loop_detection(0.0, 1.0)
    loaded.run_loop_detection(0.0, 2.0)
    time.sleep(0.8)
    assert rec.count("detection_complete") == 1
    assert rec.count("detection_started") == 4


# --------------------------------------------------------- control surfaces --

def test_midi_actions_route_into_state_manager(loaded, fake):
    import mido
    rec = Recorder(loaded, "go_fired", "markers_changed", "live_mode_changed")
    loaded.midi.load_mappings({"note_on:60": "go", "cc:64": "add_marker",
                               "pc:3": "toggle_live", "note_on:61": "exit_vamp"})
    assert loaded.midi.handle_message(mido.Message("note_on", note=60, velocity=100)) == "go"
    assert loaded.state == PlaybackState.PLAYING
    assert rec.args("go_fired")[0][0]["source"] == "midi"
    assert loaded.midi.handle_message(mido.Message("control_change", control=64, value=127)) == "add_marker"
    assert len(loaded.markers) == 1
    assert loaded.midi.handle_message(mido.Message("control_change", control=64, value=0)) is None
    assert loaded.midi.handle_message(mido.Message("program_change", program=3)) == "toggle_live"
    assert loaded.live_mode is True
    loaded.perform_action("stop")
    assert loaded.state == PlaybackState.STOPPED
    loaded.perform_action("bogus")                      # ignored


def test_osc_events_from_state_manager(loaded, fake, song):
    clients = []

    def factory(host, port):
        client = FakeOscClient(host, port)
        clients.append(client)
        return client

    loaded.osc._client_factory = factory
    loaded.configure_osc(True, "10.0.0.5", 9000, "loopstation")
    assert preferences.load_preferences()["osc"]["port"] == 9000

    loop = loaded.add_loop(2.0, 3.0, name="V")
    loaded.set_item_osc_cue(loop.id, "5")
    marker = loaded.add_marker(1.0, name="M")
    loaded.set_item_osc_cue(marker.id, "9")
    hold = loaded.add_hold(4.0, name="H")
    loaded.set_song_osc_cue("1")

    assert loaded.go() is True                                   # play -> song cue
    for _ in range(4):                                           # marker crossed between ticks
        fake.advance(0.3)
        _tick(loaded)
    fake.advance(1.8)                                            # vamp entered
    loaded._last_go_time = -1e9
    loaded.go()                                                  # exit vamp
    fake.advance(1.0)                                            # cut exit at 3.0
    _tick(loaded)
    fake.advance(1.0)                                            # hold reached
    assert loaded.osc.flush()

    sent = clients[0].sent
    assert (clients[0].host, clients[0].port) == ("10.0.0.5", 9000)
    assert ("/loopstation/go", ["play", 0.0, "ui", "GO: play"]) in sent
    assert ("/loopstation/cue", ["1"]) in sent
    assert ("/loopstation/cue_reached", [marker.id, "M", "marker"]) in sent
    assert ("/loopstation/cue", ["9"]) in sent
    assert ("/loopstation/vamp_enter", [loop.id, "V"]) in sent
    assert ("/loopstation/cue", ["5"]) in sent
    assert ("/loopstation/vamp_exit", [loop.id, "V", "cut"]) in sent
    assert ("/loopstation/hold_reached", [hold.id, "H"]) in sent
    assert ("/loopstation/cue_reached", [hold.id, "H", "hold"]) in sent
    assert loaded.state == PlaybackState.HELD

    # qlab scheme: only cues, as /cue/<id>/start
    loaded.configure_osc(True, "10.0.0.5", 9000, "qlab")
    loaded.stop()
    before = len(sent)
    loaded._last_go_time = -1e9
    loaded.go()
    assert loaded.osc.flush()
    assert sent[before:] == [("/cue/1/start", [])]

    # A dead client never raises into the caller
    clients[0].send_message = lambda *a: (_ for _ in ()).throw(OSError("boom"))
    loaded.stop()
    loaded._last_go_time = -1e9
    assert loaded.go() is True
    assert loaded.osc.flush()
    assert loaded.osc.last_error == "boom"


# ------------------------------------------------------------ engine events --

def test_unknown_engine_events_and_device_errors(loaded, fake):
    rec = Recorder(loaded, "device_error", "render_state")
    fake._fire("something_new", {"x": 1})            # ignored, no crash
    fake._fire("device_error", {"message": "no device"})
    fake._fire("render_failed", {"message": "ffmpeg died"})
    assert rec.args("device_error") == [("no device",)]
    assert rec.args("render_state") == [("failed", "ffmpeg died")]


def test_failed_load_resets_to_no_song(loaded, tmp_path):
    rec = Recorder(loaded, "song_loaded", "loops_changed", "stems_changed")
    loaded.add_loop(0.5, 1.5)
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"not audio at all")
    assert not loaded.load_song(str(bad))
    assert loaded.current_song_path == ""
    assert loaded.loops == [] and loaded.holds == [] and loaded.stems == []
    assert loaded.song_length == 0.0
    assert ("song_loaded", ("", 0.0)) in rec.items
    assert loaded.standby()["label"] == "No song loaded"
    loaded.play()  # must not raise
    assert loaded.go() is False


# --------------------------------------------------------------- monitor --

def test_monitor_restart_spawns_fresh_thread(sm_threaded, song):
    sm = sm_threaded
    assert sm.load_song(song)
    sm.play_from(0.1)
    first = sm._monitor_thread
    assert first.is_alive()
    sm.stop()
    sm.play_from(0.1)
    second = sm._monitor_thread
    assert second is not first
    assert second.is_alive()
    assert not first.is_alive()
    sm.stop()
    assert wait_until(lambda: not second.is_alive(), timeout=2.0)


def test_position_updates_are_throttled(sm_threaded, song, fake):
    sm = sm_threaded
    assert sm.load_song(song)
    rec = Recorder(sm, "position_update")
    sm.play_from(0.0)
    time.sleep(0.5)
    sm.stop()
    n = rec.count("position_update")
    assert 5 <= n <= 25   # ~30 Hz, not the 100 Hz scan rate
    assert rec.args("position_update")[0] == (0.0, False)
    assert wait_until(lambda: not sm._monitor_thread.is_alive(), timeout=2.0)


def test_monitor_thread_arms_engine_and_keeps_running_while_paused(sm_threaded, song, fake):
    sm = sm_threaded
    assert sm.load_song(song)
    sm.play()
    sm.add_hold(4.0)
    assert wait_until(lambda: fake.armed_hold == 4.0, timeout=2.0)
    sm.pause()
    sm.add_loop(6.0, 7.0)
    assert wait_until(lambda: fake.armed_loop is not None and fake.armed_loop["end"] == 7.0, timeout=2.0)
    assert sm._monitor_thread.is_alive()
    sm.stop()
    assert wait_until(lambda: not sm._monitor_thread.is_alive(), timeout=2.0)


# ------------------------------------------------------------- misc API --

def test_format_time_uses_shared_formatter():
    from utils.formatting import format_time
    assert StateManager.format_time(83.456) == format_time(83.456)


def test_loop_region_range_is_atomic():
    loop = LoopRegion(1.0, 2.0)
    loop.set_range(3.0, 4.5)
    assert (loop.start, loop.end) == (3.0, 4.5)
    loop.start = 2.5
    loop.end = 5
    assert loop.to_dict()["start"] == 2.5 and loop.to_dict()["end"] == 5.0
    loop.repeat_count = 4
    restored = LoopRegion.from_dict(loop.to_dict())
    assert (restored.start, restored.end, restored.repeat_count) == (2.5, 5.0, 4)
    assert LoopRegion.from_dict({"start": 0, "end": 1}).repeat_count is None
    assert LoopRegion.from_dict({"start": 0, "end": 1, "repeat_count": 0}).repeat_count is None


def test_every_documented_event_is_registrable(sm):
    for name in ("holds_changed", "hold_reached", "hold_released", "standby_changed",
                 "go_fired", "live_mode_changed", "settings_changed", "render_state",
                 "stems_changed", "tempo_estimated", "blocked_in_live_mode", "loop_cycle",
                 "position_update", "state_change", "loop_mode_enter", "loop_mode_exit",
                 "song_loaded", "song_ended", "loops_changed", "markers_changed",
                 "skips_changed", "loop_skip_queued", "loop_skip_cleared",
                 "data_load_failed", "preview_changed"):
        sm.on(name, lambda *a: None)
        assert len(sm._callbacks[name]) == 1


def test_output_device_restored_by_name_when_index_shifts(sm, monkeypatch):
    from utils import preferences
    store = {}
    monkeypatch.setattr(preferences, "load_preferences", lambda: dict(store))
    monkeypatch.setattr(preferences, "save_preferences", lambda p: store.update(p))
    devices = [{"index": 3, "name": "Interface A"}, {"index": 5, "name": "Speakers"}]
    monkeypatch.setattr(sm, "list_output_devices", lambda: list(devices))
    applied = []
    monkeypatch.setattr(sm.audio, "set_output_device", lambda i: applied.append(i), raising=False)
    assert sm.set_output_device(3)
    assert store["audio"] == {"output_device": 3, "output_device_name": "Interface A"}
    devices[:] = [{"index": 2, "name": "Speakers"}, {"index": 7, "name": "Interface A"}]
    assert sm.restore_output_device() == 7
    devices[:] = [{"index": 3, "name": "Speakers"}]          # interface unplugged
    assert sm.restore_output_device() is None                # default, not index 3


# ------------------------------------------------------- review fixes (r2) --

class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_watchdog_restarts_stream_when_frames_stop(loaded, fake):
    clock = _Clock()
    loaded._clock = clock
    rec = Recorder(loaded, "device_error", "device_lost", "device_restored", "state_change")
    loaded.play()
    fake.advance(1.0)
    for _ in range(5):                       # frames advance: nothing happens
        clock.t += 0.1
        _tick(loaded)
    assert rec.names() == ["state_change"]

    fake._stream_alive = False               # stream dies, device still there
    clock.t += 0.1
    _tick(loaded)                            # 100 ms without a new frame
    clock.t += 0.15
    _tick(loaded)
    assert rec.count("device_lost") == 0     # 250 ms: not yet
    clock.t += 0.1
    _tick(loaded)                            # 350 ms stalled -> restart
    assert rec.args("device_lost") == [("Audio output stopped (the device may have been unplugged)",)]
    assert rec.count("device_error") == 1
    assert rec.args("device_restored") == [("Fake Output",)]
    assert len(fake.calls_to("restart_stream")) == 1
    assert loaded.state == PlaybackState.PLAYING and not loaded.device_lost
    assert fake.get_position() == pytest.approx(1.0)
    clock.t += 1.0
    _tick(loaded)                            # running again: no new alarm
    assert rec.count("device_lost") == 1


def test_watchdog_failed_restart_parks_paused_and_retries(loaded, fake):
    clock = _Clock()
    loaded._clock = clock
    rec = Recorder(loaded, "device_error", "device_lost", "device_restored")
    loaded.play()
    fake.advance(2.0)
    _tick(loaded)
    fake.unplug()
    clock.t += 0.05
    _tick(loaded)
    clock.t += 0.4
    _tick(loaded)
    assert loaded.device_lost and loaded.state == PlaybackState.PAUSED
    assert rec.count("device_error") == 1    # the engine's own failure is not re-reported
    assert loaded.standby()["label"] == "Audio device lost - check interface"
    assert loaded.standby()["action"] == "none"
    assert loaded.go() is False              # GO does nothing in the alert state

    def restarts():
        return len(fake.calls_to("restart_stream"))
    assert restarts() == 1

    clock.t += 1.0
    _tick(loaded)
    assert restarts() == 1                   # retry only every 2 s
    clock.t += 1.1
    _tick(loaded)
    assert restarts() == 2 and loaded.device_lost
    assert rec.count("device_error") == 1 and rec.count("device_lost") == 1

    fake.replug()
    clock.t += 2.1
    _tick(loaded)
    assert restarts() == 3 and not loaded.device_lost
    assert rec.args("device_restored") == [("Fake Output",)]
    assert loaded.state == PlaybackState.PAUSED
    assert loaded.standby()["label"] == "GO: resume"
    loaded._last_go_time = -1e9
    assert loaded.go() is True and loaded.state == PlaybackState.PLAYING
    assert fake.get_position() == pytest.approx(2.0)


def test_play_while_device_lost_retries_once_and_stays_parked(loaded, fake):
    clock = _Clock()
    loaded._clock = clock
    loaded.play()
    fake.unplug()
    _tick(loaded)
    clock.t += 0.5
    _tick(loaded)
    assert loaded.device_lost and loaded.state == PlaybackState.PAUSED
    loaded.play()                            # still unplugged
    assert loaded.state == PlaybackState.PAUSED and len(fake.calls_to("restart_stream")) == 2
    fake.replug()
    loaded.play()
    assert loaded.state == PlaybackState.PLAYING and not loaded.device_lost


def test_device_error_event_while_playing_triggers_restart(loaded, fake):
    clock = _Clock()
    loaded._clock = clock
    rec = Recorder(loaded, "device_error", "device_lost", "device_restored")
    loaded.play()
    fake._fire("device_error", {"message": "stream callback died"})
    assert rec.names() == []                 # handled on the monitor thread
    _tick(loaded)
    assert rec.names() == ["device_lost", "device_error", "device_restored"]
    assert rec.args("device_error") == [("stream callback died",)]
    assert loaded.state == PlaybackState.PLAYING
    # render failures are reported but are not a device loss
    fake._fire("device_error", {"message": "render failed: boom"})
    assert rec.args("device_error")[-1] == ("render failed: boom",)
    assert len(fake.calls_to("restart_stream")) == 1


def test_watchdog_on_real_monitor_thread(sm_threaded, song, fake):
    rec = Recorder(sm_threaded, "device_lost", "device_restored")
    assert sm_threaded.load_song(song)
    sm_threaded.play()
    time.sleep(0.5)
    assert rec.names() == []                 # frames advancing: quiet
    fake.unplug()
    assert wait_until(lambda: sm_threaded.device_lost, timeout=2.0)
    assert wait_until(lambda: sm_threaded.state == PlaybackState.PAUSED, timeout=1.0)
    fake.replug()
    assert wait_until(lambda: not sm_threaded.device_lost, timeout=4.0)
    assert rec.names() == ["device_lost", "device_restored"]


def test_watchdog_ignores_paused_and_held(loaded, fake):
    clock = _Clock()
    loaded._clock = clock
    rec = Recorder(loaded, "device_lost")
    loaded.add_hold(1.0)
    loaded.play()
    fake.advance(1.5)                         # parks on the hold
    assert loaded.state == PlaybackState.HELD
    for _ in range(10):
        clock.t += 0.2
        _tick(loaded)
    loaded._release_hold()
    loaded.pause()
    for _ in range(10):
        clock.t += 0.2
        _tick(loaded)
    assert rec.names() == []


def test_go_while_paused_in_vamp_resumes(loaded, fake):
    loaded.add_loop(1.0, 2.0, name="V")
    loaded.play()
    fake.advance(2.5)
    assert fake.mode == "loop"
    loaded.pause()
    sb = loaded.standby()
    assert sb["action"] == "play" and sb["label"] == "GO: resume"
    loaded._last_go_time = -1e9
    assert loaded.go() is True
    assert loaded.state == PlaybackState.PLAYING and not loaded.exit_queue_active
    assert fake.calls_to("queue_loop_exit") == []
    assert fake.mode == "loop" and not fake.is_paused
    assert loaded.standby()["action"] == "exit_vamp"


def test_live_mode_blocks_rehearsal_navigation(loaded, fake):
    rec = Recorder(loaded, "blocked_in_live_mode")
    marker = loaded.add_marker(3.0)
    loaded.add_marker(6.0)
    loaded.set_live_mode(True)
    loaded.play()
    fake.advance(1.0)
    assert loaded.preview_loop(1.0, 2.0) is False and not loaded.is_previewing()
    assert loaded.preview_from(4.0) is False
    assert loaded.user_seek(5.0) is False
    assert loaded.nudge(0.1) is False
    assert loaded.seek_to_start() is False
    assert fake.get_position() == pytest.approx(1.0)
    assert loaded.state == PlaybackState.PLAYING
    assert rec.args("blocked_in_live_mode") == [
        ("preview_loop",), ("preview",), ("seek",), ("nudge",), ("seek_start",)]
    # cue jumps, pause, stop stay allowed
    assert loaded.jump_to_next_marker() is True
    assert fake.get_position() == pytest.approx(marker.time)
    loaded.pause()
    assert loaded.state == PlaybackState.PAUSED
    loaded.stop()
    loaded.set_live_mode(False)
    assert loaded.user_seek(5.0) is True and fake.get_position() == pytest.approx(5.0)
    assert loaded.nudge(-1.0) is True and fake.get_position() == pytest.approx(4.0)
    assert loaded.seek_to_start() is True and fake.get_position() == 0.0
    assert loaded.preview_loop(1.0, 2.0) is True and loaded.is_previewing()
    loaded.stop()
    assert loaded.preview_from(4.0) is True and fake.get_position() == pytest.approx(4.0)


def test_midi_toggle_live_needs_two_presses_to_leave(loaded):
    clock = _Clock()
    loaded._clock = clock
    rec = Recorder(loaded, "blocked_in_live_mode", "live_mode_changed")
    loaded.perform_action("toggle_live", source="midi")       # enter: one press
    assert loaded.live_mode
    loaded.perform_action("toggle_live", source="midi")       # arms the exit
    assert loaded.live_mode
    assert rec.args("blocked_in_live_mode") == [("leave_live_confirm",)]
    clock.t += 2.5                                             # too late
    loaded.perform_action("toggle_live", source="midi")
    assert loaded.live_mode
    clock.t += 1.0
    loaded.perform_action("toggle_live", source="midi")       # second press in time
    assert not loaded.live_mode
    assert rec.args("live_mode_changed") == [(True,), (False,)]
    loaded.perform_action("toggle_live", source="midi")       # and back in with one
    assert loaded.live_mode


def test_failed_routing_or_device_change_is_not_persisted(loaded, fake, prefs_file):
    rec = Recorder(loaded, "device_error", "settings_changed")
    loaded.add_marker(1.0)                   # something on disk for this song
    assert loaded.set_output_device(0) is True
    fake.fail_device_change = True
    assert loaded.set_routing("multichannel") is False
    assert loaded.settings.routing_mode == "stereo"
    assert rec.args("device_error") == [
        ("output device cannot open for 'multichannel'; routing reverted",)]
    assert rec.args("settings_changed")[-1][0].routing_mode == "stereo"   # UI reverts
    loaded.flush_save()
    with open(loaded.data_file, encoding="utf-8") as f:
        saved = json.load(f)
    assert saved[loaded.current_song_id]["settings"]["routing_mode"] == "stereo"

    assert loaded.set_output_device(3) is False
    assert rec.count("device_error") == 2
    assert preferences.load_preferences()["audio"]["output_device"] == 0
    fake.fail_device_change = False
    assert loaded.set_routing("click_right") is True
    assert loaded.settings.routing_mode == "click_right"


def test_failed_routing_while_playing_is_not_a_device_loss(loaded, fake):
    rec = Recorder(loaded, "device_lost")
    loaded.play()
    fake.fail_device_change = True
    assert loaded.set_routing("multichannel") is False
    _tick(loaded)
    assert rec.names() == [] and fake.calls_to("restart_stream") == []


def test_engine_stopped_event_stops_without_song_ended(loaded, fake):
    rec = Recorder(loaded, "state_change", "song_ended")
    loaded.play()
    fake._fire("stopped", {"position": 3.0, "reason": "fade"})
    assert loaded.state == PlaybackState.STOPPED
    assert rec.args("state_change")[-1] == (PlaybackState.STOPPED,)
    assert rec.count("song_ended") == 0
    n = rec.count("state_change")
    fake._fire("stopped", {"position": 3.0, "reason": "fade"})   # not playing: ignored
    assert rec.count("state_change") == n


def test_fade_exit_never_queued_outside_loop_mode(loaded, fake, monkeypatch):
    loaded.add_loop(1.0, 2.0)
    loaded.play()
    fake.advance(2.2)
    assert fake.mode == "loop"
    # The vamp ends between the mode check and the engine call
    real_current = loaded._current_loop

    def leave_then_current():
        fake.mode = "transport"
        return real_current()
    monkeypatch.setattr(loaded, "_current_loop", leave_then_current)
    loaded.queue_exit(fade_mode=True)
    assert fake.calls_to("queue_loop_exit") == []
    assert not loaded.exit_queue_active and not loaded.exit_fade_mode


def test_late_exit_after_count_vamp_is_ignored(loaded, fake):
    clock = _Clock()
    loaded._clock = clock
    rec = Recorder(loaded, "loop_skip_queued")
    loaded.add_loop(1.0, 2.0, name="V1")
    loaded.update_selected_loop(repeat_count=2)
    loaded.add_loop(4.0, 5.0, name="V2")
    loaded.play()
    fake.advance(2.0)
    _tick(loaded)
    fake.advance(1.0)                          # second pass ends -> count exit
    assert fake.mode == "transport"
    assert fake.events[-1] == ("loop_exited", {"position": 2.0, "reason": "count"})
    clock.t += 0.8
    loaded.perform_action("exit_vamp")         # late press for V1
    assert rec.names() == [] and loaded.temp_skip_loop_id == loaded.loops[0].id
    clock.t += 1.0                             # 1.8 s later it means "skip V2"
    loaded.perform_action("exit_vamp")
    assert rec.args("loop_skip_queued") == [("V2",)]
