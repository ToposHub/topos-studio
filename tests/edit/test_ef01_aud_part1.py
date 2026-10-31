"""EF-01 part 1 — src/features/edit/core Suggestion 级审计修复回归（24 条）。

每条用例对应 REMEDIATION_PLAN 的一个 AUD ID；修复被回退时对应用例必须失败。

覆盖清单：
    AUD-15200 (docstring 漂移)          AUD-15204 (get/reset 竞态返回 None)
    AUD-15423 (目录遍历无界阻塞)        AUD-15505 (mixer 锁内 close 解码器)
    AUD-15506 (mix 热路径逐块 import)   AUD-15511 (反压只睡首次)
    AUD-15514 (stop 无界 wait)          AUD-15601 (WaveformCache 无锁单例)
    AUD-15605 (cancel_all 无界 wait)    AUD-15606 (peaks_max 语义文档)
    AUD-15607 (失败清新回调竞态)        AUD-15610 (record/stop 无并发保护)
    AUD-15611 (Touch release 未实现)    AUD-15801 (promote 兜底泄漏首建轨)
    AUD-15802 (no-op 样式误伤锁定 cue)  AUD-15803 (merge 缺相邻/同 clip 校验)
    AUD-16111 (FFMetadata 文档)         AUD-16112 (少于 3 章节静默)
    AUD-16316 (4 元组 crop 崩溃)        AUD-16317 (astype 后冗余 copy)
    AUD-16416 (frame_ready 无 emit)     AUD-16419 (_select_quality 参数未用)
    AUD-16422 (状态字段锁纪律不一致)    AUD-16504 (placeholder 拒绝注释漂移)
"""

from __future__ import annotations

import inspect
import threading
import time
import types
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

pytestmark = pytest.mark.edit_core


# ================================================================
# AUD-15200: 模块 docstring 职责清单与实际导出对齐
# ================================================================

class TestAud15200:
    def test_docstring_responsibilities_match_exports(self) -> None:
        import src.features.edit.core as pkg

        doc = pkg.__doc__ or ""
        # 漂移已修正：新模块在清单中，已不再导出的 timeline_ops 移除
        assert "edit_project_io" in doc
        assert "timeline_render_plan" in doc
        assert "timeline_ops" not in doc

        # 清单里的每个 "- xxx:" 模块都必须真实出现在 __all__ 相关导入中
        listed = {
            line.split(":")[0].lstrip("- ").strip()
            for line in doc.splitlines()
            if line.strip().startswith("- ") and ":" in line
        }
        expected = {
            "project", "serialization", "edit_project_io", "clip_params",
            "timeline_v2", "timeline_render_plan", "commands", "selection",
            "audio_waveform",
        }
        assert listed == expected
        # __all__ 与清单模块的实际导出非空对应
        assert len(pkg.__all__) > 0
        assert "EditProjectIO" in pkg.__all__
        assert "TimelineRenderPlan" in pkg.__all__


# ================================================================
# AUD-15204: get/reset 双检锁竞态不得向调用方返回 None
# ================================================================

