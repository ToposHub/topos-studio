"""wd3-g20（计划批 D）修复回归测试。

对应 REMEDIATION_PLAN.md 批 D 的 Major 条目（AUD-0910 除外——该条需要
GLSL/MSL 双侧同步修改 shader_nodes.py + shader_nodes_msl.py，超出本批
文件白名单，见 fixes/reports/wd3-g20.md）：

- AUD-0907  降噪表面缓存键去尺寸（多分辨率 GPU 显存无上界累积）
- AUD-0911  apply_soft_clip 补齐 GPU 的两条 elif 路径
- AUD-0912  前向模型补齐 GPU 的暗部保护与 toe RGB 平衡
- AUD-0913  hue_shift 前不再 clip（HDR/负值 CPU/GPU 分叉）
- AUD-1000  reset 后同 identity 状态机不再卡死在 delta==0
- AUD-1001  _downsample_luma 用被采样纹理自身尺寸作 u_texel
- AUD-1005  预设读取失败时 save/delete 拒绝整体覆盖写回
- AUD-1006  预设写入失败有日志且 save 返回 None
- AUD-24200 全量恢复对 layers 快照逐条容错 + 原子替换
- AUD-24201 collect 采集 video_denoise（与 apply 对称）
- AUD-24202 快照往返补 BITMAP/AI-matte inline 通道
- AUD-24301 frozen dataclass 的 Mapping 字段冻结为只读视图
- AUD-24302 validate_hdr 拒绝未知 LUT transfer_function
- AUD-24400 导出跟踪模式做 Composite 词表映射
- AUD-24500 环路径发布不再清空在途 trackerball 批处理
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from src.features.color.adapters.color_stack_hydrator import (
    _AI_MATTE_INLINE_KEY,
    _BITMAP_INLINE_KEY,
    ColorStackHydrator,
)
from src.features.color.ai.color_context import (
    HDR_PQ,
    OP_LUT,
    OP_PRIMARY,
    SDR,
    BrandColorProfile,
    ColorContextSnapshot,
    ColorGradePlan,
    ColorMeasurement,
    GradeOperation,
    HDRViolation,
    LookRecipe,
    validate_hdr,
)
from src.features.color.core.the_decompiler import GradingMath
from src.features.color.core.video_spatial_denoise import VideoSpatialDenoiser
from src.features.color.core.video_temporal_denoise import (
    TemporalDenoiseState,
    VideoTemporalDenoiser,
)
from src.features.color.core.workspace_presets import (
    CONFIG_KEY,
    delete_user_workspace,
    load_user_workspaces,
    save_user_workspace,
)
from src.features.color.facade import ColorFacade
from src.shared.project.color_stack import ColorStackData
from src.shared.video.denoise.params import (
    SpatialDenoiseParams,
    TemporalDenoiseParams,
)

pytestmark = [pytest.mark.regression, pytest.mark.color_core]


# ---------------------------------------------------------------------------
# 共用测试替身
# ---------------------------------------------------------------------------


class _FakeUniform:
    def __init__(self) -> None:
        self.value: Any = None


class _FakeProgram:
    def __init__(self, names) -> None:
        self.uniforms = {name: _FakeUniform() for name in names}

    def __getitem__(self, name: str) -> _FakeUniform:
        return self.uniforms[name]


class _FakeTexture:
    def __init__(self, size) -> None:
        self.size = tuple(size)

    def use(self, index: int = 0) -> None:
        pass


class _FakeSurface:
    def __init__(self, size) -> None:
        self.size = tuple(size)
        self.texture = _FakeTexture(size)

    def use(self) -> None:
        pass


class _RecordingSurfaces:
    """记录 cache.get/peek 调用的假表面缓存（key, w, h）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, int, int]] = []

    def get(self, key, width, height, components: int = 4,
            dtype: str = "f2"):
        self.calls.append((key, width, height))
        return _FakeSurface((width, height))

    def peek(self, key):
        self.calls.append((key, -1, -1))
        return _FakeSurface((8, 8))

    @property
    def keys(self) -> list[Any]:
        return [call[0] for call in self.calls]


class _FakeCtx:
    def __init__(self) -> None:
        self.viewport: Any = None


class _FakeQuad:
    def draw(self, program) -> None:
        pass


# ---------------------------------------------------------------------------
# AUD-0907: 表面缓存键去尺寸
# ---------------------------------------------------------------------------


