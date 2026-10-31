"""wd17-g102：video 域中段（帧队列/监视器输出/播放时钟/会话/异步渲染器）16 条审计修复测试。

覆盖（docs/plans/2026-09-28-multi-machine-program/REMEDIATION_PLAN.md）：
AUD-39201 / AUD-39202 / AUD-39207 / AUD-39209 / AUD-39210 / AUD-39211 /
AUD-39306 / AUD-39307 / AUD-39315 / AUD-39400 / AUD-39401 / AUD-39405 /
AUD-39406 / AUD-39502 / AUD-39503 / AUD-39504

GPU 路径全部用 fake 对象 / monkeypatch 验证（不创建 GL 上下文）。
"""
from __future__ import annotations

import io
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import src.shared.video.frame_queue as fq_module
from src.shared.video.frame_queue import (
    FrameSource,
    QueueEntry,
    ReadyFrameQueue,
    drain_pending_gpu_frame_releases,
    pending_gpu_release_count,
    set_gpu_frame_release_thread,
)
from src.shared.video.playback_clock import FrameScheduler, PlaybackClock, SourceManager
from src.shared.video.playback_frame_trace import EditPlaybackFrameTrace
from src.shared.video.playback_state import PlaybackCoordinator

JOIN_TIMEOUT_S = 3.0


@pytest.fixture(autouse=True)
def _reset_gpu_release_thread():
    """每个测试后恢复 GPU 释放线程注册（模块级状态）。"""
    yield
    set_gpu_frame_release_thread(None)


@pytest.fixture(autouse=True)
def _reset_decklink_probe_cache():
    import src.shared.video.monitor_output as mo

    mo._DECKLINK_SUPPORT_CACHE.clear()
    yield
    mo._DECKLINK_SUPPORT_CACHE.clear()


def _cpu_entry(frame_index: int, can_drop: bool = False, gen: int = 0) -> QueueEntry:
    return QueueEntry(
        frame=SimpleNamespace(frame_index=frame_index),
        decode_ms=10.0,
        source=FrameSource.MASTER if not can_drop else FrameSource.PROXY,
        can_drop=can_drop,
        generation_id=gen,
    )


def _gpu_entry(frame_index: int, can_drop: bool = False, gen: int = 0) -> QueueEntry:
    gpu_frame = MagicMock(spec=fq_module.GpuDecodedFrame)
    gpu_frame.frame_index = frame_index
    return QueueEntry(
        frame=gpu_frame,
        decode_ms=5.0,
        source=FrameSource.GPU_MASTER if not can_drop else FrameSource.GPU_PROXY,
        can_drop=can_drop,
        generation_id=gen,
    )


# ============================================================================
# AUD-39201: 队列在非渲染线程同步触发 GPU 帧 release 回调
# ============================================================================


class TestAud39201DeferredGpuRelease:
    def test_foreign_thread_drop_defers_until_drain(self, monkeypatch) -> None:
        """UI/解码线程淘汰 GPU 帧 → 挂起不释放；drain 才真正释放。"""
        released: list[int] = []
        monkeypatch.setattr(
            fq_module, "safe_release_gpu_frame",
            lambda f: released.append(f.frame_index))

        # 注册一个不属于当前线程的"渲染线程"
        set_gpu_frame_release_thread(threading.get_ident() + 100_000)
        queue = ReadyFrameQueue()
        queue.enqueue(_gpu_entry(1, can_drop=True, gen=0))

        # 非渲染线程 seek 翻代数 → 旧 GPU 帧被淘汰但不得同步释放
        queue.increment_generation()

        assert released == [], "非渲染线程不得同步触发 release 回调"
        assert pending_gpu_release_count() == 1, "被淘汰帧应挂入 pending"

        # 渲染线程排水（drain 不校验调用者，模拟渲染线程调用）
        count = drain_pending_gpu_frame_releases()
        assert count == 1
        assert released == [1]
        assert pending_gpu_release_count() == 0

    def test_release_on_owner_thread_is_immediate(self, monkeypatch) -> None:
        """当前线程即注册的渲染线程 → 立即释放（无延迟开销）。"""
        released: list[int] = []
        monkeypatch.setattr(
            fq_module, "safe_release_gpu_frame",
            lambda f: released.append(f.frame_index))

        set_gpu_frame_release_thread(threading.get_ident())
        queue = ReadyFrameQueue()
        queue.enqueue(_gpu_entry(7, can_drop=True, gen=0))
        queue.increment_generation()

        assert released == [7]
        assert pending_gpu_release_count() == 0

    def test_no_registration_keeps_legacy_immediate_release(
            self, monkeypatch) -> None:
        """未注册渲染线程（测试/无 GL 环境）→ 保持立即释放的既有行为。"""
        released: list[int] = []
        monkeypatch.setattr(
            fq_module, "safe_release_gpu_frame",
            lambda f: released.append(f.frame_index))

        set_gpu_frame_release_thread(None)
        queue = ReadyFrameQueue()
        queue.enqueue(_gpu_entry(3, can_drop=True, gen=0))
        queue.increment_generation()

        assert released == [3]
        assert pending_gpu_release_count() == 0

    def test_clear_all_defers_gpu_release_on_foreign_thread(
            self, monkeypatch) -> None:
        """unload（clear_all）路径同样遵守延迟释放契约。"""
        released: list[int] = []
        monkeypatch.setattr(
            fq_module, "safe_release_gpu_frame",
            lambda f: released.append(f.frame_index))

        set_gpu_frame_release_thread(threading.get_ident() + 100_000)
        queue = ReadyFrameQueue()
        queue.enqueue(_gpu_entry(5, can_drop=True, gen=0))
        proxy_n, master_n = queue.clear_all()

        assert (proxy_n, master_n) == (1, 0)
        assert released == []
        assert pending_gpu_release_count() == 1
        drain_pending_gpu_frame_releases()
        assert released == [5]

    def test_unregister_releases_pending(self, monkeypatch) -> None:
        """注销渲染线程时立即清空 pending（不留死账）。"""
        released: list[int] = []
        monkeypatch.setattr(
            fq_module, "safe_release_gpu_frame",
            lambda f: released.append(f.frame_index))

        set_gpu_frame_release_thread(threading.get_ident() + 100_000)
        queue = ReadyFrameQueue()
        queue.enqueue(_gpu_entry(9, can_drop=True, gen=0))
        queue.increment_generation()
        assert pending_gpu_release_count() == 1

        set_gpu_frame_release_thread(None)
        assert released == [9]
        assert pending_gpu_release_count() == 0


