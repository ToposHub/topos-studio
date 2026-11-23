"""插件与内置条目的 Metal 对拍验证记录（阶段 6.2 / 6.3 / 6.5）。

这是**数据**模块：记录「哪个 ID 在哪台设备、以多大误差通过了 GL 参考值 vs
Metal 读回的对拍」。能力解析与兼容矩阵从这里读取验证状态；
``tests/shared/test_tglsl_metal_parity.py`` 在有 Metal 设备的机器上重跑同一对拍，
并断言这里登记为 ``verified`` 的每一个 ID 仍然通过。

源码头部声明了 ``metal`` 但这里没有 ``verified`` 记录的插件，只算「已声明、未验证」
（测试会拒绝这种不一致）。

状态词：
    - ``verified``：真实运行通过（门限见 ``PLUGIN_CORPUS`` 与 tests/shared/tglsl_metal_parity.py）；
    - ``noise_gated``：可转译、可编译，但逐像素结果依赖哈希，GL 与 Metal 之间
      天然不可复现，不计入 verified，也不声明 metal；
    - ``unverified``：尚未对拍。
"""

from __future__ import annotations

from typing import Dict

from src.features.shared.effects.msl_effect_registry import (
    STATUS_NOISE_GATED,
    STATUS_VERIFIED,
    VerificationRecord,
)

__all__ = [
    "EVIDENCE_FILE",
    "EXPOSURE_VERIFICATION",
    "PLUGIN_CORPUS",
    "PLUGIN_VERIFICATION_RECORDS",
    "VERIFICATION_DATE",
    "VERIFICATION_DEVICE",
]

VERIFICATION_DEVICE = "AMD Radeon RX 6900 XT (Intel Mac, hasUnifiedMemory=False)"
VERIFICATION_DATE = "2026-11-23"
EVIDENCE_FILE = "docs/macos_metal_migration/evidence/effect-metal-parity-2026-11-23.json"
PLUGIN_CORPUS = (
    "GL: dctl_wrapper.wrap_dctl_process + EffectPassRunner 顶点/quad，RGBA16F；5 种尺寸"
    "（19x13、7x1、1x7、64x48、33x32）× 3 类图像（HDR 负值/零/超白、边缘极值、渐变）"
    "× 每个参数 min/default/max 加 4 组固定种子随机组合；"
    "门限：Alpha 逐位相同且 |Δ| ≤ max(2.44e-4, 9.77e-4·|v|, 1e-6·输入幅度)")

#: 内置曝光条目 ``builtin:exposure``（经注册表 helper migration_exposure 调用）。
EXPOSURE_VERIFICATION = VerificationRecord(
    status=STATUS_VERIFIED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
    max_abs_diff=0.0, max_rel_diff=0.0, max_fp16_ulp=0,
    corpus=PLUGIN_CORPUS + "；曝光取 -5、-4、-2、-1、-0.25、0、0.25、1、2、4、5（共 165 次渲染，逐位相同）",
    evidence=EVIDENCE_FILE,
    note="另验：曝光 0 透传逐位相同；±1/±2/+4 档逐像素倍率误差为 0；两实例链式与独立 numpy 模型逐位相同；参数 JSON 往返后重渲染逐位相同。fp16 输出在 ±65504 饱和（+5 档的超白像素两后端一致）。",
)

