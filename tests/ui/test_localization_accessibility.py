"""T71 (localization) + T77 (accessibility) tests.

T71 — verifies every keyframes/undo i18n key the new Graph Editor code
references resolves in BOTH en_US and zh_CN locale files (no missing
translations, no fallback-only keys for shipped strings).

T77 — verifies icon-only toolbar buttons expose an accessible name +
tooltip (screen readers announce them; colour isn't the only status cue)
and are keyboard-focusable.
"""

import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

_LOCALES = os.path.join(os.path.dirname(__file__), '..', '..',
                        'src', 'shared', 'i18n', 'locales')


def _load(locale):
    with open(os.path.join(_LOCALES, f"{locale}.json"), encoding="utf-8") as f:
        return json.load(f)


# The keys the Graph Editor + panel code references (kept in sync with the
# I18N.tr(...) call sites in the composite UI).
KEYFRAME_KEYS = [
    "add_kf", "center_playhead", "col_name", "col_value", "collapse_all",
    "del_kf", "empty", "expand_all", "expression_for", "expression_invalid",
    "expression_none", "expression_placeholder", "first_frame", "fit",
    "fit_selection", "graph_editor", "graph_type", "graph_type_auto",
    "graph_type_speed", "graph_type_value", "last_frame", "next_frame",
    "next_kf", "pan", "pause", "play", "prev_frame", "prev_kf", "range",
    "snap", "unit_frame", "unit_seconds", "unit_time", "unit_timecode",
    "zoom_in", "zoom_out",
]
UNDO_KEYS = ["graph_drag"]


# ── T71: localization ─────────────────────────────────────────────────


@pytest.mark.parametrize("locale", ["en_US", "zh_CN"])
class TestLocalization:
    def test_keyframes_section_exists(self, locale):
        d = _load(locale)
        assert "keyframes" in d, f"{locale} has no 'keyframes' section"
        assert isinstance(d["keyframes"], dict)

    @pytest.mark.parametrize("key", KEYFRAME_KEYS)
    def test_keyframe_key_present(self, locale, key):
        d = _load(locale)
        assert key in d["keyframes"], (
            f"{locale}: missing keyframes.{key}")

    @pytest.mark.parametrize("key", KEYFRAME_KEYS)
    def test_keyframe_key_nonempty(self, locale, key):
        d = _load(locale)
        val = d["keyframes"][key]
        assert isinstance(val, str) and val.strip(), (
            f"{locale}: keyframes.{key} is empty/not a string")

    @pytest.mark.parametrize("key", UNDO_KEYS)
    def test_undo_key_present(self, locale, key):
        d = _load(locale)
        assert "undo" in d, f"{locale} has no 'undo' section"
        assert key in d["undo"], f"{locale}: missing undo.{key}"


def test_en_and_zh_have_same_keyframe_keys():
    """Both locales ship the same keyframes key set (no key missing on one)."""
    en = set(_load("en_US").get("keyframes", {}).keys())
    zh = set(_load("zh_CN").get("keyframes", {}).keys())
    assert en == zh, (
        f"keyframes key mismatch — en only: {en - zh}, zh only: {zh - en}")


# ── T71: keys resolve through I18N.tr ─────────────────────────────────


class TestI18nResolution:
    def test_tr_resolves_loaded_key(self):
        """When translations are loaded, I18N.tr resolves to the locale value
        (not the fallback)."""
        from src.shared.i18n.i18n import I18N
        I18N._translations = _load("en_US")
        try:
            assert I18N.tr("keyframes.graph_editor", "FALLBACK") == \
                "Graph Editor (Shift+F3)"
        finally:
            I18N._translations = {}  # reset for other tests

    def test_tr_falls_back_when_not_loaded(self):
        """With no translations loaded, tr returns the fallback arg."""
        from src.shared.i18n.i18n import I18N
        I18N._translations = {}
        assert I18N.tr("keyframes.graph_editor", "FALLBACK") == "FALLBACK"


# ── T77: accessibility ────────────────────────────────────────────────


class TestAccessibility:
    def test_icon_button_has_accessible_name(self, qapp):
        """Every icon-only toolbar button (with an action) exposes an
        accessible name. The toolbar's overflow/extension arrow has no
        action and is excluded (it's not a user-meaningful control)."""
        from PyQt6.QtWidgets import QToolBar, QToolButton
        from src.features.composite.ui.keyframes_panel import KeyframesPanel
        from src.features.composite.ui.keyframes_model import \
            CompositeTimelineState
        panel = KeyframesPanel()
        panel.set_timeline_state(CompositeTimelineState(start_frame=0,
                                                        end_frame=100))
        # Find all QToolButtons in the toolbar that have a defaultAction
        # (the overflow arrow doesn't — skip it).
        toolbars = panel.findChildren(QToolBar)
        buttons = []
        for tb in toolbars:
            for b in tb.findChildren(QToolButton):
                if b.defaultAction() is not None:
                    buttons.append(b)
        assert buttons, "no action toolbar buttons found"
        unnamed = [b for b in buttons
                   if not b.accessibleName().strip()]
        assert not unnamed, (
            f"{len(unnamed)} toolbar buttons lack an accessible name")

    def test_icon_button_has_tooltip(self, qapp):
        """Every icon-only button (with an action) has a tooltip."""
        from PyQt6.QtWidgets import QToolBar, QToolButton
        from src.features.composite.ui.keyframes_panel import KeyframesPanel
        from src.features.composite.ui.keyframes_model import \
            CompositeTimelineState
        panel = KeyframesPanel()
        panel.set_timeline_state(CompositeTimelineState(start_frame=0,
                                                        end_frame=100))
        toolbars = panel.findChildren(QToolBar)
        buttons = []
        for tb in toolbars:
            for b in tb.findChildren(QToolButton):
                if b.defaultAction() is not None:
                    buttons.append(b)
        notipped = [b for b in buttons if not b.toolTip().strip()]
        assert not notipped, (
            f"{len(notipped)} toolbar buttons lack a tooltip")

    def test_icon_button_is_focusable(self, qapp):
        """Icon buttons are keyboard-focusable (tab chain reaches them)."""
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QToolBar, QToolButton
        from src.features.composite.ui.keyframes_panel import KeyframesPanel
        from src.features.composite.ui.keyframes_model import \
            CompositeTimelineState
        panel = KeyframesPanel()
        panel.set_timeline_state(CompositeTimelineState(start_frame=0,
                                                        end_frame=100))
        toolbars = panel.findChildren(QToolBar)
        buttons = []
        for tb in toolbars:
            buttons.extend(tb.findChildren(QToolButton))
        unfocusable = [b for b in buttons
                       if b.focusPolicy() == Qt.FocusPolicy.NoFocus]
        assert not unfocusable, (
            f"{len(unfocusable)} toolbar buttons aren't keyboard-focusable")

    def test_graph_editor_action_has_accessible_text(self, qapp):
        """The Graph Editor toggle action carries a human label (the tooltip
        + accessible name derive from it)."""
        from src.features.composite.ui.keyframes_panel import KeyframesPanel
        from src.features.composite.ui.keyframes_model import \
            CompositeTimelineState
        panel = KeyframesPanel()
        panel.set_timeline_state(CompositeTimelineState(start_frame=0,
                                                        end_frame=100))
        assert hasattr(panel, "_act_graph_editor")
        assert panel._act_graph_editor.text().strip(), \
            "Graph Editor action has no label"