# ============================================================================
# AUD-39202: peek/get_latest/get_latest_before_or_at 返回队列内部 entry 引用
# ============================================================================


class TestAud39202SnapshotAccessors:
    def test_peek_returns_snapshot_not_queue_reference(self) -> None:
        queue = ReadyFrameQueue()
        entry = _cpu_entry(10, can_drop=True)
        queue.enqueue(entry)

        peeked = queue.peek(is_master=False)
        assert peeked is not None
        assert peeked is not entry, "peek 不得返回队列内部 entry 引用"
        assert peeked.frame_index == 10
        assert queue.get_size(is_master=False) == 1, "peek 不出队"

        # 调用方修改快照不得串改队列状态
        peeked.decode_ms = 999.0
        assert queue._proxy_queue[0].decode_ms == 10.0

    def test_get_latest_returns_snapshot(self) -> None:
        queue = ReadyFrameQueue()
        for i in range(3):
            queue.enqueue(_cpu_entry(i, can_drop=True))

        latest = queue.get_latest(is_master=False)
        assert latest is not None
        assert latest.frame_index == 2
        assert latest is not queue._proxy_queue[-1]

    def test_get_latest_before_or_at_returns_snapshot(self) -> None:
        queue = ReadyFrameQueue()
        for i in (1, 5, 9):
            queue.enqueue(_cpu_entry(i, can_drop=True))

        result = queue.get_latest_before_or_at(7, is_master=False)
        assert result.found
        assert result.actual_frame_index == 5
        assert result.entry is not None
        assert result.entry is not queue._proxy_queue[1]
        # 不出队、不丢弃
        assert queue.get_size(is_master=False) == 3

    def test_dequeue_transfers_real_entry(self) -> None:
        """所有权语义对照：dequeue 返回真实 entry（出队转移所有权）。"""
        queue = ReadyFrameQueue()
        entry = _cpu_entry(4, can_drop=True)
        queue.enqueue(entry)

        taken = queue.dequeue(is_master=False)
        assert taken is entry
        assert queue.get_size(is_master=False) == 0


# ============================================================================
# AUD-39207: 多个 media 文件匹配同一预算 glob 时只裁决第一个
# ============================================================================


def _runner_report(media):
    return {
        "$schema_version": 1,
        "machine": {"platform": "macOS-15.3-x86_64"},
        "media": media,
    }


def _media_entry(name, steady_p95, seek_p95=None):
    phases = {"steady_sequential": {"p95_ms": steady_p95}}
    if seek_p95 is not None:
        phases["seek_random"] = {"p95_ms": seek_p95}
    return {"file": f"tests/test_videos/{name}", "phases": phases}


class TestAud39207AllGlobMatchesAdjudicated:
    def test_second_matching_file_breach_is_caught(self) -> None:
        """第一个匹配文件达标、第二个超硬门 → 门禁必须失败。"""
        from src.shared.video.performance_budgets import (
            evaluate_report_against_budgets, load_budgets,
        )

        registry = load_budgets()
        # BUD-B03-SEEK-P95（active，glob '4K_prores*.mov'，hard 150）：
        # LT 37ms 达标在前、HQ 200ms 超硬门在后——旧实现取第一个（pass），
        # 第二个文件逃过门禁。
        report = _runner_report([
            _media_entry("4K_prores422LT.mov", steady_p95=20.0, seek_p95=37.0),
            _media_entry("4K_prores422HQ.mov", steady_p95=20.0, seek_p95=200.0),
        ])
        verdicts = evaluate_report_against_budgets(report, registry)
        seek = next(v for v in verdicts
                    if v["budget_id"] == "BUD-B03-SEEK-P95")
        assert seek["verdict"] == "hard_breach", (
            "glob 命中的所有文件都必须裁决，最差样本决定 verdict"
        )
        assert seek["value"] == 200.0
        assert seek["media"].endswith("4K_prores422HQ.mov")

    def test_worst_sample_drives_verdict_regardless_of_order(self) -> None:
        from src.shared.video.performance_budgets import (
            evaluate_report_against_budgets, load_budgets,
        )

        registry = load_budgets()
        report = _runner_report([
            _media_entry("4K_prores422HQ.mov", steady_p95=38.2),
            _media_entry("4K_prores422LT.mov", steady_p95=28.8),
        ])
        verdicts = evaluate_report_against_budgets(report, registry)
        steady = next(v for v in verdicts
                      if v["budget_id"] == "BUD-B03-STEADY-P95")
        # 38.2 为最差样本（> hard 32，known_breach 登记状态）
        assert steady["value"] == 38.2
        assert steady["verdict"] == "known_breach"

    def test_all_pass_still_passes(self) -> None:
        from src.shared.video.performance_budgets import (
            evaluate_report_against_budgets, load_budgets,
        )

        registry = load_budgets()
        report = _runner_report([
            _media_entry("4K_prores422LT.mov", steady_p95=20.0, seek_p95=37.0),
            _media_entry("4K_prores422HQ.mov", steady_p95=20.0, seek_p95=37.0),
        ])
        verdicts = evaluate_report_against_budgets(report, registry)
        seek = next(v for v in verdicts
                    if v["budget_id"] == "BUD-B03-SEEK-P95")
        assert seek["verdict"] == "pass"


# ============================================================================
# AUD-39209: ffmpeg stderr=PIPE 从不读取（管道缓冲写满后监控输出冻结）
# ============================================================================


