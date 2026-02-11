"""
Improved Loop Detection with Beat, Zero-Crossing, and Phase Analysis
"""

import numpy as np
import logging

logger = logging.getLogger("LoopStation.Detector")

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

class LoopDetector:
    def __init__(self, raw_audio_data, sample_rate):
        self.audio_data = raw_audio_data
        self.sr = sample_rate
        
        # Convert to mono float32
        if self.audio_data.dtype != np.float32:
            self.float_data = self.audio_data.astype(np.float32) / 32768.0
        else:
            self.float_data = self.audio_data
            
        if len(self.float_data.shape) > 1:
            self.mono_data = np.mean(self.float_data, axis=1)
        else:
            self.mono_data = self.float_data

    def find_loops(self, start_time, end_time, min_conf=50):
        """Find loop points using multiple analysis methods."""
        try:
            import librosa
        except ImportError:
            logger.error("Librosa not found - using fallback method")
            return self._find_zero_crossing_loops_only(start_time, end_time)

        start_sample = int(start_time * self.sr)
        end_sample = int(end_time * self.sr)
        
        region = self.mono_data[start_sample:end_sample]
        
        if len(region) < self.sr:
            return []

        candidates = []
        
        # METHOD 1: Beat-aligned musical loops
        beat_candidates = self._find_beat_aligned_loops(region, start_time)
        
        # METHOD 2: Zero-crossing optimized loops
        zc_candidates = self._find_zero_crossing_loops(region, start_time)
        
        # METHOD 3: Phase-coherent loops (original harmonic method)
        harmonic_candidates = self._find_harmonic_loops(region, start_time)
        
        # Merge and rank candidates
        all_candidates = beat_candidates + zc_candidates + harmonic_candidates
        
        # Deduplicate (merge candidates within 50ms of each other)
        merged = self._merge_similar_candidates(all_candidates)
        
        # Sort by confidence
        merged.sort(key=lambda x: x.confidence, reverse=True)
        
        return merged[:10]

    def _find_beat_aligned_loops(self, region, start_time):
        """Find loops aligned to beat grid."""
        try:
            import librosa
        except:
            return []
        
        candidates = []
        
        try:
            # Detect tempo and beats
            tempo, beats = librosa.beat.beat_track(y=region, sr=self.sr)
            
            # Convert tempo to scalar if it's an array
            if isinstance(tempo, np.ndarray):
                tempo = float(tempo.item()) if tempo.size == 1 else float(tempo[0])
            else:
                tempo = float(tempo)
            
            if len(beats) < 4:
                return []
            
            beat_times = librosa.frames_to_time(beats, sr=self.sr)
            
            # Try loops of different bar lengths (4, 8, 16, 32 beats)
            for bar_length in [4, 8, 16, 32]:
                if len(beat_times) < bar_length + 1:
                    continue
                
                for i in range(len(beat_times) - bar_length):
                    loop_start = start_time + float(beat_times[i])
                    loop_end = start_time + float(beat_times[i + bar_length])
                    duration = loop_end - loop_start
                    
                    if duration < 1.0 or duration > 30.0:
                        continue
                    
                    # Calculate confidence based on bar length
                    bar_conf = min(100, 60 + (bar_length * 2))
                    
                    candidates.append(LoopCandidate(
                        start=loop_start,
                        end=loop_end,
                        confidence=bar_conf,
                        description=f"Beat-aligned ({bar_length} bars, {tempo:.0f} BPM)"
                    ))
        except Exception as e:
            logger.warning(f"Beat detection failed: {e}")
        
        return candidates

    def _find_zero_crossing_loops(self, region, start_time):
        """Find loops with zero-crossings at both ends."""
        candidates = []
        
        # Find zero crossings in first 10% and last 10% of region
        region_len = len(region)
        search_window = int(region_len * 0.1)
        
        if search_window < 100:
            return []
        
        # Find zero crossings at start
        start_crossings = self._find_zero_crossings(region[:search_window])
        
        # Find zero crossings at end
        end_crossings = self._find_zero_crossings(region[-search_window:])
        end_crossings = [x + (region_len - search_window) for x in end_crossings]
        
        if not start_crossings or not end_crossings:
            return []
        
        # Create candidates for combinations
        for start_idx in start_crossings[:5]:  # Top 5 start points
            for end_idx in end_crossings[:5]:  # Top 5 end points
                if end_idx <= start_idx:
                    continue
                
                duration_samples = end_idx - start_idx
                duration = duration_samples / self.sr
                
                if duration < 0.5 or duration > 30.0:
                    continue
                
                # Calculate phase similarity at boundaries
                window = min(100, duration_samples // 10)
                if start_idx + window >= len(region) or end_idx - window < 0:
                    continue
                    
                phase_conf = self._calculate_phase_similarity(
                    region[start_idx:start_idx+window],
                    region[end_idx-window:end_idx]
                )
                
                candidates.append(LoopCandidate(
                    start=start_time + (start_idx / self.sr),
                    end=start_time + (end_idx / self.sr),
                    confidence=70 + int(phase_conf * 30),  # 70-100%
                    description=f"Zero-crossing optimized (phase: {int(phase_conf * 100)}%)"
                ))
        
        return candidates

    def _find_harmonic_loops(self, region, start_time):
        """Original harmonic similarity method."""
        try:
            import librosa
        except:
            return []
        
        candidates = []
        
        try:
            chroma = librosa.feature.chroma_cqt(y=region, sr=self.sr, hop_length=512)
            
            hop_length = 512
            frame_duration = hop_length / self.sr
            
            max_frames = chroma.shape[1] // 2
            min_frames = int(1.0 / frame_duration)
            
            for lag in range(min_frames, max_frames, 4):  # Step by 4 for speed
                n_compare = chroma.shape[1] - lag
                if n_compare <= 0:
                    continue

                chunk1 = chroma[:, 0:n_compare]
                chunk2 = chroma[:, lag:lag+n_compare]
                
                # Sample fewer frames for speed
                sample_frames = min(chunk1.shape[1], 50)
                similarity = np.mean([
                    np.dot(chunk1[:, j], chunk2[:, j]) / 
                    (np.linalg.norm(chunk1[:, j]) * np.linalg.norm(chunk2[:, j]) + 1e-8)
                    for j in range(sample_frames)
                ])
                
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

    def _find_zero_crossings(self, audio_segment):
        """Find indices where audio crosses zero."""
        crossings = []
        for i in range(1, len(audio_segment)):
            if (audio_segment[i-1] <= 0 and audio_segment[i] > 0) or \
               (audio_segment[i-1] >= 0 and audio_segment[i] < 0):
                crossings.append(i)
        return crossings

    def _calculate_phase_similarity(self, start_segment, end_segment):
        """Calculate how similar two segments are in phase/shape."""
        if len(start_segment) != len(end_segment) or len(start_segment) == 0:
            return 0.0
        
        # Normalize both segments
        start_max = np.max(np.abs(start_segment))
        end_max = np.max(np.abs(end_segment))
        
        if start_max < 1e-8 or end_max < 1e-8:
            return 0.0
            
        start_norm = start_segment / start_max
        end_norm = end_segment / end_max
        
        # Calculate correlation
        try:
            correlation = np.corrcoef(start_norm, end_norm)[0, 1]
            # Return similarity (0-1)
            return max(0.0, min(1.0, correlation))
        except:
            return 0.0

    def _merge_similar_candidates(self, candidates):
        """Merge candidates that are very close to each other."""
        if not candidates:
            return []
        
        merged = []
        candidates.sort(key=lambda x: x.start)
        
        current = candidates[0]
        
        for next_cand in candidates[1:]:
            # If starts are within 50ms, merge them
            if abs(next_cand.start - current.start) < 0.05:
                # Keep the one with higher confidence
                if next_cand.confidence > current.confidence:
                    current = next_cand
            else:
                merged.append(current)
                current = next_cand
        
        merged.append(current)
        return merged

    def _find_zero_crossing_loops_only(self, start_time, end_time):
        """Fallback method when librosa is not available."""
        start_sample = int(start_time * self.sr)
        end_sample = int(end_time * self.sr)
        region = self.mono_data[start_sample:end_sample]
        
        return self._find_zero_crossing_loops(region, start_time)

    def find_smart_cuts(self, start_time, end_time):
        """
        Analyze a selected region and propose beat-aligned cut points.
        
        Args:
            start_time: Rough start of the section to remove.
            end_time: Rough end of the section to remove.
        """
        candidates = []
        
        # fallback if librosa is missing
        try:
            import librosa
        except ImportError:
            # Just return the exact selection as a fallback
            return [CutCandidate(start_time, end_time, 100, "Exact selection (No Librosa)")]

        try:
            # Define a window around the selection to find beats
            # We look 1 second before and after to ensure we catch the nearest beat
            analysis_start = max(0, start_time - 2.0)
            analysis_end = min(len(self.mono_data) / self.sr, end_time + 2.0)
            
            start_sample = int(analysis_start * self.sr)
            end_sample = int(analysis_end * self.sr)
            
            region = self.mono_data[start_sample:end_sample]
            
            # 1. Detect Beats
            tempo, beat_frames = librosa.beat.beat_track(y=region, sr=self.sr)
            beat_times = librosa.frames_to_time(beat_frames, sr=self.sr) + analysis_start
            
            if len(beat_times) < 2:
                return [CutCandidate(start_time, end_time, 50, "No clear beats found")]

            # 2. Find nearest beat to START
            # minimal absolute difference between beat time and user start time
            start_beat_idx = (np.abs(beat_times - start_time)).argmin()
            snapped_start = beat_times[start_beat_idx]
            
            # 3. Find nearest beat to END
            end_beat_idx = (np.abs(beat_times - end_time)).argmin()
            snapped_end = beat_times[end_beat_idx]
            
            # Ensure we don't have inverted or zero-length cuts
            if snapped_end <= snapped_start:
                # If snapped points collapsed, try to enforce at least 1 beat duration
                if end_beat_idx < len(beat_times) - 1:
                    snapped_end = beat_times[start_beat_idx + 1]
                else:
                    snapped_end = start_time + 0.5 # Fallback

            # 4. Calculate Rhythm Consistency Score
            # (Does the cut maintain the 4/4 grid?)
            beats_skipped = end_beat_idx - start_beat_idx
            is_musical_bar = (beats_skipped % 4 == 0) or (beats_skipped % 3 == 0)
            
            confidence = 90 if is_musical_bar else 70
            desc = f"Beat Snap ({beats_skipped} beats removed)"
            
            candidates.append(CutCandidate(snapped_start, snapped_end, confidence, desc))
            
            # 5. Add a 'Fade' optimized candidate (Zero-crossing snap)
            # Find nearest zero crossing to user selection (ignoring beats)
            # (Reusing existing zero crossing logic if available, or simple logic here)
            candidates.append(CutCandidate(start_time, end_time, 60, "Raw Selection"))

        except Exception as e:
            logger.error(f"Smart cut detection failed: {e}")
            candidates.append(CutCandidate(start_time, end_time, 0, "Detection Failed"))
            
        return candidates