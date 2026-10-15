"""
Compatibility alias: the folder song library became the Show sidebar
(running order / setlist), see ``frontend/show_sidebar.py``.
"""

from .show_sidebar import ShowSidebar

LibrarySidebar = ShowSidebar

__all__ = ["ShowSidebar", "LibrarySidebar"]
