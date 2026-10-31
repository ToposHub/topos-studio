"""
SaveRouter 单元测试。

测试统一保存路由器的核心功能：
- 上下文识别和路由
- 工程包路径管理
- 保存目标描述
- 回调机制
"""

import pytest
import os
import tempfile
import zipfile
import json
import threading
import time
from unittest.mock import MagicMock, patch

from src.shared.project.save_router import (
    SaveRouter,
    SaveContext,
    SaveTarget,
)
from src.shared.project.edit_project_adapter import edit_project_adapter
from src.shared.project.manifest import ToposManifest


class TestSaveRouterBasics:
    """SaveRouter 基础功能测试。"""

    def test_init(self):
        """测试初始化。"""
        router = SaveRouter()
        assert router._callbacks == {}
        assert router._project_path_cache is None

    def test_set_callback(self):
        """测试设置回调。"""
        router = SaveRouter()
        router.set_callback("test", lambda: "value")
        assert "test" in router._callbacks
        assert router._call_callback("test") == "value"

    def test_set_callbacks_batch(self):
        """测试批量设置回调。"""
        router = SaveRouter()
        router.set_callbacks({
            "get_current_page": lambda: "Color",
            "get_current_file_path": lambda: "/test/image.jpg",
        })
        assert router._call_callback("get_current_page") == "Color"
        assert router._call_callback("get_current_file_path") == "/test/image.jpg"

    def test_call_missing_callback(self):
        """测试调用未设置的回调。"""
        router = SaveRouter()
        result = router._call_callback("missing")
        assert result is None


class TestSaveContextRouting:
    """保存上下文路由测试。"""

    def test_get_context_from_page_library(self):
        """测试从 Library 页面获取上下文。"""
        router = SaveRouter()
        context = router._get_context_from_page("Library")
        assert context == SaveContext.LIBRARY_SINGLE

    def test_get_context_from_page_color(self):
        """测试从 Color 页面获取上下文。"""
        router = SaveRouter()
        # 无 clip_id → COLOR_ASSET
        context = router._get_context_from_page("Color")
        assert context == SaveContext.COLOR_ASSET

    def test_get_context_from_page_edit(self):
        """测试从 Edit 页面获取上下文。"""
        router = SaveRouter()
        context = router._get_context_from_page("Edit")
        assert context == SaveContext.EDIT_TIMELINE

    def test_get_context_from_page_composite(self):
        """测试从 Composite 页面获取上下文。"""
        router = SaveRouter()
        context = router._get_context_from_page("Composite")
        assert context == SaveContext.COMPOSITE

    def test_get_target_region_library(self):
        """测试 Library 上下文的目标区域。"""
        router = SaveRouter()
        region = router._get_target_region(SaveContext.LIBRARY_SINGLE)
        assert region == "assets+color_stack"

    def test_get_target_region_color_asset(self):
        """测试 Color Asset 上下文的目标区域。"""
        router = SaveRouter()
        region = router._get_target_region(SaveContext.COLOR_ASSET)
        assert region == "color_stacks[asset_default_stack_id]"

    def test_get_target_region_color_clip(self):
        """测试 Color Clip 上下文的目标区域。"""
        router = SaveRouter()
        region = router._get_target_region(SaveContext.COLOR_CLIP)
        assert region == "ClipInstance.color_stack_id+color_stacks"

    def test_get_target_region_composite(self):
        """测试 Composite 上下文的目标区域。"""
        router = SaveRouter()
        region = router._get_target_region(SaveContext.COMPOSITE)
        assert region == "compositions[active_composite_id]"

    def test_get_target_region_edit_timeline(self):
        """测试 Edit Timeline 上下文的目标区域。"""
        router = SaveRouter()
        region = router._get_target_region(SaveContext.EDIT_TIMELINE)
        assert region == "timeline_sequences"


