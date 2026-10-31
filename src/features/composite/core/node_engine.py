"""
Node Graph Engine - Manages the directed acyclic graph of nodes.

Handles:
- Adding/removing nodes
- Connecting/disconnecting ports
- Topological sort for evaluation order
- Graph evaluation (executes all nodes in order)
- Incremental evaluation (only dirty nodes)
- Serialization (save/load)
"""

import copy
import math
import threading
import logging
from collections import OrderedDict, deque
from typing import Any, Dict, List, Optional, Set, Tuple, Type

logger = logging.getLogger(__name__)

from src.features.composite.nodes.base_node import Node, Connection, TextureResult
from src.features.composite.core.graph_snapshot import GraphSnapshot, NodeSnapshot, ConnectionSnapshot
from src.features.composite.nodes.color_node import COLORNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.image_input_node import ImageInputNode
from src.features.composite.nodes.blend_node import MergeNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.grade_node import GradeNode
from src.features.composite.nodes.color_correct_node import ColorCorrectNode
from src.features.composite.nodes.huecorrect_node import HueCorrectNode
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.invert_node import InvertNode
from src.features.composite.nodes.premult_node import PremultNode
from src.features.composite.nodes.unpremult_node import UnpremultNode
from src.features.composite.nodes.shuffle_node import ShuffleNode
from src.features.composite.nodes.copy_channel_node import CopyChannelNode
from src.features.composite.nodes.channel_merge_node import ChannelMergeNode
from src.features.composite.nodes.cs_transform_node import CSTransformNode
from src.features.composite.nodes.reformat_node import ReformatNode
from src.features.composite.nodes.crop_node import CropNode
from src.features.composite.nodes.glow_node import GlowNode
from src.features.composite.nodes.denoise_node import DenoiseNode
from src.features.composite.nodes.grain_node import GrainNode
from src.features.composite.nodes.vectorblur_node import VectorBlurNode
from src.features.composite.nodes.colorlookup_node import ColorLookupNode
from src.features.composite.nodes.cdl_node import CDLNode
from src.features.composite.nodes.degrain_node import DeGrainNode
from src.features.composite.nodes.mask_node import MaskNode
from src.features.composite.nodes.matte_node import MatteNode
from src.features.composite.nodes.keyer_node import KeyerNode
from src.features.composite.nodes.video_frame_node import VideoFrameNode
from src.features.composite.nodes.file_output_node import FileOutputNode
from src.features.composite.nodes.dissolve_node import DissolveNode
from src.features.composite.nodes.compare_node import CompareNode
from src.features.composite.nodes.switch_node import SwitchNode
from src.features.composite.nodes.set_alpha_node import SetAlphaNode
from src.features.composite.nodes.remove_channels_node import RemoveChannelsNode
from src.features.composite.nodes.dot_node import DotNode
from src.features.composite.nodes.noop_node import NoOpNode
from src.features.composite.nodes.clamp_node import ClampNode
from src.features.composite.nodes.normalize_node import NormalizeNode
from src.features.composite.nodes.chroma_keyer_node import ChromaKeyerNode
from src.features.composite.nodes.luma_keyer_node import LumaKeyerNode
from src.features.composite.nodes.sharpen_node import SharpenNode
from src.features.composite.nodes.erode_dilate_node import ErodeDilateNode
from src.features.composite.nodes.defocus_node import DefocusNode
from src.features.composite.nodes.cornerpin_node import CornerPinNode
from src.features.composite.nodes.mask_merge_node import MaskMergeNode
from src.features.composite.nodes.matte_cleanup_node import MatteCleanupNode
from src.features.composite.nodes.light_wrap_node import LightWrapNode
from src.features.composite.nodes.relight_node import RelightNode
from src.features.composite.nodes.cryptomatte_select_node import CryptomatteSelectNode
from src.features.composite.nodes.zblur_node import ZBlurNode
from src.features.composite.nodes.fog_node import FogNode
from src.features.composite.nodes.depth_matte_node import DepthMatteNode
from src.features.composite.nodes.godrays_node import GodRaysNode
from src.features.composite.nodes.chromatic_aberration_node import ChromaticAberrationNode
from src.features.composite.nodes.vignette_node import VignetteNode
from src.features.composite.nodes.lens_flare_node import LensFlareNode
from src.features.composite.nodes.deflicker_node import DeflickerNode
from src.features.composite.nodes.ai_gen_expand_node import AIGenExpandNode
from src.features.composite.nodes.frame_interpolate_node import FrameInterpolateNode
from src.features.composite.nodes.face_track_node import FaceTrackNode
from src.features.composite.nodes.face_smooth_node import FaceSmoothNode
from src.features.composite.nodes.pointcloud3d_node import PointCloud3DNode
from src.features.composite.nodes.volume3d_node import Volume3DNode
from src.features.composite.nodes.time_offset_node import TimeOffsetNode
from src.features.composite.nodes.frame_hold_node import FrameHoldNode
from src.features.composite.nodes.retime_node import RetimeNode
from src.features.composite.nodes.tracker_node import TrackerNode
from src.features.composite.nodes.lens_distortion_node import LensDistortionNode
from src.features.composite.nodes.group_node import GroupNode
from src.features.composite.nodes.precomp_node import PrecompNode
from src.features.composite.nodes.unknown_placeholder_node import UnknownPlaceholderNode
from src.features.composite.nodes.lut_node import LutNode
from src.features.composite.nodes.deep_merge_node import DeepMergeNode
from src.features.composite.nodes.keymix_node import KeymixNode
from src.features.composite.nodes.noise_node import NoiseNode
from src.features.composite.nodes.ramp_node import RampNode
from src.features.composite.nodes.edge_detect_node import EdgeDetectNode
from src.features.composite.nodes.difference_keyer_node import DifferenceKeyerNode
from src.features.composite.nodes.text_node import TextNode
from src.features.composite.nodes.roto_shape_node import RotoShapeNode
from src.features.composite.nodes.paint_node import PaintNode
from src.features.composite.nodes.particle_node import ParticleNode
from src.features.composite.nodes.warp_node import WarpNode
from src.features.composite.nodes.ai_generate_node import AIGenerateNode
from src.features.composite.nodes.ai_inpaint_node import AIInpaintNode
from src.features.composite.nodes.ai_expand_node import AIExpandNode
from src.features.composite.nodes.ai_remove_node import AIRemoveNode
from src.features.composite.nodes.ai_denoise_node import AIDenoiseNode
from src.features.composite.nodes.timeline_clip_input_node import TimelineClipInputNode
from src.features.composite.nodes.topos_color_node import ToposColorNode
from src.features.composite.nodes.scene3d_node import Scene3DNode
from src.features.composite.nodes.camera3d_node import Camera3DNode
from src.features.composite.nodes.light3d_node import Light3DNode
from src.features.composite.nodes.geo3d_node import Geo3DNode
from src.features.composite.nodes.model3d_node import Model3DNode
from src.features.composite.nodes.deformer3d_node import Deformer3DNode
from src.features.composite.nodes.lib1_nodes import (
    Lib1EdgeExtendNode, Lib1GradientMapNode,
    Lib1RgbMatrixNode, Lib1DespillNode, Lib1SolarizeNode,
    Lib1MosaicNode, Lib1CrtNode, Lib1GlitchNode, Lib1KaleidoscopeNode,
    Lib1MedianNode, Lib1ConvolutionNode, Lib1HalationNode,
    Lib1HalftoneNode, Lib1HatchingNode,
    Lib1PolarCoordinatesNode, Lib1TileArrayNode,
    Lib1DistanceMatteNode, Lib1FillHolesNode,
    Lib1VectorDisplaceNode, Lib1StmapNode,
    Lib1FrequencySeparationNode,
)


# Registry for node types (for deserialization)
# NODE_REGISTRY: Dict[str, Optional[Type[Node]]] = {
#
# DEFERRED_NODE_TYPES lists the node types whose registry value is None at
# import time ON PURPOSE: the owning (optional) feature module registers the
# concrete class via register_node_type() when it enables, and
# unregister_node_type() restores the None placeholder on teardown.  A None
# entry outside this map is an unexplained placeholder bug — the registry
# contract test enforces exactly that split.
DEFERRED_NODE_TYPES: Dict[str, str] = {
    "motion_graphic": "teaching_animation feature module (registered on enable)",
}

NODE_REGISTRY = {
    # Input nodes
    # DEPRECATED: color_input is kept for deserialization compatibility only.
    # On load, migrate_color_input_to_topos_color() auto-converts these to
    # timeline_clip_input -> topos_color chains.  New graphs should never
    # create color_input nodes.  See R-6 legacy cleanup.
    "color_input": COLORNode,
    "image_input": ImageInputNode,
    "constant": ConstantNode,
    "video_frame": VideoFrameNode,
    "timeline_clip_input": TimelineClipInputNode,
    # Optional Teaching Animation node. The feature module replaces this
    # placeholder with the concrete class when enabled, keeping core imports
    # independent from the optional package.
    "motion_graphic": None,
    "topos_color": ToposColorNode,
    "text": TextNode,
    "noise": NoiseNode,  # from noise_node.py
    "ramp": RampNode,  # from ramp_node.py
    # Output nodes
    "output": OutputNode,
    "file_output": FileOutputNode,
    # Merge nodes
    "blend": MergeNode,
    "merge": MergeNode,
    "deep_merge": DeepMergeNode,
    "keymix": KeymixNode,  # CP-011
    "premult": PremultNode,
    "unpremult": UnpremultNode,
    "mask": MaskNode,
    "matte": MatteNode,
    "dissolve": DissolveNode,  # from dissolve_node.py
    "switch": SwitchNode,  # from switch_node.py
    # Transform nodes
    "transform": TransformNode,
    "reformat": ReformatNode,
    "crop": CropNode,
    "cornerpin": CornerPinNode,
    # Channel nodes
    "shuffle": ShuffleNode,
    "copy_channel": CopyChannelNode,
    "channel_merge": ChannelMergeNode,  # from channel_merge_node.py
    "set_alpha": SetAlphaNode,  # from set_alpha_node.py
    "remove_channels": RemoveChannelsNode,  # from remove_channels_node.py
    # Color nodes
    "grade": GradeNode,
    "color_correct": ColorCorrectNode,
    "cs_transform": CSTransformNode,
    "huecorrect": HueCorrectNode,
    "lut": LutNode,
    # Filter nodes
    "blur": BlurNode,
    "glow": GlowNode,
    "denoise": DenoiseNode,
    "grain": GrainNode,  # CP-019
    "vectorblur": VectorBlurNode,  # CP-020
    "colorlookup": ColorLookupNode,  # CP-014
    "cdl": CDLNode,  # CP-015
    "degrain": DeGrainNode,  # CP-019
    "sharpen": SharpenNode,
    "erode_dilate": ErodeDilateNode,
    "defocus": DefocusNode,
    "edge_detect": EdgeDetectNode,  # from edge_detect_node.py
    "relight": RelightNode,  # A1 AOV re-lighting
    "zblur": ZBlurNode,  # A3 depth tools
    "fog": FogNode,
    "godrays": GodRaysNode,  # B3 volumetric light
    "chromatic_aberration": ChromaticAberrationNode,  # B4 lens kit
    "vignette": VignetteNode,
    "lens_flare": LensFlareNode,
    "deflicker": DeflickerNode,  # A4 temporal deflicker
    # Keyer nodes
    "keyer": KeyerNode,
    "luma_keyer": LumaKeyerNode,
    "chroma_keyer": ChromaKeyerNode,
    "difference_keyer": DifferenceKeyerNode,
    "matte_cleanup": MatteCleanupNode,
    "light_wrap": LightWrapNode,
    "cryptomatte_select": CryptomatteSelectNode,  # A2 ID matte
    "depth_matte": DepthMatteNode,  # A3 depth tools
    # Mask nodes
    "mask_merge": MaskMergeNode,
    "roto_shape": RotoShapeNode,
    "paint": PaintNode,
    "particle": ParticleNode,
    "mesh_warp": WarpNode,
    # Utility nodes
    "invert": InvertNode,
    "clamp": ClampNode,  # from clamp_node.py
    "normalize": NormalizeNode,  # from normalize_node.py
    "compare": CompareNode,  # from compare_node.py
    "dot": DotNode,  # from dot_node.py
    "noop": NoOpNode,  # from noop_node.py
    # Time nodes
    "time_offset": TimeOffsetNode,
    "frame_hold": FrameHoldNode,
    "frame_interpolate": FrameInterpolateNode,  # C2 RIFE in-betweening
    "retime": RetimeNode,
    "tracker": TrackerNode,
    "lens_distortion": LensDistortionNode,
    # AI nodes
    "ai_generate": AIGenerateNode,
    "ai_gen_expand": AIGenExpandNode,  # C1 generative expand/fill
    "ai_inpaint": AIInpaintNode,
    "ai_expand": AIExpandNode,
    "ai_remove": AIRemoveNode,
    "ai_denoise": AIDenoiseNode,
    "face_track": FaceTrackNode,  # C3 face landmarks
    "face_smooth": FaceSmoothNode,  # C3 skin smoothing
    # Group/Macro nodes
    "group": GroupNode,
    "precomp": PrecompNode,
    # 3D nodes
    "scene3d": Scene3DNode,
    "camera3d": Camera3DNode,
    "light3d": Light3DNode,
    "geo3d": Geo3DNode,
    "model3d": Model3DNode,
    "deformer3d": Deformer3DNode,
    "pointcloud3d": PointCloud3DNode,  # B2 point cloud
    "volume3d": Volume3DNode,  # B1 volume (downgraded path)
    # LIB1 内容库节点（计划 L / L04：C01/C17；dispatch 见 base_evaluator）
    "lib1_gradient_map": Lib1GradientMapNode,
    "lib1_edge_extend": Lib1EdgeExtendNode,
    "lib1_rgb_matrix": Lib1RgbMatrixNode,
    "lib1_despill": Lib1DespillNode,
    "lib1_solarize": Lib1SolarizeNode,
    "lib1_mosaic": Lib1MosaicNode,
    "lib1_crt": Lib1CrtNode,
    "lib1_glitch": Lib1GlitchNode,
    "lib1_kaleidoscope": Lib1KaleidoscopeNode,
    "lib1_median": Lib1MedianNode,
    "lib1_convolution": Lib1ConvolutionNode,
    "lib1_halation": Lib1HalationNode,
    "lib1_halftone": Lib1HalftoneNode,
    "lib1_hatching": Lib1HatchingNode,
    "lib1_polar_coordinates": Lib1PolarCoordinatesNode,
    "lib1_tile_array": Lib1TileArrayNode,
    "lib1_distance_matte": Lib1DistanceMatteNode,
    "lib1_fill_holes": Lib1FillHolesNode,
    "lib1_vector_displace": Lib1VectorDisplaceNode,
    "lib1_stmap": Lib1StmapNode,
    "lib1_frequency_separation": Lib1FrequencySeparationNode,
}

