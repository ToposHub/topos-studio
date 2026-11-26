"""The Color, Edit, and Mixer effect panels share the same scrollbar geometry."""

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtWidgets import QStyle, QStyleOptionSlider
from pytestqt.qtbot import QtBot

from src.features.color.ui.effects_browser_panel import ColorEffectsBrowser
from src.features.edit.ui.edit_effects_panel import EditEffectsPanel
from src.features.mixer.ui.effects_panel import MixerEffectsPanel


@pytest.mark.parametrize("panel_type", [ColorEffectsBrowser, EditEffectsPanel, MixerEffectsPanel])
def test_effects_scrollbar_is_flush_and_the_thumb_reaches_the_end(
    panel_type: type[ColorEffectsBrowser | EditEffectsPanel | MixerEffectsPanel], qtbot: QtBot,
) -> None:
    panel = panel_type()
    qtbot.addWidget(panel)
    if isinstance(panel, ColorEffectsBrowser):
        panel.set_resources([{"id": f"effect_{i}"} for i in range(40)])
        view = panel.resource_list
    elif isinstance(panel, EditEffectsPanel):
        view = panel.video_effects_tree
    else:
        view = panel.tree
    panel.resize(520 if isinstance(panel, EditEffectsPanel) else 300, 500)
    panel.show()
    bar = view.verticalScrollBar()
    qtbot.waitUntil(lambda: bar.isVisible() and bar.maximum() > 0)

    assert bar.mapTo(panel, bar.rect().topRight()).x() == panel.rect().right()
    assert bar.width() == 6
    option = QStyleOptionSlider()
    bar.initStyleOption(option)
    thumb = bar.style().subControlRect(
        QStyle.ComplexControl.CC_ScrollBar, option, QStyle.SubControl.SC_ScrollBarSlider, bar,
    )
    assert 166 <= thumb.height() <= 168

    qtbot.keyClick(bar, Qt.Key.Key_PageDown)
    assert bar.value() == min(bar.pageStep(), bar.maximum())
    bar.setValue(bar.minimum())
    end = QPoint(thumb.center().x(), bar.height() - thumb.height() // 2)
    qtbot.mousePress(bar, Qt.MouseButton.LeftButton, pos=thumb.center())
    qtbot.mouseMove(bar, end)
    qtbot.mouseRelease(bar, Qt.MouseButton.LeftButton, pos=end)
    assert bar.value() == bar.maximum()
