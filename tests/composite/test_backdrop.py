"""
Tests for backdrop_item.py — Backdrop CRUD and serialization.

Covers:
- Backdrop creation
- Property setters/getters (title, note, color, z_order)
- Serialization (to_dict / from_dict)
- Resize handle update

Note: Tests focus on pure logic; UI interaction is not tested.
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtWidgets import QGraphicsScene
from src.features.composite.ui.backdrop_item import BackdropItem


# Qt 图形测试 marker - 可通过 pytest -m "not visual" 跳过
pytestmark = pytest.mark.visual


@pytest.fixture
def scene(qapp):
    """创建 QGraphicsScene 用于测试，并在测试后清理。"""
    scene = QGraphicsScene()
    yield scene
    # 清理 scene 中的所有 items
    scene.clear()


@pytest.fixture
def backdrop(scene):
    """创建 BackdropItem 并添加到 scene，测试后自动清理。"""
    item = BackdropItem(backdrop_id="test-backdrop")
    scene.addItem(item)
    yield item
    # 确保从 scene 移除
    if item.scene() is not None:
        scene.removeItem(item)


class TestBackdropCreation:
    """Backdrop creation and initialization."""

    def test_create_backdrop_with_defaults(self, backdrop):
        """创建 backdrop 使用默认值。"""
        assert backdrop.backdrop_id == "test-backdrop"
        assert backdrop.title() == "Backdrop"
        assert backdrop.note() == ""
        assert backdrop.rect().width() == 300
        assert backdrop.rect().height() == 200
        assert backdrop.zValue() == -10

    def test_create_backdrop_with_custom_title(self, scene):
        """创建 backdrop 使用自定义标题。"""
        backdrop = BackdropItem(
            backdrop_id="backdrop-1",
            title="My Group"
        )
        scene.addItem(backdrop)
        assert backdrop.title() == "My Group"
        scene.removeItem(backdrop)

    def test_create_backdrop_with_position(self, scene):
        """创建 backdrop 使用指定位置。"""
        backdrop = BackdropItem(
            backdrop_id="backdrop-1",
            position=QPointF(100, 200)
        )
        scene.addItem(backdrop)
        assert backdrop.x() == 100
        assert backdrop.y() == 200
        scene.removeItem(backdrop)

    def test_create_backdrop_with_size(self, scene):
        """创建 backdrop 使用指定大小。"""
        backdrop = BackdropItem(
            backdrop_id="backdrop-1",
            size=QRectF(0, 0, 400, 300)
        )
        scene.addItem(backdrop)
        assert backdrop.rect().width() == 400
        assert backdrop.rect().height() == 300
        scene.removeItem(backdrop)

    def test_create_backdrop_with_color(self, scene):
        """创建 backdrop 使用指定颜色。"""
        backdrop = BackdropItem(
            backdrop_id="backdrop-1",
            color="#FF0000"
        )
        scene.addItem(backdrop)
        assert backdrop.color().lower() == "#ff0000"
        scene.removeItem(backdrop)

    def test_create_backdrop_with_note(self, scene):
        """创建 backdrop 使用指定 note。"""
        backdrop = BackdropItem(
            backdrop_id="backdrop-1",
            note="This is a note"
        )
        scene.addItem(backdrop)
        assert backdrop.note() == "This is a note"
        scene.removeItem(backdrop)

    def test_create_backdrop_with_z_order(self, scene):
        """创建 backdrop 使用指定 z-order。"""
        backdrop = BackdropItem(
            backdrop_id="backdrop-1",
            z_order=-20
        )
        scene.addItem(backdrop)
        assert backdrop.zOrder() == -20
        assert backdrop.zValue() == -20
        scene.removeItem(backdrop)

    def test_default_colors_available(self):
        """应有预设颜色。"""
        assert len(BackdropItem.DEFAULT_COLORS) == 5
        for color in BackdropItem.DEFAULT_COLORS:
            assert color.startswith("#")


class TestBackdropProperties:
    """Property setters and getters."""

    def test_set_title(self, backdrop):
        """设置标题。"""
        backdrop.setTitle("New Title")
        assert backdrop.title() == "New Title"

    def test_set_note(self, backdrop):
        """设置 note。"""
        backdrop.setNote("Updated note")
        assert backdrop.note() == "Updated note"

    def test_set_color(self, backdrop):
        """设置颜色。"""
        backdrop.setColor("#3d2d4a")
        assert backdrop.color() == "#3d2d4a"

    def test_set_z_order(self, backdrop):
        """设置 z-order。"""
        backdrop.setZOrder(-5)
        assert backdrop.zOrder() == -5
        assert backdrop.zValue() == -5

    def test_item_flags(self, backdrop):
        """Backdrop 应有正确的交互 flags。"""
        from PyQt6.QtWidgets import QGraphicsItem
        # Should be movable
        assert backdrop.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable
        # Should be selectable
        assert backdrop.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
        # Should send geometry changes
        assert backdrop.flags() & QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges


class TestBackdropSerialization:
    """Serialization: to_dict / from_dict."""

    def test_serialize_backdrop(self, scene):
        """序列化 backdrop。"""
        backdrop = BackdropItem(
            backdrop_id="backdrop-1",
            title="My Group",
            note="Important nodes here",
            position=QPointF(100, 200),
            size=QRectF(0, 0, 400, 300),
            color="#2d4a3e",
            z_order=-15
        )
        scene.addItem(backdrop)

        data = backdrop.to_dict()

        assert data["id"] == "backdrop-1"
        assert data["title"] == "My Group"
        assert data["note"] == "Important nodes here"
        assert data["color"] == "#2d4a3e"
        assert data["position"] == [100, 200]
        assert data["size"] == [400, 300]
        assert data["z_order"] == -15

        scene.removeItem(backdrop)

    def test_deserialize_backdrop(self, scene):
        """反序列化 backdrop。"""
        data = {
            "id": "backdrop-1",
            "title": "Restored Backdrop",
            "note": "Restored note",
            "color": "#3d2d4a",
            "position": [150, 250],
            "size": [350, 250],
            "z_order": -12
        }

        backdrop = BackdropItem.from_dict(data)
        scene.addItem(backdrop)

        assert backdrop.backdrop_id == "backdrop-1"
        assert backdrop.title() == "Restored Backdrop"
        assert backdrop.note() == "Restored note"
        assert backdrop.color() == "#3d2d4a"
        assert backdrop.x() == 150
        assert backdrop.y() == 250
        assert backdrop.rect().width() == 350
        assert backdrop.rect().height() == 250
        assert backdrop.zOrder() == -12

        scene.removeItem(backdrop)

    def test_roundtrip_serialization(self, scene):
        """序列化和反序列化应保留所有属性。"""
        original = BackdropItem(
            backdrop_id="backdrop-test",
            title="Test Group",
            note="Test note",
            position=QPointF(50, 100),
            size=QRectF(0, 0, 250, 180),
            color="#FFCC80",
            z_order=-8
        )
        scene.addItem(original)

        data = original.to_dict()
        restored = BackdropItem.from_dict(data)
        scene.addItem(restored)

        assert restored.backdrop_id == original.backdrop_id
        assert restored.title() == original.title()
        assert restored.note() == original.note()
        assert restored.color() == original.color()
        assert restored.x() == original.x()
        assert restored.y() == original.y()
        assert restored.rect().width() == original.rect().width()
        assert restored.rect().height() == original.rect().height()
        assert restored.zOrder() == original.zOrder()

        scene.removeItem(original)
        scene.removeItem(restored)

    def test_deserialize_with_defaults(self, scene):
        """反序列化时缺少的属性应使用默认值。"""
        data = {
            "id": "backdrop-1",
            "title": "Minimal Backdrop",
            "position": [0, 0],
            "size": [200, 150]
        }

        backdrop = BackdropItem.from_dict(data)
        scene.addItem(backdrop)

        # Missing fields should use defaults
        assert backdrop.note() == ""  # default note
        assert backdrop.color() == BackdropItem.DEFAULT_COLORS[0]  # default color
        assert backdrop.zOrder() == -10  # default z_order

        scene.removeItem(backdrop)


class TestBackdropResize:
    """Resize handle behavior."""

    def test_resize_handle_exists(self, backdrop):
        """Backdrop 应有 resize handle。"""
        assert backdrop._resize_handle is not None

    def test_resize_handle_position(self, scene):
        """Resize handle 应在右下角。"""
        backdrop = BackdropItem(
            backdrop_id="backdrop-1",
            size=QRectF(0, 0, 300, 200)
        )
        scene.addItem(backdrop)

        handle_rect = backdrop._resize_handle.rect()
        expected_size = backdrop._resize_handle_size

        # Handle should be at bottom-right
        assert handle_rect.x() == 300 - expected_size
        assert handle_rect.y() == 200 - expected_size
        assert handle_rect.width() == expected_size
        assert handle_rect.height() == expected_size

        scene.removeItem(backdrop)

    def test_resize_handle_updates_on_rect_change(self, scene):
        """改变 rect 时 resize handle 应更新。"""
        backdrop = BackdropItem(
            backdrop_id="backdrop-1",
            size=QRectF(0, 0, 300, 200)
        )
        scene.addItem(backdrop)

        # Change rect
        backdrop.setRect(0, 0, 400, 300)

        handle_rect = backdrop._resize_handle.rect()
        expected_size = backdrop._resize_handle_size

        assert handle_rect.x() == 400 - expected_size
        assert handle_rect.y() == 300 - expected_size

        scene.removeItem(backdrop)


class TestBackdropContainedNodes:
    """update_contained_nodes logic."""

    def test_update_contained_nodes_empty_list(self, backdrop):
        """空节点列表应返回空列表。"""
        changed = backdrop.update_contained_nodes({})
        assert changed is False
        assert backdrop.contained_node_ids() == []

    def test_update_contained_nodes_with_nodes(self, scene):
        """检查节点是否在 backdrop 内（需要 scene）。"""
        backdrop = BackdropItem(
            backdrop_id="backdrop-1",
            position=QPointF(0, 0),
            size=QRectF(0, 0, 300, 200)
        )
        scene.addItem(backdrop)

        # With an empty dict, no nodes are contained
        changed = backdrop.update_contained_nodes({})
        assert changed is False
        assert backdrop.contained_node_ids() == []

        scene.removeItem(backdrop)


class TestBackdropChangedHandler:
    """Changed handler callback pattern."""

    def test_changed_handler_exists(self, backdrop):
        """Backdrop 应支持 changed_handler。"""
        assert hasattr(backdrop, 'set_changed_handler')
        assert hasattr(backdrop, '_changed_handler')

    def test_handler_called_on_title_change(self, backdrop):
        """改变标题时应调用 handler。"""
        handler_called = []
        backdrop.set_changed_handler(lambda b: handler_called.append("title"))
        backdrop.setTitle("New Title")
        assert "title" in handler_called

    def test_handler_called_on_color_change(self, backdrop):
        """改变颜色时应调用 handler。"""
        handler_called = []
        backdrop.set_changed_handler(lambda b: handler_called.append("color"))
        backdrop.setColor("#3d2d4a")
        assert "color" in handler_called