"""``MainWindowAiEditHostBridge`` —— ``AiEditHostBridge`` 的生产实现（M1.2 / M2.2）。

把 AI 编辑子系统（``AssistantService`` / ``AiEditController``）与 ``MainWindow``
的真实状态、真实 ``CommandStack``、真实刷新链解耦地接到一起。

设计要点：

- 持有对 ``MainWindow`` 的**弱引用**（避免循环引用 + 便于测试用 fake host 注入）。
- **提交（M2.2）**：``commit_ai_command`` 解析活动 timeline 的 ``TimelineView`` 并
  调 ``execute_command(macro)``。这复用了界面那条已验证的刷新链
  （``timeline_changed`` → ``_on_edit_timeline_changed`` → widget/viewer/renderer/
  media-pool ref/mixer/captions/``_current_timeline_sequences``），并且整个 AI 编辑
  作为**单个** MacroCommand 进入 stack —— 一次 Ctrl+Z 即可整体撤销。
- **授权（M0.1）**：``request_plan_approval`` 弹 ``QMessageBox`` 展示 diff 摘要，
  用户确认后用持有的 ``ApprovalService`` 经 ``_expected_commit_scope`` 签发绑定
  (project/timeline/base_revision/plan_hash) 的 ``ApprovalGrant``。不显示 plan JSON
  / secret。
- 失败不静默：无工程 / 无活动 timeline / view 缺失都返回确定结果（None），由
  ``AiEditController`` 据此把 service 置 FAILED。

本类**不**导入 renderer / GPU；长任务不在主线程跑（本批用确定性 planner，无长任务）。
"""

from __future__ import annotations

import logging
import math
import os
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import QObject, pyqtSignal

from src.features.edit.ai_editing.approval_service import ApprovalGrant, ApprovalService
from src.features.edit.ai_editing.capability_manifest import build_manifest
from src.features.edit.ai_editing.models import CapabilityManifest, EditPlan
from src.features.edit.ai_editing.semantic_index import SemanticIndex, build_index_from_analysis
from src.features.edit.ai_editing.transaction_service import PreviewResult
from src.features.composite.edit_service import (
    CompositeApplyResult,
    CompositeEditPlan,
    CompositeEditService,
    CompositeValidationReport,
)

if TYPE_CHECKING:
    from src.app.main_window import MainWindow
    from src.features.edit.ai_editing.analysis_service import AnalysisService

logger = logging.getLogger(__name__)

#: AUD-2111: keyring 探测结果缓存 TTL（秒）。真实 OS 钥匙串/文件后端的
#: ``get_secret`` 是阻塞 I/O，manifest 每次构建（对话/能力查询触发）都在
#: 主线程执行；TTL 既消除热路径重复 I/O，又把"凭证被增删后 manifest 状态
#: 短暂滞后"限制在 30s 内（该字段是 informational，策略/授权门在真实
#: 生成路径仍会重新取 secret）。
_CREDENTIAL_PROBE_TTL_S = 30.0


class _AiTranscriptionRelay(QObject):
    """AUD-2108: 把 ASR worker（QThread）信号经 Qt 队列连接转回主线程。

    worker 的 ``progress``/``completed``/``failed``/``finished`` 直接连接
    普通 Python 回调时是直连语义——回调在 ASR 工作线程执行，调用方一旦在
    回调里触碰 UI/工程对象即违反"UI 线程铁律"，且无任何运行期防护。
    本中继是主线程亲和的 QObject：worker 信号 → 中继信号为自动连接
    （接收者线程亲和 = 主线程 → queued），中继信号 → 普通 Python 回调
    因此在主线程事件循环里执行，不再依赖"回调只碰纯数据"的口头约定。
    """

    progress_received = pyqtSignal(object, str)
    completed_received = pyqtSignal(object)
    failed_received = pyqtSignal(str)
    finished_received = pyqtSignal()


def _resolve_subtitle_safe_area_policy(inputs: dict[str, Any]) -> tuple[str, float]:
    """Resolve the export safe-area profile to a validated fractional inset."""
    from src.features.edit.ai_editing.delivery_contracts import (
        SUBTITLE_SAFE_AREA_INSETS,
    )

    profile_value = inputs.get("safe_area_profile", inputs.get("subtitle_safe_area_profile"))
    profile = str(profile_value or "title_safe").strip().lower().replace("-", "_")
    raw_inset = inputs.get("safe_area_inset", inputs.get("subtitle_safe_area_inset"))
    if raw_inset not in (None, ""):
        try:
            inset = float(raw_inset)
        except (TypeError, ValueError) as exc:
            raise ValueError("自定义字幕安全区 inset 无效") from exc
        if profile in ("", "title_safe", "default"):
            profile = "custom"
    elif profile in ("", "default"):
        profile = "title_safe"
        inset = SUBTITLE_SAFE_AREA_INSETS[profile]
    elif profile == "custom":
        raise ValueError("custom 字幕安全区必须配置 safe_area_inset")
    else:
        try:
            inset = SUBTITLE_SAFE_AREA_INSETS[profile]
        except KeyError as exc:
            raise ValueError(f"未知字幕安全区 profile：{profile}") from exc
    if not math.isfinite(inset) or not 0.0 <= inset < 0.5:
        raise ValueError("字幕安全区 inset 必须在 [0, 0.5) 内")
    return profile, inset


def _count_enabled_subtitle_clips(sequence: Any) -> int:
    """AUD-2110: 轻量统计启用的字幕条数（纯数据遍历，不做纹理测量）。"""
    tracks = tuple(getattr(sequence, "subtitle_tracks", ()) or ())
    return sum(
        1
        for track in tracks
        if getattr(track, "is_enabled", True) and getattr(track, "is_visible", True)
        for clip in (getattr(track, "subtitle_clips", ()) or ())
        if getattr(clip, "is_enabled", True)
    )


def _build_measured_subtitle_quality_cues(
    sequence: Any,
    expected_resolution: tuple[int, int] | None,
    *,
    safe_area_profile: str,
    safe_area_inset: float,
) -> tuple[dict[str, Any], ...]:
    """Build delivery cues from the same resolved style the exporter burns in."""
    tracks = tuple(getattr(sequence, "subtitle_tracks", ()) or ())
    clips = tuple(
        clip
        for track in tracks
        if getattr(track, "is_enabled", True) and getattr(track, "is_visible", True)
        for clip in (getattr(track, "subtitle_clips", ()) or ())
        if getattr(clip, "is_enabled", True)
    )
    if not clips:
        return ()
    if expected_resolution is None:
        raise ValueError("存在字幕时必须提供导出画布分辨率，才能测量安全区")
    try:
        canvas_w, canvas_h = (int(expected_resolution[0]), int(expected_resolution[1]))
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError("导出画布分辨率无效") from exc

    from src.features.edit.ai_editing.delivery_contracts import build_subtitle_safe_area
    from src.features.edit.core.captions.model import (
        EMPTY_STYLE,
        clip_style_base,
        resolve_style,
    )
    from src.features.edit.core.subtitle_renderer import measure_subtitle_render_bbox

    safe_area = build_subtitle_safe_area(
        canvas_w, canvas_h, inset=safe_area_inset
    )
    measured: list[dict[str, Any]] = []
    for track in tracks:
        if not getattr(track, "is_enabled", True) or not getattr(track, "is_visible", True):
            continue
        track_metadata = getattr(track, "caption_metadata", None)
        track_default = (
            getattr(track_metadata, "default_style", None)
            if track_metadata is not None
            else None
        ) or EMPTY_STYLE
        safe_anchor = getattr(track_metadata, "safe_anchor", None)
        for clip in (getattr(track, "subtitle_clips", ()) or ()):
            if not getattr(clip, "is_enabled", True):
                continue
            try:
                style = resolve_style(
                    track_default,
                    getattr(clip, "style_override", None),
                    base=clip_style_base(clip),
                )
                bbox = measure_subtitle_render_bbox(
                    clip, canvas_w, canvas_h, style=style
                )
            except Exception as exc:  # noqa: BLE001
                raise ValueError(
                    f"字幕 {getattr(clip, 'clip_id', '')} 安全区测量失败"
                ) from exc
            if len(bbox) != 4 or not all(math.isfinite(float(value)) for value in bbox):
                raise ValueError(
                    f"字幕 {getattr(clip, 'clip_id', '')} bbox 测量结果无效"
                )
            measured.append({
                "clip_id": str(getattr(clip, "clip_id", "")),
                "start_frame": int(getattr(clip, "timeline_start", 0)),
                "end_frame": int(getattr(clip, "timeline_end")),
                "text": str(getattr(clip, "text", "") or ""),
                "bbox": [float(value) for value in bbox],
                "safe_area": [float(value) for value in safe_area],
                "safe_area_profile": safe_area_profile,
                "safe_anchor": safe_anchor,
            })
    return tuple(measured)


