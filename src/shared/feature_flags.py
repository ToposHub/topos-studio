import os
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_FEATURE_FLAGS: dict[str, bool] = {
    # "library" 门控的是应用级 Library 页面（src/app/ui/library_panel），
    # 不是 src/features/<module> —— features/ 下没有同名目录属正常（R3-5）。
    "library": True,
    "color": True,
    "edit": True,
    "composite": True,
    "deliver": True,
    # Teaching Animation ships with the default Studio runtime so its
    # timeline/Assistant host actions are available immediately.  Product
    # presets and TOPOS_FEATURE_TEACHING_ANIMATION can still disable it.
    "teaching_animation": True,
    # AI Assistant capability flags (M0 freeze).
    # These gate every AI/automation surface and MUST default to False so that
    # importing the app or switching product mode never silently turns on
    # analysis, cloud upload, generation or external MCP. They are only enabled
    # via an explicit TOPOS_FEATURE_* env var or an explicit config entry.
    # See docs/ai_assistant_agent_harness_product_plan_2026-08-01.md §11.1 M0.
    "ai_assistant": False,
    "media_analysis": False,
    "cloud_analysis": False,
    "generation": False,
    "mcp_server": False,
    # Commercial automation controls (P0): every high-impact action is opt-in.
    "agent_auto_mode": False,
    "cloud_upload": False,
    "auto_export": False,
    # AI Workflow node-canvas page (P8). Gates the AI Workflow dock + module
    # registration. The node canvas is an advanced surface for power users;
    # the dock ships hidden by default (default_visible=False), so turning
    # the feature on does not clutter the Edit page — users opt-in via
    # Window > Panels → Edit → "AI Workflow".
    # Override to off with TOPOS_FEATURE_AI_WORKFLOW=0 or a config entry.
    "ai_workflow": True,
}

#: The AI/automation capability flags registered above. Used by tests, docs and
#: any UI that needs to enumerate the core AI capability surface.
AI_CAPABILITY_FLAGS: tuple[str, ...] = (
    "ai_assistant",
    "media_analysis",
    "cloud_analysis",
    "generation",
    "mcp_server",
)

#: High-impact commercial actions.  Kept separate from the legacy five-flag AI
#: contract so existing integrations can enumerate the original surface while
#: these new P0 controls remain independently opt-in.
COMMERCIAL_AUTOMATION_FLAGS: tuple[str, ...] = (
    "agent_auto_mode",
    "cloud_upload",
    "auto_export",
)

#: First stem shared by every AI/automation capability id ("ai_assistant" ->
#: "ai", "media_analysis" -> "media", ...).  ``FeatureFlags.is_enabled`` uses
#: these stems to fail CLOSED for an *unregistered* id that still looks like
#: a capability reference (e.g. "media_analysys", "cloud_analyse") — a typo
#: must never silently open an AI/automation surface (M0 freeze, AUD-33700).
_CAPABILITY_ID_STEMS: frozenset[str] = frozenset(
    name.split("_", 1)[0]
    for name in AI_CAPABILITY_FLAGS + COMMERCIAL_AUTOMATION_FLAGS
)


def _ai_capability_defaults() -> dict[str, bool]:
    """AI capability flags all default to off in every product mode."""
    return {name: False for name in AI_CAPABILITY_FLAGS}


# Product mode presets: mode -> {feature: enabled}
PRODUCT_MODE_PRESETS: dict[str, dict[str, bool]] = {
    "color": {
        "library": True,
        "color": True,
        "edit": False,
        "composite": False,
        "deliver": True,
        "teaching_animation": False,
        **_ai_capability_defaults(),
    },
    "studio": {
        "library": True,
        "color": True,
        "edit": True,
        "composite": True,
        "deliver": True,
        "teaching_animation": True,
        **_ai_capability_defaults(),
    },
    "edit": {
        "library": True,
        "color": True,
        "edit": True,
        "composite": False,
        "deliver": True,
        # Edit can load an existing Motion Graphic Clip, but does not expose
        # the Teaching Animation editor by default.
        "teaching_animation": False,
        **_ai_capability_defaults(),
    },
    "composite": {
        "library": True,
        "color": True,
        "edit": False,
        "composite": True,
        "deliver": True,
        "teaching_animation": True,
        **_ai_capability_defaults(),
    },
}

DEFAULT_PRODUCT_MODE = "studio"

