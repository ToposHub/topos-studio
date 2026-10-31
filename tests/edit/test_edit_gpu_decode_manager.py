from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager


@dataclass
class _FakeDecodedFrame:
    def __init__(self, frame_index: int) -> None:
        self.frame_index = frame_index


class _FakeSequentialPlanarSource:
    def __init__(self, current_frame: int = 0) -> None:
        self.current_frame = current_frame
        self.read_next_calls: list[int] = []
        self.read_frame_planar_calls: list[int] = []

    def read_next_frame(self) -> _FakeDecodedFrame:
        frame = _FakeDecodedFrame(self.current_frame)
        self.read_next_calls.append(self.current_frame)
        self.current_frame += 1
        return frame

    def read_frame_planar(self, frame_index: int) -> _FakeDecodedFrame:
        self.read_frame_planar_calls.append(frame_index)
        self.current_frame = frame_index + 1
        return _FakeDecodedFrame(frame_index)

    def close(self) -> None:
        pass


class _FakeInteractivePixelBufferSource:
    def __init__(self) -> None:
        self.calls: list[tuple[int, object, object]] = []

    def read_frame_pixel_buffer_interactive(
        self,
        frame_index: int,
        *,
        is_stale=None,
        deadline_at=None,
    ) -> _FakeDecodedFrame:
        self.calls.append((frame_index, is_stale, deadline_at))
        return _FakeDecodedFrame(frame_index)

    def close(self) -> None:
        pass


class _FakeInteractivePlanarSource(_FakeSequentialPlanarSource):
    def __init__(self, current_frame: int = 0) -> None:
        super().__init__(current_frame=current_frame)
        self.read_next_interactive_calls: list[tuple[object, object]] = []
        self.read_planar_interactive_calls: list[tuple[int, object, object]] = []

    def read_next_frame_interactive(self, *, is_stale=None, deadline_at=None) -> _FakeDecodedFrame:
        self.read_next_interactive_calls.append((is_stale, deadline_at))
        return self.read_next_frame()

    def read_frame_planar_interactive(
        self,
        frame_index: int,
        *,
        is_stale=None,
        deadline_at=None,
        prefer_seek: bool = False,
    ) -> _FakeDecodedFrame:
        self.read_planar_interactive_calls.append((frame_index, is_stale, deadline_at, prefer_seek))
        return self.read_frame_planar(frame_index)


class _SlowInteractivePlanarSource(_FakeInteractivePlanarSource):
    def __init__(self, current_frame: int = 0, delay: float = 0.05) -> None:
        super().__init__(current_frame=current_frame)
        self.delay = delay

    def read_frame_planar_interactive(
        self,
        frame_index: int,
        *,
        is_stale=None,
        deadline_at=None,
        prefer_seek: bool = False,
    ) -> _FakeDecodedFrame | None:
        end_at = time.perf_counter() + self.delay
        while time.perf_counter() < end_at:
            if is_stale is not None and is_stale():
                return None
            time.sleep(0.001)
        return super().read_frame_planar_interactive(
            frame_index,
            is_stale=is_stale,
            deadline_at=deadline_at,
            prefer_seek=prefer_seek,
        )


class _CloseTrackingSource(_FakeSequentialPlanarSource):
    def __init__(self, current_frame: int = 0) -> None:
        super().__init__(current_frame=current_frame)
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_preview_target_size_is_bucketed_to_avoid_decoder_reopen() -> None:
    manager = EditGpuDecodeManager()

    assert manager._normalize_target_size((555, 312)) == (576, 324)
    assert manager._normalize_target_size((577, 325)) == (576, 324)


def test_planar_small_forward_gap_uses_sequential_decode() -> None:
    manager = EditGpuDecodeManager()
    source = _FakeSequentialPlanarSource(current_frame=20)

    frame = manager._read_source_frame(source, "planar", 27)

    assert frame is not None
    assert frame.frame_index == 27
    assert source.read_next_calls == [20, 21, 22, 23, 24, 25, 26, 27]
    assert source.read_frame_planar_calls == []


def test_planar_large_forward_gap_falls_back_to_precise_read() -> None:
    manager = EditGpuDecodeManager()
    source = _FakeSequentialPlanarSource(current_frame=20)

    frame = manager._read_source_frame(source, "planar", 90)

    assert frame is not None
    assert frame.frame_index == 90
    assert source.read_next_calls == []
    assert source.read_frame_planar_calls == [90]


def test_stream_key_keeps_same_asset_clips_on_independent_sources() -> None:
    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]
    created_sources: list[_FakeSequentialPlanarSource] = []

    def _create_source(asset_path: str, target_size, **kwargs):
        source = _FakeSequentialPlanarSource(current_frame=0)
        created_sources.append(source)
        return source, "planar"

    manager._create_source = _create_source  # type: ignore[method-assign]

    frame_a, type_a = manager.get_frame(
        "/same.mov",
        0,
        target_size=(640, 360),
        stream_key="clip-a",
    )
    frame_b, type_b = manager.get_frame(
        "/same.mov",
        0,
        target_size=(640, 360),
        stream_key="clip-b",
    )
    frame_a2, type_a2 = manager.get_frame(
        "/same.mov",
        1,
        target_size=(704, 396),
        stream_key="clip-a",
    )

    assert frame_a is not None and frame_a.frame_index == 0
    assert frame_b is not None and frame_b.frame_index == 0
    assert frame_a2 is not None and frame_a2.frame_index == 1
    assert (type_a, type_b, type_a2) == ("planar", "planar", "planar")
    assert len(created_sources) == 3


