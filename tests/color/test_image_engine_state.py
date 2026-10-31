"""Tests for ImageEngine state management methods."""

import pytest
import numpy as np
from unittest.mock import MagicMock


class TestImageEngineInitialState:
    def test_has_one_default_layer(self, image_engine):
        assert len(image_engine.layers) == 1
        assert image_engine.layers[0].name == "Layer 1"

    def test_active_layer_index_zero(self, image_engine):
        assert image_engine.active_layer_index == 0

    def test_no_image_initially(self, image_engine):
        assert image_engine.img_full_original is None

    def test_renderer_is_none(self, image_engine):
        assert image_engine.renderer is None

    def test_not_interacting(self, image_engine):
        assert image_engine.is_interacting is False

    def test_split_view_off(self, image_engine):
        assert image_engine.split_view_active is False

    def test_split_position_half(self, image_engine):
        assert image_engine.split_position == 0.5

    def test_mask_preview_off(self, image_engine):
        assert image_engine.show_mask_mode is False

    def test_not_offline(self, image_engine):
        assert image_engine.is_offline_mode is False

    def test_can_undo_false(self, image_engine):
        assert image_engine.can_undo() is False

    def test_can_redo_false(self, image_engine):
        assert image_engine.can_redo() is False


class TestGetDisplayImage:
    def test_returns_full_original_by_default(self, image_engine):
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        image_engine.img_full_original = img
        result = image_engine.get_display_image()
        assert result is img

    def test_returns_source_when_crop_interacting(self, image_engine):
        img_full = np.zeros((100, 100, 3), dtype=np.uint8)
        img_source = np.ones((100, 100, 3), dtype=np.uint8) * 128
        image_engine.img_full_original = img_full
        image_engine.img_source_full = img_source
        image_engine.is_interacting = True
        image_engine.interaction_type = "crop"
        result = image_engine.get_display_image()
        assert result is img_source


class TestSourceBakeState:
    def test_none_initially(self, image_engine):
        assert image_engine.get_source_bake_state() is None

    def test_set_and_get(self, image_engine):
        state = {"source_bake_file_name": "bake.exr", "source_bake_type": "denoise"}
        image_engine.set_source_bake_state(state)
        result = image_engine.get_source_bake_state()
        assert result["source_bake_file_name"] == "bake.exr"
        assert result["source_bake_type"] == "denoise"

    def test_set_none_clears(self, image_engine):
        image_engine.set_source_bake_state({"source_bake_file_name": "bake.exr", "source_bake_type": "denoise"})
        image_engine.set_source_bake_state(None)
        assert image_engine.source_bake_file_name is None
        assert image_engine.source_bake_type is None

    def test_set_empty_clears(self, image_engine):
        image_engine.set_source_bake_state({})
        assert image_engine.source_bake_file_name is None


class TestGetRenderResult:
    def test_returns_display_image_when_no_cache(self, image_engine):
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        image_engine.img_full_original = img
        image_engine.cached_thumbnail = None
        image_engine.temp_image = None
        result = image_engine.get_render_result()
        assert result is img

    def test_returns_cached_thumbnail_first(self, image_engine):
        cached = np.ones((50, 50, 3), dtype=np.uint8) * 42
        image_engine.cached_thumbnail = cached
        result = image_engine.get_render_result()
        assert result is cached

    def test_converts_float32_temp_to_uint8(self, image_engine):
        image_engine.cached_thumbnail = None
        image_engine.temp_image = np.ones((50, 50, 3), dtype=np.float32) * 0.5
        result = image_engine.get_render_result()
        assert result.dtype == np.uint8


def test_render_full_resolution_does_not_return_stale_result(monkeypatch):
    """A failed/no-op full-res submission must not reuse a prior export."""
    from src.features.color.core.image_engine import ImageEngine

    engine = ImageEngine.__new__(ImageEngine)
    engine.img_full_original = np.zeros((2, 2, 3), dtype=np.uint8)
    engine.temp_image = np.ones((2, 2, 3), dtype=np.float32)
    monkeypatch.setattr(engine, "render_pipeline", lambda **_: None)

    assert engine.render_full_resolution() is None
    assert engine.temp_image is None


