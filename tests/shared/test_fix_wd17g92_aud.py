"""WD17-G92 修复回归测试：media 代理系统 + 像素格式 + seek（16 条）。

逐条覆盖：
AUD-35405 performance_monitor clear()/reset() 清理不完整
AUD-35500 Subsampling.NONE 与 YUV_444 枚举别名
AUD-35504 generate_proxy_key 缺 topos 三参数
AUD-35505 最小分辨率按轴独立钳制（破坏宽高比 + 上采样小源）
AUD-35506 calculate_target_size 奇数高度未取偶
AUD-35600 成功路径重复探测源文件 / fps=0 误报失败
AUD-35601 CLI 进度监控按 \\n 分行（\\r 结尾的 stats 解析停摆）
AUD-35602 Windows stderr 不排水（改为后台排水线程模型）
AUD-35603 _run_pyav_to_topos 硬编码 limited range
AUD-35607 max_concurrent 从未实施
AUD-35608 on_complete/on_error 持管理器锁调用
AUD-35609 pause() 对进程内编码路径无效
AUD-35610 同源重复入队静默丢弃后来调用方回调
AUD-35616 _source_index 每源仅一条索引
AUD-35701 estimate_seek_cost 反向小距离跳转低估
AUD-35711 RAW BITMAP 缩略图缺 RGB→BGR 转换
"""

from __future__ import annotations

import io
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from src.shared.media.pixel_format import (
    ColorModel,
    PixelFormatRegistry,
    Subsampling,
)
from src.shared.media.proxy import (
    DiskProxyConfig,
    ProxyFormat,
    ProxyResolution,
    ProxyStrategy,
    calculate_proxy_size,
    generate_proxy_key,
)
from src.shared.media.proxy_generator import (
    ProxyGenerationResult,
    ProxyGenerator,
    ProxyProgress,
)
from src.shared.media.proxy_manager import ProxyManager
from src.shared.media.proxy_manifest import ProxyManifest, ProxyManifestEntry, ProxyManifestKey
from src.shared.media.proxy import ProxyStatus
from src.shared.media.seek import estimate_seek_cost


# ======================================================================
# AUD-35405：performance_monitor clear()/reset() 清理不完整
# ======================================================================

class TestAUD35405:
    def _populate(self, monitor) -> None:
        """填充各类统计数据，覆盖 clear/reset 应清理的字段族。"""
        monitor.start_session()
        monitor.track_decode_time(0, 12.0)
        monitor.track_upload_time(0, 3.0)
        monitor.track_frame_drop(0, "late")
        monitor.track_tick_time(9.0)
        monitor.track_process_time(0, 5.0)
        monitor.track_render_submit_time(0, 2.0)
        monitor.track_queue_depth(4)
        monitor.track_seek_time(0, 20.0)
        monitor.track_read_time(0, 1.0)
        monitor.track_cache_hit(0, True)
        monitor.track_renderer_queue_depth(2)
        monitor.track_dropped_preview_frame(3)
        monitor.track_held_frames(5)
        monitor.track_plane_prepare_time(0.5)
        monitor.track_texture_write_time(1.5)
        monitor.track_yuv_render_time(2.5)
        monitor.track_frame_path("gpu")
        monitor.track_coalesced_video_commands(7)
        monitor.track_cpu_download_frame()
        monitor.track_cpu_upload_frame()
        monitor.track_external_texture_frame()
        monitor.track_frame_total_time(33.0)
        # 直接设置 S5.B 降级状态（触发升级需要 30 轮慢 tick，这里
        # 等价注入以验证 clear/reset 的清理语义）
        monitor._degradation_level = 2
        monitor._consecutive_slow_ticks = 17
        monitor._prefetch_lookahead_reduction = 0.5

    def test_clear_resets_all_counters_and_degradation(self) -> None:
        from src.shared.media.performance_monitor import VideoPerformanceMonitor
        monitor = VideoPerformanceMonitor(name="test-clear")
        self._populate(monitor)

        monitor.clear()

        stats = monitor.get_stats()
        assert stats["decode_count"] == 0
        assert stats["total_drops"] == 0
        assert monitor.frames_processed == 0
        # 陈旧降级状态必须一并清理
        assert monitor.get_degradation_level() == 0
        assert monitor.get_prefetch_lookahead_reduction() == 0.0
        with monitor._lock:
            assert monitor._renderer_queue_depths == []
            assert monitor._dropped_preview_frames == 0
            assert monitor._held_frames == []
            assert monitor._plane_prepare_times == []
            assert monitor._texture_write_times == []
            assert monitor._yuv_render_times == []
            assert monitor._frame_paths == []
            assert monitor._coalesced_video_commands == []
            assert monitor._total_frame_times == []
            assert monitor._zero_copy_frames == 0
            assert monitor._cpu_download_frames == 0
            assert monitor._cpu_upload_frames == 0
            assert monitor._external_texture_frames == 0
            assert monitor._consecutive_slow_ticks == 0
        # clear 保持会话状态不变
        assert monitor.is_active is True

    def test_reset_also_clears_session_state(self) -> None:
        from src.shared.media.performance_monitor import VideoPerformanceMonitor
        monitor = VideoPerformanceMonitor(name="test-reset")
        self._populate(monitor)

        monitor.reset()

        assert monitor.is_active is False
        assert monitor.frames_processed == 0
        assert monitor.get_degradation_level() == 0
        stats = monitor.get_stats()
        assert stats["decode_count"] == 0

    def test_early_return_consistent_with_degradation_level(self) -> None:
        """样本不足的早退必须返回当前降级级别，而非硬编码 0。"""
        from src.shared.media.performance_monitor import VideoPerformanceMonitor
        monitor = VideoPerformanceMonitor(name="test-early")
        monitor.start_session()
        monitor._degradation_level = 2
        # tick 样本 < 10：早退路径
        for tick in (9.5, 9.6):
            monitor.track_tick_time(tick)
        assert len(monitor._tick_times) < 10
        assert monitor.check_tick_performance_and_degrade() == 2
        assert monitor.check_tick_performance_and_degrade() == \
            monitor.get_degradation_level()


