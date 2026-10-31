"""M21.2（R05）· 交付 QC 测量 producer 回归。

验收：真实合成文件经 ``measure_delivery_quality`` 实测——黑帧计数来自
实际解码（不是默认 0 冒充）；探测失败返回 None（≠通过）；测量结果经
质量门裁决。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.delivery_quality_gate import (
    evaluate_delivery_quality,
)
from src.features.edit.ai_editing.delivery_quality_probe import (
    measure_delivery_quality,
)


def _write_test_video(path: Path, *, frames: int = 10, fps: int = 25,
                      size: tuple[int, int] = (64, 48),
                      black_frames: tuple[int, ...] = (),
                      frozen_from: int | None = None,
                      with_audio: bool = True) -> None:
    import av

    container = av.open(str(path), mode="w")
    vstream = container.add_stream("libx264", rate=fps)
    vstream.width, vstream.height = size
    vstream.pix_fmt = "yuv420p"
    for i in range(frames):
        if i in black_frames:
            arr = np.zeros((*size, 3), dtype=np.uint8)
        elif frozen_from is not None and i >= frozen_from:
            arr = np.full((*size, 3), 128, dtype=np.uint8)
        else:
            arr = np.full((*size, 3), 40 + i * 15, dtype=np.uint8)
        frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
        for packet in vstream.encode(frame):
            container.mux(packet)
    if with_audio:
        astream = container.add_stream("aac", rate=48000)
        astream.layout = "stereo"
        # 0.4 秒 440Hz 音频
        n = int(48000 * 0.4)
        t = np.linspace(0, 0.4, n, endpoint=False)
        sig = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        # fltp 布局期望 (channels, samples) 的二维数组
        arr = np.stack([sig, sig], axis=0)  # (2, n)
        for i in range(0, n, 1024):
            block = np.ascontiguousarray(arr[:, i:i + 1024])
            aframe = av.AudioFrame.from_ndarray(
                block, format="fltp", layout="stereo")
            aframe.sample_rate = 48000
            aframe.pts = None
            for packet in astream.encode(aframe):
                container.mux(packet)
        for packet in astream.encode():
            container.mux(packet)
    for packet in vstream.encode():
        container.mux(packet)
    container.close()


class TestMeasureDeliveryQuality:
    def test_clean_file_measured_zero_black(self, tmp_path):
        out = tmp_path / "clean.mp4"
        _write_test_video(out)
        inp = measure_delivery_quality(out)
        assert inp is not None
        # 实测值（0 = 真实测得零，非默认冒充）
        assert inp.black_frame_count == 0
        assert inp.frozen_frame_count == 0
        assert inp.corrupt_frame_count == 0
        assert inp.audio_present is True
        assert inp.audio_sample_rate == 48000
        assert inp.resolution == (64, 48)
        assert inp.output_hash
        result = evaluate_delivery_quality(inp)
        assert result.passed, result.blocking

    def test_black_frames_measured(self, tmp_path):
        out = tmp_path / "black.mp4"
        _write_test_video(out, black_frames=(2, 3))
        inp = measure_delivery_quality(out)
        assert inp is not None
        assert inp.black_frame_count >= 1  # 编码后黑色帧可能扩展，实测非零
        result = evaluate_delivery_quality(inp)
        assert not result.passed
        assert any("黑帧" in b for b in result.blocking)

    def test_probe_failure_returns_none(self, tmp_path):
        assert measure_delivery_quality(tmp_path / "missing.mp4") is None
        bad = tmp_path / "bad.mp4"
        bad.write_bytes(b"not a video")
        assert measure_delivery_quality(bad) is None

    def test_unmeasured_fields_stay_none_not_fake_zero(self, tmp_path):
        out = tmp_path / "noaudio.mp4"
        _write_test_video(out, with_audio=False)
        inp = measure_delivery_quality(out)
        assert inp is not None
        assert inp.audio_present is False
        # 静音测量未发生（无音频）→ None，不冒充 0
        assert inp.silence_seconds is None
        assert inp.integrated_lufs is None
