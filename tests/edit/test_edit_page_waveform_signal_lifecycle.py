"""T-P1-71 回归：EditPage 析构不得在全局波形单例上留下悬空连接。

历史缺陷（全套件在 test_audio_waveform 处 SIGSEGV 的最小复现链）：

    EditPage.__init__ 曾连接
        WaveformExtractor.instance().status_changed.connect(
            self.waveform_status_message.emit)
    signal→.emit 的 C 函数槽不带接收者上下文——EditPage 被销毁后连接
    悬空，下一次单例 emit 直接调用已删除对象的 bound signal emit，
    原生层段错误（.ips 栈：pyqtBoundSignal_emit → PyQtSlotProxy → 悬空）。

修复：改为 signal→signal 连接（PyQt 注册接收者上下文，析构自动断开）。

本测试在单进程内断言：页面销毁后单例 emit 安全（连接已被 Qt 断开）。
若回归（改回 .emit 连接），本测试进程将以 SIGSEGV 方式失败——
这是该缺陷类唯一可靠的失败信号。
"""

from __future__ import annotations

import gc

import pytest

pytestmark = pytest.mark.edit_ui


def test_edit_page_destruction_disconnects_waveform_status(qapp) -> None:
    from PyQt6 import sip

    from src.features.edit.core.audio_waveform import WaveformExtractor
    from src.features.edit.ui.edit_page import EditPage

    singleton = WaveformExtractor.instance()
    assert not sip.isdeleted(singleton)

    page = EditPage()
    assert page.waveform_status_message is not None
    # 页面销毁（模拟测试套件中 page 局部变量被 GC 的真实时序）
    page_id = id(page)
    page.deleteLater()
    del page
    gc.collect()
    qapp.processEvents()

    # 单例仍然健康；emit 不得触达已删除页面的槽（悬空连接 = SIGSEGV）
    assert not sip.isdeleted(singleton)
    singleton.status_changed.emit("回归探测", 0)
    qapp.processEvents()
    assert page_id  # 仅保持 page_id 引用语义完整


def test_waveform_singleton_survives_emit_without_receivers(qapp) -> None:
    """无任何接收者时单例 emit 必须安全（基线健康检查）。"""
    from src.features.edit.core.audio_waveform import WaveformExtractor

    singleton = WaveformExtractor.instance()
    seen: list[tuple[str, int]] = []

    def _receive(message: str, timeout_ms: int) -> None:
        seen.append((message, timeout_ms))

    singleton.status_changed.connect(_receive)
    singleton.status_changed.emit("直接槽", 0)
    assert seen[-1] == ("直接槽", 0)
    singleton.status_changed.disconnect(_receive)
