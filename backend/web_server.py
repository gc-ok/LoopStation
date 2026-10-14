"""
Web Server for Loop Station - Local Network Cue Monitor.

Serves a mobile-friendly read-only view of the current cue sheet state
over the local network. Designed for theater/rehearsal use where
tech crew, stage managers, and directors need to see cues on their
phones or tablets backstage.

Usage:
    The server is started/stopped from the main app UI.
    It binds to 0.0.0.0 on an available port (default 8080).

    Devices on the same WiFi network can access the monitor at:
        http://<your-local-ip>:<port>/?k=<token>

    Every start() generates a fresh random token. Requests without the
    token (as the ``k`` query parameter, or the cookie that the first
    tokenised hit sets) are rejected with 403, so other people on the
    venue Wi-Fi cannot read director/tech notes without the share link
    or QR code the operator hands out.

    Routes: ``/`` (page), ``/api/state`` (JSON, polled every 500 ms),
    ``POST /api/ack`` ({device, cue_id} -> 204; a backstage device
    acknowledges a cue), ``/qr.png``.

Dependencies:
    pip install flask qrcode pillow
"""

import contextlib
import errno
import io
import ipaddress
import json
import secrets
import socket
import logging
import threading
import time
from collections import OrderedDict
from typing import Optional, Dict, Any, List

logger = logging.getLogger("LoopStation.WebServer")

from flask import Flask, jsonify, Response, request, g, abort

try:
    import qrcode
    HAS_QRCODE = True
except ImportError:  # optional dependency; frontend shows a hint instead
    HAS_QRCODE = False

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import TAG_COLORS

TOKEN_PARAM = "k"
TOKEN_COOKIE = "loopstation_monitor"
TOKEN_COOKIE_MAX_AGE = 12 * 60 * 60  # one long tech rehearsal


class WebServerStartError(RuntimeError):
    """Raised by CueWebServer.start() when the listener cannot be bound."""


# =========================================================================
# LOCAL IP DISCOVERY
# =========================================================================

def _udp_route_candidate() -> Optional[str]:
    """
    Address of the interface that holds the default route (UDP connect
    trick; no packets are sent). None when there is no route at all,
    e.g. an isolated rehearsal network without a gateway.
    """
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError as e:
        logger.debug(f"No default route for local IP detection: {e}")
        return None
    finally:
        if s is not None:
            s.close()


def _hostname_candidates() -> List[str]:
    """All IPv4 addresses the OS resolves for this machine's hostname."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError as e:
        logger.debug(f"Hostname address lookup failed: {e}")
        return []
    return [info[4][0] for info in infos if info[4]]


def _ip_rank(ip: str) -> int:
    """Lower is better. Typical venue Wi-Fi is 192.168.x / 10.x."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return 99
    if addr.is_loopback or addr.is_multicast or addr.is_unspecified:
        return 99
    if addr in ipaddress.ip_network("192.168.0.0/16"):
        return 0
    if addr in ipaddress.ip_network("10.0.0.0/8"):
        return 1
    if addr in ipaddress.ip_network("172.16.0.0/12"):
        return 2
    if addr.is_link_local:   # 169.254.x: APIPA, only useful with no DHCP
        return 4
    if addr.is_private:
        return 3
    return 5


def get_local_ips() -> List[str]:
    """
    Candidate LAN IPv4 addresses for this machine, best first.

    Never raises. Returns an empty list when no usable (non-loopback)
    address is found, so the caller can fall back to 127.0.0.1.
    """
    route_ip = _udp_route_candidate()
    seen = []
    for ip in ([route_ip] if route_ip else []) + _hostname_candidates():
        if ip and ip not in seen and _ip_rank(ip) < 99:
            seen.append(ip)
    # Stable sort: rank first, default-route interface wins ties.
    return sorted(seen, key=lambda ip: (_ip_rank(ip), ip != route_ip))


def get_local_ip() -> str:
    """Best guess for the machine's local network IP address."""
    ips = get_local_ips()
    return ips[0] if ips else "127.0.0.1"


# =========================================================================
# SHARED STATE (updated by main app, read by web server)
# =========================================================================

GO_LOG_LIMIT = 20        # entries of app_state.go_log mirrored to the page
ACK_CUE_LIMIT = 50       # cue ids whose acknowledgements we remember
ACK_DEVICE_MAX = 64      # characters; longer device names are rejected
ACK_CUE_ID_MAX = 128
ACK_DEVICES_PER_CUE = 32  # devices remembered per cue id (oldest dropped)
MAX_REQUEST_BYTES = 4096  # request bodies larger than this get 413


def _safe_call(obj, name, default):
    """``obj.name()`` if it exists and is callable, else ``default``.

    The StateManager is being extended concurrently; the monitor must keep
    working against both the old object (no standby()/go_log/holds) and the
    new one, so every new attribute is read through here or getattr().
    """
    fn = getattr(obj, name, None)
    if not callable(fn):
        return default
    try:
        return fn()
    except Exception as e:  # never let a show-logic bug kill the monitor push
        logger.debug(f"{name}() failed: {e}")
        return default


def _item_time(item, item_type) -> Optional[float]:
    """Trigger time of a timeline item: markers/holds have .time, vamps .start."""
    if item_type == 'vamp':
        value = getattr(item, 'start', None)
    else:
        value = getattr(item, 'time', None)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _item_to_dict(item, item_type, position: Optional[float] = None) -> Optional[dict]:
    """
    JSON-ready view of a timeline object for the page.

    ``item_type`` is the StateManager vocabulary: "marker", "vamp", "hold".
    The page vocabulary is "cue", "vamp", "hold" (kept for compatibility).
    A HoldCue is treated like a marker (single trigger time). When
    ``position`` is given a ``countdown`` (seconds until the item) is added.
    """
    if item is None:
        return None
    kind = 'vamp' if item_type == 'vamp' else ('hold' if item_type == 'hold' else 'cue')
    tag_notes = getattr(item, 'tag_notes', None) or {}
    d: Dict[str, Any] = {
        "id": getattr(item, 'id', None),
        "name": getattr(item, 'name', '') or '',
        "type": kind,
        "tag_notes": dict(tag_notes) if isinstance(tag_notes, dict) else {},
    }
    if kind == 'vamp':
        d["start"] = getattr(item, 'start', 0.0)
        d["end"] = getattr(item, 'end', 0.0)
    else:
        d["time"] = getattr(item, 'time', 0.0)
    if position is not None:
        t = _item_time(item, item_type)
        d["countdown"] = max(0.0, t - position) if t is not None else None
    return d


