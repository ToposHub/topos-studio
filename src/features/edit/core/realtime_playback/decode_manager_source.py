"""Media-source adapter for the Edit GPU decode manager.

The realtime scheduler deliberately knows nothing about Edit's decoder
implementation.  This adapter lets its worker pool reuse the already-owned
``EditGpuDecodeManager`` source cache and VideoToolbox/planar routing instead
of opening a second PyAV/VT session per timeline clip.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .clip_decode_stream import StreamKey

logger = logging.getLogger(__name__)


class DecodeManagerMediaSource:
    """Small ``read_frame`` source backed by ``EditGpuDecodeManager``.

    ``ClipDecodeStream`` treats ``None`` as a decode miss.  CPU-packed results
    are intentionally returned as misses here: the scheduler is only allowed
    to feed the GPU preview path, and consuming a packed frame would cause a
    later GPU task to silently perform an incompatible conversion.
    """

    _GPU_FRAME_TYPES = frozenset({"gpu", "vt_pixel_buffer", "planar", "vt_planar"})

    def __init__(self, manager: Any, key: StreamKey) -> None:
        self._manager = manager
        self._key = key
        self._current_frame = -1
        # AUD-17315：解码管理器异常必须可观测（首例 warning 带栈，后续
        # 同源失败降级 debug 防逐帧刷屏）。真错误与"未就绪"的 miss 语义
        # 仍以 None 返回，但不再零日志地伪装成普通 miss。
        self._error_logged = False

    @property
    def current_frame(self) -> int:
        """Last source frame read by this adapter."""
        return self._current_frame

    def read_frame(self, frame_index: int) -> Optional[Any]:
        """Decode one exact source frame through the shared GPU manager."""
        if self._manager is None:
            return None
        try:
            decoded, frame_type = self._manager.get_frame(
                self._key.source_path,
                int(frame_index),
                target_size=(int(self._key.target_width), int(self._key.target_height)),
                # Match the worker's decoder_stream_key so contiguous cuts
                # share the manager's source cache/cursor when the timeline
                # declares them as one forward decode run.
                stream_key=(self._key.decoder_stream_key or self._key.clip_id),
            )
        except Exception as e:  # noqa: BLE001 — miss 契约要求返回 None
            if not self._error_logged:
                logger.warning(
                    "DecodeManagerMediaSource: decode manager get_frame failed "
                    "(source=%s frame=%s, subsequent failures logged at debug): %s",
                    self._key.source_path, frame_index, e,
                    exc_info=True,
                )
                self._error_logged = True
            else:
                logger.debug(
                    "DecodeManagerMediaSource: decode manager get_frame failed "
                    "(source=%s frame=%s): %s",
                    self._key.source_path, frame_index, e,
                )
            return None
        if decoded is None or str(frame_type) not in self._GPU_FRAME_TYPES:
            return None
        self._current_frame = int(frame_index)
        return decoded

    def read_next_frame(self) -> Optional[Any]:
        """Read the next source frame, preserving sequential decode semantics."""
        return self.read_frame(self._current_frame + 1)

    def close(self) -> None:
        """The manager owns decoder sessions; there is nothing to close here."""
        self._manager = None
        self._error_logged = False
