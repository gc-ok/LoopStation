"""Main/backup mirroring (backend/mirror.py) over localhost with two StateManagers."""
import json
import os
import socket
import threading
import time

import pytest

from backend import mirror as mirror_mod
from backend.mirror import MirrorLink, LineReader, decode, encode
from backend.state_manager import StateManager, PlaybackState
from utils import preferences

from fake_engine import FakeEngine
from helpers_audio import make_signal, write_wav

PIN = "4321"


def wait_until(cond, timeout=4.0, step=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(step)
    return cond()


@pytest.fixture(autouse=True)
def prefs_file(tmp_path, monkeypatch):
    path = str(tmp_path / "prefs" / "user_preferences.json")
    os.makedirs(os.path.dirname(path))
    monkeypatch.setattr(preferences, "PREFS_FILE", path)
    monkeypatch.setattr(mirror_mod, "RECONNECT_DELAYS", (0.05,))
    return path


def _song(folder, name, secs, freq):
    os.makedirs(folder, exist_ok=True)
    return write_wav(os.path.join(folder, name), make_signal(secs, sr=8000, freq=freq), sr=8000)


@pytest.fixture
def music(tmp_path):
    folder = str(tmp_path / "music")
    return [_song(folder, n, 20.0, 300 + 40 * i)
            for i, n in enumerate(["01 Opening.wav", "02 Ballad.wav", "03 Finale.wav"])]


def fast_link(sm, **kw):
    opts = dict(heartbeat_s=0.04, lost_s=0.5, ping_s=0.1, guard_s=0.0, tick_s=0.02)
    opts.update(kw)
    sm.mirror = MirrorLink(sm, **opts)
    return sm.mirror


@pytest.fixture
def make_sm(tmp_path, monkeypatch):
    monkeypatch.setattr(StateManager, "_start_monitor", lambda self: None)
    made = []

    def _make(name):
        data = tmp_path / f"data-{name}"
        data.mkdir()
        sm = StateManager(ffmpeg_path="no-ffmpeg", data_file=str(data / "loop_data.json"),
                          engine=FakeEngine())
        made.append(sm)
        return sm
    yield _make
    for sm in made:
        sm.mirror.stop()
    for sm in made:
        sm.cleanup()


@pytest.fixture
def pair(make_sm, music, tmp_path):
    """Main + backup on the same .loopshow (same entry ids), connected."""
    main, backup = make_sm("main"), make_sm("backup")
    main.new_show("Show")
    ids = main.add_songs(music)
    show_file = str(tmp_path / "show.loopshow")
    assert main.save_show(show_file)
    assert backup.open_show(show_file)
    fast_link(main)
    fast_link(backup)
    assert main.mirror.start("main", port=0, pin=PIN)
    assert backup.mirror.start("backup", "127.0.0.1", port=main.mirror.bound_port, pin=PIN)
    assert wait_until(lambda: backup.mirror.status()["connected"]
                      and main.mirror.status()["peers"] == 1)
    return main, backup, ids


def load_both(main, backup, entry_id):
    assert main.load_entry(entry_id, from_go=True)
    assert wait_until(lambda: backup.current_entry_id == entry_id
                      and backup.current_song_name == main.current_song_name)


# ------------------------------------------------------------ protocol --

def test_line_reader_drops_oversized_and_decode_rejects_bad():
    r = LineReader(cap=10)
    assert r.feed(b"short\n" + b"x" * 30) == [b"short"]
    assert r.feed(b"yyyy\nok\n") == [b"ok"]          # rest of the long line discarded
    assert r.dropped == 1
    assert decode(b'{"type":"hb","pos":NaN}') is None
    assert decode(b"[1,2]") is None and decode(b"\xff\xfe") is None
    assert decode(b'{"type":"x"}') == {"type": "x"}
    with pytest.raises(ValueError):
        encode({"type": "hb", "pos": float("nan")})


def test_handshake_status_and_wrong_pin_rejected(pair, make_sm):
    main, backup, _ = pair
    st = backup.mirror.status()
    assert st["role"] == "backup" and st["following"] and st["show_match"] == "same"
    assert st["message"].startswith("Following main")
    assert main.mirror.status()["role"] == "main" and "1 backup connected" in main.mirror.status()["message"]

    intruder = make_sm("intruder")
    fast_link(intruder)
    intruder.mirror.start("backup", "127.0.0.1", port=main.mirror.bound_port, pin="0000")
    assert wait_until(lambda: "rejected the PIN" in intruder.mirror.status()["message"])
    assert not intruder.mirror.status()["connected"]
    assert main.mirror.status()["peers"] == 1


def test_settings_persisted_without_auto_start(pair):
    saved = mirror_mod.load_settings()
    assert saved["role"] == "backup" and saved["pin"] == PIN and saved["peer_host"] == "127.0.0.1"
    assert saved["auto_start"] is False


def test_on_status_called_on_worker_thread(make_sm):
    sm = make_sm("solo")
    link = fast_link(sm)
    seen = []
    link.on_status = lambda s: seen.append((threading.current_thread().name, s))
    assert link.start("main", port=0, pin="")
    assert wait_until(lambda: any(s["role"] == "main" for _, s in seen))
    name, st = [x for x in seen if x[1]["role"] == "main"][-1]
    assert name == "mirror-status" and "no PIN set" in st["message"]


def test_main_port_in_use_reports_error(make_sm):
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("0.0.0.0", 0))
    blocker.listen(1)
    try:
        sm = make_sm("m")
        link = fast_link(sm)
        assert not link.start("main", port=blocker.getsockname()[1])
        st = link.status()
        assert st["role"] == "off" and "Could not listen" in st["message"]
    finally:
        blocker.close()


