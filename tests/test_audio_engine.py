"""Tests for backend/audio_engine.py and backend/mixer_core.py (no audio device needed)."""
import gc
import os
import queue
import struct
import subprocess
import threading
import time
import types

import numpy as np
import pytest

from backend import audio_engine
from backend.audio_engine import (
    AudioEngine, MixerCore, build_loop_body, parse_wav_data_chunk,
    sweep_stale_temp_files, atempo_chain, build_tempo_pitch_filter, TEMP_FILE_PREFIX,
)
from config import SAMPLE_RATE, CHANNELS

from helpers_audio import (
    find_ffmpeg, make_signal, write_wav, read_wav, wait_until, fake_stream_factory,
)

SR = SAMPLE_RATE
FFMPEG = find_ffmpeg()
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not available")
NO_FFMPEG = "definitely-not-an-ffmpeg-binary"


# ================================================================ helpers ==

def ramp(n, mod=30000):
    """int16 (n, 2) signal where every sample is easy to identify."""
    left = (np.arange(n) % mod) - mod // 2
    return np.stack([left, -left // 2], axis=1).astype(np.int16)


def f32(a):
    return np.asarray(a, dtype=np.float32) / 32768.0


def make_core(stems, declick_ms=0):
    core = MixerCore(SR, declick_ms)
    core.set_stems(stems)
    return core


def run(core, frames, block=512):
    """Render ``frames`` frames in callback-sized blocks."""
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


# Loop geometry used by the loop tests: 0.5 s .. 1.0 s, 20 ms crossfade
IN_F, OUT_F, CF = 22050, 44100, 882
BODY = OUT_F - IN_F               # full musical period, 22050 frames
EF = OUT_F                       # enter and leave at the exact loop end
START_F = 39690                   # play from 0.9 s
TO_EF = EF - START_F              # 4410 frames until entry
TO_WRAP = OUT_F - START_F         # 4410 frames until the first wrap (= loop_out)


@pytest.fixture
def loop_core():
    src = ramp(SR * 2)
    core = make_core({"mix": src})
    return core, src


# ============================================================ pure helpers ==

def test_build_loop_body_preserves_duration_and_joins_with_context():
    n = 10_000
    audio = np.stack([np.arange(n), np.arange(n)], axis=1).astype(np.int16)
    start, end, cf = 1000, 5000, 200
    body, used = build_loop_body(audio, start, end, cf)
    assert used == cf
    assert len(body) == end - start
    assert np.array_equal(body[cf:], audio[start + cf:end])
    assert body[0, 0] == audio[end, 0]
    assert body[cf - 1, 0] == audio[start + cf - 1, 0]
    assert int(body[0, 0]) - int(body[-1, 0]) == 1


def test_build_loop_body_clamps_crossfade_and_handles_zero():
    audio = make_signal(0.1)
    body, used = build_loop_body(audio, 0, 300, 1000)
    assert used == 100 and len(body) == 300
    body0, used0 = build_loop_body(audio, 0, 300, 0)
    assert used0 == 0 and np.array_equal(body0, audio[:300])
    with pytest.raises(ValueError):
        build_loop_body(audio, 10, 10, 5)


def _fmt_chunk(channels=2, rate=44100, bits=16, extensible=False):
    block_align = channels * bits // 8
    payload = struct.pack("<HHIIHH", 0xFFFE if extensible else 1, channels, rate,
                          rate * block_align, block_align, bits)
    if extensible:
        payload += struct.pack("<HHI", 22, bits, 3) + struct.pack("<H", 1) + b"\x00" * 14
    return b"fmt " + struct.pack("<I", len(payload)) + payload


def _chunk(cid, payload):
    data = cid + struct.pack("<I", len(payload)) + payload
    return data + (b"\x00" if len(payload) & 1 else b"")


def _write_riff(path, chunks, riff_size=None):
    body = b"".join(chunks)
    size = len(body) + 4 if riff_size is None else riff_size
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", size) + b"WAVE" + body)


def test_parse_wav_handles_list_chunk_and_padding(tmp_path):
    frames = make_signal(0.01)
    pcm = frames.tobytes()
    list_payload = b"INFOISFT" + struct.pack("<I", 5) + b"Lavf\x00"
    path = tmp_path / "list.wav"
    _write_riff(path, [_fmt_chunk(), _chunk(b"LIST", list_payload), _chunk(b"data", pcm)])
    info = parse_wav_data_chunk(path)
    assert info["frames"] == len(frames) and info["data_offset"] != 44
    mm = np.memmap(path, dtype=np.int16, mode="r", offset=info["data_offset"],
                   shape=(info["frames"], 2))
    assert np.array_equal(mm, frames)
    del mm


def test_parse_wav_clamps_unknown_size_and_reads_extensible(tmp_path):
    frames = make_signal(0.01)
    pcm = frames.tobytes()
    path = tmp_path / "stream.wav"
    _write_riff(path, [_fmt_chunk(extensible=True),
                       b"data" + struct.pack("<I", 0xFFFFFFFF) + pcm], riff_size=0xFFFFFFFF)
    info = parse_wav_data_chunk(path)
    assert info["format_tag"] == 1 and info["frames"] == len(frames)


def test_parse_wav_rejects_non_riff(tmp_path):
    path = tmp_path / "junk.wav"
    path.write_bytes(b"not a wav file at all, definitely not")
    with pytest.raises(ValueError):
        parse_wav_data_chunk(path)


def test_sweep_removes_stale_files_but_keeps_own(tmp_path):
    d = tmp_path / "LoopStation"
    d.mkdir()
    stale = d / f"{TEMP_FILE_PREFIX}1234_abc.wav"
    own = d / f"{TEMP_FILE_PREFIX}{os.getpid()}_own.wav"
    foreign = d / "unrelated.txt"
    for p in (stale, own, foreign):
        p.write_bytes(b"x")
    assert sweep_stale_temp_files(str(d), keep_pid=os.getpid()) == 1
    assert not stale.exists() and own.exists() and foreign.exists()


@pytest.mark.parametrize("factor", [1.0, 1.25, 0.7, 0.3, 3.0, 4.5])
def test_atempo_chain_factors_in_range_and_multiply(factor):
    chain = atempo_chain(factor)
    assert all(0.5 <= f <= 2.0 for f in chain)
    assert np.prod(chain) == pytest.approx(factor) if chain else factor == 1.0


def test_tempo_pitch_filter_strings():
    assert build_tempo_pitch_filter(1.0, 0.0) is None
    assert build_tempo_pitch_filter(1.25, 0.0) == "atempo=1.250000"
    f = build_tempo_pitch_filter(1.0, 12.0)
    assert f.startswith(f"asetrate={2 * SR},aresample={SR},atempo=0.5")


# ============================================================ mixer core ==

def test_render_plays_source_exactly_and_tracks_position():
    src = ramp(SR)
    core = make_core({"mix": src})
    core.play(0.0)
    out = run(core, 12345)
    assert out.shape == (12345, 2)
    assert np.array_equal(out, f32(src[:12345]))
    assert core.get_position() == pytest.approx(12345 / SR, abs=1e-12)


def test_stems_mute_solo_gain():
    mix = ramp(4000)
    click = np.full((4000, 2), 1000, dtype=np.int16)
    core = make_core({"mix": mix, "click": click})
    core.play(0.0)
    assert np.allclose(run(core, 500), f32(mix[:500]) + f32(click[:500]), atol=1e-6)

    core.set_stem_gain("mix", -20 * np.log10(2))
    assert np.allclose(run(core, 500), 0.5 * f32(mix[500:1000]) + f32(click[500:1000]), atol=1e-5)
    core.set_stem_gain("mix", 0.0)

    core.set_stem_mute("click", True)
    assert np.array_equal(run(core, 500), f32(mix[1000:1500]))
    core.set_stem_mute("click", False)

    core.set_stem_solo("click", True)
    assert np.array_equal(run(core, 500), f32(click[1500:2000]))
    stems = {s.name: s for s in core.get_stems()}
    assert stems["click"].solo and not stems["mix"].solo


def test_unequal_stems_rejected_by_core():
    with pytest.raises(ValueError):
        make_core({"mix": ramp(100), "click": ramp(90)})


def test_routing_modes():
    mix = ramp(1000)
    click = np.stack([np.full(1000, 2000), np.full(1000, 4000)], axis=1).astype(np.int16)
    core = make_core({"mix": mix, "click": click})

    core.set_routing("click_right")
    core.play(0.0)
    out = run(core, 1000)
    assert np.allclose(out[:, 0], f32(mix).mean(axis=1), atol=1e-6)
    assert np.allclose(out[:, 1], f32(click).mean(axis=1), atol=1e-6)

    core.set_routing("multichannel")
    assert core.output_channels == 4
    core.play(0.0)
    out = run(core, 1000)
    assert out.shape == (1000, 4)
    assert np.array_equal(out[:, 0:2], f32(mix)) and np.array_equal(out[:, 2:4], f32(click))

    core.set_channel_map(mix=(0, 1), click=(2,))
    assert core.get_routing() == {"mode": "multichannel", "output_channels": 3,
                                  "channel_map": {"mix": (0, 1), "click": (2,)}}
    core.play(0.0)
    out = run(core, 1000)
    assert np.allclose(out[:, 2], f32(click).mean(axis=1), atol=1e-6)

    with pytest.raises(ValueError):
        core.set_routing("quad")


def test_skip_region_jumps_at_exact_sample():
    src = ramp(SR)
    core = make_core({"mix": src})
    core.set_skip_regions([(0.2, 0.6)])     # frames 8820 -> 26460
    core.play(0.1)                          # frame 4410
    out = run(core, 8000)
    assert np.array_equal(out[:4410], f32(src[4410:8820]))
    assert np.array_equal(out[4410:], f32(src[26460:26460 + 3590]))
    evs = drain(core)
    assert evs == [("skip_executed", {"from_pos": pytest.approx(0.2), "to_pos": pytest.approx(0.6)})]


def test_armed_loop_enters_at_loop_out_and_counts_cycles(loop_core):
    core, src = loop_core
    core.arm_loop(0.5, 1.0, crossfade_ms=20)
    assert core.is_loop_ready()
    body, _ = build_loop_body(src, IN_F, OUT_F, CF)
    core.play(0.9)
    out = run(core, TO_WRAP + 3 * BODY + 10)

    assert np.array_equal(out[:TO_WRAP], f32(src[START_F:OUT_F]))     # song untouched through seam holds
    assert np.array_equal(out[TO_WRAP:TO_WRAP + 2000], f32(body[:2000]))
    evs = drain(core)
    assert names(evs) == ["loop_entered", "loop_cycle", "loop_cycle", "loop_cycle", "loop_cycle"]
    assert [e[1]["cycle"] for e in evs[1:]] == [1, 2, 3, 4]
    assert core.mode == "loop" and core.get_loop_cycle_count() == 4
    # 10 frames into cycle 4's body
    assert core.get_loop_cycle_position() == pytest.approx(10 / SR)
    assert core.get_position() == pytest.approx((IN_F + 10) / SR)


def test_loop_count_two_exits_after_two_cycles_seamlessly(loop_core):
    core, src = loop_core
    core.arm_loop(0.5, 1.0, crossfade_ms=20, count=2)
    core.play(0.9)
    exit_k = TO_WRAP + 2 * BODY
    out = run(core, exit_k + 3000)
    evs = drain(core)
    assert names(evs) == ["loop_entered", "loop_cycle", "loop_cycle", "loop_exited"]
    assert evs[-1][1] == {"position": pytest.approx(EF / SR), "reason": "count"}
    # Around the exit the output is the untouched song: no seam at all
    assert np.array_equal(out[exit_k - 1000:exit_k + 3000], f32(src[EF - 1000:EF + 3000]))
    assert core.mode == "transport" and core.is_playing
    # Not re-entered on the way out
    run(core, 5000)
    assert drain(core) == []


def test_cut_exit_continues_seamlessly_from_loop_out(loop_core):
    core, src = loop_core
    core.arm_loop(0.5, 1.0, crossfade_ms=20)
    core.play(0.9)
    run(core, TO_WRAP + 5000)
    drain(core)
    core.queue_loop_exit("cut")
    out = run(core, (BODY - 5000) + 2000)
    k = BODY - 5000
    assert np.array_equal(out[k - 500:k + 2000], f32(src[EF - 500:EF + 2000]))
    evs = drain(core)
    assert evs == [("loop_exited", {"position": pytest.approx(EF / SR), "reason": "cut"})]
    assert core.get_position() == pytest.approx((EF + 2000) / SR)


def test_cancel_cut_exit_keeps_looping(loop_core):
    core, _ = loop_core
    core.arm_loop(0.5, 1.0)
    core.play(0.9)
    run(core, TO_WRAP + 100)
    core.queue_loop_exit("cut")
    core.cancel_loop_exit()
    run(core, BODY)
    assert core.mode == "loop" and "loop_exited" not in names(drain(core))


def test_fade_exit_reaches_silence_and_stops(loop_core):
    core, src = loop_core
    ref = make_core({"mix": src})
    for c in (core, ref):
        c.arm_loop(0.5, 1.0)
        c.play(0.9)
        run(c, TO_WRAP + 1000)
    drain(core)
    fade = 4410
    core.queue_loop_exit("fade", fade_ms=100)
    assert core.is_fading_out()
    out = run(core, fade + 3000)
    expected = run(ref, fade + 3000)
    env = (1.0 - np.arange(1, fade + 1, dtype=np.float32) / fade)[:, None]
    assert np.allclose(out[:fade], expected[:fade] * env, atol=1e-6)
    assert not out[fade:].any()
    assert not core.is_playing and core.mode == "transport" and not core.is_fading_out()
    assert drain(core) == [("loop_exited", {"position": pytest.approx(1.0), "reason": "fade"})]


def test_hold_stops_at_exact_sample_and_release_resumes():
    src = ramp(SR)
    core = make_core({"mix": src})
    core.hold_at(0.5)                       # frame 22050
    core.play(0.4)                          # frame 17640
    out = run(core, 8000)
    assert np.array_equal(out[:4410], f32(src[17640:22050]))
    assert not out[4410:].any()
    assert core.is_held and core.is_playing
    assert core.get_position() == pytest.approx(0.5, abs=1e-12)
    assert drain(core) == [("hold_reached", {"position": pytest.approx(0.5)})]

    core.release_hold()
    out = run(core, 1000)
    assert np.array_equal(out, f32(src[22050:23050]))
    assert not core.is_held
    assert drain(core) == [("hold_released", {"position": pytest.approx(0.5)})]


def test_pause_stop_and_song_end():
    src = ramp(3000)
    core = make_core({"mix": src})
    core.play(0.0)
    run(core, 1000)
    core.pause()
    assert not run(core, 500).any()
    assert core.get_position() == pytest.approx(1000 / SR)
    core.unpause()
    out = run(core, 2500)
    assert np.array_equal(out[:2000], f32(src[1000:3000]))
    assert not out[2000:].any()
    assert not core.is_playing
    assert names(drain(core)) == ["song_ended"]
    core.stop()
    assert core.get_position() == 0.0


def test_manual_entry_fade_in_and_seek_exits_loop(loop_core):
    core, _ = loop_core
    core.set_loop_points(0.5, 1.0)
    core.play(0.1)
    assert core.enter_loop(fade_in_ms=10)
    out = run(core, 441)
    level = np.abs(out[:, 0])
    assert level[:20].max() < level[-50:].max()       # ramping up
    core.seek(0.2)
    assert core.mode == "transport"
    evs = drain(core)
    assert names(evs) == ["loop_entered", "loop_exited"] and evs[1][1]["reason"] == "seek"


def test_declick_smooths_seek_discontinuity():
    src = make_signal(2.0)

    def max_step(declick_ms):
        core = make_core({"mix": src}, declick_ms=declick_ms)
        core.play(0.0)
        a = run(core, 1000)
        core.seek(1.0013)
        b = run(core, 2000)
        return np.abs(np.diff(np.concatenate([a, b]), axis=0)).max()

    natural = 2 * np.pi * 440 / SR * 12000 / 32768   # max slope of the sine
    assert max_step(0) > 3 * natural  # the raw jump really is a click
    assert max_step(10) < 1.1 * natural


def test_tempo_swap_keeps_source_position_and_rebuilds_loop(loop_core):
    core, src = loop_core
    core.set_loop_points(0.5, 1.0)
    core.play(0.25)
    run(core, 1000)
    pos = core.get_position()
    fast = np.ascontiguousarray(src[::2])         # stand-in for a 2x render
    core.swap_buffers({"mix": fast}, 2.0, 0.0)
    core.apply_pending_swap()
    assert core.n_active_frames == len(fast) and core.tempo_ratio == 2.0
    assert core.get_position() == pytest.approx(pos, abs=2 / SR)
    run(core, 1000)                               # 1000 active frames = 2000 source frames
    assert core.get_position() == pytest.approx(pos + 2000 / SR, abs=2 / SR)
    assert core.get_loop_points() == (0.5, 1.0, 20)
    # Crossfade never changes the source musical period, even at 2x.
    assert core.loop_duration == pytest.approx((OUT_F - IN_F) / SR, abs=4 / SR)


# ================================================================== engine ==

@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    root = tmp_path / "systmp"
    root.mkdir()
    monkeypatch.setattr(audio_engine.tempfile, "gettempdir", lambda: str(root))
    return root


@pytest.fixture
def song_wav(tmp_path):
    data = make_signal(2.0)
    return write_wav(tmp_path / "song.wav", data), data


def _engine(ffmpeg, streams, realtime=False):
    return AudioEngine(ffmpeg_path=ffmpeg, stream_factory=fake_stream_factory(realtime, streams))


@pytest.fixture
def make_engine(temp_root):
    made = []

    def factory(ffmpeg=NO_FFMPEG, realtime=False):
        streams = []
        eng = _engine(ffmpeg, streams, realtime)
        made.append(eng)
        return eng, streams
    yield factory
    for eng in made:
        eng.close()


class Recorder:
    def __init__(self):
        self.events = []
        self.threads = set()

    def __call__(self, name, payload):
        self.threads.add(threading.get_ident())
        self.events.append((name, payload))

    def names(self):
        return [e[0] for e in self.events]


def test_engine_init_sweeps_previous_run(temp_root):
    d = temp_root / "LoopStation"
    d.mkdir()
    leftover = d / f"{TEMP_FILE_PREFIX}42_leftover.wav"
    leftover.write_bytes(b"x")
    eng = _engine(NO_FFMPEG, [])
    try:
        assert eng.temp_dir == str(d) and not leftover.exists()
    finally:
        eng.close()


def test_load_native_wav_without_ffmpeg_and_position(make_engine, song_wav):
    path, data = song_wav
    eng, streams = make_engine()
    length, ratio = eng.load_file(path)
    assert (length, ratio) == (len(data) / SR, 1.0)
    assert np.array_equal(eng.get_raw_audio_data(), data)
    eng.play_transport(0.0)
    out = streams[-1].pump(12345)
    assert eng.get_position() == pytest.approx(12345 / SR, abs=1e-12)
    # 10 ms declick on start is the only difference from the source
    assert np.allclose(out[441:], f32(data[441:12345]), atol=1e-6)
    assert eng.is_transport_active()
    eng.cleanup()
    assert os.path.exists(path) and eng.get_raw_audio_data() is None


@needs_ffmpeg
def test_ffmpeg_decode_memmap_matches_wav(make_engine, song_wav):
    path, data = song_wav
    eng, _ = make_engine(FFMPEG)
    length, _ = eng.load_file(path)
    raw = eng.get_raw_audio_data()
    assert isinstance(raw, np.memmap) and raw.shape == (len(data), CHANNELS)
    assert np.array_equal(raw, data)
    tmp = eng._stem_sources["mix"]["path"]
    assert os.path.dirname(tmp) == eng.temp_dir
    assert os.path.basename(tmp).startswith(TEMP_FILE_PREFIX)
    assert np.array_equal(read_wav(tmp), data)


@needs_ffmpeg
def test_ffmpeg_decodes_flac_and_pads_short_stems(make_engine, song_wav, tmp_path):
    path, data = song_wav
    flac = tmp_path / "song.flac"
    subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-y", "-i", path, str(flac)], check=True)
    click = make_signal(1.0, freq=1000.0)
    click_path = write_wav(tmp_path / "click.wav", click)
    eng, _ = make_engine(FFMPEG)
    length, _ = eng.load_file(str(flac), {"click": click_path})
    assert length == len(data) / SR
    assert [s.name for s in eng.get_stems()] == ["mix", "click"]
    padded = eng._stem_sources["click"]["data"]
    assert len(padded) == len(data)
    assert np.array_equal(padded[:len(click)], click) and not padded[len(click):].any()


@needs_ffmpeg
def test_ffmpeg_failure_raises_and_leaves_nothing(make_engine, tmp_path):
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"\x00" * 2048)
    eng, _ = make_engine(FFMPEG)
    with pytest.raises(RuntimeError):
        eng.load_file(str(bad))
    assert eng.get_raw_audio_data() is None
    assert [n for n in os.listdir(eng.temp_dir) if n.startswith(TEMP_FILE_PREFIX)] == []


def test_routing_restarts_stream_with_channel_count(make_engine, song_wav):
    eng, streams = make_engine()
    eng.load_file(song_wav[0])
    assert streams[-1].channels == 2
    eng.set_routing("multichannel")
    assert streams[-1].channels == 4 and streams[-2].closed
    assert eng.get_routing()["output_channels"] == 4
    eng.play_transport(0.0)
    assert streams[-1].pump(512).shape == (512, 4)


def test_device_error_event_and_consistent_state(temp_root, song_wav):
    def broken(*a, **k):
        raise RuntimeError("no such device")
    eng = AudioEngine(ffmpeg_path=NO_FFMPEG, stream_factory=broken)
    rec = Recorder()
    eng.on_event(rec)
    try:
        assert wait_until(lambda: "device_error" in rec.names())
        eng.load_file(song_wav[0])
        eng.play_transport(0.5)
        assert eng.is_playing and eng.get_position() == pytest.approx(0.5)
        assert eng.get_output_device() is None
    finally:
        eng.close()


def test_events_are_delivered_off_the_render_thread(make_engine, song_wav):
    eng, streams = make_engine(realtime=True)
    rec = Recorder()
    eng.on_event(rec)
    eng.load_file(song_wav[0])
    eng.arm_loop(0.5, 0.7, count=1)
    eng.play_transport(0.6)
    assert wait_until(lambda: "loop_exited" in rec.names(), timeout=3.0)
    assert rec.names()[:3] == ["loop_entered", "loop_cycle", "loop_exited"]
    stream = streams[-1]
    assert stream.thread_ident is not None
    assert rec.threads == {eng._event_thread.ident}
    assert stream.thread_ident not in rec.threads
    # pump() renders on this thread: events still arrive on the worker
    eng.seek_transport(0.0)
    eng.set_skip_regions([(0.1, 0.2)])
    eng.play_transport(0.09)
    assert wait_until(lambda: "skip_executed" in rec.names())
    assert threading.get_ident() not in rec.threads


def test_compat_surface(make_engine, song_wav):
    eng, streams = make_engine()
    eng.load_file(song_wav[0])
    eng.set_loop_points(0.5, 1.0, crossfade_ms=20)
    assert eng.is_loop_ready() and eng.get_loop_points() == (0.5, 1.0, 20)
    assert (eng.loop_in, eng.loop_out) == (0.5, 1.0)
    assert eng.loop_duration == pytest.approx(BODY / SR)
    eng.play_transport(0.6)
    streams[-1].pump(1024)
    assert eng.start_loop_mode() and eng.mode == "loop" and eng.is_loop_active()
    streams[-1].pump(1024)
    assert 0 < eng.get_loop_cycle_position() < eng.loop_duration
    eng.execute_loop_exit()
    assert eng.mode == "transport" and eng.get_position() == pytest.approx(1.0)
    eng.perform_skip(1.5)
    assert eng.transport_offset == pytest.approx(1.5)
    eng.execute_fade_exit(50)
    assert eng.is_fading_out()
    streams[-1].pump(SR // 10)
    assert not eng.is_playing and eng.finish_fade_exit() is None
    with eng.lock:
        pass


@needs_ffmpeg
def test_tempo_render_hot_swaps_and_keeps_source_seconds(make_engine, song_wav, tmp_path):
    path, data = song_wav
    click_path = write_wav(tmp_path / "click.wav", make_signal(2.0, freq=1000.0))
    eng, streams = make_engine(FFMPEG)
    rec = Recorder()
    eng.on_event(rec)
    eng.load_file(path, {"click": click_path})
    eng.set_loop_points(0.5, 1.0)
    eng.play_transport(1.0)
    stream = streams[-1]
    stream.pump(4410)

    eng.set_tempo_pitch(0.8, 2.0)
    assert wait_until(lambda: eng.core._pending_swap is not None or "render_failed" in rec.names(),
                      timeout=20.0)
    assert "render_failed" not in rec.names(), rec.events
    assert rec.names()[0] == "render_started"
    assert eng.core.n_active_frames == len(data)      # old buffers until the next callback
    assert eng.get_tempo_pitch() == (1.0, 0.0)
    pos_before = eng.get_position()
    stream.pump(512)                                  # swap happens inside render
    assert wait_until(lambda: "render_done" in rec.names())
    assert eng.core.n_active_frames == pytest.approx(len(data) / 0.8, rel=0.01)
    assert eng.get_tempo_pitch() == (0.8, 2.0)
    # 512 frames of 0.8x playback = 0.8 * 512 source frames
    assert eng.get_position() == pytest.approx(pos_before + 0.8 * 512 / SR, abs=0.005)
    assert eng.get_loop_points() == (0.5, 1.0, 20) and eng.is_loop_ready()
    assert len(eng.get_raw_audio_data()) == len(data)  # source untouched
    rendered = list(eng._render_paths)
    assert len(rendered) == 2 and all(os.path.exists(p) for p in rendered)

    eng.set_tempo_pitch(1.0, 0.0)
    stream.pump(512)
    assert eng.core.n_active_frames == len(data)
    assert eng.get_tempo_pitch() == (1.0, 0.0)

    eng.cleanup()
    gc.collect()
    assert [n for n in os.listdir(eng.temp_dir) if n.startswith(TEMP_FILE_PREFIX)] == []


@needs_ffmpeg
def test_cleanup_removes_temp_files(make_engine, song_wav):
    eng, _ = make_engine(FFMPEG)
    eng.load_file(song_wav[0])
    tmp = eng._stem_sources["mix"]["path"]
    assert os.path.exists(tmp)
    eng.cleanup()
    assert not os.path.exists(tmp)
    assert eng.get_stems() == [] and eng.song_length == 0.0


def test_list_output_devices_shape(make_engine):
    eng, _ = make_engine()
    for d in eng.list_output_devices():
        assert set(d) == {"index", "name", "channels", "default", "hostapi"}
        assert d["channels"] > 0


# ===================================================== review regressions ==

def _dc(n, value=16000):
    return np.full((n, 2), value, dtype=np.int16)


def _slow(buffers, factor=1.25):
    """Stand-in for a 0.8x tempo render: each frame doubled, trimmed to n*factor."""
    return {k: np.ascontiguousarray(np.repeat(v, 2, axis=0)[:int(len(v) * factor)])
            for k, v in buffers.items()}


def _assert_bodies_match_active(core):
    """Installed loop frames/bodies are the ones built from the ACTIVE buffers."""
    in_f, out_f, cf_req = core._loop_frames_for(core.n_active_frames, core.loop_in,
                                                core.loop_out, core.crossfade_ms)
    assert (core._loop_in_f, core._loop_out_f) == (in_f, out_f)
    for s in core._stems.values():
        body, cf = build_loop_body(s.active, in_f, out_f, cf_req)
        assert core._cf == cf and np.array_equal(s.body, body)


class _RenderGuard:
    """Wraps core.render/_build_bodies: counts bodies built inside render and
    records the wall time of every render call."""

    def __init__(self, core, monkeypatch):
        self.in_render = False
        self.built_in_render = 0
        self.times = []
        orig_render, orig_build = core.render, core._build_bodies

        def render(frames):
            self.in_render = True
            t = time.perf_counter()
            try:
                return orig_render(frames)
            finally:
                self.times.append(time.perf_counter() - t)
                self.in_render = False

        def build(*a, **k):
            if self.in_render:
                self.built_in_render += 1
            return orig_build(*a, **k)

        monkeypatch.setattr(core, "render", render)
        monkeypatch.setattr(core, "_build_bodies", build)


@pytest.fixture(scope="module")
def three_stems():
    return {"mix": make_signal(40.0), "click": make_signal(40.0, freq=1000.0),
            "bass": make_signal(40.0, freq=110.0)}, SR * 40


def test_swap_with_loop_set_while_pending_never_builds_in_render(three_stems, monkeypatch):
    """Finding 2: arm a 30 s vamp while a tempo swap is pending (3 stems)."""
    stems, n = three_stems
    core = make_core(stems, declick_ms=10)
    guard = _RenderGuard(core, monkeypatch)
    core.play(0.0)
    run(core, 2048)
    core.swap_buffers(_slow(stems), 0.8, 0.0)
    core.set_loop_points(5.0, 35.0, 20)           # 30 s vamp while the swap is pending
    guard.times.clear()
    run(core, 512 * 8)                            # the swap is applied in here
    assert core.tempo_ratio == 0.8 and core.n_active_frames == int(n * 1.25)
    assert guard.built_in_render == 0
    assert max(guard.times) < 0.005, f"render took {max(guard.times) * 1000:.1f} ms"
    _assert_bodies_match_active(core)


def test_swap_scheduled_after_loop_installs_prebuilt_bodies(three_stems, monkeypatch):
    stems, n = three_stems
    core = make_core(stems, declick_ms=10)
    core.arm_loop(5.0, 35.0, 20)
    core.play(0.0)
    run(core, 2048)
    guard = _RenderGuard(core, monkeypatch)
    core.swap_buffers(_slow(stems), 0.8, 0.0)
    run(core, 512 * 8)
    assert core.n_active_frames == int(n * 1.25)
    assert guard.built_in_render == 0 and max(guard.times) < 0.005
    _assert_bodies_match_active(core)


def test_stale_pending_bodies_defer_swap_and_rebuild_off_thread(three_stems, monkeypatch):
    """If the pending bodies no longer match the loop, render defers the swap
    (never building) and a worker rebuilds them."""
    stems, n = three_stems
    core = make_core(stems, declick_ms=10)
    core.set_loop_points(5.0, 35.0, 20)
    core.play(0.0)
    run(core, 1024)
    core.swap_buffers(_slow(stems), 0.8, 0.0)
    pending = core._pending_swap
    assert pending.bodies is not None
    guard = _RenderGuard(core, monkeypatch)
    with core.lock:      # as if set_loop_points' phase 1 just stored new points
        core._crossfade_ms = 10
    run(core, 512)
    assert core.n_active_frames == n              # deferred: old buffers still playing
    assert guard.built_in_render == 0 and max(guard.times) < 0.005
    assert wait_until(lambda: pending.loop_key == (5.0, 35.0, 10), timeout=10.0)
    run(core, 512)
    assert core.n_active_frames == int(n * 1.25) and core._pending_swap is None
    assert guard.built_in_render == 0
    _assert_bodies_match_active(core)


def test_render_during_set_loop_points_build_defers_and_stays_fast(three_stems, monkeypatch):
    """The callback runs while set_loop_points is building (swap pending): the
    swap is neither applied with stale bodies nor builds them inline."""
    stems, n = three_stems
    core = make_core(stems, declick_ms=10)
    core.set_loop_points(5.0, 35.0, 20)
    core.play(0.0)
    run(core, 1024)
    core.swap_buffers(_slow(stems), 0.8, 0.0)
    guard = _RenderGuard(core, monkeypatch)
    inner = core._build_bodies
    calls = []

    def build(*a, **k):
        r = inner(*a, **k)
        if not calls:
            calls.append(1)
            core.render(512)                      # audio callback mid-build
        return r
    monkeypatch.setattr(core, "_build_bodies", build)
    core.set_loop_points(6.0, 36.0, 20)
    monkeypatch.setattr(core, "_build_bodies", inner)
    run(core, 1024)
    assert core.n_active_frames == int(n * 1.25)
    assert guard.built_in_render == 0 and max(guard.times) < 0.005
    _assert_bodies_match_active(core)


def test_set_loop_points_rebuilds_when_swap_applied_during_build(monkeypatch):
    """Finding 3: a swap applied by render between the two phases of
    set_loop_points must not leave frames built for the old buffers."""
    src = make_signal(30.0)
    core = make_core({"mix": src})
    core.play(0.0)
    run(core, 512)
    core.swap_buffers(_slow({"mix": src}), 0.8, 0.0)
    gen = core._buf_gen
    orig = core._build_bodies
    calls = []

    def build(*a, **k):
        r = orig(*a, **k)
        calls.append(1)
        core.render(512)                          # callback applies the swap now
        return r
    monkeypatch.setattr(core, "_build_bodies", build)
    core.set_loop_points(10.0, 12.0, 20)
    monkeypatch.setattr(core, "_build_bodies", orig)
    assert core._buf_gen == gen + 1 and core.n_active_frames == int(len(src) * 1.25)
    assert (core._loop_in_f, core._loop_out_f) == (551250, 661500)
    _assert_bodies_match_active(core)
    assert len(calls) >= 2                        # it retried after the swap


def test_swap_buffers_rebuilds_when_loop_changes_during_build(monkeypatch):
    src = make_signal(10.0)
    core = make_core({"mix": src})
    core.set_loop_points(1.0, 2.0, 20)
    orig = core._build_bodies
    state = {"done": False}

    def build(*a, **k):
        r = orig(*a, **k)
        if not state["done"]:
            state["done"] = True
            core.set_loop_points(3.0, 4.0, 20)    # UI moves the loop meanwhile
        return r
    monkeypatch.setattr(core, "_build_bodies", build)
    core.swap_buffers(_slow({"mix": src}), 0.8, 0.0)
    monkeypatch.setattr(core, "_build_bodies", orig)
    assert core._pending_swap.loop_key == (3.0, 4.0, 20)
    core.render(512)
    assert core._pending_swap is None
    _assert_bodies_match_active(core)


def test_frames_rendered_is_monotonic_across_seeks_and_swaps():
    src = make_signal(5.0)
    core = make_core({"mix": src})
    assert core.frames_rendered == 0
    run(core, 1000)                               # silent while stopped: still counted
    core.play(1.0)
    run(core, 1000)
    core.seek(0.0)
    core.stop()
    run(core, 48)
    core.swap_buffers(_slow({"mix": src}), 0.8, 0.0)
    run(core, 952)
    assert core.frames_rendered == 3000


def test_transport_fade_emits_stopped():
    """Finding 10."""
    core = make_core({"mix": make_signal(10.0)})
    core.play(0.0)
    run(core, 512)
    drain(core)
    core.queue_loop_exit("fade", 100)
    assert core.is_fading_out()
    out = run(core, SR // 5)
    evs = drain(core)
    assert names(evs) == ["stopped"] and evs[0][1]["reason"] == "fade"
    assert evs[0][1]["position"] == pytest.approx((512 + 4410) / SR, abs=2 / SR)
    assert not core.is_playing and not core.is_fading_out()
    assert not out[4410:].any()
    # paused transport: stops at once; stopped engine: ignored
    core.play(1.0)
    run(core, 512)
    core.pause()
    drain(core)
    core.queue_loop_exit("fade", 100)
    assert names(drain(core)) == ["stopped"] and not core.is_playing
    core.queue_loop_exit("fade", 100)
    assert drain(core) == [] and not core.is_fading_out()


def test_declick_tail_fades_at_song_end_instead_of_truncating():
    """Finding 11: seek 100 frames before the end of a DC song."""
    core = make_core({"mix": _dc(SR)}, declick_ms=10)
    core.play(0.0)
    a = run(core, SR - 100)
    core.seek(0.5)
    b = run(core, 600)
    out = np.concatenate([a, b])
    assert np.abs(np.diff(out[:, 0])).max() < 0.05


def test_declick_tail_wraps_into_loop_body_on_pause_near_wrap():
    """Finding 11: pause 200 frames (< 10 ms) before a wrap."""
    core = make_core({"mix": _dc(SR * 2)}, declick_ms=10)
    core.set_loop_points(0.5, 1.0, 20)
    core.play(0.6)
    run(core, 1000)
    assert core.enter_loop()
    body = core._body_len
    a = run(core, body - 200 - core._loop_pos)
    assert core._loop_pos == body - 200
    core.pause()
    b = run(core, 1500)
    out = np.concatenate([a, b])
    assert np.abs(np.diff(out[:, 0])).max() < 0.05
    assert not b[-500:].any()                     # silent once the tail has faded


# ---------------------------------------------------------- device loss --

def test_lost_stream_reports_device_error_and_restart_resumes_same_sample(make_engine, song_wav):
    """Finding 1."""
    path, data = song_wav
    eng, streams = make_engine()
    rec = Recorder()
    eng.on_event(rec)
    eng.load_file(path)
    # our own stream restarts never count as a lost device
    eng.set_routing("multichannel")
    eng.set_routing("stereo")
    eng.play_transport(0.0)
    s1 = streams[-1]
    s1.pump(10000)
    assert eng.is_stream_active() and eng.frames_rendered >= 10000
    s1.fail()                                     # device unplugged
    assert wait_until(lambda: "device_error" in rec.names())
    assert rec.names().count("device_error") == 1
    assert not eng.is_stream_active()
    before = eng.frames_rendered
    s1.pump(4096)                                 # dead stream: nothing renders
    assert eng.frames_rendered == before          # what a watchdog looks at
    assert eng.core.get_position() == pytest.approx(10000 / SR)

    assert eng.restart_stream() is True
    assert wait_until(lambda: "device_restored" in rec.names())
    assert isinstance(rec.events[-1][1]["name"], str)
    assert eng.is_stream_active() and streams[-1] is not s1
    out = streams[-1].pump(2000)
    assert np.allclose(out, f32(data[10000:12000]), atol=1e-6)   # same sample onward
    assert rec.names().count("device_error") == 1


def test_underflow_flags_are_counted(make_engine, song_wav):
    eng, streams = make_engine()
    eng.load_file(song_wav[0])
    eng.play_transport(0.0)
    streams[-1].pump(1024)
    assert eng.underflow_count == 0
    streams[-1].pump(1024, status=types.SimpleNamespace(output_underflow=True))
    streams[-1].pump(512, status=types.SimpleNamespace(output_underflow=False))
    assert eng.underflow_count == 2               # two 512-frame blocks flagged


def _device_factory(holder, eng_ref, rejects):
    """Fake factory that consults the engine's selected device / channel count."""
    inner = fake_stream_factory(False, holder)

    def factory(callback, channels, sample_rate, block_size, finished_callback=None):
        device = eng_ref[0]._device if eng_ref else None
        if rejects(device, channels):
            raise RuntimeError(f"cannot open device {device} with {channels} ch")
        return inner(callback, channels, sample_rate, block_size,
                     finished_callback=finished_callback)
    return factory


def test_restart_stream_falls_back_to_default_device(temp_root, song_wav):
    streams, ref, dead = [], [], set()
    eng = AudioEngine(ffmpeg_path=NO_FFMPEG,
                      stream_factory=_device_factory(streams, ref, lambda d, ch: d in dead))
    ref.append(eng)
    rec = Recorder()
    eng.on_event(rec)
    try:
        eng.load_file(song_wav[0])
        eng.set_output_device(5)
        assert eng._device == 5
        eng.play_transport(0.0)
        streams[-1].pump(512)
        dead.add(5)
        streams[-1].fail()
        assert eng.restart_stream() is True       # device 5 gone: system default
        assert eng._device is None and eng.is_stream_active()
        assert wait_until(lambda: "device_restored" in rec.names())
        dead.add(None)                            # nothing works at all
        streams[-1].fail()
        assert wait_until(lambda: rec.names().count("device_error") == 2)
        assert eng.restart_stream() is False and not eng.is_stream_active()
        assert wait_until(lambda: rec.names().count("device_error") == 3)
    finally:
        eng.close()


def test_set_routing_unsupported_channels_reverts_and_raises(temp_root, song_wav):
    """Finding 7: a device with only 2 output channels."""
    streams, ref = [], []
    eng = AudioEngine(ffmpeg_path=NO_FFMPEG,
                      stream_factory=_device_factory(streams, ref, lambda d, ch: ch > 2))
    ref.append(eng)
    try:
        eng.load_file(song_wav[0])
        with pytest.raises(RuntimeError, match="multichannel"):
            eng.set_routing("multichannel")
        assert eng.get_routing()["mode"] == "stereo"
        assert eng.is_stream_active() and streams[-1].channels == 2
        eng.play_transport(0.0)
        assert streams[-1].pump(512).shape == (512, 2)
        eng.set_routing("click_right")            # 2 channels: fine
        assert eng.get_routing()["mode"] == "click_right"
        assert eng.is_stream_active()
    finally:
        eng.close()


def test_set_output_device_failure_reverts_then_raises(temp_root, song_wav):
    streams, ref = [], []
    eng = AudioEngine(ffmpeg_path=NO_FFMPEG,
                      stream_factory=_device_factory(streams, ref, lambda d, ch: d == 7))
    ref.append(eng)
    try:
        eng.load_file(song_wav[0])
        eng.set_output_device(3)
        with pytest.raises(RuntimeError):
            eng.set_output_device(7)
        assert eng._device == 3 and eng.is_stream_active()
    finally:
        eng.close()


def test_manual_loop_entry_does_not_inherit_armed_count():
    """t1(e): enter_loop after a count=1 arm loops until exit; count= is honoured."""
    core = make_core({"mix": make_signal(30.0)})
    core.arm_loop(1.0, 2.0, 20, count=1)
    core.disarm_loop()
    core.set_loop_points(5.0, 6.0, 20)
    core.play(5.2)
    run(core, 512)
    assert core.enter_loop()
    drain(core)
    run(core, 512 * 400)                          # ~4.6 s = several cycles
    evs = drain(core)
    assert "loop_exited" not in names(evs) and core.mode == "loop"
    assert names(evs).count("loop_cycle") >= 3
    # an explicit count still works for a manual entry
    core.seek(5.2)
    run(core, 512)
    assert core.enter_loop(count=2)
    drain(core)
    run(core, 512 * 400)
    evs = drain(core)
    assert names(evs) == ["loop_cycle", "loop_cycle", "loop_exited"]
    assert evs[-1][1]["reason"] == "count"