class TestAUD0907SurfaceCacheKeys:
    def _spatial(self, surfaces: _RecordingSurfaces) -> VideoSpatialDenoiser:
        d = VideoSpatialDenoiser.__new__(VideoSpatialDenoiser)
        d._ctx = _FakeCtx()
        d._quad = _FakeQuad()
        d._surfaces = surfaces
        d._programs = {
            "spatial_fast": _FakeProgram(
                ["u_texture", "u_texel", "u_luma_strength",
                 "u_chroma_strength", "u_edge_threshold", "u_radius"]),
            "spatial_sep": _FakeProgram(
                ["u_texture", "u_texel", "u_dir", "u_luma_strength",
                 "u_chroma_strength", "u_edge_threshold", "u_radius",
                 "u_pass_index"]),
            "spatial_down": _FakeProgram(["u_texture", "u_texel"]),
            "spatial_up_detail": _FakeProgram(
                ["u_original", "u_denoised", "u_lowfreq",
                 "u_detail_recovery"]),
            "detail_recovery": _FakeProgram(
                ["u_original", "u_denoised", "u_detail_recovery"]),
        }
        d.last_mode = None
        d.last_pass_count = 0
        return d

    def test_spatial_keys_have_no_size(self):
        surfaces = _RecordingSurfaces()
        d = self._spatial(surfaces)
        p = SpatialDenoiseParams(
            enabled=True, mode="fast", luma=50.0, chroma=50.0)
        d._process_fast(_FakeTexture((1920, 1080)), p, 1920, 1080)
        d._process_fast(_FakeTexture((640, 360)), p, 640, 360)
        # 键不再内嵌尺寸：同 key 由 RenderSurfaceCache 尺寸不匹配重建逻辑
        # 复用同一槽位，多分辨率场景 GPU 表面数有界。
        assert surfaces.keys == ["spatial_out", "spatial_out"]

    def test_spatial_all_paths_share_size_free_keys(self):
        surfaces = _RecordingSurfaces()
        d = self._spatial(surfaces)
        p = SpatialDenoiseParams(
            enabled=True, mode="enhanced", luma=50.0, chroma=50.0,
            detail_recovery=0.0)
        d._process_enhanced(_FakeTexture((128, 64)), p, 128, 64)
        d._process_separable(_FakeTexture((128, 64)), p, 128, 64)
        d._apply_detail_recovery(
            _FakeTexture((128, 64)), _FakeTexture((128, 64)), p, 128, 64)
        assert all(isinstance(key, str) for key in surfaces.keys)
        # enhanced 需要 half 分辨率表面——半/全分辨率各自占一个键，
        # 但键集合固定（不随出现过的分辨率种类增长）。
        assert set(surfaces.keys) == {
            "enh_low", "enh_low_mid", "enh_low_out", "spatial_out",
            "spatial_mid", "spatial_detail_out",
        }

    def _temporal(self, surfaces: _RecordingSurfaces) -> VideoTemporalDenoiser:
        d = VideoTemporalDenoiser.__new__(VideoTemporalDenoiser)
        d._ctx = _FakeCtx()
        d._quad = _FakeQuad()
        d._surfaces = surfaces
        d._programs = {
            "luma_quarter": _FakeProgram(["u_texture", "u_texel"]),
            "copy": _FakeProgram(["u_texture"]),
            "temporal_accum": _FakeProgram(
                ["u_current", "u_history", "u_motion", "u_luma_strength",
                 "u_chroma_strength", "u_threshold", "u_blend",
                 "u_noise_sigma", "u_scene_cut_threshold",
                 "u_history_frames"]),
            "motion_match": _FakeProgram(
                ["u_current_luma", "u_previous_luma", "u_luma_texel",
                 "u_search_radius", "u_match_scale"]),
            "motion_smooth": _FakeProgram(["u_motion", "u_texel"]),
            "debug_overlay": _FakeProgram(["u_texture", "u_motion"]),
        }
        d.state = TemporalDenoiseState()
        d._hist_index = 0
        d._prev_luma_valid = False
        d._last_motion_key = "temporal_motion"
        d.stats = {"frames_processed": 0, "resets": 0, "passthrough": 0}
        return d

    def test_temporal_causal_keys_have_no_size(self):
        surfaces = _RecordingSurfaces()
        d = self._temporal(surfaces)
        p = TemporalDenoiseParams(enabled=True, luma=50.0, chroma=50.0)
        d._process_causal(
            _FakeTexture((1280, 720)), p, 1280, 720, history_usable=False)
        assert all(isinstance(key, str) for key in surfaces.keys)
        assert set(surfaces.keys) >= {
            "temporal_luma_cur", "temporal_hist_a", "temporal_hist_b",
            "temporal_out", "temporal_luma_prev",
        }

    def test_temporal_window_and_debug_keys_have_no_size(self):
        surfaces = _RecordingSurfaces()
        d = self._temporal(surfaces)
        p = TemporalDenoiseParams(enabled=True, luma=50.0, chroma=50.0)
        d._replay_window(
            [_FakeTexture((128, 64)), _FakeTexture((128, 64))], p, 128, 64)
        window_keys = {k for k in surfaces.keys if k.startswith("window_")}
        assert window_keys == {
            "window_temporal_luma_cur", "window_temporal_luma_prev",
            "window_temporal_luma_tmp", "window_temporal_hist_a",
            "window_temporal_hist_b", "window_temporal_out",
            "window_temporal_motion", "window_temporal_motion_sm",
        }
        d.debug_overlay(_FakeTexture((128, 64)))
        assert "temporal_debug" in surfaces.keys
        assert all(isinstance(key, str) for key in surfaces.keys)

    def test_real_surface_cache_rebuilds_on_size_change(self):
        """尺寸变化复用同一 key 槽位（RenderSurfaceCache 契约端到端）。"""
        moderngl = pytest.importorskip("moderngl")
        from src.features.color.core.video_denoise_gl import (
            RenderSurfaceCache,
        )
        try:
            ctx = moderngl.create_standalone_context()
        except Exception as exc:  # pragma: no cover - 无 GPU 环境跳过
            pytest.skip(f"standalone ModernGL context unavailable: {exc}")
        try:
            cache = RenderSurfaceCache(ctx)
            s1 = cache.get("k", 64, 64)
            assert s1 is not None
            s2 = cache.get("k", 32, 32)
            assert s2 is not None and s2.size == (32, 32)
            assert cache.surface_count == 1
        finally:
            ctx.release()


# ---------------------------------------------------------------------------
# AUD-1000: 状态机 reset 后同 identity 恢复
# ---------------------------------------------------------------------------


class TestAUD1000TemporalStateAfterReset:
    def test_reset_then_same_identity_recovers(self):
        s = TemporalDenoiseState()
        key = ("vd", 1)
        assert s.prepare("clip", 10, 1, key, (1920, 1080)) is False
        assert s.prepare("clip", 11, 1, key, (1920, 1080)) is True
        s.reset("manual")  # force_reset() 复用此入口
        # 同 clip/generation/params/size 的下一帧不得再落入
        # "同帧重绘"短路（旧代码 delta==0 恒命中，状态机永久卡死）。
        assert s.prepare("clip", 11, 1, key, (1920, 1080)) is False
        assert s.accumulated_frames == 1
        assert s.prepare("clip", 12, 1, key, (1920, 1080)) is True
        assert s.accumulated_frames == 2

    def test_reset_then_same_frame_index_advances(self):
        s = TemporalDenoiseState()
        key = ("vd", 1)
        s.prepare("clip", 5, 1, key, (960, 540))
        s.prepare("clip", 6, 1, key, (960, 540))
        s.reset("transient_gl_error")
        assert s.prepare("clip", 6, 1, key, (960, 540)) is False
        assert s.last_frame_index == 6
        assert s.prepare("clip", 7, 1, key, (960, 540)) is True

    def test_same_frame_redraw_semantics_preserved(self):
        """既有行为：真正的同帧重绘仍复用历史、不推进计数。"""
        s = TemporalDenoiseState()
        key = ("vd", 1)
        s.prepare("clip", 10, 1, key, (1920, 1080))
        s.prepare("clip", 11, 1, key, (1920, 1080))
        assert s.prepare("clip", 11, 1, key, (1920, 1080)) is True
        assert s.accumulated_frames == 2


