"""TOPOS-backed payload codec for the Composite node disk cache (P0-7).

The A6 node disk cache historically stored RGBA float16 npz payloads.
P0-7/P1-11 direct persistent Composite caches to the native TOPOS
formats: single-frame node results map to ``.toos`` (TPIM envelope)
written with the Topos 4444 family's planar GBR+alpha 12-bit layout
(frame: profile 5 / pixel_format GBR / bd12 / straight alpha (lossless 16-bit); TPIM
image profile 2 "XQ" — the same combination the image-sequence export
path uses, so every combination is inside the native IDSC's domain).

Payload policy:
- LDR RGBA ([0, 1]) maps exactly onto the 12-bit fixed range; the
  residual quantization (≤ 1/4095 ≈ 2.4e-4) is below 8-bit display
  resolution — visually exact.
- HDR payloads (any value outside [0, 1]) are NOT representable in the
  12-bit fixed range; :func:`encode_toos_payload` rejects them so the
  store can fall back to the lossless npz payload instead of clipping.

The native library is optional: when it cannot be loaded the caller
falls back to npz (the store treats this codec as one payload format
among several, keyed in the entry manifest).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

FORMAT_ID = "topos_gbrap12a16"
# G6：HDR/超界载荷的 HALF 载体（image_profile 4，spec §15）
FORMAT_ID_FLOAT = "topos_half_f16"
FILE_SUFFIX = ".toos"
_BIT_DEPTH = 12
_MAX_CODE = (1 << _BIT_DEPTH) - 1  # 4095
# AUD-R2-006：float16 最大有限值。HALF .toos 样本域按 spec §15 直通不
# 裁剪，超过 ±65504 的 HDR 值会在 f16 转换中变 inf 并毒化恢复侧的每
# 个下游消费方——饱和必须在缓存存储边界完成（与 npz 路径
# disk_cache_store._write_payload 的 clip 同一契约）。
_F16_MAX = 65504.0
# T-P1-71：alpha 走无损 16-bit（native alpha_mode=1）。此前 mode=2 受限
# 近似（≤8 code 偏差）违背 P0-7"visually exact"承诺；GBR 平面仍为 12-bit
# 直通（diff 实测 = 0）。
_ALPHA_BIT_DEPTH = 16
_ALPHA_MAX_CODE = (1 << _ALPHA_BIT_DEPTH) - 1  # 65535


@dataclass(frozen=True)
class ToposPayloadInfo:
    """Static capability facts recorded into cache manifests (P0-7)."""

    available: bool
    format_id: str = FORMAT_ID
    bit_depth: int = _BIT_DEPTH
    alpha_bit_depth: int = _ALPHA_BIT_DEPTH
    layout: str = "gbrap planar"
    abi_version: int = 0


_probe_lock = threading.Lock()
_probe_result: Optional[ToposPayloadInfo] = None


def probe_topos_payload_codec() -> ToposPayloadInfo:
    """Probe the native TOPOS image codec once per process."""
    global _probe_result
    if _probe_result is not None:
        return _probe_result
    with _probe_lock:
        if _probe_result is not None:
            return _probe_result
        info = ToposPayloadInfo(available=False)
        try:
            from src.shared.codec.topos_image_binding import (
                check_image_codec_available,
            )
            if check_image_codec_available():
                from src.shared.codec.topos_binding import (
                    TOPOS_CODEC_ABI_VERSION,
                )
                info = ToposPayloadInfo(
                    available=True,
                    abi_version=int(TOPOS_CODEC_ABI_VERSION),
                )
        except Exception as exc:  # missing/incompatible native library
            logger.info("TOPOS payload codec unavailable (%s); npz fallback",
                        exc)
        _probe_result = info
        return _probe_result


def _rgba_to_planes(rgba: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Map a top-down RGBA float32 array to G/B/R/A u16 planes.

    RGB uses the full 12-bit code range; alpha is straight coverage on
    the same scale.  Returns planes in the codec's G,B,R,A order.

    T-P1-71（磁盘缓存静默像素损坏修复）：必须落成 uint16——此前
    ``np.rint`` 返回 float32，``tobytes()`` 后 float 字节被当 u16 像素
    供给编码器，缓存载荷往返全部变垃圾（测试此前因套件崩溃不可达）。
    """
    codes = np.rint(np.clip(rgba[:, :, :3], 0.0, 1.0)
                    * _MAX_CODE).astype(np.uint16)
    a = np.rint(np.clip(rgba[:, :, 3], 0.0, 1.0)
                * _ALPHA_MAX_CODE).astype(np.uint16)
    g = np.ascontiguousarray(codes[:, :, 1])
    b = np.ascontiguousarray(codes[:, :, 2])
    r = np.ascontiguousarray(codes[:, :, 0])
    return g, b, r, np.ascontiguousarray(a)


