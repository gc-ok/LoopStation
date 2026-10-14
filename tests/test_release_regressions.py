"""Regression coverage for the eleven performance-readiness findings."""
import json
import pathlib
import shutil
import subprocess
import threading

import numpy as np
import pytest

from backend import audio_engine, state_manager, web_server
from backend.audio_engine import AudioEngine, build_tempo_pitch_filter
from backend.mixer_core import MixerCore, build_loop_body
from backend.osc_output import OscOutput
from backend.state_manager import StateManager, Marker
from fake_engine import FakeEngine
from helpers_audio import SR, fake_stream_factory, make_signal, write_wav
from utils import preferences


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, 'PREFS_FILE', str(tmp_path / 'prefs.json'))
    monkeypatch.setattr(StateManager, '_start_monitor', lambda self: None)
    sm = StateManager(engine=FakeEngine(), data_file=str(tmp_path / 'show.json'))
    assert sm.load_song(write_wav(tmp_path / 'song.wav', make_signal(5)))
    yield sm
    sm.cleanup()


@pytest.mark.parametrize('crossfade_ms', [0, 20, 100])
def test_rhythmic_loop_keeps_beat_spacing_for_many_passes(crossfade_ms):
    # Interior clicks must recur every two seconds, regardless of seam length.
    src = np.zeros((SR * 5, 2), dtype=np.int16)
    src[int(SR * 1.5)] = 20000
    core = MixerCore(SR, declick_ms=0)
    core.set_stems({'mix': src})
    core.arm_loop(1, 3, crossfade_ms)
    core.play(1)
    out = core.render(SR * 20 + 1)
    clicks = np.flatnonzero(out[:, 0] > 0.5)
    assert len(clicks) >= 10
    assert np.all(np.diff(clicks) == SR * 2)
    assert core.loop_duration == 2


def test_seam_at_eof_preserves_length_and_continuity():
    src = np.full((1000, 2), 10000, dtype=np.int16)
    body, cf = build_loop_body(src, 100, 1000, 30)
    assert len(body) == 900 and cf == 30
    assert np.all(body == 10000)
    core = MixerCore(1000, declick_ms=0)
    core.set_stems({'mix': src})
    core.arm_loop(0.1, 1, crossfade_ms=30)
    core.play(0.9)
    assert np.all(core.render(2000) > 0)
    assert core.mode == 'loop'


@pytest.mark.parametrize('hold', [2.99, 3.0])
def test_hold_at_vamp_seam_precedes_entry(hold):
    core = MixerCore(SR, declick_ms=0)
    core.set_stems({'mix': make_signal(5)})
    core.arm_loop(1, 3, crossfade_ms=20)
    core.hold_at(hold)
    core.play(2.9)
    core.render(SR)
    assert core.is_held and core.mode == 'transport'
    assert core.get_position() == pytest.approx(hold, abs=1 / SR)
    core.release_hold()
    core.render(SR)
    assert core.mode == 'loop'


def test_mac_allowlist_covers_render_filters_and_ci_tests_bundle():
    workflow = pathlib.Path('.github/workflows/build.yml').read_text(encoding='utf-8')
    allowlist = workflow.split('--enable-filter=', 1)[1].split()[0].split(',')
    required = {part.split('=', 1)[0] for part in build_tempo_pitch_filter(0.8, 2).split(',')}
    assert required <= set(allowlist)
    assert 'LOOPSTATION_FFMPEG: ${{ env.FFMPEG_PATH }}' in workflow