# ---------------------------------------------------------------------------
# AUD-1001: _downsample_luma 使用被采样纹理自身尺寸
# ---------------------------------------------------------------------------


class TestAUD1001DownsampleTexel:
    def test_texel_uses_source_texture_size(self):
        d = VideoTemporalDenoiser.__new__(VideoTemporalDenoiser)
        d._ctx = _FakeCtx()
        d._quad = _FakeQuad()
        d._surfaces = _RecordingSurfaces()
        prog = _FakeProgram(["u_texture", "u_texel"])
        d._programs = {"luma_quarter": prog}
        # 邻帧 320x240（proxy 降采样上传）→ 目标 1/4 表面 80x60：
        # u_texel 必须按源纹理 320x240 计算，而非当前帧尺寸。
        d._downsample_luma(_FakeTexture((320, 240)), _FakeSurface((80, 60)))
        assert prog.uniforms["u_texel"].value == (1.0 / 320.0, 1.0 / 240.0)

    def test_full_res_source_keeps_equivalent_behavior(self):
        d = VideoTemporalDenoiser.__new__(VideoTemporalDenoiser)
        d._ctx = _FakeCtx()
        d._quad = _FakeQuad()
        d._surfaces = _RecordingSurfaces()
        prog = _FakeProgram(["u_texture", "u_texel"])
        d._programs = {"luma_quarter": prog}
        d._downsample_luma(_FakeTexture((128, 64)), _FakeSurface((32, 16)))
        assert prog.uniforms["u_texel"].value == (1.0 / 128.0, 1.0 / 64.0)


# ---------------------------------------------------------------------------
# AUD-0911: apply_soft_clip 补齐两条 elif 路径
# ---------------------------------------------------------------------------


class TestAUD0911SoftClipElifPaths:
    def test_toe_clamp_when_black_point_above_low(self):
        # black_point(0.2) >= low(-0.1)：GPU elif (c < black_point) 钳制，
        # 旧 CPU 实现完全缺失该路径。
        params = {"enabled": True, "low": -0.1, "black_point": 0.2,
                  "toe_strength": 1.0, "high": 1.0, "white_point": 1.1,
                  "shoulder_strength": 1.0, "exposure": 0.0, "contrast": 0.0}
        color = np.array([[-0.5, 0.1, 0.25]])
        out = GradingMath.apply_soft_clip(color, params)
        assert out[0, 0] == pytest.approx(0.2)
        assert out[0, 1] == pytest.approx(0.2)
        assert out[0, 2] == pytest.approx(0.25)  # >= black_point 不动

    def test_toe_compress_preserved_when_black_point_below_low(self):
        # 既有正确行为：bp < low 时软压缩照旧（通道全部落在 toe 区且
        # 相互接近，不触发 AUD-0912 新增的 RGB 平衡块）。
        params = {"enabled": True, "low": 0.0, "black_point": -0.1,
                  "toe_strength": 1.0, "high": 1.0, "white_point": 1.1,
                  "shoulder_strength": 1.0, "exposure": 0.0, "contrast": 0.0}
        color = np.array([[-0.5, -0.4, -0.3]])
        out = GradingMath.apply_soft_clip(color, params)
        rng = 0.1
        expected = [
            -0.1 + rng * np.power(1.0 + 1.0 * d / rng, -1.0)
            for d in (0.5, 0.4, 0.3)
        ]
        np.testing.assert_allclose(out[0], expected, atol=1e-12)

    def test_shoulder_clamp_when_white_point_below_high(self):
        # white_point(0.5) <= high(1.2)：GPU elif (max_c > white_point) 钳制，
        # 旧 CPU 实现完全缺失该路径（wp<=high 时什么都不做）。
        params = {"enabled": True, "low": -0.1, "black_point": -0.2,
                  "toe_strength": 1.0, "high": 1.2, "white_point": 0.5,
                  "shoulder_strength": 1.0, "exposure": 0.0, "contrast": 0.0}
        color = np.array([[0.4, 0.8, 0.9]])
        out = GradingMath.apply_soft_clip(color, params)
        assert out.max() == pytest.approx(0.5)
        # 比例保持：钳制后各通道 = 原 * 0.5/0.9
        assert out[0, 0] == pytest.approx(0.4 * 0.5 / 0.9)
        assert out[0, 1] == pytest.approx(0.8 * 0.5 / 0.9)

    def test_shoulder_compress_preserved_when_white_point_above_high(self):
        params = {"enabled": True, "low": -0.1, "black_point": -0.2,
                  "toe_strength": 1.0, "high": 1.0, "white_point": 1.1,
                  "shoulder_strength": 1.0, "exposure": 0.0, "contrast": 0.0}
        color = np.array([[1.0, 2.0, 3.0]])
        out = GradingMath.apply_soft_clip(color, params)
        # shoulder 映射后最大通道 < white_point，其余通道按比例缩放
        assert out.max() < 1.1
        assert out.max() > 1.0
        assert out[0, 0] == pytest.approx(out[0, 1] / 2.0)
        assert out[0, 1] == pytest.approx(out[0, 2] / 1.5)

    def test_values_within_range_untouched(self):
        params = {"enabled": True, "low": 0.0, "black_point": -0.1,
                  "toe_strength": 1.0, "high": 1.0, "white_point": 1.1,
                  "shoulder_strength": 1.0, "exposure": 0.0, "contrast": 0.0}
        color = np.array([[0.2, 0.5, 0.8]])
        out = GradingMath.apply_soft_clip(color, params)
        np.testing.assert_allclose(out, color, atol=1e-12)


