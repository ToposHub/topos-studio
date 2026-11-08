"""VP-B09：Viewport 用户偏好解析——版本化、校验回落、旧键迁移。"""

from __future__ import annotations

import json

import pytest

from src.features.edit.core.viewport_preferences import (
    LEGACY_KEY_DUAL_VIEW,
    LEGACY_KEY_SPLITTER_SIZES,
    ViewportPreferences,
    parse_viewport_preferences,
    preferences_to_json,
)

pytestmark = pytest.mark.edit_core


def test_b09_defaults_on_empty():
    prefs, diagnostics = parse_viewport_preferences({})
    assert prefs.dual_view is False
    assert prefs.splitter_sizes is None
    assert prefs.single_view_target == "timeline"
    assert diagnostics == []


def test_b09_valid_roundtrip():
    raw = {"version": 1, "dual_view": True,
           "splitter_sizes": [400, 600],
           "single_view_target": "source"}
    prefs, diagnostics = parse_viewport_preferences(raw)
    assert diagnostics == []
    assert prefs.dual_view is True
    assert prefs.splitter_sizes == (400, 600)
    assert prefs.single_view_target == "source"
    revived, _ = parse_viewport_preferences(
        json.loads(preferences_to_json(prefs)))
    assert revived == prefs


def test_b09_bad_types_fall_back_per_field():
    prefs, diagnostics = parse_viewport_preferences({
        "version": 1, "dual_view": "yes",
        "splitter_sizes": [0, 100],
        "single_view_target": "program",
    })
    assert prefs.dual_view is False
    assert prefs.splitter_sizes is None
    assert prefs.single_view_target == "timeline"
    assert len(diagnostics) == 3  # 每项一条，不清空其余项


def test_b09_splitter_validation_matrix():
    for bad in ([100], [10, 20, 30], [-5, 100], [True, 100],
                ["400", 600], [0, 0], [1, 100001], "400x600"):
        prefs, diagnostics = parse_viewport_preferences({
            "version": 1, "splitter_sizes": bad})
        assert prefs.splitter_sizes is None, bad
        assert diagnostics, bad
    prefs, _ = parse_viewport_preferences(
        {"version": 1, "splitter_sizes": [1, 100000]})
    assert prefs.splitter_sizes == (1, 100000)  # 边界含端点


def test_b09_future_version_all_defaults():
    prefs, diagnostics = parse_viewport_preferences({
        "version": 2, "dual_view": True, "splitter_sizes": [5, 5]})
    assert prefs == ViewportPreferences()
    assert any("version" in d for d in diagnostics)


def test_b09_non_dict_all_defaults():
    prefs, diagnostics = parse_viewport_preferences("junk")
    assert prefs == ViewportPreferences()
    assert diagnostics


def test_b09_legacy_flat_keys_migrate():
    prefs, diagnostics = parse_viewport_preferences({
        LEGACY_KEY_DUAL_VIEW: True,
        LEGACY_KEY_SPLITTER_SIZES: [300, 500],
    })
    assert prefs.dual_view is True
    assert prefs.splitter_sizes == (300, 500)
    assert any("migrated" in d for d in diagnostics)
    # 迁移同样过校验：坏尺寸回落 + 诊断
    prefs, diagnostics = parse_viewport_preferences({
        LEGACY_KEY_DUAL_VIEW: True,
        LEGACY_KEY_SPLITTER_SIZES: [0, 500],
    })
    assert prefs.splitter_sizes is None
    assert prefs.dual_view is True
