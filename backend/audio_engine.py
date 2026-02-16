"""
Audio Engine for Loop Station.

Handles all audio operations with two distinct modes:

1. TRANSPORT MODE: Uses pygame.mixer.music for streaming playback.
   - Good for: playing whole song, scrubbing, finding loop points

2. LOOP MODE: Uses pygame.mixer.Sound with pre-sliced, crossfaded audio in RAM.
   - Good for: seamless, mathematically-perfect looping

The key insight is that pygame.mixer.Sound.play(loops=-1) handles looping
at the C/SDL layer, removing Python from the timing-critical path.

This module has NO UI dependencies and can be tested independently.
"""

import os
import sys
import time
import wave
import logging
import tempfile
import threading
import subprocess
import numpy as np

# Suppress console window on Windows for subprocess calls
_SUBPROCESS_FLAGS = {}
if os.name == 'nt':
    _SUBPROCESS_FLAGS['creationflags'] = subprocess.CREATE_NO_WINDOW

# Add parent directory to path for config import
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pygame
from config import (
    SAMPLE_RATE, CHANNELS, MIXER_BUFFER_SIZE,
    LOOP_CROSSFADE_MS, MIN_LOOP_DURATION,
    EXIT_PATCH_DURATION_MS, EXIT_PATCH_FADE_IN_MS, EXIT_PATCH_FADE_OUT_MS,
    TRANSPORT_RESUME_OFFSET_MS, FADE_EXIT_DURATION_MS,
)

logger = logging.getLogger("LoopStation.AudioEngine")


