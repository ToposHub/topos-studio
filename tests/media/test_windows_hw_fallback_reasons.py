"""W0-05：Windows 回退语义 reason 词表钉值测试（计划 §4 代码级展开）。

每个失败点一个显式 reason 常量，禁止裸字符串散落。本套件把词表值、
参数化格式、以及 capability/bridge/decoder 各失败点的产出 reason
全部钉死 —— 后续 W1~W3 真机实现时契约不得漂移。
"""

from __future__ import annotations

import pytest

from src.shared.media.windows_hw import capability as cap_module
from src.shared.media.windows_hw import gpu_capability as gpu_module
from src.shared.media.windows_hw.d3d11_decoder import D3D11Decoder
from src.shared.media.windows_hw.d3d11_gl_bridge import D3D11GLBridge
from src.shared.media.windows_hw.reasons import (
    FallbackReason,
    codec_unsupported_reason,
    param,
    reason_prefix,
)
from src.shared.media.windows_hw.windows_zero_copy_probe import run_probe


class TestFallbackReasonVocabulary:
    def test_values_are_stable_snake_case(self):
        """词表值即落盘字符串（诊断链/日志/JSON 消费），不得改写。"""
        expected = {
            "not_windows", "env_zero_copy_off", "env_force_cpu_upload",
            "not_probed", "gpu_probe_failed", "probe_error",
            "nvidia_driver_version_unavailable", "driver_below_gate",
            "interop_not_yet_implemented", "interop_init_failed",
            "wgl_nv_dx_interop_unavailable", "adapter_mismatch",
            "codec_unsupported_by_hw", "encoder_hw_unavailable",
            "cuda_unavailable", "not_yet_implemented",
        }
        assert {r.value for r in FallbackReason} == expected

    def test_param_format(self):
        assert (
            param(FallbackReason.GPU_PROBE_FAILED, "factory_failed:hr=0x887A0001")
            == "gpu_probe_failed:factory_failed:hr=0x887A0001"
        )

    def test_reason_prefix_ignores_detail_colons(self):
        assert reason_prefix("codec_unsupported_by_hw:h264/yuv422p10le") == (
            "codec_unsupported_by_hw"
        )
        assert reason_prefix("not_windows") == "not_windows"

    def test_codec_unsupported_reason(self):
        assert codec_unsupported_reason("h264") == "codec_unsupported_by_hw:h264"
        assert (
            codec_unsupported_reason("h264", "yuv422p10le")
            == "codec_unsupported_by_hw:h264/yuv422p10le"
        )


class TestCapabilityReasonsFromVocabulary:
    """capability.py 各失败门产出的 reason 全部来自词表。"""

    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        cap_module._cached_capability = None
        yield
        cap_module._cached_capability = None

    def _win_env(self, monkeypatch):
        monkeypatch.setattr(cap_module.platform, "system", lambda: "Windows")
        monkeypatch.setattr(cap_module, "_zero_copy_env_var", "auto")
        monkeypatch.setattr(cap_module, "_force_cpu_upload", False)

    def test_not_windows(self, monkeypatch):
        monkeypatch.setattr(cap_module.platform, "system", lambda: "Darwin")
        cap = cap_module.probe_zero_copy_capability()
        assert cap.reason == FallbackReason.NOT_WINDOWS.value

    def test_env_gates(self, monkeypatch):
        monkeypatch.setattr(cap_module.platform, "system", lambda: "Windows")
        monkeypatch.setattr(cap_module, "_zero_copy_env_var", "off")
        monkeypatch.setattr(cap_module, "_force_cpu_upload", False)
        assert cap_module.probe_zero_copy_capability().reason == (
            FallbackReason.ENV_ZERO_COPY_OFF.value
        )
        cap_module._cached_capability = None
        monkeypatch.setattr(cap_module, "_zero_copy_env_var", "auto")
        monkeypatch.setattr(cap_module, "_force_cpu_upload", True)
        assert cap_module.probe_zero_copy_capability().reason == (
            FallbackReason.ENV_FORCE_CPU_UPLOAD.value
        )

    def test_gpu_probe_failed_parametrized(self, monkeypatch):
        self._win_env(monkeypatch)
        gpu = gpu_module.WindowsGpuCapability(probe_ok=False, reason="factory_failed:hr=0x887A0001")
        monkeypatch.setattr(gpu_module, "probe_gpu_capability", lambda: gpu)
        cap = cap_module.probe_zero_copy_capability()
        assert reason_prefix(cap.reason) == FallbackReason.GPU_PROBE_FAILED.value
        assert "factory_failed" in cap.reason

    def test_nvidia_driver_gate_reason(self, monkeypatch):
        self._win_env(monkeypatch)
        gpu = gpu_module.WindowsGpuCapability(
            probe_ok=True, reason="ok",
            adapters=[gpu_module.AdapterInfo(
                description="NVIDIA", vendor_id=0x10DE, vendor_name="nvidia")],
            driver_version="", primary_vendor="nvidia",
        )
        monkeypatch.setattr(gpu_module, "probe_gpu_capability", lambda: gpu)
        assert cap_module.probe_zero_copy_capability().reason == (
            FallbackReason.NVIDIA_DRIVER_VERSION_UNAVAILABLE.value
        )

    def test_wgl_interop_unavailable_reason(self, monkeypatch):
        self._win_env(monkeypatch)
        gpu = gpu_module.WindowsGpuCapability(
            probe_ok=True, reason="ok",
            adapters=[gpu_module.AdapterInfo(
                description="AMD", vendor_id=0x1002, vendor_name="amd")],
            primary_vendor="amd",
        )
        monkeypatch.setattr(gpu_module, "probe_gpu_capability", lambda: gpu)
        monkeypatch.setattr(cap_module, "_detect_wgl_nv_dx_interop", lambda: False)
        assert cap_module.probe_zero_copy_capability().reason == (
            FallbackReason.WGL_NV_DX_INTEROP_UNAVAILABLE.value
        )


class TestSkeletonReasonContract:
    """W0-02/W0-05：骨架的失败点必须带词表 reason，真机实现时不得漂移。"""

    def test_decoder_open_reason(self):
        decoder = D3D11Decoder()
        assert decoder.last_fallback_reason == FallbackReason.NOT_PROBED.value
        assert decoder.open("x.mp4") is False
        assert decoder.last_fallback_reason == FallbackReason.NOT_YET_IMPLEMENTED.value

    def test_bridge_reason_and_fail_count(self):
        bridge = D3D11GLBridge(ring_size=4)
        assert bridge.initialize() is False
        assert bridge.last_fallback_reason == (
            FallbackReason.INTEROP_NOT_YET_IMPLEMENTED.value
        )
        assert bridge.bind_texture(0, 0) is None
        assert bridge.bind_texture(0, 1) is None
        assert bridge.last_fallback_reason == (
            FallbackReason.INTEROP_NOT_YET_IMPLEMENTED.value
        )

    def test_probe_skeleton_reason(self, monkeypatch):
        monkeypatch.setattr(
            __import__(
                "src.shared.media.windows_hw.windows_zero_copy_probe",
                fromlist=["platform"],
            ).platform,
            "system",
            lambda: "Darwin",
        )
        result = run_probe("x.mp4")
        assert result.fallback_reason == FallbackReason.NOT_WINDOWS.value
        assert result.zero_copy is False
