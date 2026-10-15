"""Background preload of the next song (AudioEngine.preload_file & co.)."""
import gc
import os
import stat
import sys
import threading
import time

import numpy as np
import pytest

from backend import audio_engine
from backend.audio_engine import AudioEngine, MixerCore, TEMP_FILE_PREFIX

from helpers_audio import (
    find_ffmpeg, make_signal, write_wav, wait_until, fake_stream_factory,
)
from fake_engine import FakeEngine

SR = 44100
FFMPEG = find_ffmpeg()
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not available")
NO_FFMPEG = "definitely-not-an-ffmpeg-binary"


# ================================================================ helpers ==

class Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, name, payload):
        self.events.append((name, payload))

    def names(self):
        return [e[0] for e in self.events]

    def of(self, name):
        return [p for n, p in self.events if n == name]


@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    root = tmp_path / "systmp"
    root.mkdir()
    monkeypatch.setattr(audio_engine.tempfile, "gettempdir", lambda: str(root))
    return root


@pytest.fixture
def make_engine(temp_root):
    made = []

    def factory(ffmpeg=FFMPEG):
        streams = []
        eng = AudioEngine(ffmpeg_path=ffmpeg, stream_factory=fake_stream_factory(False, streams))
        rec = Recorder()
        eng.on_event(rec)
        made.append(eng)
        return eng, streams, rec
    yield factory
    for eng in made:
        eng.close()


@pytest.fixture
def popen_count(monkeypatch):
    """Counts every subprocess the engine starts (ffmpeg runs)."""
    calls = []
    real = audio_engine.subprocess.Popen

    def counting(cmd, *a, **kw):
        calls.append((threading.current_thread().name, list(cmd)))
        return real(cmd, *a, **kw)
    monkeypatch.setattr(audio_engine.subprocess, "Popen", counting)
    return calls


@pytest.fixture
def song(tmp_path):
    mix = make_signal(2.0)
    click = make_signal(1.5, freq=1000.0)      # shorter: gets padded
    return (write_wav(tmp_path / "song.wav", mix), write_wav(tmp_path / "click.wav", click),
            mix, click)


def temp_files(eng):
    gc.collect()
    return sorted(n for n in os.listdir(eng.temp_dir) if n.startswith(TEMP_FILE_PREFIX))


def basenames(paths):
    return sorted(os.path.basename(p) for p in paths)


def ready(rec, path):
    return wait_until(lambda: any(p["path"] == path for p in rec.of("preload_ready"))
                      or rec.of("preload_failed"), timeout=30.0)


# ===================================================== adoption / no ffmpeg ==

@needs_ffmpeg
def test_preload_then_load_adopts_files_without_ffmpeg(make_engine, song, popen_count):
    path, click_path, mix, click = song
    eng, streams, rec = make_engine()
    assert eng.preload_file(path, {"click": click_path})
    assert ready(rec, path) and not rec.of("preload_failed"), rec.events
    assert all(name == "AudioPreload" for name, _ in popen_count) and len(popen_count) == 2
    assert eng.is_preloaded(path, {"click": click_path})
    assert eng.is_preloaded(path, {"click": click_path}, (1.0, 0.0))
    assert not eng.is_preloaded(path, {"click": click_path}, (0.9, 0.0))
    assert not eng.is_preloaded(path)                       # stems differ
    pre_files = temp_files(eng)
    assert len(pre_files) == 2     # decoded mix + padded click (unpadded one deleted)

    wave_view = eng.get_preloaded_audio(path)
    assert wave_view is not None and not wave_view.flags.writeable
    assert np.array_equal(wave_view, mix)
    del wave_view

    # same request again: no-op
    assert eng.preload_file(path, {"click": click_path})
    assert len(popen_count) == 2

    n_before = len(popen_count)
    length, ratio = eng.load_file(path, {"click": click_path})
    assert len(popen_count) == n_before                    # no ffmpeg at all
    assert ratio == 1.0 and length == pytest.approx(len(mix) / SR)
    assert np.array_equal(eng.get_raw_audio_data(), mix)
    buffers = eng.core.get_stem_buffers()
    assert list(buffers) == ["mix", "click"]
    assert np.array_equal(buffers["click"][:len(click)], click)
    assert not buffers["click"][len(click):].any()
    assert basenames(eng._temp_paths) == pre_files         # files now owned by the song
    assert not eng.is_preloaded(path, {"click": click_path})   # slot consumed
    assert eng.get_tempo_pitch() == (1.0, 0.0)

    eng.play_transport(0.5)
    streams[-1].pump(512)
    assert eng.get_position() == pytest.approx(0.5 + 512 / SR, abs=1e-4)

    eng.cleanup()
    assert temp_files(eng) == []


