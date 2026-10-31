"""host_protocol —— 工具包与宿主之间的三个只读协议（T01 定稿，T02/G1 落地）。

G1 集成时宿主（EditModule 边界的 mixin/commands_facade 薄方法）实现
:class:`InsightsHostProtocol`；工具包一律面向该 Protocol 编程，不直接触
碰 MainWindow 属性。三个协议对应计划 §2.3：

1. ``capture_readonly_state`` —— 返回同一时点的 project + 完整序列
   （``InsightSnapshot``）。捕获短小（白名单深复制），分析/生成图片一律
   在工具自己的 worker 里做；宿主侧重试有上限。
2. ``navigate_to`` —— UI 线程调用；携带捕获身份 + 目标帧；工程或目标
   变化明确拒绝（结构化 reason），返回实际定位结果。
3. ``request_thumbnail`` —— 复用既有异步缩略图服务；结果标明质量
   （源缩略图/占位/不可用）与源指纹；不把裸 GL 句柄交给工具 worker。

实现方注意：所有 request/result 均为深冻结 dataclass；宿主可以在别的
线程产出 result，但 NavigateResult 的消费必须在 UI 线程完成回调。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from src.features.edit.insights.dto import InsightSnapshot

# ── 1. 只读捕获 ──────────────────────────────────────────────────

# CaptureResult.error_code 取值
CAPTURE_OK = "ok"
CAPTURE_NO_PROJECT = "no_project"            # 无工程：UI 提示正常加载，不造空状态
CAPTURE_INCONSISTENT = "inconsistent_state"  # ref/sequence 两层 key 不一致
CAPTURE_FAILED = "capture_failed"            # 捕获异常（含原因 message）


@dataclass(frozen=True)
class CaptureResult:
    ok: bool
    snapshot: InsightSnapshot | None = None
    error_code: str = CAPTURE_OK
    message: str = ""


# ── 2. 导航 ──────────────────────────────────────────────────────

# NavigateResult.status / reason 取值
NAVIGATED = "navigated"
NAV_REJECTED = "rejected"

REASON_NO_PROJECT = "no_project"            # 无工程
REASON_CAPTURE_STALE = "capture_stale"      # capture_id 已不被宿主承认
REASON_PROJECT_CHANGED = "project_changed"  # project revision 超出接受漂移
REASON_SEQUENCE_CHANGED = "sequence_changed"
REASON_TARGET_MISSING = "target_missing"    # 目标序列/实体不存在
REASON_NOT_SUPPORTED = "not_supported"


@dataclass(frozen=True)
class NavigateRequest:
    capture_id: str
    project_id: str
    sequence_id: str
    target_frame: int
    entity_id: str | None = None   # clip/cue/marker id；None = 只定位序列
    track_id: str | None = None
    expected_sequence_revision: int | None = None  # None = 不做 revision 检查


@dataclass(frozen=True)
class NavigateResult:
    ok: bool
    status: str                      # NAVIGATED / NAV_REJECTED
    reason: str = ""                 # 成功时 ""；拒绝时 REASON_*
    actual_sequence_id: str | None = None
    actual_frame: int | None = None  # 实际定位帧（宿主可夹紧到序列范围）
    message: str = ""


# ── 3. 缩略图 ────────────────────────────────────────────────────

# ThumbnailResult.quality 取值
THUMB_SOURCE = "source_thumbnail"   # 源缩略图（无调色/合成/字幕，非最终画面）
THUMB_PLACEHOLDER = "placeholder"   # 占位（离线/不支持映射/缺帧）
THUMB_UNAVAILABLE = "unavailable"   # 服务不可用/取消


@dataclass(frozen=True)
class ThumbnailRequest:
    capture_id: str
    asset_id: str
    source_frame: int | None = None   # None = 素材默认代表帧
    max_width: int = 320
    max_height: int = 180
    source_fingerprint: str | None = None  # 源指纹（防迟到串帧）


@dataclass(frozen=True)
class ThumbnailResult:
    ok: bool
    quality: str                     # THUMB_*
    media_type: str | None = None    # "png" 等；占位/不可用为 None
    data: bytes | None = None        # 编码字节；UI 线程才转 QPixmap
    fingerprint: str | None = None
    message: str = ""


# ── 协议汇总 ─────────────────────────────────────────────────────


@runtime_checkable
class InsightsHostProtocol(Protocol):
    """G1 落地时宿主需实现的全部方法（全部只读/导航，无工程写回）。"""

    def capture_readonly_state(self) -> CaptureResult:
        """同一时点捕获 project + 完整 sequence map → InsightSnapshot。"""
        ...

    def navigate_to(self, request: NavigateRequest) -> NavigateResult:
        """UI 线程调用；切换序列/移动播放头走宿主既有路径。"""
        ...

    def request_thumbnail(self, request: ThumbnailRequest) -> ThumbnailResult:
        """请求源缩略图；宿主可异步回填同一 result 形态（T20 有界队列）。"""
        ...
