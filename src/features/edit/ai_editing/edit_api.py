"""ToposEditAPI / EditSession — 受信任脚本的 Python facade（Phase 3）。

给本地、已签名或用户确认的 Python 脚本的高层 API。它是 TAEP operation 的
Python facade，**不准绕开 command compiler**（TAEP §8）。

设计（TAEP §8 脚本规则）：

- 所有编辑仍经 ``validate → compile → MacroCommand → commit``；
- 每次脚本执行默认生成一个 undo group；
- 提供 ``dry_run``，不提供任意对象内部引用；
- 文件路径仅能由用户选取或 capability token 授权；输出不允许静默覆盖；
- 长任务复用 export queue/job API（P4），并可取消；
- 禁止 UI 线程内 GPU 操作，脚本只提交任务/等待状态；
- 对 AI 生成但由用户运行的脚本，在编辑器中标记为“未受信任”（UI 层职责）。

典型用法（TAEP §8）::

    from topos.edit import EditSession

    session = EditSession.current()
    plan = session.load_plan("/approved/rough_cut.taep.json")
    report = session.validate(plan)
    if report.has_errors:
        raise RuntimeError(report.format())
    session.apply(plan, approval="local-user")
    session.export_from_preset("social-vertical")

本模块是纯逻辑层；与 controller 的接线（拿 ``ProjectSequences``、刷新 UI）由
``EditSession.current()`` 的宿主（app/controller）提供，本模块不导入 PyQt。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from src.features.edit.ai_editing.approval_service import (
    ApprovalGrant,
    ApprovalService,
)
from src.features.edit.ai_editing.diagnostics import (
    DiagnosticSeverity,
    TaepDiagnosticCode,
    make_diagnostic,
)
from src.features.edit.ai_editing.edit_project_service import (
    CommitResult,
    ProjectSequences,
)
from src.features.edit.ai_editing.jobs import JobRegistry
from src.features.edit.ai_editing.models import (
    CapabilityManifest,
    DetailLevel,
    EditPlan,
    ProjectSnapshot,
    ValidationReport,
)
from src.features.edit.ai_editing.snapshot_service import build_snapshot
from src.features.edit.ai_editing.transaction_service import (
    PreviewResult,
    TimelineDiff,
    TransactionService,
)
from src.features.composite.edit_service import (
    CompositeApplyResult,
    CompositeEditPlan,
    CompositeValidationReport,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
# 宿主协议：EditSession 通过 host 拿到当前两层状态并回写
# ---------------------------------------------------------------------- #


class EditSessionHost(Protocol):
    """EditSession 与宿主（app/controller）之间的窄接口。

    宿主负责：提供当前 ``ProjectSequences``、在 commit 成功后刷新 UI/renderer、
    解析用户授权。本协议让 ``EditSession`` 不直接依赖 PyQt / controller 内部。

    授权契约（M0.1）：``request_user_approval`` 返回的是受信任宿主签发的
    ``ApprovalGrant``（不透明、一次性、绑定作用域），而不是任意字符串 token。
    """

    def get_state(self) -> ProjectSequences: ...

    def get_manifest(self) -> CapabilityManifest: ...

    def on_committed(self, result: CommitResult) -> None:
        """commit 成功后由宿主刷新 timeline view / renderer / media pool。"""
        ...

    def request_user_approval(
        self, plan: EditPlan, diff: TimelineDiff
    ) -> "ApprovalGrant | None":
        """请求用户批准；返回宿主签发的 ``ApprovalGrant`` 或 None（被拒绝）。

        宿主实现应弹出确认对话框，用户确认后通过其持有的 ``ApprovalService``
        签发绑定 (project, timeline, base_revision, plan_hash) 的 grant。
        """
        ...

    def approval_service(self) -> "ApprovalService | None":
        """宿主持有的 ``ApprovalService``（用于 consume 校验）。可 None。"""
        ...

    # Composite is optional for hosts that only expose editorial timelines.
    # The concrete MainWindow bridge implements these methods; keeping them
    # optional at runtime preserves the existing headless/fake hosts.
    def get_composite_snapshot(self) -> dict[str, Any] | None: ...

    def validate_composite_edit(self, plan: CompositeEditPlan) -> CompositeValidationReport: ...

    def apply_composite_edit(
        self, plan: CompositeEditPlan, *, approval: ApprovalGrant | str | None = None,
        client: str = "ai",
    ) -> CompositeApplyResult: ...


# ---------------------------------------------------------------------- #
# 路径授权 token（TAEP §8：输出不允许静默覆盖）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class PathGrant:
    """用户授权的文件路径 token。

    - ``allow_overwrite``：是否允许覆盖已存在文件。由
      ``EditSession.request_export`` 在创建导出 job 前强制执行（AUD-13402）：
      目标已存在且本位为 False（或缺 grant）→ 拒绝并返回 blocking
      preflight（``TAEP_PERMISSION_DENIED``）。
    - ``granted_by``：授权来源（如 "local-user" / "ui-pick"）。
    脚本不得自行构造 PathGrant；只能从 ``EditSession.request_path_grant`` 获得。
    """

    path: str
    allow_overwrite: bool = False
    granted_by: str = "local-user"


# ---------------------------------------------------------------------- #
# EditSession
# ---------------------------------------------------------------------- #


class EditSession:
    """受信任脚本的会话 facade。

    通过 ``EditSessionHost`` 与宿主交互；自身无 Qt / GPU 依赖，可在脚本线程
    调用（长任务经 job API，P4）。
    """

    def __init__(self, host: EditSessionHost, *, jobs: JobRegistry | None = None) -> None:
        self._host = host
        self._txn = TransactionService()
        # Preview/export requests and ToolFacade job queries share this
        # session-scoped registry instead of creating unreachable registries.
        self._jobs = jobs or JobRegistry()

    # ---------------- 静态入口 ---------------- #

    _current: "EditSession | None" = None

    @staticmethod
    def current() -> "EditSession":
        """获取当前会话（由宿主在 app 启动时绑定）。"""
        if EditSession._current is None:
            raise RuntimeError(
                "No active EditSession; the host application must bind one via "
                "EditSession.bind(host)."
            )
        return EditSession._current

    @staticmethod
    def bind(
        host: EditSessionHost, *, jobs: JobRegistry | None = None,
    ) -> "EditSession":
        """宿主绑定当前会话。"""
        session = EditSession(host, jobs=jobs)
        EditSession._current = session
        return session

    @staticmethod
    def unbind() -> None:
        EditSession._current = None

    # ---------------- 只读 ---------------- #

    def get_capabilities(self) -> CapabilityManifest:
        """返回当前能力表（TAEP §5.1）。"""
        return self._host.get_manifest()

    @property
    def jobs(self) -> JobRegistry:
        """Return the registry used by this session's long-running jobs."""
        return self._jobs

    def get_snapshot(
        self, *, detail_level: DetailLevel = DetailLevel.STRUCTURE,
        timeline_id: str | None = None, asset_limit: int | None = None,
    ) -> ProjectSnapshot:
        """返回脱敏只读快照（TAEP §5.2）。"""
        return build_snapshot(
            self._host.get_state(),
            detail_level=detail_level, timeline_id=timeline_id, asset_limit=asset_limit,
        )

    def inspect_subtitle_track(
        self, track_id: str, *, timeline_id: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run deterministic subtitle QC without changing the project."""
        state = self._host.get_state()
        target_id = timeline_id or state.project.active_timeline_id
        sequence = state.sequences.get(target_id) if target_id else None
        if sequence is None:
            raise ValueError("No active timeline")
        track = sequence.get_track_by_id(track_id)
        if track is None or track.track_type != "subtitle":
            raise ValueError(f"Subtitle track not found: {track_id}")
        from src.features.edit.core.captions.quality import inspect_subtitle_track

        opts = dict(options or {})
        report = inspect_subtitle_track(
            track,
            sequence.fps,
            timeline_duration_frames=sequence.duration_frames,
            max_chars_per_line=int(opts.get("max_chars_per_line", 42)),
            max_lines=int(opts.get("max_lines", 2)),
            max_chars_per_second=float(opts.get("max_chars_per_second", 20.0)),
        )
        return report.to_dict()

    # ---------------- Composite 统一编辑 API ---------------- #

    def get_composite_snapshot(self) -> dict[str, Any] | None:
        """返回合成节点、参数定义和关键帧轨道的脱敏快照。"""
        fn = getattr(self._host, "get_composite_snapshot", None)
        return fn() if fn is not None else None

    def validate_composite_edit(
        self, plan: CompositeEditPlan | dict[str, Any]
    ) -> CompositeValidationReport:
        """只校验 Composite 计划，不改变工程。"""
        parsed = plan if isinstance(plan, CompositeEditPlan) else CompositeEditPlan.from_dict(plan)
        fn = getattr(self._host, "validate_composite_edit", None)
        if fn is None:
            raise RuntimeError("Composite editing is not available in the current host")
        return fn(parsed)

    def apply_composite_edit(
        self, plan: CompositeEditPlan | dict[str, Any], *,
        approval: "ApprovalGrant | str | None" = None,
    ) -> CompositeApplyResult:
        """应用 Composite 计划；宿主负责授权、主线程、撤销和 UI 刷新。"""
        parsed = plan if isinstance(plan, CompositeEditPlan) else CompositeEditPlan.from_dict(plan)
        fn = getattr(self._host, "apply_composite_edit", None)
        if fn is None:
            raise RuntimeError("Composite editing is not available in the current host")
        return fn(parsed, approval=approval, client="ai")

    # ---------------- plan 加载 ---------------- #

    def load_plan(self, path: str) -> EditPlan:
        """从 JSON 文件加载 TAEP plan。

        路径必须由用户提供（脚本参数或 UI 选取）；本方法不做任意路径遍历。
        """
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return EditPlan.from_dict(data)

    def load_plan_from_dict(self, data: dict[str, Any]) -> EditPlan:
        return EditPlan.from_dict(data)

    # ---------------- validate / dry-run ---------------- #

    def validate(self, plan: EditPlan) -> ValidationReport:
        """dry-run：只校验，不提交（TAEP §5.6 mode=preview 内核）。"""
        from src.features.edit.ai_editing.plan_validator import validate_plan

        return validate_plan(plan, state=self._host.get_state(), manifest=self._host.get_manifest())

    def preview(self, plan: EditPlan) -> PreviewResult:
        """validate + compile + simulate + diff，不提交。"""
        return self._txn.preview(plan, self._host.get_state(), manifest=self._host.get_manifest())

    # ---------------- apply / commit ---------------- #

    def apply(
        self, plan: EditPlan, *, approval: "ApprovalGrant | str | None" = None
    ) -> CommitResult:
        """提交 plan 为单个 MacroCommand undo group。

        授权契约（M0.1）：``approval`` 应为宿主签发的 ``ApprovalGrant``。
        成功后宿主刷新 UI。授权失败返回 ``applied=False`` 且工程不变。
        """
        approval_svc = self._host_approval_service()
        result = self._txn.commit(
            plan, self._host.get_state(),
            manifest=self._host.get_manifest(),
            approval=approval,
            approval_service=approval_svc,
            client="script",
        )
        if result.applied:
            self._host.on_committed(result)
        return result

    def apply_with_confirmation(self, plan: EditPlan) -> CommitResult:
        """走宿主的用户确认流程后提交（TAEP §11.6 步骤 11）。"""
        preview = self.preview(plan)
        if preview.has_errors:
            return CommitResult(
                applied=False,
                new_project=self._host.get_state().project,
                new_sequences=dict(self._host.get_state().sequences),
                error="plan has validation errors",
                errors=tuple(d.message for d in preview.report.errors),
            )
        if preview.diff is None:
            return CommitResult(
                applied=False,
                new_project=self._host.get_state().project,
                new_sequences=dict(self._host.get_state().sequences),
                error="no diff produced",
            )
        grant = self._host.request_user_approval(plan, preview.diff)
        if grant is None:
            return CommitResult(
                applied=False,
                new_project=self._host.get_state().project,
                new_sequences=dict(self._host.get_state().sequences),
                error="user declined the plan",
            )
        return self.apply(plan, approval=grant)

    def _host_approval_service(self) -> "ApprovalService | None":
        """取宿主的 ApprovalService（协议方法可选）。

        AUD-13401：宿主访问器的真实异常不再被 ``except Exception`` 吞掉后
        伪装成"无授权服务"（那会把宿主缺陷降级为误导性的授权失败）。协议
        方法缺失（宿主不支持授权）仍返回 None；宿主访问器抛出的异常先留
        完整堆栈日志再**原样上抛**——fail loud，不掩盖领域错误。
        """
        fn = getattr(self._host, "approval_service", None)
        if fn is None:
            return None
        try:
            return fn()
        except Exception:  # noqa: BLE001 — 只留痕并上抛，不吞（AUD-13401）
            logger.exception(
                "host approval_service() accessor raised; surfacing the host "
                "defect instead of masking it as 'no approval service'"
            )
            raise

    # ---------------- 路径授权 ---------------- #

    def request_path_grant(
        self, suggested_path: str, *, allow_overwrite: bool = False
    ) -> PathGrant | None:
        """请求用户授权一个输出路径（TAEP §8）。

        默认实现要求宿主实现；若宿主未提供则返回 None（脚本应中止输出）。
        """
        grant_fn = getattr(self._host, "request_path_grant", None)
        if grant_fn is None:
            return None
        return grant_fn(suggested_path, allow_overwrite=allow_overwrite)

    # ---------------- 导出（P4）---------------- #

    def request_export(
        self, request: "Any", *, path_grant: "PathGrant | None" = None
    ) -> "Any":
        """创建导出提议：build config → preflight → job（不立即写文件）。

        需要 path grant 才能写输出；preflight 有 blocking 时不提交 job。

        AUD-13402（TAEP §8"输出不允许静默覆盖"的强制执行点）：目标文件
        已存在时，仅 ``PathGrant.allow_overwrite=True`` 的显式授权允许覆盖；
        无 grant 或 grant 未授权覆盖 → 拒绝创建 job，返回 blocking 的
        ``PreflightReport``（``job=None``、``config=None``），调用方据稳定
        诊断码 ``TAEP_PERMISSION_DENIED`` 提示用户重新授权。检查发生在
        job 创建边界；实际写文件仍由导出 worker 执行。
        """
        from src.features.edit.ai_editing.export_service import ExportService

        # 若 request 没有显式 output_path，但给了 path_grant，则用 grant 的 path。
        if path_grant is not None and not getattr(request, "output_path", None):
            from dataclasses import replace as _replace

            request = _replace(request, output_path=path_grant.path)

        # AUD-13402：覆盖已存在文件必须经显式授权（allow_overwrite 此前
        # 全库无消费者，授权合同形同虚设）。
        overwrite_allowed = path_grant is not None and path_grant.allow_overwrite
        output_path = getattr(request, "output_path", None)
        if output_path and not overwrite_allowed and Path(output_path).exists():
            diag = make_diagnostic(
                DiagnosticSeverity.ERROR,
                TaepDiagnosticCode.PERMISSION_DENIED,
                reason=(
                    f"输出文件已存在且未获覆盖授权：{output_path}（TAEP §8 "
                    "输出不允许静默覆盖；需 request_path_grant("
                    "allow_overwrite=True) 的用户授权）"
                ),
            )
            from src.features.edit.ai_editing.export_service import PreflightReport

            failed = PreflightReport(
                ok=False, blocking=(diag.message,), diagnostics=(diag,)
            )
            return {"job": None, "preflight": failed, "config": None}

        service = ExportService(job_registry=self._jobs)
        job, report, config = service.request_export(request, self._host.get_state())
        return {"job": job, "preflight": report, "config": config}

    def request_preview(
        self, timeline_id: str, *, preset_name: str = "quick_10s",
        clip_range: "Any | None" = None,
    ) -> "Any":
        """创建短范围预览 job（不与最终 export 混用）。"""
        from src.features.edit.ai_editing.export_service import ExportService

        service = ExportService(job_registry=self._jobs)
        job, err = service.request_preview(
            timeline_id, self._host.get_state(),
            preset_name=preset_name, clip_range=clip_range,
        )
        return {"job": job, "error": err}

    def export_from_preset(self, preset: str, *, path_grant: PathGrant | None = None) -> Any:
        """从 deliver preset 创建导出请求（便捷封装）。

        P4：转发到 ``request_export``。preset 解析由 deliver 模块负责；此处构造
        一个默认 ExportRequest。
        """
        state = self._host.get_state()
        tid = state.project.active_timeline_id
        if tid is None:
            raise RuntimeError("no active timeline to export")
        from src.features.edit.ai_editing.export_service import ExportRequest

        request = ExportRequest(
            timeline_id=tid,
            codec="h264",
            output_path=path_grant.path if path_grant else None,
            preset_name=preset,
        )
        return self.request_export(request, path_grant=path_grant)