# ======================================================================
# AUD-35500：Subsampling.NONE 与 YUV_444 枚举别名
# ======================================================================

class TestAUD35500:
    def test_none_is_distinct_member(self) -> None:
        assert Subsampling.NONE is not Subsampling.YUV_444
        assert Subsampling.NONE in list(Subsampling)
        assert Subsampling.NONE.value != Subsampling.YUV_444.value

    def test_from_tuple_unambiguous(self) -> None:
        assert Subsampling.from_tuple((4, 4, 4)) is Subsampling.YUV_444
        assert Subsampling.from_tuple((0, 0, 0)) is Subsampling.NONE

    def test_none_divisors_do_not_crash(self) -> None:
        assert Subsampling.NONE.chroma_width_divisor == 1
        assert Subsampling.NONE.chroma_height_divisor == 1

    def test_get_by_properties_444_excludes_rgb_gray(self) -> None:
        """按 YUV_444 查询不得混入以 NONE 注册的 RGB/GRAY 格式。"""
        results = PixelFormatRegistry.get_by_properties(
            subsampling=Subsampling.YUV_444)
        assert results, "注册表应含 4:4:4 YUV 格式"
        for fmt in results:
            assert fmt.color_model == ColorModel.YUV, fmt.name
        names = {fmt.name for fmt in results}
        assert "rgb24" not in names
        assert "gray" not in names

    def test_get_by_properties_none_returns_only_rgb_gray(self) -> None:
        results = PixelFormatRegistry.get_by_properties(
            subsampling=Subsampling.NONE)
        assert results, "RGB/GRAY 格式应以 NONE 注册"
        for fmt in results:
            assert fmt.color_model in (ColorModel.RGB, ColorModel.GRAY)


# ======================================================================
# AUD-35504：generate_proxy_key 缺 topos 三参数
# ======================================================================

class TestAUD35504:
    def test_key_stable_for_identical_config(self, tmp_path: Path) -> None:
        config = DiskProxyConfig(cache_dir=str(tmp_path))
        k1 = generate_proxy_key("s.mp4", 1.0, 2, config, fps=24.0)
        k2 = generate_proxy_key("s.mp4", 1.0, 2, config, fps=24.0)
        assert k1 == k2

    def test_key_changes_with_topos_params(self, tmp_path: Path) -> None:
        base = DiskProxyConfig(
            format=ProxyFormat.TOPOS_PROXY, cache_dir=str(tmp_path),
            topos_bit_depth=10, topos_keep_444=False, topos_qp=34)

        bit_depth = DiskProxyConfig(
            format=ProxyFormat.TOPOS_PROXY, cache_dir=str(tmp_path),
            topos_bit_depth=12, topos_keep_444=False, topos_qp=34)
        keep_444 = DiskProxyConfig(
            format=ProxyFormat.TOPOS_PROXY, cache_dir=str(tmp_path),
            topos_bit_depth=10, topos_keep_444=True, topos_qp=34)
        qp = DiskProxyConfig(
            format=ProxyFormat.TOPOS_PROXY, cache_dir=str(tmp_path),
            topos_bit_depth=10, topos_keep_444=False, topos_qp=28)

        ref = generate_proxy_key("s.mp4", 1.0, 2, base, fps=24.0)
        assert generate_proxy_key("s.mp4", 1.0, 2, bit_depth, fps=24.0) != ref
        assert generate_proxy_key("s.mp4", 1.0, 2, keep_444, fps=24.0) != ref
        assert generate_proxy_key("s.mp4", 1.0, 2, qp, fps=24.0) != ref


# ======================================================================
# AUD-35505：最小分辨率按轴独立钳制
# ======================================================================

