"""
Topos Video Codec 媒体辅助层（无 Qt 依赖）。

从 utils.py 迁入的绑定层管线：元数据面板 + 首帧缩略图。
供插件 setup() 注册到 utils 的 provider 注册表，也可独立使用。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 插件包内运行时确保仓库共享层（topos_binding）可导入
_REPO_ROOT = str(Path(__file__).resolve().parents[4])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# —— H.273 码值 → 显示名（元数据面板用；与 AVCodecContext 枚举同源） ——
_MATRIX_NAMES = {1: 'BT.709', 5: 'BT.601', 6: 'SMPTE 170M', 9: 'BT.2020 NC'}
_TRANSFER_NAMES = {1: 'BT.709', 8: 'Linear', 16: 'PQ (HDR)', 18: 'HLG (HDR)'}


def topos_color_info(info) -> str:
    """info() → 元数据面板的 Color Info 字符串。"""
    m = _MATRIX_NAMES.get(info.color_matrix, f'matrix={info.color_matrix}')
    t = _TRANSFER_NAMES.get(info.color_transfer, f'transfer={info.color_transfer}')
    rng = 'Full' if info.color_range == 1 else 'Limited'
    return f'{m} / {t} / {rng}'


def _topos_session(path):
    from .topos_player import ToposMovieSession
    return ToposMovieSession(path, slice_workers=1)


def get_topos_metadata(path):
    """Topos 元数据 → 前端属性面板键（script.js isVideo 判定依赖这些键名）"""
    try:
        from .topos_player import topos_color_info
        sess = _topos_session(path)
        try:
            info = sess.info
            meta = {
                'Codec': 'Topos Video Codec 2.0',
                'Resolution': f"{info.width}x{info.height}",
                'Frame Rate': f"{sess.fps:.2f} fps",
                'Bit Depth': f"{info.bit_depth}-bit",
                'Color Info': topos_color_info(info),
            }
            dur = sess.duration_sec
            if dur > 0:
                meta['Bitrate'] = f"{os.path.getsize(path) * 8 / dur / 1e6:.0f} Mbps"
            return meta
        finally:
            sess.close()
    except Exception:
        return {}


# 三档尺寸/质量，与 thumb_engine.VIDEO_SIZE_SPECS 保持一致（无 import 依赖）
_THUMB_SPECS = (('.webp', 300, 60), ('_lg.webp', 600, 70), ('_preview.webp', 1920, 80))


def generate_topos_thumbnail(path, thumb_path):
    """Topos 首帧 → WEBP 缩略图（解码帧 0，全 intra 无需定位）。

    一次解码产出三档尺寸：thumb_path 是 small 目标路径，
    large/preview 由同 stem 后缀推导，与 thumb_engine 缓存命名一致。
    """
    try:
        from PIL import Image
        sess = _topos_session(path)
        try:
            img = Image.fromarray(sess.frame_rgb(0))
        finally:
            sess.close()
        stem = thumb_path[:-len('.webp')] if thumb_path.endswith('.webp') else thumb_path
        for suffix, px, quality in _THUMB_SPECS:
            out = stem + suffix
            out_dir = os.path.dirname(out)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            sized = img.copy()
            sized.thumbnail((px, px))
            part = out + '.part'
            sized.save(part, 'WEBP', quality=quality)
            os.replace(part, out)  # 原子替换，避免半成品被前端命中
        return True
    except Exception:
        return False
