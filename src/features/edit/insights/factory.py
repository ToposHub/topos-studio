"""T02 · 惰性窗口 factory：重复打开不泄漏窗口/worker。

G1 菜单回调只调 :func:`open_insights_window`——首次创建窗口（连带
worker/词条加载），再次调用前置/激活既有实例；窗口关闭即注销登记，
下次打开重新创建。无全局启动成本（不 import 时零开销）。
"""

from __future__ import annotations

from PyQt6.QtWidgets import QWidget

from src.features.edit.insights.runtime import InsightsConfig
from src.features.edit.insights.window import InsightsWindow

_current: InsightsWindow | None = None


def _forget() -> None:
    global _current
    _current = None


def current_window() -> InsightsWindow | None:
    """当前已打开的洞察窗口（无则 None；供宿主在工程切换时刷新/关闭）。"""
    return _current


def open_insights_window(
    host: object,
    config: InsightsConfig | None = None,
    parent: QWidget | None = None,
) -> InsightsWindow:
    """打开（或激活）唯一洞察窗口。宿主菜单回调的唯一入口。"""
    global _current
    if _current is not None:
        _current.raise_()
        _current.activateWindow()
        return _current
    window = InsightsWindow(host, config, parent=parent)
    _current = window
    window.closed.connect(_forget)
    window.show()
    return window


def shutdown_insights_window() -> None:
    """宿主在工程关闭/切换时调用：停 worker、关窗口（不回写 UI）。"""
    global _current
    if _current is None:
        return
    window = _current
    _current = None
    window.shutdown()
    window.close()
