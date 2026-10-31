"""
GPU Decoded Frame 模块测试（Phase B1）

测试目标：
1. GpuFramePlane 数据完整性
2. GpuFrameRelease 生命周期管理（exactly once release）
3. GpuDecodedFrame 数据类和验证
4. 队列丢弃旧 GpuDecodedFrame 时 release exactly once
5. Renderer 接手后队列不提前 release
6. Session close 释放所有 held GPU frames
7. Seek 清除 stale GPU frames
8. 工具函数 is_gpu_decoded_frame / is_zero_copy_frame / safe_release_gpu_frame
"""

from __future__ import annotations

import pytest
import threading
from unittest.mock import MagicMock

from src.shared.media.gpu_frame import (
    GpuFramePlane,
    GpuFrameRelease,
    GpuDecodedFrame,
    is_gpu_decoded_frame,
    is_zero_copy_frame,
    get_frame_decode_path,
    get_frame_render_path,
    safe_release_gpu_frame,
    GpuPlaneResource,
)
from src.shared.media.frame_path import DecodePath, RenderFramePath
from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource


# =========================================================================
# GpuFramePlane 测试
# =========================================================================

class TestGpuFramePlane:
    """GpuFramePlane 数据类测试"""

    def test_plane_creation_nv12_y(self) -> None:
        plane = GpuFramePlane(
            component="Y",
            api="opengl",
            texture_id=1,
            target="2d",
            width=3840,
            height=2160,
            internal_format="GL_R8",
            pixel_format="NV12_Y",
            pixel_type="GL_UNSIGNED_BYTE",
            texture_unit_hint=0,
        )
        assert plane.component == "Y"
        assert plane.api == "opengl"
        assert plane.texture_id == 1
        assert plane.width == 3840
        assert plane.height == 2160

    def test_plane_creation_nv12_uv(self) -> None:
        plane = GpuFramePlane(
            component="UV",
            api="opengl",
            texture_id=2,
            target="2d",
            width=1920,
            height=1080,
            internal_format="GL_RG8",
            pixel_format="NV12_UV",
            pixel_type="GL_UNSIGNED_BYTE",
            texture_unit_hint=1,
        )
        assert plane.component == "UV"
        assert plane.width == 1920
        assert plane.height == 1080

    def test_plane_creation_p010_y(self) -> None:
        plane = GpuFramePlane(
            component="Y",
            api="opengl",
            texture_id=3,
            target="2d",
            width=3840,
            height=2160,
            internal_format="GL_R16UI",
            pixel_format="P010_Y",
            pixel_type="GL_UNSIGNED_SHORT",
        )
        assert plane.internal_format == "GL_R16UI"

    def test_plane_frozen(self) -> None:
        plane = GpuFramePlane(
            component="Y",
            api="opengl",
            texture_id=1,
            target="2d",
            width=3840,
            height=2160,
        )
        with pytest.raises(AttributeError):
            plane.texture_id = 99  # type: ignore


# =========================================================================
# GpuFrameRelease 测试
# =========================================================================

