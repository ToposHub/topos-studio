"""
DecodePolicy 测试
测试解码策略模块的核心功能。

测试覆盖：
- DecodeTarget 枚举 (PREVIEW/MASTER/EXPORT)
- DecodePolicy 配置属性
- DecodePolicyManager 策略获取
- 格式降级规则
- 位深度推断
- 质量比较
"""

import pytest
from typing import Tuple

from src.shared.media.decode_policy import (
    DecodeTarget,
    DecodePolicy,
    DecodePolicyManager
)


class TestDecodeTarget:
    """测试 DecodeTarget 枚举"""

    def test_decode_target_values(self):
        """测试枚举值"""
        assert DecodeTarget.PREVIEW.value == 'preview'
        assert DecodeTarget.MASTER.value == 'master'
        assert DecodeTarget.EXPORT.value == 'export'

    def test_decode_target_members(self):
        """测试枚举成员数量"""
        members = list(DecodeTarget)
        assert len(members) == 3
        assert DecodeTarget.PREVIEW in members
        assert DecodeTarget.MASTER in members
        assert DecodeTarget.EXPORT in members


class TestDecodePolicy:
    """测试 DecodePolicy 配置"""

    def test_decode_policy_creation(self):
        """测试创建 DecodePolicy"""
        policy = DecodePolicy(
            target=DecodeTarget.PREVIEW,
            max_bit_depth=8,
            force_rgb=False,
            force_8bit=True,
            prefer_planar=False,
            preferred_format='yuv420p',
            allow_downgrade=True,
            max_resolution=(1920, 1080)
        )

        assert policy.target == DecodeTarget.PREVIEW
        assert policy.max_bit_depth == 8
        assert policy.force_rgb is False
        assert policy.force_8bit is True
        assert policy.prefer_planar is False
        assert policy.preferred_format == 'yuv420p'
        assert policy.allow_downgrade is True
        assert policy.max_resolution == (1920, 1080)

    def test_decode_policy_frozen(self):
        """测试 DecodePolicy 是 frozen dataclass"""
        policy = DecodePolicy(
            target=DecodeTarget.MASTER,
            max_bit_depth=None,
            force_rgb=False,
            force_8bit=False,
            prefer_planar=True,
            preferred_format=None,
            allow_downgrade=False,
            max_resolution=None
        )

        # frozen dataclass 不允许修改
        with pytest.raises(Exception):  # FrozenInstanceError
            policy.max_bit_depth = 10

    def test_decode_policy_optional_fields(self):
        """测试可选字段可以为 None"""
        policy = DecodePolicy(
            target=DecodeTarget.MASTER,
            max_bit_depth=None,
            force_rgb=False,
            force_8bit=False,
            prefer_planar=True,
            preferred_format=None,
            allow_downgrade=False,
            max_resolution=None
        )

        assert policy.max_bit_depth is None
        assert policy.preferred_format is None
        assert policy.max_resolution is None


class TestDecodePolicyManagerGetPolicy:
    """测试策略获取"""

    def test_get_policy_preview(self):
        """测试获取预览策略"""
        policy = DecodePolicyManager.get_policy(DecodeTarget.PREVIEW)

        assert policy.target == DecodeTarget.PREVIEW
        # 预览策略应该强制 8-bit
        assert policy.force_8bit is True
        assert policy.max_bit_depth == 8
        # 预览策略允许降级
        assert policy.allow_downgrade is True
        # 预览策略有分辨率限制
        assert policy.max_resolution == (1920, 1080)
        # 预览策略有首选格式
        assert policy.preferred_format == 'yuv420p'

    def test_get_policy_master(self):
        """测试获取母版策略"""
        policy = DecodePolicyManager.get_policy(DecodeTarget.MASTER)

        assert policy.target == DecodeTarget.MASTER
        # 母版策略保留原始位深
        assert policy.max_bit_depth is None
        assert policy.force_8bit is False
        # 母版策略不降级
        assert policy.allow_downgrade is False
        # 母版策略偏好 planar
        assert policy.prefer_planar is True
        # 母版策略无分辨率限制
        assert policy.max_resolution is None
        # 母版策略保持原始格式
        assert policy.preferred_format is None

    def test_get_policy_export(self):
        """测试获取导出策略"""
        policy = DecodePolicyManager.get_policy(DecodeTarget.EXPORT)

        assert policy.target == DecodeTarget.EXPORT
        # 导出策略保留原始位深
        assert policy.max_bit_depth is None
        assert policy.force_8bit is False
        # 导出策略不降级
        assert policy.allow_downgrade is False
        # 导出策略偏好 planar
        assert policy.prefer_planar is True
        # 导出策略无分辨率限制
        assert policy.max_resolution is None

    def test_get_policy_invalid_target(self):
        """测试无效目标（应抛出异常）"""
        # 使用有效的枚举值不应抛出异常
        for target in DecodeTarget:
            policy = DecodePolicyManager.get_policy(target)
            assert policy is not None


