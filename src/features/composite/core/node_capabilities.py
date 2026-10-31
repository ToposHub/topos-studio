"""
NodeCapabilities - Centralized node capability definitions.

Provides a unified data structure for declaring node capabilities that are
currently scattered across multiple files (scheduler, evaluator, ROI, tile,
cache, fusion). This addresses the drift problem where different subsystems
maintain hand-written lists that become inconsistent.

Architecture:
1. NodeCapability dataclass defines all capability fields
2. _NODE_CAPABILITY_TABLE provides defaults for each node type
3. get_node_capability() merges class attributes with table defaults
4. Helper functions derive specific properties from capabilities

Key design principle: READ FIRST, REWRITE LATER
- This module does NOT force existing subsystems to use capabilities
- It provides the data structure and reading interface
- Subsequent tasks (S3-B/C/D) will migrate subsystems to use capabilities

Usage:
    from src.features.composite.core.node_capabilities import get_node_capability

    cap = get_node_capability("blur")
    if cap.action_type == "effect":
        ...
    if cap.tile_padding > 0:
        ...
"""

from dataclasses import dataclass
from typing import Dict, List, Literal, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:
    pass


# ── Action Types ──
# Matches RenderScheduler.NodePlan.action classification

ActionType = Literal["source", "effect", "output", "passthrough", "placeholder"]


# ── ROI Policies ──
# Determines how ROI propagates through nodes

ROIPolicy = Literal[
    "full_frame",       # Full frame output (sources, generators)
    "identity",         # Same as input (simple filters)
    "expand",           # Expands ROI by padding (blur, glow)
    "transform",        # Transforms ROI (transform, reformat)
    "crop",             # Reduces ROI (crop)
    "merge_union",      # Union of inputs (merge)
    "merge_intersection",  # Intersection of inputs
    "copy_a",           # Copy from input A
    "copy_b",           # Copy from input B
    "passthrough",      # Pass through first input
    "none",             # No output (output node)
]


# ── NodeCapability Dataclass ──

@dataclass(frozen=True)
class NodeCapability:
    """Immutable declaration of a node type's capabilities.

    This dataclass centralizes information that was previously scattered across:
    - render_scheduler.py: _GPU_STATEFUL_TYPES, _PLACEHOLDER_TYPES (removed)
    - base_evaluator.py: _NODE_TYPE_MAP, _CACHE_OWNED_TYPES, _PASSTHROUGH_NODE_TYPES
    - tile_padding_computer.py: _PADDING_NODE_TYPES
    - pass_fusion.py: FUSIBLE_TYPES, NON_FUSIBLE_TYPES

    Fields are designed to be:
    1. Derivable from existing node class attributes where possible
    2. Overridable via per-node entries in _NODE_CAPABILITY_TABLE
    3. Read-only after creation (frozen=True)

    Attributes:
        node_type: Unique node type identifier (e.g., "blur", "grade").
        action_type: Classification for scheduling:
            - "source": GPU-stateful node (color_input, image_input, constant)
            - "effect": Pure effect node needing GPU dispatch
            - "output": Output node (viewer, file_output)
            - "passthrough": Disabled/bypass node
            - "placeholder": Unimplemented node type
        effect_type: Dispatcher key for effect nodes (e.g., "blur", "merge").
                     None for non-effect nodes.
        owns_color_texture: True if this node creates new color textures.
                            False if it passes through or references external.
        owns_depth_texture: True if this node creates depth textures.
        roi_policy: How ROI propagates through this node.
        tile_padding: Pixels of padding needed for tile rendering.
                      > 0 for spatial effects (blur, glow, erode_dilate).
        fusible: True if this node can be fused into a mega-shader chain.
        requires_frame_number: True if this node needs frame number for cache key.
        requires_gl: True if this node requires OpenGL context for evaluation.
                     False for pure-CPU or passthrough nodes.
        category: UI category for organization (from NODE_CATEGORIES).
        is_placeholder: Explicit placeholder flag (from IS_PLACEHOLDER class attr).
        bypass_mapping: Dict mapping output_port -> input_port for bypass/disabled mode.
    """
    node_type: str
    action_type: ActionType
    effect_type: Optional[str] = None
    owns_color_texture: bool = False
    owns_depth_texture: bool = False
    roi_policy: ROIPolicy = "full_frame"
    tile_padding: int = 0
    fusible: bool = False
    requires_frame_number: bool = False
    requires_gl: bool = True
    category: Optional[str] = None
    is_placeholder: bool = False
    bypass_mapping: Optional[Dict[str, str]] = None


# ── Capability Table ──
# Centralized defaults for all registered node types
# Derived from existing subsystems to ensure consistency