# -------------------------------------------------------- mirrored actions --

def test_go_play_pause_stop_seek_load_mirrored(pair):
    main, backup, ids = pair
    load_both(main, backup, ids[0])
    assert main.go("ui")
    assert wait_until(lambda: backup.state == PlaybackState.PLAYING)
    main.audio.advance(1.0)
    main.pause()
    assert wait_until(lambda: backup.state == PlaybackState.PAUSED)
    assert backup.audio.get_position() == pytest.approx(1.0, abs=0.01)
    main.seek(5.0)
    assert wait_until(lambda: abs(backup.audio.get_position() - 5.0) < 0.01)
    main.play()
    assert wait_until(lambda: backup.state == PlaybackState.PLAYING)
    main.stop()
    assert wait_until(lambda: backup.state == PlaybackState.STOPPED)
    load_both(main, backup, ids[2])
    assert backup.current_song_name == "03 Finale.wav"


def test_vamp_exit_fade_mirrored(pair):
    main, backup, ids = pair
    load_both(main, backup, ids[1])
    for sm in (main, backup):
        sm.add_loop(0.5, 1.5, name="Vamp")
    backup.mirror.drift_threshold_ms = 1e9      # positions are moved by hand here
    main.play()
    assert wait_until(lambda: backup.state == PlaybackState.PLAYING)
    for sm in (main, backup):
        sm.audio.advance(1.6)
        assert sm.audio.mode == "loop"
    main.queue_exit(fade_mode=True, fade_ms=750)
    assert wait_until(lambda: backup.exit_queue_active)
    assert backup.exit_fade_mode and backup.exit_fade_ms == 750
    calls = backup.audio.calls_to("queue_loop_exit")
    assert calls[-1][1] == ("fade",) and calls[-1][2] == {"fade_ms": 750}


def test_hold_release_arriving_before_backup_reaches_hold(pair):
    main, backup, ids = pair
    load_both(main, backup, ids[0])
    for sm in (main, backup):
        sm.add_hold(2.0)
    backup.mirror.drift_threshold_ms = 1e9
    main.play()
    assert wait_until(lambda: backup.state == PlaybackState.PLAYING)
    main.audio.advance(2.5)
    assert main.state == PlaybackState.HELD
    main.go("ui")                                 # releases the hold on main
    assert main.state == PlaybackState.PLAYING
    time.sleep(0.1)
    backup.audio.advance(2.5)                     # backup reaches the hold late
    assert wait_until(lambda: backup.state == PlaybackState.PLAYING)


def test_no_echo_mirrored_actions_are_quiet(pair):
    main, backup, ids = pair
    echoed = []
    backup._mirror_sink = lambda kind, data: echoed.append(kind)
    load_both(main, backup, ids[0])
    main.play()
    assert wait_until(lambda: backup.state == PlaybackState.PLAYING)
    main.stop()
    assert wait_until(lambda: backup.state == PlaybackState.STOPPED)
    assert echoed == []
    backup._mirror_sink = None