@needs_ffmpeg
def test_tempo_rendered_preload_matches_set_tempo_pitch(make_engine, song, popen_count):
    path, click_path, mix, _click = song
    stems = {"click": click_path}
    tp = (0.8, 2.0)

    # Reference: normal load + set_tempo_pitch render.
    ref, ref_streams, ref_rec = make_engine()
    ref.load_file(path, stems)
    ref.set_tempo_pitch(*tp)
    assert wait_until(lambda: ref.core._pending_swap is not None
                      or "render_failed" in ref_rec.names(), timeout=30.0)
    ref.core.apply_pending_swap()
    assert ref.get_tempo_pitch() == tp

    eng, streams, rec = make_engine()
    eng.preload_file(path, stems, tp)
    assert ready(rec, path) and not rec.of("preload_failed"), rec.events
    assert eng.is_preloaded(path, stems, tp) and eng.is_preloaded(path, stems)
    assert not eng.is_preloaded(path, stems, (0.9, 2.0))
    n_before = len(popen_count)
    eng.load_file(path, stems, tempo_pitch=tp)
    assert len(popen_count) == n_before
    assert eng.get_tempo_pitch() == tp
    assert "render_started" not in rec.names() and "render_done" not in rec.names()

    # identical audio and lengths to the set_tempo_pitch render
    assert eng.core.n_source_frames == ref.core.n_source_frames == len(mix)
    assert eng.core.n_active_frames == ref.core.n_active_frames == int(round(len(mix) / 0.8))
    for name in ("mix", "click"):
        assert np.array_equal(eng.core._stems[name].active, ref.core._stems[name].active)
        assert np.array_equal(eng.core._stems[name].source, ref.core._stems[name].source)
    assert np.array_equal(eng.get_raw_audio_data(), mix)    # source tempo
    assert eng.get_playback_rate() == pytest.approx(0.8, rel=1e-3)
    assert len(eng._render_paths) == 2

    # source-second positions, loops and holds map like a completed swap
    eng.set_loop_points(0.5, 1.0)
    ref.set_loop_points(0.5, 1.0)
    assert eng.is_loop_ready() and eng.get_loop_points() == (0.5, 1.0, 20)
    assert (eng.core._loop_in_f, eng.core._loop_out_f, eng.core._body_len) == \
        (ref.core._loop_in_f, ref.core._loop_out_f, ref.core._body_len)
    eng.hold_at(1.5)
    assert eng.core._hold_f == int(round(1.5 * SR / eng.core._scale()))
    eng.play_transport(1.2)
    streams[-1].pump(512)
    assert eng.get_position() == pytest.approx(1.2 + 0.8 * 512 / SR, abs=1e-3)

    # back to the original tempo: swaps to the sources, renders retired
    rendered = list(eng._render_paths)
    eng.set_tempo_pitch(1.0, 0.0)
    streams[-1].pump(512)
    assert eng.get_tempo_pitch() == (1.0, 0.0)
    assert eng.core.n_active_frames == len(mix)
    gc.collect()
    eng._retire_render_paths([])
    assert not any(os.path.exists(p) for p in rendered)
    eng.cleanup()
    ref.cleanup()               # same temp dir
    assert temp_files(eng) == []


# ============================================================ mismatches ==

@needs_ffmpeg
def test_different_stems_decode_normally_and_keep_preload(make_engine, song, popen_count):
    path, click_path, mix, _ = song
    eng, _s, rec = make_engine()
    eng.preload_file(path, {"click": click_path})
    assert ready(rec, path)
    n = len(popen_count)
    eng.load_file(path)                                  # no stems: not the preload
    assert len(popen_count) == n + 1
    assert eng.is_preloaded(path, {"click": click_path})  # still there for later
    eng.load_file(path, {"click": click_path})
    assert len(popen_count) == n + 1                      # now adopted