# Node categories for UI menus and toolbars
# Organized following NUKE/Fusion professional compositing conventions
NODE_CATEGORIES: Dict[str, List[Tuple[str, Optional[Type[Node]]]]] = {
    "Input": [
        ("image_input", ImageInputNode),
        ("constant", ConstantNode),
        ("video_frame", VideoFrameNode),
        ("timeline_clip_input", TimelineClipInputNode),
        ("text", TextNode),
        ("noise", NoiseNode),
        ("ramp", RampNode),
    ],
    "Output": [
        ("output", OutputNode),
        ("file_output", FileOutputNode),
    ],
    "Keyer": [
        ("keyer", KeyerNode),
        ("chroma_keyer", ChromaKeyerNode),
        ("luma_keyer", LumaKeyerNode),
        ("difference_keyer", DifferenceKeyerNode),
        ("light_wrap", LightWrapNode),
        ("cryptomatte_select", CryptomatteSelectNode),  # A2
        ("matte_cleanup", MatteCleanupNode),  # CP-001：注册表已有但曾漏出分类菜单
    ],
    "Merge": [
        ("blend", MergeNode),
        ("merge", MergeNode),
        ("deep_merge", DeepMergeNode),
        ("keymix", KeymixNode),  # CP-011
        ("premult", PremultNode),
        ("unpremult", UnpremultNode),
        ("matte", MatteNode),
        ("dissolve", DissolveNode),
        ("switch", SwitchNode),
    ],
    "Transform": [
        ("transform", TransformNode),
        ("reformat", ReformatNode),
        ("crop", CropNode),
        ("cornerpin", CornerPinNode),
        ("tracker", TrackerNode),
    ],
    "Channel": [
        ("shuffle", ShuffleNode),
        ("copy_channel", CopyChannelNode),
        ("channel_merge", ChannelMergeNode),
        ("set_alpha", SetAlphaNode),
        ("remove_channels", RemoveChannelsNode),
    ],
    "Color": [
        ("grade", GradeNode),
        ("color_correct", ColorCorrectNode),
        ("cs_transform", CSTransformNode),
        ("huecorrect", HueCorrectNode),
        ("lut", LutNode),
        ("topos_color", ToposColorNode),
        ("colorlookup", ColorLookupNode),  # CP-014
        ("cdl", CDLNode),  # CP-015
    ],
    "Filter": [
        ("lens_distortion", LensDistortionNode),
        ("mesh_warp", WarpNode),
        ("blur", BlurNode),
        ("glow", GlowNode),
        ("denoise", DenoiseNode),
        ("grain", GrainNode),  # CP-019
        ("vectorblur", VectorBlurNode),  # CP-020
        ("degrain", DeGrainNode),  # CP-019
        ("sharpen", SharpenNode),
        ("erode_dilate", ErodeDilateNode),
        ("defocus", DefocusNode),
        ("edge_detect", EdgeDetectNode),
        ("relight", RelightNode),  # A1 AOV re-lighting
        ("zblur", ZBlurNode),  # A3 depth tools
        ("fog", FogNode),
        ("godrays", GodRaysNode),  # B3
        ("chromatic_aberration", ChromaticAberrationNode),  # B4
        ("vignette", VignetteNode),
        ("lens_flare", LensFlareNode),
        ("deflicker", DeflickerNode),  # A4
        ("lib1_gradient_map", Lib1GradientMapNode),  # LIB1（计划 L / L04）
        ("lib1_edge_extend", Lib1EdgeExtendNode),  # LIB1（计划 L / L04）
        ("lib1_rgb_matrix", Lib1RgbMatrixNode),  # LIB1（计划 L / L16）
        ("lib1_despill", Lib1DespillNode),  # LIB1（计划 L / L16）
        ("lib1_solarize", Lib1SolarizeNode),  # LIB1（计划 L / L16）
        ("lib1_mosaic", Lib1MosaicNode),  # LIB1（计划 L / L16 批2）
        ("lib1_crt", Lib1CrtNode),  # LIB1（计划 L / L16 批2）
        ("lib1_glitch", Lib1GlitchNode),  # LIB1（计划 L / L16 批2）
        ("lib1_kaleidoscope", Lib1KaleidoscopeNode),  # LIB1（计划 L / L16 批2）
        ("lib1_median", Lib1MedianNode),  # LIB1（计划 L / L16 批3）
        ("lib1_convolution", Lib1ConvolutionNode),  # LIB1（计划 L / L16 批3）
        ("lib1_halation", Lib1HalationNode),  # LIB1（计划 L / L16 批3）
        ("lib1_halftone", Lib1HalftoneNode),  # LIB1（计划 L / L16 批3b）
        ("lib1_hatching", Lib1HatchingNode),  # LIB1（计划 L / L16 批3b）
        ("lib1_polar_coordinates", Lib1PolarCoordinatesNode),  # LIB1（L17）
        ("lib1_tile_array", Lib1TileArrayNode),  # LIB1（L17）
        ("lib1_distance_matte", Lib1DistanceMatteNode),  # LIB1（L17 批2 全帧）
        ("lib1_fill_holes", Lib1FillHolesNode),  # LIB1（L17 批2 全帧）
        ("lib1_vector_displace", Lib1VectorDisplaceNode),  # LIB1（L17 批3 多输入）
        ("lib1_stmap", Lib1StmapNode),  # LIB1（L17 批3 多输入）
        ("lib1_frequency_separation", Lib1FrequencySeparationNode),  # LIB1（L17 批3 多输出）
    ],
    "Mask": [
        ("mask", MaskNode),
        ("mask_merge", MaskMergeNode),
        ("roto_shape", RotoShapeNode),
        ("depth_matte", DepthMatteNode),  # A3 depth tools
    ],
    "Paint": [
        ("paint", PaintNode),
    ],
    "Particle": [
        ("particle", ParticleNode),
    ],
    "Utility": [
        ("invert", InvertNode),
        ("clamp", ClampNode),
        ("normalize", NormalizeNode),
        ("compare", CompareNode),
        ("dot", DotNode),
        ("noop", NoOpNode),
    ],
    "Time": [
        ("time_offset", TimeOffsetNode),
        ("frame_hold", FrameHoldNode),
        ("frame_interpolate", FrameInterpolateNode),  # C2
        ("retime", RetimeNode),
    ],
    "AI": [
        ("ai_generate", AIGenerateNode),
        ("ai_gen_expand", AIGenExpandNode),  # C1
        ("ai_inpaint", AIInpaintNode),
        ("ai_expand", AIExpandNode),
        ("ai_remove", AIRemoveNode),
        ("ai_denoise", AIDenoiseNode),
        ("face_track", FaceTrackNode),  # C3
        ("face_smooth", FaceSmoothNode),  # C3
    ],
    "Group": [
        ("group", GroupNode),
        ("precomp", PrecompNode),
    ],
    "3D": [
        ("scene3d", Scene3DNode),
        ("camera3d", Camera3DNode),
        ("light3d", Light3DNode),
        ("geo3d", Geo3DNode),
        ("model3d", Model3DNode),
        ("deformer3d", Deformer3DNode),
        ("pointcloud3d", PointCloud3DNode),  # B2
        ("volume3d", Volume3DNode),  # B1
    ],
}


# 节点类型是否启用实例序号命名（display_title 末尾拼数字，如 Camera1/Light2）。
# None/未列入 → 不编号（保持原 node_title 显示）。其余多实例 3D 源节点也列入。
_INSTANCE_NUMBERED_TYPES = frozenset({"camera3d", "light3d"})


def assign_instance_index(node: "Node", graph: "NodeGraph") -> None:
    """为新加入 ``graph`` 的 ``node`` 分配实例序号（写入 ``node.instance_label``）。

    命名升级（P1）：让同类节点在画布上显示带序号的标题（Camera1/Camera2…），
    便于区分多个摄影机/灯光。

    算法（"只增不减"策略）：
        - 扫描图中同 ``node_type`` 已存在节点的 ``instance_label``，取最大整数 +1。
        - 找不到任何整数标签时（如全 None）从 1 开始。
        - 删除节点不重排剩余序号，避免 undo/redo 下编号反复跳变；新建节点永远递增。

    Args:
        node: 刚创建、即将/已加入图的节点。
        graph: 目标图（用于查同类节点）。
    """
    if node.node_type not in _INSTANCE_NUMBERED_TYPES:
        return
    max_index = 0
    for existing in graph.get_nodes_by_type(node.node_type):
        if existing is node:
            continue
        label = existing.instance_label
        if label is None:
            continue
        try:
            idx = int(label)
        except (TypeError, ValueError):
            continue
        if idx > max_index:
            max_index = idx
    node.instance_label = str(max_index + 1)


