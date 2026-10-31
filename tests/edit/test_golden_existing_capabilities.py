"""F0.3 既有能力专项验收——Smart Bin 动态更新 / Adjustment Clip 作用范围 /
跨时间线剪贴板载荷（模型级）。

按 F0.3 要求先核已有实现的真实边界，不从零重建；G 级（真实窗口）的
Smart Bin 刷新与跨时间线粘贴由 ``tests/validation/drive_golden_real_app.py``
驱动验收。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.commands import (
    CreateSmartBinCommand,
    UpdateAssetLogCommand,
)
from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.project import EditProject, MediaAsset, SmartBin
from src.features.edit.core.timeline_render_plan import TimelineRenderPlan
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineClipboardPayload,
    TimelineSequence,
)
from src.shared.media.metadata import Rational


def _asset(asset_id: str, path: str, **overrides) -> MediaAsset:
    params = dict(
        asset_id=asset_id,
        path=path,
        media_type="video",
        display_name=f"{asset_id}.mov",
        metadata={"codec": "h264"},
        keywords=(),
    )
    params.update(overrides)
    return MediaAsset(**params)


def _smart_bin(name: str, **filter_params) -> SmartBin:
    return SmartBin(
        bin_id=f"sb-{name}",
        name=name,
        filter_type="custom",
        filter_params=filter_params,
    )


# ── Smart Bin：关键词/评级修改、移除后正确更新（拉取式重算契约）──

class TestSmartBinDynamicUpdate:
    def _project(self) -> EditProject:
        return EditProject(
            project_id="gp1", name="golden-bins",
            assets={
                "a1": _asset("a1", "/m/a1.mov", keywords=("interview",), rating=3),
                "a2": _asset("a2", "/m/a2.mov", keywords=("broll",), rating=2),
            },
            root_bin_id="root",
        )

    def test_keyword_change_updates_bin(self) -> None:
        project = self._project()
        project = CreateSmartBinCommand.create(
            _smart_bin("kw", keywords=["interview"])).apply(project)
        assert {a.asset_id for a in project.get_smart_bin_assets("sb-kw")} == {"a1"}
        # a2 加上关键词 → 动态进入 bin（拉取式重算）
        project = UpdateAssetLogCommand(
            command_id="cmd-kw", label="add keyword", asset_id="a2",
            new_keywords=("interview",)).apply(project)
        assert {a.asset_id for a in project.get_smart_bin_assets("sb-kw")} == {"a1", "a2"}

    def test_rating_change_and_undo(self) -> None:
        project = self._project()
        project = CreateSmartBinCommand.create(
            _smart_bin("rated", min_rating=3)).apply(project)
        assert {a.asset_id for a in project.get_smart_bin_assets("sb-rated")} == {"a1"}
        cmd = UpdateAssetLogCommand(
            command_id="cmd-rating", label="downgrade", asset_id="a1", new_rating=1)
        project = cmd.apply(project)
        assert project.get_smart_bin_assets("sb-rated") == []
        project = cmd.undo(project)
        assert {a.asset_id for a in project.get_smart_bin_assets("sb-rated")} == {"a1"}

    def test_asset_removal_updates_bin(self) -> None:
        project = self._project()
        project = CreateSmartBinCommand.create(
            _smart_bin("kw", keywords=["interview"])).apply(project)
        project = project.remove_asset("a1")
        assert project.get_smart_bin_assets("sb-kw") == []


# ── Adjustment Clip：重叠 / 轨道禁用 / 嵌套边界 / 效果顺序（渲染计划级）──

def _adjustment_sequence(*, upper_enabled: bool = True) -> tuple[EditProject, TimelineSequence, dict]:
    project = EditProject.create("AdjGolden")
    project, red_id = project.create_solid_color_asset(name="Lower", color_rgb=(1.0, 0.0, 0.0))
    project, adj_id = project.create_adjustment_clip(name="Adj")
    project, green_id = project.create_solid_color_asset(name="Upper", color_rgb=(0.0, 1.0, 0.0))
    adj_effect = EffectInstance(effect_type="blur", params={"radius": 8.0, "mix": 1.0}, order=0)
    lower = ClipInstance("ag-lower", red_id, "ag-v1", 0, 60, 0, 60)
    adj = ClipInstance("ag-adj", adj_id, "ag-v2", 0, 60, 0, 60, effects=(adj_effect,))
    upper = ClipInstance("ag-upper", green_id, "ag-v3", 0, 60, 0, 60)
    seq = TimelineSequence(
        timeline_id="ag-seq", name="AdjGolden", fps=Rational(24, 1), width=64, height=64,
        video_tracks=(
            EditTrack("ag-v1", "V1", "video", 0, clips=(lower,)),
            EditTrack("ag-v2", "V2 Adj", "video", 1, clips=(adj,)),
            EditTrack("ag-v3", "V3", "video", 2, clips=(upper,), is_enabled=upper_enabled),
        ),
    )
    return project, seq, {"red": red_id, "adj": adj_id, "green": green_id}


class TestAdjustmentClipScopeGolden:
    def test_overlap_z_order_bounds_scope(self) -> None:
        _p, seq, ids = _adjustment_sequence()
        frame_plan = TimelineRenderPlan(seq).get_frame_plan(0)
        by_asset = {layer.asset_id: layer for layer in frame_plan.layers}
        assert by_asset[ids["red"]].track_index < by_asset[ids["adj"]].track_index \
            < by_asset[ids["green"]].track_index
        adj_layer = by_asset[ids["adj"]]
        effects = getattr(adj_layer.clip, "effects", ())
        assert [e.effect_type for e in effects] == ["blur"]

    def test_disabled_track_excluded_from_plan(self) -> None:
        _p, seq, ids = _adjustment_sequence(upper_enabled=False)
        frame_plan = TimelineRenderPlan(seq).get_frame_plan(0)
        asset_ids = {layer.asset_id for layer in frame_plan.layers}
        assert ids["green"] not in asset_ids
        assert ids["red"] in asset_ids and ids["adj"] in asset_ids

    def test_adjacent_frame_outside_adjustment_has_no_adj_layer(self) -> None:
        _p, seq, ids = _adjustment_sequence()
        plan = TimelineRenderPlan(seq)
        inside = plan.get_frame_plan(30)
        outside = plan.get_frame_plan(60)
        assert any(l.asset_id == ids["adj"] for l in inside.layers)
        assert not any(l.asset_id == ids["adj"] for l in outside.layers)


# ── 跨时间线剪贴板载荷（模型级；粘贴语义在 G 级驱动验证）──

class TestTimelineClipboardPayloadGolden:
    def test_payload_roundtrip_keeps_link_and_timebase(self) -> None:
        clip_v = {"clip_id": "cv1", "asset_ref": "a1", "track_id": "v1",
                  "linked_clip_id": "ca1", "timeline_start": 0}
        clip_a = {"clip_id": "ca1", "asset_ref": "a1", "track_id": "a1",
                  "linked_clip_id": "cv1", "timeline_start": 0}
        payload = TimelineClipboardPayload(
            clips=(clip_v, clip_a), anchor_frame=12, fps=24.0)
        data = payload.to_dict()
        assert data["type"] == "timeline_clips"
        assert data["fps"] == 24.0 and data["anchor_frame"] == 12
        restored = TimelineClipboardPayload.from_dict(data)
        ids = {c["clip_id"]: c for c in restored.clips}
        assert ids["cv1"]["linked_clip_id"] == "ca1"
        assert ids["ca1"]["linked_clip_id"] == "cv1"
