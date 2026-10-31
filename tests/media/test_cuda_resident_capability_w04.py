"""W04（计划 W-I04）：设备匹配与平面格式能力门 —— L1 逻辑测试。

覆盖（全平台可跑，cuda.bindings 用替身）：

1. initialize 设备对齐：cuGLGetDevices 关联设备优先（拒绝固定 device 0
   隐含假设）；GL 在非 CUDA 适配器（NO_DEVICE）fail-closed；无 current
   GL + 全机唯一设备 → device 0 与之重合（如实记录）；多设备无法对齐
   → fail-closed；stats 携带设备/驱动身份（能力缓存失效条件）。
2. validate_resident_plane_geometry：4:2:0 半平面拷贝不变量（ndim/shape/
   itemsize/pitch/指针），4:2:2/4:4:4 交错格式逐格拒绝（不错读 UV）。
3. bind 前平面指针设备身份门：非设备内存 / 设备序号不匹配 / 属性查询
   失败 → fail-closed（喂给 W03 降级链）。
4. _convert_frame_planar 平面数门（真实 DLPack 胶囊经 numpy 构造）。

真实 4090 的 md5 对拍/L3 已由 test_windows_cuda_resident.py 与
test_cuda_resident_*_l3.py 覆盖（本文件改动后全绿 = 真机验证）。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.shared.media.windows_hw.cuda_resident import (  # noqa: E402
    CudaGlInteropSession,
    CudaResidentPlane,
    validate_resident_plane_geometry,
)
from tests.media.test_cuda_resident_lifecycle_w02 import (  # noqa: E402
    _FakeTex,
    _install_fake_driver_ns,
    _make_session,
)


def _info(itemsize=1, shape=(2160, 3840), strides=None, ptr=0x1000):
    return {"itemsize": itemsize, "shape": list(shape),
            "strides": list(strides) if strides else None,
            "data_ptr": ptr, "byte_offset": 0}


class TestDeviceAlignment:
    """initialize 的 GL 适配器对齐（W04 核心）。"""

    def test_gl_associated_device_is_used_not_device0(self, monkeypatch):
        drv, _ns = _install_fake_driver_ns(monkeypatch)
        drv.gl_devices = [7]
        session = CudaGlInteropSession()
        monkeypatch.setattr(
            session, "_create_plane_textures",
            lambda w, h, f: (_FakeTex(), _FakeTex()))

        assert session.initialize() is True
        assert drv.created_device == 7  # 不再隐含 CUdevice(0)
        stats = session.stats
        assert stats["device_source"] == 'gl_context'
        assert stats["device_ordinal"] == 7
        assert stats["device_name"] == "Fake CUDA Device"
        assert stats["driver_version"] == 13000

    def test_gl_on_non_cuda_adapter_fails_closed(self, monkeypatch):
        drv, _ns = _install_fake_driver_ns(monkeypatch)
        drv.gl_devices = None
        drv.gl_err = 100  # CUDA_ERROR_NO_DEVICE：GL 在非 CUDA 适配器
        session = CudaGlInteropSession()
        assert session.initialize() is False
        assert "no_cuda_device_for_gl_context" in session.last_fallback_reason
        assert "cuCtxCreate" not in drv.calls  # 上下文未创建

    def test_no_gl_context_single_device_falls_back_with_reason(
            self, monkeypatch):
        drv, _ns = _install_fake_driver_ns(monkeypatch)
        drv.gl_devices = None
        drv.gl_err = 304  # CUDA_ERROR_OPERATING_SYSTEM：无 current GL
        drv.device_count = 1
        session = CudaGlInteropSession()
        monkeypatch.setattr(
            session, "_create_plane_textures",
            lambda w, h, f: (_FakeTex(), _FakeTex()))
        assert session.initialize() is True
        assert drv.created_device == 0
        assert session.stats["device_source"] == 'single_cuda_device'

    def test_multi_device_without_alignment_fails_closed(self, monkeypatch):
        drv, _ns = _install_fake_driver_ns(monkeypatch)
        drv.gl_devices = None
        drv.gl_err = 304
        drv.device_count = 3
        session = CudaGlInteropSession()
        assert session.initialize() is False
        assert "cannot_align_gl_adapter" in session.last_fallback_reason
        assert "cuCtxCreate" not in drv.calls


class TestPlaneGeometryValidator:
    """4:2:0 半平面拷贝不变量（真实 ABI：UV ndim=3 交错对）。"""

    def test_real_abi_shapes_accepted(self):
        # 4090 实测形态：Y [h,w] uint8；UV [h/2, w/2, 2] uint8
        y, uv = validate_resident_plane_geometry(
            3840, 2160, "nv12",
            _info(1, (2160, 3840)),
            _info(1, (1080, 1920, 2)),
        )
        assert (y.row_bytes, y.rows) == (3840, 2160)
        assert (uv.row_bytes, uv.rows) == (3840, 1080)
        # p016le：itemsize 2（MSB 对齐 10bit）
        y2, uv2 = validate_resident_plane_geometry(
            1920, 1080, "p016le",
            _info(2, (1080, 1920)),
            _info(2, (540, 960, 2)),
        )
        assert (y2.row_bytes, uv2.row_bytes) == (3840, 3840)

    def test_pitch_covers_alignment_padding(self):
        # CUDA 分配常见对齐 padding：pitch（元素步长）≥ 行字节
        y, uv = validate_resident_plane_geometry(
            3840, 2160, "nv12",
            _info(1, (2160, 3840), strides=[4096]),
            _info(1, (1080, 1920, 2), strides=[4096]),
        )
        assert y.pitch == 4096 and uv.pitch == 4096

    def test_pitch_below_row_bytes_rejected(self):
        with pytest.raises(ValueError, match="plane_pitch_below_row_bytes"):
            validate_resident_plane_geometry(
                3840, 2160, "nv12",
                _info(1, (2160, 3840), strides=[2048]),
                _info(1, (1080, 1920, 2)),
            )

    def test_itemsize_mismatch_rejected(self):
        with pytest.raises(ValueError, match="plane_itemsize_mismatch"):
            validate_resident_plane_geometry(
                1920, 1080, "p016le",
                _info(1, (1080, 1920)),
                _info(2, (540, 960, 2)),
            )

    def test_plane_too_small_rejected(self):
        with pytest.raises(ValueError, match="y_plane_too_small"):
            validate_resident_plane_geometry(
                3840, 2176, "nv12",  # 高度超出平面
                _info(1, (2160, 3840)),
                _info(1, (1080, 1920, 2)),
            )
        with pytest.raises(ValueError, match="uv_plane_too_small"):
            validate_resident_plane_geometry(
                3840, 2160, "nv12",
                _info(1, (2160, 3840)),
                _info(1, (540, 1920, 2)),
            )

    def test_bad_ndim_and_interleave_depth_rejected(self):
        with pytest.raises(ValueError, match="plane_ndim_invalid"):
            validate_resident_plane_geometry(
                3840, 2160, "nv12",
                _info(1, (2160, 3840, 1)),  # Y 不允许 3-D
                _info(1, (1080, 1920, 2)),
            )
        with pytest.raises(ValueError, match="uv_interleave_depth_invalid"):
            validate_resident_plane_geometry(
                3840, 2160, "nv12",
                _info(1, (2160, 3840)),
                _info(1, (1080, 1920, 3)),  # 3 分量 = 4:4:4 交错
            )

    def test_null_device_pointer_rejected(self):
        with pytest.raises(ValueError, match="plane_device_ptr_null"):
            validate_resident_plane_geometry(
                3840, 2160, "nv12",
                _info(1, (2160, 3840), ptr=0),
                _info(1, (1080, 1920, 2), ptr=0x2000),
            )


class TestBindPointerIdentityGate:
    """bind 前逐平面设备身份核验（喂给 W03 降级链）。"""

    @staticmethod
    def _bind_once(session):
        from src.shared.media.windows_hw.cuda_resident import (
            CudaResidentFrame,
        )
        frame = CudaResidentFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            width=64, height=64, pixel_format="nv12", bit_depth=8,
            planes=(CudaResidentPlane("Y", 0x1000, 64, 64, 64),
                    CudaResidentPlane("UV", 0x2000, 64, 64, 32)),
            av_frame=object(),
        )
        return session.bind_resident_frame(frame)

    def test_non_device_memory_rejected(self, monkeypatch):
        session, drv = _make_session(monkeypatch)
        assert session.initialize() is True
        drv.ptr_memtype = 0  # CU_MEMORYTYPE_HOST
        out = self._bind_once(session)
        assert out is None
        assert "plane_not_device_memory" in session.last_fallback_reason

    def test_device_ordinal_mismatch_rejected(self, monkeypatch):
        session, drv = _make_session(monkeypatch)
        assert session.initialize() is True
        drv.ptr_ordinal = drv.created_device + 5  # 平面在别的设备上
        out = self._bind_once(session)
        assert out is None
        assert "plane_device_mismatch" in session.last_fallback_reason

    def test_pointer_probe_failure_rejected(self, monkeypatch):
        session, drv = _make_session(monkeypatch)
        assert session.initialize() is True
        drv.ptr_fail = True
        out = self._bind_once(session)
        assert out is None
        # 属性查询失败在首个属性（内存类型）上即 fail-closed：
        # err 非成功 → plane_not_device_memory 拒绝（不冒充通过）
        assert "plane_not_device_memory" in session.last_fallback_reason

    def test_matching_pointers_pass(self, monkeypatch):
        session, drv = _make_session(monkeypatch)
        assert session.initialize() is True
        # 默认替身：memtype=device、ordinal=会话设备 → 通过（W03 注入
        # copy_failed 的前提是身份门先放行）
        out = self._bind_once(session)
        assert out is not None


class TestConvertPlaneCountGate:
    """_convert_frame_planar 平面数门（真实 DLPack 胶囊）。"""

    @staticmethod
    def _source():
        from src.shared.media.windows_hw.cuda_resident import (
            WindowsCudaResidentSource,
        )

        src = WindowsCudaResidentSource.__new__(WindowsCudaResidentSource)
        src._path = "/fake_w04.mov"
        src._resident_contract_format = "nv12"
        src._resident_bit_depth = 8
        src._current_frame = 3
        src._video_stream = None
        return src

    @staticmethod
    def _fake_av_frame(w=64, h=64, nplanes=2):
        import numpy as np

        planes = [
            np.zeros((h, w), dtype=np.uint8),          # Y
            np.zeros((h // 2, w // 2, 2), dtype=np.uint8),  # UV
        ]
        if nplanes > 2:
            planes.append(np.zeros((h, w), dtype=np.uint8))  # 4:4:4 第三平面
        return SimpleNamespace(
            width=w, height=h,
            format=SimpleNamespace(name="cuda"),
            planes=planes[:nplanes], pts=3,
        )

    def test_two_plane_nv12_converts(self):
        from src.shared.media.windows_hw.cuda_resident import (
            is_cuda_resident_frame,
        )

        out = self._source()._convert_frame_planar(self._fake_av_frame())
        assert is_cuda_resident_frame(out)
        assert out.planes[0].row_bytes == 64 and out.planes[0].rows == 64
        assert out.planes[1].rows == 32

    def test_three_plane_444_rejected(self):
        from src.shared.media.source import MediaDecodeError

        with pytest.raises(MediaDecodeError, match="planes"):
            self._source()._convert_frame_planar(
                self._fake_av_frame(nplanes=3))
