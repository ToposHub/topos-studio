"""
Timecode Unit Tests
测试 Timecode 专业时间码模型。

覆盖：
- DF/NDF 帧率检测
- 时间码解析（冒号/分号分隔符）
- DF/NDF 转换算法
- 边界验证（分钟开始帧）
- 双向转换一致性
- 比较运算
"""

import pytest
from src.shared.media.timecode import Timecode, TimecodeType, DROP_FRAME_FPS_RATIOS
from src.shared.media import Rational, FPS_23_976, FPS_24, FPS_25, FPS_29_97, FPS_30, FPS_50, FPS_59_94, FPS_60


class TestTimecodeType:
    """TimecodeType 枚举测试"""

    def test_enums_exist(self) -> None:
        """测试枚举值存在"""
        assert TimecodeType.NON_DROP_FRAME.value == 'ndf'
        assert TimecodeType.DROP_FRAME.value == 'df'

    def test_enum_comparison(self) -> None:
        """测试枚举比较"""
        assert TimecodeType.NON_DROP_FRAME != TimecodeType.DROP_FRAME


class TestDropFrameDetection:
    """丢帧帧率检测测试"""

    def test_23_976_is_ndf(self) -> None:
        """23.976fps 是 NDF（不丢帧）"""
        assert Timecode.is_drop_frame_fps(FPS_23_976) is False

    def test_29_97_is_df(self) -> None:
        """29.97fps 是 DF（丢帧）"""
        assert Timecode.is_drop_frame_fps(FPS_29_97) is True

    def test_59_94_is_df(self) -> None:
        """59.94fps 是 DF（丢帧）"""
        assert Timecode.is_drop_frame_fps(FPS_59_94) is True

    def test_24fps_is_ndf(self) -> None:
        """24fps 是 NDF"""
        assert Timecode.is_drop_frame_fps(FPS_24) is False

    def test_25fps_is_ndf(self) -> None:
        """25fps 是 NDF"""
        assert Timecode.is_drop_frame_fps(FPS_25) is False

    def test_30fps_is_ndf(self) -> None:
        """30fps 是 NDF（虽然接近 29.97，但整数帧率不丢帧）"""
        assert Timecode.is_drop_frame_fps(FPS_30) is False

    def test_50fps_is_ndf(self) -> None:
        """50fps 是 NDF"""
        assert Timecode.is_drop_frame_fps(FPS_50) is False

    def test_60fps_is_ndf(self) -> None:
        """60fps 是 NDF"""
        assert Timecode.is_drop_frame_fps(FPS_60) is False

    def test_auto_detect_type(self) -> None:
        """测试自动检测推荐类型"""
        assert Timecode.auto_detect_type(FPS_23_976) == TimecodeType.NON_DROP_FRAME
        assert Timecode.auto_detect_type(FPS_29_97) == TimecodeType.DROP_FRAME
        assert Timecode.auto_detect_type(FPS_59_94) == TimecodeType.DROP_FRAME
        assert Timecode.auto_detect_type(FPS_24) == TimecodeType.NON_DROP_FRAME


class TestTimecodeFromString:
    """时间码字符串解析测试"""

    def test_parse_ndf_colon_format(self) -> None:
        """解析 NDF 格式（全冒号）"""
        tc = Timecode.from_string('01:00:00:00', FPS_29_97)
        assert tc.tc_type == TimecodeType.NON_DROP_FRAME
        assert tc.hours == 1
        assert tc.minutes == 0
        assert tc.seconds == 0
        assert tc.frames == 0

    def test_parse_df_semicolon_format(self) -> None:
        """解析 DF 格式（最后分号）"""
        tc = Timecode.from_string('01:00:00;00', FPS_29_97)
        assert tc.tc_type == TimecodeType.DROP_FRAME
        assert tc.hours == 1
        assert tc.minutes == 0
        assert tc.seconds == 0
        assert tc.frames == 0

    def test_parse_zero_timecode(self) -> None:
        """解析零时间码"""
        tc = Timecode.from_string('00:00:00:00', FPS_24)
        assert tc.hours == 0
        assert tc.minutes == 0
        assert tc.seconds == 0
        assert tc.frames == 0

    def test_parse_with_spaces(self) -> None:
        """解析带空格的时间码"""
        tc = Timecode.from_string('  01:00:00:00  ', FPS_24)
        assert tc.hours == 1

    def test_invalid_format_no_separator(self) -> None:
        """无效格式：无分隔符"""
        with pytest.raises(ValueError, match="Invalid timecode format"):
            Timecode.from_string('01000000', FPS_24)

    def test_invalid_format_wrong_parts(self) -> None:
        """无效格式：部分数量错误"""
        with pytest.raises(ValueError, match="expected 4 parts"):
            Timecode.from_string('01:00:00', FPS_24)


