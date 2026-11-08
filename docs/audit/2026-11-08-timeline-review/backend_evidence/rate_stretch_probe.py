"""Read-only disposable Qt fixture: mixed-rate stretch uses timeline duration."""
from __future__ import annotations

import json
from PyQt6.QtWidgets import QApplication
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.ui.timeline_view import TimelineView
from src.shared.media.metadata import Rational


def main() -> None:
    app = QApplication([])
    clip = ClipInstance(
        "v", "asset", "v1", 0, 100, 0, 200,
        speed=SpeedParams(reverse=True, preserve_pitch=True),
        _source_fps_num=48, _source_fps_den=1,
        _timeline_fps_num=24, _timeline_fps_den=1,
    )
    sequence = TimelineSequence("rate-stretch", "Rate stretch", fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(clip,)),))
    view = TimelineView()
    view.load_timeline(sequence)
    view._on_rate_stretch_finished("v", "v1", 100, 50)
    changed = view.get_timeline().get_clip_by_id("v")
    result = {"requested_timeline_duration": 50, "actual_timeline_duration": changed.timeline_duration,
              "expected_speed": 2.0, "actual_speed": changed.speed.speed,
              "reverse_before": True, "reverse_after": changed.speed.reverse,
              "pitch_before": True, "pitch_after": changed.speed.preserve_pitch}
    print(json.dumps(result, indent=2))
    assert changed.timeline_duration != 50
    assert not changed.speed.reverse and not changed.speed.preserve_pitch
    view.close()
    app.processEvents()


if __name__ == "__main__":
    main()
