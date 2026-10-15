"""Tests for backend/web_server.py (LAN cue monitor)."""
import socket
import urllib.request
import urllib.error

import pytest

from backend import web_server
from backend.web_server import (
    CueWebServer, SharedCueState, WebServerStartError, create_flask_app,
    get_local_ip, get_local_ips, TOKEN_COOKIE,
)

TOKEN = "test-token-123"


@pytest.fixture
def state():
    s = SharedCueState()
    s.update(song_name="show.wav", _port=8080,
             current={"name": "<b>Cue</b>", "type": "cue", "time": 1.0,
                      "tag_notes": {"Lights": "<script>alert(1)</script>"}})
    return s


@pytest.fixture
def client(state):
    app = create_flask_app(state, token=TOKEN, share_url="http://1.2.3.4:8080/?k=" + TOKEN)
    app.testing = True
    return app.test_client()


# ---------------------------------------------------------------- auth ----

def test_no_token_is_forbidden(client):
    for path in ("/", "/api/state", "/qr.png"):
        resp = client.get(path)
        assert resp.status_code == 403, path
        assert b"share link" in resp.data
        assert TOKEN_COOKIE not in resp.headers.get("Set-Cookie", "")


def test_wrong_token_is_forbidden(client):
    assert client.get("/?k=nope").status_code == 403
    client.set_cookie(TOKEN_COOKIE, "nope")
    assert client.get("/api/state").status_code == 403


def test_token_in_query_sets_cookie(client):
    resp = client.get("/?k=" + TOKEN)
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert TOKEN_COOKIE + "=" + TOKEN in set_cookie
    assert "HttpOnly" in set_cookie
    assert resp.headers["Cache-Control"] == "no-store"


def test_cookie_allows_api_state(client):
    client.get("/?k=" + TOKEN)          # first hit stores the cookie
    resp = client.get("/api/state")     # polls carry no query string
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["song_name"] == "show.wav"
    assert data["current"]["name"] == "<b>Cue</b>"


def test_internal_keys_not_served(client):
    client.set_cookie(TOKEN_COOKIE, TOKEN)
    data = client.get("/api/state").get_json()
    assert not any(k.startswith("_") for k in data)


def test_qr_uses_share_url(client):
    client.set_cookie(TOKEN_COOKIE, TOKEN)
    resp = client.get("/qr.png")
    if not web_server.HAS_QRCODE:
        assert resp.status_code == 404
    else:
        assert resp.status_code == 200
        assert resp.mimetype == "image/png"
        assert resp.data[:8] == b"\x89PNG\r\n\x1a\n"


def test_factory_generates_token_when_missing(state):
    app = create_flask_app(state)
    assert len(app.config["CUE_TOKEN"]) >= 16
    app.testing = True
    assert app.test_client().get("/api/state").status_code == 403


# ---------------------------------------------------------- escaping ----

def test_html_escapes_user_text(client):
    html = client.get("/?k=" + TOKEN).get_data(as_text=True)
    assert "function esc(" in html
    assert "${esc(item.name)}" in html
    assert "${esc(tag)}" in html
    assert "esc(notes)" in html
    assert "${item.name}" not in html
    assert "${notes || '(no notes)'}" not in html
    assert "pollInFlight" in html


# ------------------------------------------------------------ server ----

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read()


def test_start_stop_twice_rebinds_cleanly(state):
    port = _free_port()
    server = CueWebServer(state, port=port)
    first_token = None
    try:
        for _ in range(2):
            url = server.start()
            assert server.running
            assert server.port == port
            assert server.token and ("?k=" + server.token) in url
            # the token lives for the process: phones survive Share off/on
            assert first_token in (None, server.token)
            first_token = server.token
            assert server.get_url() == url
            assert server.get_urls()[0] == url
            assert server.get_ips() == server.ips

            local = f"http://127.0.0.1:{port}/api/state"
            with pytest.raises(urllib.error.HTTPError) as exc:
                _get(local)
            assert exc.value.code == 403
            status, body = _get(local + "?k=" + server.token)
            assert status == 200 and b"show.wav" in body

            server.stop()
            assert not server.running
            assert server.get_url() == ""
            assert server._thread is None and server._server is None
            # Listener is gone: port is reusable immediately.
            with pytest.raises(urllib.error.URLError):
                _get(local + "?k=" + first_token, timeout=1)
    finally:
        server.stop()
    server.stop()   # idempotent


