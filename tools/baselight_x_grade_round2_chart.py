"""Build the X Grade holdout and long-drag TIFFs from cards and read-only ProRes clips."""
from __future__ import annotations

import argparse
from colorsys import hsv_to_rgb
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any

import cv2
import numpy as np

from src.shared.color.color_space_descriptor import descriptor_for_space_preset
from src.shared.color.project_video_signal import convert_video_signal_bgr
from tools.baselight_x_grade_comparison import _ANCHORS
from tools.resolve_primary_calibration import (
    GRAY, GRID, HEIGHT, HOLDOUT, MAX_CODE, TILE, WIDTH, chart_codes,
)


_CLIPS = (
    ("A004_C026_1022E2.mov", "Strong chroma accents and yellow/purple structures"),
    ("B006_C026_1023BW_003.mov", "Face, green background and bright bubbles"),
    ("B023_C009_1031E2.mov", "Saturated red fabric and pale blue sky"),
    ("B031_C031_11061V_001.mov", "White dress, skin, foliage and dark background"),
)
_CLIP_SIZE = (472, 249)
_LONGDRAG_ANCHORS = {
    **_ANCHORS,
    "purple": (0.49, 0.20, 0.63),
    "cyan": (0.16, 0.55, 0.60),
    "yellow": (0.65, 0.56, 0.14),
}
_LONGDRAG_CLIPS_PER_ROW = 6
_LONGDRAG_CLIP_ROWS = 4
_LONGDRAG_CLIP_TOP = 544
_TAGS = {
    "color_range": "tv",
    "color_space": "bt709",
    "color_transfer": "bt709",
    "color_primaries": "bt709",
}


def _run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(command, capture_output=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-3000:])
    return result


def _sha(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _probe(path: Path) -> dict[str, Any]:
    probe = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams",
                  "-show_format", "-of", "json", str(path)])
    streams = json.loads(probe.stdout)["streams"]
    if not streams:
        raise ValueError(f"No video stream in {path}")
    stream: dict[str, Any] = streams[0]
    if any(stream.get(key) != value for key, value in _TAGS.items()):
        raise ValueError(f"{path.name} lacks the BT.709 limited-range tags required for this chart")
    return stream


def _extract(
    source: Path, description: str, *, size: tuple[int, int] = _CLIP_SIZE,
    stream: dict[str, Any] | None = None, frame_index: int | None = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    if stream is None:
        stream = _probe(source)
    fps = Fraction(stream["avg_frame_rate"])
    if frame_index is None:
        frame_index = int(stream["nb_frames"]) // 2
    frame_time = frame_index / fps
    width, height = size
    filters = (f"showinfo,scale={width}:{height}:in_color_matrix=bt709:"
               "in_range=tv:out_range=pc:flags=lanczos+accurate_rnd,format=rgb48le")
    arguments = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "info", "-threads", "2",
                 "-filter_threads", "1", "-copyts", "-ss", f"{float(frame_time):.9f}",
                 "-i", str(source), "-map", "0:v:0", "-frames:v", "1", "-vf", filters,
                 "-f", "rawvideo", "-pix_fmt", "rgb48le", "pipe:1"]
    result = _run(arguments)
    decoded = np.frombuffer(result.stdout, dtype="<u2")
    if decoded.size != width * height * 3:
        raise ValueError(f"Unexpected raw RGB frame size for {source.name}")
    frame_pts = re.search(rb"\bn:\s*0\s+pts:\s*-?\d+\s+pts_time:([^ ]+)", result.stderr)
    if frame_pts is None:
        raise RuntimeError(f"Cannot verify decoded frame timestamp for {source.name}")
    actual_pts = float(frame_pts.group(1))
    source_start = float(stream.get("start_time") or 0.0)
    if abs(actual_pts - source_start - float(frame_time)) > 0.001:
        raise RuntimeError(f"{source.name} returned frame at {actual_pts}, expected {source_start + frame_time}")

    bt709 = descriptor_for_space_preset("rec709-bt709")
    gamma24 = descriptor_for_space_preset("rec709-g24")
    frame = decoded.reshape(height, width, 3)[..., ::-1].copy()
    converted = convert_video_signal_bgr(frame, bt709, gamma24)
    tags = {key: stream[key] for key in (
        "codec_name", "profile", "width", "height", "pix_fmt", "avg_frame_rate", *_TAGS)}
    return converted, {
        "filename": source.name,
        "frame_index": frame_index,
        "frame_time_seconds": float(frame_time),
        "source_pts_seconds": actual_pts,
        "description": description,
        "stream": tags,
        "ffmpeg_arguments": arguments,
    }