def test_render_full_resolution_rejects_reentrant_request(monkeypatch):
    """A nested full-res call must not start a second render pass."""
    from src.features.color.core.image_engine import ImageEngine

    engine = ImageEngine.__new__(ImageEngine)
    engine.img_full_original = np.zeros((2, 2, 3), dtype=np.uint8)
    engine.temp_image = None
    nested_results = []

    def _nested_render(**_kwargs):
        nested_results.append(engine.render_full_resolution())

    monkeypatch.setattr(engine, "render_pipeline", _nested_render)
    assert engine.render_full_resolution() is None
    assert nested_results == [None]
    assert engine._full_res_render_active is False


def test_request_full_resolution_exposes_cancelable_request(monkeypatch, image_engine):
    """Async full-res callers receive an id and can cancel the same request."""
    image_engine.img_full_original = np.zeros((2, 2, 3), dtype=np.uint8)
    image_engine.renderer = MagicMock()
    submitted = []

    def _submit(**kwargs):
        submitted.append(kwargs)
        return kwargs["render_request_id"]

    monkeypatch.setattr(image_engine, "render_pipeline", _submit)
    request_id = image_engine.request_full_resolution()

    assert request_id == submitted[0]["render_request_id"]
    assert image_engine._full_res_render_active is True
    assert image_engine._full_res_pending_request_id == request_id

    image_engine.cancel_full_resolution(request_id)
    image_engine.renderer.cancel_full_resolution.assert_called_once_with(request_id)

    image_engine._on_full_resolution_failed(request_id, "cancelled")
    assert image_engine._full_res_render_active is False
    assert image_engine._full_res_pending_request_id is None


class TestResetAllLayers:
    def test_resets_all_layer_params(self, image_engine):
        image_engine.layers[0].grading_params["exposure"] = 2.0
        image_engine.add_layer()
        image_engine.layers[1].grading_params["contrast"] = 1.0
        image_engine.reset_all_layers()
        for layer in image_engine.layers:
            assert layer.grading_params["exposure"] == 0
            assert layer.grading_params["contrast"] == 0


class TestDiscardChanges:
    def test_resets_active_layer(self, image_engine):
        image_engine.layers[0].grading_params["exposure"] = 3.0
        image_engine.discard_changes()
        assert image_engine.layers[0].grading_params["exposure"] == 0

    def test_discard_submits_clear_aux_for_active_layer(self, image_engine):
        """C-P0-05: reset 后必须通知渲染器清理该层的陈旧 AUX 纹理。

        否则 GPU 端 layer_textures[layer.id] 仍保留旧 curves/LUT/warper，
        bind 路径继续绑定陈旧纹理，造成"重置后画面仍是旧调色"。
        """
        from src.shared.video.render_commands import RenderCommand

        active = image_engine.layers[0]
        image_engine.renderer = MagicMock()

        image_engine.discard_changes()

        submitted = image_engine.renderer.submit.call_args_list
        clear_calls = [
            c for c in submitted
            if c.args and c.args[0] == RenderCommand.CLEAR_AUX_TEX
        ]
        assert len(clear_calls) == 1
        # G-P0-09: layer_id 必须是稳定 UUID，不再是 id(layer)。
        assert clear_calls[0].args[1] == {'layer_id': active.id}

    def test_discard_skips_clear_aux_when_no_renderer(self, image_engine):
        """无 renderer（夹具默认）时不应抛错。"""
        assert image_engine.renderer is None
        # 不应抛异常
        image_engine.discard_changes()
        assert image_engine.layers[0].grading_params["exposure"] == 0


