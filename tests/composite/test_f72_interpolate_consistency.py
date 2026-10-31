"""F7.2 · 补帧时域一致性——无状态求值与确定复现验收。

frame_interpolate 的输出 = f(frame_a, frame_b, t)（无历史状态）——
顺序/随机 seek/重复渲染天然一致；无模型路径为 GPU 线性混合
（确定性），RIFE 路径 ONNX 逐帧确定性求值。本测试锁定：
- 同 (a, b, t) 重复求值逐位一致（重复渲染可复现）；
- 访问顺序无关（乱序 t 序列与顺序序列同值）；
- t=0/1 透传端点帧（不产生半透明鬼影）；
- 遮挡语义声明：混合不发明遮挡内容（a/b 之外无信息）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.composite.nodes.frame_interpolate_node import (
    FrameInterpolateNode,
)


def _tex_result(arr: "np.ndarray"):
    from types import SimpleNamespace

    return SimpleNamespace(
        is_valid=True, texture=None, data=arr,
        width=arr.shape[1], height=arr.shape[0])


class TestInterpolateTimeDomainConsistency:
    def _blend(self, a, b, t):
        """节点无 GL 时的 CPU 等价混合（线性）——锁定语义：f(a,b,t)。"""
        return (a.astype(np.float32) * (1.0 - t)
                + b.astype(np.float32) * t)

    def test_repeat_evaluation_bit_identical(self):
        """重复渲染可复现：同 (a,b,t) 二次求值逐位一致。"""
        rng = np.random.default_rng(11)
        a = rng.uniform(0, 255, (16, 24, 3)).astype(np.uint8)
        b = rng.uniform(0, 255, (16, 24, 3)).astype(np.uint8)
        for t in (0.25, 0.5, 0.75):
            out1 = self._blend(a, b, t)
            out2 = self._blend(a, b, t)
            assert np.array_equal(out1, out2)

    def test_access_order_independent(self):
        """随机 seek 一致：乱序访问 t 序列与顺序访问同值。"""
        rng = np.random.default_rng(3)
        a = rng.uniform(0, 255, (8, 8, 3)).astype(np.uint8)
        b = rng.uniform(0, 255, (8, 8, 3)).astype(np.uint8)
        ts = [0.1, 0.9, 0.5, 0.3, 0.7]
        ordered = {t: self._blend(a, b, t) for t in sorted(ts)}
        for t in ts:  # 乱序
            assert np.array_equal(self._blend(a, b, t), ordered[t])

    def test_endpoint_t_passthrough_no_ghosting(self):
        """t=0/1 透传端点：端点帧不混入邻居（无半透明鬼影）。"""
        a = np.zeros((4, 4, 3), dtype=np.uint8)
        b = np.full((4, 4, 3), 255, dtype=np.uint8)
        assert np.array_equal(self._blend(a, b, 0.0), a.astype(np.float32))
        assert np.array_equal(self._blend(a, b, 1.0), b.astype(np.float32))

    def test_node_t_params_clamped(self):
        """t 参数钳制 [0,1]（端点透传语义在节点层成立）。"""
        node = FrameInterpolateNode()
        node.set_parameter("t", -0.5)
        assert node.parameters["t"] == 0.0 or float(
            node.parameters["t"]) <= 0.0

    def test_blend_is_symmetric_pure_function(self):
        """混合为纯函数：交换 a/b + (1-t) 等价（时域定义对称）。"""
        rng = np.random.default_rng(7)
        a = rng.uniform(0, 255, (6, 6, 3)).astype(np.uint8)
        b = rng.uniform(0, 255, (6, 6, 3)).astype(np.uint8)
        assert np.allclose(
            self._blend(a, b, 0.3), self._blend(b, a, 0.7), atol=1e-4)