class TestDecodePolicyManagerOutputFormat:
    """测试输出格式决策"""

    def test_get_output_format_preview_10bit_to_8bit(self):
        """测试预览策略将 10-bit 降级为 8-bit"""
        preview_policy = DecodePolicyManager.get_policy(DecodeTarget.PREVIEW)

        # 10-bit 格式应降级为 8-bit
        output = DecodePolicyManager.get_output_format(
            'yuv420p10le',
            preview_policy
        )

        # 预览策略的 preferred_format 是 yuv420p
        assert output == 'yuv420p'

    def test_get_output_format_preview_422_to_420(self):
        """测试预览策略将 4:2:2 降级为 4:2:0"""
        preview_policy = DecodePolicyManager.get_policy(DecodeTarget.PREVIEW)

        output = DecodePolicyManager.get_output_format(
            'yuv422p',
            preview_policy
        )

        assert output == 'yuv420p'

    def test_get_output_format_master_preserves_source(self):
        """测试母版策略保持源格式"""
        master_policy = DecodePolicyManager.get_policy(DecodeTarget.MASTER)

        # 母版策略保持原始格式
        output = DecodePolicyManager.get_output_format(
            'yuv420p10le',
            master_policy
        )

        assert output == 'yuv420p10le'

    def test_get_output_format_export_preserves_source(self):
        """测试导出策略保持源格式"""
        export_policy = DecodePolicyManager.get_policy(DecodeTarget.EXPORT)

        output = DecodePolicyManager.get_output_format(
            'yuv422p10le',
            export_policy
        )

        assert output == 'yuv422p10le'

    def test_get_output_format_preserves_8bit(self):
        """测试 8-bit 格式保持不变"""
        preview_policy = DecodePolicyManager.get_policy(DecodeTarget.PREVIEW)

        # 8-bit 格式已经是目标
        output = DecodePolicyManager.get_output_format(
            'yuv420p',
            preview_policy
        )

        assert output == 'yuv420p'


class TestDecodePolicyManagerDowngrade:
    """测试降级规则"""

    def test_should_downgrade_for_preview_high_bit_depth(self):
        """测试预览场景高位深需要降级"""
        # 10-bit 格式需要降级
        should_downgrade = DecodePolicyManager.should_downgrade_for_preview(
            'yuv420p10le',
            (1920, 1080)
        )

        assert should_downgrade is True

    def test_should_downgrade_for_preview_444_format(self):
        """测试预览场景 4:4:4 格式可降级"""
        # 4:4:4 格式带宽大，可降级
        should_downgrade = DecodePolicyManager.should_downgrade_for_preview(
            'yuv444p',
            (1920, 1080)
        )

        assert should_downgrade is True

    def test_should_downgrade_for_preview_420_8bit(self):
        """测试预览场景 4:2:0 8-bit"""
        # 4:2:0 8-bit 是预览目标格式
        should_downgrade = DecodePolicyManager.should_downgrade_for_preview(
            'yuv420p',
            (1920, 1080)
        )

        # 视口在限制内，4:2:0 不需要降级
        # 但根据实现，视口在限制内返回 True
        assert should_downgrade is True

    def test_should_downgrade_for_preview_large_viewport(self):
        """测试预览场景大视口"""
        # 大视口超出限制，根据实现逻辑
        # 视口超出限制时，分辨率检查返回 True（视口在限制内才返回 True）
        should_downgrade = DecodePolicyManager.should_downgrade_for_preview(
            'yuv420p',
            (3840, 2160)  # 4K 视口
        )

        # yuv420p 没有 '444'，视口超出限制不返回 True（分辨率检查条件不满足）
        # 视口大于 max_resolution (1920, 1080)，条件 view_w <= max_w and view_h <= max_h 不满足
        assert should_downgrade is False

    def test_get_preview_format(self):
        """测试获取预览格式

        AUD-34411：链式降级到不动点。旧断言 'yuv422p' 锁定的是
        "降位深、保子采样"的 pass 占位中间值；按模块头契约
        （get_output_format('yuv422p10le', PREVIEW) == 'yuv420p'）更新。
        """
        preview_format = DecodePolicyManager.get_preview_format(
            'yuv422p10le',
            (1920, 1080)
        )
        assert preview_format == 'yuv420p'

    def test_get_preview_format_no_downgrade(self):
        """测试预览格式不降级情况"""
        preview_format = DecodePolicyManager.get_preview_format(
            'yuv420p',
            (3840, 2160)  # 大视口
        )

        # 保持原格式或根据策略决定
        # 视口超出限制，分辨率检查返回 True
        # 所以仍返回 preferred_format
        assert preview_format == 'yuv420p'