class _TrackingBytesIO(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.read_to_eof = False

    def readline(self, size: int = -1) -> bytes:
        line = super().readline(size)
        if line == b"":
            self.read_to_eof = True
        return line


class _BrokenStdin:
    def write(self, data) -> None:
        raise BrokenPipeError("pipe gone")

    def flush(self) -> None:
        pass


class _FakeFfmpegProcess:
    def __init__(self, stderr_data: bytes) -> None:
        self.stdin = _BrokenStdin()
        self.stderr = _TrackingBytesIO(stderr_data)
        self.terminated = False

    def poll(self):
        return None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout=None) -> int:
        return 0

    def kill(self) -> None:
        pass


class TestAud39209StderrDrained:
    def test_run_spawns_stderr_drainer(self, monkeypatch) -> None:
        import src.shared.video.monitor_output as mo

        monkeypatch.setattr(mo, "resolve_ffmpeg_path", lambda p: "/fake/ffmpeg")
        monkeypatch.setattr(mo, "_decklink_support_cached", lambda p, timeout=5.0: True)

        stderr_data = b"warning: decklink output overflow\n" * 8
        process = _FakeFfmpegProcess(stderr_data)
        config = mo.DeckLinkMonitorConfig(
            enabled=True, device="DeckLink", ffmpeg_path="ffmpeg")
        backend = mo.FfmpegDeckLinkMonitorOutput(
            config, process_factory=lambda *a, **k: process)

        assert backend.start() is True
        backend._thread.join(JOIN_TIMEOUT_S)
        assert not backend._thread.is_alive()

        assert process.stderr.read_to_eof, (
            "stderr 必须被持续读取（否则管道缓冲写满后 ffmpeg 停止 "
            "消费 stdin，监控输出冻结）"
        )
        assert process.stderr.closed

    def test_drain_stderr_helper_consumes_and_closes(self) -> None:
        from src.shared.video.monitor_output import (
            DeckLinkMonitorConfig, FfmpegDeckLinkMonitorOutput,
        )

        stream = _TrackingBytesIO(b"line1\nline2\n")
        backend = FfmpegDeckLinkMonitorOutput(
            DeckLinkMonitorConfig(enabled=True, device="d"))
        backend._drain_stderr(stream)
        assert stream.read_to_eof
        assert stream.closed

    def test_drain_stderr_tolerates_none(self) -> None:
        from src.shared.video.monitor_output import (
            DeckLinkMonitorConfig, FfmpegDeckLinkMonitorOutput,
        )

        backend = FfmpegDeckLinkMonitorOutput(
            DeckLinkMonitorConfig(enabled=True, device="d"))
        backend._drain_stderr(None)  # 不得抛错


# ============================================================================
# AUD-39210: start() 在调用线程同步执行 ffmpeg 子进程探测（阻塞 UI）
# ============================================================================


class TestAud39210AsyncProbe:
    def test_start_does_not_probe_on_caller_thread(self, monkeypatch) -> None:
        import src.shared.video.monitor_output as mo

        probe_idents: list[int] = []

        def slow_probe(path, timeout=5.0):
            probe_idents.append(threading.get_ident())
            time.sleep(0.2)
            return False  # 探测失败 → 后台线程退出

        monkeypatch.setattr(mo, "resolve_ffmpeg_path", lambda p: "/fake/ffmpeg")
        monkeypatch.setattr(mo, "ffmpeg_supports_decklink", slow_probe)

        config = mo.DeckLinkMonitorConfig(
            enabled=True, device="DeckLink", ffmpeg_path="ffmpeg")
        backend = mo.FfmpegDeckLinkMonitorOutput(config)

        t0 = time.monotonic()
        assert backend.start() is True
        elapsed = time.monotonic() - t0

        # 探测可能已在后台线程开始，但绝不能发生在调用线程，且 start()
        # 不得等待探测完成（探测内含 0.2s 睡眠）
        assert elapsed < 0.1, f"start() 应立即返回（实测 {elapsed * 1000:.0f}ms）"

        backend._thread.join(JOIN_TIMEOUT_S)
        assert not backend._thread.is_alive()
        assert len(probe_idents) == 1, "探测应移到后台线程执行"
        assert probe_idents[0] != threading.get_ident()

    def test_cached_failure_fails_fast_without_probe(self, monkeypatch) -> None:
        import src.shared.video.monitor_output as mo

        probe_calls: list[str] = []
        monkeypatch.setattr(mo, "resolve_ffmpeg_path", lambda p: "/fake/ffmpeg")
        monkeypatch.setattr(
            mo, "ffmpeg_supports_decklink",
            lambda p, timeout=5.0: probe_calls.append(p) or True)

        # 预置缓存结论：该 ffmpeg 不支持 DeckLink
        mo._DECKLINK_SUPPORT_CACHE["/fake/ffmpeg"] = (
            False, time.monotonic() + 60.0)

        config = mo.DeckLinkMonitorConfig(
            enabled=True, device="DeckLink", ffmpeg_path="ffmpeg")
        backend = mo.FfmpegDeckLinkMonitorOutput(config)
        assert backend.start() is False, "缓存结论为不支持时必须立即失败"
        assert probe_calls == []

    def test_missing_ffmpeg_still_fails_fast(self, monkeypatch) -> None:
        import src.shared.video.monitor_output as mo

        monkeypatch.setattr(mo, "resolve_ffmpeg_path", lambda p: None)
        config = mo.DeckLinkMonitorConfig(
            enabled=True, device="DeckLink", ffmpeg_path="ffmpeg")
        backend = mo.FfmpegDeckLinkMonitorOutput(config)
        assert backend.start() is False


# ============================================================================
# AUD-39211: YUV→BGR 硬编码单一系数集（外部监看颜色偏移）
# ============================================================================


