"""Disk-backed packages for reusable Motion Graphic generator templates.

Each direct child of :func:`default_motion_graphic_template_root` is one
portable template package.  A package contains a ``manifest.json``, its HTML
source, and (optionally) a pre-rendered GIF preview.  Keeping every artifact
next to the manifest means authors can migrate or share a template by copying
one folder, without embedding machine paths in project data.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
from typing import Any, Mapping

from .executable_template import ParamSpec
from .html_template import HtmlTemplate
from .motion_graphic_generator_registry import MotionGraphicGeneratorRegistry


logger = logging.getLogger(__name__)

_MANIFEST_NAME = "manifest.json"
# Hard size ceilings keep a hostile or broken template package from loading
# megabytes of JSON/HTML into memory before validation can reject it.
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_TEMPLATE_BYTES = 4 * 1024 * 1024
_MAX_PREVIEW_BYTES = 32 * 1024 * 1024
_REQUIRED_METADATA = {
    "render_profile": "motion_graphic_freeform",
    "allow_inline_scripts": True,
    "allow_freeform_css": True,
}


@dataclass(frozen=True)
class MotionGraphicTemplatePackage:
    """One validated template directory and the runtime recipe it provides."""

    root: Path
    template: HtmlTemplate
    preview_path: Path | None = None


def default_motion_graphic_template_root() -> Path:
    """Return the bundled, copyable Motion Graphic template-library root."""

    return Path(__file__).resolve().parent.parent / "templates" / "motion_graphic"


def _safe_package_file(package_root: Path, value: Any, field: str, *, max_bytes: int) -> Path:
    """Resolve a manifest file name without allowing it to escape its package."""

    relative = Path(str(value or "")).expanduser()
    if not str(relative) or relative.is_absolute():
        raise ValueError(f"{field} must be a relative package path")
    resolved_root = package_root.resolve()
    resolved = (resolved_root / relative).resolve()
    if resolved_root not in resolved.parents:
        raise ValueError(f"{field} must stay inside the template package")
    if not resolved.is_file():
        raise ValueError(f"{field} does not exist: {relative}")
    size = resolved.stat().st_size
    if size > max_bytes:
        raise ValueError(f"{field} exceeds the {max_bytes // 1024} KiB limit: {relative}")
    return resolved


def _param_specs(raw_params: Any) -> tuple[ParamSpec, ...]:
    if not isinstance(raw_params, list):
        raise ValueError("params_schema must be a list")
    specs: list[ParamSpec] = []
    for raw in raw_params:
        if not isinstance(raw, Mapping):
            raise ValueError("each params_schema entry must be an object")
        name = str(raw.get("name", "")).strip()
        param_type = str(raw.get("type", "")).strip()
        if not name or not param_type:
            raise ValueError("each parameter needs a name and type")
        enum = raw.get("enum", ())
        if not isinstance(enum, (list, tuple)):
            raise ValueError(f"parameter {name!r} enum must be a list")
        specs.append(ParamSpec(
            name=name,
            type=param_type,
            default=raw.get("default"),
            required=bool(raw.get("required", False)),
            enum=tuple(str(item) for item in enum),
            description=str(raw.get("description", "")),
        ))
    return tuple(specs)


def load_motion_graphic_template_package(package_root: Path) -> MotionGraphicTemplatePackage:
    """Load and validate one self-contained template directory.

    The manifest deliberately has a compact public schema so third-party
    authors do not need to import Python classes to make a template package.
    """

    package_root = Path(package_root).resolve()
    manifest_path = package_root / _MANIFEST_NAME
    if not manifest_path.is_file():
        raise ValueError(f"template package has no {_MANIFEST_NAME}: {package_root}")
    if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise ValueError("manifest.json exceeds the 256 KiB limit")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {manifest_path.name}: {exc.msg}") from exc
    if not isinstance(raw, Mapping):
        raise ValueError("manifest root must be an object")
    if int(raw.get("schema_version", 0)) != 1:
        raise ValueError("unsupported or missing template manifest schema_version")

    html_path = _safe_package_file(
        package_root, raw.get("template_file"), "template_file", max_bytes=_MAX_TEMPLATE_BYTES)
    preview_value = raw.get("preview_file")
    if preview_value is None:
        raise ValueError("preview_file is required for hover preview")
    preview_path = _safe_package_file(
        package_root, preview_value, "preview_file", max_bytes=_MAX_PREVIEW_BYTES)
    if preview_path.suffix.lower() != ".gif":
        raise ValueError("preview_file must be a GIF")

    metadata = dict(_REQUIRED_METADATA)
    raw_metadata = raw.get("metadata", {})
    if not isinstance(raw_metadata, Mapping):
        raise ValueError("metadata must be an object")
    metadata.update(dict(raw_metadata))
    # Optional per-locale display text: resolved at UI time against the
    # current language, with the base manifest string as final fallback.
    from .content_localization import sanitized_i18n_dict
    for field in ("display_name", "description"):
        values = sanitized_i18n_dict(raw.get(f"{field}_i18n"))
        if values:
            metadata[f"{field}_i18n"] = values
    template = HtmlTemplate(
        template_id=str(raw.get("template_id", "")).strip(),
        display_name=str(raw.get("display_name", "")).strip(),
        description=str(raw.get("description", "")),
        html_template=html_path.read_text(encoding="utf-8"),
        params_schema=_param_specs(raw.get("params_schema", [])),
        viewport_width=int(raw.get("viewport_width", 1920)),
        viewport_height=int(raw.get("viewport_height", 1080)),
        version=int(raw.get("version", 1)),
        fps_num=int(raw.get("fps_num", 24)),
        fps_den=int(raw.get("fps_den", 1)),
        duration_frames=int(raw.get("duration_frames", 240)),
        metadata=metadata,
    )
    # AUD-27702: third-party manifests are external data.  HtmlTemplate does
    # not validate viewport/fps/duration, so zero/negative values used to
    # register fine and only blew up downstream (e.g. Fraction(num, 0)) when
    # the template was instantiated.  Reject them at load so discover can
    # drop the package with a diagnosable warning.
    for field in ("viewport_width", "viewport_height", "fps_num", "fps_den", "duration_frames"):
        if getattr(template, field) <= 0:
            raise ValueError(f"{field} must be a positive integer")
    return MotionGraphicTemplatePackage(package_root, template, preview_path)


def discover_motion_graphic_template_packages(
    root: Path | None = None,
) -> tuple[MotionGraphicTemplatePackage, ...]:
    """Discover valid package folders, logging invalid third-party packages."""

    library_root = Path(root) if root is not None else default_motion_graphic_template_root()
    if not library_root.is_dir():
        return ()
    packages: list[MotionGraphicTemplatePackage] = []
    for child in sorted(library_root.iterdir(), key=lambda path: path.name.casefold()):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        try:
            packages.append(load_motion_graphic_template_package(child))
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Ignoring invalid Motion Graphic template package %s: %s", child, exc)
    return tuple(packages)


def load_motion_graphic_template_library(
    registry: MotionGraphicGeneratorRegistry,
    root: Path | None = None,
) -> tuple[MotionGraphicTemplatePackage, ...]:
    """Register every package and retain only runtime preview paths in memory."""

    packages = discover_motion_graphic_template_packages(root)
    for package in packages:
        registry.register(
            package.template,
            preview_path=package.preview_path,
            packaged=True,
        )
    return packages


__all__ = [
    "MotionGraphicTemplatePackage",
    "default_motion_graphic_template_root",
    "discover_motion_graphic_template_packages",
    "load_motion_graphic_template_library",
    "load_motion_graphic_template_package",
]
