"""Tests for backend/midi_control.py and backend/osc_output.py (no devices, no network)."""
import os
import threading
from collections import deque

import mido
import pytest

from backend import midi_control, osc_output
from backend.midi_control import MidiController, ACTIONS, message_key
from backend.osc_output import OscOutput
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
    """Stands in for a mido input: iter_pending() drains a queue."""

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


def _note(note, velocity=100):
    return mido.Message("note_on", note=note, velocity=velocity)


def _cc(control, value):
    return mido.Message("control_change", control=control, value=value)


@pytest.fixture
def ports():
    return {}


@pytest.fixture
def controller(ports):
    actions = []
    learned = []

    def factory(name):
        port = FakePort(name)
        ports[name] = port
        return port

    ctl = MidiController(on_action=actions.append, port_factory=factory,
                         list_inputs_fn=lambda: ["Pedal", "Keys"])
    ctl.on_learned = lambda action, key: learned.append((action, key))
    ctl.actions = actions
    ctl.learned = learned
    yield ctl
    ctl.close()


def test_message_key_rules():
    assert message_key(_note(60)) == "note_on:60"
    assert message_key(_note(60, velocity=0)) is None          # note-off in disguise
    assert message_key(mido.Message("note_off", note=60)) is None
    assert message_key(_cc(64, 127)) == "cc:64"
    assert message_key(_cc(64, 64)) == "cc:64"
    assert message_key(_cc(64, 63)) is None                     # pedal released
    assert message_key(mido.Message("program_change", program=5)) == "pc:5"
    assert message_key(mido.Message("pitchwheel", pitch=100)) is None
    assert message_key(object()) is None


def test_list_open_close_with_fake_ports(controller, ports):
    assert controller.list_inputs() == ["Pedal", "Keys"]
    assert not controller.is_open
    assert controller.open(None) is True                        # None = first input
    assert controller.port_name == "Pedal" and controller.is_open
    assert controller.open("Keys") is True                      # reopening closes the old port
    assert ports["Pedal"].closed and controller.port_name == "Keys"
    controller.close()
    assert ports["Keys"].closed and not controller.is_open and controller.port_name is None
    controller.close()                                          # idempotent


def test_open_failures():
    ctl = MidiController(on_action=lambda a: None, port_factory=lambda n: FakePort(n),
                         list_inputs_fn=lambda: [])
    assert ctl.open(None) is False and "no MIDI inputs" in ctl.last_error

    def broken(name):
        raise OSError("busy")
    ctl = MidiController(on_action=lambda a: None, port_factory=broken,
                         list_inputs_fn=lambda: ["X"])
    assert ctl.open("X") is False and ctl.last_error == "busy"


def test_learn_then_dispatch_through_polling_thread(controller, ports):
    assert controller.open("Pedal")
    port = ports["Pedal"]

    controller.learn("go")
    assert controller.learning == "go"
    port.push(_note(60, velocity=0))                            # ignored, still learning
    port.push(_note(60))
    assert wait_until(lambda: controller.learned == [("go", "note_on:60")], timeout=2.0)
    assert controller.mappings == {"note_on:60": "go"}
    assert controller.learning is None
    assert controller.actions == []                             # the learned message is not dispatched

    port.push(_note(60))
    port.push(_cc(64, 10))                                      # below threshold: ignored
    port.push(_note(61))                                        # unmapped: ignored
    assert wait_until(lambda: controller.actions == ["go"], timeout=2.0)

    # Learning again moves the action to the new key (one key per action)
    controller.learn("go")
    port.push(_cc(64, 127))
    assert wait_until(lambda: controller.mappings == {"cc:64": "go"}, timeout=2.0)
    assert controller.key_for("go") == "cc:64"
    port.push(_note(60))
    port.push(_cc(64, 100))                                     # still held since learning: no edge
    port.push(_cc(64, 0))                                       # released
    port.push(_cc(64, 100))                                     # pressed again: fires
    assert wait_until(lambda: controller.actions == ["go", "go"], timeout=2.0)


def test_learn_timeout_and_cancel(controller):
    controller.learn("stop", timeout_s=0.01)
    assert wait_until(lambda: controller.learning is None, timeout=1.0)
    assert controller.handle_message(_note(1)) is None          # expired learn does not map
    assert controller.mappings == {}
    controller.learn("stop")
    controller.cancel_learn()
    assert controller.handle_message(_note(1)) is None
    with pytest.raises(ValueError):
        controller.learn("not-an-action")