# ---------------------------------------------------------------------------
# AUD-0912: 暗部保护级 + toe RGB 平衡
# ---------------------------------------------------------------------------


def _gpu_shadow_guard(x: float) -> float:
    """GLSL/MSL 权威公式（_grading_shadow_guard）。"""
    if x < -0.5:
        return -0.5 - (abs(x + 0.5) * 0.1)
    return x


class TestAUD0912DarkProtectionAndToeBalance:
    def test_grading_applies_shadow_guard(self):
        # 全零参数下步骤 1-4 不改值，暗部保护直接作用于输入：
        # luma = dot([-1.0, -0.2, 0.05]) < 0 → 触发压缩 + 通道平衡。
        color = np.array([[-1.0, -0.2, 0.05]])
        out = GradingMath.apply_grading(color, {})
        guarded = np.array([
            [_gpu_shadow_guard(-1.0), _gpu_shadow_guard(-0.2),
             _gpu_shadow_guard(0.05)]])
        luma = float((color @ np.array([0.299, 0.587, 0.114]))[0])
        min_ch = float(guarded.min())
        max_ch = float(guarded.max())
        assert max_ch - min_ch > 0.15 and min_ch < 0.0  # 平衡块生效条件
        factor = min(0.15, max(0.0, abs(min_ch) * 0.25))
        expected = guarded * (1.0 - factor) + luma * factor
        np.testing.assert_allclose(out, expected, atol=1e-7)

    def test_grading_guard_only_when_negative_luma(self):
        # 正常 SDR 输入（luma >= 0）不受暗部保护影响。
        color = np.array([[0.1, 0.5, 0.9]])
        out = GradingMath.apply_grading(color, {})
        np.testing.assert_allclose(out, color, atol=1e-7)

    def test_soft_clip_toe_balance_block(self):
        # toe 压缩后 RGB 差异 > 0.2 且最小通道 < low → 向 luma 方向平衡。
        params = {"enabled": True, "low": 0.0, "black_point": -0.1,
                  "toe_strength": 1.0, "high": 1.0, "white_point": 1.1,
                  "shoulder_strength": 1.0, "exposure": 0.0, "contrast": 0.0}
        color = np.array([[-0.5, 0.5, 0.5]])
        out = GradingMath.apply_soft_clip(color, params)
        # 手工复算 GPU 公式
        toe = -0.1 + 0.1 * np.power(1.0 + 1.0 * 0.5 / 0.1, -1.0)
        res = np.array([[toe, 0.5, 0.5]])
        toe_min, toe_max = toe, 0.5
        assert toe_max - toe_min > 0.2 and toe_min < 0.0
        factor = min(0.15, max(0.0, (toe_max - toe_min) * 0.25))
        luma = float((res @ np.array([0.299, 0.587, 0.114]))[0])
        t = factor * abs(toe_min)
        expected = res * (1.0 - t) + luma * t
        np.testing.assert_allclose(out, expected, atol=1e-7)
        # 平衡后 RGB 差异缩小
        assert (out.max() - out.min()) < (0.5 - toe)


# ---------------------------------------------------------------------------
# AUD-0913: hue_shift 不再 clip
# ---------------------------------------------------------------------------


class TestAUD0913HueShiftNoClip:
    def test_hdr_values_survive_hue_shift(self):
        # 仅 hue_shift 非零时，前置步骤不改值；GPU 直接对未 clip 的 color
        # 做 rgb2hsv。旧 CPU 实现先 clip(res, 0, 1) 会把 v=2.0 压到 1.0。
        color = np.array([[2.0, 1.0, 0.5]])
        out = GradingMath.apply_grading(color, {"hue_shift": 90.0})
        hsv = GradingMath.rgb2hsv(color)
        hsv[..., 0] = (hsv[..., 0] + 90.0 / 360.0) % 1.0
        expected = GradingMath.hsv2rgb(hsv)
        np.testing.assert_allclose(out, expected, atol=1e-7)
        assert out.max() > 1.5  # HDR 幅度保留（旧实现输出 <= 1.0）

    def test_negative_values_not_clipped_before_hue_shift(self):
        # 负通道但 luma >= 0（不触发暗部保护），仅 hue_shift 生效。
        color = np.array([[-0.1, 0.5, 0.5]])
        out = GradingMath.apply_grading(color, {"hue_shift": 45.0})
        hsv = GradingMath.rgb2hsv(color)
        hsv[..., 0] = (hsv[..., 0] + 45.0 / 360.0) % 1.0
        expected = GradingMath.hsv2rgb(hsv)
        np.testing.assert_allclose(out, expected, atol=1e-7)


# ---------------------------------------------------------------------------
# AUD-1005 / AUD-1006: workspace presets fail-safe
# ---------------------------------------------------------------------------


class _StubConfig:
    def __init__(self, store=None, get_error=None, set_error=None):
        self.store: dict = dict(store or {})
        self.get_error = get_error
        self.set_error = set_error

    def get(self, key):
        if self.get_error is not None:
            raise self.get_error
        return self.store.get(key)

    def set(self, key, value):
        if self.set_error is not None:
            raise self.set_error
        self.store[key] = value


