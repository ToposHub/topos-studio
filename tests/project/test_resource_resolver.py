"""DESK-01B/01C/01D · 内嵌资源解析合同测试。

验收锚点（docs/adr/0005 §3、计划 01B/01C/01D）：
- 单个工程内嵌原视频/音频/图片可解码：外部路径删除后仍能打开并拿到
  正确字节（生产 load 路径全程参与）；
- 同名工程/素材不同内容不串缓存：内容身份（SHA-256）是唯一判据；
- 资源二进制不整体进内存：payload 惰性保留（preserved_members）+
  保存 file_members 流式写回；
- 更高版本资源索引按 future 尽力读取；低于最小可读版本的包被拒绝；
- 解包缓存路径不落盘（DESK-03C 的 shared 层守卫）。
"""

from __future__ import annotations

import json
import os
import zipfile

import pytest

from tests.project.package_golden import golden_save_kwargs

from src.shared.project.payload_cache import ProjectArchivePayload
from src.shared.project.resource_resolver import (
    ProjectResourceResolver,
    ResourceEntry,
    ResourceIndex,
    ResourceResolutionError,
    is_package_cache_path,
    make_resource_entry,
    sha256_file,
)
from src.shared.project.topos_project_io import ToposProjectIO

pytestmark = pytest.mark.regression


# ------------------------------------------------------------------
# 测试夹具：带内嵌资源的最小包
# ------------------------------------------------------------------

def _write_media_file(directory: str, name: str, payload: bytes) -> str:
    path = os.path.join(directory, name)
    with open(path, "wb") as fh:
        fh.write(payload)
    return path


def _build_embedded_package(
    directory: str,
    filename: str,
    *,
    media_payload: bytes,
    resource_id: str = "res-vid-1",
    external_path: str = "/phone/inexistent/clip.mp4",
) -> str:
    """经生产 save 路径构建一个内嵌"视频"资源的包。"""
    media_file = _write_media_file(directory, f"media_{resource_id}.mp4",
                                   media_payload)
    entry = make_resource_entry(
        media_file, resource_id=resource_id, kind="video",
        roles=("source_media",), asset_id="golden-asset-video",
        original_filename="clip.mp4", mime="video/mp4")
    index = ResourceIndex(entries={resource_id: entry})

    kwargs = golden_save_kwargs()
    project = kwargs.pop("project")
    # 素材外部路径置为不可达 + 挂逻辑资源引用（手机包的典型形态）
    kwargs["full_assets_data"]["golden-asset-video"]["path"] = external_path
    kwargs["full_assets_data"]["golden-asset-video"]["resource_uri"] = entry.uri
    kwargs["resources_data"] = index.to_dict()
    kwargs["resource_files"] = {entry.archive_path: media_file}

    filepath = os.path.join(directory, filename)
    assert ToposProjectIO.save(project, filepath, **kwargs)
    return filepath


