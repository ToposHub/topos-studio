"""BE-03 · local/remote 域解析服务验收测试。

验收锚点（计划 §6 BE-03 完成定义：域与 stack scope 区分；共享影响
明确；迁移文档/测试齐全——语义文档见 color_version_domains.py 模块
docstring 与 IMPLEMENTATION_PLAN.md §3.2）：
- 域判定以被引用栈 scope 为权威，color_shared 仅交叉校验；
- LOCAL 域解析含 legacy 裸栈（按引用）与切共享后的属主盖章遗留栈；
- REMOTE 域解析做 scope 校验（不误中同素材 clip_local 栈）；
- 旧工程（无版本键）零迁移可读；
- live_modified 计算排除身份字段与时间戳。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.color_version_domains import (
    VersionDomain,
    domain_in_use,
    domain_snapshot,
    find_clip_ref,
    live_matches_active_version,
    resolve_clip_binding,
    resolve_domain_stack_id,
    resolve_local_stack_id,
    resolve_remote_stack_id,
    shared_binding_conflict,
)
from src.features.edit.core.color_versions import (
    ActivateColorVersionCommand,
    SaveColorVersionCommand,
    stack_dependent_clips,
)
from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_v2 import (
    ClipInstance, EditTrack, TimelineSequence,
)
from src.shared.project.color_stack import ColorStackData

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, asset_id: str, stack_id: str | None,
          shared: bool = False, index: int = 0) -> ClipInstance:
    return ClipInstance(
        clip_id, asset_id, "v1", index * 10, 10, 0, 10,
        color_stack_id=stack_id, color_shared=shared)


def _project(clips, stacks) -> EditProject:
    timeline = TimelineSequence(
        timeline_id="tl1", name="TL", fps=24.0,
        video_tracks=(EditTrack(
            "v1", "V1", "video", 0, clips=tuple(clips)),))
    return EditProject(
        project_id="p", name="P",
        color_stacks={s.stack_id: s for s in stacks},
        timelines={"tl1": timeline})


def _local_stack(stack_id, clip_id="c1", asset_id="a1",
                 exposure=5.0, stamped=True) -> ColorStackData:
    """clip_local 栈；stamped=False 表示 legacy 无属主盖章（scope 仍为
    clip_local——from_dict 的缺省回退即此形态）。"""
    return ColorStackData(
        stack_id=stack_id, asset_id=asset_id, name=stack_id,
        scope="clip_local",
        source_kind="timeline_clip" if stamped else None,
        timeline_id="tl1" if stamped else None,
        clip_id=clip_id if stamped else None,
        grading_params={"exposure": exposure})


def _remote_stack(stack_id, asset_id="a1",
                  exposure=9.0) -> ColorStackData:
    return ColorStackData(
        stack_id=stack_id, asset_id=asset_id, name=stack_id,
        scope="asset_default", source_kind="asset_default",
        grading_params={"exposure": exposure})


class TestDomainResolution:
    def test_local_in_use_by_stack_scope(self):
        p = _project([_clip("c1", "a1", "loc-1", shared=False)],
                     [_local_stack("loc-1")])
        binding = resolve_clip_binding(p, "tl1", "c1")
        assert binding.domain == VersionDomain.LOCAL
        assert binding.stack_id == "loc-1"
        assert domain_in_use(p, "tl1", "c1", VersionDomain.LOCAL)
        assert not domain_in_use(p, "tl1", "c1", VersionDomain.REMOTE)

    def test_remote_in_use_by_stack_scope_ignoring_color_shared_flag(self):
        """权威依据是被引用栈 scope，不是 color_shared 标志。"""
        # color_shared=False 但栈是 asset_default → 仍是 remote（以栈为准）
        p = _project([_clip("c1", "a1", "rem-1", shared=False)],
                     [_remote_stack("rem-1")])
        binding = resolve_clip_binding(p, "tl1", "c1")
        assert binding.domain == VersionDomain.REMOTE
        assert shared_binding_conflict(p, "tl1", "c1") != ""
        assert domain_in_use(p, "tl1", "c1", VersionDomain.REMOTE)

    def test_local_referenced_stack_wins_even_if_stale_stamp_exists(self):
        """片段当前引用的 clip_local 栈优先于遗留盖章栈。"""
        p = _project(
            [_clip("c1", "a1", "loc-cur", shared=False)],
            [_local_stack("loc-cur", exposure=1.0),
             _local_stack("loc-stale", exposure=2.0)])
        assert resolve_local_stack_id(p, "tl1", "c1") == "loc-cur"

    def test_legacy_unstamped_local_stack_resolved_by_reference(self):
        """legacy 无盖章裸栈（scope 默认 clip_local、无属主）：按引用归属。"""
        p = _project(
            [_clip("c1", "a1", "legacy-1", shared=False)],
            [_local_stack("legacy-1", stamped=False)])
        binding = resolve_clip_binding(p, "tl1", "c1")
        assert binding.domain == VersionDomain.LOCAL
        assert resolve_local_stack_id(p, "tl1", "c1") == "legacy-1"

    def test_local_stack_found_by_ownership_stamp_after_switching_remote(self):
        """片段切到共享栈后：按属主盖章找回遗留私有栈（LOCAL 域仍可达）。"""
        p = _project(
            [_clip("c1", "a1", "rem-1", shared=True)],
            [_local_stack("loc-1", clip_id="c1"),
             _local_stack("loc-2", clip_id="c2"),  # 他者片段的私有栈
             _remote_stack("rem-1")])
        assert resolve_local_stack_id(p, "tl1", "c1") == "loc-1"
        snap = domain_snapshot(p, "tl1", "c1", "a1", VersionDomain.LOCAL)
        assert snap["stack_id"] == "loc-1"
        assert not snap["domain_in_use"]  # 当前实际用 remote
        rem = domain_snapshot(p, "tl1", "c1", "a1", VersionDomain.REMOTE)
        assert rem["stack_id"] == "rem-1" and rem["domain_in_use"]

    def test_remote_scope_checked_not_first_asset_stack(self):
        """REMOTE 解析必须查 scope：同素材的 clip_local 栈不得误中。"""
        p = _project(
            [_clip("c1", "a1", "loc-1")],
            [_local_stack("loc-1"), _remote_stack("rem-1")])
        assert resolve_remote_stack_id(p, "a1") == "rem-1"
        # 无 asset_default 栈 → 空域
        p2 = _project([_clip("c1", "a1", "loc-1")], [_local_stack("loc-1")])
        assert resolve_remote_stack_id(p2, "a1") is None
        snap = domain_snapshot(p2, "tl1", "c1", "a1", VersionDomain.REMOTE)
        assert snap["stack_id"] is None
        assert snap["versions_state"]["versions"] == {}

    def test_missing_clip_or_timeline_returns_none(self):
        p = _project([_clip("c1", "a1", "loc-1")], [_local_stack("loc-1")])
        assert resolve_clip_binding(p, "tl1", "nope") is None
        assert resolve_clip_binding(p, "no-tl", "c1") is None
        assert find_clip_ref(p, "tl1", "nope") is None
        assert domain_in_use(p, "tl1", "nope", VersionDomain.LOCAL) is False

    def test_trim_stack_reported_but_never_domain_target(self):
        """微调栈在绑定快照中可见（审计），但不是任何域的解析目标。"""
        clip = ClipInstance(
            "c1", "a1", "v1", 0, 10, 0, 10,
            color_stack_id="rem-1", color_shared=True,
            color_trim_stack_id="trim-c1",
        )
        p = _project([clip], [_remote_stack("rem-1"),
                              _local_stack("trim-c1", clip_id="c1")])
        binding = resolve_clip_binding(p, "tl1", "c1")
        assert binding.trim_stack_id == "trim-c1"
        assert binding.domain == VersionDomain.REMOTE
        assert resolve_domain_stack_id(
            p, "tl1", "c1", "a1", VersionDomain.LOCAL) is None


class TestLiveModifiedComputation:
    def test_matches_right_after_save_and_after_activate(self):
        p = _project([_clip("c1", "a1", "loc-1")],
                     [_local_stack("loc-1", exposure=5.0)])
        p = SaveColorVersionCommand.create(p, "loc-1", "A").apply(p)
        assert live_matches_active_version(p, "loc-1") is True
        # 保存后修改 live → 偏离
        from dataclasses import replace
        stack = replace(p.color_stacks["loc-1"],
                        grading_params={"exposure": 8.0})
        p2 = p.with_color_stacks({**p.color_stacks, "loc-1": stack})
        assert live_matches_active_version(p2, "loc-1") is False
        # 激活回 A → 一致（身份字段与时间戳不参与比对）
        p3 = ActivateColorVersionCommand.create(
            "loc-1", "A").apply(p2).project
        assert live_matches_active_version(p3, "loc-1") is True

    def test_no_versions_or_active_means_diverged(self):
        p = _project([_clip("c1", "a1", "loc-1")], [_local_stack("loc-1")])
        # 无版本表：live 不属于任何已保存版本
        assert live_matches_active_version(p, "loc-1") is False

    def test_modified_at_only_advance_not_counted_as_modified(self):
        """仅时间戳推进、参数未变：不算 live_modified（避免误报）。"""
        p = _project([_clip("c1", "a1", "loc-1")],
                     [_local_stack("loc-1", exposure=5.0)])
        p = SaveColorVersionCommand.create(p, "loc-1", "A").apply(p)
        from dataclasses import replace
        stack = replace(p.color_stacks["loc-1"], modified_at=999.0)
        p2 = p.with_color_stacks({**p.color_stacks, "loc-1": stack})
        assert live_matches_active_version(p2, "loc-1") is True


class TestSharedImpactAndLegacyProject:
    def test_remote_activation_impacts_all_dependent_clips(self):
        """共享影响明确：stack_dependent_clips 给出 remote 域影响范围。"""
        clip_a = _clip("c1", "a1", "rem-1", shared=True, index=0)
        clip_b = _clip("c2", "a1", "rem-1", shared=True, index=1)
        clip_c = _clip("c3", "a2", "loc-3", index=2)
        p = _project([clip_a, clip_b, clip_c],
                     [_remote_stack("rem-1"), _local_stack("loc-3")])
        assert set(stack_dependent_clips(p, "rem-1")) == {
            ("tl1", "c1"), ("tl1", "c2")}

    def test_old_project_without_version_keys_reads_empty(self):
        """旧工程零迁移：无 color_stack_versions 键 → 空表，不崩溃。"""
        p = _project([_clip("c1", "a1", "loc-1")], [_local_stack("loc-1")])
        assert p.color_stack_versions == {}
        snap = domain_snapshot(p, "tl1", "c1", "a1", VersionDomain.LOCAL)
        assert snap["stack_id"] == "loc-1"
        assert snap["versions_state"] == {
            "active": None, "locked": False, "versions": {},
            "default_version": None}
        assert snap["live_matches_active"] is False


class TestInheritedPrivateStackIsolation:
    def test_unactivated_split_cannot_resolve_source_clips_private_versions(self):
        from dataclasses import replace

        source = _local_stack("source-local", clip_id="source", exposure=1.0)
        p = _project([
            _clip("source", "a1", "source-local"),
            _clip("split-right", "a1", "source-local", index=1),
        ], [source])
        p = SaveColorVersionCommand.create(p, "source-local", "Original").apply(p)
        p = p.with_color_stacks({
            "source-local": replace(source, grading_params={"exposure": 9.0})})
        assert resolve_local_stack_id(p, "tl1", "source") == "source-local"
        assert resolve_local_stack_id(p, "tl1", "split-right") is None
        assert not domain_in_use(p, "tl1", "split-right", VersionDomain.LOCAL)
        snapshot = domain_snapshot(p, "tl1", "split-right", "a1", VersionDomain.LOCAL)
        assert snapshot["stack_id"] is None
        assert not snapshot["stack_exists"]
        assert not snapshot["domain_in_use"]
        assert snapshot["versions_state"]["versions"] == {}
        # No loadable local target leaks from the source clip. A future CREATE
        # must initialize an independent domain; it must not activate Original
        # on the source's private stack.
        assert p.color_stacks["source-local"].grading_params["exposure"] == 9.0

    def test_foreign_current_reference_falls_back_to_owned_stack_without_marking_it_in_use(self):
        p = _project([_clip("c1", "a1", "foreign")], [
            _local_stack("foreign", clip_id="c2"),
            _local_stack("owned", clip_id="c1"),
        ])
        assert resolve_local_stack_id(p, "tl1", "c1") == "owned"
        assert not domain_in_use(p, "tl1", "c1", VersionDomain.LOCAL)
        assert not domain_snapshot(p, "tl1", "c1", "a1", VersionDomain.LOCAL)["domain_in_use"]

    def test_timeline_ownership_mismatch_is_not_a_local_domain(self):
        from dataclasses import replace

        foreign = replace(_local_stack("foreign", clip_id="c1"), timeline_id="other-timeline")
        p = _project([_clip("c1", "a1", "foreign")], [foreign])
        assert resolve_local_stack_id(p, "tl1", "c1") is None
        assert not domain_in_use(p, "tl1", "c1", VersionDomain.LOCAL)

    def test_partial_legacy_stamp_allows_only_matching_nonempty_owner(self):
        from dataclasses import replace

        legacy = replace(_local_stack("legacy", stamped=False), timeline_id="tl1")
        p = _project([_clip("c1", "a1", "legacy")], [legacy])
        assert resolve_local_stack_id(p, "tl1", "c1") == "legacy"
        assert domain_in_use(p, "tl1", "c1", VersionDomain.LOCAL)
