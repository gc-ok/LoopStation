"""Regression tests for the review fixes in midi_control / osc_output / web_server."""
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import deque

import mido
import pytest

from backend import midi_control, osc_output, web_server
from backend.midi_control import MidiController, describe_key, legacy_key, _match_port
from backend.osc_output import OscOutput, sanitize_osc_cue
from backend.web_server import (
    CueWebServer, SharedCueState, WebServerStartError, create_flask_app, get_local_ips,
    TOKEN_COOKIE,
)
from utils import preferences

from helpers_audio import wait_until


@pytest.fixture(autouse=True)
def prefs_file(tmp_path, monkeypatch):
    path = str(tmp_path / "prefs" / "user_preferences.json")
    os.makedirs(os.path.dirname(path))
    monkeypatch.setattr(preferences, "PREFS_FILE", path)
    return path


# ------------------------------------------------------------------ MIDI --

class FakePort:
    def __init__(self, name):
        self.name = name
        self.queue = deque()
        self.closed = False
        self.fail = False

    def push(self, msg):
        self.queue.append(msg)

    def iter_pending(self):
        if self.fail:
            raise OSError("device unplugged")
        while self.queue:
            yield self.queue.popleft()

    def close(self):
        self.closed = True


@pytest.fixture
def fast_reconnect(monkeypatch):
    monkeypatch.setattr(midi_control, "RECONNECT_INTERVAL_S", 0.1)


def _poll_threads():
    return [t for t in threading.enumerate() if t.name == "midi-poll" and t.is_alive()]


