"""wd17-g104：YUV GPU 上传/自检 8 条审计修复测试。

覆盖（docs/plans/2026-09-28-multi-machine-program/REMEDIATION_PLAN.md）：
AUD-39712 / AUD-39720 / AUD-39801 / AUD-39802 / AUD-39803 /
AUD-39804 / AUD-39805 / AUD-39806

GPU 路径优先用 fake 对象 / monkeypatch 验证（不依赖 GL 上下文）；
run_r16ui_self_test 的成功路径附带真实 standalone context 用例
（无 GL 环境自动 skip，与 tests/video/test_yuv_gpu_upload.py 同款）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import MagicMock, call

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import src.shared.video.video_upload as vu_module
from src.shared.video.video_upload import YuvPlaneUploader
import src.shared.video.yuv_gpu_self_test as selftest_module
from src.shared.video.yuv_gpu_self_test import run_yuv_gpu_self_test
from src.shared.video.yuv_gpu_upload import YUVGPUUploader, YUVTexturePool

try:
    import moderngl
    HAS_MODERNGL = True
except ImportError:  # pragma: no cover
    HAS_MODERNGL = False


# ============================================================================
# 共享构造工具
# ============================================================================


def _fake_plane_tex(dtype: str) -> SimpleNamespace:
    """伪造的平面纹理：带 dtype（ModernGL Texture.dtype 的最小面）。"""
    return SimpleNamespace(dtype=dtype, use=MagicMock())


class _RecordingCtx:
    """记录 texture/buffer/program/vertex_array/framebuffer 创建的 fake ctx。

    每次调用都产生并记录一个新 MagicMock（MagicMock 的 return_value
    会被复用，无法区分多次创建）。
    """

    def __init__(self) -> None:
        self.created_textures: List[MagicMock] = []
        self.created_buffers: List[MagicMock] = []
        self.created_programs: List[MagicMock] = []
        self.created_vaos: List[MagicMock] = []
        self.created_fbos: List[MagicMock] = []

    def texture(self, size=None, components=None, dtype=None, *args, **kwargs) -> MagicMock:
        tex = MagicMock()
        tex.size = size
        tex.components = components
        tex.dtype = dtype
        self.created_textures.append(tex)
        return tex

    def buffer(self, *args, **kwargs) -> MagicMock:
        buf = MagicMock()
        self.created_buffers.append(buf)
        return buf

    def program(self, *args, **kwargs) -> MagicMock:
        prog = MagicMock()
        self.created_programs.append(prog)
        return prog

    def vertex_array(self, *args, **kwargs) -> MagicMock:
        vao = MagicMock()
        self.created_vaos.append(vao)
        return vao

    def framebuffer(self, *args, **kwargs) -> MagicMock:
        fbo = MagicMock()
        self.created_fbos.append(fbo)
        return fbo

    def __getattr__(self, name: str) -> MagicMock:
        # clear/use/viewport 等杂项调用
        return MagicMock()


def _mock_uploader() -> YUVGPUUploader:
    """MagicMock ctx 构造的 uploader（探针全部'通过'，不触碰真实 GL）。"""
    return YUVGPUUploader(MagicMock())


# ============================================================================
# AUD-39712: create_yuv_program 用 CWD 相对路径加载外部 shader
# ============================================================================


class TestAud39712ShaderPathAnchoredToRepoRoot:
    EXTERNAL_VS_MARKER = "Standard passthrough vertex shader for full-screen quad rendering"
    BUILTIN_FS_MARKER = "const mat3 bt709"

    def test_loads_external_shader_from_foreign_cwd(self, monkeypatch, tmp_path) -> None:
        """启动目录与仓库根无关时仍能加载外部 shader（不再静默降级）。"""
        monkeypatch.chdir(tmp_path)  # 模拟任意启动目录
        ctx = MagicMock()
        ctx.program = MagicMock(return_value=MagicMock())
        uploader = YuvPlaneUploader(ctx)

        program = uploader.create_yuv_program()

        ctx.program.assert_called_once()
        kwargs = ctx.program.call_args.kwargs
        assert self.EXTERNAL_VS_MARKER in kwargs["vertex_shader"], (
            "外部 shader 的 VS 必须被加载（锚定仓库根，而不是 CWD 找不到就降级内置）"
        )
        assert program is ctx.program.return_value

    def test_candidates_anchor_to_repo_root_from_module_file(self) -> None:
        """候选路径首位由 __file__ 上溯仓库根，与 CWD 无关。"""
        candidates = vu_module._video_yuv_shader_candidates()
        assert len(candidates) >= 1
        anchored = candidates[0]
        assert anchored.is_absolute(), "锚定路径必须是绝对路径"
        assert anchored == (
            Path(vu_module.__file__).resolve().parents[3]
            / "src" / "features" / "color" / "shaders" / "video_yuv.glsl"
        )
        assert anchored.is_file(), "仓库内该 shader 必须真实存在"

    def test_falls_back_to_builtin_only_when_all_candidates_missing(
        self, monkeypatch, tmp_path, caplog
    ) -> None:
        """所有候选都不存在时才回退内置 shader，并给出明确告警。"""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            vu_module,
            "_video_yuv_shader_candidates",
            lambda: [tmp_path / "nope" / "video_yuv.glsl"],
        )
        ctx = MagicMock()
        ctx.program = MagicMock(return_value=MagicMock())
        uploader = YuvPlaneUploader(ctx)

        with caplog.at_level("WARNING"):
            program = uploader.create_yuv_program()

        ctx.program.assert_called_once()
        kwargs = ctx.program.call_args.kwargs
        assert self.BUILTIN_FS_MARKER in kwargs["fragment_shader"], (
            "候选全部缺失时回退内置 FS"
        )
        assert any("built-in shader" in r.message for r in caplog.records), (
            "回退必须留下告警（不再是静默降级）"
        )
        assert program is ctx.program.return_value


# ============================================================================
# AUD-39720: run_yuv_gpu_self_test 外层异常路径不清理 output_tex/fbo
# ============================================================================


class TestAud39720SelfTestExceptionPathCleanup:
    def _make_ctx_with_fbo_tracking(self) -> tuple:
        textures: List[MagicMock] = []
        fbos: List[MagicMock] = []

        ctx = MagicMock()

        def fake_texture(size, components, dtype):
            tex = MagicMock()
            tex.size = size
            tex.components = components
            tex.dtype = dtype
            textures.append(tex)
            return tex

        def fake_framebuffer(color_attachments):
            fbo = MagicMock()
            fbo.color_attachments = color_attachments
            fbos.append(fbo)
            return fbo

        ctx.texture = fake_texture
        ctx.framebuffer = fake_framebuffer
        return ctx, textures, fbos

    def test_render_exception_releases_output_tex_and_fbo(self) -> None:
        """render/readback 抛异常时必须释放本次自检创建的 FBO 与输出纹理。"""
        ctx, textures, fbos = self._make_ctx_with_fbo_tracking()
        uploader = MagicMock()
        uploader.upload_yuv444.return_value = (
            _fake_plane_tex("f1"), _fake_plane_tex("f1"), _fake_plane_tex("f1"))
        uploader.render_yuv_to_rgb.side_effect = RuntimeError("boom during render")

        result = run_yuv_gpu_self_test(ctx, uploader)

        assert result.passed is False
        assert result.reason.startswith("exception:")

        # 输出纹理（4 通道 f1）与 FBO 恰好各创建一份，且都恰好释放一次
        output_texs = [t for t in textures if t.components == 4]
        assert len(output_texs) == 1
        output_texs[0].release.assert_called_once()
        assert len(fbos) == 1
        fbos[0].release.assert_called_once()

    def test_upload_failure_before_fbo_creation_leaks_nothing(self) -> None:
        """上传阶段失败（FBO 尚未创建）不触发清理崩溃也不误释放。"""
        ctx, textures, fbos = self._make_ctx_with_fbo_tracking()
        uploader = MagicMock()
        uploader.upload_yuv444.side_effect = RuntimeError("upload boom")

        result = run_yuv_gpu_self_test(ctx, uploader)

        assert result.passed is False
        assert result.reason.startswith("upload_failed:")
        assert textures == [] and fbos == []

    def test_readback_exception_also_releases(self) -> None:
        """readback 阶段抛异常同样清理（异常点不止 render 一处）。"""
        ctx, textures, fbos = self._make_ctx_with_fbo_tracking()
        uploader = MagicMock()
        uploader.upload_yuv444.return_value = (
            _fake_plane_tex("f1"), _fake_plane_tex("f1"), _fake_plane_tex("f1"))
        # fbo 在 render 前已创建：让 fbo.read 抛异常，命中外层 except
        def _render_and_poison_read(*_a, **_k):
            fbos[0].read = MagicMock(side_effect=RuntimeError("readback boom"))
        uploader.render_yuv_to_rgb.side_effect = _render_and_poison_read

        result = run_yuv_gpu_self_test(ctx, uploader)

        assert result.reason.startswith("exception:")
        output_texs = [t for t in textures if t.components == 4]
        assert len(output_texs) == 1
        output_texs[0].release.assert_called_once()
        fbos[0].release.assert_called_once()


# ============================================================================
# AUD-39801: _upload_planar_r8 回退丢失 subsampling
# ============================================================================


class TestAud39801R8FallbackKeepsSubsampling:
    def _yuv444_8bit(self, w=8, h=8):
        y = np.full((h, w), 128, np.uint8)
        u = np.full((h, w), 128, np.uint8)
        v = np.full((h, w), 128, np.uint8)
        return y, u, v

    def _yuv422_8bit(self, w=8, h=8):
        y = np.full((h, w), 128, np.uint8)
        u = np.full((h, (w + 1) // 2), 128, np.uint8)
        v = np.full((h, (w + 1) // 2), 128, np.uint8)
        return y, u, v

    @staticmethod
    def _uploader_with_r8_disabled(monkeypatch) -> tuple:
        uploader = _mock_uploader()
        uploader._r8_pool = None  # 模拟 R8 池不可用 → 触发回退
        targets = {
            "420": MagicMock(return_value=("y420", "u420", "v420")),
            "422": MagicMock(return_value=("y422", "u422", "v422")),
            "444": MagicMock(return_value=("y444", "u444", "v444")),
        }
        monkeypatch.setattr(uploader, "upload_yuv420", targets["420"])
        monkeypatch.setattr(uploader, "upload_yuv422", targets["422"])
        monkeypatch.setattr(uploader, "upload_yuv444", targets["444"])
        return uploader, targets

    def test_444_fallback_dispatches_to_yuv444_not_yuv420(self, monkeypatch) -> None:
        """旧实现：8-bit 444 回退 upload_yuv420 → 420 尺寸断言崩溃。"""
        uploader, targets = self._uploader_with_r8_disabled(monkeypatch)
        y, u, v = self._yuv444_8bit()

        result = uploader._upload_planar_r8(
            y, u, v, 0, u_width=y.shape[1], u_height=y.shape[0])

        targets["444"].assert_called_once()
        targets["420"].assert_not_called()
        assert result == ("y444", "u444", "v444")

    def test_422_fallback_dispatches_to_yuv422(self, monkeypatch) -> None:
        uploader, targets = self._uploader_with_r8_disabled(monkeypatch)
        y, u, v = self._yuv422_8bit()

        result = uploader._upload_planar_r8(
            y, u, v, 0, u_width=(y.shape[1] + 1) // 2, u_height=y.shape[0])

        targets["422"].assert_called_once()
        targets["420"].assert_not_called()
        assert result == ("y422", "u422", "v422")

    def test_subsampling_derived_from_plane_shapes_when_none(self, monkeypatch) -> None:
        """subsampling=None 时按平面形状推导，直接调用也不丢元数据。"""
        uploader, targets = self._uploader_with_r8_disabled(monkeypatch)
        y, u, v = self._yuv422_8bit()

        uploader._upload_planar_r8(y, u, v, 0,
                                   u_width=u.shape[1], u_height=u.shape[0])

        targets["422"].assert_called_once()

    @pytest.mark.skipif(not HAS_MODERNGL, reason="moderngl not available")
    def test_real_gl_8bit_422_upload_survives_r8_pool_unavailable(self) -> None:
        """端到端：R8 池置空后 8-bit 422 上传不再 AssertionError。"""
        ctx = moderngl.create_standalone_context()
        try:
            uploader = YUVGPUUploader(ctx, pool_size=3)
            uploader._r8_pool = None
            y, u, v = self._yuv422_8bit(16, 16)
            uploader.release()

            uploader = YUVGPUUploader(ctx, pool_size=3)
            uploader._r8_pool = None
            texes = uploader.upload_yuv422(y, u, v, 0, bit_depth=8)
            assert len(texes) == 3
            assert uploader._last_upload_mode == "float16"
            uploader.release()
        finally:
            ctx.release()


# ============================================================================
# AUD-39802 / AUD-39803: run_r16ui_self_test 状态恢复 + 异常清理
# ============================================================================


class TestAud39802R16uiSelfTestRestoresSharedState:
    def test_exception_path_restores_matrix_and_range(self, monkeypatch) -> None:
        uploader = _mock_uploader()
        assert uploader._r16ui_probe_ok, "mock ctx 下探针应通过"
        uploader.set_yuv_matrix(YUVGPUUploader.MATRIX_BT2020)
        uploader.set_color_range(YUVGPUUploader.RANGE_FULL)
        monkeypatch.setattr(
            uploader, "render_yuv_r16ui_to_rgb",
            MagicMock(side_effect=RuntimeError("render boom")))

        passed = uploader.run_r16ui_self_test()

        assert passed is False
        assert uploader._yuv_matrix == YUVGPUUploader.MATRIX_BT2020, (
            "自检覆盖 _yuv_matrix 后必须恢复（AUD-39802）"
        )
        assert uploader._color_range == YUVGPUUploader.RANGE_FULL
        assert "exception" in uploader._r16ui_disabled_reason

    @pytest.mark.skipif(not HAS_MODERNGL, reason="moderngl not available")
    def test_real_gl_success_path_restores_matrix_and_range(self) -> None:
        """真实 GL：自检完整跑通（成功或失败）后共享参数原样恢复。"""
        ctx = moderngl.create_standalone_context()
        try:
            uploader = YUVGPUUploader(ctx, pool_size=3)
            uploader.set_yuv_matrix(YUVGPUUploader.MATRIX_BT601)
            uploader.set_color_range(YUVGPUUploader.RANGE_FULL)

            uploader.run_r16ui_self_test()

            assert uploader._yuv_matrix == YUVGPUUploader.MATRIX_BT601
            assert uploader._color_range == YUVGPUUploader.RANGE_FULL
            uploader.release()
        finally:
            ctx.release()


class TestAud39803R16uiSelfTestExceptionPathCleanup:
    def test_exception_path_releases_fbo_and_output_texture(self, monkeypatch) -> None:
        ctx = _RecordingCtx()
        uploader = YUVGPUUploader(ctx)
        assert uploader._r16ui_probe_ok
        monkeypatch.setattr(
            uploader, "render_yuv_r16ui_to_rgb",
            MagicMock(side_effect=RuntimeError("render boom")))

        passed = uploader.run_r16ui_self_test()

        assert passed is False
        # 自检创建的 FBO 必须被释放
        assert len(ctx.created_fbos) == 1
        ctx.created_fbos[0].release.assert_called_once()
        # 输出纹理（4 通道 f1，自检在渲染前直接创建）必须被释放
        output_texs = [t for t in ctx.created_textures
                       if t.components == 4 and t.dtype == "f1"]
        assert len(output_texs) == 1
        output_texs[0].release.assert_called_once()


# ============================================================================
# AUD-39804: 纹理池容量未满时优先抢占 busy 槽位
# ============================================================================


class TestAud39804PoolCreatesNewSlotBeforePreemptingBusy:
    def test_fills_to_capacity_before_preempting(self) -> None:
        """容量未满时逐帧新建独立槽位；池满后才按 LRU 抢占最旧。"""
        ctx = MagicMock()
        ctx.texture = MagicMock(side_effect=lambda size, components, dtype: MagicMock())
        pool = YUVTexturePool(ctx, y_pool_size=3, uv_pool_size=3)

        t1 = pool.acquire_y_texture(64, 64, frame_index=0)
        t2 = pool.acquire_y_texture(64, 64, frame_index=1)
        # 在轨帧（frame 0 尚未释放）不被抢占 → 独立纹理
        assert t2 is not t1
        assert pool.get_stats()["y_pool_size"] == 2

        t3 = pool.acquire_y_texture(64, 64, frame_index=2)
        assert t3 is not t1 and t3 is not t2
        assert pool.get_stats()["y_pool_size"] == 3  # 池满

        # 池满后：抢占最旧（frame 0 的 t1），复用其纹理对象
        t4 = pool.acquire_y_texture(64, 64, frame_index=3)
        assert t4 is t1
        assert pool.get_stats()["y_pool_size"] == 3

        pool.release_all()

    def test_released_slot_is_reused_before_creating_new(self) -> None:
        """帧释放（frame_index<0）后优先复用该空闲槽，而不是继续扩池。"""
        ctx = MagicMock()
        ctx.texture = MagicMock(side_effect=lambda size, components, dtype: MagicMock())
        pool = YUVTexturePool(ctx, y_pool_size=4, uv_pool_size=4)

        t1 = pool.acquire_y_texture(32, 32, frame_index=0)
        slot = pool._y_slots[0]
        slot.frame_index = -1  # 模拟消费方释放该帧

        t2 = pool.acquire_y_texture(32, 32, frame_index=1)
        assert t2 is t1
        assert pool.get_stats()["y_pool_size"] == 1

        pool.release_all()

    def test_plane_types_do_not_share_slots_when_not_full(self) -> None:
        """u/v/a 各自新建槽位，互不抢占（容量未满阶段）。"""
        ctx = MagicMock()
        ctx.texture = MagicMock(side_effect=lambda size, components, dtype: MagicMock())
        pool = YUVTexturePool(ctx, y_pool_size=4, uv_pool_size=4)

        y = pool.acquire_y_texture(16, 16, 0)
        u = pool.acquire_u_texture(8, 8, 0)
        v = pool.acquire_v_texture(8, 8, 0)
        a = pool.acquire_a_texture(16, 16, 0)
        assert len({id(y), id(u), id(v), id(a)}) == 4
        pool.release_all()


# ============================================================================
# AUD-39805: _last_upload_mode 全局单值决定渲染 shader 分发
# ============================================================================


class TestAud39805PerTextureSamplerDispatch:
    def test_u2_texture_after_float_upload_still_uses_usampler(self, monkeypatch) -> None:
        """8-bit 轨道上传覆盖 _last_upload_mode 后，R16UI 纹理仍走 usampler。"""
        uploader = _mock_uploader()
        uploader._last_upload_mode = "nv12_r8_rg8"  # 模拟后续 8-bit 上传覆盖
        r16ui_render = MagicMock()
        monkeypatch.setattr(uploader, "render_yuv_r16ui_to_rgb", r16ui_render)

        y_tex = _fake_plane_tex("u2")
        uploader.render_yuv_to_rgb(
            y_tex, _fake_plane_tex("u2"), _fake_plane_tex("u2"),
            MagicMock(), bit_depth=10)

        r16ui_render.assert_called_once()

    def test_f1_texture_after_r16ui_upload_uses_float_sampler(self, monkeypatch) -> None:
        """反向混合：mode 仍是 r16ui_direct，但本帧是 f1 纹理 → float 路径。"""
        uploader = _mock_uploader()
        uploader._last_upload_mode = "r16ui_direct"
        r16ui_render = MagicMock()
        monkeypatch.setattr(uploader, "render_yuv_r16ui_to_rgb", r16ui_render)
        # 预置 float program（render_yuv_to_rgb 依赖 self._program）
        float_program = MagicMock()
        uploader._program = float_program

        y_tex = _fake_plane_tex("f1")
        uploader.render_yuv_to_rgb(
            y_tex, _fake_plane_tex("f1"), _fake_plane_tex("f1"),
            MagicMock(), bit_depth=8)

        r16ui_render.assert_not_called()
        # float 路径确实执行：program 的 uniforms 被读取并设置
        float_program.__getitem__.assert_any_call("bit_depth")

    def test_texture_without_dtype_falls_back_to_upload_mode(self, monkeypatch) -> None:
        """mock/无 dtype 对象回退 _last_upload_mode（兼容旧行为）。"""
        uploader = _mock_uploader()
        uploader._last_upload_mode = "r16ui_direct"
        r16ui_render = MagicMock()
        monkeypatch.setattr(uploader, "render_yuv_r16ui_to_rgb", r16ui_render)

        plain_tex = SimpleNamespace(use=MagicMock())  # 无 dtype 属性
        uploader.render_yuv_to_rgb(
            plain_tex, plain_tex, plain_tex, MagicMock(), bit_depth=12)

        r16ui_render.assert_called_once()

    def test_nv12_dispatch_also_uses_texture_dtype(self, monkeypatch) -> None:
        """同型缺陷（nv12 分发点）同样按纹理 dtype 判定。"""
        uploader = _mock_uploader()

        # u2 纹理 → usampler 路径（即使 mode 刚被 8-bit 上传改写）
        uploader._last_upload_mode = "nv12_r8_rg8"
        r16ui_render = MagicMock()
        monkeypatch.setattr(uploader, "render_nv12_r16ui_to_rgb", r16ui_render)
        uploader.render_nv12_to_rgb(
            _fake_plane_tex("u2"), _fake_plane_tex("u2"), MagicMock(), bit_depth=10)
        r16ui_render.assert_called_once()

        # f1 纹理 → float 路径（即使 mode 仍是 r16ui_direct）
        uploader._last_upload_mode = "r16ui_direct"
        r16ui_render.reset_mock()
        float_path = MagicMock()
        monkeypatch.setattr(uploader, "render_nv12_r16ui_to_rgb", r16ui_render)
        monkeypatch.setattr(uploader, "get_nv12_shader", lambda: float_path)
        uploader.render_nv12_to_rgb(
            _fake_plane_tex("f1"), _fake_plane_tex("f1"), MagicMock(), bit_depth=8)
        r16ui_render.assert_not_called()


# ============================================================================
# AUD-39806: render_nv12_to_rgb 每帧创建/销毁临时 VBO/IBO/VAO
# ============================================================================


class TestAud39806Nv12QuadVaoReuse:
    def _render_twice(self, ctx: _RecordingCtx) -> YUVGPUUploader:
        uploader = YUVGPUUploader(ctx)
        fbo = MagicMock()
        y = _fake_plane_tex("f1")
        uv = _fake_plane_tex("f1")
        uploader.render_nv12_to_rgb(y, uv, fbo, bit_depth=8)
        uploader.render_nv12_to_rgb(y, uv, fbo, bit_depth=8)
        return uploader

    def test_renders_twice_create_quad_geometry_once(self) -> None:
        """两次渲染只创建一次 VBO/IBO/VAO（消除每帧 churn）。"""
        ctx = _RecordingCtx()
        uploader = self._render_twice(ctx)

        assert len(ctx.created_vaos) == 1, "VAO 必须按 shader 缓存复用"
        assert ctx.created_vaos[0].render.call_count == 2
        # VBO + IBO 各一个（quad_buffer 与 nv12_quad_ibo），不再每帧新增
        assert len(ctx.created_buffers) == 2

        uploader.release()

    def test_release_frees_cached_vao_and_ibo(self) -> None:
        """release() 释放缓存 VAO 与共享 VBO/IBO（不留 GL 对象）。"""
        ctx = _RecordingCtx()
        uploader = self._render_twice(ctx)

        uploader.release()

        ctx.created_vaos[0].release.assert_called_once()
        for buf in ctx.created_buffers:
            buf.release.assert_called_once_with()

    def test_render_exception_creates_no_transient_objects(self) -> None:
        """vao.render 抛异常时无临时对象可泄漏（缓存对象仍在，随 release 释放）。"""
        ctx = _RecordingCtx()
        uploader = YUVGPUUploader(ctx)
        # 首次渲染时让 VAO 的 render 抛异常
        original_vertex_array = ctx.vertex_array

        def va_with_boom(*args, **kwargs):
            vao = original_vertex_array(*args, **kwargs)
            vao.render.side_effect = RuntimeError("render boom")
            return vao

        ctx.vertex_array = va_with_boom
        y = _fake_plane_tex("f1")
        uv = _fake_plane_tex("f1")

        with pytest.raises(RuntimeError):
            uploader.render_nv12_to_rgb(y, uv, MagicMock(), bit_depth=8)

        assert len(ctx.created_vaos) == 1
        assert len(ctx.created_buffers) == 2  # 仅共享 VBO/IBO，无每帧临时对象
        uploader.release()
        ctx.created_vaos[0].release.assert_called_once()

    def test_external_shader_gets_own_cached_vao(self) -> None:
        """外部传入不同 shader 时按对象分别缓存，总量有界。"""
        ctx = _RecordingCtx()
        uploader = YUVGPUUploader(ctx)
        y = _fake_plane_tex("f1")
        uv = _fake_plane_tex("f1")
        fbo = MagicMock()

        uploader.render_nv12_to_rgb(y, uv, fbo, shader=MagicMock(), bit_depth=8)
        uploader.render_nv12_to_rgb(y, uv, fbo, shader=MagicMock(), bit_depth=8)
        uploader.render_nv12_to_rgb(y, uv, fbo, bit_depth=8)  # 内置 shader

        assert len(ctx.created_vaos) == 3  # 3 个不同 shader 对象各一个
        uploader.release()
