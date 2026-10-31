"""
Topos Video Codec Media Source Module
自研 Topos MOV（TPIC）媒体源（阶段 6，ADR-C001 C-14 施工图落地）。

## 职责

- ToposMediaSource: 实现 MediaSource Protocol 的 Topos MOV 解码后端
- is_topos_movie: 结构化内容探测（factory auto 路由用，风险 R-17）
- check_topos_codec_available: libtopos_codec 可用性检查（带缓存）

## 设计原则

- 恒 planar 输出（YUV 4:2:2 [+ Alpha 全分辨率]，uint16 满刻度码值），
  与 yuv_gpu_upload R16UI 路径及 color_decode 满刻度约定一致（C-14 第 2 条）；
- 全 intra + 容器 O(1) 索引：seek 只是移动游标，无解码器状态需要冲洗；
- 顺序读前批量预取（阶段4 兑现）：顺序前进确认后一次 decode_views_batch
  批量解码深度自适应的若干帧（首帧即时返回、其余入缓存），seek/跳读自动
  失效回单帧路径；输出与逐帧解码逐位一致，不新增线程；
- 单实例不可重入（对齐 SourceManager._decode_lock / ClipSession.decode_lock
  的串行化假设）；ToposMovieFile 原生句柄不自带锁；
- 普通 MediaSource 接口不触碰 OpenGL/Qt，只产出 numpy 平面；显式的
  ``read_frame_metal`` 导出接口例外地创建 IOSurface→Metal 资源，供渲染线程消费；
- target_size 在新 native request/context 可用时直接申请目标平面；旧 dylib
  才回退纯 numpy 分离 box 滤波，放大请求回退原始分辨率。
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from typing import Any, Optional

import numpy as np

from src.shared.codec.topos_binding import (
    TC_AUDIO_CODEC_LPCM,
    TC_AUDIO_CODEC_MP4A,
    TC_AUDIO_FMT_FLOAT32,
    TC_AUDIO_FMT_INT,
    TC_DECODE_FLAG_DROP_ALPHA,
    TC_DECODE_FLAG_NO_DEBLOCK,
    TC_DECODE_MODE_AUTO_2K,
    TC_DECODE_MODE_FULL,
    TC_DECODE_MODE_REDUCED,
    TC_DECODE_SCALE_EIGHTH,
    TC_DECODE_SCALE_HALF,
    TC_DECODE_SCALE_QUARTER,
    TC_DECODE_SCALE_THIRD,
    TOPOS_CODEC_ABI_VERSION,
    ToposCodec,
    ToposCodecError,
    ToposDecoder,
    ToposGopContext,
    ToposFrame,
    ToposFrameInfo,
    ToposMovieFile,
    ToposMovieSummary,
)
from src.shared.codec.decode_worker_budget import DECODE_WORKER_BUDGET, DecodeWorkerLease
from src.shared.codec.topos_profiles import topos_tier_label_by_numeric_id

from .color_metadata import (
    map_color_primaries,
    map_color_space,
    map_color_transfer,
)
from .color_decode import (
    ToposColorMetadataError,
    ToposColorSemantics,
    resolve_topos_color_semantics,
)
from .frame import DecodedFrame, PlaneInfo
from .frame_path import DecodePath
from .metadata import Rational, VideoMetadata
from .source import MediaDecodeError, MediaOpenError, MediaSourceError

logger = logging.getLogger(__name__)

# —— libtopos_codec 共享实例（CDLL 加载 + 签名配置只做一次） ——
_CODEC_INSTANCE_LOCK = threading.Lock()
_CODEC_INSTANCE: Optional[ToposCodec] = None
_CODEC_UNAVAILABLE: Optional[str] = None  # 缓存失败原因，避免反复尝试加载

# 色彩码表（bitstream_spec §A.7，H.273 对齐）→ 名称复用 color_metadata 的
# map_*（1=bt709、6=smpte170m、9=bt2020、16=smpte2084、18=arib-std-b67 …）。
# 注意 color_range 是唯一不可复用 map_* 的字段：tpcC/帧头语义为 0=limited /
# 1=full，而 FFmpeg 枚举是 0=unspecified / 1=limited / 2=full，必须直接翻译。

# v1 profile 编号 → 展示名（bitstream_spec §4.1；profile 3 = Standard）
_PROFILE_NAMES: dict[int, str] = {
    3: 'Topos 422',
    5: 'Topos 4444',   # R4.4
    6: 'Topos 4444 XQ',   # R4.4
}

# 无 Alpha 输出与 PyAV 'yuv422p{10,12}le' 语义逐位一致（U/V = ceil(w/2)×h，
# uint16 满刻度），直接复用该注册名，GPU 上传链零新增接线（R4.1：12-bit）。
# R4.2/R4.3：pf=1（4:4:4）/pf=2（GBR）命名分支；未知组合显式抛错（W1，
# 不再静默回 4:2:2 10-bit）
_PIXEL_FORMAT_NO_ALPHA = {
    (0, 10): 'yuv422p10le', (0, 12): 'yuv422p12le',
    (0, 16): 'yuv422p16le',
    (1, 10): 'yuv444p10le', (1, 12): 'yuv444p12le',
    (1, 16): 'yuv444p16le',
    (2, 10): 'gbrp10le', (2, 12): 'gbrp12le',
    (2, 16): 'gbrp16le',
}

# 顺序读前批量预取（阶段4 兑现）：decode_views_batch 单批跨帧解码。
# D1 实测显示同步批量会把整批成本集中到首个显示帧，抬高 4K p95/p99；
# 因此播放路径默认关闭批量，导出路径仅自动使用有界深度，仍保留
# TOPOS_READAHEAD/read_ahead 显式 opt-in/override。
# 显式深度仍受“预取帧总平面字节数 ≤ 预算”约束（0=关闭、正数=固定深度）。
# P1-03：预算按"平面池条目（max(5, depth+2)）× 帧平面字节"记账，任何来
# 源的深度都不得突破；超预算覆盖必须显式设 TOPOS_UNSAFE_READAHEAD 真值
# 并记 warning（可见降级，审计 P1-03）。
_READAHEAD_BUDGET_BYTES = 160 << 20
_READAHEAD_MAX_DEPTH = 0
# 导出由顺序生产者 + 编码队列吸收首批尾延迟；小于 4K 目标的素材可获得
# 跨帧批处理收益，而 4K 4444 会因预算 cap 自动回到 0，不额外占用内存。
_READAHEAD_EXPORT_DEPTH = 4
_READAHEAD_HARD_MAX_DEPTH = 32
_READAHEAD_UNSAFE_ENV = 'TOPOS_UNSAFE_READAHEAD'
_TRUTHY = frozenset(('1', 'true', 'yes', 'on'))


def _get_shared_codec() -> ToposCodec:
    """进程级共享 ToposCodec（线程安全懒加载；失败原因按三态分类缓存）。

    T-P0-10：库解析经 ``resolve_topos_codec_library`` 收敛为
    "正确 release / 不兼容 / 不存在"三态——不兼容（旧库/损坏/缺符号）的
    失败消息携带 ``TOPOS_CODEC_INCOMPATIBLE`` 标记，调用方不得把该情形
    误报为"媒体损坏"。
    """
    global _CODEC_INSTANCE, _CODEC_UNAVAILABLE
    if _CODEC_INSTANCE is not None:
        return _CODEC_INSTANCE
    with _CODEC_INSTANCE_LOCK:
        if _CODEC_INSTANCE is not None:
            return _CODEC_INSTANCE
        if _CODEC_UNAVAILABLE is not None:
            raise ToposCodecError(_CODEC_UNAVAILABLE)
        try:
            from src.shared.codec.topos_binding import (
                LIB_RESOLUTION_INCOMPATIBLE,
                LIB_RESOLUTION_OK,
                load_library,
                resolve_topos_codec_library,
            )

            resolution = resolve_topos_codec_library()
            if resolution.status != LIB_RESOLUTION_OK:
                reason = ("TOPOS_CODEC_INCOMPATIBLE"
                          if resolution.status == LIB_RESOLUTION_INCOMPATIBLE
                          else "TOPOS_CODEC_LIB_NOT_FOUND")
                _CODEC_UNAVAILABLE = (
                    f"[{reason}] libtopos_codec 不可用: "
                    f"{resolution.detail or resolution.status}"
                )
                logger.warning(_CODEC_UNAVAILABLE)
                raise ToposCodecError(_CODEC_UNAVAILABLE)
            _CODEC_INSTANCE = ToposCodec(
                load_library(resolution.library_path))
        except ToposCodecError as exc:
            if _CODEC_UNAVAILABLE is None:
                _CODEC_UNAVAILABLE = str(exc)
            raise
        return _CODEC_INSTANCE


def check_topos_codec_available() -> bool:
    """libtopos_codec 是否可用（结果随单例缓存，供 factory 注册检查）。"""
    try:
        _get_shared_codec()
        return True
    except ToposCodecError as exc:
        logger.debug(f"libtopos_codec 不可用: {exc}")
        return False


def _iter_atoms(buf: bytes, start: int, end: int):
    """遍历 [start, end) 内的 MOV 兄弟 atom，yield (type4cc, body_start, body_end)。

    防御性解析（P0-06）：任何非法 size（< 头长、越界、截断）立即终止遍历，
    绝不抛异常——探测函数只允许返回 True/False。
    """
    off = start
    while off + 8 <= end:
        size = int.from_bytes(buf[off:off + 4], 'big')
        hdr = 8
        if size == 1:  # 64 位 largesize 形式
            if off + 16 > end:
                return
            size = int.from_bytes(buf[off + 8:off + 16], 'big')
            hdr = 16
        elif size == 0:  # size 到容器末尾
            size = end - off
        if size < hdr or off + size > end:
            return
        yield buf[off + 4:off + 8], off + hdr, off + size
        off += size


def _find_atom(buf: bytes, start: int, end: int, type4: bytes):
    for t, s, e in _iter_atoms(buf, start, end):
        if t == type4:
            return s, e
    return None


def _stsd_first_entry_is_tpic(buf: bytes, moov_body: int, moov_end: int) -> bool:
    """moov → trak → mdia → minf → stbl → stsd，首个 sample entry 是否 'TPIC'。

    只有 stsd 的 sample entry 4CC 才算数——moov 内任何其它位置出现 'TPIC'
    文本（元数据/注释/free atom）都不命中（P0-06：消灭裸子串误判）。
    """
    for t, s, e in _iter_atoms(buf, moov_body, moov_end):
        if t != b'trak':
            continue
        mdia = _find_atom(buf, s, e, b'mdia')
        if mdia is None:
            continue
        minf = _find_atom(buf, mdia[0], mdia[1], b'minf')
        if minf is None:
            continue
        stbl = _find_atom(buf, minf[0], minf[1], b'stbl')
        if stbl is None:
            continue
        stsd = _find_atom(buf, stbl[0], stbl[1], b'stsd')
        if stsd is None:
            continue
        ss, se = stsd
        # stsd body：version/flags(4) + entry_count(4) + sample entries
        if se - ss < 8:
            continue
        entry = ss + 8
        if entry + 8 > se:
            continue
        esize = int.from_bytes(buf[entry:entry + 4], 'big')
        if esize < 8 or entry + esize > se:
            continue
        if buf[entry + 4:entry + 8] == b'TPIC':
            return True
    return False


# FastStart 探测的 moov 读取上限（异常 size 防御；本库 moov 随帧数线性增长，
# 1 小时 25fps ≈ 0.9 MB，64 MiB 覆盖数天时长）
_MOOV_READ_CAP = 64 << 20


def _faststart_is_tpic(path: str, head: bytes) -> bool:
    """moov-first 布局：读全 moov（有界）后结构化走 stsd 判定。

    moov 头位于 ftyp 之后（偏移 20）：32 位 size 或 size==1 + 64 位 largesize。
    """
    if head[20:24] == b'\x00\x00\x00\x01':
        if len(head) < 36:
            return False
        moov_size = int.from_bytes(head[28:36], 'big')
        body_off = 20 + 16
    else:
        moov_size = int.from_bytes(head[20:24], 'big')
        body_off = 20 + 8
    if moov_size < 8:
        return False
    need = 20 + moov_size  # moov 起点恒在 ftyp 后（偏移 20），终点 = 起点+size
    if need > _MOOV_READ_CAP:
        return False
    buf = head
    if len(buf) < need:
        try:
            with open(path, 'rb') as fh:
                buf = fh.read(need)
        except OSError:
            return False
        if len(buf) < need:
            return False
    return _stsd_first_entry_is_tpic(buf, body_off, need)


def is_topos_movie(path: str, sniff_bytes: int = 4096) -> bool:
    """结构化探测文件是否为 Topos MOV（factory auto 路由，R-17）。

    只认可容器规范写入器的两种顶层布局特征（container_spec_v1 §2）：

    - 标准布局 ``ftyp(20)+mdat``：mdat 头之后紧跟首个 packet magic
      ``TPIC``（64 位 size 形式在偏移 36，32 位形式在 28）；
    - FastStart ``ftyp(20)+moov``：走有界 atom 解析
      ``moov/trak/mdia/minf/stbl/stsd``，且 stsd 首个 sample entry 的 4CC
      必须是 ``TPIC``（P0-06：moov 内任意位置的 'TPIC' 文本不再命中）。

    Args:
        path: 文件路径
        sniff_bytes: moov-first 布局的初始探测窗口（不足时按 moov 实际
            size 有界扩展读取）

    Returns:
        是 Topos MOV 返回 True；读取失败/结构不符返回 False
    """
    try:
        with open(path, 'rb') as fh:
            head = fh.read(sniff_bytes)
    except OSError:
        return False
    if len(head) < 40 or head[4:8] != b'ftyp':
        return False
    if b'qt  ' not in head[8:20]:
        return False
    if head[24:28] == b'mdat':
        # 标准/常规布局：mdat 直接跟在 ftyp 后，首 packet 紧贴 mdat 头
        if head[20:24] == b'\x00\x00\x00\x01':  # 64 位 size 形式（本库写入器恒定）
            return head[36:40] == b'TPIC'
        return head[28:32] == b'TPIC'  # 32 位 size 形式（外来兼容）
    if head[24:28] == b'moov':
        # FastStart：结构化 stsd sample entry 判定（P0-06）
        return _faststart_is_tpic(path, head)
    return False


def _pixel_format_name(alpha_mode: int, alpha_bit_depth: int,
                       bit_depth: int = 10, pixel_format: int = 0) -> str:
    """pixel_format 注册名（见 frame.py PIXEL_FORMATS 的 topos 条目注释）。"""
    if pixel_format == 3:
        # M4-R9：TRAW CFA 相位平面（profile 7，无 alpha 域）——4×ceil(W/2)
        # ×ceil(H/2) 平面，注册名与编码器/图片线一致（topos_cfa12le/16le）
        return f'topos_cfa{bit_depth}le'
    if alpha_mode == 0:
        # W1（F1 同款纠偏）：未知 (pf, bd) 组合显式失败——静默冒充
        # yuv422p10le 会把 16-bit/444 流按 10-bit 422 解释（错色 + 几何
        # 语义错位）。表随批 4 补齐 bd16；域外组合 = 注册表过旧或流损坏。
        try:
            return _PIXEL_FORMAT_NO_ALPHA[(pixel_format, bit_depth)]
        except KeyError:
            raise ValueError(
                f"Topos MOV: 未知的 pixel_format={pixel_format}/"
                f"bit_depth={bit_depth} 组合——注册表缺该枚举名，"
                f"拒绝静默错标") from None
    if pixel_format == 2:
        return f'topos_gbrap{bit_depth}a{alpha_bit_depth}'
    cf = '444' if pixel_format == 1 else '422'
    return f'topos_yuva{cf}p{bit_depth}a{alpha_bit_depth}'


def _plane_geoms(pixel_format: int, plane_count: int,
                 w: int, h: int) -> "list[tuple[int, int]]":
    """解码平面几何（(rows 宽, cols 高) 顺序 = numpy (h, w) 反转前约定）。

    M4-R9：pf=3（TRAW CFA）= 4 相位平面 R/Gr/Gb/B，全部半几何
    ceil(W/2)×ceil(H/2)；pf=0（4:2:2）色度半宽全高；pf∈{1,2} 全宽全高；
    第 4 平面（alpha）恒全几何。
    """
    if pixel_format == 3:
        pw, ph = (w + 1) // 2, (h + 1) // 2
        return [(pw, ph)] * 4 if plane_count >= 4 else [(pw, ph)] * plane_count
    cw = w if pixel_format != 0 else (w + 1) // 2
    geoms = [(w, h), (cw, h), (cw, h)]
    if plane_count >= 4:
        geoms.append((w, h))
    return geoms


def _downscale_axis(plane: np.ndarray, axis: int, src: int, out: int) -> np.ndarray:
    """单轴 box 缩小（变宽分箱取整平均；仅缩小路径，起点严格递增）。

    中间累加用 uint32：单轴最大分箱 8192 × 最大码值 65535 < 2^32，
    不会溢出。两轴分离各舍入一次（代理近似，误差 ≤ 1 LSB/轴）。
    """
    if src == out:
        return plane
    starts = np.floor(np.arange(out, dtype=np.float64) * src / out).astype(np.intp)
    summed = np.add.reduceat(plane.astype(np.uint32), starts, axis=axis)
    counts = np.diff(np.append(starts, src)).astype(np.float64)
    if axis == 0:
        return np.rint(summed / counts[:, None]).astype(np.uint16)
    return np.rint(summed / counts[None, :]).astype(np.uint16)


def _downscale_plane(plane: np.ndarray, out_h: int, out_w: int) -> np.ndarray:
    """分离 box 缩小；尺寸相同则原样返回（保持 read-only 视图语义）。"""
    if plane.shape == (out_h, out_w):
        return plane
    rows = _downscale_axis(plane, 0, plane.shape[0], out_h)
    return _downscale_axis(rows, 1, plane.shape[1], out_w)


class _ToposPlaneEntry:
    """池化输出平面组（M2）：numpy '<u2' (h, w) 数组 + 预构建 plane views。

    P1-13：alpha 移位输出（声明位深满刻度）写入同生命周期 scratch——
    不再每帧新分配 alpha 平面（4K ≈ 33 MB/帧 的临时分配）。
    """

    __slots__ = ("key", "arrays", "views", "alpha_scratch")

    def __init__(self, key: tuple, arrays: list[np.ndarray], views) -> None:
        self.key = key
        self.arrays = arrays
        self.views = views
        self.alpha_scratch: "np.ndarray | None" = None

    def alpha_scratch_for(self, base: "np.ndarray") -> "np.ndarray | None":
        """取（懒建）与 base 同几何的移位输出缓冲；几何不符返回 None。"""
        if self.alpha_scratch is not None and \
                self.alpha_scratch.shape == base.shape and \
                self.alpha_scratch.dtype == base.dtype:
            return self.alpha_scratch
        if self.alpha_scratch is None:
            self.alpha_scratch = np.empty_like(base)
            self.alpha_scratch.flags.writeable = False
            return self.alpha_scratch
        return None


def _entry_free(entry: _ToposPlaneEntry) -> bool:
    """平面组是否可安全复用（消费方已全部释放）。

    getrefcount 计入其参数临时引用：仅 entry.arrays 持有时基线 = 2
    （tuple/列表 1 + 参数 1）。DecodedFrame.planes / GPU 上传缓存仍持有
    任一平面时 > 2 → 不可回收（计划 §6.2：不允许覆盖仍在使用的帧）。
    P1-13：alpha 移位输出持有的是 scratch（非 raw 平面）——scratch 的
    引用计数同样参与判定，否则持有移位帧时 raw 已释放 → 池误判空闲。
    """
    arrays = entry.arrays
    for i in range(len(arrays)):
        if sys.getrefcount(arrays[i]) > 2:
            return False
    # P1-13/AUD-35900：alpha 移位输出持有的是 scratch（非 raw 平面）——
    # scratch 的引用计数同样参与判定，否则持有移位帧时 raw 已释放 →
    # 池误判空闲。取计数时不得先落入局部变量：局部引用会把基线从 2 抬到
    # 3，导致带 scratch 的条目永远判定为忙（池复用静默失效、每帧临时
    # 分配），直接以属性表达式调用保持与 arrays 相同的基线口径。
    if entry.alpha_scratch is not None and sys.getrefcount(entry.alpha_scratch) > 2:
        return False
    return True


class _ToposPlanePool:
    """有界输出平面池（M2，计划 §6.2）：滚动复用（容量随批量深度放大）。

    命中条件：几何 key 一致且全部平面已被消费方释放（引用计数判定）。
    池满且无空闲项时临时分配不回收——正确性优先，实际由 GC 兜底。
    """

    __slots__ = ("_entries", "_max")

    def __init__(self, max_entries: int = 5) -> None:
        self._entries: list[_ToposPlaneEntry] = []
        self._max = max_entries

    def acquire(
        self,
        decoder: ToposDecoder,
        codec: ToposCodec,
        packet,
        packet_size: Optional[int] = None,
        request=None,
    ) -> "tuple[_ToposPlaneEntry, ToposFrameInfo]":
        """取（或建）一帧输出平面组并完成整帧解码。

        返回 (entry, info)；entry.arrays 即解码结果（须在再次 acquire 前
        消费完毕或保持引用直至弃用）。packet 为复用 ctypes 缓冲时须传
        packet_size（len 返回容量）。
        """
        pinfo = (
            decoder.prepare_request(packet, request, packet_size)
            if request is not None else decoder.prepare(packet, packet_size)
        )
        entry = self.acquire_entry(decoder, codec, pinfo)
        info = (
            decoder.decode_views_request(packet, entry.views, request, packet_size)
            if request is not None
            else decoder.decode_views(packet, entry.views, pinfo.plane_count, packet_size)
        )
        return entry, info

    def acquire_entry(
        self,
        decoder: ToposDecoder,
        codec: ToposCodec,
        pinfo,
    ) -> _ToposPlaneEntry:
        """按 prepare 几何取（或建）一帧输出平面组（不解码——批量路径用）。

        返回的平面组在被显式持有（tuple/DecodedFrame 平面引用）之前视为
        空闲，可能被后续 acquire_entry 复用覆盖——批量解码须逐帧持有。
        """
        key = (
            int(pinfo.visible_width), int(pinfo.visible_height),
            int(pinfo.plane_count), int(pinfo.pixel_format), int(pinfo.alpha_mode),
        )
        for cand in self._entries:
            if cand.key == key and _entry_free(cand):
                return cand
        geoms = [codec.plane_geometry(pinfo, p) for p in range(pinfo.plane_count)]
        arrays = []
        for (w, h) in geoms:
            arr = np.zeros((h, w), dtype='<u2')
            # 对齐旧路径 read-only 契约（消费方不得就地改写解码帧）；
            # native 经指针直写不受该标志影响
            arr.flags.writeable = False
            arrays.append(arr)
        views = decoder.make_views(arrays)
        entry = _ToposPlaneEntry(key, arrays, views)
        if len(self._entries) < self._max:
            self._entries.append(entry)
        return entry

    def close(self) -> None:
        self._entries.clear()


class ToposMediaSource:
    """Topos MOV 媒体源（全 intra、O(1) 随机访问、恒 planar uint16 输出）。

    实现 MediaSource Protocol（open/close/probe/read_frame/seek_frame +
    is_open/current_frame/path），并额外提供调度层实际依赖的
    ``read_next_frame()``（EOF 抛 StopIteration，对齐 FFmpegMediaSource）。

    线程契约：**单实例不可重入**——原生 movie 句柄不自带锁，跨线程访问
    必须由上层串行化（SourceManager._decode_lock / ClipSession.decode_lock
    已按此假设加锁）。读前批量预取（阶段4 兑现）完全在该串行化内完成，
    不新增线程：顺序前进确认后一次批量解码 depth 帧，seek/跳读失效回
    单帧路径，输出与逐帧解码逐位一致。

    Note:
        preserve_planes 仅为对齐 factory 闭包签名而接受：Topos 输出天然
        planar（无 packed RGB 路径），该开关不改变行为。
    """

    def __init__(
        self,
        path: str,
        target_size: Optional[tuple[int, int]] = None,
        preserve_planes: bool = True,
        read_ahead: Optional[int] = None,
        decode_workers: Optional[int] = None,
        decode_role: str = "playback",
        drop_alpha: bool = False,
        no_deblock: bool = False,
    ) -> None:
        """
        Args:
            path: Topos MOV 文件路径
            target_size: 可选代理目标尺寸 (w, h)（仅缩小；放大回退原始分辨率）
            preserve_planes: 接受但对本源无效果（恒 planar）
            read_ahead: 顺序读前批量预取深度；None=自动策略（播放关闭、导出为
                4 帧并受预算限制；TOPOS_READAHEAD 可显式覆盖），0=关闭，正数=固定深度
            decode_workers: 本 source decoder context 的 worker 上限；None=按
                D3 共享预算公平分配，0=跟随进程池，正数=显式上限。也可用
                TOPOS_DECODE_WORKERS 设置默认值。
            decode_role: 预算诊断角色（通常为 playback 或 export）。
            drop_alpha: 导出目标明确不带 Alpha 时，请 native 解码跳过 Alpha
                slice；旧库/不支持的码流自动保留四平面路径。
        """
        self._path = path
        self._target_size: Optional[tuple[int, int]] = None
        if target_size is not None:
            tw, th = int(target_size[0]), int(target_size[1])
            if tw > 0 and th > 0:
                self._target_size = (tw, th)
            else:
                logger.warning(f"ToposMediaSource: 非法 target_size {target_size}，忽略")
        self._preserve_planes = preserve_planes
        self._read_ahead_param = read_ahead
        self._decode_workers_param = decode_workers
        self._decode_role = str(decode_role or "playback")
        self._drop_alpha_requested = bool(drop_alpha)
        self._decode_workers_requested: Optional[int] = None
        self._decode_workers_assigned = 0
        self._decode_budget_lease: Optional[DecodeWorkerLease] = None
        self._codec: Optional[ToposCodec] = None
        self._movie: Optional[ToposMovieFile] = None
        self._summary: Optional[ToposMovieSummary] = None
        self._metadata: Optional[VideoMetadata] = None
        self._color_semantics: Optional[ToposColorSemantics] = None
        # RD7-02：轨级版本只读自 tpcC；旧 V1–V6 不被重写，AUTO_2K 的
        # legacy reduced fallback 必须在 diagnostics 中可见，避免把旧流
        # 的固定比例路径误标成尚未发布的 V7 base/band。
        self._bitstream_major = 0
        self._stream_compatibility = 'unknown'
        self._legacy_reduced_fallback = False
        # R3：tpcB alpha 预算元数据（无则 None——旧文件兼容），逐帧挂 extra
        self._alpha_budget: Optional[dict] = None
        # RC1：TRWM as-shot 元数据（无 trwm 原子则 None）
        self._trwm: Optional[dict] = None
        self._current_frame = 0
        # M2：持久 decoder context + 输出平面池（旧 dylib 无符号时回退旧路径）
        self._decoder_ctx: Optional[ToposDecoder] = None
        self._plane_pool: Optional[_ToposPlanePool] = None
        # RD1：target_size 优先走 native request；旧 dylib 保留 legacy box fallback。
        self._decode_request = None
        self._native_target_decode = False
        self._native_drop_alpha = False
        self._no_deblock_requested = bool(no_deblock)
        self._native_no_deblock = False
        self._v7b_base_range_decode = False
        self._v7b_base_range_samples = 0
        # V9（micro-gop 计划批 5）：GOP context + 有界 GOP 缓存。
        # _gop_pos = ctx 已 feed 到的 sample 序号（-1 = 无位置；seek/abort
        # 后失效）——顺序前进时单帧 O(1) feed（V9 复审 2026-09-14）。
        self._is_v9 = False
        self._gop_ctx: Optional[ToposGopContext] = None
        self._gop_cache: "dict[int, tuple[list[np.ndarray], ToposFrameInfo, int]]" = {}
        self._gop_pos = -1
        # AUD-35904：V9 平面复用槽。消费方全部释放（refcount 判定同
        # _entry_free）后下次解码复用同几何平面组，消除每帧 ~25MB 新分配
        # + 清零页触碰；消费方仍持有时维持新分配（不允许覆盖在用帧）。
        self._v9_spare_planes: "Optional[list[np.ndarray]]" = None
        self._gop_em = 0  # 帧间载体 cfg 选择子（10=V9 / 11=V7-R3；0=非帧间）
        # 阶段4 兑现：顺序读前批量预取（depth=0 关闭；open 时定容）
        self._ra_depth = 0
        self._ra_seq = False  # 上一次 read_next_frame 是否顺序前进（批量触发条件）
        self._ra_cache: dict[int, "tuple[_ToposPlaneEntry, tuple, ToposFrameInfo, int]"] = {}
        self._ra_stats = {
            'batch_decodes': 0,
            'prefetched_frames': 0,
            'cache_hits': 0,
            'cache_discarded': 0,
            'batch_fallbacks': 0,
            'base_batch_fallbacks': 0,
        }
        # C17：音频 chunk 前缀采样索引（惰性构建；None = 未建/已失效）
        self._audio_chunk_prefix: "np.ndarray | None" = None
        # P1-04：解码路径诊断——退化（context 创建失败/缺批量符号/旧 dylib）
        # 不得静默；路径与原因在 open 时定值，经 diagnostics 暴露
        self._decode_path = 'closed'
        self._decode_path_reason = 'not_opened'
        # macOS Metal export path: lazily created IOSurface ring.  The normal
        # MediaSource contract continues to return CPU ``DecodedFrame``
        # objects; ``read_frame_metal`` opts into a GPU-visible surface for
        # callers that can keep the frame alive until their Metal fence.
        self._metal_surface_pool: Optional[Any] = None
        # Metal objects are process/device scoped.  Recreating the device and
        # importing the same IOSurface planes on every frame adds a measurable
        # fixed cost to Topos export (the decode itself is already native).
        # Keep one device and one imported texture tuple per pool slot; the
        # IOSurface storage is reused by the ring and only its contents change.
        self._metal_device: Optional[Any] = None
        self._metal_texture_cache: dict[int, tuple[Any, ...]] = {}
        self._metal_surface_frames = 0
        self._metal_surface_failures = 0

    # —— MediaSource Protocol ——

    def open(self) -> None:
        """打开媒体源（解析 moov 建索引；重复调用为无操作）。"""
        if self._movie is not None:
            return
        try:
            self._codec = _get_shared_codec()
        except ToposCodecError as exc:
            raise MediaOpenError(
                f"libtopos_codec 不可用: {exc}", source_path=self._path
            ) from exc
        try:
            self._movie = ToposMovieFile(self._codec, self._path)
        except (ToposCodecError, OSError) as exc:
            raise MediaOpenError(
                f"打开 Topos MOV 失败: {exc}", source_path=self._path
            ) from exc
        try:
            self._summary = self._movie.info()
            _, dur0 = self._movie.pts(0)
            last_pts, last_duration = self._movie.pts(self._summary.sample_count - 1)
            duration_ticks = int(last_pts + last_duration)
            self._metadata = self._build_metadata(
                self._summary, dur0, duration_ticks=duration_ticks
            )
            self._alpha_budget = self._movie.alpha_budget()
            # RC1：TRAW 开发元数据（trwm 原子；无则 None——旧文件常态）。
            # 2026-09-21 复查：解码失败（如 CRC 不符）显式告警——静默落
            # None 会让"损坏 TRWM"与"无 TRWM"不可区分（W1 姿态）。
            # "文件无 trwm 原子"（TC_ERR_STATE）是旧文件常态，不告警。
            try:
                # ToposCodecError 用模块级导入：函数内再 import 同名符号会把
                # 整个函数体的该引用变成局部名，本函数前面的 except 因而
                # 在缺文件等路径上抛 UnboundLocalError（掩盖真正的打开失败）。
                from src.shared.codec.topos_trwm import decode_trwm

                self._trwm = decode_trwm(self._movie.read_raw_meta())
            except ToposCodecError as exc:
                self._trwm = None
                if "无 trwm" not in str(exc):
                    logger.warning(
                        "TRWM 元数据读取失败（按无 TRWM 处理，WB/gamut "
                        "回退默认值）：%s", exc)
            except Exception as exc:
                self._trwm = None
                logger.warning(
                    "TRWM 元数据解码失败（按无 TRWM 处理，WB/gamut 回退 "
                    "默认值）：%s", exc)
            self._bitstream_major = int(self._summary.bitstream_major)
            # 帧间微 GOP 载体识别（ADR-C048）：V9 流 tpcC major=9；V7-R3
            # 流 tpcC major=7 与 V7-R2 帧内流同 major——靠首包熵字节区分
            # （R3=8，R2=7；包结构自描述）。_gop_em 传给 GOP context cfg。
            self._gop_em = 0
            if self._bitstream_major == 9:
                self._gop_em = 10
            elif self._bitstream_major == 7 and self._summary.sample_count > 0:
                pkt0 = self._movie.packet(0)
                if len(pkt0) > 45 and pkt0[45] == 8 and pkt0[15] == 0:
                    self._gop_em = 11
            self._is_v9 = (self._gop_em != 0)
            if 1 <= self._bitstream_major <= 6:
                self._stream_compatibility = 'legacy_v1_v6'
            elif self._is_v9:
                # 帧间微 GOP（V9 批 5 / V7-R3 ADR-C048）：读路径走 GOP
                # context（prev_sync 定位 + 顺序解 + 有界 GOP 缓存），
                # 禁用无状态 readahead/批量机制（它们假设逐帧独立解码）。
                self._stream_compatibility = 'v9_micro_gop'
            elif self._bitstream_major == 0:
                # Compatibility with a pre-RD7 native dylib: the MOV parser
                # already validated the stream, but cannot report its major.
                self._stream_compatibility = 'legacy_metadata_unavailable'
            else:
                self._stream_compatibility = 'unsupported_major'
        except (ToposCodecError, ToposColorMetadataError, ValueError) as exc:
            self.close()
            raise MediaOpenError(
                f"Topos MOV 元数据解析失败: {exc}", source_path=self._path
            ) from exc
        self._current_frame = 0
        # 代理只缩小；请求放大时回退原始分辨率（保持宽高语义不撒谎）
        if self._target_size is not None:
            tw, th = self._target_size
            if tw > self._summary.width or th > self._summary.height:
                logger.warning(
                    f"ToposMediaSource: target_size {self._target_size} 大于原始 "
                    f"{self._summary.width}x{self._summary.height}，回退原始分辨率"
                )
                self._target_size = None
        if (self._drop_alpha_requested and self._summary.alpha_mode != 0
                and getattr(self._codec, "_has_capabilities", False)
                and getattr(self._codec, "_has_decoder_request", False)):
            try:
                self._native_drop_alpha = bool(
                    self._codec.capabilities().decode_drop_alpha)
            except ToposCodecError:
                self._native_drop_alpha = False
        if (self._no_deblock_requested
                and getattr(self._codec, "_has_capabilities", False)
                and getattr(self._codec, "_has_decoder_request", False)):
            try:
                self._native_no_deblock = bool(
                    self._codec.capabilities().decode_no_deblock)
            except ToposCodecError:
                self._native_no_deblock = False
            if not self._native_no_deblock:
                # 旧 dylib 对未定义请求位**干净拒绝**（不是忽略）——不置位，
                # 预览保持滤波，只留一行可见日志（fail-closed，不静默降级）
                logger.info(
                    "ToposMediaSource: 本机 dylib 不广播 NO_DEBLOCK 能力，"
                    "忽略预览关滤波开关（保持滤波）"
                )
        if ((self._target_size is not None or self._native_drop_alpha
                or self._native_no_deblock)
                and int(getattr(self._summary, 'pixel_format', 0) or 0) != 3
                and getattr(self._codec, "_has_decoder_request", False)):
            # M4-R9：TRAW CFA 不走 native target decode——半几何相位平面
            # 的缩放语义在 native request 域未定义（fail-closed）；代理
            # 显示路径由 legacy box 平面缩放承担（仅显示，不涉交付）。
            self._decode_request = self._build_target_decode_request()
            self._native_target_decode = self._decode_request is not None
            self._legacy_reduced_fallback = (
                self._stream_compatibility == 'legacy_v1_v6'
                and int(self._decode_request.mode) == TC_DECODE_MODE_REDUCED
            )
        # M2：可用则启用 context + 平面池低拷贝路径（P1-04：退化须留痕）。
        # V9：读路径走 GOP context（下方 _v9_* 方法）——无状态 readahead/
        # 批量机制假设逐帧独立解码，对帧间流不成立，禁用。
        if self._codec._has_decoder_ctx and not self._is_v9:
            try:
                requested = self._resolve_decode_workers()
                budget_capable = getattr(
                    self._codec, "_has_decoder_worker_budget", False)
                initial = requested
                if budget_capable:
                    capacity = max(1, int(self._codec.slice_threads()))
                    # Register before constructing the context to compute a
                    # fair initial share; the callback is replaced with the
                    # live context setter immediately after construction.
                    self._decode_budget_lease = DECODE_WORKER_BUDGET.acquire(
                        f"media:{id(self)}", requested=requested,
                        role=self._decode_role, capacity=capacity,
                    )
                    initial = self._decode_budget_lease.assigned
                self._decoder_ctx = ToposDecoder(self._codec,
                                                 max_slice_workers=initial)
                self._decode_workers_assigned = initial
                # Rebind the lease callback now that the context exists.  The
                # callback uses an atomic native setter, so a later open/close
                # can rebalance all live contexts without recreating them.
                if self._decode_budget_lease is not None:
                    DECODE_WORKER_BUDGET.bind(
                        self._decode_budget_lease.key, self._apply_decode_workers)
            except ToposCodecError as exc:
                # P1-04：回退高拷贝路径必须可见——warning + diagnostics
                self._decoder_ctx = None
                self._plane_pool = None
                self._decode_request = None
                self._native_target_decode = False
                self._native_drop_alpha = False
                self._native_no_deblock = False
                if self._decode_budget_lease is not None:
                    self._decode_budget_lease.release()
                    self._decode_budget_lease = None
                self._decode_path = 'legacy'
                self._decode_path_reason = f'decoder_create_failed: {exc}'
                logger.warning(
                    "ToposMediaSource: decoder context 创建失败，回退高拷贝"
                    "逐帧路径（decode_path=legacy）: %s", exc,
                )
            else:
                # 阶段4 兑现：批量预取需 context + 批量符号；深度按几何定容，
                # 平面池容量 = 深度 + 2 在飞余量（消费方持有 1-2 帧不释放时，
                # 批内分配变池内滚动复用而非临时分配——稳态内存不变、免页错误）
                depth = self._readahead_depth()
                if depth > 0 and not self._codec._has_decoder_batch:
                    depth = 0
                    self._decode_path = 'context'
                    self._decode_path_reason = 'batch_symbols_unavailable'
                elif depth > 0 and self._native_target_decode and not getattr(
                        self._codec, "_has_decoder_batch_request", False):
                    depth = 0
                    self._decode_path = 'context'
                    self._decode_path_reason = 'request_batch_symbols_unavailable'
                elif (depth > 0 and self._bitstream_major == 7 and
                      self._native_target_decode and getattr(
                          self._codec, "_has_movie_packet_base", False) and
                      not getattr(self._codec, "_has_movie_packet_base_batch", False)):
                    # The current batch reader still coalesces complete MOV
                    # samples. Keep V7 preview on the range reader until a
                    # packet_base_batch API exists; otherwise explicit
                    # read-ahead would silently reintroduce residual I/O.
                    depth = 0
                    self._decode_path = 'context'
                    self._decode_path_reason = 'v7b_base_range_batch_unavailable'
                else:
                    self._decode_path = 'context-batch' if depth > 0 else 'context'
                    self._decode_path_reason = 'ok'
                self._ra_depth = depth
                self._plane_pool = _ToposPlanePool(max_entries=max(5, depth + 2))
                pool_bytes = max(5, depth + 2) * self._frame_plane_bytes()
                logger.debug(
                    "ToposMediaSource: decode_path=%s depth=%d 平面池 %d 条目"
                    " ≈ %.1f MiB（%s）",
                    self._decode_path, depth, max(5, depth + 2),
                    pool_bytes / (1 << 20), self._path,
                )
        else:
            self._decode_request = None
            self._native_target_decode = False
            self._native_drop_alpha = False
            self._native_no_deblock = False
            if self._is_v9:
                self._decode_path = 'v9-gop'
                self._decode_path_reason = 'ok'
            else:
                self._decode_path = 'legacy'
                self._decode_path_reason = 'dylib_missing_decoder_ctx'

    def close(self) -> None:
        """关闭并释放原生句柄与文件描述符（幂等；可重新 open）。"""
        self._ra_cache.clear()
        self._ra_seq = False
        self._gop_cache.clear()
        self._gop_pos = -1
        # AUD-35904：close/重开时释放 V9 平面复用槽（消费方可能仍持有
        # 上一帧平面——仅解除本源持有的引用，不强制回收）
        self._v9_spare_planes = None
        self._audio_chunk_prefix = None  # C17：重开句柄后索引重建
        if self._gop_ctx is not None:
            self._gop_ctx.close()
            self._gop_ctx = None
        self._decode_request = None
        self._native_target_decode = False
        self._native_drop_alpha = False
        self._native_no_deblock = False
        self._legacy_reduced_fallback = False
        self._v7b_base_range_decode = False
        self._v7b_base_range_samples = 0
        self._bitstream_major = 0
        self._stream_compatibility = 'unknown'
        if self._decoder_ctx is not None:
            self._decoder_ctx.close()
            self._decoder_ctx = None
        if self._decode_budget_lease is not None:
            self._decode_budget_lease.release()
            self._decode_budget_lease = None
        self._decode_workers_assigned = 0
        if self._plane_pool is not None:
            self._plane_pool.close()
            self._plane_pool = None
        if self._metal_surface_pool is not None:
            try:
                self._metal_surface_pool.close()
            except Exception:
                # A caller-owned GpuDecodedFrame may still hold one leased
                # IOSurface.  Keep the pool reachable through its release
                # callback and let that frame release it after the GPU fence.
                logger.debug(
                    "ToposMediaSource: Metal surface pool still has in-flight "
                    "leases", exc_info=True,
                )
            self._metal_surface_pool = None
        self._metal_device = None
        self._metal_texture_cache.clear()
        if self._movie is not None:
            self._movie.close()
            self._movie = None

    def probe(self) -> VideoMetadata:
        """探测视频元数据（需已 open）。"""
        self._require_open()
        assert self._metadata is not None
        return self._metadata

    # —— V9 帧间微 GOP 读路径（micro-gop 计划批 5；§4.4）——

    def _v9_ensure_ctx(self) -> ToposGopContext:
        """惰性创建 GOP context（几何/格式取自轨摘要）。"""
        if self._gop_ctx is None:
            import ctypes
            from src.shared.codec.topos_binding import _CFrameConfig
            s = self._summary
            cfg = _CFrameConfig()
            cfg.struct_size = ctypes.sizeof(_CFrameConfig)
            cfg.abi_version = TOPOS_CODEC_ABI_VERSION
            cfg.visible_width = int(s.width)
            cfg.visible_height = int(s.height)
            cfg.profile = int(s.profile)
            cfg.pixel_format = int(s.pixel_format)
            cfg.bit_depth = int(s.bit_depth)
            cfg.qmatrix_id = int(getattr(s, 'qmatrix_id', 0) or 0)
            cfg.color_range = int(getattr(s, 'color_range', 1) or 1)
            cfg.color_primaries = int(getattr(s, 'color_primaries', 1) or 1)
            cfg.color_transfer = int(getattr(s, 'color_transfer', 1) or 1)
            cfg.color_matrix = int(getattr(s, 'color_matrix', 1) or 1)
            cfg.reserved[0] = self._gop_em  # 10=V9 / 11=V7-R3（open 时识别）
            self._gop_ctx = ToposGopContext(self._codec, cfg)
        return self._gop_ctx

    def _v9_decode_gop(self, target: int) -> None:
        """解码 target 帧并推进参考链，结果入 `_gop_cache[target]`。

        顺序前进（ctx 已 feed 到 target-1）→ 单帧 O(1) feed；否则 prev_sync
        定位：前缀帧只推进参考（feed 无 planes——零像素分配），仅 target
        产出像素。feed 失败时 `_gop_pos` 停在最后成功帧，下次 miss 路径
        abort 重定位自动恢复（V9 复审 2026-09-14：旧实现每次 miss 都全量
        重解且缓存保留已消费方向——顺序读每帧重解 GOP 前缀，IP-4 达 2.5×）。
        """
        assert self._movie is not None
        ctx = self._v9_ensure_ctx()
        if self._gop_pos != target - 1:
            sync = self._movie.prev_sync(target)
            ctx.abort(reset_to_i=True)
            self._gop_cache.clear()
            for k in range(sync, target):
                ctx.feed(self._movie.packet(k))  # 前缀：只推进参考（无输出）
                self._gop_pos = k
        else:
            self._gop_cache.clear()  # 顺序路径：旧条目已被消费，保持 ≤1
        s = self._summary
        vw, vh = int(s.width), int(s.height)
        cw = vw if int(s.pixel_format) != 0 else (vw + 1) // 2
        geoms = [(vh, vw), (vh, cw), (vh, cw)]  # numpy shape = (rows, cols)
        # AUD-35904：优先复用消费方已释放的同几何平面组（refcount 判定与
        # _entry_free 同口径：基线 2 = spare 列表容器 1 + getrefcount 参数
        # 1）；消费方仍持有上一帧时维持新分配。native 经指针直写不受
        # writeable 标志影响（与池路径 503-505 行同一契约）。
        planes: "list[np.ndarray]" = []
        spare = self._v9_spare_planes
        if (spare is not None
                and [p.shape for p in spare] == geoms
                and all(sys.getrefcount(p) <= 2 for p in spare)):
            planes = spare
            self._v9_spare_planes = None
        else:
            planes = [np.zeros(g, dtype=np.uint16) for g in geoms]
            # 对齐池/legacy 路径 read-only 交付契约（消费方不得就地改写）
            for p in planes:
                p.flags.writeable = False
        ctx.feed(self._movie.packet(target), planes=planes)
        self._gop_pos = target
        pts, _dur = self._movie.pts(target)
        finfo = ToposFrameInfo(
            visible_width=vw, visible_height=vh, plane_count=3,
            bit_depth=int(s.bit_depth), profile=int(s.profile),
            pixel_format=int(s.pixel_format), alpha_mode=0,
            concealed_slices=0, slice_count=0, slice_status=(),
        )
        self._gop_cache[target] = (planes, finfo, pts)

    def _v9_read_next(self) -> DecodedFrame:
        """V9 顺序读：缓存命中直接装配；未命中从 sync 重解（seek 后）。"""
        index = self._current_frame
        if index >= self._total():
            raise StopIteration("End of video stream")
        if index not in self._gop_cache:
            self._v9_decode_gop(index)
        planes, finfo, pts = self._gop_cache.pop(index)
        # AUD-35904：交付后把平面组挂入复用槽——消费方仍持有时 refcount
        # 判定保持"忙"，释放后下次解码复用（不覆盖在用帧）。native 解码
        # 每帧全量重写平面（与池路径复用语义一致，无脏数据残留）。
        self._v9_spare_planes = planes
        self._current_frame = index + 1
        return self._assemble_decoded_frame(index, finfo, planes, pts)

    def read_frame(self, frame_index: int) -> DecodedFrame:
        """读取指定帧（当前游标命中则免 seek；越界抛 ValueError）。"""
        self._require_open()
        total = self._total()
        if not 0 <= frame_index < total:
            raise ValueError(f"帧号越界: {frame_index}（共 {total} 帧）")
        if self._current_frame != frame_index:
            self.seek_frame(frame_index)
        return self.read_next_frame()

    def read_frame_metal(self, frame_index: int) -> Optional[Any]:
        """Decode one master frame into IOSurface-backed Metal planes.

        This is an export/Metal opt-in API; the ordinary ``read_frame`` path
        intentionally remains CPU ``DecodedFrame`` for compatibility.  The
        native Topos decoder performs the reconstruction on CPU, but writes
        directly to mapped IOSurface planes.  Metal imports those planes as
        ``R16Uint`` textures, so the caller avoids a NumPy allocation and an
        explicit CPU→GPU texture upload.

        The returned ``GpuDecodedFrame`` owns its surface lease.  Consumers
        must call ``frame.release.release()`` only after their Metal command
        buffer has completed.  Unsupported streams (V9, alpha, non-macOS,
        unavailable Metal/IOSurface) return ``None`` so the caller can use
        the established CPU path.
        """
        self._require_open()
        if not 0 <= int(frame_index) < self._total():
            raise ValueError(
                f"帧号越界: {frame_index}（共 {self._total()} 帧）"
            )
        # The first production slice is native context + non-alpha 4:2:2.
        # V9 has a reference chain whose current context cannot target an
        # external surface yet; keep that path on its correct CPU decoder.
        if (sys.platform != 'darwin' or self._is_v9
                or self._decoder_ctx is None or self._movie is None
                or self._codec is None or self._summary is None
                or int(self._summary.pixel_format) != 0
                # The surface pool currently mirrors the master geometry;
                # proxy target decode will stay on its correctly scaled CPU
                # planar path until a scaled IOSurface pool is added.
                or self._target_size is not None):
            return None
        if int(self._summary.alpha_mode) != 0:
            return None

        try:
            from src.shared.media.macos_vt.topos_surface import (
                ToposIOSurfacePool, ToposSurfaceUnavailable,
            )
            import Metal
            from src.shared.media.gpu_frame import (
                GpuDecodedFrame, GpuFramePlane, GpuFrameRelease,
                GpuPlaneResource,
            )

            if self._metal_surface_pool is None:
                self._metal_surface_pool = ToposIOSurfacePool(
                    int(self._summary.width), int(self._summary.height),
                    pixel_format=int(self._summary.pixel_format),
                    plane_count=3,
                    # T-P0-31：导出生产者流水线（队列深 3 + 渲染中 1 +
                    # 编码在途若干）会同时持有多个平面租约，ring=3 在
                    # 1080p 长序列上间歇性饿死 → CPU 帧回落 → bridge
                    # 拒收。8 槽 ≈ 66MB@1080p422，换取确定性。
                    ring_size=8,
                    # The lease is fenced by the caller's Metal command
                    # completion; no second CPU-side fence is manufactured.
                    fence_callback=lambda _surface: None,
                )
            pool = self._metal_surface_pool
            lease = pool.acquire()
            if lease is None:
                # 槽位由 Metal 命令完成回调释放（毫秒级）；短暂等待而非
                # 立即降级 CPU 路径——bridge 激活时单帧 CPU 回落会硬失败。
                deadline = time.monotonic() + 0.5
                while lease is None and time.monotonic() < deadline:
                    time.sleep(0.002)
                    lease = pool.acquire()
            if lease is None:
                self._metal_surface_failures += 1
                return None
            lease.acquire()
            # C18：fenced lease → 成功转交 GpuFrameRelease 全程统一异常
            # 清理——历史构造帧/取 PTS 抛错时外层直接返回 None，
            # lease.release() 未执行，池槽位随失败累积泄漏（饿死后永久
            # 回落 CPU）。所有权转交点 = GpuDecodedFrame 构造成功。
            lease_owned = True
            try:
                lease.map()
                packet, packet_size = self._movie.packet_into(int(frame_index))
                info = self._decoder_ctx.decode_surface_lease(
                    packet, lease, size=packet_size,
                )
                if (int(info.plane_count) != 3 or int(info.pixel_format) != 0
                        or int(info.bit_depth) != 10):
                    raise ToposSurfaceUnavailable(
                        "Metal Topos path currently requires 3-plane 10-bit 4:2:2"
                    )
                surface = lease.resource
                # Device creation is relatively expensive and cannot improve
                # correctness per frame.  Cache it for the lifetime of this
                # media source; reset in close() so a reopened source does not
                # retain native objects indefinitely.
                if self._metal_device is None:
                    self._metal_device = Metal.MTLCreateSystemDefaultDevice()
                device = self._metal_device
                if device is None:
                    raise ToposSurfaceUnavailable("Metal device unavailable")
                widths = (
                    int(info.visible_width),
                    (int(info.visible_width) + 1) // 2,
                    (int(info.visible_width) + 1) // 2,
                )
                # The pool slot is reused after the renderer releases its
                # fence.  An imported MTLTexture remains bound to that
                # IOSurface plane, so keeping it avoids three descriptor and
                # import calls per frame while preserving the same ownership
                # and fence protocol.
                cache_key = id(surface)
                textures = self._metal_texture_cache.get(cache_key)
                if textures is None:
                    created = []
                    for plane, width in enumerate(widths):
                        descriptor = (
                            Metal.MTLTextureDescriptor
                            .texture2DDescriptorWithPixelFormat_width_height_mipmapped_(
                                Metal.MTLPixelFormatR16Uint,
                                width,
                                int(info.visible_height),
                                False,
                            )
                        )
                        descriptor.setUsage_(Metal.MTLTextureUsageShaderRead)
                        texture = device.newTextureWithDescriptor_iosurface_plane_(
                            descriptor, surface, plane,
                        )
                        if texture is None:
                            raise ToposSurfaceUnavailable(
                                f"Metal IOSurface texture import failed for plane {plane}"
                            )
                        created.append(texture)
                    textures = tuple(created)
                    self._metal_texture_cache[cache_key] = textures
                # Native decode is complete before exposing the IOSurface to
                # Metal.  Unlocking here makes the shared contents visible;
                # the release callback keeps the surface leased until the
                # renderer's command buffer fence has completed.
                lease.unmap()
                lease.fence()
            except Exception:
                try:
                    if lease.state.value in ("mapped", "decoding"):
                        if lease.state.value == "decoding":
                            lease.end_decode()
                        lease.unmap()
                    if lease.state.value == "unmapped":
                        lease.fence()
                    if lease.state.value in ("fenced", "unmapped"):
                        lease.release()
                    if lease.state.value == "acquired":
                        lease.abort()
                except Exception:
                    logger.debug(
                        "ToposMediaSource: failed to unwind Metal surface lease",
                        exc_info=True,
                    )
                raise

            # C18：post-fence 段（PTS/平面/帧构造）失败同样要释放 lease——
            # 内层 try 的状态机清理只覆盖到 fence()。所有权转交点 =
            # GpuDecodedFrame 构造成功（成功路径只释放一次）。
            try:
                pts, _duration = self._movie.pts(int(frame_index))
                stride_bytes = tuple(
                    int(pool._slots[0].row_bytes[i])
                    if getattr(pool, "_slots", None) else 0
                    for i in range(3)
                )
                planes = []
                for i, (component, width, texture) in enumerate(
                        zip(("Y", "U", "V"), widths, textures)):
                    resource = GpuPlaneResource(
                        api="metal",
                        native_handle=texture,
                        resource_format="r16uint",
                        width=width,
                        height=int(info.visible_height),
                        stride_bytes=stride_bytes[i],
                        target="2d",
                        ownership="borrowed",
                    )
                    planes.append(GpuFramePlane(
                        component=component,
                        api="metal",
                        texture_id=0,
                        target="2d",
                        width=width,
                        height=int(info.visible_height),
                        internal_format="r16uint",
                        pixel_format="topos_yuv422p10",
                        pixel_type="uint16",
                        resource=resource,
                    ))

                release = GpuFrameRelease(
                    retain_token=lease.resource,
                    release_callback=lease.release,
                    owner_backend="metal",
                    destructor_policy="warn_only",
                )
                self._metal_surface_frames += 1
                timescale = int(self._summary.timescale or 1)
                frame = GpuDecodedFrame(
                    frame_index=int(frame_index),
                    pts=int(pts),
                    time_seconds=float(pts) / timescale,
                    width=int(info.visible_width),
                    height=int(info.visible_height),
                    visible_rect=(0, 0, int(info.visible_width), int(info.visible_height)),
                    pixel_format="yuv422p10le",
                    bit_depth=int(info.bit_depth),
                    planes=tuple(planes),
                    color_range=("full" if int(self._summary.color_range) == 1
                                 else "limited"),
                    color_matrix=str(getattr(self._metadata, "color_matrix", "bt709")
                                     or "bt709"),
                    color_primaries=str(
                        getattr(self._metadata, "color_primaries", "bt709")
                        or "bt709"),
                    color_transfer=str(
                        getattr(self._metadata, "color_transfer", "bt709")
                        or "bt709"),
                    decode_path=DecodePath.MACOS_TOPOS_IOSURFACE_ZERO_COPY.value,
                    zero_copy=True,
                    # T-P0-31：export telemetry 以 source_kind 作为 decode 阶段
                    # backend；留空默认值会把该阶段记成 "unknown"（非 GPU 词表
                    # → 误报 blocker）。原生解码器写映射 IOSurface 平面，
                    # cpu_upload_bytes 恒为 0，如实标记 topos_native。
                    source_kind="topos_native",
                    release=release,
                )
            except BaseException:  # noqa: BLE001 — 释放后原样上抛
                if lease_owned:
                    try:
                        lease.release()
                    except Exception as release_error:
                        logger.warning(
                            "ToposMediaSource: 释放 Metal surface lease 失败"
                            "（frame %s）: %s", frame_index, release_error)
                raise
            # 构造成功 → 所有权随 release 转交消费方
            lease_owned = False
            return frame
        except Exception as error:
            self._metal_surface_failures += 1
            logger.info(
                "ToposMediaSource: Metal surface decode unavailable for frame %s: %s",
                frame_index, error,
            )
            return None

    def seek_frame(self, frame_index: int) -> None:
        """定位到指定帧（全 intra + O(1) 索引：仅移动游标，无状态冲洗）。

        读前缓存按顺序游标填充：任何重定位直接失效，首帧回到单帧路径。
        """
        self._require_open()
        total = self._total()
        if not 0 <= frame_index < total:
            raise ValueError(f"帧号越界: {frame_index}（共 {total} 帧）")
        if self._is_v9:
            # V9（§4.4）：顺序前进由 _gop_pos 单帧 O(1) feed；seek/跨 GOP
            # 由 _v9_decode_gop prev_sync 定位重解（缓存 ≤1 帧，RSS 有界）。
            self._current_frame = frame_index
            return
        self._ra_stats['cache_discarded'] += len(self._ra_cache)
        self._current_frame = frame_index
        self._ra_cache.clear()
        self._ra_seq = False

    def read_next_frame(self) -> DecodedFrame:
        """顺序读取下一帧（调度层实际依赖的接口；EOF 抛 StopIteration）。

        M2 低拷贝路径：packet 复用缓冲 + 持久 context 单次原生调用 +
        池化 numpy 平面直写（零 packet/输出复制）；旧 dylib 回退旧路径。
        阶段4 兑现：顺序前进确认后（第二次顺序读起）一次批量解码 depth
        帧进池化平面，首帧即时返回、其余入缓存——后续顺序读纯命中（仅
        装配）。seek/跳读自动失效；批量被整批拒绝时回退单帧，错误语义
        与逐帧解码一致。
        """
        self._require_open()
        index = self._current_frame
        if index >= self._total():
            raise StopIteration("End of video stream")
        if self._is_v9:
            return self._v9_read_next()
        movie = self._movie
        codec = self._codec
        assert movie is not None and codec is not None
        if self._decoder_ctx is not None and self._plane_pool is not None:
            decoder = self._decoder_ctx
            pool = self._plane_pool
            try:
                if self._ra_depth > 0:
                    hit = self._ra_cache.pop(index, None)
                    if hit is not None:
                        self._ra_stats['cache_hits'] += 1
                        entry, arrays, info, pts = hit
                        self._current_frame = index + 1
                        self._ra_seq = True
                        return self._assemble_decoded_frame(
                            index, info, list(arrays), pts, pool_entry=entry)
                    if self._ra_seq:
                        served = self._read_ahead(index)
                        if served is not None:
                            return served
                        # 整批拒绝/末尾不足 2 帧 → 回退本帧单帧路径（保持错误语义）
                        self._ra_stats['batch_fallbacks'] += 1
                    else:
                        # 首帧单解，下一帧起顺序性确认、批量读前
                        self._ra_seq = True
                decode_request = self._decode_request
                is_base_packet = False
                if (decode_request is not None and self._bitstream_major == 7 and
                        getattr(codec, "_has_movie_packet_base", False) and
                        int(decode_request.mode) in (
                            TC_DECODE_MODE_REDUCED, TC_DECODE_MODE_AUTO_2K)):
                    buf, n, is_base_packet = movie.packet_base_into(index)
                    if is_base_packet:
                        self._v7b_base_range_decode = True
                        self._v7b_base_range_samples += 1
                        # The embedded base is already the bounded preview
                        # layer. Applying the source-relative ratio again would
                        # undersize the output (for example, 4K/2K -> 1K).
                        decode_request = ToposDecoder.make_request(
                            TC_DECODE_MODE_FULL)
                else:
                    buf, n = movie.packet_into(index)
                entry, info = pool.acquire(
                    decoder, codec, buf, n, request=decode_request)
                pts, _ = movie.pts(index)
            except ToposCodecError as exc:
                raise MediaDecodeError(
                    f"Topos 解码失败（帧 {index}）: {exc}", source_path=self._path
                ) from exc
            self._current_frame = index + 1
            # AUD-35900：传列表副本（与 1273/1796 两处调用一致）——装配
            # 尾部即使回归为原地改写，也不会替换 entry.arrays[3] 使原始
            # alpha 平面在 entry.views 仍持其裸地址时被释放。
            return self._assemble_decoded_frame(index, info, list(entry.arrays),
                                                pts, pool_entry=entry)
        try:
            packet = movie.packet(index)
            frame = codec.decode(packet)  # 坏 slice 按 §9 conceal，帧仍交付
            pts, _ = movie.pts(index)
        except ToposCodecError as exc:
            raise MediaDecodeError(
                f"Topos 解码失败（帧 {index}）: {exc}", source_path=self._path
            ) from exc
        self._current_frame = index + 1
        return self._build_decoded_frame(index, frame, pts)

    def _audio_chunk_prefix_index(self, ai, frame_bytes: int) -> np.ndarray:
        """C17：chunk 前缀采样帧索引（惰性构建一次，文件只读 → 终身有效）。

        逐 chunk probe 精确字节长度（不读数据），累计采样帧数。构建成本
        O(chunk_count) 次 O(1) 元数据查询；seek 只剩二分 + 覆盖区间的
        数据读取。
        """
        if self._audio_chunk_prefix is None:
            prefix = np.zeros(ai.chunk_count + 1, dtype=np.int64)
            for i in range(ai.chunk_count):
                prefix[i + 1] = prefix[i] + (
                    self._movie.audio_chunk_size(i) // frame_bytes)
            self._audio_chunk_prefix = prefix
        return self._audio_chunk_prefix

    def read_audio_samples(self, start_pts: int, count: int) -> Optional[np.ndarray]:
        """读取音频采样（v1.1；container_spec_v1.1 音频轨）。

        Args:
            start_pts: 起始采样帧序号（音频 timescale = 采样率，1 tick =
                1 采样帧）
            count: 请求的采样帧数

        Returns:
            (samples, channels) numpy 数组：整数 PCM（s16/s24/s32）为 int32
            （s16/s24 符号扩展、s32 原值；大端存储已转换为主机序）；
            float32 母带（TC_AUDIO_FMT_FLOAT32）为 float32，取值域 [-1, 1]
            语义直读（C06：不再按位深误读为 int32）。
            无音轨 / 起点越界 → None；
            末尾不足 count 时截断到实际可用长度。
            mp4a 档返回 None（容器只存 AAC 包，本层不解码——回放音频
            恒取 master，ADR-C008 语义不变）。
        """
        if self._movie is None:
            return None
        ai = self._movie.audio_info()
        if not ai.has_audio or ai.codec == TC_AUDIO_CODEC_MP4A:
            return None
        if ai.codec != TC_AUDIO_CODEC_LPCM or ai.bits_per_sample not in (16, 24, 32):
            return None
        total_frames = ai.sample_count
        start = max(0, int(start_pts))
        if start >= total_frames or count <= 0:
            return None
        want = min(int(count), total_frames - start)
        bytes_per_sample = ai.bits_per_sample // 8
        frame_bytes = ai.channel_count * bytes_per_sample
        # AUD-35906：channel_count=0（损坏音轨头）使 frame_bytes=0，前缀
        # 索引（audio_chunk_size // frame_bytes）与 n_frames 计算会除零。
        # 按本方法"不可解码返回 None"的既有约定处理。
        if frame_bytes <= 0:
            return None

        # C17（2026-09-27 检查计划）：chunk 前缀采样索引 + 二分定位——
        # 历史每次读取都从 chunk 0 线性累积（尾部 seek 代价随总时长线性
        # 增长；浮点音频每 1024 采样帧一个 chunk，一小时 48kHz ≈ 16.9 万
        # chunk）。索引惰性构建一次（probe-only，无数据 I/O），内存
        # O(chunk_count)（int64 前缀和，一小时轨 ≈ 1.3 MB，有上限）。
        prefix = self._audio_chunk_prefix_index(ai, frame_bytes)
        n_chunks = len(prefix) - 1
        chunk = int(np.searchsorted(prefix, start, side='right')) - 1
        if chunk < 0:
            chunk = 0
        pieces: list[np.ndarray] = []
        taken = 0
        while chunk < n_chunks and taken < want:
            data = self._movie.read_audio_samples(chunk, 1)
            n_frames = len(data) // frame_bytes
            chunk_start = int(prefix[chunk])
            chunk_end = chunk_start + n_frames
            lo = max(start, chunk_start) - chunk_start
            hi = min(start + want, chunk_end) - chunk_start
            if hi > lo:
                piece = data[lo * frame_bytes:hi * frame_bytes]
                pieces.append(piece)
                taken += hi - lo
            chunk += 1
        if not pieces:
            return None
        raw = b"".join(pieces)
        if ai.bits_per_sample == 16:
            arr = np.frombuffer(raw, dtype='>i2').astype(np.int32)
        elif ai.bits_per_sample == 24:
            b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
            arr = (b[:, 0].astype(np.int32) << 16) | \
                  (b[:, 1].astype(np.int32) << 8) | b[:, 2].astype(np.int32)
            arr = np.where(arr >= 1 << 23, arr - (1 << 24), arr)
        elif ai.bits_per_sample == 32 and \
                getattr(ai, "sample_format", TC_AUDIO_FMT_INT) == TC_AUDIO_FMT_FLOAT32:
            # C06：float32 母带（lpcm formatFlags 0xB）按 IEEE 大端浮点解析。
            # 历史按位深分支一律 >i4——0.25 被读成 1048576000。
            arr = np.frombuffer(raw, dtype='>f4').astype(np.float32)
        else:
            arr = np.frombuffer(raw, dtype='>i4').astype(np.int32)
        return arr.reshape(-1, ai.channel_count)

    @property
    def is_open(self) -> bool:
        """是否已打开。"""
        return self._movie is not None

    @property
    def current_frame(self) -> int:
        """当前游标（下一帧序号，从 0 开始）。"""
        return self._current_frame

    @property
    def path(self) -> str:
        """媒体源路径。"""
        return self._path

    @property
    def diagnostics(self) -> dict:
        """P1-04：解码路径诊断（性能报告不得在退化路径上误标低拷贝）。

        decode_path: 'closed' | 'legacy'（高拷贝逐帧）| 'context'（低拷贝
        单帧）| 'context-batch'（低拷贝 + 读前批量）；decode_path_reason
        记录退化原因（dylib 缺符号 / decoder 创建失败 / 批量符号缺失）。
        """
        d = {
            'decode_path': self._decode_path,
            'decode_path_reason': self._decode_path_reason,
            'native_target_decode': self._native_target_decode,
            'drop_alpha_requested': self._drop_alpha_requested,
            'native_drop_alpha': self._native_drop_alpha,
            'native_no_deblock': self._native_no_deblock,
            'bitstream_major': self._bitstream_major,
            'stream_compatibility': self._stream_compatibility,
            'legacy_reduced_fallback': self._legacy_reduced_fallback,
            'v7b_base_range_decode': self._v7b_base_range_decode,
            'v7b_base_range_samples': self._v7b_base_range_samples,
            'decode_request_mode': (
                int(self._decode_request.mode)
                if self._decode_request is not None else None
            ),
            'decode_request_scale': (
                int(self._decode_request.scale)
                if self._decode_request is not None else None
            ),
            'read_ahead_depth': self._ra_depth,
            'read_ahead_stats': dict(self._ra_stats),
            'decode_workers_requested': self._decode_workers_requested,
            'decode_workers_assigned': self._decode_workers_assigned,
            'decode_worker_budget': DECODE_WORKER_BUDGET.describe(),
            'metal_surface_frames': self._metal_surface_frames,
            'metal_surface_failures': self._metal_surface_failures,
            'metal_surface_active': self._metal_surface_pool is not None,
        }
        if self._plane_pool is not None:
            d['plane_pool_entries'] = self._plane_pool._max
            if self._summary is not None:
                d['frame_bytes_estimate'] = self._frame_plane_bytes()
        return d

    @property
    def color_semantics(self) -> ToposColorSemantics:
        """已校验的 MOV 色彩语义；与 ToposImageSource 共用同一解析器。"""
        self._require_open()
        if self._color_semantics is None:
            raise MediaOpenError("Topos MOV 色彩语义尚未解析", source_path=self._path)
        return self._color_semantics

    # —— VideoSource 便捷属性（duck-type 补全；播放/导出侧 hasattr 探测用） ——

    @property
    def total_frames(self) -> int:
        """总帧数。"""
        self._require_open()
        return self._total()

    @property
    def fps(self) -> float:
        """帧率（浮点；精确值见 probe().fps 的 Rational）。"""
        self._require_open()
        assert self._metadata is not None
        return self._metadata.fps.to_fps()

    @property
    def width(self) -> int:
        """视频宽度（像素）。"""
        self._require_open()
        assert self._summary is not None
        return self._summary.width

    @property
    def height(self) -> int:
        """视频高度（像素）。"""
        self._require_open()
        assert self._summary is not None
        return self._summary.height

    @property
    def duration(self) -> float:
        """时长（秒）。"""
        self._require_open()
        assert self._metadata is not None
        return self._metadata.duration_seconds or 0.0

    def get_frame_at_time(self, time_seconds: float) -> DecodedFrame:
        """读取指定时间点的帧（夹取到有效范围）。"""
        self._require_open()
        assert self._metadata is not None
        frame = self._metadata.fps.seconds_to_frame(max(0.0, float(time_seconds)))
        return self.read_frame(max(0, min(frame, self._total() - 1)))

    def get_thumbnail(
        self, frame_index: int = 0, max_size: int = 320
    ) -> Optional[np.ndarray]:
        """获取缩略图（暂不实现，协议允许 None）。"""
        return None

    # —— 可选能力探测（playback_clock hasattr 探测的接口）——

    def set_preserve_planes(self, value: bool) -> None:
        """接受 preserve_planes 开关（本源恒 planar，不改变行为）。"""
        self._preserve_planes = bool(value)

    @property
    def is_planar_mode(self) -> bool:
        """是否 planar 输出（恒 True）。"""
        return True

    @property
    def output_mode(self) -> str:
        """输出模式：YUV 流 = 'planar_yuv'；GBR 流 = 'planar_rgb'；
        TRAW CFA 流 = 'planar_cfa'（复验 P1-09：GBR 平面是 (G,B,R) 恒等序，
        下游按 YUV 消费会误解色差平面语义；M4-R9：CFA 相位平面同理）。"""
        pf = 0
        if self._summary is not None:
            pf = int(getattr(self._summary, 'pixel_format', 0) or 0)
        if pf == 3:
            return 'planar_cfa'
        return 'planar_rgb' if pf == 2 else 'planar_yuv'

    @property
    def raw_meta(self) -> "Optional[dict]":
        """TRWM as-shot 元数据（RC1；无 trwm 原子 → None）。"""
        return self._trwm

    # —— 上下文管理 / 析构 ——

    def __enter__(self) -> 'ToposMediaSource':
        self.open()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    # —— 内部 ——

    def _require_open(self) -> None:
        if self._movie is None:
            raise MediaSourceError(f"媒体源未打开: {self._path}", source_path=self._path)

    def _total(self) -> int:
        assert self._summary is not None
        return self._summary.sample_count

    def _frame_plane_bytes(self) -> int:
        """单帧全平面字节数（moov 摘要几何恒定；alpha/色度计入）。"""
        s = self._summary
        assert s is not None
        if self._native_target_decode and self._target_size is not None:
            w, h = self._target_size
        else:
            w, h = int(s.width), int(s.height)
        if int(s.pixel_format) == 3:
            # M4-R9：TRAW = 4 相位平面各 ceil(W/2)×ceil(H/2)，无 alpha
            planes_px = 4 * ((w + 1) // 2) * ((h + 1) // 2)
            return max(planes_px * 2, 1)
        cw = w if int(s.pixel_format) else (w + 1) // 2
        planes_px = w * h + 2 * cw * h
        if s.alpha_mode != 0 and not self._native_drop_alpha:
            planes_px += w * h
        return max(planes_px * 2, 1)

    def _readahead_depth(self) -> int:
        """读前批量深度：构造参数 > TOPOS_READAHEAD > 角色对应默认值。

        P1-03：预算按"平面池条目 max(5, depth+2) × 帧平面字节"记账，任何
        来源的深度都不得突破 _READAHEAD_BUDGET_BYTES（安全默认）。显式
        参数/环境变量只能在预算内选择更小深度，钳到预算容量并记 warning；
        确需突破必须同时设 TOPOS_UNSAFE_READAHEAD 真值（仍钳 32），以
        warning 记录估计占用。自适应在预算容不下最小摊销深度时诚实关闭
        （depth=0）而非超预算预取。
        """
        raw = self._read_ahead_param
        if raw is None:
            env = os.environ.get('TOPOS_READAHEAD')
            if env is not None:
                try:
                    raw = int(env)
                except ValueError:
                    logger.warning(f"TOPOS_READAHEAD={env!r} 非法，忽略（自动深度）")
                    raw = None
        frame_bytes = self._frame_plane_bytes()
        # 预算可容纳的池条目数 → 深度上限 = 条目 − 2 余量；条目 < 5（池基线
        # 都放不下）时深度必须为 0（池基线占用经 open 的 debug 日志可见）
        entries_allow = _READAHEAD_BUDGET_BYTES // frame_bytes
        cap = max(0, entries_allow - 2) if entries_allow >= 5 else 0
        if raw is not None:
            explicit = max(0, int(raw))
            unsafe = os.environ.get(_READAHEAD_UNSAFE_ENV, '').strip().lower() in _TRUTHY
            if unsafe:
                depth = min(explicit, _READAHEAD_HARD_MAX_DEPTH)
                est = max(5, depth + 2) * frame_bytes
                logger.warning(
                    "%s：读前深度 %d 突破内存预算（估计池占用 ≈ %.1f MiB，"
                    "预算 %d MiB，帧 ≈ %.1f MiB）",
                    _READAHEAD_UNSAFE_ENV, depth, est / (1 << 20),
                    _READAHEAD_BUDGET_BYTES >> 20, frame_bytes / (1 << 20),
                )
                return depth
            depth = min(explicit, cap, _READAHEAD_HARD_MAX_DEPTH)
            if depth != explicit:
                logger.warning(
                    "读前深度显式请求 %d 超出内存预算，钳到 %d（帧 ≈ %.1f MiB，"
                    "预算 %d MiB；确需突破设 %s=1）",
                    explicit, depth, frame_bytes / (1 << 20),
                    _READAHEAD_BUDGET_BYTES >> 20, _READAHEAD_UNSAFE_ENV,
                )
            return depth
        # 播放路径以每帧尾延迟为优先；导出路径的顺序生产者可由编码队列
        # 吸收批首延迟，因此自动启用一个受预算约束的小深度。
        default_depth = (
            _READAHEAD_EXPORT_DEPTH
            if self._decode_role == "export"
            else _READAHEAD_MAX_DEPTH
        )
        depth = max(0, min(cap, default_depth))
        return depth

    def _resolve_decode_workers(self) -> int:
        """Resolve source worker preference without changing the native pool."""
        raw = self._decode_workers_param
        if raw is None:
            env = os.environ.get("TOPOS_DECODE_WORKERS")
            if env is not None:
                try:
                    raw = int(env)
                except ValueError:
                    logger.warning("TOPOS_DECODE_WORKERS=%r 非法，使用共享预算", env)
                    raw = None
        if raw is None:
            requested = 0
        else:
            requested = max(0, min(16, int(raw)))
            if int(raw) != requested:
                logger.warning("decode_workers=%r 被钳到 %d", raw, requested)
        self._decode_workers_requested = requested
        return requested

    def _build_target_decode_request(self):
        """将 target_size 映射为 native reduced 或 AUTO_2K request。

        只有目标几何严格等于现有固定比例时才使用低频 reduced；任意目标
        尺寸使用 AUTO_2K 携带精确目标几何。两者都直接写目标平面，不再
        为播放器创建完整源分辨率 numpy 中间帧；V7-B 可在 AUTO_2K 下读取
        有界 base range，再在 base 上完成目标尺寸重采样。
        """
        assert self._summary is not None
        source_w = int(self._summary.width)
        source_h = int(self._summary.height)
        flags = TC_DECODE_FLAG_DROP_ALPHA if self._native_drop_alpha else 0
        if self._native_no_deblock:
            # 预览提速：跳过输出去块（只对带特性位的 V7-R2 产品流可见）
            flags |= TC_DECODE_FLAG_NO_DEBLOCK
        if self._target_size is None:
            return ToposDecoder.make_request(
                TC_DECODE_MODE_FULL, flags=flags)
        target_w, target_h = self._target_size
        ratios = (
            (TC_DECODE_SCALE_HALF, 2),
            (TC_DECODE_SCALE_THIRD, 3),
            (TC_DECODE_SCALE_QUARTER, 4),
            (TC_DECODE_SCALE_EIGHTH, 8),
        )
        for scale, divisor in ratios:
            if ((source_w + divisor - 1) // divisor == target_w and
                    (source_h + divisor - 1) // divisor == target_h):
                return ToposDecoder.make_request(
                    TC_DECODE_MODE_REDUCED, scale=scale, flags=flags)
        return ToposDecoder.make_request(
            TC_DECODE_MODE_AUTO_2K, target_size=(target_w, target_h),
            flags=flags)

    def _apply_decode_workers(self, workers: int) -> None:
        decoder = self._decoder_ctx
        if decoder is None:
            return
        decoder.set_max_slice_workers(int(workers))
        self._decode_workers_assigned = int(workers)

    def _read_ahead(self, index: int) -> Optional[DecodedFrame]:
        """顺序读前批量解码：一次 decode_views_batch 解 depth 帧进池化平面。

        有批量聚读符号时经 packets_into_batch 一次原生调用（连续布局单次
        pread）取整批；V7-B preview 则经 packets_base_into_batch 逐帧只取
        header/directory/base range。两者都解码直读 arena 槽位（零 bytes
        物化拷贝）；旧 dylib 回退逐包 packet()。首帧即时返回，其余入缓存（缓存 tuple 持平面
        引用 → 池判定"使用中"，不会被后续批次覆盖）。返回 None 表示本批
        不可用（末尾不足 2 帧 / 整批被拒——含批内任一 packet 结构损坏），
        调用方回退单帧路径：坏帧在轮到它时才以单帧语义报错，邻帧交付不受
        影响。
        """
        movie, codec = self._movie, self._codec
        decoder, pool = self._decoder_ctx, self._plane_pool
        assert movie is not None and codec is not None
        assert decoder is not None and pool is not None
        total = self._total()
        count = min(self._ra_depth, total - index)
        if count < 2:
            return None
        try:
            batch_request = self._decode_request
            packets = None
            if (self._bitstream_major == 7 and self._decode_request is not None and
                    getattr(codec, "_has_movie_packet_base_batch", False)):
                arena, offs, sizes, base_flags = movie.packets_base_into_batch(
                    index, count)
                if all(int(base_flags[k]) != 0 for k in range(count)):
                    self._v7b_base_range_decode = True
                    self._v7b_base_range_samples += count
                    batch_request = None
                    base = ctypes.addressof(arena)
                    packets = [
                        (base + int(offs[k]), int(sizes[k]))
                        for k in range(count)
                    ]
                else:
                    # 普通 V7/V7-R 样本没有 embedded base，这是单帧路径
                    # 的合法分支，不应让整个批量预读退化为逐帧解码。此时
                    # 读取完整 sample，再把同一个 reduced request 交给
                    # batch decoder；真正的 V7-B 全 base 批次仍走上面的
                    # range-only 路径，避免读取 residual。
                    self._ra_stats['base_batch_fallbacks'] += 1
            if packets is None and codec._has_movie_packet_batch:
                arena, offs, sizes = movie.packets_into_batch(index, count)
                base = ctypes.addressof(arena)
                packets = [(base + int(offs[k]), int(sizes[k])) for k in range(count)]
            elif packets is None:
                packets = [movie.packet(index + k) for k in range(count)]
            entries: list[_ToposPlaneEntry] = []
            views_list = []
            hold: list[tuple] = []  # 批内并行存活的平面组（防池复用覆盖）
            pts_list = []
            for k in range(count):
                pkt, sz = packets[k] if isinstance(packets[k], tuple) \
                    else (packets[k], None)
                pinfo = (decoder.prepare_request(pkt, batch_request, sz)
                         if batch_request is not None else decoder.prepare(pkt, sz))
                entry = pool.acquire_entry(decoder, codec, pinfo)
                entries.append(entry)
                views_list.append(entry.views)
                hold.append(tuple(entry.arrays))
                pts, _ = movie.pts(index + k)
                pts_list.append(pts)
            infos = (
                decoder.decode_views_batch_request(
                    packets, views_list, batch_request)
                if batch_request is not None
                else decoder.decode_views_batch(packets, views_list)
            )
        except ToposCodecError:
            logger.debug(
                "读前批量解码被拒（帧 %d 起），回退单帧路径", index, exc_info=True
            )
            return None
        for k in range(count):
            self._ra_cache[index + k] = (
                entries[k], tuple(entries[k].arrays), infos[k], pts_list[k],
            )
        self._ra_stats['batch_decodes'] += 1
        self._ra_stats['prefetched_frames'] += max(0, count - 1)
        self._current_frame = index + 1
        self._ra_seq = True
        _entry, arrays, info, pts = self._ra_cache.pop(index)
        return self._assemble_decoded_frame(index, info, list(arrays), pts,
                                            pool_entry=_entry)

    def _build_metadata(
        self, s: ToposMovieSummary, dur0: int, *, duration_ticks: int
    ) -> VideoMetadata:
        """tpcC 轨级配置 → VideoMetadata（色彩码表见 bitstream_spec §A.7）。

        色彩字段先通过与 `.toos` 共用的参考解析器；未知或互相矛盾的字段
        直接拒绝打开，不再在视频路径静默猜测 sRGB。MOV 当前没有独立 ICC/OCIO
        chunk，因此 tpcC/CICP 是数值重建和显示标签的唯一权威。
        """
        semantics = resolve_topos_color_semantics({
            'pixel_format': int(s.pixel_format),
            'bit_depth': int(s.bit_depth),
            'color_range': int(s.color_range),
            'color_primaries': int(s.color_primaries),
            'color_transfer': int(s.color_transfer),
            'color_matrix': int(s.color_matrix),
            'chroma_siting': int(s.chroma_siting),
        })
        self._color_semantics = semantics
        fps = (
            Rational(s.timescale * s.sample_count, duration_ticks)
            if duration_ticks > 0 and s.sample_count > 0
            else Rational(24, 1)
        )
        # v1.7 tpcD 档位（container_spec v1.7）：导出时写入的精确档位
        # （'Topos 422 HQ' 等）；旧文件无 tpcD → tier_id=0，回退码流
        # profile 名（'Topos 422'）。
        codec_profile = (
            topos_tier_label_by_numeric_id(s.tier_id)
            or _PROFILE_NAMES.get(s.profile, f'Profile {s.profile}')
        )
        return VideoMetadata(
            width=s.width,
            height=s.height,
            fps=fps,
            total_frames=s.sample_count,
            codec_name='topos',
            codec_profile=codec_profile,
            pixel_format=_pixel_format_name(s.alpha_mode, s.alpha_bit_depth,
                                             s.bit_depth, s.pixel_format),
            bit_depth=s.bit_depth,
            color_primaries=map_color_primaries(s.color_primaries),
            color_transfer=map_color_transfer(s.color_transfer),
            color_matrix=map_color_space(s.color_matrix),
            color_range='full' if s.color_range == 1 else 'limited',
            sample_aspect_ratio=(
                Rational(s.sar_num, s.sar_den) if s.sar_num and s.sar_den else None
            ),
            **self._timecode_metadata_fields(),
            has_alpha=s.alpha_mode != 0,
            field_order='progressive',
            rotation=0,
            timebase=Rational(1, s.timescale),
            duration_ticks=duration_ticks,
            **self._audio_metadata_fields(),
        )

    _AUDIO_CODEC_NAMES = {
        (TC_AUDIO_CODEC_LPCM, 16): 'pcm_s16be',
        (TC_AUDIO_CODEC_LPCM, 24): 'pcm_s24be',
        (TC_AUDIO_CODEC_LPCM, 32): 'pcm_s32be',
        (TC_AUDIO_CODEC_MP4A, 0): 'aac',
    }

    def _audio_codec_display_name(self, ti) -> str:
        """(codec, bits, fmt) → ffprobe 同名显示名（f32 优先于 s32）。"""
        if ti.codec == TC_AUDIO_CODEC_LPCM and ti.bits_per_sample == 32 \
                and int(ti.sample_format) == 1:
            return 'pcm_f32be'
        return self._AUDIO_CODEC_NAMES.get(
            (ti.codec, ti.bits_per_sample), f'topos_audio_{ti.codec}')

    def _timecode_metadata_fields(self) -> dict:
        """v1.5 tmcd 起始时码 → VideoMetadata 字段（无 tmcd 轨三者同 None）。

        timecode = HH:MM:SS[;:]FF 标准显示形态（DF 以分号区分）；
        fps/drop_frame 供时间线 TC 对位。读回失败（无 tmcd 轨 → C 层
        TC_ERR_STATE）静默降级为无时码，不影响纯视频打开路径。
        """
        none = {'timecode': None, 'timecode_fps': None,
                'timecode_drop_frame': None}
        if self._movie is None:
            return none
        try:
            tc = self._movie.timecode()
        except ToposCodecError:
            return none
        return {
            'timecode': tc.format(),
            'timecode_fps': int(tc.fps),
            'timecode_drop_frame': bool(tc.drop_frame),
        }

    def _audio_metadata_fields(self) -> dict:
        """v1.1/v1.6 音频轨 → VideoMetadata 音频字段。

        旧字段（has_audio/channels/rate/codec）恒为首轨视图；v1.6 起另给
        audio_tracks 逐轨元组（含轨名），无音轨为空元组。
        """
        if self._movie is None:
            return {'has_audio': False}
        try:
            n_tracks = self._movie.audio_track_count()
        except ToposCodecError:
            n_tracks = 0
        if n_tracks == 0:
            return {'has_audio': False, 'audio_tracks': ()}
        tracks = []
        for i in range(n_tracks):
            ti = self._movie.audio_info_at(i)
            tracks.append({
                'name': ti.name,
                'codec': self._audio_codec_display_name(ti),
                'sample_rate': int(ti.sample_rate),
                'channel_count': int(ti.channel_count),
                'channel_layout': int(ti.channel_layout),
                'bits_per_sample': int(ti.bits_per_sample),
                'sample_format': int(ti.sample_format),
                'priming_samples': int(ti.priming_samples),
            })
        return {
            'has_audio': True,
            'audio_channels': tracks[0]['channel_count'],
            'audio_sample_rate': tracks[0]['sample_rate'],
            'audio_codec': tracks[0]['codec'],
            'audio_tracks': tuple(tracks),
        }

    def _build_decoded_frame(
        self,
        index: int,
        frame: ToposFrame,
        pts: int,
    ) -> DecodedFrame:
        """ToposFrame（tight uint16 LE bytes）→ planar DecodedFrame（旧路径）。"""
        info = frame.info
        vw, vh = int(info.visible_width), int(info.visible_height)
        # R4.2/R4.3：pf≠0（4:4:4/GBR）第 2/3 平面全宽；pf=0 半宽全高
        # （帧头为权威，tpcC 恒一致——mux 保证）；M4-R9：pf=3 全平面半几何
        plane_geoms = _plane_geoms(int(info.pixel_format),
                                   int(info.plane_count), vw, vh)

        planes: list[np.ndarray] = []
        for i in range(info.plane_count):
            w, h = plane_geoms[i]
            raw = frame.planes[i]
            if len(raw) != w * h * 2:
                raise MediaDecodeError(
                    f"Topos 平面 {i} 字节数 {len(raw)} != {w}×{h}×2",
                    source_path=self._path,
                )
            # read-only 视图（零拷贝；消费方不得就地改写解码帧）
            planes.append(np.frombuffer(raw, dtype='<u2').reshape(h, w))
        return self._assemble_decoded_frame(index, info, planes, pts)

    def _assemble_decoded_frame(
        self,
        index: int,
        info: ToposFrameInfo,
        planes: "list[np.ndarray]",
        pts: int,
        pool_entry: "Optional[_ToposPlaneEntry]" = None,
    ) -> DecodedFrame:
        """公共装配尾部：ndarray 平面列表 → DecodedFrame（M2 两路径共用）。

        P1-13：alpha 移位优先写入 pool_entry 的同生命周期 scratch（零新
        分配）；无池（legacy 路径）回退表达式分配。"""
        assert self._summary is not None and self._metadata is not None
        vw, vh = int(info.visible_width), int(info.visible_height)
        plane_geoms = _plane_geoms(int(info.pixel_format),
                                   int(info.plane_count), vw, vh)
        for i in range(info.plane_count):
            w, h = plane_geoms[i]
            if planes[i].shape != (h, w):
                raise MediaDecodeError(
                    f"Topos 平面 {i} 形状 {planes[i].shape} != ({h}, {w})",
                    source_path=self._path,
                )

        s = self._summary
        # spec §8.6：码流内 alpha 是 16-bit 满刻度容器，alpha_bit_depth 只是
        # 顶层 N-bit 预量化精度；应用层平面语义统一为"声明位深满刻度"
        #（GPU 上传按 plane_infos[3].bit_depth 归一化——阶段 7 契约）。
        # concealment 中性值 65535 右移后 = 满刻度不透明，语义保持。
        # M4-R9：pf=3 同为 4 平面但 alpha_mode 恒 0（TRAW no-alpha）——
        # 不得进入 alpha 移位分支（否则相位平面 3 被错误右移）。
        if (info.plane_count == 4 and s.alpha_mode != 0
                and s.alpha_bit_depth < 16):
            shift = 16 - s.alpha_bit_depth
            scratch = None
            if pool_entry is not None:
                scratch = pool_entry.alpha_scratch_for(planes[3])
            if scratch is not None:
                # 移位写入池内 scratch（临时恢复可写；交付时只读契约不变）
                scratch.flags.writeable = True
                np.right_shift(planes[3], shift, out=scratch)
                scratch.flags.writeable = False
                shifted_alpha = scratch
            else:
                shifted_alpha = planes[3] >> shift
            # AUD-35900：不得原地改写调用方的 planes 列表。单帧路径直接
            # 传入 entry.arrays——原地替换会使 entry.arrays[3] 丢失原始
            # alpha 平面的最后引用（被释放），而 entry.views[3] 仍持有其
            # 裸地址，池条目下次复用时原生解码写入已释放内存（UAF）。
            # 在局部副本上替换，entry.arrays 与 entry.views 保持绑定。
            planes = [*planes[:3], shifted_alpha]
        if int(info.pixel_format) == 3:
            # M4-R9：TRAW 相位平面位深 = 主位深（无 alpha 域、无 16-bit 容器
            # 预量化语义——码值即声明位深满刻度）
            bit_depths = [s.bit_depth] * 4
        else:
            bit_depths = [s.bit_depth] * 3
            if info.plane_count == 4:
                bit_depths.append(s.alpha_bit_depth)

        # legacy fallback：旧 dylib/请求符号不可用时，保留原有完整帧后
        # box 缩放语义。native request 路径的 planes 已经是目标几何，禁止
        # 再次缩放或创建完整分辨率中间帧。
        out_geoms = plane_geoms
        if self._target_size is not None and not self._native_target_decode:
            tw, th = self._target_size
            out_geoms = _plane_geoms(int(info.pixel_format),
                                     int(info.plane_count), tw, th)
            planes = [
                _downscale_plane(p, oh, ow)
                for p, (ow, oh) in zip(planes, out_geoms)
            ]

        # 复验 P1-09：GBR 流平面序 = (G,B,R[,A])——组件标签必须如实标注，
        # 下游按 'Y/U/V' 标签或 planar_yuv 语义分派会把色差当 luma
        # M4-R9：TRAW CFA 相位平面序 = (R, Gr, Gb, B)（拼接序与编码器/
        # 图片线同契约）
        if int(info.pixel_format) == 3:
            components = ('R', 'Gr', 'Gb', 'B')[: info.plane_count]
        elif int(info.pixel_format) == 2:
            components = ('G', 'B', 'R', 'A')[: info.plane_count]
        else:
            components = ('Y', 'U', 'V', 'A')[: info.plane_count]
        plane_infos = tuple(
            PlaneInfo.create(
                index=i,
                width=w,
                height=h,
                component=c,
                dtype=np.dtype(np.uint16),
                bit_depth=bit_depths[i],
                stride=w * 2,  # 应用层 stride 语义 = 行字节数（tight）
            )
            for i, (c, (w, h)) in enumerate(zip(components, out_geoms))
        )

        extra = {
            'topos_concealed_slices': int(info.concealed_slices),
            'topos_slice_count': int(info.slice_count),
            'topos_alpha_premultiplied': bool(s.alpha_premultiplied),
            'topos_color_metadata_authority': (
                self._color_semantics.display_authority.value
                if self._color_semantics is not None else 'cicp'
            ),
            'topos_chroma_siting': int(s.chroma_siting),
        }
        if self._alpha_budget is not None:
            # R3：文件级 alpha 预算记录（tpcB；共享 dict 引用，零拷贝）
            extra['topos_alpha_budget'] = self._alpha_budget
        if self._trwm is not None:
            # RC1/RC4：as-shot 基线随帧透传（消费方据此初始化 RAW 参数）
            extra['topos_trwm'] = self._trwm
        # M4-R9：CFA 帧 width/height = 全幅语义（相位平面 0 是半几何，
        # 不得作为帧几何——编码器/渲染器按 ceil(W/2) 自行推导平面）。
        # 奇数全幅下 ×2 会失真，直接取摘要/目标尺寸。
        if int(info.pixel_format) == 3:
            if self._target_size is not None and not self._native_target_decode:
                frame_w, frame_h = self._target_size
            else:
                frame_w, frame_h = int(s.width), int(s.height)
        else:
            frame_w, frame_h = out_geoms[0][0], out_geoms[0][1]
        return DecodedFrame(
            frame_index=index,
            pts=pts,
            time_seconds=pts / s.timescale,
            data=None,
            planes=tuple(planes),
            pixel_format=_pixel_format_name(
                0 if info.plane_count < 4 else info.alpha_mode,
                s.alpha_bit_depth,
                s.bit_depth,
                s.pixel_format,
            ),
            dtype=np.dtype(np.uint16),
            width=frame_w,
            height=frame_h,
            metadata=self._metadata,
            plane_infos=plane_infos,
            is_proxy=self._target_size is not None,
            extra=extra,
        )


# ============================================================================
# 8-bit 显示路径助手（缩略图 / Source Viewer 回退等）
# ============================================================================

def _topos_rgb_display_encode(rgb: np.ndarray, transfer: str) -> np.ndarray:
    """G3：源 transfer → 显示域（近似 SDR）。PQ 按 203-nit 参白归一（高光
    截断、无 tone mapping——缩略图/海报帧可接受口径），HLG 按 0.75=漫射白
    归一；bt709/gamma/sRGB 等显示码原样通过。"""
    t = str(transfer or "").lower()
    if t in ("smpte2084", "pq"):
        from .color_decode import ColorConverter

        conv = ColorConverter()
        linear = conv.pq_to_linear(np.asarray(rgb, dtype=np.float32))
        return np.clip(linear / 203.0, 0.0, 1.0)
    if t in ("arib-std-b67", "hlg"):
        from .color_decode import ColorConverter

        conv = ColorConverter()
        linear = conv.hlg_to_linear(np.asarray(rgb, dtype=np.float32))
        # hlg_to_linear(0.75) ≈ 3.179 = 漫射白 → SDR 1.0
        return np.clip(linear / 3.179, 0.0, 1.0)
    return rgb


def topos_frame_to_rgb(frame) -> Optional[np.ndarray]:
    """把 Topos planar 帧转成 RGB uint8 (H, W, 3)；失败返回 None。

    仅供缩略图/源监视器这类 8-bit 显示路径使用。时间线播放必须走
    planar GPU 上传（yuv_gpu_upload），不要经此函数制造 CPU RGB 往返。
    G3：GBR（pf=2）平面是 (G,B,R) 恒等序直通（不套 YUV 矩阵——此前
    海报帧/胶片条颜色静默错误）；PQ/HLG 源做显示变换（此前直接 clip）；
    CFA（pf=3）经 W4 CPU debayer（2026-09-21 复查接线——此前"落地前
    不乱渲"的回退已过期，TRAW MOV 一直没有海报帧/胶片条）。
    """
    try:
        planes = getattr(frame, "planes", None)
        if not planes or len(planes) < 3:
            return None
        infos = getattr(frame, "plane_infos", None) or ()
        bit_depth = int(getattr(infos[0], "bit_depth", 10) or 10)
        md = getattr(frame, "metadata", None)
        pf_name = str(getattr(frame, "pixel_format", "") or "").lower()

        # —— TRAW CFA 相位平面：CPU debayer（as-shot TRWM 基线）——
        if "cfa" in pf_name:
            if len(planes) != 4:
                return None
            from src.shared.video.camera_gamut_matrices import (
                resolve_debayer_params)
            from src.shared.video.cfa_cpu_debayer import debayer_cfa_planes

            cfa_bd = bit_depth if bit_depth in (12, 16) else (
                16 if "cfa16" in pf_name else 12)
            trwm = (getattr(frame, "extra", None) or {}).get(
                "topos_trwm") or {}
            rp = resolve_debayer_params(None, trwm)
            rgb = debayer_cfa_planes(
                planes,
                int(getattr(frame, "width", 0) or 0) or planes[0].shape[1] * 2,
                int(getattr(frame, "height", 0) or 0) or planes[0].shape[0] * 2,
                bit_depth=cfa_bd,
                wb_gains=rp["wb_gains"],
                exposure_stops=rp["exposure_stops"],
                black_level=rp["black_level"],
                gamma_mode=rp["gamma_mode"],
                gamut_matrix=rp["matrix"],
            )
            return (np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8)

        # —— G3-a：GBR 恒等序直通 ——
        if "gbr" in pf_name:
            full = float((1 << bit_depth) - 1)
            rgb = np.stack([
                np.asarray(planes[2], dtype=np.float32),   # R
                np.asarray(planes[0], dtype=np.float32),   # G
                np.asarray(planes[1], dtype=np.float32),   # B
            ], axis=-1) / full
            rgb = _topos_rgb_display_encode(
                rgb, str(getattr(md, "color_transfer", "") or ""))
            return (np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8)

        from src.shared.color.types import ColorMatrix, ColorRange
        from .color_decode import ColorConverter

        matrix_name = str(
            getattr(md, "color_matrix", "") or "bt709"
        ).lower()
        matrix_map = {
            "bt709": ColorMatrix.BT709,
            "bt2020nc": ColorMatrix.BT2020_NC,
            "bt2020nc-const": ColorMatrix.BT2020_NC,
            "bt2020": ColorMatrix.BT2020_NC,
            "smpte170m": ColorMatrix.BT601,
            "bt470bg": ColorMatrix.BT601_PAL,
        }
        md_range = getattr(md, "color_range", "limited") or "limited"
        range_ = ColorRange.FULL if str(md_range).lower() == "full" \
            else ColorRange.LIMITED

        matrix_enum = matrix_map.get(matrix_name)
        if matrix_enum is None:
            # W1 姿态（2026-09-21 复查补）：未知 matrix 此前静默按 BT.709
            # （filmstrip/海报帧颜色静默偏移）——告警一次后仍按 709 近似
            # （显示路径，不值得硬失败）。
            logger.warning(
                "topos_frame_to_rgb：未知 color_matrix %r，缩略图按 BT.709 "
                "近似", matrix_name)
            matrix_enum = ColorMatrix.BT709
        rgb = ColorConverter().yuv_to_rgb(
            np.asarray(planes[0]), np.asarray(planes[1]), np.asarray(planes[2]),
            matrix_enum, range_, bit_depth,
        )
        # G3-b：PQ/HLG 源做显示变换（此前直接 clip——高光/中灰静默错色）
        rgb = _topos_rgb_display_encode(
            rgb, str(getattr(md, "color_transfer", "") or ""))
        return (np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8)
    except Exception:  # noqa: BLE001 - 显示回退路径，任何失败都回落调用方占位
        logger.exception("topos_frame_to_rgb 转换失败")
        return None


def read_topos_frame_rgb(path: str, frame_index: int,
                         target_size: "tuple[int, int] | None" = None
                         ) -> Optional[np.ndarray]:
    """一次性打开→读帧→转 RGB（每次调用独立 open/close，适合低频单帧）。

    G8：``target_size`` 透传 native 缩放解码（AUTO_2K/reduced）——海报帧
    这类 ≤1024px 的低频单帧不再做全帧解码 + 事后 resize（4K/8K 源海报帧
    耗时数倍下降）；仅缩小语义，放大请求回退原始分辨率（source 内部处理）。
    """
    try:
        source = ToposMediaSource(path, target_size=target_size)
        source.open()
    except Exception:  # noqa: BLE001 - 文件损坏/库缺失时让调用方回落占位图
        logger.warning("read_topos_frame_rgb: 打开失败 %s", path)
        return None
    try:
        total = int(source.probe().total_frames or 0)
        idx = max(0, min(frame_index, total - 1)) if total > 0 else frame_index
        return topos_frame_to_rgb(source.read_frame(idx))
    except Exception:  # noqa: BLE001
        logger.warning("read_topos_frame_rgb: 读帧失败 %s#%d", path, frame_index)
        return None
    finally:
        try:
            source.close()
        except Exception:  # noqa: BLE001
            pass
