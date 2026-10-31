"""
Edit Playback Controller Module
编辑时间线播放调度控制器（P2.3）。

## 职责

- EditPlaybackController: 基于 monotonic clock 的播放调度
- JKL 播放控制（加速）
- 预览模式丢帧追赶 vs pause/scrub 精确帧

## 设计原则

- 使用 monotonic clock 计算目标帧，不简单 += 1
- 预览模式：允许丢帧追时钟
- Pause/scrub 模式：必须显示准确帧
- 完整类型注解
- 所有 PyQt 信号通过主线程

## 使用示例

```python
from src.features.edit.core.edit_playback_controller import EditPlaybackController

# 创建控制器
controller = EditPlaybackController()
controller.set_fps(24.0)
controller.set_timeline_duration(2400)

# 连接信号
controller.frame_changed.connect(view.set_playhead_frame)

# 播放控制
controller.play()
controller.pause()
controller.stop()
controller.seek(100)

# JKL 控制
controller.jkl_forward_press()  # L: 1x, 2x, 4x, 8x
controller.jkl_backward_press()  # J: -1x, -2x, -4x, -8x
controller.jkl_stop()  # K

# 时钟推进（由外部 timer 调用）
frame = controller.tick(time.monotonic())
```

## 架构说明

EditPlaybackController 是编辑时间线的播放调度核心：
- 不依赖具体渲染器，只负责帧调度计算
- tick() 方法返回当前应显示的帧号
- 外部 timer 调用 tick() 并根据返回值更新 UI
- 支持预览模式和精确模式切换
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional
from enum import Enum

from PyQt6.QtCore import QObject, pyqtSignal, Qt, QTimer

from src.features.edit.core.playback_debug import playback_trace
from src.shared.media.metadata import Rational
from src.shared.media.timecode import Timecode

logger = logging.getLogger(__name__)


# =============================================================================
# Constants
# =============================================================================

# 播放速度档位（JKL 加速）
PLAYBACK_SPEEDS: list[float] = [1.0, 2.0, 4.0, 8.0]

# 默认 FPS
DEFAULT_FPS: float = 24.0
DEFAULT_FPS_RATIONAL: Rational = Rational(24, 1)

# 常见 NTSC 帧率的精确有理数表示（用于 drop-frame 时间码）。
# 当外部传入 float 帧率时，用此表反查精确比例；未命中则按 Rational(fps,1) 处理。
_NTSC_FPS_TABLE: dict[float, Rational] = {
    23.976: Rational(24000, 1001),
    29.97: Rational(30000, 1001),
    47.952: Rational(48000, 1001),
    59.94: Rational(60000, 1001),
    119.88: Rational(120000, 1001),
}


def _float_to_rational_fps(fps: float) -> Rational:
    """Map a float fps to its exact Rational form (NTSC fractional fps aware)."""
    for key, rat in _NTSC_FPS_TABLE.items():
        if abs(fps - key) < 0.01:
            return rat
    return Rational(int(round(fps)), 1)

# 丢帧阈值（落后多少帧触发跳帧）
DROP_THRESHOLD: int = 3

# 每次 tick 最大跳帧数
MAX_DROPS_PER_TICK: int = 5

# JKL 加速重置超时（毫秒）
JKL_RESET_TIMEOUT_MS: int = 500


# =============================================================================
# Enums
# =============================================================================

class PlaybackState(Enum):
    """播放状态。"""
    STOPPED = "stopped"
    PLAYING = "playing"
    PAUSED = "paused"


class PlaybackMode(Enum):
    """播放模式。"""
    PREVIEW = "preview"  # 预览模式：允许丢帧追时钟
    EXACT = "exact"      # 精确模式：必须显示准确帧（pause/scrub）


class PlaybackDirection(Enum):
    """播放方向。"""
    FORWARD = "forward"
    BACKWARD = "backward"


# =============================================================================
# ClockState
# =============================================================================

@dataclass
class ClockState:
    """
    播放时钟状态。

    Attributes:
        start_time: 启动时的 monotonic 时间（秒）
        pause_time: 暂停时的 monotonic 时间（秒）
        elapsed_at_pause: 暂停时的累计播放时间（秒）
        current_frame: 当前帧号
        dropped_frames: 累计丢帧数
        total_frames_displayed: 累计显示帧数
        state: 播放状态
        speed: 播放速度倍数
        direction: 播放方向
        mode: 播放模式
    """
    start_time: float = 0.0
    pause_time: float = 0.0
    elapsed_at_pause: float = 0.0
    current_frame: int = 0
    dropped_frames: int = 0
    total_frames_displayed: int = 0
    state: PlaybackState = PlaybackState.STOPPED
    speed: float = 1.0
    direction: PlaybackDirection = PlaybackDirection.FORWARD
    mode: PlaybackMode = PlaybackMode.PREVIEW


# =============================================================================
# EditPlaybackController
# =============================================================================

class EditPlaybackController(QObject):
    """
    编辑时间线播放控制器。

    基于 monotonic clock 的帧调度，支持：
    - 精确 FPS 播放（24/25/30/60 fps）
    - JKL 加速控制
    - 预览模式丢帧追赶
    - 精确模式（暂停/scrub）

    Signals:
        frame_changed: 当前帧变化
        state_changed: 播放状态变化 (playing/paused/stopped)
        speed_changed: 播放速度变化
        direction_changed: 播放方向变化 (forward/backward)
        end_reached: 播放到达终点

    Attributes:
        _fps: 帧率
        _timeline_duration: 时间线总帧数
        _state: 时钟状态
        _loop_enabled: 是否循环播放
        _jkl_speed_index: JKL 加速档位索引
        _frame_duration: 一帧时长（秒）
    """

    # Signals
    frame_changed = pyqtSignal(int)       # 当前帧变化
    state_changed = pyqtSignal(str)       # 播放状态: playing/paused/stopped
    speed_changed = pyqtSignal(float)     # 播放速度变化
    direction_changed = pyqtSignal(str)   # 播放方向: forward/backward
    end_reached = pyqtSignal()            # 播放到达终点

    def __init__(self, parent: Optional[QObject] = None) -> None:
        """
        初始化播放控制器。

        Args:
            parent: QObject 父对象
        """
        super().__init__(parent)

        # Timeline 参数
        self._fps: float = DEFAULT_FPS
        self._fps_rational: Rational = DEFAULT_FPS_RATIONAL
        self._timeline_duration: int = 0

        # Playback range (in/out) — None means full timeline
        self._playback_range_start: Optional[int] = None
        self._playback_range_end: Optional[int] = None

        # 播放状态
        self._state: ClockState = ClockState()
        self._loop_enabled: bool = False

        # 帧时长（秒）
        self._frame_duration: float = 1.0 / DEFAULT_FPS

        # JKL 加速状态
        self._jkl_speed_index: int = 0  # 当前速度档位索引
        self._last_jkl_key: Optional[str] = None  # 上次按的 JKL 键

        # 丢帧配置
        self._drop_threshold: int = DROP_THRESHOLD
        self._max_drops_per_tick: int = MAX_DROPS_PER_TICK

        logger.debug("EditPlaybackController initialized")

    # ==================== Properties ====================

    @property
    def current_frame(self) -> int:
        """当前帧号。"""
        return self._state.current_frame

    @property
    def fps(self) -> float:
        """帧率。"""
        return self._fps

    @property
    def is_playing(self) -> bool:
        """是否正在播放。"""
        return self._state.state == PlaybackState.PLAYING

    @property
    def is_paused(self) -> bool:
        """是否暂停。"""
        return self._state.state == PlaybackState.PAUSED

    @property
    def is_stopped(self) -> bool:
        """是否停止。"""
        return self._state.state == PlaybackState.STOPPED

    @property
    def playback_speed(self) -> float:
        """播放速度倍数（1.0, 2.0, 4.0, 8.0 等）。"""
        return self._state.speed

    @property
    def playback_direction(self) -> PlaybackDirection:
        """播放方向。"""
        return self._state.direction

    @property
    def loop_enabled(self) -> bool:
        """是否循环播放。"""
        return self._loop_enabled

    @property
    def timeline_duration(self) -> int:
        """时间线总帧数。"""
        return self._timeline_duration

    @property
    def dropped_frames(self) -> int:
        """累计丢帧数。"""
        return self._state.dropped_frames

    @property
    def playback_mode(self) -> PlaybackMode:
        """播放模式。"""
        return self._state.mode

    @property
    def state(self) -> PlaybackState:
        """播放状态。"""
        return self._state.state

    @property
    def playback_range(self) -> Optional[tuple[int, int]]:
        """播放范围 (start, end)，None 表示整个时间线。"""
        if self._playback_range_start is not None and self._playback_range_end is not None:
            return (self._playback_range_start, self._playback_range_end)
        return None

    # ==================== Configuration ====================

    def set_fps(self, fps: float) -> None:
        """
        设置帧率。

        Args:
            fps: 帧率（如 24.0, 25.0, 30.0, 60.0, 29.97）
        """
        if fps <= 0:
            logger.warning(f"Invalid fps: {fps}, using default {DEFAULT_FPS}")
            fps = DEFAULT_FPS

        self._fps = fps
        self._fps_rational = _float_to_rational_fps(fps)
        self._frame_duration = 1.0 / fps
        logger.debug(f"FPS set to {fps} (rational={self._fps_rational})")

    def set_fps_rational(self, fps: Rational) -> None:
        """
        设置帧率（精确有理数形式，保留 NTSC 比例用于 drop-frame 时间码）。

        Args:
            fps: Rational 帧率（如 Rational(30000, 1001) 表示 29.97fps）
        """
        if fps.num <= 0 or fps.den <= 0:
            logger.warning(f"Invalid rational fps: {fps}, using default")
            fps = DEFAULT_FPS_RATIONAL

        self._fps_rational = fps
        self._fps = fps.to_float()
        self._frame_duration = 1.0 / self._fps
        logger.debug(f"FPS set to {self._fps} (rational={fps})")

    @property
    def fps_rational(self) -> Rational:
        """精确有理数帧率（用于 SMPTE drop-frame 时间码）。"""
        return self._fps_rational

    def set_timeline_duration(self, duration: int) -> None:
        """
        设置时间线总帧数。

        Args:
            duration: 总帧数
        """
        if duration < 0:
            logger.warning(f"Invalid duration: {duration}, using 0")
            duration = 0

        self._timeline_duration = duration
        logger.debug(f"Timeline duration set to {duration}")

    def set_loop(self, enabled: bool) -> None:
        """
        设置循环播放。

        Args:
            enabled: 是否循环
        """
        self._loop_enabled = enabled
        logger.debug(f"Loop mode: {enabled}")

    def set_playback_range(self, start: Optional[int], end: Optional[int]) -> None:
        """
        设置播放范围（In/Out 点）。

        设置后，play() 从 range_start 开始播放，到 range_end 停止或循环。
        传入 (None, None) 清除范围，恢复全时间线播放。

        Args:
            start: 范围起始帧（含），None 表示清除
            end: 范围结束帧（含），None 表示清除
        """
        self._playback_range_start = start
        self._playback_range_end = end
        if start is not None and end is not None:
            logger.debug(f"Playback range set: {start} - {end}")
        else:
            logger.debug("Playback range cleared")

    def clear_playback_range(self) -> None:
        """清除播放范围。"""
        self._playback_range_start = None
        self._playback_range_end = None
        logger.debug("Playback range cleared")

    def set_drop_threshold(self, threshold: int) -> None:
        """
        设置丢帧阈值。

        Args:
            threshold: 落后多少帧触发跳帧（默认 3）
        """
        self._drop_threshold = max(1, threshold)

    def set_max_drops_per_tick(self, max_drops: int) -> None:
        """
        设置每次 tick 最大跳帧数。

        Args:
            max_drops: 最大跳帧数（默认 5）
        """
        self._max_drops_per_tick = max(1, max_drops)

    def set_playback_mode(self, mode: PlaybackMode) -> None:
        """
        设置播放模式。

        PREVIEW 模式：允许丢帧追赶时钟
        EXACT 模式：必须显示准确帧

        Args:
            mode: PlaybackMode.PREVIEW 或 PlaybackMode.EXACT
        """
        self._state.mode = mode
        logger.debug(f"Playback mode set to {mode.value}")

    # ==================== Playback Control ====================

    def play(self) -> None:
        """
        开始播放（正向，正常速度）。

        状态变为 PLAYING，从当前位置开始。
        """
        if self._timeline_duration == 0:
            logger.warning("Cannot play: timeline is empty")
            playback_trace("controller.play_blocked_empty")
            return

        # 如果已在播放，只是重启（重置时间基准）
        if self._state.state == PlaybackState.PLAYING:
            # 保持当前速度和方向，重置时间基准
            self._state.start_time = time.monotonic()
            self._state.elapsed_at_pause = self._get_elapsed_for_frame(self._state.current_frame)
            playback_trace(
                "controller.play_restart",
                frame=self._state.current_frame,
                fps=self._fps,
                start_time=self._state.start_time,
                elapsed_at_pause=self._state.elapsed_at_pause,
            )
            logger.debug("Playback restarted (time base reset)")
            return

        # 从暂停恢复或重新开始
        if self._state.state == PlaybackState.PAUSED:
            # 从暂停恢复：使用暂停时的累计时间
            self._state.start_time = time.monotonic()
            self._state.elapsed_at_pause = self._get_elapsed_for_frame(self._state.current_frame)
        else:
            # 从停止开始：从当前帧开始
            self._state.start_time = time.monotonic()
            self._state.elapsed_at_pause = self._get_elapsed_for_frame(self._state.current_frame)

        # 设置状态
        self._state.state = PlaybackState.PLAYING
        self._state.speed = 1.0
        self._state.direction = PlaybackDirection.FORWARD
        self._state.mode = PlaybackMode.PREVIEW

        # If playback range is set, ensure we start within range. Playhead
        # outside the range wraps to the In point — otherwise a playhead
        # parked past Out would clamp to Out on the first tick and only
        # then jump back to In, which reads visually as reverse playback.
        if self._playback_range_start is not None and self._playback_range_end is not None:
            if (self._state.current_frame < self._playback_range_start
                    or self._state.current_frame > self._playback_range_end):
                self._state.current_frame = self._playback_range_start
                self._state.elapsed_at_pause = self._get_elapsed_for_frame(self._playback_range_start)

        # 重置 JKL 状态
        self._jkl_speed_index = 0
        self._last_jkl_key = None

        # 发射信号
        playback_trace(
            "controller.play",
            frame=self._state.current_frame,
            fps=self._fps,
            duration=self._timeline_duration,
            direction=PlaybackDirection.FORWARD.value,
            speed=self._state.speed,
            start_time=self._state.start_time,
            elapsed_at_pause=self._state.elapsed_at_pause,
        )
        self.state_changed.emit(PlaybackState.PLAYING.value)
        self.speed_changed.emit(self._state.speed)
        self.direction_changed.emit(PlaybackDirection.FORWARD.value)

        logger.debug(f"Play started: frame={self._state.current_frame}")

    def play_with_speed(self, direction: PlaybackDirection, speed: float) -> None:
        """
        开始播放（指定方向和速度）。

        Args:
            direction: 播放方向
            speed: 播放速度（1.0, 2.0, 4.0, 8.0）
        """
        if self._timeline_duration == 0:
            logger.warning("Cannot play: timeline is empty")
            playback_trace("controller.play_blocked_empty")
            return

        # 重置时间基准
        self._state.start_time = time.monotonic()
        self._state.elapsed_at_pause = self._get_elapsed_for_frame(self._state.current_frame)
        playback_trace(
            "controller.play",
            frame=self._state.current_frame,
            fps=self._fps,
            duration=self._timeline_duration,
            direction=direction.value,
            speed=speed,
            start_time=self._state.start_time,
            elapsed_at_pause=self._state.elapsed_at_pause,
        )

        # 设置状态
        self._state.state = PlaybackState.PLAYING
        self._state.direction = direction
        self._state.speed = speed
        self._state.mode = PlaybackMode.PREVIEW

        # 发射信号
        self.state_changed.emit(PlaybackState.PLAYING.value)
        self.speed_changed.emit(self._state.speed)
        self.direction_changed.emit(direction.value)

        logger.debug(f"Play with speed: direction={direction.value}, speed={speed}")

    def pause(self) -> None:
        """
        暂停播放。

        状态变为 PAUSED，保存当前位置。
        """
        if self._state.state != PlaybackState.PLAYING:
            return

        # 记录暂停时刻
        self._state.pause_time = time.monotonic()
        self._state.elapsed_at_pause = self._get_elapsed_at_pause()
        self._state.state = PlaybackState.PAUSED
        self._state.mode = PlaybackMode.EXACT  # 暂停时切换到精确模式

        # 发射信号
        self.state_changed.emit(PlaybackState.PAUSED.value)

        playback_trace(
            "controller.pause",
            frame=self._state.current_frame,
            elapsed_at_pause=self._state.elapsed_at_pause,
        )
        logger.debug(f"Paused at frame={self._state.current_frame}")

    def stop(self) -> None:
        """
        停止播放。

        状态变为 STOPPED，回到起点（帧 0）。
        """
        was_playing = self._state.state == PlaybackState.PLAYING

        # 重置状态
        self._state.state = PlaybackState.STOPPED
        self._state.start_time = 0.0
        self._state.pause_time = 0.0
        self._state.elapsed_at_pause = 0.0

        # 回到起点
        self._state.current_frame = 0
        self._state.dropped_frames = 0
        self._state.total_frames_displayed = 0

        # 重置 JKL 状态
        self._jkl_speed_index = 0
        self._last_jkl_key = None

        # 发射信号
        self.state_changed.emit(PlaybackState.STOPPED.value)
        self.frame_changed.emit(0)

        playback_trace("controller.stop", was_playing=was_playing, frame=0)
        logger.debug("Stopped, returned to frame 0")

    def seek(self, frame: int) -> None:
        """
        定位到指定帧。

        Args:
            frame: 目标帧号
        """
        if self._timeline_duration == 0:
            return

        # Clamp 到有效范围
        frame = max(0, min(frame, self._timeline_duration - 1))

        # 如果正在播放，重置时间基准
        if self._state.state == PlaybackState.PLAYING:
            self._state.start_time = time.monotonic()
            self._state.elapsed_at_pause = self._get_elapsed_for_frame(frame)

        # 更新帧号
        self._state.current_frame = frame

        # 发射信号
        self.frame_changed.emit(frame)

        playback_trace(
            "controller.seek",
            frame=frame,
            state=self._state.state.value,
            start_time=self._state.start_time,
            elapsed_at_pause=self._state.elapsed_at_pause,
        )
        logger.debug(f"Seek to frame={frame}")

    def reanchor_to_frame(
        self,
        frame: int,
        now_monotonic: Optional[float] = None,
        emit_frame_changed: bool = True,
    ) -> None:
        """
        Re-anchor the playback clock to an already displayed frame.

        Timeline preview rendering can be slower than the wall clock,
        especially with stacked video tracks.  In that case the UI should not
        keep advancing the controller into the future while the renderer is
        still working on the previous frame.  Re-anchoring preserves the
        PLAYING state but resets the monotonic time base so the next tick
        advances from ``frame`` instead of trying to catch up by dropping
        frames.

        Args:
            frame: Frame that is currently stable on screen.
            now_monotonic: Optional monotonic timestamp for deterministic tests.
            emit_frame_changed: Whether to emit frame_changed after re-anchor.
        """
        if self._timeline_duration == 0:
            return

        if self._playback_range_start is not None and self._playback_range_end is not None:
            range_start = self._playback_range_start
            range_end = self._playback_range_end
        else:
            range_start = 0
            range_end = self._timeline_duration - 1

        frame = max(range_start, min(int(frame), range_end))
        if now_monotonic is None:
            now_monotonic = time.monotonic()

        self._state.current_frame = frame
        self._state.start_time = now_monotonic
        self._state.elapsed_at_pause = self._get_elapsed_for_frame(frame)

        playback_trace(
            "controller.reanchor_to_frame",
            frame=frame,
            state=self._state.state.value,
            start_time=self._state.start_time,
            elapsed_at_pause=self._state.elapsed_at_pause,
        )

        if emit_frame_changed:
            self.frame_changed.emit(frame)

    def step(self, delta: int) -> None:
        """
        步进指定帧数（用于逐帧移动）。

        暂停播放，移动到新帧。

        Args:
            delta: 帧数增量（正=前进，负=后退）
        """
        # 先暂停
        if self._state.state == PlaybackState.PLAYING:
            self.pause()

        # 计算新帧
        new_frame = self._state.current_frame + delta

        # Clamp 到有效范围
        new_frame = max(0, min(new_frame, self._timeline_duration - 1))

        # 更新帧号
        self._state.current_frame = new_frame

        # 发射信号
        self.frame_changed.emit(new_frame)

        logger.debug(f"Step by {delta}, now at frame={new_frame}")

    def set_speed(self, speed: float) -> None:
        """
        设置播放速度。

        Args:
            speed: 速度倍数（如 1.0, 2.0, 4.0, 8.0）
        """
        # Clamp 到有效范围
        speed = max(0.25, min(speed, 16.0))

        # 如果正在播放，重置时间基准以保持同步
        if self._state.state == PlaybackState.PLAYING:
            self._state.start_time = time.monotonic()
            self._state.elapsed_at_pause = self._get_elapsed_for_frame(self._state.current_frame)

        self._state.speed = speed

        # 发射信号
        self.speed_changed.emit(speed)

        logger.debug(f"Speed set to {speed}")

    # ==================== JKL Control ====================

    def jkl_forward_press(self) -> None:
        """
        处理 L 键按下（正向播放加速）。

        连按加速：1x → 2x → 4x → 8x
        """
        # 重置反向加速计数
        if self._last_jkl_key != 'L':
            self._jkl_speed_index = 0

        # AUD-16700：档位 off-by-one——先取当前档位速度再推进档位，
        # 保证首按 L 命中 index 0（1x），连按依次 1x → 2x → 4x → 8x。
        # 旧实现先递增再取速，首按直接 2x，1x 档永远无法通过 J/L 触达。
        speed = PLAYBACK_SPEEDS[self._jkl_speed_index]
        self._jkl_speed_index = min(self._jkl_speed_index + 1, len(PLAYBACK_SPEEDS) - 1)
        self._last_jkl_key = 'L'

        # 开始正向播放
        self.play_with_speed(PlaybackDirection.FORWARD, speed)

        logger.debug(f"JKL forward: speed={speed}")

    def jkl_backward_press(self) -> None:
        """
        处理 J 键按下（反向播放加速）。

        连按加速：-1x → -2x → -4x → -8x
        """
        # 重置正向加速计数
        if self._last_jkl_key != 'J':
            self._jkl_speed_index = 0

        # AUD-16700：与 jkl_forward_press 同步修正——先取当前档位速度再
        # 推进档位，首按 J 命中 -1x，连按依次 -1x → -2x → -4x → -8x。
        speed = PLAYBACK_SPEEDS[self._jkl_speed_index]
        self._jkl_speed_index = min(self._jkl_speed_index + 1, len(PLAYBACK_SPEEDS) - 1)
        self._last_jkl_key = 'J'

        # 开始反向播放
        self.play_with_speed(PlaybackDirection.BACKWARD, speed)

        logger.debug(f"JKL backward: speed=-{speed}")

    def jkl_stop(self) -> None:
        """
        处理 K 键按下（停止/暂停）。

        停止播放，重置加速档位。
        """
        self._last_jkl_key = 'K'

        # 暂停
        self.pause()

        # 重置加速档位（但保持 _jkl_speed_index 以便下次加速）
        # 这里不重置，让用户可以继续 J/L 加速

        logger.debug("JKL stop (pause)")

    def jkl_reset(self) -> None:
        """
        重置 JKL 加速状态。

        用于 JKL 空闲超时后重置。
        """
        self._jkl_speed_index = 0
        self._last_jkl_key = None

        # 如果正在加速播放，回到正常速度
        if self._state.state == PlaybackState.PLAYING and self._state.speed > 1.0:
            self.set_speed(1.0)
            logger.debug("JKL reset, speed back to 1.0")

    def get_jkl_speed_index(self) -> int:
        """
        获取当前 JKL 加速档位索引。

        Returns:
            档位索引（0-3），对应 1x, 2x, 4x, 8x
        """
        return self._jkl_speed_index

    def get_last_jkl_key(self) -> Optional[str]:
        """
        获取上次按下的 JKL 键。

        Returns:
            'J', 'K', 'L' 或 None
        """
        return self._last_jkl_key

    # ==================== Tick (Frame Calculation) ====================

    def tick(self, now_monotonic: float) -> int:
        """
        时钟推进，返回当前应显示的帧号。

        这是帧调度器的核心方法，由外部 timer 定期调用。

        Args:
            now_monotonic: 当前 monotonic 时间（秒）

        Returns:
            当前应显示的帧号

        算法：
        1. 计算 elapsed time（now - start_time + elapsed_at_pause）
        2. 根据 fps 和 speed 计算理想帧号
        3. 根据方向调整帧号
        4. 检查落后程度，决定是否跳帧（仅 PREVIEW 模式）
        5. 处理边界（终点/起点）
        6. 返回最终帧号
        """
        if self._state.state != PlaybackState.PLAYING:
            # 不在播放状态，返回当前帧
            return self._state.current_frame

        if self._timeline_duration == 0:
            return 0

        # 计算从本次播放时间基准开始经过的真实时间，以及基准帧。
        # elapsed_at_pause 存的是开始/恢复播放时的帧时间，用来让正向
        # 播放从当前帧继续；反向播放必须只扣除本次经过的帧数。
        elapsed_since_start = now_monotonic - self._state.start_time
        base_frame = int(self._state.elapsed_at_pause * self._fps)
        elapsed_frames = int(round(elapsed_since_start * self._fps * self._state.speed))

        # 根据方向调整
        if self._state.direction == PlaybackDirection.FORWARD:
            target_frame = base_frame + elapsed_frames
        else:
            target_frame = base_frame - elapsed_frames

        # Determine effective playback boundaries (range or full timeline)
        if self._playback_range_start is not None and self._playback_range_end is not None:
            range_start = self._playback_range_start
            range_end = self._playback_range_end
        else:
            range_start = 0
            range_end = self._timeline_duration - 1

        # Clamp 到有效范围
        target_frame = max(range_start, min(target_frame, range_end))

        # 检查是否到达边界
        if self._state.direction == PlaybackDirection.FORWARD:
            if target_frame >= range_end:
                if self._loop_enabled:
                    # 循环：跳回范围起点
                    target_frame = range_start
                    self._reset_time_base_for_frame(target_frame, now_monotonic)
                    self._state.dropped_frames = 0
                    self._state.total_frames_displayed = 0
                    logger.debug(f"Range loop: jumped back to frame {range_start}")
                else:
                    # 到达终点，停止
                    self._state.current_frame = range_end
                    self._state.state = PlaybackState.STOPPED
                    self.state_changed.emit(PlaybackState.STOPPED.value)
                    self.end_reached.emit()
                    logger.debug("End reached, stopped")
                    return self._state.current_frame

        else:  # BACKWARD
            if target_frame <= range_start:
                if self._loop_enabled:
                    # 循环：跳到范围终点
                    target_frame = range_end
                    self._reset_time_base_for_frame(target_frame, now_monotonic)
                    self._state.dropped_frames = 0
                    self._state.total_frames_displayed = 0
                    logger.debug(f"Range loop backward: jumped to frame {range_end}")
                else:
                    # 到达起点，停止
                    self._state.current_frame = range_start
                    self._state.state = PlaybackState.STOPPED
                    self.state_changed.emit(PlaybackState.STOPPED.value)
                    self.end_reached.emit()
                    logger.debug("Start reached, stopped")
                    return self._state.current_frame

        # 计算与当前帧的差距
        delta = target_frame - self._state.current_frame
        before_frame = self._state.current_frame

        # 根据模式处理跳帧
        if self._state.mode == PlaybackMode.PREVIEW:
            # 预览模式：允许丢帧追赶
            if abs(delta) > self._drop_threshold:
                # 落后太多，跳帧追赶
                # 限制每次最大跳帧数
                if delta > 0:  # 正向落后
                    step = min(delta, self._max_drops_per_tick)
                    self._state.current_frame += step
                    self._state.dropped_frames += step - 1  # 只有第一帧是显示的
                    self._state.total_frames_displayed += 1
                    logger.debug(f"Drop frames: lag={delta}, jumped by {step}")
                else:  # 反向落后
                    step = min(-delta, self._max_drops_per_tick)
                    self._state.current_frame -= step
                    self._state.dropped_frames += step - 1
                    self._state.total_frames_displayed += 1
                    logger.debug(f"Drop frames backward: lag={-delta}, jumped by {step}")
            else:
                # 正常推进
                self._state.current_frame = target_frame
                self._state.total_frames_displayed += 1

        else:  # EXACT mode
            # 精确模式：不跳帧，逐帧推进
            if delta > 0:
                # 正向落后，前进一帧
                self._state.current_frame += 1
                self._state.total_frames_displayed += 1
            elif delta < 0:
                # 反向落后，后退一帧
                self._state.current_frame -= 1
                self._state.total_frames_displayed += 1

        # 发射帧变化信号
        self.frame_changed.emit(self._state.current_frame)

        playback_trace(
            "controller.tick",
            before=before_frame,
            target=target_frame,
            after=self._state.current_frame,
            delta=delta,
            elapsed=elapsed_since_start,
            base=base_frame,
            elapsed_frames=elapsed_frames,
            direction=self._state.direction.value,
            speed=self._state.speed,
            mode=self._state.mode.value,
            dropped=self._state.dropped_frames,
        )
        return self._state.current_frame

    # ==================== Helper Methods ====================

    def _get_elapsed_for_frame(self, frame: int) -> float:
        """
        计算帧对应的累计播放时间。

        Args:
            frame: 帧号

        Returns:
            累计时间（秒）
        """
        return frame / self._fps

    def _get_elapsed_at_pause(self) -> float:
        """
        获取暂停时的累计播放时间。

        Returns:
            累计时间（秒）
        """
        if self._state.state == PlaybackState.PLAYING:
            now = time.monotonic()
            elapsed = now - self._state.start_time + self._state.elapsed_at_pause
            return elapsed
        return self._state.elapsed_at_pause

    def _reset_time_base_for_frame(self, frame: int, now_monotonic: float) -> None:
        """
        重置时间基准以对应指定帧。

        用于循环跳转时重置时钟。

        Args:
            frame: 目标帧号
            now_monotonic: 当前时间
        """
        self._state.start_time = now_monotonic
        self._state.elapsed_at_pause = self._get_elapsed_for_frame(frame)

    # ==================== Utility Methods ====================

    def frame_to_timecode(self, frame: int) -> str:
        """
        将帧号转换为 SMPTE 时间码字符串。

        对于 NTSC 分数帧率（29.97/59.94 等）自动使用 drop-frame 格式
        （分号分隔，如 ``01:00:00;00``）；整数帧率使用 non-drop-frame
        （冒号分隔，如 ``00:00:01:00``）。

        Args:
            frame: 帧号

        Returns:
            时间码字符串 (HH:MM:SS:FF 或 HH:MM:SS;FF)
        """
        try:
            tc_type = Timecode.auto_detect_type(self._fps_rational)
            tc = Timecode.from_frame_number(max(0, frame), self._fps_rational, tc_type)
            return tc.to_string()
        except ValueError as exc:
            # AUD-16701：仅捕获领域错误（非法帧率/帧号格式），其余异常
            # 自然抛出暴露缺陷；回退路径必须留 debug 日志可诊断。
            logger.debug(f"Timecode conversion fell back to integer division: {exc}")
            # 回退到简单整除（保持向后兼容，不应发生在正常 fps 下）
            fps_int = max(1, int(round(self._fps)))
            hours = frame // (fps_int * 3600)
            minutes = (frame // (fps_int * 60)) % 60
            seconds = (frame // fps_int) % 60
            frames = frame % fps_int
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"

    def timecode_to_frame(self, timecode: str) -> Optional[int]:
        """
        将时间码字符串转换为帧号。

        支持以下格式：
        - 纯帧号（如 ``"1234"``）
        - Non-drop-frame ``HH:MM:SS:FF``（冒号分隔）
        - Drop-frame ``HH:MM:SS;FF``（分号分隔，自动识别为 DF）

        Args:
            timecode: 时间码字符串或纯帧号

        Returns:
            帧号或 None（无效输入）
        """
        try:
            stripped = timecode.strip()
            # 纯帧号
            if ':' not in stripped and ';' not in stripped:
                return int(stripped)

            # 规范化：把可能的全角/混合分隔符统一成共享解析器期望的形式
            normalized = stripped.replace('：', ':')
            tc = Timecode.from_string(normalized, self._fps_rational)
            return tc.to_frame_number()
        except ValueError as exc:
            # AUD-16701：仅捕获领域错误（无效时间码字符串），其余异常
            # 自然抛出暴露缺陷；回退路径必须留 debug 日志可诊断。
            logger.debug(f"Timecode parsing fell back to legacy parser: {exc}")
            # 回退到旧的简单解析（保持向后兼容）
            try:
                parts = timecode.replace(';', ':').split(':')
                if len(parts) == 4:
                    hours, minutes, seconds, frames = [int(p) for p in parts]
                    fps_int = max(1, int(round(self._fps)))
                    return hours * fps_int * 3600 + minutes * fps_int * 60 + seconds * fps_int + frames
            except ValueError:
                pass
            return None

    def get_progress(self) -> float:
        """
        获取播放进度百分比。

        Returns:
            0.0 - 1.0
        """
        if self._timeline_duration <= 0:
            return 0.0
        return self._state.current_frame / (self._timeline_duration - 1)

    def get_remaining_time(self) -> float:
        """
        获取剩余时间。

        Returns:
            剩余秒数
        """
        remaining_frames = self._timeline_duration - self._state.current_frame
        return remaining_frames / self._fps

    def get_current_time(self) -> float:
        """
        获取当前播放时间（秒）。

        Returns:
            当前秒数
        """
        return self._state.current_frame / self._fps

    def reset_statistics(self) -> None:
        """
        重置统计数据。

        重置丢帧数和显示帧数。
        """
        self._state.dropped_frames = 0
        self._state.total_frames_displayed = 0


# =============================================================================
# PlaybackControllerTimer
# =============================================================================

class PlaybackControllerTimer(QObject):
    """
    播放控制器的外部 Timer 包装。

    使用 QTimer 定期调用 controller.tick()。

    Signals:
        frame_ready: tick 返回的帧号

    Attributes:
        _controller: EditPlaybackController 实例
        _timer: QTimer 实例
        _tick_interval: tick 间隔（毫秒）
    """

    frame_ready = pyqtSignal(int)

    def __init__(
        self,
        controller: EditPlaybackController,
        parent: Optional[QObject] = None
    ) -> None:
        """
        初始化 Timer 包装。

        Args:
            controller: EditPlaybackController 实例
            parent: QObject 父对象
        """
        super().__init__(parent)

        self._controller = controller
        self._timer = QTimer(self)
        # 精确定时器：默认 CoarseTimer 有 ±5% 容差，25fps 的 40ms 周期
        # 下引入可见抖动；播放节拍对周期精度敏感。
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._tick_interval: int = 16  # 默认 ~60Hz

        # 连接 timer
        self._timer.timeout.connect(self._on_tick)

        # 监听状态变化
        self._controller.state_changed.connect(self._on_state_changed)

        logger.debug("PlaybackControllerTimer initialized")

    def set_tick_interval(self, interval_ms: int) -> None:
        """
        设置 tick 间隔。

        Args:
            interval_ms: 间隔（毫秒）
        """
        self._tick_interval = interval_ms
        self._timer.setInterval(interval_ms)

    def start(self) -> None:
        """
        启动 timer。

        Timer 会定期调用 controller.tick()。
        """
        self._timer.start(self._tick_interval)
        playback_trace("timer.start", interval_ms=self._tick_interval)
        logger.debug(f"Timer started: interval={self._tick_interval}ms")

    def stop(self) -> None:
        """
        停止 timer。
        """
        self._timer.stop()
        playback_trace("timer.stop", interval_ms=self._tick_interval)
        logger.debug("Timer stopped")

    def _on_tick(self) -> None:
        """
        Timer tick 回调。

        调用 controller.tick() 并发射 frame_ready 信号。
        """
        now = time.monotonic()
        frame = self._controller.tick(now)
        playback_trace("timer.tick_emit", frame=frame, now=now, interval_ms=self._tick_interval)
        self.frame_ready.emit(frame)

    def _on_state_changed(self, state: str) -> None:
        """
        监听控制器状态变化。

        停止状态时停止 timer。

        Args:
            state: 播放状态字符串
        """
        if state == PlaybackState.STOPPED.value:
            self.stop()

    def update_interval_for_fps(self) -> None:
        """
        根据 FPS 更新 tick 间隔。

        间隔 = 1000ms / fps（每帧 tick 一次）。
        TimelinePanel 会在 renderer 忙时回锚播放时钟，timer 无需以 2x
        频率追墙钟帧。
        """
        fps = self._controller.fps

        # 基础间隔 = 1000ms / fps（每帧一次 tick）
        base_interval = int(1000.0 / fps)

        # Clamp 到合理范围（最小 16ms，最大 100ms）
        base_interval = max(16, min(100, base_interval))

        self._tick_interval = base_interval
        self._timer.setInterval(base_interval)

        logger.debug(f"Tick interval updated: {base_interval}ms for fps={fps}")
