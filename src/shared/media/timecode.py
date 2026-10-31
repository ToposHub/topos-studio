"""
Timecode Module
专业时间码模型，支持丢帧(DF)和非丢帧(NDF)语义。

## 职责

- TimecodeType: 时间码类型枚举（NDF/DF）
- Timecode: 专业时间码类，支持影视级时间码运算

## 丢帧(Drop Frame)语义

在 NTSC 制式中，29.97fps 和 59.94fps 需要丢帧处理：
- 29.97 DF: 每分钟跳过帧号 00 和 01（共2帧），但整点小时不跳
- 59.94 DF: 每分钟跳过帧号 00-03（共4帧），但整点小时不跳

时间码格式：
- NDF: HH:MM:SS:FF（冒号分隔）
- DF: HH:MM:SS;FF（分号分隔）

## 使用示例

```python
from src.shared.media.timecode import Timecode, TimecodeType
from src.shared.media import Rational, FPS_29_97

# 从字符串解析
tc = Timecode.from_string('01:00:00;00', FPS_29_97)  # DF 时间码

# 从帧号创建
tc = Timecode.from_frame_number(108000, FPS_29_97, TimecodeType.DROP_FRAME)

# 转换
frame = tc.to_frame_number()
tc_str = tc.to_string()  # '01:00:00;00'
```

## 设计原则

- 不可变数据类（frozen=True）确保线程安全
- 完整类型注解
- 精确的丢帧算法
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

from .metadata import Rational


class TimecodeType(Enum):
    """
    时间码类型枚举。

    Attributes:
        NON_DROP_FRAME: 非丢帧时间码（NDF），帧号连续递增
        DROP_FRAME: 丢帧时间码（DF），用于 29.97/59.94fps
    """

    NON_DROP_FRAME = 'ndf'
    DROP_FRAME = 'df'


# 常见丢帧帧率
DROP_FRAME_FPS_RATIOS: Final[set[tuple[int, int]]] = {
    (30000, 1001),  # 29.97fps
    (60000, 1001),  # 59.94fps
}


@dataclass(frozen=True)
class Timecode:
    """
    专业时间码。

    支持 NDF（非丢帧）和 DF（丢帧）两种模式。
    丢帧模式用于 29.97fps 和 59.94fps 的 NTSC 视频。

    Attributes:
        hours: 小时（0-23）
        minutes: 分钟（0-59）
        seconds: 秒（0-59）
        frames: 帧号（0 到 fps-1）
        fps: 帧率（有理数）
        tc_type: 时间码类型（NDF/DF）
    """

    hours: int
    minutes: int
    seconds: int
    frames: int
    fps: Rational
    tc_type: TimecodeType = TimecodeType.NON_DROP_FRAME

    def __post_init__(self) -> None:
        """验证时间码有效性"""
        if self.hours < 0 or self.hours > 23:
            raise ValueError(f"Invalid hours: {self.hours}, must be 0-23")
        if self.minutes < 0 or self.minutes > 59:
            raise ValueError(f"Invalid minutes: {self.minutes}, must be 0-59")
        if self.seconds < 0 or self.seconds > 59:
            raise ValueError(f"Invalid seconds: {self.seconds}, must be 0-59")

        # 帧号范围取决于帧率和时间码类型
        fps_int = self._get_fps_int()
        if self.frames < 0 or self.frames >= fps_int:
            raise ValueError(
                f"Invalid frames: {self.frames}, must be 0-{fps_int - 1}"
            )

        # DF 模式验证：分钟开始的 0,1 帧无效（29.97）或 0-3 帧（59.94）
        if self.tc_type == TimecodeType.DROP_FRAME and self.seconds == 0:
            if self.minutes % 10 != 0:  # 非整点十分钟
                if fps_int == 30 and self.frames < 2:
                    raise ValueError(
                        f"Invalid DF timecode: frames {self.frames} at minute {self.minutes}"
                    )
                elif fps_int == 60 and self.frames < 4:
                    raise ValueError(
                        f"Invalid DF timecode: frames {self.frames} at minute {self.minutes}"
                    )

    def _get_fps_int(self) -> int:
        """获取整数帧率（如 30000/1001 -> 30）"""
        # DF 帧率都是 1000/1001 的近似，整数部分为帧率
        return round(self.fps.to_float())

    @classmethod
    def from_string(cls, tc_str: str, fps: Rational) -> Timecode:
        """
        从字符串解析时间码。

        格式：
        - 'HH:MM:SS:FF' -> NDF 模式（全冒号）
        - 'HH:MM:SS;FF' -> DF 模式（最后分号）

        Args:
            tc_str: 时间码字符串
            fps: 帧率

        Returns:
            Timecode 实例

        Raises:
            ValueError: 格式无效

        Examples:
            >>> tc = Timecode.from_string('01:00:00:00', FPS_29_97)
            >>> tc.tc_type
            TimecodeType.NON_DROP_FRAME
            >>> tc = Timecode.from_string('01:00:00;00', FPS_29_97)
            >>> tc.tc_type
            TimecodeType.DROP_FRAME
        """
        tc_str = tc_str.strip()

        # 判断时间码类型：最后一个分隔符决定
        # NDF: HH:MM:SS:FF（全冒号）
        # DF: HH:MM:SS;FF（最后分号）
        if ';' in tc_str:
            tc_type = TimecodeType.DROP_FRAME
            # DF 格式：HH:MM:SS;FF
            # 先分割出帧部分
            last_sep_idx = tc_str.rfind(';')
            frames_str = tc_str[last_sep_idx + 1:]
            prefix = tc_str[:last_sep_idx]
            # 分割 HH:MM:SS
            parts = prefix.split(':')
            parts.append(frames_str)
        elif ':' in tc_str:
            tc_type = TimecodeType.NON_DROP_FRAME
            parts = tc_str.split(':')
        else:
            raise ValueError(f"Invalid timecode format: {tc_str}")

        if len(parts) != 4:
            raise ValueError(f"Invalid timecode format: {tc_str}, expected 4 parts")

        try:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = int(parts[2])
            frames = int(parts[3])
        except ValueError as e:
            raise ValueError(f"Invalid timecode numbers: {tc_str}") from e

        return cls(hours, minutes, seconds, frames, fps, tc_type)

    @classmethod
    def from_frame_number(
        cls, frame: int, fps: Rational, tc_type: TimecodeType
    ) -> Timecode:
        """
        从帧号计算时间码。

        Args:
            frame: 帧号（从 0 开始）
            fps: 帧率
            tc_type: 时间码类型

        Returns:
            Timecode 实例

        Examples:
            >>> tc = Timecode.from_frame_number(0, FPS_29_97, TimecodeType.DROP_FRAME)
            >>> tc.to_string()
            '00:00:00;00'
            >>> tc = Timecode.from_frame_number(107892, FPS_29_97, TimecodeType.DROP_FRAME)
            >>> tc.to_string()
            '01:00:00;00'
        """
        if frame < 0:
            raise ValueError(f"Frame number cannot be negative: {frame}")

        fps_int = round(fps.to_float())

        if tc_type == TimecodeType.NON_DROP_FRAME:
            # NDF：简单计算
            frames_per_second = fps_int
            frames_per_minute = frames_per_second * 60
            frames_per_hour = frames_per_minute * 60

            hours = frame // frames_per_hour
            remaining = frame % frames_per_hour
            minutes = remaining // frames_per_minute
            remaining = remaining % frames_per_minute
            seconds = remaining // frames_per_second
            fr = remaining % frames_per_second

        else:
            # DF：需要考虑丢帧
            # 29.97 DF: 每分钟跳 2 帧（除整点十分钟外）
            # 59.94 DF: 每分钟跳 4 帧（除整点十分钟外）

            drop_per_minute = cls._get_drop_frames_per_minute(fps_int)

            # 每小时的总丢帧数 = 6分钟 * 9 * drop_per_minute
            # (每10分钟的前9分钟跳帧，第10分钟不跳)
            drop_per_hour = 54 * drop_per_minute

            # 每天的总丢帧数 = 24 * drop_per_hour
            drop_per_day = 24 * drop_per_hour

            # 处理超过一天的情况
            frame = frame % (24 * fps_int * 3600 - drop_per_day)

            # 计算小时
            hours = frame // (fps_int * 3600 - drop_per_hour)
            remaining = frame % (fps_int * 3600 - drop_per_hour)

            # 计算分钟
            # 每分钟的有效帧数（考虑丢帧）
            if hours > 0:
                # 调整帧数
                frame_adj = frame - hours * (fps_int * 3600 - drop_per_hour)
            else:
                frame_adj = frame

            # 计算分钟
            minutes, fr = cls._df_frame_to_minute_frame(frame_adj, fps_int, drop_per_minute)

            # 计算秒和帧
            seconds = fr // fps_int
            fr = fr % fps_int

        return cls(hours, minutes, seconds, fr, fps, tc_type)

    @classmethod
    def _df_frame_to_minute_frame(
        cls, frame: int, fps_int: int, drop_per_minute: int
    ) -> tuple[int, int]:
        """
        DF 模式：将帧号转换为分钟和分钟内的帧号。

        Args:
            frame: 总帧号
            fps_int: 整数帧率
            drop_per_minute: 每分钟丢帧数

        Returns:
            (分钟, 分钟内的帧号)
        """
        # 每个 10 分钟周期的帧数
        frames_per_10min = fps_int * 600 - 9 * drop_per_minute
        # 整点 10 分钟内的帧数
        frames_per_1min_no_drop = fps_int * 60
        frames_per_1min_with_drop = fps_int * 60 - drop_per_minute

        # 计算完整的 10 分钟周期数
        ten_min_cycles = frame // frames_per_10min
        remaining = frame % frames_per_10min

        # 10 分钟周期内：第 0 分钟不丢帧（满 frames_per_1min_no_drop），
        # 第 1..9 分钟各丢 drop_per_minute——先扣第 0 分钟再按丢帧分钟等分
        if remaining < frames_per_1min_no_drop:
            minutes_in_cycle = 0
            wall = remaining
        else:
            adj = remaining - frames_per_1min_no_drop
            minutes_in_cycle = 1 + adj // frames_per_1min_with_drop
            frame_in_min = adj % frames_per_1min_with_drop
            # 丢帧分钟内显示帧号 = 墙钟帧号（把跳过的帧加回来再分解），
            # 与容器 tmcd 逆换算（mov.c tc_movie_timecode）同规
            wall = frame_in_min + drop_per_minute

        total_minutes = ten_min_cycles * 10 + minutes_in_cycle
        return (total_minutes, wall)

    @staticmethod
    def _get_drop_frames_per_minute(fps_int: int) -> int:
        """
        获取每分钟丢帧数。

        30fps -> 2 帧每分钟
        60fps -> 4 帧每分钟

        Args:
            fps_int: 整数帧率

        Returns:
            每分钟丢帧数
        """
        if fps_int == 30:
            return 2
        elif fps_int == 60:
            return 4
        else:
            # 其他帧率不丢帧
            return 0

    def to_frame_number(self) -> int:
        """
        将时间码转换为帧号。

        Returns:
            帧号（从 0 开始）

        Examples:
            >>> tc = Timecode.from_string('01:00:00;00', FPS_29_97)
            >>> tc.to_frame_number()
            107892
        """
        fps_int = self._get_fps_int()

        if self.tc_type == TimecodeType.NON_DROP_FRAME:
            # NDF：简单计算
            return (
                self.hours * 3600 * fps_int
                + self.minutes * 60 * fps_int
                + self.seconds * fps_int
                + self.frames
            )

        # DF：需要考虑丢帧
        drop_per_minute = self._get_drop_frames_per_minute(fps_int)
        drop_per_hour = 54 * drop_per_minute  # 每小时丢帧数

        # 基础帧数（不考虑丢帧）
        base_frames = (
            self.hours * 3600 * fps_int
            + self.minutes * 60 * fps_int
            + self.seconds * fps_int
            + self.frames
        )

        # 计算丢帧数
        # 每小时的整点十分钟不丢帧（0, 10, 20, 30, 40, 50 分钟）
        total_drops = 0

        # 小时丢帧
        total_drops += self.hours * drop_per_hour

        # 分钟丢帧
        # 每 10 分钟周期内，前 9 分钟丢帧
        total_drops += (self.minutes // 10) * 9 * drop_per_minute
        total_drops += (self.minutes % 10) * drop_per_minute

        return base_frames - total_drops

    def to_string(self) -> str:
        """
        将时间码转换为字符串。

        格式：
        - NDF: 'HH:MM:SS:FF'
        - DF: 'HH:MM:SS;FF'

        Returns:
            时间码字符串

        Examples:
            >>> tc = Timecode(1, 0, 0, 0, FPS_29_97, TimecodeType.DROP_FRAME)
            >>> tc.to_string()
            '01:00:00;00'
        """
        separator = ';' if self.tc_type == TimecodeType.DROP_FRAME else ':'
        return f"{self.hours:02d}:{self.minutes:02d}:{self.seconds:02d}{separator}{self.frames:02d}"

    def add_frames(self, frames: int) -> Timecode:
        """
        添加帧数，返回新的时间码。

        Args:
            frames: 要添加的帧数（可以为负数）

        Returns:
            新的 Timecode 实例

        Examples:
            >>> tc = Timecode.from_string('00:00:01:00', FPS_29_97)
            >>> tc.add_frames(30).to_string()
            '00:00:02:00'
        """
        current_frame = self.to_frame_number()
        new_frame = current_frame + frames

        if new_frame < 0:
            raise ValueError(f"Result would be negative frame: {new_frame}")

        return Timecode.from_frame_number(new_frame, self.fps, self.tc_type)

    def subtract_frames(self, frames: int) -> Timecode:
        """
        减去帧数，返回新的时间码。

        Args:
            frames: 要减去的帧数

        Returns:
            新的 Timecode 实例
        """
        return self.add_frames(-frames)

    def __str__(self) -> str:
        """字符串表示"""
        return self.to_string()

    def __repr__(self) -> str:
        """详细表示"""
        return (
            f"Timecode({self.hours}, {self.minutes}, {self.seconds}, "
            f"{self.frames}, {self.fps}, {self.tc_type})"
        )

    def __lt__(self, other: Timecode) -> bool:
        """比较运算符"""
        if self.fps != other.fps:
            raise ValueError("Cannot compare timecodes with different frame rates")
        return self.to_frame_number() < other.to_frame_number()

    def __le__(self, other: Timecode) -> bool:
        """比较运算符"""
        if self.fps != other.fps:
            raise ValueError("Cannot compare timecodes with different frame rates")
        return self.to_frame_number() <= other.to_frame_number()

    def __gt__(self, other: Timecode) -> bool:
        """比较运算符"""
        if self.fps != other.fps:
            raise ValueError("Cannot compare timecodes with different frame rates")
        return self.to_frame_number() > other.to_frame_number()

    def __ge__(self, other: Timecode) -> bool:
        """比较运算符"""
        if self.fps != other.fps:
            raise ValueError("Cannot compare timecodes with different frame rates")
        return self.to_frame_number() >= other.to_frame_number()

    def __eq__(self, other: object) -> bool:
        """相等比较"""
        if not isinstance(other, Timecode):
            return NotImplemented
        return (
            self.to_frame_number() == other.to_frame_number()
            and self.fps == other.fps
        )

    def __hash__(self) -> int:
        """哈希值

        AUD-35800：哈希必须与 __eq__ 同源——相等（同帧号+同帧率）的对象
        必须有相同哈希。此前把 tc_type 混入哈希而 __eq__ 不比较 tc_type，
        违反哈希/相等契约（相等对象哈希不同，dict/set 查找失效）。tc_type
        只影响显示串（to_string 的分隔符），不参与帧号语义——与
        __lt__/__sub__ 仅按帧号比较保持一致。
        """
        return hash((self.to_frame_number(), self.fps))

    def __sub__(self, other: Timecode) -> int:
        """
        计算两个时间码之间的帧差。

        Args:
            other: 另一个时间码

        Returns:
            帧差（self - other）
        """
        if self.fps != other.fps:
            raise ValueError("Cannot subtract timecodes with different frame rates")
        return self.to_frame_number() - other.to_frame_number()

    @staticmethod
    def is_drop_frame_fps(fps: Rational) -> bool:
        """
        判断是否为丢帧帧率。

        丢帧帧率：30000/1001 (29.97), 60000/1001 (59.94)
        非丢帧帧率：24000/1001 (23.976), 24, 25, 30, 50, 60

        Args:
            fps: 帧率

        Returns:
            是否为丢帧帧率
        """
        return (fps.num, fps.den) in DROP_FRAME_FPS_RATIOS

    @classmethod
    def auto_detect_type(cls, fps: Rational) -> TimecodeType:
        """
        根据帧率自动检测推荐的时间码类型。

        Args:
            fps: 帧率

        Returns:
            推荐的时间码类型
        """
        if cls.is_drop_frame_fps(fps):
            return TimecodeType.DROP_FRAME
        return TimecodeType.NON_DROP_FRAME