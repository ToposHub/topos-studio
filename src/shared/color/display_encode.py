"""F5.1 单点显示编码——场景线性 → sRGB 显示（唯一真值 = provider 传输函数）。

历史上线性→sRGB OETF（IEC 61966-2-1）在约 20 处逐份手写（CPU numpy /
GLSL 常数串）；SDR 常数彼此一致但从未合同化。本模块把**非热路径**的
CPU 内联拷贝收口为对 provider 传输函数 ``linear_to_srgb_np`` 的单点调用
（形状无关、逐通道、BGR 直传安全——同 BT709 primaries）；GPU 回读 shader
（readback_srgb_bgr）保留自有实现，其与 provider 的等价性由
``tests/color/test_f51_display_encode_contract.py`` 冻结。

注意：不走完整 ``transform_color`` 管线——OCIO provider 对非 (h,w,c)
形状会异常并静默回退恒等（display_encode 不允许该降级）；完整管线的
OCIO/Builtin 一致性由 provider parity 测试另行锁定。

语义：>1 的 HDR 线性高光按现状硬剪到 1.0（无 tonemap——Edit/Color 预览
的视频主路径为编码信号直通，见 F5.1 ledger 三路语义表）。
"""

from __future__ import annotations

import numpy as np

from src.shared.color.color_transform_provider import linear_to_srgb_np


def linear_to_srgb_display(lin: np.ndarray) -> np.ndarray:
    """场景线性 [0,1]（越界硬剪）→ sRGB 编码，float32。

    形状契约：末维必须是 3 或 4（RGB/RGBA，或单个颜色三元组的 1-D）；
    逐通道 OETF，其余维度与通道序（BGR 直传）保持。其他形状显式拒绝
    （provider 传输函数按末 3 通道切片，静默错切比报错更危险）。
    """
    arr = np.asarray(lin, dtype=np.float32)
    if arr.shape == () or arr.shape[-1] not in (3, 4):
        raise ValueError(
            f"linear_to_srgb_display expects (..., 3|4), got {arr.shape}")
    clipped = np.clip(arr, 0.0, 1.0)
    return linear_to_srgb_np(clipped).astype(np.float32)


def linear_bgr_to_srgb_display_u8(bgr_linear: np.ndarray) -> np.ndarray:
    """(…,3) BGR 场景线性 float → sRGB 编码 uint8 BGR（*255 饱和剪）。"""
    encoded = linear_to_srgb_display(bgr_linear)
    return np.clip(encoded * 255.0, 0, 255).astype(np.uint8)