# Work Mode: a studio-only preference that scopes the product's UI to either
# image grading or video grading. It is only honored when the product mode is
# "studio"; other launchers ignore it. There is no "full" mode — every studio
# user picks exactly one workflow, which keeps the UI focused and the codebase
# free of three-way branches.
#   "image" — image grading: hide the Edit page; the Composite page keeps its
#             node effects but disables keyframe/animation inputs.
#   "video" — video grading: timeline-driven workflows; all features on.
#             (default — video grading is the richer workflow and the typical
#             entry point for new users.)
WORK_MODES: tuple[str, ...] = ("image", "video")
DEFAULT_WORK_MODE = "video"

# Work-mode feature overrides applied on top of the product-mode preset. Only
# effective for studio; only edit is toggled here (composite stays on so users
# can still build static effects on an image). TOPOS_FEATURE_* env vars still
# win over this (see load_feature_flags Step 7).
WORK_MODE_FEATURE_OVERRIDES: dict[str, dict[str, bool]] = {
    "image": {
        "edit": False,
    },
    "video": {},
}

PRODUCT_MODE_ALIASES: dict[str, str] = {
    "topos-studio": "studio",
    "topos-color": "color",
    "topos-edit": "edit",
    "topos-composite": "composite",
}


def normalize_product_mode(product_mode: str | None) -> str:
    """Normalize short and packaged product names to feature-flag presets."""
    mode = (product_mode or DEFAULT_PRODUCT_MODE).strip().lower()
    if not mode:
        mode = DEFAULT_PRODUCT_MODE
    return PRODUCT_MODE_ALIASES.get(mode, mode)


@dataclass(frozen=True)
class FeatureFlags:
    library: bool = True
    color: bool = True
    edit: bool = True
    composite: bool = True
    deliver: bool = True
    teaching_animation: bool = False
    # AI Assistant capability flags default to False (M0 freeze). Unlike the
    # product-module flags above, they are never implicitly enabled: importing
    # the app, changing product mode or referencing an unknown capability must
    # not surface AI analysis/generation/MCP without explicit opt-in.
    ai_assistant: bool = False
    media_analysis: bool = False
    cloud_analysis: bool = False
    generation: bool = False
    mcp_server: bool = False
    agent_auto_mode: bool = False
    cloud_upload: bool = False
    auto_export: bool = False
    # AI Workflow node canvas: advanced surface, ships on but dock stays hidden
    # (default_visible=False) — users opt-in via Window > Panels.
    ai_workflow: bool = True

    def is_enabled(self, feature_id: str) -> bool:
        if feature_id == "color":
            return True
        # AUD-33700 / M0 freeze: an id that is NOT a registered field but
        # looks like an AI/automation capability reference (shares its first
        # stem, e.g. "media_analysys", "ai_assistants", "cloud_analysis_v2")
        # must fail CLOSED — a typo can never silently open an AI surface.
        # Unrecognized plain UI ids (e.g. display-side page prefixes such as
        # "app") stay permissive (True) for backward compatibility.
        if not hasattr(self, feature_id):
            if feature_id.split("_", 1)[0] in _CAPABILITY_ID_STEMS:
                logger.warning(
                    "Unknown AI/automation capability id %r treated as "
                    "disabled (fail-closed, M0 freeze)",
                    feature_id,
                )
                return False
            logger.debug(
                "Unknown feature id %r is not registered; treating as "
                "enabled (permissive legacy default)",
                feature_id,
            )
            return True
        return getattr(self, feature_id)

    def is_available(self, feature_id: str) -> bool:
        return self.is_enabled(feature_id)

    def get_enabled_features(self) -> list[str]:
        return [f for f in DEFAULT_FEATURE_FLAGS.keys() if self.is_enabled(f)]

    def get_disabled_features(self) -> list[str]:
        return [f for f in DEFAULT_FEATURE_FLAGS.keys() if not self.is_enabled(f)]


def get_product_mode() -> str:
    """Get the current product mode from environment or default."""
    return normalize_product_mode(os.environ.get("TOPOS_PRODUCT_MODE"))


def get_work_mode(config: Any = None) -> str:
    """Resolve the work mode ("image" or "video").

    Work mode only applies to the studio product; every other launcher returns
    the default (video) because it keeps its own PRODUCT_MODE preset
    authoritative. The value comes from the persisted ``work_mode`` config
    key; an unrecognised value (e.g. a legacy "all") falls back to the default.
    """
    if get_product_mode() != "studio":
        return DEFAULT_WORK_MODE
    if config is not None and hasattr(config, "get"):
        try:
            mode = config.get("work_mode")
        except TypeError:
            # ConfigManager.get takes a single arg (no default); fall back.
            mode = None
        if isinstance(mode, str) and mode in WORK_MODES:
            return mode
    return DEFAULT_WORK_MODE