class TestNDFTimecodeConversion:
    """NDF 时间码转换测试"""

    def test_ndf_24fps_hour(self) -> None:
        """24fps NDF 一小时"""
        tc = Timecode.from_string('01:00:00:00', FPS_24)
        assert tc.to_frame_number() == 24 * 60 * 60  # 86400

    def test_ndf_24fps_roundtrip(self) -> None:
        """24fps NDF 双向转换"""
        for frame in [0, 24, 86400, 172800]:
            tc = Timecode.from_frame_number(frame, FPS_24, TimecodeType.NON_DROP_FRAME)
            back = tc.to_frame_number()
            assert back == frame

    def test_ndf_23976fps_hour(self) -> None:
        """23.976fps NDF 一小时"""
        tc = Timecode.from_string('01:00:00:00', FPS_23_976)
        # 23.976 NDF 每小时 = 24 * 60 * 60 帧（整数帧率表示）
        assert tc.to_frame_number() == 86400

    def test_ndf_25fps_hour(self) -> None:
        """25fps NDF 一小时"""
        tc = Timecode.from_string('01:00:00:00', FPS_25)
        assert tc.to_frame_number() == 25 * 60 * 60  # 90000


class TestDFTimecodeConversion:
    """DF 时间码转换测试（核心算法）"""

    def test_df_2997fps_hour(self) -> None:
        """29.97fps DF 一小时"""
        tc = Timecode.from_string('01:00:00;00', FPS_29_97)
        # 29.97 DF: 每小时丢帧数 = 54 * 2 = 108
        # 每小时帧数 = 30 * 3600 - 108 = 107892
        assert tc.to_frame_number() == 107892

    def test_df_5994fps_hour(self) -> None:
        """59.94fps DF 一小时"""
        tc = Timecode.from_string('01:00:00;00', FPS_59_94)
        # 59.94 DF: 每小时丢帧数 = 54 * 4 = 216
        # 每小时帧数 = 60 * 3600 - 216 = 215784
        assert tc.to_frame_number() == 215784

    def test_df_roundtrip_various_frames(self) -> None:
        """DF 双向转换一致性测试"""
        test_frames = [0, 30, 107892, 215784, 1078920]  # 0, 1秒, 1小时, 2小时, 10小时
        for frame in test_frames:
            tc = Timecode.from_frame_number(frame, FPS_29_97, TimecodeType.DROP_FRAME)
            back = tc.to_frame_number()
            assert back == frame, f"Roundtrip failed for frame {frame}"

    def test_df_roundtrip_5994fps(self) -> None:
        """59.94fps DF 双向转换"""
        test_frames = [0, 60, 215784, 431568]  # 0, 1秒, 1小时, 2小时
        for frame in test_frames:
            tc = Timecode.from_frame_number(frame, FPS_59_94, TimecodeType.DROP_FRAME)
            back = tc.to_frame_number()
            assert back == frame

    def test_df_first_minute(self) -> None:
        """DF 第一分钟（分钟00不丢帧）"""
        tc = Timecode.from_string('00:00:00;00', FPS_29_97)
        assert tc.to_frame_number() == 0

        tc_30 = Timecode.from_string('00:00:01;00', FPS_29_97)
        assert tc_30.to_frame_number() == 30

    def test_df_second_minute(self) -> None:
        """DF 第二分钟（分钟01丢帧）"""
        # 00:01:00;02 有效（跳过了帧号00和01）
        tc = Timecode.from_string('00:01:00;02', FPS_29_97)
        # 基础帧数 = 1*60*30 + 0*30 + 2 = 1802
        # 丢帧数 = 2（分钟01丢帧）
        # 实际帧号 = 1800
        assert tc.to_frame_number() == 1800

    def test_df_minute_tail_boundaries(self) -> None:
        """分钟尾边界（块内第 0 分钟按满帧 1800 计）。

        回归：旧算法把块内第 0 分钟当丢帧分钟（1798），:59 秒尾部
        2/4 帧抛 ValueError，UI 回退错误 NDF 显示。与容器侧
        tmcd 逆换算（mov.c）同规。"""
        df = TimecodeType.DROP_FRAME
        cases_30 = [
            (1797, '00:00:59;27'), (1798, '00:00:59;28'), (1799, '00:00:59;29'),
            (1800, '00:01:00;02'), (1801, '00:01:00;03'), (3597, '00:01:59;29'),
            (3598, '00:02:00;02'),
            (16181, '00:08:59;27'), (16182, '00:08:59;28'),
            (16183, '00:08:59;29'),
            (17981, '00:09:59;29'), (17982, '00:10:00;00'), (17983, '00:10:00;01'),
            (19780, '00:10:59;28'), (19781, '00:10:59;29'), (19782, '00:11:00;02'),
        ]
        for frame, want in cases_30:
            tc = Timecode.from_frame_number(frame, FPS_29_97, df)
            assert tc.to_string() == want, (
                f"30DF {frame}: got {tc.to_string()} want {want}")
            assert tc.to_frame_number() == frame, (
                f"30DF roundtrip fail at {frame}")

    def test_df_minute_tail_boundaries_60fps(self) -> None:
        """60DF 分钟尾边界（每分钟跳 4 帧；每 10 分钟整不跳）"""
        df = TimecodeType.DROP_FRAME
        cases_60 = [
            (3595, '00:00:59;55'), (3599, '00:00:59;59'),
            (3600, '00:01:00;04'), (3601, '00:01:00;05'),
            (7195, '00:01:59;59'), (7196, '00:02:00;04'),
            (35963, '00:09:59;59'), (35964, '00:10:00;00'), (35965, '00:10:00;01'),
            (215784, '01:00:00;00'),
        ]
        for frame, want in cases_60:
            tc = Timecode.from_frame_number(frame, FPS_59_94, df)
            assert tc.to_string() == want, (
                f"60DF {frame}: got {tc.to_string()} want {want}")
            assert tc.to_frame_number() == frame, (
                f"60DF roundtrip fail at {frame}")

    def test_df_roundtrip_random_sweep(self) -> None:
        """全量域随机抽样往返（seed 固定；双帧率族）"""
        import random
        rng = random.Random(20260921)
        for _ in range(500):
            frame = rng.randrange(0, 107892 * 3)
            tc = Timecode.from_frame_number(frame, FPS_29_97,
                                            TimecodeType.DROP_FRAME)
            assert tc.to_frame_number() == frame
        for _ in range(500):
            frame = rng.randrange(0, 215784 * 2)
            tc = Timecode.from_frame_number(frame, FPS_59_94,
                                            TimecodeType.DROP_FRAME)
            assert tc.to_frame_number() == frame

    def test_df_10th_minute(self) -> None:
        """DF 第十分钟（整点十分钟不丢帧）"""
        # 分钟10是整点十分钟，帧号00有效
        tc = Timecode.from_string('00:10:00;00', FPS_29_97)
        # 基础帧数 = 10*60*30 = 18000
        # 丢帧数 = 9*2 = 18（前9分钟每分钟丢2帧）
        # 实际帧号 = 17982
        assert tc.to_frame_number() == 17982


