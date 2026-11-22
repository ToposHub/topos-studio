from __future__ import annotations

import platform
import sys
import threading
from ctypes import POINTER, c_int64, cast
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from src.shared.export.gpu_encode_surface import (
    GpuEncodeSurface,
    GpuEncodeSurfaceDescriptor,
)
from src.shared.gpu.metal.context import MetalComputeContext
from src.shared.media.gpu_frame import (
    GpuFramePlane,
    GpuFrameRelease,
    GpuPlaneResource,
)
from src.shared.media.macos_vt.vt_compression import (
    VideoToolboxCompressionError,
    VideoToolboxCompressionSession,
    VideoToolboxPacketMuxer,
    _packet_is_keyframe,
)


@pytest.mark.parametrize("rejected", [None, "ColorPrimaries"])
def test_vt_compression_options_without_hardware(
    monkeypatch: pytest.MonkeyPatch, rejected: str | None,
) -> None:
    from src.shared.media.macos_vt import vt_compression

    properties: dict[str, Any] = {}
    objects: dict[int, str | int] = {}
    invalidated: list[int] = []
    released: list[int] = []

    def retain_fake(value: str | int) -> int:
        identifier = len(released) + len(objects) + 20
        objects[identifier] = value
        return identifier

    def release_fake(ref: Any) -> None:
        identifier = getattr(ref, "value", ref)
        released.append(identifier)
        objects.pop(identifier, None)

    def start_fake(*args: Any) -> int:
        args[-1]._obj.value = 11
        return 0

    def set_fake(_session: Any, key: int, value: int) -> int:
        name = str(objects[key])
        properties[name] = objects[value]
        return -12900 if rejected == name else 0

    cf = SimpleNamespace(
        CFStringCreateWithCString=lambda _parent, value, _encoding: retain_fake(value.decode("ascii")),
        CFNumberCreate=lambda _parent, _kind, data: retain_fake(cast(data, POINTER(c_int64)).contents.value),
        CFRelease=release_fake,
    )
    monkeypatch.setattr(vt_compression, "_compression_core_foundation", lambda: cf)
    monkeypatch.setattr(vt_compression, "_MACOS_AVAILABLE", True)
    monkeypatch.setattr(vt_compression, "_CM_LIB", object())
    monkeypatch.setattr(vt_compression, "_VT_LIB", SimpleNamespace(
        VTCompressionSessionCreate=start_fake,
        VTSessionSetProperty=set_fake,
        VTCompressionSessionInvalidate=lambda ref: invalidated.append(ref.value),
    ))

    def create() -> VideoToolboxCompressionSession:
        return VideoToolboxCompressionSession(
            width=1920, height=1080, require_hardware=False,
            h264_profile="high", bit_rate=50_000_000, bt709_limited=True,
        )

    if rejected:
        with pytest.raises(VideoToolboxCompressionError, match="ColorPrimaries:-12900"):
            create()
    else:
        session = create()
        session.abort()
        assert properties["TransferFunction"] == properties["YCbCrMatrix"] == "ITU_R_709_2"
    assert properties["ProfileLevel"] == "H264_High_AutoLevel"
    assert properties["AverageBitRate"] == 50_000_000
    assert properties["ColorPrimaries"] == "ITU_R_709_2"
    assert invalidated == [11] and released.count(11) == 1
    assert not objects


