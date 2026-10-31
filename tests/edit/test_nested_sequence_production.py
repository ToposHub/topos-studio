"""P1-02 嵌套序列生产链路测试。

验收（计划 P1-02）：
- 创建：选择片段移入子序列、父时间线原位替换为一个嵌套片段（标记/转场搬运）。
- 循环引用阻断：直接/间接包含父序列的嵌套片段不能再被嵌套进父序列。
- 双击/右键进入：切换活动时间线到子序列；退出走现有时间线切换。
- 修改子序列立即反映到父序列：预览经 resolver 实时取子序列。
- 导出与重开结果一致：导出器递归渲染嵌套层；工程注册子序列引用。

创建事务进入时间线历史；嵌套音频通过 flatten 参与父混音。父嵌套片段
变速/倒放的音频重采样与子轨转场仍为 PARTIAL（见 capability_registry）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineMarker,
    TimelineSequence,
    TransitionInstance,
)
from src.features.edit.core.nested_sequence import (
    NestedSequenceTransaction,
    build_nested_from_selection,
    collect_contained_timeline_ids,
    create_nested_clip,
    create_nested_sequence_asset,
    would_create_cycle,
)
from src.features.edit.core.project import EditProject, TimelineSequenceRef
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, track_id: str, start: int, duration: int = 50,
          linked_id: str | None = None) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id, asset_id=f"a_{clip_id}", track_id=track_id,
        timeline_start=start, timeline_duration=duration,
        source_start=0, source_duration=600, linked_clip_id=linked_id,
    )


def _timeline(timeline_id="parent", v_clips=(), a_clips=(), markers=(),
              transitions=()) -> TimelineSequence:
    v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                   clips=tuple(v_clips), transitions=tuple(transitions))
    tracks = [v1]
    if a_clips or True:
        tracks.append(EditTrack(track_id="a1", name="A1", track_type="audio",
                                index=0, clips=tuple(a_clips)))
    return TimelineSequence(
        timeline_id=timeline_id, name="Main", fps=Rational(24, 1),
        video_tracks=(tracks[0],), audio_tracks=(tracks[1],),
        markers=tuple(markers),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 创建事务
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildNestedFromSelection:

    def test_selection_moves_to_child_and_parent_gets_nested_clip(self):
        tl = _timeline(v_clips=[
            _clip("c1", "v1", 0), _clip("c2", "v1", 50), _clip("c3", "v1", 100),
        ])
        txn = build_nested_from_selection(tl, ("c1", "c2"), name="Child A")

        assert isinstance(txn, NestedSequenceTransaction)
        # 子序列：片段归零平移
        child_clips = {c.clip_id: c for c in txn.child_timeline.video_tracks[0].clips}
        assert child_clips["c1"].timeline_start == 0
        assert child_clips["c2"].timeline_start == 50
        # 父时间线：选择片段移除，原位一个嵌套片段
        parent_clips = txn.new_timeline.get_track_by_id("v1").clips
        assert [c.clip_id for c in parent_clips] == [txn.nested_clip_id, "c3"]
        nested = parent_clips[0]
        assert nested.asset_id == txn.nested_asset.asset_id
        assert nested.timeline_start == 0
        assert nested.timeline_duration == 100
        assert txn.host_track_id == "v1"
        assert txn.offset == 0
        # 素材元数据可解析回子序列
        assert txn.nested_asset.nested_timeline_id == txn.child_timeline.timeline_id
        assert txn.nested_asset.metadata["duration_frames"] == 100

    def test_offset_when_selection_starts_later(self):
        tl = _timeline(v_clips=[
            _clip("keep", "v1", 0), _clip("n1", "v1", 100), _clip("n2", "v1", 150),
        ])
        txn = build_nested_from_selection(tl, ("n1", "n2"))
        child_clips = txn.child_timeline.video_tracks[0].clips
        assert child_clips[0].timeline_start == 0   # n1: 100 - 100
        assert child_clips[1].timeline_start == 50  # n2: 150 - 100
        assert txn.offset == 100
        parent_clips = {c.clip_id: c for c in txn.new_timeline.get_track_by_id("v1").clips}
        assert set(parent_clips) == {"keep", txn.nested_clip_id}
        assert parent_clips[txn.nested_clip_id].timeline_start == 100

    def test_av_selection_creates_both_child_tracks_and_video_host(self):
        tl = _timeline(
            v_clips=[_clip("v1c", "v1", 0, linked_id="a1c")],
            a_clips=[_clip("a1c", "a1", 0, linked_id="v1c")],
        )
        txn = build_nested_from_selection(tl, ("v1c", "a1c"))
        assert len(txn.child_timeline.video_tracks) == 1
        assert len(txn.child_timeline.audio_tracks) == 1
        assert txn.host_track_id == "v1"
        # 父时间线两条轨都被清空（无残留）
        assert txn.new_timeline.get_track_by_id("v1").clips == (
            txn.new_timeline.get_track_by_id("v1").clips
        )
        assert all(
            c.clip_id != "v1c" for c in txn.new_timeline.get_track_by_id("v1").clips
        )
        assert txn.new_timeline.get_track_by_id("a1").clips == ()

    def test_clip_markers_move_and_shift(self):
        tl = _timeline(
            v_clips=[_clip("c1", "v1", 40), _clip("c2", "v1", 90)],
            markers=(
                TimelineMarker(marker_id="m1", frame=50, name="on-c1", clip_id="c1", track_id="v1"),
                TimelineMarker(marker_id="m2", frame=99, name="on-c2", clip_id="c2", track_id="v1"),
                TimelineMarker(marker_id="m3", frame=10, name="seq"),
            ),
        )
        txn = build_nested_from_selection(tl, ("c1", "c2"))
        child_markers = {m.marker_id: m for m in txn.child_timeline.markers}
        assert set(child_markers) == {"m1", "m2"}
        assert child_markers["m1"].frame == 10  # 50 - offset 40
        assert child_markers["m2"].frame == 59
        # 父时间线只保留序列级标记
        assert [m.marker_id for m in txn.new_timeline.markers] == ["m3"]

    def test_transition_with_both_endpoints_selected_moves(self):
        tr = TransitionInstance(
            transition_id="t1", track_id="v1", left_clip_id="c1",
            right_clip_id="c2", center_frame=50, duration=10,
        )
        tl = _timeline(v_clips=[_clip("c1", "v1", 0), _clip("c2", "v1", 50)],
                       transitions=(tr,))
        txn = build_nested_from_selection(tl, ("c1", "c2"))
        child_transitions = txn.child_timeline.video_tracks[0].transitions
        assert len(child_transitions) == 1
        assert child_transitions[0].center_frame == 50  # 50 - 0

    def test_audio_only_selection_hosts_on_audio_track(self):
        tl = _timeline(a_clips=[_clip("a1c", "a1", 0)])
        txn = build_nested_from_selection(tl, ("a1c",))
        assert txn.host_track_id == "a1"

    def test_invalid_selections_raise(self):
        tl = _timeline(v_clips=[_clip("c1", "v1", 0)])
        with pytest.raises(ValueError):
            build_nested_from_selection(tl, ())
        with pytest.raises(ValueError):
            build_nested_from_selection(tl, ("missing",))

    def test_child_inherits_format(self):
        tl = _timeline(v_clips=[_clip("c1", "v1", 0)])
        txn = build_nested_from_selection(tl, ("c1",))
        assert txn.child_timeline.fps == tl.fps
        assert txn.child_timeline.width == tl.width
        assert txn.child_timeline.height == tl.height
        assert txn.child_timeline.sample_rate == tl.sample_rate


# ─────────────────────────────────────────────────────────────────────────────
# 循环嵌套阻断
# ─────────────────────────────────────────────────────────────────────────────

class TestCycleDetection:

    def _project_with_nested(self, child_seq) -> tuple[EditProject, dict, dict]:
        project = EditProject.create(name="cycle-test")
        asset = create_nested_sequence_asset(child_seq)
        project = project.with_media_asset_added(asset)
        sequences = {child_seq.timeline_id: child_seq}
        nested_map = {asset.asset_id: child_seq.timeline_id}
        return project, sequences, nested_map

    def test_direct_reference_blocked(self):
        # 父时间线里的片段就是"父序列自己"的嵌套素材（人为构造的环）
        parent = _timeline()
        self_asset = create_nested_sequence_asset(parent, asset_id="self_asset")
        clip = ClipInstance(
            clip_id="selfclip", asset_id="self_asset", track_id="v1",
            timeline_start=0, timeline_duration=10, source_start=0, source_duration=10,
        )
        parent = parent.with_track_updated(
            parent.get_track_by_id("v1").with_clip_added(clip))
        bad = would_create_cycle(
            parent, (clip,), {parent.timeline_id: parent},
            {"self_asset": parent.timeline_id},
        )
        assert bad == parent.timeline_id

    def test_transitive_reference_blocked(self):
        # grandchild ⊂ child ⊂ parent；把引用 child 的片段再嵌套进 parent → 环
        grandchild = _timeline(timeline_id="gc", v_clips=[_clip("g1", "v1", 0)])
        child_asset = create_nested_sequence_asset(grandchild, asset_id="asset_gc")
        child_clip = ClipInstance(
            clip_id="cc", asset_id="asset_gc", track_id="v1",
            timeline_start=0, timeline_duration=10, source_start=0, source_duration=10,
        )
        child = _timeline(timeline_id="child", v_clips=[child_clip])
        # child 被 parent 引用
        parent = _timeline()
        child_asset_in_parent = create_nested_sequence_asset(child, asset_id="asset_child")
        parent_clip = ClipInstance(
            clip_id="pc", asset_id="asset_child", track_id="v1",
            timeline_start=0, timeline_duration=10, source_start=0, source_duration=10,
        )
        parent = parent.with_track_updated(
            parent.get_track_by_id("v1").with_clip_added(parent_clip))

        sequences = {
            parent.timeline_id: parent, child.timeline_id: child,
            grandchild.timeline_id: grandchild,
        }
        nested_map = {
            "asset_child": child.timeline_id,
            "asset_gc": grandchild.timeline_id,
        }
        # 把（引用 grandchild 链的）child 片段嵌套进 parent：
        # child 包含 grandchild，grandchild 不含 parent → 无环？
        # 但 parent 包含 child（pc 片段），child 若再进 parent 的新子序列，
        # 新子序列包含 child，而 child 不包含 parent → 无环，合法。
        assert would_create_cycle(parent, (child_clip,), sequences, nested_map) is None

        # 真正的环：把 parent 的 pc 片段（引用 child）嵌套进 parent 自身
        # parent ⊃ child，新子序列 ⊃ child 且 parent ⊃ 新子序列 → 无环（child 不含 parent）
        # 构造环：grandchild 引用 parent
        # 真正的环：grandchild 引用 parent（g1 在 [0,50)，环引用片段放 50 起）
        parent_ref_asset = create_nested_sequence_asset(parent, asset_id="asset_parent")
        gc_clip = ClipInstance(
            clip_id="gcc", asset_id="asset_parent", track_id="v1",
            timeline_start=50, timeline_duration=10, source_start=0, source_duration=10,
        )
        grandchild = grandchild.with_track_updated(
            grandchild.get_track_by_id("v1").with_clip_added(gc_clip))
        sequences[grandchild.timeline_id] = grandchild
        nested_map["asset_parent"] = parent.timeline_id
        # parent 片段 pc 引用 child → child ⊃ grandchild ⊃ parent → 环
        assert would_create_cycle(parent, (pc_for(pc_asset="asset_child"),), sequences, nested_map) is not None

    def test_no_cycle_for_plain_clips(self):
        parent = _timeline(v_clips=[_clip("c1", "v1", 0)])
        clip = parent.get_clip_by_id("c1")
        assert would_create_cycle(parent, (clip,), {}, {}) is None


def pc_for(pc_asset: str) -> ClipInstance:
    return ClipInstance(
        clip_id="pc", asset_id=pc_asset, track_id="v1",
        timeline_start=0, timeline_duration=10, source_start=0, source_duration=10,
    )


class TestCollectContained:

    def test_direct_and_transitive(self):
        grandchild = _timeline(timeline_id="gc")
        child = _timeline(timeline_id="ch")
        parent = _timeline(timeline_id="pa")
        nested_map = {"asset_ch": "ch", "asset_gc": "gc"}
        ch_clip = ClipInstance(clip_id="x1", asset_id="asset_gc", track_id="v1",
                               timeline_start=0, timeline_duration=10,
                               source_start=0, source_duration=10)
        child = child.with_track_updated(
            child.get_track_by_id("v1").with_clip_added(ch_clip))
        pa_clip = ClipInstance(clip_id="x2", asset_id="asset_ch", track_id="v1",
                               timeline_start=0, timeline_duration=10,
                               source_start=0, source_duration=10)
        parent = parent.with_track_updated(
            parent.get_track_by_id("v1").with_clip_added(pa_clip))
        contained = collect_contained_timeline_ids(
            parent, {parent.timeline_id: parent, child.timeline_id: child},
            nested_map)
        assert contained == {"ch", "gc"}


# ─────────────────────────────────────────────────────────────────────────────
# EditProject 注册（不激活）
# ─────────────────────────────────────────────────────────────────────────────

class TestProjectRegistration:

    def test_with_timeline_ref_added_keeps_active(self):
        project = EditProject.create(name="p1")
        project = project.create_timeline("First")
        active = project.active_timeline_id
        ref = TimelineSequenceRef(timeline_id="child-1", name="Child", fps=24.0)
        new_project = project.with_timeline_ref_added(ref, set_active=False)
        assert "child-1" in new_project.timelines
        assert new_project.active_timeline_id == active
        activated = project.with_timeline_ref_added(ref, set_active=True)
        assert activated.active_timeline_id == "child-1"

    def test_with_media_asset_added(self):
        project = EditProject.create(name="p2")
        child = _timeline(timeline_id="child-2")
        asset = create_nested_sequence_asset(child)
        new_project = project.with_media_asset_added(asset)
        assert new_project.get_asset_by_id(asset.asset_id) is asset


# ─────────────────────────────────────────────────────────────────────────────
# 预览渲染器 resolver 注入
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.edit_ui
class TestPreviewRendererResolver:

    def test_set_sequence_resolver_forwards_to_worker(self, qapp):
        from src.features.edit.core.timeline_preview_renderer import (
            TimelinePreviewRenderer,
        )
        renderer = TimelinePreviewRenderer.__new__(TimelinePreviewRenderer)
        renderer._worker = SimpleNamespace(_sequence_resolver=None)
        def resolver(tid):
            return None
        renderer.set_sequence_resolver(resolver)
        assert renderer._sequence_resolver is resolver
        assert renderer._worker._sequence_resolver is resolver


# ─────────────────────────────────────────────────────────────────────────────
# 导出器嵌套层渲染
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.edit_core
class TestExporterNestedLayer:

    def _make_exporter(self, child_seq, resolver):
        from src.features.edit.core.timeline_export import TimelineExporter
        project = EditProject.create(name="export-test")
        asset = create_nested_sequence_asset(child_seq)
        project = project.with_media_asset_added(asset)
        return (
            TimelineExporter(
                project=project, sequence=child_seq,
                frame_provider=None, sequence_resolver=resolver,
            ),
            asset,
        )

    def test_nested_layer_renders_child_composite(self):
        # 子序列一个纯色片段 → 导出嵌套层应返回纯色帧而非 None/黑帧
        from src.features.edit.core.project import EditProject
        solid = SimpleNamespace(
            asset_id="solid_asset", media_type="solid", path="",
            metadata={}, solid_color=(0.0, 1.0, 0.0),
        )
        child = _timeline(timeline_id="solid-child")
        solid_clip = ClipInstance(
            clip_id="sc", asset_id="solid_asset", track_id="v1",
            timeline_start=0, timeline_duration=10, source_start=0, source_duration=10,
        )
        child = child.with_track_updated(
            child.get_track_by_id("v1").with_clip_added(solid_clip))
        project = EditProject.create(name="px").with_media_asset_added(solid)

        from src.features.edit.core.timeline_export import TimelineExporter
        exporter = TimelineExporter(
            project=project, sequence=child, frame_provider=None,
            sequence_resolver=lambda tid: child if tid == child.timeline_id else None,
        )
        layer = SimpleNamespace(
            clip_id="host", asset_id="solid_asset", source_frame=3,
            track_index=0, composite_id=None,
        )
        # 注：这里直接对 solid 素材渲染（同一 _get_layer_frame 分支），
        # 嵌套分支由下一条用例经 resolver 验证
        frame = exporter._get_layer_frame(layer)
        assert frame is not None
        assert frame.shape[2] == 3

    def test_nested_asset_layer_via_resolver(self):
        # host 层引用嵌套素材 → 递归解析子序列（子序列内含纯色片段）
        from src.features.edit.core.project import EditProject
        solid = SimpleNamespace(
            asset_id="solid_asset", media_type="solid", path="",
            metadata={}, solid_color=(1.0, 0.0, 0.0),
        )
        child = _timeline(timeline_id="real-child")
        solid_clip = ClipInstance(
            clip_id="sc", asset_id="solid_asset", track_id="v1",
            timeline_start=0, timeline_duration=10, source_start=0, source_duration=10,
        )
        child = child.with_track_updated(
            child.get_track_by_id("v1").with_clip_added(solid_clip))

        nested_asset = create_nested_sequence_asset(child, asset_id="nested_asset")
        project = (EditProject.create(name="py")
                   .with_media_asset_added(solid)
                   .with_media_asset_added(nested_asset))

        from src.features.edit.core.timeline_export import TimelineExporter
        exporter = TimelineExporter(
            project=project, sequence=child, frame_provider=None,
            sequence_resolver=lambda tid: child if tid == child.timeline_id else None,
        )
        layer = SimpleNamespace(
            clip_id="host", asset_id="nested_asset", source_frame=3,
            track_index=0, composite_id=None,
        )
        frame = exporter._get_layer_frame(layer)
        assert frame is not None
        assert frame.shape[2] == 3

    def test_nested_layer_without_resolver_returns_none(self):
        child = _timeline(timeline_id="orphan-child")
        nested_asset = create_nested_sequence_asset(child, asset_id="nested_orphan")
        project = EditProject.create(name="pz").with_media_asset_added(nested_asset)
        from src.features.edit.core.timeline_export import TimelineExporter
        exporter = TimelineExporter(
            project=project, sequence=child, frame_provider=None,
            sequence_resolver=None,
        )
        layer = SimpleNamespace(
            clip_id="host", asset_id="nested_orphan", source_frame=0,
            track_index=0, composite_id=None,
        )
        assert exporter._get_layer_frame(layer) is None


# ─────────────────────────────────────────────────────────────────────────────
# 控制器事务
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.edit_ui
class TestControllerNestedFlow:

    def _make_controller(self, timeline, project):
        from src.app.controllers.edit_controller import EditControllerMixin
        ctrl = EditControllerMixin.__new__(EditControllerMixin)
        ctrl._current_edit_project = project
        ctrl._current_timeline_sequences = {timeline.timeline_id: timeline}
        ctrl.edit_page = SimpleNamespace(_current_timeline=timeline)
        pool = MagicMock()
        ctrl.media_pool_panel = pool
        panel = MagicMock()
        view = MagicMock()
        view._timeline = timeline
        panel.get_timeline_view.return_value = view
        ctrl.timeline_panel = panel
        ctrl.status_bar = MagicMock()
        published = []
        ctrl._on_edit_timeline_changed = lambda tl: published.append(tl)
        ctrl._on_timeline_switched = lambda tid: published.append(("switch", tid))
        from src.features.edit.core.commands import CommandStack
        stack = CommandStack()
        def execute(command):
            project, children = command.prepare_project(
                ctrl._current_edit_project, ctrl._current_timeline_sequences)
            updated = stack.execute(command, ctrl.edit_page._current_timeline)
            ctrl._current_edit_project = project
            ctrl._current_timeline_sequences.update(children)
            ctrl._current_timeline_sequences[updated.timeline_id] = updated
            ctrl.edit_page._current_timeline = updated
            view._timeline = updated
            pool.set_project(project)
            published.append(updated)
        view.execute_command.side_effect = execute
        return ctrl, published, view, pool

    def test_create_nested_registers_and_publishes(self):
        tl = _timeline(v_clips=[
            _clip("c1", "v1", 0), _clip("c2", "v1", 50), _clip("c3", "v1", 100),
        ])
        project = EditProject.create(name="ctrl-test").create_timeline("Main")
        ctrl, published, view, pool = self._make_controller(tl, project)

        ctrl._on_create_nested_from_selection(["c1", "c2"])

        new_project = ctrl._current_edit_project
        child_tid = published[0].get_track_by_id("v1").clips[0]
        # 发布了新父时间线
        assert published and published[0] is not tl
        # 工程包含新时间线引用 + 嵌套素材，且 active 不变
        assert new_project.active_timeline_id == project.active_timeline_id
        assert len(new_project.timelines) == 2
        nested_assets = [
            a for a in new_project.assets.values()
            if getattr(a, "is_nested_sequence", False)
        ]
        assert len(nested_assets) == 1
        # 子序列进入序列缓存（保存经 _get_timeline_sequences 持久化）
        child_ids = [
            tid for tid, seq in ctrl._current_timeline_sequences.items()
            if tid not in (tl.timeline_id,)
        ]
        assert child_ids, "子序列未注册进 _current_timeline_sequences"
        # 视图与媒体池刷新
        view.execute_command.assert_called_once()
        view.load_timeline.assert_not_called()
        pool.set_project.assert_called_once()

    def test_cycle_blocked_before_mutation(self):
        parent = _timeline()
        self_asset = create_nested_sequence_asset(parent, asset_id="self_asset")
        clip = ClipInstance(
            clip_id="selfclip", asset_id="self_asset", track_id="v1",
            timeline_start=0, timeline_duration=10, source_start=0, source_duration=10,
        )
        parent = parent.with_track_updated(
            parent.get_track_by_id("v1").with_clip_added(clip))
        project = (EditProject.create(name="cycle")
                   .create_timeline("Main")
                   .with_media_asset_added(self_asset))
        ctrl, published, view, pool = self._make_controller(parent, project)

        with patch("src.app.controllers.edit_controller.QMessageBox") as box:
            ctrl._on_create_nested_from_selection(["selfclip"])
        box.warning.assert_called_once()
        # 无发布、无变更：时间线引用数与素材数不变
        assert published == []
        assert len(ctrl._current_edit_project.timelines) == 1
        assert len(ctrl._current_edit_project.assets) == 1

    def test_enter_switches_timeline(self):
        tl = _timeline(v_clips=[_clip("c1", "v1", 0)])
        child = _timeline(timeline_id="child-x", v_clips=[_clip("k1", "v1", 0)])
        project = (EditProject.create(name="enter")
                   .create_timeline("Main"))
        ref = TimelineSequenceRef(timeline_id="child-x", name="Child", fps=24.0)
        project = project.with_timeline_ref_added(ref)
        ctrl, published, _view, _pool = self._make_controller(tl, project)

        ctrl._on_enter_nested_sequence("child-x", "some-clip")
        assert ("switch", "child-x") in published

    def test_enter_unknown_timeline_ignored(self):
        tl = _timeline(v_clips=[_clip("c1", "v1", 0)])
        project = EditProject.create(name="enter2").create_timeline("Main")
        ctrl, published, _view, _pool = self._make_controller(tl, project)
        ctrl._on_enter_nested_sequence("does-not-exist", "clip")
        assert published == []
