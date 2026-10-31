"""
O2 接缝测试：预览渲染器的遮挡裁剪接入共享模块。

验证 ``PreviewRenderWorker._prune_occluded_layers_for_preview`` 的行为：
- 全屏不透明上轨 → 下轨被裁（并写入 preview telemetry）；
- 源 Alpha / 混合模式 / 效果链 / 转场 → fail-closed 保留下层
  （相对旧实现的的安全收紧：旧实现不检查这三类条件）。

测试用 duck-typed host 借用 worker 的未绑定方法，避免实例化 QThread。
"""

from __future__ import annotations

import pytest

from src.features.edit.core import timeline_occlusion as oc
from src.features.edit.core.timeline_occlusion import LayerOcclusionFacts
from src.features.edit.core.timeline_preview_renderer import PreviewRenderWorker

from tests.edit.test_timeline_occlusion import FakeLayer, VIDEO_FACTS


class FakeResolver:
    """AssetResolver 测试替身。"""

    def __init__(self, asset_types=None, metadata=None):
        self._types = asset_types or {}
        self._metadata = metadata or {}

    def get_asset_type(self, asset_id):
        return self._types.get(asset_id, "video")

    def get_asset_metadata(self, asset_id):
        return self._metadata.get(asset_id, {"pixel_format": "yuv422p10le"})


class _PruneHost:
    """借用 PreviewRenderWorker 未绑定方法的轻量宿主。"""

    _prune_occluded_layers_for_preview = (
        PreviewRenderWorker._prune_occluded_layers_for_preview
    )
    _occlusion_facts_for_layer = PreviewRenderWorker._occlusion_facts_for_layer

    def __init__(self, resolver, effects_by_clip=None):
        self._asset_resolver = resolver
        self._effects_by_clip = effects_by_clip or {}

    @property
    def _decode_target_size(self):
        return (1920, 1080)

    def _get_sequence_resolution(self):
        return (1920, 1080)

    def _get_clip_effects_snapshot(self, layer):
        return tuple(self._effects_by_clip.get(layer.clip_id, ()))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("TOPOS_TIMELINE_OCCLUSION_CULL", raising=False)
    monkeypatch.delenv("TOPOS_TIMELINE_OCCLUSION_RECT", raising=False)
    monkeypatch.delenv("TOPOS_TIMELINE_OCCLUSION_DEBUG", raising=False)
    oc.reset_env_cache()
    oc.get_occlusion_telemetry().reset()
    yield
    oc.reset_env_cache()
    oc.get_occlusion_telemetry().reset()


def _host(metadata=None, effects=None) -> _PruneHost:
    return _PruneHost(FakeResolver(metadata=metadata or {}), effects)


class TestPreviewSeam:
    def test_full_opaque_upper_prunes_lower(self):
        host = _host()
        lower = FakeLayer("v1", 0)
        upper = FakeLayer("v2", 1)
        result = host._prune_occluded_layers_for_preview(
            (lower, upper), (), frame=42,
        )
        assert result == (upper,)

    def test_telemetry_records_preview_mode(self):
        host = _host()
        host._prune_occluded_layers_for_preview(
            (FakeLayer("v1", 0), FakeLayer("v2", 1)), (), frame=7,
        )
        snap = oc.get_occlusion_telemetry().snapshot()
        assert snap["frames"] == 1
        assert snap["modes"] == {"preview": 1}
        assert snap["culled_layers"] == 1

    def test_no_cull_records_no_cull_reason(self):
        host = _host()
        host._prune_occluded_layers_for_preview(
            (FakeLayer("v1", 0), FakeLayer("v2", 1, opacity=0.5)), (), frame=0,
        )
        snap = oc.get_occlusion_telemetry().snapshot()
        assert snap["frames"] == 1
        assert snap["culled_layers"] == 0

    def test_cull_disabled_env_keeps_all(self, monkeypatch):
        monkeypatch.setenv("TOPOS_TIMELINE_OCCLUSION_CULL", "0")
        oc.reset_env_cache()
        host = _host()
        layers = (FakeLayer("v1", 0), FakeLayer("v2", 1))
        result = host._prune_occluded_layers_for_preview(layers, (), frame=0)
        assert result == layers


