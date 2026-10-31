"""ToposImageSequence —— .toos 图片序列解析与有界预取源（Topos Image 阶段 6）。

目标（计划书阶段 6）：稳定读取 `frame_0001.toos` 一类序列，不另建无界线程
系统。资源有界：
- 预取单后台线程 + generation cancel（seek/换向即失效旧代，旧结果不覆盖新帧）；
- 解码平面缓存字节级上限（LRU 淘汰）；
- encoded packet 不缓存（单 intra 帧解码已足够快，缓存平面即可）；
- 帧级并行=1（预取线程）× slice 级并行（codec 内部预算）——不过度订阅 CPU。

帧号语法（冻结）：`<prefix><frame><suffix>`，frame 为十进制（支持负号），
padding 可选（`0001`=4 位补零）。枚举顺序按数值帧号升序，start/end/step 由
调用方过滤；padding 只影响文件名匹配，不影响帧号数值。
"""
from __future__ import annotations

import bisect
import logging
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

import numpy as np

from ..codec.topos_image_binding import (
    TC_IMG_SAMPLE_KIND_HALF,
    get_image_codec,
)
from .topos_image_source import ToposImageSource, _COMPONENTS, _pixel_format_name
from .frame import DecodedFrame, PlaneInfo
from .metadata import Rational
from .source import MediaSourceError

logger = logging.getLogger(__name__)


class SequencePatternError(ValueError):
    """序列模式无法解析。"""


@dataclass(frozen=True)
class FrameSlot:
    """单个文件槽位：数值帧号 + 磁盘路径。"""

    frame: int
    path: str
    size: int
    mtime_ns: int


@dataclass
class MissingFramePolicy:
    """缺帧策略（冻结：缺帧不伪造成功）。

    - 'skip'：读取缺失帧号时跳到最近的后继有效帧（无后继则报错）；
    - 'error'：打开时检测 [首帧, 末帧] 区间空洞并报错；读取缺失帧号亦报错；
    - 'hold'：保留（复用）最近的前驱帧；连续空洞超过 max_consecutive_missing
      时不再 hold（防止用远帧冒充）并报错。
    """

    mode: str = "skip"
    max_consecutive_missing: int = 8

    def __post_init__(self) -> None:
        if self.mode not in ("skip", "error", "hold"):
            raise SequencePatternError(
                f"缺帧策略必须是 skip/error/hold（得到 {self.mode!r}）")
        if self.max_consecutive_missing < 1:
            raise SequencePatternError("max_consecutive_missing 必须 ≥ 1")


def parse_frame_pattern(path: str) -> Optional[tuple[str, int, str]]:
    """解析单一路径样例为 (prefix, padding, suffix)。

    padding: 0=无补零；否则为补零位数。非序列名（无数字帧号）返回 None。
    确定性：纯正则，无 IO。
    """
    base = os.path.basename(path)
    m = re.fullmatch(r"^(.*?)(-?\d+)(\.[A-Za-z0-9]+)$", base)
    if m is None:
        return None
    prefix, frame, suffix = m.group(1), m.group(2), m.group(3)
    padding = 0
    if frame.startswith("-"):
        digits = frame[1:]
        padding = len(digits) if digits and digits[0] == "0" else 0
    else:
        padding = len(frame) if frame[0] == "0" else 0
    return prefix, padding, suffix


def frame_file_name(prefix: str, padding: int, suffix: str, frame: int) -> str:
    """帧号 → 文件名（与 parse_frame_pattern 互逆；负帧号补零作用于数字位）。"""
    if padding > 0:
        if frame < 0:
            return f"{prefix}-{abs(frame):0{padding}d}{suffix}"
        return f"{prefix}{frame:0{padding}d}{suffix}"
    return f"{prefix}{frame}{suffix}"


