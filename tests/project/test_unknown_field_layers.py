"""DESK-04A · 未知字段保留的包级合同测试（生产 load→改已知值→另存）。

验收锚点（计划 04A）："在每层注入未知键，读/改已知值/另存后未知值仍在"。
逐层注入：project.json（顶层+素材 ref）、media_pool 素材、bins、
timelines ref、timeline_sequences 的 sequence/track/clip/transform/crop/
speed。单元级逐类审计见 tools/probe_unknown_field_preservation.py
（14/14 PASS）。
"""

from __future__ import annotations

import dataclasses
import json
import os
import zipfile

import pytest

from tests.project.package_golden import save_golden_package

from src.features.edit.core.edit_project_io import EditProjectIO

pytestmark = pytest.mark.regression

UNKNOWN = {
    ("project.json", ("project_future",)): {"who": "mobile-2077"},
    ("project.json", ("assets", "golden-asset-video", "ref_future")): 11,
    ("edit/media_pool.json",
     ("golden-asset-video", "asset_future")): [1, 2, 3],
    ("edit/bins.json", ("golden-bin-root", "bin_future")): "keep-me",
    ("edit/timelines.json",
     ("golden-timeline-main", "tref_future")): True,
    ("edit/timeline_sequences.json",
     ("golden-timeline-main", "seq_future")): {"k": "v"},
    ("edit/timeline_sequences.json",
     ("golden-timeline-main", "video_tracks", 0, "track_future")): 7,
    ("edit/timeline_sequences.json",
     ("golden-timeline-main", "video_tracks", 0, "clips", 0,
      "clip_future")): {"z": [3]},
    ("edit/timeline_sequences.json",
     ("golden-timeline-main", "video_tracks", 0, "clips", 0,
      "transform", "transform_future")): 1.25,
    ("edit/timeline_sequences.json",
     ("golden-timeline-main", "video_tracks", 0, "clips", 0,
      "crop", "crop_future")): 2.5,
    ("edit/timeline_sequences.json",
     ("golden-timeline-main", "video_tracks", 0, "clips", 0,
      "speed", "speed_future")): 3.75,
}


def _inject(path: str) -> None:
    """按 UNKNOWN 清单向包内各层注入未知键（路径感知改写 JSON）。"""
    with zipfile.ZipFile(path) as zf:
        contents = {i.filename: zf.read(i.filename)
                    for i in zf.infolist() if not i.is_dir()}
    docs = {name: json.loads(raw) for name, raw in contents.items()
            if name.endswith(".json")}
    for (entry, keys), value in UNKNOWN.items():
        node = docs[entry]
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    for name, doc in docs.items():
        contents[name] = json.dumps(doc, indent=2,
                                    ensure_ascii=False).encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, raw in contents.items():
            zf.writestr(name, raw)


def _read_json(path: str, name: str) -> dict:
    with zipfile.ZipFile(path) as zf:
        return json.loads(zf.read(name))


def _get(doc, keys):
    node = doc
    for key in keys:
        node = node[key]
    return node


@pytest.fixture(scope="module")
def roundtrip_package(tmp_path_factory) -> str:
        tmp = tmp_path_factory.mktemp("unknown")
        source = save_golden_package(str(tmp), "injected.topos")
        _inject(source)

        # 生产 load（完整转换链：迁移 → EditProject/TimelineSequence）
        io = EditProjectIO()
        loaded = io.load_project(source)
        assert loaded is not None
        project, _ui_state, sequences = loaded

        # 改已知值：clip 时长 +1（经 with_clip_updated 派生链）
        seq = sequences["golden-timeline-main"]
        track = seq.video_tracks[0]
        clip = track.clips[0]
        edited_clip = dataclasses.replace(
            clip, timeline_duration=clip.timeline_duration + 1)
        edited_seq = seq.with_clip_updated(edited_clip)
        edited_project = dataclasses.replace(project, name="Desktop Edited")

        # 另存新路径（DESK-03B backing 携带 + 04A 序列化链回写）
        target = str(tmp / "desktop_edit.topos")
        assert io.save_project(
            target, edited_project,
            timeline_sequences={
                "golden-timeline-main": edited_seq})
        return target


class TestUnknownFieldsSurviveRoundtrip:
    @pytest.mark.parametrize(("entry", "keys"), list(UNKNOWN.keys()))
    def test_unknown_value_survives(
        self, roundtrip_package: str, entry: str, keys: tuple,
    ) -> None:
        doc = _read_json(roundtrip_package, entry)
        assert _get(doc, keys) == UNKNOWN[(entry, keys)], (
            f"{entry}::{'/'.join(map(str, keys))} 未随往返保留")

    def test_known_value_edit_landed(self, roundtrip_package: str) -> None:
        """同一保存里已知值的修改确实生效（不是原样拷贝在糊弄）。"""
        doc = _read_json(roundtrip_package, "edit/timeline_sequences.json")
        clip = doc["golden-timeline-main"]["video_tracks"][0]["clips"][0]
        assert clip["timeline_duration"] == 49  # 48 + 1
        project = _read_json(roundtrip_package, "project.json")
        assert project["name"] == "Desktop Edited"
