"""W0-03：BackendCapabilities 平台维度（d3d11/cuda/directml...）测试。

验收标准（计划 Phase W0-03）：
- 四维快照含 d3d11/cuda/directml 维度（platform 词典，可序列化）；
- backend_selector 在非 Windows 上对 d3d11/cuda 显式报 ``not_windows``
  类 reason（不报未知错误）；
- Windows 分支解码与零拷贝解耦（D3D11VA adapter 级可用 ≠ interop 可用）。
"""

from __future__ import annotations

import json

import pytest

from src.shared.gpu.backend_selector import resolve_backend_plan
from src.shared.gpu.backend_types import (
    BackendSelection,
    ComputeBackendKind,
    DecodeBackendKind,
    GraphicsBackendKind,
)
from src.shared.gpu.capabilities import (
    detect_backend_capabilities,
    detect_platform_video_kinds,
)


_PLATFORM_KEYS = {
    "d3d11", "d3d11va", "nvdec", "qsv", "amf", "cuda", "directml",
}


class TestPlatformKindsNonWindows:
    def test_all_kinds_report_not_windows(self):
        caps = detect_platform_video_kinds(system="Darwin")
        assert set(caps) == _PLATFORM_KEYS
        for key, cap in caps.items():
            assert cap.available is False, key
            assert cap.reason == "not_windows", key
            assert cap.evidence == "static", key

    def test_kinds_use_existing_vocabulary(self):
        """复用 P7-06/07/08 的 graphics/compute/decode 词汇表，不发明新词。"""
        caps = detect_platform_video_kinds(system="Linux")
        assert caps["d3d11"].kind is GraphicsBackendKind.D3D11
        assert caps["d3d11va"].kind is DecodeBackendKind.D3D11VA
        assert caps["nvdec"].kind is DecodeBackendKind.NVDEC
        assert caps["qsv"].kind is DecodeBackendKind.QSV
        assert caps["amf"].kind is DecodeBackendKind.AMF
        assert caps["cuda"].kind is ComputeBackendKind.CUDA
        assert caps["directml"].kind is ComputeBackendKind.DIRECTML


@pytest.fixture()
def _win_gpu(monkeypatch):
    """AMD RX 6800 + Intel iGPU 双适配器注入（真机形态）。"""
    from src.shared.media.windows_hw.gpu_capability import (
        AdapterInfo, WindowsGpuCapability,
    )

    gpu = WindowsGpuCapability(
        probe_ok=True, reason="ok",
        adapters=[
            AdapterInfo(description="AMD Radeon RX 6800", vendor_id=0x1002,
                        device_id=0x73BF, vendor_name="amd"),
            AdapterInfo(description="Intel UHD", vendor_id=0x8086,
                        vendor_name="intel"),
        ],
        primary_vendor="amd",
    )
    monkeypatch.setattr(
        "src.shared.media.windows_hw.gpu_capability.probe_gpu_capability",
        lambda: gpu,
    )
    return gpu


