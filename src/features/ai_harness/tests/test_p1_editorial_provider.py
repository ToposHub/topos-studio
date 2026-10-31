"""P1 最后一公里批次2：editorial_provider 接入 AnalysisService（真实 transcript evidence）。

> 验证"自动字幕"/"字幕翻译配音"能拿到真实 transcript evidence（不再降级 No evidence）：
> - make_editorial_provider 适配函数：(asset_ids, kind) -> [SemanticSegment]
> - 按 SegmentKind 过滤 get_segments 结果
> - AnalysisService 无该 asset 时返回空（不崩）
> - 未知 kind 字符串返回空（不崩）
> - build_p1_dependencies 传 provider 后 adapter._editorial_provider 非 None
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from src.features.edit.ai_editing.editorial_models import (
    FrameRange, SegmentKind, SemanticSegment,
)

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from src.features.edit.ai_editing.analysis_service import AnalysisService

pytestmark = pytest.mark.edit_core


# ---------------------------------------------------------------------- #
# 辅助：构造含 transcript 的 AnalysisService
# ---------------------------------------------------------------------- #


def _make_analysis_service_with_transcript() -> "AnalysisService":
    """构造一个含 transcript + shot segments 的 AnalysisService。"""
    from src.features.edit.ai_editing.analysis_service import AnalysisService
    from src.features.edit.ai_editing.analysis_models import (
        AssetAnalysis, AssetFingerprint,
    )

    svc = AnalysisService()
    segs = (
        SemanticSegment(
            segment_id="t1", asset_id="a1",
            source_range=FrameRange(start=0, duration=100),
            kind=SegmentKind.TRANSCRIPT,
            payload={"text": "你好世界"},
            confidence=0.9,
        ),
        SemanticSegment(
            segment_id="t2", asset_id="a1",
            source_range=FrameRange(start=100, duration=50),
            kind=SegmentKind.TRANSCRIPT,
            payload={"text": "这是测试"},
            confidence=0.85,
        ),
        SemanticSegment(
            segment_id="s1", asset_id="a1",
            source_range=FrameRange(start=0, duration=150),
            kind=SegmentKind.SHOT,
            payload={"label": "scene-1"},
            confidence=0.8,
        ),
    )
    svc.ingest(
        "a1",
        AssetAnalysis(asset_id="a1", fingerprint=AssetFingerprint(size=100)),
        segs,
    )
    return svc


# ---------------------------------------------------------------------- #
# make_editorial_provider 适配函数
# ---------------------------------------------------------------------- #


def test_make_editorial_provider_filters_by_kind():
    """provider(asset_ids, 'transcript') 只返回 TRANSCRIPT segments（不含 SHOT）。"""
    from src.app.controllers.ai_editing_mixin import make_editorial_provider

    svc = _make_analysis_service_with_transcript()
    provider = make_editorial_provider(svc)

    result = provider(("a1",), "transcript")
    assert len(result) == 2, "应返回 2 个 transcript segment"
    assert all(s.kind == SegmentKind.TRANSCRIPT for s in result)


def test_make_editorial_provider_multiple_assets():
    """provider 支持多个 asset_ids（聚合各 asset 的 segments）。"""
    from src.app.controllers.ai_editing_mixin import make_editorial_provider
    from src.features.edit.ai_editing.analysis_service import AnalysisService
    from src.features.edit.ai_editing.analysis_models import (
        AssetAnalysis, AssetFingerprint,
    )

    svc = AnalysisService()
    svc.ingest(
        "a1",
        AssetAnalysis(asset_id="a1", fingerprint=AssetFingerprint(size=10)),
        (SemanticSegment(
            segment_id="t1", asset_id="a1",
            source_range=FrameRange(start=0, duration=10),
            kind=SegmentKind.TRANSCRIPT, payload={"text": "a1 文本"},
        ),),
    )
    svc.ingest(
        "a2",
        AssetAnalysis(asset_id="a2", fingerprint=AssetFingerprint(size=20)),
        (SemanticSegment(
            segment_id="t2", asset_id="a2",
            source_range=FrameRange(start=0, duration=10),
            kind=SegmentKind.TRANSCRIPT, payload={"text": "a2 文本"},
        ),),
    )

    provider = make_editorial_provider(svc)
    result = provider(("a1", "a2"), "transcript")
    assert len(result) == 2


def test_make_editorial_provider_empty_for_unknown_asset():
    """provider 对未知 asset 返回空（不崩）。"""
    from src.app.controllers.ai_editing_mixin import make_editorial_provider
    from src.features.edit.ai_editing.analysis_service import AnalysisService

    svc = AnalysisService()
    provider = make_editorial_provider(svc)
    result = provider(("nonexistent",), "transcript")
    assert result == [] or result == ()


def test_make_editorial_provider_empty_for_unknown_kind():
    """provider 对未知 kind 字符串返回空（不崩）。"""
    from src.app.controllers.ai_editing_mixin import make_editorial_provider

    svc = _make_analysis_service_with_transcript()
    provider = make_editorial_provider(svc)
    result = provider(("a1",), "unknown_kind_xyz")
    assert result == [] or result == ()


def test_make_editorial_provider_empty_asset_ids():
    """provider 对空 asset_ids 返回空。"""
    from src.app.controllers.ai_editing_mixin import make_editorial_provider

    svc = _make_analysis_service_with_transcript()
    provider = make_editorial_provider(svc)
    result = provider((), "transcript")
    assert result == [] or result == ()


# ---------------------------------------------------------------------- #
# build_p1_dependencies 传 editorial_provider
# ---------------------------------------------------------------------- #


def test_build_p1_dependencies_accepts_editorial_provider():
    """build_p1_dependencies 传 editorial_provider 后 adapter._editorial_provider 非 None。"""
    from src.app.controllers.ai_editing_mixin import build_p1_dependencies
    from src.features.ai_harness.runtime import HarnessRuntime

    svc = _make_analysis_service_with_transcript()
    from src.app.controllers.ai_editing_mixin import make_editorial_provider
    provider = make_editorial_provider(svc)

    deps = build_p1_dependencies(
        service=svc, runtime=HarnessRuntime(),
        editorial_provider=provider,
    )
    assert deps["adapter"]._editorial_provider is not None, (
        "adapter 应装配 editorial_provider（不再降级 No evidence）"
    )


def test_build_p1_dependencies_default_provider_none():
    """未传 editorial_provider 时 adapter._editorial_provider 为 None（向后兼容）。"""
    from src.app.controllers.ai_editing_mixin import build_p1_dependencies
    from src.features.ai_harness.runtime import HarnessRuntime

    svc = _make_analysis_service_with_transcript()
    deps = build_p1_dependencies(
        service=svc, runtime=HarnessRuntime(),
    )
    assert deps["adapter"]._editorial_provider is None


# ---------------------------------------------------------------------- #
# 端到端：provider 返回的 segments 能被 _fetch_evidence 消费
# ---------------------------------------------------------------------- #


def test_provider_result_consumable_by_fetch_evidence():
    """provider 返回的 segments 经 adapter._fetch_evidence 能正确取到。"""
    from src.app.controllers.ai_editing_mixin import make_editorial_provider
    from src.features.ai_harness.legacy_adapters import LegacyHarnessAdapter
    from src.features.ai_harness.runtime import HarnessRuntime

    svc = _make_analysis_service_with_transcript()
    provider = make_editorial_provider(svc)
    adapter = LegacyHarnessAdapter(svc, HarnessRuntime())
    adapter.set_editorial_provider(provider)

    segments = adapter._fetch_evidence(("a1",), "transcript")
    assert len(segments) == 2, "_fetch_evidence 应取到 2 个 transcript segment"
    assert all(s.kind == SegmentKind.TRANSCRIPT for s in segments)