_NODE_CAPABILITY_TABLE: Dict[str, NodeCapability] = {
    # ── Input / Source Nodes ──
    # GPU-stateful nodes that hold persistent textures
    "color_input": NodeCapability(
        node_type="color_input",
        action_type="source",
        roi_policy="full_frame",
        requires_gl=True,
        category="Input",
        owns_color_texture=False,  # External texture, not owned
    ),
    "image_input": NodeCapability(
        node_type="image_input",
        action_type="source",
        roi_policy="full_frame",
        requires_gl=True,
        category="Input",
        owns_color_texture=False,  # External texture
    ),
    "timeline_clip_input": NodeCapability(
        node_type="timeline_clip_input",
        action_type="source",
        roi_policy="full_frame",
        requires_gl=True,
        category="Input",
        owns_color_texture=False,  # External texture from runtime context
    ),
    "motion_graphic": NodeCapability(
        node_type="motion_graphic",
        action_type="source",
        roi_policy="full_frame",
        requires_frame_number=True,
        requires_gl=True,
        category="Input",
        owns_color_texture=False,
    ),
    "constant": NodeCapability(
        node_type="constant",
        action_type="source",
        roi_policy="full_frame",
        requires_gl=True,
        category="Input",
        # Constant self-manages its texture lifecycle: it releases the old
        # GL object on parameter/size change (_do_evaluate) and in cleanup().
        # owns_color_texture=False keeps it OUT of CACHE_OWNED_TYPES so the
        # cache manager does NOT call release_texture_by_id() on its glo —
        # that would double-free the same object Constant already released,
        # causing driver-level crashes (especially on Constant→Output, where
        # the passthrough shares the glo). This matches image_input/video_frame.
        owns_color_texture=False,
    ),
    "video_frame": NodeCapability(
        node_type="video_frame",
        action_type="source",
        roi_policy="full_frame",
        requires_frame_number=True,  # Frame-dependent
        requires_gl=True,
        category="Input",
        owns_color_texture=False,  # External texture
    ),
    "text": NodeCapability(
        node_type="text",
        action_type="source",
        roi_policy="full_frame",
        requires_gl=True,
        category="Input",
        # Text self-manages its texture lifecycle: it releases the old GL
        # object on content/size change (_do_evaluate) and in cleanup().
        # owns_color_texture=False keeps it OUT of CACHE_OWNED_TYPES so the
        # cache manager does NOT call release_texture_by_id() on its glo —
        # that would double-free the same object Text already released.
        # Mirrors the Constant node's policy (see constant capability entry).
        owns_color_texture=False,
    ),
    "scene3d": NodeCapability(
        node_type="scene3d",
        action_type="source",
        roi_policy="full_frame",
        requires_gl=True,
        requires_frame_number=True,  # 相机/物体参数可挂动画，换帧需重渲染
        category="3D",
        # Scene3D 自管 GL 资源（color texture + depth renderbuffer + R32F 深度纹理
        # + FBO + VBO/VAO/program），在内容/尺寸变更时释放重建，并在 cleanup() 释放全部。
        # owns_color_texture=False 保持其游离于 CACHE_OWNED_TYPES 之外，避免 CacheManager
        # 对其 glo 调 release_texture_by_id() 造成双重释放（与 ConstantNode 同款策略）。
        owns_color_texture=False,
        # 阶段六：深度合成 —— Scene3D 输出 R32F 可采样线性深度（depth_texture_id），
        # 供 deep_merge 消费。owns_depth_texture=True 让 CacheManager 不回收该 glo。
        owns_depth_texture=True,
    ),
    # ── 阶段七：Scene3D 节点拆分 —— 4 个 scene 数据源节点 ──
    # 这些节点产出 ScenePayload（通过双轨制旁路），仍以 action_type="source" 注册，
    # 由 evaluator 的 _sync_render_nodes 维护持久实例（与 ConstantNode/Scene3D 同款）。
    # owns_color_texture=False：与 Scene3D 一致，避免 CacheManager 双重释放。
    # 占位阶段不产出真实 TextureResult（仅 emit scene payload），但保持 source 分类
    # 以便持久实例被正确维护；提交 2/3 补全后行为不变。
    "camera3d": NodeCapability(
        node_type="camera3d",
        action_type="source",
        roi_policy="full_frame",
        requires_gl=True,
        requires_frame_number=True,  # cam_* 参数可挂动画
        category="3D",
        owns_color_texture=False,
    ),
    "light3d": NodeCapability(
        node_type="light3d",
        action_type="source",
        roi_policy="full_frame",
        requires_gl=True,
        requires_frame_number=True,  # light_*/anim 参数可挂动画
        category="3D",
        owns_color_texture=False,
    ),
    "geo3d": NodeCapability(
        node_type="geo3d",
        action_type="source",
        roi_policy="full_frame",
        requires_gl=True,
        requires_frame_number=True,  # object_pos/rot/instance 参数可挂动画
        category="3D",
        owns_color_texture=False,
    ),
    "model3d": NodeCapability(
        node_type="model3d",
        action_type="source",
        roi_policy="full_frame",
        requires_gl=True,
        requires_frame_number=True,  # anim_time 可挂动画
        category="3D",
        owns_color_texture=False,
    ),
    "deformer3d": NodeCapability(
        node_type="deformer3d",
        action_type="source",  # scene 中间节点，走 source 轨道（emit scene payload）
        roi_policy="full_frame",
        requires_gl=True,
        requires_frame_number=True,  # strength/time 参数可挂动画
        category="3D",
        owns_color_texture=False,
    ),
    "noise": NodeCapability(
        node_type="noise",
        action_type="effect",
        effect_type="noise",
        roi_policy="full_frame",
        owns_color_texture=True,  # Generator
        requires_gl=True,
        category="Input",
    ),
    "ramp": NodeCapability(
        node_type="ramp",
        action_type="effect",
        effect_type="ramp",
        roi_policy="full_frame",
        owns_color_texture=True,  # Generator
        requires_gl=True,
        category="Input",
    ),

    # ── Output Nodes ──
    "output": NodeCapability(
        node_type="output",
        action_type="output",
        roi_policy="none",
        owns_color_texture=True,  # Display texture
        requires_gl=True,
        category="Output",
    ),
    "file_output": NodeCapability(
        node_type="file_output",
        action_type="output",
        roi_policy="none",
        owns_color_texture=False,
        requires_gl=True,
        # The node performs a frame-scoped disk write in export mode. Keep the
        # frame in its cache key so sequence export cannot reuse the first
        # frame's side effect for later frames.
        requires_frame_number=True,
        category="Output",
        is_placeholder=False,
    ),

    # ── Merge / Blend Nodes ──
    "merge": NodeCapability(
        node_type="merge",
        action_type="effect",
        effect_type="merge",
        roi_policy="merge_union",
        owns_color_texture=True,
        requires_gl=True,
        category="Merge",
        fusible=False,  # Multi-input, cannot fuse
    ),
    "blend": NodeCapability(
        node_type="blend",
        action_type="effect",
        effect_type="merge",
        roi_policy="merge_union",
        owns_color_texture=True,
        requires_gl=True,
        category="Merge",
        fusible=False,
    ),
    "deep_merge": NodeCapability(
        node_type="deep_merge",
        action_type="effect",
        effect_type="deep_merge",
        roi_policy="merge_union",
        owns_color_texture=True,
        owns_depth_texture=True,  # Deep merge produces depth
        requires_gl=True,
        category="Merge",
        fusible=False,
    ),
    "premult": NodeCapability(
        node_type="premult",
        action_type="effect",
        effect_type="premult",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Merge",
        fusible=True,  # Single-input, per-pixel
    ),
    "unpremult": NodeCapability(
        node_type="unpremult",
        action_type="effect",
        effect_type="unpremult",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Merge",
        fusible=True,
    ),
    "keymix": NodeCapability(
        node_type="keymix",
        action_type="effect",
        effect_type="keymix",
        roi_policy="merge_union",
        owns_color_texture=True,
        requires_gl=True,
        category="Merge",
        fusible=False,  # Multi-input (CP-011)
    ),
    "cdl": NodeCapability(
        node_type="cdl",
        action_type="effect",
        effect_type="cdl",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Color",
        fusible=False,
    ),
    "colorlookup": NodeCapability(
        node_type="colorlookup",
        action_type="effect",
        effect_type="colorlookup",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Color",
        fusible=False,
    ),
    "vectorblur": NodeCapability(
        node_type="vectorblur",
        action_type="effect",
        effect_type="vectorblur",
        roi_policy="expand",
        tile_padding=10,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=False,
    ),
    "grain": NodeCapability(
        node_type="grain",
        action_type="effect",
        effect_type="film_grain",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=False,
    ),
    "degrain": NodeCapability(
        node_type="degrain",
        action_type="effect",
        effect_type="degrain",
        roi_policy="expand",
        tile_padding=4,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=False,
    ),
    "dissolve": NodeCapability(
        node_type="dissolve",
        action_type="effect",
        effect_type="dissolve",
        roi_policy="merge_union",
        owns_color_texture=True,
        requires_gl=True,
        category="Merge",
        fusible=False,  # Multi-input
    ),
    "switch": NodeCapability(
        node_type="switch",
        action_type="passthrough",  # BUG-14: Corrected to match evaluator's _PASSTHROUGH_NODE_TYPES
        roi_policy="passthrough",
        owns_color_texture=False,  # Passthrough
        requires_gl=False,  # Pure passthrough logic
        category="Merge",
        fusible=False,
        bypass_mapping={"result": "input_0"},  # Default bypass
    ),
    "mask": NodeCapability(
        node_type="mask",
        action_type="effect",
        effect_type="mask",
        roi_policy="identity",
        owns_color_texture=True,  # Matte output
        requires_gl=True,
        category="Mask",
    ),
    "matte": NodeCapability(
        node_type="matte",
        action_type="effect",
        effect_type="matte",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Merge",
    ),

    # ── Transform Nodes ──
    "transform": NodeCapability(
        node_type="transform",
        action_type="effect",
        effect_type="transform",
        roi_policy="transform",
        tile_padding=1,  # Sampling edge pixels
        owns_color_texture=True,
        requires_gl=True,
        category="Transform",
        fusible=False,  # Spatial transform
    ),
    "reformat": NodeCapability(
        node_type="reformat",
        action_type="effect",
        effect_type="reformat",
        roi_policy="transform",
        owns_color_texture=True,
        requires_gl=True,
        category="Transform",
        fusible=False,
    ),
    "crop": NodeCapability(
        node_type="crop",
        action_type="effect",
        effect_type="crop",
        roi_policy="crop",
        owns_color_texture=True,
        requires_gl=True,
        category="Transform",
        fusible=False,
    ),
    "cornerpin": NodeCapability(
        node_type="cornerpin",
        action_type="effect",
        effect_type="cornerpin",
        roi_policy="transform",
        tile_padding=1,
        owns_color_texture=True,
        requires_gl=True,
        category="Transform",
        fusible=False,
    ),

    # ── Channel Nodes ──
    "shuffle": NodeCapability(
        node_type="shuffle",
        action_type="effect",
        effect_type="shuffle",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Channel",
        fusible=True,
    ),
    "copy_channel": NodeCapability(
        node_type="copy_channel",
        action_type="effect",
        effect_type="copy_channel",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Channel",
        fusible=False,  # Multi-input
    ),
    "channel_merge": NodeCapability(
        node_type="channel_merge",
        action_type="effect",
        effect_type="channel_merge",
        roi_policy="merge_union",
        owns_color_texture=True,
        requires_gl=True,
        category="Channel",
        fusible=False,
    ),
    "set_alpha": NodeCapability(
        node_type="set_alpha",
        action_type="effect",
        effect_type="set_alpha",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Channel",
        fusible=False,  # Multi-input
    ),
    "remove_channels": NodeCapability(
        node_type="remove_channels",
        action_type="effect",
        effect_type="remove_channels",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Channel",
        fusible=False,
    ),

    # ── Color Nodes ──
    "grade": NodeCapability(
        node_type="grade",
        action_type="effect",
        effect_type="grade",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Color",
        fusible=True,
    ),
    "color_correct": NodeCapability(
        node_type="color_correct",
        action_type="effect",
        effect_type="color_correct",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Color",
        fusible=True,
    ),
    "cs_transform": NodeCapability(
        node_type="cs_transform",
        action_type="effect",
        effect_type="cs_transform",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Color",
        fusible=True,
    ),
    "huecorrect": NodeCapability(
        node_type="huecorrect",
        action_type="effect",
        effect_type="huecorrect",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Color",
        fusible=True,
    ),
    "lut": NodeCapability(
        node_type="lut",
        action_type="effect",
        effect_type="lut",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Color",
        fusible=False,  # LUT sampling
    ),
    "topos_color": NodeCapability(
        node_type="topos_color",
        action_type="effect",
        effect_type="topos_color",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Color",
        # P0-09: the fused mega-shader has no topos_color stage
        # (see pass_fusion._is_order_compatible), so it could never
        # actually fuse despite being single-input per-pixel.
        fusible=False,
        bypass_mapping={"result": "image"},
    ),
    "invert": NodeCapability(
        node_type="invert",
        action_type="effect",
        effect_type="invert",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Utility",
        fusible=True,
    ),
    "clamp": NodeCapability(
        node_type="clamp",
        action_type="effect",
        effect_type="clamp",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Utility",
        fusible=True,
    ),
    "normalize": NodeCapability(
        node_type="normalize",
        action_type="effect",
        effect_type="normalize",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Utility",
        fusible=True,
    ),

    # ── Filter Nodes ──
    "blur": NodeCapability(
        node_type="blur",
        action_type="effect",
        effect_type="blur",
        roi_policy="expand",
        tile_padding=10,  # Default max radius placeholder
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=False,  # Spatial effect
    ),
    "defocus": NodeCapability(
        node_type="defocus",
        action_type="effect",
        effect_type="defocus",
        roi_policy="expand",
        tile_padding=10,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=False,
    ),
    "glow": NodeCapability(
        node_type="glow",
        action_type="effect",
        effect_type="glow",
        roi_policy="expand",
        tile_padding=10,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=False,
    ),
    "sharpen": NodeCapability(
        node_type="sharpen",
        action_type="effect",
        effect_type="sharpen",
        roi_policy="expand",
        tile_padding=2,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=False,
    ),
    "erode_dilate": NodeCapability(
        node_type="erode_dilate",
        action_type="effect",
        effect_type="erode_dilate",
        roi_policy="expand",
        tile_padding=10,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=False,
    ),
    "denoise": NodeCapability(
        node_type="denoise",
        action_type="effect",
        effect_type="denoise",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=False,
    ),
    "edge_detect": NodeCapability(
        node_type="edge_detect",
        action_type="effect",
        effect_type="edge_detect",
        roi_policy="expand",
        tile_padding=2,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=False,
    ),
    # A1 AOV re-lighting — purely per-pixel, no spatial sampling.
    "relight": NodeCapability(
        node_type="relight",
        action_type="effect",
        effect_type="relight",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=False,
    ),
    # A2 Cryptomatte Select — per-pixel id comparison.
    "cryptomatte_select": NodeCapability(
        node_type="cryptomatte_select",
        action_type="effect",
        effect_type="cryptomatte_select",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Keyer",
        fusible=False,
    ),
    # A3 depth tools.  zblur gathers up to max_blur_radius px.
    "zblur": NodeCapability(
        node_type="zblur",
        action_type="effect",
        effect_type="zblur",
        roi_policy="expand",
        tile_padding=12,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=False,
    ),
    "fog": NodeCapability(
        node_type="fog",
        action_type="effect",
        effect_type="fog",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=False,
    ),
    "depth_matte": NodeCapability(
        node_type="depth_matte",
        action_type="effect",
        effect_type="depth_matte",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Mask",
        fusible=False,
    ),
    # B3 God Rays — radial march is global: full-frame-only, no ROI clip.
    "godrays": NodeCapability(
        node_type="godrays",
        action_type="effect",
        effect_type="godrays",
        roi_policy="full_frame",
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=False,
    ),
    # B4 lens kit — CA shifts by up to |offset| px; the others per-pixel.
    "chromatic_aberration": NodeCapability(
        node_type="chromatic_aberration",
        action_type="effect",
        effect_type="chromatic_aberration",
        roi_policy="expand",
        tile_padding=2,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=False,
    ),
    "vignette": NodeCapability(
        node_type="vignette",
        action_type="effect",
        effect_type="vignette",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=False,
    ),
    "lens_flare": NodeCapability(
        node_type="lens_flare",
        action_type="effect",
        effect_type="lens_flare",
        roi_policy="full_frame",
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=False,
    ),

    # ── Keyer Nodes ──
    "keyer": NodeCapability(
        node_type="keyer",
        action_type="effect",
        effect_type="keyer",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Keyer",
    ),
    "luma_keyer": NodeCapability(
        node_type="luma_keyer",
        action_type="effect",
        effect_type="luma_keyer",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Keyer",
    ),
    "chroma_keyer": NodeCapability(
        node_type="chroma_keyer",
        action_type="effect",
        effect_type="chroma_keyer",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Keyer",
    ),
    "difference_keyer": NodeCapability(
        node_type="difference_keyer",
        action_type="effect",
        effect_type="difference_keyer",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="Keyer",
    ),
    "matte_cleanup": NodeCapability(
        node_type="matte_cleanup",
        action_type="effect",
        effect_type="matte_cleanup",
        roi_policy="expand",
        tile_padding=5,
        owns_color_texture=True,
        requires_gl=True,
        category="Keyer",
        # Matte cleanup dispatches to the existing morphology/blur utilities
        # plus its alpha-aware levels shader.
        is_placeholder=False,
    ),
    "light_wrap": NodeCapability(
        node_type="light_wrap",
        action_type="effect",
        effect_type="light_wrap",
        roi_policy="expand",
        tile_padding=12,
        owns_color_texture=True,
        requires_gl=True,
        category="Keyer",
        fusible=False,
        is_placeholder=False,
    ),

    # ── Mask Nodes ──
    "mask_merge": NodeCapability(
        node_type="mask_merge",
        action_type="effect",
        effect_type="mask_merge",
        roi_policy="merge_union",
        owns_color_texture=True,
        requires_gl=True,
        category="Mask",
    ),
    "roto_shape": NodeCapability(
        node_type="roto_shape",
        action_type="effect",
        effect_type="roto_shape",
        roi_policy="full_frame",
        owns_color_texture=True,
        requires_gl=True,
        category="Mask",
    ),

    # C2: paint strokes are rasterized by the evaluator (_apply_paint);
    # owns_color_texture=True — the effect marker produces fresh textures
    # owned by the cache (mirrors roto_shape).
    "paint": NodeCapability(
        node_type="paint",
        action_type="effect",
        effect_type="paint",
        roi_policy="full_frame",
        owns_color_texture=True,
        requires_gl=True,
        category="Paint",
    ),

    # C1: stateless deterministic emitter — self-managed texture like
    # text (owns_color_texture=False keeps the cache manager off its glo).
    "particle": NodeCapability(
        node_type="particle",
        action_type="source",
        roi_policy="full_frame",
        requires_gl=True,
        category="Particle",
        owns_color_texture=False,
    ),
    # A4 Deflicker — persistent instance + own CPU evaluation (particle
    # precedent): the temporal ring buffer must survive across frames.
    "deflicker": NodeCapability(
        node_type="deflicker",
        action_type="source",
        roi_policy="full_frame",
        requires_gl=True,
        category="Filter",
        owns_color_texture=False,
    ),
    # LIB1 内容库节点（计划 L / L04：C01/C17）
    "lib1_gradient_map": NodeCapability(
        node_type="lib1_gradient_map",
        action_type="effect",
        effect_type="lib1_gradient_map",
        roi_policy="expand",
        tile_padding=0,  # 纯逐像素映射
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=True,
    ),
    "lib1_rgb_matrix": NodeCapability(
        node_type="lib1_rgb_matrix",
        action_type="effect",
        effect_type="lib1_rgb_matrix",
        roi_policy="expand",
        tile_padding=0,  # 纯逐像素矩阵
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=True,
    ),
    "lib1_despill": NodeCapability(
        node_type="lib1_despill",
        action_type="effect",
        effect_type="lib1_despill",
        roi_policy="expand",
        tile_padding=0,  # 纯逐像素去溢色
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=True,
    ),
    "lib1_solarize": NodeCapability(
        node_type="lib1_solarize",
        action_type="effect",
        effect_type="lib1_solarize",
        roi_policy="expand",
        tile_padding=0,  # 纯逐像素折返
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=True,
    ),
    "lib1_mosaic": NodeCapability(
        node_type="lib1_mosaic",
        action_type="effect",
        effect_type="lib1_mosaic",
        roi_policy="expand",
        tile_padding=0,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=True,
    ),
    "lib1_crt": NodeCapability(
        node_type="lib1_crt",
        action_type="effect",
        effect_type="lib1_crt",
        roi_policy="expand",
        tile_padding=0,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=True,
    ),
    "lib1_glitch": NodeCapability(
        node_type="lib1_glitch",
        action_type="effect",
        effect_type="lib1_glitch",
        roi_policy="expand",
        tile_padding=0,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=True,
    ),
    "lib1_kaleidoscope": NodeCapability(
        node_type="lib1_kaleidoscope",
        action_type="effect",
        effect_type="lib1_kaleidoscope",
        roi_policy="expand",
        tile_padding=0,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=True,
    ),
    "lib1_median": NodeCapability(
        node_type="lib1_median",
        action_type="effect",
        effect_type="lib1_median",
        roi_policy="expand",
        tile_padding=2,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=True,
    ),
    "lib1_convolution": NodeCapability(
        node_type="lib1_convolution",
        action_type="effect",
        effect_type="lib1_convolution",
        roi_policy="expand",
        tile_padding=1,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=True,
    ),
    "lib1_halation": NodeCapability(
        node_type="lib1_halation",
        action_type="effect",
        effect_type="lib1_halation",
        roi_policy="expand",
        tile_padding=0,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=True,
    ),
    "lib1_halftone": NodeCapability(
        node_type="lib1_halftone",
        action_type="effect",
        effect_type="lib1_halftone",
        roi_policy="expand",
        tile_padding=0,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=True,
    ),
    "lib1_hatching": NodeCapability(
        node_type="lib1_hatching",
        action_type="effect",
        effect_type="lib1_hatching",
        roi_policy="expand",
        tile_padding=0,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=True,
    ),
    "lib1_polar_coordinates": NodeCapability(
        node_type="lib1_polar_coordinates",
        action_type="effect",
        effect_type="lib1_polar_coordinates",
        roi_policy="expand",
        tile_padding=0,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=True,
    ),
    "lib1_tile_array": NodeCapability(
        node_type="lib1_tile_array",
        action_type="effect",
        effect_type="lib1_tile_array",
        roi_policy="expand",
        tile_padding=0,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=True,
    ),
    "lib1_distance_matte": NodeCapability(
        node_type="lib1_distance_matte",
        action_type="effect",
        effect_type="lib1_distance_matte",
        roi_policy="full_frame",   # 全帧声明子门（不宣称 tile-safe）
        tile_padding=0,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=False,   # CPU 全帧计算不参与融合
    ),
    "lib1_fill_holes": NodeCapability(
        node_type="lib1_fill_holes",
        action_type="effect",
        effect_type="lib1_fill_holes",
        roi_policy="full_frame",
        tile_padding=0,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=False,
    ),
    "lib1_vector_displace": NodeCapability(
        node_type="lib1_vector_displace",
        action_type="effect",
        effect_type="lib1_vector_displace",
        roi_policy="expand",
        tile_padding=0,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=True,
    ),
    "lib1_stmap": NodeCapability(
        node_type="lib1_stmap",
        action_type="effect",
        effect_type="lib1_stmap",
        roi_policy="expand",
        tile_padding=0,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=True,
    ),
    "lib1_frequency_separation": NodeCapability(
        node_type="lib1_frequency_separation",
        action_type="effect",
        effect_type="lib1_frequency_separation",
        roi_policy="expand",
        tile_padding=0,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=False,   # 双输出不参与融合
    ),
    "lib1_edge_extend": NodeCapability(
        node_type="lib1_edge_extend",
        action_type="effect",
        effect_type="lib1_edge_extend",
        roi_policy="expand",
        tile_padding=8,  # 表内静态上限；实际随 radius 参数（0–8）由
        #                # tile_padding_computer 取节点 _tile_padding 动态值
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=False,  # 乒乓多 pass 不参与融合
    ),
    # C1 generative expand/fill — persistent node, own evaluation.
    "ai_gen_expand": NodeCapability(
        node_type="ai_gen_expand",
        action_type="source",
        roi_policy="full_frame",
        requires_gl=True,
        category="AI",
        owns_color_texture=False,
    ),
    # C2 RIFE interpolation — stateless but CPU-ONNX evaluated.
    "frame_interpolate": NodeCapability(
        node_type="frame_interpolate",
        action_type="source",
        roi_policy="full_frame",
        requires_gl=True,
        category="Time",
        owns_color_texture=False,
    ),
    # B1 volume — downgraded CPU path (pyopenvdb spike failed).
    "volume3d": NodeCapability(
        node_type="volume3d",
        action_type="source",
        roi_policy="full_frame",
        requires_gl=True,
        category="3D",
        owns_color_texture=False,
    ),
    # B2 point cloud — source node with own CPU rasterization.
    "pointcloud3d": NodeCapability(
        node_type="pointcloud3d",
        action_type="source",
        roi_policy="full_frame",
        requires_gl=True,
        category="3D",
        owns_color_texture=False,
    ),
    # C3 face workflow — container + marker nodes.
    "face_track": NodeCapability(
        node_type="face_track",
        action_type="source",
        roi_policy="full_frame",
        requires_gl=True,
        category="AI",
        owns_color_texture=False,
    ),
    "face_smooth": NodeCapability(
        node_type="face_smooth",
        action_type="effect",
        effect_type="denoise",  # reuses the validated bilateral shader
        roi_policy="expand",
        tile_padding=4,
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=False,
    ),

    # C7: grid warp is an effect with dynamic tile padding (identity grid
    # needs none); the evaluator owns the output texture.
    "mesh_warp": NodeCapability(
        node_type="mesh_warp",
        action_type="effect",
        effect_type="mesh_warp",
        roi_policy="full_frame",
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
    ),

    # ── Utility Nodes ──
    "dot": NodeCapability(
        node_type="dot",
        action_type="passthrough",
        roi_policy="passthrough",
        owns_color_texture=False,
        requires_gl=False,
        category="Utility",
        fusible=False,
        bypass_mapping={"result": "input"},
    ),
    "lens_distortion": NodeCapability(
        # A4: spatial warp — tile padding scales with the k coefficients.
        node_type="lens_distortion",
        action_type="effect",
        effect_type="lens_distortion",
        roi_policy="expand",
        owns_color_texture=True,
        requires_gl=True,
        category="Filter",
        fusible=False,
        bypass_mapping={"result": "image"},
    ),
    "tracker": NodeCapability(
        # A1: data node — passes the image through; analysis is a page-driven
        # worker writing the track_data parameter.
        node_type="tracker",
        action_type="passthrough",
        roi_policy="passthrough",
        owns_color_texture=False,
        requires_gl=False,
        category="Transform",
        fusible=False,
        bypass_mapping={"image": "image"},
    ),
    "noop": NodeCapability(
        node_type="noop",
        action_type="passthrough",
        roi_policy="passthrough",
        owns_color_texture=False,
        requires_gl=False,
        category="Utility",
        fusible=False,
        bypass_mapping={"result": "input"},
    ),
    "compare": NodeCapability(
        node_type="compare",
        action_type="effect",
        effect_type="compare",
        roi_policy="merge_union",
        owns_color_texture=True,
        requires_gl=True,
        category="Utility",
        is_placeholder=False,
    ),

    # ── Time Nodes ──
    "time_offset": NodeCapability(
        node_type="time_offset",
        action_type="effect",
        effect_type="time_offset",
        roi_policy="identity",
        requires_frame_number=True,
        owns_color_texture=False,  # Passthrough with frame remap
        requires_gl=False,
        category="Time",
    ),
    "frame_hold": NodeCapability(
        node_type="frame_hold",
        action_type="effect",
        effect_type="frame_hold",
        roi_policy="identity",
        requires_frame_number=True,
        owns_color_texture=False,
        requires_gl=False,
        category="Time",
    ),
    "retime": NodeCapability(
        node_type="retime",
        action_type="effect",
        effect_type="retime",
        roi_policy="identity",
        requires_frame_number=True,
        owns_color_texture=False,
        requires_gl=False,
        category="Time",
    ),

    # ── AI Nodes ──
    "ai_generate": NodeCapability(
        node_type="ai_generate",
        action_type="effect",
        effect_type="ai_generate",
        roi_policy="full_frame",
        owns_color_texture=True,
        requires_gl=True,
        category="AI",
        is_placeholder=True,  # Needs external model
    ),
    "ai_inpaint": NodeCapability(
        node_type="ai_inpaint",
        action_type="effect",
        effect_type="ai_inpaint",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="AI",
        is_placeholder=True,
    ),
    "ai_expand": NodeCapability(
        node_type="ai_expand",
        action_type="effect",
        effect_type="ai_expand",
        roi_policy="expand",
        owns_color_texture=True,
        requires_gl=True,
        category="AI",
        is_placeholder=True,
    ),
    "ai_remove": NodeCapability(
        node_type="ai_remove",
        action_type="effect",
        effect_type="ai_remove",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="AI",
        # B6: wired to the async LaMa compute branch (evaluators/base_evaluator
        # :: _evaluate_ai_node). ai_inpaint / ai_expand remain placeholders
        # until their own wiring task lifts them.
        is_placeholder=False,
    ),
    "ai_denoise": NodeCapability(
        node_type="ai_denoise",
        action_type="effect",
        effect_type="ai_denoise",
        roi_policy="identity",
        owns_color_texture=True,
        requires_gl=True,
        category="AI",
        is_placeholder=True,
    ),

    # ── Group Nodes ──
    "group": NodeCapability(
        node_type="group",
        action_type="passthrough",
        effect_type=None,
        roi_policy="passthrough",
        owns_color_texture=False,
        requires_gl=False,
        category="Group",
        is_placeholder=False,
    ),
    "precomp": NodeCapability(
        node_type="precomp",
        action_type="passthrough",
        roi_policy="passthrough",
        owns_color_texture=False,
        requires_gl=False,
        category="Group",
        bypass_mapping={"result": "image"},
    ),
}