class TestProjectPathManagement:
    """工程包路径管理测试。"""

    def test_get_project_path_for_file(self):
        """测试根据文件路径获取工程包路径。"""
        router = SaveRouter()
        path = router._get_project_path_for_file("/test/image.jpg")
        assert path == "/test/image.topos"

    def test_get_project_path_for_video(self):
        """测试根据视频文件路径获取工程包路径。"""
        router = SaveRouter()
        path = router._get_project_path_for_file("/test/video.mp4")
        assert path == "/test/video.topos"

    def test_get_project_path_for_file_empty(self):
        """测试空文件路径返回 None。"""
        router = SaveRouter()
        path = router._get_project_path_for_file("")
        assert path is None

    def test_get_project_path_for_file_none(self):
        """测试 None 文件路径返回 None。"""
        router = SaveRouter()
        path = router._get_project_path_for_file(None)
        assert path is None

    def test_convert_to_topos_path_replaces_unknown_extension(self):
        """Unknown project-like extensions should normalize to .topos."""
        router = SaveRouter()
        path = router._convert_to_topos_path("/test/project.legacy")
        assert path == "/test/project.topos"

    def test_convert_to_topos_path_already_topos(self):
        """测试已为 .topos 的路径。"""
        router = SaveRouter()
        path = router._convert_to_topos_path("/test/project.topos")
        assert path == "/test/project.topos"

    def test_convert_to_topos_path_strips_recovery_suffixes(self):
        router = SaveRouter()
        assert router._convert_to_topos_path("/test/project.topos.autosave") == "/test/project.topos"
        assert router._convert_to_topos_path("/test/project.topos.bak") == "/test/project.topos"

    def test_convert_to_topos_path_other(self):
        """Non-.topos path should get extension replaced."""
        router = SaveRouter()
        path = router._convert_to_topos_path("/test/project.json")
        # 新逻辑直接替换扩展名，不是追加
        assert path == "/test/project.topos"  # 不是 .json.topos

    def test_set_and_get_project_path(self):
        """测试手动设置和获取工程路径。"""
        router = SaveRouter()
        router.set_project_path("/test/custom.topos")
        assert router._project_path_cache == "/test/custom.topos"
        assert router.get_current_project_path() == "/test/custom.topos"

    def test_clear_cache(self):
        """测试清除缓存。"""
        router = SaveRouter()
        router.set_project_path("/test/project.topos")
        router.clear_cache()
        assert router._project_path_cache is None


class TestUnifiedProjectBackupAndRead:
    """统一 .topos 备份和读取测试。"""

    def test_backup_uses_edit_project_path_without_current_media(self):
        """Edit/Media Pool 工程没有 current_file_path 时仍应能备份当前 .topos。"""
        router = SaveRouter()

        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = os.path.join(tmpdir, "edit_project.topos")
            backup_dir = os.path.join(tmpdir, "backups")

            with zipfile.ZipFile(project_path, "w") as zf:
                zf.writestr("manifest.json", json.dumps({
                    "format": "topos_project_archive",
                    "project_id": "test-123",
                    "project_mode": "edit_project",
                }))
                zf.writestr("project.json", json.dumps({
                    "name": "Edit Project",
                    "project_id": "test-123",
                }))

            router.set_callbacks({
                "get_current_file_path": lambda: None,
                "get_edit_project_filepath": lambda: project_path,
            })

            assert router.backup_project(backup_dir=backup_dir, max_backups=5)

            backups = [name for name in os.listdir(backup_dir) if name.endswith(".topos")]
            assert len(backups) == 1
            assert zipfile.is_zipfile(os.path.join(backup_dir, backups[0]))