class NodeGraph:
    """Directed acyclic graph of composite nodes."""

    # LRU cache size limit - prevents unbounded memory growth
    # A typical composite has 20-50 nodes; 100 provides headroom
    EVALUATION_CACHE_MAX_SIZE: int = 100

    def __init__(self) -> None:
        self.nodes: Dict[str, Node] = {}  # node_id -> Node
        self.connections: List[Connection] = []
        self.backdrops: Dict[str, dict] = {}  # backdrop_id -> UI metadata dict
        self._sticky_notes: Dict[str, dict] = {}  # sticky_note_id -> UI metadata dict
        self._node_groups: Dict[str, dict] = {}  # group_id -> UI group metadata
        # ── AE-style layer stacks (plan §3) ──
        # stack_id -> LayerStack dataclass.  MVP keeps a single main stack but
        # the dict container avoids a future schema bump for precomps/groups.
        # Kept on the graph (not on a parallel doc) so undo/snapshot/eval all
        # see one consistent object; layer state itself never affects eval.
        self._layer_stacks: Dict[str, Any] = {}  # stack_id -> LayerStack
        # Flat layer index for the stacks above; rebuilt when stacks mutate.
        # Stored as a dict {layer_id: Layer} for O(1) lookup by the compiler.
        self._layers: Dict[str, Any] = {}  # layer_id -> Layer
        self._viewport_state: Dict[str, Any] = {}  # node_editor viewport state (zoom, center)
        self._viewer_state: Dict[str, Any] = {}  # composite viewer state (viewing_node_id, etc.)
        # Project-level timing metadata.  Timeline-backed composites refresh
        # these values from their source clip at runtime; standalone comps
        # persist them with the graph and use them for playback/keyframes.
        self.fps: float = 24.0
        self.duration_frames: int = 0
        # LRU cache: OrderedDict maintains access order for eviction
        self._evaluation_cache: OrderedDict[str, Dict[str, TextureResult]] = OrderedDict()
        self._force_full_eval = True  # First eval always full
        self._lock = threading.RLock()  # Thread safety for graph reads/writes
        self._generation: int = 0  # Incremented on every mutation
        self._topology_revision: int = 0  # Incremented on connect/disconnect

        # ── Expression System ──
        # Expression manager for dependency tracking and propagation
        from src.features.composite.utils.expression_parser import ExpressionManager
        self._expression_manager = ExpressionManager()

    # ── Node management ──

    def add_node(self, node: Node) -> str:
        """Add a node to the graph. Returns the node ID.

        If the node is a GroupNode, sets its _node_graph reference
        so it can access member nodes during evaluation.
        """
        with self._lock:
            self.nodes[node.id] = node
            # Set graph reference for GroupNode to enable subgraph evaluation
            if isinstance(node, GroupNode):
                node.set_node_graph(self)
            # Nodes can be assembled by importers/tests before being attached
            # to a graph.  Rehydrate their expression edges here so later
            # source edits invalidate dependents just like expressions added
            # through ``set_node_expression``.
            for param_name, expression in getattr(
                    node, "_expressions", {}).items():
                try:
                    local_param_names = {
                        str(name) for name in node.get_param_defs().keys()}
                except Exception:
                    local_param_names = None
                self._expression_manager.set_expression(
                    node.id, param_name, expression,
                    local_param_names=local_param_names,
                    resolve_node_ref=self._resolve_expression_node_ref)
            self._force_full_eval = True
            self._generation += 1
            return node.id

    def get_nodes_by_type(self, node_type: str) -> List[Node]:
        """Return all nodes matching ``node_type`` (insertion order preserved).

        命名升级（P1）用：计算同类节点实例序号时遍历。Python 3.7+ dict 保持
        插入顺序，故返回顺序反映创建/加载顺序。
        """
        with self._lock:
            return [n for n in self.nodes.values() if n.node_type == node_type]

    def _detach_node_references_unlocked(self, node_id: str,
                                          node: Node) -> None:
        """Shared cleanup when a node leaves the graph (AUD-5100).

        ``remove_node`` and ``remove_node_and_reconnect`` both funnel
        through here so the reconnection path cannot skip the
        expression-manager teardown, pick-whip ``parameter_bindings`` on
        *other* nodes pointing at the removed one, dangling children's
        ``parent_id``, and the GroupNode back-reference.

        Caller must hold ``self._lock`` and have already removed the node
        from ``self.nodes``.
        """
        self._expression_manager.remove_node(node_id)
        for other in self.nodes.values():
            if getattr(other, "parent_id", None) == node_id:
                other.parent_id = None
            for pname, binding in list(
                    getattr(other, "parameter_bindings", {}).items()):
                if binding.get("node_id") == node_id:
                    other.parameter_bindings.pop(pname, None)
        # Clear graph reference if it's a GroupNode
        if isinstance(node, GroupNode):
            node._node_graph = None

    def remove_node(self, node_id: str) -> Optional[Node]:
        """Remove a node and all its connections.

        Note: GPU cleanup is NOT called here. The caller must schedule
        GPU resource release through the render thread to avoid
        releasing resources on the wrong thread.
        """
        with self._lock:
            node = self.nodes.pop(node_id, None)
            if node:
                # Remove all connections involving this node
                self.connections = [
                    c for c in self.connections
                    if c.src_node_id != node_id and c.dst_node_id != node_id
                ]
                self._detach_node_references_unlocked(node_id, node)
                self._force_full_eval = True
                self._generation += 1
                self._topology_revision += 1
            return node

    def remove_node_and_reconnect(self, node_id: str) -> bool:
        """Remove a node and auto-reconnect its upstream to downstream.

        Only works for nodes with exactly one image input and one or more
        image outputs. For complex nodes (multiple inputs/outputs), falls
        back to simple removal without reconnection.

        This is the primary deletion method for processing nodes like
        Topos Color, Grade, Blur, etc.

        Args:
            node_id: ID of the node to remove

        Returns:
            True if the node was removed (with or without reconnection)
        """
        with self._lock:
            node = self.nodes.get(node_id)
            if node is None:
                return False

            # Find image input connections (what feeds into this node)
            incoming = [c for c in self.connections if c.dst_node_id == node_id]
            # Find image output connections (what this node feeds into)
            outgoing = [c for c in self.connections if c.src_node_id == node_id]

            # Only auto-reconnect for single-input/single-output image nodes
            image_inputs = [c for c in incoming if any(
                p.name == c.dst_port_name and p.data_type == "image"
                for p in node.inputs
            )]
            image_outputs = [c for c in outgoing if any(
                p.name == c.src_port_name and p.data_type == "image"
                for p in node.outputs
            )]

            # Perform reconnection if exactly one image input and one or more
            # image outputs
            if len(image_inputs) == 1 and len(image_outputs) >= 1:
                src_conn = image_inputs[0]
                for dst_conn in image_outputs:
                    new_conn = Connection(
                        src_node_id=src_conn.src_node_id,
                        src_port_name=src_conn.src_port_name,
                        dst_node_id=dst_conn.dst_node_id,
                        dst_port_name=dst_conn.dst_port_name,
                    )
                    # Basic validation: both endpoints must exist and be
                    # type-compatible
                    src_node = self.nodes.get(new_conn.src_node_id)
                    dst_node = self.nodes.get(new_conn.dst_node_id)
                    if src_node and dst_node:
                        src_port = src_node.get_output_port(new_conn.src_port_name)
                        dst_port = dst_node.get_input_port(new_conn.dst_port_name)
                        if (src_port and dst_port
                                and src_port.data_type == dst_port.data_type):
                            self.connections.append(new_conn)

            # Mark downstream nodes as dirty BEFORE stripping the removed
            # node's connections (AUD-5101): _mark_dirty_downstream walks
            # the edges leaving ``node_id`` — calling it after the filter
            # below was a guaranteed no-op, leaving reconnected consumers
            # clean for any incremental evaluator that trusts _dirty.
            self._mark_dirty_downstream(node_id)

            # Remove all connections involving this node
            self.connections = [
                c for c in self.connections
                if c.src_node_id != node_id and c.dst_node_id != node_id
            ]

            # Remove the node
            del self.nodes[node_id]

            # AUD-5100: expression/binding/parent cleanup shared with
            # remove_node — the reconnection path used to skip it, leaving
            # dangling expression edges, pick-whip bindings pointing at the
            # deleted node, and orphaned children.
            self._detach_node_references_unlocked(node_id, node)

            self._force_full_eval = True
            self._generation += 1
            self._topology_revision += 1

            return True

    def get_node(self, node_id: str) -> Optional[Node]:
        with self._lock:
            return self.nodes.get(node_id)

    def clear(self) -> None:
        """Clear all nodes, connections, and metadata from the graph."""
        with self._lock:
            self.nodes.clear()
            self.connections.clear()
            self.backdrops.clear()
            self._sticky_notes.clear()
            self._node_groups.clear()
            self._layer_stacks.clear()
            self._layers.clear()
            self._viewport_state.clear()
            self._viewer_state.clear()
            self._evaluation_cache.clear()
            self._expression_manager.clear()
            self._force_full_eval = True
            self._generation += 1
            self._topology_revision += 1

    def set_node_parameter(self, node_id: str, key: str, value) -> bool:
        """Thread-safe parameter setting. Holds the graph lock while modifying node state."""
        with self._lock:
            node = self.nodes.get(node_id)
            if node:
                old_value = node.parameters.get(key)
                node.set_parameter(key, value)
                if old_value != node.parameters.get(key):
                    # A parameter may be a source for expressions on nodes
                    # that are otherwise clean.  Mark the full transitive
                    # dependent set dirty so an incremental render cannot
                    # reuse textures computed from the old value.
                    self._propagate_expression_change(node_id, key)
                self._generation += 1
                return True
            return False

    # ── AE-style parameter / parent bindings ──

    def get_parameter_binding(self, node_id: str,
                              param_name: str) -> Optional[Dict[str, str]]:
        """Return the pick-whip source for a parameter, if one is set."""
        with self._lock:
            node = self.nodes.get(node_id)
            binding = getattr(node, "parameter_bindings", {}).get(param_name)
            return copy.deepcopy(binding) if binding else None

    def set_parameter_binding(self, node_id: str, param_name: str,
                              source_node_id: str,
                              source_param_name: str) -> Tuple[bool, Optional[str]]:
        """Bind one numeric parameter to another numeric parameter.

        The persisted binding is accompanied by a generated expression such as
        ``n_a1b2c3d4.radius``.  This reuses the already-tested expression manager
        for dependency ordering and circular-dependency rejection while the
        metadata keeps the UI and scripting API explicit.
        """
        with self._lock:
            target = self.nodes.get(node_id)
            source = self.nodes.get(source_node_id)
            if target is None or source is None:
                return False, "Source or target node not found"
            if node_id == source_node_id:
                return False, "A parameter cannot bind to itself"
            if not self._is_numeric_parameter(target, param_name):
                return False, f"Target parameter '{param_name}' is not numeric"
            if not self._is_numeric_parameter(source, source_param_name):
                return False, f"Source parameter '{source_param_name}' is not numeric"
            # A pick-whip adds an edge target -> source.  Use a prefixed
            # expression alias because UUID-like node IDs may begin with a
            # digit, which is not a legal Python/AE identifier.
            if self._binding_reaches(
                    (source_node_id, source_param_name),
                    (node_id, param_name)):
                return False, "Circular parameter binding detected"

            expression = f"{self._expression_alias(source_node_id)}.{source_param_name}"
            # The graph lock is an RLock, so calling the existing method keeps
            # validation and dependency-manager bookkeeping in one place.
            success, error = self.set_node_expression(
                node_id, param_name, expression)
            if not success:
                return False, error
            target.parameter_bindings[param_name] = {
                "node_id": source_node_id,
                "param_name": source_param_name,
            }
            self._generation += 1
            return True, None

    def remove_parameter_binding(self, node_id: str,
                                 param_name: str) -> bool:
        """Remove a pick-whip binding without deleting an unrelated expression."""
        with self._lock:
            node = self.nodes.get(node_id)
            if node is None or param_name not in getattr(
                    node, "parameter_bindings", {}):
                return False
            binding = node.parameter_bindings.pop(param_name)
            generated = f"{self._expression_alias(binding.get('node_id', ''))}."
            generated += str(binding.get("param_name", ""))
            if node.get_expression(param_name) == generated:
                self.remove_node_expression(node_id, param_name)
            self._generation += 1
            return True

    def set_node_parent(self, child_node_id: str,
                        parent_node_id: Optional[str]) -> Tuple[bool, Optional[str]]:
        """Set or clear a node parent, rejecting self/cyclic hierarchies."""
        with self._lock:
            child = self.nodes.get(child_node_id)
            if child is None:
                return False, "Child node not found"
            if parent_node_id in (None, ""):
                child.parent_id = None
                child._dirty = True
                child.param_revision += 1
                child.bump_output_revision()
                self._generation += 1
                return True, None
            if parent_node_id == child_node_id or parent_node_id not in self.nodes:
                return False, "Invalid parent node"
            visited = {child_node_id}
            current = parent_node_id
            while current:
                if current in visited:
                    return False, "Parent cycle detected"
                visited.add(current)
                current_node = self.nodes.get(current)
                current = getattr(current_node, "parent_id", None) \
                    if current_node is not None else None
            child.parent_id = parent_node_id
            child._dirty = True
            child.param_revision += 1
            child.bump_output_revision()
            self._generation += 1
            return True, None

    def get_node_parent(self, node_id: str) -> Optional[str]:
        with self._lock:
            node = self.nodes.get(node_id)
            return getattr(node, "parent_id", None) if node else None

    @staticmethod
    def _expression_alias(value: str) -> str:
        return f"n_{value}"

    @staticmethod
    def _expression_identifier(value: str) -> bool:
        return bool(value) and (value[0].isalpha() or value[0] == "_") \
            and all(ch.isalnum() or ch == "_" for ch in value)

    @staticmethod
    def _is_numeric_parameter(node: Node, param_name: str) -> bool:
        pdef = node.get_param_defs().get(param_name)
        if pdef is not None:
            return pdef.is_expression_compatible()
        value = node.parameters.get(param_name)
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    def _parameter_dependency_reaches(
            self, start: Tuple[str, str], target: Tuple[str, str]) -> bool:
        """Return whether existing expression edges reach ``target``."""
        manager = self._expression_manager
        pending = [start]
        visited = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(manager._dependencies.get(current, set()))
        return False

    def _binding_reaches(self, start: Tuple[str, str],
                         target: Tuple[str, str]) -> bool:
        """Follow explicit bindings, then existing expression edges."""
        current = start
        visited = set()
        while current not in visited:
            if current == target:
                return True
            visited.add(current)
            node = self.nodes.get(current[0])
            binding = getattr(node, "parameter_bindings", {}).get(current[1]) \
                if node is not None else None
            if not binding:
                break
            current = (binding.get("node_id", ""),
                       binding.get("param_name", ""))
        return self._parameter_dependency_reaches(
            (self._expression_alias(start[0]), start[1]), target)

    def set_node_disabled(self, node_id: str, disabled: bool) -> bool:
        """Thread-safe disable/bypass toggle. Holds the graph lock while modifying node state."""
        with self._lock:
            node = self.nodes.get(node_id)
            if node:
                node.disabled = disabled
                node._dirty = True
                self._generation += 1
                return True
            return False

    # ── Expression Management ──

    def _expression_would_cycle(self, node_id: str, param_name: str) -> bool:
        """Real circular-dependency detection over expression edges (AUD-R2-013).

        ``ExpressionManager.detect_circular_dependency``
        (utils/expression_parser.py) marks the start key visited before it can
        ever be revisited, so its DFS detects NO cycle at all — not even a
        self-reference. The engine is that detector's only consumer, so this
        local walk restores genuine detection: follow dependency edges from the
        key that was just (re)registered; reaching the key again through at
        least one edge means the new expression closes a cycle.

        Runs under the graph lock (callers hold ``self._lock``); each
        ``get_dependencies`` read takes the expression manager's own lock
        briefly, with no reverse lock ordering (the manager never calls back
        into the graph).
        """
        start: Tuple[str, str] = (node_id, param_name)
        visited: Set[Tuple[str, str]] = set()
        stack: List[Tuple[str, str]] = [start]
        while stack:
            current = stack.pop()
            if current in visited:
                if current == start:
                    # Reached the start again through >= 1 dependency edge.
                    return True
                continue
            visited.add(current)
            for dep in self._expression_manager.get_dependencies(
                    current[0], current[1]):
                stack.append(dep)
        return False

    def _resolve_expression_node_ref(self, ref: str) -> Optional[str]:
        """N-03：表达式节点引用 canonical 化——ID / ``n_<id>`` 别名 / 可用
        显示名统一映射到真实 node ID。

        使提交校验（_expression_would_cycle 经 manager 依赖图）、快照
        求值与 preflight 共享同一依赖图；UUID 以数字开头导致的 ``n_``
        前缀别名不再绕过环检测。必须在持有 self._lock 时调用。
        """
        if ref in self.nodes:
            return ref
        if ref.startswith("n_") and ref[2:] in self.nodes:
            return ref[2:]
        for node_id, node in self.nodes.items():
            if getattr(node, "display_title", None) == ref:
                return node_id
        return None

    def set_node_expression(self, node_id: str, param_name: str,
                            expression: str) -> Tuple[bool, Optional[str]]:
        """Set an expression for a node parameter.

        Validates the expression and updates dependency tracking.
        Returns (success, error_message).

        Args:
            node_id: Node ID.
            param_name: Parameter name.
            expression: Expression string (e.g., "this.width * 0.5").

        Returns:
            Tuple of (success, error_message). error_message is None on success.
        """
        with self._lock:
            node = self.nodes.get(node_id)
            if not node:
                return (False, f"Node '{node_id}' not found")

            # N-01：捕获提交前状态。set_expression 会按 A-15 互斥政策
            # 删除同参数关键帧轨道；环检测失败时旧实现只移除新表达式，
            # 被删的轨道/旧表达式就此永久丢失且不可撤销。
            prev_expression = node.get_expression(param_name)
            prev_enabled = node.is_expression_enabled(param_name)
            prev_track = copy.deepcopy(node.keyframe_tracks.get(param_name))

            # Set expression on node
            success = node.set_expression(param_name, expression)
            if not success:
                from src.features.composite.utils.expression_parser import ExpressionParser
                parser = ExpressionParser()
                result = parser.validate(expression)
                error_msg = result.error.message if result.error else "Invalid expression"
                return (False, error_msg)

            # Update expression manager for dependency tracking.
            # N-02/N-03：本地 this.foo 引用注册为同节点图边，节点引用
            # （ID/n_别名/显示名）canonical 化——三者此前不入图/保留别名，
            # 提交校验绕过本地环与跨别名环，求值顺序依赖插入顺序。
            try:
                local_param_names = {
                    str(name) for name in node.get_param_defs().keys()}
            except Exception:
                local_param_names = None
            self._expression_manager.set_expression(
                node_id, param_name, expression,
                local_param_names=local_param_names,
                resolve_node_ref=self._resolve_expression_node_ref)

            # Check for circular dependencies (AUD-R2-013: the manager's own
            # detect_circular_dependency is a dead DFS that never reports any
            # cycle; _expression_would_cycle performs the real graph walk).
            if self._expression_would_cycle(node_id, param_name):
                # N-01：完整回滚到提交前状态，而不是停留在“新表达式已
                # 删、旧表达式与轨道也丢了”的中间态。直接写私有容器避免
                # 再触发 set_expression 的轨道删除副作用。
                if prev_expression is not None:
                    node._expressions[param_name] = prev_expression
                    node._expression_enabled[param_name] = prev_enabled
                    self._expression_manager.set_expression(
                        node_id, param_name, prev_expression)
                else:
                    node.remove_expression(param_name)
                    self._expression_manager.remove_expression(node_id, param_name)
                if prev_track is not None:
                    node.keyframe_tracks[param_name] = copy.deepcopy(prev_track)
                node._dirty = True
                node.param_revision += 1
                node.bump_output_revision()
                return (False, "Circular dependency detected")

            # Propagate changes to dependent nodes
            self._propagate_expression_change(node_id, param_name)

            self._generation += 1
            return (True, None)

    def remove_node_expression(self, node_id: str, param_name: str) -> bool:
        """Remove expression from a node parameter."""
        with self._lock:
            node = self.nodes.get(node_id)
            if not node:
                return False
            if not node.has_expression(param_name):
                return False
            node.remove_expression(param_name)
            self._expression_manager.remove_expression(node_id, param_name)
            self._generation += 1
            return True

    def set_node_expression_enabled(self, node_id: str, param_name: str,
                                    enabled: bool) -> Tuple[bool, Optional[str]]:
        """Toggle an expression while preserving its source text."""
        with self._lock:
            node = self.nodes.get(node_id)
            if node is None:
                return False, f"Node '{node_id}' not found"
            if not node.has_expression(param_name):
                return False, f"Parameter '{param_name}' has no expression"
            if not node.set_expression_enabled(param_name, enabled):
                return False, f"Failed to update expression state for {node_id}.{param_name}"
            self._propagate_expression_change(node_id, param_name)
            self._generation += 1
            return True, None

    def get_node_expression(self, node_id: str, param_name: str) -> Optional[str]:
        """Get expression for a node parameter."""
        with self._lock:
            node = self.nodes.get(node_id)
            if node:
                return node.get_expression(param_name)
            return None

    def get_expression_dependencies(self, node_id: str) -> Dict[str, List[str]]:
        """Get all expression dependencies for a node."""
        with self._lock:
            node = self.nodes.get(node_id)
            if node:
                return node.get_expression_dependencies()
            return {}

    def _propagate_expression_change(self, node_id: str, param_name: str) -> None:
        """Propagate expression change to dependent nodes."""
        dependents = self._get_all_expression_dependents(node_id, param_name)
        for dep_node_id, dep_param_name in dependents:
            dep_node = self.nodes.get(dep_node_id)
            if dep_node:
                dep_node._dirty = True
                dep_node.param_revision += 1
                dep_node.bump_output_revision()

    def _get_all_expression_dependents(self, node_id: str, param_name: str) -> Set[Tuple[str, str]]:
        """Get all transitive dependents of a parameter through expressions."""
        key = (node_id, param_name)
        visited: Set[Tuple[str, str]] = set()
        stack: List[Tuple[str, str]] = [key]
        while stack:
            current = stack.pop()
            direct_dependents = self._expression_manager.get_dependents(*current)
            for dep in direct_dependents:
                if dep not in visited:
                    visited.add(dep)
                    stack.append(dep)
        return visited

    def resolve_node_expressions(self, node_id: str,
                                 input_attrs: Optional[Dict[str, int]] = None,
                                 frame: int = 0) -> Dict[str, Any]:
        """Resolve expressions for a single node."""
        with self._lock:
            node = self.nodes.get(node_id)
            if not node:
                return {}
            values, _, _ = self._evaluate_snapshot_parameters(
                frame,
                (input_attrs or {}).get("width", 1920),
                (input_attrs or {}).get("height", 1080),
                24.0,
            )
            return values.get(node_id, dict(node.parameters))

    def resolve_all_expressions(self, input_attrs: Optional[Dict[str, int]] = None,
                                frame: int = 0,
                                fps: float = 24.0) -> Dict[str, Dict[str, Any]]:
        """Resolve expressions for all nodes in dependency order."""
        with self._lock:
            attrs = input_attrs or {}
            resolved, _, _ = self._evaluate_snapshot_parameters(
                frame, attrs.get("width", 1920), attrs.get("height", 1080), fps)
            for nid, params in resolved.items():
                node = self.nodes.get(nid)
                if node:
                    for param_name, value in params.items():
                        if node.has_expression(param_name):
                            node.parameters[param_name] = value
            return resolved

    # ── UI metadata ──

    def set_backdrop(self, backdrop_id: str, data: Dict[str, Any]) -> None:
        """Store backdrop UI metadata without affecting graph evaluation."""
        with self._lock:
            backdrop_data = copy.deepcopy(data)
            backdrop_data["id"] = backdrop_id
            self.backdrops[backdrop_id] = backdrop_data

    def remove_backdrop(self, backdrop_id: str) -> None:
        """Remove backdrop UI metadata."""
        with self._lock:
            self.backdrops.pop(backdrop_id, None)

    # ── Sticky Note management ──

    def add_sticky_note(self, note_id: str, data: Dict[str, Any]) -> None:
        """Store sticky note UI metadata without affecting graph evaluation.

        Args:
            note_id: Unique identifier for the sticky note
            data: Dict containing note metadata (text, color, position, size, etc.)
        """
        with self._lock:
            note_data = copy.deepcopy(data)
            note_data["id"] = note_id
            self._sticky_notes[note_id] = note_data

    def remove_sticky_note(self, note_id: str) -> None:
        """Remove sticky note UI metadata.

        Args:
            note_id: The sticky note ID to remove
        """
        with self._lock:
            self._sticky_notes.pop(note_id, None)

    def get_sticky_note(self, note_id: str) -> Optional[Dict]:
        """Get sticky note data by ID.

        Args:
            note_id: The sticky note ID

        Returns:
            Sticky note data dict or None if not found
        """
        with self._lock:
            return self._sticky_notes.get(note_id)

    def get_sticky_notes(self) -> Dict[str, Dict]:
        """Get all sticky notes.

        Returns:
            Dict of note_id -> note_data
        """
        with self._lock:
            return dict(self._sticky_notes)

    def update_sticky_note(self, note_id: str, data: Dict[str, Any]) -> bool:
        """Update sticky note properties.

        Args:
            note_id: The sticky note ID
            data: New note metadata to merge

        Returns:
            True if note was updated successfully
        """
        with self._lock:
            if note_id not in self._sticky_notes:
                return False

            self._sticky_notes[note_id].update(data)
            return True

    # ── Layer stack management (AE/Nuke hybrid, plan §3) ──
    # LayerStack and Layer are pure state containers; they never participate
    # in evaluation.  The compiler (layer_stack_compiler.py) is the only code
    # that translates layer state into node connections, and it is invoked by
    # the controller, never by these accessors.

    def set_layer_stack(self, stack: Any) -> None:
        """Insert or replace a :class:`LayerStack` on the graph.

        Also registers every layer it references into the flat ``_layers``
        index so :meth:`get_layer` is O(1).
        """
        with self._lock:
            self._layer_stacks[stack.stack_id] = stack
            self._rebuild_layer_index()

    def remove_layer_stack(self, stack_id: str) -> None:
        """Remove a stack and drop orphaned layer index entries."""
        with self._lock:
            self._layer_stacks.pop(stack_id, None)
            self._rebuild_layer_index()

    def get_layer_stack(self, stack_id: str) -> Optional[Any]:
        with self._lock:
            return self._layer_stacks.get(stack_id)

    def get_layer_stacks(self) -> Dict[str, Any]:
        """Return a shallow copy of ``stack_id -> LayerStack``."""
        with self._lock:
            return dict(self._layer_stacks)

    def get_main_layer_stack(self) -> Optional[Any]:
        """Return the single main stack (MVP: first/only stack), or None.

        With multiple stacks this picks the one whose ``stack_id == "main"``
        if present, otherwise the first inserted.  Callers that need a
        specific stack must use :meth:`get_layer_stack`.
        """
        with self._lock:
            if not self._layer_stacks:
                return None
            return self._layer_stacks.get("main") or next(
                iter(self._layer_stacks.values()))

    def upsert_layer(self, stack_id: str, layer: Any) -> bool:
        """Add or replace a layer on ``stack_id``.

        Returns False if the stack does not exist.  Does NOT mutate layer
        order if the layer already exists; callers reposition via the
        controller.  Keeps the flat index in sync.
        """
        with self._lock:
            stack = self._layer_stacks.get(stack_id)
            if stack is None:
                return False
            if layer.layer_id not in stack.layer_ids:
                stack.layer_ids.append(layer.layer_id)
            self._layers[layer.layer_id] = layer
            return True

    def remove_layer(self, layer_id: str) -> Optional[Any]:
        """Detach a layer from any stack and return it (or None)."""
        with self._lock:
            removed = self._layers.pop(layer_id, None)
            if removed is not None:
                for stack in self._layer_stacks.values():
                    if layer_id in stack.layer_ids:
                        stack.layer_ids.remove(layer_id)
                        if layer_id in stack.generated_merge_node_ids:
                            stack.generated_merge_node_ids.remove(layer_id)
            return removed

    def get_layer(self, layer_id: str) -> Optional[Any]:
        with self._lock:
            return self._layers.get(layer_id)

    def get_all_layers(self) -> List[Any]:
        """Return all layers across all stacks, in stack-insertion order."""
        with self._lock:
            out = []
            for stack in self._layer_stacks.values():
                for lid in stack.layer_ids:
                    layer = self._layers.get(lid)
                    if layer is not None:
                        out.append(layer)
            return out

    def _rebuild_layer_index(self) -> None:
        """Drop orphaned index entries (layers no longer referenced by a stack).

        Layer objects are inserted into ``_layers`` by :meth:`upsert_layer`
        / :meth:`set_layer_stack` (via the controller).  This helper only
        prunes entries whose layer_id is not referenced by any stack, so a
        removed stack does not keep dead layers alive.
        """
        # Already holding ``_lock`` from the caller.
        referenced = set()
        for stack in self._layer_stacks.values():
            referenced.update(stack.layer_ids)
        for lid in list(self._layers.keys()):
            if lid not in referenced:
                self._layers.pop(lid, None)

    def _load_layer_state(self, layer_stacks_data: Any,
                          layers_data: Any) -> None:
        """Deserialize stacks + layers (called from :meth:`from_dict`).

        Both arguments are optional raw dicts; missing or malformed data is
        tolerated so older projects (saved before layer stacks existed)
        load as ``_layer_stacks == {}``.
        """
        from .layer_model import LayerStack, Layer
        self._layer_stacks.clear()
        self._layers.clear()
        if isinstance(layer_stacks_data, dict):
            for stack_id, stack_dict in layer_stacks_data.items():
                if not isinstance(stack_dict, dict):
                    continue
                try:
                    self._layer_stacks[stack_id] = LayerStack.from_dict(stack_dict)
                except (KeyError, TypeError):
                    continue
        if isinstance(layers_data, dict):
            for layer_id, layer_dict in layers_data.items():
                if not isinstance(layer_dict, dict):
                    continue
                try:
                    self._layers[layer_id] = Layer.from_dict(layer_dict)
                except (KeyError, TypeError):
                    continue

    # ── Connection management ──

    def connect(self, src_node_id: str, src_port_name: str,
                dst_node_id: str, dst_port_name: str) -> bool:
        """Connect an output port to an input port.

        Validates the connection fully (type compatibility, no cycles)
        before removing any existing connection to the input port.
        Returns False without modifying the graph if validation fails.
        """
        with self._lock:
            src_node = self.nodes.get(src_node_id)
            dst_node = self.nodes.get(dst_node_id)
            if not src_node or not dst_node:
                return False

            src_port = src_node.get_output_port(src_port_name)
            dst_port = dst_node.get_input_port(dst_port_name)
            if not src_port or not dst_port:
                return False

            # Check data type compatibility
            if src_port.data_type != dst_port.data_type:
                return False

            # Check for cycles BEFORE removing any existing connection
            if self._would_create_cycle(src_node_id, dst_node_id):
                return False

            # Only now is it safe to replace existing connection on the input port.
            # - max_connections == 1：单输入端口，满载时替换（旧连接已通过成环检测）。
            # - max_connections > 1：多输入端口，满载时**拒绝**新连接以保护已有连接，
            #   而不是清空全部（否则多输入聚合被破坏）。
            if dst_port.max_connections > 0:
                existing = sum(1 for c in self.connections
                              if c.dst_node_id == dst_node_id and c.dst_port_name == dst_port_name)
                if existing >= dst_port.max_connections:
                    if dst_port.max_connections == 1:
                        # Single-input: replace the existing connection
                        self.connections = [
                            c for c in self.connections
                            if not (c.dst_node_id == dst_node_id and c.dst_port_name == dst_port_name)
                        ]
                    else:
                        # Multi-input port at capacity: refuse without disturbing existing
                        return False

            conn = Connection(src_node_id, src_port_name, dst_node_id, dst_port_name)
            self.connections.append(conn)

            # Mark downstream nodes as dirty
            self._mark_dirty_downstream(dst_node_id)
            self._generation += 1
            self._topology_revision += 1

            return True

    def disconnect(self, src_node_id: str, src_port_name: str,
                   dst_node_id: str, dst_port_name: str) -> bool:
        """Disconnect two ports."""
        with self._lock:
            original_len = len(self.connections)
            self.connections = [
                c for c in self.connections
                if not (c.src_node_id == src_node_id and c.src_port_name == src_port_name
                        and c.dst_node_id == dst_node_id and c.dst_port_name == dst_port_name)
            ]
            disconnected = len(self.connections) < original_len
            if disconnected:
                self._mark_dirty_downstream(dst_node_id)
                self._generation += 1
                self._topology_revision += 1
            return disconnected

    def disconnect_by_dst(self, dst_node_id: str, dst_port_name: str) -> None:
        """Remove all connections to a specific input port."""
        with self._lock:
            self.connections = [
                c for c in self.connections
                if not (c.dst_node_id == dst_node_id and c.dst_port_name == dst_port_name)
            ]
            self._mark_dirty_downstream(dst_node_id)
            self._generation += 1
            self._topology_revision += 1

    def get_connections_for_node(self, node_id: str) -> List[Connection]:
        """Get all connections involving a node."""
        with self._lock:
            return [c for c in self.connections
                    if c.src_node_id == node_id or c.dst_node_id == node_id]

    def get_input_connections(self, node_id: str) -> List[Connection]:
        """Get all connections feeding into a node's input ports."""
        with self._lock:
            return [c for c in self.connections if c.dst_node_id == node_id]

    def get_output_connections(self, node_id: str) -> List[Connection]:
        """Get all connections from a node's output ports."""
        with self._lock:
            return [c for c in self.connections if c.src_node_id == node_id]

    # ── Evaluation ──

    def topological_sort(self) -> List[str]:
        """
        Return node IDs in evaluation order (dependencies first).
        Uses Kahn's algorithm.
        """
        with self._lock:
            in_degree: Dict[str, int] = {nid: 0 for nid in self.nodes}
            adjacency: Dict[str, List[str]] = {nid: [] for nid in self.nodes}

            for conn in self.connections:
                adjacency[conn.src_node_id].append(conn.dst_node_id)
                in_degree[conn.dst_node_id] += 1

            queue = deque(nid for nid, deg in in_degree.items() if deg == 0)
            result = []

            # AUD-5110: deque.popleft() — a list-front pop is O(n) per pop,
            # making Kahn's algorithm O(n²) on wide graphs.
            while queue:
                node_id = queue.popleft()
                result.append(node_id)
                for neighbor in adjacency[node_id]:
                    in_degree[neighbor] -= 1
                    if in_degree[neighbor] == 0:
                        queue.append(neighbor)

            if len(result) != len(self.nodes):
                raise ValueError("Graph contains a cycle")

            return result

    def evaluate(self, ctx, fb_manager) -> Optional[TextureResult]:
        """
        Evaluate the graph. Returns the output node's result.

        Supports incremental evaluation: only re-evaluates dirty nodes
        and their downstream dependents.

        Note: The CompositeRenderer uses its own _evaluate_with_effects()
        instead of this method, because it needs to process GPU effect
        markers inline during evaluation. This method is kept for
        non-GPU or test usage.

        Threading (AUD-5102): the node map, the incoming-edge index and
        the eval order are captured under ``self._lock`` up-front so a
        concurrent UI-thread add/remove cannot raise KeyError mid-loop.
        The per-node evaluation itself still runs outside the lock —
        holding it across ``node.evaluate`` (GPU work) would starve every
        graph accessor.  Callers must still confine calls to a single
        thread; node ``_dirty`` flags and the evaluation cache remain
        owned by the evaluating thread.
        """
        with self._lock:
            eval_order = self.topological_sort()
            # Snapshot the structures the loop indexes so graph mutations
            # on another thread cannot tear the iteration (AUD-5102).
            nodes_snapshot: Dict[str, Node] = dict(self.nodes)
            incoming_index: Dict[str, List[Connection]] = {}
            for conn in self.connections:
                incoming_index.setdefault(conn.dst_node_id, []).append(conn)
            if self._force_full_eval:
                # Full evaluation - clear cache
                self._evaluation_cache.clear()
                self._force_full_eval = False

        output_result = None

        for node_id in eval_order:
            node = nodes_snapshot.get(node_id)
            if node is None:
                continue

            # Skip if not dirty and has cached results
            if not node._dirty and node_id in self._evaluation_cache:
                # LRU: move to end (mark as recently used)
                self._evaluation_cache.move_to_end(node_id)
                cached = self._evaluation_cache[node_id]
                if isinstance(node, OutputNode):
                    output_result = cached.get("display")
                continue

            # Gather inputs from upstream nodes
            input_textures: Dict[str, TextureResult] = {}
            for conn in incoming_index.get(node_id, ()):
                src_node = nodes_snapshot.get(conn.src_node_id)
                if src_node and src_node.id in self._evaluation_cache:
                    src_outputs = self._evaluation_cache[src_node.id]
                    if conn.src_port_name in src_outputs:
                        input_textures[conn.dst_port_name] = src_outputs[conn.src_port_name]

            # Evaluate the node
            try:
                outputs = node.evaluate(input_textures, ctx, fb_manager)
                # LRU: add to cache and enforce size limit
                self._evaluation_cache[node_id] = outputs
                # Evict oldest entries if cache exceeds limit
                while len(self._evaluation_cache) > self.EVALUATION_CACHE_MAX_SIZE:
                    self._evaluation_cache.popitem(last=False)  # Remove oldest
                node._dirty = False

                if isinstance(node, OutputNode):
                    output_result = outputs.get("display")
            except Exception as e:
                logger.warning(f"Error evaluating node {node_id} ({node.node_type}): {e}")
                self._evaluation_cache[node_id] = {}
                # Enforce cache size limit after error entry too
                while len(self._evaluation_cache) > self.EVALUATION_CACHE_MAX_SIZE:
                    self._evaluation_cache.popitem(last=False)

        return output_result

    def has_connected_output(self) -> bool:
        """Check if the Output node has a connected input."""
        with self._lock:
            for nid, node in self.nodes.items():
                if isinstance(node, OutputNode):
                    return len(self.get_input_connections(nid)) > 0
        return False

    def validate(self) -> List[str]:
        """Return a list of validation errors.

        AUD-5103: reads the node map under ``self._lock`` so a concurrent
        mutation cannot tear the iteration (topological_sort /
        get_input_connections re-enter the same RLock).
        """
        errors: List[str] = []
        with self._lock:
            try:
                self.topological_sort()
            except ValueError as e:
                errors.append(str(e))

            for nid, node in self.nodes.items():
                if isinstance(node, OutputNode):
                    conns = self.get_input_connections(nid)
                    if not conns:
                        errors.append(f"Output node '{node.id}' has no input connected")

        return errors

    # ── Snapshot ──

    @property
    def generation(self) -> int:
        """Current generation counter — incremented on every graph mutation."""
        return self._generation

    def bump_generation(self) -> None:
        """Force the next snapshot's generation to change.

        Used when render-affecting state that is not graph topology
        changed (e.g. a layer's in_frame/out_frame frame gate, P0-05) so
        the renderer's snapshot-skip check does not reuse a stale frame.
        """
        with self._lock:
            self._generation += 1

    @staticmethod
    def _build_snapshot_track_meta(node) -> tuple:
        """N-14：构建快照的 (keyframe_tracks, param_defs_meta) 序列化。

        从原 snapshot() 内联逻辑抽出，供 param_revision 缓存复用。
        """
        import copy as _copy
        keyframe_tracks: Dict[str, Dict[str, Any]] = {}
        param_defs_meta: Dict[str, Dict[str, Any]] = {}
        pdef_by_name = node.get_param_defs()
        for pdef in pdef_by_name.values():
            param_defs_meta[pdef.name] = {
                "ptype": pdef.ptype,
                "min_val": pdef.min_val,
                "max_val": pdef.max_val,
                "animated": pdef.animated,
            }
        for pname, track_data in node.keyframe_tracks.items():
            if not isinstance(track_data, dict):
                continue
            kfs = track_data.get("keyframes")
            if not kfs:
                continue
            pdef = pdef_by_name.get(pname)
            keyframe_tracks[pname] = {
                "param_name": pname,
                "value_type": (
                    pdef.ptype if pdef and pdef.ptype in {
                        "color", "vec2", "vec3", "bool", "option"
                    } else track_data.get("value_type", "scalar")),
                "keyframes": _copy.deepcopy(kfs),
                "min_val": track_data.get("min_val",
                                          pdef.min_val if pdef else 0.0),
                "max_val": track_data.get("max_val",
                                          pdef.max_val if pdef else 1.0),
                "pre_extrap": track_data.get("pre_extrap", "constant"),
                "post_extrap": track_data.get("post_extrap", "constant"),
            }
            if pname in param_defs_meta:
                param_defs_meta[pname]["animated"] = True
        return keyframe_tracks, param_defs_meta

    def _expression_alias_targets(self, expressions: Dict[str, Dict[str, str]]
                                   ) -> set:
        """N-14：表达式文本实际以 ``n_<id>`` 别名引用的节点集合。

        别名键在 ``evaluate_all`` 里缺席时只是跳过对应 alias 注册项
        （真实键结果不受影响），因此按需构建是安全的。
        """
        texts = [str(text)
                 for node_exprs in expressions.values()
                 for text in node_exprs.values()]
        if not texts:
            return set()
        targets: set = set()
        for node_id in self.nodes:
            alias = self._expression_alias(node_id)
            if any(alias in text for text in texts):
                targets.add(node_id)
        return targets

    def _evaluate_snapshot_parameters(self, frame_number: int,
                                      project_width: int,
                                      project_height: int,
                                      fps: float) -> Tuple[
                                          Dict[str, Dict[str, Any]],
                                          Dict[str, Dict[str, str]],
                                          Dict[str, Dict[str, str]]]:
        """Resolve keyframes and expressions without mutating live nodes."""
        from src.features.composite.utils.expression_parser import ExpressionManager
        from src.shared.keyframe import KeyframeTrack, keyframe_track_from_dict

        raw_params: Dict[str, Dict[str, Any]] = {}
        expressions: Dict[str, Dict[str, str]] = {}
        expression_errors: Dict[str, Dict[str, str]] = {}
        layer_attrs: Dict[str, Dict[str, Any]] = {}

        # Build a detached layer namespace for expressions.  A layer may
        # point at either a source node or the processed root node; both are
        # valid expression targets for the same row metadata.
        for stack in self._layer_stacks.values():
            for index, layer_id in enumerate(getattr(stack, "layer_ids", ())):
                layer = self._layers.get(layer_id)
                if layer is None:
                    continue
                in_point = (int(layer.in_frame)
                            if layer.in_frame is not None else 0)
                out_point = (int(layer.out_frame)
                             if layer.out_frame is not None
                             else max(0, int(self.duration_frames) - 1))
                attrs = {
                    "index": index,
                    "enabled": bool(layer.enabled),
                    "solo": bool(layer.solo),
                    "locked": bool(layer.locked),
                    "shy": bool(layer.shy),
                    "opacity": float(layer.opacity),
                    "inPoint": in_point,
                    "outPoint": out_point,
                    "in_frame": in_point,
                    "out_frame": out_point,
                }
                for candidate in (layer.root_node_id,
                                  layer.source_node_id):
                    if candidate:
                        layer_attrs[str(candidate)] = dict(attrs)

        for node_id, node in self.nodes.items():
            # N-14：参数浅拷贝（仅容器值深拷）——deepcopy 对标量也有逐对象
            # 开销，50 节点快照路径每帧×每参数都在付。
            params = {
                key: (copy.deepcopy(value)
                      if isinstance(value, (list, dict, set)) else value)
                for key, value in node.parameters.items()
            }
            # N-14：按 param_revision 缓存的已编译轨道——每帧只二分采样。
            for param_name, track in node.compiled_eval_tracks().items():
                try:
                    params[param_name] = track.get_value_float(
                        float(frame_number),
                        default=params.get(param_name, 0.0))
                except Exception:
                    continue
            raw_params[node_id] = params
            node_exprs = dict(getattr(node, "_expressions", {}))
            if node_exprs:
                expressions[node_id] = node_exprs

        # Generated pick-whip expressions use ``n_<node_id>`` aliases.  Keep
        # aliases in the evaluation context so UUIDs beginning with a digit
        # remain bindable without changing the serialized graph identity.
        # N-14：只为表达式文本实际引用的节点构建别名——旧实现给每个节点
        # deepcopy 一份完整参数，无表达式的图也在付这笔成本。
        if expressions:
            for node_id in self._expression_alias_targets(expressions):
                params = raw_params.get(node_id)
                if params is not None:
                    raw_params[self._expression_alias(node_id)] = (
                        copy.deepcopy(params))

        manager = ExpressionManager()
        for node_id, node_exprs in expressions.items():
            node = self.nodes.get(node_id)
            # N-02/N-03：快照重建与提交路径共享同一依赖契约（本地
            # this.foo 边 + 引用 canonical 化），求值顺序/环语义一致。
            try:
                local_param_names = (
                    {str(name) for name in node.get_param_defs().keys()}
                    if node is not None else None)
            except Exception:
                local_param_names = None
            for param_name, expression in node_exprs.items():
                if node is not None and not node.is_expression_enabled(param_name):
                    continue
                result = manager.set_expression(
                    node_id, param_name, expression,
                    local_param_names=local_param_names,
                    resolve_node_ref=self._resolve_expression_node_ref)
                if not result.success:
                    expression_errors.setdefault(node_id, {})[param_name] = (
                        result.error.message if result.error else "Invalid expression"
                    )
                alias_result = manager.set_expression(
                    self._expression_alias(node_id), param_name, expression,
                    local_param_names=local_param_names,
                    resolve_node_ref=self._resolve_expression_node_ref)
                if not alias_result.success:
                    # Alias errors are internal duplicates; the real node's
                    # error above is the one the UI should report.
                    continue

        def sample_value(node_id: str, param_name: str, sample_time: float) -> Any:
            if node_id.startswith("n_") and node_id[2:] in self.nodes:
                node_id = node_id[2:]
            node = self.nodes.get(node_id)
            if node is None:
                return 0.0
            value = node.parameters.get(param_name, 0.0)
            data = node.keyframe_tracks.get(param_name)
            if isinstance(data, dict) and data.get("keyframes"):
                try:
                    pdef = node.get_param_defs().get(param_name)
                    value_type = getattr(pdef, "ptype", "")
                    if value_type not in {"color", "vec2", "vec3", "bool", "option"}:
                        value_type = data.get("value_type", "scalar")
                    track = keyframe_track_from_dict({
                        "param_name": param_name,
                        "keyframes": data.get("keyframes", []),
                        "value_type": value_type,
                        "min_val": data.get("min_val", 0.0),
                        "max_val": data.get("max_val", 1.0),
                        "pre_extrap": data.get("pre_extrap", "constant"),
                        "post_extrap": data.get("post_extrap", "constant"),
                    })
                    return track.get_value_float(
                        float(sample_time) * float(fps or 24.0), default=value)
                except Exception as e:
                    logger.debug(f"Keyframe sampling failed for {node_id}.{param_name}, "
                                 f"using static value: {e}")
            return value

        def loop_value(node_id: str, param_name: str, mode: str,
                       count: int, current_frame: int, is_out: bool) -> Any:
            node = self.nodes.get(node_id)
            data = node.keyframe_tracks.get(param_name) if node else None
            if not isinstance(data, dict) or not data.get("keyframes"):
                return raw_params.get(node_id, {}).get(param_name, 0.0)
            try:
                track = KeyframeTrack.from_dict({
                    "param_name": param_name,
                    "keyframes": data.get("keyframes", []),
                    "min_val": data.get("min_val", 0.0),
                    "max_val": data.get("max_val", 1.0),
                    "pre_extrap": data.get("pre_extrap", "constant"),
                    "post_extrap": data.get("post_extrap", "constant"),
                })
                keys = track.keyframes
                if len(keys) < 2:
                    return keys[0].value if keys else 0.0
                count = max(0, min(int(count), len(keys)))
                active = keys[-count:] if is_out and count else keys[:count] if count else keys
                first, last = active[0], active[-1]
                # N-04：关键帧范围内的帧直接采样轨道——loopOut/loopIn
                # 只接管范围外推（AE 语义）。旧实现把范围内帧也走循环
                # 映射（offset=0 → mapped=first.frame），0→24 线性轨道在
                # 帧 12/24 全部错成首帧值 0。
                current = int(current_frame)
                if (current <= last.frame if is_out else current >= first.frame):
                    return track.get_value_float(float(current), default=0.0)
                span = max(1, last.frame - first.frame)
                offset = (max(0, int(current_frame) - last.frame) if is_out
                          else max(0, first.frame - int(current_frame)))
                mode = str(mode or "cycle").lower()
                if mode == "continue":
                    a, b = (active[-2], active[-1]) if is_out else (active[0], active[1])
                    slope = (b.value - a.value) / max(1, b.frame - a.frame)
                    return (last.value + slope * offset if is_out
                            else first.value - slope * offset)
                cycles, remainder = divmod(offset, span)
                if mode == "pingpong" and cycles % 2:
                    mapped = last.frame - remainder if is_out else first.frame + remainder
                else:
                    mapped = first.frame + remainder if is_out else last.frame - remainder
                base = track.get_value_float(float(mapped), default=0.0)
                if mode == "offset":
                    delta = last.value - first.value
                    return base + (delta * cycles if is_out else -delta * cycles)
                return base
            except Exception:
                return raw_params.get(node_id, {}).get(param_name, 0.0)

        resolved = manager.evaluate_all(
            raw_params,
            {"width": int(project_width or 1920), "height": int(project_height or 1080)},
            frame_number,
            fps=float(fps or 24.0),
            project_width=int(project_width or 1920),
            project_height=int(project_height or 1080),
            value_at_time=sample_value,
            loop_in=lambda nid, pname, mode, count, frame:
                loop_value(nid, pname, mode, count, frame, False),
            loop_out=lambda nid, pname, mode, count, frame:
                loop_value(nid, pname, mode, count, frame, True),
            layer_attrs=layer_attrs,
        )
        for (error_node_id, param_name), error in manager.last_errors.items():
            # Alias entries are an internal implementation detail used for
            # UUID-safe pick-whip references.  Report only the canonical node
            # entry so the same failure does not appear twice in the UI.
            canonical_id = (error_node_id[2:]
                            if error_node_id.startswith("n_")
                            and error_node_id[2:] in self.nodes
                            else error_node_id)
            if canonical_id in self.nodes and not error_node_id.startswith("n_"):
                expression_errors.setdefault(canonical_id, {})[param_name] = (
                    error.message or "Expression evaluation failed")
        # Never send NaN/Infinity to a GPU uniform. Keep the last safe base
        # value when an expression contains an invalid runtime operation such
        # as division by zero.
        for node_id, params in resolved.items():
            for param_name, value in list(params.items()):
                if isinstance(value, float) and not math.isfinite(value):
                    params[param_name] = raw_params.get(node_id, {}).get(
                        param_name, 0.0)

        # Apply the useful subset of AE parenting that maps naturally to this
        # node compositor: Transform nodes inherit a parent's transform when
        # both nodes expose the same transform channels.  Other node types
        # still retain the parent relationship for hierarchy/UI purposes but
        # are not forced to accept parameters they do not understand.
        world_cache: Dict[str, Dict[str, Any]] = {}

        def world_transform(node_id: str, visiting: Optional[set] = None):
            if node_id in world_cache:
                return world_cache[node_id]
            visiting = set() if visiting is None else set(visiting)
            if node_id in visiting:
                return dict(resolved.get(node_id, raw_params.get(node_id, {})))
            visiting.add(node_id)
            current = dict(resolved.get(node_id, raw_params.get(node_id, {})))
            node = self.nodes.get(node_id)
            parent_id = getattr(node, "parent_id", None) if node else None
            if parent_id in self.nodes:
                parent = world_transform(parent_id, visiting)
                if "translate_x" in current and "translate_x" in parent:
                    current["translate_x"] = float(current.get("translate_x", 0.0)) \
                        + float(parent.get("translate_x", 0.0))
                if "translate_y" in current and "translate_y" in parent:
                    current["translate_y"] = float(current.get("translate_y", 0.0)) \
                        + float(parent.get("translate_y", 0.0))
                if "rotation" in current and "rotation" in parent:
                    current["rotation"] = float(current.get("rotation", 0.0)) \
                        + float(parent.get("rotation", 0.0))
                if "scale_x" in current and "scale_x" in parent:
                    current["scale_x"] = float(current.get("scale_x", 1.0)) \
                        * float(parent.get("scale_x", 1.0))
                if "scale_y" in current and "scale_y" in parent:
                    current["scale_y"] = float(current.get("scale_y", 1.0)) \
                        * float(parent.get("scale_y", 1.0))
                # 3D layer roots use explicit object transform channels. Keep
                # the composition additive for translation/rotation and
                # multiplicative for uniform mesh scale, matching the 2D
                # parenting rules above without forcing 3D nodes to expose
                # 2D parameter names.
                for axis in ("x", "y", "z"):
                    pos_key = f"object_pos_{axis}"
                    if pos_key in current and pos_key in parent:
                        current[pos_key] = float(current.get(pos_key, 0.0)) \
                            + float(parent.get(pos_key, 0.0))
                    rot_key = f"object_rot_{axis}"
                    if rot_key in current and rot_key in parent:
                        current[rot_key] = float(current.get(rot_key, 0.0)) \
                            + float(parent.get(rot_key, 0.0))
                if "mesh_scale" in current and "mesh_scale" in parent:
                    current["mesh_scale"] = float(current.get("mesh_scale", 1.0)) \
                        * float(parent.get("mesh_scale", 1.0))
            world_cache[node_id] = current
            return current

        for node_id in self.nodes:
            if getattr(self.nodes[node_id], "parent_id", None):
                resolved[node_id] = world_transform(node_id)
        return resolved, expressions, expression_errors

    def get_expression_errors(self, frame_number: int = 0,
                              color_width: int = 0,
                              color_height: int = 0,
                              fps: float = 24.0
                              ) -> Dict[str, Dict[str, str]]:
        """Return per-node expression diagnostics for one evaluation frame.

        This is a read-only diagnostics API.  It evaluates the same immutable
        parameter preparation used by :meth:`snapshot` and does not mutate
        live node parameters.
        """
        with self._lock:
            _, _, errors = self._evaluate_snapshot_parameters(
                frame_number, color_width, color_height, fps)
            return copy.deepcopy(errors)

    def snapshot(self, color_texture_id: int = 0, color_width: int = 0,
                 color_height: int = 0, color_revision: int = 0,
                 frame_number: int = 0, fps: float = 24.0) -> GraphSnapshot:
        """Create an immutable snapshot of the current graph state.

        Must be called on the UI thread. The returned snapshot is safe to
        pass to the render thread for lock-free evaluation.

        Args:
            color_texture_id: COLOR node texture ID from ColorOutput.
            color_width: COLOR node texture width.
            color_height: COLOR node texture height.
            color_revision: COLOR node texture revision.
            frame_number: Current frame for animation parameter interpolation.
            fps: Composition frame rate used by ``time`` and ``valueAtTime``.
        """
        with self._lock:
            # N-06/N-07（2026-10-14 审计）：时间重映射先于参数求值。
            # 旧顺序先把全部节点按合成帧插值、事后才算 frame_overrides，
            # FrameHold 下游的上游动画参数永不冻结（帧 20 的 Transform
            # 参数取 20 而不是 hold 帧）。现在按每个节点被重映射后的
            # 有效帧分组求值；同一有效帧内表达式/父子世界变换照常一致。
            from types import SimpleNamespace as _TimeNS
            from .time_remap import compute_frame_overrides
            conn_snaps = [
                ConnectionSnapshot(
                    src_node_id=c.src_node_id,
                    src_port_name=c.src_port_name,
                    dst_node_id=c.dst_node_id,
                    dst_port_name=c.dst_port_name,
                )
                for c in self.connections
            ]
            try:
                pre_eval_order = self._snapshot_topological_sort(
                    list(self.nodes), conn_snaps)
            except ValueError:
                pre_eval_order = list(self.nodes.keys())
            time_conflicts: list = []
            pre_nodes = {
                nid: _TimeNS(
                    node_type=node.node_type,
                    parameters=dict(node.parameters),
                    disabled=node.disabled,
                    time_range=node.time_range,
                )
                for nid, node in self.nodes.items()
            }
            frame_overrides = compute_frame_overrides(
                pre_nodes,
                lambda nid: [c for c in conn_snaps if c.dst_node_id == nid],
                pre_eval_order,
                conflict_logger=time_conflicts.append,
                frame_number=frame_number,
            )
            if time_conflicts:
                # N-07 短期方案：共享源被多条时间链要求不同帧时不再静默
                # first-wins——快照携带可定位诊断（导出/preflight 可上报），
                # 正确的按分支求值见计划 B3 后续。
                logger.warning(
                    "[snapshot] time-branch conflicts: %s",
                    "; ".join(time_conflicts))

            effective_frame: Dict[str, int] = {}
            for nid in self.nodes:
                remap = frame_overrides.get(nid)
                try:
                    effective_frame[nid] = (
                        int(remap.apply(frame_number))
                        if remap is not None else int(frame_number))
                except Exception:
                    effective_frame[nid] = int(frame_number)
            evaluated_params: Dict[str, Dict[str, Any]] = {}
            snapshot_expressions: Dict[str, Dict[str, str]] = {}
            expression_errors: Dict[str, Dict[str, str]] = {}
            frame_params_cache: Dict[int, tuple] = {}
            for f in set(effective_frame.values()):
                frame_params_cache[f] = self._evaluate_snapshot_parameters(
                    f, color_width, color_height, fps)
            for nid in self.nodes:
                params_f, exprs_f, errors_f = frame_params_cache[
                    effective_frame[nid]]
                evaluated_params[nid] = params_f.get(nid, {})
                if nid in exprs_f:
                    snapshot_expressions[nid] = exprs_f[nid]
                if nid in errors_f:
                    expression_errors[nid] = errors_f[nid]
            # Capture node state
            node_snaps: Dict[str, NodeSnapshot] = {}
            for nid, node in self.nodes.items():
                # N-14：轨道/参数 meta 序列化按 param_revision 缓存。
                # 旧实现每帧 deepcopy 全部轨道（50 节点×120 键时是快照
                # 主要成本）；轨道写入路径（set/remove/ml_roto/N-01 回滚）
                # 都 bump revision，缓存键安全。缓存 dict 跨快照共享——
                # 快照消费方只读（frozen 契约）。
                _tracks_cache = getattr(node, "_snapshot_tracks_cache", None)
                if (_tracks_cache is not None
                        and _tracks_cache[0] == node.param_revision):
                    keyframe_tracks, param_defs_meta = (
                        _tracks_cache[1], _tracks_cache[2])
                else:
                    keyframe_tracks, param_defs_meta = (
                        self._build_snapshot_track_meta(node))
                    node._snapshot_tracks_cache = (
                        node.param_revision, keyframe_tracks, param_defs_meta)

                group_members = tuple(
                    str(member_id)
                    for member_id in getattr(node, "member_node_ids", ())
                )
                group_inputs = {
                    str(port): (str(mapping[0]), str(mapping[1]))
                    for port, mapping in getattr(
                        node, "input_mappings", {}).items()
                    if isinstance(mapping, (tuple, list)) and len(mapping) == 2
                }
                group_outputs = {
                    str(port): (str(mapping[0]), str(mapping[1]))
                    for port, mapping in getattr(
                        node, "output_mappings", {}).items()
                    if isinstance(mapping, (tuple, list)) and len(mapping) == 2
                }

                node_snaps[nid] = NodeSnapshot(
                    node_id=nid,
                    node_type=node.node_type,
                    parameters=copy.deepcopy(evaluated_params.get(nid, node.parameters)),
                    disabled=node.disabled,
                    input_ports=[(p.name, p.data_type) for p in node.inputs],
                    output_ports=[(p.name, p.data_type) for p in node.outputs],
                    state=node._state.name.lower(),
                    progress=node.progress,
                    error_message=node.error_message,
                    param_revision=node.param_revision,
                    # ── Port-level revision tracking ──
                    port_revisions=dict(node._port_revisions),
                    upstream_port_revisions=dict(node._upstream_port_revisions),
                    # ── Animation Support (P1) ──
                    keyframe_tracks=keyframe_tracks,
                    param_defs_meta=param_defs_meta,
                    expressions=copy.deepcopy(snapshot_expressions.get(nid, {})),
                    expression_errors=copy.deepcopy(expression_errors.get(nid, {})),
                    # ── Per-node time range + color (P1) ──
                    time_range=copy.deepcopy(node.time_range) if node.time_range else None,
                    node_color=node.effective_header_color(),
                    group_member_node_ids=group_members,
                    group_input_mappings=group_inputs,
                    group_output_mappings=group_outputs,
                )

            # Group nodes are macro boundaries rather than extra render steps.
            # Rewrite external edges through their declared ports so the
            # render thread evaluates the member graph with the same topology
            # the user sees at the group boundary. The original live graph is
            # never mutated; this only affects the immutable render snapshot.
            conn_snaps = self._flatten_group_boundaries(node_snaps, conn_snaps)

            # Compute topological order from the flattened snapshot topology.
            # (conn_snaps/overrides 已在函数头部计算——N-06 参数求值需要
            # 先行；此处基于 flatten 后的拓扑重排 eval_order。)
            try:
                eval_order = self._snapshot_topological_sort(
                    list(node_snaps), conn_snaps)
            except ValueError:
                eval_order = list(self.nodes.keys())

            # ── Time semantics (plan P0-04 / P0-05) ──
            # Layer in/out gates come straight from the graph's layer state;
            # time-node frame remaps were precomputed above (N-06) so both
            # parameter evaluation and frame-reading sources see the same
            # mapping.
            from .time_remap import compute_layer_frame_gates
            layer_frame_gates = compute_layer_frame_gates(
                self._layer_stacks.values(), self._layers.get)

            return GraphSnapshot(
                generation=self._generation,
                nodes=node_snaps,
                connections=conn_snaps,
                eval_order=eval_order,
                color_texture_id=color_texture_id,
                color_width=color_width,
                color_height=color_height,
                color_revision=color_revision,
                topology_revision=self._topology_revision,
                frame_number=frame_number,
                layer_frame_gates=layer_frame_gates,
                frame_overrides=frame_overrides,
                # N-07：共享源时间分支冲突诊断（不再静默 first-wins）
                time_conflicts=tuple(time_conflicts),
                # R-14：全局 3D 场景成品设置随快照进入所有渲染/导出路径
                global_3d_scene_settings=copy.deepcopy(
                    self._viewer_state.get("default_3d_scene", {})
                    if isinstance(self._viewer_state, dict) else {}),
            )

    @staticmethod
    def _resolve_group_port(
        node_id: str,
        port_name: str,
        nodes: Dict[str, NodeSnapshot],
        mapping_name: str,
    ) -> Tuple[str, str]:
        """Resolve nested GroupNode boundary ports to a concrete member port."""
        current_id = str(node_id)
        current_port = str(port_name)
        visited: Set[Tuple[str, str]] = set()
        while True:
            marker = (current_id, current_port)
            if marker in visited:
                # Preserve the original endpoint; preflight will report the
                # malformed recursive boundary instead of hiding it here.
                return str(node_id), str(port_name)
            visited.add(marker)
            node = nodes.get(current_id)
            if node is None or node.node_type != "group":
                return current_id, current_port
            mappings = getattr(node, mapping_name, {}) or {}
            target = mappings.get(current_port)
            if not isinstance(target, (tuple, list)) or len(target) != 2:
                return current_id, current_port
            current_id, current_port = str(target[0]), str(target[1])

    @classmethod
    def _flatten_group_boundaries(
        cls,
        nodes: Dict[str, NodeSnapshot],
        connections: List[ConnectionSnapshot],
    ) -> List[ConnectionSnapshot]:
        """Return snapshot edges with GroupNode boundaries flattened.

        Group members remain ordinary nodes in the same immutable snapshot,
        which preserves the existing evaluator and cache implementation while
        giving the group real input/output semantics. Nested group mappings
        are resolved transitively and duplicate edges are removed.
        """
        flattened: List[ConnectionSnapshot] = []
        seen: Set[Tuple[str, str, str, str]] = set()
        for connection in connections:
            src_id, src_port = cls._resolve_group_port(
                connection.src_node_id, connection.src_port_name,
                nodes, "group_output_mappings")
            dst_id, dst_port = cls._resolve_group_port(
                connection.dst_node_id, connection.dst_port_name,
                nodes, "group_input_mappings")
            key = (src_id, src_port, dst_id, dst_port)
            if key in seen:
                continue
            seen.add(key)
            flattened.append(ConnectionSnapshot(
                src_node_id=src_id,
                src_port_name=src_port,
                dst_node_id=dst_id,
                dst_port_name=dst_port,
            ))
        return flattened

    @staticmethod
    def _snapshot_topological_sort(
        node_ids: List[str],
        connections: List[ConnectionSnapshot],
    ) -> List[str]:
        """Topologically sort a snapshot connection list without live graph access."""
        in_degree: Dict[str, int] = {node_id: 0 for node_id in node_ids}
        adjacency: Dict[str, List[str]] = {node_id: [] for node_id in node_ids}
        for connection in connections:
            if (connection.src_node_id not in in_degree
                    or connection.dst_node_id not in in_degree):
                continue
            adjacency[connection.src_node_id].append(connection.dst_node_id)
            in_degree[connection.dst_node_id] += 1

        # AUD-5110: index connections once — the previous per-node full scan
        # of ``connections`` made the sort O(V·E).
        queue = deque(
            node_id for node_id in node_ids if in_degree[node_id] == 0)
        result: List[str] = []
        while queue:
            node_id = queue.popleft()
            result.append(node_id)
            for neighbor in adjacency[node_id]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
        if len(result) != len(node_ids):
            raise ValueError("Graph contains a cycle")
        return result

    # ── Group Management ──

    def create_group(self, group_id: str, node_ids: List[str], group_data: Dict[str, Any]) -> bool:
        """Create a UI-level group from selected nodes.
        
        Args:
            group_id: Unique identifier for the group
            node_ids: List of node IDs to include in the group
            group_data: Group metadata (title, color, position, etc.)
            
        Returns:
            True if group was created successfully
        """
        with self._lock:
            if group_id in self._node_groups:
                return False
            
            self._node_groups[group_id] = {
                'id': group_id,
                'node_ids': list(node_ids),
                **group_data
            }
            self._generation += 1
            return True
    
    def remove_group(self, group_id: str) -> bool:
        """Remove a group (nodes remain in the graph).
        
        Args:
            group_id: The group ID to remove
            
        Returns:
            True if group was removed successfully
        """
        with self._lock:
            if group_id not in self._node_groups:
                return False
            
            del self._node_groups[group_id]
            self._generation += 1
            return True
    
    def add_nodes_to_group(self, group_id: str, node_ids: List[str]) -> bool:
        """Add nodes to an existing group.
        
        Args:
            group_id: The group ID
            node_ids: Node IDs to add
            
        Returns:
            True if nodes were added successfully
        """
        with self._lock:
            if group_id not in self._node_groups:
                return False
            
            group = self._node_groups[group_id]
            for nid in node_ids:
                if nid not in group['node_ids']:
                    group['node_ids'].append(nid)
            
            self._generation += 1
            return True
    
    def remove_nodes_from_group(self, group_id: str, node_ids: List[str]) -> bool:
        """Remove nodes from a group.
        
        Args:
            group_id: The group ID
            node_ids: Node IDs to remove
            
        Returns:
            True if nodes were removed successfully
        """
        with self._lock:
            if group_id not in self._node_groups:
                return False
            
            group = self._node_groups[group_id]
            for nid in node_ids:
                if nid in group['node_ids']:
                    group['node_ids'].remove(nid)
            
            self._generation += 1
            return True
    
    def update_group(self, group_id: str, new_data: Dict[str, Any]) -> bool:
        """Update group properties.
        
        Args:
            group_id: The group ID
            new_data: New group metadata to merge
            
        Returns:
            True if group was updated successfully
        """
        with self._lock:
            if group_id not in self._node_groups:
                return False
            
            self._node_groups[group_id].update(new_data)
            self._generation += 1
            return True
    
    def get_group(self, group_id: str) -> Optional[Dict]:
        """Get group data by ID.
        
        Args:
            group_id: The group ID
            
        Returns:
            Group data dict or None if not found
        """
        with self._lock:
            return self._node_groups.get(group_id)
    
    def get_group_for_node(self, node_id: str) -> Optional[Dict]:
        """Get the group that contains a node.
        
        Args:
            node_id: The node ID to search for
            
        Returns:
            Group data dict or None if node is not in any group
        """
        with self._lock:
            for group in self._node_groups.values():
                if node_id in group['node_ids']:
                    return group
            return None
    
    def get_all_groups(self) -> Dict[str, Dict]:
        """Get all groups in the graph.
        
        Returns:
            Dict of group_id -> group_data
        """
        with self._lock:
            return dict(self._node_groups)
    
    def is_node_in_group(self, node_id: str) -> bool:
        """Check if a node is in any group.
        
        Args:
            node_id: The node ID to check
            
        Returns:
            True if node is in a group
        """
        with self._lock:
            return self.get_group_for_node(node_id) is not None

    # ── Serialization ──

    def to_dict(self) -> dict:
        """Serialize the entire graph to a dict.

        AUD-5103: the full serialization runs under ``self._lock``.  This
        method is called from file/export/bake paths that may run off the
        UI thread; iterating ``nodes``/``connections`` unlocked against a
        concurrent mutation raised ``dictionary changed size during
        iteration`` and could capture a torn topology.
        """
        from .graph_migration import COMPOSITE_SCHEMA_VERSION
        with self._lock:
            data = {
                "composite_schema_version": COMPOSITE_SCHEMA_VERSION,
                "fps": float(self.fps),
                "duration_frames": int(self.duration_frames),
                # Cheap cache invalidation tokens for headless Comp Clip preview.
                # They are separate from the render snapshot generation: the
                # serialized graph is the producer ownership boundary, while the
                # snapshot generation is local to the render-thread graph.
                "graph_revision": int(self._generation),
                "topology_revision": int(self._topology_revision),
                "nodes": [node.to_dict() for node in self.nodes.values()],
                "connections": [
                    {
                        "src_node_id": c.src_node_id,
                        "src_port_name": c.src_port_name,
                        "dst_node_id": c.dst_node_id,
                        "dst_port_name": c.dst_port_name,
                    }
                    for c in self.connections
                ],
                "backdrops": copy.deepcopy(self.backdrops),
                "sticky_notes": copy.deepcopy(self._sticky_notes),
            }

            # Serialize groups if they exist
            if self._node_groups:
                data["groups"] = {
                    gid: dict(gdata)
                    for gid, gdata in self._node_groups.items()
                }

            # Serialize layer stacks (AE/Nuke hybrid, plan §3).  Two parallel
            # dicts are written: ``layer_stacks`` (stack metadata + ordered
            # layer_ids) and ``layers`` (per-layer state).  Older readers that
            # predate the layer system simply ignore these keys.
            if self._layer_stacks:
                data["layer_stacks"] = {
                    sid: stack.to_dict() for sid, stack in self._layer_stacks.items()
                }
            if self._layers:
                data["layers"] = {
                    lid: layer.to_dict() for lid, layer in self._layers.items()
                }

            # Serialize UI state (viewport and viewer)
            if self._viewport_state:
                data["viewport_state"] = copy.deepcopy(self._viewport_state)
            if self._viewer_state:
                data["viewer_state"] = copy.deepcopy(self._viewer_state)

            return data

    @classmethod
    def from_dict(cls, data: dict) -> 'NodeGraph':
        """Deserialize a graph from a dict.

        Automatically runs schema migration if the stored version is
        older than the current COMPOSITE_SCHEMA_VERSION.
        """
        from .graph_migration import migrate_graph
        data = migrate_graph(data)

        graph = cls()
        graph.fps = float(data.get("fps", 24.0) or 24.0)
        graph.duration_frames = int(data.get("duration_frames", 0) or 0)
        graph.backdrops = cls._load_backdrops(data.get("backdrops", {}))
        graph._sticky_notes = cls._load_sticky_notes(data.get("sticky_notes", {}))

        # AUD-5104: project data is external input — a structurally valid
        # JSON with a non-dict node entry or a connection missing one of
        # its four string fields used to abort the whole project load with
        # a raw KeyError/AttributeError.  Malformed entries are now
        # skipped with a warning; the rest of the graph still loads.
        nodes_field = data.get("nodes") or ()
        if not isinstance(nodes_field, (list, tuple)):
            logger.warning(
                "from_dict: 'nodes' is not a list (got %s); ignored.",
                type(nodes_field).__name__)
            nodes_field = ()
        for node_data in nodes_field:
            if not isinstance(node_data, dict):
                logger.warning(
                    "from_dict: skipping non-dict node entry %r.",
                    node_data)
                continue
            node_type = node_data.get("type")
            node_cls = NODE_REGISTRY.get(node_type)
            if node_cls is None:
                # Unregistered type or None placeholder in registry
                # Create UnknownPlaceholderNode to preserve the node
                node = UnknownPlaceholderNode.from_dict(node_data)
                graph.add_node(node)
            elif node_cls:
                node = node_cls.from_dict(node_data)
                graph.add_node(node)

        connections_field = data.get("connections") or ()
        if not isinstance(connections_field, (list, tuple)):
            logger.warning(
                "from_dict: 'connections' is not a list (got %s); ignored.",
                type(connections_field).__name__)
            connections_field = ()
        for conn_data in connections_field:
            if not isinstance(conn_data, dict):
                logger.warning(
                    "from_dict: skipping non-dict connection entry %r.",
                    conn_data)
                continue
            src_id = conn_data.get("src_node_id")
            src_port = conn_data.get("src_port_name")
            dst_id = conn_data.get("dst_node_id")
            dst_port = conn_data.get("dst_port_name")
            if not all(isinstance(field, str) and field
                       for field in (src_id, src_port, dst_id, dst_port)):
                logger.warning(
                    "from_dict: skipping malformed connection entry %r "
                    "(needs four non-empty string fields).", conn_data)
                continue
            if not graph.connect(src_id, src_port, dst_id, dst_port):
                logger.warning(
                    "from_dict: connection %s:%s → %s:%s was rejected "
                    "(missing node/port, type mismatch, or cycle).",
                    src_id, src_port, dst_id, dst_port)

        # Rebuild dependency metadata for expressions restored from disk so a
        # later Keyframes pick-whip edit sees the complete dependency graph,
        # not only expressions created during this session.
        for node in graph.nodes.values():
            for param_name, expression in getattr(
                    node, "_expressions", {}).items():
                try:
                    local_param_names = {
                        str(name) for name in node.get_param_defs().keys()}
                except Exception:
                    local_param_names = None
                graph._expression_manager.set_expression(
                    node.id, param_name, expression,
                    local_param_names=local_param_names,
                    resolve_node_ref=graph._resolve_expression_node_ref)
        
        # Deserialize groups if present (AUD-5105): normalize + deepcopy via
        # _load_groups instead of aliasing the caller's dict — a shared
        # reference let later external edits mutate live graph state, and a
        # group missing 'node_ids' crashed get_group_for_node with KeyError.
        graph._node_groups = cls._load_groups(data.get("groups"))

        # Deserialize layer stacks (AE/Nuke hybrid, plan §3).  Tolerates
        # absence so pre-layer projects load unchanged.
        graph._load_layer_state(data.get("layer_stacks"), data.get("layers"))

        # Deserialize UI state (viewport and viewer) (AUD-5105): copy, never
        # alias the caller's dict, and ignore wrong-typed payloads instead
        # of smuggling them into live state.
        viewport_state = data.get("viewport_state")
        if isinstance(viewport_state, dict):
            graph._viewport_state = copy.deepcopy(viewport_state)
        elif viewport_state is not None:
            logger.warning(
                "from_dict: ignoring non-dict viewport_state (%s).",
                type(viewport_state).__name__)
        viewer_state = data.get("viewer_state")
        if isinstance(viewer_state, dict):
            graph._viewer_state = copy.deepcopy(viewer_state)
        elif viewer_state is not None:
            logger.warning(
                "from_dict: ignoring non-dict viewer_state (%s).",
                type(viewer_state).__name__)

        # R-6: Auto-migrate deprecated color_input nodes to topos_color
        # on load so that legacy projects transparently use the new path.
        graph.migrate_color_input_to_topos_color()

        # Output node authority: ensure the Output node is in custom format
        # mode so it owns the composition resolution. Legacy graphs stored an
        # empty parameters dict on the Output node, leaving format_mode at its
        # runtime default "input" (resolution inherited from upstream).
        graph.migrate_output_format_mode()

        return graph

    def serialize_to_bytes(
        self,
        format: str = "json",
        compress: bool = False,
        incremental: bool = False,
        saver: Optional[Any] = None
    ) -> bytes:
        """Serialize graph to bytes with format options.

        Args:
            format: Serialization format ("json" or "msgpack")
            compress: Apply gzip compression
            incremental: Use incremental save (requires saver with base snapshot)
            saver: IncrementalSaver instance for delta computation

        Returns:
            Serialized bytes
        """
        from src.features.composite.utils.serialization_formats import (
            SerializationFormat, CompressionFormat,
            serialize, save_incremental
        )

        # Map string to enum
        ser_format = SerializationFormat.MSGPACK if format == "msgpack" \
            else SerializationFormat.JSON
        compression = CompressionFormat.GZIP if compress \
            else CompressionFormat.NONE

        data = self.to_dict()

        if incremental and saver:
            return save_incremental(data, saver, ser_format, compression)

        return serialize(data, ser_format, compression)

    @classmethod
    def deserialize_from_bytes(
        cls,
        data: bytes,
        base_snapshot: Optional[dict] = None,
        saver: Optional[Any] = None
    ) -> 'NodeGraph':
        """Deserialize graph from bytes with auto-format detection.

        Supports:
        - JSON format (backward compatible)
        - MessagePack binary format
        - gzip compression (auto-detected)
        - Incremental delta format (requires base_snapshot or saver)

        Args:
            data: Serialized bytes
            base_snapshot: Optional base snapshot for delta reconstruction
            saver: Optional IncrementalSaver for delta reconstruction

        Returns:
            NodeGraph instance
        """
        from src.features.composite.utils.serialization_formats import (
            deserialize, load_incremental
        )

        # Check for incremental format
        try:
            loaded = deserialize(data)
            if "delta_type" in loaded:
                # Incremental delta - reconstruct
                loaded = load_incremental(data, base_snapshot, saver)
        except Exception:
            # Try direct deserialize
            loaded = deserialize(data)

        return cls.from_dict(loaded)

    @staticmethod
    def _load_groups(groups_data: Any) -> Dict[str, Dict[str, Any]]:
        """Normalize persisted group metadata into a dict keyed by ID.

        AUD-5105: mirrors :meth:`_load_backdrops` — tolerates absent or
        wrong-typed payloads, deep-copies entries so the graph never shares
        state with the caller's dict, and guarantees the ``node_ids`` key
        the membership accessors index on (``get_group_for_node``,
        ``add_nodes_to_group``, ...) exists as a list of strings.
        """
        groups: Dict[str, Dict[str, Any]] = {}
        if not isinstance(groups_data, dict):
            return groups
        for group_id, raw_data in groups_data.items():
            if not group_id or not isinstance(raw_data, dict):
                continue
            group = copy.deepcopy(raw_data)
            group["id"] = str(group_id)
            group["node_ids"] = [
                str(nid) for nid in (group.get("node_ids") or ())
                if nid
            ]
            groups[str(group_id)] = group
        return groups

    @staticmethod
    def _load_backdrops(backdrops_data: Any) -> Dict[str, dict]:
        """Normalize persisted backdrop metadata into a dict keyed by ID."""
        backdrops: Dict[str, dict] = {}

        if isinstance(backdrops_data, dict):
            items = backdrops_data.items()
        elif isinstance(backdrops_data, list):
            items = ((item.get("id"), item) for item in backdrops_data if isinstance(item, dict))
        else:
            return backdrops

        for backdrop_id, raw_data in items:
            if not backdrop_id or not isinstance(raw_data, dict):
                continue
            backdrop = copy.deepcopy(raw_data)
            backdrop["id"] = str(backdrop_id)
            backdrops[str(backdrop_id)] = backdrop

        return backdrops

    @staticmethod
    def _load_sticky_notes(sticky_notes_data: Any) -> Dict[str, dict]:
        """Normalize persisted sticky note metadata into a dict keyed by ID."""
        sticky_notes: Dict[str, dict] = {}

        if isinstance(sticky_notes_data, dict):
            items = sticky_notes_data.items()
        elif isinstance(sticky_notes_data, list):
            items = ((item.get("id"), item) for item in sticky_notes_data if isinstance(item, dict))
        else:
            return sticky_notes

        for note_id, raw_data in items:
            if not note_id or not isinstance(raw_data, dict):
                continue
            note = copy.deepcopy(raw_data)
            note["id"] = str(note_id)
            sticky_notes[str(note_id)] = note

        return sticky_notes

    # ── Helpers ──

    def _would_create_cycle(self, src_node_id: str, dst_node_id: str) -> bool:
        """Check if adding src→dst would create a cycle (can dst reach src?)."""
        if src_node_id == dst_node_id:
            return True

        # AUD-5110: index connections once — the previous full
        # ``self.connections`` scan per visited node was O(V·E).
        adjacency: Dict[str, List[str]] = {}
        for conn in self.connections:
            adjacency.setdefault(conn.src_node_id, []).append(conn.dst_node_id)

        visited = set()
        stack = [dst_node_id]
        while stack:
            current = stack.pop()
            if current == src_node_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            for neighbor in adjacency.get(current, []):
                stack.append(neighbor)
        return False

    def _mark_dirty_downstream(self, node_id: str) -> None:
        """Mark node and all downstream nodes as dirty."""
        # AUD-5110: index connections once — the previous full
        # ``self.connections`` scan per visited node was O(V·E).
        adjacency: Dict[str, List[str]] = {}
        for conn in self.connections:
            adjacency.setdefault(conn.src_node_id, []).append(conn.dst_node_id)

        visited = set()
        stack = [node_id]
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            node = self.nodes.get(current)
            if node:
                node._dirty = True
            for neighbor in adjacency.get(current, []):
                stack.append(neighbor)

    def migrate_color_input_to_topos_color(self, color_stack_id: str = "") -> bool:
        """Migrate deprecated color_input nodes to topos_color nodes.

        Old graphs: color_input -> Output
        Migrated graphs: timeline_clip_input -> topos_color -> Output

        The topos_color node is ALWAYS inserted. When no color_stack_id
        is provided, it runs in bypass (pass-through) mode.

        Args:
            color_stack_id: ColorStack ID to assign to the migrated topos_color node.

        Returns:
            True if any migration was performed.
        """
        migrated = False

        # AUD-5103: public graph mutation — run under the lock like every
        # other writer (it rewrites self.connections/self.nodes directly
        # and can be re-invoked when a ColorStack binds).
        with self._lock:
            # Standalone-image detection: if the graph has color_input but no
            # timeline_clip_input, it is a single-image composite where
            # color_input bridges to the Color page output. Do NOT migrate it —
            # the migrated timeline_clip_input would have no timeline backing
            # and render empty.
            has_timeline_source = any(
                n.node_type == "timeline_clip_input"
                for n in self.nodes.values()
            )
            if not has_timeline_source:
                return False

            for nid, node in list(self.nodes.items()):
                if node.node_type != "color_input":
                    continue

                # Find what the color_input connects to
                outgoing = [c for c in self.connections if c.src_node_id == nid]

                # Remove the color_input node's connections
                self.connections = [
                    c for c in self.connections
                    if c.src_node_id != nid and c.dst_node_id != nid
                ]

                # Remove the old node
                del self.nodes[nid]
                migrated = True

                # Check if there's already a timeline_clip_input
                has_source = any(
                    n.node_type == "timeline_clip_input"
                    for n in self.nodes.values()
                )

                if not has_source:
                    # Add timeline_clip_input
                    from src.features.composite.nodes.timeline_clip_input_node import TimelineClipInputNode
                    source_id = f"migrated_src_{nid}"
                    source_node = TimelineClipInputNode(node_id=source_id)
                    source_node.set_parameter("input_stage", "source_raw")
                    self.nodes[source_id] = source_node
                else:
                    source_id = next(
                        nid2 for nid2, n in self.nodes.items()
                        if n.node_type == "timeline_clip_input"
                    )

                # Always add topos_color node (bypass when no color_stack_id)
                from src.features.composite.nodes.topos_color_node import ToposColorNode
                tc_id = f"migrated_tc_{nid}"
                tc_node = ToposColorNode(node_id=tc_id)
                tc_node.set_parameter("stack_source", "clip_stack")
                tc_node.set_parameter("color_stack_id", color_stack_id or "")
                tc_node.set_parameter("role", "primary_clip_grade")
                tc_node.set_parameter("linked", True)
                tc_node.set_parameter("bypass", not bool(color_stack_id))
                tc_node.set_parameter("intensity", 1.0)
                self.nodes[tc_id] = tc_node

                # Connect: source -> topos_color -> each downstream
                from src.features.composite.nodes.base_node import Connection
                self.connections.append(Connection(
                    src_node_id=source_id,
                    src_port_name="output",
                    dst_node_id=tc_id,
                    dst_port_name="image",
                ))
                for old_out in outgoing:
                    self.connections.append(Connection(
                        src_node_id=tc_id,
                        src_port_name="result",
                        dst_node_id=old_out.dst_node_id,
                        dst_port_name=old_out.dst_port_name,
                    ))

            if migrated:
                self._generation += 1
                self._topology_revision += 1
                self._force_full_eval = True

            return migrated

    def migrate_output_format_mode(self) -> bool:
        """Ensure the Output node owns the composition resolution.

        Legacy graphs stored an empty parameters dict on the Output node,
        leaving ``format_mode`` at its runtime default ``"input"`` — the
        resolution was then implicitly inherited from whichever upstream
        node happened to feed it (Constant for empty comps, the source
        asset for timeline clips). That made "composition resolution"
        depend on graph topology rather than a single authoritative value.

        This migration switches the Output node to ``format_mode="custom"``
        so its own width/height params become the single source of truth,
        matching the AE-style "composition settings = container size" model.
        Width/height are seeded from the upstream node when available.

        Returns:
            True if any migration was performed.
        """
        # AUD-5103: mutates node parameters and bumps graph state — hold
        # the lock for the whole read-modify-write like every other writer.
        with self._lock:
            output_node = next(
                (n for n in self.nodes.values() if n.node_type == "output"),
                None,
            )
            if output_node is None:
                return False

            # Already explicitly configured — respect the user's choice.
            current_mode = output_node.parameters.get("format_mode")
            if current_mode == "custom":
                return False

            # Seed resolution from the first upstream source/constant node that
            # carries width/height params, falling back to the Output node's own
            # defaults (1920x1080).
            seed_w = output_node.parameters.get("width", 1920)
            seed_h = output_node.parameters.get("height", 1080)
            for node in self.nodes.values():
                if node.node_type in ("constant", "timeline_clip_input",
                                      "image_input", "color_input"):
                    w = node.parameters.get("width")
                    h = node.parameters.get("height")
                    # P1-4: image_input width/height default to 0 until the
                    # media is decoded, and this migration runs at from_dict
                    # time — before any decode. Seeding 0 collapsed the Output
                    # node to 1x1 via max(1, 0). Only trust strictly positive
                    # dimensions; otherwise keep looking / fall back to the
                    # Output node's own 1920x1080 defaults.
                    if (isinstance(w, (int, float)) and isinstance(h, (int, float))
                            and w > 0 and h > 0):
                        seed_w, seed_h = int(w), int(h)
                        break

            output_node.set_parameter("format_mode", "custom")
            output_node.set_parameter("width", int(seed_w))
            output_node.set_parameter("height", int(seed_h))
            self._generation += 1
            self._force_full_eval = True
            return True

    def create_default_graph(self) -> str:
        """Create a default graph with TimelineClipInput → Topos Color → Output.

        Returns output node ID. The Topos Color node is always present;
        it runs in bypass (pass-through) mode when no color_stack_id is set.
        """
        source_node = TimelineClipInputNode()
        source_node.position = (100, 200)
        source_node.set_parameter("input_stage", "source_raw")
        self.add_node(source_node)

        topos_color_node = ToposColorNode()
        topos_color_node.position = (400, 200)
        topos_color_node.set_parameter("stack_source", "clip_stack")
        topos_color_node.set_parameter("color_stack_id", "")
        topos_color_node.set_parameter("role", "primary_clip_grade")
        topos_color_node.set_parameter("linked", True)
        topos_color_node.set_parameter("bypass", True)
        topos_color_node.set_parameter("intensity", 1.0)
        self.add_node(topos_color_node)

        output_node = OutputNode()
        output_node.position = (700, 200)
        # Output node owns the authoritative composition resolution.
        output_node.set_parameter("format_mode", "custom")
        self.add_node(output_node)

        self.connect(source_node.id, "output", topos_color_node.id, "image")
        self.connect(topos_color_node.id, "result", output_node.id, "image")

        return output_node.id

    def create_image_default_graph(self, image_path: str, color_stack_id: str = "") -> str:
        """Create a default graph with ImageInput → Topos Color → Output.

        Used when entering Composite from Color page with a standalone image
        (no Edit timeline clip context available).

        Args:
            image_path: Absolute path to the image file.
            color_stack_id: Color grading stack ID for the Topos Color node.

        Returns output node ID.
        """
        source_node = ImageInputNode()
        source_node.position = (100, 200)
        source_node.set_parameter("file_path", image_path)
        source_node.set_parameter("color_space", "auto")
        self.add_node(source_node)

        topos_color_node = ToposColorNode()
        topos_color_node.position = (400, 200)
        topos_color_node.set_parameter("stack_source", "clip_stack")
        topos_color_node.set_parameter("color_stack_id", color_stack_id or "")
        topos_color_node.set_parameter("role", "primary_clip_grade")
        topos_color_node.set_parameter("linked", True)
        topos_color_node.set_parameter("bypass", not bool(color_stack_id))
        topos_color_node.set_parameter("intensity", 1.0)
        self.add_node(topos_color_node)

        output_node = OutputNode()
        output_node.position = (700, 200)
        # Output node owns the authoritative composition resolution.
        output_node.set_parameter("format_mode", "custom")
        self.add_node(output_node)

        self.connect(source_node.id, "image", topos_color_node.id, "image")
        self.connect(topos_color_node.id, "result", output_node.id, "image")

        return output_node.id


