"""
Realtime Playback Telemetry
专业 NLE 实时播放每帧遥测数据结构与聚合器。

对应规格书 §18。

设计要点：
- 每帧必须输出 PlaybackFrameTelemetry，记录完整 pipeline 耗时与 source 决策。
- TelemetryAggregator 维护滑动窗口，输出 p50/p95/max、source kind 分布、hit rate。
- 所有 master_requests / cpu_qimage_frames / wrong_source_frame 等关键断言指标
  都来自这里，便于 benchmark 与 regression test 复用。
"""

from __future__ import annotations

import math
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Tuple

from src.shared.video.frame_trace_schema import (
    PRESENT_STATUSES,
    STAGE_VALIDITY_STATES,
)

from .types import PlaybackMode, SourceKind

#: A0.1：telemetry 覆盖的 pipeline 段名（与 frame_trace_schema 的分段
#: 词表同源；'' / 缺省键 = unset）。
TELEMETRY_STAGES: Tuple[str, ...] = (
    "decode", "import_upload", "composite", "present", "total",
)

#: 段名 -> PlaybackFrameTelemetry 字段名（summary 覆盖率采样用）。
_STAGE_FIELD_NAMES: Dict[str, str] = {
    "decode": "decode_ms",
    "import_upload": "upload_ms",
    "composite": "composite_ms",
    "present": "present_ms",
    "total": "total_ms",
}


@dataclass
class PlaybackFrameTelemetry:
    """
    单帧播放遥测数据（规格书 §18.1）。

    所有 *_ms 字段是该段 pipeline 的耗时（毫秒）。A0.1（schema v3）起
    每段另带有效性状态（stage_status，词表 STAGE_VALIDITY_STATES）：
    实测 0ms（measured）与未测量（unset）/该帧无需该段（not_applicable）/
    不可测量（unavailable）分开表示，summary 据此输出样本覆盖率，
    禁止用默认 0 冒充有效样本。
    """

    timeline_frame: int = -1
    generation_id: int = 0
    # P0-01/P0-02：统一 trace id（engine 入口分配，与 FrameTraceCollector
    # 的 EditPlaybackFrameTrace.trace_id 同源，可跨通道对拍）。
    trace_id: int = 0
    mode: str = "realtime"
    priority: str = ""
    backend: str = ""
    source_kinds: Tuple[str, ...] = ()
    proxy_ready: bool = False
    render_cache_ready: bool = False
    ring_hit: bool = False
    decode_ms: float = 0.0
    upload_ms: float = 0.0
    composite_ms: float = 0.0
    present_ms: float = 0.0
    total_ms: float = 0.0
    miss_reason: str | None = None

    # A0.1：段有效性（stage -> STAGE_VALIDITY_STATES；缺键 = unset）。
    stage_status: Dict[str, str] = field(default_factory=dict)
    # A0.1：TPR 可观测的最终呈现结果（PRESENT_STATUSES；'' = 未观测，
    # displayed 仅存在于 trace 通道——engine/telemetry 看不到 Viewer paint）。
    present_outcome: str = ""

    # 关键断言辅助指标
    master_requested: bool = False
    cpu_qimage_used: bool = False
    wrong_source_frame: bool = False
    stale_generation: bool = False

    def mark_stage(self, stage: str, status: str) -> None:
        """标记一个 pipeline 段的有效性（词表外取值直接抛错）。"""
        if stage not in _STAGE_FIELD_NAMES:
            raise ValueError(f"unknown telemetry stage {stage!r}; "
                             f"expected one of {TELEMETRY_STAGES}")
        if status not in STAGE_VALIDITY_STATES:
            raise ValueError(f"invalid stage status {status!r}; "
                             f"expected one of {STAGE_VALIDITY_STATES}")
        self.stage_status[stage] = status

    def stage_is_measured(self, stage: str) -> bool:
        """该段是否为有效样本：显式 measured，或旧记录无状态但值>0。"""
        status = self.stage_status.get(stage)
        if status is not None:
            return status == "measured"
        return getattr(self, _STAGE_FIELD_NAMES[stage]) > 0

    @property
    def is_realtime_mode(self) -> bool:
        return self.mode == PlaybackMode.REALTIME.value


def _percentile(sorted_values: List[float], pct: float) -> float:
    """计算分位数（输入必须已排序升序）。"""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    # 线性插值
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    lower = int(math.floor(rank))
    upper = int(math.ceil(rank))
    if lower == upper:
        return sorted_values[lower]
    frac = rank - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * frac


