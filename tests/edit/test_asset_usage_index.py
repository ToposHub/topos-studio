"""Asset Usage Index（P1-13 地基）测试。"""

from __future__ import annotations

import json

from src.features.edit.core.asset_usage_index import build_asset_usage_index
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)


def _clip(clip_id: str, asset_id: str, track_id: str, start: int, duration: int):
    return ClipInstance(
        clip_id=clip_id, asset_id=asset_id, track_id=track_id,
        timeline_start=start, timeline_duration=duration,
        source_start=start, source_duration=duration,
    )


def _sequence(seq_id: str, video_clips=(), audio_clips=()):
    return TimelineSequence(
        timeline_id=seq_id, name=f"Seq-{seq_id}", fps=Rational(24, 1),
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=tuple(video_clips)),),
        audio_tracks=(EditTrack(track_id="a1", name="A1", track_type="audio", index=0, clips=tuple(audio_clips)),),
    )


def _project(*asset_ids, path="/tmp/{aid}.mov"):
    project = EditProject(project_id="p", name="p")
    for aid in asset_ids:
        project.assets[aid] = MediaAsset(
            asset_id=aid, path=path.format(aid=aid), media_type="video",
            display_name=f"{aid}.mov", metadata={},
        )
    return project


class TestIndex:
    def test_locations_across_tracks_and_sequences(self):
        seq = _sequence(
            "s1",
            video_clips=(_clip("c1", "a1", "v1", 0, 48), _clip("c2", "a1", "v1", 48, 24)),
            audio_clips=(_clip("a-c1", "a1", "a1", 0, 72),),
        )
        index = build_asset_usage_index([seq])
        locs = index.locations_for("a1")
        assert len(locs) == 3
        kinds = {l.track_type for l in locs}
        assert kinds == {"video", "audio"}
        assert index.usage_count("a1") == 3
        assert index.is_used("a1")

    def test_unused_assets_reported(self):
        seq = _sequence("s1", video_clips=(_clip("c1", "a1", "v1", 0, 24),))
        index = build_asset_usage_index([seq], _project("a1", "ghost", "unused"))
        assert index.unused_asset_ids == ("ghost", "unused")
        assert index.used_asset_ids() == ("a1",)

    def test_no_project_skips_unused(self):
        index = build_asset_usage_index([])
        assert index.unused_asset_ids == ()
        assert index.to_dict()["used_assets"] == {}

    def test_to_dict_json_serializable(self):
        seq = _sequence("s1", video_clips=(_clip("c1", "a1", "v1", 12, 24),))
        index = build_asset_usage_index([seq], _project("a1"))
        data = index.to_dict()
        json.dumps(data)
        loc = data["used_assets"]["a1"][0]
        assert loc["timeline_start"] == 12
        assert loc["timeline_end"] == 36

    def test_format_location_human_readable(self):
        seq = _sequence("s1", video_clips=(_clip("c1", "a1", "v1", 0, 24),))
        index = build_asset_usage_index([seq])
        text = index.locations_for("a1")[0].format()
        assert "Seq-s1" in text and "V1" in text and "c1" in text


class TestManifestIntegration:
    def test_manifest_contains_asset_usage(self, tmp_path):
        from src.features.edit.core.export_manifest import build_export_manifest

        media = tmp_path / "a1.mov"
        media.write_bytes(b"x" * 32)
        seq = _sequence("s1", video_clips=(_clip("c1", "a1", "v1", 0, 24),))
        project = _project("a1", path=str(media))
        manifest = build_export_manifest(project, seq, output_path=str(media))
        usage = manifest["asset_usage"]
        assert usage["used_assets"]["a1"][0]["clip_id"] == "c1"
        assert usage["unused_asset_ids"] == []
