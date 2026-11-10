"""SR04：受管资源稳定引用与库迁移验收。

对应《第二轮复查计划》SR04（2026-11-10）：
- 引用模块（gallery_resource_refs）：受管引用解析/非法引用拒绝；
- 迁移：只把**内容证据确凿**（位于库 ``LUTs/`` 且文件名 hash 前缀与
  内容一致）的绝对引用升级为受管引用；外部/相对/前缀不符的引用不猜；
- 导出侧：受管引用解析到**当前库根**后随包携带——库迁移后导出仍完整。
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from src.features.color.core.gallery_bundle_service import GalleryBundleService
from src.features.color.core.gallery_repository import GalleryFolderRepository
from src.features.color.core.gallery_resource_refs import (
    managed_lut_reference,
    parse_managed_lut_reference,
    resolve_lut_reference,
)
from src.features.color.core.gallery_transfer_service import (
    GalleryTransferService,
)
from src.features.color.core.preset_meta import PresetMeta, save_preset_meta

LUT_BYTES = b"LUT3D SR04 payload"


class TestReferenceEncoding:
    def test_roundtrip_and_non_managed(self):
        ref = managed_lut_reference("ab12cd34_look.cube")
        assert ref == "topos-lut://LUTs/ab12cd34_look.cube"
        assert parse_managed_lut_reference(ref) == "ab12cd34_look.cube"
        assert parse_managed_lut_reference("/abs/lut.cube") is None
        assert parse_managed_lut_reference("") is None
        assert parse_managed_lut_reference(None) is None

    def test_malformed_managed_refs_rejected(self):
        assert parse_managed_lut_reference("topos-lut://etc/passwd") is None
        assert parse_managed_lut_reference(
            "topos-lut://LUTs/../x.cube") is None
        assert parse_managed_lut_reference("topos-lut://LUTs/") is None
        assert parse_managed_lut_reference(
            "topos-lut://LUTs/sub/x.cube") is None

    def test_resolve_uses_current_library_root(self, tmp_path):
        ref = managed_lut_reference("ab12cd34_look.cube")
        first = resolve_lut_reference(ref, str(tmp_path / "Presets"))
        second = resolve_lut_reference(ref, str(tmp_path / "Moved"))
        assert first != second                       # 随库根换算
        assert first.endswith("Presets/LUTs/ab12cd34_look.cube")
        # 非受管引用原样通过（不猜路径）
        assert resolve_lut_reference(
            "/user/own.cube", str(tmp_path)) == "/user/own.cube"


def _seed_grade(folder: Path, stem: str, ref: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    topos = folder / f"{stem}.topos"
    with zipfile.ZipFile(topos, "w") as zf:
        zf.writestr("project.json", json.dumps(
            {"layers": [{"name": "g", "active_lut_path": ref}]}))
    save_preset_meta(str(topos), PresetMeta(still_id=f"S-{stem}"))
    return topos


@pytest.fixture()
def library(tmp_path: Path):
    root = tmp_path / "Presets"
    (root / "User").mkdir(parents=True)
    digest = hashlib.sha256(LUT_BYTES).hexdigest()
    lut_dir = root / "LUTs"
    lut_dir.mkdir()
    lut = lut_dir / f"{digest[:8]}_look.cube"
    lut.write_bytes(LUT_BYTES)
    service = GalleryTransferService(
        GalleryFolderRepository(str(root)), GalleryBundleService(str(root)))
    return root, digest, service


class TestMigration:
    def test_provable_absolute_ref_upgraded(self, library):
        root, digest, service = library
        lut_path = root / "LUTs" / f"{digest[:8]}_look.cube"
        _seed_grade(root / "User", "Look", str(lut_path))
        warnings = service.migrate_album_lut_references(str(root / "User"))
        assert any("migrated" in w for w in warnings)
        with zipfile.ZipFile(root / "User" / "Look.topos") as zf:
            project = json.loads(zf.read("project.json"))
        assert project["layers"][0]["active_lut_path"] == \
            managed_lut_reference(f"{digest[:8]}_look.cube")
        # 幂等：再跑一次无新迁移
        again = service.migrate_album_lut_references(str(root / "User"))
        assert not [w for w in again if "migrated" in w]

    def test_foreign_and_unproven_refs_untouched(self, library, tmp_path):
        root, digest, service = library
        outside = tmp_path / "outside.cube"
        outside.write_bytes(b"OUTSIDE")
        unproven = root / "LUTs" / "deadbeef_other.cube"
        unproven.write_bytes(b"PREFIX DOES NOT MATCH CONTENT")
        _seed_grade(root / "User", "Outside", str(outside))
        _seed_grade(root / "User", "Unproven", str(unproven))
        _seed_grade(root / "User", "Relative", "relative/look.cube")
        service.migrate_album_lut_references(str(root / "User"))
        for stem, expect in (("Outside", str(outside)),
                             ("Unproven", str(unproven)),
                             ("Relative", "relative/look.cube")):
            with zipfile.ZipFile(root / "User" / f"{stem}.topos") as zf:
                project = json.loads(zf.read("project.json"))
            assert project["layers"][0]["active_lut_path"] == expect

    def test_migrated_library_moves_and_still_exports(self, library,
                                                      tmp_path):
        """迁移后整库搬家：受管引用导出仍把 LUT 打进包（SR04 验收）。"""
        root, digest, service = library
        lut_path = root / "LUTs" / f"{digest[:8]}_look.cube"
        _seed_grade(root / "User", "Look", str(lut_path))
        service.migrate_album_lut_references(str(root / "User"))

        moved = tmp_path / "MovedPresets"
        root.rename(moved)
        moved_service = GalleryTransferService(
            GalleryFolderRepository(str(moved)),
            GalleryBundleService(str(moved)))
        archive = moved / "Look.toposbundle"
        result = moved_service.export_bundle(
            str(moved / "User"), "Look", str(archive))
        assert result.succeeded
        assert result.warnings and any("portable: full" in w
                                        for w in result.warnings)
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            manifest = json.loads(zf.read("bundle_manifest.json"))
        assert any(n.startswith("assets/luts/") for n in names)
        assert manifest["portable"] is True