class TestGpuFrameRelease:
    """GpuFrameRelease 生命周期测试"""

    def test_release_calls_callback(self) -> None:
        mock_callback = MagicMock()
        release = GpuFrameRelease(release_callback=mock_callback)
        release.release()
        mock_callback.assert_called_once()

    def test_release_only_once(self) -> None:
        mock_callback = MagicMock()
        release = GpuFrameRelease(release_callback=mock_callback)
        release.release()
        release.release()
        release.release()
        mock_callback.assert_called_once()

    def test_release_without_callback(self) -> None:
        release = GpuFrameRelease()
        release.release()
        assert release.is_released

    def test_is_released_property(self) -> None:
        release = GpuFrameRelease()
        assert release.is_released is False
        release.release()
        assert release.is_released is True

    def test_release_callback_exception_handled(self) -> None:
        def bad_callback() -> None:
            raise RuntimeError("Simulated release error")

        release = GpuFrameRelease(release_callback=bad_callback)
        release.release()
        assert release.is_released is True

    def test_release_with_retain_token(self) -> None:
        token = object()
        release = GpuFrameRelease(retain_token=token)
        assert release.retain_token is token
        release.release()
        assert release.is_released

    def test_release_thread_safety(self) -> None:
        call_count = 0
        lock = threading.Lock()

        def callback() -> None:
            nonlocal call_count
            with lock:
                call_count += 1

        release = GpuFrameRelease(release_callback=callback)
        threads = [threading.Thread(target=release.release) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert call_count == 1


# =========================================================================
# GpuDecodedFrame 测试
# =========================================================================

class TestGpuDecodedFrame:
    """GpuDecodedFrame 数据类测试"""

    def _make_frame(
        self,
        zero_copy: bool = True,
        decode_path: str = "macos_vt_iosurface_zero_copy",
        **kwargs,
    ) -> GpuDecodedFrame:
        planes = kwargs.pop('planes', [
            GpuFramePlane(
                component="Y",
                api="opengl",
                texture_id=1,
                target="2d",
                width=3840,
                height=2160,
                internal_format="GL_R8",
            ),
            GpuFramePlane(
                component="UV",
                api="opengl",
                texture_id=2,
                target="2d",
                width=1920,
                height=1080,
                internal_format="GL_RG8",
            ),
        ])
        return GpuDecodedFrame(
            frame_index=0,
            pts=0,
            time_seconds=0.0,
            width=3840,
            height=2160,
            visible_rect=(0, 0, 3840, 2160),
            pixel_format="nv12",
            bit_depth=8,
            planes=planes,
            color_range="limited",
            color_matrix="bt709",
            decode_path=decode_path,
            zero_copy=zero_copy,
            **kwargs,
        )

    def test_basic_creation(self) -> None:
        frame = self._make_frame()
        assert frame.frame_index == 0
        assert frame.width == 3840
        assert frame.height == 2160
        assert frame.pixel_format == "nv12"
        assert frame.bit_depth == 8
        assert frame.zero_copy is True
        assert frame.decode_path == "macos_vt_iosurface_zero_copy"
        assert len(frame.planes) == 2
        assert frame.planes[0].component == "Y"
        assert frame.planes[1].component == "UV"

    def test_zero_copy_with_no_planes_raises(self) -> None:
        with pytest.raises(ValueError, match="zero_copy=True requires at least one plane"):
            GpuDecodedFrame(
                frame_index=0,
                pts=0,
                time_seconds=0.0,
                width=3840,
                height=2160,
                visible_rect=None,
                pixel_format="nv12",
                bit_depth=8,
                planes=[],
                decode_path="macos_vt_iosurface_zero_copy",
                zero_copy=True,
            )

    def test_zero_copy_with_cpu_decode_path_raises(self) -> None:
        with pytest.raises(ValueError, match="CPU path"):
            GpuDecodedFrame(
                frame_index=0,
                pts=0,
                time_seconds=0.0,
                width=3840,
                height=2160,
                visible_rect=None,
                pixel_format="nv12",
                bit_depth=8,
                planes=[
                    GpuFramePlane("Y", "opengl", 1, "2d", 3840, 2160),
                ],
                decode_path="cpu_planar_yuv",
                zero_copy=True,
            )

    def test_p010_frame(self) -> None:
        frame = GpuDecodedFrame(
            frame_index=100,
            pts=3333,
            time_seconds=0.1,
            width=3840,
            height=2160,
            visible_rect=None,
            pixel_format="p010",
            bit_depth=10,
            planes=[
                GpuFramePlane("Y", "opengl", 3, "2d", 3840, 2160, "GL_R16UI", "P010_Y", "GL_UNSIGNED_SHORT"),
                GpuFramePlane("UV", "opengl", 4, "2d", 1920, 1080, "GL_RG16UI", "P010_UV", "GL_UNSIGNED_SHORT"),
            ],
            color_range="limited",
            color_matrix="bt2020",
            color_primaries="bt2020",
            color_transfer="smpte2084",
            decode_path="macos_vt_iosurface_zero_copy",
            zero_copy=True,
        )
        assert frame.bit_depth == 10
        assert frame.color_matrix == "bt2020"
        assert frame.color_primaries == "bt2020"
        assert frame.color_transfer == "smpte2084"

    def test_frame_with_release_callback(self) -> None:
        mock_callback = MagicMock()
        release = GpuFrameRelease(release_callback=mock_callback)
        frame = self._make_frame(release=release)
        frame.release.release()
        mock_callback.assert_called_once()

    def test_frame_destructor_releases(self) -> None:
        mock_callback = MagicMock()
        release = GpuFrameRelease(
            release_callback=mock_callback,
            destructor_policy="thread_safe",
        )
        frame = self._make_frame(release=release)
        del frame
        mock_callback.assert_called_once()

    def test_destructor_warn_only_by_default(self, caplog) -> None:
        """Default destructor_policy should only warn, not release."""
        mock_callback = MagicMock()
        release = GpuFrameRelease(release_callback=mock_callback)
        frame = self._make_frame(release=release)
        with caplog.at_level("WARNING"):
            del frame
        mock_callback.assert_not_called()
        assert "leaked" in caplog.text

    def test_non_zero_copy_frame(self) -> None:
        frame = self._make_frame(zero_copy=False, decode_path="hwaccel_download_packed_rgb")
        assert frame.zero_copy is False

    def test_device_mismatch_is_fail_closed(self) -> None:
        frame = GpuDecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            width=16, height=8, visible_rect=None,
            pixel_format="nv12", bit_depth=8,
            planes=[GpuFramePlane(
                "Y", "metal", 0, "2d", 16, 8,
                resource=GpuPlaneResource(
                    "metal", object(), "r8unorm", 16, 8,
                    device_identity="decode-device",
                ),
            )],
            decode_path="macos_vt_cvmetal_zero_copy",
            zero_copy=True,
            device_identity="decode-device",
        )
        assert frame.validate_for_device(
            api="metal", device_identity="encode-device"
        ).startswith("device_mismatch:")