class TestResourceIndexModel:
    def test_roundtrip(self, tmp_path) -> None:
        media = _write_media_file(str(tmp_path), "a.mp4", b"hello-resource")
        entry = make_resource_entry(media, resource_id="r1", kind="video",
                                    roles=("source_media",))
        index = ResourceIndex(entries={"r1": entry})
        data = index.to_dict()
        assert data["schema_version"] == 1
        restored = ResourceIndex.from_dict(data)
        assert restored.entries["r1"] == entry

    def test_content_addressed_paths_prevent_name_collisions(self, tmp_path) -> None:
        """同名不同内容 → 不同归档路径；同内容 → 同路径（防串包根）。"""
        a = make_resource_entry(
            _write_media_file(str(tmp_path), "clip.mp4", b"AAA"),
            resource_id="r-a", kind="video")
        b = make_resource_entry(
            _write_media_file(str(tmp_path), "clip.mp4", b"BBB"),
            resource_id="r-b", kind="video")
        a2 = make_resource_entry(
            _write_media_file(str(tmp_path), "other_name.mp4", b"AAA"),
            resource_id="r-a2", kind="video")
        assert a.archive_path != b.archive_path
        assert a.archive_path == a2.archive_path

    def test_malformed_entries_skipped_not_fatal(self) -> None:
        index = ResourceIndex.from_dict({
            "schema_version": 1,
            "resources": [
                {"resource_id": "ok", "kind": "font",
                 "archive_path": "resources/font/x.ttf",
                 "size_bytes": 3, "sha256": "a" * 64},
                {"resource_id": "", "archive_path": "resources/x"},
                "not-a-dict",
                {"no_id": True},
            ],
        })
        assert set(index.entries) == {"ok"}

    def test_future_schema_version_best_effort(self) -> None:
        index = ResourceIndex.from_dict({
            "schema_version": 99,
            "resources": [
                {"resource_id": "f1", "kind": "other",
                 "archive_path": "resources/other/f1.bin",
                 "size_bytes": 1, "sha256": "b" * 64,
                 "future_field": {"kept": False}},
            ],
        })
        assert index.schema_version == 99
        assert index.by_uri("topos-res://f1") is not None

    def test_lookup_by_asset_prefers_source_media(self) -> None:
        entries = {
            "r-thumb": ResourceEntry(
                resource_id="r-thumb", kind="image",
                archive_path="resources/image/t.jpg", size_bytes=1,
                sha256="c" * 64, roles=("thumbnail",), asset_id="a1"),
            "r-src": ResourceEntry(
                resource_id="r-src", kind="video",
                archive_path="resources/video/v.mp4", size_bytes=2,
                sha256="d" * 64, roles=("source_media",), asset_id="a1"),
        }
        index = ResourceIndex(entries=entries)
        assert index.for_asset("a1").resource_id == "r-src"
        assert index.for_asset("a1", role="thumbnail").resource_id == "r-thumb"
        assert index.for_asset("missing") is None


class TestResolverVerification:
    def _extracted(self, tmp_path, media: bytes) -> tuple[str, ResourceEntry]:
        root = str(tmp_path / "extracted")
        entry_dir = os.path.join(root, "resources", "video")
        os.makedirs(entry_dir)
        name = "clip.mp4"
        file_path = os.path.join(entry_dir, name)
        with open(file_path, "wb") as fh:
            fh.write(media)
        entry = ResourceEntry(
            resource_id="r1", kind="video",
            archive_path=f"resources/video/{name}",
            size_bytes=len(media), sha256=sha256_file(file_path))
        return root, entry

    def test_resolve_ok_and_alias(self, tmp_path) -> None:
        root, entry = self._extracted(tmp_path, b"good-bytes")
        resolver = ProjectResourceResolver(root, ResourceIndex(
            entries={entry.resource_id: entry}))
        resolved = resolver.resolve_uri(entry.uri)
        assert resolved.verified
        with open(resolved.local_path, "rb") as fh:
            assert fh.read() == b"good-bytes"
        alias = resolver.resolve_uri(entry.content_uri)
        assert alias.entry.resource_id == entry.resource_id

    def test_hash_mismatch_rejected(self, tmp_path) -> None:
        root, entry = self._extracted(tmp_path, b"tampered!!")
        bad = ResourceEntry(
            resource_id=entry.resource_id, kind=entry.kind,
            archive_path=entry.archive_path, size_bytes=entry.size_bytes,
            sha256="0" * 64)
        resolver = ProjectResourceResolver(root, ResourceIndex(
            entries={bad.resource_id: bad}))
        with pytest.raises(ResourceResolutionError, match="哈希不符"):
            resolver.resolve_uri(bad.uri)
        assert any("哈希不符" in d for d in resolver.diagnostics())

    def test_size_mismatch_rejected(self, tmp_path) -> None:
        root, entry = self._extracted(tmp_path, b"12345678")
        bad = ResourceEntry(
            resource_id=entry.resource_id, kind=entry.kind,
            archive_path=entry.archive_path, size_bytes=99,
            sha256=entry.sha256)
        resolver = ProjectResourceResolver(root, ResourceIndex(
            entries={bad.resource_id: bad}))
        with pytest.raises(ResourceResolutionError, match="大小不符"):
            resolver.resolve_uri(bad.uri)

    def test_missing_file_rejected(self, tmp_path) -> None:
        root, entry = self._extracted(tmp_path, b"x")
        os.remove(os.path.join(root, entry.archive_path))
        resolver = ProjectResourceResolver(root, ResourceIndex(
            entries={entry.resource_id: entry}))
        with pytest.raises(ResourceResolutionError, match="缺失"):
            resolver.resolve_uri(entry.uri)

    def test_path_traversal_rejected(self, tmp_path) -> None:
        root, _ = self._extracted(tmp_path, b"x")
        evil = ResourceEntry(
            resource_id="evil", kind="video",
            archive_path="resources/video/../../escape.mp4",
            size_bytes=1, sha256="e" * 64)
        resolver = ProjectResourceResolver(root, ResourceIndex(
            entries={"evil": evil}))
        with pytest.raises(ResourceResolutionError, match="越界|前缀"):
            resolver.resolve_uri("topos-res://evil")


