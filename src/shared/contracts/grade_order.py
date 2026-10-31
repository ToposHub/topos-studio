"""DESK-06C · 调色工具应用顺序 / 参数域共享契约（双端同一份顺序表）。

问题
----
移动端要与桌面端对同一 grading_params 渲染出同一像素。调色是非线性
参数的顺序组合（exposure 在 contrast 前 vs 后结果不同），顺序散在
GPU 权威文本（shader_nodes.GRADING_SNIPPET / shader_compiler 组合
着色器 main()）与 CPU 镜像（the_decompiler.GradingMath）里，移动端
无法"照读实现"——需要一份机器可读的**顺序表**。

单一事实来源（现状，如实记录）
--------------------------------
1. **时间线 clip 的栈链顺序**集中在一处：``edit/core/color_chain.py``
   （F4.1 单点化）：``pre_group → clip 主栈 → clip 微调(trim) →
   post_group``。预览 GPU/CPU、导出 CPU、Metal 导出三个消费点全部经
   ``chain_extra_params`` 取链（段带 ``_chain_segment`` 标签）。
2. **单栈内部工具顺序**的权威文本是 GPU 侧
   ``color/core/shader_nodes.py::GRADING_SNIPPET``（MSL 镜像
   shader_nodes_msl.py；CPU 镜像 the_decompiler.GradingMath，
   逐条对应但**不含 6.5 色调区间饱和度**，见"已知分工边界"）。
3. **Color 页层管线顺序**（同一 layer 上 grade 之后的工具）集中在
   ``color/core/shader_compiler.py`` 组合着色器 main() 的发射序
   （blur/sharpen → grading → curves → HSL → soft clip → mixer →
   warper → 3D LUT → 插件 → 混合/蒙版/不透明度 → 胶片颗粒）。

因此顺序**有**单一来源（每层一个），本模块不复制公式、只冻结**顺序
与参数域**为跨端数据（``data/grade_order_v1.json``）；桌面合同测试
（tests/shared/test_grade_order_contract.py）再对着生产源码文本与
CPU 数值行为钉住。

参数域（按生产实现冻结）
------------------------
- 标量工具（exposure/temp/tint/contrast/shadow(s)/highlights/
  saturation/vibrance/mid_detail/sharpness）：设计单位域 ±100
  （着色器一律 ``x/100`` 折算；tonal_saturation_panel 等同域）。
- 色调区间饱和度（lift/gamma/gain_saturation）：±100（Color 页扩展，
  mega.frag ``tonal_saturation_adjust_g`` 同款）。
- hue_shift：度，±360（着色器 ``fract(h + hue/360)``）。
- lift/gamma/gain 轮：存储为 (hue, saturation, brightness) 三元组，
  hue 0..360 / sat 0..1 / bright ±1（轮心 0 = 恒等）；转 RGB 走
  ``GradingMath.params_to_rgb``（bright 平方×6 stops、hue 环形）。
- lut_intensity ∈ [0,1]；soft_clip 见 ColorStackData 默认值。

已知分工边界（不是 bug，如实冻结）
----------------------------------
- **编辑页主 grade**（时间线预览 primary_grade_shader + 导出
  GradingMath.apply_grading）只消费 11 标量 + 3 轮
  （color_chain._IDENTITY_SCALARS 全列）；**色调区间饱和度是 Color
  页专属工具**——编辑页 GPU 预览与导出两侧一致地不应用它，移动端
  编辑页不得悄悄支持（否则双端同工程不同像素）。
- ColorStackData.grading_params 的持久化域包含全部 18 键（含三个
  tonal saturation）；键集冻结见本模块 ``grading_param_keys``。

为什么不并入 edit_semantics.py：DESK-05B 的语义集覆盖"编辑结构"
（磁性/链接/ripple），调色顺序随 Color 管线独立演进——分开的 set id
让两份契约各自 bump、各自 digest，评审互不干扰（与 effect_catalog
独立于 timebase 同理）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

__all__ = [
    "GRADE_ORDER_SET_ID",
    "GRADE_ORDER_FILENAME",
    "build_grade_order",
    "grade_order_path",
    "write_grade_order_file",
    "load_grade_order_file",
    "grade_order_digest",
]

GRADE_ORDER_SET_ID = "topos.grade-order.v1"
GRADE_ORDER_FILENAME = "grade_order_v1.json"
_DATA_DIR = Path(__file__).parent / "data"

#: 单栈内部 primary grade 的工具顺序（shader_nodes.GRADING_SNIPPET 权威）。
PRIMARY_GRADE_TOOLS: tuple[str, ...] = (
    "exposure",            # 线性乘 2^(stops)，stops = sign(x)(x/100)²·6
    "white_balance",       # temp/tint 简单增益（R=1+t/200, B=1-t/200, G=1-tint/200）
    "contrast",            # pivot 0.18 幂曲线
    "lift_gamma_gain",     # gain 乘 + lift 仿射 → gamma（HDR/负值线性延伸）
    "dark_protection",     # luma<0 软压缩 + 通道平衡（GPU/CPU 镜像分支）
    "shadows",             # 幂曲线提/压暗部（shadow）
    "highlights",          # Reinhard 压缩/提升亮部
    "tonal_saturation",    # lift/gamma/gain_saturation 三区间（Color 页）
    "vibrance",
    "mid_detail_sharpness",  # 空间邻域（3px/1px，分辨率补偿）
    "hue_shift",           # 度，HSV 旋转（负值取 abs 再还原符号）
    "saturation",
)

#: Color 页层管线顺序（shader_compiler 组合着色器 main() 发射序）。
COLOR_LAYER_PIPELINE: tuple[str, ...] = (
    "blur_sharpen", "primary_grade", "curves", "hsl_curve",
    "soft_clip", "mixer", "warper", "lut3d", "plugin_effects",
    "blend_mask_opacity", "film_grain",
)

#: 时间线 clip 栈链顺序（edit/core/color_chain.py，F4.1 单点）。
CLIP_STACK_CHAIN: tuple[str, ...] = (
    "pre_group", "clip_main", "clip_trim", "post_group",
)

#: 编辑页主 grade 消费的标量（color_chain._IDENTITY_SCALARS 冻结）。
EDIT_PAGE_PRIMARY_SCALARS: tuple[str, ...] = (
    "exposure", "temp", "tint", "contrast", "shadow", "highlights",
    "saturation", "vibrance", "mid_detail", "sharpness", "hue_shift",
)

#: ±100 设计域的标量（含 Color 页扩展三个）。
_EDIT100 = EDIT_PAGE_PRIMARY_SCALARS[:10]  # 除 hue_shift 外的 10 个
PLUS_MINUS_100_SCALARS: tuple[str, ...] = (
    *_EDIT100,
    "lift_saturation", "gamma_saturation", "gain_saturation",
)

WHEEL_PARAMS: tuple[str, ...] = ("lift", "gamma", "gain")


def build_grade_order() -> dict[str, Any]:
    """构造冻结顺序/域的机器可读形式（确定性）。"""
    return {
        "set_id": GRADE_ORDER_SET_ID,
        "clip_stack_chain": list(CLIP_STACK_CHAIN),
        "primary_grade_tools": list(PRIMARY_GRADE_TOOLS),
        "color_layer_pipeline": list(COLOR_LAYER_PIPELINE),
        "param_domains": {
            "scalars_pm100": sorted(PLUS_MINUS_100_SCALARS),
            "hue_shift": {"unit": "degrees", "range": [-360, 360]},
            "wheels": {
                name: {
                    "storage": "(hue_deg 0..360, saturation 0..1, "
                               "brightness -1..1)",
                    "identity": [0, 0, 0],
                    "to_rgb": "GradingMath.params_to_rgb (hue ring, "
                              "brightness^2*6 stops)",
                }
                for name in WHEEL_PARAMS
            },
            "lut_intensity": [0.0, 1.0],
        },
        "edit_page_primary_domain": {
            "scalars": list(EDIT_PAGE_PRIMARY_SCALARS),
            "wheels": list(WHEEL_PARAMS),
            "note": "tonal saturation trio is Color-page only; edit "
                    "preview and export both omit it consistently",
        },
        "grading_param_keys": [
            "lift", "gamma", "gain",
            *EDIT_PAGE_PRIMARY_SCALARS,
            "lift_saturation", "gamma_saturation", "gain_saturation",
        ],
    }


def grade_order_path() -> Path:
    return _DATA_DIR / GRADE_ORDER_FILENAME


def write_grade_order_file(path: Path | None = None) -> Path:
    target = path or grade_order_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(build_grade_order(), indent=2, sort_keys=True,
                   ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8")
    return target


def load_grade_order_file(path: Path | None = None) -> dict[str, Any]:
    return json.loads(
        (path or grade_order_path()).read_text(encoding="utf-8"))


def grade_order_digest(data: dict[str, Any] | None = None) -> str:
    payload = data if data is not None else build_grade_order()
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
