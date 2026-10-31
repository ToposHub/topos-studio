"""
Tests for sticky_note_item.py — Sticky Note functionality.

Covers:
- Sticky Note creation
- Property setters/getters (text, color, font_size, z_order)
- Serialization (to_dict / from_dict)
- Resize handle update

Note: Tests focus on pure logic; UI interaction is not tested.
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from PyQt6.QtCore import QPointF, QRectF
from src.features.composite.ui.sticky_note_item import StickyNoteItem


# Qt 图形测试 marker
pytestmark = pytest.mark.visual


@pytest.fixture(autouse=True)
def _qapp(qapp):
    """Ensure QApplication exists for all sticky note tests."""
    pass


class TestStickyNoteCreation:
    """Sticky Note creation and initialization."""

    def test_create_sticky_note_with_defaults(self):
        """创建 sticky note 使用默认值。"""
        note = StickyNoteItem(note_id="note-1")

        assert note.note_id() == "note-1"
        assert note.text() == ""
        assert note.color().lower() == "#ffe082"  # Yellow default
        assert note.rect().width() == 200
        assert note.rect().height() == 150
        assert note.fontSize() == 12
        assert note.zOrder() == -5

    def test_create_sticky_note_with_text(self):
        """创建 sticky note 使用指定文本。"""
        note = StickyNoteItem(
            note_id="note-1",
            text="Hello World"
        )

        assert note.text() == "Hello World"

    def test_create_sticky_note_with_position(self):
        """创建 sticky note 使用指定位置。"""
        note = StickyNoteItem(
            note_id="note-1",
            position=QPointF(100, 200)
        )

        assert note.pos().x() == 100
        assert note.pos().y() == 200

    def test_create_sticky_note_with_size(self):
        """创建 sticky note 使用指定大小。"""
        note = StickyNoteItem(
            note_id="note-1",
            size=QRectF(0, 0, 300, 200)
        )

        assert note.rect().width() == 300
        assert note.rect().height() == 200

    def test_create_sticky_note_with_color(self):
        """创建 sticky note 使用指定颜色。"""
        note = StickyNoteItem(
            note_id="note-1",
            color="#81D4FA"  # Blue
        )

        assert note.color().lower() == "#81d4fa"

    def test_create_sticky_note_with_font_size(self):
        """创建 sticky note 使用指定字体大小。"""
        note = StickyNoteItem(
            note_id="note-1",
            font_size=16
        )

        assert note.fontSize() == 16

    def test_create_sticky_note_with_z_order(self):
        """创建 sticky note 使用指定 z-order。"""
        note = StickyNoteItem(
            note_id="note-1",
            z_order=-3
        )

        assert note.zOrder() == -3
        assert note.zValue() == -3

    def test_default_colors_available(self):
        """应有预设颜色。"""
        assert len(StickyNoteItem.DEFAULT_COLORS) == 5
        colors = StickyNoteItem.DEFAULT_COLORS
        assert colors[0] == "#FFE082"  # Yellow
        assert colors[1] == "#81D4FA"  # Blue
        assert colors[2] == "#A5D6A7"  # Green
        assert colors[3] == "#F8BBD9"  # Pink
        assert colors[4] == "#FFCC80"  # Orange


class TestStickyNoteProperties:
    """Property setters and getters."""

    def test_set_text(self):
        """设置文本。"""
        note = StickyNoteItem(note_id="note-1")
        note.setText("New text content")

        assert note.text() == "New text content"

    def test_set_color(self):
        """设置颜色。"""
        note = StickyNoteItem(note_id="note-1")
        note.setColor("#A5D6A7")  # Green

        assert note.color().lower() == "#a5d6a7"

    def test_set_font_size(self):
        """设置字体大小。"""
        note = StickyNoteItem(note_id="note-1")
        note.setFontSize(18)

        assert note.fontSize() == 18

    def test_set_z_order(self):
        """设置 z-order。"""
        note = StickyNoteItem(note_id="note-1")
        note.setZOrder(-8)

        assert note.zOrder() == -8
        assert note.zValue() == -8

    def test_item_flags(self):
        """Sticky note 应有正确的交互 flags。"""
        from PyQt6.QtWidgets import QGraphicsItem
        note = StickyNoteItem(note_id="note-1")

        # Should be movable
        assert note.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable
        # Should be selectable
        assert note.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
        # Should send geometry changes
        assert note.flags() & QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        # Should accept hover events
        assert note.acceptHoverEvents()


class TestStickyNoteSerialization:
    """Serialization: to_dict / from_dict."""

    def test_serialize_sticky_note(self):
        """序列化 sticky note。"""
        note = StickyNoteItem(
            note_id="note-1",
            text="Important note",
            color="#81D4FA",
            position=QPointF(150, 250),
            size=QRectF(0, 0, 250, 180),
            font_size=14,
            z_order=-7
        )

        data = note.to_dict()

        assert data["id"] == "note-1"
        assert data["type"] == "sticky_note"
        assert data["text"] == "Important note"
        assert data["color"].lower() == "#81d4fa"
        assert data["position"] == [150, 250]
        assert data["size"] == [250, 180]
        assert data["font_size"] == 14
        assert data["z_order"] == -7

    def test_deserialize_sticky_note(self):
        """反序列化 sticky note。"""
        data = {
            "id": "note-1",
            "type": "sticky_note",
            "text": "Restored note",
            "color": "#A5D6A7",
            "position": [200, 300],
            "size": [180, 120],
            "font_size": 16,
            "z_order": -4
        }

        note = StickyNoteItem.from_dict(data)

        assert note.note_id() == "note-1"
        assert note.text() == "Restored note"
        assert note.color().lower() == "#a5d6a7"
        assert note.pos().x() == 200
        assert note.pos().y() == 300
        assert note.rect().width() == 180
        assert note.rect().height() == 120
        assert note.fontSize() == 16
        assert note.zOrder() == -4

    def test_roundtrip_serialization(self):
        """序列化和反序列化应保留所有属性。"""
        original = StickyNoteItem(
            note_id="note-test",
            text="Test note content",
            color="#F8BBD9",
            position=QPointF(50, 100),
            size=QRectF(0, 0, 280, 160),
            font_size=14,
            z_order=-6
        )

        data = original.to_dict()
        restored = StickyNoteItem.from_dict(data)

        assert restored.note_id() == original.note_id()
        assert restored.text() == original.text()
        assert restored.color() == original.color()
        assert restored.pos().x() == original.pos().x()
        assert restored.pos().y() == original.pos().y()
        assert restored.rect().width() == original.rect().width()
        assert restored.rect().height() == original.rect().height()
        assert restored.fontSize() == original.fontSize()
        assert restored.zOrder() == original.zOrder()

    def test_deserialize_with_defaults(self):
        """反序列化时缺少的属性应使用默认值。"""
        data = {
            "id": "note-1",
            "type": "sticky_note",
            "text": "Minimal note",
            "position": [0, 0],
            "size": [150, 100]
        }

        note = StickyNoteItem.from_dict(data)

        # Missing fields should use defaults
        assert note.color().lower() == "#ffe082"  # default yellow
        assert note.fontSize() == 12  # default font size
        assert note.zOrder() == -5  # default z_order

    def test_serialize_empty_text(self):
        """序列化空文本。"""
        note = StickyNoteItem(note_id="note-1", text="")
        data = note.to_dict()

        assert data["text"] == ""

    def test_serialize_multiline_text(self):
        """序列化多行文本。"""
        note = StickyNoteItem(
            note_id="note-1",
            text="Line 1\nLine 2\nLine 3"
        )
        data = note.to_dict()

        assert data["text"] == "Line 1\nLine 2\nLine 3"

        restored = StickyNoteItem.from_dict(data)
        assert restored.text() == "Line 1\nLine 2\nLine 3"


class TestStickyNoteResize:
    """Resize handle behavior."""

    def test_resize_handle_exists(self):
        """Sticky note 应有 resize handle。"""
        note = StickyNoteItem(note_id="note-1")
        assert note._resize_handle is not None

    def test_resize_handle_position(self):
        """Resize handle 应在右下角。"""
        note = StickyNoteItem(
            note_id="note-1",
            size=QRectF(0, 0, 200, 150)
        )

        handle_rect = note._resize_handle.rect()
        expected_size = note._resize_handle_size

        # Handle should be at bottom-right
        assert handle_rect.x() == 200 - expected_size - 2
        assert handle_rect.y() == 150 - expected_size - 2
        assert handle_rect.width() == expected_size
        assert handle_rect.height() == expected_size

    def test_resize_handle_updates_on_rect_change(self):
        """改变 rect 时 resize handle 应更新。"""
        note = StickyNoteItem(
            note_id="note-1",
            size=QRectF(0, 0, 200, 150)
        )

        # Change rect
        note.setRect(0, 0, 300, 200)

        handle_rect = note._resize_handle.rect()
        expected_size = note._resize_handle_size

        assert handle_rect.x() == 300 - expected_size - 2
        assert handle_rect.y() == 200 - expected_size - 2

    def test_title_bar_removed(self):
        """标题栏已随 UI 统一（ee007c43）移除：便签为圆角矩形+文本。"""
        note = StickyNoteItem(note_id="note-1")
        assert not hasattr(note, "_title_bar")
        # 文本与缩放手柄仍然存在
        assert note._text_item is not None
        assert note._resize_handle is not None


class TestStickyNoteTextItem:
    """Text display behavior."""

    def test_text_item_exists(self):
        """Sticky note 应有文本显示项。"""
        note = StickyNoteItem(note_id="note-1")
        assert note._text_item is not None

    def test_text_item_content(self):
        """文本项应显示正确内容。"""
        note = StickyNoteItem(
            note_id="note-1",
            text="Test content"
        )

        assert note._text_item.toPlainText() == "Test content"

    def test_text_item_position(self):
        """文本项应在正确位置。"""
        note = StickyNoteItem(note_id="note-1")
        text_pos = note._text_item.pos()

        assert text_pos.x() == note._padding
        assert text_pos.y() == note._padding

    def test_text_width_wraps(self):
        """文本应自动换行。"""
        note = StickyNoteItem(
            note_id="note-1",
            size=QRectF(0, 0, 200, 150)
        )

        # Text width should be rect width minus padding (both sides)
        text_width = note._text_item.textWidth()
        assert text_width == 200 - 2 * note._padding

    def test_text_width_updates_on_resize(self):
        """改变大小时文本宽度应更新。"""
        note = StickyNoteItem(
            note_id="note-1",
            size=QRectF(0, 0, 200, 150)
        )

        note.setRect(0, 0, 300, 200)

        text_width = note._text_item.textWidth()
        assert text_width == 300 - 2 * note._padding


class TestStickyNoteChangedHandler:
    """Change handler callback."""

    def test_set_changed_handler(self):
        """设置 change handler。"""
        note = StickyNoteItem(note_id="note-1")

        handler_called = []
        def handler(n):
            handler_called.append(n.note_id())

        note.set_changed_handler(handler)
        note.setText("New text")

        assert len(handler_called) == 1
        assert handler_called[0] == "note-1"

    def test_handler_called_on_color_change(self):
        """改变颜色时应调用 handler。"""
        note = StickyNoteItem(note_id="note-1")

        handler_called = []
        note.set_changed_handler(lambda n: handler_called.append("color"))

        note.setColor("#81D4FA")

        assert "color" in handler_called

    def test_handler_called_on_font_size_change(self):
        """改变字体大小时应调用 handler。"""
        note = StickyNoteItem(note_id="note-1")

        handler_called = []
        note.set_changed_handler(lambda n: handler_called.append("font"))

        note.setFontSize(16)

        assert "font" in handler_called


class TestStickyNoteRepr:
    """String representation."""

    def test_repr(self):
        """__repr__ 应返回有用的信息。"""
        note = StickyNoteItem(
            note_id="note-123",
            text="This is a test note for repr"
        )

        repr_str = repr(note)
        assert "note-123" in repr_str
        assert "This is a test" in repr_str


class TestStickyNoteCornerRadius:
    """Visual properties."""

    def test_corner_radius_exists(self):
        """应有圆角半径属性。"""
        note = StickyNoteItem(note_id="note-1")
        assert hasattr(note, '_corner_radius')
        assert note._corner_radius == 8.0

    def test_border_color_exists(self):
        """应有边框颜色属性。"""
        note = StickyNoteItem(note_id="note-1", color="#FFE082")
        assert hasattr(note, '_border_color')
        # Border should be darker than background
        assert note._border_color.name() != note.color()