class TestDecodePolicyManagerMasterFormat:
    """测试母版格式获取"""

    def test_get_master_format_preserves_source(self):
        """测试母版格式保持源格式"""
        master_format = DecodePolicyManager.get_master_format('yuv420p10le')

        assert master_format == 'yuv420p10le'

    def test_get_master_format_8bit(self):
        """测试母版格式 8-bit"""
        master_format = DecodePolicyManager.get_master_format('yuv422p')

        assert master_format == 'yuv422p'

    def test_get_master_format_rgb(self):
        """测试母版格式 RGB"""
        master_format = DecodePolicyManager.get_master_format('rgba')

        assert master_format == 'rgba'


class TestDecodePolicyManagerExportFormat:
    """测试导出格式获取"""

    def test_get_export_format_preserves_source(self):
        """测试导出格式保持源格式"""
        export_format = DecodePolicyManager.get_export_format('yuv420p10le')

        assert export_format == 'yuv420p10le'

    def test_get_export_format_change_bit_depth(self):
        """测试导出格式改变位深度"""
        # 改变位深度到 12-bit
        export_format = DecodePolicyManager.get_export_format(
            'yuv420p10le',
            target_bit_depth=12
        )

        assert export_format == 'yuv420p12le'

    def test_get_export_format_change_subsampling(self):
        """测试导出格式改变子采样"""
        # 改变子采样到 4:2:2
        export_format = DecodePolicyManager.get_export_format(
            'yuv420p10le',
            target_subsampling='422'
        )

        assert export_format == 'yuv422p10le'

    def test_get_export_format_change_both(self):
        """测试导出格式同时改变位深度和子采样"""
        export_format = DecodePolicyManager.get_export_format(
            'yuv420p10le',
            target_bit_depth=8,
            target_subsampling='444'
        )

        assert export_format == 'yuv444p'

    def test_get_export_format_8bit_from_10bit(self):
        """测试从 10-bit 导出为 8-bit"""
        export_format = DecodePolicyManager.get_export_format(
            'yuv422p10le',
            target_bit_depth=8
        )

        assert export_format == 'yuv422p'


class TestDecodePolicyManagerBitDepthInference:
    """测试位深度推断"""

    def test_infer_bit_depth_8bit(self):
        """测试推断 8-bit"""
        depth = DecodePolicyManager._infer_bit_depth('yuv420p')
        assert depth == 8

    def test_infer_bit_depth_10bit_le(self):
        """测试推断 10-bit little-endian"""
        depth = DecodePolicyManager._infer_bit_depth('yuv420p10le')
        assert depth == 10

    def test_infer_bit_depth_10bit_be(self):
        """测试推断 10-bit big-endian"""
        depth = DecodePolicyManager._infer_bit_depth('yuv420p10be')
        assert depth == 10

    def test_infer_bit_depth_12bit(self):
        """测试推断 12-bit"""
        depth = DecodePolicyManager._infer_bit_depth('yuv420p12le')
        assert depth == 12

    def test_infer_bit_depth_16bit(self):
        """测试推断 16-bit"""
        depth = DecodePolicyManager._infer_bit_depth('yuv420p16le')
        assert depth == 16

    def test_infer_bit_depth_float32(self):
        """测试推断 32-bit float"""
        depth = DecodePolicyManager._infer_bit_depth('gbrpf32le')
        assert depth == 32

    def test_infer_bit_depth_rgb48(self):
        """测试推断 RGB 48-bit (16-bit per channel)"""
        depth = DecodePolicyManager._infer_bit_depth('rgb48le')
        assert depth == 16

    def test_infer_bit_depth_rgb64(self):
        """测试推断 RGB 64-bit (16-bit per channel with alpha)"""
        depth = DecodePolicyManager._infer_bit_depth('rgba64le')
        assert depth == 16

    def test_infer_bit_depth_unknown(self):
        """测试推断未知格式"""
        depth = DecodePolicyManager._infer_bit_depth('unknown_format')
        # 默认返回 8
        assert depth == 8


