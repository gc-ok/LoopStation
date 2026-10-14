"""StateManager driven against the REAL AudioEngine (synchronous fake stream)."""
import time

import pytest

from backend.audio_engine import AudioEngine
from backend.state_manager import StateManager, PlaybackState

from helpers_audio import find_ffmpeg, make_signal, write_wav, fake_stream_factory, wait_until

FFMPEG = find_ffmpeg()
pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not available")
SR = 44100


@pytest.fixture
def rig(tmp_path):
    streams = []
    engine = AudioEngine(ffmpeg_path=FFMPEG, stream_factory=fake_stream_factory(False, streams))
    sm = StateManager(ffmpeg_path=FFMPEG, data_file=str(tmp_path / "d.json"), engine=engine)
    song = write_wav(tmp_path / "song.wav", make_signal(12.0))
    assert sm.load_song(song)
    events = []
    for name in ("loop_mode_enter", "loop_mode_exit", "loop_cycle", "hold_reached",
                 "hold_released", "go_fired", "state_change", "song_ended"):
        sm.on(name, lambda *a, n=name: events.append((n, a)))

    def run(seconds):
        """Render audio and let the event thread + monitor catch up."""
        stream = streams[-1]
        end = int(seconds * SR)
        while end > 0:
            n = min(end, 4410)
            stream.pump(n)
            end -= n
            sm._monitor_tick(emit=False)
        time.sleep(0.05)

    yield sm, engine, run, events
    sm.stop()
    sm.cleanup()


def names(events):
    return [n for n, _ in events]


def test_vamp_with_count_then_continue(rig):
    sm, engine, run, events = rig
    sm.add_loop(2.0, 3.0, name="Vamp")
    sm.update_selected_loop(repeat_count=2)
    sm.play_from(1.0)
    sm._monitor_tick(emit=False)        # arm
    run(2.2)                            # 1.0 -> 3.2: reaches loop_out, wraps
    assert wait_until(lambda: "loop_mode_enter" in names(events))
    run(1.0)                            # second pass ends -> auto exit
    assert wait_until(lambda: "loop_mode_exit" in names(events))
    assert engine.mode == "transport"
    assert 3.0 < engine.get_position() < 4.0
    assert sm.state == PlaybackState.PLAYING


def test_go_exits_indefinite_vamp(rig):
    sm, engine, run, events = rig
    sm.add_loop(2.0, 3.0, name="Vamp")
    sm.play_from(1.5)
    sm._monitor_tick(emit=False)
    run(2.5)                            # looping for a while
    assert wait_until(lambda: engine.mode == "loop")
    assert sm.standby()["action"] == "exit_vamp"
    assert sm.go("test")
    run(1.2)                            # next wrap -> continue from 3.0
    assert wait_until(lambda: engine.mode == "transport")
    assert engine.get_position() > 3.0
    assert "go_fired" in names(events)


def test_hold_and_go_release(rig):
    sm, engine, run, events = rig
    sm.add_hold(2.0, name="Caesura")
    sm.play_from(1.0)
    sm._monitor_tick(emit=False)
    run(1.5)
    assert wait_until(lambda: sm.state == PlaybackState.HELD)
    held_at = engine.get_position()
    assert held_at == pytest.approx(2.0, abs=0.01)
    run(0.5)                            # silence, position frozen
    assert engine.get_position() == pytest.approx(held_at, abs=0.001)
    assert sm.standby()["action"] == "release_hold"
    assert sm.go("test")
    run(0.5)
    assert wait_until(lambda: sm.state == PlaybackState.PLAYING)
    assert engine.get_position() > 2.3


def test_go_from_stopped_plays_and_debounces(rig):
    sm, engine, run, events = rig
    assert sm.standby()["action"] == "play"
    assert sm.go("test")
    assert not sm.go("test")            # second press within debounce
    run(0.3)
    assert sm.state == PlaybackState.PLAYING
    assert engine.get_position() > 0.2
    assert len(sm.go_log) == 1


@pytest.mark.parametrize('hold_time', [2.99, 3.0])
def test_hold_near_vamp_end_is_reached_then_go_enters_vamp(rig, hold_time):
    sm, engine, run, events = rig
    sm.add_loop(1, 3, name='Vamp')
    sm.add_hold(hold_time)
    sm.play_from(2.9)
    run(0.5)
    assert wait_until(lambda: sm.state == PlaybackState.HELD)
    assert engine.get_position() == pytest.approx(hold_time, abs=1 / SR)
    assert sm.go('test')
    run(0.5)
    assert engine.mode == 'loop' and not engine.is_held


def test_device_loss_watchdog_restarts_real_engine(tmp_path):
    """Real engine + real StateManager: a stream that dies mid-show is restarted."""
    streams = []
    engine = AudioEngine(ffmpeg_path=FFMPEG, stream_factory=fake_stream_factory(False, streams))
    sm = StateManager(ffmpeg_path=FFMPEG, data_file=str(tmp_path / "d.json"), engine=engine)
    try:
        assert sm.load_song(write_wav(tmp_path / "s.wav", make_signal(6.0)))
        seen = []
        for n in ("device_lost", "device_restored"):
            sm.on(n, lambda *a, n=n: seen.append(n))
        sm.play_from(1.0)
        streams[-1].pump(4410)
        pos_before = engine.get_position()
        streams[-1].fail()                     # interface unplugged
        assert wait_until(lambda: "device_restored" in seen, timeout=3.0)
        assert "device_lost" in seen
        assert len(streams) >= 2               # a new stream was opened
        assert sm.state == PlaybackState.PLAYING
        assert engine.get_position() == pytest.approx(pos_before, abs=0.05)
        streams[-1].pump(4410)
        assert engine.get_position() > pos_before + 0.05
    finally:
        sm.stop()
        sm.cleanup()