# ── Dynamic Node Registration ──
# These functions allow plugins to register new node types at runtime.


def register_node_type(node_type: str, node_class: Type[Node], category: str = None) -> bool:
    """Register a new node type in NODE_REGISTRY.

    This allows plugins to add custom node types that will be
    recognized during graph deserialization and available in UI menus.

    Args:
        node_type: Unique identifier for the node type (snake_case).
        node_class: The Node subclass to register.
        category: Optional category for UI menu organization.
                  If None, uses "Plugins" as default.

    Returns:
        True if registration succeeded, False if type already exists.

    Example:
        register_node_type("my_filter", MyFilterNode, category="Filter")
    """
    if node_type in NODE_REGISTRY and NODE_REGISTRY[node_type] is not None:
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(f"Node type '{node_type}' already registered, skipping")
        return False

    NODE_REGISTRY[node_type] = node_class

    # Add to category
    if category is None:
        category = "Plugins"

    if category not in NODE_CATEGORIES:
        NODE_CATEGORIES[category] = []

    # Check if already in category
    for existing_type, _ in NODE_CATEGORIES[category]:
        if existing_type == node_type:
            return True

    NODE_CATEGORIES[category].append((node_type, node_class))

    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"Registered node type '{node_type}' in category '{category}'")

    return True


