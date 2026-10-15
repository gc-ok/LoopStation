"""
Waveform Display Widget for Loop Station.

PERFORMANCE: The playhead is drawn on the Tk canvas that backs matplotlib,
NOT via matplotlib redraw. This gives ~60fps playhead updates.

COORDINATE FIX: Uses matplotlib's ax.transData to convert between data
coordinates (0-1) and display pixels. This properly accounts for the fact
that the tk canvas widget may be larger than the matplotlib figure.
"""

import logging
import math
import numpy as np
import tkinter as tk
from typing import Optional, Callable, List, Tuple

import matplotlib
matplotlib.use('TkAgg')
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle

from utils.fonts import FONT_UI
from config import (
    COLOR_WAVEFORM, COLOR_WAVEFORM_BG, COLOR_PLAYHEAD,
    COLOR_LOOP_IN, COLOR_LOOP_OUT, COLOR_LOOP_REGION,
    COLOR_LOOP_GHOST, COLOR_LOOP_GHOST_ALPHA,
    COLOR_MARKER, COLOR_TEXT_DIM,
    WAVEFORM_TARGET_SAMPLES, WAVEFORM_HEIGHT, COLOR_SKIP_REGION
)

logger = logging.getLogger("LoopStation.Waveform")

# Hold cues (caesuras): distinct from cue yellow, vamp green and cut red.
COLOR_HOLD = "#5ec8f2"
# Bar grid: faint line every bar, a stronger one every BAR_GRID_STRONG_EVERY bars.
COLOR_BAR_LINE = "#ffffff"
BAR_LINE_ALPHA = 0.07
BAR_LINE_STRONG_ALPHA = 0.18
BAR_GRID_STRONG_EVERY = 4
# Thinning thresholds (pixels between drawn lines / labels).
BAR_GRID_MIN_PX = 5
BAR_LABEL_MIN_PX = 34
# Never build more than this many bar lines in one draw.
BAR_GRID_MAX_LINES = 4000

# Frames read from the memmap per step while computing the envelope.
# 1 M stereo int16 frames = 4 MB resident at a time.
_ENVELOPE_CHUNK_FRAMES = 1 << 20


