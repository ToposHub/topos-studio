"""The Edit effect library resolves labels at the active locale."""

from pathlib import Path

from PyQt6.QtWidgets import QTreeWidget
from pytest import MonkeyPatch
from pytestqt.qtbot import QtBot

from src.features.edit.ui.edit_effects_panel import EditEffectsPanel
from src.shared.i18n.i18n import I18N


def _cards(tree: QTreeWidget) -> dict[str, str]:
    labels = {}
    for index in range(tree.topLevelItemCount()):
        section = tree.itemWidget(tree.topLevelItem(index), 0)
        for card in section._cards:
            if not card.effect_type.startswith("motion_graphic_template:"):
                labels[card.effect_type] = card.text()
    return labels


def test_edit_effects_use_chinese_or_fall_back_to_english(
    qtbot: QtBot, monkeypatch: MonkeyPatch,
) -> None:
    for attribute in ("_current_locale", "_translations", "_fallback_translations", "_locales_dir"):
        monkeypatch.setattr(I18N, attribute, getattr(I18N, attribute))
    monkeypatch.setattr(I18N, "_module_locales_dirs", [])
    assert I18N.reload_locale("en_US")
    edit_locales = Path(__file__).resolve().parents[2] / "src" / "features" / "edit" / "locales"
    I18N.load_module_translations(str(edit_locales))

    def labels() -> tuple[list[str], dict[str, str], str, str]:
        panel = EditEffectsPanel()
        qtbot.addWidget(panel)
        tabs = [panel.tabs.tabText(index) for index in range(panel.tabs.count())]
        cards = {}
        for tree in (panel.video_effects_tree, panel.audio_effects_tree, panel.transitions_tree):
            cards.update(_cards(tree))
        title_section = panel.caption_effects_tree.topLevelItem(0)
        return tabs, cards, title_section.text(0), title_section.child(0).text(0)

    english = labels()
    assert english[0] == ["Effects", "Transitions", "Text", "Audio", "Subtitles"]
    assert english[1]["plugin:Film_Grain"] == "Film Grain"
    assert english[1]["dynamics"] == "Dynamics"
    assert english[1]["cross_dissolve"] == "Cross Dissolve"
    assert english[2:] == ("Caption Effects", "Fade In and Out")

    assert I18N.reload_locale("zh_CN")
    tabs, chinese_cards, title_group, title_template = labels()
    assert tabs == ["特效", "转场", "文字", "音频", "字幕"]
    assert chinese_cards.keys() == english[1].keys()
    assert chinese_cards["plugin:Film_Grain"] == "胶片颗粒"
    assert chinese_cards["plugin:Color_Space_Transform"] == "色彩空间转换器"
    assert chinese_cards["strip_eq"] == "参数化 EQ"
    assert chinese_cards["dynamics"] == "动态处理"
    assert chinese_cards["cross_dissolve"] == "交叉溶解"
    assert chinese_cards["audio:constant_power"] == "恒功率交叉淡化"
    assert (title_group, title_template) == ("字幕特效", "淡入淡出")
    assert all(any("\u4e00" <= char <= "\u9fff" for char in name) for name in chinese_cards.values())

    panel = EditEffectsPanel()
    qtbot.addWidget(panel)
    panel.search_input.setText("胶片颗粒")
    panel._apply_search_filter()
    assert _cards(panel.video_effects_tree) == {"plugin:Film_Grain": "胶片颗粒"}

    assert I18N.reload_locale("de_DE")
    assert I18N.tr("edit.effects.titles", "Titles") == "Titel"
    assert labels() == english
