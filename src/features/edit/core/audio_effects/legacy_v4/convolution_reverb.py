# Frozen compatibility implementation from 1414562c^:src/features/edit/core/audio_effects/convolution_reverb.py.
# Keep historical PCM behavior; current DSP lives in the parent package.
"""P1 · Convolution Reverb（2026-11-19 计划第三阶段 · 效果器接入 #6）。

- IR 资源登记：``IrResource``（URI + SHA-256 指纹 + 域信息）；装载统一
  重采样（会话率）、首尾静音裁切与峰值规范化；
- 分区 FFT 卷积（overlap-save；IR 长于单窗时分区级联），端点加窗
  （IR 首尾短淡入淡出防爆音）；
- 声道矩阵：mono IR → 各声道同卷积（mono→stereo）；立体声 IR 逐声道；
  true stereo 四路径（L→L/L→R/R→L/R→R 各自 IR，规格显式）；
- 延迟与尾长精确报告（``latency_samples`` 接 PDC；tail = IR 长 +
  分区延迟）——单位脉冲 IR 逐样本复现 IR（回归锚点）。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

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


def _sha256_of(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
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
) -> Tuple[np.ndarray, IrResource]:
    """装载 IR：指纹校验域 + 重采样 + 裁切 + 规范化 + 端点加窗。

    返回 (ir planar float (channels, n), resource)。
    """
    import wave

    with wave.open(path, "rb") as wav:
        ir_rate = wav.getframerate()
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        frames = wav.getnframes()
        raw = wav.readframes(frames)
    if width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported IR sample width: {width}")
    data = data.reshape(-1, channels).T  # (channels, n)

    # 统一重采样到会话率。
    if ir_rate != sample_rate:
        if data.shape[1] > 8:
            data = np.stack([
                resample_poly(data[ch], sample_rate, ir_rate)
                for ch in range(data.shape[0])]).astype(np.float32)
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
        uri=path, sha256=_sha256_of(path), sample_rate=ir_rate,
        channels=channels, samples=int(data.shape[1]))
    return np.ascontiguousarray(data, dtype=np.float32), resource


class PartitionedConvolver:
    """分区 FFT 卷积（overlap-save；状态跨块）。"""

    def __init__(self, ir: np.ndarray, block: int = CONV_BLOCK) -> None:
        self._block = int(block)
        self._ir = np.asarray(ir, dtype=np.float32)
        ir_len = self._ir.shape[-1]
        # 分区大小：每段与块卷积的 FFT 长度。
        self._fft = _next_pow2(self._block + ir_len - 1) if ir_len > 1 else \
            _next_pow2(self._block)
        self._history = np.zeros(
            (self._ir.shape[0] if self._ir.ndim > 1 else 1,
             max(0, self._fft - self._block)),
            dtype=np.float32)
        self._irs = self._ir.reshape(
            (self._ir.shape[0] if self._ir.ndim > 1 else 1, -1))

    @property
    def latency(self) -> int:
        """分区卷积的算法延迟（overlap-save 的历史窗长）。"""
        return max(0, self._fft - self._block)

    def process(self, x: np.ndarray) -> np.ndarray:
        """x: (channels, n)（单声道 IR 时各声道独立同卷积）。

        标准 overlap-save：每个输出块取 [历史 | 当前块] 的 fft 窗，
        卷积后取窗尾 ``block`` 样本（因果——输出只依赖当前与过去输入）。
        """
        channels = x.shape[0]
        n = x.shape[-1]
        ir_len = self._irs.shape[-1]
        if ir_len <= 1:
            gain = float(self._irs.reshape(-1)[0]) if self._irs.size else 0.0
            self._history = np.zeros((channels, 0), dtype=np.float32)
            return x * gain

        kernels = (np.repeat(self._irs, channels, axis=0)
                   if self._irs.shape[0] == 1 else self._irs)
        if kernels.shape[0] < channels:
            kernels = np.repeat(
                kernels, channels // kernels.shape[0], axis=0)
        hist_len = max(0, self._fft - self._block)
        hist = (self._history
                if self._history.shape == (channels, hist_len)
                else np.zeros((channels, hist_len), dtype=np.float32))
        stream = np.concatenate([hist, x], axis=1)
        spec_ir = np.fft.rfft(kernels, n=self._fft, axis=-1)

        out = np.zeros((channels, n), dtype=np.float32)
        produced = 0
        while produced < n:
            take = min(self._block, n - produced)
            end_stream = hist_len + produced + take
            window = stream[:, max(0, end_stream - self._fft):end_stream]
            if window.shape[1] < self._fft:
                window = np.pad(
                    window, ((0, 0), (self._fft - window.shape[1], 0)))
            conv = np.fft.irfft(
                np.fft.rfft(window, axis=-1) * spec_ir,
                n=self._fft, axis=-1)
            out[:, produced:produced + take] = conv[:, -take:]
            produced += take
        # 新历史 = 全流 [旧历史 | 本块] 的最后 hist_len 样本（不足补零）。
        if hist_len > 0:
            if stream.shape[1] >= hist_len:
                self._history = np.ascontiguousarray(
                    stream[:, -hist_len:])
            else:
                self._history = np.ascontiguousarray(np.pad(
                    stream, ((0, 0), (hist_len - stream.shape[1], 0))))
        else:
            self._history = np.zeros((channels, 0), dtype=np.float32)
        return out

    def reset(self) -> None:
        self._history = np.zeros_like(self._history)


class ConvolutionReverbEffect(AudioEffect):
    """卷积混响（mono→stereo / 立体声 / true stereo 四路径）。"""

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._params: dict = {"enabled": True, "wet": 0.35, "dry": 1.0,
                              "true_stereo": False}
        self._mono_ir: Optional[np.ndarray] = None
        self._stereo_ir: Optional[np.ndarray] = None
        # true stereo：{"LL": ir, "LR": ir, "RL": ir, "RR": ir}
        self._matrix_ir: Dict[str, np.ndarray] = {}
        self._matrix_convs: Dict[str, PartitionedConvolver] = {}
        self._conv: Optional[PartitionedConvolver] = None
        self._latency = 0
        # ir_path 装载去重（set_params 每块都会调用；同路径不重复读盘）
        self._loaded_ir_key: Optional[tuple] = None

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
        if self._matrix_ir:
            # 四路径：out_L = LL⊛xL + LR⊛xR；out_R = RL⊛xL + RR⊛xR。
            self._matrix_convs = {
                key: PartitionedConvolver(np.asarray(kernel, dtype=np.float32))
                for key, kernel in self._matrix_ir.items()}
            self._conv = None
        elif self._stereo_ir is not None:
            self._conv = PartitionedConvolver(self._stereo_ir)
        elif self._mono_ir is not None:
            self._conv = PartitionedConvolver(self._mono_ir)
        else:
            self._conv = None
        if self._matrix_convs:
            self._latency = max(c.latency for c in self._matrix_convs.values())
        else:
            self._latency = self._conv.latency if self._conv else 0

    def set_params(self, params: object,
                   sample_rate: Optional[float] = None,
                   algorithm_version: Optional[int] = None) -> None:
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)
        merged = dict(self._params)
        if isinstance(params, dict):
            merged.update(params)
        self._params = merged
        # UI 文件选择/序列化往返：params 携带 ir_path 时按需装载 IR。
        # 同 (path, sample_rate, true_stereo) 只读盘一次（set_params 在
        # 自动化路径逐块触发，必须保持廉价）。
        ir_path = str(merged.get("ir_path", "") or "")
        if ir_path:
            key = (ir_path, self._sample_rate,
                   bool(merged.get("true_stereo", False)))
            if key != self._loaded_ir_key:
                try:
                    ir, _resource = load_ir(ir_path, int(self._sample_rate))
                    self.set_ir(ir, true_stereo=bool(
                        merged.get("true_stereo", False)))
                    self._loaded_ir_key = key
                except Exception as exc:  # noqa: BLE001 — 坏 IR 不阻断混音
                    logger.warning("convolution reverb: IR load failed for %s: %s",
                                   ir_path, exc)
                    self._loaded_ir_key = None
        elif self._loaded_ir_key is not None:
            # ir_path 被清空 → 卸载 IR（is_active 回落到 False）
            self._loaded_ir_key = None

    def reset_state(self) -> None:
        if self._conv is not None:
            self._conv.reset()
        for conv in self._matrix_convs.values():
            conv.reset()

    @property
    def is_active(self) -> bool:
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
