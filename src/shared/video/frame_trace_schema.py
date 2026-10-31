"""
Unified Frame Trace Schema (P0-01)
统一帧追踪 id 与事件 schema（性能总计划 §6 P0-01 / P0-02）。

## 职责

- FRAME_TRACE_SCHEMA_VERSION: 唯一的播放帧 trace schema 版本号。
- next_frame_trace_id(): 进程内单调递增的每帧 trace id 分配器（线程安全）。
- FrameTraceContext: 一帧在各 pipeline 段之间传递的身份信封
  （trace_id / frame / generation / deadline / priority / backend）。
- 共享词表（pipeline stage、任务优先级、图形 backend、GPU 时间可用性），
  供 playback_frame_trace / telemetry / HUD / benchmark 复用，避免各模块
  各写一套字符串导致 trace 无法对拍。

## 约定

- trace_id == 0 表示"未经 RealtimePlaybackEngine 派发的帧"
  （scrub/缩略图/单素材页等旁路路径），合法且必须显式保留。
- 任何"未测量"的 GPU 时间不得写成 0.0 冒充极快：必须置
  gpu_time_unavailable=True（与 ExportGpuTrail 的 fail-closed 语义一致）。
- 日志与导出不得包含用户路径或素材内容（脱敏由导出层负责）。
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Schema 版本
# ---------------------------------------------------------------------------

#: 播放帧 trace 的 schema 版本。字段增删必须递增并在 docs 记录变更。
#: v2: P1-06 分段 deadline（deadline_missed_segment / import_upload_ms /
#: present_ms）+ SEGMENT_BUDGET_RATIOS 预算表；P1-01 路由通道正字段
#: dispatch_channel（engine/scrub/background/direct/refresh）。
#: v3: A0.1 帧预算遥测契约（docs/perf_frame_budget_schema_2026-10.md）——
#: present_status / present_slot_id / present_submitted_mono / readback_kind /
#: import_upload_status 字段 + STAGE_VALIDITY_STATES / PRESENT_STATUSES /
#: READBACK_KINDS 词表；annotate() 的字节字段改为累加合并并收紧白名单。
FRAME_TRACE_SCHEMA_VERSION = 3

# ---------------------------------------------------------------------------
# 共享词表
# ---------------------------------------------------------------------------

#: 一帧从请求到呈现的 pipeline 段（P0-01 验收：request → source → decode →
#: import/upload → effects → transition → composite → present 可串起）。
PIPELINE_STAGES = (
    "request",
    "source",
    "decode",
    "import_upload",
    "effects",
    "transition",
    "composite",
    "present",
    "encode",
    "mux",
)

#: 任务优先级协议（P1-02 的词表先于协议落地，trace 先带 priority 字段）。
#: scrub latest > playhead > imminent prefetch > cache fill > thumbnail/GIF > export
TASK_PRIORITY_LEVELS = (
    "scrub_latest",
    "playhead",
    "prefetch",
    "cache_fill",
    "thumbnail",
    "export",
)

#: 图形 backend 词表（帧 trace / HUD 使用的统一名字）。
GRAPHICS_BACKENDS = (
    "opengl",
    "metal",
    "d3d11",
    "cpu",
)

#: GPU 时间不可用时的显式标记（代替 0.0 冒充）。
GPU_TIME_UNAVAILABLE = "gpu_time_unavailable"

# ---------------------------------------------------------------------------
# A0.1 帧预算遥测契约词表（schema v3）
# ---------------------------------------------------------------------------

#: pipeline 段计时的有效性状态。''（缺省）等价 unset。
#: - measured：本段有实测值（0.0 也是有效实测，不代表"极快"以外的语义）。
#: - not_applicable：该帧路径无需本段操作（如零拷贝导入帧的 import_upload）。
#: - unavailable：该段在本帧上不可测量（驱动/上下文/平台不支持）。
#: - unset：尚未回填（迟到证据未到或该帧被丢弃）。
STAGE_VALIDITY_STATES = (
    "unset",
    "measured",
    "not_applicable",
    "unavailable",
)

#: 呈现状态机（一帧可顺序经历多个状态，最终状态以最后一次写入为准）：
#: - submitted：渲染线程 composite 完成，present descriptor 已发出（fence 就绪）。
#: - viewer_copy：Viewer 已把 present slot blit 进自有纹理（blit 命令已入队，
#:   即 ack 时点——不代表可观测显示，也不代表物理扫描完成）。
#: - displayed：Viewer paint 完成可观测呈现（paint 事件内 _render_texture 之后；
#:   无平台 vsync 反馈，不得解读为物理屏幕扫描时间）。
#: - superseded：pending 帧在 paint 前被更新的帧顶掉（未显示即回收）。
#: - dropped：因 stale/切代/背压等在显示前被显式丢弃。
#: - slot_released：present slot 经 ack 回收（资源协议终点）。
PRESENT_STATUSES = (
    "submitted",
    "viewer_copy",
    "displayed",
    "superseded",
    "dropped",
    "slot_released",
)

#: GPU→CPU 回读分类（普通预览热路径不应出现任何回读；出现即按此分类）。
READBACK_KINDS = (
    "seek_snapshot",        # 暂停态 seek/scrub settle 的 1/4 分辨率快照
    "background_snapshot",  # idle 后台预缓冲快照
    "explicit_snapshot",    # 显式请求的快照/导出
    "monitor",              # 监看输出（PBO/直读回读链）
    "cpu_fallback",         # CPU 合成回退路径的整帧结果
)

# ---------------------------------------------------------------------------
# P1-06 分段 deadline 预算
# ---------------------------------------------------------------------------

#: 单帧总 budget 内各 pipeline 段的预算比例（P1-06）。
#: 段顺序即归因优先级：decode 先越界先记 decode，避免双重计数。
#: 比例合计必须为 1.0（测试锁定）。
SEGMENT_BUDGET_RATIOS = (
    ("decode", 0.40),
    ("import_upload", 0.15),
    ("effects", 0.20),
    ("transition", 0.05),
    ("composite", 0.15),
    ("present", 0.05),
)


def segment_budgets_ms(deadline_ns: int) -> dict:
    """按 deadline 计算各段预算（毫秒）。

    deadline_ns <= 0（无 deadline）时返回空 dict——调用方不得凭空
    套用默认预算：没有 deadline 就没有越界语义。
    """
    if deadline_ns <= 0:
        return {}
    total_ms = deadline_ns / 1e6
    return {
        name: round(total_ms * ratio, 3)
        for name, ratio in SEGMENT_BUDGET_RATIOS
    }

# ---------------------------------------------------------------------------
# trace id 分配器
# ---------------------------------------------------------------------------

_trace_id_lock = threading.Lock()
_trace_id_counter = itertools.count(1)


def next_frame_trace_id() -> int:
    """分配一个进程内唯一的单调 trace id（线程安全）。

    0 保留给"非 engine 派发"的帧，所以从 1 开始。
    """
    with _trace_id_lock:
        return next(_trace_id_counter)


# ---------------------------------------------------------------------------
# 帧身份信封
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameTraceContext:
    """一帧在 pipeline 各段之间传递的身份信封（P0-02 的可关联字段）。

    engine 在 request_present 入口构造一次，随 delegate → worker →
    GL present 逐段携带；所有段的事件都用同一个 trace_id 关联。
    """

    trace_id: int
    timeline_frame: int
    generation_id: int
    deadline_ns: int = 0
    mode: str = ""
    priority: str = ""
    backend: str = ""

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "timeline_frame": self.timeline_frame,
            "generation_id": self.generation_id,
            "deadline_ns": self.deadline_ns,
            "mode": self.mode,
            "priority": self.priority,
            "backend": self.backend,
        }