@needs_ffmpeg
def test_changed_file_falls_back_and_deletes_stale_preload(make_engine, song, popen_count):
    path, _c, mix, _ = song
    eng, _s, rec = make_engine()
    eng.preload_file(path)
    assert ready(rec, path)
    stale = temp_files(eng)
    new = make_signal(2.5, freq=330.0)
    write_wav(path, new)                                  # size changes
    assert not eng.is_preloaded(path)
    n = len(popen_count)
    eng.load_file(path)
    assert len(popen_count) == n + 1
    assert np.array_equal(eng.get_raw_audio_data(), new)
    assert not set(stale) & set(temp_files(eng))
    assert len(temp_files(eng)) == 1


@needs_ffmpeg
def test_different_tempo_adopts_decode_only(make_engine, song, popen_count):
    path, _c, mix, _ = song
    eng, _s, rec = make_engine()
    eng.preload_file(path, None, (0.8, 0.0))
    assert ready(rec, path)
    assert len(temp_files(eng)) == 2                      # decode + render
    n = len(popen_count)
    eng.load_file(path, None, tempo_pitch=(0.9, 0.0))
    assert len(popen_count) == n
    assert eng.get_tempo_pitch() == (1.0, 0.0)            # state manager renders 0.9
    assert eng.core.n_active_frames == len(mix)
    assert len(temp_files(eng)) == 1                      # unused render deleted
    assert eng._render_paths == []


@needs_ffmpeg
def test_load_without_tempo_ignores_render(make_engine, song):
    path, _c, mix, _ = song
    eng, _s, rec = make_engine()
    eng.preload_file(path, None, (0.8, 0.0))
    assert ready(rec, path)
    eng.load_file(path)
    assert eng.get_tempo_pitch() == (1.0, 0.0) and len(temp_files(eng)) == 1


# =================================================== replacement / cancel ==

@needs_ffmpeg
def test_replacement_deletes_old_files(make_engine, song, tmp_path):
    path, _c, _mix, _ = song
    other = write_wav(tmp_path / "other.wav", make_signal(1.0, freq=550.0))
    eng, _s, rec = make_engine()
    eng.preload_file(path)
    assert ready(rec, path)
    old = temp_files(eng)
    eng.preload_file(other)
    assert ready(rec, other)
    now = temp_files(eng)
    assert len(now) == 1 and not set(old) & set(now)
    assert not eng.is_preloaded(path) and eng.is_preloaded(other)
    assert eng.get_preloaded_audio(path) is None


@needs_ffmpeg
@pytest.mark.parametrize("how", ["cancel", "cleanup", "close"])
def test_cancel_cleanup_close_delete_preload_files(make_engine, song, how):
    path, click_path, _mix, _ = song
    eng, _s, rec = make_engine()
    eng.preload_file(path, {"click": click_path}, (1.1, -1.0))
    assert ready(rec, path)
    assert len(temp_files(eng)) == 4
    getattr(eng, {"cancel": "cancel_preload"}.get(how, how))()
    assert temp_files(eng) == []
    assert not eng.is_preloaded(path, {"click": click_path})


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell script as ffmpeg")
def test_replacement_kills_a_running_ffmpeg_promptly(make_engine, song, tmp_path):
    path, _c, _mix, _ = song
    slow = tmp_path / "slow-ffmpeg"
    slow.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
    slow.chmod(slow.stat().st_mode | stat.S_IEXEC)
    eng, _s, rec = make_engine(str(slow))
    eng.preload_file(path)
    pre = eng._preload
    assert wait_until(lambda: pre.state == "running" and pre.thread.is_alive())
    time.sleep(0.2)                                     # ffmpeg is running
    other = write_wav(tmp_path / "other.wav", make_signal(1.0))
    t0 = time.monotonic()
    eng.preload_file(other)                             # non-blocking
    assert time.monotonic() - t0 < 0.5
    assert pre.done.wait(3.0) and pre.state == "cancelled"
    assert time.monotonic() - t0 < 3.0
    eng.cancel_preload()
    assert temp_files(eng) == []
    assert "preload_ready" not in rec.names()


# ============================================================== failures ==

