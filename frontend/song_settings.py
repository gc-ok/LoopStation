"""
Song settings dialog (header "Song" button). Everything here is per song and
saved by the StateManager under the song's "settings".

Sections:
- Tempo & key: tempo % slider (50-150) and key stepper (-6..+6 semitones);
  Apply calls set_tempo_pitch(). The render runs in the background; the app
  forwards 'render_state' events to on_render_state() (Apply is disabled
  while rendering).
- Tempo map: BPM / beats per bar / downbeat offset, "Detect" (estimate_tempo,
  filled in by on_tempo_estimated) and "Tap tempo"; Save calls
  set_song_tempo_map().
- Stems: per-stem mute / solo / gain (-24..+6 dB), routing mode and output
  device.
- OSC: the cue sent when GO starts this song (set_song_osc_cue).
"""

import logging
import time
from typing import Callable, Dict, List, Optional

import tkinter as tk
import customtkinter as ctk

from config import (
    COLOR_BG_DARK, COLOR_TEXT, COLOR_TEXT_DIM,
    COLOR_BTN_PRIMARY, COLOR_BTN_SUCCESS, COLOR_BTN_WARNING, COLOR_BTN_DANGER,
    COLOR_BTN_TEXT,
)
from .widgets import ModalDialog, SECONDARY_BTN

logger = logging.getLogger("LoopStation.SongSettings")

TEMPO_MIN, TEMPO_MAX = 50, 150
SEMI_MIN, SEMI_MAX = -6, 6
GAIN_MIN, GAIN_MAX = -24.0, 6.0
GAIN_DEBOUNCE_MS = 150
TAP_RESET_S = 2.0
TAP_MAX = 8

ROUTING_LABELS = {"stereo": "Stereo", "click_right": "Click → right", "multichannel": "Multichannel"}
ROUTING_BY_LABEL = {v: k for k, v in ROUTING_LABELS.items()}
DEFAULT_DEVICE = "System default"


def tap_bpm(taps: List[float]) -> Optional[float]:
    """BPM from tap timestamps (seconds): mean interval of the last taps."""
    if len(taps) < 2:
        return None
    recent = taps[-TAP_MAX:]
    intervals = [b - a for a, b in zip(recent, recent[1:]) if b > a]
    if not intervals:
        return None
    return 60.0 / (sum(intervals) / len(intervals))


