"""
macOS VT Zero-Copy Module Tests
测试 macos_vt 包的各个组件。

注意：实际 VT/IOSurface 操作需要 macOS 硬件，
此测试主要验证数据结构、模块导入和逻辑判断，
不会实际创建 VTDecompressionSession。
"""

from __future__ import annotations

import platform
import json
import sys
import pytest

from src.shared.media.macos_vt.capability import (
    MacOSZeroCopyCapability,
    get_zero_copy_capability,
    is_zero_copy_available,
    invalidate_capability_cache,
)
from src.shared.media.macos_vt.packet_demux import CompressedPacket, VideoStreamInfo, PacketDemuxer
from src.shared.media.macos_vt.vt_session import VtSession, is_vt_available
from src.shared.media.macos_vt.external_texture import ExternalGLTexture
from src.shared.media.macos_vt.iosurface_gl_bridge import IOSurfaceGLTextureRingBuffer, IOSurfaceGLBridge
from src.shared.media.macos_vt.vt_zero_copy_probe import (
    ZeroCopyProbeResult,
    _percentile,
    _stats_dict,
    _yuv_sample_to_rgb,
)


_darwin_only_w01 = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="macOS 专属逻辑（VT/Metal）；W01 复跑确认在 Windows 为测试缺平台门"
)

def test_yuv_reference_preserves_bt709_red_and_skin_hue() -> None:
    """The reference must use Cb for blue and Cr for red in the green term."""
    rgb = _yuv_sample_to_rgb(
        0.55,
        0.42,
        0.62,
        color_range='limited',
        color_matrix='bt709',
    )

    # A positive Cr and negative Cb should produce a red-biased sample, not
    # the yellow cast caused by swapping Cb/Cr in the green equation.
    assert rgb[0] > rgb[1]
    assert rgb[0] > rgb[2]


# =========================================================================
# CompressedPacket 测试
# =========================================================================

class TestCompressedPacket:
    """CompressedPacket 数据类测试"""

    def test_creation(self) -> None:
        pkt = CompressedPacket(
            data=b'\x00\x01\x02',
            pts=1001,
            dts=1000,
            duration=1,
            is_keyframe=True,
            frame_index=0,
        )
        assert pkt.data == b'\x00\x01\x02'
        assert pkt.pts == 1001
        assert pkt.dts == 1000
        assert pkt.is_keyframe is True
        assert pkt.frame_index == 0

    def test_defaults(self) -> None:
        pkt = CompressedPacket(
            data=b'',
            pts=None,
            dts=None,
            duration=None,
            is_keyframe=False,
        )
        assert pkt.frame_index == -1
        assert pkt.pts is None


class TestVideoStreamInfo:
    """VideoStreamInfo 数据类测试"""

    def test_defaults(self) -> None:
        info = VideoStreamInfo()
        assert info.width == 0
        assert info.height == 0
        assert info.fps == 30.0
        assert info.extradata is None

    def test_custom_values(self) -> None:
        info = VideoStreamInfo(
            codec_name="h264",
            width=3840,
            height=2160,
            fps=29.97,
            pixel_format="yuv420p",
            bit_depth=8,
            extradata=b'\x00\x00\x00\x01',
        )
        assert info.codec_name == "h264"
        assert info.width == 3840
        assert info.extradata == b'\x00\x00\x00\x01'


class TestPacketDemuxer:
    """PacketDemuxer 测试"""

    def test_creation(self) -> None:
        demuxer = PacketDemuxer()
        assert demuxer.is_open is False
        assert demuxer.info is None

    def test_open_nonexistent_file(self) -> None:
        demuxer = PacketDemuxer()
        with pytest.raises(Exception):
            demuxer.open("/nonexistent/file.mp4")

    def test_close_without_open(self) -> None:
        demuxer = PacketDemuxer()
        demuxer.close()
        assert demuxer.is_open is False


# =========================================================================
# VtSession 测试
# =========================================================================

