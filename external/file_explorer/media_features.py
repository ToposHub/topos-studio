"""[V3/GAP-08+09] 媒体感知特征：64bit 感知哈希（pHash）+ 自动主色桶。

- pHash：DCT 感知哈希（32x32 灰度 → 低频 8x8 → 中值阈值位图），用于
  "查找相似"（同图不同压缩/尺寸/微调色）。自实现 numpy DCT，不引入
  imagehash 依赖；numpy 缺失时优雅降级为 None。
- 主色桶：16x16 缩样 HSV 直方图，按色相 30° 分 12 桶（0-11）；
  低饱和/极暗/极亮像素计入灰阶，灰阶占比过半 → 桶 = -1。

输入统一取自已生成的缩略图文件（缩略图管线一次解码两用，零额外解码）。
"""
import logging

from PIL import Image

logger = logging.getLogger(__name__)

_PHASH_N = 32
_PHASH_GRID = 8


def compute_phash(img):
    """PIL.Image → 16 位 hex pHash；失败/无 numpy 返回 None。"""
    try:
        import numpy as np
    except ImportError:
        return None
    try:
        with img:
            small = img.convert('L').resize((_PHASH_N, _PHASH_N), Image.LANCZOS)
        arr = np.asarray(small, dtype=np.float64)
        n = _PHASH_N
        k = np.arange(n).reshape(-1, 1)
        m = np.arange(n).reshape(1, -1)
        c = np.cos(np.pi * (2 * m + 1) * k / (2 * n))
        c[0, :] = c[0, :] / np.sqrt(2.0)
        d = c @ arr @ c.T
        low = d[:_PHASH_GRID, :_PHASH_GRID].flatten()
        med = np.median(low[1:])  # 排除 DC 分量取中值
        bits = low > med
        val = 0
        for b in bits:
            val = (val << 1) | int(bool(b))
        return f'{val:016x}'
    except Exception:
        return None


def color_bucket(img):
    """PIL.Image → 主色桶 0-11（色相 30° 分桶），-1 = 灰阶；失败 None。

    用彩色像素的均值色判定单一桶：逐像素投票会被 JPEG 色度噪声把红色
    打进相邻桶。桶边界偏移 15°，正红（0°）落在桶 0 中央——否则 ±2 的
    b/g 噪声会把 0.3° 环绕成 359.7° 掉进品红桶。"""
    import colorsys
    try:
        with img:
            small = img.convert('RGBA').resize((16, 16))
            pixels = list(small.getdata())
    except Exception:
        return None
    rs = gs = bs = 0
    colored = 0
    total = 0
    gray = 0
    for r, g, b, a in pixels:
        if a < 128:
            continue
        total += 1
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        if s < 0.12 or v < 0.08 or v > 0.97:
            gray += 1
            continue
        rs += r
        gs += g
        bs += b
        colored += 1
    if total == 0:
        return None
    if gray > total * 0.6 or colored == 0:
        return -1
    h, s, v = colorsys.rgb_to_hsv(rs / colored / 255.0, gs / colored / 255.0, bs / colored / 255.0)
    if s < 0.12 or v < 0.08 or v > 0.97:
        return -1
    return int(((h * 360 + 15) % 360) // 30) % 12


def features_from_file(path):
    """缩略图文件 → (phash, bucket)。一次解码两用。"""
    try:
        with Image.open(path) as img:
            return compute_phash(img), color_bucket(img)
    except Exception:
        return None, None


def hamming_distance(hex_a: str, hex_b: str) -> int:
    """两个 hex pHash 的汉明距离。"""
    try:
        return bin(int(hex_a, 16) ^ int(hex_b, 16)).count('1')
    except (TypeError, ValueError):
        return 64
