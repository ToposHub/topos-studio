#!/usr/bin/env python3
"""调色页「同一素材剪开连续播放，后续片段颜色全部相同」病因诊断。

复刻真实链路（FileOpsMixin 的真实方法）：
  _on_color_video_source_frame_changed
  -> _maybe_preadvance_same_source_clip   （连续同源片段：预推进）
  -> _maybe_advance_to_next_same_source_clip（非连续同源片段：wrap 推进）

检查点：跨片段推进后，调色栈相关状态是否跟随切换——
  1. _flush_active_grading_stack_before_switch（保存旧片段调色）
  2. _setup_grading_save_callback（绑定新片段栈）
  3. _load_color_stack_to_grading_panel（把新片段调色加载进渲染管线）
  4. ColorSession / strip 高亮

手动点击片段卡片走 open_timeline_clip_in_color 完整事务，所以正常；
连续播放走上面的预推进路径，若缺少栈切换，engine.layers 保持旧片段
调色参数，后续所有同源片段画面就会"一模一样"。

用法:
    .venv/bin/python tools/diag_color_cross_clip_grade.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import logging
logging.getLogger().setLevel(logging.ERROR)

from src.app.controllers.file_controller import FileOpsMixin
from src.features.edit.core.timeline_v2 import ClipInstance

VIDEO = ROOT / "tests" / "test_videos" / "2K_h264.mp4"


class _FakeVideoBar:
    def __init__(self) -> None:
        self.total_frames = None
        self.current_frame = None

    def set_total_frames(self, total: int) -> None:
        self.total_frames = int(total)

    def on_frame_changed(self, frame: int) -> None:
        self.current_frame = int(frame)


class _FakeVideoController:
    def __init__(self, current_frame: int = 0) -> None:
        self.current_frame = current_frame
        self.seek_calls: list[int] = []
        self.is_playing = True

    def seek_to_frame(self, frame: int) -> None:
        self.seek_calls.append(int(frame))
        self.current_frame = int(frame)
        self.is_playing = False

    def set_in_out_range(self, in_frame: int, out_frame: int) -> None:
        pass

    def clear_in_out_range(self) -> None:
        pass

    def start_playback(self) -> None:
        self.is_playing = True


class _Track:
    def __init__(self, clips) -> None:
        self._clips = tuple(clips)

    def get_clip_at(self, frame: int):
        for c in self._clips:
            if c.timeline_start <= frame < c.timeline_start + c.timeline_duration:
                return c
        return None


class _Strip:
    def __init__(self) -> None:
        self.active_clip_id = None

    def set_active_clip(self, clip_id) -> None:
        self.active_clip_id = clip_id


class _Controller(FileOpsMixin):
    """Real FileOpsMixin + instrumented grading-stack transaction hooks."""

    def __init__(self, clip: ClipInstance) -> None:
        self.current_page = "Color"
        self.canvas = SimpleNamespace(video_control_bar=_FakeVideoBar())
        self.video_controller = _FakeVideoController(current_frame=clip.source_start)
        self._color_timeline_clip_playback_context = None
        self.events: list[str] = []

    # --- instrumented color-stack transaction -------------------------
    def _flush_active_grading_stack_before_switch(self, target_stack_id):
        self.events.append(f"flush->{target_stack_id}")

    def _setup_grading_save_callback(self, stack_id: str) -> None:
        self._grading_save_stack_id = stack_id
        self.events.append(f"save_callback->{stack_id}")

    def _load_color_stack_to_grading_panel(self, stack_id: str) -> None:
        self.events.append(f"hydrate->{stack_id}")

    def _ensure_clip_has_color_stack(self, clip_id: str, asset_id: str):
        # Mirror real behavior: each clip owns a stack named after itself.
        return f"stack-{clip_id}"


def build(clips: list[ClipInstance]) -> _Controller:
    project = SimpleNamespace(
        assets={
            c.asset_id: SimpleNamespace(path=str(VIDEO)) for c in clips
        },
        project_id="proj-diag",
    )
    controller = _Controller(clips[0])
    controller._current_edit_project = project
    controller.color_timeline_clip_strip = _Strip()
    controller._current_timeline_sequences = {
        "tl-1": SimpleNamespace(video_tracks=(_Track(clips),)),
    }
    controller._grading_save_stack_id = f"stack-{clips[0].clip_id}"
    controller._set_color_timeline_clip_playback_context("tl-1", clips[0])
    return controller


def simulate_contiguous_playback(controller: _Controller, clips: list[ClipInstance]) -> None:
    """Emit every displayed source frame across all clips, like playback."""
    for clip in clips:
        start = int(clip.source_start)
        for frame in range(start, start + int(clip.source_duration)):
            controller._on_color_video_source_frame_changed(frame)


def main() -> int:
    if not VIDEO.exists():
        print(f"[SKIP] test video not found: {VIDEO}")
        return 2

    # 三个同源连续片段：source 0..29 / 30..59 / 60..89
    clips = [
        ClipInstance(clip_id="clipA", asset_id="asset-src", track_id="v1",
                     timeline_start=0, timeline_duration=30,
                     source_start=0, source_duration=30),
        ClipInstance(clip_id="clipB", asset_id="asset-src", track_id="v1",
                     timeline_start=30, timeline_duration=30,
                     source_start=30, source_duration=30),
        ClipInstance(clip_id="clipC", asset_id="asset-src", track_id="v1",
                     timeline_start=60, timeline_duration=30,
                     source_start=60, source_duration=30),
    ]
    controller = build(clips)
    simulate_contiguous_playback(controller, clips)

    ctx = controller._active_color_timeline_clip_playback_context()
    bound_clip = ctx["clip"].clip_id if ctx else None
    strip_id = controller.color_timeline_clip_strip.active_clip_id
    save_stack = getattr(controller, "_grading_save_stack_id", None)
    hydrated = [e for e in controller.events if e.startswith("hydrate->")]

    print(f"asset path        : {VIDEO.name}")
    print(f"context clip      : {bound_clip}")
    print(f"strip highlight   : {strip_id}")
    print(f"save callback     : {save_stack}")
    print(f"stack hydration   : {hydrated or '(从未切换调色栈!)'}")
    print(f"events            : {controller.events}")

    ok = (
        bound_clip == "clipC"
        and strip_id == "clipC"
        and save_stack == "stack-clipC"
        and hydrated == ["hydrate->stack-clipB", "hydrate->stack-clipC"]
    )
    if ok:
        print("[PASS] 跨片段推进时调色栈正确跟随切换")
        return 0
    print("[FAIL] 推进后调色栈未切换 —— 后续片段沿用旧片段调色（病因确认）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