class TestAud39211MatrixAwareYuvConversion:
    @staticmethod
    def _frame(planes, pixel_format="yuv444p", **color):
        return SimpleNamespace(
            pixel_format=pixel_format, planes=planes, bit_depth=8, **color)

    def test_bt601_vs_bt709_full_range_differ(self) -> None:
        from src.shared.video.monitor_output import decoded_frame_to_bgr_array

        y = np.full((1, 1), 128, np.uint8)
        u = np.full((1, 1), 178, np.uint8)  # U' = +50
        v = np.full((1, 1), 128, np.uint8)

        bgr601 = decoded_frame_to_bgr_array(
            self._frame((y, u, v), color_matrix="bt601", color_range="full"))
        bgr709 = decoded_frame_to_bgr_array(
            self._frame((y, u, v), color_matrix="bt709", color_range="full"))

        # 独立参考计算（BT.601 全量程）
        assert bgr601 is not None and bgr709 is not None
        assert tuple(bgr601[0, 0]) == (
            int(128 + 1.772 * 50),    # B
            int(128 - 0.3441 * 50),   # G
            int(128 + 1.402 * 0),     # R
        )
        # 独立参考计算（BT.709 全量程）
        assert tuple(bgr709[0, 0]) == (
            int(128 + 1.8556 * 50),
            int(128 - 0.1873 * 50),
            int(128 + 1.5748 * 0),
        )
        assert tuple(bgr601[0, 0]) != tuple(bgr709[0, 0]), (
            "不同色彩矩阵必须产生不同转换结果"
        )

    def test_limited_range_expands_studio_swing(self) -> None:
        from src.shared.video.monitor_output import decoded_frame_to_bgr_array

        y = np.full((1, 1), 128, np.uint8)
        neutral = np.full((1, 1), 128, np.uint8)
        bgr_limited = decoded_frame_to_bgr_array(
            self._frame((y, neutral, neutral), color_matrix="bt709",
                        color_range="limited"))
        bgr_full = decoded_frame_to_bgr_array(
            self._frame((y, neutral, neutral), color_matrix="bt709",
                        color_range="full"))

        assert bgr_limited is not None and bgr_full is not None
        # limited：Y=128 → (128-16)*255/219 ≈ 130.5；full：原值 128
        assert tuple(bgr_limited[0, 0]) == (130, 130, 130)
        assert tuple(bgr_full[0, 0]) == (128, 128, 128)

    def test_missing_metadata_keeps_legacy_coefficients(self) -> None:
        """无色彩元数据 → 保持旧行为（BT.709 全量程、无范围扩展）。"""
        from src.shared.video.monitor_output import decoded_frame_to_bgr_array

        y = np.full((1, 1), 128, np.uint8)
        u = np.full((1, 1), 178, np.uint8)
        v = np.full((1, 1), 128, np.uint8)
        bgr = decoded_frame_to_bgr_array(self._frame((y, u, v)))

        assert bgr is not None
        # 旧实现的硬编码公式：b = y + 1.8556*u'; g = y - 0.1873*u' - 0.4681*v'
        assert tuple(bgr[0, 0]) == (
            int(128 + 1.8556 * 50),
            int(128 - 0.1873 * 50 - 0.4681 * 0),
            int(128 + 1.5748 * 0),
        )

    def test_nv12_uses_frame_color_metadata(self) -> None:
        from src.shared.video.monitor_output import decoded_frame_to_bgr_array

        # 2x2 NV12：Y=128，UV 交错行 = [u, v]（宽 2 字节 = 1 对）
        data = np.zeros((3, 2), np.uint8)
        data[:2, :] = 128
        data[2, :] = [178, 128]

        bgr601 = decoded_frame_to_bgr_array(SimpleNamespace(
            pixel_format="nv12", data=data, bit_depth=8,
            color_matrix="bt601", color_range="full"))
        bgr709 = decoded_frame_to_bgr_array(SimpleNamespace(
            pixel_format="nv12", data=data, bit_depth=8,
            color_matrix="bt709", color_range="full"))

        assert bgr601 is not None and bgr709 is not None
        assert bgr601.shape == (2, 2, 3)
        assert tuple(bgr601[0, 0]) != tuple(bgr709[0, 0]), (
            "NV12 路径也必须按帧色彩矩阵转换（此前硬编码 BT.601）"
        )
        # 601 参考值与 planar 路径一致
        assert tuple(bgr601[0, 0]) == (
            int(128 + 1.772 * 50), int(128 - 0.3441 * 50), 128)

    def test_bt2020_supported(self) -> None:
        from src.shared.video.monitor_output import decoded_frame_to_bgr_array

        y = np.full((1, 1), 128, np.uint8)
        u = np.full((1, 1), 178, np.uint8)
        v = np.full((1, 1), 128, np.uint8)
        bgr = decoded_frame_to_bgr_array(
            self._frame((y, u, v), color_matrix="bt2020nc", color_range="full"))
        assert bgr is not None
        assert tuple(bgr[0, 0]) == (
            int(128 + 1.8814 * 50), int(128 - 0.1646 * 50), 128)


# ============================================================================
# AUD-39306: 共享源（无需代理）master/proxy 双游标各自记账
# ============================================================================


class _FakeSharedSource:
    """记录 seek/read 行为的可控媒体源（模拟共享 MediaSource）。"""

    def __init__(self) -> None:
        self.pos = -1
        self.seek_calls: list[int] = []

    def seek_frame(self, idx: int) -> None:
        self.seek_calls.append(idx)
        self.pos = idx - 1

    def read_next_frame(self):
        self.pos += 1
        return SimpleNamespace(
            frame_index=self.pos,
            data=np.zeros((1, 1, 3), np.uint8))

    def read_frame(self, idx: int):
        self.pos = idx
        return SimpleNamespace(
            frame_index=idx,
            data=np.zeros((1, 1, 3), np.uint8))


def _shared_source_manager() -> tuple[SourceManager, _FakeSharedSource]:
    sm = SourceManager("/mock/shared.mov")
    fake = _FakeSharedSource()
    sm._master_source = fake
    sm._proxy_source = fake  # 无需代理：共享同一源对象
    sm._is_open = True
    return sm, fake


