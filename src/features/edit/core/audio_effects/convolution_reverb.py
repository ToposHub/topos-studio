"""P1 · Convolution Reverb（2026-11-19 计划第三阶段 · 效果器接入 #6）。

- IR 资源登记：``IrResource``（URI + SHA-256 指纹 + 域信息）；装载统一
  重采样（会话率）、首尾静音裁切与峰值规范化；
- 低延迟分区 FFT 卷积（overlap-save）：频域分区 kernel 在装载时一次
  冻结；每个 ≤P 子块只做一次输入 FFT（窗口 = [P 样本历史 | 子块]），
  与全部分区谱相乘累加后一次逆变换——输出因果、与输入逐样本对齐，
  ``latency`` 恒为实测 onset（0）；端点加窗防爆音；
- 声道矩阵：mono IR → 各声道同卷积（mono→stereo）；立体声 IR 逐声道；
  true stereo 四路径（LL/LR/RL/RR，第一字母=输出，规格显式；4 路 WAV
  按声道序显式成矩阵，2 路 WAV 交叉路径缺数据 → 对角退化）；
- 装载生命周期：加载键含内容指纹（mtime+size），同路径替换重验哈希；
  清空或确认失败在参数边界停用旧 kernel、清历史，失败键不逐块重试。
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
import wave
from collections import OrderedDict, deque
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Deque, Dict, List, Optional, Tuple

import numpy as np

import logging

logger = logging.getLogger(__name__)

from src.features.edit.core.audio_dsp import AudioEffect

try:  # pragma: no cover - 环境探测
    from scipy.signal import resample_poly
    _SCIPY = True
except ImportError:  # pragma: no cover
    _SCIPY = False

#: 分区卷积的块长（样本；与引擎帧块同量级）。
CONV_BLOCK = 2048


def _next_pow2(n: int) -> int:
    return 1 << max(0, int(np.ceil(np.log2(max(1, n)))))


@dataclass(frozen=True)
class IrResource:
    """登记的 IR 资源（URI + 指纹 + 域信息；移动工程后指纹校验）。"""

    uri: str
    sha256: str
    sample_rate: int
    channels: int
    samples: int
    label: str = ""

    def to_dict(self) -> dict:
        return {"uri": self.uri, "sha256": self.sha256,
                "sample_rate": self.sample_rate, "channels": self.channels,
                "samples": self.samples, "label": self.label}

    @classmethod
    def from_dict(cls, data: dict) -> "IrResource":
        return cls(uri=str(data["uri"]), sha256=str(data.get("sha256", "")),
                   sample_rate=int(data.get("sample_rate", 48000)),
                   channels=int(data.get("channels", 1)),
                   samples=int(data.get("samples", 0)),
                   label=str(data.get("label", "") or ""))


def _sha256_of(path: str, chunk: int = 1 << 20,
               cancel_check: Optional[Callable[[], bool]] = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            if cancel_check is not None and cancel_check():
                raise CancelledError()
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def load_ir(
    path: str,
    sample_rate: int,
    *,
    normalize_peak_db: float = -6.0,
    trim_silence_db: float = -60.0,
    edge_fade_ms: float = 4.0,
    max_seconds: float = 8.0,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Tuple[np.ndarray, IrResource]:
    """装载 IR：指纹校验域 + 重采样 + 裁切 + 规范化 + 端点加窗。

    返回 (ir planar float (channels, n), resource)。
    """
    import wave

    if sample_rate <= 0 or not np.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError("IR sample rate and duration must be positive")
    if cancel_check is not None and cancel_check():
        raise CancelledError()
    with wave.open(path, "rb") as wav:
        ir_rate = wav.getframerate()
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        frames = min(wav.getnframes(), int(np.ceil(max_seconds * ir_rate)))
        if channels not in (1, 2, 4) or frames <= 0:
            raise ValueError("IR must contain nonempty mono, stereo or four-path PCM")
        chunks = []
        for start in range(0, frames, 65536):
            if cancel_check is not None and cancel_check():
                raise CancelledError()
            chunks.append(wav.readframes(min(65536, frames - start)))
        raw = b"".join(chunks)
    if width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 3:
        packed = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        signed = packed[:, 0] | (packed[:, 1] << 8) | (packed[:, 2] << 16)
        signed = (signed ^ 0x800000) - 0x800000
        data = signed.astype(np.float32) / 8388608.0
    else:
        raise ValueError(f"unsupported IR sample width: {width}")
    data = data.reshape(-1, channels).T  # (channels, n)

    # 统一重采样到会话率。
    if ir_rate != sample_rate:
        if not _SCIPY:
            raise ValueError("IR resampling requires scipy")
        data = resample_poly(data, sample_rate, ir_rate, axis=-1).astype(np.float32)
        ir_rate = sample_rate

    # 时长护栏。
    max_samples = int(max_seconds * sample_rate)
    if data.shape[1] > max_samples:
        data = data[:, :max_samples]

    # 首尾静音裁切（按阈值；保留少量前置 pre-delay 邻域）。
    mono = np.max(np.abs(data), axis=0)
    threshold = 10.0 ** (trim_silence_db / 20.0)
    above = np.nonzero(mono > threshold)[0]
    if above.size:
        pad = int(0.002 * sample_rate)
        lo = max(0, int(above[0]) - pad)
        hi = min(data.shape[1], int(above[-1]) + 1 + pad)
        data = data[:, lo:hi]

    # 端点加窗（防爆音）。
    fade = max(1, int(edge_fade_ms * sample_rate / 1000.0))
    if data.shape[1] > 2 * fade:
        ramp_in = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        ramp_out = np.linspace(1.0, 0.0, fade, dtype=np.float32)
        data[:, :fade] *= ramp_in[np.newaxis, :]
        data[:, -fade:] *= ramp_out[np.newaxis, :]

    # 峰值规范化（加窗后——规范化目标包含窗后的实际峰值）。
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    if peak > 1e-9:
        data = data * (10.0 ** (normalize_peak_db / 20.0) / peak)

    resource = IrResource(
        uri=path, sha256=_sha256_of(path, cancel_check=cancel_check), sample_rate=ir_rate,
        channels=channels, samples=int(data.shape[1]))
    return np.ascontiguousarray(data, dtype=np.float32), resource


class PartitionedConvolver:
    """低延迟分区 FFT 卷积（头分区滑动卷积 + 尾部 UPC 频域延迟线）。

    - 头分区（IR 前 P 样本）：滑动 overlap-save 处理任意块长子块
      （窗口 = [P 历史 | 子块]，FFT 恒 2P，不随 IR 全长膨胀）——
      零算法延迟（推导：``(h⊛w)[P+j] = Σ h[k]·x[s+j-k] = y[s+j]``）；
    - 其余分区在 P 对齐网格上跑均匀分区卷积（UPC）：输出块 B 的尾部
      贡献 ``W_B = Σ_{i≥1} IFFT(H_i · X̂_{B-i})``，其中 ``X̂_j`` 是
      输入块 j 的 2P 窗谱。``W_B`` 只依赖截至 ``B·P`` 的输入 →
      部分段（网格未齐）的输出同样零延迟；
    - 频域分区 kernel 在构造时一次冻结，``process`` 不再重复变换。

    输出因果且与输入逐样本对齐：``latency`` 恒为实测 onset（0）；
    ``tail`` = IR 长度（全部因果样本）。
    """

    def __init__(self, ir: np.ndarray, block: int = CONV_BLOCK) -> None:
        kernel = np.asarray(ir, dtype=np.float32)
        if kernel.ndim == 1:
            kernel = kernel.reshape(1, -1)
        self._kernel_channels = int(kernel.shape[0])
        self._ir_len = int(kernel.shape[-1])
        # 分区大小：短 IR 收缩 FFT 尺寸；长 IR 封顶 block，避免 FFT 随
        # IR 全长增大（旧实现 next_pow2(block + ir_len - 1)）。
        self._part = int(max(
            64, min(int(block), _next_pow2(max(1, self._ir_len)))))
        self._nfft = 2 * self._part
        # 1 样本 IR：逐声道增益直通（mono 广播、多声道逐列）。
        self._gain: Optional[np.ndarray] = (
            kernel.astype(np.float32).copy()
            if self._ir_len == 1 and kernel.size else None)
        # 冻结频域分区 kernel：head = IR[0:P]；tail = 余下 P 大小的分区。
        self._head_spec: Optional[np.ndarray] = None
        self._tail_specs: List[np.ndarray] = []
        if self._ir_len > 1:
            head = kernel[:, :self._part]
            self._head_spec = np.fft.rfft(head, n=self._nfft, axis=-1)
            for start in range(self._part, self._ir_len, self._part):
                part = kernel[:, start:start + self._part]
                self._tail_specs.append(
                    np.fft.rfft(part, n=self._nfft, axis=-1))
        self._reset_stream()

    # -- 状态 ----------------------------------------------------------

    def _reset_stream(self) -> None:
        part = self._part
        kch = self._kernel_channels
        self._hist = np.zeros((kch, part), dtype=np.float32)
        self._prev_block = np.zeros((kch, part), dtype=np.float32)
        self._grid_buf = np.zeros((kch, part), dtype=np.float32)
        self._grid_fill = 0
        self._blocks_done = 0
        self._out_pos = 0
        # 频域延迟线：最近 N 个输入块谱（X̂_{B-i}）；N = 尾部分区数。
        self._fdl: Deque[np.ndarray] = deque(maxlen=len(self._tail_specs))
        # 待消费的网格尾部输出：(块号 B, W_B 覆盖 [B·P,(B+1)·P))。
        self._pending: Deque[Tuple[int, np.ndarray]] = deque()

    def reset(self) -> None:
        self._reset_stream()

    @property
    def latency(self) -> int:
        """实测算法延迟：头滑动段 + 网格尾部均因果对齐，恒为 0（= onset）。"""
        return 0

    @property
    def ir_length(self) -> int:
        return self._ir_len

    # -- 处理 ----------------------------------------------------------

    @staticmethod
    def _apply_spec(spec: np.ndarray, spec_x: np.ndarray,
                    channels: int) -> np.ndarray:
        """分区谱 × 输入谱：mono 广播、多声道截断/平铺（保持旧语义）。"""
        kch = spec.shape[0]
        if kch == 1 or kch == channels:
            return spec * spec_x
        if kch > channels:
            return spec[:channels] * spec_x
        return np.repeat(spec, channels // kch, axis=0) * spec_x

    def _process_head(self, x: np.ndarray, channels: int,
                      n: int) -> np.ndarray:
        """头分区贡献：任意块长滑动 overlap-save（零延迟）。"""
        if self._hist.shape != (channels, self._part):
            self._hist = np.zeros((channels, self._part), dtype=np.float32)
        out = np.empty((channels, n), dtype=np.float32)
        pos = 0
        while pos < n:
            take = min(self._part, n - pos)
            window = np.zeros((channels, self._nfft), dtype=np.float32)
            window[:, :self._part] = self._hist
            window[:, self._part:self._part + take] = x[:, pos:pos + take]
            spec_x = np.fft.rfft(window, axis=-1)
            conv = np.fft.irfft(
                self._apply_spec(self._head_spec, spec_x, channels),
                n=self._nfft, axis=-1)
            out[:, pos:pos + take] = conv[:, self._part:self._part + take]
            if take == self._part:
                self._hist = x[:, pos:pos + take].copy()
            else:
                self._hist = np.concatenate(
                    (self._hist[:, take:], x[:, pos:pos + take]), axis=1)
            pos += take
        return out

    def _feed_grid(self, x: np.ndarray, channels: int, n: int) -> None:
        """喂入网格输入并完成尽可能多的网格块（更新延迟线与待发尾部）。"""
        pos = 0
        while pos < n:
            take = min(self._part - self._grid_fill, n - pos)
            self._grid_buf[:, self._grid_fill:self._grid_fill + take] = \
                x[:, pos:pos + take]
            self._grid_fill += take
            if self._grid_fill == self._part:
                self._complete_grid_block(channels)
            pos += take

    def _complete_grid_block(self, channels: int) -> None:
        window = np.concatenate((self._prev_block, self._grid_buf), axis=1)
        spec_x = np.fft.rfft(window, axis=-1)
        self._fdl.append(spec_x)
        self._prev_block = self._grid_buf.copy()
        self._grid_fill = 0
        self._blocks_done += 1
        if not self._tail_specs:
            return
        # W_B = Σ_{i≥1} H_i·X̂_{B-i}（流首缺的老谱按零处理）。
        acc = np.zeros_like(spec_x)
        fdl = self._fdl
        for i, h_spec in enumerate(self._tail_specs, start=1):
            if i > len(fdl):
                break
            acc += self._apply_spec(h_spec, fdl[-i], channels)
        tail = np.fft.irfft(acc, n=self._nfft, axis=-1)[:, self._part:]
        self._pending.append(
            (self._blocks_done, np.ascontiguousarray(tail, dtype=np.float32)))

    def _add_pending_tail(self, out: np.ndarray, channels: int,
                          start_abs: int, n: int) -> None:
        """把网格尾部贡献加进输出（按绝对样本位消费待发块）。"""
        end_abs = start_abs + n
        idx = start_abs
        while idx < end_abs:
            block = idx // self._part
            while self._pending and self._pending[0][0] < block:
                self._pending.popleft()  # 过期保护（正常不发生）
            if self._pending and self._pending[0][0] == block:
                w = self._pending[0][1]
                seg_end = min(end_abs, (block + 1) * self._part)
                lo = idx - block * self._part
                hi = seg_end - block * self._part
                out[:, idx - start_abs:seg_end - start_abs] += w[:, lo:hi]
                if seg_end == (block + 1) * self._part:
                    self._pending.popleft()
                idx = seg_end
            else:
                # 流首块（B=0）无尾部贡献（历史为零）。
                idx = (block + 1) * self._part

    def process(self, x: np.ndarray) -> np.ndarray:
        """x: (channels, n)；返回 (channels, n)，逐样本对齐、状态跨块。"""
        x = np.asarray(x, dtype=np.float32)
        channels = int(x.shape[0])
        n = int(x.shape[-1])
        if self._ir_len <= 1:
            self._reset_stream()
            gain = self._gain
            kch = int(gain.shape[0])
            if kch == 1 or kch == channels:
                scaled = x * gain
            elif kch > channels:
                scaled = x * gain[:channels]
            else:
                scaled = x * np.repeat(
                    gain, channels // kch, axis=0)
            return np.ascontiguousarray(scaled, dtype=np.float32)
        if channels != self._kernel_channels and self._kernel_channels != 1:
            # 声道布局变化超出合同：整流复位（保持因果，不串旧状态）。
            self._reset_stream()
        if self._grid_buf.shape != (channels, self._part):
            # mono kernel 广播 / 首次 stereo 输入：网格状态按输入声道就位。
            self._grid_buf = np.zeros(
                (channels, self._part), dtype=np.float32)
            self._prev_block = np.zeros(
                (channels, self._part), dtype=np.float32)
            self._grid_fill = 0
            self._blocks_done = 0
            self._fdl.clear()
            self._pending.clear()
        out = self._process_head(x, channels, n)
        if self._tail_specs:
            self._feed_grid(x, channels, n)
            self._add_pending_tail(
                out, channels, self._out_pos, n)
        self._out_pos += n
        return np.ascontiguousarray(out, dtype=np.float32)


#: 解码后的 IR 缓存（键含内容指纹；同文件多实例/试听共用，容量有界）。
_IR_CACHE: "OrderedDict[tuple, Tuple[np.ndarray, IrResource]]" = OrderedDict()
_IR_CACHE_MAX = 8
_IR_CACHE_LOCK = threading.Lock()
_IR_WORKERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="topos-ir")


def _cached_ir(path: str, rate: int, true_stereo: bool,
               cancel_check: Optional[Callable[[], bool]] = None
               ) -> Tuple[tuple, np.ndarray, IrResource]:
    """Read and validate on the preparation thread, never the device callback."""
    key = (path, _ir_fingerprint(path), rate, true_stereo)
    with _IR_CACHE_LOCK:
        cached = _IR_CACHE.get(key)
        if cached is not None:
            _IR_CACHE.move_to_end(key)
    if cached is None:
        cached = load_ir(path, rate, cancel_check=cancel_check)
        if key[1] != _ir_fingerprint(path):
            raise ValueError("IR changed while it was being prepared; retry loading")
        with _IR_CACHE_LOCK:
            _IR_CACHE[key] = cached
            while len(_IR_CACHE) > _IR_CACHE_MAX:
                _IR_CACHE.popitem(last=False)
    return key, cached[0], cached[1]


def _prepare_ir(path: str, rate: int, true_stereo: bool,
                previous_key: Optional[tuple], cancelled: threading.Event,
                algorithm_version: Optional[int]
                ) -> tuple:
    key = (path, _ir_fingerprint(path), rate, true_stereo, algorithm_version)
    if key == previous_key:
        return key, None
    try:
        _cache_key, ir, resource = _cached_ir(path, rate, true_stereo, cancelled.is_set)
    except (OSError, ValueError, EOFError, wave.Error) as exc:
        return key, exc
    if cancelled.is_set():
        raise CancelledError()
    prepared = ConvolutionReverbEffect(rate)
    prepared._algorithm_version = algorithm_version
    prepared._install_decoded_ir(ir, true_stereo)
    prepared._resource = resource
    if cancelled.is_set():
        raise CancelledError()
    return key, prepared


def _ir_fingerprint(path: str) -> Tuple[int, int]:
    """廉价内容指纹（mtime_ns + size）：同路径替换 → 键变化 → 重验哈希。"""
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, -1)


class ConvolutionReverbEffect(AudioEffect):
    """卷积混响（mono→stereo / 立体声 / true stereo 四路径）。"""

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._params: dict = {"enabled": True, "wet": 0.35, "dry": 1.0,
                              "true_stereo": False}
        self._algorithm_version: Optional[int] = None
        self._mono_ir: Optional[np.ndarray] = None
        self._stereo_ir: Optional[np.ndarray] = None
        # true stereo：{"LL": ir, "LR": ir, "RL": ir, "RR": ir}
        # （第一字母 = 输出声道；out_L = LL⊛xL + LR⊛xR）。
        self._matrix_ir: Dict[str, np.ndarray] = {}
        self._matrix_convs: Dict[str, PartitionedConvolver] = {}
        self._conv: Optional[PartitionedConvolver] = None
        self._latency = 0
        # ir_path 装载去重（set_params 每块都会调用；同键不重复读盘）。
        # 键含内容指纹：同路径替换后自动重载并重验 SHA-256。
        self._loaded_ir_key: Optional[tuple] = None
        # 确认失败的键：资源指纹变化前不逐块重试（避免每块读盘）。
        self._failed_ir_key: Optional[tuple] = None
        self._resource: Optional[IrResource] = None
        self._async_resources = False
        self._request_key: Optional[tuple] = None
        self._prepare_future: Optional[Future] = None
        self._prepare_cancel = threading.Event()
        self._next_probe = 0.0
        self._resource_status = "empty"
        self._resource_error = ""

    def enable_async_resources(self) -> None:
        """Realtime hosts prepare files and FFT kernels outside the mixer lock."""
        self._async_resources = True

    def release_resources(self) -> None:
        """Invalidate pending work when an instance is removed or replaced."""
        self._prepare_cancel.set()
        if self._prepare_future is not None:
            self._prepare_future.cancel()
        self._prepare_future = None
        self._request_key = None

    def _poll_preparation(self) -> None:
        future = self._prepare_future
        if future is None or not future.done():
            return
        self._prepare_future = None
        try:
            key, prepared = future.result()
        except CancelledError:
            return
        except (OSError, ValueError, EOFError, wave.Error) as exc:
            self._unload_ir()
            self._loaded_ir_key = None
            self._resource_status = "failed"
            self._resource_error = str(exc) or type(exc).__name__
            return
        if prepared is not None:
            if isinstance(prepared, Exception):
                self._unload_ir()
                self._loaded_ir_key = None
                self._failed_ir_key = key
                self._resource_status = "failed"
                self._resource_error = str(prepared) or type(prepared).__name__
                return
            # The worker owns this fresh stream until this atomic block-boundary
            # handoff. Nothing allocates a kernel or reads a file here.
            for name in ("_mono_ir", "_stereo_ir", "_matrix_ir", "_matrix_convs",
                         "_conv", "_latency", "_resource"):
                setattr(self, name, getattr(prepared, name))
        elif key == self._failed_ir_key:
            return
        self._loaded_ir_key = key
        self._failed_ir_key = None
        self._resource_status = "ready"
        self._resource_error = ""

    def _request_preparation(self, path: str, true_stereo: bool) -> None:
        request = (path, int(self._sample_rate), true_stereo, self._algorithm_version)
        if request != self._request_key:
            self.release_resources()
            self._unload_ir()
            self._loaded_ir_key = None
            self._failed_ir_key = None
            self._request_key = request
            self._next_probe = 0.0
        self._poll_preparation()
        now = time.monotonic()
        if self._prepare_future is None and now >= self._next_probe:
            self._prepare_cancel = threading.Event()
            self._prepare_future = _IR_WORKERS.submit(
                _prepare_ir, path, int(self._sample_rate), true_stereo,
                self._loaded_ir_key or self._failed_ir_key,
                self._prepare_cancel, self._algorithm_version)
            self._next_probe = now + 1.0
            if self._loaded_ir_key is None:
                self._resource_status = "preparing"

    def collect_telemetry(self) -> dict:
        return {"resource_status": self._resource_status,
                "resource_error": self._resource_error,
                "ir_samples": self._resource.samples if self._resource else 0,
                "ir_sha256": self._resource.sha256 if self._resource else ""}

    def _unload_ir(self) -> None:
        """停用全部 IR kernel 并清历史（清空/确认失败后的直通状态）。"""
        self._mono_ir = None
        self._stereo_ir = None
        self._matrix_ir = {}
        self._matrix_convs = {}
        self._conv = None
        self._latency = 0
        self._resource = None

    def set_ir(self, ir: np.ndarray, *, true_stereo: bool = False,
               matrix: Optional[Dict[str, np.ndarray]] = None) -> None:
        """装载 IR（mono→stereo 用单 IR；true stereo 用四路径矩阵）。"""
        ir = np.asarray(ir, dtype=np.float32)
        if true_stereo and matrix is not None:
            self._matrix_ir = {key: np.asarray(value, dtype=np.float32)
                               for key, value in matrix.items()}
            self._stereo_ir = None
            self._mono_ir = None
        elif ir.ndim > 1 and ir.shape[0] > 1:
            self._stereo_ir = ir
            self._mono_ir = None
            self._matrix_ir = {}
            self._matrix_convs = {}
        else:
            self._mono_ir = ir.reshape(1, -1)
            self._stereo_ir = None
            self._matrix_ir = {}
            self._matrix_convs = {}
        self._rebuild_conv()

    def _rebuild_conv(self) -> None:
        conv_class = PartitionedConvolver
        if self._algorithm_version is not None and self._algorithm_version <= 4:
            from .legacy_v4.convolution_reverb import PartitionedConvolver as LegacyConvolver
            conv_class = LegacyConvolver
        if self._matrix_ir:
            # 四路径：out_L = LL⊛xL + LR⊛xR；out_R = RL⊛xL + RR⊛xR。
            self._matrix_convs = {
                key: conv_class(np.asarray(kernel, dtype=np.float32))
                for key, kernel in self._matrix_ir.items()}
            self._conv = None
        elif self._stereo_ir is not None:
            self._conv = conv_class(self._stereo_ir)
        elif self._mono_ir is not None:
            self._conv = conv_class(self._mono_ir)
        else:
            self._conv = None
        if self._matrix_convs:
            self._latency = max(c.latency for c in self._matrix_convs.values())
        else:
            self._latency = self._conv.latency if self._conv else 0

    def set_params(self, params: object,
                   sample_rate: Optional[float] = None,
                   algorithm_version: Optional[int] = None) -> None:
        version_changed = self._algorithm_version != algorithm_version
        self._algorithm_version = algorithm_version
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)
        merged = dict(self._params)
        if isinstance(params, dict):
            merged.update(params)
        self._params = merged
        # UI 文件选择/序列化往返：params 携带 ir_path 时按需装载 IR。
        # 键 = (path, 内容指纹, sample_rate, true_stereo)；set_params 在
        # 自动化路径逐块触发，必须保持廉价（stat + 键比较）。
        ir_path = str(merged.get("ir_path", "") or "")
        true_stereo = bool(merged.get("true_stereo", False))
        if self._async_resources:
            if ir_path:
                self._request_preparation(ir_path, true_stereo)
            elif self._request_key is not None:
                self.release_resources()
                self._unload_ir()
                self._loaded_ir_key = None
                self._resource_status = "empty"
                self._resource_error = ""
            return
        if ir_path:
            key = (ir_path, _ir_fingerprint(ir_path),
                   int(self._sample_rate), true_stereo, algorithm_version)
            if key == self._loaded_ir_key:
                return
            if key != self._failed_ir_key:
                self._attempt_ir_load(ir_path, key, true_stereo)
        elif (self._loaded_ir_key is not None
              or self._failed_ir_key is not None):
            # ir_path 被清空 → 音频块边界停用旧 kernel、清历史（直通）。
            self._loaded_ir_key = None
            self._failed_ir_key = None
            self._unload_ir()
            self._resource_status = "empty"
            self._resource_error = ""
        elif version_changed:
            self._rebuild_conv()

    def _attempt_ir_load(self, ir_path: str, key: tuple,
                         true_stereo: bool) -> None:
        try:
            _cache_key, ir, resource = _cached_ir(ir_path, int(self._sample_rate), true_stereo)
        except (OSError, ValueError, EOFError, wave.Error) as exc:
            logger.warning("convolution reverb: IR load failed for %s: %s",
                           ir_path, exc)
            # 确认失败：停用旧 kernel、清历史；同键不再逐块重试。
            self._loaded_ir_key = None
            self._failed_ir_key = key
            self._unload_ir()
            self._resource_status = "failed"
            self._resource_error = str(exc)
            return
        self._failed_ir_key = None
        self._install_decoded_ir(ir, true_stereo)
        self._resource = resource
        self._loaded_ir_key = key
        self._resource_status = "ready"
        self._resource_error = ""

    def _install_decoded_ir(self, ir: np.ndarray, true_stereo: bool) -> None:
        if self._algorithm_version is not None and self._algorithm_version <= 4:
            self.set_ir(ir, true_stereo=true_stereo)
            return
        if true_stereo and ir.shape[0] == 4:
            # 四路 WAV：按文件声道序显式 LL/LR/RL/RR（第一字母=输出）。
            self.set_ir(ir[0:1].copy(), true_stereo=True, matrix={
                "LL": ir[0:1].copy(), "LR": ir[1:2].copy(),
                "RL": ir[2:3].copy(), "RR": ir[3:4].copy()})
        elif true_stereo and ir.shape[0] == 2:
            # 两路 WAV 交叉路径缺数据 → 对角退化（L←ch0，R←ch1），
            # 与普通逐声道立体声一致（策略显式、有测试固化）。
            self.set_ir(ir, true_stereo=False)
        else:
            self.set_ir(ir, true_stereo=true_stereo)

    def reset_state(self) -> None:
        if self._conv is not None:
            self._conv.reset()
        for conv in self._matrix_convs.values():
            conv.reset()

    @property
    def is_active(self) -> bool:
        if self._async_resources:
            path = str(self._params.get("ir_path", "") or "")
            if path:
                self._request_preparation(path, bool(self._params.get("true_stereo", False)))
        return (bool(self._params.get("enabled", True))
                and (self._conv is not None or bool(self._matrix_convs)))

    def latency_samples(self) -> int:
        return self._latency

    def tail_estimate_samples(self) -> int:
        if self._matrix_convs:
            ir_len = max(
                (k.shape[-1] for k in self._matrix_ir.values()), default=0)
            return int(ir_len) + self._latency
        if self._conv is None:
            return 0
        ir = self._stereo_ir if self._stereo_ir is not None else self._mono_ir
        if ir is None:
            return 0
        return int(ir.shape[-1]) + self._latency

    def process(self, samples: np.ndarray) -> np.ndarray:
        if not self.is_active or samples is None or samples.ndim != 2:
            return samples
        n = samples.shape[-1]
        channels = samples.shape[0]
        wet_gain = float(np.clip(self._params.get("wet", 0.35), 0.0, 1.0))
        dry_gain = float(np.clip(self._params.get("dry", 1.0), 0.0, 1.0))
        if self._matrix_convs and channels >= 2:
            # 四路径求和：out_L = LL⊛xL + LR⊛xR；out_R = RL⊛xL + RR⊛xR。
            xl = samples[0:1]
            xr = samples[1:2]
            wet = np.zeros_like(samples)
            for key, target in (("LL", 0), ("LR", 0), ("RL", 1), ("RR", 1)):
                conv = self._matrix_convs.get(key)
                if conv is None:
                    continue
                source = xl if key in ("LL", "RL") else xr
                wet[target] += conv.process(source)[0][:n]
            for ch in range(2, channels):
                wet[ch] = wet[ch % 2]
        else:
            if self._conv is None:
                return samples
            wet = self._conv.process(samples)
            if wet.shape[-1] < n:
                wet = np.pad(wet, ((0, 0), (0, n - wet.shape[-1])))
            wet = wet[:, :n]
        return np.ascontiguousarray(
            samples * dry_gain + wet * wet_gain, dtype=np.float32)
