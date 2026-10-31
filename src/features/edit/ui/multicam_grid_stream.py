"""F2.5 · MulticamGridStream——主时钟驱动的有界实时多机位监看流。

设计（对齐计划 F2.5「一个主时钟驱动 + 独立有界解码/缩放预算；优先
保证 Program 输出，降质/掉监看帧可解释」与调研备忘片2清单）：

- 单后台线程 + per-角度解码器（同一素材同一档位复用，最多保留 9 路），
  latest-wins：每角度只解码最新请求帧，被更新请求追上的未解码帧计数
  ``superseded``（= 掉监看帧，可解释）。
- 降质阶梯：解码耗时超预算先抽稀（每 2/3 个请求解码一次）再降目标高度
  （96→64→48，经 ``target_size`` 直接进解码器，缩放不占用全分辨率
  解码预算）；恢复带滞回，不抖动。
- generation token：每次会话（播放开始/大跳 seek）bump；投递侧二次校验，
  旧 generation 帧不投递。解码器句柄只在工作线程内创建/关闭
  （AUD-20910 同型）。
- 可见性门控：面板隐藏/窗口最小化时 ``set_active(False)``——工作线程
  不再解码，恢复后从最新请求继续。
- 每角度统计（decoded/superseded/skipped/decode_ms p50/降质档/状态）
  随 ``session_summary`` 信号与 ``playback_trace`` 输出，验收「记录同机
  负载与掉帧」；本流为独立 CPU 小尺寸解码，不占用 Program 的 VT 硬解
  会话池（网格让位 Program 的资源隔离面）。

诚实边界：网格流解码的是各角度源帧（与切角命令同帧映射），用于监看；
Program 输出仍走主渲染管线，本流不参与成片。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QImage

from src.features.edit.core.playback_debug import playback_trace

logger = logging.getLogger(__name__)

#: 降质档：每档 = (抽稀间隔, 目标高度 px)。level 0 = 全速 96px。
DEGRADE_LADDER: tuple[tuple[int, int], ...] = (
    (1, 96),   # level 0：每个请求都解码
    (2, 96),   # level 1：隔 1 解 1
    (2, 64),   # level 2：降目标高度
    (3, 48),   # level 3：再抽稀 + 再降高度
)
#: 单帧解码预算（毫秒）≈ 24fps 一个帧间隔；超预算即升档。
DECODE_BUDGET_MS = 1000.0 / 24.0
#: 恢复滞回：连续 N 次解码显著低于预算才降档，避免抖动。
RECOVER_STREAK = 4
RECOVER_HEADROOM = 0.5
#: 解码器打开失败后的重试间隔（按该角度收到的请求数计）。
MISSING_RETRY_EVERY = 8
#: 每角度 decode_ms p50 采样窗口。
_MS_WINDOW = 24


class MulticamGridSignals(QObject):
    """工作线程 → UI 线程的排队信号（跨线程自动排队投递）。"""

    #: asset_id, timeline_frame, QImage, generation——generation 不匹配
    #: 时 UI 侧丢弃；timeline_frame 供"同一时间线时刻"对拍。
    frame_ready = pyqtSignal(str, int, QImage, int)
    #: asset_id, state ∈ {"live", "missing"}, generation
    angle_state = pyqtSignal(str, str, int)
    #: 会话统计快照（end_session / shutdown 时发射）
    session_summary = pyqtSignal(dict)


class _AnglePending:
    """某角度的最新待解码请求（latest-wins 槽位）。"""

    __slots__ = ("path", "source_frame", "timeline_frame", "generation",
                 "seq", "total_frames")

    def __init__(self) -> None:
        self.path: str = ""
        self.source_frame: int = -1
        self.timeline_frame: int = -1
        self.generation: int = -1
        self.seq: int = 0
        self.total_frames: int = 0


class _AngleStats:
    """某角度的运行统计（只在工作线程内写，快照时拷贝）。"""

    __slots__ = ("decoded", "superseded", "skipped", "decode_ms", "level",
                 "recover_streak", "failures", "last_error",
                 "pushes_since_fail", "last_seq")

    def __init__(self) -> None:
        self.decoded = 0
        self.superseded = 0
        self.skipped = 0
        self.decode_ms: list[float] = []
        self.level = 0
        self.recover_streak = 0
        self.failures = 0
        self.last_error = ""
        self.pushes_since_fail = 0
        self.last_seq = -1

    def record_ms(self, ms: float) -> None:
        self.decode_ms.append(ms)
        if len(self.decode_ms) > _MS_WINDOW:
            del self.decode_ms[0]

    def p50_ms(self) -> float:
        if not self.decode_ms:
            return 0.0
        ordered = sorted(self.decode_ms)
        return ordered[len(ordered) // 2]

    def snapshot(self) -> dict:
        return {
            "decoded": self.decoded,
            "superseded": self.superseded,
            "skipped": self.skipped,
            "decode_ms_p50": round(self.p50_ms(), 2),
            "level": self.level,
            "failures": self.failures,
            "last_error": self.last_error,
        }


class MulticamGridStream:
    """多机位角度网格实时解码流（播放会话期间常驻，暂停即让路）。"""

    def __init__(
        self,
        owner: Optional[QObject] = None,
        *,
        decoder_factory: Optional[Callable[[str, Optional[tuple[int, int]]], Any]] = None,
        frame_converter: Optional[Callable[[Any], Optional[np.ndarray]]] = None,
        trace: Optional[Callable[..., None]] = None,
    ) -> None:
        # AUD-20910 同型：signals 不挂 owner 父对象。
        self._signals = MulticamGridSignals()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._active = True            # 可见性门控（默认开，面板隐藏时关）
        self._generation = 0
        self._pending: dict[str, _AnglePending] = {}
        self._order: list[str] = []    # 角度轮转顺序（最新一次 push 的顺序）
        # {(path, target_height): decoder}——只在工作线程内创建/关闭
        self._decoders: dict[tuple[str, int], Any] = {}
        self._decoder_factory = decoder_factory
        self._frame_converter = frame_converter
        self._trace = trace if trace is not None else playback_trace
        self._session_started_at = 0.0
        self._stats: dict[str, _AngleStats] = {}
        # 一轮无任何解码动作（全被抽稀/退避跳过）→ 等下一个 push，
        # 不空转（否则缺路退避会以 µs 级间隔 hammer 重开）。
        self._idle = False

    # ── 生命周期 ──────────────────────────────────────────────────────

    @property
    def signals(self) -> MulticamGridSignals:
        return self._signals

    def start(self) -> None:
        with self._cond:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="MulticamGridStream")
        self._thread.start()

    def shutdown(self) -> None:
        """结束线程并输出最后一次会话统计（不 join；解码器由 run 自行释放）。"""
        with self._cond:
            summary = self._summary_locked()
            self._running = False
            self._cond.notify_all()
        if summary is not None:
            self._emit_summary(summary)

    def begin_session(self) -> int:
        """开始一次播放会话：bump generation，旧请求全部作废。"""
        with self._cond:
            summary = self._summary_locked()
            self._generation += 1
            self._session_started_at = time.monotonic()
            self._stats = {aid: _AngleStats() for aid in self._pending}
            self._cond.notify_all()
        if summary is not None:
            self._emit_summary(summary)
        with self._cond:
            return self._generation

    def end_session(self) -> None:
        """结束会话：bump generation 并发射统计（暂停/切时间线时调用）。"""
        with self._cond:
            summary = self._summary_locked()
            self._generation += 1
            for pending in self._pending.values():
                pending.generation = -1  # 作废未解码请求
            self._cond.notify_all()
        if summary is not None:
            self._emit_summary(summary)

    def set_active(self, active: bool) -> None:
        """可见性门控：False 时工作线程不解码（面板隐藏/最小化）。"""
        with self._cond:
            if self._active != bool(active):
                self._active = bool(active)
                self._cond.notify_all()

    # ── 请求（UI/控制器线程调用） ────────────────────────────────────

    def push(
        self,
        frames: dict,
        infos: dict,
        timeline_frame: int,
        generation: int = 0,
    ) -> None:
        """推送各角度最新应显示的源帧（latest-wins，全角度同刻）。

        Args:
            frames: asset_id → 源帧号（与切角命令同帧映射的权威值）。
            infos: asset_id → {"path": str, "total_frames": int}。
            timeline_frame: 主时钟当前时间线帧（同一批次同值，供对拍）。
            generation: 会话 generation（begin_session 返回值）。
        """
        with self._cond:
            if not self._running:
                return
            self._idle = False
            order = [aid for aid in frames if aid in infos]
            self._order = order
            for asset_id in order:
                info = infos[asset_id] or {}
                path = str(info.get("path", "") or "")
                if not path:
                    continue
                total = int(info.get("total_frames", 0) or 0)
                frame = int(frames[asset_id])
                if total > 0:
                    frame = max(0, min(frame, total - 1))
                pending = self._pending.get(asset_id)
                if pending is None:
                    pending = _AnglePending()
                    self._pending[asset_id] = pending
                    self._stats.setdefault(asset_id, _AngleStats())
                if pending.generation == int(generation) and \
                        pending.source_frame == frame:
                    continue  # 同刻同帧：无需重解
                pending.path = path
                pending.source_frame = frame
                pending.timeline_frame = int(timeline_frame)
                pending.generation = int(generation)
                pending.total_frames = total
                pending.seq += 1
            self._cond.notify_all()

    def stats(self) -> dict:
        """当前统计快照（线程安全）。"""
        with self._lock:
            return {aid: s.snapshot() for aid, s in self._stats.items()}

    # ── 工作线程 ──────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            while True:
                with self._cond:
                    while self._running and (
                            not self._active or not self._pending
                            or self._idle):
                        self._cond.wait(timeout=0.5)
                    if not self._running:
                        return
                    if not self._active:
                        continue
                worked = self._drain_one_pass()
                with self._cond:
                    self._idle = not worked
        finally:
            self._dispose_all()

    def _drain_one_pass(self) -> bool:
        """按轮转顺序解码每个角度一次；返回本轮是否发生了解码动作。"""
        with self._lock:
            order = list(self._order)
            generation = self._generation
        worked = False
        for asset_id in order:
            with self._lock:
                if not self._running or not self._active:
                    return worked
                if generation != self._generation:
                    return worked  # 会话已切换：整批作废
                pending = self._pending.get(asset_id)
                if pending is None or pending.generation != generation:
                    continue
                stats = self._stats.setdefault(asset_id, _AngleStats())
                # 同一请求只解码一次（静置不重解；排队投递迟到不再产生
                # 重复帧）
                if pending.seq == stats.last_seq:
                    continue
                interval, _height = DEGRADE_LADDER[stats.level]
                stats.pushes_since_fail += 1
                if pending.seq % interval != 0:
                    # 抽稀档：跳过本请求（帧未变时 seq 不增，不会卡死）
                    stats.skipped += 1
                    continue
                work = (pending.path, pending.source_frame,
                        pending.timeline_frame, pending.seq, asset_id)
            self._decode_angle(work, stats, generation)
            worked = True
        return worked

    def _decode_angle(
        self, work: tuple, stats: _AngleStats, generation: int,
    ) -> None:
        path, source_frame, timeline_frame, seq, asset_id = work
        _, target_height = DEGRADE_LADDER[stats.level]
        stats.last_seq = seq
        key = (path, target_height)
        if self._decoders.get(key) is None:
            # 缺路退避：打开/解码失败后按请求数节流重开，避免对永久
            # 缺失的源每轮 hammer open；重开成功即恢复（验收"缺路恢复"）。
            if stats.failures and \
                    stats.pushes_since_fail < MISSING_RETRY_EVERY:
                return
            stats.pushes_since_fail = 0
            decoder = self._decoder_for(path, target_height, stats,
                                        asset_id, generation)
        else:
            decoder = self._decoders[key]
        if decoder is None:
            return
        started = time.monotonic()
        try:
            decoded = decoder.read_frame(source_frame)
            rgb = self._to_rgb(decoded)
            if rgb is None:
                raise ValueError("empty_frame")
        except Exception as exc:
            stats.failures += 1
            stats.last_error = str(exc)
            logger.debug(
                "MulticamGrid: decode failed %s@%s: %s",
                path, source_frame, exc)
            self._maybe_recover_decoder(path, target_height)
            self._signals.angle_state.emit(asset_id, "missing", generation)
            return
        decode_ms = (time.monotonic() - started) * 1000.0
        stats.record_ms(decode_ms)
        stats.decoded += 1
        stats.failures = 0
        stats.last_error = ""
        self._adjust_level(stats, decode_ms, asset_id, path)
        # 投递前二次校验：请求已被更新（superseded）或会话已切换 → 丢弃
        with self._lock:
            pending = self._pending.get(asset_id)
            fresh = (
                pending is not None
                and pending.seq == seq
                and pending.generation == generation
                and self._generation == generation
            )
        if not fresh:
            stats.superseded += 1
            return
        qimage = self._rgb_to_qimage(rgb)
        if qimage is None:
            return
        self._signals.frame_ready.emit(asset_id, timeline_frame, qimage,
                                       generation)

    def _decoder_for(
        self, path: str, target_height: int, stats: _AngleStats,
        asset_id: str, generation: int,
    ) -> Any:
        key = (path, target_height)
        decoder = self._decoders.get(key)
        if decoder is not None:
            return decoder
        # 有界：超出最多 9 路时先释放最久未用的（同素材换档位不重复计入）
        if len({p for p, _h in self._decoders}) >= 9 and \
                path not in {p for p, _h in self._decoders}:
            self._dispose_least_recent_new_path(path)
        try:
            decoder = self._open_decoder(path, target_height)
            self._decoders[key] = decoder
            stats.last_error = ""
            self._signals.angle_state.emit(asset_id, "live", generation)
            return decoder
        except Exception as exc:
            stats.failures += 1
            stats.last_error = str(exc)
            logger.debug("MulticamGrid: open failed %s: %s", path, exc)
            self._signals.angle_state.emit(asset_id, "missing", generation)
            return None

    def _maybe_recover_decoder(self, path: str, target_height: int) -> None:
        """解码失败：丢弃该解码器，等待 MISSING_RETRY_EVERY 后重开。"""
        key = (path, target_height)
        decoder = self._decoders.pop(key, None)
        if decoder is not None:
            try:
                decoder.close()
            except Exception as exc:
                logger.debug("MulticamGrid: close failed: %s", exc)

    def _adjust_level(
        self, stats: _AngleStats, decode_ms: float,
        asset_id: str, path: str,
    ) -> None:
        if decode_ms > DECODE_BUDGET_MS and stats.level < len(DEGRADE_LADDER) - 1:
            stats.level += 1
            stats.recover_streak = 0
            self._trace(
                "multicam_grid.degrade",
                asset_id=asset_id, level=stats.level,
                decode_ms=round(decode_ms, 2))
            # 降高度档需要换解码器：旧档位句柄由有界释放/关闭覆盖
            self._dispose_heights(path, except_height=DEGRADE_LADDER[stats.level][1])
        elif decode_ms < DECODE_BUDGET_MS * RECOVER_HEADROOM and stats.level > 0:
            stats.recover_streak += 1
            if stats.recover_streak >= RECOVER_STREAK:
                stats.level -= 1
                stats.recover_streak = 0
                self._dispose_heights(
                    path, except_height=DEGRADE_LADDER[stats.level][1])
        else:
            stats.recover_streak = 0

    def _dispose_heights(self, path: str, except_height: int) -> None:
        """档位变化：关闭该素材其他高度的解码器（线程内安全）。"""
        for key in list(self._decoders):
            p, height = key
            if p == path and height != except_height:
                decoder = self._decoders.pop(key)
                try:
                    decoder.close()
                except Exception as exc:
                    logger.debug("MulticamGrid: close failed: %s", exc)

    def _dispose_least_recent_new_path(self, keep_path: str) -> None:
        """路径数超上限：关闭一个非当前素材的解码器（dict 序 = 最近使用）。"""
        for key in list(self._decoders):
            path, _height = key
            if path == keep_path:
                continue
            decoder = self._decoders.pop(key)
            try:
                decoder.close()
            except Exception as exc:
                logger.debug("MulticamGrid: close failed: %s", exc)
            return

    def _open_decoder(self, path: str, target_height: int) -> Any:
        if self._decoder_factory is not None:
            return self._decoder_factory(path, (target_height * 16 // 9,
                                                target_height))
        from src.features.edit.ui.source_viewer import (
            _open_cpu_decode_source,
        )
        return _open_cpu_decode_source(
            path, (target_height * 16 // 9, target_height))

    def _to_rgb(self, decoded) -> Optional[np.ndarray]:
        if self._frame_converter is not None:
            return self._frame_converter(decoded)
        from src.features.edit.ui.source_viewer import (
            _decoded_frame_to_rgb24,
        )
        return _decoded_frame_to_rgb24(decoded)

    @staticmethod
    def _rgb_to_qimage(rgb: np.ndarray) -> Optional[QImage]:
        try:
            rgb = np.ascontiguousarray(rgb)
            height, width = rgb.shape[:2]
            if height < 1 or width < 1:
                return None
            return QImage(
                rgb.data, int(width), int(height), int(3 * width),
                QImage.Format.Format_RGB888,
            ).copy()  # 独立持有数据（工作线程缓冲不复用）
        except Exception:  # noqa: BLE001 — 尺寸异常时静默丢帧
            logger.debug("MulticamGrid: rgb→qimage failed", exc_info=True)
            return None

    def _dispose_all(self) -> None:
        for key in list(self._decoders):
            decoder = self._decoders.pop(key)
            try:
                decoder.close()
            except Exception as exc:
                logger.debug("MulticamGrid: close failed: %s", exc)

    # ── 统计 ──────────────────────────────────────────────────────────

    def _summary_locked(self) -> Optional[dict]:
        """会话统计（调用方持锁）；无任何数据时返回 None 不发射。"""
        if not self._stats or not self._session_started_at:
            return None
        duration_s = time.monotonic() - self._session_started_at
        summary = {
            "duration_s": round(duration_s, 3),
            "angles": {
                aid: stats.snapshot() for aid, stats in self._stats.items()
            },
        }
        self._session_started_at = 0.0
        return summary

    def _emit_summary(self, summary: dict) -> None:
        self._trace("multicam_grid.session_summary", **{
            "duration_s": summary.get("duration_s"),
            "angles": ";".join(
                f"{aid}:{snap['decoded']}d/{snap['superseded']}x/"
                f"{snap['skipped']}s/L{snap['level']}/"
                f"{snap['decode_ms_p50']}ms"
                for aid, snap in summary.get("angles", {}).items()
            ) or "-",
        })
        self._signals.session_summary.emit(summary)


__all__ = ["MulticamGridStream", "MulticamGridSignals", "DEGRADE_LADDER",
           "DECODE_BUDGET_MS"]
