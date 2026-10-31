"""Small, pixel-free transition descriptions for GPU render graphs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Optional

from src.features.edit.core.transition_mask import _shape_vertices
from src.features.edit.core.transition_wipe import get_wipe_params
from src.features.edit.core.transition_catalog import TRANSITION_EFFECT_TYPES


@dataclass(frozen=True)
class GpuTransitionMaskSpec:
    """Procedural transition parameters consumed by GL/Metal GPU passes.

    ``kind`` is 0 for a directional wipe, 1 for a circle, 2 for an ellipse,
    3 for a centred polygon, and 4 for a full-frame dissolve. ``vertices`` is
    a small control payload, not a per-pixel CPU mask. ``effect_mode`` adds a
    source-side GPU effect for the motion-blur transition family.
    """

    kind: int
    progress: float
    direction: int = 0
    angle: float = 0.0
    softness: float = 0.02
    reverse: bool = False
    vertices: tuple[tuple[float, float], ...] = ()
    transition_mode: int = 0  # full frame: blur, defocus, film, smooth cut
    effect_mode: int = 0  # motion, whip, zoom, spin, RGB split
    blur_ratio: float = 0.045
    blur_radius: float = 0.0
    blur_mix: float = 0.0
    shutter: float = 1.0
    blur_strength: float = 0.18
    spin_angle: float = 18.0
    split_amount: float = 0.045
    split_mix: float = 0.96
    # LIB1（L03）：kind=5 时钟极角的中心（归一）；resample_mode=1 双源
    # 平移（mask 通道弃用，GL main() 走 push 分支）。
    center: tuple[float, float] = (0.5, 0.5)
    resample_mode: int = 0  # 0=mask composite, 1=dual-source push
    push_direction: int = 0  # 0=left, 1=right, 2=up, 3=down
    # LIB1（L13）mask 家族参数块（最多 8 个 float，GLSL 侧 u_mask_p0/p1）：
    # kind=6 百叶 (count, angle, phase, softness)
    # kind=7 棋盘 (rows, columns, phase, softness)
    # kind=8 亮度 (threshold, span, invert, source)
    # kind=9 噪声 (scale, seed, softness, mode)
    # kind=10 网点 (spacing, angle, softness, shape)
    mask_params: tuple[float, ...] = ()


def _float_param(
    params: Mapping[str, Any] | None,
    key: str,
    default: float,
) -> float:
    try:
        value = float((params or {}).get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value == value and abs(value) != float("inf") else default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def build_gpu_transition_mask_spec(
    transition_type: str,
    progress: float,
    params: Mapping[str, Any] | None = None,
) -> Optional[GpuTransitionMaskSpec]:
    """Build a GPU-safe procedural mask description or return ``None``."""
    from src.features.edit.core.custom_transitions import (
        merge_user_transition_params,
    )

    resolved = merge_user_transition_params(transition_type, params)
    if resolved is not None:
        base_type, merged_params = resolved
        return build_gpu_transition_mask_spec(
            base_type, progress, merged_params)

    transition_type = str(transition_type or "")
    if transition_type == "lib1_clock_wipe":
        # LIB1/X01（L03）：kind=5 时钟极角。angle 复用为 start_angle（度）、
        # reverse 复用为逆时针开关、softness 为归一角宽（×2π——与 wipe 的
        # 长边比例口径不同，见 transition_packs cores 注释）。
        return GpuTransitionMaskSpec(
            kind=5,
            progress=max(0.0, min(1.0, float(progress))),
            angle=_float_param(params, "start_angle", 0.0),
            softness=_clamp(_float_param(params, "softness", 0.02), 0.0, 0.5),
            reverse=not bool((params or {}).get("clockwise", True)),
            center=(
                _clamp(_float_param(params, "center_x", 0.5), 0.0, 1.0),
                _clamp(_float_param(params, "center_y", 0.5), 0.0, 1.0),
            ),
        )

    if transition_type == "lib1_venetian_blinds":
        # LIB1/X02（L13）：kind=6 周期百叶（全带同时扫掠）。
        return GpuTransitionMaskSpec(
            kind=6,
            progress=max(0.0, min(1.0, float(progress))),
            mask_params=(
                _clamp(float(int(_float_param(params, "count", 6))), 2.0, 32.0),
                _clamp(_float_param(params, "angle", 0.0), -90.0, 90.0),
                _float_param(params, "phase", 0.0) % 1.0,
                _clamp(_float_param(params, "softness", 0.05), 0.0, 0.5),
            ),
        )

    if transition_type == "lib1_checker_reveal":
        # LIB1/X03（L13）：kind=7 棋盘奇偶两波。
        return GpuTransitionMaskSpec(
            kind=7,
            progress=max(0.0, min(1.0, float(progress))),
            mask_params=(
                _clamp(float(int(_float_param(params, "rows", 4))), 1.0, 16.0),
                _clamp(float(int(_float_param(params, "columns", 4))), 1.0, 16.0),
                _float_param(params, "phase", 0.0) % 1.0,
                _clamp(_float_param(params, "softness", 0.05), 0.0, 0.5),
            ),
        )

    if transition_type == "lib1_luma_reveal":
        # LIB1/X05（L13）：kind=8 内容驱动亮度揭示（GLSL 采样 u_left_tex）。
        source = str((params or {}).get("source", "A"))
        return GpuTransitionMaskSpec(
            kind=8,
            progress=max(0.0, min(1.0, float(progress))),
            mask_params=(
                _clamp(_float_param(params, "threshold", 0.5), 0.0, 1.0),
                _clamp(_float_param(params, "span", 0.2), 0.01, 0.5),
                1.0 if bool((params or {}).get("invert", False)) else 0.0,
                0.0 if source != "B" else 1.0,
            ),
        )

    if transition_type == "lib1_noise_dissolve":
        # LIB1/X06（L13）：kind=9 uint32 格点散列 rank 场。
        mode = str((params or {}).get("noise_mode", "Value"))
        return GpuTransitionMaskSpec(
            kind=9,
            progress=max(0.0, min(1.0, float(progress))),
            mask_params=(
                _clamp(float(int(_float_param(params, "scale", 16))), 2.0, 64.0),
                _clamp(float(int(_float_param(params, "seed", 0))), 0.0, 100.0),
                _clamp(_float_param(params, "softness", 0.05), 0.0, 0.5),
                0.0 if mode != "Cell" else 1.0,
            ),
        )

    if transition_type == "lib1_halftone_reveal":
        # LIB1/X08（L13）：kind=10 网点扩张（Dot 径向 / Square 切比雪夫）。
        shape = str((params or {}).get("shape", "Dot"))
        return GpuTransitionMaskSpec(
            kind=10,
            progress=max(0.0, min(1.0, float(progress))),
            mask_params=(
                _clamp(float(int(_float_param(params, "spacing", 16))), 4.0, 64.0),
                _clamp(_float_param(params, "angle", 45.0), -90.0, 90.0),
                _clamp(_float_param(params, "softness", 0.1), 0.0, 0.5),
                0.0 if shape != "Square" else 1.0,
            ),
        )

    if transition_type == "lib1_voronoi_reveal":
        # LIB1/X07（L14）：kind=11 voronoi 晶胞推进（hash 扰动种子）。
        cell = str((params or {}).get("cell_count", "16"))
        if cell not in ("8", "16", "32"):
            cell = "16"
        return GpuTransitionMaskSpec(
            kind=11,
            progress=max(0.0, min(1.0, float(progress))),
            mask_params=(
                float(cell),
                _clamp(float(int(_float_param(params, "seed", 7))), 0.0, 100.0),
                _clamp(_float_param(params, "edge_width", 0.1), 0.0, 0.5),
                0.0,
            ),
        )

    if transition_type == "lib1_cube_faces":
        # LIB1/X11（L14b 批B）：kind=20 复合（双面立方）。
        axis = str((params or {}).get("axis", "Horizontal"))
        return GpuTransitionMaskSpec(
            kind=20,
            progress=max(0.0, min(1.0, float(progress))),
            resample_mode=2,
            mask_params=(
                _clamp(_float_param(params, "perspective", 0.5), 0.0, 1.0),
                _clamp(_float_param(params, "shadow", 0.5), 0.0, 1.0),
                1.0 if axis == "Vertical" else 0.0,
                0.0,
            ),
        )

    if transition_type == "lib1_hinged_doors":
        # LIB1/X10（L14b 批B）：kind=19 复合（铰链门）。
        hinge = str((params or {}).get("hinge", "Both"))
        return GpuTransitionMaskSpec(
            kind=19,
            progress=max(0.0, min(1.0, float(progress))),
            resample_mode=2,
            mask_params=(
                _clamp(_float_param(params, "perspective", 0.5), 0.0, 2.0),
                _clamp(_float_param(params, "shadow", 0.5), 0.0, 1.0),
                1.0 if hinge == "Left" else 0.0,
                0.0,
            ),
        )

    if transition_type == "lib1_page_curl":
        # LIB1/X12（L14b 批B）：kind=18 复合（卷页）。
        direction = str((params or {}).get("direction", "Left"))
        back = (params or {}).get("back_color", (0.85, 0.82, 0.78))
        return GpuTransitionMaskSpec(
            kind=18,
            progress=max(0.0, min(1.0, float(progress))),
            resample_mode=2,
            mask_params=(
                _clamp(_float_param(params, "radius", 0.25), 0.05, 0.5),
                _clamp(_float_param(params, "shadow", 0.5), 0.0, 1.0),
                1.0 if direction == "Right" else 0.0,
                0.0,
            ),
            # back_color 走 vertices[0..1]（(r,g) 与 (b,0)）复用现成 payload
            vertices=((float(back[0]), float(back[1])),
                      (float(back[2]), 0.0)),
        )

    if transition_type == "lib1_staggered_tiles":
        # LIB1/X04（L14b 批B）：kind=21 复合（错峰块收缩）。
        order = str((params or {}).get("order", "Row"))
        return GpuTransitionMaskSpec(
            kind=21,
            progress=max(0.0, min(1.0, float(progress))),
            resample_mode=2,
            mask_params=(
                _clamp(float(int(_float_param(params, "grid", 4))), 2.0, 16.0),
                _clamp(_float_param(params, "delay", 0.2), 0.0, 0.8),
                _clamp(float(int(_float_param(params, "seed", 0))), 0.0, 100.0),
                1.0 if order == "Random" else 0.0,
            ),
        )

    if transition_type == "lib1_kaleido_bridge":
        # LIB1/X17（L14b 批B）：kind=17 复合（角度折叠桥）。
        return GpuTransitionMaskSpec(
            kind=17,
            progress=max(0.0, min(1.0, float(progress))),
            resample_mode=2,
            center=(
                _clamp(_float_param(params, "center_x", 0.5), 0.0, 1.0),
                _clamp(_float_param(params, "center_y", 0.5), 0.0, 1.0),
            ),
            mask_params=(
                _clamp(float(int(_float_param(params, "sectors", 6))), 3.0, 12.0),
                _clamp(_float_param(params, "rotation", 0.0), -180.0, 180.0),
                _clamp(_float_param(params, "strength", 0.8), 0.0, 1.0),
                0.0,
            ),
        )

    if transition_type == "lib1_burn_reveal":
        # LIB1/X18（L14b 批B）：kind=16 复合（rank 揭示+前沿发热）。
        pattern = str((params or {}).get("pattern", "Field"))
        return GpuTransitionMaskSpec(
            kind=16,
            progress=max(0.0, min(1.0, float(progress))),
            resample_mode=2,
            mask_params=(
                _clamp(_float_param(params, "char_width", 0.08), 0.0, 0.5),
                _clamp(_float_param(params, "emission", 0.5), 0.0, 1.0),
                _clamp(float(int(_float_param(params, "seed", 0))), 0.0, 100.0),
                1.0 if pattern == "Edge" else 0.0,
            ),
        )

    if transition_type == "lib1_displacement_bridge":
        # LIB1/X14（L14b）：kind=13 复合（噪声位移桥）。
        mode = str((params or {}).get("noise_mode", "Wave"))
        return GpuTransitionMaskSpec(
            kind=13,
            progress=max(0.0, min(1.0, float(progress))),
            resample_mode=2,
            mask_params=(
                _clamp(_float_param(params, "scale", 0.1), 0.0, 0.25),
                _clamp(_float_param(params, "frequency", 4.0), 1.0, 16.0),
                _clamp(float(int(_float_param(params, "seed", 0))), 0.0, 100.0),
                1.0 if mode == "Cell" else 0.0,
            ),
        )

    if transition_type == "lib1_ripple_refraction":
        # LIB1/X15（L14b）：kind=14 复合（径向涟漪折射）。
        return GpuTransitionMaskSpec(
            kind=14,
            progress=max(0.0, min(1.0, float(progress))),
            resample_mode=2,
            center=(
                _clamp(_float_param(params, "center_x", 0.5), 0.0, 1.0),
                _clamp(_float_param(params, "center_y", 0.5), 0.0, 1.0),
            ),
            mask_params=(
                _clamp(_float_param(params, "frequency", 8.0), 1.0, 32.0),
                _clamp(_float_param(params, "amplitude", 0.1), 0.0, 0.25),
                _clamp(_float_param(params, "damping", 0.3), 0.0, 1.0),
                0.0,
            ),
        )

    if transition_type == "lib1_pixelate_bridge":
        # LIB1/X13（L14b）：kind=15 复合（块采样桥，In/Out 剖面）。
        direction = str((params or {}).get("direction", "In"))
        return GpuTransitionMaskSpec(
            kind=15,
            progress=max(0.0, min(1.0, float(progress))),
            resample_mode=2,
            mask_params=(
                _clamp(float(int(_float_param(params, "cell", 16))), 4.0, 64.0),
                _clamp(_float_param(params, "source_switch", 0.5), 0.01, 0.99),
                1.0 if direction == "Out" else 0.0,
                0.0,
            ),
        )

    if transition_type == "lib1_scanline_slices":
        # LIB1/X16（L14b 批A）：kind=12 复合家族（resample_mode=2，
        # GL main() 按 kind 分派 UV 错位）。curve=Ease 用 w 位。
        curve = str((params or {}).get("curve", "Linear"))
        return GpuTransitionMaskSpec(
            kind=12,
            progress=max(0.0, min(1.0, float(progress))),
            resample_mode=2,
            mask_params=(
                _clamp(float(int(_float_param(params, "band_px", 8))), 2.0, 64.0),
                _clamp(_float_param(params, "amount", 0.1), 0.0, 0.25),
                _clamp(float(int(_float_param(params, "seed", 0))), 0.0, 100.0),
                1.0 if curve == "Ease" else 0.0,
            ),
        )

    if transition_type == "lib1_true_push":
        # LIB1/X09（L03）：双源 UV 重采样；mask 通道弃用（resample 分支）。
        direction_map = {"left": 0, "right": 1, "up": 2, "down": 3}
        direction = str((params or {}).get("direction", "left"))
        if direction not in direction_map:
            direction = "left"
        return GpuTransitionMaskSpec(
            kind=0,
            progress=max(0.0, min(1.0, float(progress))),
            resample_mode=1,
            push_direction=direction_map[direction],
            # u_softness 在 resample 语义下 = 归一接缝半宽（默认 0=硬接缝）
            softness=_clamp(_float_param(params, "seam_softness", 0.0), 0.0, 0.25),
        )

    if transition_type in {
        "blur_dissolve", "defocus_dissolve", "film_dissolve", "smooth_cut",
    }:
        transition_mode = {
            "blur_dissolve": 0,
            "defocus_dissolve": 1,
            "film_dissolve": 2,
            "smooth_cut": 3,
        }[transition_type]
        return GpuTransitionMaskSpec(
            kind=4,
            progress=max(0.0, min(1.0, float(progress))),
            transition_mode=transition_mode,
            blur_ratio=max(0.0, _float_param(
                params, "blur_ratio",
                0.075 if transition_type == "defocus_dissolve" else 0.045,
            )),
        )

    effect_mode = 0
    if transition_type in TRANSITION_EFFECT_TYPES:
        if transition_type.startswith("motion_blur_"):
            effect_mode = 1
        elif transition_type.startswith("whip_pan_"):
            effect_mode = 2
        elif transition_type.startswith("zoom_blur_"):
            effect_mode = 3
        elif transition_type.startswith("spin_blur_"):
            effect_mode = 4
        elif transition_type.startswith("rgb_split_"):
            effect_mode = 5

    if transition_type in {
        "slide_left", "slide_right", "slide_up", "slide_down",
    }:
        direction = transition_type.removeprefix("slide_")
        params = {**(params or {}), "direction": direction}
        transition_type = "wipe"

    if transition_type in TRANSITION_EFFECT_TYPES:
        if transition_type in {
            "zoom_blur_in", "zoom_blur_out", "spin_blur_cw",
            "spin_blur_ccw",
        }:
            # Zoom/spin use a centred reveal; their source pre-pass supplies
            # the blur while the procedural mask supplies the geometry.
            base_spec = build_gpu_transition_mask_spec(
                "iris_circle", progress, params,
            )
            if base_spec is None:
                return None
            reverse_radial = transition_type in {
                "zoom_blur_out", "spin_blur_ccw",
            }
            return replace(
                base_spec,
                # The geometric circle ignores direction, while the source
                # radial pass uses it to distinguish in/out and CW/CCW.
                direction=1 if reverse_radial else 0,
                effect_mode=effect_mode,
                blur_mix=_clamp(_float_param(params, "blur_mix", 0.88), 0.0, 1.0),
                blur_strength=max(0.0, _float_param(params, "blur_strength", 0.18)),
                spin_angle=max(0.0, _float_param(params, "spin_angle", 18.0)),
            )
        direction = transition_type.rsplit("_", 1)[-1]
        params = {**(params or {}), "direction": direction}
        transition_type = "wipe"

    if transition_type == "wipe":
        wipe = get_wipe_params(params)
        direction = {
            "left": 0, "right": 1, "up": 2, "down": 3, "angle": 4,
        }[wipe["direction"]]
        spec = GpuTransitionMaskSpec(
            kind=0,
            progress=max(0.0, min(1.0, float(progress))),
            direction=direction,
            angle=float(wipe["angle"]),
            softness=float(wipe["softness"]),
            reverse=bool(wipe["reverse"]),
        )
        if effect_mode:
            default_radius = 34.0 if effect_mode == 2 else 22.0
            return replace(
                spec,
                effect_mode=effect_mode,
                blur_radius=max(0.0, _float_param(
                    params, "blur_radius", default_radius)),
                blur_mix=_clamp(_float_param(
                    params, "blur_mix", 0.92 if effect_mode == 2 else 0.82),
                    0.0, 1.0),
                shutter=_clamp(_float_param(params, "shutter", 1.0), 0.0, 2.0),
                split_amount=max(0.0, _float_param(
                    params, "split_amount", 0.045)),
                split_mix=_clamp(_float_param(
                    params, "split_mix", 0.96), 0.0, 1.0),
            )
        return spec

    if transition_type in {"iris_circle", "iris_ellipse"}:
        return GpuTransitionMaskSpec(
            kind=1 if transition_type == "iris_circle" else 2,
            progress=max(0.0, min(1.0, float(progress))),
        )

    vertices = _shape_vertices(transition_type)
    if vertices.size == 0 or len(vertices) > 48:
        return None
    return GpuTransitionMaskSpec(
        kind=3,
        progress=max(0.0, min(1.0, float(progress))),
        vertices=tuple(
            (float(vertex[0]), float(vertex[1])) for vertex in vertices
        ),
    )


__all__ = ["GpuTransitionMaskSpec", "build_gpu_transition_mask_spec"]
