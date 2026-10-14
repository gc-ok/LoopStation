"""
Loop Detection with Beat, Zero-Crossing, and Phase Analysis.

The detector keeps a reference to the engine's int16 memmap and only converts
the analysed region to float32 (never the whole song).
"""

import numpy as np
import logging

from config import DEFAULT_MIN_CONFIDENCE, MAX_LOOP_CANDIDATES

logger = logging.getLogger("LoopStation.Detector")

# Candidates whose start AND end are both within this many seconds are
# considered duplicates.
MERGE_TOLERANCE_S = 0.05


class CutCandidate:
    """Represents a suggested section to remove."""
    def __init__(self, start, end, confidence, description=""):
        self.start = start
        self.end = end
        self.confidence = confidence
        self.description = description
        self.duration = end - start


class LoopCandidate:
    def __init__(self, start, end, confidence, description=""):
        self.start = start
        self.end = end
        self.confidence = confidence
        self.description = description
        self.duration = end - start


def _try_import_librosa():
    try:
        import librosa
        return librosa
    except ImportError:
        return None


class LoopDetector:
    def __init__(self, raw_audio_data, sample_rate):
        """
        Args:
            raw_audio_data: int16 (frames, channels) array or memmap, or a
                float32 array. It is NOT copied here.
            sample_rate: sample rate of raw_audio_data
        """
        self.audio_data = raw_audio_data
        self.sr = int(sample_rate)
        self.total_frames = len(raw_audio_data) if raw_audio_data is not None else 0

    @property
    def duration(self):
        return self.total_frames / self.sr if self.sr else 0.0

    # ------------------------------------------------------------------
    # Region access
    # ------------------------------------------------------------------

    def _mono_region(self, start_time, end_time):
        """
        Slice [start_time, end_time) from the source and return it as a
        float32 mono array in [-1, 1]. Only this slice is read from disk /
        converted, so memory use is proportional to the region, not the song.
        """
        start = max(0, int(start_time * self.sr))
        end = min(self.total_frames, int(end_time * self.sr))
        if end <= start:
            return np.zeros(0, dtype=np.float32)

        chunk = self.audio_data[start:end]
        if np.issubdtype(np.asarray(chunk).dtype, np.integer):
            region = np.asarray(chunk, dtype=np.float32)
            region *= (1.0 / 32768.0)
        else:
            region = np.array(chunk, dtype=np.float32)

        if region.ndim > 1:
            region = region.mean(axis=1, dtype=np.float32)
        return region

    # ------------------------------------------------------------------
    # Loop detection
    # ------------------------------------------------------------------

    def find_loops(self, start_time, end_time, min_conf=DEFAULT_MIN_CONFIDENCE):
        """
        Find loop points using multiple analysis methods.

        Args:
            start_time, end_time: region to analyse (seconds)
            min_conf: minimum confidence (0-100) a candidate must reach

        Returns:
            Up to MAX_LOOP_CANDIDATES LoopCandidate objects, best first.
        """
        region = self._mono_region(start_time, end_time)
        if len(region) < self.sr:
            return []

        librosa = _try_import_librosa()
        if librosa is None:
            logger.error("Librosa not found - using zero-crossing fallback only")
            candidates = self._find_zero_crossing_loops(region, start_time)
        else:
            candidates = (
                self._find_beat_aligned_loops(region, start_time, librosa)
                + self._find_zero_crossing_loops(region, start_time)
                + self._find_harmonic_loops(region, start_time, librosa)
            )

        merged = self._merge_similar_candidates(candidates)
        merged = [c for c in merged if c.confidence >= min_conf]
        merged.sort(key=lambda x: x.confidence, reverse=True)
        return merged[:MAX_LOOP_CANDIDATES]

    def _find_beat_aligned_loops(self, region, start_time, librosa):
        """Find loops aligned to the beat grid (4/8/16/32 beats long)."""
        candidates = []
        try:
            tempo, beats = librosa.beat.beat_track(y=region, sr=self.sr)

            if isinstance(tempo, np.ndarray):
                tempo = float(tempo.item()) if tempo.size == 1 else float(tempo[0])
            else:
                tempo = float(tempo)

            if len(beats) < 4:
                return []

            beat_times = librosa.frames_to_time(beats, sr=self.sr)

            for beat_count in [4, 8, 16, 32]:
                if len(beat_times) < beat_count + 1:
                    continue

                for i in range(len(beat_times) - beat_count):
                    loop_start = start_time + float(beat_times[i])
                    loop_end = start_time + float(beat_times[i + beat_count])
                    duration = loop_end - loop_start

                    if duration < 1.0 or duration > 30.0:
                        continue

                    conf = min(100, 60 + (beat_count * 2))
                    candidates.append(LoopCandidate(
                        start=loop_start,
                        end=loop_end,
                        confidence=conf,
                        description=f"Beat-aligned ({beat_count} beats, {tempo:.0f} BPM)"
                    ))
        except Exception as e:
            logger.warning(f"Beat detection failed: {e}")

        return candidates

    def _find_zero_crossing_loops(self, region, start_time):
        """Find loops with zero-crossings at both ends."""
        candidates = []

        region_len = len(region)
        search_window = int(region_len * 0.1)
        if search_window < 100:
            return []

        start_crossings = self._find_zero_crossings(region[:search_window])
        end_crossings = self._find_zero_crossings(region[-search_window:])
        end_crossings = [x + (region_len - search_window) for x in end_crossings]

        if not start_crossings or not end_crossings:
            return []

        for start_idx in start_crossings[:5]:
            for end_idx in end_crossings[:5]:
                if end_idx <= start_idx:
                    continue

                duration_samples = end_idx - start_idx
                duration = duration_samples / self.sr
                if duration < 0.5 or duration > 30.0:
                    continue

                window = min(100, duration_samples // 10)
                if window <= 0 or start_idx + window >= region_len or end_idx - window < 0:
                    continue

                phase_conf = self._calculate_phase_similarity(
                    region[start_idx:start_idx + window],
                    region[end_idx - window:end_idx]
                )

                candidates.append(LoopCandidate(
                    start=start_time + (start_idx / self.sr),
                    end=start_time + (end_idx / self.sr),
                    confidence=70 + int(phase_conf * 30),  # 70-100%
                    description=f"Zero-crossing optimized (phase: {int(phase_conf * 100)}%)"
                ))

        return candidates

    def _find_harmonic_loops(self, region, start_time, librosa):
        """Harmonic (chroma) self-similarity method."""
        candidates = []
        try:
            hop_length = 512
            chroma = librosa.feature.chroma_cqt(y=region, sr=self.sr, hop_length=hop_length)
            frame_duration = hop_length / self.sr

            max_frames = chroma.shape[1] // 2
            min_frames = int(1.0 / frame_duration)

            for lag in range(min_frames, max_frames, 4):  # Step by 4 for speed
                n_compare = chroma.shape[1] - lag
                if n_compare <= 0:
                    continue

                sample_frames = min(n_compare, 50)
                chunk1 = chroma[:, :sample_frames]
                chunk2 = chroma[:, lag:lag + sample_frames]

                dots = np.einsum('ij,ij->j', chunk1, chunk2)
                norms = np.linalg.norm(chunk1, axis=0) * np.linalg.norm(chunk2, axis=0) + 1e-8
                similarity = float(np.mean(dots / norms))

                conf = int(similarity * 100)
                if conf > 50:
                    loop_dur = lag * frame_duration
                    candidates.append(LoopCandidate(
                        start=start_time,
                        end=start_time + loop_dur,
                        confidence=conf,
                        description=f"Harmonic match ({loop_dur:.2f}s)"
                    ))
        except Exception as e:
            logger.warning(f"Harmonic detection failed: {e}")

        return candidates

    @staticmethod
    def _find_zero_crossings(audio_segment):
        """Indices i where the signal changes sign between i-1 and i (vectorised)."""
        seg = np.asarray(audio_segment)
        if seg.size < 2:
            return []
        prev = seg[:-1]
        cur = seg[1:]
        mask = ((prev <= 0) & (cur > 0)) | ((prev >= 0) & (cur < 0))
        return (np.nonzero(mask)[0] + 1).tolist()

    @staticmethod
    def _calculate_phase_similarity(start_segment, end_segment):
        """Calculate how similar two segments are in phase/shape (0-1)."""
        if len(start_segment) != len(end_segment) or len(start_segment) == 0:
            return 0.0

        start_max = np.max(np.abs(start_segment))
        end_max = np.max(np.abs(end_segment))
        if start_max < 1e-8 or end_max < 1e-8:
            return 0.0

        start_norm = start_segment / start_max
        end_norm = end_segment / end_max

        try:
            correlation = np.corrcoef(start_norm, end_norm)[0, 1]
            if np.isnan(correlation):
                return 0.0
            return max(0.0, min(1.0, float(correlation)))
        except Exception:
            return 0.0

    @staticmethod
    def _merge_similar_candidates(candidates, tolerance=MERGE_TOLERANCE_S):
        """
        Merge candidates whose start AND end are both within ``tolerance``
        seconds, keeping the higher-confidence one. Candidates that share a
        start but have different lengths (e.g. 4- vs 8-beat loops) are kept.
        """
        if not candidates:
            return []

        ordered = sorted(candidates, key=lambda c: (c.start, c.end))
        merged = []

        for cand in ordered:
            # Only candidates near the tail of `merged` can share this start
            # (merged is start-ordered), so scan backwards until out of range.
            matched = False
            for k in range(len(merged) - 1, -1, -1):
                existing = merged[k]
                if cand.start - existing.start > tolerance:
                    break
                if abs(cand.end - existing.end) <= tolerance:
                    if cand.confidence > existing.confidence:
                        merged[k] = cand
                    matched = True
                    break
            if not matched:
                merged.append(cand)

        return merged

    def _find_zero_crossing_loops_only(self, start_time, end_time):
        """Fallback method when librosa is not available."""
        region = self._mono_region(start_time, end_time)
        return self._find_zero_crossing_loops(region, start_time)

    # ------------------------------------------------------------------
    # Smart cuts
    # ------------------------------------------------------------------

    def find_smart_cuts(self, start_time, end_time):
        """
        Analyze a selected region and propose beat-aligned cut points.

        Args:
            start_time: Rough start of the section to remove.
            end_time: Rough end of the section to remove.
        """
        candidates = []

        librosa = _try_import_librosa()
        if librosa is None:
            return [CutCandidate(start_time, end_time, 100, "Exact selection (No Librosa)")]

        try:
            # Look 2 s before and after the selection to catch the nearest beats
            analysis_start = max(0.0, start_time - 2.0)
            analysis_end = min(self.duration, end_time + 2.0)
            region = self._mono_region(analysis_start, analysis_end)
            if len(region) == 0:
                return [CutCandidate(start_time, end_time, 0, "Empty region")]

            tempo, beat_frames = librosa.beat.beat_track(y=region, sr=self.sr)
            beat_times = librosa.frames_to_time(beat_frames, sr=self.sr) + analysis_start

            if len(beat_times) < 2:
                return [CutCandidate(start_time, end_time, 50, "No clear beats found")]

            start_beat_idx = int((np.abs(beat_times - start_time)).argmin())
            snapped_start = float(beat_times[start_beat_idx])

            end_beat_idx = int((np.abs(beat_times - end_time)).argmin())
            snapped_end = float(beat_times[end_beat_idx])

            # Guard against inverted or zero-length cuts: enforce at least one
            # beat after the snapped start when one exists.
            if snapped_end <= snapped_start:
                if start_beat_idx + 1 < len(beat_times):
                    end_beat_idx = start_beat_idx + 1
                    snapped_end = float(beat_times[end_beat_idx])
                else:
                    end_beat_idx = start_beat_idx
                    snapped_end = snapped_start + 0.5  # Fallback

            beats_skipped = end_beat_idx - start_beat_idx
            is_musical_bar = beats_skipped > 0 and (
                (beats_skipped % 4 == 0) or (beats_skipped % 3 == 0))

            confidence = 90 if is_musical_bar else 70
            desc = f"Beat Snap ({beats_skipped} beats removed)"
            candidates.append(CutCandidate(snapped_start, snapped_end, confidence, desc))

            candidates.append(CutCandidate(start_time, end_time, 60, "Raw Selection"))

        except Exception as e:
            logger.error(f"Smart cut detection failed: {e}")
            candidates.append(CutCandidate(start_time, end_time, 0, "Detection Failed"))

        return candidates
