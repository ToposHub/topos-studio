"""
Base node classes for the composite node graph.

Provides the fundamental data model: Port, Connection, TextureResult, and Node.
All specific node types inherit from Node.

Note: CacheEntry is now defined in cache_manager.py for category-aware caching.

EFFECT MARKER PROTOCOL:
Some GPU-backed nodes do not render directly in _do_evaluate(). Instead, they
return a transient marker dict that GraphEvaluatorMixin._resolve_output()
consumes and replaces with real TextureResult outputs.

Required marker shape:
    {
        "<declared_output_port>": TextureResult(
            texture_id=-1,
            width=...,
            height=...,
        ),
        "_effect_type": "<stable snake_case dispatcher key>",
        "_effect_params": {...},
    }

Single-input effects must also include "_effect_input" with the primary
TextureResult. Optional mask inputs use "_effect_mask". Multi-input,
time-dependent, or procedural effects may use additional private input keys
such as "_input_a", "_input_b", "_input", "_effect_input_a", or
"_effect_input_b"; those keys are part of the owning dispatcher's contract,
not generic node outputs.

Marker keys are private implementation metadata: they must not be serialized,
exposed as node output ports, or returned after _resolve_output(). No-op and
passthrough paths should return only real TextureResult outputs, without
"_effect_*" marker keys.
"""

# -- CACHE KEY STRATEGY -------------------------------------------------------
#
# Cache keys are produced by CacheManager.compute_key(), not by Node instances.
# A cache key must describe the complete render contract for one node
# evaluation:
#
# - Node identity: node_type and node_id, so two instances of the same type do
#   not accidentally share results.
# - Node state: serialized parameters and disabled flag, so UI/value changes
#   invalidate stale outputs.
# - Upstream content: each input TextureResult contributes texture_id,
#   revision, format, bbox, alpha_premultiplied, and color_space.
# - Render context: render_mode, ROI, output_format, alpha_mode, proxy_scale,
#   and frame_number keep preview/export, zoomed ROI, proxy, and animation
#   results isolated.
# - Source state: source nodes with externally owned textures must expose
#   content revisions or content hashes through the snapshot/cache inputs.
#
# Node authors should:
# - Mutate parameters through set_parameter() so param_revision and dirty state
#   stay correct.
# - Preserve TextureResult metadata when passing textures through.
# - Bump output revisions whenever node-owned texture content changes.
# - Avoid treating texture_id alone as content identity; GPU ids can be reused.
# - Extend CacheManager.compute_key() before relying on cache reuse for any new
#   input, mode, or metadata field that can affect rendered pixels.

import copy
import logging
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, ClassVar, Optional, Dict, List, Tuple, TYPE_CHECKING
from .node_state import NodeState

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from src.features.composite.core.format import ImageFormat, BBox
    from src.features.composite.utils.expression_parser import ExpressionContext
    from src.shared.keyframe import KeyframeTrack


@dataclass
class TextureResult:
    """Represents a GPU texture output from a node evaluation.

    Attributes:
        texture_id: OpenGL texture ID (glo).
        width: Actual texture pixel width.
        height: Actual texture pixel height.
        revision: Content version — bumped when texture content changes.
        format: Optional ImageFormat describing the canvas/frame this texture
                belongs to. When present, downstream nodes should prefer
                format.width/height for layout decisions.
        bbox: Optional BBox describing the data window — the sub-region that
              actually contains useful pixel data.  None means "implicit full
              frame", i.e. bbox = (0, 0, width, height).
        alpha_premultiplied: True=premultiplied, False=straight, None=unknown.
        color_space: Color space tag — "linear", "srgb", "bt709", "bt2020",
                     "aces_cg", "display", "unknown", None=unspecified.
        bit_depth: Bit depth of the source content (8, 10, 12, 16, 32).
                   Used for HDR processing and display conversion.
        frame_metadata: Optional VideoMetadata from MediaSource, containing
                        full color info (primaries, transfer, matrix, range).
                        Used for accurate color management downstream.
        depth_texture_id: Optional OpenGL texture ID for depth channel.
                          Used for Deep compositing — stores per-pixel Z values.
                          None means regular RGBA image (no depth info).
        has_deep_data: True if this texture contains deep data (depth_texture_id
                       is valid and represents meaningful depth information).
        aov_texture_ids: Mapping from render-pass name to auxiliary texture glo.
        aov_dimensions: Optional dimensions for each auxiliary render pass.
    """
    texture_id: int  # OpenGL texture ID (glo)
    width: int = 0
    height: int = 0
    revision: int = 0  # Content version — bumped when texture content changes
    format: Optional["ImageFormat"] = None
    bbox: Optional["BBox"] = None
    alpha_premultiplied: Optional[bool] = None  # True=premultiplied, False=straight, None=unknown
    color_space: Optional[str] = None           # "linear", "srgb", "bt709", "bt2020", "aces_cg", etc.
    bit_depth: int = 8                          # Source bit depth (8, 10, 12, 16, 32)
    frame_metadata: Optional[Any] = None        # VideoMetadata from MediaSource (type avoid circular import)
    # Deep compositing support
    depth_texture_id: Optional[int] = None      # Depth channel texture ID (glo)
    has_deep_data: bool = False                 # True if depth info is meaningful
    # P0-07: auxiliary render-pass contract. The main ``texture_id`` is the
    # beauty/display pass; these maps carry additional GPU passes without
    # forcing every node to grow a new output port.
    aov_texture_ids: Dict[str, int] = field(default_factory=dict)
    aov_dimensions: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    # Semantic name of the selected image. ``beauty`` is a colour image;
    # auxiliary passes use their AOV name and must not receive display OETF or
    # gamut transforms during export/readback.
    aov_semantic: str = "beauty"
    # P7: 渲染诊断元数据（Scene3D 等 3D 节点填充，供 UI 状态栏/日志显示）。
    # 含 active_camera/light_count/geometry_count/model_error/msaa/has_depth 等。
    # None = 无诊断信息（普通 2D 节点）。
    render_diagnostics: Optional[Any] = None    # Dict[str, Any]

    @property
    def is_valid(self) -> bool:
        return self.texture_id > 0

    def has_depth(self) -> bool:
        """Check if this texture has valid depth information."""
        return self.has_deep_data and self.depth_texture_id is not None and self.depth_texture_id > 0

    def aov_signature(self) -> tuple:
        """Return a stable identity for auxiliary render passes.

        A pass is identified by its semantic name, GPU texture id, declared
        dimensions, and the parent result revision. The signature is used by
        the evaluator cache so a beauty-only result can never be reused for a
        result carrying a different set of AOV buffers.
        """
        return tuple(
            (
                str(name),
                int(texture_id),
                tuple(self.aov_dimensions.get(name, (self.width, self.height))),
                int(self.revision),
            )
            for name, texture_id in sorted(self.aov_texture_ids.items())
            if int(texture_id) > 0
        )

    def with_updates(self, **changes: Any) -> "TextureResult":
        """Copy this result while preserving its complete metadata contract.

        GPU nodes frequently create a new colour texture for a format, alpha,
        or colour-space pass. ``dataclasses.replace`` makes it difficult to
        accidentally drop depth/AOV/diagnostic metadata at those boundaries.
        """
        return replace(self, **changes)

    def effective_bbox(self) -> "BBox":
        """Return the actual bbox, falling back to full-frame if None."""
        if self.bbox is not None:
            return self.bbox
        from src.features.composite.core.format import BBox
        return BBox.full_frame(self.width, self.height)

    def memory_bytes(self) -> int:
        """Estimate GPU memory used by this texture (bytes).

        Assumes RGBA half-float (4 channels × 2 bytes) unless overridden.
        For deep textures, adds depth channel (1 channel × 2 bytes).
        """
        if not self.is_valid:
            return 0
        base_memory = self.width * self.height * 4 * 2
        if self.has_depth():
            # Add depth channel memory (single channel, half-float)
            base_memory += self.width * self.height * 1 * 2
        # P0-07: account for auxiliary GPU passes in cache memory budgeting.
        # Scene3D currently emits R32F depth and RGBA8 object-id buffers.
        counted_ids = {self.texture_id}
        if self.depth_texture_id is not None:
            counted_ids.add(self.depth_texture_id)
        for name, texture_id in self.aov_texture_ids.items():
            texture_id = int(texture_id)
            if texture_id <= 0 or texture_id in counted_ids:
                continue
            pass_width, pass_height = self.aov_dimensions.get(
                name, (self.width, self.height))
            channels = 1 if str(name) == "depth" else 4
            bytes_per_channel = 4 if str(name) == "depth" else 1
            base_memory += (
                int(pass_width) * int(pass_height) * channels * bytes_per_channel
            )
            counted_ids.add(texture_id)
        return base_memory