class TestAud39306SharedSourceUnifiedCursor:
    def test_master_read_advances_proxy_cursor(self) -> None:
        sm, fake = _shared_source_manager()
        assert sm._shares_master_proxy_source

        sm.get_master_frame(10)
        assert sm._master_current_frame == 10
        assert sm._proxy_current_frame == 10, (
            "共享源时 master 读取必须推进 proxy 游标（同一解码器）"
        )

    def test_proxy_sequential_uses_master_cursor_after_master_read(self) -> None:
        """审计复现场景：master 读到 10 后 proxy 请求 11。"""
        sm, fake = _shared_source_manager()
        sm.get_master_frame(10)
        fake.seek_calls.clear()

        frame = sm.get_proxy_frame(11)
        assert frame is not None
        assert frame.frame_index == 11, "顺序请求 11 必须真的读到第 11 帧"
        assert fake.seek_calls == [], "顺序下一帧免 seek 优化保留"
        assert sm._cache.get_proxy(11) is frame

    def test_proxy_request_after_master_read_reseeks_correctly(self) -> None:
        """旧缺陷复现：master 读取后 proxy 请求低帧号必须 seek 而非顺序读。

        旧实现 proxy 游标仍是 -1：请求 0 被判为"顺序下一帧"（0 == -1+1）
        免 seek，read_next 返回解码器当前位置的下一帧（11），被当作第 0
        帧缓存。修复后以 master 游标为权威：0 != 10+1 → seek(0) → 读到 0。
        """
        sm, fake = _shared_source_manager()
        sm.get_master_frame(10)
        fake.seek_calls.clear()

        frame = sm.get_proxy_frame(0)
        assert frame is not None
        assert frame.frame_index == 0, (
            "共享源下 proxy 请求 0 必须读到第 0 帧（而非解码器当前位置）"
        )
        assert fake.seek_calls == [0]

    def test_separate_sources_keep_independent_cursors(self) -> None:
        """独立 proxy 源（正常降级场景）不受共享源逻辑影响。"""
        sm = SourceManager("/mock/proxied.mov")
        master = _FakeSharedSource()
        proxy = _FakeSharedSource()
        sm._master_source = master
        sm._proxy_source = proxy
        sm._is_open = True
        assert not sm._shares_master_proxy_source

        sm.get_master_frame(10)
        assert sm._master_current_frame == 10
        assert sm._proxy_current_frame == -1

        sm._proxy_current_frame = 14  # 模拟 proxy worker 此前的顺序读取
        proxy.pos = 14
        frame = sm.get_proxy_frame(15)
        assert frame is not None
        assert frame.frame_index == 15
        assert proxy.seek_calls == [], "独立源顺序播放免 seek 保留"


# ============================================================================
# AUD-39307: FrameScheduler 未适配"未知时长"（total_frames=0 预读被禁用）
# ============================================================================


def _scheduler_source_manager():
    return SimpleNamespace(cache=SimpleNamespace(
        proxy_cache=SimpleNamespace(
            get_missing_frames=lambda s, e: list(range(s, e + 1))),
        has_proxy=lambda i: False,
    ))


class TestAud39307UnknownDurationPrefetch:
    def test_known_total_frames_property(self) -> None:
        assert PlaybackClock(fps=24.0, total_frames=0).known_total_frames is None
        assert PlaybackClock(fps=24.0, total_frames=100).known_total_frames == 100
        assert PlaybackClock(fps=24.0, total_frames=0).total_frames == 0

    def test_prefetch_range_without_upper_bound(self) -> None:
        clock = PlaybackClock(fps=24.0, total_frames=0)
        scheduler = FrameScheduler(clock, _scheduler_source_manager(),
                                   prefetch_lookahead=8)
        # 旧实现：min(0+8, 0-1) = -1 → 预读被整体禁用
        assert scheduler.get_prefetch_range() == (1, 8)

    def test_schedule_next_frame_enabled_for_unknown_duration(self) -> None:
        clock = PlaybackClock(fps=24.0, total_frames=0)
        scheduler = FrameScheduler(clock, _scheduler_source_manager())
        # 旧实现：next_frame(1) >= total_frames(0) → 恒 None
        assert scheduler.schedule_next_frame() == 1

    def test_known_duration_behavior_unchanged(self) -> None:
        clock = PlaybackClock(fps=24.0, total_frames=10)
        scheduler = FrameScheduler(clock, _scheduler_source_manager(),
                                   prefetch_lookahead=8)
        assert scheduler.get_prefetch_range() == (1, 8)
        # 总帧数小于前瞻窗口时仍被 clamp 到末帧
        short_clock = PlaybackClock(fps=24.0, total_frames=5)
        short = FrameScheduler(short_clock, _scheduler_source_manager(),
                               prefetch_lookahead=8)
        assert short.get_prefetch_range() == (1, 4)
        clock.seek_to_frame(9)
        assert scheduler.get_next_frame() == 10
        assert scheduler.schedule_next_frame() is None, (
            "已知时长越过末帧仍不得调度"
        )


# ============================================================================
# AUD-39315: is_full_gpu 的 decode 白名单与全库 decode_path 词表不符
# ============================================================================


