"""W1-03 / W2-01 / W2-02：Windows vendor 感知解码路由与适配器匹配测试。

核心契约（AMD/Intel 机器上实测钉死）：
- NVDEC 判定只信真实 NVIDIA GPU，不信 ffmpeg CLI 编译项（否则 gyan/
  BtbN 构建会把 AMD 机器误判 NVDEC 可用 → 默认选 nvdec → 静默软解）；
- Windows 非 NVIDIA 机器的平台默认解码器是 D3D11VA；
- AV1 在 D3D11VA/NVDEC 矩阵内（实际生效与否由 open() 首帧验证裁决）；
- adapter 匹配：LUID 相等才放行 interop，不等则显式 adapter_mismatch。
"""

from __future__ import annotations

import platform

import pytest

from src.shared.media import hwaccel as hw_module
from src.shared.media.hwaccel import HardwareCapability, HardwareDecoder
from src.shared.media.windows_hw import gpu_capability as gpu_module
from src.shared.media.windows_hw.gpu_capability import (
    AdapterInfo,
    check_adapter_match,
    decode_render_adapter_report,
    pick_primary_hardware_adapter,
)


@pytest.fixture(autouse=True)
def _reset_hwaccel_caches():
    HardwareCapability._detected_decoders = None
    HardwareCapability._nvdec_available = None
    HardwareCapability._qsv_available = None
    yield
    HardwareCapability._detected_decoders = None
    HardwareCapability._nvdec_available = None
    HardwareCapability._qsv_available = None