class TestResetAllLayersClearsAux:
    def test_reset_all_submits_clear_aux_for_each_layer(self, image_engine):
        """C-P0-05: reset_all_layers 必须为每一层提交 CLEAR_AUX_TEX。"""
        from src.shared.video.render_commands import RenderCommand

        image_engine.add_layer()
        image_engine.renderer = MagicMock()
        # G-P0-09: layer_id 是稳定 UUID（layer.id），不再是 id(layer)。
        layer_ids = [l.id for l in image_engine.layers]

        image_engine.reset_all_layers()

        submitted = image_engine.renderer.submit.call_args_list
        cleared_ids = [
            c.args[1]['layer_id'] for c in submitted
            if c.args and c.args[0] == RenderCommand.CLEAR_AUX_TEX
        ]
        assert sorted(cleared_ids) == sorted(layer_ids)


class TestSetInteractionMode:
    def test_set_active(self, image_engine):
        image_engine.set_interaction_mode(True, "crop")
        assert image_engine.is_interacting is True
        assert image_engine.interaction_type == "crop"

    def test_set_inactive(self, image_engine):
        image_engine.is_interacting = True
        image_engine.set_interaction_mode(False)
        assert image_engine.is_interacting is False


class TestVideoRenderGeometry:
    def test_full_resolution_uses_renderer_thread_command(self, image_engine):
        """Full-res requests submit a critical renderer command with RGB data."""
        from src.features.color.core.image_engine import ImageEngine
        from src.shared.video.render_commands import RenderCommand

        image_engine.renderer = MagicMock()
        image_engine.renderer.current_video_texture = None
        image_engine.renderer.current_video_planes = None
        image_engine.renderer.video_source_size = (0, 0)
        image_engine._is_video_mode = False
        image_engine.img_full_original = np.zeros((3, 4, 3), dtype=np.uint8)
        image_engine.img_high = image_engine.img_full_original
        image_engine.visible_rect = (0.0, 0.0, 4.0, 3.0)
        image_engine.viewport_size = (2, 2)
        image_engine._update_mask_textures = lambda: None

        request_id = ImageEngine.render_pipeline(image_engine, is_full_res=True)

        assert request_id.startswith("render-")
        command, payload = image_engine.renderer.submit.call_args.args
        assert command == RenderCommand.RENDER_FULL_RESOLUTION
        assert payload["color_output_target"] == "full_res"
        assert payload["viewport_size"] == (4, 3)
        assert payload["source_image"].dtype == np.float32
        assert payload["source_image"].shape == (3, 4, 3)
        assert payload["full_resolution_request_id"] == request_id

    def test_video_set_image_keeps_video_path_for_raw_and_live_controls(self, image_engine):
        frame = np.zeros((180, 320, 3), dtype=np.uint8)
        image_engine.renderer = MagicMock()

        image_engine.set_image(
            frame,
            frame,
            frame,
            is_new_file=True,
            is_video=True,
            video_path="/tmp/clip.mov",
        )

        assert image_engine.current_file_path == "/tmp/clip.mov"
        assert image_engine._current_video_path == "/tmp/clip.mov"

    def test_grading_render_scales_scene_rect_to_active_video_texture(self, image_engine):
        from src.features.color.core.image_engine import ImageEngine

        image_engine.renderer = MagicMock()
        texture = MagicMock()
        texture.size = (960, 540)
        image_engine.renderer.current_video_texture = texture
        image_engine.renderer.current_video_planes = None
        image_engine.renderer.video_source_size = (960, 540)
        image_engine._is_video_mode = True
        image_engine.img_full_original = np.zeros((2160, 3840, 3), dtype=np.uint8)
        image_engine.img_high = np.zeros((2160, 3840, 3), dtype=np.float32)
        image_engine.scene_size = (3840.0, 2160.0)
        image_engine.visible_rect = (960.0, 540.0, 1920.0, 1080.0)
        image_engine.viewport_size = (960, 540)

        ImageEngine.render_pipeline(image_engine, render_reason='grading')

        image_engine.renderer.submit.assert_called_once()
        _, payload = image_engine.renderer.submit.call_args.args
        assert payload["visible_rect"] == (240.0, 135.0, 480.0, 270.0)
        assert payload["scene_visible_rect"] == (960.0, 540.0, 1920.0, 1080.0)

    def test_video_initial_rect_uses_timeline_scene_size(self, image_engine):
        from src.features.color.core.image_engine import ImageEngine

        image_engine.renderer = MagicMock()
        image_engine.renderer.current_video_texture = None
        image_engine.renderer.current_video_planes = None
        image_engine.renderer.video_source_size = (0, 0)
        image_engine._is_video_mode = True
        image_engine.img_full_original = np.zeros((2160, 3840, 3), dtype=np.uint8)
        image_engine.img_high = np.zeros((2160, 3840, 3), dtype=np.float32)
        image_engine.scene_size = (1920.0, 1080.0)
        image_engine.visible_rect = (0.0, 0.0, 1.0, 1.0)
        image_engine.viewport_size = (960, 540)

        ImageEngine.render_pipeline(image_engine, render_reason='grading')

        image_engine.renderer.submit.assert_called_once()
        _, payload = image_engine.renderer.submit.call_args.args
        assert payload["scene_visible_rect"] == (0.0, 0.0, 1920.0, 1080.0)
        assert payload["visible_rect"] == (0.0, 0.0, 3840.0, 2160.0)