def _planes_to_rgba(g: Any, b: Any, r: Any, a: Any,
                    h: int, w: int,
                    rgb_max: int = _MAX_CODE,
                    alpha_max: int = _ALPHA_MAX_CODE) -> np.ndarray:
    """Inverse of :func:`_rgba_to_planes` → top-down RGBA f32.

    rgb_max/alpha_max 由文件 info 的 bit_depth/alpha_bit_depth 决定
    （GBR 12-bit、alpha 无损 16-bit），不能按固定 4095 一刀切。
    """
    g = np.asarray(g, dtype="<u2").reshape(h, w).astype(np.float32)
    b = np.asarray(b, dtype="<u2").reshape(h, w).astype(np.float32)
    r = np.asarray(r, dtype="<u2").reshape(h, w).astype(np.float32)
    a = np.asarray(a, dtype="<u2").reshape(h, w).astype(np.float32)
    rgb = np.stack([r, g, b], axis=-1) / float(rgb_max)
    alpha = a[:, :, np.newaxis] / float(alpha_max)
    return np.concatenate([rgb, alpha], axis=-1).astype(np.float32)


def payload_is_ldr(rgba: np.ndarray) -> bool:
    """True when the payload fits the 12-bit fixed range without clipping."""
    if rgba.size == 0:
        return True
    return bool(rgba[:, :, :3].min() >= 0.0
                and rgba[:, :, :3].max() <= 1.0
                and rgba[:, :, 3].min() >= 0.0
                and rgba[:, :, 3].max() <= 1.0)


def encode_toos_payload(path: str, rgba: np.ndarray) -> Dict[str, Any]:
    """Write one top-down RGBA f32 payload as a single-frame .toos file.

    Returns a manifest fragment (P0-7 fields).  Raises on failure or for
    out-of-range (HDR) payloads — the caller decides the fallback.
    """
    if rgba.ndim != 3 or rgba.shape[2] < 4:
        raise ValueError(f"payload must be HxWx≥4 RGBA, got {rgba.shape}")
    if not payload_is_ldr(rgba):
        raise ValueError("HDR payload outside [0, 1] is not representable "
                         "in the 12-bit TOPOS range; use the npz payload")
    h, w = rgba.shape[:2]
    from src.shared.codec.topos_binding import (
        TOPOS_CODEC_ABI_VERSION, _CFrameConfig, ToposCodec,
    )
    from src.shared.codec.topos_image_binding import get_image_codec

    planes = _rgba_to_planes(rgba)
    fc = _CFrameConfig()
    import ctypes as _ctypes
    fc.struct_size = _ctypes.sizeof(fc)
    fc.abi_version = TOPOS_CODEC_ABI_VERSION
    fc.visible_width = w
    fc.visible_height = h
    fc.profile = 5              # 4444 family
    fc.pixel_format = 2         # GBR planar
    fc.bit_depth = _BIT_DEPTH
    fc.qp_base = 0              # finest tier for cache payloads
    # G5：显式钉 V7-R2（em 8，ADR-C038 产品默认）——ctypes 零初始化会隐式
    # 落 V1（Rice）熵，放弃 rans2 压缩收益（缓存文件偏大）；全仓最后一个
    # 隐式代际写点，钉死后与其他写面口径一致。
    fc.reserved[0] = 8
    fc.alpha_mode = 1           # 无损 alpha（mode=2 近似 ≤8 code 违背承诺）
    fc.alpha_bit_depth = _ALPHA_BIT_DEPTH
    fc.alpha_premultiplied = 0  # straight (app compositing convention)
    codec = ToposCodec()
    # G5 收尾（2026-09-21 复查）：binding 支持指针直传（G7 同款）——去掉
    # 每帧 4× 全帧 tobytes() 序列化拷贝
    packet, _stats = codec.encode_frame(
        fc, [np.ascontiguousarray(p) for p in planes])
    file_bytes = get_image_codec().write_packet(path, packet, 2)
    return {
        "format": FORMAT_ID,
        "width": int(w), "height": int(h),
        "codec": "topos", "profile": "4444",
        "pix_layout": "gbrap planar",
        "bit_depth": _BIT_DEPTH, "alpha_bit_depth": _ALPHA_BIT_DEPTH,
        "alpha_mode": "straight",
        "abi_version": int(TOPOS_CODEC_ABI_VERSION),
        # AUD-8505: 键名与消费方（disk_cache_store 读取 "bytes"）对齐，
        # 原名 "file_bytes" 使 override 成为死代码。数值仍为 payload 文件
        # 实际字节数，启用 override 不改变既有行为。
        "bytes": int(file_bytes),
    }