class TestAud39315FullGpuDecodeWhitelist:
    def test_real_zero_copy_paths_recognized(self) -> None:
        for path in (
            "macos_vt_iosurface_zero_copy",
            "macos_topos_iosurface_zero_copy",
            "macos_vt_cvmetal_zero_copy",
            "windows_d3d11_gl_zero_copy",
        ):
            trace = EditPlaybackFrameTrace(
                decode_path=path, render_path="gpu_composite")
            assert trace.is_full_gpu(), f"{path} 是真实零拷贝路径，必须判全 GPU"

    def test_generic_gpu_values_recognized(self) -> None:
        for path in ("gpu", "gpu_provider", "gpu_async_submitted"):
            trace = EditPlaybackFrameTrace(
                decode_path=path, render_path="gpu_direct")
            assert trace.is_full_gpu()

    def test_dead_whitelist_entries_no_longer_count(self) -> None:
        """旧白名单中不存在的值（hw_decode_planar/gpu_direct）不得再放行。

        注意 'macos_vt_zero_copy' 是真实产出值（timeline_preview_renderer
        的 gpu/vt_pixel_buffer 层标记，收尾复检时修正回集合），不在死值之列。
        """
        for dead in ("hw_decode_planar", "gpu_direct"):
            trace = EditPlaybackFrameTrace(
                decode_path=dead, render_path="gpu_composite")
            assert not trace.is_full_gpu(), dead
        # 真实 VT 零拷贝 token 必须继续放行
        live = EditPlaybackFrameTrace(
            decode_path="macos_vt_zero_copy", render_path="gpu_composite")
        assert live.is_full_gpu()

    def test_cpu_and_download_paths_rejected(self) -> None:
        for path in ("cpu_packed_rgb", "cpu_planar_yuv",
                     "hwaccel_download_planar_yuv", "hwaccel_download_packed_rgb"):
            trace = EditPlaybackFrameTrace(
                decode_path=path, render_path="gpu_composite")
            assert not trace.is_full_gpu(), (
                f"{path} 涉及 CPU/下载回退，不得判全 GPU"
            )

    def test_multilayer_join_all_segments_must_be_gpu(self) -> None:
        ok = EditPlaybackFrameTrace(
            decode_path="macos_vt_iosurface_zero_copy,windows_d3d11_gl_zero_copy",
            render_path="gpu_composite")
        assert ok.is_full_gpu()

        mixed = EditPlaybackFrameTrace(
            decode_path="macos_vt_iosurface_zero_copy,cpu_packed_rgb",
            render_path="gpu_composite")
        assert not mixed.is_full_gpu()

    def test_empty_decode_path_rejected(self) -> None:
        trace = EditPlaybackFrameTrace(decode_path="", render_path="gpu_composite")
        assert not trace.is_full_gpu()


# ============================================================================
# AUD-39400: 状态回调在持有 _session_lock 时同步触发（Qt 直连槽自死锁）
# ============================================================================


class _ReentrantFakeSession:
    """模拟 Qt 直连槽：状态回调内重入协调器属性（同线程二次取锁）。"""

    total_frames = None
    is_proxy = False

    def __init__(self, coordinator: PlaybackCoordinator) -> None:
        self._coordinator = coordinator

    @property
    def state(self):
        from src.shared.video.session import PlaybackState
        return PlaybackState.READY

    def _reenter(self) -> None:
        # 旧实现：_session_lock 非重入 → 此处死锁
        _ = self._coordinator.is_loaded
        _ = self._coordinator.total_frames

    def set_on_state_change(self, cb) -> None:
        self._cb = cb
        if cb is not None:
            cb(self.state)  # 模拟状态回调（重入协调器）

    def load(self, path: str) -> bool:
        self._reenter()
        return True

    def unload(self, timeout: float = 2.0) -> bool:
        self._reenter()
        return True

    def start_playback(self) -> bool:
        self._reenter()
        return True

    def stop_playback(self, timeout: float = 2.0) -> bool:
        self._reenter()
        return True

    def pause(self) -> bool:
        self._reenter()
        return True

    def resume(self) -> bool:
        self._reenter()
        return True

    def seek_to_frame(self, frame, *, blocking=True, churn_generation=True) -> bool:
        self._reenter()
        return True


class TestAud39400CallbacksOutsideSessionLock:
    def _run_with_deadlock_guard(self, fn, timeout: float = 2.0):
        result: dict = {}

        def _target() -> None:
            try:
                result["value"] = fn()
            except BaseException as exc:  # noqa: BLE001 - 测试捕获
                result["error"] = exc

        worker = threading.Thread(target=_target, daemon=True)
        worker.start()
        worker.join(timeout)
        assert not worker.is_alive(), (
            "状态回调重入协调器属性时死锁（_session_lock 持锁触发回调）"
        )
        assert "error" not in result, result.get("error")
        return result["value"]

    def test_start_playback_reentrant_callback(self) -> None:
        coordinator = PlaybackCoordinator()
        session = _ReentrantFakeSession(coordinator)
        coordinator._session = session

        assert self._run_with_deadlock_guard(coordinator.start_playback) is True

    def test_stop_pause_resume_seek_reentrant(self) -> None:
        coordinator = PlaybackCoordinator()
        coordinator._session = _ReentrantFakeSession(coordinator)

        assert self._run_with_deadlock_guard(coordinator.pause) is True
        assert self._run_with_deadlock_guard(coordinator.resume) is True
        assert self._run_with_deadlock_guard(coordinator.stop_playback) is True
        assert self._run_with_deadlock_guard(
            lambda: coordinator.seek_to_frame(5)) is True

    def test_load_reentrant_callback(self, monkeypatch) -> None:
        coordinator = PlaybackCoordinator()
        old_session = _ReentrantFakeSession(coordinator)
        coordinator._session = old_session

        import src.shared.video.session as session_module
        monkeypatch.setattr(
            session_module, "VideoSession",
            lambda **kwargs: _ReentrantFakeSession(coordinator))

        assert self._run_with_deadlock_guard(
            lambda: coordinator.load("/mock/v.mov")) is True
        # 旧会话在锁外被卸载（回调重入不死锁）
        assert coordinator.session is not old_session

    def test_unload_reentrant_callback(self) -> None:
        coordinator = PlaybackCoordinator()
        coordinator._session = _ReentrantFakeSession(coordinator)

        assert self._run_with_deadlock_guard(coordinator.unload) is True
        assert coordinator.session is None


# ============================================================================
# AUD-39401: 加载失败后 is_loaded 仍返回 True（ERROR 未排除）
# ============================================================================


class _StateStubSession:
    def __init__(self, state_name: str) -> None:
        from src.shared.video.session import PlaybackState
        self.state = getattr(PlaybackState, state_name)