def _write_tiff(path: Path, image: np.ndarray) -> None:
    if path.exists():
        raise FileExistsError(path)
    if not cv2.imwrite(str(path), image, [cv2.IMWRITE_TIFF_COMPRESSION, 1]):
        raise OSError(f"Cannot write {path}")


def _write_verified_chart(path: Path, image: np.ndarray) -> None:
    _write_tiff(path, image)
    decoded = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if decoded is None or decoded.dtype != np.uint16 or not np.array_equal(decoded, image):
        raise RuntimeError(f"16-bit RGB TIFF failed lossless readback: {path}")


def _code_bgr(colors: np.ndarray) -> np.ndarray:
    return np.rint(colors[..., ::-1] * MAX_CODE).astype(np.uint16)


def _paint_original(image: np.ndarray, *, tile: int = TILE) -> dict[str, dict[str, Any]]:
    colors = chart_codes()
    columns = WIDTH // tile
    rows = (len(colors) + columns - 1) // columns
    grid = np.zeros((rows * columns, 3), dtype=np.uint16)
    grid[:len(colors)] = colors[:, ::-1]
    image[:rows * tile] = grid.reshape(rows, columns, 3).repeat(tile, axis=0).repeat(tile, axis=1)
    groups = {}
    start = 0
    for group, count in (("rgb_grid", GRID**3), ("rgb_holdout", HOLDOUT), ("gray_ramp", GRAY)):
        row, col = np.divmod(np.arange(start, start + count), columns)
        points = np.stack((col * tile + tile // 2, row * tile + tile // 2), axis=-1).tolist()
        top, bottom = int(row[0]) * tile, (int(row[-1]) + 1) * tile
        groups[group] = {"roi": [0, top, WIDTH, bottom - top], "sample_points": points,
                         "sampling": "patch_centers"}
        start += count
    return groups


def _color_cards() -> dict[str, np.ndarray]:
    hues = np.arange(24) / 24.0
    hue_chroma = np.array([[hsv_to_rgb(hue, sat, 0.80) for hue in hues]
                           for sat in (0.08, 0.23, 0.45, 0.72, 0.95)])
    luminance = np.array((0.0, 0.0005, 0.001, 0.002, 0.0035, 0.005, 0.008, 0.012,
                          0.018, 0.025, 0.035, 0.05, 0.07, 0.09, 0.12, 0.16,
                          0.20, 0.27, 0.35, 0.45, 0.56, 0.70, 0.84, 1.0))
    profiles = np.asarray(((1.0, 1.0, 1.0), *_ANCHORS.values()))
    profiles /= profiles.max(axis=1, keepdims=True)
    luma_gray = profiles[:, None, :] * (luminance[None, :, None] ** (1.0 / 2.4))
    skin_hues = np.linspace(8.0, 42.0, 24) / 360.0
    skin = np.array([[hsv_to_rgb(hue, sat, value) for hue in skin_hues]
                     for sat, value in ((0.35, 0.30), (0.42, 0.44), (0.49, 0.57),
                                        (0.52, 0.72), (0.56, 0.90))])
    return {"hue_saturation": hue_chroma, "luma_gray": luma_gray, "skin_neighborhood": skin}


def _paint_cards(
    image: np.ndarray, *, top: int = 628, tile_height: int = 24,
) -> dict[str, dict[str, Any]]:
    groups = {}
    tile_width = 26
    for index, (name, colors) in enumerate(_color_cards().items()):
        rows, cols, _ = colors.shape
        left = 8 + index * 640
        width, height = cols * tile_width, rows * tile_height
        image[top:top + height, left:left + width] = (
            _code_bgr(colors).repeat(tile_height, axis=0).repeat(tile_width, axis=1))
        groups[name] = {
            "roi": [left, top, width, height],
            "sample_points": [[left + x * tile_width + tile_width // 2,
                               top + y * tile_height + tile_height // 2]
                              for y in range(rows) for x in range(cols)],
            "sampling": "patch_centers",
            "rgb_codes": np.rint(colors.reshape(-1, 3) * MAX_CODE).astype(np.uint16).tolist(),
        }
    return groups


def _paint_anchors(
    image: np.ndarray, colors: dict[str, tuple[float, float, float]], *,
    top: int, row_height: int, row_stride: int,
) -> dict[str, dict[str, Any]]:
    anchors = {}
    width = WIDTH // 4
    for index, (name, rgb) in enumerate(colors.items()):
        row, column = divmod(index, 4)
        left, y = column * width, top + row * row_stride
        image[y:y + row_height, left:left + width] = _code_bgr(np.asarray(rgb))
        anchors[name] = {"rgb": rgb, "roi": [left, y, width, row_height],
                         "sample_xy": [left + width // 2, y + row_height // 2]}
    return anchors


def _image_metadata(path: Path) -> dict[str, Any]:
    return {
        "filename": path.name, "width": WIDTH, "height": HEIGHT, "sha256": _sha(path),
        "color_space": "Rec.709 Gamma 2.4 Full", "bit_depth": 16, "channels": "RGB",
    }


def build_anchor_reference(work: Path) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    path = work / "rec709-g24-anchor-reference.tif"
    manifest_path = work / "anchor-reference.json"
    if path.exists() or manifest_path.exists():
        raise FileExistsError("The reference chart or its manifest already exists")
    image = np.empty((HEIGHT, WIDTH, 3), dtype=np.uint16)
    image[:] = _code_bgr(np.array((0.12, 0.12, 0.12)))
    anchors = _paint_anchors(image, _LONGDRAG_ANCHORS, top=0, row_height=HEIGHT // 2, row_stride=HEIGHT // 2)
    _write_verified_chart(path, image)
    manifest = {
        "schema_version": 1,
        "image": _image_metadata(path),
        "coordinates": "Top-left zero-based. ROI is [x, y, width, height], right/bottom are exclusive.",
        "anchors": anchors,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


def build(source_dir: Path, work: Path) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    path = work / "rec709-g24-expanded.tif"
    manifest_path = work / "manifest.json"
    if path.exists() or manifest_path.exists():
        raise FileExistsError("The expanded chart or its manifest already exists")
    images_dir = work / "frames"
    images_dir.mkdir(exist_ok=True)
    image = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint16)
    groups = _paint_original(image)
    anchors = _paint_anchors(image, _ANCHORS, top=540, row_height=80, row_stride=80)

    groups.update(_paint_cards(image))
    clips = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = [pool.submit(_extract, source_dir / name, description) for name, description in _CLIPS]
        for index, task in enumerate(pending):
            frame, evidence = task.result()
            left, top = 4 + index * 480, 768
            width, height = _CLIP_SIZE
            image[top:top + height, left:left + width] = frame
            frame_path = images_dir / f"{Path(evidence['filename']).stem}-{evidence['frame_index']:04d}.tif"
            _write_tiff(frame_path, frame)
            clips[f"clip_{index + 1}"] = {
                **evidence, "roi": [left, top, width, height],
                "frame_file": str(frame_path.relative_to(work)), "sha256": _sha(frame_path),
            }

    _write_verified_chart(path, image)
    _write_manifest(manifest_path, path, source_dir, anchors, groups, clips)
    return path


def _extract_longdrag_clip(source: Path) -> list[tuple[np.ndarray, dict[str, Any]]]:
    stream = _probe(source)
    display_aspect = Fraction(stream["width"], stream["height"]) * Fraction(
        stream["sample_aspect_ratio"].replace(":", "/")
    )
    box_width = WIDTH // _LONGDRAG_CLIPS_PER_ROW - 8
    box_height = (HEIGHT - _LONGDRAG_CLIP_TOP) // _LONGDRAG_CLIP_ROWS - 8
    if display_aspect >= Fraction(box_width, box_height):
        size = (box_width, round(box_width / display_aspect))
    else:
        size = (round(box_height * display_aspect), box_height)
    frame_count = int(stream["nb_frames"])
    if frame_count < 3:
        raise ValueError(f"Need at least three source frames in {source.name}")
    description = dict(_CLIPS).get(source.name, source.stem)
    return [
        _extract(source, description, size=size, stream=stream, frame_index=frame_count * part // 3)
        for part in (1, 2)
    ]


def build_longdrag(source_dir: Path, work: Path) -> Path:
    source_paths = sorted(path for path in source_dir.iterdir() if path.is_file() and path.suffix.lower() == ".mov")
    max_clips = _LONGDRAG_CLIP_ROWS * _LONGDRAG_CLIPS_PER_ROW // 2
    if not 1 <= len(source_paths) <= max_clips:
        raise ValueError(f"Expected 1 to {max_clips} source MOVs for the two-frame layout: {source_dir}")
    work.mkdir(parents=True, exist_ok=True)
    path = work / "rec709-g24-longdrag.tif"
    manifest_path = work / "manifest.json"
    if path.exists() or manifest_path.exists():
        raise FileExistsError("The long-drag chart or its manifest already exists")
    image = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint16)
    groups = _paint_original(image, tile=4)
    anchors = _paint_anchors(image, _LONGDRAG_ANCHORS, top=348, row_height=40, row_stride=44)
    groups.update(_paint_cards(image, top=440, tile_height=20))

    with ThreadPoolExecutor(max_workers=4) as pool:
        frames_by_shot = list(pool.map(_extract_longdrag_clip, source_paths))
    images_dir = work / "frames"
    images_dir.mkdir(exist_ok=True)
    cell_width = WIDTH // _LONGDRAG_CLIPS_PER_ROW
    cell_height = (HEIGHT - _LONGDRAG_CLIP_TOP) // _LONGDRAG_CLIP_ROWS
    clips = {}
    for shot, frames in enumerate(frames_by_shot):
        for view, (frame, evidence) in enumerate(frames):
            row, col = divmod(shot * 2 + view, _LONGDRAG_CLIPS_PER_ROW)
            height, width = frame.shape[:2]
            left = col * cell_width + (cell_width - width) // 2
            top = _LONGDRAG_CLIP_TOP + row * cell_height + (cell_height - height) // 2
            image[top:top + height, left:left + width] = frame
            frame_path = images_dir / f"{Path(evidence['filename']).stem}-{evidence['frame_index']:04d}.tif"
            _write_tiff(frame_path, frame)
            moment = ("early", "late")[view]
            clips[f"clip_{shot + 1:02d}_{moment}"] = {
                **evidence, "frame_selection": f"{view + 1}/3", "roi": [left, top, width, height],
                "frame_file": str(frame_path.relative_to(work)), "sha256": _sha(frame_path),
            }

    _write_verified_chart(path, image)
    _write_manifest(manifest_path, path, source_dir, anchors, groups, clips)
    return path


def _write_manifest(
    manifest_path: Path, path: Path, source_dir: Path, anchors: dict[str, Any],
    groups: dict[str, Any], clips: dict[str, Any],
) -> None:
    manifest = {
        "schema_version": 1,
        "image": _image_metadata(path),
        "coordinates": "Top-left zero-based. ROI is [x, y, width, height], right/bottom are exclusive.",
        "conversion": {
            "source_dir": str(source_dir),
            "matrix_levels_resize": "FFmpeg Lanczos, BT.709 YCbCr limited range to full RGB48",
            "transfer": "Topos built-in inverse BT.709 OETF, then Rec.709 Gamma 2.4",
            "assumption": "Interpret the BT.709 tags literally. The source may use Rec.709 finishing convention.",
            "sample_overlap": "Original patch rows may bound other patches. Use sample_points for color group metrics.",
        },
        "anchors": anchors,
        "groups": groups,
        "clips": clips,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir", type=Path, help="Directory containing the source MOVs")
    parser.add_argument("output_dir", type=Path, nargs="?", help="Scratch directory for TIFF and manifest")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--anchor-reference", action="store_true", help="Build only the reference, using one output path")
    mode.add_argument("--long-drag", action="store_true", help="Build the two-frame-per-shot long-drag comparison chart")
    args = parser.parse_args()
    if args.anchor_reference:
        if args.output_dir is not None:
            parser.error("--anchor-reference takes only the output directory")
        print(build_anchor_reference(args.source_dir))
        return
    if args.output_dir is None:
        parser.error("Specify the source and output directories")
    build_chart = build_longdrag if args.long_drag else build
    print(build_chart(args.source_dir.resolve(strict=True), args.output_dir))


if __name__ == "__main__":
    main()