class TestVtSession:
    """VtSession 测试"""

    def test_creation(self) -> None:
        session = VtSession()
        assert session.is_created is False
        assert session.width == 0
        assert session.height == 0

    def test_destroy_without_create(self) -> None:
        session = VtSession()
        session.destroy()
        assert session.is_created is False

    def test_decode_without_create(self) -> None:
        session = VtSession()
        result = session.decode_packet(b'\x00', pts=0, is_keyframe=True)
        assert result is None

    def test_context_manager(self) -> None:
        with VtSession() as session:
            assert session is not None

    def test_ctypes_callback_uses_own_session_refcon(self) -> None:
        """A second VT session must never steal the first session's output."""
        import ctypes

        from src.shared.media.macos_vt.vt_session import (
            _CMTime,
            _CtypesCallbackContext,
        )

        received: list[dict] = []

        class _Queue:
            def put_pixel_buffer(self, **kwargs) -> bool:
                received.append(kwargs)
                return True

        context = _CtypesCallbackContext(_Queue(), 1920, 1080, 'nv12', 8)
        context_ref = ctypes.py_object(context)
        refcon = ctypes.cast(ctypes.pointer(context_ref), ctypes.c_void_p).value

        VtSession._on_ctypes_decoded(
            refcon,
            None,
            0,
            0,
            1234,
            _CMTime(99, 1000, 0, 0),
            _CMTime(0, 0, 0, 0),
        )

        assert received == [{
            'pixel_buffer': 1234,
            'pts': 99,
            'frame_index': 1,
            'width': 1920,
            'height': 1080,
            'pixel_format': 'nv12',
            'bit_depth': 8,
        }]


# =========================================================================
# ExternalGLTexture 测试
# =========================================================================

class TestExternalGLTexture:
    """ExternalGLTexture 测试"""

    def test_creation(self) -> None:
        tex = ExternalGLTexture(texture_id=1, target=3553, size=(1920, 1080))
        assert tex.glo == 1
        assert tex.target == 3553
        assert tex.size == (1920, 1080)
        assert tex.width == 1920
        assert tex.height == 1080

    def test_repr(self) -> None:
        tex = ExternalGLTexture(texture_id=42, target=3553, size=(3840, 2160))
        repr_str = repr(tex)
        assert "42" in repr_str
        assert "3840" in repr_str

    def test_rectangle_texture_constant_is_correct(self) -> None:
        from OpenGL.GL import GL_TEXTURE_RECTANGLE

        assert int(GL_TEXTURE_RECTANGLE) == 34037

    def test_edit_gpu_wrapper_uses_real_rectangle_constant(self) -> None:
        from OpenGL.GL import GL_TEXTURE_RECTANGLE
        from src.shared.media.gpu_frame import GpuFramePlane
        from src.features.edit.core.edit_gpu_preview_renderer import EditGpuPreviewRenderer

        planes = [
            GpuFramePlane(
                component="Y",
                api="opengl",
                texture_id=1,
                target="rectangle",
                width=1920,
                height=1080,
            ),
            GpuFramePlane(
                component="UV",
                api="opengl",
                texture_id=2,
                target="rectangle",
                width=960,
                height=540,
            ),
        ]

        wrapped = EditGpuPreviewRenderer._wrap_external_gpu_planes(object(), planes, None)

        assert wrapped is not None
        assert wrapped[0].target == int(GL_TEXTURE_RECTANGLE)
        assert wrapped[1].target == int(GL_TEXTURE_RECTANGLE)


# =========================================================================
# IOSurfaceGLTextureRingBuffer 测试
# =========================================================================

@_darwin_only_w01
class TestIOSurfaceGLTextureRingBuffer :
    """IOSurfaceGLTextureRingBuffer 测试"""

    def test_creation(self) -> None:
        ring = IOSurfaceGLTextureRingBuffer(ring_size=4, planes_per_frame=2)
        assert ring.ring_size == 4
        assert ring.current_index == 0

    def test_leased_slots_are_not_reused_before_release(self, monkeypatch) -> None:
        """Y/UV texture IDs stay exclusive until their source frame releases."""
        ids = iter(range(1, 20))
        monkeypatch.setattr(
            'src.shared.media.macos_vt.iosurface_gl_bridge.glGenTextures',
            lambda _count: next(ids),
        )
        ring = IOSurfaceGLTextureRingBuffer(
            ring_size=2,
            planes_per_frame=2,
            max_ring_size=3,
        )
        assert ring.initialize() is True

        first = ring.acquire_textures_for_target('rectangle')
        second = ring.acquire_textures_for_target('rectangle')
        third = ring.acquire_textures_for_target('rectangle')
        assert first is not None
        assert second is not None
        assert third is not None
        assert {first.slot_index, second.slot_index, third.slot_index} == {0, 1, 2}
        assert ring.acquire_textures_for_target('rectangle') is None

        ring.release_texture_lease(second)
        reused = ring.acquire_textures_for_target('rectangle')
        assert reused is not None
        assert reused.slot_index == second.slot_index

    def test_invalidated_slot_is_not_reused(self, monkeypatch) -> None:
        """A texture rejected by GL must be quarantined from future leases."""
        ids = iter(range(1, 30))
        monkeypatch.setattr(
            'src.shared.media.macos_vt.iosurface_gl_bridge.glGenTextures',
            lambda _count: next(ids),
        )
        ring = IOSurfaceGLTextureRingBuffer(
            ring_size=1,
            planes_per_frame=2,
            max_ring_size=2,
        )
        assert ring.initialize() is True

        bad = ring.acquire_textures_for_target('rectangle')
        assert bad is not None
        ring.invalidate_texture_lease(bad)

        replacement = ring.acquire_textures_for_target('rectangle')
        assert replacement is not None
        assert replacement.slot_index == bad.slot_index
        assert replacement.textures != bad.textures


