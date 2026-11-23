"""Metal composite of an Edit preview frame (plan task 2.6).

The Metal preview used to take one untouched VideoToolbox clip.  A frame now
qualifies when *every* layer of the task can be reproduced pass by pass on
Metal textures, and nothing else of the task needs a GL-only stage:

* sources: an SDR NV12 VideoToolbox frame, or a plain solid colour;
* per layer: the clip transform (move, scale, rotate, custom pivot), the
  crop matte, the layer opacity and blend mode, and a single serial LUT;
* between layers: the bottom-up blend of ``_composite_tracks`` — which is
  also what a cross dissolve is (two layers with rewritten opacities);
* after the composite: ``dip_to_color`` jobs.

Everything else — clip effects, other grades, adjustment clips, planar or
packed sources, comp handles, spatial transitions — makes the *whole frame*
stay on the GL path; no frame is ever partly Metal.  Subtitles are Qt
overlays and nested sequences never reach a GPU task, so they need nothing
here.

:func:`build_metal_composite_plan` is pure data and owns that decision;
:class:`MetalEditCompositor` encodes the passes.  Both belong to the render
thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import numpy as np

from src.features.edit.core.metal_composite_msl import EDIT_COMPOSITE_MSL


MTL_PIXEL_FORMAT_RGBA8_UNORM = 70


class MetalCompositeError(RuntimeError):
    """The frame could not be encoded; nothing of it was consumed."""



#: More layers than this on one frame stay on the GL path: each Metal layer
#: is a full-frame track texture, and deep stacks are rare in previews.
MAX_COMPOSITE_LAYERS = 8

_LAYER_PARAMS_DTYPE = np.dtype([
    ("row0", "<f4", (4,)),
    ("row1", "<f4", (4,)),
    ("crop_rect", "<f4", (4,)),
    ("crop_feather", "<f4", (4,)),
    ("flags", "<i4", (4,)),
])


@dataclass(frozen=True)
class MetalLayerPlan:
    """One layer of a Metal composite.

    ``transform`` and ``crop`` are ``None`` when they change nothing: the GL
    path then skips the same passes, and so do the kernels.
    """

    kind: str                                  # "nv12" or "solid"
    source: Any                                # the task's VT frame ("nv12")
    solid_color: Tuple[float, float, float]
    transform: Optional[tuple]                 # TransformParams 8-tuple
    crop: Optional[Any]                        # crop_render.CropParams
    opacity: float
    blend_mode: int
    lut: Optional[Tuple[str, float]]           # (reference, strength)


@dataclass(frozen=True)
class MetalCompositePlan:
    layers: Tuple[MetalLayerPlan, ...]         # bottom → top, task order
    dips: Tuple[Tuple[float, Tuple[float, float, float]], ...]   # (factor, rgb)

    @property
    def is_single_untouched_clip(self) -> bool:
        """One clip without transform, crop or partial opacity: the fused
        one-pass kernels of the single-clip preview already cover it."""
        if len(self.layers) != 1 or self.dips:
            return False
        layer = self.layers[0]
        return (layer.kind == "nv12" and layer.transform is None
                and layer.crop is None and layer.opacity == 1.0)


@dataclass(frozen=True)
class PlanRefusal:
    """Why a frame stays on GL; ``reason`` is safe for logs and traces."""

    reason: str


def _refuse(reason: str) -> PlanRefusal:
    return PlanRefusal(reason)


# ---------------------------------------------------------------------------
# 计划（纯数据）
# ---------------------------------------------------------------------------

def nv12_source_eligible(source: Any) -> bool:
    """The source contract of the Metal preview kernels (limited BT.709)."""
    return bool(
        source is not None and getattr(source, "pixel_buffer", None) is not None
        and getattr(source, "pixel_format", None) == "nv12"
        and getattr(source, "bit_depth", None) == 8
        and getattr(source, "color_matrix", None) == "bt709"
        and getattr(source, "color_range", None) == "limited"
        and getattr(source, "color_primaries", None) in (None, "bt709")
        and getattr(source, "color_transfer", None) in (None, "bt709")
        and int(getattr(source, "width", 0) or 0) > 0
        and int(getattr(source, "height", 0) or 0) > 0
    )


def gl_crop_params(crop: Any) -> Optional[Any]:
    """The crop the GL path applies, or ``None`` when it adds no pass.

    Mirrors ``_apply_crop``: the layer carries a 5-tuple, a crop that cannot
    be built or that removes nothing is skipped there — and here.
    """
    if not crop:
        return None
    from src.features.edit.core.clip_params import CropParams
    from src.features.edit.core import crop_render

    try:
        if isinstance(crop, CropParams):
            params = crop
        else:
            params = CropParams(
                left=float(crop[0]), right=float(crop[1]),
                top=float(crop[2]), bottom=float(crop[3]),
                feather=float(crop[4]),
            )
    except (TypeError, ValueError, IndexError):
        return None
    return None if crop_render.crop_is_identity(params) else params


def _transform_plan(transform: Any) -> Union[Optional[tuple], PlanRefusal]:
    """``None`` for an identity transform, the 8-tuple otherwise."""
    from src.features.edit.core.edit_gpu_preview_renderer import (
        _transform_tuple_is_identity,
    )

    if transform is None or _transform_tuple_is_identity(transform):
        # The GL path feeds the identity matrix to a transform that is
        # identity within its tolerance; the kernels then skip the quad test.
        return None
    if not isinstance(transform, (tuple, list)) or len(transform) != 8:
        return _refuse("transform_unsupported")
    if str(transform[5]) not in ("center", "custom"):
        return _refuse(f"transform_pivot_unsupported:{transform[5]}")
    values = tuple(transform)
    try:
        floats = [float(values[i]) for i in (0, 1, 2, 3, 4, 6, 7)]
    except (TypeError, ValueError):
        return _refuse("transform_not_numeric")
    if not all(np.isfinite(floats)):
        return _refuse("transform_not_finite")
    # A quad collapsed by a zero scale has no inverse (see inverse_quad_map).
    if abs(floats[2]) < 1e-9 or abs(floats[3]) < 1e-9:
        return _refuse("transform_degenerate")
    return values


def _lut_plan(info: dict) -> Union[Optional[Tuple[str, float]], PlanRefusal]:
    grade = info.get("preview_grade")
    if not grade:
        return None
    from src.features.color.core.color_graph_texture import serial_lut_only
    from src.shared.project.color_graph import ColorGraphData

    graph = grade.get("color_graph") if isinstance(grade, dict) else None
    if not isinstance(graph, ColorGraphData) or grade.get("chain_extra_params"):
        return _refuse("grade_unsupported")
    lut = serial_lut_only(graph)
    if lut is None:
        return _refuse("grade_unsupported")
    return lut


def _solid_color(info: dict) -> Tuple[float, float, float]:
    """Resolve a solid layer's colour exactly as ``_paint_solid_to_track_fbo``."""
    generator = info.get("generator_params")
    if isinstance(generator, dict):
        return (
            float(generator.get("color_r", 0.0)),
            float(generator.get("color_g", 0.6)),
            float(generator.get("color_b", 0.8)),
        )
    rgb = info.get("solid_color") or (0.0, 0.6, 0.8)
    return (float(rgb[0]), float(rgb[1]), float(rgb[2]))


