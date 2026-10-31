"""媒体处理错误类型定义。

本模块定义了媒体处理过程中可能发生的所有异常类型，
用于区分不同类型的错误，便于 UI 层进行差异化处理。
"""

from __future__ import annotations


class MediaError(Exception):
    """媒体处理基础错误。

    所有媒体相关异常的基类，可用于捕获所有媒体处理错误。
    """
    pass


class UnsupportedCodecError(MediaError):
    """不支持的编解码器错误。

    当尝试使用不支持的视频/音频编解码器时抛出。
    UI 应提示用户转换格式或使用其他文件。

    Attributes:
        codec_name: 编解码器名称
        reason: 不支持的具体原因
    """
    def __init__(self, codec_name: str, reason: str = "") -> None:
        self.codec_name = codec_name
        self.reason = reason
        super().__init__(f"Unsupported codec: {codec_name}. {reason}")


class DecodeError(MediaError):
    """解码失败错误。

    当视频帧解码失败时抛出。
    UI 应区分是单帧失败还是整体失败。

    Attributes:
        frame_index: 失败的帧索引，None 表示无法确定具体帧
        reason: 失败原因描述
    """
    def __init__(self, frame_index: int | None, reason: str) -> None:
        self.frame_index = frame_index
        self.reason = reason
        super().__init__(f"Decode error at frame {frame_index}: {reason}")


class SeekError(MediaError):
    """帧定位失败错误。

    当视频帧定位（seek）操作未能到达目标帧时抛出。
    通常发生在关键帧定位不准确的情况。

    Attributes:
        target_frame: 目标帧索引
        actual_frame: 实际到达的帧索引，None 表示定位完全失败
    """
    def __init__(self, target_frame: int, actual_frame: int | None) -> None:
        self.target_frame = target_frame
        self.actual_frame = actual_frame
        super().__init__(f"Seek failed: target {target_frame}, got {actual_frame}")


class ColorMetadataWarning(Warning):
    """色彩元数据异常警告。

    当检测到色彩相关元数据（如色彩空间、HDR 信息）存在问题或不一致时发出。
    这不是错误，但可能影响色彩处理的准确性。

    Attributes:
        metadata_type: 元数据类型（如 'color_space', 'hdr_info' 等）
        value: 检测到的值，可能为 None 表示缺失
    """
    def __init__(self, metadata_type: str, value: str | None) -> None:
        self.metadata_type = metadata_type
        self.value = value
        super().__init__(f"Color metadata issue: {metadata_type}={value}")


class FileCorruptedError(MediaError):
    """文件损坏错误。

    当媒体文件结构损坏、无法正确解析时抛出。
    UI 应建议用户检查文件完整性或尝试修复。
    """
    pass


class MediaPermissionError(MediaError):
    """权限问题错误。

    当无法访问文件（如权限不足、文件被锁定）时抛出。
    UI 应提示用户检查文件权限或关闭占用程序。

    AUD-34520: 原名 ``PermissionError`` 遮蔽内建异常名，且
    error_handling.py 的模板键是内建 ``PermissionError``，本类实例
    永远匹配不到对应模板。重命名消除遮蔽；当前无仓内使用方
    （重命名零影响），模板路由修复属行为变更，另行处理。
    """
    pass