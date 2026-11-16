"""U05 / B06：原生信息栏的字段级元数据编辑（混合值不写、失败不假成功）。"""
from __future__ import annotations

from PyQt6.QtCore import QItemSelectionModel, Qt

from src.app.ui.native_files_panel import NativeFilesPanel


def _entries():
    return [
        {"name": "A.mov", "path": "/m/A.mov", "type": "video", "size_bytes": 1024,
         "rating": 3, "tags": "采访, 夜景", "comments": "主镜头",
         "duration": 65.0, "width": 1920, "height": 1080, "codec": "ProRes 422"},
        {"name": "B.mov", "path": "/m/B.mov", "type": "video", "size_bytes": 2048,
         "rating": 5, "tags": "空镜", "comments": "备用"},
    ]


def _panel(qtbot):
    panel = NativeFilesPanel()
    qtbot.addWidget(panel)
    panel.set_entries("/m", _entries())
    return panel


def _select(panel, *names: str) -> None:
    """按显示名选行；首行 ClearAndSelect，其余叠加 Select（整行）。"""
    panel.selection.clearSelection()
    for position, name in enumerate(names):
        for row in range(panel.proxy.rowCount()):
            index = panel.proxy.index(row, 0)
            if index.data(Qt.ItemDataRole.DisplayRole) != name:
                continue
            flag = (QItemSelectionModel.SelectionFlag.ClearAndSelect
                    if position == 0 else QItemSelectionModel.SelectionFlag.Select)
            panel.selection.select(index, flag | QItemSelectionModel.SelectionFlag.Rows)
            break


def test_single_selection_fills_metadata_and_technical_info(qtbot):
    panel = _panel(qtbot)
    _select(panel, "A.mov")
    assert panel.rating_combo.currentData() == 3
    assert panel.tag_edit.text() == "采访,夜景"  # tag_text 统一为无空格分隔
    assert panel.comment_edit.text() == "主镜头"
    assert panel.info_fields["resolution"].text() == "1920×1080"  # TechnicalMetadata.resolution 用紧凑写法
    assert panel.info_fields["codec"].text() == "ProRes 422"
    assert "1:05" in panel.info_fields["duration"].text() or "65" in panel.info_fields["duration"].text()


def test_rating_change_sends_only_that_field(qtbot):
    panel = _panel(qtbot)
    _select(panel, "A.mov")
    with qtbot.waitSignal(panel.metadata_patch_requested) as received:
        panel.rating_combo.setCurrentIndex(panel.rating_combo.findData(5))
    assert received.args[0] == ["/m/A.mov"]
    assert received.args[1] == {"rating": 5}  # tags/comments 不得顺带写回
    assert "保存" in panel.meta_status.text()


def test_applied_patch_updates_model_and_display(qtbot):
    panel = _panel(qtbot)
    _select(panel, "A.mov")
    panel.rating_combo.setCurrentIndex(panel.rating_combo.findData(5))
    panel.apply_metadata_result(["/m/A.mov"], {"rating": 5}, "已保存")
    entry = panel.model.entries[panel._entry_rows["/m/A.mov"]]
    assert entry.user.rating == 5
    star_row = panel.model.index(panel._entry_rows["/m/A.mov"], 4).data()
    assert star_row == "★★★★★"


def test_mixed_values_show_mixed_and_do_not_clear_tags(qtbot):
    panel = _panel(qtbot)
    _select(panel, "A.mov", "B.mov")
    assert panel.rating_combo.currentData() == -1
    assert panel.tag_edit.text() == ""
    assert "混合" in panel.tag_edit.placeholderText()

    emitted: list[tuple] = []
    panel.metadata_patch_requested.connect(lambda *args: emitted.append(args))
    panel._tags_committed()  # 空输入 + 混合值 = 用户没有表达"清空"
    assert emitted == []

    with qtbot.waitSignal(panel.metadata_patch_requested) as received:
        panel.rating_combo.setCurrentIndex(panel.rating_combo.findData(2))
    assert sorted(received.args[0]) == ["/m/A.mov", "/m/B.mov"]
    assert received.args[1] == {"rating": 2}


def test_failed_patch_relabels_and_keeps_old_values(qtbot):
    panel = _panel(qtbot)
    _select(panel, "A.mov")
    panel.rating_combo.setCurrentIndex(panel.rating_combo.findData(5))
    panel.metadata_failed(["/m/A.mov"], {"rating": 5}, "只读挂载")
    assert "只读挂载" in panel.meta_status.text()
    entry = panel.model.entries[panel._entry_rows["/m/A.mov"]]
    assert entry.user.rating == 3  # 未确认的写入不得落到模型
    assert panel.rating_combo.currentData() == 3


def test_multi_selection_aggregates_real_totals(qtbot):
    panel = _panel(qtbot)
    _select(panel, "A.mov", "B.mov")
    assert "2" in panel.info.text()
    assert "3.0 KB" in panel.info_path.text() or "3072" in panel.info_path.text()
    assert panel.info_fields["type"].text().endswith("种类型")
