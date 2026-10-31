"""
Clip Decode Stream
单个 timeline clip 的解码流（规格书 §10 / Stage E2）。

职责：
- 一个 timeline clip 对应一个 display decode stream。
- 维护 source、decode cursor、FrameRingBuffer。
- 支持 disk proxy / render cache / master 按策略打开。
- 顺序播放优先 read_next_frame，避免每帧 seek。
- cursor 隔离：同素材不同 clip 各自独立 stream（规格书 §5.3 / §10.1）。

关键不变量（规格书 §10）：
- 不接受 adjacent frame 作为真实展示帧（realtime miss 时返回 None/hold）。
- 不缓存错误 source frame。
- 不在多个 timeline clip 之间共享 decode cursor。
- 大跳变时 flush 当前 decode cursor，旧 generation 的结果丢弃。
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Optional

from .frame_ring_buffer import FrameRingBuffer
from .types import FrameHandle, FrameIdentity, SourceKind

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StreamKey:
    """
    Stream 标识。

    clip_id 必须进入 key（规格书 §10.1），保证同素材不同 clip 不共享 cursor。
    source_path 可以是 proxy/cache/master path。
    """
    timeline_id: str
    clip_id: str
    asset_id: str
    source_kind: SourceKind
    source_path: str
    target_width: int
    target_height: int
    decoder_stream_key: str = ""


class ClipDecodeStream:
    """
    单 clip 解码流。

    用法：
        stream = ClipDecodeStream(key=..., media_source=source)
        stream.open_async()  # 在非热路径打开
        stream.prefetch_range(0, 30, direction=1)
        frame = stream.get_ready_frame(10)
        if frame is None:
            stream.request_frame(10, exact=True, priority=0)
    """

    def __init__(
        self,
        key: StreamKey,
        media_source: object | None,
        ring_buffer: FrameRingBuffer | None = None,
        *,
        capacity: int = 48,
    ) -> None:
        self._key = key
        self._media_source = media_source
        self._ring = ring_buffer or FrameRingBuffer(capacity=capacity)
        self._lock = threading.RLock()
        # A media source usually owns a mutable decode cursor.  Serialize the
        # source read per clip, while allowing foreground requests to jump
        # ahead of queued prefetch work.
        self._decode_condition = threading.Condition(self._lock)
        self._decode_active = False
        self._foreground_waiters = 0
        self._deferred_source_closes: list[object] = []
        self._ready = media_source is not None
        self._cursor: int = -1  # 最后一次 decode 的 source_frame
        self._closed = False
        # generation：seek flush 后递增，旧 generation 的后台解码结果丢弃
        self._generation = 0

    # ------------------------------------------------------------------
    # properties
    # ------------------------------------------------------------------

    @property
    def key(self) -> StreamKey:
        return self._key

    @property
    def ring(self) -> FrameRingBuffer:
        return self._ring

    @property
    def is_ready(self) -> bool:
        return self._ready and not self._closed

    @property
    def generation(self) -> int:
        return self._generation

    # ------------------------------------------------------------------
    # open / close
    # ------------------------------------------------------------------

    def open_async(self) -> None:
        """
        标记 stream 为 ready（source 已在非热路径打开）。

        实际的 source 打开应在 AssetProxyManager.open_proxy_source 或
        MediaSourceFactory.create 中完成，不在 decode worker 调用。
        """
        with self._lock:
            self._ready = self._media_source is not None

    def set_source(self, media_source: object) -> None:
        """注入/切换已打开的 media source（proxy ready 热切换用）。"""
        previous_source: object | None = None
        with self._lock:
            self._generation += 1
            previous_source = self._media_source
            self._media_source = media_source
            self._ready = True
            self._cursor = -1
            self._ring.clear("source switched")
            if previous_source is not None and previous_source is not media_source:
                if self._decode_active:
                    self._deferred_source_closes.append(previous_source)
                    previous_source = None
        if previous_source is not None and previous_source is not media_source:
            self._close_source(previous_source)

    def close(self) -> None:
        """关闭 stream，释放 source。"""
        with self._lock:
            self._closed = True
            # Invalidate in-flight reads too; otherwise a late decode could
            # repopulate a ring already detached from the scheduler.
            self._generation += 1
            self._ring.clear("stream closed")
            src = self._media_source
            self._media_source = None
            self._ready = False
            if self._decode_active:
                if src is not None:
                    self._deferred_source_closes.append(src)
                src = None
            self._decode_condition.notify_all()
        self._close_source(src)

    # ------------------------------------------------------------------
    # request / get
    # ------------------------------------------------------------------

    def request_frame(
        self,
        source_frame: int,
        *,
        exact: bool,
        priority: int = 0,
        expected_generation: Optional[int] = None,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        """
        请求解码一帧（同步，供 scheduler 在 decode worker 调用）。

        顺序播放优化：如果请求的 frame == cursor + 1，直接 read_next_frame，
        不 seek（规格书 §10.3）。

        exact=True 时，解码出的 frame_index 必须等于请求值，否则丢弃。
        exact=False 时允许 adjacent frame（仅 scrub 过渡）。
        """
        foreground = int(priority) <= 0
        with self._decode_condition:
            if self._closed or not self._ready:
                return False
            if cancel_event is not None and cancel_event.is_set():
                return False
            if expected_generation is not None and self._generation != expected_generation:
                return False
            if foreground:
                self._foreground_waiters += 1
            try:
                # Prefetch never waits in front of a display request.  A
                # foreground request may wait for the one read already in
                # progress, the only safe point to hand the cursor back.
                if not foreground and (
                    self._decode_active or self._foreground_waiters > 0
                ):
                    return False
                while self._decode_active:
                    if cancel_event is not None and cancel_event.is_set():
                        return False
                    self._decode_condition.wait()
                    if self._closed or not self._ready:
                        return False
                    if expected_generation is not None and self._generation != expected_generation:
                        return False
                self._decode_active = True
            finally:
                if foreground:
                    self._foreground_waiters -= 1

            src = self._media_source
            gen = self._generation
        if src is None:
            with self._decode_condition:
                self._decode_active = False
                self._decode_condition.notify_all()
            return False

        try:
            try:
                decoded = self._decode(src, source_frame)
            except Exception as e:
                logger.debug(f"request_frame decode failed: {e}")
                decoded = None

            # 旧 generation 的结果丢弃（seek/source switch/close 后）
            with self._lock:
                stale_generation = (
                    gen != self._generation
                    or self._closed
                    or not self._ready
                    or (expected_generation is not None and gen != expected_generation)
                    or (cancel_event is not None and cancel_event.is_set())
                )
            if stale_generation or decoded is None:
                self._release_decoded(decoded)
                return False

            actual_index = getattr(decoded, "frame_index", source_frame)
            mismatch = abs(actual_index - source_frame)
            # exact 模式拒绝错帧；non-exact 允许 adjacent（仅 scrub 过渡）
            if exact and mismatch > 0:
                logger.debug(
                    f"reject non-exact frame: requested={source_frame} actual={actual_index}"
                )
                self._release_decoded(decoded)
                return False
            if mismatch > 1:
                # 严重错帧一律拒绝（规格书 §25 wrong_source_frame_presented == 0）
                logger.debug(
                    f"reject far-wrong frame: requested={source_frame} actual={actual_index}"
                )
                self._release_decoded(decoded)
                return False

            handle = self._build_handle(actual_index, decoded)
            with self._lock:
                stale_generation = (
                    gen != self._generation
                    or self._closed
                    or not self._ready
                    or (expected_generation is not None and gen != expected_generation)
                    or (cancel_event is not None and cancel_event.is_set())
                )
                if not stale_generation:
                    self._cursor = actual_index
                    self._ring.put(handle)
            if stale_generation:
                self._release_decoded(decoded)
                return False
            return True
        finally:
            deferred_sources: list[object] = []
            with self._decode_condition:
                self._decode_active = False
                deferred_sources = self._deferred_source_closes
                self._deferred_source_closes = []
                self._decode_condition.notify_all()
            for deferred_source in deferred_sources:
                self._close_source(deferred_source)

    def _decode(self, src: object, source_frame: int) -> object | None:
        """
        解码一帧。顺序播放优先 read_next_frame，避免每帧 seek。
        """
        with self._lock:
            cursor = self._cursor
        read_frame = getattr(src, "read_frame", None)
        if not callable(read_frame):
            return None
        # 顺序播放：cursor+1 直接 read_next_frame
        if cursor >= 0 and source_frame == cursor + 1:
            read_next = getattr(src, "read_next_frame", None)
            if callable(read_next):
                try:
                    return read_next()
                except Exception:
                    pass  # 回退到 read_frame
        # 非顺序：read_frame 内部会 seek
        try:
            return read_frame(source_frame)
        except Exception as e:
            logger.debug(f"read_frame({source_frame}) failed: {e}")
            return None

    def get_ready_frame(self, source_frame: int) -> FrameHandle | None:
        """从 ring buffer 取 ready frame（命中计 hit）。"""
        return self._ring.get_exact(source_frame)

    def prefetch_range(
        self,
        start: int,
        end: int,
        direction: int = 1,
        *,
        priority: int = 10,
        expected_generation: Optional[int] = None,
        cancel_event: threading.Event | None = None,
    ) -> int:
        """
        预取 [start, end] 范围内缺失的帧。

        Returns:
            实际解码的帧数。
        """
        if self._closed or not self._ready:
            return 0
        decoded = 0
        rng = range(start, end + 1) if direction >= 0 else range(end, start - 1, -1)
        for f in rng:
            if cancel_event is not None and cancel_event.is_set():
                break
            with self._lock:
                if expected_generation is not None and self._generation != expected_generation:
                    break
                if self._closed or not self._ready:
                    break
            if self._ring.contains(f):
                continue
            self.request_frame(
                f,
                exact=True,
                priority=priority,
                expected_generation=expected_generation,
                cancel_event=cancel_event,
            )
            if self._ring.contains(f):
                decoded += 1
        return decoded

    # ------------------------------------------------------------------
    # seek flush
    # ------------------------------------------------------------------

    def flush(self, reason: str = "") -> int:
        """
        Flush 当前 decode cursor（大跳变时调用）。

        递增 generation，丢弃所有旧 ring frame 和进行中的解码结果。
        """
        with self._lock:
            self._generation += 1
            self._cursor = -1
            self._decode_condition.notify_all()
        n = self._ring.size
        self._ring.clear(reason or "seek flush")
        return n

    # ------------------------------------------------------------------
    # window eviction
    # ------------------------------------------------------------------

    def evict_outside_window(self, start: int, end: int) -> int:
        return self._ring.evict_outside_window(start, end)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _close_source(source: object | None) -> None:
        """Close a source only after an in-flight read has released it."""
        if source is None:
            return
        try:
            close_fn = getattr(source, "close", None)
            if callable(close_fn):
                close_fn()
        except Exception as e:
            logger.debug(f"close media source failed: {e}")

    @staticmethod
    def _release_decoded(decoded: object | None) -> None:
        """Release a GPU frame that cannot enter the ring."""
        if decoded is None:
            return
        try:
            from src.shared.media.gpu_frame import safe_release_gpu_frame

            safe_release_gpu_frame(decoded)
        except Exception:
            logger.debug("ClipDecodeStream decoded payload release failed", exc_info=True)

    def _build_handle(self, frame_index: int, decoded: object) -> FrameHandle:
        """从解码器帧构建 ``FrameHandle``，保留真实 payload 类型。

        调度器是 decode 与 GPU pool 之间的契约边界。旧实现把所有帧都
        标成 ``cpu_ndarray``，即使 payload 实际是 ``GpuDecodedFrame`` 或
        planar ``DecodedFrame``，导致后续消费者无法选择零拷贝/平面上传
        路径。这里只做类型识别，不创建 GL 资源；GL 线程上传仍由
        ``GpuFramePool`` 负责。
        """
        from src.shared.media.gpu_frame import is_gpu_decoded_frame

        data = getattr(decoded, "data", None)
        planes = getattr(decoded, "planes", ()) or ()
        # 尺寸从 metadata 或 data 推断
        width = 0
        height = 0
        if data is not None and hasattr(data, "shape") and len(data.shape) >= 2:
            height, width = int(data.shape[0]), int(data.shape[1])
        elif planes:
            first_plane = planes[0]
            if hasattr(first_plane, "shape") and len(first_plane.shape) >= 2:
                height, width = int(first_plane.shape[0]), int(first_plane.shape[1])
            elif getattr(first_plane, "height", 0) and getattr(first_plane, "width", 0):
                width = int(first_plane.width)
                height = int(first_plane.height)
        meta = getattr(decoded, "metadata", None)
        if meta is not None:
            width = width or getattr(meta, "width", 0) or 0
            height = height or getattr(meta, "height", 0) or 0
        pixel_format = getattr(decoded, "pixel_format", "bgr24") or "bgr24"
        pts = getattr(decoded, "pts", None)
        pts_seconds = float(getattr(decoded, "time_seconds", 0.0) or 0.0)
        if pts is not None and pts_seconds == 0.0:
            pts_seconds = float(pts)
        # 从 metadata 推断 time_base → seconds（简化：用 frame_index/fps）
        if meta is not None and pts is None:
            fps = getattr(getattr(meta, "fps", None), "to_fps", lambda: 25.0)()
            try:
                pts_seconds = float(frame_index) / max(float(fps), 1e-6)
            except Exception:
                pass

        if width <= 0:
            width = int(getattr(decoded, "width", 0) or 0)
        if height <= 0:
            height = int(getattr(decoded, "height", 0) or 0)

        if is_gpu_decoded_frame(decoded):
            payload_type = "gpu_texture"
        elif bool(planes) or bool(getattr(decoded, "is_planar", False)):
            payload_type = "planar_yuv"
        else:
            payload_type = "cpu_ndarray"

        raw_bit_depth = getattr(decoded, "bit_depth", 0)
        bit_depth = int(raw_bit_depth) if isinstance(raw_bit_depth, (int, float)) else 0
        if bit_depth <= 0 and meta is not None:
            raw_meta_depth = getattr(meta, "bit_depth", 0)
            if isinstance(raw_meta_depth, (int, float)):
                bit_depth = int(raw_meta_depth)
        if bit_depth <= 0:
            # DecodedFrame carries the authoritative format string rather
            # than a separate bit_depth field.  Avoid treating every uint16
            # plane as 16-bit: P010/yuv422p10le are 10-bit samples stored in
            # uint16 containers.
            # AUD-17310：旧正则要求捕获组前一个字符非数字，"p010le"/"p016le"
            # 中 "10"/"16" 前面是 "0"，永远不匹配。改为取格式串末尾完整数字
            # 串再按位深白名单判定："p010le"→"010"→10，"p016le"→"016"→16，
            # "yuv422p10le"→10，"bgr24"→24 不在白名单→8。nv12/nv21/nv16/
            # nv24 的末尾数字是子采样标记（8-bit），显式排除不参与推断。
            fmt_lower = pixel_format.lower()
            if not re.fullmatch(r"nv\d{2}(?:le|be)?", fmt_lower):
                match = re.search(r"(\d+)(?:le|be)?$", fmt_lower)
                if match:
                    trailing_depth = int(match.group(1))
                    if trailing_depth in (8, 10, 12, 14, 16):
                        bit_depth = trailing_depth
        bit_depth = bit_depth or 8
        color_space = (
            getattr(decoded, "color_space", None)
            or getattr(decoded, "color_matrix", None)
        )
        if not color_space and meta is not None:
            raw_color_space = getattr(meta, "color_space", None)
            if isinstance(raw_color_space, str):
                color_space = raw_color_space

        identity = FrameIdentity(
            timeline_id=self._key.timeline_id,
            clip_id=self._key.clip_id,
            asset_id=self._key.asset_id,
            source_path=self._key.source_path,
            source_frame=frame_index,
            source_kind=self._key.source_kind,
        )
        return FrameHandle(
            identity=identity,
            frame_index=frame_index,
            width=width,
            height=height,
            pixel_format=pixel_format,
            bit_depth=bit_depth,
            color_space=str(color_space) if color_space else None,
            pts_seconds=pts_seconds,
            source_kind=self._key.source_kind,
            payload=decoded,
            payload_type=payload_type,
            exact=True,
        )