class TestAUD35505:
    def test_half_quarter_regression(self) -> None:
        assert calculate_proxy_size(
            (3840, 2160), (1920, 1080), ProxyStrategy.HALF) == (1920, 1080)
        assert calculate_proxy_size(
            (3840, 2160), (1920, 1080), ProxyStrategy.QUARTER) == (960, 540)

    def test_min_clamp_preserves_aspect_ratio(self) -> None:
        """宽源缩放后低于 min_w 时，钳制不得破坏宽高比（400:1000）。"""
        w, h = calculate_proxy_size(
            (400, 1000), (1920, 1080), ProxyStrategy.HALF)
        assert w <= 400 and h <= 1000, "不得上采样"
        assert abs((w / h) - 0.4) < 0.01, f"宽高比被破坏: {w}x{h}"

    def test_small_source_never_upsampled(self) -> None:
        for strategy in (ProxyStrategy.HALF, ProxyStrategy.QUARTER,
                         ProxyStrategy.AUTO, ProxyStrategy.CUSTOM):
            w, h = calculate_proxy_size(
                (320, 240), (1920, 1080), strategy,
                custom_max=(1920, 1080))
            assert (w, h) == (320, 240), f"{strategy}: {w}x{h}"

    def test_proxy_resolution_calculate_size_clamp(self) -> None:
        # 常规缩放回归（偶数、比例正确）
        assert ProxyResolution.EIGHTH.calculate_size((3840, 2160)) == (480, 270)
        # 小源不上采样、宽高比保持、两维取偶
        w, h = ProxyResolution.EIGHTH.calculate_size((400, 1000))
        assert w % 2 == 0 and h % 2 == 0
        assert w <= 400 and h <= 1000
        assert abs((w / h) - 0.4) < 0.02, f"宽高比被破坏: {w}x{h}"
        # 极小源保持原尺寸（不放大到 320x180）
        assert ProxyResolution.EIGHTH.calculate_size((100, 60)) == (100, 60)


# ======================================================================
# AUD-35506：calculate_target_size 奇数高度未取偶
# ======================================================================

class TestAUD35506:
    def test_odd_source_height_floored_to_even(self) -> None:
        config = DiskProxyConfig(target_height=1080)
        w, h = config.calculate_target_size((1920, 1079))
        assert h % 2 == 0, f"高度必须为偶数: {h}"
        assert h == 1078

    def test_odd_target_height_floored_to_even(self) -> None:
        config = DiskProxyConfig(target_height=1079)
        w, h = config.calculate_target_size((3840, 2160))
        assert h % 2 == 0 and h == 1078
        assert w % 2 == 0

    def test_even_sizes_unchanged(self) -> None:
        config = DiskProxyConfig(target_height=1080)
        assert config.calculate_target_size((1920, 1080)) == (1920, 1080)


# ======================================================================
# AUD-35600 / 35601 / 35602：_run_ffmpeg 监控循环
# ======================================================================

class _FakeFFmpegProcess:
    """替身 ffmpeg 进程：stderr 为 BytesIO，poll 可控制存活轮数。"""

    def __init__(self, stderr_payload: bytes, returncode: int = 0,
                 live_polls: int = 0) -> None:
        self.stderr = io.BytesIO(stderr_payload)
        self.stdin = io.BytesIO()
        self._returncode = returncode
        self._live_polls = live_polls
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self._live_polls > 0:
            self._live_polls -= 1
            return None
        self.returncode = self._returncode
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = self._returncode
        return self._returncode

    def kill(self) -> None:
        self.returncode = -9


