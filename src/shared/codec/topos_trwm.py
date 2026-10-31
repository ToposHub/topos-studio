"""TRWM——TRAW 开发元数据块（RC1，计划表 M6）。

容器位置：`.toos` 的 optional chunk（FourCC ``'TRWM'``，native 视角为
带限额的不透明 optional chunk，OPTIONAL|PRESERVE）；MOV 的 stsd TPIC
entry 追加 ``trwm`` 子原子（tpcD 同模式，旧 reader 按未知子原子跳过）。

载荷结构（v1，固定 40 字节，大端）：

======  ====  ==================
Offset  Size  字段
======  ====  ==================
0       4     magic ``'TRWM'``
4       u16   version_major = 1
6       u16   version_minor = 0
8       f32   wb_gain_r（as-shot 线性域 WB gain）
12      f32   wb_gain_g
16      f32   wb_gain_b
20      f32   exposure_stops（EI 语义 = 2^stops）
24      f32   black_level（码值域）
28      u8    cfa_layout（0=RGGB 1=BGGR 2=GRBG 3=GBRG）
29      u8    sensor_matrix_id（0 = 未知/未声明，1.. = 预设表）
30      u16   reserved = 0
32      u32   reserved = 0
36      u32   crc32（覆盖 0..35）
======  ====  ==================

读侧语义：全部字段作为 **as-shot 基线**（RC4）——宿主 UI 显示相对
偏移量，不改变码流像素。未知 sensor_matrix_id = "未知相机"（显式提示，
不静默猜，RC2）。
"""

from __future__ import annotations

import struct
import zlib
from typing import Any, Dict

TRWM_MAGIC = b"TRWM"
TRWM_VERSION_MAJOR = 1
TRWM_VERSION_MINOR = 0
TRWM_SIZE = 40

CFA_LAYOUTS = {"RGGB": 0, "BGGR": 1, "GRBG": 2, "GBRG": 3}
CFA_LAYOUT_NAMES = {v: k for k, v in CFA_LAYOUTS.items()}

_HEAD = struct.Struct(">4sHHfffffBBHI")
_BODY_LEN = _HEAD.size          # 36
_CRC_LEN = 4


class ToposTrwmError(ValueError):
    """载荷违反 TRWM 布局（与 native MALFORMED/CHECKSUM 同语义）。"""


def encode_trwm(
    wb_gain_r: float = 1.0,
    wb_gain_g: float = 1.0,
    wb_gain_b: float = 1.0,
    exposure_stops: float = 0.0,
    black_level: float = 0.0,
    cfa_layout: str = "RGGB",
    sensor_matrix_id: int = 0,
) -> bytes:
    """as-shot 参数 → TRWM v1 载荷（40B，CRC32 覆盖 0..35）。"""
    layout = CFA_LAYOUTS.get(str(cfa_layout).upper())
    if layout is None:
        raise ToposTrwmError(
            f"cfa_layout 非法: {cfa_layout!r}（可选：{sorted(CFA_LAYOUTS)}）")
    if not 0 <= int(sensor_matrix_id) <= 255:
        raise ToposTrwmError(
            f"sensor_matrix_id 越界（0..255）: {sensor_matrix_id!r}")
    body = _HEAD.pack(
        TRWM_MAGIC, TRWM_VERSION_MAJOR, TRWM_VERSION_MINOR,
        float(wb_gain_r), float(wb_gain_g), float(wb_gain_b),
        float(exposure_stops), float(black_level),
        layout, int(sensor_matrix_id), 0, 0,
    )
    return body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def decode_trwm(payload: bytes) -> Dict[str, Any]:
    """TRWM 载荷 → as-shot 参数 dict（严格校验：magic/版本/CRC/域）。"""
    if len(payload) != TRWM_SIZE:
        raise ToposTrwmError(f"载荷长度须为 {TRWM_SIZE}: {len(payload)}")
    (magic, major, minor, wb_r, wb_g, wb_b, stops, black,
     layout, matrix_id, reserved, tail) = _HEAD.unpack_from(payload, 0)
    if magic != TRWM_MAGIC:
        raise ToposTrwmError(f"magic 不符: {magic!r}")
    if major != TRWM_VERSION_MAJOR:
        raise ToposTrwmError(f"version_major 不符: {major}")
    if reserved != 0 or tail != 0:
        raise ToposTrwmError("reserved 域非 0")
    stored_crc = struct.unpack_from(">I", payload, _BODY_LEN)[0]
    if zlib.crc32(payload[:_BODY_LEN]) & 0xFFFFFFFF != stored_crc:
        raise ToposTrwmError("CRC 不符")
    if layout not in CFA_LAYOUT_NAMES:
        raise ToposTrwmError(f"cfa_layout 越界: {layout}")
    for name, v in (("wb_gain_r", wb_r), ("wb_gain_g", wb_g),
                    ("wb_gain_b", wb_b)):
        if not (v > 0.0) or v != v or v == float("inf"):
            raise ToposTrwmError(f"{name} 须为正有限值: {v!r}")
    # 2026-09-21 复查收紧：exposure/black 须有限（NaN/Inf 会顺流污染
    # debayer 的 gain/black 减除）
    for name, v in (("exposure_stops", stops), ("black_level", black)):
        if v != v or v in (float("inf"), float("-inf")):
            raise ToposTrwmError(f"{name} 须为有限值: {v!r}")
    return {
        "version": (int(major), int(minor)),
        "wb_gain_r": float(wb_r),
        "wb_gain_g": float(wb_g),
        "wb_gain_b": float(wb_b),
        "exposure_stops": float(stops),
        "black_level": float(black),
        "cfa_layout": CFA_LAYOUT_NAMES[layout],
        "sensor_matrix_id": int(matrix_id),
    }