# ── Helper Sets Derived from Table ──

def _build_derived_sets() -> tuple:
    """Build helper sets from the capability table for compatibility."""
    source_types: Set[str] = set()
    effect_types: Set[str] = set()
    output_types: Set[str] = set()
    passthrough_types: Set[str] = set()
    placeholder_types: Set[str] = set()
    cache_owned_types: Set[str] = set()
    tile_padding_types: Set[str] = set()
    fusible_types: Set[str] = set()
    frame_number_types: Set[str] = set()
    node_type_map: Dict[str, str] = {}  # node_type -> effect_type

    for node_type, cap in _NODE_CAPABILITY_TABLE.items():
        if cap.action_type == "source":
            source_types.add(node_type)
        elif cap.action_type == "effect":
            effect_types.add(node_type)
            if cap.effect_type:
                node_type_map[node_type] = cap.effect_type
        elif cap.action_type == "output":
            output_types.add(node_type)
        elif cap.action_type == "passthrough":
            passthrough_types.add(node_type)

        if cap.is_placeholder:
            placeholder_types.add(node_type)

        if cap.owns_color_texture:
            cache_owned_types.add(node_type)

        if cap.tile_padding > 0:
            tile_padding_types.add(node_type)

        if cap.fusible:
            fusible_types.add(node_type)

        if cap.requires_frame_number:
            frame_number_types.add(node_type)

    return (
        frozenset(source_types),
        frozenset(effect_types),
        frozenset(output_types),
        frozenset(passthrough_types),
        frozenset(placeholder_types),
        frozenset(cache_owned_types),
        frozenset(tile_padding_types),
        frozenset(fusible_types),
        frozenset(frame_number_types),
        node_type_map,
    )