class TelemetryAggregator:
    """
    滑动窗口遥测聚合器（线程安全）。

    用法：
        agg = TelemetryAggregator(window=120)
        for trace in traces:
            agg.record(trace)
        summary = agg.summary()
        assert summary["master_requests"] == 0
    """

    def __init__(self, window: int = 120) -> None:
        self._lock = threading.Lock()
        self._window = max(8, int(window))
        self._records: Deque[PlaybackFrameTelemetry] = deque(maxlen=self._window)
        self._session_totals: Dict[str, int] = {
            "frames": 0,
            "master_requests": 0,
            "cpu_qimage_frames": 0,
            "wrong_source_frame": 0,
            "stale_generation": 0,
            "realtime_master_requests": 0,  # realtime 模式下的 master 请求（必须为 0）
            "ring_hits": 0,
            "gpu_composite_frames": 0,
            "miss_count": 0,
            # A0.1：enrich_last 匹配失败计数（此前调用方直接吞掉返回值，
            # 回填丢失不可见）。
            "enrich_misses": 0,
        }
        self._source_kind_counter: Counter[str] = Counter()
        self._present_outcome_counter: Counter[str] = Counter()
        self._first_record_ts: float | None = None
        self._last_record_ts: float | None = None

    # ------------------------------------------------------------------
    # record / reset
    # ------------------------------------------------------------------

    def record(self, trace: PlaybackFrameTelemetry) -> None:
        """记录一帧遥测。

        A0.1：stage_status 的键值受词表约束（未知段名/状态直接抛
        ValueError，禁止任意字段静默进入聚合）。
        """
        for stage, status in trace.stage_status.items():
            if stage not in _STAGE_FIELD_NAMES:
                raise ValueError(
                    f"unknown telemetry stage {stage!r} in record(); "
                    f"expected one of {TELEMETRY_STAGES}")
            if status not in STAGE_VALIDITY_STATES:
                raise ValueError(
                    f"invalid stage status {status!r} for stage {stage!r}; "
                    f"expected one of {STAGE_VALIDITY_STATES}")
        now = time.perf_counter()
        with self._lock:
            self._records.append(trace)
            self._session_totals["frames"] += 1
            if trace.master_requested:
                self._session_totals["master_requests"] += 1
                if trace.is_realtime_mode:
                    self._session_totals["realtime_master_requests"] += 1
            if trace.cpu_qimage_used:
                self._session_totals["cpu_qimage_frames"] += 1
            if trace.wrong_source_frame:
                self._session_totals["wrong_source_frame"] += 1
            if trace.stale_generation:
                self._session_totals["stale_generation"] += 1
            if trace.ring_hit:
                self._session_totals["ring_hits"] += 1
            if trace.present_outcome:
                self._present_outcome_counter[trace.present_outcome] += 1
            if trace.miss_reason is None:
                # 没有 miss 且非 placeholder 才算成功合成（GPU composite）
                # 这里用 composite_ms > 0 近似表示有 GPU composite；
                # 真正标记由调用方通过 source_kinds 判定。
                if any(k == SourceKind.DISK_PROXY.value for k in trace.source_kinds):
                    if trace.composite_ms > 0 or trace.total_ms > 0:
                        self._session_totals["gpu_composite_frames"] += 1
            else:
                self._session_totals["miss_count"] += 1
            for k in trace.source_kinds:
                self._source_kind_counter[k] += 1
            if self._first_record_ts is None:
                self._first_record_ts = now
            self._last_record_ts = now

    def enrich_last(
        self,
        timeline_frame: int,
        decode_ms: float | None = None,
        total_ms: float | None = None,
        composite_ms: float | None = None,
        trace_id: int = 0,
        *,
        upload_ms: float | None = None,
        upload_status: str = "",
        present_ms: float | None = None,
        present_status: str = "",
    ) -> bool:
        """渲染完成后回填该帧记录的真实管线耗时。

        request_present 在派发时记录的 total_ms 只是调度耗时（~0.15ms），
        解码/合成的真实成本由渲染 worker 事后才知道。按帧号从新到旧找到
        记录并覆盖，使 summary 的 decode/total/effective_fps 反映真实
        管线（此前 effective_fps≈6445、decode_ms≡0 全部失真）。

        P0-01：trace_id 非零时优先按 trace_id 精确匹配（消除同帧多次
        重渲染/跳帧时的回填歧义）；匹配不到再退回帧号匹配。

        A0.1：decode/total/composite 之外新增 upload/present 段回填；
        每个传入的段自动标记 stage_status='measured'（实测 0.0 也算
        有效样本）。upload_status/present_status 可单独传
        'not_applicable'/'unavailable' 表示该帧无需该段或不可测量。
        匹配失败累计 enrich_misses（summary 可见，不再静默）。
        """
        target = None
        with self._lock:
            if trace_id:
                for record in reversed(self._records):
                    if record.trace_id == trace_id:
                        target = record
                        break
            if target is None:
                for record in reversed(self._records):
                    if record.timeline_frame == timeline_frame:
                        target = record
                        break
                    if record.timeline_frame < timeline_frame - 64:
                        # 播放前进方向上找不到即可提前停止（跳帧回填场景）
                        break
            if target is None:
                self._session_totals["enrich_misses"] += 1
                return False
            if decode_ms is not None:
                target.decode_ms = float(decode_ms)
                target.stage_status["decode"] = "measured"
            if total_ms is not None:
                target.total_ms = float(total_ms)
                target.stage_status["total"] = "measured"
            if composite_ms is not None:
                target.composite_ms = float(composite_ms)
                target.stage_status["composite"] = "measured"
            if upload_ms is not None:
                target.upload_ms = float(upload_ms)
                target.stage_status["import_upload"] = "measured"
            elif upload_status:
                if upload_status not in STAGE_VALIDITY_STATES:
                    raise ValueError(f"invalid upload_status {upload_status!r}")
                target.stage_status["import_upload"] = upload_status
            if present_ms is not None:
                target.present_ms = float(present_ms)
                target.stage_status["present"] = "measured"
            elif present_status:
                if present_status not in STAGE_VALIDITY_STATES:
                    raise ValueError(f"invalid present_status {present_status!r}")
                target.stage_status["present"] = present_status
            return True

    def note_present_outcome(
        self, trace_id: int, timeline_frame: int, outcome: str,
    ) -> bool:
        """A0.1：回填该帧的最终呈现结果（TPR 可观测子集）。

        outcome ∈ PRESENT_STATUSES（'displayed' 仅存在于 trace 通道，
        engine/telemetry 侧不应传入）。按 trace_id 精确匹配，匹配不到
        退回帧号；两者都失败返回 False（计入 enrich_misses）。
        """
        if outcome not in PRESENT_STATUSES:
            raise ValueError(f"invalid present outcome {outcome!r}; "
                             f"expected one of {PRESENT_STATUSES}")
        target = None
        with self._lock:
            if trace_id:
                for record in reversed(self._records):
                    if record.trace_id == trace_id:
                        target = record
                        break
            if target is None:
                for record in reversed(self._records):
                    if record.timeline_frame == timeline_frame:
                        target = record
                        break
                    if record.timeline_frame < timeline_frame - 64:
                        break
            if target is None:
                self._session_totals["enrich_misses"] += 1
                return False
            previous = target.present_outcome
            target.present_outcome = outcome
            if previous:
                self._present_outcome_counter[previous] -= 1
                if self._present_outcome_counter[previous] <= 0:
                    del self._present_outcome_counter[previous]
            self._present_outcome_counter[outcome] += 1
            return True

    def reset(self) -> None:
        with self._lock:
            self._records.clear()
            for key in list(self._session_totals.keys()):
                self._session_totals[key] = 0
            self._source_kind_counter.clear()
            self._present_outcome_counter.clear()
            self._first_record_ts = None
            self._last_record_ts = None

    # ------------------------------------------------------------------
    # summary
    # ------------------------------------------------------------------

    def summary(self) -> Dict[str, object]:
        """输出当前窗口的聚合摘要。"""
        with self._lock:
            records = list(self._records)
            totals = dict(self._session_totals)
            source_counts = dict(self._source_kind_counter)
            present_outcomes = dict(self._present_outcome_counter)
            # AUD-17418：ts 快照必须在锁内完成——锁外读会与 reset()/
            # record() 竞态（读到 None 或与 records 不一致的时间戳）。
            first_ts = self._first_record_ts or 0.0
            last_ts = self._last_record_ts or 0.0

        if not records:
            return {
                "frames": 0,
                "window": self._window,
                "averaged_total_ms": 0.0,
                "master_requests": 0,
                "realtime_master_requests": 0,
                "cpu_qimage_frames": 0,
                "wrong_source_frame": 0,
                "stale_generation": 0,
                "ring_hit_rate": 0.0,
                "effective_fps": 0.0,
                "source_kinds": {},
                "miss_count": 0,
                "session_total_frames": totals.get("frames", 0),
                # A0.1：空窗口也保持新键形状
                "enrich_misses": totals.get("enrich_misses", 0),
                "present_outcome_counts": present_outcomes,
                "coverage": {},
            }

        # A0.1：按段有效性选样（显式 measured 优先；无状态标注的旧记录
        # 退回 value>0 判定保持兼容），并输出样本数/覆盖率——实测 0 与
        # 未测量不得混淆，被排除的帧在 coverage 里可见。
        stage_samples: Dict[str, List[float]] = {}
        for stage, field_name in _STAGE_FIELD_NAMES.items():
            stage_samples[stage] = [
                getattr(r, field_name) for r in records if r.stage_is_measured(stage)
            ]
        coverage = {
            stage: {
                "samples": len(values),
                "coverage": round(len(values) / float(len(records)), 4),
            }
            for stage, values in stage_samples.items()
        }

        totals_ms = stage_samples["total"]
        decode_ms = stage_samples["decode"]
        composite_ms = stage_samples["composite"]
        upload_ms = stage_samples["import_upload"]
        present_ms_samples = stage_samples["present"]

        totals_ms_sorted = sorted(totals_ms)
        decode_sorted = sorted(decode_ms)
        composite_sorted = sorted(composite_ms)
        upload_sorted = sorted(upload_ms)
        present_sorted = sorted(present_ms_samples)

        window_frames = len(records)
        # AUD-17418：first_ts/last_ts 已在上方锁内快照。
        elapsed = max(1e-6, last_ts - first_ts)
        # 有效 fps：用平均 total_ms 推算（避免 1 帧 / 长 interval 失真）
        avg_total = (sum(totals_ms) / len(totals_ms)) if totals_ms else 0.0
        effective_fps = (1000.0 / avg_total) if avg_total > 0 else 0.0

        ring_hit_rate = (
            totals["ring_hits"] / float(window_frames) if window_frames else 0.0
        )

        source_kinds_pct = {
            k: round(v / float(window_frames), 4) for k, v in source_counts.items()
        }

        summary: Dict[str, object] = {
            "frames": window_frames,
            "window": self._window,
            "total_ms_mean": round(avg_total, 3) if totals_ms else 0.0,
            "total_ms_p50": round(_percentile(totals_ms_sorted, 50), 3),
            "total_ms_p95": round(_percentile(totals_ms_sorted, 95), 3),
            "total_ms_max": round(totals_ms_sorted[-1], 3) if totals_ms_sorted else 0.0,
            "decode_ms_mean": round(sum(decode_ms) / len(decode_ms), 3) if decode_ms else 0.0,
            "decode_ms_p50": round(_percentile(decode_sorted, 50), 3),
            "decode_ms_p95": round(_percentile(decode_sorted, 95), 3),
            "composite_ms_mean": round(sum(composite_ms) / len(composite_ms), 3) if composite_ms else 0.0,
            "composite_ms_p50": round(_percentile(composite_sorted, 50), 3),
            "composite_ms_p95": round(_percentile(composite_sorted, 95), 3),
            "upload_ms_mean": round(sum(upload_ms) / len(upload_ms), 3) if upload_ms else 0.0,
            "upload_ms_p50": round(_percentile(upload_sorted, 50), 3),
            "upload_ms_p95": round(_percentile(upload_sorted, 95), 3),
            "present_ms_mean": round(sum(present_ms_samples) / len(present_ms_samples), 3) if present_ms_samples else 0.0,
            "present_ms_p50": round(_percentile(present_sorted, 50), 3),
            "present_ms_p95": round(_percentile(present_sorted, 95), 3),
            "effective_fps": round(effective_fps, 2),
            # 关键断言指标
            "master_requests": totals["master_requests"],
            "realtime_master_requests": totals["realtime_master_requests"],
            "cpu_qimage_frames": totals["cpu_qimage_frames"],
            "wrong_source_frame": totals["wrong_source_frame"],
            "stale_generation": totals["stale_generation"],
            "ring_hit_rate": round(ring_hit_rate, 4),
            "gpu_composite_frames": totals["gpu_composite_frames"],
            "miss_count": totals["miss_count"],
            "source_kinds": source_kinds_pct,
            "session_total_frames": totals["frames"],
            "wall_elapsed_s": round(elapsed, 3),
            # A0.1：段样本覆盖率 + 回填失败计数 + 呈现结果分布
            "coverage": coverage,
            "enrich_misses": totals.get("enrich_misses", 0),
            "present_outcome_counts": present_outcomes,
        }
        return summary

    # ------------------------------------------------------------------
    # 断言辅助
    # ------------------------------------------------------------------

    def assert_realtime_invariants(self) -> Tuple[bool, str]:
        """校验规格书 §25 的关键断言。返回 (passed, message)。"""
        s = self.summary()
        if s["realtime_master_requests"] != 0:
            return False, f"realtime master_requests={s['realtime_master_requests']} (expect 0)"
        if s["wrong_source_frame"] != 0:
            return False, f"wrong_source_frame={s['wrong_source_frame']} (expect 0)"
        if s["stale_generation"] != 0:
            return False, f"stale_generation={s['stale_generation']} (expect 0)"
        return True, "all realtime invariants passed"