class TestPlatformKindsWindows:

    def test_amd_machine_enables_d3d11va_and_amf_only(self, _win_gpu):
        caps = detect_platform_video_kinds(system="Windows")
        assert caps["d3d11va"].available is True
        assert caps["d3d11va"].zero_copy is False  # download 路径，非零拷贝
        assert caps["amf"].available is True
        assert caps["qsv"].available is True  # 有 Intel 适配器（混合平台）
        assert caps["nvdec"].available is False
        assert caps["nvdec"].reason == "cuda_unavailable"
        assert caps["cuda"].available is False
        assert caps["d3d11"].available is False
        assert caps["d3d11"].reason == "not_yet_implemented"
        assert caps["directml"].available is False
        assert caps["directml"].reason == "not_probed"

    def test_probe_failure_is_parametrized(self, monkeypatch):
        from src.shared.media.windows_hw import gpu_capability as gpu_module
        from src.shared.media.windows_hw.gpu_capability import WindowsGpuCapability

        gpu = WindowsGpuCapability(probe_ok=False, reason="factory_failed:hr=0x80004002")
        monkeypatch.setattr(
            "src.shared.media.windows_hw.gpu_capability.probe_gpu_capability",
            lambda: gpu,
        )
        caps = detect_platform_video_kinds(system="Windows")
        assert caps["d3d11va"].available is False
        assert caps["d3d11va"].reason == "gpu_probe_failed:factory_failed:hr=0x80004002"
        # 回归：probe 失败时连 vendor 都未知，nvdec/cuda 不得猜
        # cuda_unavailable（探测失败 ≠ 能力缺席）
        assert caps["nvdec"].reason.startswith("gpu_probe_failed")
        assert caps["cuda"].reason.startswith("gpu_probe_failed")

    def test_probe_ok_without_hardware_adapter_never_reports_reason_ok(self, monkeypatch):
        """回归：probe_ok=True 但只有 Basic Render Driver（unknown
        vendor）时，不可用 reason 绝不能是 "ok"——曾出现
        available=False + reason="ok" 的自相矛盾诊断（VM/远程会话形态）。"""
        from src.shared.media.windows_hw.gpu_capability import (
            AdapterInfo, WindowsGpuCapability,
        )
        from src.shared.gpu.capabilities import detect_decode_backend

        gpu = WindowsGpuCapability(
            probe_ok=True, reason="ok",
            adapters=[AdapterInfo(
                description="Microsoft Basic Render Driver",
                vendor_name="unknown")],
            primary_vendor="unknown",
        )
        monkeypatch.setattr(
            "src.shared.media.windows_hw.gpu_capability.probe_gpu_capability",
            lambda: gpu,
        )
        caps = detect_platform_video_kinds(system="Windows")
        for key in ("d3d11va", "qsv", "amf"):
            assert caps[key].available is False, key
            assert caps[key].reason not in ("", "ok"), key
        assert caps["d3d11va"].reason == "vendor_adapter_absent"

        decode = detect_decode_backend(system="Windows")
        assert decode.available is False
        assert decode.reason not in ("", "ok")
        assert decode.reason == "no_hardware_adapter"


class TestSnapshotAndSelector:
    def test_snapshot_serializable_with_platform(self):
        caps = detect_backend_capabilities(system="Darwin")
        data = caps.as_dict()
        assert set(data["platform"]) == _PLATFORM_KEYS
        # 快照可序列化（诊断链落盘要求）
        json.dumps(data)

    def test_explicit_d3d11va_on_macos_reports_not_windows(self):
        caps = detect_backend_capabilities(system="Darwin")
        plan = resolve_backend_plan(
            BackendSelection(decode=DecodeBackendKind.D3D11VA), caps)
        dim = plan.decode
        assert dim.available is False
        assert dim.fallback_reason == "not_windows"

    def test_explicit_dml_on_macos_reports_not_windows(self):
        caps = detect_backend_capabilities(system="Darwin")
        plan = resolve_backend_plan(
            BackendSelection(compute=ComputeBackendKind.DIRECTML), caps)
        assert plan.compute.fallback_reason == "not_windows"

    def test_auto_decode_on_macos_never_selects_windows_kinds(self):
        caps = detect_backend_capabilities(system="Darwin")
        plan = resolve_backend_plan(BackendSelection(), caps)
        assert plan.decode.selected in ("videotoolbox", "cpu")

    def test_windows_decode_decoupled_from_zero_copy(self):
        """AMD 真机语义：D3D11VA 硬解可用，但 zero_copy 不为真。"""
        from src.shared.media.windows_hw.gpu_capability import probe_gpu_capability

        gpu = probe_gpu_capability()
        if not any(a.vendor_name in ("amd", "nvidia", "intel")
                   for a in gpu.adapters):
            pytest.skip("needs real hardware adapter (CI runner: basic display only)")
        caps = detect_backend_capabilities(system="Windows")
        assert caps.decode.kind is DecodeBackendKind.D3D11VA
        assert caps.decode.available is True
        assert caps.decode.zero_copy is False
