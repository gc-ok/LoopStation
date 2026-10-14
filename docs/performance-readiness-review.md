# Performance readiness review

Initially reviewed October 14, 2026, against the working tree, including its uncommitted and untracked application modules. All eleven findings have since been corrected in this working tree. The sections below preserve the initial evidence; their faulty reproduction outputs describe the version reviewed, not the corrected implementation.

## Resolution and regression coverage

| ID | Implemented change | Regression evidence |
|----|--------------------|---------------------|
| LS-01 | Full-length bodies, context blended into the head, exact boundary entry/exit, compatible total-play counting. | Repeated interior clicks retain two-second spacing across ten passes with 0/20/100 ms seams; EOF continuity; source-preserving tempo swaps. |
| LS-02 | Required filters enabled; `LOOPSTATION_FFMPEG` points tests at the workflow's bundled binary. | Build allowlist checked against generated filter chains; existing FFmpeg tempo/pitch integration test. Packaged Mac execution remains outstanding. |
| LS-03 | Dirty state survives failure; save error/success events reach the UI; Save flushes before claiming success. | Injected replacement failure preserves the previous file; retry succeeds without another edit; real UI displays failure and recovery. |
| LS-04 | Stale timing hidden, request abort timeout, playback-update freshness checking, recovery on fresh data. | Actual monitor functions executed in Node for network errors, hung requests, missing polls, frozen desktop timestamps, and reconnection. |
| LS-05 | Component stems opt in; source selector switches without summing outgoing/incoming sources; peak/clipping feedback. | Full mix plus component defaults do not clip; deliberate custom summing is detected; click choices preserved; real dialog feedback. |
| LS-06 | Holds precede entry at the actual loop-end sample; arming is retained when a callback ends exactly there. | Core and real-engine/state-manager tests for holds at 2.990 and 3.000 seconds in a 1–3 second vamp, followed by GO. |
| LS-07 | Applied-rate conversion, remaining counted passes, unknown arrival beyond indefinite waits. | Half-speed phone payload, counted vamps, holds, indefinite vamps, and browser interpolation. |
| LS-08 | Paused/held/stopped status overrides loop mode. | Actual JavaScript confirms a paused vamp stays paused with a frozen countdown. |
| LS-09 | Requested values remain pending; applied settings persist only after a buffer swap; failures reconcile to actual playback. | Synchronous/asynchronous failures and persistence; real engine failed render; pending versus applied hot-swap metadata. |
| LS-10 | State-manager final cleanup calls audio close and OSC close; owned workers are joined. | Idempotent final cleanup leaves audio-event and OSC workers stopped. |
| LS-11 | Song controls disabled and handlers guarded; model rejects tempo-map, song/per-item OSC, and note edits. | Backend mutation attempts and real Song dialog checks while Live Mode is active. |

The permanent regression checks are in [`test_release_regressions.py`](../tests/test_release_regressions.py), [`monitor_regression.js`](../tests/monitor_regression.js), and the expanded audio, integration, and frontend tests. The historical temporary probes asserted defects and are superseded by these tests of the corrected behavior.

## Verification after fixes

- Full suite: **241 passed in 51.59 seconds**, with no skipped tests, on Windows/Python 3.13.6.
- Pyflakes: passed for backend, frontend, utilities, entry point, configuration, and the new regression module.
- Real frontend widgets, real engine/state-manager integration with fake streams, actual FFmpeg rendering, and the actual monitor JavaScript were exercised.
- Save failures also prevent shutdown/theme restart until persistence can be retried. A load already in progress cannot reopen audio after final close.
- Packaged Mac execution and physical production equipment remain unverified locally; the build workflow now runs its FFmpeg tests against the bundled binary.

## Initial review validation (before fixes)

