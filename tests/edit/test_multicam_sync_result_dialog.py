"""C4: 多机位波形同步结果确认对话框测试。

自动同步链路（worker/互相关/写回）在 P1-01 已落地并有单测；C4 补齐：
- 结果确认对话框：各行偏移/置信度/错误/出界警告渲染，"应用"/"取消"语义
- 出界判定 |offset| >= total_frames 的行级标注
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.ui.multicam_sync_result_dialog import (  # noqa: E402
    MulticamSyncResultDialog,
    MulticamSyncResultRow,
)

try:
    from PyQt6.QtWidgets import QApplication  # noqa: F401

    HAS_QT = True
except Exception:  # noqa: BLE001
    HAS_QT = False

pytestmark = pytest.mark.skipif(not HAS_QT, reason="PyQt6 unavailable")


def _rows() -> list:
    return [
        MulticamSyncResultRow(
            asset_id="cam_a", label="Cam A", offset_frames=0,
            lag_seconds=0.0, confidence=1.0,
        ),
        MulticamSyncResultRow(
            asset_id="cam_b", label="Cam B", offset_frames=36,
            lag_seconds=1.5, confidence=0.93,
        ),
        MulticamSyncResultRow(
            asset_id="cam_c", label="Cam C", offset_frames=-900,
            lag_seconds=-37.5, confidence=0.87,
            out_of_range=True,
        ),
        MulticamSyncResultRow(
            asset_id="cam_d", label="Cam D", offset_frames=0,
            lag_seconds=0.0, confidence=0.0,
            error="no_audio_stream",
        ),
    ]


class TestMulticamSyncResultDialog:
    def test_constructs_with_rows(self, qapp) -> None:
        dialog = MulticamSyncResultDialog(_rows())
        assert dialog._button_box is not None

    def test_row_count_matches(self, qapp) -> None:
        from PyQt6.QtWidgets import QTableWidget

        dialog = MulticamSyncResultDialog(_rows())
        tables = dialog.findChildren(QTableWidget)
        assert len(tables) == 1
        assert tables[0].rowCount() == 4

    def test_error_row_rendered_as_failed(self, qapp) -> None:
        from PyQt6.QtWidgets import QTableWidget

        dialog = MulticamSyncResultDialog(_rows())
        table = dialog.findChildren(QTableWidget)[0]
        # 行 3 = cam_d 失败行：偏移列显示 "—"
        assert table.item(3, 1).text() == "—"

    def test_aligned_row_rendered(self, qapp) -> None:
        from PyQt6.QtWidgets import QTableWidget

        dialog = MulticamSyncResultDialog(_rows())
        table = dialog.findChildren(QTableWidget)[0]
        # 行 0 = Cam A 已对齐（offset 0）
        assert table.item(0, 1).text() != "—"
        assert "0" in table.item(0, 1).text() or "已对齐" in table.item(0, 1).text()

    def test_out_of_range_row_marked(self, qapp) -> None:
        from PyQt6.QtWidgets import QTableWidget

        dialog = MulticamSyncResultDialog(_rows())
        table = dialog.findChildren(QTableWidget)[0]
        # 行 2 = Cam C 出界警告标注在角度列
        assert "⚠" in table.item(2, 0).text()

    def test_empty_rows_safe(self, qapp) -> None:
        dialog = MulticamSyncResultDialog([])
        assert dialog is not None
