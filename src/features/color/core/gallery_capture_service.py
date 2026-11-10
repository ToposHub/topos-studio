"""G06：抓帧会话一致性与取消——请求／阶段复核／取消令牌。

R01 已把抓帧的**文件提交**收敛为多文件事务（staging→备份→提交，失败
逐字节恢复）。本模块补上会话层契约（计划 §5 G06）：

- 一次抓帧 = 一个 ``GalleryCaptureRequest``（G01 合同）＋ 一组发起时刻
  的 ``GalleryCaptureFacts``（clip/asset/timeline/帧/媒体/目标集合）。
- 会话分阶段推进（begin → prepare 取帧 → persist 提交），**每个阶段边界
  复核**取消令牌与上下文事实：换 clip／换源／帧号变化 → ``cancelled``
  （reason=``context_changed``），不把上一帧写进新上下文的 bundle。
- 取消来源集中在 ``GalleryCaptureCoordinator``：用户取消／工程关闭／
  窗口关闭 ``cancel_all()`` 使全部在场令牌失效；迟到阶段据此终止。
- 阶段回调由 app 层提供（当前为同步执行；渲染线程队列/后台写接入时
  复用同一复核点）：``prepare_frame`` 取精确帧，``persist_bundle`` 走
  R01 事务提交。staging 清理属 ``StillCaptureTransaction`` 职责，本层
  不重复清理也不碰既有 bundle。

结果统一 ``GalleryOperationResult``：拒绝／取消／成功都带稳定 reason，
"拒绝无文件"由 persist 回调（R01 事务）保证。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from src.features.color.core.gallery_contracts import (
    GalleryCaptureRequest,
    GalleryOperationResult,
    OP_CANCELLED,
    OP_REJECTED,
    OP_SUCCESS,
)

# 稳定 reason 键
REASON_CONTEXT_CHANGED = "context_changed"
REASON_RENDER_FAILED = "render_failed"
REASON_SAVE_FAILED = "save_failed"
REASON_USER_DECLINED = "user_declined"
REASON_CANCELLED = "cancelled"

# 上下文事实中"变了就必须取消"的字段（album 允许变——写入目的地在请求
# 发起时已锁定，用户切走 album 只影响完成后的刷新，见控制器侧）。
# BR03（2026-11-10）：grade_revision／project_id 升为阻断字段——发起后
# 调色被改动或换了工程，persist 不得把旧帧写进新 grade／新工程上下文。
_BLOCKING_FACT_FIELDS = ("clip_id", "asset_id", "timeline_id", "media_path",
                         "source_frame", "timeline_frame", "grade_revision",
                         "project_id")


@dataclass(frozen=True)
class GalleryCaptureFacts:
    """发起时刻的抓帧上下文事实（各阶段复核同一组）。"""

    album_id: str
    media_path: Optional[str] = None
    clip_id: Optional[str] = None
    asset_id: Optional[str] = None
    timeline_id: Optional[str] = None
    source_frame: Optional[int] = None
    timeline_frame: Optional[int] = None
    grade_revision: int = 0
    project_id: Optional[str] = None

    def diff_blocking(self, other: "GalleryCaptureFacts"
                      ) -> Tuple[str, ...]:
        """返回与 ``other`` 不同的阻断字段名（空元组 = 一致）。"""
        return tuple(field for field in _BLOCKING_FACT_FIELDS
                     if getattr(self, field) != getattr(other, field))


PrepareFrame = Callable[[], object]
PersistBundle = Callable[[object], GalleryOperationResult]
ContextProbe = Callable[[], Optional[GalleryCaptureFacts]]


@dataclass(frozen=True)
class CaptureFrameSnapshot:
    """prepare 阶段取得的同一呈现代际快照（BR03）。

    JPEG 视口图与 float 帧一次取得；persist 消费快照，不二次读取
    engine——prepare 与 persist 之间换 grade／帧不会把旧 JPEG 与新
    EXR／grade 混装进同一个 bundle。
    """

    image: object
    hdr_frame: object = None
    grade_revision: int = 0


class GalleryCaptureSession:
    """单次抓帧会话：阶段间复核取消令牌与上下文事实。"""

    def __init__(self, request: GalleryCaptureRequest,
                 facts: GalleryCaptureFacts,
                 context_probe: ContextProbe) -> None:
        self.request = request
        self.facts = facts
        self._context_probe = context_probe
        self._cancelled_reason: Optional[str] = None

    # ── 取消 ───────────────────────────────────────────────────────────

    def cancel(self, reason: str = REASON_CANCELLED) -> None:
        """外部取消（用户/工程关闭/窗口关闭）。"""
        self._cancelled_reason = reason

    @property
    def cancelled_reason(self) -> Optional[str]:
        return self._cancelled_reason

    def _check_cancelled(self) -> Optional[GalleryOperationResult]:
        if self._cancelled_reason is not None:
            return GalleryOperationResult(
                request_id=self.request.request_id, status=OP_CANCELLED,
                reason=self._cancelled_reason)
        return None

    def _check_context(self) -> Optional[GalleryOperationResult]:
        fresh = self._context_probe()
        if fresh is None:
            # 上下文源消失（工程关闭）：与取消同等处理，不猜事实
            return GalleryOperationResult(
                request_id=self.request.request_id, status=OP_CANCELLED,
                reason=REASON_CONTEXT_CHANGED)
        changed = self.facts.diff_blocking(fresh)
        if changed:
            return GalleryOperationResult(
                request_id=self.request.request_id, status=OP_CANCELLED,
                reason=REASON_CONTEXT_CHANGED,
                warnings=(f"changed: {', '.join(changed)}",))
        return None

    def check_before_write(self) -> Optional[GalleryOperationResult]:
        """写盘前的最后复核（BR03：覆盖对话框返回后调用）。

        取消令牌或上下文已变 → 返回取消结果（调用方不得继续写盘）；
        一致 → None。
        """
        return self._check_cancelled() or self._check_context()

    # ── 执行 ───────────────────────────────────────────────────────────

    def run(self, prepare_frame: PrepareFrame,
            persist_bundle: PersistBundle
            ) -> GalleryOperationResult:
        """按阶段推进：begin → prepare → 复核 → persist → 复核。

        回调异常不吞——persist 阶段的文件事务自行回滚；本层把异常留给
        调用方的事务边界（R01 模式）处理，会话本身不产生半提交。
        """
        rejected = self._check_cancelled() or self._check_context()
        if rejected is not None:
            return rejected

        rendered = prepare_frame()
        rejected = self._check_cancelled() or self._check_context()
        if rejected is not None:
            return rejected
        if rendered is None:
            return GalleryOperationResult(
                request_id=self.request.request_id, status=OP_REJECTED,
                reason=REASON_RENDER_FAILED)

        result = persist_bundle(rendered)
        # persist 之后不再复核上下文——bundle 已按发起事实提交，迟到取消
        # 只影响完成后的刷新（控制器侧），不回滚已落盘的事务。
        return result


class GalleryCaptureCoordinator:
    """窗口级抓帧会话登记处：取消／关闭使全部在场令牌失效。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: Dict[str, GalleryCaptureSession] = {}
        self._shutdown = False

    def register(self, session: GalleryCaptureSession) -> None:
        with self._lock:
            if self._shutdown:
                session.cancel("shutdown")
            else:
                self._sessions[session.request.request_id] = session

    def finish(self, session: GalleryCaptureSession) -> None:
        with self._lock:
            self._sessions.pop(session.request.request_id, None)

    def cancel_all(self, reason: str = REASON_CANCELLED) -> int:
        with self._lock:
            return self._cancel_all_locked(reason)

    def _cancel_all_locked(self, reason: str) -> int:
        """已持锁路径用（Lock 不可重入）。"""
        sessions = list(self._sessions.values())
        for session in sessions:
            session.cancel(reason)
        return len(sessions)

    def mark_shutdown(self) -> int:
        with self._lock:
            count = self._cancel_all_locked("shutdown")
            self._shutdown = True
            return count

    @property
    def shutdown(self) -> bool:
        with self._lock:
            return self._shutdown

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._sessions)


def describe_capture_outcome(result: GalleryOperationResult) -> str:
    """给状态栏的简短结论（稳定 reason → 可读短语）。"""
    if result.succeeded:
        names = ", ".join(result.affected_still_ids)
        return f"captured: {names}" if names else "captured"
    if result.status == OP_CANCELLED:
        return f"capture cancelled ({result.reason})"
    return f"capture failed ({result.reason})"
