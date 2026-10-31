"""Plan-02 补测：ImageEngine（src/features/color/core/image_engine.py）覆盖补齐。

聚焦可 headless 验证的单元：
- 图层/参数状态管理（add/remove/bake/unbake/插件/SAM 会话）
- 参数应用与快照/恢复路径（_restore_state、undo/redo、apply_changes）
- 蒙版与 bake 状态机（_update_mask_textures、_validate_baked_prefixes）
- 异常/边界路径（decompile 失败、full-res 注册失败、渲染入口互斥）

GPU 真实路径全部 mock（renderer / GLContextManager / FileIO）；
与 tests/color/test_image_engine_{state,layers,params,history,transforms}.py
及 test_bake_prefix_semantics.py 互补，不重复既有断言。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.features.color.core.image_engine import (
    GRAIN_ANIMATION_STEP,
    ImageEngine,
    _layer_render_data,
    effective_grain_seed,
)
from src.features.color.core.layer import Layer, MaskGroup, MaskShape
from src.shared.video.render_commands import RenderCommand


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _small_image(h: int = 4, w: int = 6) -> np.ndarray:
    """确定性的小 BGR uint8 测试图。"""
    rng = np.arange(h * w * 3, dtype=np.uint8).reshape(h, w, 3)
    return rng


def _make_circle(size: float = 0.6, operation: str = "add") -> MaskShape:
    shape = MaskShape(MaskShape.CIRCLE)
    shape.center = [0.5, 0.5]
    shape.size = [size, size]
    shape.operation = operation
    shape.feather = 0  # MaskShape 默认 feather=50（大范围羽化），测试要锐利边缘
    return shape


def _spy(engine: ImageEngine, signal_name: str) -> List[Tuple[Any, ...]]:
    """收集 Qt 信号参数的简易 spy。"""
    records: List[Tuple[Any, ...]] = []

    def _record(*args: Any) -> None:
        records.append(args)

    getattr(engine, signal_name).connect(_record)
    return records


def _submit_calls(engine: ImageEngine, command: Any) -> List[Any]:
    """提取 renderer.submit 收到的指定命令 payload 列表。"""
    return [
        c.args[1]
        for c in engine.renderer.submit.call_args_list
        if c.args and c.args[0] == command
    ]


# ---------------------------------------------------------------------------
# A. 纯函数：grain seed / 摘要 / 请求 ID
# ---------------------------------------------------------------------------

class TestEffectiveGrainSeed:
    def test_static_seed_unchanged_without_animation(self) -> None:
        params: Dict[str, Any] = {"seed": 0.25, "animated": False}
        assert effective_grain_seed(params, frame_index=123) == 0.25

    def test_animated_advances_by_golden_ratio_step(self) -> None:
        params: Dict[str, Any] = {"seed": 0.1, "animated": True}
        expected = (0.1 + 3 * GRAIN_ANIMATION_STEP) % 1.0
        assert effective_grain_seed(params, frame_index=3) == pytest.approx(expected)

    def test_animated_wraps_modulo_one(self) -> None:
        params: Dict[str, Any] = {"seed": 0.99, "animated": True}
        value = effective_grain_seed(params, frame_index=1)
        assert 0.0 <= value < 1.0
        assert value == pytest.approx((0.99 + GRAIN_ANIMATION_STEP) % 1.0)

    def test_negative_frame_keeps_base_seed(self) -> None:
        params: Dict[str, Any] = {"seed": 0.4, "animated": True}
        assert effective_grain_seed(params, frame_index=-1) == 0.4

    def test_missing_or_falsy_seed_defaults_to_zero(self) -> None:
        assert effective_grain_seed({"animated": False}, frame_index=0) == 0.0
        assert effective_grain_seed({"seed": None, "animated": False}, 0) == 0.0


class TestFilmGrainHelpers:
    def test_reseed_film_grain_is_deterministic(self, image_engine: ImageEngine) -> None:
        image_engine.film_grain_params["seed"] = 0.2
        first = image_engine.reseed_film_grain()
        image_engine.film_grain_params["seed"] = 0.2
        second = image_engine.reseed_film_grain()
        assert first == pytest.approx((0.2 + 1.0 - GRAIN_ANIMATION_STEP) % 1.0)
        assert first == second
        assert image_engine.film_grain_params["seed"] == pytest.approx(first)

    def test_effective_params_copy_without_animation(self, image_engine: ImageEngine) -> None:
        image_engine.film_grain_params.update({"seed": 0.7, "animated": False})
        effective = image_engine._effective_film_grain_params()
        assert effective["seed"] == 0.7
        assert effective is not image_engine.film_grain_params

    def test_effective_params_animated_uses_renderer_frame(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.renderer.current_video_frame_index = 5
        engine.film_grain_params.update({"seed": 0.1, "animated": True})
        effective = engine._effective_film_grain_params()
        assert effective["seed"] == pytest.approx(
            (0.1 + 5 * GRAIN_ANIMATION_STEP) % 1.0)
        # 原参数不被帧求值污染
        assert engine.film_grain_params["seed"] == 0.1

    def test_effective_params_animated_without_renderer_keeps_seed(
        self, image_engine: ImageEngine
    ) -> None:
        engine = image_engine
        assert engine.renderer is None
        engine.film_grain_params.update({"seed": 0.3, "animated": True})
        assert engine._effective_film_grain_params()["seed"] == 0.3


class TestNextRenderRequestId:
    def test_ids_are_unique_and_ordered(self, image_engine: ImageEngine) -> None:
        first = image_engine._next_render_request_id()
        second = image_engine._next_render_request_id()
        third = image_engine._next_render_request_id()
        assert first.startswith("render-")
        assert (first, second, third) == (
            "render-00000001", "render-00000002", "render-00000003"
        )

    def test_counter_recovers_from_none(self, image_engine: ImageEngine) -> None:
        image_engine._render_request_counter = None
        assert image_engine._next_render_request_id() == "render-00000001"


class TestCanonicalDigestPart:
    def test_dict_key_order_does_not_matter(self) -> None:
        a = ImageEngine._canonical_digest_part({"x": 1, "y": [2, 3]})
        b = ImageEngine._canonical_digest_part({"y": [2, 3], "x": 1})
        assert a == b

    def test_ndarray_dtype_and_shape_sensitivity(self) -> None:
        base = np.ones((4, 4), dtype=np.float32)
        other_dtype = np.ones((4, 4), dtype=np.float64)
        other_shape = np.ones((2, 8), dtype=np.float32)
        d_base = ImageEngine._canonical_digest_part(base)
        assert d_base != ImageEngine._canonical_digest_part(other_dtype)
        assert d_base != ImageEngine._canonical_digest_part(other_shape)
        assert d_base == ImageEngine._canonical_digest_part(base.copy())

    def test_list_and_tuple_digest_equal(self) -> None:
        assert (ImageEngine._canonical_digest_part([1, 2, 3])
                == ImageEngine._canonical_digest_part((1, 2, 3)))

    def test_scalar_repr_distinction(self) -> None:
        assert (ImageEngine._canonical_digest_part(1)
                != ImageEngine._canonical_digest_part(1.0))


class TestPrefixDigestBakedFlagExclusion:
    def test_last_dto_is_baked_flag_excluded(self, image_engine: ImageEngine) -> None:
        """末位 DTO 的 is_baked 不参与摘要（bake 完成后 False→True 不得误判过期）。"""
        engine = image_engine
        engine.layers = [Layer("L0"), Layer("L1")]
        prefix = [_layer_render_data(l) for l in engine.layers]
        before = engine._compute_prefix_digest(prefix)

        engine.layers[1].is_baked = True
        after = engine._compute_prefix_digest(
            [_layer_render_data(l) for l in engine.layers])
        assert before == after

    def test_non_last_dto_is_baked_flag_counts(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.layers = [Layer("L0"), Layer("L1")]
        engine.layers[1].is_baked = True
        baseline = engine._compute_prefix_digest(
            [_layer_render_data(l) for l in engine.layers])

        engine.layers[0].is_baked = True
        changed = engine._compute_prefix_digest(
            [_layer_render_data(l) for l in engine.layers])
        assert changed != baseline


# ---------------------------------------------------------------------------
# B. 线性传输探测（M1-F5）
# ---------------------------------------------------------------------------

class TestPathTransferIsLinear:
    def test_non_toos_path_is_not_linear(self) -> None:
        assert ImageEngine._path_transfer_is_linear("/tmp/photo.png") is False

    def test_toos_linear_probe_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.shared.media.topos_image_source as tis

        calls: List[str] = []

        class _FakeSource:
            def __init__(self, path: str) -> None:
                calls.append("init")

            def open(self) -> None:
                calls.append("open")

            def probe(self) -> MagicMock:
                meta = MagicMock()
                meta.color_transfer = "linear"
                return meta

            def close(self) -> None:
                calls.append("close")

        monkeypatch.setattr(tis, "ToposImageSource", _FakeSource)
        assert ImageEngine._path_transfer_is_linear("/tmp/linear.toos") is True
        assert "close" in calls, "探测后必须关闭源"

    def test_toos_non_linear_probe_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.shared.media.topos_image_source as tis

        class _FakeSource:
            def open(self) -> None:
                return None

            def probe(self) -> MagicMock:
                meta = MagicMock()
                meta.color_transfer = "srgb"
                return meta

            def close(self) -> None:
                return None

        monkeypatch.setattr(tis, "ToposImageSource", _FakeSource)
        assert ImageEngine._path_transfer_is_linear("/tmp/gamma.toos") is False

    def test_probe_exception_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.shared.media.topos_image_source as tis

        class _BrokenSource:
            def __init__(self, path: str) -> None:
                raise IOError("corrupt header")

        monkeypatch.setattr(tis, "ToposImageSource", _BrokenSource)
        assert ImageEngine._path_transfer_is_linear("/tmp/bad.toos") is False


# ---------------------------------------------------------------------------
# C. set_image / RAW 参数状态
# ---------------------------------------------------------------------------

class TestSetImage:
    def _prepare(self, engine: ImageEngine) -> None:
        engine.renderer = MagicMock()
        engine.crop_params = {"rect": (0.1, 0.1, 0.5, 0.5), "rotation": 12.0}
        engine._flip_h = True
        engine._flip_v = True
        engine.ai_plugin_result_cache[("dead", "beef")] = np.zeros((2, 2, 3))

    def test_new_file_resets_transient_state(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        self._prepare(engine)
        img = _small_image()
        epoch_before = engine._source_epoch

        engine.set_image(img, img, img, is_new_file=True, path="/tmp/a.png")

        assert engine._source_epoch == epoch_before + 1
        assert engine.current_file_path == "/tmp/a.png"
        assert engine.crop_params == {"rect": (0.0, 0.0, 1.0, 1.0), "rotation": 0.0}
        assert engine._flip_h is False and engine._flip_v is False
        assert engine.ai_plugin_result_cache == {}
        assert engine.is_current_image_srgb is True
        assert engine.current_raw_metadata["camera_wb"] == [1.0, 1.0, 1.0, 1.0]
        assert engine._last_applied_raw_params["user_wb"] == [1.0, 1.0, 1.0, 1.0]
        assert engine.current_raw_params["gamma"] == "Linear"

    def test_normalizes_previews_to_float32(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        self._prepare(engine)
        img = _small_image()
        engine.set_image(img, img, img, is_new_file=True, path="/tmp/a.png")
        assert engine.img_high.dtype == np.float32
        assert engine.img_low.dtype == np.float32
        np.testing.assert_allclose(engine.img_high.max(), img.max() / 255.0, atol=1e-6)

    def test_uint16_input_normalized_by_65535(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        img16 = np.full((2, 2, 3), 65535, dtype=np.uint16)
        engine.set_image(img16, img16, img16, is_new_file=True, path="/tmp/b.png")
        assert engine.img_high.dtype == np.float32
        assert engine.img_high.max() == pytest.approx(1.0)

    def test_marks_all_layer_masks_dirty(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.img_full_original = _small_image()  # is_new_file=False 仍需当前图
        engine.add_layer()
        for layer in engine.layers:
            layer.mask_dirty = False
        img = _small_image()
        engine.set_image(img, img, img, is_new_file=False)
        assert all(layer.mask_dirty for layer in engine.layers)

    def test_submits_upload_and_reference(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        img = _small_image(4, 6)
        engine.set_image(img, img, img, is_new_file=True, path="/tmp/a.png")

        uploads = _submit_calls(engine, RenderCommand.UPLOAD_IMAGE)
        assert len(uploads) == 1
        assert uploads[0]["image"].dtype == np.float32
        assert uploads[0]["original_size"] == (6, 4)
        assert uploads[0]["is_video"] is False
        references = _submit_calls(engine, RenderCommand.UPLOAD_REFERENCE)
        assert len(references) == 1
        assert references[0].dtype == np.float32

    def test_raw_extension_selects_raw_defaults(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        img = _small_image()
        engine.set_image(img, img, img, is_new_file=True, path="/tmp/shot.arw")
        assert engine.is_current_image_srgb is False
        assert engine.current_raw_params["gamma"] == "sRGB"
        # rawpy 解码失败（文件不存在/未安装）时回退默认 camera_wb
        assert engine.current_raw_metadata["camera_wb"] == [2.0, 1.0, 1.5, 1.0]
        assert engine._last_applied_raw_params["user_wb"] == [2.0, 1.0, 1.5, 1.0]

    def test_none_full_is_rejected(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.set_image(None, None, None)
        assert engine.img_full_original is None
        engine.renderer.submit.assert_not_called()


class TestOnRawParamsChanged:
    def _base_params(self) -> Dict[str, Any]:
        return {
            "exposure": 0.5, "saturation": 150.0, "color_space": "Rec.709",
            "gamma": "sRGB", "wb_type": "camera",
        }

    def test_identical_params_short_circuit(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        params = self._base_params()
        engine._last_applied_raw_params = dict(params)
        engine.current_raw_params = {"sentinel": True}

        engine.on_raw_params_changed(dict(params))

        assert engine.current_raw_params == {"sentinel": True}
        engine.renderer.submit.assert_not_called()

    def test_without_file_path_only_stores_params(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.current_file_path = None
        params = self._base_params()

        engine.on_raw_params_changed(params)

        # AUD-0612：入口浅拷贝与调用方对象解耦，存储的是副本
        assert engine.current_raw_params == params
        assert engine.current_raw_params is not params
        engine.renderer.submit.assert_not_called()

    def test_non_raw_applies_uniforms_and_syncs_last_applied(
        self, image_engine: ImageEngine
    ) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.current_file_path = "/tmp/p.jpg"
        params = self._base_params()

        engine.on_raw_params_changed(params)

        uniforms = _submit_calls(engine, RenderCommand.UPDATE_UNIFORMS)
        assert len(uniforms) == 1
        raw = uniforms[0]["raw_preview"]
        assert raw["exposure"] == 0.5
        assert raw["saturation"] == pytest.approx(1.5)
        assert raw["color_space"] == 1
        assert raw["gamma"] == 1
        # camera_wb 基线 [2,1,1.5] 相对 last_decode [1,1,1] → (2,1,1.5)
        assert raw["gains"] == (2.0, 1.0, 1.5)
        # 非 RAW：立即同步 last applied；AUD-0612 入口拷贝后 user_wb
        # 写入引擎持有的副本，调用方 dict 不再被就地变异
        assert engine._last_applied_raw_params == {
            **params, "user_wb": [2.0, 1.0, 1.5, 1.0]}
        assert "user_wb" not in params

    def test_custom_wb_and_input_lut_change(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.current_file_path = "/tmp/r.arw"
        engine.is_interacting = True  # 避免 RAW worker 排队
        params = self._base_params()
        params.update({"wb_type": "custom", "temp": 5200, "tint": 3,
                       "input_lut": "/tmp/lut.cube"})

        with patch("src.shared.io.file_io.FileIO.calculate_wb_gains",
                   return_value=(2.0, 1.0, 0.5)) as wb_mock:
            engine.on_raw_params_changed(params)
        wb_mock.assert_called_once()

        uniforms = _submit_calls(engine, RenderCommand.UPDATE_UNIFORMS)
        assert uniforms[0]["raw_preview"]["gains"] == (2.0, 1.0, 0.5)
        aux = _submit_calls(engine, RenderCommand.UPDATE_AUX_TEX)
        lut_calls = [a for a in aux if a.get("type") == "raw_input_lut"]
        assert len(lut_calls) == 1
        assert lut_calls[0]["path"] == "/tmp/lut.cube"
        # RAW + 交互中：不重解码，last applied 的 user_wb 基线不被覆盖
        assert engine._last_applied_raw_params["user_wb"] == [1.0, 1.0, 1.0, 1.0]


class TestOnRawUpdateFinished:
    def test_none_full_ignored(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine._on_raw_update_finished(None, None, None, {"user_wb": [1, 1, 1, 1]})
        engine.renderer.submit.assert_not_called()

    def test_replaces_images_and_resets_shader_preview(
        self, image_engine: ImageEngine
    ) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        full = _small_image(8, 8)
        applied = {"user_wb": [3.0, 1.0, 1.0, 1.0], "exposure": 1.25}
        engine.layers[0].mask_dirty = False

        engine._on_raw_update_finished(full, full, full, applied)

        assert engine.img_full_original is full
        assert engine.img_high.dtype == np.float32
        assert engine.layers[0].mask_dirty is True
        assert engine._last_applied_raw_params == applied
        # 独立副本：后续修改 applied 不影响引擎状态
        applied["exposure"] = 9.9
        assert engine._last_applied_raw_params["exposure"] == 1.25

        uniforms = _submit_calls(engine, RenderCommand.UPDATE_UNIFORMS)
        assert uniforms and uniforms[0]["raw_preview"]["exposure"] == 0.0
        assert uniforms[0]["raw_preview"]["gains"] == (1.0, 1.0, 1.0)
        uploads = _submit_calls(engine, RenderCommand.UPLOAD_IMAGE)
        assert uploads and uploads[0]["original_size"] == (8, 8)
        references = _submit_calls(engine, RenderCommand.UPLOAD_REFERENCE)
        assert references, "解码完成后必须同步参考纹理"


# ---------------------------------------------------------------------------
# D. 离线模式状态机
# ---------------------------------------------------------------------------

class TestOfflineMode:
    def _baked_layer(self) -> Layer:
        layer = Layer("Baked")
        layer.is_baked = True
        layer.baked_file_name = "baked_x.exr"
        return layer

    def test_no_baked_layer_returns_false(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        assert engine.enter_offline_mode([Layer("L0")]) is False
        assert engine.is_offline_mode is False

    def test_failed_cache_load_returns_false(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        baked = self._baked_layer()
        with patch("src.shared.io.file_io.FileIO.load_baked_layer",
                   return_value=None):
            assert engine.enter_offline_mode([Layer("L0"), baked]) is False
        assert engine.is_offline_mode is False
        assert engine.img_full_original is None

    def test_enter_builds_black_canvas_and_uploads(
        self, image_engine: ImageEngine
    ) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.crop_params = {"rect": (0.2, 0.2, 0.3, 0.3), "rotation": 45.0}
        engine._flip_h = True
        baked = self._baked_layer()
        cached = np.ones((8, 12, 3), dtype=np.float32)

        with patch("src.shared.io.file_io.FileIO.load_baked_layer",
                   return_value=cached) as load_mock, \
             patch("src.shared.io.file_io.FileIO._resize_to_fit",
                   side_effect=lambda img, size: img):
            assert engine.enter_offline_mode([Layer("L0"), baked]) is True

        assert engine.is_offline_mode is True
        assert engine._offline_canvas_size == (8, 12)
        assert engine.img_full_original.shape == (8, 12, 3)
        assert engine.img_full_original.dtype == np.uint8
        assert engine.img_full_original.max() == 0  # 纯黑占位画布
        assert engine.img_high.dtype == np.float32
        assert engine.current_file_path is None
        assert engine.is_current_image_srgb is True
        assert engine.crop_params["rotation"] == 0.0
        assert engine._flip_h is False
        assert load_mock.call_count == 2  # 尺寸探测 + 逐层上传

        uploads = _submit_calls(engine, RenderCommand.UPLOAD_IMAGE)
        assert uploads and uploads[0]["original_size"] == (12, 8)
        baked_uploads = _submit_calls(engine, RenderCommand.UPLOAD_BAKED_TEXTURE)
        assert len(baked_uploads) == 1
        assert baked_uploads[0]["layer_id"] == baked.id
        assert baked_uploads[0]["image_array"] is cached

    def test_exit_clears_flags(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.is_offline_mode = True
        engine._offline_canvas_size = (4, 4)
        engine.exit_offline_mode()
        assert engine.is_offline_mode is False
        assert engine._offline_canvas_size is None


# ---------------------------------------------------------------------------
# E. 蒙版状态机（CPU 侧）
# ---------------------------------------------------------------------------

class TestUpdateMaskTextures:
    def test_no_renderer_is_noop(self, image_engine: ImageEngine) -> None:
        image_engine.img_full_original = _small_image()
        image_engine.layers[0].mask_dirty = True
        image_engine._update_mask_textures()  # 不应抛错
        assert image_engine.layers[0].mask_dirty is True

    def test_no_image_is_noop(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.img_full_original = None
        engine.layers[0].mask_dirty = True
        engine._update_mask_textures()
        engine.renderer.submit.assert_not_called()
        assert engine.layers[0].mask_dirty is True

    def test_clean_layer_not_touched(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.img_full_original = _small_image()
        engine.layers[0].mask_dirty = False
        engine._update_mask_textures()
        engine.renderer.submit.assert_not_called()

    def test_no_visible_shapes_uploads_zero_mask(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.img_full_original = np.zeros((100, 100, 3), dtype=np.uint8)
        engine.layers[0].mask_dirty = True

        engine._update_mask_textures()

        aux = _submit_calls(engine, RenderCommand.UPDATE_AUX_TEX)
        masks = [a for a in aux if a["type"] == "mask"]
        assert len(masks) == 1
        assert masks[0]["size"] == (1024, 1024)
        assert masks[0]["data"] == bytes(1024 * 1024)
        assert engine.layers[0].mask_dirty is False

    def test_visible_shape_uploads_nonzero_mask_and_clears_dirty(
        self, image_engine: ImageEngine
    ) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.img_full_original = np.zeros((100, 100, 3), dtype=np.uint8)
        engine.layers[0].add_shape(MaskShape.CIRCLE)
        engine.layers[0].mask_shapes[0].size = [0.5, 0.5]

        engine._update_mask_textures()

        masks = [a for a in _submit_calls(engine, RenderCommand.UPDATE_AUX_TEX)
                 if a["type"] == "mask"]
        assert len(masks) == 1
        assert masks[0]["size"] == (1024, 1024)
        assert len(masks[0]["data"]) == 1024 * 1024
        assert max(masks[0]["data"]) > 0
        assert engine.layers[0].mask_dirty is False

    def test_interaction_mode_uses_reduced_base_size(
        self, image_engine: ImageEngine
    ) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.img_full_original = np.zeros((100, 100, 3), dtype=np.uint8)
        engine.is_interacting = True
        engine.layers[0].mask_dirty = True

        engine._update_mask_textures()

        masks = [a for a in _submit_calls(engine, RenderCommand.UPDATE_AUX_TEX)
                 if a["type"] == "mask"]
        assert masks[0]["size"] == (512, 512)

    def test_mask_size_preserves_aspect_ratio(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.img_full_original = np.zeros((50, 100, 3), dtype=np.uint8)
        engine.layers[0].mask_dirty = True
        engine._update_mask_textures()
        masks = [a for a in _submit_calls(engine, RenderCommand.UPDATE_AUX_TEX)
                 if a["type"] == "mask"]
        assert masks[0]["size"] == (1024, 512)

    def test_ai_matte_combine_operations(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.img_full_original = np.zeros((64, 64, 3), dtype=np.uint8)
        layer = engine.layers[0]
        layer.ai_matte = np.ones((64, 64), dtype=np.float32) * 0.5
        layer.ai_matte_enabled = True
        layer.ai_matte_operation = "add"
        shape = layer.add_shape(MaskShape.CIRCLE)
        shape.size = [0.3, 0.3]
        shape.feather = 0  # 锐利边缘，断言不受默认羽化影响
        layer.mask_dirty = True

        engine._update_mask_textures()

        masks = [a for a in _submit_calls(engine, RenderCommand.UPDATE_AUX_TEX)
                 if a["type"] == "mask"]
        data = np.frombuffer(masks[0]["data"], dtype=np.uint8).reshape(
            masks[0]["size"][1], masks[0]["size"][0]).astype(np.float32) / 255.0
        # add：圆外 0.5，圆内 max(1, 0.5) = 1
        assert data[0, 0] == pytest.approx(0.5, abs=0.02)
        assert data[data.shape[0] // 2, data.shape[1] // 2] == pytest.approx(1.0, abs=0.02)


class TestRenderMaskItemsRecursive:
    def test_first_visible_operation_is_ignored(self, image_engine: ImageEngine) -> None:
        a = _make_circle(0.6, operation="subtract")  # 首个可见项的 op 不生效
        mask = image_engine._render_mask_items_recursive([a], 64, 64, 64)
        assert mask.shape == (64, 64)
        assert mask.dtype == np.float32
        assert mask.max() == pytest.approx(1.0, abs=0.01)

    def test_boolean_operations(self, image_engine: ImageEngine) -> None:
        base = image_engine._render_mask_items_recursive(
            [_make_circle(0.6)], 64, 64, 64)
        subtracted = image_engine._render_mask_items_recursive(
            [_make_circle(0.6), _make_circle(0.3, "subtract")], 64, 64, 64)
        intersected = image_engine._render_mask_items_recursive(
            [_make_circle(0.6), _make_circle(0.3, "intersect")], 64, 64, 64)

        cy = cx = 32
        assert base[cy, cx] == pytest.approx(1.0, abs=0.01)
        assert subtracted[cy, cx] == pytest.approx(0.0, abs=0.01)
        assert intersected[cy, cx] == pytest.approx(1.0, abs=0.01)
        # 外圈（大圆内、小圆外）
        assert intersected[cy, 4] == pytest.approx(0.0, abs=0.05)
        assert subtracted[cy, 4] == pytest.approx(1.0, abs=0.01)

    def test_invisible_items_skipped(self, image_engine: ImageEngine) -> None:
        hidden = _make_circle(0.6)
        hidden.visible = False
        mask = image_engine._render_mask_items_recursive([hidden], 32, 32, 32)
        assert mask.max() == 0.0

    def test_group_opacity_and_invert(self, image_engine: ImageEngine) -> None:
        group = MaskGroup("G")
        group.children = [_make_circle(0.6)]
        group.opacity = 0.5
        mask = image_engine._render_mask_items_recursive([group], 64, 64, 64)
        assert mask.max() == pytest.approx(0.5, abs=0.02)

        group.invert = True
        inverted = image_engine._render_mask_items_recursive([group], 64, 64, 64)
        assert inverted.max() == pytest.approx(1.0, abs=0.01)
        assert inverted[32, 32] == pytest.approx(0.5, abs=0.02)

    def test_nested_group_recursion(self, image_engine: ImageEngine) -> None:
        inner = MaskGroup("inner")
        inner.children = [_make_circle(0.6)]
        outer = MaskGroup("outer")
        outer.children = [inner]
        mask = image_engine._render_mask_items_recursive([outer], 64, 64, 64)
        assert mask[32, 32] == pytest.approx(1.0, abs=0.01)

    def test_unknown_item_type_skipped(self, image_engine: ImageEngine) -> None:
        unknown = SimpleNamespace(visible=True)  # 非 shape/group 且可读 visible
        mask = image_engine._render_mask_items_recursive(
            [unknown], 16, 16, 16)  # type: ignore[list-item]
        assert mask.max() == 0.0


class TestHasVisibleMaskItems:
    def test_all_invisible_returns_false(self, image_engine: ImageEngine) -> None:
        shape = _make_circle()
        shape.visible = False
        assert image_engine._has_visible_mask_items([shape]) is False

    def test_visible_shape_returns_true(self, image_engine: ImageEngine) -> None:
        assert image_engine._has_visible_mask_items([_make_circle()]) is True

    def test_visible_group_with_visible_child(self, image_engine: ImageEngine) -> None:
        group = MaskGroup("G")
        group.children = [_make_circle()]
        assert image_engine._has_visible_mask_items([group]) is True

    def test_visible_group_with_invisible_children(self, image_engine: ImageEngine) -> None:
        group = MaskGroup("G")
        child = _make_circle()
        child.visible = False
        group.children = [child]
        assert image_engine._has_visible_mask_items([group]) is False

    def test_invisible_group_shadows_visible_child(self, image_engine: ImageEngine) -> None:
        group = MaskGroup("G")
        group.visible = False
        group.children = [_make_circle()]
        assert image_engine._has_visible_mask_items([group]) is False


class TestFastFeatherBlur:
    def test_small_radius_uses_gaussian(self, image_engine: ImageEngine) -> None:
        arr = np.zeros((64, 64), dtype=np.float32)
        arr[32, 32] = 1.0
        out = image_engine._fast_feather_blur(arr, 5)
        assert out.shape == arr.shape
        assert out.max() < 1.0
        assert out.sum() > 0.0

    def test_large_radius_uses_box_path(self, image_engine: ImageEngine) -> None:
        arr = np.zeros((96, 96), dtype=np.float32)
        arr[48, 48] = 1.0
        out = image_engine._fast_feather_blur(arr, 40)
        assert out.shape == arr.shape
        assert out.max() < 1.0
        assert out.sum() > 0.0


class TestBrushMaskRendering:
    def _brush_shape(self, strokes: List[Dict[str, Any]]) -> MaskShape:
        shape = MaskShape(MaskShape.BRUSH)
        shape.brush_strokes = strokes
        return shape

    def test_two_point_stroke_draws(self, image_engine: ImageEngine) -> None:
        shape = self._brush_shape([{
            "points": [(0.2, 0.5), (0.8, 0.5)],
            "size": 20, "opacity": 1.0, "feather": 0, "flow": 1.0,
        }])
        mask = image_engine._render_brush_mask_to_canvas(shape, 64, 64, 100)
        assert mask.shape == (64, 64)
        assert mask[32, 16] > 0.5
        assert mask[32, 48] > 0.5
        assert mask[5, 5] == 0.0

    def test_single_point_stroke_skipped(self, image_engine: ImageEngine) -> None:
        shape = self._brush_shape([{
            "points": [(0.5, 0.5)], "size": 20,
            "opacity": 1.0, "feather": 0, "flow": 1.0,
        }])
        mask = image_engine._render_brush_mask_to_canvas(shape, 32, 32, 100)
        assert mask.max() == 0.0

    def test_eraser_subtracts(self, image_engine: ImageEngine) -> None:
        strokes = [
            {"points": [(0.2, 0.5), (0.8, 0.5)], "size": 30,
             "opacity": 1.0, "feather": 0, "flow": 1.0},
            {"points": [(0.2, 0.5), (0.8, 0.5)], "size": 10,
             "opacity": 1.0, "feather": 0, "flow": 1.0, "is_eraser": True},
        ]
        shape = self._brush_shape(strokes)
        mask = image_engine._render_brush_mask_to_canvas(shape, 64, 64, 100)
        # 中心线被橡皮擦清除；同一 x、画笔覆盖但橡皮够不到的 y 保留
        assert mask[32, 32] == 0.0
        assert mask[15, 32] > 0.5


class TestGenerateShapeTexture:
    def test_bitmap_resized_to_canvas(self, image_engine: ImageEngine) -> None:
        shape = MaskShape(MaskShape.BITMAP)
        shape.bitmap_data = np.ones((4, 4), dtype=np.float32)
        arr = image_engine._generate_shape_texture(shape, 16, 16, 100)
        assert arr.shape == (16, 16)
        np.testing.assert_allclose(arr, 1.0, atol=0.01)

    def test_gradient_linear_ramp(self, image_engine: ImageEngine) -> None:
        shape = MaskShape(MaskShape.GRADIENT)
        shape.grad_points = [(0.0, 0.0), (1.0, 1.0)]
        arr = image_engine._generate_shape_texture(shape, 32, 32, 100)
        assert arr.shape == (32, 32)
        assert arr[0, 0] == pytest.approx(0.0, abs=0.02)
        # mgrid 用整数像素坐标：对角端点像素 t≈31/32，非精确 1.0
        assert arr[31, 31] == pytest.approx(1.0, abs=0.05)
        assert arr[0, 31] > arr[0, 0]

    def test_padded_canvas_dimensions(self, image_engine: ImageEngine) -> None:
        shape = _make_circle(0.5)
        arr = image_engine._generate_shape_texture(shape, 32, 32, 100, pad=8)
        assert arr.shape == (48, 48)


class TestRenderSingleShape:
    def test_result_is_cached(self, image_engine: ImageEngine) -> None:
        shape = _make_circle(0.5)
        first = image_engine._render_single_shape(shape, 48, 48, 100)
        assert shape._render_cache is not None
        second = image_engine._render_single_shape(shape, 48, 48, 100)
        np.testing.assert_array_equal(first, second)
        assert second is not shape._render_cache  # 返回副本，防调用方污染

    def test_opacity_and_invert_applied(self, image_engine: ImageEngine) -> None:
        shape = _make_circle(0.4)  # size 以画布宽为单位的半径：0.4*48=19.2px
        shape.opacity = 0.25
        mask = image_engine._render_single_shape(shape, 48, 48, 100)
        assert mask[24, 24] == pytest.approx(0.25, abs=0.02)
        assert mask[0, 0] == pytest.approx(0.0, abs=0.01)

        shape.invert = True
        inverted = image_engine._render_single_shape(shape, 48, 48, 100)
        assert inverted[0, 0] == pytest.approx(1.0, abs=0.01)
        assert inverted[24, 24] == pytest.approx(0.75, abs=0.02)

    def test_translation_moves_shape(self, image_engine: ImageEngine) -> None:
        shape = _make_circle(0.3)
        shape.center = [0.5, 0.5]
        centered = image_engine._render_single_shape(shape, 64, 64, 100)
        shape.center = [0.2, 0.5]
        moved = image_engine._render_single_shape(shape, 64, 64, 100)
        # 中心左移：原中心像素值下降，新中心保持峰值
        assert moved[32, 13] == pytest.approx(centered[32, 32], abs=0.05)
        assert moved[32, 32] < centered[32, 32]

    def test_rotation_rotates_spatial_layout(self, image_engine: ImageEngine) -> None:
        shape = _make_circle(0.7)
        shape.rotation = 45.0
        mask = image_engine._render_single_shape(shape, 64, 64, 100)
        assert mask.shape == (64, 64)
        assert mask[32, 32] == pytest.approx(1.0, abs=0.02)


# ---------------------------------------------------------------------------
# F. bake / unbake 状态机
# ---------------------------------------------------------------------------

class TestBakeLayerGuards:
    def test_without_renderer_returns_early(self, image_engine: ImageEngine) -> None:
        assert image_engine.renderer is None
        started = _spy(image_engine, "layer_baking_started")
        image_engine.bake_layer(0)
        assert started == []

    def test_invalid_index_returns_early(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        started = _spy(engine, "layer_baking_started")
        engine.bake_layer(99)
        assert started == []
        engine.renderer.submit.assert_not_called()

    def test_already_baked_layer_is_skipped(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.layers[0].is_baked = True
        started = _spy(engine, "layer_baking_started")
        engine.bake_layer(0)
        assert started == []
        engine.renderer.submit.assert_not_called()


class TestBakeLayerSubmission:
    def test_payload_and_pending_digest(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.layers = [Layer("L0"), Layer("L1")]
        engine.img_full_original = _small_image(4, 6)
        started = _spy(engine, "layer_baking_started")

        engine.bake_layer(1)

        assert started == [(1,)]
        payloads = _submit_calls(engine, RenderCommand.BAKE_LAYER)
        assert len(payloads) == 1
        payload = payloads[0]
        assert payload["layer_id"] == engine.layers[1].id
        assert payload["stack_prefix"][0]["id"] == engine.layers[0].id
        assert payload["source_size"] == (6, 4)
        full = payload["full_res_image"]
        assert full.dtype == np.float32
        assert full.shape == (4, 6, 4)  # BGR→RGB→RGBA
        # 摘要先入 pending 台账，完成时转正
        assert engine._pending_bake_digests[engine.layers[1].id] == payload["prefix_digest"]
        assert engine.layers[1].id not in engine._bake_prefix_digests

    def test_snapshot_always_unbaked(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.layers = [Layer("L0"), Layer("L1")]
        engine.layers[0].is_baked = True  # 底层已冻结不影响 bake 目标快照
        engine.img_full_original = None
        engine.bake_layer(1)
        payload = _submit_calls(engine, RenderCommand.BAKE_LAYER)[0]
        assert payload["layer"]["is_baked"] is False
        assert payload["layer"]["id"] == engine.layers[1].id


class TestBakeCompletion:
    def test_on_layer_baked_unknown_id_is_noop(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine._on_layer_baked("no-such-id", np.zeros((2, 2, 3), np.float32))
        engine.renderer.submit.assert_not_called()

    def test_on_baked_save_finished_promotes_state(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        layer = engine.layers[0]
        engine._pending_bake_digests[layer.id] = "digest-abc"
        # AUD-0608：完成信号携带 bake 代数，须与台账当前代数一致才转正
        engine._bake_generation[layer.id] = 1
        finished = _spy(engine, "layer_baking_finished")
        baked = np.zeros((4, 4, 3), np.float32)

        engine._on_baked_save_finished(layer.id, f"baked_{layer.id}.exr", baked, 1)

        assert layer.is_baked is True
        assert layer.baked_file_name == f"baked_{layer.id}.exr"
        assert layer.is_dirty is False
        assert engine._bake_prefix_digests[layer.id] == "digest-abc"
        assert layer.id not in engine._pending_bake_digests
        uploads = _submit_calls(engine, RenderCommand.UPLOAD_BAKED_TEXTURE)
        assert uploads and uploads[0]["layer_id"] == layer.id
        assert uploads[0]["image_array"] is baked
        assert finished == [(0,)]

    def test_on_baked_save_finished_without_pending_digest(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        layer = engine.layers[0]
        engine._bake_generation[layer.id] = 1  # AUD-0608：代数守卫须匹配
        engine._on_baked_save_finished(layer.id, "baked.exr", np.zeros((2, 2, 3), np.float32), 1)
        assert layer.is_baked is True
        assert layer.id not in engine._bake_prefix_digests


class TestUnbakeLayer:
    def test_offline_mode_blocks_unbake(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.is_offline_mode = True
        engine.layers[0].is_baked = True
        assert engine.unbake_layer(0) is False
        assert engine.layers[0].is_baked is True

    def test_invalid_index_returns_false(self, image_engine: ImageEngine) -> None:
        assert image_engine.unbake_layer(7) is False

    def test_unbake_cleans_all_resources(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        layer = engine.layers[0]
        layer.is_baked = True
        layer.baked_file_name = "baked_y.exr"
        engine._bake_prefix_digests[layer.id] = "d0"

        with patch("src.shared.io.file_io.FileIO.delete_baked_layer") as delete_mock:
            assert engine.unbake_layer(0) is True

        delete_mock.assert_called_once_with("baked_y.exr")
        assert layer.is_baked is False
        assert layer.baked_file_name is None
        assert layer.baked_texture_id is None
        assert layer.is_dirty is True
        assert layer.id not in engine._bake_prefix_digests
        clears = _submit_calls(engine, RenderCommand.CLEAR_BAKED_TEXTURE)
        assert clears == [layer.id]


class TestBakedPrefixValidation:
    def test_no_digests_is_noop(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.layers[0].is_baked = True  # 旧项目无台账
        engine._validate_baked_prefixes()
        assert engine.layers[0].is_baked is True
        engine.renderer.submit.assert_not_called()

    def test_pending_only_digest_does_not_validate(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        layer = engine.layers[0]
        layer.is_baked = True
        engine._pending_bake_digests[layer.id] = "bake-in-flight"
        engine._validate_baked_prefixes()
        assert layer.is_baked is True  # 进行中的 bake 不参与失效校验

    def test_source_replacement_invalidates_bake(self, image_engine: ImageEngine) -> None:
        """set_image（源图替换）→ 下次渲染入口校验自动解冻（P0-COL-04）。"""
        engine = image_engine
        engine.renderer = MagicMock()
        engine.layers = [Layer("L0"), Layer("L1")]
        target = engine.layers[1]
        digest = engine._compute_prefix_digest(
            [_layer_render_data(l) for l in engine.layers[:2]])
        engine._bake_prefix_digests[target.id] = digest
        target.is_baked = True
        target.baked_file_name = f"baked_{target.id}.exr"

        img = _small_image(4, 6)
        engine.set_image(img, img, img, is_new_file=True, path="/tmp/new.png")

        with patch("src.shared.io.file_io.FileIO.delete_baked_layer") as delete_mock:
            engine._validate_baked_prefixes()

        assert target.is_baked is False
        delete_mock.assert_called_once_with(f"baked_{target.id}.exr")
        assert _submit_calls(engine, RenderCommand.CLEAR_BAKED_TEXTURE) == [target.id]

    def test_upper_layer_change_keeps_lower_bake(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.layers = [Layer("L0"), Layer("L1"), Layer("L2")]
        bottom = engine.layers[0]
        digest = engine._compute_prefix_digest(
            [_layer_render_data(l) for l in engine.layers[:1]])
        engine._bake_prefix_digests[bottom.id] = digest
        bottom.is_baked = True

        engine.layers[2].grading_params["exposure"] = 5.0  # 只改上层
        engine._validate_baked_prefixes()
        assert bottom.is_baked is True


# ---------------------------------------------------------------------------
# G. 跟踪几何（蒙版跟踪绑定，CPU 侧视图）
# ---------------------------------------------------------------------------

class TestTrackingGeometry:
    def _shape(self) -> MaskShape:
        shape = _make_circle(0.4)
        return shape

    def test_no_provider_returns_empty_context(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.tracking_context_provider = None
        assert engine._tracking_frame_context() == ("", 0)

    def test_provider_failure_falls_back(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.tracking_context_provider = lambda: (_ for _ in ()).throw(ValueError("x"))
        assert engine._tracking_frame_context() == ("", 0)

    def test_non_numeric_frame_coerced_to_zero(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.tracking_context_provider = lambda: ("clip-1", "twelve")
        assert engine._tracking_frame_context() == ("clip-1", 0)

    def test_get_tracked_geometry_requires_image(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.tracking_context_provider = lambda: ("clip-1", 5)
        engine.img_full_original = None
        assert engine.get_tracked_geometry(self._shape()) is None

    def test_get_tracked_geometry_without_clip_returns_none(
        self, image_engine: ImageEngine
    ) -> None:
        engine = image_engine
        engine.tracking_context_provider = lambda: ("", 5)
        engine.img_full_original = np.zeros((10, 20, 3), dtype=np.uint8)
        assert engine.get_tracked_geometry(self._shape()) is None

    def test_get_tracked_geometry_evaluates_with_image_size(
        self, image_engine: ImageEngine
    ) -> None:
        engine = image_engine
        engine.tracking_context_provider = lambda: ("clip-1", 7)
        engine.img_full_original = np.zeros((10, 20, 3), dtype=np.uint8)
        engine.tracking_evaluator = MagicMock()
        engine.tracking_evaluator.evaluate_geometry.return_value = {
            "center": [0.7, 0.3], "size": [0.2, 0.2], "rotation": 15.0}
        shape = self._shape()

        geometry = engine.get_tracked_geometry(shape)
        assert geometry is not None and geometry["rotation"] == 15.0
        engine.tracking_evaluator.evaluate_geometry.assert_called_once_with(
            "clip-1", shape.id, 7, (20, 10))

    def test_tracked_shape_view_proxies_geometry(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.tracking_context_provider = lambda: ("clip-1", 7)
        engine.img_full_original = np.zeros((10, 20, 3), dtype=np.uint8)
        engine.tracking_evaluator = MagicMock()
        engine.tracking_evaluator.evaluate_geometry.return_value = {
            "center": [0.7, 0.3], "size": [0.2, 0.2], "rotation": 15.0}
        shape = self._shape()

        view = engine._tracked_shape_view(shape)
        assert view is not shape
        assert view.center == [0.7, 0.3]
        assert view.rotation == 15.0
        assert view._render_cache is None
        # 原形状不被污染
        assert shape.center == [0.5, 0.5]
        assert shape.rotation == 0.0

    def test_tracked_shape_view_without_geometry_returns_original(
        self, image_engine: ImageEngine
    ) -> None:
        engine = image_engine
        engine.tracking_context_provider = None
        shape = self._shape()
        assert engine._tracked_shape_view(shape) is shape


class TestLayerContainsShape:
    def test_flat_and_nested(self, image_engine: ImageEngine) -> None:
        layer = Layer("L")
        shape = layer.add_shape(MaskShape.CIRCLE)
        nested = MaskGroup("inner")
        nested_shape = _make_circle()
        nested.children = [nested_shape]
        outer = MaskGroup("outer")
        outer.children = [nested]
        layer.mask_shapes.append(outer)

        assert image_engine._layer_contains_shape(layer, shape.id) is True
        assert image_engine._layer_contains_shape(layer, nested_shape.id) is True
        assert image_engine._layer_contains_shape(layer, "missing") is False


class TestRefreshTrackingMasks:
    def _engine_with_binding(self, image_engine: ImageEngine, contains: bool
                             ) -> Tuple[ImageEngine, MagicMock, MagicMock]:
        engine = image_engine
        engine.img_full_original = np.zeros((10, 20, 3), dtype=np.uint8)
        engine.tracking_context_provider = lambda: ("clip-1", 5)
        store = MagicMock()
        store.is_empty.return_value = False
        binding = MagicMock()
        binding.frame_range_with_results.return_value = (0, 10)
        binding.contains_frame.return_value = True
        binding.shape_id = "shape-1"
        store.bindings_for_clip.return_value = [binding]
        engine.tracking_store = store
        engine.tracking_evaluator = MagicMock()
        return engine, store, binding

    def test_empty_store_returns_false(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.img_full_original = np.zeros((4, 4, 3), dtype=np.uint8)
        engine.tracking_store = MagicMock()
        engine.tracking_store.is_empty.return_value = True
        assert engine.refresh_tracking_masks() is False

    def test_no_image_returns_false(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.tracking_store = MagicMock()
        engine.tracking_store.is_empty.return_value = False
        engine.img_full_original = None
        assert engine.refresh_tracking_masks() is False

    def test_no_clip_context_returns_false(self, image_engine: ImageEngine) -> None:
        engine, _, _ = self._engine_with_binding(image_engine, True)
        engine.tracking_context_provider = lambda: ("", 5)
        assert engine.refresh_tracking_masks() is False

    def test_binding_without_results_skipped(self, image_engine: ImageEngine) -> None:
        engine, store, binding = self._engine_with_binding(image_engine, True)
        binding.frame_range_with_results.return_value = None
        assert engine.refresh_tracking_masks() is False
        binding.contains_frame.assert_not_called()

    def test_binding_outside_frame_range_skipped(self, image_engine: ImageEngine) -> None:
        engine, _, binding = self._engine_with_binding(image_engine, True)
        binding.contains_frame.return_value = False
        assert engine.refresh_tracking_masks() is False

    def test_matching_binding_marks_layer_dirty_and_updates(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine, store, binding = self._engine_with_binding(image_engine, True)
        shape = engine.layers[0].add_shape(MaskShape.CIRCLE)
        binding.shape_id = shape.id
        engine.layers[0].mask_dirty = False
        recorder = MagicMock()
        monkeypatch.setattr(engine, "_update_mask_textures", recorder)

        assert engine.refresh_tracking_masks(source_frame=5) is True
        assert engine.layers[0].mask_dirty is True
        recorder.assert_called_once()
        store.bindings_for_clip.assert_called_once_with("clip-1")

    def test_shape_on_other_layer_not_marked(self, image_engine: ImageEngine) -> None:
        engine, _, binding = self._engine_with_binding(image_engine, True)
        engine.add_layer()
        engine.layers[1].add_shape(MaskShape.CIRCLE)
        binding.shape_id = "shape-not-in-stack"
        for layer in engine.layers:
            layer.mask_dirty = False
        assert engine.refresh_tracking_masks() is False
        assert all(not layer.mask_dirty for layer in engine.layers)


# ---------------------------------------------------------------------------
# H. render_pipeline 入口语义（mock renderer，真实方法）
# ---------------------------------------------------------------------------

def _prep_render_engine(monkeypatch: pytest.MonkeyPatch, engine: ImageEngine) -> MagicMock:
    """给引擎配置 mock renderer 与最小图像状态，屏蔽蒙版上传。"""
    engine.renderer = MagicMock()
    engine.renderer.current_video_texture = None
    engine.renderer.current_video_planes = None
    engine.renderer.video_source_size = (0, 0)
    engine._is_video_mode = False
    engine.img_full_original = _small_image(4, 6)
    engine.img_high = np.zeros((4, 6, 3), dtype=np.float32)
    engine.visible_rect = (0.0, 0.0, 6.0, 4.0)
    engine.viewport_size = (2, 2)
    recorder = MagicMock()
    monkeypatch.setattr(engine, "_update_mask_textures", recorder)
    return recorder


class TestRenderPipelineReasonSemantics:
    def test_full_reason_updates_masks_and_marks_color_output(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        recorder = _prep_render_engine(monkeypatch, engine)
        engine._color_output.update(1, 2, 3)  # is_dirty -> False

        rid = ImageEngine.render_pipeline(engine, render_reason="full")

        recorder.assert_called_once()
        assert engine._color_output.is_dirty is True
        command, payload = engine.renderer.submit.call_args.args
        assert command == RenderCommand.RENDER_FRAME
        assert payload["render_reason"] == "full"
        assert rid == payload["render_request_id"]

    def test_grading_reason_skips_masks_but_marks_color_output(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        recorder = _prep_render_engine(monkeypatch, engine)
        engine._color_output.update(1, 2, 3)

        ImageEngine.render_pipeline(engine, render_reason="grading")

        recorder.assert_not_called()
        assert engine._color_output.is_dirty is True

    def test_viewport_refresh_skips_masks_and_dirty_marking(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        recorder = _prep_render_engine(monkeypatch, engine)
        engine._color_output.update(1, 2, 3)

        ImageEngine.render_pipeline(engine, render_reason="viewport_refresh")

        recorder.assert_not_called()
        assert engine._color_output.is_dirty is False
        command, _ = engine.renderer.submit.call_args.args
        assert command == RenderCommand.RENDER_FRAME

    def test_preview_render_submits_nothing(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        recorder = _prep_render_engine(monkeypatch, engine)
        result = ImageEngine.render_pipeline(engine, is_preview=True)
        recorder.assert_not_called()
        engine.renderer.submit.assert_not_called()
        assert result is None

    def test_canvas_draw_background_guard_blocks_submit(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        _prep_render_engine(monkeypatch, engine)
        canvas = MagicMock()
        canvas._in_draw_background = True
        engine.canvas_ref = canvas

        result = ImageEngine.render_pipeline(engine)

        assert result is None
        engine.renderer.submit.assert_not_called()

    def test_playback_lane_invalidation_skips_submit(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        _prep_render_engine(monkeypatch, engine)
        engine._is_video_mode = True
        engine.scene_size = (1920.0, 1080.0)
        canvas = MagicMock()
        canvas._in_draw_background = False
        controller = canvas.window.return_value.video_controller
        controller.is_playing = True
        engine.canvas_ref = canvas

        result = ImageEngine.render_pipeline(engine, render_reason="grading")

        assert result is None
        engine.renderer.submit.assert_not_called()
        controller.invalidate_cached_render_params.assert_called_once()

    def test_playback_stopped_still_submits(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        _prep_render_engine(monkeypatch, engine)
        engine._is_video_mode = True
        engine.scene_size = (1920.0, 1080.0)
        canvas = MagicMock()
        canvas._in_draw_background = False
        canvas.window.return_value.video_controller.is_playing = False
        engine.canvas_ref = canvas

        rid = ImageEngine.render_pipeline(engine, render_reason="grading")
        assert rid is not None
        engine.renderer.submit.assert_called_once()

    def test_visible_rect_derived_from_image_when_degenerate(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        _prep_render_engine(monkeypatch, engine)
        engine.visible_rect = (0.0, 0.0, 1.0, 1.0)  # w<=1 → 重推

        ImageEngine.render_pipeline(engine)

        _, payload = engine.renderer.submit.call_args.args
        assert payload["visible_rect"] == (0.0, 0.0, 6.0, 4.0)
        assert engine.visible_rect == (0.0, 0.0, 6.0, 4.0)

    def test_gpu_scale_when_preview_smaller_than_full(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        _prep_render_engine(monkeypatch, engine)
        engine.img_high = np.zeros((2, 3, 3), dtype=np.float32)  # 半尺寸代理

        ImageEngine.render_pipeline(engine)

        _, payload = engine.renderer.submit.call_args.args
        assert payload["visible_rect"] == (0.0, 0.0, 3.0, 2.0)  # 缩放到 GPU 纹理坐标
        assert payload["grain_visible_rect"] == (0.0, 0.0, 6.0, 4.0)  # grain 用原图坐标
        assert payload["scene_visible_rect"] == (0.0, 0.0, 6.0, 4.0)

    def test_raw_bypass_params_for_srgb_source(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        _prep_render_engine(monkeypatch, engine)

        ImageEngine.render_pipeline(engine)

        _, payload = engine.renderer.submit.call_args.args
        raw = payload["raw_preview"]
        assert raw["color_space"] == -1
        assert raw["gains"] == (1.0, 1.0, 1.0)
        assert raw["gamma"] == 0
        assert raw["is_srgb_source"] is False


class TestRenderPipelineFullResGuards:
    def test_duplicate_registration_returns_none(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        _prep_render_engine(monkeypatch, engine)
        engine.renderer.register_full_resolution.return_value = False

        result = ImageEngine.render_pipeline(engine, is_full_res=True)

        assert result is None
        engine.renderer.submit.assert_not_called()

    def test_submit_failure_forgets_registration(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        _prep_render_engine(monkeypatch, engine)
        engine.renderer.register_full_resolution.return_value = True
        engine.renderer.submit.side_effect = RuntimeError("GL context lost")

        with pytest.raises(RuntimeError):
            ImageEngine.render_pipeline(engine, is_full_res=True)

        registered_id = engine.renderer.register_full_resolution.call_args.args[0]
        engine.renderer.forget_full_resolution.assert_called_once_with(registered_id)

    def test_full_res_converts_source_to_rgb_float(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        _prep_render_engine(monkeypatch, engine)
        engine.renderer.register_full_resolution.return_value = True

        rid = ImageEngine.render_pipeline(engine, is_full_res=True)

        assert rid is not None
        command, payload = engine.renderer.submit.call_args.args
        assert command == RenderCommand.RENDER_FULL_RESOLUTION
        assert payload["color_output_target"] == "full_res"
        assert payload["viewport_size"] == (6, 4)
        assert payload["source_image"].dtype == np.float32
        assert payload["full_resolution_request_id"] == rid


# ---------------------------------------------------------------------------
# I. ColorOutput / scope 请求路径
# ---------------------------------------------------------------------------

class TestRequestColorOutput:
    def test_without_renderer_returns(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.img_full_original = _small_image()
        assert engine.renderer is None
        engine.request_color_output()  # 不应抛错

    def test_without_image_returns(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.img_full_original = None
        engine.request_color_output()
        engine.renderer.submit.assert_not_called()

    def test_payload_contract(self, image_engine: ImageEngine,
                              monkeypatch: pytest.MonkeyPatch) -> None:
        engine = image_engine
        _prep_render_engine(monkeypatch, engine)
        engine.layers[0].grading_params["exposure"] = 1.0

        engine.request_color_output(
            max_proxy_dim=512, update_masks=True, target="monitor",
            quality_mode="export")

        engine._update_mask_textures.assert_called_once()  # recorder 复用
        command, payload = engine.renderer.submit.call_args.args
        assert command == RenderCommand.RENDER_COLOR_OUTPUT
        assert payload["max_proxy_dim"] == 512
        assert payload["color_output_target"] == "monitor"
        assert payload["video_denoise_quality_mode"] == "export"
        assert len(payload["layers"]) == len(engine.layers)
        assert payload["layers"][0]["grading_params"]["exposure"] == 1.0
        assert payload["visible_rect"] == (0.0, 0.0, 6.0, 4.0)
        assert payload["raw_preview"]["gains"] == (1.0, 1.0, 1.0)
        assert "film_grain" in payload

    def test_update_masks_false_skips_mask_upload(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        _prep_render_engine(monkeypatch, engine)
        engine.request_color_output(update_masks=False)
        engine._update_mask_textures.assert_not_called()

    def test_default_quality_mode_omitted(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        _prep_render_engine(monkeypatch, engine)
        engine.request_color_output(quality_mode=None)
        _, payload = engine.renderer.submit.call_args.args
        assert "video_denoise_quality_mode" not in payload


class TestColorOutputSlots:
    def test_on_color_output_ready_updates_cache_and_emits(
        self, image_engine: ImageEngine
    ) -> None:
        engine = image_engine
        emitted = _spy(engine, "color_output_changed")
        engine._color_output.update(1, 1, 1)  # 建立 revision 基线
        revision = engine._color_output.revision

        engine._on_color_output_ready(42, 640, 480)

        assert engine.color_output.texture_id == 42
        assert engine.color_output.width == 640
        assert engine.color_output.height == 480
        assert engine.color_output.is_dirty is False
        assert engine._color_output.revision == revision + 1
        assert emitted == [(42, 640, 480)]

    def test_on_monitor_output_ready_emits_without_cache_update(
        self, image_engine: ImageEngine
    ) -> None:
        engine = image_engine
        emitted = _spy(engine, "monitor_color_output_changed")
        engine._on_monitor_color_output_ready(7, 8, 9)
        assert emitted == [(7, 8, 9)]
        assert engine.color_output.texture_id is None  # 不污染 Composite 缓存


class TestScopeRequests:
    def test_snapshot_disabled_by_default(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        assert engine.scope_snapshots_enabled is False
        engine.request_scope_snapshot(max_dim=256)
        engine.renderer.submit.assert_not_called()

    def test_snapshot_enabled_submits_with_clamped_dim(
        self, image_engine: ImageEngine
    ) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.scope_snapshots_enabled = True
        engine.request_scope_snapshot(max_dim=0)
        command, payload = engine.renderer.submit.call_args.args
        assert command == RenderCommand.RENDER_SCOPE
        assert payload == {"max_dim": 1}

    def test_gpu_scope_submits_demand(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        demand = MagicMock()
        engine.request_gpu_scope(demand)
        engine.renderer.submit.assert_called_once_with(
            RenderCommand.RENDER_GPU_SCOPE, demand)

    def test_gpu_scope_without_renderer_noop(self, image_engine: ImageEngine) -> None:
        image_engine.request_gpu_scope(MagicMock())  # 不应抛错

    def test_scope_source_revision(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        assert engine.scope_source_revision == 0
        engine.renderer = MagicMock()
        engine.renderer._scope_source_revision = 11
        assert engine.scope_source_revision == 11

    def test_discard_pending_gpu_scope(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.discard_pending_gpu_scope()
        engine.renderer.discard_pending_gpu_scope.assert_called_once_with()


# ---------------------------------------------------------------------------
# J. 渲染器初始化与线程生命周期
# ---------------------------------------------------------------------------

class TestInitializeAsyncRenderer:
    def test_creates_renderer_and_wires_signals(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        with patch("src.features.color.core.image_engine.AsyncRenderer") as renderer_cls:
            instance = MagicMock()
            renderer_cls.return_value = instance
            context = object()

            engine.initialize_async_renderer(context)

            renderer_cls.assert_called_once_with(context)
            assert engine.renderer is instance
            instance.start.assert_called_once()
            instance.frame_ready.connect.assert_called_once()
            instance.scope_snapshot_ready.connect.assert_called_once()
            instance.layer_baked_ready.connect.assert_called_once()
            instance.color_output_ready.connect.assert_called_once()

    def test_second_call_is_noop(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        with patch("src.features.color.core.image_engine.AsyncRenderer") as renderer_cls:
            instance = MagicMock()
            renderer_cls.return_value = instance
            engine.initialize_async_renderer(object())
            engine.initialize_async_renderer(object())
            assert renderer_cls.call_count == 1
            assert instance.start.call_count == 1


class TestShutdownWorkerThreads:
    def test_quit_waits_for_running_thread(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.shutdown_worker_threads()  # 先安静地停掉真实线程

        stopped = MagicMock()
        stopped.isRunning.return_value = True
        stopped.wait.return_value = True
        engine.ai_worker_thread = stopped
        missing: Optional[MagicMock] = None
        engine.raw_worker_thread = missing  # None → 跳过

        engine.shutdown_worker_threads()

        stopped.quit.assert_called_once()
        stopped.terminate.assert_not_called()

    def test_timeout_falls_back_to_terminate(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.shutdown_worker_threads()

        stuck = MagicMock()
        stuck.isRunning.return_value = True
        stuck.wait.side_effect = [False] + [True] * 10  # 首次超时，此后成功
        engine.raw_worker_thread = stuck
        quiet = MagicMock()
        quiet.isRunning.return_value = False
        engine.ai_worker_thread = quiet

        engine.shutdown_worker_threads()

        stuck.quit.assert_called_once()
        stuck.terminate.assert_called_once()
        quiet.quit.assert_not_called()

    def test_not_running_thread_not_touched(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.shutdown_worker_threads()
        idle = MagicMock()
        idle.isRunning.return_value = False
        engine.raw_worker_thread = idle
        engine.shutdown_worker_threads()
        idle.quit.assert_not_called()


# ---------------------------------------------------------------------------
# K. 快照 / 恢复路径（plan-01 审查优先）
# ---------------------------------------------------------------------------

class TestRestoreState:
    def _state_for(self, engine: ImageEngine, image: Optional[np.ndarray],
                   active_index: int = 0) -> Dict[str, Any]:
        return {
            "image": image,
            "layers": [
                layer.to_dict(assets_dir=None) for layer in engine.layers
            ],
            "active_layer_index": active_index,
        }

    def test_image_layers_and_index_restored(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.img_full_original = None
        state = self._state_for(engine, _small_image(8, 8), active_index=0)

        engine._restore_state(state)

        np.testing.assert_array_equal(engine.img_full_original, _small_image(8, 8))
        assert engine.active_layer_index == 0
        engine.render_pipeline.assert_called_once()

    def test_restored_layers_are_new_objects(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        original_layer = engine.layers[0]
        state = self._state_for(engine, _small_image())
        engine._restore_state(state)
        assert engine.layers[0] is not original_layer
        assert engine.layers[0].name == original_layer.name

    def test_image_is_copied_not_aliased(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        img = _small_image(4, 4)
        state = self._state_for(engine, img)
        engine._restore_state(state)
        img[:] = 0
        assert engine.img_full_original.max() > 0

    def test_none_image_keeps_current(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.img_full_original = _small_image(4, 4)
        state = self._state_for(engine, None)
        engine._restore_state(state)
        assert engine.img_full_original is not None

    def test_empty_layers_keeps_current(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        before = engine.layers
        state = {"image": _small_image(), "layers": [], "active_layer_index": 0}
        engine._restore_state(state)
        assert engine.layers is before

    def test_active_index_clamped_to_layers(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        state = self._state_for(engine, _small_image(), active_index=99)
        engine._restore_state(state)
        assert engine.active_layer_index == 0  # 单层栈 → 0

    def test_active_index_default_zero(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        state = {"image": _small_image(), "layers": [
            layer.to_dict(assets_dir=None) for layer in engine.layers]}
        engine._restore_state(state)
        assert engine.active_layer_index == 0

    def test_grade_keyframes_reevaluated_at_current_frame(
        self, image_engine: ImageEngine
    ) -> None:
        """undo 恢复的是压栈时刻的参数；带轨道时必须按当前帧重新求值。"""
        engine = image_engine
        assert engine.record_grade_keyframe(0, "exposure", 0.0) is True
        assert engine.record_grade_keyframe(10, "exposure", 4.0) is True
        state = self._state_for(engine, _small_image())
        engine.layers[0].grading_params["exposure"] = 1.0
        engine.current_grade_frame = 10

        engine._restore_state(state)

        assert engine.layers[0].grading_params["exposure"] == pytest.approx(4.0)

    def test_no_keyframes_no_reevaluation(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        state = self._state_for(engine, _small_image())
        engine.current_grade_frame = 10
        engine._restore_state(state)
        assert engine.layers[0].grading_params["exposure"] == 0


class TestUndoRestoreGradingParams:
    # 曾以 xfail 锁定 BUG-A2-03（Layer.to_dict 按引用序列化）；history 快照
    # 现已 deepcopy（AUD-0501 修复），翻正断言。
    def test_undo_restores_pushed_grading_params(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.img_full_original = _small_image(4, 4)
        engine.layers[0].grading_params["exposure"] = 1.5
        engine.history.push_state(engine.img_full_original, engine.layers, 0)

        engine.layers[0].grading_params["exposure"] = 9.0
        assert engine.undo() is True

        assert engine.layers[0].grading_params["exposure"] == 1.5


class TestUndoRestoreFlipState:
    # 曾以 xfail 锁定 BUG-A2-01（history state 携带 flip 字段但
    # _restore_state 未消费）；现已恢复（undo/redo 对称交换也记录翻转），
    # 翻正断言。
    def test_undo_restores_flip_flags(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.img_source_full = _small_image(4, 4)
        engine.img_full_original = engine.img_source_full.copy()
        with patch("src.shared.io.file_io.FileIO._resize_to_fit",
                   side_effect=lambda img, size: img):
            engine.flip_image(horizontal=True)
        assert engine._flip_h is True
        engine.undo()
        assert engine._flip_h is False

    def test_redo_restores_flip_flags(self, image_engine: ImageEngine) -> None:
        """undo 后 redo：对称交换记录的翻转态必须完整回来。"""
        engine = image_engine
        engine.img_source_full = _small_image(4, 4)
        engine.img_full_original = engine.img_source_full.copy()
        with patch("src.shared.io.file_io.FileIO._resize_to_fit",
                   side_effect=lambda img, size: img):
            engine.flip_image(horizontal=True)
            engine.flip_image(horizontal=False)  # vertical
        assert engine._flip_h is True and engine._flip_v is True
        assert engine.undo() is True
        assert engine._flip_v is False and engine._flip_h is True
        assert engine.redo() is True
        assert engine._flip_h is True and engine._flip_v is True

    def test_flip_get_flip_state_after_undo(self, image_engine: ImageEngine) -> None:
        """撤销翻转后项目序列化读到的翻转态必须回到撤销前。"""
        engine = image_engine
        engine.img_source_full = _small_image(4, 4)
        engine.img_full_original = engine.img_source_full.copy()
        with patch("src.shared.io.file_io.FileIO._resize_to_fit",
                   side_effect=lambda img, size: img):
            engine.flip_image(horizontal=True)
        assert engine.get_flip_state() == {"flip_h": True, "flip_v": False}
        engine.undo()
        assert engine.get_flip_state() == {"flip_h": False, "flip_v": False}


class TestUndoRestorePreviewDtype:
    # 曾以 xfail 锁定 BUG-A2-02（uint8 直写破坏 float32[0,1] 不变量）；
    # _restore_state 现已归一化（AUD-0600 修复），翻正断言。
    def test_restored_previews_are_float32(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.img_full_original = _small_image(8, 8)
        engine.history.push_state(engine.img_full_original, engine.layers, 0)
        engine.img_full_original = _small_image(4, 4)
        assert engine.undo() is True
        assert engine.img_high.dtype == np.float32
        assert engine.img_high.max() <= 1.0


class TestApplyChanges:
    def test_result_baked_into_new_image_state(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        result = np.full((4, 4, 3), 200, dtype=np.uint8)
        monkeypatch.setattr(engine, "render_full_resolution", lambda: result)
        engine.set_image = MagicMock()
        engine.img_full_original = _small_image(4, 4)

        with patch("src.shared.io.file_io.FileIO._resize_to_fit",
                   side_effect=lambda img, size: img):
            engine.apply_changes()

        assert engine.can_undo() is True
        engine.set_image.assert_called_once()
        assert engine.set_image.call_args.args[0] is result

    def test_float_result_converted_to_uint8(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        result = np.full((4, 4, 3), 0.5, dtype=np.float32)
        monkeypatch.setattr(engine, "render_full_resolution", lambda: result)
        engine.set_image = MagicMock()
        engine.img_full_original = _small_image(4, 4)

        with patch("src.shared.io.file_io.FileIO._resize_to_fit",
                   side_effect=lambda img, size: img):
            engine.apply_changes()

        passed = engine.set_image.call_args.args[0]
        assert passed.dtype == np.uint8
        assert passed[0, 0, 0] == 127

    def test_failed_render_pushes_nothing(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        monkeypatch.setattr(engine, "render_full_resolution", lambda: None)
        engine.set_image = MagicMock()
        engine.img_full_original = _small_image(4, 4)

        engine.apply_changes()

        assert engine.can_undo() is False
        engine.set_image.assert_not_called()


# ---------------------------------------------------------------------------
# L. 调色关键帧（P1-COLOR-03）
# ---------------------------------------------------------------------------

class TestGradeKeyframes:
    def test_record_rejects_unknown_param(self, image_engine: ImageEngine) -> None:
        assert image_engine.record_grade_keyframe(0, "bogus_param", 1.0) is False

    def test_record_creates_and_overwrites(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        assert engine.record_grade_keyframe(0, "exposure", 1.0) is True
        assert engine.record_grade_keyframe(10, "exposure", 3.0) is True
        track = engine.layers[0].grade_keyframes["exposure"]
        assert [kf.frame for kf in track.keyframes] == [0, 10]

        assert engine.record_grade_keyframe(10, "exposure", 4.5) is True
        assert len(track.keyframes) == 2  # 覆盖而非追加
        assert track.value_at(10, 0.0) == 4.5

    def test_has_grade_keyframes(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        assert engine.has_grade_keyframes() is False
        engine.add_layer()
        engine.record_grade_keyframe(5, "contrast", 0.5)
        assert engine.has_grade_keyframes() is True

    def test_set_frame_none_records_without_render(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.set_grade_frame(None)
        assert engine.current_grade_frame is None
        engine.render_pipeline.assert_not_called()

    def test_set_frame_without_tracks_no_render(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.set_grade_frame(7)
        assert engine.current_grade_frame == 7
        engine.render_pipeline.assert_not_called()

    def test_set_frame_with_tracks_evaluates_and_renders(
        self, image_engine: ImageEngine
    ) -> None:
        engine = image_engine
        engine.record_grade_keyframe(0, "exposure", 0.0)
        engine.record_grade_keyframe(10, "exposure", 4.0)
        engine.render_pipeline.reset_mock()

        engine.set_grade_frame(5)

        assert engine.layers[0].grading_params["exposure"] == pytest.approx(2.0)
        engine.render_pipeline.assert_called_once()

    def test_apply_grade_keyframes_render_false(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.record_grade_keyframe(0, "exposure", 0.0)
        engine.record_grade_keyframe(10, "exposure", 4.0)
        engine.render_pipeline.reset_mock()

        changed = engine.apply_grade_keyframes(10, render=False)

        assert changed is True
        assert engine.layers[0].is_dirty is True
        assert engine.layers[0].grading_params["exposure"] == pytest.approx(4.0)
        engine.render_pipeline.assert_not_called()

    def test_apply_grade_keyframes_layer_without_tracks_untouched(
        self, image_engine: ImageEngine
    ) -> None:
        engine = image_engine
        engine.add_layer()
        engine.set_active_layer(0)  # add_layer 会把活动层切到新层
        engine.record_grade_keyframe(0, "contrast", 1.0)  # 只在 layer0
        params_before = dict(engine.layers[1].grading_params)
        engine.apply_grade_keyframes(3)
        assert engine.layers[1].grading_params == params_before

    def test_remove_grade_keyframe(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.record_grade_keyframe(0, "exposure", 0.0)
        engine.record_grade_keyframe(10, "contrast", 0.5)

        assert engine.remove_grade_keyframe(10) is True
        assert "contrast" not in engine.layers[0].grade_keyframes  # 空轨道被清
        assert "exposure" in engine.layers[0].grade_keyframes
        assert engine.remove_grade_keyframe(99) is False

    def test_remove_at_current_frame_reevaluates(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.record_grade_keyframe(0, "exposure", 0.0)
        engine.record_grade_keyframe(10, "exposure", 4.0)
        engine.current_grade_frame = 10
        engine.set_grade_frame(10)
        engine.render_pipeline.reset_mock()

        assert engine.remove_grade_keyframe(10) is True
        # 唯一关键帧被删 → 求值回落到轨道外的当前值（保持 4.0 不被改写）
        engine.render_pipeline.assert_called()

    def test_active_grade_keyframe_frames_union(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.record_grade_keyframe(0, "exposure", 0.0)
        engine.record_grade_keyframe(12, "contrast", 1.0)
        engine.record_grade_keyframe(6, "temp", 0.3)
        assert engine.active_grade_keyframe_frames() == [0, 6, 12]

    def test_active_grade_keyframe_frames_empty(self, image_engine: ImageEngine) -> None:
        assert image_engine.active_grade_keyframe_frames() == []


# ---------------------------------------------------------------------------
# M. AI 掩码 / SAM 会话状态机
# ---------------------------------------------------------------------------

class TestOnAiMaskReady:
    def test_without_layer_is_noop(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.active_layer_index = 5  # 越界 → get_active_layer None
        finished = _spy(engine, "ai_process_finished")
        engine.on_ai_mask_ready(np.zeros((4, 4), np.float32))
        assert finished == []

    def test_continuous_session_updates_existing_shape(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        layer = engine.layers[0]
        shape = layer.add_shape(MaskShape.BITMAP)
        engine._active_sam_shape_id = shape.id
        engine.render_pipeline.reset_mock()
        shapes_changed = _spy(engine, "mask_shapes_changed")
        finished = _spy(engine, "ai_process_finished")
        matte = np.ones((8, 8), np.float32) * 0.5

        engine.on_ai_mask_ready(matte)

        assert shape.bitmap_data is matte
        assert layer.mask_dirty is True
        assert layer.is_dirty is True
        assert len(layer.mask_shapes) == 1  # 不新建形状
        assert shapes_changed == []  # 更新会话不发 shapes_changed
        assert finished == [()]
        engine.render_pipeline.assert_called_once()

    def test_one_shot_creates_bitmap_shape(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        layer = engine.layers[0]
        engine._active_sam_shape_id = None
        assert not hasattr(engine, "_sam_points")
        shapes_changed = _spy(engine, "mask_shapes_changed")
        finished = _spy(engine, "ai_process_finished")
        matte = np.ones((8, 8), np.float32)

        engine.on_ai_mask_ready(matte)

        assert len(layer.mask_shapes) == 1
        shape = layer.mask_shapes[0]
        assert shape.type == MaskShape.BITMAP
        assert shape.name == "AI Mask"
        assert shape.feather == 0
        assert shape.bitmap_data is matte
        assert engine._active_sam_shape_id is None  # 非 SAM 会话不跟踪
        assert len(shapes_changed) == 1
        assert finished == [()]

    def test_sam_session_tracks_new_shape(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine._sam_points = [(0.1, 0.1)]  # 有点 → SAM 连续会话
        engine._sam_labels = [1]
        engine.on_ai_mask_ready(np.zeros((4, 4), np.float32))
        assert engine._active_sam_shape_id == engine.layers[0].mask_shapes[0].id


class TestSamPointManagement:
    def _wire_decode_signal(self, monkeypatch: pytest.MonkeyPatch,
                            engine: ImageEngine) -> MagicMock:
        emit_mock = MagicMock()
        monkeypatch.setattr(engine, "sig_process_sam_decode", emit_mock)
        return emit_mock

    def test_request_sam_decode_accumulates_points(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        emit_mock = self._wire_decode_signal(monkeypatch, engine)

        engine.request_sam_decode(0.1, 0.2, 1)
        engine.request_sam_decode(0.3, 0.4, 0)

        assert engine._sam_points == [(0.1, 0.2), (0.3, 0.4)]
        assert engine._sam_labels == [1, 0]
        # 每次加点都携带全部点重发 decode（连续会话刷新）
        assert emit_mock.emit.call_count == 2
        points_arg, labels_arg = emit_mock.emit.call_args.args
        assert points_arg == [(0.1, 0.2), (0.3, 0.4)]
        assert labels_arg == [1, 0]

    def test_reset_sam_points_clears_tracking(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine._sam_points = [(0.5, 0.5)]
        engine._sam_labels = [1]
        engine._active_sam_shape_id = "shape-x"
        engine.reset_sam_points()
        assert engine._sam_points == []
        assert engine._sam_labels == []
        assert engine._active_sam_shape_id is None

    def test_undo_sam_point_empty_returns_false(self, image_engine: ImageEngine) -> None:
        assert image_engine.undo_sam_point() is False

    def test_undo_sam_point_pops_and_reemits(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        emit_mock = self._wire_decode_signal(monkeypatch, engine)
        engine._sam_points = [(0.1, 0.1), (0.2, 0.2)]
        engine._sam_labels = [1, 1]

        assert engine.undo_sam_point() is True

        assert engine._sam_points == [(0.1, 0.1)]
        emit_mock.emit.assert_called_once()
        points_arg, _ = emit_mock.emit.call_args.args
        assert points_arg == [(0.1, 0.1)]

    def test_undo_last_point_emits_nothing(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        emit_mock = self._wire_decode_signal(monkeypatch, engine)
        engine._sam_points = [(0.1, 0.1)]
        engine._sam_labels = [1]

        assert engine.undo_sam_point() is True
        emit_mock.emit.assert_not_called()
        assert engine._sam_points == []

    def test_remove_sam_point_invalid_index(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine._sam_points = [(0.1, 0.1)]
        engine._sam_labels = [1]
        assert engine.remove_sam_point(-1) is False
        assert engine.remove_sam_point(5) is False

    def test_remove_sam_point_without_points(self, image_engine: ImageEngine) -> None:
        assert image_engine.remove_sam_point(0) is False

    def test_remove_last_point_drops_tracked_shape(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        emit_mock = self._wire_decode_signal(monkeypatch, engine)
        layer = engine.layers[0]
        shape = layer.add_shape(MaskShape.BITMAP)
        engine._sam_points = [(0.1, 0.1)]
        engine._sam_labels = [1]
        engine._active_sam_shape_id = shape.id
        shapes_changed = _spy(engine, "mask_shapes_changed")

        assert engine.remove_sam_point(0) is True

        assert shape not in layer.mask_shapes
        assert engine._active_sam_shape_id is None
        assert engine._sam_points == []
        emit_mock.emit.assert_not_called()
        assert len(shapes_changed) == 1
        assert layer.mask_dirty is True

    def test_remove_middle_point_reemits(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        emit_mock = self._wire_decode_signal(monkeypatch, engine)
        engine._sam_points = [(0.1, 0.1), (0.5, 0.5), (0.9, 0.9)]
        engine._sam_labels = [1, 0, 1]

        assert engine.remove_sam_point(1) is True

        assert engine._sam_points == [(0.1, 0.1), (0.9, 0.9)]
        emit_mock.emit.assert_called_once()


class TestPrepareImageForAi:
    def test_none_passthrough(self, image_engine: ImageEngine) -> None:
        assert image_engine._prepare_image_for_ai(None) is None

    def test_uint8_bgr_to_rgb(self, image_engine: ImageEngine) -> None:
        bgr = np.zeros((2, 2, 3), dtype=np.uint8)
        bgr[..., 0] = 255  # B 通道
        out = image_engine._prepare_image_for_ai(bgr)
        assert out.dtype == np.uint8
        assert out[..., 2].max() == 255  # B → R
        assert out.flags["C_CONTIGUOUS"]

    def test_uint16_divides_by_256(self, image_engine: ImageEngine) -> None:
        img = np.full((2, 2, 3), 512, dtype=np.uint16)
        out = image_engine._prepare_image_for_ai(img)
        assert out.dtype == np.uint8
        assert out[0, 0, 0] == 2

    def test_float_clips_to_uint8(self, image_engine: ImageEngine) -> None:
        img = np.array([[[2.0, 0.5, 0.0]]], dtype=np.float32)  # BGR
        out = image_engine._prepare_image_for_ai(img)
        assert out.dtype == np.uint8
        assert out[0, 0].tolist() == [0, 127, 255]  # BGR→RGB 后裁剪


class TestAiPluginHelpers:
    def test_get_layer_ai_texture_without_renderer(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.layers[0].plugins = [{
            "instance_id": "i1", "plugin_id": "p", "type": "ai", "enabled": True}]
        assert engine._get_layer_ai_texture(engine.layers[0]) is None

    def test_get_layer_ai_texture_prefers_last_enabled(
        self, image_engine: ImageEngine
    ) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        layer = engine.layers[0]
        layer.plugins = [
            {"instance_id": "i1", "plugin_id": "p", "type": "ai", "enabled": True},
            {"instance_id": "i2", "plugin_id": "p", "type": "ai", "enabled": True},
            {"instance_id": "i3", "plugin_id": "p", "type": "ai", "enabled": False},
        ]
        expected = object()
        engine.renderer.ai_plugin_textures.get.return_value = expected

        assert engine._get_layer_ai_texture(layer) is expected
        engine.renderer.ai_plugin_textures.get.assert_called_once_with(
            (layer.id, "i2"))

    def test_get_layer_ai_texture_no_ai_plugins(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        layer = engine.layers[0]
        layer.plugins = [
            {"instance_id": "d1", "plugin_id": "p", "type": "dctl", "enabled": True}]
        assert engine._get_layer_ai_texture(layer) is None

    def test_remove_plugin_invalid_index_noop(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.remove_plugin(-1, "i1")
        engine.renderer.submit.assert_not_called()

    def test_remove_plugin_cleans_all_resources(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        layer = engine.layers[0]
        layer.plugins.append({
            "instance_id": "i1", "plugin_id": "p", "type": "ai", "enabled": True})
        engine.ai_plugin_result_cache[(layer.id, "i1")] = np.zeros((2, 2, 3))

        engine.remove_plugin(0, "i1")

        assert layer.plugins == []
        assert (layer.id, "i1") not in engine.ai_plugin_result_cache
        assert layer.is_dirty is True
        removes = _submit_calls(engine, RenderCommand.REMOVE_AI_PLUGIN_TEXTURE)
        assert removes == [{"layer_id": layer.id, "instance_id": "i1"}]
        engine.render_pipeline.assert_called_once()

    def test_process_ai_plugin_requires_image(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.ai_plugin_engine = MagicMock()
        engine.img_full_original = None
        layer = engine.layers[0]
        layer.plugins.append({
            "instance_id": "i1", "plugin_id": "p", "type": "ai", "enabled": True,
            "params": {}})
        engine.process_ai_plugin(0, "i1")  # 无图 → 早退，不触发推理
        engine.ai_plugin_engine.process_plugin.assert_not_called()

    def test_on_ai_plugin_error_forwards_signal(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        errors = _spy(engine, "error_occurred")
        engine._on_ai_plugin_error("layer-id", "i1", "model load failed")
        assert errors == [("AI Plugin Error: model load failed",)]


class TestBirefnetSamEntry:
    def test_birefnet_without_image_no_emit(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        emit_mock = MagicMock()
        monkeypatch.setattr(engine, "sig_process_birefnet", emit_mock)
        engine.request_birefnet()
        emit_mock.emit.assert_not_called()

    def test_birefnet_emits_prepared_rgb(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        emit_mock = MagicMock()
        monkeypatch.setattr(engine, "sig_process_birefnet", emit_mock)
        img = np.full((2, 2, 3), 128, dtype=np.uint8)
        engine.img_full_original = img

        engine.request_birefnet()

        emit_mock.emit.assert_called_once()
        sent = emit_mock.emit.call_args.args[0]
        np.testing.assert_array_equal(sent, np.full((2, 2, 3), 128, dtype=np.uint8))

    def test_sam_encode_without_image_no_emit(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        emit_mock = MagicMock()
        monkeypatch.setattr(engine, "sig_process_sam_encode", emit_mock)
        engine.request_sam_encode()
        emit_mock.emit.assert_not_called()


# ---------------------------------------------------------------------------
# N. 插件/资源同步与 LUT 反编译
# ---------------------------------------------------------------------------

class TestSyncAllResources:
    def test_without_renderer_noop(self, image_engine: ImageEngine) -> None:
        image_engine.sync_all_resources()  # 不应抛错

    def test_submits_curves_lut_and_warper(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        layer = engine.layers[0]
        layer.curves_lut = np.ones((256, 3), dtype=np.float64)  # 非 float32 → 转换
        layer.active_lut_path = "/tmp/lut.cube"
        layer.warper_params = np.ones((4, 5, 2), dtype=np.float32)

        engine.sync_all_resources()

        aux = _submit_calls(engine, RenderCommand.UPDATE_AUX_TEX)
        by_type = {a["type"]: a for a in aux}
        assert set(by_type) == {"curves", "lut3d", "warper"}
        assert by_type["curves"]["layer_id"] == layer.id
        assert len(by_type["curves"]["data"]) == 256 * 3 * 4
        assert by_type["lut3d"]["path"] == "/tmp/lut.cube"
        assert by_type["warper"]["size"] == (5, 4)  # (h,w,c) → (w,h)
        assert len(by_type["warper"]["data"]) == 4 * 5 * 2 * 4

    def test_submits_hsl_curve_lut_on_reload(self, image_engine: ImageEngine) -> None:
        """工程重载后 HSL 曲线纹理必须随 sync_all_resources 恢复。

        回归：此前 sync 只上传 curves/lut3d/warper，漏 hsl_curve——
        shader 编入 apply_hsl_curve 分支却绑定恒等默认纹理，保存的
        HSL 曲线整体 no-op。
        """
        engine = image_engine
        engine.renderer = MagicMock()
        layer = engine.layers[0]
        layer.hsl_curve_lut = np.ones((6, 256), dtype=np.float64)  # 非 f32 → 转换

        engine.sync_all_resources()

        aux = _submit_calls(engine, RenderCommand.UPDATE_AUX_TEX)
        hsl = [a for a in aux if a["type"] == "hsl_curve"]
        assert len(hsl) == 1
        assert hsl[0]["layer_id"] == layer.id
        assert len(hsl[0]["data"]) == 6 * 256 * 4

    def test_set_warper_params_none_skips_submit(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.set_warper_params(None)
        aux = _submit_calls(engine, RenderCommand.UPDATE_AUX_TEX)
        assert aux == []
        engine.render_pipeline.assert_called_once()


class TestDecompileLut:
    def _results(self) -> Dict[str, Any]:
        return {
            "grading": {"exposure": 2.0, "contrast": 0.4},
            "curves": np.ones((256, 3), dtype=np.float32),
            "warper": np.zeros((4, 4, 2), dtype=np.float32),
        }

    def test_success_applies_and_disables_lut(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.layers[0].active_lut_path = "/tmp/old.cube"
        results = self._results()

        with patch("src.features.color.core.the_decompiler.Decompiler") as decompiler_cls:
            decompiler_cls.return_value.decompile.return_value = results
            returned = engine.decompile_lut("/tmp/new.cube")

        assert returned is results
        layer = engine.layers[0]
        assert layer.grading_params["exposure"] == 2.0
        assert layer.grading_params["contrast"] == 0.4
        assert layer.curves_lut is results["curves"]
        assert layer.warper_params is results["warper"]
        assert layer.active_lut_path is None  # 反编译后禁用 3D LUT
        assert _submit_calls(engine, RenderCommand.UPDATE_AUX_TEX)
        engine.render_pipeline.assert_called_once()

    def test_failure_returns_none_and_keeps_state(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.layers[0].active_lut_path = "/tmp/old.cube"

        with patch("src.features.color.core.the_decompiler.Decompiler") as decompiler_cls:
            decompiler_cls.return_value.decompile.side_effect = RuntimeError("bad cube")
            returned = engine.decompile_lut("/tmp/broken.cube")

        assert returned is None
        assert engine.layers[0].active_lut_path == "/tmp/old.cube"
        engine.render_pipeline.assert_not_called()

    def test_without_active_layer_returns_none(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.active_layer_index = 9
        assert engine.decompile_lut("/tmp/whatever.cube") is None


# ---------------------------------------------------------------------------
# O. 图层删除与磁盘清理
# ---------------------------------------------------------------------------

class TestRemoveLayerDiskCleanup:
    def test_baked_file_deleted_and_layer_resource_released(
        self, image_engine: ImageEngine
    ) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.add_layer()
        victim = engine.layers[0]
        victim.baked_file_name = "baked_dead.exr"

        with patch("src.shared.io.file_io.FileIO.delete_baked_layer") as delete_mock:
            engine.remove_layer(0)

        delete_mock.assert_called_once_with("baked_dead.exr")
        removes = _submit_calls(engine, RenderCommand.REMOVE_LAYER)
        assert removes == [victim.id]

    def test_delete_failure_is_swallowed(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.add_layer()
        engine.layers[0].baked_file_name = "locked.exr"

        with patch("src.shared.io.file_io.FileIO.delete_baked_layer",
                   side_effect=OSError("file in use")):
            engine.remove_layer(0)  # 不应抛错

        assert len(engine.layers) == 1


# ---------------------------------------------------------------------------
# P. 全分辨率请求簿记
# ---------------------------------------------------------------------------

class TestFullResolutionRequestBookkeeping:
    def test_without_image_returns_none(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.img_full_original = None
        engine.renderer = MagicMock()
        assert engine.request_full_resolution() is None

    def test_reentrant_request_rejected(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.img_full_original = _small_image(2, 2)
        engine._full_res_render_active = True
        assert engine.request_full_resolution() is None

    def test_without_renderer_returns_none(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.img_full_original = _small_image(2, 2)
        assert engine.renderer is None
        assert engine.request_full_resolution() is None
        assert engine._full_res_render_active is False

    def test_render_failure_clears_flags_and_reraises(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.img_full_original = _small_image(2, 2)
        engine.render_pipeline = MagicMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError):
            engine.request_full_resolution()

        assert engine._full_res_render_active is False
        assert engine._full_res_pending_request_id is None

    def test_falsy_submission_clears_flags(self, image_engine: ImageEngine,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
        engine = image_engine
        engine.img_full_original = _small_image(2, 2)
        engine.render_pipeline = MagicMock(return_value=None)
        assert engine.request_full_resolution() is None
        assert engine._full_res_render_active is False

    def test_custom_pipeline_id_is_authoritative(
        self, image_engine: ImageEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.img_full_original = _small_image(2, 2)
        engine.render_pipeline = MagicMock(return_value="render-00009999")

        returned = engine.request_full_resolution()

        assert returned == "render-00009999"
        assert engine._full_res_pending_request_id == "render-00009999"

    def test_cancel_requires_renderer_and_id(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        assert engine.cancel_full_resolution() is False
        engine.renderer = MagicMock()
        assert engine.cancel_full_resolution("") is False

    def test_cancel_forwards_to_renderer(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.renderer.cancel_full_resolution.return_value = True
        engine._full_res_pending_request_id = "render-00000042"
        assert engine.cancel_full_resolution() is True
        engine.renderer.cancel_full_resolution.assert_called_once_with(
            "render-00000042")

    def test_clear_state_ignores_other_request_ids(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine._full_res_render_active = True
        engine._full_res_pending_request_id = "render-00000001"

        engine._clear_full_resolution_request_state("render-00000002")

        assert engine._full_res_render_active is True
        assert engine._full_res_pending_request_id == "render-00000001"

    def test_ready_signal_forwarded_with_matching_clear(
        self, image_engine: ImageEngine
    ) -> None:
        engine = image_engine
        engine._full_res_render_active = True
        engine._full_res_pending_request_id = "render-00000001"
        emitted = _spy(engine, "full_resolution_ready")
        img = np.zeros((2, 2, 3), np.uint8)

        engine._on_full_resolution_ready("render-00000001", img)

        assert emitted == [("render-00000001", img)]
        assert engine._full_res_render_active is False
        assert engine._full_res_pending_request_id is None

    def test_failed_signal_forwarded(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine._full_res_render_active = True
        engine._full_res_pending_request_id = "render-00000001"
        emitted = _spy(engine, "full_resolution_failed")

        engine._on_full_resolution_failed("render-00000001", "cancelled")

        assert emitted == [("render-00000001", "cancelled")]
        assert engine._full_res_render_active is False

    def test_progress_signal_forwarded(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        emitted = _spy(engine, "full_resolution_progress")
        engine._on_full_resolution_progress("render-00000001", 50, "rendering")
        assert emitted == [("render-00000001", 50, "rendering")]


# ---------------------------------------------------------------------------
# Q. 杂项状态读子
# ---------------------------------------------------------------------------

class TestMiscStateGetters:
    def test_get_current_image_prefers_temp(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        temp = np.zeros((2, 2, 3), np.uint8)
        engine.temp_image = temp
        engine.img_full_original = np.ones((4, 4, 3), np.uint8)
        assert engine.get_current_image() is temp

    def test_get_current_image_falls_back_to_full(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.temp_image = None
        full = np.ones((4, 4, 3), np.uint8)
        engine.img_full_original = full
        assert engine.get_current_image() is full

    def test_get_video_denoise_state_none_when_inactive(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.video_denoise_params = None
        assert engine.get_video_denoise_state() is None
        inactive = MagicMock()
        inactive.is_active.return_value = False
        engine.video_denoise_params = inactive
        assert engine.get_video_denoise_state() is None

    def test_get_video_denoise_state_serializes_when_active(
        self, image_engine: ImageEngine
    ) -> None:
        engine = image_engine
        active = MagicMock()
        active.is_active.return_value = True
        active.to_dict.return_value = {"temporal": 0.4}
        engine.video_denoise_params = active
        assert engine.get_video_denoise_state() == {"temporal": 0.4}

    def test_set_video_denoise_state_none(self, image_engine: ImageEngine) -> None:
        image_engine.set_video_denoise_state(None)
        assert image_engine.video_denoise_params is None

    def test_set_video_denoise_state_pushes_to_renderer(
        self, image_engine: ImageEngine
    ) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        params = MagicMock()

        with patch("src.shared.video.denoise.params.VideoDenoiseParams") as vdp:
            vdp.from_dict.return_value = params
            engine.set_video_denoise_state({"temporal": 0.2})

        vdp.from_dict.assert_called_once_with({"temporal": 0.2})
        assert engine.video_denoise_params is params
        engine.renderer.set_video_denoise_params.assert_called_once_with(params)

    def test_set_video_denoise_state_without_renderer_ok(
        self, image_engine: ImageEngine
    ) -> None:
        engine = image_engine
        with patch("src.shared.video.denoise.params.VideoDenoiseParams") as vdp:
            vdp.from_dict.return_value = MagicMock()
            engine.set_video_denoise_state({"temporal": 0.2})  # 不应抛错

    def test_flip_state_round_trip(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        source = _small_image(4, 4)
        engine.img_source_full = source
        engine.img_full_original = source.copy()
        engine.set_image = MagicMock()

        with patch("src.shared.io.file_io.FileIO._resize_to_fit",
                   side_effect=lambda img, size: img):
            engine.set_flip_state({"flip_h": True, "flip_v": False})

        assert engine.get_flip_state() == {"flip_h": True, "flip_v": False}
        np.testing.assert_array_equal(engine.img_full_original,
                                      np.flip(source, axis=1))
        assert engine.set_image.call_count == 1

    def test_reset_flip_restores_source(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        source = _small_image(4, 4)
        engine.img_source_full = source
        engine.img_full_original = np.flip(source, axis=0)
        engine._flip_v = True
        engine.set_image = MagicMock()

        with patch("src.shared.io.file_io.FileIO._resize_to_fit",
                   side_effect=lambda img, size: img):
            engine.reset_flip()

        np.testing.assert_array_equal(engine.img_full_original, source)
        assert engine.get_flip_state() == {"flip_h": False, "flip_v": False}

    def test_set_flip_state_none_is_noop(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.img_source_full = _small_image(4, 4)
        engine._flip_h = True
        engine.set_flip_state(None)
        assert engine._flip_h is True

    def test_apply_flip_state_without_source_is_noop(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.img_source_full = None
        engine._apply_flip_state()  # 不应抛错
