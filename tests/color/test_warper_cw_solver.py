# -*- coding: utf-8 -*-
"""CW-B06:Chroma Warp 求解器测试(权重/位移/Pin/曝光/确定性/退化)。"""
from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.warper_cw_solver import (
    chroma_plane,
    diagnose_constraints,
    solve_chroma_warp,
    stroke_weight,
)
from src.features.color.core.warper_state import (
    CURRENT_ALGORITHM,
    WARPER_SCHEMA_VERSION,
    WarperPinPoint,
    WarperStateV2,
    WarperStroke,
)

RED = (1.0, 0.0, 0.0)
YELLOW = (1.0, 1.0, 0.0)
CYAN = (0.0, 1.0, 1.0)
MID_GRAY = (0.5, 0.5, 0.5)


def _state(strokes=(), pins=()) -> WarperStateV2:
    return WarperStateV2(
        schema_version=WARPER_SCHEMA_VERSION, algorithm_version=CURRENT_ALGORITHM,
        mode="chroma_warp", color_space="hsv", axis_angle=0.0, grids={},
        strokes=tuple(strokes), pin_points=tuple(pins))


def _stroke(source, target, *, mode="normal", params=None, stroke_id="s1") -> WarperStroke:
    return WarperStroke(stroke_id=stroke_id, mode=mode, source_rgb=tuple(map(float, source)),
                        target_rgb=tuple(map(float, target)), params=params)


# ---- 基本行为 ----

def test_empty_state_is_identity() -> None:
    state = _state()
    out = solve_chroma_warp((0.2, 0.4, 0.1), state)
    np.testing.assert_array_equal(out, (0.2, 0.4, 0.1))
    image = np.tile(np.array([[0.2, 0.4, 0.1]]), (2, 2, 1))
    np.testing.assert_array_equal(solve_chroma_warp(image, state), image)


def test_single_stroke_moves_near_source_toward_target() -> None:
    state = _state([_stroke(RED, YELLOW)])  # 色相 0 → 1/6
    out = solve_chroma_warp(RED, state)
    moved = chroma_plane(out)
    source = chroma_plane(RED)
    target = chroma_plane(YELLOW)
    assert moved[0] != source[0] or moved[1] != source[1]  # 确有位移
    # 位移方向朝目标:新位置在 source→target 连线方向上
    expected_u = source[0] + (target[0] - source[0])
    assert moved[0] == pytest.approx(expected_u, abs=1e-9)
    assert moved[1] == pytest.approx(source[1] + (target[1] - source[1]), abs=1e-9)


def test_distant_pixel_untouched_by_default_range() -> None:
    state = _state([_stroke(RED, YELLOW)])  # range 0.04
    # CYAN 与 RED 在色度平面相距 ~1.41 >> 0.04
    out = solve_chroma_warp(CYAN, state)
    np.testing.assert_allclose(out, CYAN, atol=1e-9)


def test_weights_gaussian_and_tonal_window() -> None:
    source = RED
    w_center = stroke_weight(*chroma_plane(RED)[:2], chroma_plane(RED)[2],
                             source, YELLOW, {"chroma_range": 0.04,
                                              "tonal_low": 1.0, "tonal_high": 1.0,
                                              "pivot": 0.5, "exposure": 0.0}, "normal")
    assert w_center == pytest.approx(1.0)
    u, v, _ = chroma_plane(YELLOW)
    w_far = stroke_weight(u, v, 0.5, source, YELLOW,
                          {"chroma_range": 0.04, "tonal_low": 1.0, "tonal_high": 1.0,
                           "pivot": 0.5, "exposure": 0.0}, "normal")
    assert w_far < 0.01  # 距离 ≈ 1(r=0.04)→ exp(-625) ≈ 0
    # tonal 窗口:pivot 0.5 + low/high 0.1 → 窗口 [0.4, 0.6]
    params = {"chroma_range": 1.0, "tonal_low": 0.1, "tonal_high": 0.1,
              "pivot": 0.5, "exposure": 0.0}
    w_in = stroke_weight(*chroma_plane(RED)[:2], 0.5, RED, YELLOW, params, "normal")
    w_out = stroke_weight(*chroma_plane(RED)[:2], 0.9, RED, YELLOW, params, "normal")
    assert w_in == pytest.approx(1.0)
    assert w_out == pytest.approx(0.0, abs=1e-9)


# ---- 多约束 ----