def get_effective_layout_mode(config: Any = None) -> str:
    """The composite key used to scope persisted page layouts.

    Returns the product mode unchanged for non-studio launchers. For studio it
    always appends the work mode (``studio_image`` / ``studio_video``) so the
    two workflows keep fully independent layouts.
    """
    product = get_product_mode()
    if product != "studio":
        return product
    work_mode = get_work_mode(config)
    return f"{product}_{work_mode}"



def load_feature_flags(config: Any = None) -> FeatureFlags:
    """Load feature flags with clear precedence:

    1. TOPOS_FEATURE_<NAME> explicit env var (highest priority)
    2. TOPOS_DISABLE_FEATURES bulk disable
    3. Product mode preset (only if TOPOS_PRODUCT_MODE is explicitly set;
       never overrides an AI capability flag the config set explicitly —
       the module contract promises an explicit config entry as an
       enablement path for these opt-in capabilities, AUD-33701)
    4. config file values
    5. DEFAULT_FEATURE_FLAGS defaults (lowest priority)
    6. Color is always enabled regardless
    """
    flags = DEFAULT_FEATURE_FLAGS.copy()
    # Keys the config set explicitly.  Used below to keep the product-mode
    # preset from silently flipping an AI capability the user opted into
    # via config back off (AUD-33701).
    explicit_config_keys: set[str] = set()

    # Step 4: Apply config values
    if config:
        if hasattr(config, 'get'):
            config_features = config.get("features")
            if config_features and isinstance(config_features, dict):
                for key, value in config_features.items():
                    if key in flags:
                        flags[key] = bool(value)
                        explicit_config_keys.add(key)

    # Step 3: Apply product mode preset (only when explicitly set, overrides
    # config — except for explicitly configured AI capability flags, see
    # docstring note 3).
    product_mode_env = os.environ.get("TOPOS_PRODUCT_MODE")
    if product_mode_env is not None:
        product_mode = normalize_product_mode(product_mode_env)
        if product_mode in PRODUCT_MODE_PRESETS:
            for key, value in PRODUCT_MODE_PRESETS[product_mode].items():
                if (
                    key in AI_CAPABILITY_FLAGS
                    and key in explicit_config_keys
                    and flags[key] != value
                ):
                    logger.info(
                        "Product mode preset %r keeps config value for AI "
                        "capability %r (explicit config entry wins over the "
                        "preset default)",
                        product_mode,
                        key,
                    )
                    continue
                flags[key] = value

    # Step 2: Bulk disable via TOPOS_DISABLE_FEATURES (overrides product mode)
    disabled_str = os.environ.get("TOPOS_DISABLE_FEATURES", "")
    if disabled_str:
        for name in disabled_str.split(","):
            name = name.strip()
            if name in flags:
                flags[name] = False

    # Step 1: Per-feature TOPOS_FEATURE_* env vars (highest priority, overrides all)
    for feature in flags.keys():
        env_var = f"TOPOS_FEATURE_{feature.upper()}"
        env_value = os.environ.get(env_var)
        if env_value is not None:
            flags[feature] = env_value.lower() not in ("0", "false", "no", "disabled")

    # Step 6: Color cannot be disabled
    if flags.get("color") is False:
        logger.warning("Color feature cannot be disabled - it is the core functionality")
        flags["color"] = True

    # Step 7: Apply work_mode override (studio-only). Work mode further
    # restricts the product-mode preset (e.g. image grading hides Edit). It
    # never overrides an explicit TOPOS_FEATURE_* env var, and Color stays on.
    work_mode = get_work_mode(config)
    for feature, enabled in WORK_MODE_FEATURE_OVERRIDES.get(work_mode, {}).items():
        env_var = f"TOPOS_FEATURE_{feature.upper()}"
        if os.environ.get(env_var) is None and feature in flags:
            flags[feature] = enabled

    return FeatureFlags(
        library=flags["library"],
        color=flags["color"],
        edit=flags["edit"],
        composite=flags["composite"],
        deliver=flags["deliver"],
        teaching_animation=flags["teaching_animation"],
        ai_assistant=flags["ai_assistant"],
        media_analysis=flags["media_analysis"],
        cloud_analysis=flags["cloud_analysis"],
        generation=flags["generation"],
        mcp_server=flags["mcp_server"],
        agent_auto_mode=flags["agent_auto_mode"],
        cloud_upload=flags["cloud_upload"],
        auto_export=flags["auto_export"],
        ai_workflow=flags["ai_workflow"],
    )