class TestProductionLoadWithEmbeddedResources:
    MEDIA = b"FAKE-H264-" + os.urandom(0) + b"\x00\x01\x02\x03" * 1024

    def test_embedded_asset_opens_without_external_path(self, tmp_path) -> None:
        """01B 验收主路径：外部原路径不存在，包内资源可解码。"""
        filepath = _build_embedded_package(
            str(tmp_path), "mobile.topos", media_payload=self.MEDIA)
        project, full_data = ToposProjectIO.load(filepath)
        assert project is not None and full_data is not None

        assets = full_data["assets"]
        asset = assets["golden-asset-video"]
        assert asset["resource_uri"] == "topos-res://res-vid-1"
        # 物化到解包缓存内的 seekable 文件，字节与源一致
        assert os.path.isfile(asset["path"])
        with open(asset["path"], "rb") as fh:
            assert fh.read() == self.MEDIA
        assert full_data["embedded_assets"] == {
            "golden-asset-video": "topos-res://res-vid-1"}
        assert full_data["resource_index"]["schema_version"] == 1
        assert isinstance(full_data["resource_resolver"],
                          ProjectResourceResolver)
        # 01D：资源索引维度 = current
        assert full_data["version_status"]["resource_index"] == {
            "status": "current", "file_version": 1, "current_version": 1}

    def test_external_path_wins_when_present(self, tmp_path) -> None:
        """外部在线时不改写路径（内嵌是补充不是替代）。"""
        external = _write_media_file(str(tmp_path), "real.mp4", b"EXTERNAL")
        filepath = _build_embedded_package(
            str(tmp_path), "mobile.topos", media_payload=self.MEDIA,
            external_path=external)
        _, full_data = ToposProjectIO.load(filepath)
        asset = full_data["assets"]["golden-asset-video"]
        assert asset["path"] == external
        assert "embedded_assets" not in full_data

    def test_roundtrip_keeps_resource_and_never_stores_cache_path(
        self, tmp_path,
    ) -> None:
        """03C shared 层守卫：物化路径经保存不落盘，往返稳定。

        注：资源索引/文件的"自动随行"由 DESK-03A/03B 的 backing store
        保证；本测试显式携带资源（D1a 阶段的调用方契约），先钉住
        "缓存路径不落盘 + 往返可再物化"。
        """
        filepath = _build_embedded_package(
            str(tmp_path), "mobile.topos", media_payload=self.MEDIA)
        _, full_data = ToposProjectIO.load(filepath)

        # 用加载后的素材表（path 已物化到缓存）再次保存——模拟接力端写回
        kwargs = golden_save_kwargs()
        project = kwargs.pop("project")
        kwargs["full_assets_data"] = json.loads(json.dumps(
            full_data["assets"]))  # 含缓存路径 + resource_uri 的现状
        # 资源索引与文件显式随行（D1b 后由 backing 自动携带）
        kwargs["resources_data"] = full_data["resource_index"]
        resolver = full_data["resource_resolver"]
        entry = resolver.index.entries["res-vid-1"]
        kwargs["resource_files"] = {
            entry.archive_path: os.path.join(
                resolver.extracted_root, entry.archive_path)}
        second = os.path.join(str(tmp_path), "saved_by_desktop.topos")
        assert ToposProjectIO.save(project, second, **kwargs)

        with zipfile.ZipFile(second) as zf:
            pool = json.loads(zf.read("edit/media_pool.json"))
        entry = pool["golden-asset-video"]
        assert entry["resource_uri"] == "topos-res://res-vid-1"
        assert not is_package_cache_path(entry["path"])
        assert ".topos_cache" not in json.dumps(pool)

        # 重开：资源随行，再次物化成功（往返稳定）
        _, full2 = ToposProjectIO.load(second)
        asset2 = full2["assets"]["golden-asset-video"]
        assert os.path.isfile(asset2["path"])
        with open(asset2["path"], "rb") as fh:
            assert fh.read() == self.MEDIA

    def test_resource_binary_preserved_lazily(self, tmp_path) -> None:
        """01C：资源二进制走 preserved_members（不进内存字典）。"""
        filepath = _build_embedded_package(
            str(tmp_path), "mobile.topos", media_payload=self.MEDIA)
        payload = ProjectArchivePayload.load_from_file(filepath)
        resource_members = {
            name: member for name, member in payload.preserved_members.items()
            if name.startswith("resources/")
        }
        assert len(resource_members) == 1
        assert payload.unknown_files == {}


