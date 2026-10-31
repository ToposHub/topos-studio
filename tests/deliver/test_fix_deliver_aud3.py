"""Regression tests for AUD-12200 (batch-A deliver UI fix, machine w3-g7).

Custom Range 的 "To End" 哨兵值 -1 不得被 QSpinBox 钳位为 0：
修复前 ``setRange(0, ...)`` 使 ``setValue(-1)`` 被钳位到 0，
Custom 范围默认导出 [0, 0) —— 0 帧空文件且界面提示导出成功。

修复采用 Qt 哨兵模式：``minimum=-1`` + ``specialValueText="To End"``，
``get_export_config`` 将负值统一映射为 -1，由 TimelineExportConfig
解析为"导出到时间线末尾"（end_frame < 0 → duration）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtWidgets import QApplication

from src.features.deliver.ui.deliver_page import DeliverPage
from src.features.edit.core.timeline_export import ExportRangeType


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _make_page_with_custom_range(qapp) -> DeliverPage:
    page = DeliverPage()
    idx = page._range_combo.findData(ExportRangeType.CUSTOM)
    assert idx >= 0, "Custom Range 选项必须存在（edit feature 可用）"
    page._range_combo.setCurrentIndex(idx)
    page._sequence = SimpleNamespace(timeline_id="timeline-1")
    page._path_input.setText("/tmp/topos_aud12200_out.mov")
    return page


def test_end_frame_spin_keeps_to_end_sentinel(qapp) -> None:
    """哨兵 -1 不被钳位为 0，且 minimum 时显示 specialValueText。"""
    page = DeliverPage()

    assert page._end_frame_spin.minimum() == -1
    assert page._end_frame_spin.value() == -1  # 修复前被钳位为 0
    assert page._end_frame_spin.specialValueText()  # "To End" 哨兵文案存在


def test_custom_range_default_exports_to_end(qapp) -> None:
    """默认 Custom 范围（End 显示 To End）必须导出完整尾部。"""
    page = _make_page_with_custom_range(qapp)

    config = page.get_export_config()

    assert config is not None
    assert config.range_type == ExportRangeType.CUSTOM
    assert config.start_frame == 0
    # 哨兵语义：-1 传给 TimelineExportConfig → 导出至时间线末尾
    assert config.end_frame == -1


def test_custom_range_explicit_end_frame(qapp) -> None:
    """用户显式输入终止帧时按字面值传递。"""
    page = _make_page_with_custom_range(qapp)

    page._end_frame_spin.setValue(120)
    config = page.get_export_config()

    assert config is not None
    assert config.end_frame == 120

    page._start_frame_spin.setValue(10)
    config = page.get_export_config()
    assert config is not None
    assert config.start_frame == 10
    assert config.end_frame == 120


def test_custom_range_user_can_return_to_to_end(qapp) -> None:
    """用户把 End 调回 minimum 时恢复 "To End" 哨兵语义。"""
    page = _make_page_with_custom_range(qapp)

    page._end_frame_spin.setValue(50)
    page._end_frame_spin.setValue(-1)  # 回到 minimum → "To End"

    assert page._end_frame_spin.value() == -1
    config = page.get_export_config()
    assert config is not None
    assert config.end_frame == -1
