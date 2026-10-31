"""Batch B: asset provider registry (RD-05/TM-07) + font registry (RD-06)."""

from __future__ import annotations

import pytest

from src.features.teaching_animation.core import (
    AssetDescriptor, AssetKind, AssetLicenseStatus, AssetPreflightIssue,
    AssetProviderRegistry, FontEntry, FontLicense, FontRegistry, MotionAssetPack,
)


# ---------------------------------------------------------------------------
# RD-05 asset provider
# ---------------------------------------------------------------------------

class TestAssetDescriptor:
    def test_round_trip(self) -> None:
        d = AssetDescriptor(asset_id="logo", kind=AssetKind.RASTER, sha256="abc",
                            license=AssetLicenseStatus.OK, width=128, height=128)
        restored = AssetDescriptor.from_dict(d.to_dict())
        assert restored == d

    def test_absolute_uri_rejected(self) -> None:
        with pytest.raises(ValueError):
            AssetDescriptor(asset_id="x", kind=AssetKind.RASTER, source_uri="/etc/x")

    def test_animated_detection(self) -> None:
        still = AssetDescriptor(asset_id="s", kind=AssetKind.RASTER)
        clip = AssetDescriptor(asset_id="c", kind=AssetKind.LOTTIE, frame_count=24)
        assert still.is_animated is False
        assert clip.is_animated is True


class _StubProvider:
    """Minimal in-process provider for the registry tests."""
    provider_id = "stub"
    supported_kinds = (AssetKind.RASTER, AssetKind.SVG)

    def __init__(self, descriptors: dict[str, AssetDescriptor]) -> None:
        self._descriptors = descriptors

    def resolve(self, asset_id: str) -> AssetDescriptor | None:
        return self._descriptors.get(asset_id)

    def can_seek(self, descriptor: AssetDescriptor) -> bool:
        return descriptor.kind in self.supported_kinds

    def seek(self, descriptor: AssetDescriptor, frame: int) -> object:
        class _R:
            available = True
        return _R()


class TestAssetProviderRegistry:
    def test_resolve_via_provider(self) -> None:
        reg = AssetProviderRegistry()
        reg.register_provider(_StubProvider({"logo": AssetDescriptor("logo", AssetKind.RASTER, provider_id="stub")}))
        resolved = reg.resolve("logo")
        assert resolved is not None
        descriptor, provider = resolved
        assert descriptor.kind == AssetKind.RASTER
        assert provider.provider_id == "stub"

    def test_preflight_unresolved(self) -> None:
        reg = AssetProviderRegistry()
        issues = reg.preflight(("ghost",))
        assert issues[0].code == "unresolved"

    def test_preflight_license_blocks_delivery(self) -> None:
        reg = AssetProviderRegistry()
        reg.register_provider(_StubProvider({
            "preview_only": AssetDescriptor("preview_only", AssetKind.RASTER, license=AssetLicenseStatus.PREVIEW_ONLY, provider_id="stub")}))
        issues = reg.preflight(("preview_only",), for_delivery=True)
        assert any(i.code == "license_blocks" for i in issues)
        # preview-only is fine for non-delivery
        assert reg.preflight(("preview_only",), for_delivery=False) == ()

    def test_preflight_no_provider_for_kind(self) -> None:
        reg = AssetProviderRegistry()
        # descriptor registered but no provider handles its kind
        reg.register_descriptor(AssetDescriptor("lottie_clip", AssetKind.LOTTIE, provider_id="missing"))
        issues = reg.preflight(("lottie_clip",))
        assert any(i.code == "no_provider" for i in issues)


# ---------------------------------------------------------------------------
# TM-07 MotionAssetPack
# ---------------------------------------------------------------------------

class TestMotionAssetPack:
    def test_round_trip_and_fallback(self) -> None:
        pack = MotionAssetPack(
            pack_id="brand_kit", version=2, license=AssetLicenseStatus.OK,
            assets=(AssetDescriptor("icon_play", AssetKind.SVG), AssetDescriptor("loader", AssetKind.LOTTIE)),
            fallback_for={"loader": "icon_play"},
        )
        assert pack.resolve_fallback("loader") == "icon_play"
        assert "icon_play" in pack.asset_ids()

    def test_duplicate_asset_ids_rejected(self) -> None:
        with pytest.raises(ValueError):
            MotionAssetPack(pack_id="p", version=1,
                            assets=(AssetDescriptor("a", AssetKind.SVG), AssetDescriptor("a", AssetKind.SVG)))


# ---------------------------------------------------------------------------
# RD-06 font registry
# ---------------------------------------------------------------------------

class TestFontEntry:
    def test_covers_basic_latin(self) -> None:
        font = FontEntry(family="Sans", coverage_blocks=frozenset({"basic_latin"}), license=FontLicense.OPEN)
        ok, missing = font.covers_text("Hello")
        assert ok and missing == ()

    def test_reports_missing_cjk(self) -> None:
        font = FontEntry(family="Sans", coverage_blocks=frozenset({"basic_latin"}), license=FontLicense.OPEN)
        ok, missing = font.covers_text("你好")
        assert ok is False
        assert 0x4F60 in missing  # '你'

    def test_explicit_codepoints_override(self) -> None:
        font = FontEntry(family="Sans", coverage_blocks=frozenset({"basic_latin"}),
                         explicit_codepoints=frozenset({0x4F60, 0x597D}), license=FontLicense.OPEN)
        ok, _ = font.covers_text("你好")
        assert ok

    def test_absolute_uri_rejected(self) -> None:
        with pytest.raises(ValueError):
            FontEntry(family="Sans", file_uri="/abs/x.ttf")

    def test_unknown_block_rejected(self) -> None:
        with pytest.raises(ValueError):
            FontEntry(family="Sans", coverage_blocks=frozenset({"made_up_block"}))


class TestFontRegistry:
    def test_resolve_chain_with_fallback(self) -> None:
        reg = FontRegistry()
        reg.register(FontEntry(family="Sans", coverage_blocks=frozenset({"basic_latin"}), license=FontLicense.OPEN))
        reg.register(FontEntry(family="CJK", coverage_blocks=frozenset({"cjk_unified"}), license=FontLicense.OPEN))
        resolution = reg.resolve_chain("Hello 你好", ["Sans", "CJK"])
        assert resolution.chosen is not None and resolution.chosen.family == "Sans"
        # both fonts together cover the text -> no unresolved
        assert resolution.missing_by_font.get("__unresolved__") in ((), None)

    def test_missing_glyph_unresolved(self) -> None:
        reg = FontRegistry()
        reg.register(FontEntry(family="Sans", coverage_blocks=frozenset({"basic_latin"}), license=FontLicense.OPEN))
        resolution = reg.resolve_chain("你好", ["Sans"])
        assert 0x4F60 in resolution.missing_by_font["__unresolved__"]

    def test_missing_license_blocks_delivery(self) -> None:
        reg = FontRegistry()
        reg.register(FontEntry(family="Sans", coverage_blocks=frozenset({"basic_latin"}), license=FontLicense.MISSING))
        resolution = reg.resolve_chain("Hi", ["Sans"])
        assert resolution.blocks_delivery is True

    def test_no_entry_for_family(self) -> None:
        reg = FontRegistry()
        resolution = reg.resolve_chain("Hi", ["Ghost"])
        assert resolution.chosen is None
