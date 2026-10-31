import os
import shutil
import numpy as np
import logging

logger = logging.getLogger(__name__)
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QPushButton, QFileDialog, QListWidget, QListWidgetItem,
                             QDoubleSpinBox, QMenu)
from PyQt6.QtCore import Qt, pyqtSignal, QSize
from src.shared.ui.widgets.clickable_slider import ClickableSlider
from PyQt6.QtGui import QPixmap, QImage, QIcon
from typing import List, Optional
from src.shared.i18n.i18n import I18N
from src.shared.paths.paths import get_lut_dir
from src.shared.ui.style_sheet import LUT_TOOLBAR_LABEL_STYLE, LUT_LIST_STYLE


class LutGallery(QWidget):
    lut_selected = pyqtSignal(str)
    lut_removed = pyqtSignal(str)
    decompile_requested = pyqtSignal(str)
    intensity_changed = pyqtSignal(float)
    lut_reset = pyqtSignal()
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()

    # Default card size constants
    DEFAULT_ICON_WIDTH = 110
    DEFAULT_ICON_HEIGHT = 80
    DEFAULT_ITEM_SIZE = 120

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.lut_dir: str = get_lut_dir()
        if not os.path.exists(self.lut_dir):
            os.makedirs(self.lut_dir)

        self.base_thumb: np.ndarray = self._create_base_thumbnail()
        self.items: List[QListWidgetItem] = []
        self._intensity_updating: bool = False
        self.init_ui()
        self.scan_luts()

    def init_ui(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(5)
        self.setLayout(layout)

        # Toolbar Row 1: Buttons
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(5)

        self.add_btn = QPushButton(I18N.tr("lut.import", "导入 LUT"))
        self.add_btn.setObjectName("gradingApplyBtn")
        self.add_btn.clicked.connect(self.import_lut)
        btn_layout.addWidget(self.add_btn)

        # Decompile button: hidden but functionality preserved via right-click menu
        self.decompile_btn = QPushButton(I18N.tr("lut.decompile", "反编译"))
        self.decompile_btn.setToolTip(I18N.tr("tooltip.lut_decompile", "将选定的 LUT 反编译为调色参数"))
        self.decompile_btn.clicked.connect(self.on_decompile_click)
        self.decompile_btn.setVisible(False)
        btn_layout.addWidget(self.decompile_btn)

        self.reset_lut_btn = QPushButton(I18N.tr("lut.reset", "重置"))
        self.reset_lut_btn.clicked.connect(self._on_reset_clicked)
        btn_layout.addWidget(self.reset_lut_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # Toolbar Row 2: Intensity Slider + SpinBox + Zoom Slider
        controls_layout = QHBoxLayout()
        controls_layout.setSpacing(10)

        # Intensity
        intensity_label = QLabel(I18N.tr("lut.intensity", "强度:"))
        intensity_label.setStyleSheet(LUT_TOOLBAR_LABEL_STYLE)
        controls_layout.addWidget(intensity_label)

        self.intensity_slider = ClickableSlider(Qt.Orientation.Horizontal, default_val=100)
        self.intensity_slider.setRange(0, 100)
        self.intensity_slider.setValue(100)
        self.intensity_slider.setFixedHeight(18)
        self.intensity_slider.valueChanged.connect(self._on_intensity_slider_changed)
        self.intensity_slider.sliderPressed.connect(self.interaction_started.emit)
        self.intensity_slider.sliderReleased.connect(self.interaction_ended.emit)
        controls_layout.addWidget(self.intensity_slider)

        # Intensity SpinBox (0.0 - 1.0, supports manual input)
        self.intensity_spin = QDoubleSpinBox()
        self.intensity_spin.setRange(0.0, 1.0)
        self.intensity_spin.setDecimals(2)
        self.intensity_spin.setSingleStep(0.01)
        self.intensity_spin.setValue(1.0)
        self.intensity_spin.setFixedWidth(55)
        self.intensity_spin.setMinimumHeight(18)
        # 使用全局 SpinBox 样式（由 DARK_THEME 统一管理）
        self.intensity_spin.valueChanged.connect(self._on_intensity_spin_changed)
        controls_layout.addWidget(self.intensity_spin)

        # Zoom (placed right after Intensity): column-count model — the
        # slider picks how many cards tile the row, card width follows the
        # live viewport width (flush right, no dead strip).
        zoom_label = QLabel(I18N.tr("lut.columns", "列数:"))
        zoom_label.setStyleSheet(LUT_TOOLBAR_LABEL_STYLE)
        controls_layout.addWidget(zoom_label)

        self.zoom_slider = ClickableSlider(Qt.Orientation.Horizontal, default_val=4)
        self.zoom_slider.setFixedHeight(18)
        self.zoom_slider.setMaximumWidth(100)  # Limit width for zoom slider
        controls_layout.addWidget(self.zoom_slider)

        self.zoom_value_label = QLabel("4")
        self.zoom_value_label.setStyleSheet(f"{LUT_TOOLBAR_LABEL_STYLE} min-width: 20px;")
        self.zoom_value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        controls_layout.addWidget(self.zoom_value_label)

        layout.addLayout(controls_layout)

        # List Widget for Gallery
        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListWidget.ViewMode.IconMode)
        self.list_widget.setIconSize(QSize(110, 80))
        self.list_widget.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list_widget.setSpacing(10)
        self.list_widget.setMovement(QListWidget.Movement.Static)
        self.list_widget.setStyleSheet(LUT_LIST_STYLE)
        self.list_widget.itemClicked.connect(self._on_item_clicked)
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self.show_context_menu)
        # Column-count zoom: LUT thumbs are 110x80 (11:8) with a caption
        # line under the icon; the controller derives card size from the
        # viewport so every row tiles flush with the right edge.
        from src.shared.ui.column_grid import ColumnGridController
        self._column_grid = ColumnGridController(
            self.list_widget, slider=self.zoom_slider,
            settings_key="lut_columns", columns_default=4,
            gap=6, aspect=80.0 / 110.0, extra_height=30,
            on_columns_applied=self._on_columns_applied)

        layout.addWidget(self.list_widget)

    def _on_reset_clicked(self) -> None:
        """Reset button: rescan LUT directory + emit reset signal + clear UI state."""
        self.rescan_luts()
        self._intensity_updating = True
        self.intensity_slider.setValue(100)
        self.intensity_spin.setValue(1.0)
        self._intensity_updating = False
        self.list_widget.clearSelection()
        self.lut_reset.emit()

    def rescan_luts(self) -> None:
        """Clear the gallery and rescan the luts directory for new files."""
        self.list_widget.clear()
        self.scan_luts()

    def show_context_menu(self, pos) -> None:
        item = self.list_widget.itemAt(pos)
        if not item:
            return

        from src.shared.i18n.i18n import I18N

        menu = QMenu(self)
        path: str = item.data(Qt.ItemDataRole.UserRole)

        apply_act = menu.addAction(I18N.tr("lut.apply", "Apply LUT"))
        apply_act.triggered.connect(lambda: self.lut_selected.emit(path))

        decompile_act = menu.addAction(I18N.tr("lut.smart_decompile", "Smart Decompile (逆向解析)"))
        decompile_act.triggered.connect(lambda: self.decompile_requested.emit(path))

        menu.addSeparator()
        delete_act = menu.addAction(I18N.tr("lut.remove", "Remove from Gallery"))
        delete_act.triggered.connect(lambda: self.remove_item(item))

        menu.exec(self.list_widget.mapToGlobal(pos))

    def remove_item(self, item: QListWidgetItem) -> None:
        path: str = item.data(Qt.ItemDataRole.UserRole)
        self.list_widget.takeItem(self.list_widget.row(item))
        self.lut_removed.emit(path)

    def on_decompile_click(self) -> None:
        """Decompile via hidden button (preserved for programmatic use)."""
        item = self.list_widget.currentItem()
        if item:
            path: str = item.data(Qt.ItemDataRole.UserRole)
            self.decompile_requested.emit(path)
        else:
            # If nothing selected, prompt for file
            file_path, _ = QFileDialog.getOpenFileName(
                self, "Select LUT to Decompile", "", "Cube Files (*.cube)"
            )
            if file_path:
                self.decompile_requested.emit(file_path)

    def _create_base_thumbnail(self) -> np.ndarray:
        """Create a colorful gradient thumbnail."""
        thumb = np.zeros((80, 110, 3), dtype=np.uint8)
        for i in range(110):
            thumb[:, i, 0] = int(i / 110.0 * 255)  # R
            thumb[:, i, 1] = 128  # G
        for j in range(80):
            thumb[j, :, 2] = int(j / 80.0 * 255)  # B
        return thumb

    def scan_luts(self) -> None:
        """Scans the luts directory and loads existing cube files."""
        if not os.path.exists(self.lut_dir):
            return
        for file in os.listdir(self.lut_dir):
            if file.lower().endswith((".cube", ".3dl")):
                path: str = os.path.join(self.lut_dir, file)
                self.add_lut_item(path, copy_to_local=False)

    def import_lut(self) -> None:
        file_paths, _ = QFileDialog.getOpenFileNames(
            self, "Import 3D LUTs", "",
            "3D LUTs (*.cube *.3dl);;Cube Files (*.cube);;Lustre 3DL (*.3dl)"
        )
        for path in file_paths:
            self.add_lut_item(path, copy_to_local=True)

    def add_lut_item(self, path: str, copy_to_local: bool = True) -> None:
        name: str = os.path.basename(path)
        target_path: str = path

        if copy_to_local:
            target_path = os.path.join(self.lut_dir, name)
            if path != target_path:
                try:
                    base, ext = os.path.splitext(name)
                    suffix = 1
                    while os.path.exists(target_path):
                        target_path = os.path.join(self.lut_dir, f"{base}_{suffix}{ext}")
                        suffix += 1
                    shutil.copy2(path, target_path)
                    name = os.path.basename(target_path)
                except OSError as e:
                    # AUD-38308: 复制失败（源不可读/目标不可写/磁盘满等）时
                    # target_path 在磁盘上不存在——直接返回，不得把指向
                    # 不存在路径的条目加入图库（点击应用会静默失败）。
                    logger.error(f"Failed to copy LUT {path!r} to {target_path!r}: {e}")
                    return

        # Create Thumbnail Icon
        height, width, channel = self.base_thumb.shape
        qimg = QImage(self.base_thumb.data.tobytes(), width, height, 3 * width, QImage.Format.Format_RGB888)
        pixmap = QPixmap.fromImage(qimg)

        list_item = QListWidgetItem(QIcon(pixmap), name)
        list_item.setData(Qt.ItemDataRole.UserRole, target_path)
        list_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        # P1-COLOR-11: LUT metadata tooltip（title/尺寸/格式/描述）
        try:
            from src.shared.color.lut_loader import probe_lut_metadata
            meta = probe_lut_metadata(target_path)
            tip_parts = [name]
            if meta.get('title'):
                tip_parts.append(f"Title: {meta['title']}")
            if meta.get('size'):
                tip_parts.append(f"Size: {meta['size']}³")
            if meta.get('format'):
                tip_parts.append(f"Format: .{meta['format']}")
            if meta.get('description'):
                tip_parts.append(str(meta['description'])[:120])
            list_item.setToolTip('\n'.join(tip_parts))
        except Exception:
            pass

        self.list_widget.addItem(list_item)
        # Pin to the current column cell (fresh items default to 120x120).
        self._column_grid.pin_items()

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        path: str = item.data(Qt.ItemDataRole.UserRole)
        self.lut_selected.emit(path)

    def _on_intensity_slider_changed(self, val: int) -> None:
        """Slider changed → sync spinbox + emit signal (with anti-recursion lock)."""
        if self._intensity_updating:
            return
        self._intensity_updating = True
        float_val: float = val / 100.0
        self.intensity_spin.setValue(float_val)
        self._intensity_updating = False
        self.intensity_changed.emit(float_val)

    def _on_intensity_spin_changed(self, val: float) -> None:
        """Spinbox changed → sync slider + emit signal (with anti-recursion lock)."""
        if self._intensity_updating:
            return
        self._intensity_updating = True
        self.intensity_slider.setValue(int(val * 100))
        self._intensity_updating = False
        self.intensity_changed.emit(val)

    def _on_columns_applied(self, icon: QSize, cell: QSize) -> None:
        # Fires once during the controller's own construction (before
        # _column_grid is bound) — the label just keeps its default then.
        grid = getattr(self, "_column_grid", None)
        if grid is not None:
            self.zoom_value_label.setText(str(grid.columns))

    def _on_zoom_changed(self, val: int) -> None:
        """Legacy zoom hook — the slider now drives ColumnGridController."""
        self._column_grid.apply_columns(val)

    def set_selected_lut(self, path: Optional[str]) -> None:
        self.list_widget.blockSignals(True)
        self.list_widget.clearSelection()
        if path:
            for i in range(self.list_widget.count()):
                item = self.list_widget.item(i)
                if item.data(Qt.ItemDataRole.UserRole) == path:
                    item.setSelected(True)
                    self.list_widget.scrollToItem(item)
                    break
        self.list_widget.blockSignals(False)

    def set_intensity(self, val: float) -> None:
        """Set intensity from backend (0.0-1.0), updating both slider and spinbox."""
        self._intensity_updating = True
        self.intensity_slider.setValue(int(val * 100))
        self.intensity_spin.setValue(val)
        self._intensity_updating = False
