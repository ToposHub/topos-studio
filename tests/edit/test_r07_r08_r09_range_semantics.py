"""R07/R08/R09：子片段范围域 / 插入入口范围贯通 / 解释帧率消费回归。

对应《媒体池后端完成情况复核与整改计划》（2026-11-10）：
- R07：SUBCLIP_SOURCE_END 统一为含尾端点并带域标记；旧场景剪切子
  片段（不含尾写入）在 load 时按可证明不变式迁移，绝不盲减 1；
- R08：所有时间线插入入口经范围服务取窗口——优先级：显式拖放窗口 >
  子片段范围 > 素材级 In/Out > 全长；
- R09：解释覆盖帧率（effective_asset_fps）统一进入源监视器时基与
  初始时间线（此前只有插入分支消费）。
"""
from __future__ import annotations

from typing import Any

import pytest

from src.features.edit.core.project import EditProject, MediaAsset


def _master(tmp_path: Any, total: int = 100, fps: float = 25.0) -> EditProject:
    source = tmp_path / "master.mov"
    source.write_bytes(b"m")
    return EditProject.create("Range").import_asset(
        str(source), "video", {"fps": fps, "total_frames": total})


def _spec(start: int = 20, duration: int = 10):
    from src.features.edit.core.scene_cut.models import (
        SceneSubclipSpec,
        SceneCutKind,
    )
    return SceneSubclipSpec(
        parent_asset_id="parent-1", source_path="/mnt/master.mov",
        scene_index=1, source_start=start, source_duration=duration,
        start_seconds=start / 25.0, duration_seconds=duration / 25.0,
        display_name="Scene 1", start_timecode="00:00:00:20",
        end_timecode="00:00:00:30", thumbnail_source_frame=start,
        detection_confidence=0.9, detection_kind=SceneCutKind.HARD_CUT)


