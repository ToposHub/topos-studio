"""WindowsGpuCapability 测试（计划 Phase 8 P8-01）。

COM 走查层（ctypes/DXGI）只能在真实 Windows 上验证；本套件用假数据
注入走查层，覆盖数据整形与门禁判定路径（vendor 映射、描述解析、
probe 组装、零拷贝门禁不再是常量 False）。
"""

from __future__ import annotations

import platform

import pytest

from src.shared.media.windows_hw import capability as cap_module
from src.shared.media.windows_hw import gpu_capability as gpu_module
from src.shared.media.windows_hw.gpu_capability import (
    AdapterInfo,
    WindowsGpuCapability,
    parse_adapter_desc,
    probe_gpu_capability,
)


def _desc_bytes(description: str, vendor: int, device: int, vmem: int) -> bytes:
    """构造 DXGI_ADAPTER_DESC 原始字节（64-bit 布局，304 字节；LUID 在末尾）。"""
    raw = bytearray(256 + 16 + 24 + 8)
    enc = description.encode("utf-16-le")[:254]
    raw[0:len(enc)] = enc
    raw[256:260] = vendor.to_bytes(4, "little")   # VendorId
    raw[260:264] = device.to_bytes(4, "little")   # DeviceId
    raw[272:280] = vmem.to_bytes(8, "little")     # DedicatedVideoMemory
    raw[296:300] = (0x2A5C1B3F).to_bytes(4, "little")  # AdapterLuid.LowPart
    raw[300:304] = (0x00001234).to_bytes(4, "little")  # AdapterLuid.HighPart
    return bytes(raw)


class TestParseAdapterDesc:
    def test_nvidia_adapter(self):
        info = parse_adapter_desc(_desc_bytes("NVIDIA GeForce RTX", 0x10DE, 0x2684, 24 * 1024 * 1024 * 1024))
        assert info.vendor_name == "nvidia"
        assert info.vendor_id == 0x10DE
        assert info.device_id == 0x2684
        assert info.dedicated_video_memory == 24 * 1024 ** 3
        assert info.to_dict()["dedicated_video_memory_mb"] == 24576
        assert "GeForce" in info.description

    def test_amd_and_intel_mapping(self):
        assert parse_adapter_desc(_desc_bytes("AMD", 0x1002, 1, 0)).vendor_name == "amd"
        assert parse_adapter_desc(_desc_bytes("Intel", 0x8086, 1, 0)).vendor_name == "intel"

    def test_unknown_vendor(self):
        info = parse_adapter_desc(_desc_bytes("Mystery GPU", 0xABCD, 1, 0))
        assert info.vendor_name == "unknown"

    def test_json_shape(self):
        info = AdapterInfo(description="NVIDIA", vendor_id=0x10DE, dedicated_video_memory=1 << 30)
        d = WindowsGpuCapability(adapters=[info], probe_ok=True, reason="ok").to_json()
        assert '"primary_vendor": "unknown"' in d or "nvidia" in d
        assert "0x10DE" in d


