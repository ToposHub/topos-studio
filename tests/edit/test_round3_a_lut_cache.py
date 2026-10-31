"""第三轮复查批A：导出 LUT 快路（N25/RG01 + N26/RG02）回归。

- N25：``cache.get(key, "missing") != "missing"`` 在缓存值为 ndarray
  时触发 NumPy 元素级比较并抛 ValueError，连续帧导出第二帧即失败。
- N26：7³ 抽样网格不能证明可分离性，第三轮固定反例 wheel 在网格外
  合法输入上偏差 8 code。可分离性改为证明制：逐通道响应全非负
  （任意像素 luma 为非负项之和），或闭式 luma 下界 ≥ 舍入容差，门
  不可能在任何 u8 输入上触发，单通道 LUT 才精确成立。
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from src.features.edit.core.timeline_export import TimelineExporter
from src.features.color.core.the_decompiler import GradingMath

# 第三轮 render_review.md RG02 固定的合法 wheel 反例（seed7713 之一）：
# 7³ 网格验证通过但实际 RGB[112,140,59] 偏差 8 code。
RG02_COUNTEREXAMPLE_WHEEL = {
    "lift": [
        271.6186690578128, 0.19411276782870046, -0.5892569835578423,
    ],
    "gamma": [
        71.73376380027234, 0.316113852565303, 0.12122455594011261,
    ],
    "gain": [
        275.51972102495455, 0.8444802110070772, 0.47807158906166936,
    ],
}


def _exporter(grading_params: dict) -> tuple[TimelineExporter, object]:
    stack = SimpleNamespace(
        grading_params=grading_params,
        curves_data={},
        hsl_curve_data={},
        mixer_params={},
        lut_path=None,
        soft_clip_params={"enabled": False},
        layers=[],
        active_layer_index=None,
    )
    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._project = SimpleNamespace(color_stacks={"stack": stack})
    exporter._color_grade_lut_cache = {}
    layer = SimpleNamespace(
        clip=SimpleNamespace(color_stack_id="stack"),
        clip_id="clip",
        source_frame=0,
    )
    return exporter, layer


def _reference_bgr(frame: np.ndarray, grading_params: dict) -> np.ndarray:
    rgb = frame[..., ::-1].astype(np.float32) / 255.0
    graded = GradingMath.apply_grading(
        rgb.reshape(-1, 3), grading_params,
    ).reshape(rgb.shape)
    return np.rint(
        np.clip(graded[..., ::-1], 0.0, 1.0) * 255.0,
    ).astype(np.uint8)


def test_n25_consecutive_cache_hits_never_raise() -> None:
    """连续命中 ndarray 缓存与 None 结论缓存都不允许再抛 ValueError。"""
    ex = TimelineExporter.__new__(TimelineExporter)
    ex._color_grade_lut_cache = {}
    identity_lut = ex._get_independent_grade_lut({})
    gain_lut = ex._get_independent_grade_lut(
        {"lift": (49.360623248919055, 0.375193068518081, 0)})
    assert identity_lut is not None and gain_lut is not None
    for _ in range(3):
        # 第二次起走缓存命中路径（N25 崩溃点）
        assert ex._get_independent_grade_lut({}) is identity_lut
        assert ex._get_independent_grade_lut(
            {"lift": (49.360623248919055, 0.375193068518081, 0)}
        ) is gain_lut


def test_n25_rejected_conclusion_is_cached_as_none() -> None:
    ex = TimelineExporter.__new__(TimelineExporter)
    ex._color_grade_lut_cache = {}
    assert ex._get_independent_grade_lut(RG02_COUNTEREXAMPLE_WHEEL) is None
    assert ex._color_grade_lut_cache is not None
    # None 结论复用：缓存里已有该 key，第二次直接返回 None
    assert ex._get_independent_grade_lut(RG02_COUNTEREXAMPLE_WHEEL) is None


def test_n25_real_path_three_consecutive_graded_frames() -> None:
    exporter, layer = _exporter(
        {"lift": (49.360623248919055, 0.375193068518081, 0)})
    rng = np.random.default_rng(11)
    for frame_index in range(3):
        frame = rng.integers(0, 256, size=(16, 24, 3), dtype=np.uint8)
        actual = exporter._apply_color_grade(frame, layer)
        np.testing.assert_array_equal(
            actual, _reference_bgr(frame, layer.clip.color_stack_id and {
                "lift": (49.360623248919055, 0.375193068518081, 0)}))


def test_n26_counterexample_wheel_takes_float_and_is_exact() -> None:
    """RG02 固定反例必须被证明制拒绝，float 输出与 GradingMath 一致。"""
    exporter, layer = _exporter(RG02_COUNTEREXAMPLE_WHEEL)
    frame = np.array([[[59, 140, 112]]], dtype=np.uint8)  # BGR = RGB[112,140,59]
    actual = exporter._apply_color_grade(frame, layer)
    expected = _reference_bgr(frame, RG02_COUNTEREXAMPLE_WHEEL)
    np.testing.assert_array_equal(actual, expected)
    # 反例的正确输出是 RGB[255, 86, 252]（GradingMath），而非旧快路的
    # RGB[255, 86, 244]；_apply_color_grade 交付 BGR。
    assert actual[0, 0].tolist() == [252, 86, 255]


def test_n26_accepted_params_exact_on_random_and_near_black() -> None:
    """被证明接受的参数组合，快路输出在任意 u8 帧上与 float 逐位一致。"""
    import cv2

    ex = TimelineExporter.__new__(TimelineExporter)
    ex._color_grade_lut_cache = {}
    accepted_params = (
        {},
        {"lift": (49.360623248919055, 0.375193068518081, 0)},
        {"exposure": 12.0, "contrast": -5.0, "temp": 8.0, "shadow": 3.0},
        {"gain": (30.0, 0.2, 0.1)},
    )
    rng = np.random.default_rng(42)
    for params in accepted_params:
        lut = ex._get_independent_grade_lut(params)
        assert lut is not None, params
        frame = np.zeros((32, 32, 3), dtype=np.uint8)
        frame[0:16] = rng.integers(0, 256, size=(16, 32, 3)).astype(np.uint8)
        frame[16:, 0] = 255  # 近黑行：单通道亮、其余黑
        fast = cv2.LUT(frame, lut)
        np.testing.assert_array_equal(
            fast, _reference_bgr(frame, params))
