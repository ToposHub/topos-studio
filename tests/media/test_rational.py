"""
Rational Unit Tests
测试 Rational 时间模型的转换方法。

覆盖：
- 常见帧率（23.976, 24, 25, 29.97, 30, 50, 60fps）
- frame_to_pts / pts_to_frame 转换
- frame_to_seconds / seconds_to_frame 转换
- 有理数运算
- 规范化和约分
- 字符串解析
"""

import pytest
from src.shared.media import Rational, FPS_23_976, FPS_24, FPS_25, FPS_29_97, FPS_30, FPS_50, FPS_60


class TestRationalBasic:
    """Rational 基础功能测试"""

    def test_basic_creation(self) -> None:
        """测试基本创建"""
        r = Rational(24, 1)
        assert r.num == 24
        assert r.den == 1

    def test_reduction(self) -> None:
        """测试约分"""
        r = Rational(24000, 1001)
        assert r.num == 24000
        assert r.den == 1001

        # 测试可约分的情况
        r2 = Rational(48, 2)
        assert r2.num == 24
        assert r2.den == 1

    def test_negative_handling(self) -> None:
        """测试负数处理"""
        # 分母为负时转为分子负
        r = Rational(24, -1)
        assert r.num == -24
        assert r.den == 1

        # 分子为负保持
        r2 = Rational(-24, 1)
        assert r2.num == -24
        assert r2.den == 1

    def test_zero_denominator_error(self) -> None:
        """测试分母为零错误"""
        with pytest.raises(ValueError, match="Denominator cannot be zero"):
            Rational(24, 0)

    def test_to_float(self) -> None:
        """测试浮点转换"""
        assert Rational(24, 1).to_float() == 24.0
        assert Rational(24000, 1001).to_float() == pytest.approx(23.976, rel=1e-4)
        assert Rational(30000, 1001).to_float() == pytest.approx(29.97, rel=1e-4)

    def test_to_fps(self) -> None:
        """测试帧率转换"""
        assert Rational(25, 1).to_fps() == 25.0
        assert Rational(60, 1).to_fps() == 60.0

    def test_string_representation(self) -> None:
        """测试字符串表示"""
        r = Rational(24000, 1001)
        assert str(r) == "24000/1001"
        assert repr(r) == "Rational(24000, 1001)"


class TestRationalFrameConversion:
    """Rational 帧转换测试（覆盖 5+ 种帧率）"""

    def test_frame_to_pts_24fps(self) -> None:
        """测试 24fps 帧到 PTS 转换"""
        fps = FPS_24
        assert fps.frame_to_pts(0) == 0
        assert fps.frame_to_pts(1) == 1
        assert fps.frame_to_pts(100) == 100
        assert fps.frame_to_pts(2400) == 2400  # 100秒

    def test_frame_to_pts_23976fps(self) -> None:
        """测试 23.976fps 帧到 PTS 转换"""
        fps = FPS_23_976
        assert fps.frame_to_pts(0) == 0
        assert fps.frame_to_pts(1) == 1001
        assert fps.frame_to_pts(100) == 100100
        assert fps.frame_to_pts(2398) == 2398 * 1001

    def test_frame_to_pts_25fps(self) -> None:
        """测试 25fps 帧到 PTS 转换"""
        fps = FPS_25
        assert fps.frame_to_pts(0) == 0
        assert fps.frame_to_pts(1) == 1
        assert fps.frame_to_pts(100) == 100
        assert fps.frame_to_pts(2500) == 2500  # 100秒

    def test_frame_to_pts_2997fps(self) -> None:
        """测试 29.97fps 帧到 PTS 转换"""
        fps = FPS_29_97
        assert fps.frame_to_pts(0) == 0
        assert fps.frame_to_pts(1) == 1001
        assert fps.frame_to_pts(100) == 100100
        assert fps.frame_to_pts(2997) == 2997 * 1001

    def test_frame_to_pts_30fps(self) -> None:
        """测试 30fps 帧到 PTS 转换"""
        fps = FPS_30
        assert fps.frame_to_pts(0) == 0
        assert fps.frame_to_pts(1) == 1
        assert fps.frame_to_pts(100) == 100
        assert fps.frame_to_pts(3000) == 3000  # 100秒

    def test_frame_to_pts_50fps(self) -> None:
        """测试 50fps 帧到 PTS 转换"""
        fps = FPS_50
        assert fps.frame_to_pts(0) == 0
        assert fps.frame_to_pts(1) == 1
        assert fps.frame_to_pts(100) == 100
        assert fps.frame_to_pts(5000) == 5000  # 100秒

    def test_frame_to_pts_60fps(self) -> None:
        """测试 60fps 帧到 PTS 转换"""
        fps = FPS_60
        assert fps.frame_to_pts(0) == 0
        assert fps.frame_to_pts(1) == 1
        assert fps.frame_to_pts(100) == 100
        assert fps.frame_to_pts(6000) == 6000  # 100秒

    def test_pts_to_frame_roundtrip(self) -> None:
        """测试 PTS 到帧的往返转换（各种帧率）"""
        fps_list = [FPS_23_976, FPS_24, FPS_25, FPS_29_97, FPS_30, FPS_50, FPS_60]

        for fps in fps_list:
            for frame in [0, 1, 50, 100, 500, 1000]:
                pts = fps.frame_to_pts(frame)
                recovered = fps.pts_to_frame(pts)
                assert recovered == frame, f"Roundtrip failed for {fps}: frame {frame} -> pts {pts} -> frame {recovered}"

    def test_pts_to_frame_negative(self) -> None:
        """测试负 PTS 处理"""
        fps = FPS_24
        assert fps.pts_to_frame(-100) == 0


