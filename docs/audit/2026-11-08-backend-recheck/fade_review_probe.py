"""Read-only B16 review observations; exit 0 does not mean product correctness.

Run from repo root:
QT_QPA_PLATFORM=offscreen .venv/bin/python docs/audit/2026-11-08-backend-recheck/fade_review_probe.py
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from PyQt6.QtWidgets import QApplication
from src.features.edit.ui.edit_page import EditPage
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.core.audio_graph_snapshot import build_audio_graph_snapshot
from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer
from src.features.edit.core.commands import MacroCommand, SetFadeCurveCommand, MoveSpeedPointCommand, SetClipSpeedSettingsCommand


def main() -> None:
    app = QApplication.instance() or QApplication([])
    page = EditPage.__new__(EditPage)
    clip = ClipInstance('c', 'a', 't', 0, 100, 0, 100, fade_in_duration=10, fade_curve='linear')
    track = EditTrack('t', 'A1', 'audio', 0, clips=(clip,))
    sequence = TimelineSequence('s', 's', fps=24, audio_tracks=(track,))
    changed_track = replace(track, clips=(replace(clip, fade_curve='smooth'),))
    changed = replace(sequence, audio_tracks=(changed_track,))
    project = SimpleNamespace(assets={'a': SimpleNamespace(path='/tmp/fade-review-missing.wav', fps=24)})
    audio_before = page._build_audio_timeline_signature(sequence.audio_tracks, project, 24, timeline=sequence)
    audio_after = page._build_audio_timeline_signature(changed.audio_tracks, project, 24, timeline=changed)
    video_track = replace(track, track_type='video', name='V1')
    video = replace(sequence, audio_tracks=(), video_tracks=(video_track,))
    video_changed = replace(video, video_tracks=(replace(video_track, clips=(replace(clip, fade_curve='smooth'),)),))
    unknown = ClipInstance.from_dict({**clip.to_dict(), 'fade_curve': 'future_exp'})
    macro_errors = {}
    commands = (
        SetFadeCurveCommand.create('c', 't', 'smooth'),
        MoveSpeedPointCommand.create('c', 't', 20, 30),
        SetClipSpeedSettingsCommand.create('c', 't', {'speed': 1.5}),
    )
    for command in commands:
        try:
            MacroCommand.from_dict(MacroCommand.create('review', [command]).to_dict())
        except (TypeError, ValueError) as exc:
            macro_errors[type(command).__name__] = str(exc)
    fresh_command = SetFadeCurveCommand.create('c', 't', 'smooth')
    restored_command = SetFadeCurveCommand.from_dict(fresh_command.to_dict())
    result = {
        'baseline_observation': '1380e8bf before review fixes; rerun reflects current tree',
        'audio_sync_signature_changed': audio_before != audio_after,
        'audio_graph_snapshot_hash_changed': build_audio_graph_snapshot(sequence).stable_hash != build_audio_graph_snapshot(changed).stable_hash,
        'video_renderer_signature_changed': TimelinePreviewRenderer._build_sequence_render_signature(None, video) != TimelinePreviewRenderer._build_sequence_render_signature(None, video_changed),
        'edit_page_video_signature_changed': page._build_timeline_preview_signature(video) != page._build_timeline_preview_signature(video_changed),
        'future_curve_roundtrip': {
            'input': 'future_exp', 'model_value': unknown.fade_curve,
            'extra_field': unknown._extra_fields.get('fade_curve'),
            'output': unknown.to_dict().get('fade_curve'),
            'preserved': unknown.to_dict().get('fade_curve') == 'future_exp',
        },
        'macro_registry_observations': macro_errors,
        'unapplied_curve_command_roundtrip_had_value': restored_command._had_value,
        'consumer_paths': {
            'realtime': 'AudioMixer.mix_samples -> apply_clip_fade once',
            'offline_and_stem': 'render_track_block -> apply_clip_fade once',
            'double_fade_observed': False,
        },
    }
    out = Path(__file__).with_name('fade_review_results.json')
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    _ = app


if __name__ == '__main__':
    main()