def encode_toos_payload_float(path: str, rgba: np.ndarray) -> Dict[str, Any]:
    """G6：HDR/超界 payload → HALF .toos 单帧（image_profile 4，spec §15）。

    替代 npz f16 回退——同精度（f16 存储）但经 rans2 压缩（缓存体积
    显著下降）。编码走 ToposImageSequenceEncoder（gbrph16 链，M1 验证）：
    topos_gbraph16a16 + crf=20（HDR 实测无损窗口中点，f16 域逐位往返）；
    临时目录建在目标同级避免跨盘 rename。
    """
    import os as _os
    import shutil as _shutil
    import tempfile as _tempfile

    if rgba.ndim != 3 or rgba.shape[2] < 4:
        raise ValueError(f"payload must be HxWx≥4 RGBA, got {rgba.shape}")
    h, w = rgba.shape[:2]
    from src.shared.export.topos_encoder import ToposImageSequenceEncoder
    from src.shared.export.video_encoder import FFmpegEncoderConfig
    from src.shared.media.frame import DecodedFrame
    from src.shared.media.metadata import Rational

    parent = _os.path.dirname(_os.path.abspath(path))
    _os.makedirs(parent, exist_ok=True)
    tmp_dir = _tempfile.mkdtemp(prefix=".topos-cache-half-", dir=parent)
    try:
        cfg = FFmpegEncoderConfig(
            codec="topos_image_sequence", width=w, height=h,
            fps=Rational(24, 1), pix_fmt="topos_gbraph16a16",
            profile="4444", crf=20, has_alpha=True,
        )
        enc = ToposImageSequenceEncoder(_os.path.join(tmp_dir, "frame"), cfg)
        enc.open()
        # AUD-8501：encode_frame 的转换/编码错误会被包装成 RuntimeError
        # 再抛（topos_encoder.py）。open() 已创建 native GOP context 与
        # mux 文件，异常路径必须 abort()（幂等，丢弃未提交事务并释放
        # native 资源），否则泄漏只能依赖 GC 兜底，HALF 链路持续故障时
        # 每次 store 尝试都重复泄漏一个实例。
        try:
            # AUD-R2-006：写入前饱和到 ±f16 max——超界 HDR 值（如 1e5）在
            # HALF 载体内变 inf，恢复侧拿到 inf 会污染下游（GPU 纹理/统计）。
            frame_rgba = np.ascontiguousarray(
                np.clip(rgba[:, :, :4], -_F16_MAX, _F16_MAX), dtype=np.float32)
            enc.encode_frame(DecodedFrame(
                frame_index=0, pts=0, time_seconds=0.0, data=frame_rgba,
                pixel_format="rgba", width=w, height=h))
            enc.close()
        except Exception:
            try:
                enc.abort()
            except Exception:
                pass
            raise
        produced = _os.path.join(tmp_dir, "frame", "frame_0001.toos")
        if not _os.path.isfile(produced):
            raise RuntimeError("HALF cache encode produced no frame file")
        _os.replace(produced, path)
    finally:
        _shutil.rmtree(tmp_dir, ignore_errors=True)
    return {
        "format": FORMAT_ID_FLOAT,
        "width": int(w), "height": int(h),
        "codec": "topos", "profile": "4444-half",
        "pix_layout": "gbrap half planar",
        "bit_depth": 16, "alpha_bit_depth": 16,
        "alpha_mode": "straight",
        # AUD-8505: "file_bytes" → "bytes"（与消费方 disk_cache_store 对齐）
        "bytes": int(_os.path.getsize(path)),
    }


def decode_toos_payload(path: str, width: int, height: int,
                        target_size: "tuple[int, int] | None" = None
                        ) -> np.ndarray:
    """Read a .toos payload written by :func:`encode_toos_payload` /
    :func:`encode_toos_payload_float`（G6：按 sample_kind 分派）。

    G13：``target_size`` 透传缩放解码（native reduced/python 回退，与
    图片 decode 同语义）——低分辨率消费方（预览回退等）不再强制全尺寸
    读回；``width``/``height`` 仍为 manifest 声明的全尺寸（组装按缩放后
    info 实际几何）。
    """
    from src.shared.codec.topos_image_binding import (
        TC_IMG_SAMPLE_KIND_HALF, get_image_codec,
    )
    codec = get_image_codec()
    probe = codec.probe(path)
    if int(getattr(probe, "sample_kind", 0)) == TC_IMG_SAMPLE_KIND_HALF:
        planes, info, _pts = codec.decode_float(path, target_size=target_size)
        rgb = np.stack([planes[2], planes[0], planes[1]],
                       axis=-1).astype(np.float32)
        if len(planes) >= 4:
            alpha = planes[3].astype(np.float32)[:, :, None]
        else:
            alpha = np.ones_like(rgb[:, :, :1])
        return np.concatenate([rgb, alpha], axis=-1).astype(np.float32)
    planes, info, _pts = codec.decode(path, target_size=target_size)
    if len(planes) < 4:
        raise ValueError(f"expected 4 GBR+A planes, got {len(planes)}")
    rgb_max = (1 << int(getattr(info, "bit_depth", _BIT_DEPTH))) - 1
    alpha_max = (1 << int(getattr(
        info, "alpha_bit_depth", _ALPHA_BIT_DEPTH))) - 1
    out_h, out_w = planes[0].shape[:2]
    return _planes_to_rgba(planes[0], planes[1], planes[2], planes[3],
                           out_h, out_w, rgb_max=rgb_max, alpha_max=alpha_max)