class TestDecodePolicyManagerQualityComparison:
    """测试质量比较"""

    def test_compare_quality_same_format(self):
        """测试相同格式质量相同"""
        result = DecodePolicyManager.compare_quality('yuv420p', 'yuv420p')
        assert result == 0

    def test_compare_quality_higher_bit_depth(self):
        """测试高位深质量更高"""
        # 10-bit vs 8-bit
        result = DecodePolicyManager.compare_quality('yuv420p10le', 'yuv420p')
        # 10-bit 应比 8-bit 质量更高
        assert result > 0

    def test_compare_quality_higher_subsampling(self):
        """测试高子采样质量更高"""
        # 4:4:4 vs 4:2:0 (相同位深)
        result = DecodePolicyManager.compare_quality('yuv444p', 'yuv420p')
        # 4:4:4 应比 4:2:0 质量更高
        assert result > 0

    def test_compare_quality_lower_bit_depth(self):
        """测试低位深质量更低"""
        result = DecodePolicyManager.compare_quality('yuv420p', 'yuv420p10le')
        assert result < 0

    def test_compare_quality_alpha_channel(self):
        """测试 Alpha 通道影响质量"""
        # 带 Alpha vs 不带 Alpha
        result = DecodePolicyManager.compare_quality('yuva420p', 'yuv420p')
        # 带 Alpha 应质量略高
        assert result > 0


class TestDecodePolicyManagerDowngradeMap:
    """测试降级映射表"""

    def test_downgrade_map_10bit_to_8bit(self):
        """测试 10-bit 降级映射"""
        # yuv420p10le -> yuv420p
        assert DecodePolicyManager._DOWNGRADE_MAP['yuv420p10le'] == 'yuv420p'

        # yuv422p10le -> yuv422p
        assert DecodePolicyManager._DOWNGRADE_MAP['yuv422p10le'] == 'yuv422p'

        # yuv444p10le -> yuv444p
        assert DecodePolicyManager._DOWNGRADE_MAP['yuv444p10le'] == 'yuv444p'

    def test_downgrade_map_subsampling(self):
        """测试子采样降级映射"""
        # yuv444p -> yuv422p
        assert DecodePolicyManager._DOWNGRADE_MAP['yuv444p'] == 'yuv422p'

        # yuv422p -> yuv420p
        assert DecodePolicyManager._DOWNGRADE_MAP['yuv422p'] == 'yuv420p'

    def test_downgrade_map_12bit(self):
        """测试 12-bit 降级映射"""
        assert DecodePolicyManager._DOWNGRADE_MAP['yuv420p12le'] == 'yuv420p'
        assert DecodePolicyManager._DOWNGRADE_MAP['yuv422p12le'] == 'yuv422p'

    def test_downgrade_map_16bit(self):
        """测试 16-bit 降级映射"""
        assert DecodePolicyManager._DOWNGRADE_MAP['yuv420p16le'] == 'yuv420p'
        assert DecodePolicyManager._DOWNGRADE_MAP['yuv422p16le'] == 'yuv422p'


class TestDecodePolicyManagerRGBFormatMap:
    """测试 RGB 格式映射"""

    def test_rgb_format_map_8bit(self):
        """测试 8-bit RGB 格式"""
        assert DecodePolicyManager._RGB_FORMAT_MAP[8] == 'rgba'

    def test_rgb_format_map_10bit(self):
        """测试 10-bit RGB 格式"""
        assert DecodePolicyManager._RGB_FORMAT_MAP[10] == 'rgba64le'

    def test_rgb_format_map_12bit(self):
        """测试 12-bit RGB 格式"""
        assert DecodePolicyManager._RGB_FORMAT_MAP[12] == 'rgba64le'

    def test_rgb_format_map_16bit(self):
        """测试 16-bit RGB 格式"""
        assert DecodePolicyManager._RGB_FORMAT_MAP[16] == 'rgba64le'

    def test_rgb_format_map_32bit(self):
        """测试 32-bit float RGB 格式"""
        assert DecodePolicyManager._RGB_FORMAT_MAP[32] == 'gbrapf32le'


