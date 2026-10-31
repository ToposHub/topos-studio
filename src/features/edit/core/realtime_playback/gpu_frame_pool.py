"""
GPU Frame Pool
FrameHandle payload → GPU-resident frame 的生命周期管理（规格书 §12 / Stage F）。

职责：
- 将 FrameHandle 的 payload（planar YUV / cpu ndarray / external texture）
  转成 GPU 可合成资源。
- 管理 GPU texture 生命周期（generation release / GC）。
- 复用现有 yuv_gpu_upload.py 的上传逻辑。

线程规则（规格书 §12.3）：
- ensure_gpu_frame() 必须在 GL/render 线程执行。
- decode thread 只产出 CPU/planar/external handle，不直接创建 ModernGL texture。
- external texture 必须有明确 release 语义。
- AUD-17404：GL 删除（handle.release()）同样只在 GL 线程执行——
  release_generation()/shutdown()/容量淘汰在非 GL 线程调用时把释放
  延迟入队，由 ensure_gpu_frame 入口 process_pending_releases() drain。

关键不变量（规格书 §12.3）：
- 不在 decode worker 调 ModernGL。
- 不每帧创建大量 texture 后不释放。
- 不 GPU readback 到 CPU 再上传。
- 不把 QImage 作为 GPU pool 的中间格式。

所有权语义（AUD-17400）：
- 池独占释放：resident 期间由池统一通过 release_generation()/shutdown()/
  容量淘汰释放纹理。调用方不得对 ensure_gpu_frame 返回的共享 handle 调
  release()（同一 cache key 的 handle 是池内单例，调用方释放会造成
  use-after-release / 双重释放）。release 回调已由池包装为幂等，误调用
  只会把条目安全逐出，不会二次释放底层纹理。
- external texture（IOSurface 等）：所有权归解码侧，池不释放
  （release=None 即"外部所有者"登记，见 _default_upload）。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

from PyQt6.QtCore import QObject

from .timeline_gpu_compositor import GpuFrameHandle
from .types import FrameHandle

logger = logging.getLogger(__name__)


class GpuFramePool(QObject):
    """
    GPU frame 上传/缓存池。

    用法（GL/render 线程）：
        pool = GpuFramePool()
        gpu_frame = pool.ensure_gpu_frame(frame_handle, generation_id=1)
        ... 使用 gpu_frame.texture_ids ...
        # 池独占释放（AUD-17400）：调用方不要调 gpu_frame.release()。
        pool.release_generation(1)   # seek 后批量释放
        pool.shutdown()              # 关闭时释放全部
    """

    def __init__(self, max_resident: int = 64, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._max_resident = max(4, int(max_resident))
        self._lock = threading.RLock()
        # (clip_id, frame_index, generation_id) -> GpuFrameHandle
        self._resident: Dict[tuple, GpuFrameHandle] = {}
        self._upload_fn: Optional[callable] = None  # 注入的 GPU 上传函数
        # AUD-17404：延迟释放队列。release_generation/shutdown/容量淘汰
        # 可能在非 GL 线程被调用，而 handle.release() 内部执行 GL 删除
        # （ModernGL texture release）——统一改为入队，由 GL 线程在
        # ensure_gpu_frame 入口 drain（process_pending_releases）。
        self._pending_releases: list = []
        # 首次 ensure_gpu_frame 的线程 id（= GL/render 线程）；None 表示
        # 尚无 GL 线程记录（headless/测试），此时释放退回原行为（就地）。
        self._gl_thread_id: Optional[int] = None
        # 统计
        self._uploads = 0
        self._cache_hits = 0
        self._releases = 0
        self._deferred_releases = 0
        self._gl_calls_from_decode_thread = 0  # 必须 0（规格书 §25）

    # ------------------------------------------------------------------
    # configure
    # ------------------------------------------------------------------

    def set_upload_function(self, upload_fn: callable) -> None:
        """
        注入 GPU 上传函数。

        upload_fn 签名：(frame: FrameHandle) -> GpuFrameHandle
        必须在 GL/render 线程执行。默认使用 lazy import 的 yuv_gpu_upload。
        """
        self._upload_fn = upload_fn

    def _default_upload(self, frame: FrameHandle) -> Optional[GpuFrameHandle]:
        """
        默认上传逻辑（包装现有 yuv_gpu_upload / video_upload）。

        在 GL/render 线程执行。如果 payload 已经是 external GPU texture，
        直接包装返回。
        """
        payload = frame.payload
        if payload is None:
            return None

        # external texture（macOS VT IOSurface 等）直接包装。
        # AUD-17401：external 纹理所有权归解码侧（IOSurface 生命周期由
        # 解码器管理），这里 release=None 是显式的"外部所有者"登记——
        # 池只登记不释放，由 payload 提供方释放；若 payload 自带
        # release 回调则透传给池统一调用。
        texture_id = getattr(payload, "texture_id", 0)
        if texture_id and texture_id > 0:
            external_release = getattr(payload, "release", None)
            return GpuFrameHandle(
                identity=frame.identity,
                texture_ids=(int(texture_id),),
                texture_type="external",
                width=getattr(payload, "width", frame.width),
                height=getattr(payload, "height", frame.height),
                bit_depth=frame.bit_depth,
                color_space=frame.color_space,
                generation_id=0,
                release=external_release if callable(external_release) else None,
            )

        # planar / cpu payload：尝试通过 video_upload 上传
        try:
            from src.shared.video.video_upload import upload_frame_to_gpu
            result = upload_frame_to_gpu(payload)
            if result is not None:
                tex_id = getattr(result, "texture_id", 0)
                if tex_id > 0:
                    # AUD-17401：上传纹理必须带 release 回调入池，否则容量
                    # 淘汰/release_generation/shutdown 全部跳过释放 → GPU
                    # 纹理泄漏。上传结果不提供 release 时拒绝入池（返回
                    # None），不允许 release=None 的池自有纹理存在。
                    upload_release = getattr(result, "release", None)
                    if not callable(upload_release):
                        logger.warning(
                            "GpuFramePool._default_upload: upload result has no "
                            "release callback; refusing to pool texture %s",
                            tex_id,
                        )
                        return None
                    return GpuFrameHandle(
                        identity=frame.identity,
                        texture_ids=(int(tex_id),),
                        texture_type="rgb",
                        width=getattr(result, "width", frame.width),
                        height=getattr(result, "height", frame.height),
                        bit_depth=frame.bit_depth,
                        color_space=frame.color_space,
                        generation_id=0,
                        release=upload_release,
                    )
        except ImportError:
            logger.debug(
                "GpuFramePool._default_upload: upload_frame_to_gpu unavailable"
            )
        except Exception as e:
            logger.debug(f"_default_upload failed: {e}")

        # 无法上传到 GPU
        return None

    # ------------------------------------------------------------------
    # ensure_gpu_frame（规格书 §12.1）
    # ------------------------------------------------------------------

    def ensure_gpu_frame(
        self,
        frame: FrameHandle,
        *,
        generation_id: int,
    ) -> Optional[GpuFrameHandle]:
        """
        确保 FrameHandle 有对应的 GPU-resident frame。

        必须在 GL/render 线程执行。

        Returns:
            GpuFrameHandle 或 None（无法上传时）。
        """
        # AUD-17404：本方法是契约规定的 GL/render 线程入口——记录线程
        # 身份并顺带 drain 此前从非 GL 线程延迟下来的释放。
        if self._gl_thread_id is None:
            self._gl_thread_id = threading.get_ident()
        self.process_pending_releases()
        # Batch 4 (缺陷计划 RP-P0-06/P1-2): cache key 必须完整纳入所有影响
        # GPU texture 内容的字段，否则同 clip/frame 在不同 source（proxy/master/
        # cache）或不同 pixel_format/size 间会复用错误 texture，显示错帧。
        cache_key = self._build_cache_key(frame, generation_id)
        with self._lock:
            cached = self._resident.get(cache_key)
            if cached is not None:
                self._cache_hits += 1
                return cached

        # 上传（GL/render 线程）
        upload_fn = self._upload_fn or self._default_upload
        try:
            gpu_frame = upload_fn(frame)
        except Exception as e:
            logger.debug(f"ensure_gpu_frame upload failed: {e}")
            return None

        if gpu_frame is None:
            return None

        gpu_frame.generation_id = generation_id
        # AUD-17400：池独占释放语义。包装 release 为幂等回调：释放时同步
        # 把条目逐出 _resident（调用方误调 release 后缓存不会再命中已释放
        # 纹理），且底层释放只执行一次（容量淘汰/release_generation/shutdown
        # 与外部误调用并发时不会双重释放）。
        self._install_pool_release(gpu_frame, cache_key)
        with self._lock:
            self._resident[cache_key] = gpu_frame
            self._uploads += 1
            # 容量淘汰
            self._enforce_capacity_locked()
        return gpu_frame

    def _install_pool_release(
        self,
        handle: GpuFrameHandle,
        cache_key: tuple,
    ) -> None:
        """把 handle.release 包装成池持有的幂等释放回调（AUD-17400）。

        - 首次调用：逐出 resident 条目并调用原始 release（external 纹理
          的原始 release 为 None 时只逐出，不动外部纹理）。
        - 重复调用：无操作（防双重释放）。
        """
        original_release = handle.release
        released = threading.Event()

        def _pool_release() -> None:
            if released.is_set():
                return
            released.set()
            with self._lock:
                self._resident.pop(cache_key, None)
            if original_release is not None:
                try:
                    original_release()
                except Exception:
                    logger.debug("GpuFramePool release callback failed", exc_info=True)

        handle.release = _pool_release

    @staticmethod
    def _build_cache_key(frame: FrameHandle, generation_id: int) -> tuple:
        """
        Batch 4 (缺陷计划 RP-P0-06/P1-2): 构建完整的 cache key。

        key 必须包含所有影响 GPU texture 内容的字段：
        - timeline_id / clip_id / asset_id / source_path：帧身份
        - source_frame：源帧号
        - source_kind：DISK_PROXY / RENDER_CACHE / MEMORY_PROXY / MASTER / PLACEHOLDER
        - proxy_key / render_cache_key：代理/缓存标识（同 source 不同 proxy 不复用）
        - pixel_format / bit_depth / width / height：纹理格式（不同格式不复用）
        - generation_id：调度 generation（seek 后失效）

        不纳入这些字段会导致：同 clip/frame 从 proxy 切到 master 后命中旧
        proxy texture，显示错帧（缺陷计划 §5 "缓存 GPU texture id 到 frame cache"）。
        """
        identity = frame.identity
        return (
            identity.timeline_id,
            identity.clip_id,
            identity.asset_id,
            identity.source_path,
            identity.source_frame,
            identity.source_kind,            # SourceKind enum
            identity.proxy_key,              # 磁盘代理 key（可空）
            identity.render_cache_key,       # 渲染缓存 key（可空）
            frame.pixel_format,              # bgr24 / yuv420p / yuv422p10le ...
            frame.bit_depth,                 # 8 / 10 / 12 / 16
            frame.width,
            frame.height,
            generation_id,
        )

    def _enforce_capacity_locked(self) -> None:
        """容量淘汰（持锁调用）。"""
        if len(self._resident) <= self._max_resident:
            return
        # 简单 FIFO 淘汰（按插入顺序）
        excess = len(self._resident) - self._max_resident
        keys = list(self._resident.keys())[:excess]
        for k in keys:
            # AUD-17400：resident 句柄的 release 已由 _install_pool_release
            # 包装为池持有的幂等回调（含条目逐出），此处无需再判 None。
            handle = self._resident.pop(k, None)
            if handle is not None:
                # AUD-17404：容量淘汰也可能发生在非 GL 线程（shutdown 前
                # 的统计/维护路径），底层 GL 删除统一走延迟规则。
                self._defer_or_release(handle)
                self._releases += 1

    # ------------------------------------------------------------------
    # release
    # ------------------------------------------------------------------

    def _on_gl_thread(self) -> bool:
        """当前线程是否为已记录的 GL/render 线程（AUD-17404）。

        尚无记录（headless/测试，从未跑过 ensure_gpu_frame）时保守返回
        True——无从延迟，保持旧的就地释放行为。
        """
        recorded = self._gl_thread_id
        return recorded is None or recorded == threading.get_ident()

    def _defer_or_release(self, handle: Any) -> None:
        """GL 删除操作只允许发生在 GL 线程（AUD-17404）。

        - GL 线程（或无记录）：就地 release（旧行为）。
        - 非 GL 线程：入队延迟，由 ensure_gpu_frame 入口的
          process_pending_releases() 在 GL 线程 drain。条目已先从
          _resident 逐出，延迟不会引起新条目被误逐出。
        """
        if self._on_gl_thread():
            release = getattr(handle, "release", None)
            if release is not None:
                try:
                    release()
                except Exception:
                    logger.debug("GpuFramePool inline release failed", exc_info=True)
            return
        with self._lock:
            self._pending_releases.append(handle)
            self._deferred_releases += 1

    def process_pending_releases(self) -> int:
        """在 GL/render 线程 drain 延迟释放队列（AUD-17404）。

        ensure_gpu_frame 入口自动调用；关闭路径也可显式调用。
        Returns: 本次实际释放的句柄数。
        """
        while True:
            with self._lock:
                if not self._pending_releases:
                    return 0
                handle = self._pending_releases.pop(0)
            release = getattr(handle, "release", None)
            if release is not None:
                try:
                    release()
                except Exception:
                    logger.debug("GpuFramePool deferred release failed", exc_info=True)

    def release_generation(self, generation_id: int) -> int:
        """释放某 generation 的所有 GPU frame（seek 后）。

        AUD-17404：本方法可从非 GL 线程（UI/engine）调用——handle.release()
        内部是 GL 删除操作，非 GL 线程一律入队延迟（见 _defer_or_release）。
        """
        released = 0
        with self._lock:
            # Batch 4: cache key 最后一个字段是 generation_id（index 12）。
            stale = [k for k in self._resident if k[-1] == generation_id]
            for k in stale:
                handle = self._resident.pop(k, None)
                if handle is not None:
                    self._defer_or_release(handle)
                    released += 1
                    self._releases += 1
        return released

    def collect_garbage(self) -> int:
        """显式 GC（可定期调用）。"""
        # 当前 _enforce_capacity_locked 已在 put 时淘汰，
        # 这里额外释放所有 release 不为 None 的过期 entry。
        with self._lock:
            return 0  # capacity-based GC 已足够

    def shutdown(self) -> None:
        """释放所有 GPU frame。

        AUD-17404：与 release_generation 相同的线程规则——非 GL 线程调用
        时底层 GL 删除延迟入队（GL 线程下次 ensure 时 drain），不在此
        执行跨线程 GL 删除。
        """
        with self._lock:
            handles = list(self._resident.values())
            self._resident.clear()
            # AUD-17400：逐出必须持锁完成——包装 release 会按 cache_key
            # 逐出 resident 条目，与并发 ensure_gpu_frame 的新条目（同 key
            # 重传）竞争时会误逐出。底层 release 走 _defer_or_release。
            for handle in handles:
                self._defer_or_release(handle)
            self._releases += len(handles)

    # ------------------------------------------------------------------
    # stats
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        with self._lock:
            return {
                "resident": len(self._resident),
                "max_resident": self._max_resident,
                "uploads": self._uploads,
                "cache_hits": self._cache_hits,
                "releases": self._releases,
                "deferred_releases": self._deferred_releases,
                "pending_releases": len(self._pending_releases),
                "gl_calls_from_decode_thread": self._gl_calls_from_decode_thread,
            }
