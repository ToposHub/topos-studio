"""B11 · 录音时钟、Punch、多声道采集与中断恢复核心（主计划 §20/B11）。

数据与算法（对应实施步骤 1/2/4/6 与手册验证条款）：

- ``CaptureBlock``：采集 backend 保证帧顺序的块记录；``CaptureStream``
  以严格累计设备样本索引核对连续性（重复/缺块 → discontinuity 标记，
  不悄悄丢样本宣称完整 take）。
- ``PunchRange``：半开样本区间（不能按播放帧取整裁切录音）；
  ``punch_slice`` 按整数样本在块边界精确切片（手册算法 oracle）。
- ``ClockAnchor``：设备 sample clock ↔ timeline 有理时钟建锚；成对锚
  最小二乘估计漂移比例（如 480048/480000），``timeline_sample_for_device``
  连续映射、锚误差 ≤1 reference sample（±100ppm 漂移 fixture 校验）。
  系统单调时钟只作观测（host_monotonic_ns），不驱动定位。
- ``CaptureJournal``：恢复日志——每段记录已落盘样本数/CRC/来源；
  ``recoverable_prefix`` 返回连续已确认前缀（磁盘满/掉线后恢复成
  partial take，不虚报完整）。写入走追加+flush+fsync 节流。

坐标：``device_sample`` 为设备时钟绝对样本；``timeline_sample`` 为工程
参考率绝对样本。多声道：每目标轨按 index map 取通道（v2 patch 契约）。
"""
from __future__ import annotations

import logging
import os
import struct
import time
import zlib
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

RECORDING_SESSION_PLAN_VERSION = 2


@dataclass(frozen=True)
class CaptureBlock:
    """一个采集块（backend 保证帧顺序；通道按设备原始布局）。"""

    device_session_id: str
    sequence_no: int
    first_device_sample: int
    host_monotonic_ns: int
    pcm: np.ndarray            # (channels, n) float32
    discontinuity: bool = False

    @property
    def sample_count(self) -> int:
        return int(self.pcm.shape[-1])


class CaptureStream:
    """块序列核对器：累计设备样本索引，识别重复/缺块（§20/B11 步骤 1）。"""

    def __init__(self, device_session_id: str,
                 expected_channels: Optional[int] = None) -> None:
        self._session = device_session_id
        self._expected_channels = expected_channels
        self._next_expected: Optional[int] = None
        self._sequence_no = 0
        self.total_samples = 0
        self.gaps: List[Tuple[int, int]] = []       # (expected, got) 缺块
        self.duplicates: List[Tuple[int, int]] = [] # (expected, got) 重复
        self.discontinuities = 0

    def accept(self, pcm: np.ndarray,
               first_device_sample: Optional[int] = None,
               host_monotonic_ns: Optional[int] = None) -> CaptureBlock:
        """接收一个块；first_device_sample 缺失时按累计推进（backend
        必须保证有序——乱序属于 backend 合同违约，此处按 discontinuity 记录）。"""
        pcm = np.asarray(pcm)
        if pcm.ndim != 2 or pcm.dtype != np.float32:
            raise ValueError("capture pcm must be (channels, n) float32")
        if (self._expected_channels is not None
                and pcm.shape[0] != self._expected_channels):
            raise ValueError(
                f"capture channels {pcm.shape[0]} != expected "
                f"{self._expected_channels}")
        n = int(pcm.shape[-1])
        if n <= 0:
            raise ValueError("empty capture block")
        if first_device_sample is None:
            first_device_sample = (self._next_expected
                                   if self._next_expected is not None else 0)
        discontinuity = False
        if self._next_expected is not None:
            if first_device_sample > self._next_expected:
                self.gaps.append((self._next_expected, first_device_sample))
                discontinuity = True
            elif first_device_sample < self._next_expected:
                self.duplicates.append((self._next_expected,
                                        first_device_sample))
                discontinuity = True
        if discontinuity:
            self.discontinuities += 1
        block = CaptureBlock(
            device_session_id=self._session,
            sequence_no=self._sequence_no,
            first_device_sample=int(first_device_sample),
            host_monotonic_ns=(int(host_monotonic_ns)
                               if host_monotonic_ns is not None
                               else time.monotonic_ns()),
            pcm=pcm, discontinuity=discontinuity,
        )
        self._sequence_no += 1
        self._next_expected = first_device_sample + n
        self.total_samples += n
        return block

    @property
    def is_continuous(self) -> bool:
        return self.discontinuities == 0


