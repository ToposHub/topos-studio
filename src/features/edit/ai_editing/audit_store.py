"""TAEP Audit Store（Phase 6）。

可选的工程级 AI 审计存储（TAEP §6.3）。保存 plan/revision/diagnostics 摘要，
**默认不**保存原始对话、API key、本机绝对路径或素材内容。

支持 audit/replay（TAEP §12 P6）：用相同 snapshot、manifest、plan 重放到副本工程，
便于 bug report。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.features.ai_harness.invocation_models import sanitize_for_storage
from src.features.edit.ai_editing.safe_path_policy import SafePathPolicy, safe_filename


@dataclass
class AuditEntry:
    """单条审计记录。"""

    timestamp: float
    tool: str
    plan_id: str
    applied: bool
    new_revision: int | None
    client: str = ""
    diagnostics_summary: dict[str, int] = field(default_factory=dict)
    # 可选：用于 replay 的 plan 快照（不含 secret）
    plan_snapshot: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "timestamp": self.timestamp,
            "tool": self.tool,
            "plan_id": self.plan_id,
            "applied": self.applied,
            "new_revision": self.new_revision,
            "client": self.client,
            "diagnostics_summary": dict(self.diagnostics_summary),
        }
        if self.plan_snapshot is not None:
            d["plan_snapshot"] = self.plan_snapshot
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuditEntry":
        """Restore one sanitized entry from a project payload."""
        if not isinstance(data, dict):
            raise ValueError("audit entry must be an object")
        diagnostics = data.get("diagnostics_summary", {})
        if not isinstance(diagnostics, dict):
            diagnostics = {}
        snapshot = data.get("plan_snapshot")
        if snapshot is not None and not isinstance(snapshot, dict):
            snapshot = None
        revision = data.get("new_revision")
        return cls(
            timestamp=float(data.get("timestamp", 0.0) or 0.0),
            tool=str(data.get("tool", ""))[:160],
            plan_id=str(data.get("plan_id", ""))[:256],
            applied=bool(data.get("applied", False)),
            new_revision=int(revision) if revision is not None else None,
            client=str(data.get("client", ""))[:160],
            diagnostics_summary={
                str(key)[:120]: int(value)
                for key, value in diagnostics.items()
                if isinstance(value, (int, float))
            },
            plan_snapshot=sanitize_for_storage(snapshot),
        )


class AuditStore:
    """审计存储：内存 + 可选落盘（``ai/audit/<plan_id>.json``）。

    线程模型（AUD-13100）：与同包 ``AutonomousMissionStore`` /
    ``AutonomousCheckpointStore`` 一致，一把 ``threading.RLock`` 覆盖全部内存
    读写路径（record/list_entries/get/clear/export_all/import_all），
    落盘在锁外执行——record 的磁盘 IO 不阻塞其他查询线程。

    隐私策略（TAEP §6.3）：
    - 默认不保存原始对话、prompt、API key、本机绝对路径、素材内容。
    - 需要可复现时，由用户选择保存 plan/revision/diagnostics hash。
    - 落盘路径必须由宿主授权（不自行遍历文件系统）。
    """

    def __init__(
        self, sink_dir: str | Path | None = None, *, max_entries: int = 256,
    ) -> None:
        self._entries: list[AuditEntry] = []
        self._max_entries = max(1, int(max_entries))
        self._sink_dir = Path(sink_dir) if sink_dir else None
        # M0.4：落盘路径统一经 SafePathPolicy（授权根 = sink_dir），杜绝路径穿越 /
        # symlink 跳转 / 相对路径逃逸。
        self._path_policy: SafePathPolicy | None = (
            SafePathPolicy(self._sink_dir) if self._sink_dir is not None else None
        )
        # AUD-13100：与同包 mission/checkpoint store 的线程安全约定保持一致。
        self._lock = threading.RLock()

    def record(self, entry: AuditEntry) -> None:
        # 内存记录始终成功（即使落盘失败也保留内存条目，供 UI/审计查询）。
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > self._max_entries:
                del self._entries[:-self._max_entries]
        if self._sink_dir is None or self._path_policy is None:
            return
        try:
            self._sink_dir.mkdir(parents=True, exist_ok=True)
            # M0.4：plan_id 经 safe_filename 规范化为不含路径语义的 basename，
            # 再经 SafePathPolicy.resolve_within 断言仍在 sink_dir 内。
            fname = safe_filename(entry.plan_id) + ".json"
            fpath = self._path_policy.resolve_within(fname)
            # 不覆盖已存在的（避免抹掉历史）
            if not fpath.exists():
                fpath.write_text(
                    json.dumps(entry.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        except (OSError, ValueError) as exc:
            # 审计 I/O 失败不能让已成功的编辑被误报为失败（审查 M0.4 / §6.3）。
            # 降级为 warning，内存条目仍保留。
            logging.getLogger(__name__).warning(
                "audit write failed for plan %r: %s", entry.plan_id, exc
            )
        except Exception as exc:  # PathPolicyError 等：路径越权
            logging.getLogger(__name__).warning(
                "audit path rejected for plan %r: %s", entry.plan_id, exc
            )

    def list_entries(self) -> list[AuditEntry]:
        with self._lock:
            return list(self._entries)

    def get(self, plan_id: str) -> AuditEntry | None:
        with self._lock:
            for e in reversed(self._entries):
                if e.plan_id == plan_id:
                    return e
            return None

    def replay_context(self, plan_id: str) -> dict[str, Any] | None:
        """取某 plan 的 replay 上下文（snapshot/manifest/plan）。

        TAEP §12 P6：用相同上下文重放到副本工程，便于 bug report。
        仅当记录时保存了 plan_snapshot 才可用。
        """
        entry = self.get(plan_id)
        if entry is None or entry.plan_snapshot is None:
            return None
        return {"plan": entry.plan_snapshot, "client": entry.client}

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def export_all(self) -> dict[str, Any] | None:
        """Export a bounded, sanitized audit history for project persistence."""
        # AUD-13100：锁内取快照、锁外序列化，避免并发 record 时迭代被改写。
        with self._lock:
            snapshot = list(self._entries)
        if not snapshot:
            return None
        return {
            "schema_version": 1,
            "entries": [sanitize_for_storage(entry.to_dict()) for entry in snapshot],
        }

    def import_all(self, data: Any) -> int:
        """Restore valid entries; malformed rows never block project loading."""
        if not isinstance(data, dict) or int(data.get("schema_version", 0) or 0) != 1:
            return 0
        rows = data.get("entries", ())
        if not isinstance(rows, (list, tuple)):
            return 0
        restored = 0
        for raw in rows[: self._max_entries]:
            try:
                self.record(AuditEntry.from_dict(raw))
            except (TypeError, ValueError, KeyError):
                continue
            restored += 1
        return restored
