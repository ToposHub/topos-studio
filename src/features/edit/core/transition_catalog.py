"""Shared transition catalogue used by the Edit UI and render pipeline.

Keeping the names in one place prevents the transition picker, model validation
and the native renderer from drifting apart as new transitions are added.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TransitionDefinition:
    transition_type: str
    label: str
    category: str
    spatial: bool = False
    # C2: 用户自定义转场（source="user"）；base_type 为内置参数化基类型，
    # params 为定义级默认参数（实例 params 优先合并）。
    source: str = "builtin"
    base_type: str = ""
    params: dict = field(default_factory=dict)


TRANSITION_DEFINITIONS: tuple[TransitionDefinition, ...] = (
    TransitionDefinition("cross_dissolve", "Cross Dissolve", "Dissolve"),
    TransitionDefinition("dip_to_color", "Dip To Color", "Dissolve"),
    # Full-frame, content-driven dissolves. These deliberately do not use a
    # geometric mask: both sources are composed across the whole image so the
    # cut can disappear inside defocus, linear-light blending, or GPU motion
    # smoothing.
    TransitionDefinition("blur_dissolve", "Blur Dissolve", "Dissolve"),
    TransitionDefinition("defocus_dissolve", "Defocus Dissolve", "Dissolve"),
    TransitionDefinition("film_dissolve", "Film Dissolve", "Dissolve"),
    TransitionDefinition("smooth_cut", "Smooth Cut", "Dissolve"),
    TransitionDefinition("wipe", "Wipe", "Motion", spatial=True),
    TransitionDefinition("iris_circle", "Iris Circle", "Iris", spatial=True),
    TransitionDefinition("iris_diamond", "Iris Diamond", "Iris", spatial=True),
    TransitionDefinition("iris_hexagon", "Iris Hexagon", "Iris", spatial=True),
    TransitionDefinition("iris_plus", "Iris Plus", "Iris", spatial=True),
    TransitionDefinition("iris_square", "Iris Square", "Iris", spatial=True),
    TransitionDefinition("iris_ellipse", "Iris Ellipse", "Iris", spatial=True),
    TransitionDefinition("iris_triangle", "Iris Triangle", "Shape", spatial=True),
    TransitionDefinition("iris_arrow", "Iris Arrow", "Shape", spatial=True),
    TransitionDefinition("iris_star", "Iris Star", "Shape", spatial=True),
    TransitionDefinition("iris_heart", "Iris Heart", "Shape", spatial=True),
    TransitionDefinition("slide_left", "Slide Left", "Motion", spatial=True),
    TransitionDefinition("slide_right", "Slide Right", "Motion", spatial=True),
    TransitionDefinition("slide_up", "Slide Up", "Motion", spatial=True),
    TransitionDefinition("slide_down", "Slide Down", "Motion", spatial=True),
    # Camera-style transitions are spatial composites as well, but the CPU
    # compositor additionally applies a direction-aware streak before the
    # mask.  Keeping them in the shared catalogue makes them available to the
    # UI, timeline model, AI capability manifest and preview generator at
    # once.
    TransitionDefinition(
        "motion_blur_left", "Motion Blur Left", "Motion Blur", spatial=True,
    ),
    TransitionDefinition(
        "motion_blur_right", "Motion Blur Right", "Motion Blur", spatial=True,
    ),
    TransitionDefinition(
        "motion_blur_up", "Motion Blur Up", "Motion Blur", spatial=True,
    ),
    TransitionDefinition(
        "motion_blur_down", "Motion Blur Down", "Motion Blur", spatial=True,
    ),
    TransitionDefinition(
        "whip_pan_left", "Whip Pan Left", "Motion Blur", spatial=True,
    ),
    TransitionDefinition(
        "whip_pan_right", "Whip Pan Right", "Motion Blur", spatial=True,
    ),
    TransitionDefinition(
        "zoom_blur_in", "Zoom Blur In", "Motion Blur", spatial=True,
    ),
    TransitionDefinition(
        "zoom_blur_out", "Zoom Blur Out", "Motion Blur", spatial=True,
    ),
    TransitionDefinition(
        "spin_blur_cw", "Spin Blur CW", "Motion Blur", spatial=True,
    ),
    TransitionDefinition(
        "spin_blur_ccw", "Spin Blur CCW", "Motion Blur", spatial=True,
    ),
    TransitionDefinition(
        "rgb_split_left", "RGB Split Left", "Motion Blur", spatial=True,
    ),
    TransitionDefinition(
        "rgb_split_right", "RGB Split Right", "Motion Blur", spatial=True,
    ),
    # LIB1 内容包（计划 L / L03）：X01 时钟极角 mask（现有转场零极角实现）
    # 与 X09 真双源平移（slide 复用 wipe mask、双源不动——见 L00 台账）。
    # CPU 合成与 GPU spec 走 transition_packs/library_v1；MSL 未实现，
    # metal 导出按 lib1 前缀显式回退 CPU（禁静默替换）。
    TransitionDefinition(
        "lib1_clock_wipe", "Clock Wipe (Lib1)", "Motion", spatial=True,
    ),
    TransitionDefinition(
        "lib1_true_push", "True Push (Lib1)", "Motion", spatial=True,
    ),
    # LIB1 内容包（计划 L / L13）：周期/棋盘/内容驱动/噪声/网点揭示 mask 家族。
    TransitionDefinition(
        "lib1_venetian_blinds", "Venetian Blinds (Lib1)", "Motion", spatial=True,
    ),
    TransitionDefinition(
        "lib1_checker_reveal", "Checker Reveal (Lib1)", "Motion", spatial=True,
    ),
    TransitionDefinition(
        "lib1_luma_reveal", "Luma Reveal (Lib1)", "Motion", spatial=True,
    ),
    TransitionDefinition(
        "lib1_noise_dissolve", "Noise Dissolve (Lib1)", "Motion", spatial=True,
    ),
    TransitionDefinition(
        "lib1_halftone_reveal", "Halftone Reveal (Lib1)", "Motion", spatial=True,
    ),
    # LIB1 内容包（计划 L / L14/L15）：双源重采样/复合家族 + X07 mask。
    TransitionDefinition(
        "lib1_staggered_tiles", "Staggered Tiles (Lib1)", "Motion", spatial=True,
    ),
    TransitionDefinition(
        "lib1_hinged_doors", "Hinged Doors (Lib1)", "Motion", spatial=True,
    ),
    TransitionDefinition(
        "lib1_cube_faces", "Cube Faces (Lib1)", "Motion", spatial=True,
    ),
    TransitionDefinition(
        "lib1_page_curl", "Page Curl (Lib1)", "Motion", spatial=True,
    ),
    TransitionDefinition(
        "lib1_pixelate_bridge", "Pixelate Bridge (Lib1)", "Motion", spatial=True,
    ),
    TransitionDefinition(
        "lib1_displacement_bridge", "Displacement Bridge (Lib1)", "Motion", spatial=True,
    ),
    TransitionDefinition(
        "lib1_ripple_refraction", "Ripple Refraction (Lib1)", "Motion", spatial=True,
    ),
    TransitionDefinition(
        "lib1_scanline_slices", "Scanline Slices (Lib1)", "Motion", spatial=True,
    ),
    TransitionDefinition(
        "lib1_kaleido_bridge", "Kaleido Bridge (Lib1)", "Motion", spatial=True,
    ),
    TransitionDefinition(
        "lib1_burn_reveal", "Burn Reveal (Lib1)", "Motion", spatial=True,
    ),
    TransitionDefinition(
        "lib1_voronoi_reveal", "Voronoi Reveal (Lib1)", "Motion", spatial=True,
    ),
)

TRANSITION_TYPE_OPTIONS: tuple[str, ...] = tuple(
    definition.transition_type for definition in TRANSITION_DEFINITIONS
)
TRANSITION_TYPE_LABELS: tuple[str, ...] = tuple(
    definition.label for definition in TRANSITION_DEFINITIONS
)
SPATIAL_TRANSITION_TYPES = frozenset(
    definition.transition_type
    for definition in TRANSITION_DEFINITIONS
    if definition.spatial
)
FULL_FRAME_TRANSITION_TYPES = frozenset({
    "blur_dissolve",
    "defocus_dissolve",
    "film_dissolve",
    "smooth_cut",
})
GPU_FULL_FRAME_TRANSITION_TYPES = frozenset({
    "blur_dissolve",
    "defocus_dissolve",
    "film_dissolve",
    "smooth_cut",
})
COMPOSITE_TRANSITION_TYPES = frozenset(
    set(SPATIAL_TRANSITION_TYPES) | set(FULL_FRAME_TRANSITION_TYPES)
)
MOTION_BLUR_TRANSITION_TYPES = frozenset(
    definition.transition_type
    for definition in TRANSITION_DEFINITIONS
    if definition.transition_type.startswith(("motion_blur_", "whip_pan_"))
)
# LIB1 双源重采样类（L03/X09）：合成方式是双源 UV 位移而非几何 mask；
# CPU 合成在 mask 路径前分流，GPU 走 resample_mode spec。
RESAMPLE_TRANSITION_TYPES = frozenset({
    "lib1_true_push",
    # L14/L15 双源复合家族（CPU 合成统一分派 composite_lib1_transition）
    "lib1_staggered_tiles", "lib1_hinged_doors", "lib1_cube_faces",
    "lib1_page_curl", "lib1_pixelate_bridge", "lib1_displacement_bridge",
    "lib1_ripple_refraction", "lib1_scanline_slices", "lib1_kaleido_bridge",
    "lib1_burn_reveal",
})
# MSL 侧未实现的 LIB1 转场（metal 导出显式回退 CPU，禁静默替换）。
LIB1_TRANSITION_TYPES = frozenset({
    "lib1_clock_wipe",
    "lib1_true_push",
    "lib1_venetian_blinds",
    "lib1_checker_reveal",
    "lib1_luma_reveal",
    "lib1_noise_dissolve",
    "lib1_halftone_reveal",
    # L14/L15（metal 侧 MSL 均未实现，导出显式回退 CPU）
    "lib1_staggered_tiles", "lib1_hinged_doors", "lib1_cube_faces",
    "lib1_page_curl", "lib1_pixelate_bridge", "lib1_displacement_bridge",
    "lib1_ripple_refraction", "lib1_scanline_slices", "lib1_kaleido_bridge",
    "lib1_burn_reveal", "lib1_voronoi_reveal",
})
TRANSITION_EFFECT_TYPES = frozenset(
    definition.transition_type
    for definition in TRANSITION_DEFINITIONS
    if definition.category == "Motion Blur"
)
TRANSITION_LABELS = {
    definition.transition_type: definition.label
    for definition in TRANSITION_DEFINITIONS
}


def resolve_transition_base_type(transition_type: str) -> str:
    """C2/AUD-19202: 返回转场的内置 base 类型。

    用户自定义转场（``user_*``）解析为其声明的内置参数化 base；内置类型
    原样返回；未注册的 user_* / 未知类型原样返回（调用方按未知处理）。
    """
    from src.features.edit.core.custom_transitions import (
        resolve_user_transition,
    )

    resolved = resolve_user_transition(transition_type)
    if resolved is not None:
        return resolved[1]
    return transition_type


def is_dip_transition(transition_type: str) -> bool:
    """AUD-19202: 是否经 dip-to-color 渲染（合成域颜色 pass）。

    覆盖内置 ``dip_to_color`` 与 base 为 ``dip_to_color`` 的用户自定义
    转场——后者此前不被任何判定继承，预览/导出中退化为普通溶解，dip
    效果完全不渲染。
    """
    return resolve_transition_base_type(transition_type) == "dip_to_color"


def is_spatial_transition(transition_type: str) -> bool:
    """Return whether a transition needs a two-source spatial composite."""
    if transition_type in SPATIAL_TRANSITION_TYPES:
        return True
    # C2: 用户自定义转场继承 base 的空间属性
    from src.features.edit.core.custom_transitions import (
        is_user_transition_spatial,
    )
    user_spatial = is_user_transition_spatial(transition_type)
    return bool(user_spatial)


def is_full_frame_transition(transition_type: str) -> bool:
    """Return whether a transition composes both sources over the full frame.

    AUD-19202: 用户自定义转场按解析后的 base 判定（无几何 mask 的整幅
    双源合成，含 dip_to_color 的整幅颜色叠加语义）。
    """
    base = resolve_transition_base_type(transition_type)
    return base in FULL_FRAME_TRANSITION_TYPES or base == "dip_to_color"


def is_composite_transition(transition_type: str) -> bool:
    """Return whether a transition needs a two-source composite pass.

    AUD-19202: 统一解析用户转场 base，替代调用方各自内联的冻结集合判断
    （内联判断使 user_* 类型永远不进双源合成循环）。dip 类转场不在此列
    ——它们经合成域颜色 pass 渲染（见 :func:`is_dip_transition`），与
    内置 dip_to_color 的既有处理路径一致。
    """
    base = resolve_transition_base_type(transition_type)
    return (
        base in COMPOSITE_TRANSITION_TYPES
        or is_spatial_transition(transition_type)
    )


def is_motion_blur_transition(transition_type: str) -> bool:
    """Return whether a spatial transition also needs a streak pass."""
    return transition_type in MOTION_BLUR_TRANSITION_TYPES


def transition_label(transition_type: str) -> str:
    """C2: 转场显示名（用户转场回退其 label / type id）。"""
    label = TRANSITION_LABELS.get(transition_type)
    if label is not None:
        return label
    from src.features.edit.core.custom_transitions import user_transition_registry

    entry = user_transition_registry().get(transition_type)
    return entry.label if entry is not None else transition_type