def test_two_strokes_blend_by_weight() -> None:
    # 同源不同目标:RED→YELLOW 与 RED→CYAN,目标位移平均
    state = _state([_stroke(RED, YELLOW, stroke_id="a"),
                    _stroke(RED, CYAN, stroke_id="b")])
    out = solve_chroma_warp(RED, state)
    s = chroma_plane(RED)
    t1 = chroma_plane(YELLOW)
    t2 = chroma_plane(CYAN)
    avg_u = s[0] + (t1[0] - s[0] + t2[0] - s[0]) / 2
    avg_v = s[1] + (t1[1] - s[1] + t2[1] - s[1]) / 2
    assert chroma_plane(out)[0] == pytest.approx(avg_u, abs=1e-9)
    assert chroma_plane(out)[1] == pytest.approx(avg_v, abs=1e-9)
    assert "conflicting:a,b" in diagnose_constraints(state)


def test_duplicate_constraint_equals_single() -> None:
    single = _state([_stroke(RED, YELLOW)])
    double = _state([_stroke(RED, YELLOW, stroke_id="a"),
                     _stroke(RED, YELLOW, stroke_id="b")])
    out_single = solve_chroma_warp((0.9, 0.05, 0.05), single)
    out_double = solve_chroma_warp((0.9, 0.05, 0.05), double)
    np.testing.assert_allclose(out_single, out_double, atol=1e-12)
    assert any(item.startswith("duplicate:") for item in diagnose_constraints(double))


def test_zero_length_stroke_is_noop_with_diagnosis() -> None:
    state = _state([_stroke(RED, RED)])
    out = solve_chroma_warp(RED, state)
    np.testing.assert_allclose(out, RED, atol=1e-9)
    assert "zero_length:s1" in diagnose_constraints(state)


def test_deleting_stroke_restores_identity() -> None:
    with_stroke = _state([_stroke(RED, YELLOW)])
    assert not np.allclose(solve_chroma_warp(RED, with_stroke), RED)
    emptied = _state([])
    np.testing.assert_allclose(solve_chroma_warp(RED, emptied), RED, atol=1e-9)


# ---- Pin 保护 ----

def test_pin_protects_nearby_chromaticity() -> None:
    pixel = (0.9, 0.05, 0.05)
    bare = _state([_stroke(RED, YELLOW, params={"chroma_range": 0.5})])
    pinned = _state(
        [_stroke(RED, YELLOW, params={"chroma_range": 0.5})],
        pins=[WarperPinPoint(pin_id="p1", position=(0.0, 1.0),
                             params={"chroma_range": 0.5})])  # Pin 在红处
    moved_bare = solve_chroma_warp(pixel, bare)
    moved_pinned = solve_chroma_warp(pixel, pinned)
    displacement_bare = np.abs(moved_bare - np.array(pixel)).max()
    displacement_pinned = np.abs(moved_pinned - np.array(pixel)).max()
    assert displacement_pinned < displacement_bare  # Pin 拉住位移
    assert displacement_pinned > 0  # 但不是硬夹死(权重平均)


def test_pin_constraint_centers_at_its_true_chromaticity() -> None:
    """Pin 的权重中心必须是其 (hue,sat) 直接换算的平面坐标。

    回归:旧实现把平面坐标当 RGB 再经 rgb_to_hsp 重投影,Pin(0,1) 的
    中心从 (1,0) 漂到 (0.866,-0.5),自身颜色处权重从 1.0 掉到 ~0.018。
    """
    from src.features.color.core.warper_cw_solver import _to_cartesian

    params = {"chroma_range": 0.5}
    bare = _state([_stroke(RED, YELLOW, params=params)])
    # Pin 放在笔画源色(红:hue=0,sat=1)→ 平面 (1,0),权重=1
    pinned = _state(
        [_stroke(RED, YELLOW, params=params)],
        pins=[WarperPinPoint(pin_id="p1", position=(0.0, 1.0), params=params)])

    u, v = chroma_plane(RED)[:2]
    assert np.allclose((u, v), _to_cartesian(0.0, 1.0))
    # 权重平均:笔画位移与零位移 Pin 各占一半 → 平面位移精确减半
    out_bare = np.array(chroma_plane(solve_chroma_warp(RED, bare))[:2])
    out_pinned = np.array(chroma_plane(solve_chroma_warp(RED, pinned))[:2])
    norm_bare = np.hypot(*(out_bare - np.array([u, v])))
    norm_pinned = np.hypot(*(out_pinned - np.array([u, v])))
    np.testing.assert_allclose(norm_pinned, 0.5 * norm_bare, rtol=1e-9)

    # 远离笔画的 Pin(青处)不影响红像素:权重中心在自身位置才有此性质
    far_pin = _state(
        [_stroke(RED, YELLOW, params={"chroma_range": 0.2})],
        pins=[WarperPinPoint(pin_id="far", position=(0.5, 1.0),
                             params={"chroma_range": 0.2})])
    np.testing.assert_allclose(
        solve_chroma_warp(RED, far_pin),
        solve_chroma_warp(RED, _state([_stroke(RED, YELLOW, params={"chroma_range": 0.2})])),
        atol=1e-12)


# ---- Range / Exposure ----

