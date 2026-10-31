"""
Adaptive Playback Quality Controller
缺陷计划 RP-P1-10：负载驱动的自适应播放质量。

## 职责

把播放负载观测（miss / back-pressure）转换为离散质量档位，供渲染端
调整 decode scale。纯决策逻辑，不依赖 Qt、不调用渲染/解码，方便单测。

## 设计要点（对应 P1-10 验收）

- 档位 ladder：NORMAL(1.0) → ELEVATED(0.5) → CRITICAL(0.25)，
  只作用于 auto/smooth 预览档（用户显式选择分辨率时视为手动覆盖，
  自适应不介入——"用户偏好允许下"的实现）。
- 迟滞（hysteresis）：连续 ``down_streak`` 帧压力才降档；连续
  ``up_streak`` 帧健康才升档，避免单帧抖动引发档位震荡。
- 最小驻留（dwell）：每档至少保持 ``min_dwell_frames`` 帧，防止在
  解码会话缓存键（64px 桶）之间反复切换造成 open/close 风暴。
- 可见可回退：每次档位变化返回事件（旧档/新档/原因/连续计数），
  由 engine 发信号、renderer 发 trace；升档与播放停止自动回退。
- 归因感知（复用 P1-05 MissReason）：只有降 scale 真正有帮助的
  miss 类别计入压力（decode/gpu busy/cache warming/back pressure/
  delegate）；proxy pending 与 unsupported effect 不计（降分辨率
  无法改善这两类等待）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from .types import MissReason, classify_miss_reason


# 降 scale 能改善的 miss 类别（P1-05 词表的负载子集）。
LOAD_PRESSURE_REASONS = frozenset({
    MissReason.DECODE_MISS,
    MissReason.GPU_BUSY,
    MissReason.CACHE_WARMING,
    MissReason.BACK_PRESSURE,
    MissReason.DELEGATE_MISS,
})


class QualityPressureLevel(Enum):
    """自适应质量档位（PLAYING 态 auto2k 基准上的缩放系数）。"""

    NORMAL = "normal"      # 1.0：无持续压力
    ELEVATED = "elevated"  # 0.5：持续压力一档
    CRITICAL = "critical"  # 0.25：持续压力两档

    @property
    def scale(self) -> float:
        return {
            QualityPressureLevel.NORMAL: 1.0,
            QualityPressureLevel.ELEVATED: 0.5,
            QualityPressureLevel.CRITICAL: 0.25,
        }[self]

    @property
    def rank(self) -> int:
        """压力序（越大越紧张），用于步进比较。"""
        return {
            QualityPressureLevel.NORMAL: 0,
            QualityPressureLevel.ELEVATED: 1,
            QualityPressureLevel.CRITICAL: 2,
        }[self]


_LEVEL_BY_VALUE = {level.value: level for level in QualityPressureLevel}


def level_from_value(value: Any) -> Optional[QualityPressureLevel]:
    """把信号侧字符串还原为档位（未知值返回 None）。"""
    return _LEVEL_BY_VALUE.get(str(value))


@dataclass(frozen=True)
class AdaptiveQualityEvent:
    """一次档位变化（可见可回退的证据载体）。"""

    old_level: QualityPressureLevel
    new_level: QualityPressureLevel
    reason: str
    pressure_streak: int = 0
    healthy_streak: int = 0


class AdaptiveQualityController:
    """负载观测 → 质量档位状态机。

    用法（engine 每播放 tick 一次）::

        ctrl = AdaptiveQualityController()
        event = ctrl.observe(
            presented=ok,
            back_pressured=budget.is_back_pressured(),
            miss_reason=reason,
        )
        if event is not None:
            emit_signal(event.new_level.value, {...})
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        down_streak: int = 6,
        up_streak: int = 24,
        min_dwell_frames: int = 30,
    ) -> None:
        self._enabled = bool(enabled)
        self._down_streak = max(1, int(down_streak))
        self._up_streak = max(1, int(up_streak))
        self._min_dwell_frames = max(0, int(min_dwell_frames))
        self._level = QualityPressureLevel.NORMAL
        self._frames_at_level = 0
        self._pressure_streak = 0
        self._healthy_streak = 0
        self._total_downgrades = 0
        self._total_upgrades = 0

    # ------------------------------------------------------------------
    # 观测与状态机
    # ------------------------------------------------------------------

    def observe(
        self,
        *,
        presented: bool,
        back_pressured: bool = False,
        miss_reason: Optional[str] = None,
    ) -> Optional[AdaptiveQualityEvent]:
        """消费一帧播放观测，可能返回档位变化事件。

        Args:
            presented: 本帧是否成功提交呈现。
            back_pressured: 播放预算是否处于 back-pressure（连续超预算）。
            miss_reason: miss 时的归因字符串（P1-05 词表或生产方原文）。
        """
        is_pressure = bool(back_pressured)
        if not presented and not is_pressure:
            is_pressure = classify_miss_reason(miss_reason) in LOAD_PRESSURE_REASONS

        self._frames_at_level += 1
        if is_pressure:
            self._pressure_streak += 1
            self._healthy_streak = 0
        else:
            self._healthy_streak += 1
            self._pressure_streak = 0

        if not self._enabled:
            return self._force_normal("disabled")

        if (
            self._pressure_streak >= self._down_streak
            and self._level.rank < QualityPressureLevel.CRITICAL.rank
            and self._frames_at_level >= self._min_dwell_frames
        ):
            return self._step(
                QualityPressureLevel(self._level.value),
                delta=+1,
                reason=f"pressure_streak:{self._pressure_streak}",
            )
        if (
            self._healthy_streak >= self._up_streak
            and self._level.rank > QualityPressureLevel.NORMAL.rank
            and self._frames_at_level >= self._min_dwell_frames
        ):
            return self._step(
                QualityPressureLevel(self._level.value),
                delta=-1,
                reason=f"healthy_streak:{self._healthy_streak}",
            )
        return None

    def reset(self, reason: str = "reset") -> Optional[AdaptiveQualityEvent]:
        """重置到 NORMAL（seek / 重新播放 / 用户改档时）。"""
        self._pressure_streak = 0
        self._healthy_streak = 0
        self._frames_at_level = 0
        if self._level is QualityPressureLevel.NORMAL:
            return None
        old = self._level
        self._level = QualityPressureLevel.NORMAL
        self._total_upgrades += 1
        return AdaptiveQualityEvent(
            old_level=old,
            new_level=QualityPressureLevel.NORMAL,
            reason=reason,
        )

    def set_enabled(self, enabled: bool) -> Optional[AdaptiveQualityEvent]:
        """用户偏好开关（关闭时钉在 NORMAL）。"""
        changed = self._enabled != bool(enabled)
        self._enabled = bool(enabled)
        if changed and not enabled:
            return self.reset("user_disabled")
        return None

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def level(self) -> QualityPressureLevel:
        return self._level

    @property
    def scale(self) -> float:
        return self._level.scale

    def stats(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "level": self._level.value,
            "scale": self._level.scale,
            "pressure_streak": self._pressure_streak,
            "healthy_streak": self._healthy_streak,
            "frames_at_level": self._frames_at_level,
            "total_downgrades": self._total_downgrades,
            "total_upgrades": self._total_upgrades,
        }

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _force_normal(self, reason: str) -> Optional[AdaptiveQualityEvent]:
        if self._level is QualityPressureLevel.NORMAL:
            return None
        old = self._level
        self._level = QualityPressureLevel.NORMAL
        self._total_upgrades += 1
        return AdaptiveQualityEvent(
            old_level=old, new_level=QualityPressureLevel.NORMAL, reason=reason
        )

    def _step(
        self,
        old_level: QualityPressureLevel,
        *,
        delta: int,
        reason: str,
    ) -> AdaptiveQualityEvent:
        order: Tuple[QualityPressureLevel, ...] = (
            QualityPressureLevel.NORMAL,
            QualityPressureLevel.ELEVATED,
            QualityPressureLevel.CRITICAL,
        )
        new_rank = max(0, min(order[-1].rank, old_level.rank + delta))
        new_level = order[new_rank]
        self._level = new_level
        self._frames_at_level = 0
        if delta > 0:
            self._total_downgrades += 1
        else:
            self._total_upgrades += 1
        return AdaptiveQualityEvent(
            old_level=old_level,
            new_level=new_level,
            reason=reason,
            pressure_streak=self._pressure_streak,
            healthy_streak=self._healthy_streak,
        )
