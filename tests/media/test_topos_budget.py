"""P1 预算接口测试（画质对齐计划 §5 P1，2026-09-21）。

三级预算优先级 max_file_bytes > max_video_bytes > bitrate > 档位 bpp：

- max_video_bytes：视频流总字节硬上限（ToposRateFeedback 预算模式）；
- max_file_bytes：文件总字节上限（扣除音轨 + 容器预留后给视频）；
- bitrate（bps）：显式覆盖档位 bpp；
- close() 超限 = 定稿失败（硬上限，不静默交付）。

素材：确定性 PRNG 噪声帧（高频 → 逼真码控压力，sized 搜索有实际工作量）。
"""

from __future__ import annotations

import os
import sys
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.shared.codec.color_metadata_config import ColorMetadataConfig  # noqa: E402
from src.shared.codec.topos_binding import ToposCodec, ToposMovieFile  # noqa: E402
from src.shared.export.topos_encoder import ToposVideoEncoder  # noqa: E402

FPS = Fraction(25, 1)
W, H = 320, 180


def _config(**kw):
    base = dict(
        width=W, height=H, fps=FPS, pix_fmt="yuv422p10le",
        profile="proxy", crf=None, has_alpha=False,
        entropy_mode="rans2", aq_mode="off", rdo_mode="off", gop="intra",
        color_metadata=ColorMetadataConfig(),
        max_video_bytes=None, max_file_bytes=None, budget_frames=None,
        bitrate=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _Frame:
    """planar yuv422p10le 帧协议（ToposVideoEncoder 直通域）。"""

    def __init__(self, index: int):
        rng = np.random.default_rng(index)
        cw = (W + 1) // 2
        self.planes = (
            rng.integers(64, 941, size=(H, W), dtype=np.uint16).astype("<u2"),
            rng.integers(64, 961, size=(H, cw), dtype=np.uint16).astype("<u2"),
            rng.integers(64, 961, size=(H, cw), dtype=np.uint16).astype("<u2"),
        )
        self.pixel_format = "yuv422p10le"
        self.width, self.height = W, H
        self.frame_index = index
        self.is_planar = True


def _audio_samples(n: int) -> np.ndarray:
    rng = np.random.default_rng(1234)
    return (rng.uniform(-0.5, 0.5, size=(2, n))).astype(np.float32)


def _encode(tmp_path: Path, frames: int, cfg, audio: bool = False,
            audio_samples: int = 48000) -> Path:
    out = tmp_path / "budget.mov"
    enc = ToposVideoEncoder(str(out), cfg, slice_threads=1)
    enc.open()
    if audio:
        enc.add_audio_stream(codec="pcm_s24le", sample_rate=48000, channels=2)
    for i in range(frames):
        enc.encode_frame(_Frame(i))
    if audio:
        enc.write_audio_samples(_audio_samples(audio_samples), 48000)
    enc.close()
    return out


def _video_stream_bytes(path: Path, codec: ToposCodec) -> int:
    movie = ToposMovieFile(codec, str(path))
    try:
        n = int(movie.info().sample_count)
        return sum(movie.packet_into(i)[1] for i in range(n))
    finally:
        movie.close()


def test_max_video_bytes_hard_cap(tmp_path):
    """视频流字节 ≤ max_video_bytes，且预算模式确实抬高了码率。"""
    frames = 40
    # 预算须高于 qp95 地板（噪声帧 ~22.5KB/帧 @320×180；2026-09-21
    # id4 边缘均衡矩阵后地板略升 ~1.5MB→实测地板 1.5003MB，取 1.55MB
    # 留余量）——地板以下属硬上限正确拒绝域（overflow_fails_close 覆盖）
    cfg = _config(max_video_bytes=1_550_000, budget_frames=frames)
    out = _encode(tmp_path, frames, cfg)
    used = _video_stream_bytes(out, ToposCodec())
    assert used <= 1_550_000
    # 同素材无预算（proxy 档 bpp 0.29108）作对照：预算应高于档位默认
    cfg_ref = _config()
    out_ref = _encode(tmp_path, frames, cfg_ref)
    used_ref = _video_stream_bytes(out_ref, ToposCodec())
    assert used > used_ref, "预算模式应比 proxy 档默认码率用得更多"


def test_max_file_bytes_with_audio(tmp_path):
    """max_file_bytes 扣音轨+容器预留：最终文件字节 ≤ 上限。"""
    frames = 32
    audio_samples = 48000
    # 1.65MB：2026-09-21 边缘均衡矩阵（id4）后 422 视频地板字节略升，
    # 1.6MB 派生的视频预算距 qp 顶格地板仅 ~10B（刀口）。测试意图是
    # "文件上限（扣音轨/容器）被尊重"，给地板留正常余量。
    cfg = _config(max_file_bytes=1_650_000, budget_frames=frames)
    out = _encode(tmp_path, frames, cfg, audio=True,
                  audio_samples=audio_samples)
    assert out.stat().st_size <= 1_650_000


def test_bitrate_overrides_tier_bpp(tmp_path):
    """bitrate（bps）覆盖档位 bpp：均值贴近目标（±25% 容差）。"""
    frames = 40
    target_mbps = 30.0
    cfg = _config(bitrate=int(target_mbps * 1e6))
    out = _encode(tmp_path, frames, cfg)
    used = _video_stream_bytes(out, ToposCodec())
    actual_mbps = used * 8.0 / (frames / 25.0) / 1e6
    assert target_mbps * 0.75 <= actual_mbps <= target_mbps * 1.25


def test_budget_overflow_fails_close(tmp_path):
    """预算远低于 qp95 下限 → close() 拒绝定稿（不产出超限文件）。"""
    frames = 24
    cfg = _config(max_video_bytes=2048, budget_frames=frames)
    out = tmp_path / "overflow.mov"
    enc = ToposVideoEncoder(str(out), cfg, slice_threads=1)
    enc.open()
    for i in range(frames):
        enc.encode_frame(_Frame(i))
    with pytest.raises(RuntimeError, match="预算超限"):
        enc.close()
    assert not out.exists()
    assert not Path(str(out) + ".topos-tmp").exists()


def test_budget_conflicts_rejected(tmp_path):
    """crf / 预算缺 budget_frames / lp 档：构造期显式失败。"""
    with pytest.raises(ValueError, match="互斥"):
        ToposVideoEncoder(str(tmp_path / "a.mov"),
                          _config(crf=40, max_video_bytes=1000,
                                  budget_frames=10))
    with pytest.raises(ValueError, match="budget_frames"):
        ToposVideoEncoder(str(tmp_path / "b.mov"),
                          _config(max_video_bytes=1000))
    with pytest.raises(ValueError, match="互斥"):
        ToposVideoEncoder(str(tmp_path / "c.mov"),
                          _config(bitrate=10_000_000, profile="lp",
                                  gop="ip2"))


def test_no_budget_backward_compatible(tmp_path):
    """无预算字段：走档位 bpp，行为与历史一致（逐帧目标 = tier 定标）。"""
    frames = 12
    out = _encode(tmp_path, frames, _config())
    movie = ToposMovieFile(ToposCodec(), str(out))
    try:
        assert int(movie.info().sample_count) == frames
    finally:
        movie.close()


def test_rate_feedback_budget_mode():
    """ToposRateFeedback 预算模式：目标自适应 + 超限判定。"""
    from src.shared.export.topos_rate_control import ToposRateFeedback

    rc = ToposRateFeedback(1000, total_budget_bytes=10_000, total_frames=10)
    assert rc.target == 1000.0
    rc.seed(50, 1000)
    assert rc.total == 1000
    assert rc.target == int((10_000 - 1000) * rc.BUDGET_TARGET_MARGIN) // 9
    assert not rc.over_budget
    for _ in range(9):
        rc.note(1100)
    assert rc.frames == 10 and rc.total == 10_000 + 900
    assert rc.over_budget
    with pytest.raises(ValueError):
        ToposRateFeedback(1000, total_budget_bytes=100)  # 缺 total_frames
