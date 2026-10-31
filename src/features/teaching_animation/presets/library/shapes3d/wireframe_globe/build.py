"""线框地球仪 — breathing meridians inside an outlined sphere."""

from src.features.teaching_animation.presets.builders import key, scene_document, shape_element, track

PRESET = {
    "preset_id": "wireframe_globe",
    "name": "线框地球仪",
    "name_i18n": {"en_US": "Wireframe Globe"},
    "category": "shapes3d",
    "description": "圆轮廓 + 三条经线椭圆呼吸开合 + 赤道纬线，缓慢自转错觉",
    "description_i18n": {"en_US": "Circle outline with three breathing meridian ellipses and an equator line"},
    "tags": ["3d", "wireframe", "globe", "shape", "3D", "地球"],
}

_RADIUS = 330.0
_CENTER = (960.0, 500.0)


def _meridian(width: float, z: int, phase: int):
    cx, cy = _CENTER
    tracks = (
        track("scale_x", key(0, 1.0), key(phase + 18, 0.22, "ease_in_out"),
              key(phase + 36, 1.0, "ease_in_out"), key(72, 1.0)),
    )
    return shape_element(f"meridian_{z}", cx - width / 2.0, cy - _RADIUS,
                         width, _RADIUS * 2.0, kind="ellipse", z=z,
                         fill="#0b1122", fill_opacity=0.0, stroke="#38bdf8",
                         stroke_width=4.0, tracks=tracks)


def build() -> dict:
    cx, cy = _CENTER
    return scene_document([
        shape_element("sphere", cx - _RADIUS, cy - _RADIUS, _RADIUS * 2.0, _RADIUS * 2.0,
                      kind="circle", z=1, fill="#0b1122", fill_opacity=0.35,
                      stroke="#7dd3fc", stroke_width=6.0),
        _meridian(_RADIUS * 2.0, 2, 0),
        _meridian(_RADIUS * 1.32, 3, 10),
        _meridian(_RADIUS * 0.62, 4, 20),
        shape_element("equator", cx - _RADIUS, cy - _RADIUS * 0.26,
                      _RADIUS * 2.0, _RADIUS * 0.52, kind="ellipse", z=5,
                      fill="#0b1122", fill_opacity=0.0, stroke="#38bdf8", stroke_width=4.0),
        shape_element("tropic", cx - _RADIUS * 0.82, cy - _RADIUS * 0.62,
                      _RADIUS * 1.64, _RADIUS * 0.4, kind="ellipse", z=6,
                      fill="#0b1122", fill_opacity=0.0, stroke="#1e5f8a", stroke_width=3.0),
        shape_element("axis", cx - 2.0, cy - _RADIUS - 70.0, 4.0, _RADIUS * 2.0 + 140.0,
                      kind="rect", z=0, fill="#1e3a5f",
                      tracks=(track("opacity", key(0, 0.4), key(36, 0.8), key(72, 0.4)))),
    ], name="Wireframe Globe", duration_frames=72).to_dict()
