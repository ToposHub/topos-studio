"""G08 Gallery bundle 导入／导出测试：可移植包、安全限制、身份政策。

门槛映射：导出不动源、失败不留半目标；跨目录重导入恢复 grade＋参考＋
alpha 声明；恶意（路径穿越/symlink/超大/未来 schema）拒绝且不落散文件；
hash 篡改拒绝；同 ID 同内容＝重复拒绝、同 ID 不同内容＝换发本库新 ID
（origin 保留）；重名自动分配且结果可审阅；只 JPEG 导出不称 HDR。
"""
import json
import os
import zipfile

import pytest

from src.features.color.core.gallery_bundle_service import GalleryBundleService
from src.features.color.core.gallery_repository import GalleryFolderRepository
from src.features.color.core.gallery_transfer_service import (
    BUNDLE_MANIFEST_NAME,
    GalleryTransferService,
)
from src.features.color.core.preset_meta import (
    PresetMeta,
    load_preset_meta,
    save_preset_meta,
    still_alpha_declaration,
)

ALL_SUFFIXES = (".topos", ".thumb.jpg", ".still.jpg", ".meta.json",
                ".still.exr", ".still.exr.json")


def _seed_bundle(folder, stem, *, still_id="S-origin000000001",
                 label=None, grade_content=None):
    os.makedirs(folder, exist_ok=True)
    grade = grade_content or json.dumps({"layers": [
        {"name": "g", "active_lut_path": "/luts/a.cube"}]})
    with zipfile.ZipFile(os.path.join(folder, f"{stem}.topos"), "w") as zf:
        zf.writestr("project.json", grade)
    for suffix, data in ((".thumb.jpg", b"thumb"), (".still.jpg", b"still"),
                         (".still.exr", b"exr-bytes")):
        with open(os.path.join(folder, f"{stem}{suffix}"), "wb") as fh:
            fh.write(data)
    with open(os.path.join(folder, f"{stem}.still.exr.json"), "w") as fh:
        json.dump({"alpha_semantics": "premultiplied", "channels": 4,
                   "precision": "float32"}, fh)
    save_preset_meta(os.path.join(folder, f"{stem}.topos"), PresetMeta(
        still_id=still_id, label=label, timeline_id="t1",
        timeline_name="Main"))


@pytest.fixture
def svc(tmp_path):
    root = tmp_path / "Presets"
    (root / "User").mkdir(parents=True)
    (root / "System").mkdir(parents=True)
    repository = GalleryFolderRepository(str(root))
    return GalleryTransferService(
        repository, GalleryBundleService(str(root))), str(root)


def _folder(svc_root, name="User"):
    return os.path.join(svc_root, name)


class TestExport:
    def test_export_produces_portable_zip_without_touching_source(self, svc):
        service, root = svc
        folder = _folder(root)
        _seed_bundle(folder, "Look")
        dest = os.path.join(root, "Look.toposbundle")
        result = service.export_bundle(folder, "Look", dest)
        assert result.succeeded
        with zipfile.ZipFile(dest) as zf:
            names = set(zf.namelist())
            assert "Look.topos" in names and "Look.still.exr.json" in names
            manifest = json.loads(zf.read(BUNDLE_MANIFEST_NAME))
        assert manifest["kind"] == "topos-bundle" and manifest["schema"] == 1
        assert manifest["still"]["still_id"] == "S-origin000000001"
        assert manifest["still"]["timeline_name"] == "Main"
        assert manifest["precision"] == "exr_float32"
        assert manifest["alpha_semantics"] == "premultiplied"
        assert manifest["grade_lut_reference_count"] == 1   # LUT 引用汇总
        assert any(f["role"] == "grade" and len(f["sha256"]) == 64
                   for f in manifest["files"])
        # 源 bundle 原样
        for suffix in ALL_SUFFIXES:
            assert os.path.exists(os.path.join(folder, f"Look{suffix}"))

    def test_export_without_exr_declares_jpeg_not_hdr(self, svc):
        service, root = svc
        folder = _folder(root)
        _seed_bundle(folder, "J")
        dest = os.path.join(root, "j.toposbundle")
        result = service.export_bundle(folder, "J", dest, include_exr=False)
        assert result.succeeded
        with zipfile.ZipFile(dest) as zf:
            names = zf.namelist()
            manifest = json.loads(zf.read(BUNDLE_MANIFEST_NAME))
        assert not any(n.endswith(".still.exr") for n in names)
        assert manifest["precision"] == "jpeg8"      # 只 JPEG 不称 HDR
        assert manifest["exr_included"] is False

    def test_export_failure_leaves_no_half_target(self, svc, monkeypatch):
        service, root = svc
        folder = _folder(root)
        _seed_bundle(folder, "X")
        dest = os.path.join(root, "out.toposbundle")
        import src.features.color.core.gallery_transfer_service as mod

        def boom(src, dst):
            raise OSError(28, "No space left on device")
        monkeypatch.setattr(mod.os, "replace", boom)
        result = service.export_bundle(folder, "X", dest)
        monkeypatch.setattr(mod.os, "replace", os.replace)
        assert result.status == "rejected"
        assert not os.path.exists(dest)
        assert not [n for n in os.listdir(root) if ".tmp-" in n]