class TestProjectAutosavePath:
    """Recovery saves must stay separate from canonical project paths."""

    def test_topos_file_recognizes_recovery_suffixes(self):
        from src.shared.project.topos_project_io import ToposProjectIO

        assert ToposProjectIO.is_topos_file("Demo.topos")
        assert ToposProjectIO.is_topos_file("Demo.topos.autosave")
        assert ToposProjectIO.is_topos_file("Demo.topos.bak")
        assert not ToposProjectIO.is_topos_file("Demo.autosave")
        assert not ToposProjectIO.is_topos_file("Demo.json")

    def test_autosave_keeps_topos_autosave_suffix(self, qapp, tmp_path, monkeypatch):
        from src.features.edit.core.edit_project_io import EditProjectIO, EditProjectIOConfig
        from src.features.edit.core.project import EditProject
        from src.shared.project.topos_project_io import ToposProjectIO

        captured: dict[str, str] = {}

        def fake_save(*args, **kwargs):
            captured["path"] = str(args[1])
            return True

        monkeypatch.setattr(ToposProjectIO, "save", fake_save)
        io = EditProjectIO(
            EditProjectIOConfig(autosave_interval=0, backup_on_save=False),
            parent=qapp,
        )
        source_path = tmp_path / "Demo.topos"

        assert io.save_project(
            f"{source_path}.autosave",
            EditProject.create("Demo"),
            create_backup=False,
            autosave=True,
        )
        assert captured["path"].endswith("Demo.topos.autosave")
        assert io.get_current_filepath() is None

    def test_autosave_only_surfaces_when_newer_than_project(self, qapp, tmp_path):
        from src.features.edit.core.edit_project_io import EditProjectIO, EditProjectIOConfig

        io = EditProjectIO(
            EditProjectIOConfig(autosave_interval=0, backup_on_save=False),
            parent=qapp,
        )
        project_path = tmp_path / "Demo.topos"
        autosave_path = tmp_path / "Demo.topos.autosave"
        project_path.write_bytes(b"project")
        autosave_path.write_bytes(b"autosave")

        import os

        os.utime(project_path, (100.0, 100.0))
        os.utime(autosave_path, (200.0, 200.0))
        assert io.get_autosave_filepath(project_path) == str(autosave_path)

        os.utime(project_path, (300.0, 300.0))
        assert io.get_autosave_filepath(project_path) is None
        assert not autosave_path.exists()

    def test_clean_shutdown_waits_for_autosave_before_removing_recovery_file(
        self, qapp, tmp_path, monkeypatch
    ):
        """A running autosave must not recreate its file after clean shutdown."""
        from src.features.edit.core.edit_project_io import EditProjectIO, EditProjectIOConfig
        from src.features.edit.core.project import EditProject
        from src.shared.project.topos_project_io import ToposProjectIO

        project_path = tmp_path / "Demo.topos"
        autosave_path = tmp_path / "Demo.topos.autosave"
        project_path.write_bytes(b"canonical")
        write_started = threading.Event()

        def slow_save(_project, filepath, *_args, **_kwargs):
            write_started.set()
            time.sleep(0.05)
            autosave_path.write_bytes(b"recovery")
            assert str(filepath) == str(autosave_path)
            return True

        monkeypatch.setattr(ToposProjectIO, "save", slow_save)
        io = EditProjectIO(
            EditProjectIOConfig(autosave_interval=0, backup_on_save=False),
            parent=qapp,
        )
        io.set_current_filepath(project_path)
        io.set_callbacks(
            get_project=lambda: EditProject.create("Demo"),
            get_ui_state=lambda: None,
            get_timeline_sequences=lambda: {},
        )

        assert io.force_autosave()
        assert write_started.wait(timeout=1.0)
        io.cleanup()

        assert not autosave_path.exists()
        assert not io._autosave_in_progress

    def test_load_single_asset_data_restores_full_layer_payload(self):
        """新格式 color/color_stacks.json 应能恢复完整 Layer 调色数据。"""
        from src.features.color.core.layer import Layer
        from src.shared.project.topos_project import ToposProject
        from src.shared.project.topos_project_io import ToposProjectIO

        router = SaveRouter()

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = os.path.join(tmpdir, "image.png")
            project_path = os.path.join(tmpdir, "image.topos")
            open(image_path, "wb").close()

            project = ToposProject.create_single_asset(
                name="Image",
                asset_path=image_path,
                media_type="image",
            )

            layer = Layer("Grade")
            layer.grading_params["exposure"] = 1.25
            layer.curves_data = {"rgb": [[0.0, 0.0], [1.0, 1.0]]}
            layer.hsl_curve_data = {"HueSat": [[0.0, 0.5], [1.0, 0.5]]}
            layer.mixer_params = {"red": [1.0, 0.0, 0.0]}
            layer.soft_clip_params.update({"enabled": True, "high": 0.9})
            layer.active_lut_path = "/tmp/look.cube"
            layer.lut_intensity = 0.7

            stack_id = project.primary_color_stack_id
            color_stacks = {
                stack_id: {
                    "stack_id": stack_id,
                    "name": "Primary",
                    "layers": [layer.to_dict()],
                    "raw_params": {"enabled": True, "exposure": 0.2},
                    "active_layer_index": 0,
                }
            }

            assert ToposProjectIO.save(
                project=project,
                filepath=project_path,
                full_color_stacks_data=color_stacks,
            )

            result = router.load_single_asset_data(project_path)

            assert result is not None
            restored = result["layers"][0]
            assert restored.grading_params["exposure"] == 1.25
            assert restored.curves_data == layer.curves_data
            assert restored.hsl_curve_data == layer.hsl_curve_data
            assert restored.mixer_params == layer.mixer_params
            assert restored.soft_clip_params["enabled"] is True
            assert restored.soft_clip_params["high"] == 0.9
            assert restored.active_lut_path == "/tmp/look.cube"
            assert restored.lut_intensity == 0.7
            assert result["raw_params"]["exposure"] == 0.2


