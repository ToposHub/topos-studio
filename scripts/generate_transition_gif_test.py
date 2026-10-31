"""Generate a small transition GIF from the bundled preview source scenes."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from src.features.edit.core.transition_mask import compute_transition_mask
from src.shared.export.gif_encoder import GifVideoEncoder
from src.shared.export.video_encoder import FFmpegEncoderConfig
from src.shared.media.frame import DecodedFrame
from src.shared.media.metadata import Rational


WIDTH = 640
HEIGHT = 360
FPS = 15
GIF_COLORS = 64
HOLD_FRAMES = 8
TRANSITION_FRAMES = 15


def _load_scene(path: Path) -> np.ndarray:
    with Image.open(path) as source:
        image = source.convert("RGB").resize(
            (WIDTH, HEIGHT), Image.Resampling.LANCZOS
        )
        return np.asarray(image, dtype=np.uint8)


def _blend_scenes(first: np.ndarray, second: np.ndarray, progress: float) -> np.ndarray:
    mask = compute_transition_mask(
        WIDTH, HEIGHT, progress, "iris_circle"
    )[..., None]
    blended = first.astype(np.float32) * (1.0 - mask)
    blended += second.astype(np.float32) * mask
    return np.clip(blended, 0.0, 255.0).astype(np.uint8)


def _make_frames(first: np.ndarray, second: np.ndarray) -> list[np.ndarray]:
    frames = [first.copy() for _ in range(HOLD_FRAMES)]
    for index in range(TRANSITION_FRAMES):
        progress = index / float(TRANSITION_FRAMES - 1)
        frames.append(_blend_scenes(first, second, progress))
    frames.extend(second.copy() for _ in range(HOLD_FRAMES))
    return frames


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    scenes_root = project_root / "src/features/edit/ui/transition_previews/source_scenes"
    first_path = scenes_root / "scene_d1_cool_portrait.png"
    second_path = scenes_root / "scene_d2_warm_portrait.png"
    output_path = project_root / "artifacts/transition_gif_test_portrait_iris_circle.gif"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    first = _load_scene(first_path)
    second = _load_scene(second_path)
    frames = _make_frames(first, second)
    config = FFmpegEncoderConfig(
        codec="gif",
        width=WIDTH,
        height=HEIGHT,
        fps=Rational(FPS, 1),
        pix_fmt="rgb24",
        gif_color_count=GIF_COLORS,
    )
    encoder = GifVideoEncoder(str(output_path), config)
    encoder.open()
    try:
        for index, data in enumerate(frames):
            encoder.encode_frame(
                DecodedFrame(
                    frame_index=index,
                    pts=index,
                    time_seconds=index / float(FPS),
                    data=data,
                    pixel_format="rgb24",
                    width=WIDTH,
                    height=HEIGHT,
                )
            )
    except Exception:
        encoder.abort()
        raise
    else:
        encoder.close()

    with Image.open(output_path) as animation:
        print(
            f"output={output_path}\n"
            f"size={animation.size[0]}x{animation.size[1]} "
            f"frames={animation.n_frames} duration_ms={animation.info.get('duration')} "
            f"file_bytes={output_path.stat().st_size}"
        )


if __name__ == "__main__":
    main()