def scan_sequence(directory: str, prefix: str, padding: int, suffix: str,
                  start: Optional[int] = None, end: Optional[int] = None,
                  step: int = 1) -> list[FrameSlot]:
    """扫描目录，返回按帧号升序的槽位列表（含 stat 指纹）。

    空洞（文件不存在的帧号）不进入列表；step 必须 ≥ 1；负帧号支持。
    padding > 0 时按严格位宽匹配（`frame_1.toos` 不混入 `frame_0001.toos`
    的 4 位序列——变宽命名属不同序列，不静默合并）；padding = 0 接受任意
    位宽（帧号数值为准）。

    step 锚点语义（冻结）：`(frame - anchor) % step == 0` 才入选，anchor =
    显式 start；start=None 时锚定绝对帧号 0（不是"扫描到的最小帧号"——
    过滤结果不得依赖目录里恰好存在哪些文件，负帧号序列同样确定）。
    """
    if step < 1:
        raise SequencePatternError(f"step 必须 ≥ 1（得到 {step}）")
    slots: list[FrameSlot] = []
    lo = start if start is not None else -(1 << 31)
    hi = end if end is not None else (1 << 31) - 1
    with os.scandir(directory) as it:
        names = {e.name: e.stat() for e in it if e.is_file()}
    for name, st in names.items():
        m = re.fullmatch(rf"^{re.escape(prefix)}(-?\d+){re.escape(suffix)}$", name)
        if m is None:
            continue
        frame_str = m.group(1)
        if padding > 0:
            digits = frame_str[1:] if frame_str.startswith("-") else frame_str
            if len(digits) != padding:
                continue
        frame = int(frame_str)
        if frame < lo or frame > hi or (frame - (start or 0)) % step != 0:
            continue
        slots.append(FrameSlot(frame=frame, path=os.path.join(directory, name),
                               size=st.st_size, mtime_ns=st.st_mtime_ns))
    slots.sort(key=lambda s: s.frame)
    return slots


@dataclass
class SequenceOptions:
    """序列源配置（有界性参数显式化）。"""

    prefetch_depth: int = 4            # 预取窗口（帧）；0 = 关闭
    plane_cache_bytes: int = 256 << 20  # 解码平面缓存字节上限（LRU）
    missing: MissingFramePolicy = field(default_factory=MissingFramePolicy)
    slice_threads: Optional[int] = None  # 透传 codec（None=继承进程默认）
    target_size: Optional[tuple[int, int]] = None  # 播放预览包围盒
    fps: Rational = field(default_factory=lambda: Rational(24, 1))


class PlaneCache:
    """解码平面 LRU 缓存（字节记账）。

    key = (path, size, mtime_ns, decode_token)——stat 指纹（文件被替换即失效）
    + 解码器构建身份（库重构建后旧缓存不与新构建混用；计划书阶段 6 的
    codec/file version 维度）。value = 平面数组列表（置为只读，契约与视频链
    解码帧一致：消费方不得就地改写）。容量按字节数硬上限，超限淘汰最旧。
    """

    def __init__(self, max_bytes: int) -> None:
        self._max = max_bytes
        self._map: OrderedDict[tuple, list[np.ndarray]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key: tuple) -> Optional[list[np.ndarray]]:
        with self._lock:
            planes = self._map.get(key)
            if planes is not None:
                self._map.move_to_end(key)
            return planes

    def put(self, key: tuple, planes: list[np.ndarray]) -> None:
        # AUD-35811：记账与冻结循环统一契约——None 平面一律跳过。原实现
        # 字节记账未防护 None（p.nbytes 先行崩溃），而下方冻结循环却防护
        # 了，两处假设不一致。
        planes = [p for p in planes if p is not None]
        nbytes = sum(p.nbytes for p in planes)
        if nbytes > self._max:
            return  # 单帧超预算：不缓存（不无界增长）
        for p in planes:
            p.flags.writeable = False
        with self._lock:
            # 同 key 重复 put（前台与预取并发解码同一帧的常态路径）：
            # 覆盖前先扣除旧值，否则 _bytes 单向虚高 → 缓存被过早淘汰
            old = self._map.get(key)
            if old is not None:
                self._bytes -= sum(p.nbytes for p in old)
            self._map[key] = planes
            self._map.move_to_end(key)
            self._bytes += nbytes
            while self._bytes > self._max and self._map:
                _, evicted = self._map.popitem(last=False)
                self._bytes -= sum(p.nbytes for p in evicted)

    def clear(self) -> None:
        with self._lock:
            self._map.clear()
            self._bytes = 0