def _prepare_brand_quality_contract(
    inputs: dict[str, Any],
    expected_resolution: tuple[int, int] | None,
    *,
    safe_area_inset: float,
    sequence: Any | None = None,
    assets: Mapping[str, Any] | None = None,
) -> tuple[tuple[dict[str, Any], ...], tuple[Any, ...]]:
    """Normalize measured brand cues and attach the selected safe rectangle."""
    raw_requirements = inputs.get(
        "brand_requirements", inputs.get("required_brand_elements", ())
    )
    if raw_requirements in (None, ""):
        return (), ()
    requirements = (
        (raw_requirements,)
        if isinstance(raw_requirements, Mapping)
        else tuple(raw_requirements)
    )
    if expected_resolution is None:
        raise ValueError("存在品牌元素要求时必须提供导出画布分辨率")
    try:
        canvas_w, canvas_h = int(expected_resolution[0]), int(expected_resolution[1])
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError("品牌元素导出画布分辨率无效") from exc
    from src.features.edit.ai_editing.delivery_contracts import build_subtitle_safe_area

    safe_area = build_subtitle_safe_area(
        canvas_w, canvas_h, inset=safe_area_inset
    )
    raw_elements = inputs.get("brand_elements", ())
    if not raw_elements and sequence is not None:
        from src.features.edit.ai_editing.delivery_contracts import (
            materialize_brand_elements_from_timeline,
        )

        raw_elements = materialize_brand_elements_from_timeline(
            sequence,
            assets,
            expected_resolution=expected_resolution,
            safe_area=safe_area,
        )
    if raw_elements in (None, ""):
        elements = ()
    elif isinstance(raw_elements, Mapping):
        elements = (raw_elements,)
    else:
        elements = tuple(raw_elements)
    normalized: list[dict[str, Any]] = []
    for element in elements:
        if not isinstance(element, Mapping):
            raise ValueError("品牌元素测量必须是 JSON 对象")
        payload = dict(element)
        payload.setdefault("safe_area", [float(value) for value in safe_area])
        normalized.append(payload)
    return tuple(normalized), requirements


