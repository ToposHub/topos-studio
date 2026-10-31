"""P1 最后一公里集成验收（真实链路激活）。

> 验证 3 个"最后一公里"缺口的端到端修复：
> 1. selected_asset_ids 端到端：bridge.get_selected_asset_ids 真正取到选中素材
> 2. editorial evidence 端到端：AnalysisService 有 transcript → provider 返回非空
>    → _fetch_evidence 取到 → auto_caption 不降级
> 3. cancel 端到端：plan worker 取消 → run 标 CANCELLED
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.features.ai_harness.runtime import HarnessRuntime
from src.features.ai_harness.invocation_models import RunStatus
from src.features.edit.ai_editing.editorial_models import (
    FrameRange, SegmentKind, SemanticSegment,
)

pytestmark = pytest.mark.edit_core


# ---------------------------------------------------------------------- #
# 1. selected_asset_ids 端到端
# ---------------------------------------------------------------------- #


def test_selected_asset_ids_e2e_bridge_reads_media_pool_panel(qapp):
    """端到端：fake media_pool_panel 有选中 → bridge.get_selected_asset_ids 非空。"""
    from src.app.controllers.ai_edit_host_bridge_impl import MainWindowAiEditHostBridge

    fake_mp = MagicMock()
    fake_mp.get_selected_asset_ids.return_value = ("asset-1", "asset-2", "asset-3")
    fake_mw = MagicMock()
    fake_mw.media_pool_panel = fake_mp

    bridge = MainWindowAiEditHostBridge(fake_mw, MagicMock(), MagicMock())
    ids = bridge.get_selected_asset_ids()

    assert len(ids) == 3, "应取到 3 个选中素材"
    assert "asset-1" in ids


def test_selected_asset_ids_empty_when_no_selection(qapp):
    """无选中时返回空（editorial skill 进 WAITING_INPUT）。"""
    from src.app.controllers.ai_edit_host_bridge_impl import MainWindowAiEditHostBridge

    fake_mp = MagicMock()
    fake_mp.get_selected_asset_ids.return_value = ()
    fake_mw = MagicMock()
    fake_mw.media_pool_panel = fake_mp

    bridge = MainWindowAiEditHostBridge(fake_mw, MagicMock(), MagicMock())
    assert bridge.get_selected_asset_ids() == ()


# ---------------------------------------------------------------------- #
# 2. editorial evidence 端到端
# ---------------------------------------------------------------------- #


def _make_analysis_with_transcript():
    """构造含 transcript 的 AnalysisService。"""
    from src.features.edit.ai_editing.analysis_service import AnalysisService
    from src.features.edit.ai_editing.analysis_models import (
        AssetAnalysis, AssetFingerprint,
    )
    svc = AnalysisService()
    svc.ingest(
        "asset-1",
        AssetAnalysis(asset_id="asset-1", fingerprint=AssetFingerprint(size=100)),
        (SemanticSegment(
            segment_id="t1", asset_id="asset-1",
            source_range=FrameRange(start=0, duration=200),
            kind=SegmentKind.TRANSCRIPT,
            payload={"text": "这是一段转录文本"},
            confidence=0.92,
        ),),
    )
    return svc


def test_editorial_evidence_e2e_provider_returns_transcript():
    """端到端：AnalysisService 有 transcript → provider 返回非空。"""
    from src.app.controllers.ai_editing_mixin import make_editorial_provider

    svc = _make_analysis_with_transcript()
    provider = make_editorial_provider(svc)
    segments = provider(("asset-1",), "transcript")

    assert len(segments) == 1, "应返回 1 个 transcript segment"
    assert segments[0].kind == SegmentKind.TRANSCRIPT
    assert segments[0].payload["text"] == "这是一段转录文本"


def test_editorial_evidence_e2e_fetch_evidence_non_empty():
    """端到端：provider 接 adapter → _fetch_evidence 取到真实 evidence。"""
    from src.app.controllers.ai_editing_mixin import make_editorial_provider
    from src.features.ai_harness.legacy_adapters import LegacyHarnessAdapter

    svc = _make_analysis_with_transcript()
    provider = make_editorial_provider(svc)
    adapter = LegacyHarnessAdapter(svc, HarnessRuntime())
    adapter.set_editorial_provider(provider)

    segments = adapter._fetch_evidence(("asset-1",), "transcript")
    assert len(segments) >= 1, "_fetch_evidence 应取到真实 transcript（不降级 No evidence）"


def test_editorial_evidence_none_provider_still_degrades():
    """provider=None 时 _fetch_evidence 返回空（降级，不崩）。"""
    from src.features.ai_harness.legacy_adapters import LegacyHarnessAdapter

    adapter = LegacyHarnessAdapter(MagicMock(), HarnessRuntime())
    # 不调 set_editorial_provider → _editorial_provider 仍 None
    segments = adapter._fetch_evidence(("asset-1",), "transcript")
    assert segments == (), "provider=None 时应降级返回空"


# ---------------------------------------------------------------------- #
# 3. cancel 端到端
# ---------------------------------------------------------------------- #


def test_cancel_e2e_run_marked_cancelled(qapp):
    """端到端：runtime.cancel + finish(success=False) → run 终态 CANCELLED。"""
    from src.features.ai_harness.invocation_models import (
        Invocation, InvocationScope, InvocationSource,
    )
    runtime = HarnessRuntime()
    inv = Invocation(
        source=InvocationSource.CHAT, skill_id="topos.test",
        scope=InvocationScope(project_id="p1"),
    )
    run_id = runtime.start(inv)
    runtime.mark_running(run_id)
    runtime.cancel(run_id)
    runtime.finish(run_id, success=False)

    rec = runtime.get_run(run_id)
    assert rec.status == RunStatus.CANCELLED


# ---------------------------------------------------------------------- #
# 4. 红线：真实链路不引入 moderngl/composite.core
# ---------------------------------------------------------------------- #


def test_no_moderngl_or_composite_core_in_last_mile():
    """最后一公里改动不引入 moderngl/composite.core。"""
    import pathlib
    files = [
        "src/features/edit/ui/media_pool_panel.py",
        "src/app/controllers/ai_edit_host_bridge_impl.py",
        "src/app/controllers/ai_editing_mixin.py",
    ]
    root = pathlib.Path(__file__).resolve().parents[2]
    for rel in files:
        src = (root / rel).read_text(encoding="utf-8")
        assert "import moderngl" not in src, f"{rel} 不应 import moderngl"
        assert "from composite.core" not in src, f"{rel} 不应 import composite.core"