def test_mirrored_actions_apply_in_live_mode(pair):
    main, backup, ids = pair
    main.set_live_mode(True)
    assert wait_until(lambda: backup.live_mode)
    load_both(main, backup, ids[1])               # load_song is locked in Live Mode otherwise
    main.seek(3.0)                                # internal seek: the main's own lock is the UI's
    assert wait_until(lambda: abs(backup.audio.get_position() - 3.0) < 0.01)
    main.set_live_mode(False)
    main.set_tempo_pitch(0.9, 1.0)
    main.set_live_mode(True)
    assert wait_until(lambda: backup.audio.get_tempo_pitch() == (0.9, 1.0))
    assert main.set_next_up(ids[0])
    assert wait_until(lambda: backup.next_up_entry_id == ids[0])


# ----------------------------------------------------------- drift / align --

def test_drift_correction_seeks_only_when_both_in_transport(pair):
    main, backup, ids = pair
    load_both(main, backup, ids[0])
    main.play()
    assert wait_until(lambda: backup.state == PlaybackState.PLAYING)
    main.audio.advance(0.5)                       # backup is 500 ms behind
    assert wait_until(lambda: abs(backup.audio.get_position() - 0.5) < 0.05)
    assert backup.audio.calls_to("seek_transport")
    # small drift (< 40 ms) is left alone
    before = len(backup.audio.calls_to("seek_transport"))
    main.audio.advance(0.02)
    time.sleep(0.3)
    assert len(backup.audio.calls_to("seek_transport")) == before


def test_no_drift_seek_during_vamp_or_guard(pair):
    main, backup, ids = pair
    load_both(main, backup, ids[1])
    main.add_loop(0.5, 1.5, name="Vamp")
    main.play()
    assert wait_until(lambda: backup.state == PlaybackState.PLAYING)
    main.audio.advance(1.7)
    assert main.audio.mode == "loop"
    seeks = len(backup.audio.calls_to("seek_transport"))
    time.sleep(0.4)
    assert len(backup.audio.calls_to("seek_transport")) == seeks
    assert backup.mirror.status()["drift_ms"] is None
    # guard: within guard_s of a discrete action nothing is corrected
    main.stop()
    assert wait_until(lambda: backup.state == PlaybackState.STOPPED)
    backup.mirror.guard_s = 30.0
    main.play()
    assert wait_until(lambda: backup.state == PlaybackState.PLAYING)
    main.audio.advance(0.3)
    time.sleep(0.4)
    assert backup.audio.get_position() == pytest.approx(0.0, abs=0.01)


def test_backup_loads_main_song_from_heartbeat(pair):
    main, backup, ids = pair
    backup.mirror.stop()
    load_both_main_only = main.load_entry(ids[2], from_go=True)
    assert load_both_main_only
    main.play()
    main.audio.advance(2.0)
    assert backup.mirror.start("backup", "127.0.0.1", port=main.mirror.bound_port, pin=PIN)
    assert wait_until(lambda: backup.current_entry_id == ids[2]
                      and backup.state == PlaybackState.PLAYING
                      and abs(backup.audio.get_position() - 2.0) < 0.05)


def test_main_lost_keeps_backup_playing_and_reconnect_realigns(pair):
    main, backup, ids = pair
    load_both(main, backup, ids[0])
    main.play()
    assert wait_until(lambda: backup.state == PlaybackState.PLAYING)
    port = main.mirror.bound_port
    # 1. heartbeats stop (main wedged) -> MAIN LOST by timeout
    main.mirror.heartbeat_s = 100.0
    assert wait_until(lambda: "MAIN LOST" in backup.mirror.status()["message"])
    st = backup.mirror.status()
    assert not st["connected"] and st["following"]
    assert backup.state == PlaybackState.PLAYING
    # 2. main goes away entirely; it pauses meanwhile
    main.mirror.stop()
    time.sleep(0.2)
    assert backup.state == PlaybackState.PLAYING
    main.audio.advance(1.0)
    main.pause()
    fast_link(main)
    assert main.mirror.start("main", port=port, pin=PIN)
    assert wait_until(lambda: backup.mirror.status()["connected"])
    assert wait_until(lambda: backup.state == PlaybackState.PAUSED
                      and abs(backup.audio.get_position() - 1.0) < 0.05)