# ─────────────────────────────────────────────────────────────────────────────
# Punch（半开区间；块边界整数样本切片）
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PunchRange:
    """Punch 有效使用窗 [start, end)（设备样本坐标；半开区间）。"""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("empty punch range")


def punch_slice(block: np.ndarray, first: int, start: int,
                end: int) -> np.ndarray:
    """从块 ``[first, first+n)`` 中切出 punch ``[start, end)`` 的部分。

    手册算法（§20/B11）：进入/退出边界按整数样本切片；块与 punch 不相交
    返回空。多块拼接后 recorded == arange(start, end)。
    """
    if end <= start:
        raise ValueError("empty punch")
    lo, hi = max(first, start), min(first + block.shape[-1], end)
    if hi <= lo:
        return block[:, :0]
    return block[:, lo - first:hi - first]


def apply_punch(blocks: List[Tuple[int, np.ndarray]], start: int,
                end: int) -> np.ndarray:
    """Join an exact punch window, refusing missing/overlapping channel data.

    Packing discontinuous chunks back-to-back would shorten a failed take and
    hide a recording dropout. Callers must explicitly repair/mark partial takes
    rather than receive an apparently complete punch result.
    """
    if end <= start:
        raise ValueError("empty punch")
    chunks: List[np.ndarray] = []
    expected = start
    channels: Optional[int] = None
    for first, block in blocks:
        block = np.asarray(block)
        if block.ndim != 2:
            raise ValueError("punch pcm must be planar (channels, samples)")
        chunk = punch_slice(block, first, start, end)
        if not chunk.shape[-1]:
            continue
        lo = max(first, start)
        if lo != expected:
            raise ValueError("punch window has missing or overlapping samples")
        if channels is None:
            channels = block.shape[0]
        elif channels != block.shape[0]:
            raise ValueError("punch channel layout changed between blocks")
        chunks.append(chunk)
        expected += chunk.shape[-1]
    if expected != end:
        raise ValueError("punch window is incomplete")
    return np.concatenate(chunks, axis=1)



# ─────────────────────────────────────────────────────────────────────────────
# 时钟锚定与漂移
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AnchorPair:
    device_sample: int
    timeline_sample: int


