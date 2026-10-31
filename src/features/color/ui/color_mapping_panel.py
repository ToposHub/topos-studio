"""Combined Color Mapping panel.

The panel groups the three color-remapping tools behind a single tab bar while
keeping each tool's existing widget hierarchy and behavior unchanged.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QTabWidget, QVBoxLayout, QWidget

from src.shared.i18n.i18n import I18N


class ColorMappingPanel(QWidget):
    """Container for HSL Curves, Warper, and Mixer panels.

    The child panels are supplied by the main window so existing controller
    references and signal connections continue to use the original panel
    instances.  This class only owns the outer tab navigation.
    """

    def __init__(
        self,
        hsl_curves_panel: QWidget,
        warper_panel: QWidget,
        mixer_panel: QWidget,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self.hsl_curves_panel = hsl_curves_panel
        self.warper_panel = warper_panel
        self.mixer_panel = mixer_panel

        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("color_mapping_tabs")
        self.tabs.setStyleSheet(
            """
            QTabBar::tab {
                padding: 5px 15px;
                background: #222;
                color: #888;
            }
            QTabBar::tab:selected {
                background: #333;
                color: #cc6600;
            }
            """
        )
        self.tabs.addTab(
            self.hsl_curves_panel,
            I18N.tr("panel.hsl_curves", "HSL Curves"),
        )
        self.tabs.addTab(
            self.warper_panel,
            I18N.tr("panel.warper", "Warper"),
        )
        self.tabs.addTab(
            self.mixer_panel,
            I18N.tr("panel.mixer", "Mixer"),
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.tabs)


__all__ = ["ColorMappingPanel"]