def _layer_plan(layer: Any) -> Union[MetalLayerPlan, PlanRefusal]:
    if not isinstance(layer, dict):
        return _refuse("layer_not_a_dict")
    info = layer.get("track_info") or {}
    frame_type = layer.get("frame_type")
    if info.get("clip_effects"):
        return _refuse("clip_effects")
    transform = _transform_plan(info.get("transform"))
    if isinstance(transform, PlanRefusal):
        return transform
    crop = gl_crop_params(info.get("crop"))
    lut = _lut_plan(info)
    if isinstance(lut, PlanRefusal):
        return lut
    try:
        opacity = float(info.get("opacity", 1.0))
    except (TypeError, ValueError):
        return _refuse("opacity_not_numeric")
    if not np.isfinite(opacity):
        return _refuse("opacity_not_finite")

    if frame_type == "vt_pixel_buffer":
        source = layer.get("vt_pixel_buffer")
        if not nv12_source_eligible(source):
            return _refuse("source_unsupported")
        kind, color = "nv12", (0.0, 0.0, 0.0)
    elif frame_type == "solid" and info.get("is_solid"):
        generator = info.get("generator_params")
        # A solid is the only generator the GL path paints without a shader.
        if isinstance(generator, dict) and generator.get("mode", "solid") != "solid":
            return _refuse(f"generator_unsupported:{generator.get('mode')}")
        kind, color, source = "solid", _solid_color(info), None
    else:
        return _refuse(f"source_unsupported:{frame_type}")
    from src.features.edit.core.edit_gpu_preview_renderer import _blend_code_of

    try:
        blend_mode = int(_blend_code_of(info))
    except (TypeError, ValueError):
        return _refuse("blend_mode_unsupported")
    return MetalLayerPlan(
        kind=kind, source=source, solid_color=color, transform=transform, crop=crop,
        opacity=opacity, blend_mode=blend_mode, lut=lut,
    )


