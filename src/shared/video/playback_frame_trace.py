"""
Edit Playback Frame Trace Module
剪辑页播放链路每帧诊断追踪。

## 职责

- EditPlaybackFrameTrace: 每帧 pipeline 状态数据结构
- FrameTraceCollector: ring buffer + JSON 导出
- 统一记录 decode_path、render_path、fallback_reason、sync_seek 等

## 使用示例

```python
from src.shared.video.playback_frame_trace import (
    EditPlaybackFrameTrace,
    FrameTraceCollector,
    get_trace_collector,
)

collector = get_trace_collector()
collector.start_session(page='edit_timeline')

# 每帧记录
trace = EditPlaybackFrameTrace(
    frame_index=100,
    asset_path='/path/to/video.mp4',
    decode_path='macos_vt_iosurface_zero_copy',
    render_path='gpu_yuv_to_rgb_composite',
)
collector.record(trace)

# 导出
report = collector.export_json()
```
"""

from __future__ import annotations

import json
import time
import threading
import logging
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Deque, Dict, List, Any, Optional

from src.shared.video.frame_trace_schema import (
    FRAME_TRACE_SCHEMA_VERSION,
    PRESENT_STATUSES,
    READBACK_KINDS,
    STAGE_VALIDITY_STATES as _STAGE_VALIDITY_TUPLE,
    segment_budgets_ms,
)

logger = logging.getLogger(__name__)


# AUD-39315: is_full_gpu 的 decode 白名单必须与全库实际产出的
# decode_path 词表一致。旧集合 {'hw_decode_planar', 'gpu_direct',
# 'gpu'} 中前两个不是任何生产方写出的值，导致 GPU/硬解帧全部被
# 误判为"非全 GPU"，汇总里 cpu_fallback_frames 虚高。注意
# 'macos_vt_zero_copy' 是真实产出值（timeline_preview_renderer 的
# gpu/vt_pixel_buffer 层标记），必须保留在集合内。
# 词表来源：
# - DecodePath 四个零拷贝成员（src/shared/media/frame_path.py）：
#   macos_vt_iosurface_zero_copy / macos_topos_iosurface_zero_copy /
#   macos_vt_cvmetal_zero_copy / windows_d3d11_gl_zero_copy
# - trace 生产方的 GPU 泛化值：'gpu'（timeline_preview_renderer 兜底）、
#   'gpu_provider' / 'gpu_async_submitted'（source_viewer GPU 路径）
# 注意：hwaccel_download_* 是"硬解后下载回 CPU"的路径，不算全 GPU，
# 不在白名单内。
_GPU_DECODE_PATHS: frozenset = frozenset({
    'macos_vt_zero_copy',
    'macos_vt_iosurface_zero_copy',
    'macos_topos_iosurface_zero_copy',
    'macos_vt_cvmetal_zero_copy',
    'windows_d3d11_gl_zero_copy',
    'gpu',
    'gpu_provider',
    'gpu_async_submitted',
})

_GPU_RENDER_PATHS: frozenset = frozenset({
    'gpu_direct', 'gpu_planar_upload', 'gpu_composite',
})

# A0.1（schema v3）：annotate() 迟到合并的字段白名单与累加型字节字段。
# 不在白名单内的字段（extra 除外）会被显式拒绝，防止任意字段静默进入
# 报告。字节字段为累加合并：同一帧的多份迟到证据（planar 上传、快照
# 回读、监看回读）各自累加。
_ANNOTATE_BYTE_FIELDS: frozenset = frozenset({
    'cpu_upload_bytes', 'cpu_download_bytes', 'surface_map_bytes',
    'texture_write_bytes', 'cross_device_copy_bytes',
})

_ANNOTATE_FIELD_WHITELIST: frozenset = frozenset({
    'cpu_upload_bytes', 'cpu_download_bytes', 'surface_map_bytes',
    'texture_write_bytes', 'cross_device_copy_bytes',
    'import_upload_ms', 'import_upload_status',
    'decode_path', 'render_path',
    'decode_layers', 'transition_layers',
    'gpu_submit_ms', 'gpu_time_unavailable',
    'present_status', 'present_slot_id', 'present_submitted_mono',
    'readback_kind',
})


