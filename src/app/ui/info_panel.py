from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QMenu)
from src.shared.i18n.i18n import I18N
from PyQt6.QtCore import Qt

class InfoPanel(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setStyleSheet("InfoPanel { background: #27272b; }")

        # 默认显示的参数
        self.default_keys = {
            "camera", "lens", "focal_length", "iso", "aperture",
            "shutter", "resolution", "manufacturer", "size"
        }
        # 可选显示的参数
        self.optional_keys = {
            "exp_comp", "white_balance", "style", "color_space", "date"
        }
        # 当前可见的参数（默认显示的加上用户选择显示的）
        self.visible_keys = set(self.default_keys)

        # Content Scroll Area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("background: transparent; border: none;")
        scroll.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        scroll.customContextMenuRequested.connect(self.show_context_menu)

        self.content_widget = QWidget()
        self.content_widget.setStyleSheet("background: #27272b;")
        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(12, 10, 12, 10)
        self.content_layout.setSpacing(5)
        self.content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.info_labels = {}
        self.info_rows = {}  # 保存行引用用于显示/隐藏

        # 所有可用参数及其翻译
        self.all_info_keys = [
            ("camera", I18N.tr("info.camera", "相机")),
            ("lens", I18N.tr("info.lens", "镜头")),
            ("focal_length", I18N.tr("info.focal_length", "焦距")),
            ("iso", I18N.tr("info.iso", "ISO")),
            ("aperture", I18N.tr("info.aperture", "光圈")),
            ("shutter", I18N.tr("info.shutter", "快门")),
            ("exp_comp", I18N.tr("info.exp_comp", "曝光补偿")),
            ("white_balance", I18N.tr("info.white_balance", "白平衡")),
            ("style", I18N.tr("info.style", "风格")),
            ("color_space", I18N.tr("info.color_space", "色彩空间")),
            ("resolution", I18N.tr("info.resolution", "分辨率")),
            ("date", I18N.tr("info.date", "日期")),
            ("manufacturer", I18N.tr("info.manufacturer", "制造商")),
            ("size", I18N.tr("info.size", "大小"))
        ]

        for key, label_text in self.all_info_keys:
            row = QWidget()
            row.setStyleSheet("background: transparent;")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 2, 0, 2)

            label = QLabel(label_text)
            label.setStyleSheet("""
                color: #888;
                font-size: 11px;
                font-weight: bold;
                background: transparent;
            """)

            value = QLabel("-")
            value.setStyleSheet("""
                color: #ddd;
                font-size: 11px;
                font-family: 'Menlo', 'Monaco', 'Consolas', monospace;
                background: transparent;
            """)
            value.setAlignment(Qt.AlignmentFlag.AlignRight)

            row_layout.addWidget(label)
            row_layout.addStretch()
            row_layout.addWidget(value)

            self.content_layout.addWidget(row)
            self.info_labels[key] = value
            self.info_rows[key] = row

        # 初始只显示默认参数
        self.update_visible_rows()

        scroll.setWidget(self.content_widget)
        layout.addWidget(scroll)

    def update_visible_rows(self):
        """根据visible_keys更新行的显示/隐藏"""
        for key, row in self.info_rows.items():
            row.setVisible(key in self.visible_keys)

    def show_context_menu(self, pos):
        """显示右键菜单，让用户选择要显示的额外参数"""
        menu = QMenu(self)

        # 添加可选参数
        for key, label_text in self.all_info_keys:
            if key in self.optional_keys:
                action = menu.addAction(label_text)
                action.setCheckable(True)
                action.setChecked(key in self.visible_keys)
                action.triggered.connect(lambda checked, k=key: self.toggle_field(k))

        menu.exec(self.mapToGlobal(pos))

    def toggle_field(self, key: str):
        """切换字段的显示状态"""
        if key in self.visible_keys:
            self.visible_keys.remove(key)
        else:
            self.visible_keys.add(key)
        self.update_visible_rows()

    # Mapping from get_metadata() keys (Title Case) to info_panel keys (snake_case)
    _KEY_MAP: dict[str, str] = {
        "Camera": "camera",
        "Lens": "lens",
        "Focal Length": "focal_length",
        "ISO": "iso",
        "Aperture": "aperture",
        "Shutter": "shutter",
        "Exp. Comp.": "exp_comp",
        "White Balance": "white_balance",
        "Style": "style",
        "Color Space": "color_space",
        "Resolution": "resolution",
        "Date": "date",
        "Manufacturer": "manufacturer",
        "Size": "size",
        # Also support direct snake_case keys (for rawpy fallback)
        "camera": "camera",
        "lens": "lens",
        "focal_length": "focal_length",
        "iso": "iso",
        "aperture": "aperture",
        "shutter": "shutter",
        "exp_comp": "exp_comp",
        "white_balance": "white_balance",
        "style": "style",
        "color_space": "color_space",
        "resolution": "resolution",
        "date": "date",
        "manufacturer": "manufacturer",
        "size": "size",
    }

    def update_metadata(self, metadata: dict) -> None:
        """Updates the metadata display with new information.

        Accepts keys from FileIO.get_metadata() (Title Case) and maps them
        to the panel's internal snake_case keys.

        [AUD-3805] 缺失字段一律重置为 "-"：从有镜头/ISO 信息的图 A 切到
        无这些字段的图 B（RAW 无 Lens EXIF、rawpy 回退部分键缺失）时，
        此前只更新新元数据中出现的键，面板残留图 A 的旧值向用户展示
        错误的拍摄参数；空 metadata 也用于清空面板。
        """
        # Normalize keys: map Title Case → snake_case
        normalized: dict[str, str] = {}
        if metadata:
            for src_key, value in metadata.items():
                panel_key = self._KEY_MAP.get(src_key)
                if panel_key:
                    normalized[panel_key] = str(value)
        for key, label in self.info_labels.items():
            label.setText(normalized.get(key, "-"))