def test_preview_uses_ready_disk_proxy_without_extra_scaling(tmp_path) -> None:
    manager = EditGpuDecodeManager()
    proxy_path = tmp_path / "clip-proxy.mov"
    proxy_path.write_bytes(b"proxy")
    manager._get_disk_proxy_info = lambda asset_path: SimpleNamespace(  # type: ignore[method-assign]
        is_ready=True,
        proxy_path=str(proxy_path),
        needs_generation=False,
    )
    created: list[tuple[str, object]] = []

    def _create_source(asset_path: str, target_size, **kwargs):
        created.append((asset_path, target_size))
        return _FakeSequentialPlanarSource(current_frame=0), "planar"

    manager._create_source = _create_source  # type: ignore[method-assign]

    frame, frame_type = manager.get_frame(
        "/original-prores.mov",
        0,
        target_size=(1920, 1080),
        stream_key="clip-proxy",
    )

    assert frame is not None and frame.frame_index == 0
    assert frame_type == "planar"
    assert created == [(str(proxy_path), None)]


def test_preview_decode_does_not_start_proxy_transcode_when_missing() -> None:
    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: SimpleNamespace(  # type: ignore[method-assign]
        is_ready=False,
        proxy_path=None,
        needs_generation=True,
    )
    created: list[tuple[str, object]] = []

    def _create_source(asset_path: str, target_size, **kwargs):
        created.append((asset_path, target_size))
        return _FakeSequentialPlanarSource(current_frame=0), "planar"

    manager._create_source = _create_source  # type: ignore[method-assign]

    frame, frame_type = manager.get_frame(
        "/missing-proxy.mov",
        0,
        target_size=(1280, 720),
        stream_key="clip-missing-proxy",
    )

    assert frame is not None and frame.frame_index == 0
    assert frame_type == "planar"
    assert created == [("/missing-proxy.mov", (1280, 720))]


def test_4k_prores_uses_native_planar_decode_for_1080p_program_preview() -> None:
    """4K ProRes must avoid CPU swscale before the GPU program FBO pass."""
    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]
    manager._probe_codec = lambda asset_path: "prores"  # type: ignore[method-assign]
    manager._probe_resolution = lambda asset_path: (3840, 2160)  # type: ignore[method-assign]

    spec = manager._resolve_preview_decode_source(
        "/4k-prores4444.mov",
        (1920, 1080),
    )

    assert spec.target_size is None
    assert spec.source_kind == "original_native_planar"


def test_native_planar_policy_keeps_cpu_only_sub_1080p_requests_small() -> None:
    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]
    manager._probe_codec = lambda asset_path: "prores"  # type: ignore[method-assign]
    manager._probe_resolution = lambda asset_path: (3840, 2160)  # type: ignore[method-assign]

    spec = manager._resolve_preview_decode_source(
        "/4k-prores4444.mov",
        (1280, 720),
    )

    assert spec.target_size == (1280, 720)
    assert spec.source_kind == "original_downscaled"


def test_topos_preview_passes_target_to_native_reduced_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Topos V7-B must receive the preview box instead of full-decoding for GPU scale."""
    manager = EditGpuDecodeManager()
    manager._probe_codec = lambda asset_path: "topos"  # type: ignore[method-assign]
    manager._should_use_vt_planar_source = (  # type: ignore[method-assign]
        lambda asset_path, target_size: False
    )
    created: list[dict[str, object]] = []

    class FakeToposSource:
        def open(self) -> None:
            pass

        def close(self) -> None:
            pass

    from src.shared.media.factory import MediaSourceFactory

    def create(path: str, **kwargs: object):
        created.append({"path": path, **kwargs})
        return FakeToposSource()

    monkeypatch.setattr(MediaSourceFactory, "create", create)

    source, source_type = manager._create_source(
        "/12k.topos.mov", (1920, 1080), vt_policy="auto",
    )

    assert source is not None
    assert source_type == "planar"
    assert created == [{
        "path": "/12k.topos.mov",
        "backend": "topos",
        "target_size": (1920, 1080),
        # 预览关滤波开关（默认关）随 source 创建一起下传
        "no_deblock": False,
    }]


def test_topos_preview_routes_by_container_before_codec_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Topos MOV must bypass FFmpeg metadata and hardware probing."""
    from src.shared.media import topos_source
    from src.shared.media.factory import MediaSourceFactory

    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]
    manager._probe_codec = lambda asset_path: pytest.fail(  # type: ignore[method-assign]
        "Topos should be routed by its container signature"
    )
    monkeypatch.setattr(topos_source, "is_topos_movie", lambda path: True)

    created: list[dict[str, object]] = []

    class FakeToposSource:
        def open(self) -> None:
            pass

        def close(self) -> None:
            pass

    def create(path: str, **kwargs: object):
        created.append({"path": path, **kwargs})
        return FakeToposSource()

    monkeypatch.setattr(MediaSourceFactory, "create", create)

    spec = manager._resolve_preview_decode_source("/topos.mov", (1920, 1080))
    source, source_type = manager._create_source(
        spec.decode_path, spec.target_size, vt_policy=spec.vt_policy,
    )

    assert spec.source_kind == "original_topos_reduced"
    assert source is not None
    assert source_type == "planar"
    assert created == [{
        "path": "/topos.mov",
        "backend": "topos",
        "target_size": (1920, 1080),
        "no_deblock": False,
    }]


def test_h26x_vt_native_decode_is_used_when_preview_target_downscales_master() -> None:
    manager = EditGpuDecodeManager()
    manager._probe_codec = lambda asset_path: "h264"  # type: ignore[method-assign]
    manager._probe_resolution = lambda asset_path: (3840, 2160)  # type: ignore[method-assign]

    assert manager._should_use_vt_planar_source("/4k.mov", (1280, 720))
    assert manager._should_use_vt_planar_source("/4k.mov", (3840, 2160))
    assert manager._should_use_vt_planar_source("/4k.mov", None)


