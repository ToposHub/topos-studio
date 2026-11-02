"""B00 synthetic fixture generators —— 调色节点后端迁移基线数据集。

计划表 §7.1 固定数据集（B00.2）：F01/F02/F03 的**确定性**生成器。
全部为纯函数（无随机源 / 无 IO / 无 GL / 无 Qt），同机同版本多次调用
字节级一致，供三类消费者复用：

1. ``scripts/generate_color_graph_baselines.py`` —— 旧引擎 float reference；
2. ``tests/color/test_legacy_baseline_behaviors.py`` 等 CPU 行为用例；
3. B07+ 的 Corrector 算子等价对照（同一输入 → 同一参考）。

F06/F07（关键帧序列 / legacy stack 载荷）的生成器随对应任务补齐；
本包只冻结 B00 需要的三个数据集与 manifest。
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

from src.features.color.core.layer import Layer

FIXTURE_SPEC_VERSION = 1

# F01 锚点值（计划 §7.1：覆盖 −0.25、0、0.18、1、4、16）
F01_ANCHORS: Tuple[float, ...] = (-0.25, 0.0, 0.18, 1.0, 4.0, 16.0)

# F02 调色板：RGB / CMY / 肤色三阶 / 极值（scene-linear 近似值）
F02_PALETTE: Tuple[Tuple[float, float, float], ...] = (
    (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0),  # RGB
    (0.0, 1.0, 1.0), (1.0, 0.0, 1.0), (1.0, 1.0, 0.0),  # CMY
    (0.2140, 0.1611, 0.1285),                            # 肤色 ~18% 灰线性
    (0.859, 0.729, 0.604),                               # 亮肤色（sRGB 近似线性化）
    (0.055, 0.055, 0.055), (0.945, 0.945, 0.945),        # 暗端/亮端
)

# 误差规则（计划 §7.1；B00 冻结，后续任务不得无依据放宽）
ERROR_CONTRACT: Dict[str, Any] = {
    "exact": "纯数据 roundtrip / ID / 命令原子性采用字节级精确比较",
    "cpu_oracle_abs": 1e-5,
    "gl_float16_abs": 2e-3,
    "hdr_relative": 2e-3,
    "note": (
        "CPU float32 oracle 基础算子 abs<=1e-5；GL/Metal float16 路径对"
        "规范化测试值 abs<=2e-3 并记录 relative/max/mean；HDR 用"
        " relative<=2e-3 + near-zero 规则。实际阈值由 B00/B07 测量冻结。"
    ),
}


# ── F01：HDR ramp ────────────────────────────────────────────────────────

def f01_hdr_ramp(size: int = 64) -> np.ndarray:
    """HDR 亮度斜坡，(H, W, 4) float32。

    布局（B00 冻结，改变布局即改变基线语义）：
      - 水平方向 4 段：[-0.25, 0] 负值段 | [0, 0.18] | [0.18, 1] 常规段 |
        [1, 16] 对数 HDR 段（每段等像素宽，段内线性/对数插值）；
      - 顶部 4 行恒 0.18 灰锚带，底部 4 行按 F01_ANCHORS 的阶梯带；
      - alpha 恒 1（透明边属于 F02 的职责）。
    """
    if size < 16:
        raise ValueError(f"f01_hdr_ramp size>=16 required, got {size}")
    w = size
    seg_w = max(1, w // 4)
    img = np.zeros((size, w, 4), dtype=np.float32)

    def _seg(v0: float, v1: float, log: bool = False) -> np.ndarray:
        n = seg_w
        t = np.arange(n, dtype=np.float64) / max(1, n - 1)
        if log:
            lo = max(abs(v0), 1e-6)
            hi = max(abs(v1), 1e-6)
            return (lo * (hi / lo) ** t).astype(np.float32)
        return (v0 + (v1 - v0) * t).astype(np.float32)

    ramp = np.concatenate([
        _seg(-0.25, 0.0),
        _seg(0.0, 0.18),
        _seg(0.18, 1.0),
        _seg(1.0, 16.0, log=True),
    ])[:w]
    if ramp.shape[0] < w:  # size 不整除 4 时补齐最后一段末值
        ramp = np.concatenate([ramp, np.full(w - ramp.shape[0], 16.0, np.float32)])

    img[:, :, :3] = ramp[None, :, None]
    # 顶部锚带：恒 0.18
    img[0:4, :, :3] = 0.18
    # 底部阶梯带：F01_ANCHORS 均分画布宽
    step_w = max(1, w // len(F01_ANCHORS))
    for i, v in enumerate(F01_ANCHORS):
        img[-4:, i * step_w:(i + 1) * step_w, :3] = v
    img[..., 3] = 1.0
    return np.ascontiguousarray(img)


# ── F02：patch / 透明边 / alpha 梯度 ─────────────────────────────────────

def f02_patches(size: int = 64, border: int = 4) -> np.ndarray:
    """调色板 patch 棋盘 + 透明边 + 独立 alpha 梯度，(H, W, 4) float32。

    布局：四周 ``border`` 像素透明（RGB 保留调色板值、alpha=0，用于断言
    key 与 image alpha 分离）；内部按 3×3 棋盘铺 F02_PALETTE（循环取色）；
    alpha 通道为水平 0→1 梯度（与 RGB 独立）。
    """
    if size < 3 * border + 6:
        raise ValueError("f02_patches size too small for border")
    img = np.zeros((size, size, 4), dtype=np.float32)
    inner = size - 2 * border
    cell = max(1, inner // 3)
    for r in range(3):
        for c in range(3):
            color = F02_PALETTE[(r * 3 + c) % len(F02_PALETTE)]
            y0 = border + r * cell
            x0 = border + c * cell
            img[y0:y0 + cell, x0:x0 + cell, :3] = color
    # 内部之外的边带 RGB 置为暗肤色系（避免全 0 让 alpha 分离断言退化）
    mask_inner = np.zeros((size, size), dtype=bool)
    mask_inner[border:border + 3 * cell, border:border + 3 * cell] = True
    img[~mask_inner, :3] = (0.2140, 0.1611, 0.1285)
    # alpha：全画布水平梯度；透明边强制 alpha=0
    alpha = np.linspace(0.0, 1.0, size, dtype=np.float32)[None, :]
    img[..., 3] = np.broadcast_to(alpha, (size, size))
    img[:border, :, 3] = 0.0
    img[-border:, :, 3] = 0.0
    img[:, :border, 3] = 0.0
    img[:, -border:, 3] = 0.0
    return np.ascontiguousarray(img)


# ── F03：三个非交换 Corrector 栈 ─────────────────────────────────────────

def _neutral_layer(name: str) -> Layer:
    layer = Layer(name)
    return layer


def _curve_lut(points: Sequence[Tuple[float, float]]) -> np.ndarray:
    """由控制点折线生成 (256, 1, 3) 曲线 LUT（与 Layer.curves_lut 约定一致）。"""
    xs = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    ctrl_x = np.array([p[0] for p in points], dtype=np.float32)
    ctrl_y = np.array([p[1] for p in points], dtype=np.float32)
    ys = np.interp(xs, ctrl_x, ctrl_y).astype(np.float32)
    lut = np.zeros((256, 1, 3), dtype=np.float32)
    lut[:, 0, :] = ys[:, None]
    return lut


def f03_layer_stack() -> List[Layer]:
    """三个非交换 Corrector（B00 冻结参数）。

    非交换性设计：
      - L0「Print Warmer」：gain 蓝 −15% + 曝光 +0.5（乘性）；
      - L1「Lift Shadows」：lift 亮度 +0.05 + 对比 −0.2（加性 + S 曲线）；
      - L2「Tone Curve」：2.4 gamma 型曲线 LUT + 饱和 1.15。
    乘性/加性/曲线三类的先后互换会改变结果（B07/B03 用作顺序等价断言）。
    """
    l0 = _neutral_layer("Print Warmer")
    l0.grading_params["exposure"] = 0.5
    l0.grading_params["gain"] = (0.0, 0.0, -0.15)

    l1 = _neutral_layer("Lift Shadows")
    l1.grading_params["lift"] = (0.0, 0.0, 0.05)
    l1.grading_params["contrast"] = -0.2

    l2 = _neutral_layer("Tone Curve")
    # 曲线以可持久化控制点定义（UI 屏幕坐标约定：y=0 顶部=最大输出；
    # 恒等曲线 = [(0,255),(255,0)]）。LUT 由数据派生——legacy save/load
    # 只持久化控制点，LUT 是渲染侧缓存（B09 迁移链路必须可重建）。
    l2.curves_data = {"M": [(0, 255), (64, 209), (191, 46), (255, 0)]}
    from src.features.color.core.layer import build_curves_lut_from_data
    l2.curves_lut = build_curves_lut_from_data(l2.curves_data)
    l2.grading_params["saturation"] = 1.15
    return [l0, l1, l2]


def f03_disabled_variant() -> List[Layer]:
    """F03 的 bypass 变体：L1 visible=False（enabled/bypass 断言用）。"""
    stack = f03_layer_stack()
    stack[1].visible = False
    return stack


# ── 插件 stub（EffectSpec 纯数据构造，不依赖 plugins/ 目录扫描） ─────────

def f03_plugin_stub_binding(index: int = 0):
    """确定性 DCTL 插件 stub 的 PluginBinding（B00 manifest 身份冻结）。

    用最小 DCTL_PROCESS 源（乘色温系数）构造 EffectSpec——语义与
    ``plugin_bindings_from_instances`` 对真实 .tglsl 的处理同构，但不触发
    PluginManager 扫描，保证跨机器可复现。
    """
    from src.features.shared.effects.effect_spec import (
        EffectSpec,
        ParamDef,
        PluginBinding,
    )

    spec = EffectSpec(
        effect_type="plugin:baseline_gain_stub",
        name="Baseline Gain Stub",
        category="plugin",
        param_defs=(
            ParamDef(name="amount", ptype="float",
                     default=1.0, min_val=0.0, max_val=2.0),
        ),
        shader_kind="dctl_process",
        shader_sources=(
            "vec3 process(vec3 color, vec2 uv) {\n"
            "    return color * amount;\n"
            "}\n",
        ),
        dispatch_key="plugin:baseline_gain_stub",
        plugin_id="baseline_gain_stub",
        plugin_version="1.0.0",
        plugin_content_hash=_stub_content_hash(),
    )
    return PluginBinding(spec=spec, enabled=True, values={"amount": 1.25},
                         defaults={"amount": 1.0}, index=index)


def _stub_content_hash() -> str:
    return hashlib.sha256(b"topos-b00-baseline-gain-stub-v1").hexdigest()[:16]


# ── manifest ─────────────────────────────────────────────────────────────

def _array_digest(a: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(a).tobytes()).hexdigest()


def fixture_manifest() -> Dict[str, Any]:
    """B00 基线 manifest：颜色上下文 / 参数 / 插件 stub 身份 / 资源 hash。

    供 reference 归档与后续 B03/B07 等价对照引用；不包含机器绝对路径。
    """
    f01_64 = f01_hdr_ramp(64)
    f02_64 = f02_patches(64)
    stack = f03_layer_stack()
    binding = f03_plugin_stub_binding()
    return {
        "fixture_spec_version": FIXTURE_SPEC_VERSION,
        "color_context": {
            "working_space": "scene-linear (BT.709 primaries, no transfer)",
            "internal_precision": "float32 arrays; GL ping-pong RGBA16F",
            "display_transform": "not applied (reference is pre-display)",
        },
        "error_contract": ERROR_CONTRACT,
        "f01": {
            "anchors": list(F01_ANCHORS),
            "sizes": [64, 256],
            "sha256_64": _array_digest(f01_64),
        },
        "f02": {
            "palette": [list(c) for c in F02_PALETTE],
            "size": 64,
            "border": 4,
            "alpha": "horizontal gradient 0->1; border alpha=0",
            "sha256_64": _array_digest(f02_64),
        },
        "f03": {
            "layers": [
                {
                    "name": l.name,
                    "grading_params": {
                        k: list(v) if isinstance(v, tuple) else v
                        for k, v in l.grading_params.items()
                        if v not in (0, (0, 0, 0)) or k in ("exposure", "lift", "gain", "contrast", "saturation")
                    },
                    "has_curves_lut": l.curves_lut is not None,
                    "curves_lut_sha256": (
                        _array_digest(l.curves_lut) if l.curves_lut is not None else None),
                }
                for l in stack
            ],
            "non_commutative": True,
        },
        "plugin_stub": {
            "plugin_id": binding.spec.plugin_id,
            "plugin_version": binding.spec.plugin_version,
            "plugin_content_hash": binding.spec.plugin_content_hash,
            "values": dict(binding.values),
        },
    }