class TestAUD1005ReadFailureFailSafe:
    def test_save_refuses_to_overwrite_when_read_fails(self):
        existing = {"items": [
            {"preset_id": "user.keep1", "name": "Keep1",
             "visible_panels": ["color.grading"]},
            {"preset_id": "user.keep2", "name": "Keep2",
             "visible_panels": ["color.lut"]},
        ]}
        config = _StubConfig(store={CONFIG_KEY: existing},
                              get_error=RuntimeError("transient IO"))
        assert save_user_workspace(config, "New", ["color.curves"]) is None
        # 未发生任何写回：既有数据不可能被覆盖
        assert CONFIG_KEY not in config.store or config.store[CONFIG_KEY] is existing

    def test_delete_refuses_when_read_fails(self):
        existing = {"items": [
            {"preset_id": "user.keep1", "name": "Keep1",
             "visible_panels": ["color.grading"]},
        ]}
        config = _StubConfig(store={CONFIG_KEY: existing},
                              get_error=RuntimeError("locked"))
        assert delete_user_workspace(config, "user.keep1") is False
        assert config.store[CONFIG_KEY] is existing

    def test_load_returns_empty_list_on_read_failure(self):
        config = _StubConfig(get_error=RuntimeError("boom"))
        assert load_user_workspaces(config) == []

    def test_save_still_works_when_data_genuinely_missing(self):
        config = _StubConfig()  # 无数据（非读取失败）
        preset = save_user_workspace(config, "Look", ["color.grading"])
        assert preset is not None
        assert load_user_workspaces(config)[0].name == "Look"


class TestAUD1006WriteFailureVisible:
    def test_save_returns_none_when_write_fails(self, caplog):
        config = _StubConfig(set_error=OSError("disk full"))
        preset = save_user_workspace(config, "Look", ["color.grading"])
        assert preset is None
        assert any("color_workspaces" in r.message for r in caplog.records)

    def test_delete_returns_false_when_write_fails(self):
        existing = {"items": [
            {"preset_id": "user.del", "name": "Del",
             "visible_panels": ["color.grading"]},
        ]}
        config = _StubConfig(store={CONFIG_KEY: existing},
                              set_error=OSError("read-only"))
        assert delete_user_workspace(config, "user.del") is False

    def test_save_success_returns_preset_and_persists(self):
        config = _StubConfig()
        preset = save_user_workspace(config, "Look", ["color.grading"])
        assert preset is not None
        stored = config.store[CONFIG_KEY]["items"]
        assert len(stored) == 1 and stored[0]["name"] == "Look"


# ---------------------------------------------------------------------------
# AUD-24200: layers 快照逐条容错 + 原子替换
# ---------------------------------------------------------------------------


def _layer_engine(layers):
    return SimpleNamespace(layers=list(layers), active_layer_index=0,
                           tracking_store=None)


class TestAUD24200LayerSnapshotValidation:
    def _hydrator(self):
        return ColorStackHydrator()

    def test_corrupt_entries_skipped_valid_restored(self, caplog):
        from src.features.color.core.layer import Layer

        valid = Layer("Good").to_dict(None)
        corrupt_blend = {"name": "Bad1", "blend_mode": None}  # bm.title() 抛错
        corrupt_non_dict = 42  # dict(42) 抛 TypeError
        stack = ColorStackData(
            stack_id="stack-a", layers=(valid, corrupt_blend, corrupt_non_dict),
            active_layer_index=2)
        engine = _layer_engine([Layer("Old")])
        self._hydrator().apply_stack_to_engine(stack, engine)
        assert len(engine.layers) == 1
        assert engine.layers[0].name == "Good"
        # active_layer_index 越界（指向被丢弃的层）时钳制
        assert engine.active_layer_index == 0
        assert any("corrupt layer" in r.message for r in caplog.records)

    def test_all_corrupt_keeps_current_engine_state(self):
        stack = ColorStackData(
            stack_id="stack-b",
            layers=({"name": "Bad", "blend_mode": None},),
            active_layer_index=0)
        from src.features.color.core.layer import Layer

        old = Layer("Old")
        engine = _layer_engine([old])
        self._hydrator().apply_stack_to_engine(stack, engine)
        # 整体回退：不替换、不清空
        assert engine.layers == [old]
        assert engine.active_layer_index == 0

    def test_valid_layers_fully_restored(self):
        from src.features.color.core.layer import Layer

        l0 = Layer("A")
        l1 = Layer("B")
        stack = ColorStackData(
            stack_id="stack-c",
            layers=(l0.to_dict(None), l1.to_dict(None)),
            active_layer_index=1)
        engine = _layer_engine([Layer("Old")])
        self._hydrator().apply_stack_to_engine(stack, engine)
        assert [ly.name for ly in engine.layers] == ["A", "B"]
        assert engine.active_layer_index == 1


# ---------------------------------------------------------------------------
# AUD-24201: collect 采集 video_denoise
# ---------------------------------------------------------------------------