class TestRationalTimeConversion:
    """Rational 时间转换测试"""

    def test_frame_to_seconds_24fps(self) -> None:
        """测试 24fps 帧到秒转换"""
        fps = FPS_24
        assert fps.frame_to_seconds(0) == 0.0
        assert fps.frame_to_seconds(24) == 1.0
        assert fps.frame_to_seconds(2400) == 100.0
        assert fps.frame_to_seconds(1200) == 50.0

    def test_frame_to_seconds_23976fps(self) -> None:
        """测试 23.976fps 帧到秒转换"""
        fps = FPS_23_976
        assert fps.frame_to_seconds(0) == 0.0
        assert fps.frame_to_seconds(24000) == pytest.approx(1001.0, rel=1e-4)  # 约 1001 秒
        # 第 1000 帧约 41.71 秒
        assert fps.frame_to_seconds(1000) == pytest.approx(41.71, rel=1e-3)

    def test_frame_to_seconds_25fps(self) -> None:
        """测试 25fps 帧到秒转换"""
        fps = FPS_25
        assert fps.frame_to_seconds(0) == 0.0
        assert fps.frame_to_seconds(25) == 1.0
        assert fps.frame_to_seconds(2500) == 100.0

    def test_frame_to_seconds_2997fps(self) -> None:
        """测试 29.97fps 帧到秒转换"""
        fps = FPS_29_97
        assert fps.frame_to_seconds(0) == 0.0
        assert fps.frame_to_seconds(30000) == pytest.approx(1001.0, rel=1e-4)
        # 第 1000 帧约 33.37 秒
        assert fps.frame_to_seconds(1000) == pytest.approx(33.37, rel=1e-3)

    def test_seconds_to_frame_24fps(self) -> None:
        """测试 24fps 秒到帧转换"""
        fps = FPS_24
        assert fps.seconds_to_frame(0.0) == 0
        assert fps.seconds_to_frame(1.0) == 24
        assert fps.seconds_to_frame(100.0) == 2400
        assert fps.seconds_to_frame(50.5) == 1212  # 向下取整

    def test_seconds_to_frame_25fps(self) -> None:
        """测试 25fps 秒到帧转换"""
        fps = FPS_25
        assert fps.seconds_to_frame(0.0) == 0
        assert fps.seconds_to_frame(1.0) == 25
        assert fps.seconds_to_frame(100.0) == 2500

    def test_seconds_to_frame_roundtrip(self) -> None:
        """测试秒到帧的往返转换"""
        fps_list = [FPS_24, FPS_25, FPS_30, FPS_50, FPS_60]

        for fps in fps_list:
            for seconds in [0.0, 1.0, 10.0, 50.0, 100.0]:
                frame = fps.seconds_to_frame(seconds)
                recovered = fps.frame_to_seconds(frame)
                # 允许微小误差（浮点精度）
                assert abs(recovered - seconds) < 0.001, f"Roundtrip failed for {fps}: {seconds}s -> frame {frame} -> {recovered}s"


