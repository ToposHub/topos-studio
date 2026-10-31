"""Resolve the live host context used by a Workflow invocation.

The workflow UI is deliberately not allowed to invent project, timeline, asset,
brief, or analysis identifiers.  This module is a small, dependency-light
boundary between the host bridge and :class:`WorkflowCompiler`.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


class WorkflowContextError(ValueError):
    """A workflow cannot be safely compiled against the current host state."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class WorkflowInvocationContext:
    project_id: str
    timeline_id: str
    selected_asset_ids: tuple[str, ...]
    brief_id: str
    brief_revision: int
    base_revision: int
    analysis_snapshot_hash: str
    profile_id: str = ""
    permission: str = "propose"
    network_allowed: bool = False
    available_capabilities: frozenset[str] = frozenset()

    @property
    def context_hash(self) -> str:
        payload = {
            "project_id": self.project_id,
            "timeline_id": self.timeline_id,
            "selected_asset_ids": self.selected_asset_ids,
            "brief_id": self.brief_id,
            "brief_revision": self.brief_revision,
            "base_revision": self.base_revision,
            "analysis_snapshot_hash": self.analysis_snapshot_hash,
            "profile_id": self.profile_id,
            "permission": self.permission,
            "network_allowed": self.network_allowed,
            "available_capabilities": sorted(self.available_capabilities),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "timeline_id": self.timeline_id,
            "selected_asset_ids": list(self.selected_asset_ids),
            "brief_id": self.brief_id,
            "brief_revision": self.brief_revision,
            "base_revision": self.base_revision,
            "analysis_snapshot_hash": self.analysis_snapshot_hash,
            "profile_id": self.profile_id,
            "permission": self.permission,
            "network_allowed": self.network_allowed,
            "available_capabilities": sorted(self.available_capabilities),
            "context_hash": self.context_hash,
        }


def resolve_workflow_context(
    host_bridge: Any,
    assistant_service: Any,
    *,
    permission: str = "propose",
) -> WorkflowInvocationContext:
    """Read and validate one immutable snapshot from the active host.

    Missing context fails closed.  In particular, an empty asset selection is
    not silently promoted to "all assets".
    """
    get_project_id = getattr(host_bridge, "get_project_id", None)
    get_timeline_id = getattr(host_bridge, "get_active_timeline_id", None)
    get_assets = getattr(host_bridge, "get_selected_asset_ids", None)
    project_id = str(get_project_id() if callable(get_project_id) else "" or "")
    timeline_id = str(get_timeline_id() if callable(get_timeline_id) else "" or "")
    asset_ids = tuple(str(x) for x in (get_assets() if callable(get_assets) else ()) or ())
    if not project_id:
        raise WorkflowContextError("NO_PROJECT", "没有活动工程，无法编译 Workflow")
    if not timeline_id:
        raise WorkflowContextError("NO_TIMELINE", "没有活动时间线，无法编译 Workflow")
    if not asset_ids:
        raise WorkflowContextError("NO_ASSETS", "没有选中的素材；请先选择本次 Workflow 的素材范围")

    base_revision = _timeline_revision(host_bridge, timeline_id)
    brief_id = str(getattr(assistant_service, "active_brief_id", "") or "default")
    brief = None
    get_brief = getattr(assistant_service, "get_brief", None)
    if callable(get_brief):
        brief = get_brief(brief_id)
    brief_revision = int(getattr(brief, "revision", 0) or 0)
    analysis_hash = _analysis_snapshot_hash(host_bridge, asset_ids)
    profile_id = str(getattr(assistant_service, "active_profile_id", "") or "")
    network_allowed = permission != "read_only" and _network_allowed(assistant_service)
    capabilities = _available_capabilities(host_bridge, profile_id)
    return WorkflowInvocationContext(
        project_id=project_id,
        timeline_id=timeline_id,
        selected_asset_ids=asset_ids,
        brief_id=brief_id,
        brief_revision=brief_revision,
        base_revision=base_revision,
        analysis_snapshot_hash=analysis_hash,
        profile_id=profile_id,
        permission=permission,
        network_allowed=network_allowed,
        available_capabilities=capabilities,
    )