def test_failed_save_remains_pending_and_recovers_without_an_edit(manager, monkeypatch):
    manager.flush_save()
    previous = pathlib.Path(manager.data_file).read_bytes() if pathlib.Path(manager.data_file).exists() else None
    manager.add_marker(1, 'New cue')
    failures, successes = [], []
    manager.on('save_failed', failures.append)
    manager.on('save_succeeded', lambda: successes.append(True))
    original = state_manager.os.replace

    def fail(*args):
        raise PermissionError('disk unavailable')

    with monkeypatch.context() as mp:
        mp.setattr(state_manager.os, 'replace', fail)
        assert manager.flush_save() is False
    assert manager._save_dirty and failures and not successes
    if previous is not None:
        assert pathlib.Path(manager.data_file).read_bytes() == previous
    assert state_manager.os.replace is original
    assert manager.flush_save() is True
    saved = json.loads(pathlib.Path(manager.data_file).read_text())
    assert saved[manager.current_song_id]['markers'][0]['name'] == 'New cue'
    assert not manager._save_dirty and manager.last_save_error is None and successes


def test_mix_components_default_to_safe_opt_in_and_report_clipping():
    src = np.full((SR, 2), 20000, dtype=np.int16)
    core = MixerCore(SR, declick_ms=0)
    core.set_stems({'mix': src, 'instrument': src.copy(), 'click': np.zeros_like(src)})
    stems = {st.name: st for st in core.get_stems()}
    assert stems['instrument'].muted and not stems['mix'].muted and not stems['click'].muted
    core.play(0)
    assert np.allclose(core.render(512), 20000 / 32768)
    assert core.clipped_blocks == 0
    core.set_stem_mute('instrument', False)
    core.render(512)
    assert core.output_peak > 1 and core.clipped_blocks == 1


def test_explicit_source_selection_preserves_click_and_saved_choices(manager):
    fake = manager.audio
    fake._stems.append({'name': 'piano', 'gain_db': 0, 'muted': True, 'solo': False})
    fake._stems.append({'name': 'click', 'gain_db': 0, 'muted': True, 'solo': False})
    assert manager.set_playback_source('Stems')
    assert fake._stem('mix')['muted'] and not fake._stem('piano')['muted']
    assert fake._stem('click')['muted']
    assert manager.set_playback_source('Full mix')
    assert not fake._stem('mix')['muted'] and fake._stem('piano')['muted']
    manager.flush_save()
    assert manager.settings.stems['piano']['muted']


def test_countdown_uses_applied_rate_and_is_unknown_beyond_hold(manager):
    manager.set_tempo_pitch(0.5, 0)
    marker = Marker(4, name='Next')
    shared = web_server.SharedCueState()
    shared.update_from_app(manager, 2, None, None, marker, 'marker')
    payload = shared.get_state()
    assert payload['playback_rate'] == 0.5
    assert payload['next']['countdown'] == 4
    manager.add_hold(3)
    shared.update_from_app(manager, 2, None, None, marker, 'marker')
    assert shared.get_state()['next']['countdown'] is None


def test_countdown_accounts_for_counted_vamps_and_indefinite_waits(manager):
    manager.add_loop(1, 2)
    manager.update_selected_loop(repeat_count=3)
    assert manager.countdown_to(4, 0) == 6
    manager.update_selected_loop(repeat_count=0)
    assert manager.countdown_to(4, 0) is None
    assert manager.countdown_to(1.5, 0) == 1.5


def test_failed_tempo_request_never_persists_unapplied_settings(manager, monkeypatch):
    manager.set_tempo_pitch(0.9, -1)

    def fail(*args):
        raise RuntimeError('missing filter')

    monkeypatch.setattr(manager.audio, 'set_tempo_pitch', fail)
    assert manager.set_tempo_pitch(0.5, 2) is False
    assert manager.get_tempo_pitch() == (0.9, -1)
    assert (manager.settings.tempo_ratio, manager.settings.semitones) == (0.9, -1)
    assert manager.pending_tempo_pitch is None
    manager.flush_save()
    data = json.loads(pathlib.Path(manager.data_file).read_text())[manager.current_song_id]
    assert data['settings']['tempo_ratio'] == 0.9