def _standby_to_dict(standby) -> dict:
    """Flatten StateManager.standby() to {action, label, next}; tolerant of junk."""
    if not isinstance(standby, dict):
        return {"action": "none", "label": "", "next": None}
    next_item = standby.get("next_item")
    next_type = standby.get("next_type")
    next_dict = None
    if next_item is not None:
        nd = _item_to_dict(next_item, next_type)
        if nd is not None:
            next_dict = {"id": nd["id"], "name": nd["name"], "type": nd["type"]}
    return {
        "action": str(standby.get("action") or "none"),
        "label": str(standby.get("label") or ""),
        "next": next_dict,
    }


def _go_log_entries(go_log) -> List[dict]:
    """Last GO_LOG_LIMIT entries of go_log as plain dicts (newest last)."""
    try:
        entries = list(go_log)[-GO_LOG_LIMIT:]
    except TypeError:
        return []
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        out.append({
            "ts": e.get("ts"),
            "source": str(e.get("source") or ""),
            "action": str(e.get("action") or ""),
            "position": e.get("position"),
            "label": str(e.get("label") or ""),
        })
    return out


class SharedCueState:
    """Thread-safe shared state between the main app and web server."""

    def __init__(self):
        self._lock = threading.Lock()
        self._state: Dict[str, Any] = {
            "song_name": "",
            "position": 0.0,
            "song_duration": 0.0,
            "is_playing": False,
            "is_paused": False,
            "is_looping": False,
            "state": "stopped",     # "stopped" | "playing" | "paused" | "held"
            "live": False,          # StateManager.live_mode
            "standby": {"action": "none", "label": "", "next": None},
            "go_log": [],           # last GO_LOG_LIMIT of {ts, source, action, position, label}
            "holds": [],            # [{id, time, name}] for the whole song
            "current_id": None,
            "next_id": None,
            "current": None,   # {id, name, type, time|start/end, tag_notes}
            "next": None,      # {id, name, type, ..., tag_notes, countdown}
        }
        # cue_id -> [{device, ts}]; insertion-ordered so the oldest cue id
        # can be dropped once ACK_CUE_LIMIT ids are remembered.
        self._acks: "OrderedDict[str, List[dict]]" = OrderedDict()

    def update(self, **kwargs):
        with self._lock:
            self._state.update(kwargs)
            self._state['updated_at'] = time.time()

    def get_state(self) -> dict:
        with self._lock:
            return dict(self._state)

    # ---- acknowledgements ------------------------------------------------

    def add_ack(self, cue_id: str, device: str, ts: Optional[float] = None) -> dict:
        """
        Record that ``device`` acknowledged ``cue_id``. One entry per device
        per cue (a repeat ACK refreshes its timestamp). Returns the entry.
        """
        entry = {"device": device, "ts": time.time() if ts is None else ts}
        with self._lock:
            acks = self._acks.get(cue_id)
            if acks is None:
                acks = self._acks[cue_id] = []
                while len(self._acks) > ACK_CUE_LIMIT:
                    self._acks.popitem(last=False)
            else:
                acks[:] = [a for a in acks if a["device"] != device]
            acks.append(entry)
            if len(acks) > ACK_DEVICES_PER_CUE:
                del acks[:len(acks) - ACK_DEVICES_PER_CUE]
        return dict(entry)

    def get_acks(self, cue_id) -> List[dict]:
        """Copies of the acks recorded for ``cue_id`` (oldest first)."""
        with self._lock:
            return [dict(a) for a in self._acks.get(cue_id, ())]

    def acks_snapshot(self) -> Dict[str, List[dict]]:
        """{cue_id: [{device, ts}, ...]} for every remembered cue (copy)."""
        with self._lock:
            return {cid: [dict(a) for a in acks] for cid, acks in self._acks.items()}

    @property
    def acks(self) -> Dict[str, List[dict]]:
        """Read-only snapshot of all acknowledgements (see acks_snapshot)."""
        return self.acks_snapshot()

    def clear_acks(self) -> None:
        with self._lock:
            self._acks.clear()

    def public_state(self) -> dict:
        """
        What /api/state serves: the state without ``_``-prefixed keys plus
        ``acks`` for the current and next cue ids.
        """
        with self._lock:
            public = {k: v for k, v in self._state.items() if not k.startswith('_')}
            acks = {}
            for cid in (self._state.get("current_id"), self._state.get("next_id")):
                if cid and cid in self._acks:
                    acks[cid] = [dict(a) for a in self._acks[cid]]
        public["acks"] = acks
        return public

    # ---- app -> monitor --------------------------------------------------

    def update_from_app(self, app_state, position, current_item, current_type,
                        next_item, next_type):
        """
        Convenience method called from the main app's position update.
        Builds the full state dict from app objects.

        ``current_type``/``next_type`` are "marker", "vamp" or "hold" (a
        HoldCue is treated like a marker). Everything new on the
        StateManager (standby(), go_log, live_mode, holds, HELD state) is
        read defensively so an older app_state still works.
        """
        current_dict = _item_to_dict(current_item, current_type)
        next_dict = _item_to_dict(next_item, next_type, position)
        rate = float(_safe_call(app_state, 'get_playback_rate', 1.0)) if app_state else 1.0
        rate = max(0.01, rate)
        countdown = getattr(app_state, 'countdown_to', None)
        if next_dict is not None:
            target = _item_time(next_item, next_type)
            if target is not None:
                next_dict['countdown'] = (countdown(target, position) if callable(countdown)
                                           else max(0.0, target - position) / rate)
        duration = getattr(app_state, 'song_length', 0.0) or 0.0
        end_countdown = (countdown(duration, position) if callable(countdown)
                         else max(0.0, duration - position) / rate)

        if app_state is not None:
            state_obj = getattr(app_state, 'state', None)
            state_name = str(getattr(state_obj, 'name', '') or '').lower() or 'stopped'
            holds = []
            for h in (getattr(app_state, 'holds', None) or []):
                holds.append({
                    "id": getattr(h, 'id', None),
                    "time": getattr(h, 'time', 0.0),
                    "name": getattr(h, 'name', '') or '',
                })
            self.update(
                playback_rate=rate, end_countdown=end_countdown,
                song_name=getattr(app_state, 'current_song_name', '') or '',
                position=position,
                song_duration=getattr(app_state, 'song_length', 0.0) or 0.0,
                is_playing=bool(_safe_call(app_state, 'is_playing', False)),
                is_paused=(state_name == 'paused'),
                is_looping=bool(_safe_call(app_state, 'is_in_loop_mode', False)),
                state=state_name,
                live=bool(getattr(app_state, 'live_mode', False)),
                standby=_standby_to_dict(_safe_call(app_state, 'standby', None)),
                go_log=_go_log_entries(getattr(app_state, 'go_log', None) or []),
                holds=holds,
                current_id=current_dict["id"] if current_dict else None,
                next_id=next_dict["id"] if next_dict else None,
                current=current_dict,
                next=next_dict,
            )
        else:
            self.update(
                playback_rate=1.0, end_countdown=None,
                song_name="",
                position=position,
                song_duration=0.0,
                is_playing=False,
                is_paused=False,
                is_looping=False,
                state="stopped",
                live=False,
                standby={"action": "none", "label": "", "next": None},
                go_log=[],
                holds=[],
                current_id=current_dict["id"] if current_dict else None,
                next_id=next_dict["id"] if next_dict else None,
                current=current_dict,
                next=next_dict,
            )


