"""Regression tests for the audio review fixes (mixer core, engine, loop detector)."""
import queue
import threading

import numpy as np
import pytest

from backend import audio_engine
from backend.audio_engine import AudioEngine, MixerCore, build_tempo_pitch_filter
from backend.loop_detector import LoopDetector
from config import MIN_LOOP_DURATION

from helpers_audio import make_signal, write_wav, wait_until, fake_stream_factory

SR = 44100
NO_FFMPEG = "definitely-not-an-ffmpeg-binary"


# ================================================================ helpers ==

def make_core(stems, declick_ms=0):
    core = MixerCore(SR, declick_ms)
    core.set_stems(stems)
    return core


def run(core, frames, block=512):
    out = []
    left = frames
    while left > 0:
        n = min(block, left)
        out.append(core.render(n))
        left -= n
    return np.concatenate(out)


def drain(core):
    evs = []
    while True:
        try:
            evs.append(core.events.get_nowait())
        except queue.Empty:
            return evs


def names(evs):
    return [e[0] for e in evs]


def sine(seconds, freq=220.0, amp=12000):
    t = np.arange(int(seconds * SR)) / SR
    s = (np.sin(2 * np.pi * freq * t) * amp).astype(np.int16)
    return np.stack([s, s], axis=1)


def counter(seconds):
    """Each sample identifies its frame (left = frame % 30000)."""
    n = int(seconds * SR)
    left = (np.arange(n) % 30000) - 15000
    return np.stack([left, left], axis=1).astype(np.int16)


def max_step(x):
    return float(np.max(np.abs(np.diff(x[:, 0]))))


# ======================================== 1: editing the playing loop ==

def _core_in_loop(sig, declick_ms=10):
    core = make_core({"mix": sig}, declick_ms)
    core.arm_loop(1.0, 3.0, 20)
    core.play(2.5)
    run(core, SR)                  # entered at 3.0 s, now 0.5 s into the body
    assert core.mode == "loop"
    return core


def test_edit_playing_loop_keeps_song_sample():
    core = _core_in_loop(counter(5.0), declick_ms=0)
    before = core.get_position()
    core.set_loop_points(0.8, 3.0, 20)       # still contains the playhead
    assert core.get_position() == pytest.approx(before, abs=1.0 / SR)
    assert core.loop_in == 0.8 and core.mode == "loop"
    out = core.render(64)
    frame = int(round(before * SR))
    assert np.allclose(out[:, 0] * 32768, (np.arange(frame, frame + 64) % 30000) - 15000)


def test_edit_playing_loop_outside_playhead_restarts_at_new_in():
    core = _core_in_loop(counter(5.0), declick_ms=0)
    core.set_loop_points(2.0, 3.0, 20)       # playhead (1.5 s) is outside
    assert core.get_position() == pytest.approx(2.0)
    assert core.mode == "loop"


def test_edit_playing_loop_is_declicked():
    sig = sine(5.0)
    core = _core_in_loop(sig)
    a = core.render(512)
    core.set_loop_points(1.70114, 3.0, 20)   # restart 1/4 period out of phase
    b = core.render(512)
    natural = max_step(sig.astype(np.float32) / 32768)
    assert max_step(np.concatenate([a, b])) <= natural * 1.5


def test_reported_points_follow_the_playing_bodies_while_building(monkeypatch):
    core = _core_in_loop(sine(5.0))
    seen = []
    real = core._build_bodies

    def spy(*args):
        seen.append(core.get_loop_points())
        return real(*args)
    monkeypatch.setattr(core, "_build_bodies", spy)
    core.set_loop_points(1.2, 3.0, 20)
    assert seen and seen[0] == (1.0, 3.0, 20)   # old bodies still playing
    assert core.get_loop_points() == (1.2, 3.0, 20)


# ======================================== 2: loop detector phase score ==

def _sine_region():
    # 220.5 Hz at 44.1 kHz: period exactly 200 samples
    t = np.arange(SR * 4) / SR
    s = (np.sin(2 * np.pi * 220.5 * t) * 16000).astype(np.int16)
    det = LoopDetector(np.stack([s, s], 1), SR)
    return det, det._mono_region(0, 4)


def test_phase_score_exact_period_high_half_period_low():
    det, region = _sine_region()
    s = 1001
    assert det._loop_phase_score(region, s, s + 200 * 500, 100) > 0.99
    assert det._loop_phase_score(region, s, s + 200 * 500 + 100, 100) < 0.1
    # at the region end the windows before both points are compared
    e = len(region) - 50
    s2 = e - 200 * 700
    assert det._loop_phase_score(region, s2, e, 100) > 0.99


