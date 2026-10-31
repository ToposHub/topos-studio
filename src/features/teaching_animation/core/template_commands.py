"""TM-02 (W3 subset): high-level template + brand commands.

These commands complete the AI-facing structured-scene facade alongside the W1
motion commands:

* ``ApplyTemplateCommand`` — instantiate + compile a template into a scene,
  storing the ``TemplateInstance`` so the scene stays parameter-driven.
* ``SetBrandThemeCommand`` — overlay a brand theme; if the scene came from a
  template it re-compiles with the themed tokens (so one colour change updates
  every reference), otherwise it re-resolves token references in styles.
* ``ReplaceAssetSlotCommand`` — swap an element's bound asset + register it.

All are immutable, undoable, and produce readable diffs.  They never write
low-level keyframes or hand-built DOM; AI emits intent, the core owns state.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping
import uuid

from .design_tokens import BrandTheme, DesignTokenSet, resolve_style, validate_theme
from .executable_template import ExecutableTemplateRegistry, TemplateBuildContext, TemplateInstance
from .model import SceneAssetRef, TeachingSceneDocument


_TEMPLATE_INSTANCE_KEY = "template_instance"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class ApplyTemplateCommand:
    """Compile a template into a scene, recording its instance for re-edits."""

    template_id: str
    params: Mapping[str, Any] = field(default_factory=dict)
    registry: ExecutableTemplateRegistry | None = None
    context: TemplateBuildContext | None = None
    command_id: str = field(default_factory=lambda: _new_id("apply_template"))
    label: str = "Apply Template"
    _previous: TeachingSceneDocument | None = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from .builtin_templates import create_builtin_template_registry
        registry = self.registry or create_builtin_template_registry()
        instance = registry.instantiate(self.template_id, self.params, context=self.context)
        compiled = registry.compile(instance, self.context)
        # Carry the template instance so brand/edit commands can re-compile.
        new_metadata = {**dict(compiled.metadata), _TEMPLATE_INSTANCE_KEY: instance.to_dict()}
        compiled = replace(compiled, metadata=new_metadata)
        object.__setattr__(self, "_previous", scene)
        return compiled

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous is None:
            raise RuntimeError("apply_template has not been applied")
        return self._previous


@dataclass(frozen=True)
class SetBrandThemeCommand:
    """Overlay a brand theme; re-compiles template scenes with themed tokens."""

    theme: BrandTheme
    base_tokens: DesignTokenSet | None = None
    registry: ExecutableTemplateRegistry | None = None
    command_id: str = field(default_factory=lambda: _new_id("set_brand_theme"))
    label: str = "Set Brand Theme"
    _previous: TeachingSceneDocument | None = None
    _conflict: tuple[str, ...] = ()

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        from .design_tokens import default_brand_tokens
        from .builtin_templates import create_builtin_template_registry
        base = self.base_tokens or default_brand_tokens()
        conflict = validate_theme(base, self.theme)
        object.__setattr__(self, "_conflict", conflict.unresolved)
        object.__setattr__(self, "_previous", scene)
        themed = base.overlay(self.theme)

        instance_data = scene.metadata.get(_TEMPLATE_INSTANCE_KEY) if isinstance(scene.metadata, Mapping) else None
        if isinstance(instance_data, Mapping):
            registry = self.registry or create_builtin_template_registry()
            instance = TemplateInstance.from_dict(instance_data)
            # Re-compile the same instance against the themed token set.
            themed_context = replace(instance.context, tokens=themed)
            recompiled = registry.compile(instance, themed_context)
            # AUD-27717: the recompiled document only owns the template's own
            # elements.  Post-template edits on the live scene (added/removed
            # elements, cues, markers, pages, asset refs, html_layer and the
            # rest of its metadata) must survive the re-theme instead of being
            # silently discarded with the whole-document swap.
            themed_by_id = {element.element_id: element for element in recompiled.elements}
            themed_elements = tuple(
                themed_by_id.get(element.element_id, element)
                for element in scene.elements
            )
            new_metadata = {**dict(recompiled.metadata), **dict(scene.metadata),
                            _TEMPLATE_INSTANCE_KEY: instance.to_dict(),
                            "brand_theme": self.theme.theme_id}
            return replace(scene, elements=themed_elements, metadata=new_metadata)
        # Non-template scene: re-resolve any lingering token references in styles.
        new_elements = tuple(
            replace(element, style=resolve_style(element.style, themed)) for element in scene.elements
        )
        new_metadata = {**dict(scene.metadata), "brand_theme": self.theme.theme_id}
        return replace(scene, elements=new_elements, metadata=new_metadata)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous is None:
            raise RuntimeError("set_brand_theme has not been applied")
        return self._previous

    @property
    def unresolved_tokens(self) -> tuple[str, ...]:
        """Token references the theme expects but the base does not define."""
        return self._conflict


@dataclass(frozen=True)
class ReplaceAssetSlotCommand:
    """Swap an element's bound asset and register the new asset ref."""

    element_id: str
    asset: SceneAssetRef
    command_id: str = field(default_factory=lambda: _new_id("replace_asset"))
    label: str = "Replace Asset Slot"
    _previous_asset_ref: str | None = None
    _previous_refs: tuple[SceneAssetRef, ...] = ()
    _added: bool = False

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        element = scene.element_by_id(self.element_id)
        if element is None:
            raise ValueError(f"Element not found: {self.element_id}")
        object.__setattr__(self, "_previous_asset_ref", element.asset_ref)
        object.__setattr__(self, "_previous_refs", scene.asset_refs)
        updated_element = replace(element, asset_ref=self.asset.asset_id)
        updated = scene.with_element_updated(updated_element)
        existing_ids = {ref.asset_id for ref in scene.asset_refs}
        if self.asset.asset_id not in existing_ids:
            updated = replace(updated, asset_refs=updated.asset_refs + (self.asset,))
            object.__setattr__(self, "_added", True)
        return updated

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        element = scene.element_by_id(self.element_id)
        if element is None:
            raise ValueError(f"Element not found: {self.element_id}")
        restored = replace(element, asset_ref=self._previous_asset_ref)
        updated = scene.with_element_updated(restored)
        if self._added:
            updated = replace(updated, asset_refs=self._previous_refs)
        return updated


__all__ = [
    "ApplyTemplateCommand",
    "ReplaceAssetSlotCommand",
    "SetBrandThemeCommand",
]