# Export derived sets
(
    SOURCE_NODE_TYPES,
    EFFECT_NODE_TYPES,
    OUTPUT_NODE_TYPES,
    PASSTHROUGH_NODE_TYPES,
    PLACEHOLDER_NODE_TYPES,
    CACHE_OWNED_TYPES,
    TILE_PADDING_TYPES,
    FUSIBLE_NODE_TYPES,
    FRAME_NUMBER_TYPES,
    NODE_TYPE_TO_EFFECT_TYPE,
) = _build_derived_sets()


# ── Main API: get_node_capability ──

# ── T-P1-60：节点契约五分类 ──
# 计划卡：93 节点不得再用同一 image-input harness 一概判断。分类是
# fixture 选择与"可解释状态"的依据（ai_model 缺模型 → blocked_missing_model；
# 3d/file_output 缺资源 → blocked_missing_resource；source/generator 平图
# 可为 passed_flat_by_contract；image_filter 平图 → failed）。
NodeContractClass = Literal[
    "image_filter",      # 有图像输入的滤镜/变换/键控/合并
    "source",            # 引用外部媒体的输入（video_frame/image_input/...）
    "generator",         # 程序化生成（constant/text/ramp/noise/particle/...）
    "ai_model",          # 依赖模型包的 AI 节点
    "3d_file_output",    # 3D 场景元素与文件/查看器输出
]

