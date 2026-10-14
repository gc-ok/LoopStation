"""Tests for the pure waveform helpers: envelope and bar grid (no Tk window needed)."""
import os
import tempfile

import numpy as np
import pytest

from frontend.waveform import bar_times, compute_envelope


def test_empty_and_none_inputs():
    assert compute_envelope(None, 100).shape == (0,)
    assert compute_envelope(np.zeros((0, 2), dtype=np.int16), 100).shape == (0,)
    assert compute_envelope(np.ones((10, 2), dtype=np.int16), 0).shape == (0,)


def test_bucket_count_and_normalisation():
    rng = np.random.default_rng(0)
    samples = rng.integers(-20000, 20000, size=(10_000, 2), dtype=np.int16)
    env = compute_envelope(samples, 250)
    assert env.shape == (250,)
    assert env.dtype == np.float32
    assert env.max() == pytest.approx(1.0)
    assert env.min() >= 0.0


def test_max_abs_per_bucket_across_channels():
    # 8 frames, 2 channels, 4 buckets of 2 frames
    samples = np.array([
        [10, -5], [3, 2],        # bucket 0 -> 10
        [-40, 1], [0, 0],        # bucket 1 -> 40 (negative peak)
        [1, 20], [0, -2],        # bucket 2 -> 20 (right channel)
        [0, 0], [0, 0],          # bucket 3 -> 0
    ], dtype=np.int16)
    env = compute_envelope(samples, 4)
    np.testing.assert_allclose(env, np.array([10, 40, 20, 0]) / 40.0, rtol=1e-6)


def test_int16_minimum_does_not_overflow():
    samples = np.zeros((100, 2), dtype=np.int16)
    samples[10, 0] = -32768
    samples[60, 1] = 16384
    env = compute_envelope(samples, 2)
    np.testing.assert_allclose(env, [1.0, 0.5], rtol=1e-6)


def test_fewer_frames_than_buckets_and_mono():
    samples = np.array([1, -3, 2], dtype=np.int16)
    env = compute_envelope(samples, 2000)
    np.testing.assert_allclose(env, [1 / 3, 1.0, 2 / 3], rtol=1e-6)


def test_silence_stays_zero():
    env = compute_envelope(np.zeros((5000, 2), dtype=np.int16), 50)
    assert env.shape == (50,)
    assert not env.any()


def test_chunked_memmap_matches_in_memory(monkeypatch):
    import frontend.waveform as wf
    # Force several chunks so the chunk boundary code path is exercised
    monkeypatch.setattr(wf, "_ENVELOPE_CHUNK_FRAMES", 1024)

    rng = np.random.default_rng(1)
    data = rng.integers(-32768, 32767, size=(50_000, 2), dtype=np.int16)

    fd, path = tempfile.mkstemp(suffix=".raw")
    os.close(fd)
    try:
        data.tofile(path)
        mm = np.memmap(path, dtype=np.int16, mode="r", shape=data.shape)
        env_mm = compute_envelope(mm, 2000)
        del mm
    finally:
        os.unlink(path)

    env_mem = compute_envelope(data, 2000)
    np.testing.assert_array_equal(env_mm, env_mem)

    # Reference: plain max(|x|) per bucket on the whole array
    bucket = data.shape[0] // 2000
    ref = np.abs(data[:bucket * 2000].astype(np.int32)).reshape(2000, -1).max(axis=1)
    ref = ref / ref.max()
    np.testing.assert_allclose(env_mem, ref, rtol=1e-6)


# --------------------------------------------------------------------------
# bar_times: bar-grid math
# --------------------------------------------------------------------------

def test_bar_times_basic_120bpm_4_4():
    # 120 bpm, 4/4 -> 2.0 s per bar, downbeat at 0
    bars = bar_times(120, 4, 0.0, 0.0, 10.0)
    assert [n for _, n in bars] == [1, 2, 3, 4, 5, 6]
    np.testing.assert_allclose([t for t, _ in bars], [0, 2, 4, 6, 8, 10])


def test_bar_times_offset_and_window():
    # 90 bpm 3/4 -> 2.0 s per bar; first downbeat at 0.5 s
    bars = bar_times(90, 3, 0.5, 3.0, 9.0)
    np.testing.assert_allclose([t for t, _ in bars], [4.5, 6.5, 8.5])
    assert [n for _, n in bars] == [3, 4, 5]


def test_bar_times_pickup_bars_before_downbeat():
    # Downbeat at 3.0 s, 2.0 s bars: the bar starting at 1.0 s is the pickup (bar 0)
    bars = bar_times(120, 4, 3.0, 0.0, 5.0)
    assert bars == [(pytest.approx(1.0), 0), (pytest.approx(3.0), 1), (pytest.approx(5.0), 2)]


def test_bar_times_matches_state_manager_numbering():
    """Bar n from bar_times is the bar bar_beat_at reports just after that time."""
    bpm, bpb, offset = 137.0, 7, 1.234
    bar_len = 60.0 / bpm * bpb
    for t, n in bar_times(bpm, bpb, offset, 0.0, 60.0):
        total_beats = (t + 1e-6 - offset) * bpm / 60.0
        assert int(np.floor(total_beats)) // bpb + 1 == n
        assert t == pytest.approx(offset + (n - 1) * bar_len)


def test_bar_times_inclusive_edges_and_float_noise():
    # 100 bpm 4/4 -> 2.4 s bars; 2.4 * 3 = 7.199999... must still be included
    bars = bar_times(100, 4, 0.0, 2.4, 7.2)
    assert [n for _, n in bars] == [2, 3, 4]


def test_bar_times_disabled_or_invalid():
    assert bar_times(0, 4, 0.0, 0.0, 100.0) == []
    assert bar_times(-120, 4, 0.0, 0.0, 100.0) == []
    assert bar_times(120, 4, 0.0, 10.0, 5.0) == []
    assert bar_times(float("nan"), 4, 0.0, 0.0, 10.0) == []
    assert bar_times(None, 4, 0.0, 0.0, 10.0) == []


def test_bar_times_beats_per_bar_floor_and_empty_window():
    # beats_per_bar < 1 is treated as 1 (one beat per bar): 60 bpm -> 1 s bars
    assert [n for _, n in bar_times(60, 0, 0.0, 0.0, 3.0)] == [1, 2, 3, 4]
    # a window between two bar lines has no bars
    assert bar_times(60, 4, 0.0, 0.5, 3.5) == []


def test_bar_times_is_ascending_and_bounded():
    bars = bar_times(128, 4, 0.37, 12.3, 245.6)
    times = [t for t, _ in bars]
    assert times == sorted(times)
    assert all(12.3 - 1e-9 <= t <= 245.6 + 1e-9 for t in times)
    nums = [n for _, n in bars]
    assert nums == list(range(nums[0], nums[0] + len(nums)))
