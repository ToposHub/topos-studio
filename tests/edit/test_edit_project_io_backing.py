"""DESK-03A/B · EditProjectIO 层的 backing 贯通测试。

生产链路验证：load 捕获来源 backing → 手动另存（后台 worker）与自动
保存（.autosave）都携带来源载荷 → 未知模块/资源随行。shared 层语义
见 tests/project/test_archive_backing.py。
"""

from __future__ import annotations

import json
import os
import time
import zipfile

import pytest

from tests.project.package_golden import save_golden_package

pytestmark = pytest.mark.edit_core


def _inject_unknown_module(path: str, name: str, data: dict) -> None:
    with zipfile.ZipFile(path) as zf:
        contents = {i.filename: zf.read(i.filename)
                    for i in zf.infolist() if not i.is_dir()}
    contents[name] = json.dumps(data, indent=2).encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry, raw in contents.items():
            zf.writestr(entry, raw)


def _wait_for_file(qapp, path: str, timeout_s: float = 15.0) -> bool:
    """后台 worker 写完文件前泵事件循环（queued 信号需要事件处理）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if os.path.exists(path):
            # 原子替换完成后，再泵一轮让完成信号送达
            for _ in range(20):
                qapp.processEvents()
                time.sleep(0.01)
            return True
        time.sleep(0.02)
    return False


def _assert_module_present(path: str, name: str) -> None:
    with zipfile.ZipFile(path) as zf:
        assert name in zf.namelist(), f"{name} 未随保存携带"


@pytest.fixture
def io_with_loaded_project(qapp, tmp_path):
    """加载一个带未知模块的包，返回 (io, 包路径, project)。"""
    from src.features.edit.core.edit_project_io import EditProjectIO

    package = save_golden_package(str(tmp_path), "handoff.topos")
    _inject_unknown_module(package, "future_module/data.json",
                           {"writer": "mobile", "v": 9})
    io = EditProjectIO()
    loaded = io.load_project(package)
    assert loaded is not None
    project, _ui_state, _sequences = loaded
    return io, package, project


class TestManualSaveAsCarriesBacking:
    def test_save_as_preserves_unknown_module(
        self, qapp, io_with_loaded_project, tmp_path,
    ) -> None:
        io, package, project = io_with_loaded_project
        assert io.get_current_backing() is not None

        target_dir = tmp_path / "elsewhere"
        target_dir.mkdir()
        target = str(target_dir / "desktop_edit.topos")
        assert io.save_project_as(target, project)
        assert _wait_for_file(qapp, target)
        _assert_module_present(target, "future_module/data.json")

        # 新包可再次生产加载
        assert io.load_project(target) is not None

    def test_quick_save_same_path_unchanged_semantics(
        self, qapp, io_with_loaded_project,
    ) -> None:
        io, package, project = io_with_loaded_project
        io.set_callbacks(
            get_project=lambda: project,
            get_ui_state=lambda: None,
            get_timeline_sequences=lambda: None,
        )
        # 原地快存：模块保留语义不变（Phase B 从目标自身读取）
        before = os.path.getmtime(package)
        time.sleep(0.01)
        assert io.quick_save()
        assert _wait_for_file(qapp, package)
        assert os.path.getmtime(package) >= before
        _assert_module_present(package, "future_module/data.json")


class TestAutosaveCarriesBacking:
    def test_autosave_file_has_source_modules(
        self, qapp, io_with_loaded_project,
    ) -> None:
        io, package, project = io_with_loaded_project
        io.set_callbacks(
            get_project=lambda: project,
            get_ui_state=lambda: None,
            get_timeline_sequences=lambda: None,
        )
        autosave_path = package + ".autosave"
        if os.path.exists(autosave_path):
            os.remove(autosave_path)

        assert io.force_autosave()
        assert _wait_for_file(qapp, autosave_path), "autosave 未落盘"
        # backing 使恢复文件持有来源的全部未知模块
        _assert_module_present(autosave_path, "future_module/data.json")

        io.cleanup()
