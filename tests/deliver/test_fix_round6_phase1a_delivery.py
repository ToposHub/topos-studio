"""第六轮审计 阶段1A 回归：交付主链路（U01/U02/U03）。

U01：自定义范围导出误判零帧——preflight 上下文读取模型不存在的
``frame_count`` 而缺省 0。修复后以 ``duration_frames`` 为权威字段，
Custom/In-Out 范围不再被阻断，真正越界仍阻断。

U02：嵌套序列导出宿主未传全量序列——DeliverWorkspace 缺
``set_timeline_sequences``、JobContext/Runner 不携带序列 map。修复后
workspace 接收完整序列 map 并 promote 进 batch controller，导出上下文
带 ``timeline_sequences``。

U03：非当前时间线导出拿到轻量 TimelineSequenceRef——combo 选择只改显示
不切序列、controller 登记了 Ref。修复后 combo 按选择解析完整目标序列，
Ref 不再进入队列上下文。

全部用真实 TimelineSequence/EditTrack/ClipInstance 数据类夹具
（第六轮 §7：不得只用 SimpleNamespace 测宿主装配语义）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtWidgets import QApplication

from src.features.deliver.core.video_batch_export_controller import (
    VideoBatchExportController,
)
from src.features.deliver.core.video_export_job import (
    AudioConfig,
    RangeType,
    VideoConfig,
    VideoExportJob,
)
from src.features.edit.core.project import TimelineSequenceRef
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.shared.media.metadata import Rational


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _sequence(
    timeline_id: str,
    name: str,
    duration: int,
    *,
    with_audio: bool = False,
) -> TimelineSequence:
    """Build a real TimelineSequence whose tracks span ``duration`` frames."""
    video_clip = ClipInstance(
        clip_id=f"{timeline_id}-v0",
        asset_id="asset-video",
        track_id=f"{timeline_id}-vt0",
        timeline_start=0,
        timeline_duration=duration,
        source_start=0,
        source_duration=duration,
    )
    video_track = EditTrack(
        track_id=f"{timeline_id}-vt0",
        name="V1",
        track_type="video",
        index=0,
        clips=(video_clip,),
    )
    audio_tracks: tuple[EditTrack, ...] = ()
    if with_audio:
        audio_clip = ClipInstance(
            clip_id=f"{timeline_id}-a0",
            asset_id="asset-audio",
            track_id=f"{timeline_id}-at0",
            timeline_start=0,
            timeline_duration=duration,
            source_start=0,
            source_duration=duration,
        )
        audio_tracks = (
            EditTrack(
                track_id=f"{timeline_id}-at0",
                name="A1",
                track_type="audio",
                index=0,
                clips=(audio_clip,),
            ),
        )
    return TimelineSequence(
        timeline_id=timeline_id,
        name=name,
        width=1920,
        height=1080,
        fps=Rational(25, 1),
        video_tracks=(video_track,),
        audio_tracks=audio_tracks,
    )


# ── U01：自定义/入出点范围不再被误判零帧 ──


class TestU01CustomRangePreflight:
    @pytest.fixture
    def panel(self, qapp, tmp_path):
        from src.features.deliver.ui.video_export_settings_panel import (
            VideoExportSettingsPanel,
        )

        panel = VideoExportSettingsPanel()
        seq = _sequence("seq-u01", "Main", 300)
        panel.set_project(object(), project_id="p1", project_name="Proj")
        panel.set_timelines([("seq-u01", "Main")])
        panel.set_timeline_sequences({"seq-u01": seq})
        panel.set_sequence(seq)
        panel._output_dir_edit.setText(str(tmp_path))
        yield panel
        panel.close()

    def test_custom_range_within_timeline_builds_jobs(self, panel):
        """300 帧时间线 + Custom 0–50：不得因 “0 frames” 阻断（U01）。"""
        panel._select_combo_by_data(panel._range_combo, RangeType.CUSTOM)
        panel._start_frame_spin.setValue(0)
        panel._end_frame_spin.setValue(50)

        jobs, result = panel.build_jobs(preflight=True)
        assert jobs, f"Custom 0–50 应成功构建任务，被阻断: {[i.message for i in (result.issues if result else [])]}"
        assert jobs[0].start_frame == 0
        assert jobs[0].end_frame == 50

    def test_in_out_range_respects_sequence_marks(self, panel):
        seq = panel._sequence
        object.__setattr__  # noqa: B018 — dataclass immutable via replace below
        from dataclasses import replace

        panel.set_sequence(replace(seq, range_start=10, range_end=99))
        panel._select_combo_by_data(panel._range_combo, RangeType.IN_OUT)
        jobs, _ = panel.build_jobs(preflight=True)
        assert jobs, "In/Out 范围应成功构建任务"

    def test_custom_range_out_of_bounds_still_blocks(self, panel):
        """真正越界（start ≥ 时长）仍必须阻断——修复不是放开校验。"""
        panel._select_combo_by_data(panel._range_combo, RangeType.CUSTOM)
        panel._start_frame_spin.setValue(300)
        panel._end_frame_spin.setValue(350)

        jobs, result = panel.build_jobs(preflight=True)
        assert not jobs
        assert result is not None
        assert any(i.code == "range.start_out_of_bounds" for i in result.blocking)

    def test_effective_frame_count_reads_duration_frames(self, panel):
        """preflight 上下文读 ``duration_frames``（300），而不是缺失的
        ``frame_count``（旧代码缺省 0）。"""
        ctx = panel._build_preflight_context(None)
        assert ctx.timeline_frame_count == 300

    def test_frame_count_absent_returns_none_not_zero(self, panel, qapp):
        """模型两字段都没有时返回 None（跳过越界检查），不臆造 0。"""
        stub = SimpleNamespace(name="stub")  # 无 duration_frames/frame_count
        assert panel._effective_timeline_frame_count(stub) is None


# ── U02：嵌套导出全量序列贯通 workspace → controller → JobContext ──


class TestU02NestedSequencePlumbing:
    def test_workspace_set_timeline_sequences_forwards_and_promotes(self, qapp):
        from src.features.deliver.ui.deliver_workspace import DeliverWorkspace

        workspace = DeliverWorkspace()
        try:
            parent = _sequence("seq-parent", "Parent", 100, with_audio=True)
            child = _sequence("seq-child", "Child", 50, with_audio=True)
            project = SimpleNamespace(
                project_id="p1",
                name="Proj",
                revision=1,
                timelines={
                    "seq-parent": TimelineSequenceRef(
                        timeline_id="seq-parent", name="Parent", fps=25.0
                    ),
                    "seq-child": TimelineSequenceRef(
                        timeline_id="seq-child", name="Child", fps=25.0
                    ),
                },
                active_timeline=None,
                sequences=None,
            )
            workspace.set_project(project)
            # 宿主（page_controller）在进入 Deliver 时传入完整序列 map。
            workspace.set_timeline_sequences(
                {"seq-parent": parent, "seq-child": child}
            )

            # settings panel 的 nested resolver 与真实导出同源。
            resolver = workspace._settings_panel._sequence_resolver()
            assert resolver is not None
            assert resolver("seq-child") is child

            # controller 队列上下文 promote 成完整序列（不再是 Ref）。
            by_id = workspace._controller._sequences_by_id
            assert by_id.get("seq-parent") is parent
            assert by_id.get("seq-child") is child
        finally:
            workspace.close()

    def test_resolve_context_carries_timeline_sequences_map(self):
        controller = VideoBatchExportController()
        parent = _sequence("seq-parent", "Parent", 100, with_audio=True)
        child = _sequence("seq-child", "Child", 50, with_audio=True)
        controller.set_active_project(
            object(), project_id="p1", sequences=[parent, child]
        )

        job = VideoExportJob(
            timeline_id="seq-parent",
            output_path="/tmp/nest.mov",
            video_config=VideoConfig(codec="prores_422", container="mov"),
            audio_config=AudioConfig(include_audio=True, codec="aac"),
        )
        ctx = controller._resolve_context(job)
        assert ctx.is_resolvable
        # U02：Runner 侧导出需要这个 map 解析嵌套子序列。
        assert ctx.timeline_sequences is not None
        assert ctx.timeline_sequences["seq-child"] is child
        assert ctx.timeline_sequences["seq-parent"] is parent


# ── U03：combo 选择非当前时间线时解析完整目标序列 ──


class TestU03OtherTimelineSelection:
    @pytest.fixture
    def panel(self, qapp, tmp_path):
        from src.features.deliver.ui.video_export_settings_panel import (
            VideoExportSettingsPanel,
        )

        panel = VideoExportSettingsPanel()
        self.seq_a = _sequence("seq-a", "Alpha", 500, with_audio=True)
        self.seq_b = _sequence("seq-b", "Beta", 300, with_audio=True)
        panel.set_project(object(), project_id="p1", project_name="Proj")
        panel.set_timelines([("seq-a", "Alpha"), ("seq-b", "Beta")])
        panel.set_timeline_sequences({"seq-a": self.seq_a, "seq-b": self.seq_b})
        # 当前 Program 在 B（宿主先安装 B）。
        panel.set_sequence(self.seq_b)
        panel._output_dir_edit.setText(str(tmp_path))
        yield panel
        panel.close()

    def test_combo_switch_installs_full_target_sequence(self, panel):
        """Program 处于 B 时 combo 只选 A：draft 序列必须切到完整 A。"""
        panel.set_active_timeline("seq-a")
        assert panel._sequence is self.seq_a

        jobs, _ = panel.build_jobs(preflight=False)
        assert jobs[0].timeline_id == "seq-a"
        assert panel._build_preflight_context(jobs[0]).sequence is self.seq_a

    def test_alternating_selection_resolves_each_target(self, panel):
        for target in ("seq-a", "seq-b", "seq-a"):
            panel.set_active_timeline(target)
            expected = self.seq_a if target == "seq-a" else self.seq_b
            assert panel._sequence is expected
            jobs, _ = panel.build_jobs(preflight=False)
            assert jobs[0].timeline_id == target
            ctx = panel._build_preflight_context(jobs[0])
            assert getattr(ctx.sequence, "timeline_id") == target

    def test_ref_only_selection_does_not_masquerade(self, panel):
        """combo 选到没有完整序列的时间线时不得用其他序列冒充。

        live 序列与选择同 id 时返回 live 是正确的（它就是完整序列）；
        这里验证真正的冒充场景——map 缺失 + live 是另一条时间线。
        """
        # live 是 seq-b（fixture 已同步 combo → seq-b）。
        assert panel._sequence_for_combo_selection() is self.seq_b
        # 清空 map 后 combo 切到 seq-a：无完整序列来源，live 不得冒充。
        panel.set_timeline_sequences({})
        panel.set_active_timeline("seq-a")
        assert panel._sequence is self.seq_b, "无解析来源时不得安装其他序列"
        assert panel._sequence_for_combo_selection() is None

    def test_panel_sequence_install_syncs_combo(self, panel):
        """宿主安装 live 序列时 combo 同步指向该时间线（U03 前置条件）。"""
        panel.set_sequence(self.seq_a)
        assert panel._timeline_combo.currentData() == "seq-a"
        # combo 已在 seq-a 时再点选同一项不触发信号，但 draft 序列已正确。
        panel.set_active_timeline("seq-a")
        assert panel._sequence is self.seq_a

    def test_controller_rejects_ref_registration(self):
        """Ref 不是可导出序列：register/set_active_project 均不得登记。"""
        controller = VideoBatchExportController()
        ref = TimelineSequenceRef(timeline_id="seq-ref", name="Ref", fps=25.0)
        controller.set_active_project(
            object(), project_id="p1", sequences=[ref]
        )
        assert controller._sequences_by_id == {}

        controller.register_sequence(ref)
        assert controller._sequences_by_id == {}

        full = _sequence("seq-full", "Full", 10)
        controller.register_sequence(full)
        assert controller._sequences_by_id.get("seq-full") is full

    def test_resolve_context_for_unregistered_ref_reports_missing(self):
        controller = VideoBatchExportController()
        controller.set_active_project(object(), project_id="p1", sequences=[])
        job = VideoExportJob(
            timeline_id="seq-ref",
            timeline_name="Ref",
            output_path="/tmp/x.mov",
            video_config=VideoConfig(codec="prores_422", container="mov"),
        )
        ctx = controller._resolve_context(job)
        assert not ctx.is_resolvable
        assert "not found" in ctx.reason
