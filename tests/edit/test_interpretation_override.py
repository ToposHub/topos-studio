"""F2.7 · 素材解释覆盖——模型/命令/持久化/插入消费/片段重锚。

验收锁定：
- 原始探测不改写（metadata["fps"] 原样，覆盖独立存放）；
- 单事务（宏单步撤销）+ 逐项失败报告（缺失素材不入事务）；
- 撤销/重开恢复（命令 undo + 工程载荷 roundtrip）；
- 失败不留下半更新（apply 抛错不入栈）；
- 解释变更清代理状态（代理/原片切换不串解释）；
- 新插入按生效帧率建时基快照；既有片段重锚保持源帧窗重算时长。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.commands import (
    CommandStack,
    ProjectMacroCommand,
    UpdateAssetInterpretationCommand,
    UpdateClipFpsInterpretationCommand,
)
from src.features.edit.core.interpretation import (
    InterpretationOverride,
    effective_asset_fps,
    fps_as_fraction,
    interpretation_for_asset,
)
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)


def _video_asset(asset_id: str = "v25", fps: float = 25.0,
                 with_proxy: bool = False) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id, path=f"/tmp/{asset_id}.mp4", media_type="video",
        display_name=asset_id,
        metadata={"fps": fps, "duration": 10.0},
        proxy_path="/tmp/proxy.mov" if with_proxy else None,
        proxy_status="ready" if with_proxy else "none",
    )


def _project(*assets: MediaAsset) -> EditProject:
    return EditProject(
        "p", "P",
        assets={a.asset_id: a for a in assets})


def _timeline_with_clip(asset_id: str, source_fps_num: int = 25) -> TimelineSequence:
    # 100 源帧 @25fps → 24fps 时间线 = 96 帧（与插入时统一换算一致）
    clip = ClipInstance(
        "c1", asset_id, "v1", 0, 96, 0, 100,
        _source_fps_num=source_fps_num, _source_fps_den=1,
        _timeline_fps_num=24, _timeline_fps_den=1)
    return TimelineSequence(
        timeline_id="t1", name="T", fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(clip,)),))


class TestInterpretationModel:
    def test_override_wins_over_probe(self) -> None:
        asset = _video_asset()
        overrides = {"v25": InterpretationOverride(fps=24.0)}
        assert effective_asset_fps(asset, overrides) == 24.0
        assert asset.metadata["fps"] == 25.0  # 探测不改写

    def test_no_override_falls_back_to_probe(self) -> None:
        asset = _video_asset()
        assert effective_asset_fps(asset, {}) == 25.0
        assert effective_asset_fps(asset, {"v25": InterpretationOverride()}) \
            == 25.0
        assert interpretation_for_asset({}, "v25").is_empty()

    def test_validate_rejects_out_of_range(self) -> None:
        with pytest.raises(ValueError):
            InterpretationOverride(fps=0.0).validate()
        with pytest.raises(ValueError):
            InterpretationOverride(fps=500.0).validate()

    def test_dict_roundtrip(self) -> None:
        override = InterpretationOverride(fps=23.976)
        assert InterpretationOverride.from_dict(override.to_dict()) == override


class TestInterpretationCommand:
    def test_apply_undo_roundtrip(self) -> None:
        project = _project(_video_asset(with_proxy=True))
        cmd = UpdateAssetInterpretationCommand.create(
            "v25", InterpretationOverride(fps=24.0))
        applied = cmd.apply(project)
        assert effective_asset_fps(
            applied.assets["v25"], applied.interpretation_overrides) == 24.0
        # 代理失效（解释变更不串旧代理）
        assert applied.assets["v25"].proxy_status == "none"
        assert applied.assets["v25"].proxy_path is None
        undone = cmd.undo(applied)
        assert undone.interpretation_overrides == {}
        assert undone.assets["v25"].proxy_status == "ready"
        assert undone.assets["v25"].proxy_path == "/tmp/proxy.mov"

    def test_clear_with_none(self) -> None:
        project = _project(_video_asset())
        project = UpdateAssetInterpretationCommand.create(
            "v25", InterpretationOverride(fps=24.0)).apply(project)
        cleared = UpdateAssetInterpretationCommand.create(
            "v25", None).apply(project)
        assert cleared.interpretation_overrides == {}

    def test_invalid_value_rejected_no_partial_write(self) -> None:
        project = _project(_video_asset())
        cmd = UpdateAssetInterpretationCommand.create(
            "v25", InterpretationOverride(fps=999.0))
        with pytest.raises(ValueError):
            cmd.apply(project)
        assert project.interpretation_overrides == {}  # 零半更新

    def test_unknown_asset_raises(self) -> None:
        cmd = UpdateAssetInterpretationCommand.create(
            "missing", InterpretationOverride(fps=24.0))
        with pytest.raises(ValueError):
            cmd.apply(_project(_video_asset()))


class TestBatchInterpretTransaction:
    def test_macro_single_undo(self) -> None:
        project = _project(_video_asset("a1"), _video_asset("a2"))
        macro = ProjectMacroCommand.create("批量解释帧率", [
            UpdateAssetInterpretationCommand.create(
                "a1", InterpretationOverride(fps=24.0)),
            UpdateAssetInterpretationCommand.create(
                "a2", InterpretationOverride(fps=24.0)),
        ])
        applied = macro.apply(project)
        assert len(applied.interpretation_overrides) == 2
        undone = macro.undo(applied)
        assert undone.interpretation_overrides == {}

    def test_missing_assets_excluded_with_report_semantics(self) -> None:
        """逐项失败报告：缺失素材不进事务（命令对存在素材成宏）。"""
        project = _project(_video_asset("a1"))
        existing = [aid for aid in ("a1", "missing")
                    if aid in project.assets]
        missing = [aid for aid in ("a1", "missing")
                   if aid not in project.assets]
        assert existing == ["a1"] and missing == ["missing"]
        macro = ProjectMacroCommand.create("批量解释帧率", [
            UpdateAssetInterpretationCommand.create(
                aid, InterpretationOverride(fps=24.0))
            for aid in existing])
        applied = macro.apply(project)
        assert set(applied.interpretation_overrides) == {"a1"}


class TestPersistenceRoundtrip:
    def test_settings_roundtrip(self) -> None:
        from src.features.edit.core.edit_project_io import (
            _deserialize_interpretation_overrides,
            _serialize_interpretation_overrides,
        )
        overrides = {"a1": InterpretationOverride(fps=24.0)}
        saved = _serialize_interpretation_overrides(overrides)
        assert saved == {"a1": {"fps": 24.0}}
        restored = _deserialize_interpretation_overrides(saved)
        assert restored == overrides
        # 未知载荷保留原样
        garbage = {"legacy": {"weird": True}, "junk": 7}
        loaded = _deserialize_interpretation_overrides(garbage)
        assert loaded == garbage
        assert interpretation_for_asset(loaded, "legacy").is_empty()


class TestInsertConsumesEffectiveFps:
    def test_insert_gate_uses_override(self, qapp, qtbot) -> None:
        """新插入按解释覆盖建 fps 快照（25 探测解释为 24 → 快照 24）。"""
        from src.features.edit.ui.timeline_view import TimelineView

        project = _project(_video_asset("v25", fps=25.0))
        project = UpdateAssetInterpretationCommand.create(
            "v25", InterpretationOverride(fps=24.0)).apply(project)
        view = TimelineView()
        qtbot.addWidget(view)
        view.load_timeline(TimelineSequence(
            timeline_id="t", name="T", fps=Rational(24, 1),
            video_tracks=(EditTrack("v1", "V1", "video", 0),)))
        view.set_project(project)

        num, den, _tn, _td = view._resolve_source_fps_metadata("v25")
        assert (num, den) == (24, 1)

        assert view.insert_clip("v25", 0, 47, playhead=0) is True
        clip = view._timeline.video_tracks[0].clips[0]
        assert (clip._source_fps_num, clip._source_fps_den) == (24, 1)


class TestClipReinterpretCommand:
    def test_reeanctor_keeps_source_window_recomputes_duration(self) -> None:
        """重锚：源帧窗不变，时长按新解释重算（保持 100 帧：96→100@24fps）。"""
        timeline = _timeline_with_clip("v25", source_fps_num=25)
        num, den = fps_as_fraction(24.0)
        stack = CommandStack()
        cmd = UpdateClipFpsInterpretationCommand.create(
            "v1", "c1", num, den)
        updated = stack.execute(cmd, timeline)
        clip = updated.get_clip_by_id("c1")
        assert clip.source_duration == 100  # 源帧窗不动（DaVinci 语义）
        assert (clip._source_fps_num, clip._source_fps_den) == (24, 1)
        assert clip.timeline_duration == 100  # 同帧数@同帧率 = 1:1
        # 单步撤销恢复
        restored = stack.undo(updated)
        clip2 = restored.get_clip_by_id("c1")
        assert (clip2._source_fps_num, clip2._source_fps_den) == (25, 1)
        assert clip2.timeline_duration == 96

    def test_locked_track_rejected(self) -> None:
        timeline = _timeline_with_clip("v25")
        timeline = timeline._rebuild(video_tracks=(EditTrack(
            "v1", "V1", "video", 0,
            clips=timeline.video_tracks[0].clips, is_locked=True),))
        cmd = UpdateClipFpsInterpretationCommand.create("v1", "c1", 24, 1)
        with pytest.raises(ValueError, match="locked"):
            cmd.apply(timeline)

    def test_dict_roundtrip(self) -> None:
        cmd = UpdateClipFpsInterpretationCommand.create("v1", "c1", 24, 1)
        restored = UpdateClipFpsInterpretationCommand.from_dict(cmd.to_dict())
        assert (restored.new_fps_num, restored.new_fps_den) == (24, 1)
        assert restored.track_id == "v1" and restored.clip_id == "c1"