class ToposImageSequenceSource:
    """`.toos` 序列源：枚举 + 有界预取 + generation cancel。

实现统一 MediaSource 协议，并额外提供序列扫描与有界预取：
    - frames()：槽位枚举；
    - read_slot(frame)：同步解码（缓存命中零解码）；
    - prefetch_around(frame)：后台有界预取（generation cancel）。
    """

    def __init__(self, slots: list[FrameSlot], options: Optional[SequenceOptions] = None,
                 codec=None) -> None:
        if not slots:
            raise SequencePatternError("序列为空")
        # 重复帧号显式拒绝（不静默保留最后者——计划书要求定义重复策略）
        by_frame: dict[int, FrameSlot] = {}
        for s in slots:
            dup = by_frame.get(s.frame)
            if dup is not None:
                raise SequencePatternError(
                    f"重复帧号 {s.frame}: {dup.path} 与 {s.path}")
            by_frame[s.frame] = s
        self._slots = sorted(slots, key=lambda s: s.frame)
        self._by_frame = by_frame
        self._frames_sorted = sorted(by_frame)
        self._options = options or SequenceOptions()
        # 'error' 模式：打开时检测 [首帧, 末帧] 区间空洞并报错（不伪造成功）
        if self._options.missing.mode == "error":
            holes = self._detect_holes()
            if holes:
                raise SequencePatternError(
                    f"序列存在空洞（缺帧策略 error）: 缺失 {holes[:8]}"
                    f"{'…' if len(holes) > 8 else ''}（共 {len(holes)} 帧）")
        self._cache = PlaneCache(self._options.plane_cache_bytes)
        codec_obj = codec or get_image_codec()
        self._codec = codec_obj
        # 缓存 key 的解码器身份维度（库重构建后旧平面不与新构建混用）
        # 缓存键必须随解码布局变化：同一 bitstream 在 full、scaled 或
        # fixed-ratio reduced 预览下的平面几何/像素语义不同，不能互相命中。
        # 批 4：native preview 缩放解码已承载 pf=3（dec_frame_build_jobs
        # CFA 目标半维 + plane_geometry 按目标帧语义），CFA 序列与视频同策略
        # 跟随 options.target_size（平面 = 目标帧各再减半）。
        effective_target = self._options.target_size
        self._decode_token = (
            codec_obj.library_identity(),
            "full" if effective_target is None else
            ("preview", tuple(int(v) for v in effective_target)),
        )
        self._single = ToposImageSource(slots[0].path, codec=codec)
        self._single.open()
        base_meta = self._single.probe()
        # C07：首帧 native 头信息作为逐帧一致性锚（几何/像素/样本域/位深/
        # alpha/色彩）；混合格式序列在读取时明确拒绝。
        self._base_image_info = self._codec.probe(slots[0].path)
        output_width, output_height = self._output_dimensions(
            base_meta.width, base_meta.height)
        self._meta = replace(
            base_meta,
            width=output_width,
            height=output_height,
            fps=self._options.fps,
            total_frames=len(self._slots),
            codec_name="topos-image-sequence",
        )
        self._single.close()
        # generation：预取任务代号；seek/换向 +1，旧代结果全部丢弃
        self._generation = 0
        self._closed = False
        # pending 携带发起时的代号：(gen, frame, direction)——worker 采用
        # pending 自带的 gen，而不是自己出生时的 gen（否则新一代必杀旧
        # worker，"自取 pending"不可达，见 prefetch_around 注释）
        self._pending: Optional[tuple[int, int, int]] = None
        self._worker_done = False
        self._prefetch_thread: Optional[threading.Thread] = None
        self._prefetch_lock = threading.Lock()
        self._open = True
        self._current_frame = 0

    def _output_dimensions(self, width: int, height: int) -> tuple[int, int]:
        target = self._options.target_size
        if target is None:
            return width, height
        scale = min(1.0, target[0] / max(width, 1), target[1] / max(height, 1))
        return max(1, int(width * scale + 0.5)), max(1, int(height * scale + 0.5))

    def _check_frame_consistency(self, info, slot) -> None:
        """C07（2026-09-27 检查计划）：逐帧与首帧的元数据一致性校验。

        `_meta` 只以首帧构建；混合格式序列（64×48 GBR 接 64×32 YUVA 等）
        曾产出自相矛盾的帧（平面高 32 / 元数据高 48 / 标签仍 GBR）。
        现几何、像素域、样本域、位深、alpha 与色彩标签任一不一致即明确
        拒绝——可变格式支持须先定义逐帧元数据契约。
        """
        base = self._base_image_info
        diffs: list[str] = []
        if (info.visible_width, info.visible_height) != \
                (base.visible_width, base.visible_height):
            diffs.append(
                f"尺寸 {info.visible_width}x{info.visible_height} != 首帧 "
                f"{base.visible_width}x{base.visible_height}")
        if info.pixel_format != base.pixel_format:
            diffs.append(f"像素域 {info.pixel_format} != 首帧 {base.pixel_format}")
        if info.bit_depth != base.bit_depth:
            diffs.append(f"位深 {info.bit_depth} != 首帧 {base.bit_depth}")
        if info.sample_kind != base.sample_kind:
            diffs.append(f"样本域 {info.sample_kind} != 首帧 {base.sample_kind}")
        if bool(info.has_alpha) != bool(base.has_alpha) or \
                info.alpha_bit_depth != base.alpha_bit_depth:
            diffs.append(
                f"alpha {info.has_alpha}/{info.alpha_bit_depth}bit != 首帧 "
                f"{base.has_alpha}/{base.alpha_bit_depth}bit")
        for attr in ("color_range", "color_primaries", "color_transfer",
                     "color_matrix"):
            if getattr(info, attr) != getattr(base, attr):
                diffs.append(f"{attr} {getattr(info, attr)} != 首帧 "
                             f"{getattr(base, attr)}")
        if diffs:
            raise MediaSourceError(
                f"图片序列第 {slot.frame} 帧与首帧元数据不一致："
                f"{'; '.join(diffs)}（混合格式序列不受支持）",
                self.path)

    def _detect_holes(self) -> list[int]:
        frames = self._frames_sorted
        holes: list[int] = []
        for a, b in zip(frames, frames[1:]):
            if b - a > 1:
                holes.extend(range(a + 1, b))
        return holes

    # —— 枚举 ——

    def frames(self) -> list[int]:
        return list(self._frames_sorted)

    def metadata(self):
        return self._meta

    # —— MediaSource / DecodeQueue 接线 ——

    def open(self) -> None:
        """恢复序列源的可读状态；文件句柄按帧短时打开。"""
        with self._prefetch_lock:
            self._closed = False
            self._open = True

    def probe(self):
        return self._meta

    def read_frame(self, frame_index: int) -> DecodedFrame:
        """按播放序号读取帧，并保留实际磁盘帧号用于诊断。"""
        if not self._open or self._closed:
            raise MediaSourceError("图片序列未打开", self.path)
        if frame_index < 0 or frame_index >= len(self._slots):
            raise MediaSourceError(
                f"图片序列帧号越界: {frame_index}（有效范围 0..{len(self._slots) - 1}）",
                self.path,
            )
        disk_frame = self._slots[frame_index].frame
        planes, slot = self.read_slot(disk_frame)
        self._current_frame = frame_index
        info = self._codec.probe(slot.path)
        self._check_frame_consistency(info, slot)
        # 帧尺寸 = 有效输出尺寸（target_size 缩放解码后 = _meta 维度；
        # 2026-09-13 修复：此前取 native probe 可见尺寸，缩放预览下与
        # 平面几何/元数据三处不一致——alpha 形状校验必挂）。CFA（pf=3）
        # 平面为 ceil(W/2)×ceil(H/2)，同样不能从平面 0 推帧宽高。
        is_cfa = info.pixel_format == 3
        # M1-F3：浮点样本域（HALF）→ float16 平面 + 'gbrph16le'
        floating = int(getattr(info, "sample_kind", 0)) == TC_IMG_SAMPLE_KIND_HALF
        plane_dtype = np.float16 if floating else np.uint16
        width = int(self._meta.width)
        height = int(self._meta.height)
        alpha_bd = info.alpha_bit_depth if info.has_alpha else 0
        components = _COMPONENTS.get(info.pixel_format, ("P0", "P1", "P2", "A"))
        bit_depths = [info.bit_depth] * (4 if is_cfa else 3)
        if info.has_alpha:
            bit_depths.append(info.alpha_bit_depth)
        plane_infos = tuple(
            PlaneInfo.create(
                index=i,
                width=int(plane.shape[1]),
                height=int(plane.shape[0]),
                component=components[i],
                dtype=plane_dtype,
                bit_depth=bit_depths[i],
            )
            for i, plane in enumerate(planes)
        )
        return DecodedFrame(
            frame_index=frame_index,
            pts=self._meta.fps.frame_to_pts(frame_index),
            time_seconds=self._meta.fps.frame_to_seconds(frame_index),
            planes=tuple(planes),
            pixel_format=_pixel_format_name(info.pixel_format, info.bit_depth, alpha_bd),
            dtype=np.dtype(plane_dtype),
            width=width,
            height=height,
            metadata=self._meta,
            plane_infos=plane_infos,
            extra={
                "source": "topos-image-sequence",
                "sequence_frame": slot.frame,
                "image_profile": info.image_profile,
                "color_primaries": info.color_primaries,
                "color_transfer": info.color_transfer,
                "color_matrix": info.color_matrix,
                "color_range": info.color_range,
                "alpha_mode": info.alpha_mode,
                "topos_alpha_premultiplied": bool(info.alpha_premultiplied),
                # TRAW（批 2）：CFA 相位序与曲线 id 供 debayer 消费
                # （transfer=20=TC_TRANSFER_TRAW_LOG0，PWL-log12 §3.2）
                "topos_cfa": bool(is_cfa),
                "cfa_pattern": "rggb" if is_cfa else None,
            },
        )

    def seek_frame(self, frame_index: int) -> None:
        if frame_index < 0 or frame_index >= len(self._slots):
            raise MediaSourceError(f"图片序列无帧 {frame_index}", self.path)
        self._current_frame = frame_index
        self.prefetch_around(self._slots[frame_index].frame)

    def read_audio_samples(self, start_pts: int, count: int):
        return None

    @property
    def is_open(self) -> bool:
        return self._open and not self._closed

    @property
    def current_frame(self) -> int:
        return self._current_frame

    @property
    def path(self) -> str:
        return self._slots[0].path

    def close(self) -> None:
        with self._prefetch_lock:
            self._generation += 1  # 使在飞预取失效
            self._closed = True
            self._open = False
        if self._prefetch_thread is not None:
            self._prefetch_thread.join(timeout=5)
        self._cache.clear()

    # —— 读取 ——

    def slot(self, frame: int) -> Optional[FrameSlot]:
        return self._by_frame.get(frame)

    def _fingerprint(self, slot: FrameSlot) -> tuple:
        return (slot.path, slot.size, slot.mtime_ns, self._decode_token)

    @staticmethod
    def _current_fingerprint(slot: FrameSlot, token: tuple) -> Optional[tuple]:
        """读取时的实时指纹（文件被替换/删除即刻反映；扫描态仅作回退）。"""
        try:
            st = os.stat(slot.path)
        except OSError:
            return None
        return (slot.path, st.st_size, st.st_mtime_ns, token)

    def read_slot(self, frame: int) -> tuple[list[np.ndarray], FrameSlot]:
        """同步读取并解码一帧（缓存命中则零解码；返回平面为只读）。

        缺帧按 MissingFramePolicy 处理：skip→后继帧 / error→报错 /
        hold→前驱帧（连续空洞超限报错）。文件被替换（size/mtime 变化）
        → 指纹变化 → 重新解码，不返回旧数据。
        """
        with self._prefetch_lock:
            gen = self._generation
        return self._read_slot(frame, gen)

    def _read_slot(self, frame: int, gen: int) -> tuple[list[np.ndarray], FrameSlot]:
        slot = self._by_frame.get(frame)
        if slot is None:
            mode = self._options.missing.mode
            if mode == "hold":
                nearest = self._nearest_before(frame)
                limit = self._options.missing.max_consecutive_missing
                if nearest is None or frame - nearest > limit:
                    raise FileNotFoundError(
                        f"序列缺帧 {frame} 且无可保留的前驱帧"
                        f"（连续空洞超过 {limit}）")
                slot = self._by_frame[nearest]
            elif mode == "skip":
                nxt = self._nearest_after(frame)
                if nxt is None:
                    raise FileNotFoundError(f"序列缺帧 {frame} 且无后继帧")
                return self._read_slot(nxt, gen)
            else:  # 'error'
                raise FileNotFoundError(f"序列缺帧: {frame}")
        key = self._current_fingerprint(slot, self._decode_token)
        if key is None:
            # 文件消失：hold 保留上一已解码帧（无则按缺帧报错）
            # AUD-35809：_nearest_before(f) 返回 <= f 的最近帧，f 自身在
            # 索引中时恒命中 f 自身——旧实现对 slot.frame 查询因此不可达。
            # 必须查 f-1 才能取到严格前驱；前驱文件也消失时继续回退，
            # 连续空洞超过 max_consecutive_missing 同样报错（与缺帧
            # hold 契约一致，防止用远帧冒充）。
            if self._options.missing.mode == "hold":
                limit = self._options.missing.max_consecutive_missing
                probe = slot.frame - 1
                while True:
                    nearest = self._nearest_before(probe)
                    if nearest is None or slot.frame - nearest > limit:
                        break
                    try:
                        return self._read_slot(nearest, gen)
                    except FileNotFoundError:
                        # 前驱文件同样不可读（正被替换/删除）——继续回退
                        probe = nearest - 1
            raise FileNotFoundError(f"文件不可读（可能正被替换/删除）: {slot.path}")
        # 实时指纹与扫描态不一致 → 刷新槽位（后续调用用新 stat）
        if key[:3] != self._fingerprint(slot)[:3]:
            new_slot = FrameSlot(frame=slot.frame, path=slot.path,
                                 size=key[1], mtime_ns=key[2])
            # AUD-35810：回写共享 _by_frame 必须持 _prefetch_lock——预取
            # 线程与前台可同时执行此回写，这是本文件唯一绕过锁纪律的共享
            # 可变状态写（CPython GIL 下 dict 赋值原子且两线程写入语义
            # 等价，但 PyPy/无 GIL 构建下是竞态）。
            with self._prefetch_lock:
                self._by_frame[new_slot.frame] = new_slot
            slot = new_slot
        cached = self._cache.get(key)
        if cached is not None:
            return cached, slot
        # Sequence reads already have the file geometry and a process-shared
        # codec.  Decode directly through the image binding instead of
        # constructing ToposImageSource/opening and probing the same file a
        # second time for every frame.  This is the image-sequence analogue of
        # the persistent video decoder path; metadata remains available from
        # ``self._meta`` and the returned planes keep the historical alpha
        # scale contract.
        # TRAW（pf=3/image_profile=3）：批 4 起 native preview 缩放解码承载
        # CFA（目标帧各平面再减半），GPU debayer 按实际平面尺寸渲染——
        # 预览链路解码量随 target_size 降低（播放提速）。
        # M1-F3：HALF 样本域（浮点档）走 decode_float——输出 float16 线性
        # 平面；UINT 文件仍走 decode()。probe 仅缓存未命中路径执行（≈17µs，
        # 相对解码可忽略）。
        slot_info = self._codec.probe(slot.path)
        # F05（2026-09-27）：binding 的 decode/decode_float 每次调用新分配
        # 平面（np.zeros per plane，零拷贝直写），调用方独占所有权——
        # 历史 [np.array(p, copy=True)] 再整平面复制一次纯属多余
        # （4K 444 uint16 三平面额外 ≈47.5 MiB/帧）。alpha 右移直接在
        # 独占平面原地完成；进入缓存/交付消费方的所有权语义不变。
        if int(slot_info.sample_kind) == TC_IMG_SAMPLE_KIND_HALF:
            planes, info, decode_extra = self._codec.decode_float(
                slot.path, target_size=self._options.target_size)
        else:
            planes, info, decode_extra = self._codec.decode(
                slot.path, target_size=self._options.target_size)
            planes = list(planes)
            if info.has_alpha and 0 < info.alpha_bit_depth < 16:
                planes[3] = planes[3] >> (16 - info.alpha_bit_depth)
        # N07（2026-09-27 复验）：alpha 转换完成后、入缓存决策之前统一
        # 冻结——read_slot 契约是"平面只读"；旧实现只在 PlaneCache.put
        # 接纳帧时冻结，容量 0 / 单帧超预算 / 代际失效不入缓存的路径
        # 返回可写平面，调用方可就地改写污染后续读取。
        for p in planes:
            if p is not None:
                p.flags.writeable = False
        # 缓存写入前验代：close() 或新一代（seek）之后的旧结果不落缓存
        with self._prefetch_lock:
            if gen == self._generation and not self._closed:
                self._cache.put(key, planes)
        return planes, slot

    def _nearest_before(self, frame: int) -> Optional[int]:
        i = bisect.bisect_right(self._frames_sorted, frame)
        return self._frames_sorted[i - 1] if i > 0 else None

    def _nearest_after(self, frame: int) -> Optional[int]:
        i = bisect.bisect_right(self._frames_sorted, frame)
        return self._frames_sorted[i] if i < len(self._frames_sorted) else None

    # —— 有界预取 ——

    def prefetch_around(self, frame: int, direction: int = 1) -> None:
        """围绕 frame 启动一代有界预取（后台单线程，代际取消）。

        direction=+1 顺播 / -1 反向；窗口 = prefetch_depth。
        新一代启动即取消旧代（generation cancel）；旧窗口的候选解码逐个
        检查代号、发现被取代立即弃窗，但 worker 本身不退出——回到循环顶
        自取最新挂起的 _pending（pending 携带新代代号），不丢请求、不叠加
        线程。worker 所有退出路径都在锁内置 _worker_done：只要本方法在锁
        内观察到 not worker_done，worker 必然还会再进锁读一次 _pending，
        因此本方法设置的 pending 一定会被取走（关闭旧版"挂起请求被静默
        丢弃"的竞态：旧实现 worker 按出生代号退出，pending 分支不可达）。
        """
        with self._prefetch_lock:
            self._generation += 1
            gen = self._generation
            depth = max(0, self._options.prefetch_depth)
            if depth == 0 or self._closed:
                return
            self._pending = (gen, frame, direction)
            t = self._prefetch_thread
            if t is not None and t.is_alive() and not self._worker_done:
                return  # 在飞 worker 弃旧窗后自取 pending（不叠加线程）

            def worker() -> None:
                try:
                    while True:
                        with self._prefetch_lock:
                            if self._closed:
                                self._worker_done = True
                                return
                            req = self._pending
                            self._pending = None
                            if req is None:
                                self._worker_done = True
                                return
                            gen, f0, direction0 = req
                        order: list[int] = []
                        f = f0 + direction0
                        while len(order) < depth and (f in self._by_frame):
                            order.append(f)
                            f += direction0
                        for candidate in order:
                            with self._prefetch_lock:
                                if gen != self._generation or self._closed:
                                    break  # 本窗被取代/关闭：弃窗回顶自取新 pending
                            try:
                                self._read_slot(candidate, gen)
                            except (FileNotFoundError, OSError):
                                if self._options.missing.mode == "error":
                                    break
                            except Exception:  # noqa: BLE001 —— 预取失败不影响前台
                                logger.debug("prefetch %s 帧失败", candidate,
                                             exc_info=True)

                finally:
                    with self._prefetch_lock:
                        self._worker_done = True

            self._worker_done = False
            self._prefetch_thread = threading.Thread(
                target=worker, name="toos-prefetch", daemon=True)
            self._prefetch_thread.start()


def open_sequence(pattern_path: str, options: Optional[SequenceOptions] = None,
                  start: Optional[int] = None, end: Optional[int] = None,
                  step: int = 1) -> ToposImageSequenceSource:
    """从样例路径打开序列（自动解析 prefix/padding/suffix 并扫描目录）。"""
    parsed = parse_frame_pattern(pattern_path)
    if parsed is None:
        raise SequencePatternError(f"不是可解析的序列模式: {pattern_path}")
    prefix, padding, suffix = parsed
    sample = Path(pattern_path)
    slots = scan_sequence(str(sample.parent), prefix, padding, suffix,
                          start=start, end=end, step=step)
    if not slots:
        raise FileNotFoundError(f"序列为空: {pattern_path}")
    return ToposImageSequenceSource(slots, options)
