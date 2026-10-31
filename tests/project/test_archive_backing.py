"""DESK-03A/B/C · ArchiveBackingStore 合同测试（shared 层）。

验收锚点（计划表 03A/03B/03C）：
- 原路径保存保留未知模块；**不依赖回读同名目标包**才能保留；
- 新目录/新文件名另存后：未知内容、原片（内嵌资源）与来源 manifest
  溯源（created_at/created_by）与 future 版本戳均随行；
- 显式清空的模块不被 backing 复活（目标优先语义）；
- 来源包消失时保存受控失败，不产出半包；
- 清缓存/移动包后再次打开正确（缓存非权威）。
"""

from __future__ import annotations

import json
import os
import shutil
import zipfile

import pytest

from tests.project.package_golden import golden_save_kwargs, save_golden_package

from src.shared.project.archive_backing import (
    ArchiveBackingStore,
    merge_backing_payload,
)
from src.shared.project.payload_cache import ProjectArchivePayload
from src.shared.project.topos_project_io import ToposProjectIO

pytestmark = pytest.mark.regression


def _entry_names(path: str) -> set[str]:
    with zipfile.ZipFile(path) as zf:
        return {i.filename for i in zf.infolist() if not i.is_dir()}


def _read_entry(path: str, name: str):
    with zipfile.ZipFile(path) as zf:
        raw = zf.read(name)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw


def _inject_unknown_module(path: str, name: str, data: dict) -> None:
    with zipfile.ZipFile(path) as zf:
        contents = {i.filename: zf.read(i.filename)
                    for i in zf.infolist() if not i.is_dir()}
    contents[name] = json.dumps(data, indent=2).encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry, raw in contents.items():
            zf.writestr(entry, raw)


class TestMergeSemantics:
    def test_destination_wins_backing_fills_gaps(self) -> None:
        dst = ProjectArchivePayload()
        dst.module_payloads["edit/media_pool.json"] = {"cleared": {}}
        src = ProjectArchivePayload()
        src.module_payloads["edit/media_pool.json"] = {"old": "data"}
        src.module_payloads["future_module/x.json"] = {"a": 1}
        src.unknown_files["assets/blob.bin"] = b"\x00\x01"
        merged = merge_backing_payload(dst, src)
        # 目标已有（含显式清空）不被覆盖；缺的补上
        assert merged.module_payloads["edit/media_pool.json"] == {"cleared": {}}
        assert merged.module_payloads["future_module/x.json"] == {"a": 1}
        assert merged.unknown_files["assets/blob.bin"] == b"\x00\x01"

    def test_manifest_adopted_only_when_destination_empty(self) -> None:
        dst = ProjectArchivePayload()
        dst.manifest = {"schema_version": 3, "created_at": 111.0}
        src = ProjectArchivePayload()
        src.manifest = {"schema_version": 3, "created_at": 999.0}
        merge_backing_payload(dst, src)
        assert dst.manifest["created_at"] == 111.0

        empty = ProjectArchivePayload()
        merge_backing_payload(empty, src)
        assert empty.manifest["created_at"] == 999.0