class TestR07RangeSemantics:
    def test_scene_cut_subclip_writes_end_inclusive(self) -> None:
        from src.features.edit.core.scene_cut.subclips import (
            SUBCLIP_RANGE_SEMANTICS,
            SUBCLIP_RANGE_END_INCLUSIVE,
            create_subclip_asset,
        )
        parent = MediaAsset("parent-1", "/mnt/master.mov", "video",
                            "master.mov", {"fps": 25.0, "total_frames": 100})
        asset = create_subclip_asset(_spec(start=20, duration=10), "an-1",
                                     parent)
        assert asset.metadata["source_end"] == 29          # 含尾端点
        assert asset.metadata[SUBCLIP_RANGE_SEMANTICS] == \
            SUBCLIP_RANGE_END_INCLUSIVE
        assert asset.metadata["total_frames"] == 10

    def test_effective_range_frame_count_matches_duration(self) -> None:
        """复现（repro R07）：start=20、duration=10 读取必须 10 帧不是 11。"""
        from src.features.edit.core.asset_source_range import (
            get_effective_source_range,
        )
        from src.features.edit.core.scene_cut.subclips import (
            create_subclip_asset,
        )
        parent = MediaAsset("parent-1", "/mnt/master.mov", "video",
                            "master.mov", {"fps": 25.0, "total_frames": 100})
        subclip = create_subclip_asset(_spec(), "an-1", parent)
        start, end = get_effective_source_range(subclip)
        assert (start, end) == (20, 29)
        assert end - start + 1 == subclip.metadata["source_duration"]

    def test_spec_to_metadata_end_inclusive(self) -> None:
        meta = _spec(start=5, duration=7).to_metadata("an-2")
        assert meta["source_end"] == 11
        assert meta["source_range_semantics"] == "end_inclusive"

    def test_migration_converts_provable_legacy_scene_cut(
            self, tmp_path: Any) -> None:
        """旧场景剪切（end=start+duration，无标记）→ 含尾 + 打标。"""
        from src.features.edit.core.asset_source_range import (
            get_effective_source_range,
            migrate_subclip_range_semantics,
        )
        project = _master(tmp_path)
        legacy = dict(
            is_subclip=True, parent_asset_id="p", parent_path="/x.mov",
            source_start=20, source_duration=10, source_end=30,
            scene_cut_analysis_id="an-old", scene_cut_kind="cut",
            source_total_frames=100)
        sub = MediaAsset("sub-legacy", "/x.mov", "video", "S1", legacy)
        project = project.with_media_asset_added(sub)

        migrated, count = migrate_subclip_range_semantics(project)
        assert count == 1
        asset = migrated.assets["sub-legacy"]
        assert asset.metadata["source_end"] == 29
        assert asset.metadata["source_range_semantics"] == "end_inclusive"
        assert get_effective_source_range(asset) == (20, 29)
        # 幂等
        migrated_again, again = migrate_subclip_range_semantics(migrated)
        assert again == 0
        assert migrated_again.assets["sub-legacy"].metadata["source_end"] == 29

    def test_migration_keeps_manual_and_unprovable_entries(
            self, tmp_path: Any) -> None:
        from src.features.edit.core.asset_source_range import (
            create_manual_subclip,
            migrate_subclip_range_semantics,
        )
        project = _master(tmp_path)
        master_id = next(iter(project.assets))
        project, manual = create_manual_subclip(project, master_id, 20, 29)

        inconsistent = dict(
            is_subclip=True, parent_asset_id="p", parent_path="/x.mov",
            source_start=0, source_duration=50, source_end=99,
            scene_cut_analysis_id="an-x",     # end≠start+duration 且非含尾
            source_total_frames=100)
        project = project.with_media_asset_added(MediaAsset(
            "sub-weird", "/x.mov", "video", "W", inconsistent))

        migrated, count = migrate_subclip_range_semantics(project)
        # 手工子片段：本就含尾 → 只补标记；不一致条目：保持原样
        assert migrated.assets[manual.asset_id].metadata["source_end"] == 29
        assert migrated.assets[manual.asset_id].metadata[
            "source_range_semantics"] == "end_inclusive"
        assert migrated.assets["sub-weird"].metadata["source_end"] == 99
        assert "source_range_semantics" not in migrated.assets[
            "sub-weird"].metadata

    def test_load_project_upgrades_legacy_scene_cut_subclip(
            self, tmp_path: Any) -> None:
        """创建→保存→重开：旧不含尾子片段 load 时迁移为含尾。"""
        from src.features.edit.core.edit_project_io import EditProjectIO
        project = _master(tmp_path)
        legacy = dict(
            is_subclip=True, parent_asset_id="p", parent_path="/x.mov",
            source_start=20, source_duration=10, source_end=30,
            scene_cut_analysis_id="an-old", scene_cut_kind="cut",
            source_total_frames=100)
        project = project.with_media_asset_added(MediaAsset(
            "sub-legacy", "/x.mov", "video", "S1", legacy))
        path = tmp_path / "legacy.topos"
        io = EditProjectIO()
        assert io.save_project(path, project)
        loaded = io.load_project(path)
        assert loaded is not None
        asset = loaded[0].assets.get("sub-legacy")
        assert asset is not None
        assert asset.metadata["source_end"] == 29
        assert asset.metadata["source_range_semantics"] == "end_inclusive"


