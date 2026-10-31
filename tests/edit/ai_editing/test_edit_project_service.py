"""TAEP Phase 0 — EditProjectService 提交基石测试。

验证：
- revision 字段默认 0 且可序列化 round-trip。
- ``commit_command`` 是 UI/AI/脚本的统一入口：apply → validate → bump revision
  → sync ref↔sequence 两层。
- 乐观锁（base_revision 冲突）折叠为 ``applied=False`` 的 CommitResult
  （AUD-13406：统一错误通道，错误信息含 base/current；旧断言"抛
  RevisionConflict"已被锁定旧行为协议翻正）。
- domain 校验失败（transition 引用不存在 clip）时 ``applied=False`` 且两层不变。
- preview/dry-run 不 bump revision、不 sync。
- command.apply 内的 overlap 错误被捕获为 ``applied=False``。

参考：docs/ai_editing_protocol_and_implementation_plan_2026-07-31.md §2.1, §6.1, §6.2
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing import (
    EditProjectService,
    ProjectSequences,
    RevisionConflict,
)
from src.features.edit.core.clip_params import CropParams, SpeedParams, TransformParams
from src.features.edit.core.commands import AddClipCommand, MacroCommand
from src.features.edit.core.project import EditProject, TimelineSequenceRef
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.media.metadata import Rational


# ================================================================
# Fixtures
# ================================================================


def _make_clip(clip_id: str, track_id: str, start: int = 0, duration: int = 100) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id="asset-1",
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=0,
        source_duration=duration,
        speed=SpeedParams(),
        transform=TransformParams(),
        crop=CropParams(),
    )


def _make_timeline() -> TimelineSequence:
    tid = str(uuid.uuid4())
    track = EditTrack(
        track_id="v1",
        name="V1",
        track_type="video",
        index=0,
        clips=(),
    )
    return TimelineSequence(
        timeline_id=tid,
        name="Main",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        sample_rate=48000,
        video_tracks=(track,),
    )


@pytest.fixture
def state() -> ProjectSequences:
    """两层状态：project 持 ref，sequences map 持完整 sequence。"""
    seq = _make_timeline()
    ref = TimelineSequenceRef(
        timeline_id=seq.timeline_id,
        name=seq.name,
        fps=float(seq.fps),
        width=seq.width,
        height=seq.height,
        sample_rate=seq.sample_rate,
        format_settings=dict(seq.format_settings),
    )
    project = EditProject(
        project_id=str(uuid.uuid4()),
        name="AI Test Project",
        timelines={seq.timeline_id: ref},
        root_bin_id="root",
    )
    return ProjectSequences(project=project, sequences={seq.timeline_id: seq})


# ================================================================
# revision 字段
# ================================================================


def test_timeline_sequence_revision_defaults_zero():
    seq = TimelineSequence(name="t")
    assert seq.revision == 0
    bumped = seq.with_revision_bumped()
    assert bumped.revision == 1
    # immutable: original unchanged
    assert seq.revision == 0


def test_timeline_sequence_rebuild_preserves_revision():
    seq = TimelineSequence(name="t").with_revision_bumped()
    assert seq.revision == 1
    # _rebuild 路径（with_master_volume）不 bump
    seq2 = seq.with_master_volume(0.5)
    assert seq2.revision == 1


def test_timeline_sequence_revision_round_trip():
    seq = TimelineSequence(name="t").with_revision_bumped().with_revision_bumped()
    d = seq.to_dict()
    assert d["revision"] == 2
    rt = TimelineSequence.from_dict(d)
    assert rt.revision == 2
    # legacy data without revision field defaults to 0
    legacy = TimelineSequence.from_dict({"timeline_id": "x", "name": "old"})
    assert legacy.revision == 0


def test_edit_project_revision_defaults_zero():
    p = EditProject.create("Test")
    assert p.revision == 0


# ================================================================
# commit_command: 统一入口
# ================================================================


def test_commit_command_bumps_both_revisions_and_syncs_ref(state):
    tid = state.project.active_timeline_id or next(iter(state.sequences))
    base_revision = state.sequences[tid].revision  # 0
    clip = _make_clip("c1", "v1")
    cmd = AddClipCommand.create("v1", clip)

    result = EditProjectService.commit_command(
        state, tid, cmd, base_revision=base_revision, undo_label="Add clip"
    )

    assert result.applied is True
    assert result.error is None
    # timeline revision bumped
    assert result.new_timeline is not None
    assert result.new_timeline.revision == base_revision + 1
    assert result.undo_label == "Add clip"
    # project revision bumped
    assert result.new_project.revision == state.project.revision + 1
    # sequence map updated
    assert result.new_sequences[tid].revision == base_revision + 1
    assert len(result.new_sequences[tid].video_tracks[0].clips) == 1
    # ref synced (key consistency preserved)
    assert set(result.new_project.timelines) == set(result.new_sequences)
    # original state untouched (immutable)
    assert state.sequences[tid].revision == base_revision


def test_commit_command_without_base_revision_does_not_check_lock(state):
    """UI 直接交互路径可不传 base_revision（用户最新操作定义真相）。"""
    tid = next(iter(state.sequences))
    clip = _make_clip("c1", "v1")
    cmd = AddClipCommand.create("v1", clip)

    result = EditProjectService.commit_command(state, tid, cmd)  # no base_revision
    assert result.applied is True
    assert result.new_sequences[tid].revision == 1


def test_commit_command_revision_conflict_raises(state):
    """AUD-13406：revision 冲突与 domain 失败同通道——CommitResult(applied=False)。

    旧断言（pytest.raises(RevisionConflict)）锁定的是 docstring 与实现矛盾
    的旧通道；按锁定旧行为协议翻正：冲突现在折叠为 ``applied=False``，错误
    文本含 base/current 两个 revision，两层状态不变。
    """
    tid = next(iter(state.sequences))
    current = state.sequences[tid].revision  # 0
    clip = _make_clip("c1", "v1")
    cmd = AddClipCommand.create("v1", clip)

    # AI plan was built against revision 5, but current is 0
    result = EditProjectService.commit_command(state, tid, cmd, base_revision=5)
    assert result.applied is False
    assert result.error is not None
    assert "5" in result.error and str(current) in result.error
    assert any("revision" in e.lower() for e in result.errors)
    # two layers unchanged
    assert state.sequences[tid].revision == current
    # RevisionConflict 类仍是公共 API（结构化字段保留，供深层调用方）。
    conflict = RevisionConflict(5, current, timeline_id=tid)
    assert conflict.base_revision == 5
    assert conflict.current_revision == current


def test_commit_command_macro_command_single_undo(state):
    """MacroCommand 把整个 plan 变成一个命名 undo group（单 revision bump）。"""
    tid = next(iter(state.sequences))
    base_revision = state.sequences[tid].revision
    c1 = _make_clip("c1", "v1", start=0, duration=100)
    c2 = _make_clip("c2", "v1", start=100, duration=100)
    cmd1 = AddClipCommand.create("v1", c1)
    cmd2 = AddClipCommand.create("v1", c2)
    macro = MacroCommand.create("AI plan: add two clips", (cmd1, cmd2))

    result = EditProjectService.commit_command(
        state, tid, macro, base_revision=base_revision, undo_label="AI plan"
    )
    assert result.applied is True
    assert result.new_timeline.revision == base_revision + 1  # single bump
    track = result.new_sequences[tid].video_tracks[0]
    assert len(track.clips) == 2


def test_commit_command_overlap_returns_not_applied(state):
    """command.apply 内的 overlap 错误被捕获，applied=False，两层不变。"""
    tid = next(iter(state.sequences))
    # First add a clip at [0,100)
    c1 = _make_clip("c1", "v1", start=0, duration=100)
    state2 = ProjectSequences(
        project=state.project,
        sequences={tid: state.sequences[tid].with_track_updated(
            EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=(c1,))
        )},
    )
    # Now try to add an overlapping clip — AddClipCommand enforces no-overlap
    c_overlap = _make_clip("c2", "v1", start=50, duration=100)
    cmd = AddClipCommand.create("v1", c_overlap)

    result = EditProjectService.commit_command(state2, tid, cmd, base_revision=0)
    assert result.applied is False
    assert result.error is not None
    # two layers unchanged
    assert result.new_project is state2.project or result.new_project.revision == state2.project.revision
    assert len(result.new_sequences[tid].video_tracks[0].clips) == 1


def test_commit_command_missing_sequence_returns_error(state):
    """key 一致性：timeline ref 存在但 sequence 未物化 -> 明确错误。"""
    # add a ref without a sequence
    orphan_ref = TimelineSequenceRef(
        timeline_id="orphan", name="Orphan", fps=24.0, width=1920, height=1080, sample_rate=48000
    )
    from dataclasses import replace

    bad_state = ProjectSequences(
        project=replace(state.project, timelines={**state.project.timelines, "orphan": orphan_ref}),
        sequences=dict(state.sequences),  # missing "orphan"
    )
    clip = _make_clip("c1", "v1")
    cmd = AddClipCommand.create("v1", clip)

    result = EditProjectService.commit_command(bad_state, "orphan", cmd)
    assert result.applied is False
    assert "not been materialized" in (result.error or "") or "inconsistent" in (result.error or "")


def test_commit_command_ref_metadata_drift_synced(state):
    """若 timeline 的 name/fps 漂移，ref 层必须被重建并保持一致。"""
    tid = next(iter(state.sequences))

    # 模拟“command 产生了 name 漂移的 sequence”：用 with_track_updated 加 clip，
    # 但直接构造一个 rename 后的 sequence 来触发 ref 反向同步。
    from src.features.edit.core.timeline_v2 import EditTrack

    clip = _make_clip("c1", "v1")
    base_seq = state.sequences[tid]
    new_track = EditTrack(
        track_id="v1", name="V1", track_type="video", index=0, clips=(clip,)
    )
    renamed_seq = base_seq.with_track_updated(new_track)._rebuild(name="Renamed by AI")

    # 直接调用内部 sync helper，验证 ref 被重建为新的 name
    new_project, new_sequences = EditProjectService._sync_two_layers(state, tid, renamed_seq)

    new_ref = new_project.timelines[tid]
    assert new_ref.name == "Renamed by AI"  # ref 反向同步
    assert new_ref.fps == float(renamed_seq.fps)
    # project revision bumped
    assert new_project.revision == state.project.revision + 1
    # key consistency
    assert set(new_project.timelines) == set(new_sequences)


# ================================================================
# preview / dry-run
# ================================================================


def test_preview_command_does_not_bump_or_sync(state):
    tid = next(iter(state.sequences))
    base_revision = state.sequences[tid].revision
    clip = _make_clip("c1", "v1")
    cmd = AddClipCommand.create("v1", clip)

    proposed, errors = EditProjectService.preview_command(state, tid, cmd)
    assert errors == []
    # preview does NOT bump revision
    assert proposed.revision == base_revision
    # proposed has the clip applied
    assert len(proposed.video_tracks[0].clips) == 1
    # original state untouched
    assert state.sequences[tid].revision == base_revision
    assert len(state.sequences[tid].video_tracks[0].clips) == 0
