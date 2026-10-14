"""Tests for backend/loop_detector.py."""
import types

import numpy as np
import pytest

from backend import loop_detector
from backend.loop_detector import LoopDetector, LoopCandidate

from helpers_audio import make_signal, write_wav

SR = 44100


class RecordingArray:
    """Wraps an int16 array and refuses whole-array conversions."""

    def __init__(self, data):
        self._data = data
        self.slices = []
        self.dtype = data.dtype
        self.shape = data.shape

    def __len__(self):
        return len(self._data)

    def __getitem__(self, item):
        self.slices.append(item)
        return self._data[item]

    def astype(self, *_args, **_kwargs):  # pragma: no cover - guard
        raise AssertionError("whole-song conversion must not happen")

    def __array__(self, *_args, **_kwargs):  # pragma: no cover - guard
        raise AssertionError("whole-song conversion must not happen")


def test_init_does_not_convert_whole_song_and_region_is_sliced_first():
    data = make_signal(10.0)
    wrapped = RecordingArray(data)

    det = LoopDetector(wrapped, SR)  # must not touch the samples
    assert wrapped.slices == []

    region = det._mono_region(2.0, 3.0)
    assert len(wrapped.slices) == 1
    assert wrapped.slices[0] == slice(2 * SR, 3 * SR)
    assert region.dtype == np.float32
    assert len(region) == SR
    expected = data[2 * SR:3 * SR].astype(np.float32).mean(axis=1) / 32768.0
    assert np.allclose(region, expected, atol=1e-6)


def test_mono_region_from_memmap(tmp_path):
    data = make_signal(2.0)
    path = write_wav(tmp_path / "m.wav", data)
    mm = np.memmap(path, dtype=np.int16, mode="r", offset=44, shape=(len(data), 2))
    det = LoopDetector(mm, SR)
    region = det._mono_region(0.5, 0.75)
    assert len(region) == int(0.25 * SR)
    assert det.duration == pytest.approx(2.0)
    # Out-of-range / inverted regions are empty, not errors
    assert len(det._mono_region(5.0, 6.0)) == 0
    assert len(det._mono_region(1.0, 0.5)) == 0
    del mm


def test_find_zero_crossings_vectorised_matches_reference():
    rng = np.random.default_rng(7)
    seg = rng.normal(size=5000).astype(np.float32)
    seg[::97] = 0.0  # include exact zeros to exercise the <= / >= edges

    def reference(a):
        out = []
        for i in range(1, len(a)):
            if (a[i - 1] <= 0 and a[i] > 0) or (a[i - 1] >= 0 and a[i] < 0):
                out.append(i)
        return out

    assert LoopDetector._find_zero_crossings(seg) == reference(seg)
    assert LoopDetector._find_zero_crossings(np.zeros(1)) == []


def test_merge_requires_start_and_end_to_match():
    cands = [
        LoopCandidate(10.00, 12.00, 70, "a"),
        LoopCandidate(10.01, 12.02, 80, "b"),   # duplicate of a, higher conf
        LoopCandidate(10.00, 14.00, 60, "c"),   # same start, different end: keep
        LoopCandidate(10.02, 18.00, 65, "d"),   # same start, different end: keep
        LoopCandidate(20.00, 22.00, 50, "e"),
    ]
    merged = LoopDetector._merge_similar_candidates(cands)
    ends = sorted((round(c.start, 2), round(c.end, 2), c.confidence) for c in merged)
    assert ends == [(10.0, 14.0, 60), (10.01, 12.02, 80), (10.02, 18.0, 65), (20.0, 22.0, 50)]
    assert LoopDetector._merge_similar_candidates([]) == []


def test_find_loops_honours_min_conf(monkeypatch):
    det = LoopDetector(make_signal(3.0), SR)
    monkeypatch.setattr(loop_detector, "_try_import_librosa", lambda: None)
    monkeypatch.setattr(det, "_find_zero_crossing_loops", lambda region, start: [
        LoopCandidate(0.0, 1.0, 40, "low"),
        LoopCandidate(0.0, 2.0, 90, "high"),
        LoopCandidate(0.5, 2.5, 50, "edge"),
    ])
    got = det.find_loops(0.0, 3.0, min_conf=50)
    assert [c.confidence for c in got] == [90, 50]
    assert det.find_loops(0.0, 3.0, min_conf=95) == []
    # Too-short regions return nothing
    assert det.find_loops(0.0, 0.5) == []


def _fake_librosa(beat_times, tempo=120.0):
    """Minimal librosa stand-in: beat_track returns frame indices, frames_to_time maps them back."""
    frames = np.arange(len(beat_times))
    fake = types.SimpleNamespace()
    fake.beat = types.SimpleNamespace(beat_track=lambda y, sr: (np.array([tempo]), frames))
    fake.frames_to_time = lambda f, sr: np.asarray(beat_times, dtype=float)[np.asarray(f)]
    return fake


def test_beat_aligned_description_counts_beats_not_bars():
    det = LoopDetector(make_signal(1.0), SR)
    beats = [i * 0.5 for i in range(10)]  # 120 BPM, 4.5 s of beats
    cands = det._find_beat_aligned_loops(np.zeros(10), 1.0, _fake_librosa(beats))
    assert cands, "expected 4- and 8-beat candidates"
    assert all("beats" in c.description and "bars" not in c.description for c in cands)
    four = [c for c in cands if "(4 beats" in c.description]
    assert four and four[0].start == pytest.approx(1.0) and four[0].end == pytest.approx(3.0)


def test_smart_cut_collapsed_guard_uses_next_beat(monkeypatch):
    det = LoopDetector(make_signal(5.0), SR)
    beats = [0.5, 1.0, 1.5]
    monkeypatch.setattr(loop_detector, "_try_import_librosa", lambda: _fake_librosa(beats))

    # Selection collapses onto the middle beat: next beat is used, no IndexError
    cands = det.find_smart_cuts(1.0, 1.0)
    snap = cands[0]
    assert snap.description.startswith("Beat Snap")
    assert (snap.start, snap.end) == pytest.approx((1.0, 1.5))
    assert "1 beats removed" in snap.description

    # Selection collapses onto the LAST beat: falls back to +0.5 s
    cands = det.find_smart_cuts(1.5, 1.5)
    snap = cands[0]
    assert (snap.start, snap.end) == pytest.approx((1.5, 2.0))
    assert "0 beats removed" in snap.description


def test_smart_cut_without_librosa_returns_selection(monkeypatch):
    det = LoopDetector(make_signal(1.0), SR)
    monkeypatch.setattr(loop_detector, "_try_import_librosa", lambda: None)
    cands = det.find_smart_cuts(0.2, 0.6)
    assert len(cands) == 1 and (cands[0].start, cands[0].end) == (0.2, 0.6)


def test_find_loops_smoke_with_real_librosa():
    pytest.importorskip("librosa")
    # Click track at 120 BPM so beat tracking has something to find
    data = np.zeros((SR * 4, 2), dtype=np.int16)
    for beat in range(8):
        i = int(beat * 0.5 * SR)
        data[i:i + 200] = 20000
    det = LoopDetector(data, SR)
    result = det.find_loops(0.0, 4.0, min_conf=0)
    assert isinstance(result, list)
    assert all(c.end > c.start for c in result)
