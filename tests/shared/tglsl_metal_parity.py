"""GL 参考值 vs Metal 读回的对拍支撑（阶段 6.2 / 6.3 测试与证据共用，不被收集）。

参考值：真实的 ``dctl_wrapper.wrap_dctl_process`` + ``EffectPassRunner`` 的顶点
着色器与全屏 quad，在 standalone GL 3.3 上下文上渲染（即生产 GL 路径的同一段
着色器）。被测值：转译器生成的 MSL 经 ``MetalEffectRuntime`` 在真实设备上
dispatch 后读回。两侧输入都是同一份按像素格式量化后的纹理。

坐标：GL 的 ``v_uv`` 原点落在 numpy 第 0 行，与 ABI 的 ``gl_row0_is_v0`` 约定
逐像素同构，因此两边数组可以直接按下标比较。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

_SHADABLE = ("slider", "checkbox", "color", "select")

#: 对拍尺寸：非方形、1xN、Nx1、较大偶数、仅差一行的近似方形。
SIZES: Tuple[Tuple[int, int], ...] = ((19, 13), (7, 1), (1, 7), (64, 48), (33, 32))

RANDOM_COMBINATIONS = 4


def default_value(param: Dict[str, Any]) -> Any:
    if param["type"] == "color":
        return tuple(float(v) for v in param["default"])
    return param["default"]


def param_sets(params: Sequence[Dict[str, Any]], *, seed: int = 20261123) -> List[Tuple[str, Dict[str, Any]]]:
    """每个参数取端点 + 默认 + 固定种子的随机组合；返回 (标签, 用户侧取值)。"""
    usable = [p for p in params if p.get("type") in _SHADABLE and p.get("var_name")]
    base = {p["var_name"]: default_value(p) for p in usable}
    sets: List[Tuple[str, Dict[str, Any]]] = [("default", dict(base))]
    for p in usable:
        name, kind = p["var_name"], p["type"]
        if kind == "slider":
            for label, value in (("min", p["min"]), ("max", p["max"])):
                candidate = dict(base)
                candidate[name] = float(value)
                sets.append((f"{name}={label}", candidate))
        elif kind == "checkbox":
            for value in (0, 1):
                candidate = dict(base)
                candidate[name] = value
                sets.append((f"{name}={value}", candidate))
        elif kind == "select":
            for index in range(len(p.get("options") or ())):
                candidate = dict(base)
                candidate[name] = index
                sets.append((f"{name}={index}", candidate))
        elif kind == "color":
            for label, value in (("0", (0.0, 0.0, 0.0)), ("1", (1.0, 1.0, 1.0))):
                candidate = dict(base)
                candidate[name] = value
                sets.append((f"{name}={label}", candidate))
    rng = np.random.default_rng(seed)
    for index in range(RANDOM_COMBINATIONS):
        candidate = dict(base)
        for p in usable:
            name, kind = p["var_name"], p["type"]
            if kind == "slider":
                candidate[name] = float(rng.uniform(p["min"], p["max"]))
            elif kind == "checkbox":
                candidate[name] = int(rng.integers(0, 2))
            elif kind == "select":
                candidate[name] = int(rng.integers(0, max(1, len(p.get("options") or ()))))
            elif kind == "color":
                candidate[name] = tuple(float(v) for v in rng.uniform(0.0, 1.0, 3))
        sets.append((f"random{index}", candidate))
    unique: List[Tuple[str, Dict[str, Any]]] = []
    seen = set()
    for label, values in sets:
        key = tuple(sorted((k, tuple(v) if isinstance(v, (list, tuple)) else v) for k, v in values.items()))
        if key not in seen:
            seen.add(key)
            unique.append((label, values))
    return unique


def corpus_image(kind: str, width: int, height: int, seed: int = 20261123) -> np.ndarray:
    """确定性测试图像 (H, W, 4) float32，覆盖负值 / 零 / 超白 / 边缘极值。"""
    rng = np.random.default_rng(seed + (width * 131 + height))
    image = np.ones((height, width, 4), dtype=np.float32)
    if kind == "hdr":
        image[..., :3] = rng.uniform(-0.5, 4.0, (height, width, 3))
        image[..., 3] = rng.uniform(0.0, 1.0, (height, width))
        image[0, 0, :3] = (-2.0, 0.0, 16.0)
        if width > 1:
            image[0, 1, :3] = (60000.0, -0.25, 1.0)
    elif kind == "edges":
        image[..., :3] = 0.18
        border = np.zeros((height, width), dtype=bool)
        border[0, :] = border[-1, :] = True
        border[:, 0] = border[:, -1] = True
        stripes = (np.indices((height, width)).sum(axis=0) % 2 == 0)
        image[border & stripes, :3] = (60000.0, -4.0, 0.0)
        image[border & ~stripes, :3] = (-0.5, 32.0, 0.5)
    elif kind == "ramp":
        xs = np.linspace(0.0, 1.0, width, dtype=np.float32)
        ys = np.linspace(-0.1, 1.1, height, dtype=np.float32)
        image[..., 0] = ys[:, None]
        image[..., 1] = xs[None, :]
        image[..., 2] = (xs[None, :] * ys[:, None])
    else:
        raise ValueError(f"未知语料图像 {kind!r}")
    return image


CORPUS_KINDS: Tuple[str, ...] = ("hdr", "edges", "ramp")


def fp16_ordered_keys(values: np.ndarray) -> np.ndarray:
    """float16 位模式 → 跨零单调的整数键（两个键相减即 fp16 ulp 距离）。"""
    bits = np.ascontiguousarray(values, dtype=np.float16).view(np.uint16).astype(np.int32)
    magnitude = bits & 0x7FFF
    return np.where((bits >> 15) & 1 == 1, -magnitude, magnitude)


def fp16_ulp_distance(reference: np.ndarray, actual: np.ndarray) -> np.ndarray:
    return np.abs(fp16_ordered_keys(reference) - fp16_ordered_keys(actual))


#: verified 门限的三项容差。每一项对应一种**已测明**的 GL / Metal 后端差异来源
#:（AMD RX 6900 XT，2026-11-23；数值见 effect-metal-parity-2026-11-23.json）：
#: - ABS_TOL：视觉无关量级——1/16 个 8-bit 码值、1/4 个 10-bit 码值；
#: - REL_TOL：正常范围数值的 2 个 fp16 ulp（fp16 尾数 10 位），覆盖舍入跨界；
#: - SCALE_TOL：fp32 舍入噪声相对**运算对象 / 采样邻域幅度**的比例。双线性采样
#:   的坐标误差约 2e-7 个纹素，遇到幅度为 S 的邻点时绝对误差 ∝ S。
ABS_TOL = 2.0 ** -12          # 2.44e-4
REL_TOL = 2.0 ** -10          # 9.77e-4
SCALE_TOL = 1e-6


def input_scale(image: np.ndarray) -> float:
    """输入图像 RGB 的最大幅度（SCALE_TOL 的参考量）。"""
    return float(np.abs(np.asarray(image, dtype=np.float32)[..., :3]).max())


def parity_metrics(reference: np.ndarray, actual: np.ndarray, *, scale: float = 0.0) -> Dict[str, Any]:
    """RGB 三通道的差异度量 + Alpha 是否逐位相同 + 是否在 verified 门限内。

    通过条件：Alpha 逐位相同，且每个 RGB 分量满足
    ``|a - b| <= max(ABS_TOL, REL_TOL * max(|a|, |b|), SCALE_TOL * scale)``。
    ``worst_ratio`` 是最坏差异相对该容差的比例（<= 1 即通过）。
    """
    ref = np.asarray(reference, dtype=np.float32)[..., :3]
    act = np.asarray(actual, dtype=np.float32)[..., :3]
    abs_diff = np.abs(ref - act)
    magnitude = np.maximum(np.abs(ref), np.abs(act))
    with np.errstate(divide="ignore", invalid="ignore"):
        rel = np.where(magnitude > 0, abs_diff / magnitude, 0.0)
    limit = np.maximum(ABS_TOL, REL_TOL * magnitude)
    if scale > 0:
        limit = np.maximum(limit, SCALE_TOL * scale)
    exceeded = abs_diff > limit
    alpha_identical = bool(np.array_equal(np.asarray(reference)[..., 3], np.asarray(actual)[..., 3]))
    non_finite_mismatch = bool((np.isnan(ref) != np.isnan(act)).any() or (np.isinf(ref) != np.isinf(act)).any())
    return {
        "identical_fraction": float(np.mean(np.all(ref == act, axis=-1))),
        "max_abs": float(abs_diff.max()) if abs_diff.size else 0.0,
        "max_rel": float(rel.max()) if rel.size else 0.0,
        "max_fp16_ulp": int(fp16_ulp_distance(ref, act).max()) if ref.size else 0,
        "worst_ratio": float((abs_diff / limit).max()) if abs_diff.size else 0.0,
        "alpha_identical": alpha_identical,
        "non_finite_mismatch": non_finite_mismatch,
        "non_finite_reference": bool(np.isnan(ref).any() or np.isinf(ref).any()),
        "non_finite_actual": bool(np.isnan(act).any() or np.isinf(act).any()),
        "gate_ok": bool(alpha_identical and not non_finite_mismatch and not exceeded.any()),
    }


def merge_metrics(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """把多次对拍的度量合成一个最坏值摘要（任一次不过门即整体不过）。"""
    return {
        "renders": len(results),
        "identical_fraction": min(r["identical_fraction"] for r in results),
        "max_abs": max(r["max_abs"] for r in results),
        "max_rel": max(r["max_rel"] for r in results),
        "max_fp16_ulp": max(r["max_fp16_ulp"] for r in results),
        "worst_ratio": max(r["worst_ratio"] for r in results),
        "alpha_identical": all(r["alpha_identical"] for r in results),
        "non_finite_reference": any(r["non_finite_reference"] for r in results),
        "non_finite_actual": any(r["non_finite_actual"] for r in results),
        "gate_ok": all(r["gate_ok"] for r in results),
    }


def passes_gate(metrics: Dict[str, Any]) -> bool:
    """是否在 verified 门限内（见 :func:`parity_metrics`）。"""
    return bool(metrics["gate_ok"])


# ── GL 参考渲染 ─────────────────────────────────────────────────────────


def create_gl_context() -> Any:
    import moderngl

    return moderngl.create_standalone_context(require=330)


def _uniform_value(param: Dict[str, Any], value: Any) -> Any:
    """用户侧取值 → GL uniform 值（与 EffectPassRunner._pack_plugin_params 同口径）。"""
    if param["type"] == "slider":
        return float(value)
    if param["type"] in ("checkbox", "select"):
        if isinstance(value, str):
            options = param.get("options") or []
            return options.index(value) if value in options else 0
        return int(value)
    if param["type"] == "color":
        return tuple(float(v) for v in value)
    raise ValueError(f"参数类型 {param['type']!r} 没有 uniform 投影")


def to_unorm8(image: np.ndarray) -> np.ndarray:
    """float (H, W, 4) → 归一化 8-bit 码值（裁到 [0, 1] 后四舍五入）。"""
    return np.rint(np.clip(np.asarray(image, dtype=np.float32), 0.0, 1.0) * 255.0).astype(np.uint8)


def gl_render_reference(ctx: Any, shader_code: str, params: Sequence[Dict[str, Any]],
                        image: np.ndarray, values: Optional[Dict[str, Any]] = None,
                        *, wrap_repeat: bool = False, eight_bit: bool = False) -> np.ndarray:
    """用生产的 DCTL wrapper 在 GL 上渲染一帧并读回 (H, W, 4) float32。

    ``wrap_repeat=True`` 让 GL 输入纹理用 REPEAT 寻址（moderngl 默认值），
    用于判断插件是否依赖纹理寻址模式；默认 CLAMP_TO_EDGE 以对齐 ABI 的 sampler。
    ``eight_bit=True`` 走 RGBA8 纹理与渲染目标（Edit 预览的 8-bit 链路），返回值
    是 0…255 的码值；默认走 RGBA16F（HDR 链路）。
    """
    import moderngl

    from src.features.shared.effects.dctl_wrapper import wrap_dctl_process
    from src.features.shared.effects.effect_pass_runner import _PASSTHROUGH_VERTEX, _QUAD_VERTICES

    height, width = image.shape[0], image.shape[1]
    program = ctx.program(vertex_shader=_PASSTHROUGH_VERTEX,
                          fragment_shader=wrap_dctl_process(shader_code, list(params)))
    dtype = "f1" if eight_bit else "f2"
    payload = to_unorm8(image) if eight_bit else image.astype(np.float16)
    source = ctx.texture((width, height), 4, payload.tobytes(), dtype=dtype)
    source.filter = (moderngl.LINEAR, moderngl.LINEAR)
    source.repeat_x = wrap_repeat
    source.repeat_y = wrap_repeat
    target = ctx.texture((width, height), 4, dtype=dtype)
    fbo = ctx.framebuffer(color_attachments=[target])
    vbo = ctx.buffer(_QUAD_VERTICES.tobytes())
    vao = ctx.vertex_array(program, vbo, "in_vert", "in_uv")
    try:
        source.use(0)
        program["u_texture"].value = 0
        if "u_resolution" in program:
            program["u_resolution"].value = (float(width), float(height))
        merged = {p["var_name"]: default_value(p) for p in params if p.get("type") in _SHADABLE}
        merged.update(values or {})
        for p in params:
            key = f"u_{p.get('var_name', '')}"
            if p.get("type") not in _SHADABLE or p["var_name"] not in merged or key not in program:
                continue
            program[key].value = _uniform_value(p, merged[p["var_name"]])
        fbo.use()
        vao.render(moderngl.TRIANGLE_STRIP)
        raw = fbo.read(components=4, dtype=dtype)
    finally:
        for obj in (vao, vbo, fbo, target, source, program):
            obj.release()
    if eight_bit:
        return np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 4).astype(np.float32)
    return np.frombuffer(raw, dtype=np.float16).reshape(height, width, 4).astype(np.float32)
