"""src/products/ 产品装配矩阵专测（P5-T2）。

覆盖：四产品 preset 集合正确性、ALWAYS_ENABLED 压制、短名/未知名环境
解析、get_enabled_features 与 feature_flags 的一致性、packaging 配置与
preset 的对应、未知产品拒绝、各产品入口 register() 委托语义。
"""

from __future__ import annotations

import pytest

from src.products import (
    ALL_FEATURES,
    ALWAYS_ENABLED,
    FEATURE_COLOR,
    FEATURE_COMPOSITE,
    FEATURE_DELIVER,
    FEATURE_EDIT,
    FEATURE_LIBRARY,
    FEATURE_TEACHING_ANIMATION,
    PRODUCT_PRESETS,
    ProductPreset,
    get_current_product,
    get_enabled_features,
)
from src.products.packaging import get_packaging_config


EXPECTED = {
    # product: (resolved_enabled, resolved_disabled)
    "topos-studio": (
        ALL_FEATURES,
        frozenset(),
    ),
    "topos-color": (
        frozenset({FEATURE_LIBRARY, FEATURE_COLOR, FEATURE_DELIVER}),
        frozenset({FEATURE_EDIT, FEATURE_COMPOSITE, FEATURE_TEACHING_ANIMATION}),
    ),
    "topos-edit": (
        frozenset({FEATURE_LIBRARY, FEATURE_COLOR, FEATURE_EDIT, FEATURE_DELIVER}),
        frozenset({FEATURE_COMPOSITE, FEATURE_TEACHING_ANIMATION}),
    ),
    "topos-composite": (
        frozenset({FEATURE_LIBRARY, FEATURE_COLOR, FEATURE_COMPOSITE, FEATURE_DELIVER,
                   FEATURE_TEACHING_ANIMATION}),
        frozenset({FEATURE_EDIT}),
    ),
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_preset_resolution_matches_contract(name):
    preset = PRODUCT_PRESETS[name]
    enabled, disabled = EXPECTED[name]
    assert preset.resolved_enabled == frozenset(enabled)
    assert preset.resolved_disabled == frozenset(disabled)


def test_every_feature_direction_is_total():
    # 每个产品的 enabled/disabled 必须互斥且并集覆盖全部特性域
    for name, preset in PRODUCT_PRESETS.items():
        assert preset.resolved_enabled & preset.resolved_disabled == frozenset(), name
        assert preset.resolved_enabled | preset.resolved_disabled == ALL_FEATURES, name


def test_always_enabled_wins_over_disabled():
    preset = ProductPreset(
        name="t", display_name="t",
        enabled_features=frozenset(),
        disabled_features=frozenset(ALL_FEATURES),
    )
    assert preset.resolved_enabled == ALWAYS_ENABLED
    assert FEATURE_COLOR not in preset.resolved_disabled
    assert FEATURE_LIBRARY not in preset.resolved_disabled


@pytest.mark.parametrize(
    ("mode", "expect"),
    [
        ("", "topos-studio"),            # 未设置 → 默认全量
        ("studio", "topos-studio"),      # 短名
        ("topos-studio", "topos-studio"),  # 全名
        ("Topos-Color", "topos-color"),  # 大小写不敏感
        ("color", "topos-color"),
        ("edit", "topos-edit"),
        ("composite", "topos-composite"),
        ("nonsense", "topos-studio"),    # 未知 → 回退默认
    ],
)
def test_get_current_product_env_matrix(monkeypatch, mode, expect):
    monkeypatch.setenv("TOPOS_PRODUCT_MODE", mode)
    assert get_current_product().name == expect


def test_get_current_product_unset_defaults(monkeypatch):
    monkeypatch.delenv("TOPOS_PRODUCT_MODE", raising=False)
    assert get_current_product().name == "topos-studio"


def test_get_enabled_features_follows_disable_env(monkeypatch):
    monkeypatch.setenv("TOPOS_PRODUCT_MODE", "studio")
    monkeypatch.delenv("TOPOS_DISABLE_FEATURES", raising=False)
    monkeypatch.delenv("TOPOS_FEATURE_COMPOSITE", raising=False)
    full = get_enabled_features()
    assert FEATURE_COMPOSITE in full

    monkeypatch.setenv("TOPOS_DISABLE_FEATURES", "composite")
    trimmed = get_enabled_features()
    assert FEATURE_COMPOSITE not in trimmed
    assert FEATURE_COLOR in trimmed  # 核心不可禁
    monkeypatch.delenv("TOPOS_DISABLE_FEATURES", raising=False)


# ---------------------------------------------------------------------------
# packaging
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_packaging_config_consistent_with_preset(name):
    cfg = get_packaging_config(name)
    enabled, disabled = EXPECTED[name]
    assert cfg["include_features"] == sorted(enabled)
    assert cfg["exclude_features"] == sorted(disabled)
    # include/exclude 目录互斥
    assert not (set(cfg["include_dirs"]) & set(cfg["exclude_dirs"]))


def test_packaging_color_omits_edit_and_composite_dirs():
    cfg = get_packaging_config("topos-color")
    assert "color" in cfg["include_dirs"]
    assert "edit" not in cfg["include_dirs"]
    assert "composite" not in cfg["include_dirs"]
    assert "edit" in cfg["exclude_dirs"]
    assert cfg["include_plugins"] == []


def test_packaging_composite_ships_plugins():
    cfg = get_packaging_config("topos-composite")
    assert cfg["include_plugins"] == ["*"]
    assert "composite" in cfg["include_dirs"]


def test_packaging_unknown_product_rejected():
    with pytest.raises(ValueError, match="Unknown product"):
        get_packaging_config("topos-nonexistent")


def test_packaging_keys_contract():
    cfg = get_packaging_config("topos-studio")
    assert set(cfg) == {
        "include_features", "exclude_features", "include_dirs",
        "exclude_dirs", "include_locales", "include_plugins",
    }
    # studio 全量：不排除任何特性
    assert cfg["exclude_features"] == []


# ---------------------------------------------------------------------------
# 各产品入口 register() 委托
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module_name", "preset_name"),
    [
        ("src.products.topos_studio", "topos-studio"),
        ("src.products.topos_color", "topos-color"),
        ("src.products.topos_edit", "topos-edit"),
        ("src.products.topos_composite", "topos-composite"),
    ],
)
def test_product_register_delegates_to_module_loader(monkeypatch, module_name, preset_name):
    import importlib
    import src.app.module_loader as ml
    mod = importlib.import_module(module_name)

    recorded: list[tuple[object, ProductPreset]] = []
    monkeypatch.setattr(ml, "register_preset_modules",
                        lambda registry, preset: recorded.append((registry, preset)))
    sentinel = object()
    mod.register(sentinel)
    assert len(recorded) == 1
    assert recorded[0][0] is sentinel
    assert recorded[0][1].name == preset_name