def test_zero_crossing_loops_prefer_whole_periods():
    det, region = _sine_region()
    cands = det._find_zero_crossing_loops(region, 0.0)
    assert cands
    for c in cands:
        whole = round((c.end - c.start) * SR) % 200 == 0
        assert (c.confidence >= 99) == whole


# ==================================== 3: short loops / guard / events ==

def test_loops_shorter_than_min_duration_are_rejected():
    core = make_core({"mix": sine(3.0)})
    core.set_loop_points(1.0, 1.0 + MIN_LOOP_DURATION * 0.5)
    assert not core.is_loop_ready()
    core.set_loop_points(2.8, 4.0)           # only 0.2 s inside the song
    assert not core.is_loop_ready()
    core.set_loop_points(1.0, 1.0 + MIN_LOOP_DURATION)
    assert core.is_loop_ready()


def test_tiny_armed_loop_never_stops_silently():
    core = make_core({"mix": sine(3.0)})
    core.arm_loop(1.0, 1.0001, 20)
    core.play(0.99)
    run(core, 4096)
    assert core.is_playing and core.mode == "transport"


def test_many_wraps_in_one_block_do_not_trip_the_guard():
    core = make_core({"mix": sine(3.0)})
    core.arm_loop(1.0, 1.5, 20)
    core.play(1.4)
    core.render(int(SR * 0.5 * 70))          # ~70 wraps in a single block
    evs = names(drain(core))
    assert core.is_playing and core.mode == "loop"
    assert "stopped" not in evs and evs.count("loop_cycle") >= 69


@pytest.mark.parametrize("in_loop", [False, True])
def test_guard_trip_reports_a_stop(monkeypatch, in_loop):
    core = make_core({"mix": sine(3.0)})
    core.set_loop_points(1.0, 2.0)
    core.play(1.2)
    if in_loop:
        assert core.enter_loop()
    run(core, 512)
    drain(core)
    monkeypatch.setattr(core, "_next_event", lambda: (0, "noop"))
    monkeypatch.setattr(core, "_handle_event", lambda kind: None)
    core.render(512)
    evs = drain(core)
    expected = (["loop_exited"] if in_loop else []) + ["stopped"]
    assert names(evs) == expected
    assert evs[-1][1]["reason"] == "error"
    assert not core.is_playing and core.mode == "transport"


# ================================== 4: manual entry in the blended head ==

