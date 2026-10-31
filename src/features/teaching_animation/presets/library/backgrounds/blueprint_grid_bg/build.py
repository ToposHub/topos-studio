"""蓝图网格背景 — blueprint grid with a slow scanning highlight."""

from src.features.teaching_animation.presets.builders import (
    key, linear_gradient, scene_document, shape_element, text_element, track,
)

PRESET = {
    "preset_id": "blueprint_grid_bg",
    "name": "蓝图网格背景",
    "name_i18n": {"en_US": "Blueprint Grid Background"},
    "category": "backgrounds",
    "description": "深蓝底细网格 + 缓慢垂直扫描光带，科技感演示背景",
    "description_i18n": {"en_US": "Deep-blue fine grid with a slow vertical scan band for tech presentations"},
    "tags": ["background", "grid", "blueprint", "背景", "网格"],
}


def build() -> dict:
    elements = [
        shape_element("bg", 0, 0, 1920, 1080, kind="rect", z=0,
                      fill="#0a1a33",
                      gradient=linear_gradient((0.0, "#0a1a33"), (1.0, "#050d1f"), angle=90.0)),
    ]
    z = 1
    for index in range(1, 8):
        x = index * 240
        elements.append(shape_element(f"grid_v{index}", x, 0, 2, 1080, kind="rect",
                                      z=z, fill="#123055", fill_opacity=0.8))
        z += 1
    # AUD-28501：纵向 8 格（步长 240）全覆盖宽度，横向步长 180 对应
    # 1080/180 = 6 格需 5 条内线——历史上只画 4 条（缺 y=900），下半部
    # 无横线、与纵向覆盖密度不对称。
    for index in range(1, 6):
        y = index * 180
        elements.append(shape_element(f"grid_h{index}", 0, y, 1920, 2, kind="rect",
                                      z=z, fill="#123055", fill_opacity=0.8))
        z += 1
    elements.extend([
        shape_element("frame", 60, 60, 1800, 960, kind="rect", z=z,
                      fill="#0a1a33", fill_opacity=0.0,
                      stroke="#2f6fb2", stroke_width=3.0),
        shape_element("scan_beam", 60, 80, 1800, 180, kind="rect", z=z + 1,
                      fill="#3b82f6", fill_opacity=0.12,
                      gradient=linear_gradient((0.0, "#3b82f6"), (1.0, "#3b82f6"),
                                               opacities=(0.05, 0.22)),
                      tracks=(track("y", key(0, 80.0), key(72, 820.0, "ease_in_out")),)),
        text_element("corner_mark", 84, 78, "TOPOS · BLUEPRINT 1920×1080",
                     size=22, color="#5b93d1", z=z + 2, family="monospace"),
    ])
    return scene_document(elements, name="Blueprint Grid Background",
                          duration_frames=72).to_dict()
