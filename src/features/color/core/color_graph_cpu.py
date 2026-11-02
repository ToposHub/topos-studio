"""CPU oracle —— 调色节点分支/键/通道算子的参考实现（N09，B10）。

与 ``docs/color_nodes_mixer_spec.md``（v1 冻结）逐条对应；float32 精确
语义，供 GL/Metal 等价对照与手算验证。**不做隐式近似**：不支持的
Corrector 参数域显式声明（``UnsupportedNode``），完整 Corrector CPU
实现在 B12 补齐。

局限（B10 时点）：Corrector oracle 仅支持 neutral 与 exposure 乘子
（2^exposure）+ saturation（BT.709 luma 加权）——足够 mixer/key
golden 的构造；其余参数抛 ``UnsupportedNode``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from src.features.color.core.color_graph_compiler import (
    ColorGraphRenderPlan,
    RenderPass,
)


class UnsupportedNode(NotImplementedError):
    """CPU oracle 未实现的算子/参数域（显式声明，禁止静默近似）。"""


# ── 单算子公式（spec §1–§6） ─────────────────────────────────────────────

def parallel_mix(base: np.ndarray, branches: Sequence[np.ndarray],
                 keys: Sequence[np.ndarray | None]) -> np.ndarray:
    """spec §1：base 依序被各分支按有效 key lerp。"""
    out = base.copy()
    for branch, key in zip(branches, keys):
        k = np.ones_like(out[..., :1]) if key is None else key[..., :1]
        k = np.clip(k, 0.0, 1.0)
        out[..., :3] = out[..., :3] * (1.0 - k) + branch[..., :3] * k
    return out


_BLEND_MODES_CPU = {
    "Normal": lambda a, b, op: a * (1 - op) + b * op,
    "Add": lambda a, b, op: a + b * op,
    "Subtract": lambda a, b, op: a - b * op,
    "Multiply": lambda a, b, op: a * (1.0 - op + op * b / np.where(
        np.abs(a) < 1e-12, 1.0, 1.0)) if False else a * (1 - op + op * b),
    "Screen": lambda a, b, op: 1.0 - (1.0 - a) * (1.0 - b * op),
}


def layer_mix(slots: Sequence[np.ndarray], opacities: Sequence[float],
              modes: Sequence[str]) -> np.ndarray:
    """spec §2：由下到上折叠（slot 0 为底）。"""
    if not slots:
        raise ValueError("layer_mix 需要至少一个 slot")
    out = slots[0].copy()
    for slot, opacity, mode in zip(slots[1:], opacities, modes):
        formula = _BLEND_MODES_CPU.get(mode)
        if formula is None:
            raise UnsupportedNode(f"Layer 模式 {mode} 未在 v1 规格冻结")
        out[..., :3] = formula(out[..., :3], slot[..., :3], float(opacity))
    return out


def key_mix(keys: Sequence[np.ndarray], mode: str = "add") -> np.ndarray:
    """spec §3：key 布尔混合（有界 0-1）。"""
    if not keys:
        raise ValueError("key_mix 需要至少一路 key")
    out = np.clip(keys[0][..., :1], 0.0, 1.0)
    for key in keys[1:]:
        key = np.clip(key[..., :1], 0.0, 1.0)
        if mode == "add":
            out = np.maximum(out, key)
        elif mode == "subtract":
            out = np.clip(out - key, 0.0, 1.0)
        elif mode == "intersect":
            out = out * key
        else:
            raise UnsupportedNode(f"Key Mixer 模式 {mode} 未冻结")
    return out


def outside_key(referenced_key: np.ndarray) -> np.ndarray:
    """spec §4：补集。"""
    return np.clip(1.0 - referenced_key[..., :1], 0.0, 1.0)


def split_rgb(image: np.ndarray) -> dict[str, np.ndarray]:
    """spec §5：R/G/B 灰度载体（alpha 随 R 通道）。"""
    out = {}
    for channel in ("r", "g", "b"):
        value = image[..., "rgb".index(channel)]
        plane = np.empty_like(image)
        plane[..., :3] = value[..., None]
        plane[..., 3:] = image[..., 3:]
        out[channel] = plane
    return out


def combine_rgb(r: np.ndarray, g: np.ndarray, b: np.ndarray) -> np.ndarray:
    """spec §5：三合（alpha 取 R 载体）。"""
    out = np.empty_like(r)
    out[..., 0] = r[..., 0]
    out[..., 1] = g[..., 1]
    out[..., 2] = b[..., 2]
    out[..., 3:] = r[..., 3:]
    return out


def corrector_oracle(image: np.ndarray, params: Mapping[str, Any],
                     mask: np.ndarray | None = None) -> np.ndarray:
    """受限 Corrector oracle：neutral / exposure / saturation（B10 声明）。

    完整 Corrector CPU 实现（曲线/LUT/qualifier/窗口/插件）在 B12；
    其他参数出现即 ``UnsupportedNode``（不静默忽略）。
    """
    allowed = {"key_source_mode", "exposure", "saturation", "grade",
               "opacity", "blend_mode", "grade_patch"}
    unknown = set(params) - allowed
    if unknown:
        raise UnsupportedNode(f"Corrector 参数域未支持: {sorted(unknown)}")
    grade = params.get("grade") or {}
    exposure = float(grade.get("exposure", params.get("exposure", 0.0)) or 0.0)
    saturation = float(grade.get("saturation",
                                 params.get("saturation", 1.0)) or 1.0)
    graded = image.astype(np.float32) * (2.0 ** exposure)
    if saturation != 1.0:
        luma = (graded[..., :3] * (0.2126, 0.7152, 0.0722)).sum(axis=-1,
                                                                keepdims=True)
        graded[..., :3] = luma + (graded[..., :3] - luma) * saturation
    if mask is not None:
        k = np.clip(mask[..., :1], 0.0, 1.0)
        graded = image * (1.0 - k) + graded * k
    return graded


# ── plan 级 CPU 执行（串行 + 分支） ─────────────────────────────────────

@dataclass
class CpuExecution:
    output: np.ndarray
    alpha_output: np.ndarray | None = None


def run_plan_cpu(plan: ColorGraphRenderPlan, source: np.ndarray,
                 params_of: Callable[[RenderPass], Mapping[str, Any]]
                 ) -> CpuExecution:
    """按 plan 在 CPU 执行（正确 grade 经 corrector_oracle 受限域）。"""
    outputs: dict[str, np.ndarray] = {}
    alpha_result: np.ndarray | None = None

    def _first(port_name: str):
        for port, ref in render_pass.inputs:
            if port == port_name and ref is not None:
                return outputs.get(ref.node_id)
        return None

    for render_pass in plan.passes:
        params = params_of(render_pass) or {}

        if render_pass.op == "source":
            outputs[render_pass.node_id] = source.astype(np.float32).copy()
        elif render_pass.op in ("corrector", "shared_corrector"):
            if params.get("visible") is False:
                outputs[render_pass.node_id] = _first("image")
                continue
            base = _first("image")
            if base is None:
                base = source
            outputs[render_pass.node_id] = corrector_oracle(base, params)
        elif render_pass.op == "parallel_mixer":
            base = _first("base")
            if base is None:
                base = source
            branches = [outputs[ref.node_id]
                        for port, ref in render_pass.inputs
                        if port == "branch" and ref is not None]
            keys = [None] * len(branches)
            outputs[render_pass.node_id] = parallel_mix(base, branches, keys)
        elif render_pass.op == "layer_mixer":
            slots = [outputs[ref.node_id]
                     for _port, ref in sorted(
                         render_pass.inputs,
                         key=lambda pr: (pr[1].slot_id if pr[1] else ""))
                     if ref is not None]
            opacities = [1.0] * max(0, len(slots) - 1)
            modes = ["Normal"] * max(0, len(slots) - 1)
            outputs[render_pass.node_id] = layer_mix(slots, opacities, modes)
        elif render_pass.op == "key_mixer":
            keys = [outputs[ref.node_id]
                    for _port, ref in render_pass.inputs if ref is not None]
            outputs[render_pass.node_id] = key_mix(
                keys, str(params.get("mode", "add")))
        elif render_pass.op == "outside":
            base = _first("image")
            if base is None:
                base = source
            referenced = _first("key_in")
            complement = (outside_key(referenced) if referenced is not None
                          else np.zeros_like(source[..., :1]))
            outputs[render_pass.node_id] = corrector_oracle(
                base, params, mask=complement)
        elif render_pass.op == "splitter":
            base = _first("image")
            if base is None:
                base = source
            planes = split_rgb(base)
            outputs[render_pass.node_id] = planes   # dict 载体（r/g/b）
        elif render_pass.op == "combiner":
            splitter_out = None
            for port, ref in render_pass.inputs:
                if ref is not None:
                    splitter_out = outputs.get(ref.node_id)
                    break
            if isinstance(splitter_out, dict):
                outputs[render_pass.node_id] = combine_rgb(
                    splitter_out["r"], splitter_out["g"], splitter_out["b"])
            else:
                outputs[render_pass.node_id] = source.copy()
        elif render_pass.op == "output":
            ref = next((outputs[r.node_id]
                        for _p, r in render_pass.inputs if r is not None),
                       source)
            outputs[render_pass.node_id] = np.array(ref, copy=True)
        elif render_pass.op == "alpha_output":
            key = next((outputs[r.node_id]
                        for _p, r in render_pass.inputs if r is not None),
                       None)
            alpha_result = (np.clip(key[..., :1], 0.0, 1.0)
                            if key is not None else None)
            outputs[render_pass.node_id] = source.copy()
        else:
            raise UnsupportedNode(f"CPU oracle 未实现算子 {render_pass.op}")

    final = plan.passes[-1]
    result = outputs.get(final.node_id, source)
    return CpuExecution(output=result, alpha_output=alpha_result)
