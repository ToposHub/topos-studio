"""Pure contracts for the optional Teaching Animation module."""

from .contracts import (
    AlphaMode,
    AnimationFrame,
    Capability,
    CanvasSpace,
    ColorSpace,
    FrameAnimationContract,
    FrameContext,
    ProxyTier,
    RenderContext,
    RenderError,
    RenderErrorCode,
    RenderEvidence,
    RenderedFrame,
    RenderPass,
    RenderPlan,
    RenderSurface,
    RendererResult,
    RendererSession,
    SceneJson,
    SceneRendererV2,
)
from .commands import (
    AddAnimationCommand,
    AddElementCommand,
    RemoveElementCommand,
    ReorderElementCommand,
    SceneCommandStack,
    AddComponentInstanceCommand,
    RegisterComponentMasterCommand,
    SetComponentOverrideCommand,
    SetComponentVariantCommand,
    SetDurationCommand,
    SetElementEffectsCommand,
    SetAudioRenderPlanCommand,
    SetCompositionCommand,
    SetProfessionalCapabilitiesCommand,
    SetSceneGpuEffectsCommand,
    SetSceneExpressionsCommand,
    SetParentCommand,
    SetResponsiveVariantsCommand,
    UpdateComponentMasterCommand,
    UpdateElementCommand,
)
from .model import (
    PAYLOAD_SCHEMA_VERSION,
    SCENE_SCHEMA_VERSION,
    MotionGraphicClip,
    MotionGraphicNode,
    SceneAnimation,
    SceneAssetRef,
    SceneCue,
    SceneExpression,
    SceneElement,
    SceneMarker,
    SceneTransform,
    TeachingAnimationProjectPayload,
    TeachingSceneDocument,
)
from .migration import SceneMigrationError, migrate_payload_json, migrate_scene_json
from .template_registry import (
    SceneTemplate,
    SceneTemplateRegistry,
    TemplateCapabilities,
    create_default_template_registry,
)
from .layout import Rect  # noqa: F401 — re-export（tests.core.test_sg07_graphics 使用）
from .design_tokens import BrandTheme, default_brand_tokens  # noqa: F401 — re-export（tests.core.test_w3_templates 使用）
from .executable_template import (
    ExecutableTemplate, ExecutableTemplateRegistry, ParamSpec,  # noqa: F401 — re-export
    TemplateBuildContext,  # noqa: F401 — re-export
)
from .builtin_templates import TITLE_CARD, create_builtin_template_registry  # noqa: F401 — re-export
from .template_commands import (
    ApplyTemplateCommand, ReplaceAssetSlotCommand, SetBrandThemeCommand,  # noqa: F401 — re-export
)
from .html_layer_commands import (
    RemoveHtmlLayerCommand, SetHtmlLayerCommand,  # noqa: F401 — re-export
    build_html_layer_from_pipeline,  # noqa: F401 — re-export
)
from .graphics import (
    AnchorSide, Connector, Path, PathOp, Routing,  # noqa: F401 — re-export
    anchor_point, arrow_path, circle_path, combine_paths, polygon_path,  # noqa: F401 — re-export
    recompute_connectors, rect_path, rounded_rect_path,  # noqa: F401 — re-export
    route_orthogonal,  # noqa: F401 — re-export
)
from .html_layer import FreeformHtmlMotionLayer, HtmlEditSlot, build_html_layer  # noqa: F401 — re-export
from .audio_plan import (
    audio_plan_for_scene,
    read_audio_render_plan, write_audio_render_plan_into,
)
from .composition_schema import write_composition_into
from .professional import (
    ParticleEmitter, SceneColorPipeline, particle_positions, read_particle_emitters,
    read_scene_color_pipeline, write_particle_emitters_into, write_scene_color_pipeline_into,
)
from .style_schema import (
    AlphaCompositing, BlendMode, Gradient, GradientStop, Shadow, Stroke, StyleSchema,  # noqa: F401 — re-export
)
from .hierarchy import (
    Affine2d, Hierarchy,  # noqa: F401 — re-export
    effective_visibility, validate_hierarchy,  # noqa: F401 — re-export
    world_affine,  # noqa: F401 — re-export
)
from .components import (
    ComponentInstance, ComponentMaster, ResponsiveVariant,
    read_active_variants, read_component_instances, read_component_masters,
    read_responsive_variants, resolve_component_updates, resolve_responsive_updates,
)
from .asset_provider import (
    AssetDescriptor, AssetKind, AssetLicenseStatus, AssetPreflightIssue,  # noqa: F401 — re-export
    AssetProviderRegistry, LocalAssetProvider, MotionAssetPack,  # noqa: F401 — re-export（LocalAssetProvider 系 __all__ 成员）
    build_local_asset_provider,
)
from .font_registry import FontEntry, FontLicense, FontRegistry  # noqa: F401 — re-export（tests.core.test_batch_b_provenance 使用）
from .validator import (
    PAYLOAD_JSON_SCHEMA,
    SCENE_JSON_SCHEMA,
    SCENE_SCHEMA,
    SceneValidationError,
    ValidationIssue,
    ValidationResult,
    assert_valid_scene_json,
    validate_scene,
    validate_scene_json,
    validate_project_payload,
)

