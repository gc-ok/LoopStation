# LoopStation — notes for Claude

Seamless audio loop / vamp player for live theater (Python, customtkinter UI,
sounddevice playback, ffmpeg tempo/key renders, Flask backstage phone monitor,
MIDI in, OSC out). Entry point: `main.py`.

## Platforms: Windows AND macOS

- **Every change must work on both Windows and macOS.** Windows cannot be
  tested from here, and the owner tests on Mac by hand, later. So write
  cross-platform code deliberately and say in your report what still needs a
  real-machine check.
- Guard platform code with `sys.platform`. Existing examples:
  `utils/paths.py`, `utils/tooltip.py`, `tk::mac::Quit` in `frontend/app.py`.
- Windows specifics:
  - Files can be locked by antivirus or OneDrive, so `open` and
    `os.replace` can raise `PermissionError`. Use the retry helpers in
    `utils/fileio.py`.
  - A file that is still memory-mapped can't be deleted.
  - Subprocesses need `CREATE_NO_WINDOW`.
  - `SO_REUSEADDR` lets two servers share a port.
  - MIDI port names get numeric suffixes ("Pedal 1").
  - `sys.stdout`/`stderr` are `None` in windowed builds.
  - Plain tk widgets don't follow customtkinter's HiDPI scaling.
- Always pass `encoding="utf-8"` when opening files, and use
  `os.path`/`pathlib` for paths.

## Environment and commands

- Venv: `.venv` (gitignored). Local Python is Homebrew 3.14, which needed
  `brew install python-tk@3.14` for Tk. CI uses Python 3.12
  (`.github/workflows/build.yml`), so avoid 3.13+-only syntax and APIs.
- The pygame build for 3.14 has no portmidi, so MIDI input needs
  `python-rtmidi`. It's in `requirements.txt`, but on 3.14 it builds from
  source and needs a C++ compiler.
- Tests: `.venv/bin/python -m pytest -q`
- Lint (CI runs it): `.venv/bin/python -m pyflakes backend frontend utils main.py config.py`
- `tests/monitor_regression.js` (phone monitor JS) needs Node. Node isn't
  installed here, so that test is skipped locally but runs in CI.
- Running from source, user data goes in `<repo>/data/`. Packaged builds use
  `~/Library/Application Support/LoopStation` (Mac) or
  `%LOCALAPPDATA%\LoopStation` (Windows). See `utils/paths.py`.

## Test gotchas

- The test process here can't take keyboard focus. Synthetic key events are
  dropped or land in the wrong window, so use `send_key()` from
  `tests/test_frontend_smoke.py`.
- To reuse its fixtures, import names explicitly
  (`from tests.test_frontend_smoke import _app, ui, pump, send_key, _Key`).
  Never use `import *`, or pytest collects that module's tests twice.
- Each frontend test module shares a single app (one Tcl interpreter). Tests
  must pass in any file order; check by running the frontend test files in
  reverse order.
- `tests/fake_engine.py` is a fake audio engine for state and UI tests.

## Design rules (keep consistent)

- **Threads:** Tk is only touched on the Tk thread. Audio, MIDI, web and
  detector threads post work through `msg_queue`/`after()`.
- **Audio callback (`backend/mixer_core.py`):**
  - It must stay real-time safe: no thread starts, blocking locks, file I/O
    or heavy allocation.
  - Loop-body rebuilds and page prefetch run on long-lived workers.
  - `docs/audio-engine-api.md` is the engine contract. Keep it in sync.
- **Live Mode is intentional and limited:**
  - Allowed: GO (including GO to the next song and segues), play/pause/stop,
    next/prev cue (key or MIDI), picking the "next up" song in the Show
    (`set_next_up`), saving the Show, and TAKE OVER on a backup. The test
    `test_live_mode_blocks_rehearsal_navigation` documents this.
  - The owner asked "what did you do to live mode?" when the extra blocks
    were added, so explain any Live Mode change plainly and get approval
    first.
  - Blocked: arbitrary seeks (waveform click, Go to bar, hold jump, cue-sheet
    click jumps through `jump_to_marker`/`play_from`), editing, key/MIDI/OSC
    remapping, mirror settings, and opening or reordering a Show.
  - Separately, a backup that is following its main has its own transport
    locked until TAKE OVER.
  - The rule is enforced in `state_manager` (`_blocked()`) as well as in the
    UI.
- **UI styling:**
  - Fonts come from `utils/fonts.py` (`FONT_UI`, `FONT_MONO`). Never write a
    font family name: "Segoe UI"/"Consolas" don't exist on macOS, and Tk's
    proportional fallback made the clock jitter. Use `FONT_MONO` for
    clocks, timecodes and counters.
  - Surfaces come from the theme (`config.load_theme()`): window
    `COLOR_BG_DARK` < panel `COLOR_BG_MEDIUM` < raised control
    `COLOR_BG_LIGHT`, plus `COLOR_BORDER`, `COLOR_HOVER`,
    `COLOR_BTN_DISABLED`, `COLOR_TEXT_DISABLED`. For a coloured state
    surface use `tint(color, amount)`, never a fixed dark hex, which
    breaks the light themes.
  - Use text or monochrome glyphs (◆ ↻ ⏸ ▶ ⏹) in controls, not colour
    emoji, because they render differently on each platform.
