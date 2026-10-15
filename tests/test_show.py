"""Show (setlist) model and its StateManager integration."""
import json
import os
import threading

import pytest

from backend import show as show_mod
from backend.show import Show, ShowEntry, ShowFileError, folder_show_path
from backend.state_manager import StateManager, PlaybackState
from utils import preferences

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


@pytest.fixture(autouse=True)
def prefs_file(tmp_path, monkeypatch):
    path = str(tmp_path / "prefs" / "user_preferences.json")
    os.makedirs(os.path.dirname(path))
    monkeypatch.setattr(preferences, "PREFS_FILE", path)
    return path


@pytest.fixture
def data_dir(tmp_path):
    path = tmp_path / "appdata"
    path.mkdir()
    return str(path)


def song(folder, name, secs=2.0, freq=440.0):
    os.makedirs(folder, exist_ok=True)
    return write_wav(os.path.join(folder, name), make_signal(secs, sr=8000, freq=freq), sr=8000)


@pytest.fixture
def lib(tmp_path):
    folder = str(tmp_path / "Show Music")
    paths = [song(folder, n, secs=2.0 + i * 0.25, freq=300.0 + 50 * i)
             for i, n in enumerate(["b Two.wav", "A One.wav", "c Three.wav"])]
    with open(os.path.join(folder, "notes.txt"), "w", encoding="utf-8") as f:
        f.write("not audio")
    return folder, sorted(paths, key=lambda p: os.path.basename(p).casefold())


