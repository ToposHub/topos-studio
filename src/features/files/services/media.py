"""Local read-only thumbnails; video decoding has a process deadline."""
from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from typing import Sequence
import zipfile

from PIL import Image, ImageOps

from src.shared.media.format_registry import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS

MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_IMAGE_PIXELS = 100_000_000
_COMMON_IMAGES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
RAW_PREVIEW_SUFFIXES = frozenset({
    ".arw", ".cr2", ".cr3", ".dng", ".nef", ".orf", ".raf", ".sr2", ".srf", ".rw2", ".pef", ".x3f",
})


def source_dimensions(path: str, kind: str) -> str | None:
    """Read source geometry for gallery captions without decoding a thumbnail."""
    from src.features.files.contracts import format_resolution

    if kind == "image":
        from PyQt6.QtGui import QImageIOHandler, QImageReader

        reader = QImageReader(path)
        size = reader.size()
        if size.isValid():
            if reader.transformation() & QImageIOHandler.Transformation.TransformationRotate90:
                size.transpose()
            return format_resolution(size.width(), size.height())
        if Path(path).suffix.lower() in RAW_PREVIEW_SUFFIXES:
            try:
                import rawpy
            except ImportError:
                return None
            try:
                if Path(path).stat().st_size <= MAX_SOURCE_BYTES:
                    with rawpy.imread(path) as raw:
                        size = raw.sizes
                        width, height = size.width, size.height
                        if size.flip in (5, 6):
                            width, height = height, width
                        return format_resolution(width, height)
            except (OSError, rawpy.LibRawError):
                return None
    elif kind == "video":
        from src.shared.media.factory import MediaSourceFactory

        metadata = MediaSourceFactory.probe_video_metadata(path)
        if metadata is not None:
            return format_resolution(metadata.width, metadata.height)
    return None


def embedded_raw_preview(path: str, *, max_bytes: int) -> bytes:
    """Read the camera's display preview without demosaicing the sensor image."""
    import rawpy

    source = Path(path)
    if source.suffix.lower() not in RAW_PREVIEW_SUFFIXES or source.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("RAW 图片超过浏览预览的读取预算")
    with rawpy.imread(path) as raw:
        preview = raw.extract_thumb()
        if preview.format == rawpy.ThumbFormat.JPEG:
            if len(preview.data) > max_bytes:
                raise ValueError("内嵌预览超过读取预算")
            return bytes(preview.data)
        image = Image.fromarray(preview.data)
        image.thumbnail((1024, 1024))
        return _encode(image)


def _encode(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG")
    data = output.getvalue()
    if len(data) > MAX_OUTPUT_BYTES:
        raise ValueError("缩略图超过字节预算")
    return data


def _image_thumbnail(path: str) -> bytes:
    source = Path(path)
    if not source.is_file() or source.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("图片不存在或超过读取预算")
    if source.suffix.lower() in _COMMON_IMAGES:
        with Image.open(source) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("图片超过像素预算")
            image.draft("RGB", (512, 512))
            preview = ImageOps.exif_transpose(image)
            preview.thumbnail((512, 512))
            return _encode(preview.convert("RGBA" if "A" in preview.getbands() else "RGB"))
    # Professional still formats use the existing shared decoder/provider.
    from src.shared.media.thumbnail_provider import ThumbnailRequest
    import cv2
    bgr = ThumbnailRequest(path, None, 512)._load_fast_thumbnail(path)
    if bgr is None:
        raise ValueError("当前后端不能生成此格式的缩略图")
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    image = Image.fromarray(rgb)
    image.thumbnail((512, 512))
    return _encode(image)


def _video_thumbnail(path: str, strip: bool) -> bytes:
    from src.shared.media.topos_source import is_topos_movie
    from src.shared.playback.topos_player import ToposMovieSession
    from src.shared.playback.libav_player import _LibavSession
    if Path(path).suffix.lower() == ".topos" and zipfile.is_zipfile(path):
        if strip:
            raise ValueError("工程文件没有视频胶片条")
        from src.shared.project.library import COVER_THUMBNAIL_PATH
        with zipfile.ZipFile(path) as project:
            cover = project.getinfo(COVER_THUMBNAIL_PATH)
            if cover.file_size > MAX_OUTPUT_BYTES:
                raise ValueError("工程封面超过读取预算")
            with project.open(cover) as source, Image.open(source) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ValueError("工程封面超过像素预算")
                image.thumbnail((512, 512))
                return _encode(image.convert("RGB"))
    session = ToposMovieSession(path, slice_workers=2) if is_topos_movie(path) else _LibavSession(path)
    try:
        count = max(1, int(session.frame_count))
        samples = 8 if strip else 1
        images: list[Image.Image] = []
        for i in range(samples):
            fraction = (i + 0.5) / samples if strip else 0.1
            index = min(count - 1, int(count * fraction))
            image = Image.fromarray(session.frame_rgb(index, size=(160, 90) if strip else (512, 512)))
            if strip:
                image = ImageOps.pad(image, (160, 90), color="#141414")
            images.append(image)
        if strip:
            canvas = Image.new("RGB", (160 * samples, 90))
            for i, image in enumerate(images):
                canvas.paste(image, (160 * i, 0))
            return _encode(canvas)
        return _encode(images[0])
    finally:
        session.close()


def worker_main(argv: Sequence[str]) -> int:
    """Private source/frozen entry point; outputs only a bounded PNG file."""
    if len(argv) != 3:
        return 2
    path, output, operation = argv
    try:
        data = _video_thumbnail(path, operation == "strip") if operation in {"video", "strip"} else _image_thumbnail(path)
        Path(output).write_bytes(data)
        return 0
    except (OSError, ValueError, RuntimeError, ImportError, KeyError, zipfile.BadZipFile):
        return 1


def thumbnail_bytes(path: str, *, strip: bool = False,
                    cancel: threading.Event | None = None, timeout: float = 12.0) -> bytes:
    """Decode in a killable worker, without a Files server or source-side cache."""
    source = Path(path)
    suffix = source.suffix.lower()
    video = suffix == ".topos" or suffix in VIDEO_EXTENSIONS and suffix not in IMAGE_EXTENSIONS
    if not source.is_file() or (strip and not video) or (not video and suffix not in IMAGE_EXTENSIONS):
        raise ValueError("此文件没有可用的缩略图后端")
    if cancel is not None and cancel.is_set():
        raise ValueError("缩略图请求已取消")
    # Common bounded still images need no process startup; large/native
    # formats and video decode remain isolated from the host's worker pool.
    if suffix in _COMMON_IMAGES:
        return _image_thumbnail(path)
    with tempfile.TemporaryDirectory(prefix="topos-media-") as temporary:
        output = Path(temporary) / "preview.png"
        operation = "strip" if strip else "video" if video else "image"
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--files-media-worker", path, str(output), operation]
        else:
            command = [sys.executable, "-m", __name__, path, str(output), operation]
        environment = os.environ.copy()
        environment["QT_QPA_PLATFORM"] = "offscreen"
        environment.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
        process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[4],
                                   env=environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline:
                    process.kill()
                    raise ValueError("缩略图已取消或解码超时")
                try:
                    process.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode != 0 or not output.is_file():
                raise ValueError("缩略图后端不可用或媒体无法解码")
            if output.stat().st_size > MAX_OUTPUT_BYTES:
                raise ValueError("缩略图超过字节预算")
            return output.read_bytes()
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


if __name__ == "__main__":
    raise SystemExit(worker_main(sys.argv[1:]))
