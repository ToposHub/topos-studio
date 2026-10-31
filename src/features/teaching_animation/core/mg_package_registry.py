"""R4: one unified catalog over the two reusable MG content systems.

The project grew two parallel libraries — HTML template packages
(``templates/motion_graphic/<id>/`` for browser-native generators) and
structured scene presets (``presets/library/<category>/<id>/`` for DOM/SVG
element grafts).  Both are folder-per-package with a GIF preview, but nothing
shared a listing, so panels and tooling saw half the catalog each.

This module is the unified *read model*: one ``MgPackage`` shape with a
``kind`` discriminator, locale-resolvable display text, preview path and
origin, refreshed from both systems.  Write paths (save/delete/import/export)
stay with their owning systems — the catalog never mutates them — so unified
listing cannot corrupt either library.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

KIND_HTML_TEMPLATE = "html_template"
KIND_SCENE_PRESET = "scene_preset"


@dataclass(frozen=True)
class MgPackage:
    """One reusable MG package, regardless of which library owns it."""

    package_id: str
    kind: str
    display_name: str
    description: str
    category: str
    version: int
    origin: str
    preview_path: Path | None = None
    # The owning system's record (HtmlTemplate-backed package or
    # PresetRecord).  Read-only passthrough for callers that need details.
    source: Any = None

    @property
    def is_html_template(self) -> bool:
        return self.kind == KIND_HTML_TEMPLATE

    @property
    def is_scene_preset(self) -> bool:
        return self.kind == KIND_SCENE_PRESET

    def matches(self, text: str) -> bool:
        needle = text.strip().lower()
        if not needle:
            return True
        haystack = " ".join((self.display_name, self.description, self.category)).lower()
        return needle in haystack


class MgPackageCatalog:
    """Unified, read-only view over template packages + scene presets."""

    def __init__(self, *, presets: Any = None, template_packages: Iterable[Any] | None = None,
                 generator_registry: Any = None) -> None:
        self._presets = presets
        self._template_packages = template_packages
        self._generator_registry = generator_registry
        self._packages: tuple[MgPackage, ...] = ()

    # -- construction ------------------------------------------------------

    def refresh(self) -> int:
        from ..core.content_localization import template_description, template_display_name
        from ..core.motion_graphic_template_store import discover_stored_templates
        from ..presets.library import MgPresetLibrary
        from ..presets.manifest import localized_preset_description, localized_preset_name

        packages: list[MgPackage] = []
        presets = self._presets if self._presets is not None else MgPresetLibrary()
        for record in presets.records():
            packages.append(MgPackage(
                package_id=record.preset_id,
                kind=KIND_SCENE_PRESET,
                display_name=localized_preset_name(record.manifest),
                description=localized_preset_description(record.manifest),
                category=record.manifest.category,
                version=1,
                origin=record.origin,
                preview_path=record.preview_path,
                source=record,
            ))
        if self._template_packages is not None:
            # Explicitly injected packages (tests, session generators) stay
            # first-class citizens with their own origin.
            for package in self._template_packages:
                template = package.template
                packages.append(MgPackage(
                    package_id=template.template_id,
                    kind=KIND_HTML_TEMPLATE,
                    display_name=template_display_name(template),
                    description=template_description(template),
                    category="html_templates",
                    version=template.version,
                    origin="bundled",
                    preview_path=package.preview_path,
                    source=package,
                ))
        else:
            # R-05: three-tier discovery (project > user > bundled) with the
            # winning origin recorded on the package.
            for stored in discover_stored_templates():
                template = stored.package.template
                packages.append(MgPackage(
                    package_id=template.template_id,
                    kind=KIND_HTML_TEMPLATE,
                    display_name=template_display_name(template),
                    description=template_description(template),
                    category="html_templates",
                    version=template.version,
                    origin=stored.origin,
                    preview_path=stored.package.preview_path,
                    source=stored.package,
                ))
        # Session-registered generators (AI publishes, "Save as Template")
        # extend the disk catalog without restarting it.
        if self._generator_registry is not None:
            known_ids = {p.package_id for p in packages if p.is_html_template}
            for template in self._generator_registry.list_templates():
                if template.template_id in known_ids:
                    continue
                packages.append(MgPackage(
                    package_id=template.template_id,
                    kind=KIND_HTML_TEMPLATE,
                    display_name=template_display_name(template),
                    description=template_description(template),
                    category="html_templates",
                    version=template.version,
                    origin="session",
                    preview_path=None,
                    source=template,
                ))
        self._packages = tuple(sorted(
            packages, key=lambda item: (item.kind, item.category, item.display_name.casefold())))
        return len(self._packages)

    # -- queries -----------------------------------------------------------

    def packages(self, kind: str | None = None) -> tuple[MgPackage, ...]:
        if kind is None:
            return self._packages
        return tuple(package for package in self._packages if package.kind == kind)

    def get(self, package_id: str, kind: str | None = None) -> MgPackage | None:
        for package in self._packages:
            if package.package_id == package_id and (kind is None or package.kind == kind):
                return package
        return None

    def search(self, text: str = "", kind: str | None = None) -> tuple[MgPackage, ...]:
        return tuple(package for package in self.packages(kind) if package.matches(text))

    def categories(self, kind: str | None = None) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for package in self.packages(kind):
            seen.setdefault(package.category, None)
        return tuple(seen)

    def __len__(self) -> int:
        return len(self._packages)


def instantiate_html_template_package(
    package: MgPackage,
    *,
    width: int,
    height: int,
    duration_frames: int,
    fps_num: int = 24,
    fps_den: int = 1,
    params: dict[str, Any] | None = None,
) -> Any:
    """Build a FreeformHtmlMotionLayer from a catalog template package.

    The same ``apply_html_template`` path an Effects-panel drop uses, with the
    canvas geometry re-bound to the host scene — this is the unified drag
    protocol's template branch.
    """
    if not package.is_html_template:
        raise ValueError(f"not an HTML template package: {package.package_id}")
    from dataclasses import replace as dc_replace
    from ..render.html_layer.template_promote import apply_html_template

    template = package.source.template if hasattr(package.source, "template") else package.source
    template = dc_replace(
        template, viewport_width=int(width), viewport_height=int(height),
        duration_frames=int(duration_frames), fps_num=int(fps_num), fps_den=int(fps_den),
    )
    return apply_html_template(
        template, params if params is not None else template.default_params(),
        layer_id=f"tpl_{template.template_id}",
    )


__all__ = [
    "KIND_HTML_TEMPLATE",
    "KIND_SCENE_PRESET",
    "MgPackage",
    "MgPackageCatalog",
    "instantiate_html_template_package",
]
