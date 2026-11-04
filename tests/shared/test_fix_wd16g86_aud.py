"""wd16-g86：计划表批 D（video_encoder / video_export_worker 域 Major）
6 条条目的根因修复测试。

覆盖：AUD-33505 / 33506 / 33507 / 33508 / 33509（docs/audit/2026-09-28/
batches/batch-335-shared-export.md）与 AUD-33600（batch-336-shared-export.md）。

- 编码器行为用真实 PyAV（本 venv 17.0.1）验证；close() 异常路径用
  MagicMock('av') 隔离注入故障。
- VideoExporter.cleanup 用例依赖 tests/conftest.py 的 qapp
  （QT_QPA_PLATFORM=offscreen）。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# 必须在创建 QApplication 之前设置 offscreen 平台
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import pytest

av = pytest.importorskip("av", reason="需要真实 PyAV 验证编码器行为")

from src.shared.export.video_encoder import FFmpegEncoderConfig, FFmpegVideoEncoder
from src.shared.media.frame import DecodedFrame
from src.shared.media.metadata import Rational


# =============================================================================
# 公共构件
# =============================================================================

def _tiny_config(**overrides: object) -> FFmpegEncoderConfig:
    """64x64 h264 软编配置（避开 x265 10-bit kill-switch 环境基线）。"""
    kwargs: dict = dict(
        codec="h264", width=64, height=64, fps=Rational(24, 1),
        pix_fmt="yuv420p", crf=0, preset="ultrafast", gop_size=48,
        threads=0,
    )
    kwargs.update(overrides)
    return FFmpegEncoderConfig(**kwargs)


def _rgb_frame(index: int, w: int = 64, h: int = 64,
               channels: int = 3, dtype=np.uint8) -> DecodedFrame:
    fmt = {3: "rgb24", 4: "rgba"}[channels]
    return DecodedFrame(
        frame_index=index, pts=index, time_seconds=index / 24.0,
        data=np.full((h, w, channels), 64, dtype=dtype),
        pixel_format=fmt, width=w, height=h, dtype=dtype,
    )


@pytest.fixture
def out_path(tmp_path):
    """临时输出路径（tmp_path 随测试清理）。"""
    return str(tmp_path / "out.mp4")


# =============================================================================
# AUD-33505：音频码率选项键 'b:a' → 'b'
# =============================================================================

class TestAud33505AudioBitrateOptionKey:
    def test_bitrate_uses_avcodec_canonical_key(self, out_path: str) -> None:
        """stream.options 必须用 'b'（AVOption 正名），'b:a' 会被静默忽略。"""
        enc = FFmpegVideoEncoder(out_path, _tiny_config())
        enc.open()
        try:
            enc.add_audio_stream(codec="aac", sample_rate=48000,
                                 channels=2, bitrate=192)
            assert enc._audio_stream.options == {"b": "192k"}
            assert "b:a" not in enc._audio_stream.options
        finally:
            enc.close()

    def test_no_bitrate_leaves_options_unset(self, out_path: str) -> None:
        """bitrate=None 时不应写入任何 options。"""
        enc = FFmpegVideoEncoder(out_path, _tiny_config())
        enc.open()
        try:
            enc.add_audio_stream(codec="aac", sample_rate=48000, channels=2)
            assert not enc._audio_stream.options
        finally:
            enc.close()


# =============================================================================
# AUD-33506：close() 异常路径（容器显式关闭 + 半成品清理）
# =============================================================================

class TestAud33506CloseExceptionPath:
    @patch.dict("sys.modules", {"av": MagicMock()})
    def test_close_failure_closes_container_and_removes_partial(
        self, out_path: str,
    ) -> None:
        """flush 失败：best-effort 关闭容器 + 删除半成品 + 状态复位。"""
        mock_av = sys.modules["av"]
        mock_container = MagicMock()
        mock_stream = MagicMock()
        mock_stream.encode.side_effect = RuntimeError("flush boom")
        mock_container.add_stream.return_value = mock_stream
        mock_av.open.return_value = mock_container

        # 磁盘上预置旧成功成片（最终路径）与半成品（临时写路径）。
        # M21.1（R04）发布事务：close 失败只清 .topos-partial 临时文件，
        # 旧成功输出保持不动——发布以 close 成功为准。
        with open(out_path, "wb") as f:
            f.write(b"previous-good-output")
        partial_path = out_path + ".topos-partial"
        with open(partial_path, "wb") as f:
            f.write(b"partial-mux-data")

        enc = FFmpegVideoEncoder(out_path, _tiny_config())
        enc.open()
        assert enc.is_open

        with pytest.raises(RuntimeError, match="flush boom"):
            enc.close()

        # (1) 容器被显式 best-effort 关闭（try 内 close 未到达，异常路径补上）
        assert mock_container.close.call_count == 1
        # (2) 半成品（临时写路径）被删除；旧成功成片保持原样
        assert not os.path.exists(partial_path)
        with open(out_path, "rb") as f:
            assert f.read() == b"previous-good-output"
        # (3) 状态复位：引用清空、可安全丢弃
        assert enc._container is None and enc._stream is None
        assert enc._audio_stream is None
        assert not enc.is_open

    def test_close_success_keeps_output(self, out_path: str) -> None:
        """正常路径不受影响：文件保留、计数完整。"""
        enc = FFmpegVideoEncoder(out_path, _tiny_config())
        enc.open()
        for i in range(2):
            enc.encode_frame(_rgb_frame(i))
        enc.close()
        assert os.path.exists(out_path)
        assert enc.frame_count == 2
        assert not enc.is_open


# =============================================================================
# AUD-33507：RGB 序 16-bit 4 通道 packed 帧 → rgba64le
# =============================================================================

class TestAud33507RgbHighDepthFourChannel:
    def test_rgba_uint16_rgb_order_converts(self, out_path: str) -> None:
        """修复前：uint16 4ch 落入 uint8 'rgba' → ValueError；修复后走 rgba64le。"""
        enc = FFmpegVideoEncoder(out_path, _tiny_config())
        enc.open()
        try:
            frame = _rgb_frame(0, channels=4, dtype=np.uint16)
            av_frame = enc._convert_packed_frame(frame, "yuv420p")
            assert av_frame is not None
            assert av_frame.format.name == "yuv420p"
        finally:
            enc.close()

    def test_rgb48_three_channel_high_depth_still_works(self, out_path: str) -> None:
        """3 通道高位深 rgb48le 路径保持不变。"""
        enc = FFmpegVideoEncoder(out_path, _tiny_config())
        enc.open()
        try:
            frame = _rgb_frame(0, channels=3, dtype=np.uint16)
            av_frame = enc._convert_packed_frame(frame, "yuv420p")
            assert av_frame.format.name == "yuv420p"
        finally:
            enc.close()

    def test_bgra_uint16_bgr_branch_regression(self, out_path: str) -> None:
        """BGR 序 4ch 高位深（对照分支）回归不破坏。"""
        enc = FFmpegVideoEncoder(out_path, _tiny_config())
        enc.open()
        try:
            frame = DecodedFrame(
                frame_index=0, pts=0, time_seconds=0.0,
                data=np.full((64, 64, 4), 1000, dtype=np.uint16),
                pixel_format="bgra", width=64, height=64, dtype=np.uint16,
            )
            av_frame = enc._convert_packed_frame(frame, "yuv420p")
            assert av_frame.format.name == "yuv420p"
        finally:
            enc.close()


# =============================================================================
# AUD-33508：_converted_map 输出侧背压 + close 排空判空入锁
# =============================================================================

class _TrackingDict(dict):
    """记录插入后 map 最大长度的 dict（观察输出侧背压上界）。"""

    def __init__(self) -> None:
        super().__init__()
        self.max_size: int = 0

    def __setitem__(self, key: int, value: object) -> None:
        super().__setitem__(key, value)
        if len(self) > self.max_size:
            self.max_size = len(self)


class TestAud33508ConvertedMapBackpressure:
    def test_map_capacity_equals_workers(self, out_path: str) -> None:
        enc = FFmpegVideoEncoder(
            out_path, _tiny_config(
                pipelined_conversion=True, convert_workers=3,
            )
        )
        assert enc._converted_map is not None
        assert enc._converted_map_capacity == 3

    def test_map_bounded_slow_first_frame_no_deadlock(self, out_path: str) -> None:
        """慢首帧 + 快后续帧：map 不超 capacity+1（next 帧旁路），无死锁，
        帧数完整——修复前 map 可积压全部已完成帧且无上界。"""
        workers = 3
        n = 16
        enc = FFmpegVideoEncoder(
            out_path, _tiny_config(
                pipelined_conversion=True, convert_workers=workers,
            )
        )
        tracking = _TrackingDict()
        enc._converted_map = tracking  # 线程惰性启动前替换，直接生效

        real_create = enc._create_av_frame

        def slow_first_frame(frame: DecodedFrame):
            if frame.frame_index == 0:
                time.sleep(0.2)  # 制造完成序倾斜（首帧最慢）
            return real_create(frame)

        enc._create_av_frame = slow_first_frame

        enc.open()
        try:
            for i in range(n):
                enc.encode_frame(_rgb_frame(i))
            enc.close()
            # 完整性：不丢帧（若背压实现死锁，close 的 join/wait 将拖垮此断言）
            assert enc.frame_count == n
            # 上界：capacity + 1（编码器正等待的 next 帧旁路）
            assert tracking.max_size <= workers + 1, tracking.max_size
        finally:
            if enc.is_open:
                enc.close()

    def test_multi_worker_ordering_preserved(self, out_path: str) -> None:
        """多 worker 下 pts 序重排语义保持（乱序完成 → 按序编码）。"""
        n = 12
        enc = FFmpegVideoEncoder(
            out_path, _tiny_config(
                pipelined_conversion=True, convert_workers=3,
            )
        )
        enc.open()
        try:
            for i in range(n):
                # 亮度随 pts 递增，回读校验重排正确性
                data = np.full((64, 64, 3), i * 15, dtype=np.uint8)
                frame = DecodedFrame(
                    frame_index=i, pts=i, time_seconds=i / 24.0,
                    data=data, pixel_format="rgb24", width=64, height=64,
                    dtype=np.uint8,
                )
                enc.encode_frame(frame)
            enc.close()
            assert enc.frame_count == n
            with av.open(out_path) as container:
                means = [
                    float(f.to_ndarray().mean())
                    for f in container.decode(video=0)
                ]
            assert len(means) == n
            assert all(b >= a - 2 for a, b in zip(means, means[1:])), means
        finally:
            if enc.is_open:
                enc.close()


# =============================================================================
# AUD-33509：音频声道布局单一真相 + dtype 契约
# =============================================================================

class TestAud33509ChannelLayoutSingleSource:
    def test_six_channels_stream_and_frame_share_layout(self, out_path: str) -> None:
        """6ch：流/帧同用 '5.1'（修复前帧侧为 '6c'）。端到端回读 6 声道。

        音频流须在首帧 mux 前声明（与 timeline_export 生产时序一致——
        ffmpeg 不允许输出开始后新增流）。"""
        enc = FFmpegVideoEncoder(out_path, _tiny_config())
        enc.open()
        try:
            enc.add_audio_stream(codec="aac", sample_rate=48000, channels=6)
            assert enc._audio_layout_name == "5.1"
            for i in range(2):
                enc.encode_frame(_rgb_frame(i))
            pcm = np.zeros((6, 2048), dtype=np.float32)
            enc.write_audio_samples(pcm, sample_rate=48000)
        finally:
            enc.close()

        with av.open(out_path) as container:
            audio_streams = container.streams.audio
            assert audio_streams, "输出应含音轨"
            assert int(audio_streams[0].codec_context.layout.nb_channels) == 6

    def test_nonstandard_channel_count_raises(self, out_path: str) -> None:
        """3ch 非标准声道数显式报错，而非静默降 mono（调用方按需降级）。"""
        enc = FFmpegVideoEncoder(out_path, _tiny_config())
        enc.open()
        try:
            with pytest.raises(ValueError, match="[Uu]nknown channel count"):
                enc.add_audio_stream(codec="aac", sample_rate=48000, channels=3)
        finally:
            enc.close()

    def test_stream_frame_channel_mismatch_raises(self, out_path: str) -> None:
        """帧声道数与流布局不符时显式报错（而非构造失配帧）。"""
        enc = FFmpegVideoEncoder(out_path, _tiny_config())
        enc.open()
        try:
            enc.add_audio_stream(codec="aac", sample_rate=48000, channels=2)
            pcm = np.zeros((6, 512), dtype=np.float32)
            with pytest.raises(RuntimeError, match="channel mismatch"):
                enc.write_audio_samples(pcm, sample_rate=48000)
        finally:
            enc.close()

    def test_dtype_contract_enforced(self, out_path: str) -> None:
        """float64 输入显式报错并给出修复指引（此前 from_ndarray 裸抛）。"""
        enc = FFmpegVideoEncoder(out_path, _tiny_config())
        enc.open()
        try:
            enc.add_audio_stream(codec="aac", sample_rate=48000, channels=2)
            pcm = np.zeros((2, 512), dtype=np.float64)
            with pytest.raises(ValueError, match="float32"):
                enc.write_audio_samples(pcm, sample_rate=48000)
        finally:
            enc.close()

    def test_channel_layout_unavailable_fallback_same_source(
        self, out_path: str,
    ) -> None:
        """ChannelLayout 模块不可用时回退 PyAV 朴素命名（不再降 mono）。"""
        enc = FFmpegVideoEncoder(out_path, _tiny_config())
        enc.open()
        blocked = {"src.features.edit.core.channel_layout": None}
        try:
            with patch.dict("sys.modules", blocked):
                for channels, expected in ((1, "mono"), (2, "stereo"),
                                           (6, "6c"), (8, "8c")):
                    enc.add_audio_stream(
                        codec="aac", sample_rate=48000, channels=channels,
                    )
                    assert enc._audio_layout_name == expected, channels
        finally:
            enc.close()

    def test_layout_name_reset_on_close(self, out_path: str) -> None:
        """close 后布局名单一真相状态复位，不跨会话泄漏。"""
        enc = FFmpegVideoEncoder(out_path, _tiny_config())
        enc.open()
        enc.add_audio_stream(codec="aac", sample_rate=48000, channels=6)
        assert enc._audio_layout_name == "5.1"
        enc.close()
        assert enc._audio_layout_name is None


# =============================================================================
# AUD-33600：cleanup() 先请求取消 + 检查 wait() 结果
# =============================================================================

class _RecorderThread:
    """QThread 替身：记录调用序列并可编排 wait() 结果。"""

    def __init__(self, wait_results: list) -> None:
        self.calls: list = []
        self._wait_results = iter(wait_results)

    def quit(self) -> None:
        self.calls.append("quit")

    def requestInterruption(self) -> None:
        self.calls.append("interrupt")

    def wait(self, msecs: int) -> bool:
        self.calls.append(f"wait({msecs})")
        try:
            return bool(next(self._wait_results))
        except StopIteration:
            return True


class TestAud33600Cleanup:
    def _make_exporter(self, qapp):
        from src.shared.export.video_export_worker import VideoExporter
        exporter = VideoExporter()
        real_thread = exporter._worker_thread
        return exporter, real_thread

    def test_cleanup_waits_timeout_path_escalates(
        self, qapp, caplog,
    ) -> None:
        """wait 超时：先取消、后 quit、requestInterruption + 二次等待，
        并以 error 级日志留痕（修复前超时结果被静默忽略）。"""
        exporter, real_thread = self._make_exporter(qapp)
        fake = _RecorderThread(wait_results=[False, False])
        try:
            with patch.object(exporter._worker, "request_cancel",
                              side_effect=lambda: fake.calls.append("cancel")):
                exporter._worker_thread = fake
                with caplog.at_level(logging.ERROR,
                                     logger="src.shared.export.video_export_worker"):
                    exporter.cleanup()
            assert fake.calls[0] == "cancel", "必须先请求取消再 quit/wait"
            assert fake.calls[1] == "quit"
            assert "wait(3000)" in fake.calls
            assert "interrupt" in fake.calls
            assert fake.calls[-1] == "wait(2000)"
            assert "未退出" in caplog.text
        finally:
            exporter._worker_thread = real_thread
            real_thread.quit()
            assert real_thread.wait(2000)

    def test_cleanup_fast_path_returns_after_first_wait(
        self, qapp, caplog,
    ) -> None:
        """取消生效（wait 首次 True）：无需升级中断，也不产生错误日志。"""
        exporter, real_thread = self._make_exporter(qapp)
        fake = _RecorderThread(wait_results=[True])
        try:
            with patch.object(exporter._worker, "request_cancel",
                              side_effect=lambda: fake.calls.append("cancel")):
                exporter._worker_thread = fake
                with caplog.at_level(
                    logging.ERROR,
                    logger="src.shared.export.video_export_worker",
                ):
                    exporter.cleanup()
            assert fake.calls == ["cancel", "quit", "wait(3000)"]
            assert "未退出" not in caplog.text
        finally:
            exporter._worker_thread = real_thread
            real_thread.quit()
            assert real_thread.wait(2000)

    def test_cleanup_during_busy_export_stops_thread(self, qapp, monkeypatch):
        """集成：run() 执行中调用 cleanup()——先请求取消使 run 退出，
        线程正常停止（修复前 wait 超时被忽略，QThread 析构 qFatal）。"""
        import src.features.edit.core.timeline_export as timeline_export_mod
        from src.shared.export.video_export_worker import VideoExporter

        started = threading.Event()
        cancelled = threading.Event()

        class _FakeExporter:
            def cancel_export(self) -> None:
                cancelled.set()

            def pause_export(self) -> None:  # pragma: no cover - 未触发
                pass

            def resume_export(self) -> None:  # pragma: no cover - 未触发
                pass

            def export_timeline(self, config):
                started.set()
                cancelled.wait(timeout=10)  # 模拟长渲染，取消即返回
                return SimpleNamespace(
                    success=False, cancelled=True, error_code=None, cause=None,
                )

        monkeypatch.setattr(
            timeline_export_mod, "create_timeline_exporter",
            lambda **kwargs: _FakeExporter(),
        )

        exporter = VideoExporter()
        config = SimpleNamespace(output_path="unused", codec="h264")
        exporter.export(
            project=None, sequence=None, config=config,
            on_finished=None, on_error=None, on_progress=None,
        )
        assert started.wait(timeout=5), "run() 应已在 worker 线程启动"
        exporter.cleanup()  # 导出仍在运行时清理（审计触发场景）
        thread = exporter._worker_thread
        deadline = time.monotonic() + 3.0
        while thread.isRunning() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not thread.isRunning(), "cleanup 后线程必须已退出"
        # 复位以正常释放（线程已停，安全析构）
        exporter._worker_thread = None
        thread.wait(1000)