class TestAUD24201CollectVideoDenoise:
    def test_collect_gathers_engine_video_denoise(self):
        from src.features.color.core.layer import Layer

        vd = {"enabled": True, "luma": 40.0, "chroma": 30.0}
        engine = SimpleNamespace(
            layers=[Layer("L")], active_layer_index=0, tracking_store=None,
            get_video_denoise_state=lambda: vd)
        stack = ColorStackHydrator().collect_stack_from_engine_and_panels(
            "s1", engine, {})
        assert stack.video_denoise == vd

    def test_collect_without_denoise_state(self):
        from src.features.color.core.layer import Layer

        engine = SimpleNamespace(
            layers=[Layer("L")], active_layer_index=0, tracking_store=None)
        stack = ColorStackHydrator().collect_stack_from_engine_and_panels(
            "s1", engine, {})
        assert stack.video_denoise is None

    def test_collect_roundtrips_denoise_through_apply(self):
        from src.features.color.core.layer import Layer

        # 引擎 get_video_denoise_state() 返回 VideoDenoiseParams.to_dict()
        # 的嵌套结构（temporal/spatial）。
        vd = {
            "temporal": {"enabled": True, "luma": 30.0, "chroma": 20.0},
            "spatial": {"enabled": True, "mode": "fast", "luma": 40.0,
                        "chroma": 30.0},
        }
        engine = SimpleNamespace(
            layers=[Layer("L")], active_layer_index=0, tracking_store=None,
            get_video_denoise_state=lambda: dict(vd))
        hydrator = ColorStackHydrator()
        stack = hydrator.collect_stack_from_engine_and_panels("s1", engine, {})
        assert stack.video_denoise == vd
        target = SimpleNamespace(layers=[Layer("T")], active_layer_index=0,
                                 tracking_store=None)
        hydrator.apply_stack_to_engine(stack, target)
        assert target.video_denoise_params is not None
        assert target.video_denoise_params.spatial.luma == 40.0

    def test_collect_with_mock_engine_keeps_none(self):
        from unittest.mock import MagicMock

        # MagicMock 引擎：get_video_denoise_state() 返回 MagicMock 非 dict
        # → 不携带 video_denoise（isinstance 过滤，不炸快照收集）。
        layer = MagicMock()
        layer.grading_params = {}
        layer.curves_data = {}
        layer.hsl_curve_data = {}
        layer.mixer_params = {}
        layer.soft_clip_params = {}
        layer.active_lut_path = None
        layer.lut_intensity = 1.0
        engine = MagicMock()
        engine.layers = [layer]
        engine.active_layer_index = 0
        engine.tracking_store = None
        stack = ColorStackHydrator().collect_stack_from_engine_and_panels(
            "s1", engine, {})
        assert stack.video_denoise is None


# ---------------------------------------------------------------------------
# AUD-24202: BITMAP/AI-matte inline 快照通道
# ---------------------------------------------------------------------------


class TestAUD24202InlineBitmapRoundtrip:
    def test_bitmap_mask_and_ai_matte_survive_roundtrip(self):
        cv2 = pytest.importorskip("cv2")
        from src.features.color.core.layer import Layer, MaskShape

        layer = Layer("L")
        mask = MaskShape(MaskShape.BITMAP)
        mask.bitmap_data = (
            np.arange(64, dtype=np.float32).reshape(8, 8) / 63.0)
        layer.mask_shapes.append(mask)
        layer.ai_matte = np.full((8, 8), 0.5, dtype=np.float32)

        engine = SimpleNamespace(
            layers=[layer], active_layer_index=0, tracking_store=None)
        hydrator = ColorStackHydrator()
        stack = hydrator.collect_stack_from_engine_and_panels("s1", engine, {})

        snapshot = stack.layers[0]
        assert _AI_MATTE_INLINE_KEY in snapshot
        mask_dict = next(iter(snapshot["mask_shapes"]))
        assert _BITMAP_INLINE_KEY in mask_dict

        target = SimpleNamespace(
            layers=[Layer("T")], active_layer_index=0, tracking_store=None)
        hydrator.apply_stack_to_engine(stack, target)
        restored = target.layers[0]
        restored_mask = next(
            s for s in restored.mask_shapes if s.type == MaskShape.BITMAP)
        assert restored_mask.bitmap_data is not None
        np.testing.assert_allclose(
            restored_mask.bitmap_data, mask.bitmap_data, atol=1.0 / 255.0)
        assert restored.ai_matte is not None
        np.testing.assert_allclose(
            restored.ai_matte, layer.ai_matte, atol=1.0 / 255.0)

    def test_no_inline_payload_without_bitmap_data(self):
        from src.features.color.core.layer import Layer, MaskShape

        layer = Layer("L")
        layer.mask_shapes.append(MaskShape(MaskShape.CIRCLE))
        engine = SimpleNamespace(
            layers=[layer], active_layer_index=0, tracking_store=None)
        stack = ColorStackHydrator().collect_stack_from_engine_and_panels(
            "s1", engine, {})
        snapshot = stack.layers[0]
        assert _AI_MATTE_INLINE_KEY not in snapshot
        assert _BITMAP_INLINE_KEY not in snapshot["mask_shapes"][0]

    def test_nested_group_bitmap_survives_roundtrip(self):
        pytest.importorskip("cv2")
        from src.features.color.core.layer import Layer, MaskGroup, MaskShape

        layer = Layer("L")
        group = MaskGroup("G")
        mask = MaskShape(MaskShape.BITMAP)
        mask.bitmap_data = np.eye(4, dtype=np.float32)
        group.add_child(mask)
        layer.mask_shapes.append(group)

        engine = SimpleNamespace(
            layers=[layer], active_layer_index=0, tracking_store=None)
        hydrator = ColorStackHydrator()
        stack = hydrator.collect_stack_from_engine_and_panels("s1", engine, {})
        group_dict = stack.layers[0]["mask_shapes"][0]
        child_dict = group_dict["children"][0]
        assert _BITMAP_INLINE_KEY in child_dict

        target = SimpleNamespace(
            layers=[Layer("T")], active_layer_index=0, tracking_store=None)
        hydrator.apply_stack_to_engine(stack, target)
        restored_group = target.layers[0].mask_shapes[0]
        restored_mask = restored_group.children[0]
        assert restored_mask.bitmap_data is not None
        np.testing.assert_allclose(
            restored_mask.bitmap_data, np.eye(4, dtype=np.float32),
            atol=1.0 / 255.0)


# ---------------------------------------------------------------------------
# AUD-24301: Mapping 字段冻结
# ---------------------------------------------------------------------------


