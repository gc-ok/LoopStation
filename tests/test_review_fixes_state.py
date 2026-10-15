"""Regression tests for the review findings fixed in state_manager / utils / main."""
import glob
import json
import logging
import math
import os
import threading

import pytest

from backend import state_manager
from backend.state_manager import StateManager, PlaybackState, SongSettings, UNREADABLE_KEY
from config import MIN_LOOP_DURATION
from utils import preferences
from utils.formatting import format_time, parse_time

from fake_engine import FakeEngine
from helpers_audio import make_signal, write_wav


# ---------------------------------------------------------------- fixtures --

class Recorder:
    def __init__(self, sm, *events):
        self.items = []
        for name in events:
            sm.on(name, lambda *a, _n=name: self.items.append((_n, a)))

    def args(self, name):
        return [a for n, a in self.items if n == name]

    def count(self, name):
        return len(self.args(name))


@pytest.fixture(autouse=True)
def prefs_file(tmp_path, monkeypatch):
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


@pytest.fixture
def make_sm(data_file, monkeypatch):
    monkeypatch.setattr(StateManager, "_start_monitor", lambda self: None)
    made = []

    def _make(engine=None, path=None):
        manager = StateManager(ffmpeg_path="no-ffmpeg", data_file=path or data_file,
                               engine=engine or FakeEngine())
        made.append(manager)
        return manager
    yield _make
    for manager in made:
        manager.stop()
        manager.cleanup()


@pytest.fixture
def sm(make_sm, fake):
    return make_sm(fake)


@pytest.fixture
def loaded(sm, song):
    assert sm.load_song(song)
    return sm