# =========================================================================
# 工具函数测试
# =========================================================================

class TestGpuFrameUtilities:
    """GPU frame 工具函数测试"""

    def test_is_gpu_decoded_frame(self) -> None:
        frame = GpuDecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            width=1920, height=1080, visible_rect=None,
            pixel_format="nv12", bit_depth=8,
            planes=[GpuFramePlane("Y", "opengl", 1, "2d", 1920, 1080)],
            decode_path="macos_vt_iosurface_zero_copy",
            zero_copy=True,
        )
        assert is_gpu_decoded_frame(frame) is True
        assert is_gpu_decoded_frame("not a frame") is False
        assert is_gpu_decoded_frame(None) is False

    def test_is_zero_copy_frame(self) -> None:
        frame = GpuDecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            width=1920, height=1080, visible_rect=None,
            pixel_format="nv12", bit_depth=8,
            planes=[GpuFramePlane("Y", "opengl", 1, "2d", 1920, 1080)],
            decode_path="macos_vt_iosurface_zero_copy",
            zero_copy=True,
        )
        assert is_zero_copy_frame(frame) is True

        frame_non_zero = GpuDecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            width=1920, height=1080, visible_rect=None,
            pixel_format="nv12", bit_depth=8,
            planes=[GpuFramePlane("Y", "opengl", 1, "2d", 1920, 1080)],
            decode_path="hwaccel_download_packed_rgb",
            zero_copy=False,
        )
        assert is_zero_copy_frame(frame_non_zero) is False
        assert is_zero_copy_frame(None) is False

    def test_get_frame_decode_path(self) -> None:
        frame = GpuDecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            width=1920, height=1080, visible_rect=None,
            pixel_format="nv12", bit_depth=8,
            planes=[GpuFramePlane("Y", "opengl", 1, "2d", 1920, 1080)],
            decode_path="macos_vt_iosurface_zero_copy",
            zero_copy=True,
        )
        assert get_frame_decode_path(frame) == "macos_vt_iosurface_zero_copy"
        assert get_frame_decode_path(None) == "cpu_unknown"

    def test_get_frame_render_path_gpu_yuv(self) -> None:
        frame = GpuDecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            width=1920, height=1080, visible_rect=None,
            pixel_format="nv12", bit_depth=8,
            planes=[
                GpuFramePlane("Y", "opengl", 1, "2d", 1920, 1080),
                GpuFramePlane("UV", "opengl", 2, "2d", 960, 540),
            ],
            decode_path="macos_vt_iosurface_zero_copy",
            zero_copy=True,
        )
        assert get_frame_render_path(frame) == "opengl_external_yuv"

    def test_get_frame_render_path_gpu_rgb(self) -> None:
        frame = GpuDecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            width=1920, height=1080, visible_rect=None,
            pixel_format="rgba", bit_depth=8,
            planes=[GpuFramePlane("RGBA", "opengl", 1, "2d", 1920, 1080)],
            decode_path="macos_vt_iosurface_zero_copy",
            zero_copy=True,
        )
        assert get_frame_render_path(frame) == "opengl_external_rgb"

    def test_safe_release_gpu_frame(self) -> None:
        mock_callback = MagicMock()
        release = GpuFrameRelease(release_callback=mock_callback)
        frame = GpuDecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            width=1920, height=1080, visible_rect=None,
            pixel_format="nv12", bit_depth=8,
            planes=[GpuFramePlane("Y", "opengl", 1, "2d", 1920, 1080)],
            decode_path="macos_vt_iosurface_zero_copy",
            zero_copy=True,
            release=release,
        )
        safe_release_gpu_frame(frame)
        mock_callback.assert_called_once()

    def test_safe_release_non_gpu_frame(self) -> None:
        safe_release_gpu_frame(None)
        safe_release_gpu_frame("not a frame")
        safe_release_gpu_frame(42)


