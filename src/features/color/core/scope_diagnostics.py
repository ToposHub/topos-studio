"""GPU 示波器分段结构化诊断（计划 S01）。

目的：把示波器链路（E09 提交 → E02 转发 → E03 GPU_SCOPE 队列/执行 →
ScopePanel 接收 → GpuScopeWidget 呈现）中原本**静默**的丢弃/拒绝变成
可统计、可关联的事实——区分「未请求、未执行、回传被丢弃、呈现失败」。

契约（计划 S01 验收）：

- **可关闭**：``TOPOS_SCOPE_DIAG=0`` 关闭（``set_enabled`` 运行期切换）；
  默认开启——记录只是计数器 + 每 (stage, reason) 一份最后上下文快照，
  常数级开销，无逐帧分配压力。
- **不逐帧刷屏**：默认无日志；``TOPOS_SCOPE_DIAG=verbose`` 时按
  ``(stage, reason)`` 去重告警（每键最多 3 次 + 首次全文），与
  ``_gpu_scope_errors`` 同规约。
- **不同步读回、不阻塞 UI**：纯计数/快照，锁内仅 dict 操作；GL 查询
  仍只在调用方已有上下文里发生（本模块不做任何 GL 调用）。
- 关联字段：source_revision / demand revision / mode / 纹理 ID/尺寸
  （由调用方按可用性提供）。

线程边界：UI 线程（submit/panel/widget）与渲染线程（execute/queue）
都会调用；内部持锁，锁内无回调、无 IO。
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

#: 阶段（链路分段）
STAGES = ("submit", "queue", "execute", "panel", "widget")

#: 保留上下文字段（快照只留这些，避免持有纹理/帧对象）
_CONTEXT_FIELDS = (
    "mode", "revision", "source_revision", "demand_revision",
    "tex_id", "width", "height", "detail",
)

_ENV = os.environ.get("TOPOS_SCOPE_DIAG", "").strip().lower()
_VERBOSE = _ENV in ("verbose", "debug", "1")


class ScopeDiagnostics:
    """示波器链路丢弃/拒绝计数器 + 最后上下文快照（进程级单例语义）。"""

    def __init__(self, *, enabled: bool = True, verbose: bool = False
                 ) -> None:
        self._lock = threading.Lock()
        self._enabled = bool(enabled)
        self._verbose = bool(verbose) or _VERBOSE
        # (stage, reason) → {count, last_context, first_ts?}
        self._events: dict[tuple[str, str], dict[str, Any]] = {}
        self._log_counts: dict[tuple[str, str], int] = {}
        self._last_activity_monotonic: float | None = None

    # ── 开关 ──

    def set_enabled(self, enabled: bool) -> None:
        """运行期开关（False 时 record 为空操作，snapshot 保留旧数据）。"""
        with self._lock:
            self._enabled = bool(enabled)

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    # ── 记录 ──

    def record(self, stage: str, reason: str,
               **context: Any) -> None:
        """记录一次丢弃/拒绝（各字段可选；只保留白名单内的标量）。

        Args:
            stage: ``submit``/``queue``/``execute``/``panel``/``widget``。
            reason: 稳定原因码（如 ``no_front_texture``、
                ``stale_revision``、``mutex_busy``）。
            context: 关联字段（revision/mode/tex_id/尺寸等，标量）。
        """
        with self._lock:
            if not self._enabled:
                return
            key = (stage, reason)
            entry = self._events.get(key)
            clean = {name: context[name] for name in _CONTEXT_FIELDS
                     if name in context}
            if entry is None:
                entry = {"count": 0, "last_context": {}}
                self._events[key] = entry
            entry["count"] += 1
            entry["last_context"] = clean
            if self._verbose:
                count = self._log_counts.get(key, 0) + 1
                self._log_counts[key] = count
                if count <= 3:
                    logger.warning(
                        "[scope-diag] %s/%s #%d %s", stage, reason, count,
                        clean or "")

    def record_activity(self, now: float | None = None) -> None:
        """记录最近一次成功提交/回传时间（区分「未请求」与「被拒」）。"""
        with self._lock:
            self._last_activity_monotonic = now

    # ── 读取 ──

    def snapshot(self) -> dict[str, Any]:
        """诊断快照：{events: {"stage/reason": {count, last}}, last_activity}。"""
        with self._lock:
            events = {f"{stage}/{reason}": {
                "count": entry["count"],
                "last": dict(entry["last_context"])}
                for (stage, reason), entry in self._events.items()}
            return {"enabled": self._enabled,
                    "events": events,
                    "last_activity": self._last_activity_monotonic}

    def count(self, stage: str, reason: str) -> int:
        with self._lock:
            entry = self._events.get((stage, reason))
            return entry["count"] if entry else 0

    def reset(self) -> None:
        """清零（测试/「重新开始观察」入口）。"""
        with self._lock:
            self._events.clear()
            self._log_counts.clear()
            self._last_activity_monotonic = None


#: 进程级实例（E09/E02/E03/panel/widget 共用）
scope_diagnostics = ScopeDiagnostics(
    enabled=_ENV not in ("0", "off", "false", "no"))
