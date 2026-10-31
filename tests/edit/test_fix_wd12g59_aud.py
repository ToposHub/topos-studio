"""wd12-g59 批次修复回归测试（REMEDIATION_PLAN 批 D：代理策略 + 实时播放 + 录制域）。

覆盖条目：
- AUD-17233 代理编码进度按总时长归一化（不再秒到 95%）
- AUD-17235 delete_all_proxies_in_directory 只删能回溯到源文件的代理
- AUD-17251 未知尺寸在 mode="all" 下也返回 dimensions_unknown
- AUD-17300 引擎状态字段统一持锁（无死锁/信号在锁外发射）
- AUD-17310 P010/P016 位深回退正则命中
- AUD-17315 解码管理器异常可观测（首例 warning 带栈）
- AUD-17400 GPU 池独占释放语义（幂等 release + 误释放不双重释放）
- AUD-17401 上传纹理必须带 release 入池（external 显式登记外部所有者）
- AUD-17409 render cache 快照按 clip 查询（多 clip 不串缓存路径）
- AUD-17410 register_manifest 真实 fps/尺寸 + get_cache_entry 完整 key 比对
- AUD-17419 scheduler.configure 工厂调用/close 移出锁外
- AUD-17500 attach_backend 部分失败回滚已建立连接
- AUD-17501 非 gpu/planar payload 显式拒绝（语义化 miss）
- AUD-17602 溢写磁盘预检前移（预检失败块不丢、丢块不虚增时长）
- AUD-17601 take 落盘后台化（flushing 态 + 主线程完成回调）
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtCore import QCoreApplication

from src.features.edit.core.proxy_generator import (
    _encode_progress_percent,
    delete_all_proxies_in_directory,
)
from src.features.edit.core.proxy_policy import (
    ProxyPolicySettings,
    evaluate_proxy_action,
)
from src.features.edit.core.realtime_playback.engine import (
    EngineState,
    RealtimePlaybackEngine,
)
from src.features.edit.core.realtime_playback.clip_decode_stream import (
    ClipDecodeStream,
    StreamKey,
)
from src.features.edit.core.realtime_playback.decode_manager_source import (
    DecodeManagerMediaSource,
)
from src.features.edit.core.realtime_playback.gpu_frame_pool import GpuFramePool
from src.features.edit.core.realtime_playback.render_cache_adapter import (
    RenderCacheAdapter,
    RenderCacheEntry,
    RenderCacheKey,
)
from src.features.edit.core.realtime_playback.source_resolver import (
    LayerSourceQuery,
    SourceResolver,
)
from src.features.edit.core.realtime_playback.timeline_decode_scheduler import (
    LayerSpec,
    TimelineDecodeScheduler,
)
from src.features.edit.core.realtime_playback.timeline_gpu_compositor import (
    GpuLayerInput,
    TimelineGpuCompositor,
    TimelineGpuRenderTask,
)
from src.features.edit.core.realtime_playback.types import (
    FrameHandle,
    FrameIdentity,
    PlaybackMode,
    SourceKind,
)
from src.features.edit.core.recording_manager import (
    DiskSpaceExhausted,
    RecordingConfig,
    RecordingManager,
    _ChunkedPcmBuffer,
)


@pytest.fixture(scope="module")
def qapp():
    return QCoreApplication.instance() or QCoreApplication([])


# =============================================================================
# AUD-17233：进度归一化
# =============================================================================


class TestAud17233ProgressNormalization:
    def test_long_video_progress_scales_by_duration(self) -> None:
        # 10 秒素材的第 1 秒 → 9%（旧行为直接 95%）
        assert _encode_progress_percent(1.0, 10.0) == 9

    def test_progress_capped_at_95(self) -> None:
        assert _encode_progress_percent(9.9, 10.0) == 94
        assert _encode_progress_percent(20.0, 10.0) == 95  # 超界钳制

    def test_progress_monotonic_and_zero_based(self) -> None:
        values = [
            _encode_progress_percent(t / 10.0, 10.0) for t in range(11)
        ]
        assert values == sorted(values)
        assert values[0] == 0

    def test_unknown_duration_falls_back_to_clamped_estimate(self) -> None:
        assert _encode_progress_percent(0.5, 0.0) == 50
        assert _encode_progress_percent(5.0, 0.0) == 95

    def test_none_frame_time_is_safe(self) -> None:
        assert _encode_progress_percent(None, 10.0) == 0


# =============================================================================
# AUD-17235：目录代理删除只删可回溯条目
# =============================================================================


class TestAud17235SafeDirectoryDelete:
    def _setup(self, tmp_path: Path) -> None:
        (tmp_path / "a.mov").write_bytes(b"source-a")
        (tmp_path / "a_proxy.mov").write_bytes(b"proxy-a")
        # 用户自命名含 "_proxy" 的原片（无对应源）——不得删除
        (tmp_path / "interview_proxy_reel.mov").write_bytes(b"user-media")
        # 孤儿代理（源已不在）——保守不删
        (tmp_path / "b_proxy.mov").write_bytes(b"orphan-proxy")

    def test_only_traceable_proxies_deleted(self, tmp_path: Path) -> None:
        self._setup(tmp_path)
        deleted = delete_all_proxies_in_directory(str(tmp_path))
        assert deleted == 1
        assert not (tmp_path / "a_proxy.mov").exists()
        assert (tmp_path / "a.mov").exists()
        assert (tmp_path / "interview_proxy_reel.mov").exists()
        assert (tmp_path / "b_proxy.mov").exists()

    def test_custom_suffix(self, tmp_path: Path) -> None:
        (tmp_path / "clip.mp4").write_bytes(b"s")
        (tmp_path / "clip_lite.mp4").write_bytes(b"p")
        assert delete_all_proxies_in_directory(str(tmp_path), suffix="_lite") == 1
        assert not (tmp_path / "clip_lite.mp4").exists()

    def test_empty_suffix_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "x.mov").write_bytes(b"s")
        assert delete_all_proxies_in_directory(str(tmp_path), suffix="") == 0
        assert (tmp_path / "x.mov").exists()

    def test_missing_directory_returns_zero(self, tmp_path: Path) -> None:
        assert delete_all_proxies_in_directory(str(tmp_path / "nope")) == 0


# =============================================================================
# AUD-17251：all 模式不盲生成未知尺寸素材
# =============================================================================


class TestAud17251DimensionsUnknownBeforeAll:
    def test_all_mode_unknown_dimensions_skips(self) -> None:
        policy = ProxyPolicySettings(mode="all")
        decision = evaluate_proxy_action(
            policy, media_type="video", width=0, height=0,
        )
        assert decision.action == "skip"
        assert decision.reason == "dimensions_unknown"

    def test_all_mode_with_dimensions_still_generates(self) -> None:
        policy = ProxyPolicySettings(mode="all")
        decision = evaluate_proxy_action(
            policy, media_type="video", width=640, height=360,
        )
        assert decision.should_generate
        assert decision.reason == "policy_all"

    def test_auto_mode_dimensions_unknown_unchanged(self) -> None:
        decision = evaluate_proxy_action(
            ProxyPolicySettings(), media_type="video", width=0, height=0,
        )
        assert decision.reason == "dimensions_unknown"

    def test_all_mode_topos_with_dimensions_still_skips(self) -> None:
        decision = evaluate_proxy_action(
            ProxyPolicySettings(mode="all"),
            media_type="video", width=1920, height=1080, codec="topos422",
        )
        assert decision.action == "skip"
        assert decision.reason == "topos_edit_codec_no_proxy_needed"


# =============================================================================
# AUD-17300：引擎状态字段加锁一致
# =============================================================================


class TestAud17300EngineStateLocking:
    def test_state_changed_emitted_outside_lock(self, qapp) -> None:
        engine = RealtimePlaybackEngine()
        engine.configure(delegate_render=lambda *a, **k: True)  # → READY
        states: list[str] = []
        engine.state_changed.connect(states.append)

        def _probe() -> None:
            # 直连槽在发射线程执行：读取 state（需要锁）不得死锁。
            _ = engine.state

        engine.state_changed.connect(_probe)
        engine.set_mode(PlaybackMode.REALTIME)
        assert engine.state is EngineState.PLAYING
        assert "playing" in states

    def test_set_mode_idempotent_emits_once(self, qapp) -> None:
        engine = RealtimePlaybackEngine()
        emitted: list[str] = []
        engine.state_changed.connect(emitted.append)
        engine.set_mode(PlaybackMode.PAUSED)
        engine.set_mode(PlaybackMode.PAUSED)
        assert emitted.count("paused") == 1

    def test_concurrent_state_mutations_are_safe(self, qapp) -> None:
        """多线程 set_mode/seek/state 读取不抛异常不死锁（AUD-17300 竞争面）。"""
        engine = RealtimePlaybackEngine()
        engine.configure(delegate_render=lambda *a, **k: True)
        stop = threading.Event()
        errors: list[Exception] = []

        def _mode_flipper() -> None:
            try:
                while not stop.is_set():
                    engine.set_mode(PlaybackMode.REALTIME)
                    engine.set_mode(PlaybackMode.PAUSED)
            except Exception as e:  # pragma: no cover — 记录竞争异常
                errors.append(e)

        def _seeker() -> None:
            try:
                for _ in range(200):
                    engine.seek(10)
                    _ = engine.state
                    _ = engine.generation_id
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [
            threading.Thread(target=_mode_flipper),
            threading.Thread(target=_seeker),
        ]
        for t in threads:
            t.start()
        for _ in range(200):
            engine.seek(5)
        stop.set()
        for t in threads:
            t.join(timeout=5.0)
            assert not t.is_alive(), "engine 状态竞争导致挂死（AUD-17300）"
        assert errors == []
        assert engine.generation_id >= 200

    def test_shutdown_blocks_request_present(self, qapp) -> None:
        engine = RealtimePlaybackEngine()
        engine.configure(delegate_render=lambda *a, **k: True)
        engine.shutdown()
        assert engine.request_present(0) is False


# =============================================================================
# AUD-17310：P010/P016 位深回退
# =============================================================================


def _make_stream() -> ClipDecodeStream:
    return ClipDecodeStream(
        key=StreamKey(
            timeline_id="t", clip_id="c", asset_id="a",
            source_kind=SourceKind.MASTER, source_path="/m.mov",
            target_width=1920, target_height=1080,
        ),
        media_source=None,
    )


class TestAud17310BitDepthInference:
    @pytest.mark.parametrize(
        "pixel_format,expected",
        [
            ("p010le", 10),
            ("p016le", 16),
            ("yuv422p10le", 10),
            ("yuv444p16be", 16),
            ("gray16le", 16),
            ("bgr24", 8),      # 24 不在位深白名单
            ("yuv420p", 8),    # 无末尾数字
            ("nv12", 8),
        ],
    )
    def test_format_to_bit_depth(self, pixel_format: str, expected: int) -> None:
        stream = _make_stream()
        decoded = SimpleNamespace(
            data=None, planes=(), metadata=None,
            pixel_format=pixel_format, width=1920, height=1080,
            pts=None, time_seconds=0.0,
        )
        handle = stream._build_handle(0, decoded)
        assert handle.bit_depth == expected, pixel_format


# =============================================================================
# AUD-17315：解码管理器异常可观测
# =============================================================================


class TestAud17315DecodeManagerErrorLogging:
    def _make_source(self) -> tuple[DecodeManagerMediaSource, MagicMock]:
        manager = MagicMock()
        manager.get_frame.side_effect = RuntimeError("vt session dead")
        source = DecodeManagerMediaSource(
            manager,
            StreamKey(
                timeline_id="t", clip_id="c", asset_id="a",
                source_kind=SourceKind.MASTER, source_path="/m.mov",
                target_width=1920, target_height=1080,
            ),
        )
        return source, manager

    def test_exception_returns_none_with_warning(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        source, _ = self._make_source()
        with caplog.at_level(logging.WARNING, logger="src.features.edit.core.realtime_playback.decode_manager_source"):
            assert source.read_frame(3) is None
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "解码管理器异常必须至少 warning 一次（AUD-17315）"
        assert any("vt session dead" in r.getMessage() for r in warnings)

    def test_subsequent_failures_do_not_flood(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        source, _ = self._make_source()
        with caplog.at_level(logging.DEBUG, logger="src.features.edit.core.realtime_playback.decode_manager_source"):
            for _ in range(5):
                assert source.read_frame(1) is None
        warnings = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING
            and "decode manager" in r.getMessage()
        ]
        assert len(warnings) == 1  # 首例 warning，其余降级 debug


# =============================================================================
# AUD-17400 / AUD-17401：GPU 池所有权与释放
# =============================================================================


def _frame(index: int = 0) -> FrameHandle:
    identity = FrameIdentity(
        timeline_id="t", clip_id="c", asset_id="a",
        source_path="/x.mov", source_frame=index,
        source_kind=SourceKind.DISK_PROXY, proxy_key="p1",
    )
    return FrameHandle(
        identity=identity, frame_index=index, width=8, height=8,
        pixel_format="yuv420p", bit_depth=8, color_space=None,
        pts_seconds=0.0, source_kind=SourceKind.DISK_PROXY,
        payload=object(), payload_type="gpu_texture",
    )


class TestAud17400PoolOwnedRelease:
    def test_shared_handle_and_single_release(self, qapp) -> None:
        pool = GpuFramePool()
        released: list[int] = []

        def upload(f: FrameHandle):
            from src.features.edit.core.realtime_playback import GpuFrameHandle
            handle = GpuFrameHandle(
                identity=f.identity, texture_ids=(42,),
                texture_type="rgb", width=8, height=8, bit_depth=8,
                color_space=None, generation_id=0,
                release=lambda: released.append(1),
            )
            return handle

        pool.set_upload_function(upload)
        frame = _frame()
        h1 = pool.ensure_gpu_frame(frame, generation_id=1)
        h2 = pool.ensure_gpu_frame(frame, generation_id=1)
        assert h1 is h2  # 池内单例

        # 调用方误按旧文档 release（AUD-17400 场景）
        h1.release()
        h1.release()  # 幂等：不双重释放
        assert len(released) == 1

        # 误释放后缓存不得命中已释放纹理：重新上传
        h3 = pool.ensure_gpu_frame(frame, generation_id=1)
        assert h3 is not h1
        assert pool.stats()["cache_hits"] == 1

        # 池路径释放同样只触发一次底层释放
        pool.release_generation(1)
        pool.shutdown()
        assert len(released) == 2

    def test_generation_release_evicts_resident(self, qapp) -> None:
        pool = GpuFramePool()
        from src.features.edit.core.realtime_playback import GpuFrameHandle
        pool.set_upload_function(lambda f: GpuFrameHandle(
            identity=f.identity, texture_ids=(7,), texture_type="rgb",
            width=8, height=8, bit_depth=8, color_space=None,
            generation_id=0, release=lambda: None,
        ))
        pool.ensure_gpu_frame(_frame(), generation_id=5)
        assert pool.release_generation(5) == 1
        assert pool.stats()["resident"] == 0


class TestAud17401UploadRequiresRelease:
    def test_payload_without_texture_id_cannot_upload(self, qapp) -> None:
        pool = GpuFramePool()
        frame = _frame()
        frame.payload = object()  # 非 external、upload_frame_to_gpu 不可用
        assert pool._default_upload(frame) is None

    def test_external_payload_release_passthrough(self, qapp) -> None:
        calls: list[str] = []
        external = SimpleNamespace(
            texture_id=99, width=8, height=8,
            release=lambda: calls.append("external-release"),
        )
        frame = _frame()
        frame.payload = external
        pool = GpuFramePool()
        handle = pool._default_upload(frame)
        assert handle is not None
        assert handle.texture_type == "external"
        assert handle.release is not None
        handle.release()
        assert calls == ["external-release"]

    def test_upload_without_release_refuses_to_pool(
        self, qapp, monkeypatch,
    ) -> None:
        import src.shared.video.video_upload as video_upload_module

        result = SimpleNamespace(texture_id=55, width=8, height=8)  # 无 release
        monkeypatch.setattr(
            video_upload_module, "upload_frame_to_gpu",
            lambda payload: result, raising=False,
        )
        frame = _frame()
        frame.payload = SimpleNamespace()  # 非 external、可走 upload
        handle = GpuFramePool()._default_upload(frame)
        assert handle is None, "无 release 回调的上传纹理禁止入池（AUD-17401）"

    def test_upload_with_release_is_pooled(self, qapp, monkeypatch) -> None:
        import src.shared.video.video_upload as video_upload_module

        calls: list[str] = []
        result = SimpleNamespace(
            texture_id=66, width=8, height=8,
            release=lambda: calls.append("released"),
        )
        monkeypatch.setattr(
            video_upload_module, "upload_frame_to_gpu",
            lambda payload: result, raising=False,
        )
        frame = _frame()
        frame.payload = SimpleNamespace()
        handle = GpuFramePool()._default_upload(frame)
        assert handle is not None
        assert handle.texture_type == "rgb"
        handle.release()
        assert calls == ["released"]


# =============================================================================
# AUD-17409 / AUD-17410：render cache 适配器
# =============================================================================


def _cache_key(clip_id: str = "c1", **overrides) -> RenderCacheKey:
    fields = dict(
        timeline_id="t", clip_id=clip_id, asset_id="a", source_path="/m.mov",
        frame_rate_num=25, frame_rate_den=1, width=1920, height=1080,
        render_hash="h1",
    )
    fields.update(overrides)
    return RenderCacheKey(**fields)


class TestAud17409PerClipRenderCache:
    def _adapter_with_two_clips(self) -> RenderCacheAdapter:
        adapter = RenderCacheAdapter()
        adapter.upsert_entry(RenderCacheEntry(
            key=_cache_key("clip-a"), status="valid",
            cache_path="/cache/a.mov", width=1920, height=1080,
            start_frame=0, duration_frames=10,
        ))
        # clip-b 无 ready 缓存
        adapter.upsert_entry(RenderCacheEntry(
            key=_cache_key("clip-b", render_hash="hb"), status="rendering",
            cache_path=None, width=1920, height=1080,
            start_frame=0, duration_frames=10,
        ))
        return adapter

    def test_per_clip_query_semantics(self) -> None:
        adapter = self._adapter_with_two_clips()
        assert adapter.has_render_cache_ready_for("clip-a") is True
        assert adapter.render_cache_path_for("clip-a") == "/cache/a.mov"
        assert adapter.has_render_cache_ready_for("clip-b") is False
        assert adapter.render_cache_path_for("clip-b") is None
        # 全局属性保留诊断语义
        assert adapter.has_render_cache_ready is True

    def test_resolver_does_not_cross_assign_cache_paths(self) -> None:
        """多 clip 时间线：无缓存 layer 不得拿到 clip-a 的缓存路径。"""
        adapter = self._adapter_with_two_clips()
        resolver = SourceResolver(proxy_manager=None, render_cache=adapter)
        decisions = resolver.resolve_layers(
            mode=PlaybackMode.REALTIME,
            queries=[
                LayerSourceQuery(
                    clip_id="clip-a", asset_id="a", source_path="/a.mov",
                    source_frame=0, timeline_frame=0, requires_effect_cache=True,
                ),
                LayerSourceQuery(
                    clip_id="clip-b", asset_id="b", source_path="/b.mov",
                    source_frame=0, timeline_frame=0, requires_effect_cache=True,
                ),
            ],
        )
        assert decisions[0].source_kind is SourceKind.RENDER_CACHE
        assert decisions[0].source_path == "/cache/a.mov"
        # 旧行为：clip-b 会被判成 RENDER_CACHE 且指向 /cache/a.mov
        assert decisions[1].source_kind is not SourceKind.RENDER_CACHE
        assert decisions[1].source_path is None


class TestAud17410ManifestKeyIntegrity:
    def _manifest(self, **attrs) -> SimpleNamespace:
        base = dict(
            timeline_id="t1", width=1920, height=1080, path="/cache/m.mov",
            status="valid", start_frame=0, duration_frames=50,
        )
        base.update(attrs)
        return SimpleNamespace(**base)

    def test_register_requires_real_frame_rate(self) -> None:
        adapter = RenderCacheAdapter()
        with pytest.raises(ValueError):
            adapter.register_manifest("c1", self._manifest(), render_hash="rh")

    def test_register_explicit_frame_rate(self) -> None:
        adapter = RenderCacheAdapter()
        entry = adapter.register_manifest(
            "c1", self._manifest(), "rh",
            frame_rate_num=24000, frame_rate_den=1001,
        )
        assert entry.key.frame_rate_num == 24000
        assert entry.key.frame_rate_den == 1001

    def test_register_manifest_fps_attribute(self) -> None:
        adapter = RenderCacheAdapter()
        entry = adapter.register_manifest(
            "c1", self._manifest(fps=50.0), "rh",
        )
        assert entry.key.frame_rate_num == 50
        assert entry.key.frame_rate_den == 1

    def test_register_requires_dimensions(self) -> None:
        adapter = RenderCacheAdapter()
        with pytest.raises(ValueError):
            adapter.register_manifest(
                "c1", self._manifest(width=0), "rh",
                frame_rate_num=25, frame_rate_den=1,
            )

    def test_get_cache_entry_full_key_compare(self) -> None:
        adapter = RenderCacheAdapter()
        adapter.register_manifest(
            "c1", self._manifest(), "rh",
            frame_rate_num=25, frame_rate_den=1,
        )
        # 注册条目 timeline_id="t1"/asset_id=""/source_path=""/cache_version=1
        def _query(**overrides) -> RenderCacheKey:
            fields = dict(
                timeline_id="t1", clip_id="c1", asset_id="", source_path="",
                frame_rate_num=25, frame_rate_den=1, width=1920, height=1080,
                render_hash="rh", cache_version=1,
            )
            fields.update(overrides)
            return RenderCacheKey(**fields)

        # 完全一致 → 命中
        assert adapter.get_cache_entry(_query()) is not None
        # fps 变化 → 失效（旧行为命中）
        assert adapter.get_cache_entry(_query(frame_rate_num=30)) is None
        # cache_version 变化 → 失效（旧行为命中）
        assert adapter.get_cache_entry(_query(cache_version=2)) is None
        # source_path 变化 → 失效（旧行为命中）
        assert adapter.get_cache_entry(_query(source_path="/other.mov")) is None
        # timeline_id 变化 → 失效（旧行为命中）
        assert adapter.get_cache_entry(_query(timeline_id="t2")) is None


# =============================================================================
# AUD-17419：configure 工厂/close 出锁
# =============================================================================


class _InstrumentedLock:
    """记录嵌套深度的 RLock 替身（with 协议兼容）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.depth = 0

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        ok = self._lock.acquire(blocking, -1 if timeout < 0 else timeout)
        if ok:
            self.depth += 1
        return ok

    def release(self) -> None:
        self.depth -= 1
        self._lock.release()

    def __enter__(self) -> "_InstrumentedLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