class TestRationalArithmetic:
    """Rational 运算测试"""

    def test_multiply_int(self) -> None:
        """测试整数乘法"""
        r = Rational(24, 1)
        result = r * 2
        assert result.num == 48
        assert result.den == 1

        r2 = Rational(24000, 1001)
        result2 = r2 * 2
        assert result2.num == 48000
        assert result2.den == 1001

    def test_multiply_rational(self) -> None:
        """测试有理数乘法"""
        r1 = Rational(2, 3)
        r2 = Rational(3, 4)
        result = r1 * r2
        assert result.num == 1  # 6/12 约分后为 1/2
        assert result.den == 2

    def test_divide_int(self) -> None:
        """测试整数除法"""
        r = Rational(24, 1)
        result = r / 2
        # 24/1 / 2 = 24/2 = 12/1 (约分后)
        assert result.num == 12
        assert result.den == 1

        # 不约分的情况
        r2 = Rational(24, 3)
        result2 = r2 / 2
        # 24/3 / 2 = 24/6 = 4/1 (约分后)
        assert result2.num == 4
        assert result2.den == 1

    def test_divide_rational(self) -> None:
        """测试有理数除法"""
        r1 = Rational(1, 2)
        r2 = Rational(1, 4)
        result = r1 / r2
        assert result.num == 2
        assert result.den == 1


class TestRationalFromString:
    """Rational 字符串解析测试"""

    def test_from_string_fraction(self) -> None:
        """测试分数格式解析"""
        r = Rational.from_string("24000/1001")
        assert r.num == 24000
        assert r.den == 1001

        r2 = Rational.from_string("24/1")
        assert r2.num == 24
        assert r2.den == 1

    def test_from_string_float(self) -> None:
        """测试浮点数格式解析"""
        r = Rational.from_string("23.976")
        # 近似值，允许误差
        assert r.to_float() == pytest.approx(23.976, rel=1e-4)

        r2 = Rational.from_string("24")
        assert r2.to_float() == pytest.approx(24.0)

    def test_from_float(self) -> None:
        """测试从浮点数创建"""
        r = Rational.from_float(23.976)
        assert r.to_float() == pytest.approx(23.976, rel=1e-4)

        r2 = Rational.from_float(29.97)
        assert r2.to_float() == pytest.approx(29.97, rel=1e-4)


class TestCommonFrameRates:
    """预定义帧率常量测试"""

    def test_fps_constants(self) -> None:
        """测试帧率常量值"""
        assert FPS_23_976.to_float() == pytest.approx(23.976, rel=1e-4)
        assert FPS_24.to_float() == 24.0
        assert FPS_25.to_float() == 25.0
        assert FPS_29_97.to_float() == pytest.approx(29.97, rel=1e-4)
        assert FPS_30.to_float() == 30.0
        assert FPS_50.to_float() == 50.0
        assert FPS_60.to_float() == 60.0

    def test_common_frame_rates_list(self) -> None:
        """测试帧率列表"""
        from src.shared.media import COMMON_FRAME_RATES
        assert len(COMMON_FRAME_RATES) == 8
        assert FPS_23_976 in COMMON_FRAME_RATES
        assert FPS_24 in COMMON_FRAME_RATES
        assert FPS_25 in COMMON_FRAME_RATES


class TestRationalEdgeCases:
    """Rational 边缘情况测试"""

    def test_zero_numerator(self) -> None:
        """测试分子为零"""
        r = Rational(0, 1)
        assert r.num == 0
        assert r.den == 1
        assert r.to_float() == 0.0

    def test_large_numbers(self) -> None:
        """测试大数值"""
        # 4K 120fps 视频时长计算
        fps = Rational(120, 1)
        total_frames = 14400  # 2 分钟
        duration = fps.frame_to_seconds(total_frames)
        assert duration == 120.0

    def test_gcd_reduction(self) -> None:
        """测试最大公约数约分"""
        # 60000/1001 应保持不变（不可约）
        r = Rational(60000, 1001)
        assert r.num == 60000
        assert r.den == 1001

        # 可约分
        r2 = Rational(60000, 1000)
        assert r2.num == 60
        assert r2.den == 1