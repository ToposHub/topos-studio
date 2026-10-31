"""Project-independent registry for reusable Motion Graphic generator templates.

The Edit Effects panel consumes this registry as a palette.  A template is not
an asset in the Media Pool: it is a versioned recipe that becomes a
scene-backed clip only when the user drops it onto the timeline.
"""

from __future__ import annotations

from pathlib import Path
from threading import RLock
from typing import Callable

from .html_template import HtmlTemplate


MOTION_GRAPHIC_GENERATOR_MIME = "application/x-topos-motion-graphic-generator"


class MotionGraphicGeneratorRegistry:
    """Small observable registry shared by the editor and Effects panel."""

    def __init__(self) -> None:
        self._templates: dict[tuple[str, int], HtmlTemplate] = {}
        # Preview locations are deliberately runtime-only.  They remain next
        # to their package on disk and are never serialized into a .topos
        # project as absolute machine paths.
        self._preview_paths: dict[tuple[str, int], Path] = {}
        self._packaged_keys: set[tuple[str, int]] = set()
        self._latest: dict[str, int] = {}
        self._listeners: list[Callable[[], None]] = []
        self._lock = RLock()

    def register(
        self,
        template: HtmlTemplate,
        *,
        preview_path: Path | None = None,
        packaged: bool = False,
    ) -> None:
        if not isinstance(template, HtmlTemplate):
            raise TypeError("Motion Graphic generator must be an HtmlTemplate")
        with self._lock:
            key = (template.template_id, template.version)
            self._templates[key] = template
            if preview_path is None:
                self._preview_paths.pop(key, None)
            else:
                self._preview_paths[key] = Path(preview_path)
            if packaged:
                self._packaged_keys.add(key)
            else:
                self._packaged_keys.discard(key)
            self._latest[template.template_id] = max(
                template.version, self._latest.get(template.template_id, 0)
            )
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener()
            except Exception:
                # A palette refresh must never break template registration.
                pass

    def get(self, template_id: str, version: int | None = None) -> HtmlTemplate | None:
        with self._lock:
            if version is None:
                version = self._latest.get(str(template_id))
            if version is None:
                return None
            return self._templates.get((str(template_id), int(version)))

    def list_templates(self) -> tuple[HtmlTemplate, ...]:
        with self._lock:
            return tuple(
                self._templates[(template_id, version)]
                for template_id, version in sorted(self._latest.items())
                if (template_id, version) in self._templates
            )

    def preview_path(self, template_id: str, version: int | None = None) -> Path | None:
        """Return the local packaged GIF for the requested/latest template."""

        with self._lock:
            if version is None:
                version = self._latest.get(str(template_id))
            if version is None:
                return None
            return self._preview_paths.get((str(template_id), int(version)))

    def is_packaged(self, template_id: str, version: int) -> bool:
        """Whether this exact template version came from the disk library."""

        with self._lock:
            return (str(template_id), int(version)) in self._packaged_keys

    def list_project_templates(self) -> tuple[HtmlTemplate, ...]:
        """List latest templates that must travel with a project file."""

        with self._lock:
            return tuple(
                self._templates[(template_id, version)]
                for template_id, version in sorted(self._latest.items())
                if (template_id, version) in self._templates
                and (template_id, version) not in self._packaged_keys
            )

    def clear(self) -> None:
        with self._lock:
            self._templates.clear()
            self._preview_paths.clear()
            self._packaged_keys.clear()
            self._latest.clear()
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener()
            except Exception:
                pass

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe


_REGISTRY = MotionGraphicGeneratorRegistry()


def get_motion_graphic_generator_registry() -> MotionGraphicGeneratorRegistry:
    return _REGISTRY


def register_motion_graphic_generator(template: HtmlTemplate) -> None:
    _REGISTRY.register(template)


__all__ = [
    "MOTION_GRAPHIC_GENERATOR_MIME",
    "MotionGraphicGeneratorRegistry",
    "get_motion_graphic_generator_registry",
    "register_motion_graphic_generator",
]
