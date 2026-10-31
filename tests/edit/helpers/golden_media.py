"""F0.3 黄金媒体生成器——精确音频/视频向量，供黄金工程与各功能任务复用。

约定：
- 全部生成器确定性（无随机种子依赖），参数即冻结的验收向量基线。
- 合成媒体写入调用方提供的 tmp 路径；真实镜头一律经 ``tests/real_media.py``
  取材（本模块不重复管理真实素材清单）。
- alpha 梯度不带视频封装（H.264 无 alpha），像素级 alpha 向量用
  ``tests/edit/helpers/frame_fixtures.py`` 的 RGBA 帧函数。
- 音频落盘复用 round7 验证过的 PyAV fltp→pcm_s16le 写法。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import numpy as np

SR: Final[int] = 48000


# ── 精确音频向量 ──────────────────────────────────────────────────────

def sine_pcm(freq: float, seconds: float, amp: float = 0.3, channels: int = 2) -> np.ndarray:
    """等幅正弦，形状 (channels, samples)，float32。所有声道同相。"""
    t = np.arange(int(seconds * SR)) / SR
    sig = (amp * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)
    return np.stack([sig] * channels)


def inverted_phase_stereo_pcm(freq: float, seconds: float, amp: float = 0.3) -> np.ndarray:
    """反相立体声：L=+sin, R=-sin。L+R 逐样本相消为 0（相位对拍基准）。"""
    t = np.arange(int(seconds * SR)) / SR
    sig = (amp * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)
    return np.stack([sig, -sig])


def pulse_train_pcm(pulses_per_sec: float, seconds: float, amp: float = 0.9) -> np.ndarray:
    """单样本脉冲串（mono→stereo 复制）。脉冲间隔精确 = SR/pulses_per_sec 样本。"""
    n = int(seconds * SR)
    sig = np.zeros(n, dtype=np.float32)
    period = SR / pulses_per_sec
    idx = np.arange(0, n, int(round(period)))
    sig[idx] = amp
    return np.stack([sig, sig])


def pcm_channel_sum_is_silent(pcm: np.ndarray, atol: float = 1e-6) -> bool:
    """反相立体声校验：全部声道求和应逐样本近零。"""
    return bool(np.max(np.abs(pcm.sum(axis=0))) <= atol)


def write_wav(path: Path, pcm: np.ndarray, sample_rate: int = SR) -> Path:
    """PCM (ch, n) float32 → wav(pcm_s16le)。"""
    import av

    channels = pcm.shape[0]
    layout = {1: "mono", 2: "stereo"}.get(channels, f"{channels}c")
    container = av.open(str(path), mode="w", format="wav")
    stream = container.add_stream("pcm_s16le", rate=sample_rate)
    stream.layout = layout
    block = sample_rate // 5
    for start in range(0, pcm.shape[1], block):
        chunk = np.clip(pcm[:, start:start + block], -1.0, 1.0)
        frame = av.AudioFrame.from_ndarray(
            chunk.astype(np.float32), format="fltp", layout=layout)
        frame.sample_rate = sample_rate
        for pkt in stream.encode(frame):
            container.mux(pkt)
    for pkt in stream.encode(None):
        container.mux(pkt)
    container.close()
    return path


def dominant_freq(pcm: np.ndarray, sample_rate: int = SR) -> float:
    """FFT 主频（校验读回频率不变）。"""
    sig = pcm[0] if pcm.ndim == 2 else pcm
    spec = np.abs(np.fft.rfft(sig * np.hanning(sig.shape[0])))
    return float(np.fft.rfftfreq(sig.shape[0], 1.0 / sample_rate)[np.argmax(spec)])


# ── 精确视频向量 ──────────────────────────────────────────────────────

def gradient_rgb_frame(width: int, height: int, frame_idx: int) -> np.ndarray:
    """角锚定 RGB 双线性渐变 + 帧序号游标。

    R 沿 x、G 沿 y、B = 固定 1/3；左上角一个 4px 游标块的 x 位置随
    frame_idx 移动，用于区分帧。返回 uint8 (h, w, 3) RGB。
    """
    xs = np.linspace(0, 255, width, dtype=np.float32)
    ys = np.linspace(0, 255, height, dtype=np.float32)
    r = np.tile(xs, (height, 1))
    g = np.tile(ys[:, None], (1, width))
    b = np.full((height, width), 255.0 / 3.0)
    frame = np.stack([r, g, b], axis=-1).astype(np.uint8)
    marker_x = int((frame_idx * max(width - 8, 1)) / 64.0) % max(width - 4, 1)
    frame[0:4, marker_x:marker_x + 4] = (255, 255, 255)
    return frame


def write_gradient_video(
    path: Path,
    width: int = 128,
    height: int = 128,
    frames: int = 48,
    fps_num: int = 24,
    fps_den: int = 1,
) -> Path:
    """渐变视频 → mp4(libx264, crf10, yuv420p)。支持有理数帧率（混 fps 黄金素材）。"""
    import av
    from fractions import Fraction

    container = av.open(str(path), mode="w", format="mp4")
    stream = container.add_stream("libx264", rate=Fraction(fps_num, fps_den))
    stream.width = width
    stream.height = height
    stream.pix_fmt = "yuv420p"
    stream.options = {"crf": "10"}
    for idx in range(frames):
        rgb = gradient_rgb_frame(width, height, idx)
        frame = av.VideoFrame.from_ndarray(rgb, format="rgb24")
        for pkt in stream.encode(frame):
            container.mux(pkt)
    for pkt in stream.encode(None):
        container.mux(pkt)
    container.close()
    return path