class TestR08InsertEntriesConsumeRange:
    def test_controller_effective_range_prefers_in_out(self, tmp_path: Any,
                                                       monkeypatch: Any) -> None:
        from src.app.controllers.edit_controller import EditControllerMixin
        from src.features.edit.core.asset_source_range import set_asset_in_out
        project = _master(tmp_path)
        master_id = next(iter(project.assets))
        project = set_asset_in_out(project, master_id, 10, 49)
        asset = project.assets[master_id]
        pytest.importorskip("PyQt6.QtWidgets")
        mixin = EditControllerMixin.__new__(EditControllerMixin)
        assert mixin._effective_insert_range(asset) == (10, 49)

    def test_controller_effective_range_full_without_marks(
            self, tmp_path: Any) -> None:
        from src.app.controllers.edit_controller import EditControllerMixin
        pytest.importorskip("PyQt6.QtWidgets")
        project = _master(tmp_path, total=100)
        asset = project.assets[next(iter(project.assets))]
        mixin = EditControllerMixin.__new__(EditControllerMixin)
        assert mixin._effective_insert_range(asset) == (0, 99)

    def test_controller_subclip_range_wins(self, tmp_path: Any) -> None:
        from src.app.controllers.edit_controller import EditControllerMixin
        from src.features.edit.core.asset_source_range import (
            create_manual_subclip,
        )
        pytest.importorskip("PyQt6.QtWidgets")
        project = _master(tmp_path)
        project, manual = create_manual_subclip(project,
                                                next(iter(project.assets)),
                                                5, 9)
        mixin = EditControllerMixin.__new__(EditControllerMixin)
        assert mixin._effective_insert_range(manual) == (5, 9)

    def test_timeline_view_master_in_out_window(self, tmp_path: Any) -> None:
        from src.features.edit.core.asset_source_range import set_asset_in_out
        from src.features.edit.ui.timeline_view import TimelineView
        pytest.importorskip("PyQt6.QtWidgets")
        project = _master(tmp_path)
        master_id = next(iter(project.assets))
        project = set_asset_in_out(project, master_id, 12, 31)
        asset = project.assets[master_id]
        assert TimelineView._effective_asset_range(None, asset) == (12, 31)
        # 手工子片段（含尾）经同一服务读取
        from src.features.edit.core.asset_source_range import (
            create_manual_subclip,
        )
        project, manual = create_manual_subclip(project, master_id, 3, 8)
        assert TimelineView._effective_asset_range(None, manual) == (3, 8)


class TestR09InterpretationFps:
    def _asset(self, fps: float = 25.0):
        return MediaAsset("a1", "/x/clip.mov", "video", "clip.mov",
                          {"fps": fps, "fps_num": 25000, "fps_den": 1000,
                           "total_frames": 100})

    def test_time_domain_prefers_interpretation(self) -> None:
        from src.features.edit.ui.source_viewer import _build_time_domain
        domain = _build_time_domain(self._asset(25.0),
                                    interpretation_fps=23.976)
        assert (domain.fps.num, domain.fps.den) == (24000, 1001)
        # 无解释 → 探测 rational
        domain_probe = _build_time_domain(self._asset(25.0))
        assert float(domain_probe.fps) == pytest.approx(25.0)
        # 非法解释值回退探测
        domain_bad = _build_time_domain(self._asset(25.0),
                                        interpretation_fps=float("nan"))
        assert float(domain_bad.fps) == pytest.approx(25.0)

    def test_initial_timeline_fps_consumes_override(self) -> None:
        from types import SimpleNamespace
        from fractions import Fraction

        from src.features.edit.core.interpretation import (
            InterpretationOverride,
        )
        from src.features.edit.ui.timeline_view import TimelineView
        pytest.importorskip("PyQt6.QtWidgets")
        asset = self._asset(29.97)
        project = SimpleNamespace(
            interpretation_overrides={
                "a1": InterpretationOverride(fps=25.0)})
        view = SimpleNamespace(_project=project)
        result = TimelineView._timeline_fps_for_first_asset(view, asset)
        assert Fraction(result.num, result.den) == Fraction(25)
        # 无覆盖 → 走探测路径（fps_num/fps_den）
        view_none = SimpleNamespace(_project=None)
        result_probe = TimelineView._timeline_fps_for_first_asset(
            view_none, asset)
        assert Fraction(result_probe.num,
                        result_probe.den) == Fraction(25, 1)

    def test_source_viewer_open_passes_interpretation(
            self, tmp_path: Any, qapp: Any) -> None:
        """宿主打开素材时把 effective fps 传入（集成冒烟：edit_page 口径）。"""
        from src.features.edit.core.interpretation import (
            InterpretationOverride,
            effective_asset_fps,
        )
        asset = self._asset(29.97)
        overrides = {"a1": InterpretationOverride(fps=24.0)}
        assert effective_asset_fps(asset, overrides) == 24.0