def _dip_jobs(task: Any) -> Union[Tuple[Tuple[float, Tuple[float, float, float]], ...], PlanRefusal]:
    """``dip_to_color`` jobs as ``(factor, rgb)``; any other transition refuses."""
    dips: List[Tuple[float, Tuple[float, float, float]]] = []
    for job in getattr(task, "transition_jobs", ()) or ():
        trans, spec = job
        if spec is not None or getattr(trans, "transition_type", None) != "dip_to_color":
            return _refuse(f"transition_unsupported:{getattr(trans, 'transition_type', '?')}")
        try:
            factor = float(trans.get_color_factor())
            color = tuple(max(0.0, min(1.0, float(c))) for c in trans.blend_color)
        except (AttributeError, TypeError, ValueError):
            return _refuse("transition_malformed")
        if not np.isfinite(factor) or len(color) != 3:
            return _refuse("transition_malformed")
        # The GL composite skips a dip that contributes nothing.
        if factor > 0.0:
            dips.append((factor, color))
    return tuple(dips)


def build_metal_composite_plan(
    task: Any, *, max_layers: int = MAX_COMPOSITE_LAYERS,
) -> Union[MetalCompositePlan, PlanRefusal]:
    """Decide whether ``task`` may be composited on Metal; never consumes it.

    The caller has already tried the single-clip plan, so a refusal here
    leaves the frame to the GL path untouched.
    """
    layers = getattr(task, "layers", None) or []
    if not layers:
        return _refuse("no_layers")
    if len(layers) > max_layers:
        return _refuse(f"too_many_layers:{len(layers)}")
    if getattr(task, "adjustment_layers", None):
        return _refuse("adjustment_layers")
    dips = _dip_jobs(task)
    if isinstance(dips, PlanRefusal):
        return dips
    planned: List[MetalLayerPlan] = []
    for layer in layers:
        planned_layer = _layer_plan(layer)
        if isinstance(planned_layer, PlanRefusal):
            return planned_layer
        planned.append(planned_layer)
    return MetalCompositePlan(layers=tuple(planned), dips=dips)


def inverse_quad_map(
    transform: tuple, output_size: Tuple[int, int], coordinate_size: Tuple[int, int],
) -> Tuple[Tuple[float, float, float, float], Tuple[float, float, float, float]]:
    """Inverse of the GL quad deformation, as the two rows the kernels use.

    ``_transform_to_ndc_mat3_bytes`` maps the undeformed quad (NDC) to the
    screen.  A rasteriser interpolates the quad's texcoords affinely, so the
    texcoord of a screen pixel is that matrix applied backwards:
    ``tc = (M⁻¹ · ndc + 1) / 2``.  The rows return the ``ndc → M⁻¹ · ndc``
    half; the kernels add the ``+ 1`` over two themselves.
    """
    from src.features.edit.core.edit_gpu_preview_renderer import (
        _transform_to_ndc_mat3_bytes,
    )

    matrix = np.frombuffer(_transform_to_ndc_mat3_bytes(
        transform, int(output_size[0]), int(output_size[1]), track_fbo_y_flipped=True,
        coordinate_width=int(coordinate_size[0]), coordinate_height=int(coordinate_size[1]),
    ), dtype=np.float32)
    m00, m10, _2, m01, m11, _5, m02, m12, _8 = (float(value) for value in matrix)
    determinant = m00 * m11 - m01 * m10
    if abs(determinant) < 1e-9:
        raise ValueError("transform collapses the quad (singular matrix)")
    a, b = m11 / determinant, -m01 / determinant
    c, d = -m10 / determinant, m00 / determinant
    return (a, b, -(a * m02 + b * m12), 0.0), (c, d, -(c * m02 + d * m12), 0.0)