def test_enter_loop_in_head_crossfade_is_faded():
    core = make_core({"mix": sine(4.0)}, declick_ms=10)
    core.set_loop_points(1.0, 3.0, 20)
    core.play(0.5)
    run(core, SR // 2 + 100)                 # 100 frames past loop_in (cf = 882)
    assert core.enter_loop()
    assert core._fadein_total > 0 and core._tail_mix is not None


def test_enter_loop_past_head_crossfade_is_seamless():
    core = make_core({"mix": sine(4.0)}, declick_ms=10)
    core.set_loop_points(1.0, 3.0, 20)
    core.play(0.5)
    run(core, SR)                            # 0.5 s past loop_in
    assert core.enter_loop()
    assert core._fadein_total == 0 and core._tail_mix is None


# ======================================== 5: routing before the reopen ==

def test_render_uses_requested_channel_count():
    core = make_core({"mix": sine(1.0), "click": sine(1.0, 1000)})
    core.set_routing("multichannel")
    core.play(0.0)
    assert core.render(256).shape == (256, 4)
    assert core.render(256, 2).shape == (256, 2)
    core.set_routing("stereo")
    assert core.render(256, 4).shape == (256, 4)


@pytest.fixture
def engine_rig(tmp_path, monkeypatch):
    root = tmp_path / "systmp"
    root.mkdir()
    monkeypatch.setattr(audio_engine.tempfile, "gettempdir", lambda: str(root))
    streams = []
    eng = AudioEngine(ffmpeg_path=NO_FFMPEG, stream_factory=fake_stream_factory(False, streams))
    yield eng, streams, tmp_path
    eng.close()


def test_routing_change_before_stream_reopen_is_not_a_device_error(engine_rig):
    eng, streams, tmp_path = engine_rig
    eng.load_file(write_wav(tmp_path / "song.wav", make_signal(1.0)))
    eng.play_transport(0.0)
    drain(eng.core)
    eng.core.set_routing("multichannel")     # core changed, stream still 2 ch
    out = streams[-1].pump(1024)
    assert out.shape[1] == 2 and np.abs(out).max() > 0
    assert "device_error" not in names(drain(eng.core))
    assert not eng._callback_error_logged


# ============================================== 6: gain/mute ramps ==

def test_mute_ramps_over_the_declick_length():
    dc = np.full((SR, 2), 16384, dtype=np.int16)
    core = make_core({"mix": dc}, declick_ms=10)
    core.play(0.0)
    run(core, 1024)
    core.set_stem_mute("mix", True)
    out = core.render(1024)[:, 0]
    n = core.declick_frames
    assert out[0] > 0.45 and np.all(np.diff(out[:n]) < 0)
    assert np.all(out[n:] == 0)
    core.set_stem_mute("mix", False)
    out = core.render(1024)[:, 0]
    assert out[0] < 0.01 and out[n:].min() == pytest.approx(0.5)


def test_gain_change_without_declick_is_immediate():
    dc = np.full((SR, 2), 16384, dtype=np.int16)
    core = make_core({"mix": dc}, declick_ms=0)
    core.play(0.0)
    core.render(256)
    core.set_stem_gain("mix", -6.0206)
    assert core.render(16)[0, 0] == pytest.approx(0.25, abs=1e-3)


def test_source_swap_crossfades_under_the_lock():
    dc = np.full((SR, 2), 8192, dtype=np.int16)
    core = make_core({"mix": dc, "drums": dc}, declick_ms=10)   # drums default muted
    core.play(0.0)
    run(core, 1024)
    with core.lock:
        core.set_stem_mute("mix", True)
        core.set_stem_mute("drums", False)
    out = core.render(1024)[:, 0]
    assert np.allclose(out, 0.25, atol=1e-3)      # equal levels: no dip, no bump


# =============================== 7: due events before a tempo swap ==

def _rounding_trap(n_src, hold_s):
    """(n1, n2): after a swap to n1 frames the hold frame, re-rounded for a
    swap to n2, lands one frame BEFORE the playhead's re-rounded position."""
    for n1 in range(int(n_src * 1.2), int(n_src * 1.2) + 2000):
        h1 = round(hold_s * SR * n1 / n_src)
        for n2 in range(int(n_src * 0.85), int(n_src * 0.85) + 300):
            if round(h1 * n2 / n1) > round(hold_s * SR * n2 / n_src):
                return n1, n2
    raise AssertionError("no rounding trap found")


def _stretched(a, n):
    idx = np.minimum((np.arange(n) * len(a)) // n, len(a) - 1)
    return np.ascontiguousarray(a[idx])


def test_hold_exactly_at_block_end_survives_a_tempo_swap():
    src = sine(4.0)
    hold_s = 1.0
    n1, n2 = _rounding_trap(len(src), hold_s)
    core = make_core({"mix": src})
    core.swap_buffers({"mix": _stretched(src, n1)}, len(src) / n1, 0.0)
    core.apply_pending_swap()
    core.hold_at(hold_s)
    core.play(0.0)
    run(core, core._hold_f)                   # block ends exactly on the hold
    assert core._pos == core._hold_f and not core.is_held
    core.swap_buffers({"mix": _stretched(src, n2)}, len(src) / n2, 0.0)
    drain(core)
    core.render(512)
    assert "hold_reached" in names(drain(core)) and core.is_held


def test_entry_guard_is_rescaled_by_a_swap():
    src = sine(4.0)
    core = make_core({"mix": src})
    core.arm_loop(1.0, 2.0, 20)
    core.play(1.9)
    run(core, int(0.2 * SR))
    core.exit_loop_now()
    assert core._entry_min_frame == core._loop_out_f + 1
    n2 = int(len(src) * 1.25)
    core.swap_buffers({"mix": _stretched(src, n2)}, 0.8, 0.0)
    core.render(256)
    assert core.n_active_frames == n2
    assert core._entry_min_frame == core._loop_out_f + 1


# ================================= 8: click rendered like the music ==

def test_all_stems_use_one_filter_and_nominal_length(engine_rig, monkeypatch):
    eng, streams, tmp_path = engine_rig
    sig = make_signal(2.0)
    eng.load_file(write_wav(tmp_path / "song.wav", sig),
                  {"click": write_wav(tmp_path / "click.wav", sig)})
    tempo, semi = 0.8, 2.0
    nominal = int(round(len(sig) / tempo))
    lengths = {"mix": nominal - 37, "click": nominal + 25}   # ffmpeg rounding
    calls = []

    def fake_ffmpeg(args, out_path, what):
        name = what.split()[-1]
        calls.append((name, args[args.index('-af') + 1]))
        write_wav(out_path, np.ones((lengths[name], 2), dtype=np.int16))
        return out_path
    monkeypatch.setattr(eng, "_run_ffmpeg", fake_ffmpeg)
    eng._render_worker(eng._render_gen, tempo, semi)
    filters = dict(calls)
    assert filters["click"] == filters["mix"] == build_tempo_pitch_filter(tempo, semi, SR)
    eng.core.apply_pending_swap()
    assert eng.core.n_active_frames == nominal
    assert {len(s.active) for s in eng.core._stems.values()} == {nominal}
    # the padded tail is silence; the short original file is gone
    mix = eng.core._stems["mix"].active
    assert mix[nominal - 38, 0] == 1 and mix[-1, 0] == 0
    assert len(eng._render_paths) == 2


# ============================ 9: rebuild worker / prefetch spans ==

def test_render_never_starts_a_thread_for_a_stale_swap(monkeypatch):
    src = sine(6.0)
    core = make_core({"mix": src}, declick_ms=10)
    core.set_loop_points(1.0, 3.0, 20)
    core.play(0.0)
    core.swap_buffers({"mix": _stretched(src, int(len(src) * 1.25))}, 0.8, 0.0)
    pending = core._pending_swap
    with core.lock:
        core._crossfade_ms = 10          # bodies now stale
    started = []
    real_start = threading.Thread.start

    def spy(self):
        started.append(self.name)
        return real_start(self)
    monkeypatch.setattr(threading.Thread, "start", spy)
    core.render(512)
    assert started == []
    assert wait_until(lambda: pending.loop_key == (1.0, 3.0, 10), timeout=10.0)
    core.render(512)
    assert core._pending_swap is None and started == []
    core.shutdown()
    assert not core._rebuild_thread.is_alive()


def test_prefetch_spans_cover_playhead_and_exit():
    src = sine(6.0)
    core = make_core({"mix": src})
    assert core.prefetch_spans(1000) == []          # stopped
    core.set_skip_regions([(2.0, 4.0)])
    core.play(1.0)
    spans = [(a, b) for _buf, a, b in core.prefetch_spans(1000)]
    assert (SR, SR + 1000) in spans and (4 * SR, 4 * SR + 1000) in spans
    core.set_loop_points(1.0, 1.5)
    assert core.enter_loop()
    spans = [(a, b) for _buf, a, b in core.prefetch_spans(1000)]
    assert spans == [(int(1.5 * SR), int(1.5 * SR) + 1000)]


def _bursts(seconds, marks):
    x = np.zeros((int(seconds * SR), 2), np.int16)
    burst = (np.sin(2 * np.pi * 1000 * np.arange(441) / SR) * np.hanning(441) * 20000).astype(np.int16)
    for m in marks:
        i = int(m * SR)
        x[i:i + 441, 0] = burst
        x[i:i + 441, 1] = burst
    return x


def test_click_stays_aligned_with_music_after_real_render(tmp_path, monkeypatch):
    from helpers_audio import find_ffmpeg
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        pytest.skip("ffmpeg not available")
    root = tmp_path / "systmp"
    root.mkdir()
    monkeypatch.setattr(audio_engine.tempfile, "gettempdir", lambda: str(root))
    marks = [1.0, 5.0, 9.0]
    sig = _bursts(10.0, marks)
    eng = AudioEngine(ffmpeg_path=ffmpeg, stream_factory=fake_stream_factory(False, []))
    try:
        eng.load_file(write_wav(tmp_path / "song.wav", sig),
                      {"click": write_wav(tmp_path / "click.wav", sig)})
        eng.set_tempo_pitch(0.8, 3.0)
        assert wait_until(lambda: eng.core._pending_swap is not None, timeout=30.0)
        eng.core.apply_pending_swap()
        nominal = int(round(len(sig) / 0.8))
        stems = eng.core._stems
        assert {len(s.active) for s in stems.values()} == {nominal}
        for m in marks:
            centre = int(m * SR / 0.8)
            lo, hi = centre - SR // 4, centre + SR // 4
            pos = {}
            for name in ("mix", "click"):
                e = np.abs(np.asarray(stems[name].active[lo:hi, 0], dtype=np.float64))
                pos[name] = (e * np.arange(len(e))).sum() / e.sum()
            assert abs(pos["mix"] - pos["click"]) < 0.001 * SR
    finally:
        eng.close()