class TestAUD35600:
    def test_success_path_no_source_reprobe(self, tmp_path: Path,
                                            monkeypatch) -> None:
        """成功路径只允许探测代理产物一次，源文件不得重复探测。"""
        proxy_file = tmp_path / "proxy.mov"
        proxy_file.write_bytes(b"fake")
        gen = ProxyGenerator(DiskProxyConfig(cache_dir=str(tmp_path)))

        probe_calls: list[str] = []

        def fake_probe(path: str):
            probe_calls.append(path)
            return (640, 360, 24.0, 1.0, 24)

        monkeypatch.setattr(gen, "_probe_source", fake_probe)
        proc = _FakeFFmpegProcess(b"frame=   10 \r\n")
        monkeypatch.setattr(
            subprocess, "Popen",
            lambda *a, **k: proc)

        result = gen._run_ffmpeg(
            ffmpeg_cmd=["ffmpeg"], source_path=str(tmp_path / "src.mp4"),
            proxy_path=str(proxy_file), total_frames=48,
            start_time=time.time(), progress_callback=None,
            source_fps=24.0)

        assert result.success, result.error_message
        assert result.duration_seconds == pytest.approx(2.0)  # 48/24
        assert probe_calls == [str(proxy_file)], \
            "成功路径不得对源文件重复 _probe_source"

    def test_zero_fps_degrades_to_zero_duration(self, tmp_path: Path,
                                                monkeypatch) -> None:
        proxy_file = tmp_path / "proxy.mov"
        proxy_file.write_bytes(b"fake")
        gen = ProxyGenerator(DiskProxyConfig(cache_dir=str(tmp_path)))
        monkeypatch.setattr(
            gen, "_probe_source",
            lambda path: (640, 360, 0.0, 1.0, 24))
        proc = _FakeFFmpegProcess(b"")
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: proc)

        result = gen._run_ffmpeg(
            ffmpeg_cmd=["ffmpeg"], source_path=str(tmp_path / "src.mp4"),
            proxy_path=str(proxy_file), total_frames=10,
            start_time=time.time(), progress_callback=None,
            source_fps=0.0)

        assert result.success, "fps=0 不得把成功误报为失败"
        assert result.duration_seconds == 0.0

    def test_missing_fps_degrades_to_zero_duration(self, tmp_path: Path,
                                                   monkeypatch) -> None:
        proxy_file = tmp_path / "proxy.mov"
        proxy_file.write_bytes(b"fake")
        gen = ProxyGenerator(DiskProxyConfig(cache_dir=str(tmp_path)))
        monkeypatch.setattr(
            gen, "_probe_source",
            lambda path: (640, 360, 24.0, 1.0, 24))
        proc = _FakeFFmpegProcess(b"")
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: proc)

        result = gen._run_ffmpeg(
            ffmpeg_cmd=["ffmpeg"], source_path=str(tmp_path / "src.mp4"),
            proxy_path=str(proxy_file), total_frames=10,
            start_time=time.time(), progress_callback=None,
            source_fps=None)

        assert result.success
        assert result.duration_seconds == 0.0


class TestAUD35601:
    def test_carriage_return_stats_parsed_mid_encode(self, tmp_path: Path,
                                                      monkeypatch) -> None:
        """以 \\r 结尾的 ffmpeg stats 行必须在编码期间被解析为帧数。"""
        proxy_file = tmp_path / "proxy.mov"
        proxy_file.write_bytes(b"fake")
        gen = ProxyGenerator(DiskProxyConfig(cache_dir=str(tmp_path)))
        monkeypatch.setattr(
            gen, "_probe_source",
            lambda path: (640, 360, 24.0, 1.0, 24))
        payload = (
            b"ffmpeg version 7.0 banner\n"
            b"frame=    3 fps= 12 q=28.0 size=     256kB time=00:00:00.12\r"
            b"frame=   42 fps= 14 q=28.0 size=    1024kB time=00:00:01.75\r"
        )
        proc = _FakeFFmpegProcess(payload, live_polls=6)
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: proc)

        progress_frames: list[int] = []

        def on_progress(progress: ProxyProgress) -> None:
            progress_frames.append(progress.current_frame)

        # start_time 回拨 1s，使第一轮循环即满足 0.5s 进度回调节流
        result = gen._run_ffmpeg(
            ffmpeg_cmd=["ffmpeg"], source_path=str(tmp_path / "src.mp4"),
            proxy_path=str(proxy_file), total_frames=100,
            start_time=time.time() - 1.0, progress_callback=on_progress,
            source_fps=24.0)

        assert result.success, result.error_message
        assert progress_frames, "编码期间应至少触发一次进度回调"
        assert 42 in progress_frames, \
            f"\\r 结尾的 stats 帧数未被解析: {progress_frames}"


class TestAUD35602:
    def test_stderr_fully_drained_on_failure(self, tmp_path: Path,
                                              monkeypatch) -> None:
        """失败路径错误信息须来自排水线程收齐的 stderr（含 \\r 行）。"""
        gen = ProxyGenerator(DiskProxyConfig(cache_dir=str(tmp_path)))
        payload = (
            b"Error initializing complex filters\r"
            b"Unknown encoder 'bogus_vcodec'\r\n"
        )
        proc = _FakeFFmpegProcess(payload, returncode=1)
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: proc)

        result = gen._run_ffmpeg(
            ffmpeg_cmd=["ffmpeg"], source_path=str(tmp_path / "src.mp4"),
            proxy_path=str(tmp_path / "proxy.mov"), total_frames=10,
            start_time=time.time(), progress_callback=None,
            source_fps=24.0)

        assert not result.success
        assert "bogus_vcodec" in (result.error_message or ""), \
            "stderr 应被排水线程完整收集并进入错误信息"


# ======================================================================
# AUD-35603：_run_pyav_to_topos 硬编码 limited range
# ======================================================================

def _topos_codec_available() -> bool:
    from src.shared.media.topos_source import check_topos_codec_available
    return check_topos_codec_available()