class SongSettingsDialog(ModalDialog):
    WIDTH = 600

    def __init__(self, parent, state_manager, on_close: Optional[Callable] = None):
        song = getattr(state_manager, "current_song_name", "") or "song"
        # Non-modal: GO and the transport must stay usable while it is open
        super().__init__(parent, f"Song Settings: {song}", on_close=on_close, modal=False)
        self.app_state = state_manager
        self._rendering = False
        self._live = bool(getattr(state_manager, "live_mode", False))
        self._taps: List[float] = []
        self._stem_rows: Dict[str, dict] = {}
        self._stem_names: List[str] = []
        self._gain_pending: Dict[str, float] = {}
        self._gain_after = None
        self._devices: Dict[str, Optional[int]] = {}
        self._last_device_error: Optional[str] = None
        state_manager.on('device_error', self._remember_device_error)

        settings = state_manager.settings
        self._build_tempo_key(settings)
        self._build_tempo_map(settings)
        self._build_stems(settings)
        self._build_osc(settings)

        self.lbl_status = ctk.CTkLabel(self.actions, text="", font=("Segoe UI", 11),
                                       text_color=COLOR_TEXT_DIM, anchor="w")
        self.lbl_status.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(self.actions, text="Close", width=100, height=36,
                      **SECONDARY_BTN, text_color=COLOR_TEXT,
                      command=self.close).pack(side="right")

        self.set_live_mode(self._live)
        self.finish_layout()

    # ------------------------------------------------------- tempo & key

    def _build_tempo_key(self, s):
        box = self.section("⏩ Tempo & key (rendered in the background)")
        r1 = ctk.CTkFrame(box, fg_color="transparent")
        r1.pack(fill="x", pady=2)
        ctk.CTkLabel(r1, text="Tempo", width=60, anchor="w", text_color=COLOR_TEXT).pack(side="left")
        self.tempo_slider = ctk.CTkSlider(r1, from_=TEMPO_MIN, to=TEMPO_MAX,
                                          number_of_steps=TEMPO_MAX - TEMPO_MIN,
                                          command=self._on_tempo_slide)
        self.tempo_slider.pack(side="left", fill="x", expand=True, padx=6)
        self.lbl_tempo = ctk.CTkLabel(r1, text="", width=56, font=("Consolas", 13, "bold"),
                                      text_color=COLOR_TEXT)
        self.lbl_tempo.pack(side="left")
        pct = int(round(max(TEMPO_MIN, min(TEMPO_MAX, s.tempo_ratio * 100))))
        self.tempo_slider.set(pct)
        self._on_tempo_slide(pct)

        r2 = ctk.CTkFrame(box, fg_color="transparent")
        r2.pack(fill="x", pady=2)
        ctk.CTkLabel(r2, text="Key", width=60, anchor="w", text_color=COLOR_TEXT).pack(side="left")
        ctk.CTkButton(r2, text="−", width=32, height=28, **SECONDARY_BTN,
                      text_color=COLOR_TEXT, command=lambda: self._step_semis(-1)).pack(side="left", padx=(6, 0))
        self.lbl_semis = ctk.CTkLabel(r2, text="", width=110, font=("Consolas", 13, "bold"),
                                      text_color=COLOR_TEXT)
        self.lbl_semis.pack(side="left")
        ctk.CTkButton(r2, text="+", width=32, height=28, **SECONDARY_BTN,
                      text_color=COLOR_TEXT, command=lambda: self._step_semis(1)).pack(side="left")
        self._semis = int(round(max(SEMI_MIN, min(SEMI_MAX, s.semitones))))
        self._show_semis()

        self.btn_reset_tk = ctk.CTkButton(r2, text="Reset", width=64, height=30,
                                          **SECONDARY_BTN, text_color=COLOR_TEXT,
                                          command=self._reset_tempo_key)
        self.btn_reset_tk.pack(side="right")
        self.btn_apply = ctk.CTkButton(r2, text="Apply", width=90, height=30,
                                       fg_color=COLOR_BTN_SUCCESS, text_color=COLOR_BTN_TEXT,
                                       font=("Segoe UI", 12, "bold"), command=self._apply_tempo_key)
        self.btn_apply.pack(side="right", padx=6)
        self.lbl_render = ctk.CTkLabel(box, text="Cue times stay on the original song time.",
                                       font=("Segoe UI", 10), text_color=COLOR_TEXT_DIM, anchor="w")
        self.lbl_render.pack(fill="x")

    def _on_tempo_slide(self, value):
        self.lbl_tempo.configure(text=f"{int(round(float(value)))}%")

    def _step_semis(self, delta):
        self._semis = max(SEMI_MIN, min(SEMI_MAX, self._semis + delta))
        self._show_semis()

    def _show_semis(self):
        n = self._semis
        self.lbl_semis.configure(text="original key" if n == 0 else f"{n:+d} semitone{'s' if abs(n) != 1 else ''}")

    def _reset_tempo_key(self):
        self.tempo_slider.set(100)
        self._on_tempo_slide(100)
        self._semis = 0
        self._show_semis()

    def _apply_tempo_key(self):
        if self._rendering:
            return
        ratio = int(round(self.tempo_slider.get())) / 100.0
        ok = self.app_state.set_tempo_pitch(ratio, float(self._semis))
        if ok is False:
            self.lbl_render.configure(text="🔒 Blocked in Live Mode" if self._live else "Tempo/key request failed",
                                      text_color="#ff7b72")
            return
        # The render_state events arrive through the app's queue; lock Apply now
        # so a double click cannot start two renders.
        self.on_render_state("rendering", "")

    def on_render_state(self, state, message=""):
        """'render_state' from the app (Tk thread)."""
        if self._closed:
            return
        if state == "rendering":
            self._rendering = True
            self.lbl_render.configure(text="⏳ Rendering tempo/key… playback continues meanwhile",
                                      text_color="#f0a020")
        elif state == "done":
            self._rendering = False
            self.lbl_render.configure(text="✓ Tempo/key applied", text_color="#4cd38a")
        else:
            self._rendering = False
            self.lbl_render.configure(text=f"✗ Render failed: {message}"[:120], text_color="#ff7b72")
        if not self._rendering:
            self.on_settings_changed(self.app_state.settings)
        self._update_apply_state()

    def _update_apply_state(self):
        disabled = self._rendering or self._live
        self.btn_apply.configure(state="disabled" if disabled else "normal",
                                 text="Rendering…" if self._rendering else "Apply")
        self.btn_reset_tk.configure(state="disabled" if self._live else "normal")

    # --------------------------------------------------------- tempo map

    def _build_tempo_map(self, s):
        box = self.section("🥁 Tempo map (bar:beat readout, bar grid, Go to bar)")
        r = ctk.CTkFrame(box, fg_color="transparent")
        r.pack(fill="x", pady=2)
        self.entry_bpm = self._labeled_entry(r, "BPM", 70, f"{s.bpm:g}" if s.bpm > 0 else "")
        self.entry_bpb = self._labeled_entry(r, "Beats/bar", 44, str(s.beats_per_bar))
        self.entry_offset = self._labeled_entry(r, "Downbeat (s)", 70, f"{s.downbeat_offset:.3f}")

        r2 = ctk.CTkFrame(box, fg_color="transparent")
        r2.pack(fill="x", pady=(4, 2))
        self.btn_detect = ctk.CTkButton(r2, text="🔎 Detect", width=90, height=28,
                                        fg_color=COLOR_BTN_PRIMARY, text_color=COLOR_BTN_TEXT,
                                        command=self._detect)
        self.btn_detect.pack(side="left")
        self.btn_tap = ctk.CTkButton(r2, text="👆 Tap tempo", width=110, height=28,
                                     **SECONDARY_BTN, text_color=COLOR_TEXT,
                                     command=self._tap)
        self.btn_tap.pack(side="left", padx=6)
        self.btn_offset_here = ctk.CTkButton(r2, text="Downbeat = playhead", width=150, height=28,
                                             **SECONDARY_BTN, text_color=COLOR_TEXT,
                                             command=self._offset_from_playhead)
        self.btn_offset_here.pack(side="left")
        self.btn_save_map = ctk.CTkButton(r2, text="Save", width=70, height=28, fg_color=COLOR_BTN_SUCCESS,
                      text_color=COLOR_BTN_TEXT, command=self._save_tempo_map)
        self.btn_save_map.pack(side="right")
        self.lbl_map = ctk.CTkLabel(box, text="BPM 0 = no tempo map.", font=("Segoe UI", 10),
                                    text_color=COLOR_TEXT_DIM, anchor="w")
        self.lbl_map.pack(fill="x")

    def _labeled_entry(self, parent, label, width, value):
        ctk.CTkLabel(parent, text=label, anchor="w", text_color=COLOR_TEXT).pack(side="left", padx=(0, 4))
        e = ctk.CTkEntry(parent, width=width, justify="center")
        e.insert(0, value)
        e.pack(side="left", padx=(0, 12))
        return e

    @staticmethod
    def _set_entry(entry, value):
        entry.delete(0, "end")
        entry.insert(0, value)

    def _detect(self):
        if self._live:
            return
        try:
            self.app_state.estimate_tempo()
        except Exception as e:
            self.lbl_map.configure(text=f"Detect failed: {e}", text_color="#ff7b72")
            return
        self.btn_detect.configure(state="disabled", text="Detecting…")
        self.lbl_map.configure(text="Analysing the song…", text_color="#f0a020")

    def on_tempo_estimated(self, bpm, offset):
        """'tempo_estimated' from the app (Tk thread): fill the fields."""
        if self._closed:
            return
        self.btn_detect.configure(state="normal", text="🔎 Detect")
        if not bpm:
            self.lbl_map.configure(text="No steady tempo found.", text_color="#ff7b72")
            return
        self._set_entry(self.entry_bpm, f"{float(bpm):.1f}")
        self._set_entry(self.entry_offset, f"{float(offset):.3f}")
        self.lbl_map.configure(text=f"Detected ≈{float(bpm):.1f} BPM — check it, then Save.",
                               text_color="#4cd38a")

    def _tap(self):
        now = time.monotonic()
        if self._taps and now - self._taps[-1] > TAP_RESET_S:
            self._taps = []
        self._taps.append(now)
        bpm = tap_bpm(self._taps)
        if bpm is None:
            self.lbl_map.configure(text="Keep tapping on the beat…", text_color=COLOR_TEXT_DIM)
            return
        self._set_entry(self.entry_bpm, f"{bpm:.1f}")
        self.lbl_map.configure(text=f"Tapped {len(self._taps)} beats: {bpm:.1f} BPM",
                               text_color=COLOR_TEXT_DIM)

    def _offset_from_playhead(self):
        try:
            pos = float(self.app_state.get_position())
        except Exception:
            return
        self._set_entry(self.entry_offset, f"{pos:.3f}")

    def _save_tempo_map(self):
        if self._live:
            self.lbl_map.configure(text="🔒 Blocked in Live Mode", text_color="#ff7b72")
            return
        try:
            bpm_text = self.entry_bpm.get().strip()
            bpm = float(bpm_text) if bpm_text else 0.0
            bpb = int(self.entry_bpb.get().strip() or 4)
            offset = float(self.entry_offset.get().strip() or 0.0)
            if bpm < 0 or bpm > 400 or bpb < 1 or bpb > 32:
                raise ValueError
        except ValueError:
            self.lbl_map.configure(text="BPM 0-400, beats/bar 1-32, offset in seconds.",
                                   text_color="#ff7b72")
            return
        self.app_state.set_song_tempo_map(bpm, bpb, offset)
        self.lbl_map.configure(text="Saved." if bpm > 0 else "Tempo map cleared.",
                               text_color="#4cd38a")

    # -------------------------------------------------------------- stems

    def _build_stems(self, s):
        box = self.section("🎚 Stems & output")
        self.source_menu = ctk.CTkSegmentedButton(
            box, values=["Full mix", "Stems", "Custom"], command=self._on_source)
        self.source_menu.pack(fill="x", pady=(0, 4))
        self.lbl_levels = ctk.CTkLabel(box, text="Output: silent", anchor="w",
                                      text_color=COLOR_TEXT_DIM)
        self.lbl_levels.pack(fill="x")
        self.stems_box = ctk.CTkFrame(box, fg_color="transparent")
        self.stems_box.pack(fill="x")
        self.on_stems_changed(list(getattr(self.app_state, "stems", []) or []))

        r = ctk.CTkFrame(box, fg_color="transparent")
        r.pack(fill="x", pady=(8, 2))
        ctk.CTkLabel(r, text="Routing", width=70, anchor="w", text_color=COLOR_TEXT).pack(side="left")
        self.routing = ctk.CTkSegmentedButton(r, values=list(ROUTING_LABELS.values()),
                                              command=self._on_routing)
        self.routing.set(ROUTING_LABELS.get(s.routing_mode, "Stereo"))
        self.routing.pack(side="left")

        r2 = ctk.CTkFrame(box, fg_color="transparent")
        r2.pack(fill="x", pady=2)
        ctk.CTkLabel(r2, text="Output", width=70, anchor="w", text_color=COLOR_TEXT).pack(side="left")
        self.device_menu = ctk.CTkOptionMenu(r2, values=[DEFAULT_DEVICE], width=380,
                                             command=self._on_device)
        self.device_menu.pack(side="left")
        self.lbl_out = ctk.CTkLabel(box, text="Click → right: song in the left output, click in "
                                    "the right (for in-ear click). Multichannel: song 1-2, click 3-4.",
                                    font=("Segoe UI", 10), text_color=COLOR_TEXT_DIM, anchor="w",
                                    wraplength=540, justify="left")
        self.lbl_out.pack(fill="x")
        self._load_devices()

    def on_stems_changed(self, stems):
        """'stems_changed' from the app: rebuild rows when the set changed, else update."""
        if self._closed:
            return
        names = [st.name for st in stems]
        components = [st for st in stems if st.name != 'mix' and 'click' not in st.name.lower()]
        mix = next((st for st in stems if st.name == 'mix'), None)
        custom = mix is not None and not mix.muted and any(not st.muted for st in components)
        self.source_menu.set("Custom" if custom else "Stems" if mix is not None and mix.muted else "Full mix")
        self.source_menu.configure(state="normal" if components and not self._live else "disabled")
        if names != self._stem_names:
            for child in self.stems_box.winfo_children():
                child.destroy()
            self._stem_rows = {}
            self._stem_names = names
            if len(stems) <= 1:
                ctk.CTkLabel(
                    self.stems_box, anchor="w", justify="left", wraplength=540,
                    font=("Segoe UI", 10), text_color=COLOR_TEXT_DIM,
                    text=("No stems for this song. Put stems in a folder named "
                          "\"<song>.stems\" next to the song, or name files "
                          "\"<song> - click.wav\", \"<song> - vocals.wav\"…"),
                ).pack(fill="x", pady=(0, 4))
            for st in stems:
                self._add_stem_row(st)
        for st in stems:
            row = self._stem_rows.get(st.name)
            if row is None:
                continue
            row["mute"].configure(fg_color=COLOR_BTN_DANGER if st.muted else "transparent")
            row["solo"].configure(fg_color=COLOR_BTN_WARNING if st.solo else "transparent",
                                  text_color="#000000" if st.solo else COLOR_TEXT)
            row["muted"], row["soloed"] = st.muted, st.solo
            if st.name not in self._gain_pending:
                row["slider"].set(max(GAIN_MIN, min(GAIN_MAX, st.gain_db)))
                row["gain"].configure(text=f"{st.gain_db:+.1f} dB")

    def _add_stem_row(self, st):
        r = ctk.CTkFrame(self.stems_box, fg_color=COLOR_BG_DARK, corner_radius=6)
        r.pack(fill="x", pady=2)
        label = st.name + ("  🎧" if "click" in st.name.lower() else "")
        ctk.CTkLabel(r, text=label, width=110, anchor="w", text_color=COLOR_TEXT,
                     font=("Segoe UI", 12, "bold")).pack(side="left", padx=(8, 4))
        btn_m = ctk.CTkButton(r, text="M", width=30, height=26, **SECONDARY_BTN,
                              text_color=COLOR_TEXT,
                              command=lambda n=st.name: self._toggle_mute(n))
        btn_m.pack(side="left", padx=2)
        btn_s = ctk.CTkButton(r, text="S", width=30, height=26, **SECONDARY_BTN,
                              text_color=COLOR_TEXT,
                              command=lambda n=st.name: self._toggle_solo(n))
        btn_s.pack(side="left", padx=2)
        lbl = ctk.CTkLabel(r, text="", width=72, font=("Consolas", 11), text_color=COLOR_TEXT)
        lbl.pack(side="right", padx=(4, 8))
        slider = ctk.CTkSlider(r, from_=GAIN_MIN, to=GAIN_MAX, number_of_steps=60,
                               command=lambda v, n=st.name: self._on_gain(n, v))
        slider.pack(side="left", fill="x", expand=True, padx=6, pady=6)
        slider.bind("<Double-Button-1>", lambda _e, n=st.name: self._reset_gain(n))
        self._stem_rows[st.name] = {"mute": btn_m, "solo": btn_s, "slider": slider,
                                    "gain": lbl, "muted": st.muted, "soloed": st.solo}

    def _toggle_mute(self, name):
        row = self._stem_rows[name]
        self.app_state.set_stem_mute(name, not row["muted"])

    def _toggle_solo(self, name):
        row = self._stem_rows[name]
        self.app_state.set_stem_solo(name, not row["soloed"])

    def _on_gain(self, name, value):
        self._stem_rows[name]["gain"].configure(text=f"{float(value):+.1f} dB")
        self._gain_pending[name] = float(value)
        if self._gain_after is not None:
            self.after_cancel(self._gain_after)
        self._gain_after = self.after(GAIN_DEBOUNCE_MS, self._flush_gain)

    def _reset_gain(self, name):
        self._stem_rows[name]["slider"].set(0.0)
        self._on_gain(name, 0.0)

    def _flush_gain(self):
        self._gain_after = None
        pending, self._gain_pending = self._gain_pending, {}
        for name, value in pending.items():
            self.app_state.set_stem_gain(name, round(value, 1))

    def _on_routing(self, label):
        mode = ROUTING_BY_LABEL.get(label, "stereo")
        if not self.app_state.set_routing(mode):
            msg = self._last_device_error or "the output device cannot do this"
            self.lbl_out.configure(text=f"Could not switch routing to {label}: {msg}",
                                   text_color="#ff7b72")
            self.routing.set(ROUTING_LABELS.get(self.app_state.settings.routing_mode, "Stereo"))
        else:
            self.lbl_out.configure(text=f"Routing: {label}", text_color=COLOR_TEXT_DIM)

    def _load_devices(self):
        devices = self.app_state.list_output_devices()
        self._devices = {DEFAULT_DEVICE: None}
        for d in devices:
            label = f"{d.get('index')}: {d.get('name', '?')} ({d.get('channels', '?')} ch)"
            self._devices[label] = d.get("index")
        self.device_menu.configure(values=list(self._devices))
        current = None
        try:
            cur = self.app_state.audio.get_output_device()
            current = cur.get("index") if cur else None
        except Exception:
            current = None
        if current is None:
            try:
                from utils import preferences
                current = (preferences.load_preferences().get("audio") or {}).get("output_device")
            except Exception:
                current = None
        chosen = DEFAULT_DEVICE
        for label, idx in self._devices.items():
            if idx is not None and idx == current:
                chosen = label
        self.device_menu.set(chosen)

    def _on_device(self, label):
        index = self._devices.get(label)
        if self.app_state.set_output_device(index):
            self.lbl_out.configure(text=f"Output: {label}", text_color=COLOR_TEXT_DIM)
        else:
            msg = self._last_device_error or "device unavailable"
            self.lbl_out.configure(text=f"Could not open {label}: {msg}", text_color="#ff7b72")
            self._load_devices()        # selector back to the device in use

    # ---------------------------------------------------------------- OSC

    def _build_osc(self, s):
        box = self.section("📤 OSC cue for this song")
        r = ctk.CTkFrame(box, fg_color="transparent")
        r.pack(fill="x", pady=2)
        self.entry_osc = ctk.CTkEntry(r, width=200, placeholder_text="e.g. 12.5")
        if s.osc_cue:
            self.entry_osc.insert(0, s.osc_cue)
        self.entry_osc.pack(side="left")
        self.entry_osc.bind("<Return>", lambda _e: self._save_osc())
        self.btn_save_osc = ctk.CTkButton(r, text="Save", width=70, height=28, fg_color=COLOR_BTN_SUCCESS,
                      text_color=COLOR_BTN_TEXT, command=self._save_osc)
        self.btn_save_osc.pack(side="left", padx=6)
        self.lbl_osc = ctk.CTkLabel(box, text="Sent when GO starts this song (OSC must be on in ⚙ Settings).",
                                    font=("Segoe UI", 10), text_color=COLOR_TEXT_DIM, anchor="w")
        self.lbl_osc.pack(fill="x")

    def _save_osc(self):
        if self._live:
            self.lbl_osc.configure(text="🔒 Blocked in Live Mode", text_color="#ff7b72")
            return
        self.app_state.set_song_osc_cue(self.entry_osc.get().strip())
        self.lbl_osc.configure(text="Saved.", text_color="#4cd38a")

    # ---------------------------------------------------------- live mode

    def set_live_mode(self, live: bool):
        self._live = bool(live)
        self._update_apply_state()
        state = "disabled" if self._live else "normal"
        for w in (self.routing, self.device_menu, self.source_menu,
                  self.entry_bpm, self.entry_bpb, self.entry_offset,
                  self.btn_detect, self.btn_tap, self.btn_offset_here, self.btn_save_map,
                  self.entry_osc, self.btn_save_osc):
            try:
                w.configure(state=state)
            except (tk.TclError, ValueError):
                pass
        self.lbl_status.configure(
            text="🔒 Live Mode: song editing is locked" if self._live else "")
        if not self._live:
            self.on_stems_changed(self.app_state.stems)

    def _on_source(self, source):
        if self._live or source == 'Custom':
            return
        self.app_state.set_playback_source(source)

    def update_output_levels(self, levels):
        if self._closed:
            return
        peak = float(levels.get('peak', 0))
        clips = int(levels.get('clipped_blocks', 0))
        import math
        db = 20 * math.log10(peak) if peak > 0 else None
        label = f"Output: {db:+.1f} dBFS" if db is not None else "Output: silent"
        if clips:
            label += f" · CLIPPING ({clips} blocks) — lower gains"
        self.lbl_levels.configure(text=label, text_color="#ff7b72" if clips else COLOR_TEXT_DIM)

    def _remember_device_error(self, message):
        # Emitted synchronously inside set_routing / set_output_device, so the
        # message is known when the call returns False.
        self._last_device_error = str(message)

    def on_device_error(self, message):
        """'device_error' from the app (Tk thread)."""
        if self._closed:
            return
        try:
            self.lbl_out.configure(text=f"⚠ {message}", text_color="#ff7b72")
        except Exception:
            pass

    def on_settings_changed(self, settings):
        """'settings_changed' from the app; keep the routing selector in sync."""
        if self._closed:
            return
        try:
            if not self._rendering:
                self.tempo_slider.set(settings.tempo_ratio * 100)
                self._on_tempo_slide(settings.tempo_ratio * 100)
                self._semis = int(round(settings.semitones))
                self._show_semis()
            self.routing.set(ROUTING_LABELS.get(settings.routing_mode, "Stereo"))
        except Exception:
            pass

    def close(self):
        try:
            self.app_state.off('device_error', self._remember_device_error)
        except Exception:
            pass
        if self._gain_after is not None:
            try:
                self.after_cancel(self._gain_after)
            except Exception:
                pass
            self._flush_gain()
        super().close()
