from __future__ import annotations

from dataclasses import asdict
import json
import logging
from pathlib import Path
import sys
from time import perf_counter

from PyQt6.QtCore import QEventLoop, QThreadPool, QTimer
from PyQt6.QtWidgets import QApplication

OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(OUT.parents[1]))

from matrix import FPS, WIDTH, HEIGHT, cases
from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_export import ExportCodec, TimelineExportConfig, create_timeline_exporter
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.media_pool_panel import MediaPoolPanel
from src.shared.codec.topos_image_binding import get_image_codec
from src.shared.media.factory import MediaSourceFactory


def emit(**data: object) -> None:
    print(json.dumps(data), flush=True)


def import_media(pool: MediaPoolPanel, paths: list[Path]) -> EditProject:
    loop = QEventLoop()
    result = []
    messages = []
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)

    def completed(project: EditProject) -> None:
        result.append(project)
        loop.quit()

    pool.project_changed.connect(completed)
    pool.media_import_status.connect(messages.append)
    start = perf_counter()
    pool._import_files_async([str(p) for p in paths], files_only=True)
    if not result:
        timer.start(20000)
        loop.exec()
    timer.stop()
    elapsed = perf_counter() - start
    pool.project_changed.disconnect(completed)
    pool.media_import_status.disconnect(messages.append)
    assert result, messages
    emit(event="pool-import", files=len(paths), time_ms=elapsed * 1000, paths=[
        {"type": a.media_type, "path": a.path,
         "frames": a.metadata.get("sequence", {}).get("frame_count"),
         "pixel_format": a.metadata.get("pixel_format")} for a in result[0].assets.values()
    ])
    return result[0]


def run_delivery(project: EditProject, path: Path, name: str, count: int,
                 spec: ExportCodec, profile: str | None, fmt: str = "",
                 qp: int | None = None) -> None:
    asset = next(a for a in project.assets.values() if Path(a.path) == path)
    clip = ClipInstance(name + "-clip", asset.asset_id, "v0", 0, count, 0, count)
    sequence = TimelineSequence(
        timeline_id=name, name=name, width=WIDTH, height=HEIGHT, fps=FPS,
        video_tracks=(EditTrack("v0", "V1", "video", 0, clips=(clip,)),),
    )
    folder = OUT / "timeline"
    folder.mkdir(exist_ok=True)
    target = folder / (name if spec == ExportCodec.TOPOS_IMAGE_SEQUENCE else name + ".mov")
    config = TimelineExportConfig(
        codec=spec, codec_profile=profile, timeline_id=name, include_audio=False,
        pixel_format=fmt, quality=qp, output_path=str(target), burn_subtitles=False,
    )
    exporter = create_timeline_exporter(project, sequence)
    start = perf_counter()
    result = exporter.export_timeline(config)
    seconds = perf_counter() - start
    verified = None
    if result.success:
        first = target / "frame_0001.toos" if target.is_dir() else target
        if target.is_dir():
            assert len(list(target.glob("*.toos"))) == count
            img = get_image_codec().validate(str(first), deep=True)
            assert (img.visible_width, img.visible_height) == (WIDTH, HEIGHT)
        src = MediaSourceFactory.create(str(first), preserve_planes=True, auto_open=True)
        try:
            meta = src.probe()
            assert meta.total_frames == count
            frame = src.read_frame(count - 1)
            assert frame.width == WIDTH and frame.height == HEIGHT
            verified = {"fmt": frame.pixel_format, "dtype": str(frame.dtype),
                        "planes": len(frame.planes), "count": meta.total_frames}
            if frame.planes:
                verified["distinct_from_first"] = any(
                    (p != q).any() for p, q in zip(frame.planes, src.read_frame(0).planes)
                )
        finally:
            src.close()
    emit(event="timeline-export", name=name, source=asset.media_type, count=count,
         fps=count / seconds if result.success else None, seconds=seconds,
         verified=verified, **asdict(result))


def main() -> None:
    app = QApplication([])
    logging.basicConfig(level=logging.WARNING)
    pool = MediaPoolPanel()
    pool.set_project(EditProject("diagnostic-toos", "Topos sequence performance"), check_offline=False)
    data = OUT / "new-1"
    specs = cases()
    seq_paths = [p for c in specs for p in sorted((data / c.name).glob("*.toos"))]
    assert len(seq_paths) == 12 * len(specs)
    import_media(pool, seq_paths)
    common = data / "422-low.mov"
    raw12 = data / "raw12-4.mov"
    raw16 = data / "raw16-4.mov"
    project = import_media(pool, [common, raw12, raw16])
    images = [a for a in project.assets.values() if a.media_type == "image_sequence"]
    assert len(images) == len(specs), [a.path for a in images]
    entries = [
        (common, "422-sequence", ExportCodec.TOPOS_IMAGE_SEQUENCE, "422-low", "", None),
        (common, "422-video", ExportCodec.TOPOS, None, "yuv422p10le", 63),
        (common, "gbr10-sequence", ExportCodec.TOPOS_IMAGE_SEQUENCE, "444-high", "", None),
        (common, "gbr10-video", ExportCodec.TOPOS_4444, None, "gbrp10le", 63),
        (common, "half-sequence", ExportCodec.TOPOS_IMAGE_SEQUENCE, "float-ultra", "", None),
        (common, "half-video", ExportCodec.TOPOS_4444, None, "gbrph16le", 44),
        (raw12, "raw12-sequence", ExportCodec.TOPOS_IMAGE_SEQUENCE, "raw12-4", "", None),
        (raw12, "raw12-video", ExportCodec.TOPOS_RAW, "raw12-4", "", None),
        (raw16, "raw16-sequence", ExportCodec.TOPOS_IMAGE_SEQUENCE, "raw16-4", "", None),
        (raw16, "raw16-video", ExportCodec.TOPOS_RAW, "raw16-4", "", None),
        (data / "422-low/frame_0001.toos", "422-reimport", ExportCodec.TOPOS_IMAGE_SEQUENCE, "422-low", "", None),
        (data / "float-ultra/frame_0001.toos", "half-reimport", ExportCodec.TOPOS_IMAGE_SEQUENCE, "float-ultra", "", None),
        (data / "raw12-4/frame_0001.toos", "raw12-reimport", ExportCodec.TOPOS_IMAGE_SEQUENCE, "raw12-4", "", None),
    ]
    for path, name, codec, profile, fmt, qp in entries:
        run_delivery(project, path, name, 12, codec, profile, fmt, qp)
    QThreadPool.globalInstance().waitForDone(5000)
    pool.close()
    app.processEvents()


if __name__ == "__main__":
    main()
