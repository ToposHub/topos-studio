"""Generate animated GIF thumbnails for the transition library.

The generated files intentionally use the same numpy transition masks as the
editor preview/export path.  A seeded scene-pair choice makes a batch
reproducible while still allowing the next batch to use different source
scenes.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from src.features.edit.core.transition_catalog import (
    FULL_FRAME_TRANSITION_TYPES,
    TRANSITION_EFFECT_TYPES,
    TRANSITION_DEFINITIONS,
)
from src.features.edit.core.transition_effects import (
    apply_full_frame_transition,
    apply_transition_effect,
)
from src.features.edit.core.transition_mask import compute_transition_mask
from src.shared.export.gif_encoder import GifVideoEncoder
from src.shared.export.video_encoder import FFmpegEncoderConfig
from src.shared.media.frame import DecodedFrame
from src.shared.media.metadata import Rational


# Keep the source GIFs compact while leaving enough detail for the largest
# library card (192x108). The panel scales these 16:9 previews down for the
# small and medium card presets.
WIDTH = 256
HEIGHT = 144
FPS = 12
GIF_COLORS = 64
HOLD_FRAMES = 6
TRANSITION_FRAMES = 18
WIPE_PRESETS = (
    ("wipe_left", "wipe", {"direction": "left"}),
    ("wipe_up", "wipe", {"direction": "up"}),
)


def _load_source_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_scene(path: Path) -> np.ndarray:
    with Image.open(path) as source:
        image = source.convert("RGB").resize(
            (WIDTH, HEIGHT), Image.Resampling.LANCZOS
        )
        return np.asarray(image, dtype=np.uint8)


def _transition_frame(
    first: np.ndarray,
    second: np.ndarray,
    progress: float,
    transition_type: str,
    params: dict[str, Any] | None,
) -> np.ndarray:
    if transition_type in FULL_FRAME_TRANSITION_TYPES:
        return apply_full_frame_transition(
            first,
            second,
            progress,
            transition_type,
            params,
        )
    if transition_type == "cross_dissolve":
        first_weight = 1.0 - progress
        second_weight = progress
        blended = first.astype(np.float32) * first_weight
        blended += second.astype(np.float32) * second_weight
    elif transition_type == "dip_to_color":
        first_weight = max(0.0, 1.0 - progress * 2.0)
        second_weight = max(0.0, (progress - 0.5) * 2.0)
        blended = first.astype(np.float32) * first_weight
        blended += second.astype(np.float32) * second_weight
    else:
        if transition_type in TRANSITION_EFFECT_TYPES:
            first = apply_transition_effect(
                first, progress, transition_type, params
            )
            second = apply_transition_effect(
                second, progress, transition_type, params
            )
        mask = compute_transition_mask(
            WIDTH, HEIGHT, progress, transition_type, params
        )[..., None]
        blended = first.astype(np.float32) * (1.0 - mask)
        blended += second.astype(np.float32) * mask

    if transition_type == "dip_to_color":
        color_factor = (
            progress * 2.0 if progress <= 0.5 else (1.0 - progress) * 2.0
        )
        blended = blended * (1.0 - color_factor)

    return np.clip(blended, 0.0, 255.0).astype(np.uint8)


def _make_frames(
    first: np.ndarray,
    second: np.ndarray,
    transition_type: str,
    params: dict[str, Any] | None,
) -> list[np.ndarray]:
    frames = [first.copy() for _ in range(HOLD_FRAMES)]
    for index in range(TRANSITION_FRAMES):
        progress = index / float(TRANSITION_FRAMES - 1)
        frames.append(
            _transition_frame(first, second, progress, transition_type, params)
        )
    frames.extend(second.copy() for _ in range(HOLD_FRAMES))
    return frames


def _encode_gif(output_path: Path, frames: list[np.ndarray]) -> None:
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


def _preview_specs() -> list[tuple[str, str, dict[str, Any] | None]]:
    specs = [
        (definition.transition_type, definition.transition_type, None)
        for definition in TRANSITION_DEFINITIONS
    ]
    specs.extend(WIPE_PRESETS)
    return specs


def _choose_source_pair(
    pairs: list[dict[str, Any]],
    rng: random.Random,
    transition_type: str,
) -> dict[str, Any]:
    """Choose a representative pair, favouring people for Smooth Cut."""
    if transition_type == "smooth_cut":
        people_pairs = [pair for pair in pairs if pair.get("content") == "people"]
        if people_pairs:
            return rng.choice(people_pairs)
    return rng.choice(pairs)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed", type=int, default=20260915,
        help="seed for selecting source-scene pairs",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    scenes_root = project_root / "src/features/edit/ui/transition_previews/source_scenes"
    output_root = project_root / "src/features/edit/ui/transition_previews/generated"
    output_root.mkdir(parents=True, exist_ok=True)
    source_manifest = _load_source_manifest(scenes_root / "manifest.json")
    pairs = source_manifest["pairs"]
    rng = random.Random(args.seed)
    generated: list[dict[str, Any]] = []

    for output_stem, transition_type, params in _preview_specs():
        pair = _choose_source_pair(pairs, rng, transition_type)
        first = _load_scene(scenes_root / pair["first"])
        second = _load_scene(scenes_root / pair["second"])
        output_path = output_root / f"{output_stem}.gif"
        _encode_gif(
            output_path,
            _make_frames(first, second, transition_type, params),
        )
        generated.append(
            {
                "transition_type": transition_type,
                "output": output_path.name,
                "source_pair_id": pair["id"],
                "first": pair["first"],
                "second": pair["second"],
                "params": params or {},
            }
        )
        print(f"generated {output_path.name} from {pair['id']}")

    manifest = {
        "version": 1,
        "width": WIDTH,
        "height": HEIGHT,
        "fps": FPS,
        "gif_color_count": GIF_COLORS,
        "seed": args.seed,
        "items": generated,
    }
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"manifest {output_root / 'manifest.json'}")


if __name__ == "__main__":
    main()