@dataclass
class ParamDef:
    """Parameter definition with metadata for UI generation.

    Supports keyframe animation for numeric, vector, color, and discrete parameters:
    - animated: True if this parameter can be animated (default False for non-animatable types)
    - keyframes: List of keyframe data dicts (serialized from KeyframeTrack)

    String/file/channel parameters cannot be animated. Colors and vectors are
    stored as atomic values; bool/option values use hold semantics.

    Expression support:
    - allow_expression: True if this parameter can use expressions
    - expression_hint: Example expression for UI hint
    """
    name: str
    ptype: str = "float"        # "float", "int", "str", "bool", "option", "color", "file", "vec2", "vec3", "channel", "hdri"
    default: Any = None
    min_val: float = 0.0
    max_val: float = 1.0
    step: float = 0.01
    labels: Optional[List[str]] = None  # For "option" type
    decimals: int = 2          # For float display

    # ── Animation Support ──
    # Animation is supported for types with a deterministic typed track.
    animated: bool = False     # True if this parameter has keyframes
    keyframes: Optional[List[Dict[str, Any]]] = None  # Serialized keyframes from KeyframeTrack

    # Grouping and display metadata
    group: Optional[str] = None  # Parameter group name for UI organization
    hidden: bool = False         # Hide from UI (internal parameter)

    # ── Expression Support ──
    # Expressions are supported only for numeric parameters (float, int)
    allow_expression: bool = True  # True if this parameter can use expressions
    expression_hint: Optional[str] = None  # Example expression for UI hint (e.g., "this.width * 0.5")

    # ── X/Y Pair / Chain-Link Metadata ──
    # 配对/联动元数据：本参数与 peer 构成 X/Y 对（如 radius_x ↔ radius_y）。
    # 编辑页 effect 面板据此把两个参数渲染成带 ⛓ 链锁的一行（默认锁住、1:1
    # 等值联动）；值为 peer 参数的 name。None 表示无配对。
    linked: Optional[str] = None

    def is_animatable(self) -> bool:
        """Check if this parameter type supports animation."""
        return self.ptype in ("float", "int", "color", "vec2", "vec3",
                              "bool", "option")

    def is_expression_compatible(self) -> bool:
        """Return whether an expression can produce this parameter type."""
        return self.ptype in ("float", "int")

    def get_keyframe_track(self) -> Optional['KeyframeTrack']:
        """Get the keyframe track if animated.

        Returns:
            KeyframeTrack if animated and has keyframes, None otherwise.
        """
        if not self.animated or not self.keyframes:
            return None
        from src.shared.keyframe import keyframe_track_from_dict
        return keyframe_track_from_dict({
            "param_name": self.name,
            "keyframes": self.keyframes,
            "value_type": self.ptype,
            "min_val": self.min_val,
            "max_val": self.max_val,
        })

    def set_keyframe_track(self, track: Any) -> None:
        """Set the keyframe track from a KeyframeTrack object.

        Args:
            track: KeyframeTrack to set.
        """
        if track and self.is_animatable():
            self.animated = True
            self.keyframes = [kf.to_dict() for kf in track.keyframes]

    def make_default_parameters(self) -> Dict[str, Any]:
        """Return a parameters dict with default values."""
        if self.default is not None:
            return {self.name: self.default}
        if self.ptype == "float":
            return {self.name: 0.0}
        elif self.ptype == "int":
            return {self.name: 0}
        elif self.ptype == "vec2":
            return {self.name: (0.0, 0.0)}
        elif self.ptype == "vec3":
            return {self.name: (0.0, 0.0, 0.0)}
        elif self.ptype == "str":
            return {self.name: ""}
        elif self.ptype == "bool":
            return {self.name: False}
        elif self.ptype == "color":
            return {self.name: (0.0, 0.0, 0.0, 1.0)}
        elif self.ptype == "option" and self.labels:
            return {self.name: self.labels[0]}
        return {self.name: None}


@dataclass
class Port:
    """Input or output port on a node."""
    name: str
    direction: str  # "input" or "output"
    data_type: str = "image"  # "image", "float", "vec2", "vec3", "int", "bool", "scene"
    value: Any = None  # Default/user-set value for input ports
    max_connections: int = 0  # 0 = unlimited

    def __post_init__(self):
        if self.direction == "input":
            # Input ports default to 1 connection
            if self.max_connections == 0:
                self.max_connections = 1
        # Output ports: 0 = unlimited connections


@dataclass
class Connection:
    """A connection between two node ports."""
    src_node_id: str
    src_port_name: str
    dst_node_id: str
    dst_port_name: str