def test_handle_message_dispatch_and_action_errors(controller):
    controller.load_mappings({"note_on:36": "play_pause", "pc:2": "next_cue", "cc:1": "bogus"})
    assert controller.mappings == {"note_on:36": "play_pause", "pc:2": "next_cue"}
    assert controller.handle_message(_note(36)) == "play_pause"
    assert controller.handle_message(mido.Message("program_change", program=2)) == "next_cue"
    assert controller.actions == ["play_pause", "next_cue"]

    def boom(action):
        raise RuntimeError("ui exploded")
    controller.on_action = boom
    assert controller.handle_message(_note(36)) == "play_pause"  # error logged, not raised
    controller.clear_mapping("play_pause")
    assert controller.handle_message(_note(36)) is None


@pytest.fixture
def fast_reconnect(monkeypatch):
    monkeypatch.setattr(midi_control, "RECONNECT_INTERVAL_S", 0.1)


def test_port_error_reports_status_and_reconnects_by_name(controller, ports, fast_reconnect):
    statuses = []
    controller.on_status = statuses.append
    controller.load_mappings({"note_on:60": "go"})
    assert controller.open("Pedal")
    first = ports["Pedal"]
    thread = controller._thread
    first.fail = True
    assert wait_until(lambda: not controller.is_open, timeout=2.0)
    assert "unplugged" in controller.last_error and first.closed
    assert thread.is_alive()                                    # keeps trying
    assert controller.port_name == "Pedal"
    assert wait_until(lambda: controller.is_open and ports["Pedal"] is not first, timeout=2.0)
    assert statuses == ["MIDI: Pedal", "MIDI 'Pedal' lost - reconnecting...",
                        "MIDI 'Pedal' reconnected"]
    ports["Pedal"].push(_note(60))
    assert wait_until(lambda: controller.actions == ["go"], timeout=2.0)


def test_port_vanishing_from_the_list_is_detected(ports, fast_reconnect):
    available = ["Pedal"]
    statuses = []

    def factory(name):
        if name not in available:
            raise OSError("no such port")
        ports[name] = FakePort(name)
        return ports[name]

    ctl = MidiController(on_action=lambda a: None, port_factory=factory,
                         list_inputs_fn=lambda: list(available))
    ctl.on_status = statuses.append
    try:
        assert ctl.open("Pedal")
        available.clear()                                       # unplugged, port never raises
        assert wait_until(lambda: not ctl.is_open, timeout=2.0)
        assert "disconnected" in ctl.last_error
        import time as _time
        _time.sleep(0.3)
        assert not ctl.is_open                                  # still absent: no reopen
        available.append("Pedal")
        assert wait_until(lambda: ctl.is_open, timeout=2.0)
        assert statuses[-2:] == ["MIDI 'Pedal' lost - reconnecting...", "MIDI 'Pedal' reconnected"]
    finally:
        ctl.close()
    assert not ctl.is_open


def test_close_stops_reconnecting(controller, ports, fast_reconnect):
    assert controller.open("Pedal")
    thread = controller._thread
    ports["Pedal"].fail = True
    assert wait_until(lambda: not controller.is_open, timeout=2.0)
    controller.close()
    assert wait_until(lambda: not thread.is_alive(), timeout=2.0)
    assert not controller.is_open and controller.port_name is None


def test_cc_is_edge_triggered_per_channel_and_controller(controller):
    controller.load_mappings({"cc:64": "go", "cc:65": "stop", "note_on:36": "next_cue"})
    seq = [_cc(64, 100), _cc(64, 110), _cc(64, 127),           # one press, streaming values
           _cc(65, 127),                                        # other controller: independent
           mido.Message("control_change", channel=1, control=64, value=127),  # other channel
           _cc(64, 63), _cc(64, 64),                            # release then press
           _cc(64, 64)]
    fired = [controller.handle_message(m) for m in seq]
    assert fired == ["go", None, None, "stop", "go", None, "go", None]
    assert controller.actions == ["go", "stop", "go", "go"]
    # notes stay per press
    assert controller.handle_message(_note(36)) == "next_cue"
    assert controller.handle_message(_note(36)) == "next_cue"


