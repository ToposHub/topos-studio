"""Node usage help system — bilingual doc loading and the header "?" button.

Covers:
- get_node_help() language selection (zh_CN / en_US / fallback)
- NodeSectionWidget help button wiring (dialog for documented nodes,
  friendly notice for nodes without a document yet)
"""

from PyQt6.QtWidgets import QMessageBox

from src.features.composite.nodes.cornerpin_node import CornerPinNode
from src.features.composite.ui.node_properties_panel import (
    NodePropertiesPanel,
    NodeSectionWidget,
)
from src.shared import node_help
from src.shared.node_help import available_langs, get_node_help, has_node_help


# ── Document loader ─────────────────────────────────────────────────────────


def test_cornerpin_help_chinese() -> None:
    doc = get_node_help("cornerpin", locale_code="zh_CN")
    assert doc is not None
    assert "CornerPin" in doc
    assert "对齐模式" in doc


def test_cornerpin_help_english() -> None:
    doc = get_node_help("cornerpin", locale_code="en_US")
    assert doc is not None
    assert "Alignment mode" in doc


def test_non_chinese_locale_falls_back_to_english() -> None:
    doc = get_node_help("cornerpin", locale_code="fr_FR")
    assert doc == get_node_help("cornerpin", locale_code="en_US")


def test_unknown_node_has_no_help() -> None:
    assert get_node_help("definitely_not_a_node") is None
    assert not has_node_help("definitely_not_a_node")
    assert has_node_help("cornerpin", locale_code="zh_CN")


def test_missing_chinese_doc_falls_back_to_english(tmp_path, monkeypatch) -> None:
    # Only an English document exists for this fake node.
    (tmp_path / "only_en.en_US.md").write_text("# English only", encoding="utf-8")
    monkeypatch.setattr(node_help, "get_docs_dir", lambda: str(tmp_path))
    assert get_node_help("only_en", locale_code="zh_CN") == "# English only"


def test_locale_without_override_follows_app_locale(monkeypatch) -> None:
    # get_node_help() with no explicit locale reads the app locale.
    monkeypatch.setattr(node_help, "_current_app_locale", lambda: "zh_CN")
    assert "对齐模式" in (get_node_help("cornerpin") or "")
    monkeypatch.setattr(node_help, "_current_app_locale", lambda: "en_US")
    assert "Alignment mode" in (get_node_help("cornerpin") or "")


def test_available_langs_lists_both_cornerpin_docs(monkeypatch) -> None:
    monkeypatch.setattr(node_help, "_current_app_locale", lambda: "zh_CN")
    langs = available_langs("cornerpin")
    assert langs[0] == "zh_CN"  # app locale first
    assert "en_US" in langs
    assert available_langs("definitely_not_a_node") == []


# ── Header help button ──────────────────────────────────────────────────────


def _make_section(node, panel: NodePropertiesPanel) -> NodeSectionWidget:
    section = NodeSectionWidget(
        node_id=node.id,
        node_type=node.node_type,
        node_title=node.node_title,
        parameters=dict(node.parameters),
        param_options=node.get_param_options(),
        param_defs=node.get_param_defs(),
        parent_panel=panel,
    )
    panel._sections[node.id] = section
    return section


def test_section_header_has_help_button(qapp) -> None:
    panel = NodePropertiesPanel()
    section = _make_section(CornerPinNode(node_id="cp"), panel)
    btn = section._help_btn
    assert btn is not None
    assert btn.toolTip()  # i18n tooltip present


def test_help_button_opens_dialog_with_node_doc(qapp) -> None:
    import src.shared.node_help.dialog as dialog_mod

    panel = NodePropertiesPanel()
    section = _make_section(CornerPinNode(node_id="cp"), panel)

    captured = {}

    class _FakeDialog:
        def __init__(self, node_type, node_title, parent=None):
            captured["node_type"] = node_type
            captured["title"] = node_title

        def exec(self):
            captured["execed"] = True

    import unittest.mock as mock
    with mock.patch.object(dialog_mod, "NodeHelpDialog", _FakeDialog):
        section._help_btn.click()

    assert captured.get("execed") is True
    assert captured["node_type"] == "cornerpin"
    assert captured["title"] == "CornerPin"


