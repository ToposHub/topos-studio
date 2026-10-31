#!/usr/bin/env python3
"""Repro: CPU export chain first-frame black (parallel-session finding).

Builds a single-clip timeline from a real test video, exports a few frames
via the product TimelineExporter on the CPU chain, then decodes the output's
first frames and reports mean luma. A healthy export has frame 0 ≈ source
frame 0; the reported bug yields a black (or near-black) frame 0.
"""

from __future__ import annotations

import sys
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEST_ROOT))

import numpy as np  # noqa: E402

from src.features.edit.core.project import EditProject, MediaAsset  # noqa: E402
from src.features.edit.core.timeline_export import (  # noqa: E402
    ExportCodec,
    ExportRangeType,
    ExportSource,
    TimelineExportConfig,
    create_timeline_exporter,
)
from src.features.edit.core.timeline_v2 import (  # noqa: E402
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.shared.media.metadata import Rational  # noqa: E402

VIDEO_DIR = TEST_ROOT / "tests" / "test_videos"
OUT_DIR = TEST_ROOT / "bench_out" / "first_frame_repro"


def _probe_source_fps(source: Path) -> Rational:
    try:
        import av

        with av.open(str(source)) as container:
            rate = container.streams.video[0].average_rate
            if rate is not None:
                return Rational(int(rate.numerator), int(rate.denominator))
    except Exception:
        pass
    return Rational(24, 1)


def build_case(source: Path, frames: int, layers: int = 1):
    fps = _probe_source_fps(source)
    project = EditProject.create(name="first-frame-repro")
    # 用产品导入路径同款探测（含 codec/pixel_format/color 持久化）
    from src.features.edit.core.project import probe_media_metadata
    probed = probe_media_metadata(str(source)) or {}
    tracks = []
    for i in range(layers):
        metadata = {"fps": float(fps), "total_frames": frames}
        metadata.update(probed)
        metadata["total_frames"] = frames
        asset = MediaAsset(
            asset_id=f"asset_repro_{i}",
            path=str(source),
            media_type="video",
            display_name=f"repro_{i}",
            metadata=metadata,
        )
        project = project.with_media_asset_added(asset)
        clip = ClipInstance(
            clip_id=f"clip_repro_{i}",
            asset_id=asset.asset_id,
            track_id=f"v{i + 1}",
            timeline_start=0,
            timeline_duration=frames,
            source_start=0,
            source_duration=frames,
        )
        tracks.append(EditTrack(track_id=f"v{i + 1}", name=f"V{i + 1}",
                                track_type="video", index=i, clips=(clip,)))
    sequence = TimelineSequence(
        timeline_id="tl-repro", name="Repro", fps=fps,
        video_tracks=tuple(tracks), audio_tracks=(),
    )
    return project, sequence


def decode_frames(path: Path, count: int = 3) -> list[np.ndarray] | None:
    import av

    frames = []
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        for frame in container.decode(stream):
            arr = frame.to_ndarray(format="rgb24")
            frames.append(arr)
            if len(frames) >= count:
                break
    return frames or None


def main() -> int:
    source_name = sys.argv[3] if len(sys.argv) > 3 else "2K_h264.mp4"
    source = VIDEO_DIR / source_name
    if not source.exists():
        print(f"SKIP: source missing: {source}")
        return 0

    frames = 12
    layers = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    runs = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    any_black = False
    for run in range(runs):
        project, sequence = build_case(source, frames, layers=layers)
        output = OUT_DIR / "repro_out.mov"
        if output.exists():
            output.unlink()

        config = TimelineExportConfig(
            source=ExportSource.TIMELINE,
            timeline_id=sequence.timeline_id,
            range_type=ExportRangeType.ENTIRE,
            codec=ExportCodec.H264,
            include_audio=False,
            output_path=str(output),
        )
        exporter = create_timeline_exporter(project=project, sequence=sequence)
        outcome = exporter.export_timeline(config)
        if not outcome.success:
            print(f"EXPORT FAILED: {getattr(outcome, 'error_code', '')} {outcome.cause}")
            return 1

        decoded = decode_frames(output, 3)
        if decoded is None:
            print("DECODE FAILED")
            return 1

        luma = [float(arr.mean()) for arr in decoded]
        black = luma[0] < 4.0 and luma[-1] > 4.0
        any_black = any_black or black
        print(f"run {run}: layers={layers} frame luma "
              f"{[round(v, 2) for v in luma]} {'BLACK_FIRST_FRAME' if black else 'ok'}")

    print(f"VERDICT: {'BLACK_FIRST_FRAME' if any_black else 'OK'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
