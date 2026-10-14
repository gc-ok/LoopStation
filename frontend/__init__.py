"""
Frontend module for Loop Station.

Contains all UI components built with CustomTkinter.
Each component is a separate class for easy modification and testing.

``LoopStationApp`` is imported lazily so that ``from frontend import
__version__`` does not pull in matplotlib/pygame.
"""

__version__ = "1.0.0"

__all__ = ['LoopStationApp', '__version__']


def __getattr__(name):
    if name == 'LoopStationApp':
        from .app import LoopStationApp
        return LoopStationApp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
