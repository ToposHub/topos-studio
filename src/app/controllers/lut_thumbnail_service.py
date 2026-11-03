"""R07: LUT 缩略图后台任务（DTO + 有界 LRU + 世代守卫 + GUI 投递）。

契约（计划 R07）：

- **请求 DTO**：``LutThumbnailRequest`` 携带 LUT 内容身份（sha256，非
  filename）、源身份（session/stack/frame/内容 revision）、显示变换 id、
  尺寸、会话世代——缓存键是这些的元组，**绝不只按文件名**。
- **后台评估**：单 daemon 工作线程按序处理请求：源低分辨率帧（注入的
  provider）→ sRGB 解码到 scene-linear → 候选 LUT（load_lut 支持
  topos-res://）三线性采样（R05 规格强度 1）→ sRGB 显示变换 → QImage。
- **GUI 投递**：``thumbnail_ready(entry_id, image)`` 经排队信号在 GUI 线程
  接收（调用方转 QPixmap）；过期世代/已取消的任务不投递（不覆盖当前
  clip/文件夹）。失败发 ``thumbnail_failed(entry_id, code)``（UI 显示
  "预览失败"，无图保持占位）。
- **预算**：在途请求有上限（默认 8）；缓存有界 LRU（默认 256 项）；
  ``cancel_all()`` 供滚动/搜索/关面板调用。
- **v1 近似说明**：源取自显示域低分辨率预览（sRGB round-trip）；正式
  的逐节点线性输入（R08）与真实 viewer 显示变换接入后，缓存键的
  display_transform/source_identity 自然分档，不混用旧图。
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap

_DISPLAY_TRANSFORM_V1 = "srgb-roundtrip-v1"


@dataclass(frozen=True)
class LutThumbnailRequest:
    """一次缩略图评估的完整身份（缓存键成员，非文件名）。"""

    request_id: str
    entry_id: str
    lut_identity: str            # sha256（topos-res id 内嵌 / 路径条目现场哈希）
    lut_ref: str                 # 解析引用（topos-res:// 或路径）
    source_identity: tuple        # (session_id, stack_id, frame, content_rev)
    display_transform: str
    size: tuple[int, int]
    session_generation: int

    def cache_key(self) -> tuple:
        return (self.lut_identity, self.source_identity,
                self.display_transform, self.size)


def _srgb_decode(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.04045, x / 12.92,
                    ((x + 0.055) / 1.055) ** 2.4)


def _srgb_encode(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, x * 12.92,
                    1.055 * np.power(x, 1.0 / 2.4) - 0.055)


class LutThumbnailService(QObject):
    """LUT 候选缩略图后台评估（线程安全；QImage 仅在 GUI 线程转 QPixmap）。"""

    thumbnail_ready = pyqtSignal(str, QImage)      # entry_id, 已评估图像
    thumbnail_failed = pyqtSignal(str, str)        # entry_id, 错误码

    def __init__(self, *, source_provider: Callable[[], np.ndarray | None],
                 max_pending: int = 8, cache_size: int = 256,
                 lut_hasher: Callable[[str], str] | None = None) -> None:
        super().__init__()
        self._source_provider = source_provider
        self._max_pending = int(max_pending)
        self._cache_size = int(cache_size)
        self._lut_hasher = lut_hasher or self._default_lut_hasher
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple, QPixmap] = OrderedDict()
        self._pending: dict[str, LutThumbnailRequest] = {}
        self._queue: list[LutThumbnailRequest] = []
        self._cancelled: set[str] = set()
        self._generation = 0
        self._source_identity: tuple = ("", "", 0, 0)
        self._shutdown = False
        self._worker = threading.Thread(target=self._run, name="lut-thumbnails",
                                        daemon=True)
        self._wakeup = threading.Event()
        self._worker.start()

    # ── 生命周期 ───────────────────────────────────────────────────
    @staticmethod
    def _default_lut_hasher(lut_ref: str) -> str:
        from src.shared.color.lut_resource_registry import get_lut_resource_session
        from src.shared.project.resource_resolver import resolve_project_reference
        session = get_lut_resource_session()
        if lut_ref.startswith("topos-res://"):
            # 资源 id 内嵌内容哈希（lut_<sha32>）；内容别名同样可查
            entry = None
            try:
                index = getattr(session, "_entries", {}) or {}
                for candidate in index.values():
                    if candidate.uri == lut_ref:
                        entry = candidate
                        break
            except Exception:  # noqa: BLE001
                entry = None
            if entry is not None:
                return entry.sha256
            local = resolve_project_reference(lut_ref)
            if local:
                from src.shared.project.resource_resolver import sha256_file
                return sha256_file(local)
            return lut_ref
        import os
        if os.path.isfile(lut_ref):
            from src.shared.project.resource_resolver import sha256_file
            return sha256_file(lut_ref)
        return lut_ref

    def set_source_identity(self, session_id: str, stack_id: str,
                            frame: int, content_revision: int) -> None:
        """切 clip/seek：世代 +1（在途结果作废），更新源身份。"""
        with self._lock:
            self._generation += 1
            self._source_identity = (str(session_id), str(stack_id),
                                     int(frame), int(content_revision))
            self._queue.clear()
            self._pending.clear()
        self._wakeup.set()

    def cancel_all(self) -> None:
        """滚动/搜索/关面板：清空在途（含已弹出评估中的；缓存保留）。"""
        with self._lock:
            self._cancelled.update(r.request_id for r in self._queue)
            self._cancelled.update(self._pending.keys())
            self._queue.clear()
            self._pending.clear()
        self._wakeup.set()

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True
        self._wakeup.set()

    # ── 请求 ───────────────────────────────────────────────────────
    def request(self, entry_id: str, lut_ref: str) -> str | None:
        """请求一个候选缩略图；命中缓存直接发 ready（GUI 线程安全调用）。"""
        if not isinstance(lut_ref, str) or not lut_ref:
            return None
        with self._lock:
            generation = self._generation
            source_identity = self._source_identity
            if len(self._queue) + len(self._pending) >= self._max_pending:
                return None  # 在途预算满：本轮不排（滚动会再请求）
        identity = self._lut_hasher(lut_ref)
        req = LutThumbnailRequest(
            request_id=f"{entry_id}:{identity[:12]}:{generation}",
            entry_id=entry_id, lut_identity=identity, lut_ref=lut_ref,
            source_identity=source_identity,
            display_transform=_DISPLAY_TRANSFORM_V1,
            size=(128, 72), session_generation=generation)
        cached = self._cached_pixmap(req.cache_key())
        if cached is not None:
            self.thumbnail_ready.emit(entry_id, cached.toImage())
            return req.request_id
        with self._lock:
            if any(r.entry_id == entry_id for r in self._queue):
                return None  # 同条目去重
            self._queue.append(req)
        self._wakeup.set()
        return req.request_id

    def _cached_pixmap(self, key: tuple) -> QPixmap | None:
        with self._lock:
            pixmap = self._cache.get(key)
            if pixmap is not None:
                self._cache.move_to_end(key)
            return pixmap

    # ── 工作线程 ───────────────────────────────────────────────────
    def _run(self) -> None:
        while True:
            self._wakeup.wait()
            self._wakeup.clear()
            while True:
                with self._lock:
                    if self._shutdown:
                        return
                    if not self._queue:
                        break
                    req = self._queue.pop(0)
                    if req.request_id in self._cancelled:
                        self._cancelled.discard(req.request_id)
                        continue
                    self._pending[req.request_id] = req
                    generation = self._generation
                try:
                    image = self._evaluate(req)
                except Exception as exc:  # noqa: BLE001 — 失败必须可见
                    self._finish(req, generation, failed=str(exc)[:120] or
                                 type(exc).__name__)
                    continue
                self._finish(req, generation, image=image)

    def _finish(self, req: LutThumbnailRequest, generation: int,
                image: QImage | None = None, failed: str | None = None) -> None:
        with self._lock:
            self._pending.pop(req.request_id, None)
            stale = (self._shutdown or req.session_generation != generation
                     or req.request_id in self._cancelled)
            self._cancelled.discard(req.request_id)
            if stale:
                return
            if image is not None:
                pixmap = QPixmap.fromImage(image)
                self._cache[req.cache_key()] = pixmap
                self._cache.move_to_end(req.cache_key())
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
        if stale:
            return
        if image is not None:
            self.thumbnail_ready.emit(req.entry_id, image)
        else:
            self.thumbnail_failed.emit(req.entry_id, failed or "failed")

    def _evaluate(self, req: LutThumbnailRequest) -> QImage:
        from src.shared.color.lut_loader import (
            load_lut, resample_to_unit_domain, sample_lut_trilinear,
        )
        source = self._source_provider()
        if source is None:
            raise RuntimeError("no_source")
        arr = np.asarray(source, dtype=np.float32)
        if arr.ndim != 3 or arr.shape[-1] < 3:
            raise RuntimeError("bad_source")
        # 显示域低分辨率源 → scene-linear（v1 近似：sRGB round-trip）
        linear = _srgb_decode(arr[..., :3].astype(np.float64))
        lut = resample_to_unit_domain(load_lut(req.lut_ref))
        # 强度 1（R05 规格）；LUT 采样输入即 [0,1] 域
        flat = linear.reshape(-1, 3)
        # LUT 表定义域 [0,1]，显示域源解码后已在该域
        out = sample_lut_trilinear(lut, np.clip(flat, 0.0, 1.0))
        display = _srgb_encode(out.astype(np.float64))
        h, w = arr.shape[:2]
        rgb = (np.clip(display, 0.0, 1.0).reshape(h, w, 3) * 255.0
               ).astype(np.uint8)
        image = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888)
        return image.copy()  # 脱离 numpy 缓冲所有权

    # ── 诊断 ───────────────────────────────────────────────────────
    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"cached": len(self._cache),
                    "pending": len(self._pending),
                    "queued": len(self._queue),
                    "generation": self._generation}
