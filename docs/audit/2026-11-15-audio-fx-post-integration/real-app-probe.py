"""Run the existing full-app benchmark with the user's video and two new FX.

This is an explicit interactive benchmark, not a pytest test. It opens a
separate MainWindow and AudioSink. Config/project inputs live in a temporary
folder. The benchmark force-exits at the end; natural shutdown is not tested.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import wave

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
ARTIFACTS = Path(__file__).resolve().parent
os.environ['TOPOS_AUDIO_FX'] = 'heavy'
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')


def main() -> None:
    import av
    import numpy as np
    folder = Path(tempfile.mkdtemp(prefix='topos-fx-post-integration-'))
    config_path = ROOT / '.topos/config.json'
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    config['auto_save_enabled'] = False
    task_config = folder / 'config.json'
    task_config.write_text(json.dumps(config))
    os.environ['TOPOS_CONFIG_PATH'] = str(task_config)
    video = ROOT / 'tests/test_videos/2K_h264.mp4'
    resampler = av.AudioResampler(format='fltp', layout='stereo', rate=48000)
    chunks = []
    count = 0
    with av.open(str(video)) as container:
        for frame in container.decode(audio=0):
            for converted in resampler.resample(frame):
                audio = converted.to_ndarray()
                chunks.append(audio)
                count += audio.shape[-1]
            if count >= 12 * 48000:
                break
    pcm = np.concatenate(chunks, axis=-1)[:, :12 * 48000]
    source = folder / 'video-audio.wav'
    with wave.open(str(source), 'wb') as file:
        file.setnchannels(2)
        file.setsampwidth(2)
        file.setframerate(48000)
        file.writeframes(np.rint(np.clip(pcm.T, -1.0, 1.0) * 32767).astype(np.int16).tobytes())

    driver_path = ROOT / 'tests/benchmark/drive_real_app_audio_tracks.py'
    driver = types.ModuleType('topos_review_real_app_driver')
    driver.__file__ = str(driver_path)
    events = []
    mix_times = []
    editors = {}
    mode = os.environ.get('TOPOS_POST_FX_PROBE_MODE', 'both')
    state = {'temporary_folder': str(folder), 'source_video': str(video), 'mode': mode}

    def chain():
        from src.features.edit.core.timeline_v2 import (
            EffectChainParams, TrackEffectInstance, VoiceChangerParams, SpectralManglerParams,
        )
        if mode == 'none':
            return EffectChainParams()
        return EffectChainParams(effects=(
            TrackEffectInstance('voice_changer', VoiceChangerParams(
                pitch_enabled=True, pitch_semitones=4.0,
                formant_shift_st=2.0, random_seed=2026).to_dict(), instance_id='review-voice'),
            TrackEffectInstance('spectral_mangler', SpectralManglerParams(
                ring_enabled=True, ring_mode='dual', ring_mix_pct=35.0,
                dist_enabled=True, dist_drive_db=6.0, dist_mix_pct=20.0,
                random_seed=77).to_dict(), order=1, instance_id='review-mangler'),
        ))

    def install(mw, manager):
        from src.features.edit.core.audio_playback import AudioMixer
        original = AudioMixer.mix_samples

        def timed(mixer, *args, **kwargs):
            before = time.perf_counter()
            result = original(mixer, *args, **kwargs)
            mix_times.append((time.perf_counter() - before) * 1000.0)
            return result

        AudioMixer.mix_samples = timed
        mw._sync_audio_mixer_from_current_timeline()
        for strip in mw.audio_mixer_panel._track_mixers.values():
            for inst in getattr(strip._effect_chain, 'effects', ()):
                if inst.effect_type == 'voice_changer':
                    strip._open_voice_changer_editor(inst)
                    editors['voice'] = strip._voice_changer_dialogs[inst.instance_id]
                elif inst.effect_type == 'spectral_mangler':
                    strip._open_spectral_mangler_editor(inst)
                    editors['mangler'] = strip._spectral_mangler_dialogs[inst.instance_id]
        state['opened_editors'] = list(editors)

        def observe(payload):
            for tid, levels in payload.items():
                taps = levels.get('taps', {})
                if 'review-voice' in taps:
                    voice = taps['review-voice']
                    mangler = taps.get('review-mangler', {})
                    events.append({'track': tid, 'voice_out_db': voice.get('out_db'),
                                   'mangler_out_db': mangler.get('out_db'),
                                   'voice_f0': voice.get('analysis', {}).get('f0_in_hz')})
        manager.audio_track_levels_changed.connect(observe)

    def finish(mw, manager):
        from PyQt6.QtWidgets import QApplication
        until = time.perf_counter() + 0.2
        while time.perf_counter() < until:
            QApplication.processEvents()
            time.sleep(0.005)
        state['emissions'] = len(events)
        state['first'] = events[0] if events else None
        state['last'] = events[-1] if events else None
        state['level_ranges'] = {
            key: [min(values), max(values)]
            for key in ('voice_out_db', 'mangler_out_db')
            if (values := [event[key] for event in events if event[key] is not None])
        }
        state['mix_ms'] = ({'p50': float(np.percentile(mix_times, 50)),
                            'p99': float(np.percentile(mix_times, 99)),
                            'max': max(mix_times), 'n': len(mix_times)} if mix_times else {})
        state['ring_stats'] = manager.ring_stats()
        state['paused_meters'] = {
            'voice': editors['voice']._widget.meter_output._value if 'voice' in editors else None,
            'mangler': editors['mangler']._widget.master_strip._out_db if 'mangler' in editors else None,
        }
        filename = 'real-app-none-result.json' if mode == 'none' else 'real-app-result.json'
        (ARTIFACTS / filename).write_text(json.dumps(state, indent=2))
        print('POST_FX_MAINWINDOW_RESULT', json.dumps(state), flush=True)

    driver.__dict__.update(_review_install=install, _review_finish=finish)
    text = driver_path.read_text()
    text = text.replace('if isinstance(w, QDialog) and w.isVisible():',
        'if isinstance(w, QDialog) and w.isVisible() and type(w).__name__ not in ("VoiceChangerDialog", "SpectralManglerDialog"):')
    marker = '    audio_mgr = getattr(mw.edit_page, "_audio_manager", None)'
    assert marker in text
    text = text.replace(marker, marker + '\n    _review_install(mw, audio_mgr)', 1)
    assert '    mw.close()\n' in text
    text = text.replace('    mw.close()\n', '    _review_finish(mw, audio_mgr)\n    mw.close()\n', 1)
    exec(compile(text, str(driver_path), 'exec'), driver.__dict__)
    driver._heavy_fx_chain = chain
    sys.argv = [str(driver_path), str(video), str(source), '8']
    driver.main()


if __name__ == '__main__':
    main()
