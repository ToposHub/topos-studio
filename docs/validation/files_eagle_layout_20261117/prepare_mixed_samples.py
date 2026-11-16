"""Produce a small, read-only media fixture for four Files layout screenshots.

Only this validation directory receives derived PNGs and JSON. The ordered
manifest interleaves actual portrait/landscape images, stills, and video codecs.
No library, cache, preferences, source media or GPU resource is modified.

Source directories are passed explicitly (--photos/--videos)：私人照片目录
不写入仓库默认值。交错顺序按 照片/静图/视频 轮转取前若干项，保证竖幅、
横幅、PNG 与视频同屏可见，不依赖具体文件名。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
HERE = Path(__file__).resolve().parent


def image_sample(path: Path, output: Path) -> dict[str, Any]:
    from PyQt6.QtCore import QSize, Qt
    from PyQt6.QtGui import QImageReader

    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    size = reader.size()
    if not size.isValid():
        raise ValueError(f'Cannot read image header: {path.name}')
    codec = bytes(reader.format()).decode('ascii', errors='replace')
    reader.setScaledSize(size.scaled(QSize(480, 480), Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        raise ValueError(f'{path.name}: {reader.errorString()}')
    if not image.save(str(output), 'PNG'):
        raise OSError(f'Cannot save derived image: {output}')
    return {'type': 'image', 'width': size.width(), 'height': size.height(),
            'duration': None, 'duration_seconds': None, 'fps': None, 'codec': codec,
            'thumbnail_width': image.width(), 'thumbnail_height': image.height()}


def video_sample(path: Path, output: Path) -> dict[str, Any]:
    import av
    from PIL import Image
    from src.shared.playback.libav_player import _LibavSession
    from src.shared.playback.player_host import is_topos_content
    from src.shared.playback.topos_player import ToposMovieSession

    av.logging.restore_default_callback()
    native = is_topos_content(str(path))
    session = ToposMovieSession(str(path)) if native else _LibavSession(str(path))
    try:
        scale = min(480 / session.width, 480 / session.height)
        width = max(2, int(session.width * scale) // 2 * 2)
        height = max(2, int(session.height * scale) // 2 * 2)
        index = min(100, max(0, session.frame_count // 3))
        rgb = session.frame_rgb(index, size=(width, height))
        image = Image.fromarray(rgb)
        image.save(output, 'PNG')
        return {'type': 'video', 'width': session.width, 'height': session.height,
                'duration': session.duration_sec, 'duration_seconds': session.duration_sec,
                'fps': session.fps, 'codec': 'topos' if native else session.codec_name,
                'pixel_format': session.vfmt if native else session.pix_fmt,
                'poster_frame_index': index,
                'thumbnail_width': image.width, 'thumbnail_height': image.height}
    finally:
        session.close()


def interleave(photos: list[Path], stills: list[Path], videos: list[Path],
               total: int = 21) -> list[Path]:
    """Round-robin 照片/PNG静图/视频，取满 total 项；每类不足时由其余类补齐。"""
    order: list[Path] = []
    pools = [photos, stills, videos]
    indexes = [0, 0, 0]
    while len(order) < total and any(indexes[i] < len(pools[i]) for i in range(3)):
        for i in range(3):
            if indexes[i] < len(pools[i]) and len(order) < total:
                order.append(pools[i][indexes[i]])
                indexes[i] += 1
    return order


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--photos', type=Path, required=True,
                        help='照片目录（含私人内容，不写默认值）')
    parser.add_argument('--videos', type=Path, required=True)
    args = parser.parse_args()
    photos = sorted(path for path in args.photos.iterdir()
                    if path.suffix.lower() in ('.jpg', '.jpeg'))
    stills = sorted(path for path in (args.videos / 'quality_compare').glob('*.png')
                    if path.is_file()) if (args.videos / 'quality_compare').is_dir() else \
        sorted(path for path in args.videos.glob('*.png') if path.is_file())
    videos = sorted(path for path in args.videos.iterdir()
                    if path.suffix.lower() in ('.mov', '.mp4'))
    choices = interleave(photos, stills, videos)[:21]
    missing = [str(path) for path in choices if not path.is_file()]
    if missing:
        raise FileNotFoundError(missing)
    before = {str(path): (path.stat().st_size, path.stat().st_mtime_ns) for path in choices}
    thumbs = HERE / 'mixed_thumbs'
    thumbs.mkdir(exist_ok=True)
    entries: list[dict[str, Any]] = []
    for index, path in enumerate(choices, 1):
        output = thumbs / f'{index:02d}.png'
        metadata = (image_sample(path, output) if path.suffix.lower() in ('.jpg', '.png')
                    else video_sample(path, output))
        stat = path.stat()
        entries.append({'path': str(path), 'name': path.name,
                        'size_bytes': stat.st_size, 'mtime_ts': stat.st_mtime,
                        'thumb': output.relative_to(HERE).as_posix(), **metadata})
        print(f'{index:02d} {path.name}: {metadata["width"]}×{metadata["height"]}', flush=True)
    after = {str(path): (path.stat().st_size, path.stat().st_mtime_ns) for path in choices}
    manifest = {'schema_version': 1, 'date': '2026-11-17',
                'scope': f'{len(choices)} real items, interleaved by '
                         f'{len(photos)} photographs / {len(stills)} PNG stills / {len(videos)} videos (bounded)',
                'source_size_and_mtime_unchanged': before == after,
                'thumbnail_bytes': sum((HERE / entry['thumb']).stat().st_size for entry in entries),
                'entries': entries}
    (HERE / 'mixed_samples.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f'Saved {len(entries)} entries, {manifest["thumbnail_bytes"]} derived bytes', flush=True)


if __name__ == '__main__':
    main()