class TestAud17419ConfigureOutOfLock:
    def test_factory_not_called_under_lock(self) -> None:
        scheduler = TimelineDecodeScheduler()
        instrumented = _InstrumentedLock()
        scheduler._lock = instrumented  # type: ignore[assignment]

        observed_depths: list[int] = []

        def factory(key):
            observed_depths.append(instrumented.depth)
            return None

        scheduler.configure(
            layers=[
                LayerSpec(
                    clip_id="c1", asset_id="a", timeline_id="t",
                    source_path="/p.mov", source_kind=SourceKind.DISK_PROXY,
                ),
            ],
            media_source_factory=factory,
        )
        assert observed_depths, "工厂必须被调用"
        assert all(d == 0 for d in observed_depths), (
            "media_source_factory 不得在调度器锁内执行（AUD-17419）"
        )

    def test_reconfigure_closes_and_rebuilds_outside_lock(self) -> None:
        scheduler = TimelineDecodeScheduler()
        instrumented = _InstrumentedLock()
        scheduler._lock = instrumented

        def factory(key):
            assert instrumented.depth == 0
            return object()

        layer = LayerSpec(
            clip_id="c1", asset_id="a", timeline_id="t",
            source_path="/p.mov", source_kind=SourceKind.DISK_PROXY,
        )
        scheduler.configure(layers=[layer], media_source_factory=factory)
        stream_v1 = scheduler._streams.get("c1")
        assert stream_v1 is not None

        # 换 source_path → 重建流（key 变化）
        layer2 = LayerSpec(
            clip_id="c1", asset_id="a", timeline_id="t",
            source_path="/proxy2.mov", source_kind=SourceKind.DISK_PROXY,
        )
        scheduler.configure(layers=[layer2], media_source_factory=factory)
        stream_v2 = scheduler._streams.get("c1")
        assert stream_v2 is not None
        assert stream_v2 is not stream_v1
        assert instrumented.depth == 0

    def test_removed_layer_stream_is_closed(self) -> None:
        scheduler = TimelineDecodeScheduler()
        layer = LayerSpec(
            clip_id="c1", asset_id="a", timeline_id="t",
            source_path="/p.mov", source_kind=SourceKind.DISK_PROXY,
        )
        scheduler.configure(layers=[layer])
        stream = scheduler._streams["c1"]
        scheduler.configure(layers=[])
        assert "c1" not in scheduler._streams
        assert stream._closed is True