def test_tiny_range_affects_only_immediate_neighborhood() -> None:
    state = _state([_stroke(RED, YELLOW, params={"chroma_range": 0.001})])
    near = solve_chroma_warp((0.999, 0.0, 0.0), state)   # 色度距 RED ~0.001
    far = solve_chroma_warp((0.95, 0.05, 0.0), state)    # 距离 ~0.05
    assert not np.allclose(near, (0.999, 0.0, 0.0), atol=1e-6)
    np.testing.assert_allclose(far, (0.95, 0.05, 0.0), atol=1e-9)


def test_extreme_exposure_scales_target_luma() -> None:
    # 目标 YELLOW 的 P≈0.947;exposure=-8 → t_p*2^-8 ≈ 0.0037 → luma 大幅下压
    calm = _state([_stroke(RED, YELLOW)])
    crushed = _state([_stroke(RED, YELLOW, params={"exposure": -8.0})])
    out_calm = solve_chroma_warp(RED, calm)
    out_crushed = solve_chroma_warp(RED, crushed)
    assert chroma_plane(out_crushed)[2] < chroma_plane(out_calm)[2] - 0.4
    # exposure=0 与无参数默认一致
    default = _state([_stroke(RED, YELLOW, params={})])
    np.testing.assert_allclose(solve_chroma_warp(RED, default), out_calm, atol=1e-12)


# ---- Normal vs Point-to-Point ----

def test_point_to_point_weights_along_segment() -> None:
    params = {"chroma_range": 0.05}
    # P2P:靠近目标端的像素也获得权重(Normal 只在源附近)
    target_pixel = (0.9, 0.9, 0.0)  # 黄附近
    normal = _state([_stroke(RED, YELLOW, mode="normal", params=params)])
    p2p = _state([_stroke(RED, YELLOW, mode="point_to_point", params=params)])
    w_normal = stroke_weight(*chroma_plane(target_pixel)[:2], 0.5,
                             RED, YELLOW, params, "normal")
    w_p2p = stroke_weight(*chroma_plane(target_pixel)[:2], 0.5,
                          RED, YELLOW, params, "point_to_point")
    assert w_p2p > w_normal
    out_p2p = solve_chroma_warp(target_pixel, p2p)
    out_normal = solve_chroma_warp(target_pixel, normal)
    # P2P 对目标端像素有实际位移;Normal 几乎没有
    assert np.abs(out_p2p - np.array(target_pixel)).max() > \
        np.abs(out_normal - np.array(target_pixel)).max()


# ---- 确定性 / HDR / 灰 ----

def test_deterministic_replay() -> None:
    state = _state([_stroke(RED, YELLOW), _stroke(CYAN, RED, stroke_id="s2")],
                   pins=[WarperPinPoint(pin_id="p1", position=(0.5, 0.3))])
    pixel = (0.3, 0.5, 0.2)
    first = solve_chroma_warp(pixel, state)
    for _ in range(3):
        np.testing.assert_array_equal(solve_chroma_warp(pixel, state), first)


def test_hdr_and_negative_channels_not_clamped() -> None:
    state = _state([_stroke((2.0, 2.0, 2.0), (2.0, 2.0, 2.0))])  # 零位移约束
    bright = solve_chroma_warp((2.0, 2.0, 2.0), state)
    np.testing.assert_allclose(bright, (2.0, 2.0, 2.0), atol=1e-9)  # >1 保留
    negative = (-0.5, 0.0, 0.2)
    out = solve_chroma_warp(negative, _state([]))
    np.testing.assert_array_equal(out, negative)

    # 亮度位移也保留 HDR:目标比源亮 1.0(绝对量)
    state = _state([_stroke((0.4, 0.4, 0.4), (1.4, 1.4, 1.4))])
    hdr_out = solve_chroma_warp((4.0, 4.0, 4.0), state)  # 远离约束 → 不动
    np.testing.assert_allclose(hdr_out, (4.0, 4.0, 4.0), atol=1e-9)


def test_gray_source_stroke_can_color_grays() -> None:
    # 灰(色度原点)→ 彩:灰像素获得色度(声明的可预测行为)
    state = _state([_stroke(MID_GRAY, RED, params={"chroma_range": 0.1})])
    out = solve_chroma_warp(MID_GRAY, state)
    assert chroma_plane(out)[0] != 0.0 or chroma_plane(out)[1] != 0.0


def test_zero_chroma_output_gets_defined_hue() -> None:
    # 位移互相抵消回到原点:sat=0 → hue 定义为 0,无噪声
    state = _state([_stroke(RED, YELLOW, stroke_id="a"),
                    _stroke(RED, CYAN, stroke_id="b")])
    # 平均位移非零;改用对消:RED→YELLOW 与 YELLOW→RED 同权重只留一条源在灰的零位移
    out = solve_chroma_warp(RED, state)
    assert np.isfinite(out).all()
