"""F2.3 · Trim Monitor 双帧供应——左右片真实保留边界帧的后台解码。

设计（对齐调研备忘 item 8/9）：
- 单后台线程 + per-asset 解码器（最多左右各一），latest-wins 合并请求：
  快速拖动只解码最新 cut 位置，不按帧堆积。
- generation token：每次会话结束/取消/切源 bump；投递与 UI 侧消费双重
  校验，旧 generation 帧不显示（验收"快速拖动/切源/关闭不显示旧
  generation"）。解码器句柄只在工作线程内创建/关闭（AUD-20910 同型）。
- 帧直接取自左右片素材源（经 timeline_to_source_frame 权威映射），
  与命令提交后的源窗一致（验收"单帧修剪与最终输出一致"）；不走时间线
  合成 renderer（合成帧不是保留边界帧）。
- GPU 纹理不涉及：QImage 显示路径无 GPU 句柄，无泄漏面；关闭走
  stop() → 工作线程自行 dispose（scene_cut deferred-close 同语义）。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QImage

logger = logging.getLogger(__name__)

_CLOSE = object()  # 关闭全部解码器的哨兵请求


class TrimDualFrameSignals(QObject):
    """工作线程 → UI 线程的排队信号（跨线程自动排队投递）。"""

    #: side ∈ {"left", "right"}；generation 不匹配时 UI 侧丢弃
    frame_ready = pyqtSignal(str, int, QImage, int)
    decode_failed = pyqtSignal(str, str, int)


class TrimDualFrameProvider:
    """Trim Monitor 双帧 latest-wins 解码器（roll/trim 会话期间常驻）。"""

    def __init__(
        self,
        owner: Optional[QObject] = None,
        *,
        decoder_factory: Optional[Callable[[str], Any]] = None,
        frame_converter: Optional[Callable[[Any], Optional[np.ndarray]]] = None,
    ) -> None:
        # AUD-20910 同型：signals 不挂 owner 父对象（owner 先销毁时 emit
        # 抛 RuntimeError；无父对象由本对象持有引用，断连后 emit 为 no-op）。
        self._signals = TrimDualFrameSignals()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        # (left_path, left_frame, right_path, right_frame, generation) 或 _CLOSE
        self._request: Optional[tuple] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        # {asset_path: decoder}——只在工作线程内创建/关闭；最多保留 2 个
        self._decoders: dict[str, Any] = {}
        self._decoder_factory = decoder_factory
        self._frame_converter = frame_converter

    @property
    def signals(self) -> TrimDualFrameSignals:
        return self._signals

    def start(self) -> None:
        with self._cond:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="TrimDualFrameProvider")
        self._thread.start()

    def stop(self) -> None:
        """请求线程退出（不 join；解码器由 run 循环自行 dispose）。"""
        with self._cond:
            self._running = False
            self._request = None
            self._cond.notify()

    def request(
        self,
        left_path: Optional[str],
        left_frame: Optional[int],
        right_path: Optional[str],
        right_frame: Optional[int],
        generation: int,
    ) -> None:
        """提交双帧解码请求（latest-wins：只保留最新一条）。"""
        with self._cond:
            if not self._running:
                return
            self._request = (
                left_path, left_frame, right_path, right_frame, int(generation))
            self._cond.notify()

    def invalidate(self) -> None:
        """bump 前清空待处理请求（会话结束/取消时配合 generation 校验）。"""
        with self._cond:
            self._request = None

    def close_decoders(self) -> None:
        """请求丢弃全部解码器（源切换/关闭时由 UI 线程调用）。"""
        with self._cond:
            if not self._running:
                return
            self._request = _CLOSE
            self._cond.notify()

    # ── 工作线程 ──────────────────────────────────────────────────────

    def _open_decoder(self, path: str) -> Any:
        if self._decoder_factory is not None:
            return self._decoder_factory(path)
        from src.features.edit.ui.source_viewer import (
            _open_cpu_decode_source,
        )
        return _open_cpu_decode_source(path, None)

    def _to_rgb(self, decoded) -> Optional[np.ndarray]:
        if self._frame_converter is not None:
            return self._frame_converter(decoded)
        from src.features.edit.ui.source_viewer import (
            _decoded_frame_to_rgb24,
        )
        return _decoded_frame_to_rgb24(decoded)

    def _run(self) -> None:
        try:
            while True:
                with self._cond:
                    while self._running and self._request is None:
                        self._cond.wait(timeout=1.0)
                    if not self._running:
                        return
                    request = self._request
                    self._request = None
                if request is _CLOSE:
                    self._dispose_all()
                    continue
                left_path, left_frame, right_path, right_frame, generation = \
                    request
                sides = (
                    ("left", left_path, left_frame),
                    ("right", right_path, right_frame),
                )
                for side, path, frame in sides:
                    if path is None or frame is None:
                        continue
                    self._decode_side(side, str(path), int(frame), generation)
        finally:
            self._dispose_all()

    def _decode_side(
        self, side: str, path: str, frame: int, generation: int,
    ) -> None:
        decoder = self._decoders.get(path)
        if decoder is None:
            # 最多保留左右各一：超出的旧解码器在本线程关闭
            if len(self._decoders) >= 2:
                self._dispose_all_except((path,))
            try:
                decoder = self._open_decoder(path)
                self._decoders[path] = decoder
            except Exception as exc:
                logger.debug("TrimDualFrame: open failed for %s: %s", path, exc)
                self._signals.decode_failed.emit(side, str(exc), generation)
                return
        try:
            decoded = decoder.read_frame(frame)
            rgb = self._to_rgb(decoded)
            if rgb is None:
                self._signals.decode_failed.emit(side, "empty_frame", generation)
                return
            rgb = np.ascontiguousarray(rgb)
            height, width = rgb.shape[:2]
            qimage = QImage(
                rgb.data, int(width), int(height), int(3 * width),
                QImage.Format.Format_RGB888,
            ).copy()  # 独立持有数据（工作线程缓冲不复用）
            self._signals.frame_ready.emit(side, frame, qimage, generation)
        except Exception as exc:
            logger.debug("TrimDualFrame: decode failed %s@%s: %s", path, frame, exc)
            self._signals.decode_failed.emit(side, str(exc), generation)

    def _dispose_all(self) -> None:
        for path in list(self._decoders):
            decoder = self._decoders.pop(path)
            try:
                decoder.close()
            except Exception as exc:
                logger.debug("TrimDualFrame: close failed: %s", exc)

    def _dispose_all_except(self, keep: tuple) -> None:
        for path in list(self._decoders):
            if path in keep:
                continue
            decoder = self._decoders.pop(path)
            try:
                decoder.close()
            except Exception as exc:
                logger.debug("TrimDualFrame: close failed: %s", exc)


__all__ = ["TrimDualFrameProvider", "TrimDualFrameSignals"]