_GENERATOR_NODE_TYPES = frozenset({
    # Input 类的程序化生成器（非外部媒体引用）
    "constant", "color_input", "text", "ramp", "noise", "motion_graphic",
})
_SOURCE_MEDIA_NODE_TYPES = frozenset({
    "image_input", "video_frame", "timeline_clip_input",
})


def get_node_contract_class(node_type: str) -> NodeContractClass:
    """节点契约五分类（T-P1-60 步骤 1）。

    判定顺序：AI → 3D/输出 → 外部媒体 source → 程序化 generator →
    其余一律 image_filter。未知节点类型按 image_filter 处理（调用方
    以 is_placeholder/registry 可见性另行解释）。
    """
    capability = get_node_capability(node_type)
    category = (capability.category if capability is not None else "") or ""
    action = capability.action_type if capability is not None else ""
    if category == "AI":
        return "ai_model"
    if category == "3D" or action == "output":
        return "3d_file_output"
    if node_type in _SOURCE_MEDIA_NODE_TYPES:
        return "source"
    if node_type in _GENERATOR_NODE_TYPES or category in ("Paint", "Particle"):
        return "generator"
    return "image_filter"


def _synthesize_registry_capability(node_type: str,
                                    node_cls: type) -> NodeCapability:
    """Build a conservative capability for a registry-only node type.

    AUD-5008: types registered at runtime via ``register_node_type`` /
    ``register_plugin_nodes`` (plugin authors cannot edit the static
    capability table) used to get ``None`` from :func:`get_node_capability`
    and were consequently classified as *placeholder* by the render
    scheduler — registered, valid nodes silently stopped rendering.

    The synthesis honours the ``IS_PLACEHOLDER`` class convention and
    otherwise assumes a plain effect node (the common plugin shape: image
    in → processed image out): action "effect", its own ``effect_type``,
    and effect-style texture ownership.  Non-placeholder plugin nodes are
    therefore dispatched rather than skipped.
    """
    is_placeholder = bool(getattr(node_cls, "IS_PLACEHOLDER", False))
    return NodeCapability(
        node_type=node_type,
        action_type="placeholder" if is_placeholder else "effect",
        effect_type=None if is_placeholder else node_type,
        owns_color_texture=not is_placeholder,
        roi_policy="full_frame",
        category="Plugins",
        is_placeholder=is_placeholder,
    )


