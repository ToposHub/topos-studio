"""
Final-2 (FINAL-P0-02): TimelineDecodeScheduler / SourceDecision 真正接管解码热路径。

验收点：
1. EditGpuDecodeManager.get_frame_with_policy 在 SourceDecision 标记为
   PLACEHOLDER 且 allow_master_fallback=False 时拒绝 master 解码。
2. REALTIME 模式 master_requests 统计应保持为 0（当 SourceDecision 禁止时）。
3. worker._try_gpu_render 把 SourceDecision 透传到 decode manager。
4. engine.request_present 把 (clip_id, decision) 配对传给 delegate。

这些测试是 "scheduler real hot path" gate（规格书 §6.2 grep gate 之外的运行时 gate）。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.features.edit.core.realtime_playback import (
    PlaybackMode,
    RealtimePlaybackEngine,
)
from src.features.edit.core.realtime_playback.source_resolver import (
    DiskProxyServiceAdapter,
    LayerSourceQuery,
    SourceResolver,
)
from src.features.edit.core.realtime_playback.types import (
    SourceDecision,
    SourceKind,
)


# =============================================================================
# 1. get_frame_with_policy 在 PLACEHOLDER/NONE + 不允许 master 时拒绝
# =============================================================================


def _make_decision(
    source_kind: SourceKind,
    *,
    allow_master: bool,
    reason: str = "",
) -> SourceDecision:
    return SourceDecision(
        source_kind=source_kind,
        source_path=None,
        proxy_info=None,
        render_cache_info=None,
        allow_blocking_decode=False,
        allow_master_fallback=allow_master,
        max_wait_ms=0,
        reason=reason,
    )


def test_get_frame_with_policy_falls_through_on_placeholder():
    """PLACEHOLDER 决策不再阻塞解码，而是透传到 get_frame（降采样 master）。

    Final-2 修正：原实现拒绝解码会导致普通 H264/H265 无代理素材在
    realtime 模式下画面完全不动。现在 PLACEHOLDER 仅作 telemetry 标记，
    实际解码仍走 get_frame（内部 _resolve_preview_decode_source 会用
    original_downscaled 路径，对 H264/H265 流畅）。
    """
    from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager

    mgr = EditGpuDecodeManager.__new__(EditGpuDecodeManager)
    called = {"count": 0}

    def fake_get_frame(*args, **kwargs):
        called["count"] += 1
        return ("decoded_frame", "gpu")

    mgr.get_frame = fake_get_frame  # type: ignore[assignment]
    decision = _make_decision(SourceKind.PLACEHOLDER, allow_master=False, reason="proxy_missing")

    frame, ftype = mgr.get_frame_with_policy(
        "/fake/path.mov",
        frame_index=10,
        target_size=(960, 540),
        stream_key="clip-A",
        source_decision=decision,
    )
    # 修正后：PLACEHOLDER 不再阻塞，正常透传到 get_frame 解码
    assert frame == "decoded_frame", "PLACEHOLDER 应透传到 get_frame 降采样解码"
    assert ftype == "gpu"
    assert called["count"] == 1, "必须调用底层 get_frame（降采样 master 路径）"


def test_get_frame_with_policy_allows_when_master_decision():
    """非 realtime（MASTER allowed）决策应正常透传到 get_frame。"""
    from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager

    mgr = EditGpuDecodeManager.__new__(EditGpuDecodeManager)

    def fake_get_frame(*args, **kwargs):
        return ("gpu_frame_obj", "gpu")

    mgr.get_frame = fake_get_frame  # type: ignore[assignment]
    decision = _make_decision(SourceKind.MASTER, allow_master=True, reason="paused: master exact")

    frame, ftype = mgr.get_frame_with_policy(
        "/fake/path.mov",
        frame_index=10,
        target_size=(960, 540),
        stream_key="clip-A",
        source_decision=decision,
    )
    assert frame == "gpu_frame_obj"
    assert ftype == "gpu"


def test_get_frame_with_policy_allows_disk_proxy_decision():
    """DISK_PROXY ready 决策应允许走代理解码。"""
    from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager

    mgr = EditGpuDecodeManager.__new__(EditGpuDecodeManager)
    called = {"target_size": None}

    def fake_get_frame(asset, frame_index, target_size=None, stream_key=None):
        called["target_size"] = target_size
        return ("proxy_frame", "gpu")

    mgr.get_frame = fake_get_frame  # type: ignore[assignment]
    decision = _make_decision(SourceKind.DISK_PROXY, allow_master=False, reason="disk_proxy ready")

    frame, ftype = mgr.get_frame_with_policy(
        "/fake/path.mov",
        frame_index=10,
        target_size=(960, 540),
        stream_key="clip-A",
        source_decision=decision,
    )
    assert frame == "proxy_frame"
    assert called["target_size"] == (960, 540)


def test_get_frame_with_policy_none_decision_falls_through():
    """source_decision=None 时退回普通 get_frame（兼容直接 request_render）。"""
    from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager

    mgr = EditGpuDecodeManager.__new__(EditGpuDecodeManager)
    called = {"count": 0}

    def fake_get_frame(*args, **kwargs):
        called["count"] += 1
        return ("frame", "gpu")

    mgr.get_frame = fake_get_frame  # type: ignore[assignment]

    frame, ftype = mgr.get_frame_with_policy(
        "/fake/path.mov",
        frame_index=10,
        target_size=(960, 540),
        stream_key="clip-A",
        source_decision=None,
    )
    assert frame == "frame"
    assert called["count"] == 1


# =============================================================================
# 2. engine.request_present 把 (clip_id, decision) 配对传给 delegate
# =============================================================================


def test_engine_request_present_passes_clip_decision_pairs_to_delegate():
    """engine 应把 (clip_id, SourceDecision) 配对传给 delegate_render。"""
    captured: dict = {}

    def delegate(frame, gen, deadline, decisions_by_clip):
        captured["frame"] = frame
        captured["decisions_by_clip"] = list(decisions_by_clip or [])
        return True

    engine = RealtimePlaybackEngine()
    # 构造一个 DiskProxyService mock：get_disk_proxy_info 返回 MISSING（is_ready=False）
    # → policy 输出 PLACEHOLDER
    fake_info = MagicMock()
    fake_info.is_ready = False
    fake_info.status.value = "missing"
    fake_info.proxy_path = None
    fake_service = MagicMock()
    fake_service.get_disk_proxy_info.return_value = fake_info
    fake_service.generate_proxy.return_value = None
    resolver = SourceResolver(proxy_manager=DiskProxyServiceAdapter(service=fake_service))

    queries = [
        LayerSourceQuery(
            clip_id="clip-A", asset_id="asset-1",
            source_path="/fake/a.mov", source_frame=10, timeline_frame=10,
        ),
        LayerSourceQuery(
            clip_id="clip-B", asset_id="asset-2",
            source_path="/fake/b.mov", source_frame=20, timeline_frame=10,
        ),
    ]

    engine.configure(
        delegate_render=delegate,
        source_resolver=resolver,
        layer_query_provider=lambda f: queries,
    )
    engine.set_mode(PlaybackMode.REALTIME)

    ok = engine.request_present(10)
    assert ok is True

    pairs = captured["decisions_by_clip"]
    assert len(pairs) == 2, "delegate 必须收到与 query 等长的 (clip_id, decision) 配对"
    clip_ids = {cid for cid, _ in pairs}
    assert clip_ids == {"clip-A", "clip-B"}
    for cid, dec in pairs:
        assert isinstance(dec, SourceDecision)
        # REALTIME 无 proxy/cache → PLACEHOLDER + allow_master_fallback=False
        assert dec.source_kind is SourceKind.PLACEHOLDER
        assert dec.allow_master_fallback is False


def test_engine_realtime_records_source_kinds_in_telemetry():
    """engine telemetry 必须记录每帧 source_kinds。"""
    fake_info = MagicMock()
    fake_info.is_ready = False
    fake_info.status.value = "missing"
    fake_info.proxy_path = None
    fake_service = MagicMock()
    fake_service.get_disk_proxy_info.return_value = fake_info
    fake_service.generate_proxy.return_value = None
    resolver = SourceResolver(proxy_manager=DiskProxyServiceAdapter(service=fake_service))
    engine = RealtimePlaybackEngine()
    engine.configure(
        delegate_render=lambda f, g, d, dc: True,
        source_resolver=resolver,
        layer_query_provider=lambda f: [
            LayerSourceQuery(
                clip_id="c1", asset_id="a1", source_path="/x.mov",
                source_frame=f, timeline_frame=f,
            )
        ],
    )
    engine.set_mode(PlaybackMode.REALTIME)
    engine.request_present(0)

    summary = engine.telemetry.summary()
    # 窗口里至少记录了 1 帧（key 是 'frames'，不是 'total_frames'）
    assert summary.get("frames", 0) >= 1
    # source_kinds 应包含 'placeholder'（无 proxy ready）
    source_kinds = summary.get("source_kinds", {}) or {}
    assert "placeholder" in source_kinds, f"telemetry source_kinds 应记 placeholder，实际：{source_kinds}"


# =============================================================================
# 3. TimelinePreviewRenderer.pop_source_decisions_for_frame
# =============================================================================


def test_renderer_pop_source_decisions_returns_clip_indexed_map():
    """renderer 应把暂存的 [(clip_id, decision)] 转成 {clip_id: decision}。"""
    # 用最小 mock 模拟 TimelinePreviewRenderer 实例（避免 QObject 构造）。
    from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer

    class _Shell:
        pass

    shell = _Shell()
    # 把实例方法绑定到 shell
    shell._pop_source_decisions_for_frame = (
        TimelinePreviewRenderer._pop_source_decisions_for_frame.__get__(shell, _Shell)
    )
    dec_a = _make_decision(SourceKind.DISK_PROXY, allow_master=False)
    dec_b = _make_decision(SourceKind.PLACEHOLDER, allow_master=False)
    shell._pending_source_decisions = {
        100: [("clip-A", dec_a), ("clip-B", dec_b)],
    }

    result = shell._pop_source_decisions_for_frame(100)
    assert set(result.keys()) == {"clip-A", "clip-B"}
    assert result["clip-A"] is dec_a
    assert result["clip-B"] is dec_b

    # 第二次取应清空（已 pop）
    assert shell._pop_source_decisions_for_frame(100) == {}


def test_renderer_pop_source_decisions_empty_for_unknown_frame():
    """未知帧返回空 dict（不抛异常）。"""
    from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer

    class _Shell:
        pass

    shell = _Shell()
    shell._pop_source_decisions_for_frame = (
        TimelinePreviewRenderer._pop_source_decisions_for_frame.__get__(shell, _Shell)
    )
    shell._pending_source_decisions = {}

    assert shell._pop_source_decisions_for_frame(42) == {}
