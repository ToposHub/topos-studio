#!/usr/bin/env python3
"""Send a short native DeckLink test signal through the Topos bridge."""

from __future__ import annotations

import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import topos_decklink


WIDTH = 1920
HEIGHT = 1080
FPS = 25


def bgra_color_frame(blue: int, green: int, red: int) -> bytes:
    pixel = bytes((blue, green, red, 255))
    return pixel * (WIDTH * HEIGHT)


def main() -> int:
    devices = topos_decklink.list_devices()
    if not devices:
        print("No DeckLink devices found")
        return 2

    device = devices[0]
    print(f"Using DeckLink device: {device}")
    output = topos_decklink.create_output(
        device=device,
        width=WIDTH,
        height=HEIGHT,
        fps=float(FPS),
        mode="1080i50",
        connection="hdmi",
        pixel_format="yuv",
    )
    output.start()
    try:
        colors = [
            bgra_color_frame(0, 0, 255),
            bgra_color_frame(0, 255, 0),
            bgra_color_frame(255, 0, 0),
            bgra_color_frame(255, 255, 255),
        ]
        frame_index = 0
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            output.submit_bgra(colors[(frame_index // FPS) % len(colors)], WIDTH, HEIGHT, frame_index)
            frame_index += 1
            time.sleep(1.0 / FPS)
        print(output.status())
    finally:
        output.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
