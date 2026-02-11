"""
Formatting utilities for Loop Station.
"""

from typing import Optional


def format_time(seconds: float, include_ms: bool = True) -> str:
    """
    Format seconds as a time string.
    
    Args:
        seconds: Time in seconds
        include_ms: Whether to include milliseconds
        
    Returns:
        Formatted string like "1:23.45" or "1:23"
    """
    if seconds < 0:
        seconds = 0
    
    minutes = int(seconds // 60)
    secs = seconds % 60
    
    if include_ms:
        return f"{minutes}:{secs:05.2f}"
    else:
        return f"{minutes}:{int(secs):02d}"


def parse_time(text: str) -> Optional[float]:
    """
    Parse a time string to seconds.
    
    Accepts formats:
    - "1:23.45" (M:SS.ms)
    - "1:23" (M:SS)
    - "83.45" (just seconds)
    - "83" (just seconds, integer)
    
    Args:
        text: Time string to parse
        
    Returns:
        Time in seconds, or None if parsing failed
    """
    text = text.strip()
    
    try:
        if ':' in text:
            parts = text.split(':')
            if len(parts) == 2:
                minutes = int(parts[0])
                seconds = float(parts[1])
                return minutes * 60 + seconds
            elif len(parts) == 3:
                hours = int(parts[0])
                minutes = int(parts[1])
                seconds = float(parts[2])
                return hours * 3600 + minutes * 60 + seconds
        else:
            return float(text)
    except (ValueError, IndexError):
        return None


def format_duration(seconds: float) -> str:
    """
    Format a duration for display.
    
    Args:
        seconds: Duration in seconds
        
    Returns:
        Formatted string like "2.980s" or "1m 23.4s"
    """
    if seconds < 60:
        return f"{seconds:.3f}s"
    else:
        minutes = int(seconds // 60)
        secs = seconds % 60
        return f"{minutes}m {secs:.1f}s"