def _write_range_tagged_h264(path: Path, color_range: int) -> None:
    av = pytest.importorskip("av")
    output = av.open(str(path), "w")
    stream = output.add_stream("libx264", rate=24)
    stream.width = 160
    stream.height = 90
    stream.pix_fmt = "yuvj420p" if color_range == 2 else "yuv420p"
    stream.codec_context.color_range = color_range
    for value in range(6):
        img = np.full((90, 160, 3), (value * 40) % 255, dtype=np.uint8)
        frame = av.VideoFrame.from_ndarray(img, format="rgb24")
        for packet in stream.encode(frame):
            output.mux(packet)
    for packet in stream.encode():
        output.mux(packet)
    output.close()


class TestAUD35603:
    def test_full_range_source_labeled_full(self, tmp_path: Path) -> None:
        if not _topos_codec_available():
            pytest.skip("native topos codec unavailable")
        source = tmp_path / "full.mp4"
        _write_range_tagged_h264(source, color_range=2)

        generator = ProxyGenerator(DiskProxyConfig(
            format=ProxyFormat.TOPOS_PROXY,
            target_height=72,
            cache_dir=str(tmp_path / "proxy-cache"),
        ))
        result = generator.generate(str(source))
        assert result.success, result.error_message

        from src.shared.media.topos_source import ToposMediaSource
        src = ToposMediaSource(result.proxy_path)
        src.open()
        try:
            md = src.probe()
        finally:
            src.close()
        assert str(md.color_range).lower() == "full", \
            "全量程源必须如实标注 full，不得硬编码 limited"

    def test_limited_range_source_still_limited(self, tmp_path: Path) -> None:
        if not _topos_codec_available():
            pytest.skip("native topos codec unavailable")
        source = tmp_path / "limited.mp4"
        _write_range_tagged_h264(source, color_range=1)

        generator = ProxyGenerator(DiskProxyConfig(
            format=ProxyFormat.TOPOS_PROXY,
            target_height=72,
            cache_dir=str(tmp_path / "proxy-cache"),
        ))
        result = generator.generate(str(source))
        assert result.success, result.error_message

        from src.shared.media.topos_source import ToposMediaSource
        src = ToposMediaSource(result.proxy_path)
        src.open()
        try:
            md = src.probe()
        finally:
            src.close()
        assert str(md.color_range).lower() == "limited"


# ======================================================================
# AUD-35607 / 35608 / 35610：ProxyManager 并发 / 锁序 / 回调合并
# ======================================================================

def _patch_generate_success(monkeypatch, delay: float = 0.0) -> dict:
    """把 ProxyGenerator.generate 换成可观测的假实现。

    假实现落盘代理产物（is_proxy_valid 需要）、触发一次进度回调，
    并维护 AUD-35609 的进程内编码标志（与真实 generate() 的
    置位/复位语义一致）。
    """
    state = {"active": 0, "peak": 0}
    guard = threading.Lock()

    def fake_generate(self, source_path, progress_callback=None):
        with self._lock:
            self._inprocess_encode = True
        with guard:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            if delay:
                time.sleep(delay)
            if progress_callback is not None:
                progress_callback(ProxyProgress(
                    percent=50.0, current_frame=1, total_frames=2))
            proxy_path = source_path + ".proxy"
            Path(proxy_path).write_bytes(b"fake-proxy")
            return ProxyGenerationResult(
                success=True, source_path=source_path,
                proxy_path=proxy_path)
        finally:
            with guard:
                state["active"] -= 1
            with self._lock:
                self._inprocess_encode = False

    monkeypatch.setattr(ProxyGenerator, "generate", fake_generate)
    return state