def test_non_h26x_vt_planar_still_respects_preview_downscale_guard() -> None:
    manager = EditGpuDecodeManager()
    manager._probe_codec = lambda asset_path: "dnxhd"  # type: ignore[method-assign]
    manager._probe_resolution = lambda asset_path: (3840, 2160)  # type: ignore[method-assign]

    # DNxHR (VideoToolbox 不支持)：小预览目标仍回退 FFmpeg 降采样解码。
    assert not manager._should_use_vt_planar_source("/4k-dnxhr.mov", (1280, 720))
    assert manager._should_use_vt_planar_source("/4k-dnxhr.mov", (3840, 2160))
    assert manager._should_use_vt_planar_source("/4k-dnxhr.mov", None)


def test_prores_uses_native_vt_even_for_downscaled_preview() -> None:
    """ProRes 走 VideoToolbox 原生解码，缩放交给 GPU 预览 FBO。

    VT 自带的 ProRes 解码器（T2/Apple Silicon 硬解，其它机器为优化软解）
    快于 FFmpeg 软解，且 CVPixelBuffer 零拷贝输出省掉逐帧 planar CPU 拷贝，
    因此小预览目标也不再回退 FFmpeg 降采样。
    """
    manager = EditGpuDecodeManager()
    manager._probe_codec = lambda asset_path: "prores"  # type: ignore[method-assign]
    manager._probe_resolution = lambda asset_path: (3840, 2160)  # type: ignore[method-assign]

    assert manager._should_use_vt_planar_source("/4k-prores.mov", (1280, 720))
    assert manager._should_use_vt_planar_source("/4k-prores.mov", (3840, 2160))
    assert manager._should_use_vt_planar_source("/4k-prores.mov", None)


