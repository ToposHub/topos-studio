"""Read-only audit probes, not product acceptance tests.

Run from the repository root with .venv/bin/python <this file>.
Exit zero means the probes ran; issue_present=True records an observed gap.
No project, user setting, GPU context, or media output is written.
"""
from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


def main() -> None:
    import numpy as np
    from src.features.edit.core.project import EditProject, MediaAsset
    from src.features.edit.core.project_snapshots import take_snapshot, restore_snapshot
    from src.features.edit.core.edit_project_io import (
        convert_edit_project_to_topos_project as to_topos,
        convert_topos_project_to_edit_project as to_edit,
    )
    results: list[dict] = []
    project = EditProject(project_id="audit", name="Audit").with_media_asset_added(
        MediaAsset("a", "/missing.mov", "video", "a", {"fps": 25}, rating=5))
    project, _ = take_snapshot(project, "v1")
    restored, _ = restore_snapshot(project, "v1")
    roundtrip = to_edit(to_topos(project))
    results.append({"id": "S01", "issue_present": restored.assets["a"].metadata != {"fps": 25}
                    or restored.assets["a"].rating != 5 or "v1" not in roundtrip.project_snapshots,
                    "restored_metadata": restored.assets["a"].metadata,
                    "restored_rating": restored.assets["a"].rating,
                    "roundtrip_snapshot_names": list(roundtrip.project_snapshots)})

    from src.features.edit.ai_editing.filler_removal import (
        build_filler_removal_plan, FillerDetectionResult, FillerHit)
    from src.features.edit.ai_editing.models import PlanTarget
    from src.features.edit.ai_editing.plan_compiler import compile_plan, CompileError
    # M11 后生产形态：词帧域经宿主时间线映射到真实 track/clip
    from src.features.edit.core.timeline_v2 import (
        ClipInstance, EditTrack, TimelineSequence)
    _clip = ClipInstance(
        clip_id="c1", asset_id="a1", track_id="v1",
        timeline_start=0, timeline_duration=40,
        source_start=0, source_duration=40, speed=1.0)
    _track = EditTrack(track_id="v1", track_type="video", name="V1",
                       clips=(_clip,))
    _timeline = TimelineSequence(
        timeline_id="t", name="T", fps=25,
        video_tracks=(_track,), audio_tracks=(), subtitle_tracks=())
    plan = build_filler_removal_plan(
        FillerDetectionResult((FillerHit("um", 10, 15, .9),)),
        target=PlanTarget(project_id="audit", timeline_id="t"),
        base_revision=0, plan_id="audit-filler", timeline=_timeline)
    try:
        compile_plan(plan)
        error = ""
    except CompileError as exc:
        error = str(exc)
    results.append({"id": "S02", "issue_present": bool(error), "compile_error": error})

    from src.features.edit.ai_editing.editorial_advanced import LinkedVersion, VersionDerivationPlan
    from src.features.deliver.core.version_compiler import compile_version_jobs
    from src.features.deliver.core.video_export_job import VideoExportJob
    derivation = VersionDerivationPlan("m", (LinkedVersion("v", "m", "9:16", 20),))
    compiled = compile_version_jobs(derivation, {"output_path": "/out/m.mp4", "resolution": (3840, 2160)})
    resolution = VideoExportJob.from_flat_config(compiled.jobs[0].config).video_config.resolution
    refused = compile_version_jobs(derivation, {"output_path": "/out/m.mp4"}, all_assets_online=False)
    results.append({"id": "S03", "issue_present": tuple(resolution) != (1080, 1920) or bool(refused.jobs),
                    "actual_resolution": resolution, "expected_resolution": [1080, 1920],
                    "offline_ok": refused.ok, "offline_job_count": len(refused.jobs)})

    from src.shared.media.ffmpeg_source import extract_pts_index_seconds
    from src.features.edit.core.timeline_frame_mapper import FrameMapRequest, map_frame

    class Container:
        streams = NS(video=[NS(time_base=Fraction(1, 25))])
        def __enter__(self) -> Container: return self
        def __exit__(self, *args: object) -> None: pass
        def demux(self, stream: object) -> list[NS]:
            return [NS(pts=p, dts=d) for p, d in [(0, -2), (1, -1), (3, 0), (4, 2)]]

    with patch("av.open", return_value=Container()):
        index = extract_pts_index_seconds("fake.mov")
    request = FrameMapRequest(
        timeline_frame=0, timeline_fps_num=25, timeline_fps_den=1,
        clip_timeline_start=0, clip_timeline_duration=4, source_start_frame=0,
        source_duration_frames=4, source_fps_num=25, source_fps_den=1,
        source_pts_index=tuple(index))
    source_frame = map_frame(request).source_frame
    results.append({"id": "S04", "issue_present": source_frame != 0,
                    "packet_index_seconds": index, "actual_first_source_frame": source_frame,
                    "expected_first_source_frame": 0, "scope": "packet stand-in, not real-media proof"})

    from src.features.edit.core.audio_sync import wav_timecode_seconds
    tc = wav_timecode_seconds(NS(metadata={"timecode": "00:00:01:12", "fps": 24}), "")
    results.append({"id": "S06", "issue_present": tc != 1.5,
                    "actual_seconds_at_24fps": tc, "expected_seconds": 1.5,
                    "scope": "helper fps only; controller offset sign checked statically"})

    from src.features.edit.core.verified_ingest import ingest_files
    error = ""
    with patch("src.features.edit.core.verified_ingest.os.path.isfile", return_value=True), \
         patch("src.features.edit.core.verified_ingest.os.makedirs"), \
         patch("src.features.edit.core.verified_ingest._sha256", side_effect=OSError("source removed")):
        try:
            ingest_files(["/card/a.mov"], "/dest")
        except OSError as exc:
            error = str(exc)
    results.append({"id": "S07", "issue_present": bool(error), "uncaught_source_read_error": error})

    from src.features.edit.core.timeline_export import TimelineExporter
    from src.features.color.core.the_decompiler import GradingMath
    # M05 后生产 schema：链段带 _chain_segment 标签（color_chain.chain_extra_params）
    main_params, pre = {"exposure": 20.0}, {"contrast": 35.0, "_chain_segment": "pre"}
    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._get_clip_color_grade = lambda _: {"grading_params": main_params, "chain_extra_params": [pre]}
    frame = np.array([[[.25, .35, .45]]], dtype=np.float32)
    actual = exporter._apply_color_grade(frame, NS(clip_id="x", source_frame=0))
    rgb = frame[..., ::-1].reshape(-1, 3)
    expected = GradingMath.apply_grading(GradingMath.apply_grading(rgb, pre), main_params).reshape(frame.shape)[..., ::-1]
    difference = float(np.max(np.abs(actual - expected)))
    results.append({"id": "S08", "issue_present": difference > 1e-6,
                    "actual_bgr": actual.tolist(), "expected_bgr": expected.tolist(),
                    "max_abs_error": difference, "scope": "production CPU exporter with injected resolved chain"})

    from src.features.edit.core.color_versions import _plugin_manifest
    manifest = _plugin_manifest(NS(layers=({"plugins": [
        {"plugin_id": "example_plugin", "type": "dctl", "enabled": True}]},)))
    results.append({"id": "S09", "issue_present": not manifest, "actual_plugin_manifest": manifest})

    from src.features.composite.bake_service import _bake_source_signature
    nodes = {i: NS(parameters={"v": i}, enabled=True) for i in ("a", "b", "merge")}
    connections = [NS(src_node_id="a", src_port_name="out", dst_node_id="merge", dst_port_name="A"),
                   NS(src_node_id="b", src_port_name="out", dst_node_id="merge", dst_port_name="B")]
    graph = NS(connections=connections, get_node=nodes.get)
    first = _bake_source_signature(graph, "merge")
    connections[0].dst_port_name, connections[1].dst_port_name = "B", "A"
    unchanged = first == _bake_source_signature(graph, "merge")
    results.append({"id": "S10", "issue_present": unchanged, "signature_unchanged_after_port_swap": unchanged})
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
