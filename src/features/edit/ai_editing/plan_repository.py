"""TAEP Plan Repository（M2.1）。

保存"被批准的那一份 plan"——审查发现旧 ``EditVariant`` 只保留
``edit_plan_ref`` 字符串，planner 编译后的真实 ``EditPlan`` 对象直接丢失，
controller 依赖一个从未实现的 ``build_plan_for_variant`` 重建，apply 路径必然
no-op（审查 M2.1）。

本仓库是 plan 的不可变真相源：

- planner 编译 variant 时立即 ``store(plan, source_variant_id=...)``；
- variant 只引用 ``plan_id``/``plan_hash``（不持有 plan 对象）；
- 用户确认时对**仓库里的同一份** plan 签发 grant；commit 必须取仓库中的 immutable
  plan，不接受 UI 重新组装的 plan（总体不变量 §1.4：预览即提交）；
- preview 结果与 ``(plan_hash, project_id, timeline_id, base_revision,
  manifest_hash, state_fingerprint)`` 绑定，任一改变即 stale。

首版为进程内存仓库（M4 接 ``.topos`` payload 持久化）。线程安全（``RLock``）。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from threading import RLock
from typing import Any


def _canonical_json(plan: Any) -> bytes:
    """plan 的稳定序列化（用于 content_hash + 一致性比较）。"""
    return json.dumps(plan.to_dict(), sort_keys=True, ensure_ascii=False).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class StoredPlan:
    """仓库中一份不可变 plan + 元信息。

    Attributes:
        plan: 不可变 ``EditPlan``。
        canonical_json: plan 的稳定序列化（``to_dict`` + sort_keys）。
        content_hash: ``canonical_json`` 的 sha256 hex；grant 绑定此值。
        created_at: 存入时间戳。
        source_variant_id: 产生它的 variant（可 None，如脚本直存）。
        preview: 可选绑定的预览结果（``PreviewResult``）；与 ``preview_state_fingerprint``
            绑定，任一改变即 stale。
        preview_state_fingerprint: 预览所基于的状态指纹（``project_revision`` /
            ``manifest_hash`` 等）。
    """

    plan: Any
    canonical_json: bytes
    content_hash: str
    created_at: float = 0.0
    source_variant_id: str | None = None
    preview: Any = None  # PreviewResult | None
    preview_state_fingerprint: str | None = None

    @property
    def plan_id(self) -> str:
        return self.plan.plan_id

    def to_dict(self) -> dict[str, Any]:
        """可安全序列化的表示（供 ``.topos`` 持久化）。

        ``canonical_json`` 存 utf-8 解码后的字符串；``plan`` 走其自身 ``to_dict``。
        ``preview`` 若存在则存其 ``to_dict`` 子集（活对象 compiled/proposed_sequence
        不持久化，恢复后必须重新 preview）。
        """
        d: dict[str, Any] = {
            "plan": self.plan.to_dict(),
            "canonical_json": self.canonical_json.decode("utf-8"),
            "content_hash": self.content_hash,
            "created_at": self.created_at,
        }
        if self.source_variant_id is not None:
            d["source_variant_id"] = self.source_variant_id
        if self.preview is not None and hasattr(self.preview, "to_dict"):
            d["preview"] = self.preview.to_dict()
        if self.preview_state_fingerprint is not None:
            d["preview_state_fingerprint"] = self.preview_state_fingerprint
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StoredPlan":
        """从 ``to_dict`` 输出重建。

        恢复后的 ``preview``（若有）是回看用子集（无 compiled/proposed_sequence），
        因此 ``is_preview_stale`` 必然为 True——commit 前必须重新 preview。
        """
        from src.features.edit.ai_editing.models import EditPlan
        from src.features.edit.ai_editing.transaction_service import PreviewResult
        plan = EditPlan.from_dict(data["plan"])
        preview_obj = None
        preview_data = data.get("preview")
        if preview_data is not None:
            try:
                preview_obj = PreviewResult.from_dict(preview_data)
            except (KeyError, TypeError, ValueError):
                preview_obj = None
        return cls(
            plan=plan,
            canonical_json=data["canonical_json"].encode("utf-8"),
            content_hash=str(data["content_hash"]),
            created_at=float(data.get("created_at", 0.0)),
            source_variant_id=data.get("source_variant_id"),
            preview=preview_obj,
            preview_state_fingerprint=data.get("preview_state_fingerprint"),
        )


def state_fingerprint(
    *, project_revision: int | None, manifest_hash: str | None,
    timeline_revision: int | None,
) -> str:
    """构造状态指纹：把会影响 preview 有效性的维度拼成一个稳定字符串。

    任一维度变化（project revision、manifest、timeline revision）即认为旧 preview
    stale，必须重新 preview（总体不变量 §1.4）。
    """
    return _sha256(
        f"{project_revision}|{manifest_hash}|{timeline_revision}".encode("utf-8")
    )


class PlanRepository:
    """不可变 plan 仓库（进程内存；M4 接 payload 持久化）。

    语义：

    - ``store``：存入一份 plan（去重按 plan_id；同一 plan_id 重新存视为更新）。
    - ``get``：按 plan_id 取回不可变 plan（None = 不在仓库，apply 路径应中止）。
    - ``find_by_hash``：按 content_hash 取（用于校验"用户看到的那一份"）。
    - ``attach_preview``：把预览结果绑定到 plan，并记录其状态指纹。
    - ``is_preview_stale``：当前状态指纹与存储的不一致 → stale。

    线程模型 / 容量（AUD-14105）：全部读写经 ``RLock`` 保护；维护
    ``_by_hash`` 反向索引使 ``find_by_hash`` 为 O(1)（此前 O(n) 线性扫描）。
    仓库默认**无淘汰**（plan 只进不出，仅显式 remove/clear），因为 apply 路径
    随时可能按 plan_id/hash 回读历史 plan；调用方可传 ``max_plans`` 启用按
    ``created_at`` 的 LRU 淘汰来约束长会话内存。
    """

    def __init__(self, *, max_plans: int | None = None) -> None:
        self._by_id: dict[str, StoredPlan] = {}
        # AUD-14105：content_hash → [plan_id] 反向索引（插入序 = 存入序），
        # store/remove/clear/import_all 同步维护。
        self._by_hash: dict[str, list[str]] = {}
        self._lock = RLock()
        if max_plans is not None and int(max_plans) <= 0:
            raise ValueError("max_plans must be positive")
        self._max_plans = int(max_plans) if max_plans is not None else None

    def _index_plan_locked(self, plan_id: str, content_hash: str) -> None:
        """把 (plan_id, content_hash) 记入反向索引（持锁调用）。

        注意：若 plan_id 曾以不同 hash 入库，调用方必须先
        :meth:`_unindex_plan_locked` 旧 hash 再写入 ``_by_id``。
        """
        ids = self._by_hash.setdefault(content_hash, [])
        if plan_id not in ids:
            ids.append(plan_id)

    def _unindex_plan_locked(self, plan_id: str, content_hash: str) -> None:
        ids = self._by_hash.get(content_hash)
        if ids is None:
            return
        if plan_id in ids:
            ids.remove(plan_id)
        if not ids:
            self._by_hash.pop(content_hash, None)

    def _evict_locked(self) -> None:
        """超过 max_plans 时按 created_at 淘汰最旧 plan（持锁调用）。"""
        if self._max_plans is None:
            return
        excess = len(self._by_id) - self._max_plans
        if excess <= 0:
            return
        oldest = sorted(
            self._by_id.values(), key=lambda sp: (sp.created_at, sp.plan_id),
        )[:excess]
        for sp in oldest:
            self._by_id.pop(sp.plan_id, None)
            self._unindex_plan_locked(sp.plan_id, sp.content_hash)

    def store(
        self, plan: Any, *, source_variant_id: str | None = None,
        created_at: float | None = None,
    ) -> StoredPlan:
        """存入 plan，返回 ``StoredPlan``。同 plan_id 重新存会覆盖（保留 preview 绑定）。"""
        canonical = _canonical_json(plan)
        stored = StoredPlan(
            plan=plan,
            canonical_json=canonical,
            content_hash=_sha256(canonical),
            created_at=created_at if created_at is not None else time.time(),
            source_variant_id=source_variant_id,
        )
        with self._lock:
            # 保留旧的 preview 绑定（若内容 hash 相同）。
            old = self._by_id.get(plan.plan_id)
            if old is not None and old.content_hash == stored.content_hash:
                stored = StoredPlan(
                    plan=stored.plan,
                    canonical_json=stored.canonical_json,
                    content_hash=stored.content_hash,
                    created_at=stored.created_at,
                    source_variant_id=stored.source_variant_id,
                    preview=old.preview,
                    preview_state_fingerprint=old.preview_state_fingerprint,
                )
            if old is not None:
                self._unindex_plan_locked(plan.plan_id, old.content_hash)
            self._by_id[plan.plan_id] = stored
            self._index_plan_locked(plan.plan_id, stored.content_hash)
            self._evict_locked()
        return stored

    def get(self, plan_id: str) -> StoredPlan | None:
        with self._lock:
            return self._by_id.get(plan_id)

    def get_plan(self, plan_id: str) -> Any | None:
        """便捷：直接取 plan 对象（None = 不在仓库）。"""
        stored = self.get(plan_id)
        return stored.plan if stored is not None else None

    def find_by_hash(self, content_hash: str) -> StoredPlan | None:
        """按 content_hash 取最早存入的 plan（O(1) 反向索引，AUD-14105）。"""
        with self._lock:
            ids = self._by_hash.get(str(content_hash))
            if not ids:
                return None
            for plan_id in ids:
                sp = self._by_id.get(plan_id)
                if sp is not None and sp.content_hash == str(content_hash):
                    return sp
        return None

    def attach_preview(
        self, plan_id: str, preview: Any, *, state_fingerprint_value: str,
    ) -> bool:
        """把预览结果绑定到 plan，并记录状态指纹。返回是否绑定成功（plan 存在）。"""
        with self._lock:
            sp = self._by_id.get(plan_id)
            if sp is None:
                return False
            self._by_id[plan_id] = StoredPlan(
                plan=sp.plan,
                canonical_json=sp.canonical_json,
                content_hash=sp.content_hash,
                created_at=sp.created_at,
                source_variant_id=sp.source_variant_id,
                preview=preview,
                preview_state_fingerprint=state_fingerprint_value,
            )
            return True

    def is_preview_stale(
        self, plan_id: str, *, current_fingerprint: str,
    ) -> bool:
        """当前状态指纹与存储的不一致 → stale（需重新 preview）。

        plan 不在仓库、或未绑定 preview → 视为 stale（必须先 preview）。
        """
        with self._lock:
            sp = self._by_id.get(plan_id)
            if sp is None or sp.preview is None or sp.preview_state_fingerprint is None:
                return True
            return sp.preview_state_fingerprint != current_fingerprint

    def remove(self, plan_id: str) -> bool:
        with self._lock:
            sp = self._by_id.pop(plan_id, None)
            if sp is None:
                return False
            self._unindex_plan_locked(plan_id, sp.content_hash)
            return True

    def clear(self) -> None:
        with self._lock:
            self._by_id.clear()
            self._by_hash.clear()

    def count(self) -> int:
        with self._lock:
            return len(self._by_id)

    # ---------------- 持久化（M4：接 .topos payload）---------------- #

    def export_all(self) -> dict[str, Any]:
        """导出全部 StoredPlan 供 ``.topos`` 持久化。

        返回 ``{"schema_version": 1, "plans": [StoredPlan.to_dict(), ...]}``。
        纯只读、不持锁过序列化（快照一致性靠 GIL + 短临界区取列表）。
        """
        with self._lock:
            items = list(self._by_id.values())
        return {
            "schema_version": 1,
            "plans": [sp.to_dict() for sp in items],
        }

    def import_all(self, data: dict[str, Any]) -> int:
        """从 ``export_all`` 的输出恢复（工程重开后回填仓库）。

        返回成功导入的数量。已存在的 plan_id 会被覆盖（持久化版本视为权威）。
        无效条目（缺字段/格式错）跳过，不抛——避免单条损坏阻塞整个恢复。
        """
        plans_data = data.get("plans", []) if isinstance(data, dict) else []
        n = 0
        with self._lock:
            for pd in plans_data:
                if not isinstance(pd, dict) or "plan" not in pd:
                    continue
                try:
                    sp = StoredPlan.from_dict(pd)
                except (KeyError, TypeError, ValueError):
                    continue
                old_sp = self._by_id.get(sp.plan_id)
                if old_sp is not None:
                    self._unindex_plan_locked(sp.plan_id, old_sp.content_hash)
                self._by_id[sp.plan_id] = sp
                self._index_plan_locked(sp.plan_id, sp.content_hash)
                n += 1
            self._evict_locked()
        return n