class TestAud15204:
    def test_get_capabilities_never_returns_none_when_reset_races_upgrade(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """模拟 reset 恰在 unknown→真探测升级分支加锁前清空缓存。

        旧实现：`current = _cached` 读到 None → 跳过探测 → 返回 None。
        新实现：current 为 None 时就地补探测，永不返回 None。
        """
        from src.features.edit.core import audio_capabilities as ac

        def fake_probe(check_output_device: bool = True):
            # 无 Qt 依赖的"真实探测"结果（output_device.unknown=False）
            return ac.AudioCapabilities(
                numpy=ac.CapabilityResult(True),
                pyav=ac.CapabilityResult(True),
                scipy=ac.CapabilityResult(True),
                output_device=ac.CapabilityResult(True, "fake-device"),
            )

        monkeypatch.setattr(ac, "probe_audio_capabilities", fake_probe)

        # 预置缓存为 unknown 占位 → 触发升级分支
        unknown_caps = ac.AudioCapabilities(
            numpy=ac.CapabilityResult(True),
            pyav=ac.CapabilityResult(True),
            scipy=ac.CapabilityResult(True),
            output_device=ac.CapabilityResult(False, "跳过探测（unknown）", unknown=True),
        )
        monkeypatch.setattr(ac, "_cached", unknown_caps, raising=False)

        class ResettingLockProxy:
            """第一次 __enter__（升级分支加锁）前把缓存清空，模拟 reset 竞态。"""

            def __init__(self) -> None:
                self._real = ac._cache_lock
                self._entered = 0

            def __enter__(self) -> None:
                self._entered += 1
                if self._entered == 1:
                    ac._cached = None  # reset 恰在此刻发生
                self._real.acquire()

            def __exit__(self, *args: object) -> None:
                self._real.release()

        monkeypatch.setattr(ac, "_cache_lock", ResettingLockProxy())

        result = ac.get_audio_capabilities(check_output_device=True)
        assert result is not None, "get/reset 竞态下不得向调用方返回 None"
        assert not result.output_device.unknown

    def test_reset_then_get_returns_fresh_snapshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.features.edit.core import audio_capabilities as ac

        monkeypatch.setattr(ac, "_cached", None, raising=False)
        first = ac.get_audio_capabilities(check_output_device=False)
        assert first is not None
        ac.reset_audio_capabilities_cache()
        second = ac.get_audio_capabilities(check_output_device=False)
        assert second is not None


# ================================================================
# AUD-15423: probe_dir_size_mb 遍历时间预算
# ================================================================

class TestAud15423:
    def test_probe_dir_size_measures_known_files(self, tmp_path: Path) -> None:
        from src.features.edit.core.audio_health import probe_dir_size_mb

        (tmp_path / "a.bin").write_bytes(b"x" * 4096)
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.bin").write_bytes(b"y" * 1024)

        size_mb = probe_dir_size_mb(str(tmp_path))
        assert size_mb is not None
        assert size_mb == pytest.approx((4096 + 1024) / (1024.0 ** 2), rel=1e-3)

    def test_probe_missing_dir_returns_none(self) -> None:
        from src.features.edit.core.audio_health import probe_dir_size_mb

        assert probe_dir_size_mb("") is None
        assert probe_dir_size_mb("/nonexistent_ef01_dir_xyz") is None

    def test_zero_budget_stops_walk_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """max_seconds=0 时立刻停止遍历并告警——回退（无预算参数）时本用例
        因 TypeError 失败。"""
        from src.features.edit.core.audio_health import probe_dir_size_mb

        for i in range(200):
            (tmp_path / f"f{i:03d}.bin").write_bytes(b"z" * 100)

        import logging
        with caplog.at_level(logging.WARNING, logger="src.features.edit.core.audio_health"):
            result = probe_dir_size_mb(str(tmp_path), max_seconds=0.0)

        assert result is not None  # 返回部分统计而非 None/挂起
        assert any("budget" in r.message.lower() for r in caplog.records)


# ================================================================
# AUD-15505: decoder.close() 移出 mixer 锁
# ================================================================

class TestAud15505:
    def _make_mixer(self) -> "audio_playback.AudioMixer":  # type: ignore[name-defined]
        from src.features.edit.core.audio_playback import AudioMixer

        return AudioMixer()

    def test_replace_track_closes_old_decoder_outside_lock(self) -> None:
        from src.features.edit.core.audio_playback import AudioDecoder

        mixer = self._make_mixer()
        old_decoder = Mock(spec=AudioDecoder)

        def assert_lock_free() -> None:
            # close 执行时 mixer 锁必须已释放（锁内 tryLock 必失败）
            assert mixer._mutex.tryLock(), (
                "AUD-15505: decoder.close() 仍在 mixer 锁内执行"
            )
            mixer._mutex.unlock()

        old_decoder.close.side_effect = assert_lock_free
        new_decoder = Mock(spec=AudioDecoder)

        mixer.add_track("t1", old_decoder)
        mixer.add_track("t1", new_decoder, replace_existing=True)

        old_decoder.close.assert_called_once()

    def test_remove_track_closes_decoder_outside_lock(self) -> None:
        from src.features.edit.core.audio_playback import AudioDecoder

        mixer = self._make_mixer()
        decoder = Mock(spec=AudioDecoder)

        def assert_lock_free() -> None:
            assert mixer._mutex.tryLock(), (
                "AUD-15505: remove_track 的 decoder.close() 仍在锁内执行"
            )
            mixer._mutex.unlock()

        decoder.close.side_effect = assert_lock_free
        mixer.add_track("t1", decoder)
        mixer.remove_track("t1")
        decoder.close.assert_called_once()
        assert "t1" not in mixer._decoders


# ================================================================
# AUD-15506: mix 热路径——模块级导入 + solo 闭包缓存
# ================================================================

class TestAud15506:
    def test_hotpath_dependencies_imported_at_module_level(self) -> None:
        from src.features.edit.core import audio_playback

        for name in (
            "adapt_channels", "pan_gains_for_channels",
            "apply_clip_fade", "balance_gains",
        ):
            assert hasattr(audio_playback, name), (
                f"AUD-15506: {name} 应为模块级导入"
            )

    def test_solo_closure_cached_and_invalidated(self) -> None:
        from src.features.edit.core.audio_playback import (
            AudioDecoder,
            AudioMixer,
            AudioTrackState,
        )

        mixer = AudioMixer()
        mixer.register_bus_track("bus")
        decoder = Mock(spec=AudioDecoder)
        decoder.decode_samples_for_frame.return_value = None
        mixer.add_track("a", decoder, AudioTrackState(track_id="a", solo=True))
        mixer.set_track_routing("a", output_bus_id="bus", sends=[])

        mixer.mix_samples(0, 24.0, 480)
        cache1 = mixer._solo_closure_cache
        assert cache1 is not None
        assert "a" in cache1  # solo 轨自身必可听

        # 拓扑未变 → 复用缓存对象（不重算）
        mixer.mix_samples(480, 24.0, 480)
        assert mixer._solo_closure_cache is cache1

        # 路由变化 → 缓存失效 → 下次重算出新对象
        mixer.set_track_routing("a", output_bus_id=None, sends=[])
        assert mixer._solo_closure_cache is None
        mixer.mix_samples(960, 24.0, 480)
        assert mixer._solo_closure_cache is not None
        assert mixer._solo_closure_cache is not cache1

    def test_solo_state_change_invalidates_cache(self) -> None:
        from src.features.edit.core.audio_playback import (
            AudioDecoder,
            AudioMixer,
            AudioTrackState,
        )

        mixer = AudioMixer()
        decoder = Mock(spec=AudioDecoder)
        decoder.decode_samples_for_frame.return_value = None
        mixer.add_track("a", decoder)
        mixer.mix_samples(0, 24.0, 480)
        assert mixer._solo_closure_cache is None  # 无 solo → 从不缓存

        mixer.update_track_solo("a", True)
        mixer.mix_samples(0, 24.0, 480)
        cache1 = mixer._solo_closure_cache
        assert cache1 is not None and "a" in cache1

        mixer.update_track_solo("a", False)
        assert mixer._solo_closure_cache is None

    def test_declick_function_cached_across_calls(self) -> None:
        from src.features.edit.core import audio_playback
        from src.features.edit.core.audio_playback import AudioMixer

        mixer = AudioMixer()
        assert mixer._declick_edges_fn is None

        gate = np.array([0.0, 1.0, 1.0, 0.0], dtype=np.float32)
        out1 = mixer._apply_declick(gate, 48000.0)
        assert out1.shape == gate.shape
        expected = audio_playback  # 引用模块，确认与源实现等价
        from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer

        ref = AudioGraphRenderer._apply_declick_edges(
            np.array([0.0, 1.0, 1.0, 0.0], dtype=np.float32), 48000.0,
        )
        assert np.allclose(out1, ref)

        # 第二次调用复用缓存的函数引用
        fn = mixer._declick_edges_fn
        assert fn is not None
        out2 = mixer._apply_declick(gate, 48000.0)
        assert mixer._declick_edges_fn is fn
        assert np.allclose(out2, ref)
        del expected  # 仅防未使用告警


# ================================================================
# AUD-15511: 反压分支每个阻塞周期都退避
# ================================================================

class TestAud15511:
    def test_backpressure_sleeps_on_every_blocked_cycle(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.features.edit.core import audio_playback
        from src.features.edit.core.audio_playback import AudioPlaybackThread

        thread = AudioPlaybackThread()
        sleeps: list[int] = []
        traces: list[tuple] = []
        monkeypatch.setattr(audio_playback, "playback_trace",
                            lambda *a, **k: traces.append(a))
        thread.msleep = lambda ms: sleeps.append(ms)  # type: ignore[method-assign]

        output = Mock()
        output.bytes_per_frame.return_value = 4
        output.bytes_free.return_value = 0  # 永远反压
        thread._output = output
        thread._samples_per_frame = 480
        thread._next_chunk_samples = Mock(return_value=1920)  # type: ignore[method-assign]
        thread._write_blocked_logged = False
        thread._sample_chunk_fraction = 0.0

        assert thread._write_next_output_chunk() is False
        assert thread._write_next_output_chunk() is False

        # 每个阻塞轮次都退避；日志只记录首次
        assert sleeps == [5, 5], f"AUD-15511: 反压退避应逐轮执行，实际 {sleeps}"
        assert len(traces) == 1

    def test_backpressure_resets_after_successful_write(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.features.edit.core import audio_playback
        from src.features.edit.core.audio_playback import AudioPlaybackThread

        thread = AudioPlaybackThread()
        sleeps: list[int] = []
        monkeypatch.setattr(audio_playback, "playback_trace", lambda *a, **k: None)
        thread.msleep = lambda ms: sleeps.append(ms)  # type: ignore[method-assign]
        thread._clock = Mock()  # 成功写入后 add_samples 计入时钟

        output = Mock()
        output.bytes_per_frame.return_value = 4
        output.bytes_free.return_value = 1 << 20  # 空间充足
        output.write_samples.return_value = 1920 * 4
        thread._output = output
        thread._samples_per_frame = 480
        thread._next_chunk_samples = Mock(return_value=1920)  # type: ignore[method-assign]
        thread._write_blocked_logged = True  # 上轮被阻塞过
        thread._sample_chunk_fraction = 0.0
        ring = Mock()
        ring.filled = 1 << 20
        ring.peek.return_value = np.zeros((2, 1920), dtype=np.float32)
        thread._ring = ring
        thread._pending_fade_in = False
        thread._latest_per_track_peaks = {}
        thread._last_track_level_emit_ms = 0.0

        assert thread._write_next_output_chunk() is True
        assert sleeps == []
        assert thread._write_blocked_logged is False  # 成功后复位


# ================================================================
# AUD-15514: manager.stop() 有界 wait
# ================================================================

class TestAud15514:
    def test_stop_uses_bounded_first_wait(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.features.edit.core.audio_playback import AudioPlaybackManager

        manager = AudioPlaybackManager()
        fake_thread = Mock()
        fake_thread.wait.return_value = True
        fake_thread.isRunning.return_value = False
        manager._playback_thread = fake_thread
        manager._scrub_preview_worker = None
        manager._scrub_preview_output = None

        manager.stop()

        first_wait = fake_thread.wait.call_args_list[0]
        assert first_wait.args and first_wait.args[0] == 1000, (
            "AUD-15514: stop() 首次 wait 必须带超时"
        )
        assert manager._playback_thread is None

    def test_stop_warns_when_thread_does_not_stop_in_time(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        from src.features.edit.core.audio_playback import AudioPlaybackManager

        manager = AudioPlaybackManager()
        fake_thread = Mock()
        fake_thread.wait.return_value = False  # 1s 超时后仍活着
        fake_thread.isRunning.return_value = True
        manager._playback_thread = fake_thread
        manager._scrub_preview_worker = None
        manager._scrub_preview_output = None

        with caplog.at_level(logging.WARNING, logger="src.features.edit.core.audio_playback"):
            manager.stop()

        assert fake_thread.wait.call_count == 2  # 有界 + 收尾
        assert any("did not stop" in r.message for r in caplog.records)
        assert manager._playback_thread is None


# ================================================================
# AUD-15601: WaveformCache 单例无锁竞态
# ================================================================

class TestAud15601:
    @pytest.fixture(autouse=True)
    def _reset_singleton(self) -> None:
        from src.features.edit.core.audio_waveform import WaveformCache

        WaveformCache._instance = None
        yield
        WaveformCache._instance = None

    def test_concurrent_first_construction_yields_single_instance(self) -> None:
        from src.features.edit.core.audio_waveform import WaveformCache

        barrier = threading.Barrier(8)
        instances: list[WaveformCache] = []  # type: ignore[name-defined]

        def worker() -> None:
            barrier.wait()
            instances.append(WaveformCache())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(instances) == 8
        assert all(inst is instances[0] for inst in instances)

    def test_new_holds_class_mutex_during_creation(self) -> None:
        from src.features.edit.core.audio_waveform import WaveformCache

        source = inspect.getsource(WaveformCache.__new__)
        assert "cls._mutex.lock()" in source and "cls._mutex.unlock()" in source, (
            "AUD-15601: __new__ 的 check-then-create 必须整体持类级 _mutex"
        )


# ================================================================
# AUD-15605: cancel_all 有界等待 + 超时托管
# ================================================================

class _FakeWorker:
    """非 QObject 假 worker（WaveformExtractor 对注入条目有防御）。"""

    def __init__(self, asset_id: str, wait_result: bool) -> None:
        self._asset_id = asset_id
        self._wait_result = wait_result
        self.interrupted = False
        self.wait_args: list[tuple] = []
        self.deleted = False

    def isRunning(self) -> bool:
        return True

    def requestInterruption(self) -> None:
        self.interrupted = True

    def wait(self, *args: int) -> bool:
        self.wait_args.append(args)
        return self._wait_result

    def deleteLater(self) -> None:
        self.deleted = True


class TestAud15605:
    def test_cancel_all_waits_with_timeout_and_detaches_stuck_worker(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        from src.features.edit.core import audio_waveform as aw
        from src.features.edit.core.audio_waveform import WaveformExtractor

        monkeypatch.setattr(aw, "WAVEFORM_CANCEL_WAIT_MS", 25)
        WaveformCache = aw.WaveformCache
        WaveformCache._instance = None
        try:
            ext = WaveformExtractor()
            stuck = _FakeWorker("a", wait_result=False)
            ext._workers["a"] = stuck

            with caplog.at_level(logging.WARNING, logger="src.features.edit.core.audio_waveform"):
                ext.cancel_all()

            assert stuck.interrupted
            assert stuck.wait_args and stuck.wait_args[0] == (25,), (
                "AUD-15605: cancel_all 必须 wait(超时)"
            )
            assert ext._detached_workers.get("a") is stuck, (
                "超时 worker 必须被托管（防 wrapper GC 时线程仍在跑）"
            )
            assert "a" not in ext._workers
            assert any("did not stop" in r.message for r in caplog.records)
        finally:
            WaveformCache._instance = None

    def test_cancel_all_clean_worker_not_detached(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.features.edit.core import audio_waveform as aw
        from src.features.edit.core.audio_waveform import WaveformExtractor

        monkeypatch.setattr(aw, "WAVEFORM_CANCEL_WAIT_MS", 25)
        WaveformCache = aw.WaveformCache
        WaveformCache._instance = None
        try:
            ext = WaveformExtractor()
            worker = _FakeWorker("a", wait_result=True)
            ext._workers["a"] = worker
            ext._callbacks["a"] = [lambda wf: None]

            ext.cancel_all()

            assert worker.wait_args and worker.wait_args[0] == (25,)
            assert "a" not in ext._detached_workers
            assert ext._callbacks == {}
        finally:
            WaveformCache._instance = None

    def test_detached_worker_reclaimed_on_finished(self) -> None:
        from src.features.edit.core.audio_waveform import WaveformExtractor

        ext = WaveformExtractor()
        worker = _FakeWorker("a", wait_result=True)
        ext._detached_workers["a"] = worker

        ext._on_worker_finished("a")

        assert worker.deleted
        assert "a" not in ext._detached_workers


# ================================================================
# AUD-15606: peaks_max 是幅值包络（文档与实现一致）
# ================================================================

class TestAud15606:
    def test_waveform_data_documented_as_magnitude_envelope(self) -> None:
        from src.features.edit.core.audio_waveform import WaveformData

        doc = (WaveformData.__doc__ or "").lower()
        assert "magnitude" in doc, "AUD-15606: peaks_max 文档必须声明幅值包络语义"
        assert "aud-15606" in doc

    def test_all_negative_block_produces_positive_magnitude(self) -> None:
        """全负样本块：上半包络是深谷的幅值（非 0/非正峰）——与新文档一致。"""
        from src.features.edit.core.audio_waveform import _PeakAggregator

        agg = _PeakAggregator(channels=1, max_buckets=1024,
                              samples_per_second=2048.0,
                              sample_rate=44100.0,
                              estimated_duration_seconds=1.0)
        agg.add(np.array([[-0.5, -0.8]], dtype=np.float32))
        result = agg.finish()
        assert result is not None
        peaks_min, peaks_max, _sps, count = result
        assert count >= 1
        assert peaks_min[0] == pytest.approx(-0.8, abs=1e-6)
        assert peaks_max[0] == pytest.approx(0.8, abs=1e-6), (
            "peaks_max 应为 max(|samples|) 幅值包络"
        )


# ================================================================
# AUD-15607: 失败处理器按代际保留新请求回调
# ================================================================

class TestAud15607:
    def test_stale_failure_keeps_newer_callbacks(self) -> None:
        from src.features.edit.core.audio_waveform import WaveformExtractor

        ext = WaveformExtractor()
        failures: list[tuple[str, str]] = []
        ext.extraction_failed.connect(lambda aid, err: failures.append((aid, err)))

        ext._request_generations["a"] = 2  # 新请求已是第 2 代
        new_callback = Mock(name="new_cb")
        ext._callbacks["a"] = [new_callback]

        # 旧代际（1）的失败信号迟到
        ext._on_extraction_failed("a", "boom", generation=1)

        assert failures == [("a", "boom")]
        assert ext._callbacks.get("a") == [new_callback], (
            "AUD-15607: 旧代际失败不得清掉新请求的回调"
        )

        # 当前代际失败 → 正常清回调
        ext._on_extraction_failed("a", "boom2", generation=2)
        assert "a" not in ext._callbacks
        assert len(failures) == 2

    def test_unversioned_failure_keeps_legacy_behavior(self) -> None:
        from src.features.edit.core.audio_waveform import WaveformExtractor

        ext = WaveformExtractor()
        cb = Mock()
        ext._callbacks["a"] = [cb]
        ext._on_extraction_failed("a", "boom")  # 无代际（旧接线兼容）
        assert "a" not in ext._callbacks

    def test_request_waveform_bumps_generation_per_new_request(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.features.edit.core import audio_waveform as aw
        from src.features.edit.core.audio_waveform import WaveformExtractor

        aw.WaveformCache._instance = None
        try:
            ext = WaveformExtractor()
            started: list[str] = []
            monkeypatch.setattr(ext, "_start_worker",
                                lambda asset_id, path: started.append(asset_id))
            cb = Mock()

            ext.request_waveform("a", "/p1", cb)
            assert ext._request_generations["a"] == 1
            assert started == ["a"]

            # pending 期间追加回调：不升代际、不重复启动
            ext.request_waveform("a", "/p1", Mock())
            assert ext._request_generations["a"] == 1
            assert len(ext._callbacks["a"]) == 2
            assert started == ["a"]

            # 失败清 pending 后的新请求：代际 +1
            aw.WaveformCache().clear_pending("a")
            ext.request_waveform("a", "/p2", cb)
            assert ext._request_generations["a"] == 2
            assert started == ["a", "a"]
        finally:
            aw.WaveformCache._instance = None


# ================================================================
# AUD-15610: AutomationRecorder record/stop 并发保护
# ================================================================

class TestAud15610:
    def test_record_blocks_while_stop_path_holds_lock(self) -> None:
        """锁纪律：record 的样本写入与 stop 的取走在同一把锁上串行。"""
        from src.features.edit.core.automation_recorder import AutomationRecorder

        recorder = AutomationRecorder()
        recorder.start("volume", "write", "t1")

        recorder._lock.acquire()
        try:
            t = threading.Thread(target=lambda: recorder.record(1, 0.5))
            t.start()
            t.join(0.3)
            assert len(recorder._samples) == 0, (
                "AUD-15610: record 应被 _lock 挡住（旧实现无锁立即写入）"
            )
        finally:
            recorder._lock.release()
        t.join()
        assert len(recorder._samples) == 1  # 锁释放后写入完成

    def test_stop_blocks_until_lock_released(self) -> None:
        """锁纪律：stop 的取走路径必须经过 _lock（与并发 record 串行）。"""
        from src.features.edit.core.automation_recorder import AutomationRecorder

        recorder = AutomationRecorder()
        recorder.start("volume", "write", "t1")
        for f in range(10):
            recorder.record(f, 0.5)

        results: list[object] = []
        recorder._lock.acquire()
        try:
            t = threading.Thread(
                target=lambda: results.append(recorder.stop()))
            t.start()
            t.join(0.3)
            # stop 被锁挡住：状态未翻转、命令未生成
            assert t.is_alive(), "AUD-15610: stop 应被 _lock 挡住（旧实现无锁立即完成）"
            assert recorder.is_recording is True
            assert len(recorder._samples) == 10
        finally:
            recorder._lock.release()
        t.join()

        assert recorder.is_recording is False
        assert recorder._samples == []
        assert len(results) == 1 and results[0] is not None


# ================================================================
# AUD-15611: Touch release 行为边界显式声明
# ================================================================

class TestAud15611:
    def test_module_documents_touch_release_contract(self) -> None:
        """E16 后契约：release 已实现（provider 驱动），并在文档中保留
        无 provider 时退化为旧行为的边界声明。"""
        from src.features.edit.core import automation_recorder as ar

        doc = ar.__doc__ or ""
        assert "AUD-15611" in doc
        assert "release_provider" in doc, "文档必须声明 provider 驱动的 release"
        assert "退化为旧行为" in doc, "无 provider 的边界仍须显式声明"

    def test_touch_release_stops_writing_without_ramp(self) -> None:
        from src.features.edit.core.automation_recorder import AutomationRecorder

        recorder = AutomationRecorder()
        recorder.start("volume", "touch", "t1")
        recorder.record(10, 0.8, fader_down=True)
        recorder.record(11, 0.9, fader_down=True)
        recorder.record(12, 0.5, fader_down=False)  # 松手：停写、闭合段
        recorder.record(13, 0.2, fader_down=False)

        cmd = recorder.stop()
        assert cmd is not None
        frames = [kf[0] for kf in cmd.keyframes]
        assert frames == [10, 11], "无 provider 时松手后不得生成 release 关键帧"
        assert cmd.overwrite_ranges == ((10, 11),)


# ================================================================
# AUD-15801: promote 兜底不泄漏首建空图形轨
# ================================================================

def _promote_timeline() -> "TimelineSequence":  # type: ignore[name-defined]
    from src.features.edit.core.captions.model import CaptionCueMetadata
    from src.features.edit.core.timeline_v2 import EditTrack, SubtitleClip, TimelineSequence

    subtitle_track = EditTrack(
        track_id="sub-track", track_type="subtitle", name="Captions",
        subtitle_clips=(SubtitleClip(
            clip_id="cue-1", track_id="sub-track", timeline_start=10,
            timeline_duration=20, text="Hello",
            caption_metadata=CaptionCueMetadata(),
        ),),
    )
    return TimelineSequence(
        timeline_id="timeline-1", name="Test",
        video_tracks=(EditTrack(track_id="v1"),),
        subtitle_tracks=(subtitle_track,),
    )


class TestAud15801:
    def test_fallback_removes_first_created_track(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """首建图形轨上 with_clip_added 抛 ValueError 时：先摘除首建轨，
        兜底轨成为唯一图形轨，undo 后无残留。"""
        from src.features.edit.core.captions.commands import PromoteSubtitleToGraphicCommand
        from src.features.edit.core.timeline_v2 import TimelineSequence

        timeline = _promote_timeline()
        original = TimelineSequence.with_clip_added
        injected = {"done": False}

        def fake_with_clip_added(self: TimelineSequence, track_id: str, clip: object):
            track = self.get_track_by_id(track_id)
            if (
                track is not None
                and getattr(track, "track_role", "") == "graphics"
                and not track.clips
                and not injected["done"]
            ):
                injected["done"] = True  # 仅拦截首次（首建空轨）
                raise ValueError("injected overlap for AUD-15801")
            return original(self, track_id, clip)

        monkeypatch.setattr(TimelineSequence, "with_clip_added", fake_with_clip_added)

        command = PromoteSubtitleToGraphicCommand.create("cue-1")
        promoted, applied = command.apply(timeline)

        graphics = [t for t in promoted.video_tracks
                    if t.track_role == "graphics"]
        assert len(graphics) == 1, (
            "AUD-15801: 首建空图形轨必须被移除，只留兜底轨"
        )
        assert len(graphics[0].clips) == 1
        assert applied._created_track_id == graphics[0].track_id

        restored = applied.undo(promoted)
        assert [t for t in restored.video_tracks
                if t.track_role == "graphics"] == []


# ================================================================
# AUD-15802: no-op 样式提交不因锁定 cue 抛错
# ================================================================

class TestAud15802:
    def _sequence_with_locked_inherited_cue(self) -> "TimelineSequence":  # type: ignore[name-defined]
        from src.features.edit.core.captions.model import (
            CaptionCueMetadata,
            CaptionStyle,
            CaptionTrackMetadata,
        )
        from src.features.edit.core.timeline_v2 import EditTrack, SubtitleClip, TimelineSequence

        cue = SubtitleClip(
            clip_id="cue-1", track_id="sub", timeline_start=0,
            timeline_duration=10, text="x",
        ).with_caption_metadata(CaptionCueMetadata(locked=True))
        track = EditTrack(
            track_id="sub", track_type="subtitle", subtitle_clips=(cue,),
            caption_metadata=CaptionTrackMetadata(
                default_style=CaptionStyle(font_size=100.0),
            ),
        )
        return TimelineSequence(timeline_id="t", subtitle_tracks=(track,))

    def test_noop_style_passes_locked_cue(self) -> None:
        from src.features.edit.core.captions.commands import UpdateSubtitleTrackStyleCommand
        from src.features.edit.core.captions.model import CaptionStyle

        seq = self._sequence_with_locked_inherited_cue()
        same_style = CaptionStyle(font_size=100.0)  # 与当前完全一致

        updated, _cmd = UpdateSubtitleTrackStyleCommand.create("sub", same_style).apply(seq)
        assert updated.subtitle_tracks[0].caption_metadata.default_style.font_size == 100.0

    def test_real_change_still_rejects_locked_inherited_cue(self) -> None:
        from src.features.edit.core.captions.commands import UpdateSubtitleTrackStyleCommand
        from src.features.edit.core.captions.model import CaptionStyle

        seq = self._sequence_with_locked_inherited_cue()
        changed = CaptionStyle(font_size=120.0)
        with pytest.raises(ValueError, match="Locked subtitle cue"):
            UpdateSubtitleTrackStyleCommand.create("sub", changed).apply(seq)


# ================================================================
# AUD-15803: merge 校验相邻性与同 clip
# ================================================================

class TestAud15803:
    @staticmethod
    def _seq(clips: tuple) -> "TimelineSequence":  # type: ignore[name-defined]
        from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence

        track = EditTrack(
            track_id="s1", name="S1", track_type="subtitle", index=0,
            subtitle_clips=tuple(clips),
        )
        return TimelineSequence(
            timeline_id="seq", name="Seq", fps=24, subtitle_tracks=(track,),
        )

    @staticmethod
    def _cue(cid: str, start: int, dur: int):
        from src.features.edit.core.timeline_v2 import SubtitleClip

        return SubtitleClip(
            clip_id=cid, track_id="s1",
            timeline_start=start, timeline_duration=dur, text="x",
        )

    def test_merge_rejects_gap_between_clips(self) -> None:
        from src.features.edit.core.captions.commands import MergeSubtitleClipsCommand

        seq = self._seq((self._cue("a", 0, 50), self._cue("b", 90, 50)))
        with pytest.raises(ValueError, match="gap"):
            MergeSubtitleClipsCommand.create("a", "b").apply(seq)

    def test_merge_rejects_same_clip(self) -> None:
        from src.features.edit.core.captions.commands import MergeSubtitleClipsCommand

        seq = self._seq((self._cue("a", 0, 50),))
        with pytest.raises(ValueError, match="merge a clip with itself"):
            MergeSubtitleClipsCommand.create("a", "a").apply(seq)

    def test_merge_still_allows_adjacent_clips(self) -> None:
        from src.features.edit.core.captions.commands import MergeSubtitleClipsCommand

        adjacent = self._seq((self._cue("a", 0, 50), self._cue("b", 50, 50)))
        merged, _cmd = MergeSubtitleClipsCommand.create("a", "b").apply(adjacent)
        assert len(merged.subtitle_tracks[0].subtitle_clips) == 1


# ================================================================
# AUD-16111 / AUD-16112: chapter_export 文档与告警
# ================================================================

class _Marker:
    def __init__(self, frame: int, name: str, kind: str = "chapter",
                 clip_id: object = None) -> None:
        self.frame = frame
        self.name = name
        self.kind = kind
        self.clip_id = clip_id


class TestAud16111:
    def test_module_docstring_matches_millisecond_timebase(self) -> None:
        from src.features.edit.core import chapter_export

        doc = chapter_export.__doc__ or ""
        assert "TIMEBASE=1/1000" in doc
        assert "TIMEBASE=1/fps" not in doc, (
            "AUD-16111: 模块头必须与实现（毫秒时间基）一致"
        )

    def test_ffmetadata_output_uses_milliseconds(self) -> None:
        from src.features.edit.core.chapter_export import format_ffmetadata

        text = format_ffmetadata([_Marker(0, "A"), _Marker(48, "B")], fps=24.0)
        assert "TIMEBASE=1/1000" in text
        assert "START=0" in text and "END=2000" in text  # 48 帧 @24fps = 2000ms


class TestAud16112:
    def test_fewer_than_three_chapters_warns(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        from src.features.edit.core.chapter_export import format_youtube_chapters

        with caplog.at_level(logging.WARNING, logger="src.features.edit.core.chapter_export"):
            out = format_youtube_chapters([_Marker(0, "A"), _Marker(48, "B")], fps=24.0)

        assert out  # 仍输出，交由调用方决定
        assert any("at least 3" in r.message for r in caplog.records), (
            "AUD-16112: 1-2 条章节必须告警"
        )

    def test_three_chapters_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        from src.features.edit.core.chapter_export import format_youtube_chapters

        markers = [_Marker(0, "A"), _Marker(48, "B"), _Marker(96, "C")]
        with caplog.at_level(logging.WARNING, logger="src.features.edit.core.chapter_export"):
            out = format_youtube_chapters(markers, fps=24.0)

        assert out
        assert not [r for r in caplog.records if "at least 3" in r.message]


# ================================================================
# AUD-16316: 4 元组 crop 契约统一
# ================================================================

class TestAud16316:
    def test_four_element_crop_applies_without_error(self) -> None:
        from src.features.edit.core.crop_cpu import apply_crop_cpu

        frame = np.full((10, 10, 3), 200, dtype=np.uint8)
        crop4 = (0.5, 0.0, 0.0, 0.0)  # 缺 feather 的历史 4 元组

        out = apply_crop_cpu(frame, crop4)

        assert out.shape == (10, 10, 4)
        assert np.all(out[:, :5, 3] == 0)      # 左半被裁掉
        assert np.all(out[:, 5:, 3] == 255)    # 保留区不透明（feather=0）

    def test_four_element_identity_crop_returns_frame(self) -> None:
        from src.features.edit.core.crop_cpu import apply_crop_cpu, crop_is_identity

        frame = np.zeros((8, 8, 3), dtype=np.uint8)
        assert crop_is_identity((0.0, 0.0, 0.0, 0.0)) is True
        assert crop_is_identity((0.1, 0.0, 0.0, 0.0)) is False
        assert apply_crop_cpu(frame, (0.0, 0.0, 0.0, 0.0)) is frame

    def test_five_element_crop_unchanged(self) -> None:
        from src.features.edit.core.crop_cpu import apply_crop_cpu

        frame = np.full((10, 10, 3), 200, dtype=np.uint8)
        out = apply_crop_cpu(frame, (0.5, 0.0, 0.0, 0.0, 0.0))
        assert out.shape == (10, 10, 4)
        assert np.all(out[:, :5, 3] == 0)


# ================================================================
# AUD-16317: apply_crop_premultiplied 移除冗余 copy
# ================================================================

class TestAud16317:
    def test_no_redundant_copy_after_astype(self) -> None:
        from src.features.edit.core.crop_render import apply_crop_premultiplied

        source = inspect.getsource(apply_crop_premultiplied)
        assert ".astype(np.float32).copy()" not in source, (
            "AUD-16317: astype 默认拷贝，多余的 .copy() 每帧多一次全帧分配"
        )

    def test_result_independent_and_mask_applied(self) -> None:
        from src.features.edit.core.crop_render import CropRect, apply_crop_premultiplied

        rect = CropRect(x0=2.0, y0=2.0, x1=8.0, y1=8.0,
                        feather_px=0.0, width=10, height=10)
        frame = np.ones((10, 10, 4), dtype=np.float32)
        out = apply_crop_premultiplied(frame, rect)

        # 输入未被修改；输出是独立数组
        assert np.all(frame == 1.0)
        assert out.base is not frame
        assert np.all(out[:2, :, 3] == 0.0)     # 裁掉区全透明
        assert np.all(out[5, 5] == 1.0)         # 保留区不变


# ================================================================
# AUD-16416: frame_ready 信号真实发射
# ================================================================

class _FakeFrameService:
    """EditFrameCoordinator 的最小 service 替身（无真实解码）。"""

    def __init__(self) -> None:
        self.requested: list[tuple[str, int, str, bool]] = []
        self.next_frame: object = object()
        self.prefetched: list[tuple] = []

    def open_clip(self, path: str) -> str:
        return f"clip-{path}"

    def get_clip_info(self, clip_id: str):
        return types.SimpleNamespace(total_frames=100)

    def request_frame(self, clip_id: str, frame_index: int, quality: str,
                      allow_master_fallback: bool = True):
        self.requested.append((clip_id, frame_index, quality, allow_master_fallback))
        return self.next_frame

    def prefetch(self, clip_id: str, start: int, end: int, quality: str) -> None:
        self.prefetched.append((clip_id, start, end, quality))

    def get_stats(self) -> dict:
        return {}


class TestAud16416:
    def test_request_frame_emits_frame_ready(self) -> None:
        from src.features.edit.core.edit_frame_coordinator import EditFrameCoordinator

        service = _FakeFrameService()
        coord = EditFrameCoordinator(service=service)
        coord.add_clip("asset-1", "/media/a.mp4")

        received: list[tuple] = []
        coord.frame_ready.connect(
            lambda aid, idx, q, frame: received.append((aid, idx, q, frame))
        )

        frame = coord.request_frame("asset-1", 7)
        assert frame is service.next_frame
        assert received == [("asset-1", 7, "master", frame)], (
            "AUD-16416: 成功取帧后必须发射 frame_ready"
        )

    def test_failed_request_does_not_emit(self) -> None:
        from src.features.edit.core.edit_frame_coordinator import EditFrameCoordinator

        service = _FakeFrameService()
        coord = EditFrameCoordinator(service=service)
        coord.add_clip("asset-1", "/media/a.mp4")
        service.next_frame = None

        received: list[tuple] = []
        coord.frame_ready.connect(
            lambda aid, idx, q, frame: received.append((aid, idx, q, frame))
        )
        result = coord.request_frame("asset-1", 7)
        assert result is None
        assert received == []


# ================================================================
# AUD-16419: _select_quality 实现驻留升级
# ================================================================

class TestAud16419:
    def _coordinator(self):
        from src.features.edit.core.edit_frame_coordinator import EditFrameCoordinator

        service = _FakeFrameService()
        coord = EditFrameCoordinator(service=service)
        coord.add_clip("asset-1", "/media/a.mp4")
        return coord, service

    def test_playing_advancing_frames_use_proxy(self) -> None:
        coord, service = self._coordinator()
        coord.on_play()

        coord.request_frame("asset-1", 10)
        coord.request_frame("asset-1", 11)
        coord.request_frame("asset-1", 12)
        assert [entry[2] for entry in service.requested] == [
            "proxy", "proxy", "proxy",
        ]

    def test_long_dwell_on_same_frame_upgrades_to_master(self) -> None:
        coord, service = self._coordinator()
        coord.on_play()

        coord.request_frame("asset-1", 10)
        assert service.requested[-1][2] == "proxy"

        # 模拟停帧驻留：把上次请求时间回拨到阈值之外
        handle = coord.get_clip_handle("asset-1")
        assert handle is not None
        handle.last_requested_at = time.monotonic() - 10.0
        coord.request_frame("asset-1", 10)
        assert service.requested[-1][2] == "master", (
            "AUD-16419: 播放态长时间停在同一帧应升级 master"
        )

    def test_paused_and_explicit_quality_unchanged(self) -> None:
        coord, service = self._coordinator()
        coord.on_play()
        coord.on_pause()
        coord.request_frame("asset-1", 5)
        assert service.requested[-1][2] == "master"

        coord.on_play()
        coord.request_frame("asset-1", 6, quality="master")
        assert service.requested[-1][2] == "master"


# ================================================================
# AUD-16422: 播放态字段统一锁纪律
# ================================================================

class TestAud16422:
    def test_state_write_is_serialized_by_lock(self) -> None:
        from src.features.edit.core.edit_frame_coordinator import EditFrameCoordinator

        coord = EditFrameCoordinator(service=_FakeFrameService())

        coord._lock.acquire()
        try:
            t = threading.Thread(target=coord._on_state_changed, args=("playing",))
            t.start()
            t.join(0.3)
            assert coord._current_quality == "master", (
                "AUD-16422: _on_state_changed 的写入必须被 _lock 挡住"
            )
        finally:
            coord._lock.release()
        t.join()
        assert coord.current_quality == "proxy"
        assert coord.is_paused is False

    def test_on_pause_on_play_lock_discipline(self) -> None:
        from src.features.edit.core.edit_frame_coordinator import EditFrameCoordinator

        coord = EditFrameCoordinator(service=_FakeFrameService())
        coord.on_play()
        assert coord.current_quality == "proxy"
        coord.on_pause()
        assert coord.current_quality == "master"
        assert coord.is_paused is True

        # on_pause 的写入同样持锁：锁被持有期间写不落地
        coord.on_play()
        coord._lock.acquire()
        try:
            t = threading.Thread(target=coord.on_pause)
            t.start()
            t.join(0.3)
            assert coord._current_quality == "proxy"  # on_play 的值未被覆盖
            assert coord._is_paused is False
        finally:
            coord._lock.release()
        t.join()
        assert coord.current_quality == "master"


# ================================================================
# AUD-16504: placeholder 拒绝描述已订正（仅遥测）
# ================================================================

class TestAud16504:
    def test_stale_placeholder_denial_docs_removed(self) -> None:
        from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager

        class_source = inspect.getsource(EditGpuDecodeManager)
        assert "返回 (None, 'placeholder')" not in class_source, (
            "AUD-16504: 段注释仍描述已删除的拒绝行为"
        )
        fn_doc = EditGpuDecodeManager.get_frame_with_policy.__doc__ or ""
        assert "When denied" not in fn_doc
        assert "telemetry" in fn_doc

    def test_placeholder_decision_still_decodes_telemetry_only(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.features.edit.core import edit_gpu_decode_manager as gm

        manager = gm.EditGpuDecodeManager.__new__(gm.EditGpuDecodeManager)
        sentinel = object()
        get_frame_calls: list[tuple] = []
        monkeypatch.setattr(
            manager, "get_frame",
            lambda path, idx, target_size=None, stream_key=None:
                get_frame_calls.append((path, idx)) or (sentinel, "cpu"),
        )
        traces: list[str] = []
        monkeypatch.setattr(gm, "playback_trace",
                            lambda name, **kw: traces.append(name))

        decision = types.SimpleNamespace(
            source_kind=types.SimpleNamespace(value="placeholder"),
            reason="no_proxy",
        )
        frame, frame_type = manager.get_frame_with_policy(
            "/media/a.mp4", 3, source_decision=decision,
        )

        assert get_frame_calls == [("/media/a.mp4", 3)], (
            "PLACEHOLDER 决策只做遥测，不得阻塞解码"
        )
        assert frame is sentinel and frame_type == "cpu"
        assert "decode_manager.realtime_no_proxy_fallback_decode" in traces
