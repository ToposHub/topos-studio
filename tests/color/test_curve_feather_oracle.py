"""P0-COL-05：Curve 逐点羽化的 label 解码回归（手算 oracle）。

缺陷：`_render_curve_feather_sdf` 把 `cv2.DIST_LABEL_PIXEL` 的 label 当成
线性坐标 (y*w+x) 解码，而 OpenCV 返回的是「零像素按行主序扫描的序号
（从 1 起）」。解码几乎永远匹配不到采样点，逐点 fi/fo 退化为全局
feather/2——不同控制点的羽化宽度全部一样。

场景（全部可手算）：
- sharp_mask = 左半填充：边界为 x=100 竖直线，内部 sdf=100-x、
  外部 sdf=x-100；
- 曲线 = 沿 x=0.5 的闭合直线段（上端点 fi=fo=0、下端点 fi=fo=25，
  把手与端点重合）。注意直线段的 bezier 参数化不均匀（smoothstep
  疏密），oracle 只依赖「参数 t 处 fi 线性插值」与「曲线像素自身的
  label-0 精确覆盖」两类无距离歧义的锚点；
- feather_scale = 4*(200/5)/100 = 1.6 px/单位。

锚点：下端点本身是零像素 (180,100)，fi=fo=25*1.6=40px（return 段
t=0 精确取端点值），其邻域 alpha 为纯 smoothstep 手算值。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.layer import MaskShape

W = H = 200
BOUNDARY_X = 100
FEATHER_SCALE = 4.0 * (W / 5.0) / 100.0  # 1.6 px / feather 单位


def _smoothstep(t):
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _left_half_mask() -> np.ndarray:
    m = np.zeros((H, W), dtype=np.float32)
    m[:, :BOUNDARY_X] = 1.0
    return m


def _vertical_boundary_curve(fi_top=0.0, fo_top=0.0,
                             fi_bottom=25.0, fo_bottom=25.0) -> MaskShape:
    shape = MaskShape(MaskShape.CURVE)
    shape.feather = 0  # 全局羽化关闭：非零羽化必须来自逐点 fi/fo
    shape.points = [
        {"pos": [0.5, 0.1], "c1": [0.5, 0.1], "c2": [0.5, 0.1],
         "fi": fi_top, "fo": fo_top},
        {"pos": [0.5, 0.9], "c1": [0.5, 0.9], "c2": [0.5, 0.9],
         "fi": fi_bottom, "fo": fo_bottom},
    ]
    return shape


def test_curve_pixel_alpha_matches_hand_oracle(image_engine):
    """零像素 (180,100)（下端点）处 fi=fo=40px 精确成立，邻域纯 smoothstep。

    修复前该处 fi/fo 解码失败、退化为全局 0（硬边）——alpha 只能是
    0/1，不可能给出这些中间值。
    """
    shape = _vertical_boundary_curve()
    alpha = image_engine._render_curve_feather_sdf(shape, _left_half_mask(), W, H)

    f = 25.0 * FEATHER_SCALE  # 40px：下端点 fi=fo 的像素值
    # sdf 定义（掩码填充 x≤99）：x=99→+1、x=100→-1、x=101→-2
    # 曲线像素 x=100：sdf=-1 → t=(f-1)/(2f)=0.4875
    assert float(alpha[180, BOUNDARY_X]) == pytest.approx(
        float(_smoothstep((f - 1.0) / (2.0 * f))), abs=1e-6)
    # 边界外 1px（x=101）：sdf=-2 → t=(f-2)/(2f)=0.475
    expected_out = float(_smoothstep((f - 2.0) / (2.0 * f)))
    assert float(alpha[180, BOUNDARY_X + 1]) == pytest.approx(
        expected_out, abs=1e-6)
    # 边界内 1px（x=99）：sdf=+1 → t=(f+1)/(2f)=0.5125
    expected_in = float(_smoothstep((f + 1.0) / (2.0 * f)))
    assert float(alpha[180, BOUNDARY_X - 1]) == pytest.approx(
        expected_in, abs=1e-6)


def test_per_point_feather_varies_along_curve(image_engine):
    """核心回归：fi=0 端保持硬边、fi=25 端显著柔化——逐点羽化真实生效。"""
    shape = _vertical_boundary_curve()
    alpha = image_engine._render_curve_feather_sdf(shape, _left_half_mask(), W, H)

    # 上端（fi=fo=0 附近）：无论最近采样是哪个端部采样（fi≤5.33px），
    # sdf=±20 都远超羽化宽度 → 硬边精确成立
    assert float(alpha[24, BOUNDARY_X - 20]) == pytest.approx(1.0, abs=1e-6), (
        "fi=0 端内侧应为硬边 alpha=1（label 解码回归会使此处被全局羽化污染）"
    )
    assert float(alpha[24, BOUNDARY_X + 20]) == pytest.approx(0.0, abs=1e-6), (
        "fi=0 端外侧应为硬边 alpha=0"
    )

    # 下端（fi=fo≈38.7px）：sdf=±20 处必须处于过渡带（修复前为硬边 1/0）
    soft_in = float(alpha[176, BOUNDARY_X - 20])
    soft_out = float(alpha[176, BOUNDARY_X + 20])
    assert 0.5 <= soft_in <= 0.93, (
        f"宽羽化端内侧 20px 应处于过渡带上半段，实际 {soft_in:.3f}"
    )
    assert 0.07 <= soft_out <= 0.5, (
        f"宽羽化端外侧 20px 应处于过渡带下半段，实际 {soft_out:.3f}"
    )

    # 两端行为可测地不同（缺陷信号：修复前后此处完全不同）
    assert (soft_in - soft_out) > 0.3, "下端应存在显著的羽化过渡"
    assert float(alpha[24, BOUNDARY_X - 20]) == 1.0 and soft_in < 0.95, (
        "上端硬边 vs 下端柔化必须同时成立"
    )


def test_all_points_without_fi_fo_falls_back_to_global(image_engine):
    """无逐点值时回退全局 feather/2：两端行为一致（契约保持）。"""
    shape = _vertical_boundary_curve(fi_top=None, fo_top=None,
                                     fi_bottom=None, fo_bottom=None)
    shape.feather = 20.0
    alpha = image_engine._render_curve_feather_sdf(shape, _left_half_mask(), W, H)

    f_px = (20.0 / 2.0) * FEATHER_SCALE  # 全局 feather/2 → 16px
    # 曲线像素 x=100：sdf=-1 → t=(16-1)/32=0.46875
    assert float(alpha[100, BOUNDARY_X]) == pytest.approx(
        float(_smoothstep((f_px - 1.0) / (2.0 * f_px))), abs=1e-6)
    # 中部采样 (100,100) 附近无距离歧义（相邻采样间距 ≥13px）：
    # 外侧 5px（x=105）：sdf=-6 → t=(16-6)/32
    expected = float(_smoothstep((f_px - 6.0) / (2.0 * f_px)))
    assert float(alpha[100, BOUNDARY_X + 5]) == pytest.approx(
        expected, abs=1e-6)
    # 两端同一偏移处行为一致（回退语义：逐点不存在 → 全局统一）
    assert float(alpha[24, BOUNDARY_X - 20]) == pytest.approx(1.0, abs=1e-6)
    assert float(alpha[176, BOUNDARY_X - 20]) == pytest.approx(1.0, abs=0.05)