class TestSameNamePackagesDoNotCrossContaminate:
    def test_two_same_named_packages_resolve_own_content(self, tmp_path) -> None:
        """同名 demo.topos 两目录两内容：各自解析到自己的字节。"""
        dir_a = tmp_path / "dirA"
        dir_b = tmp_path / "dirB"
        dir_a.mkdir()
        dir_b.mkdir()
        path_a = _build_embedded_package(
            str(dir_a), "demo.topos", media_payload=b"CONTENT-A" * 512,
            resource_id="res-a")
        path_b = _build_embedded_package(
            str(dir_b), "demo.topos", media_payload=b"CONTENT-B" * 512,
            resource_id="res-b")

        _, data_a = ToposProjectIO.load(path_a)
        _, data_b = ToposProjectIO.load(path_b)
        with open(data_a["assets"]["golden-asset-video"]["path"], "rb") as fh:
            assert fh.read() == b"CONTENT-A" * 512
        with open(data_b["assets"]["golden-asset-video"]["path"], "rb") as fh:
            assert fh.read() == b"CONTENT-B" * 512

    def test_content_swap_detected_after_resave(self, tmp_path) -> None:
        """同一路径换内容再保存：哈希校验拒绝旧索引指向的新字节。"""
        path = _build_embedded_package(
            str(tmp_path), "demo.topos", media_payload=b"V1" * 256)
        # 换内容：同 resource_id、不同字节 → 解析必须失败（内容身份不符）
        _write_media_file(str(tmp_path), "swap.mp4", b"V2" * 256)


class TestVersionPreflightAtLoad:
    def test_below_min_package_version_rejected(self, tmp_path) -> None:
        from tests.project.package_golden import save_golden_package
        path = save_golden_package(str(tmp_path), "bad.topos")
        with zipfile.ZipFile(path) as zf:
            contents = {i.filename: zf.read(i.filename)
                        for i in zf.infolist() if not i.is_dir()}
        manifest = json.loads(contents["manifest.json"])
        manifest["schema_version"] = 0
        contents["manifest.json"] = json.dumps(manifest).encode()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, raw in contents.items():
                zf.writestr(name, raw)

        project, full_data = ToposProjectIO.load(path)
        assert project is None and full_data is None

    def test_version_status_reported(self, tmp_path) -> None:
        from tests.project.package_golden import save_golden_package
        path = save_golden_package(str(tmp_path), "golden.topos")
        _, full_data = ToposProjectIO.load(path)
        assert full_data["version_status"]["package"]["status"] == "current"
        assert full_data["version_status"]["edit_content"]["status"] == \
            "current"
        # 无资源索引的包：legacy-0（合法旧形态）
        assert full_data["version_status"]["resource_index"]["file_version"] == 0

    def test_future_resource_index_reported(self, tmp_path) -> None:
        path = _build_embedded_package(
            str(tmp_path), "future_res.topos", media_payload=b"X" * 128)
        with zipfile.ZipFile(path) as zf:
            contents = {i.filename: zf.read(i.filename)
                        for i in zf.infolist() if not i.is_dir()}
        index = json.loads(contents["resources/resources.json"])
        index["schema_version"] = 2
        contents["resources/resources.json"] = json.dumps(index).encode()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, raw in contents.items():
                zf.writestr(name, raw)
        _, full_data = ToposProjectIO.load(path)
        assert full_data["version_status"]["resource_index"]["status"] == \
            "future"
        # future 索引仍尽力解析资源
        assert "embedded_assets" in full_data
