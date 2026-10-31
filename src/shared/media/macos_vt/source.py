"""
macOS VideoToolbox Zero-Copy Source
MediaSource 适配器，将 VTDecodeSession 的 GpuDecodedFrame 输出接入播放链路。

## 职责

- 实现 VideoSource 协议，输出 GpuDecodedFrame
- MacOSVtZeroCopySource.read_next_frame() -> GpuDecodedFrame
- capability gate 控制：仅在 zero_copy 可用时启用
- 失败时返回明确 fallback reason

## 验收

- GpuDecodedFrame.zero_copy == True
- GpuDecodedFrame.decode_path == "macos_vt_iosurface_zero_copy"
- 不调用 frame.to_ndarray() / frame.transfer()
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

import numpy as np

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from .vt_session import VtDecodedPixelBuffer

from ...media.gpu_frame import (
    GpuDecodedFrame,
    GpuFrameRelease,
    normalize_color_matrix,
    normalize_color_range,
)
from ...media.frame import DecodedFrame, PlaneInfo
from ...media.frame_path import DecodePath
from ...media.metadata import VideoMetadata, FPS_24, Rational

logger = logging.getLogger(__name__)


def metal_video_preview_enabled() -> bool:
    """P5-08 开发者开关（默认关）：Metal 视频零拷贝预览。

    默认播放路由完全不受影响（playback_should_prefer_ffmpeg 照常）；
    本开关只放行 benchmark 与开发者预览的 Metal 垂直链路。
    """
    import os

    return os.environ.get("TOPOS_METAL_VIDEO_PREVIEW", "").strip().lower() in (
        "1", "true", "yes",
    )


def metal_zero_copy_gate(
    codec: str,
    bit_depth: int,
    width: int = 0,
    height: int = 0,
    path: str = "",
) -> tuple[bool, str]:
    """Metal 零拷贝可用性门禁（P5-08）：可用返回 (True, '')，否则
    (False, 显式原因)。逐条诊断，绝不静默降级（.clinerules:11）。

    条件（plan §5 Phase 5）：
    1. 开发者开关开启；
    2. Metal 设备 + CVMetal interop 可用；
    3. 像素格式在支持矩阵（nv12/p010，4:2:0 双平面）内；
    4. **VT 真有硬解**（plan §0.1：无硬解即无零拷贝源头；本机 ProRes
       软解 → 显式拒绝，不伪装）。
    """
    if not metal_video_preview_enabled():
        return False, "metal_video_preview_disabled(TOPOS_METAL_VIDEO_PREVIEW)"
    from .cv_metal_bridge import _PLANE_LAYOUTS, cv_metal_bridge_available

    ok, reason = cv_metal_bridge_available()
    if not ok:
        return False, f"metal_unavailable:{reason}"
    pixel_format = "nv12" if int(bit_depth) <= 8 else "p010"
    if pixel_format not in _PLANE_LAYOUTS:
        return False, f"unsupported_pixel_format:{pixel_format}"
    from .hardware_capability import vt_hardware_decode_available

    verdict = vt_hardware_decode_available(path, codec, bit_depth, width, height)
    if verdict is not True:
        return False, (
            f"no_vt_hardware_decode(codec={codec},bit_depth={bit_depth},"
            f"verdict={verdict})"
        )
    return True, ""


@dataclass
class VtPixelBufferFrame:
    """
    Unbound VT/CVPixelBuffer frame for renderer-thread GL import.

    Decode threads may produce this object without touching OpenGL.  The edit
    GPU renderer imports the pixel_buffer into IOSurface-backed GL textures in
    its own current GL context, then calls release exactly once after the GPU
    has finished sampling it.
    """

    source: "MacOSVtZeroCopySource"
    decoded: object
    frame_index: int
    pts: int | None
    time_seconds: float
    width: int
    height: int
    pixel_format: str
    bit_depth: int
    color_range: str = "limited"
    color_matrix: str = "bt709"
    color_primaries: object | None = None
    color_transfer: object | None = None
    decode_path: str = DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY.value
    _released: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @property
    def pixel_buffer(self) -> object | None:
        return getattr(self.decoded, "pixel_buffer", None)

    def release(self) -> None:
        """Release the retained CVPixelBuffer exactly once."""
        with self._lock:
            if self._released:
                return
            self._released = True
        self.source.release_pixel_buffer_frame(self)


class MacOSVtZeroCopySource:
    """
    macOS VideoToolbox 零拷贝媒体源。

    将 VTDecompressionSession 的 GpuDecodedFrame 输出接入播放链路。
    仅在 zero_copy 能力可用时启用。

    Usage:
        source = MacOSVtZeroCopySource()
        if source.open('/path/to/4k_h264.mp4'):
            frame = source.read_next_frame()  # GpuDecodedFrame
            if is_gpu_decoded_frame(frame):
                # 直接提交到 renderer
                pass
        source.close()
    """

    # reorder 缓冲淘汰参数：±2 对齐 deadline 邻近兜底窗口；
    # 正常 B 帧重排窗口 ≤ ~8 帧，16 是异常 GOP 结构的硬上限兜底。
    _REORDER_KEEP_BEHIND = 2
    _REORDER_BUFFER_MAX = 16

    def __init__(self) -> None:
        self._demuxer = None
        self._vt_session = None
        self._bridge = None
        self._metal_bridge = None
        self._info: VideoMetadata | None = None
        self._current_frame: int = 0
        self._is_open: bool = False
        self._fallback_reason: str | None = None
        self._zero_copy_available: bool = False
        self._time_base_num: int = 1
        self._time_base_den: int = 30
        self._reorder_buffer: dict[int, VtPixelBufferFrame] = {}
        # 流级色彩标记回退值（VT 解码帧本身不携带色彩元数据）。
        # 默认与调色页一致：bt709 / limited；open() 时按容器标记覆盖。
        self._stream_color_matrix: str = "bt709"
        self._stream_color_range: str = "limited"

    def open(self, path: str, *, require_zero_copy: bool = True, require_hardware: bool = False) -> bool:
        """
        打开视频文件并检测零拷贝能力。

        Args:
            path: 视频文件路径
            require_zero_copy: 要求 GL 零拷贝管线可用
            require_hardware: True 时会话以 hardware-required 规格创建 ——
                硬解实例被占（VideoToolbox 会静默落软解）时直接 open 失败，
                让调用方回退到 FFmpeg，而不是接受一个终身软解的会话。

        Returns:
            True 如果零拷贝路径可用，False 进入 fallback
        """
        from .packet_demux import PacketDemuxer
        from .vt_session import VtSession, is_vt_available

        self._fallback_reason = None

        cap = None
        if require_zero_copy:
            from .capability import get_zero_copy_capability

            cap = get_zero_copy_capability()
            if not cap.available:
                self._fallback_reason = cap.reason or "zero_copy_unavailable"
                logger.info(f"MacOSVtZeroCopySource: zero-copy unavailable, reason={self._fallback_reason}")
                return False
        elif not is_vt_available():
            self._fallback_reason = "vt_decode_unavailable"
            logger.info("MacOSVtZeroCopySource: VT decode unavailable")
            return False

        try:
            self._demuxer = PacketDemuxer()
            info = self._demuxer.open(path)
        except Exception as e:
            self._fallback_reason = f"demux_error: {e}"
            logger.error(f"MacOSVtZeroCopySource: demux failed: {e}")
            return False

        codec = info.codec_name.lower()
        supported_codecs = cap.supported_codecs if cap is not None else {'h264', 'hevc', 'h265', 'prores'}
        if codec not in supported_codecs:
            self._fallback_reason = f"unsupported_codec: {codec}"
            logger.info(f"MacOSVtZeroCopySource: codec '{codec}' not in supported set {supported_codecs}")
            self._demuxer.close()
            self._demuxer = None
            return False

        self._vt_session = VtSession()
        pixel_format = 'nv12' if info.bit_depth <= 8 else 'p010'
        # ProRes 的格式描述来自容器 fourcc（无 extradata）。
        codec_tag = getattr(info, 'codec_tag', '') or ''
        created = self._vt_session.create(
            width=info.width,
            height=info.height,
            codec=codec,
            pixel_format=pixel_format,
            extradata=info.extradata,
            require_hw_accel=require_hardware,
            codec_tag=codec_tag,
        )

        if not created:
            self._fallback_reason = (
                "vt_hardware_session_unavailable"
                if require_hardware
                else "vt_session_creation_failed"
            )
            logger.error(
                "MacOSVtZeroCopySource: VT session creation failed "
                f"(codec={codec}, tag={codec_tag})"
            )
            self._demuxer.close()
            self._demuxer = None
            return False

        self._info = VideoMetadata(
            width=info.width,
            height=info.height,
            fps=Rational(int(info.fps * 1000), 1000) if info.fps > 0 else FPS_24,
            total_frames=max(info.total_frames, 0),
            codec_name=codec,
            codec_profile='',
            pixel_format=pixel_format,
            bit_depth=info.bit_depth,
        )
        self._time_base_num = int(getattr(info, "time_base_num", 1) or 1)
        self._time_base_den = int(getattr(info, "time_base_den", 30) or 30)
        if self._time_base_den <= 0:
            self._time_base_den = 30

        # 从容器流标记提取色彩元数据。不提取时所有帧都会默认 bt709/
        # limited，BT.601/full-range 素材会被错误矩阵/范围解码，
        # 表现为肤色色偏（与调色页读流元数据的行为对齐）。
        stream_color_space = str(getattr(info, "color_space", "") or "")
        stream_color_range = str(getattr(info, "color_range", "") or "")
        self._stream_color_matrix = (
            normalize_color_matrix(stream_color_space) if stream_color_space else "bt709"
        )
        self._stream_color_range = (
            normalize_color_range(stream_color_range) if stream_color_range else "limited"
        )

        self._is_open = True
        self._zero_copy_available = require_zero_copy
        self._current_frame = 0

        logger.info(
            f"MacOSVtZeroCopySource: opened {path}, "
            f"codec={codec}, {info.width}x{info.height}, "
            f"zero_copy={require_zero_copy}"
        )
        return True

    def close(self) -> None:
        """关闭源"""
        self._clear_reorder_buffer()
        if self._vt_session is not None:
            self._vt_session.destroy()
            self._vt_session = None
        if self._demuxer is not None:
            self._demuxer.close()
            self._demuxer = None
        if self._bridge is not None:
            self._bridge.destroy()
            self._bridge = None
        if self._metal_bridge is not None:
            self._metal_bridge.destroy()
            self._metal_bridge = None
        self._is_open = False
        self._info = None
        self._time_base_num = 1
        self._time_base_den = 30
        logger.info("MacOSVtZeroCopySource: closed")

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def path(self) -> str:
        return ""

    @property
    def current_frame(self) -> int:
        return self._current_frame

    @property
    def metadata(self) -> VideoMetadata | None:
        return self._info

    @property
    def fallback_reason(self) -> str | None:
        return self._fallback_reason

    @property
    def zero_copy_available(self) -> bool:
        return self._zero_copy_available

    def _pts_to_frame(self, pts: int | None) -> int | None:
        """Convert a stream PTS to a best-effort frame index."""
        if pts is None or self._info is None:
            return None

        raw_fps = getattr(self._info, "fps", 30.0)
        try:
            fps = float(raw_fps)
        except Exception:
            num = getattr(raw_fps, "num", None)
            den = getattr(raw_fps, "den", None)
            if num is None:
                num = getattr(raw_fps, "numerator", None)
            if den is None:
                den = getattr(raw_fps, "denominator", None)
            try:
                fps = float(num) / float(den) if den else 30.0
            except Exception:
                fps = 30.0
        if fps <= 0:
            fps = 30.0
        seconds = pts * self._time_base_num / self._time_base_den
        return max(0, int(round(seconds * fps)))

    def _pts_to_seconds(self, pts: int | None) -> float:
        """Convert stream PTS to seconds using the demuxer time base."""
        if pts is None:
            return 0.0
        return float(pts) * float(self._time_base_num) / float(self._time_base_den)

    def _clear_reorder_buffer(self) -> None:
        """Release decoded frames waiting for presentation-order output."""
        if not self._reorder_buffer:
            return
        pending = list(self._reorder_buffer.values())
        self._reorder_buffer.clear()
        for frame in pending:
            try:
                frame.release()
            except Exception:
                pass

    def _buffer_decoded_frame(self, frame: VtPixelBufferFrame, target_frame: int) -> None:
        """Store a decoded frame by presentation frame index."""
        if frame.frame_index < target_frame:
            frame.release()
            return
        existing = self._reorder_buffer.get(frame.frame_index)
        if existing is not None:
            frame.release()
            return
        self._reorder_buffer[frame.frame_index] = frame
        self._prune_reorder_buffer(target_frame)

    def _prune_reorder_buffer(self, target_frame: int) -> None:
        """Evict reorder entries that can never be popped again.

        Entries leave this buffer only via exact frame hit or the ±2
        nearest deadline fallback; anything below ``target_frame - 2`` is
        dead for the current walk and for every later forward walk, since
        a backward jump takes the seek path which clears the whole buffer.
        Without eviction, mid-speed forward drags that never seek strand
        one 4K CVPixelBuffer (~12MB IOSurface, kernel-accounted) per
        aborted walk until the next seek — that accumulation is what made
        scrubbing degrade the longer a clip was used. A hard cap backstops
        pathological GOP structures.
        """
        floor = target_frame - self._REORDER_KEEP_BEHIND
        if floor > 0:
            for idx in [i for i in self._reorder_buffer if i < floor]:
                try:
                    self._reorder_buffer.pop(idx).release()
                except Exception:
                    pass
        while len(self._reorder_buffer) > self._REORDER_BUFFER_MAX:
            oldest = min(self._reorder_buffer)
            try:
                self._reorder_buffer.pop(oldest).release()
            except Exception:
                pass

    def _pop_buffered_frame(self, frame_index: int) -> VtPixelBufferFrame | None:
        frame = self._reorder_buffer.pop(frame_index, None)
        if frame is not None:
            self._current_frame = max(self._current_frame, frame.frame_index + 1)
        return frame

    def _pop_nearest_buffered_frame(
        self,
        target_frame: int,
        *,
        max_distance: int | None = None,
    ) -> VtPixelBufferFrame | None:
        """Return the nearest decoded frame at or after target when exact is unavailable."""
        if max_distance is not None:
            candidates = [
                idx for idx in self._reorder_buffer
                if abs(int(idx) - int(target_frame)) <= int(max_distance)
            ]
        else:
            candidates = [idx for idx in self._reorder_buffer if idx >= target_frame]
        if not candidates:
            return None
        nearest = min(candidates, key=lambda idx: (abs(int(idx) - int(target_frame)), idx < target_frame, idx))
        frame = self._reorder_buffer.pop(nearest)
        self._current_frame = max(self._current_frame, frame.frame_index + 1)
        logger.debug(
            "MacOSVtZeroCopySource: exact frame unavailable, returning nearest "
            f"target={target_frame}, got={frame.frame_index}"
        )
        return frame

    def _decode_until_present_frame(
        self,
        target_frame: int,
        *,
        max_decode_frames: int = 500,
        allow_nearest: bool = True,
        is_stale: Callable[[], bool] | None = None,
        deadline_at: float | None = None,
        deadline_nearest_max_distance: int | None = None,
    ) -> VtPixelBufferFrame | None:
        """
        Decode compressed packets into a presentation-order reorder buffer.

        H.264/H.265 files with B-frames arrive from the demuxer/VT path in decode
        order, for example 0, 4, 2, 1, 3. Timeline playback must output
        presentation order, so callers ask for a target frame and this method
        buffers future frames until the target is available.
        """
        exact = self._pop_buffered_frame(target_frame)
        if exact is not None:
            return exact

        # 顺序追帧前先清掉不可能再被命中的残留条目（deadline 中止留下的
        # 近邻候选帧等），避免不 seek 的持续向前拖拽无限滞留。
        self._prune_reorder_buffer(target_frame)

        decoded_count = 0
        while decoded_count < max_decode_frames:
            if is_stale is not None and is_stale():
                logger.debug(
                    "MacOSVtZeroCopySource: interactive decode stale "
                    f"target={target_frame}, decoded={decoded_count}"
                )
                return None
            if deadline_at is not None and time.perf_counter() >= deadline_at:
                nearest = (
                    self._pop_nearest_buffered_frame(
                        target_frame,
                        max_distance=deadline_nearest_max_distance,
                    )
                    if allow_nearest
                    else None
                )
                logger.debug(
                    "MacOSVtZeroCopySource: interactive decode deadline "
                    f"target={target_frame}, decoded={decoded_count}, "
                    f"nearest={getattr(nearest, 'frame_index', None)}"
                )
                return nearest
            decoded = self._read_next_decoded_pixel_buffer()
            if decoded is None:
                break
            decoded_count += 1
            frame = self._make_pixel_buffer_frame(decoded)
            if frame.frame_index == target_frame:
                self._current_frame = max(self._current_frame, frame.frame_index + 1)
                return frame
            if (
                deadline_at is not None
                and deadline_nearest_max_distance is not None
                and frame.frame_index < target_frame
                and target_frame - frame.frame_index <= deadline_nearest_max_distance
            ):
                existing = self._reorder_buffer.get(frame.frame_index)
                if existing is not None:
                    frame.release()
                else:
                    self._reorder_buffer[frame.frame_index] = frame
            else:
                self._buffer_decoded_frame(frame, target_frame)
            exact = self._pop_buffered_frame(target_frame)
            if exact is not None:
                return exact

        if allow_nearest:
            return self._pop_nearest_buffered_frame(target_frame)
        return None

    def read_frame_pixel_buffer_interactive(
        self,
        frame_index: int,
        *,
        is_stale: Callable[[], bool] | None = None,
        deadline_at: float | None = None,
        prefer_seek: bool = False,
        coarse: bool = False,
    ) -> VtPixelBufferFrame | None:
        """Read a frame for interactive scrub, aborting when superseded.

        ``coarse`` is the fast-shuttle mode used while the playhead is being
        dragged quickly: instead of walking the whole gap to the exact target
        (one VT frame per step, easily 40-100ms for long GOPs), it seeks to
        the keyframe at/before the target and decodes a single presentable
        frame. The viewer keeps moving at seek+1-frame cost; the exact frame
        is produced once the drag slows down or settles.
        """
        if not self._is_open:
            return None
        if is_stale is not None and is_stale():
            return None
        if deadline_at is not None and time.perf_counter() >= deadline_at:
            return None

        if frame_index == self._current_frame:
            return self._decode_until_present_frame(
                self._current_frame,
                is_stale=is_stale,
                deadline_at=deadline_at,
                # Keep interactive fallback within two frames of the target;
                # a 12-frame approximation is visibly inaccurate during drag.
                deadline_nearest_max_distance=2,
            )

        max_forward_without_seek = 120
        if (
            coarse
            and self._demuxer is not None
            and abs(frame_index - self._current_frame) > 2
        ):
            # In coarse (fast-drag) mode even a short exact walk is a bad
            # trade: each sequential VT decode costs ~15-20ms, so an 8-frame
            # walk already blows the display budget. Only a cursor already
            # sitting on the target (gap <= 2) takes the sequential path.
            try:
                if is_stale is not None and is_stale():
                    return None
                if deadline_at is not None and time.perf_counter() >= deadline_at:
                    return None
                if self._vt_session is not None:
                    self._vt_session.flush()
                self._clear_reorder_buffer()
                self._demuxer.seek_to_frame(frame_index)
                # After a keyframe seek the first decoded output is the IDR
                # itself: presentable immediately, no reorder walk needed.
                decoded = self._read_next_decoded_pixel_buffer()
                if decoded is None:
                    return None
                frame = self._make_pixel_buffer_frame(decoded)
                # The cursor must report where decoding actually stands (the
                # keyframe), not the requested target, or a later sequential
                # read would return the wrong frame.
                self._current_frame = frame.frame_index + 1
                return frame
            except Exception as e:
                logger.warning(
                    f"MacOSVtZeroCopySource: coarse seek to frame {frame_index} failed: {e}"
                )
                return None

        if (
            not prefer_seek
            and
            frame_index > self._current_frame
            and frame_index - self._current_frame <= max_forward_without_seek
        ):
            return self._decode_until_present_frame(
                frame_index,
                is_stale=is_stale,
                deadline_at=deadline_at,
                deadline_nearest_max_distance=2,
            )

        if self._demuxer is not None:
            try:
                if is_stale is not None and is_stale():
                    return None
                if deadline_at is not None and time.perf_counter() >= deadline_at:
                    return None
                if self._vt_session is not None:
                    self._vt_session.flush()
                self._clear_reorder_buffer()
                self._demuxer.seek_to_frame(frame_index)
                self._current_frame = frame_index
                return self._decode_until_present_frame(
                    frame_index,
                    max_decode_frames=500,
                    is_stale=is_stale,
                    deadline_at=deadline_at,
                    deadline_nearest_max_distance=2,
                )
            except Exception as e:
                logger.warning(
                    f"MacOSVtZeroCopySource: interactive seek to frame {frame_index} failed: {e}"
                )
                return None

        return None

    # AUD-35130：VT 解码器存在解码器延迟与 B 帧重排（输出滞后提交若干
    # 包属正常），单次取帧最多喂这么多个包仍无输出才放弃（重排深度罕见
    # 超过 16，32 已是宽裕上界；同时防止坏流下的无界 stall）。
    _MAX_DECODE_PACKETS_PER_READ = 32

    def _read_next_decoded_pixel_buffer(self) -> "VtDecodedPixelBuffer | None":
        """
        Decode the next VT frame without touching OpenGL.

        AUD-35130：旧实现单包提交且把 decode_packet 返回 None 当 EOF——
        VT 的解码器延迟/重排期间"暂无输出"是正常中间态，这会让上层解码
        提前终止，且已从 demuxer 消费的包被丢弃（丢包叠加）。新实现：

        1. 先非阻塞取输出队列（上一包提交后的延迟输出）；
        2. 有界预算内逐包提交并取输出——None 只表示"暂无输出"，
           继续喂包，绝不当作 EOF；
        3. demuxer 耗尽后做一次终态排空（短阻塞等待在飞异步帧落地）。
           注意不能用 flush()：它会清空输出队列，反而丢弃重排尾帧。

        Returns:
            VtDecodedPixelBuffer 或 None（包预算耗尽仍无输出，或流耗尽
            且无在飞帧）
        """
        if not self._is_open or self._demuxer is None:
            return None
        if self._vt_session is None:
            return None

        # 1) 上一包提交后的延迟输出（重排/解码器延迟的产物）
        pending = self._vt_session.get_decoded_frame(timeout=0.0)
        if pending is not None:
            return self._finalize_decoded_pixel_buffer(pending, packet_pts=None)

        # 2) 有界预算内喂包；"暂无输出" != EOF
        for pkt in self._demuxer.packets(
            max_packets=self._MAX_DECODE_PACKETS_PER_READ
        ):
            if self._vt_session is None:
                return None
            decoded = self._vt_session.decode_packet(
                packet_data=pkt.data,
                pts=pkt.pts,
                is_keyframe=pkt.is_keyframe,
            )
            if decoded is not None and decoded.pixel_buffer is not None:
                return self._finalize_decoded_pixel_buffer(
                    decoded, packet_pts=pkt.pts
                )
            # decoded 为 None：VT 暂无输出（重排/延迟）——继续下一包；
            # 有 decoded 无 pixel_buffer：无表面可交付，同样继续

        # 3) demuxer 耗尽：终态排空，等待在飞异步帧落地并取回
        tail = self._vt_session.get_decoded_frame(timeout=0.2)
        if tail is not None:
            return self._finalize_decoded_pixel_buffer(tail, packet_pts=None)
        return None

    def _finalize_decoded_pixel_buffer(
        self,
        decoded: "VtDecodedPixelBuffer",
        packet_pts: int | None,
    ) -> "VtDecodedPixelBuffer":
        """
        AUD-35130：为解码输出补齐时间线元数据（frame_index/time_seconds）。

        Args:
            decoded: VT 解码输出（decode_packet 已在 pts 可用时透传包 pts）
            packet_pts: 触发该输出的包 pts（队列排空路径无包可引用时为 None）

        Returns:
            同一 decoded 对象（元数据已补齐）
        """
        frame_index = self._pts_to_frame(decoded.pts)
        if frame_index is None:
            frame_index = self._pts_to_frame(packet_pts)
        if frame_index is None:
            frame_index = self._current_frame
        try:
            decoded.frame_index = frame_index
            decoded.time_seconds = self._pts_to_seconds(decoded.pts)
        except AttributeError:
            # 冻结数据类等极端情况：元数据补齐失败不阻断帧交付
            pass
        return decoded

    def _make_pixel_buffer_frame(self, decoded: object) -> VtPixelBufferFrame:
        cm = getattr(decoded, "color_metadata", None) or {}
        raw_matrix = cm.get('color_matrix', getattr(decoded, 'color_matrix', self._stream_color_matrix))
        raw_range = cm.get('color_range', self._stream_color_range)
        raw_primaries = cm.get('color_primaries', getattr(decoded, 'color_primaries', None))
        raw_transfer = cm.get('color_transfer', getattr(decoded, 'color_transfer', None))
        return VtPixelBufferFrame(
            source=self,
            decoded=decoded,
            frame_index=int(getattr(decoded, "frame_index", 0) or 0),
            pts=getattr(decoded, "pts", None),
            time_seconds=float(getattr(decoded, "time_seconds", 0.0) or 0.0),
            width=int(getattr(decoded, "width", 0) or 0),
            height=int(getattr(decoded, "height", 0) or 0),
            pixel_format=str(getattr(decoded, "pixel_format", "nv12") or "nv12"),
            bit_depth=int(getattr(decoded, "bit_depth", 8) or 8),
            color_range=normalize_color_range(raw_range),
            color_matrix=normalize_color_matrix(raw_matrix),
            color_primaries=raw_primaries,
            color_transfer=raw_transfer,
        )

    def read_next_pixel_buffer(self) -> VtPixelBufferFrame | None:
        """Decode the next frame and return an unbound pixel buffer frame."""
        if not self._is_open:
            return None
        return self._decode_until_present_frame(self._current_frame)

    def release_pixel_buffer_frame(self, frame: VtPixelBufferFrame) -> None:
        """Release a VtPixelBufferFrame returned by read_*_pixel_buffer."""
        if self._vt_session is None or frame.decoded is None:
            return
        try:
            self._vt_session.release_decoded_frame(frame.decoded)
        except Exception as e:
            logger.warning(f"MacOSVtZeroCopySource: release pixel buffer failed: {e}")

    def import_pixel_buffer_frame(self, frame: VtPixelBufferFrame) -> GpuDecodedFrame | None:
        """
        Import a VtPixelBufferFrame into GL textures.

        This method must be called from the renderer thread with a current GL
        context.  It is kept here for backwards-compatible callers, but the edit
        timeline now does this import inside EditGpuPreviewRenderer.
        """
        from .iosurface_gl_bridge import IOSurfaceGLBridge

        pixel_buffer = frame.pixel_buffer
        if pixel_buffer is None:
            frame.release()
            self._fallback_reason = "missing_pixel_buffer"
            return None

        if self._bridge is None:
            self._bridge = IOSurfaceGLBridge(ring_size=4)
            if not self._bridge.initialize(planes_per_frame=2):
                self._bridge = None
                self._fallback_reason = "bridge_init_failed"
                frame.release()
                return None

        planes_result = self._bridge.bind_pixel_buffer(
            pixel_buffer=pixel_buffer,
            frame_index=frame.frame_index,
            pixel_format=frame.pixel_format,
            bit_depth=frame.bit_depth,
            decode_path=DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY.value,
        )
        if planes_result is None:
            self._fallback_reason = self._bridge.last_fallback_reason or "bind_failed"
            frame.release()
            return None

        gpu_planes, gpu_release = planes_result

        def _release() -> None:
            try:
                gpu_release.release()
            finally:
                frame.release()

        return GpuDecodedFrame(
            frame_index=frame.frame_index,
            pts=frame.pts,
            time_seconds=frame.time_seconds,
            width=frame.width,
            height=frame.height,
            visible_rect=(0, 0, frame.width, frame.height),
            pixel_format=frame.pixel_format,
            bit_depth=frame.bit_depth,
            planes=gpu_planes,
            color_range=frame.color_range,
            color_matrix=frame.color_matrix,
            color_primaries=frame.color_primaries,
            color_transfer=frame.color_transfer,
            decode_path=DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY.value,
            zero_copy=True,
            release=GpuFrameRelease(release_callback=_release),
        )

    # -- Phase 5：Metal 零拷贝导入（P5-07 / P5-08） ---------------------

    def import_pixel_buffer_frame_metal(self, frame: VtPixelBufferFrame) -> GpuDecodedFrame | None:
        """将 VtPixelBufferFrame 导入为 Metal GpuDecodedFrame（CVMetalTextureCache）。

        Metal 无 context 亲和性：解码线程可直接导入（与 GL 导入必须在
        渲染线程不同）；但 dispatch 消费仍须由渲染侧单线程串行（§7.1）。
        失败显式记 fallback_reason 并释放 frame——调用方回退 GL/planar
        路径，绝不黑屏。
        """
        from .cv_metal_bridge import CVMetalVideoBridge

        pixel_buffer = frame.pixel_buffer
        if pixel_buffer is None:
            frame.release()
            self._fallback_reason = "missing_pixel_buffer"
            return None

        if self._metal_bridge is None:
            self._metal_bridge = CVMetalVideoBridge(ring_size=4)
            if not self._metal_bridge.initialize():
                self._fallback_reason = (
                    self._metal_bridge.last_fallback_reason
                    or "metal_bridge_init_failed"
                )
                self._metal_bridge = None
                frame.release()
                return None

        decode_path = DecodePath.MACOS_VT_CVMETAL_ZERO_COPY.value
        result = self._metal_bridge.import_pixel_buffer(
            pixel_buffer=pixel_buffer,
            frame_index=frame.frame_index,
            pixel_format=frame.pixel_format,
            bit_depth=frame.bit_depth,
            decode_path=decode_path,
        )
        if result is None:
            self._fallback_reason = (
                self._metal_bridge.last_fallback_reason or "metal_bind_failed"
            )
            frame.release()
            return None

        gpu_planes, gpu_release = result

        def _release() -> None:
            try:
                gpu_release.release()
            finally:
                frame.release()

        return GpuDecodedFrame(
            frame_index=frame.frame_index,
            pts=frame.pts,
            time_seconds=frame.time_seconds,
            width=frame.width,
            height=frame.height,
            visible_rect=(0, 0, frame.width, frame.height),
            pixel_format=frame.pixel_format,
            bit_depth=frame.bit_depth,
            planes=gpu_planes,
            color_range=frame.color_range,
            color_matrix=frame.color_matrix,
            color_primaries=frame.color_primaries,
            color_transfer=frame.color_transfer,
            decode_path=decode_path,
            zero_copy=True,
            release=GpuFrameRelease(release_callback=_release),
        )

    def read_next_frame_metal(self) -> GpuDecodedFrame | None:
        """解码下一帧并导入为 Metal GpuDecodedFrame（benchmark/开发者预览用）。

        调用方（渲染侧）在 MetalComputeContext.finish() 之后才可释放帧。
        """
        frame = self.read_next_pixel_buffer()
        if frame is None:
            return None
        return self.import_pixel_buffer_frame_metal(frame)

    def read_frame_metal(self, frame_index: int) -> GpuDecodedFrame | None:
        """读取指定帧并导入为 Metal GpuDecodedFrame（seek 会从关键帧重解）。"""
        frame = self.read_frame_pixel_buffer(frame_index)
        if frame is None:
            return None
        if frame.frame_index != frame_index:
            frame.release()
            return None
        return self.import_pixel_buffer_frame_metal(frame)

    def wrap_pixel_buffer_deferred(self, frame) -> GpuDecodedFrame | None:
        """将 VtPixelBufferFrame 包装为"延迟 GL 导入"的 GpuDecodedFrame。

        供 read_frame_gpu_deferred 与交互式（拖拽 coarse）读取共用。
        """
        if frame is None:
            return None

        def _release() -> None:
            frame.release()

        gpu_frame = GpuDecodedFrame(
            frame_index=frame.frame_index,
            pts=frame.pts,
            time_seconds=frame.time_seconds,
            width=frame.width,
            height=frame.height,
            visible_rect=(0, 0, frame.width, frame.height),
            pixel_format=frame.pixel_format,
            bit_depth=frame.bit_depth,
            planes=(),
            color_range=frame.color_range,
            color_matrix=frame.color_matrix,
            color_primaries=frame.color_primaries,
            color_transfer=frame.color_transfer,
            decode_path=DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY.value,
            zero_copy=False,
            release=GpuFrameRelease(release_callback=_release, retain_token=frame),
        )
        gpu_frame.deferred_import = 'macos_vt_pixel_buffer'
        gpu_frame.deferred_vt_frame = frame
        return gpu_frame

    def read_frame_gpu_deferred(self, frame_index: int) -> GpuDecodedFrame | None:
        """读取指定帧并包装为"延迟 GL 导入"的 GpuDecodedFrame。

        供无 GL 上下文的解码线程（共享帧服务 worker）调用：只做 VT 解码并
        retain CVPixelBuffer，不做任何 GL 操作。返回的帧 planes 为空、
        zero_copy=False，并携带 deferred_import='macos_vt_pixel_buffer' 与
        deferred_vt_frame 属性；渲染线程（AsyncRenderer /
        EditGpuPreviewRenderer）在持有 GL 上下文时通过
        IOSurfaceGLBridge.bind_pixel_buffer 完成导入。

        帧号不精确匹配（理论不应发生）时返回 None 并释放像素缓冲。
        """
        frame = self.read_frame_pixel_buffer(frame_index)
        if frame is None:
            return None
        if frame.frame_index != frame_index:
            frame.release()
            return None
        return self.wrap_pixel_buffer_deferred(frame)

    def read_next_frame(self) -> GpuDecodedFrame | None:
        """
        读取下一帧，返回 GpuDecodedFrame。

        This legacy API imports into GL immediately, so callers must have a
        current GL context.  Timeline playback should use read_next_pixel_buffer
        and import on the renderer thread instead.
        """
        frame = self.read_next_pixel_buffer()
        if frame is None:
            return None
        return self.import_pixel_buffer_frame(frame)

    def read_frame(self, frame_index: int) -> GpuDecodedFrame | None:
        """
        读取指定帧号的帧。

        对于 VT zero-copy 源，非顺序读取需要重置解码器到最近的关键帧。
        此操作可能较慢（需要从关键帧重新解码），但不会阻塞 UI。

        Args:
            frame_index: 目标帧号

        Returns:
            GpuDecodedFrame 或 None（EOF 或错误）
        """
        frame = self.read_frame_pixel_buffer(frame_index)
        if frame is None:
            return None
        return self.import_pixel_buffer_frame(frame)

    def read_frame_pixel_buffer(self, frame_index: int) -> VtPixelBufferFrame | None:
        """
        Read a specific frame without GL import.

        This is the preferred edit timeline path: the decode thread produces a
        retained CVPixelBuffer and the GPU renderer imports it in its GL context.
        """
        if not self._is_open:
            return None

        # 如果请求当前帧，直接顺序读取
        if frame_index == self._current_frame:
            return self.read_next_pixel_buffer()

        # 前瞻预取/历史追帧留在 reorder 缓冲里的帧：游标已越过目标时
        # 精确命中直接弹出，避免 seek 回退 + 关键帧重解（这些帧是
        # 真实解码出来的，内容与 seek 重解完全一致）。
        buffered = self._pop_buffered_frame(frame_index)
        if buffered is not None:
            return buffered

        max_forward_without_seek = 120
        if (
            frame_index > self._current_frame
            and frame_index - self._current_frame <= max_forward_without_seek
        ):
            return self._decode_until_present_frame(frame_index)

        # 非顺序读取：需要 seek 到最近的关键帧
        # 对于 zero-copy 源，seek 会落到附近关键帧，必须继续 decode
        # forward 到目标帧，不能把 seek 后第一帧冒充成目标帧。
        if self._demuxer is not None:
            try:
                if self._vt_session is not None:
                    self._vt_session.flush()
                self._clear_reorder_buffer()
                self._demuxer.seek_to_frame(frame_index)
                self._current_frame = frame_index
                frame = self._decode_until_present_frame(frame_index, max_decode_frames=500)
                if frame is not None:
                    return frame
                logger.warning(
                    "MacOSVtZeroCopySource: failed to reach target frame "
                    f"{frame_index} after reorder decode"
                )
                return None
            except Exception as e:
                logger.warning(
                    f"MacOSVtZeroCopySource: seek to frame {frame_index} failed: {e}"
                )
                return None

        return None

    def prefetch_sequential_frame(self, frame_index: int) -> bool:
        """预取下一个顺序帧并放入 reorder 缓冲（播放前瞻解码）。

        仅当 frame_index 正好是当前游标（下一顺序帧）且尚未缓冲时执行，
        其它情况（乱序请求、已缓冲、EOF）直接放弃 —— 绝不 seek，绝不
        超前多于 1 帧。返回是否成功入缓冲。

        配合 read_frame_pixel_buffer 开头的精确命中快路径：播放解码波
        请求 N+1 时直接弹出预取帧，把"按需解码延迟"变成后台吞吐。
        """
        if not self._is_open:
            return False
        if frame_index != self._current_frame:
            return False
        if frame_index in self._reorder_buffer:
            return False
        frame = self._decode_until_present_frame(frame_index, allow_nearest=False)
        if frame is None or frame.frame_index != frame_index:
            if frame is not None:
                frame.release()
            return False
        self._reorder_buffer[frame_index] = frame
        return True

    def seek_frame(self, frame_index: int) -> bool:
        """
        Seek 到指定帧号。

        VT zero-copy 源的 seek 通过 demuxer 重新定位到最近关键帧。
        实际的帧数据在后续 read_next_frame() 调用时才解码。

        Args:
            frame_index: 目标帧号

        Returns:
            True 如果 seek 成功
        """
        if not self._is_open or self._demuxer is None:
            return False

        try:
            if self._vt_session is not None:
                self._vt_session.flush()
            self._clear_reorder_buffer()
            self._demuxer.seek_to_frame(frame_index)
            self._current_frame = frame_index
            logger.debug(f"MacOSVtZeroCopySource: seeked to frame {frame_index}")
            return True
        except Exception as e:
            logger.warning(
                f"MacOSVtZeroCopySource: seek_frame({frame_index}) failed: {e}"
            )
            return False

    def get_metadata(self) -> VideoMetadata | None:
        """
        获取视频元数据。

        Returns:
            VideoMetadata 或 None
        """
        return self._info


class MacOSVtPlanarSource(MacOSVtZeroCopySource):
    """VideoToolbox decode with a safe CPU-visible NV12/P010 plane download.

    This is the fallback for macOS systems where the IOSurface -> CGL bridge can
    bind textures but cannot prove correct chroma. It retains VideoToolbox's
    hardware decode, copies only YUV planes out of the CVPixelBuffer, and lets
    the edit GPU renderer perform the YUV upload and RGB conversion.
    """

    def open(self, path: str) -> bool:
        """Open a VT decode session without requiring the GL zero-copy proof."""
        return super().open(path, require_zero_copy=False)

    def read_next_frame(self) -> DecodedFrame | None:
        """Read the next display-order frame as CPU-visible planar YUV."""
        return self.read_next_planar_frame()

    def read_next_frame_interactive(
        self,
        *,
        is_stale: Callable[[], bool] | None = None,
        deadline_at: float | None = None,
    ) -> DecodedFrame | None:
        """Read the next planar frame, aborting interactive scrub if stale."""
        return self.read_next_planar_frame_interactive(
            is_stale=is_stale,
            deadline_at=deadline_at,
        )

    def read_frame(self, frame_index: int) -> DecodedFrame | None:
        """Read an exact display-order frame as CPU-visible planar YUV."""
        return self.read_frame_planar(frame_index)

    def read_next_planar_frame(self) -> DecodedFrame | None:
        """Decode the next frame and copy its native YUV planes once."""
        frame = self.read_next_pixel_buffer()
        return self._copy_pixel_buffer_frame(frame)

    def read_next_planar_frame_interactive(
        self,
        *,
        is_stale: Callable[[], bool] | None = None,
        deadline_at: float | None = None,
    ) -> DecodedFrame | None:
        """Decode the next frame with cooperative scrub cancellation."""
        frame = self.read_frame_pixel_buffer_interactive(
            self._current_frame,
            is_stale=is_stale,
            deadline_at=deadline_at,
        )
        if frame is None:
            return None
        if is_stale is not None and is_stale():
            frame.release()
            return None
        return self._copy_pixel_buffer_frame(frame)

    def read_frame_planar(self, frame_index: int) -> DecodedFrame | None:
        """Decode an exact frame and copy its native YUV planes once."""
        frame = self.read_frame_pixel_buffer(frame_index)
        return self._copy_pixel_buffer_frame(frame)

    def read_frame_planar_interactive(
        self,
        frame_index: int,
        *,
        is_stale: Callable[[], bool] | None = None,
        deadline_at: float | None = None,
        prefer_seek: bool = False,
    ) -> DecodedFrame | None:
        """Decode an exact planar frame with cooperative scrub cancellation."""
        frame = self.read_frame_pixel_buffer_interactive(
            frame_index,
            is_stale=is_stale,
            deadline_at=deadline_at,
            prefer_seek=prefer_seek,
        )
        if frame is None:
            return None
        if is_stale is not None and is_stale():
            frame.release()
            return None
        return self._copy_pixel_buffer_frame(frame)

    def _copy_pixel_buffer_frame(
        self,
        frame: VtPixelBufferFrame | None,
    ) -> DecodedFrame | None:
        """Copy a retained CVPixelBuffer to tight NV12/P010 NumPy planes."""
        if frame is None:
            return None
        try:
            planes = self._copy_pixel_buffer_planes(frame)
            if planes is None:
                return None
            y_plane, uv_plane = planes
            bit_depth = int(frame.bit_depth or 8)
            pixel_format = 'p010' if bit_depth > 8 else 'nv12'
            bytes_per_sample = 2 if bit_depth > 8 else 1
            decoded = DecodedFrame(
                frame_index=frame.frame_index,
                pts=frame.pts,
                time_seconds=frame.time_seconds,
                data=None,
                planes=(y_plane, uv_plane),
                pixel_format=pixel_format,
                dtype=np.uint16 if bytes_per_sample == 2 else np.uint8,
                width=frame.width,
                height=frame.height,
                metadata=self.metadata,
                is_proxy=False,
                plane_infos=(
                    PlaneInfo.create(
                        index=0,
                        width=frame.width,
                        height=frame.height,
                        component='Y',
                        dtype=np.uint16 if bytes_per_sample == 2 else np.uint8,
                        bit_depth=bit_depth,
                    ),
                    PlaneInfo.create(
                        index=1,
                        width=frame.width // 2,
                        height=frame.height // 2,
                        component='UV',
                        dtype=np.uint16 if bytes_per_sample == 2 else np.uint8,
                        bit_depth=bit_depth,
                        stride=frame.width * bytes_per_sample,
                    ),
                ),
                extra={
                    'decode_path': DecodePath.HWACCEL_DOWNLOAD_PLANAR_YUV.value,
                    'source': 'macos_vt_planar',
                },
            )
            # DecodedFrame permits transport metadata without a new shared DTO.
            decoded.color_range = frame.color_range
            decoded.color_matrix = frame.color_matrix
            decoded.color_primaries = frame.color_primaries
            decoded.color_transfer = frame.color_transfer
            return decoded
        finally:
            frame.release()

    @staticmethod
    def _copy_pixel_buffer_planes(
        frame: VtPixelBufferFrame,
    ) -> tuple[np.ndarray, np.ndarray] | None:
        """Copy visible CVPixelBuffer rows, removing CoreVideo row padding."""
        try:
            from .vt_session import _CV_LIB

            if _CV_LIB is None or frame.pixel_buffer is None:
                return None
            pixel_buffer = frame.pixel_buffer
            if isinstance(pixel_buffer, int):
                pointer = pixel_buffer
            else:
                import objc

                pointer = int(objc.pyobjc_id(pixel_buffer))
            if pointer == 0:
                return None

            cv_buffer = ctypes.c_void_p(pointer)
            if _CV_LIB.CVPixelBufferLockBaseAddress(cv_buffer, 1) != 0:
                return None
            try:
                bit_depth = int(frame.bit_depth or 8)
                bytes_per_sample = 2 if bit_depth > 8 else 1
                dtype = np.uint16 if bytes_per_sample == 2 else np.uint8
                y_stride = int(_CV_LIB.CVPixelBufferGetBytesPerRowOfPlane(cv_buffer, 0))
                uv_stride = int(_CV_LIB.CVPixelBufferGetBytesPerRowOfPlane(cv_buffer, 1))
                y_base = int(_CV_LIB.CVPixelBufferGetBaseAddressOfPlane(cv_buffer, 0) or 0)
                uv_base = int(_CV_LIB.CVPixelBufferGetBaseAddressOfPlane(cv_buffer, 1) or 0)
                if not y_base or not uv_base or y_stride <= 0 or uv_stride <= 0:
                    return None

                def copy_plane(base: int, stride: int, rows: int) -> np.ndarray:
                    visible_row_bytes = frame.width * bytes_per_sample
                    # ``ctypes.string_at`` first allocates a Python bytes copy,
                    # then ``.copy()`` below allocates the owned NumPy frame.
                    # Read through a borrowed ctypes view instead so the only
                    # copy is the required tight frame that outlives the lock.
                    raw_type = ctypes.c_ubyte * (stride * rows)
                    bytes_view = np.ctypeslib.as_array(
                        raw_type.from_address(base)
                    ).reshape(rows, stride)
                    tight = bytes_view[:, :visible_row_bytes].copy()
                    return tight.view(dtype).reshape(rows, frame.width)

                y_plane = copy_plane(y_base, y_stride, frame.height)
                uv_plane = copy_plane(uv_base, uv_stride, frame.height // 2)
                # CoreVideo P010 stores 10-bit code values in the high 10 bits
                # of each uint16. The upload shader expects 0..1023 samples.
                if bit_depth == 10:
                    np.right_shift(y_plane, 6, out=y_plane)
                    np.right_shift(uv_plane, 6, out=uv_plane)
                return y_plane, uv_plane
            finally:
                _CV_LIB.CVPixelBufferUnlockBaseAddress(cv_buffer, 1)
        except Exception as error:
            logger.debug(f"MacOSVtPlanarSource: CVPixelBuffer plane copy failed: {error}")
            return None


# Q01/F1.2 根治：VT 源解码公共入口加进程级 in-flight 计数（计数范围见
# src/shared/media/av_concurrency.py 模块说明）。
from src.shared.media.av_concurrency import av_operation_decorator as _av_op_dec  # noqa: E402

for _cls in [c for c in list(globals().values()) if isinstance(c, type)]:
    for _name in (
        "open", "read_frame", "read_frame_pixel_buffer", "read_frame_metal",
        "read_frame_gpu_deferred", "read_frame_pixel_buffer_interactive",
        "seek_frame",
    ):
        if _name in _cls.__dict__:
            setattr(_cls, _name, _av_op_dec(_cls.__dict__[_name]))
