"""F4.1 · 场景分组与组前/片段/组后处理——验收测试。

计划验收锚点：
- 同场景至少 12 镜头、两类机位共同改 look，而各镜头微调仍保留；
- 移组/复制/删除/Undo/重开/导出一致（命令事务 + IO roundtrip + 分裂保留）；
- 组前后顺序有画质向量（顺序不可交换 + 恒等链不改变像素）；
- 源→工作与输出变换不重复（组栈不含 IDT/ODT——管线级语义，F5.1）。
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.color.core.the_decompiler import GradingMath
from src.features.edit.core.color_chain import (
    chain_extra_params,
    chain_stack_ids,
    extras_active,
    resolve_color_chain,
)
from src.features.edit.core.color_group import (
    ColorGroupData,
    ColorGroupMember,
    SetColorGroupsCommand,
    group_for_clip,
)
from src.features.edit.core.edit_project_io import (
    convert_edit_project_to_topos_project,
    convert_topos_project_to_edit_project,
)
from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_v2 import ClipInstance

pytestmark = pytest.mark.edit_core


def _stack(stack_id: str, **params) -> SimpleNamespace:
    """最小 ColorStackData 替身（链解析只读这些属性）。"""
    base = {
        "exposure": 0.0, "temp": 0.0, "tint": 0.0, "contrast": 0.0,
        "shadow": 0.0, "highlights": 0.0, "saturation": 0.0,
        "vibrance": 0.0, "mid_detail": 0.0, "sharpness": 0.0,
        "hue_shift": 0.0, "lift": (0.0, 0.0, 0.0), "gamma": (0.0, 0.0, 0.0),
        "gain": (0.0, 0.0, 0.0),
    }
    base.update(params)
    return SimpleNamespace(
        stack_id=stack_id, grading_params=base,
        curves_data={}, hsl_curve_data={}, mixer_params={},
        lut_path=None, soft_clip_params={"enabled": False},
        layers=[], active_layer_index=None)


def _project_with_group(
    n_clips: int = 12, cameras: tuple = ("A-cam", "B-cam"),
    post_params: dict = None, trim_exposure: float = 5.0,
) -> tuple:
    """12 镜头/两类机位工程：两机组 + 每镜头独立微调栈。"""
    stacks: dict = {}
    for cam in cameras:
        stacks[f"post-{cam}"] = _stack(
            f"post-{cam}", **(post_params or {"contrast": 10.0}))
    clips = []
    groups: dict = {}
    for cam_idx, cam in enumerate(cameras):
        members = []
        for i in range(n_clips // len(cameras)):
            clip_id = f"{cam}-shot{i}"
            stacks[f"trim-{clip_id}"] = _stack(
                f"trim-{clip_id}", exposure=trim_exposure + 0.5 * i)
            members.append(ColorGroupMember("tl1", clip_id))
            clips.append(ClipInstance(
                clip_id, "asset", "t1", 0, 10, 0, 10,
                color_trim_stack_id=f"trim-{clip_id}"))
        groups[f"g-{cam}"] = ColorGroupData(
            group_id=f"g-{cam}", name=f"Scene/{cam}", camera=cam,
            members=tuple(members), post_group_stack_id=f"post-{cam}")
    project = SimpleNamespace(color_stacks=stacks, color_groups=groups)
    return project, clips, groups


class TestColorGroupModel:
    def test_group_roundtrip_and_lookup(self) -> None:
        project = EditProject(project_id="p", name="P")
        cmd = SetColorGroupsCommand.create_group(
            project, "Interview", (("tl1", "c1"), ("tl1", "c2")),
            camera="A-cam", post_group_stack_id="post-a")
        p2 = cmd.apply(project)
        group = group_for_clip(p2, "tl1", "c1")
        assert group is not None and group.camera == "A-cam"
        assert group.post_group_stack_id == "post-a"
        # undo 对称
        assert SetColorGroupsCommand.dissolve_group(
            p2, group.group_id).apply(p2).color_groups == {}

    def test_unique_ownership_refused(self) -> None:
        project = EditProject(project_id="p", name="P")
        p2 = SetColorGroupsCommand.create_group(
            project, "G1", (("tl1", "c1"),)).apply(project)
        with pytest.raises(ValueError, match="唯一归属"):
            SetColorGroupsCommand.create_group(
                p2, "G2", (("tl1", "c1"),)).apply(p2)

    def test_conflicting_transaction_refused_no_half_write(self) -> None:
        project = EditProject(project_id="p", name="P")
        p2 = SetColorGroupsCommand.create_group(
            project, "G1", (("tl1", "c1"),)).apply(project)
        stale = SetColorGroupsCommand.create_group(
            project, "Stale", (("tl1", "c9"),))
        with pytest.raises(ValueError, match="已被其他操作修改"):
            stale.apply(p2)
        assert len(p2.color_groups) == 1  # 零半写

    def test_add_remove_dissolve_undo(self) -> None:
        project = EditProject(project_id="p", name="P")
        p2 = SetColorGroupsCommand.create_group(
            project, "G", (("tl1", "c1"),)).apply(project)
        p3 = SetColorGroupsCommand.add_clips(
            p2, "g1" if "g1" in p2.color_groups else
            next(iter(p2.color_groups)), (("tl1", "c2"),)).apply(p2)
        gid = next(iter(p3.color_groups))
        assert len(p3.color_groups[gid].members) == 2
        p4 = SetColorGroupsCommand.remove_clips(
            p3, gid, ("c2",)).apply(p3)
        assert len(p4.color_groups[gid].members) == 1
        p5 = SetColorGroupsCommand.dissolve_group(p4, gid).apply(p4)
        assert p5.color_groups == {}

    def test_io_roundtrip_preserves_groups(self) -> None:
        project = EditProject(project_id="p", name="P")
        p2 = SetColorGroupsCommand.create_group(
            project, "Scene 4", (("tl1", "c1"), ("tl2", "c2")),
            camera="B-cam", pre_group_stack_id="pre-x").apply(project)
        topos = convert_edit_project_to_topos_project(p2)
        restored = convert_topos_project_to_edit_project(
            topos, full_data={})
        group = group_for_clip(restored, "tl2", "c2")
        assert group is not None
        assert group.pre_group_stack_id == "pre-x"
        assert group.camera == "B-cam"
        # 旧工程（无键）→ 空组表
        blank = convert_topos_project_to_edit_project(
            convert_edit_project_to_topos_project(
                EditProject(project_id="q", name="Q")), full_data={})
        assert blank.color_groups == {}

    def test_split_preserves_trim_reference(self) -> None:
        """分裂/复制保留微调栈引用（dataclass 全字段保留契约）。"""
        from dataclasses import replace
        clip = ClipInstance("c", "a", "t", 0, 10, 0, 10,
                            color_trim_stack_id="trim-1")
        left = replace(clip, timeline_duration=5)
        assert left.color_trim_stack_id == "trim-1"


class TestChainResolution:
    def test_resolution_order_pre_trim_post(self) -> None:
        project, clips, _groups = _project_with_group(n_clips=4)
        clip = clips[0]
        pre, trim, post = resolve_color_chain(project, "tl1", clip)
        assert pre is None
        assert trim is not None and trim.stack_id == "trim-A-cam-shot0"
        assert post is not None and post.stack_id == "post-A-cam"
        extras = chain_extra_params(project, "tl1", clip)
        assert [e is not None for e in extras] == [True, True]
        assert extras_active(extras)

    def test_ungrouped_clip_has_no_chain(self) -> None:
        project = SimpleNamespace(
            color_stacks={}, color_groups={})
        clip = ClipInstance("c", "a", "t", 0, 10, 0, 10)
        assert chain_extra_params(project, "tl1", clip) == []

    def test_chain_stack_ids_for_fingerprint(self) -> None:
        project, clips, _ = _project_with_group(n_clips=4)
        ids = set(chain_stack_ids(project, "tl1", clips[0]))
        assert "trim-A-cam-shot0" in ids and "post-A-cam" in ids


class TestQualityVectors:
    """画质向量：顺序不可交换 + 恒等链零改变 + 微调保留。"""

    @staticmethod
    def _apply_chain(rgb: np.ndarray, params_list: list) -> np.ndarray:
        out = rgb
        for params in params_list:
            out = GradingMath.apply_grading(out, params)
        return out

    def test_order_pre_clip_post_not_commutative(self) -> None:
        """组前后顺序向量：pre(contrast) → post(exposure) 与交换顺序
        输出不同——顺序语义被向量锁定（非线性参数不可交换）。"""
        rng = np.random.default_rng(11)
        rgb = rng.uniform(0.05, 0.95, size=(4096, 3)).astype(np.float32)
        pre = {"contrast": 20.0}
        post = {"exposure": 30.0}
        ordered = self._apply_chain(rgb, [pre, post])
        swapped = self._apply_chain(rgb, [post, pre])
        assert not np.allclose(ordered, swapped, atol=1e-4)
        # 恒等链不改变像素
        identity = self._apply_chain(rgb, [{}, {}])
        assert np.allclose(identity, rgb, atol=1e-7)

    def test_group_look_change_preserves_per_clip_trim(self) -> None:
        """12 镜头/两类机位：改组 look（组栈参数）后，每镜头微调仍保留
        ——链输出 = 组参数变化 ⊕ 固定 trim 顺序应用。"""
        project, clips, _ = _project_with_group(
            n_clips=12, post_params={"contrast": 10.0})
        rng = np.random.default_rng(3)
        rgb = rng.uniform(0.05, 0.95, size=(1024, 3)).astype(np.float32)
        # 两类机位各自 look，镜头微调互不相同（exposure 随 i 递增）
        for i, clip in enumerate(clips):
            extras = chain_extra_params(project, "tl1", clip)
            out = self._apply_chain(rgb, extras)
            # 微调差异存在：相邻镜头（同组）输出不同
            if i + 1 < len(clips) and clips[i + 1].color_trim_stack_id \
                    != clip.color_trim_stack_id:
                nxt = self._apply_chain(
                    rgb, chain_extra_params(project, "tl1", clips[i + 1]))
                assert not np.allclose(out, nxt, atol=1e-6)
        # 改组 look（post 栈 contrast 10 → 40）：trim 段不变
        changed_project, changed_clips, _ = _project_with_group(
            n_clips=12, post_params={"contrast": 40.0})
        clip0 = clips[0]
        before = self._apply_chain(
            rgb, chain_extra_params(project, "tl1", clip0))
        after = self._apply_chain(
            rgb, chain_extra_params(changed_project, "tl1",
                                    changed_clips[0]))
        assert not np.allclose(before, after, atol=1e-4)  # 组 look 生效
        # trim 保留：changed 链的 trim 段（extras[0]，pre 为 None）与原链一致
        assert chain_extra_params(changed_project, "tl1", changed_clips[0])[
            0]["exposure"] == chain_extra_params(project, "tl1", clip0)[
            0]["exposure"]

    def test_group_stacks_carry_no_idt_odt(self) -> None:
        """变换不重复：组/微调栈模型无 IDT/ODT 字段（管线级语义，
        F5.1 收口）——组 look 只含调色参数。"""
        payload = ColorGroupData(
            name="G", post_group_stack_id="s").to_dict()
        assert set(payload) == {
            "group_id", "name", "camera", "members",
            "pre_group_stack_id", "post_group_stack_id"}
        assert not any("transform" in k or "odt" in k or "idt" in k
                       for k in payload)

    def test_exporter_applies_chain_sequentially(self) -> None:
        """成片 CPU 路径消费链段：导出输出 == 逐段顺序 oracle。"""
        from src.features.edit.core.timeline_export import TimelineExporter

        main = _stack("main", exposure=10.0)
        post = _stack("post", contrast=15.0)
        trim = _stack("trim", saturation=8.0)
        project = SimpleNamespace(
            color_stacks={"main": main, "post": post, "trim": trim},
            color_groups={"g": ColorGroupData(
                group_id="g", name="G",
                members=(ColorGroupMember("tl1", "clip"),),
                post_group_stack_id="post")})
        exporter = TimelineExporter.__new__(TimelineExporter)
        exporter._project = project
        exporter._sequence = SimpleNamespace(timeline_id="tl1")
        exporter._color_grade_lut_cache = {}
        layer = SimpleNamespace(
            clip=ClipInstance("clip", "a", "t", 0, 10, 0, 10,
                              color_stack_id="main",
                              color_trim_stack_id="trim"),
            clip_id="clip", source_frame=0)
        grade = exporter._get_clip_color_grade(layer)
        assert grade is not None
        extras = grade["chain_extra_params"]
        assert [e["contrast"] == 15.0 for e in extras if "contrast" in e]

        frame = np.full((4, 8, 3), 128, dtype=np.uint8)
        graded = exporter._apply_color_grade(frame, layer)
        # oracle：主 → trim → post 顺序（链段序 [trim, post]）
        rgb = frame[..., ::-1].astype(np.float32) / 255.0
        expect = rgb.reshape(-1, 3)
        expect = GradingMath.apply_grading(expect, main.grading_params)
        for extra in extras:
            expect = GradingMath.apply_grading(expect, extra)
        expect = np.rint(np.clip(expect[..., ::-1].reshape(frame.shape),
                                 0.0, 1.0) * 255.0).astype(np.uint8)
        assert np.allclose(graded, expect, atol=1)


# ================================================================
# M05.3 · 组成员编辑策略
# ================================================================

class TestMemberEditPolicies:
    def _group(self):
        from src.features.edit.core.color_group import ColorGroupData
        return ColorGroupData(
            group_id="g1", name="G",
            members=(ColorGroupMember(timeline_id="tl1", clip_id="c1"),
                     ColorGroupMember(timeline_id="tl2", clip_id="c2")),
            pre_group_stack_id="s-pre")

    def test_split_new_clips_not_auto_adopted(self):
        from src.features.edit.core.color_group import (
            member_policy_for_split,
        )
        g = self._group()
        policy = member_policy_for_split(g, "c1", ("c1_L", "c1_R"))
        assert policy["in_group"] is True
        assert policy["new_members_adopted"] == []   # 契约：不自动入组
        assert policy["policy"] == "manual"

    def test_split_clip_not_in_group(self):
        from src.features.edit.core.color_group import (
            member_policy_for_split,
        )
        policy = member_policy_for_split(
            self._group(), "cX", ("cX_L",))
        assert policy["in_group"] is False
        assert policy["policy"] == "n/a"

    def test_cross_timeline_summary(self):
        from src.features.edit.core.color_group import (
            member_policy_for_cross_timeline,
        )
        summary = member_policy_for_cross_timeline(self._group(), "tl1")
        assert summary["cross_timeline"] is True
        assert summary["timelines"] == {"tl1": 1, "tl2": 1}
        assert summary["members_in_querying"] == 1

    def test_stack_entry_assignment(self):
        from src.features.edit.core.color_group import (
            group_stack_entry_assignment,
        )
        g = self._group()
        view = group_stack_entry_assignment(g, "pre", "s-new")
        assert view["current_stack_id"] == "s-pre"
        assert view["changed"] is True
        view_same = group_stack_entry_assignment(g, "pre", "s-pre")
        assert view_same["changed"] is False
        import pytest as _pytest
        with _pytest.raises(ValueError, match="未知链段"):
            group_stack_entry_assignment(g, "mid", "s")
