"""摄影机 filmback、匹配和世界单位的纯数据辅助函数。

这些函数不依赖 Qt/OpenGL，供 Camera3D、导入适配器和无 GPU 测试共享。
项目世界单位约定为米；Camera3D 的距离类参数可以用 ``unit`` 输入，随后
统一转换为渲染所用的米制世界空间。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import atan, degrees, radians, tan
from typing import Dict


WORLD_UNIT_TO_METERS: Dict[str, float] = {
    "m": 1.0,
    "cm": 0.01,
    "mm": 0.001,
    "ft": 0.3048,
    "in": 0.0254,
}


@dataclass(frozen=True)
class FilmbackSpec:
    """传感器尺寸（毫米）。"""

    name: str
    width_mm: float
    height_mm: float


# Width/height values follow common photographic gates. ``custom`` is kept
# compatible with the legacy Camera3D ``cam_sensor_size`` parameter.
FILMBACK_PRESETS: Dict[str, FilmbackSpec] = {
    "full_frame": FilmbackSpec("full_frame", 36.0, 24.0),
    "super35": FilmbackSpec("super35", 24.89, 14.0),
    "aps_c": FilmbackSpec("aps_c", 22.2, 14.8),
    "micro_four_thirds": FilmbackSpec("micro_four_thirds", 17.3, 13.0),
    "16mm": FilmbackSpec("16mm", 10.26, 7.49),
}


def world_unit_scale(unit: str) -> float:
    """Return the number of meters represented by one input world unit."""

    return WORLD_UNIT_TO_METERS.get(str(unit).strip().lower(), 1.0)


def filmback_spec(name: str, custom_width_mm: float = 36.0,
                  custom_height_mm: float = 36.0) -> FilmbackSpec:
    """Resolve a preset, clamping custom dimensions to safe positive values."""

    key = str(name or "custom")
    if key in FILMBACK_PRESETS:
        return FILMBACK_PRESETS[key]
    return FilmbackSpec(
        "custom",
        max(0.1, float(custom_width_mm)),
        max(0.1, float(custom_height_mm)),
    )


def sensor_height_for_gate_fit(spec: FilmbackSpec, aspect: float,
                               gate_fit: str) -> float:
    """Return the sensor height used by a vertical-FOV projection.

    ``vertical`` is the conventional filmback interpretation. ``horizontal``
    keeps the horizontal gate fixed and derives the vertical FOV from the
    output aspect. ``fill``/``overscan`` follow the Autodesk Maya convention
    (AUD-8605): Fill fits the RESOLUTION gate within the film gate (effective
    height = the SMALLER gate), Overscan fits the FILM gate within the
    resolution gate (effective height = the LARGER gate, shows more).
    """

    safe_aspect = max(float(aspect), 1e-6)
    horizontal_gate_height = spec.width_mm / safe_aspect
    key = str(gate_fit or "vertical").lower()
    if key == "horizontal":
        return horizontal_gate_height
    if key == "fill":
        # Resolution gate inside film gate → smaller effective height.
        return min(spec.height_mm, horizontal_gate_height)
    if key == "overscan":
        # Film gate inside resolution gate → larger effective height.
        return max(spec.height_mm, horizontal_gate_height)
    return spec.height_mm


def fov_from_focal_and_filmback(focal_length_mm: float, spec: FilmbackSpec,
                                aspect: float, gate_fit: str) -> float:
    """Compute vertical FOV from focal length and a filmback gate."""

    sensor_height = sensor_height_for_gate_fit(spec, aspect, gate_fit)
    focal = max(float(focal_length_mm), 0.1)
    return degrees(2.0 * atan(sensor_height / (2.0 * focal)))


def focal_from_fov_and_filmback(fov_deg: float, spec: FilmbackSpec,
                                aspect: float, gate_fit: str) -> float:
    """Compute focal length from vertical FOV and a filmback gate."""

    sensor_height = sensor_height_for_gate_fit(spec, aspect, gate_fit)
    fov = max(0.1, min(179.0, float(fov_deg)))
    return sensor_height / (2.0 * tan(radians(fov) * 0.5))