- **Saved data is precious:**
  - Never overwrite `loop_data.json` or preferences that failed to read.
    Quarantine only real parse errors.
  - Malformed items are kept in `_unreadable` and carried forward on save.
  - Write atomically through `utils/fileio.py`.
- **Minimum vamp length:** vamps shorter than `config.MIN_LOOP_DURATION` are
  rejected, both in the engine and in `state_manager`.
- **OSC cue numbers** are always passed through
  `backend.osc_output.sanitize_osc_cue` before they are stored or sent.
- **MIDI mapping keys** look like `note_on:<ch>:<n>`, `cc:<ch>:<n>`,
  `pc:<ch>:<p>`, with the channel as 1–16. Old keys without a channel still
  match any channel. Use `describe_key()` to display one.
- **Web monitor:**
  - The access token lasts for the whole process; `revoke_token()` resets it.
  - The port falls back from 8080 to 8090. Read it from the server, never
    hard-code it.
  - ACKs belong to an `ack_epoch`, which changes on song change, on stop and
    when a cue comes up again.
- **GO at the end of a song** (it played out, or a fade exit finished)
  loads the next-up song of the Show and starts it. This works in Live Mode
  too (the UI handles `go_next_entry` -> `load_entry(id, from_go=True)` ->
  `play_after_go_load()`; it ignores `go_next_song` while the Show has
  entries).
  - At the end of the Show, GO does nothing.
  - GO never restarts the same song from 0:00; Play replays it.
  - `next_song_provider` is only a fallback for an empty Show (the UI sets
    it to None).
- **Shows (setlists, `backend/show.py`):**
  - `StateManager.show` is always a `Show`. When it has entries, GO at the
    end of a song goes to `next_up_entry_id` (operator's `set_next_up`, else
    the next playable entry); an empty show falls back to
    `next_song_provider`.
  - Cue data stays per song (fingerprint) in `loop_data.json`; a show holds
    only order, titles and the `cut` / `segue` flags. One file may appear in
    several entries (reprise).
  - Live Mode: open/new show and every order edit are blocked via
    `_blocked()`; `set_next_up`, GO advance, segue and saving are allowed.
  - `.loopshow` files store paths relative to the show file (`/`
    separators) plus the absolute path; malformed entries are kept in
    `_unreadable`. Folder-derived shows autosave to
    `<data dir>/shows/<hash>.loopshow`.
  - The next-up song is preloaded with its saved tempo/key on a worker
    thread (`preload_file`); `load_file` gets `tempo_pitch` so a matching
    preload skips the load-time render.
- **Mirroring (main/backup, `backend/mirror.py`, `StateManager.mirror`):**
  - Main sends discrete actions (hooked via `StateManager._mirror_emit`) plus
    a 200 ms heartbeat over newline-delimited JSON on TCP 53100 (PIN,
    64 KB line cap). Backups apply them on ONE applier thread inside
    `_mirror_quiet()` (no re-emit; internal entry points skip Live Mode
    locks, e.g. `_mirror_load` uses the `from_go` path).
  - A backup never stops audio because the network dropped ("MAIN LOST"
    keeps playing); drift seeks only when both are playing in transport
    (never in a vamp, hold, fade, or within 1 s of an action).
  - New state actions that change playback should call `_mirror_emit`;
    internal sub-steps (e.g. the `stop()` inside `load_song`) are wrapped
    in `_mirror_quiet()`. Running-order export is `backend/running_order.py`.

## Status (as of 2026-10-14)

- Built on 2026-10-14, all untested on real hardware or Windows:
  - Shows: `frontend/show_sidebar.py` replaces the folder library.
  - Next-song preload for instant GO.
  - Segues.
  - Main/backup mirroring.
  - Printable running order.
  - Drag-and-drop through the optional `tkinterdnd2`.
  Packaged builds bundle `tkinterdnd2` and `rtmidi` (see
  `.github/workflows/build.yml`).
- A full review found about 50 issues. All are fixed in the working tree and
  covered by `tests/test_review_fixes_{state,audio,io,frontend}.py`. These
  changes have NOT been tested on real audio or MIDI hardware, a packaged
  build, Windows, or the phone-monitor JS under Node.
- Feature backlog from market research, highest value for effort first:
  1. Bar/beat-quantized vamp exit.
  2. Count-in / pre-roll for Go to bar.
  3. OSC *input* (remote GO).
  4. Export the cue sheet, notes and GO log (PDF/CSV).
  5. Score measure numbers and rehearsal letters.
  6. ~~Setlist with segues.~~ Done (Shows).
  7. Emergency "vamp here".
  8. Export cast rehearsal tracks.
  9. Alternate cuts per song.
  10. ~~Main/backup mirroring.~~ Done.
  11. MIDI timecode/LTC output.
  12. Spoken guide cues.
  13. Step-up practice loop.
  14. Live conductor-follow tempo.
- Deliberately not done yet: undo, delete confirmations, waveform pan/zoom,
  and editing notes for future cues.
