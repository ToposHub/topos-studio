"""Motion Graphic generator palette contracts (no Qt/browser required)."""

from __future__ import annotations

import json

import pytest

from src.features.teaching_animation.core.executable_template import ParamSpec
from src.features.teaching_animation.core.html_template import HtmlTemplate
from src.features.teaching_animation.core.motion_graphic_generator_registry import (
    get_motion_graphic_generator_registry,
)
from src.features.teaching_animation.core.motion_graphic_template_library import (
    discover_motion_graphic_template_packages,
    load_motion_graphic_template_library,
    load_motion_graphic_template_package,
)
from src.features.teaching_animation.render.html_layer.template_promote import (
    apply_html_template,
)


@pytest.fixture(autouse=True)
def _clean_generator_palette():
    registry = get_motion_graphic_generator_registry()
    registry.clear()
    yield
    registry.clear()


def _template() -> HtmlTemplate:
    return HtmlTemplate(
        template_id="particle-bg",
        display_name="Particle Background",
        description="A reusable background generator",
        html_template=(
            "<style>:root{--accent:{{token:accent}};--density:{{token:density}}}"
            "body{color:var(--accent)}</style>"
            "<div data-topos-page='1' data-topos-node-id='title'>{{slot:title}}</div>"
        ),
        params_schema=(
            ParamSpec("title", "string", "Particles"),
            ParamSpec("accent", "color", "#ff8800"),
            ParamSpec("density", "integer", 24),
        ),
        viewport_width=1920,
        viewport_height=1080,
        duration_frames=60,
        metadata={
            "render_profile": "motion_graphic_freeform",
            "allow_inline_scripts": True,
            "allow_freeform_css": True,
        },
    )


def test_registry_keeps_latest_version_and_not_media_pool_asset() -> None:
    registry = get_motion_graphic_generator_registry()
    registry.register(_template())
    newer = _template()
    newer = HtmlTemplate.from_dict({**newer.to_dict(), "version": 2})
    registry.register(newer)

    assert registry.get("particle-bg") is newer
    assert [item.template_id for item in registry.list_templates()] == ["particle-bg"]


def test_template_parameters_are_validated_before_html_instantiation() -> None:
    template = _template()
    registry = get_motion_graphic_generator_registry()
    registry.register(template)

    layer = apply_html_template(
        registry.get("particle-bg"),
        {"title": "Signal", "accent": "#00e5ff", "density": 48},
        layer_id="layer-1",
    )
    assert "Signal" in layer.sanitized_html
    assert "#00e5ff" in layer.sanitized_html
    assert layer.metadata["render_profile"] == "motion_graphic_freeform"

    with pytest.raises(ValueError, match="unknown parameter"):
        apply_html_template(
            template,
            {"unknown": 1},
            layer_id="layer-2",
        )


def test_packaged_library_keeps_preview_out_of_project_templates() -> None:
    """A bundled folder is portable at runtime but never serializes a path."""
    registry = get_motion_graphic_generator_registry()
    packages = discover_motion_graphic_template_packages()
    particle_package = next(
        package for package in packages
        if package.template.template_id == "particle-flow-background"
    )

    load_motion_graphic_template_library(registry)

    template = registry.get("particle-flow-background")
    assert template is not None
    assert "particle-flow" in template.html_template
    assert "radial-gradient" not in template.html_template
    assert "createRadialGradient" not in template.html_template
    assert registry.preview_path(template.template_id, template.version) == particle_package.preview_path
    assert registry.is_packaged(template.template_id, template.version)
    assert registry.list_project_templates() == ()

    layer = apply_html_template(template, layer_id="packaged-particle-layer")
    assert "particle-flow" in layer.sanitized_html
    assert "{{" not in layer.sanitized_html
    # The editor receives types/ranges from the package, not only CSS strings,
    # so it can use the same colour/numeric controls as clip Properties.
    assert {
        item["name"]: item["type"]
        for item in layer.metadata["motion_graphic_parameter_schema"]
    } == {
        "accent": "color",
        "particle_density": "integer",
        "motion_speed": "number",
    }
    assert layer.metadata["parameter_ui"]["motion_speed"]["max"] == 2.5


def test_package_loader_rejects_files_outside_template_folder(tmp_path) -> None:
    """A copied manifest cannot read an arbitrary local file as HTML/GIF."""
    package_root = tmp_path / "unsafe-template"
    package_root.mkdir()
    (tmp_path / "outside.html").write_text("<div>unsafe</div>", encoding="utf-8")
    (package_root / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "template_id": "unsafe",
        "display_name": "Unsafe",
        "template_file": "../outside.html",
        "params_schema": [],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="stay inside"):
        load_motion_graphic_template_package(package_root)