def unregister_node_type(node_type: str) -> bool:
    """Remove a node type from NODE_REGISTRY.

    Args:
        node_type: The node type identifier to remove.

    Returns:
        True if the type was removed, False if not found.
    """
    if node_type not in NODE_REGISTRY:
        return False

    NODE_REGISTRY[node_type] = None  # Keep placeholder for compatibility

    # Remove from all categories
    for category, nodes in NODE_CATEGORIES.items():
        nodes[:] = [(t, c) for t, c in nodes if t != node_type]

    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"Unregistered node type '{node_type}'")

    return True


def register_plugin_nodes(plugin_manager=None) -> int:
    """Register all node types from loaded plugins.

    This function integrates the NodePluginManager with NODE_REGISTRY,
    making plugin-provided nodes available for graph operations.

    N-13（2026-10-14 审计）：同时记录插件拥有的 node types 并在
    manager 上安装 on_registry_sync 钩子——插件卸载后核心注册表同步
    移除，create_node_by_type 不再能创建已卸载插件的类。

    Args:
        plugin_manager: Optional NodePluginManager instance.
                        If None, uses the global manager from node_plugin.py.

    Returns:
        Number of node types successfully registered.
    """
    if plugin_manager is None:
        from src.features.composite.core.node_plugin import get_plugin_manager
        plugin_manager = get_plugin_manager()

    registered_count = 0

    for node_type, node_class in plugin_manager.node_types.items():
        # Get category from plugin
        plugin = plugin_manager.get_plugin_for_node(node_type)
        category = plugin.category() if plugin else "Plugins"

        if register_node_type(node_type, node_class, category):
            _PLUGIN_OWNED_NODE_TYPES.add(node_type)
            registered_count += 1

    # N-13：manager 加载/卸载后同步核心注册表。
    plugin_manager.on_registry_sync = sync_plugin_node_registry

    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"Registered {registered_count} node types from plugins")

    return registered_count