def get_node_capability(node_type: str) -> Optional[NodeCapability]:
    """Get the capability definition for a node type.

    Resolution order:
    1. Capability table entry (provides curated defaults).
    2. Node class attributes merged over the table entry — the
       ``IS_PLACEHOLDER`` class attribute overrides the table's
       ``is_placeholder``.
    3. Types missing from the table but present in ``NODE_REGISTRY``
       (runtime/plugin registrations) get a synthesized conservative
       capability (AUD-5008) instead of None.
    4. Genuinely unknown types return None (treated as placeholder
       elsewhere).

    Args:
        node_type: Node type string (e.g., "blur", "grade").

    Returns:
        NodeCapability if found or synthesized, None if unknown.
    """
    from .node_engine import NODE_REGISTRY

    cap = _NODE_CAPABILITY_TABLE.get(node_type)
    if cap is None:
        node_cls = NODE_REGISTRY.get(node_type)
        if node_cls is None:
            return None
        # Registry-only type (e.g. a plugin node registered at runtime):
        # synthesize a default so registered nodes are never mistaken for
        # placeholders by the scheduler (AUD-5008).
        return _synthesize_registry_capability(node_type, node_cls)

    # Check node class attributes for overrides
    # Lazy import to avoid circular dependency
    node_cls = NODE_REGISTRY.get(node_type)
    if node_cls is not None:
        # Check IS_PLACEHOLDER attribute
        is_placeholder_attr = getattr(node_cls, "IS_PLACEHOLDER", False)
        if is_placeholder_attr != cap.is_placeholder:
            # Class attribute overrides table - create updated capability
            return NodeCapability(
                node_type=cap.node_type,
                action_type="placeholder" if is_placeholder_attr else cap.action_type,
                effect_type=cap.effect_type,
                owns_color_texture=cap.owns_color_texture,
                owns_depth_texture=cap.owns_depth_texture,
                roi_policy=cap.roi_policy,
                tile_padding=cap.tile_padding,
                fusible=cap.fusible,
                requires_frame_number=cap.requires_frame_number,
                requires_gl=cap.requires_gl,
                category=cap.category,
                is_placeholder=is_placeholder_attr,
                bypass_mapping=cap.bypass_mapping,
            )

    return cap