def test_vt_muxer_sdr_tags_without_hardware(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = SimpleNamespace()
    container = SimpleNamespace(add_stream=lambda *_args, **_kwargs: stream, close=lambda: None)
    monkeypatch.setitem(sys.modules, "av", SimpleNamespace(open=lambda *_args, **_kwargs: container))
    muxer = VideoToolboxPacketMuxer("test.mp4", width=1920, height=1080, bt709_limited=True)
    try:
        assert muxer.stream.pix_fmt == "yuv420p"
        assert (stream.color_primaries, stream.color_trc, stream.colorspace, stream.color_range) == (1, 1, 1, 1)
    finally:
        muxer.close()


@pytest.mark.parametrize("mode", [
    "success", "cancel", "short", "flush_error", "trailer_error", "late_cancel",
    "cancel_during_flush", "cancel_during_trailer",
])
def test_vt_bridge_transaction_without_hardware(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    from src.shared.media.macos_vt import gpu_export_bridge

    destination = tmp_path / "master.mp4"
    destination.write_bytes(b"existing master")
    calls: list[str] = []
    session_options: dict[str, Any] = {}
    finalizing = threading.Event()
    cancelled = threading.Event()

    def request_cancel() -> None:
        if finalizing.wait(timeout=5):
            cancelled.set()

    class Muxer:
        def __init__(self, path: str, **settings: Any) -> None:
            self.path = Path(path)
            self.packet_count = 0
            assert self.path.parent == tmp_path and self.path.suffix == ".mp4"
            assert self.path != destination
            assert settings["bt709_limited"] is True

        def write_packet(self) -> None:
            self.packet_count += 1

        def close(self) -> None:
            calls.append("trailer")
            assert destination.read_bytes() == b"existing master"
            if mode == "trailer_error":
                raise OSError("test trailer failure")
            self.path.write_bytes(b"complete movie")
            if mode == "late_cancel":
                bridge.abort()
            if mode == "cancel_during_trailer":
                finalizing.set()
                assert cancelled.wait(timeout=5)

        def abort(self) -> None:
            calls.append("muxer cleanup")

    class Session:
        last_error = ""
        dropped_packets = 0
        submit = object()

        def __init__(self, *, on_packet: Callable[[], None], **settings: Any) -> None:
            session_options.update(settings)
            self._on_packet = on_packet

        def close(self) -> None:
            calls.append("flush")
            if mode == "cancel_during_flush":
                finalizing.set()
                assert cancelled.wait(timeout=5)
            if mode == "flush_error":
                self.last_error = "test flush failure"
            self._on_packet()
            if mode != "short":
                self._on_packet()

        def abort(self) -> None:
            calls.append("invalidate")

    monkeypatch.setattr(gpu_export_bridge, "VideoToolboxPacketMuxer", Muxer)
    monkeypatch.setattr(gpu_export_bridge, "VideoToolboxCompressionSession", Session)
    monkeypatch.setattr(gpu_export_bridge, "VideoToolboxGpuSurfaceProvider",
                        lambda *_args, **_kwargs: SimpleNamespace(close=lambda: calls.append("release")))
    bridge = gpu_export_bridge.VideoToolboxGpuExportBridge(
        object(), str(destination), width=1920, height=1080, expected_frames=2,
        h264_profile="high", bit_rate=50_000_000, bt709_limited=True,
        should_cancel=cancelled.is_set,
    )
    assert session_options["h264_profile"] == "high"
    assert session_options["bit_rate"] == 50_000_000 and session_options["bt709_limited"] is True

    if mode == "cancel":
        bridge.abort()
        assert calls == ["invalidate", "release", "muxer cleanup"]
    elif mode == "short":
        with pytest.raises(VideoToolboxCompressionError, match="packet_count_mismatch:1!=2"):
            bridge.close()
    elif mode == "flush_error":
        with pytest.raises(VideoToolboxCompressionError, match="test flush failure"):
            bridge.close()
    elif mode == "trailer_error":
        with pytest.raises(OSError, match="test trailer failure"):
            bridge.close()
    elif mode in {"cancel_during_flush", "cancel_during_trailer"}:
        cancellation = threading.Thread(target=request_cancel)
        cancellation.start()
        try:
            assert bridge.close() is False
        finally:
            cancellation.join(timeout=5)
        assert not cancellation.is_alive() and cancelled.is_set()
    else:
        assert bridge.close() is (mode == "success")
        assert calls == ["flush", "release", "trailer", "muxer cleanup"]
    assert bridge.close() is (mode == "success")
    assert destination.read_bytes() == (b"complete movie" if mode == "success" else b"existing master")
    assert list(tmp_path.iterdir()) == [destination]


def _fake_surface(
    pixel_buffer: object,
    width: int,
    height: int,
    submitter=None,
) -> GpuEncodeSurface:
    planes = tuple(
        GpuFramePlane(
            component=component,
            api="metal",
            texture_id=0,
            target="2d",
            width=width if component == "Y" else width // 2,
            height=height if component == "Y" else height // 2,
            resource=GpuPlaneResource(
                api="metal",
                native_handle=0,
                resource_format="r8unorm" if component == "Y" else "rg8unorm",
                width=width if component == "Y" else width // 2,
                height=height if component == "Y" else height // 2,
            ),
        )
        for component in ("Y", "UV")
    )
    return GpuEncodeSurface(
        GpuEncodeSurfaceDescriptor(
            backend="videotoolbox",
            pixel_format="nv12",
            width=width,
            height=height,
            bit_depth=8,
            device_identity="test",
            planes=planes,
            pts=0,
            native_handle=pixel_buffer,
        ),
        GpuFrameRelease(),
        submitter or (lambda _surface: None),
    )


def test_h264_length_prefixed_idr_is_keyframe() -> None:
    # One four-byte length-prefixed IDR NAL unit.
    assert _packet_is_keyframe(b"\x00\x00\x00\x02\x65\x88", "h264", 4)
    assert not _packet_is_keyframe(b"\x00\x00\x00\x02\x41\x88", "h264", 4)


@pytest.mark.skipif(
    platform.system() != "Darwin",
    reason="requires the macOS VideoToolbox runtime",
)
def test_native_videotoolbox_submitter_consumes_cv_pixel_buffer(tmp_path) -> None:
    import Quartz

    width, height = 1920, 1080
    status, pixel_buffer = Quartz.CVPixelBufferCreate(
        None,
        width,
        height,
        Quartz.kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
        {
            Quartz.kCVPixelBufferIOSurfacePropertiesKey: {},
            Quartz.kCVPixelBufferMetalCompatibilityKey: True,
        },
        None,
    )
    assert int(status) == 0

    packets = []
    packet_ready = threading.Event()
    muxer = VideoToolboxPacketMuxer(
        str(tmp_path / "native-vt.mp4"),
        width=width,
        height=height,
    )

    def on_packet(packet) -> None:
        packets.append(packet)
        muxer.write_packet(packet)
        packet_ready.set()

    try:
        session = VideoToolboxCompressionSession(
            width=width,
            height=height,
            codec="h264",
            on_packet=on_packet,
            require_hardware=True,
        )
    except VideoToolboxCompressionError as exc:
        muxer.close()
        pytest.skip(str(exc))

    surface = _fake_surface(
        pixel_buffer,
        width,
        height,
        submitter=session.submit,
    )
    try:
        receipt = surface.submit()
        assert receipt["queued"] is True
        session.complete()
        assert packet_ready.wait(2.0)
        assert packets
        assert packets[0].codec == "h264"
        assert packets[0].data
        assert packets[0].parameter_sets
        assert session.pending_count == 0
    finally:
        session.close()
        muxer.close()
        surface.release()

    import av

    with av.open(str(tmp_path / "native-vt.mp4")) as container:
        muxed_packets = list(container.demux(video=0))
    assert muxer.packet_count == 1
    assert muxed_packets and muxed_packets[0].size > 0


@pytest.mark.skipif(
    not MetalComputeContext.is_supported(),
    reason="requires an available Metal device",
)
def test_m5_metal_surface_to_videotoolbox_mp4_smoke(tmp_path) -> None:
    """Prove one real Metal NV12 frame reaches a playable VT H.264 file."""
    import av

    from src.features.edit.core.timeline_metal_export_renderer import (
        TimelineMetalExportRenderer,
    )
    from src.shared.export.gpu_trail import ExportGpuTrail
    from src.shared.gpu.metal.context import MetalComputeContext
    from src.shared.media.gpu_frame import safe_release_gpu_frame
    from src.shared.media.macos_vt.gpu_export_bridge import (
        VideoToolboxGpuExportBridge,
    )
    from src.shared.video.frame_provider_service import VideoFrameProviderService

    source_path = Path(__file__).resolve().parents[1] / "test_videos" / "2K_h264.mp4"
    if not source_path.is_file():
        pytest.skip("real H.264 test material is unavailable")

    frame_provider = VideoFrameProviderService()
    clip_id = frame_provider.open_clip(str(source_path), share_by_path=False)
    frame = frame_provider.request_metal_frame(clip_id, 0, quality="master")
    if frame is None or getattr(frame, "pixel_format", "") != "nv12":
        frame_provider.close_clip(clip_id)
        pytest.skip("VideoToolbox NV12 Metal input is unavailable")

    output_path = tmp_path / "m5-metal-videotoolbox.mp4"
    context = MetalComputeContext()
    try:
        bridge = VideoToolboxGpuExportBridge(
            context,
            str(output_path),
            width=frame.width,
            height=frame.height,
            fps=25,
        )
    except VideoToolboxCompressionError as exc:
        safe_release_gpu_frame(frame)
        frame_provider.close_clip(clip_id)
        pytest.skip(str(exc))

    trail = ExportGpuTrail("m5-metal-videotoolbox")
    renderer = TimelineMetalExportRenderer(trail)
    surface = bridge.acquire(
        width=frame.width,
        height=frame.height,
        frame_index=0,
        pts=0,
    )
    try:
        result = renderer.render_metal_texture_frame(
            frame,
            None,
            frame.width,
            frame.height,
            1.0,
            output_yuv420p=True,
            output_surface=surface,
        )
        assert result is surface, renderer.last_fallback_reason
        assert renderer.last_fallback_reason is None
        assert trail.snapshot().stage("yuv_conversion").cpu_download_bytes == 0
        result.submit()
        bridge.close()

        with av.open(str(output_path)) as container:
            decoded = list(container.decode(video=0))
        assert bridge.packet_count == 1
        assert len(decoded) == 1
        assert decoded[0].width == frame.width
        assert decoded[0].height == frame.height
    finally:
        bridge.close()
        surface.release()
        renderer.release()
        safe_release_gpu_frame(frame)
        frame_provider.close_clip(clip_id)


@pytest.mark.skipif(
    not MetalComputeContext.is_supported(),
    reason="requires an available Metal device",
)
@pytest.mark.parametrize(
    ("use_default_factory", "export_codec"),
    (
        (False, "h264"),
        (True, "h264"),
        (False, "hevc"),
        (True, "hevc"),
    ),
)
def test_timeline_exporter_native_bridge_multi_frame(
    tmp_path, monkeypatch, use_default_factory: bool, export_codec: str,
) -> None:
    """Run explicit/factory native VT routes through TimelineExporter."""
    import av

    from src.features.edit.core.project import EditProject
    from src.features.edit.core.timeline_v2 import (
        ClipInstance,
        EditTrack,
        TimelineSequence,
    )
    from src.features.edit.core.timeline_export import (
        ExportCodec,
        ExportRangeType,
        ExportSource,
        TimelineExportConfig,
        TimelineExporter,
    )
    from src.shared.media.metadata import Rational
    from src.shared.media.macos_vt.gpu_export_bridge import (
        VideoToolboxGpuExportBridge,
    )
    from src.shared.video.frame_provider_service import VideoFrameProviderService

    monkeypatch.setenv("TOPOS_EXPORT_GPU_RENDER", "1")
    if use_default_factory:
        monkeypatch.setenv("TOPOS_EXPORT_NATIVE_VT", "1")
    source_path = Path(__file__).resolve().parents[1] / "test_videos" / "2K_h264.mp4"
    if not source_path.is_file():
        pytest.skip("real H.264 test material is unavailable")

    project = EditProject.create("M5 Timeline")
    # T-P0-10 契约：真实 import 会持久化 pixel_format，原生桥资格检查
    # 对缺失 fail-closed——手工构造的元数据须补齐该声明
    project = project.import_asset(
        path=str(source_path),
        media_type="video",
        metadata={
            "width": 1920,
            "height": 1080,
            "fps": 25.0,
            "total_frames": 3,
            "duration_seconds": 3 / 25.0,
            "pixel_format": "yuv420p10le" if export_codec == "hevc" else "yuv420p",
        },
    )
    asset_id = next(iter(project.assets))
    track_id = "m5-v1"
    clip = ClipInstance(
        clip_id="m5-clip",
        asset_id=asset_id,
        track_id=track_id,
        timeline_start=0,
        timeline_duration=3,
        source_start=0,
        source_duration=3,
    )
    sequence = TimelineSequence(
        name="M5 Timeline",
        width=1920,
        height=1080,
        fps=Rational(25, 1),
        video_tracks=(
            EditTrack(
                track_id=track_id,
                name="Video 1",
                track_type="video",
                index=0,
                clips=(clip,),
            ),
        ),
    )
    output_path = tmp_path / f"timeline-native-bridge-{export_codec}.mp4"
    bridge = None
    if not use_default_factory:
        context = MetalComputeContext()
        try:
            bridge = VideoToolboxGpuExportBridge(
                context,
                str(output_path),
                width=1920,
                height=1080,
                fps=25,
                codec=export_codec,
                pixel_format="p010" if export_codec == "hevc" else "nv12",
            )
        except VideoToolboxCompressionError as exc:
            pytest.skip(str(exc))
    elif use_default_factory:
        # 与显式桥变体的跳过语义对齐：先探测本机 VT 会话能力，
        # 平台拒绝该 codec/位深会话时（如本机 HEVC p010 报 -12900）
        # 跳过，而非任其落回软件编码再伪装成导出失败
        try:
            _probe = VideoToolboxGpuExportBridge(
                MetalComputeContext(),
                str(tmp_path / f"vt-capability-probe-{export_codec}.mp4"),
                width=1920,
                height=1080,
                fps=25,
                codec=export_codec,
                pixel_format="p010" if export_codec == "hevc" else "nv12",
            )
        except VideoToolboxCompressionError as exc:
            pytest.skip(str(exc))
        else:
            _probe.close()

    frame_provider = VideoFrameProviderService()
    exporter = TimelineExporter(
        project=project,
        sequence=sequence,
        frame_provider=frame_provider,
        gpu_encode_surface_provider=bridge,
    )
    try:
        outcome = exporter.export_timeline(
            TimelineExportConfig(
                source=ExportSource.TIMELINE,
                timeline_id=sequence.timeline_id,
                range_type=ExportRangeType.ENTIRE,
                codec=(
                    ExportCodec.HEVC
                    if export_codec == "hevc" else ExportCodec.H264
                ),
                output_path=str(output_path),
                resolution=(1920, 1080),
                fps=25.0,
                include_audio=False,
                burn_subtitles=False,
            )
        )
        assert outcome.success, outcome.cause
        actual_bridge = bridge or exporter._gpu_encode_surface_provider
        assert actual_bridge is not None
        assert actual_bridge.packet_count == 3
        trail = exporter.gpu_trail_snapshot
        assert trail is not None
        assert trail.stage("yuv_conversion").cpu_download_bytes == 0

        with av.open(str(output_path)) as container:
            decoded = list(container.decode(video=0))
        assert len(decoded) == 3
        assert (decoded[0].width, decoded[0].height) == (1920, 1080)
    finally:
        if bridge is not None:
            bridge.close()


@pytest.mark.skipif(
    not MetalComputeContext.is_supported(),
    reason="requires an available Metal device",
)
@pytest.mark.parametrize("use_default_factory", (False, True))
def test_timeline_exporter_native_bridge_multitrack_surface(
    tmp_path, monkeypatch, use_default_factory: bool,
) -> None:
    """Run a two-layer Metal stack directly into VT NV12.

    The explicit bridge case also carries a native Blur effect; the factory
    case stays on the proven no-effect eligibility subset.
    """
    import av

    from src.features.edit.core.effect_instance import EffectInstance
    from src.features.edit.core.project import EditProject
    from src.features.edit.core.timeline_v2 import (
        ClipInstance,
        EditTrack,
        TimelineSequence,
    )
    from src.features.edit.core.timeline_export import (
        ExportCodec,
        ExportRangeType,
        ExportSource,
        TimelineExportConfig,
        TimelineExporter,
    )
    from src.shared.media.metadata import Rational
    from src.shared.media.macos_vt.gpu_export_bridge import (
        VideoToolboxGpuExportBridge,
    )
    from src.shared.video.frame_provider_service import VideoFrameProviderService

    monkeypatch.setenv("TOPOS_EXPORT_GPU_RENDER", "1")
    if use_default_factory:
        monkeypatch.setenv("TOPOS_EXPORT_NATIVE_VT", "1")
    source_path = Path(__file__).resolve().parents[1] / "test_videos" / "2K_h264.mp4"
    if not source_path.is_file():
        pytest.skip("real H.264 test material is unavailable")

    project = EditProject.create("M5 Multitrack")
    # T-P0-10 契约：原生桥资格检查要求元数据声明 pixel_format（缺失
    # fail-closed），手工构造的元数据须补齐（源 2K_h264.mp4 为 8 位）
    project = project.import_asset(
        path=str(source_path),
        media_type="video",
        metadata={
            "width": 1920,
            "height": 1080,
            "fps": 25.0,
            "total_frames": 3,
            "duration_seconds": 3 / 25.0,
            "pixel_format": "yuv420p",
        },
    )
    asset_id = next(iter(project.assets))
    clip_one = ClipInstance(
        clip_id="m5-multi-clip-1", asset_id=asset_id, track_id="m5-v1",
        timeline_start=0, timeline_duration=3, source_start=0, source_duration=3,
    )
    clip_two = ClipInstance(
        clip_id="m5-multi-clip-2", asset_id=asset_id, track_id="m5-v2",
        timeline_start=0, timeline_duration=3, source_start=0, source_duration=3,
        effects=(
            EffectInstance(effect_type="blur", params={"radius": 4.0}, order=0),
        ) if not use_default_factory else (),
    )
    sequence = TimelineSequence(
        name="M5 Multitrack",
        width=1920,
        height=1080,
        fps=Rational(25, 1),
        video_tracks=(
            EditTrack(
                track_id="m5-v1", name="Video 1", track_type="video", index=0,
                clips=(clip_one,),
            ),
            EditTrack(
                track_id="m5-v2", name="Video 2", track_type="video", index=1,
                clips=(clip_two,),
            ),
        ),
    )
    output_path = tmp_path / "timeline-native-multitrack.mp4"
    bridge = None
    if not use_default_factory:
        try:
            bridge = VideoToolboxGpuExportBridge(
                MetalComputeContext(), str(output_path),
                width=1920, height=1080, fps=25,
                codec="h264", pixel_format="nv12",
            )
        except VideoToolboxCompressionError as exc:
            pytest.skip(str(exc))

    frame_provider = VideoFrameProviderService()
    exporter = TimelineExporter(
        project=project,
        sequence=sequence,
        frame_provider=frame_provider,
        gpu_encode_surface_provider=bridge,
    )
    try:
        outcome = exporter.export_timeline(
            TimelineExportConfig(
                source=ExportSource.TIMELINE,
                timeline_id=sequence.timeline_id,
                range_type=ExportRangeType.ENTIRE,
                codec=ExportCodec.H264,
                output_path=str(output_path),
                resolution=(1920, 1080),
                fps=25.0,
                include_audio=False,
                burn_subtitles=False,
            )
        )
        assert outcome.success, outcome.cause
        actual_bridge = bridge or exporter._gpu_encode_surface_provider
        assert actual_bridge is not None
        assert actual_bridge.packet_count == 3
        trail = exporter.gpu_trail_snapshot
        assert trail is not None
        assert trail.stage("yuv_conversion").cpu_download_bytes == 0
        with av.open(str(output_path)) as container:
            decoded = list(container.decode(video=0))
        assert len(decoded) == 3
        assert (decoded[0].width, decoded[0].height) == (1920, 1080)
    finally:
        if bridge is not None:
            bridge.close()


@pytest.mark.skipif(
    not MetalComputeContext.is_supported(),
    reason="requires an available Metal device",
)
def test_timeline_exporter_native_bridge_spatial_transition_surface(
    tmp_path, monkeypatch,
) -> None:
    """Run a procedural wipe stack directly into a VT NV12 surface."""
    import av

    from src.features.edit.core.project import EditProject
    from src.features.edit.core.timeline_v2 import (
        ClipInstance,
        EditTrack,
        TimelineSequence,
        TransitionInstance,
    )
    from src.features.edit.core.timeline_export import (
        ExportCodec,
        ExportRangeType,
        ExportSource,
        TimelineExportConfig,
        TimelineExporter,
    )
    from src.shared.media.metadata import Rational
    from src.shared.media.macos_vt.gpu_export_bridge import (
        VideoToolboxGpuExportBridge,
    )
    from src.shared.video.frame_provider_service import VideoFrameProviderService

    monkeypatch.setenv("TOPOS_EXPORT_GPU_RENDER", "1")
    source_path = Path(__file__).resolve().parents[1] / "test_videos" / "2K_h264.mp4"
    if not source_path.is_file():
        pytest.skip("real H.264 test material is unavailable")

    project = EditProject.create("M5 Spatial Transition")
    project = project.import_asset(
        path=str(source_path),
        media_type="video",
        metadata={
            "width": 1920, "height": 1080, "fps": 25.0,
            "total_frames": 6, "duration_seconds": 6 / 25.0,
        },
    )
    asset_id = next(iter(project.assets))
    left = ClipInstance(
        clip_id="m5-wipe-left", asset_id=asset_id, track_id="m5-wipe-track",
        timeline_start=0, timeline_duration=3, source_start=0, source_duration=3,
    )
    right = ClipInstance(
        clip_id="m5-wipe-right", asset_id=asset_id, track_id="m5-wipe-track",
        timeline_start=3, timeline_duration=3, source_start=0, source_duration=3,
    )
    wipe = TransitionInstance(
        transition_id="m5-wipe", transition_type="wipe",
        track_id="m5-wipe-track", left_clip_id=left.clip_id,
        right_clip_id=right.clip_id, center_frame=3, duration=2,
        alignment="centered", params={"direction": "left", "softness": 0.05},
    )
    sequence = TimelineSequence(
        name="M5 Spatial Transition", width=1920, height=1080,
        fps=Rational(25, 1),
        video_tracks=(EditTrack(
            track_id="m5-wipe-track", name="Video 1", track_type="video", index=0,
            clips=(left, right), transitions=(wipe,),
        ),),
    )
    output_path = tmp_path / "timeline-native-wipe.mp4"
    try:
        bridge = VideoToolboxGpuExportBridge(
            MetalComputeContext(), str(output_path),
            width=1920, height=1080, fps=25,
            codec="h264", pixel_format="nv12",
        )
    except VideoToolboxCompressionError as exc:
        pytest.skip(str(exc))
    frame_provider = VideoFrameProviderService()
    exporter = TimelineExporter(
        project=project, sequence=sequence, frame_provider=frame_provider,
        gpu_encode_surface_provider=bridge,
    )
    try:
        outcome = exporter.export_timeline(
            TimelineExportConfig(
                source=ExportSource.TIMELINE,
                timeline_id=sequence.timeline_id,
                range_type=ExportRangeType.ENTIRE,
                codec=ExportCodec.H264,
                output_path=str(output_path), resolution=(1920, 1080), fps=25.0,
                include_audio=False, burn_subtitles=False,
            )
        )
        assert outcome.success, outcome.cause
        assert bridge.packet_count == 6
        trail = exporter.gpu_trail_snapshot
        assert trail is not None
        assert trail.stage("yuv_conversion").cpu_download_bytes == 0
        with av.open(str(output_path)) as container:
            decoded = list(container.decode(video=0))
            assert len(decoded) == 6
    finally:
        bridge.close()


@pytest.mark.skipif(
    not MetalComputeContext.is_supported(),
    reason="requires an available Metal device",
)
def test_native_videotoolbox_bridge_muxes_audio_before_video_close(tmp_path) -> None:
    """Verify the native video bridge can carry TimelineExporter audio too."""
    import av
    import numpy as np

    from src.features.edit.core.timeline_metal_export_renderer import (
        TimelineMetalExportRenderer,
    )
    from src.shared.export.gpu_trail import ExportGpuTrail
    from src.shared.media.gpu_frame import safe_release_gpu_frame
    from src.shared.media.macos_vt.gpu_export_bridge import (
        VideoToolboxGpuExportBridge,
    )
    from src.shared.video.frame_provider_service import VideoFrameProviderService

    source_path = Path(__file__).resolve().parents[1] / "test_videos" / "2K_h264.mp4"
    if not source_path.is_file():
        pytest.skip("real H.264 test material is unavailable")

    frame_provider = VideoFrameProviderService()
    clip_id = frame_provider.open_clip(str(source_path), share_by_path=False)
    frame = frame_provider.request_metal_frame(clip_id, 0, quality="master")
    if frame is None or getattr(frame, "pixel_format", "") != "nv12":
        frame_provider.close_clip(clip_id)
        pytest.skip("VideoToolbox NV12 Metal input is unavailable")

    output_path = tmp_path / "native-vt-audio.mp4"
    context = MetalComputeContext()
    try:
        bridge = VideoToolboxGpuExportBridge(
            context,
            str(output_path),
            width=frame.width,
            height=frame.height,
            fps=25,
        )
        bridge.add_audio_stream("aac", sample_rate=48000, channels=2, bitrate=128)
    except VideoToolboxCompressionError as exc:
        safe_release_gpu_frame(frame)
        frame_provider.close_clip(clip_id)
        pytest.skip(str(exc))

    renderer = TimelineMetalExportRenderer(ExportGpuTrail("m5-vt-audio"))
    surface = bridge.acquire(
        width=frame.width,
        height=frame.height,
        frame_index=0,
        pts=0,
    )
    try:
        result = renderer.render_metal_texture_frame(
            frame,
            None,
            frame.width,
            frame.height,
            1.0,
            output_yuv420p=True,
            output_surface=surface,
        )
        assert result is surface, renderer.last_fallback_reason
        result.submit()
        bridge.write_audio_samples(
            np.zeros((2, 2048), dtype=np.float32), sample_rate=48000
        )
        bridge.close()

        with av.open(str(output_path)) as container:
            assert len(container.streams.video) == 1
            assert len(container.streams.audio) == 1
            assert list(container.decode(audio=0))
    finally:
        bridge.close()
        surface.release()
        renderer.release()
        safe_release_gpu_frame(frame)
        frame_provider.close_clip(clip_id)


@pytest.mark.skipif(
    not MetalComputeContext.is_supported(),
    reason="requires an available Metal device",
)
def test_m7_metal_p010_surface_to_videotoolbox_hevc_mp4_smoke(tmp_path) -> None:
    """Prove one real Metal P010 frame reaches a VT HEVC MP4 file."""
    import av

    from src.features.edit.core.timeline_metal_export_renderer import (
        TimelineMetalExportRenderer,
    )
    from src.shared.export.gpu_trail import ExportGpuTrail
    from src.shared.media.gpu_frame import safe_release_gpu_frame
    from src.shared.media.macos_vt.gpu_export_bridge import (
        VideoToolboxGpuExportBridge,
    )
    from src.shared.video.frame_provider_service import VideoFrameProviderService

    source_path = Path(__file__).resolve().parents[1] / "test_videos" / "2K_H265.mov"
    if not source_path.is_file():
        pytest.skip("real HEVC/P010 test material is unavailable")

    frame_provider = VideoFrameProviderService()
    clip_id = frame_provider.open_clip(str(source_path), share_by_path=False)
    frame = frame_provider.request_metal_frame(clip_id, 0, quality="master")
    if frame is None or getattr(frame, "pixel_format", "") != "p010":
        frame_provider.close_clip(clip_id)
        pytest.skip("VideoToolbox P010 Metal input is unavailable")
    frames = [frame]
    for frame_index in (1, 2):
        next_frame = frame_provider.request_metal_frame(
            clip_id, frame_index, quality="master"
        )
        if next_frame is None or getattr(next_frame, "pixel_format", "") != "p010":
            for pending_frame in frames:
                safe_release_gpu_frame(pending_frame)
            if next_frame is not None:
                safe_release_gpu_frame(next_frame)
            frame_provider.close_clip(clip_id)
            pytest.skip("VideoToolbox P010 multi-frame input is unavailable")
        frames.append(next_frame)

    output_path = tmp_path / "m7-metal-p010-hevc.mp4"
    context = MetalComputeContext()
    try:
        bridge = VideoToolboxGpuExportBridge(
            context,
            str(output_path),
            width=frame.width,
            height=frame.height,
            fps=25,
            codec="hevc",
            pixel_format="p010",
        )
    except VideoToolboxCompressionError as exc:
        for pending_frame in frames:
            safe_release_gpu_frame(pending_frame)
        frame_provider.close_clip(clip_id)
        pytest.skip(str(exc))

    trail = ExportGpuTrail("m7-vt-p010-hevc")
    renderer = TimelineMetalExportRenderer(trail)
    surfaces = []
    try:
        for frame_index, source_frame in enumerate(frames):
            surface = bridge.acquire(
                width=source_frame.width,
                height=source_frame.height,
                pixel_format="p010",
                frame_index=frame_index,
                pts=frame_index,
            )
            surfaces.append(surface)
            result = renderer.render_metal_texture_frame(
                source_frame,
                None,
                source_frame.width,
                source_frame.height,
                1.0,
                output_yuv420p=True,
                output_surface=surface,
            )
            assert result is surface, renderer.last_fallback_reason
            assert renderer.last_fallback_reason is None
            assert trail.snapshot().stage("yuv_conversion").cpu_download_bytes == 0
            result.submit()
        bridge.close()

        with av.open(str(output_path)) as container:
            decoded = list(container.decode(video=0))
        assert bridge.packet_count == 3
        assert len(decoded) == 3
        assert decoded[0].width == frame.width
        assert decoded[0].height == frame.height
    finally:
        bridge.close()
        for surface in surfaces:
            surface.release()
        renderer.release()
        for source_frame in frames:
            safe_release_gpu_frame(source_frame)
        frame_provider.close_clip(clip_id)
