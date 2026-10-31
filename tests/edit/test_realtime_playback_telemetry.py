"""
Stage A unit tests: realtime playback types + telemetry aggregator.
对应规格书 §5 / §18。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.realtime_playback.types import (
    DecodeMiss,
    FrameHandle,
    FrameIdentity,
    LayerFrameBundle,
    PlaybackMode,
    SourceDecision,
    SourceKind,
)
from src.features.edit.core.realtime_playback.telemetry import (
    PlaybackFrameTelemetry,
    TelemetryAggregator,
)


# =============================================================================
# PlaybackMode
# =============================================================================


class TestPlaybackMode:
    def test_realtime_disallows_master(self) -> None:
        assert PlaybackMode.REALTIME.allows_master is False
        assert PlaybackMode.REALTIME.allows_blocking is False
        assert PlaybackMode.REALTIME.is_realtime is True

    def test_non_realtime_allows_master(self) -> None:
        for m in (PlaybackMode.PAUSED, PlaybackMode.SCRUB, PlaybackMode.STEP, PlaybackMode.EXPORT):
            assert m.allows_master is True

    def test_paused_step_export_allow_blocking(self) -> None:
        assert PlaybackMode.PAUSED.allows_blocking is True
        assert PlaybackMode.STEP.allows_blocking is True
        assert PlaybackMode.EXPORT.allows_blocking is True
        assert PlaybackMode.SCRUB.allows_blocking is False


# =============================================================================
# SourceKind
# =============================================================================


class TestSourceKind:
    def test_disk_proxy_is_realtime_ready(self) -> None:
        assert SourceKind.DISK_PROXY.is_realtime_ready is True
        assert SourceKind.RENDER_CACHE.is_realtime_ready is True

    def test_memory_proxy_not_realtime_ready(self) -> None:
        # memory proxy 仅过渡，不是专业实时主路径
        assert SourceKind.MEMORY_PROXY.is_realtime_ready is False

    def test_master_not_realtime_ready(self) -> None:
        assert SourceKind.MASTER.is_realtime_ready is False
        assert SourceKind.MASTER.is_master is True

    def test_placeholder_not_ready(self) -> None:
        assert SourceKind.PLACEHOLDER.is_realtime_ready is False


# =============================================================================
# FrameIdentity (clip_id 必须进入 key)
# =============================================================================


class TestFrameIdentity:
    def test_clip_id_is_part_of_identity(self) -> None:
        """同一 source_path 的不同 clip 必须有不同 identity，避免共享 decode cursor。"""
        id1 = FrameIdentity(
            timeline_id="t1",
            clip_id="clip-A",
            asset_id="asset-1",
            source_path="/a/b.mov",
            source_frame=10,
            source_kind=SourceKind.DISK_PROXY,
        )
        id2 = FrameIdentity(
            timeline_id="t1",
            clip_id="clip-B",
            asset_id="asset-1",
            source_path="/a/b.mov",
            source_frame=10,
            source_kind=SourceKind.DISK_PROXY,
        )
        assert id1 != id2
        assert id1.clip_id == "clip-A"
        assert id2.clip_id == "clip-B"

    def test_frozen(self) -> None:
        id1 = FrameIdentity(
            timeline_id="t1", clip_id="c", asset_id="a", source_path="/x",
            source_frame=0, source_kind=SourceKind.DISK_PROXY,
        )
        with pytest.raises(Exception):
            id1.clip_id = "other"  # type: ignore[misc]


# =============================================================================
# FrameHandle / LayerFrameBundle
# =============================================================================


def _make_handle(frame: int, source_kind: SourceKind = SourceKind.DISK_PROXY, exact: bool = True) -> FrameHandle:
    return FrameHandle(
        identity=FrameIdentity(
            timeline_id="t1", clip_id="c", asset_id="a", source_path="/x",
            source_frame=frame, source_kind=source_kind,
        ),
        frame_index=frame,
        width=1920, height=1080,
        pixel_format="yuv420p",
        bit_depth=8,
        color_space=None,
        pts_seconds=float(frame) / 25.0,
        source_kind=source_kind,
        payload=None,
        payload_type="gpu_texture",
        exact=exact,
    )


class TestFrameHandle:
    def test_is_gpu_payload(self) -> None:
        h = _make_handle(0)
        assert h.is_gpu_payload() is True

    def test_placeholder_flag(self) -> None:
        h = _make_handle(0, source_kind=SourceKind.PLACEHOLDER, exact=False)
        h.payload_type = "placeholder"
        assert h.is_placeholder() is True


class TestLayerFrameBundle:
    def test_stale_generation(self) -> None:
        bundle = LayerFrameBundle(
            timeline_frame=5,
            generation_id=1,
            layers=(_make_handle(5),),
            deadline_ns=0,
            complete=True,
        )
        assert bundle.is_stale(current_generation_id=1) is False
        assert bundle.is_stale(current_generation_id=2) is True

    def test_layer_count(self) -> None:
        bundle = LayerFrameBundle(
            timeline_frame=5,
            generation_id=1,
            layers=(_make_handle(5), _make_handle(6)),
            deadline_ns=0,
            complete=True,
        )
        assert bundle.layer_count == 2


# =============================================================================
# TelemetryAggregator
# =============================================================================


class TestTelemetryAggregator:
    def test_master_requests_counted(self) -> None:
        agg = TelemetryAggregator(window=10)
        agg.record(PlaybackFrameTelemetry(timeline_frame=0, master_requested=True, mode="paused"))
        agg.record(PlaybackFrameTelemetry(timeline_frame=1, master_requested=False, mode="realtime"))
        s = agg.summary()
        assert s["master_requests"] == 1
        # realtime 模式下的 master 必须单独计数（规格书关键断言）
        assert s["realtime_master_requests"] == 0

    def test_realtime_master_violates_invariant(self) -> None:
        agg = TelemetryAggregator(window=10)
        agg.record(PlaybackFrameTelemetry(
            timeline_frame=0, mode="realtime", master_requested=True,
        ))
        passed, msg = agg.assert_realtime_invariants()
        assert passed is False
        assert "master" in msg

    def test_ring_hit_rate(self) -> None:
        agg = TelemetryAggregator(window=10)
        for i in range(10):
            agg.record(PlaybackFrameTelemetry(
                timeline_frame=i, mode="realtime",
                ring_hit=(i < 8), source_kinds=("disk_proxy",),
                composite_ms=1.0 if i >= 0 else 0.0,
                total_ms=2.0,
            ))
        s = agg.summary()
        assert s["frames"] == 10
        assert s["ring_hit_rate"] == 0.8

    def test_window_eviction(self) -> None:
        # minimum enforced window is 8
        agg = TelemetryAggregator(window=8)
        for i in range(20):
            agg.record(PlaybackFrameTelemetry(timeline_frame=i, mode="realtime", total_ms=2.0))
        s = agg.summary()
        # 滑动窗口只保留最近 8 帧
        assert s["frames"] == 8
        # 但 session_total_frames 累计所有
        assert s["session_total_frames"] == 20

    def test_percentile_calculation(self) -> None:
        agg = TelemetryAggregator(window=100)
        for ms in [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]:
            agg.record(PlaybackFrameTelemetry(
                timeline_frame=0, mode="realtime", total_ms=ms,
            ))
        s = agg.summary()
        assert 50.0 <= s["total_ms_p50"] <= 60.0
        assert s["total_ms_p95"] >= 90.0

    def test_reset(self) -> None:
        agg = TelemetryAggregator(window=10)
        agg.record(PlaybackFrameTelemetry(timeline_frame=0, mode="realtime", total_ms=2.0))
        agg.reset()
        s = agg.summary()
        assert s["frames"] == 0
        assert s["session_total_frames"] == 0

    def test_source_kinds_distribution(self) -> None:
        agg = TelemetryAggregator(window=10)
        for i in range(6):
            agg.record(PlaybackFrameTelemetry(
                timeline_frame=i, mode="realtime",
                source_kinds=("disk_proxy",), total_ms=2.0,
            ))
        for i in range(4):
            agg.record(PlaybackFrameTelemetry(
                timeline_frame=10 + i, mode="realtime",
                source_kinds=("memory_proxy",), total_ms=2.0,
            ))
        s = agg.summary()
        assert s["source_kinds"]["disk_proxy"] == 0.6
        assert s["source_kinds"]["memory_proxy"] == 0.4

    def test_invariants_pass_when_clean(self) -> None:
        agg = TelemetryAggregator(window=10)
        for i in range(5):
            agg.record(PlaybackFrameTelemetry(
                timeline_frame=i, mode="realtime",
                source_kinds=("disk_proxy",), total_ms=2.0,
            ))
        passed, msg = agg.assert_realtime_invariants()
        assert passed is True