#: 逐插件记录：21 个 verified（头部声明 metal），3 个 noise_gated（不声明 metal）。
PLUGIN_VERIFICATION_RECORDS: Dict[str, VerificationRecord] = {
    "Bleach_Bypass": VerificationRecord(
        status=STATUS_VERIFIED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
        max_abs_diff=0.00390625, max_rel_diff=0.0009661835501901805, max_fp16_ulp=1,
        corpus=PLUGIN_CORPUS, evidence=EVIDENCE_FILE,
        note='165 次渲染，99.6% 像素逐位相同',
    ),
    "CRT_Scanlines": VerificationRecord(
        status=STATUS_VERIFIED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
        max_abs_diff=32.0, max_rel_diff=0.0009756097570061684, max_fp16_ulp=1,
        corpus=PLUGIN_CORPUS, evidence=EVIDENCE_FILE,
        note='195 次渲染，39.7% 像素逐位相同；特性 texture_size；仅 39.7% 像素逐位相同：fract(px.y / period) 的除法舍入差异，全程 ≤ 1 fp16 ulp。',
    ),
    "Channel_Mixer": VerificationRecord(
        status=STATUS_VERIFIED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
        max_abs_diff=0.00048828125, max_rel_diff=0.0016666667070239782, max_fp16_ulp=1,
        corpus=PLUGIN_CORPUS, evidence=EVIDENCE_FILE,
        note='360 次渲染，99.6% 像素逐位相同',
    ),
    "Cross_Process": VerificationRecord(
        status=STATUS_VERIFIED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
        max_abs_diff=0.00390625, max_rel_diff=0.012987012974917889, max_fp16_ulp=1,
        corpus=PLUGIN_CORPUS, evidence=EVIDENCE_FILE,
        note='135 次渲染，99.9% 像素逐位相同',
    ),
    "Day_Night_Converter": VerificationRecord(
        status=STATUS_VERIFIED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
        max_abs_diff=0.0078125, max_rel_diff=0.0009578543831594288, max_fp16_ulp=1,
        corpus=PLUGIN_CORPUS, evidence=EVIDENCE_FILE,
        note='195 次渲染，99.2% 像素逐位相同',
    ),
    "Duotone_Toner": VerificationRecord(
        status=STATUS_VERIFIED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
        max_abs_diff=0.001953125, max_rel_diff=0.015243902802467346, max_fp16_ulp=5,
        corpus=PLUGIN_CORPUS, evidence=EVIDENCE_FILE,
        note='210 次渲染，85.7% 像素逐位相同；fp16 ulp 计数 5 出现在次正规区间；正常范围内的差异为 1 ulp。',
    ),
    "Edge_Ink_Outline": VerificationRecord(
        status=STATUS_VERIFIED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
        max_abs_diff=16.0, max_rel_diff=0.0008779631461948156, max_fp16_ulp=1,
        corpus=PLUGIN_CORPUS, evidence=EVIDENCE_FILE,
        note='180 次渲染，71.4% 像素逐位相同；特性 sampling, texture_size；含纹素对齐采样；全程 ≤ 1 fp16 ulp。',
    ),
    "Fake_Infrared": VerificationRecord(
        status=STATUS_VERIFIED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
        max_abs_diff=0.0625, max_rel_diff=0.0013351135421544313, max_fp16_ulp=1,
        corpus=PLUGIN_CORPUS, evidence=EVIDENCE_FILE,
        note='165 次渲染，99.9% 像素逐位相同',
    ),
    "Film_Halation": VerificationRecord(
        status=STATUS_VERIFIED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
        max_abs_diff=0.0, max_rel_diff=0.0, max_fp16_ulp=0,
        corpus=PLUGIN_CORPUS, evidence=EVIDENCE_FILE,
        note='120 次渲染，100.0% 像素逐位相同',
    ),
    "Frequency_Split": VerificationRecord(
        status=STATUS_VERIFIED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
        max_abs_diff=0.00390625, max_rel_diff=0.06666667014360428, max_fp16_ulp=2,
        corpus=PLUGIN_CORPUS, evidence=EVIDENCE_FILE,
        note='180 次渲染，98.8% 像素逐位相同；特性 sampling；依赖纹理寻址模式：在 CLAMP_TO_EDGE 的 GL 参考下通过（与 ABI 默认 sampler 一致）；REPEAT 纹理须选 linear+repeat sampler 规格。',
    ),
    "Grad_Filter_Exposure": VerificationRecord(
        status=STATUS_VERIFIED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
        max_abs_diff=0.03125, max_rel_diff=0.0009708738070912659, max_fp16_ulp=1,
        corpus=PLUGIN_CORPUS, evidence=EVIDENCE_FILE,
        note='195 次渲染，85.7% 像素逐位相同',
    ),
    "Kaleido_Mirror": VerificationRecord(
        status=STATUS_VERIFIED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
        max_abs_diff=0.01025390625, max_rel_diff=0.013944223523139954, max_fp16_ulp=21,
        corpus=PLUGIN_CORPUS, evidence=EVIDENCE_FILE,
        note='165 次渲染，99.6% 像素逐位相同；特性 sampling, texture_size；含双线性采样；最大差异来自采样坐标的 fp32 舍入（相对采样邻域幅度 ≤ 2.3e-7）。',
    ),
    "LIB1_Gradient_Map": VerificationRecord(
        status=STATUS_VERIFIED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
        max_abs_diff=0.00048828125, max_rel_diff=0.0029069767333567142, max_fp16_ulp=1,
        corpus=PLUGIN_CORPUS, evidence=EVIDENCE_FILE,
        note='285 次渲染，99.6% 像素逐位相同',
    ),
    "Lens_Distortion": VerificationRecord(
        status=STATUS_VERIFIED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
        max_abs_diff=0.013671875, max_rel_diff=0.016826923936605453, max_fp16_ulp=28,
        corpus=PLUGIN_CORPUS, evidence=EVIDENCE_FILE,
        note='135 次渲染，99.1% 像素逐位相同；特性 sampling, texture_size；含双线性采样；最大差异来自采样坐标的 fp32 舍入（相对采样邻域幅度 ≤ 2.3e-7）。',
    ),
    "Light_Leak_Corner": VerificationRecord(
        status=STATUS_VERIFIED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
        max_abs_diff=0.000244140625, max_rel_diff=0.000859845255035907, max_fp16_ulp=1,
        corpus=PLUGIN_CORPUS, evidence=EVIDENCE_FILE,
        note='210 次渲染，99.8% 像素逐位相同',
    ),
    "Sky_Replace_Matte": VerificationRecord(
        status=STATUS_VERIFIED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
        max_abs_diff=0.0078125, max_rel_diff=0.0036900369450449944, max_fp16_ulp=1,
        corpus=PLUGIN_CORPUS, evidence=EVIDENCE_FILE,
        note='240 次渲染，85.7% 像素逐位相同',
    ),
    "Three_Way_Tint": VerificationRecord(
        status=STATUS_VERIFIED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
        max_abs_diff=0.001953125, max_rel_diff=0.0009233610471710563, max_fp16_ulp=1,
        corpus=PLUGIN_CORPUS, evidence=EVIDENCE_FILE,
        note='255 次渲染，99.9% 像素逐位相同',
    ),
    "Vibrance_Saturation": VerificationRecord(
        status=STATUS_VERIFIED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
        max_abs_diff=0.001953125, max_rel_diff=0.0017152659129351377, max_fp16_ulp=1,
        corpus=PLUGIN_CORPUS, evidence=EVIDENCE_FILE,
        note='150 次渲染，98.4% 像素逐位相同',
    ),
    "Vignette_Shapes": VerificationRecord(
        status=STATUS_VERIFIED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
        max_abs_diff=1.0, max_rel_diff=0.25, max_fp16_ulp=1,
        corpus=PLUGIN_CORPUS, evidence=EVIDENCE_FILE,
        note='180 次渲染，99.2% 像素逐位相同',
    ),
    "White_Balance_Advanced": VerificationRecord(
        status=STATUS_VERIFIED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
        max_abs_diff=0.0078125, max_rel_diff=0.0008787346305325627, max_fp16_ulp=1,
        corpus=PLUGIN_CORPUS, evidence=EVIDENCE_FILE,
        note='165 次渲染，85.7% 像素逐位相同',
    ),
    "warm_tint": VerificationRecord(
        status=STATUS_VERIFIED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
        max_abs_diff=0.0, max_rel_diff=0.0, max_fp16_ulp=0,
        corpus=PLUGIN_CORPUS, evidence=EVIDENCE_FILE,
        note='105 次渲染，100.0% 像素逐位相同',
    ),
    "Film_Grain": VerificationRecord(
        status=STATUS_NOISE_GATED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
        max_abs_diff=0.25048828125, max_rel_diff=1.8974961042404175, max_fp16_ulp=24099,
        corpus=PLUGIN_CORPUS, evidence=EVIDENCE_FILE,
        note='195 次渲染，68.2% 像素逐位相同；算术哈希 grain_hash 在 GL 与 Metal 之间不可复现；68.2% 像素逐位相同；关闭 FP 合并（#pragma clang fp contract(off)）数值不变。',
    ),
    "Glitch_Blocks": VerificationRecord(
        status=STATUS_NOISE_GATED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
        max_abs_diff=468.0, max_rel_diff=1.941216230392456, max_fp16_ulp=17773,
        corpus=PLUGIN_CORPUS, evidence=EVIDENCE_FILE,
        note='195 次渲染，53.7% 像素逐位相同；特性 sampling, texture_size；算术哈希 glitch_hash 在 GL 与 Metal 之间不可复现；53.7% 像素逐位相同；边缘极值语料上最大绝对差 468。',
    ),
    "HighPass_Retouch": VerificationRecord(
        status=STATUS_NOISE_GATED, device=VERIFICATION_DEVICE, date=VERIFICATION_DATE,
        max_abs_diff=4040.0, max_rel_diff=2.0, max_fp16_ulp=46539,
        corpus=PLUGIN_CORPUS, evidence=EVIDENCE_FILE,
        note='150 次渲染，0.0% 像素逐位相同；特性 sampling, texture_size；宿主 helper fast_blur 的 sin-hash 抖动不可复现：同一输入下 GL 与 Metal 的 fract(sin(d)*43758.5453) 仅 81% 相等。',
    ),
}