class TestSaveTarget:
    """SaveTarget 测试。"""

    def test_get_save_target_basic(self):
        """测试基本保存目标获取。"""
        router = SaveRouter()
        router.set_callbacks({
            "get_current_page": lambda: "Color",
            "get_current_file_path": lambda: "/test/image.jpg",
        })

        target = router.get_save_target()
        assert target is not None
        assert target.context == SaveContext.COLOR_ASSET
        assert target.project_path == "/test/image.topos"
        assert target.target_region == "color_stacks[asset_default_stack_id]"

    def test_get_save_target_with_clip(self):
        """测试带剪辑 ID 的保存目标。"""
        router = SaveRouter()
        router.set_callbacks({
            "get_current_page": lambda: "Color",
            "get_current_file_path": lambda: "/test/image.jpg",
            "get_current_clip_id": lambda: "clip-123",
            "get_current_stack_id": lambda: "stack-456",
        })

        target = router.get_save_target()
        assert target.context == SaveContext.COLOR_CLIP
        assert target.clip_id == "clip-123"
        assert target.stack_id == "stack-456"

    def test_get_save_target_no_page(self):
        """测试无页面时的保存目标。"""
        router = SaveRouter()
        router.set_callbacks({
            "get_current_page": lambda: None,
        })

        target = router.get_save_target()
        assert target is None


