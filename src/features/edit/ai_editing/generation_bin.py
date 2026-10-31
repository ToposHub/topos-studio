"""TAEP M11 — 生成素材箱（§8.2 G1-G2 / §11.1 M11 / §11.2 M11 step 2）。

"生成素材箱"是生成候选的暂存区（§8.2）：所有候选先入素材箱，用户预览后**显式**导入
Media Pool，才成为普通 ``MediaAsset``（§11.2 M11 step 2）。在此之前生成候选**不是**
``MediaAsset``，不入 timeline，不污染工程（§11.1 M11 退出条件："生成取消/失败不污染工程"）。

设计（遵循 .clinerules / §0 / §1.2 / §8.2 / §11.2 M11）：

- **ai_editing 层暂存**（v1 决策）：``GenerationBin`` 是 in-memory 的候选暂存（``GeneratedAsset``
  列表 + 状态机），**不直接动** core ``project.py`` 的 ``MediaAsset``（那是接线批次）。
  镜像 M6 ``ArtifactStore`` 的 in-memory + 可注入模式。
- **状态机**（§8.2 G1-G2）：``generated → previewed → imported``（成 MediaAsset）或
  ``↘ discarded``。``import_to_media_pool()`` 是注入点（宿主接 MediaAsset / project IO）。
- **删除候选不影响原素材**（§11.2 M11 完成检查）：discard 只移除素材箱条目，不触碰 timeline
  或 Media Pool 里已存在的素材。
- **不可导入 gate**（§8.2 G2 + 内容安全）：``safety.blocked`` 或已 ``imported``/``discarded``
  的候选不可导入；``license=UNKNOWN`` 可导入但 ``is_commercial_ready=False``（导出时阻断
  商用就绪声明，§11.2 M11 step 4）。
- **provenance 可追溯**（§11.2 M11 完成检查："每个使用位置、模型版本和用户批准可追溯"）：
  每个 ``GeneratedAsset`` 携带完整 ``GenerationProvenance``；``import_to_media_pool`` 记录
  导入事件（asset_id + 时间 + 批准者）。
- **可导出 provenance report**（§11.2 M11 step 4）：``provenance_report()`` 汇总所有
  imported 候选的来源/许可/成本/安全。

红线：本模块**不**：自己写 timeline / 改 MediaAsset 定义 / 持久化到工程文件（落地由宿主
闭环负责；GenerationBin 是会话级暂存，工程关闭可按设置清除）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from src.features.edit.ai_editing.diagnostics import (
    Diagnostic,
    DiagnosticSeverity,
    TaepDiagnosticCode,
    make_diagnostic,
)
from src.features.edit.ai_editing.generation_models import (
    GeneratedAsset,
    GeneratedAssetState,
    LicenseStatus,
)


# ---------------------------------------------------------------------- #
# MediaPoolImporter —— 注入点（宿主接 MediaAsset / project IO）
# ---------------------------------------------------------------------- #


class MediaPoolImporter(Protocol):
    """把生成候选导入 Media Pool 的注入点（§8.2 G2："引入媒体必须走既有 Media Pool / project IO"）。

    宿主实现本协议，把 ``GeneratedAsset`` 转成 core ``MediaAsset`` 并加入 project。
    v1 不在此处改 core/project.py（接线批次）；测试用 stub。
    """

    def import_asset(self, asset: GeneratedAsset) -> tuple[str | None, list[Diagnostic]]:
        """导入一个生成候选到 Media Pool。

        Returns:
            (media_asset_id, diagnostics)：成功时 media_asset_id 非空；失败时 diagnostics 含 error。
        """
        ...


# ---------------------------------------------------------------------- #
# ImportEvent —— 导入审计（provenance 追溯）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ImportEvent:
    """一次候选导入事件（§11.2 M11 完成检查："用户批准可追溯"）。"""

    asset_id: str              # GeneratedAsset.asset_id
    media_asset_id: str        # 转换后的 MediaAsset.asset_id
    imported_at: str           # ISO 时间
    approved_by: str = ""      # 批准者
    license_at_import: str = ""  # 导入时的授权状态（LicenseStatus value）

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "media_asset_id": self.media_asset_id,
            "imported_at": self.imported_at,
            "approved_by": self.approved_by,
            "license_at_import": self.license_at_import,
        }


# ---------------------------------------------------------------------- #
# GenerationBin —— 候选暂存 + 状态机 + 导入
# ---------------------------------------------------------------------- #


class GenerationBin:
    """生成素材箱（§8.2 / §11.2 M11 step 2）。

    线程模型（AUD-13505）：与同目录 ``FeedbackStore`` 相同，一把
    ``threading.RLock`` 覆盖全部读写，**线程安全**——``AsyncGeneratedMediaMaterializer``
    的 ``on_completed`` 在 daemon worker 线程触发 ``set_preview_handle``，而
    UI 线程并发 ``add_candidates``/``discard`` 不会撕裂 dict 迭代。

    用法::

        bin = GenerationBin()
        bin.add_candidates(result.candidates)   # provider 回收的候选入箱
        bin.mark_previewed("gen-req1-c1")        # 用户预览后
        media_id, diags = bin.import_to_media_pool(
            "gen-req1-c1", importer=my_importer, approved_by="user",
        )                                        # 显式导入 Media Pool
        bin.discard("gen-req1-c2")               # 丢弃不满意的候选

    红线：候选在 imported 前不是 MediaAsset，不入 timeline（§11.1 M11 退出条件）。
    """

    def __init__(self) -> None:
        self._assets: dict[str, GeneratedAsset] = {}
        self._import_events: list[ImportEvent] = []
        self._lock = threading.RLock()

    # ---------------- 查询 ---------------- #

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._assets)

    def get(self, asset_id: str) -> GeneratedAsset | None:
        with self._lock:
            return self._assets.get(asset_id)

    def require(self, asset_id: str) -> GeneratedAsset:
        a = self.get(asset_id)
        if a is None:
            raise KeyError(f"generated asset not in bin: {asset_id}")
        return a

    def all_assets(self) -> tuple[GeneratedAsset, ...]:
        with self._lock:
            return tuple(self._assets.values())

    def by_state(self, state: GeneratedAssetState) -> tuple[GeneratedAsset, ...]:
        with self._lock:
            return tuple(a for a in self._assets.values() if a.state == state)

    def by_request(self, request_id: str) -> tuple[GeneratedAsset, ...]:
        with self._lock:
            return tuple(a for a in self._assets.values() if a.request_id == request_id)

    def pending_preview(self) -> tuple[GeneratedAsset, ...]:
        """待预览的候选（state=GENERATED）。"""
        return self.by_state(GeneratedAssetState.GENERATED)

    def importable(self) -> tuple[GeneratedAsset, ...]:
        """当前可导入的候选（safety 通过 + 未 imported/discarded）。"""
        with self._lock:
            return tuple(a for a in self._assets.values() if a.importable)

    # ---------------- 添加 / 状态迁移 ---------------- #

    def add_candidates(self, candidates: tuple[GeneratedAsset, ...]) -> None:
        """把 provider 回收的候选入箱（§8.2 G1）。已存在的 asset_id 跳过（幂等）。"""
        with self._lock:
            for c in candidates:
                if c.asset_id not in self._assets:
                    self._assets[c.asset_id] = c

    def mark_previewed(self, asset_id: str) -> GeneratedAsset:
        """标记候选已预览（generated → previewed）。"""
        with self._lock:
            a = self.require(asset_id)
            if a.state != GeneratedAssetState.GENERATED:
                return a  # 幂等：已 previewed/imported 不降级
            updated = a.with_state(GeneratedAssetState.PREVIEWED)
            self._assets[asset_id] = updated
            return updated

    def set_preview_handle(self, asset_id: str, preview_handle: str) -> GeneratedAsset:
        """记录 provider 句柄物化后的本地缓存路径。"""
        with self._lock:
            asset = self.require(asset_id)
            if not preview_handle:
                raise ValueError("preview_handle cannot be empty")
            updated = asset.with_preview_handle(preview_handle)
            self._assets[asset_id] = updated
            return updated

    def discard(self, asset_id: str) -> GeneratedAsset | None:
        """丢弃候选（不污染工程；§11.2 M11 完成检查："删除生成候选不影响原素材"）。

        已 imported 的候选不可 discard（已进 Media Pool，需走 Media Pool 的删除路径）。
        """
        with self._lock:
            a = self.get(asset_id)
            if a is None:
                return None
            if a.state == GeneratedAssetState.IMPORTED:
                return a  # 已导入，不能从素材箱丢弃
            updated = a.with_state(GeneratedAssetState.DISCARDED)
            self._assets[asset_id] = updated
            return updated

    # ---------------- 导入 Media Pool（注入点）---------------- #

    def import_to_media_pool(
        self,
        asset_id: str,
        *,
        importer: MediaPoolImporter,
        approved_by: str = "",
    ) -> tuple[str | None, list[Diagnostic]]:
        """显式导入候选到 Media Pool（§8.2 G2 / §11.2 M11 step 2）。

        红线：``safety.blocked`` / 已 imported/discarded 的候选拒绝导入（返回诊断）。
        导入成功后 state → IMPORTED，并记 ``ImportEvent``（provenance 追溯）。
        全程持锁（AUD-13505）：状态检查 → importer 注入点 → 状态迁移是同一
        临界区，防止并发 discard/import 撕裂状态机。
        """
        with self._lock:
            a = self.get(asset_id)
            if a is None:
                return None, [make_diagnostic(
                    DiagnosticSeverity.ERROR, TaepDiagnosticCode.ASSET_NOT_FOUND,
                    asset_id=asset_id,
                )]
            if not a.importable:
                reasons: list[str] = []
                if a.safety.blocked:
                    reasons.append(f"content safety blocked ({a.safety.categories})")
                if a.qa.blocked:
                    reasons.append(f"quality gate failed ({a.qa.failed_checks})")
                if a.state == GeneratedAssetState.IMPORTED:
                    reasons.append("already imported")
                if a.state == GeneratedAssetState.DISCARDED:
                    reasons.append("already discarded")
                return None, [make_diagnostic(
                    DiagnosticSeverity.ERROR, TaepDiagnosticCode.PERMISSION_DENIED,
                    reason=f"asset {asset_id} not importable: {'; '.join(reasons)}",
                )]

            # 经注入点导入（宿主实现 MediaPoolImporter.import_asset → 转 MediaAsset + 加 project）
            media_asset_id, importer_diags = importer.import_asset(a)
            if media_asset_id is None:
                # 导入失败：不改状态，把 importer 的诊断透传
                return None, importer_diags

            # 成功：状态迁移 + 记导入事件
            updated = a.with_state(GeneratedAssetState.IMPORTED)
            self._assets[asset_id] = updated
            self._import_events.append(ImportEvent(
                asset_id=asset_id,
                media_asset_id=media_asset_id,
                imported_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                approved_by=approved_by,
                license_at_import=a.license.value,
            ))
            return media_asset_id, []

    # ---------------- provenance report（§11.2 M11 step 4）---------------- #

    def import_events(self) -> tuple[ImportEvent, ...]:
        """所有导入事件（provenance 追溯）。"""
        with self._lock:
            return tuple(self._import_events)

    def provenance_report(self) -> dict[str, Any]:
        """生成 provenance report（§11.2 M11 step 4："导出时可产生 provenance report"）。

        汇总所有 imported 候选的来源/许可/成本/安全 + 导入事件。
        许可未知时 ``commercial_ready=False``，阻断"商用就绪"声明。
        """
        with self._lock:
            imported = self.by_state(GeneratedAssetState.IMPORTED)
            events_snapshot = tuple(self._import_events)
        entries = []
        audit_counters = {
            "missing_import_audit_count": 0,
            "missing_approval_count": 0,
            "missing_provider_identity_count": 0,
            "missing_model_identity_count": 0,
            "missing_prompt_hash_count": 0,
            "missing_license_evidence_count": 0,
            "missing_license_check_time_count": 0,
            "license_region_missing_count": 0,
            "safety_blocked_count": 0,
        }
        for a in imported:
            evts = [e for e in events_snapshot if e.asset_id == a.asset_id]
            event = evts[0] if evts else None
            provenance = a.provenance
            missing_import_audit = event is None or not event.media_asset_id
            missing_approval = event is None or not event.approved_by
            missing_provider = not provenance.provider_id
            missing_model = not provenance.model_id or not provenance.model_version
            missing_prompt_hash = not provenance.input_prompt_hash
            missing_license_evidence = (
                a.license.commercial_ready and not provenance.license_evidence_ref
            )
            missing_license_check_time = (
                a.license.commercial_ready and not provenance.license_checked_at
            )
            missing_license_region = (
                a.license.commercial_ready and not provenance.license_region
            )
            safety_blocked = a.safety.blocked
            for key, missing in (
                ("missing_import_audit_count", missing_import_audit),
                ("missing_approval_count", missing_approval),
                ("missing_provider_identity_count", missing_provider),
                ("missing_model_identity_count", missing_model),
                ("missing_prompt_hash_count", missing_prompt_hash),
                ("missing_license_evidence_count", missing_license_evidence),
                ("missing_license_check_time_count", missing_license_check_time),
                ("license_region_missing_count", missing_license_region),
                ("safety_blocked_count", safety_blocked),
            ):
                audit_counters[key] += int(missing)
            audit_complete = not any((
                missing_import_audit,
                missing_approval,
                missing_provider,
                missing_model,
                missing_prompt_hash,
                missing_license_evidence,
                missing_license_check_time,
                missing_license_region,
                safety_blocked,
            ))
            entries.append({
                "asset_id": a.asset_id,
                "media_asset_id": event.media_asset_id if event else "",
                "modality": a.modality.value,
                "provenance": provenance.to_dict(),
                "license": a.license.value,
                "commercial_ready": a.is_commercial_ready,
                "safety": a.safety.to_dict(),
                "cost_units": a.cost_units,
                "approved_by": event.approved_by if event else "",
                "imported_at": event.imported_at if event else "",
                "audit_complete": audit_complete,
            })
        audit_complete = not any(audit_counters.values())
        return {
            "imported_count": len(imported),
            "commercial_ready_count": sum(1 for a in imported if a.is_commercial_ready),
            "total_cost_units": sum(a.cost_units for a in imported),
            "any_license_unknown": any(a.license == LicenseStatus.UNKNOWN for a in imported),
            "any_license_pending": any(a.license == LicenseStatus.PENDING for a in imported),
            "audit_complete": audit_complete,
            **audit_counters,
            "entries": entries,
        }

    # ---------------- 清理 ---------------- #

    def clear_discarded(self) -> int:
        """清理已 discarded 的候选（释放内存）。返回清理数。"""
        with self._lock:
            before = len(self._assets)
            self._assets = {
                aid: a for aid, a in self._assets.items()
                if a.state != GeneratedAssetState.DISCARDED
            }
            return before - len(self._assets)


__all__ = [
    "MediaPoolImporter",
    "ImportEvent",
    "GenerationBin",
]