class ClockAnchor:
    """设备时钟 ↔ 工程参考时钟锚（成对锚最小二乘漂移估计；步骤 2/3）。"""

    def __init__(self, reference_rate: int, device_rate: int) -> None:
        if reference_rate <= 0 or device_rate <= 0:
            raise ValueError("rates must be positive")
        self._reference_rate = int(reference_rate)
        self._device_rate = int(device_rate)
        self._pairs: List[AnchorPair] = []
        self._slope: Optional[float] = None
        self._intercept: Optional[float] = None

    def add_anchor(self, device_sample: int, timeline_sample: int) -> None:
        """追加成对锚（loopback 校准/ punched 起点等；≥2 对后可估漂移）。"""
        if self._pairs and (device_sample <= self._pairs[-1].device_sample
                            or timeline_sample <= self._pairs[-1].timeline_sample):
            raise ValueError("clock anchors must advance both clocks strictly")
        self._pairs.append(AnchorPair(int(device_sample), int(timeline_sample)))
        self._slope = None
        self._intercept = None

    def estimate_drift(self) -> float:
        """最小二乘斜率：每设备样本消耗的 reference 样本比例。

        例：设备走 480048 而参考走 480000 → ≈ 0.9999。样本 <2 时返回
        理想比例 reference_rate/device_rate（比例语义，非测量值）。
        """
        if len(self._pairs) < 2:
            return self._reference_rate / self._device_rate
        xs = np.array([p.device_sample for p in self._pairs], dtype=np.float64)
        ys = np.array([p.timeline_sample for p in self._pairs], dtype=np.float64)
        x_mean = float(xs.mean())
        y_mean = float(ys.mean())
        denom = float(((xs - x_mean) ** 2).sum())
        if denom <= 1e-9:
            self._slope = self._reference_rate / self._device_rate
            self._intercept = y_mean - self._slope * x_mean
            return self._slope
        slope = float(((xs - x_mean) * (ys - y_mean)).sum() / denom)
        # 合理性护栏：漂移超出 ±1000ppm 拒绝拟合（锚点污染/时钟异常）
        ideal = self._reference_rate / self._device_rate
        if abs(slope / ideal - 1.0) > 1.0e-3:
            raise ValueError(
                f"estimated drift {slope / ideal - 1.0:+.0%} exceeds sanity "
                "band (±1000ppm); re-anchor required")
        self._slope = slope
        self._intercept = y_mean - slope * x_mean
        return slope

    def timeline_sample_for_device(self, device_sample: int) -> int:
        """设备样本 → 参考时钟样本（有理取整；锚误差目标 ≤1 样本）。

        无可用拟合（首锚后未 estimate / 拟合被合理性护栏拒绝）时按
        **最新锚点 + 理想斜率**换算——锚点处精确，不虚构漂移，也不把
        已有锚点整体丢弃退回过原点恒等（单锚 5000→9000 时查 5000 不得
        回 5000）。无任何锚点时才按理想比率过原点（调用方负责锚定）。
        """
        if self._slope is not None:
            slope = Fraction(self._slope)
            intercept = (Fraction(self._intercept)
                         if self._intercept is not None else Fraction(0))
        elif self._pairs:
            ideal = Fraction(self._reference_rate, self._device_rate)
            latest = self._pairs[-1]
            slope = ideal
            intercept = (Fraction(int(latest.timeline_sample))
                         - ideal * int(latest.device_sample))
        else:
            slope = Fraction(self._reference_rate, self._device_rate)
            intercept = Fraction(0)
        value = slope * int(device_sample) + intercept
        # 四舍五入到整数（对称取整）
        if value < 0:
            return -int((-2 * value.numerator + value.denominator)
                        // (2 * value.denominator))
        return int((2 * value.numerator + value.denominator)
                   // (2 * value.denominator))


# ─────────────────────────────────────────────────────────────────────────────
# 恢复日志（§20/B11 步骤 6）
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class JournalSegment:
    sequence_no: int
    first_device_sample: int
    sample_count: int
    crc32: int
    fsynced: bool
    _journal_written: bool = field(default=False, repr=False, compare=False)


class CaptureJournal:
    """录音恢复日志：确认已落盘的连续段，崩溃后恢复连续有效前缀。

    写入模式：``record_segment``（内存）→ ``commit_segment``（追加
    JSONL + flush + 节流 fsync）。``recoverable_prefix`` = 从 sequence 0
    起连续已 fsync 的段总样本数——崩溃后以此恢复 partial take，断点保留
    诊断；已确认数之后的内容不虚报。
    """

    def __init__(self, journal_path: Path, *,
                 fsync_every: int = 8) -> None:
        self._path = Path(journal_path)
        self._fsync_every = max(1, int(fsync_every))
        self._segments: List[JournalSegment] = []
        self._pending_writes = 0
        self._fh = None
        self._confirmed_bytes = 0
        self._load_existing()

    @staticmethod
    def _read_journal(path: Path) -> Tuple[List[JournalSegment], int]:
        """Read durable checkpoints; never promote a merely readable v2 tail.

        Legacy unversioned JSONL records remain readable. Version 2 records
        become confirmed only when followed by their matching checkpoint.
        Returning the byte boundary lets a resumed writer remove crash debris
        before appending, so one torn line cannot hide all future recordings.
        """
        segments: List[JournalSegment] = []
        confirmed_bytes = 0
        for group, boundary in CaptureJournal._iter_confirmed_groups(path):
            segments.extend(group)
            confirmed_bytes = boundary
        return segments, confirmed_bytes

    @staticmethod
    def _iter_confirmed_groups(path: Path) -> Iterator[Tuple[Tuple[JournalSegment, ...], int]]:
        """Stream checkpoint groups with bounded parsing memory.

        Production writers checkpoint every eight blocks. Refuse pathological
        groups over 4096 records rather than allocating an unbounded recovery
        index; the previously confirmed prefix remains recoverable.
        """
        import json

        pending: List[JournalSegment] = []
        with open(path, "rb") as fh:
            while line := fh.readline(65536):
                if len(line) >= 65536:
                    break  # corrupt journal metadata cannot allocate an unbounded line
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    if not isinstance(rec, dict):
                        break
                    version = rec.get("journal_version", 1)
                    if isinstance(version, bool) or version not in (1, 2):
                        break
                    if version == 2 and "confirmed_segments" in rec:
                        count = rec["confirmed_segments"]
                        if isinstance(count, bool) or not isinstance(count, int) or count != len(pending) or count <= 0:
                            break
                        yield tuple(pending), fh.tell()
                        pending = []
                        continue
                    fields = (rec["sequence_no"], rec["first_device_sample"],
                              rec["sample_count"], rec.get("crc32", 0))
                    if any(isinstance(v, bool) or not isinstance(v, int) for v in fields):
                        break
                    seq, first, count, crc = fields
                    if seq < 0 or first < 0 or count <= 0 or not 0 <= crc <= 0xFFFFFFFF:
                        break
                except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                    break
                segment = JournalSegment(seq, first, count, crc, True)
                if version == 1:
                    if pending:
                        break
                    yield (segment,), fh.tell()
                else:
                    pending.append(segment)
                    if len(pending) > 4096:
                        break

    def _load_existing(self) -> None:
        if not self._path.exists():
            return
        try:
            segments, self._confirmed_bytes = self._read_journal(self._path)
            self._segments.extend(segments)
        except OSError:
            logger.warning("capture journal unreadable: %s", self._path,
                           exc_info=True)

    def record_segment(self, block: CaptureBlock) -> JournalSegment:
        """登记一个已写入临时 take 的段（CRC 按块 PCM 字节计算）。"""
        pcm_bytes = np.ascontiguousarray(block.pcm).tobytes()
        seg = JournalSegment(
            sequence_no=block.sequence_no,
            first_device_sample=block.first_device_sample,
            sample_count=block.sample_count,
            crc32=zlib.crc32(pcm_bytes) & 0xFFFFFFFF,
            fsynced=False,
        )
        self._segments.append(seg)
        return seg

    def commit_segment(self, sequence_no: int) -> bool:
        """把段标记为已落盘（追加日志；每 fsync_every 段 fsync 一次）。

        按 sequence_no **从最新向旧**匹配——日志复用（崩溃恢复后同一
        路径开新会话，sequence 从 0 重启）时，旧会话的同号段不得吞掉
        新会话的提交（否则新 take 永远写不进日志）。
        """
        import json

        seg = next((s for s in reversed(self._segments)
                    if s.sequence_no == sequence_no), None)
        if seg is None or seg.fsynced or seg._journal_written:
            return False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._fh is None:
            # A previous crash may leave a complete but unconfirmed record or
            # a torn line. Truncate only that tail, preserving the durable prefix.
            self._fh = open(self._path, "r+" if self._path.exists() else "w+",
                            encoding="utf-8")
            self._fh.seek(self._confirmed_bytes)
            self._fh.truncate()
        self._fh.write(json.dumps({
            "journal_version": 2,
            "sequence_no": seg.sequence_no,
            "first_device_sample": seg.first_device_sample,
            "sample_count": seg.sample_count,
            "crc32": seg.crc32,
        }) + "\n")
        seg._journal_written = True
        self._pending_writes += 1
        if self._pending_writes >= self._fsync_every:
            self._checkpoint()
        return True

    def _checkpoint(self) -> None:
        """Sync data records before publishing and syncing their commit marker."""
        import json

        if self._fh is None or self._pending_writes == 0:
            return
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.write(json.dumps({"journal_version": 2,
                                   "confirmed_segments": self._pending_writes}) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._confirmed_bytes = self._fh.tell()
        self._pending_writes = 0
        for recorded in self._segments:
            if recorded._journal_written:
                recorded.fsynced = True

    def close(self) -> None:
        """Flush and checkpoint every written segment when recording stops."""
        if self._fh is not None:
            self._checkpoint()
            self._fh.close()
            self._fh = None

    def recoverable_prefix(self) -> Tuple[int, int]:
        """(连续已 fsync 段数, 前缀总样本数)——恢复 partial take 的依据。"""
        confirmed: List[JournalSegment] = []
        if self._path.exists():
            try:
                confirmed, _ = self._read_journal(self._path)
            except OSError:
                logger.warning("capture journal unreadable: %s", self._path,
                               exc_info=True)
        total = 0
        expected_seq = 0
        expected_device: Optional[int] = None
        for seg in confirmed:
            if seg.sequence_no == 0 and expected_seq > 0:
                # sequence 重置 = 新会话开始（日志文件跨会话追加）——恢复
                # 依据切换到最新会话，不把旧会话的段计入新 take 的前缀。
                total = 0
                expected_seq = 0
                expected_device = None
            if seg.sequence_no != expected_seq:
                break
            if expected_device is not None and seg.first_device_sample != expected_device:
                break
            expected_device = seg.first_device_sample + seg.sample_count
            total += seg.sample_count
            expected_seq += 1
        return expected_seq, total

    def segments(self) -> Tuple[JournalSegment, ...]:
        return tuple(self._segments)


__all__ = [
    "AnchorPair",
    "CaptureBlock",
    "CaptureJournal",
    "CaptureStream",
    "ClockAnchor",
    "JournalSegment",
    "PunchRange",
    "RECORDING_SESSION_PLAN_VERSION",
    "apply_punch",
    "punch_slice",
]