@dataclass
class EditPlaybackFrameTrace:
    """
    单帧 pipeline 状态追踪数据。

    每帧渲染完成后填充，记录该帧经过的完整 pipeline 路径。

    P0-01/P0-02：trace_id 由 RealtimePlaybackEngine.request_present 入口
    分配并逐段携带；trace_id == 0 表示非 engine 派发的旁路帧。
    P0-03：gpu_submit_ms 是 GL timer query 实测的合成 pass GPU 时间，
    未测量时 gpu_time_unavailable=True（禁止写 0.0 冒充）。
    P0-04：*_bytes 是本帧 CPU/GPU 像素拷贝字节（0 = 未发生）。
    """

    # 帧标识
    frame_index: int = -1
    source_frame: int = -1
    asset_path: str = ''
    page: str = ''  # edit_source, edit_timeline, color, composite

    # 统一身份（P0-01/P0-02）
    schema_version: int = FRAME_TRACE_SCHEMA_VERSION
    trace_id: int = 0  # 0 = 非 engine 派发（scrub/缩略图/单素材页旁路）
    priority: str = ''  # TASK_PRIORITY_LEVELS 词表
    backend: str = ''  # GRAPHICS_BACKENDS 词表
    deadline_ns: int = 0
    #: P1-01 路由通道正字段：engine（播放 tick 经 request_present）/
    #: scrub（scrub 经 request_scrub_present）/ background（idle 预缓冲）/
    #: direct（暂停刷新等合法旁路）/ refresh（剪辑命令单帧刷新）。
    #: 验收口径：播放会话内不得出现 trace_id==0 且 dispatch_channel
    #: 不属于 {engine, scrub, background, refresh} 的"无主播放帧"。
    dispatch_channel: str = ''

    # Pipeline 路径
    decode_path: str = ''  # macos_vt_zero_copy, hw_decode_planar, cpu_packed_rgb, cpu_planar, unknown
    render_path: str = ''  # gpu_direct, gpu_planar_upload, gpu_composite, cpu_qimage_upload, cpu_numpy_upload, unknown

    # 问题标记
    fallback_reason: str = ''  # 空=无fallback
    sync_seek_count: int = 0  # 本帧同步 seek 次数（应为0）
    cpu_to_ndarray: bool = False  # 是否发生 CPU to_ndarray()
    qimage_copy: bool = False  # 是否发生 QImage.copy()
    gl_tex_image_upload: bool = False  # 是否 glTexImage2D 全量上传
    gl_tex_sub_image_upload: bool = False  # 是否 glTexSubImage2D 局部更新

    # GPU texture 生命周期
    gpu_texture_slot: int = -1  # GPU renderer 使用的 texture slot
    texture_slot_reused_without_ack: bool = False  # 是否检测到未 ack 复用

    # 帧序守卫
    stale_frame_emitted: bool = False  # 是否发出了旧帧
    request_id: int = 0
    generation_id: int = 0
    playback_epoch: int = 0

    # 性能计时
    decode_ms: float = 0.0
    render_ms: float = 0.0
    total_ms: float = 0.0
    #: P0-03：GL timer query 实测的合成 pass GPU 提交时间（毫秒）。
    #: 仅当 gpu_time_unavailable=False 时有意义。
    gpu_submit_ms: float = 0.0
    #: P0-03：GPU 时间不可用标记（驱动不支持 timer query / 测量失败）。
    #: True 时消费方必须显示 gpu_time_unavailable，不得把 gpu_submit_ms=0 当作极快。
    gpu_time_unavailable: bool = True
    #: 本帧并行解码流数（可见可解码轨数；W3 立项遥测维度——decode_ms
    #: 按 decode_layers 分桶随轨数恶化 = CPU 解码核饱和的触发信号）
    decode_layers: int = 0
    #: 本帧涉及的转场数（转场段 FPS 分桶基线——P0-PREV-GPU-T 前后对比
    #: 转场段渲染耗时/回退率的关键维度；0 = 非转场帧）
    transition_layers: int = 0
    timestamp: float = field(default_factory=time.monotonic)

    # 像素拷贝字节审计（P0-04；0 = 本帧未发生该类拷贝）
    cpu_upload_bytes: int = 0
    cpu_download_bytes: int = 0
    surface_map_bytes: int = 0
    texture_write_bytes: int = 0
    cross_device_copy_bytes: int = 0

    # P1-06 分段 deadline（schema v2）
    #: 首个越界的 pipeline 段（'' = 无 deadline 或全部达标；词表 =
    #: decode / import_upload / effects / transition / composite / total）。
    #: 由 FrameTraceCollector.record() 经 evaluate_deadline_miss() 统一归因。
    deadline_missed_segment: str = ''
    #: 预留段位（import/upload 与 present 尚未埋点，字段先行保证
    #: schema 稳定；0 = 未测量）。
    import_upload_ms: float = 0.0
    present_ms: float = 0.0

    # A0.1 帧预算遥测契约（schema v3；词表见 frame_trace_schema）
    #: import/upload 段有效性（STAGE_VALIDITY_STATES；'' = unset）。
    #: 零拷贝 IOSurface 导入帧应标 not_applicable，实测 0ms 的 planar
    #: 上传帧标 measured——两种"0"不得混淆。
    import_upload_status: str = ''
    #: 呈现状态（PRESENT_STATUSES；'' = 尚无呈现事件）。一帧可顺序经历
    #: submitted → viewer_copy → displayed → slot_released，或
    #: submitted → superseded/slot_released、submitted → dropped。
    present_status: str = ''
    #: 提交时携带的 present 槽位（-1 = 无槽位跟踪的降级 descriptor）。
    present_slot_id: int = -1
    #: submitted 事件的单调时钟（time.monotonic()，仅用于计算
    #: present_ms = displayed 时点 - submitted 时点；不与 wall-clock 混用）。
    present_submitted_mono: float = 0.0
    #: 本帧 GPU→CPU 回读分类（READBACK_KINDS；'' = 本帧无回读）。
    readback_kind: str = ''

    # 扩展字段
    extra: Dict[str, Any] = field(default_factory=dict)

    def is_full_gpu(self) -> bool:
        """判断该帧是否走完全 GPU 路径（无 CPU 回退）"""
        # Multi-track frames join per-layer decode paths with ',' — every
        # segment must be a GPU decode path, not just the joined string.
        decode_paths = {
            p.strip() for p in str(self.decode_path or '').split(',') if p.strip()
        }
        render_paths = {
            p.strip() for p in str(self.render_path or '').split(',') if p.strip()
        }
        return (
            bool(decode_paths)
            and decode_paths <= _GPU_DECODE_PATHS
            and bool(render_paths)
            and render_paths <= _GPU_RENDER_PATHS
            and not self.fallback_reason
            and self.sync_seek_count == 0
            and not self.cpu_to_ndarray
            and not self.qimage_copy
            and not self.gl_tex_image_upload
            and not self.texture_slot_reused_without_ack
        )

    def evaluate_deadline_miss(self) -> str:
        """P1-06: 按 SEGMENT_BUDGET_RATIOS 归因首个越界段。

        已测段（decode_ms、gpu_submit_ms≈composite）先按段预算逐段检查；
        段内都达标但 total_ms 超总预算记 'total'（越界发生在未测段：
        import/effect/transition/present）。无 deadline 时返回 ''。
        归因结果同时写入 self.deadline_missed_segment（幂等）。
        """
        if self.deadline_missed_segment:
            return self.deadline_missed_segment
        budgets = segment_budgets_ms(self.deadline_ns)
        if not budgets:
            return ''
        if self.decode_ms > budgets["decode"]:
            miss = "decode"
        elif (not self.gpu_time_unavailable
              and self.gpu_submit_ms > budgets["composite"]):
            miss = "composite"
        elif self.import_upload_ms > budgets["import_upload"]:
            # T-03：planar 上传耗时经 annotate() 迟到合并后可归因
            miss = "import_upload"
        elif self.total_ms > (self.deadline_ns / 1e6):
            miss = "total"
        else:
            miss = ''
        self.deadline_missed_segment = miss
        return miss

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return asdict(self)

    def to_compact_dict(self) -> Dict[str, Any]:
        """转换为紧凑字典（省略默认值）"""
        d = {}
        data = asdict(self)
        for k, v in data.items():
            if k == 'extra':
                if v:
                    d[k] = v
            elif isinstance(v, bool):
                if v:
                    d[k] = v
            elif isinstance(v, (int, float)):
                if v != 0 and v != 0.0:
                    d[k] = v
            elif isinstance(v, str):
                if v:
                    d[k] = v
            else:
                d[k] = v
        # 始终包含关键字段
        for key in ('frame_index', 'trace_id', 'decode_path', 'render_path',
                    'fallback_reason'):
            if key not in d:
                d[key] = data[key]
        return d