class TestProbeGpuCapability:
    def test_non_windows_is_honest(self, monkeypatch):
        monkeypatch.setattr(gpu_module.platform, "system", lambda: "Darwin")
        cap = probe_gpu_capability()
        assert cap.probe_ok is False
        assert cap.reason == "not_windows"

    def test_probe_assembles_adapters(self, monkeypatch):
        """走查层假数据注入：两个适配器（NVIDIA 主 + Intel 核显）。"""
        monkeypatch.setattr(gpu_module.platform, "system", lambda: "Windows")
        monkeypatch.setattr(gpu_module.ctypes, "CDLL", lambda path: object())  # dxgi.dll 加载桩
        descs = [
            _desc_bytes("NVIDIA GeForce RTX 4090", 0x10DE, 0x2684, 24 << 30),
            _desc_bytes("Intel(R) UHD Graphics", 0x8086, 0x9BC0, 0),
        ]
        ptrs = iter([0xDEAD1, 0xDEAD2, 0])
        monkeypatch.setattr(gpu_module, "_create_dxgi_factory", lambda dxgi: 0xBEEF)
        monkeypatch.setattr(gpu_module, "_enum_adapters", lambda f, i: next(ptrs))
        monkeypatch.setattr(gpu_module, "_get_adapter_desc", lambda p: descs[p - 0xDEAD1])
        monkeypatch.setattr(gpu_module, "_com_release", lambda p: None)
        monkeypatch.setattr(gpu_module, "_probe_driver_version_nvml", lambda: "566.36")

        cap = probe_gpu_capability()
        assert cap.probe_ok is True
        assert cap.reason == "ok"
        assert cap.primary_vendor == "nvidia"
        assert cap.driver_version == "566.36"
        assert [a.vendor_name for a in cap.adapters] == ["nvidia", "intel"]
        # last_probe_json 可直接被 capability 门禁记录
        assert "RTX 4090" in cap.to_json()

    def test_probe_no_adapter(self, monkeypatch):
        monkeypatch.setattr(gpu_module.platform, "system", lambda: "Windows")
        monkeypatch.setattr(gpu_module.ctypes, "CDLL", lambda path: object())
        monkeypatch.setattr(gpu_module, "_create_dxgi_factory", lambda dxgi: 0xBEEF)
        monkeypatch.setattr(gpu_module, "_enum_adapters", lambda f, i: 0)
        monkeypatch.setattr(gpu_module, "_com_release", lambda p: None)
        monkeypatch.setattr(gpu_module, "_probe_driver_version_nvml", lambda: "")
        cap = probe_gpu_capability()
        assert cap.probe_ok is True
        assert cap.reason == "no_adapter"
        assert cap.adapters == []

    def test_probe_dxgi_load_failure_is_honest(self, monkeypatch):
        """dxgi.dll 加载失败 → probe_ok False + 原因。

        注入 CDLL 失败而不是依赖宿主平台：在真实 Windows 上 dxgi.dll
        必然加载成功（W1-01 已验证走查层可用），该分支只在异常环境触发。
        """
        monkeypatch.setattr(gpu_module.platform, "system", lambda: "Windows")

        def _raise(path: str):
            raise OSError(f"cannot load {path}")

        monkeypatch.setattr(gpu_module.ctypes, "CDLL", _raise)
        cap = probe_gpu_capability()
        assert cap.probe_ok is False
        assert cap.reason.startswith("dxgi_load_failed")


