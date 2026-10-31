"""
P2-17 规范化 SegmentRenderKey 测试。

覆盖：
- 键可哈希化：dict/list 字段归一化后可直接做 dict key / set 成员
- 等价形式（tuple vs list、dict vs 有序对）在 __eq__ / __hash__ / digest
  三者上一致（修复此前 eq 与 digest 的裂缝）
- media_versions 媒体版本维度：文件内容变化/缺失改变 digest
- _comp_frame_cache_key 真实取值：fps/matrix/精度/OETF 版本/渲染器版本
- B 套 composite 命中校验：时间线分辨率/帧率变化后不再报 cached
"""

from __future__ import annotations

import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.shared.export.render_cache import DiskRenderCache, SegmentRenderKey

from src.shared.media.metadata import Rational


def make_key(**overrides: object) -> SegmentRenderKey:
    values: dict[str, object] = {
        "source_fingerprint": "graph-1:sha256:abc",
        "in_frame": 0,
        "out_frame": 1,
        "retime": (),
        "layer_stack": ("graph-1",),
        "effects": (),
        "grade": ("bt709", "bt709", "bt709", "limited"),
        "color_management": ("export_oetf_v1",),
        "output_format": (1920, 1080, 24000, 1001, "u8"),
        "renderer_backend": "composite_graph_cpu",
        "renderer_version": "comp-graph-render-v1",
    }
    values.update(overrides)
    return SegmentRenderKey(**values)  # type: ignore[arg-type]


class TestKeyNormalization:
    """P2-17 键规范化：可哈希 + 三方一致。"""

    def test_key_is_hashable_and_set_member(self) -> None:
        key_a = make_key(grade={"b": 2, "a": 1})
        key_b = make_key(grade={"a": 1, "b": 2})
        # 归一化后两个等价键 hash 一致，可直接进 set / dict
        assert hash(key_a) == hash(key_b)
        assert key_a == key_b
        assert len({key_a, key_b}) == 1
        mapping = {key_a: "hit"}
        assert mapping[key_b] == "hit"

    def test_tuple_and_list_forms_agree_across_eq_hash_digest(self) -> None:
        tuple_key = make_key(layer_stack=("graph-1",))
        list_key = make_key(layer_stack=["graph-1"])
        assert tuple_key == list_key
        assert hash(tuple_key) == hash(list_key)
        assert tuple_key.digest == list_key.digest

    def test_from_mapping_roundtrip_preserves_eq_and_digest(self) -> None:
        original = make_key(effects=("blur",), media_versions=(("/a.mov", 10, 20),))
        restored = SegmentRenderKey.from_mapping(original.to_dict())
        assert restored == original
        assert hash(restored) == hash(original)
        assert restored.digest == original.digest

    def test_media_versions_change_digest(self) -> None:
        base = make_key(media_versions=(("/a.mov", 100, 111),))
        other = make_key(media_versions=(("/a.mov", 200, 111),))
        missing = make_key(media_versions=())
        assert base.digest != other.digest
        assert base.digest != missing.digest
        assert base != other
        assert hash(base) != hash(other)

    def test_disk_cache_separates_media_versions(self, tmp_path) -> None:
        cache = DiskRenderCache(tmp_path / "rc")
        key_v1 = make_key(media_versions=(("/a.mov", 100, 111),))
        key_v2 = make_key(media_versions=(("/a.mov", 100, 222),))
        cache.put(key_v1, b"frame-v1")
        # 同图不同素材版本：不得命中 v1 的帧
        assert cache.get(key_v2) is None
        assert cache.get(key_v1) == (b"frame-v1", {})