def test_help_dialog_switches_language(qapp) -> None:
    from src.shared.node_help.dialog import NodeHelpDialog
    from src.shared.ui.widgets.chevron_combo import ChevronComboBox

    dialog = NodeHelpDialog("cornerpin", "CornerPin")

    # Uses the project-wide unified dropdown, no custom styling.
    assert isinstance(dialog._lang_combo, ChevronComboBox)

    # Wide enough to show the longest option without truncation.
    fm = dialog._lang_combo.fontMetrics()
    widest = max(fm.horizontalAdvance(dialog._lang_combo.itemText(i))
                 for i in range(dialog._lang_combo.count()))
    assert dialog._lang_combo.minimumWidth() >= widest

    # Both languages available in the selector.
    combo_langs = [dialog._lang_combo.itemData(i)
                   for i in range(dialog._lang_combo.count())]
    assert "zh_CN" in combo_langs and "en_US" in combo_langs

    dialog.set_language("en_US")
    assert dialog.current_lang() == "en_US"
    assert "Alignment mode" in dialog._browser.toPlainText()

    dialog.set_language("zh_CN")
    assert dialog.current_lang() == "zh_CN"
    assert "对齐模式" in dialog._browser.toPlainText()


def test_help_dialog_typography_hierarchy(qapp) -> None:
    from PyQt6.QtGui import QTextCursor
    from src.shared.node_help import dialog as dialog_mod
    from src.shared.node_help.dialog import NodeHelpDialog

    dialog = NodeHelpDialog("cornerpin", "CornerPin")
    doc = dialog._browser.document()

    found = {"h1": False, "h2": False, "body": False}
    block = doc.begin()
    while block.isValid():
        level = block.blockFormat().headingLevel()
        cursor = QTextCursor(doc)
        cursor.setPosition(block.position())
        color = cursor.charFormat().foreground().color()
        if level == 1:
            assert color == dialog_mod._H1_COLOR  # light red
            assert cursor.charFormat().fontPointSize() == dialog_mod._H1_SIZE
            found["h1"] = True
        elif level == 2:
            assert color == dialog_mod._H2_COLOR  # light red, smaller
            assert cursor.charFormat().fontPointSize() == dialog_mod._H2_SIZE
            found["h2"] = True
        elif level >= 3:
            assert color == dialog_mod._H3_COLOR  # off-white
        elif block.text().strip():
            assert color == dialog_mod._BODY_COLOR  # light gray
            found["body"] = True
        block = block.next()

    assert all(found.values()), found


def test_help_dialog_typography_level3_headings(
        qapp, tmp_path, monkeypatch) -> None:
    from PyQt6.QtGui import QTextCursor
    from src.shared.node_help import dialog as dialog_mod
    from src.shared.node_help.dialog import NodeHelpDialog

    (tmp_path / "fake.zh_CN.md").write_text(
        "# Top\n\n## Section\n\n### Subsection\n\nbody text\n",
        encoding="utf-8")
    monkeypatch.setattr(node_help, "get_docs_dir", lambda: str(tmp_path))

    dialog = NodeHelpDialog("fake", "Fake")
    doc = dialog._browser.document()

    seen = {}
    block = doc.begin()
    while block.isValid():
        level = block.blockFormat().headingLevel()
        cursor = QTextCursor(doc)
        cursor.setPosition(block.position())
        seen[level] = (cursor.charFormat().foreground().color(),
                       cursor.charFormat().fontPointSize())
        block = block.next()

    assert seen[1] == (dialog_mod._H1_COLOR, dialog_mod._H1_SIZE)
    assert seen[2] == (dialog_mod._H2_COLOR, dialog_mod._H2_SIZE)
    assert seen[3] == (dialog_mod._H3_COLOR, dialog_mod._H3_SIZE)
    # Off-white headings stay visibly brighter than light-gray body text.
    assert dialog_mod._H3_COLOR.lightness() > dialog_mod._BODY_COLOR.lightness()


def test_help_button_shows_notice_when_no_doc(qapp, monkeypatch) -> None:
    panel = NodePropertiesPanel()
    node = CornerPinNode(node_id="cp")
    section = _make_section(node, panel)
    section.node_type = "definitely_not_a_node"

    shown = {}

    def fake_info(parent, title, text):
        shown["title"] = title
        shown["text"] = text
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "information", staticmethod(fake_info))
    section._help_btn.click()

    assert shown["text"]  # unavailable notice displayed