class TestProjectCreationAndLoading:
    """工程包创建和加载测试。"""

    def test_create_new_project(self):
        """测试创建新工程包。"""
        router = SaveRouter()

        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = os.path.join(tmpdir, "test_project.topos")

            # Mock EditProjectIO
            mock_edit_project_io = MagicMock()
            mock_edit_project_io.save_project.return_value = True

            # Mock edit_project_adapter.create_project to avoid registration requirement
            mock_project = MagicMock()
            mock_project.name = "Test Project"

            router.set_callback("get_edit_project_io", lambda: mock_edit_project_io)

            with patch.object(edit_project_adapter, 'create_project', return_value=mock_project):
                success = router.create_new_project(project_path, "Test Project")
                assert success

                # 验证 save_project 被调用
                mock_edit_project_io.save_project.assert_called_once()

    def test_load_project(self):
        """测试加载工程包。"""
        router = SaveRouter()

        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = os.path.join(tmpdir, "test_project.topos")

            # 创建一个简单的 ZIP 文件模拟工程包
            with zipfile.ZipFile(project_path, 'w') as zf:
                zf.writestr("manifest.json", json.dumps({
                    "format": "topos_project_archive",
                    "project_id": "test-123",
                    "project_mode": "edit_project",
                }))
                zf.writestr("project.json", json.dumps({
                    "name": "Test Project",
                    "project_id": "test-123",
                }))

            # Mock EditProjectIO
            mock_edit_project_io = MagicMock()
            mock_edit_project = MagicMock()
            mock_edit_project.name = "Test Project"
            mock_ui_state = MagicMock()
            mock_edit_project_io.load_project.return_value = (
                mock_edit_project, mock_ui_state, {}
            )

            router.set_callback("get_edit_project_io", lambda: mock_edit_project_io)

            # 加载
            result = router.load_project(project_path)
            assert result is not None

            edit_project, ui_state, timeline_sequences = result
            assert edit_project is not None
            assert edit_project.name == "Test Project"

    def test_load_nonexistent_project(self):
        """测试加载不存在的工程包。"""
        router = SaveRouter()
        # Mock EditProjectIO
        mock_edit_project_io = MagicMock()
        router.set_callback("get_edit_project_io", lambda: mock_edit_project_io)

        result = router.load_project("/nonexistent/path.topos")
        assert result is None

    def test_load_invalid_zip(self):
        """测试加载无效的 ZIP 文件。"""
        router = SaveRouter()

        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建一个非 ZIP 文件
            invalid_path = os.path.join(tmpdir, "invalid.topos")
            with open(invalid_path, 'w') as f:
                f.write("not a zip file")

            # Mock EditProjectIO
            mock_edit_project_io = MagicMock()
            mock_edit_project_io.load_project.return_value = None
            router.set_callback("get_edit_project_io", lambda: mock_edit_project_io)

            result = router.load_project(invalid_path)
            assert result is None


# TestManifestBuilding 和 TestProjectDataBuilding 测试类已删除
# 因为 _build_manifest() 和 _build_topos_project_data() 方法已被重构
# 现在使用 EditProjectIO.save_project() 统一入口


class TestIsZipFile:
    """ZIP 文件检测测试。"""

    def test_is_zip_file_valid(self):
        """测试检测有效的 ZIP 文件。"""
        router = SaveRouter()

        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, "test.zip")
            with zipfile.ZipFile(zip_path, 'w') as zf:
                zf.writestr("test.txt", "content")

            assert router._is_zip_file(zip_path)

    def test_is_zip_file_invalid(self):
        """测试检测无效的文件。"""
        router = SaveRouter()

        with tempfile.TemporaryDirectory() as tmpdir:
            invalid_path = os.path.join(tmpdir, "invalid.txt")
            with open(invalid_path, 'w') as f:
                f.write("not a zip")

            assert not router._is_zip_file(invalid_path)


