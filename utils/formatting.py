"""
Formatting utilities for Loop Station.
"""

import math
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
    if not math.isfinite(seconds) or seconds < 0:
        seconds = 0
    
    if include_ms:
        # Round to centiseconds first so 59.996 reads "1:00.00", not "0:60.00"
        centis = int(round(seconds * 100))
        minutes, centis = divmod(centis, 6000)
        return f"{minutes}:{centis / 100:05.2f}"
    else:
        minutes = int(seconds // 60)
        return f"{minutes}:{int(seconds % 60):02d}"


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
        Time in seconds, or None if parsing failed (also for negative
        parts and for "nan" / "inf")
    """
    text = text.strip()
    
    try:
        if ':' in text:
            parts = text.split(':')
            if any(part.strip().startswith('-') for part in parts):
                return None   # "-0:05", "1:-5"
            if len(parts) == 2:
                minutes = int(parts[0])
                seconds = float(parts[1])
                if minutes < 0:
                    return None
                result = minutes * 60 + seconds
            elif len(parts) == 3:
                hours = int(parts[0])
                minutes = int(parts[1])
                seconds = float(parts[2])
                if hours < 0 or minutes < 0:
                    return None
                result = hours * 3600 + minutes * 60 + seconds
            else:
                return None
        else:
            seconds = result = float(text)
    except (ValueError, IndexError, AttributeError):
        return None
    if not math.isfinite(seconds) or seconds < 0 or not math.isfinite(result):
        return None
    return result


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