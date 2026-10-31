"""M21.2c · 多版本 QC 面板呈现回归。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _lufs_report():
    return {
        "/out/v1_main.mp4": {
            "input": NS(integrated_lufs=-16.2, audio_present=True),
            "error": "",
            "integrated_lufs": -16.2,
            "lufs_deviation": 0.3,
        },
        "/out/v2_web.mp4": {
            "input": None,
            "error": "测量失败（文件不可读？）",
        },
    }


def _subtitle_summary():
    return {
        "all_ok": False,
        "total_blocking": 2,
        "versions": [
            {"version_index": 0, "blocking": 0, "warning": 1, "ok": True},
            {"version_index": 1, "blocking": 2, "warning": 0, "ok": False},
        ],
    }


def test_panel_renders_per_version_rows(qapp):
    from src.features.deliver.ui.m21_multi_version_qc_panel import (
        MultiVersionQcPanel,
    )
    panel = MultiVersionQcPanel(_lufs_report(), _subtitle_summary())
    assert "2" in panel._summary_label.text() and "字幕阻塞" in (
        panel._summary_label.text())
    table = panel._table
    assert table.rowCount() == 2
    # 版本 1：实测 LUFS/偏差/有音轨/字幕通过
    row0 = [table.item(0, c).text() for c in range(7)]
    assert row0[0] == "v1_main.mp4"
    assert row0[1] == "-16.2" and row0[2] == "0.30"
    assert row0[3] == "有" and row0[4] == "0" and row0[6] == "✓"
    # 版本 2：测量失败显式标出（不冒充数值），字幕阻塞 2 → 判定 ✗
    row1 = [table.item(1, c).text() for c in range(7)]
    assert row1[1] == "测量失败"
    assert row1[4] == "2" and row1[6] == "✗"


def test_panel_empty_data_honest(qapp):
    from src.features.deliver.ui.m21_multi_version_qc_panel import (
        MultiVersionQcPanel,
    )
    panel = MultiVersionQcPanel()
    assert "无版本测量数据" in panel._summary_label.text()
    assert panel._table.rowCount() == 0


def test_panel_untested_lufs_not_faked(qapp):
    """input 有但 integrated_lufs=None → 未测（不显示 0.0）。"""
    from src.features.deliver.ui.m21_multi_version_qc_panel import (
        MultiVersionQcPanel,
    )
    report = {"/out/v3.mp4": {
        "input": NS(integrated_lufs=None, audio_present=False),
        "error": "",
    }}
    panel = MultiVersionQcPanel(report, {"versions": [
        {"version_index": 0, "blocking": 0, "warning": 0, "ok": True}]})
    assert panel._table.item(0, 1).text() == "未测"
    assert panel._table.item(0, 3).text() == "无"


def test_registry_opens_multi_version_qc(qapp):
    from src.shared.ui.upgrade_panel_actions import open_panel
    panel = open_panel("deliver.multi_version_qc",
                       lufs_report=_lufs_report(),
                       subtitle_summary=_subtitle_summary())
    from src.features.deliver.ui.m21_multi_version_qc_panel import (
        MultiVersionQcPanel,
    )
    assert isinstance(panel, MultiVersionQcPanel)
    assert panel._table.rowCount() == 2
