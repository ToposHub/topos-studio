"""F3.1 · 嵌套音频组合入队预检——BLOCKING/WARNING/跳过语义。

此前父 retime 嵌套等组合在任务运行时才失败（timeline_export strict 门）；
本检查把同一 diagnostics 在入队前显式化（与 Stem 对话框开箱预检对齐）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.features.deliver.core.video_export_preflight import (
    PreflightContext,
    PreflightResult,
    _check_nested_audio,
    run_video_export_preflight,
)
from tests.edit.test_round4_nested_audio_gate import _crossfade_setup, _setup


def _ctx(parent, child, project) -> PreflightContext:
    return PreflightContext(
        project=project, sequence=parent,
        sequence_resolver={"child": child}.get)


def _job(include_audio: bool = True) -> Mock:
    job = Mock(spec=["include_audio"])
    job.include_audio = include_audio
    return job


class TestNestedAudioPreflight:
    def test_parent_retime_is_blocking_before_enqueue(self) -> None:
        from src.features.edit.core.clip_params import SpeedParams

        parent, child, project = _setup(speed=SpeedParams(speed=2.0))
        result = PreflightResult()
        _check_nested_audio(_job(), _ctx(parent, child, project), result)
        assert not result.ok
        blocking = [i for i in result.issues if i.severity == "blocking"]
        assert any(i.code == "nested_audio.nested_audio_retime_unsupported"
                   for i in blocking)

    def test_mixed_child_fps_is_blocking(self) -> None:
        from src.features.edit.core.clip_params import SpeedParams

        parent, child, project = _setup(speed=SpeedParams(speed=1.0),
                                        child_fps=30.0)
        result = PreflightResult()
        _check_nested_audio(_job(), _ctx(parent, child, project), result)
        assert any(i.code == "nested_audio.nested_audio_fps_unsupported"
                   for i in result.issues if i.severity == "blocking")

    def test_child_crossfade_drop_is_warning_not_blocking(self) -> None:
        parent, child, project = _crossfade_setup()
        result = PreflightResult()
        _check_nested_audio(_job(), _ctx(parent, child, project), result)
        assert result.ok, "转场丢弃可完成渲染——只警告不阻断"
        assert any(i.code == "nested_audio.nested_audio_transition_dropped"
                   for i in result.issues if i.severity == "warning")

    def test_video_only_job_skips_check(self) -> None:
        from src.features.edit.core.clip_params import SpeedParams

        parent, child, project = _setup(speed=SpeedParams(speed=2.0))
        result = PreflightResult()
        _check_nested_audio(_job(include_audio=False),
                            _ctx(parent, child, project), result)
        assert result.issues == []

    def test_healthy_nested_passes(self) -> None:
        from src.features.edit.core.clip_params import SpeedParams

        parent, child, project = _setup(speed=SpeedParams(speed=1.0))
        result = PreflightResult()
        _check_nested_audio(_job(), _ctx(parent, child, project), result)
        assert result.issues == []

    def test_missing_context_facts_skip(self) -> None:
        result = PreflightResult()
        _check_nested_audio(_job(), PreflightContext(), result)
        assert result.issues == []

    def test_check_registered_in_orchestrator(self, monkeypatch) -> None:
        """注册验证：run_video_export_preflight 必然调用嵌套音频检查。"""
        called = []
        monkeypatch.setattr(
            "src.features.deliver.core.video_export_preflight._check_nested_audio",
            lambda job, ctx, result: called.append(True))
        for name in ("_check_context", "_check_structural",
                     "_check_runtime_capability", "_check_output_path",
                     "_check_disk_space", "_check_edit_feature_preflights",
                     "_check_color_delivery_preflight", "_check_topos_video_only"):
            monkeypatch.setattr(
                f"src.features.deliver.core.video_export_preflight.{name}",
                lambda *a, **k: None)
        run_video_export_preflight(SimpleNamespace(), PreflightContext())
        assert called == [True]