- Windows, Python 3.13.6; review dependencies and an FFmpeg test binary were installed into a temporary directory, without changing the user's Python installation.
- Existing suite: **216 passed in 42.35 seconds**, including frontend smoke and audio integration tests. No tests were skipped in that run.
- Existing lint command, `python -m pyflakes backend frontend utils main.py config.py`: passed.
- Supplemental probes: **12 passed** for current mixer, persistence, render metadata, monitor JavaScript, cleanup, and model behavior; **1 passed** for Live Mode edits through the actual Song dialog. These probes asserted the faulty behavior to confirm each finding, not the desired behavior after a repair.
- Preferences, saved cues, and decoded-audio caches were redirected to isolated review directories. Device-free streams and synthetic audio were used. No physical output device was exercised.

An earlier suite run had one failure caused by the review harness replacing `get_temp_dir()`, conflicting with the existing cache-location test. Preserving the original function and redirecting its temporary-directory root resolved the harness error; the corrected full run passed all 216 cases.

The initial suite passing did not invalidate the findings. Its loop geometry assertions have now been updated to the full musical period, and regression tests cover the previously missing combinations.

## LS-01: Crossfades shorten the loop's musical period

**Initial review status:** reproduced; the original design behavior that required a musical-timing decision.

**Source:** [`build_loop_body()`](../backend/mixer_core.py), [`loop_duration`](../backend/mixer_core.py), and the loop geometry tests in [`test_audio_engine.py`](../tests/test_audio_engine.py).

The builder returns `audio[start + cf:end]` and blends its last `cf` samples with the head. The body has `end - start - cf` frames. At the original tempo, a marked 2.000-second region with a 20 ms seam therefore repeats every 1.980 seconds. Increasing the seam changes the repeat period further.

Minimal reproduction after installing the application dependencies:

```python
import numpy as np
from backend.mixer_core import MixerCore

core = MixerCore(44100, declick_ms=0)
core.set_stems({"mix": np.zeros((44100 * 5, 2), dtype=np.int16)})
core.arm_loop(1.0, 3.0, crossfade_ms=20)
print(core.loop_duration)  # 1.98, rather than 2.0
```

This proves the period change, not how objectionable it sounds for every track. Add rhythmic fixtures and listen to repeated passages when choosing a duration-preserving strategy. Smooth boundaries and preserved bar lengths need separate acceptance checks.

## LS-02: Mac FFmpeg configuration omits required filters

**Initial review status:** confirmed by source/configuration comparison; packaged Mac execution outstanding.

**Source:** [Mac FFmpeg build flags](../.github/workflows/build.yml) and [`build_tempo_pitch_filter()`](../backend/audio_engine.py).

The Mac build disables filters and enables only `aresample,anull`. A tempo request produces an `atempo` filter, and a pitch request produces `asetrate`, `aresample`, and compensating `atempo` filters. `atempo` and `asetrate` are absent from the configured allowlist.

Enable the required filters, then validate the exact bundled executable with `ffmpeg -filters` and actual tempo/key conversions. CI's current Python tests use the FFmpeg discovered by `tests/helpers_audio.py`; they do not explicitly select the executable identified by the workflow's `FFMPEG_PATH`. A passing suite can therefore exercise a different FFmpeg from the bundled one. Set `LOOPSTATION_FFMPEG` to the bundled executable for those tests.

## LS-03: A failed save clears pending-write state

**Initial review status:** reproduced with an injected filesystem error.

**Source:** [`flush_save()` and `_save_loop_data()`](../backend/state_manager.py).

The probe set a pending save, made `os.replace()` raise `PermissionError`, and called `flush_save()` twice. Only the first call attempted replacement. The destination did not exist, `_save_dirty` was false, and the only failure signal was a log entry.

The problem also applies to other write failures caught by `_save_loop_data()`. A subsequent edit may schedule another attempt, but simply stopping or closing after the failed save does not automatically retry it.

Retain dirty state until successful replacement, emit a save-error event, and test failure followed by recovery without requiring another edit. The frontend Save handler should distinguish a scheduled save from confirmed persistence.

## LS-04: Disconnected backstage timing continues to advance

