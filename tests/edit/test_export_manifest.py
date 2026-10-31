"""Export Manifest（P1-18）测试。

验收口径（计划表）：
- 导出报告能回答"使用了什么源、什么版本、哪些地方降级"；
- 同一 revision 可复现（revision + 源媒体 hash 落档）；
- manifest 落盘原子、JSON 可读，且写失败不影响导出结果。
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from src.features.edit.core.capability_registry import build_capability_registry
from src.features.edit.core.delivery_qc import run_delivery_qc
from src.features.edit.core.export_manifest import (
    EXPORT_MANIFEST_SCHEMA,
    build_export_manifest,
    collect_asset_records,
    manifest_path_for,
    write_manifest_file,
)
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)


def _asset(asset_id: str, path: str, fps: float = 24.0) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id,
        path=path,
        media_type="video",
        display_name=f"{asset_id}.mov",
        metadata={"fps": fps},
    )


def _clip(clip_id: str, asset_id: str) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=asset_id,
        track_id="v1",
        timeline_start=0,
        timeline_duration=48,
        source_start=0,
        source_duration=48,
    )


def _sequence(clips: tuple[ClipInstance, ...]) -> TimelineSequence:
    return TimelineSequence(
        timeline_id="seq-manifest",
        name="Manifest",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        revision=7,
        video_tracks=(
            EditTrack(track_id="v1", track_type="video", index=0, clips=clips),
        ),
    )


def _project(*assets: MediaAsset) -> EditProject:
    project = EditProject(project_id="proj-manifest", name="Manifest")
    for asset in assets:
        project.assets[asset.asset_id] = asset
    return project


class TestAssetRecords:
    def test_hash_and_size_recorded(self, tmp_path):
        media = tmp_path / "a.mov"
        payload = b"source-payload" * 100
        media.write_bytes(payload)
        records, warnings = collect_asset_records(
            _sequence((_clip("c1", "asset-1"),)), _project(_asset("asset-1", str(media)))
        )
        assert warnings == ()
        rec = records[0]
        assert rec.status == "ok"
        assert rec.file_size == len(payload)
        assert rec.sha256 == hashlib.sha256(payload).hexdigest()

    def test_missing_file_reported(self, tmp_path):
        records, warnings = collect_asset_records(
            _sequence((_clip("c1", "asset-1"),)),
            _project(_asset("asset-1", str(tmp_path / "ghost.mov"))),
        )
        assert records[0].status == "missing"
        assert records[0].sha256 is None
        assert any("不存在" in w for w in warnings)

    def test_unresolved_asset_reported(self):
        records, warnings = collect_asset_records(
            _sequence((_clip("c1", "ghost"),)), _project()
        )
        assert records[0].status == "unresolved"
        assert any("解析" in w for w in warnings)

    def test_duplicate_asset_ids_deduped(self, tmp_path):
        media = tmp_path / "a.mov"
        media.write_bytes(b"x" * 16)
        first = _clip("c1", "asset-1")
        second = ClipInstance(
            clip_id="c2",
            asset_id="asset-1",
            track_id="v1",
            timeline_start=48,
            timeline_duration=24,
            source_start=48,
            source_duration=24,
        )
        records, _ = collect_asset_records(
            _sequence((first, second)),
            _project(_asset("asset-1", str(media))),
        )
        assert len(records) == 1

    def test_hash_can_be_deferred_for_export_completion(self, tmp_path):
        media = tmp_path / "a.mov"
        media.write_bytes(b"source-payload" * 100)
        records, warnings = collect_asset_records(
            _sequence((_clip("c1", "asset-1"),)),
            _project(_asset("asset-1", str(media))),
            hash_files=False,
        )
        assert records[0].status == "ok"
        assert records[0].file_size == media.stat().st_size
        assert records[0].sha256 is None
        assert any("延后" in warning for warning in warnings)


class TestBuildManifest:
    @pytest.fixture()
    def setup(self, tmp_path):
        media = tmp_path / "src.mov"
        media.write_bytes(b"source" * 50)
        output = tmp_path / "out.mov"
        output.write_bytes(b"delivered" * 10)
        sequence = _sequence((_clip("c1", "asset-1"),))
        project = _project(_asset("asset-1", str(media)))
        return sequence, project, str(output)

    def test_structure_and_revisions(self, setup):
        sequence, project, output = setup
        project = project  # revision defaults 0
        manifest = build_export_manifest(project, sequence, output_path=output)
        assert manifest["schema"] == EXPORT_MANIFEST_SCHEMA
        assert manifest["timeline"]["revision"] == 7
        assert manifest["timeline"]["fps"] == {"num": 24, "den": 1, "float": 24.0}
        assert manifest["timeline"]["duration_frames"] == 48
        assert manifest["project"]["project_id"] == "proj-manifest"
        assert manifest["created_at"]

    def test_source_and_output_hashes(self, setup):
        sequence, project, output = setup
        manifest = build_export_manifest(project, sequence, output_path=output)
        src = manifest["assets"][0]
        assert src["sha256"] == hashlib.sha256(b"source" * 50).hexdigest()
        out = manifest["output"]
        assert out["present"] is True
        assert out["sha256"] == hashlib.sha256(b"delivered" * 10).hexdigest()

    def test_manifest_hash_assets_false_keeps_metadata(self, setup):
        sequence, project, output = setup
        manifest = build_export_manifest(
            project, sequence, output_path=output, hash_assets=False,
        )
        assert manifest["assets"][0]["file_size"] == len(b"source" * 50)
        assert manifest["assets"][0]["sha256"] is None
        assert any("延后" in warning for warning in manifest["asset_warnings"])

    def test_capability_snapshot_attached(self, setup):
        sequence, project, output = setup
        manifest = build_export_manifest(
            project, sequence, capabilities=build_capability_registry(),
            output_path=output,
        )
        caps = manifest["capabilities"]
        assert caps["schema"] == "topos.capability_registry.v1"
        assert any(c["capability_id"] == "edit.interchange.bwf" for c in caps["capabilities"])

    def test_qc_and_preflight_attached(self, setup):
        from types import SimpleNamespace

        sequence, project, output = setup
        qc = run_delivery_qc(sequence, project)
        preflight = SimpleNamespace(
            ok=True, blocking_messages=(), degraded_messages=("EQ 导出走 CPU 后端",)
        )
        manifest = build_export_manifest(
            project, sequence, qc_report=qc, preflight_result=preflight,
            output_path=output,
        )
        assert manifest["delivery_qc"]["passed"] is True
        assert manifest["preflight"]["degraded"] == ["EQ 导出走 CPU 后端"]

    def test_optional_sections_omitted(self, setup):
        sequence, project, output = setup
        manifest = build_export_manifest(project, sequence, output_path=output)
        assert "delivery_qc" not in manifest
        assert "preflight" not in manifest
        assert "loudness" not in manifest

    def test_config_snapshot(self, setup):
        from types import SimpleNamespace

        sequence, project, output = setup
        config = SimpleNamespace(
            codec="prores_422", resolution=(1920, 1080), fps=24.0,
            include_audio=True, burn_subtitles=False,
        )
        manifest = build_export_manifest(
            project, sequence, config=config, output_path=output
        )
        assert manifest["config"]["codec"] == "prores_422"
        assert manifest["config"]["include_audio"] is True

    def test_real_export_config_enum_fields_serializable(self, setup):
        """真实 TimelineExportConfig（含 range_type/codec 枚举）必须可序列化。"""
        from src.features.edit.core.timeline_export import TimelineExportConfig

        sequence, project, output = setup
        config = TimelineExportConfig(
            timeline_id=sequence.timeline_id,
            output_path=output,
            include_audio=False,
        )
        manifest = build_export_manifest(
            project, sequence, config=config, output_path=output
        )
        # 枚举降级为字符串而不是抛 TypeError
        assert isinstance(manifest["config"]["range_type"], str)
        json.dumps(manifest)

    def test_missing_output_recorded(self, setup, tmp_path):
        sequence, project, _ = setup
        manifest = build_export_manifest(
            project, sequence, output_path=str(tmp_path / "nope.mov")
        )
        assert manifest["output"]["present"] is False


class TestWriteManifestFile:
    def test_atomic_write_and_roundtrip(self, tmp_path, setup=None):
        manifest = {"schema": EXPORT_MANIFEST_SCHEMA, "value": 42}
        path = str(tmp_path / "out.mov.manifest.json")
        written = write_manifest_file(path, manifest)
        assert written == path
        with open(path, encoding="utf-8") as fh:
            assert json.load(fh)["value"] == 42
        assert not os.path.exists(path + ".tmp")

    def test_manifest_path_for(self):
        assert manifest_path_for("/x/deliver.mov") == "/x/deliver.mov.manifest.json"

    def test_exporter_success_path_writes_manifest(self, tmp_path):
        """_write_success_manifest：写档 + 访问器；写失败不抛错。"""
        from src.features.edit.core.timeline_export import (
            TimelineExportConfig,
            TimelineExporter,
        )

        media = tmp_path / "src.mov"
        media.write_bytes(b"source")
        out = tmp_path / "out.mov"
        out.write_bytes(b"delivered")
        sequence = _sequence((_clip("c1", "asset-1"),))
        project = _project(_asset("asset-1", str(media)))
        exporter = TimelineExporter(project, sequence, frame_provider=None)
        exporter._last_qc_report = run_delivery_qc(sequence, project)
        config = TimelineExportConfig(
            timeline_id=sequence.timeline_id,
            output_path=str(out),
            include_audio=False,
        )

        exporter._write_success_manifest(config)

        manifest_path = manifest_path_for(str(out))
        assert os.path.exists(manifest_path)
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        assert manifest["schema"] == EXPORT_MANIFEST_SCHEMA
        assert manifest["delivery_qc"]["passed"] is True
        assert exporter.last_export_manifest is not None

        # 写失败（目录不可写等）绝不向调用方抛错
        import unittest.mock as mock

        with mock.patch(
            "src.features.edit.core.export_manifest.write_manifest_file",
            side_effect=OSError("disk full"),
        ):
            exporter._write_success_manifest(config)  # 不抛异常


class TestManifestOutputProbeAndDegradations:
    """T-P1-70 批次二：统一 output probe、GPU trail、降级清单入 manifest。"""

    @pytest.fixture()
    def setup(self, tmp_path):
        media = tmp_path / "src.mov"
        media.write_bytes(b"source" * 50)
        sequence = _sequence((_clip("c1", "asset-1"),))
        project = _project(_asset("asset-1", str(media)))
        return sequence, project

    @staticmethod
    def _real_video(tmp_path, frames=4, with_audio=False):
        """用 PyAV 编码一个真实小视频（h264 yuv420p），供 probe 正向样本。"""
        import numpy as np
        import av

        path = tmp_path / f"probe_{frames}f_{int(with_audio)}aud.mp4"
        with av.open(str(path), "w") as container:
            v = container.add_stream("h264", rate=24)
            v.width, v.height = 64, 48
            v.pix_fmt = "yuv420p"
            a = None
            if with_audio:
                a = container.add_stream("aac", rate=48000)
                a.layout = "stereo"
            for i in range(frames):
                arr = np.full((48, 64, 3), i * 30 % 255, dtype=np.uint8)
                for frame in v.encode(av.VideoFrame.from_ndarray(arr, format="rgb24")):
                    container.mux(frame)
            for stream in (v, a):
                if stream is None:
                    continue
                for packet in stream.encode(None):
                    container.mux(packet)
        return path

    def test_probe_failed_recorded_honestly(self, setup, tmp_path):
        sequence, project = setup
        fake = tmp_path / "fake.mov"
        fake.write_bytes(b"not a media file")
        manifest = build_export_manifest(
            project, sequence, output_path=str(fake), hash_assets=False,
        )
        probe = manifest["output_probe"]
        assert probe["present"] is True
        assert "probe_failed" in probe  # 不伪造字段

    def test_probe_full_media_fields(self, setup, tmp_path):
        sequence, project = setup
        video = self._real_video(tmp_path, frames=4)
        manifest = build_export_manifest(
            project, sequence, output_path=str(video), hash_assets=False,
        )
        probe = manifest["output_probe"]
        assert probe["video"]["codec"] == "h264"
        assert probe["video"]["pix_fmt"] == "yuv420p"
        assert probe["video"]["width"] == 64
        assert probe["frame_count"] == 4
        assert probe["first_frame_decodes"] is True
        assert probe["last_frame_decodes"] is True
        assert probe["audio_streams"] == []

    def test_stage_telemetry_and_effect_skips_enter_manifest(
            self, setup, tmp_path):
        sequence, project = setup
        video = self._real_video(tmp_path, frames=2)
        telemetry = {
            "full_gpu": False,
            "full_gpu_blockers": ["stage_backend_cpu:render:opengl",
                                  "cpu_path_marker:cpu_encoder_surface_path"],
            "fallback_root_cause": "cpu_encoder_surface_path",
        }
        manifest = build_export_manifest(
            project, sequence, output_path=str(video), hash_assets=False,
            stage_telemetry=telemetry,
            effect_export_skips=("plugin:warm_tint",),
        )
        assert manifest["stage_telemetry"] == telemetry
        codes = {d["code"] for d in manifest["degradations"]}
        assert "gpu_blocker" in codes
        assert "fallback_root_cause" in codes
        assert "effect_export_skipped" in codes

    def test_audio_omitted_detected(self, setup, tmp_path):
        """时间线含音频片段但交付无音频流 → audio_omitted 进 manifest。"""
        audio_clip = ClipInstance(
            clip_id="a1", asset_id="asset-1", track_id="a1",
            timeline_start=0, timeline_duration=48,
            source_start=0, source_duration=48,
        )
        audio_track = EditTrack(track_id="a1", track_type="audio",
                                index=0, clips=(audio_clip,))
        base_seq, project = setup
        sequence = base_seq.__class__(
            timeline_id=base_seq.timeline_id, name=base_seq.name,
            fps=base_seq.fps, width=1920, height=1080,
            video_tracks=base_seq.video_tracks,
            audio_tracks=(audio_track,),
        )
        from src.features.edit.core.timeline_export import TimelineExportConfig
        video = self._real_video(tmp_path, frames=2)  # 无音频流
        config = TimelineExportConfig(
            timeline_id=sequence.timeline_id, output_path=str(video),
            include_audio=True,
        )
        manifest = build_export_manifest(
            project, sequence, config=config, output_path=str(video),
            hash_assets=False,
        )
        codes = {d["code"] for d in manifest["degradations"]}
        assert "audio_omitted" in codes

    def test_clean_export_has_no_degradations_key(self, setup, tmp_path):
        """无降级时 degradations 键整体省略（干净交付不制造噪音）。"""
        sequence, project = setup
        video = self._real_video(tmp_path, frames=2)
        manifest = build_export_manifest(
            project, sequence, output_path=str(video), hash_assets=False,
        )
        assert "degradations" not in manifest
        assert "stage_telemetry" not in manifest


class TestManifestAiTraceability:
    """T-P1-70 步骤 4：AI plan/job ID 溯源字段入 manifest config 快照。"""

    def _build(self, tmp_path, **ai_kwargs):
        from src.features.edit.core.export_manifest import build_export_manifest
        from src.features.edit.core.timeline_export import TimelineExportConfig

        media = tmp_path / "src2.mov"
        media.write_bytes(b"source" * 40)
        sequence = _sequence((_clip("c1", "asset-1"),))
        project = _project(_asset("asset-1", str(media)))
        config = TimelineExportConfig(
            output_path=str(tmp_path / "out.mp4"),
            **ai_kwargs,
        )
        manifest = build_export_manifest(
            project, sequence, config, hash_assets=False, probe_media=False)
        return manifest

    def test_ai_ids_enter_config_snapshot(self, tmp_path):
        manifest = self._build(
            tmp_path, ai_plan_id="plan-42", ai_job_id="job-7")
        assert manifest["config"]["ai_plan_id"] == "plan-42"
        assert manifest["config"]["ai_job_id"] == "job-7"

    def test_non_ai_export_omits_ai_keys(self, tmp_path):
        manifest = self._build(tmp_path)
        assert "ai_plan_id" not in manifest["config"]
        assert "ai_job_id" not in manifest["config"]