class TestAUD24301FrozenMappings:
    def test_grade_operation_parameters_readonly(self):
        source = {"gain": 1.1}
        op = GradeOperation(
            op_id="op1", kind=OP_PRIMARY, clip_id="c1", parameters=source)
        with pytest.raises(TypeError):
            op.parameters["gain"] = 2.0  # type: ignore[index]
        # 源 dict 后续修改不影响已冻结对象（hash 绑定不可被绕过）
        source["gain"] = 9.9
        assert dict(op.parameters) == {"gain": 1.1}
        plan = ColorGradePlan(
            plan_id="p", project_id="pr", timeline_id="t",
            context_snapshot_hash="h", base_revision=1, operations=(op,))
        before = plan.plan_hash()
        source["gain"] = 0.1
        assert plan.plan_hash() == before

    def test_context_snapshot_mappings_readonly(self):
        ctx = ColorContextSnapshot(
            project_id="p", timeline_id="t", metadata={"a": 1},
            detected_input_spaces={"clip": "rec709"})
        with pytest.raises(TypeError):
            ctx.metadata["b"] = 2  # type: ignore[index]
        with pytest.raises(TypeError):
            ctx.detected_input_spaces["clip"] = "srgb"  # type: ignore[index]

    def test_measurement_mappings_readonly(self):
        m = ColorMeasurement(
            clip_id="c", vectorscope_tint={"r": 1.0},
            histogram_quantiles={"p50": 0.4})
        with pytest.raises(TypeError):
            m.vectorscope_tint["r"] = 2.0  # type: ignore[index]
        with pytest.raises(TypeError):
            m.histogram_quantiles["p50"] = 0.5  # type: ignore[index]

    def test_violation_details_readonly(self):
        v = HDRViolation(code="X", severity="error", message="m",
                         details={"k": 1})
        with pytest.raises(TypeError):
            v.details["k"] = 2  # type: ignore[index]

    def test_look_and_brand_mappings_readonly(self):
        look = LookRecipe(recipe_id="r", version=1, label="L",
                          palette={"sky": "#87ceeb"})
        with pytest.raises(TypeError):
            look.palette["sky"] = "#000000"  # type: ignore[index]
        brand = BrandColorProfile(
            brand_id="b", label="B", primary_colors={"logo": "#ff0000"},
            max_delta_e={"logo": 2.0})
        with pytest.raises(TypeError):
            brand.primary_colors["logo"] = "#00ff00"  # type: ignore[index]
        with pytest.raises(TypeError):
            brand.max_delta_e["logo"] = 5.0  # type: ignore[index]

    def test_to_dict_still_serializable(self):
        op = GradeOperation(
            op_id="op1", kind=OP_PRIMARY, clip_id="c1", parameters={"g": 1.0})
        assert op.to_dict()["parameters"] == {"g": 1.0}


# ---------------------------------------------------------------------------
# AUD-24302: 未知 transfer_function 拒绝
# ---------------------------------------------------------------------------


_MISSING = object()


def _lut_plan(tf_value) -> ColorGradePlan:
    params = {} if tf_value is _MISSING else {"transfer_function": tf_value}
    return ColorGradePlan(
        plan_id="p", project_id="pr", timeline_id="t",
        context_snapshot_hash="h", base_revision=1, operations=(
            GradeOperation(op_id="lut", kind=OP_LUT, clip_id="c1",
                           parameters=params),
        ))


class TestAUD24302UnknownTransferFunction:
    def _ctx(self, tf: str = SDR) -> ColorContextSnapshot:
        return ColorContextSnapshot(
            project_id="p", timeline_id="t", timeline_transfer_function=tf)

    def test_unknown_tf_is_blocking_error(self):
        report = validate_hdr(_lut_plan("hdr10"), self._ctx())
        assert report.is_blocking
        assert any(
            v.code == "HDR_UNKNOWN_TRANSFER_FUNCTION" for v in report.errors)

    def test_none_tf_is_blocking_error(self):
        report = validate_hdr(_lut_plan(None), self._ctx())
        assert any(
            v.code == "HDR_UNKNOWN_TRANSFER_FUNCTION" for v in report.errors)

    def test_valid_sdr_on_sdr_timeline_passes(self):
        report = validate_hdr(_lut_plan(SDR), self._ctx())
        assert not report.violations

    def test_missing_tf_defaults_to_timeline(self):
        report = validate_hdr(_lut_plan(_MISSING), self._ctx(HDR_PQ))
        # 未显式指定 → 继承时间线 tf，不触发未知违规
        assert not any(
            v.code == "HDR_UNKNOWN_TRANSFER_FUNCTION"
            for v in report.violations)

    def test_sdr_lut_on_hdr_timeline_still_blocks(self):
        report = validate_hdr(_lut_plan(SDR), self._ctx(HDR_PQ))
        assert any(v.code == "HDR_SDR_LUT_ON_HDR_TIMELINE"
                   for v in report.errors)


# ---------------------------------------------------------------------------
# AUD-24400: 跟踪模式词表映射
# ---------------------------------------------------------------------------


def _fake_binding(mode: str, with_matrix: bool = True) -> SimpleNamespace:
    matrix = ((1.0, 0.0, 1.0), (0.0, 1.0, 2.0), (0.0, 0.0, 1.0))
    transform = SimpleNamespace(
        translation=(3.0, 4.0), scale=(1.1, 0.9), rotation=5.0,
        status=SimpleNamespace(value="valid"),
        matrix=(1.0, 0.0, 1.0, 0.0, 1.0, 2.0, 0.0, 0.0, 1.0)
        if with_matrix else None,
    )
    return SimpleNamespace(
        binding_id="b1", shape_name="win", clip_id="c1", reference_frame=0,
        mode=SimpleNamespace(value=mode),
        transforms={0: transform}, manual_corrections={},
    )


