"""P1-COLOR-09 Power Window 子批测试：锁定/隐藏 + 批量复制。

覆盖：
- MaskShape/MaskGroup locked 字段默认值与 to_dict/from_dict 往返
- ShapeItemWidget 锁定按钮信号；面板行构建传递 locked
- MaskShapeItem.set_locked：ItemIsMovable 切换 + 句柄禁用；句柄事件门控
- 控制器：duplicate（原地复制+新 id+单一 undo push）、copy-to-layer
  （全量复制+新 id+目标层 dirty+undo push）、锁定参数应用路径
"""
from unittest.mock import MagicMock


from src.features.color.core.layer import Layer, MaskGroup, MaskShape


# ---------------------------------------------------------------------------
# 数据层：locked 字段
# ---------------------------------------------------------------------------

def test_maskshape_locked_default_and_roundtrip():
    shape = MaskShape(MaskShape.CIRCLE)
    assert shape.locked is False
    shape.locked = True
    data = shape.to_dict()
    assert data["locked"] is True
    restored = MaskShape.from_dict(data)
    assert restored.locked is True
    assert restored.id == shape.id
    # 旧工程无 locked 键 → False
    assert not MaskShape.from_dict({**data, "locked": None}).locked
    legacy_data = {k: v for k, v in data.items() if k != "locked"}
    assert MaskShape.from_dict(legacy_data).locked is False


def test_maskgroup_locked_default_and_roundtrip():
    group = MaskGroup("G")
    assert group.locked is False
    child = MaskShape(MaskShape.RECT)
    child.locked = True
    group.add_child(child)
    group.locked = True
    restored = MaskGroup.from_dict(group.to_dict())
    assert restored.locked is True
    assert restored.children[0].locked is True


# ---------------------------------------------------------------------------
# 面板：锁定按钮
# ---------------------------------------------------------------------------

def test_shape_item_widget_lock_toggle(qapp):
    from src.features.color.ui.mask_panel import ShapeItemWidget

    widget = ShapeItemWidget("Win", True, False, "add", "circle", locked=False)
    assert widget.lock_btn.isChecked() is False

    events = []
    widget.lock_toggled.connect(lambda v: events.append(v))
    widget.lock_btn.setChecked(True)
    assert events == [True]
    assert widget.lock_btn.toolTip()  # 锁定态 tooltip 非空


def test_mask_panel_row_emits_locked_update(qapp):
    from src.features.color.ui.mask_panel import MaskPanel

    panel = MaskPanel()
    layer = Layer("L")
    shape = MaskShape(MaskShape.CIRCLE)
    shape.locked = True
    layer.mask_shapes = [shape]

    updates = []
    panel.shape_updated.connect(
        lambda sid, params: updates.append((sid, params)))
    panel.update_shapes_list(layer.mask_shapes)
    panel._do_update_shapes_list()  # update_shapes_list 是 0ms 延迟执行

    # 行构建时读取 locked；切换按钮 → shape_updated({"locked": ...})
    widget = None
    from src.features.color.ui.mask_panel import ShapeItemWidget
    for w in panel.findChildren(ShapeItemWidget):
        widget = w
    assert widget is not None
    assert widget.lock_btn.isChecked() is True
    widget.lock_btn.setChecked(False)
    assert updates and updates[-1] == (shape.id, {"locked": False})


# ---------------------------------------------------------------------------
# Canvas：锁定门控
# ---------------------------------------------------------------------------

def test_mask_shape_item_set_locked(qapp):
    from PyQt6.QtWidgets import QGraphicsItem, QGraphicsScene
    from src.features.color.ui.canvas import MaskShapeItem

    scene = QGraphicsScene()
    canvas = MagicMock()
    item = MaskShapeItem("s1", "circle", canvas)
    scene.addItem(item)
    assert item._locked is False

    item.set_locked(True)
    assert item._locked is True
    assert not item.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable
    # 句柄全部禁用
    assert all(not h.isEnabled() for h in item.handles.values())

    item.set_locked(False)
    assert item.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable
    assert all(h.isEnabled() for h in item.handles.values())


def test_mask_handle_press_blocked_when_parent_locked(qapp):
    from PyQt6.QtWidgets import QGraphicsItem, QGraphicsScene
    from PyQt6.QtCore import QPointF
    from src.features.color.ui.canvas import MaskShapeItem

    scene = QGraphicsScene()
    canvas = MagicMock()
    item = MaskShapeItem("s1", "circle", canvas)
    scene.addItem(item)
    handle = item.handles['resize_w']

    # 锁定：句柄按下在进入 super 前被忽略（MagicMock 即可，不触 Qt 内部）
    item.set_locked(True)
    ignored = []
    event = MagicMock()
    event.ignore = lambda: ignored.append(True)
    handle.mousePressEvent(event)
    assert ignored == [True]
    assert handle._is_dragging is False  # 未进入拖动标记
    # 位置变更同样被冻结在原位
    frozen = handle.itemChange(
        QGraphicsItem.GraphicsItemChange.ItemPositionChange, QPointF(50, 50))
    assert frozen == handle.pos()


