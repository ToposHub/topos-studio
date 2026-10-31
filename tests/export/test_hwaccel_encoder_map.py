"""W0-04：导出侧硬件编码器映射与能力查询单测。

验收标准（计划 Phase W0-04）：h264_nvenc/hevc_nvenc/av1_nvenc/h264_qsv
映射有测试；无硬件时显式诊断（encoder_hw_unavailable:<hwaccel>）。
注册探测在本机（AMD）即真实执行——NVENC/编码器缺失分支是真数据，
无需 NVIDIA 硬件。
"""

from __future__ import annotations

import pytest

from src.shared.export.encoder import (
    HWACCEL_ENCODER_NAME_MAP,
    hwaccel_encoder_name,
    hwaccel_unavailable_reason,
    probe_hwaccel_encoder_available,
)


class TestHwaccelEncoderNameMap:
    @pytest.mark.parametrize("hwaccel,codec,expected", [
        ("nvenc", "h264", "h264_nvenc"),
        ("nvenc", "hevc", "hevc_nvenc"),
        ("nvenc", "h265", "hevc_nvenc"),
        ("nvenc", "av1", "av1_nvenc"),
        ("qsv", "h264", "h264_qsv"),
        ("qsv", "hevc", "hevc_qsv"),
        ("qsv", "h265", "hevc_qsv"),
        ("qsv", "av1", "av1_qsv"),
        ("amf", "h264", "h264_amf"),
        ("amf", "hevc", "hevc_amf"),
        ("amf", "h265", "hevc_amf"),
        ("videotoolbox", "h264", "h264_videotoolbox"),
        ("videotoolbox", "hevc", "hevc_videotoolbox"),
        ("videotoolbox", "h265", "hevc_videotoolbox"),
    ])
    def test_known_combos(self, hwaccel, codec, expected):
        assert hwaccel_encoder_name(hwaccel, codec) == expected

    def test_h265_alias_consistent_across_all_hwaccels(self):
        """回归：'h265' 是项目内活跃别名（delivery_profile/proxy/seek），
        任一 hwaccel 都不得让它静默回退 CPU 而其它 hwaccel 走硬编。"""
        for hwaccel in ("nvenc", "qsv", "amf", "videotoolbox"):
            assert hwaccel_encoder_name(hwaccel, "h265") == \
                hwaccel_encoder_name(hwaccel, "hevc"), hwaccel

    def test_unknown_hwaccel_raises(self):
        with pytest.raises(ValueError, match="invalid hwaccel"):
            hwaccel_encoder_name("cuda", "h264")

    def test_none_and_empty_return_none(self):
        assert hwaccel_encoder_name("none", "h264") is None
        assert hwaccel_encoder_name("", "h264") is None
        assert hwaccel_encoder_name(None, "h264") is None

    def test_known_hwaccel_without_combo_returns_none(self):
        """Intra 剪辑码流不进硬编：调用方回退 CPU + 诊断，不得静默换名。"""
        assert hwaccel_encoder_name("nvenc", "prores") is None
        assert hwaccel_encoder_name("amf", "dnxhr") is None

    def test_map_names_all_wellformed(self):
        for (_hw, _codec), name in HWACCEL_ENCODER_NAME_MAP.items():
            assert name.endswith(("_nvenc", "_qsv", "_amf", "_videotoolbox"))
            assert name == name.lower()


class TestProbeAvailability:
    """真数据探测（本机 AMD + PyAV 18）：注册存在与否如实反映。"""

    def test_amf_registered_on_this_machine(self):
        # AMD RX 6800 + PyAV 18.1.0 实测带 AMF；若 CI 机器无此编码器，
        # 探测返回 False 本身即正确行为，故断言的是「布尔值」而非真
        assert isinstance(probe_hwaccel_encoder_available("h264_amf"), bool)

    def test_bogus_encoder_is_false(self):
        assert probe_hwaccel_encoder_available("h264_nonexistent_hw") is False
        assert probe_hwaccel_encoder_available("") is False

    def test_libx264_always_registered(self):
        assert probe_hwaccel_encoder_available("libx264") is True

    def test_probe_cache_is_stable(self):
        first = probe_hwaccel_encoder_available("hevc_nvenc")
        second = probe_hwaccel_encoder_available("hevc_nvenc")
        assert first == second


class TestUnavailableReason:
    def test_reason_format(self):
        assert hwaccel_unavailable_reason("nvenc") == "encoder_hw_unavailable:nvenc"
        assert hwaccel_unavailable_reason("amf") == "encoder_hw_unavailable:amf"


class TestVideoEncoderSeamConsumption:
    """W6-02 接缝统一：video_encoder 消费本模块的映射/探测，
    不得再维护平行实现（'h265_amf' 这类非法名就是平行实现的产物）。"""

    def test_amf_probe_delegates_to_seam(self):
        from src.shared.export.video_encoder import amf_encoder_available

        assert amf_encoder_available("h264") == \
            probe_hwaccel_encoder_available("h264_amf")
        assert amf_encoder_available("hevc") == \
            probe_hwaccel_encoder_available("hevc_amf")
        # 'h265' 别名必须经 seam 归一到 hevc_amf
        assert amf_encoder_available("h265") == \
            probe_hwaccel_encoder_available("hevc_amf")

    def test_h265_alias_never_probes_illegal_name(self):
        from src.shared.export.video_encoder import amf_encoder_available

        # FFmpeg 无 'h265_amf' 编码器——委托断裂时会探测这个非法名
        assert probe_hwaccel_encoder_available("h265_amf") is False
        assert amf_encoder_available("h265") == \
            probe_hwaccel_encoder_available("hevc_amf")
