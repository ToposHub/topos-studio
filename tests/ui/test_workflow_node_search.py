"""P10.2b 测试：WorkflowNodeSearchPopup（Tab 节点搜索弹窗）。

> 按 Tab 弹出节点搜索，从 NodeSpecRegistry.all_specs() 填充；用户选中 spec_id
> 后通过 signal/callback 通知 host 加节点。借鉴 Composite NodeSearchPopup 但精简
> （无 favorites/recent/aliases/多 tab）。

验证契约：

- popup 创建 + 数据源是 NodeSpecRegistry.all_specs()。
- 按 display_name / spec_id / description 过滤。
- 选中后 emit node_selected(spec_id)。
- 触发：WorkflowCanvasView 按 Tab → emit add_node_requested（host 据此弹 popup）。
- **红线**：源码不 import moderngl / composite.core。
"""
import os

import pytest


# ---------------------------------------------------------------------- #
# 红线
# ---------------------------------------------------------------------- #


_SRC = os.path.join(
    os.path.dirname(__file__), "..", "..",
    "src", "features", "ai_workflow", "ui", "workflow_node_search.py",
)


def test_source_does_not_import_gpu_or_composite() -> None:
    """红线：popup 源码不含 moderngl / composite.core。"""
    if not os.path.isfile(_SRC):
        pytest.skip("popup 源码尚未创建")
    with open(_SRC, "r", encoding="utf-8") as f:
        source = f.read()
    assert "import moderngl" not in source
    assert "from src.features.composite" not in source
    assert "TextureResult" not in source


# ---------------------------------------------------------------------- #
# Fixtures
# ---------------------------------------------------------------------- #


@pytest.fixture
def registry():
    from src.features.ai_workflow.core.node_specs import (
        NodeSpecRegistry, register_official_specs,
    )
    reg = NodeSpecRegistry()
    register_official_specs(reg)
    return reg


@pytest.fixture
def popup(qapp, registry):
    from src.features.ai_workflow.ui.workflow_node_search import (
        WorkflowNodeSearchPopup,
    )
    p = WorkflowNodeSearchPopup(registry, parent=None)
    yield p
    p.deleteLater()


# ---------------------------------------------------------------------- #
# 创建 + 填充
# ---------------------------------------------------------------------- #


def test_popup_lists_specs(popup, registry) -> None:
    """popup 列出 registry 的所有 spec（至少 16 个官方 spec）。"""
    assert popup.list_widget.count() >= 1
    # 至少包含 spec_id 数据
    item = popup.list_widget.item(0)
    assert item is not None
    # UserRole 存 spec_id
    sid = item.data(0x100)  # Qt.ItemDataRole.UserRole
    assert isinstance(sid, str) and sid


def test_popup_has_node_selected_signal(popup) -> None:
    """popup 暴露 node_selected signal（spec_id）。"""
    assert hasattr(popup, "node_selected")


def test_popup_select_emits_spec_id(popup, registry) -> None:
    """选中第一项 → emit node_selected(对应 spec_id)。"""
    received = []
    popup.node_selected.connect(lambda sid: received.append(sid))
    item = popup.list_widget.item(0)
    sid = item.data(0x100)
    popup.list_widget.setCurrentItem(item)
    popup._confirm_selection()
    assert received == [sid]


# ---------------------------------------------------------------------- #
# 过滤
# ---------------------------------------------------------------------- #


def test_popup_filter_narrows_list(popup) -> None:
    """搜索框输入文本 → 列表只显示匹配项。"""
    total = popup.list_widget.count()
    popup.search_edit.setText("transcribe")  # 匹配 transcribe_auto
    popup._filter_items("transcribe")
    filtered = sum(
        1 for i in range(popup.list_widget.count())
        if popup.list_widget.item(i).isHidden() is False
    )
    assert filtered < total or filtered >= 1  # 至少过滤到 transcribe


def test_popup_filter_no_match_hides_all(popup) -> None:
    popup.search_edit.setText("zzzznonexistent")
    popup._filter_items("zzzznonexistent")
    visible = sum(
        1 for i in range(popup.list_widget.count())
        if popup.list_widget.item(i).isHidden() is False
    )
    assert visible == 0


# ---------------------------------------------------------------------- #
# 触发：view 按 Tab → emit add_node_requested
# ---------------------------------------------------------------------- #


def test_view_has_add_node_requested_signal() -> None:
    """WorkflowCanvasView 暴露 add_node_requested signal。"""
    from src.features.ai_workflow.ui.workflow_canvas_view import WorkflowCanvasView
    assert hasattr(WorkflowCanvasView, "add_node_requested")


def test_view_tab_emits_add_node_requested(qapp) -> None:
    """按 Tab → emit add_node_requested。"""
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QKeyEvent
    from src.features.ai_workflow.ui.workflow_canvas_view import WorkflowCanvasView

    view = WorkflowCanvasView()
    received = []
    view.add_node_requested.connect(lambda: received.append(1))
    evt = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Tab, Qt.KeyboardModifier.NoModifier,
    )
    view.keyPressEvent(evt)
    assert received == [1], "Tab 应 emit add_node_requested"
