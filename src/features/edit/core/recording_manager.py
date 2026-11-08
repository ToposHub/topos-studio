"""
Recording Manager — 录音/ADR（AM-08 阶段 4.4）。

专业影视 VO/ADR 录音基础设施。基于 PyQt6 QAudioSource（输入设备抽象），
支持 pre-roll、take 命名、BWF/WAV 落盘、latency 校准、失败恢复。

设计原则（计划表 4.4 / P1-10）：
- 输入设备选择、input patch、pre-roll、take 命名、BWF/WAV 落盘。
- latency calibration：录音偏移校准后 ≤10ms（compute_time_reference
  把校准偏移写进 bext TimeReference，实现音视频对齐）。
- rolling pre-roll：倒计时期间持续采集，take 文件包含 pre-roll 音频。
- post-roll：设备在采时停止按钮先进入 stopping 继续采集尾巴再落盘
  （enable_live_post_roll 由控制器在事件循环可用时开启）。
- 失败恢复：设备拔出时 UI 安全恢复，不丢工程。
- Loop record：连续生成 takes（控制器在 take 完成回调后重启）。

线程模型
--------
- 录音在 QAudioSource 的内部线程进行，PCM 通过IODevice.read回读。
- 录音管理器在主线程协调（start/stop/take 管理），IO 读取在录音线程。
- 设备失败通过 QAudioSource.stateChanged 信号安全上报，不丢已录数据。

模块边界
--------
本模块只负责"录到 PCM 并落盘 + take 元数据"。录完的 take 作为新 asset
导入工程（由调用方 controller 处理 import），不直接修改 timeline。
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Iterator, List, Optional, Tuple

import numpy as np

if TYPE_CHECKING:  # pragma: no cover - annotation-only imports
    from PyQt6.QtMultimedia import QAudio

    from src.features.edit.core.bwf import BextMetadata

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 分块 PCM 缓冲（P0-07：长录音内存有界 + 无 O(n²) 拼接）
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_SPOOL_LIMIT_BYTES = 64 * 1024 * 1024  # 内存中最多保留 64MB PCM
_DISK_WARN_BYTES = 500 * 1024 * 1024           # 剩余磁盘 < 500MB 预警
_DISK_FAIL_BYTES = 100 * 1024 * 1024           # 剩余磁盘 < 100MB 安全终止


class DiskSpaceExhausted(Exception):
    """溢写磁盘空间耗尽（P0-07）：录音需安全终止并保留已录数据。"""


class _ChunkedPcmBuffer:
    """分块 planar PCM 缓冲。

    旧实现对每次设备回调执行 np.concatenate，长录音为 O(n²) 拷贝且内存
    峰值翻倍。本类把回调块追加到 chunk 列表（O(1) 摊销）；累计超过
    memory_limit_bytes 后，把最老的块转成交错 float32 顺序刷入临时
    裸 PCM 文件，内存占用保持有界。finalize 时经 iter_blocks() 流式
    读取（先磁盘后内存），全程不整体物化。
    """

    def __init__(
        self,
        channels: int,
        spool_dir: str = "",
        memory_limit_bytes: int = _DEFAULT_SPOOL_LIMIT_BYTES,
        on_disk_warning: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._channels = max(1, int(channels))
        self._chunks: List[np.ndarray] = []  # 每个 (channels, n) planar float32
        self._memory_bytes = 0
        self._total_samples = 0
        self._memory_limit = max(1024, int(memory_limit_bytes))
        self._spool_dir = spool_dir or "."
        self._spool_path: Optional[str] = None
        self._spool_file = None
        self._spooled_samples = 0
        self._on_disk_warning = on_disk_warning
        self._disk_warned = False

    # ── 兼容视图 ──

    @property
    def shape(self) -> Tuple[int, int]:
        """numpy 风格 (channels, total_samples)，供既有测试/诊断读取。"""
        return (self._channels, self._total_samples)

    @property
    def total_samples(self) -> int:
        return self._total_samples

    @property
    def memory_bytes(self) -> int:
        return self._memory_bytes

    @property
    def spooled_bytes(self) -> int:
        return self._spooled_samples * self._channels * 4

    def __bool__(self) -> bool:
        return self._total_samples > 0

    # ── 追加与溢写 ──

    def append(self, block: np.ndarray) -> None:
        """追加一个 (channels, n) planar 块；超限后溢写最老块到磁盘。"""
        if block.ndim == 1:
            block = block.reshape(1, -1)
        block = np.ascontiguousarray(block, dtype=np.float32)
        self._chunks.append(block)
        self._memory_bytes += block.nbytes
        self._total_samples += block.shape[1]
        while self._memory_bytes > self._memory_limit and len(self._chunks) > 1:
            self._spool_oldest()

    def _spool_oldest(self) -> None:
        # AUD-17602：磁盘预检前移到 pop 之前——预检抛出 DiskSpaceExhausted
        # 时块仍在 _chunks 且已计入 _total_samples，partial take 不缺段、
        # 时长不虚高（旧实现先 pop 再预检，块既未落盘也回不了内存）。
        oldest = self._chunks[0]
        self._check_disk_space(oldest.nbytes)
        self._chunks.pop(0)
        self._memory_bytes -= oldest.nbytes
        if self._spool_file is None:
            os.makedirs(self._spool_dir, exist_ok=True)
            self._spool_path = os.path.join(
                self._spool_dir, f"topos_rec_spool_{uuid.uuid4().hex}.f32"
            )
            self._spool_file = open(self._spool_path, "wb")
        interleaved = np.ascontiguousarray(oldest.T)  # (n, channels)
        try:
            self._spool_file.write(interleaved.tobytes())
        except OSError as e:
            # 审计修复：溢写 I/O 失败（磁盘在预检后被填满/目录被删/网络盘
            # 掉线）必须安全终止，而不是让设备回调逐个静默丢块。该块已经
            # pop 出内存——写不进去只能丢弃并终止，由 _on_audio_ready 的
            # DiskSpaceExhausted 处理链 save_partial 保住已录数据。
            # AUD-17602：被丢弃的块从 _total_samples 扣除，保证
            # duration_seconds 与文件实际内容一致（外部 DAW 按 bext
            # TimeReference 对齐不出现缺口）。
            self._total_samples -= oldest.shape[1]
            raise DiskSpaceExhausted(f"录音溢写失败，安全终止: {e}") from e
        self._spooled_samples += oldest.shape[1]

    def _check_disk_space(self, incoming_bytes: int) -> None:
        try:
            usage = shutil.disk_usage(self._spool_dir)
        except OSError:
            return
        if usage.free - incoming_bytes < _DISK_FAIL_BYTES:
            raise DiskSpaceExhausted(
                f"磁盘剩余空间不足（{usage.free / 1e6:.0f}MB），录音将安全终止"
            )
        if usage.free - incoming_bytes < _DISK_WARN_BYTES and not self._disk_warned:
            self._disk_warned = True
            if self._on_disk_warning:
                self._on_disk_warning(
                    f"磁盘剩余空间低（{usage.free / 1e6:.0f}MB）"
                )

    # ── 流式读取 ──

    def iter_blocks(self, block_frames: int = 65536) -> Iterator[np.ndarray]:
        """按顺序产出 (channels, n) 块：先磁盘溢写部分，后内存部分。"""
        if self._spool_file is not None:
            self._spool_file.flush()
            frames_per_io = max(1, block_frames)
            with open(self._spool_path, "rb") as f:  # type: ignore[arg-type]
                while True:
                    raw = f.read(frames_per_io * self._channels * 4)
                    if not raw:
                        break
                    arr = np.frombuffer(raw, dtype=np.float32).reshape(-1, self._channels)
                    yield np.ascontiguousarray(arr.T)
        for chunk in self._chunks:
            yield chunk

    def to_array(self) -> np.ndarray:
        """整体物化（仅短 take / 测试诊断用）。"""
        return np.concatenate(list(self.iter_blocks()), axis=1)

    def clear(self) -> None:
        """丢弃全部数据并删除溢写临时文件。"""
        self._chunks = []
        self._memory_bytes = 0
        self._total_samples = 0
        if self._spool_file is not None:
            try:
                self._spool_file.close()
            except Exception:
                pass
            self._spool_file = None
        if self._spool_path is not None:
            try:
                os.remove(self._spool_path)
            except OSError:
                pass
            self._spool_path = None
        self._spooled_samples = 0
        self._disk_warned = False


# ─────────────────────────────────────────────────────────────────────────────
# 录音配置与 Take 元数据
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RecordingConfig:
    """录音配置（不可变）。

    Attributes:
        device_id: 输入设备 ID（None=系统默认）
        sample_rate: 采样率（专业 ADR 固定 48000）
        channels: 声道数（1=mono VO, 2=stereo）
        bit_depth: 位深（16/24，专业交付 24）
        pre_roll_seconds: pre-roll 秒数（录音前的缓冲，给演员准备）
        post_roll_seconds: post-roll 秒数（录音后的缓冲）
        output_dir: take 文件输出目录
        project_name: 工程名（用于 BWF metadata）
        scene_name: 场次（ADR 用，如 "Scene 12"）
        talent_name: 演员名
    """
    device_id: Optional[str] = None
    sample_rate: int = 48000
    channels: int = 1  # VO/ADR 通常 mono
    bit_depth: int = 24
    pre_roll_seconds: float = 2.0
    post_roll_seconds: float = 1.0
    output_dir: str = ""
    project_name: str = ""
    scene_name: str = ""
    talent_name: str = ""
    # P1-10：时间码对齐 —— in-point 所在序列帧率与起始偏移（帧）。
    # fps 支持 Rational/float；None 表示不计算时间基准。
    fps: Any = None
    sequence_start_frame: int = 0
    # P1-10：输入延迟补偿（ms）。set_calibration 的校准结果优先于此值。
    latency_ms: float = 0.0

    def to_dict(self) -> dict:
        fps_val = self.fps
        if hasattr(fps_val, "num") and hasattr(fps_val, "den"):
            fps_val = float(fps_val.num) / float(fps_val.den)
        return {
            "device_id": self.device_id, "sample_rate": self.sample_rate,
            "channels": self.channels, "bit_depth": self.bit_depth,
            "pre_roll_seconds": self.pre_roll_seconds,
            "post_roll_seconds": self.post_roll_seconds,
            "output_dir": self.output_dir, "project_name": self.project_name,
            "scene_name": self.scene_name, "talent_name": self.talent_name,
            "fps": fps_val, "sequence_start_frame": self.sequence_start_frame,
            "latency_ms": self.latency_ms,
        }


@dataclass
class TakeInfo:
    """单个 take 的元数据（可变，录音中累积）。

    Attributes:
        take_id: 唯一 ID
        take_number: take 序号（同一场次递增）
        name: take 名称（如 "Scene12_Take03"）
        file_path: WAV 文件路径
        start_timestamp: 录音开始时间戳（ISO）
        duration_seconds: 录音时长
        sample_rate: 采样率
        channels: 声道数
        scene_name: 场次
        talent_name: 演员名
        status: take 生命周期状态，实际取值（AUD-17606：文档与实现同步，
            补齐缺失的 "pending" / "completed_partial"）：
            - "pending"：已创建、尚未开始录音（默认值）
            - "recording"：录音进行中
            - "completed"：录音完成且已落盘
            - "completed_partial"：录音中断但已保存部分文件（save_partial）
            - "failed"：录音失败（未保存或保存失败）
            - "discarded"：用户丢弃
        peak_dbfs: 峰值（dBFS，供 UI meter）
        time_reference_samples: 录音起始的时间线采样索引（写入 BWF bext
            TimeReference，供外部 DAW/NLE 按时间码对齐回时间线）
        notes: 备注
    """
    take_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    take_number: int = 1
    name: str = ""
    file_path: str = ""
    start_timestamp: str = ""
    duration_seconds: float = 0.0
    sample_rate: int = 48000
    channels: int = 1
    scene_name: str = ""
    talent_name: str = ""
    status: str = "pending"
    peak_dbfs: float = -np.inf
    time_reference_samples: int = 0
    notes: str = ""
    # P1-10：时间码对齐。in_point_frame = 打点处时间线帧（-1=未提供）；
    # start_timecode = 文件首样本对应的 SMPTE 时间码字符串。
    in_point_frame: int = -1
    start_timecode: str = ""
    # R03：文件首样本的时间线帧位（未钳制、可为负；None=未知）。
    # time_reference_samples 在首样本位于零点前时被钳到 0，无法反推
    # 零前内容长度；落 clip 用这个未钳制值对齐预卷。
    first_sample_frame: int | None = None

    def to_dict(self) -> dict:
        return {
            "take_id": self.take_id, "take_number": self.take_number,
            "name": self.name, "file_path": self.file_path,
            "start_timestamp": self.start_timestamp,
            "duration_seconds": self.duration_seconds,
            "sample_rate": self.sample_rate, "channels": self.channels,
            "scene_name": self.scene_name, "talent_name": self.talent_name,
            "status": self.status,
            "peak_dbfs": (None if self.peak_dbfs == -np.inf
                          else float(self.peak_dbfs)),
            "time_reference_samples": self.time_reference_samples,
            "notes": self.notes,
            "in_point_frame": self.in_point_frame,
            "start_timecode": self.start_timecode,
            "first_sample_frame": self.first_sample_frame,
        }


# ─────────────────────────────────────────────────────────────────────────────
# 延迟校准
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LatencyCalibration:
    """延迟校准结果（不可变）。

    Attributes:
        input_latency_ms: 输入延迟（ms，设备缓冲 + ADC）
        output_latency_ms: 输出延迟（ms，DAC + 设备缓冲）
        round_trip_ms: 往返延迟（ms，loop 测量值）
        calibrated_offset_ms: 校准后的录音偏移补偿（ms）
        timestamp: 校准时间戳
    """
    input_latency_ms: float = 0.0
    output_latency_ms: float = 0.0
    round_trip_ms: float = 0.0
    calibrated_offset_ms: float = 0.0
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "input_latency_ms": self.input_latency_ms,
            "output_latency_ms": self.output_latency_ms,
            "round_trip_ms": self.round_trip_ms,
            "calibrated_offset_ms": self.calibrated_offset_ms,
            "timestamp": self.timestamp,
        }


def calibrate_latency(
    round_trip_measurement_ms: float,
    known_input_ms: float = 0.0,
    known_output_ms: float = 0.0,
) -> LatencyCalibration:
    """从往返测量计算延迟校准（AM-08 阶段 4.4）。

    典型流程：用户播放一个 click，loop 回输入，测量往返延迟。
    校准后的 offset 用于时间戳对齐（录音偏移补偿）。

    Args:
        round_trip_measurement_ms: 实测往返延迟（ms）
        known_input_ms: 已知输入延迟（设备规格，默认 0）
        known_output_ms: 已知输出延迟（设备规格，默认 0）

    Returns:
        LatencyCalibration：校准后的偏移（≤10ms 为合格）
    """
    # 往返延迟 = 输入 + 输出（近似平分，除非有已知规格）
    if known_input_ms > 0 and known_output_ms > 0:
        input_ms = known_input_ms
        output_ms = known_output_ms
    else:
        # 平分往返延迟
        input_ms = round_trip_measurement_ms / 2.0
        output_ms = round_trip_measurement_ms / 2.0

    # 校准偏移 = 输入延迟（录音时间戳需提前此值补偿）
    # 验收：校准后剩余偏移 ≤10ms（round-trip 测量本身的误差）
    return LatencyCalibration(
        input_latency_ms=input_ms,
        output_latency_ms=output_ms,
        round_trip_ms=round_trip_measurement_ms,
        calibrated_offset_ms=input_ms,
        timestamp=datetime.now().isoformat(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# P1-10：时间码对齐 / 延迟补偿 / 校准测量（纯函数，可离线测试）
# ─────────────────────────────────────────────────────────────────────────────

_NTSC_DF_RATES = (30000.0 / 1001.0, 60000.0 / 1001.0)  # 29.97 / 59.94


def _fps_value(fps: Any) -> float:
    """Rational/float 帧率 → float（非法值返回 0）。"""
    if fps is None:
        return 0.0
    if hasattr(fps, "num") and hasattr(fps, "den"):
        try:
            den = float(fps.den)
            return float(fps.num) / den if den else 0.0
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0
    try:
        return float(fps)
    except (TypeError, ValueError):
        return 0.0


def is_drop_frame_rate(fps: Any) -> bool:
    """29.97/59.94 家族使用 SMPTE drop-frame 时间码。"""
    value = _fps_value(fps)
    return any(abs(value - rate) < 0.01 for rate in _NTSC_DF_RATES)


def first_sample_timeline_frame(
    in_point_frame: int,
    fps: Any,
    pre_roll_captured_seconds: float = 0.0,
    latency_ms: float = 0.0,
    sequence_start_frame: int = 0,
) -> float:
    """R03：文件首样本的时间线帧位（**未钳制**，可为负）。

    与 ``compute_time_reference`` 同一公式，但不做 max(0, ·) 钳制：
    预卷跨过时间线零点时，钳制会丢失"零前内容"的长度，落 clip
    就无法正确裁掉头部。fps 非法时退回 in-point + 序列偏移。
    """
    fps_value = _fps_value(fps)
    lead_seconds = (
        max(0.0, pre_roll_captured_seconds) + max(0.0, latency_ms) / 1000.0
    )
    base = float(int(in_point_frame or 0)) + float(int(sequence_start_frame))
    if fps_value <= 0:
        return base
    return base - lead_seconds * fps_value


def compute_time_reference(
    in_point_frame: int,
    fps: Any,
    sample_rate: int,
    pre_roll_captured_seconds: float = 0.0,
    latency_ms: float = 0.0,
    sequence_start_frame: int = 0,
) -> Tuple[int, str]:
    """计算 take 首样本的时间基准（BWF bext TimeReference + SMPTE TC）。

    文件首样本的时间线位置 = 序列起始偏移 + in-point −（已采集的
    pre-roll 秒数 + 输入延迟秒数）× 帧率。外部 DAW/NLE 依据 bext
    TimeReference（采样索引）即可把 take 对齐回时间线，无需猜测。

    Returns:
        (time_reference_samples, start_timecode)；fps 非法时返回 (0, "")。
    """
    fps_value = _fps_value(fps)
    if fps_value <= 0:
        return 0, ""
    from src.features.edit.core.interchange import frames_to_timecode

    first_frame = first_sample_timeline_frame(
        in_point_frame, fps,
        pre_roll_captured_seconds=pre_roll_captured_seconds,
        latency_ms=latency_ms,
        sequence_start_frame=sequence_start_frame,
    )
    samples = max(0, int(round(first_frame / fps_value * sample_rate)))
    timecode = frames_to_timecode(
        max(0, int(round(first_frame))), fps_value, is_drop_frame_rate(fps_value),
    )
    return samples, timecode


def default_click_template(sample_rate: int, duration_seconds: float = 0.004) -> np.ndarray:
    """生成延迟校准用的 click 脉冲（mono float32，带汉宁窗包络）。"""
    n = max(8, int(sample_rate * duration_seconds))
    t = np.arange(n) / float(max(1, sample_rate))
    envelope = np.hanning(n)
    carrier = np.sin(2.0 * np.pi * 1000.0 * t)
    return (0.8 * envelope * carrier).astype(np.float32)


def estimate_click_offset_ms(
    captured: np.ndarray,
    sample_rate: int,
    click: Optional[np.ndarray] = None,
    correlation_threshold: float = 0.35,
) -> Optional[float]:
    """在捕获信号中定位 click，返回 click 起始的毫秒偏移。

    归一化互相关（valid 模式）：峰值滞后即 click 相对捕获起点的延迟。
    校准验收：合成信号（已知 click 位置 + 噪声）的估计误差应远小于
    10ms 阈值。峰值相关低于 threshold 时返回 None（未检测到 click，
    避免把噪声当延迟）。
    """
    if captured is None or captured.size == 0:
        return None
    signal = np.asarray(captured, dtype=np.float32)
    if signal.ndim > 1:
        signal = signal.mean(axis=0)
    template = (default_click_template(sample_rate) if click is None
                else np.asarray(click, dtype=np.float32))
    if template.ndim > 1:
        template = template.mean(axis=0)
    if signal.size < template.size:
        return None

    sig_centered = signal - signal.mean()
    tpl_centered = template - template.mean()
    tpl_norm = float(np.sqrt(np.dot(tpl_centered, tpl_centered)))
    if tpl_norm <= 0:
        return None

    correlation = np.correlate(sig_centered, tpl_centered, mode="valid")
    peak_index = int(np.argmax(correlation))
    peak_value = float(correlation[peak_index])
    # 归一化：相关峰值 / (模板能量 × 局部信号能量^0.5) 的简化上界
    window_norm = float(np.sqrt(np.dot(
        sig_centered[peak_index:peak_index + template.size],
        sig_centered[peak_index:peak_index + template.size],
    ))) * tpl_norm
    if window_norm <= 0 or peak_value / window_norm < correlation_threshold:
        return None
    return peak_index / float(max(1, sample_rate)) * 1000.0


def measure_round_trip_latency(
    device_id: Optional[str] = None,
    sample_rate: int = 48000,
    capture_seconds: float = 2.0,
) -> Optional[LatencyCalibration]:
    """真实往返延迟测量：播放 click → 回采 → 互相关定位（P1-10）。

    需要真实音频设备与运行中的 Qt 事件循环（用户点击"校准"触发）。
    click 计划在采集开始后 CLICK_DELAY_MS 播出，测得的互相关偏移
    减去该计划点即往返延迟；estimate_click_offset_ms 定位失败（无
    loop 线/静音输入）返回 None。

    Returns:
        LatencyCalibration；设备/事件循环不可用或未检测到 click 时 None。
    """
    click_delay_ms = 300   # 采集开始后 click 的计划播出点
    schedule_delay_ms = 50  # loop 启动后再调度播出的额外延迟（审计修复：
    # 旧实现只减 click_delay_ms，少减这 50ms 调度延迟 → 往返/输入延迟
    # 系统性高估 ~25ms+，污染"≤10ms 合格"判定）
    try:
        from PyQt6.QtCore import QEventLoop, QTimer
        from PyQt6.QtMultimedia import QAudioFormat, QAudioSink, QAudioSource
    except ImportError:
        return None

    click = default_click_template(sample_rate, duration_seconds=0.02)
    lead = np.zeros(int(sample_rate * click_delay_ms / 1000.0), dtype=np.float32)
    tail = np.zeros(int(sample_rate * 0.3), dtype=np.float32)
    payload = np.concatenate([lead, click, tail]).tobytes()

    fmt = QAudioFormat()
    fmt.setSampleRate(int(sample_rate))
    fmt.setChannelCount(1)
    fmt.setSampleFormat(QAudioFormat.SampleFormat.Float)

    captured = bytearray()
    source = None
    sink = None
    try:
        device = None
        if device_id:
            from PyQt6.QtMultimedia import QMediaDevices
            for d in QMediaDevices.audioInputs():
                # F3.4：与 list_input_devices 同口径 str 化匹配
                if bytes(d.id()).decode("utf-8", "replace") == device_id:
                    device = d
                    break
        source = QAudioSource(device, fmt) if device is not None else QAudioSource(fmt)
        io = source.start()
        if io is None:
            return None
        io.readyRead.connect(lambda: captured.extend(bytes(io.readAll())))

        sink = QAudioSink(fmt)
        sink_io = sink.start()
        if sink_io is None:
            return None
        QTimer.singleShot(50, lambda: sink_io.write(payload))

        loop = QEventLoop()
        QTimer.singleShot(int(capture_seconds * 1000), loop.quit)
        loop.exec()
    except Exception as e:
        logger.warning("Latency measurement failed: %s", e)
        return None
    finally:
        for dev in (source, sink):
            if dev is not None:
                try:
                    dev.stop()
                except Exception:
                    pass

    signal = np.frombuffer(bytes(captured), dtype=np.float32)
    offset_ms = estimate_click_offset_ms(signal, sample_rate, click)
    if offset_ms is None:
        logger.info("Latency measurement: click not detected in capture")
        return None
    round_trip_ms = max(0.0, offset_ms - click_delay_ms - schedule_delay_ms)
    logger.info("Latency measured: offset=%.1fms round_trip=%.1fms",
                offset_ms, round_trip_ms)
    return calibrate_latency(round_trip_ms)


# ─────────────────────────────────────────────────────────────────────────────
# WAV 写入（P0-06：真正的 BWF —— EBU Tech 3285 bext + RIFF INFO）
# ─────────────────────────────────────────────────────────────────────────────

def is_true_bwf_supported() -> bool:
    """P0-06: 是否支持真正的 BWF（Broadcast Wave Format）。

    write_take_wav 现在在 PyAV 写完音频后注入 EBU Tech 3285 bext chunk
    （originator/originator_reference/origination date-time/TimeReference/
    coding history），由 src/features/edit/core/bwf.py 提供构建/注入/解析。
    """
    from src.features.edit.core.bwf import is_true_bwf_supported as _supported
    return _supported()


def _build_bext_for_take(
    take: Optional["TakeInfo"],
    sample_rate: int,
    bit_depth: int,
    channels: int,
) -> "BextMetadata":
    """从 TakeInfo 构建 bext 元数据。"""
    from src.features.edit.core.bwf import BextMetadata, default_coding_history

    time_ref = int(getattr(take, "time_reference_samples", 0) or 0) if take else 0
    description_parts: list[str] = []
    if take is not None:
        if take.scene_name:
            description_parts.append(f"Scene: {take.scene_name}")
        if take.talent_name:
            description_parts.append(f"Talent: {take.talent_name}")
        description_parts.append(f"Take: {take.take_number}")
        if take.name:
            description_parts.append(f"Name: {take.name}")
    return BextMetadata(
        description="; ".join(description_parts),
        originator="Topos Color",
        time_reference_samples=time_ref,
        coding_history=default_coding_history(sample_rate, bit_depth, channels),
    )


def write_take_wav(
    path: str,
    samples: np.ndarray,
    sample_rate: int = 48000,
    bit_depth: int = 24,
    take: Optional[TakeInfo] = None,
) -> bool:
    """将录音 PCM 写入 BWF 文件（bext chunk + INFO 元数据）。

    P0-06：先用 PyAV 写标准 WAV（含 ICMT/IART/INAM INFO tag），随后原子
    注入 EBU Tech 3285 bext chunk（TimeReference = take.time_reference_samples，
    供外部 DAW/NLE 按采样对齐回时间线）。bext 注入失败时文件仍是有效
    WAV（含 INFO 元数据），不视为整体失败。

    Args:
        path: 输出路径
        samples: (channels, n) PCM float32
        sample_rate: 采样率
        bit_depth: 16/24
        take: take 元数据（写入 bext/INFO）

    Returns:
        成功 True，失败 False
    """
    if samples.ndim == 1:
        samples = samples.reshape(1, -1)
    return write_take_blocks(
        path, iter([samples]), int(samples.shape[0]),
        sample_rate=sample_rate, bit_depth=bit_depth, take=take,
    )


def write_take_blocks(
    path: str,
    blocks: Iterator[np.ndarray],
    channels: int,
    sample_rate: int = 48000,
    bit_depth: int = 24,
    take: Optional[TakeInfo] = None,
) -> bool:
    """把 (channels, n) 块流式写入 BWF（P0-07：长录音不整体物化）。

    经临时文件 + os.replace 原子落盘：编码失败不会留下半成品文件
    覆盖已有 take。
    """
    try:
        import av
    except ImportError:
        logger.error("PyAV not available, cannot write take WAV")
        return False

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    codec = "pcm_s24le" if bit_depth == 24 else "pcm_s16le"
    from src.features.edit.core.channel_layout import ChannelLayout
    try:
        layout = ChannelLayout.from_channel_count(channels).av_layout_name
    except ValueError:
        layout = f"{channels}c"

    tmp_path = f"{path}.write.tmp"
    container = None
    try:
        container = av.open(tmp_path, mode="w", format="wav")
        stream = container.add_stream(codec, rate=int(sample_rate))
        stream.layout = layout

        # INFO 元数据
        if take is not None:
            comment_parts = []
            if take.scene_name:
                comment_parts.append(f"Scene: {take.scene_name}")
            if take.talent_name:
                comment_parts.append(f"Talent: {take.talent_name}")
            comment_parts.append(f"Take: {take.take_number}")
            comment_parts.append(f"Recorded: {take.start_timestamp}")
            try:
                container.metadata["ICMT"] = "; ".join(comment_parts)
                if take.talent_name:
                    container.metadata["IART"] = take.talent_name
                if take.name:
                    container.metadata["INAM"] = take.name
            except Exception as e:
                logger.warning(f"WAV metadata write failed for {path}: {e}")  # metadata 写入失败不阻断录音

        for block in blocks:
            if block.ndim == 1:
                block = block.reshape(1, -1)
            clipped = np.clip(block, -1.0, 1.0).astype(np.float32)
            frame = av.AudioFrame.from_ndarray(
                np.ascontiguousarray(clipped), format="fltp", layout=layout
            )
            frame.sample_rate = int(sample_rate)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
        container.close()
        container = None
        os.replace(tmp_path, path)
    except Exception as e:
        logger.warning(f"Failed to write take WAV {path}: {e}")
        if container is not None:
            try:
                container.close()
            except Exception:
                pass
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False

    # P0-06：注入 EBU Tech 3285 bext（TimeReference/coding history）。
    # 注入失败不否定已写好的 WAV+INFO，仅记录告警。
    from src.features.edit.core.bwf import inject_bext_chunk
    try:
        inject_bext_chunk(
            path,
            _build_bext_for_take(take, int(sample_rate), int(bit_depth), channels),
        )
    except Exception as e:
        logger.warning(f"bext injection failed for {path}: {e}")

    logger.info(f"Take BWF written: {path} ({channels}ch, {sample_rate}Hz)")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# RecordingManager：录音协调器
# ─────────────────────────────────────────────────────────────────────────────

class RecordingManager:
    """录音/ADR 协调器（AM-08 阶段 4.4）。

    职责：
    - 输入设备枚举与选择
    - pre-roll 倒计时后开始录音
    - take 命名与累积（loop record 连续生成 takes）
    - PCM 缓冲 + 落盘（BWF/WAV）
    - latency 校准
    - 设备失败安全恢复（不丢已录数据）

    信号（通过回调，避免 Qt 依赖耦合）：
    - on_state_changed(state): idle/pre_roll/recording/stopping/failed
    - on_take_completed(take): 一个 take 录完
    - on_level(dbfs): 实时电平（供 UI meter）
    - on_error(msg): 错误上报（设备拔出等）

    线程安全：录音数据采集在设备线程，take 管理在主线程。
    """

    STATE_IDLE = "idle"
    STATE_PRE_ROLL = "pre_roll"
    STATE_RECORDING = "recording"
    STATE_STOPPING = "stopping"
    STATE_FLUSHING = "flushing"
    STATE_FAILED = "failed"

    def __init__(self, config: RecordingConfig) -> None:
        self._config = config
        self._state: str = self.STATE_IDLE
        self._takes: List[TakeInfo] = []
        self._current_take: Optional[TakeInfo] = None
        self._pcm_buffer: Optional[_ChunkedPcmBuffer] = None
        self._buffer_lock = threading.Lock()
        # 溢写磁盘预警队列：缓冲在持锁 append 内部触发预警，必须先入队、
        # 退出 _buffer_lock 后再转发 on_error（外部回调不得持锁执行）。
        self._pending_disk_warnings: List[str] = []
        self._calibration: Optional[LatencyCalibration] = None

        # P1-10：rolling pre-roll —— pre-roll 倒计时期间持续采集，
        # _begin_capture 时把最近 pre_roll_seconds 的音频并入 take
        # （专业 ADR：文件包含 pre-roll 音频，首样本时间基准前移）。
        self._rolling_chunks: List[np.ndarray] = []
        self._rolling_samples: int = 0

        # P1-10 审计：打点上下文（_begin_capture 按实际 pre-roll 回修用）
        self._current_in_point_frame: Optional[int] = None
        self._current_latency_ms: float = 0.0

        # 回调
        self.on_state_changed: Optional[Callable[[str], None]] = None
        self.on_take_completed: Optional[Callable[[TakeInfo], None]] = None
        self.on_level: Optional[Callable[[float], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None
        # P1-10：输入监听 —— 实时采集块转发（planar (channels, n)），
        # 由 UI 层接 QAudioSink 做输入监听，核心保持无输出设备依赖。
        self.on_monitor_block: Optional[Callable[[np.ndarray], None]] = None

        # P1-10 post-roll：真实设备 + Qt 事件循环可用时由控制器置 True。
        # 离线/注入路径保持 stop_recording 同步落盘语义（测试与批处理依赖）。
        self.enable_live_post_roll: bool = False
        # AUD-17601：take 落盘（整段 WAV 流式写盘 + bext 注入）后台化。
        # 置 True 后 _finalize_take 在后台线程写盘（STATE_FLUSHING），
        # 完成后在主线程收 on_take_completed——长 take（10 分钟约 86MB，
        # ADR session 可达数百 MB）不再冻结 UI。默认 False：离线/注入
        # 路径与既有测试保持同步落盘语义；控制器应在事件循环可用时与
        # enable_live_post_roll 一并开启。
        self.enable_background_flush: bool = False
        # 落盘中的线程与轮询定时器（flushing 期间防重复 stop/finalize）
        self._flush_thread: Optional[threading.Thread] = None
        self._flush_timer: Any = None

        # Qt 设备（惰性初始化，避免无 Qt 环境测试失败）
        self._audio_source: Any = None
        self._io_device: Any = None
        self._start_time: float = 0.0
        self._pre_roll_timer: Any = None
        self._post_roll_timer: Any = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def takes(self) -> List[TakeInfo]:
        return list(self._takes)

    @property
    def calibration(self) -> Optional[LatencyCalibration]:
        return self._calibration

    @property
    def is_recording(self) -> bool:
        # STOPPING = post-roll 仍在采集，对外应视为录音中（防 post-roll
        # 秒内重复 arm/误判"已停"——审计修复）
        return self._state in (self.STATE_PRE_ROLL, self.STATE_RECORDING,
                               self.STATE_STOPPING)

    def set_calibration(self, calibration: LatencyCalibration) -> None:
        """设置延迟校准结果。"""
        self._calibration = calibration

    # ── 设备枚举 ──

    @staticmethod
    def list_input_devices() -> List[Tuple[str, str]]:
        """枚举可用输入设备。

        Returns:
            [(device_id, description), ...]
        """
        try:
            from PyQt6.QtMultimedia import QMediaDevices
            devices = QMediaDevices.audioInputs()
            # F3.4：PyQt6 的 d.id() 返回 QByteArray——契约要求 str
            # （UI 下拉与 config.device_id 持久化都是 str）。
            return [(bytes(d.id()).decode("utf-8", "replace"),
                     d.description()) for d in devices]
        except Exception as e:
            logger.warning(f"Failed to enumerate input devices: {e}")
            return []

    # ── 录音控制 ──

    def start_recording(
        self,
        take_number: Optional[int] = None,
        in_point_frame: Optional[int] = None,
    ) -> bool:
        """开始录音（含 pre-roll 倒计时）。

        Args:
            take_number: take 序号，None=自动递增
            in_point_frame: 打点处的时间线帧（P1-10 时间码对齐）。
                提供且 config.fps 有效时，计算 bext TimeReference 与
                take.start_timecode；None 表示不对齐时间线。

        Returns:
            True=已启动（进入 pre_roll），False=失败
        """
        if self.is_recording or self._state in (self.STATE_STOPPING,
                                                self.STATE_FLUSHING):
            logger.warning("Already recording")
            return False

        # take 序号
        if take_number is None:
            take_number = (self._takes[-1].take_number + 1) if self._takes else 1

        # 创建 take
        scene = self._config.scene_name or "Untitled"
        take_name = f"{scene}_Take{take_number:02d}"
        timestamp = datetime.now().isoformat()
        file_path = os.path.join(
            self._config.output_dir or ".",
            f"{take_name}_{timestamp.replace(':', '-')}.wav",
        )
        take = TakeInfo(
            take_number=take_number, name=take_name, file_path=file_path,
            start_timestamp=timestamp, sample_rate=self._config.sample_rate,
            channels=self._config.channels, scene_name=scene,
            talent_name=self._config.talent_name, status="recording",
        )
        # P1-10：时间码对齐（首样本 = in-point − pre-roll − 输入延迟）。
        # 先按名义 pre-roll 计算；_begin_capture 并入 rolling 后按实际
        # 采集量回修（设备打开耗时会吃掉倒计时头部，审计修复：文件
        # 实际含多少 pre-roll，bext TimeReference 就前移多少）。
        self._current_in_point_frame = in_point_frame
        self._current_latency_ms = 0.0
        if in_point_frame is not None:
            take.in_point_frame = int(in_point_frame)
            latency_ms = (
                self._calibration.calibrated_offset_ms
                if self._calibration is not None
                else self._config.latency_ms
            )
            self._current_latency_ms = latency_ms
            take.time_reference_samples, take.start_timecode = compute_time_reference(
                int(in_point_frame), self._config.fps, self._config.sample_rate,
                pre_roll_captured_seconds=self._config.pre_roll_seconds,
                latency_ms=latency_ms,
                sequence_start_frame=self._config.sequence_start_frame,
            )
            # R03：未钳制首样本帧位（预卷跨零点时 time_reference 被钳
            # 到 0，零前内容长度从这里才可恢复）
            take.first_sample_frame = int(round(first_sample_timeline_frame(
                int(in_point_frame), self._config.fps,
                pre_roll_captured_seconds=self._config.pre_roll_seconds,
                latency_ms=latency_ms,
                sequence_start_frame=self._config.sequence_start_frame,
            )))
        self._current_take = take

        # PCM 缓冲（P0-07：分块 + 磁盘溢写，长录音内存有界）
        with self._buffer_lock:
            if self._pcm_buffer is not None:  # 防御：上次录音残留
                self._pcm_buffer.clear()
            self._pcm_buffer = _ChunkedPcmBuffer(
                channels=self._config.channels,
                spool_dir=self._config.output_dir or ".",
                # 预警只入队：append 在 _buffer_lock 内执行，直接回调
                # on_error 会在持锁状态下运行外部代码（回调若再入
                # stop_recording→_finalize_take 将二次加锁死锁）。
                on_disk_warning=self._pending_disk_warnings.append,
            )
            self._rolling_chunks = []
            self._rolling_samples = 0

        # pre-roll
        if self._config.pre_roll_seconds > 0:
            self._set_state(self.STATE_PRE_ROLL)
            # P1-10 rolling pre-roll：倒计时期间就开始采集（capture 会进入
            # rolling 缓冲）。启动失败不阻断倒计时，_begin_capture 会重试。
            self._start_capture_device()
            # 实际 pre-roll 用 QTimer；测试可跳过
            try:
                from PyQt6.QtCore import QTimer
                self._pre_roll_timer = QTimer()
                self._pre_roll_timer.setSingleShot(True)
                self._pre_roll_timer.timeout.connect(self._begin_capture)
                self._pre_roll_timer.start(int(self._config.pre_roll_seconds * 1000))
            except Exception:
                # 无 Qt 环境（测试）：直接开始
                self._begin_capture()
        else:
            self._begin_capture()

        return True

    def _start_capture_device(self) -> bool:
        """启动 QAudioSource 输入设备（已启动时直接返回 True）。"""
        if self._io_device is not None:
            return True
        try:
            from PyQt6.QtMultimedia import QAudioSource, QAudioFormat
        except ImportError:
            logger.warning("Qt multimedia not available for recording")
            return False

        fmt = QAudioFormat()
        fmt.setSampleRate(self._config.sample_rate)
        fmt.setChannelCount(self._config.channels)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Float)

        try:
            # 选择设备
            device = None
            if self._config.device_id:
                from PyQt6.QtMultimedia import QMediaDevices
                for d in QMediaDevices.audioInputs():
                    if bytes(d.id()).decode(
                            "utf-8", "replace") == self._config.device_id:
                        device = d
                        break
            if device is not None:
                self._audio_source = QAudioSource(device, fmt)
            else:
                self._audio_source = QAudioSource(fmt)
            self._io_device = self._audio_source.start()
            if self._io_device is not None:
                self._io_device.readyRead.connect(self._on_audio_ready)
                # 监听设备错误
                self._audio_source.stateChanged.connect(self._on_source_state_changed)
                return True
            logger.error("QAudioSource.start() returned None")
            if self.on_error:
                self.on_error("无法启动录音设备")
            self._audio_source = None
            return False
        except Exception as e:
            logger.error(f"Failed to start recording: {e}")
            self._audio_source = None
            if self.on_error:
                self.on_error(f"录音启动失败: {e}")
            return False

    def _begin_capture(self) -> None:
        """pre-roll 结束：rolling 缓冲并入 take，正式进入 recording。"""
        # rolling pre-roll → take 缓冲（只保留最近 pre_roll_seconds）
        with self._buffer_lock:
            rolling = self._rolling_chunks
            self._rolling_chunks = []
            self._rolling_samples = 0
        if rolling and self._pcm_buffer is not None:
            merged = np.concatenate(rolling, axis=1) if len(rolling) > 1 else rolling[0]
            keep = int(self._config.pre_roll_seconds * self._config.sample_rate)
            if keep > 0 and merged.shape[1] > keep:
                merged = merged[:, merged.shape[1] - keep:]
            if merged.size > 0:
                with self._buffer_lock:
                    self._pcm_buffer.append(np.ascontiguousarray(merged, dtype=np.float32))
                # 审计修复：实际并入的 pre-roll（秒）回写时间基准，
                # bext TimeReference 与文件内容一致（外部 DAW 对齐精度）
                if (self._current_take is not None
                        and self._current_in_point_frame is not None):
                    actual_seconds = float(merged.shape[1]) / float(
                        self._config.sample_rate)
                    self._current_take.time_reference_samples, \
                        self._current_take.start_timecode = \
                        compute_time_reference(
                            int(self._current_in_point_frame),
                            self._config.fps, self._config.sample_rate,
                            pre_roll_captured_seconds=actual_seconds,
                            latency_ms=self._current_latency_ms,
                            sequence_start_frame=self._config.sequence_start_frame,
                        )
                    # R03：实际并入量同步回修未钳制首样本帧位
                    self._current_take.first_sample_frame = int(round(
                        first_sample_timeline_frame(
                            int(self._current_in_point_frame),
                            self._config.fps,
                            pre_roll_captured_seconds=actual_seconds,
                            latency_ms=self._current_latency_ms,
                            sequence_start_frame=self._config.sequence_start_frame,
                        )))

        self._set_state(self.STATE_RECORDING)
        self._start_time = time.time()

        # pre-roll 期间设备未启动成功（或无 pre-roll 路径）→ 现在启动
        if not self._start_capture_device():
            self._set_state(self.STATE_FAILED)

    def _on_audio_ready(self) -> None:
        """设备有数据可读：按状态路由到 rolling / take 缓冲。"""
        if self._io_device is None:
            return
        if self._state not in (self.STATE_PRE_ROLL, self.STATE_RECORDING,
                               self.STATE_STOPPING):
            return
        try:
            data = self._io_device.readAll()
            if data is None or data.size() == 0:
                return
            import numpy as _np
            # float32 planar（QAudio Float format）
            raw = _np.frombuffer(bytes(data), dtype=_np.float32)
            channels = self._config.channels
            if channels > 1:
                # PyAV/Qt 交错 → reshape 为 (channels, n)
                n = len(raw) // channels
                raw = raw[:n * channels].reshape(n, channels).T
            else:
                raw = raw.reshape(1, -1)
            raw = _np.ascontiguousarray(raw, dtype=_np.float32)

            # P1-10 输入监听：实时块转发（pre-roll 期间演员就要听到输入）
            if self.on_monitor_block is not None and raw.size > 0:
                try:
                    self.on_monitor_block(raw)
                except Exception as e:
                    logger.warning(f"monitor block forward failed: {e}")

            if self._state == self.STATE_PRE_ROLL:
                # rolling pre-roll：只保留最近 pre_roll_seconds
                keep = int(self._config.pre_roll_seconds * self._config.sample_rate)
                with self._buffer_lock:
                    self._rolling_chunks.append(raw)
                    self._rolling_samples += raw.shape[1]
                    while (self._rolling_samples > keep and self._rolling_chunks
                           and self._rolling_chunks[0].shape[1] < self._rolling_samples - keep):
                        self._rolling_samples -= self._rolling_chunks[0].shape[1]
                        self._rolling_chunks.pop(0)
            else:
                disk_error: Optional[DiskSpaceExhausted] = None
                with self._buffer_lock:
                    if self._pcm_buffer is None:
                        self._pcm_buffer = _ChunkedPcmBuffer(channels=channels)
                    try:
                        self._pcm_buffer.append(raw)
                    except DiskSpaceExhausted as e:
                        # P0-07：磁盘耗尽 → 安全终止并保留已录数据。
                        # 锁内只捕获：_finalize_take 与 on_error 都可能再次
                        # 获取 _buffer_lock（threading.Lock 非重入），
                        # 必须退出 with 块后再处理，否则同线程死锁挂死。
                        disk_error = e
                if disk_error is not None:
                    self._drain_disk_warnings()
                    self._set_state(self.STATE_FAILED)
                    if self.on_error:
                        self.on_error(str(disk_error))
                    self._finalize_take(save_partial=True)
                    return
            # 磁盘预警在锁外转发（append 期间只入队）
            self._drain_disk_warnings()

            # 实时电平
            peak = float(_np.max(_np.abs(raw))) if raw.size > 0 else 0.0
            dbfs = 20 * _np.log10(max(peak, 1e-9))
            if self._current_take is not None:
                self._current_take.peak_dbfs = max(self._current_take.peak_dbfs, dbfs)
            if self.on_level:
                self.on_level(dbfs)
        except Exception as e:
            logger.warning(f"Audio read failed: {e}")

    def _on_source_state_changed(self, state: "QAudio.State") -> None:
        """设备状态变化（含错误/断开）。"""
        try:
            from PyQt6.QtMultimedia import QAudio
            if state == QAudio.State.StoppedState:
                # 检查是否是错误（设备拔出）
                err = self._audio_source.error() if self._audio_source else None
                if err is not None and err != QAudio.Error.NoErrorError:
                    msg = f"录音设备错误: {err}"
                    logger.error(msg)
                    self._set_state(self.STATE_FAILED)
                    if self.on_error:
                        self.on_error(msg)
                    # 安全保存已录数据（失败恢复：不丢工程）
                    self._finalize_take(save_partial=True)
        except Exception as e:
            logger.warning(f"state changed handler error: {e}")

    def stop_recording(self) -> Optional[TakeInfo]:
        """停止录音，落盘 take。

        P1-10 post-roll：真实设备在采时，先进入 STATE_STOPPING 并继续
        采集 post_roll_seconds（保留打点后的尾巴），定时到后统一落盘；
        此时返回 None（take 仍经 on_take_completed 单一事件源交付）。
        离线/注入路径（无活动设备）保持同步落盘并返回 take。
        AUD-17601：enable_background_flush 开启时落盘在后台线程进行
        （STATE_FLUSHING），期间 stop/start 均被状态守卫拒绝。

        Returns:
            完成的 TakeInfo，或 None（无录音/失败/延迟落盘/落盘中）
        """
        if self._state not in (self.STATE_RECORDING, self.STATE_PRE_ROLL,
                               self.STATE_STOPPING, self.STATE_FAILED):
            return None

        # 取消 pre-roll 定时器
        if self._pre_roll_timer is not None:
            try:
                self._pre_roll_timer.stop()
            except Exception:
                pass
            self._pre_roll_timer = None

        # P1-10 post-roll：设备在采 → 定时延迟落盘（期间继续捕获）。
        # 二次调用（post-roll 中再按停止）立即落盘。
        if (self._state == self.STATE_RECORDING
                and self.enable_live_post_roll
                and self._io_device is not None
                and self._config.post_roll_seconds > 0
                and self._post_roll_timer is None):
            self._set_state(self.STATE_STOPPING)
            try:
                from PyQt6.QtCore import QTimer
                self._post_roll_timer = QTimer()
                self._post_roll_timer.setSingleShot(True)
                self._post_roll_timer.timeout.connect(self._on_post_roll_finished)
                self._post_roll_timer.start(
                    int(self._config.post_roll_seconds * 1000))
                return None
            except Exception:
                self._post_roll_timer = None  # 无 Qt：立即落盘

        return self._finalize_take(save_partial=False)

    def _on_post_roll_finished(self) -> None:
        """post-roll 定时到：停止设备并落盘。"""
        self._post_roll_timer = None
        if self._state != self.STATE_STOPPING:
            return
        self._finalize_take(save_partial=False)

    def _finalize_take(self, save_partial: bool) -> Optional[TakeInfo]:
        """落盘当前 take 并归档。

        AUD-17601：enable_background_flush 开启且 Qt 事件循环可用时，
        WAV 流式写盘在后台线程执行（STATE_FLUSHING，期间重复 stop 返回
        None、start 被拒），完成后在主线程归档并回调 on_take_completed。
        默认（离线/注入/测试）保持同步落盘语义不变。
        """
        take = self._current_take
        if take is None:
            self._set_state(self.STATE_IDLE)
            return None

        # RMA-11: 已被 discard_current_take 标记为 discarded 的 take 不得落盘。
        # 丢弃时缓冲已被清空，这里二次防御：即便外部仍调 stop_recording，
        # 也不会把已丢弃的 take 写成文件或覆盖回 completed。
        if take.status == "discarded":
            with self._buffer_lock:
                if self._pcm_buffer is not None:
                    self._pcm_buffer.clear()
                    self._pcm_buffer = None
                self._rolling_chunks = []
                self._rolling_samples = 0
            self._takes.append(take)
            self._current_take = None
            self._set_state(self.STATE_IDLE)
            return take

        # 停止 post-roll 定时器（立即落盘路径）
        if self._post_roll_timer is not None:
            try:
                self._post_roll_timer.stop()
            except Exception:
                pass
            self._post_roll_timer = None

        # 停止设备
        if self._audio_source is not None:
            try:
                self._audio_source.stop()
            except Exception:
                pass
            self._audio_source = None
        self._io_device = None

        # 缓冲交接（锁内摘除引用；此后仅落盘路径持有 pcm）
        with self._buffer_lock:
            pcm = self._pcm_buffer
            self._pcm_buffer = None
            self._rolling_chunks = []
            self._rolling_samples = 0
        # flushing 是终止态：take 上下文立即归零，重复 stop/start 全部
        # 走状态守卫，不会二次处理同一 take。
        self._current_take = None

        if pcm is None or pcm.total_samples <= 0:
            # 空 take：无 I/O，直接在当前线程完成
            if pcm is not None:
                pcm.clear()
            if self._start_time > 0:
                take.duration_seconds = max(0.0, time.time() - self._start_time)
            take.status = "discarded" if not save_partial else "failed"
            return self._archive_take(take)

        if self._try_start_background_flush(take, pcm, save_partial):
            return None

        # 同步落盘（默认路径：离线/注入/无 Qt 事件循环）
        self._write_take_payload(take, pcm, save_partial)
        return self._archive_take(take)

    def _write_take_payload(
        self,
        take: TakeInfo,
        pcm: _ChunkedPcmBuffer,
        save_partial: bool,
    ) -> None:
        """整段 WAV 流式写盘 + bext 注入（AUD-17601 抽出，可后台执行）。

        锁纪律：pcm 已在 _finalize_take 内完成交接（独占引用），本方法
        不取任何管理器锁。
        """
        # P1-10：时长用采样数（含 pre-roll 内容），比墙钟精确
        take.duration_seconds = pcm.total_samples / float(self._config.sample_rate)
        ok = write_take_blocks(
            take.file_path, pcm.iter_blocks(),
            channels=self._config.channels,
            sample_rate=self._config.sample_rate,
            bit_depth=self._config.bit_depth, take=take,
        )
        pcm.clear()
        if ok:
            take.status = "completed" if not save_partial else "completed_partial"
        else:
            take.status = "failed"

    def _archive_take(self, take: TakeInfo) -> TakeInfo:
        """归档完成的 take（主线程执行：列表追加 + 状态回调）。"""
        self._takes.append(take)
        self._set_state(self.STATE_IDLE)
        if take.status in ("completed", "completed_partial") and self.on_take_completed:
            self.on_take_completed(take)
        return take

    def _try_start_background_flush(
        self,
        take: TakeInfo,
        pcm: _ChunkedPcmBuffer,
        save_partial: bool,
    ) -> bool:
        """AUD-17601：后台落盘。成功启动返回 True（调用方返回 None）。

        任何前置条件不满足（未启用/无 Qt 事件循环/已在落盘中/定时器不可
        用）都返回 False，调用方回退同步落盘；线程一旦启动则必须完成交付
        （轮询定时器在主线程驱动归档，不依赖跨线程 Qt 调用）。
        """
        if not self.enable_background_flush or self._flush_thread is not None:
            return False
        try:
            from PyQt6.QtCore import QCoreApplication, QTimer
            if QCoreApplication.instance() is None:
                return False
            done = threading.Event()
            poll = QTimer()
            poll.setInterval(15)

            def _poll_flush_done() -> None:
                if not done.is_set():
                    return
                poll.stop()
                self._flush_timer = None
                self._flush_thread = None
                self._archive_take(take)

            poll.timeout.connect(_poll_flush_done)
        except Exception:
            logger.debug("background flush unavailable, falling back to sync")
            return False

        def _flush_worker() -> None:
            try:
                self._write_take_payload(take, pcm, save_partial)
            except Exception as e:  # noqa: BLE001 — worker 边界必须交付
                logger.warning(f"Take flush failed: {e}", exc_info=True)
                take.status = "failed"
            finally:
                done.set()

        self._set_state(self.STATE_FLUSHING)
        thread = threading.Thread(
            target=_flush_worker, name="TakeFlush", daemon=True,
        )
        started = False
        try:
            thread.start()
            started = True
            poll.start()
        except Exception:
            # 线程/定时器启动失败的同步兜底。已启动的 worker 必须先 join
            #（它可能正在写同一文件），归档但绝不二次写盘。
            logger.warning(
                "background flush start failed, completing synchronously",
                exc_info=True,
            )
            if started:
                thread.join()
            else:
                self._write_take_payload(take, pcm, save_partial)
            self._flush_thread = None
            self._archive_take(take)
            return True
        self._flush_thread = thread
        self._flush_timer = poll
        return True

    def discard_current_take(self) -> None:
        """丢弃当前 take（演员要求重录，RMA-11）。

        discard 必须是"不落盘"的终止：停止设备、清缓冲、归档为 discarded，
        且不得在后续 stop_recording() 时又把已丢弃的 PCM 写成文件。
        旧实现只把 status 标记为 discarded，但 _finalize_take 不读 status、
        只看 PCM 是否非空 → 文件仍会落盘且 status 被覆盖回 completed。
        """
        if self._current_take is None:
            return
        self._current_take.status = "discarded"
        # 停止设备 + 清缓冲，确保后续不会再触发落盘
        if self._pre_roll_timer is not None:
            try:
                self._pre_roll_timer.stop()
            except Exception:
                pass
            self._pre_roll_timer = None
        if self._post_roll_timer is not None:
            try:
                self._post_roll_timer.stop()
            except Exception:
                pass
            self._post_roll_timer = None
        if self._audio_source is not None:
            try:
                self._audio_source.stop()
            except Exception:
                pass
            self._audio_source = None
        self._io_device = None
        with self._buffer_lock:
            if self._pcm_buffer is not None:
                self._pcm_buffer.clear()
                self._pcm_buffer = None
            self._rolling_chunks = []
            self._rolling_samples = 0
        # 归档已丢弃的 take（供 UI/审计），但不触发 on_take_completed
        self._takes.append(self._current_take)
        self._current_take = None
        self._set_state(self.STATE_IDLE)

    def cancel_recording(self) -> None:
        """取消录音（不落盘，丢弃缓冲）。"""
        # AUD-17601：落盘已在进行（flushing）时，后台写盘不可中断——
        # 等它结束后 cancel 只清理剩余状态（take 由归档路径交付）。
        flush_thread = self._flush_thread
        if flush_thread is not None:
            flush_thread.join()
            self._flush_thread = None
        if self._pre_roll_timer is not None:
            try:
                self._pre_roll_timer.stop()
            except Exception:
                pass
        if self._post_roll_timer is not None:
            try:
                self._post_roll_timer.stop()
            except Exception:
                pass
            self._post_roll_timer = None
        if self._audio_source is not None:
            try:
                self._audio_source.stop()
            except Exception:
                pass
            self._audio_source = None
        self._io_device = None
        with self._buffer_lock:
            if self._pcm_buffer is not None:
                self._pcm_buffer.clear()
                self._pcm_buffer = None
            self._rolling_chunks = []
            self._rolling_samples = 0
        self._current_take = None
        self._set_state(self.STATE_IDLE)

    def _set_state(self, state: str) -> None:
        self._state = state
        if self.on_state_changed:
            self.on_state_changed(state)

    def _drain_disk_warnings(self) -> None:
        """把持锁期间入队的磁盘预警转发给 on_error。

        必须在 _buffer_lock 之外调用：on_error 属外部回调，若其在持锁
        状态下执行并再入 stop_recording/_finalize_take，会对非重入锁
        二次加锁而死锁（AUD-17600 同类路径）。
        """
        if not self._pending_disk_warnings:
            return
        if self.on_error is None:
            self._pending_disk_warnings.clear()
            return
        while self._pending_disk_warnings:
            self.on_error(self._pending_disk_warnings.pop(0))

    # ── 测试/离线辅助：注入 PCM（不经设备）──

    def inject_pcm(self, samples: np.ndarray) -> None:
        """注入 PCM（测试/离线用，不经 QAudioSource）。

        pre_roll 状态下注入进入 rolling 缓冲（与真实设备路由一致），
        _begin_capture 时并入 take；其余状态直接进 take 缓冲。

        Args:
            samples: (channels, n) PCM float32
        """
        if samples.ndim == 1:
            samples = samples.reshape(1, -1)
        if self._state == self.STATE_PRE_ROLL:
            keep = int(self._config.pre_roll_seconds * self._config.sample_rate)
            with self._buffer_lock:
                self._rolling_chunks.append(
                    np.ascontiguousarray(samples, dtype=np.float32))
                self._rolling_samples += samples.shape[1]
                while (self._rolling_samples > keep and self._rolling_chunks
                       and self._rolling_chunks[0].shape[1] < self._rolling_samples - keep):
                    self._rolling_samples -= self._rolling_chunks[0].shape[1]
                    self._rolling_chunks.pop(0)
        else:
            with self._buffer_lock:
                if self._pcm_buffer is None:
                    self._pcm_buffer = _ChunkedPcmBuffer(channels=self._config.channels)
                self._pcm_buffer.append(samples)
        # 磁盘预警在锁外转发（append 期间只入队）
        self._drain_disk_warnings()
        # 更新电平
        if self._current_take is not None and samples.size > 0:
            peak = float(np.max(np.abs(samples)))
            dbfs = 20 * np.log10(max(peak, 1e-9))
            self._current_take.peak_dbfs = max(self._current_take.peak_dbfs, dbfs)
            if self.on_level:
                self.on_level(dbfs)