def layer_params(
    layer: MetalLayerPlan, output_size: Tuple[int, int], coordinate_size: Tuple[int, int],
) -> np.ndarray:
    """The ``edit_layer_params`` struct of one layer as a numpy record."""
    params = np.zeros(1, dtype=_LAYER_PARAMS_DTYPE)
    if layer.transform is None:
        # The undeformed quad: ndc -> ndc, so a pixel reads its own place.
        params["row0"][0] = (1.0, 0.0, 0.0, 0.0)
        params["row1"][0] = (0.0, 1.0, 0.0, 0.0)
    else:
        row0, row1 = inverse_quad_map(layer.transform, output_size, coordinate_size)
        params["row0"][0] = row0
        params["row1"][0] = row1
        params["flags"][0][0] = 1
    if layer.crop is not None:
        from src.features.edit.core.crop_render import crop_gl_uniforms

        rect, feather = crop_gl_uniforms(
            layer.crop, int(output_size[0]), int(output_size[1]), fbo_y_flipped=False,
        )
        params["crop_rect"][0] = rect
        params["crop_feather"][0] = (feather[0], feather[1], 0.0, 0.0)
        params["flags"][0][1] = 1
    return params


# ---------------------------------------------------------------------------
# 编码
# ---------------------------------------------------------------------------