class FrameTraceCollector:
    """
    帧追踪收集器。

    维护最近 N 帧的 ring buffer，支持 JSON 导出和统计摘要。
    线程安全。
    """

    def __init__(self, max_frames: int = 600) -> None:
        self._max_frames = max_frames
        self._lock = threading.RLock()
        self._traces: List[EditPlaybackFrameTrace] = []
        self._session_active: bool = False
        self._session_page: str = ''
        self._session_start: float = 0.0
        # A0.1：迟到证据合并成功/失败计数（失败 = 滚出 ring 或 trace_id=0，
        # 不得静默——GPU query 迟到回填的丢弃率从这里可见）。
        self._annotate_merged: int = 0
        self._annotate_missed: int = 0
        # A1.1：GC 停顿记录（由 GcPauseObserver 推送；monotonic 时间轴与
        # trace.timestamp 同源，供 summary()['gc'] 做 deadline miss 重叠关联）。
        self._gc_pauses: Deque[tuple] = deque(maxlen=128)

    def start_session(self, page: str = '') -> None:
        """开始新的追踪会话"""
        with self._lock:
            self._traces.clear()
            self._session_active = True
            self._session_page = page
            self._session_start = time.monotonic()
            self._annotate_merged = 0
            self._annotate_missed = 0
            logger.info(f"FrameTraceCollector: session started, page={page}")

    def end_session(self) -> None:
        """结束追踪会话"""
        with self._lock:
            self._session_active = False
            logger.info(f"FrameTraceCollector: session ended, traces={len(self._traces)}")

    def record(self, trace: EditPlaybackFrameTrace) -> None:
        """记录一帧追踪数据（P1-06: record 是分段 deadline 归因的唯一咽喉）。"""
        try:
            trace.evaluate_deadline_miss()
        except Exception:  # noqa: BLE001 归因失败不丢 trace
            pass
        with self._lock:
            self._traces.append(trace)
            # Ring buffer: 超出最大帧数时丢弃最早的
            if len(self._traces) > self._max_frames:
                self._traces = self._traces[-self._max_frames:]

    def annotate(self, trace_id: int, **fields: Any) -> bool:
        """
        向已记录的 trace 迟到合并字段（T-03/P0-04/A0.1 证据链）。

        背景：GPU 渲染任务在 worker 侧 submit 后由 GL 线程异步执行，
        worker 记录 trace 时 planar 上传尚未发生。GL 完成回调拿到上传
        统计后通过本接口按 trace_id 合并进最近的对应记录。

        schema v3（A0.1）规则：
        - 字段白名单收紧：不在 ``_ANNOTATE_FIELD_WHITELIST`` 内且不是
          ``extra`` 的字段直接 raise ValueError——禁止任意字段静默进入
          报告（调用方拼写错误必须在开发期暴露）。
        - 5 个像素拷贝字节字段（cpu_upload_bytes 等）为**累加合并**：
          同一帧的多次迟到证据（planar 上传、快照回读、监看回读）
          各自累加，不再覆盖。
        - present_status='displayed' 且目标已带 present_submitted_mono 时，
          自动计算 present_ms = displayed 时点 - submitted 时点（单调时钟）。
        - 合并成功/失败分别计数（``summary()['annotate']``），失败不再
          静默——滚出 ring 或 trace_id=0 的迟到证据在报告里可见。

        Returns:
            找到并合并了目标 trace 返回 True；未找到（已滚出 ring /
            trace_id=0）返回 False。
        """
        if not trace_id:
            with self._lock:
                self._annotate_missed += 1
            return False
        unknown = [k for k in fields if k != "extra"
                   and k not in _ANNOTATE_FIELD_WHITELIST]
        if unknown:
            raise ValueError(
                f"annotate(): unknown field(s) {unknown}; "
                f"whitelist={sorted(_ANNOTATE_FIELD_WHITELIST)}"
            )
        for key in ("present_status", "readback_kind", "import_upload_status"):
            value = fields.get(key)
            if value:
                vocab = {"present_status": PRESENT_STATUSES,
                         "readback_kind": READBACK_KINDS,
                         "import_upload_status": _STAGE_VALIDITY_TUPLE}[key]
                if value not in vocab:
                    raise ValueError(
                        f"annotate(): {key}={value!r} not in vocabulary {vocab}"
                    )
        with self._lock:
            target = None
            for trace in reversed(self._traces[-128:]):
                if trace.trace_id == trace_id:
                    target = trace
                    break
            if target is None:
                self._annotate_missed += 1
                return False
            for key in _ANNOTATE_BYTE_FIELDS:
                if key in fields:
                    setattr(target, key, int(getattr(target, key))
                            + int(fields[key]))
            for key in _ANNOTATE_FIELD_WHITELIST:
                if key in fields and key not in _ANNOTATE_BYTE_FIELDS:
                    setattr(target, key, fields[key])
            extra_fields = fields.get("extra")
            if isinstance(extra_fields, dict):
                target.extra.update(extra_fields)
            if (fields.get("present_status") == "displayed"
                    and target.present_submitted_mono > 0):
                target.present_ms = round(
                    (time.monotonic() - target.present_submitted_mono) * 1000.0, 3)
            if (fields.get("import_upload_ms") or
                    fields.get("present_status") == "displayed"):
                if not target.deadline_missed_segment:
                    try:
                        target.evaluate_deadline_miss()
                    except Exception:  # noqa: BLE001
                        pass
            self._annotate_merged += 1
            return True

    def note_gc_pause(self, stop_mono: float, duration_s: float,
                      generation: int = -1) -> None:
        """A1.1：记录一次 GC 停顿（GcPauseObserver 推送；有界 128 条）。

        时间轴为 monotonic，与 EditPlaybackFrameTrace.timestamp 同源；
        summary()['gc'] 据此计算 deadline miss 帧与停顿的重叠。
        """
        if duration_s is None or duration_s < 0:
            return
        with self._lock:
            self._gc_pauses.append(
                (float(stop_mono), float(duration_s), int(generation)))

    def get_traces(self, last_n: Optional[int] = None) -> List[EditPlaybackFrameTrace]:
        """获取追踪数据"""
        with self._lock:
            if last_n is not None:
                return list(self._traces[-last_n:])
            return list(self._traces)

    def get_summary(self, last_n: Optional[int] = None) -> Dict[str, Any]:
        """
        获取统计摘要。

        返回最近 N 帧的汇总统计，包括:
        - 总帧数
        - GPU 路径帧数
        - CPU fallback 帧数和原因分布
        - sync seek 次数
        - QImage upload 次数
        - 平均 decode/render/total ms
        """
        with self._lock:
            traces = self._traces if last_n is None else self._traces[-last_n:]
            if not traces:
                return {
                    'total_frames': 0,
                    # 空会话也保持键形状一致（消费方无需判空）
                    'decode_saturation': {
                        'budget_ms': 33.3, 'buckets': {},
                    },
                    'transition_frames': {
                        'transition': {'frames': 0},
                        'non_transition': {'frames': 0},
                    },
                'gpu_time': {'measured_frames': 0, 'unavailable_frames': 0},
                'pixel_copy_bytes': {
                    'cpu_upload': 0, 'cpu_download': 0, 'surface_map': 0,
                    'texture_write': 0, 'cross_device': 0,
                },
                # A0.1：空会话也保持新分节键形状一致
                'present_status_counts': {},
                'readback_kind_counts': {},
                'annotate': {'merged': 0, 'missed': 0},
                'import_upload_coverage': {
                    'measured_frames': 0, 'not_applicable_frames': 0,
                    'unavailable_frames': 0, 'unset_frames': 0,
                },
                # A1.1：GC 停顿与 deadline miss 的重叠关联（空会话键形状一致）
                'gc': {'pause_count': 0, 'pause_ms_total': 0.0,
                       'pause_ms_max': 0.0,
                       'deadline_miss_frames_overlapping_gc': 0},
            }

            total = len(traces)
            gpu_frames = sum(1 for t in traces if t.is_full_gpu())
            cpu_to_ndarray_frames = sum(1 for t in traces if t.cpu_to_ndarray)
            qimage_upload_frames = sum(1 for t in traces if t.qimage_copy or t.gl_tex_image_upload)
            sync_seek_frames = sum(1 for t in traces if t.sync_seek_count > 0)
            stale_frames = sum(1 for t in traces if t.stale_frame_emitted)
            slot_violations = sum(1 for t in traces if t.texture_slot_reused_without_ack)

            # Fallback 原因分布
            fallback_reasons: Dict[str, int] = {}
            for t in traces:
                if t.fallback_reason:
                    fallback_reasons[t.fallback_reason] = fallback_reasons.get(t.fallback_reason, 0) + 1

            # Decode path 分布
            decode_paths: Dict[str, int] = {}
            for t in traces:
                if t.decode_path:
                    decode_paths[t.decode_path] = decode_paths.get(t.decode_path, 0) + 1

            # Render path 分布
            render_paths: Dict[str, int] = {}
            for t in traces:
                if t.render_path:
                    render_paths[t.render_path] = render_paths.get(t.render_path, 0) + 1

            # 性能统计
            decode_times = [t.decode_ms for t in traces if t.decode_ms > 0]
            render_times = [t.render_ms for t in traces if t.render_ms > 0]
            total_times = [t.total_ms for t in traces if t.total_ms > 0]

            return {
                'page': self._session_page,
                'total_frames': total,
                'gpu_frames': gpu_frames,
                'cpu_fallback_frames': total - gpu_frames,
                'cpu_to_ndarray_frames': cpu_to_ndarray_frames,
                'qimage_upload_frames': qimage_upload_frames,
                'sync_seek_frames': sync_seek_frames,
                'stale_frame_emitted': stale_frames,
                'texture_slot_reuse_without_ack': slot_violations,
                'fallback_frames': total - gpu_frames,
                'fallback_reasons': fallback_reasons,
                'decode_path_counts': decode_paths,
                'render_path_counts': render_paths,
                'decode_avg_ms': sum(decode_times) / len(decode_times) if decode_times else 0.0,
                'render_avg_ms': sum(render_times) / len(render_times) if render_times else 0.0,
                'total_avg_ms': sum(total_times) / len(total_times) if total_times else 0.0,
                'ui_thread_sync_decode_frames': sum(1 for t in traces if t.sync_seek_count > 0),
                'gl_tex_image_upload_frames': sum(1 for t in traces if t.gl_tex_image_upload),
                'decode_saturation': self._summarize_decode_saturation(traces),
                'transition_frames': self._summarize_transition_frames(traces),
                # P0-03：GPU 段实测时间覆盖情况（unavailable 率必须可见）
                'gpu_time': self._summarize_gpu_time(traces),
                # P0-04：本会话像素拷贝字节合计（累计审计的 per-frame 侧写）
                'pixel_copy_bytes': self._summarize_pixel_copy_bytes(traces),
                # P1-06：分段 deadline 越界归因（按段计数）
                'deadline': self._summarize_deadline_misses(traces),
                # A0.1（schema v3）：呈现状态分布 / 回读分类分布 /
                # 迟到证据合并计数 / import_upload 段有效性覆盖。
                'present_status_counts': self._count_field(traces, 'present_status'),
                'readback_kind_counts': self._count_field(traces, 'readback_kind'),
                'annotate': {
                    'merged': self._annotate_merged,
                    'missed': self._annotate_missed,
                },
                'import_upload_coverage': self._summarize_stage_validity(traces),
                # A1.1：GC 停顿摘要 + 与 deadline miss 的重叠关联
                'gc': self._summarize_gc(traces),
            }

    def _summarize_gc(
        self, traces: List["EditPlaybackFrameTrace"] = (),
    ) -> Dict[str, Any]:
        """A1.1：GC 停顿聚合 + deadline miss 帧的渲染窗口重叠计数。

        重叠窗口 = [trace.timestamp - total_ms, trace.timestamp]（worker 在
        渲染结束时记录 trace）。只有重叠计数本身，不做因果断言——
        关联成立与否由消费方按计划 A1.1 的验收口径（延迟/掉帧/内存）判定。
        """
        with self._lock:
            pauses = list(self._gc_pauses)
        total_s = sum(d for _, d, _ in pauses)
        overlapping = 0
        if pauses and traces:
            for t in traces:
                if not t.deadline_missed_segment:
                    continue
                span_s = max(0.0, t.total_ms) / 1000.0
                t0 = t.timestamp - span_s
                t1 = t.timestamp
                for stop_ts, dur, _gen in pauses:
                    if max(stop_ts - dur, t0) < min(stop_ts, t1):
                        overlapping += 1
                        break
        return {
            'pause_count': len(pauses),
            'pause_ms_total': round(total_s * 1000.0, 3),
            'pause_ms_max': round(
                max((d for _, d, _ in pauses), default=0.0) * 1000.0, 3),
            'deadline_miss_frames_overlapping_gc': overlapping,
        }

    @staticmethod
    def _count_field(
        traces: List["EditPlaybackFrameTrace"], field_name: str,
    ) -> Dict[str, int]:
        """按词表字段值计数（空串跳过——unset 不进分布）。"""
        counts: Dict[str, int] = {}
        for t in traces:
            value = getattr(t, field_name, '')
            if value:
                counts[value] = counts.get(value, 0) + 1
        return counts

    @staticmethod
    def _summarize_stage_validity(
        traces: List["EditPlaybackFrameTrace"],
    ) -> Dict[str, int]:
        """A0.1：import_upload 段有效性覆盖（measured / not_applicable /
        unavailable / unset 四态分开计数，实测 0 与未测量不得混淆）。"""
        out = {'measured_frames': 0, 'not_applicable_frames': 0,
               'unavailable_frames': 0, 'unset_frames': 0}
        for t in traces:
            status = t.import_upload_status or 'unset'
            key = f'{status}_frames' if status != 'unset' else 'unset_frames'
            if key in out:
                out[key] += 1
        return out

    @staticmethod
    def _summarize_deadline_misses(
        traces: List["EditPlaybackFrameTrace"],
    ) -> Dict[str, Any]:
        """P1-06 deadline 越界摘要：总越界帧数 + 按段分布。"""
        miss_by_segment: Dict[str, int] = {}
        for t in traces:
            if t.deadline_missed_segment:
                miss_by_segment[t.deadline_missed_segment] = (
                    miss_by_segment.get(t.deadline_missed_segment, 0) + 1
                )
        evaluated = sum(1 for t in traces if t.deadline_ns > 0)
        return {
            "frames_with_deadline": evaluated,
            "miss_frames": sum(miss_by_segment.values()),
            "miss_by_segment": miss_by_segment,
        }

    @staticmethod
    def _summarize_gpu_time(
        traces: List["EditPlaybackFrameTrace"],
    ) -> Dict[str, Any]:
        """GPU 合成 pass 实测时间摘要（P0-03）。

        unavailable_frames > 0 表示有帧无法测 GPU 时间（驱动/上下文不支持），
        报告必须显式携带，不允许用 wall time 冒充 GPU time。
        """
        measured = sorted(t.gpu_submit_ms for t in traces
                          if not t.gpu_time_unavailable and t.gpu_submit_ms > 0)
        unavailable = sum(1 for t in traces if t.gpu_time_unavailable)
        out: Dict[str, Any] = {'measured_frames': len(measured),
                               'unavailable_frames': unavailable}
        if measured:
            n = len(measured)
            p95_idx = max(0, min(n - 1, int(round(n * 0.95)) - 1)) if n > 1 else 0
            out.update({
                'gpu_submit_ms_p50': round(measured[n // 2], 3),
                'gpu_submit_ms_p95': round(measured[p95_idx], 3),
                'gpu_submit_ms_max': round(measured[-1], 3),
            })
        return out

    @staticmethod
    def _summarize_pixel_copy_bytes(
        traces: List["EditPlaybackFrameTrace"],
    ) -> Dict[str, int]:
        """本会话帧级像素拷贝字节合计（P0-04 per-frame 侧写）。"""
        return {
            'cpu_upload': sum(t.cpu_upload_bytes for t in traces),
            'cpu_download': sum(t.cpu_download_bytes for t in traces),
            'surface_map': sum(t.surface_map_bytes for t in traces),
            'texture_write': sum(t.texture_write_bytes for t in traces),
            'cross_device': sum(t.cross_device_copy_bytes for t in traces),
        }

    @staticmethod
    def _summarize_decode_saturation(
        traces: List["EditPlaybackFrameTrace"],
        budget_ms: float = 33.3,
    ) -> Dict[str, Any]:
        """按并行解码流数分桶的 decode_ms 分布（多流解码波饱和遥测）。

        判读（W3 zero-copy 立项的 K-W1 触发信号采集，
        见 docs/windows_optimization_plan_2026-09-11.md §11）：
        - 单流（layers=1）decode_ms 已超预算 → 单流解码本身是瓶颈；
        - decode_ms 随 decode_layers 线性恶化且单流不超预算 → 多流下
          CPU 解码核饱和，硬解 zero-copy / 代理档才有立项收益；
        - over_budget 高但 decode_ms 不随轨数恶化 → 瓶颈在别的段。
        """
        buckets: Dict[int, List[float]] = {}
        for t in traces:
            if t.decode_ms > 0 and t.decode_layers > 0:
                buckets.setdefault(int(t.decode_layers), []).append(t.decode_ms)
        out: Dict[str, Any] = {'budget_ms': budget_ms, 'buckets': {}}
        for layers in sorted(buckets):
            times = sorted(buckets[layers])
            n = len(times)
            p50 = times[n // 2]
            p95_idx = max(0, min(n - 1, int(round(n * 0.95)) - 1)) if n > 1 else 0
            out['buckets'][str(layers)] = {
                'frames': n,
                'decode_ms_p50': round(p50, 2),
                'decode_ms_p95': round(times[p95_idx], 2),
                'decode_ms_max': round(times[-1], 2),
                'over_budget': sum(1 for v in times if v > budget_ms),
            }
        return out

    @staticmethod
    def _summarize_transition_frames(
        traces: List["EditPlaybackFrameTrace"],
        budget_ms: float = 33.3,
    ) -> Dict[str, Any]:
        """转场段与非转场帧的渲染耗时对比（P0-PREV-GPU-T 基线分桶）。

        判读：
        - transition 帧占比高且 total_ms 显著高于 non-transition → 转场段
          是播放瓶颈（GPU 转场接线前该分桶应显著恶化，接线后收敛）；
        - transition 帧的 fallback_reason 分布揭示回退词表
          （native_transition_cpu_compositor 等）。
        """
        def _bucket(items: List["EditPlaybackFrameTrace"]) -> Dict[str, Any]:
            totals = sorted(t.total_ms for t in items if t.total_ms > 0)
            out: Dict[str, Any] = {'frames': len(items)}
            if totals:
                n = len(totals)
                p95_idx = max(0, min(n - 1, int(round(n * 0.95)) - 1)) if n > 1 else 0
                out.update({
                    'total_ms_p50': round(totals[n // 2], 2),
                    'total_ms_p95': round(totals[p95_idx], 2),
                    'over_budget': sum(1 for v in totals if v > budget_ms),
                })
            fallbacks: Dict[str, int] = {}
            for t in items:
                if t.fallback_reason:
                    fallbacks[t.fallback_reason] = (
                        fallbacks.get(t.fallback_reason, 0) + 1
                    )
            if fallbacks:
                out['fallback_reasons'] = fallbacks
            return out

        transition = [t for t in traces if t.transition_layers > 0]
        normal = [t for t in traces if t.transition_layers == 0]
        return {
            'transition': _bucket(transition),
            'non_transition': _bucket(normal),
        }

    def export_json(self, last_n: Optional[int] = None) -> str:
        """导出为 JSON 字符串

        AUD-39317: summary 与 traces 必须取自同一份快照。_lock 是
        RLock（可重入），在持锁状态下调用 get_summary/get_traces，
        保证两者之间没有写入者插入（写入方同样需要 _lock）。
        """
        with self._lock:
            traces = self.get_traces(last_n)
            summary = self.get_summary(last_n)
        data = {
            'summary': summary,
            'traces': [t.to_compact_dict() for t in traces],
        }
        return json.dumps(data, indent=2, ensure_ascii=False, default=str)

    def clear(self) -> None:
        """清空追踪数据"""
        with self._lock:
            self._traces.clear()
            self._annotate_merged = 0
            self._annotate_missed = 0

    @property
    def is_active(self) -> bool:
        return self._session_active

    @property
    def trace_count(self) -> int:
        with self._lock:
            return len(self._traces)


# ================================================================
# 全局实例
# ================================================================

_global_collector: Optional[FrameTraceCollector] = None
_global_collector_lock = threading.Lock()


def get_trace_collector() -> FrameTraceCollector:
    """获取全局帧追踪收集器"""
    global _global_collector
    if _global_collector is None:
        with _global_collector_lock:
            if _global_collector is None:
                _global_collector = FrameTraceCollector()
    return _global_collector


def reset_trace_collector() -> None:
    """重置全局收集器"""
    global _global_collector
    with _global_collector_lock:
        if _global_collector is not None:
            _global_collector.clear()
            _global_collector = None
