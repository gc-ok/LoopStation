"""Running-order export (backend/running_order.py)."""
import csv
import io
import os

import pytest

from backend import running_order as ro
from backend.state_manager import StateManager
from utils import preferences

from fake_engine import FakeEngine
from helpers_audio import make_signal, write_wav


@pytest.fixture(autouse=True)
def prefs_file(tmp_path, monkeypatch):
    path = str(tmp_path / "prefs" / "user_preferences.json")
    os.makedirs(os.path.dirname(path))
    monkeypatch.setattr(preferences, "PREFS_FILE", path)
    return path


@pytest.fixture
def sm(tmp_path, monkeypatch):
    monkeypatch.setattr(StateManager, "_start_monitor", lambda self: None)
    data = tmp_path / "appdata"
    data.mkdir()
    state = StateManager(ffmpeg_path="no-ffmpeg", data_file=str(data / "loop_data.json"),
                         engine=FakeEngine())
    yield state
    state.cleanup()


def _song(folder, name, secs, freq=440.0):
    os.makedirs(folder, exist_ok=True)
    return write_wav(os.path.join(folder, name), make_signal(secs, sr=8000, freq=freq), sr=8000)


@pytest.fixture
def show(sm, tmp_path):
    folder = str(tmp_path / "music")
    a = _song(folder, "Overture <A&B>.wav", 2.0, 300)
    b = _song(folder, "Ballad, \"Slow\".wav", 3.0, 400)
    c = _song(folder, "Cut Song.wav", 4.0, 500)
    d = _song(folder, "Finale ♪ Ünï.wav", 5.0, 600)
    sm.new_show("Act <One> & Two")
    sm.add_divider("Act 1 <script>")
    ids = sm.add_songs([a, b, c])
    sm.add_divider("Act 2")
    ids += sm.add_songs([d])
    sm.set_entry_cut(ids[2], True)
    sm.set_entry_segue(ids[0], True)
    # Saved cue data for the ballad: 2 vamps, 1 hold, 1 cue, tempo 90 %, +2 st, OSC, notes
    assert sm.load_song(b)
    sm.add_loop(0.5, 1.5, name="Vamp A")
    sm.add_loop(1.6, 2.6, name="Vamp B")
    sm.add_hold(2.8)
    marker = sm.add_marker(0.2, name="Lights <go>")
    sm.set_item_tag_note(marker.id, "Lights", "Cue 12 = blackout & haze")
    sm.settings.tempo_ratio = 0.9
    sm.settings.semitones = 2.0
    sm.set_song_osc_cue("=SUM(1)")
    sm.save_loop()
    sm.flush_save()
    return sm, ids


def test_numbering_skips_dividers_and_cut_and_totals(show):
    sm, ids = show
    data = ro.build_running_order(sm)
    rows = data["rows"]
    assert [r["kind"] for r in rows] == ["divider", "song", "song", "song", "divider", "song"]
    assert [r["number"] for r in rows] == [None, 1, 2, None, None, 3]
    assert rows[3]["cut"] and rows[1]["segue"]
    ballad = rows[2]
    assert (ballad["vamps"], ballad["holds"], ballad["cues"]) == (2, 1, 1)
    assert ballad["tempo"] == "90%" and ballad["key"] == "+2 st"
    assert abs(ballad["running"] - 3.0 / 0.9) < 0.01
    assert rows[1]["duration"] == pytest.approx(2.0, abs=0.01)   # from the file header
    assert data["song_count"] == 3 and data["cut_count"] == 1
    # cut song (4 s) is excluded: 2 + 3/0.9 + 5
    assert data["total_seconds"] == pytest.approx(2.0 + 3.0 / 0.9 + 5.0, abs=0.02)
    assert ro.format_duration(data["total_seconds"]) == "0:10"


def test_html_is_escaped_self_contained_and_printable(show, tmp_path):
    sm, _ = show
    path = str(tmp_path / "out" / "order.html")
    assert ro.export_running_order(sm, path, "html") == path
    text = open(path, encoding="utf-8").read()
    assert "<script>" not in text
    assert "Act 1 &lt;script&gt;" in text
    assert "Act &lt;One&gt; &amp; Two" in text
    assert "Overture &lt;A&amp;B&gt;" in text
    assert "Cue 12 = blackout &amp; haze" in text
    assert "@media print" in text
    assert "http://" not in text and "https://" not in text and "<link" not in text
    assert "Finale ♪ Ünï" in text
    assert "3 songs" in text and "1 cut" in text


def test_html_page_break_per_act(show, tmp_path):
    sm, _ = show
    path = str(tmp_path / "order.html")
    ro.export_running_order(sm, path, "html", page_break_per_act=True)
    text = open(path, encoding="utf-8").read()
    assert text.count('<tbody class="act-break">') == 1


def test_csv_bom_quoting_unicode_and_formula_guard(show, tmp_path):
    sm, _ = show
    path = str(tmp_path / "order.csv")
    ro.export_running_order(sm, path, "csv")
    raw = open(path, "rb").read()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in raw
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
    assert rows[0] == ro.CSV_COLUMNS
    titles = [r[2] for r in rows[1:] if len(r) > 2]
    assert 'Ballad, "Slow"' in titles
    assert "Finale ♪ Ünï" in titles
    ballad = next(r for r in rows if len(r) > 2 and r[2] == 'Ballad, "Slow"')
    assert ballad[0] == "2" and ballad[7:10] == ["2", "1", "1"]
    assert ballad[12] == "'=SUM(1)"            # Excel would evaluate a leading '='
    assert "Lights: Cue 12 = blackout & haze" in ballad[13]
    cut = next(r for r in rows if len(r) > 2 and r[2] == "Cut Song")
    assert cut[0] == "" and "CUT" in cut[3]
    section = next(r for r in rows if len(r) > 2 and r[1] == "Section" and r[2] == "Act 2")
    assert section[0] == ""
    assert rows[-1][1] == "Total" and rows[-1][2].startswith("3 songs")


def test_unknown_duration_blank_and_counted(sm, tmp_path):
    folder = str(tmp_path / "m")
    a = _song(folder, "a.wav", 1.0)
    bad = os.path.join(folder, "broken.wav")
    with open(bad, "wb") as f:
        f.write(b"not a wav at all" * 10)
    sm.new_show("S")
    sm.add_songs([a, bad])
    data = ro.build_running_order(sm)
    assert data["rows"][1]["running"] is None
    assert data["unknown_durations"] == 1
    csv_text = ro.render_csv(data)
    assert "unknown length" in csv_text


def test_bad_format_and_open_in_browser(sm, tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        ro.export_running_order(sm, str(tmp_path / "x.pdf"), "pdf")
    opened = []
    monkeypatch.setattr(ro.webbrowser, "open", lambda uri: opened.append(uri) or True)
    path = tmp_path / "dir with space" / "o.html"
    path.parent.mkdir()
    path.write_text("x", encoding="utf-8")
    assert ro.open_in_browser(str(path))
    assert opened[0].startswith("file://") and "%20" in opened[0]