class MetalEditCompositor:
    """Scratch textures and kernels for compositing on one Metal context.

    ``lut_provider(reference, strength)`` returns the ``(texture, strength
    buffer)`` of a 3D LUT; the :class:`MetalTimelinePreview` owns that cache.
    """

    _KERNELS = (
        "edit_layer_nv12", "edit_layer_solid", "edit_crop_matte_pass", "edit_track_lut",
        "edit_composite_first", "edit_composite_blend", "edit_dip_to_color",
    )

    def __init__(
        self,
        context: Any,
        lut_provider: Callable[[str, float], Tuple[Any, Any]],
    ) -> None:
        self._context = context
        self._lut_provider = lut_provider
        self._kernels: Dict[str, Any] = {
            name: context.compile_kernel(name, EDIT_COMPOSITE_MSL) for name in self._KERNELS
        }
        self._size: Optional[Tuple[int, int]] = None
        self._tracks: List[Any] = []       # track texture, LUT-graded texture
        self._accum: List[Any] = []        # composite ping-pong

    def _ensure(self, size: Tuple[int, int]) -> None:
        """(Re)create the full-frame scratch textures for ``size``.

        Old textures are only referenced by this compositor.  A command
        buffer that still reads or writes them keeps them alive until it
        completes, so dropping the Python reference here is safe.
        """
        size = (int(size[0]), int(size[1]))
        if size == self._size:
            return
        self._tracks = [
            self._context.create_texture_2d(
                MTL_PIXEL_FORMAT_RGBA8_UNORM, size[0], size[1], shader_write=True)
            for _ in range(2)
        ]
        self._accum = [
            self._context.create_texture_2d(
                MTL_PIXEL_FORMAT_RGBA8_UNORM, size[0], size[1], shader_write=True)
            for _ in range(2)
        ]
        self._size = size

    @property
    def size(self) -> Optional[Tuple[int, int]]:
        return self._size

    def release(self) -> None:
        self._tracks = []
        self._accum = []
        self._size = None

    def _dispatch(self, kernel: str, *, size: Tuple[int, int],
                  buffers: Optional[Dict[int, Any]] = None,
                  textures: Optional[Dict[int, Any]] = None) -> None:
        self._context.dispatch(
            self._kernels[kernel], buffers=buffers, textures=textures,
            threads=(size[0], size[1], 1),
        )

    def _buffer(self, values: Sequence[float], dtype: Any) -> Any:
        """A small per-dispatch constant buffer (the command buffer owns it)."""
        return self._context.create_buffer_from_numpy(np.asarray(values, dtype=dtype))

    @staticmethod
    def _release_leases(leases: List[Any]) -> None:
        for lease in leases:
            lease.release()
        leases.clear()

    def encode(
        self,
        plan: MetalCompositePlan,
        output: Any,
        size: Tuple[int, int],
        coordinate_size: Tuple[int, int],
        *,
        import_source: Callable[[Any], Optional[Tuple[Tuple[Any, Any], Any]]],
    ) -> List[Any]:
        """Encode the frame into ``output``; nothing is submitted or waited for.

        ``import_source(vt_frame)`` imports one layer's planes and returns
        ``((y_plane, uv_plane), lease)``, or ``None`` when that frame cannot
        be imported.  Returns the import leases: the caller keeps them (and
        the VT frames) alive until the GPU finished reading.  On any failure
        the leases taken so far are released again and the error is raised,
        so the caller can hand the untouched frame to the GL path.
        """
        self._ensure(size)
        leases: List[Any] = []
        fresh = not self._context.has_pending_work
        track, graded = self._tracks
        # Without a dip the last blend goes straight into the output; with
        # dips the composite ping-pongs and the last dip writes the output.
        direct = not plan.dips
        current: Any = None                 # the composite so far
        current_slot = 1                    # its accum index (ignored once current is None)
        try:
            for index, layer in enumerate(plan.layers):
                planes: Optional[Tuple[Any, Any]] = None
                if layer.kind == "nv12":
                    imported = import_source(layer.source)
                    if imported is None:
                        raise MetalCompositeError("source import unavailable")
                    planes, lease = imported
                    leases.append(lease)
                # Per-track chain: source with its transform, the crop matte,
                # then the LUT — each reading what the GL track FBO holds.
                params = self._context.create_buffer_from_numpy(
                    layer_params(layer, size, coordinate_size))
                if planes is not None:
                    self._dispatch(
                        "edit_layer_nv12", size=size, buffers={0: params},
                        textures={0: planes[0].resolved_handle(),
                                  1: planes[1].resolved_handle(), 2: track},
                    )
                else:
                    solid = self._buffer((*layer.solid_color, 1.0), np.float32)
                    self._dispatch(
                        "edit_layer_solid", size=size, buffers={0: params, 1: solid},
                        textures={0: track},
                    )
                    if layer.crop is not None:
                        # The GL order for a solid: clear, transform, then
                        # the matte over the colour the track FBO already
                        # holds — not over the float the clear started from.
                        self._dispatch(
                            "edit_crop_matte_pass", size=size, buffers={0: params},
                            textures={0: track, 1: graded},
                        )
                        track, graded = graded, track
                track_texture = track
                if layer.lut is not None:
                    lut_texture, strength = self._lut_provider(*layer.lut)
                    self._dispatch(
                        "edit_track_lut", size=size, buffers={0: strength},
                        textures={0: track, 1: graded, 2: lut_texture},
                    )
                    track_texture = graded

                # Blend stage, bottom-up as _composite_tracks.
                opacity = self._buffer((layer.opacity,), np.float32)
                if index == 0:
                    # The bottom layer is drawn over black, blend mode forced to normal.
                    if direct:
                        dest, slot = output, None
                    else:
                        dest, slot = self._accum[0], 0
                    self._dispatch(
                        "edit_composite_first", size=size, buffers={0: opacity},
                        textures={0: track_texture, 1: dest},
                    )
                else:
                    if direct and index == len(plan.layers) - 1:
                        dest, slot = output, None
                    else:
                        slot = 1 - current_slot
                        dest = self._accum[slot]
                    mode = self._buffer((layer.blend_mode,), np.int32)
                    self._dispatch(
                        "edit_composite_blend", size=size,
                        buffers={0: mode, 1: opacity},
                        textures={0: current, 1: track_texture, 2: dest},
                    )
                current, current_slot = dest, slot if slot is not None else current_slot

            for position, (factor, color) in enumerate(plan.dips):
                last = position == len(plan.dips) - 1
                if last:
                    dest = output
                else:
                    current_slot = 1 - current_slot
                    dest = self._accum[current_slot]
                dip = self._buffer((*color, factor), np.float32)
                self._dispatch(
                    "edit_dip_to_color", size=size, buffers={0: dip},
                    textures={0: current, 1: dest},
                )
                current = dest
        except Exception:
            self._release_leases(leases)
            if fresh:
                # The layers encoded so far would run with the next
                # submission and read the imports that were just given back.
                self._context.discard_pending()
            raise
        return leases