class TestSetMaskPreviewMode:
    def test_enable(self, image_engine):
        image_engine.set_mask_preview_mode(True)
        assert image_engine.show_mask_mode is True

    def test_disable(self, image_engine):
        image_engine.show_mask_mode = True
        image_engine.set_mask_preview_mode(False)
        assert image_engine.show_mask_mode is False


class TestSetSplitViewMode:
    def test_enable(self, image_engine):
        image_engine.set_split_view_mode(True)
        assert image_engine.split_view_active is True
        assert image_engine.viewport_size == (0, 0)

    def test_disable(self, image_engine):
        image_engine.split_view_active = True
        image_engine.set_split_view_mode(False)
        assert image_engine.split_view_active is False


class TestSetSplitPosition:
    def test_valid_value(self, image_engine):
        image_engine.set_split_position(0.7)
        assert image_engine.split_position == 0.7

    def test_clamp_high(self, image_engine):
        image_engine.set_split_position(1.5)
        assert image_engine.split_position == 1.0

    def test_clamp_low(self, image_engine):
        image_engine.set_split_position(-0.5)
        assert image_engine.split_position == 0.0

    def test_boundary_zero(self, image_engine):
        image_engine.set_split_position(0.0)
        assert image_engine.split_position == 0.0

    def test_boundary_one(self, image_engine):
        image_engine.set_split_position(1.0)
        assert image_engine.split_position == 1.0


class TestClearAuxTexHandler:
    """C-P0-05: AsyncRenderer._do_clear_aux_tex 释放并移除陈旧 AUX 纹理。"""

    def _make_renderer(self):
        from PyQt6.QtCore import QThread
        from src.features.color.core.async_renderer import AsyncRenderer

        renderer = AsyncRenderer.__new__(AsyncRenderer)
        QThread.__init__(renderer)
        renderer.layer_textures = {}
        renderer.matte_cache = {}
        renderer._matte_viewport_textures = {}
        renderer.fb_manager = MagicMock()
        return renderer

    def test_clear_releases_and_removes_layer_aux_slot(self):
        renderer = self._make_renderer()
        curves_tex = MagicMock()
        lut_tex = MagicMock()
        mask_tex = MagicMock()
        # G-P0-09: 生产端 layer_id 为稳定 UUID 字符串。
        layer_id = 'layer-uuid-1234'
        renderer.layer_textures[layer_id] = {
            'curves': curves_tex, 'lut3d': lut_tex, 'mask': mask_tex,
        }

        renderer._do_clear_aux_tex({'layer_id': layer_id})

        curves_tex.release.assert_called_once()
        lut_tex.release.assert_called_once()
        mask_tex.release.assert_called_once()
        # 槽位被移除：bind 路径将回退到 identity 默认纹理。
        assert layer_id not in renderer.layer_textures

    def test_clear_releases_and_removes_matte_cache(self):
        renderer = self._make_renderer()
        matte_tex = MagicMock()
        # G-P0-09: 生产端 layer_id 为稳定 UUID 字符串。
        layer_id = 'layer-uuid-5678'
        renderer.matte_cache[layer_id] = {'matte': matte_tex}

        renderer._do_clear_aux_tex({'layer_id': layer_id})

        matte_tex.release.assert_called_once()
        assert layer_id not in renderer.matte_cache

    def test_clear_missing_layer_is_noop(self):
        """清理不存在的层不应抛错。"""
        renderer = self._make_renderer()
        # 不应抛异常
        renderer._do_clear_aux_tex({'layer_id': 'layer-uuid-missing'})
        assert renderer.layer_textures == {}


