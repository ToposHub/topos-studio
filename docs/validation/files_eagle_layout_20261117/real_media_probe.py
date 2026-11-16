"""Read-only, bounded probes of user-selected media through existing decoders.

No GL, audio-device, cache/provider writes or source mutations. Video probes run
in isolated processes with a deadline so native codec failure cannot stop the
whole matrix. These results are decoder checks, not playback/GPU acceptance.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from collections import Counter
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
VIDEO_ROOT = Path('/Volumes/Project_1/开发项目/topos-color-multitask /topos-color01/tests/test_videos')
# 照片目录含私人内容，不写入仓库默认值：必须经 --photo-root 显式传入
OUTPUT = Path(__file__).resolve().with_name('real_media_results.json')
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff', '.heic', '.bmp'}
VIDEO_EXTENSIONS = {'.mov', '.mp4', '.m4v', '.mxf', '.braw', '.r3d'}


def visible_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob('*') if p.is_file()
                  and not any(part.startswith('.') for part in p.relative_to(root).parts))


def probe_image(path: Path) -> dict[str, Any]:
    from PIL import Image
    from src.app.ui.native_files_preview import decode_preview

    started = time.perf_counter()
    result: dict[str, Any] = {'name': path.name, 'directory': str(path.parent)}
    try:
        with Image.open(path) as source:
            original = source.size
            orientation = int(source.getexif().get(274, 1))
        image, error = decode_preview(path.read_bytes())
        if error or image.isNull():
            raise ValueError(error or 'null image')
        expected = original[::-1] if orientation in (5, 6, 7, 8) else original
        actual = (image.width(), image.height())
        aspect_error = abs(actual[0] / actual[1] - expected[0] / expected[1])
        result.update(status='passed' if aspect_error < 0.005 else 'aspect_mismatch',
                      original_size=original, exif_orientation=orientation,
                      expected_oriented_size=expected, decoded_size=actual,
                      aspect_error=aspect_error)
    except (OSError, ValueError, RuntimeError) as error:
        result.update(status='failed', error=f'{type(error).__name__}: {error}')
    result['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 2)
    return result


def probe_video(path: Path) -> dict[str, Any]:
    import av
    import numpy as np
    av.logging.restore_default_callback()
    from src.shared.playback.libav_player import _LibavSession
    from src.shared.playback.player_host import decide_route
    from src.shared.playback.topos_player import ToposMovieSession

    started = time.perf_counter()
    result: dict[str, Any] = {'name': path.name, 'directory': str(path.parent),
                              'bytes': path.stat().st_size, 'frames': []}
    session = None
    try:
        result['playback_route'] = decide_route(str(path))
        if path.suffix.lower() in {'.braw', '.r3d'}:
            result.update(status='not_supported_by_files_player',
                          reason='Files player routes lack a dedicated BRAW/RED SDK backend; this is not a claim about Edit providers.')
            return result
        native = result['playback_route'] == 'topos'
        session = ToposMovieSession(str(path)) if native else _LibavSession(str(path))
        result.update(decoder=type(session).__name__, source_size=[session.width, session.height],
                      fps=session.fps, duration_seconds=session.duration_sec,
                      frame_count=session.frame_count,
                      source_pixel_format=getattr(session, 'pix_fmt', getattr(session, 'vfmt', '')))
        mid = max(0, min(session.frame_count - 1, session.frame_count // 2))
        for index, mode in [(0, 'rgb'), (mid, 'yuv'), (0, 'yuv')]:
            tick = time.perf_counter()
            if mode == 'rgb':
                frame = session.frame_rgb(index, size=(320, 180))
                frame_result = {'shape': list(frame.shape), 'dtype': str(frame.dtype),
                                'valid': frame.ndim == 3 and frame.shape[2] == 3 and bool(np.isfinite(frame).all())}
            else:
                frame = session.frame_yuv(index, size=(320, 180))
                frame_result = {'shape': [frame.height, frame.width], 'dtype': str(frame.y.dtype),
                                'valid': frame.is_valid(), 'subsampling': frame.subsampling,
                                'bit_depth': frame.bit_depth}
            frame_result.update(index=index, mode=mode,
                                elapsed_ms=round((time.perf_counter() - tick) * 1000, 2))
            result['frames'].append(frame_result)
            if not frame_result['valid']:
                raise ValueError(f'invalid {mode} frame at {index}')
        # Non-square viewport checks the same aspect contract as ordinary Files previews.
        rgb = session.frame_rgb(0, size=(320, 240))
        ratio = session.width / session.height
        actual_ratio = rgb.shape[1] / rgb.shape[0]
        result['rgb_non_square_box'] = {
            'box': [320, 240], 'decoded_size': [int(rgb.shape[1]), int(rgb.shape[0])],
            'source_ratio': ratio, 'decoded_ratio': actual_ratio,
            'preserves_aspect': abs(actual_ratio / ratio - 1.0) < 0.03,
        }
        result['status'] = 'passed' if result['rgb_non_square_box']['preserves_aspect'] else 'aspect_mismatch'
    except Exception as error:  # Each native decoder boundary must be reported, not mask the matrix.
        result.update(status='failed', error=f'{type(error).__name__}: {error}')
    finally:
        if session is not None:
            session.close()
        result['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 2)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--video-worker', type=Path)
    parser.add_argument('--photo-root', type=Path,
                        required='--video-worker' not in sys.argv)
    parser.add_argument('--video-root', type=Path, default=VIDEO_ROOT)
    args = parser.parse_args()
    if args.video_worker:
        print('__RESULT__' + json.dumps(probe_video(args.video_worker), ensure_ascii=False))
        return
    photo_root: Path = args.photo_root
    video_root: Path = args.video_root
    started = time.perf_counter()
    files = visible_files(photo_root) + visible_files(video_root)
    before = {str(path): (path.stat().st_size, path.stat().st_mtime_ns) for path in files}
    images = [path for path in files if path.suffix.lower() in IMAGE_EXTENSIONS]
    videos = [path for path in files if path.suffix.lower() in VIDEO_EXTENSIONS]
    result: dict[str, Any] = {
        'date': '2026-11-17', 'scope': 'read-only shared CPU decoder sampling; no GL/audio/full playback acceptance',
        'inventory': {str(root): dict(Counter(p.suffix.lower() for p in visible_files(root)))
                      for root in (photo_root, video_root)},
        'excluded': 'hidden cache folders/files; .topos files in photo folder are project sidecars, not video samples',
        'images': [], 'videos': [],
    }
    for path in images:
        result['images'].append(probe_image(path))
    print(f'Images: {dict(Counter(item["status"] for item in result["images"]))}', flush=True)
    for path in videos:
        try:
            proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--video-worker', str(path)],
                                  cwd=ROOT, env={**os.environ, 'TOPOS_TEST_MODE': '1', 'OPENBLAS_NUM_THREADS': '1'},
                                  capture_output=True, text=True, timeout=40)
            lines = [line for line in proc.stdout.splitlines() if line.startswith('__RESULT__')]
            record = json.loads(lines[-1][len('__RESULT__'):]) if lines else {
                'name': path.name, 'status': 'worker_failed', 'exit_code': proc.returncode,
                'diagnostic': proc.stderr[-2000:]}
        except subprocess.TimeoutExpired:
            record = {'name': path.name, 'status': 'timeout', 'timeout_seconds': 40}
        result['videos'].append(record)
        print(f'{path.name}: {record["status"]}', flush=True)
        OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    after = {str(path): (path.stat().st_size, path.stat().st_mtime_ns) for path in files}
    result['source_size_and_mtime_unchanged'] = before == after
    result['summary'] = {'images': dict(Counter(item['status'] for item in result['images'])),
                         'videos': dict(Counter(item['status'] for item in result['videos'])),
                         'elapsed_seconds': round(time.perf_counter() - started, 3)}
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result['summary'], ensure_ascii=False))


if __name__ == '__main__':
    main()