def test_flatten_locked_inherits_from_group():
    """canvas 同步的扁平化：组锁定继承到子形状（以纯函数语义验证）。"""
    group = MaskGroup("G")
    group.locked = True
    child = MaskShape(MaskShape.RECT)
    child.locked = False
    group.add_child(child)
    free = MaskShape(MaskShape.CIRCLE)

    def flatten(items, parent_locked=False):
        out = []
        for it in items:
            locked = parent_locked or getattr(it, 'locked', False)
            if isinstance(it, MaskGroup):
                out.extend(flatten(it.children, locked))
            else:
                out.append((it, locked))
        return out

    flat = flatten([group, free])
    assert flat[0] == (child, True)   # 继承组锁
    assert flat[1] == (free, False)


# ---------------------------------------------------------------------------
# 控制器：批量复制（bare mixin）
# ---------------------------------------------------------------------------

def _controller(layers, active_index=0):
    from src.app.controllers.color_controller import ColorControllerMixin

    ctrl = ColorControllerMixin.__new__(ColorControllerMixin)
    ctrl.engine = MagicMock()
    ctrl.engine.layers = layers
    ctrl.engine.active_layer_index = active_index
    ctrl.engine.get_active_layer.return_value = layers[active_index]
    ctrl.engine.img_full_original = None
    pushes = []
    ctrl.engine.history.push_state = lambda *a, **kw: pushes.append((a, kw))
    ctrl.canvas = MagicMock()
    ctrl.mask_panel = MagicMock()
    ctrl.layer_panel = MagicMock()
    ctrl.status_bar = MagicMock()
    renders = []
    ctrl.request_render = lambda: renders.append(True)
    return ctrl, pushes, renders


def test_duplicate_mask_shape(qapp):
    layer = Layer("L")
    shape = MaskShape(MaskShape.CIRCLE)
    shape.locked = True
    shape.feather = 77
    layer.mask_shapes = [shape]
    ctrl, pushes, renders = _controller([layer])

    ctrl.on_duplicate_mask_shape(shape.id)
    assert len(layer.mask_shapes) == 2
    clone = layer.mask_shapes[1]
    assert clone.id != shape.id
    assert clone.locked is True and clone.feather == 77
    assert clone.type == MaskShape.CIRCLE
    assert len(pushes) == 1 and len(pushes[0][0]) == 3  # 单一 undo push
    assert renders
    assert layer.is_dirty and layer.mask_dirty
    # 未知 id 不动作
    ctrl.on_duplicate_mask_shape("missing")
    assert len(layer.mask_shapes) == 2


def test_duplicate_mask_group(qapp):
    layer = Layer("L")
    group = MaskGroup("G")
    child = MaskShape(MaskShape.RECT)
    group.add_child(child)
    layer.mask_shapes = [group]
    ctrl, _, _ = _controller([layer])

    ctrl.on_duplicate_mask_shape(group.id)
    assert len(layer.mask_shapes) == 2
    clone = layer.mask_shapes[1]
    assert isinstance(clone, MaskGroup)
    assert clone.id != group.id
    assert clone.children[0].id != child.id  # 子形状 id 同样全新


def test_copy_masks_to_layer(qapp, monkeypatch):
    layer_a = Layer("A")
    s1 = MaskShape(MaskShape.CIRCLE)
    s2 = MaskShape(MaskShape.RECT)
    layer_a.mask_shapes = [s1, s2]
    layer_b = Layer("B")
    ctrl, pushes, renders = _controller([layer_a, layer_b])

    # 弹菜单：直接选第一个动作（图层 B）——QMenu 在方法内 import，
    # 需 patch PyQt6.QtWidgets.QMenu 源属性
    chosen = {}

    class FakeMenu:
        def __init__(self, *a, **kw):
            self._acts = []

        def addAction(self, label):
            act = MagicMock()
            act._label = label
            self._acts.append(act)
            return act

        def exec(self, pos):
            chosen["menu"] = self
            return self._acts[0]

    monkeypatch.setattr("PyQt6.QtWidgets.QMenu", FakeMenu)
    ctrl.on_copy_masks_to_layer_requested()

    assert len(layer_b.mask_shapes) == 2
    assert [s.id for s in layer_b.mask_shapes] != [s1.id, s2.id]
    assert layer_b.is_dirty and layer_b.mask_dirty
    assert layer_a.mask_shapes == [s1, s2]  # 源不动
    assert len(pushes) == 1
    assert renders
    assert ctrl.status_bar.showMessage.called


def test_copy_masks_nothing_to_copy(qapp):
    layer_a = Layer("A")
    layer_b = Layer("B")
    ctrl, pushes, _ = _controller([layer_a, layer_b])
    ctrl.on_copy_masks_to_layer_requested()  # 无窗口 → 状态栏提示，无复制
    assert len(layer_b.mask_shapes) == 0
    assert pushes == []


def test_locked_param_applied_via_process_mask_update():
    """process_mask_update 的 locked 键正确落到 shape/group。"""
    from src.app.controllers.color_controller import ColorControllerMixin

    ctrl = ColorControllerMixin.__new__(ColorControllerMixin)
    layer = Layer("L")
    shape = MaskShape(MaskShape.CIRCLE)
    group = MaskGroup("G")
    layer.mask_shapes = [shape, group]
    ctrl.engine = MagicMock()
    ctrl.engine.get_active_layer.return_value = layer
    ctrl.canvas = MagicMock()
    ctrl.canvas.active_shape_id = None
    ctrl.mask_panel = MagicMock()
    ctrl.request_render = lambda: None
    ctrl._trigger_autosave = lambda: None

    ctrl.pending_mask_updates = {
        shape.id: {"locked": True},
        group.id: {"locked": True},
    }
    ctrl.process_mask_update()
    assert shape.locked is True
    assert group.locked is True