class TestAud39401IsLoadedExcludesError:
    def test_error_state_is_not_loaded(self) -> None:
        coordinator = PlaybackCoordinator()
        coordinator._session = _StateStubSession("ERROR")
        assert coordinator.is_loaded is False, (
            "加载失败（ERROR）后会话不得再报告为已加载"
        )

    def test_unloaded_is_not_loaded(self) -> None:
        coordinator = PlaybackCoordinator()
        coordinator._session = _StateStubSession("UNLOADED")
        assert coordinator.is_loaded is False

    def test_ready_is_loaded(self) -> None:
        coordinator = PlaybackCoordinator()
        coordinator._session = _StateStubSession("READY")
        assert coordinator.is_loaded is True

    def test_no_session_is_not_loaded(self) -> None:
        assert PlaybackCoordinator().is_loaded is False


# ============================================================================
# AUD-39405: get_frame_at(is_master=True) 缺 PLAYING 防护且共享模式无 fallback
# ============================================================================


class TestAud39405GetFrameAtMasterGuards:
    def _session(self, **overrides) -> "VideoSession":
        from src.shared.video.session import VideoSession
        session = VideoSession(use_shared_frame_provider=False)
        for key, value in overrides.items():
            setattr(session, key, value)
        return session

    def test_no_sync_fallback_during_playback(self) -> None:
        from src.shared.video.session import PlaybackState

        source = SimpleNamespace(
            get_master_frame=lambda idx: pytest.fail(
                "播放态禁止同步回退解码（P2-1）"))
        session = self._session(
            _state=PlaybackState.PLAYING,
            _frame_queue=None,
            _source_manager=source,
        )
        assert session.get_frame_at(10, is_master=True) is None

    def test_shared_provider_mode_has_adapter_fallback(self) -> None:
        from src.shared.video.session import PlaybackState

        sentinel = SimpleNamespace(frame_index=10)
        adapter = SimpleNamespace(
            get_master_frame=lambda idx: sentinel)
        session = self._session(
            _use_shared_frame_provider=True,
            _frame_provider_adapter=adapter,
            _frame_queue=None,
            _source_manager=None,
            _state=PlaybackState.READY,
        )
        got = session.get_frame_at(10, is_master=True)
        assert got is sentinel, "共享帧服务模式必须有 adapter fallback"
        assert session._current_master_frame is sentinel

    def test_independent_mode_source_manager_fallback_unchanged(self) -> None:
        from src.shared.video.session import PlaybackState

        sentinel = SimpleNamespace(frame_index=3)
        session = self._session(
            _state=PlaybackState.PAUSED,
            _frame_queue=None,
            _source_manager=SimpleNamespace(
                get_master_frame=lambda idx: sentinel),
        )
        assert session.get_frame_at(3, is_master=True) is sentinel

    def test_ready_frame_taken_with_ownership(self) -> None:
        """等待循环取帧必须 dequeue（所有权转移）而不是 peek 快照。"""
        from src.shared.video.session import PlaybackState

        queue = ReadyFrameQueue()
        entry = _cpu_entry(42)
        queue.enqueue(entry)
        session = self._session(
            _state=PlaybackState.PAUSED, _frame_queue=queue)

        got = session.get_frame_at(42, is_master=True)
        assert got is entry.frame
        assert queue.get_size(is_master=True) == 0, (
            "get_frame_at 取走帧后不得残留在队列（防重复消费/释放竞态）"
        )


# ============================================================================
# AUD-39406: seek_to_frame 无状态校验且恒返回 True
# ============================================================================


class TestAud39406SeekStateValidation:
    def _session(self, state_name: str) -> "VideoSession":
        from src.shared.video.session import PlaybackState, VideoSession
        session = VideoSession(use_shared_frame_provider=False)
        session._state = getattr(PlaybackState, state_name)
        return session

    @pytest.mark.parametrize("state_name", ["UNLOADED", "LOADING", "ERROR"])
    def test_invalid_states_return_false(self, state_name: str) -> None:
        session = self._session(state_name)
        assert session.seek_to_frame(100) is False, (
            f"{state_name} 状态下没有可 seek 的媒体，不得假装成功"
        )
        assert session.state.name == state_name, "拒绝后状态不得被改成 SEEKING"

    def test_ready_state_seek_succeeds(self) -> None:
        session = self._session("READY")
        assert session.seek_to_frame(0) is True
        assert session.current_frame == 0


# ============================================================================
# AUD-39502: 非 float32 图像原样写入 f2 纹理（字节被当 float16 解释）
# ============================================================================


def _bare_renderer():
    """绕过 __init__（需要 Qt GL 上下文）构造最小渲染器实例。"""
    from src.shared.video.video_async_renderer import VideoAsyncRenderer
    renderer = VideoAsyncRenderer.__new__(VideoAsyncRenderer)
    renderer.ctx = MagicMock()
    renderer._video_texture_pool = None
    renderer._yuv_gpu_uploader = None
    renderer.fb_manager = None
    renderer.utility_shaders = {}
    renderer.current_frame_tex = None
    renderer.current_frame_index = -1
    renderer.frame_size = (0, 0)
    renderer._fallback_frame_tex = None
    renderer._fallback_frame_tex_desc = None
    renderer._convert_buffer = None
    renderer._buffer_size = (0, 0)
    renderer._yuv_y_tex = None
    renderer._yuv_u_tex = None
    renderer._yuv_v_tex = None
    renderer._yuv_uv_tex = None
    renderer._is_yuv_frame = False
    renderer._yuv_composite_fbo = None
    renderer._yuv_composite_tex = None
    renderer.fbo_pair = [None, None]
    renderer.tex_pair = [None, None]
    renderer._initialized = True
    renderer.render_params = {}
    return renderer


