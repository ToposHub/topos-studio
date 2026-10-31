#!/usr/bin/env python3
"""Play a real test video through the native Topos DeckLink bridge."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import topos_decklink  # noqa: E402


WIDTH = 1920
HEIGHT = 1080
FPS = 25.0


def fps_for_mode(mode: str) -> float:
    normalized = mode.lower()
    if normalized in {"1080p50", "hp50", "720p50"}:
        return 50.0
    if normalized in {"1080p60", "hp60", "720p60", "1080i60", "hi60"}:
        return 60.0
    if normalized in {"1080p5994", "1080p59.94", "720p5994", "720p59.94", "1080i5994", "1080i59.94", "hi59"}:
        return 60000.0 / 1001.0
    return 25.0


def fit_bgr_to_bgra_1080(frame: np.ndarray) -> bytes:
    src_h, src_w = frame.shape[:2]
    scale = min(WIDTH / float(src_w), HEIGHT / float(src_h))
    draw_w = max(1, int(round(src_w * scale)))
    draw_h = max(1, int(round(src_h * scale)))
    interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    fitted = cv2.resize(frame, (draw_w, draw_h), interpolation=interpolation)

    canvas = np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8)
    canvas[:, :, 3] = 255
    y0 = (HEIGHT - draw_h) // 2
    x0 = (WIDTH - draw_w) // 2
    canvas[y0 : y0 + draw_h, x0 : x0 + draw_w, 0:3] = fitted
    return canvas.tobytes()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "video",
        nargs="?",
        default="tests/test_videos/2K_h264.mp4",
        help="Video file to send to DeckLink",
    )
    parser.add_argument("--seconds", type=float, default=45.0)
    parser.add_argument("--device", default="")
    parser.add_argument("--mode", default="1080i50")
    parser.add_argument("--fps", type=float, default=0.0)
    parser.add_argument("--connection", choices=("auto", "hdmi", "sdi"), default="hdmi")
    parser.add_argument("--pixel-format", choices=("bgra", "yuv"), default="yuv")
    args = parser.parse_args()

    video_path = Path(args.video)
    if not video_path.exists():
        print(f"Video does not exist: {video_path}", file=sys.stderr)
        return 2

    devices = topos_decklink.list_devices()
    if not devices:
        print("No DeckLink devices found", file=sys.stderr)
        return 2
    device = args.device or devices[0]

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        print(f"Could not open video: {video_path}", file=sys.stderr)
        return 2

    source_fps = capture.get(cv2.CAP_PROP_FPS) or FPS
    source_w = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    source_h = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    print(f"Using DeckLink device: {device}", flush=True)
    print(f"Source: {video_path} ({source_w}x{source_h} @ {source_fps:.3f}fps)", flush=True)
    output_fps = args.fps if args.fps > 0.0 else fps_for_mode(args.mode)
    print(
        f"Output: 1920x1080 @ {output_fps:g}fps {args.mode} "
        f"{args.pixel_format.upper()} via Native SDK bridge",
        flush=True,
    )

    output = topos_decklink.create_output(
        device=device,
        width=WIDTH,
        height=HEIGHT,
        fps=output_fps,
        mode=args.mode,
        connection="" if args.connection == "auto" else args.connection,
        pixel_format=args.pixel_format,
    )
    ok, first_frame = capture.read()
    if not ok:
        print("Could not decode the first video frame", file=sys.stderr)
        return 1
    output.submit_bgra(fit_bgr_to_bgra_1080(first_frame), WIDTH, HEIGHT, 0)
    output.start()
    frame_index = 1
    deadline = time.monotonic() + max(1.0, args.seconds)
    next_time = time.monotonic()
    try:
        while time.monotonic() < deadline:
            ok, frame = capture.read()
            if not ok:
                capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = capture.read()
                if not ok:
                    print("Video decode stopped", file=sys.stderr)
                    return 1
            output.submit_bgra(fit_bgr_to_bgra_1080(frame), WIDTH, HEIGHT, frame_index)
            frame_index += 1
            if frame_index % max(1, int(round(output_fps))) == 0:
                print(f"submitted {frame_index} frames", flush=True)
            next_time += 1.0 / output_fps
            delay = next_time - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_time = time.monotonic()
        print(output.status(), flush=True)
    finally:
        output.stop()
        capture.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
