"""DESK-08C · 多轮往返耐久与旧包兼容回归。

锚点（计划 08C）：清缓存重开（D1b 已钉）、10 轮往返不丢数据、
取消/磁盘满不丢数据（D3a 已钉）、旧工程可回归。
"""

from __future__ import annotations

import json
import os
import zipfile

import pytest

from tests.project.package_golden import golden_save_kwargs, save_golden_package

from src.shared.project.resource_resolver import ResourceIndex, make_resource_entry
from src.shared.project.topos_project_io import ToposProjectIO

pytestmark = pytest.mark.regression

MEDIA = b"ROUNDTRIP-MEDIA-" + b"\xab" * (256 * 1024)  # 256 KiB 假媒体


def _build_start_package(directory: str) -> str:
    media_file = os.path.join(directory, "clip.mp4")
    with open(media_file, "wb") as fh:
        fh.write(MEDIA)
    entry = make_resource_entry(
        media_file, resource_id="res-vid-1", kind="video",
        roles=("source_media",), asset_id="golden-asset-video",
        original_filename="clip.mp4")
    kwargs = golden_save_kwargs()
    project = kwargs.pop("project")
    kwargs["full_assets_data"]["golden-asset-video"]["resource_uri"] = entry.uri
    kwargs["full_assets_data"]["golden-asset-video"]["path"] = "/phone/x.mp4"
    kwargs["full_assets_data"]["golden-asset-video"]["future_v9"] = {"keep": 1}
    kwargs["resources_data"] = ResourceIndex(
        entries={entry.resource_id: entry}).to_dict()
    kwargs["resource_files"] = {entry.archive_path: media_file}
    path = os.path.join(directory, "endurance.topos")
    assert ToposProjectIO.save(project, path, **kwargs)
    with zipfile.ZipFile(path) as zf:
        contents = {i.filename: zf.read(i.filename)
                    for i in zf.infolist() if not i.is_dir()}
    contents["future_module/data.json"] = json.dumps({"v": 9}).encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, raw in contents.items():
            zf.writestr(name, raw)
    return path


class TestTenRoundTrips:
    def test_load_edit_save_ten_rounds_no_drift(self, tmp_path) -> None:
        from src.features.edit.core.edit_project_io import EditProjectIO

        path = _build_start_package(str(tmp_path))
        io = EditProjectIO()

        expected_duration = 48
        for round_no in range(10):
            loaded = io.load_project(path)
            assert loaded is not None, f"round {round_no}: load failed"
            project, _ui, sequences = loaded

            # 已知值修改：clip 时长 +1 / 工程名追加轮次
            seq = sequences["golden-timeline-main"]
            clip = seq.video_tracks[0].clips[0]
            assert clip.timeline_duration == expected_duration, (
                f"round {round_no}: duration drifted")
            import dataclasses
            edited_clip = dataclasses.replace(
                clip, timeline_duration=clip.timeline_duration + 1)
            edited_seq = seq.with_clip_updated(edited_clip)
            edited_project = dataclasses.replace(
                project, name=f"Round {round_no}")

            # 生产入口保存（EditProject→ToposProject 转换 + 资源随
            # backing 自动携带 + 素材缓存路径 03C 守卫）
            assert io.save_project(
                path, edited_project,
                timeline_sequences={
                    "golden-timeline-main": edited_seq},
                create_backup=False,
            ), f"round {round_no}: save"

            expected_duration += 1

            # 每轮不变量：内嵌资源字节、未知键、版本不降级、缓存不落盘
            _, full = ToposProjectIO.load(path)
            with open(full["assets"]["golden-asset-video"]["path"], "rb") as fh:
                assert fh.read() == MEDIA, f"round {round_no}: media drift"
            with zipfile.ZipFile(path) as zf:
                pool = json.loads(zf.read("edit/media_pool.json"))
                assert pool["golden-asset-video"]["future_v9"] == {"keep": 1}
                assert json.loads(zf.read("future_module/data.json")) == {"v": 9}
                manifest = json.loads(zf.read("manifest.json"))
            assert ".topos_cache" not in json.dumps(pool)
            assert manifest["schema_version"] == 3
            assert manifest["edit_content_schema_version"] == 4


class TestLegacyPackageReopen:
    def test_v1_content_schema_package_loads(self, tmp_path) -> None:
        """无 edit_content_schema_version 的历史包：legacy 链升级可开。"""
        path = save_golden_package(str(tmp_path), "legacy.topos")
        with zipfile.ZipFile(path) as zf:
            contents = {i.filename: zf.read(i.filename)
                        for i in zf.infolist() if not i.is_dir()}
        manifest = json.loads(contents["manifest.json"])
        manifest.pop("edit_content_schema_version", None)  # v1 历史形态
        contents["manifest.json"] = json.dumps(manifest).encode()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, raw in contents.items():
                zf.writestr(name, raw)

        project, full = ToposProjectIO.load(path)
        assert project is not None
        assert full["version_status"]["edit_content"]["status"] == "legacy"
        assert full["version_status"]["edit_content"]["file_version"] == 1
