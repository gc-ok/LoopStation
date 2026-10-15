"""
Running-order export: the current show as a printable HTML page or a CSV.

Per show entry, in order: number (songs that are not cut; dividers and cut
songs get none), title, kind (dividers become section rows), CUT / segue /
missing flags, file name, duration, vamp / hold / cue counts from the saved
loop data, saved tempo / key when not default, the song's OSC cue and an
optional summary of the per-department notes.

Durations come from the saved loop data ("duration") or the file header
(soundfile / wave) -- nothing is decoded. Unknown durations stay blank and
are left out of the total. The running time shown and totalled is the
playing time at the song's saved tempo (source duration / tempo ratio).

Public API:
    build_running_order(state) -> dict        (plain data; used by both writers)
    export_running_order(state, path, fmt="html"|"csv", include_notes=True,
                         page_break_per_act=False) -> str   (the path written)
    open_in_browser(path) -> bool

Thread safety: reads the show through show_snapshot() and loop_data under the
state's save lock; it can run on any thread (fingerprinting reads ~12 KB per
file, so the UI may prefer a worker for very long shows).
"""

import csv
import html
import io
import logging
import math
import os
import pathlib
import time
import wave
import webbrowser
from typing import Any, Dict, List, Optional

from utils.fileio import write_text_atomic

logger = logging.getLogger("LoopStation.RunningOrder")

FORMATS = ("html", "csv")
NOTE_TEXT_LIMIT = 200      # characters per note in the export
NOTES_PER_SONG_LIMIT = 30  # note lines per song (the rest are counted)

CSV_COLUMNS = ["#", "Kind", "Title", "Flags", "File", "Duration", "Duration (s)",
               "Vamps", "Holds", "Cues", "Tempo", "Key", "OSC cue", "Notes"]


# ------------------------------------------------------------- helpers --

def _finite(value) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def format_duration(seconds: Optional[float]) -> str:
    """M:SS (H:MM:SS from an hour); blank for unknown."""
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return ""
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _header_duration(path: str) -> Optional[float]:
    """Duration from the file header (no decoding). None if unknown."""
    try:
        import soundfile
        info = soundfile.info(path)
        if info.samplerate and info.frames and info.frames > 0:
            return info.frames / float(info.samplerate)
    except Exception:
        pass
    if path.lower().endswith(".wav"):
        try:
            with wave.open(path, "rb") as wf:
                rate = wf.getframerate()
                if rate:
                    return wf.getnframes() / float(rate)
        except Exception:
            pass
    return None


def _saved_entry(state, path: str) -> Optional[dict]:
    """The song's saved loop data (by content fingerprint), or None."""
    try:
        song_id = state._get_file_fingerprint(path)
    except Exception:
        return None
    if not song_id:
        return None
    lock = getattr(state, "_save_lock", None)
    try:
        if lock is not None:
            with lock:
                saved = state.loop_data.get(song_id)
                return _copy_entry(saved)
        return _copy_entry(state.loop_data.get(song_id))
    except Exception:
        return None


def _copy_entry(saved) -> Optional[dict]:
    if not isinstance(saved, dict):
        return None
    out = {}
    for key in ("duration", "loops", "markers", "holds", "settings"):
        value = saved.get(key)
        if isinstance(value, list):
            out[key] = [dict(v) for v in value if isinstance(v, dict)]
        elif isinstance(value, dict):
            out[key] = dict(value)
        else:
            out[key] = value
    return out


def _tempo_text(ratio: Optional[float]) -> str:
    if ratio is None or abs(ratio - 1.0) < 1e-6:
        return ""
    return f"{ratio * 100:.0f}%"


def _key_text(semis: Optional[float]) -> str:
    if semis is None or abs(semis) < 1e-6:
        return ""
    if abs(semis - round(semis)) < 1e-6:
        return f"{int(round(semis)):+d} st"
    return f"{semis:+.1f} st"


def _notes(saved: dict) -> List[dict]:
    """[{"cue", "dept", "text"}] from vamps, cues and holds (non-empty notes only)."""
    found = []
    for kind in ("loops", "markers", "holds"):
        for item in saved.get(kind) or []:
            notes = item.get("tag_notes")
            if not isinstance(notes, dict):
                continue
            for dept, text in notes.items():
                text = str(text or "").strip()
                if not text:
                    continue
                if len(text) > NOTE_TEXT_LIMIT:
                    text = text[:NOTE_TEXT_LIMIT - 1] + "…"
                found.append({"cue": str(item.get("name") or ""), "dept": str(dept),
                              "text": " ".join(text.split()),
                              "time": _finite(item.get("start", item.get("time"))) or 0.0})
    found.sort(key=lambda n: n["time"])
    return found


# ---------------------------------------------------------------- model --