# =========================================================================
# HTML PAGE (embedded - mobile-first responsive design)
# =========================================================================

# Tag colors as CSS variables
_tag_css = "\n".join(
    f"    .tag-{tag.lower()} {{ background: {color}; }}"
    for tag, color in TAG_COLORS.items()
)

# Department filter chips: "All" plus every tag, with its colour for the chip.
DEPARTMENTS: List[str] = ["All"] + list(TAG_COLORS.keys())
_dept_js = ("const DEPTS = " + json.dumps(DEPARTMENTS) + ";\n"
            "const DEPT_COLORS = " + json.dumps(dict(TAG_COLORS)) + ";")

MONITOR_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Loop Station Monitor</title>
<style>
  *, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }
  /* Flex items default to min-width:auto, which lets one long unbroken
     word (a cue name, a song title) push the whole page wider than the
     phone. Every flex child here may shrink below its content width. */
  .header > *, .cue-card-header > *, .tag-list > *, .dept-row > *,
  .device-bar > *, .ack-row > *, .golog-row > *, .standby > * { min-width: 0; }

  :root {
    --bg: #0d1117;
    --surface: #161b22;
    --surface2: #1c2333;
    --text: #e6edf3;
    --dim: #7d8590;
    --green: #3fb950;
    --yellow: #d29922;
    --red: #f85149;
    --blue: #58a6ff;
  }
  
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    min-height: 100dvh;
    width: 100%;
    overflow-wrap: anywhere;   /* long cue names / notes wrap, never overflow */
  }
  
  /* Header */
  .header {
    background: var(--surface);
    padding: 12px 16px;
    border-bottom: 1px solid #30363d;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    position: sticky;
    top: 0;
    z-index: 10;
  }
  .header-text { flex: 1 1 auto; }
  .header-status { flex: 0 0 auto; white-space: nowrap; font-size: 12px; color: var(--dim); }
  .header h1 { font-size: 14px; color: var(--dim); font-weight: 600; }
  .song-name { font-size: 16px; font-weight: 700; }
  .status-dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: var(--dim);
    display: inline-block;
    margin-right: 6px;
  }
  .status-dot.playing { background: var(--green); animation: pulse 1.5s infinite; }
  .status-dot.looping { background: var(--blue); animation: pulse 0.8s infinite; }
  .status-dot.paused { background: var(--yellow); }
  .status-dot.held { background: var(--red); animation: pulse 1s infinite; }
  .pill {
    display: inline-block;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 1px;
    padding: 2px 7px;
    border-radius: 10px;
    margin-left: 6px;
    vertical-align: middle;
  }
  .pill.live { background: var(--red); color: #fff; animation: pulse 2s infinite; }
  [hidden] { display: none !important; }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }

  /* HELD banner: full-width, impossible to miss */
  .banner {
    text-align: center;
    font-size: 18px;
    font-weight: 800;
    letter-spacing: 1px;
    padding: 12px;
    margin-bottom: 12px;
    border-radius: 10px;
    background: #3a1214;
    color: #ffb3b0;
    border: 1px solid var(--red);
    animation: pulse 1.2s infinite;
  }

  /* Department filter chips */
  .dept-row {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 12px;
  }
  .chip {
    font: inherit;
    font-size: 12px;
    font-weight: 600;
    color: var(--text);
    background: var(--surface);
    border: 1px solid #30363d;
    border-radius: 14px;
    padding: 5px 10px;
    cursor: pointer;
    border-left-width: 4px;
    border-left-color: var(--chip, #30363d);
    -webkit-tap-highlight-color: transparent;
  }
  .chip.on { background: var(--surface2); border-color: var(--blue); border-left-color: var(--chip, var(--blue)); }
  .tag-card.filtered-out { display: none; }
  .no-notes { font-size: 12px; color: var(--dim); font-style: italic; margin-top: 8px; }

  /* STANDBY line: what the next GO will do */
  .standby {
    display: flex;
    align-items: baseline;
    gap: 8px;
    font-size: 13px;
    padding: 8px 12px;
    margin-bottom: 12px;
    background: var(--surface);
    border: 1px solid #30363d;
    border-radius: 10px;
  }
  .standby-key { flex: 0 0 auto; font-size: 11px; letter-spacing: 1px; color: var(--yellow); font-weight: 700; }
  .standby-text { flex: 1 1 auto; }
  .standby.idle .standby-text { color: var(--dim); }

  /* ACK */
  .ack-row { display: flex; align-items: center; gap: 10px; margin-top: 10px; }
  .ack-btn {
    font: inherit;
    font-size: 14px;
    font-weight: 800;
    letter-spacing: 1px;
    color: #fff;
    background: var(--blue);
    border: none;
    border-radius: 8px;
    padding: 10px 18px;
    min-height: 44px;     /* comfortable thumb target */
    cursor: pointer;
    flex: 0 0 auto;
    -webkit-tap-highlight-color: transparent;
  }
  .ack-btn.acked { background: #1f6f3a; }
  .ack-btn:disabled { opacity: 0.6; cursor: default; }
  .ack-count { font-size: 12px; color: var(--dim); flex: 1 1 auto; }

  /* Device name */
  .device-bar {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    font-size: 12px;
    color: var(--dim);
    padding: 8px 12px;
    margin-bottom: 12px;
    background: var(--surface);
    border: 1px solid #30363d;
    border-radius: 10px;
  }
  .device-bar input {
    font: inherit;
    font-size: 14px;
    flex: 1 1 120px;
    min-height: 36px;
    padding: 4px 8px;
    color: var(--text);
    background: var(--bg);
    border: 1px solid #30363d;
    border-radius: 6px;
  }
  .device-bar button, .link-btn {
    font: inherit;
    font-size: 12px;
    color: var(--blue);
    background: none;
    border: 1px solid var(--blue);
    border-radius: 6px;
    padding: 6px 10px;
    cursor: pointer;
  }
  .device-name { color: var(--text); font-weight: 700; }

  /* GO log */
  .golog {
    margin-bottom: 12px;
    background: var(--surface);
    border: 1px solid #30363d;
    border-radius: 10px;
  }
  .golog summary {
    cursor: pointer;
    padding: 10px 12px;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 1px;
    color: var(--dim);
    list-style: none;
  }
  .golog summary::-webkit-details-marker { display: none; }
  .golog summary::before { content: '▸ '; }
  .golog[open] summary::before { content: '▾ '; }
  .golog-list { padding: 0 12px 10px; font-size: 13px; }
  .golog-row { display: flex; gap: 8px; padding: 4px 0; border-top: 1px solid #30363d; }
  .golog-time { flex: 0 0 auto; font-family: 'SF Mono', 'Consolas', monospace; color: var(--dim); }
  .golog-action { flex: 0 0 auto; color: var(--yellow); text-transform: uppercase; font-size: 11px; font-weight: 700; padding-top: 2px; }
  .golog-label { flex: 1 1 auto; }
  .golog-empty { color: var(--dim); font-style: italic; }
  
  /* Main content */
  .content { padding: 12px; max-width: 600px; margin: 0 auto; }
  
  /* Countdown - the star of the show */
  .countdown-section {
    text-align: center;
    padding: 20px 12px;
    margin-bottom: 12px;
    background: var(--surface);
    border-radius: 12px;
    border: 1px solid #30363d;
  }
  .countdown-label { font-size: 11px; text-transform: uppercase; color: var(--dim); letter-spacing: 1px; margin-bottom: 4px; }
  .countdown-value {
    /* 72px on phones >= 400px wide, scales down to ~58px at 320px so
       "--:--" / "12:34" always fit inside the card */
    font-size: clamp(44px, 18vw, 72px);
    font-weight: 800;
    font-variant-numeric: tabular-nums;
    font-family: 'SF Mono', 'Consolas', monospace;
    color: var(--green);
    line-height: 1;
    transition: color 0.3s;
  }
  .countdown-value.warn { color: var(--yellow); }
  .countdown-value.urgent { color: var(--red); }
  .countdown-value.now { color: var(--red); font-size: clamp(36px, 14vw, 56px); }
  .next-name-preview { font-size: 14px; color: var(--dim); margin-top: 6px; }
  
  /* Cue cards */
  .cue-card {
    background: var(--surface);
    border-radius: 10px;
    padding: 14px;
    margin-bottom: 10px;
    border-left: 4px solid transparent;
    border: 1px solid #30363d;
  }
  .cue-card.current { border-left-color: var(--green); background: #0d2818; }
  .cue-card.next { border-left-color: var(--yellow); background: #1c1a0d; }
  
  .cue-card-header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
  }
  .cue-type-icon { font-size: 18px; flex: 0 0 auto; }
  .cue-name { font-size: 18px; font-weight: 700; flex: 1 1 0; }
  .cue-label {
    flex: 0 0 auto;
    white-space: nowrap;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--dim);
    padding: 2px 8px;
    border-radius: 4px;
    background: rgba(255,255,255,0.06);
  }
  .cue-time {
    font-family: 'SF Mono', 'Consolas', monospace;
    font-size: 13px;
    color: var(--dim);
    margin-bottom: 8px;
  }
  
  /* Tags */
  .tag-list { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .tag-card {
    border-radius: 8px;
    padding: 8px 10px;
    font-size: 12px;
    flex: 1 1 100%;
    width: 100%;
    background: var(--surface2);
    border-left: 3px solid var(--dim);
  }
  .tag-badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 700;
    color: #fff;
    margin-bottom: 4px;
  }
  .tag-notes {
    font-size: 13px;
    color: var(--text);
    line-height: 1.4;
    margin-top: 3px;
  }
  .tag-notes.empty { color: var(--dim); font-style: italic; }
  
  /* Tag colors */
""" + _tag_css + """
  
  /* Position bar */
  .position-bar {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
    padding: 8px;
    font-family: 'SF Mono', 'Consolas', monospace;
    font-size: 28px;
    font-weight: 700;
    color: var(--text);
  }
  
  /* No cue state */
  .empty-state {
    text-align: center;
    padding: 40px 20px;
    color: var(--dim);
    font-size: 14px;
  }
  
  /* Connection indicator */
  .connection {
    position: fixed;
    bottom: 8px;
    right: 8px;
    max-width: calc(100% - 16px);
    font-size: 10px;
    color: var(--dim);
    background: var(--surface);
    padding: 4px 8px;
    border-radius: 6px;
    border: 1px solid #30363d;
  }
  .connection.error { color: var(--red); }
</style>
</head>
<body>

<div class="header">
  <div class="header-text">
    <h1>LOOP STATION MONITOR<span class="pill live" id="livePill" hidden>LIVE</span></h1>
    <div class="song-name" id="songName">--</div>
  </div>
  <div class="header-status">
    <span class="status-dot" id="statusDot"></span>
    <span id="statusText">--</span>
  </div>
</div>

<div class="content">
  <!-- HELD BANNER -->
  <div class="banner held" id="heldBanner" hidden>HELD — waiting for GO</div>

  <!-- BIG COUNTDOWN -->
  <div class="countdown-section">
    <div class="countdown-label" id="countdownLabel">NEXT CUE IN</div>
    <div class="countdown-value" id="countdown">--:--</div>
    <div class="next-name-preview" id="nextPreview"></div>
  </div>

  <!-- POSITION -->
  <div class="position-bar" id="positionBar">0:00.00</div>

  <!-- STANDBY: what GO does next -->
  <div class="standby idle" id="standbyLine">
    <span class="standby-key">STANDBY</span>
    <span class="standby-text" id="standbyText">--</span>
  </div>

  <!-- DEPARTMENT FILTER -->
  <div class="dept-row" id="deptRow" role="group" aria-label="Department filter"></div>

  <!-- DEVICE NAME -->
  <div class="device-bar" id="deviceBar">
    <span id="deviceShown" hidden>Device: <span class="device-name" id="deviceName"></span></span>
    <button type="button" class="link-btn" id="deviceChange" hidden>change</button>
    <span id="deviceAsk">Your name / device for ACKs:</span>
    <input type="text" id="deviceInput" maxlength="64" placeholder="e.g. Lighting desk" autocomplete="off">
    <button type="button" id="deviceSave">Save</button>
  </div>

  <!-- CURRENT CUE -->
  <div id="currentCard"></div>

  <!-- NEXT CUE -->
  <div id="nextCard"></div>

  <!-- GO LOG -->
  <details class="golog" id="goLog">
    <summary>GO LOG (<span id="goLogCount">0</span>)</summary>
    <div class="golog-list" id="goLogList"><div class="golog-empty">No GOs yet</div></div>
  </details>
</div>

<div class="connection" id="connStatus">Connecting...</div>

<script>
const API_URL = '/api/state';
const ACK_URL = '/api/ack';
""" + _dept_js + """
let failCount = 0;
let pollInFlight = false;     // Guard against overlapping fetches on slow Wi-Fi

// Escape user-entered text (cue names, tags, notes) before it goes into innerHTML.
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[c]);
}

// Interpolation state — smoothly tick between server polls
let serverCountdown = null;   // Last countdown value from server
let serverPosition = null;    // Last position value from server
let serverDuration = 0;       // Song duration for end-of-song countdown
let serverIsPlaying = false;
let lastPollTime = 0;         // performance.now() when we last got data
let serverPlaybackRate = 1;
let serverEndCountdown = null;
let serverUpdatedAt = null;
let serverUpdateSeenAt = 0;
const STALE_MS = 3000;
const FETCH_TIMEOUT_MS = 2500;
let lastNextName = '';

function markStale(message) {
  serverIsPlaying = false;
  document.getElementById('countdown').textContent = '--:--';
  document.getElementById('countdownLabel').textContent = 'UPDATES STOPPED';
  document.getElementById('statusText').textContent = 'Disconnected';
  document.getElementById('statusDot').className = 'status-dot';
  const conn = document.getElementById('connStatus');
  conn.textContent = message;
  conn.className = 'connection error';
}

// ---- per-device preferences (localStorage; all optional) ----------------
const LS_DEPT = 'ls_dept', LS_DEVICE = 'ls_device', LS_ACKED = 'ls_acked', LS_GOLOG_OPEN = 'ls_golog_open';
function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

let dept = 'All';                 // department filter
let deviceName = '';              // who is pressing ACK
let ackedIds = [];                // cue ids this device acked (newest last)
let lastAcks = {};                // acks for current/next cue ids from the server
let cardSig = { currentCard: '', nextCard: '' };   // re-render cards only on change
let ackInFlight = false;

function initDept() {
  let d = null;
  try { d = new URLSearchParams(location.search).get('dept'); } catch (e) {}
  if (!d) d = lsGet(LS_DEPT);
  const match = d && DEPTS.find(x => x.toLowerCase() === String(d).toLowerCase());
  dept = match || 'All';
  lsSet(LS_DEPT, dept);
  renderDeptChips();
}

function renderDeptChips() {
  const row = document.getElementById('deptRow');
  row.innerHTML = DEPTS.map(d => {
    const color = DEPT_COLORS[d] || '';
    return `<button type="button" class="chip${d === dept ? ' on' : ''}" data-dept="${esc(d)}"` +
           (color ? ` style="--chip: ${esc(color)}"` : '') + `>${esc(d)}</button>`;
  }).join('');
}

document.getElementById('deptRow').addEventListener('click', ev => {
  const btn = ev.target.closest('.chip');
  if (!btn) return;
  dept = btn.dataset.dept || 'All';
  lsSet(LS_DEPT, dept);
  renderDeptChips();
  rerenderCards();
});

function initDevice() {
  deviceName = (lsGet(LS_DEVICE) || '').trim();
  try { ackedIds = JSON.parse(lsGet(LS_ACKED) || '[]'); } catch (e) { ackedIds = []; }
  if (!Array.isArray(ackedIds)) ackedIds = [];
  renderDeviceBar();
}

function renderDeviceBar() {
  const has = !!deviceName;
  document.getElementById('deviceShown').hidden = !has;
  document.getElementById('deviceChange').hidden = !has;
  document.getElementById('deviceAsk').hidden = has;
  document.getElementById('deviceInput').hidden = has;
  document.getElementById('deviceSave').hidden = has;
  document.getElementById('deviceName').textContent = deviceName;
}

function saveDevice() {
  const v = document.getElementById('deviceInput').value.trim().slice(0, 64);
  if (!v) { document.getElementById('deviceInput').focus(); return; }
  deviceName = v;
  lsSet(LS_DEVICE, v);
  renderDeviceBar();
  rerenderCards();
}
document.getElementById('deviceSave').addEventListener('click', saveDevice);
document.getElementById('deviceInput').addEventListener('keydown', ev => { if (ev.key === 'Enter') saveDevice(); });
document.getElementById('deviceChange').addEventListener('click', () => {
  document.getElementById('deviceInput').value = deviceName;
  deviceName = '';
  renderDeviceBar();
  document.getElementById('deviceInput').focus();
});

function isAcked(cueId) {
  if (!cueId) return false;
  if (ackedIds.indexOf(cueId) >= 0) return true;
  const list = lastAcks[cueId] || [];
  return !!deviceName && list.some(a => a.device === deviceName);
}

async function sendAck(cueId) {
  if (!cueId || ackInFlight) return;
  if (!deviceName) {
    document.getElementById('deviceInput').focus();
    document.getElementById('deviceBar').scrollIntoView({ block: 'center' });
    return;
  }
  ackInFlight = true;
  try {
    const res = await fetch(ACK_URL, {
      method: 'POST', cache: 'no-store', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device: deviceName, cue_id: cueId }),
    });
    if (res.status === 403) throw new Error('Not authorised - rescan the QR code or reopen the share link');
    if (!res.ok && res.status !== 204) throw new Error('HTTP ' + res.status);
    ackedIds = ackedIds.filter(x => x !== cueId).concat([cueId]).slice(-50);
    lsSet(LS_ACKED, JSON.stringify(ackedIds));
    // optimistic count until the next poll confirms
    const list = (lastAcks[cueId] || []).filter(a => a.device !== deviceName);
    list.push({ device: deviceName, ts: Date.now() / 1000 });
    lastAcks[cueId] = list;
  } catch (e) {
    const conn = document.getElementById('connStatus');
    conn.textContent = 'ACK failed: ' + (e && e.message ? e.message : e);
    conn.className = 'connection error';
  } finally {
    ackInFlight = false;
    rerenderCards();
  }
}

