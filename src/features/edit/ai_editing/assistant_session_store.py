"""TAEP Assistant Session Store（Phase 9 UI-6，补齐 §11.8；plan §10.2 P0.4 演进）。

可选的工程本地 brief/variant/decision 持久化（TAEP §11.8 / §6.3）。

设计（TAEP §6.3 / §11.9 UI-6）：

- 默认**不**保存原始对话、API key、本机绝对路径或素材内容。
- 需要可复现时，由用户选择保存 brief/variant 摘要、decision、audit refs。
- 作为工程包的可选 payload（``ai/`` 子目录），不旁路项目保存。
- 会话恢复：重开工程后可恢复 brief、variant 摘要、decision、audit refs。

plan §10.2 P0.4 演进（会话目录）：

- 在保留旧 ``SessionSnapshot``（v1 单快照）API 的基础上，新增 **会话目录**
  （``SessionCatalog``，v2）：新建 / 重命名 / 归档 / 恢复多会话。
- 每个会话保存：脱敏消息摘要、active run、artifact/job refs、压缩 checkpoint。
- 旧工程读取后不丢 Brief/Variant/Decision（:func:`migrate_legacy_snapshot`）。
- 落盘文件分两个：``ai/session.json``（v1 旧快照，向后兼容）与
  ``ai/session_catalog.json``（v2 目录）。新工程只写 v2；读取时 v1 自动迁移。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.features.edit.ai_editing.editorial_models import (
    EditDecision,
    EditVariant,
    EditorialBrief,
    UserOverride,
)
from src.features.edit.ai_editing.session_models import (
    CompressionCheckpoint,
    MessageSummary,
    RunRef,
    SessionCatalog,
    SessionCatalogEntry,
)

logger = logging.getLogger(__name__)

#: 工程 payload 解析失败时的兜底捕获集（AUD-13018）。
#: ``SessionSnapshot/SessionCatalog/SessionCatalogEntry.from_dict`` 对畸形
#: 字段可抛 TypeError/AttributeError/KeyError/ValueError——全部按"缓存损坏"
#: 处理，绝不阻断工程打开。
_PAYLOAD_ERRORS = (
    OSError, json.JSONDecodeError, ValueError, TypeError, AttributeError, KeyError,
)


@dataclass
class SessionSnapshot:
    """单个工程的 AI 会话快照（可序列化）。"""

    briefs: dict[str, dict[str, Any]] = field(default_factory=dict)
    variants: dict[str, dict[str, Any]] = field(default_factory=dict)
    decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_active_brief_id: str = ""
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "briefs": dict(self.briefs),
            "variants": dict(self.variants),
            "decisions": dict(self.decisions),
            "overrides": dict(self.overrides),
            "last_active_brief_id": self.last_active_brief_id,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SessionSnapshot":
        return SessionSnapshot(
            briefs=dict(data.get("briefs", {})),
            variants=dict(data.get("variants", {})),
            decisions=dict(data.get("decisions", {})),
            overrides=dict(data.get("overrides", {})),
            last_active_brief_id=str(data.get("last_active_brief_id", "")),
            updated_at=float(data.get("updated_at", 0.0)),
        )


class AssistantSessionStore:
    """工程本地 AI 会话存储。

    落盘到工程包 ``ai/session.json``（由调用方提供目录），并将脱敏的短消息摘要
    自动写入用户本机缓存（最多 50 个会话）。不保存 secret/绝对路径/素材内容。
    """

    DEFAULT_CACHE_LIMIT = 50

    def __init__(
        self,
        *,
        cache_path: str | Path | None = None,
        cache_limit: int = DEFAULT_CACHE_LIMIT,
    ) -> None:
        self._snapshot = SessionSnapshot()
        # plan §10.2 P0.4：会话目录（v2）。默认空；load 时按文件存在与否填充。
        self._catalog = SessionCatalog()
        # 对话历史是用户体验数据，不能只依赖“保存工程”才落盘。这里保留一份
        # 本机轻量缓存；工程 payload 仍是权威的项目内备份，缓存只保存脱敏摘要。
        # 允许注入路径，便于测试和便携版部署。
        default_cache = Path.home() / ".topos_cache" / "ai_assistant_history.json"
        configured_cache = os.environ.get("TOPOS_ASSISTANT_CACHE_PATH", "").strip()
        self._cache_path = Path(cache_path or configured_cache or default_cache)
        self._cache_limit = max(1, int(cache_limit))
        self._cache_sessions: dict[str, SessionCatalogEntry] = {}
        self._load_cache()

    # ---------------- 写入 ---------------- #

    def save_brief(self, brief: EditorialBrief) -> None:
        self._snapshot.briefs[brief.brief_id] = brief.to_dict()
        self._snapshot.last_active_brief_id = brief.brief_id
        self._touch()

    def save_variant(self, variant: EditVariant) -> None:
        self._snapshot.variants[variant.variant_id] = variant.to_dict()
        self._touch()

    def save_decision(self, decision: EditDecision) -> None:
        self._snapshot.decisions[decision.decision_id] = decision.to_dict()
        self._touch()

    def record_override(self, override: UserOverride) -> None:
        self._snapshot.overrides[override.override_id] = override.to_dict()
        self._touch()

    def clear_variant(self, variant_id: str) -> None:
        self._snapshot.variants.pop(variant_id, None)
        self._touch()

    # ---------------- 读取 ---------------- #

    def get_brief(self, brief_id: str) -> EditorialBrief | None:
        d = self._snapshot.briefs.get(brief_id)
        return EditorialBrief.from_dict(d) if d else None

    def get_variant(self, variant_id: str) -> EditVariant | None:
        d = self._snapshot.variants.get(variant_id)
        return EditVariant.from_dict(d) if d else None

    def list_variants(self) -> list[EditVariant]:
        return [EditVariant.from_dict(d) for d in self._snapshot.variants.values()]

    def get_decision(self, decision_id: str) -> EditDecision | None:
        d = self._snapshot.decisions.get(decision_id)
        return EditDecision.from_dict(d) if d else None

    def list_decisions(self) -> list[EditDecision]:
        return [EditDecision.from_dict(d) for d in self._snapshot.decisions.values()]

    @property
    def snapshot(self) -> SessionSnapshot:
        return self._snapshot

    # ---------------- 持久化 ---------------- #

    def persist(self, target_dir: str | Path) -> bool:
        """落盘到 ``target_dir/ai/session.json``（v1）+ ``session_catalog.json``（v2）。

        v1 文件保留以便旧版本 Topos 读取（向后兼容，不丢 brief/variant/decision）；
        v2 文件是会话目录的真实来源。两者都写失败才返回 False。
        """
        ai_dir = Path(target_dir) / "ai"
        # AUD-13017：mkdir（只读/不可写工程目录会抛 PermissionError/OSError）
        # 纳入异常保护——契约是返回 False，且仍尽力保存本机历史缓存。
        try:
            ai_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning(
                "cannot create project ai/ dir %s; session persist skipped",
                ai_dir, exc_info=True,
            )
            self._save_cache()
            return False
        self._snapshot.updated_at = time.time()
        self._catalog.touch()
        ok1 = True
        try:
            (ai_dir / "session.json").write_text(
                json.dumps(self._snapshot.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            ok1 = False
        ok2 = True
        try:
            (ai_dir / "session_catalog.json").write_text(
                json.dumps(self._catalog.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            ok2 = False
        # 即使工程目录只读，也尽力保存本机历史缓存；工程保存结果仍由两个
        # 工程文件的写入结果决定，避免把缓存权限问题伪装成工程保存失败。
        self._save_cache()
        return ok1 and ok2

    def load(self, target_dir: str | Path) -> bool:
        """从 ``target_dir/ai/`` 恢复。

        优先读 v2 ``session_catalog.json``；不存在则读 v1 ``session.json`` 并迁移。
        两者都不存在返回 False（非错误）。
        """
        ai_dir = Path(target_dir) / "ai"
        catalog_path = ai_dir / "session_catalog.json"
        snapshot_path = ai_dir / "session.json"

        # 1. v2 目录优先
        if catalog_path.exists():
            try:
                data = json.loads(catalog_path.read_text(encoding="utf-8"))
                self._catalog = SessionCatalog.from_dict(data)
                self._sync_catalog_to_cache()
                # v1 快照也尽量同步加载（catalog.legacy_snapshot 可能含旧 brief）
                if snapshot_path.exists():
                    try:
                        snap = json.loads(snapshot_path.read_text(encoding="utf-8"))
                        self._snapshot = SessionSnapshot.from_dict(snap)
                    except _PAYLOAD_ERRORS:
                        logger.warning(
                            "malformed legacy session.json under %s; brief/"
                            "variant snapshot skipped", ai_dir, exc_info=True,
                        )
                return True
            except _PAYLOAD_ERRORS:
                # AUD-13018：损坏的 ai/*.json（标量 briefs、非 dict messages 等
                # 触发 TypeError/AttributeError/KeyError）不得使工程打开崩溃——
                # 记日志后落到 v1 路径再兜底。
                logger.warning(
                    "malformed session_catalog.json under %s; falling back to "
                    "legacy snapshot", ai_dir, exc_info=True,
                )
                self._catalog = SessionCatalog()

        # 2. v1 单快照（迁移到 v2）
        if snapshot_path.exists():
            try:
                data = json.loads(snapshot_path.read_text(encoding="utf-8"))
                self._snapshot = SessionSnapshot.from_dict(data)
                # 迁移：把 v1 数据转成 v2 目录（保留 legacy_snapshot）
                from src.features.edit.ai_editing.session_models import (
                    migrate_legacy_snapshot,
                )
                self._catalog = migrate_legacy_snapshot(data)
                self._sync_catalog_to_cache()
                return True
            except _PAYLOAD_ERRORS:
                logger.warning(
                    "malformed session.json under %s; session history not "
                    "restored", ai_dir, exc_info=True,
                )
                return False
        return False

    def _touch(self) -> None:
        self._snapshot.updated_at = time.time()
        self._catalog.touch()
        self._sync_catalog_to_cache()

    # ================================================================ #
    # 本机历史缓存（最多保留最近 50 个会话）
    # ================================================================ #

    def _load_cache(self) -> None:
        """加载本机缓存；缓存损坏时静默回到空目录，不影响打开工程。"""
        try:
            if not self._cache_path.exists():
                return
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
            sessions = data.get("sessions", {}) if isinstance(data, dict) else {}
            if not isinstance(sessions, dict):
                return
            for session_id, raw in sessions.items():
                if not isinstance(raw, dict):
                    continue
                entry = SessionCatalogEntry.from_dict(raw)
                self._cache_sessions[str(session_id)] = entry
            self._trim_cache()
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            self._cache_sessions.clear()

    def _merge_cache_from_disk(self) -> None:
        """AUD-13020：写前把磁盘上其它实例新增的会话合并进内存。

        本机缓存是跨实例共享的单文件；各实例构造时各自持有内存快照，直接整写
        会 last-writer-wins 丢会话。这里只吸收**内存中不存在**的 session_id；
        冲突（同 id）以本实例为准（本实例刚执行过用户操作，数据较新）。
        """
        try:
            if not self._cache_path.exists():
                return
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return
        sessions = data.get("sessions", {}) if isinstance(data, dict) else {}
        if not isinstance(sessions, dict):
            return
        merged = 0
        for session_id, raw in sessions.items():
            key = str(session_id)
            if key in self._cache_sessions:
                continue
            if not isinstance(raw, dict):
                continue
            try:
                self._cache_sessions[key] = SessionCatalogEntry.from_dict(raw)
                merged += 1
            except _PAYLOAD_ERRORS:
                continue
        if merged:
            logger.debug("merged %d foreign cache session(s) from disk", merged)

    def _save_cache(self, *, merge: bool = True) -> None:
        """原子替换本机缓存文件，失败不阻塞正常对话。

        AUD-13020：

        - ``merge=True``（默认）时先重读磁盘合并其它实例新增的会话，避免多实例
          并发整写互相覆盖（last-writer-wins 丢会话）；``clear_history`` 用
          ``merge=False``——清空语义必须胜出，不复活磁盘上的旧会话；
        - 临时文件改为 ``tempfile.mkstemp`` 唯一名 + ``os.replace``，两个写者
          不再交叉写同一固定 ``.tmp`` 文件。
        """
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            if merge:
                self._merge_cache_from_disk()
            self._trim_cache()
            payload = json.dumps(
                {"version": 1, "sessions": {
                    sid: entry.to_dict()
                    for sid, entry in self._cache_sessions.items()
                }},
                ensure_ascii=False,
                indent=2,
            )
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{self._cache_path.name}.", suffix=".tmp",
                dir=self._cache_path.parent,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, self._cache_path)
                temp_name = ""
            finally:
                if temp_name:
                    try:
                        os.remove(temp_name)
                    except OSError:
                        pass
        except OSError as e:
            logger.warning("assistant session cache write failed: %s", e)

    def clear_history(self) -> None:
        """清空本机缓存与当前工程会话目录。

        Brief/variant/decision 属于工程级编辑状态，不是对话历史，因此保留；
        这里只清理会话、消息摘要、运行引用和压缩检查点。
        """
        self._cache_sessions.clear()
        self._catalog.sessions.clear()
        self._catalog.active_session_id = ""
        self._catalog.touch()
        # 即使为空也写回空对象，避免旧 JSON 在下一次启动时复活。
        # merge=False：清空语义必须胜出，不把磁盘上其它实例的会话合并回来。
        self._save_cache(merge=False)

    def _trim_cache(self) -> None:
        """按更新时间保留最近 ``cache_limit`` 个会话，覆盖最旧版本。"""
        ordered = sorted(
            self._cache_sessions.items(),
            key=lambda item: item[1].updated_at,
            reverse=True,
        )
        self._cache_sessions = dict(ordered[: self._cache_limit])

    def _sync_catalog_to_cache(self) -> None:
        """同步当前工程目录的非空会话到本机缓存。"""
        changed = False
        for session_id, entry in self._catalog.sessions.items():
            # 空会话不占用历史名额；第一条消息进入后才成为可恢复历史。
            if not entry.messages and not entry.run_refs:
                continue
            self._cache_sessions[session_id] = SessionCatalogEntry.from_dict(
                entry.to_dict()
            )
            changed = True
        if changed:
            self._trim_cache()
            self._save_cache()

    def set_cache_limit(self, limit: int) -> None:
        """更新缓存上限并立即淘汰最旧会话。"""
        self._cache_limit = max(1, int(limit))
        self._trim_cache()
        self._save_cache()

    @property
    def cache_limit(self) -> int:
        return self._cache_limit

    def restore_catalog(self, data: dict[str, Any]) -> None:
        """恢复工程 payload，同时保留本机缓存中的其它历史会话。

        AUD-13018：损坏的 payload（from_dict 对畸形字段可抛
        TypeError/AttributeError/KeyError）不得使工程打开崩溃——记日志并回到
        空目录（与 ``_load_cache`` 的"缓存损坏 → 空目录"语义一致）。
        """
        try:
            self._catalog = SessionCatalog.from_dict(data)
        except _PAYLOAD_ERRORS:
            logger.warning(
                "malformed session catalog payload; starting with an empty "
                "catalog", exc_info=True,
            )
            self._catalog = SessionCatalog()
        self._sync_catalog_to_cache()

    # ================================================================ #
    # plan §10.2 P0.4：会话目录 API（新增；不动旧 brief/variant API）
    # ================================================================ #

    @property
    def catalog(self) -> SessionCatalog:
        """会话目录（v2）。"""
        return self._catalog

    def create_session(self, title: str = "") -> SessionCatalogEntry:
        """新建会话并设为 active。不删除任何历史（红线 §10.2 P0.4）。"""
        return self._catalog.create_session(title)

    def get_session(self, session_id: str) -> SessionCatalogEntry | None:
        return self._catalog.get_session(session_id) or self._cache_sessions.get(session_id)

    def get_active_session(self) -> SessionCatalogEntry:
        """返回当前会话；若目录为空则自动新建一个。"""
        return self._catalog.get_or_create_active()

    def rename_session(self, session_id: str, title: str) -> bool:
        ok = self._catalog.rename_session(session_id, title)
        if ok:
            self._sync_catalog_to_cache()
        return ok

    def set_session_title(self, session_id: str, title: str) -> bool:
        """设置会话标题，并同步缓存；空标题由调用方决定是否忽略。

        AUD-13019：仅存在于本机缓存的会话（跨工程恢复的历史）改名后必须落盘——
        ``_sync_catalog_to_cache`` 只在 catalog 有非空会话时才写缓存，缓存-only
        条目的变更会被跳过，因此这里对 cache-only 会话直接 ``_save_cache()``。
        """
        entry = self._catalog.get_session(session_id)
        if entry is None:
            entry = self._cache_sessions.get(session_id)
        if entry is None:
            return False
        entry.title = title.strip()
        entry.touch()
        in_catalog = session_id in self._catalog.sessions
        if in_catalog:
            self._catalog.touch()
        self._sync_catalog_to_cache()
        if not in_catalog and session_id in self._cache_sessions:
            # cache-only 会话：catalog 联动不会触发写盘，必须显式落盘，
            # 否则返回 True 但重启即丢（用户数据丢失）。
            self._save_cache()
        return True

    def archive_session(self, session_id: str) -> bool:
        ok = self._catalog.archive_session(session_id)
        if ok:
            self._sync_catalog_to_cache()
        return ok

    def set_active_session(self, session_id: str) -> bool:
        """切换会话。归档的会话不能设为 active（需先恢复）。"""
        if session_id not in self._catalog.sessions and session_id in self._cache_sessions:
            # 缓存会话切换回当前工程时复制一份；原缓存保留，便于跨工程恢复。
            self._catalog.sessions[session_id] = SessionCatalogEntry.from_dict(
                self._cache_sessions[session_id].to_dict()
            )
        ok = self._catalog.set_active(session_id)
        if ok:
            self._sync_catalog_to_cache()
        return ok

    def list_sessions(self, *, include_archived: bool = True) -> list[SessionCatalogEntry]:
        # 历史菜单只展示当前工程目录。全局缓存仍用于灾备/跨次启动，但不能
        # 与其它工程混在一起，否则用户会看到不属于当前工作区的会话。
        values = list(self._catalog.sessions.values())
        if not include_archived:
            values = [entry for entry in values if not entry.is_archived]
        values.sort(key=lambda entry: entry.updated_at, reverse=True)
        return values

    # ---------------- 会话内容（消息 / run / artifact 引用）---------------- #

    def add_message_summary(self, msg: MessageSummary, *, session_id: str | None = None) -> bool:
        """把一条脱敏消息摘要加进会话。``session_id`` 省略时用 active 会话。"""
        entry = (
            self._catalog.get_session(session_id) if session_id
            else self._catalog.get_or_create_active()
        )
        if entry is None:
            return False
        entry.add_message(msg)
        # 会话标题只在第一条用户消息到达时自动生成；后续多轮消息继续
        # 追加到同一个 active session，不会再创建“每条消息一个历史”。
        if msg.role == "user" and not entry.title.strip() and msg.text.strip():
            entry.title = " ".join(msg.text.split())[:42]
            entry.touch()
        # AUD-13021: _touch() 内已含 _sync_catalog_to_cache()，不再重复同步
        self._touch()
        return True

    def record_run(self, run_ref: RunRef, *, session_id: str | None = None) -> bool:
        """把一次 run 的引用登记进会话（active_run_id 同步更新）。"""
        entry = (
            self._catalog.get_session(session_id) if session_id
            else self._catalog.get_or_create_active()
        )
        if entry is None:
            return False
        entry.record_run(run_ref)
        # AUD-13021: _touch() 内已含 _sync_catalog_to_cache()，不再重复同步
        self._touch()
        return True

    def add_compression_checkpoint(
        self, cp: CompressionCheckpoint, *, session_id: str | None = None,
    ) -> bool:
        """记录一次上下文压缩的 checkpoint（plan §10.2 P0.5：可恢复压缩前）。"""
        entry = (
            self._catalog.get_session(session_id) if session_id
            else self._catalog.get_or_create_active()
        )
        if entry is None:
            return False
        entry.add_compression_checkpoint(cp)
        # AUD-13021: _touch() 内已含 _sync_catalog_to_cache()，不再重复同步
        self._touch()
        return True