def build_running_order(state) -> Dict[str, Any]:
    """
    Plain-data running order:
    {"show_name", "generated", "rows": [row], "song_count", "total_seconds",
     "unknown_durations", "cut_count"}
    row: {"kind": "song"|"divider", "number": int|None, "title", "cut", "segue",
          "missing", "file", "duration", "running", "vamps", "holds", "cues",
          "tempo", "key", "osc_cue", "notes": [{"cue", "dept", "text"}]}
    """
    snap = state.show_snapshot()
    rows: List[dict] = []
    number = 0
    total = 0.0
    unknown = 0
    cut_count = 0
    for e in snap.get("entries", []):
        kind = e.get("kind")
        title = str(e.get("title") or "")
        if kind != "song":
            rows.append({"kind": "divider", "number": None, "title": title})
            continue
        path = e.get("path") or ""
        cut = bool(e.get("cut"))
        missing = bool(e.get("missing"))
        saved = _saved_entry(state, path) if path and not missing else None
        saved = saved or {}
        duration = _finite(saved.get("duration"))
        if (duration is None or duration <= 0) and path and not missing:
            duration = _header_duration(path)
        settings = saved.get("settings") if isinstance(saved.get("settings"), dict) else {}
        ratio = _finite(settings.get("tempo_ratio"))
        semis = _finite(settings.get("semitones"))
        running = None
        if duration is not None and duration > 0:
            running = duration / ratio if ratio and ratio > 0 else duration
        if cut:
            cut_count += 1
        else:
            number += 1
            if running is None:
                unknown += 1
            else:
                total += running
        rows.append({
            "kind": "song", "number": None if cut else number, "title": title,
            "cut": cut, "segue": bool(e.get("segue")), "missing": missing,
            "file": os.path.basename(path) if path else "",
            "duration": duration, "running": running,
            "vamps": len(saved.get("loops") or []),
            "holds": len(saved.get("holds") or []),
            "cues": len(saved.get("markers") or []),
            "tempo": _tempo_text(ratio), "key": _key_text(semis),
            "osc_cue": str(settings.get("osc_cue") or ""),
            "notes": _notes(saved),
        })
    return {"show_name": str(snap.get("name") or "Show"),
            "generated": time.strftime("%Y-%m-%d %H:%M"),
            "rows": rows, "song_count": number, "total_seconds": total,
            "unknown_durations": unknown, "cut_count": cut_count}


def _flags(row: dict) -> List[str]:
    flags = []
    if row.get("cut"):
        flags.append("CUT")
    if row.get("segue"):
        flags.append("SEGUE")
    if row.get("missing"):
        flags.append("MISSING")
    return flags


def _totals_text(data: dict) -> str:
    text = f"{data['song_count']} songs, running time {format_duration(data['total_seconds']) or '0:00'}"
    if data["unknown_durations"]:
        text += f" (+{data['unknown_durations']} with unknown length)"
    if data["cut_count"]:
        text += f"; {data['cut_count']} cut (not counted)"
    return text


# ------------------------------------------------------------------ CSV --

def _csv_cell(value) -> str:
    """Text for a CSV cell; formula-looking text is prefixed with ' (Excel)."""
    text = "" if value is None else str(value)
    if text and text[0] in ("=", "+", "@", "\t", "\r"):
        text = "'" + text
    return text


def render_csv(data: dict, include_notes: bool = True) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for row in data["rows"]:
        if row["kind"] == "divider":
            writer.writerow(["", "Section", _csv_cell(row["title"])] + [""] * (len(CSV_COLUMNS) - 3))
            continue
        notes = ""
        if include_notes:
            notes = " | ".join(f"{n['dept']}: {n['text']}" + (f" ({n['cue']})" if n["cue"] else "")
                               for n in row["notes"][:NOTES_PER_SONG_LIMIT])
        running = row["running"]
        writer.writerow([
            row["number"] if row["number"] is not None else "", "Song",
            _csv_cell(row["title"]), " ".join(_flags(row)), _csv_cell(row["file"]),
            format_duration(running), f"{running:.1f}" if running is not None else "",
            row["vamps"], row["holds"], row["cues"], row["tempo"], row["key"],
            _csv_cell(row["osc_cue"]), _csv_cell(notes),
        ])
    writer.writerow([])
    writer.writerow(["", "Total", _csv_cell(_totals_text(data))])
    return "﻿" + buf.getvalue()


# ----------------------------------------------------------------- HTML --