def test_start_reports_port_in_use(state):
    blocker = socket.socket()
    blocker.bind(("0.0.0.0", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        server = CueWebServer(state, port=port)
        with pytest.raises(WebServerStartError) as exc:
            server.start()
        assert "already in use" in str(exc.value)
        assert str(port) in str(exc.value)
        assert not server.running
        assert server.get_url() == ""
    finally:
        blocker.close()


def test_get_qr_png(state):
    server = CueWebServer(state, port=_free_port())
    try:
        server.start()
        if web_server.HAS_QRCODE:
            assert server.get_qr_png()[:4] == b"\x89PNG"
        else:
            with pytest.raises(RuntimeError):
                server.get_qr_png()
    finally:
        server.stop()


# ---------------------------------------------------------- local ip ----

def test_local_ips_without_network(monkeypatch):
    monkeypatch.setattr(web_server, "_udp_route_candidate", lambda: None)

    def no_dns(*a, **k):
        raise socket.gaierror("no network")
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)

    assert get_local_ips() == []
    assert get_local_ip() == "127.0.0.1"


def test_local_ips_prefers_lan_and_dedupes(monkeypatch):
    monkeypatch.setattr(web_server, "_udp_route_candidate", lambda: "10.0.0.5")
    monkeypatch.setattr(
        web_server, "_hostname_candidates",
        lambda: ["127.0.0.1", "169.254.1.2", "10.0.0.5", "192.168.1.20", "172.16.3.4"])
    ips = get_local_ips()
    # the default-route interface wins; the rest by private range
    assert ips == ["10.0.0.5", "192.168.1.20", "172.16.3.4", "169.254.1.2"]
    assert get_local_ip() == "10.0.0.5"


def test_local_ips_real_machine_does_not_raise():
    ips = get_local_ips()
    assert isinstance(ips, list)
    assert "127.0.0.1" not in ips
    assert isinstance(get_local_ip(), str)


# ------------------------------------------------------------- phase 2 ----
# ack endpoint, standby / go_log / live / held in the state payload,
# department chips + ACK markup, and tolerance of the old StateManager.

class _Obj:
    """Bare attribute bag standing in for Marker / LoopRegion / HoldCue."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _State:
    def __init__(self, name):
        self.name = name


class _OldApp:
    """StateManager before phase 2: no standby/go_log/live_mode/holds."""
    current_song_name = "old.wav"
    song_length = 100.0
    state = _State("PAUSED")

    def is_playing(self):
        return False

    def is_in_loop_mode(self):
        return False


class _NewApp(_OldApp):
    state = _State("HELD")
    live_mode = True

    def __init__(self):
        self.holds = [_Obj(id="h1", time=40.0, name="Caesura", tag_notes={"Stage": "freeze"})]
        self.go_log = [{"ts": 1700000000.0 + i, "source": "ui", "action": "play",
                        "position": float(i), "label": f"go {i}", "_private": i}
                       for i in range(30)]

    def standby(self):
        return {"action": "release_hold", "label": "Release hold: Caesura",
                "next_item": self.holds[0], "next_type": "hold"}


def _auth(client):
    client.set_cookie(TOKEN_COOKIE, TOKEN)
    return client


def test_ack_requires_token(client):
    resp = client.post("/api/ack", json={"device": "SM", "cue_id": "c1"})
    assert resp.status_code == 403
    assert client.post("/api/ack?k=nope", json={"device": "SM", "cue_id": "c1"}).status_code == 403


def test_ack_accepts_query_token_and_cookie(state, client):
    assert client.post("/api/ack?k=" + TOKEN, json={"device": "SM", "cue_id": "c1"}).status_code == 204
    assert client.post("/api/ack", json={"device": "LX", "cue_id": "c1"}).status_code == 204   # cookie set by 1st
    devices = [a["device"] for a in state.get_acks("c1")]
    assert devices == ["SM", "LX"]
    assert all(isinstance(a["ts"], float) for a in state.get_acks("c1"))


@pytest.mark.parametrize("payload", [
    None, "not json", [], {}, {"device": "SM"}, {"cue_id": "c1"},
    {"device": "", "cue_id": "c1"}, {"device": "SM", "cue_id": "  "},
    {"device": 5, "cue_id": "c1"}, {"device": "SM", "cue_id": ["c1"]},
    {"device": "x" * 65, "cue_id": "c1"}, {"device": "SM", "cue_id": "y" * 129},
])
def test_ack_rejects_bad_input(state, client, payload):
    _auth(client)
    if payload is None:
        resp = client.post("/api/ack")
    elif isinstance(payload, str):
        resp = client.post("/api/ack", data=payload, content_type="application/json")
    else:
        resp = client.post("/api/ack", json=payload)
    assert resp.status_code == 400
    assert state.acks_snapshot() == {}


def test_ack_get_only_is_not_allowed(client):
    _auth(client)
    assert client.get("/api/ack").status_code == 405


def test_ack_storage_dedupes_per_device_and_normalises(state, client):
    _auth(client)
    client.post("/api/ack", json={"device": "  Lighting\n desk ", "cue_id": " c1 "})
    client.post("/api/ack", json={"device": "Lighting desk", "cue_id": "c1"})
    acks = state.get_acks("c1")
    assert len(acks) == 1 and acks[0]["device"] == "Lighting desk"
    assert state.get_acks("missing") == []
    # copies on read: mutating what we got back must not leak into the store
    acks[0]["device"] = "hacked"
    assert state.get_acks("c1")[0]["device"] == "Lighting desk"
    snap = state.acks_snapshot()
    assert snap == {"c1": [{"device": "Lighting desk", "ts": snap["c1"][0]["ts"]}]}
    assert state.acks == snap


def test_ack_store_is_bounded_to_50_cue_ids(state):
    for i in range(60):
        state.add_ack(f"cue{i}", "SM", ts=float(i))
    snap = state.acks_snapshot()
    assert len(snap) == web_server.ACK_CUE_LIMIT == 50
    assert "cue9" not in snap and "cue10" in snap and "cue59" in snap
    # re-acking an old id does not revive it or bump a newer one out
    state.add_ack("cue10", "LX")
    assert len(state.acks_snapshot()) == 50 and len(state.get_acks("cue10")) == 2
    state.clear_acks()
    assert state.acks_snapshot() == {}


def test_state_payload_has_phase2_fields_and_acks_for_current_next(state, client):
    _auth(client)
    app = _NewApp()
    cur = _Obj(id="m1", name="Verse", time=10.0, tag_notes={"Director": "watch"})
    state.update_from_app(app, 12.0, cur, "marker", app.holds[0], "hold")
    state.add_ack("m1", "SM")
    state.add_ack("h1", "LX")
    state.add_ack("other", "Props")

    data = client.get("/api/state").get_json()
    assert not any(k.startswith("_") for k in data)
    assert data["state"] == "held" and data["is_playing"] is False and data["is_paused"] is False
    assert data["live"] is True
    assert data["standby"] == {"action": "release_hold", "label": "Release hold: Caesura",
                               "next": {"id": "h1", "name": "Caesura", "type": "hold"}}
    assert len(data["go_log"]) == 20
    assert data["go_log"][-1]["label"] == "go 29"
    assert set(data["go_log"][0]) == {"ts", "source", "action", "position", "label"}
    assert data["holds"] == [{"id": "h1", "time": 40.0, "name": "Caesura"}]
    assert data["current_id"] == "m1" and data["next_id"] == "h1"
    assert data["current"]["id"] == "m1" and data["current"]["type"] == "cue"
    assert data["next"]["type"] == "hold" and data["next"]["time"] == 40.0
    assert data["next"]["countdown"] == pytest.approx(28.0)
    assert data["next"]["tag_notes"] == {"Stage": "freeze"}
    assert set(data["acks"]) == {"m1", "h1"}          # not "other"
    assert data["acks"]["m1"][0]["device"] == "SM"


def test_update_from_app_tolerates_old_state_manager(state, client):
    _auth(client)
    vamp = _Obj(id="v1", name="Vamp", start=5.0, end=9.0, tag_notes={"Sound": ""})
    marker = _Obj(id="m2", name="Next", time=20.0, tag_notes={})
    state.update_from_app(_OldApp(), 6.0, vamp, "vamp", marker, "marker")
    data = client.get("/api/state").get_json()
    assert data["song_name"] == "old.wav" and data["is_paused"] is True
    assert data["state"] == "paused" and data["live"] is False
    assert data["standby"] == {"action": "none", "label": "", "next": None}
    assert data["go_log"] == [] and data["holds"] == [] and data["acks"] == {}
    assert data["current"]["type"] == "vamp" and data["current"]["start"] == 5.0
    assert data["next"]["countdown"] == pytest.approx(14.0)

    # and with no app at all / no items
    state.update_from_app(None, 0.0, None, None, None, None)
    data = client.get("/api/state").get_json()
    assert data["current"] is None and data["next"] is None and data["current_id"] is None


def test_update_from_app_survives_broken_standby(state):
    class Broken(_NewApp):
        def __init__(self):
            super().__init__()
            self.go_log = "not a list"

        def standby(self):
            raise RuntimeError("boom")
    state.update_from_app(Broken(), 1.0, None, None, None, None)
    s = state.get_state()
    assert s["standby"]["action"] == "none" and s["go_log"] == []


def test_page_has_dept_chips_ack_and_phase2_markup(client):
    html = client.get("/?k=" + TOKEN).get_data(as_text=True)
    for dept in web_server.DEPARTMENTS:
        assert f'"{dept}"' in html
    assert web_server.DEPARTMENTS[0] == "All"
    assert 'id="deptRow"' in html and 'class="chip' in html
    assert "localStorage" in html and "URLSearchParams" in html and "'dept'" in html
    assert 'id="heldBanner"' in html and "HELD — waiting for GO" in html
    assert 'id="livePill"' in html and ">LIVE<" in html
    assert 'id="standbyText"' in html
    assert 'id="goLog"' in html and "<details" in html
    assert 'id="deviceInput"' in html and 'id="deviceSave"' in html
    assert 'class="ack-btn' in html and "'/api/ack'" in html and "Acked ✓" in html
    assert 'data-cue="${esc(id)}"' in html
    assert "${esc(count)}" in html and "${esc(e.label || '')}" in html
    assert "setInterval(poll, 500)" in html and "pollInFlight" in html
    assert "box-sizing: border-box" in html