class TestLayerIdKeyingMigration:
    """G-P0-09: layer 资源字典统一用稳定 UUID（layer.id）作 key。

    此前 layer_textures/matte_cache 用 id(layer)（int），而 remove_layer 走
    layer.id（UUID），删除层后这两类条目无法被清除而泄漏。迁移后两端一致，
    删除/清理应能命中。
    """

    def _make_renderer(self):
        from PyQt6.QtCore import QThread
        from src.features.color.core.async_renderer import AsyncRenderer

        renderer = AsyncRenderer.__new__(AsyncRenderer)
        QThread.__init__(renderer)
        renderer.layer_textures = {}
        renderer.matte_cache = {}
        renderer._matte_viewport_textures = {}
        renderer._mask_epoch = {}
        renderer.baked_textures = {}
        renderer.ai_plugin_textures = {}
        renderer.fb_manager = MagicMock()
        return renderer

    def test_remove_layer_clears_layer_textures_and_matte(self, image_engine):
        """删除层后，用同一 layer.id 作 key 的 layer_textures/matte_cache 必须被清掉。

        这是 G-P0-09 的核心验收：迁移前 remove_layer 传 UUID，但字典以 id(layer)
        （int）为 key，删除是 no-op，条目泄漏。
        """
        from src.shared.video.render_commands import RenderCommand

        # remove_layer 要求至少 2 层；先加一层，再去删第 0 层。
        image_engine.add_layer()
        fake_renderer = self._make_renderer()
        layer = image_engine.layers[0]
        # 模拟该层在 GPU 端有上传过的 AUX 纹理与 matte，key 用 layer.id。
        fake_renderer.layer_textures[layer.id] = {'curves': MagicMock()}
        fake_renderer.matte_cache[layer.id] = {'matte': MagicMock()}

        # remove_layer 会 submit(REMOVE_LAYER, layer.id)；这里直接验证 handler
        # 用同一个 layer.id 能命中并清除上述条目（迁移前会因 key 类型不一致漏掉）。
        image_engine.renderer = MagicMock()
        image_engine.remove_layer(0)

        # 取出 remove_layer 实际提交的 layer_id，交给真实 handler 处理。
        remove_calls = [
            c for c in image_engine.renderer.submit.call_args_list
            if c.args and c.args[0] == RenderCommand.REMOVE_LAYER
        ]
        assert remove_calls
        submitted_layer_id = remove_calls[0].args[1]
        # 提交的 id 必须与字典里的 key（layer.id）一致。
        assert submitted_layer_id == layer.id
        fake_renderer._do_remove_layer(submitted_layer_id)
        assert layer.id not in fake_renderer.layer_textures
        assert layer.id not in fake_renderer.matte_cache

    def test_apply_curves_uses_stable_uuid_layer_id(self, image_engine):
        """apply_curves 提交的 UPDATE_AUX_TEX 的 layer_id 必须是 layer.id（str）。"""
        from src.shared.video.render_commands import RenderCommand

        layer = image_engine.layers[0]
        image_engine.renderer = MagicMock()

        lut = np.ones((256, 1, 3), dtype=np.float32)
        image_engine.apply_curves(lut)

        aux_calls = [
            c for c in image_engine.renderer.submit.call_args_list
            if c.args and c.args[0] == RenderCommand.UPDATE_AUX_TEX
        ]
        assert aux_calls, "expected an UPDATE_AUX_TEX submit"
        submitted_id = aux_calls[0].args[1]['layer_id']
        # 必须是稳定 UUID 字符串，且等于该层的 id。
        assert submitted_id == layer.id
        assert isinstance(submitted_id, str)