@needs_ffmpeg
def test_preload_failed_on_a_bad_file(make_engine, tmp_path):
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"this is not audio" * 100)
    eng, _s, rec = make_engine()
    eng.preload_file(str(bad))
    assert wait_until(lambda: rec.of("preload_failed"), timeout=20.0)
    failed = rec.of("preload_failed")[0]
    assert failed["path"] == str(bad) and failed["error"]
    assert not eng.is_preloaded(str(bad)) and temp_files(eng) == []
    assert eng.preload_file(str(bad)) is False          # same request: no retry loop
    assert len(rec.of("preload_failed")) == 1
    with pytest.raises(RuntimeError):
        eng.load_file(str(bad))


def test_preload_failed_on_a_missing_file(make_engine, tmp_path):
    eng, _s, rec = make_engine(NO_FFMPEG)
    assert eng.preload_file(str(tmp_path / "missing.wav")) is False
    assert wait_until(lambda: rec.of("preload_failed"))
    assert rec.of("preload_failed")[0]["error"] == "file not found"


def test_native_wav_preload_without_ffmpeg_owns_no_files(make_engine, song):
    path, _c, mix, _ = song
    eng, _s, rec = make_engine(NO_FFMPEG)
    eng.preload_file(path)
    assert ready(rec, path) and eng.is_preloaded(path)
    assert temp_files(eng) == []
    eng.load_file(path)
    assert np.array_equal(eng.get_raw_audio_data(), mix) and os.path.exists(path)
    eng.cleanup()
    assert os.path.exists(path)                          # the source is never deleted