def compute_envelope(samples, n_buckets: int) -> np.ndarray:
    """
    Reduce PCM samples to a small peak envelope for drawing.

    Args:
        samples: array-like of shape (frames,) or (frames, channels), any
            integer or float dtype. A numpy memmap is read in chunks so the
            full file is never materialised (no float64 copy of the song).
        n_buckets: number of output points (the waveform's pixel budget).

    Returns:
        float32 array of length min(n_buckets, frames) with the max absolute
        sample of each bucket (across all channels), normalised to 0..1.
        Empty input gives an empty array.
    """
    if samples is None or n_buckets <= 0:
        return np.zeros(0, dtype=np.float32)
    n_frames = int(samples.shape[0]) if hasattr(samples, 'shape') else len(samples)
    if n_frames == 0:
        return np.zeros(0, dtype=np.float32)

    n_buckets = min(int(n_buckets), n_frames)
    bucket = n_frames // n_buckets           # frames per bucket (>= 1)
    env = np.zeros(n_buckets, dtype=np.float32)

    # Step over whole buckets so each chunk reshapes cleanly.
    buckets_per_chunk = max(1, _ENVELOPE_CHUNK_FRAMES // bucket)
    for b0 in range(0, n_buckets, buckets_per_chunk):
        b1 = min(n_buckets, b0 + buckets_per_chunk)
        block = np.asarray(samples[b0 * bucket:b1 * bucket])
        block = block.reshape(b1 - b0, -1)   # (buckets, frames*channels)
        # max(|x|) without an abs() copy and without int16 overflow on -32768
        hi = block.max(axis=1).astype(np.float32)
        lo = block.min(axis=1).astype(np.float32)
        env[b0:b1] = np.maximum(hi, -lo)

    peak = float(env.max()) if env.size else 0.0
    if peak > 0:
        env /= peak
    return env


def bar_times(bpm: float, beats_per_bar: int, downbeat_offset: float,
              start: float, end: float) -> List[Tuple[float, int]]:
    """
    Bar-line times inside [start, end] (seconds) for a constant tempo map.

    Bar ``n`` starts at ``downbeat_offset + (n - 1) * bar_seconds`` -- the
    same numbering as StateManager.bar_beat_at, so bar 1 begins at the
    first downbeat and a pickup before it is bar 0 (or lower).

    Returns a list of (time, bar_number), ascending. Empty when bpm <= 0,
    the range is empty, or the inputs are not finite.
    """
    try:
        bpm = float(bpm)
        bpb = max(1, int(beats_per_bar))
        offset = float(downbeat_offset or 0.0)
        start = float(start)
        end = float(end)
    except (TypeError, ValueError):
        return []
    if not all(math.isfinite(v) for v in (bpm, offset, start, end)):
        return []
    if bpm <= 0 or end < start:
        return []
    bar_len = 60.0 / bpm * bpb
    first = math.ceil((start - offset) / bar_len - 1e-9)   # zero-based bar index
    last = math.floor((end - offset) / bar_len + 1e-9)
    if last < first or last - first + 1 > BAR_GRID_MAX_LINES * 16:
        return []        # empty, or absurdly dense (caller would thin it away)
    return [(offset + k * bar_len, k + 1) for k in range(first, last + 1)]


def _widget_scaling(widget) -> float:
    """customtkinter's widget scaling for ``widget`` (1.0 if unknown)."""
    try:
        import customtkinter as ctk
        value = float(ctk.ScalingTracker.get_widget_scaling(widget))
    except Exception:
        return 1.0
    if not math.isfinite(value) or value <= 0:
        return 1.0
    return max(0.5, min(value, 4.0))


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
        self._placeholder_text = "Open a show or add songs, then pick a song to see its waveform"

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
        self.holds = []

        # Tempo map for the bar grid: (bpm, beats_per_bar, offset); bpm 0 = off
        self._tempo_map = (0.0, 4, 0.0)
        self._grid_drawn_width = 0

        # Plain tk widgets are not scaled by customtkinter: on a high-DPI
        # Windows display (150 %, 200 %...) use its widget scaling for the
        # fixed height. Text inside the figure is already scaled by
        # matplotlib's TkAgg device-pixel-ratio handling (figure dpi x
        # ratio), so the dpi stays 100 here. macOS reports 1.0 (Retina is
        # handled by the OS).
        self._scaling = _widget_scaling(parent)
        height_px = int(round(WAVEFORM_HEIGHT * self._scaling))

        # Container frame with fixed height
        self.container = tk.Frame(parent, bg=COLOR_WAVEFORM_BG, height=height_px)
        self.container.pack_propagate(False)  # Enforce fixed height
        
        # Matplotlib figure
        # figsize width is arbitrary since we pack fill=x, but height matters
        # We'll compute inches from the pixel height at 100 dpi
        fig_height_inches = height_px / 100.0
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
        self._hold_ids = []
        self._holds_sig = None
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
        # Resizing: FigureCanvasTk binds <Configure> on its own canvas and
        # resizes the figure to the widget size, so nothing to do here.

        # Signature of the last drawn loop/skip regions (to skip redundant redraws)
        self._drawn_regions_sig = None

        logger.debug("WaveformWidget initialized")

    def _on_mpl_draw(self, event):
        """Called after matplotlib redraws. Redraw overlay items on top."""
        self._draw_overlay_playhead()
        self._draw_overlay_markers()
        self._draw_overlay_holds()
        # matplotlib re-creates its image item on resize, which would cover
        # our overlay items; keep them on top.
        self._tk_canvas.tag_raise("overlay")
        # The bar grid is thinned for the canvas width; after a big resize,
        # rebuild it once for the new width.
        if self._tempo_map[0] > 0 and self.waveform_data is not None:
            width = self._tk_canvas.winfo_width()
            old = self._grid_drawn_width
            if width > 1 and old > 1 and abs(width - old) > 0.3 * old:
                self._grid_drawn_width = width
                self._tk_canvas.after_idle(self._draw_waveform)

    def _setup_axes(self):
        """Configure the matplotlib axes to fill the entire figure."""
        self.ax.set_facecolor(COLOR_WAVEFORM_BG)
        self.ax.set_xlim(self.view_start, self.view_end)
        self.ax.set_ylim(-1, 1)
        self.ax.axis('off')
        self.figure.subplots_adjust(left=0, right=1, top=1, bottom=0)
        if self.waveform_data is None and self._placeholder_text:
            # Empty state: tell the user what this area is for.
            self.ax.text(0.5, 0.5, self._placeholder_text,
                         ha='center', va='center', fontsize=11,
                         color=COLOR_TEXT_DIM, transform=self.ax.transAxes)

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
        """Move the playhead line on the tk canvas (fast, no matplotlib redraw)."""
        canvas_height = self._tk_canvas.winfo_height()
        if canvas_height <= 1:
            return

        x = self._frac_to_pixel_x(self._playhead_frac)

        if self._playhead_line_id is None:
            self._playhead_line_id = self._tk_canvas.create_line(
                x, 0, x, canvas_height,
                fill=COLOR_PLAYHEAD, width=2, tags="overlay"
            )
        else:
            self._tk_canvas.coords(self._playhead_line_id, x, 0, x, canvas_height)

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
                font=(FONT_UI, 8),
                tags="overlay"
            )
            self._marker_ids.append(text_id)
    
    def _draw_overlay_holds(self):
        """Draw hold (caesura) lines and ⏸ labels on the tk canvas."""
        for item_id in self._hold_ids:
            self._tk_canvas.delete(item_id)
        self._hold_ids.clear()

        if not self.holds or self.duration <= 0:
            return
        canvas_height = self._tk_canvas.winfo_height()
        if canvas_height <= 1:
            return

        for hold in self.holds:
            frac = hold.time / self.duration
            if frac < 0 or frac > 1:
                continue
            x = self._frac_to_pixel_x(frac)
            self._hold_ids.append(self._tk_canvas.create_line(
                x, 0, x, canvas_height,
                fill=COLOR_HOLD, width=2, tags="overlay"))
            name = hold.name[:12] + "…" if len(hold.name) > 12 else hold.name
            # Labels sit at the bottom so they never collide with cue labels.
            self._hold_ids.append(self._tk_canvas.create_text(
                x + 3, canvas_height - 4, text=f"⏸ {name}",
                fill=COLOR_HOLD, anchor="sw",
                font=(FONT_UI, 8, "bold"), tags="overlay"))

    @staticmethod
    def _holds_signature(holds):
        return tuple((h.id, h.time, h.name) for h in holds)

    def update_holds_display(self, holds):
        """Store holds and redraw their overlay only if they changed."""
        holds = list(holds or [])
        self.holds = holds
        sig = self._holds_signature(holds)
        if sig == self._holds_sig:
            return
        self._holds_sig = sig
        self._draw_overlay_holds()
        self._tk_canvas.tag_raise("overlay")

    # =========================================================================
    # BAR GRID
    # =========================================================================

    def set_tempo_map(self, bpm, beats_per_bar, downbeat_offset):
        """
        Show a bar grid for a constant tempo (bpm <= 0 removes it). Redraws
        only when the map actually changes; zooming redraws it along with
        the waveform.
        """
        try:
            bpm = float(bpm or 0.0)
            bpb = max(1, int(beats_per_bar or 4))
            offset = float(downbeat_offset or 0.0)
        except (TypeError, ValueError):
            bpm, bpb, offset = 0.0, 4, 0.0
        new_map = (bpm, bpb, offset) if bpm > 0 else (0.0, 4, 0.0)
        if new_map == self._tempo_map:
            return
        self._tempo_map = new_map
        self._draw_waveform()

    def _draw_bar_grid(self):
        """Faint bar lines (stronger every 4 bars), numbers when zoomed in."""
        bpm, bpb, offset = self._tempo_map
        if bpm <= 0 or self.duration <= 0:
            return
        view_w = self.view_end - self.view_start
        if view_w <= 0:
            return
        width = self._tk_canvas.winfo_width()
        if width <= 1:
            width = int(self.figure.get_figwidth() * self.figure.dpi)
        self._grid_drawn_width = width

        bar_len = 60.0 / bpm * bpb
        px_per_bar = width * (bar_len / self.duration) / view_w
        if px_per_bar >= BAR_GRID_MIN_PX:
            step = 1
        elif px_per_bar * BAR_GRID_STRONG_EVERY >= BAR_GRID_MIN_PX:
            step = BAR_GRID_STRONG_EVERY
        else:
            return  # far too dense to be useful

        bars = bar_times(bpm, bpb, offset,
                         self.view_start * self.duration, self.view_end * self.duration)
        if not bars or len(bars) // step > BAR_GRID_MAX_LINES:
            return

        weak, strong = [], []
        for t, n in bars:
            if (n - 1) % step:
                continue
            if (n - 1) % BAR_GRID_STRONG_EVERY == 0:
                strong.append(t / self.duration)
            else:
                weak.append(t / self.duration)
        if weak:
            self.ax.vlines(weak, -1, 1, colors=COLOR_BAR_LINE,
                           alpha=BAR_LINE_ALPHA, linewidth=1, zorder=1.2)
        if strong:
            self.ax.vlines(strong, -1, 1, colors=COLOR_BAR_LINE,
                           alpha=BAR_LINE_STRONG_ALPHA, linewidth=1, zorder=1.2)

        # Bar numbers: every bar when roomy, otherwise every 4th bar.
        if px_per_bar >= BAR_LABEL_MIN_PX:
            label_every = 1
        elif px_per_bar * BAR_GRID_STRONG_EVERY >= BAR_LABEL_MIN_PX:
            label_every = BAR_GRID_STRONG_EVERY
        else:
            return
        for t, n in bars:
            if n < 1 or (n - 1) % label_every:
                continue
            self.ax.text(t / self.duration, -0.98, f" {n}", ha='left', va='bottom',
                         fontsize=6.5, color=COLOR_BAR_LINE, alpha=0.5,
                         zorder=5, fontfamily='sans-serif', clip_on=True)

    # =========================================================================
    # WAVEFORM GENERATION
    # =========================================================================
    
    def begin_song(self, duration: float):
        """
        A new song was loaded but its envelope is still being computed:
        drop the old waveform and remember the duration so loop/marker
        updates that arrive before the envelope are positioned correctly.
        """
        self.waveform_data = None
        self.duration = duration
        self.loops = []
        self.skips = []
        self.markers = []
        self.holds = []
        self._holds_sig = None
        self._draw_overlay_holds()
        self.selected_index = -1
        self._drawn_regions_sig = None
        self.view_start, self.view_end = 0.0, 1.0
        self._playhead_frac = 0.0
        # A selection belongs to the previous song's timeline
        self.is_selecting = False
        self.selection_rect = None      # removed by ax.clear() below
        self.selection_start = None
        self.selection_end = None
        self._placeholder_text = "Loading waveform…"
        self.ax.clear()
        self._setup_axes()
        self.canvas.draw_idle()

    def load_waveform(self, envelope: np.ndarray, duration: float):
        """
        Display a precomputed envelope (see compute_envelope). Raw PCM is
        accepted too, but callers should compute the envelope off the Tk
        thread and pass the small array in.
        """
        self.duration = duration

        if envelope is None or len(envelope) == 0:
            logger.warning("No audio data for waveform")
            return

        envelope = np.asarray(envelope)
        if envelope.ndim != 1 or len(envelope) > WAVEFORM_TARGET_SAMPLES:
            envelope = compute_envelope(envelope, WAVEFORM_TARGET_SAMPLES)

        self.waveform_data = envelope.astype(np.float32, copy=False)
        self._drawn_regions_sig = None
        self._draw_waveform()
        logger.debug(f"Waveform plotted ({len(self.waveform_data)} points)")

    @staticmethod
    def _regions_signature(loops, skips, selected_index, tempo_map=None):
        return (
            tuple((l.start, l.end, l.name, getattr(l, 'active', True)) for l in loops),
            tuple((s.start, s.end, getattr(s, 'active', True)) for s in skips),
            selected_index,
            tempo_map,
        )

    def _redraw_if_regions_changed(self):
        sig = self._regions_signature(self.loops, self.skips, self.selected_index,
                                      self._tempo_map)
        if sig == self._drawn_regions_sig:
            return
        self._draw_waveform()

    def update_skips_display(self, skips):
        """Update stored skips and redraw if they changed."""
        self.skips = skips
        self._redraw_if_regions_changed()

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
        
        # Bar grid (faint lines over the waveform, under the vamp regions)
        self._draw_bar_grid()

        # Draw regions (Loops, Skips)
        self._draw_skip_regions() 
        self._draw_loop_regions()
        
        if self.selection_start is not None and self.selection_end is not None:
            self._redraw_selection_patch()

        self._drawn_regions_sig = self._regions_signature(
            self.loops, self.skips, self.selected_index, self._tempo_map)
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
        """Update stored loops and redraw if they changed."""
        self.loops = loops
        self.selected_index = selected_index
        self._redraw_if_regions_changed()

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
        """
        Store the selected vamp's in/out points. Drawing uses the loop list
        (update_loops_display), so this never triggers a redraw by itself.
        """
        self.loop_in = loop_in
        self.loop_out = loop_out

    def clear(self):
        """Clear the waveform display."""
        self.waveform_data = None
        self.duration = 0.0
        self.loops = []
        self.skips = []
        self.markers = []
        self.holds = []
        self._holds_sig = None
        self._draw_overlay_holds()
        self.selected_index = -1
        self._drawn_regions_sig = None
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

                # The callback moves the playhead once the seek is accepted
                # (update_playhead); a refused seek (Live Mode) returns False
                # and the playhead stays where the engine is. Callbacks that
                # return nothing get the old immediate move.
                if self.on_seek(frac) is None:
                    self._playhead_frac = frac
                    self._draw_overlay_playhead()

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

    def get_selection(self):
        """(start, end) of the current selection in seconds, or None."""
        if self.selection_start is None or self.selection_end is None or self.duration <= 0:
            return None
        a, b = sorted((self.selection_start, self.selection_end))
        a = max(0.0, min(1.0, a))
        b = max(0.0, min(1.0, b))
        if b <= a:
            return None
        return (a * self.duration, b * self.duration)

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