class TestSaveRouterWithMockedEngine:
    """使用 Mocked Engine 的 SaveRouter 测试。"""

    def test_save_from_library_success(self):
        """Library and Color pages save the active unified project."""
        from src.features.edit.core.project import EditProject
        router = SaveRouter()

        # Mock callbacks
        mock_engine = MagicMock()
        mock_engine.layers = []
        mock_engine.active_layer_index = 0

        project = EditProject.create_image_project("Image", "/test/image.jpg")
        edit_project_io = MagicMock()
        edit_project_io.save_project.return_value = True
        router.set_callbacks({
            "get_current_page": lambda: "Library",
            "get_current_file_path": lambda: "/test/image.jpg",
            "get_engine": lambda: mock_engine,
            "get_current_edit_project": lambda: project,
            "get_edit_project_io": lambda: edit_project_io,
            "get_ui_state": lambda: None,
            "get_current_timeline_sequences": lambda: None,
            "on_edit_project_updated": lambda updated: None,
        })

        # Mock ToposProjectIO.save - patch 实际导入路径
        success = router.save()
        assert success

    def test_save_from_color_success(self):
        """Color saves the active image project through EditProjectIO."""
        from src.features.edit.core.project import EditProject
        router = SaveRouter()

        # Mock callbacks
        mock_engine = MagicMock()
        mock_engine.layers = []
        mock_engine.active_layer_index = 0

        project = EditProject.create_image_project("Image", "/test/image.jpg")
        edit_project_io = MagicMock()
        edit_project_io.save_project.return_value = True
        router.set_callbacks({
            "get_current_page": lambda: "Color",
            "get_current_file_path": lambda: "/test/image.jpg",
            "get_engine": lambda: mock_engine,
            "get_current_edit_project": lambda: project,
            "get_edit_project_io": lambda: edit_project_io,
            "get_ui_state": lambda: None,
            "get_current_timeline_sequences": lambda: None,
            "on_edit_project_updated": lambda updated: None,
        })

        # Mock ToposProjectIO.save - patch 实际导入路径
        success = router.save()
        assert success

    def test_save_from_color_no_engine(self):
        """测试 Color 页面无 Engine 时失败。"""
        router = SaveRouter()

        router.set_callbacks({
            "get_current_page": lambda: "Color",
            "get_current_file_path": lambda: "/test/image.jpg",
            "get_engine": lambda: None,
            "get_current_edit_project": lambda: None,
        })

        success = router.save()
        assert not success

    def test_save_from_unknown_page(self):
        """测试未知页面保存失败。"""
        router = SaveRouter()

        router.set_callbacks({
            "get_current_page": lambda: "Unknown",
        })

        success = router.save()
        assert not success

    # ------------------------------------------------------------------
    # 回归：Library/Color 页 Ctrl+S 必须一并保存 composite_graph
    # 背景：独立图片在 Composite 页加的效果，如果用户切回 Color/Library
    #       页按 Ctrl+S，旧版本会用空 composite 覆盖掉已存的合成图，
    #       导致批量导出（离线读 .topos）读不到 composite 效果。
    # ------------------------------------------------------------------

    def test_save_color_single_asset_session(self):
        """Color save persists an image project through the unified route."""
        from src.features.color.core.color_session import ColorSession, SourceKind
        from src.features.edit.core.project import EditProject

        router = SaveRouter()
        mock_engine = MagicMock()
        mock_engine.layers = []
        mock_engine.active_layer_index = 0

        session = ColorSession.create_single_asset(
            project_id="proj-1",
            color_stack_id="stack-1",
            source_path="/test/image.jpg",
        )
        assert session.source_kind == SourceKind.SINGLE_ASSET
        project = EditProject.create_image_project("Image", "/test/image.jpg")
        edit_project_io = MagicMock()
        edit_project_io.save_project.return_value = True

        router.set_callbacks({
            "get_current_page": lambda: "Color",
            "get_engine": lambda: mock_engine,
            "get_current_color_session": lambda: session,
            "get_current_edit_project": lambda: project,
            "get_current_file_path": lambda: "/test/image.jpg",
            "get_edit_project_io": lambda: edit_project_io,
            "get_ui_state": lambda: None,
            "get_current_timeline_sequences": lambda: None,
            "on_edit_project_updated": lambda updated: None,
        })

        success = router.save()
        assert success

    def test_save_color_timeline_clip_session(self):
        """Color save with TIMELINE_CLIP session routes to EditProject."""
        from src.features.color.core.color_session import ColorSession, SourceKind

        router = SaveRouter()
        mock_engine = MagicMock()
        mock_engine.layers = []
        mock_engine.active_layer_index = 0

        mock_edit_project = MagicMock()
        mock_edit_project.color_stacks = {}
        mock_edit_project.project_id = "proj-1"
        mock_edit_project.name = "Test"
        mock_edit_project.assets = {}
        mock_edit_project.bins = {}
        mock_edit_project.timelines = {}
        mock_edit_project.compositions = {}
        mock_edit_project.root_bin_id = None
        mock_edit_project.active_timeline_id = None
        mock_edit_project.created_at = 0.0
        mock_edit_project.modified_at = 0.0

        session = ColorSession.create_timeline_clip(
            project_id="proj-1",
            asset_id="asset-1",
            clip_id="clip-1",
            color_stack_id="stack-1",
            source_path="/test/video.mp4",
            timeline_id="timeline-1",
        )
        assert session.source_kind == SourceKind.TIMELINE_CLIP
        assert session.clip_id == "clip-1"
        assert session.timeline_id == "timeline-1"

        mock_edit_project_io = MagicMock()
        mock_edit_project_io.save_project.return_value = True

        router.set_callbacks({
            "get_current_page": lambda: "Color",
            "get_engine": lambda: mock_engine,
            "get_current_color_session": lambda: session,
            "get_current_edit_project": lambda: mock_edit_project,
            "get_edit_project_filepath": lambda: "/test/project.topos",
            "get_edit_project_io": lambda: mock_edit_project_io,
            "get_ui_state": lambda: None,
            "get_current_timeline_sequences": lambda: None,
            "on_edit_project_updated": lambda p: None,
        })

        success = router.save()
        assert success
        # Verify EditProjectIO.save_project was called (full EditProject save)
        mock_edit_project_io.save_project.assert_called_once()

    def test_save_color_timeline_clip_no_edit_project_fails(self):
        """Color save with TIMELINE_CLIP session fails if no EditProject."""
        from src.features.color.core.color_session import ColorSession, SourceKind

        router = SaveRouter()
        mock_engine = MagicMock()
        mock_engine.layers = []
        mock_engine.active_layer_index = 0

        session = ColorSession.create_timeline_clip(
            project_id="proj-1",
            asset_id="asset-1",
            clip_id="clip-1",
            color_stack_id="stack-1",
            source_path="/test/video.mp4",
            timeline_id="timeline-1",
        )

        router.set_callbacks({
            "get_current_page": lambda: "Color",
            "get_engine": lambda: mock_engine,
            "get_current_color_session": lambda: session,
            "get_current_edit_project": lambda: None,  # Missing!
            "get_edit_project_filepath": lambda: "/test/project.topos",
        })

        success = router.save()
        assert not success  # Must fail: TIMELINE_CLIP requires EditProject


