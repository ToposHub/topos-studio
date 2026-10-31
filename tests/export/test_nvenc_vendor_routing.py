"""W6-01：video_encoder vendor 泛化（NVENC 通道）路由单测。

vendor 探测/格式映射/路由决策全部可注入（monkeypatch
``detect_hw_vendor``），不依赖真机；注册探测走真实 PyAV（与
test_hwaccel_encoder_map 同语义——断言布尔而非真值）。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from src.shared.media.metadata import Rational


def _cfg(**kw):
    from src.shared.export.video_encoder import FFmpegEncoderConfig

    base = dict(
        codec="h264", width=1920, height=1080, fps=Rational(25, 1),
        pix_fmt="yuv420p", gop_size=50,
    )
    base.update(kw)
    return FFmpegEncoderConfig(**base)


@pytest.fixture()
def vendor(monkeypatch):
    """vendor 路由注入器：vendor('nvidia') 后所有 auto 路由走 NVENC。"""
    def set_vendor(name: str) -> None:
        import src.shared.export.video_encoder as ve

        monkeypatch.setattr(ve.sys, "platform", "win32")
        monkeypatch.setattr(ve, "detect_hw_vendor", lambda: name)
        monkeypatch.setattr(ve, "_hw_vendor_cache", name, raising=False)
    return set_vendor


class TestNvencInputPixFmt:
    """NVENC 输入格式门控（纯逻辑，真机实测形态见 probe 归档）。"""

    def test_8bit_420_direct(self):
        for codec in ("h264", "hevc", "av1"):
            assert _cfg(codec=codec).nvenc_input_pix_fmt() == "yuv420p", codec

    def test_10bit_hevc_av1_via_p010le(self):
        assert _cfg(
            codec="hevc", pix_fmt="yuv420p10le",
        ).nvenc_input_pix_fmt() == "p010le"
        assert _cfg(
            codec="av1", pix_fmt="yuv420p10le",
        ).nvenc_input_pix_fmt() == "p010le"

    def test_h264_10bit_unsupported(self):
        assert _cfg(pix_fmt="yuv420p10le").nvenc_input_pix_fmt() is None

    def test_422_and_444_unsupported(self):
        assert _cfg(pix_fmt="yuv422p10le").nvenc_input_pix_fmt() is None
        assert _cfg(pix_fmt="yuv422p").nvenc_input_pix_fmt() is None
        assert _cfg(pix_fmt="yuv444p").nvenc_input_pix_fmt() is None


class TestVendorRouting:
    """auto 模式 vendor → 通道分发（注入探测，不依赖真机）。"""

    def test_hw_vendor_auto_nvidia(self, vendor):
        vendor("nvidia")
        assert _cfg().hw_vendor() == "nvenc"
        assert _cfg(hwaccel="amf").hw_vendor() == "amf"  # 显式优先
        assert _cfg(hwaccel=None).hw_vendor() == ""

    def test_hw_vendor_auto_unknown(self, vendor):
        vendor("")
        assert _cfg().hw_vendor() == ""
        assert _cfg().hw_input_pix_fmt() is None

    def test_hw_input_pix_fmt_dispatch(self, vendor):
        vendor("nvidia")
        assert _cfg(codec="hevc").hw_input_pix_fmt() == "yuv420p"
        # AMF 不收 av1 映射；NVENC 收——同一配置跨 vendor 通道不同
        assert _cfg(codec="av1").hw_input_pix_fmt() == "yuv420p"
        vendor("amd")
        assert _cfg(codec="av1").hw_input_pix_fmt() is None

    def test_get_encoder_name_routes_by_vendor(self, vendor):
        from src.shared.export.video_encoder import (
            amf_encoder_available, nvenc_encoder_available,
        )

        vendor("nvidia")
        assert _cfg().get_encoder_name() == (
            "h264_nvenc" if nvenc_encoder_available("h264") else "libx264")
        assert _cfg(codec="hevc").get_encoder_name() == (
            "hevc_nvenc" if nvenc_encoder_available("hevc") else "libx265")
        vendor("amd")
        assert _cfg().get_encoder_name() == (
            "h264_amf" if amf_encoder_available("h264") else "libx264")
        vendor("")
        assert _cfg().get_encoder_name() == "libx264"


class TestVideoToolboxRouting:
    """macOS H.264/HEVC auto 路由与输入格式门控。"""

    def test_videotoolbox_input_pix_fmt_is_limited_to_8bit_420(self):
        assert _cfg(hwaccel="videotoolbox").hw_input_pix_fmt() == "yuv420p"
        assert _cfg(codec="hevc", hwaccel="videotoolbox").hw_input_pix_fmt() == "yuv420p"
        assert _cfg(codec="hevc", pix_fmt="yuv420p10le",
                    hwaccel="videotoolbox").hw_input_pix_fmt() is None
        assert _cfg(pix_fmt="yuv422p", hwaccel="videotoolbox").hw_input_pix_fmt() is None

    def test_auto_routes_to_videotoolbox_on_macos(self, monkeypatch):
        import src.shared.export.video_encoder as ve

        monkeypatch.setattr(ve.sys, "platform", "darwin")
        monkeypatch.setattr(ve, "videotoolbox_encoder_available", lambda codec: True)
        cfg = _cfg()
        assert cfg.hw_vendor() == "videotoolbox"
        assert cfg.hwaccel_engaged() is True
        assert cfg.get_encoder_name() == "h264_videotoolbox"

    def test_auto_videotoolbox_respects_hdr_and_kill_switch(self, monkeypatch):
        import src.shared.export.video_encoder as ve
        from src.shared.codec.color_metadata_config import ColorMetadataConfig

        monkeypatch.setattr(ve.sys, "platform", "darwin")
        monkeypatch.setattr(ve, "videotoolbox_encoder_available", lambda codec: True)
        hdr = _cfg(color_metadata=ColorMetadataConfig(transfer="smpte2084"))
        assert hdr.hwaccel_engaged() is False
        monkeypatch.setenv("TOPOS_DISABLE_MACOS_HW_ENCODE", "1")
        assert _cfg().hwaccel_engaged() is False

    def test_videotoolbox_crf_maps_to_bitrate(self):
        from src.shared.export.video_encoder import FFmpegVideoEncoder

        encoder = FFmpegVideoEncoder(
            "out.mp4", _cfg(crf=20, hwaccel="videotoolbox"))
        assert encoder._videotoolbox_target_bitrate() == 8_000_000
        options = encoder._build_videotoolbox_options()
        assert options["b"] == "8000k"
        assert options["allow_sw"] == "0"
        assert "crf" not in options

    def test_hardware_probe_uses_actual_output_dimensions(self, monkeypatch):
        from src.shared.export.video_encoder import FFmpegVideoEncoder

        calls = {}

        class FakeContext:
            width = height = 0
            pix_fmt = ""
            options = None
            bit_rate = 0

            def open(self):
                calls["opened"] = (self.width, self.height, self.pix_fmt)

            def encode(self, frame):
                calls["frame"] = (frame.width, frame.height, frame.format)
                return []

        context = FakeContext()
        fake_av = SimpleNamespace(
            codec=SimpleNamespace(
                CodecContext=SimpleNamespace(
                    create=lambda name, mode: context,
                ),
            ),
            VideoFrame=lambda width, height, pixel_format: SimpleNamespace(
                width=width, height=height, format=pixel_format,
            ),
        )
        monkeypatch.setitem(sys.modules, "av", fake_av)
        encoder = FFmpegVideoEncoder(
            "out.mp4", _cfg(crf=20, hwaccel="videotoolbox"))

        assert encoder._probe_encoder_open("h264_videotoolbox") is True
        assert calls["opened"] == (1920, 1080, "yuv420p")
        assert calls["frame"] == (1920, 1080, "yuv420p")
        assert context.options["allow_sw"] == "0"
        assert context.bit_rate == 8_000_000

    def test_engaged_auto_requires_registered_encoder(self, vendor):
        vendor("nvidia")
        from src.shared.export.video_encoder import nvenc_encoder_available

        cfg = _cfg()
        assert cfg.hwaccel_engaged() == nvenc_encoder_available("h264")

    def test_explicit_hwaccel_not_vendor_preflighted(self, vendor):
        """显式点错通道（AMD 机器点 nvenc）不做 vendor 预检——
        可用性裁决在 open() 探针（同 AMF 显式模式先例）。"""
        vendor("amd")
        from src.shared.export.video_encoder import nvenc_encoder_available

        cfg = _cfg(hwaccel="nvenc")
        assert cfg.hwaccel_engaged() == nvenc_encoder_available("h264")


class TestSdrHdrGating:
    def test_auto_hdr_locked_to_cpu(self, vendor):
        from src.shared.codec.color_metadata_config import ColorMetadataConfig

        vendor("nvidia")
        cfg = _cfg(
            codec="hevc", pix_fmt="yuv420p10le",
            color_metadata=ColorMetadataConfig(transfer="smpte2084"),
        )
        assert cfg.hwaccel_engaged() is False

    def test_explicit_hwaccel_allows_hdr(self, vendor):
        from src.shared.codec.color_metadata_config import ColorMetadataConfig
        from src.shared.export.video_encoder import nvenc_encoder_available

        vendor("nvidia")
        cfg = _cfg(
            codec="hevc", pix_fmt="yuv420p10le",
            color_metadata=ColorMetadataConfig(transfer="smpte2084"),
            hwaccel="nvenc",
        )
        assert cfg.hwaccel_engaged() == nvenc_encoder_available("hevc")

    def test_kill_switch(self, vendor, monkeypatch):
        vendor("nvidia")
        monkeypatch.setenv("TOPOS_DISABLE_WIN_HW_ENCODE", "1")
        assert _cfg().hwaccel_engaged() is False


class TestRateControlOptionKeys:
    """码率选项键名回归（2026-09-12 真机实证 ``b:v`` 被静默忽略）。

    PyAV stream.options → avcodec_open2 消费的是 libavcodec AVOption
    正名 ``b``；``b:v`` 是 ffmpeg CLI 流指定符语法，落进 av_dict 后
    被静默忽略——NVENC 码率模式会退回默认码率（~2Mbps 量级）输出，
    无任何报错。本组测试钉住三条硬编/CPU 码率路径的键名。
    """

    @staticmethod
    def _encoder(cfg):
        from src.shared.export.video_encoder import FFmpegVideoEncoder

        return FFmpegVideoEncoder("out.mp4", cfg)

    def test_nvenc_bitrate_uses_flat_b(self):
        enc = self._encoder(_cfg(
            codec="hevc", hwaccel="nvenc", bitrate=20_000_000,
            buffer_size=10_000_000, max_bitrate=20_000_000,
            min_bitrate=20_000_000,
        ))
        opts = enc._build_nvenc_options("hevc")
        assert opts["b"] == "20000k"
        assert "b:v" not in opts
        assert opts["rc"] == "cbr"  # min==max → CBR
        assert opts["maxrate"] == "20000k"
        assert opts["bufsize"] == "10000k"

    def test_nvenc_crf_uses_cq(self):
        enc = self._encoder(_cfg(codec="hevc", hwaccel="nvenc", crf=23))
        opts = enc._build_nvenc_options("hevc")
        assert opts["cq"] == "23"
        assert opts["rc"] == "vbr"
        assert "b" not in opts and "b:v" not in opts

    def test_amf_bitrate_uses_flat_b(self):
        enc = self._encoder(_cfg(
            codec="h264", hwaccel="amf", bitrate=12_000_000,
        ))
        opts = enc._build_amf_options("h264")
        assert opts["b"] == "12000k"
        assert "b:v" not in opts

    def test_av1_cpu_bitrate_not_x265_params(self):
        """libsvtav1 不认 x265-params（真机实证 BRC 停留默认 CRF）。"""
        enc = self._encoder(_cfg(
            codec="av1", bitrate=8_000_000, max_bitrate=8_000_000,
        ))
        opts = enc._build_rate_control_options("av1")
        assert opts == {"b": "8000k", "maxrate": "8000k"}

    def test_hevc_cpu_bitrate_keeps_x265_params(self):
        enc = self._encoder(_cfg(codec="hevc", bitrate=8_000_000))
        opts = enc._build_rate_control_options("hevc")
        assert opts["x265-params"] == "bitrate=8000"


class TestSeamDelegation:
    """nvenc_encoder_available 必须消费 encoder.py seam（同 AMF W6-02）。"""

    def test_nvenc_probe_delegates_to_seam(self):
        from src.shared.export.encoder import probe_hwaccel_encoder_available
        from src.shared.export.video_encoder import nvenc_encoder_available

        assert nvenc_encoder_available("h264") == \
            probe_hwaccel_encoder_available("h264_nvenc")
        assert nvenc_encoder_available("h265") == \
            probe_hwaccel_encoder_available("hevc_nvenc")

    def test_vendor_hwaccel_name_map(self):
        from src.shared.export.video_encoder import vendor_hwaccel_name

        assert vendor_hwaccel_name("nvidia") == "nvenc"
        assert vendor_hwaccel_name("amd") == "amf"
        assert vendor_hwaccel_name("intel") == ""
        assert vendor_hwaccel_name("") == ""


class TestAv1CpuEncoderName:
    """av1 CPU 编码器名按注册探测解析（libaom-av1 缺失的 wheel 修复）。"""

    def test_prefers_registered_svtav1(self, monkeypatch):
        import src.shared.export.encoder as enc_mod
        import src.shared.export.video_encoder as ve

        monkeypatch.setattr(
            enc_mod, "probe_hwaccel_encoder_available",
            lambda name: name == "libsvtav1")
        assert ve.cpu_av1_encoder_name() == "libsvtav1"
        assert _cfg(codec="av1", hwaccel=None).get_encoder_name() == "libsvtav1"

    def test_falls_back_to_libaom_when_svt_absent(self, monkeypatch):
        import src.shared.export.encoder as enc_mod
        import src.shared.export.video_encoder as ve

        monkeypatch.setattr(
            enc_mod, "probe_hwaccel_encoder_available", lambda name: False)
        assert ve.cpu_av1_encoder_name() == "libaom-av1"
        assert _cfg(codec="av1", hwaccel=None).get_encoder_name() == "libaom-av1"