# N-13：由插件进入全局 NODE_REGISTRY 的 node types（用于卸载时同步
# 移除；内置类型不受影响）。
_PLUGIN_OWNED_NODE_TYPES: set = set()


def sync_plugin_node_registry(plugin_manager=None) -> dict:
    """Reconcile NODE_REGISTRY/NODE_CATEGORIES with the plugin manager (N-13).

    增量同步：manager 仍提供的插件类型保持注册（缺失的补注册）；
    已不在 manager.node_types 里的插件拥有类型从全局注册表与分类中
    移除。返回 ``{"registered": [...], "unregistered": [...]}``。
    """
    import logging

    if plugin_manager is None:
        from src.features.composite.core.node_plugin import get_plugin_manager
        plugin_manager = get_plugin_manager()

    logger = logging.getLogger(__name__)
    manager_types = dict(plugin_manager.node_types)

    registered: list = []
    for node_type, node_class in manager_types.items():
        if NODE_REGISTRY.get(node_type) is node_class:
            continue
        plugin = plugin_manager.get_plugin_for_node(node_type)
        category = plugin.category() if plugin else "Plugins"
        if node_type in NODE_REGISTRY:
            # override/版本更替：直接换类（不经 register_node_type 的
            # 已存在拒绝分支），分类保持。
            NODE_REGISTRY[node_type] = node_class
            _PLUGIN_OWNED_NODE_TYPES.add(node_type)
            registered.append(node_type)
        elif register_node_type(node_type, node_class, category):
            _PLUGIN_OWNED_NODE_TYPES.add(node_type)
            registered.append(node_type)

    unregistered: list = []
    for node_type in list(_PLUGIN_OWNED_NODE_TYPES):
        if node_type in manager_types:
            continue
        if NODE_REGISTRY.get(node_type) is not None:
            del NODE_REGISTRY[node_type]
        for category, entries in list(NODE_CATEGORIES.items()):
            NODE_CATEGORIES[category] = [
                (nt, cls) for (nt, cls) in entries if nt != node_type]
            if not NODE_CATEGORIES[category]:
                del NODE_CATEGORIES[category]
        _PLUGIN_OWNED_NODE_TYPES.discard(node_type)
        unregistered.append(node_type)

    if registered or unregistered:
        logger.info(
            "Plugin registry sync: +%d / -%d node types (%s)",
            len(registered), len(unregistered),
            {"registered": registered, "unregistered": unregistered})
    return {"registered": registered, "unregistered": unregistered}