def _merge_brief_export_contract(
    service: Any,
    scope: Any,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    """Resolve persisted EditorialBrief delivery requirements into export inputs."""
    merged = dict(inputs)
    get_brief = getattr(service, "get_brief", None)
    if not callable(get_brief):
        return merged
    brief_id = str(
        getattr(scope, "brief_id", "")
        or getattr(service, "active_brief_id", "")
        or "default"
    )
    brief = get_brief(brief_id)
    if brief is None and brief_id != "default":
        brief = get_brief("default")
    if brief is None:
        return merged
    if not merged.get("brand_requirements") and getattr(brief, "brand_requirements", ()):
        merged["brand_requirements"] = tuple(brief.brand_requirements)
    if not merged.get("safe_area_profile") and getattr(brief, "safe_area_profile", None):
        merged["safe_area_profile"] = brief.safe_area_profile
    return merged


class MainWindowAiEditHostBridge:
    """``MainWindow`` ↔ AI 编辑子系统的类型化桥接（实现 ``AiEditHostBridge``）。

    构造时接收 ``assistant_service``（用于 ``get_plan_by_id`` 转发到其 PlanRepository）
    与 ``approval_service``（用于签发 grant）。``mainwindow`` 以弱引用持有。
    """

    def __init__(
        self,
        mainwindow: "MainWindow",
        assistant_service: Any,
        approval_service: ApprovalService,
        analysis_service: "AnalysisService | None" = None,
        *,
        legacy_no_policy: bool = False,
    ) -> None:
        self._mw = mainwindow
        self._service = assistant_service
        self._approval = approval_service
        # 共享 AnalysisService（与 AssistantService / MediaAnalysisController 同一实例）。
        # 可选：无工程/未装配时为 None，get_semantic_index 退化为空索引。
        self._analysis_service = analysis_service
        # AUD-2105：旧的"宿主缺 feature_flags 即放行"是隐式 fail-open 后门
        # （无头宿主 / 装配不完整的 fake / 未来 CLI 宿主都会绕过整份
        # ExecutionPolicy）。改为显式注入：仅 legacy transaction 测试的最小
        # fake 显式传 True；生产/未完全初始化的宿主一律 fail-closed。
        self._legacy_no_policy = bool(legacy_no_policy)
        self._generation_service: Any = None
        self._generation_materializer: Any = None
        self._async_generation_materializer: Any = None

    def set_generation_runtime(self, generation_service: Any) -> None:
        """注入本工程会话的 GenerationService（组合根调用）。"""
        self._generation_service = generation_service
        try:
            from src.features.edit.ai_editing.generated_media_materializer import (
                AsyncGeneratedMediaMaterializer,
                GeneratedMediaMaterializer,
            )

            self._generation_materializer = GeneratedMediaMaterializer()
            self._async_generation_materializer = AsyncGeneratedMediaMaterializer(
                self._generation_materializer,
                generation_service.job_registry,
            )
        except Exception:
            logger.debug("generated media materializer wiring failed", exc_info=True)

    # ------------------------------------------------------------------ #
    # 只读世界状态
    # ------------------------------------------------------------------ #

    def get_project_sequences(self) -> Any:
        """组装当前两层状态（``ProjectSequences``）。无工程返回 None。"""
        from src.features.edit.ai_editing.edit_project_service import ProjectSequences

        project = getattr(self._mw, "_current_edit_project", None)
        sequences = getattr(self._mw, "_current_timeline_sequences", None)
        if project is None or sequences is None:
            return None
        return ProjectSequences(project=project, sequences=dict(sequences))

    def _probe_credential_available(
        self, active_profile: Any, credential_store: Any,
    ) -> bool:
        """AUD-2111: 带 TTL 缓存的凭证可用性探测，避免主线程反复碰钥匙串。

        本地 provider（ollama/local_http/openai_compat）不需要 secret，直接
        可用；其余 profile 的 ``get_secret`` 落到真实 OS 钥匙串/文件 I/O，
        缓存 ``_CREDENTIAL_PROBE_TTL_S`` 秒。keyring 探测失败按不可用处理
        并留痕，不让 I/O 异常逃出 manifest 构建。
        """
        if active_profile is None:
            return False
        kind = str(getattr(active_profile, "provider_kind", "") or "")
        if kind in {"ollama", "local_http", "openai_compat"}:
            return True
        credential_ref = str(getattr(active_profile, "credential_ref", "") or "")
        if credential_store is None or not credential_ref:
            return False
        import time
        cache = getattr(self, "_credential_probe_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._credential_probe_cache = cache
        key = (kind, credential_ref, id(credential_store))
        entry = cache.get(key)
        now = time.monotonic()
        if entry is not None:
            cached_at, cached_value = entry
            if now - cached_at < _CREDENTIAL_PROBE_TTL_S:
                return bool(cached_value)
        try:
            available = bool(credential_store.get_secret(credential_ref))
        except Exception:  # noqa: BLE001 — 钥匙串被锁/后端故障按不可用降级
            logger.warning("credential availability probe failed", exc_info=True)
            available = False
        cache[key] = (now, available)
        return available

    def get_capability_manifest(self) -> CapabilityManifest:
        # P0: expose live, read-only capability status alongside the protocol
        # surface.  The manifest remains informational; policy/approval gates
        # still decide whether an operation may execute.
        controller = getattr(self._mw, "_ai_edit_controller", None)
        profiles = getattr(controller, "_profile_registry", None)
        active_profile = getattr(profiles, "active_profile", None)
        credential_store = getattr(controller, "_credential_store", None)
        credential_available = self._probe_credential_available(
            active_profile, credential_store,
        )
        generation_providers: tuple[Any, ...] = ()
        if self._generation_service is not None:
            registry = getattr(self._generation_service, "_registry", None)
            list_providers = getattr(registry, "list_providers", None)
            if callable(list_providers):
                generation_providers = tuple(list_providers())
        return build_manifest(
            feature_flags=getattr(self._mw, "feature_flags", None),
            profile=active_profile,
            credential_available=credential_available,
            generation_providers=generation_providers,
            analysis_provider_ids=tuple(
                self._analysis_service.available_providers()
                if self._analysis_service is not None
                else ()
            ),
            # T-P0-00：所有入口共享同一份四态运行时能力事实（含 CLI 离线路径）。
            runtime_snapshot=self._runtime_capability_snapshot(),
        )

    def _runtime_capability_snapshot(self) -> Any:
        """进程级缓存的 RuntimeCapabilitySnapshot（T-P0-00）。

        首次访问构建；环境变化（dylib/GPU/模型安装）后由
        ``invalidate_runtime_capability_snapshot()`` 显式失效——探测含
        subprocess/dylib 加载，不做隐式自动刷新。
        """
        snapshot = getattr(self, "_cached_capability_snapshot", None)
        if snapshot is None:
            from src.features.edit.ai_editing.runtime_capability_snapshot import (
                build_runtime_capability_snapshot,
            )

            model_manager = getattr(self._mw, "model_pack_manager", None)
            snapshot = build_runtime_capability_snapshot(
                model_pack_manager=model_manager,
            )
            self._cached_capability_snapshot = snapshot
        return snapshot

    def invalidate_runtime_capability_snapshot(self) -> None:
        """显式失效缓存的运行时能力快照（下次 get_capability_manifest 重建）。"""
        self._cached_capability_snapshot = None

    def get_semantic_index(self) -> SemanticIndex:
        """当前工程的语义索引（从真实分析证据构建）。

        收集当前工程涉及的全部 asset_id（优先 timeline clips，回退到 project.assets），
        用共享 helper ``build_index_from_analysis`` 把已分析素材的 segment 灌入索引。
        这让自由对话 / legacy brief 路径（路径 A）与 skill 工作流路径（路径 B）
        共用同一份证据收集逻辑，避免路径 A 因拿不到证据而必败（TAEP §5.4）。

        无 analysis_service / 无工程 / 无已分析素材时返回空索引——planner 据此
        产出"请先分析素材"诊断，而非假装理解素材。
        """
        if self._analysis_service is None:
            return SemanticIndex()
        asset_ids = self._collect_timeline_asset_ids()
        return build_index_from_analysis(self._analysis_service, asset_ids)

    def _collect_timeline_asset_ids(self) -> tuple[str, ...]:
        """收集当前工程涉及的 asset_id（timeline clips 优先，回退 project.assets）。

        顺序：先遍历活动 timeline 的全部 clip（含字幕轨道之外），拿到它们引用的
        asset_id；若 timeline 为空或无 clip，回退到工程全部素材。这样无论用户是否
        已把素材放上时间线，只要该素材被分析过，证据都能进入索引。
        """
        ps = self.get_project_sequences()
        if ps is None:
            return ()
        # 优先：timeline clips 引用的 asset
        clip_ids: list[str] = []
        sequences = getattr(ps, "sequences", None) or {}
        for seq in sequences.values():
            all_tracks = getattr(seq, "all_tracks", None) or ()
            for track in all_tracks:
                for clip in getattr(track, "clips", ()) or ():
                    aid = getattr(clip, "asset_id", "")
                    if aid:
                        clip_ids.append(aid)
        if clip_ids:
            # 去重保序
            seen: set[str] = set()
            ordered: list[str] = []
            for aid in clip_ids:
                if aid not in seen:
                    seen.add(aid)
                    ordered.append(aid)
            return tuple(ordered)
        # 回退：工程全部素材（含尚未放上时间线的）
        project = getattr(ps, "project", None)
        assets = getattr(project, "assets", None) or {}
        return tuple(str(aid) for aid in assets.keys())

    # ------------------------------------------------------------------ #
    # 授权签发（M0.1）
    # ------------------------------------------------------------------ #

    def approval_service(self) -> ApprovalService:
        return self._approval

    def is_autonomous_mode_enabled(self) -> bool:
        """Read live opt-in plus the independent emergency kill switch."""
        flags = getattr(self._mw, "feature_flags", None)
        checker = getattr(flags, "is_enabled", None)
        if not callable(checker):
            return False
        try:
            if not bool(checker("agent_auto_mode")):
                return False
        except Exception:  # noqa: BLE001 - policy lookup fails closed
            return False
        kill_switch = getattr(self._mw, "_ai_kill_switch", None)
        if kill_switch is None:
            return True
        try:
            from src.features.edit.ai_editing.kill_switch import Capability

            return bool(
                kill_switch.is_enabled(Capability.AI_ASSISTANT)
                and kill_switch.is_enabled(Capability.SKILLS)
            )
        except Exception:  # noqa: BLE001 - a broken safety control fails closed
            return False

    def request_plan_approval(
        self, plan: EditPlan, preview: PreviewResult
    ) -> ApprovalGrant | None:
        """弹确认对话框；用户确认后签发绑定作用域的 ``ApprovalGrant``。

        展示 diff 摘要（added/removed/duration）+ undo label；**不**显示 plan JSON
        或 secret。用户拒绝 / 关闭对话框返回 None。
        """
        # 构造人类可读的 diff 摘要
        diff = preview.diff
        if diff is not None:
            summary = (
                f"新增 {len(diff.added_clips)} 个片段，"
                f"删除 {len(diff.removed_clips)} 个片段，"
                f"移动 {len(diff.moved_clips)}，裁切 {len(diff.trimmed_clips)}\n"
                f"时长：{diff.duration_before} → {diff.duration_after} 帧\n"
                f"影响轨道：{', '.join(diff.affected_track_ids) or '无'}"
            )
            for w in diff.warnings:
                summary += f"\n⚠ {w}"
        else:
            summary = "（无 diff）"

        label = getattr(getattr(preview, "compiled", None), "undo_label", None) or plan.title or "AI 编辑"

        try:
            from PyQt6.QtWidgets import QMessageBox

            from src.shared.i18n.i18n import tr

            reply = QMessageBox.question(
                self._mw,
                tr("ai_assistant.approve_title", "确认应用 AI 方案"),
                tr(
                    "ai_assistant.approve_message",
                    "即将以「{label}」应用以下变更，可作为一次撤销恢复：\n\n{summary}",
                    label=label, summary=summary,
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return None
        except Exception:
            # 无 Qt 环境（测试 / headless）：默认拒绝，避免误授权
            logger.debug("approval dialog unavailable; declining by default")
            return None

        # 用户确认 → 签发绑定作用域的 grant
        from src.features.edit.ai_editing.transaction_service import _expected_commit_scope

        scope = _expected_commit_scope(plan, diff_hash=preview.diff_hash)
        return self._approval.issue_after_user_confirmation(scope, issued_by="main-window")

    def issue_automatic_plan_approval(
        self, plan: EditPlan, preview: PreviewResult, *,
        asset_ids: tuple[str, ...] = (),
    ) -> ApprovalGrant | None:
        """Issue one bounded grant for the explicitly enabled auto mode.

        ``agent_auto_mode`` is the user's durable opt-in.  We still rebuild the
        live project/timeline scope immediately before issuing the grant so a
        stale plan, a switched timeline, or a disabled policy cannot become an
        implicit write permission.
        """
        if not self.is_autonomous_mode_enabled() or preview is None or preview.has_errors:
            return None
        target = getattr(plan, "target", None)
        if str(getattr(target, "project_id", "") or "") != self.get_project_id():
            return None
        selected_asset_ids = tuple(self.get_selected_asset_ids() or ())
        requested_asset_ids = tuple(str(asset_id) for asset_id in asset_ids if str(asset_id))
        if selected_asset_ids and not set(requested_asset_ids).issubset(set(selected_asset_ids)):
            return None
        timeline_id = str(getattr(target, "timeline_id", "") or "")
        base_revision = int(getattr(plan, "base_revision", -1))
        current_timeline = self.get_active_timeline_id()
        if not timeline_id or timeline_id != current_timeline:
            return None
        policy = self.check_ai_action(
            "timeline_write",
            timeline_id=timeline_id,
            base_revision=base_revision,
            asset_ids=requested_asset_ids,
            approval_granted=True,
        )
        if not getattr(policy, "allowed", False):
            return None
        from src.features.edit.ai_editing.transaction_service import _expected_commit_scope

        scope = _expected_commit_scope(plan, diff_hash=preview.diff_hash)
        return self._approval.issue_after_user_confirmation(
            scope, issued_by="restricted-automatic",
        )

    # ------------------------------------------------------------------ #
    # plan 检索（M2.1）—— 转发给 service 的 PlanRepository
    # ------------------------------------------------------------------ #

    def get_plan_by_id(self, plan_id: str) -> EditPlan | None:
        repo = getattr(self._service, "plan_repository", None)
        if repo is None:
            return None
        return repo.get_plan(plan_id)

    # ------------------------------------------------------------------ #
    # 提交（M2.2：进入真实 CommandStack）
    # ------------------------------------------------------------------ #

    def commit_ai_command(
        self, timeline_id: str, macro: Any, *, undo_label: str
    ) -> Any:
        """把 ``macro`` 推入活动 timeline 的 ``TimelineView._command_stack``。

        复用界面 ``execute_command`` 的刷新链：单 undo + 全量刷新
        （widget/viewer/renderer/media-pool ref/mixer/captions/``_current_timeline_sequences``）。

        失败（无 view / timeline 不活动 / view 无 _timeline）返回 None。
        """
        view = self._resolve_timeline_view(timeline_id)
        if view is None:
            logger.warning("commit_ai_command: timeline view for %r not available", timeline_id)
            return None
        current = getattr(view, "_timeline", None)
        if current is None or getattr(current, "timeline_id", None) != timeline_id:
            logger.warning(
                "commit_ai_command: active timeline %r != target %r",
                getattr(current, "timeline_id", None), timeline_id,
            )
            return None
        policy = self.check_ai_action(
            "timeline_write",
            timeline_id=timeline_id,
            base_revision=int(getattr(current, "revision", 0)),
            approval_granted=True,
        )
        if not policy.allowed:
            logger.warning("commit_ai_command blocked by execution policy: %s", policy.reason)
            return None
        try:
            # execute_command 内部：command_stack.execute(macro) → 单 undo 条目；
            # emit timeline_changed → _on_edit_timeline_changed 全量刷新。
            view.execute_command(macro)
            return getattr(view, "_timeline", None)
        except Exception as exc:
            logger.error("commit_ai_command failed: %s", exc, exc_info=True)
            return None

    def detect_autonomous_replay(self, plan: EditPlan, state: Any) -> dict[str, Any]:
        """Detect an exact add-only replay after a crash window.

        The check is deliberately conservative: unsupported operation kinds
        return ``known=False`` and let the normal revision gate fail closed.
        For deterministic add operations, a fully present result is treated as
        already applied; a partially present result is blocked for inspection
        instead of risking a duplicate insertion.
        """
        target = getattr(plan, "target", None)
        timeline_id = str(getattr(target, "timeline_id", "") or "")
        sequence_map = getattr(state, "sequences", {}) or {}
        sequence = sequence_map.get(timeline_id)
        if sequence is None:
            return {"known": False}
        if int(getattr(sequence, "revision", -1)) <= int(getattr(plan, "base_revision", -1)):
            return {"known": False}

        expected_operations: list[tuple[str, Any, dict[str, Any]]] = []
        for operation in tuple(getattr(plan, "operations", ()) or ()):
            params = getattr(operation, "params", {}) or {}
            kind = getattr(getattr(operation, "kind", None), "value", None)
            kind = str(kind or getattr(operation, "kind", ""))
            if kind == "add_clip":
                op_id = str(getattr(operation, "op_id", "") or "")
                clip_id = str(params.get("clip_id", "") or "")
                if not clip_id:
                    from src.features.edit.ai_editing.plan_compiler import _derive_clip_id

                    clip_id = _derive_clip_id(str(getattr(plan, "plan_id", "")), op_id)
                expected_operations.append((clip_id, operation, params))
            elif kind == "add_subtitle":
                op_id = str(getattr(operation, "op_id", "") or "")
                clip_id = str(params.get("clip_id", "") or "")
                if not clip_id:
                    clip_id = f"{getattr(plan, 'plan_id', '')}:subtitle:{op_id}"
                expected_operations.append((clip_id, operation, params))
            else:
                return {"known": False}

        if not expected_operations:
            return {"known": False}
        present_by_id = {
            str(getattr(clip, "clip_id", "")): clip
            for track in (getattr(sequence, "all_tracks", ()) or ())
            for clip in (
                tuple(getattr(track, "clips", ()) or ())
                + tuple(getattr(track, "subtitle_clips", ()) or ())
            )
        }
        matched_count = 0
        conflict_count = 0
        for clip_id, operation, params in expected_operations:
            clip = present_by_id.get(clip_id)
            if clip is None:
                continue
            kind = getattr(getattr(operation, "kind", None), "value", None)
            kind = str(kind or getattr(operation, "kind", ""))
            if self._autonomous_replay_clip_matches(clip, kind, params):
                matched_count += 1
            else:
                conflict_count += 1
        if matched_count == len(expected_operations):
            return {"known": True, "already_applied": True}
        if matched_count or conflict_count:
            return {
                "known": True,
                "ambiguous": True,
                "reason": (
                    "检测到自主计划只完成了部分新增片段"
                    if matched_count else "检测到相同 clip_id 但内容不一致的片段"
                ),
            }
        return {"known": True, "already_applied": False}

    @staticmethod
    def _autonomous_replay_clip_matches(
        clip: Any, kind: str, params: dict[str, Any],
    ) -> bool:
        """Match stable plan fields to avoid treating an ID collision as replay."""
        expected_track = str(params.get("track_id", "") or "")
        if expected_track and str(getattr(clip, "track_id", "")) != expected_track:
            return False
        timeline = params.get("timeline")
        if isinstance(timeline, dict):
            if "start" in timeline and int(getattr(clip, "timeline_start", -1)) != int(timeline["start"]):
                return False
            if "duration" in timeline and int(getattr(clip, "timeline_duration", -1)) != int(timeline["duration"]):
                return False
        if kind == "add_clip":
            asset_id = str(params.get("asset_id", "") or "")
            if asset_id and str(getattr(clip, "asset_id", "")) != asset_id:
                return False
        if kind == "add_subtitle":
            raw_start = timeline.get("start") if isinstance(timeline, dict) else params.get("start_frame")
            raw_duration = timeline.get("duration") if isinstance(timeline, dict) else params.get("duration_frames")
            if raw_start is not None and int(getattr(clip, "timeline_start", -1)) != int(raw_start):
                return False
            if raw_duration is not None and int(getattr(clip, "timeline_duration", -1)) != int(raw_duration):
                return False
            if "text" in params and str(getattr(clip, "text", "")) != str(params.get("text", "")):
                return False
        return True

    def verify_autonomous_commit(
        self,
        plan: EditPlan,
        before_state: Any,
        after_state: Any,
        _preview: PreviewResult,
    ) -> dict[str, Any]:
        """Verify structural postconditions after a restricted auto commit.

        This is deliberately a host-side invariant check, not a subjective
        aesthetic score.  It confirms that the active project/timeline still
        matches the plan and that every referenced ``add_clip`` asset is now
        present.  A failed check is rolled back by ``AutonomousAgent``.
        """
        target = getattr(plan, "target", None)
        project_id = str(getattr(target, "project_id", "") or "")
        timeline_id = str(getattr(target, "timeline_id", "") or "")
        if project_id != self.get_project_id():
            return {"passed": False, "reason": "提交后工程作用域已变化"}
        after_sequences = getattr(after_state, "sequences", None)
        if isinstance(after_sequences, dict):
            sequence = after_sequences.get(timeline_id)
        else:
            sequence = after_state if getattr(after_state, "timeline_id", "") == timeline_id else None
        if sequence is None:
            return {"passed": False, "reason": "提交后目标时间线不可用"}
        before_sequences = getattr(before_state, "sequences", {}) or {}
        before_sequence = before_sequences.get(timeline_id)
        if before_sequence is not None and int(getattr(sequence, "revision", -1)) <= int(
            getattr(before_sequence, "revision", -1)
        ):
            return {"passed": False, "reason": "提交后时间线 revision 未前进"}

        clips = tuple(
            clip
            for track in (getattr(sequence, "all_tracks", ()) or ())
            for clip in (getattr(track, "clips", ()) or ())
        )
        for operation in tuple(getattr(plan, "operations", ()) or ()):
            kind = getattr(getattr(operation, "kind", None), "value", None)
            kind = str(kind or getattr(operation, "kind", ""))
            if kind != "add_clip":
                continue
            asset_id = str((getattr(operation, "params", {}) or {}).get("asset_id", "") or "")
            if asset_id and not any(str(getattr(clip, "asset_id", "")) == asset_id for clip in clips):
                return {
                    "passed": False,
                    "reason": f"提交后未找到新增素材 {asset_id}",
                }
        return {"passed": True, "reason": "结构化后置校验通过"}

    def rollback_ai_command(
        self,
        timeline_id: str,
        *,
        expected_undo_label: str,
        expected_revision: int | None,
    ) -> Any:
        """Undo an AI commit only when it is still the latest host command."""
        view = self._resolve_timeline_view(timeline_id)
        if view is None:
            return None
        current = getattr(view, "_timeline", None)
        if current is None or getattr(current, "timeline_id", None) != timeline_id:
            return None
        if expected_revision is not None and int(getattr(current, "revision", -1)) != int(expected_revision):
            logger.info("rollback_ai_command blocked by revision mismatch")
            return None
        can_undo = getattr(view, "can_undo", None)
        undo_label = getattr(view, "_command_stack", None)
        get_label = getattr(undo_label, "get_undo_label", None)
        if not callable(can_undo) or not callable(get_label):
            return None
        if not can_undo() or str(get_label() or "") != str(expected_undo_label):
            logger.info("rollback_ai_command blocked by undo target mismatch")
            return None
        try:
            view.undo()
            return getattr(view, "_timeline", None)
        except Exception as exc:
            logger.error("rollback_ai_command failed: %s", exc, exc_info=True)
            return None

    def _resolve_timeline_view(self, timeline_id: str) -> Any:
        """解析活动 timeline 的 TimelineView（弱依赖，缺失返回 None）。"""
        panel = getattr(self._mw, "timeline_panel", None)
        if panel is None:
            return None
        return getattr(panel, "timeline_view", None)

    # ------------------------------------------------------------------ #
    # 活动目标 + 提交反馈
    # ------------------------------------------------------------------ #

    def get_active_timeline_id(self) -> str | None:
        project = getattr(self._mw, "_current_edit_project", None)
        if project is None:
            return None
        return getattr(project, "active_timeline_id", None)

    def get_selected_asset_ids(self) -> tuple[str, ...]:
        """媒体池当前选中素材 ID（P1：InvocationScope.selected_asset_ids）。

        经 ``self._mw`` 访问 media pool selection；无选中或无法访问返回空 tuple
        （editorial skill 进 WAITING_INPUT，不崩）。
        """
        # 时间线上的音频片段是 AI“把音频转字幕”最明确的作用域；优先使用它，
        # 避免媒体池里残留的选中状态把请求导向另一份素材。
        selected_clip = self.get_selected_audio_clip()
        if selected_clip is not None:
            return (selected_clip[2],)
        try:
            # MainWindow 实际属性名是 media_pool_panel；旧名 media_pool_controller /
            # media_pool / _media_pool 作为向后兼容保留。
            for attr in ("media_pool_panel", "media_pool_controller",
                         "media_pool", "_media_pool"):
                mp = getattr(self._mw, attr, None)
                if mp is None:
                    continue
                # 公共方法优先（MediaPoolPanel.get_selected_asset_ids）
                if hasattr(mp, "get_selected_asset_ids"):
                    ids = mp.get_selected_asset_ids()
                    if ids:
                        return tuple(str(i) for i in ids)
                # 旧属性兜底
                if hasattr(mp, "selected_asset_ids"):
                    ids = getattr(mp, "selected_asset_ids", None)
                    if ids:
                        return tuple(str(i) for i in ids)
        except Exception:  # noqa: BLE001
            pass
        return ()

    def _selected_timeline_clip(self) -> tuple[Any, Any] | None:
        """返回当前选中的 ``(timeline, clip)``，不触碰渲染器。"""
        panel = getattr(self._mw, "timeline_panel", None)
        if panel is None:
            return None
        view = getattr(panel, "timeline_view", None)
        timeline = getattr(view, "_timeline", None)
        if timeline is None:
            timeline = getattr(getattr(self._mw, "edit_page", None), "_current_timeline", None)
        if timeline is None:
            return None
        getter = getattr(panel, "get_selected_clip_ids", None)
        clip_ids = tuple(getter() or ()) if callable(getter) else ()
        if not clip_ids:
            return None
        for clip_id in clip_ids:
            clip = timeline.get_clip_by_id(clip_id)
            if clip is not None:
                return timeline, clip
        return None

    def get_selected_audio_clip(self) -> tuple[str, str, str] | None:
        """返回当前选中的音频片段 ``(clip_id, track_id, asset_id)``。"""
        selected = self._selected_timeline_clip()
        if selected is None:
            return None
        timeline, clip = selected
        track = next(
            (candidate for candidate in timeline.all_tracks
             if candidate.get_clip_by_id(clip.clip_id) is not None),
            None,
        )
        if track is None or track.track_type != "audio":
            return None
        return str(clip.clip_id), str(track.track_id), str(clip.asset_id)

    def start_ai_transcription(
        self,
        asset_id: str,
        options: dict[str, Any],
        *,
        on_progress: Any,
        on_completed: Any,
        on_failed: Any,
    ) -> tuple[bool, str]:
        """启动 AI assistant 专用 ASR worker。

        复用编辑页已经验证过的 ``_SpeechToTextWorker``，但跳过右键菜单设置框；
        assistant 的默认设置由 controller 传入，所有耗时推理仍在 QThread 中运行。
        """
        from src.app.controllers.edit_controller import _SpeechToTextWorker
        from src.features.edit.ai_editing.analysis_models import AssetFingerprint
        from src.shared.i18n.i18n import tr

        current = getattr(self._mw, "_speech_to_text_worker", None)
        ai_current = getattr(self._mw, "_ai_transcription_worker", None)
        if (current is not None and current.isRunning()) or (
            ai_current is not None and ai_current.isRunning()
        ):
            return False, tr(
                "ai_assistant.auto_caption.busy",
                "A speech-to-text task is already running.",
            )
        service = getattr(self._mw, "_analysis_service", None)
        if service is None:
            return False, tr(
                "ai_assistant.auto_caption.service_unavailable",
                "The media analysis service is not initialized.",
            )
        project = getattr(self._mw, "_current_edit_project", None)
        asset = getattr(project, "assets", {}).get(asset_id) if project is not None else None
        path = str(getattr(asset, "path", "") or "")
        if not path or not os.path.isfile(path):
            return False, tr(
                "ai_assistant.auto_caption.media_missing",
                "The selected audio media file cannot be found.",
            )
        try:
            stat = os.stat(path)
            fingerprint = AssetFingerprint(
                size=int(stat.st_size), mtime_ns=int(stat.st_mtime_ns),
            )
            selected = self._selected_timeline_clip()
            timeline = selected[0] if selected is not None else None
            fps_obj = getattr(timeline, "fps", 24.0)
            fps = (
                f"{fps_obj.num}/{fps_obj.den}"
                if hasattr(fps_obj, "num") and hasattr(fps_obj, "den")
                else f"{float(fps_obj):.6f}"
            )
        except (OSError, TypeError, ValueError) as exc:
            return False, f"无法读取音频信息：{exc}"

        validator = getattr(getattr(self._mw, "_transcript_router", None), "validate_options", None)
        if callable(validator):
            validation_error = validator(dict(options or {}))
            if validation_error:
                return False, str(validation_error)

        worker = _SpeechToTextWorker(
            service, asset_id, fingerprint, fps,
            options=dict(options or {}), parent=self._mw,
        )
        self._mw._ai_transcription_worker = worker

        # AUD-2108: 回调经主线程中继转发。此前 worker 信号直连普通 Python
        # 函数（直连语义），on_progress/on_completed/on_failed 与清引用的
        # lambda 全部在 ASR 线程执行，安全性只靠"回调只碰纯数据"的约定。
        relay = _AiTranscriptionRelay(parent=self._mw)
        self._mw._ai_transcription_relay = relay

        relay.progress_received.connect(
            lambda fraction, message: on_progress(fraction, message))
        relay.completed_received.connect(on_completed)
        relay.failed_received.connect(lambda message: on_failed(str(message)))

        def _finished() -> None:
            self._mw._ai_transcription_worker = None
            self._mw._ai_transcription_relay = None

        relay.finished_received.connect(_finished)

        worker.progress.connect(relay.progress_received)
        worker.completed.connect(relay.completed_received)
        worker.failed.connect(relay.failed_received)
        worker.finished.connect(relay.finished_received)
        worker.start()
        return True, ""

    def commit_ai_transcript_subtitles(
        self, clip_id: str, track_id: str, asset_id: str, analysis: Any,
    ) -> tuple[bool, str]:
        """将 ASR 结果转换为真实 EditPlan，再审批并提交为一个 undo 单元。"""
        from src.features.edit.ai_editing.models import (
            EditOperation, EditPlan, OperationKind, PlanTarget,
        )
        from src.features.edit.ai_editing.plan_compiler import compile_plan
        from src.features.edit.ai_editing.transaction_service import _expected_commit_scope
        from src.features.edit.ai_editing.subtitle_placement import build_subtitle_placements

        state = self.get_project_sequences()
        if state is None:
            return False, "无活动工程，无法预览字幕写入"
        view = self._resolve_timeline_view(str(self.get_active_timeline_id() or ""))
        timeline = getattr(view, "_timeline", None)
        clip = timeline.get_clip_by_id(clip_id) if timeline is not None else None
        if clip is None:
            return False, "音频片段已被移除，无法生成字幕"

        placements = build_subtitle_placements(analysis.transcript, clip)
        if not placements:
            return False, "没有检测到可生成的字幕内容"

        project_id = str(getattr(state.project, "project_id", "") or "default")
        timeline_id = str(getattr(timeline, "timeline_id", "") or self.get_active_timeline_id() or "")
        existing_track = next(
            (candidate for candidate in timeline.subtitle_tracks
             if candidate.name == f"AI 字幕 ({asset_id[:8]})"),
            None,
        )
        track_name = f"AI 字幕 ({asset_id[:8]})"
        track_target_id = (
            existing_track.track_id if existing_track is not None
            else "ai-subtitle-" + uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"topos-color/{project_id}/{timeline_id}/{track_name}",
            ).hex[:20]
        )
        plan_id = str(uuid.uuid4())
        operations: list[EditOperation] = [EditOperation(
            "ensure_subtitle_track",
            OperationKind.ENSURE_SUBTITLE_TRACK,
            params={"track_id": track_target_id, "track_name": track_name},
            intent={"reason": "auto_caption: ensure subtitle track"},
        )]
        for index, placement in enumerate(placements):
            operations.append(EditOperation(
                f"subtitle_{index:03d}", OperationKind.ADD_SUBTITLE,
                params={
                    "track_id": track_target_id,
                    "timeline": {
                        "start": placement.start_frame,
                        "duration": placement.duration_frames,
                    },
                    "text": placement.text,
                    "word_timings": [
                        word.to_dict() for word in placement.word_timings
                    ],
                },
                intent={"reason": "auto_caption: from ASR evidence"},
            ))
        plan = EditPlan.create(
            target=PlanTarget(project_id, timeline_id),
            base_revision=int(getattr(timeline, "revision", 0)),
            operations=tuple(operations),
            title="生成 AI 字幕",
            rationale="ASR 结果经字幕时间映射后生成，待预览与审批写入",
            plan_id=plan_id,
        )
        preview = self._service.preview_plan(plan, state)
        if preview is None or preview.has_errors or preview.compiled is None:
            return False, "字幕方案预览未通过，请检查时间线状态"
        grant = self.request_plan_approval(plan, preview)
        if grant is None:
            return False, "用户取消了字幕写入"
        try:
            self._approval.consume(grant.grant_id, _expected_commit_scope(plan, diff_hash=preview.diff_hash))
            compiled = compile_plan(plan)
            new_timeline = self.commit_ai_command(
                timeline_id, compiled.macro, undo_label=compiled.undo_label,
            )
            if new_timeline is None:
                return False, "字幕提交失败：找不到活动时间线视图"
            self.refresh_after_ai_commit(new_timeline)
            return True, ""
        except Exception as exc:  # noqa: BLE001
            logger.exception("AI transcript subtitle plan commit failed")
            return False, str(exc)

    def show_ai_transcription_progress(
        self, fraction: float | None, message: str,
    ) -> None:
        show = getattr(self._mw, "_on_speech_to_text_progress", None)
        if callable(show):
            show(fraction, message)

    def show_ai_transcription_message(
        self, message: str, *, warning: bool = False,
    ) -> None:
        show = getattr(self._mw, "_show_speech_to_text_message", None)
        if callable(show):
            show(message, warning=warning)

    def request_ai_export(
        self, scope: Any, inputs: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        """安全启动 AI 视频导出并接入宿主完成后的质量门。"""
        from src.features.edit.ai_editing.export_service import (
            ExportRequest, ExportService, run_export_preflight,
        )
        from src.features.edit.ai_editing.delivery_analyzer import FfmpegDeliveryAnalyzer
        from src.features.edit.ai_editing.delivery_quality_gate import (
            SILENCE_POLICY_HARD_MAX,
            SILENCE_POLICY_MEASURE_ONLY,
        )
        from src.features.edit.ai_editing.jobs import JobKind
        from src.features.edit.ai_editing.media_probe import FfprobeMediaProbe

        state = self.get_project_sequences()
        timeline_id = str(getattr(scope, "timeline_id", "") or "")
        if state is None or not timeline_id:
            return False, "当前没有可导出的时间线"
        sequence = state.sequences.get(timeline_id)
        if sequence is None:
            return False, "找不到当前时间线"
        inputs = _merge_brief_export_contract(
            self._service,
            scope,
            dict(inputs or {}),
        )
        codec = str(inputs.get("codec") or "h264")
        include_audio = bool(inputs.get("include_audio", True))
        burn_subtitles = bool(inputs.get("burn_subtitles", True))
        commercial_ready_required = bool(inputs.get("commercial_ready_required", False))
        platform_spec = inputs.get("platform_spec", inputs.get("platform"))
        if platform_spec in (None, ""):
            platform_spec = None
        target_lufs: float | None = None
        if inputs.get("target_lufs") not in (None, ""):
            try:
                target_lufs = float(inputs["target_lufs"])
            except (TypeError, ValueError):
                return False, "目标 LUFS 无效"
            if not math.isfinite(target_lufs) or not -70.0 <= target_lufs <= 0.0:
                return False, "目标 LUFS 必须在 -70 到 0 之间"
        try:
            lufs_tolerance = float(inputs.get("lufs_tolerance", 1.0))
        except (TypeError, ValueError):
            return False, "LUFS 容差无效"
        if not math.isfinite(lufs_tolerance) or lufs_tolerance < 0.0:
            return False, "LUFS 容差必须为非负数"
        silence_policy = str(inputs.get("silence_policy", SILENCE_POLICY_MEASURE_ONLY)).strip()
        if silence_policy not in (SILENCE_POLICY_MEASURE_ONLY, SILENCE_POLICY_HARD_MAX):
            return False, "静音策略无效"
        max_silence_seconds: float | None = None
        if inputs.get("max_silence_seconds") not in (None, ""):
            try:
                max_silence_seconds = float(inputs["max_silence_seconds"])
            except (TypeError, ValueError):
                return False, "最大静音时长无效"
            if not math.isfinite(max_silence_seconds) or max_silence_seconds < 0.0:
                return False, "最大静音时长必须为非负数"
        if silence_policy == SILENCE_POLICY_HARD_MAX and max_silence_seconds is None:
            return False, "静音硬门必须配置最大静音时长"

        try:
            from PyQt6.QtWidgets import QFileDialog, QMessageBox

            path, _filter = QFileDialog.getSaveFileName(
                self._mw,
                "导出 AI 成片",
                "AI_export.mp4",
                "MP4 Video (*.mp4);;MOV Video (*.mov);;All Files (*)",
            )
        except Exception:
            logger.debug("AI export path dialog unavailable", exc_info=True)
            return False, "无法打开导出路径选择器"
        if not path:
            return False, "已取消导出路径选择"

        request = ExportRequest(
            timeline_id=timeline_id,
            codec=codec,
            include_audio=include_audio,
            burn_subtitles=burn_subtitles,
            output_path=path,
            commercial_ready_required=commercial_ready_required,
        )
        generation_bin = getattr(self._generation_service, "generation_bin", None)
        service = ExportService(generation_bin=generation_bin)
        config, error = service.build_export_config(request, state)
        if config is None:
            return False, getattr(error, "message", "导出配置无效")
        report = run_export_preflight(
            config,
            sequence,
            generation_bin=generation_bin,
            commercial_ready_required=commercial_ready_required,
        )
        if report.has_blocking:
            return False, "导出预检未通过：" + "; ".join(report.blocking)

        expected_resolution = getattr(config, "resolution", None)
        if expected_resolution is None:
            width = getattr(sequence, "width", None)
            height = getattr(sequence, "height", None)
            if width and height:
                expected_resolution = (int(width), int(height))
        # AUD-2110: 轻量预检留在对话框前（profile/inset 校验是纯数据），
        # 快速失败语义不变。
        try:
            safe_area_profile, safe_area_inset = _resolve_subtitle_safe_area_policy(inputs)
        except ValueError as exc:
            return False, str(exc)
        # AUD-2110: 字幕 bbox 逐条测量（每条可见字幕一次纹理栅格化）与品牌
        # 元素物化是 O(字幕数) 的重活，此前在 UI 线程同步执行，长节目可造成
        # 可感知卡顿。测量结果只被 _post_export 的交付质量门消费，因此移到
        # 编码 worker 的质量门阶段执行（编码前信号之前）。
        subtitle_clip_count = _count_enabled_subtitle_clips(sequence)
        brand_requested = inputs.get(
            "brand_requirements", inputs.get("required_brand_elements")
        ) not in (None, "")

        def _measure_export_quality_contracts(
        ) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...], tuple[Any, ...]]:
            """编码 worker 内测量字幕安全区 cues 与品牌元素合同（AUD-2110）。"""
            cues = _build_measured_subtitle_quality_cues(
                sequence,
                expected_resolution,
                safe_area_profile=safe_area_profile,
                safe_area_inset=safe_area_inset,
            )
            elements, requirements = _prepare_brand_quality_contract(
                inputs,
                expected_resolution,
                safe_area_inset=safe_area_inset,
                sequence=sequence,
                assets=getattr(state.project, "assets", {}) or {},
            )
            return cues, elements, requirements

        summary = (
            f"输出：{path}\n"
            f"编码：{codec}，音频：{'保留' if include_audio else '关闭'}，"
            f"字幕：{'烧录' if burn_subtitles else '不烧录'}"
        )
        if target_lufs is not None:
            summary += f"\n目标响度：{target_lufs:.1f} LUFS（容差 ±{lufs_tolerance:.1f}）"
        if platform_spec is not None:
            summary += f"\n平台交付规格：{platform_spec if isinstance(platform_spec, str) else '自定义'}"
        if subtitle_clip_count:
            summary += (
                f"\n字幕安全区：{safe_area_profile}（有效区域 "
                f"{(1.0 - 2.0 * safe_area_inset):.0%}，待测量 "
                f"{subtitle_clip_count} 条）"
            )
        if brand_requested:
            summary += "\n品牌元素合同：将在导出质量门阶段测量"
        summary += "\n静音策略：仅测量" if silence_policy == SILENCE_POLICY_MEASURE_ONLY else (
            f"\n静音硬门：最长 {max_silence_seconds:.1f} 秒"
        )
        if report.degraded:
            summary += "\n注意：" + "; ".join(report.degraded)
        try:
            reply = QMessageBox.question(
                self._mw,
                "确认导出 AI 成片",
                summary,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return False, "用户取消了导出"
        except Exception:
            return False, "无法完成导出确认"

        policy = self.check_ai_action(
            "final_export",
            timeline_id=timeline_id,
            base_revision=int(getattr(sequence, "revision", 0)),
            file_extension=os.path.splitext(path)[1],
            approval_granted=True,
        )
        if not policy.allowed:
            return False, f"导出策略阻断：{policy.reason}"

        start_export = getattr(self._mw, "perform_video_export", None)
        if not callable(start_export):
            return False, "宿主视频导出器尚未初始化"

        # AUD-2103：预检/字幕安全区/品牌合同/LUFS 全部按 scope.timeline_id
        # 测量，而宿主 perform_video_export 实际编码的是 Edit 页当前打开的
        # 时间线（settings 不携带 timeline_id）。两条时间线不一致时交付质量
        # 合同失真——在提交 Job 之前确定性失败（与 commit_ai_command 的
        # 一致性校验同构）。
        edit_page = getattr(self._mw, "edit_page", None)
        active_sequence = (
            getattr(edit_page, "_current_timeline", None)
            if edit_page is not None else None
        )
        if getattr(active_sequence, "timeline_id", None) != timeline_id:
            logger.warning(
                "AI export target %r is not the active timeline %r",
                timeline_id, getattr(active_sequence, "timeline_id", None),
            )
            return False, (
                "目标时间线未在编辑页打开：请先切换到该时间线再导出，"
                "否则质量合同测量的成片与实际导出不一致。"
            )

        job = service.jobs.submit(
            JobKind.EXPORT,
            label=f"AI export {codec} {timeline_id}",
        )
        delivery_analyzer = FfmpegDeliveryAnalyzer()

        expected_fps = str(getattr(config, "fps", None) or getattr(sequence, "fps", "")) or None
        subtitle_duration_frames = int(getattr(sequence, "duration_frames", 0) or 0) or None
        sequence_fps = getattr(sequence, "fps", None)
        if hasattr(sequence_fps, "to_float"):
            subtitle_fps = float(sequence_fps.to_float())
        else:
            subtitle_fps = float(sequence_fps or 24.0)
        try:
            subtitle_max_words_per_second = float(
                inputs.get("subtitle_max_words_per_second", 20.0)
            )
            subtitle_max_chars_per_line = int(
                inputs.get("subtitle_max_chars_per_line", 42)
            )
        except (TypeError, ValueError):
            return False, "字幕质量门参数无效"
        if subtitle_max_words_per_second <= 0 or subtitle_max_chars_per_line <= 0:
            return False, "字幕质量门参数必须为正数"

        def _post_export(output_path: str, stage_telemetry=None) -> tuple[bool, str]:
            """Run in the encoder worker before its success signal is emitted."""
            # AUD-2110: 字幕/品牌合同测量在编码 worker 内执行（本回调的
            # 运行线程），测量失败按质量门失败处理（返回 False → job FAIL），
            # 不再把逐条 bbox 栅格化压回 UI 线程。
            try:
                subtitle_cues, brand_quality_elements, brand_requirements_m = (
                    _measure_export_quality_contracts()
                )
            except ValueError as exc:
                return False, f"导出质量合同测量失败：{exc}"
            return service.complete_host_export(
                job,
                output_path,
                probe=FfprobeMediaProbe(),
                quality_analyzer=delivery_analyzer,
                export_telemetry=stage_telemetry,
                expected_resolution=expected_resolution,
                expected_fps=expected_fps,
                expected_audio_sample_rate=48000 if include_audio else None,
                expected_lufs=target_lufs,
                lufs_tolerance=lufs_tolerance,
                silence_policy=silence_policy,
                max_silence_seconds=max_silence_seconds,
                platform_spec=platform_spec,
                subtitle_cues=subtitle_cues,
                subtitle_timeline_duration_frames=subtitle_duration_frames,
                subtitle_fps=subtitle_fps,
                subtitle_max_words_per_second=subtitle_max_words_per_second,
                subtitle_max_chars_per_line=subtitle_max_chars_per_line,
                brand_elements=brand_quality_elements,
                brand_requirements=brand_requirements_m or None,
                brand_timeline_duration_frames=subtitle_duration_frames,
                artifact_metadata={
                    "project_id": str(getattr(state.project, "project_id", "")),
                    "timeline_id": timeline_id,
                    "codec": codec,
                    "include_audio": include_audio,
                    "burn_subtitles": burn_subtitles,
                },
            )

        job.mark_running()
        # AUD-2104：宿主 perform_video_export 存在多个不抛异常的提前 return
        # （无时间线 QMessageBox+return / config.validate 失败 / 用户拒绝
        # DeliveryMatrixDialog / 已有导出进行中）。这些"静默取消"路径下
        # job 不能永久停留在 running——以宿主的导出启动状态为接受信号
        # （接受路径会写 _video_export_started_at / _video_exporter）。
        started_at_before = getattr(self._mw, "_video_export_started_at", None)
        exporter_before = getattr(self._mw, "_video_exporter", None)
        try:
            start_export({
                "path": path,
                "codec": codec,
                "include_audio": include_audio,
                "resolution": getattr(config, "resolution", None),
                "fps": getattr(config, "fps", None),
                "_post_export": _post_export,
            })
        except Exception as exc:  # noqa: BLE001
            logger.exception("starting AI video export failed")
            job.fail("EXPORT_START_FAILED", str(exc))
            return False, f"导出启动失败：{exc}"
        started_at_after = getattr(self._mw, "_video_export_started_at", None)
        exporter_after = getattr(self._mw, "_video_exporter", None)
        accepted = (
            started_at_after is not None and started_at_after is not started_at_before
        ) or (
            exporter_after is not None and exporter_after is not exporter_before
        )
        if not accepted and job.status.is_active:
            # 宿主没有接受导出请求（提前 return 且未抛异常）。job 已 mark_
            # running，必须显式 fail，否则 JobRegistry 留下永久 running 的
            # 导出任务（is_active 守卫：编码 worker 若已同步完成则不覆盖终态）。
            job.fail(
                "EXPORT_NOT_STARTED",
                "宿主未启动导出（前置校验被拒绝、用户取消或已有导出进行中）",
            )
            return False, "导出未启动：宿主拒绝了导出请求。"
        return True, "导出任务已启动，正在后台编码。"

    def request_ai_generation_consent(
        self, scope: Any, inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """显示单次生成确认，返回 provider-neutral consent 字段。"""
        inputs = dict(inputs or {})
        modality = str(inputs.get("modality") or "image")
        provider_id = "deterministic-stub"
        if self._generation_service is not None:
            try:
                from src.features.edit.ai_editing.generation_models import GenerationModality

                provider = self._generation_service.preferred_provider(
                    GenerationModality(modality),
                )
                provider_id = str(getattr(provider, "provider_id", "") or provider_id)
            except Exception:
                logger.debug("resolve generation provider failed", exc_info=True)
        prompt = str(inputs.get("prompt") or "").strip()
        summary = (
            f"类型：{modality}\n"
            f"模型：{provider_id}\n"
            "结果：先进入生成素材箱，不会自动放入时间线。\n"
            "版权/上传：当前请求将按 provider 的生成条款处理，商用前仍需人工清权。"
        )
        if prompt:
            summary += f"\n提示摘要：{prompt[:120]}"
        try:
            from PyQt6.QtWidgets import QMessageBox

            reply = QMessageBox.question(
                self._mw,
                "确认 AI 媒体生成",
                summary,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
        except Exception:
            logger.debug("AI generation consent dialog unavailable", exc_info=True)
            return None
        if reply != QMessageBox.StandardButton.Yes:
            return None
        policy = self.check_ai_action(
            "paid_generation",
            timeline_id=getattr(scope, "timeline_id", None),
            asset_ids=tuple(getattr(scope, "selected_asset_ids", ()) or ()),
            template_id=str(inputs.get("workflow_id") or "") or None,
            approval_granted=True,
        )
        if not policy.allowed:
            logger.info("AI generation blocked by execution policy: %s", policy.reason)
            return None
        return {
            "confirmed": True,
            "provider_id": provider_id,
            "cost_acknowledged": True,
            "upload_acknowledged": True,
            "copyright_acknowledged": True,
            "target_location": "generation_bin",
        }

    def import_ai_generated_asset(self, asset_id: str) -> tuple[bool, str]:
        """显式把生成素材箱候选导入 Media Pool；不接受虚拟/远端句柄。"""
        service = self._generation_service
        project = getattr(self._mw, "_current_edit_project", None)
        if service is None or project is None:
            return False, "当前没有可用的生成素材箱或工程"
        gen_bin = getattr(service, "generation_bin", None)
        asset = gen_bin.get(asset_id) if gen_bin is not None else None
        if asset is None:
            return False, "找不到生成候选：" + str(asset_id)
        materializer = self._generation_materializer
        if materializer is None:
            return False, "生成媒体物化器尚未初始化"
        materialized = materializer.materialize(asset)
        if not materialized.succeeded or not materialized.path:
            return False, materialized.message
        return self.import_ai_generated_asset_from_path(asset_id, materialized.path)

    def start_ai_generated_materialization(
        self,
        asset_id: str,
        *,
        on_progress: Any,
        on_completed: Any,
        on_failed: Any,
    ) -> tuple[bool, str, str | None]:
        """把生成句柄交给后台 Job；回调只传递数据，不触碰工程/UI对象。"""
        service = self._generation_service
        gen_bin = getattr(service, "generation_bin", None) if service is not None else None
        asset = gen_bin.get(asset_id) if gen_bin is not None else None
        worker = self._async_generation_materializer
        if asset is None:
            return False, "找不到生成候选：" + str(asset_id), None
        if worker is None:
            return False, "生成媒体后台物化器尚未初始化", None
        try:
            def _progress(job: Any, fraction: float, message: str) -> None:
                if callable(on_progress):
                    on_progress(asset_id, fraction, message)

            def _completed(job: Any, result: Any) -> None:
                if result.succeeded and result.path:
                    if callable(on_completed):
                        on_completed(asset_id, result.path, result.message)
                elif callable(on_failed):
                    on_failed(asset_id, result.message)

            job = worker.start(
                asset,
                on_progress=_progress,
                on_completed=_completed,
            )
            return True, "已加入后台生成媒体下载队列。", job.job_id
        except Exception as exc:  # noqa: BLE001
            logger.exception("start generated media materialization failed")
            if callable(on_failed):
                try:
                    on_failed(asset_id, str(exc))
                except Exception:  # noqa: BLE001
                    logger.debug("generated materialization failure callback failed", exc_info=True)
            return False, f"无法启动生成媒体下载：{exc}", None

    def import_ai_generated_asset_from_path(
        self, asset_id: str, path: str,
    ) -> tuple[bool, str]:
        """在主线程把已下载的本地文件导入 Media Pool。"""
        service = self._generation_service
        project = getattr(self._mw, "_current_edit_project", None)
        gen_bin = getattr(service, "generation_bin", None) if service is not None else None
        asset = gen_bin.get(asset_id) if gen_bin is not None else None
        if project is None:
            return False, "当前没有可用的工程"
        if asset is None:
            return False, "找不到生成候选：" + str(asset_id)
        preview_handle = str(path or "").strip()
        if not preview_handle or not os.path.isfile(preview_handle):
            return False, "生成媒体本地文件不存在"
        try:
            asset = gen_bin.set_preview_handle(asset_id, preview_handle)
        except Exception as exc:  # noqa: BLE001
            logger.debug("persist generated materialized handle failed", exc_info=True)
            return False, f"无法记录生成媒体缓存：{exc}"

        modality = getattr(getattr(asset, "modality", None), "value", "image")
        media_type = "audio" if modality in ("music", "voice") else modality
        importer = self._GeneratedAssetImporter(self, project, preview_handle, media_type, asset)
        media_asset_id, diagnostics = gen_bin.import_to_media_pool(
            asset_id, importer=importer, approved_by="user",
        )
        if media_asset_id is None:
            message = diagnostics[0].message if diagnostics else "导入媒体池失败"
            return False, str(message)
        return True, f"生成候选已导入媒体池：{media_asset_id}"

    def get_ai_generated_media_asset_id(self, asset_id: str) -> str | None:
        """从 GenerationBin 导入审计中解析对应的 MediaAsset ID。"""
        service = self._generation_service
        gen_bin = getattr(service, "generation_bin", None) if service is not None else None
        if gen_bin is None:
            return None
        for event in gen_bin.import_events():
            if event.asset_id == asset_id:
                return event.media_asset_id
        return None

    def preview_ai_generated_asset(self, asset_id: str) -> tuple[bool, str]:
        """标记候选已预览，并把真实本地/远程句柄交给系统预览器。"""
        service = self._generation_service
        gen_bin = getattr(service, "generation_bin", None) if service is not None else None
        asset = gen_bin.get(asset_id) if gen_bin is not None else None
        if asset is None:
            return False, "找不到生成候选：" + str(asset_id)
        try:
            gen_bin.mark_previewed(asset_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("mark generated asset previewed failed")
            return False, f"无法更新候选预览状态：{exc}"
        handle = str(getattr(asset, "preview_handle", "") or "")
        if not handle:
            return True, "候选已标记为已预览，但 provider 尚未提供预览句柄"
        if handle.startswith("stub://"):
            return True, "候选已标记为已预览；当前 deterministic provider 只有虚拟预览句柄"
        try:
            from PyQt6.QtCore import QUrl
            from PyQt6.QtGui import QDesktopServices

            url = QUrl.fromLocalFile(handle) if os.path.isfile(handle) else QUrl(handle)
            if QDesktopServices.openUrl(url):
                return True, "已打开生成候选预览"
            return False, "系统预览器未能打开该候选"
        except Exception as exc:  # noqa: BLE001
            logger.debug("open generated preview failed", exc_info=True)
            return False, f"预览打开失败：{exc}"

    def get_ai_playhead_frame(self) -> int | None:
        """读取活动时间线播放头，供生成素材放置计划使用。"""
        panel = getattr(self._mw, "timeline_panel", None)
        getter = getattr(panel, "get_playhead_frame", None) if panel is not None else None
        if not callable(getter):
            view = getattr(panel, "timeline_view", None) if panel is not None else None
            getter = getattr(view, "get_playhead_frame", None)
        if not callable(getter):
            return None
        try:
            return max(0, int(getter()))
        except (TypeError, ValueError):
            return None

    def get_current_gl_viewer(self) -> Any:
        """当前可见页面的 QOpenGLWidget viewer（Color/Edit/Composite）；不可用返回 None。

        必须在 GUI 线程调用（grabFramebuffer 要求）。Deliver 复用 Edit 的 viewer。
        """
        page = getattr(self._mw, "current_page", None)
        viewer = None
        try:
            if page == "Composite":
                viewer = getattr(self._mw, "composite_viewer", None)
            elif page in ("Edit", "Deliver"):
                widget = getattr(self._mw, "program_viewer_widget", None)
                if widget is not None and hasattr(widget, "get_viewer"):
                    viewer = widget.get_viewer()
            else:  # "Color"（默认页）
                canvas = getattr(self._mw, "canvas", None)
                viewer = getattr(canvas, "gl_viewport", None) or (
                    canvas.viewport() if canvas is not None else None
                )
        except Exception:  # noqa: BLE001 — 抓帧是辅助能力，绝不能抛
            return None
        if viewer is None:
            return None
        if hasattr(viewer, "grabFramebuffer") and (
            not hasattr(viewer, "isVisible") or viewer.isVisible()
        ):
            return viewer
        return None

    class _GeneratedAssetImporter:
        """GenerationBin 的宿主注入点，复用 EditProject.import_asset。"""

        def __init__(self, bridge: Any, project: Any, path: str, media_type: str, asset: Any) -> None:
            self._bridge = bridge
            self._project = project
            self._path = path
            self._media_type = media_type
            self._asset = asset

        def import_asset(self, asset: Any) -> tuple[str | None, list[Any]]:
            try:
                new_project = self._project.import_asset(
                    path=self._path,
                    media_type=self._media_type,
                    metadata={
                        "ai_generated": True,
                        "generated_asset_id": getattr(asset, "asset_id", ""),
                        "generation_request_id": getattr(asset, "request_id", ""),
                        "provenance": getattr(getattr(asset, "provenance", None), "to_dict", lambda: {})(),
                    },
                    display_name=f"AI {getattr(asset, 'asset_id', 'generated')}",
                )
                if new_project is not self._project:
                    apply_change = getattr(self._bridge._mw, "_apply_project_change", None)
                    if callable(apply_change):
                        apply_change(new_project)
                imported = new_project.get_asset_by_path(self._path)
                imported_id = getattr(imported, "asset_id", "") if imported is not None else ""
                if not imported_id:
                    return None, []
                return str(imported_id), []
            except Exception:  # noqa: BLE001
                logger.exception("generated asset import failed")
                return None, []

    def get_project_id(self) -> str:
        """当前工程 ID（P1：InvocationScope.project_id 必填）。无工程返回空串。"""
        project = getattr(self._mw, "_current_edit_project", None)
        if project is None:
            return ""
        return str(getattr(project, "project_id", "") or "")

    def check_ai_action(
        self,
        action: Any,
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
    ) -> Any:
        """Evaluate the live action policy at the final host boundary.

        Intent routing is not a security boundary: export, generation and
        CommandStack can be reached through several UI paths.  Rebuild the
        current project scope immediately before the side effect and fail
        closed when the host is not fully initialized.
        """
        from src.features.edit.ai_editing.execution_policy import (
            ActionRequest,
            ExecutionPolicy,
            ExecutionScope,
        )

        project_id = self.get_project_id()
        if not project_id:
            from src.features.edit.ai_editing.execution_policy import PolicyDecision

            # AUD-2105：不再按"宿主缺 feature_flags"隐式放行（fail-open）。
            # 缺工程的宿主一律 fail-closed；只有显式声明 legacy_no_policy
            # 的最小测试 fake（legacy transaction 测试不建模 MainWindow /
            # feature flags）保持旧的放行行为。
            if self._legacy_no_policy:
                return PolicyDecision(True, reason="legacy host has no policy context", action=str(action))
            return PolicyDecision(False, reason="no active project", action=str(action))
        state = self.get_project_sequences()
        current_sequence = None
        if state is not None:
            selected_timeline_id = timeline_id or self.get_active_timeline_id()
            current_sequence = getattr(state, "sequences", {}).get(selected_timeline_id)
        current_revision = (
            int(getattr(current_sequence, "revision", 0))
            if current_sequence is not None
            else base_revision
        )
        scope = ExecutionScope(
            project_id=project_id,
            timeline_id=self.get_active_timeline_id(),
            base_revision=current_revision,
            asset_ids=tuple(asset_ids) or self.get_selected_asset_ids(),
            allowed_hosts=self._allowed_ai_hosts(),
        )
        policy = ExecutionPolicy.from_feature_flags(
            getattr(self._mw, "feature_flags", None), scope,
        )
        return policy.evaluate(
            ActionRequest(
                action=action,
                project_id=project_id,
                timeline_id=timeline_id,
                base_revision=base_revision,
                asset_ids=tuple(asset_ids),
                template_id=template_id,
                host=host,
                file_extension=file_extension,
                cost_units=cost_units,
                duration_seconds=duration_seconds,
            ),
            approval_granted=approval_granted,
        )

    def _allowed_ai_hosts(self) -> tuple[str, ...]:
        """Return configured provider hosts without exposing credentials."""
        from urllib.parse import urlparse

        controller = getattr(self._mw, "_ai_edit_controller", None)
        registry = getattr(controller, "_profile_registry", None)
        hosts: set[str] = set()
        profiles = registry.list() if registry is not None else ()
        for profile in profiles:
            base_url = str(getattr(profile, "base_url", "") or "")
            host = urlparse(base_url).hostname if base_url else None
            if host:
                hosts.add(host)
        return tuple(sorted(hosts))

    def refresh_after_ai_commit(self, result: Any) -> None:
        """``execute_command`` 已 emit ``timeline_changed`` 触发全量刷新；此处为空。"""
        return None

    # ------------------------------------------------------------------ #
    # Composite 统一编辑协议（AI/MCP）
    # ------------------------------------------------------------------ #

    def _composite_page(self) -> Any | None:
        page = getattr(self._mw, "composite_page", None)
        if page is None or getattr(page, "node_graph", None) is None:
            return None
        return page

    def get_composite_snapshot(self) -> dict[str, Any] | None:
        page = self._composite_page()
        if page is None:
            return None
        snapshot = CompositeEditService(page.node_graph).snapshot()
        snapshot["composition_id"] = (
            getattr(page, "_current_composite_id", "")
            or getattr(self._mw, "_current_composite_id", "")
            or None
        )
        snapshot["project_id"] = self.get_project_id() or None
        return snapshot

    def validate_composite_edit(self, plan: CompositeEditPlan) -> CompositeValidationReport:
        page = self._composite_page()
        if page is None:
            from src.features.composite.edit_service import CompositeDiagnostic
            return CompositeValidationReport((CompositeDiagnostic("error", "Composite page is not available"),))
        return CompositeEditService(page.node_graph).validate(plan)

    def apply_composite_edit(
        self, plan: CompositeEditPlan, *, approval: ApprovalGrant | str | None = None,
        client: str = "ai",
    ) -> CompositeApplyResult:
        page = self._composite_page()
        if page is None:
            from src.features.composite.edit_service import CompositeDiagnostic
            report = CompositeValidationReport((CompositeDiagnostic("error", "Composite page is not available"),))
            return CompositeApplyResult(False, plan.plan_id, plan.content_hash(), 0, 0, report=report, error="Composite page is not available")

        service = CompositeEditService(page.node_graph)
        current_generation = service.generation
        report = service.validate(plan)
        if not report.ok:
            return CompositeApplyResult(
                False, plan.plan_id, plan.content_hash(), current_generation,
                current_generation, report=report, error="composite plan validation failed",
            )
        # Local Python scripts are already user initiated. AI/MCP remains a
        # capability-gated caller and must carry a host-issued, one-use grant.
        if client != "script":
            if approval is None:
                from src.features.composite.edit_service import CompositeDiagnostic
                report = CompositeValidationReport((CompositeDiagnostic("error", "host approval is required"),))
                return CompositeApplyResult(False, plan.plan_id, plan.content_hash(), current_generation, current_generation, report=report, error="host approval is required")
            try:
                from src.features.edit.ai_editing.approval_service import ApprovalScope

                grant_id = approval.grant_id if isinstance(approval, ApprovalGrant) else str(approval)
                self._approval.consume(grant_id, ApprovalScope(
                    action="commit_plan",
                    project_id=self.get_project_id(),
                    timeline_id=plan.composition_id,
                    base_revision=plan.base_generation,
                    plan_hash=plan.content_hash(),
                ))
            except Exception as exc:
                from src.features.composite.edit_service import CompositeDiagnostic
                report = CompositeValidationReport((CompositeDiagnostic("error", str(exc)),))
                return CompositeApplyResult(False, plan.plan_id, plan.content_hash(), current_generation, current_generation, report=report, error=str(exc))

        result = service.apply(plan)
        if result.applied:
            page.record_external_composite_edit(result.before or {}, result.after)
        return result

    # ------------------------------------------------------------------ #
    # 生命周期（M1.2 teardown）
    # ------------------------------------------------------------------ #

    def teardown_for_project(self, project_id: str) -> None:
        """工程关闭/切换时撤销该工程的 grants。不抛。"""
        try:
            n = self._approval.revoke_for_project(project_id)
            logger.debug("revoked %d AI approval grants for project %r", n, project_id)
        except Exception:
            logger.debug("teardown_for_project: revoke failed", exc_info=True)