def _wait_task_terminal(manager: ProxyManager, task_id: str,
                        timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = manager.get_task_status(task_id)
        if status in ("complete", "failed", "cancelled"):
            return status
        time.sleep(0.05)
    raise AssertionError(
        f"task {task_id} not terminal after {timeout}s: "
        f"{manager.get_task_status(task_id)}")


class TestAUD35607:
    def test_max_concurrent_enforced(self, tmp_path: Path, monkeypatch) -> None:
        state = _patch_generate_success(monkeypatch, delay=0.4)
        manager = ProxyManager(
            DiskProxyConfig(cache_dir=str(tmp_path)), max_concurrent=1)

        t1 = manager.queue_proxy_generation("a", str(tmp_path / "v1.mp4"))
        t2 = manager.queue_proxy_generation("b", str(tmp_path / "v2.mp4"))
        assert t1 and t2

        assert _wait_task_terminal(manager, t1) == "complete"
        assert _wait_task_terminal(manager, t2) == "complete"
        assert state["peak"] == 1, \
            f"max_concurrent=1 未生效，峰值并发 {state['peak']}"

    def test_concurrency_slots_released_on_task_completion(
            self, tmp_path: Path, monkeypatch) -> None:
        _patch_generate_success(monkeypatch, delay=0.05)
        manager = ProxyManager(
            DiskProxyConfig(cache_dir=str(tmp_path)), max_concurrent=1)
        ids = [manager.queue_proxy_generation(f"a{i}", str(tmp_path / f"v{i}.mp4"))
               for i in range(4)]
        for task_id in ids:
            assert _wait_task_terminal(manager, task_id) == "complete"
        # 槽位释放后可用配额恢复满值（max_concurrent=1 → 恰好可取一次）
        assert manager._concurrency_slots.acquire(timeout=1.0), \
            "任务完成后并发槽位未释放"
        manager._concurrency_slots.release()


class TestAUD35608:
    def test_on_complete_called_outside_manager_lock(self, tmp_path: Path,
                                                     monkeypatch) -> None:
        _patch_generate_success(monkeypatch)
        manager = ProxyManager(
            DiskProxyConfig(cache_dir=str(tmp_path)), max_concurrent=2)
        observed: dict = {}

        def on_complete(result: ProxyGenerationResult) -> None:
            is_owned = getattr(manager._lock, "_is_owned", None)
            observed["manager_lock_held"] = (
                bool(is_owned()) if callable(is_owned) else None)

        task_id = manager.queue_proxy_generation(
            "a", str(tmp_path / "v.mp4"), on_complete=on_complete)
        assert task_id
        assert _wait_task_terminal(manager, task_id) == "complete"
        if observed.get("manager_lock_held") is not None:
            assert observed["manager_lock_held"] is False, \
                "on_complete 不得在持有管理器锁时被调用"

    def test_on_error_called_outside_manager_lock(self, tmp_path: Path,
                                                  monkeypatch) -> None:
        def fake_generate(self, source_path, progress_callback=None):
            return ProxyGenerationResult(
                success=False, source_path=source_path,
                error_message="boom")

        monkeypatch.setattr(ProxyGenerator, "generate", fake_generate)
        manager = ProxyManager(
            DiskProxyConfig(cache_dir=str(tmp_path)), max_concurrent=2)
        observed: dict = {}

        def on_error(message: str) -> None:
            is_owned = getattr(manager._lock, "_is_owned", None)
            observed["manager_lock_held"] = (
                bool(is_owned()) if callable(is_owned) else None)

        task_id = manager.queue_proxy_generation(
            "a", str(tmp_path / "v.mp4"), on_error=on_error)
        assert task_id
        assert _wait_task_terminal(manager, task_id) == "failed"
        if observed.get("manager_lock_held") is not None:
            assert observed["manager_lock_held"] is False

    def test_sync_callback_for_valid_proxy_outside_lock(self, tmp_path: Path,
                                                        monkeypatch) -> None:
        """queue 时发现已有有效代理的同步 on_complete 也不得持锁调用。"""
        _patch_generate_success(monkeypatch)
        manager = ProxyManager(
            DiskProxyConfig(cache_dir=str(tmp_path)), max_concurrent=2)
        source = str(tmp_path / "v.mp4")

        t1 = manager.queue_proxy_generation("a", source)
        assert t1
        assert _wait_task_terminal(manager, t1) == "complete"
        assert manager.is_proxy_valid(source)

        observed: dict = {}

        def on_complete(result: ProxyGenerationResult) -> None:
            is_owned = getattr(manager._lock, "_is_owned", None)
            observed["fired"] = True
            observed["manager_lock_held"] = (
                bool(is_owned()) if callable(is_owned) else None)

        assert manager.queue_proxy_generation("b", source,
                                              on_complete=on_complete) is None
        assert observed.get("fired") is True
        if observed.get("manager_lock_held") is not None:
            assert observed["manager_lock_held"] is False


class TestAUD35610:
    def test_duplicate_enqueue_merges_callbacks(self, tmp_path: Path,
                                                monkeypatch) -> None:
        _patch_generate_success(monkeypatch, delay=0.3)
        manager = ProxyManager(
            DiskProxyConfig(cache_dir=str(tmp_path)), max_concurrent=2)
        source = str(tmp_path / "v.mp4")
        fired: list[str] = []

        t1 = manager.queue_proxy_generation(
            "a", source,
            on_complete=lambda r: fired.append("first"))
        t2 = manager.queue_proxy_generation(
            "b", source,
            on_complete=lambda r: fired.append("second"),
            on_error=lambda m: fired.append("second-error"),
            on_progress=lambda p: fired.append("second-progress"))
        assert t1 and t2 == t1, "同源重复入队应返回既有任务 ID"

        assert _wait_task_terminal(manager, t1) == "complete"
        # 两个调用方的完成回调都必须被调用，不得丢弃后来者
        assert "first" in fired and "second" in fired, fired
        assert "second-progress" in fired, fired
        assert "second-error" not in fired, fired


# ======================================================================
# AUD-35609：pause() 对进程内编码路径无效
# ======================================================================

class TestAUD35609:
    def test_cooperative_pause_blocks_and_resumes(self) -> None:
        gen = ProxyGenerator(DiskProxyConfig())
        gen._inprocess_encode = True
        assert gen.pause() is True

        waiter = threading.Thread(target=gen._wait_if_paused, daemon=True)
        waiter.start()
        time.sleep(0.3)
        assert waiter.is_alive(), "暂停期间检查点应阻塞让出 CPU"

        assert gen.resume() is True
        waiter.join(timeout=2.0)
        assert not waiter.is_alive(), "resume 后检查点应立即放行"

    def test_cancel_breaks_pause_wait(self) -> None:
        gen = ProxyGenerator(DiskProxyConfig())
        gen._inprocess_encode = True
        assert gen.pause() is True

        waiter = threading.Thread(target=gen._wait_if_paused, daemon=True)
        waiter.start()
        time.sleep(0.2)
        assert waiter.is_alive()
        gen.cancel()
        waiter.join(timeout=2.0)
        assert not waiter.is_alive(), "取消必须立即解除暂停等待"

    def test_pause_false_when_idle(self) -> None:
        gen = ProxyGenerator(DiskProxyConfig())
        assert gen._inprocess_encode is False
        assert gen.pause() is False
        assert gen.resume() is False

    def test_manager_pause_all_reaches_inprocess_path(
            self, tmp_path: Path, monkeypatch) -> None:
        """pause_all_generation 经 generator.pause() 生效于进程内路径。"""
        started = threading.Event()
        release = threading.Event()

        def fake_generate(self, source_path, progress_callback=None):
            # 与真实 generate() 一致：进程内编码期间置位标志
            with self._lock:
                self._inprocess_encode = True
            started.set()
            release.wait(timeout=10.0)
            with self._lock:
                self._inprocess_encode = False
            proxy_path = source_path + ".proxy"
            Path(proxy_path).write_bytes(b"fake-proxy")
            return ProxyGenerationResult(
                success=True, source_path=source_path,
                proxy_path=proxy_path)

        monkeypatch.setattr(ProxyGenerator, "generate", fake_generate)
        manager = ProxyManager(
            DiskProxyConfig(cache_dir=str(tmp_path)), max_concurrent=2)
        task_id = manager.queue_proxy_generation(
            "a", str(tmp_path / "v.mp4"))
        assert started.wait(timeout=5.0)
        time.sleep(0.1)  # 等 _inprocess_encode 置位可见

        assert manager.pause_all_generation() >= 1, \
            "进程内编码路径必须能被 pause_all_generation 挂起"
        release.set()
        assert _wait_task_terminal(manager, task_id) == "complete"
        manager.resume_all_generation()


# ======================================================================
# AUD-35616：_source_index 每源仅一条索引
# ======================================================================

def _make_entry(source: str, codec_policy: str, *,
                status: ProxyStatus = ProxyStatus.READY,
                updated_at: float = 100.0) -> ProxyManifestEntry:
    key = ProxyManifestKey(
        source_path=source, source_mtime_ns=1, source_size=2,
        codec_policy=codec_policy, resolution_policy="HALF",
        app_version="1")
    return ProxyManifestEntry(
        key=key, status=status, proxy_path=f"/proxy/{codec_policy}.mov",
        width=960, height=540, codec=codec_policy, pixel_format="yuv422p10le",
        created_at=updated_at - 1, updated_at=updated_at)


class TestAUD35616:
    def test_invalidate_by_source_covers_all_policies(self, tmp_path: Path) -> None:
        manifest = ProxyManifest(tmp_path)
        source = "/media/a.mp4"
        manifest.upsert_entry(_make_entry(source, "prores_proxy",
                                          updated_at=100.0))
        manifest.upsert_entry(_make_entry(source, "topos_proxy",
                                          updated_at=200.0))
        assert manifest.entry_count == 2

        manifest.invalidate_by_source(source, reason="source modified")

        # 两条策略条目都必须被失效，而非仅索引指向的最后一条
        for key_hash, entry in manifest._entries.items():
            assert entry.status is ProxyStatus.STALE, key_hash

    def test_remove_entry_covers_all_policies(self, tmp_path: Path) -> None:
        manifest = ProxyManifest(tmp_path)
        source = "/media/a.mp4"
        manifest.upsert_entry(_make_entry(source, "prores_proxy"))
        manifest.upsert_entry(_make_entry(source, "topos_proxy"))

        manifest.remove_entry(source)
        assert manifest.entry_count == 0
        assert manifest.get_entry(source) is None

    def test_reload_keeps_all_policy_entries(self, tmp_path: Path) -> None:
        manifest = ProxyManifest(tmp_path)
        source = "/media/a.mp4"
        manifest.upsert_entry(_make_entry(source, "prores_proxy",
                                          updated_at=100.0))
        manifest.upsert_entry(_make_entry(source, "topos_proxy",
                                          updated_at=200.0))
        manifest.save()

        reloaded = ProxyManifest(tmp_path)
        reloaded.load()
        assert reloaded.entry_count == 2, "重载不得挤掉多策略条目"
        # get_entry 返回 updated_at 最新的那条
        entry = reloaded.get_entry(source)
        assert entry is not None
        assert entry.codec == "topos_proxy"

    def test_stale_policy_entry_not_resurrected_after_reload(
            self, tmp_path: Path) -> None:
        """失效后的旧策略条目不得在重载后重新可见为 READY。"""
        manifest = ProxyManifest(tmp_path)
        source = "/media/a.mp4"
        old = _make_entry(source, "prores_proxy", updated_at=100.0)
        manifest.upsert_entry(old)
        manifest.upsert_entry(_make_entry(source, "topos_proxy",
                                          updated_at=200.0))
        manifest.invalidate_by_source(source, reason="test")
        manifest.save()

        reloaded = ProxyManifest(tmp_path)
        reloaded.load()
        assert reloaded.has_ready_proxy(source) is False, \
            "重载后陈旧条目不得重新以 READY 可见"


# ======================================================================
# AUD-35701：estimate_seek_cost 反向小距离跳转低估
# ======================================================================

class TestAUD35701:
    def test_reverse_small_jump_returns_gop_cost(self) -> None:
        """current=100 → target=95, gop=12：必须回到 keyframe 84 解码 11 帧。"""
        assert estimate_seek_cost(100, 95, "h264", keyframe_interval=12) == 11

    def test_reverse_jump_on_keyframe_target(self) -> None:
        # target=96 恰为 keyframe：decode_forward=0 → 至少 1
        assert estimate_seek_cost(100, 96, "h264", keyframe_interval=12) == 1

    def test_forward_small_jump_still_sequential(self) -> None:
        assert estimate_seek_cost(100, 105, "h264", keyframe_interval=12) == 5

    def test_forward_large_jump_returns_gop_cost(self) -> None:
        # 100 → 150：keyframe_before=144，当前帧 100 <= 144 → seek 分支
        assert estimate_seek_cost(100, 150, "h264", keyframe_interval=12) == 6

    def test_intra_codec_direct(self) -> None:
        assert estimate_seek_cost(100, 95, "prores") == 1


# ======================================================================
# AUD-35711：RAW BITMAP 缩略图缺 RGB→BGR 转换
# ======================================================================

class TestAUD35711:
    def test_bitmap_thumb_converted_to_bgr(self, tmp_path: Path,
                                           monkeypatch) -> None:
        rawpy = pytest.importorskip("rawpy")
        pytest.importorskip("cv2")
        from src.shared.media import thumbnail_provider as tp

        # 构造红蓝不对称的 RGB 位图：R=255, B=10
        rgb = np.zeros((32, 32, 3), dtype=np.uint8)
        rgb[..., 0] = 255  # R
        rgb[..., 2] = 10   # B

        thumb = type("Thumb", (), {})()
        thumb.format = rawpy.ThumbFormat.BITMAP
        thumb.data = rgb

        class _FakeRaw:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_thumb(self):
                return thumb

            def postprocess(self, **kw):  # pragma: no cover - 不应走到
                raise AssertionError("BITMAP 分支不应回落 postprocess")

        monkeypatch.setattr(tp.rawpy, "imread", lambda path: _FakeRaw())

        request = tp.ThumbnailRequest(str(tmp_path / "photo.nef"), None, 64)
        img_bgr = request._load_fast_thumbnail(str(tmp_path / "photo.nef"))

        assert img_bgr is not None
        # BGR 出口：通道 2 应为 R（255），通道 0 应为 B（10）
        assert int(img_bgr[..., 2].mean()) == 255
        assert int(img_bgr[..., 0].mean()) == 10

    def test_jpeg_thumb_branch_unchanged(self, tmp_path: Path, monkeypatch) -> None:
        """JPEG 分支（imdecode 产出 BGR）行为不受影响。"""
        rawpy = pytest.importorskip("rawpy")
        cv2 = pytest.importorskip("cv2")
        from src.shared.media import thumbnail_provider as tp

        bgr_jpeg = np.zeros((32, 32, 3), dtype=np.uint8)
        bgr_jpeg[..., 0] = 10   # B
        bgr_jpeg[..., 2] = 255  # R
        ok, buf = cv2.imencode(".jpg", bgr_jpeg)
        assert ok

        thumb = type("Thumb", (), {})()
        thumb.format = rawpy.ThumbFormat.JPEG
        thumb.data = buf.tobytes()

        class _FakeRaw:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_thumb(self):
                return thumb

        monkeypatch.setattr(tp.rawpy, "imread", lambda path: _FakeRaw())

        request = tp.ThumbnailRequest(str(tmp_path / "photo.nef"), None, 64)
        img_bgr = request._load_fast_thumbnail(str(tmp_path / "photo.nef"))
        assert img_bgr is not None
        # JPEG 分支直接 imdecode 为 BGR：R 均值显著高于 B
        assert int(img_bgr[..., 2].mean()) > 200
        assert int(img_bgr[..., 0].mean()) < 60
