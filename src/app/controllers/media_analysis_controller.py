"""MediaAnalysisController — 媒体池分析编排（M3 / §17.2 / §17.3）。

把 M2 的 ``AnalysisRequest`` 三步流（propose → confirm → execute）+ ``AnalysisService``
接进 app 生命周期。这是 M3 的**接入层**：让 M2 地基"通电"。

设计参考 ``scene_cut_controller.py`` 的 QObject + 信号模式，但边界更严（计划 §18.4）：

- **不解析模型结果**（provider 输出由 AnalysisService 归一）。
- **不写 timeline**（分析结果只能提出剪辑候选，绝不直接改工程）。
- **不直接操作媒体池 widget**（只发信号；UI 据 ``analysis_status_changed`` 刷 badge）。
- **工程切换时 unbind**（``on_project_changed(None)`` → cancel_all + 清状态，无 orphan worker）。

三步流落到 controller（§17.3 红线：未经用户确认不得分析整批素材）：

1. ``request_analysis_draft`` = propose（UI/Assistant 创建未确认 request）。
2. ``confirm_and_execute`` = confirm + execute（用户确认卡点确认后）。
3. execute 经 ``AnalysisJobScheduler`` 按优先级/并发出队（播放优先于分析）。

per-asset 分析状态（供 UI badge）：unknown / pending / running / ready / stale / failed。
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, Callable, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from src.features.edit.ai_editing.analysis_request import (
    AnalysisDataPolicy,
    AnalysisRecipe,
    AnalysisRequest,
)
from src.features.edit.ai_editing.analysis_scheduler import (
    AnalysisJobScheduler,
)
from src.features.edit.ai_editing.jobs import JobHandle

if TYPE_CHECKING:
    from src.features.edit.ai_editing.analysis_models import AssetFingerprint

logger = logging.getLogger(__name__)


# per-asset 分析状态字符串（UI badge 用；与 AnalysisService.get_status 对齐 + scheduler 补充）
STATUS_UNKNOWN = "unknown"
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_READY = "ready"
STATUS_STALE = "stale"
STATUS_FAILED = "failed"


class MediaAnalysisController(QObject):
    """媒体池分析编排（M3 / §17.2）。

    Signals:
        analysis_proposed: 发出一个待确认的 AnalysisRequest（UI 据此弹确认卡：
            范围/recipe/费用/上传提示）。
        analysis_status_changed: 某 asset 的分析状态变化（asset_id, status）。
            UI 据此刷新 badge（未分析/分析中/完成/过期/失败）。
        analysis_completed: 一个 request 全部完成（request_id, result summary dict）。
        analysis_failed: 一个 request 失败（request_id, error_message）。
    """

    analysis_proposed = pyqtSignal(object)            # AnalysisRequest（待确认）
    analysis_status_changed = pyqtSignal(str, str)    # asset_id, status
    analysis_completed = pyqtSignal(str, object)      # request_id, summary dict
    analysis_failed = pyqtSignal(str, str)            # request_id, error_message

    def __init__(
        self,
        analysis_service: Any,
        scheduler: AnalysisJobScheduler,
        project_getter: Optional[Callable[[], Any]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        """
        Args:
            analysis_service: ``AnalysisService`` 实例（M2 三步流 + provider 编排）。
            scheduler: ``AnalysisJobScheduler`` 实例（优先级/并发/取消）。
            project_getter: 返回当前 EditProject 的 callable（取 project_id/revision）。
                None 时 project_id 必须由调用方在 request 中显式提供。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self._service = analysis_service
        self._scheduler = scheduler
        self._project_getter = project_getter
        # request_id → 该 request 涉及的 asset_ids（状态聚合用）
        self._request_assets: dict[str, tuple[str, ...]] = {}
        # asset_id → 本 controller 观察到的最近一次生命周期状态
        # （pending/running/failed 等瞬态；AUD-2920：running/failed 只在
        # 这里可见，service.get_status 不跟踪它们）
        self._asset_status: dict[str, str] = {}
        self._current_project_id: str | None = None

    # ---------------- 三步流：propose ---------------- #

    def request_analysis_draft(
        self,
        asset_ids: tuple[str, ...] | list[str],
        *,
        recipe: AnalysisRecipe = AnalysisRecipe.FAST_LOCAL,
        data_policy: AnalysisDataPolicy = AnalysisDataPolicy.LOCAL_ONLY,
        requested_by: str = "media_pool",
        project_id: str | None = None,
        title: str = "",
    ) -> AnalysisRequest:
        """创建一份**未确认**的分析提议（§17.3：Assistant/Skill 只能 propose）。

        发出 ``analysis_proposed`` 信号；UI 据此展示确认卡（范围/recipe/费用/上传提示）。
        用户确认后调 ``confirm_and_execute``。

        ``project_id`` 为 None 时从 project_getter 取。
        """
        pid = project_id or self._resolve_project_id() or ""
        if not pid:
            raise ValueError(
                "project_id is required (no project_getter or explicit project_id)"
            )
        request = AnalysisRequest.create(
            project_id=pid,
            asset_ids=tuple(asset_ids),
            recipe_id=recipe,
            data_policy=data_policy,
            requested_by=requested_by,
            project_revision=self._resolve_project_revision(),
            title=title,
        )
        # 登记到 service（propose，未确认）
        self._service.propose_analysis(request)
        self._request_assets[request.request_id] = tuple(asset_ids)
        # 标记这些 asset 为 pending（待确认；UI 可显示"等待确认"）
        for aid in asset_ids:
            self._emit_status(aid, STATUS_PENDING)
        self.analysis_proposed.emit(request)
        return request

    # ---------------- 三步流：confirm + execute ---------------- #

    def confirm_and_execute(
        self, request_id: str, *, confirmed_at: float | None = None,
    ) -> tuple[JobHandle | None, str]:
        """用户确认一份提议 → confirm + execute（经 scheduler 异步）。

        未确认的 request 调本方法会因 service.execute_analysis 的红线被拒
        （§17.3：user_confirmed_at 为空时 scheduler 必须拒绝执行）。
        """
        req = self._service.get_request(request_id)
        if req is None:
            return None, f"unknown AnalysisRequest {request_id!r}"
        if not req.is_confirmed:
            # confirm 这一步
            try:
                self._service.confirm_analysis(request_id, confirmed_at=confirmed_at)
            except (KeyError, ValueError) as exc:
                return None, f"confirm failed: {exc}"

        # 标记 asset 为 running
        assets = self._request_assets.get(request_id, ())
        for aid in assets:
            self._emit_status(aid, STATUS_RUNNING)

        # 经 scheduler 异步执行（service.execute_analysis 接 scheduler 时走异步路径）
        job, err = self._service.execute_analysis(
            request_id, scheduler=self._scheduler,
            on_asset_done=self._on_asset_done,
            on_request_done=(
                self._on_request_done
                if getattr(self._scheduler, "asynchronous", False)
                else None
            ),
        )
        if err:
            for aid in assets:
                self._emit_status(aid, STATUS_FAILED)
            self.analysis_failed.emit(request_id, err)
            return None, err

        # job 完成（同步路径下已完成）→ 聚合状态
        self._finalize_request(request_id, job)
        return job, ""

    # ---------------- 状态查询（UI badge 用）---------------- #

    def get_asset_status(self, asset_id: str) -> str:
        """取某 asset 的分析状态（unknown/pending/running/ready/stale/failed）。

        查询顺序（AUD-2920：此前 scheduler/瞬态从不可达、stale 恒不可达）：

        1. 本 controller 观察到的瞬态（pending 待确认 / running 执行中 /
           failed 最近一次失败）优先——这些状态只在请求生命周期信号里
           出现，``AnalysisService.get_status`` 不跟踪它们；
        2. 其余交给 ``AnalysisService.get_status``（ready/unknown/pending）；
        3. service 报 ready 时再做指纹校验（``service.is_valid``）：
           素材文件已变更（size/mtime 不匹配）则降级为 stale。
        """
        cached = self._asset_status.get(asset_id)
        if cached in (STATUS_PENDING, STATUS_RUNNING, STATUS_FAILED):
            return cached

        svc_status = self._service.get_status(asset_id)
        if svc_status == "ready":
            if self._is_asset_stale(asset_id):
                return STATUS_STALE
            return STATUS_READY
        if svc_status == "pending":
            return STATUS_PENDING
        return STATUS_UNKNOWN

    def _is_asset_stale(self, asset_id: str) -> bool:
        """分析记录是否已过期（素材指纹不再匹配）。

        当前指纹从注入的 ``project_getter`` 解析素材文件路径后按
        size+mtime_ns 计算（与 ai_editing_mixin._fingerprint_from_asset
        同口径）；无法解析（无 project_getter / 虚拟素材 / 文件缺失 /
        is_valid 异常）时按"不误报"原则返回 False。
        """
        current_fp = self._current_fingerprint(asset_id)
        if current_fp is None:
            return False
        is_valid = getattr(self._service, "is_valid", None)
        if not callable(is_valid):
            return False
        try:
            return not bool(is_valid(asset_id, current_fp))
        except Exception:
            logger.debug("stale check failed for %s", asset_id, exc_info=True)
            return False

    def _current_fingerprint(self, asset_id: str) -> Optional["AssetFingerprint"]:
        """从 project_getter 解析素材的当前 AssetFingerprint（size+mtime_ns）。"""
        if self._project_getter is None:
            return None
        try:
            proj = self._project_getter()
        except Exception:
            logger.debug("project_getter failed while resolving fingerprint",
                         exc_info=True)
            return None
        if proj is None:
            return None
        assets = getattr(proj, "assets", None)
        asset = assets.get(asset_id) if hasattr(assets, "get") else None
        if asset is None:
            return None
        path = getattr(asset, "path", "") or ""
        if not path:
            return None
        try:
            st = os.stat(path)
        except OSError:
            return None
        from src.features.edit.ai_editing.analysis_models import AssetFingerprint

        return AssetFingerprint(size=st.st_size, mtime_ns=st.st_mtime_ns)

    def cancel_request(self, request_id: str) -> bool:
        """取消尚未执行的 Agent 分析提议。"""
        return bool(self._service.cancel_request(request_id))

    # ---------------- 工程生命周期 ---------------- #

    def on_project_changed(self, project_id: str | None) -> None:
        """工程切换/关闭钩子（§17.7：无 orphan worker）。

        - project_id=None（关闭）：cancel_all 取消所有 pending/running 分析。
        - project_id 切换：unbind 当前 request 状态（旧工程的 request 不带到新工程）。
        """
        self._current_project_id = project_id
        cancelled = self._scheduler.cancel_all()
        if cancelled:
            logger.info(
                "MediaAnalysisController: cancelled %d analysis job(s) on project change",
                cancelled,
            )
        # 清空 request→asset 映射与瞬态状态缓存（旧工程的不保留）
        self._request_assets.clear()
        self._asset_status.clear()

    # ---------------- 内部 ---------------- #

    def _resolve_project_id(self) -> str | None:
        if self._project_getter is None:
            return None
        try:
            proj = self._project_getter()
            return getattr(proj, "project_id", None) if proj is not None else None
        except Exception:
            return None

    def _resolve_project_revision(self) -> int | None:
        if self._project_getter is None:
            return None
        try:
            proj = self._project_getter()
            return getattr(proj, "revision", None) if proj is not None else None
        except Exception:
            return None

    def _emit_status(self, asset_id: str, status: str) -> None:
        """发出 asset 状态变化信号（UI 据 this 刷 badge）。

        同时记入 ``_asset_status`` 缓存，供 ``get_asset_status`` 查询
        pending/running/failed 等瞬态（AUD-2920）。
        """
        self._asset_status[asset_id] = status
        self.analysis_status_changed.emit(asset_id, status)

    def _on_asset_done(self, asset_id: str, success: bool) -> None:
        """单个 asset 分析完成的回调（service.execute_analysis 在 scheduler 出队时调用）。"""
        self._emit_status(asset_id, STATUS_READY if success else STATUS_FAILED)

    def _on_request_done(self, request_id: str, success: bool) -> None:
        """异步 scheduler 完成所有素材后回到 Qt signal 层。"""
        assets = self._request_assets.get(request_id, ())
        if success:
            self.analysis_completed.emit(request_id, {
                "assets": len(assets),
                "request_id": request_id,
                "async": True,
            })
        else:
            self.analysis_failed.emit(request_id, "一个或多个素材分析失败")

    def _finalize_request(self, request_id: str, job: JobHandle | None) -> None:
        """request 执行完毕的状态聚合。"""
        from src.features.edit.ai_editing.jobs import JobStatus

        assets = self._request_assets.get(request_id, ())
        if job is not None and job.status == JobStatus.SUCCEEDED:
            summary = {
                "assets": len(assets),
                "job_id": job.job_id,
                "result": job.result_manifest or {},
            }
            self.analysis_completed.emit(request_id, summary)
        elif job is not None and job.status in (JobStatus.FAILED, JobStatus.CANCELLED):
            err = job.error_message or f"job {job.status.value}"
            for aid in assets:
                self._emit_status(aid, STATUS_FAILED)
            self.analysis_failed.emit(request_id, err)
