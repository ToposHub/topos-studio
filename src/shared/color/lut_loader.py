"""统一 LUT 加载与采样（P1-COLOR-11）。

收敛此前分散在 the_decompiler / lut_exporter / filter_evaluator 的
三份 .cube 解析，补齐 .3dl（Lustre）格式与 tetrahedral 精确采样。

约定（与现有 GPU 路径一致，勿改）：
- ``table`` 形状 (N, N, N, 3)，轴序 (B, G, R)，即 .cube 文件行序
  ``for b: for g: for r:`` 的直接 reshape；
- 数值域 [0, 1] float32；
- CPU tetrahedral 用于导出/精确路径，GPU 侧保持硬件三线性
  （两者误差由测试锁定）。

已知驱动行为（2026-08 实测，AMD Radeon RX 6900 XT / macOS）：
ModernGL ``texture3d.repeat_* = False`` 生效为 GL_CLAMP 而非
CLAMP_TO_EDGE——直接用输入值当纹理坐标会把边缘半 texel 重映射，
有效插值区间为 [0.5/N, 1-0.5/N]。生产 shader 已修复：采样坐标先做
texel 中心映射 ``t = ((N-1)*c + 0.5)/N``（shader_nodes.LUT3D_SNIPPET
与两处 apply_lut_hdr_safe），与 CPU 网格点对齐语义一致；GPU/CPU
parity 由 test_lut_loader.py 锁定在帧缓冲量化级。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np


@dataclass
class LoadedLut:
    """解析后的 3D LUT 及其 metadata。"""

    table: np.ndarray                 # (N, N, N, 3) float32 [0,1]，轴序 (B,G,R)
    size: int
    format: str                       # 'cube' | '3dl'
    title: Optional[str] = None
    description: Optional[str] = None
    domain_min: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    domain_max: Tuple[float, float, float] = (1.0, 1.0, 1.0)
    shaper_1d: Optional[np.ndarray] = None  # (M,) float32，cube 的 1D 预表
    metadata: Dict[str, str] = field(default_factory=dict)
    source_path: Optional[str] = None

    def describe(self) -> str:
        parts = [f"{self.size}³", self.format]
        if self.title:
            parts.insert(0, self.title)
        if self.shaper_1d is not None:
            parts.append(f"+1D({len(self.shaper_1d)})")
        return " · ".join(parts)


def parse_cube(text: str, source_path: Optional[str] = None) -> LoadedLut:
    """解析 .cube 文本（IRIDAS/Adobe 扩展 metadata 全保留）。"""
    size = 0
    one_d_size = 0
    title = None
    description = None
    domain_min = (0.0, 0.0, 0.0)
    domain_max = (1.0, 1.0, 1.0)
    extra_meta: Dict[str, str] = {}
    table_rows: List[List[float]] = []
    shaper_rows: List[float] = []
    # 解析状态：默认读 3D 表；遇到 LUT_1D_SIZE 后读 1D；遇到 LUT_3D_SIZE
    # 切回 3D（cube 规范：shaper 在 3D 段之前）
    section = '3d'

    for raw in text.splitlines():
        line = raw.split('#')[0].strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith('TITLE'):
            title = line.split(None, 1)[1].strip().strip('"') \
                if len(line.split(None, 1)) > 1 else None
            continue
        if upper.startswith(('DESCRIPTION', 'COMMENT')):
            key = line.split(None, 1)[0].upper()
            extra_meta[key] = line.split(None, 1)[1].strip().strip('"') \
                if len(line.split(None, 1)) > 1 else ''
            if key == 'DESCRIPTION':
                description = extra_meta[key]
            continue
        if upper.startswith('LUT_1D_SIZE'):
            one_d_size = int(line.split()[1])
            section = '1d'
            continue
        if upper.startswith('LUT_3D_SIZE'):
            size = int(line.split()[1])
            section = '3d'
            continue
        if upper.startswith('DOMAIN_MIN'):
            parts = line.split()
            domain_min = tuple(float(v) for v in parts[1:4])
            continue
        if upper.startswith('DOMAIN_MAX'):
            parts = line.split()
            domain_max = tuple(float(v) for v in parts[1:4])
            continue
        if upper.startswith('LUT_3D_INPUT_RANGE'):
            parts = line.split()
            if len(parts) >= 3:
                domain_min = (float(parts[1]),) * 3
                domain_max = (float(parts[2]),) * 3
            continue
        if line[0].isalpha() or line[0] == '_':
            # 未知关键字行（未来扩展），进 metadata 不报错
            key = line.split(None, 1)[0].upper()
            extra_meta[key] = line[len(key):].strip().strip('"')
            continue
        parts = line.split()
        try:
            values = [float(v) for v in parts]
        except ValueError:
            continue
        if section == '1d':
            if len(values) >= 1:
                shaper_rows.append(values[0])
        elif len(values) == 3:
            table_rows.append(values)

    if size <= 0 or len(table_rows) != size ** 3:
        raise ValueError(
            f"Invalid .cube: size={size}, points={len(table_rows)}")
    table = np.asarray(table_rows, dtype=np.float32).reshape(size, size, size, 3)
    shaper = None
    if one_d_size > 0 and len(shaper_rows) == one_d_size:
        shaper = np.asarray(shaper_rows, dtype=np.float32)
    elif shaper_rows:
        raise ValueError(
            f"Invalid .cube 1D shaper: size={one_d_size}, "
            f"points={len(shaper_rows)}")
    return LoadedLut(
        table=table, size=size, format='cube', title=title,
        description=description, domain_min=domain_min,
        domain_max=domain_max, shaper_1d=shaper, metadata=extra_meta,
        source_path=source_path)


def parse_3dl(text: str, source_path: Optional[str] = None) -> LoadedLut:
    """解析 .3dl（Autodesk Lustre）。

    首行为网格尺寸（三个整数）；数据为行主序、R 变化最快，整数
    （位深由最大值推断）或浮点（直接 [0,1]）。
    """
    lines = [ln.split('#')[0].strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        raise ValueError("Empty .3dl")
    header = lines[0].split()
    try:
        dims = [int(v) for v in header]
    except ValueError:
        raise ValueError(f"Invalid .3dl header: {lines[0]!r}")
    if len(dims) == 1:
        dims = dims * 3
    if len(dims) != 3 or len(set(dims)) != 1:
        raise ValueError(f"Non-cubic .3dl unsupported: {dims}")
    size = dims[0]

    rows: List[List[float]] = []
    for line in lines[1:]:
        if line[0].isalpha():
            continue  # '#comment' 已剥；残余关键字行跳过
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            rows.append([float(v) for v in parts])
        except ValueError:
            continue
    if len(rows) != size ** 3:
        raise ValueError(
            f"Invalid .3dl: size={size}, points={len(rows)}")

    data = np.asarray(rows, dtype=np.float64)
    vmax = float(data.max())
    if vmax > 1.0 + 1e-9:
        # 整数编码：按最大值推断满量程（10-bit 常见）
        data = data / vmax
    table = data.astype(np.float32).reshape(size, size, size, 3)
    return LoadedLut(table=table, size=size, format='3dl',
                     source_path=source_path)


def load_lut(path: str) -> LoadedLut:
    """按扩展名（及内容探测）加载 LUT 文件。

    DESK-02B：*path* 也可以是 ``topos-res://`` 逻辑资源引用（手机交接
    包内嵌 LUT）。解析顺序：①引用经当前工程资源上下文物化为本地文件
    （内容已按 SHA-256 校验）；②外部路径直读。URI 无工程上下文或解析
    失败时按普通路径处理（最终 FileNotFoundError/解析错误如实上抛）。
    """
    if isinstance(path, str) and path.startswith("topos-res://"):
        from src.shared.project.resource_resolver import (
            resolve_project_reference,
        )
        local = resolve_project_reference(path)
        if local is None:
            raise FileNotFoundError(
                f"工程内嵌 LUT 不可用（无打开的工程或资源缺失）: {path}")
        path = local
    ext = os.path.splitext(path)[1].lower()
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        text = f.read()
    if ext == '.3dl':
        return parse_3dl(text, source_path=path)
    if ext == '.cube':
        return parse_cube(text, source_path=path)
    # 未知扩展：内容探测（cube 有 LUT_3D_SIZE 关键字）
    if 'LUT_3D_SIZE' in text or 'LUT_1D_SIZE' in text:
        return parse_cube(text, source_path=path)
    first = next((ln.split('#')[0].strip()
                  for ln in text.splitlines() if ln.strip()), '')
    parts = first.split()
    if len(parts) == 3 and all(p.lstrip('-').isdigit() for p in parts):
        return parse_3dl(text, source_path=path)
    return parse_cube(text, source_path=path)


def probe_lut_metadata(path: str, max_bytes: int = 8192) -> dict:
    """轻量探测 LUT 头部 metadata（不加载整表）。

    用于画廊扫描 tooltip：只读文件头，返回 title/size/format/
    description；解析失败返回空 dict（不抛）。
    """
    meta: Dict[str, object] = {}
    try:
        with open(path, 'rb') as f:
            head = f.read(max_bytes).decode('utf-8', errors='replace')
    except OSError:
        return meta
    ext = os.path.splitext(path)[1].lower()
    for raw in head.splitlines():
        line = raw.split('#')[0].strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith('TITLE') and 'title' not in meta:
            parts = line.split(None, 1)
            meta['title'] = parts[1].strip().strip('"') if len(parts) > 1 else ''
        elif upper.startswith('DESCRIPTION') and 'description' not in meta:
            parts = line.split(None, 1)
            meta['description'] = parts[1].strip().strip('"') if len(parts) > 1 else ''
        elif upper.startswith('LUT_3D_SIZE'):
            try:
                meta['size'] = int(line.split()[1])
            except (ValueError, IndexError):
                pass
    if 'size' not in meta and ext == '.3dl':
        first = next((ln.split('#')[0].strip()
                      for ln in head.splitlines() if ln.strip()), '')
        try:
            dims = [int(v) for v in first.split()]
            if dims:
                meta['size'] = dims[0] if len(set(dims)) == 1 else dims[-1]
        except ValueError:
            pass
    meta['format'] = ext.lstrip('.') or 'cube'
    return meta


def _apply_domain(table: np.ndarray, lut: LoadedLut,
                  rgb: np.ndarray) -> np.ndarray:
    """DOMAIN ≠ [0,1] 时把输入重映射到格点索引域。"""
    dmin = np.asarray(lut.domain_min, dtype=np.float64)
    dmax = np.asarray(lut.domain_max, dtype=np.float64)
    span = np.where(dmax - dmin > 0, dmax - dmin, 1.0)
    return np.clip((rgb - dmin) / span, 0.0, 1.0)


def resample_to_unit_domain(lut: LoadedLut) -> LoadedLut:
    """非单位 DOMAIN 的表重采样到 [0,1] 域，供 GPU [0,1] 采样路径使用。

    GPU 侧（async_renderer / image_engine）把输入 clamp 到 [0,1] 后直接
    作为纹理坐标；声明了 DOMAIN_MIN/MAX 的 HDR cube 必须先把表搬到
    单位域，否则预览与 CPU 采样（``_apply_domain``，导出用）不一致。
    重采样在原表格点上进行（u 网格经 domain 逆映射恰好落在格点上），
    三线性插值零误差。单位域表原样返回（同一对象）。
    """
    dmin = np.asarray(lut.domain_min, dtype=np.float64)
    dmax = np.asarray(lut.domain_max, dtype=np.float64)
    if np.allclose(dmin, 0.0) and np.allclose(dmax, 1.0):
        return lut
    axis = np.linspace(0.0, 1.0, lut.size)
    b_ax, g_ax, r_ax = np.meshgrid(axis, axis, axis, indexing='ij')
    u = np.stack([r_ax, g_ax, b_ax], axis=-1)   # (N,N,N,3)，RGB 顺序
    rgb_in = dmin + u * (dmax - dmin)
    table = sample_lut_trilinear(lut, rgb_in)
    return LoadedLut(
        table=np.ascontiguousarray(table, dtype=np.float32),
        size=lut.size, format=lut.format, title=lut.title,
        description=lut.description,
        domain_min=(0.0, 0.0, 0.0), domain_max=(1.0, 1.0, 1.0),
        shaper_1d=lut.shaper_1d, metadata=dict(lut.metadata),
        source_path=lut.source_path)


def sample_lut_trilinear(lut: LoadedLut, rgb: np.ndarray) -> np.ndarray:
    """CPU 三线性采样（与 GPU ``texture()`` 同算法，parity 基准）。

    输入 (..., 3) 是 RGB 顺序；table 轴序是 (B, G, R)，索引时换轴。
    """
    xyz = _apply_domain(lut.table, lut, np.asarray(rgb, dtype=np.float64))
    n = lut.size
    coord = xyz * (n - 1)
    i0 = np.clip(np.floor(coord).astype(np.int64), 0, n - 2)
    f = coord - i0
    # 输入通道 (R, G, B) → table 轴 (B, G, R)
    b0, g0, r0 = i0[..., 2], i0[..., 1], i0[..., 0]
    fb, fg, fr = f[..., 2, None], f[..., 1, None], f[..., 0, None]
    table = lut.table
    c000 = table[b0, g0, r0]
    c100 = table[b0 + 1, g0, r0]
    c010 = table[b0, g0 + 1, r0]
    c110 = table[b0 + 1, g0 + 1, r0]
    c001 = table[b0, g0, r0 + 1]
    c101 = table[b0 + 1, g0, r0 + 1]
    c011 = table[b0, g0 + 1, r0 + 1]
    c111 = table[b0 + 1, g0 + 1, r0 + 1]
    out = (c000 * (1 - fb) * (1 - fg) * (1 - fr)
           + c100 * fb * (1 - fg) * (1 - fr)
           + c010 * (1 - fb) * fg * (1 - fr)
           + c110 * fb * fg * (1 - fr)
           + c001 * (1 - fb) * (1 - fg) * fr
           + c101 * fb * (1 - fg) * fr
           + c011 * (1 - fb) * fg * fr
           + c111 * fb * fg * fr)
    return out.astype(np.float32)


def sample_lut_tetrahedral(lut: LoadedLut, rgb: np.ndarray) -> np.ndarray:
    """CPU tetrahedral 精确采样（Bentley 四面体插值，行业 CPU 标准）。

    输入 (..., 3)（RGB 顺序，值域按 DOMAIN），输出 (..., 3) float32。
    路径选择：frac 分量按大小降序走棱——每步把一个通道从 0 变 1，
    顶点 = 已走通道集合对应的格点偏移。
    """
    xyz = _apply_domain(lut.table, lut, np.asarray(rgb, dtype=np.float64))
    n = lut.size
    coord = xyz * (n - 1)
    i0 = np.clip(np.floor(coord).astype(np.int64), 0, n - 2)
    f = coord - i0
    # 输入通道 (R, G, B) → table 轴 (B, G, R)
    b, g, r = i0[..., 2], i0[..., 1], i0[..., 0]
    fb, fg, fr = f[..., 2], f[..., 1], f[..., 0]
    table = lut.table
    corners = {
        '':     table[b, g, r].astype(np.float64),
        'B':    table[b + 1, g, r].astype(np.float64),
        'G':    table[b, g + 1, r].astype(np.float64),
        'R':    table[b, g, r + 1].astype(np.float64),
        'BG':   table[b + 1, g + 1, r].astype(np.float64),
        'BR':   table[b + 1, g, r + 1].astype(np.float64),
        'GR':   table[b, g + 1, r + 1].astype(np.float64),
        'BGR':  table[b + 1, g + 1, r + 1].astype(np.float64),
    }
    fracs = {'B': fb, 'G': fg, 'R': fr}
    out = np.empty_like(corners[''])

    # 6 种排列（权重大的通道先走）
    orderings = (
        ('R', 'G', 'B'), ('G', 'R', 'B'),
        ('R', 'B', 'G'), ('B', 'R', 'G'),
        ('G', 'B', 'R'), ('B', 'G', 'R'),
    )
    a, c, d = orderings[0]
    assigned = (fr >= fg) & (fg >= fb)
    for order in orderings:
        if order == orderings[0]:
            mask = assigned
        else:
            x, y, z = order
            fx, fy, fz = fracs[x], fracs[y], fracs[z]
            mask = (fx >= fy) & (fy >= fz) & ~assigned
            assigned |= mask
        if not np.any(mask):
            continue
        prev = corners[''][mask]
        result = prev.copy()
        acc = ''
        for channel in order:
            acc += channel
            nxt = corners[''.join(sorted(acc))][mask]
            result += (nxt - prev) * fracs[channel][mask][:, None]
            prev = nxt
        out[mask] = result
    return out.astype(np.float32)
