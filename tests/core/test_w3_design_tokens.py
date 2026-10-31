"""W3 design tokens + brand theming (TM-05)."""

from __future__ import annotations

import pytest

from src.features.teaching_animation.core.design_tokens import (
    BrandTheme, DesignTokenSet, default_brand_tokens, resolve_style, validate_theme,
)


class TestResolve:
    def test_resolves_palette_ref(self) -> None:
        tokens = default_brand_tokens()
        assert tokens.resolve_value("$palette.primary") == "#3b82f6"

    def test_unresolved_ref_passes_through(self) -> None:
        tokens = default_brand_tokens()
        assert tokens.resolve_value("$palette.nonexistent") == "$palette.nonexistent"

    def test_concrete_value_unchanged(self) -> None:
        assert default_brand_tokens().resolve_value("#ff0000") == "#ff0000"

    def test_resolve_style_replaces_refs(self) -> None:
        tokens = default_brand_tokens()
        style = {"color": "$palette.primary", "font_size": "$type_scale.title"}
        resolved = resolve_style(style, tokens)
        assert resolved["color"] == "#3b82f6"
        assert resolved["font_size"] == 36.0


class TestBrandOverlay:
    def test_overlay_replaces_primary(self) -> None:
        base = default_brand_tokens()
        theme = BrandTheme(theme_id="brand", palette={"primary": "#2563eb"})
        themed = base.overlay(theme)
        assert themed.resolve_value("$palette.primary") == "#2563eb"
        # other tokens survive
        assert themed.resolve_value("$palette.accent") == "#f59e0b"

    def test_overlay_safe_area(self) -> None:
        base = default_brand_tokens()
        theme = BrandTheme(theme_id="brand", safe_area=(100.0, 100.0, 100.0, 100.0))
        themed = base.overlay(theme)
        assert themed.safe_area == (100.0, 100.0, 100.0, 100.0)


class TestLocked:
    def test_locked_keys_preserved(self) -> None:
        tokens = default_brand_tokens()
        style = {"color": "$palette.primary", "font_size": 12.0, "__locked__": ["color"]}
        resolved = resolve_style(style, tokens)
        assert resolved["color"] == "$palette.primary"  # untouched
        assert resolved["font_size"] == 12.0

    def test_whole_style_locked(self) -> None:
        tokens = default_brand_tokens()
        style = {"color": "$palette.primary", "__locked__": True}
        resolved = resolve_style(style, tokens)
        assert resolved["color"] == "$palette.primary"


class TestValidateTheme:
    def test_reports_unresolved_refs(self) -> None:
        base = default_brand_tokens()
        theme = BrandTheme(theme_id="brand", palette={"primary": "$palette.missing_ref"})
        conflict = validate_theme(base, theme)
        assert len(conflict.unresolved) == 1
        assert "primary" in conflict.unresolved[0]

    def test_no_conflict_for_concrete(self) -> None:
        base = default_brand_tokens()
        theme = BrandTheme(theme_id="brand", palette={"primary": "#123456"})
        assert validate_theme(base, theme).unresolved == ()