class TestInspectAndImport:
    def _export(self, service, root, stem="Look"):
        folder = _folder(root)
        _seed_bundle(folder, stem)
        dest = os.path.join(root, f"{stem}.toposbundle")
        assert service.export_bundle(folder, stem, dest).succeeded
        return dest

    def test_roundtrip_restores_grade_refs_and_alpha(self, svc, tmp_path):
        service, root = svc
        dest = self._export(service, root)
        target = _folder(root, "System")
        result = service.import_bundle(dest, target)
        assert result.succeeded
        assert os.path.isfile(os.path.join(target, "Look.topos"))
        assert still_alpha_declaration(
            os.path.join(target, "Look.topos")) == "premultiplied"
        meta = load_preset_meta(os.path.join(target, "Look.topos"))
        assert meta.still_id == "S-origin000000001"   # 无冲突保留原 ID
        with zipfile.ZipFile(os.path.join(target, "Look.topos")) as zf:
            assert b"active_lut_path" in zf.read("project.json")
        # 无散文件（事务目录清干净）
        assert not [n for n in os.listdir(target)
                    if n.startswith(".gallery-import")]

    def test_name_conflict_auto_allocates_auditable_suffix(self, svc):
        service, root = svc
        # User 里已有同名的另一条目（不同 ID/内容 → 非重复导入）
        _seed_bundle(_folder(root), "Look", still_id="S-user111111111111")
        system = _folder(root, "System")
        _seed_bundle(system, "Look", still_id="S-other2222222222",
                     grade_content=json.dumps({"layers": [{"n": 2}]}))
        dest = os.path.join(root, "look.toposbundle")
        assert service.export_bundle(system, "Look", dest).succeeded
        result = service.import_bundle(dest, _folder(root))
        assert result.succeeded
        assert result.affected_still_ids[0].startswith("Look_")
        assert any("imported as" in w for w in result.warnings)
        assert os.path.isfile(os.path.join(_folder(root), "Look_1.topos"))

    def test_same_id_same_content_rejected_as_duplicate(self, svc):
        service, root = svc
        dest = self._export(service, root)     # User/Look 已在场同内容
        result = service.import_bundle(dest, _folder(root), new_name="Copy")
        assert result.status == "rejected"
        assert result.reason == "duplicate_identity"
        # 拒绝不落任何文件
        assert not os.path.exists(os.path.join(_folder(root), "Copy.topos"))

    def test_same_id_different_content_reallocates_local_id(self, svc):
        service, root = svc
        folder = _folder(root)
        _seed_bundle(folder, "Look")
        dest = os.path.join(root, "look.toposbundle")
        # 另一部机器上同一 ID 的不同内容
        other = os.path.join(root, "other")
        os.makedirs(other)
        _seed_bundle(other, "Look", grade_content=json.dumps(
            {"layers": [{"name": "different"}]}))
        assert service.export_bundle(other, "Look", dest,
                                     include_exr=False).succeeded
        result = service.import_bundle(dest, folder)
        assert result.succeeded
        imported_stem = result.affected_still_ids[0]     # Look_1（重名）
        meta = load_preset_meta(
            os.path.join(folder, f"{imported_stem}.topos"))
        assert meta.still_id != "S-origin000000001"      # 本库新 ID
        assert meta.extra_fields["origin_still_id"] == "S-origin000000001"
        assert any("origin" in w for w in result.warnings)

    def _make_zip(self, path, entries):
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in entries.items():
                if isinstance(data, bytes):
                    zf.writestr(name, data)
                else:
                    zf.writestr(name, data, compress_type=data[0]
                                if isinstance(data, tuple) else None)

    def test_path_traversal_rejected_without_extraction(self, svc):
        service, root = svc
        evil = os.path.join(root, "evil.toposbundle")
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("../escape.sh", b"rm -rf")
            zf.writestr(BUNDLE_MANIFEST_NAME, json.dumps(
                {"schema": 1, "kind": "topos-bundle",
                 "files": [{"role": "grade", "name": "../escape.sh",
                            "sha256": "x"}]}))
        result = service.import_bundle(evil, _folder(root))
        assert result.status == "rejected"
        assert result.reason == "path_escape"
        assert not os.path.exists(os.path.join(root, "escape.sh"))

    def test_future_schema_rejected(self, svc):
        service, root = svc
        future = os.path.join(root, "future.toposbundle")
        with zipfile.ZipFile(future, "w") as zf:
            zf.writestr(BUNDLE_MANIFEST_NAME, json.dumps(
                {"schema": 99, "kind": "topos-bundle", "files": []}))
        inspection = service.inspect_bundle(future)
        assert inspection.ok is False
        assert inspection.reasons[0][0] == "future_schema"

    def test_size_and_count_limits_rejected(self, svc):
        service, root = svc
        big = os.path.join(root, "big.toposbundle")
        with zipfile.ZipFile(big, "w") as zf:
            for index in range(20):
                zf.writestr(f"f{index}.dat", b"x" * 10)
        inspection = service.inspect_bundle(big)
        reasons = {r for r, _ in inspection.reasons}
        assert "file_count_limit" in reasons or "not_a_bundle" in reasons

    def test_hash_tamper_rejected_before_install(self, svc):
        service, root = svc
        dest = self._export(service, root)
        target = _folder(root, "System")
        # 篡改 zip 内 grade 内容（保住 manifest 的旧 hash）
        tampered = os.path.join(root, "tampered.toposbundle")
        with zipfile.ZipFile(dest) as src, \
                zipfile.ZipFile(tampered, "w") as dst:
            for info in src.infolist():
                data = src.read(info.filename)
                if info.filename.endswith(".topos"):
                    data = data.replace(b"layers", b"layerz")
                dst.writestr(info, data)
        result = service.import_bundle(tampered, target)
        assert result.status == "rejected"
        assert result.reason == "hash_mismatch"
        assert not [n for n in os.listdir(target)
                    if n.startswith("Look")]

    def test_symlink_entry_rejected(self, svc):
        service, root = svc
        evil = os.path.join(root, "link.toposbundle")
        with zipfile.ZipFile(evil, "w") as zf:
            info = zipfile.ZipInfo("Link.topos")
            info.external_attr = (0o120777 << 16)   # S_IFLNK
            zf.writestr(info, "/etc/passwd")
            zf.writestr(BUNDLE_MANIFEST_NAME, json.dumps(
                {"schema": 1, "kind": "topos-bundle", "files": []}))
        inspection = service.inspect_bundle(evil)
        assert ("path_escape", "symlink: Link.topos") in inspection.reasons

    def test_missing_grade_rejected(self, svc):
        service, root = svc
        no_grade = os.path.join(root, "nograde.toposbundle")
        with zipfile.ZipFile(no_grade, "w") as zf:
            zf.writestr("X.thumb.jpg", b"t")
            zf.writestr(BUNDLE_MANIFEST_NAME, json.dumps(
                {"schema": 1, "kind": "topos-bundle",
                 "files": [{"role": "thumbnail", "name": "X.thumb.jpg",
                            "sha256": "0" * 64}]}))
        inspection = service.inspect_bundle(no_grade)
        assert inspection.ok is False
        assert any(r == "missing_grade" for r, _ in inspection.reasons)