class TestAUD24400TrackingModeMapping:
    def _facade(self, bindings) -> ColorFacade:
        store = SimpleNamespace(all_bindings=lambda: list(bindings))
        engine = SimpleNamespace(tracking_store=store)
        return ColorFacade(SimpleNamespace(engine=engine))

    @pytest.mark.parametrize("color_mode,expected", [
        ("translation", "translation"),
        ("trs", "trs"),
        ("affine", "trs"),
        ("homography", "planar"),
        ("camera_3d", "planar"),
    ])
    def test_mode_vocabulary_mapping(self, color_mode, expected):
        payloads = self._facade([_fake_binding(color_mode)]). \
            export_tracking_bindings()
        assert len(payloads) == 1
        assert payloads[0]["mode"] == expected
        frame = payloads[0]["transforms"]["0"]
        assert frame["rotation"] == 5.0
        assert frame["scale_x"] == 1.1
        assert "matrix" in frame  # CornerPin bake 消费

    def test_unknown_mode_falls_back_with_warning(self, caplog):
        payloads = self._facade([_fake_binding("gibberish")]). \
            export_tracking_bindings()
        assert payloads[0]["mode"] == "translation"
        assert any("unmapped tracking mode" in r.message
                   for r in caplog.records)

    def test_exported_modes_within_composite_vocabulary(self):
        from src.features.composite.nodes.tracker_node import TRACKER_MODES

        bindings = [_fake_binding(m)
                    for m in ("translation", "trs", "affine", "homography")]
        payloads = self._facade(bindings).export_tracking_bindings()
        assert {p["mode"] for p in payloads} <= set(TRACKER_MODES)


# ---------------------------------------------------------------------------
# AUD-24500: 环路径不再清空在途 trackerball 批处理
# ---------------------------------------------------------------------------


class _FakeHubClient:
    def __init__(self) -> None:
        self.sent: list[tuple[int, float]] = []

    def send_parameter_value(self, parameter_id, value, at_default):
        self.sent.append((parameter_id, float(value)))
        return True


class _FakeTimer:
    def __init__(self, active: bool = False) -> None:
        self._active = active

    def isActive(self) -> bool:
        return self._active

    def start(self) -> None:
        self._active = True

    def stop(self) -> None:
        self._active = False


class _FakeGradingPanel:
    """同步反馈 grading_changed 的假调色面板（12 参数签名）。"""

    def __init__(self, bridge) -> None:
        self._bridge = bridge
        self.params: dict[str, Any] = {
            "lift": (10.0, 0.2, 0.0), "gamma": (0.0, 0.0, 0.0),
            "gain": (0.0, 0.0, 0.0), "exposure": 0.0, "temp": 0.0,
            "tint": 0.0, "contrast": 0.0, "shadow": 0.0, "saturation": 0.0,
            "vibrance": 0.0, "mid_detail": 0.0, "hue_shift": 0.0,
        }
        self.set_params_calls: list[tuple] = []

    def get_current_params(self) -> dict[str, Any]:
        return dict(self.params)

    def set_params(self, lift, gamma, gain, exposure, temp, tint, contrast,
                   shadow, saturation, vibrance, mid_detail, hue_shift,
                   emit=False):
        self.params.update({
            "lift": tuple(lift), "gamma": tuple(gamma), "gain": tuple(gain),
            "exposure": exposure, "temp": temp, "tint": tint,
            "contrast": contrast, "shadow": shadow,
            "saturation": saturation, "vibrance": vibrance,
            "mid_detail": mid_detail, "hue_shift": hue_shift,
        })
        self.set_params_calls.append(tuple(lift))
        if emit:
            # 模拟真实面板：解除 _syncing_ui 后同步 emit grading_changed
            self._bridge._on_panel_changed(
                tuple(lift), tuple(gamma), tuple(gain),
                exposure, temp, tint, contrast, shadow, saturation,
                vibrance, mid_detail, hue_shift)


def _make_bridge():
    from src.features.color.integrations.tangent_bridge import (
        TangentColorBridge, _TrackerballPoint,
    )

    bridge = TangentColorBridge.__new__(TangentColorBridge)
    bridge._panel = _FakeGradingPanel(bridge)
    bridge._main_window = SimpleNamespace(
        on_interaction_started=lambda: None, on_interaction_ended=lambda: None)
    bridge._gesture_open = True
    bridge._gesture_timer = _FakeTimer()
    bridge._flushing_trackerball = False
    bridge._sensitivity = {}
    bridge._trackerball_timer = _FakeTimer(active=True)  # 球批处理在途
    bridge._trackerball_points = {
        wheel: _TrackerballPoint() for wheel in ("lift", "gamma", "gain")}
    bridge._wheel_rotation_degrees = 0.0
    bridge._hue_direction = 1.0
    bridge._last_sent_display_values = {}
    bridge.client = _FakeHubClient()
    return bridge


class TestAUD24500RingPathKeepsTrackerballState:
    RING_LIFT = 0x00310024  # _WheelParameter("lift", 2, ...) 环/亮度

    def test_ring_change_preserves_pending_trackerball(self):
        from src.features.color.integrations.tangent_bridge import (
            _TrackerballPoint,
        )

        bridge = _make_bridge()
        state: _TrackerballPoint = bridge._trackerball_points["lift"]
        state.pending_x = 0.3
        state.pending_y = -0.15
        state.pending_axes = 3
        state.pending_started_ns = 123
        state.velocity_tracking_available = True

        bridge._on_parameter_change(self.RING_LIFT, 5.0)

        # 在途批处理不被环路径回灌清空（旧实现被无条件重置为 0）
        assert state.pending_x == 0.3
        assert state.pending_y == -0.15
        assert state.pending_axes == 3
        assert state.pending_started_ns == 123
        assert state.velocity_tracking_available is True
        # 环值确实发布到了面板
        assert bridge._panel.set_params_calls, "ring value must publish"
        assert bridge._panel.params["lift"][2] == 1.0  # clamp(-1,1)
        assert bridge.client.sent  # Hub 窄同步

    def test_genuine_ui_edit_still_resets_pending(self):
        """真实 UI 编辑（非桥自身回灌）仍重置在途状态（旧行为保留）。"""
        from src.features.color.integrations.tangent_bridge import (
            _TrackerballPoint,
        )

        bridge = _make_bridge()
        state: _TrackerballPoint = bridge._trackerball_points["lift"]
        state.pending_x = 0.3
        state.pending_axes = 1

        # 直接模拟面板信号（_flushing_trackerball=False、非环路径发布）
        bridge._on_panel_changed(
            (10.0, 0.2, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0),
            0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        assert state.pending_x == 0.0
        assert state.pending_axes == 0
