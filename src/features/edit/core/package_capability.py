"""DESK-04B · 包内容能力分级（E/P/B/X）与操作限制。

问题
----
手机（或未来版本桌面）写的工程可能含本端不认识的效果/转场/结构。
此前没有统一判定"哪些内容可安全编辑、能否导出"的依据——要么全工程
拒开（过严），要么照常编辑导出（缺效果静默出错误成片，过松）。

分级（契约，随 ADR 0005 一并冻结）
----------------------------------
- **E（Editable）**：无发现或仅提示级发现——全部操作安全，双向编辑
  无损。
- **P（Partial）**：对象级锁定——存在未识别**且未参与渲染**的对象
  （禁用效果等）：其余内容照常编辑，锁定对象只读原样保留。
- **B（Bundle-aware）**：包由更高版本写入者产出（version_status 含
  future）——已识别区域仍可编辑，不熟悉的结构只读保留、保存不降级
  （never-downgrade 既有守卫）。
- **X（eXport-blocked）**：存在**阻断级**发现——启用中的未识别效果/
  转场参与渲染、manifest 必需特性本端不满足、参与渲染的素材既不在线
  也无内嵌资源。编辑仍可进行，但**导出被阻止**（绝不静默出错误成片；
  UI 可在显式告知后果后提供覆盖入口）。

优先级：X > B > P > E。

操作限制（``operation_allowed``）：
- ``export``：X 级阻止（附 blockers）；E/P/B 允许（P/B 附告警）。
- ``effect_param_edit``：锁定对象（未识别效果）阻止，其余允许。
- ``timeline_edit`` / ``clip_trim`` / ``subtitle_edit`` / ``color_edit``：
  各级均允许（结构性编辑不依赖未识别语义；未知字段由 DESK-04A 保留）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable

logger = logging.getLogger(__name__)

__all__ = [
    "GRADE_E", "GRADE_P", "GRADE_B", "GRADE_X",
    "CapabilityFinding",
    "PackageCapabilityReport",
    "grade_package_capabilities",
]

GRADE_E = "E"
GRADE_P = "P"
GRADE_B = "B"
GRADE_X = "X"

#: 操作集合（UI/导出门消费）。
OPERATIONS = (
    "timeline_edit", "clip_trim", "subtitle_edit", "color_edit",
    "effect_param_edit", "export",
)

_SEVERITY_INFO = "info"
_SEVERITY_WARNING = "warning"
_SEVERITY_BLOCKER = "blocker"


@dataclass(frozen=True)
class CapabilityFinding:
    """一条能力发现（对象级）。"""

    object_kind: str      # effect / transition / source_media / feature / package
    object_ref: str       # 定位（clip id / asset id / 特性名 / 维度名）
    capability_id: str    # 机器可查 id（如 "effect:optical_flow_v9"）
    severity: str         # info / warning / blocker
    detail: str

    @property
    def locks_object(self) -> bool:
        return self.object_kind in ("effect", "transition")


@dataclass(frozen=True)
class PackageCapabilityReport:
    """加载后的包内容能力分级与操作限制判定。"""

    grade: str
    findings: tuple[CapabilityFinding, ...] = ()

    @property
    def locked_object_refs(self) -> frozenset[str]:
        """锁定为只读的对象引用（未识别效果/转场所在对象；含禁用状态
        ——参数无从安全编辑，只是不升级导出阻断）。"""
        return frozenset(
            f.object_ref for f in self.findings if f.locks_object)

    @property
    def export_allowed(self) -> bool:
        return self.grade != GRADE_X

    @property
    def export_blockers(self) -> tuple[CapabilityFinding, ...]:
        return tuple(f for f in self.findings
                     if f.severity == _SEVERITY_BLOCKER)

    def operation_allowed(self, operation: str) -> tuple[bool, str]:
        """判定 *operation* 是否允许；返回 (允许, 原因/说明)。"""
        if operation not in OPERATIONS:
            return False, f"未知操作: {operation}"
        if operation == "export":
            if self.grade == GRADE_X:
                reasons = "; ".join(
                    f"{f.object_kind}:{f.object_ref}({f.detail})"
                    for f in self.export_blockers)
                return False, f"导出被阻止——{reasons}"
            return True, ""
        if operation == "effect_param_edit":
            # 对象级限制由调用方结合 locked_object_refs 判定；
            # 报告级：存在锁定对象时提示但不整体禁用。
            return True, (
                "存在锁定对象" if self.locked_object_refs else "")
        return True, ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "grade": self.grade,
            "findings": [
                {
                    "object_kind": f.object_kind,
                    "object_ref": f.object_ref,
                    "capability_id": f.capability_id,
                    "severity": f.severity,
                    "detail": f.detail,
                }
                for f in self.findings
            ],
            "locked_object_refs": sorted(self.locked_object_refs),
            "export_allowed": self.export_allowed,
        }


def _bump(grade: str, target: str) -> str:
    priority = {GRADE_E: 0, GRADE_P: 1, GRADE_B: 2, GRADE_X: 3}
    return target if priority[target] > priority[grade] else grade


def _iter_tracks(sequences: Iterable[Any]):
    for seq in sequences or ():
        for track in getattr(seq, "video_tracks", ()) or ():
            yield track
        for track in getattr(seq, "audio_tracks", ()) or ():
            yield track


def _iter_clips(sequences: Iterable[Any]):
    for track in _iter_tracks(sequences):
        for clip in track.clips:
            yield clip


def grade_package_capabilities(
    *,
    full_data: dict[str, Any] | None,
    timeline_sequences: Iterable[Any] | None = None,
    assets: dict[str, Any] | None = None,
    product_features: Iterable[str] | None = None,
) -> PackageCapabilityReport:
    """对加载后的包内容做 E/P/B/X 分级。

    Args:
        full_data: ``ToposProjectIO.load`` 的完整数据（含 manifest /
            version_status / resource_resolver）。
        timeline_sequences: 加载出的 TimelineSequence 集合。
        assets: 素材表（EditProject.assets 或 full_data["assets"]）。
        product_features: 本端产品特性集（None = 不检查 required_features）。
    """
    findings: list[CapabilityFinding] = []
    grade = GRADE_E
    data = full_data or {}

    # ── 1. 版本分级（D0 的 version_status）────────────────────
    for dimension, status in (data.get("version_status") or {}).items():
        if status.get("status") == "future":
            grade = _bump(grade, GRADE_B)
            findings.append(CapabilityFinding(
                object_kind="package", object_ref=dimension,
                capability_id=f"version:{dimension}",
                severity=_SEVERITY_WARNING,
                detail=(f"包由更高版本写入（v{status.get('file_version')}"
                        f" > 当前 v{status.get('current_version')}）；"
                        "未识别结构只读保留，保存不降级")))

    # ── 2. manifest 必需特性 ─────────────────────────────────
    manifest = data.get("manifest") or {}
    if product_features is not None:
        have = set(product_features)
        for feature in manifest.get("required_features", ()) or ():
            if feature not in have:
                grade = _bump(grade, GRADE_X)
                findings.append(CapabilityFinding(
                    object_kind="feature", object_ref=str(feature),
                    capability_id=f"feature:{feature}",
                    severity=_SEVERITY_BLOCKER,
                    detail="manifest 声明的必需特性本端不满足"))

    # ── 3. 效果 / 转场识别 ───────────────────────────────────
    try:
        from src.features.edit.core.effect_registry import get_effect_definition
    except Exception:  # noqa: BLE001 — 注册表不可用时跳过该维度
        get_effect_definition = None
    try:
        from src.features.edit.core.transition_catalog import (
            TRANSITION_TYPE_OPTIONS,
        )
        known_transitions = set(TRANSITION_TYPE_OPTIONS)
    except Exception:  # noqa: BLE001
        known_transitions = None

    for clip in _iter_clips(timeline_sequences):
        clip_enabled = bool(getattr(clip, "is_enabled", True))
        for effect in getattr(clip, "effects", ()) or ():
            effect_type = str(getattr(effect, "effect_type", "") or "")
            if not effect_type:
                continue
            known = (
                get_effect_definition(effect_type) is not None
                if get_effect_definition is not None else True)
            if known:
                continue
            effect_enabled = bool(getattr(effect, "is_enabled", True))
            participates = clip_enabled and effect_enabled
            severity = _SEVERITY_BLOCKER if participates else _SEVERITY_INFO
            findings.append(CapabilityFinding(
                object_kind="effect",
                object_ref=str(getattr(clip, "clip_id", "")),
                capability_id=f"effect:{effect_type}",
                severity=severity,
                detail=(f"未识别效果 {effect_type!r}"
                        + ("（启用中，参与渲染→阻止导出）" if participates
                           else "（已禁用，原样保留）"))))
            grade = _bump(
                grade, GRADE_X if participates else GRADE_P)
    if known_transitions is not None:
        # 转场挂在轨级（EditTrack.transitions），不在 clip 上
        for track in _iter_tracks(timeline_sequences):
            for transition in getattr(track, "transitions", ()) or ():
                ttype = str(getattr(transition, "transition_type", "") or "")
                if ttype and ttype not in known_transitions:
                    findings.append(CapabilityFinding(
                        object_kind="transition",
                        object_ref=str(getattr(track, "track_id", "")),
                        capability_id=f"transition:{ttype}",
                        severity=_SEVERITY_BLOCKER,
                        detail=f"未识别转场 {ttype!r}（参与渲染→阻止导出）"))
                    grade = _bump(grade, GRADE_X)

    # ── 4. 渲染必需素材可用性 ────────────────────────────────
    resolver = data.get("resource_resolver")
    resolver_diag = tuple(resolver.diagnostics()) if resolver else ()
    if resolver_diag:
        grade = _bump(grade, GRADE_X)
        for line in resolver_diag:
            findings.append(CapabilityFinding(
                object_kind="source_media", object_ref="resource",
                capability_id="resource:verify",
                severity=_SEVERITY_BLOCKER, detail=line))
    for asset_id, asset in (assets or {}).items():
        path = getattr(asset, "path", "") or ""
        has_resource = bool(getattr(asset, "resource_uri", None))
        offline = bool(getattr(asset, "is_offline", False))
        if path and os.path.exists(path):
            continue  # 在线
        if has_resource and resolver is not None:
            continue  # 内嵌资源路径（物化失败已由 resolver_diag 计入）
        if offline or not path:
            continue  # 显式离线素材（用户知情），不升级为阻断
        # 有路径但文件不在且无内嵌资源：渲染必需素材不可用
        grade = _bump(grade, GRADE_X)
        findings.append(CapabilityFinding(
            object_kind="source_media", object_ref=str(asset_id),
            capability_id="source_media:available",
            severity=_SEVERITY_BLOCKER,
            detail=f"素材文件不存在且无内嵌资源: {path}"))

    report = PackageCapabilityReport(
        grade=grade, findings=tuple(findings))
    if report.grade != GRADE_E:
        logger.info("package capability grade=%s findings=%d",
                    report.grade, len(report.findings))
    return report