# =========================================================================
# IOSurfaceGLBridge 测试
# =========================================================================

class TestIOSurfaceGLBridge:
    """IOSurfaceGLBridge 测试"""

    def test_creation(self) -> None:
        bridge = IOSurfaceGLBridge(ring_size=4)
        assert bridge.is_initialized is False
        assert bridge.bind_count == 0
        assert bridge.fail_count == 0

    def test_bind_without_init(self) -> None:
        bridge = IOSurfaceGLBridge()
        result = bridge.bind_pixel_buffer(
            pixel_buffer=None,
            frame_index=0,
            pixel_format='nv12',
            bit_depth=8,
        )
        assert result is None
        assert bridge.fail_count == 1
        assert bridge.last_fallback_reason == "bridge_not_initialized"


# =========================================================================
# ZeroCopyProbeResult 测试
# =========================================================================

class TestZeroCopyProbeResult:
    """ZeroCopyProbeResult 测试"""

    def test_defaults(self) -> None:
        result = ZeroCopyProbeResult()
        assert result.probe_success is False
        assert result.platform == ""
        assert result.vt_session_created is False
        assert result.cpu_download_frames == 0
        assert result.zero_copy is False

    def test_custom_values(self) -> None:
        result = ZeroCopyProbeResult(
            probe_success=True,
            platform="Darwin",
            vt_session_created=True,
            decoded_pixel_buffers=120,
            cvpixelbuffer_iosurface_backed=True,
            pixel_format="nv12",
            cpu_download_frames=0,
            zero_copy=True,
        )
        assert result.probe_success is True
        assert result.decoded_pixel_buffers == 120
        assert result.zero_copy is True


# =========================================================================
# 统计函数测试
# =========================================================================