**Initial review status:** reproduced by running the actual monitor JavaScript functions with a controlled clock and failing fetch.

**Source:** [`animateCountdown()` and `poll()` in the monitor page](../backend/web_server.py).

The probe delivered a playing state with a 10-second countdown, failed the next request, and advanced the local clock four seconds. The connection label changed to `Connection lost (1)`, but the displayed countdown continued to 6 and the playback-animation flag remained true.

The request also has no application timeout. A hanging request leaves `pollInFlight` true, so subsequent scheduled polls return without attempting reconnection until that request settles. That part is a source finding, not a real-network timeout measurement.

Freeze or hide stale timing, give stale state a prominent visual treatment, and abort timed-out requests. Include a last-successful-update timestamp so a responsive server serving old desktop state can also be detected.

## LS-05: Default stem mixing can duplicate the full mix and clip

**Initial review status:** reproduced in the mixer; the problematic case is a primary file containing the same material as its component stems.

**Source:** [`_Stem`, `_effective_gains()`, and `_route()`](../backend/mixer_core.py), [stem loading](../backend/audio_engine.py), and [saved stem-setting application](../backend/state_manager.py).

Every new stem starts unmuted, not soloed, and at 0 dB. The primary `mix` is included along with the discovered component stems. The buses are summed and the final output is hard-clipped to `[-1, 1]`.

The probe supplied two equal stereo signals of integer amplitude 20,000 as `mix` and `instrument`. Together they produced output samples of exactly 1.0, indicating clipping. Muting `mix` restored the component's expected amplitude of approximately 0.61035.

A main track plus an additional click is a different, valid use case. Avoid globally muting the primary file whenever any stem exists. Make the choice between a full mix and a replacement stem mix explicit, and add useful output level/clipping feedback.

## LS-06: Holds inside the entry seam are bypassed while looping

**Initial review status:** reproduced, with a zero-crossfade control case.

**Source:** [`_next_event()` and `_handle_event()`](../backend/mixer_core.py); [`add_hold()`](../backend/state_manager.py) accepts the placement without conflict validation.

Reproduction: arm a vamp from 1.000 to 3.000 seconds with a 20 ms crossfade, arm a hold at 2.990 seconds, start at 2.900, and render two seconds. The core enters loop mode at 2.980 and never emits `hold_reached` while repeating. Repeat with a zero-length crossfade and the hold is reached.

Loop entry starts at `loop_out - crossfade`, and `_next_event()` considers holds only in transport mode. A hold in that overlap is bypassed once the loop is entered; it can still be reached later if transport resumes through that location after vamp exit.

Define whether such a hold should occur before the vamp, inside it, or after release. Implement that precedence, or reject the conflicting placement. Add interaction tests combining holds, skips, and vamp seams rather than testing each feature only in isolation.

## LS-07: Tempo changes do not change countdown units

**Initial review status:** reproduced for the phone payload; the desktop uses the same source-time subtraction by source inspection.

**Source:** [`_item_to_dict()` and monitor interpolation](../backend/web_server.py), [`_update_countdown_only()`](../frontend/notes_sidebar.py), and the mixer's source-time conversion.

At source position 10, with the next marker at source time 20 and applied tempo ratio 0.5, the phone payload supplies countdown 10 rather than approximately 20 playback seconds. The payload carries no playback-rate value. The desktop also displays `next_time - position`. Browser interpolation subtracts one second per wall second regardless of playback rate.

For uninterrupted transport, convert the remaining source interval using the applied rate. Requested tempo metadata can be wrong after a failed render (LS-09), so use the actual active buffers/rate. For cues beyond an unreleased vamp or hold, a definite arrival time may be unavailable; represent that explicitly.

## LS-08: Paused vamps keep the phone clock animated

**Initial review status:** reproduced by executing the actual monitor JavaScript with a paused-loop state.

**Source:** [monitor playback/status calculation](../backend/web_server.py) and [`is_in_loop_mode()`](../backend/state_manager.py).