_CSS = """
:root { color-scheme: light; }
body { font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
       margin: 24px; color: #111; background: #fff; font-size: 13px; }
h1 { font-size: 22px; margin: 0 0 4px; }
.meta { color: #555; margin-bottom: 16px; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid #ddd; vertical-align: top; }
th { background: #f0f0f0; font-size: 11px; text-transform: uppercase; letter-spacing: .03em; }
td.num, th.num { text-align: right; white-space: nowrap; }
td.n { font-weight: 700; font-size: 15px; width: 2.5em; text-align: right; }
td.title { font-weight: 600; font-size: 14px; }
tr.section td { background: #222; color: #fff; font-weight: 700; font-size: 14px;
                text-transform: uppercase; letter-spacing: .05em; }
tr.cut td { color: #999; text-decoration: line-through; }
tr.cut td.flags { text-decoration: none; }
.flag { display: inline-block; font-size: 10px; font-weight: 700; padding: 1px 5px;
        border-radius: 3px; margin-right: 3px; border: 1px solid #888; }
.flag.CUT { border-color: #b00; color: #b00; }
.flag.SEGUE { border-color: #06c; color: #06c; }
.flag.MISSING { border-color: #b60; color: #b60; }
.file { color: #666; font-size: 11px; word-break: break-all; }
tr.notes td { border-bottom: 1px solid #ddd; padding-top: 0; color: #333; font-size: 11px; }
tr.notes ul { margin: 0; padding-left: 18px; }
.totals { margin-top: 14px; font-weight: 700; font-size: 14px; }
@media print {
  body { margin: 0; font-size: 11px; }
  th { background: #eee !important; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  tr.section td { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  tr { page-break-inside: avoid; break-inside: avoid; }
  thead { display: table-header-group; }
  tbody.act-break { page-break-before: always; break-before: page; }
}
"""


def _e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def render_html(data: dict, include_notes: bool = True, page_break_per_act: bool = False) -> str:
    cols = ["#", "Title", "Flags", "Length", "Vamps", "Holds", "Cues", "Tempo", "Key", "OSC"]
    ncols = len(cols)
    head = "".join(f'<th class="num">{_e(c)}</th>' if c in ("#", "Length", "Vamps", "Holds", "Cues")
                   else f"<th>{_e(c)}</th>" for c in cols)
    bodies: List[List[str]] = [[]]
    for row in data["rows"]:
        if row["kind"] == "divider":
            if page_break_per_act and any(bodies[-1]):
                bodies.append([])
            bodies[-1].append(f'<tr class="section"><td colspan="{ncols}">{_e(row["title"])}</td></tr>')
            continue
        flags = "".join(f'<span class="flag {f}">{f}</span>' for f in _flags(row))
        cls = ' class="cut"' if row["cut"] else ""
        bodies[-1].append(
            f"<tr{cls}>"
            f'<td class="n">{_e(row["number"] if row["number"] is not None else "")}</td>'
            f'<td class="title">{_e(row["title"])}<div class="file">{_e(row["file"])}</div></td>'
            f'<td class="flags">{flags}</td>'
            f'<td class="num">{_e(format_duration(row["running"]))}</td>'
            f'<td class="num">{row["vamps"] or ""}</td>'
            f'<td class="num">{row["holds"] or ""}</td>'
            f'<td class="num">{row["cues"] or ""}</td>'
            f'<td>{_e(row["tempo"])}</td><td>{_e(row["key"])}</td>'
            f'<td>{_e(row["osc_cue"])}</td></tr>')
        if include_notes and row["notes"]:
            items = "".join(
                f"<li><b>{_e(n['dept'])}</b>"
                + (f" ({_e(n['cue'])})" if n["cue"] else "")
                + f": {_e(n['text'])}</li>"
                for n in row["notes"][:NOTES_PER_SONG_LIMIT])
            extra = len(row["notes"]) - NOTES_PER_SONG_LIMIT
            if extra > 0:
                items += f"<li>... {extra} more</li>"
            bodies[-1].append(f'<tr class="notes"><td></td><td colspan="{ncols - 1}"><ul>{items}</ul></td></tr>')
    body_html = "".join(
        ('<tbody class="act-break">' if i else "<tbody>") + "".join(rows) + "</tbody>"
        for i, rows in enumerate(bodies) if rows or i == 0)
    title = data["show_name"]
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{_e(title)} - Running order</title><style>{_CSS}</style></head><body>"
        f"<h1>{_e(title)}</h1>"
        f"<div class=\"meta\">Running order - generated {_e(data['generated'])} by LoopStation</div>"
        f"<table><thead><tr>{head}</tr></thead>{body_html}</table>"
        f"<div class=\"totals\">{_e(_totals_text(data))}</div>"
        "</body></html>\n")


# --------------------------------------------------------------- export --

def export_running_order(state, path: str, fmt: str = "html", include_notes: bool = True,
                         page_break_per_act: bool = False) -> str:
    """
    Write the running order of ``state``'s show to ``path`` (atomically).
    ``fmt`` is "html" or "csv" (UTF-8 with BOM for Excel). Returns ``path``.
    Raises ValueError for an unknown format, OSError when writing fails.
    """
    fmt = (fmt or "").lower()
    if fmt not in FORMATS:
        raise ValueError(f"unknown running-order format {fmt!r}")
    data = build_running_order(state)
    if fmt == "csv":
        text = render_csv(data, include_notes=include_notes)
    else:
        text = render_html(data, include_notes=include_notes, page_break_per_act=page_break_per_act)
    write_text_atomic(path, text)
    logger.info(f"Running order exported to {path}")
    return path


def open_in_browser(path: str) -> bool:
    """Open an exported file in the default browser (file:// URI). Never raises."""
    try:
        uri = pathlib.Path(path).resolve().as_uri()
        return bool(webbrowser.open(uri))
    except Exception as e:
        logger.error(f"Could not open {path} in a browser: {e}")
        return False