document.querySelector('.content').addEventListener('click', ev => {
  const btn = ev.target.closest('.ack-btn');
  if (btn) sendAck(btn.dataset.cue);
});

function formatTime(s) {
  if (s == null) return '--';
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return m + ':' + sec.toFixed(2).padStart(5, '0');
}

function formatCountdown(s) {
  if (s == null || s < 0) return '--:--';
  if (s <= 0.5) return 'NOW';
  if (s < 60) return s.toFixed(1) + 's';
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return m + ':' + String(sec).padStart(2, '0');
}

function renderTagNotes(tagNotes) {
  if (!tagNotes || Object.keys(tagNotes).length === 0) return '';
  const want = dept.toLowerCase();
  let html = '<div class="tag-list">';
  let shown = 0;
  for (const [tag, notes] of Object.entries(tagNotes)) {
    const cls = esc('tag-' + String(tag).toLowerCase());
    const hide = dept !== 'All' && String(tag).toLowerCase() !== want;
    if (!hide) shown++;
    const color = DEPT_COLORS[tag] || '#555';
    html += `<div class="tag-card${hide ? ' filtered-out' : ''}" style="border-left-color: ${esc(color)}">
      <span class="tag-badge ${cls}">${esc(tag)}</span>
      <div class="tag-notes ${notes ? '' : 'empty'}">${notes ? esc(notes) : '(no notes)'}</div>
    </div>`;
  }
  html += '</div>';
  if (shown === 0) html += `<div class="no-notes">No ${esc(dept)} notes on this cue</div>`;
  return html;
}

