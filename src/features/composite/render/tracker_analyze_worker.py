"""Tracker analyze worker — QThread wrapper around the shared Tracker2D.

A1: the Composite page's Analyze button spawns one worker per request.
Mirrors the Color page's TrackingWorker lifecycle (progress / cancel via
an event, no force-kill) with a composite-flavoured result payload: the
per-frame transform dict stored back into the tracker node's
``track_data`` parameter.
"""

import logging
import threading
from typing import Any, Dict

from PyQt6.QtCore import QThread, pyqtSignal

from src.shared.tracking.frame_sampler import AnalysisQuality, FrameSampler
from src.shared.tracking.tracker_2d import Tracker2D, TrackRequest, TrackDirection
from src.shared.tracking.types import TrackingMode

logger = logging.getLogger(__name__)

# TrackingMode / node-mode naming
_MODE_MAP = {
    "translation": TrackingMode.TRANSLATION,
    "trs": TrackingMode.TRANSLATION_ROTATION_SCALE,
    "planar": TrackingMode.HOMOGRAPHY,
}

# AUD-8508: 取消语义经 failed 信号传递的哨兵错误串，生产/消费两侧共用
# 此常量，避免魔法字符串漂移（消费方 composite_page._on_tracker_failed）。
TRACKER_ANALYZE_CANCELLED = "cancelled"


class TrackerAnalyzeWorker(QThread):
    """Analyze one tracker node's pattern region over the source media."""

    progress = pyqtSignal(str, int, int)      # node_id, current, total
    finished_ok = pyqtSignal(str, dict)       # node_id, track_data payload
    failed = pyqtSignal(str, str)             # node_id, error message

    def __init__(self, node_id: str, media_path: str, params: Dict[str, Any],
                 frame_start: int, frame_end: int, parent=None) -> None:
        super().__init__(parent)
        self._node_id = str(node_id)
        self._media_path = str(media_path)
        self._params = dict(params or {})
        self._frame_start = int(frame_start)
        self._frame_end = int(frame_end)
        self._cancel_event = threading.Event()

    def node_id(self) -> str:
        return self._node_id

    def request_cancel(self) -> None:
        self._cancel_event.set()

    def cancel(self) -> None:
        """Color-page TrackingWorker-compatible cancel entry point."""
        self.request_cancel()

    def run(self) -> None:
        try:
            self._run_analysis()
        except Exception as exc:  # never let the thread die silently
            logger.exception("Tracker analysis failed")
            self.failed.emit(self._node_id, str(exc))

    def _run_analysis(self) -> None:
        mode = str(self._params.get("mode", "translation"))
        reference_frame = int(self._params.get("reference_frame", 0) or 0)

        # Probe the source once up front: the requested range comes from the
        # page as "reference + 100000" (i.e. unbounded). The sampler does NOT
        # clamp — an out-of-range read raises TrackingFrameError and would
        # discard every frame tracked so far, so clamp here instead.
        width = height = total = 0
        try:
            with FrameSampler(self._media_path) as sampler:
                w, h, _fps, total = sampler.probe_info()
                width = int(w or 0)
                height = int(h or 0)
                total = int(total or 0)
        except Exception as exc:
            self.failed.emit(self._node_id, f"无法读取跟踪源: {exc}")
            return
        if total <= 0:
            self.failed.emit(
                self._node_id,
                f"跟踪源没有可用帧: {self._media_path}")
            return
        if reference_frame < 0 or reference_frame >= total:
            self.failed.emit(
                self._node_id,
                f"参考帧 {reference_frame} 超出素材范围（共 {total} 帧）")
            return
        frame_start = max(0, min(self._frame_start, total - 1))
        frame_end = max(frame_start, min(self._frame_end, total - 1))

        # center_x/center_y/size are the ON-SCREEN CENTER of the pattern
        # box; TrackRequest regions are top-left (x, y, w, h) rects.
        pcx = min(1.0, max(0.0, float(self._params.get("center_x", 0.5))))
        pcy = min(1.0, max(0.0, float(self._params.get("center_y", 0.5))))
        pw = min(1.0, max(0.01, float(self._params.get("size_x", 0.12))))
        ph = min(1.0, max(0.01, float(self._params.get("size_y", 0.12))))
        pattern = (pcx - pw * 0.5, pcy - ph * 0.5, pw, ph)
        # Search window: one full pattern of margin on every side
        # (3x the pattern box), clamped to the frame.
        sx = pw * 3.0
        sy = ph * 3.0
        search = (
            max(0.0, min(1.0 - sx, pattern[0] - pw)),
            max(0.0, min(1.0 - sy, pattern[1] - ph)),
            min(1.0, sx), min(1.0, sy),
        )

        request = TrackRequest(
            media_path=self._media_path,
            pattern_region=pattern,
            search_region=search,
            reference_frame=reference_frame,
            frame_start=frame_start,
            frame_end=frame_end,
            mode=_MODE_MAP.get(mode, TrackingMode.TRANSLATION),
            direction=TrackDirection.FORWARD,
            quality=AnalysisQuality.DRAFT,
        )

        tracker = Tracker2D()
        result = tracker.track(
            request,
            progress_cb=lambda cur, total: self.progress.emit(
                self._node_id, int(cur), int(total)),
            cancel_token=self._cancel_event,
        )

        if self._cancel_event.is_set():
            # Tell the page so the worker bookkeeping is cleared and the
            # node can be analyzed again — a silent return would leave the
            # node stuck in "already analyzing" forever.
            self.failed.emit(self._node_id, TRACKER_ANALYZE_CANCELLED)
            return

        transforms: Dict[int, Dict[str, Any]] = {}
        for frame, tr in (result.transforms or {}).items():
            entry = {
                "tx": float(getattr(tr, "translation", (0.0, 0.0))[0]),
                "ty": float(getattr(tr, "translation", (0.0, 0.0))[1]),
                "rotation": float(getattr(tr, "rotation", 0.0)),
                "scale_x": float(getattr(tr, "scale", (1.0, 1.0))[0]),
                "scale_y": float(getattr(tr, "scale", (1.0, 1.0))[1]),
                "status": str(getattr(getattr(tr, "status", None), "value", "valid")),
            }
            matrix = getattr(tr, "matrix", None)
            if matrix is not None:
                if len(matrix) == 9:
                    entry["matrix"] = [
                        [float(matrix[0]), float(matrix[1]), float(matrix[2])],
                        [float(matrix[3]), float(matrix[4]), float(matrix[5])],
                        [float(matrix[6]), float(matrix[7]), float(matrix[8])],
                    ]
                else:
                    entry["matrix"] = [[float(v) for v in row] for row in matrix]
            transforms[int(frame)] = entry

        payload = {
            "media_path": self._media_path,
            "width": width,
            "height": height,
            "reference_frame": reference_frame,
            "mode": mode,
            "transforms": {str(k): v for k, v in transforms.items()},
        }
        self.finished_ok.emit(self._node_id, payload)