def _win(monkeypatch, *, nvidia: bool, ffmpeg_cli_cuda: str = "cuda\n"):
    monkeypatch.setattr(hw_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(HardwareCapability, "has_nvidia_gpu",
                        staticmethod(lambda: nvidia))
    # 模拟「ffmpeg CLI 编译了 cuda/nvdec」的常见发行版构建
    monkeypatch.setattr(hw_module, "_ffmpeg_hwaccels_output",
                        lambda: ffmpeg_cli_cuda)
    monkeypatch.setattr(HardwareCapability, "_check_qsv", staticmethod(lambda: False))


class TestNvdecDetectionVendorStrict:
    def test_amd_machine_with_cuda_ffmpeg_is_not_nvdec(self, monkeypatch):
        """W2-02 核心：ffmpeg CLI 有 cuda ≠ 有 N 卡（AMD 机器误判源）。"""
        _win(monkeypatch, nvidia=False, ffmpeg_cli_cuda="cuda\nnvdec\nqsv\n")
        assert HardwareCapability._check_nvdec() is False
        assert HardwareCapability.has_nvidia_gpu() is False

    def test_nvidia_machine_is_nvdec(self, monkeypatch):
        _win(monkeypatch, nvidia=True, ffmpeg_cli_cuda="")
        assert HardwareCapability._check_nvdec() is True


class TestQsvDetectionVendorStrict:
    def test_qsv_requires_real_intel_dxgi_adapter(self, monkeypatch):
        """FFmpeg 编译了 QSV 不等于当前 Windows 有 Intel QSV 设备。"""
        monkeypatch.setattr(hw_module.platform, "system", lambda: "Windows")
        monkeypatch.setattr(hw_module, "_ffmpeg_hwaccels_output",
                            lambda: "qsv\n")
        gpu = gpu_module.WindowsGpuCapability(
            probe_ok=True,
            reason="ok",
            adapters=[AdapterInfo(
                description="NVIDIA GeForce RTX 4090",
                vendor_id=0x10DE,
                vendor_name="nvidia",
            )],
            primary_vendor="nvidia",
        )
        monkeypatch.setattr(gpu_module, "probe_gpu_capability", lambda: gpu)
        assert HardwareCapability._check_qsv() is False

    def test_qsv_accepts_real_intel_dxgi_adapter(self, monkeypatch):
        monkeypatch.setattr(hw_module.platform, "system", lambda: "Windows")
        gpu = gpu_module.WindowsGpuCapability(
            probe_ok=True,
            reason="ok",
            adapters=[AdapterInfo(
                description="Intel(R) UHD Graphics 770",
                vendor_id=0x8086,
                vendor_name="intel",
            )],
            primary_vendor="intel",
        )
        monkeypatch.setattr(gpu_module, "probe_gpu_capability", lambda: gpu)
        assert HardwareCapability._check_qsv() is True


class TestWindowsPlatformDefault:
    def test_amd_machine_defaults_to_d3d11va(self, monkeypatch):
        _win(monkeypatch, nvidia=False)
        available = HardwareCapability.detect_available_decoders()
        assert HardwareDecoder.D3D11VA in available
        assert HardwareDecoder.NVDEC not in available
        assert HardwareCapability.get_platform_default() is HardwareDecoder.D3D11VA

    def test_nvidia_machine_defaults_to_nvdec(self, monkeypatch):
        _win(monkeypatch, nvidia=True)
        assert HardwareCapability.get_platform_default() is HardwareDecoder.NVDEC

    def test_best_decoder_av1_on_amd_is_d3d11va(self, monkeypatch):
        """W2-01：AV1 进矩阵后，AMD 机器路由到 D3D11VA（生效与否由
        open() 首帧验证按机裁决，不误路由到 nvdec）。"""
        _win(monkeypatch, nvidia=False)
        assert HardwareCapability.get_best_decoder_for_codec("av1") is (
            HardwareDecoder.D3D11VA)

    def test_av1_in_codec_matrix(self):
        assert "av1" in hw_module.HWACCEL_CODEC_SUPPORT[HardwareDecoder.D3D11VA]
        assert "av1" in hw_module.HWACCEL_CODEC_SUPPORT[HardwareDecoder.NVDEC]

    def test_422_never_in_windows_hw_matrix(self):
        """Windows 解码单元（D3D11VA/NVDEC）不吃 intra 剪辑码流；
        VIDEOTOOLBOX 的 prores 是 macOS 专属硬件能力，不在本约束内。"""
        for decoder in (HardwareDecoder.D3D11VA, HardwareDecoder.NVDEC,
                        HardwareDecoder.QSV):
            decoders_set = hw_module.HWACCEL_CODEC_SUPPORT[decoder]
            assert "prores" not in decoders_set
            assert "dnxhr" not in decoders_set


class TestAdapterMatch:
    def _amd(self, luid: str = "000110E3-00000000") -> AdapterInfo:
        return AdapterInfo(description="AMD Radeon RX 6800", vendor_id=0x1002,
                           device_id=0x73BF, vendor_name="amd", adapter_luid=luid)

    def test_basic_render_driver_skipped(self):
        basic = AdapterInfo(description="Microsoft Basic Render Driver",
                            vendor_id=0x1414, vendor_name="unknown")
        amd = self._amd()
        assert pick_primary_hardware_adapter([basic]) is None
        assert pick_primary_hardware_adapter([basic, amd]) is amd

    def test_match_and_mismatch_reasons(self):
        ok, reason = check_adapter_match("AAA-111", "AAA-111")
        assert ok is True and reason == ""
        ok, reason = check_adapter_match("AAA-111", "BBB-222")
        assert ok is False
        assert reason == "adapter_mismatch:AAA-111|BBB-222"

    def test_report_unprobed_render_is_honest(self, monkeypatch):
        """GL 侧 LUID 未传入时如实报 unprobed，不猜。"""
        gpu = gpu_module.WindowsGpuCapability(
            probe_ok=True, reason="ok", adapters=[self._amd()],
            primary_vendor="amd")
        monkeypatch.setattr(gpu_module, "probe_gpu_capability", lambda: gpu)
        report = decode_render_adapter_report()
        assert report["match"] is False
        assert report["reason"] == "render_adapter_unprobed"

    def test_report_match_on_same_luid(self, monkeypatch):
        gpu = gpu_module.WindowsGpuCapability(
            probe_ok=True, reason="ok", adapters=[self._amd("L1-0")],
            primary_vendor="amd")
        monkeypatch.setattr(gpu_module, "probe_gpu_capability", lambda: gpu)
        report = decode_render_adapter_report(render_adapter_luid="L1-0")
        assert report["match"] is True
        assert report["reason"] == ""

    @pytest.mark.skipif(platform.system() != "Windows",
                        reason="needs real Windows (W1-03)")
    def test_real_machine_single_adapter_is_consistent(self):
        """本机（单硬件适配器）实测：decode 侧适配器可解析出 LUID。"""
        report = decode_render_adapter_report()
        assert report["probe_ok"] is True
        assert report["decode_adapter"] is not None
        assert report["decode_adapter_luid"]