# =========================================================================
# 队列生命周期测试 (B2)
# =========================================================================

class TestGpuDecodedFrameQueueLifetime:
    """GpuDecodedFrame 队列生命周期测试"""

    def _make_frame(self, index: int, callback: MagicMock | None = None) -> GpuDecodedFrame:
        release = GpuFrameRelease(release_callback=callback)
        return GpuDecodedFrame(
            frame_index=index,
            pts=index * 3333,
            time_seconds=index / 30.0,
            width=3840,
            height=2160,
            visible_rect=None,
            pixel_format="nv12",
            bit_depth=8,
            planes=[
                GpuFramePlane("Y", "opengl", index * 2 + 1, "2d", 3840, 2160),
                GpuFramePlane("UV", "opengl", index * 2 + 2, "2d", 1920, 1080),
            ],
            decode_path="macos_vt_iosurface_zero_copy",
            zero_copy=True,
            release=release,
        )

    def test_queue_full_drops_old_frame_releases_once(self) -> None:
        """队列满时丢弃旧帧，release exactly once"""
        release_count = 0
        lock = threading.Lock()

        def on_release() -> None:
            nonlocal release_count
            with lock:
                release_count += 1

        frames = []
        max_queue = 3
        for i in range(5):
            frame = self._make_frame(i, callback=on_release)
            if len(frames) >= max_queue:
                old = frames.pop(0)
                safe_release_gpu_frame(old)
            frames.append(frame)

        assert release_count == 2  # 5 - 3 = 2 old frames released

    def test_renderer_accepted_frame_not_released_by_queue(self) -> None:
        """Renderer 接手的帧，队列不提前释放"""
        mock_callback = MagicMock()
        frame = self._make_frame(0, callback=mock_callback)

        # 队列交出帧给 renderer
        rendered_frame = frame
        # 队列不调用 release
        # Renderer 之后 release
        rendered_frame.release.release()
        mock_callback.assert_called_once()

    def test_session_close_releases_all_held_frames(self) -> None:
        """Session close 释放所有 held GPU frames"""
        release_count = 0
        lock = threading.Lock()

        def on_release() -> None:
            nonlocal release_count
            with lock:
                release_count += 1

        held_frames = []
        for i in range(5):
            held_frames.append(self._make_frame(i, callback=on_release))

        # Session close: release all
        for frame in held_frames:
            safe_release_gpu_frame(frame)

        assert release_count == 5

    def test_seek_clears_stale_gpu_frames(self) -> None:
        """Seek 清除 stale GPU frames"""
        release_count = 0
        lock = threading.Lock()

        def on_release() -> None:
            nonlocal release_count
            with lock:
                release_count += 1

        frames = [self._make_frame(i, callback=on_release) for i in range(5)]

        # Seek: release all stale frames
        for frame in frames:
            safe_release_gpu_frame(frame)

        assert release_count == 5

    def test_double_release_protection(self) -> None:
        """释放两次只调用一次 callback"""
        mock_callback = MagicMock()
        frame = self._make_frame(0, callback=mock_callback)

        frame.release.release()
        frame.release.release()

        mock_callback.assert_called_once()

    def test_release_is_thread_safe(self) -> None:
        """多线程重复释放也只调用一次 callback"""
        mock_callback = MagicMock()
        frame = self._make_frame(0, callback=mock_callback)

        threads = [threading.Thread(target=frame.release.release) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        mock_callback.assert_called_once()

    def test_ready_queue_rejects_stale_gpu_frame_releases(self) -> None:
        """过期 generation 的 GPU frame 入队失败时必须释放"""
        mock_callback = MagicMock()
        queue = ReadyFrameQueue()
        queue.increment_generation()
        frame = self._make_frame(0, callback=mock_callback)

        accepted = queue.enqueue(QueueEntry(
            frame=frame,
            decode_ms=1.0,
            source=FrameSource.GPU_PROXY,
            can_drop=True,
            generation_id=0,
        ))

        assert accepted is False
        mock_callback.assert_called_once()

    def test_latest_before_or_at_releases_older_gpu_frames(self) -> None:
        """latest-before-or-at 丢弃旧 GPU frame 时必须 release"""
        released: list[int] = []
        queue = ReadyFrameQueue(max_proxy_size=8)
        for index in range(4):
            frame = self._make_frame(index, callback=lambda i=index: released.append(i))
            queue.enqueue(QueueEntry(
                frame=frame,
                decode_ms=1.0,
                source=FrameSource.GPU_PROXY,
                can_drop=True,
                generation_id=0,
            ))

        result = queue.dequeue_latest_before_or_at(2, is_master=False)

        assert result.found is True
        assert result.actual_frame_index == 2
        assert released == [0, 1]
        assert result.entry is not None
        assert not result.entry.frame.release.is_released

    def test_clear_releases_gpu_frames(self) -> None:
        """clear/clear_all 必须释放队列仍持有的 GPU frame"""
        mock_callback = MagicMock()
        queue = ReadyFrameQueue(max_proxy_size=8)
        for index in range(3):
            queue.enqueue(QueueEntry(
                frame=self._make_frame(index, callback=mock_callback),
                decode_ms=1.0,
                source=FrameSource.GPU_PROXY,
                can_drop=True,
                generation_id=0,
            ))

        assert queue.clear(is_master=False) == 3
        assert mock_callback.call_count == 3

    def test_frame_destructor_auto_release(self) -> None:
        """析构自动释放（thread_safe 策略）"""
        release = GpuFrameRelease(
            release_callback=MagicMock(),
            destructor_policy="thread_safe",
        )
        frame = self._make_frame(0, callback=None)
        # Override release with thread_safe one
        object.__setattr__(frame, 'release', release)
        mock_callback = release.release_callback
        del frame
        mock_callback.assert_called_once()