class TestDFValidation:
    """DF 时间码验证测试"""

    def test_invalid_df_minute_1_frame_0(self) -> None:
        """分钟01的帧号00无效"""
        with pytest.raises(ValueError, match="Invalid DF timecode"):
            Timecode.from_string('00:01:00;00', FPS_29_97)

    def test_invalid_df_minute_1_frame_1(self) -> None:
        """分钟01的帧号01无效"""
        with pytest.raises(ValueError, match="Invalid DF timecode"):
            Timecode.from_string('00:01:00;01', FPS_29_97)

    def test_invalid_df_minute_11_frame_0(self) -> None:
        """分钟11的帧号00无效"""
        with pytest.raises(ValueError, match="Invalid DF timecode"):
            Timecode.from_string('00:11:00;00', FPS_29_97)

    def test_valid_df_10th_minute_frame_0(self) -> None:
        """分钟10（整点）的帧号00有效"""
        tc = Timecode.from_string('00:10:00;00', FPS_29_97)
        assert tc.to_frame_number() == 17982

    def test_invalid_df_5994fps_minute_1_frames(self) -> None:
        """59.94fps DF 分钟01的帧号00-03无效"""
        # 59.94 DF 每分钟跳 4 帧
        with pytest.raises(ValueError, match="Invalid DF timecode"):
            Timecode.from_string('00:01:00;00', FPS_59_94)

        with pytest.raises(ValueError, match="Invalid DF timecode"):
            Timecode.from_string('00:01:00;03', FPS_59_94)