def is_placeholder_node_type(node_type: str) -> bool:
    """Check if a node type should be treated as placeholder.

    Replaces the hand-written lists in render_scheduler.py and
    provides a single source of truth.

    Args:
        node_type: Node type string.

    Returns:
        True if the node type is placeholder or unknown.
    """
    cap = get_node_capability(node_type)
    if cap is None:
        return True  # Unknown types are placeholders
    return cap.is_placeholder


def is_source_node_type(node_type: str) -> bool:
    """Check if a node type is a source (GPU-stateful) node."""
    cap = get_node_capability(node_type)
    if cap is None:
        return False
    return cap.action_type == "source"


def is_effect_node_type(node_type: str) -> bool:
    """Check if a node type is an effect node needing GPU dispatch."""
    cap = get_node_capability(node_type)
    if cap is None:
        return False
    return cap.action_type == "effect"


def is_output_node_type(node_type: str) -> bool:
    """Check if a node type is an output node."""
    cap = get_node_capability(node_type)
    if cap is None:
        return False
    return cap.action_type == "output"


def is_passthrough_node_type(node_type: str) -> bool:
    """Check if a node type is a passthrough node."""
    cap = get_node_capability(node_type)
    if cap is None:
        return False
    return cap.action_type == "passthrough"


def get_effect_type(node_type: str) -> Optional[str]:
    """Get the effect dispatcher key for a node type.

    Args:
        node_type: Node type string.

    Returns:
        Effect type string (e.g., "blur", "merge") or None for non-effect nodes.
    """
    cap = get_node_capability(node_type)
    if cap is None:
        return None
    return cap.effect_type