function renderAckRow(item) {
  if (!item || !item.id) return '';
  const id = String(item.id);
  const acks = lastAcks[id] || [];
  const acked = isAcked(id);
  const n = acks.length;
  const who = acks.map(a => a.device).join(', ');
  const count = n === 0 ? 'no acks yet' : (n + (n === 1 ? ' ack' : ' acks') + (who ? ': ' + who : ''));
  return `<div class="ack-row">
    <button type="button" class="ack-btn${acked ? ' acked' : ''}" data-cue="${esc(id)}">${acked ? 'Acked ✓' : 'ACK'}</button>
    <span class="ack-count">${esc(count)}</span>
  </div>`;
}

function renderCueCard(item, label, cssClass) {
  if (!item) return '';
  const icon = item.type === 'vamp' ? '🔁' : item.type === 'hold' ? '⏸' : '📍';
  const typeLabel = item.type === 'vamp' ? 'Vamp' : item.type === 'hold' ? 'Hold' : 'Cue';
  let timeStr = '';
  if (item.type === 'vamp') {
    timeStr = formatTime(item.start) + ' → ' + formatTime(item.end);
  } else {
    timeStr = 'at ' + formatTime(item.time);
  }

  return `<div class="cue-card ${cssClass}">
    <div class="cue-card-header">
      <span class="cue-type-icon">${icon}</span>
      <span class="cue-name">${esc(item.name)}</span>
      <span class="cue-label">${esc(label)}</span>
    </div>
    <div class="cue-time">${typeLabel} · ${esc(timeStr)}</div>
    ${renderTagNotes(item.tag_notes)}
    ${renderAckRow(item)}
  </div>`;
}

