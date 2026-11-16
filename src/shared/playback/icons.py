"""播放器图标助手 —— 与主程序剪辑页时间线工具栏同风格。

图标走 Font Awesome（qtawesome）：嵌入模式复用 src.shared.assets.icon_manager，
独立运行直接用 qtawesome，两者都缺时由调用方回退文本 emoji。
按钮样式与 timeline_panel.py 的 tool_btn_style 一致：透明底、hover 微亮、无底框。
"""
from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import QToolButton

GRAY = "#aaaaaa"    # 默认灰（同 IconColors.DEFAULT）
WHITE = "#ffffff"
ORANGE = "#ff8800"  # 主题橙（同 IconColors.ORANGE）

TOOL_BTN_STYLE = """
    QToolButton {
        background: transparent; border: none; border-radius: 4px;
        padding: 3px; margin: 1px;
    }
    QToolButton:hover { background-color: rgba(255, 255, 255, 18); }
    QToolButton:pressed { background-color: rgba(255, 255, 255, 10); }
"""


def icon(name: str, color: str = GRAY):
    """Font Awesome 图标；qtawesome 不可用时返回 None（调用方回退文本）。"""
    try:
        from src.shared.assets.icon_manager import get_icon as _get
        return _get(name, color=color)
    except Exception:
        pass
    try:
        import qtawesome as qta
        return qta.icon(name, color=color)
    except Exception:
        return None


def make_tool_button(parent, name: str, tooltip: str, fallback: str = '',
                     size: int = 15, color: str = GRAY) -> QToolButton:
    """时间线工具栏同款 QToolButton：透明底 + hover 微亮 + 图标。"""
    btn = QToolButton(parent)
    ic = icon(name, color=color)
    if ic is not None:
        btn.setIcon(ic)
        btn.setIconSize(QSize(size, size))
    elif fallback:
        btn.setText(fallback)
    btn.setToolTip(tooltip)
    btn.setStyleSheet(TOOL_BTN_STYLE)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


def apply_tool_button(btn, size: int = 15) -> None:
    """把既有按钮统一成时间线工具栏样式。"""
    btn.setStyleSheet(TOOL_BTN_STYLE)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if not btn.icon().isNull():
        btn.setIconSize(QSize(size, size))


def set_play_icon(btn, playing: bool, size: int = 15) -> None:
    """播放/暂停按钮图标（qtawesome 缺失时回退 ▶/⏸ 文本）。"""
    ic = icon('fa5s.pause' if playing else 'fa5s.play', color=WHITE)
    if ic is not None:
        btn.setIcon(ic)
        btn.setIconSize(QSize(size, size))
        btn.setText('')
    else:
        btn.setText('⏸' if playing else '▶')