__all__ = [
    "AlphaMode",
    "AnimationFrame",
    "Capability",
    "CanvasSpace",
    "ColorSpace",
    "FrameAnimationContract",
    "FrameContext",
    "ProxyTier",
    "RenderContext",
    "RenderError",
    "RenderErrorCode",
    "RenderEvidence",
    "RenderedFrame",
    "RenderPass",
    "RenderPlan",
    "RenderSurface",
    "RendererResult",
    "RendererSession",
    "SceneJson",
    "SceneRendererV2",
    "AddAnimationCommand",
    "AddElementCommand",
    "RemoveElementCommand",
    "ReorderElementCommand",
    "SceneCommandStack",
    "AddComponentInstanceCommand",
    "RegisterComponentMasterCommand",
    "SetComponentOverrideCommand",
    "SetComponentVariantCommand",
    "SetDurationCommand",
    "SetElementEffectsCommand",
    "SetAudioRenderPlanCommand",
    "SetCompositionCommand",
    "SetProfessionalCapabilitiesCommand",
    "ParticleEmitter",
    "SceneColorPipeline",
    "particle_positions",
    "read_particle_emitters",
    "read_scene_color_pipeline",
    "write_particle_emitters_into",
    "write_scene_color_pipeline_into",
    "audio_plan_for_scene",
    "read_audio_render_plan",
    "write_audio_render_plan_into",
    "write_composition_into",
    "SetSceneGpuEffectsCommand",
    "SetSceneExpressionsCommand",
    "LocalAssetProvider",
    "build_local_asset_provider",
    "SetParentCommand",
    "SetResponsiveVariantsCommand",
    "UpdateComponentMasterCommand",
    "UpdateElementCommand",
    "ComponentInstance",
    "ComponentMaster",
    "ResponsiveVariant",
    "read_active_variants",
    "read_component_instances",
    "read_component_masters",
    "read_responsive_variants",
    "resolve_component_updates",
    "resolve_responsive_updates",
    "MotionGraphicClip",
    "MotionGraphicNode",
    "SceneAnimation",
    "SceneAssetRef",
    "SceneCue",
    "SceneExpression",
    "SceneElement",
    "SceneMarker",
    "SceneTransform",
    "TeachingAnimationProjectPayload",
    "TeachingSceneDocument",
    "PAYLOAD_SCHEMA_VERSION",
    "SCENE_SCHEMA_VERSION",
    "SceneMigrationError",
    "migrate_payload_json",
    "migrate_scene_json",
    "SceneTemplate",
    "SceneTemplateRegistry",
    "TemplateCapabilities",
    "create_default_template_registry",
    "SceneValidationError",
    "PAYLOAD_JSON_SCHEMA",
    "SCENE_JSON_SCHEMA",
    "SCENE_SCHEMA",
    "ValidationIssue",
    "ValidationResult",
    "assert_valid_scene_json",
    "validate_scene",
    "validate_scene_json",
    "validate_project_payload",
]
