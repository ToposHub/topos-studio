"""批 A 修复回归测试（2026-09-28 审查，Critical 三项）。

- AUD-0200：``_do_bake_layer`` 引用未定义变量 ``render_request_id`` →
  AI Bake 每次抛 NameError。修复后 bake 前缀渲染走到
  ``build_composition_shader`` 并正常回读/发信号；诊断用
  ``request_id`` 从命令负载提取（缺省 None）。
- AUD-0501：历史快照与活动图层共享可变对象 → undo/redo 静默失效。
  修复后 ``_serialize_layers`` 对 ``to_dict`` 产物整体 deepcopy 冻结。
- AUD-0600：undo/redo 恢复图像后不重传 GPU 纹理、不重置蒙版脏标记。
  修复后 ``_restore_state`` 走与 ``set_image`` 同口径的归一化 +
  ``UPLOAD_IMAGE`` 提交 + 参考纹理刷新 + 全层 ``mask_dirty`` 置位，
  并递增 ``_source_epoch`` 使 bake 前缀过期。

惯例：与 test_async_renderer_coverage.py 一致，AsyncRenderer.__new__
跳过 __init__（无 GL），仅注入被测方法所需属性；需要发信号的用例先执行
QThread.__init__。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from PyQt6.QtCore import QThread

from src.features.color.core.async_renderer import AsyncRenderer
from src.features.color.core.history import HistoryManager
from src.features.color.core.layer import Layer, MaskShape
from src.features.color.core.image_engine import ImageEngine
from src.shared.video.render_commands import RenderCommand


def _small_image(h: int = 4, w: int = 6) -> np.ndarray:
    """确定性的小 BGR uint8 测试图。"""
    return np.arange(h * w * 3, dtype=np.uint8).reshape(h, w, 3)


def _submit_payloads(engine: ImageEngine, command: Any) -> List[Any]:
    """提取 renderer.submit 收到的指定命令 payload 列表。"""
    return [
        c.args[1]
        for c in engine.renderer.submit.call_args_list
        if c.args and c.args[0] == command
    ]


# =============================================================================
# AUD-0200：_do_bake_layer 的 render_request_id NameError
# =============================================================================

def make_bake_stub(**overrides: Any) -> AsyncRenderer:
    """构造可走完 _do_bake_layer 前缀渲染/回读路径的桩（GL 全 mock）。"""
    renderer = AsyncRenderer.__new__(AsyncRenderer)
    QThread.__init__(renderer)

    ctx = MagicMock()
    # framebuffer 回读需返回真实 bytes（np.frombuffer 消费）
    fbo = ctx.framebuffer.return_value
    fbo.read.return_value = b"\x00" * (3 * 4 * 2 * 3)  # w*h*3 分量 * float32

    attrs: Dict[str, Any] = dict(
        ctx=ctx,
        baked_textures={},
        pyramid_textures=[],
        pipeline=MagicMock(),  # build_composition_shader → truthy Mock prog
        _cancel_requested=MagicMock(return_value=False),
        _lookup_layer_ai_texture=MagicMock(return_value=None),
        _generate_matte=MagicMock(return_value=MagicMock()),
        _update_composition_uniforms=MagicMock(),
        _bind_aux_textures_single_layer=MagicMock(),
        _render_quad=MagicMock(),
    )
    attrs.update(overrides)
    for name, value in attrs.items():
        setattr(renderer, name, value)
    return renderer


def _bake_data(w: int = 3, h: int = 2, **extra: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "layer": {"id": "L1", "visible": True},
        "layer_id": "L1",
        "stack_prefix": [{"id": "L1", "visible": True}],
        "source_size": (w, h),
        "full_res_image": np.zeros((h, w, 4), dtype=np.float32),
    }
    data.update(extra)
    return data


class TestAud0200BakeLayerRequestId:
    def test_bake_completes_and_emits_without_name_error(self) -> None:
        """修复前：走到 build_composition_shader 即 NameError，信号永不发射。"""
        renderer = make_bake_stub()
        emitted: List[Tuple[Any, ...]] = []
        renderer.layer_baked_ready.connect(lambda *a: emitted.append(a))

        renderer._do_bake_layer(_bake_data())

        assert len(emitted) == 1
        layer_id, result = emitted[0]
        assert layer_id == "L1"
        assert isinstance(result, np.ndarray)
        assert result.shape == (2, 3, 3)

    def test_request_id_defaults_to_none(self) -> None:
        """BAKE_LAYER 负载不携带 render_request_id → 诊断 ID 为 None。"""
        renderer = make_bake_stub()
        renderer._do_bake_layer(_bake_data())
        kwargs = renderer.pipeline.build_composition_shader.call_args.kwargs
        assert kwargs["request_id"] is None

    def test_request_id_taken_from_payload(self) -> None:
        renderer = make_bake_stub()
        renderer._do_bake_layer(_bake_data(render_request_id="bake-42"))
        kwargs = renderer.pipeline.build_composition_shader.call_args.kwargs
        assert kwargs["request_id"] == "bake-42"


# =============================================================================
# AUD-0501：历史快照与活动图层共享可变对象
# =============================================================================

class TestAud0501HistorySnapshotIsolation:
    def test_undo_restores_pushed_grading_params(
        self, history_manager: HistoryManager, sample_image_bgr: np.ndarray
    ) -> None:
        """压栈后就地改 grading_params 不得污染栈内快照。"""
        layer = Layer("L1")
        layer.grading_params["exposure"] = 1.5
        history_manager.push_state(sample_image_bgr, [layer], 0)

        layer.grading_params["exposure"] = 9.0
        state = history_manager.undo(sample_image_bgr, [layer], 0)

        assert state is not None
        assert state["layers"][0]["grading_params"]["exposure"] == 1.5
        restored = HistoryManager.deserialize_layers(state["layers"])
        assert restored[0].grading_params["exposure"] == 1.5

    def test_mask_points_snapshot_isolated_from_inplace_edit(
        self, history_manager: HistoryManager, sample_image_bgr: np.ndarray
    ) -> None:
        """canvas 式就地拖点（改 points 列表元素）不得污染历史。"""
        layer = Layer("L1")
        shape = MaskShape(MaskShape.RECT)
        shape.points = [[0.1, 0.1], [0.2, 0.2]]
        layer.mask_shapes.append(shape)
        history_manager.push_state(sample_image_bgr, [layer], 0)

        shape.points[0][0] = 0.9  # 拖动曲线点
        state = history_manager.undo(sample_image_bgr, [layer], 0)

        assert state is not None
        assert state["layers"][0]["mask_shapes"][0]["points"][0][0] == 0.1
        restored = HistoryManager.deserialize_layers(state["layers"])
        assert restored[0].mask_shapes[0].points[0][0] == 0.1

    def test_plugin_state_snapshot_isolated(
        self, history_manager: HistoryManager, sample_image_bgr: np.ndarray
    ) -> None:
        """压栈后翻转 plugin enabled / 改 params 不得污染历史。"""
        layer = Layer("L1")
        layer.plugins = [{
            "instance_id": "i1", "plugin_id": "p1",
            "params": {"k": 1.0}, "enabled": True,
        }]
        history_manager.push_state(sample_image_bgr, [layer], 0)

        layer.plugins[0]["enabled"] = False
        layer.plugins[0]["params"]["k"] = 99.0
        state = history_manager.undo(sample_image_bgr, [layer], 0)

        assert state is not None
        snap = state["layers"][0]["plugins"][0]
        assert snap["enabled"] is True
        assert snap["params"]["k"] == 1.0

    def test_redo_side_state_also_isolated(
        self, history_manager: HistoryManager, sample_image_bgr: np.ndarray
    ) -> None:
        """undo() 存入 redo 栈的当前状态同样不得被后续编辑污染。"""
        layer = Layer("L1")
        layer.grading_params["exposure"] = 2.0
        history_manager.push_state(sample_image_bgr, [layer], 0)
        layer.grading_params["exposure"] = 4.0  # 待撤销的修改

        state = history_manager.undo(sample_image_bgr, [layer], 0)
        assert state is not None

        layer.grading_params["exposure"] = 7.0  # undo 后继续编辑
        redo_state = history_manager.redo(state["image"], [layer], 0)

        assert redo_state is not None
        assert redo_state["layers"][0]["grading_params"]["exposure"] == 4.0


# =============================================================================
# AUD-0600：_restore_state 不重传 GPU 纹理 / 不重置蒙版脏标记
# =============================================================================

class TestAud0600RestoreStateGpuResync:
    def test_undo_resubmits_upload_image_normalized(self, image_engine: ImageEngine) -> None:
        """恢复图像必须重传 GPU（float32 [0,1]），否则视口渲染撤销前旧图。"""
        engine = image_engine
        engine.renderer = MagicMock()
        img = np.full((4, 6, 3), 200, dtype=np.uint8)
        engine.img_full_original = img.copy()

        with patch("src.shared.io.file_io.FileIO._resize_to_fit",
                   side_effect=lambda i, s: i):
            engine.history.push_state(engine.img_full_original, engine.layers, 0)
            engine.img_full_original = np.full((2, 3, 3), 7, dtype=np.uint8)
            assert engine.undo() is True

        uploads = _submit_payloads(engine, RenderCommand.UPLOAD_IMAGE)
        assert len(uploads) == 1
        payload = uploads[0]
        assert payload["image"].dtype == np.float32
        assert payload["image"].shape == (4, 6, 3)
        assert payload["image"][0, 0, 0] == pytest.approx(200 / 255.0)
        assert payload["original_size"] == (6, 4)
        assert payload["is_video"] is False
        # 预览缓存维持 set_image 建立的 float32[0,1] 不变量
        assert engine.img_high.dtype == np.float32
        assert engine.img_high.max() <= 1.0

    def test_undo_refreshes_split_reference(self, image_engine: ImageEngine) -> None:
        """split 参考纹理必须与恢复后的主纹理同步（同尺寸同内容）。"""
        engine = image_engine
        engine.renderer = MagicMock()
        engine.img_full_original = _small_image()

        with patch("src.shared.io.file_io.FileIO._resize_to_fit",
                   side_effect=lambda i, s: i):
            engine.history.push_state(engine.img_full_original, engine.layers, 0)
            assert engine.undo() is True

        refs = _submit_payloads(engine, RenderCommand.UPLOAD_REFERENCE)
        assert len(refs) == 1
        assert refs[0].dtype == np.float32
        assert refs[0].shape == (4, 6, 3)

    def test_restore_marks_masks_dirty_when_layers_not_replaced(
        self, image_engine: ImageEngine
    ) -> None:
        """layers_data 为空（图层列表未替换）时也必须全层置 mask_dirty。"""
        engine = image_engine
        for layer in engine.layers:
            layer.mask_dirty = False

        engine._restore_state(
            {"image": _small_image(), "layers": [], "active_layer_index": 0}
        )

        assert engine.layers
        assert all(layer.mask_dirty for layer in engine.layers)

    def test_restore_marks_deserialized_layers_dirty(
        self, image_engine: ImageEngine
    ) -> None:
        engine = image_engine
        state = {
            "image": _small_image(),
            "layers": [layer.to_dict(assets_dir=None) for layer in engine.layers],
            "active_layer_index": 0,
        }
        engine._restore_state(state)
        assert all(layer.mask_dirty for layer in engine.layers)

    def test_restore_bumps_source_epoch(self, image_engine: ImageEngine) -> None:
        """恢复的源像素与 bake 前缀摘要基准不同 → src_epoch 必须递增。"""
        engine = image_engine
        before = engine._source_epoch
        engine._restore_state(
            {"image": _small_image(), "layers": [], "active_layer_index": 0}
        )
        assert engine._source_epoch == before + 1

    def test_restore_without_renderer_is_safe(self, image_engine: ImageEngine) -> None:
        """renderer=None（测试/无 GL 环境）：走 legacy no-op 路径不抛错。"""
        engine = image_engine
        engine._restore_state(
            {"image": _small_image(), "layers": [], "active_layer_index": 0}
        )
        assert engine.img_high.dtype == np.float32
        assert engine.img_high.max() <= 1.0
