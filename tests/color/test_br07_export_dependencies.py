"""BR07：导出包依赖可移植性与缺失依赖反馈验收。

对应《Gallery/Viewport 后端复查整改计划》BR07（2026-11-10）：
- grade 的外部 LUT（``active_lut_path``）默认随包携带（``assets/luts/``，
  manifest schema 2 ``dependencies`` 记录内容 hash／来源）；
- 导出后**移走原 LUT**，在另一临时库／不同根路径导入：LUT 安装进目标
  库受管 ``LUTs/``，grade 引用重写，内容 hash 与原文件一致；
- 缺失依赖不静默称完整成功（warnings + ``portable=false``）；
- 导入侧越界/缺件/坏 hash 的依赖条目拒绝，不落散文件。
"""
from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

from src.features.color.core.gallery_bundle_service import GalleryBundleService
from src.features.color.core.gallery_repository import GalleryFolderRepository
from src.features.color.core.gallery_transfer_service import (
    BUNDLE_MANIFEST_NAME,
    GalleryTransferService,
)
from src.features.color.core.preset_meta import PresetMeta, save_preset_meta

LUT_BYTES = b"LUT3D\n1 1 1\ndata..." * 16


def _seed(folder: Path, stem: str, *, lut_ref: str | None,
          still_id: str = "S-br07") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    topos = folder / f"{stem}.topos"
    layers = ([{"name": "g", "active_lut_path": lut_ref}]
              if lut_ref else [{"name": "g"}])
    with zipfile.ZipFile(topos, "w") as zf:
        zf.writestr("project.json", json.dumps({"layers": layers}))
    (folder / f"{stem}.thumb.jpg").write_bytes(b"thumb")
    save_preset_meta(str(topos), PresetMeta(still_id=still_id))
    return topos


def _svc(base: Path) -> GalleryTransferService:
    root = base / "Presets"    # 库根＝Presets 目录（与生产一致）
    return GalleryTransferService(
        GalleryFolderRepository(str(root)), GalleryBundleService(str(root)))


@pytest.fixture()
def roots(tmp_path: Path):
    (tmp_path / "a" / "Presets" / "User").mkdir(parents=True)
    (tmp_path / "b" / "Presets" / "System").mkdir(parents=True)
    return tmp_path / "a", tmp_path / "b"


