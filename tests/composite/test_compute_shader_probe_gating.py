"""W4-01：ComputeShaderManager 探测式门控（真机驱动缺陷回归钉）。

背景（2026-09-12 AMD RX 6800 / Adrenalin 26.8.1 实测，见
tests/media/results/gl_compute_probe_amd_rx6800.*）：该驱动 GL_VERSION
报 4.5/4.6、GLSL 串报 4.60，但 ``glCreateShader(GL_COMPUTE_SHADER)``
一律 ``GL_INVALID_ENUM``——**版本串不是能力证据**。旧实现版本 ≥4.3
直接置可用，导致这类机器上每个效果运行期编译失败。

契约（修复后）：版本 <4.3 直接拒绝；版本 ≥4.3 必须测试编译成功才算可用。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.features.composite.render.compute_shader_manager import ComputeShaderManager


def _ctx(version: str, compute_works: bool) -> MagicMock:
    ctx = MagicMock()
    ctx.info = {"GL_VERSION": version}
    if compute_works:
        prog = MagicMock()
        ctx.compute_shader.return_value = prog
    else:
        ctx.compute_shader.side_effect = Exception(
            "GL_INVALID_ENUM: glCreateShader(GL_COMPUTE_SHADER)")
    return ctx


class TestProbeBasedGating:
    def test_low_version_short_circuits_without_compile(self):
        """4.1（macOS）直接拒绝，不触碰 compute_shader。"""
        ctx = _ctx("4.1 ATI-6", compute_works=False)
        mgr = ComputeShaderManager(ctx)
        assert mgr.is_available() is False
        ctx.compute_shader.assert_not_called()

    def test_version_46_with_broken_compute_is_unavailable(self):
        """AMD 真机形态：报 4.6 但编译必败 → 诚实不可用（旧实现误判 True）。"""
        mgr = ComputeShaderManager(_ctx("4.6.0 Core Profile Context", compute_works=False))
        assert mgr.is_available() is False
        assert "test compilation failed" in (mgr._last_error or "")

    def test_version_45_with_working_compute_is_available(self):
        """健康驱动：4.5 + 测试编译成功 → 可用（行为不变）。"""
        mgr = ComputeShaderManager(_ctx("4.5.0 Core Profile Context", compute_works=True))
        assert mgr.is_available() is True

    def test_unknown_version_still_probes(self):
        """版本串缺失时保持旧探测路径。"""
        ctx = MagicMock()
        ctx.info = {"GL_VERSION": ""}
        ctx.compute_shader.return_value = MagicMock()
        assert ComputeShaderManager(ctx).is_available() is True
