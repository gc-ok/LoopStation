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
        http://<your-local-ip>:<port>

Dependencies:
    pip install flask qrcode pillow
"""

import io
import json
import socket
import logging
import threading
from typing import Optional, Dict, Any

logger = logging.getLogger("LoopStation.WebServer")

from flask import Flask, jsonify, Response

try:
    import qrcode
    HAS_QRCODE = True
except ImportError:
    HAS_QRCODE = False

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import TAG_COLORS, AVAILABLE_TAGS


def get_local_ip():
    """Get the machine's local network IP address."""
    try:
        # Connect to a public DNS to determine local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# =========================================================================
# SHARED STATE (updated by main app, read by web server)
# =========================================================================

class SharedCueState:
    """Thread-safe shared state between the main app and web server."""
    
    def __init__(self):
        self._lock = threading.Lock()
        self._state: Dict[str, Any] = {
            "song_name": "",
            "position": 0.0,
            "is_playing": False,
            "is_looping": False,
            "current": None,   # {name, type, time, tag_notes}
            "next": None,      # {name, type, time, tag_notes, countdown}
        }
    
    def update(self, **kwargs):
        with self._lock:
            self._state.update(kwargs)
    
    def get_state(self) -> dict:
        with self._lock:
            return dict(self._state)
    
    def update_from_app(self, app_state, position, current_item, current_type,
                        next_item, next_type):
        """
        Convenience method called from the main app's position update.
        Builds the full state dict from app objects.
        """
        current_dict = None
        if current_item:
            if current_type == 'marker':
                current_dict = {
                    "name": current_item.name,
                    "type": "cue",
                    "time": current_item.time,
                    "tag_notes": dict(current_item.tag_notes),
                }
            else:
                current_dict = {
                    "name": current_item.name,
                    "type": "vamp",
                    "start": current_item.start,
                    "end": current_item.end,
                    "tag_notes": dict(current_item.tag_notes),
                }
        
        next_dict = None
        if next_item:
            if next_type == 'marker':
                next_time = next_item.time
                next_dict = {
                    "name": next_item.name,
                    "type": "cue",
                    "time": next_time,
                    "countdown": max(0, next_time - position),
                    "tag_notes": dict(next_item.tag_notes),
                }
            else:
                next_time = next_item.start
                next_dict = {
                    "name": next_item.name,
                    "type": "vamp",
                    "start": next_item.start,
                    "end": next_item.end,
                    "countdown": max(0, next_time - position),
                    "tag_notes": dict(next_item.tag_notes),
                }
        
        self.update(
            song_name=app_state.current_song_name if app_state else "",
            position=position,
            is_playing=app_state.is_playing() if app_state else False,
            is_looping=app_state.is_in_loop_mode() if app_state else False,
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

MONITOR_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Loop Station Monitor</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  
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
    overflow-x: hidden;
  }
  
  /* Header */
  .header {
    background: var(--surface);
    padding: 12px 16px;
    border-bottom: 1px solid #30363d;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    z-index: 10;
  }
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
  
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }
  
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
    font-size: 72px;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
    font-family: 'SF Mono', 'Consolas', monospace;
    color: var(--green);
    line-height: 1;
    transition: color 0.3s;
  }
  .countdown-value.warn { color: var(--yellow); }
  .countdown-value.urgent { color: var(--red); }
  .countdown-value.now { color: var(--red); font-size: 56px; }
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
  .cue-type-icon { font-size: 18px; }
  .cue-name { font-size: 18px; font-weight: 700; flex: 1; }
  .cue-label {
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
  <div>
    <h1>LOOP STATION MONITOR</h1>
    <div class="song-name" id="songName">--</div>
  </div>
  <div>
    <span class="status-dot" id="statusDot"></span>
    <span id="statusText" style="font-size:12px;color:var(--dim)">--</span>
  </div>
</div>

<div class="content">
  <!-- BIG COUNTDOWN -->
  <div class="countdown-section">
    <div class="countdown-label">NEXT CUE IN</div>
    <div class="countdown-value" id="countdown">--:--</div>
    <div class="next-name-preview" id="nextPreview"></div>
  </div>
  
  <!-- POSITION -->
  <div class="position-bar" id="positionBar">0:00.00</div>
  
  <!-- CURRENT CUE -->
  <div id="currentCard"></div>
  
  <!-- NEXT CUE -->
  <div id="nextCard"></div>
</div>

<div class="connection" id="connStatus">Connecting...</div>

<script>
const API_URL = '/api/state';
let pollInterval = null;
let failCount = 0;

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
  let html = '<div class="tag-list">';
  for (const [tag, notes] of Object.entries(tagNotes)) {
    const cls = 'tag-' + tag.toLowerCase();
    const borderColor = getComputedStyle(document.documentElement)
      .getPropertyValue('--tag-color') || '';
    html += `<div class="tag-card" style="border-left-color: var(--${cls}-color, #555)">
      <span class="tag-badge ${cls}">${tag}</span>
      <div class="tag-notes ${notes ? '' : 'empty'}">${notes || '(no notes)'}</div>
    </div>`;
  }
  html += '</div>';
  return html;
}