class TestPreviewSafetyTightening:
    """相对旧实现的安全修复：这些条件此前会被错误裁掉。"""

    def test_alpha_source_upper_keeps_lower(self):
        # 上轨是带 Alpha 的素材（ProRes 4444 / Topos 4444）：旧实现会
        # 错误裁掉下轨；共享模块按 unknown_alpha 保留。
        host = _host(metadata={"v2": {"pixel_format": "yuva444p10le"}})
        layers = (
            FakeLayer("v1", 0),
            FakeLayer("v2", 1, asset_id="v2"),
        )
        result = host._prune_occluded_layers_for_preview(layers, (), frame=0)
        assert result == layers

    def test_unknown_pixel_format_keeps_lower(self):
        host = _host(metadata={"v2": {}})
        layers = (
            FakeLayer("v1", 0),
            FakeLayer("v2", 1, asset_id="v2"),
        )
        result = host._prune_occluded_layers_for_preview(layers, (), frame=0)
        assert result == layers

    def test_blend_mode_upper_keeps_lower(self):
        class FakeClip:
            blend_mode = "multiply"

        layers = (
            FakeLayer("v1", 0),
            FakeLayer("v2", 1, ),
        )
        host = _host()
        # 直接通过 facts 通道验证 blend 分支（FakeLayer 无真实 clip 时
        # _occlusion_facts_for_layer 走 getattr 默认 normal）。
        facts = host._occlusion_facts_for_layer(layers[1])
        assert facts is not None
        assert facts.blend_mode == "normal"

        class BlendHost(_PruneHost):
            def _occlusion_facts_for_layer(self, layer):
                return LayerOcclusionFacts(
                    asset_kind="video",
                    source_alpha_known_opaque=True,
                    blend_mode="multiply",
                )

        result = BlendHost(FakeResolver())._prune_occluded_layers_for_preview(
            layers, (), frame=0,
        )
        assert result == layers

    def test_effects_upper_keeps_lower(self):
        host = _host(effects={"v2": (object(),)})
        layers = (FakeLayer("v1", 0), FakeLayer("v2", 1))
        result = host._prune_occluded_layers_for_preview(layers, (), frame=0)
        assert result == layers

    def test_transition_frame_keeps_all(self):
        host = _host()
        layers = (FakeLayer("v1", 0), FakeLayer("v2", 1))
        result = host._prune_occluded_layers_for_preview(
            layers, ("transition",), frame=0,
        )
        assert result == layers

    def test_facts_failure_keeps_lower(self):
        class BoomHost(_PruneHost):
            def _occlusion_facts_for_layer(self, layer):
                raise RuntimeError("boom")

        layers = (FakeLayer("v1", 0), FakeLayer("v2", 1))
        result = BoomHost(FakeResolver())._prune_occluded_layers_for_preview(
            layers, (), frame=0,
        )
        assert result == layers


# ── O4 复查修复：实时调度器配置不再静默失败 ──


class TestRealtimeSchedulerConfigSeam:
    """回归：_build_realtime_scheduler_config 属于 renderer，而 prune 定义在
    worker 上。修复前 self._prune_... 必然 AttributeError 且被 except 吞掉
    → 引擎侧永远拿到 None → 实时解码调度静默降级。"""

    def _build_renderer_with_worker(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from PyQt6.QtCore import QMutex

        from src.features.edit.core.timeline_preview_renderer import (
            PreviewRenderWorker,
            TimelinePreviewRenderer,
        )
        from src.features.edit.core.timeline_render_plan import TimelineRenderPlan
        from src.features.edit.core.timeline_v2 import (
            ClipInstance, EditTrack, Rational, TimelineSequence,
        )

        lower = ClipInstance(
            clip_id="clip-lower", asset_id="a-lower", track_id="v1",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=100,
        )
        upper = ClipInstance(
            clip_id="clip-upper", asset_id="a-upper", track_id="v2",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=100,
        )
        v1 = EditTrack("v1", "V1", "video", 0, clips=(lower,))
        v2 = EditTrack("v2", "V2", "video", 1, clips=(upper,))
        sequence = TimelineSequence(
            timeline_id="seq-occl", name="S", fps=Rational(25, 1),
            width=1920, height=1080, video_tracks=(v1, v2),
        )

        resolver = MagicMock()
        resolver.get_asset_type.return_value = "video"
        resolver.get_asset_metadata.return_value = {"pixel_format": "yuv420p"}

        worker = PreviewRenderWorker(
            frame_provider=MagicMock(),
            asset_resolver=resolver,
            video_renderer=None,
            timeline_sequence=sequence,
            viewport_size=(960, 540),
            gpu_decode_manager=MagicMock(),
            gpu_renderer=MagicMock(),
        )

        renderer = TimelinePreviewRenderer.__new__(TimelinePreviewRenderer)
        renderer._realtime_decode_scheduler = MagicMock()
        renderer._worker = worker
        renderer._sequence = sequence
        renderer._asset_resolver = resolver
        renderer._render_plan_mutex = QMutex()
        renderer._render_plan = TimelineRenderPlan(sequence)
        renderer._auto2k_decode_target_size = lambda: (1920, 1080)
        renderer._realtime_scheduler_source_frames = lambda frame: {}

        return renderer, worker, sequence

    def test_scheduler_config_builds_and_applies_culling(self):
        from types import SimpleNamespace

        from src.features.edit.core.realtime_playback import SourceKind

        renderer, worker, sequence = self._build_renderer_with_worker()
        decisions = [
            ("clip-lower", SimpleNamespace(
                source_kind=SourceKind.DISK_PROXY, source_path="/proxy-l.mov")),
            ("clip-upper", SimpleNamespace(
                source_kind=SourceKind.DISK_PROXY, source_path="/proxy-u.mov")),
        ]
        oc.get_occlusion_telemetry().reset()

        config = renderer._build_realtime_scheduler_config(10, decisions)

        # 修复前：AttributeError 被吞 → None；现在必须返回有效配置。
        assert isinstance(config, dict), (
            "scheduler config silently degraded to None (pre-existing "
            "AttributeError regression)"
        )
        scheduled = [spec.clip_id for spec in config["layers"]]
        # 裁剪决策与 worker 渲染计划一致：被遮挡的下轨不进调度器。
        assert scheduled == ["clip-upper"]
        # record=False：调度器派生路径不重复计入 telemetry。
        assert oc.get_occlusion_telemetry().snapshot()["frames"] == 0
        worker.stop()