def _timeline_revision(host_bridge: Any, timeline_id: str) -> int:
    get_sequences = getattr(host_bridge, "get_project_sequences", None)
    state = get_sequences() if callable(get_sequences) else None
    sequences = getattr(state, "sequences", {}) if state is not None else {}
    sequence = sequences.get(timeline_id) if hasattr(sequences, "get") else None
    timeline = getattr(sequence, "timeline", sequence)
    revision = getattr(timeline, "revision", None)
    if revision is None:
        revision = getattr(sequence, "revision", 0)
    try:
        return max(0, int(revision or 0))
    except (TypeError, ValueError) as exc:
        raise WorkflowContextError("INVALID_REVISION", "活动时间线 revision 无效") from exc


def _analysis_snapshot_hash(host_bridge: Any, asset_ids: tuple[str, ...]) -> str:
    direct = getattr(host_bridge, "get_analysis_snapshot_hash", None)
    if callable(direct):
        value = str(direct(asset_ids) or "")
        if value:
            return value
    service = getattr(host_bridge, "_analysis_service", None)
    rows: list[dict[str, Any]] = []
    for asset_id in asset_ids:
        analysis = service.get_analysis(asset_id) if service is not None else None
        segments = service.get_segments(asset_id) if service is not None else ()
        rows.append({
            "asset_id": asset_id,
            "status": service.get_status(asset_id) if service is not None else "unknown",
            "analysis": analysis.to_dict() if analysis is not None and hasattr(analysis, "to_dict") else None,
            "segments": [s.to_dict() for s in segments if hasattr(s, "to_dict")],
        })
    raw = json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _network_allowed(assistant_service: Any) -> bool:
    profile = getattr(assistant_service, "active_profile", None)
    data_policy = getattr(profile, "data_policy", None)
    return bool(getattr(data_policy, "allow_media_upload", False))


def _comfy_executor_ready(executor: Any) -> bool:
    """executor 是否已**绑定 workflow 且探活成功**（诚实注册，AUD-23403）。

    宿主装配期会为每个已配置 profile 预建 executor（此时
    ``_bound_snapshot`` 为空、server_capabilities 为空集）——仅有配置不等于
    可用。可用信号（与 ``bind_comfy_workflow`` 的"探活 → 绑定 → 注册"顺序
    一致）：

    - ``_bound_snapshot`` 非空：宿主已绑定 ComfyUI workflow snapshot；
    - ``_caps.available_class_types`` 非空：``/object_info`` 探活成功过。
    """
    if getattr(executor, "_bound_snapshot", None) is None:
        return False
    caps = getattr(executor, "_caps", None)
    return bool(getattr(caps, "available_class_types", None))


def _available_capabilities(host_bridge: Any, profile_id: str) -> frozenset[str]:
    """Translate live provider registrations into workflow capability names."""
    service = getattr(host_bridge, "_analysis_service", None)
    provider_ids = tuple(service.available_providers()) if service is not None and callable(
        getattr(service, "available_providers", None)
    ) else ()
    capabilities: set[str] = {"ffmpeg"}
    for provider_id in provider_ids:
        value = str(provider_id).lower()
        if "asr" in value or "transcri" in value or "whisper" in value:
            capabilities.add("asr")
        if "scene" in value or "cut" in value:
            capabilities.add("scene_cut")
        if "ocr" in value:
            capabilities.add("ocr")
        if "audio" in value or "loudness" in value:
            capabilities.add("audio_features")
    if profile_id:
        capabilities.add("llm")
    # ComfyUI 执行器（批次 3）：只有**已绑定且探活成功**的 executor 才广告
    # image_gen/video_gen（AUD-23403）。装配期为已配置 profile 预建的未绑定
    # executor 不算——否则含 image_gen/video_gen 节点的图能通过编译期能力
    # 检查，运行期才因 executor 无绑定 workflow 失败。
    comfy_executors = getattr(host_bridge, "_comfy_generation_executors", None)
    if comfy_executors:
        for executor in comfy_executors.values():
            if _comfy_executor_ready(executor):
                capabilities.add("image_gen")
                capabilities.add("video_gen")
                break
    return frozenset(capabilities)


__all__ = ["WorkflowContextError", "WorkflowInvocationContext", "resolve_workflow_context"]
