"""R5: MG content localization — manifests carry locale fields, UI resolves them."""

import json
from pathlib import Path

import pytest

from src.features.teaching_animation.core.content_localization import (
    localized_text,
    sanitized_i18n_dict,
    template_description,
    template_display_name,
)
from src.features.teaching_animation.core.motion_graphic_template_library import (
    default_motion_graphic_template_root,
    discover_motion_graphic_template_packages,
)
from src.features.teaching_animation.presets.library import MgPresetLibrary
from src.features.teaching_animation.presets.manifest import (
    PresetManifest,
    localized_preset_description,
    localized_preset_name,
)
from src.shared.i18n.i18n import I18N


@pytest.fixture()
def en_locale():
    previous = I18N.get_current_locale()
    I18N.load_locale("en_US")
    yield
    I18N.load_locale(previous)


def test_localized_text_fallback_chain(en_locale) -> None:
    values = {"en_US": "English", "zh_CN": "中文"}
    assert localized_text("base", values) == "English"
    assert localized_text("base", {"zh_CN": "中文"}) == "base"
    assert localized_text("base", None) == "base"
    assert localized_text("", {"en_US": "English"}) == "English"
    assert sanitized_i18n_dict({"en_US": " x ", "": "y", 5: 6}) == {"en_US": "x"}


def test_zh_locale_prefers_exact_match() -> None:
    previous = I18N.get_current_locale()
    I18N.load_locale("zh_CN")
    try:
        assert localized_text("base", {"zh_CN": "中文", "en_US": "English"}) == "中文"
    finally:
        I18N.load_locale(previous)


def test_bundled_template_manifest_carries_i18n(en_locale) -> None:
    packages = discover_motion_graphic_template_packages(default_motion_graphic_template_root())
    assert packages, "bundled template library is missing"
    particle = next(p for p in packages if p.template.template_id == "particle-flow-background")
    i18n = particle.template.metadata.get("display_name_i18n")
    assert isinstance(i18n, dict) and i18n.get("en_US") == "Particle Flow Background"
    assert template_display_name(particle.template) == "Particle Flow Background"
    assert template_description(particle.template).startswith("Cyan particle streams")


def test_manifest_without_i18n_keeps_base_name(en_locale) -> None:
    template = type("T", (), {
        "display_name": "粒子流光背景", "description": "desc",
        "metadata": {"render_profile": "motion_graphic_freeform"},
    })()
    assert template_display_name(template) == "粒子流光背景"
    assert template_description(template) == "desc"


def test_preset_manifest_round_trips_i18n(en_locale) -> None:
    manifest = PresetManifest.from_dict({
        "preset_id": "demo", "name": "演示预设", "category": "charts",
        "name_i18n": {"en_US": "Demo Preset"},
        "description_i18n": {"en_US": "A demo"},
    })
    assert localized_preset_name(manifest) == "Demo Preset"
    assert localized_preset_description(manifest) == "A demo"
    data = manifest.to_dict()
    assert data["name_i18n"] == {"en_US": "Demo Preset"}
    restored = PresetManifest.from_dict(json.loads(json.dumps(data)))
    assert restored.name_i18n == (("en_US", "Demo Preset"),)


def test_bundled_presets_resolve_english_names_and_search(en_locale) -> None:
    library = MgPresetLibrary()
    assert library.records()
    for record in library.records():
        name = localized_preset_name(record.manifest)
        # Every bundled preset must have a non-CJK name in English mode.
        assert not any("\u4e00" <= ch <= "\u9fff" for ch in name), (
            f"preset {record.preset_id} has no en_US name: {name!r}")
    english_matches = library.search("histogram")
    assert any(r.preset_id == "animated_histogram" for r in english_matches)
    english_chart = library.search("donut")
    assert any(r.preset_id == "pie_donut_chart" for r in english_chart)


def test_effects_panel_has_no_hardcoded_cjk_text() -> None:
    source = Path("src/features/edit/ui/edit_effects_panel.py").read_text(encoding="utf-8")
    cjk = [line.strip() for line in source.splitlines()
           if any("\u4e00" <= ch <= "\u9fff" for ch in line)]
    assert not cjk, f"hardcoded CJK lines remain: {cjk[:5]}"