def test_concurrent_open_leaves_one_thread_and_close_stops_everything(fast_reconnect):
    opened = []

    def slow_factory(name):
        time.sleep(0.05)
        port = FakePort(name)
        opened.append(port)
        return port

    before = len(_poll_threads())
    ctl = MidiController(on_action=lambda a: None, port_factory=slow_factory,
                         list_inputs_fn=lambda: ["A", "B"])
    threads = [threading.Thread(target=ctl.open, args=(n,)) for n in ("A", "B", "A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wait_until(lambda: len(_poll_threads()) == before + 1, timeout=2.0)
    assert ctl.is_open
    assert sum(not p.closed for p in opened) == 1               # only the winner's port is open
    ctl.close()
    time.sleep(0.4)                                             # several reconnect intervals
    assert len(_poll_threads()) == before
    assert not ctl.is_open and all(p.closed for p in opened)


def test_superseded_poll_thread_never_reopens_after_close(fast_reconnect):
    calls = []

    def factory(name):
        calls.append(name)
        return FakePort(name)

    ctl = MidiController(on_action=lambda a: None, port_factory=factory,
                         list_inputs_fn=lambda: ["Pedal"])
    assert ctl.open("Pedal")
    thread = ctl._thread
    ctl._port.fail = True                                       # lost -> reconnect loop
    assert wait_until(lambda: not ctl.is_open, timeout=2.0)
    ctl.close()                                                 # user picks "(none)"
    assert wait_until(lambda: not thread.is_alive(), timeout=2.0)
    n = len(calls)
    time.sleep(0.4)
    assert len(calls) == n and not ctl.is_open


def test_restore_port_backs_off_after_a_user_choice():
    gate = threading.Event()

    def slow_factory(name):
        gate.wait(1.0)
        return FakePort(name)

    ctl = MidiController(on_action=lambda a: None, port_factory=slow_factory,
                         list_inputs_fn=lambda: ["Pedal"])
    try:
        worker = threading.Thread(target=ctl.restore_port, args=("Pedal",))
        worker.start()
        time.sleep(0.05)                                        # restore is inside open
        closer = threading.Thread(target=ctl.close)             # user picks "(none)"
        closer.start()
        gate.set()
        worker.join(2.0)
        closer.join(2.0)
        assert not ctl.is_open                                  # the user's choice wins
        assert ctl.restore_port("Pedal") is False               # and later restores back off
        assert not ctl.is_open
    finally:
        ctl.close()


def test_failed_or_single_missing_listing_does_not_drop_the_port(fast_reconnect):
    script = deque(["error", [], ["Pedal"], [], []])
    statuses = []

    def listing():
        item = script.popleft() if script else []
        if item == "error":
            raise RuntimeError("coremidi busy")
        return list(item)

    ctl = MidiController(on_action=lambda a: None, port_factory=FakePort,
                         list_inputs_fn=listing)
    ctl.on_status = statuses.append
    try:
        script.appendleft(["Pedal"])
        assert ctl.open("Pedal")
        # error (ignored), [] (1 miss), Pedal (reset), [] (1 miss), [] (2 misses -> lost)
        assert wait_until(lambda: not ctl.is_open, timeout=3.0)
        assert ctl.last_error == "device disconnected"
        assert statuses.count("MIDI 'Pedal' lost - reconnecting...") == 1
        assert not script                                       # took all five checks
    finally:
        ctl.close()


def test_list_inputs_still_returns_a_list_on_failure():
    def broken():
        raise RuntimeError("boom")
    ctl = MidiController(on_action=lambda a: None, list_inputs_fn=broken)
    assert ctl.list_inputs() == []
    assert ctl._list_inputs_checked() is None


def test_windows_renumbered_port_is_matched_by_base_name(fast_reconnect):
    assert _match_port("Pedal 1", ["Keys", "Pedal 2"]) == "Pedal 2"
    assert _match_port("Pedal 1", ["Pedal 2", "Pedal 3"]) is None   # ambiguous
    assert _match_port("Pedal", ["Pedal"]) == "Pedal"
    available = ["Pedal 1"]
    ctl = MidiController(on_action=lambda a: None, port_factory=FakePort,
                         list_inputs_fn=lambda: list(available))
    try:
        assert ctl.open("Pedal 1")
        ctl._port.fail = True
        available[:] = ["Pedal 2"]
        assert wait_until(lambda: ctl.is_open and ctl.port_name == "Pedal 2", timeout=2.0)
    finally:
        ctl.close()


def test_mapping_keys_include_channel_and_legacy_keys_match_any_channel():
    acts = []
    ctl = MidiController(on_action=acts.append, port_factory=FakePort,
                         list_inputs_fn=lambda: [])
    drum = mido.Message("note_on", channel=9, note=36, velocity=100)
    ctl.learn("go")
    assert ctl.handle_message(drum) == "go"
    assert ctl.mappings == {"note_on:10:36": "go"}
    assert ctl.handle_message(mido.Message("note_on", channel=0, note=36, velocity=100)) is None
    assert ctl.handle_message(drum) == "go"
    ctl.load_mappings({"note_on:36": "stop", "pc:4": "next_cue"})  # saved before channels
    assert ctl.handle_message(drum) == "stop"
    assert ctl.handle_message(mido.Message("program_change", channel=3, program=4)) == "next_cue"
    assert acts == ["go", "stop", "next_cue"]
    assert legacy_key("cc:2:64") == "cc:64" and legacy_key("cc:64") == "cc:64"
    assert describe_key("note_on:10:36") == "Note 36 (ch 10)"
    assert describe_key("cc:64") == "CC 64 (any ch)"
    assert describe_key("pc:1:5") == "Program 5 (ch 1)"


def test_cc_repeated_127_fires_again_after_rearm_but_bursts_do_not():
    acts = []
    ctl = MidiController(on_action=acts.append, port_factory=FakePort,
                         list_inputs_fn=lambda: [])
    now = [100.0]
    ctl._clock = lambda: now[0]
    ctl.load_mappings({"cc:1:64": "go"})

    def cc(value, dt):
        now[0] += dt
        return ctl.handle_message(mido.Message("control_change", control=64, value=value))

    assert cc(100, 0) == "go"
    assert cc(110, 0.005) is None and cc(127, 0.005) is None    # one press streaming
    assert cc(127, 0.1) is None                                 # still inside the burst
    assert cc(127, midi_control.CC_REARM_S + 0.05) == "go"      # 127-only pedal, pressed again
    assert cc(0, 0.05) is None and cc(127, 0.01) == "go"        # normal release / press
    assert acts == ["go", "go", "go"]


# ------------------------------------------------------------------- OSC --

def test_sanitize_osc_cue():
    assert sanitize_osc_cue(None) == ""
    assert sanitize_osc_cue("  12.5 ") == "12.5"
    assert sanitize_osc_cue(7) == "7"
    assert sanitize_osc_cue("1/2") == "12"
    assert sanitize_osc_cue("LX 5") == "LX5"
    assert sanitize_osc_cue("[1-9]") == "1-9"
    assert sanitize_osc_cue("{a,b}#?*") == "ab"
    assert sanitize_osc_cue("*") == ""
    assert sanitize_osc_cue("a\tb\nc\x00") == "abc"


class _Client:
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.sent = []

    def send_message(self, address, args):
        self.sent.append((address, list(args)))


def test_send_cue_never_sends_unsafe_addresses():
    clients = []

    def factory(host, port):
        clients.append(_Client(host, port))
        return clients[-1]

    out = OscOutput(client_factory=factory)
    out.configure(True, "127.0.0.1", 53000, "qlab")
    assert out.send_cue("1/2")
    assert out.send_cue("*") is False and out.send_cue(" , ") is False
    assert out.send_cue("LX 5")
    assert out.flush()
    out.configure(True, "127.0.0.1", 53000, "loopstation")
    assert out.send_cue("Q#3")
    assert out.flush()
    assert clients[0].sent == [("/cue/12/start", []), ("/cue/LX5/start", []),
                               ("/loopstation/cue", ["Q3"])]
    out.close()


def test_resolve_prefers_ipv4(monkeypatch):
    def fake(host, port, *a, **kw):
        return [(socket.AF_INET6, socket.SOCK_DGRAM, 17, "", ("::1", port, 0, 0)),
                (socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("127.0.0.1", port))]
    monkeypatch.setattr(osc_output.socket, "getaddrinfo", fake)
    assert osc_output._resolve("localhost", 53000) == "127.0.0.1"

    def v6_only(host, port, *a, **kw):
        return [(socket.AF_INET6, socket.SOCK_DGRAM, 17, "", ("::1", port, 0, 0))]
    monkeypatch.setattr(osc_output.socket, "getaddrinfo", v6_only)
    assert osc_output._resolve("localhost", 53000) == "::1"


# ------------------------------------------------------------- acks ----

class _St:
    def __init__(self, name):
        self.name = name


class _App:
    def __init__(self):
        self.current_song_name = "a.wav"
        self.song_length = 100.0
        self.state = _St("PLAYING")

    def is_playing(self):
        return self.state.name == "PLAYING"


class _Item:
    def __init__(self, id, time):
        self.id, self.time, self.name = id, time, id


def _push(shared, app, cur, nxt, pos=0.0):
    shared.update_from_app(app, pos, cur, "marker" if cur else None,
                           nxt, "marker" if nxt else None)


def test_ack_epoch_changes_on_new_pass_stop_and_song_change():
    shared = SharedCueState()
    app = _App()
    m1, m2, m3 = _Item("m1", 10), _Item("m2", 20), _Item("m3", 30)
    _push(shared, app, None, m1)
    e0 = shared.public_state()["ack_epoch"]
    shared.add_ack("m1", "SM")
    _push(shared, app, m1, m2)                                  # m1 goes: its acks stay
    shared.add_ack("m2", "LX")
    _push(shared, app, m2, m3)
    assert shared.ack_epoch == e0 and shared.get_acks("m2")
    _push(shared, app, m1, m2)                                  # rewound: second pass
    e1 = shared.ack_epoch
    assert e1 != e0 and shared.acks_snapshot() == {}
    shared.add_ack("m2", "LX")
    app.state = _St("STOPPED")
    _push(shared, app, None, m1)
    e2 = shared.ack_epoch
    assert e2 != e1 and shared.acks_snapshot() == {}
    app.state = _St("PLAYING")
    _push(shared, app, None, m1)
    assert shared.ack_epoch == e2                               # play from the top: same pass
    shared.add_ack("m1", "SM")
    app.current_song_name = "b.wav"
    _push(shared, app, None, m1)
    assert shared.ack_epoch != e2 and shared.acks_snapshot() == {}
    # a new process (next night) never shares an epoch with the last one
    assert SharedCueState().ack_epoch != SharedCueState().ack_epoch


def test_stale_epoch_ack_is_refused_and_page_scopes_local_acks():
    shared = SharedCueState()
    app = create_flask_app(shared, token="T")
    client = app.test_client()
    client.set_cookie(TOKEN_COOKIE, "T")
    epoch = shared.ack_epoch
    assert client.get("/api/state").get_json()["ack_epoch"] == epoch
    ok = client.post("/api/ack", json={"device": "SM", "cue_id": "c1", "epoch": epoch})
    assert ok.status_code == 204 and shared.get_acks("c1")
    shared.clear_acks()                                         # also a new epoch
    assert shared.ack_epoch != epoch
    stale = client.post("/api/ack", json={"device": "SM", "cue_id": "c1", "epoch": epoch})
    assert stale.status_code == 409 and shared.get_acks("c1") == []
    # old clients without an epoch still work
    assert client.post("/api/ack", json={"device": "SM", "cue_id": "c1"}).status_code == 204
    html = web_server.MONITOR_HTML
    assert "ackEpoch + '|' + cueId" in html and "data.ack_epoch" in html


# ------------------------------------------------------------ server ----

@pytest.fixture
def fresh_server_globals(monkeypatch):
    monkeypatch.setattr(CueWebServer, "_process_token", None)
    monkeypatch.setattr(CueWebServer, "_last_port", None)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read()


def _recv_all(sock, timeout=3.0):
    sock.settimeout(timeout)
    data = b""
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                return data, True                               # closed by the server
            data += chunk
    except socket.timeout:
        return data, False


def test_token_survives_restart_and_new_instances_until_revoked(fresh_server_globals):
    shared = SharedCueState()
    a = CueWebServer(shared, port=0)
    try:
        a.start()
        token = a.token
        a.stop()
        b = CueWebServer(shared, port=0)                        # app.py makes a new one per share
        b.start()
        assert b.token == token
        local = f"http://127.0.0.1:{b.port}/api/state?k="
        assert _get(local + token)[0] == 200
        new_url = b.revoke_token()
        assert b.token != token and ("k=" + b.token) in new_url and b.url == new_url
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(local + token)
        assert exc.value.code == 403
        assert _get(local + b.token)[0] == 200
        b.stop()
        assert CueWebServer(shared, port=0)._shared_token() == b.token
    finally:
        a.stop()
        b.stop()


def test_idle_connections_time_out_and_are_capped(fresh_server_globals, monkeypatch):
    monkeypatch.setattr(web_server._ShareRequestHandler, "timeout", 0.3)
    monkeypatch.setattr(web_server, "MAX_CONNECTIONS_PER_IP", 3)
    server = CueWebServer(SharedCueState(), port=0)
    socks = []
    try:
        server.start()
        port = server.port
        for _ in range(3):
            s = socket.create_connection(("127.0.0.1", port))
            s.sendall(b"GET /api/state HTTP/1.1\r\nHost: x\r\n")   # never finished
            socks.append(s)
        assert wait_until(lambda: server._server.connection_count == 3, timeout=2.0)
        extra = socket.create_connection(("127.0.0.1", port))
        socks.append(extra)
        data, closed = _recv_all(extra)
        assert data.startswith(b"HTTP/1.1 503") and closed
        for s in socks[:3]:
            _, closed = _recv_all(s, timeout=3.0)               # dropped after the timeout
            assert closed
        assert wait_until(lambda: server._server.connection_count == 0, timeout=2.0)
    finally:
        for s in socks:
            s.close()
        server.stop()


def test_stop_drops_lingering_connections_quickly(fresh_server_globals):
    server = CueWebServer(SharedCueState(), port=0)
    socks = []
    try:
        server.start()
        for _ in range(5):
            s = socket.create_connection(("127.0.0.1", server.port))
            s.sendall(b"GET / HTTP/1.1\r\n")
            socks.append(s)
        inner = server._server
        assert wait_until(lambda: inner.connection_count == 5, timeout=2.0)
        t0 = time.monotonic()
        server.stop()
        assert time.monotonic() - t0 < 3.0
        for s in socks:
            _, closed = _recv_all(s, timeout=2.0)
            assert closed
        assert wait_until(lambda: inner.connection_count == 0, timeout=2.0)
    finally:
        for s in socks:
            s.close()
        server.stop()


def test_stop_does_not_hang_when_serve_loop_never_started(fresh_server_globals, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(web_server._ShareServer, "serve_forever",
                        lambda self, poll_interval=0.5: release.wait(5.0))
    monkeypatch.setattr(web_server, "SHUTDOWN_TIMEOUT_S", 0.2)
    server = CueWebServer(SharedCueState(), port=0)
    try:
        server.start()
        t0 = time.monotonic()
        server.stop(timeout=0.2)
        assert time.monotonic() - t0 < 2.0
        assert not server.running
    finally:
        release.set()
        server.stop()


def test_busy_port_falls_back_to_the_next_one(fresh_server_globals):
    blocker = socket.socket()
    blocker.bind(("0.0.0.0", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    server = CueWebServer(SharedCueState(), port=port, port_fallback=5)
    try:
        url = server.start()
        assert port < server.port <= port + 5
        assert server.get_port() == server.port and f":{server.port}/" in url
        assert _get(f"http://127.0.0.1:{server.port}/api/state?k={server.token}")[0] == 200
        chosen = server.port
        server.stop()
        again = CueWebServer(SharedCueState(), port=port, port_fallback=5)
        assert again._candidate_ports()[0] == chosen            # phones keep their URL
    finally:
        server.stop()
        blocker.close()
    assert CueWebServer(SharedCueState(), port=1234)._port_fallback == 0
    assert CueWebServer(SharedCueState())._port_fallback == web_server.PORT_FALLBACK


def test_all_fallback_ports_busy_reports_the_range(fresh_server_globals, monkeypatch):
    def busy(self, port, app):
        err = WebServerStartError(f"Port {port} is already in use")
        err.in_use = True
        raise err
    monkeypatch.setattr(CueWebServer, "_bind", busy)
    server = CueWebServer(SharedCueState(), port=9000, port_fallback=2)
    with pytest.raises(WebServerStartError) as exc:
        server.start()
    assert "9000-9002" in str(exc.value) and not server.running


def test_no_reuse_address_on_windows():
    assert web_server._ShareServer.allow_reuse_address == (os.name != "nt")


def test_default_route_interface_beats_virtual_adapters(monkeypatch):
    monkeypatch.setattr(web_server, "_udp_route_candidate", lambda: "10.0.5.20")
    monkeypatch.setattr(web_server, "_hostname_candidates", lambda: ["192.168.56.1", "10.0.5.20"])
    assert get_local_ips() == ["10.0.5.20", "192.168.56.1"]
    monkeypatch.setattr(web_server, "_udp_route_candidate", lambda: "172.20.10.2")
    monkeypatch.setattr(web_server, "_hostname_candidates", lambda: ["10.211.55.2", "172.20.10.2"])
    assert get_local_ips()[0] == "172.20.10.2"
    # no default route: physical private ranges before VM adapters, link-local last
    monkeypatch.setattr(web_server, "_udp_route_candidate", lambda: None)
    monkeypatch.setattr(web_server, "_hostname_candidates",
                        lambda: ["169.254.3.3", "192.168.56.1", "10.0.0.7", "192.168.1.5"])
    assert get_local_ips() == ["192.168.1.5", "10.0.0.7", "192.168.56.1", "169.254.3.3"]
    # a link-local "route" does not jump the queue
    monkeypatch.setattr(web_server, "_udp_route_candidate", lambda: "169.254.9.9")
    monkeypatch.setattr(web_server, "_hostname_candidates", lambda: ["192.168.1.5"])
    assert get_local_ips() == ["192.168.1.5", "169.254.9.9"]