function renderCueCard(item, label, cssClass) {
  if (!item) return '';
  const icon = item.type === 'vamp' ? '🔁' : '📍';
  const typeLabel = item.type === 'vamp' ? 'Vamp' : 'Cue';
  let timeStr = '';
  if (item.type === 'vamp') {
    timeStr = formatTime(item.start) + ' → ' + formatTime(item.end);
  } else {
    timeStr = 'at ' + formatTime(item.time);
  }
  
  return `<div class="cue-card ${cssClass}">
    <div class="cue-card-header">
      <span class="cue-type-icon">${icon}</span>
      <span class="cue-name">${item.name}</span>
      <span class="cue-label">${label}</span>
    </div>
    <div class="cue-time">${typeLabel} · ${timeStr}</div>
    ${renderTagNotes(item.tag_notes)}
  </div>`;
}

async function poll() {
  try {
    const res = await fetch(API_URL);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    failCount = 0;
    
    // Song name
    document.getElementById('songName').textContent = 
      data.song_name ? data.song_name.replace(/\\.[^.]+$/, '') : '--';
    
    // Status
    const dot = document.getElementById('statusDot');
    const statusText = document.getElementById('statusText');
    if (data.is_looping) {
      dot.className = 'status-dot looping';
      statusText.textContent = 'Looping';
    } else if (data.is_playing) {
      dot.className = 'status-dot playing';
      statusText.textContent = 'Playing';
    } else {
      dot.className = 'status-dot';
      statusText.textContent = 'Stopped';
    }
    
    // Position
    document.getElementById('positionBar').textContent = formatTime(data.position);
    
    // Countdown
    const cdEl = document.getElementById('countdown');
    const nextPreview = document.getElementById('nextPreview');
    if (data.next) {
      const cd = data.next.countdown;
      cdEl.textContent = formatCountdown(cd);
      cdEl.className = 'countdown-value' + (cd < 5 ? ' urgent' : cd < 15 ? ' warn' : '');
      if (cd <= 0.5) cdEl.className = 'countdown-value now';
      nextPreview.textContent = data.next.name;
    } else {
      cdEl.textContent = '--:--';
      cdEl.className = 'countdown-value';
      nextPreview.textContent = 'End of song';
    }
    
    // Current card
    document.getElementById('currentCard').innerHTML = 
      data.current ? renderCueCard(data.current, 'NOW', 'current') : '';
    
    // Next card
    document.getElementById('nextCard').innerHTML = 
      data.next ? renderCueCard(data.next, 'UP NEXT', 'next') : '';
    
    // Connection
    document.getElementById('connStatus').textContent = 'Connected';
    document.getElementById('connStatus').className = 'connection';
    
  } catch (e) {
    failCount++;
    const conn = document.getElementById('connStatus');
    conn.textContent = 'Connection lost (' + failCount + ')';
    conn.className = 'connection error';
  }
}

// Start polling at 500ms
pollInterval = setInterval(poll, 500);
poll();

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

def create_flask_app(shared_state: SharedCueState):
    """Create and configure the Flask app."""
    app = Flask(__name__)
    app.logger.setLevel(logging.WARNING)  # Suppress request logs
    
    # Suppress werkzeug logs
    wlog = logging.getLogger('werkzeug')
    wlog.setLevel(logging.ERROR)
    
    @app.route('/')
    def index():
        return Response(MONITOR_HTML, mimetype='text/html')
    
    @app.route('/api/state')
    def api_state():
        return jsonify(shared_state.get_state())
    
    @app.route('/qr.png')
    def qr_code():
        if not HAS_QRCODE:
            return Response("QR code library not installed", status=404)
        
        ip = get_local_ip()
        port = shared_state.get_state().get('_port', 8080)
        url = f"http://{ip}:{port}"
        
        qr = qrcode.QRCode(version=1, box_size=8, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="white", back_color="#0d1117")
        
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return Response(buf.getvalue(), mimetype='image/png')
    
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
        server.start()        # Non-blocking, runs in thread
        ...
        server.stop()
    """
    
    def __init__(self, shared_state: SharedCueState, port: int = 8080):
        self.shared_state = shared_state
        self.port = port
        self._thread: Optional[threading.Thread] = None
        self._server = None
        self.running = False
        self.url = ""
    
    def start(self) -> str:
        """Start the web server. Returns the URL."""
        if self.running:
            return self.url
        
        ip = get_local_ip()
        self.url = f"http://{ip}:{self.port}"
        self.shared_state.update(_port=self.port)
        
        app = create_flask_app(self.shared_state)
        
        # Use werkzeug's make_server for clean shutdown
        from werkzeug.serving import make_server
        self._server = make_server('0.0.0.0', self.port, app, threaded=True)
        
        def _run():
            logger.info(f"Web server starting on {self.url}")
            try:
                self._server.serve_forever()
            except Exception as e:
                logger.error(f"Web server error: {e}")
            finally:
                self.running = False
                logger.info("Web server stopped")
        
        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        self.running = True
        
        logger.info(f"Cue monitor available at: {self.url}")
        return self.url
    
    def stop(self):
        """Stop the web server."""
        if self._server:
            self._server.shutdown()
            self._server = None
        self.running = False
        logger.info("Web server shutdown requested")
    
    def get_url(self) -> str:
        return self.url if self.running else ""