class TestSaveAsWithBacking:
    def test_unknown_modules_and_assets_follow_save_as(self, tmp_path) -> None:
        src_dir = tmp_path / "src"
        dst_dir = tmp_path / "dst" / "nested"
        src_dir.mkdir()
        source = save_golden_package(str(src_dir), "project.topos")
        _inject_unknown_module(source, "future_module/data.json",
                               {"schema": 9})
        _inject_unknown_module(source, "assets/binary_blob.bin",
                               {"__json__": False})

        backing = ArchiveBackingStore.capture(source)
        assert backing is not None
        assert backing.is_same_target(source)
        assert not backing.is_same_target(str(dst_dir / "copy.topos"))

        kwargs = golden_save_kwargs()
        project = kwargs.pop("project")
        dst_dir.mkdir(parents=True)
        target = str(dst_dir / "copy.topos")
        assert ToposProjectIO.save(project, target,
                                   backing_store=backing, **kwargs)

        assert "future_module/data.json" in _entry_names(target)
        assert _read_entry(target, "future_module/data.json") == {"schema": 9}
        assert "assets/binary_blob.bin" in _entry_names(target)

    def test_manifest_provenance_and_future_versions_follow(
        self, tmp_path,
    ) -> None:
        src_dir = tmp_path / "s"
        src_dir.mkdir()
        source = save_golden_package(str(src_dir), "p.topos")
        manifest = _read_entry(source, "manifest.json")
        manifest["schema_version"] = 42
        manifest["edit_content_schema_version"] = 9
        _inject_unknown_module(source, "manifest.json", manifest)

        backing = ArchiveBackingStore.capture(source)
        kwargs = golden_save_kwargs()
        project = kwargs.pop("project")
        target = str(tmp_path / "new" / "deep" / "copy.topos")
        assert ToposProjectIO.save(project, target,
                                   backing_store=backing, **kwargs)

        saved = _read_entry(target, "manifest.json")
        # never-downgrade：backing 的 future 版本戳随另存保真
        assert saved["schema_version"] == 42
        assert saved["edit_content_schema_version"] == 9
        assert saved["project_id"] == "golden-desk01a-0001"

    def test_embedded_resources_follow_save_as(self, tmp_path) -> None:
        """03B 主验收：另存新目录后内嵌原片随行、可再次打开解码。"""
        from tests.project.test_resource_resolver import (
            _build_embedded_package,
        )
        src_dir = tmp_path / "mobile"
        dst_dir = tmp_path / "desktop_save"
        src_dir.mkdir()
        dst_dir.mkdir()
        media = b"EMBEDDED-VIDEO-" + b"\x5a" * 4096
        source = _build_embedded_package(
            str(src_dir), "handoff.topos", media_payload=media)

        backing = ArchiveBackingStore.capture(source)
        # 模拟桌面接力：加载 → 编辑（不变）→ 另存新目录新文件名
        _, full_data = ToposProjectIO.load(source)
        kwargs = golden_save_kwargs()
        project = kwargs.pop("project")
        kwargs["full_assets_data"] = json.loads(json.dumps(full_data["assets"]))
        target = str(dst_dir / "desktop_edit.topos")
        assert ToposProjectIO.save(project, target,
                                   backing_store=backing, **kwargs)

        # 资源索引 + 资源二进制都随行（backing 的惰性成员流式回读）
        assert "resources/resources.json" in _entry_names(target)
        with zipfile.ZipFile(target) as zf:
            resource_entries = [n for n in zf.namelist()
                                if n.startswith("resources/")
                                and n != "resources/resources.json"]
        assert len(resource_entries) == 1
        # 换目录后打开：内嵌素材仍可解码（缓存非权威，包是自包含的）
        _, full2 = ToposProjectIO.load(target)
        assert full2 is not None
        path = full2["assets"]["golden-asset-video"]["path"]
        with open(path, "rb") as fh:
            assert fh.read() == media

    def test_same_path_save_backing_not_involved(self, tmp_path) -> None:
        """原地保存：backing 不参与（目标即最新），行为与旧路径一致。"""
        source = save_golden_package(str(tmp_path), "same.topos")
        before = _entry_names(source)
        backing = ArchiveBackingStore.capture(source)
        kwargs = golden_save_kwargs()
        project = kwargs.pop("project")
        assert ToposProjectIO.save(project, source,
                                   backing_store=backing, **kwargs)
        assert _entry_names(source) >= before

    def test_missing_source_package_fails_save_loudly(self, tmp_path) -> None:
        """来源包消失：保存受控失败（不静默丢资源）。"""
        src_dir = tmp_path / "s"
        src_dir.mkdir()
        source = save_golden_package(str(src_dir), "gone.topos")
        backing = ArchiveBackingStore.capture(source)
        kwargs = golden_save_kwargs()
        project = kwargs.pop("project")
        # backing 持有来源的 preserved 成员引用（黄金包无 assets 二进制，
        # 注入一个使流式回读必须触达来源文件）
        _inject_unknown_module(source, "assets/extra.bin", {"__json__": False})
        backing = ArchiveBackingStore.capture(source)
        os.remove(source)

        target = str(tmp_path / "t.topos")
        assert not ToposProjectIO.save(project, target,
                                       backing_store=backing, **kwargs)
        assert not os.path.exists(target)


class TestCacheIsNotAuthority:
    """03C：清缓存/移动包后再次打开正确。"""

    def _package_with_resource(self, tmp_path, media: bytes) -> str:
        from tests.project.test_resource_resolver import (
            _build_embedded_package,
        )
        return _build_embedded_package(
            str(tmp_path), "portable.topos", media_payload=media)

    def test_reopen_after_cache_delete(self, tmp_path) -> None:
        path = self._package_with_resource(str(tmp_path), b"CACHE-DELETE" * 64)
        _, first = ToposProjectIO.load(path)
        materialized = first["assets"]["golden-asset-video"]["path"]
        assert os.path.isfile(materialized)

        # 清空整个解包缓存 → 再次打开仍正确（重新解包 + 校验）
        shutil.rmtree(os.path.join(str(tmp_path), ".topos_cache"))
        assert not os.path.exists(materialized)
        _, second = ToposProjectIO.load(path)
        path2 = second["assets"]["golden-asset-video"]["path"]
        with open(path2, "rb") as fh:
            assert fh.read() == b"CACHE-DELETE" * 64

    def test_reopen_after_moving_package(self, tmp_path) -> None:
        home = tmp_path / "phone_export"
        elsewhere = tmp_path / "moved" / "by" / "usb"
        home.mkdir()
        path = self._package_with_resource(str(home), b"MOVED-PKG" * 64)

        elsewhere.mkdir(parents=True)
        new_path = str(elsewhere / "portable.topos")
        shutil.move(path, new_path)
        _, full = ToposProjectIO.load(new_path)
        with open(full["assets"]["golden-asset-video"]["path"], "rb") as fh:
            assert fh.read() == b"MOVED-PKG" * 64