class TestPortableRoundtrip:
    def test_export_packs_lut_import_installs_and_rewrites(
            self, roots, tmp_path):
        a, b = roots
        lut_dir = tmp_path / "original-luts"
        lut_dir.mkdir()
        lut = lut_dir / "look.cube"
        lut.write_bytes(LUT_BYTES)
        _seed(a / "Presets" / "User", "Look", lut_ref=str(lut))

        svc_a = _svc(a)
        archive = a / "Look.toposbundle"
        result = svc_a.export_bundle(
            str(a / "Presets" / "User"), "Look", str(archive))
        assert result.succeeded
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            manifest = json.loads(zf.read(BUNDLE_MANIFEST_NAME))
        dep_entries = [n for n in names if n.startswith("assets/luts/")]
        assert len(dep_entries) == 1
        assert manifest["portable"] is True
        assert manifest["dependencies"][0]["sha256"] == \
            __import__("hashlib").sha256(LUT_BYTES).hexdigest()

        # 移走原 LUT——包自身已携带内容
        lut.unlink()

        # 另一临时库（不同根路径）导入：安装 + 重写引用
        svc_b = _svc(b)
        target = b / "Presets" / "System"
        imported = svc_b.import_bundle(str(archive), str(target))
        assert imported.succeeded, imported.warnings
        top_lut = b / "Presets" / "LUTs"
        installed = list(top_lut.glob("*.cube"))
        assert len(installed) == 1
        assert installed[0].read_bytes() == LUT_BYTES   # 相同内容 hash
        with zipfile.ZipFile(target / "Look.topos") as zf:
            project = json.loads(zf.read("project.json"))
        rewritten = project["layers"][0]["active_lut_path"]
        assert os.path.isfile(rewritten)               # 解析得到
        assert Path(rewritten).read_bytes() == LUT_BYTES

        # 二次导入（重名顺延）：LUT 幂等复用，不重复安装
        again = svc_b.import_bundle(str(archive), str(target))
        assert again.succeeded
        assert len(list(top_lut.glob("*.cube"))) == 1

    def test_missing_lut_reference_package_is_honest(self, roots):
        a, b = roots
        _seed(a / "Presets" / "User", "Look",
              lut_ref="/gone/luts/look.cube")
        svc = _svc(a)
        archive = a / "Look.toposbundle"
        result = svc.export_bundle(
            str(a / "Presets" / "User"), "Look", str(archive))
        assert result.succeeded                       # 导出可用
        assert any("dependency not bundled" in w for w in result.warnings)
        assert any("portable: partial" in w for w in result.warnings)
        with zipfile.ZipFile(archive) as zf:
            manifest = json.loads(zf.read(BUNDLE_MANIFEST_NAME))
        assert manifest["portable"] is False
        assert manifest["dependencies"][0]["missing"] is True

        imported = _svc(b).import_bundle(
            str(archive), str(b / "Presets" / "System"))
        assert imported.succeeded
        assert any("lut not in package" in w for w in imported.warnings)
        # grade 保留原引用（缺失如实可见，不猜路径）
        with zipfile.ZipFile(b / "Presets" / "System" / "Look.topos") as zf:
            project = json.loads(zf.read("project.json"))
        assert project["layers"][0]["active_lut_path"] == "/gone/luts/look.cube"

    def test_schema1_package_still_importable(self, roots):
        """schema 1（无依赖清单）向下兼容：可导入、引用不重写。"""
        a, b = roots
        topos = _seed(a / "Presets" / "User", "Old",
                      lut_ref="/luts/legacy.cube")
        archive = a / "legacy.toposbundle"
        svc = _svc(a)
        assert svc.export_bundle(
            str(a / "Presets" / "User"), "Old", str(archive)).succeeded
        with zipfile.ZipFile(archive) as zf:
            entries = {n: zf.read(n) for n in zf.namelist()}
        manifest = json.loads(entries[BUNDLE_MANIFEST_NAME])
        manifest["schema"] = 1
        manifest.pop("dependencies", None)
        manifest.pop("portable", None)
        entries[BUNDLE_MANIFEST_NAME] = json.dumps(manifest).encode()
        with zipfile.ZipFile(archive, "w") as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
        del topos
        imported = _svc(b).import_bundle(
            str(archive), str(b / "Presets" / "System"))
        assert imported.succeeded

    def test_dependency_path_escape_rejected(self, roots):
        a, b = roots
        lut = a / "real.cube"
        lut.write_bytes(LUT_BYTES)
        _seed(a / "Presets" / "User", "Look", lut_ref=str(lut))
        archive = a / "evil.toposbundle"
        svc = _svc(a)
        assert svc.export_bundle(
            str(a / "Presets" / "User"), "Look", str(archive)).succeeded
        with zipfile.ZipFile(archive) as zf:
            entries = {n: zf.read(n) for n in zf.namelist()}
        manifest = json.loads(entries[BUNDLE_MANIFEST_NAME])
        manifest["dependencies"][0]["archive"] = "assets/luts/../../evil.cube"
        entries[BUNDLE_MANIFEST_NAME] = json.dumps(manifest).encode()
        with zipfile.ZipFile(archive, "w") as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
        result = _svc(b).import_bundle(
            str(archive), str(b / "Presets" / "System"))
        assert result.status == "rejected"
        assert result.reason == "dependency_path_escape"
        assert not [n for n in os.listdir(b / "Presets" / "System")
                    if n.startswith("Look")]

    def test_dependency_hash_tamper_rejected(self, roots):
        a, b = roots
        lut = a / "real.cube"
        lut.write_bytes(LUT_BYTES)
        _seed(a / "Presets" / "User", "Look", lut_ref=str(lut))
        archive = a / "t.toposbundle"
        svc = _svc(a)
        assert svc.export_bundle(
            str(a / "Presets" / "User"), "Look", str(archive)).succeeded
        with zipfile.ZipFile(archive) as zf:
            entries = {n: zf.read(n) for n in zf.namelist()}
        dep_name = next(n for n in entries if n.startswith("assets/luts/"))
        entries[dep_name] = LUT_BYTES + b"TAMPERED"
        with zipfile.ZipFile(archive, "w") as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
        result = _svc(b).import_bundle(
            str(archive), str(b / "Presets" / "System"))
        assert result.status == "rejected"
        assert result.reason == "hash_mismatch"
        assert not list((b / "Presets" / "LUTs").glob("*.cube")) \
            if (b / "Presets" / "LUTs").exists() else True
