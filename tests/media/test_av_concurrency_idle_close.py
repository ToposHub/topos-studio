"""F1.2/Q01 根治验证——av 并发计数与空闲窗口关闭。

背景见 src/shared/media/av_concurrency.py 与 F1.1 ledger（五轮转储收敛的
libav↔GIL 锁序反转）。本文件锁定：
  1. 计数器嵌套/异常路径正确
  2. 解码公共入口已包裹（视频/音频/VT 三处）
  3. 常驻关闭线程在 in-flight>0 时延迟、归零后完成关闭
"""

from __future__ import annotations

import threading
import time

import pytest

pytestmark = pytest.mark.edit_core

from src.shared.media import av_concurrency
from src.shared.media import ffmpeg_source as fs_mod


class TestAvConcurrencyCounter:
    def test_counter_nested_and_exception_safe(self) -> None:
        assert av_concurrency.in_flight_count() == 0
        with av_concurrency.av_operation():
            assert av_concurrency.in_flight_count() == 1
            with av_concurrency.av_operation():
                assert av_concurrency.in_flight_count() == 2
            assert av_concurrency.in_flight_count() == 1
            with pytest.raises(RuntimeError):
                with av_concurrency.av_operation():
                    raise RuntimeError("boom")
            assert av_concurrency.in_flight_count() == 1
        assert av_concurrency.in_flight_count() == 0

    def test_wait_for_idle_returns_when_idle(self) -> None:
        t0 = time.monotonic()
        av_concurrency.wait_for_idle()
        assert time.monotonic() - t0 < 0.5


class TestEntryPointsWrapped:
    def test_ffmpeg_source_entries_wrapped(self) -> None:
        for name in ("open", "probe", "read_frame", "read_next_frame",
                     "seek_frame", "warm_gop_index"):
            assert hasattr(fs_mod.FFmpegMediaSource.__dict__[name], "__wrapped__"), name

    def test_audio_decoder_entries_wrapped(self) -> None:
        from src.features.edit.core.audio_playback import AudioDecoder
        for name in ("open", "seek_to_time", "seek_to_frame",
                     "decode_next_samples", "decode_samples_for_frame"):
            assert hasattr(AudioDecoder.__dict__[name], "__wrapped__"), name

    def test_vt_source_entries_wrapped(self) -> None:
        import src.shared.media.macos_vt.source as vt_source
        found = 0
        for value in vars(vt_source).values():
            if isinstance(value, type):
                for name in ("read_frame", "read_frame_pixel_buffer", "seek_frame"):
                    method = value.__dict__.get(name)
                    if method is not None:
                        assert hasattr(method, "__wrapped__"), (value, name)
                        found += 1
        assert found >= 2, "VT 源读取入口应至少覆盖两个方法"


class TestIdleWindowClose:
    def test_closer_defers_while_decode_in_flight(self) -> None:
        class _Container:
            def __init__(self) -> None:
                self.closed = threading.Event()

            def close(self) -> None:
                self.closed.set()

        container = _Container()
        fs_mod._ensure_closer_thread().put(container)
        # 模拟解码持续进行：关闭线程必须等待空闲窗口
        with av_concurrency.av_operation():
            time.sleep(0.3)
            assert not container.closed.is_set(), "in-flight 期间不得关闭容器"
        deadline = time.monotonic() + 3.0
        while not container.closed.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert container.closed.is_set(), "空闲后应在短时间内完成关闭"


class TestPerInstanceSerialization:
    def test_same_instance_reads_serialize(self) -> None:
        """run6 转储：同实例并发访问楔死 libav——每实例 RLock 串行化。"""
        from src.shared.media.av_concurrency import av_locked_operation

        events: list[tuple[str, int]] = []

        class _Source:
            def __init__(self) -> None:
                self._av_lock = threading.Lock()

            def read_frame(self, index: int) -> None:
                events.append(("enter", index))
                time.sleep(0.05)
                events.append(("exit", index))

        _Source.read_frame = av_locked_operation(_Source.read_frame)
        source = _Source()
        threads = [threading.Thread(target=source.read_frame, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 串行化：任一时刻只有一个 read 在临界区内（enter 后必须紧跟配对 exit）
        depth = 0
        for kind, _index in events:
            if kind == "enter":
                assert depth == 0, "并发 enter——同实例未串行化"
                depth = 1
            else:
                assert depth == 1, "未配对的 exit"
                depth = 0
        assert len(events) == 8

    def test_ffmpeg_source_entries_use_locked_decorator(self) -> None:
        """生产入口换用 locked 装饰器（含 per-instance 锁路径）。"""
        import inspect

        wrapper = fs_mod.FFmpegMediaSource.__dict__["read_frame"]
        assert hasattr(wrapper, "__wrapped__")
        params = list(inspect.signature(wrapper).parameters)
        assert params[0] == "self"
