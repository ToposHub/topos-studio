"""AI Edit Host Bridge（M1.1）。

定义 ``AiEditController`` / ``AssistantService`` 与宿主（MainWindow / 既有
EditController）之间的**类型化窄接口**，取代旧实现里的 ``host: Any`` +
``hasattr``/``getattr`` 鸭子类型回退。

背景（审查 M1.1）：

- 旧 ``AiEditController`` 把 host 存为 ``self._host: Any``，再用
  ``hasattr(self._host, "build_plan_for_variant")`` 探测能力；生产宿主从未实现
  该方法，导致 apply 路径静默 no-op。
- ``AssistantService`` 通过 ``session._host.get_state()`` 直接穿透 ``EditSession``
  的私有字段（``# type: ignore[attr-defined]``）。

本模块的协议要求宿主**显式实现**以下方法，编译期即可发现缺失。生产宿主
（MainWindow 或独立 adapter）实现 ``AiEditHostBridge``；测试用 fake 实现它。
不在协议里的方法一律不再被 controller / service 调用。
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable

from src.features.edit.ai_editing.approval_service import ApprovalGrant, ApprovalService
from src.features.edit.ai_editing.models import (
    CapabilityManifest,
    EditPlan,
)
from src.features.edit.ai_editing.semantic_index import SemanticIndex
from src.features.edit.ai_editing.transaction_service import PreviewResult
from src.features.edit.ai_editing.execution_policy import (
    ExecutionAction,
    PolicyDecision,
)


@runtime_checkable
class AiEditHostBridge(Protocol):
    """宿主与 AI 编辑子系统之间的类型化桥接（M1.1）。

    所有方法由宿主（MainWindow composition root）实现。controller / service
    只通过本协议访问宿主能力，不再 ``hasattr`` 探测或穿透私有字段。

    语义分组：

    - **只读世界状态**：``get_project_sequences`` / ``get_capability_manifest`` /
      ``get_semantic_index``。
    - **授权签发**（M0.1）：``request_plan_approval`` 返回宿主签发的
      ``ApprovalGrant``（或 None=用户拒绝）；``approval_service`` 返回宿主
      持有的 ``ApprovalService``（用于 consume 校验）。
    - **plan 检索**（M2.1）：``get_plan_by_id`` 从 PlanRepository 取回被批准的
      那一份不可变 plan。
    - **提交反馈**：``commit_ai_command`` 把已编译的 MacroCommand 推入真实
      CommandStack（M2.2，单 undo + 与手动编辑一致的刷新链）；``refresh_after_ai_commit``
      刷新 timeline/renderer/media pool。``get_active_timeline_id`` 给出 AI 默认作用目标。
    """

    # ---- 只读世界状态 ---- #

    def get_project_sequences(self) -> Any:
        """返回当前两层状态（``ProjectSequences``）。"""
        ...

    def get_capability_manifest(self) -> CapabilityManifest:
        """返回当前能力表（从真实 registry + feature flags 生成）。"""
        ...

    def get_semantic_index(self) -> SemanticIndex:
        """返回当前工程的语义索引（供 planner 选段）。"""
        ...

    # ---- 授权签发（M0.1）---- #

    def request_plan_approval(
        self, plan: EditPlan, preview: PreviewResult
    ) -> "ApprovalGrant | None":
        """请求用户批准 plan。

        宿主弹确认对话框展示 ``preview.diff``；用户确认后通过其
        ``ApprovalService`` 签发绑定 (project, timeline, base_revision,
        plan_hash) 的 ``ApprovalGrant``。用户拒绝返回 None。
        """
        ...

    def issue_automatic_plan_approval(
        self, plan: EditPlan, preview: PreviewResult, *,
        asset_ids: tuple[str, ...] = (),
    ) -> "ApprovalGrant | None":
        """在用户显式开启受限自动模式后签发一次性自动授权。

        该入口只允许受信任宿主实现；模型、MCP 客户端和普通工具调用不能调用
        ``ApprovalService.issue_after_user_confirmation`` 绕过此策略。
        """
        ...

    def approval_service(self) -> "ApprovalService | None":
        """宿主持有的 ``ApprovalService``（consume 校验用）。可 None。"""
        ...

    def is_autonomous_mode_enabled(self) -> bool:
        """Return the live user opt-in for restricted autonomous editing."""
        ...

    def detect_autonomous_replay(self, plan: EditPlan, state: Any) -> Any:
        """Detect an exact already-committed plan after restart/crash recovery."""
        ...

    # ---- plan 检索（M2.1）---- #

    def get_plan_by_id(self, plan_id: str) -> "EditPlan | None":
        """从 PlanRepository 取回不可变 plan（被用户看到/批准的那一份）。

        返回 None 表示 plan 已不在仓库（如工程重开）；apply 路径应中止并提示
        重新规划。
        """
        ...

    # ---- 提交（M2.2：进入真实 CommandStack，保证 Ctrl+Z 一致）---- #

    def commit_ai_command(
        self, timeline_id: str, macro: Any, *, undo_label: str
    ) -> Any:
        """把已编译的 ``MacroCommand`` 推入该 timeline 的真实 ``CommandStack``。

        宿主实现应解析对应 timeline 的 ``TimelineView`` 并调其 ``execute_command(macro)``。
        这会：

        - 在 stack 里记录**单个** undo 条目（整个 AI 编辑 = 一次 undo）；
        - emit ``timeline_changed``，触发与手动编辑**完全相同**的刷新链
          （widget / program viewer / renderer / media-pool ref / mixer /
          captions / ``_current_timeline_sequences``）。

        乐观锁与授权校验由调用方（``AiEditController``）在调本方法**之前**完成
        （``TransactionService.preview`` + ``ApprovalService.consume``）；本方法只
        负责"推栈 + 刷新"。

        Args:
            timeline_id: 目标 timeline。必须与当前活动 timeline 一致（AI 默认作用于
                活动 timeline；非活动 timeline 由宿主先切换或返回 None）。
            macro: 已编译的 ``MacroCommand``（来自 ``compile_plan``）。
            undo_label: undo 显示名（来自 ``compiled.undo_label``）。

        Returns:
            新的 ``TimelineSequence``；失败返回 None（调用方据此记 FAILED）。
        """
        ...

    def rollback_ai_command(
        self,
        timeline_id: str,
        *,
        expected_undo_label: str,
        expected_revision: int | None,
    ) -> Any:
        """Undo the latest command only after validating its label and revision.

        Returns the restored timeline, or ``None`` when the host cannot prove
        that the latest command is the AI command being rolled back.
        """
        ...

    def get_active_timeline_id(self) -> str | None:
        """当前活动 timeline 的 id（AI 默认作用于活动 timeline）。无工程返回 None。"""
        ...

    def get_selected_asset_ids(self) -> tuple[str, ...]:
        """媒体池当前选中素材 ID（P1：供 InvocationScope.selected_asset_ids）。

        无选中 / 未装配返回空 tuple（editorial skill 进 WAITING_INPUT）。
        """
        ...

    def get_selected_audio_clip(self) -> tuple[str, str, str] | None:
        """返回当前选中的音频片段 ``(clip_id, track_id, asset_id)``。"""
        ...

    def start_ai_transcription(
        self,
        asset_id: str,
        options: dict[str, Any],
        *,
        on_progress: Callable[[float | None, str], Any],
        on_completed: Callable[[Any], Any],
        on_failed: Callable[[str], Any],
    ) -> tuple[bool, str]:
        """在宿主后台启动 ASR，不阻塞 controller/UI 线程。"""
        ...

    def commit_ai_transcript_subtitles(
        self, clip_id: str, track_id: str, asset_id: str, analysis: Any,
    ) -> tuple[bool, str]:
        """把 ASR 结果以一个可撤销事务写入字幕轨道。"""
        ...

    def show_ai_transcription_progress(
        self, fraction: float | None, message: str,
    ) -> None:
        """在宿主主线程显示 AI 转录进度。"""
        ...

    def show_ai_transcription_message(
        self, message: str, *, warning: bool = False,
    ) -> None:
        """在宿主主线程显示 AI 转录结果/错误。"""
        ...

    def get_project_id(self) -> str:
        """当前工程 ID（P1：InvocationScope.project_id 必填）。无工程返回空串。"""
        ...

    def check_ai_action(
        self,
        action: ExecutionAction | str,
        *,
        timeline_id: str | None = None,
        base_revision: int | None = None,
        asset_ids: tuple[str, ...] = (),
        template_id: str | None = None,
        host: str | None = None,
        file_extension: str | None = None,
        cost_units: float = 0.0,
        duration_seconds: float = 0.0,
        approval_granted: bool = False,
    ) -> PolicyDecision:
        """在宿主动作真正发生前执行当前工程的策略门。"""
        ...

    def request_ai_export(
        self, scope: Any, inputs: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        """执行 AI 导出预检、路径选择和用户确认后启动后台导出。"""
        ...

    def request_ai_generation_consent(
        self, scope: Any, inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """展示单次生成确认，并返回已确认的 consent 字段；取消返回 None。"""
        ...

    def import_ai_generated_asset(self, asset_id: str) -> tuple[bool, str]:
        """把生成素材箱中的候选显式导入 Media Pool。"""
        ...

    def start_ai_generated_materialization(
        self,
        asset_id: str,
        *,
        on_progress: Callable[[str, float, str], Any],
        on_completed: Callable[[str, str, str], Any],
        on_failed: Callable[[str, str], Any],
    ) -> tuple[bool, str, str | None]:
        """后台物化生成媒体句柄，并返回可取消的 Job ID。"""
        ...

    def import_ai_generated_asset_from_path(
        self, asset_id: str, path: str,
    ) -> tuple[bool, str]:
        """在宿主主线程把已物化文件导入 Media Pool。"""
        ...

    def get_ai_generated_media_asset_id(self, asset_id: str) -> str | None:
        """返回已导入候选对应的 MediaAsset ID；未导入返回 None。"""
        ...

    def preview_ai_generated_asset(self, asset_id: str) -> tuple[bool, str]:
        """标记候选已预览，并打开 provider 返回的预览句柄。"""
        ...

    def get_ai_playhead_frame(self) -> int | None:
        """返回活动时间线播放头帧；不可用返回 None。"""
        ...

    def get_current_gl_viewer(self) -> Any:
        """返回当前可见页面的 QOpenGLWidget viewer（用于抓帧）；不可用返回 None。

        必须在 GUI 线程调用（grabFramebuffer 要求）。返回的对象应有
        ``grabFramebuffer()`` 方法。
        """
        ...

    def refresh_after_ai_commit(self, result: Any) -> None:
        """AI 提交成功后的额外刷新（``execute_command`` 已触发主刷新链，多数宿主
        在此为空实现，仅为满足协议）。"""
        ...


class NullAiEditHostBridge:
    """无操作实现，供无工程/只读/未装配场景返回确定状态。

    避免上层用 broad except-pass 掩盖"宿主未装配"——调用方拿到 Null 桥后
    会得到明确的空结果，便于 UI 显示确定状态（M1.2 要求）。
    """

    def get_project_sequences(self) -> Any:
        return None

    def get_capability_manifest(self) -> CapabilityManifest:
        from src.features.edit.ai_editing.capability_manifest import build_manifest

        return build_manifest()

    def get_semantic_index(self) -> SemanticIndex:
        return SemanticIndex()

    def request_plan_approval(
        self, plan: EditPlan, preview: PreviewResult
    ) -> "ApprovalGrant | None":
        return None  # 未装配 → 总是"拒绝"

    def issue_automatic_plan_approval(
        self, plan: EditPlan, preview: PreviewResult, *,
        asset_ids: tuple[str, ...] = (),
    ) -> "ApprovalGrant | None":
        return None  # 未装配 → 自动模式也不能写入

    def approval_service(self) -> "ApprovalService | None":
        return None

    def is_autonomous_mode_enabled(self) -> bool:
        return False

    def detect_autonomous_replay(self, plan: EditPlan, state: Any) -> dict[str, Any]:
        # AUD-2100：协议成员补齐——Null 桥必须满足自己宣称实现的协议
        # （@runtime_checkable 的 isinstance 检查 / 静态检查 / 后续新增调用点）。
        # 未装配宿主不可能有已提交计划，保守返回 known=False（与生产实现的
        # "无法证明" 分支语义一致，让修订门 fail-closed）。
        return {"known": False}

    def get_plan_by_id(self, plan_id: str) -> "EditPlan | None":
        return None

    def commit_ai_command(
        self, timeline_id: str, macro: Any, *, undo_label: str
    ) -> Any:
        return None  # 未装配 → 提交无效

    def rollback_ai_command(
        self,
        timeline_id: str,
        *,
        expected_undo_label: str,
        expected_revision: int | None,
    ) -> Any:
        # AUD-2100：协议成员补齐——未装配宿主无法证明 undo 目标是 AI 命令，
        # 返回 None（调用方 autonomous_agent.rollback 据此保守中止）。
        return None

    def get_active_timeline_id(self) -> str | None:
        return None

    def get_selected_asset_ids(self) -> tuple[str, ...]:
        return ()

    def get_selected_audio_clip(self) -> tuple[str, str, str] | None:
        return None

    def start_ai_transcription(
        self,
        asset_id: str,
        options: dict[str, Any],
        *,
        on_progress: Callable[[float | None, str], Any],
        on_completed: Callable[[Any], Any],
        on_failed: Callable[[str], Any],
    ) -> tuple[bool, str]:
        return False, "AI 语音转字幕宿主尚未初始化"

    def commit_ai_transcript_subtitles(
        self, clip_id: str, track_id: str, asset_id: str, analysis: Any,
    ) -> tuple[bool, str]:
        return False, "AI 字幕写入宿主尚未初始化"

    def show_ai_transcription_progress(
        self, fraction: float | None, message: str,
    ) -> None:
        return None

    def show_ai_transcription_message(
        self, message: str, *, warning: bool = False,
    ) -> None:
        return None

    def get_project_id(self) -> str:
        return ""

    def check_ai_action(
        self,
        action: ExecutionAction | str,
        *,
        timeline_id: str | None = None,
        base_revision: int | None = None,
        asset_ids: tuple[str, ...] = (),
        template_id: str | None = None,
        host: str | None = None,
        file_extension: str | None = None,
        cost_units: float = 0.0,
        duration_seconds: float = 0.0,
        approval_granted: bool = False,
    ) -> PolicyDecision:
        return PolicyDecision(
            False,
            reason="AI host bridge is not initialized",
            action=str(action),
        )

    def request_ai_export(
        self, scope: Any, inputs: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        return False, "AI 导出宿主尚未初始化"

    def request_ai_generation_consent(
        self, scope: Any, inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        return None

    def import_ai_generated_asset(self, asset_id: str) -> tuple[bool, str]:
        return False, "AI 生成宿主尚未初始化"

    def start_ai_generated_materialization(
        self,
        asset_id: str,
        *,
        on_progress: Callable[[str, float, str], Any],
        on_completed: Callable[[str, str, str], Any],
        on_failed: Callable[[str, str], Any],
    ) -> tuple[bool, str, str | None]:
        return False, "AI 生成后台物化宿主尚未初始化", None

    def import_ai_generated_asset_from_path(
        self, asset_id: str, path: str,
    ) -> tuple[bool, str]:
        return False, "AI 生成宿主尚未初始化"

    def get_ai_generated_media_asset_id(self, asset_id: str) -> str | None:
        return None

    def preview_ai_generated_asset(self, asset_id: str) -> tuple[bool, str]:
        return False, "AI 生成预览宿主尚未初始化"

    def get_ai_playhead_frame(self) -> int | None:
        return None

    def get_current_gl_viewer(self) -> Any:
        return None

    def refresh_after_ai_commit(self, result: Any) -> None:
        pass

    # Composite optional surface: the null host is deliberately read-only.
    def get_composite_snapshot(self) -> dict[str, Any] | None:
        return None

    def validate_composite_edit(self, plan: Any) -> Any:
        from src.features.composite.edit_service import CompositeDiagnostic, CompositeValidationReport
        return CompositeValidationReport((CompositeDiagnostic("error", "Composite host is not available"),))

    def apply_composite_edit(self, plan: Any, *, approval: Any = None, client: str = "ai") -> Any:
        from src.features.composite.edit_service import CompositeApplyResult, CompositeDiagnostic, CompositeValidationReport
        report = CompositeValidationReport((CompositeDiagnostic("error", "Composite host is not available"),))
        return CompositeApplyResult(False, plan.plan_id, plan.content_hash(), 0, 0, report=report, error="Composite host is not available")
