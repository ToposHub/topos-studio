"""The real Qt media chooser must show supported filenames from the registry."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtGui import QFileSystemModel
from PyQt6.QtWidgets import QFileDialog, QWidget
from pytestqt.qtbot import QtBot

from src.features.edit.ui.media_pool_panel import MediaPoolPanel
from src.shared.i18n.i18n import I18N
from src.shared.media.format_registry import (
    AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, SUBTITLE_EXTENSIONS, VIDEO_EXTENSIONS,
)


@pytest.mark.parametrize("filter_index,extensions", [
    (0, VIDEO_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS | SUBTITLE_EXTENSIONS),
    (1, VIDEO_EXTENSIONS),
    (2, IMAGE_EXTENSIONS),
    (3, AUDIO_EXTENSIONS),
    (4, SUBTITLE_EXTENSIONS),
])
def test_import_chooser_filters_supported_files_with_real_qt_model(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    filter_index: int, extensions: frozenset[str],
) -> None:
    all_extensions = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS | SUBTITLE_EXTENSIONS
    for extension in all_extensions | {".unsupported"}:
        (tmp_path / f"media{extension}").write_bytes(b"fixture")
    # Old translated complete filters must not override the registry's broader
    # patterns (e.g. MOV-only translation must still allow MP4/MXF/R3D).
    monkeypatch.setattr(I18N, "_translations", {"edit": {"media_pool": {
        "filter_video": "旧视频筛选 (*.mov)",
        "filter_all": "旧筛选 (*)",
        "import_filter_label_all": "全部媒体",
        "import_filter_label_video": "视频",
        "import_filter_label_image": "图像",
        "import_filter_label_audio": "音频",
        "import_filter_label_subtitle": "字幕",
    }}})
    importer = Mock()

    def choose_files(
        parent: QWidget | None, title: str, directory: str, name_filters: str,
    ) -> tuple[list[str], str]:
        dialog = QFileDialog(parent)
        qtbot.addWidget(dialog)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog)
        dialog.setFileMode(QFileDialog.FileMode.ExistingFiles)
        dialog.setDirectory(str(tmp_path))
        dialog.setNameFilters(name_filters.split(";;"))
        dialog.selectNameFilter(dialog.nameFilters()[filter_index])
        dialog.show()
        model = dialog.findChild(QFileSystemModel)
        assert model is not None
        root = model.index(str(tmp_path))

        def visible_files() -> set[str]:
            return {model.fileName(model.index(row, 0, root))
                    for row in range(model.rowCount(root))
                    if not model.isDir(model.index(row, 0, root))}

        expected = {f"media{extension}" for extension in extensions}
        qtbot.waitUntil(lambda: visible_files() == expected, timeout=5000)
        assert all(pattern.startswith("*.") for pattern in model.nameFilters())
        assert dialog.selectedNameFilter().startswith(("全部媒体", "视频", "图像", "音频", "字幕"))
        dialog.close()
        return [], ""

    monkeypatch.setattr(QFileDialog, "getOpenFileNames", choose_files)
    host = SimpleNamespace(_get_top_level_widget=lambda: None, _import_files=importer)

    MediaPoolPanel._on_import_clicked(host)

    importer.assert_not_called()