def test_mappings_persist_in_preferences(controller, prefs_file):
    assert controller.open("Keys")
    controller.load_mappings({"note_on:60": "go", "cc:65": "fade_exit"})
    saved = controller.save_mappings()
    assert saved == {"mappings": {"note_on:60": "go", "cc:65": "fade_exit"}, "port": "Keys"}
    assert preferences.load_preferences()["midi"] == saved

    other = MidiController(on_action=lambda a: None)
    assert other.load_from_preferences() == saved
    assert other.mappings == {"note_on:60": "go", "cc:65": "fade_exit"}
    assert MidiController(on_action=lambda a: None).load_from_preferences() == saved


def test_backend_selection_prefers_rtmidi_only_if_importable(monkeypatch):
    monkeypatch.setattr(midi_control, "_backend_ready", False)
    chosen = []
    import mido as real_mido
    monkeypatch.setattr(real_mido, "set_backend", lambda name: chosen.append(name))
    assert midi_control._ensure_backend() is True
    expected = "mido.backends.rtmidi" if __import__("importlib").util.find_spec("rtmidi") else "mido.backends.pygame"
    assert chosen == [expected]
    assert midi_control._ensure_backend() is True               # cached
    assert chosen == [expected]


def test_actions_list_matches_contract():
    assert ACTIONS == ["go", "play_pause", "stop", "exit_vamp", "fade_exit", "skip_vamp",
                       "next_cue", "prev_cue", "add_marker", "toggle_live"]


# ------------------------------------------------------------------- OSC --

class FakeClient:
    instances = []

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.sent = []
        self.fail = False
        FakeClient.instances.append(self)

    def send_message(self, address, value):
        if self.fail:
            raise OSError("network down")
        self.sent.append((address, list(value)))


@pytest.fixture
def osc(monkeypatch):
    FakeClient.instances = []
    monkeypatch.setattr(osc_output, "SimpleUDPClient", FakeClient)
    return OscOutput()


def test_loopstation_scheme_sends_events_and_cues(osc):
    osc.configure(True, "192.168.1.20", 7000, "loopstation")
    assert osc.send_event("go", action="play", position=1.5, source="ui", flag=True, none=None)
    assert osc.send_cue("12.5")
    assert osc.send_cue("") is False
    assert osc.flush()
    client = FakeClient.instances[0]
    assert (client.host, client.port) == ("192.168.1.20", 7000)
    assert client.sent == [
        ("/loopstation/go", ["play", 1.5, "ui", 1, ""]),
        ("/loopstation/cue", ["12.5"]),
    ]
    assert len(FakeClient.instances) == 1                        # client reused


def test_qlab_scheme_sends_only_cues(osc):
    osc.configure(True, "127.0.0.1", 53000, "qlab")
    assert osc.send_event("vamp_enter", name="V") is False
    assert osc.send_cue("3.1")
    assert osc.flush()
    assert FakeClient.instances[0].sent == [("/cue/3.1/start", [])]


def test_disabled_and_bad_settings(osc):
    assert osc.send_event("go") is False and FakeClient.instances == []
    osc.configure(False, "127.0.0.1", 53000, "qlab")
    assert osc.send_cue("1") is False and FakeClient.instances == []
    osc.configure(True, "", "not-a-port", "weird")
    assert osc.settings() == {"enabled": True, "host": "127.0.0.1", "port": 53000,
                              "scheme": "loopstation"}


def test_send_never_raises_and_rebuilds_client(osc):
    osc.configure(True, "127.0.0.1", 9000)
    assert osc.send_event("go") and osc.flush()
    FakeClient.instances[0].fail = True
    assert osc.send_event("go") is True                          # queued; failure happens on the worker
    assert osc.flush()
    assert osc.last_error == "network down"
    assert osc.send_cue("1") and osc.flush()                     # new client after the failure
    assert len(FakeClient.instances) == 2 and osc.last_error is None

    osc.configure(True, "127.0.0.1", 9001)                       # host/port change -> new client
    assert osc.send_event("x") and osc.flush()
    assert FakeClient.instances[-1].port == 9001


def test_missing_python_osc_is_handled(monkeypatch):
    monkeypatch.setattr(osc_output, "SimpleUDPClient", None)
    out = OscOutput()
    out.configure(True, "127.0.0.1", 9000)
    assert out.send_event("go") is False and "python-osc" in out.last_error


