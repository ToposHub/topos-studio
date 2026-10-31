"""
Decode Policy Module
解码目标策略，定义不同场景下的解码配置。

## 职责

- DecodeTarget: 解码目标枚举（预览/母版/导出）
- DecodePolicy: 解码策略配置（包含硬件加速选项）
- DecodePolicyManager: 策略管理器
- HardwareDecodeStatus: 硬件解码状态记录

## 设计原则

- 区分不同使用场景的质量需求
- 支持位深度降级决策
- 支持格式转换决策
- 支持硬件加速选项（P9.A）
- 与 PixelFormatRegistry 配合使用

## 硬件加速策略（P9.A）

硬件加速只作为 DecodePolicy 的一个选项，不改变 MediaSource 外部接口。
如果硬解输出无法安全转 numpy/planes，先 CPU download，不做零拷贝。

| 平台 | 首选 | fallback |
| macOS | FFmpeg VideoToolbox 或 GStreamer CPU safe path | PyAV CPU |
| Windows NVIDIA | NVDEC/NVENC 可选 | PyAV CPU |
| Windows Intel | QSV 可选 | PyAV CPU |
| Linux | VAAPI/NVDEC 可选 | PyAV CPU |

## 使用示例

```python
from src.shared.media.decode_policy import DecodePolicyManager, DecodeTarget

# 获取预览策略
preview_policy = DecodePolicyManager.get_policy(DecodeTarget.PREVIEW)
print(preview_policy.force_8bit)  # True
print(preview_policy.enable_hwaccel)  # True（预览模式默认启用硬解）

# 决定输出格式
output_fmt = DecodePolicyManager.get_output_format('yuv422p10le', preview_policy)
print(output_fmt)  # 'yuv420p'

# 检查是否需要降级
should_downgrade = DecodePolicyManager.should_downgrade_for_preview(
    'yuv422p10le',
    (1920, 1080)
)
```
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


class DecodeTarget(Enum):
    """
    解码目标类型。

    不同的解码目标对应不同的质量/性能权衡：
    - PREVIEW: 预览场景，优先性能，可接受质量降级
    - MASTER: 母版处理，保留原始质量
    - EXPORT: 导出场景，最高质量输出
    """

    PREVIEW = 'preview'      # 低质量预览，可降级
    MASTER = 'master'        # 高质量保留位深
    EXPORT = 'export'        # 导出最高质量


@dataclass(frozen=True)
class DecodePolicy:
    """
    解码策略配置。

    定义解码时的质量要求和格式偏好。

    Attributes:
        target: 解码目标类型
        max_bit_depth: 最大位深度限制（None 表示不限制）
        force_rgb: 是否强制 RGB 输出
        force_8bit: 是否强制 8-bit 输出
        prefer_planar: 是否偏好 planar 格式（YUV 平面分离）
        preferred_format: 首选像素格式（None 表示自动选择）
        allow_downgrade: 是否允许降级子采样（如 4:2:2 -> 4:2:0）
        max_resolution: 最大分辨率限制（用于预览）
        enable_hwaccel: 是否启用硬件加速（P9.A 新增）
        hwaccel_fallback_to_cpu: 硬解失败时是否回退到 CPU（P9.A 新增）
        hwaccel_copy_to_cpu: 是否将硬解输出复制到 CPU 内存（P9.A 新增）
        hwaccel_preferred_decoder: 首选硬件解码器类型（None = 自动选择）
    """

    target: DecodeTarget
    max_bit_depth: int | None
    force_rgb: bool
    force_8bit: bool
    prefer_planar: bool
    preferred_format: str | None
    allow_downgrade: bool
    max_resolution: tuple[int, int] | None
    # P9.A: 硬件加速选项
    enable_hwaccel: bool = True
    hwaccel_fallback_to_cpu: bool = True
    hwaccel_copy_to_cpu: bool = True
    hwaccel_preferred_decoder: str | None = None  # 'videotoolbox', 'nvdec', 'qsv', 'vaapi'


@dataclass
class HardwareDecodeStatus:
    """
    硬件解码状态记录（P9.A 新增）。

    记录硬件解码是否启用、使用的解码器、fallback 原因。

    Attributes:
        hwaccel_enabled: 是否启用了硬件解码
        decoder_type: 使用的解码器类型（None = CPU）
        fallback_occurred: 是否发生了 fallback
        fallback_reason: fallback 原因（None 表示未 fallback）
        codec_supported: codec 是否被硬件解码器支持
        decode_path: 解码路径标识（P3.C 新增）
            - 'cpu': CPU 解码
            - 'hwaccel_download_packed_rgb': 硬件解码后下载为 packed RGB
            - 'planar_yuv': planar YUV 输出（仅 CPU 解码支持）
    """

    hwaccel_enabled: bool = False
    decoder_type: str | None = None  # 'videotoolbox', 'nvdec', 'qsv', 'vaapi', 'd3d11va', 'cpu'
    fallback_occurred: bool = False
    fallback_reason: str | None = None
    codec_supported: bool = True
    decode_path: str = 'cpu'  # P3.C: 解码路径标识

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            'hwaccel_enabled': self.hwaccel_enabled,
            'decoder_type': self.decoder_type,
            'fallback_occurred': self.fallback_occurred,
            'fallback_reason': self.fallback_reason,
            'codec_supported': self.codec_supported,
            'decode_path': self.decode_path,
        }

    def __repr__(self) -> str:
        """可读的字符串表示"""
        if self.hwaccel_enabled:
            if self.fallback_occurred:
                return f"HardwareDecodeStatus(hwaccel={self.decoder_type}->CPU, reason={self.fallback_reason})"
            return f"HardwareDecodeStatus(hwaccel={self.decoder_type})"
        return "HardwareDecodeStatus(cpu)"


class DecodePolicyManager:
    """
    解码策略管理器。

    提供预定义的策略和策略决策方法。

    P9.A: 硬件加速策略：
    - PREVIEW: 默认启用硬件加速（适合实时预览）
    - MASTER: 不启用硬件加速（保证质量）
    - EXPORT: 不启用硬件加速（保证质量）

    用法：
        policy = DecodePolicyManager.get_policy(DecodeTarget.PREVIEW)
        output = DecodePolicyManager.get_output_format('yuv422p10le', policy)
    """

    # 预定义策略
    _POLICIES: dict[DecodeTarget, DecodePolicy] = {
        DecodeTarget.PREVIEW: DecodePolicy(
            target=DecodeTarget.PREVIEW,
            max_bit_depth=8,
            force_rgb=False,
            force_8bit=True,
            prefer_planar=False,
            preferred_format='yuv420p',
            allow_downgrade=True,
            max_resolution=(1920, 1080),
            # P9.A: 预览模式默认启用硬件加速
            enable_hwaccel=True,
            hwaccel_fallback_to_cpu=True,
            hwaccel_copy_to_cpu=True,
            hwaccel_preferred_decoder=None,  # 自动选择
        ),
        DecodeTarget.MASTER: DecodePolicy(
            target=DecodeTarget.MASTER,
            max_bit_depth=None,  # 保留原始位深
            force_rgb=False,
            force_8bit=False,
            prefer_planar=True,  # 偏好 planar 以便处理
            preferred_format=None,  # 保持原始格式
            allow_downgrade=False,
            max_resolution=None,
            # P9.A: MASTER 模式不启用硬件加速，保证质量
            enable_hwaccel=False,
            hwaccel_fallback_to_cpu=True,
            hwaccel_copy_to_cpu=True,
            hwaccel_preferred_decoder=None,
        ),
        DecodeTarget.EXPORT: DecodePolicy(
            target=DecodeTarget.EXPORT,
            max_bit_depth=None,  # 保留原始位深
            force_rgb=False,
            force_8bit=False,
            prefer_planar=True,
            preferred_format=None,  # 由导出设置决定
            allow_downgrade=False,
            max_resolution=None,
            # P9.A: EXPORT 模式不启用硬件加速，保证质量
            enable_hwaccel=False,
            hwaccel_fallback_to_cpu=True,
            hwaccel_copy_to_cpu=True,
            hwaccel_preferred_decoder=None,
        )
    }

    # 格式降级映射（高子采样 -> 低子采样）
    _DOWNGRADE_MAP: dict[str, str] = {
        # 10-bit -> 8-bit
        'yuv444p10le': 'yuv444p',
        'yuv422p10le': 'yuv422p',
        'yuv420p10le': 'yuv420p',
        'yuv444p10be': 'yuv444p',
        'yuv422p10be': 'yuv422p',
        'yuv420p10be': 'yuv420p',
        # 12-bit -> 8-bit
        'yuv444p12le': 'yuv444p',
        'yuv422p12le': 'yuv422p',
        'yuv420p12le': 'yuv420p',
        # 16-bit -> 8-bit
        'yuv444p16le': 'yuv444p',
        'yuv422p16le': 'yuv422p',
        'yuv420p16le': 'yuv420p',
        # 子采样降级 (4:4:4 -> 4:2:2 -> 4:2:0)
        'yuv444p': 'yuv422p',
        'yuv422p': 'yuv420p',
    }

    # RGB 格式映射
    _RGB_FORMAT_MAP: dict[int, str] = {
        8: 'rgba',
        10: 'rgba64le',  # 使用 16-bit 存储 10-bit
        12: 'rgba64le',
        16: 'rgba64le',
        32: 'gbrapf32le',
    }

    @staticmethod
    def get_policy(target: DecodeTarget) -> DecodePolicy:
        """
        获取指定目标的解码策略。

        Args:
            target: 解码目标类型

        Returns:
            对应的解码策略配置
        """
        return DecodePolicyManager._POLICIES[target]

    @staticmethod
    def get_output_format(
        source_format: str,
        policy: DecodePolicy
    ) -> str:
        """
        根据源格式和策略决定输出格式。

        Args:
            source_format: 源像素格式名称
            policy: 解码策略

        Returns:
            输出格式名称
        """
        # 如果策略指定了首选格式且不允许降级，直接返回首选格式
        if policy.preferred_format and not policy.allow_downgrade:
            return policy.preferred_format

        # 如果强制 RGB 输出
        if policy.force_rgb:
            return DecodePolicyManager._get_rgb_format(source_format, policy)

        # 如果强制 8-bit 输出
        if policy.force_8bit:
            return DecodePolicyManager._get_8bit_format(source_format)

        # 如果允许降级
        if policy.allow_downgrade and policy.preferred_format:
            return policy.preferred_format

        # 默认保持源格式
        return source_format

    @staticmethod
    def _get_rgb_format(source_format: str, policy: DecodePolicy) -> str:
        """
        获取对应的 RGB 输出格式。

        Args:
            source_format: 源像素格式名称
            policy: 解码策略

        Returns:
            RGB 格式名称
        """
        # 推断位深度
        bit_depth = DecodePolicyManager._infer_bit_depth(source_format)

        # 应用策略限制
        if policy.max_bit_depth is not None:
            bit_depth = min(bit_depth, policy.max_bit_depth)
        if policy.force_8bit:
            bit_depth = 8

        # 返回对应的 RGB 格式
        return DecodePolicyManager._RGB_FORMAT_MAP.get(bit_depth, 'rgba')

    @staticmethod
    def _get_8bit_format(source_format: str) -> str:
        """
        获取对应的 8-bit 格式。

        沿 ``_DOWNGRADE_MAP`` 链式降级直到不动点（AUD-34411）：
        位深降级（10/12/16-bit → 8-bit）之后继续子采样降级
        （4:4:4 → 4:2:2 → 4:2:0），兑现模块头示例
        ``'yuv422p10le' → 'yuv420p'`` 与 PREVIEW 策略
        preferred_format='yuv420p' 的意图（此前 pass 占位使
        'yuv444p'/'yuv422p' 链从该路径不可达）。

        Args:
            source_format: 源像素格式名称

        Returns:
            8-bit 格式名称（链终点；不在映射内的格式原样返回）
        """
        target = source_format
        visited: set[str] = set()
        while target in DecodePolicyManager._DOWNGRADE_MAP and target not in visited:
            visited.add(target)
            target = DecodePolicyManager._DOWNGRADE_MAP[target]
        return target

    @staticmethod
    def _infer_bit_depth(pixel_format: str) -> int:
        """
        从像素格式名称推断位深度。

        Args:
            pixel_format: 像素格式名称

        Returns:
            位深度
        """
        # 检查常见模式
        if '10le' in pixel_format or '10be' in pixel_format or 'p10' in pixel_format:
            return 10
        if '12le' in pixel_format or '12be' in pixel_format or 'p12' in pixel_format:
            return 12
        if '14le' in pixel_format or '14be' in pixel_format or 'p14' in pixel_format:
            return 14
        if '16le' in pixel_format or '16be' in pixel_format or 'p16' in pixel_format:
            return 16
        if 'f32' in pixel_format:
            return 32
        if '48' in pixel_format or '64' in pixel_format:
            return 16

        # 默认 8-bit
        return 8

    @staticmethod
    def should_downgrade_for_preview(
        source_format: str,
        viewport_size: tuple[int, int]
    ) -> bool:
        """
        判断预览场景下是否需要降级。

        考虑因素：
        - 源格式位深度（高位置深度需要降级）
        - 视口大小（小视口可接受低质量）
        - 子采样模式（4:4:4 在小视口下可降级）

        Args:
            source_format: 源像素格式名称
            viewport_size: 视口尺寸 (width, height)

        Returns:
            True 表示应该降级
        """
        # 获取预览策略
        preview_policy = DecodePolicyManager.get_policy(DecodeTarget.PREVIEW)

        # 检查位深度
        source_depth = DecodePolicyManager._infer_bit_depth(source_format)
        if source_depth > 8 and preview_policy.force_8bit:
            return True

        # 检查分辨率
        if preview_policy.max_resolution is not None:
            max_w, max_h = preview_policy.max_resolution
            view_w, view_h = viewport_size
            if view_w <= max_w and view_h <= max_h:
                # 视口在限制内，可以降级
                return True

        # 检查是否为高带宽格式（4:4:4）
        if '444' in source_format:
            # 4:4:4 格式带宽较大，可考虑降级
            return True

        return False

    @staticmethod
    def get_preview_format(
        source_format: str,
        viewport_size: tuple[int, int]
    ) -> str:
        """
        获取预览场景的最佳格式。

        Args:
            source_format: 源像素格式名称
            viewport_size: 视口尺寸 (width, height)

        Returns:
            推荐的预览格式
        """
        preview_policy = DecodePolicyManager.get_policy(DecodeTarget.PREVIEW)

        if DecodePolicyManager.should_downgrade_for_preview(source_format, viewport_size):
            return DecodePolicyManager.get_output_format(source_format, preview_policy)

        return source_format

    @staticmethod
    def get_master_format(source_format: str) -> str:
        """
        获取母版处理场景的格式。

        母版处理保留原始质量，不进行降级。

        Args:
            source_format: 源像素格式名称

        Returns:
            母版处理格式（通常与源格式相同）
        """
        master_policy = DecodePolicyManager.get_policy(DecodeTarget.MASTER)
        return DecodePolicyManager.get_output_format(source_format, master_policy)

    @staticmethod
    def get_export_format(
        source_format: str,
        target_bit_depth: int | None = None,
        target_subsampling: str | None = None
    ) -> str:
        """
        获取导出场景的格式。

        导出时可以根据需要调整位深度和子采样。

        Args:
            source_format: 源像素格式名称
            target_bit_depth: 目标位深度（None 表示保持原样）
            target_subsampling: 目标子采样模式（'420', '422', '444'，None 表示保持原样）

        Returns:
            导出格式
        """
        # 如果没有特殊要求，保持原样
        if target_bit_depth is None and target_subsampling is None:
            return source_format

        # 解析源格式
        current_depth = DecodePolicyManager._infer_bit_depth(source_format)


        # 构建目标格式
        depth = target_bit_depth if target_bit_depth is not None else current_depth

        if target_subsampling is not None:
            # 构建新的格式名称
            if depth == 8:
                return f"yuv{target_subsampling}p"
            else:
                return f"yuv{target_subsampling}p{depth}le"

        # 只改变位深度
        if depth == 8:
            # 提取子采样模式
            for pattern in ['420', '422', '444']:
                if pattern in source_format:
                    return f"yuv{pattern}p"
        else:
            for pattern in ['420', '422', '444']:
                if pattern in source_format:
                    return f"yuv{pattern}p{depth}le"

        # 无法推断，返回原格式
        return source_format

    @staticmethod
    def compare_quality(format1: str, format2: str) -> int:
        """
        比较两个格式的质量。

        Args:
            format1: 第一个格式名称
            format2: 第二个格式名称

        Returns:
            负数表示 format1 质量更低
            正数表示 format1 质量更高
            0 表示质量相同
        """
        score1 = DecodePolicyManager._calculate_quality_score(format1)
        score2 = DecodePolicyManager._calculate_quality_score(format2)
        return score1 - score2

    @staticmethod
    def _calculate_quality_score(pixel_format: str) -> int:
        """
        计算格式的质量分数。

        分数基于：
        - 位深度（权重 100）
        - 子采样模式（权重 10）
        - Alpha 通道（权重 1）

        Args:
            pixel_format: 像素格式名称

        Returns:
            质量分数
        """
        score = 0

        # 位深度
        depth = DecodePolicyManager._infer_bit_depth(pixel_format)
        score += depth * 100

        # 子采样模式
        if '444' in pixel_format:
            score += 40
        elif '422' in pixel_format:
            score += 20
        elif '420' in pixel_format:
            score += 10

        # Alpha 通道
        if 'a' in pixel_format.lower() and 'yuva' in pixel_format.lower():
            score += 1
        elif 'rgba' in pixel_format or 'bgra' in pixel_format or 'gbrap' in pixel_format:
            score += 1

        return score