# =============================================================================
# AUD-17500 / AUD-17501：compositor 连接回滚与 payload 契约
# =============================================================================


def _gpu_frame(index: int = 0) -> FrameHandle:
    identity = FrameIdentity(
        timeline_id="t", clip_id="c1", asset_id="a",
        source_path="/x.mov", source_frame=index,
        source_kind=SourceKind.DISK_PROXY,
    )
    return FrameHandle(
        identity=identity, frame_index=index, width=8, height=8,
        pixel_format="bgr24", bit_depth=8, color_space=None,
        pts_seconds=0.0, source_kind=SourceKind.DISK_PROXY,
        payload=MagicMock(), payload_type="gpu_texture",
    )


def _task(frame: FrameHandle, timeline_frame: int = 0) -> TimelineGpuRenderTask:
    return TimelineGpuRenderTask(
        timeline_frame=timeline_frame, generation_id=1,
        output_width=1920, output_height=1080,
        layers=(GpuLayerInput(
            clip_id="c1", track_index=0, opacity=1.0,
            transform=None, crop=None, frame=frame,
        ),),
    )


class TestAud17500AttachRollback:
    def _backend(self, fail_render_error: bool = False) -> MagicMock:
        backend = MagicMock()
        backend.frame_ready = MagicMock()
        backend.frame_ready.connect = MagicMock()
        backend.present_frame_ready = MagicMock()
        backend.present_frame_ready.connect = MagicMock()
        backend.render_error = MagicMock()
        backend.render_error.connect = MagicMock()
        if fail_render_error:
            backend.render_error.connect.side_effect = RuntimeError("boom")
        return backend

    def test_partial_failure_rolls_back(self, qapp) -> None:
        compositor = TimelineGpuCompositor()
        backend = self._backend(fail_render_error=True)
        compositor.attach_backend(backend)
        assert compositor._connected is False
        # 已建立的连接必须被回滚断开
        backend.frame_ready.disconnect.assert_called_once_with(
            compositor._on_backend_frame_ready
        )
        backend.present_frame_ready.disconnect.assert_called_once_with(
            compositor._on_backend_present_frame_ready
        )
        # detach 之后再调用安全（无泄漏路径）
        compositor.detach_backend()

    def test_successful_attach_disconnects_on_detach(self, qapp) -> None:
        compositor = TimelineGpuCompositor()
        backend = self._backend()
        compositor.attach_backend(backend)
        assert compositor._connected is True
        compositor.detach_backend()
        backend.frame_ready.disconnect.assert_called_once()
        backend.render_error.disconnect.assert_called_once()
        assert compositor._connected is False