class TestZeroCopyGateIntegration:
    """P8-01 验收：is_zero_copy_available() 不再恒为 False——verdict 由
    探测数据决定（interop 检测为真时 available 可为 True）。"""

    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        cap_module._cached_capability = None
        yield
        cap_module._cached_capability = None

    def _win_env(self, monkeypatch):
        monkeypatch.setattr(cap_module.platform, "system", lambda: "Windows")
        monkeypatch.setattr(cap_module, "_zero_copy_env_var", "auto")
        monkeypatch.setattr(cap_module, "_force_cpu_upload", False)

    def test_available_when_interop_detected(self, monkeypatch):
        self._win_env(monkeypatch)
        gpu = WindowsGpuCapability(
            probe_ok=True, reason="ok",
            adapters=[AdapterInfo(description="NVIDIA", vendor_id=0x10DE, vendor_name="nvidia")],
            driver_version="566.36", primary_vendor="nvidia",
        )
        monkeypatch.setattr(gpu_module, "probe_gpu_capability", lambda: gpu)
        monkeypatch.setattr(cap_module, "_detect_wgl_nv_dx_interop", lambda: True)
        cap = cap_module.probe_zero_copy_capability()
        assert cap.available is True
        assert cap.interop_method == "wgl_nv_dx_interop"
        assert cap_module.is_zero_copy_available(codec="h264", pixel_format="nv12") is True

    def test_unavailable_when_interop_absent(self, monkeypatch):
        """真实探测结果（扩展不存在）→ False，且 reason 是诊断而非 stub。"""
        self._win_env(monkeypatch)
        gpu = WindowsGpuCapability(
            probe_ok=True, reason="ok",
            adapters=[AdapterInfo(description="NVIDIA", vendor_id=0x10DE, vendor_name="nvidia")],
            driver_version="566.36", primary_vendor="nvidia",
        )
        monkeypatch.setattr(gpu_module, "probe_gpu_capability", lambda: gpu)
        monkeypatch.setattr(cap_module, "_detect_wgl_nv_dx_interop", lambda: False)
        cap = cap_module.probe_zero_copy_capability()
        assert cap.available is False
        assert cap.reason == "wgl_nv_dx_interop_unavailable"

    def test_wgl_negative_is_not_cached_forever(self, monkeypatch):
        """回归：WGL 负结果只反映「本次调用时无 GL 上下文」的时序，
        不是机器事实——诊断入口在渲染线程建上下文前询问时，曾把
        零拷贝永久锁死为不可用。负结果不得进缓存，正结果照常缓存。"""
        self._win_env(monkeypatch)
        gpu = WindowsGpuCapability(
            probe_ok=True, reason="ok",
            adapters=[AdapterInfo(description="AMD Radeon RX 6800",
                                  vendor_id=0x1002, vendor_name="amd")],
            primary_vendor="amd",
        )
        monkeypatch.setattr(gpu_module, "probe_gpu_capability", lambda: gpu)
        interop_results = iter([False, True, True])
        monkeypatch.setattr(cap_module, "_detect_wgl_nv_dx_interop",
                            lambda: next(interop_results))

        first = cap_module.probe_zero_copy_capability()
        assert first.available is False
        assert first.reason == "wgl_nv_dx_interop_unavailable"

        # 渲染线程建立 GL 上下文后重问：负结果没有被缓存锁死
        second = cap_module.probe_zero_copy_capability()
        assert second.available is True
        assert second.interop_method == "wgl_nv_dx_interop"

        # 正结果是机器事实：缓存命中，不再消耗探测侧状态
        third = cap_module.probe_zero_copy_capability()
        assert third.available is True

    def test_nvidia_without_driver_version_rejected(self, monkeypatch):
        """§8.2：驱动版本读不到时 capability gate 拒绝专属路径。"""
        self._win_env(monkeypatch)
        gpu = WindowsGpuCapability(
            probe_ok=True, reason="ok",
            adapters=[AdapterInfo(description="NVIDIA", vendor_id=0x10DE, vendor_name="nvidia")],
            driver_version="", primary_vendor="nvidia",
        )
        monkeypatch.setattr(gpu_module, "probe_gpu_capability", lambda: gpu)
        cap = cap_module.probe_zero_copy_capability()
        assert cap.available is False
        assert cap.reason == "nvidia_driver_version_unavailable"

    def test_probe_failure_recorded(self, monkeypatch):
        self._win_env(monkeypatch)
        gpu = WindowsGpuCapability(probe_ok=False, reason="factory_failed:hr=0x887A0001")
        monkeypatch.setattr(gpu_module, "probe_gpu_capability", lambda: gpu)
        cap = cap_module.probe_zero_copy_capability()
        assert cap.available is False
        assert "gpu_probe_failed" in cap.reason
        assert "factory_failed" in cap.last_probe_json


@pytest.mark.skipif(platform.system() != "Windows", reason="needs real Windows (W1-01)")
class TestRealWindowsProbe:
    """W1-01 真机收尾：COM 走查层（IID + vtable）在真实驱动上可用。

    2026-09-12 AMD RX 6800 / Win11 26200 实测：修正 IID_IDXGIFactory1
    与 GetDesc vtable 槽位后，适配器描述/VID/DID/显存/LUID 全部正确。
    """

    def test_factory_and_enum_adapters_work(self):
        cap = probe_gpu_capability()
        assert cap.probe_ok is True
        assert cap.reason == "ok"
        assert len(cap.adapters) >= 1
        for a in cap.adapters:
            d = a.to_dict()
            assert isinstance(d["description"], str)
            assert d["vendor_id"].startswith("0x")

    def test_desc_layout_roundtrip_real_driver(self):
        """真机驱动返回的 desc 必须按 304 字节布局解析出合理显存。"""
        from src.shared.media.windows_hw.gpu_capability import (
            _create_dxgi_factory,
            _com_release,
            _enum_adapters,
            _get_adapter_desc,
            parse_adapter_desc,
        )

        dxgi = platform_and_load()
        factory = _create_dxgi_factory(dxgi)
        try:
            adapter = _enum_adapters(factory, 0)
            assert adapter
            try:
                info = parse_adapter_desc(_get_adapter_desc(adapter))
            finally:
                _com_release(adapter)
        finally:
            _com_release(factory)
        # 真实适配器描述非空（含 Basic Render Driver），LUID 非全零
        assert info.description
        assert info.adapter_luid != "00000000-00000000"


def platform_and_load():
    import ctypes

    return ctypes.CDLL("dxgi.dll")