A paused vamp still has loop mode active. The monitor computes `serverIsPlaying = !held && (data.is_playing || data.is_looping)`, and its status branches check looping before paused. With `state="paused"`, `is_playing=false`, and `is_looping=true`, the probe displayed `Looping` and reduced a 10-second countdown to 8 over two wall seconds.

Paused and held state must override the loop-mode label and animation. Verify pause/resume in both transport and loop mode on the actual phone display.

## LS-09: Failed renders retain requested tempo/key settings

**Initial review status:** reproduced separately in the engine and state manager with injected rendering errors.

**Source:** [`set_tempo_pitch()` and `_render_worker()`](../backend/audio_engine.py), [tempo settings and render-failure handler](../backend/state_manager.py).

The engine sets `_tempo_pitch` before rendering. A failed 50% tempo, +2-semitone request leaves `get_tempo_pitch()` reporting `(0.5, 2)` while the core remains at ratio 1.0 and key 0. The state manager updates its settings before forwarding the request; even a synchronous engine exception returns success and saves the requested settings. Its asynchronous failure handler reports the error but does not reconcile settings.

The existing failure indicator is useful; the unresolved issue is the distinction between desired configuration and actual playback. Track pending/requested and applied values, handle synchronous failure accurately, and define persistence/retry behavior after asynchronous failure.

## LS-10: Final cleanup does not terminate the audio event worker

**Initial review status:** reproduced for `AudioEngine.cleanup()`; the desktop shutdown/restart call path is confirmed by source inspection.

**Source:** [audio cleanup versus close](../backend/audio_engine.py), [state-manager cleanup](../backend/state_manager.py), and [desktop shutdown/theme restart](../frontend/app.py).

After `cleanup()`, the probe joined the event thread briefly and confirmed it remained alive waiting on its queue. Calling `close()` sent the stop sentinel and allowed the thread to terminate. The desktop calls state-manager cleanup, which calls audio cleanup rather than audio close.

Process exit eventually ends daemon workers, but theme changes recreate the app inside the same process. Old event threads can therefore retain engines and callbacks between restarts. Use a final close operation and join workers; keep reusable playback cleanup distinct if needed. Also audit the OSC sender lifecycle, which currently has no public shutdown operation.

## LS-11: Live Mode permits some edits in the real Song dialog

**Initial review status:** reproduced through the actual desktop widgets, using the existing withdrawn-window frontend fixture.

**Source:** [Song dialog controls and `set_live_mode()`](../frontend/song_settings.py), [`set_song_tempo_map()` and `set_song_osc_cue()`](../backend/state_manager.py).

The probe opened Song settings, entered Live Mode, and confirmed tempo/key Apply was disabled. It then saved BPM 180 and song OSC cue `99`; both changes reached the model. The tempo-map and OSC edit controls remain usable, and their backend setters have no Live Mode guard.

Guard edit methods at the model boundary and disable the corresponding controls. Decide explicitly which operational controls remain available during a show. Audit tag-note and per-item OSC edits as well, since those setters also lack model-level guards even where the frontend currently prevents editing.

## Remaining validation

- Build and run the packaged Mac application, including the exact bundled FFmpeg and signing/notarization path.
- Listen to representative rhythmic, sustained, and multistem music through real audio hardware. Synthetic sample checks do not establish audible quality.
- Run long rehearsals with repeated GO, holds, vamps, seek/reload, tempo requests, and theme restarts; monitor latency, underflows, worker counts, memory, and cache growth.
- Test actual MIDI devices and pedals, including disconnect/reconnect and device enumeration changes.
- Test real phones through LAN interruptions, browser backgrounding, and screen wake-lock behavior. JavaScript probes here test logic, not browser permissions or networking.
- Check closely spaced/overlapping holds, skips, and loops, and cues whose OSC event must be delivered at a precise point. Marker OSC dispatch currently follows monitor polling rather than the audio callback.

These are outstanding checks, not additional confirmed failures.
