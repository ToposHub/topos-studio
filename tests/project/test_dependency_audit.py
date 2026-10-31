"""DESK-02C · 依赖闭包/可携性预检/打包摘要测试。

锚点：缺失和哈希错误能定位到对象；不误报"可完整精剪"（blocked 结论
必须出现在悬空引用/必需素材缺失/哈希不符时；显式离线只算 partial）。
"""

from __future__ import annotations

import json
import os
import zipfile

import pytest

from tests.project.package_golden import save_golden_package

from src.shared.project.dependency_audit import (
    collect_dependencies,
    packaging_summary,
    portability_preflight,
)
from src.shared.project.topos_project_io import ToposProjectIO

pytestmark = pytest.mark.regression


def _build_with_resource(directory, media: bytes, resource_id="res-vid-1"):
    from tests.project.test_resource_resolver import _build_embedded_package
    return _build_embedded_package(
        str(directory), "handoff.topos", media_payload=media,
        resource_id=resource_id)


class TestCollectDependencies:
    def test_full_portable_with_embedded_resource(self, tmp_path) -> None:
        media = b"FULL-PORTABLE" + b"\x11" * 2048
        package = _build_with_resource(tmp_path, media)
        project, full_data = ToposProjectIO.load(package)
        assert project is not None
        # 加载后素材已物化（内嵌资源可携）。黄金包的音频素材是外部
        # 路径形态——full 用例只审计已内嵌的 video 素材闭包。
        report = collect_dependencies(
            assets={aid: payload for aid, payload in
                    full_data["assets"].items()
                    if aid == "golden-asset-video"},
            full_data=full_data, timeline_sequences=[])
        verdict, summary, problems = portability_preflight(report)
        assert verdict == "full", problems
        assert not problems
        embedded = [r for r in report.refs if r.resolution == "embedded"]
        assert any(r.object_id == "golden-asset-video" for r in embedded)
        assert report.embedded_bytes >= len(media)
        s = packaging_summary(report)
        assert s["portability"] == "full"
        assert s["by_kind"]["asset"]["portable"] >= 1

    def test_missing_asset_is_blocked_with_object_id(self, tmp_path) -> None:
        package = save_golden_package(str(tmp_path), "golden.topos")
        _, full_data = ToposProjectIO.load(package)
        # 黄金素材路径不存在且无内嵌 → missing，且定位到 asset_id
        report = collect_dependencies(full_data=full_data)
        verdict, _summary, problems = portability_preflight(report)
        assert verdict == "blocked"
        missing = [p for p in problems if p.resolution == "missing"]
        assert {p.object_id for p in missing} == {
            "golden-asset-video", "golden-asset-audio"}

    def test_offline_flagged_is_partial_not_blocked(self, tmp_path) -> None:
        package = save_golden_package(str(tmp_path), "flagged.topos")
        _, full_data = ToposProjectIO.load(package)
        assets = dict(full_data["assets"])
        for payload in assets.values():
            payload["is_offline"] = True  # 用户知情显式离线
        report = collect_dependencies(
            assets=assets, full_data={"resource_index": None})
        verdict, _summary, problems = portability_preflight(report)
        assert verdict == "partial"
        assert all(p.resolution == "offline_flagged" for p in problems)

    def test_dangling_timeline_reference_located(self, tmp_path) -> None:
        package = save_golden_package(str(tmp_path), "dangling.topos")
        project, full_data = ToposProjectIO.load(package)
        seq = full_data["timeline_sequences"]["golden-timeline-main"]
        seq = json.loads(json.dumps(seq))
        seq["video_tracks"][0]["clips"][0]["asset_id"] = "ghost-asset"
        report = collect_dependencies(
            full_data=full_data, timeline_sequences=[_SeqShim(seq)])
        verdict, _summary, problems = portability_preflight(report)
        assert verdict == "blocked"
        dangling = [p for p in problems if p.resolution == "dangling_ref"]
        assert [p.object_id for p in dangling] == ["ghost-asset"]

    def test_lut_uri_and_missing_lut(self, tmp_path) -> None:
        report = collect_dependencies(color_stacks=[
            ("stack-embedded", "topos-res://res-lut-1"),
            ("stack-missing", "/no/such/lut.cube"),
        ])
        verdict, _s, problems = portability_preflight(report)
        assert verdict == "blocked"
        lut_missing = [p for p in problems if p.object_kind == "lut"]
        assert [p.object_id for p in lut_missing] == ["stack-missing"]
        embedded = [
            r for r in report.refs
            if r.object_kind == "lut" and r.resolution == "embedded"]
        assert embedded[0].object_id == "stack-embedded"


class _SeqShim:
    """dict 形态时间线的鸭子类型适配（video/audio tracks → clips）。"""

    def __init__(self, data: dict) -> None:
        self._data = data

    @property
    def video_tracks(self):
        return [_TrackShim(t) for t in self._data.get("video_tracks", [])]

    @property
    def audio_tracks(self):
        return [_TrackShim(t) for t in self._data.get("audio_tracks", [])]


class _TrackShim:
    def __init__(self, data: dict) -> None:
        self.clips = [_ClipShim(c) for c in data.get("clips", [])]


class _ClipShim:
    def __init__(self, data: dict) -> None:
        self.asset_id = data.get("asset_id", "")
