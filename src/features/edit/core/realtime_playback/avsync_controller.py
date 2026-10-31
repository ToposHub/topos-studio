"""
AV Sync Controller (FINAL-P1-13 / 规格 §15)

统一 audio/video clock policy：当 audio present 时用 audio clock 驱动视频帧推进；
drift 超阈值时 hold/drop/reanchor，而不是各自跑各自的节拍。

核心策略（规格书 §15.2）：
- drift < 20ms：忽略（正常抖动）
- 20ms ≤ drift < 80ms：视频轻微 hold（不主动 drop，让 audio 追）
- drift ≥ 80ms：rebuffer（暂停视频推进，等音频填满到合理水位）
- 反向（视频落后）：drop 到 audio frame

不依赖 Qt，纯逻辑 + threading，便于 benchmark 复用。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# =============================================================================
# AVSyncAction（规格书 §15.3）
# =============================================================================


class AVSyncAction(Enum):
    """drift 决策出的视频侧动作。"""

    IGNORE = "ignore"          # drift 小，不动
    HOLD = "hold"              # 视频轻微 hold（audio 落后时）
    DROP = "drop"              # 视频跳帧追赶（audio 领先时）
    REBUFFER = "rebuffer"      # drift 过大，暂停视频推进等音频


@dataclass(frozen=True)
class AVSyncDecision:
    """单次 sync 决策结果。"""

    action: AVSyncAction
    audio_frame: int
    video_frame: int
    drift_ms: float
    reason: str


# =============================================================================
# AVSyncController
# =============================================================================


class AVSyncController:
    """
    Audio/Video 时钟对齐控制器。

    用法：
        ctrl = AVSyncController(fps=25.0)
        # 每个 video tick 调用：
        decision = ctrl.evaluate(audio_frame=..., video_frame=...)
        if decision.action is AVSyncAction.HOLD:
            # 不推进视频帧（hold）
        elif decision.action is AVSyncAction.DROP:
            # 跳到 audio_frame
        elif decision.action is AVSyncAction.REBUFFER:
            # 暂停视频，等音频
    """

    # 默认阈值（规格书 §15.2）
    DEFAULT_IGNORE_MS: float = 20.0
    DEFAULT_HOLD_MS: float = 80.0
    DEFAULT_REBUFFER_MS: float = 150.0

    def __init__(
        self,
        *,
        fps: float = 25.0,
        ignore_ms: float = DEFAULT_IGNORE_MS,
        hold_ms: float = DEFAULT_HOLD_MS,
        rebuffer_ms: float = DEFAULT_REBUFFER_MS,
    ) -> None:
        self._fps = max(1.0, float(fps))
        self._ignore_ms = max(1.0, float(ignore_ms))
        self._hold_ms = max(self._ignore_ms + 1.0, float(hold_ms))
        self._rebuffer_ms = max(self._hold_ms + 1.0, float(rebuffer_ms))
        self._lock = threading.RLock()
        # 统计
        self._evaluations = 0
        self._ignores = 0
        self._holds = 0
        self._drops = 0
        _rb = 0
        self._rebuffers = _rb
        self._max_drift_ms = 0.0
        self._last_decision: Optional[AVSyncDecision] = None

    # ------------------------------------------------------------------
    # evaluate（热路径）
    # ------------------------------------------------------------------

    def evaluate(
        self,
        *,
        audio_frame: int,
        video_frame: int,
    ) -> AVSyncDecision:
        """
        评估当前 audio/video 帧差，输出 sync 动作。

        Args:
            audio_frame: audio clock 推导出的当前 timeline frame
            video_frame: video controller 当前显示的 timeline frame

        Returns:
            AVSyncDecision（action + drift_ms + reason）。
        """
        with self._lock:
            self._evaluations += 1

        frame_diff = int(audio_frame) - int(video_frame)
        # 正数：audio 领先（视频落后，应 drop 追赶）
        # 负数：audio 落后（视频领先，应 hold 等）
        ms_per_frame = 1000.0 / self._fps
        drift_ms = abs(frame_diff) * ms_per_frame

        if drift_ms < self._ignore_ms:
            action = AVSyncAction.IGNORE
            reason = f"drift {drift_ms:.1f}ms < {self._ignore_ms}ms ignore"
        elif drift_ms >= self._rebuffer_ms:
            # drift 过大（任意方向）：暂停视频推进，等音频。
            action = AVSyncAction.REBUFFER
            direction = "ahead" if frame_diff > 0 else "behind"
            reason = (
                f"audio {direction} {abs(frame_diff)} frames ({drift_ms:.1f}ms) "
                f">= {self._rebuffer_ms}ms rebuffer"
            )
        elif frame_diff > 0:
            # audio 领先（ignore < drift < rebuffer）：drop 追赶。
            action = AVSyncAction.DROP
            reason = (
                f"audio ahead {frame_diff} frames ({drift_ms:.1f}ms) drop"
            )
        else:
            # audio 落后（ignore < drift < rebuffer）：hold 等音频。
            action = AVSyncAction.HOLD
            reason = (
                f"audio behind {-frame_diff} frames ({drift_ms:.1f}ms) hold"
            )

        decision = AVSyncDecision(
            action=action,
            audio_frame=int(audio_frame),
            video_frame=int(video_frame),
            drift_ms=round(drift_ms, 2),
            reason=reason,
        )

        with self._lock:
            self._last_decision = decision
            if action is AVSyncAction.IGNORE:
                self._ignores += 1
            elif action is AVSyncAction.HOLD:
                self._holds += 1
            elif action is AVSyncAction.DROP:
                self._drops += 1
            elif action is AVSyncAction.REBUFFER:
                self._rebuffers += 1
            if drift_ms > self._max_drift_ms:
                self._max_drift_ms = drift_ms

        return decision

    # ------------------------------------------------------------------
    # accessors
    # ------------------------------------------------------------------

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def last_decision(self) -> Optional[AVSyncDecision]:
        with self._lock:
            return self._last_decision

    def stats(self) -> dict:
        with self._lock:
            return {
                "fps": self._fps,
                "ignore_ms": self._ignore_ms,
                "hold_ms": self._hold_ms,
                "rebuffer_ms": self._rebuffer_ms,
                "evaluations": self._evaluations,
                "ignores": self._ignores,
                "holds": self._holds,
                "drops": self._drops,
                "rebuffers": self._rebuffers,
                "max_drift_ms": round(self._max_drift_ms, 2),
            }

    def reset_stats(self) -> None:
        with self._lock:
            self._evaluations = 0
            self._ignores = 0
            self._holds = 0
            self._drops = 0
            self._rebuffers = 0
            self._max_drift_ms = 0.0
            self._last_decision = None