let lastCurrent = null, lastNext = null;

// Rewrite a card only when its content changed, so a tap on ACK is never
// swallowed by the 500 ms poll replacing the button under the finger.
function renderCard(elId, item, label, cssClass) {
  const html = item ? renderCueCard(item, label, cssClass) : '';
  const sig = html;
  if (cardSig[elId] === sig) return;
  cardSig[elId] = sig;
  document.getElementById(elId).innerHTML = html;
}

function rerenderCards() {
  renderCard('currentCard', lastCurrent, 'NOW', 'current');
  renderCard('nextCard', lastNext, 'UP NEXT', 'next');
}

const ACTION_TEXT = {
  play: 'GO will PLAY',
  release_hold: 'GO will RELEASE the hold',
  exit_vamp: 'GO will EXIT the vamp',
  none: 'Nothing armed',
};

function renderStandby(sb) {
  const line = document.getElementById('standbyLine');
  const text = document.getElementById('standbyText');
  sb = sb || { action: 'none', label: '', next: null };
  const action = sb.action || 'none';
  let s = ACTION_TEXT[action] || ('GO will ' + String(action).replace(/_/g, ' ').toUpperCase());
  if (sb.label) s += ' · ' + sb.label;
  if (sb.next && sb.next.name && (!sb.label || sb.label.indexOf(sb.next.name) < 0)) s += ' → ' + sb.next.name;
  text.textContent = s;
  line.className = 'standby' + (action === 'none' ? ' idle' : '');
}

