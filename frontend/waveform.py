"""
Waveform Display Widget for Loop Station.

PERFORMANCE: The playhead is drawn on the Tk canvas that backs matplotlib,
NOT via matplotlib redraw. This gives ~60fps playhead updates.

COORDINATE FIX: Uses matplotlib's ax.transData to convert between data
coordinates (0-1) and display pixels. This properly accounts for the fact
that the tk canvas widget may be larger than the matplotlib figure.
"""

import logging
import numpy as np
import tkinter as tk
from typing import Optional, Callable, List

import matplotlib
matplotlib.use('TkAgg')
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import (
    COLOR_WAVEFORM, COLOR_WAVEFORM_BG, COLOR_PLAYHEAD,
    COLOR_LOOP_IN, COLOR_LOOP_OUT, COLOR_LOOP_REGION,
    COLOR_LOOP_GHOST, COLOR_LOOP_GHOST_ALPHA,
    COLOR_MARKER, COLOR_TEXT_DIM,
    WAVEFORM_TARGET_SAMPLES, WAVEFORM_HEIGHT, COLOR_SKIP_REGION
)

logger = logging.getLogger("LoopStation.Waveform")


class WaveformWidget:
    """
    Waveform display with overlay playhead on the matplotlib tk canvas.
    """
    
    def __init__(self, parent: tk.Widget, on_seek: Optional[Callable[[float], None]] = None):
        self.parent = parent
        self.on_seek = on_seek
        
        # State
        self.duration = 0.0
        self.waveform_data: Optional[np.ndarray] = None

        self.view_start = 0.0  # 0.0 = start of song
        self.view_end = 1.0    # 1.0 = end of song
        self.min_zoom_window = 0.05 # Max zoom in (5% of song)
        
        self.loop_in = 0.0
        self.loop_out = 0.0
        
        # Multi-loop and marker storage
        self.loops = []
        self.skips = []
        self.selected_index = -1
        self.markers = []
        
        # Container frame with fixed height
        self.container = tk.Frame(parent, bg=COLOR_WAVEFORM_BG, height=WAVEFORM_HEIGHT)
        self.container.pack_propagate(False)  # Enforce fixed height
        
        # Matplotlib figure
        # figsize width is arbitrary since we pack fill=x, but height matters
        # We'll compute inches from WAVEFORM_HEIGHT at 100 dpi
        fig_height_inches = WAVEFORM_HEIGHT / 100.0
        self.figure = Figure(figsize=(10, fig_height_inches), dpi=100, facecolor=COLOR_WAVEFORM_BG)
        self.ax = self.figure.add_subplot(111)
        self._setup_axes()
        
        # Matplotlib canvas
        self.canvas = FigureCanvasTkAgg(self.figure, master=self.container)
        self.canvas_widget = self.canvas.get_tk_widget()
        self.canvas_widget.configure(bg=COLOR_WAVEFORM_BG, highlightthickness=0)
        self.canvas_widget.pack(fill="both", expand=True)
        
        # The tk widget backing matplotlib - we draw overlay items here
        self._tk_canvas = self.canvas_widget
        
        # Overlay item IDs
        self._playhead_line_id = None
        self._marker_ids = []
        self._playhead_frac = 0.0
        
        # Loop Detection Attributes
        self.selection_rect = None
        self.selection_start = None
        self.selection_end = None
        self.is_selecting = False
        self.selection_mode_active = False
        self.on_selection_change = None
        
        # Bind matplotlib events for click handling
        self.canvas.mpl_connect('button_press_event', self._on_click)
        self.canvas.mpl_connect('button_release_event', self._on_release)
        self.canvas.mpl_connect('motion_notify_event', self._on_drag)
        
        # After matplotlib draws, redraw our overlay items on top
        self.canvas.mpl_connect('draw_event', self._on_mpl_draw)
        self.canvas.mpl_connect('scroll_event', self._on_scroll)
        # Resize: need to resize the figure to match the widget
        self.container.bind("<Configure>", self._on_configure)
        
        logger.debug("WaveformWidget initialized")
    
    def _on_mpl_draw(self, event):
        """Called after matplotlib redraws. Redraw overlay items on top."""
        self._draw_overlay_playhead()
        self._draw_overlay_markers()
    
    # frontend/waveform.py

    def _on_configure(self, event):
        """
        Handle widget resize. Resize the matplotlib figure to exactly
        match the container size.
        """
        w = event.width
        h = event.height
        
        # Basic sanity check
        if w <= 1 or h <= 1:
            return
        
        dpi = self.figure.get_dpi()
        # Update the figure size to match the container's pixel dimensions
        self.figure.set_size_inches(w / dpi, h / dpi, forward=False)
        
        # Re-enforce layout and redraw
        self.figure.subplots_adjust(left=0, right=1, top=1, bottom=0)
        self.canvas.draw_idle()

    def _setup_axes(self):
        """Configure the matplotlib axes to fill the entire figure."""
        self.ax.set_facecolor(COLOR_WAVEFORM_BG)
        self.ax.set_xlim(self.view_start, self.view_end)
        self.ax.set_ylim(-1, 1)
        self.ax.axis('off')
        self.figure.subplots_adjust(left=0, right=1, top=1, bottom=0)

    def _on_scroll(self, event):
        """Handle mouse wheel for zooming."""
        if event.inaxes != self.ax:
            return
            
        # Get mouse position in data coordinates (0.0 - 1.0 relative to song)
        mouse_x = event.xdata
        if mouse_x is None: 
            return

        base_scale = 1.1
        if event.button == 'up':
            # Zoom In
            scale_factor = 1 / base_scale
        else:
            # Zoom Out
            scale_factor = base_scale

        # Current window width
        cur_width = self.view_end - self.view_start
        new_width = cur_width * scale_factor
        
        # Limit zoom
        if new_width < self.min_zoom_window:
            new_width = self.min_zoom_window
        if new_width > 1.0:
            new_width = 1.0
            
        # Calculate new bounds, keeping mouse position stationary
        # Formula: new_start = mouse_x - (mouse_x - old_start) * (new_width / old_width)
        ratio = (mouse_x - self.view_start) / cur_width
        self.view_start = mouse_x - (new_width * ratio)
        self.view_end = self.view_start + new_width
        
        # Clamp to 0-1
        if self.view_start < 0:
            self.view_start = 0
            self.view_end = new_width
        if self.view_end > 1:
            self.view_end = 1
            self.view_start = 1 - new_width
            
        # Trigger redraw
        self._draw_waveform()
    
    def get_widget(self) -> tk.Widget:
        return self.container
    
    def pack(self, **kwargs):
        self.container.pack(**kwargs)
    
    def grid(self, **kwargs):
        self.container.grid(**kwargs)
    
    # =========================================================================
    # COORDINATE HELPERS
    # =========================================================================
    
    def _frac_to_pixel_x(self, frac):
        """
        Convert 0-1 data fraction to pixel X on the tk canvas,
        accounting for the current Zoom Viewport.
        """
        canvas_width = self._tk_canvas.winfo_width()
        if canvas_width <= 1:
            return 0
            
        # 1. Check if the point is visible
        if frac < self.view_start or frac > self.view_end:
            # Return off-screen coordinates
            if frac < self.view_start: return -10
            if frac > self.view_end: return canvas_width + 10

        # 2. Normalize fraction to the current viewport
        view_width = self.view_end - self.view_start
        rel_frac = (frac - self.view_start) / view_width
        
        return rel_frac * canvas_width
    
    # =========================================================================
    # OVERLAY DRAWING (playhead + markers on tk canvas)
    # =========================================================================
    
    def _draw_overlay_playhead(self):
        """Draw playhead line on the tk canvas (fast, no matplotlib redraw)."""
        if self._playhead_line_id is not None:
            self._tk_canvas.delete(self._playhead_line_id)
            self._playhead_line_id = None
        
        canvas_height = self._tk_canvas.winfo_height()
        if canvas_height <= 1:
            return
        
        x = self._frac_to_pixel_x(self._playhead_frac)
        
        self._playhead_line_id = self._tk_canvas.create_line(
            x, 0, x, canvas_height,
            fill=COLOR_PLAYHEAD, width=2, tags="overlay"
        )
    
    def _draw_overlay_markers(self):
        """Draw marker lines and name labels on the tk canvas."""
        for item_id in self._marker_ids:
            self._tk_canvas.delete(item_id)
        self._marker_ids.clear()
        
        if not self.markers or self.duration <= 0:
            return
        
        canvas_height = self._tk_canvas.winfo_height()
        if canvas_height <= 1:
            return
        
        for marker in self.markers:
            frac = marker.time / self.duration
            if frac < 0 or frac > 1:
                continue
            
            x = self._frac_to_pixel_x(frac)
            color = marker.color or COLOR_MARKER
            
            line_id = self._tk_canvas.create_line(
                x, 0, x, canvas_height,
                fill=color, width=1, dash=(4, 4), tags="overlay"
            )
            self._marker_ids.append(line_id)
            
            display_name = marker.name[:12] + "…" if len(marker.name) > 12 else marker.name
            text_id = self._tk_canvas.create_text(
                x + 3, 10,
                text=display_name,
                fill=color, anchor="nw",
                font=("Segoe UI", 8),
                tags="overlay"
            )
            self._marker_ids.append(text_id)
    
    # =========================================================================
    # WAVEFORM GENERATION
    # =========================================================================
    
    def load_waveform(self, audio_data: np.ndarray, duration: float):
        """Load and display waveform from audio data."""
        self.duration = duration
        
        if audio_data is None or len(audio_data) == 0:
            logger.warning("No audio data for waveform")
            return
        
        logger.debug(f"Generating waveform from {len(audio_data)} samples")
        
        if len(audio_data.shape) > 1 and audio_data.shape[1] > 1:
            mono = audio_data.mean(axis=1)
        else:
            mono = audio_data.flatten()
        
        target_samples = WAVEFORM_TARGET_SAMPLES
        if len(mono) > target_samples:
            chunk_size = len(mono) // target_samples
            chunks = mono[:chunk_size * target_samples].reshape(-1, chunk_size)
            self.waveform_data = np.max(np.abs(chunks), axis=1)
        else:
            self.waveform_data = np.abs(mono)
        
        max_val = np.max(self.waveform_data)
        if max_val > 0:
            self.waveform_data = self.waveform_data / max_val
        
        self._draw_waveform()
        logger.debug("Waveform generated")
    
    def update_skips_display(self, skips):
        """Update stored skips and redraw."""
        self.skips = skips
        self._draw_waveform()

    def _draw_waveform(self):
        """Draw the waveform on the axes."""
        if self.waveform_data is None:
            return
        
        self.ax.clear()
        self._setup_axes() # This now applies set_xlim(view_start, view_end)
        
        # We plot the WHOLE waveform data (0 to 1), but set_xlim clips it visibly.
        # This is fast enough for matplotlib if data isn't huge.
        x = np.linspace(0, 1, len(self.waveform_data))
        
        self.ax.fill_between(x, -self.waveform_data, self.waveform_data, 
                             color=COLOR_WAVEFORM, alpha=0.8)
        
        # Draw regions (Loops, Skips)
        self._draw_skip_regions() 
        self._draw_loop_regions()
        
        if self.selection_start is not None and self.selection_end is not None:
            self._redraw_selection_patch()
        
        self.canvas.draw_idle()
    
    def _draw_skip_regions(self):
        """Draw skip regions (Cuts) in the theme color."""
        if self.duration <= 0 or not self.skips:
            return

        for skip in self.skips:
            if not skip.active:
                continue

            start_frac = skip.start / self.duration
            end_frac = skip.end / self.duration
            
            # Draw hatched box using theme color
            self.ax.axvspan(start_frac, end_frac, 
                          color=COLOR_SKIP_REGION, alpha=0.3, zorder=1.5, hatch='//')
            
            # Label
            mid_frac = (start_frac + end_frac) / 2
            self.ax.text(
                mid_frac, -0.85, "✂ CUT",
                ha='center', va='bottom',
                fontsize=7, color=COLOR_SKIP_REGION, # Use theme color for text too
                zorder=5, fontfamily='sans-serif'
            )

    def update_loops_display(self, loops, selected_index):
        """Update stored loops and redraw."""
        self.loops = loops
        self.selected_index = selected_index
        self._draw_waveform()

    def update_markers_display(self, markers):
        """Update stored markers and redraw overlay."""
        self.markers = markers
        self._draw_overlay_markers()

    def _draw_loop_regions(self):
        """Draw all loop regions. Selected = bright, others = ghost."""
        if self.duration <= 0:
            return

        for i, loop in enumerate(self.loops):
            start_frac = loop.start / self.duration
            end_frac = loop.end / self.duration
            
            if end_frac <= start_frac:
                continue

            if i == self.selected_index:
                color = COLOR_LOOP_REGION
                alpha = 0.4
                zorder = 2
            else:
                color = COLOR_LOOP_GHOST
                alpha = COLOR_LOOP_GHOST_ALPHA
                zorder = 1
                
            self.ax.axvspan(start_frac, end_frac, color=color, alpha=alpha, zorder=zorder)
            
            if i == self.selected_index:
                self.ax.axvline(start_frac, color=COLOR_LOOP_IN, linewidth=2, zorder=3)
                self.ax.axvline(end_frac, color=COLOR_LOOP_OUT, linewidth=2, zorder=3)
            else:
                self.ax.axvline(start_frac, color=COLOR_LOOP_GHOST, linewidth=1, alpha=0.3, zorder=1)
                self.ax.axvline(end_frac, color=COLOR_LOOP_GHOST, linewidth=1, alpha=0.3, zorder=1)
            
            mid_frac = (start_frac + end_frac) / 2
            label_color = "#aaffaa" if i == self.selected_index else "#666666"
            display_name = loop.name[:15] + "…" if len(loop.name) > 15 else loop.name
            self.ax.text(
                mid_frac, 0.85, display_name,
                ha='center', va='top',
                fontsize=7, color=label_color,
                alpha=0.8 if i == self.selected_index else 0.5,
                zorder=5, fontfamily='sans-serif'
            )
    
    # =========================================================================
    # UPDATES
    # =========================================================================
    
    def update_playhead(self, position: float):
        """Update playhead position. Only moves a tk canvas line."""
        if self.duration <= 0:
            return
        self._playhead_frac = max(0, min(1, position / self.duration))
        self._draw_overlay_playhead()
    
    def update_loop_markers(self, loop_in: float, loop_out: float):
        """Update loop in/out markers (triggers full matplotlib redraw)."""
        self.loop_in = loop_in
        self.loop_out = loop_out
        if self.duration <= 0:
            return
        self._draw_waveform()
    
    def clear(self):
        """Clear the waveform display."""
        self.waveform_data = None
        self.duration = 0.0
        self.loops = []
        self.markers = []
        self.selected_index = -1
        self.ax.clear()
        self._setup_axes()
        self.canvas.draw_idle()
    
    # =========================================================================
    # INTERACTION
    # =========================================================================
    
    def _on_click(self, event):
        """Handle matplotlib click events."""
        if event.inaxes != self.ax:
            return
        if event.xdata is None:
            return

        if self.selection_mode_active:
            self.is_selecting = True
            self.selection_start = event.xdata
            self.selection_end = event.xdata
        else:
            if self.on_seek and self.duration > 0:
                frac = max(0.0, min(1.0, event.xdata))
                
                # Immediately update playhead visually
                self._playhead_frac = frac
                self._draw_overlay_playhead()
                
                self.on_seek(frac)

    def _on_drag(self, event):
        if event.inaxes != self.ax or not self.is_selecting:
            return
        if event.xdata is None:
            return
        self.selection_end = event.xdata
        self._draw_selection()

    def _on_release(self, event):
        if self.is_selecting:
            self.is_selecting = False
            if self.selection_start is not None and self.selection_end is not None:
                if self.selection_start > self.selection_end:
                    self.selection_start, self.selection_end = self.selection_end, self.selection_start
                if self.on_selection_change:
                    self.on_selection_change(self.selection_start, self.selection_end)

    def _draw_selection(self):
        """Draw selection rectangle on matplotlib axes."""
        if self.selection_rect:
            try:
                self.selection_rect.remove()
            except Exception:
                pass
            self.selection_rect = None
        
        if self.selection_start is None or self.selection_end is None:
            return
        
        width = self.selection_end - self.selection_start
        self.selection_rect = Rectangle(
            (self.selection_start, -1), width, 2,
            facecolor='#4caf50', alpha=0.3, edgecolor='#4caf50'
        )
        self.ax.add_patch(self.selection_rect)
        self.canvas.draw_idle()
    
    def _redraw_selection_patch(self):
        if self.selection_start is None or self.selection_end is None:
            return
        width = self.selection_end - self.selection_start
        self.selection_rect = Rectangle(
            (self.selection_start, -1), width, 2,
            facecolor='#4caf50', alpha=0.3, edgecolor='#4caf50'
        )
        self.ax.add_patch(self.selection_rect)

    def set_selection_mode(self, active: bool):
        self.selection_mode_active = active
        if not active:
            self._clear_selection()

    def _clear_selection(self):
        if self.selection_rect:
            try:
                self.selection_rect.remove()
            except Exception:
                pass
            self.selection_rect = None
        self.selection_start = None
        self.selection_end = None

        self.canvas.draw_idle()
