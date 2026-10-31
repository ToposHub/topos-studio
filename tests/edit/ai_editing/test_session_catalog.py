"""AssistantSessionStore 会话目录测试（plan §10.2 P0.4）。

覆盖：

- ``SessionCatalog``：新建 / 重命名 / 归档 / 切换 / 列表。
- ``SessionCatalogEntry``：add_message / record_run / compression checkpoint。
- ``MessageSummary`` 脱敏（不存 secret / 绝对路径）。
- 持久化 roundtrip：persist 写 v1 + v2；load 优先 v2。
- v1 迁移：旧 ``SessionSnapshot`` → v2 catalog，brief 不丢。
- 红线：新对话不删历史；归档会话不能设 active；旧 grant 不恢复（数据层无 grant）。

红线对照（plan §10.2 P0.4 / §6.3）：

- 不保存 secret / 绝对路径 / 未授权 prompt 全文。
- 旧工程读取后不丢 Brief/Variant/Decision（迁移）。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.assistant_session_store import (
    AssistantSessionStore,
    SessionSnapshot,
)
from src.features.edit.ai_editing.session_models import (
    CompressionCheckpoint,
    LEGACY_SNAPSHOT_VERSION,
    MessageSummary,
    RunRef,
    SESSION_CATALOG_VERSION,
    SessionCatalog,
    SessionCatalogEntry,
    migrate_legacy_snapshot,
)


# ================================================================
# SessionCatalogEntry
# ================================================================


class TestSessionCatalogEntry:
    def test_add_message_tracks_refs(self):
        e = SessionCatalogEntry(title="s")
        e.add_message(MessageSummary(role="user", text="hi", run_id="r1", artifact_id="a1"))
        assert len(e.messages) == 1
        assert e.active_run_id == ""  # active_run 由 record_run 设
        assert "a1" in e.artifact_ids

    def test_record_run_sets_active(self):
        e = SessionCatalogEntry()
        e.record_run(RunRef(run_id="r1", status="running"))
        assert e.active_run_id == "r1"
        # 重复 record 同 run 不重复
        e.record_run(RunRef(run_id="r1", status="succeeded"))
        assert len(e.run_refs) == 1
        assert e.run_refs[0].status == "succeeded"  # 更新

    def test_record_run_distinct(self):
        e = SessionCatalogEntry()
        e.record_run(RunRef(run_id="r1"))
        e.record_run(RunRef(run_id="r2"))
        assert len(e.run_refs) == 2
        assert e.active_run_id == "r2"

    def test_compression_checkpoint(self):
        e = SessionCatalogEntry()
        cp = CompressionCheckpoint(
            summary="压缩到 50%",
            preserved_evidence_refs=("e1",),
            preserved_artifact_ids=("a1",),
            message_count_before=10,
            message_count_after=5,
        )
        e.add_compression_checkpoint(cp)
        assert len(e.compression_checkpoints) == 1

    def test_to_dict_sanitizes_text(self):
        e = SessionCatalogEntry(title="/Users/testuser/secret")
        e.add_message(MessageSummary(text="key=sk-1234567890abcdef"))
        d = e.to_dict()
        assert "/Users/testuser" not in json.dumps(d)
        assert "sk-1234567890abcdef" not in json.dumps(d)


# ================================================================
# SessionCatalog 生命周期
# ================================================================


class TestSessionCatalog:
    def test_create_and_active(self):
        c = SessionCatalog()
        e1 = c.create_session("first")
        assert c.active_session_id == e1.session_id
        assert c.get_or_create_active() is e1

    def test_rename(self):
        c = SessionCatalog()
        e = c.create_session("old")
        assert c.rename_session(e.session_id, "new") is True
        assert c.get_session(e.session_id).title == "new"

    def test_archive_clears_active(self):
        c = SessionCatalog()
        e1 = c.create_session("a")
        e2 = c.create_session("b")
        # active 是 e2
        assert c.active_session_id == e2.session_id
        c.archive_session(e2.session_id)
        # 归档当前会话后切到 e1（最近未归档）
        assert c.active_session_id == e1.session_id
        assert c.get_session(e2.session_id).is_archived

    def test_archived_cannot_be_active(self):
        c = SessionCatalog()
        e = c.create_session("a")
        c.archive_session(e.session_id)
        assert c.set_active(e.session_id) is False

    def test_list_sessions_excludes_archived(self):
        c = SessionCatalog()
        e1 = c.create_session("a")
        e2 = c.create_session("b")
        c.archive_session(e1.session_id)
        active_only = c.list_sessions(include_archived=False)
        assert [e.session_id for e in active_only] == [e2.session_id]

    def test_delete_session(self):
        c = SessionCatalog()
        e = c.create_session("a")
        assert c.delete_session(e.session_id) is True
        assert c.get_session(e.session_id) is None

    def test_list_sessions_sorted_by_updated_desc(self):
        c = SessionCatalog()
        e1 = c.create_session("a")
        e2 = c.create_session("b")
        # touch e1 later → 它应排前面
        e1.touch()
        ordered = [e.session_id for e in c.list_sessions()]
        assert ordered == [e1.session_id, e2.session_id]


# ================================================================
# MessageSummary 脱敏
# ================================================================


class TestMessageSummarySanitization:
    def test_redacts_secret_in_text(self):
        m = MessageSummary(text="my api_key=sk-1234567890abcdefghijklmnop")
        d = m.to_dict()
        assert "sk-1234567890abcdefghijklmnop" not in json.dumps(d)

    def test_redacts_absolute_path(self):
        m = MessageSummary(text="open /Users/testuser/secret/file.mov")
        d = m.to_dict()
        assert "/Users/testuser" not in json.dumps(d)

    def test_role_validated(self):
        m = MessageSummary(role="evil")
        assert m.role == "user"  # 未知 role 回退 user


# ================================================================
# 迁移
# ================================================================


class TestMigration:
    def test_migrate_v1_snapshot(self):
        v1 = {
            "briefs": {"b1": {"brief_id": "b1", "revision": 1}},
            "variants": {"v1": {"variant_id": "v1"}},
            "decisions": {"d1": {"decision_id": "d1"}},
            "overrides": {},
            "last_active_brief_id": "b1",
            "updated_at": 12345.0,
        }
        catalog = migrate_legacy_snapshot(v1)
        assert catalog.version == SESSION_CATALOG_VERSION
        assert len(catalog.sessions) == 1
        assert catalog.active_session_id  # 有 active
        entry = catalog.get_or_create_active()
        assert entry.last_active_brief_id == "b1"
        # 旧数据保留在 legacy_snapshot
        assert "briefs" in catalog.legacy_snapshot
        assert "b1" in catalog.legacy_snapshot["briefs"]

    def test_from_dict_v1_triggers_migration(self):
        v1 = {"briefs": {}, "variants": {}, "decisions": {}, "overrides": {},
              "last_active_brief_id": "x", "updated_at": 1.0}
        catalog = SessionCatalog.from_dict(v1)
        assert catalog.version == SESSION_CATALOG_VERSION
        assert len(catalog.sessions) >= 1

    def test_from_dict_v2_passthrough(self):
        c = SessionCatalog()
        e = c.create_session("hello")
        c2 = SessionCatalog.from_dict(c.to_dict())
        assert c2.version == SESSION_CATALOG_VERSION
        assert c2.active_session_id == e.session_id
        assert c2.get_session(e.session_id).title == "hello"


# ================================================================
# AssistantSessionStore 持久化 + 目录 API
# ================================================================


class TestAssistantSessionStore:
    def test_persist_writes_both_v1_and_v2(self, tmp_path):
        store = AssistantSessionStore()
        store.create_session("test")
        assert store.persist(tmp_path) is True
        assert (tmp_path / "ai" / "session.json").exists()
        assert (tmp_path / "ai" / "session_catalog.json").exists()

    def test_load_v2_roundtrip(self, tmp_path):
        store = AssistantSessionStore()
        e = store.create_session("round")
        store.add_message_summary(MessageSummary(role="user", text="hi"))
        store.record_run(RunRef(run_id="r1", skill_id="topos.subtitle_draft",
                                status="succeeded"))
        store.persist(tmp_path)

        store2 = AssistantSessionStore()
        assert store2.load(tmp_path) is True
        e2 = store2.get_active_session()
        assert len(e2.messages) == 1
        assert e2.active_run_id == "r1"
        assert e2.run_refs[0].skill_id == "topos.subtitle_draft"

    def test_load_v1_only_migrates(self, tmp_path):
        v1 = {
            "briefs": {"b1": {"brief_id": "b1", "revision": 1}},
            "variants": {}, "decisions": {}, "overrides": {},
            "last_active_brief_id": "b1", "updated_at": 1.0,
        }
        (tmp_path / "ai").mkdir(parents=True)
        (tmp_path / "ai" / "session.json").write_text(json.dumps(v1))
        store = AssistantSessionStore()
        assert store.load(tmp_path) is True
        assert store.catalog.version == SESSION_CATALOG_VERSION
        # brief 仍可读（旧 API 不丢）
        assert store.get_brief("b1") is not None

    def test_load_no_files_returns_false(self, tmp_path):
        store = AssistantSessionStore()
        assert store.load(tmp_path) is False

    def test_create_session_does_not_delete_history(self):
        store = AssistantSessionStore()
        e1 = store.create_session("first")
        store.add_message_summary(MessageSummary(text="m1"))
        e2 = store.create_session("second")
        # 第一个会话仍在
        assert store.get_session(e1.session_id) is not None
        assert len(store.get_session(e1.session_id).messages) == 1

    def test_active_session_accumulates_multi_turn_messages(self, tmp_path):
        """连续多轮消息属于同一历史，只有显式 create_session 才切分。"""
        store = AssistantSessionStore(cache_path=tmp_path / "history.json")
        first = store.get_active_session()
        store.add_message_summary(MessageSummary(role="user", text="第一轮需求"))
        store.add_message_summary(MessageSummary(role="assistant", text="第一轮回复"))
        store.add_message_summary(MessageSummary(role="user", text="第二轮补充"))

        active = store.get_active_session()
        assert active.session_id == first.session_id
        assert [message.role for message in active.messages] == [
            "user", "assistant", "user",
        ]
        assert active.title == "第一轮需求"
        assert len(store.list_sessions(include_archived=False)) == 1

    def test_archive_then_switch(self):
        store = AssistantSessionStore()
        e1 = store.create_session("a")
        e2 = store.create_session("b")
        store.archive_session(e2.session_id)
        # 切到 e1（未归档）
        assert store.set_active_session(e1.session_id) is True
        # 切到 e2（已归档）失败
        assert store.set_active_session(e2.session_id) is False

    def test_add_message_to_specified_session(self):
        store = AssistantSessionStore()
        e1 = store.create_session("a")
        e2 = store.create_session("b")
        store.add_message_summary(MessageSummary(text="to e1"), session_id=e1.session_id)
        assert len(store.get_session(e1.session_id).messages) == 1
        assert len(store.get_session(e2.session_id).messages) == 0

    def test_compression_checkpoint_persisted(self, tmp_path):
        store = AssistantSessionStore()
        store.create_session("c")
        store.add_compression_checkpoint(CompressionCheckpoint(
            summary="compressed", message_count_before=10, message_count_after=5,
        ))
        store.persist(tmp_path)
        store2 = AssistantSessionStore()
        store2.load(tmp_path)
        e = store2.get_active_session()
        assert len(e.compression_checkpoints) == 1
        assert e.compression_checkpoints[0].message_count_before == 10

    def test_local_cache_keeps_only_recent_sessions(self, tmp_path):
        cache_path = tmp_path / "assistant_history.json"
        store = AssistantSessionStore(cache_path=cache_path, cache_limit=2)
        for index in range(3):
            store.create_session(f"session-{index}")
            store.add_message_summary(MessageSummary(text=f"message-{index}"))

        data = json.loads(cache_path.read_text(encoding="utf-8"))
        assert len(data["sessions"]) == 2
        assert store.cache_limit == 2

    def test_history_list_is_project_scoped_and_clear_removes_cache(self, tmp_path):
        cache_path = tmp_path / "assistant_history.json"
        store = AssistantSessionStore(cache_path=cache_path)
        store.create_session("当前工程")
        store.add_message_summary(MessageSummary(role="user", text="当前工程消息"))

        other = AssistantSessionStore(cache_path=cache_path)
        other.create_session("另一个工程")
        other.add_message_summary(MessageSummary(role="user", text="另一个工程消息"))

        # 当前 store 的菜单不应把全局缓存中其它工程的条目合并进来。
        assert [entry.title for entry in store.list_sessions()] == ["当前工程"]

        store.clear_history()
        assert store.list_sessions() == []
        assert json.loads(cache_path.read_text(encoding="utf-8")) == {
            "version": 1, "sessions": {},
        }

    def test_old_brief_api_still_works(self):
        """旧 save_brief/get_brief/get_variant API 必须保持工作（向后兼容）。"""
        from src.features.edit.ai_editing.editorial_models import (
            DurationTarget, EditorialBrief, Platform,
        )
        store = AssistantSessionStore()
        brief = EditorialBrief(
            brief_id="b1", revision=1, platform=Platform.VERTICAL_SOCIAL,
            target_duration=DurationTarget(720, 648, 792),
            required_beats=("hook",),
        )
        store.save_brief(brief)
        assert store.get_brief("b1") is not None
        assert store.snapshot.last_active_brief_id == "b1"