class TestAud17501PayloadContract:
    def test_cpu_payload_rejected_with_semantic_error(self, qapp) -> None:
        compositor = TimelineGpuCompositor()
        backend = MagicMock()
        backend.submit.return_value = True
        compositor.attach_backend(backend)
        errors: list[tuple[int, str]] = []
        compositor.render_error.connect(lambda f, m: errors.append((f, m)))

        frame = _gpu_frame()
        frame.payload_type = "cpu_ndarray"
        frame.payload = object()  # 真实 ndarray 无 is_planar 属性
        assert compositor.submit(_task(frame)) is False
        assert backend.submit.call_count == 0  # 未进 backend
        assert errors and errors[0][1].startswith("unsupported_payload_type:cpu_ndarray")

    def test_placeholder_payload_rejected(self, qapp) -> None:
        compositor = TimelineGpuCompositor()
        backend = MagicMock()
        compositor.attach_backend(backend)
        frame = _gpu_frame()
        frame.payload_type = "placeholder"
        frame.payload = object()
        assert compositor.submit(_task(frame)) is False

    def test_gpu_and_planar_payloads_still_accepted(self, qapp) -> None:
        compositor = TimelineGpuCompositor()
        backend = MagicMock()
        backend.submit.return_value = True
        compositor.attach_backend(backend)
        assert compositor.submit(_task(_gpu_frame())) is True

        planar = _gpu_frame()
        planar.payload_type = "planar_yuv"
        assert compositor.submit(_task(planar, timeline_frame=1)) is True
        assert backend.submit.call_count == 2