def _write(path, obj_or_text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(obj_or_text if isinstance(obj_or_text, str) else json.dumps(obj_or_text))


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------- 1. data file read errors --

def test_locked_data_file_blocks_saving_until_readable(make_sm, data_file, song, monkeypatch):
    _write(data_file, {"other-song": {"loops": [], "markers": [{"time": 5, "name": "keep me"}]}})
    real_read = state_manager.read_bytes_with_retry

    def locked(path, *a, **k):
        if path == data_file:
            raise PermissionError("locked by antivirus")
        return real_read(path, *a, **k)
    monkeypatch.setattr(state_manager, "read_bytes_with_retry", locked)

    sm = make_sm()
    assert sm.loop_data == {}
    assert sm.last_load_error and "could not be read" in sm.last_load_error
    assert sm.load_song(song)
    sm.add_marker(3.0, name="new")
    assert sm.flush_save() is False                     # refuses to overwrite
    assert set(_read(data_file)) == {"other-song"}
    assert glob.glob(data_file + ".corrupt-*") == []    # not treated as corruption

    monkeypatch.setattr(state_manager, "read_bytes_with_retry", real_read)
    sm.save_loop()
    assert sm.flush_save() is True
    saved = _read(data_file)
    assert saved["other-song"]["markers"][0]["name"] == "keep me"
    assert saved[sm.current_song_id]["markers"][0]["name"] == "new"


def test_unblocked_read_keeps_disk_entry_for_song_edited_meanwhile(make_sm, data_file, song,
                                                                   monkeypatch):
    sm0 = make_sm()
    assert sm0.load_song(song)
    sm0.add_marker(1.0, name="real cue")
    sm0.flush_save()
    real_read = state_manager.read_bytes_with_retry
    monkeypatch.setattr(state_manager, "read_bytes_with_retry",
                        lambda p, *a, **k: (_ for _ in ()).throw(PermissionError("busy")))
    sm = make_sm()
    assert sm.load_song(song) and sm.markers == []      # shown empty while unreadable
    sm.add_marker(2.0, name="session cue")
    monkeypatch.setattr(state_manager, "read_bytes_with_retry", real_read)
    assert sm.flush_save() is True
    entry = _read(data_file)[sm.current_song_id]
    assert [m["name"] for m in entry["markers"]] == ["real cue"]
    assert [m["name"] for m in entry[state_manager.SESSION_COPY_KEY]["markers"]] == ["session cue"]
    # Reloading shows the real cues; saving then keeps the session copy too
    assert sm.load_song(song) and [m.name for m in sm.markers] == ["real cue"]
    sm.add_marker(4.0, name="after reload")
    sm.flush_save()
    entry = _read(data_file)[sm.current_song_id]
    assert [m["name"] for m in entry["markers"]] == ["real cue", "after reload"]
    assert state_manager.SESSION_COPY_KEY in entry


def test_corrupt_file_that_cannot_be_moved_is_copied_first(make_sm, data_file, monkeypatch):
    _write(data_file, "{not json")

    def no_move(src, dst, *a, **k):
        raise PermissionError("in use")
    monkeypatch.setattr(state_manager, "replace_with_retry", no_move)
    sm = make_sm()
    backups = glob.glob(data_file + ".corrupt-*")
    assert len(backups) == 1
    with open(backups[0], encoding="utf-8") as f:
        assert f.read() == "{not json"
    assert sm._data_read_blocked is None and "copy was saved" in sm.last_load_error


def test_corrupt_file_that_cannot_be_moved_or_copied_is_never_overwritten(make_sm, data_file,
                                                                          song, monkeypatch):
    _write(data_file, "{not json")
    monkeypatch.setattr(state_manager, "replace_with_retry",
                        lambda *a, **k: (_ for _ in ()).throw(PermissionError("in use")))
    monkeypatch.setattr(state_manager, "backup_copy", lambda *a, **k: None)
    sm = make_sm()
    assert sm._data_read_blocked
    assert sm.load_song(song)
    sm.add_marker(1.0)
    assert sm.flush_save() is False
    with open(data_file, encoding="utf-8") as f:
        assert f.read() == "{not json"


# ---------------------------------------------- 2. malformed items per item --

def test_malformed_items_are_skipped_and_preserved(make_sm, data_file, song, fake):
    sm = make_sm(fake)
    sid = sm._get_file_fingerprint(song)
    bad_marker = {"name": "no time"}
    sm.loop_data[sid] = {
        "loops": [{"start": 1, "end": 3, "name": "V"}, {"start": None, "end": 2}],
        "markers": [bad_marker, {"time": 2.0, "name": "ok"}],
        "holds": [{"time": "nan"}],
        "settings": {"bpm": None, "downbeat_offset": float("nan"), "beats_per_bar": 3},
    }
    rec = Recorder(sm, "data_load_failed")
    assert sm.load_song(song)
    assert [l.name for l in sm.loops] == ["V"]
    assert [m.name for m in sm.markers] == ["ok"]
    assert sm.holds == []
    assert sm.settings.bpm == 0.0 and sm.settings.downbeat_offset == 0.0
    assert sm.settings.beats_per_bar == 3
    assert rec.count("data_load_failed") == 1

    sm.add_hold(4.0)
    sm.flush_save()
    entry = _read(data_file)[sid]
    assert [l["name"] for l in entry["loops"]] == ["V"]
    assert entry[UNREADABLE_KEY]["markers"] == [bad_marker]
    assert len(entry[UNREADABLE_KEY]["loops"]) == 1 and len(entry[UNREADABLE_KEY]["holds"]) == 1

    # A clean reload keeps the preserved items through further saves
    sm2 = make_sm(FakeEngine())
    rec2 = Recorder(sm2, "data_load_failed")
    assert sm2.load_song(song) and rec2.count("data_load_failed") == 0
    sm2.add_marker(6.0)
    sm2.flush_save()
    assert _read(data_file)[sid][UNREADABLE_KEY]["markers"] == [bad_marker]


def test_nan_in_saved_json_is_rejected(make_sm, data_file, song):
    sm0 = make_sm()
    sid = sm0._get_file_fingerprint(song)
    _write(data_file, '{"%s": {"loops": [], "markers": [{"time": NaN}, {"time": 1.5}],'
                      ' "settings": {"downbeat_offset": Infinity}}}' % sid)
    sm = make_sm()
    assert sm.load_song(song)
    assert [m.time for m in sm.markers] == [1.5]
    assert math.isfinite(sm.settings.downbeat_offset)


def test_unusable_entry_is_kept_verbatim(make_sm, data_file, song):
    sm = make_sm()
    sid = sm._get_file_fingerprint(song)
    sm.loop_data[sid] = ["not", "an", "object"]
    assert sm.load_song(song) and sm.last_load_error
    sm.add_marker(1.0)
    sm.flush_save()
    assert _read(data_file)[sid][UNREADABLE_KEY]["entry"] == [["not", "an", "object"]]


# ------------------------------------------------------ 3. Live Mode jumps --

def test_live_mode_blocks_arbitrary_jumps_but_not_cue_stepping(loaded, fake):
    m = loaded.add_marker(8.0)
    loaded.add_marker(5.0)
    loaded.play()
    fake.advance(3.0)
    loaded.set_live_mode(True)
    rec = Recorder(loaded, "blocked_in_live_mode")
    assert loaded.jump_to_marker(m.id) is False
    assert loaded.play_from(6.0) is False
    assert fake.get_position() == pytest.approx(3.0)
    assert loaded.state == PlaybackState.PLAYING
    assert [a[0] for a in rec.args("blocked_in_live_mode")] == ["jump_to_marker", "play_from"]
    loaded.perform_action("next_cue")       # stepping through cues stays allowed
    assert fake.get_position() == 5.0
    assert loaded.jump_to_prev_marker() is False   # nothing before 5.0 - 0.5
    loaded.set_live_mode(False)
    assert loaded.jump_to_marker(m.id) is True and fake.get_position() == 8.0


# ------------------------------------------------------- 4. hold vs. pause --

def test_hold_reached_while_paused_is_reconciled_on_resume(loaded, fake):
    loaded.add_hold(3.0)
    loaded.play()
    fake.advance(2.99)
    loaded._monitor_tick()
    loaded.pause()
    fake.is_held = True                     # engine parked on the hold meanwhile
    loaded._on_engine_event("hold_reached", {"position": 3.0})
    assert loaded.state == PlaybackState.PAUSED
    assert loaded.standby()["action"] == "play"
    loaded._last_go_time = -1e9
    assert loaded.go()                      # resume -> lands on the hold
    assert loaded.state == PlaybackState.HELD
    loaded._last_go_time = -1e9
    assert loaded.go()                      # release
    assert loaded.state == PlaybackState.PLAYING
    assert not fake.is_paused and not fake.is_held


def test_release_hold_resumes_a_paused_engine(loaded, fake):
    loaded.add_hold(3.0)
    loaded.play()
    fake.advance(3.5)
    assert loaded.state == PlaybackState.HELD
    fake.is_paused = True                   # paused while parked
    loaded._release_hold()
    assert loaded.state == PlaybackState.PLAYING and not fake.is_paused


# --------------------------------------------- 5. tempo during load render --

class SlowRenderEngine(FakeEngine):
    def set_tempo_pitch(self, tempo_ratio=1.0, semitones=0.0):
        self._rec("set_tempo_pitch", tempo_ratio, semitones)   # render never finishes


def test_pending_load_render_keeps_saved_tempo(make_sm, data_file, song, song_b):
    sm = make_sm()
    assert sm.load_song(song)
    sm.set_tempo_pitch(0.9, -1.0)
    sm.flush_save()
    sid = sm.current_song_id

    engine = SlowRenderEngine()
    sm2 = make_sm(engine)
    assert sm2.load_song(song)
    assert sm2.pending_tempo_pitch == (0.9, -1.0)
    sm2.add_marker(1.0)
    assert sm2.load_song(song_b)
    settings = _read(data_file)[sid]["settings"]
    assert (settings["tempo_ratio"], settings["semitones"]) == (0.9, -1.0)


# ------------------------------------------------- 7. save never raises --

def test_to_dict_copies_tag_notes(loaded):
    m = loaded.add_marker(1.0)
    loaded.set_item_tag_note(m.id, "Director", "x")
    loaded.flush_save()
    assert loaded.loop_data[loaded.current_song_id]["markers"][0]["tag_notes"] is not m.tag_notes


def test_serialisation_error_does_not_escape_stop(loaded, monkeypatch):
    loaded.add_marker(1.0)

    def boom(*a, **k):
        raise RuntimeError("dictionary changed size during iteration")
    rec = Recorder(loaded, "save_failed")
    with monkeypatch.context() as m:
        m.setattr(state_manager.json, "dumps", boom)
        loaded.stop()                       # must not raise
    assert loaded.state == PlaybackState.STOPPED
    assert loaded._save_dirty and rec.count("save_failed") >= 1
    assert loaded.flush_save() is True


# --------------------------------------------- 8. duplicate exit after cut --

def test_second_exit_press_after_cut_does_not_skip_next_vamp(loaded, fake):
    loaded.add_loop(1.0, 2.0, name="V1")
    v2 = loaded.add_loop(4.0, 5.0, name="V2")
    loaded.play()
    loaded._monitor_tick()
    fake.advance(2.05)
    loaded._monitor_tick()
    assert fake.mode == "loop"
    loaded.queue_exit()
    fake.advance(1.0)
    loaded._monitor_tick()
    assert fake.mode == "transport"
    loaded.queue_exit()                     # double tap / second surface
    assert loaded.temp_skip_loop_id != v2.id
    fake.advance(2.0)
    loaded._monitor_tick()
    fake.advance(1.0)
    assert fake.mode == "loop"


# ------------------------------------------ 9. GO after natural song end --

def test_go_after_song_end_does_nothing_until_play(loaded, fake):
    loaded.play()
    fake.advance(20.0)                      # runs off the end
    assert loaded.state == PlaybackState.STOPPED
    assert loaded.standby()["action"] == "none"
    n = len(fake.calls_to("play_transport"))
    loaded._last_go_time = -1e9
    assert loaded.go() is False
    assert len(fake.calls_to("play_transport")) == n
    loaded.play()
    assert loaded.state == PlaybackState.PLAYING
    loaded.stop()
    assert loaded.standby()["action"] == "play"


def test_go_after_fade_exit_does_nothing(loaded, fake):
    loaded.add_loop(1.0, 2.0)
    loaded.play()
    loaded._monitor_tick()
    fake.advance(2.05)
    loaded.queue_exit(fade_mode=True, fade_ms=100)
    fake.advance(1.0)
    assert loaded.state == PlaybackState.STOPPED
    loaded._last_go_time = -1e9
    assert loaded.go() is False
    loaded.seek(1.0)                        # an explicit seek re-arms GO
    assert loaded.standby()["action"] == "play"


def test_go_after_song_end_advances_to_next_song_even_in_live(loaded, fake, song_b):
    loaded.next_song_provider = lambda: song_b
    requested = []
    loaded.on('go_next_song', requested.append)
    loaded.set_live_mode(True)
    loaded.play()
    fake.advance(20.0)                      # runs off the end
    sb = loaded.standby()
    assert sb["action"] == "next_song" and "other" in sb["label"]
    n = len(fake.calls_to("play_transport"))
    loaded._last_go_time = -1e9
    assert loaded.go() is True
    assert requested == [song_b]
    assert len(fake.calls_to("play_transport")) == n    # this song is not restarted
    assert loaded.load_song(song_b) is False             # manual switch stays locked
    assert loaded.load_song(song_b, from_go=True) is True
    loaded.play_after_go_load()
    assert loaded.state == PlaybackState.PLAYING
    assert os.path.basename(loaded.current_song_path) == "other.wav"


def test_go_at_end_of_last_song_does_nothing(loaded, fake):
    loaded.next_song_provider = lambda: None
    loaded.play()
    fake.advance(20.0)
    sb = loaded.standby()
    assert sb["action"] == "none" and "End of song list" in sb["label"]
    loaded._last_go_time = -1e9
    assert loaded.go() is False


# ------------------------------------------ 10. re-exported song file --

def test_reexported_file_at_same_path_keeps_its_cues(make_sm, data_file, tmp_path):
    path = write_wav(tmp_path / "show.wav", make_signal(10.0, sr=8000), sr=8000)
    sm = make_sm()
    assert sm.load_song(path)
    old_id = sm.current_song_id
    sm.add_marker(2.0, name="Cue A")
    sm.flush_save()
    write_wav(tmp_path / "show.wav", make_signal(10.0, sr=8000, freq=500.0), sr=8000)

    sm2 = make_sm()
    assert sm2.load_song(path)
    assert sm2.current_song_id != old_id
    assert [m.name for m in sm2.markers] == ["Cue A"]
    sm2.flush_save()
    saved = _read(data_file)
    assert old_id not in saved and sm2.current_song_id in saved


def test_name_match_requires_unique_orphaned_entry(make_sm, tmp_path):
    os.makedirs(tmp_path / "a")
    a = write_wav(tmp_path / "a" / "song.wav", make_signal(10.0, sr=8000), sr=8000)
    sm = make_sm()
    assert sm.load_song(a)
    sm.add_marker(1.0)
    sm.flush_save()
    # Same name and duration elsewhere, but the original still exists -> no match
    os.makedirs(tmp_path / "b")
    b = write_wav(tmp_path / "b" / "song.wav", make_signal(10.0, sr=8000, freq=300.0), sr=8000)
    assert sm.load_song(b) and sm.markers == []
    # Original gone -> adopted
    os.remove(a)
    sm.loop_data.pop(sm.current_song_id, None)
    assert sm.load_song(b) and len(sm.markers) == 1


# ------------------------------------------ 12. tempo map / bar-beat --

def test_tempo_map_rejects_non_finite_and_bar_beat_on_beat(loaded):
    loaded.set_song_tempo_map(120, 4, 0.1)
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            loaded.set_song_tempo_map(bad, 4, 0.0)
        with pytest.raises(ValueError):
            loaded.set_song_tempo_map(120, 4, bad)
    assert (loaded.settings.bpm, loaded.settings.downbeat_offset) == (120, 0.1)
    assert loaded.bar_beat_at(4.1)[:2] == (3, 1)
    misreads = [k for k in range(200) if loaded.bar_beat_at(0.1 + k * 0.5)[1] != k % 4 + 1]
    assert misreads == []


def test_song_settings_from_dict_sanitizes_non_finite():
    s = SongSettings.from_dict({"bpm": float("inf"), "downbeat_offset": float("nan"),
                                "tempo_ratio": 0, "beats_per_bar": None, "semitones": 2})
    assert (s.bpm, s.downbeat_offset, s.tempo_ratio, s.beats_per_bar, s.semitones) == \
        (0.0, 0.0, 1.0, 4, 2.0)


# ------------------------------------------------------ 13. formatting --

def test_format_time_rounds_before_splitting_minutes():
    assert format_time(59.996) == "1:00.00"
    assert format_time(119.999) == "2:00.00"
    assert format_time(83.45) == "1:23.45"
    assert format_time(float("nan")) == "0:00.00"
    assert format_time(59.996, include_ms=False) == "0:59"


def test_parse_time_rejects_non_finite_and_negative():
    for bad in ("nan", "inf", "-inf", "1:-5", "-1:05", "-0:05", "-3", "1:2:3:4", "1:nan"):
        assert parse_time(bad) is None, bad
    assert parse_time("1:23.45") == pytest.approx(83.45)
    assert parse_time("83") == 83.0 and parse_time("1:00:05") == 3605.0


# ---------------------------------------------- 14. sorted on load --

def test_markers_and_skips_sorted_on_load(sm, song, fake):
    sid = sm._get_file_fingerprint(song)
    sm.loop_data[sid] = {"loops": [],
                         "markers": [{"time": 8.0, "name": "C"}, {"time": 2.0, "name": "A"},
                                     {"time": 5.0, "name": "B"}],
                         "skips": [{"start": 6, "end": 7}, {"start": 1, "end": 1.5}]}
    assert sm.load_song(song)
    assert [m.name for m in sm.markers] == ["A", "B", "C"]
    assert [s.start for s in sm.skips] == [1, 6]
    fake._pos = 9.0
    assert sm.jump_to_prev_marker() and fake.get_position() == 8.0
    fake._pos = 3.0
    assert sm.jump_to_next_marker() and fake.get_position() == 5.0


# ---------------------------------------------- 15. cues inside skips --

def test_next_item_and_countdown_ignore_cues_inside_skips(loaded, fake):
    loaded.add_loop(3.0, 4.0, name="Hidden")
    loaded.add_skip(2.5, 6.0)
    loaded.add_hold(5.0)
    loaded.play()
    loaded._monitor_tick()
    assert loaded.standby()["next_item"] is None
    assert loaded.countdown_to(8.0) == pytest.approx(8.0 - 3.5)
    loaded.add_marker(7.0, name="After")
    assert loaded.standby()["label"] == "Next: After"


# ---------------------------------------------- 16. minimum vamp length --

def test_vamps_shorter_than_minimum_are_rejected(loaded):
    assert loaded.add_loop(1.0, 1.0 + MIN_LOOP_DURATION / 2) is None
    v = loaded.add_loop(1.0, 1.0 + MIN_LOOP_DURATION)
    assert v is not None
    assert loaded.update_selected_loop(end=1.0 + MIN_LOOP_DURATION / 2) is False
    assert loaded.loops[0].end == pytest.approx(1.0 + MIN_LOOP_DURATION)
    assert loaded.preview_loop(2.0, 2.0 + MIN_LOOP_DURATION / 2) is False


# ------------------------------------------------ OSC cue sanitising --

def test_osc_cue_numbers_are_sanitized(loaded):
    m = loaded.add_marker(1.0)
    assert loaded.set_item_osc_cue(m.id, " 1/2 *x{} ")
    assert m.osc_cue == "12x"
    loaded.set_song_osc_cue("4 #5,")
    assert loaded.settings.osc_cue == "45"
    assert state_manager.Marker.from_dict({"time": 1, "osc_cue": "a/b"}).osc_cue == "ab"


# ------------------------------------------------------ 6. preferences --

def test_corrupt_prefs_are_backed_up_not_silently_wiped(prefs_file):
    preferences.save_preferences({"theme": "Dark", "osc": {"host": "10.0.0.5"}})
    with open(prefs_file, encoding="utf-8") as f:
        text = f.read()
    with open(prefs_file, "w", encoding="utf-8") as f:
        f.write(text[:-5])
    preferences.set_theme_preference("Light")
    backups = glob.glob(prefs_file + ".corrupt-*")
    assert len(backups) == 1
    with open(backups[0], encoding="utf-8") as f:
        assert f.read() == text[:-5]
    assert preferences.load_preferences() == {"theme": "Light"}


def test_unreadable_prefs_are_not_overwritten(prefs_file, monkeypatch):
    preferences.save_preferences({"theme": "Dark", "midi": {"port": "X"}})
    with monkeypatch.context() as m:
        m.setattr(preferences, "read_bytes_with_retry",
                  lambda p, *a, **k: (_ for _ in ()).throw(PermissionError("locked")))
        preferences.save_preferences({"theme": "Light"})
    assert preferences.load_preferences() == {"theme": "Dark", "midi": {"port": "X"}}


def test_prefs_nested_dicts_merge_one_level():
    preferences.save_preferences({"audio": {"buffer": 256, "output_device_name": "RME"}})
    preferences.save_preferences({"audio": {"output_device": 3, "output_device_name": "RME"}})
    preferences.save_preferences({"midi": {"mappings": {"go": [1]}, "port": "A"}})
    preferences.save_preferences({"midi": {"mappings": {"stop": [2]}, "port": "A"}})
    prefs = preferences.load_preferences()
    assert prefs["audio"] == {"buffer": 256, "output_device": 3, "output_device_name": "RME"}
    assert prefs["midi"]["mappings"] == {"stop": [2]}     # replaced below the first level


def test_concurrent_pref_writers_lose_nothing(prefs_file):
    errors = []

    def writer(i):
        for j in range(50):
            try:
                preferences.save_preferences({f"k{i}": j})
            except Exception as e:   # pragma: no cover
                errors.append(e)
    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    prefs = preferences.load_preferences()
    assert not errors
    assert [prefs.get(f"k{i}") for i in range(4)] == [49] * 4
    assert glob.glob(prefs_file + ".tmp*") == []


# ----------------------------------------------------- 11. crash hooks --

def test_crash_handlers_route_to_log(monkeypatch, caplog):
    import sys
    import main
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    log = logging.getLogger("LoopStation.test")
    main.install_crash_handlers(log)

    with caplog.at_level(logging.ERROR, logger="LoopStation.test"):
        try:
            raise ValueError("main thread boom")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        t = threading.Thread(target=lambda: 1 / 0, name="worker-x")
        t.start()
        t.join()

        class Root:
            pass
        root = Root()
        main.install_tk_crash_handler(root, log)
        try:
            raise KeyError("tk boom")
        except KeyError:
            root.report_callback_exception(*sys.exc_info())
    text = caplog.text
    assert "main thread boom" in text and "worker-x" in text and "tk boom" in text
