"""Load one saved still-image grade without replacing the active document."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import cv2
import numpy as np

from src.features.color.core.async_renderer import RenderCommand
from src.features.color.core.image_engine import ImageEngine
from src.features.color.core.layer import Layer
from src.shared.color.lut_loader import resolve_lut_path
from src.shared.io.file_io import FileIO
from src.shared.project.image_sidecar import resolve_image_sidecar
from src.shared.project.resource_resolver import ProjectResourceResolver
from src.shared.project.topos_project_read_service import ToposProjectReadService


@dataclass
class ImageBatchGrade:
    pixels: np.ndarray
    layers: list[Layer]
    is_srgb: bool
    raw: dict[str, Any]
    raw_baseline: dict[str, Any]
    camera_wb: list[float]
    baked: dict[str, np.ndarray]
    ai_results: dict[tuple[str, str], np.ndarray]


def _lut_file(path: str, resolver: ProjectResourceResolver | None) -> str:
    if path.startswith("topos-res://") and resolver is not None:
        resolved = resolver.resolve_uri(path).local_path
    else:
        resolved = resolve_lut_path(path)
    if not Path(resolved).is_file():
        raise FileNotFoundError(f"LUT resource unavailable: {path}")
    return resolved


def _packaged_cache_path(filename: str, assets_dir: str | None) -> str:
    # Baked resources are basenames stored by the project, not external paths.
    basename = Path(filename).name
    if basename != filename or basename in ("", ".", ".."):
        raise ValueError(f"Invalid grade cache name: {filename}")
    if assets_dir:
        embedded = Path(assets_dir) / basename
        if embedded.is_file():
            return str(embedded)
    return basename


def load_image_batch_grade(path: str) -> ImageBatchGrade:
    sidecar, owned = resolve_image_sidecar(path)
    bundle = ToposProjectReadService().load(sidecar, activate_resources=False) if owned else None
    if owned and bundle is None:
        raise ValueError(f"Cannot load saved grade: {sidecar}")

    layers = (bundle.layers if bundle else None) or [Layer("Layer 1")]
    resolver = (bundle.full_data or {}).get("resource_resolver") if bundle else None
    assets_dir = bundle.assets_dir if bundle else None
    raw = dict(bundle.raw_params or {}) if bundle else {}
    is_raw = Path(path).suffix.lower() in ImageEngine.RAW_EXTENSIONS
    is_srgb = not is_raw and not ImageEngine._path_transfer_is_linear(path)
    raw.setdefault("enabled", False)
    raw.setdefault("color_space", "sRGB")
    raw.setdefault("gamma", "Linear" if is_srgb else "sRGB")
    camera_wb = FileIO.read_camera_white_balance(path) if is_raw else [1.0] * 4
    baseline = {"user_wb": list(camera_wb)}

    bake_name = (bundle.source_bake or {}).get("source_bake_file_name") if bundle else None
    if bake_name:
        baked_source = FileIO.load_baked_layer(_packaged_cache_path(bake_name, assets_dir))
        if baked_source is None:
            raise FileNotFoundError(f"Baked source unavailable: {bake_name}")
        pixels = cv2.cvtColor(baked_source, cv2.COLOR_RGB2BGR)
    elif is_raw and raw["enabled"]:
        pixels, _, _, applied = FileIO.update_raw_decoding(path, raw)
        baseline["user_wb"] = list(applied.get("user_wb", camera_wb))
        raw["user_wb"] = list(baseline["user_wb"])
    else:
        pixels, _, _ = FileIO.load_image(path)
    if pixels is None:
        raise ValueError(f"Cannot decode source image: {path}")
    if bundle and bundle.flip_state:
        horizontal, vertical = (bool(bundle.flip_state.get(key)) for key in ("flip_h", "flip_v"))
        if horizontal or vertical:
            pixels = cv2.flip(pixels, -1 if horizontal and vertical else int(horizontal))

    if raw.get("input_lut"):
        raw["input_lut"] = _lut_file(raw["input_lut"], resolver)
    baked: dict[str, np.ndarray] = {}
    ai_results: dict[tuple[str, str], np.ndarray] = {}
    for layer in layers:
        # Private render IDs keep textures and masks separate from the live document.
        layer.id = str(uuid4())
        if not layer.visible:
            layer.active_lut_path = None
            continue
        if layer.active_lut_path:
            layer.active_lut_path = _lut_file(layer.active_lut_path, resolver)
        if layer.is_baked and layer.baked_file_name:
            cache_path = _packaged_cache_path(layer.baked_file_name, assets_dir)
            texture = FileIO.load_baked_layer(cache_path)
            if texture is None:
                raise FileNotFoundError(f"Baked layer unavailable: {layer.baked_file_name}")
            baked[layer.id] = texture
        for plugin in layer.plugins:
            if not (plugin.get("enabled", True) and plugin.get("type") == "ai"
                    and plugin.get("ai_result_file")):
                continue
            cache_path = _packaged_cache_path(plugin["ai_result_file"], assets_dir)
            result = FileIO.load_ai_plugin_result(cache_path)
            if result is None:
                raise FileNotFoundError(f"AI grade result unavailable: {plugin['ai_result_file']}")
            if result.ndim == 3 and result.shape[2] == 3:
                result = cv2.cvtColor(result, cv2.COLOR_BGR2RGB)
            ai_results[(layer.id, plugin["instance_id"])] = result

    return ImageBatchGrade(pixels, layers, is_srgb, raw, baseline, list(camera_wb), baked, ai_results)


def render_image_batch_grade(engine: ImageEngine, path: str, grade: ImageBatchGrade) -> np.ndarray | None:
    """Render through the existing GL worker and release only temporary resources."""
    renderer = engine.renderer
    temporary = {
        "layers": grade.layers,
        "active_layer_index": 0,
        "img_full_original": grade.pixels,
        "img_source_full": grade.pixels,
        "temp_image": None,
        "current_file_path": path,
        "is_current_image_srgb": grade.is_srgb,
        "current_raw_metadata": {"camera_wb": grade.camera_wb},
        "current_raw_params": grade.raw,
        "_last_applied_raw_params": grade.raw_baseline,
        "show_mask_mode": False,
        "film_grain_params": {"enabled": False},
        "_active_color_graph": None,
    }

    @contextmanager
    def prepare_request() -> Iterator[None]:
        original = {key: getattr(engine, key) for key in temporary}
        try:
            for key, value in temporary.items():
                setattr(engine, key, value)
            engine.sync_all_resources()
            renderer.submit(RenderCommand.UPDATE_AUX_TEX,
                            {"type": "raw_input_lut", "path": grade.raw.get("input_lut"),
                             "ordered": True})
            for layer_id, pixels in grade.baked.items():
                renderer.submit(RenderCommand.UPLOAD_BAKED_TEXTURE,
                                {"layer_id": layer_id, "image_array": pixels})
            for (layer_id, instance_id), pixels in grade.ai_results.items():
                renderer.submit(RenderCommand.UPLOAD_AI_PLUGIN_RESULT, {
                    "layer_id": layer_id, "instance_id": instance_id, "image_array": pixels,
                })
            yield
        finally:
            for key, value in original.items():
                setattr(engine, key, value)
            renderer.submit(RenderCommand.UPDATE_AUX_TEX, {
                "type": "raw_input_lut", "path": engine.current_raw_params.get("input_lut"),
                "ordered": True,
            })

    if renderer is None:
        return None
    try:
        return engine.render_full_resolution(prepare_request=prepare_request())
    finally:
        for layer in grade.layers:
            renderer.submit(RenderCommand.REMOVE_LAYER, layer.id)
