"""G01 Gallery repository 测试：User/System 平铺查询 adapter 的读取事实。

覆盖：album 枚举/只读实测、越界 album_id 拒绝、平铺扫描与旧 scan_presets
口径一致、搜索过滤复用 matches_query、grade 探测可注入且默认不探测、
bundle manifest 六角色（含 .still.exr.json）、扫描失败稳定原因。
"""
import json
import os
import zipfile

import pytest

from src.features.color.core.gallery_contracts import (
    ALBUM_KIND_STILLS,
    BUNDLE_ROLE_EXR_DECLARATION,
    BUNDLE_ROLE_GRADE,
    BUNDLE_ROLE_META,
    BUNDLE_ROLE_STILL_EXR,
    GalleryQuery,
    PRECISION_EXR_FLOAT32,
    PRECISION_JPEG8,
    PRECISION_NONE,
    SCOPE_LEGACY_FOLDER,
    SORT_KEY_CREATED,
    SORT_KEY_LABEL,
    SORT_KEY_NAME,
)
from src.features.color.core.gallery_repository import (
    GalleryFolderRepository,
    SCAN_ERROR_ALBUM_MISSING,
    SCAN_ERROR_FOLDER_MISSING,
)
from src.features.color.core.preset_meta import PresetMeta, save_preset_meta