def test_osc_settings_persist(osc, prefs_file):
    osc.configure(True, "10.1.1.1", 8000, "qlab")
    assert osc.save_settings() == {"enabled": True, "host": "10.1.1.1", "port": 8000, "scheme": "qlab"}
    assert preferences.load_preferences()["osc"]["host"] == "10.1.1.1"
    fresh = OscOutput()
    assert fresh.load_settings() == osc.settings()
    assert fresh.enabled and fresh.scheme == "qlab" and fresh.port == 8000


def test_osc_is_thread_safe_under_concurrent_sends(osc):
    osc.configure(True, "127.0.0.1", 9000)
    threads = [threading.Thread(target=lambda: [osc.send_event("tick", i=i) for i in range(50)])
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert osc.flush()
    assert sum(len(c.sent) for c in FakeClient.instances) == 200


def test_send_event_never_blocks_on_a_slow_client(monkeypatch):
    """The caller (GO path, engine thread) only enqueues; the worker sends."""
    import time as _time

    class SlowClient(FakeClient):
        def send_message(self, address, value):
            _time.sleep(0.2)
            super().send_message(address, value)

    FakeClient.instances = []
    monkeypatch.setattr(osc_output, "SimpleUDPClient", SlowClient)
    out = OscOutput()
    out.configure(True, "127.0.0.1", 9000)
    assert out.flush()
    worst = 0.0
    for i in range(5):
        t0 = _time.perf_counter()
        assert out.send_event("go", i=i) is True
        worst = max(worst, _time.perf_counter() - t0)
    assert worst < 0.005
    assert out.flush(timeout=3.0)
    assert [v for _, v in FakeClient.instances[0].sent] == [[0], [1], [2], [3], [4]]


def test_host_resolved_once_on_configure_and_slow_dns_does_not_block(monkeypatch):
    import time as _time
    lookups = []

    def slow_getaddrinfo(host, port, *a, **kw):
        lookups.append(host)
        _time.sleep(0.3)
        return [(2, 2, 17, "", ("10.9.8.7", port))]

    FakeClient.instances = []
    monkeypatch.setattr(osc_output, "SimpleUDPClient", FakeClient)
    monkeypatch.setattr(osc_output.socket, "getaddrinfo", slow_getaddrinfo)
    out = OscOutput()
    t0 = _time.perf_counter()
    out.configure(True, "stage-pc.local", 53000)
    assert out.send_event("a") and out.send_event("b")
    assert _time.perf_counter() - t0 < 0.05                      # nothing waited for DNS
    assert out.flush(timeout=3.0)
    assert lookups == ["stage-pc.local"]                         # resolved once
    client = FakeClient.instances[0]
    assert (client.host, client.port) == ("10.9.8.7", 53000)
    assert [a for a, _ in client.sent] == ["/loopstation/a", "/loopstation/b"]


def test_unresolvable_host_sets_error_without_raising(monkeypatch):
    def broken(host, port, *a, **kw):
        raise OSError("no such host")

    FakeClient.instances = []
    monkeypatch.setattr(osc_output, "SimpleUDPClient", FakeClient)
    monkeypatch.setattr(osc_output.socket, "getaddrinfo", broken)
    out = OscOutput()
    out.configure(True, "nowhere.invalid", 9000)
    assert out.send_event("go") is True
    assert out.flush()
    assert "no such host" in out.last_error and FakeClient.instances == []


def test_full_queue_drops_oldest_and_logs_once(monkeypatch, caplog):
    import logging as _logging
    gate = threading.Event()

    class BlockedClient(FakeClient):
        def send_message(self, address, value):
            gate.wait(5.0)
            super().send_message(address, value)

    FakeClient.instances = []
    monkeypatch.setattr(osc_output, "SimpleUDPClient", BlockedClient)
    out = OscOutput(queue_max=3)
    out.configure(True, "127.0.0.1", 9000)
    assert out.flush()
    out.send_event("first")                                      # worker picks this up and blocks
    assert wait_until(lambda: out._busy and not out._queue, timeout=2.0)
    with caplog.at_level(_logging.WARNING, logger="LoopStation.OSC"):
        for i in range(6):
            out.send_event("n", i=i)
    assert out.dropped == 3
    assert sum("queue full" in r.message for r in caplog.records) == 1
    gate.set()
    assert out.flush(timeout=3.0)
    assert [v for _, v in FakeClient.instances[0].sent] == [[], [3], [4], [5]]