def test_async_failed_request_and_pending_save_keep_actual_settings(manager, monkeypatch):
    monkeypatch.setattr(manager.audio, 'set_tempo_pitch',
                        lambda *a: manager.audio._fire('render_started', {}))
    assert manager.set_tempo_pitch(0.5, 2)
    assert manager.pending_tempo_pitch == (0.5, 2)
    manager.save_loop(immediate=True)
    assert manager.settings.tempo_ratio == 1
    manager.audio._fire('render_failed', {'message': 'failed'})
    assert manager.settings.tempo_ratio == 1 and manager.pending_tempo_pitch is None


def test_real_engine_failed_render_reports_applied_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_engine, 'get_temp_dir', lambda: str(tmp_path))
    engine = AudioEngine(ffmpeg_path='no-such-ffmpeg', stream_factory=fake_stream_factory(False, []))
    try:
        engine.load_file(write_wav(tmp_path / 'song.wav', make_signal(1)))
        seen = threading.Event()
        engine.on_event(lambda event, data: seen.set() if event == 'render_failed' else None)
        engine.set_tempo_pitch(0.5, 2)
        assert seen.wait(2)
        assert engine.get_tempo_pitch() == (1, 0)
    finally:
        engine.close()
    assert not engine._event_thread.is_alive()


def test_manager_final_cleanup_joins_audio_and_osc_workers(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, 'PREFS_FILE', str(tmp_path / 'prefs.json'))
    monkeypatch.setattr(audio_engine, 'get_temp_dir', lambda: str(tmp_path))
    engine = AudioEngine(stream_factory=fake_stream_factory(False, []))
    manager = StateManager(engine=engine, data_file=str(tmp_path / 'data.json'))
    manager.osc.close()
    manager.osc = OscOutput(client_factory=lambda *a: object())
    manager.osc.configure(True, '127.0.0.1', 12345)
    worker = manager.osc._worker
    manager.cleanup()
    manager.cleanup()
    assert not engine._event_thread.is_alive() and not worker.is_alive()


def test_closing_during_load_cannot_reopen_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_engine, 'get_temp_dir', lambda: str(tmp_path))
    streams = []
    engine = AudioEngine(ffmpeg_path='no-such-ffmpeg', stream_factory=fake_stream_factory(False, streams))
    path = write_wav(tmp_path / 'song.wav', make_signal(1))
    started, release = threading.Event(), threading.Event()
    decode = engine._decode
    errors = []

    def blocked_decode(path):
        started.set()
        assert release.wait(3)
        return decode(path)

    def load():
        try:
            engine.load_file(path)
        except RuntimeError as error:
            errors.append(str(error))

    monkeypatch.setattr(engine, '_decode', blocked_decode)
    loader = threading.Thread(target=load)
    loader.start()
    try:
        assert started.wait(2)
        engine.close()
    finally:
        release.set()
        loader.join(3)
        engine.close()
    assert not loader.is_alive() and errors
    assert not engine.is_stream_active() and len(streams) == 1
    assert engine.core.n_source_frames == 0


def test_live_edit_methods_block_model_changes(manager):
    marker = manager.add_marker(1)
    manager.set_item_tag_note(marker.id, 'Sound', 'old')
    manager.set_live_mode(True)
    assert manager.set_song_tempo_map(180, 3, 1) is False
    assert manager.set_song_osc_cue('99') is False
    assert manager.set_item_tag_note(marker.id, 'Sound', 'new') is False
    assert manager.remove_item_tag(marker.id, 'Sound') is False
    assert manager.set_item_osc_cue(marker.id, '99') is False
    assert manager.settings.bpm == 0 and manager.settings.osc_cue == ''
    assert marker.tag_notes == {'Sound': 'old'} and marker.osc_cue == ''


def test_monitor_clock_disconnect_pause_tempo_and_timeout():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for executing monitor JavaScript')
    script = pathlib.Path(__file__).with_name('monitor_regression.js')
    result = subprocess.run([node, str(script)], input=web_server.MONITOR_HTML,
                            text=True, encoding='utf-8', capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'monitor checks passed' in result.stdout