class TestCompFrameCacheKey:
    """导出帧缓存键的真实取值（P2-17）。"""

    def _exporter(self, fps: Rational | None = None):
        from src.features.edit.core.timeline_export import TimelineExporter

        exporter = TimelineExporter.__new__(TimelineExporter)
        exporter._sequence = SimpleNamespace(
            width=1920,
            height=1080,
            fps=fps if fps is not None else Rational(24000, 1001),
        )
        exporter._project = None
        exporter._export_color_metadata = SimpleNamespace(
            transfer="smpte2084",
            primaries="bt2020",
            matrix="bt2020nc",
            range="limited",
        )
        exporter._use_high_precision_export = False
        return exporter

    def _graph_with_media(self, path: str) -> dict:
        return {
            "nodes": [
                {
                    "id": "n1",
                    "type": "video_frame",
                    "parameters": {"file_path": path},
                }
            ],
        }

    def test_key_carries_real_output_contract(self) -> None:
        from src.features.edit.core.timeline_export import EXPORT_OETF_VERSION
        from src.features.composite.render.composite_frame_renderer import (
            COMPOSITE_GRAPH_RENDERER_VERSION,
        )

        exporter = self._exporter()
        key = exporter._comp_frame_cache_key(
            "graph-1", {"nodes": []}, SimpleNamespace(source_frame=7),
        )
        assert key.in_frame == 7 and key.out_frame == 8
        assert key.output_format == (1920, 1080, 24000, 1001, "u8")
        assert key.grade == ("smpte2084", "bt2020", "bt2020nc", "limited")
        assert key.color_management == (EXPORT_OETF_VERSION,)
        assert key.renderer_version == COMPOSITE_GRAPH_RENDERER_VERSION
        # 此阶段不应用 clip 级效果/变速（下游合成应用）——诚实空值
        assert key.effects == () and key.retime == ()

    def test_precision_and_fps_change_digest(self) -> None:
        exporter = self._exporter()
        key_u8 = exporter._comp_frame_cache_key(
            "graph-1", {"nodes": []}, SimpleNamespace(source_frame=0),
        )
        exporter._use_high_precision_export = True
        key_u16 = exporter._comp_frame_cache_key(
            "graph-1", {"nodes": []}, SimpleNamespace(source_frame=0),
        )
        assert key_u8.digest != key_u16.digest

        exporter_pal = self._exporter(fps=Rational(25, 1))
        key_pal = exporter_pal._comp_frame_cache_key(
            "graph-1", {"nodes": []}, SimpleNamespace(source_frame=0),
        )
        assert key_u8.output_format[:2] == key_pal.output_format[:2]
        assert key_u8.digest != key_pal.digest

    def test_media_file_replacement_invalidates_key(self, tmp_path) -> None:
        media = tmp_path / "input.mov"
        media.write_bytes(b"v1")
        exporter = self._exporter()
        graph = self._graph_with_media(str(media))

        key_v1 = exporter._comp_frame_cache_key(
            "graph-1", graph, SimpleNamespace(source_frame=0),
        )
        versions_v1 = key_v1.media_versions
        assert versions_v1 and versions_v1[0][1] == len(b"v1")

        media.write_bytes(b"version-two-longer")
        key_v2 = exporter._comp_frame_cache_key(
            "graph-1", graph, SimpleNamespace(source_frame=0),
        )
        assert key_v1.digest != key_v2.digest

        os.remove(str(media))
        key_missing = exporter._comp_frame_cache_key(
            "graph-1", graph, SimpleNamespace(source_frame=0),
        )
        assert key_missing.media_versions[0][1] == -1
        assert key_v2.digest != key_missing.digest


class TestCompositeHitValidation:
    """B 套 composite 命中校验：时间线契约变化后不再报 cached。"""

    def _project_with_manifest(self, **manifest_overrides):
        from src.features.edit.core.project import EditProject, MediaAsset
        from src.features.edit.core.render_cache import (
            RenderCacheManifest, RenderCacheStatus,
        )
        from src.features.edit.core.render_cache_status import (
            STATUS_CACHED, STATUS_UNCACHED, cache_status_for_clip,
        )
        from src.features.edit.core.timeline_v2 import ClipInstance

        project = EditProject.create("hit-validation")
        asset = MediaAsset(
            asset_id="asset-1",
            path="/tmp/source.mp4",
            media_type="video",
            display_name="Source",
            metadata={},
        )
        bins = dict(project.bins)
        bins[project.root_bin_id] = bins[project.root_bin_id].with_asset_added(
            asset.asset_id
        )
        project = replace(
            project, assets={asset.asset_id: asset}, bins=bins,
        )
        clip = ClipInstance(
            clip_id="comp-1",
            asset_id="asset-1",
            track_id="v1",
            timeline_start=0,
            timeline_duration=25,
            source_start=0,
            source_duration=25,
            composite_id="graph-1",
        )
        manifest = RenderCacheManifest(
            cache_id="cache-1",
            timeline_id="tl-1",
            clip_id="comp-1",
            composite_id="graph-1",
            status=RenderCacheStatus.VALID,
            **manifest_overrides,
        )
        project = project.set_render_cache_manifest(manifest)
        return project, clip, STATUS_CACHED, STATUS_UNCACHED, cache_status_for_clip

    def test_matching_timeline_reports_cached(self) -> None:
        project, clip, cached, _uncached, status_for = self._project_with_manifest(
            width=1920, height=1080, timeline_fps="24000/1001",
        )
        timeline = SimpleNamespace(
            timeline_id="tl-1", width=1920, height=1080,
            fps=Rational(24000, 1001),
        )
        project = replace(project, timelines={"tl-1": timeline})
        assert status_for(project, "tl-1", clip) == cached

    def test_timeline_size_change_reports_uncached(self) -> None:
        project, clip, _cached, uncached, status_for = self._project_with_manifest(
            width=1920, height=1080, timeline_fps="24000/1001",
        )
        timeline = SimpleNamespace(
            timeline_id="tl-1", width=3840, height=2160,
            fps=Rational(24000, 1001),
        )
        project = replace(project, timelines={"tl-1": timeline})
        assert status_for(project, "tl-1", clip) == uncached

    def test_timeline_fps_change_reports_uncached(self) -> None:
        project, clip, _cached, uncached, status_for = self._project_with_manifest(
            width=1920, height=1080, timeline_fps="24000/1001",
        )
        timeline = SimpleNamespace(
            timeline_id="tl-1", width=1920, height=1080,
            fps=Rational(25, 1),
        )
        project = replace(project, timelines={"tl-1": timeline})
        assert status_for(project, "tl-1", clip) == uncached

    def test_timeline_without_size_fields_stays_cached(self) -> None:
        """timeline 缺少尺寸字段（旧调用方）时不做校验，保持旧行为。"""
        project, clip, cached, _uncached, status_for = self._project_with_manifest()
        timeline = SimpleNamespace(timeline_id="tl-1")
        project = replace(project, timelines={"tl-1": timeline})
        assert status_for(project, "tl-1", clip) == cached