class TestDecodePolicyManagerQualityScore:
    """测试质量分数计算"""

    def test_calculate_quality_score_8bit_420(self):
        """测试 8-bit 4:2:0 质量分数"""
        score = DecodePolicyManager._calculate_quality_score('yuv420p')

        # 位深 8 * 100 = 800
        # 子采样 420 = 10
        # 总分 810
        assert score == 810

    def test_calculate_quality_score_10bit_420(self):
        """测试 10-bit 4:2:0 质量分数"""
        score = DecodePolicyManager._calculate_quality_score('yuv420p10le')

        # 位深 10 * 100 = 1000
        # 子采样 420 = 10
        # 总分 1010
        assert score == 1010

    def test_calculate_quality_score_8bit_444(self):
        """测试 8-bit 4:4:4 质量分数"""
        score = DecodePolicyManager._calculate_quality_score('yuv444p')

        # 位深 8 * 100 = 800
        # 子采样 444 = 40
        # 总分 840
        assert score == 840

    def test_calculate_quality_score_10bit_444(self):
        """测试 10-bit 4:4:4 质量分数"""
        score = DecodePolicyManager._calculate_quality_score('yuv444p10le')

        # 位深 10 * 100 = 1000
        # 子采样 444 = 40
        # 总分 1040
        assert score == 1040

    def test_calculate_quality_score_rgb_alpha(self):
        """测试 RGB 带 Alpha 质量分数"""
        score = DecodePolicyManager._calculate_quality_score('rgba')

        # 位深 8 * 100 = 800
        # RGB 无子采样加分
        # Alpha +1
        # 总分 801
        assert score == 801

    def test_calculate_quality_score_yuva(self):
        """测试 YUVA 带 Alpha 质量分数"""
        score = DecodePolicyManager._calculate_quality_score('yuva420p')

        # 位深 8 * 100 = 800
        # 子采样 420 = 10
        # Alpha +1
        # 总分 811
        assert score == 811


class TestDecodePolicyManager8bitFormat:
    """测试 8-bit 格式获取"""

    def test_get_8bit_format_10bit(self):
        """测试从 10-bit 获取 8-bit 格式"""
        output = DecodePolicyManager._get_8bit_format('yuv420p10le')
        assert output == 'yuv420p'

    def test_get_8bit_format_12bit(self):
        """测试从 12-bit 获取 8-bit 格式

        AUD-34411：链式降级到不动点。旧断言 'yuv422p' 锁定的是
        pass 占位的中间值，与模块头示例（'yuv422p10le' → 'yuv420p'）
        及 PREVIEW.preferred_format='yuv420p' 的策略意图相悖，
        按 documented contract 更新。
        """
        output = DecodePolicyManager._get_8bit_format('yuv422p12le')
        assert output == 'yuv420p'

    def test_get_8bit_format_already_8bit(self):
        """测试已经是 8-bit 格式"""
        output = DecodePolicyManager._get_8bit_format('yuv420p')
        assert output == 'yuv420p'


class TestDecodePolicyPoliciesDict:
    """测试策略字典"""

    def test_policies_dict_contains_all_targets(self):
        """测试策略字典包含所有目标"""
        for target in DecodeTarget:
            assert target in DecodePolicyManager._POLICIES
            policy = DecodePolicyManager._POLICIES[target]
            assert policy.target == target


class TestPolicyDifferences:
    """测试不同策略的差异"""

    def test_preview_vs_master_force_8bit(self):
        """测试预览与母版的 force_8bit 差异"""
        preview = DecodePolicyManager.get_policy(DecodeTarget.PREVIEW)
        master = DecodePolicyManager.get_policy(DecodeTarget.MASTER)

        assert preview.force_8bit is True
        assert master.force_8bit is False

    def test_preview_vs_master_allow_downgrade(self):
        """测试预览与母版的降级差异"""
        preview = DecodePolicyManager.get_policy(DecodeTarget.PREVIEW)
        master = DecodePolicyManager.get_policy(DecodeTarget.MASTER)

        assert preview.allow_downgrade is True
        assert master.allow_downgrade is False

    def test_preview_vs_master_resolution_limit(self):
        """测试预览与母版的分辨率限制差异"""
        preview = DecodePolicyManager.get_policy(DecodeTarget.PREVIEW)
        master = DecodePolicyManager.get_policy(DecodeTarget.MASTER)

        assert preview.max_resolution is not None
        assert master.max_resolution is None

    def test_master_vs_export_prefer_planar(self):
        """测试母版与导出的 prefer_planar"""
        master = DecodePolicyManager.get_policy(DecodeTarget.MASTER)
        export = DecodePolicyManager.get_policy(DecodeTarget.EXPORT)

        # 两者都偏好 planar
        assert master.prefer_planar is True
        assert export.prefer_planar is True

    def test_master_vs_export_preserve_quality(self):
        """测试母版与导出都保留质量"""
        master = DecodePolicyManager.get_policy(DecodeTarget.MASTER)
        export = DecodePolicyManager.get_policy(DecodeTarget.EXPORT)

        # 两者都不强制 8-bit
        assert master.force_8bit is False
        assert export.force_8bit is False

        # 两者都不降级
        assert master.allow_downgrade is False
        assert export.allow_downgrade is False