class TestSaveRouterIntegration:
    """SaveRouter 集成测试。"""

    def test_full_roundtrip_create_save_load(self):
        """测试完整流程：创建 -> 保存 -> 加载。"""
        router = SaveRouter()

        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = os.path.join(tmpdir, "integration_test.topos")

            # Mock EditProjectIO
            mock_edit_project_io = MagicMock()

            # 创建阶段 - 实际创建一个 ZIP 文件
            def mock_save_project(path, project, ui_state, timeline_sequences):
                with zipfile.ZipFile(path, 'w') as zf:
                    zf.writestr("manifest.json", json.dumps({
                        "format": "topos_project_archive",
                        "project_id": "test-123",
                        "project_mode": "edit_project",
                    }))
                    zf.writestr("project.json", json.dumps({
                        "name": project.name if hasattr(project, 'name') else "Integration Test",
                        "project_id": "test-123",
                    }))
                return True

            mock_edit_project_io.save_project = mock_save_project

            # 加载阶段 - 返回模拟的 EditProject
            mock_edit_project = MagicMock()
            mock_edit_project.name = "Integration Test"
            mock_ui_state = MagicMock()

            mock_edit_project_io.load_project.return_value = (
                mock_edit_project, mock_ui_state, {}
            )

            router.set_callback("get_edit_project_io", lambda: mock_edit_project_io)

            # Mock edit_project_adapter.create_project to avoid registration requirement
            mock_project = MagicMock()
            mock_project.name = "Integration Test"

            with patch.object(edit_project_adapter, 'create_project', return_value=mock_project):
                # 1. 创建新项目（实际创建 ZIP 文件）
                success = router.create_new_project(project_path, "Integration Test")
                assert success
                assert os.path.exists(project_path)

            # 2. 加载项目
            result = router.load_project(project_path)
            assert result is not None

            edit_project, ui_state, timeline_sequences = result
            assert edit_project.name == "Integration Test"


# 运行测试
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