class TestTimecodeAddFrames:
    """帧数加减测试"""

    def test_add_frames_positive(self) -> None:
        """添加正帧数"""
        tc = Timecode.from_string('00:00:00;00', FPS_29_97)
        tc2 = tc.add_frames(30)
        assert tc2.to_string() == '00:00:01;00'

    def test_add_frames_hour(self) -> None:
        """添加帧数跨小时"""
        tc = Timecode.from_string('00:00:00;00', FPS_29_97)
        tc2 = tc.add_frames(107892)
        assert tc2.to_string() == '01:00:00;00'

    def test_subtract_frames(self) -> None:
        """减去帧数"""
        tc = Timecode.from_string('00:00:01;00', FPS_29_97)
        tc2 = tc.subtract_frames(30)
        assert tc2.to_string() == '00:00:00;00'

    def test_add_frames_negative_result_error(self) -> None:
        """添加负帧数导致负结果"""
        tc = Timecode.from_string('00:00:00;00', FPS_29_97)
        with pytest.raises(ValueError, match="negative frame"):
            tc.add_frames(-1)


class TestTimecodeComparison:
    """时间码比较测试"""

    def test_less_than(self) -> None:
        """小于比较"""
        tc1 = Timecode.from_string('00:00:00;00', FPS_29_97)
        tc2 = Timecode.from_string('00:00:01;00', FPS_29_97)
        assert tc1 < tc2

    def test_greater_than(self) -> None:
        """大于比较"""
        tc1 = Timecode.from_string('01:00:00;00', FPS_29_97)
        tc2 = Timecode.from_string('00:00:00;00', FPS_29_97)
        assert tc1 > tc2

    def test_equal(self) -> None:
        """相等比较"""
        tc1 = Timecode.from_string('01:00:00;00', FPS_29_97)
        tc2 = Timecode.from_frame_number(107892, FPS_29_97, TimecodeType.DROP_FRAME)
        assert tc1 == tc2

    def test_frame_diff(self) -> None:
        """帧差计算"""
        tc1 = Timecode.from_string('00:00:00;00', FPS_29_97)
        tc2 = Timecode.from_string('00:00:01;00', FPS_29_97)
        assert (tc2 - tc1) == 30

    def test_compare_different_fps_error(self) -> None:
        """比较不同帧率的时间码"""
        tc1 = Timecode.from_string('00:00:00:00', FPS_24)
        tc2 = Timecode.from_string('00:00:00;00', FPS_29_97)
        with pytest.raises(ValueError, match="different frame rates"):
            tc1 < tc2


class TestTimecodeToString:
    """时间码字符串输出测试"""

    def test_ndf_output_colon(self) -> None:
        """NDF 输出使用冒号"""
        tc = Timecode(1, 0, 0, 0, FPS_24, TimecodeType.NON_DROP_FRAME)
        assert tc.to_string() == '01:00:00:00'

    def test_df_output_semicolon(self) -> None:
        """DF 输出使用分号"""
        tc = Timecode(1, 0, 0, 0, FPS_29_97, TimecodeType.DROP_FRAME)
        assert tc.to_string() == '01:00:00;00'

    def test_zero_padding(self) -> None:
        """零填充"""
        tc = Timecode(1, 2, 3, 4, FPS_24, TimecodeType.NON_DROP_FRAME)
        assert tc.to_string() == '01:02:03:04'


class TestTimecodeValidation:
    """时间码验证测试"""

    def test_invalid_hours(self) -> None:
        """无效小时"""
        with pytest.raises(ValueError, match="Invalid hours"):
            Timecode(24, 0, 0, 0, FPS_24)

    def test_invalid_minutes(self) -> None:
        """无效分钟"""
        with pytest.raises(ValueError, match="Invalid minutes"):
            Timecode(0, 60, 0, 0, FPS_24)

    def test_invalid_seconds(self) -> None:
        """无效秒"""
        with pytest.raises(ValueError, match="Invalid seconds"):
            Timecode(0, 0, 60, 0, FPS_24)

    def test_invalid_frames(self) -> None:
        """无效帧号"""
        with pytest.raises(ValueError, match="Invalid frames"):
            Timecode(0, 0, 0, 30, FPS_24)  # 24fps 最大帧号是 23


class TestDROP_FRAME_FPS_RATIOS:
    """丢帧帧率常量测试"""

    def test_ratios_exist(self) -> None:
        """测试丢帧帧率集合"""
        assert (30000, 1001) in DROP_FRAME_FPS_RATIOS
        assert (60000, 1001) in DROP_FRAME_FPS_RATIOS

    def test_ndf_ratios_not_in_set(self) -> None:
        """NDF 帧率不在集合中"""
        assert (24000, 1001) not in DROP_FRAME_FPS_RATIOS
        assert (24, 1) not in DROP_FRAME_FPS_RATIOS
        assert (25, 1) not in DROP_FRAME_FPS_RATIOS