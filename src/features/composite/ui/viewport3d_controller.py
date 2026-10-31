"""Viewport3D Controller — 3D 视口交互协调层（Phase 3）。

把 3D 视口的鼠标/键盘交互逻辑从 CompositeViewer 中分离出来，作为一个聚焦的
控制器层（对应 audit plan "Recommended 3D View Architecture"：
``viewport3d_controller.py``）。

职责：
- MMB click/drag 区分：click（位移 < 阈值）→ toggle 四视图（Phase 4 接线）；
  drag → 平移；Alt+MMB 永远平移（不触发四视图）。
- 3D 工具模式快捷键分发（Q/W/E/R → SELECT/MOVE/ROTATE/SCALE）。
- 工具模式 → gizmo 显隐的协调（Phase 5 完整 gizmo 后接线）。

设计约束（audit plan）：
- 不直接操作 ModernGL / 渲染；只读写 Viewport3DState + 发信号让 viewer 反应。
- 2D 工具（ViewerToolMode）不经过本控制器；它只服务 3D 模式。
- 纯视图操作不进 undo、不脏化节点图。

Phase 3 范围：数据层 + 控制器骨架 + MMB click/drag 区分逻辑（为 Phase 4 四视图
切换铺路）。完整的 3D 导航迁移（Alt+button orbit/pan/zoom）仍由 viewer 主持，
本控制器提供可调用的判定 helper，后续逐步迁入。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from PyQt6.QtCore import QObject, QPointF, Qt, pyqtSignal

from .viewport3d_state import (
    Viewport3DState, Viewer3DToolMode, TOOL3D_MODE_SHORTCUTS)

if TYPE_CHECKING:
    from PyQt6.QtGui import QMouseEvent, QKeyEvent


# MMB click vs drag 判定的位移阈值（像素）。小于此视为 click（toggle 四视图）。
MMB_CLICK_THRESHOLD_PX = 4.0


class Viewport3DController(QObject):
    """3D 视口交互控制器（持有 Viewport3DState，协调鼠标/键盘分发）。

    CompositeViewer 在 3D 模式下把鼠标/键盘事件委托给本控制器判定，由控制器
    更新 state 并发信号，viewer 据此更新 gizmo/重绘。

    Signals:
        tool_mode_changed(Viewer3DToolMode): 3D 工具模式变化（gizmo 显隐用）。
        quad_toggled(bool): 四视图布局切换（True=QUAD, False=SINGLE）。
        mmb_drag_started(): MMB 拖拽开始（viewer 据此进入 pan）。
    """

    # 工具模式变化（viewer 据 gizmo 类型显隐）
    tool_mode_changed = pyqtSignal(object)  # Viewer3DToolMode
    # 四视图布局切换（viewer 据此重绘视口分区）
    quad_toggled = pyqtSignal(bool)  # True=QUAD
    # MMB 拖拽开始（viewer 据此进入 pan 导航；click 则不触发，改为 toggle quad）
    mmb_drag_started = pyqtSignal()

    def __init__(self, state: Optional[Viewport3DState] = None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._state: Viewport3DState = state if state is not None else Viewport3DState()
        # MMB click/drag 判定跟踪
        self._mmb_press_pos: Optional[QPointF] = None  # MMB 按下位置（view px）
        self._mmb_is_drag: bool = False                # 已超过阈值（判定为 drag）

    # ── 状态访问 ──

    @property
    def state(self) -> Viewport3DState:
        """底层 3D 视口状态（工具模式/布局/组件/吸附）。"""
        return self._state

    def set_tool_mode(self, mode: Viewer3DToolMode) -> None:
        """切换 3D 工具模式（SELECT/MOVE/ROTATE/SCALE），发 tool_mode_changed。"""
        if self._state.tool_mode != mode:
            self._state.tool_mode = mode
            self.tool_mode_changed.emit(mode)

    def reset(self) -> None:
        """重置到默认视图状态（切回 2D 模式时调）。"""
        self._state.reset_tool()
        self._state.layout_mode = (
            self._state.layout_mode.__class__.SINGLE)
        self._mmb_press_pos = None
        self._mmb_is_drag = False

    # ── MMB click/drag 区分（Phase 4 四视图切换的前置）──

    def on_mmb_press(self, event: "QMouseEvent") -> None:
        """记录 MMB 按下位置（判定 click vs drag 的起点）。

        在 viewer 的 mousePressEvent 中、3D 模式 + MMB 时调用。
        Alt+MMB 不记录（永远走 pan，不触发四视图 toggle）。
        """
        if event.modifiers() & Qt.KeyboardModifier.AltModifier:
            self._mmb_press_pos = None  # Alt+MMB 永远 pan，不做 click 判定
            self._mmb_is_drag = True
            return
        self._mmb_press_pos = QPointF(event.position())
        self._mmb_is_drag = False

    def on_mmb_move(self, event: "QMouseEvent") -> bool:
        """MMB 移动：超过阈值则判定为 drag（返回 True → viewer 进入 pan）。

        返回 True 表示此次移动应触发 pan（viewer 据此平移相机）。
        首次超过阈值时发 mmb_drag_started（一次性）。
        """
        if self._mmb_press_pos is None:
            # Alt+MMB 或已判定为 drag → 直接 pan
            return self._mmb_is_drag
        dx = event.position().x() - self._mmb_press_pos.x()
        dy = event.position().y() - self._mmb_press_pos.y()
        if (dx * dx + dy * dy) > MMB_CLICK_THRESHOLD_PX * MMB_CLICK_THRESHOLD_PX:
            if not self._mmb_is_drag:
                self._mmb_is_drag = True
                self.mmb_drag_started.emit()
            return True
        return False  # 仍在阈值内（可能是 click），暂不 pan

    def on_mmb_release(self, event: "QMouseEvent") -> bool:
        """MMB 释放：若是 click（未超过阈值）→ toggle 四视图。返回是否触发了 toggle。

        返回 True 表示触发了四视图 toggle（viewer 据此重绘）；
        返回 False 表示是 drag 结束（viewer 继续原 pan 收尾）。
        """
        was_click = (self._mmb_press_pos is not None and not self._mmb_is_drag)
        self._mmb_press_pos = None
        self._mmb_is_drag = False
        if was_click:
            self._state.toggle_layout()
            self.quad_toggled.emit(self._state.is_quad())
            return True
        return False

    # ── 3D 工具模式快捷键 ──

    def handle_key(self, event: "QKeyEvent") -> bool:
        """处理 3D 工具模式快捷键（Q/W/E/R）。返回是否消费了该事件。

        在 viewer 的 keyPressEvent 中、3D 模式时优先调用。
        不消费（返回 False）则由 viewer 继续处理（如 Alt 导航、F 帧）。
        """
        # 仅响应无修饰键的单键（避免与 Ctrl+S 等冲突）
        if event.modifiers() != Qt.KeyboardModifier.NoModifier:
            return False
        key_text = event.text().upper()
        for mode, shortcut in TOOL3D_MODE_SHORTCUTS.items():
            if key_text == shortcut:
                self.set_tool_mode(mode)
                return True
        return False
