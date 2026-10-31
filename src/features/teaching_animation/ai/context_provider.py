"""ContextProvider for the shared AI Assistant."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import logging
from typing import Any, Mapping, Protocol

from ..core.model import MotionGraphicClip, MotionGraphicNode, TeachingSceneDocument


logger = logging.getLogger(__name__)


class ContextProvider(Protocol):
    provider_id: str

    def get_context(self) -> Mapping[str, Any]:
        ...


TeachingAIContextProvider = ContextProvider


@lru_cache(maxsize=1)
def _ai_capability_catalog() -> dict[str, Any]:
    """TM-06: the registry-derived capability surface advertised to the model.

    Built once (it is scene-independent) and cached: the full edit-command
    vocabulary, every built-in template's parameter schema, and the available
    motion-preset ids.  Exporting this means the model learns each template's
    params and the exact command set instead of a stale hand-maintained list —
    and a registry change (new template / preset / command) reaches the prompt
    without a code edit here.

    The cache is process-lifetime by default; when a host registers new
    templates / presets / commands at runtime it must call
    :func:`invalidate_ai_capability_catalog` so the next prompt rebuilds the
    catalog from the updated registries instead of serving the frozen
    first-frame snapshot (AUD-26714).
    """
    from .command_catalog import EXTENDED_COMMAND_NAMES
    commands = ("add_element", "update_element", "reorder_element", "set_animation") + tuple(
        sorted(EXTENDED_COMMAND_NAMES))
    degraded: list[str] = []
    # Template parameter schemas.
    templates: list[dict[str, Any]] = []
    try:
        from ..core.builtin_templates import create_builtin_template_registry
        registry = create_builtin_template_registry()
        for template in registry.list_templates():
            templates.append(registry.schema_for_ai(template.template_id))
    except ImportError:
        # Optional capability module absent in a trimmed build: a legitimate
        # degraded mode, not an error worth a log record.
        degraded.append("templates")
    except Exception:
        # First-party registry code failed (a real bug): never block the AI,
        # but never swallow it silently either (AUD-26713).
        degraded.append("templates")
        logger.exception(
            "AI capability catalog: built-in template registry build failed; "
            "template schemas will be missing from the prompt"
        )
    # Motion preset ids.
    motion_presets: tuple[str, ...] = ()
    try:
        from ..motion.presets import create_default_preset_registry
        motion_presets = create_default_preset_registry().ids()
    except ImportError:
        degraded.append("motion_presets")
    except Exception:
        degraded.append("motion_presets")
        logger.exception(
            "AI capability catalog: default motion preset registry build failed; "
            "preset ids will be missing from the prompt"
        )
    return {
        "commands": commands,
        "templates": templates,
        "motion_presets": motion_presets,
        "degraded": tuple(degraded),
    }


def invalidate_ai_capability_catalog() -> None:
    """Drop the cached capability catalog so it rebuilds on next use (AUD-26714).

    ``_ai_capability_catalog`` is ``lru_cache``-d for prompt-build speed, which
    would otherwise freeze the advertised capability surface for the whole
    process.  A host that mutates a registry at runtime (plugin template,
    custom motion preset, extended command) calls this once after the mutation;
    the next ``get_context()`` re-derives the catalog, keeping the "registry
    changes reach the prompt" contract true.
    """

    _ai_capability_catalog.cache_clear()


@dataclass(frozen=True)
class TeachingSceneContext:
    project_id: str | None
    scene: Mapping[str, Any]
    selected_element_id: str | None
    clip_references: tuple[Mapping[str, Any], ...]
    node_references: tuple[Mapping[str, Any], ...]
    capabilities: tuple[str, ...]
    #: The full edit-command vocabulary + template parameter schemas + motion
    #: preset ids, derived from the registries (TM-06).  Empty until first built.
    commands: tuple[str, ...] = ()
    templates: tuple[Mapping[str, Any], ...] = ()
    motion_presets: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "scene": dict(self.scene),
            "selected_element_id": self.selected_element_id,
            "clip_references": [dict(item) for item in self.clip_references],
            "node_references": [dict(item) for item in self.node_references],
            "capabilities": list(self.capabilities),
            "commands": list(self.commands),
            "templates": [dict(item) for item in self.templates],
            "motion_presets": list(self.motion_presets),
        }


class TeachingSceneContextProvider:
    """Expose only the active scene and explicit host references to AI."""

    provider_id = "teaching_animation.scene"

    def __init__(self, scene: TeachingSceneDocument, *, project_id: str | None = None,
                 selected_element_id: str | None = None,
                 clips: tuple[MotionGraphicClip, ...] = (),
                 nodes: tuple[MotionGraphicNode, ...] = ()) -> None:
        self._scene = scene
        self._project_id = project_id
        self._selected_element_id = selected_element_id
        self._clips = clips
        self._nodes = nodes

    @property
    def scene(self) -> TeachingSceneDocument:
        return self._scene

    def set_scene(self, scene: TeachingSceneDocument) -> None:
        self._scene = scene

    def select_element(self, element_id: str | None) -> None:
        if element_id is not None and self._scene.element_by_id(element_id) is None:
            raise ValueError(f"Element not found: {element_id}")
        self._selected_element_id = element_id

    def set_references(self, *, clips: tuple[MotionGraphicClip, ...] = (), nodes: tuple[MotionGraphicNode, ...] = ()) -> None:
        self._clips = clips
        self._nodes = nodes

    def get_context(self) -> Mapping[str, Any]:
        catalog = _ai_capability_catalog()
        return TeachingSceneContext(
            project_id=self._project_id,
            scene=self._scene.to_dict(),
            selected_element_id=self._selected_element_id,
            clip_references=tuple(clip.to_dict() for clip in self._clips),
            node_references=tuple(node.to_dict() for node in self._nodes),
            # High-level verbs the host supports (distinct from the edit-command
            # vocabulary, which is exported in ``commands``).
            capabilities=(
                "create_scene", "render_preview", "insert_clip", "replace_clip",
                "generate_html", "apply_template", "apply_motion_preset",
            ),
            commands=catalog["commands"],
            templates=catalog["templates"],
            motion_presets=catalog["motion_presets"],
        ).to_dict()