class PreloadEngine(FakeEngine):
    """FakeEngine with the preload surface agent E adds to the real engine."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.preloads = []
        self.preload_called = threading.Event()

    def load_file(self, path, stem_paths=None, tempo_pitch=None):
        result = super().load_file(path, stem_paths)
        self.calls[-1] = ("load_file", (path, stem_paths), {"tempo_pitch": tempo_pitch})
        if tempo_pitch is not None and any(p[0] == path and tuple(p[2]) == tuple(tempo_pitch)
                                           for p in self.preloads):
            self._tempo = tuple(tempo_pitch)   # installed the pre-rendered preload
        return result

    def preload_file(self, path, stem_paths=None, tempo_pitch=(1.0, 0.0)):
        self.preloads.append((path, stem_paths, tuple(tempo_pitch)))
        self.preload_called.set()
        return True

    def cancel_preload(self):
        self._rec("cancel_preload")

    def get_preloaded_audio(self, path):
        return "MIX" if any(p[0] == path for p in self.preloads) else None


@pytest.fixture
def make_sm(data_dir, monkeypatch):
    monkeypatch.setattr(StateManager, "_start_monitor", lambda self: None)
    made = []

    def _make(engine=None):
        sm = StateManager(ffmpeg_path="no-ffmpeg", data_file=os.path.join(data_dir, "loop_data.json"),
                          engine=engine or FakeEngine())
        made.append(sm)
        return sm
    yield _make
    for sm in made:
        sm.stop()
        sm.cleanup()


def _end_song(sm):
    sm.play()
    sm.audio.advance(60.0)
    assert sm.state == PlaybackState.STOPPED and sm._ended_at_rest
    sm._last_go_time = -1e9


def _wait_preload(sm, timeout=5.0):
    thread = sm._preload_thread
    if thread is not None:
        thread.join(timeout)


# ------------------------------------------------------------------ model --

def test_from_folder_sorts_casefold_and_ignores_non_audio(lib, data_dir):
    folder, paths = lib
    show = Show.from_folder(folder, data_dir=data_dir, restore=False)
    assert [e.path for e in show.entries] == paths
    assert [e.title for e in show.entries] == ["A One", "b Two", "c Three"]
    assert show.is_folder_derived and show.file_path is None


def test_round_trip_relative_paths_and_moved_show_folder(tmp_path):
    root = tmp_path / "proj"
    a = song(str(root / "audio"), "Ünïcode Sóng ♪.wav")
    b = song(str(root / "audio"), "two.wav", freq=500)
    show = Show("My Show")
    show.add_songs([a, b])
    show.add_divider("Act 2", index=1)
    show.set_cut(show.entries[2].id, True)
    show.set_segue(show.entries[0].id, True)
    target = show.save(str(root / "my.loopshow"))
    data = json.loads(open(target, encoding="utf-8").read())
    assert data["format"] == "loopstation-show" and data["version"] == 1
    assert data["entries"][0]["rel_path"] == "audio/Ünïcode Sóng ♪.wav"
    assert data["entries"][1]["kind"] == "divider" and data["entries"][1]["rel_path"] is None

    moved = tmp_path / "elsewhere"
    os.rename(root, moved)                     # whole project moved: relative wins
    loaded = Show.load(str(moved / "my.loopshow"))
    assert [e.kind for e in loaded.entries] == ["song", "divider", "song"]
    assert loaded.entries[0].path == os.path.normpath(str(moved / "audio" / "Ünïcode Sóng ♪.wav"))
    assert loaded.entries[0].segue and loaded.entries[2].cut
    assert [e.id for e in loaded.entries] == [e.id for e in show.entries]
    assert not any(loaded.is_missing(e) for e in loaded.entries)


def test_absolute_fallback_when_only_show_file_moves(tmp_path):
    a = song(str(tmp_path / "audio"), "one.wav")
    show = Show()
    show.add_songs([a])
    show.save(str(tmp_path / "shows" / "s.loopshow"))
    os.makedirs(tmp_path / "other" / "deeper")
    os.replace(tmp_path / "shows" / "s.loopshow", tmp_path / "other" / "deeper" / "s.loopshow")
    loaded = Show.load(str(tmp_path / "other" / "deeper" / "s.loopshow"))
    assert loaded.entries[0].path == a and not loaded.is_missing(loaded.entries[0])


def test_missing_files_and_malformed_entries_are_kept(tmp_path):
    a = song(str(tmp_path), "one.wav")
    raw = {"format": "loopstation-show", "version": 1, "name": "S",
           "entries": [{"id": "x1", "kind": "song", "rel_path": "one.wav", "abs_path": a},
                       {"id": "x2", "kind": "song", "rel_path": "gone.wav", "abs_path": None},
                       {"id": "x3", "kind": "weird"}, "garbage",
                       {"kind": "song"}]}
    path = str(tmp_path / "s.loopshow")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f)
    show = Show.load(path)
    assert [e.id for e in show.entries] == ["x1", "x2"]
    assert show.is_missing(show.entries[1]) and not show.playable(show.entries[1])
    assert len(show.load_problems) == 3 and len(show.unreadable) == 3
    show.save()
    again = json.loads(open(path, encoding="utf-8").read())
    assert again["_unreadable"] == [{"id": "x3", "kind": "weird"}, "garbage", {"kind": "song"}]


def test_not_a_show_raises(tmp_path):
    path = str(tmp_path / "bad.loopshow")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not json")
    with pytest.raises(ShowFileError):
        Show.load(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"format": "other"}, f)
    with pytest.raises(ShowFileError):
        Show.load(path)


def test_relative_path_impossible_across_drives(monkeypatch):
    def boom(*a, **k):
        raise ValueError("path is on mount 'C:', start on mount 'D:'")
    monkeypatch.setattr(show_mod.os.path, "relpath", boom)
    entry = ShowEntry("song", "/x/song.wav")
    d = entry.to_dict("/elsewhere")
    assert d["rel_path"] is None and d["abs_path"] == os.path.abspath("/x/song.wav")


def test_windows_style_relative_separators_resolve(tmp_path):
    a = song(str(tmp_path / "sub"), "one.wav")
    entry = ShowEntry.from_dict({"kind": "song", "rel_path": "sub\\one.wav",
                                 "abs_path": "C:\\nowhere\\one.wav"}, str(tmp_path))
    assert entry.path == a


def test_editing_ops(lib, tmp_path):
    folder, (a, b, c) = lib
    show = Show()
    ids = show.add_songs([a, os.path.join(folder, "notes.txt"), str(tmp_path / "nope.wav"), b])
    assert len(ids) == 2
    show.add_songs([c], index=0)
    assert [e.path for e in show.entries] == [c, a, b]
    div = show.add_divider("Act 1", index=0)
    assert show.entries[0].title == "Act 1" and show.entries[0].path is None
    assert show.move(div, 2) and show.index_of(div) == 2
    dup = show.duplicate(ids[0])
    assert show.entries[show.index_of(ids[0]) + 1].id == dup and show.get(dup).path == a
    assert show.rename(ids[0], "Overture") and show.get(ids[0]).title == "Overture"
    assert show.rename(ids[0], "") and show.get(ids[0]).title == "A One"
    assert not show.set_cut(div, True)            # dividers have no flags
    assert show.remove(dup) and show.get(dup) is None
    assert show.dirty


def test_next_playable_skips_cut_divider_missing(tmp_path):
    a = song(str(tmp_path), "a.wav")
    b = song(str(tmp_path), "b.wav", freq=500)
    c = song(str(tmp_path), "c.wav", freq=600)
    d = song(str(tmp_path), "d.wav", freq=700)
    show = Show()
    ia, ib, ic, id_ = show.add_songs([a, b, c, d])
    div = show.add_divider("Act 2", index=1)
    show.set_cut(ib, True)
    os.remove(c)
    show.invalidate_missing_cache()
    assert show.next_playable_after(ia).id == id_
    assert show.next_playable_after(div).id == id_
    assert show.next_playable_after(id_) is None
    assert show.next_playable_after(None).id == ia


def test_relink_also_fixes_other_missing_entries(tmp_path):
    old = tmp_path / "old"
    a, b = song(str(old), "a.wav"), song(str(old), "b.wav", freq=500)
    show = Show()
    ia, ib = show.add_songs([a, b])
    new = tmp_path / "new"
    os.rename(old, new)
    show.invalidate_missing_cache()
    assert show.is_missing(show.get(ia)) and show.is_missing(show.get(ib))
    changed = show.relink(ia, str(new / "a.wav"))
    assert set(changed) == {ia, ib}
    assert show.get(ib).path == str(new / "b.wav") and not show.is_missing(show.get(ib))


# ----------------------------------------------------- folder autosave --

def test_folder_order_is_remembered_with_added_and_deleted_files(lib, data_dir):
    folder, (a, b, c) = lib
    show = Show.from_folder(folder, data_dir=data_dir)
    assert show.dirty                              # first time: worth remembering
    show.move(show.entries[2].id, 0)               # c, a, b
    show.autosave(data_dir)
    assert os.path.isfile(folder_show_path(folder, data_dir))

    os.remove(b)
    d = song(folder, "0 New.wav", freq=900)
    again = Show.from_folder(folder + os.sep, data_dir=data_dir)   # trailing sep: same key
    assert [e.path for e in again.entries] == [c, a, b, d]         # new file appended
    assert again.is_missing(again.entries[2])
    assert again.dirty


def test_corrupt_folder_autosave_is_backed_up(lib, data_dir):
    folder, paths = lib
    auto = folder_show_path(folder, data_dir)
    os.makedirs(os.path.dirname(auto))
    with open(auto, "w", encoding="utf-8") as f:
        f.write("{broken")
    show = Show.from_folder(folder, data_dir=data_dir)
    assert [e.path for e in show.entries] == paths
    assert any(n.startswith(os.path.basename(auto) + ".corrupt-") for n in os.listdir(os.path.dirname(auto)))


# --------------------------------------------------- StateManager glue --

def test_open_folder_autosaves_edits_and_restores(make_sm, lib, data_dir):
    folder, (a, b, c) = lib
    sm = make_sm()
    rec = Recorder(sm, "show_changed")
    assert sm.open_folder(folder)
    snap = rec.args("show_changed")[-1][0]
    assert [e["path"] for e in snap["entries"]] == [a, b, c] and snap["is_folder_derived"]
    sm.move_entry(snap["entries"][2]["id"], 0)
    assert sm.flush_show_autosave()
    sm2 = make_sm()
    sm2.open_folder(folder)
    assert [e.path for e in sm2.show.entries] == [c, a, b]
    assert preferences.get_last_show() == {"kind": "folder", "path": folder}
    assert preferences.get_recent_shows()[0] == {"kind": "folder", "path": folder}


def test_save_and_open_show_file_tracks_recent(make_sm, lib, tmp_path):
    folder, (a, b, c) = lib
    sm = make_sm()
    sm.new_show("Gala")
    sm.add_songs([b, a])
    assert sm.save_show() is False                 # no file yet: UI asks Save As
    target = str(tmp_path / "gala.loopshow")
    assert sm.save_show(target)
    assert not sm.show.dirty
    sm.new_show()
    assert sm.open_show(target)
    assert [e.path for e in sm.show.entries] == [b, a]
    assert preferences.get_recent_shows()[0] == {"kind": "file", "path": target}
    for i in range(10):
        preferences.add_recent_show("file", str(tmp_path / f"s{i}.loopshow"))
    assert len(preferences.get_recent_shows()) == preferences.MAX_RECENT_SHOWS


def test_live_mode_locks_edits_but_allows_next_up_and_go(make_sm, lib):
    folder, (a, b, c) = lib
    sm = make_sm()
    sm.open_folder(folder)
    ids = [e.id for e in sm.show.entries]
    assert sm.load_entry(ids[0])
    sm.set_live_mode(True)
    blocked = Recorder(sm, "blocked_in_live_mode", "next_up_changed", "go_next_song", "go_next_entry")
    assert sm.move_entry(ids[2], 0) is False
    assert sm.remove_entry(ids[1]) is False
    assert sm.add_divider("x") is None
    assert sm.set_entry_cut(ids[1], True) is False
    assert sm.open_folder(folder) is False
    assert sm.load_entry(ids[1]) is False          # manual switching stays locked
    assert [a_[0] for a_ in blocked.args("blocked_in_live_mode")] == [
        "move_entry", "remove_entry", "add_divider", "set_entry_cut", "open_folder", "load_song"]
    assert [e.id for e in sm.show.entries] == ids

    assert sm.set_next_up(ids[2])                  # off-script: allowed
    assert blocked.args("next_up_changed")[-1] == (ids[2],)
    _end_song(sm)
    assert sm.standby()["label"] == "GO: next song - c Three"
    assert sm.go() is True
    assert blocked.args("go_next_song") == [(c,)]
    assert blocked.args("go_next_entry") == [(ids[2],)]
    assert sm.load_song(c, from_go=True)           # what the current UI does
    assert sm.current_entry_id == ids[2] and sm.next_up_override is None
    assert sm.next_up_entry_id is None             # end of show
    _end_song(sm)
    sb = sm.standby()
    assert sb["action"] == "none" and sb["label"].startswith("End of show")


def test_next_up_clear_returns_to_computed(make_sm, lib):
    folder, _ = lib
    sm = make_sm()
    sm.open_folder(folder)
    ids = [e.id for e in sm.show.entries]
    sm.load_entry(ids[0])
    assert sm.next_up_entry_id == ids[1]
    sm.set_next_up(ids[2])
    assert sm.next_up_entry_id == ids[2]
    sm.set_next_up(None)
    assert sm.next_up_entry_id == ids[1]
    assert sm.set_next_up("nope") is False


def test_standby_skips_cut_and_divider(make_sm, lib):
    folder, (a, b, c) = lib
    sm = make_sm()
    sm.open_folder(folder)
    ids = [e.id for e in sm.show.entries]
    sm.add_divider("Act 2", index=1)
    sm.set_entry_cut(ids[1], True)
    sm.load_entry(ids[0])
    _end_song(sm)
    assert sm.standby()["label"] == "GO: next song - c Three"
    sm.rename_entry(ids[2], "Finale")
    assert sm.standby()["label"] == "GO: next song - Finale"


def test_manual_load_matches_show_entry_and_reprise(make_sm, lib):
    folder, (a, b, c) = lib
    sm = make_sm()
    sm.open_folder(folder)
    ids = [e.id for e in sm.show.entries]
    reprise = sm.duplicate_entry(ids[0])
    sm.move_entry(reprise, 3)                      # a, b, c, a(reprise)
    assert sm.load_song(b)
    assert sm.current_entry_id == ids[1]
    assert sm.load_song(a)                         # first match after current = reprise
    assert sm.current_entry_id == reprise
    assert sm.load_song(a)                         # reloading keeps the current entry
    assert sm.current_entry_id == reprise


def test_remove_current_entry_keeps_place(make_sm, lib):
    folder, _ = lib
    sm = make_sm()
    sm.open_folder(folder)
    ids = [e.id for e in sm.show.entries]
    sm.load_entry(ids[1])
    sm.remove_entry(ids[1])
    assert sm.current_entry_id is None
    assert sm.next_up_entry_id == ids[2]


def test_segue_auto_advances_like_go(make_sm, lib):
    folder, (a, b, c) = lib
    sm = make_sm()
    sm.open_folder(folder)
    ids = [e.id for e in sm.show.entries]
    sm.set_entry_segue(ids[0], True)
    sm.load_entry(ids[0])
    sm.set_live_mode(True)
    rec = Recorder(sm, "go_next_song", "go_next_entry", "go_fired")
    sent = []
    sm.osc.send_event = lambda _event, **kw: sent.append((_event, kw))
    sm.play()
    sm.audio.advance(60.0)                         # ends naturally
    assert rec.args("go_next_song") == [(b,)]
    assert rec.args("go_next_entry") == [(ids[1],)]
    fired = rec.args("go_fired")[-1][0]
    assert fired["source"] == "segue" and fired["entry_id"] == ids[1]
    assert sm.go_log[-1]["source"] == "segue"
    assert any(n == "go" and kw.get("source") == "segue" for n, kw in sent)
    # no segue flag on b: ending it waits for GO
    assert sm.load_song(b, from_go=True)
    sm.play_after_go_load()
    sm.audio.advance(60.0)
    assert len(rec.args("go_next_song")) == 1


def test_backward_compat_next_song_provider_with_empty_show(make_sm, lib):
    folder, (a, b, c) = lib
    sm = make_sm()
    sm.next_song_provider = lambda: b
    rec = Recorder(sm, "go_next_song", "go_next_entry")
    sm.load_song(a)
    _end_song(sm)
    assert sm.standby()["label"] == "GO: next song - b Two"
    assert sm.go()
    assert rec.args("go_next_song") == [(b,)] and rec.args("go_next_entry") == []
    sm.next_song_provider = lambda: None
    sm.load_song(b)
    _end_song(sm)
    assert sm.standby()["label"].startswith("End of song list")


def test_preload_next_up_with_saved_tempo(make_sm, lib):
    folder, (a, b, c) = lib
    engine = PreloadEngine()
    sm = make_sm(engine)
    # b's saved tempo/key, saved like any song setting
    assert sm.load_song(b)
    sm.settings.tempo_ratio, sm.settings.semitones = 0.9, -2.0
    sm.save_loop(immediate=True)
    sm.open_folder(folder)
    ids = [e.id for e in sm.show.entries]
    engine.preload_called.clear()
    assert sm.load_entry(ids[0])
    assert engine.preload_called.wait(5.0)
    _wait_preload(sm)
    assert engine.preloads[-1] == (b, None, (0.9, -2.0))
    assert sm.get_preloaded_audio(b) == "MIX"

    rec = Recorder(sm, "render_state")
    n_renders = len(engine.calls_to("set_tempo_pitch"))
    assert sm.load_entry(ids[1])
    assert engine.calls_to("load_file")[-1][2] == {"tempo_pitch": (0.9, -2.0)}
    assert len(engine.calls_to("set_tempo_pitch")) == n_renders   # already rendered
    assert rec.args("render_state") == []
    assert sm.get_tempo_pitch() == (0.9, -2.0)
    _wait_preload(sm)
    assert engine.preloads[-1][0] == c


def test_preload_events_are_forwarded(make_sm):
    sm = make_sm(PreloadEngine())
    rec = Recorder(sm, "preload_ready", "preload_failed")
    sm.audio._fire("preload_ready", {"path": "/x/a.wav"})
    sm.audio._fire("preload_failed", {"path": "/x/b.wav", "error": "decode error"})
    assert rec.args("preload_ready") == [("/x/a.wav",)]
    assert rec.args("preload_failed") == [("/x/b.wav", "decode error")]


class OldEngine(FakeEngine):
    """An engine from before the preload API: two-argument load_file, no preload_*."""
    preload_file = cancel_preload = get_preloaded_audio = is_preloaded = None

    def load_file(self, path, stem_paths=None):
        return super().load_file(path, stem_paths)


def test_engine_without_preload_still_loads(make_sm, lib):
    folder, _ = lib
    sm = make_sm(OldEngine())
    sm.open_folder(folder)
    assert sm.load_entry(sm.show.entries[0].id)
    assert sm.get_preloaded_audio(sm.show.entries[1].path) is None


def test_shared_fake_engine_preload_skips_load_time_render(make_sm, lib):
    folder, (a, b, c) = lib
    engine = FakeEngine()
    if not hasattr(engine, "preload_file"):
        pytest.skip("FakeEngine has no preload API yet")
    sm = make_sm(engine)
    assert sm.load_song(b)
    sm.settings.tempo_ratio, sm.settings.semitones = 1.1, 3.0
    sm.save_loop(immediate=True)
    sm.open_folder(folder)
    ids = [e.id for e in sm.show.entries]
    assert sm.load_entry(ids[0])
    _wait_preload(sm)
    assert engine.calls_to("preload_file")[-1][1][0] == b
    n = len(engine.calls_to("set_tempo_pitch"))
    assert sm.load_entry(ids[1])
    assert engine.loaded_from_preload
    assert len(engine.calls_to("set_tempo_pitch")) == n
    assert sm.get_tempo_pitch() == (1.1, 3.0)
    assert sm.pending_tempo_pitch is None and sm._saved_tempo_pitch is None
