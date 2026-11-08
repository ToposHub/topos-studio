#!/usr/bin/env python3
"""Read-only product probes. Exit 0 means observations collected, NOT product pass.

Run from the repository with .venv/bin/python. Qt uses an offscreen platform;
no user project, native app, decoder, or renderer is touched.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from dataclasses import replace
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.clip_params import SpeedParams, SpeedPoint
from src.features.edit.core.commands import MoveSpeedPointCommand
from src.features.edit.core.speed_settings import plan_speed_settings_edit
from src.features.edit.core.timeline_v2 import (
    ClipInstance, EditTrack, Rational, TimelineSequence, TransitionInstance,
)


def sequence(video: ClipInstance, audio: ClipInstance | None = None,
             transitions: tuple[TransitionInstance, ...] = (),
             audio_locked: bool = False) -> TimelineSequence:
    return TimelineSequence(
        "speed-review", "Speed review", fps=Rational(24, 1),
        video_tracks=(EditTrack("v", "V1", "video", 0, clips=(video,),
                                transitions=transitions),),
        audio_tracks=(EditTrack("a", "A1", "audio", 0,
                                clips=(audio,) if audio is not None else (),
                                is_locked=audio_locked),),
    )


def settings() -> dict[str, Any]:
    return {"speed": 2.0, "duration_frames": 50, "reverse": False,
            "freeze": False, "preserve_pitch": False, "ripple": False,
            "keyframe_timing": "maintain"}


def main() -> None:
    results: dict[str, Any] = {
        "meaning": "OBSERVATIONS ONLY; exit 0 is not a correctness assertion",
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                         text=True).strip(),
        "issues": [],
    }
    issues = results["issues"]
    video = ClipInstance("v1", "asset", "v", 0, 100, 0, 100)
    linked_video = replace(video, linked_clip_id="a1")
    audio = replace(video, clip_id="a1", track_id="a", linked_clip_id="v1",
                    speed=SpeedParams(speed_points=(SpeedPoint(50, 2.0),)))
    original = sequence(linked_video, audio)
    plan = plan_speed_settings_edit(original, "v", "v1", settings())
    changed = plan.command.apply(original)
    issues.append({
        "id": "S01", "category": "backend", "priority": "P1",
        "problem": "B14 capability preflight omits reciprocal partner",
        "expected": "Reject unsupported partner curves before editing any member",
        "observed": {"plan_executable": plan.executable,
                     "partner_points_before": [p.to_dict() for p in audio.speed.speed_points],
                     "partner_points_after": [p.to_dict() for p in changed.get_clip_by_id("a1").speed.speed_points]},
    })
    tail = TransitionInstance("tail", "cross_dissolve", "v", left_clip_id="v1",
                              center_frame=100, duration=80, alignment="end_on_cut")
    original = sequence(video, transitions=(tail,))
    plan = plan_speed_settings_edit(original, "v", "v1", settings())
    changed = plan.command.apply(original)
    undone = plan.command.undo(changed)
    issues.append({
        "id": "S02a", "category": "backend", "priority": "P1",
        "problem": "SetClipSpeedSettingsCommand undo omits transition snapshots",
        "observed": {"transition_duration_before": 80,
                     "transition_duration_after": changed.video_tracks[0].transitions[0].duration,
                     "transition_duration_undo": undone.video_tracks[0].transitions[0].duration,
                     "clip_duration_undo": undone.get_clip_by_id("v1").timeline_duration},
    })
    curved = replace(video, speed=SpeedParams(speed_points=(SpeedPoint(80, 8.0),)),
                     timeline_duration=82)
    original = sequence(curved, transitions=(replace(tail, center_frame=82, duration=70),))
    command = MoveSpeedPointCommand.create("v1", "v", 80, 10)
    changed = command.apply(original)
    undone = command.undo(changed)
    issues.append({
        "id": "S02b", "category": "backend", "priority": "P1",
        "problem": "MoveSpeedPointCommand undo omits transition snapshots",
        "observed": {"transition_duration_before": 70,
                     "transition_duration_after": changed.video_tracks[0].transitions[0].duration,
                     "transition_duration_undo": undone.video_tracks[0].transitions[0].duration,
                     "clip_duration_undo": undone.get_clip_by_id("v1").timeline_duration},
    })

    from PyQt6.QtWidgets import QApplication
    from src.features.edit.ui.timeline_view import TimelineView
    app = QApplication.instance() or QApplication([])
    view = TimelineView()
    curved = replace(linked_video, speed=SpeedParams(speed_points=(
        SpeedPoint(16, 2.0), SpeedPoint(60, 0.5))))
    partner = replace(curved, clip_id="a1", track_id="a", linked_clip_id="v1")
    original = sequence(curved, partner, audio_locked=True)
    view.load_timeline(original)
    messages: list[str] = []
    view.status_message_requested.connect(messages.append)
    exception: str | None = None
    try:
        view._on_retime_action_requested("v1", "v", "clear_point", 16, None)
    except (RuntimeError, ValueError) as exc:
        exception = f"{type(exc).__name__}: {exc}"
    issues.append({
        "id": "S03", "category": "UI", "priority": "P1",
        "problem": "Clear-point UI branch lets linked lock rejection escape its Qt slot",
        "observed": {"escaped_exception": exception,
                     "status_messages": messages,
                     "model_unchanged": view.get_timeline().to_dict() == original.to_dict()},
        "note": "May close during this review as the parent fixes UI; use original committed baseline to reproduce historical finding",
    })
    sliced = replace(video, source_duration=80,
                     speed=SpeedParams(speed_points=(SpeedPoint(40, 2.0),),
                                       curve_offset=24, curve_length=104))
    original = sequence(sliced)
    view.load_timeline(original)
    offsets = [segment[0] for segment in view._items_map["v1"]._retime_segments()]
    messages.clear()
    view._on_retime_action_requested("v1", "v", "clear_point", offsets[1], None)
    issues.append({
        "id": "S04", "category": "UI", "priority": "P2",
        "problem": "Retime item projects full-curve coordinates into a window-relative menu API",
        "expected_boundary_offset": 16,
        "observed": {"displayed_segment_offsets": offsets,
                     "status_messages": messages,
                     "remaining_points": [p.to_dict() for p in view.get_timeline().get_clip_by_id("v1").speed.speed_points]},
        "note": "May close during this review as the parent fixes UI; use original committed baseline to reproduce historical finding",
    })
    short = replace(video, timeline_duration=2, source_duration=5,
                    speed=SpeedParams(speed=3.0),
                    _source_fps_num=24, _source_fps_den=1,
                    _timeline_fps_num=24, _timeline_fps_den=1)
    view.load_timeline(sequence(short))
    view._on_rate_stretch_finished("v1", "v", 2, 10)
    stretched = view.get_timeline().get_clip_by_id("v1")
    issues.append({
        "id": "S05", "category": "backend timing adapter", "priority": "P2",
        "problem": "B18 ratio uses an already-rounded original duration",
        "observed": {"source_duration": 5, "original_speed": 3.0,
                     "original_duration": 2, "requested_duration": 10,
                     "committed_speed": stretched.speed.speed,
                     "committed_duration": stretched.timeline_duration},
        "expected": "The authoritative source/fps/phase integration solves requested duration directly; the committed edge matches preview",
    })
    view.close()
    app.processEvents()
    target = Path(__file__).with_name("speed_review_results.json")
    target.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"evidence": str(target), "meaning": results["meaning"],
                      "observations": len(issues)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