class Node:
    """Abstract base class for all composite nodes."""

    # Subclasses override these
    node_type: str = "base"
    node_title: str = "Base Node"
    header_color: str = "#555555"
    # AE-style parent binding is meaningful only for nodes that expose the
    # 2D transform channels consumed by NodeGraph's parenting resolver.
    # Keep this explicit instead of inferring from a few parameter names in
    # the UI; nodes such as Scene3D have a separate hierarchy model.
    supports_parent_transform: ClassVar[bool] = False
    # ── Layer-system capability flags (AE/Nuke hybrid, plan §3.5) ──
    # ``supports_layer_root``: the node may serve as the root of a Layer
    #   (i.e. the node that plugs into the auto Merge/Output chain).  Source
    #   nodes (image_input/video_frame) are NOT layer roots by themselves —
    #   the compiler auto-wraps them in a Transform.
    # ``supports_layer_opacity``: the node exposes an opacity-style parameter
    #   that the layer system can drive directly (otherwise opacity is
    #   projected onto the layer's auto-Merge node).
    supports_layer_root: ClassVar[bool] = False
    supports_layer_opacity: ClassVar[bool] = False
    # i18n key for the localized display title (e.g. "nodes.blur.title").
    # When set, ``display_title`` resolves it via the i18n system; when None
    # or missing, ``display_title`` falls back to ``node_title``.
    i18n_title_key: Optional[str] = None

    # Metadata attributes (subclasses should override)
    SEARCH_ALIASES: List[str] = []  # Search aliases, e.g. ["blur", "模糊", "虚化"]
    DESCRIPTION: str = ""            # Short description
    TAGS: List[str] = []             # Tags, e.g. ["filter", "image"]
    IS_PLACEHOLDER: bool = False     # Whether this is a placeholder (unimplemented)

    # ── CP-044/045: universal mask input + mix blend ──
    # True (default): when this node declares input ports but NO port named
    # "mask", a generic mask input is appended automatically and the
    # evaluator gates the effect with it (see _resolve_output's post-pass).
    # Nodes that already implement their own mask semantics (they declare a
    # "mask" port) and routing/organization sinks (Output/FileOutput/Dot/
    # NoOp/Group/Precomp/Switch/placeholder) opt out by setting False.
    GENERIC_MASK_MIX: ClassVar[bool] = True

    # Bypass mapping for disabled nodes
    # Dict mapping output_port_name -> input_port_name for pass-through
    # None or empty dict uses default behavior (first input -> first output)
    # Override this for nodes with non-standard port relationships (Shuffle, CopyChannel)
    BYPASS_MAPPING: Optional[Dict[str, str]] = None

    # Parameter definitions — subclasses should override this
    PARAM_DEFS: List[ParamDef] = []

    @property
    def display_title(self) -> str:
        """Localized title for display, falling back to node_title.

        Resolves ``i18n_title_key`` via the i18n system when set. Use this in
        UI layers (node items, search popup, menus) instead of ``node_title``
        so the on-canvas label follows the application language. ``node_title``
        itself stays the stable English identifier used for serialization and
        internal string matching.

        If ``instance_label`` is set (e.g. "1", "2"), it is appended to the
        localized title to distinguish multiple instances of the same node
        type (e.g. "Camera1", "灯光2"). The label is a pure display adornment;
        ``node_type`` / ``node_title`` / ``id`` remain the canonical identity.
        """
        # B4: a user-set label (canvas rename) wins over the auto number.
        if self.user_label:
            return self.user_label
        base = self.node_title
        if self.i18n_title_key:
            try:
                from src.shared.i18n.i18n import tr_or_none
                translated = tr_or_none(self.i18n_title_key)
                if translated:
                    base = translated
            except Exception:
                pass
        if self.instance_label:
            return f"{base}{self.instance_label}"
        return base

    def __init__(self, node_id: Optional[str] = None) -> None:
        self.id: str = node_id or str(uuid.uuid4())[:8]
        # ── Port-level revision tracking ──
        # Must exist before the ``outputs`` property setter runs
        # ``_register_output_revisions`` (see AUD-64001).
        # Each output port has a revision that bumps when its content changes
        self._port_revisions: Dict[str, int] = {}
        # Cache of upstream port revisions from last evaluation
        self._upstream_port_revisions: Dict[str, int] = {}
        # CP-044: inputs go through a property so the generic mask port is
        # appended at assignment time (mirrors the outputs property below).
        self._inputs: List[Port] = []
        self._generic_mask_port: bool = False
        self.inputs = []
        self.outputs = []
        # CP-044: parameters likewise ride a property — many subclasses
        # assign a FRESH dict in __init__ after declaring ports, which would
        # otherwise wipe the generic mask/mix defaults the inputs setter
        # seeded.
        self._parameters: Dict[str, Any] = {}
        self.parameters = {}
        self.position: Tuple[float, float] = (0.0, 0.0)
        # ── Instance label (P1 命名升级) ──
        # 同类多实例节点的区分序号（如 "1"/"2"），由创建流程分配。
        # display_title 末尾拼接此字段形成画布显示名（"Camera1"）。
        # 派生量：不进 to_dict/from_dict，加载后重新分配；删除不重排（只增不减），
        # 避免 undo/redo 下编号跳变。None 表示未分配（保持原 node_title 显示）。
        self.instance_label: Optional[str] = None
        # ── User label (B4 画布重命名) ──
        # 用户在画布上设置的自定义显示名。设置后 display_title 优先返回它
        # （覆盖 "节点名+序号" 的自动命名）。持久化字段：随 to_dict/from_dict
        # 存取；None 表示未改名（回落自动编号）。node_type / id 不受影响。
        self.user_label: Optional[str] = None
        self._dirty: bool = True
        self.disabled: bool = False
        self.param_revision: int = 1  # bumped on set_parameter, used for dirty propagation

        # State tracking
        self._state: NodeState = NodeState.DIRTY
        self.progress: float = 0.0
        self.error_message: str = ""
        self._on_state_changed: Optional[Callable] = None

        # ── Expression Support ──
        # Stores expressions for parameters: param_name -> expression string
        # ExpressionParser handles parsing and evaluation
        self._expressions: Dict[str, str] = {}
        # Per-parameter expression switch.  Keeping the source text while
        # disabled lets users temporarily fall back to the keyframe/static
        # value without losing work or changing the graph schema.
        self._expression_enabled: Dict[str, bool] = {}

        # ── Node-instance animation state (P1) ──
        # Per-node keyframe tracks: param_name -> serialized KeyframeTrack dict
        # (shape: {"param_name", "keyframes": [...], "min_val", "max_val"}).
        # This is the authoritative store for user-created animation. The old
        # approach of writing keyframes onto the class-level PARAM_DEFS polluted
        # every instance of the same node type — this field avoids that.
        self.keyframe_tracks: Dict[str, Dict[str, Any]] = {}
        # AE-style parameter pick-whip metadata.  The runtime expression is
        # still stored in ``_expressions`` so the existing dependency manager
        # evaluates it; this separate map lets the UI distinguish a deliberate
        # binding from a hand-written expression and serialize the source pair
        # without exposing internal node IDs in the property label.
        self.parameter_bindings: Dict[str, Dict[str, str]] = {}
        # Optional layer/node parent used by the Keyframes panel's parent pick
        # whip.  Keeping it on the node makes the relationship project-safe
        # and allows a later world-transform evaluator to consume it.
        self.parent_id: Optional[str] = None
        # Optional effective time range of this node within the composition:
        # {"comp_start", "comp_end", "source_in", "source_out", "offset"}.
        # None means "covers the whole composition range".
        self.time_range: Optional[Dict[str, Any]] = None
        # User-overridden header color. None = use the class default header_color.
        self.custom_header_color: Optional[str] = None

        # Forward-compatible payload for fields owned by a newer node/plugin
        # version.  It is deliberately kept separate from ``parameters`` so
        # an old host can round-trip extension data without attempting to
        # evaluate or edit it.
        self._unknown_fields: Dict[str, Any] = {}

        # Initialize parameters from PARAM_DEFS
        self._init_params_from_defs()

        # Initialize port revisions
        self._init_port_revisions()

    def _init_params_from_defs(self):
        """Initialize parameters with default values from PARAM_DEFS."""
        for pdef in self.PARAM_DEFS:
            defaults = pdef.make_default_parameters()
            for key, val in defaults.items():
                if key not in self.parameters:
                    self.parameters[key] = val

    # ── Output ports + revision tracking (AUD-64001) ────────────────────
    # Subclasses assign ``self.outputs`` AFTER ``super().__init__()`` returns
    # (the natural place), which used to run after the base class had already
    # called ``_init_port_revisions()`` — leaving revision tracking empty for
    # every such node and making ``bump_output_revision()`` a silent no-op.
    # Routing the assignment through a property registers the ports at the
    # moment they are declared, so the base-class call order no longer
    # matters. Seven node classes still re-invoke ``_init_port_revisions()``
    # manually; that stays supported (and idempotent).

    # ── CP-044/045: universal mask/mix parameter block ──
    # Appended to get_param_defs() for nodes that received the generic mask
    # port; ``setdefault`` semantics mean nodes with their own "mix" param
    # (blur/glow/grade/…) keep their definition and the evaluator skips the
    # generic mix for them (no double blending).
    GENERIC_MASK_PARAM_DEFS: ClassVar[List[ParamDef]] = [
        ParamDef("mask_channel", "option", "luma",
                 labels=["luma", "r", "g", "b", "a"], group="Mask"),
        ParamDef("mask_invert", "bool", False, group="Mask"),
        ParamDef("mix", "float", 1.0, min_val=0.0, max_val=1.0,
                 step=0.01, group="Mask"),
    ]

    @property
    def inputs(self) -> List[Port]:
        """Input ports of this node (CP-044: property with mask injection)."""
        return self._inputs

    @property
    def parameters(self) -> Dict[str, Any]:
        """Node parameters dict (CP-044: property with default re-seeding)."""
        return self._parameters

    @parameters.setter
    def parameters(self, value: Optional[Dict[str, Any]]) -> None:
        """Assign the parameters dict, re-seeding generic defaults (CP-044).

        Subclasses commonly build their port list first and then assign a
        fresh literal dict to ``self.parameters`` — without this re-seed the
        generic mask/mix defaults planted by the inputs setter would be
        wiped for exactly those nodes. ``setdefault`` semantics keep any
        key the caller (or a loaded project) explicitly provides.
        """
        self._parameters = value if isinstance(value, dict) else {}
        # Re-seed missing ParamDef defaults: subclasses that assign a fresh
        # literal dict in __init__ used to silently drop defaults for defs
        # added later (contract tests enforce their presence).
        self._init_params_from_defs()
        if getattr(self, "_generic_mask_port", False):
            for pdef in self.GENERIC_MASK_PARAM_DEFS:
                self._parameters.setdefault(pdef.name, pdef.default)

    @inputs.setter
    def inputs(self, ports: Optional[List[Port]]) -> None:
        """Assign input ports, then append the generic mask port (CP-044).

        Mirrors the ``outputs`` property pattern: subclasses assign
        ``self.inputs = [...]`` in ``__init__`` (possibly multiple times,
        e.g. dynamic-input nodes) and the generic mask rides along
        idempotently. Nodes that already declare a "mask" port (the 12
        legacy mask-aware nodes) and GENERIC_MASK_MIX=False opt-outs are
        left untouched.
        """
        self._inputs = list(ports) if ports else []
        self._generic_mask_port = False
        # Only nodes that consume image textures get the mask port — the
        # 3D scene-payload family (scene3d/model3d/deformer3d/…) carries
        # scene-typed inputs and their own port-count contracts; they opt
        # out via GENERIC_MASK_MIX instead.
        if (type(self).GENERIC_MASK_MIX and self._inputs
                and any(p.data_type == "image" for p in self._inputs)
                and not any(p.name == "mask" for p in self._inputs)):
            self._inputs.append(Port("mask", "input", "image"))
            self._generic_mask_port = True
            for pdef in self.GENERIC_MASK_PARAM_DEFS:
                self.parameters.setdefault(pdef.name, pdef.default)

    def get_param_defs(self) -> Dict[str, ParamDef]:
        """Get parameter definitions as a dict keyed by param name.

        CP-044: nodes with the generic mask port also expose the shared
        mask/mix parameter block (existing same-named definitions win).
        """
        defs = {pdef.name: pdef for pdef in self.PARAM_DEFS}
        if self._generic_mask_port:
            for pdef in self.GENERIC_MASK_PARAM_DEFS:
                defs.setdefault(pdef.name, pdef)
        return defs

    @property
    def outputs(self) -> List[Port]:
        """Output ports of this node."""
        return self._outputs

    @outputs.setter
    def outputs(self, ports: Optional[List[Port]]) -> None:
        self._outputs = ports if ports is not None else []  # type: ignore[assignment]
        self._register_output_revisions()

    def _register_output_revisions(self) -> None:
        """Register revision tracking for every current output port.

        Idempotent merge: ports already tracked keep their revision
        counters; ports that appear later (a subclass assigning
        ``outputs`` after ``super().__init__()``, or a runtime port add)
        start at 0.  Never resets live counters.

        AUD-64001: ports are declared with revision 0 ("declared, no
        content-change signalled yet"), NOT 1.  The render scheduler's
        port-revision layer compares this value against the consumer's
        cached upstream revision, which defaults to 0 until the node has
        been evaluated against that content — so 0 keeps unevaluated
        graphs clean, and the first ``bump_output_revision()`` (a real
        content change via set_parameter / keyframes / expressions)
        produces the 0→1 transition the dirty detection consumes.
        """
        revisions = getattr(self, '_port_revisions', None)
        if revisions is None:
            revisions = {}
            self._port_revisions = revisions
        for port in self._outputs:
            revisions.setdefault(port.name, 0)

    def _init_port_revisions(self) -> None:
        """Initialize revision tracking for all output ports.

        Kept for subclasses that re-invoke it after building their ports;
        delegates to the idempotent ``_register_output_revisions`` so a
        re-registration never discards live revision counters.
        """
        self._register_output_revisions()

    def bump_output_revision(self, port_name: str = None):
        """Increment the revision for an output port.

        Called after successful evaluation to signal content change to downstream.

        AUD-64001: a port name that was never registered (a port appended
        after ``__init__``, or a legacy subclass whose ports predate the
        property-based registration) is auto-registered instead of the
        bump being silently dropped — a dropped bump lost the downstream
        invalidation signal.

        Args:
            port_name: Specific port to bump. If None, bumps all output ports.
        """
        if port_name:
            current = self._port_revisions.get(port_name)
            if current is None:
                # First time this port is bumped: register at 1 (the
                # 0→1 content-change transition).
                self._port_revisions[port_name] = 1
            else:
                self._port_revisions[port_name] = current + 1
        else:
            # Bump all output ports: sync ports that appeared since the
            # last registration first so none is silently skipped.
            self._register_output_revisions()
            for name in self._port_revisions:
                self._port_revisions[name] += 1

    def get_port_revision(self, port_name: str) -> int:
        """Get the current revision for an output port."""
        return self._port_revisions.get(port_name, 0)

    def set_upstream_revision(self, input_port_name: str, revision: int):
        """Cache the upstream revision for an input port after evaluation."""
        self._upstream_port_revisions[input_port_name] = revision

    def get_upstream_revision(self, input_port_name: str) -> int:
        """Get cached upstream revision for an input port."""
        return self._upstream_port_revisions.get(input_port_name, 0)

    @classmethod
    def params_require_frame_cache(cls, params: Dict[str, object]) -> bool:
        """Hook: do these *non-animated* parameters make the output
        frame-dependent?

        Checked by BaseEvaluator._node_needs_frame_cache in addition to the
        keyframe/expression/time-range signals. Override in nodes that consume
        the timeline playhead directly (e.g. Text per-character animation reads
        ctx.frame_number without any parameter being keyframed) so the graph
        cache key includes the frame number and stale frames are not reused.
        """
        return False

    # ── Node-instance keyframe tracks (P1) ──

    def set_keyframe_track(self, param_name: str, track: 'KeyframeTrack') -> None:
        """Set a per-node keyframe track for ``param_name``.

        Stores the serialized track on this node instance (NOT on the class-level
        PARAM_DEFS), then bumps param_revision so downstream caches invalidate.
        Passing an empty / None track removes the animation for this parameter.
        """
        if track is None or not track.keyframes:
            self.remove_keyframe_track(param_name)
            return

        # Reject types without a track contract. Typed vector/discrete tracks
        # are explicitly supported and preserve their value domain.
        pdef = self.get_param_defs().get(param_name)
        if pdef is not None and not pdef.is_animatable():
            return

        min_val = getattr(track, 'min_val', None)
        if min_val is None:
            min_val = pdef.min_val if pdef else 0.0
        max_val = getattr(track, 'max_val', None)
        if max_val is None:
            max_val = pdef.max_val if pdef else 1.0

        serialized_keyframes = [kf.to_dict() for kf in track.keyframes]
        value_type = getattr(track, "value_type", "scalar")
        if pdef is not None and pdef.is_animatable():
            value_type = pdef.ptype
        if value_type in {"color", "vec2", "vec3"}:
            serialized_keyframes = [
                {**keyframe, "value": list(keyframe["value"])}
                for keyframe in serialized_keyframes
            ]
        track_data = {
            "param_name": param_name,
            "keyframes": serialized_keyframes,
            "min_val": min_val,
            "max_val": max_val,
        }
        if value_type != "scalar":
            track_data["value_type"] = value_type
        self.keyframe_tracks[param_name] = track_data
        if getattr(track, "pre_extrap", "constant") != "constant":
            self.keyframe_tracks[param_name]["pre_extrap"] = track.pre_extrap
        if getattr(track, "post_extrap", "constant") != "constant":
            self.keyframe_tracks[param_name]["post_extrap"] = track.post_extrap
        self._dirty = True
        self.param_revision += 1
        self.bump_output_revision()
        # N-04（2026-10-14 审计）：不再清除同参数表达式——轨道构成 base，
        # 启用的表达式在求值时覆盖 base（与 set_expression 的新政策
        # 对称）；表达式可用 valueAtTime/loopOut 采样该轨道。
        if self._state != NodeState.PLACEHOLDER:
            self.state = NodeState.DIRTY

    def remove_keyframe_track(self, param_name: str) -> None:
        """Remove the per-node keyframe track for ``param_name`` (no-op if absent)."""
        if param_name in self.keyframe_tracks:
            del self.keyframe_tracks[param_name]
            self._dirty = True
            self.param_revision += 1
            self.bump_output_revision()
            if self._state != NodeState.PLACEHOLDER:
                self.state = NodeState.DIRTY

    def compiled_eval_tracks(self):
        """N-14（2026-10-14 审计）：按 param_revision 缓存的已编译轨道。

        轨道写入路径（set_keyframe_track/remove_keyframe_track/ml_roto
        批量写/N-01 回滚）都会 bump param_revision；revision 未变时直接
        复用编译结果——快照每帧只做二分采样，不再逐参数反序列化
        KeyframeTrack（50 节点 × 5 轨道 × 120 键时为旧路径的主要成本）。
        """
        rev = self.param_revision
        cache = getattr(self, "_compiled_track_cache", None)
        if cache is not None and cache[0] == rev:
            return cache[1]
        from src.shared.keyframe import keyframe_track_from_dict
        defs = self.get_param_defs()
        compiled: Dict[str, Any] = {}
        for param_name, track_data in self.keyframe_tracks.items():
            if (not isinstance(track_data, dict)
                    or not track_data.get("keyframes")):
                continue
            pdef = defs.get(param_name)
            value_type = getattr(pdef, "ptype", "")
            if value_type not in {"color", "vec2", "vec3", "bool", "option"}:
                value_type = track_data.get("value_type", "scalar")
            try:
                compiled[param_name] = keyframe_track_from_dict({
                    "param_name": param_name,
                    "keyframes": track_data.get("keyframes", []),
                    "value_type": value_type,
                    "min_val": track_data.get("min_val", 0.0),
                    "max_val": track_data.get("max_val", 1.0),
                    "pre_extrap": track_data.get("pre_extrap", "constant"),
                    "post_extrap": track_data.get("post_extrap", "constant"),
                })
            except Exception:
                continue
        self._compiled_track_cache = (rev, compiled)
        return compiled

    def is_param_animated(self, param_name: str) -> bool:
        """True if this node has a non-empty keyframe track for ``param_name``."""
        track = self.keyframe_tracks.get(param_name)
        return bool(track and track.get("keyframes"))

    def effective_header_color(self) -> str:
        """Resolve the header color: user override if set, else the class default."""
        return self.custom_header_color or self.header_color

    def get_param_options(self) -> Dict[str, list]:
        """Get options lists for option-type parameters."""
        result = {}
        for pdef in self.PARAM_DEFS:
            if pdef.ptype == "option" and pdef.labels:
                result[pdef.name] = pdef.labels
        return result

    def get_input_port(self, name: str) -> Optional[Port]:
        for p in self.inputs:
            if p.name == name:
                return p
        return None

    def get_output_port(self, name: str) -> Optional[Port]:
        for p in self.outputs:
            if p.name == name:
                return p
        return None

    def set_parameter(self, key: str, value: Any) -> None:
        if self.parameters.get(key) != value:
            self.parameters[key] = value
            self._dirty = True
            self.param_revision += 1
            # Bump output port revisions to signal change to downstream
            self.bump_output_revision()
            if self._state != NodeState.PLACEHOLDER:
                self.state = NodeState.DIRTY

    def _set_parameter_silent(self, key: str, value: Any) -> None:
        """Write a parameter without dirty/revision/state side effects.

        AUD-6705: evaluation-produced durable data (FaceTrackNode's
        landmark tracks filled in by ``rerun_on_evaluate``) still belongs
        in ``parameters`` so it serializes with the project, but writing it
        via ``set_parameter`` mid-evaluation self-dirties the node, bumps
        the param/output revisions — invalidating the very cache entry
        being produced — and fires the state-changed callback from the
        render thread.  This channel only assigns the value; nodes whose
        consumers must re-read such data still get a cache dimension via
        ``params_require_frame_cache``.  Ordinary user edits keep going
        through ``set_parameter``.
        """
        if self.parameters.get(key) != value:
            self.parameters[key] = value

    # ── Expression Support ──

    def set_expression(self, key: str, expression: str) -> bool:
        """Set an expression for a parameter.

        Expression is stored separately from the parameter value.
        When evaluated, the expression result overrides the static value.

        Args:
            key: Parameter name.
            expression: Expression string (e.g., "this.width * 0.5").

        Returns:
            True if expression was set successfully, False if invalid.
        """
        pdef = self.get_param_defs().get(key)
        if pdef is not None and (not pdef.allow_expression
                                 or not pdef.is_expression_compatible()):
            return False

        # Validate expression
        from src.features.composite.utils.expression_parser import ExpressionParser
        parser = ExpressionParser()
        result = parser.validate(expression)

        if not result.success:
            # Store validation error for UI feedback
            return False

        self._expressions[key] = expression
        self._expression_enabled[key] = True
        self._dirty = True
        self.param_revision += 1
        self.bump_output_revision()

        # N-04（2026-10-14 审计）：不再删除同参数关键帧轨道。新政策：
        # 静态值/关键帧构成 base，启用的表达式在求值时覆盖 base；
        # loopOut/valueAtTime 采样 pre-expression 轨道，表达式禁用后
        # 自然回落 base。旧工程里“表达式 + 轨道”共存的载荷按同一
        # 规则求值（表达式优先），行为兼容。
        if self._state != NodeState.PLACEHOLDER:
            self.state = NodeState.DIRTY

        return True

    def get_expression(self, key: str) -> Optional[str]:
        """Get expression string for a parameter.

        Args:
            key: Parameter name.

        Returns:
            Expression string, or None if no expression set.
        """
        return self._expressions.get(key)

    def set_expression_enabled(self, key: str, enabled: bool) -> bool:
        """Enable or disable an existing expression without deleting it."""
        if key not in self._expressions:
            return False
        value = bool(enabled)
        if self._expression_enabled.get(key, True) == value:
            return True
        self._expression_enabled[key] = value
        self._dirty = True
        self.param_revision += 1
        self.bump_output_revision()
        if self._state != NodeState.PLACEHOLDER:
            self.state = NodeState.DIRTY
        return True

    def is_expression_enabled(self, key: str) -> bool:
        """Return whether an expression participates in evaluation."""
        return bool(self._expression_enabled.get(key, True))

    def has_expression(self, key: str) -> bool:
        """Check if a parameter has an expression.

        Args:
            key: Parameter name.

        Returns:
            True if expression is set for this parameter.
        """
        return key in self._expressions

    def remove_expression(self, key: str) -> None:
        """Remove expression from a parameter.

        Args:
            key: Parameter name.
        """
        if key in self._expressions:
            del self._expressions[key]
            self._expression_enabled.pop(key, None)
            self._dirty = True
            self.param_revision += 1
            self.bump_output_revision()

            if self._state != NodeState.PLACEHOLDER:
                self.state = NodeState.DIRTY

    def get_expression_dependencies(self) -> Dict[str, List[str]]:
        """Get dependencies for all expressions on this node.

        Returns a dict mapping param_name -> list of dependency specs.
        Each dependency spec is either:
        - "node_id.param" (external node reference)
        - "input.width" / "input.height" (input texture attributes)
        - "frame" (frame number)
        - "this.param" (local parameter, not tracked as external dependency)

        Returns:
            Dict of param_name -> list of dependency strings.
        """
        from src.features.composite.utils.expression_parser import ExpressionParser
        parser = ExpressionParser()

        result: Dict[str, List[str]] = {}
        for param_name, expression in self._expressions.items():
            deps = parser.extract_dependencies(expression)
            dep_strings: List[str] = []
            for dep in deps:
                if dep.node_id and dep.param_name:
                    dep_strings.append(f"{dep.node_id}.{dep.param_name}")
                elif dep.is_input_attr:
                    dep_strings.append(f"input.{dep.param_name}")
                elif dep.is_layer_attr:
                    dep_strings.append(f"layer.{dep.param_name}")
                elif dep.is_frame:
                    dep_strings.append("frame")
            result[param_name] = dep_strings

        return result

    def resolve_expressions(self, context: 'ExpressionContext') -> Dict[str, Any]:
        """Resolve all expressions and return resolved parameter values.

        Args:
            context: ExpressionContext providing variable values:
                - this_params: Current node's parameters
                - node_params: Other nodes' parameters (node_id -> {param -> value})
                - input_attrs: Input texture attributes (width, height)
                - frame: Current frame number

        Returns:
            Dict of resolved parameter values (static + evaluated expressions).
        """
        from src.features.composite.utils.expression_parser import ExpressionParser

        parser = ExpressionParser()
        resolved: Dict[str, Any] = dict(self.parameters)

        for param_name, expression in self._expressions.items():
            if not self.is_expression_enabled(param_name):
                continue
            result = parser.evaluate(expression, context)
            if result.success and result.value is not None:
                resolved[param_name] = result.value

        return resolved

    # ── Parameter Validation Helpers ──

    def _get_param_clamped(self, key: str, min_val: float = 0.0,
                           max_val: Optional[float] = None) -> float:
        """Get a parameter value, clamped to a valid range.

        Use this in _do_evaluate() to ensure numeric parameters are
        within safe bounds, preventing GPU shader errors or undefined behavior.

        Args:
            key: Parameter name.
            min_val: Minimum allowed value (default: 0.0).
            max_val: Maximum allowed value (optional, no upper bound if None).

        Returns:
            Clamped float value.

        Example:
            blur_radius = self._get_param_clamped("radius", min_val=0.0, max_val=100.0)
        """
        raw_val = self.parameters.get(key, min_val)
        val = float(raw_val) if raw_val is not None else min_val
        val = max(min_val, val)
        if max_val is not None:
            val = min(max_val, val)
        return val

    def _get_param_positive(self, key: str, default: float = 1.0) -> float:
        """Get a parameter value, ensuring it's positive (>= epsilon).

        Use for sizes, radii, scales, and other values that must be > 0.
        Returns epsilon (0.001) for zero/negative values to prevent
        division-by-zero or invalid geometry.

        Args:
            key: Parameter name.
            default: Default value if parameter is missing.

        Returns:
            Positive float value (minimum epsilon = 0.001).

        Example:
            size = self._get_param_positive("size", default=10.0)
        """
        EPSILON = 0.001
        raw_val = self.parameters.get(key, default)
        val = float(raw_val) if raw_val is not None else default
        return max(EPSILON, val)

    def _get_param_int(self, key: str, default: int = 0) -> int:
        """Get a parameter value as an integer.

        Use for index, count, or enum parameters that must be integer.
        Handles float-to-int conversion safely.

        Args:
            key: Parameter name.
            default: Default value if parameter is missing.

        Returns:
            Integer value.

        Example:
            channel = self._get_param_int("channel", default=0)
        """
        raw_val = self.parameters.get(key, default)
        if raw_val is None:
            return default
        return int(float(raw_val))  # Handle "1.0" strings safely

    def _get_param_bool(self, key: str, default: bool = False) -> bool:
        """Get a parameter value as a boolean.

        Handles string representations ("true", "false", "1", "0")
        and numeric values (1 -> True, 0 -> False).

        Args:
            key: Parameter name.
            default: Default value if parameter is missing.

        Returns:
            Boolean value.
        """
        raw_val = self.parameters.get(key, default)
        if raw_val is None:
            return default
        if isinstance(raw_val, bool):
            return raw_val
        if isinstance(raw_val, str):
            return raw_val.lower() in ("true", "yes", "1", "on")
        return bool(raw_val)

    # ── Load-time Parameter Sanitization (AUD-64002) ──────────────────

    def _sanitize_loaded_parameters(self) -> None:
        """Normalize externally loaded parameters against their ParamDefs.

        AUD-64002: ``from_dict`` used to copy persisted parameter values
        into ``self.parameters`` verbatim, so a hand-edited, corrupted or
        forward-versioned project file could push arbitrary types
        (strings, dicts, None) into runtime parameters.  Nodes then hit
        ``float()``/``int()`` TypeError inside ``_do_evaluate`` and the
        evaluator's per-node guard silently blanked the node.  Declared
        parameters are now coerced to their def type at load time; a
        value that cannot be represented falls back to the def default
        with a located warning (node id + param name) — never silent.

        Values already of the declared type pass through untouched.  In
        particular numeric values are NOT range-clamped here and option
        strings outside the label set are kept: ParamDef min/max cannot
        be distinguished from the dataclass defaults (0.0/1.0) so blanket
        clamping could corrupt legitimate legacy values, and some nodes
        deliberately map legacy label values at read time (e.g. blur
        "directional" → "motion").  Range/label enforcement stays at the
        per-parameter read sites (see ``_get_param_clamped``).  Parameters
        without a ParamDef (dynamically generated channels, plugin
        params) are kept as-is.
        """
        for name, pdef in self.get_param_defs().items():
            if name not in self.parameters:
                continue
            value = self.parameters[name]
            sanitized = self._coerce_loaded_param(pdef, value)
            if sanitized is not value:
                self.parameters[name] = sanitized

    def _coerce_loaded_param(self, pdef: "ParamDef", value: Any) -> Any:
        """Coerce one loaded parameter to its declared type.

        Returns the ORIGINAL object when it already matches the declared
        type (identity preserved so round-trip serialization stays
        byte-stable), a coerced replacement for salvageable values
        (numeric strings, bool-like strings), or the def default for
        unrepresentable ones.
        """
        ptype = pdef.ptype

        if ptype == "float":
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
            if isinstance(value, str):
                try:
                    coerced = float(value)
                except ValueError:
                    pass
                else:
                    clamped = max(pdef.min_val,
                                  min(pdef.max_val, coerced))
                    self._log_loaded_param(pdef, value, clamped, "coerced")
                    return clamped
            return self._loaded_param_fallback(pdef, value)

        if ptype == "int":
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            if isinstance(value, float):
                coerced = int(value)
                self._log_loaded_param(pdef, value, coerced, "coerced")
                return coerced
            if isinstance(value, str):
                try:
                    coerced = int(float(value))
                except ValueError:
                    pass
                else:
                    clamped = int(max(pdef.min_val,
                                      min(pdef.max_val, coerced)))
                    self._log_loaded_param(pdef, value, clamped, "coerced")
                    return clamped
            return self._loaded_param_fallback(pdef, value)

        if ptype == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                coerced = value.lower() in ("true", "yes", "1", "on")
                self._log_loaded_param(pdef, value, coerced, "coerced")
                return coerced
            if isinstance(value, (int, float)):
                coerced = bool(value)
                self._log_loaded_param(pdef, value, coerced, "coerced")
                return coerced
            return self._loaded_param_fallback(pdef, value)

        if ptype in ("str", "file", "channel", "hdri"):
            if isinstance(value, str):
                return value
            return self._loaded_param_fallback(pdef, value)

        if ptype == "option":
            # Keep any string (including legacy labels outside the current
            # set — read-time compat mappings own those); only reject
            # non-string garbage.
            if isinstance(value, str):
                return value
            return self._loaded_param_fallback(pdef, value)

        if ptype in ("color", "vec2", "vec3"):
            expected = {"color": (3, 4), "vec2": (2, 2), "vec3": (3, 3)}[ptype]
            if (isinstance(value, (list, tuple))
                    and expected[0] <= len(value) <= expected[1]
                    and all(isinstance(v, (int, float))
                            and not isinstance(v, bool) for v in value)):
                return value
            return self._loaded_param_fallback(pdef, value)

        # Unknown/extension ptype — not ours to validate.
        return value

    def _loaded_param_fallback(self, pdef: "ParamDef", original: Any) -> Any:
        """Resolve the default replacement for an unrepresentable value.

        Falls back to the ParamDef default, then the type's default from
        ``make_default_parameters``.  When no default exists at all the
        original value is kept (with a warning) rather than replaced by
        None, which would be no safer than the original.
        """
        fallback: Any = pdef.default
        if fallback is None:
            fallback = pdef.make_default_parameters().get(pdef.name)
        if fallback is None:
            self._log_loaded_param(pdef, original, original,
                                   "kept as-is (no default available)")
            return original
        self._log_loaded_param(pdef, original, fallback, "replaced")
        return fallback

    def _log_loaded_param(self, pdef: "ParamDef", original: Any,
                          sanitized: Any, action: str) -> None:
        """Locatable, non-silent record of every load-time sanitization."""
        logger.warning(
            "Node %s (%s): parameter %r has invalid value %r for ptype "
            "%r — %s as %r",
            self.id, self.node_type, pdef.name, original, pdef.ptype,
            action, sanitized)

    @property
    def state(self) -> NodeState:
        return self._state

    @state.setter
    def state(self, new_state: NodeState):
        if self._state != new_state:
            self._state = new_state
            if new_state == NodeState.DIRTY:
                self._dirty = True
            if self._on_state_changed:
                self._on_state_changed(self.id, new_state)

    def set_state_callback(self, callback: Callable):
        """Register a callback for state changes: callback(node_id, new_state)."""
        self._on_state_changed = callback

    def evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                 resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """
        Execute this node's GPU operation.

        If the node is disabled, pass through the first input unchanged.

        Args:
            inputs: Map of input port name -> TextureResult from upstream nodes
            ctx: ModernGL context
            fb_manager: FramebufferManager for temp texture allocation
            resource_pool: Optional GPUResourcePool for tracked texture allocation
            render_mode: "preview", "full", or "export" — nodes may adapt quality.

        Returns:
            Map of output port name -> TextureResult
        """
        if self.disabled:
            return self._bypass_evaluate(inputs)

        result = self._do_evaluate(inputs, ctx, fb_manager, resource_pool,
                                   render_mode=render_mode)

        # CP-044: nodes that received the generic mask port but did not
        # consume the mask themselves attach it as a DISTINCT marker — the
        # evaluator's _resolve_output post-pass (filter_evaluator) gates the
        # effect with it. Deliberately NOT "_effect_mask": that key belongs
        # to the 12 legacy mask-aware nodes whose dispatchers consume it
        # inline, and reusing it would double-apply the mask on them.
        if (self._generic_mask_port and isinstance(result, dict)
                and "_effect_type" in result and "_generic_mask" not in result):
            mask_tex = inputs.get("mask")
            if mask_tex is not None and mask_tex.is_valid:
                result["_generic_mask"] = mask_tex
        return result

    def _bypass_evaluate(self, inputs: Dict[str, TextureResult]) -> Dict[str, TextureResult]:
        """When disabled, pass through inputs to outputs according to BYPASS_MAPPING.

        The default behavior (first input -> first output) is suitable for
        simple filter nodes (Grade, Blur, Invert). Nodes with non-standard
        port relationships must override BYPASS_MAPPING:

        - Shuffle/CopyChannel: channel manipulation nodes should NOT pass through
          by default, as bypass would skip the channel operation entirely.
          Either set BYPASS_MAPPING to {} (return empty) or define explicit mapping.

        - Merge/Blend: multiple inputs to one output, should define which input
          to use for bypass (typically input_a or the "primary" input).

        Returns:
            Dict of output port name -> TextureResult from the mapped input.
        """
        # Use custom bypass mapping if defined
        if self.BYPASS_MAPPING:
            result: Dict[str, TextureResult] = {}
            for out_port, in_port in self.BYPASS_MAPPING.items():
                tex = inputs.get(in_port)
                if tex and tex.is_valid:
                    result[out_port] = tex
            return result

        # Default behavior: pass through first input to first output
        # This is suitable for simple filter nodes but may be wrong for
        # channel/merge nodes. They should override BYPASS_MAPPING.
        if self.inputs and self.outputs:
            first_input = next(iter(inputs.values()), None)
            if first_input and first_input.is_valid:
                return {self.outputs[0].name: first_input}

        # No valid bypass mapping available - return empty dict
        # This is safer than returning incorrect data
        return {}

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Actual evaluation logic — subclasses must implement this."""
        raise NotImplementedError(f"{self.__class__.__name__} must implement _do_evaluate()")

    # ── 阶段七：scene 数据旁路 ──────────────────────────────────────────

    def _emit_scene_payload(self, ctx, port: str, payload) -> None:
        """把 ScenePayload 写入求值器的旁路字典（scene 数据轨道）。

        scene 源节点（Camera3D/Light3D/Geo3D/Model3D）在 _do_evaluate 内调用：
            self._emit_scene_payload(ctx, "display", camera_payload)
        Scene3D 聚合器随后通过 _collect_scene_payload 读取上游 payload。

        若 ctx 不是 CompositeEvalContext（旧测试/无渲染管线），则静默跳过——
        保持向后兼容，不破坏无旁路环境下的单测。
        """
        scene_payloads = getattr(ctx, 'scene_payloads', None)
        if scene_payloads is None:
            return
        node_payloads = scene_payloads.setdefault(self.id, {})
        node_payloads[port] = payload

    def _collect_scene_payload(self, ctx, upstream_node_id: str) -> Optional[Any]:
        """Scene3D 聚合器：按上游 node_id 取回 scene payload。

        与 _emit_scene_payload 配对。返回 None 表示无 payload（未连接 /
        上游未求值）——调用方应降级处理。
        """
        scene_payloads = getattr(ctx, 'scene_payloads', None)
        if scene_payloads is None:
            return None
        node_payloads = scene_payloads.get(upstream_node_id)
        if not node_payloads:
            return None
        # 取该节点的第一个（通常也是唯一的）payload
        return next(iter(node_payloads.values()))

    @staticmethod
    def _inject_payload_node_id(payload: Any, upstream_id: str) -> Any:
        """Return a payload whose ``node_id`` is ``upstream_id`` (AUD-64003).

        The payload objects in ``ctx.scene_payloads`` are shared by every
        aggregator evaluating this frame.  Collection used to rewrite
        ``payload.node_id`` in place, so a second collector (virtual Viewer
        after Scene3D, or another camera aggregator) silently repointed the
        ``active_camera`` match / GPU ID picking at the wrong node.  The
        payload is now copied before injection, so each collector gets its
        own instance and the stored payload keeps its original identity.

        A payload that cannot carry the attribute (frozen dataclass, slotted
        class without ``node_id``) is returned unchanged with a located
        warning — never hidden behind a blanket ``except Exception``.
        """
        if not hasattr(payload, "node_id"):
            return payload
        if getattr(payload, "node_id", None) == upstream_id:
            return payload
        try:
            patched = replace(payload, node_id=upstream_id)
            return patched
        except TypeError:
            # Not a (replaceable) dataclass — fall back to a shallow copy.
            pass
        try:
            patched = copy.copy(payload)
            patched.node_id = upstream_id
            return patched
        except AttributeError as exc:
            # FrozenInstanceError subclasses AttributeError.
            logger.warning(
                "scene payload %s from node %s cannot carry node_id (%s); "
                "using original payload", type(payload).__name__, upstream_id,
                exc)
            return payload

    def _collect_scene_payload_by_type(self, ctx, payload_cls) -> Optional[Any]:
        """Scene3D 聚合器：按 payload 类型取回 scene payload。

        Scene3D 不知道是哪个上游 node_id 连到了 camera_in/lights_in（源节点
        之间是解耦的）。由于每种场景源类型只有一个，按 payload 类的 dataclass
        类型匹配是明确的：扫描旁路字典，返回第一个 isinstance 匹配的 payload。

        注意：此为全局扫描，不考虑连接拓扑。P1.5 后 Scene3D 优先用
        ``_collect_connected_scene_payloads`` 做端口感知收集；本方法保留作
        fallback（scene_connections 未启用时退化用之）。
        """
        scene_payloads = getattr(ctx, 'scene_payloads', None)
        if scene_payloads is None:
            return None
        # 退化全局扫描：scene_payloads 的 key 即上游 node_id，注入到 payload
        # 的 node_id 字段（CameraPayload 有此字段，供 active_camera 匹配）。
        for upstream_id, node_payloads in scene_payloads.items():
            for payload in node_payloads.values():
                if payload is not None and isinstance(payload, payload_cls):
                    return self._inject_payload_node_id(payload, upstream_id)
        return None

    def _collect_connected_scene_payloads(self, ctx, payload_cls):
        """Scene3D 聚合器：按连接拓扑取回该节点所有连接的 scene payload（P1.5）。

        只返回**真正连到本节点的 scene 输入端口**、且 payload 类型匹配的上游
        payload 列表（按连接顺序）。断开的场景节点不再影响渲染；多个 Geo3D/
        Model3D 连到 geometry_in 时全部返回（Scene3D 按顺序渲染所有几何）。

        退化条件：
          - ``ctx.scene_connections`` 为 None（端口感知未启用）→ 全局扫描取第一个匹配
          - ``ctx.scene_connections`` 非 None → 严格按连接收集。若本节点不在映射里
            或映射为空，返回空列表；断开的 Camera/Light/Geo/Model 不能影响场景。
        """
        scene_payloads = getattr(ctx, 'scene_payloads', None)
        if scene_payloads is None:
            return []
        # 连接映射：{node_id: {port: [upstream_id, ...]}}（v8 value 是列表，支持多连接）
        conns = getattr(ctx, 'scene_connections', None)
        # 退化：端口感知未启用（None）时才全局扫描。只要启用了连接映射，
        # 未连接就必须返回空，避免未连接 Camera3D 抢占 Scene3D/虚拟 Viewer。
        if conns is None:
            found = self._collect_scene_payload_by_type(ctx, payload_cls)
            return [found] if found is not None else []
        if self.id not in conns:
            return []
        # 端口感知：按连接的 upstream_id 收集（value 是列表，展开后按连接顺序）。
        # 兼容：value 可能是列表（v8 新结构）或单个 str（旧结构残留）。
        my_conns = conns[self.id]
        result = []
        for upstream_value in my_conns.values():
            if isinstance(upstream_value, str):
                upstream_ids = [upstream_value]
            else:
                upstream_ids = upstream_value
            for upstream_id in upstream_ids:
                node_payloads = scene_payloads.get(upstream_id)
                if not node_payloads:
                    continue
                for payload in node_payloads.values():
                    if payload is not None and isinstance(payload, payload_cls):
                        # P7：注入来源 node_id（CameraPayload 已有此字段用于
                        # active_camera 匹配；P7 扩展到 Model/Geo payload 供
                        # GPU ID picking 把 pick_id 映射回可选中的节点）。
                        # AUD-64003：注入到副本上，不改写共享 payload。
                        result.append(self._inject_payload_node_id(
                            payload, upstream_id))
                        break  # 每个上游节点只取一个 payload
        return result

    # ── ROI Computation Interface ──

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Compute the output bbox from input bboxes (pure analysis, no GPU).

        This method is called by the RenderScheduler during plan generation
        to determine what region of the output will contain useful data.

        Default implementation returns the first input bbox (for simple
        pixel-wise effects like Grade, Invert, Premult). Subclasses that
        change the data window (Blur, Transform, Crop, Merge) must override.

        Args:
            input_bboxes: Map of input port name -> BBox from upstream nodes.
            params: Current node parameters.

        Returns:
            The expected output bbox, or None if unknown/invalid.
        """
        if not input_bboxes:
            return None
        # Default: pass through the first input bbox
        return next(iter(input_bboxes.values()), None)

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """Compute the input ROI needed to produce a given output ROI.

        This method is called during ROI propagation (reverse traversal
        from Output node) to determine what region each input must provide.

        Default implementation returns output_roi for all inputs (for simple
        pixel-wise effects). If requires_roi_expansion() returns a positive
        padding value, the default ROI is expanded by that amount. Subclasses
        that need custom expansion logic (different per-input padding,
        transformed coordinates, etc.) should override this method.

        Args:
            output_roi: The region requested for this node's output.
            params: Current node parameters.
            input_bboxes: Optional upstream bboxes for context (used by Merge).

        Returns:
            Map of input port name -> ROI needed from that input.
        """
        padding: int = self.requires_roi_expansion(params)
        input_roi: "BBox" = output_roi.expanded(padding) if padding > 0 else output_roi

        # Default: all inputs need the same ROI. Nodes with custom expansion
        # logic should override this method.
        result: Dict[str, "BBox"] = {}
        for port in self.inputs:
            result[port.name] = input_roi
        return result

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        """Return the ROI expansion margin needed for this node.

        Used by the scheduler to know if this node type needs extra pixels
        around the requested ROI. Returns 0 for most effects, positive for
        Blur/Glow, and negative (special handling) for Transform/Crop.

        Args:
            params: Current node parameters.

        Returns:
            Number of extra pixels needed on all sides of the ROI.
        """
        return 0

    def to_dict(self) -> dict:
        """Serialize node to dict for save/load.

        Returns a deep copy of parameters so that downstream consumers
        (e.g. _relativize_composite_paths) cannot mutate the live node state.
        Includes expressions for parameters that have them.

        P1: also serializes per-node keyframe_tracks, time_range and
        custom_header_color so animation, timing and user-picked node colors
        survive a save/reload cycle.
        """
        data = {
            "id": self.id,
            "type": self.node_type,
            "position": list(self.position),
            "parameters": copy.deepcopy(self.parameters),
            "disabled": self.disabled,
        }

        # Preserve fields that this node version does not understand.  Known
        # fields are excluded at load time and are always written from the
        # live model, so stale extension data cannot override edited state.
        if self._unknown_fields:
            data.update(copy.deepcopy(self._unknown_fields))

        # Serialize expressions if any
        if self._expressions:
            data["expressions"] = dict(self._expressions)
        disabled_expressions = {
            key: False for key in self._expressions
            if not self.is_expression_enabled(key)
        }
        if disabled_expressions:
            data["expression_enabled"] = disabled_expressions

        # P1: serialize per-node animation tracks (deep copy to protect live state)
        if self.keyframe_tracks:
            data["keyframe_tracks"] = copy.deepcopy(self.keyframe_tracks)

        if self.parameter_bindings:
            data["parameter_bindings"] = copy.deepcopy(self.parameter_bindings)

        if self.parent_id:
            data["parent_id"] = self.parent_id

        # P1: serialize effective time range
        if self.time_range is not None:
            data["time_range"] = copy.deepcopy(self.time_range)

        # P1: serialize user-overridden node color
        if self.custom_header_color is not None:
            data["node_color"] = self.custom_header_color

        # B4: serialize user-renamed display label
        if self.user_label:
            data["user_label"] = self.user_label

        return data

    @classmethod
    def from_dict(cls, data: dict) -> 'Node':
        """Deserialize node from dict.

        P1: restores per-node keyframe_tracks, time_range and custom_header_color.
        Missing fields are tolerated so older projects still load.
        """
        node = cls(node_id=data.get("id"))
        node.position = tuple(data.get("position", [0, 0]))
        # Merge loaded parameters with defaults so new params get default values
        loaded_params = data.get("parameters", {})
        if not isinstance(loaded_params, dict):
            # AUD-64002: the parameters payload is external data — a
            # corrupted file must not crash the load with AttributeError.
            logger.warning(
                "Node %s (%s): 'parameters' payload is %s, not a dict — "
                "ignored", node.id, node.node_type, type(loaded_params).__name__)
            loaded_params = {}
        for key, val in loaded_params.items():
            node.parameters[key] = val
        # AUD-64002: normalize declared parameters against their ParamDefs
        # so wrong-typed persisted values cannot reach _do_evaluate (where
        # float()/int() coercion would raise and silently blank the node).
        node._sanitize_loaded_parameters()
        node.disabled = data.get("disabled", False)

        # Load expressions if present
        expressions = data.get("expressions", {})
        for key, expr in expressions.items():
            node.set_expression(key, expr)

        enabled = data.get("expression_enabled", {})
        if isinstance(enabled, dict):
            for key, state in enabled.items():
                if key in node._expressions:
                    node.set_expression_enabled(key, bool(state))

        # P1: load per-node keyframe tracks
        kfs = data.get("keyframe_tracks")
        if isinstance(kfs, dict) and kfs:
            node.keyframe_tracks = copy.deepcopy(kfs)
            # N-04（2026-10-14 审计）：载入时不再丢弃与轨道同参数的
            # 表达式。新政策是“静态/关键帧构成 base，启用的表达式覆盖
            # base”——loopOut/valueAtTime 需要采样 pre-expression 轨道，
            # 旧互斥删除使循环动画在载入后直接失效（36 帧回 0 而非 12）。
            # 表达式禁用时求值自然回落轨道/静态值。

        # Restore binding metadata after expressions/keyframes.  Bindings are
        # validated by NodeGraph when the graph is edited; loading remains
        # tolerant so old or partially migrated projects still open.
        bindings = data.get("parameter_bindings", {})
        if isinstance(bindings, dict):
            node.parameter_bindings = {
                str(param): {
                    "node_id": str(info.get("node_id", "")),
                    "param_name": str(info.get("param_name", "")),
                }
                for param, info in bindings.items()
                if isinstance(info, dict)
                and info.get("node_id") and info.get("param_name")
            }

        parent_id = data.get("parent_id")
        if parent_id:
            node.parent_id = str(parent_id)

        # P1: load time range
        tr = data.get("time_range")
        if isinstance(tr, dict):
            node.time_range = dict(tr)

        # P1: load custom node color. Stored under "node_color" on disk but
        # mirrored to header_color at runtime so existing NodeItem / Properties
        # code that reads header_color keeps working.
        nc = data.get("node_color")
        if isinstance(nc, str) and nc:
            node.custom_header_color = nc
            node.header_color = nc

        # B4: load user-renamed display label. Missing on older projects —
        # tolerated, stays None so display falls back to the auto number.
        # Stripped to match the UI commit path's normalization.
        ul = data.get("user_label")
        if isinstance(ul, str) and ul.strip():
            node.user_label = ul.strip()

        known_fields = {
            "id", "type", "position", "parameters", "disabled",
            "expressions", "keyframe_tracks", "parameter_bindings",
            "expression_enabled", "parent_id", "time_range", "node_color",
            "user_label",
        }
        node._unknown_fields = {
            str(key): copy.deepcopy(value)
            for key, value in data.items()
            if key not in known_fields
        }

        return node

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(id={self.id}, type={self.node_type})"
