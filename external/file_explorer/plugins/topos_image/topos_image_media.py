"""Native preview/metadata helpers for Topos Image (.toos) files."""

from __future__ import annotations

import logging
import os

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

_SIZE_SPECS = {
    "small": (300, 60),
    "large": (600, 70),
    "preview": (1920, 80),
}


def _size_for_target(target: str) -> tuple[int, int]:
    name = os.path.basename(target)
    if name.endswith("_preview.webp"):
        return _SIZE_SPECS["preview"]
    if name.endswith("_lg.webp"):
        return _SIZE_SPECS["large"]
    return _SIZE_SPECS["small"]


def _matrix_and_range(info):
    from src.shared.color.types import ColorMatrix, ColorRange

    matrix = {
        5: ColorMatrix.BT601_PAL,
        6: ColorMatrix.BT601,
        9: ColorMatrix.BT2020_NC,
    }.get(int(info.color_matrix), ColorMatrix.BT709)
    range_ = ColorRange.FULL if int(info.color_range) == 1 else ColorRange.LIMITED
    return matrix, range_


def _planes_to_rgb(planes, info):
    """Convert decoded Topos planes into an RGB/RGBA uint8 array."""
    import numpy as np

    if len(planes) < 3:
        raise ValueError(f"Topos image has too few planes: {len(planes)}")

    if int(info.pixel_format) == 2:  # GBR planar, spec order is G,B,R.
        if int(getattr(info, "sample_kind", 0)) == 1:
            # M1-F6：HALF 浮点样本域——线性值平面（无满刻度契约），先做
            # 线性→sRGB 显示编码再量化；不做整数归一化（half 位型 / 65535
            # 会产出垃圾像素）。
            def _linear_to_srgb(a):
                lin = np.maximum(np.asarray(a, dtype=np.float32), 0.0)
                return np.where(lin <= 0.0031308,
                                lin * 12.92,
                                1.055 * np.power(lin, 1.0 / 2.4) - 0.055)

            g, b, r = (_linear_to_srgb(planes[i]) for i in range(3))
            rgb = np.stack((r, g, b), axis=-1)
        else:
            scale = float((1 << int(info.bit_depth)) - 1)
            if scale <= 0:
                raise ValueError(f"invalid Topos bit depth: {info.bit_depth}")
            g, b, r = (np.asarray(planes[i], dtype=np.float32) / scale
                       for i in range(3))
            rgb = np.stack((r, g, b), axis=-1)
    elif int(info.pixel_format) in (0, 1):  # YUV 4:2:2 or 4:4:4.
        from src.shared.media.color_decode import ColorConverter

        matrix, range_ = _matrix_and_range(info)
        rgb = ColorConverter().yuv_to_rgb(
            np.asarray(planes[0]), np.asarray(planes[1]), np.asarray(planes[2]),
            matrix, range_, int(info.bit_depth),
        )
    else:
        raise ValueError(f"unsupported Topos pixel format: {info.pixel_format}")

    rgb8 = (np.clip(rgb, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    if bool(info.has_alpha) and len(planes) >= 4:
        from src.shared.codec.topos_image_binding import (
            TC_IMG_SAMPLE_KIND_HALF,
        )

        if int(getattr(info, "sample_kind", 0)) == TC_IMG_SAMPLE_KIND_HALF:
            # HALF alpha 平面已是 [0,1] 浮点（decode_float 路径）——
            # 不能按 16-bit 码值 /65535 归一（2026-09-21 复查）
            a_f = np.asarray(planes[3], dtype=np.float32)
        else:
            # Alpha is stored in a full 16-bit container, independently of
            # the color bit depth (Topos Image spec §8.6).
            a_f = np.asarray(planes[3], dtype=np.float32) / 65535.0
        alpha = (np.clip(a_f, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
        rgb8 = np.concatenate((rgb8, alpha[..., None]), axis=-1)
    return rgb8, info


def _decode_rgb(path: str, target_size: tuple[int, int] | None = None):
    """Decode one .toos image into an RGB/RGBA uint8 numpy array."""
    from src.shared.codec.topos_image_binding import (
        TC_IMG_SAMPLE_KIND_HALF,
        get_image_codec,
    )

    codec = get_image_codec()
    # 2026-09-21 复查：HALF 文件先 probe 再分流（此前整数 decode 白跑
    # 一次后丢弃再 decode_float——每张浮点缩略图双倍解码）。
    try:
        sample_kind = int(getattr(codec.probe(path), "sample_kind", 0))
    except Exception:
        sample_kind = 0
    if sample_kind == TC_IMG_SAMPLE_KIND_HALF:
        planes, info, _extra = codec.decode_float(
            path, target_size=target_size)
    else:
        planes, info, _extra = codec.decode(path, target_size=target_size)
    if int(getattr(info, "pixel_format", 0)) == 3:
        # W5：TRAW CFA（pf=3，4 相位平面）——CPU debayer（LOG0 逆曲线/
        # linear 归档双模式 + gamma OOTF，与 GPU 预览同式）；输出 RGB
        # uint8，无 alpha 域。target_size 由 native pf=3 缩放解码承接
        # （预览平面几何 ceil(target/2)，binding decode 内分派）。
        from src.shared.video.cfa_cpu_debayer import debayer_cfa_planes

        bd = int(getattr(info, "bit_depth", 12) or 12)
        rgb_f = debayer_cfa_planes(
            planes, int(info.visible_width), int(info.visible_height),
            bit_depth=bd, display_ootf=True)
        return (np.clip(rgb_f, 0.0, 1.0) * 255.0).astype(np.uint8)
    return _planes_to_rgb(planes, info)


def _atomic_save(image: Image.Image, target: str, size: int, quality: int) -> None:
    image = image.copy()
    image.thumbnail((size, size), Image.Resampling.LANCZOS)
    part = target + ".part"
    try:
        image.save(part, "WEBP", quality=quality, method=0)
        os.replace(part, target)
    finally:
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass


def generate_topos_image_thumbnail(source_path: str, thumb_path: str) -> bool:
    """Generate one explorer thumbnail for a `.toos` source file."""
    try:
        size, quality = _size_for_target(thumb_path)
        rgb, _info = _decode_rgb(source_path, target_size=(size, size))
        mode = "RGBA" if rgb.shape[-1] == 4 else "RGB"
        image = Image.fromarray(rgb, mode=mode)
        _atomic_save(image, thumb_path, size, quality)
        return os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0
    except Exception as exc:
        logger.warning("Topos .toos thumbnail failed for %s: %s", source_path, exc)
        return False


def get_topos_image_metadata(source_path: str) -> dict:
    """Return the metadata shape expected by ``external/file_explorer``."""
    from src.shared.codec.topos_image_binding import get_image_codec

    info = get_image_codec().probe(source_path)
    pixel_format = {
        0: "YUV 4:2:2",
        1: "YUV 4:4:4",
        2: "GBR 4:4:4",
        3: "RAW CFA",
    }.get(int(info.pixel_format), f"pixel_format {info.pixel_format}")
    profile = {
        0: "Preview",
        1: "HQ",
        2: "XQ",
        3: "RAW",
    }.get(int(info.image_profile), f"profile {info.image_profile}")
    return {
        "Codec": "Topos Image",
        "Format": "Topos Image (.toos)",
        "Resolution": f"{info.visible_width}x{info.visible_height}",
        "Bit Depth": f"{info.bit_depth}-bit",
        "Color Info": f"{pixel_format} · {profile}",
        "Channels": "RGBA" if info.has_alpha else "RGB",
    }
