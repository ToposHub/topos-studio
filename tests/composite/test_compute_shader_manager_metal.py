"""ComputeShaderManager 的 Metal compute 路径测试（计划 P4-02/P4-04）。

两层：
- Mock 层（无 GL / 无 Metal 环境可跑）：门控语义（should_use_compute_*）、
  Metal 路径委托、P4-04 统一回退通道（失败经 _fallback_callback 通知后
  返回 None）、get_stats 的 compute_api 报告。
- 端到端层（gpu_renderer marker + 真机 Metal）：standalone GL 4.1 context
  上 ComputeShaderManager 自动启用 Metal 桥，模糊结果与 numpy 双趟参考
  一致——即「macOS 大半径效果解锁」的验收测试。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.features.composite.render.compute_shader_manager import (
    COMPUTE_THRESHOLD_RADIUS,
    COMPUTE_THRESHOLD_SIZE,
    ComputeShaderManager,
)
from src.shared.gpu.metal.context import MetalComputeContext


def _mock_gl_ctx(version: str = "4.1 ATI-6") -> MagicMock:
    ctx = MagicMock()
    ctx.info = {"GL_VERSION": version}
    return ctx


class TestMetalPathGating:
    def test_metal_path_enables_should_use_compute(self, monkeypatch):
        """GL compute 不可用 + TOPOS_METAL_COMPUTE=1 → 大半径效果走 Metal。"""
        monkeypatch.setenv("TOPOS_METAL_COMPUTE", "1")
        with patch(
            "src.features.composite.render.compute_shader_manager.MetalComputeBridge"
        ) as bridge_cls:
            bridge_cls.is_supported.return_value = True
            mgr = ComputeShaderManager(_mock_gl_ctx("4.1 ATI-6"))
            assert not mgr.is_available()
            assert mgr._metal_bridge is bridge_cls.return_value
            assert mgr.should_use_compute_blur(COMPUTE_THRESHOLD_RADIUS + 1)
            assert mgr.should_use_compute_erode_dilate(COMPUTE_THRESHOLD_SIZE + 1)
            assert not mgr.should_use_compute_blur(1.0)

    def test_metal_path_disabled_by_default(self, monkeypatch):
        """P4-05 实测后默认不启用 Metal 桥（fragment fallback 更快）。"""
        monkeypatch.delenv("TOPOS_METAL_COMPUTE", raising=False)
        with patch(
            "src.features.composite.render.compute_shader_manager.MetalComputeBridge"
        ) as bridge_cls:
            bridge_cls.is_supported.return_value = True
            mgr = ComputeShaderManager(_mock_gl_ctx("4.1 ATI-6"))
            assert mgr._metal_bridge is None
            assert not mgr.should_use_compute_blur(999.0)
            bridge_cls.assert_not_called()

    def test_env_flag_does_not_bypass_device_probe(self, monkeypatch):
        monkeypatch.setenv("TOPOS_METAL_COMPUTE", "1")
        with patch(
            "src.features.composite.render.compute_shader_manager.MetalComputeBridge"
        ) as bridge_cls:
            bridge_cls.is_supported.return_value = False
            mgr = ComputeShaderManager(_mock_gl_ctx("4.1 ATI-6"))
            assert mgr._metal_bridge is None

    def test_no_metal_keeps_fragment_fallback_gating(self, monkeypatch):
        monkeypatch.setenv("TOPOS_METAL_COMPUTE", "1")
        with patch(
            "src.features.composite.render.compute_shader_manager.MetalComputeBridge"
        ) as bridge_cls:
            bridge_cls.is_supported.return_value = False
            mgr = ComputeShaderManager(_mock_gl_ctx("4.1 ATI-6"))
            assert mgr._metal_bridge is None
            assert not mgr.should_use_compute_blur(999.0)
            assert mgr.apply_blur_compute(MagicMock(), 50.0, 64, 64, None) is None

    def test_metal_init_failure_is_not_fatal(self, monkeypatch):
        monkeypatch.setenv("TOPOS_METAL_COMPUTE", "1")
        with patch(
            "src.features.composite.render.compute_shader_manager.MetalComputeBridge"
        ) as bridge_cls:
            bridge_cls.is_supported.return_value = True
            bridge_cls.side_effect = RuntimeError("no device")
            mgr = ComputeShaderManager(_mock_gl_ctx("4.1 ATI-6"))
            assert mgr._metal_bridge is None

    def test_glsl_compute_path_unchanged_when_4_3(self):
        """GL 4.3+ 时不启用 Metal 桥（不改变既有路径）。"""
        ctx = _mock_gl_ctx("4.6.0 NVIDIA 535")
        ctx.compute_shader.return_value = MagicMock()
        with patch(
            "src.features.composite.render.compute_shader_manager.MetalComputeBridge"
        ) as bridge_cls:
            mgr = ComputeShaderManager(ctx)
            assert mgr.is_available()
            bridge_cls.assert_not_called()


class TestUnifiedFallbackChannel:
    """P4-04：Metal 失败走与 GL compute 相同的 _fallback_callback。"""

    def _mgr_with_failing_bridge(self, monkeypatch):
        monkeypatch.setenv("TOPOS_METAL_COMPUTE", "1")
        with patch(
            "src.features.composite.render.compute_shader_manager.MetalComputeBridge"
        ) as bridge_cls:
            bridge_cls.is_supported.return_value = True
            mgr = ComputeShaderManager(_mock_gl_ctx("4.1 ATI-6"))
        bridge = bridge_cls.return_value
        bridge.apply_blur.side_effect = RuntimeError("boom")
        bridge.apply_erode_dilate.side_effect = RuntimeError("boom2")
        return mgr, bridge

    def test_blur_failure_notifies_fallback_callback_and_returns_none(self, monkeypatch):
        mgr, bridge = self._mgr_with_failing_bridge(monkeypatch)
        notified = []
        mgr.set_fallback_callback(lambda name, err: notified.append((name, err)))

        result = mgr.apply_blur_compute(MagicMock(), 50.0, 64, 64, None)

        assert result is None
        assert len(notified) == 1
        assert notified[0][0] == "metal_blur"
        assert "boom" in notified[0][1]
        assert "boom" in mgr.get_last_error()

    def test_erode_dilate_failure_notifies_fallback_callback(self, monkeypatch):
        mgr, bridge = self._mgr_with_failing_bridge(monkeypatch)
        notified = []
        mgr.set_fallback_callback(lambda name, err: notified.append((name, err)))

        result = mgr.apply_erode_dilate_compute(
            MagicMock(), "erode", 20, 64, 64, None, 0
        )

        assert result is None
        assert notified[0][0] == "metal_erode_dilate"

    def test_success_bridges_result_without_notification(self, monkeypatch):
        monkeypatch.setenv("TOPOS_METAL_COMPUTE", "1")
        with patch(
            "src.features.composite.render.compute_shader_manager.MetalComputeBridge"
        ) as bridge_cls:
            bridge_cls.is_supported.return_value = True
            mgr = ComputeShaderManager(_mock_gl_ctx("4.1 ATI-6"))
        sentinel = object()
        bridge_cls.return_value.apply_blur.return_value = sentinel
        notified = []
        mgr.set_fallback_callback(lambda name, err: notified.append(name))

        result = mgr.apply_blur_compute(MagicMock(), 50.0, 64, 64, None, 0)

        assert result is sentinel
        assert notified == []
        bridge_cls.return_value.apply_blur.assert_called_once()


class TestComputeApiStats:
    def test_stats_report_compute_api_metal(self, monkeypatch):
        monkeypatch.setenv("TOPOS_METAL_COMPUTE", "1")
        with patch(
            "src.features.composite.render.compute_shader_manager.MetalComputeBridge"
        ) as bridge_cls:
            bridge_cls.is_supported.return_value = True
            bridge_cls.return_value._ctx.device_name = "AMD Radeon RX 6900 XT"
            mgr = ComputeShaderManager(_mock_gl_ctx("4.1 ATI-6"))
        stats = mgr.get_stats()
        assert stats["compute_api"] == "metal"
        assert "6900 XT" in stats["metal_device"]

    def test_stats_report_compute_api_glsl(self):
        ctx = _mock_gl_ctx("4.6.0 NVIDIA 535")
        ctx.compute_shader.return_value = MagicMock()
        mgr = ComputeShaderManager(ctx)
        assert mgr.get_stats()["compute_api"] == "glsl_compute"


# ---------------------------------------------------------------------------
# 端到端：standalone GL 4.1 + 真机 Metal（gpu_renderer marker）
# ---------------------------------------------------------------------------

try:
    import moderngl
    _HAS_GL = True
except Exception:  # pragma: no cover
    _HAS_GL = False

requires_gpu = pytest.mark.gpu_renderer
requires_metal_e2e = pytest.mark.skipif(
    not MetalComputeContext.is_supported(), reason="无 Metal 设备"
)


@requires_gpu
@requires_metal_e2e
class TestEndToEndBlurOnMacOSPath:
    def test_default_off_returns_none_fragment_fallback(self, monkeypatch):
        """P4-05 实测后默认门控关闭：blur 走 fragment fallback。"""
        monkeypatch.delenv("TOPOS_METAL_COMPUTE", raising=False)
        gl = moderngl.create_standalone_context()
        try:
            mgr = ComputeShaderManager(gl)
            assert mgr.get_stats()["compute_api"] == "fragment_only"
            tex = gl.texture((16, 16), 4, dtype="f2")
            assert mgr.apply_blur_compute(tex, 50.0, 16, 16, None) is None
            tex.release()
        finally:
            gl.release()

    def test_apply_blur_compute_unlocks_on_gl41(self, monkeypatch):
        """验收：TOPOS_METAL_COMPUTE=1 时大半径模糊解锁且画质正确。"""
        monkeypatch.setenv("TOPOS_METAL_COMPUTE", "1")
        gl = moderngl.create_standalone_context()
        try:
            mgr = ComputeShaderManager(gl)
            assert not mgr.is_available()  # standalone ctx 为 GL 4.1
            w, h = 48, 32
            rng = np.random.default_rng(23)
            img = (rng.random((h, w, 4)) * 4.0).astype(np.float16)
            tex = gl.texture((w, h), 4, dtype="f2")
            tex.write(img.tobytes())

            out_tex = mgr.apply_blur_compute(tex, radius=8.0, w=w, h=h,
                                             resource_pool=None, edge_policy=0)
            assert out_tex is not None

            from src.shared.gpu.metal.kernels import gaussian_blur_reference
            ref_h = gaussian_blur_reference(img, 8.0, axis=0)
            ref = gaussian_blur_reference(ref_h, 8.0, axis=1)
            got = np.frombuffer(out_tex.read(),
                                dtype="<f2").reshape(h, w, 4)
            np.testing.assert_allclose(got.astype(np.float32),
                                       ref.astype(np.float32), atol=4e-3)
            tex.release()
            out_tex.release()
        finally:
            gl.release()