class AudioEngine:
    """
    Manages audio playback with seamless looping capabilities.
    
    This class is UI-agnostic and can be used independently for testing.
    
    Usage:
        engine = AudioEngine()
        engine.load_file("song.mp3")
        engine.set_loop_points(10.0, 20.0)  # Loop from 10s to 20s
        engine.play_transport(0)  # Start playing
        # ... later, when approaching loop end ...
        engine.start_loop_mode()  # Switch to seamless looping
        # ... when user wants to exit loop ...
        engine.execute_loop_exit()  # Exit smoothly back to transport
    """
    
    def __init__(self, ffmpeg_path="ffmpeg"):
        """
        Initialize the audio engine.
        
        Args:
            ffmpeg_path: Path to ffmpeg executable for audio conversion
        """
        self.ffmpeg_path = ffmpeg_path

        self.SAMPLE_RATE = SAMPLE_RATE
        
        # Initialize pygame mixer with low latency buffer
        pygame.mixer.init(
            frequency=SAMPLE_RATE,
            size=-16,
            channels=CHANNELS,
            buffer=MIXER_BUFFER_SIZE
        )
        
        # Audio data storage
        self.current_file_path = None
        self.raw_audio_data = None  # Full song as numpy array (int16, stereo)
        self.song_length = 0.0
        
        # Loop sound objects (pre-baked in RAM)
        self.loop_sound = None           # The seamless loop Sound object
        self.loop_channel = None         # Channel playing the loop
        self.exit_patch_sound = None     # Audio snippet for smooth exit
        
        # Loop parameters (in seconds)
        self.loop_in = 0.0
        self.loop_out = 0.0
        self.loop_duration = 0.0
        
        # State tracking
        self.mode = "transport"  # "transport" or "loop"
        self.is_playing = False
        self.is_paused = False
        self.transport_offset = 0.0  # Where transport playback started from
        
        # Loop playback timing
        self.loop_start_timestamp = 0.0  # time.time() when loop started
        
        # Thread safety - use a single RLock for all state
        self.lock = threading.RLock()
        
        # Generation state with versioning for thread safety
        self._generation_thread = None
        self._generation_lock = threading.Lock()
        self._loop_ready = False
        self._generation_id = 0  # Incremented each time we request generation
        
        logger.info("AudioEngine initialized")
    
    # =========================================================================
    # FILE LOADING
    # =========================================================================
    
    def load_file(self, path):
        """
        Load an audio file for playback.
        
        Args:
            path: Path to audio file
            
        Returns:
            Tuple of (song_duration, sync_ratio)
            sync_ratio is used to align waveform with audio if they differ slightly
        """
        self.cleanup()
        logger.info(f"=== LOADING FILE: {os.path.basename(path)} ===")
        self.stop()
        self.current_file_path = path
        self._loop_ready = False
        self.loop_sound = None
        self.exit_patch_sound = None
        
        # Load into pygame.mixer.music for transport mode
        pygame.mixer.music.load(path)
        logger.debug("Loaded into pygame.mixer.music (transport mode)")
        
        # Get duration using ffprobe (fast, no memory spike)
        self.song_length = self._get_duration_ffprobe(path)
        if self.song_length <= 0:
            # Fallback: load as Sound to get length (slow but reliable)
            logger.warning("ffprobe failed, falling back to pygame.mixer.Sound for duration")
            temp_sound = pygame.mixer.Sound(path)
            self.song_length = temp_sound.get_length()
            del temp_sound
        logger.info(f"Song duration: {self.song_length:.2f}s")
        
        # Load raw audio data into memory for slicing
        self._load_raw_audio(path)
        
        # Calculate sync ratio (for waveform alignment)
        sync_ratio = 1.0
        if self.raw_audio_data is not None:
            raw_duration = len(self.raw_audio_data) / SAMPLE_RATE
            if abs(raw_duration - self.song_length) > 0.1:
                sync_ratio = self.song_length / raw_duration
                logger.debug(f"Sync ratio: {sync_ratio:.4f}")
        
        return self.song_length, sync_ratio
    
    def _get_duration_ffprobe(self, path):
        """
        Get audio duration using ffprobe. Fast, no memory spike.
        
        Args:
            path: Path to audio file
            
        Returns:
            Duration in seconds, or 0.0 on failure
        """
        try:
            # Derive ffprobe path from ffmpeg path
            ffprobe_path = self.ffmpeg_path.replace("ffmpeg", "ffprobe")
            
            cmd = [
                ffprobe_path,
                '-v', 'quiet',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                path
            ]
            proc = subprocess.run(
                cmd, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE, 
                timeout=10,
               **_SUBPROCESS_FLAGS
            )
            
            if proc.returncode == 0 and proc.stdout.strip():
                duration = float(proc.stdout.strip())
                logger.debug(f"ffprobe duration: {duration:.3f}s")
                return duration
            else:
                logger.warning(f"ffprobe returned non-zero or empty output")
                return 0.0
                
        except FileNotFoundError:
            logger.warning("ffprobe not found, will use fallback")
            return 0.0
        except Exception as e:
            logger.warning(f"ffprobe error: {e}")
            return 0.0
    
    def _load_raw_audio(self, path):
        """
        Load audio data using memory mapping to prevent RAM spikes.
        Streams ffmpeg output to a temporary file, then maps it.
        """
        logger.debug("Loading raw audio data via memory map...")
        
        # Clean up previous temp file if it exists
        if hasattr(self, '_temp_audio_file') and self._temp_audio_file:
            try:
                self._temp_audio_file.close()
                os.unlink(self._temp_audio_path)
            except Exception as e:
                logger.warning(f"Failed to cleanup old temp file: {e}")
        
        self.raw_audio_data = None
        self._temp_audio_file = None
        self._temp_audio_path = None

        try:
            # 1. Create a temporary file on disk
            # delete=False is required so we can close it and re-open it with memmap
            self._temp_audio_file = tempfile.NamedTemporaryFile(suffix='.pcm', delete=False)
            self._temp_audio_path = self._temp_audio_file.name
            
            # 2. Stream ffmpeg output directly to this file
            cmd = [
                self.ffmpeg_path, '-i', path,
                '-f', 's16le', '-ar', str(SAMPLE_RATE), '-ac', str(CHANNELS),
                '-v', 'quiet', '-'
            ]
            
            # Use stdout=self._temp_audio_file to write directly to disk
            proc = subprocess.run(cmd, stdout=self._temp_audio_file, stderr=subprocess.PIPE, timeout=120, **_SUBPROCESS_FLAGS)
            
            # Flush and close the file handle so memmap can safely take over
            self._temp_audio_file.flush()
            self._temp_audio_file.close()
            
            if proc.returncode != 0:
                logger.error("FFmpeg failed to decode audio")
                return

            # 3. Calculate dimensions based on file size
            file_size = os.path.getsize(self._temp_audio_path)
            # 16-bit audio = 2 bytes per sample per channel
            total_samples = file_size // (2 * CHANNELS)
            
            if total_samples == 0:
                logger.warning("Decoded audio file is empty")
                return

            # 4. Create the Memory Map (This is the magic part)
            # It behaves like a numpy array but reads from disk on demand
            self.raw_audio_data = np.memmap(
                self._temp_audio_path, 
                dtype=np.int16, 
                mode='r', 
                shape=(total_samples, CHANNELS)
            )
            
            duration = total_samples / SAMPLE_RATE
            logger.info(f"Memory map created: {total_samples} samples ({duration:.2f}s) at {self._temp_audio_path}")
            
        except Exception as e:
            self.raw_audio_data = None
            logger.error(f"Error loading raw audio: {e}")

    def cleanup(self):
        """
        Permanently clean up resources and delete temporary files.
        Call this ONLY when the app is closing or loading a new song.
        """
        logger.info("Cleaning up AudioEngine resources...")
        
        # 1. Close the memory map to release the file handle
        if hasattr(self, 'raw_audio_data') and isinstance(self.raw_audio_data, np.memmap):
            try:
                # Force delete the reference so Python releases the file lock
                self.raw_audio_data._mmap.close()
                del self.raw_audio_data
                self.raw_audio_data = None
            except Exception as e:
                logger.warning(f"Error closing memmap: {e}")

        # 2. Delete the actual temp file from disk
        if hasattr(self, '_temp_audio_path') and self._temp_audio_path:
            if os.path.exists(self._temp_audio_path):
                try:
                    os.unlink(self._temp_audio_path)
                    logger.info(f"Deleted temp file: {self._temp_audio_path}")
                except Exception as e:
                    logger.warning(f"Could not delete temp file: {e}")
            self._temp_audio_path = None

    def get_raw_audio_data(self):
        """Get the raw audio data for waveform generation."""
        return self.raw_audio_data
    
    # =========================================================================
    # LOOP SOUND GENERATION
    # =========================================================================
    
    def set_loop_points(self, loop_in, loop_out, crossfade_ms=LOOP_CROSSFADE_MS):
        """
        Set loop in/out points and trigger background generation of loop sound.
        
        Args:
            loop_in: Start time in seconds
            loop_out: End time in seconds
        """
        duration = loop_out - loop_in
        logger.info(f"=== SETTING LOOP POINTS: IN={loop_in:.3f}s OUT={loop_out:.3f}s (duration={duration:.3f}s) ===")
        
        with self.lock:
            self.loop_in = loop_in
            self.loop_out = loop_out
            self.loop_duration = duration
            self._current_crossfade_ms = crossfade_ms
            self._loop_ready = False
            # Increment generation ID to invalidate any in-flight generation
            self._generation_id += 1
        
        # Trigger background generation
        self._generate_loop_sound_async()
    
    def _generate_loop_sound_async(self):
        """Generate the seamless loop sound in a background thread."""
        with self._generation_lock:
            if self._generation_thread is not None and self._generation_thread.is_alive():
                # A thread is already running. It will check generation_id and bail
                # if a newer request has come in. We still start a new one after it finishes.
                logger.debug("Loop generation already in progress, will be superseded by new ID")
            
            # Capture the current generation ID
            gen_id = self._generation_id
            logger.debug(f"Starting background loop generation thread (gen_id={gen_id})")
            self._generation_thread = threading.Thread(
                target=self._generate_loop_sound, 
                args=(gen_id,),
                daemon=True
            )
            self._generation_thread.start()
    
    def _generate_loop_sound(self, gen_id):
        """
        Create a seamless loop Sound object using numpy array slicing and crossfading.
        
        This is the core of the "Slice, Process, and Pre-load" approach:
        1. Extract the exact samples from loop_in to loop_out
        2. Apply a crossfade at the seam to eliminate clicks
        3. Convert to a pygame.mixer.Sound that can loop infinitely at the SDL layer
        
        Args:
            gen_id: The generation ID when this was requested. If it doesn't match
                    the current _generation_id, we bail out (a newer request superseded us).
        """
        logger.info(f">>> GENERATING SEAMLESS LOOP SOUND (gen_id={gen_id}) <<<")
        start_time = time.time()
        
        try:
            with self.lock:
                # Check if we've been superseded
                if gen_id != self._generation_id:
                    logger.debug(f"Generation {gen_id} superseded by {self._generation_id}, bailing")
                    return
                loop_in = self.loop_in
                loop_out = self.loop_out
                crossfade_ms = self._current_crossfade_ms
            
            if self.raw_audio_data is None or loop_out <= loop_in:
                logger.warning("Cannot generate loop: no raw audio or invalid loop points")
                return
            
            duration = loop_out - loop_in
            if duration < MIN_LOOP_DURATION:
                logger.warning(f"Loop too short ({duration:.3f}s), minimum is {MIN_LOOP_DURATION}s")
                return
            
            # Convert times to sample indices
            start_sample = int(loop_in * SAMPLE_RATE)
            end_sample = int(loop_out * SAMPLE_RATE)
            
            # Clamp to valid range
            start_sample = max(0, min(start_sample, len(self.raw_audio_data) - 1))
            end_sample = max(start_sample + 1, min(end_sample, len(self.raw_audio_data)))
            
            logger.debug(f"Slicing samples {start_sample} to {end_sample} ({end_sample - start_sample} samples)")
            
            # Calculate crossfade samples
            crossfade_samples = int((crossfade_ms / 1000.0) * SAMPLE_RATE)
            logger.debug(f"Crossfade: {LOOP_CROSSFADE_MS}ms = {crossfade_samples} samples")
            
            # Extract the loop region
            loop_audio = self.raw_audio_data[start_sample:end_sample].copy().astype(np.float32)
            
            if len(loop_audio) < crossfade_samples * 2:
                logger.warning("Loop too short for crossfade, using raw audio")
                loop_audio_int = np.clip(loop_audio, -32768, 32767).astype(np.int16)
            else:
                # Apply crossfade to create seamless loop
                logger.debug("Applying crossfade for seamless loop...")
                
                max_crossfade = len(loop_audio) // 3
                crossfade_samples = min(crossfade_samples, max_crossfade)

                # Get the beginning portion (what we'll fade INTO)
                beginning = loop_audio[:crossfade_samples].copy()
                
                # Create fade curves
                fade_out = np.linspace(1.0, 0.0, crossfade_samples).reshape(-1, 1)
                fade_in = np.linspace(0.0, 1.0, crossfade_samples).reshape(-1, 1)
                
                # Apply crossfade: fade out the end, fade in the beginning, add them
                loop_audio[-crossfade_samples:] = (
                    loop_audio[-crossfade_samples:] * fade_out +
                    beginning * fade_in
                )
                
                # Convert back to int16
                loop_audio_int = np.clip(loop_audio, -32768, 32767).astype(np.int16)
            
            # Check again if superseded before doing I/O
            with self.lock:
                if gen_id != self._generation_id:
                    logger.debug(f"Generation {gen_id} superseded after processing, bailing")
                    return
            
            # Create pygame.mixer.Sound directly from buffer (no disk I/O)
            try:
                # pygame.mixer.Sound(buffer=...) expects raw PCM in a bytes object
                # but it needs to be wrapped in a WAV-like sndarray or use the wav approach
                # safest cross-platform: use pygame.sndarray
                import io
                
                wav_buffer = io.BytesIO()
                with wave.open(wav_buffer, 'wb') as wf:
                    wf.setnchannels(CHANNELS)
                    wf.setsampwidth(2)  # 16-bit = 2 bytes
                    wf.setframerate(SAMPLE_RATE)
                    wf.writeframes(loop_audio_int.tobytes())
                
                wav_buffer.seek(0)
                new_loop_sound = pygame.mixer.Sound(file=wav_buffer)
                logger.debug("Created pygame.mixer.Sound from in-memory buffer (no disk I/O)")
                
            except Exception as loop_sound_err:
                logger.error(f"Error creating loop sound from buffer: {loop_sound_err}")
                return
            
            # Generate exit patch
            logger.debug("Generating exit patch...")
            exit_patch = self._generate_exit_patch(end_sample)
            
            # Final check and atomic store
            with self.lock:
                if gen_id != self._generation_id:
                    logger.debug(f"Generation {gen_id} superseded at final store, discarding")
                    return
                    
                self.loop_sound = new_loop_sound
                self.exit_patch_sound = exit_patch
                self._loop_ready = True
            
            elapsed = (time.time() - start_time) * 1000
            logger.info(f">>> LOOP SOUND READY ({elapsed:.1f}ms) - Duration: {duration:.3f}s (gen_id={gen_id}) <<<")
            
        except Exception as e:
            logger.error(f"Error generating loop sound: {e}")
            import traceback
            traceback.print_exc()
    
    def _generate_exit_patch(self, end_sample):
        """
        Generate a SHORT audio snippet starting at the loop end point.
        This bridges the gap while pygame.mixer.music buffers.
        """
        try:
            if self.raw_audio_data is None:
                logger.warning("No raw audio data for exit patch")
                return None
            
            # Extract audio for exit patch
            patch_duration_samples = int((EXIT_PATCH_DURATION_MS / 1000.0) * SAMPLE_RATE)
            patch_end = min(end_sample + patch_duration_samples, len(self.raw_audio_data))
            
            if patch_end <= end_sample:
                logger.warning("Not enough audio after loop end for exit patch")
                return None
            
            patch_audio = self.raw_audio_data[end_sample:patch_end].copy().astype(np.float32)
            
            # Apply fade-in at start
            fade_in_samples = min(int((EXIT_PATCH_FADE_IN_MS / 1000.0) * SAMPLE_RATE), len(patch_audio))
            if fade_in_samples > 0:
                fade_in = np.linspace(0.0, 1.0, fade_in_samples).reshape(-1, 1)
                patch_audio[:fade_in_samples] *= fade_in
            
            # Apply fade-out at end
            fade_out_samples = min(int((EXIT_PATCH_FADE_OUT_MS / 1000.0) * SAMPLE_RATE), len(patch_audio))
            if fade_out_samples > 0:
                fade_out = np.linspace(1.0, 0.0, fade_out_samples).reshape(-1, 1)
                patch_audio[-fade_out_samples:] *= fade_out
            
            # Convert back to int16
            patch_audio = np.clip(patch_audio, -32768, 32767).astype(np.int16)
            
            # Save to temp file and create Sound
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as temp_file:
                    temp_path = temp_file.name
                
                with wave.open(temp_path, 'wb') as wf:
                    wf.setnchannels(CHANNELS)
                    wf.setsampwidth(2)
                    wf.setframerate(SAMPLE_RATE)
                    wf.writeframes(patch_audio.tobytes())
                
                patch_sound = pygame.mixer.Sound(temp_path)
                patch_ms = (patch_end - end_sample) / SAMPLE_RATE * 1000
                logger.debug(f"Exit patch created: {patch_ms:.0f}ms bridge ({EXIT_PATCH_FADE_OUT_MS}ms fade-out)")
                return patch_sound
                
            finally:
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.unlink(temp_path)
                    except:
                        pass
                        
        except Exception as e:
            logger.error(f"Error generating exit patch: {e}")
            return None
    
    def is_loop_ready(self):
        """Check if the seamless loop sound has been generated."""
        with self.lock:
            return self._loop_ready and self.loop_sound is not None
    
    # =========================================================================
    # TRANSPORT MODE CONTROLS
    # =========================================================================
    
    def play_transport(self, start_pos=None):
        """
        Start or resume playback in transport mode (streaming).
        
        Args:
            start_pos: Position to start from (seconds), or None to continue
        """
        if start_pos is not None:
            self.transport_offset = start_pos
        
        # Stop any loop playback first
        self._stop_loop_channel()
        
        logger.info(f"[PLAY] TRANSPORT PLAY from {self.transport_offset:.3f}s")
        pygame.mixer.music.play(start=self.transport_offset)
        self.mode = "transport"
        self.is_playing = True
        self.is_paused = False
    
    def pause_transport(self):
        """Pause transport playback."""
        if self.mode == "transport" and self.is_playing:
            self.transport_offset = self.get_position()
            pygame.mixer.music.pause()
            self.is_paused = True
            logger.info(f"[PAUSE] TRANSPORT PAUSED at {self.transport_offset:.3f}s")

    def unpause_transport(self):
        """Resume transport playback."""
        if self.mode == "transport" and self.is_paused:
            # FIX: Use play() instead of unpause(). 
            # unpause() on macOS often fails to reset the internal get_pos() timer,
            # causing the display to "double count" the time (Offset + Old Time).
            # play() forces a clean restart from the specific timestamp.
            pygame.mixer.music.play(start=self.transport_offset)
            
            self.is_paused = False
            logger.info(f"[PLAY] TRANSPORT RESUMED from {self.transport_offset:.3f}s")
    
    def seek_transport(self, position):
        """
        Seek to a position in transport mode.
        
        Args:
            position: Time in seconds
        """
        position = max(0, min(position, self.song_length - 0.01))
        logger.debug(f"⏩ TRANSPORT SEEK to {position:.3f}s")
        self.transport_offset = position
        
        # If we're in loop mode, exit it
        if self.mode == "loop":
            logger.info("Exiting loop mode due to seek")
            self._stop_loop_channel()
            self.mode = "transport"
        
        if self.is_playing and not self.is_paused:
            pygame.mixer.music.play(start=position)
            logger.info(f"[PLAY] TRANSPORT PLAY from {position:.3f}s")
        else:
            pygame.mixer.music.play(start=position)
            pygame.mixer.music.pause()
            self.is_paused = True
    
    def get_transport_position(self):
        """Get current position in transport mode."""
        if self.mode != "transport":
            return self.transport_offset
        
        ms = pygame.mixer.music.get_pos()
        if ms >= 0:
            return self.transport_offset + (ms / 1000.0)
        return self.transport_offset
    
    # =========================================================================
    # LOOP MODE CONTROLS
    # =========================================================================
    
    def start_loop_mode(self, fade_in_ms=15):
        """
        Switch to loop mode with position synchronization.
        """
        with self.lock:
            if not self._loop_ready or self.loop_sound is None:
                logger.warning("Cannot start loop mode: loop sound not ready")
                return False
            sound_to_play = self.loop_sound
            loop_duration = self.loop_duration
            loop_in = self.loop_in
        
        # Get EXACT current position
        current_pos = self.get_position()
        
        # Calculate where we are in the loop cycle
        # This tells us how far into the loop we should be
        cycle_offset = (current_pos - loop_in) % loop_duration
        
        logger.info(f"[LOOP] Entering loop at pos={current_pos:.3f}s, cycle_offset={cycle_offset:.3f}s")
        
        # CRITICAL: Stop transport with NO fadeout
        pygame.mixer.music.stop()
        
        # Start loop sound from beginning
        with self.lock:
            self.loop_channel = sound_to_play.play(loops=-1, fade_ms=int(fade_in_ms))
            
            # SYNC FIX: Set timestamp as if we started earlier
            # This makes get_loop_cycle_position() return the correct offset
            self.loop_start_timestamp = time.time() - cycle_offset
            
            self.mode = "loop"
            self.is_playing = True
            self.is_paused = False
        
        logger.info(f"[LOOP] Timestamp synced: appears to be {cycle_offset:.3f}s into cycle")
        return True

    def pause_loop(self):
        """Pause loop playback."""
        if self.mode == "loop" and self.loop_channel:
            self.loop_channel.pause()
            self.is_paused = True
            logger.info("[PAUSE] LOOP PAUSED")
    
    def unpause_loop(self):
        """Resume loop playback."""
        if self.mode == "loop" and self.loop_channel:
            self.loop_channel.unpause()
            self.is_paused = False
            logger.info("[PLAY] LOOP RESUMED")
    
    def _stop_loop_channel(self):
        """Stop the loop sound channel."""
        if self.loop_channel:
            logger.debug("Stopping loop channel")
            self.loop_channel.fadeout(30)
            self.loop_channel = None
        self.mode = "transport"
    
    def get_loop_cycle_position(self):
        """
        Get the current position within the loop cycle.
        Returns a value between 0 and loop_duration.
        """
        if self.mode != "loop" or self.loop_duration <= 0:
            return 0.0
        
        elapsed = time.time() - self.loop_start_timestamp
        return elapsed % self.loop_duration
    
    def execute_loop_exit(self):
        """
        Actually perform the loop exit. Should be called at the loop boundary.
        
        Strategy: Use a SHORT exit patch as a bridge while transport buffers.
        - Exit patch plays instantly (RAM-based, no latency)
        - Patch fades out over last portion
        - Transport starts slightly before patch ends for overlap
        """
        logger.info(f"[EXIT] === EXECUTING LOOP EXIT at boundary ===")
        
        with self.lock:
            loop_out = self.loop_out
        
        # 1. Stop the loop immediately
        if self.loop_channel:
            logger.debug("Stopping loop channel")
            self.loop_channel.stop()
            self.loop_channel = None
        
        # 2. Play exit patch (bridges the gap while transport buffers)
        if self.exit_patch_sound:
            logger.debug(f"Playing exit patch ({EXIT_PATCH_DURATION_MS}ms bridge, fades out)")
            self.exit_patch_sound.play()
        
        # 3. Start transport (overlaps with patch fade-out)
        resume_point = loop_out + (TRANSPORT_RESUME_OFFSET_MS / 1000.0)
        if resume_point >= self.song_length:
            resume_point = loop_out
        
        logger.info(f"[PLAY] Starting transport at {resume_point:.3f}s")
        self.transport_offset = resume_point
        pygame.mixer.music.play(start=resume_point)
        
        self.mode = "transport"
        self.is_playing = True
        self.is_paused = False
    
    def execute_fade_exit(self, fade_ms=None):
        """
        Exit loop mode by fading out the loop sound, then stopping.
        Used for theater vamping where you want the music to fade away
        rather than cutting to transport.
        
        Args:
            fade_ms: Fade duration in milliseconds (uses config default if None)
        """
        if fade_ms is None:
            fade_ms = FADE_EXIT_DURATION_MS
        
        logger.info(f"[FADE-EXIT] === EXECUTING FADE EXIT ({fade_ms}ms) ===")
        
        if self.loop_channel:
            self.loop_channel.fadeout(int(fade_ms))
            # Don't set loop_channel to None yet - let the fadeout complete
        
        # Don't start transport - just let it fade to silence
        self.mode = "transport"
        self.is_playing = False
        self.is_paused = False
    
    # =========================================================================
    # COMMON CONTROLS
    # =========================================================================
    
    def get_position(self):
        """Get current playback position regardless of mode."""
        if self.mode == "loop":
            return self.loop_in + self.get_loop_cycle_position()
        else:
            return self.get_transport_position()
    
    def toggle_play_pause(self):
        """Toggle between play and pause states."""
        if self.is_playing and not self.is_paused:
            if self.mode == "loop":
                self.pause_loop()
            else:
                self.pause_transport()
        else:
            if self.mode == "loop" and self.loop_channel:
                self.unpause_loop()
            else:
                if self.is_paused:
                    self.unpause_transport()
                else:
                    self.play_transport()
    
    def stop(self):
        """Stop all playback and reset state."""
        logger.info("[STOP] STOP - Stopping all playback")
        self._stop_loop_channel()
        pygame.mixer.music.stop()
        self.is_playing = False
        self.is_paused = False
        self.transport_offset = 0.0
        self.mode = "transport"
    
    def is_transport_active(self):
        """Check if transport (streaming) is actively playing."""
        return self.mode == "transport" and pygame.mixer.music.get_busy()
    
    def is_loop_active(self):
        """Check if loop mode is active."""
        return self.mode == "loop" and self.loop_channel and self.loop_channel.get_busy()

    def perform_skip(self, target_pos, fade_out_ms=0, fade_in_ms=0):
        """
        Execute a skip jump in the transport.
        
        Args:
            target_pos: Where to jump TO (seconds)
            fade_out_ms: Duration to fade out BEFORE the jump (requires threading usually, 
                         so here we might just do a volume dip if supported, or direct seek)
            fade_in_ms: Not fully supported by pygame.mixer.music without stop/start, 
                        but we can simulate by seeking.
        """
        if self.mode != "transport":
            return

        logger.info(f"[SKIP] Jumping to {target_pos:.3f}s")
        
        # Pygame music seeking is blocking and might click. 
        # Ideally, we would lower volume -> seek -> raise volume.
        
        # 1. Simple Seek (Fastest, best for beat-matching)
        if fade_out_ms == 0:
            self.seek_transport(target_pos)
        else:
            # 2. Fade Seek (Simulated)
            # Note: Pygame mixer music fadeout STOPS playback. We don't want that.
            # We will just seek immediately for now. 
            # A true crossfaded skip requires the "Slice and Process" Loop Mode architecture, 
            # which is too heavy for random skips.
            self.seek_transport(target_pos)