def get_tile_padding(node_type: str) -> int:
    """Get the default tile padding for a node type.

    Note: Actual padding may depend on node parameters (e.g., blur radius).
    This returns the default/maximum padding value.

    Args:
        node_type: Node type string.

    Returns:
        Padding in pixels (0 if no padding needed).
    """
    cap = get_node_capability(node_type)
    if cap is None:
        return 0
    return cap.tile_padding


def owns_color_texture(node_type: str) -> bool:
    """Check if a node type owns its output color texture.

    Used by cache manager to determine which textures to release
    when cache entries are evicted.

    Args:
        node_type: Node type string.

    Returns:
        True if the node creates new textures (should be cache-owned).
    """
    cap = get_node_capability(node_type)
    if cap is None:
        return False
    return cap.owns_color_texture


def owns_depth_texture(node_type: str) -> bool:
    """Check if a node type owns its output depth texture."""
    cap = get_node_capability(node_type)
    if cap is None:
        return False
    return cap.owns_depth_texture


def is_fusible(node_type: str) -> bool:
    """Check if a node type can be fused into a mega-shader chain.

    Args:
        node_type: Node type string.

    Returns:
        True if the node is fusible (single-input, per-pixel effect).
    """
    cap = get_node_capability(node_type)
    if cap is None:
        return False
    return cap.fusible


def requires_frame_number(node_type: str) -> bool:
    """Check if a node type needs frame number in its cache key.

    Args:
        node_type: Node type string.

    Returns:
        True if the node is frame-dependent.
    """
    cap = get_node_capability(node_type)
    if cap is None:
        return False
    return cap.requires_frame_number


def get_roi_policy(node_type: str) -> ROIPolicy:
    """Get the ROI propagation policy for a node type.

    Args:
        node_type: Node type string.

    Returns:
        ROI policy string (defaults to "identity" if unknown).
    """
    cap = get_node_capability(node_type)
    if cap is None:
        return "identity"
    return cap.roi_policy


def get_bypass_mapping(node_type: str) -> Optional[Dict[str, str]]:
    """Get the bypass/disabled mapping for a node type.

    Args:
        node_type: Node type string.

    Returns:
        Dict mapping output_port -> input_port, or None for default behavior.
    """
    cap = get_node_capability(node_type)
    if cap is None:
        return None
    return cap.bypass_mapping


# ── Registry Consistency Check ──

def check_registry_consistency() -> Dict[str, List[str]]:
    """Check if capability system matches NODE_REGISTRY.

    Uses get_node_capability() to get the FINAL capability (after class attribute
    overrides) and compares that against NODE_REGISTRY class attributes.

    Returns a dict with:
    - "missing_in_table": Node types in registry but not in capability table
    - "missing_in_registry": Node types in table but not in registry
    - "raw_placeholder_mismatch": Node types where raw table disagrees with class
                                  (informational only - get_node_capability() handles this)
    - "final_placeholder_mismatch": Node types where FINAL capability disagrees with class
                                    (this is the real error)
    - "missing_in_categories": Node types creatable in registry but absent from
                               NODE_CATEGORIES (invisible to the Add-Node menu)
                               except intentional exclusions (CP-001)
    - "unknown_in_categories": NODE_CATEGORIES entries that don't exist in registry

    Used by tests to ensure capabilities stay synchronized.
    """
    from .node_engine import NODE_REGISTRY

    result: Dict[str, List[str]] = {
        "missing_in_table": [],
        "missing_in_registry": [],
        "raw_placeholder_mismatch": [],  # Raw table differs from class (handled by override)
        "final_placeholder_mismatch": [],  # Final capability differs from class (real error)
        "missing_in_categories": [],
        "unknown_in_categories": [],
    }

    # Check registry against table. Plugin-owned runtime registrations
    # (register_plugin_nodes) are by design absent from the static table —
    # get_node_capability synthesizes a conservative capability for them
    # (AUD-5008), so they are not a table-sync defect.
    from .node_engine import _PLUGIN_OWNED_NODE_TYPES

    for node_type in NODE_REGISTRY:
        if node_type in _PLUGIN_OWNED_NODE_TYPES:
            continue
        if node_type not in _NODE_CAPABILITY_TABLE:
            result["missing_in_table"].append(node_type)

    # Check table against registry
    for node_type in _NODE_CAPABILITY_TABLE:
        if node_type not in NODE_REGISTRY:
            result["missing_in_registry"].append(node_type)

    # Check placeholder consistency
    for node_type in NODE_REGISTRY:
        # Get raw table capability
        raw_cap = _NODE_CAPABILITY_TABLE.get(node_type)
        if raw_cap is None:
            continue

        node_cls = NODE_REGISTRY.get(node_type)
        if node_cls is None:
            continue

        # Get class attribute
        is_placeholder_attr = getattr(node_cls, "IS_PLACEHOLDER", False)

        # Check raw table vs class (informational)
        if is_placeholder_attr != raw_cap.is_placeholder:
            result["raw_placeholder_mismatch"].append(node_type)

        # Get FINAL capability (after class override)
        final_cap = get_node_capability(node_type)
        if final_cap is None:
            continue

        # Check final capability vs class (this is the real consistency check)
        if is_placeholder_attr != final_cap.is_placeholder:
            result["final_placeholder_mismatch"].append(node_type)

    # CP-001: category-menu coverage. Every creatable registry entry must be
    # reachable from the Add-Node menu unless explicitly excluded below.
    from .node_engine import NODE_CATEGORIES

    # color_input: deprecated, deserialization-only (auto-migrated on load).
    # motion_graphic: deferred registration, filled by teaching_animation module.
    _category_hidden = {"color_input", "motion_graphic"}
    listed = {node_type for entries in NODE_CATEGORIES.values() for node_type, _ in entries}
    for node_type in NODE_REGISTRY:
        if node_type in _category_hidden:
            continue
        if node_type not in listed:
            result["missing_in_categories"].append(node_type)
    for node_type in sorted(listed):
        if node_type not in NODE_REGISTRY:
            result["unknown_in_categories"].append(node_type)

    return result


def get_all_capabilities() -> Dict[str, NodeCapability]:
    """Get all node capabilities as a dict.

    Returns a copy of the capability table.
    """
    return dict(_NODE_CAPABILITY_TABLE)