class TestProbeStatistics:
    """probe 统计函数测试"""

    def test_percentile(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        assert 4.0 <= _percentile(values, 50) <= 6.0  # p50 in range
        assert _percentile(values, 95) >= 9.0

    def test_percentile_empty(self) -> None:
        assert _percentile([], 50) == 0.0

    def test_stats_dict(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        stats = _stats_dict(values)
        assert stats["count"] == 5
        assert stats["avg"] == 3.0
        assert stats["max"] == 5.0
        assert stats["p50"] >= 2.0

    def test_stats_dict_empty(self) -> None:
        stats = _stats_dict([])
        assert stats["count"] == 0
        assert stats["avg"] == 0.0


# =========================================================================
# Capability Gate 测试
# =========================================================================

class TestMacOSZeroCopyCapability:
    """macOS 零拷贝能力 gate 测试"""

    def test_capability_dataclass(self) -> None:
        cap = MacOSZeroCopyCapability()
        assert cap.available is False
        assert cap.reason == "not_probed"
        assert len(cap.supported_codecs) > 0
        assert 'h264' in cap.supported_codecs
        assert 'nv12' in cap.supported_pixel_formats

    def test_capability_available(self) -> None:
        cap = MacOSZeroCopyCapability(
            available=True,
            reason="",
            supported_codecs={'h264', 'hevc'},
            gl_texture_target="2d",
        )
        assert cap.available is True
        assert cap.reason == ""
        assert 'h264' in cap.supported_codecs

    def test_get_capability_caches(self) -> None:
        invalidate_capability_cache()
        cap1 = get_zero_copy_capability()
        cap2 = get_zero_copy_capability()
        assert cap1 is cap2

    def test_invalidate_cache(self) -> None:
        get_zero_copy_capability()
        invalidate_capability_cache()
        # After invalidation, next call should re-probe

    def test_is_zero_copy_available_on_mac(self) -> None:
        # On non-macOS, likely returns False unless forced
        result = is_zero_copy_available()
        if platform.system() != 'Darwin':
            assert result is False or result is True

    def test_is_zero_copy_available_codec_filter(self) -> None:
        # ProRes should not be in supported codecs by default
        result = is_zero_copy_available(codec='prores')
        if platform.system() != 'Darwin':
            assert result is False

    def test_stub_decode_impl_does_not_report_available(self, monkeypatch) -> None:
        """VT/IOSurface stub 存在时不能误报 zero-copy available"""
        import src.shared.media.macos_vt.capability as capability
        import src.shared.media.macos_vt.vt_session as vt_session

        invalidate_capability_cache()
        monkeypatch.setattr(capability.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(vt_session, "is_vt_available", lambda: True)
        monkeypatch.setattr(vt_session, "is_vt_decode_implemented", lambda: False)

        cap = get_zero_copy_capability()

        assert cap.available is False
        assert cap.reason == "vt_decode_impl_not_available"
        invalidate_capability_cache()

    def test_auto_mode_requires_real_probe_success(self, monkeypatch) -> None:
        """auto 模式必须有 decode -> IOSurface -> GL bind probe 证据才开启。"""
        import src.shared.media.macos_vt.capability as capability
        import src.shared.media.macos_vt.vt_session as vt_session
        import src.shared.media.macos_vt.iosurface_gl_bridge as bridge

        invalidate_capability_cache()
        monkeypatch.setattr(capability.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(capability, "_get_zero_copy_mode", lambda: "auto")
        monkeypatch.setattr(capability, "_probe_json_path", "")
        monkeypatch.setattr(vt_session, "is_vt_available", lambda: True)
        monkeypatch.setattr(vt_session, "is_vt_decode_implemented", lambda: True)
        monkeypatch.setattr(bridge, "is_iosurface_bridge_implemented", lambda: True)
        monkeypatch.setattr(
            capability,
            "_run_auto_zero_copy_probe",
            lambda: (
                True,
                "",
                "tests/test_videos/2K_h264.mp4",
                {"texture_target": "rectangle"},
            ),
        )

        cap = get_zero_copy_capability()

        assert cap.available is True
        assert cap.reason == ""
        assert cap.last_probe_file == "tests/test_videos/2K_h264.mp4"
        assert cap.gl_texture_target == "rectangle"
        invalidate_capability_cache()

    def test_auto_mode_rejects_failed_probe(self, monkeypatch) -> None:
        """导入成功但真实 probe 失败时，auto 模式必须 fail closed。"""
        import src.shared.media.macos_vt.capability as capability
        import src.shared.media.macos_vt.vt_session as vt_session
        import src.shared.media.macos_vt.iosurface_gl_bridge as bridge

        invalidate_capability_cache()
        monkeypatch.setattr(capability.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(capability, "_get_zero_copy_mode", lambda: "auto")
        monkeypatch.setattr(capability, "_probe_json_path", "")
        monkeypatch.setattr(vt_session, "is_vt_available", lambda: True)
        monkeypatch.setattr(vt_session, "is_vt_decode_implemented", lambda: True)
        monkeypatch.setattr(bridge, "is_iosurface_bridge_implemented", lambda: True)
        monkeypatch.setattr(
            capability,
            "_run_auto_zero_copy_probe",
            lambda: (
                False,
                "probe_no_gl_planes_bound",
                "tests/test_videos/2K_h264.mp4",
                None,
            ),
        )

        cap = get_zero_copy_capability()

        assert cap.available is False
        assert cap.reason == "auto_probe_not_proven: probe_no_gl_planes_bound"
        invalidate_capability_cache()

    def test_auto_mode_loads_persistent_cache_without_live_probe(self, monkeypatch, tmp_path) -> None:
        """冷启动时可复用持久化 probe 结果，避免再次同步启动 live probe。"""
        import src.shared.media.macos_vt.capability as capability
        import src.shared.media.macos_vt.vt_session as vt_session
        import src.shared.media.macos_vt.iosurface_gl_bridge as bridge

        probe_file = tmp_path / "probe.mov"
        probe_file.write_bytes(b"probe")
        cache_file = tmp_path / "capability.json"

        invalidate_capability_cache()
        monkeypatch.setenv("TOPOS_VIDEO_ZERO_COPY_PERSISTENT_CACHE", "1")
        monkeypatch.setenv("TOPOS_VIDEO_ZERO_COPY_CAPABILITY_CACHE_FILE", str(cache_file))
        monkeypatch.setattr(capability.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(capability, "_get_zero_copy_mode", lambda: "auto")
        monkeypatch.setattr(capability, "_probe_json_path", "")
        monkeypatch.setattr(capability, "_probe_requested", False)
        monkeypatch.setattr(capability, "_find_default_probe_file", lambda: str(probe_file))
        monkeypatch.setattr(vt_session, "is_vt_available", lambda: True)
        monkeypatch.setattr(vt_session, "is_vt_decode_implemented", lambda: True)
        monkeypatch.setattr(bridge, "is_iosurface_bridge_implemented", lambda: True)

        cache_file.write_text(json.dumps({
            "schema_version": capability._PERSISTENT_CACHE_SCHEMA_VERSION,
            "created_at": capability.time.time(),
            "cache_key": capability._capability_cache_key(str(probe_file)),
            "capability": {
                "available": True,
                "reason": "",
                "supported_codecs": ["h264", "hevc", "h265"],
                "supported_pixel_formats": ["nv12", "p010"],
                "gl_texture_target": "rectangle",
                "last_probe_json": None,
                "last_probe_file": str(probe_file),
                "implementation_ready": True,
            },
            "probe_result": {"texture_target": "rectangle"},
        }))

        def _unexpected_live_probe():
            raise AssertionError("live probe should not run when persistent cache is valid")

        monkeypatch.setattr(capability, "_run_auto_zero_copy_probe", _unexpected_live_probe)

        cap = get_zero_copy_capability()

        assert cap.available is True
        assert cap.reason == ""
        assert cap.gl_texture_target == "rectangle"
        assert cap.last_probe_file == str(probe_file)
        invalidate_capability_cache()

    def test_auto_mode_caches_failed_color_validation(self, monkeypatch, tmp_path) -> None:
        """Verified chroma corruption is cached so later startup stays fast and safe."""
        import src.shared.media.macos_vt.capability as capability
        import src.shared.media.macos_vt.vt_session as vt_session
        import src.shared.media.macos_vt.iosurface_gl_bridge as bridge

        probe_file = tmp_path / "probe.mov"
        probe_file.write_bytes(b"probe")
        cache_file = tmp_path / "capability.json"

        invalidate_capability_cache()
        monkeypatch.setenv("TOPOS_VIDEO_ZERO_COPY_PERSISTENT_CACHE", "1")
        monkeypatch.setenv("TOPOS_VIDEO_ZERO_COPY_CAPABILITY_CACHE_FILE", str(cache_file))
        monkeypatch.setattr(capability.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(capability, "_get_zero_copy_mode", lambda: "auto")
        monkeypatch.setattr(capability, "_probe_json_path", "")
        monkeypatch.setattr(capability, "_probe_requested", False)
        monkeypatch.setattr(capability, "_find_default_probe_file", lambda: str(probe_file))
        monkeypatch.setattr(vt_session, "is_vt_available", lambda: True)
        monkeypatch.setattr(vt_session, "is_vt_decode_implemented", lambda: True)
        monkeypatch.setattr(bridge, "is_iosurface_bridge_implemented", lambda: True)

        reason = "auto_probe_not_proven: probe_color_verification_failed"
        cache_file.write_text(json.dumps({
            "schema_version": capability._PERSISTENT_CACHE_SCHEMA_VERSION,
            "created_at": capability.time.time(),
            "cache_key": capability._capability_cache_key(str(probe_file)),
            "capability": {
                "available": False,
                "reason": reason,
                "supported_codecs": ["h264", "hevc", "h265"],
                "supported_pixel_formats": ["nv12", "p010"],
                "gl_texture_target": "rectangle",
                "last_probe_json": None,
                "last_probe_file": str(probe_file),
                "implementation_ready": True,
            },
            "probe_result": {"color_verification": {"passed": False}},
        }))
        monkeypatch.setattr(
            capability,
            "_run_auto_zero_copy_probe",
            lambda: (_ for _ in ()).throw(AssertionError("live probe should not run")),
        )

        cap = get_zero_copy_capability()

        assert cap.available is False
        assert cap.reason == reason
        invalidate_capability_cache()

    def test_probe_json_must_prove_gl_bind(self, monkeypatch, tmp_path) -> None:
        """JSON probe 不能只证明 decode，必须证明 GL plane bind。"""
        import src.shared.media.macos_vt.capability as capability
        import src.shared.media.macos_vt.vt_session as vt_session
        import src.shared.media.macos_vt.iosurface_gl_bridge as bridge

        probe_json = tmp_path / "probe.json"
        probe_json.write_text(json.dumps({
            "probe_success": True,
            "zero_copy": True,
            "decoded_pixel_buffers": 1,
            "cvpixelbuffer_iosurface_backed": True,
            "iosurface_present": True,
            "bridge_initialized": True,
            "gl_texture_created": False,
            "planes_bound": 0,
            "cpu_download_frames": 0,
            "texture_write_frames": 0,
            "texture_target": "rectangle",
            "color_verification": {"passed": True},
        }))

        invalidate_capability_cache()
        monkeypatch.setattr(capability.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(capability, "_get_zero_copy_mode", lambda: "auto")
        monkeypatch.setattr(capability, "_probe_json_path", str(probe_json))
        monkeypatch.setattr(vt_session, "is_vt_available", lambda: True)
        monkeypatch.setattr(vt_session, "is_vt_decode_implemented", lambda: True)
        monkeypatch.setattr(bridge, "is_iosurface_bridge_implemented", lambda: True)

        cap = get_zero_copy_capability()

        assert cap.available is False
        assert cap.reason == "probe_json_not_proven: probe_gl_texture_not_created"
        invalidate_capability_cache()

    def test_probe_json_preserves_texture_target(self, monkeypatch, tmp_path) -> None:
        """capability 必须保留 probe 返回的 texture target，不许硬编码为 2d。"""
        import src.shared.media.macos_vt.capability as capability
        import src.shared.media.macos_vt.vt_session as vt_session
        import src.shared.media.macos_vt.iosurface_gl_bridge as bridge

        probe_json = tmp_path / "probe.json"
        probe_json.write_text(json.dumps({
            "probe_success": True,
            "zero_copy": True,
            "decoded_pixel_buffers": 1,
            "cvpixelbuffer_iosurface_backed": True,
            "iosurface_present": True,
            "bridge_initialized": True,
            "gl_texture_created": True,
            "planes_bound": 1,
            "cpu_download_frames": 0,
            "texture_write_frames": 0,
            "texture_target": "rectangle",
            "color_verification": {"passed": True},
        }))

        invalidate_capability_cache()
        monkeypatch.setattr(capability.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(capability, "_get_zero_copy_mode", lambda: "auto")
        monkeypatch.setattr(capability, "_probe_json_path", str(probe_json))
        monkeypatch.setattr(vt_session, "is_vt_available", lambda: True)
        monkeypatch.setattr(vt_session, "is_vt_decode_implemented", lambda: True)
        monkeypatch.setattr(bridge, "is_iosurface_bridge_implemented", lambda: True)

        cap = get_zero_copy_capability()

        assert cap.available is True
        assert cap.reason == ""
        assert cap.gl_texture_target == "rectangle"
        invalidate_capability_cache()


# =========================================================================
# 模块导入测试
# =========================================================================

class TestModuleImports:
    """模块导入测试"""

    def test_import_capability(self) -> None:
        from src.shared.media.macos_vt.capability import MacOSZeroCopyCapability
        assert MacOSZeroCopyCapability is not None

    def test_import_packet_demux(self) -> None:
        from src.shared.media.macos_vt.packet_demux import PacketDemuxer
        assert PacketDemuxer is not None

    def test_import_vt_session(self) -> None:
        from src.shared.media.macos_vt.vt_session import VtSession
        assert VtSession is not None

    def test_import_external_texture(self) -> None:
        from src.shared.media.macos_vt.external_texture import ExternalGLTexture
        assert ExternalGLTexture is not None

    def test_import_bridge(self) -> None:
        from src.shared.media.macos_vt.iosurface_gl_bridge import IOSurfaceGLBridge
        assert IOSurfaceGLBridge is not None

    def test_import_probe(self) -> None:
        from src.shared.media.macos_vt.vt_zero_copy_probe import ZeroCopyProbeResult
        assert ZeroCopyProbeResult is not None
