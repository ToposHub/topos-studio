"""W5-G4 修复回归测试（BUG-A3-01 / BUG-C4-01 / AUD-R2-007）。

* BUG-A3-01: 无 ``.topos`` 侧车的文件批量导出必须成功（修复前
  ``_batch_export_single_file`` 在翻转状态恢复处抛 UnboundLocalError）。
* BUG-C4-01: 遮罩结构改动（删/清空/层级重建）必须先提交撤销快照，并且
  用**真实** ``HistoryManager`` 验证 undo 能恢复改动前的遮罩树（现有
  ``test_color_controller_coverage.py`` 的 ``_FakeHistory`` 只记录调用，
  不验证快照可回放）。
* AUD-R2-007: 凭据偏好读取的默认路径由 ``user_ai_config`` 模块解析——
  宿主机偏好为 keychain 时不得影响测试（隔离点在
  ``user_ai_config.USER_AI_CONFIG_PATH``，而非 ``credential_store``）。
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, List, Optional
from unittest.mock import MagicMock

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.app.controllers.color_controller import ColorControllerMixin
from src.app.controllers.deliver_controller import DeliverControllerMixin
from src.app.controllers import deliver_controller as dc
from src.features.color.core.history import HistoryManager
from src.features.color.core.layer import Layer, MaskGroup, MaskShape


# ═══════════════════════════════════════════════════════════════════════
# BUG-A3-01: batch export without a .topos sidecar
# ═══════════════════════════════════════════════════════════════════════


class _DeliverHarness(DeliverControllerMixin):
    """Minimal host for ``_batch_export_single_file``（无 Qt 依赖路径）。"""

    def __init__(self) -> None:  # noqa: D107
        self.engine = MagicMock()
        self.engine.RAW_EXTENSIONS = {".dng", ".cr2"}
        self.engine.img_full_original = None
        rendered = np.full((2, 3, 3), 0.5, dtype=np.float32)
        self.engine.get_current_image.return_value = rendered
        self._batch_settings = {"options": {}, "format": "JPEG"}
        self._batch_files: List[str] = []
        self._batch_export_dir = ""
        self._batch_paused = False
        self._batch_cancelled = False


def test_batch_export_without_topos_sidecar_succeeds(
    qapp: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """BUG-A3-01（已修复）：无侧车文件导出成功且引擎复位到全新 Layer 1。

    修复前 ``else`` 分支不给 ``project_data`` 赋值，约 20 行后的
    ``if isinstance(project_data, dict):`` 抛 UnboundLocalError，批量队列中
    任何未保存过 ``.topos`` 的文件都被上层记为渲染失败。
    """
    harness = _DeliverHarness()
    src = str(tmp_path / "fresh.png")  # 故意不写 .topos 侧车
    assert not os.path.exists(f"{os.path.splitext(src)[0]}.topos")
    harness._batch_files = [src]
    harness._batch_export_dir = str(tmp_path / "out")
    os.makedirs(harness._batch_export_dir, exist_ok=True)

    loaded = np.full((2, 3, 3), 0.5, dtype=np.float32)
    file_io = MagicMock()
    file_io.load_image.return_value = (loaded, None, None)
    file_io.save_image.return_value = True
    monkeypatch.setattr(dc, "FileIO", file_io)

    out: Optional[str] = harness._batch_export_single_file(src, 1)

    assert out == os.path.join(harness._batch_export_dir, "fresh.jpg")
    layers = harness.engine.layers
    assert len(layers) == 1
    assert isinstance(layers[0], Layer)
    assert layers[0].name == "Layer 1"
    # 无侧车 → 无工程数据 → 不得尝试恢复翻转状态
    harness.engine.set_flip_state.assert_not_called()
    # 保存的是降位后的 uint8 缓冲
    saved = file_io.save_image.call_args.args[1]
    assert saved.dtype == np.uint8


# ═══════════════════════════════════════════════════════════════════════
# BUG-C4-01: mask structure edits are undoable (real HistoryManager)
# ═══════════════════════════════════════════════════════════════════════


class _Signal:
    def __init__(self) -> None:
        self.calls = 0

    def emit(self, *args: Any) -> None:
        self.calls += 1


class _Engine:
    """Real-layer engine stand-in with a REAL HistoryManager."""

    def __init__(self) -> None:
        self.layers: List[Layer] = [Layer("Main")]
        self.active_layer_index: int = 0
        self.history = HistoryManager()
        self.mask_shapes_changed = _Signal()
        self.img_full_original: Optional[np.ndarray] = np.zeros(
            (4, 8, 3), dtype=np.float32)

    def get_active_layer(self) -> Optional[Layer]:
        if 0 <= self.active_layer_index < len(self.layers):
            return self.layers[self.active_layer_index]
        return None

    def reset_sam_points(self) -> None:
        pass


class _Panel:
    def update_shapes_list(self, shapes: Any) -> None:
        pass

    def update_layers(self, layers: Any, index: Any) -> None:
        pass

    def select_shape(self, shape_id: Any) -> None:
        pass

    def set_params(self, *args: Any) -> None:
        pass

    def set_transform_visible(self, mode: Any) -> None:
        pass


class _Canvas:
    def __init__(self) -> None:
        self.active_shape_id: Optional[str] = None

    def set_active_shape(self, shape_id: Any) -> None:
        self.active_shape_id = shape_id

    def set_drawing_mode(self, enabled: Any) -> None:
        pass

    def update_mask_overlay(self, layer: Any) -> None:
        pass

    def set_sam_mode(self, enabled: Any) -> None:
        pass

    def clear_sam_points(self) -> None:
        pass


class _MaskHost(ColorControllerMixin):
    """Plain (non-Qt) host: only the attributes the mask slots read."""

    def __init__(self) -> None:  # noqa: D107
        self.engine = _Engine()
        self.canvas = _Canvas()
        self.mask_panel = _Panel()
        self.layer_panel = _Panel()
        self.status_bar = SimpleNamespace(showMessage=lambda *a, **k: None)
        self.render_calls = 0
        self.autosave_calls = 0
        self.tracker_selections: List[Any] = []
        self.pending_mask_updates: dict = {}

    def request_render(self) -> None:
        self.render_calls += 1

    def _trigger_autosave(self) -> None:
        self.autosave_calls += 1

    def _on_tracker_shape_selected(self, shape_id: Any) -> None:
        self.tracker_selections.append(shape_id)


def _undo_restored_layers(host: _MaskHost) -> List[Layer]:
    """Undo once and deserialize the restored layer tree."""
    state = host.engine.history.undo(
        host.engine.img_full_original, host.engine.layers,
        host.engine.active_layer_index)
    assert state is not None, "undo stack must hold a pre-mutation snapshot"
    return HistoryManager.deserialize_layers(state["layers"])


def test_mask_remove_is_undoable_with_real_history(
    qapp: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """BUG-C4-01（已修复）：删除遮罩可撤销，树恢复到删除前。"""
    host = _MaskHost()
    layer = host.engine.get_active_layer()
    assert layer is not None
    shape = MaskShape("circle")
    layer.mask_shapes.append(shape)

    host.on_mask_shape_removed(shape.id)
    assert layer.mask_shapes == []
    assert host.engine.history.can_undo()

    restored = _undo_restored_layers(host)
    restored_ids = [item.id for item in restored[0].mask_shapes]
    assert restored_ids == [shape.id], (
        "undo must bring the deleted Power Window back (BUG-C4-01)")
    assert isinstance(restored[0].mask_shapes[0], MaskShape)
    assert restored[0].mask_shapes[0].type == "circle"


def test_mask_clear_all_is_undoable_with_real_history(
    qapp: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """BUG-C4-01（已修复）：Clear All Masks 可撤销（含群组嵌套树）。"""
    host = _MaskHost()
    layer = host.engine.get_active_layer()
    assert layer is not None
    group = MaskGroup("G")
    child = MaskShape("rect")
    group.add_child(child)
    layer.mask_shapes.append(group)
    layer.mask_shapes.append(MaskShape("circle"))

    host.on_all_masks_cleared()
    assert layer.mask_shapes == []
    assert host.engine.mask_shapes_changed.calls == 1

    restored = _undo_restored_layers(host)
    tree = restored[0].mask_shapes
    assert len(tree) == 2
    assert isinstance(tree[0], MaskGroup)
    assert tree[0].children[0].type == "rect"
    assert tree[1].type == "circle"


def test_mask_hierarchy_drag_is_undoable_with_real_history(
    qapp: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """BUG-C4-01（已修复）：层级拖拽（树重建）可撤销。

    快照必须在 ``extract_items`` 就地清空群组 children **之前**提交，
    否则 undo 恢复的是被拆散的中间态。
    """
    host = _MaskHost()
    layer = host.engine.get_active_layer()
    assert layer is not None
    shape = MaskShape("circle")
    layer.mask_shapes.append(shape)

    # 拖拽结果：建一个新群组并把形状挂进去
    host.on_mask_group_added([shape.id])
    assert len(layer.mask_shapes) == 1
    assert isinstance(layer.mask_shapes[0], MaskGroup)

    host.on_mask_hierarchy_updated([{"id": layer.mask_shapes[0].id}])

    # 两次结构改动 → 两次 undo：先回到"建组后"，再回到"原始平铺"
    first = _undo_restored_layers(host)
    assert isinstance(first[0].mask_shapes[0], MaskGroup)
    second = _undo_restored_layers(host)
    assert [item.id for item in second[0].mask_shapes] == [shape.id]


def test_brush_stroke_commits_undo_snapshot(
    qapp: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """BUG-C4-01（已修复）：每条落笔提交一个撤销快照（copy_image=False）。"""
    host = _MaskHost()
    layer = host.engine.get_active_layer()
    assert layer is not None

    host.on_brush_stroke_added([(0, 0), (1, 1)],
                               {"size": 30, "feather": 2, "opacity": 1.0,
                                "flow": 1.0})

    assert len(layer.mask_shapes) == 1
    brush = layer.mask_shapes[0]
    assert brush.type == "brush" and len(brush.brush_strokes) == 1

    restored = _undo_restored_layers(host)
    assert restored[0].mask_shapes == [], (
        "undo must remove the stroke's effects (BUG-C4-01)")


def test_mask_param_debounce_still_not_undo_pinned(
    qapp: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """BUG-C4-01 修复边界：参数微调（防抖路径）刻意不入撤销栈。"""
    host = _MaskHost()

    # on_mask_shape_updated 只入队 + 启动防抖 timer；不直接 push
    host.mask_update_timer = SimpleNamespace(
        isActive=lambda: False, start=lambda: None)
    host.on_mask_shape_updated("some-id", {"feather": 5})

    assert not host.engine.history.can_undo()


# ═══════════════════════════════════════════════════════════════════════
# AUD-R2-007: credential preference isolation point
# ═══════════════════════════════════════════════════════════════════════


def test_credential_preference_resolves_via_user_ai_config_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """AUD-R2-007：偏好读取走 ``user_ai_config`` 的默认路径。

    宿主机 ``~/.topos/ai_profiles.json`` 为 keychain 时，
    ``get_credential_storage_preference`` 必须如实返回 keychain（语义不
    变）；测试隔离点因此必须是 ``user_ai_config.USER_AI_CONFIG_PATH``，
    而不是 ``credential_store.USER_AI_CONFIG_PATH``（后者对 get/set 的
    默认路径无效）。
    """
    from src.features.edit.ai_editing import credential_store as cs
    from src.features.edit.ai_editing import user_ai_config as uac

    host_cfg = tmp_path / "host-ai_profiles.json"
    host_cfg.write_text('{"credential_storage": "keychain"}',
                        encoding="utf-8")
    monkeypatch.setattr(uac, "USER_AI_CONFIG_PATH", host_cfg)

    assert cs.get_credential_storage_preference() == "keychain"

    # 反证：只隔离 credential_store 侧的绑定不影响 get/set 的默认路径
    monkeypatch.setattr(cs, "USER_AI_CONFIG_PATH",
                        tmp_path / "unused-ai_profiles.json")
    assert cs.get_credential_storage_preference() == "keychain"

    # set 也必须落在 user_ai_config 的默认路径上
    cs.set_credential_storage_preference("file")
    import json
    assert json.loads(host_cfg.read_text(encoding="utf-8"))[
        "credential_storage"] == "file"


def test_credential_preference_default_is_file_on_pristine_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """AUD-R2-007：隔离到全新配置时默认偏好恒为 file（不依赖宿主状态）。"""
    from src.features.edit.ai_editing import credential_store as cs
    from src.features.edit.ai_editing import user_ai_config as uac

    pristine = tmp_path / "pristine-ai_profiles.json"
    monkeypatch.setattr(uac, "USER_AI_CONFIG_PATH", pristine)
    monkeypatch.setattr(cs, "USER_AI_CONFIG_PATH", pristine)

    assert cs.get_credential_storage_preference() == "file"
