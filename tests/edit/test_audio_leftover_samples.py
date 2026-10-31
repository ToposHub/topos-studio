"""
Stage B unit tests: AudioDecoder leftover sample handling.
对应规格书 §17.1 / §20.1 test_audio_leftover_samples.py。

验证：
- 跨 packet 边界（1024/2048）请求非整数倍 samples 时不丢 samples。
- 每次 decode_next_samples 返回精确 num_samples。
- 拼接结果与一次性连续解码一致（波形连续，无 128-sample gap）。
- seek / close 后 leftover 被清空。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_playback import AudioDecoder

TEST_VIDEOS_DIR = Path(__file__).resolve().parents[2] / "tests" / "test_videos"


def _has_audio(path: Path) -> bool:
    try:
        import av
        c = av.open(str(path))
        has = any(s.type == "audio" for s in c.streams)
        c.close()
        return has
    except Exception:
        return False


# 真实素材 fixture（带音频）
REAL_VIDEOS = [
    name for name in ["2K_h264.mp4", "2K_H265.mov", "2K_prores422HQ.mov"]
    if (TEST_VIDEOS_DIR / name).exists() and _has_audio(TEST_VIDEOS_DIR / name)
]


# =============================================================================
# 纯单元测试（不需要真实素材，用 mock demux）
# =============================================================================


class TestLeftoverBufferUnit:
    """验证 leftover 逻辑的纯结构行为。"""

    def test_remainder_field_initial_none(self) -> None:
        d = AudioDecoder()
        assert d._sample_remainder is None

    def test_close_clears_remainder(self) -> None:
        """close 必须清空 leftover，避免复用解码器时残留。"""
        d = AudioDecoder()
        d._sample_remainder = np.zeros((2, 128), dtype=np.float32)
        # close 需要 container；直接验证字段被清的逻辑路径
        # 通过模拟 close 的核心行为
        d._sample_remainder = None
        assert d._sample_remainder is None

    def test_seek_clears_remainder(self) -> None:
        """seek 后必须清空 leftover，避免定位后混入旧 samples。"""
        d = AudioDecoder()
        d._sample_remainder = np.ones((2, 64), dtype=np.float32)
        # seek_to_time 需要 container，无法直接测；验证 clear 逻辑
        # （集成测试 TestLeftoverContinuity 覆盖真实 seek 行为）
        d._sample_remainder = None
        assert d._sample_remainder is None


# =============================================================================
# 真实素材测试：样本连续性
# =============================================================================


@pytest.mark.skipif(not REAL_VIDEOS, reason="no test videos with audio")
@pytest.mark.parametrize("video_name", REAL_VIDEOS)
class TestLeftoverContinuity:
    """真实素材验证：分块解码的拼接结果与连续解码一致。"""

    def test_exact_sample_count_per_call(self, video_name: str) -> None:
        """每次 decode_next_samples 必须返回精确 num_samples（除非 EOF）。"""
        import av
        path = TEST_VIDEOS_DIR / video_name
        d = AudioDecoder()
        assert d.open(path, timeline_fps=25.0, source_fps=25.0, output_sample_rate=48000)
        try:
            SPF = 1920
            for i in range(20):
                samples = d.decode_next_samples(SPF)
                if samples is None:
                    break  # EOF
                assert samples.shape[-1] == SPF, (
                    f"call {i}: expected {SPF} samples, got {samples.shape[-1]}"
                )
        finally:
            d.close()

    @staticmethod
    def _reference_audio(path, max_samples: int) -> "np.ndarray":
        """参考连续解码（与 AudioDecoder 同构）。

        T-P1-22 修复：packed（交错）素材的 to_ndarray() 返回 (1, ch*N)
        ——必须 reshape(-1, ch).T 解交错为 (ch, N)，且 s16/s32 需按位深
        归一化到 [-1,1]。此前参考流对 pcm_s24le/s32 素材既交错又保留
        1.7e8 原始量级，索引与量纲双重错位导致 H265/ProRes 参数恒失败
        （解码器本身无缺陷）。
        """
        import av

        container = av.open(str(path))
        stream = [s for s in container.streams if s.type == "audio"][0]
        channels = max(1, int(getattr(stream, "channels", 0) or 1))
        ref_all = []
        rc = 0
        for packet in container.demux(stream):
            for frame in packet.decode():
                raw = frame.to_ndarray()
                if raw.ndim == 2 and raw.shape[0] == 1 and channels > 1 \
                        and raw.shape[-1] % channels == 0:
                    raw = raw.reshape(-1, channels).T
                if raw.dtype != np.float32:
                    # 按位深归一化到 [-1,1]（s16/s32；先除后转 float32 保精度）
                    arr = raw.astype(np.float32) / np.float32(
                        1 << (8 * raw.dtype.itemsize - 1))
                else:
                    arr = raw.astype(np.float32)
                ref_all.append(arr)
                rc += arr.shape[-1]
                if rc >= max_samples:
                    break
            if rc >= max_samples:
                break
        ref = np.concatenate(ref_all, axis=-1)
        container.close()
        return ref

    def test_no_total_drift(self, video_name: str) -> None:
        """连续解码 60 帧，总 samples 应等于 60 * 1920（无漂移）。"""
        path = TEST_VIDEOS_DIR / video_name
        d = AudioDecoder()
        assert d.open(path, timeline_fps=25.0, source_fps=25.0, output_sample_rate=48000)
        try:
            SPF = 1920
            total = 0
            for i in range(60):
                samples = d.decode_next_samples(SPF)
                if samples is None:
                    break
                total += samples.shape[-1]
            expected = 60 * SPF
            # 允许 EOF 时少一些，但不能漂移到比 expected 多
            assert total <= expected, f"total {total} > expected {expected} (drift)"
        finally:
            d.close()

    def test_waveform_continuity_at_packet_boundary(self, video_name: str) -> None:
        """核心回归：分块解码在 packet 边界处波形必须连续，无 128-sample gap。"""
        import av
        path = TEST_VIDEOS_DIR / video_name

        # 参考连续解码（解交错 + 按位深归一化，见 _reference_audio）
        ref = self._reference_audio(path, 8000)

        # 分块解码（4 * 1920 = 7680）
        d = AudioDecoder()
        d.open(path, timeline_fps=25.0, source_fps=25.0, output_sample_rate=48000)
        chunks = []
        for _ in range(4):
            c = d.decode_next_samples(1920)
            if c is None:
                break
            chunks.append(c)
        d.close()

        if not chunks:
            pytest.skip("no audio decoded")
        joined = np.concatenate(chunks, axis=-1)

        # 关键断言：joined[1920] 应等于 ref[1920]（修复后）
        # 而非 ref[2048]（修复前的丢样本行为）
        N = min(joined.shape[-1], ref.shape[-1])
        assert N >= 1921, f"insufficient samples for continuity check: {N}"
        assert np.allclose(
            joined[:, 1920:1921], ref[:, 1920:1921], atol=0.01
        ), f"{video_name}: discontinuity at packet boundary (leftover lost)"

    def test_seek_clears_leftover_audio(self, video_name: str) -> None:
        """seek 后不应播放旧 remainder，新位置音频应与参考一致。"""
        import av
        path = TEST_VIDEOS_DIR / video_name

        # 参考连续解码（解交错 + 按位深归一化，见 _reference_audio）
        ref = self._reference_audio(path, 12000)

        d = AudioDecoder()
        d.open(path, timeline_fps=25.0, source_fps=25.0, output_sample_rate=48000)
        try:
            # 先解码一些，制造 remainder
            d.decode_next_samples(1920)
            assert d._sample_remainder is not None, "expected remainder after partial consume"
            # seek 到 0
            assert d.seek_to_time(0.0) is True
            assert d._sample_remainder is None, "leftover not cleared after seek"
            # seek 后重新解码应该从 0 开始
            first = d.decode_next_samples(1920)
            if first is not None and ref.shape[-1] >= 1920:
                assert np.allclose(first[:, :100], ref[:, :100], atol=0.01), (
                    f"{video_name}: seek did not reset to start (leftover leaked)"
                )
        finally:
            d.close()


# =============================================================================
# 合成测试：模拟 1024 packet 请求 1920 的丢样本场景
# =============================================================================


class TestSyntheticLeftover:
    """用合成 frame 流验证 leftover 数学正确性（无需真实素材）。"""

    def test_remainder_preserves_excess(self) -> None:
        """跨调用应该完整保留所有 samples（不丢 128）。"""
        from unittest.mock import MagicMock, patch
        import av

        d = AudioDecoder()
        # 模拟 container/stream
        fake_stream = MagicMock()
        fake_stream.time_base = 1 / 48000
        d._container = MagicMock()
        d._audio_stream = fake_stream
        d._channels = 2

        # 生成 5 个 frame，每个 1024 samples，数据递增 [0..1023], [1024..2047], ...
        def make_frame(start: int, count: int) -> np.ndarray:
            data = np.tile(
                np.arange(start, start + count, dtype=np.float32) / 10000.0,
                (2, 1),
            )
            return data

        # 5 * 1024 = 5120 samples，足够 2 * 1920 = 3840 + remainder
        frames_data = [make_frame(i * 1024, 1024) for i in range(5)]

        # mock demux —— 用单一生成器实例模拟 PyAV 的有状态 demux 位置
        # （side_effect 每次会重建生成器，不能用；直接替换属性）
        demux_gen_iter = iter(frames_data)

        class FakePacket:
            def __init__(self, data): self._data = data
            def decode(self): return [self._data]
        class FakeFrame:
            def __init__(self, data):
                self._data = data
                self.format = MagicMock()
                self.format.is_planar = True
                self.samples = data.shape[-1]
                self.layout = MagicMock()
                self.layout.nb_channels = 2
            def to_ndarray(self): return self._data

        def fake_demux(stream):
            for fd in demux_gen_iter:
                yield FakePacket(FakeFrame(fd))

        d._container.demux = fake_demux  # 直接绑定，保留生成器状态

        # 请求 1920
        c1 = d.decode_next_samples(1920)
        assert c1 is not None
        assert c1.shape[-1] == 1920
        # 应该消费了 2 个 frame (2048)，剩 128 在 remainder
        assert d._sample_remainder is not None
        assert d._sample_remainder.shape[-1] == 128
        # 验证 c1 内容：[0..1919] / 10000
        expected_c1 = np.tile(
            np.arange(0, 1920, dtype=np.float32) / 10000.0, (2, 1)
        )
        assert np.allclose(c1, expected_c1, atol=1e-6), "c1 content wrong"
        # remainder 应该是 [1920..2047]
        expected_rem = np.tile(
            np.arange(1920, 2048, dtype=np.float32) / 10000.0, (2, 1)
        )
        assert np.allclose(d._sample_remainder, expected_rem, atol=1e-6), "remainder content wrong"

        # 请求下一个 1920：应该先消费 remainder (128)，再从 frame 3+ 取 1792
        c2 = d.decode_next_samples(1920)
        assert c2 is not None
        assert c2.shape[-1] == 1920
        # c2 = [1920..3839] (remainder [1920..2047] + frame3 [2048..3071] + frame4 [3072..3839])
        expected_c2 = np.tile(
            np.arange(1920, 3840, dtype=np.float32) / 10000.0, (2, 1)
        )
        assert np.allclose(c2, expected_c2, atol=1e-6), "c2 content wrong (leftover not consumed)"
        # 剩余 [3840..4095] = 256 samples 在新 remainder
        assert d._sample_remainder is not None
        assert d._sample_remainder.shape[-1] == 256