# =============================================================================
# AUD-17602：溢写预检前移
# =============================================================================


def _block(samples: int = 4800, channels: int = 1) -> np.ndarray:
    return np.zeros((channels, samples), dtype=np.float32)


class TestAud17602SpoolPreflight:
    def test_disk_preflight_failure_keeps_block_in_memory(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        import src.features.edit.core.recording_manager as rm

        monkeypatch.setattr(
            rm.shutil, "disk_usage",
            lambda p: SimpleNamespace(free=0),  # 低于 _DISK_FAIL_BYTES
        )
        # memory_limit 下限 1024 字节：用 4000 字节块触发溢写
        buf = _ChunkedPcmBuffer(
            channels=1, spool_dir=str(tmp_path), memory_limit_bytes=1024,
        )
        first = _block(1000)
        buf.append(first)  # 单块不溢写（len(chunks) > 1 才溢写）
        with pytest.raises(DiskSpaceExhausted):
            buf.append(_block(10))
        # 预检失败：块仍在内存且计入 total_samples（不缺段、不虚增）
        assert buf._chunks and buf._chunks[0] is first
        assert buf.total_samples == 1010
        assert buf.to_array().shape == (1, 1010)

    def test_write_failure_decrements_total_samples(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        import src.features.edit.core.recording_manager as rm

        monkeypatch.setattr(
            rm.shutil, "disk_usage",
            lambda p: SimpleNamespace(free=10**12),
        )
        buf = _ChunkedPcmBuffer(
            channels=1, spool_dir=str(tmp_path), memory_limit_bytes=1024,
        )
        first = _block(100)
        second = _block(10)
        # 预置一个必然写失败的溢写文件
        class _FailingFile:
            def write(self, data):
                raise OSError("disk full mid-write")
            def flush(self):
                return None
            def close(self):
                return None

        buf._chunks = [first, second]
        buf._memory_bytes = first.nbytes + second.nbytes
        buf._total_samples = 110
        buf._spool_file = _FailingFile()
        buf._spool_path = str(tmp_path / "spool.f32")

        with pytest.raises(DiskSpaceExhausted):
            buf._spool_oldest()
        # 丢块从 total_samples 扣除：duration 与实际产出一致
        assert buf.total_samples == 10
        assert buf._spooled_samples == 0


# =============================================================================
# AUD-17601：take 落盘后台化
# =============================================================================


def _sine(seconds: float = 0.2, freq: float = 440.0) -> np.ndarray:
    n = int(48000 * seconds)
    t = np.arange(n, dtype=np.float32) / 48000.0
    return (0.5 * np.sin(2 * np.pi * freq * t)).reshape(1, -1)


class TestAud17601BackgroundFlush:
    def _drain_events(self, qapp, mgr: RecordingManager, timeout_s: float = 5.0):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            qapp.processEvents()
            if mgr.state == RecordingManager.STATE_IDLE:
                return True
            time.sleep(0.01)
        return False

    def test_background_flush_defers_and_completes(self, qapp, tmp_path: Path):
        cfg = RecordingConfig(
            output_dir=str(tmp_path), scene_name="T", pre_roll_seconds=0,
        )
        mgr = RecordingManager(cfg)
        mgr.enable_background_flush = True
        completed: list = []
        mgr.on_take_completed = completed.append
        assert mgr.start_recording()
        mgr.inject_pcm(_sine(0.2))
        take = mgr.stop_recording()
        assert take is None  # 延迟落盘（后台 flush）
        assert mgr.state == RecordingManager.STATE_FLUSHING
        # flushing 是终止态：重复 stop/start 被拒
        assert mgr.stop_recording() is None
        assert mgr.start_recording() is False
        assert self._drain_events(qapp, mgr), "后台落盘未在超时内完成"
        assert len(completed) == 1
        assert completed[0].status == "completed"
        assert Path(completed[0].file_path).exists()
        assert len(mgr.takes) == 1
        assert mgr.state == RecordingManager.STATE_IDLE

    def test_default_remains_synchronous(self, tmp_path: Path):
        cfg = RecordingConfig(
            output_dir=str(tmp_path), scene_name="T", pre_roll_seconds=0,
        )
        mgr = RecordingManager(cfg)
        assert mgr.start_recording()
        mgr.inject_pcm(_sine(0.1))
        take = mgr.stop_recording()
        assert take is not None
        assert take.status == "completed"
        assert mgr.state == RecordingManager.STATE_IDLE