@needs_ffmpeg
def test_render_failure_keeps_the_decode(make_engine, song, monkeypatch):
    path, _c, mix, _ = song
    eng, _s, rec = make_engine()

    def broken(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(eng, "_render_stem_file", broken)
    eng.preload_file(path, None, (0.8, 0.0))
    assert wait_until(lambda: rec.of("preload_failed"), timeout=20.0)
    assert "render" in rec.of("preload_failed")[0]["error"]
    assert eng.is_preloaded(path) and not eng.is_preloaded(path, None, (0.8, 0.0))
    eng.load_file(path, None, (0.8, 0.0))
    assert eng.get_tempo_pitch() == (1.0, 0.0) and len(temp_files(eng)) == 1


# ================================================= loads while preloading ==

@needs_ffmpeg
def test_load_waits_for_a_running_matching_preload(make_engine, song, popen_count, monkeypatch):
    path, click_path, mix, _ = song
    eng, _s, rec = make_engine()
    gate = threading.Event()
    in_preload = threading.Event()
    real = eng._decode

    def gated(p, *a, **kw):
        if threading.current_thread().name == "AudioPreload":
            in_preload.set()
            assert gate.wait(10)
        return real(p, *a, **kw)
    monkeypatch.setattr(eng, "_decode", gated)
    eng.preload_file(path, {"click": click_path})
    assert in_preload.wait(5)
    result = []
    loader = threading.Thread(target=lambda: result.append(eng.load_file(path, {"click": click_path})))
    loader.start()
    time.sleep(0.2)
    assert loader.is_alive()                              # waiting, not decoding
    gate.set()
    loader.join(20)
    assert result and result[0][0] == pytest.approx(len(mix) / SR)
    assert all(name == "AudioPreload" for name, _ in popen_count)   # load ran no ffmpeg
    assert np.array_equal(eng.get_raw_audio_data(), mix)
    assert len(temp_files(eng)) == 2


@needs_ffmpeg
def test_load_skips_a_render_it_does_not_need(make_engine, song, monkeypatch):
    path, _c, mix, _ = song
    eng, _s, rec = make_engine()
    started = threading.Event()
    real = eng._run_ffmpeg

    def slow_render(args, out, what, should_stop=None):
        if what.startswith("rendering"):
            started.set()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if should_stop and should_stop():
                    raise audio_engine._Cancelled()
                time.sleep(0.01)
        return real(args, out, what, should_stop=should_stop)
    monkeypatch.setattr(eng, "_run_ffmpeg", slow_render)
    eng.preload_file(path, None, (0.8, 0.0))
    assert started.wait(10)
    t0 = time.monotonic()
    eng.load_file(path)                                   # tempo not wanted
    assert time.monotonic() - t0 < 3.0
    assert eng.get_tempo_pitch() == (1.0, 0.0)
    assert np.array_equal(eng.get_raw_audio_data(), mix)
    assert len(temp_files(eng)) == 1


@needs_ffmpeg
def test_load_of_another_song_during_preload_is_safe(make_engine, song, tmp_path):
    path, click_path, _mix, _ = song
    current = make_signal(1.0, freq=660.0)
    current_path = write_wav(tmp_path / "current.wav", current)
    eng, streams, rec = make_engine()
    eng.preload_file(path, {"click": click_path}, (0.9, 0.0))
    eng.load_file(current_path)                           # while the preload runs
    eng.play_transport(0.0)
    streams[-1].pump(2048)
    assert np.array_equal(eng.get_raw_audio_data(), current)
    assert ready(rec, path) and not rec.of("preload_failed")
    assert eng.is_preloaded(path, {"click": click_path}, (0.9, 0.0))
    assert len(temp_files(eng)) == 1 + 4
    eng.load_file(path, {"click": click_path}, (0.9, 0.0))
    assert eng.get_tempo_pitch() == (0.9, 0.0)
    assert len(temp_files(eng)) == 4                      # previous song released


# ================================================================== core ==

def test_core_set_stems_with_active_buffers_maps_source_seconds():
    src = make_signal(2.0)
    act = np.ascontiguousarray(np.repeat(src, 2, axis=0)[:int(len(src) * 1.25)])
    core = MixerCore(SR, 0)
    core.set_stems({"mix": src}, active={"mix": act}, tempo_ratio=0.8, semitones=1.0)
    assert (core.tempo_ratio, core.semitones) == (0.8, 1.0)
    assert core.n_source_frames == len(src) and core.n_active_frames == len(act)
    assert core.get_stem_buffers()["mix"] is src
    core.play(1.0)
    assert core.get_position() == pytest.approx(1.0, abs=1e-4)
    core.render(441)
    assert core.get_position() == pytest.approx(1.0 + 441 * 0.8 / SR, abs=1e-4)
    with pytest.raises(ValueError):
        core.set_stems({"mix": src}, active={"other": act})
    core.set_stems({"mix": src})                          # plain call: identity
    assert (core.tempo_ratio, core.n_active_frames) == (1.0, len(src))


# ===================================================== FakeEngine parity ==

def test_fake_engine_preload_parity(tmp_path):
    data = make_signal(1.0)
    path = write_wav(tmp_path / "a.wav", data)
    click = write_wav(tmp_path / "a-click.wav", data)
    fake = FakeEngine()
    assert fake.preload_file(path, {"click": click}, (0.9, 1.0))
    assert fake.event_names() == ["preload_ready"]
    assert fake.calls_to("preload_file")[0][1] == (path, {"click": click})
    assert fake.is_preloaded(path, {"click": click}, (0.9, 1.0))
    assert fake.is_preloaded(path, {"click": click}) and not fake.is_preloaded(path)
    assert not fake.is_preloaded(path, {"click": click}, (0.8, 1.0))
    audio = fake.get_preloaded_audio(path)
    assert np.array_equal(audio, data) and not audio.flags.writeable
    fake.preload_file(path, {"click": click}, (0.9, 1.0))         # no-op
    assert fake.event_names() == ["preload_ready"]
    fake.load_file(path, {"click": click}, tempo_pitch=(0.9, 1.0))
    assert fake.loaded_from_preload and fake.get_tempo_pitch() == (0.9, 1.0)
    assert fake.calls_to("load_file")[-1][2] == {"tempo_pitch": (0.9, 1.0)}
    assert not fake.is_preloaded(path, {"click": click})

    # old two-argument call keeps its old record shape
    fake.load_file(path, None)
    assert fake.calls_to("load_file")[-1][1:] == ((path, None), {})
    assert not fake.loaded_from_preload and fake.get_tempo_pitch() == (1.0, 0.0)

    # controllable preload, failure and cancel
    fake.preload_sync = False
    fake.preload_file(path)
    assert not fake.is_preloaded(path)
    fake.finish_preload(ok=False, error="nope")
    assert fake.events[-1] == ("preload_failed", {"path": path, "error": "nope"})
    fake.preload_file(str(tmp_path / "b.wav"))
    assert fake.events[-1][0] == "preload_failed"
    fake.preload_sync = True
    fake.preload_file(path)
    fake.cancel_preload()
    assert not fake.is_preloaded(path) and fake.calls_to("cancel_preload")
