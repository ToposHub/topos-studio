"""TAEP M6 — typed artifact + ArtifactStore 测试。

验收（§15.1 / §11.1 M6 红线）：
- 版本化：每次变更 bump revision（with_revision_bumped）。
- 来源可追溯：source（user/model/skill）+ user_edited 标记。
- 用户优先（红线）：user_edited=True 的 artifact 拒绝 model/skill 覆盖。
- 模型只能 propose patch；apply_patch 乐观锁（base_revision 必须匹配）。
- typed kinds：Script / ShotList / Storyboard / DeliveryOutline 各自正确序列化。
- 不写 timeline（artifact 是创作文档）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.artifact_models import (
    ArtifactKind,
    ArtifactPatch,
    ArtifactSource,
    DeliveryItem,
    DeliveryOutlineArtifact,
    ScriptArtifact,
    ScriptLine,
    ShotItem,
    ShotListArtifact,
    StoryboardArtifact,
)
from src.features.edit.ai_editing.artifact_store import (
    ArtifactNotFoundError,
    ArtifactOverrideError,
    ArtifactRevisionConflictError,
    ArtifactStore,
)


# ---------------------------------------------------------------------- #
# 版本化 + 来源
# ---------------------------------------------------------------------- #


class TestRevisionAndProvenance:
    def test_with_revision_bumped(self) -> None:
        s = ScriptArtifact(artifact_id="s1")
        assert s.revision == 0
        s2 = s.with_revision_bumped()
        assert s2.revision == 1
        assert s.revision == 0  # 原实例不可变

    def test_with_user_edit_marks_user_edited(self) -> None:
        s = ScriptArtifact(artifact_id="s1", source=ArtifactSource.MODEL)
        assert s.user_edited is False
        edited = s.with_user_edit()
        assert edited.user_edited is True
        assert edited.source == ArtifactSource.USER

    def test_empty_artifact_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            ScriptArtifact(artifact_id="")

    def test_negative_revision_rejected(self) -> None:
        with pytest.raises(ValueError):
            ScriptArtifact(artifact_id="s1", revision=-1)


# ---------------------------------------------------------------------- #
# ArtifactStore：用户优先覆盖（红线）
# ---------------------------------------------------------------------- #


class TestUserPriorityOverride:
    def test_model_cannot_override_user_edited(self) -> None:
        """§11.1 M6 红线：user_edited=True 的 artifact 拒绝 model 覆盖。"""
        store = ArtifactStore()
        # 用户先建脚本
        store.save(ScriptArtifact(artifact_id="s1", source=ArtifactSource.USER))
        # 标记用户编辑
        store.save_user_edit(store.get("s1"))
        # 模型试图覆盖 → 拒绝
        with pytest.raises(ArtifactOverrideError):
            store.save(ScriptArtifact(
                artifact_id="s1", source=ArtifactSource.MODEL, title="AI 改的",
            ))

    def test_user_can_override_own(self) -> None:
        """用户自己的编辑总是允许（allow_override=True）。"""
        store = ArtifactStore()
        store.save(ScriptArtifact(artifact_id="s1", source=ArtifactSource.MODEL))
        # 用户接管
        store.save(ScriptArtifact(
            artifact_id="s1", source=ArtifactSource.USER, title="用户版",
        ), allow_override=True)
        assert store.get("s1").source == ArtifactSource.USER

    def test_model_can_update_non_user_edited(self) -> None:
        """未标记 user_edited 的 artifact，模型可以更新。"""
        store = ArtifactStore()
        store.save(ScriptArtifact(artifact_id="s1", source=ArtifactSource.MODEL))
        # 模型再存（未 user_edited）→ 允许
        store.save(ScriptArtifact(
            artifact_id="s1", source=ArtifactSource.MODEL, title="v2",
        ))
        assert store.get("s1").title == "v2"

    def test_apply_patch_rejected_on_user_edited(self) -> None:
        """用户改过 → 模型 patch 被拒（红线）。"""
        store = ArtifactStore()
        store.save(ScriptArtifact(artifact_id="s1", source=ArtifactSource.USER))
        store.save_user_edit(store.get("s1"))
        patch = ArtifactPatch(
            artifact_id="s1", base_revision=store.get("s1").revision,
            changes={"title": "AI"}, source=ArtifactSource.MODEL,
        )
        with pytest.raises(ArtifactOverrideError):
            store.apply_patch(patch)


# ---------------------------------------------------------------------- #
# ArtifactStore：乐观锁 patch
# ---------------------------------------------------------------------- #


class TestOptimisticLockPatch:
    def test_apply_patch_bumps_revision(self) -> None:
        store = ArtifactStore()
        store.save(ScriptArtifact(
            artifact_id="s1", source=ArtifactSource.MODEL, title="v1",
        ))
        patch = ArtifactPatch(
            artifact_id="s1", base_revision=0,
            changes={"title": "v2"}, source=ArtifactSource.MODEL,
        )
        applied = store.apply_patch(patch)
        assert applied.revision == 1
        assert applied.title == "v2"

    def test_apply_patch_stale_revision_rejected(self) -> None:
        store = ArtifactStore()
        store.save(ScriptArtifact(artifact_id="s1", revision=2, source=ArtifactSource.MODEL))
        patch = ArtifactPatch(
            artifact_id="s1", base_revision=0,  # 过时
            changes={"title": "x"}, source=ArtifactSource.MODEL,
        )
        with pytest.raises(ArtifactRevisionConflictError):
            store.apply_patch(patch)

    def test_apply_patch_unknown_artifact(self) -> None:
        store = ArtifactStore()
        patch = ArtifactPatch(
            artifact_id="ghost", base_revision=0, changes={}, source=ArtifactSource.MODEL,
        )
        with pytest.raises(ArtifactNotFoundError):
            store.apply_patch(patch)

    def test_apply_patch_ignores_unknown_fields(self) -> None:
        """patch.changes 含未知字段 → 忽略（防注入；不崩）。"""
        store = ArtifactStore()
        store.save(ScriptArtifact(artifact_id="s1", source=ArtifactSource.MODEL))
        patch = ArtifactPatch(
            artifact_id="s1", base_revision=0,
            changes={"title": "ok", "malicious_field": "inject"},
            source=ArtifactSource.MODEL,
        )
        applied = store.apply_patch(patch)
        assert applied.title == "ok"
        assert not hasattr(applied, "malicious_field")

    def test_apply_patch_does_not_touch_artifact_id_or_kind(self) -> None:
        """patch 不能改 artifact_id / kind（identity 字段保护）。"""
        store = ArtifactStore()
        store.save(ScriptArtifact(artifact_id="s1", source=ArtifactSource.MODEL))
        patch = ArtifactPatch(
            artifact_id="s1", base_revision=0,
            changes={"artifact_id": "hacked", "kind": ArtifactKind.SHOT_LIST, "revision": 99},
            source=ArtifactSource.MODEL,
        )
        applied = store.apply_patch(patch)
        assert applied.artifact_id == "s1"
        assert applied.kind == ArtifactKind.SCRIPT
        assert applied.revision == 1  # patch 指定的 revision=99 被忽略，正常 bump


# ---------------------------------------------------------------------- #
# 查询 + 删除
# ---------------------------------------------------------------------- #


class TestQueryAndRemove:
    def test_list_by_kind(self) -> None:
        store = ArtifactStore()
        store.save(ScriptArtifact(artifact_id="s1"))
        store.save(ShotListArtifact(artifact_id="sh1"))
        store.save(ShotListArtifact(artifact_id="sh2"))
        assert len(store.list_by_kind(ArtifactKind.SCRIPT)) == 1
        assert len(store.list_by_kind(ArtifactKind.SHOT_LIST)) == 2

    def test_get_none_when_missing(self) -> None:
        store = ArtifactStore()
        assert store.get("ghost") is None

    def test_require_raises_when_missing(self) -> None:
        store = ArtifactStore()
        with pytest.raises(ArtifactNotFoundError):
            store.require("ghost")

    def test_remove(self) -> None:
        store = ArtifactStore()
        store.save(ScriptArtifact(artifact_id="s1"))
        assert store.remove("s1") is True
        assert store.get("s1") is None
        assert store.remove("s1") is False


# ---------------------------------------------------------------------- #
# typed kinds 序列化
# ---------------------------------------------------------------------- #


class TestTypedKindsSerialization:
    def test_script_to_dict(self) -> None:
        s = ScriptArtifact(
            artifact_id="s1", title="口播",
            lines=(ScriptLine(line_id="l1", text="hello", speaker="S1"),),
            source=ArtifactSource.MODEL, model_id="gpt-4",
        )
        d = s.to_dict()
        assert d["kind"] == "script"
        assert d["title"] == "口播"
        assert len(d["lines"]) == 1
        assert d["lines"][0]["speaker"] == "S1"
        assert d["model_id"] == "gpt-4"

    def test_shot_list_to_dict(self) -> None:
        sl = ShotListArtifact(
            artifact_id="sh1", shots=(
                ShotItem(shot_id="shot1", label="开场", asset_id="a1", start_frame=0, end_frame=24),
            ),
        )
        d = sl.to_dict()
        assert d["kind"] == "shot_list"
        assert d["shots"][0]["asset_id"] == "a1"

    def test_storyboard_to_dict(self) -> None:
        sb = StoryboardArtifact(
            artifact_id="sb1", panels=(
                ShotItem(shot_id="p1", description="产品特写"),
            ),
        )
        d = sb.to_dict()
        assert d["kind"] == "storyboard"
        assert d["panels"][0]["description"] == "产品特写"

    def test_delivery_outline_to_dict(self) -> None:
        do = DeliveryOutlineArtifact(
            artifact_id="do1", items=(
                DeliveryItem(item_id="d1", platform="douyin", aspect_ratio="9:16",
                             duration_frames=720),
            ),
        )
        d = do.to_dict()
        assert d["kind"] == "delivery_outline"
        assert d["items"][0]["platform"] == "douyin"
        assert d["items"][0]["duration_frames"] == 720


# ---------------------------------------------------------------------- #
# 不写 timeline（§0 红线）
# ---------------------------------------------------------------------- #


class TestNoTimelineWrite:
    def test_artifact_has_no_timeline_methods(self) -> None:
        """artifact 是创作文档，不提供任何写 timeline 入口。"""
        store = ArtifactStore()
        assert not hasattr(store, "commit")
        assert not hasattr(store, "apply_to_timeline")
        assert not hasattr(store, "execute_command")