function formatClock(ts) {
  if (ts == null) return '--:--:--';
  const d = new Date(Number(ts) * 1000);
  if (isNaN(d.getTime())) return '--:--:--';
  const p = n => String(n).padStart(2, '0');
  return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

let goLogSig = '';
function renderGoLog(entries) {
  entries = Array.isArray(entries) ? entries : [];
  const sig = JSON.stringify(entries);
  if (sig === goLogSig) return;
  goLogSig = sig;
  document.getElementById('goLogCount').textContent = entries.length;
  const list = document.getElementById('goLogList');
  if (entries.length === 0) {
    list.innerHTML = '<div class="golog-empty">No GOs yet</div>';
    return;
  }
  list.innerHTML = entries.slice().reverse().map(e => `<div class="golog-row">
      <span class="golog-time">${esc(formatClock(e.ts))}</span>
      <span class="golog-action">${esc(String(e.action || '').replace(/_/g, ' '))}</span>
      <span class="golog-label">${esc(e.label || '')}${e.position != null ? ' <span class="golog-time">@ ' + esc(formatTime(e.position)) + '</span>' : ''}</span>
    </div>`).join('');
}

const goLogEl = document.getElementById('goLog');
goLogEl.open = lsGet(LS_GOLOG_OPEN) === '1';
goLogEl.addEventListener('toggle', () => lsSet(LS_GOLOG_OPEN, goLogEl.open ? '1' : '0'));

// Smooth animation loop (runs at 60fps via requestAnimationFrame)
function animateCountdown() {
  const cdEl = document.getElementById('countdown');
  const posEl = document.getElementById('positionBar');
  
  if (serverIsPlaying) {
    if (performance.now() - lastPollTime > STALE_MS) {
      markStale('Updates stopped — waiting for connection');
      requestAnimationFrame(animateCountdown);
      return;
    }
    const elapsed = (performance.now() - lastPollTime) / 1000;
    const smoothPos = serverPosition + elapsed * serverPlaybackRate;
    posEl.textContent = formatTime(smoothPos);
    
    if (serverCountdown != null) {
      // Countdown to next cue
      const smoothCd = Math.max(0, serverCountdown - elapsed);
      cdEl.textContent = formatCountdown(smoothCd);
      cdEl.className = 'countdown-value' + (smoothCd <= 0.5 ? ' now' : smoothCd < 5 ? ' urgent' : smoothCd < 15 ? ' warn' : '');
    } else if (serverEndCountdown != null) {
      // No next cue — countdown to song end
      const remaining = Math.max(0, serverEndCountdown - elapsed);
      cdEl.textContent = formatCountdown(remaining);
      cdEl.className = 'countdown-value' + (remaining <= 0.5 ? ' now' : remaining < 5 ? ' urgent' : remaining < 15 ? ' warn' : '');
    }
  }
  
  requestAnimationFrame(animateCountdown);
}

async function poll() {
  if (pollInFlight) return;   // previous request still running; skip this tick
  pollInFlight = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(API_URL, { cache: 'no-store', credentials: 'same-origin', signal: controller.signal });
    if (res.status === 403) {
      throw new Error('Not authorised - rescan the QR code or reopen the share link');
    }
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const now = performance.now();
    if (data.updated_at !== serverUpdatedAt) {
      serverUpdatedAt = data.updated_at;
      serverUpdateSeenAt = now;
    } else if (data.state === 'playing' && now - serverUpdateSeenAt > STALE_MS) {
      throw new Error('Desktop updates stopped');
    }
    failCount = 0;
    
    // Song name
    document.getElementById('songName').textContent = 
      data.song_name ? data.song_name.replace(/\\.[^.]+$/, '') : '--';
    
    // Status
    const dot = document.getElementById('statusDot');
    const statusText = document.getElementById('statusText');
    const held = data.state === 'held';
    const paused = data.state === 'paused' || data.is_paused;
    const stopped = data.state === 'stopped';
    serverIsPlaying = !held && !paused && !stopped && Boolean(data.is_playing);
    serverPlaybackRate = Math.max(0.01, Number(data.playback_rate) || 1);
    serverEndCountdown = data.end_countdown == null ? null : Number(data.end_countdown);
    serverDuration = data.song_duration || 0;
    if (held) {
      dot.className = 'status-dot held';
      statusText.textContent = 'Held';
    } else if (paused) {
      dot.className = 'status-dot paused';
      statusText.textContent = 'Paused';
    } else if (data.is_looping && serverIsPlaying) {
      dot.className = 'status-dot looping';
      statusText.textContent = 'Looping';
    } else if (data.is_playing) {
      dot.className = 'status-dot playing';
      statusText.textContent = 'Playing';
    } else if (data.is_paused) {
      dot.className = 'status-dot paused';
      statusText.textContent = 'Paused';
    } else {
      dot.className = 'status-dot';
      statusText.textContent = 'Stopped';
    }
    document.getElementById('heldBanner').hidden = !held;
    document.getElementById('livePill').hidden = !data.live;
    renderStandby(data.standby);
    renderGoLog(data.go_log);
    
    // Store server values for interpolation
    serverPosition = data.position;
    lastPollTime = performance.now();
    
    // Position (will be overridden by animation loop when playing)
    if (!serverIsPlaying) {
      document.getElementById('positionBar').textContent = formatTime(data.position);
    }
    
    // Countdown — store for interpolation
    const cdEl = document.getElementById('countdown');
    const cdLabel = document.getElementById('countdownLabel');
    const nextPreview = document.getElementById('nextPreview');
    if (data.next) {
      serverCountdown = data.next.countdown;
      cdLabel.textContent = 'NEXT CUE IN';
      cdEl.textContent = serverCountdown == null ? 'Waiting for release' : formatCountdown(serverCountdown);
      // Update display immediately (animation loop takes over between polls)
      if (!serverIsPlaying) {
        cdEl.textContent = serverCountdown == null ? 'Waiting for release' : formatCountdown(serverCountdown);
        const cd = serverCountdown;
        cdEl.className = 'countdown-value' + (cd <= 0.5 ? ' now' : cd < 5 ? ' urgent' : cd < 15 ? ' warn' : '');
      }
      nextPreview.textContent = data.next.name;
      lastNextName = data.next.name;
    } else {
      serverCountdown = null;
      cdLabel.textContent = 'SONG ENDS IN';
      nextPreview.textContent = '';
      // Show song-end countdown even on poll (animation loop handles smooth updates)
      if (serverEndCountdown != null) {
        const remaining = Math.max(0, serverEndCountdown);
        cdEl.textContent = formatCountdown(remaining);
        cdEl.className = 'countdown-value' + (remaining <= 0.5 ? ' now' : remaining < 5 ? ' urgent' : remaining < 15 ? ' warn' : '');
      } else {
        cdEl.textContent = '--:--';
        cdEl.className = 'countdown-value';
      }
    }
    
    // Current / next cards (+ ACK state); only rewritten when changed
    lastAcks = (data.acks && typeof data.acks === 'object') ? data.acks : {};
    lastCurrent = data.current || null;
    lastNext = data.next || null;
    rerenderCards();

    // Connection
    document.getElementById('connStatus').textContent = 'Connected';
    document.getElementById('connStatus').className = 'connection';
    
  } catch (e) {
    failCount++;
    markStale((e && e.message && e.message.indexOf('authorised') >= 0)
      ? e.message
      : 'Updates stopped — ' + (e && e.message ? e.message : 'connection lost'));
  } finally {
    clearTimeout(timeout);
    pollInFlight = false;
  }
}

initDept();
initDevice();

// Poll server every 500ms for data updates
setInterval(poll, 500);
poll();

// Start smooth animation loop (60fps countdown/position)
requestAnimationFrame(animateCountdown);

