"""多流解码波饱和遥测测试（W3 立项 K-W1 触发信号采集）。

覆盖：
- EditPlaybackFrameTrace.decode_layers 字段与 compact 序列化
- FrameTraceCollector._summarize_decode_saturation 分桶/分位/超预算统计
- 判读语义：decode_ms 随轨数恶化（饱和）与不随轨数恶化（瓶颈在别处）
- _decode_telemetry_dump_every env 门控解析（默认 0 = 关闭）
"""

from __future__ import annotations

import pytest

from src.shared.video.playback_frame_trace import (
    EditPlaybackFrameTrace,
    FrameTraceCollector,
)


def _trace(layers: int, decode_ms: float, frame_index: int = 0) -> EditPlaybackFrameTrace:
    return EditPlaybackFrameTrace(
        frame_index=frame_index,
        page='edit_timeline',
        decode_ms=decode_ms,
        decode_layers=layers,
        decode_path='cpu_planar',
        render_path='gpu_composite',
    )


class TestDecodeSaturationSummary:
    def test_bucketing_and_percentiles(self) -> None:
        collector = FrameTraceCollector()
        for i, (layers, ms) in enumerate([
            (1, 10.0), (1, 14.0), (1, 40.0),          # 单流：p50=14, 1 帧超 33.3
            (2, 20.0), (2, 22.0), (2, 24.0),          # 双流：p50=22, 无超
            (4, 45.0), (4, 47.0),                     # 四流
        ]):
            collector.record(_trace(layers, ms, frame_index=i))
        sat = collector.get_summary()['decode_saturation']
        assert sat['budget_ms'] == pytest.approx(33.3)
        assert sat['buckets']['1']['frames'] == 3
        assert sat['buckets']['1']['decode_ms_p50'] == 14.0
        assert sat['buckets']['1']['over_budget'] == 1
        assert sat['buckets']['2']['decode_ms_max'] == 24.0
        assert sat['buckets']['4']['decode_ms_p95'] == 47.0
        # 无解码流数记录的帧不入桶
        collector.record(EditPlaybackFrameTrace(frame_index=99, decode_ms=5.0))
        assert collector.get_summary()['decode_saturation']['buckets']['1']['frames'] == 3

    def test_saturation_vs_elsewhere_reading(self) -> None:
        """判读语义：随轨数恶化（饱和）vs 不随轨数恶化（瓶颈在别段）。"""
        saturated = FrameTraceCollector()
        for i in range(6):
            saturated.record(_trace(1, 8.0, i))
            saturated.record(_trace(2, 16.0, i))
            saturated.record(_trace(4, 32.0, i))
        buckets = saturated.get_summary()['decode_saturation']['buckets']
        assert (
            buckets['4']['decode_ms_p50'] > buckets['2']['decode_ms_p50']
            > buckets['1']['decode_ms_p50']
        ) and buckets['1']['over_budget'] == 0

        elsewhere = FrameTraceCollector()
        for i in range(6):
            elsewhere.record(_trace(1, 30.0, i))
            elsewhere.record(_trace(4, 30.0, i))
        buckets = elsewhere.get_summary()['decode_saturation']['buckets']
        assert buckets['4']['decode_ms_p50'] == buckets['1']['decode_ms_p50']
        assert buckets['1']['over_budget'] == 0  # 30ms < 33.3 预算内

    def test_empty_collector_yields_empty_buckets(self) -> None:
        collector = FrameTraceCollector()
        sat = collector.get_summary()['decode_saturation']
        assert sat['buckets'] == {}

    def test_decode_layers_in_compact_dict(self) -> None:
        trace = _trace(3, 21.0)
        compact = trace.to_compact_dict()
        assert compact['decode_layers'] == 3
        # 零值默认不出现在紧凑序列化
        assert 'decode_layers' not in EditPlaybackFrameTrace().to_compact_dict()

    def test_full_gpu_untouched_by_new_field(self) -> None:
        trace = _trace(2, 10.0)
        trace.decode_path = 'cpu_planar'
        assert trace.is_full_gpu() is False  # cpu_planar 不在 GPU 集合


class TestTelemetryEnvGate:
    """env 门控解析。注意：不 reload renderer 模块——巨型 Qt 模块的
    reload 会重定义信号/线程类，污染同进程后续测试；直接重置模块级
    缓存变量后再触发解析即可。"""

    @staticmethod
    def _reset_gate() -> None:
        import src.features.edit.core.timeline_preview_renderer as renderer_mod

        renderer_mod._DECODE_TELEMETRY_RESOLVED = False
        renderer_mod._DECODE_TELEMETRY_DUMP_EVERY = None
        return renderer_mod

    def test_default_off_without_env(self, monkeypatch) -> None:
        renderer_mod = self._reset_gate()
        monkeypatch.delenv('TOPOS_PLAYBACK_DECODE_TELEMETRY', raising=False)
        assert renderer_mod._decode_telemetry_dump_every() == 0

    def test_env_positive_enables_dump(self, monkeypatch) -> None:
        renderer_mod = self._reset_gate()
        monkeypatch.setenv('TOPOS_PLAYBACK_DECODE_TELEMETRY', '120')
        assert renderer_mod._decode_telemetry_dump_every() == 120

    def test_env_garbage_falls_back_to_off(self, monkeypatch) -> None:
        renderer_mod = self._reset_gate()
        monkeypatch.setenv('TOPOS_PLAYBACK_DECODE_TELEMETRY', 'not-a-number')
        assert renderer_mod._decode_telemetry_dump_every() == 0