def test_vp8_uses_planar_yuv_route_for_gpu_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VP8 must skip packed RGB and feed YUV planes to the GPU renderer."""
    manager = EditGpuDecodeManager()
    manager._probe_codec = lambda asset_path: "vp8"  # type: ignore[method-assign]

    created: list[dict[str, object]] = []

    class FakeFFmpegSource:
        def __init__(self, asset_path: str, **kwargs: object) -> None:
            created.append({"asset_path": asset_path, **kwargs})

        def open(self) -> None:
            pass

    import src.shared.media.ffmpeg_source as ffmpeg_source_mod

    monkeypatch.setattr(ffmpeg_source_mod, "FFmpegMediaSource", FakeFFmpegSource)

    source, source_type = manager._create_source(
        "/vp8.webm",
        (1920, 1080),
        vt_policy="prefer_ffmpeg",
    )

    assert source is not None
    assert source_type == "planar"
    assert created == [{
        "asset_path": "/vp8.webm",
        "target_size": (1920, 1080),
        "preserve_planes": True,
        "output_mode": "planar_yuv",
    }]


def test_h26x_downscaled_preview_tries_hwaccel_before_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = EditGpuDecodeManager()
    manager._probe_codec = lambda asset_path: "h264"  # type: ignore[method-assign]
    calls: list[dict[str, object]] = []

    class FakeSource:
        def close(self) -> None:
            pass

    class FakeStatus:
        decoder_type = "macos_vt_zero_copy"
        hwaccel_enabled = True

    class FakeFactory:
        def create_with_hwaccel(self, **kwargs):
            calls.append(kwargs)
            return FakeSource(), FakeStatus()

    import src.features.edit.core.edit_gpu_decode_manager as decode_manager_mod
    import src.shared.media.factory as factory_mod

    monkeypatch.setattr(decode_manager_mod.sys, "platform", "darwin")
    monkeypatch.setattr(factory_mod, "MediaSourceFactory", lambda: FakeFactory())

    source, source_type = manager._create_source("/4k-h264.mov", (1280, 720))

    assert source is not None
    assert source_type == "vt_pixel_buffer"
    assert calls
    assert calls[0]["path"] == "/4k-h264.mov"
    assert calls[0]["backend"] == "ffmpeg"
    assert calls[0]["auto_open"] is False


def test_warmup_source_deduplicates_inflight_requests() -> None:
    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]
    started = threading.Event()
    release = threading.Event()
    create_calls: list[tuple[str, object]] = []

    def _create_source(asset_path: str, target_size, **kwargs):
        create_calls.append((asset_path, target_size))
        started.set()
        release.wait(1.0)
        return _FakeSequentialPlanarSource(current_frame=0), "planar"

    manager._create_source = _create_source  # type: ignore[method-assign]

    manager.warmup_source("/warm.mov", target_size=(640, 360), stream_key="clip-warm")
    assert started.wait(1.0)
    manager.warmup_source("/warm.mov", target_size=(640, 360), stream_key="clip-warm")
    release.set()

    assert manager.wait_for_warmups(timeout=1.0)
    assert create_calls == [("/warm.mov", (640, 360))]


def test_get_frame_reuses_source_from_inflight_warmup() -> None:
    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]
    started = threading.Event()
    release = threading.Event()
    create_calls: list[str] = []
    result: list[tuple[object, str]] = []

    def _create_source(asset_path: str, target_size, **kwargs):
        create_calls.append(asset_path)
        started.set()
        release.wait(1.0)
        return _FakeSequentialPlanarSource(current_frame=0), "planar"

    manager._create_source = _create_source  # type: ignore[method-assign]
    manager.warmup_source("/reuse.mov", target_size=(640, 360), stream_key="clip-reuse")
    assert started.wait(1.0)

    def _read_frame() -> None:
        result.append(manager.get_frame(
            "/reuse.mov",
            0,
            target_size=(640, 360),
            stream_key="clip-reuse",
        ))

    reader = threading.Thread(target=_read_frame)
    reader.start()
    time.sleep(0.02)
    release.set()
    reader.join(1.0)

    assert manager.wait_for_warmups(timeout=1.0)
    assert len(create_calls) == 1
    assert result
    frame, frame_type = result[0]
    assert frame is not None and getattr(frame, "frame_index") == 0
    assert frame_type == "planar"


def test_create_failure_does_not_cache_none_source() -> None:
    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]
    manager._create_source = lambda asset_path, target_size, **kwargs: (None, "cpu")  # type: ignore[method-assign]

    frame, frame_type = manager.get_frame(
        "/missing.mov",
        0,
        target_size=(640, 360),
        stream_key="clip-missing",
    )

    assert frame is None
    assert frame_type == ""
    assert manager._sources == {}


def test_vt_pixel_buffer_source_counts_as_gpu_source() -> None:
    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]
    source_spec = manager._resolve_preview_decode_source("/vt.mov", (640, 360))
    source_key = manager._make_source_key("clip-vt", source_spec)

    manager._source_types[source_key] = "vt_pixel_buffer"

    assert manager.is_gpu_source("/vt.mov") is True


def test_interactive_decode_uses_source_interactive_pixel_buffer_read() -> None:
    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]
    source = _FakeInteractivePixelBufferSource()
    source_spec = manager._resolve_preview_decode_source("/interactive.mov", (640, 360))
    source_key = manager._make_source_key("clip-interactive", source_spec)
    manager._sources[source_key] = source
    manager._source_types[source_key] = "vt_pixel_buffer"
    manager._source_cache[source_key] = (source, "vt_pixel_buffer")
    deadline_at = time.perf_counter() + 1.0

    frame, frame_type = manager.get_frame_interactive(
        "/interactive.mov",
        42,
        target_size=(640, 360),
        stream_key="clip-interactive",
        is_stale=lambda: False,
        deadline_at=deadline_at,
    )

    assert frame is not None and frame.frame_index == 42
    assert frame_type == "vt_pixel_buffer"
    assert source.calls
    assert source.calls[0][0] == 42
    assert source.calls[0][2] == deadline_at


def test_vt_planar_decode_keeps_hardware_frame_type() -> None:
    manager = EditGpuDecodeManager()
    source = _FakeSequentialPlanarSource(current_frame=0)

    frame, frame_type = manager._decode_frame(
        source,
        "vt_planar",
        3,
        target_size=(1280, 720),
        start=time.perf_counter(),
    )

    assert frame is not None and frame.frame_index == 3
    assert frame_type == "vt_planar"


def test_interactive_planar_catchup_uses_source_interactive_next_read() -> None:
    manager = EditGpuDecodeManager()
    source = _FakeInteractivePlanarSource(current_frame=10)
    deadline_at = time.perf_counter() + 1.0

    frame = manager._read_source_frame_interactive(
        source,
        "vt_planar",
        12,
        is_stale=lambda: False,
        deadline_at=deadline_at,
    )

    assert frame is not None and frame.frame_index == 12
    assert len(source.read_next_interactive_calls) == 3
    assert source.read_next_interactive_calls[0][1] == deadline_at
    assert source.read_frame_planar_calls == []


def test_interactive_planar_seek_uses_source_interactive_exact_read() -> None:
    manager = EditGpuDecodeManager()
    source = _FakeInteractivePlanarSource(current_frame=10)
    deadline_at = time.perf_counter() + 1.0

    frame = manager._read_source_frame_interactive(
        source,
        "vt_planar",
        100,
        is_stale=lambda: False,
        deadline_at=deadline_at,
    )

    assert frame is not None and frame.frame_index == 100
    assert source.read_next_interactive_calls == []
    assert source.read_planar_interactive_calls
    assert source.read_planar_interactive_calls[0][0] == 100
    assert source.read_planar_interactive_calls[0][2] == deadline_at


def test_interactive_vt_planar_prefers_seek_when_gop_cost_beats_forward_gap() -> None:
    manager = EditGpuDecodeManager()
    source = _FakeInteractivePlanarSource(current_frame=0)
    source.metadata = SimpleNamespace(codec_name="hevc")  # type: ignore[attr-defined]
    deadline_at = time.perf_counter() + 1.0

    frame = manager._read_source_frame_interactive(
        source,
        "vt_planar",
        74,
        is_stale=lambda: False,
        deadline_at=deadline_at,
    )

    assert frame is not None and frame.frame_index == 74
    assert source.read_next_interactive_calls == []
    assert source.read_planar_interactive_calls[0][0] == 74
    assert source.read_planar_interactive_calls[0][3] is True


def test_interactive_source_pool_prefers_forward_hot_cursor() -> None:
    manager = EditGpuDecodeManager()
    primary = _FakeSequentialPlanarSource(current_frame=690)
    pool_before = _FakeSequentialPlanarSource(current_frame=0)
    pool_far = _FakeSequentialPlanarSource(current_frame=300)
    source_key = ("clip", "/asset.mov", "/asset.mov", (1280, 720))
    manager._interactive_source_pool[source_key] = [
        (pool_before, "vt_planar"),
        (pool_far, "vt_planar"),
    ]

    selected_source, selected_type = manager._select_interactive_source(
        source_key,
        primary,
        "vt_planar",
        74,
    )

    assert selected_source is pool_before
    assert selected_type == "vt_planar"


def test_interactive_source_selection_skips_busy_pool_source() -> None:
    manager = EditGpuDecodeManager()
    primary = _FakeSequentialPlanarSource(current_frame=690)
    pool_busy = _FakeSequentialPlanarSource(current_frame=70)
    pool_free = _FakeSequentialPlanarSource(current_frame=20)
    source_key = ("clip", "/asset.mov", "/asset.mov", (1280, 720))
    manager._interactive_source_pool[source_key] = [
        (pool_busy, "vt_planar"),
        (pool_free, "vt_planar"),
    ]
    manager._interactive_busy_sources[id(pool_busy)] = pool_busy

    selected_source, selected_type = manager._select_interactive_source(
        source_key,
        primary,
        "vt_planar",
        74,
        reserve=True,
    )

    assert selected_source is pool_free
    assert selected_type == "vt_planar"
    assert manager._interactive_busy_sources.get(id(pool_free)) is pool_free
    manager._release_interactive_source(pool_free)


def test_vt_warmup_creates_interactive_source_pool() -> None:
    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]
    created: list[_CloseTrackingSource] = []

    def _create_source(asset_path, target_size, **kwargs):
        source = _CloseTrackingSource(current_frame=0)
        created.append(source)
        return source, "vt_planar"

    manager._create_source = _create_source  # type: ignore[method-assign]
    manager.warmup_source("/pool.mov", target_size=(1280, 720), stream_key="clip-pool")

    assert manager.wait_for_warmups(timeout=1.0)
    source_spec = manager._resolve_preview_decode_source("/pool.mov", (1280, 720))
    source_key = manager._make_source_key("clip-pool", source_spec)
    assert source_key in manager._sources
    assert len(manager._interactive_source_pool.get(source_key, [])) == 2
    assert len(created) == 3
    manager.close_all()
    assert all(source.closed for source in created)


def test_interactive_preposition_uses_spare_pool_source() -> None:
    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]
    primary = _FakeInteractivePlanarSource(current_frame=0)
    pool = _FakeInteractivePlanarSource(current_frame=0)
    source_spec = manager._resolve_preview_decode_source("/preposition.mov", (1280, 720))
    source_key = manager._make_source_key("clip-preposition", source_spec)
    manager._sources[source_key] = primary
    manager._source_types[source_key] = "vt_planar"
    manager._source_cache[source_key] = (primary, "vt_planar")
    manager._interactive_source_pool[source_key] = [(pool, "vt_planar")]

    started = manager.preposition_interactive_frame(
        "/preposition.mov",
        74,
        target_size=(1280, 720),
        stream_key="clip-preposition",
        deadline_ms=100.0,
    )

    assert started is True
    assert manager.wait_for_warmups(timeout=1.0)
    deadline = time.perf_counter() + 1.0
    while pool.current_frame != 75 and time.perf_counter() < deadline:
        time.sleep(0.005)
    assert pool.read_planar_interactive_calls
    assert pool.current_frame == 75
    assert primary.read_planar_interactive_calls == []


def test_interactive_preposition_latest_token_cancels_previous_task() -> None:
    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]
    primary = _FakeInteractivePlanarSource(current_frame=0)
    pool_a = _SlowInteractivePlanarSource(current_frame=0, delay=0.08)
    pool_b = _FakeInteractivePlanarSource(current_frame=0)
    source_spec = manager._resolve_preview_decode_source("/latest-preposition.mov", (1280, 720))
    source_key = manager._make_source_key("clip-latest-preposition", source_spec)
    manager._sources[source_key] = primary
    manager._source_types[source_key] = "vt_planar"
    manager._source_cache[source_key] = (primary, "vt_planar")
    manager._interactive_source_pool[source_key] = [
        (pool_a, "vt_planar"),
        (pool_b, "vt_planar"),
    ]

    first = manager.preposition_interactive_frame(
        "/latest-preposition.mov",
        74,
        target_size=(1280, 720),
        stream_key="clip-latest-preposition",
        deadline_ms=200.0,
    )
    second = manager.preposition_interactive_frame(
        "/latest-preposition.mov",
        120,
        target_size=(1280, 720),
        stream_key="clip-latest-preposition",
        deadline_ms=200.0,
    )

    assert first is True
    assert second is True
    deadline = time.perf_counter() + 1.0
    while manager._interactive_prefetch_events and time.perf_counter() < deadline:
        time.sleep(0.005)
    assert pool_a.read_planar_interactive_calls == []
    assert pool_b.read_planar_interactive_calls
    assert pool_b.current_frame == 121


def test_interactive_decode_aborts_before_source_creation_when_stale() -> None:
    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]
    created: list[str] = []

    def _create_source(asset_path: str, target_size, **kwargs):
        created.append(asset_path)
        return _FakeSequentialPlanarSource(current_frame=0), "planar"

    manager._create_source = _create_source  # type: ignore[method-assign]

    frame, frame_type = manager.get_frame_interactive(
        "/stale.mov",
        10,
        target_size=(640, 360),
        stream_key="clip-stale",
        is_stale=lambda: True,
        deadline_at=time.perf_counter() + 1.0,
    )

    assert frame is None
    assert frame_type == "interactive_stale"
    assert created == []


def test_interactive_decode_starts_warmup_instead_of_sync_cold_create() -> None:
    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]
    created: list[str] = []
    warmups: list[tuple[str, object, object]] = []

    def _create_source(asset_path: str, target_size, **kwargs):
        created.append(asset_path)
        return _FakeSequentialPlanarSource(current_frame=0), "planar"

    def _warmup_source(asset_path: str, target_size=None, stream_key=None):
        warmups.append((asset_path, target_size, stream_key))

    manager._create_source = _create_source  # type: ignore[method-assign]
    manager.warmup_source = _warmup_source  # type: ignore[method-assign]

    frame, frame_type = manager.get_frame_interactive(
        "/cold.mov",
        10,
        target_size=(640, 360),
        stream_key="clip-cold",
        is_stale=lambda: False,
        deadline_at=time.perf_counter() + 0.001,
    )

    assert frame is None
    assert frame_type == "interactive_stale"
    assert created == []
    assert warmups == [("/cold.mov", (640, 360), "clip-cold")]


def test_interactive_sequential_catchup_drops_progress_that_is_too_far_on_stale() -> None:
    manager = EditGpuDecodeManager()
    source = _FakeInteractivePlanarSource(current_frame=20)

    frame = manager._read_source_frame_interactive(
        source,
        "vt_planar",
        35,
        is_stale=lambda: source.current_frame >= 25,
        deadline_at=time.perf_counter() + 1.0,
    )

    # Eleven frames behind is too inaccurate for a frame-accurate timeline
    # scrub; keep the previous displayed frame until the newest exact request
    # can be decoded.
    assert frame is None


def test_interactive_sequential_catchup_drops_far_progress_on_stale() -> None:
    manager = EditGpuDecodeManager()
    source = _FakeInteractivePlanarSource(current_frame=20)

    frame = manager._read_source_frame_interactive(
        source,
        "vt_planar",
        50,
        is_stale=lambda: source.current_frame >= 25,
        deadline_at=time.perf_counter() + 1.0,
    )

    assert frame is None


def test_interactive_deadline_allows_hot_4k_forward_catchup() -> None:
    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]
    manager._probe_codec = lambda asset_path: "h264"  # type: ignore[method-assign]
    manager._probe_resolution = lambda asset_path: (3840, 2160)  # type: ignore[method-assign]
    source = _FakeSequentialPlanarSource(current_frame=100)
    source_spec = manager._resolve_preview_decode_source("/hot4k.mp4", (1280, 720))
    source_key = manager._make_source_key("clip-hot4k", source_spec)
    manager._sources[source_key] = source
    manager._source_types[source_key] = "vt_pixel_buffer"
    manager._source_cache[source_key] = (source, "vt_pixel_buffer")

    deadline_ms = manager.estimate_interactive_decode_deadline_ms(
        "/hot4k.mp4",
        120,
        target_size=(1280, 720),
        stream_key="clip-hot4k",
    )

    assert deadline_ms > 40.0


def test_interactive_deadline_gives_2k_h26x_exact_seek_room() -> None:
    """1080p/2K scrub should wait for the exact frame, not a distant proxy frame."""
    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]
    manager._probe_codec = lambda asset_path: "h264"  # type: ignore[method-assign]
    manager._probe_resolution = lambda asset_path: (1920, 1080)  # type: ignore[method-assign]
    source = _FakeSequentialPlanarSource(current_frame=0)
    source_spec = manager._resolve_preview_decode_source("/hot2k.mp4", (1920, 1080))
    source_key = manager._make_source_key("clip-hot2k", source_spec)
    manager._sources[source_key] = source
    manager._source_types[source_key] = "vt_pixel_buffer"
    manager._source_cache[source_key] = (source, "vt_pixel_buffer")

    deadline_ms = manager.estimate_interactive_decode_deadline_ms(
        "/hot2k.mp4",
        240,
        target_size=(1920, 1080),
        stream_key="clip-hot2k",
    )

    assert deadline_ms >= 160.0


def test_interactive_deadline_caps_8k_h26x_random_seek() -> None:
    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]
    manager._probe_codec = lambda asset_path: "h264"  # type: ignore[method-assign]
    manager._probe_resolution = lambda asset_path: (7680, 4320)  # type: ignore[method-assign]
    source = _FakeSequentialPlanarSource(current_frame=0)
    source_spec = manager._resolve_preview_decode_source("/hot8k.mp4", (1280, 720))
    source_key = manager._make_source_key("clip-hot8k", source_spec)
    manager._sources[source_key] = source
    manager._source_types[source_key] = "vt_pixel_buffer"
    manager._source_cache[source_key] = (source, "vt_pixel_buffer")

    deadline_ms = manager.estimate_interactive_decode_deadline_ms(
        "/hot8k.mp4",
        1000,
        target_size=(1280, 720),
        stream_key="clip-hot8k",
    )

    assert deadline_ms <= 40.0


def test_interactive_deadline_gives_4k_h26x_room_without_unbounded_seek() -> None:
    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]
    manager._probe_codec = lambda asset_path: "hevc"  # type: ignore[method-assign]
    manager._probe_resolution = lambda asset_path: (3840, 2160)  # type: ignore[method-assign]
    source = _FakeSequentialPlanarSource(current_frame=0)
    source_spec = manager._resolve_preview_decode_source("/hot4k-h265.mov", (1280, 720))
    source_key = manager._make_source_key("clip-hot4k-h265", source_spec)
    manager._sources[source_key] = source
    manager._source_types[source_key] = "vt_planar"
    manager._source_cache[source_key] = (source, "vt_planar")

    deadline_ms = manager.estimate_interactive_decode_deadline_ms(
        "/hot4k-h265.mov",
        689,
        target_size=(1280, 720),
        stream_key="clip-hot4k-h265",
    )

    assert 100.0 <= deadline_ms <= 120.0


def test_interactive_deadline_uses_pool_forward_cursor() -> None:
    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]
    manager._probe_codec = lambda asset_path: "hevc"  # type: ignore[method-assign]
    manager._probe_resolution = lambda asset_path: (3840, 2160)  # type: ignore[method-assign]
    primary = _FakeSequentialPlanarSource(current_frame=690)
    pool_before = _FakeSequentialPlanarSource(current_frame=0)
    source_spec = manager._resolve_preview_decode_source("/pool-deadline.mov", (1280, 720))
    source_key = manager._make_source_key("clip-pool-deadline", source_spec)
    manager._sources[source_key] = primary
    manager._source_types[source_key] = "vt_planar"
    manager._source_cache[source_key] = (primary, "vt_planar")
    manager._interactive_source_pool[source_key] = [(pool_before, "vt_planar")]

    deadline_ms = manager.estimate_interactive_decode_deadline_ms(
        "/pool-deadline.mov",
        74,
        target_size=(1280, 720),
        stream_key="clip-pool-deadline",
    )

    assert deadline_ms >= 140.0


def test_live_decode_reuses_one_source_without_parallel_prefetch() -> None:
    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]
    sources: list[_FakeSequentialPlanarSource] = []

    def _create_source(asset_path, target_size, **kwargs):
        source = _FakeSequentialPlanarSource(current_frame=0)
        sources.append(source)
        return source, 'planar'

    manager._create_source = _create_source  # type: ignore[method-assign]
    for frame_index in (4, 5, 6):
        frame, frame_type = manager.get_frame(
            '/prefetch.mov',
            frame_index,
            target_size=(640, 360),
            stream_key='clip-prefetch',
        )
        assert frame is not None and frame.frame_index == frame_index
        assert frame_type == 'planar'

    assert len(sources) == 1
    assert not hasattr(manager, 'prefetch_frames')
    manager.close_all()


def test_interactive_intra_planar_seeks_directly_even_for_small_forward_gap() -> None:
    """DNxHR/ProRes 等 intra planar 源：精确 seek（~5ms）永远优于顺序追赶。

    实测 4K DNxHR：SLICE 线程下 seek 后首帧 ~5.4ms，而顺序追赶是
    gap × 每帧成本，即使 gap=3 也更贵。intra 每帧都是关键帧，
    seek 即精确，无 GOP walk。
    """
    manager = EditGpuDecodeManager()
    source = _FakeSequentialPlanarSource(current_frame=10)
    source.metadata = SimpleNamespace(codec_name="dnxhd")  # type: ignore[attr-defined]

    frame = manager._read_source_frame_interactive(
        source,
        "planar",
        15,  # 小 gap=5：旧逻辑会顺序追赶 5 帧
        is_stale=lambda: False,
        deadline_at=time.perf_counter() + 1.0,
    )

    assert frame is not None and frame.frame_index == 15
    assert source.read_next_calls == []  # 不做顺序追赶
    assert source.read_frame_planar_calls == [15]  # 直接精确 seek


def test_interactive_gop_planar_keeps_sequential_catchup() -> None:
    """GOP planar 源保持顺序追赶（帧线程 AUTO 的 walk 吞吐仍有价值）。"""
    manager = EditGpuDecodeManager()
    source = _FakeSequentialPlanarSource(current_frame=10)
    source.metadata = SimpleNamespace(codec_name="h264")  # type: ignore[attr-defined]

    frame = manager._read_source_frame_interactive(
        source,
        "planar",
        15,
        is_stale=lambda: False,
        deadline_at=time.perf_counter() + 1.0,
    )

    assert frame is not None and frame.frame_index == 15
    assert len(source.read_next_calls) == 6  # 顺序追赶（fake 从当前帧读起）


# ================================================================
# Playback-only VT routing（硬解探测 → prefer_ffmpeg / hw_or_ffmpeg）
# ================================================================

def _patch_hw_probe(monkeypatch, verdict):
    from src.shared.media.macos_vt import hardware_capability as hc

    calls = []

    def fake(path, codec, bit_depth, width=0, height=0):
        calls.append((codec, bit_depth))
        return verdict

    monkeypatch.setattr(hc, "vt_hardware_decode_available", fake)
    return calls


def _make_routed_manager(monkeypatch, verdict, codec="hevc", pix_fmt="yuv420p10le"):
    # 播放 VT 策略在 _playback_vt_policy 有 darwin 平台门（非 macOS 返回
    # 'auto'）。这些用例测的就是 darwin 策略路由，越过平台门使全平台可跑
    # （真实 macOS 行为由 B 线回归覆盖，Windows 不改变该门本身）。
    monkeypatch.setattr(sys, "platform", "darwin")
    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]
    manager._probe_codec = lambda asset_path: codec  # type: ignore[method-assign]
    manager._probe_pix_fmt = lambda asset_path: pix_fmt  # type: ignore[method-assign]
    manager._probe_resolution = lambda asset_path: (3840, 2160)  # type: ignore[method-assign]
    calls = _patch_hw_probe(monkeypatch, verdict)
    return manager, calls


def test_playback_routes_to_ffmpeg_when_vt_is_software_only() -> None:
    manager, calls = _make_routed_manager(monkeypatch_stub(), False)
    spec = manager._resolve_preview_decode_source("/10bit.mov", (1920, 1080), for_playback=True)
    assert spec.vt_policy == "prefer_ffmpeg"
    # 4K master + 1080p target → native planar decode + GPU scale
    assert spec.source_kind == "original_native_planar"
    assert calls, "policy must consult the hardware probe"


def monkeypatch_stub():
    """Return a real pytest monkeypatch bound to the current test."""
    import pytest
    return pytest.MonkeyPatch()


def test_playback_routes_hw_or_ffmpeg_when_hardware_exists() -> None:
    manager, calls = _make_routed_manager(monkeypatch_stub(), True)
    spec = manager._resolve_preview_decode_source("/10bit.mov", (1920, 1080), for_playback=True)
    assert spec.vt_policy == "hw_or_ffmpeg"
    assert spec.source_kind == "original_native_planar"
    assert calls


def test_scrub_resolution_never_consults_playback_policy() -> None:
    manager, calls = _make_routed_manager(monkeypatch_stub(), False)
    spec = manager._resolve_preview_decode_source("/10bit.mov", (1920, 1080), for_playback=False)
    assert spec.vt_policy == "auto"
    assert calls == []


def test_playback_8bit_h26x_stays_auto_without_probe() -> None:
    manager, calls = _make_routed_manager(monkeypatch_stub(), False, pix_fmt="yuv420p")
    spec = manager._resolve_preview_decode_source("/8bit.mov", (1920, 1080), for_playback=True)
    assert spec.vt_policy == "auto"
    assert calls == []


def test_playback_and_scrub_source_keys_differ() -> None:
    manager, _ = _make_routed_manager(monkeypatch_stub(), False)
    playback_spec = manager._resolve_preview_decode_source("/10bit.mov", (1920, 1080), for_playback=True)
    scrub_spec = manager._resolve_preview_decode_source("/10bit.mov", (1920, 1080), for_playback=False)
    key_playback = manager._make_source_key("s", playback_spec)
    key_scrub = manager._make_source_key("s", scrub_spec)
    assert key_playback != key_scrub


def test_create_source_prefer_ffmpeg_skips_vt_branches() -> None:
    manager = EditGpuDecodeManager()
    manager._probe_codec = lambda asset_path: "hevc"  # type: ignore[method-assign]

    opened = []
    from src.shared.media.macos_vt import source as vt_source_mod
    from src.shared.media.macos_vt import capability as vt_cap
    from src.shared.media import ffmpeg_source as ffmpeg_mod

    monkey = monkeypatch_stub()
    monkey.setattr(vt_cap, "is_zero_copy_available", lambda codec="": True)
    monkey.setattr(
        vt_source_mod.MacOSVtZeroCopySource, "open",
        lambda self, path, *, require_zero_copy=True, require_hardware=False: opened.append(path) or True,
    )
    created_instances = []

    class _FakeFFmpeg:
        def __init__(self, path, target_size=None, preserve_planes=False, output_mode=""):
            created_instances.append((path, output_mode))

        def open(self):
            return True

        read_frame_planar = lambda self, i: None  # noqa: E731

    monkey.setattr(ffmpeg_mod, "FFmpegMediaSource", _FakeFFmpeg)

    try:
        source, source_type = manager._create_source(
            "/10bit.mov", None, vt_policy="prefer_ffmpeg",
        )
        assert opened == [], "prefer_ffmpeg must not open any VT session"
        assert source_type == "planar"
        assert created_instances and created_instances[0][1] == "planar_yuv"
    finally:
        monkey.undo()


def test_create_source_hw_or_ffmpeg_requires_hardware_session() -> None:
    manager = EditGpuDecodeManager()
    manager._probe_codec = lambda asset_path: "hevc"  # type: ignore[method-assign]

    open_calls = []
    from src.shared.media.macos_vt import source as vt_source_mod
    from src.shared.media.macos_vt import capability as vt_cap
    from src.shared.media import ffmpeg_source as ffmpeg_mod

    monkey = monkeypatch_stub()
    # 同 _make_routed_manager：VT 硬件会话门在 darwin 分支（平台门见
    # _create_source 分支 2），越过平台门使全平台可跑。
    monkey.setattr(sys, "platform", "darwin")
    monkey.setattr(vt_cap, "is_zero_copy_available", lambda codec="": True)

    def fake_open(self, path, *, require_zero_copy=True, require_hardware=False):
        open_calls.append(require_hardware)
        return not require_hardware  # hardware-required open fails here

    monkey.setattr(vt_source_mod.MacOSVtZeroCopySource, "open", fake_open)
    monkey.setattr(
        vt_source_mod.MacOSVtZeroCopySource, "close", lambda self: None,
    )

    class _FakeFFmpeg:
        def __init__(self, path, target_size=None, preserve_planes=False, output_mode=""):
            pass

        def open(self):
            return True

        read_frame_planar = lambda self, i: None  # noqa: E731

    monkey.setattr(ffmpeg_mod, "FFmpegMediaSource", _FakeFFmpeg)

    try:
        source, source_type = manager._create_source(
            "/10bit.mov", None, vt_policy="hw_or_ffmpeg",
        )
        assert open_calls == [True], "hw_or_ffmpeg must request a hardware-required session"
        assert source_type == "planar", "denied hardware session must fall to FFmpeg planar"
    finally:
        monkey.undo()


def test_warmup_source_pool_gating() -> None:
    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]
    manager._probe_codec = lambda asset_path: "h264"  # type: ignore[method-assign]
    manager._probe_resolution = lambda asset_path: (1920, 1080)  # type: ignore[method-assign]
    manager._probe_pix_fmt = lambda asset_path: "yuv420p"  # type: ignore[method-assign]

    pool_calls = []
    monkey = monkeypatch_stub()
    monkey.setattr(
        manager, "_ensure_interactive_source_pool_warmup",
        lambda *a, **k: pool_calls.append(a),
    )
    try:
        # Source already cached as VT → fast path decides pool warmup.
        class _FakeVt:
            def close(self):
                pass

        spec = manager._resolve_preview_decode_source("/warm.mov", (1280, 720))
        key = manager._make_source_key(manager._normalize_stream_key("/warm.mov", None), spec)
        manager._sources[key] = _FakeVt()
        manager._source_types[key] = "vt_pixel_buffer"
        manager._source_cache[key] = (_FakeVt(), "vt_pixel_buffer")

        manager.warmup_source("/warm.mov", target_size=(1280, 720), warm_interactive_pool=False)
        assert pool_calls == [], "playback warmup must not spawn scrub pool sessions"

        manager.warmup_source("/warm.mov", target_size=(1280, 720))
        assert pool_calls, "scrub warmup keeps the interactive pool"
    finally:
        monkey.undo()