def test_take_over_stops_following(pair):
    main, backup, ids = pair
    load_both(main, backup, ids[0])
    main.play()
    assert wait_until(lambda: backup.state == PlaybackState.PLAYING)
    assert backup.mirror.take_over()
    st = backup.mirror.status()
    assert st["role"] == "backup" and not st["following"] and st["message"].startswith("Took over")
    main.pause()
    main.stop()
    time.sleep(0.3)
    assert backup.state == PlaybackState.PLAYING
    assert wait_until(lambda: main.mirror.status()["peers"] == 0)
    assert main.state == PlaybackState.STOPPED


def test_backup_disconnect_never_affects_main(pair):
    main, backup, ids = pair
    load_both(main, backup, ids[0])
    main.play()
    backup.mirror.stop()
    assert wait_until(lambda: main.mirror.status()["peers"] == 0)
    assert main.state == PlaybackState.PLAYING


def test_show_mismatch_message_and_follow_by_file_name(make_sm, music, tmp_path):
    main, backup = make_sm("main"), make_sm("backup")
    main.new_show("Main show")
    main_ids = main.add_songs(music)
    backup.new_show("Other")
    backup.add_songs([music[2], music[0]])        # different order, different ids
    fast_link(main)
    fast_link(backup)
    main.mirror.start("main", port=0, pin="")
    backup.mirror.start("backup", "127.0.0.1", port=main.mirror.bound_port, pin="")
    assert wait_until(lambda: backup.mirror.status()["connected"])
    st = backup.mirror.status()
    assert st["show_match"] == "differs" and "show differs" in st["message"]
    assert main.load_entry(main_ids[2], from_go=True)
    assert wait_until(lambda: backup.current_song_name == "03 Finale.wav")
    assert main.load_entry(main_ids[1], from_go=True)    # not in the backup's show: same folder
    assert wait_until(lambda: backup.current_song_name == "02 Ballad.wav")


def test_backup_ignores_malformed_oversized_and_unknown(make_sm, music):
    backup = make_sm("backup")
    backup.new_show("S")
    backup.add_songs(music)
    assert backup.load_song(music[0])
    backup.play()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(5)
    fast_link(backup, lost_s=5.0)
    backup.mirror.start("backup", "127.0.0.1", port=server.getsockname()[1], pin="")
    conn, _ = server.accept()
    try:
        conn.settimeout(5)
        hello = json.loads(conn.recv(65536).split(b"\n")[0])
        assert hello["type"] == "hello" and hello["proto"] == mirror_mod.PROTO
        conn.sendall(encode({"type": "welcome", "proto": 1, "show": None, "live_mode": False}))
        conn.sendall(b"garbage\n[1]\n" + b'{"type":"act","kind":"stop","x":"' + b"a" * 70000 + b'"}\n')
        conn.sendall(b'{"type":"act","kind":"seek","position":NaN}\n')
        conn.sendall(encode({"type": "act", "kind": "self_destruct"}))
        conn.sendall(encode({"type": "act", "kind": "seek", "position": "12"}))
        conn.sendall(encode({"type": "wat"}))
        time.sleep(0.3)
        assert backup.state == PlaybackState.PLAYING          # nothing applied
        assert backup.audio.get_position() == pytest.approx(0.0, abs=0.01)
        conn.sendall(encode({"type": "act", "kind": "pause", "position": 0.0}))
        assert wait_until(lambda: backup.state == PlaybackState.PAUSED)
    finally:
        conn.close()
        server.close()


def test_main_ignores_garbage_before_hello(pair):
    main, _, _ = pair
    sock = socket.create_connection(("127.0.0.1", main.mirror.bound_port), timeout=3)
    try:
        sock.sendall(b"nonsense\n" + b"z" * 70000 + b"\n")
        sock.sendall(encode({"type": "hello", "proto": 1, "pin": PIN}))
        reply = b""
        while b"\n" not in reply:
            reply += sock.recv(65536)
        assert json.loads(reply.split(b"\n")[0])["type"] == "welcome"
    finally:
        sock.close()
