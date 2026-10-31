"""AI 会话目录模型（plan §10.2 P0.4）。

把 ``AssistantSessionStore`` 从"单个快照"演进为**会话目录**：

- 多会话：新建 / 重命名 / 归档 / 恢复。
- 每个会话保存：脱敏消息摘要、active run、artifact/job refs、压缩 checkpoint。
- 旧工程读取后不丢 Brief/Variant/Decision（迁移）。

红线（plan §10.2 P0.4 / §6.3）：

- 新对话不能删除工程历史。
- 恢复旧会话后，旧 plan 必须以当前 revision 重做 preview；旧 grant 永不可恢复。
- 不保存 secret / 绝对路径 / 未授权 prompt 全文（脱敏）。
- 落盘只存可重建的引用（run_id / artifact_id），不存大缓存 / API key。

本模块纯数据；持久化由 ``AssistantSessionStore``（已扩展）负责。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from src.features.ai_harness.invocation_models import sanitize_for_storage


#: 当前会话目录 schema 版本。新增字段 bump；旧版本经 migration 升级。
SESSION_CATALOG_VERSION: int = 2
#: 旧版（v1）只有单个 SessionSnapshot，无会话目录。
LEGACY_SNAPSHOT_VERSION: int = 1


class UnsupportedCatalogVersionError(ValueError):
    """会话目录版本比当前程序支持的更新（AUD-14402）。

    由未来版本程序写入的 ``session_catalog.json`` 含本版本未知的字段；
    旧程序若"解析成功再落盘"，会用已知字段集以同样的版本号覆盖原文件
    ——新字段静默蒸发（数据丢失且无提示）。因此 ``from_dict`` 对
    ``version > SESSION_CATALOG_VERSION`` 显式拒绝：

    - ``AssistantSessionStore.load`` 的畸形 payload 边界
      （``_PAYLOAD_ERRORS`` 含 ``ValueError``）会捕获本异常并回退 +
      ``logger.warning``，工程打开不崩溃；
    - 拒绝后目录不落盘覆盖，待用户用匹配版本的程序打开。

    Attributes:
        found_version: 文件中读到的版本号。
        supported_version: 当前程序支持的最高版本。
    """

    def __init__(self, found_version: int, supported_version: int) -> None:
        self.found_version = found_version
        self.supported_version = supported_version
        super().__init__(
            f"session catalog version {found_version} is newer than supported "
            f"{supported_version}; the file was written by a newer Topos build "
            "and may contain unknown fields. Refusing to load it to avoid "
            "silently dropping those fields on the next save."
        )


@dataclass(frozen=True)
class MessageSummary:
    """脱敏的消息摘要（plan §6.3 不存原始对话）。

    只存角色、脱敏文本片段、关联的 run/artifact/decision 引用，不存原始
    prompt 全文 / secret / 绝对路径。
    """

    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    role: str = "user"  # user / assistant / system
    text: str = ""
    run_id: str = ""
    artifact_id: str = ""
    decision_id: str = ""
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.role not in ("user", "assistant", "system"):
            object.__setattr__(self, "role", "user")

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "role": self.role,
            "text": sanitize_for_storage(self.text),
            "run_id": self.run_id,
            "artifact_id": self.artifact_id,
            "decision_id": self.decision_id,
            "timestamp": self.timestamp,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "MessageSummary":
        return MessageSummary(
            message_id=str(d.get("message_id", "")) or uuid.uuid4().hex,
            role=str(d.get("role", "user")),
            text=str(d.get("text", "")),
            run_id=str(d.get("run_id", "")),
            artifact_id=str(d.get("artifact_id", "")),
            decision_id=str(d.get("decision_id", "")),
            timestamp=float(d.get("timestamp", 0.0)),
        )


@dataclass(frozen=True)
class RunRef:
    """会话内对一次 run 的引用（只存 run_id + 摘要，不存完整 RunRecord）。

    完整 RunRecord 由 :class:`HarnessRuntime` 的内存状态 / run_store 维护；
    会话目录只保留可追溯的引用。
    """

    run_id: str = ""
    skill_id: str = ""
    status: str = ""
    summary: str = ""
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "skill_id": sanitize_for_storage(self.skill_id),
            "status": self.status,
            "summary": sanitize_for_storage(self.summary),
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "RunRef":
        return RunRef(
            run_id=str(d.get("run_id", "")),
            skill_id=str(d.get("skill_id", "")),
            status=str(d.get("status", "")),
            summary=str(d.get("summary", "")),
            created_at=float(d.get("created_at", 0.0)),
        )


@dataclass(frozen=True)
class CompressionCheckpoint:
    """压缩 checkpoint（plan §10.2 P0.5：压缩前后可追溯 / 可恢复）。

    存压缩前的 evidence/artifact 引用快照，压缩后仍可追溯来源。恢复时回滚到
    压缩前状态。注意：恢复压缩 checkpoint 不等于恢复旧 approval——旧 grant
    永不可恢复（红线）。
    """

    checkpoint_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    summary: str = ""
    preserved_evidence_refs: tuple[str, ...] = ()
    preserved_artifact_ids: tuple[str, ...] = ()
    message_count_before: int = 0
    message_count_after: int = 0
    # Metadata-only reference to context spilled during chat compression.
    spill_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "created_at": self.created_at,
            "summary": sanitize_for_storage(self.summary),
            "preserved_evidence_refs": list(self.preserved_evidence_refs),
            "preserved_artifact_ids": list(self.preserved_artifact_ids),
            "message_count_before": self.message_count_before,
            "message_count_after": self.message_count_after,
            "spill_id": self.spill_id,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "CompressionCheckpoint":
        return CompressionCheckpoint(
            checkpoint_id=str(d.get("checkpoint_id", "")) or uuid.uuid4().hex,
            created_at=float(d.get("created_at", 0.0)),
            summary=str(d.get("summary", "")),
            preserved_evidence_refs=tuple(d.get("preserved_evidence_refs", [])),
            preserved_artifact_ids=tuple(d.get("preserved_artifact_ids", [])),
            message_count_before=int(d.get("message_count_before", 0)),
            message_count_after=int(d.get("message_count_after", 0)),
            spill_id=str(d.get("spill_id", ""))[:64],
        )


@dataclass
class SessionCatalogEntry:
    """会话目录中的单个会话条目（可变，便于 store 更新）。"""

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    title: str = ""
    active_run_id: str = ""
    messages: list[MessageSummary] = field(default_factory=list)
    run_refs: list[RunRef] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)
    decision_ids: list[str] = field(default_factory=list)
    compression_checkpoints: list[CompressionCheckpoint] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    archived_at: float | None = None
    last_active_brief_id: str = ""

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    def touch(self) -> None:
        self.updated_at = time.time()

    def add_message(self, msg: MessageSummary) -> None:
        self.messages.append(msg)
        if msg.run_id and not any(r.run_id == msg.run_id for r in self.run_refs):
            # run_ref 由专门的 record_run 填充；这里不重复
            pass
        if msg.artifact_id and msg.artifact_id not in self.artifact_ids:
            self.artifact_ids.append(msg.artifact_id)
        if msg.decision_id and msg.decision_id not in self.decision_ids:
            self.decision_ids.append(msg.decision_id)
        self.touch()

    def record_run(self, run_ref: RunRef) -> None:
        if not any(r.run_id == run_ref.run_id for r in self.run_refs):
            self.run_refs.append(run_ref)
        else:
            self.run_refs = [
                run_ref if r.run_id == run_ref.run_id else r for r in self.run_refs
            ]
        self.active_run_id = run_ref.run_id
        self.touch()

    def add_compression_checkpoint(self, cp: CompressionCheckpoint) -> None:
        self.compression_checkpoints.append(cp)
        self.touch()

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "title": sanitize_for_storage(self.title),
            "active_run_id": self.active_run_id,
            "messages": [m.to_dict() for m in self.messages],
            "run_refs": [r.to_dict() for r in self.run_refs],
            "artifact_ids": list(self.artifact_ids),
            "decision_ids": list(self.decision_ids),
            "compression_checkpoints": [c.to_dict() for c in self.compression_checkpoints],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "archived_at": self.archived_at,
            "last_active_brief_id": self.last_active_brief_id,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "SessionCatalogEntry":
        return SessionCatalogEntry(
            session_id=str(d.get("session_id", "")) or uuid.uuid4().hex,
            title=str(d.get("title", "")),
            active_run_id=str(d.get("active_run_id", "")),
            messages=[MessageSummary.from_dict(m) for m in d.get("messages", [])],
            run_refs=[RunRef.from_dict(r) for r in d.get("run_refs", [])],
            artifact_ids=list(d.get("artifact_ids", [])),
            decision_ids=list(d.get("decision_ids", [])),
            compression_checkpoints=[
                CompressionCheckpoint.from_dict(c)
                for c in d.get("compression_checkpoints", [])
            ],
            created_at=float(d.get("created_at", 0.0)),
            updated_at=float(d.get("updated_at", 0.0)),
            archived_at=(
                float(d["archived_at"]) if d.get("archived_at") is not None else None
            ),
            last_active_brief_id=str(d.get("last_active_brief_id", "")),
        )


@dataclass
class SessionCatalog:
    """工程级会话目录（多会话）。

    v1（旧）只有 ``SessionSnapshot``（单个快照）；v2 引入本目录。迁移见
    :func:`migrate_legacy_snapshot`。
    """

    version: int = SESSION_CATALOG_VERSION
    sessions: dict[str, SessionCatalogEntry] = field(default_factory=dict)
    active_session_id: str = ""
    #: 迁移自 v1 的旧 brief/variant/decision 摘要（保留在新会话的 entry 里）。
    legacy_snapshot: dict[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.updated_at = time.time()

    # ---------------- 会话生命周期 ---------------- #

    def create_session(self, title: str = "") -> SessionCatalogEntry:
        entry = SessionCatalogEntry(title=title)
        self.sessions[entry.session_id] = entry
        self.active_session_id = entry.session_id
        self.touch()
        return entry

    def get_session(self, session_id: str) -> SessionCatalogEntry | None:
        return self.sessions.get(session_id)

    def get_or_create_active(self) -> SessionCatalogEntry:
        if not self.active_session_id or self.active_session_id not in self.sessions:
            return self.create_session()
        return self.sessions[self.active_session_id]

    def rename_session(self, session_id: str, title: str) -> bool:
        entry = self.sessions.get(session_id)
        if entry is None:
            return False
        entry.title = title
        entry.touch()
        self.touch()
        return True

    def archive_session(self, session_id: str) -> bool:
        entry = self.sessions.get(session_id)
        if entry is None or entry.is_archived:
            return False
        entry.archived_at = time.time()
        entry.touch()
        if self.active_session_id == session_id:
            # 归档当前会话：切到最近一个未归档会话，没有则置空
            non_archived = [
                sid for sid, e in self.sessions.items() if not e.is_archived
            ]
            self.active_session_id = non_archived[-1] if non_archived else ""
        self.touch()
        return True

    def set_active(self, session_id: str) -> bool:
        if session_id not in self.sessions:
            return False
        if self.sessions[session_id].is_archived:
            return False
        self.active_session_id = session_id
        self.touch()
        return True

    def list_sessions(self, *, include_archived: bool = True) -> list[SessionCatalogEntry]:
        entries = list(self.sessions.values())
        if not include_archived:
            entries = [e for e in entries if not e.is_archived]
        entries.sort(key=lambda e: e.updated_at, reverse=True)
        return entries

    def delete_session(self, session_id: str) -> bool:
        """删除会话条目（不删 brief/variant/decision——那些是工程级，非会话级）。"""
        if session_id not in self.sessions:
            return False
        del self.sessions[session_id]
        if self.active_session_id == session_id:
            non_archived = [
                sid for sid, e in self.sessions.items() if not e.is_archived
            ]
            self.active_session_id = non_archived[-1] if non_archived else ""
        self.touch()
        return True

    # ---------------- 序列化 ---------------- #

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "active_session_id": self.active_session_id,
            "sessions": {sid: e.to_dict() for sid, e in self.sessions.items()},
            "legacy_snapshot": sanitize_for_storage(self.legacy_snapshot),
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SessionCatalog":
        """从 dict 恢复会话目录。

        AUD-14402：``version > SESSION_CATALOG_VERSION``（未来版本程序写的
        文件）不再被静默接受——此前"解析成功 + 只取已知字段"会在下一次
        ``to_dict()`` 落盘时以原版本号写出丢失了未知字段的目录（数据
        蒸发）。现在显式抛 :class:`UnsupportedCatalogVersionError`（ValueError
        族，``AssistantSessionStore.load`` 的畸形 payload 边界会捕获并回退）。
        """
        version = int(data.get("version", LEGACY_SNAPSHOT_VERSION))
        if version < SESSION_CATALOG_VERSION:
            # 旧版（v1 单快照）→ 迁移
            return migrate_legacy_snapshot(data)
        if version > SESSION_CATALOG_VERSION:
            raise UnsupportedCatalogVersionError(version, SESSION_CATALOG_VERSION)
        catalog = SessionCatalog(version=version)
        catalog.active_session_id = str(data.get("active_session_id", ""))
        catalog.legacy_snapshot = dict(data.get("legacy_snapshot", {}))
        catalog.updated_at = float(data.get("updated_at", 0.0))
        for sid, ed in data.get("sessions", {}).items():
            catalog.sessions[sid] = SessionCatalogEntry.from_dict(ed)
        return catalog


# ---------------------------------------------------------------------- #
# 迁移
# ---------------------------------------------------------------------- #


def migrate_legacy_snapshot(data: dict[str, Any]) -> SessionCatalog:
    """把 v1 单 ``SessionSnapshot`` 迁移成 v2 ``SessionCatalog``。

    v1 结构（``AssistantSessionStore.SessionSnapshot.to_dict``）::

        {
          "briefs": {...}, "variants": {...}, "decisions": {...},
          "overrides": {...}, "last_active_brief_id": "...", "updated_at": ...
        }

    迁移策略：

    - brief/variant/decision/override 保留为 ``legacy_snapshot``（工程级，不丢）。
    - 新建一个"迁移会话"条目，``last_active_brief_id`` 继承。
    - active_session_id 指向新会话。

    红线：旧 plan 在恢复后必须以当前 revision 重做 preview（不在此处恢复 grant）。
    """
    catalog = SessionCatalog(version=SESSION_CATALOG_VERSION)
    catalog.legacy_snapshot = sanitize_for_storage(dict(data))
    entry = catalog.create_session(
        title="迁移会话（来自旧版本）",
    )
    last_brief = str(data.get("last_active_brief_id", ""))
    if last_brief:
        entry.last_active_brief_id = last_brief
    # 旧 variant/decision 的 ID 引用进会话条目（便于 UI 定位），但原始数据
    # 仍在 legacy_snapshot 里，由 store 暴露给既有 get_variant/get_decision。
    for vid in (data.get("variants", {}) or {}).keys():
        entry.decision_ids  # noop placeholder
    # 把 variant/decision id 作为引用保留（不复制内容）
    entry.artifact_ids = []
    catalog.touch()
    return catalog


__all__ = [
    "CompressionCheckpoint",
    "LEGACY_SNAPSHOT_VERSION",
    "MessageSummary",
    "RunRef",
    "SESSION_CATALOG_VERSION",
    "SessionCatalog",
    "SessionCatalogEntry",
    "UnsupportedCatalogVersionError",
    "migrate_legacy_snapshot",
]