# Node types that only make sense with a time source (video/timeline). In
# Image Color mode these are hidden from the Add Node menu because a static
# image has no frames to offset/hold/retime.
TIME_DRIVEN_NODE_TYPES: frozenset = frozenset({
    "timeline_clip_input",
    "video_frame",
    "time_offset",
    "retime",
    "frame_hold",
})


def get_categories_for_mode(
    image_mode: bool,
) -> Dict[str, List[Tuple[str, Optional[Type[Node]]]]]:
    """Return node categories filtered for the active work mode.

    In image mode the time-driven node types (timeline input, video frame,
    time offset, retime, frame hold) are stripped from every category so
    users cannot add nodes that have no meaning on a static image. The core
    registry is untouched — this only affects the Add Node UI.
    """
    if not image_mode:
        return dict(NODE_CATEGORIES)
    filtered: Dict[str, List[Tuple[str, Optional[Type[Node]]]]] = {}
    for category, nodes in NODE_CATEGORIES.items():
        kept = [(nt, cls) for (nt, cls) in nodes if nt not in TIME_DRIVEN_NODE_TYPES]
        if kept:
            filtered[category] = kept
    return filtered


def discover_and_register_plugins() -> int:
    """Discover plugins and register their node types.

    Convenience function that combines plugin discovery with registration.

    Returns:
        Total number of node types registered.
    """
    from src.features.composite.core.node_plugin import discover_and_register_plugins
    loaded_plugins = discover_and_register_plugins()
    registered_nodes = register_plugin_nodes()

    import logging
    logger = logging.getLogger(__name__)
    logger.info(
        f"Plugin discovery complete: {loaded_plugins} plugins loaded, "
        f"{registered_nodes} node types registered"
    )

    return registered_nodes


def create_node_by_type(node_type: str, node_id: str = None, **kwargs) -> Optional[Node]:
    """Create a node instance by type identifier.

    Searches NODE_REGISTRY first, then falls back to plugin manager.
    This is the unified factory method for node creation.

    Args:
        node_type: The node type identifier.
        node_id: Optional node ID. If None, generates UUID.
        **kwargs: Additional parameters for the node constructor.

    Returns:
        Node instance or None if type not found.
    """
    # Try built-in registry first
    node_class = NODE_REGISTRY.get(node_type)

    if node_class is None:
        # Try plugin manager
        from src.features.composite.core.node_plugin import get_plugin_manager
        manager = get_plugin_manager()
        node_class = manager.node_types.get(node_type)

    if node_class is None:
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(f"Unknown node type '{node_type}'")
        return None

    if node_id:
        return node_class(node_id=node_id, **kwargs)
    return node_class(**kwargs)
