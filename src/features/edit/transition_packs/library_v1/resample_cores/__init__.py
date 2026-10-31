"""LIB1 双源重采样/复合转场生产核包（L14/L15 批）。

每家族独立子模块（自包含 helpers，零改名零合并——避免跨家族
同名 helper 冲突）；暂存区 *_core.py 再导出本包对应子模块
（单一事实源，禁双份漂移）。统一签名 evaluate(a, b, progress, params)。
"""

from __future__ import annotations

from . import x04_staggered_tiles  # noqa: F401
from . import x10_hinged_doors  # noqa: F401
from . import x11_cube_faces  # noqa: F401
from . import x12_page_curl  # noqa: F401
from . import x13_pixelate_bridge  # noqa: F401
from . import x14_displacement_bridge  # noqa: F401
from . import x15_ripple_refraction  # noqa: F401
from . import x16_scanline_slices  # noqa: F401
from . import x17_kaleido_bridge  # noqa: F401
from . import x18_burn_reveal  # noqa: F401

def composite_lib1_transition(transition_type, a_img, b_img, progress,
                              params=None):
    """LIB1 双源复合转场统一分派（L14/L15）。

    未知类型 ValueError 精确拒绝（禁透传冒充成功的红线同 L04 dispatch）。
    ``transition_type`` 应传解析后的 base 类型（调用方负责 user_* 解析）。
    """
    from src.features.edit.transition_packs.library_v1.resample_cores import (
        x04_staggered_tiles, x10_hinged_doors, x11_cube_faces,
        x12_page_curl, x13_pixelate_bridge, x14_displacement_bridge,
        x15_ripple_refraction, x16_scanline_slices, x17_kaleido_bridge,
        x18_burn_reveal,
    )
    key = str(transition_type or "")
    if key == "lib1_true_push":
        from src.features.edit.transition_packs.library_v1.cores import (
            push_frame,
        )
        p = dict(params or {})
        return push_frame(a_img, b_img, float(progress),
                          str(p.get("direction", "left")),
                          float(p.get("seam_softness", 0.0)))
    table = {
        "lib1_staggered_tiles": x04_staggered_tiles,
        "lib1_hinged_doors": x10_hinged_doors,
        "lib1_cube_faces": x11_cube_faces,
        "lib1_page_curl": x12_page_curl,
        "lib1_pixelate_bridge": x13_pixelate_bridge,
        "lib1_displacement_bridge": x14_displacement_bridge,
        "lib1_ripple_refraction": x15_ripple_refraction,
        "lib1_scanline_slices": x16_scanline_slices,
        "lib1_kaleido_bridge": x17_kaleido_bridge,
        "lib1_burn_reveal": x18_burn_reveal,
    }
    mod = table.get(key)
    if mod is None:
        raise ValueError(f"未知 LIB1 复合转场类型: {transition_type!r}")
    return mod.evaluate(a_img, b_img, float(progress), params)


__all__ = ["composite_lib1_transition", "x04_staggered_tiles", "x10_hinged_doors", "x11_cube_faces", "x12_page_curl", "x13_pixelate_bridge", "x14_displacement_bridge", "x15_ripple_refraction", "x16_scanline_slices", "x17_kaleido_bridge", "x18_burn_reveal"]
