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
from typing import List, Optional, Tuple

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
    """跨块拼接 punch 结果（按 first_device_sample 顺序传入）。"""
    chunks = [punch_slice(b, first, start, end) for first, b in blocks
              if b.size]
    if not chunks:
        return np.zeros((0, 0), dtype=np.float32)
    channels = chunks[0].shape[0]
    return np.concatenate(
        [c if c.shape[0] == channels else c[:channels] for c in chunks],
        axis=1)


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

        斜率拟合在追加锚后失效，直到再次 estimate_drift（断点重新建锚）。
        """
        slope = self._slope if self._slope is not None \
            else self._reference_rate / self._device_rate
        intercept = self._intercept if self._intercept is not None else 0.0
        value = Fraction(slope) * int(device_sample) + Fraction(intercept)
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
        self._load_existing()

    def _load_existing(self) -> None:
        if not self._path.exists():
            return
        import json

        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        break  # 半行（崩溃残留）→ 前缀有效
                    self._segments.append(JournalSegment(
                        sequence_no=int(rec["sequence_no"]),
                        first_device_sample=int(rec["first_device_sample"]),
                        sample_count=int(rec["sample_count"]),
                        crc32=int(rec.get("crc32", 0)),
                        fsynced=True,
                    ))
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
        """把段标记为已落盘（追加日志；每 fsync_every 段 fsync 一次）。"""
        import json

        seg = next((s for s in self._segments
                    if s.sequence_no == sequence_no), None)
        if seg is None or seg.fsynced:
            return False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._fh is None:
            self._fh = open(self._path, "a", encoding="utf-8")
        self._fh.write(json.dumps({
            "sequence_no": seg.sequence_no,
            "first_device_sample": seg.first_device_sample,
            "sample_count": seg.sample_count,
            "crc32": seg.crc32,
        }) + "\n")
        self._pending_writes += 1
        if self._pending_writes >= self._fsync_every:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._pending_writes = 0
        seg.fsynced = True
        return True

    def close(self) -> None:
        """收尾：flush + fsync 全部未落盘日志（会话结束/停录调用）。"""
        if self._fh is not None:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
            self._fh = None
            self._pending_writes = 0

    def recoverable_prefix(self) -> Tuple[int, int]:
        """(连续已 fsync 段数, 前缀总样本数)——恢复 partial take 的依据。"""
        import json

        # 以日志文件为准（崩溃后内存态不可信）
        confirmed: List[JournalSegment] = []
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            break
                        confirmed.append(JournalSegment(
                            sequence_no=int(rec["sequence_no"]),
                            first_device_sample=int(rec["first_device_sample"]),
                            sample_count=int(rec["sample_count"]),
                            crc32=int(rec.get("crc32", 0)),
                            fsynced=True,
                        ))
            except OSError:
                pass
        total = 0
        expected_seq = 0
        for seg in confirmed:
            if seg.sequence_no != expected_seq:
                break
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