class TestAud39502DtypeSafeUpload:
    def test_uint8_normalized_before_f2_write(self) -> None:
        renderer = _bare_renderer()
        texture = MagicMock()
        renderer._video_texture_pool = SimpleNamespace(
            acquire=lambda **kwargs: texture)

        image = np.array([[[0, 128, 255]]], np.uint8)
        fmt = {"dtype": "uint8", "components": 3, "normalized": False,
               "max_value": 255.0, "is_bgr": True}
        renderer._do_upload_frame({
            "image": image, "frame_size": (1, 1), "input_format": fmt})

        expected = (image.astype(np.float32) / 255.0).astype(np.float16)
        texture.write.assert_called_once_with(expected.tobytes())
        assert renderer.current_frame_tex is texture

    def test_uint16_uses_declared_max_value(self) -> None:
        renderer = _bare_renderer()
        texture = MagicMock()
        renderer._video_texture_pool = SimpleNamespace(
            acquire=lambda **kwargs: texture)

        image = np.array([[[0, 512, 1023]]], np.uint16)  # 10-bit
        fmt = {"dtype": "uint16", "components": 3, "normalized": False,
               "max_value": 1023.0, "bit_depth": 10, "is_bgr": True}
        renderer._do_upload_frame({
            "image": image, "frame_size": (1, 1), "input_format": fmt})

        expected = (image.astype(np.float32) / 1023.0).astype(np.float16)
        texture.write.assert_called_once_with(expected.tobytes())

    def test_uint16_without_format_uses_full_scale(self) -> None:
        renderer = _bare_renderer()
        expected_scale = renderer._convert_upload_image(
            np.array([[[65535]]], np.uint16), {}, "f2")
        assert expected_scale == np.array([[[1.0]]], np.float16)

    def test_float32_passthrough_no_rescale(self) -> None:
        renderer = _bare_renderer()
        hdr = np.array([[[0.25, 2.5, 63.0]]], np.float32)
        out = renderer._convert_upload_image(hdr, {}, "f2")
        np.testing.assert_array_equal(out, hdr.astype(np.float16))


# ============================================================================
# AUD-39503: HDR float 帧按逐帧最大值归一化（亮度逐帧重缩放）
# ============================================================================


class TestAud39503NoPerFrameHdrNormalization:
    def test_detect_format_never_reports_per_frame_max(self) -> None:
        renderer = _bare_renderer()
        hdr = np.array([[[0.0, 1.0, 4.2]]], np.float32)
        fmt = renderer._detect_input_format(hdr)
        assert fmt["normalized"] is True
        assert fmt["max_value"] == 1.0, (
            "float 输入禁止用逐帧 max 作为归一化分母（亮度逐帧重缩放）"
        )

    def test_bright_and_dark_hdr_frames_keep_relative_values(self) -> None:
        renderer = _bare_renderer()
        bright = np.full((1, 1, 3), 4.0, np.float32)
        dark = np.full((1, 1, 3), 1.0, np.float32)
        out_bright = renderer._convert_upload_image(
            bright, renderer._detect_input_format(bright), "f2")
        out_dark = renderer._convert_upload_image(
            dark, renderer._detect_input_format(dark), "f2")
        # 亮度比例保持 4:1（旧的逐帧 max 归一化会把两帧都压到 1.0）
        np.testing.assert_allclose(
            float(out_bright[0, 0, 0]) / float(out_dark[0, 0, 0]), 4.0,
            rtol=1e-3)

    def test_detect_format_uint16_depth_detection_unchanged(self) -> None:
        renderer = _bare_renderer()
        fmt = renderer._detect_input_format(
            np.array([[[1000]]], np.uint16))
        assert fmt["bit_depth"] == 10
        assert fmt["max_value"] == 1023.0


# ============================================================================
# AUD-39504: fallback 每帧新建纹理且不释放上一张（GPU 内存泄漏）
# ============================================================================


class TestAud39504FallbackTextureReuse:
    def _pool_none(self):
        return SimpleNamespace(acquire=lambda **kwargs: None)

    def test_fallback_texture_reused_across_frames(self) -> None:
        renderer = _bare_renderer()
        renderer._video_texture_pool = self._pool_none()
        created: list[MagicMock] = []
        renderer.ctx.texture = lambda **kwargs: (
            created.append(MagicMock()) or created[-1])

        image = np.zeros((2, 2, 3), np.uint8)
        fmt = {"dtype": "uint8", "components": 3, "normalized": False,
               "max_value": 255.0, "is_bgr": False}
        payload = {"image": image, "frame_size": (2, 2), "input_format": fmt}

        renderer._do_upload_frame(dict(payload, frame_index=0))
        first = renderer.current_frame_tex
        renderer._do_upload_frame(dict(payload, frame_index=1))

        assert len(created) == 1, "同尺寸 fallback 纹理必须复用（不每帧新建）"
        assert renderer.current_frame_tex is first

    def test_size_change_releases_old_fallback(self) -> None:
        renderer = _bare_renderer()
        renderer._video_texture_pool = self._pool_none()
        created: list[MagicMock] = []
        renderer.ctx.texture = lambda **kwargs: (
            created.append(MagicMock()) or created[-1])

        fmt = {"dtype": "uint8", "components": 3, "normalized": False,
               "max_value": 255.0, "is_bgr": False}
        renderer._do_upload_frame({
            "image": np.zeros((2, 2, 3), np.uint8),
            "frame_size": (2, 2), "input_format": fmt})
        first = renderer.current_frame_tex

        renderer._do_upload_frame({
            "image": np.zeros((4, 4, 3), np.uint8),
            "frame_size": (4, 4), "input_format": fmt})

        assert len(created) == 2
        first.release.assert_called_once(), "换尺寸时旧 fallback 纹理必须释放"
        assert renderer.current_frame_tex is created[1]

    def test_cleanup_releases_fallback_texture(self) -> None:
        renderer = _bare_renderer()
        renderer._video_texture_pool = self._pool_none()
        created: list[MagicMock] = []
        renderer.ctx.texture = lambda **kwargs: (
            created.append(MagicMock()) or created[-1])

        renderer._do_upload_frame({
            "image": np.zeros((2, 2, 3), np.uint8),
            "frame_size": (2, 2),
            "input_format": {"dtype": "uint8", "components": 3,
                             "normalized": False, "max_value": 255.0,
                             "is_bgr": False}})
        texture = renderer._fallback_frame_tex
        assert texture is not None

        renderer._cleanup_gl_resources()
        texture.release.assert_called_once()
        assert renderer._fallback_frame_tex is None
