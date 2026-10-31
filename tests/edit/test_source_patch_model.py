"""F2.1 · Source Patching 模型片——spec 校验/解析/持久化字段/命令前奏。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.project import EditProject
from src.features.edit.core.source_patch import (
    DEFAULT_SOURCE_PATCH,
    SourcePatchSpec,
    patch_for_asset,
)


class TestSourcePatchSpec:
    def test_default_is_passthrough(self) -> None:
        assert DEFAULT_SOURCE_PATCH.video_enabled is True
        assert DEFAULT_SOURCE_PATCH.audio_enabled is True
        assert DEFAULT_SOURCE_PATCH.audio_channel_map is None
        assert DEFAULT_SOURCE_PATCH.resolve_source_channels(6) == tuple(range(6))
        assert DEFAULT_SOURCE_PATCH.target_channel_count(6) == 6

    def test_channel_selection_stereo_from_51(self) -> None:
        """5.1 源只把 L/R 送目标轨（常用配置）。"""
        spec = SourcePatchSpec(audio_channel_map=((0, 0), (1, 1)))
        spec.validate(6)
        assert spec.resolve_source_channels(6) == (0, 1)
        assert spec.target_channel_count(6) == 2

    def test_single_channel_to_target(self) -> None:
        """单选源声道 3（5.1 的 C）做单声道目标轨。"""
        spec = SourcePatchSpec(audio_channel_map=((3, 0),))
        assert spec.resolve_source_channels(6) == (3,)
        assert spec.target_channel_count(6) == 1

    def test_reorder_channels(self) -> None:
        spec = SourcePatchSpec(audio_channel_map=((1, 0), (0, 1)))
        assert spec.resolve_source_channels(2) == (1, 0)

    def test_validation_rejects_out_of_range_and_duplicates(self) -> None:
        with pytest.raises(ValueError):
            SourcePatchSpec(audio_channel_map=((6, 0),)).validate(6)
        with pytest.raises(ValueError):
            SourcePatchSpec(audio_channel_map=((-1, 0),)).validate(6)
        with pytest.raises(ValueError):
            SourcePatchSpec(audio_channel_map=((0, 0), (1, 0))).validate(6)

    def test_dict_roundtrip(self) -> None:
        spec = SourcePatchSpec(
            video_enabled=False,
            audio_channel_map=((2, 0), (3, 1)))
        restored = SourcePatchSpec.from_dict(spec.to_dict())
        assert restored == spec
        assert SourcePatchSpec.from_dict({}) == DEFAULT_SOURCE_PATCH


class TestProjectOverrideField:
    def _project(self) -> EditProject:
        return EditProject(project_id="p1", name="patch", root_bin_id="root")

    def test_override_set_and_clear(self) -> None:
        project = self._project()
        spec = SourcePatchSpec(audio_enabled=False)
        updated = project.with_source_patch_override("a1", spec)
        assert patch_for_asset(updated.source_patch_overrides, "a1") is spec
        # 原工程不可变
        assert project.source_patch_overrides == {}
        cleared = updated.with_source_patch_override("a1", None)
        assert cleared.source_patch_overrides == {}

    def test_patch_for_asset_default(self) -> None:
        assert patch_for_asset({}, "missing") is DEFAULT_SOURCE_PATCH
        assert patch_for_asset(None, "missing") is DEFAULT_SOURCE_PATCH


class TestUpdateSourcePatchCommand:
    def _project(self) -> EditProject:
        from src.features.edit.core.project import MediaAsset
        asset = MediaAsset(
            asset_id="a1",
            path="/tmp/interview.wav",
            media_type="audio",
            display_name="interview",
            metadata={"audio_info": {"channels": 6, "channel_layout": "5.1"}},
        )
        return EditProject(
            project_id="p1", name="patch", root_bin_id="root",
            assets={"a1": asset},
        )

    def test_apply_and_undo_restores_absence(self) -> None:
        from src.features.edit.core.commands import UpdateSourcePatchCommand
        project = self._project()
        cmd = UpdateSourcePatchCommand.create(
            "a1", SourcePatchSpec(audio_enabled=False))
        applied = cmd.apply(project)
        assert applied.source_patch_overrides["a1"].audio_enabled is False
        undone = cmd.undo(applied)
        assert undone.source_patch_overrides == {}

    def test_undo_restores_previous_override(self) -> None:
        from src.features.edit.core.commands import UpdateSourcePatchCommand
        previous = SourcePatchSpec(audio_channel_map=((0, 0),))
        project = self._project().with_source_patch_override("a1", previous)
        cmd = UpdateSourcePatchCommand.create(
            "a1", SourcePatchSpec(video_enabled=False))
        applied = cmd.apply(project)
        assert applied.source_patch_overrides["a1"].video_enabled is False
        undone = cmd.undo(applied)
        assert undone.source_patch_overrides["a1"] is previous

    def test_apply_none_clears_override(self) -> None:
        from src.features.edit.core.commands import UpdateSourcePatchCommand
        project = self._project().with_source_patch_override(
            "a1", SourcePatchSpec(audio_enabled=False))
        cmd = UpdateSourcePatchCommand.create("a1", None)
        applied = cmd.apply(project)
        assert applied.source_patch_overrides == {}
        undone = cmd.undo(applied)
        assert undone.source_patch_overrides["a1"].audio_enabled is False

    def test_apply_validates_against_known_source_channels(self) -> None:
        """6 声道源映射 src=6 越界——apply 即拒，不等渲染消费才失败。"""
        from src.features.edit.core.commands import UpdateSourcePatchCommand
        project = self._project()
        cmd = UpdateSourcePatchCommand.create(
            "a1", SourcePatchSpec(audio_channel_map=((6, 0),)))
        with pytest.raises(ValueError):
            cmd.apply(project)

    def test_unknown_asset_raises(self) -> None:
        from src.features.edit.core.commands import UpdateSourcePatchCommand
        cmd = UpdateSourcePatchCommand.create(
            "missing", SourcePatchSpec(audio_enabled=False))
        with pytest.raises(ValueError):
            cmd.apply(self._project())

    def test_command_dict_roundtrip(self) -> None:
        from src.features.edit.core.commands import UpdateSourcePatchCommand
        spec = SourcePatchSpec(
            video_enabled=True, audio_enabled=False,
            audio_channel_map=((0, 0), (3, 1)))
        cmd = UpdateSourcePatchCommand.create("a1", spec)
        payload = cmd.to_dict()
        assert payload["command_type"] == "UpdateSourcePatchCommand"
        restored = UpdateSourcePatchCommand.from_dict(payload)
        assert restored.asset_id == "a1"
        assert restored.new_spec == spec
        # 清除型命令（None）也往返无损
        clear = UpdateSourcePatchCommand.from_dict(
            UpdateSourcePatchCommand.create("a1", None).to_dict())
        assert clear.new_spec is None


class TestSourcePatchPersistence:
    def test_settings_roundtrip_through_converters(self) -> None:
        from src.features.edit.core.edit_project_io import (
            convert_edit_project_to_topos_project,
            convert_topos_project_to_edit_project,
        )
        spec = SourcePatchSpec(
            video_enabled=False, audio_channel_map=((2, 0), (3, 1)))
        project = EditProject.create("patch persistence")
        project = project.with_source_patch_override("asset-x", spec)

        topos_project = convert_edit_project_to_topos_project(project)
        saved = topos_project.settings["source_patch_overrides"]
        assert saved == {"asset-x": spec.to_dict()}

        restored = convert_topos_project_to_edit_project(
            topos_project,
            full_data={
                "bins": {
                    project.root_bin_id: {
                        "bin_id": project.root_bin_id,
                        "name": "Root",
                        "parent_id": None,
                        "asset_ids": [],
                        "timeline_ids": [],
                    }
                }
            },
        )
        assert restored.source_patch_overrides["asset-x"] == spec

    def test_unknown_payload_preserved_and_defaults_to_passthrough(
            self) -> None:
        """旧包/未来 schema 载荷：保留原样可重存，行为回退默认直通。"""
        from src.features.edit.core.edit_project_io import (
            _deserialize_source_patch_overrides,
            _serialize_source_patch_overrides,
        )
        garbage = {"audio_channel_map": "not-a-map", "schema": "v9"}
        loaded = _deserialize_source_patch_overrides(
            {"legacy": garbage, "junk": 42})
        assert loaded["legacy"] == garbage
        assert loaded["junk"] == 42
        # 重存不丢
        assert _serialize_source_patch_overrides(loaded) == loaded
        # 运行时按默认直通
        assert patch_for_asset(loaded, "legacy") is DEFAULT_SOURCE_PATCH
        assert patch_for_asset(loaded, "junk") is DEFAULT_SOURCE_PATCH
