"""
Backend module for Loop Station.

Contains audio processing, loop detection, and state management.
These modules are UI-agnostic and can be used independently for testing.
"""

from .audio_engine import AudioEngine
from .state_manager import StateManager, PlaybackState, LoopRegion, Marker

__all__ = [
    'AudioEngine',
    'StateManager',
    'PlaybackState',
    'LoopRegion',
    'Marker',
]