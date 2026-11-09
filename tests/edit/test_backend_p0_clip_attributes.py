"""P0-3 Clip Attributes / Interpret Media——后端核验验收。

对应《媒体池后端交接计划》P0 第 3 项（复用并核验）。既有专项套件：
- tests/edit/test_interpretation_override.py（覆盖优先/撤销/插入门/重锚）
- tests/edit/test_m04_interpretation_matrix.py（消费矩阵诚实声明）
- tests/edit/test_source_patch_model.py（音频映射 apply/undo）
本套件补跨域验收链：Clip Attributes 四个域（制作信息/日志/解释覆盖/
Source Patch）在同一命令栈全部可撤销；帧率重锚不改动导出侧消费的
源帧窗字段；全部属性随工程序列化往返。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.commands import (
    ProjectCommandStack,
    UpdateAssetInterpretationCommand,
    UpdateAssetLogCommand,
    UpdateAssetProductionInfoCommand,
    UpdateClipFpsInterpretationCommand,
    UpdateSourcePatchCommand,
)
from src.features.edit.core.interpretation import (
    InterpretationOverride,
    effective_asset_fps,
    fps_as_fraction,
)
from src.features.edit.core.project import EditProject
from src.features.edit.core.serialization import EditProjectSerializer
from src.features.edit.core.source_patch import SourcePatchSpec
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)


def _project_with_asset() -> tuple[EditProject, str]:
    project = EditProject.create("P0 attributes")
    project = project.import_asset(
        "/tmp/p0-attr.mov", "video",
        {"fps": 25.0, "fps_num": 25, "fps_den": 1, "total_frames": 100,
         "duration_seconds": 4.0,
         "audio_info": {"channels": 2, "sample_rate": 48000}},
    )
    return project, project.get_asset_by_path("/tmp/p0-attr.mov").asset_id


def _timeline_with_clip(asset_id: str) -> TimelineSequence:
    # 100 源帧 @25fps → 24fps 时间线 = 96 帧
    clip = ClipInstance(
        "c1", asset_id, "v1", 0, 96, 0, 100,
        _source_fps_num=25, _source_fps_den=1,
        _timeline_fps_num=24, _timeline_fps_den=1)
    return TimelineSequence(
        timeline_id="t1", name="T", fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(clip,)),))


def test_all_attribute_domains_undoable_via_single_stack() -> None:
    """制作信息/日志/解释覆盖/Source Patch：一次一批改，逐条撤销全复原。"""
    project, asset_id = _project_with_asset()
    stack = ProjectCommandStack()

    project = stack.execute(
        UpdateAssetProductionInfoCommand.create(
            asset_id, reel="A001", scene=None, shot=None, take=None), project)
    project = stack.execute(
        UpdateAssetLogCommand.create(asset_id, rating=5, keywords=("hot",)),
        project)
    project = stack.execute(
        UpdateAssetInterpretationCommand.create(
            asset_id, InterpretationOverride(fps=24.0)), project)
    project = stack.execute(
        UpdateSourcePatchCommand.create(
            asset_id, SourcePatchSpec(audio_channel_map=((0, 1), (1, 0)))),
        project)

    asset = project.assets[asset_id]
    assert asset.reel == "A001"
    assert asset.rating == 5 and asset.keywords == ("hot",)
    assert effective_asset_fps(asset, project.interpretation_overrides) == 24.0
    assert project.source_patch_overrides[asset_id].audio_channel_map == ((0, 1), (1, 0))

    # 四条全部撤销 → 除时间戳/revision 外全复原
    for _ in range(4):
        project = stack.undo(project)
    asset = project.assets[asset_id]
    assert asset.reel == ""
    assert asset.rating == 0 and asset.keywords == ()
    assert effective_asset_fps(asset, project.interpretation_overrides) == 25.0
    assert asset_id not in project.source_patch_overrides
    # 原始探测 metadata 从未被改写
    assert asset.metadata["fps"] == 25.0


def test_fps_interpretation_keeps_export_source_window_fields() -> None:
    """重锚只改 fps 快照与时长：导出侧消费的 source_start/source_duration
    原样（TimelineExporter._available_clip_handles 读同款字段）。"""
    project, asset_id = _project_with_asset()
    timeline = _timeline_with_clip(asset_id)

    num, den = fps_as_fraction(24.0)
    updated = UpdateClipFpsInterpretationCommand.create("v1", "c1", num, den) \
        .apply(timeline)
    clip = updated.get_clip_by_id("c1")
    # 导出/代理 handles 的源窗输入不变
    assert (clip.source_start, clip.source_duration) == (0, 100)
    # fps 快照与时长按新解释重算（同帧数 1:1 → 100）
    assert (clip._source_fps_num, clip._source_fps_den) == (24, 1)
    assert clip.timeline_duration == 100
    # 素材解释不覆盖探测（导出回退路径仍能拿到原始 fps）
    assert project.assets[asset_id].metadata["fps"] == 25.0


def test_attribute_domains_survive_serialization_roundtrip() -> None:
    project, asset_id = _project_with_asset()
    project = project.with_interpretation_override(
        asset_id, InterpretationOverride(fps=24.0, rotation_deg=90,
                                         sar=(1, 1), field_order="progressive"))
    project = project.update_asset_production_info(asset_id, reel="A001", scene="42")

    reopened = EditProjectSerializer.from_json(EditProjectSerializer.to_json(project))
    override = reopened.interpretation_overrides[asset_id]
    assert isinstance(override, InterpretationOverride)
    assert override.fps == 24.0
    assert override.rotation_deg == 90
    assert override.sar == (1, 1)
    assert override.field_order == "progressive"
    assert reopened.assets[asset_id].reel == "A001"
    # 探测值仍未改写
    assert reopened.assets[asset_id].metadata["fps"] == 25.0