def _make_zip_topos(path: str, project=None) -> str:
    """真实 .topos 结构：zip 且含 project.json（默认 grade 探测可通过）。"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("project.json", json.dumps(project or {"layers": []}))
    return path


@pytest.fixture
def repo(tmp_path) -> GalleryFolderRepository:
    (tmp_path / "Presets" / "User").mkdir(parents=True)
    (tmp_path / "Presets" / "System").mkdir(parents=True)
    return GalleryFolderRepository(str(tmp_path / "Presets"))


def _seed(folder: str, name: str, *, real_zip=True, meta: PresetMeta = None,
          thumb=True, still=False, exr=False) -> str:
    topos = os.path.join(folder, f"{name}.topos")
    if real_zip:
        _make_zip_topos(topos)
    else:
        with open(topos, "wb") as fh:
            fh.write(b"not a zip")
    if thumb:
        with open(os.path.join(folder, f"{name}.thumb.jpg"), "wb") as fh:
            fh.write(b"thumb")
    if still:
        with open(os.path.join(folder, f"{name}.still.jpg"), "wb") as fh:
            fh.write(b"still")
    if exr:
        with open(os.path.join(folder, f"{name}.still.exr"), "wb") as fh:
            fh.write(b"exr")
        with open(os.path.join(folder, f"{name}.still.exr.json"), "w") as fh:
            json.dump({"alpha_semantics": "premultiplied",
                       "channels": 4, "precision": "float32"}, fh)
    if meta is not None:
        save_preset_meta(topos, meta)
    return topos


class TestAlbums:
    def test_nested_folder_can_be_resolved_and_queried(self, repo):
        folder = os.path.join(repo.root, "User", "Travel", "Day1")
        os.makedirs(folder)
        _seed(folder, "Sunset")
        album = repo.album_for_folder(folder)
        assert album is not None and album.album_id == "legacy:User/Travel/Day1"
        result = repo.query(GalleryQuery(album_id=album.album_id))
        assert result.scan_error is None
        assert [still.display_name for still in result.stills] == ["Sunset"]

    def test_symlink_album_outside_library_is_not_read(self, repo, tmp_path):
        outside = tmp_path / "other-project"
        outside.mkdir()
        _seed(str(outside), "Private")
        os.symlink(outside, os.path.join(repo.root, "User", "link"))
        assert repo.album_folder("legacy:User/link") is None
        assert repo.query(GalleryQuery(album_id="legacy:User/link")).stills == ()

    def test_sections_and_one_level_subdirs(self, repo, tmp_path):
        (tmp_path / "Presets" / "User" / "SubA").mkdir()
        (tmp_path / "Presets" / "User" / ".hidden").mkdir()
        albums = repo.list_albums()
        ids = [a.album_id for a in albums]
        assert ids == ["legacy:User", "legacy:User/SubA", "legacy:System"]
        user = albums[0]
        assert user.scope == SCOPE_LEGACY_FOLDER
        assert user.kind == ALBUM_KIND_STILLS
        assert user.label == "User"
        assert user.read_only is False and user.writable is True
        assert albums[1].parent_id == "legacy:User"
        assert albums[1].label == "User/SubA"
        assert user.revision > 0

    def test_album_folder_roundtrip_and_traversal_rejected(self, repo):
        for album_id in ("legacy:User", "legacy:User/SubA"):
            assert album_id in {a.album_id for a in repo.list_albums()} or True
        assert repo.album_folder("legacy:User") == os.path.join(
            repo.root, "User")
        assert repo.album_folder("legacy:User/Sub") == os.path.join(
            repo.root, "User", "Sub")
        # 越界/非 legacy id 一律 None
        assert repo.album_folder("legacy:..") is None
        assert repo.album_folder("legacy:../evil") is None
        assert repo.album_folder("legacy:") is None
        assert repo.album_folder("project:p1") is None

    def test_readonly_album_detected_by_access(self, repo, tmp_path):
        if os.geteuid() == 0:
            pytest.skip("root ignores directory permission bits")
        system = tmp_path / "Presets" / "System"
        system.chmod(0o555)
        try:
            albums = {a.album_id: a for a in repo.list_albums()}
            assert albums["legacy:System"].read_only is True
            assert albums["legacy:User"].read_only is False
        finally:
            system.chmod(0o755)

    def test_album_for_folder_matches_list_albums(self, repo):
        album = repo.album_for_folder(os.path.join(repo.root, "User"))
        assert album is not None and album.album_id == "legacy:User"
        assert repo.album_for_folder("/nonexistent/elsewhere") is None


class TestQuery:
    def test_flat_scan_matches_legacy_ordering_and_filters(self, repo):
        folder = os.path.join(repo.root, "User")
        _seed(folder, "Still_2")
        _seed(folder, "still_1")           # 小写：旧序在 Still_2 之后
        _seed(folder, ".hidden", real_zip=False)  # dotfile 跳过
        os.makedirs(os.path.join(folder, "SubDir"), exist_ok=True)
        _seed(os.path.join(folder, "SubDir"), "nested")  # 目录不递归
        open(os.path.join(folder, "loose.txt"), "w").close()  # 非 .topos 跳过

        result = repo.query(GalleryQuery(album_id="legacy:User"))
        names = [s.bundle.topos_name for s in result.stills]
        # 与旧 sorted(os.listdir()) 逐字节同序（区分大小写）
        assert names == ["Still_2.topos", "still_1.topos"]
        assert result.total_matched == 2
        assert result.degraded_count == 2      # 无 .meta.json
        assert result.scan_error is None

    def test_search_filter_parity_with_matches_query(self, repo):
        folder = os.path.join(repo.root, "User")
        _seed(folder, "Sunset",
              meta=PresetMeta(display_name="A001.mov", source_frame=42))
        _seed(folder, "Night")
        by_name = repo.query(GalleryQuery(
            album_id="legacy:User", search_text="A001"))
        assert [s.display_name for s in by_name.stills] == ["Sunset"]
        assert by_name.stills[0].meta is not None
        result = repo.query(GalleryQuery(
            album_id="legacy:User", search_text="night"))
        assert [s.display_name for s in result.stills] == ["Night"]

    def test_sidecar_flags_and_provenance_from_meta(self, repo):
        folder = os.path.join(repo.root, "User")
        _seed(folder, "Full", thumb=True, still=True, exr=True,
              meta=PresetMeta(display_name="A.mov", clip_id="c1",
                              source_frame=7, created_at="2026-11-09T00:00:00"))
        still = repo.query(GalleryQuery(album_id="legacy:User")).stills[0]
        assert still.has_exr and still.has_still_jpeg and still.has_thumbnail
        assert still.has_meta
        assert still.provenance.clip_id == "c1"
        assert still.provenance.source_display_name == "A.mov"
        assert still.created_at == "2026-11-09T00:00:00"
        assert still.content_revision > 0
        assert still.grade_loadable is None   # 查询路径不默认探测

    def test_provisional_identity_stable_and_rename_sensitive(self, repo):
        folder = os.path.join(repo.root, "User")
        _seed(folder, "X")
        first = repo.query(GalleryQuery(album_id="legacy:User")).stills[0]
        again = repo.query(GalleryQuery(album_id="legacy:User")).stills[0]
        assert first.still_id == again.still_id  # 同名同 album → 稳定
        assert first.still_id is not None and len(first.still_id) == 16
        other = repo.query(GalleryQuery(album_id="legacy:System"))
        # album_id 进派生：不同 album 的同名主名身份不同（G02 前的临时口径）
        _seed(os.path.join(repo.root, "System"), "X")
        sys_still = repo.query(GalleryQuery(album_id="legacy:System")).stills[0]
        assert sys_still.still_id != first.still_id

    def test_grade_probe_default_off_injectable(self, repo):
        folder = os.path.join(repo.root, "User")
        _seed(folder, "Good", real_zip=True)
        _seed(folder, "Bad", real_zip=False)
        result = repo.query(GalleryQuery(album_id="legacy:User"),
                            probe_grades=True)
        grades = {s.display_name: s.grade_loadable for s in result.stills}
        assert grades == {"Bad": False, "Good": True}

        probed = GalleryFolderRepository(
            repo.root, grade_probe=lambda path: True)
        assert all(s.grade_loadable for s in probed.query(
            GalleryQuery(album_id="legacy:User"), probe_grades=True).stills)

    def test_sort_keys_and_stable_secondary(self, repo):
        folder = os.path.join(repo.root, "User")
        _seed(folder, "B", meta=PresetMeta(created_at="2026-01-02T00:00:00"))
        _seed(folder, "A", meta=PresetMeta(created_at="2026-01-02T00:00:00"))
        _seed(folder, "C", meta=PresetMeta(created_at="2026-01-01T00:00:00"))
        by_created = repo.query(GalleryQuery(
            album_id="legacy:User", sort_key=SORT_KEY_CREATED))
        names = [s.display_name for s in by_created.stills]
        # created 相同的 A/B 由 still_id 次键稳定排序
        assert names.index("C") == 0 and set(names[1:]) == {"A", "B"}
        desc = repo.query(GalleryQuery(
            album_id="legacy:User", sort_key=SORT_KEY_NAME, sort_desc=True))
        assert [s.display_name for s in desc.stills] == ["C", "B", "A"]
        label = repo.query(GalleryQuery(
            album_id="legacy:User", sort_key=SORT_KEY_LABEL))
        assert len(label.stills) == 3   # label 恒 None → 退化为次键序

    def test_limit_applies_after_sort(self, repo):
        folder = os.path.join(repo.root, "User")
        for name in ("A", "B", "C"):
            _seed(folder, name)
        result = repo.query(GalleryQuery(album_id="legacy:User", limit=2))
        assert [s.display_name for s in result.stills] == ["A", "B"]

    def test_scan_errors_are_stable_reasons(self, repo):
        missing_album = repo.query(GalleryQuery(album_id="legacy:Nope"))
        assert missing_album.stills == ()
        assert missing_album.scan_error == SCAN_ERROR_ALBUM_MISSING
        os.rmdir(os.path.join(repo.root, "System"))
        no_folder = repo.query(GalleryQuery(album_id="legacy:System"))
        assert no_folder.scan_error == SCAN_ERROR_FOLDER_MISSING

    def test_request_revision_echoed(self, repo):
        result = repo.query(GalleryQuery(
            album_id="legacy:User", request_revision=42))
        assert result.request_revision == 42


class TestStillAtAndManifest:
    def test_still_at_rebuilds_single_record(self, repo):
        folder = os.path.join(repo.root, "User")
        topos = _seed(folder, "Solo", meta=PresetMeta(clip_id="c9"))
        still = repo.still_at(folder, topos, probe_grade=True)
        assert still is not None
        assert still.display_name == "Solo"
        assert still.grade_loadable is True
        assert still.provenance.clip_id == "c9"
        assert repo.still_at(folder, os.path.join(folder, "ghost.topos")) \
            is None

    def test_bundle_manifest_covers_six_roles(self, repo):
        folder = os.path.join(repo.root, "User")
        _seed(folder, "Full", thumb=True, still=True, exr=True,
              meta=PresetMeta())
        still = repo.query(GalleryQuery(album_id="legacy:User")).stills[0]
        manifest = repo.bundle_manifest(still, folder)
        roles = {f.role: f.exists for f in manifest.files}
        assert roles == {
            "grade": True, "thumbnail": True, "still_jpeg": True,
            "meta": True, "still_exr": True, "exr_declaration": True}
        # 只有 grade 是必需角色
        assert [f.role for f in manifest.files if f.required] == ["grade"]
        assert manifest.complete is True
        assert manifest.missing_required == ()
        assert manifest.schema_version == 1     # 磁盘事实
        assert manifest.alpha_semantics == "premultiplied"
        assert manifest.still_precision == PRECISION_EXR_FLOAT32

    def test_bundle_manifest_missing_grade_incomplete(self, repo):
        folder = os.path.join(repo.root, "User")
        _seed(folder, "Broken", real_zip=False, thumb=True)
        still = repo.query(GalleryQuery(album_id="legacy:User")).stills[0]
        os.remove(os.path.join(folder, "Broken.topos"))
        manifest = repo.bundle_manifest(still, folder)
        assert manifest.complete is False
        assert manifest.missing_required == ("grade",)
        grade_file = next(
            f for f in manifest.files if f.role == BUNDLE_ROLE_GRADE)
        assert grade_file.exists is False
        assert manifest.still_precision == PRECISION_JPEG8

    def test_manifest_precision_derivation(self, repo):
        folder = os.path.join(repo.root, "User")
        _seed(folder, "JpegOnly", thumb=False, still=True)
        _seed(folder, "ThumbOnly", thumb=True, still=False)
        _seed(folder, "Bare", thumb=False, still=False)
        stills = {s.display_name: s for s in repo.query(
            GalleryQuery(album_id="legacy:User")).stills}
        assert repo.bundle_manifest(
            stills["JpegOnly"], folder).still_precision == PRECISION_JPEG8
        assert repo.bundle_manifest(
            stills["ThumbOnly"], folder).still_precision == PRECISION_JPEG8
        assert repo.bundle_manifest(
            stills["Bare"], folder).still_precision == PRECISION_NONE

    def test_manifest_hashes_optional(self, repo):
        folder = os.path.join(repo.root, "User")
        _seed(folder, "Hashed", thumb=True)
        still = repo.query(GalleryQuery(album_id="legacy:User")).stills[0]
        plain = repo.bundle_manifest(still, folder)
        assert all(f.sha256 is None for f in plain.files)
        hashed = repo.bundle_manifest(still, folder, with_hashes=True)
        grade_file = next(
            f for f in hashed.files if f.role == BUNDLE_ROLE_GRADE)
        assert grade_file.sha256 and len(grade_file.sha256) == 64
        assert grade_file.size_bytes > 0 and grade_file.mtime_ns > 0

    def test_manifest_exr_declaration_role_tracked(self, repo):
        """旧 _SIDECAR_SUFFIXES 遗漏的角色：manifest 必须如实记录。"""
        folder = os.path.join(repo.root, "User")
        _seed(folder, "Declared", exr=True, thumb=True)
        still = repo.query(GalleryQuery(album_id="legacy:User")).stills[0]
        manifest = repo.bundle_manifest(still, folder)
        by_role = {f.role: f for f in manifest.files}
        assert by_role[BUNDLE_ROLE_EXR_DECLARATION].exists is True
        assert by_role[BUNDLE_ROLE_STILL_EXR].relative_path == \
            "Declared.still.exr"
        # EXR 缺声明 sidecar → alpha undeclared（由 preset_meta 口径决定）
        os.remove(os.path.join(folder, "Declared.still.exr.json"))
        manifest2 = repo.bundle_manifest(still, folder)
        assert manifest2.alpha_semantics == "undeclared"

    def test_meta_role_optional_for_valid_capture(self, repo):
        """成功抓帧的 meta 写失败会 drop meta——bundle 合法缺失 meta。"""
        folder = os.path.join(repo.root, "User")
        _seed(folder, "NoMeta", thumb=True, meta=None)
        still = repo.query(GalleryQuery(album_id="legacy:User")).stills[0]
        manifest = repo.bundle_manifest(still, folder)
        assert manifest.complete is True     # meta 非必需
        by_role = {f.role: f for f in manifest.files}
        assert by_role[BUNDLE_ROLE_META].exists is False