// Keep screen awake (for mobile)
if ('wakeLock' in navigator) {
  navigator.wakeLock.request('screen').catch(() => {});
}
</script>
</body>
</html>"""


# =========================================================================
# FLASK APP
# =========================================================================

def make_qr_png(url: str) -> bytes:
    """Render ``url`` as a PNG QR code. Raises RuntimeError if qrcode is missing."""
    if not HAS_QRCODE:
        raise RuntimeError("QR code library not installed (pip install qrcode pillow)")
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="white", back_color="#0d1117")
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


_FORBIDDEN_TEXT = (
    "Forbidden: this cue monitor needs the share link from the Loop Station "
    "operator. Scan the QR code or open the full link (including ?k=...) again."
)


def create_flask_app(shared_state: SharedCueState, token: Optional[str] = None,
                     share_url: str = "") -> Flask:
    """
    Create and configure the Flask app.

    token:     secret every request must present, either as ``?k=<token>``
               or via the cookie set on the first tokenised hit. Generated
               if not supplied; read it back from ``app.config['CUE_TOKEN']``.
    share_url: the full URL (with token) that /qr.png encodes. Computed once
               by CueWebServer.start() so the route never re-detects the IP.
    """
    if not token:
        token = secrets.token_urlsafe(16)

    app = Flask(__name__)
    app.config['CUE_TOKEN'] = token
    app.config['SHARE_URL'] = share_url
    # The only body we accept is a tiny ack JSON; refuse anything bigger.
    app.config['MAX_CONTENT_LENGTH'] = MAX_REQUEST_BYTES
    app.logger.setLevel(logging.WARNING)  # Suppress request logs

    # Suppress werkzeug logs
    wlog = logging.getLogger('werkzeug')
    wlog.setLevel(logging.ERROR)

    @app.before_request
    def _require_token():
        expected = app.config['CUE_TOKEN']
        query_token = request.args.get(TOKEN_PARAM, "")
        cookie_token = request.cookies.get(TOKEN_COOKIE, "")
        g.set_token_cookie = False
        if query_token and secrets.compare_digest(query_token, expected):
            # First hit from the QR/share link: remember it so the page's
            # /api/state polls (which carry no query string) keep working.
            g.set_token_cookie = True
            return None
        if cookie_token and secrets.compare_digest(cookie_token, expected):
            return None
        abort(403)

    @app.after_request
    def _finish(resp):
        if getattr(g, 'set_token_cookie', False):
            resp.set_cookie(
                TOKEN_COOKIE, app.config['CUE_TOKEN'],
                max_age=TOKEN_COOKIE_MAX_AGE, httponly=True, samesite='Lax',
            )
        # The share link carries the secret; never let proxies/caches keep it.
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    @app.errorhandler(403)
    def _forbidden(_err):
        return Response(_FORBIDDEN_TEXT, status=403, mimetype='text/plain')

    @app.route('/')
    def index():
        return Response(MONITOR_HTML, mimetype='text/html')

    @app.route('/api/state')
    def api_state():
        # Underscore-prefixed keys are internal bookkeeping, not for clients;
        # acks are included only for the current and next cue ids.
        return jsonify(shared_state.public_state())

    @app.route('/api/ack', methods=['POST'])
    def api_ack():
        """A backstage device acknowledges a cue: {device, cue_id} -> 204."""
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return Response("expected a JSON object", status=400, mimetype='text/plain')
        device = body.get('device')
        cue_id = body.get('cue_id')
        if not isinstance(device, str) or not isinstance(cue_id, str):
            return Response("device and cue_id must be strings", status=400,
                            mimetype='text/plain')
        device = " ".join(device.split())   # collapse whitespace / newlines
        cue_id = cue_id.strip()
        if (not device or not cue_id or len(device) > ACK_DEVICE_MAX
                or len(cue_id) > ACK_CUE_ID_MAX):
            return Response("device and cue_id must be non-empty and short",
                            status=400, mimetype='text/plain')
        shared_state.add_ack(cue_id, device)
        return Response(status=204)

    @app.route('/qr.png')
    def qr_code():
        url = app.config.get('SHARE_URL') or request.url_root
        try:
            png = make_qr_png(url)
        except RuntimeError as e:
            return Response(str(e), status=404, mimetype='text/plain')
        return Response(png, mimetype='image/png')

    return app


# =========================================================================
# SERVER MANAGER
# =========================================================================

class CueWebServer:
    """
    Manages the Flask web server lifecycle.
    
    Usage:
        shared = SharedCueState()
        server = CueWebServer(shared)
        url = server.start()  # Non-blocking, runs in thread; url has ?k=token
        server.get_urls()     # same URL for every candidate LAN address
        ...
        server.stop()         # closes the socket and joins the thread
    """
    
    def __init__(self, shared_state: SharedCueState, port: int = 8080):
        self.shared_state = shared_state
        self.port = port
        self._thread: Optional[threading.Thread] = None
        self._server = None
        self._lock = threading.Lock()
        self.running = False
        self.url = ""          # best share URL, includes the access token
        self.urls: List[str] = []   # one share URL per candidate LAN address
        self.ip = ""           # address used in ``url``
        self.ips: List[str] = []    # all candidate LAN addresses, best first
        self.token = ""        # access token for the current session

    def start(self) -> str:
        """
        Start the web server. Returns the share URL (with access token).

        Raises WebServerStartError if the listener cannot be bound, e.g.
        because the port is already in use. A failed start leaves the
        server stopped; it is safe to call start() again later.
        """
        with self._lock:
            if self.running:
                return self.url

            # Detect the LAN address once; /qr.png reuses this URL.
            self.ips = get_local_ips() or ["127.0.0.1"]
            self.ip = self.ips[0]
            self.token = secrets.token_urlsafe(16)

            app = create_flask_app(self.shared_state, token=self.token)

            # Use werkzeug's make_server for clean shutdown. Binding is the
            # only thing that can fail at start; everything after is in-process.
            # NOTE: werkzeug prints the bind error and calls sys.exit(1)
            # instead of raising OSError, so catch SystemExit too and keep
            # its stderr chatter out of the console.
            from werkzeug.serving import make_server
            captured = io.StringIO()
            try:
                with contextlib.redirect_stderr(captured):
                    server = make_server('0.0.0.0', self.port, app, threaded=True)
            except (OSError, SystemExit) as e:
                msg = self._describe_bind_failure(e, captured.getvalue())
                logger.error(msg)
                raise WebServerStartError(msg) from e

            self._server = server
            self.port = server.server_port   # resolves port=0 to the real port
            self.urls = [self._share_url(ip) for ip in self.ips]
            self.url = self.urls[0]
            app.config['SHARE_URL'] = self.url

            def _run():
                logger.info(f"Web server starting on {self.url}")
                try:
                    server.serve_forever()
                except Exception:
                    logger.exception("Web server crashed")
                finally:
                    self.running = False
                    logger.info("Web server stopped")

            # Set running before the thread starts so the flag can never be
            # clobbered by _run's finally racing with this assignment.
            self.running = True
            self._thread = threading.Thread(
                target=_run, name="LoopStation-WebServer", daemon=True)
            self._thread.start()

            logger.info(f"Cue monitor available at: {self.url}")
            return self.url

    def stop(self, timeout: float = 5.0):
        """Stop the web server, release the port, and wait for the thread."""
        with self._lock:
            server, thread = self._server, self._thread
            self._server, self._thread = None, None
            self.running = False
            if server is None:
                return
            logger.info("Web server shutdown requested")
            try:
                server.shutdown()          # stop serve_forever() loop
            except Exception:
                logger.exception("Error while shutting down web server")
            try:
                server.server_close()      # close the listening socket
            except Exception:
                logger.exception("Error while closing web server socket")
            if thread is not None and thread.is_alive():
                thread.join(timeout)
                if thread.is_alive():
                    logger.warning("Web server thread did not exit within "
                                   f"{timeout}s")

    def get_url(self) -> str:
        """Best share URL (with token), or "" when not running."""
        return self.url if self.running else ""

    def get_urls(self) -> List[str]:
        """All share URLs (one per candidate LAN address), best first."""
        return list(self.urls) if self.running else []

    def get_ips(self) -> List[str]:
        """All candidate LAN addresses, best first."""
        return list(self.ips) if self.running else []

    def get_qr_png(self, url: Optional[str] = None) -> bytes:
        """PNG bytes of a QR code for the share URL (or any given URL)."""
        return make_qr_png(url or self.url)

    def _share_url(self, ip: str) -> str:
        return f"http://{ip}:{self.port}/?{TOKEN_PARAM}={self.token}"

    def _describe_bind_failure(self, exc: BaseException, stderr_text: str) -> str:
        """Human-readable reason make_server() could not bind self.port."""
        err: Optional[OSError] = exc if isinstance(exc, OSError) else None
        if err is None:
            # werkzeug swallowed the OSError; probe the port to classify it.
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind(('0.0.0.0', self.port))
            except OSError as probe_err:
                err = probe_err
            finally:
                probe.close()
        if err is not None:
            in_use = (err.errno in (errno.EADDRINUSE, errno.EACCES)
                      or getattr(err, 'winerror', None) in (10048, 10013))
            if in_use:
                return (f"Port {self.port} is already in use (is another Loop "
                        f"Station window sharing, or another app on that port?)")
            return f"Could not open port {self.port}: {err}"
        detail = stderr_text.strip().splitlines()
        return (f"Could not open port {self.port}"
                + (f": {detail[0]}" if detail else ""))
