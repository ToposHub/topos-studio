"""
Scene3D Node — 3D 场景渲染节点。

在 composite 节点图中提供真实 3D 渲染能力（对齐 Nuke ScanlineRender / Element 3D）。
在现有 ModernGL context 内执行 mesh draw call，输出一张 HDR RGBA 纹理（+ 可选深度），
作为普通 TextureResult 流入下游合成节点，零拷贝复用现有 HDR/深度管线。

架构：仿照 ConstantNode 的"自管 GL 资源源节点"模式：
- action_type="source"，由 evaluator 的 _sync_render_nodes 维护持久实例
- _do_evaluate 内执行真实 mesh draw call（不走 marker/effect 分发）
- owns_color_texture=False（避免 CacheManager 双重释放，同 ConstantNode）
- 程序/VBO/VAO 懒建并缓存于实例（_ensure_* 模式）
- requires_frame_number=True（相机/物体参数可挂动画，换帧需重渲染）
- cleanup() 释放所有 GL 资源

Scene3D 本身不再持有相机、灯光或内置几何；这些由全局默认场景和
Camera3D/Light3D/Geo3D/Model3D payload 提供。
"""

import logging
import os
import time
import numpy as np
import moderngl
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from .base_node import Node, Port, TextureResult, ParamDef
from ..core.format import ImageFormat, BBox
from ..core.color_space import ColorSpace
from ..render3d.primitives import get_primitive, MeshData
from ..render3d.gizmo_renderer import GizmoRenderer
from ..render3d.skeleton_renderer import SkeletonOverlayRenderer
from ..render3d.aov import (
    AOVPass,
    AOV_EXPORTABLE_PASSES,
    IDAllocator,
    encode_id_to_rgba,
)
from ..render3d.cryptomatte import build_cryptomatte_manifest
from ..render3d import math_utils as m3d
from ..ui.viewport3d_state import (
    ViewportQualityProfile, QUALITY_PROFILE_PARAMS,
    LARGE_MODEL_TRIANGLE_THRESHOLD,
)

if TYPE_CHECKING:  # pragma: no cover - annotation-only imports
    from ..render3d.hdri import HDRIBaker
    from ..render3d.scene_graph import SceneGraphRoot

logger = logging.getLogger(__name__)


def _srgb_to_linear_channel(value: float) -> float:
    """Convert an sRGB UI color channel to scene-linear for HDR rendering."""
    v = max(0.0, min(1.0, float(value)))
    if v <= 0.04045:
        return v / 12.92
    return ((v + 0.055) / 1.055) ** 2.4


def merge_vbo_cache_hit(prev: Optional[bool], last_was_cached: bool) -> bool:
    """AUD-7309: aggregate the per-mesh VBO pool hits for one frame.

    Semantics (documented at the recording site): ANY mesh hitting the
    pool marks the frame ``True``; a frame in which meshes were processed
    but NONE hit becomes ``False``.  The previous implementation only ever
    assigned ``True``, so ``perf_stats.vbo_cache_hit`` could not
    distinguish an all-miss frame (should be ``False``) from a cache-hit
    frame that never entered the GL build path (stays ``None``).
    """
    return bool(prev) or bool(last_was_cached)


def model_cache_hit_from_status(status: Optional[str]) -> Optional[bool]:
    """AUD-7309: convert the model import cache status into a tri-state.

    ``"in_memory"``/``"disk_hit"`` → True, ``"miss"`` → False, unknown or
    absent status → None (not measured).  Previously
    ``perf_stats.model_cache_hit`` read ``self._perf_model_cache_hit``,
    an attribute never assigned anywhere — the field was permanently None
    and carried no diagnostic value.
    """
    if not status:
        return None
    return str(status) in ("in_memory", "disk_hit")


class Scene3DNode(Node):
    """3D 场景渲染节点。

    在现有 ModernGL context 内把连接进来的 3D scene payload 渲染为一张
    HDR RGBA 纹理。相机、灯光、默认显示环境由全局 3D Scene Settings 或
    连接的 Camera3D/Light3D 提供；几何由 Geo3D/Model3D 提供。
    """

    node_type = "scene3d"
    # CP-044: 3D 场景载荷族（scene 输入语义 + 端口数契约），不参与通用 mask。
    GENERIC_MASK_MIX = False

    node_title = "Scene3D"
    i18n_title_key = "nodes.scene3d.title"
    header_color = "#2d6a4f"  # 深绿，区分 3D 类节点

    SEARCH_ALIASES = ["scene3d", "3d", "render3d", "三维", "3d场景", "mesh", "cube"]
    DESCRIPTION = "3D 场景渲染器：把连接的 3D 元素渲染为 HDR 图像"
    TAGS = ["3d", "source", "generator"]

    @staticmethod
    def _build_cryptomatte_metadata(
        model_payload: object | None,
        geo_payloads: list[object] | tuple[object, ...] = (),
    ) -> Optional[dict]:
        """Build a manifest-only object matte descriptor for diagnostics.

        The current GPU object-ID pass remains the render source. This
        descriptor records stable object names and the standard hash manifest
        so a future Cryptomatte writer can consume it without re-traversing
        the imported scene on the UI thread.
        """
        objects: list[dict[str, str]] = []
        if model_payload is not None:
            for mesh in getattr(model_payload, "flat_meshes", ()) or ():
                if not isinstance(mesh, dict):
                    continue
                name = str(mesh.get("node_name", "") or "").strip()
                node_id = str(mesh.get("node_id", "") or "").strip()
                if name or node_id:
                    objects.append({
                        "name": name,
                        "node_id": node_id,
                    })
        for index, payload in enumerate(geo_payloads or ()):
            node_id = str(getattr(payload, "node_id", "") or "").strip()
            if node_id:
                objects.append({"name": f"geo:{node_id}", "node_id": node_id})
            elif payload is not None:
                objects.append({"name": f"geo:{index}"})
        if not objects:
            return None
        try:
            manifest = build_cryptomatte_manifest(objects)
        except ValueError as exc:
            return {
                "status": "collision",
                "error": str(exc),
                "layer": "crypto_object",
                "object_count": len(objects),
            }
        metadata = manifest.to_metadata()
        metadata.update({
            "status": "manifest_only",
            "aov_source": AOVPass.OBJECT_ID.value,
        })
        return metadata

    PARAM_DEFS = [
        # ── 画幅（渲染器必需）──
        ParamDef("width", "int", 1920, min_val=1, max_val=16384, step=1),
        ParamDef("height", "int", 1080, min_val=1, max_val=16384, step=1),
        # ── 输出元数据 ──
        ParamDef("format_name", "str", "Scene3D", hidden=True),
        # 场景功能参数（cam_*/bg_*/light_*/hdri_preset/use_ibl/show_grid/
        # show_axes/output_*/msaa/active_camera_id）在 Camera3D/Light3D 或
        # 全局「3D Scene Settings」。几何/材质参数在 Geo3D/Model3D。
        # Scene3D 自身只作为 3D → 2D 渲染器。
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        # 单一 scene 输入端口 `in`：摄影机/灯光/几何/模型都连到此处。
        # Scene3D 按 payload 类型（CameraPayload/LightPayload/GeometryPayload/
        # ModelPayload）自动分派渲染路径，无需按端口区分源类型。
        # max_connections=8 支持多源（多摄影机/多灯光/多几何模型）。
        # 无连接时渲染纯背景（旧 .tos 经 v8 迁移把 camera_in/lights_in/geometry_in
        # 合并到此端口）。
        self.inputs = [
            Port("in", "input", "scene", max_connections=8),
        ]
        # Keep beauty backward-compatible as ``display`` while exposing each
        # implemented auxiliary pass as a normal image output.  The ports are
        # static so serialized connections remain valid when a user toggles an
        # AOV; disabled passes return no output and are rejected by preflight.
        self.outputs = [Port("display", "output", "image")]
        self.outputs.extend(
            Port(pass_type.value, "output", "image")
            for pass_type in AOV_EXPORTABLE_PASSES
            if pass_type != AOVPass.BEAUTY
        )
        self.parameters = {
            # 画幅
            "width": 1920,
            "height": 1080,
            # 元数据
            "format_name": "Scene3D",
        }

        # ── 持久 GL 资源（懒建，cleanup 释放）──
        # 程序与几何资源（primitive 不变则复用）
        self._mesh_prog: Optional[moderngl.Program] = None
        self._mesh_vbo: Optional[moderngl.Buffer] = None
        self._mesh_ibo: Optional[moderngl.Buffer] = None
        self._mesh_data: Optional[MeshData] = None
        self._mesh_vao: Optional[moderngl.VertexArray] = None
        self._mesh_index_count: int = 0
        self._mesh_is_lines: bool = False  # grid 用 LINES
        self._last_primitive: Optional[str] = None
        self._mesh_instance_enabled: bool = False  # procedural VAO 是否含 instance buffer

        # 导入模型（阶段五）—— 每个 FlattenedMesh 一组 GL 资源
        self._mesh_skinned_prog: Optional[moderngl.Program] = None  # 蒙皮 PBR 程序
        self._model_root: Optional["SceneGraphRoot"] = None
        self._model_path_cached: Optional[str] = None
        self._model_mtime_cached: float = 0.0
        self._model_error: Optional[str] = None  # 加载失败的提示（UI 可显示）
        self._last_gl_ctx = None
        # 扁平 mesh 的 GL 资源列表（每元素 dict: vbo/ibo/vao/index_count/is_skinned/
        # material/world_matrix/skin/node_name）；按 model_path 缓存。
        self._flat_meshes_gl: list = []
        self._flat_instance_enabled: bool = False  # flat mesh VAO 是否含 instance buffer
        # Model3D 材质升级：材质贴图加载器（render thread 用，按 path+mtime 缓存 GL 纹理）。
        # 导入材质的 TextureDescriptor（base_color/normal/ORM/AO/emissive）经此上传为
        # GL 纹理并绑定到对应 sampler，置 has_map=1。texture_overrides（外部端口输入的
        # glo）直接用 OpenGL 绑定，不经此 loader（已是 GPU 资源）。
        self._material_tex_loader = None  # 延迟创建（首次需要时）

        # 实例化（阶段六）—— instance buffer + instanced program，按 (grid_x,grid_z,spacing) 缓存
        self._mesh_instanced_prog: Optional[moderngl.Program] = None
        self._instance_vbo: Optional[moderngl.Buffer] = None
        self._instance_count_cached: int = 0       # 当前 instance buffer 的实例数
        self._instance_sig_cached: Optional[tuple] = None  # (grid_x, grid_z, spacing)

        # 深度合成（阶段六）—— R32F 可采样深度纹理（供 deep_merge 消费）。
        # 注意：_depth_rb 仍保留做遮挡测试（renderbuffer 不可采样，但 depth_test 够用）。
        self._depth_texture: Optional[moderngl.Texture] = None     # R32F 线性深度
        self._depth_write_prog: Optional[moderngl.Program] = None  # 第二遍深度写入程序
        self._depth_write_skinned_prog: Optional[moderngl.Program] = None  # 蒙皮深度写入程序
        self._depth_write_instanced_prog: Optional[moderngl.Program] = None  # 实例化深度写入程序
        self._depth_fbo: Optional[moderngl.Framebuffer] = None     # depth-only FBO
        self._depth_write_vaos: dict = {}  # 复用：procedural/flat mesh 的 depth-write VAO
        self._depth_write_buffers: dict = {}

        # P1-21：directional shadow map + transparent shadow-catcher pass.
        # The map is allocated lazily only when a source object uses the
        # shadow_catcher role, keeping ordinary Scene3D renders unchanged.
        self._shadow_texture: Optional[moderngl.Texture] = None
        self._shadow_depth_rb: Optional[moderngl.Renderbuffer] = None
        self._shadow_fbo: Optional[moderngl.Framebuffer] = None
        self._shadow_depth_prog: Optional[moderngl.Program] = None
        self._shadow_depth_skinned_prog: Optional[moderngl.Program] = None
        self._shadow_depth_instanced_prog: Optional[moderngl.Program] = None
        self._shadow_catcher_prog: Optional[moderngl.Program] = None
        self._shadow_catcher_skinned_prog: Optional[moderngl.Program] = None
        self._shadow_catcher_instanced_prog: Optional[moderngl.Program] = None
        self._shadow_write_vaos: dict = {}
        self._shadow_write_buffers: dict = {}
        self._shadow_map_size: int = 1024
        self._shadow_map_allocated_size: int = 0

        # P0-07：按需生成的几何 AOV（normal/position/uv/motion_vector）。这些纹理与主
        # render target 同生命周期，由 Scene3D 持有，沿 TextureResult 的
        # aov_texture_ids 旁路传递到 Viewer/FileOutput。
        self._aov_textures: Dict[str, Optional[moderngl.Texture]] = {}
        self._aov_fbos: Dict[str, Optional[moderngl.Framebuffer]] = {}
        self._aov_depth_rbs: Dict[str, Optional[moderngl.Renderbuffer]] = {}
        self._aov_prog: Optional[moderngl.Program] = None
        self._aov_instanced_prog: Optional[moderngl.Program] = None
        self._aov_skinned_prog: Optional[moderngl.Program] = None
        self._aov_write_vaos: dict = {}
        self._aov_write_buffers: dict = {}
        # Material IDs are stable for the lifetime of this Scene3D instance;
        # the key comes from imported material_id/name or a source-node
        # fallback for procedural Geo3D payloads.
        self._material_id_allocator = IDAllocator()
        self._motion_prog: Optional[moderngl.Program] = None
        self._motion_instanced_prog: Optional[moderngl.Program] = None
        self._motion_skinned_prog: Optional[moderngl.Program] = None
        self._motion_write_vaos: dict = {}
        self._motion_write_buffers: dict = {}
        # Motion history is render-thread state. It is deliberately kept out
        # of graph/cache signatures: a cache hit must not advance temporal
        # history, while a real frame render advances it exactly once.
        self._motion_prev_proj: Optional[np.ndarray] = None
        self._motion_prev_view: Optional[np.ndarray] = None
        self._motion_prev_frame: Optional[int] = None
        self._motion_prev_scene_key: Optional[tuple] = None
        self._motion_prev_models: Dict[str, np.ndarray] = {}
        self._motion_prev_anim_worlds: Dict[str, np.ndarray] = {}

        # P7：GPU ID picking —— RGBA8 object-id 纹理 + id-only FBO + 简单 id-write 程序。
        # 每帧渲染（3D 模式），viewer 点击时读 1 像素 → pick_id → node_id。
        self._id_texture: Optional[moderngl.Texture] = None        # RGBA8 object id
        self._id_write_prog: Optional[moderngl.Program] = None     # 常量输出 id 的 fragment
        self._id_write_skinned_prog: Optional[moderngl.Program] = None  # 蒙皮 ID 写入程序
        self._id_fbo: Optional[moderngl.Framebuffer] = None        # id-only FBO
        self._id_write_vaos: dict = {}  # 复用：procedural/flat mesh 的 id-write VAO
        # AUD-7301: id-write VAO 自建 VBO/IBO 的释放记账（与 _depth_write_buffers
        # 同模式；procedural 分支借用主 _mesh_vbo/_mesh_ibo，不在此记账）。
        self._id_write_buffers: dict = {}
        # P1-22：CPU component picking 共享 ID pass 的纯几何快照。只携带
        # numpy 顶点/索引和 world matrix，不把 GL 对象跨到 UI 线程。
        self._p7_component_pick_info: Optional[dict] = None
        # P7：选中对象描边（inverted hull）—— outline 程序 + per-mesh VAO 缓存。
        self._outline_prog: Optional[moderngl.Program] = None
        self._outline_vaos: dict = {}  # 复用：procedural/flat mesh 的 outline VAO
        # AUD-7301: outline VAO 自建 VBO/IBO 的释放记账（同 _id_write_buffers）。
        self._outline_buffers: dict = {}

        # 每帧渲染目标（绑定到具体尺寸，尺寸/内容变则重建）
        # 注意：msaa>0 时 _gpu_texture 是 resolve 后的普通纹理（下游采样用），
        # 多重采样 color/depth 是 _render_fbo 的附件（_msaa_color/_msaa_depth）。
        self._gpu_texture: Optional[moderngl.Texture] = None   # 最终输出（resolve 目标）
        self._depth_rb: Optional[moderngl.Renderbuffer] = None  # msaa=0: 主深度；msaa>0: msaa 深度
        self._render_fbo: Optional[moderngl.Framebuffer] = None
        # MSAA（msaa>0 专用）：多重采样 color 纹理 + resolve FBO。
        self._msaa_color: Optional[moderngl.Texture] = None     # msaa color（多重采样）
        self._resolve_fbo: Optional[moderngl.Framebuffer] = None  # resolve-only FBO（绑 _gpu_texture）
        self._msaa_samples: int = 0  # 当前 _render_fbo 实际用的采样数（0=非多重采样）
        # P1-4：navigation 真降采样。nav 态渲染目标用降采样尺寸 (rw*rh)，
        # _gpu_texture 保持全尺寸（输出契约不变），渲染后 copy_framebuffer 上采样。
        # _nav_render_color/_nav_render_fbo 仅在 _nav_res_scale<1.0 时创建。
        self._nav_render_color: Optional[moderngl.Texture] = None
        self._nav_render_fbo: Optional[moderngl.Framebuffer] = None
        self._nav_render_scale: float = 1.0  # 当前渲染目标实际用的 scale（1.0=全尺寸）
        # P1-4：当前渲染目标的实际像素尺寸（降采样后 != 项目尺寸）。viewport 设置 +
        # 投影矩阵 aspect 用此值，避免降采样时内容偏到左下角。
        self._render_target_w: int = 0
        self._render_target_h: int = 0

        # 场景辅助元素渲染器（地面网格）。网格作为真实几何烘焙进输出纹理，
        # 与模型共享同一个 _render_fbo + depth buffer → 硬件 DEPTH_TEST 自动遮挡。
        self._gizmo: Optional[GizmoRenderer] = None
        # F6：骨骼 overlay 渲染器（线段 + 关节点）。show_skeleton=True 时绘制。
        self._skeleton_renderer = None  # SkeletonOverlayRenderer（懒建）

        # HDRI/IBL baker（阶段四）——持有多张 cubemap + LUT，cleanup 释放
        self._hdri_baker: Optional["HDRIBaker"] = None

        # P2 灯光重构：多灯光类型数据（由 _do_evaluate 打包，_set_shared_uniforms 读）
        # 默认全 0/方向光，保证无 Light3D 连接时 shader uniform 有合法值。
        self._merged_direct_lights: Optional[list] = None
        self._ambient_color = (0.0, 0.0, 0.0)
        self._ambient_intensity: float = 0.0
        self._light_types_t = (0, 0, 0, 0)
        self._light_pos_t = ((0.0, 0.0, 0.0),) * 4
        self._light_range_t = (0.0, 0.0, 0.0, 0.0)
        self._light_inner_t = (0.866, 0.866, 0.866, 0.866)  # cos(30°)
        self._light_outer_t = (0.707, 0.707, 0.707, 0.707)  # cos(45°)
        # P3 摄影机扩展：曝光（由 cam_payload 应用时设置）
        self._cam_exposure: float = 0.0

        # 内容签名（决定是否重渲染，仿 ConstantNode 模式）
        self._last_content_sig: Optional[tuple] = None
        # P6：场景内容签名（不含相机/视图）。仅相机变化时不变 → 跳过资源重建。
        # 注意：它仍包含灯光/材质/动画/transform 等需要重画的内容；这些变化
        # 不应重建 FBO，只应重新 draw。
        self._last_scene_content_sig: Optional[tuple] = None
        self._last_size: Tuple[int, int] = (0, 0)
        self._revision: int = 0
        self._cache_invalidated_needed: bool = False
        # P7: 导航 LOD 当前是否激活（交互态降 MSAA）。诊断元数据用。
        self._lod_active: bool = False

        # ── 四视图正交纹理（quad view，M2 增强）──
        # quad 模式时额外渲染 TOP/RIGHT/FRONT 三个正交视图，各自一张独立纹理。
        # 尺寸为透视纹理的 1/2（象限只占半屏，省填充率）。ortho scale 跟随 cam_dist。
        # None = 未渲染（quad_view=False 或纹理未就绪）。每帧 content_changed 时重建。
        self._ortho_textures: Dict[str, Optional[moderngl.Texture]] = {
            "top": None, "right": None, "front": None,
        }
        self._ortho_fbos: Dict[str, Optional[moderngl.Framebuffer]] = {
            "top": None, "right": None, "front": None,
        }
        # AUD-7300: 每个 FBO 的 depth renderbuffer 记账（与 _aov_depth_rbs
        # 同款）。此前是 _render_ortho_views 的局部变量，FBO 释放不连带释放
        # depth attachment，quad 视图每次尺寸重建泄漏 3 个 renderbuffer。
        self._ortho_depth_rbs: Dict[str, Optional[moderngl.Renderbuffer]] = {
            "top": None, "right": None, "front": None,
        }
        self._ortho_size: Tuple[int, int] = (0, 0)  # 当前正交纹理尺寸
        self._last_quad_view: bool = False  # 上一帧 quad 开关（诊断用）

        # ── Milestone A / P0：3D 视口性能统计 ──
        # 每帧重置；render thread 写入，UI thread 经 render_diagnostics 读。
        # _perf_frame_id 单调递增，用于追踪帧；_perf_logger 每秒节流输出。
        from ..render3d.perf_stats import Viewport3DPerfStats, StatsLogger
        import logging as _logging
        self._perf_stats = Viewport3DPerfStats()
        self._perf_frame_id: int = 0
        self._perf_logger = StatsLogger(
            _logging.getLogger(__name__ + ".perf"),
            interval=1.0,
            enabled=False,  # 默认关，由 viewer debug overlay / 环境变量开启
        )

        # ── Milestone D / P3：材质状态缓存 ──
        # 连续 mesh 用相同材质 + 纹理时跳过 _bind_material_textures / null samplers /
        # u_has_*_map 设置（视觉等价，省 Python 调用 + GL state change）。
        # per-frame-per-view 生命周期：_do_evaluate 开头和 _render_ortho_views 每视图开头 reset。
        from ..render3d.material_state import MaterialStateCache
        self._material_cache = MaterialStateCache()
        # P3-4：sampler location 已设置的 program 集合（program 级，跨帧持久）。
        # _bind_null_material_samplers 的 sampler uniform 赋值每个 program 只做一次，
        # 避免每 mesh 重复 glActiveTexture + glUniform1i（procedural/Geo3D 路径收益大）。
        # program 释放后从集合移除（cleanup 清空）。
        self._sampler_loc_set_progs: set = set()
        # P8-2：shared uniforms（view/proj/lights/IBL，22 个）的 view 级缓存。
        # key = (program_id, view_sig)，value = view_sig（标记该 program 在本 view 下已设过）。
        # view_sig 含 proj/view 矩阵 hash + 灯光 + IBL 强度；任一变化 → 重新设全量。
        # 收益：多 mesh 同 program 场景，22 个 uniform 从 N 次/帧降到 1 次/视图/program。
        self._shared_uniforms_cache: dict = {}
        self._shared_uniforms_view_sig: Optional[tuple] = None
        # P8-0：本帧 MeshVBOPool 是否命中（_ensure_flat_meshes_gl 汇总）。
        self._last_vbo_cache_hit: Optional[bool] = None
        # Milestone F / P5-4：LOD mesh 的 VAO 缓存（content_hash → VAO）。
        # 导航态用 LOD mesh 渲染；LOD VAO 首次构建后跨帧复用（MeshVBOPool 共享 VBO/IBO）。
        # key = LOD MeshData 的 content_hash；value = (vao, triangle_count)。
        self._lod_vao_cache: dict = {}
        # Milestone C / P2-4：非活动正交视图降频时间戳。
        # key=视图名(top/right/front)，value=上次渲染该视图的 monotonic 时间戳。
        # 导航态时非活动视图每 inactive_view_fps 对应的间隔才重渲染一次（不是全跳过）。
        # content_changed=False（cache-hit）帧也据此决定是否刷新正交纹理。
        self._ortho_last_render: dict = {"top": 0.0, "right": 0.0, "front": 0.0}

    # ── 资源懒建 ──

    def _ensure_mesh_program(self, ctx) -> moderngl.Program:
        """懒编译并缓存 mesh 着色器程序（阶段四：PBR Cook-Torrance + IBL）。"""
        if self._mesh_prog is None:
            from ..core.shaders import _MESH_VERTEX, _MESH_PBR_FRAGMENT
            self._mesh_prog = ctx.program(
                vertex_shader=_MESH_VERTEX,
                fragment_shader=_MESH_PBR_FRAGMENT,
            )
        return self._mesh_prog

    def _ensure_skinned_program(self, ctx) -> moderngl.Program:
        """懒编译并缓存蒙皮 PBR 程序（阶段五：带 u_bone_matrices[64]）。"""
        if self._mesh_skinned_prog is None:
            from ..core.shaders import _MESH_VERTEX_SKINNED, _MESH_PBR_FRAGMENT
            self._mesh_skinned_prog = ctx.program(
                vertex_shader=_MESH_VERTEX_SKINNED,
                fragment_shader=_MESH_PBR_FRAGMENT,
            )
        return self._mesh_skinned_prog

    def _ensure_instanced_program(self, ctx) -> moderngl.Program:
        """懒编译并缓存实例化 PBR 程序（阶段六：in vec3 in_instance_offset）。"""
        if self._mesh_instanced_prog is None:
            from ..core.shaders import _MESH_VERTEX_INSTANCED, _MESH_PBR_FRAGMENT
            self._mesh_instanced_prog = ctx.program(
                vertex_shader=_MESH_VERTEX_INSTANCED,
                fragment_shader=_MESH_PBR_FRAGMENT,
            )
        return self._mesh_instanced_prog

    def _ensure_depth_write_program(self, ctx) -> moderngl.Program:
        """懒编译并缓存深度写入程序（阶段六：复用 _MESH_VERTEX + 深度 fragment）。"""
        if self._depth_write_prog is None:
            from ..core.shaders import _MESH_VERTEX, _MESH_DEPTH_WRITE_FRAGMENT
            self._depth_write_prog = ctx.program(
                vertex_shader=_MESH_VERTEX,
                fragment_shader=_MESH_DEPTH_WRITE_FRAGMENT,
            )
        return self._depth_write_prog

    def _ensure_depth_write_skinned_program(self, ctx) -> moderngl.Program:
        """懒编译并缓存带骨骼属性的深度写入程序。"""
        if self._depth_write_skinned_prog is None:
            from ..core.shaders import _MESH_VERTEX_SKINNED, _MESH_DEPTH_WRITE_FRAGMENT
            self._depth_write_skinned_prog = ctx.program(
                vertex_shader=_MESH_VERTEX_SKINNED,
                fragment_shader=_MESH_DEPTH_WRITE_FRAGMENT,
            )
        return self._depth_write_skinned_prog

    def _ensure_depth_write_instanced_program(self, ctx) -> moderngl.Program:
        """懒编译带实例偏移属性的深度写入程序。"""
        if self._depth_write_instanced_prog is None:
            from ..core.shaders import (
                _MESH_VERTEX_INSTANCED_DEPTH, _MESH_DEPTH_WRITE_FRAGMENT)
            self._depth_write_instanced_prog = ctx.program(
                vertex_shader=_MESH_VERTEX_INSTANCED_DEPTH,
                fragment_shader=_MESH_DEPTH_WRITE_FRAGMENT,
            )
        return self._depth_write_instanced_prog

    def _ensure_shadow_depth_program(self, ctx) -> moderngl.Program:
        """懒编译普通 directional shadow-map 深度程序。"""
        if self._shadow_depth_prog is None:
            from ..core.shaders import _MESH_VERTEX, _MESH_SHADOW_DEPTH_FRAGMENT
            self._shadow_depth_prog = ctx.program(
                vertex_shader=_MESH_VERTEX,
                fragment_shader=_MESH_SHADOW_DEPTH_FRAGMENT,
            )
        return self._shadow_depth_prog

    def _ensure_shadow_depth_skinned_program(self, ctx) -> moderngl.Program:
        """懒编译蒙皮 directional shadow-map 深度程序。"""
        if self._shadow_depth_skinned_prog is None:
            from ..core.shaders import (
                _MESH_VERTEX_SKINNED, _MESH_SHADOW_DEPTH_FRAGMENT)
            self._shadow_depth_skinned_prog = ctx.program(
                vertex_shader=_MESH_VERTEX_SKINNED,
                fragment_shader=_MESH_SHADOW_DEPTH_FRAGMENT,
            )
        return self._shadow_depth_skinned_prog

    def _ensure_shadow_depth_instanced_program(self, ctx) -> moderngl.Program:
        """懒编译实例化 directional shadow-map 深度程序。"""
        if self._shadow_depth_instanced_prog is None:
            from ..core.shaders import (
                _MESH_VERTEX_INSTANCED_DEPTH, _MESH_SHADOW_DEPTH_FRAGMENT)
            self._shadow_depth_instanced_prog = ctx.program(
                vertex_shader=_MESH_VERTEX_INSTANCED_DEPTH,
                fragment_shader=_MESH_SHADOW_DEPTH_FRAGMENT,
            )
        return self._shadow_depth_instanced_prog

    def _ensure_shadow_catcher_program(self, ctx) -> moderngl.Program:
        """懒编译普通 shadow-catcher 程序。"""
        if self._shadow_catcher_prog is None:
            from ..core.shaders import _MESH_VERTEX, _MESH_SHADOW_CATCHER_FRAGMENT
            self._shadow_catcher_prog = ctx.program(
                vertex_shader=_MESH_VERTEX,
                fragment_shader=_MESH_SHADOW_CATCHER_FRAGMENT,
            )
        return self._shadow_catcher_prog

    def _ensure_shadow_catcher_skinned_program(self, ctx) -> moderngl.Program:
        """懒编译蒙皮 shadow-catcher 程序。"""
        if self._shadow_catcher_skinned_prog is None:
            from ..core.shaders import (
                _MESH_VERTEX_SKINNED, _MESH_SHADOW_CATCHER_FRAGMENT)
            self._shadow_catcher_skinned_prog = ctx.program(
                vertex_shader=_MESH_VERTEX_SKINNED,
                fragment_shader=_MESH_SHADOW_CATCHER_FRAGMENT,
            )
        return self._shadow_catcher_skinned_prog

    def _ensure_shadow_catcher_instanced_program(self, ctx) -> moderngl.Program:
        """懒编译实例化 shadow-catcher 程序。"""
        if self._shadow_catcher_instanced_prog is None:
            from ..core.shaders import (
                _MESH_VERTEX_INSTANCED_DEPTH, _MESH_SHADOW_CATCHER_FRAGMENT)
            self._shadow_catcher_instanced_prog = ctx.program(
                vertex_shader=_MESH_VERTEX_INSTANCED_DEPTH,
                fragment_shader=_MESH_SHADOW_CATCHER_FRAGMENT,
            )
        return self._shadow_catcher_instanced_prog

    def _ensure_shadow_targets(self, ctx) -> bool:
        """Allocate the lazy R32F directional shadow map and its depth FBO."""
        size = max(256, int(self._shadow_map_size))
        if (self._shadow_texture is not None and self._shadow_fbo is not None
                and self._shadow_depth_rb is not None
                and self._shadow_map_allocated_size == size):
            return True
        if self._shadow_texture is not None:
            self._release_shadow_targets()
        try:
            self._shadow_texture = ctx.texture((size, size), 1, dtype="f4")
            self._shadow_texture.filter = (moderngl.LINEAR, moderngl.LINEAR)
            self._shadow_depth_rb = ctx.depth_renderbuffer((size, size))
            self._shadow_fbo = ctx.framebuffer(
                color_attachments=[self._shadow_texture],
                depth_attachment=self._shadow_depth_rb,
            )
            self._shadow_map_allocated_size = size
            return True
        except Exception:
            self._release_shadow_targets()
            return False

    def _release_shadow_targets(self) -> None:
        """Release shadow-map framebuffer resources on target rebuild/cleanup."""
        if self._shadow_fbo is not None:
            try: self._shadow_fbo.release()
            except Exception: pass
            self._shadow_fbo = None
        if self._shadow_depth_rb is not None:
            try: self._shadow_depth_rb.release()
            except Exception: pass
            self._shadow_depth_rb = None
        if self._shadow_texture is not None:
            try: self._shadow_texture.release()
            except Exception: pass
            self._shadow_texture = None
        self._shadow_map_allocated_size = 0

    @staticmethod
    def _shadow_light_view_proj(light_dir: tuple,
                                target: Any) -> np.ndarray:
        """Build a stable orthographic light camera for a directional light."""
        direction = np.asarray(light_dir, dtype=np.float32).reshape(3)
        length = float(np.linalg.norm(direction))
        if length < 1e-6:
            direction = np.array((0.0, 1.0, 0.0), dtype=np.float32)
        else:
            direction = direction / length
        target_vec = np.asarray(target, dtype=np.float32).reshape(3)
        # Light directions point from the surface toward the light in the PBR
        # shader, so the shadow camera sits along +direction and looks at the
        # scene target. The fixed frustum is conservative for Scene3D's default
        # 1000-unit camera range and avoids a per-frame crop/jitter feedback loop.
        light_eye = target_vec + direction * 50.0
        light_view = m3d.look_at(
            light_eye, target_vec, np.array((0.0, 1.0, 0.0), dtype=np.float32))
        light_proj = m3d.ortho(25.0, 1.0, 0.1, 200.0)
        return (light_proj @ light_view).astype(np.float32)

    def _ensure_instance_buffer(self, ctx, grid_x: int, grid_z: int, spacing: float) -> int:
        """按 (grid_x, grid_z, spacing) 构建/缓存实例偏移 buffer，返回实例数。

        offset 网格居中：(i - (gx-1)/2) * spacing, 0, (j - (gz-1)/2) * spacing。
        实例数 = grid_x * grid_z。
        """
        sig = (int(grid_x), int(grid_z), float(spacing))
        if self._instance_sig_cached == sig and self._instance_vbo is not None:
            return self._instance_count_cached
        # 释放旧 buffer
        if self._instance_vbo is not None:
            try:
                self._instance_vbo.release()
            except Exception:
                pass
            self._instance_vbo = None
        offsets = []
        for i in range(grid_x):
            for j in range(grid_z):
                ox = (i - (grid_x - 1) / 2.0) * spacing
                oz = (j - (grid_z - 1) / 2.0) * spacing
                offsets.append([float(ox), 0.0, float(oz)])
        offsets_np = np.asarray(offsets, dtype=np.float32)
        self._instance_vbo = ctx.buffer(offsets_np.tobytes())
        self._instance_count_cached = int(offsets_np.shape[0])
        self._instance_sig_cached = sig
        return self._instance_count_cached

    def _ensure_ibl(self, ctx, use_ibl: bool, hdri_preset: str):
        """确保 IBL 纹理就绪，返回 (irradiance_cube, prefilter_cube, brdf_lut)。

        hdri_preset 语义：
          - "" / "__none__" → 无 IBL（黑色占位，PBR 用解析光源；默认值，稳）
          - "__sky__" → 程序生成的天空渐变（实验性，bake_array 可能在某些
            GL 状态下失败；失败自动降级为黑色占位）
          - 文件名（如 "studio.hdr"）→ hdri/ 目录下该文件
          - 绝对路径 → 直接用
        use_ibl=False 强制走纯黑占位（覆盖 preset）。
        bake 失败降级为黑色占位，不抛异常中断渲染。
        """
        from ..render3d.hdri import HDRIBaker
        if self._hdri_baker is None:
            self._hdri_baker = HDRIBaker()

        # use_ibl=False / 空 / __none__ → 纯黑占位（默认，稳）
        if not use_ibl or not hdri_preset or hdri_preset == "__none__":
            return self._hdri_baker.bake(ctx, "")

        # 程序天空（实验性，需显式选 __sky__）
        if hdri_preset == "__sky__":
            from ..render3d.procedural_hdri import (
                generate_sky_equirect, DEFAULT_SKY_KEY)
            try:
                sky = generate_sky_equirect()
                result = self._hdri_baker.bake_array(ctx, sky, DEFAULT_SKY_KEY)
                # 校验 bake 结果非空（cube 对象可能因 GL 状态损坏为 None）
                if result[0] is None or result[1] is None or result[2] is None:
                    raise RuntimeError("bake_array 返回 None cube map")
                return result
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning(
                    "Scene3D 程序天空 IBL bake 失败，降级为黑色占位: %s", exc)
                return self._hdri_baker.bake(ctx, "")

        # 文件名或绝对路径
        import os
        path = hdri_preset
        if not os.path.isabs(path) and not os.path.isfile(path):
            # 相对路径 → 解析到 hdri/ 目录
            from src.shared.paths.paths import get_hdri_dir
            path = os.path.join(get_hdri_dir(), hdri_preset)
        try:
            result = self._hdri_baker.bake(ctx, path)
            if result[0] is None or result[1] is None or result[2] is None:
                raise RuntimeError("bake 返回 None cube map")
            return result
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "Scene3D IBL bake 失败，降级为黑色占位: %s (path=%s)", exc, path)
            return self._hdri_baker.bake(ctx, "")

    def _ensure_mesh_geometry(self, ctx, primitive: str,
                              instance_enabled: bool = False) -> None:
        """按 primitive 上传/重建几何（VBO/IBO/VAO）。

        primitive 或 instance_enabled 变了才重建；否则复用缓存。
        instance_enabled=True 时 VAO 带 instance buffer + instanced program。
        """
        if (primitive == self._last_primitive
                and self._mesh_vbo is not None
                and self._mesh_instance_enabled == instance_enabled):
            return  # 复用缓存

        # 释放旧几何
        self._release_mesh_geometry()

        mesh: MeshData = get_primitive(primitive)
        self._mesh_data = mesh
        self._mesh_vbo = ctx.buffer(mesh.interleaved().tobytes())
        self._mesh_ibo = ctx.buffer(mesh.indices.tobytes())
        self._mesh_index_count = int(mesh.indices.shape[0])
        # grid 用 LINES，其它用 TRIANGLES
        self._mesh_is_lines = (primitive == "grid")
        self._mesh_instance_enabled = instance_enabled

        if instance_enabled and self._instance_vbo is not None:
            # 实例化：instanced program + instance buffer 作 instanced 属性
            prog = self._ensure_instanced_program(ctx)
            self._mesh_vao = ctx.vertex_array(
                prog,
                [(self._mesh_vbo, '3f 3f 2f', 'in_pos', 'in_normal', 'in_uv'),
                 (self._instance_vbo, '3f /i', 'in_instance_offset')],
                index_buffer=self._mesh_ibo,
            )
        else:
            prog = self._ensure_mesh_program(ctx)
            self._mesh_vao = ctx.vertex_array(
                prog,
                [(self._mesh_vbo, '3f 3f 2f', 'in_pos', 'in_normal', 'in_uv')],
                index_buffer=self._mesh_ibo,
            )
        self._last_primitive = primitive

    def _release_mesh_geometry(self) -> None:
        """释放几何资源（保留 program）。"""
        if self._mesh_vao is not None:
            self._mesh_vao.release()
            self._mesh_vao = None
        if self._mesh_ibo is not None:
            self._mesh_ibo.release()
            self._mesh_ibo = None
        if self._mesh_vbo is not None:
            self._mesh_vbo.release()
            self._mesh_vbo = None
        self._mesh_index_count = 0
        self._mesh_is_lines = False
        self._last_primitive = None
        self._mesh_data = None

    # ── 导入模型（阶段五）──

    def _ensure_model(self, path: str) -> Optional["SceneGraphRoot"]:
        """懒加载 + 缓存导入模型（按 path+mtime 缓存）。

        成功 → 返回 SceneGraphRoot；失败 → 设 _model_error 并返回 None。
        load_model 抛 ModelLoaderUnavailable（库未装/格式不支持/解析失败）会被吞掉，
        节点层 fallback 到 procedural 几何，绝不中断渲染。
        """
        from ..render3d.model_loader import load_model, ModelLoaderUnavailable
        # 缓存命中
        try:
            import os
            mtime = os.path.getmtime(path) if path and os.path.isfile(path) else 0.0
        except OSError:
            mtime = 0.0
        if (self._model_root is not None
                and self._model_path_cached == path
                and abs(self._model_mtime_cached - mtime) < 0.1):
            return self._model_root
        # 路径变化或首次：清旧缓存
        self._release_flat_meshes_gl(self._last_gl_ctx)
        self._model_root = None
        self._model_path_cached = None
        if not path:
            self._model_error = None
            return None
        try:
            root = load_model(path)
            self._model_root = root
            self._model_path_cached = path
            self._model_mtime_cached = mtime
            self._model_error = None
            return root
        except ModelLoaderUnavailable as exc:
            import logging
            logging.getLogger(__name__).warning("Scene3D 模型加载失败，降级 procedural: %s", exc)
            self._model_error = str(exc)
            return None

    def _ensure_flat_meshes_gl(self, ctx, instance_enabled: bool = False) -> bool:
        """为导入模型构建每扁平 mesh 的 GL 资源（VBO/IBO/VAO）。已建则跳过。

        instance_enabled=True 时非蒙皮 mesh 的 VAO 带 instance buffer + instanced
        program（蒙皮 mesh 不参与实例化，仍单实例渲染——实例化 shader 无骨骼属性）。
        返回 True 表示资源就绪可渲染；False 表示无模型/失败（应 fallback procedural）。
        """
        if self._model_root is None:
            return False
        if self._flat_meshes_gl and self._flat_instance_enabled == instance_enabled:
            return True  # 已建且实例化状态一致
        # 状态变（instance_enabled 切换）：重建
        if self._flat_meshes_gl:
            self._release_flat_meshes_gl(ctx)
        self._flat_instance_enabled = instance_enabled
        from ..render3d.scene_graph import flatten_scene
        flat = flatten_scene(
            self._model_root.root,
            source_path=str(self._model_path_cached or ""),
        )
        for fm in flat:
            mesh: MeshData = fm.mesh
            try:
                # 决定能否实例化：非蒙皮 + instance_enabled + instance buffer 就绪
                can_instance = instance_enabled and not mesh.is_skinned and self._instance_vbo is not None
                if mesh.is_skinned:
                    prog = self._ensure_skinned_program(ctx)
                    layout = '3f 3f 2f 4f 4f'
                    attrs = ('in_pos', 'in_normal', 'in_uv', 'in_bone_ids', 'in_bone_weights')
                    is_skinned = True
                elif can_instance:
                    prog = self._ensure_instanced_program(ctx)
                    layout = '3f 3f 2f'
                    attrs = ('in_pos', 'in_normal', 'in_uv')
                    is_skinned = False
                else:
                    prog = self._ensure_mesh_program(ctx)
                    layout = '3f 3f 2f'
                    attrs = ('in_pos', 'in_normal', 'in_uv')
                    is_skinned = False
                # 阶段六：GPU 资源复用 —— 通过 MeshVBOPool 按 content_hash 共享 VBO/IBO。
                # 相同几何（导入模型重复部件）复用同一组 GL 对象，省内存。VAO 仍各自建。
                from ..render3d.mesh_vbo_pool import MeshVBOPool
                _pool = MeshVBOPool.for_context(ctx)
                vbo, ibo, _ic = _pool.acquire(ctx, mesh)
                # P8-0：汇总本帧 VBO 缓存命中（任一 mesh 命中即记 hit；全 miss
                # 显式记 False）。AUD-7309：旧实现只置 True、从不置 False，
                # perf_stats.vbo_cache_hit 无法区分"全 miss 帧"与"未经过该
                # 路径的 cache-hit 帧"（None 语义）。
                self._last_vbo_cache_hit = merge_vbo_cache_hit(
                    self._last_vbo_cache_hit, _pool.last_was_cached)
                contents = [(vbo, layout, *attrs)]
                if can_instance:
                    contents.append((self._instance_vbo, '3f /i', 'in_instance_offset'))
                vao = ctx.vertex_array(prog, contents, index_buffer=ibo)
                self._flat_meshes_gl.append({
                    'vbo': vbo, 'ibo': ibo, 'vao': vao,
                    'vbo_hash': mesh.content_hash(),  # 释放时减池引用计数
                    'mesh': mesh,  # 保留 MeshData 引用（视锥剔除用 local_aabb）
                    'index_count': int(mesh.indices.shape[0]),
                    'is_skinned': is_skinned,
                    'can_instance': can_instance,
                    'world_matrix': fm.world_matrix,
                    'material': fm.material,
                    'skin': fm.skin,
                    'node_name': fm.node_name,
                    'node_id': fm.node_id,
                })
            except Exception:
                import logging
                logging.getLogger(__name__).warning("Scene3D 某扁平 mesh 上传失败，跳过", exc_info=True)
                continue
        return bool(self._flat_meshes_gl)

    def _release_flat_meshes_gl(self, ctx=None) -> None:
        """释放导入模型的扁平 mesh GL 资源。

        VAO 各自释放；VBO/IBO 通过 MeshVBOPool 减引用计数（共享资源 count=0 才真删）。
        ctx 用于查找池；None 时仅释放 VAO（保守，不操作池——避免找不到池误判）。
        """
        from ..render3d.mesh_vbo_pool import MeshVBOPool
        pool = MeshVBOPool.for_context(ctx) if ctx is not None else None
        for m in self._flat_meshes_gl:
            vao = m.get('vao')
            if vao is not None:
                try: vao.release()
                except Exception: pass
            # VBO/IBO 走池引用计数（不直接 release，避免误删共享资源）
            if pool is not None and m.get('vbo_hash') is not None:
                try:
                    pool.release(m['vbo_hash'], m['is_skinned'])
                except Exception:
                    pass
        self._flat_meshes_gl = []
        # 注意：不释放 program（_mesh_prog/_mesh_skinned_prog 在 cleanup 统一释放）

    # ── PBR 共享 uniform 上传（procedural / 导入模型两条路径共用）──

    def _set_shared_uniforms(self, prog, proj, view, cam_pos,
                             dirs_t, cols_t, ints_t, light_count,
                             irradiance_cube, prefilter_cube, brdf_lut,
                             env_int_val, prefilter_max_lod,
                             light_types_t=None, light_pos_t=None,
                             light_range_t=None, light_inner_t=None,
                             light_outer_t=None,
                             light_decay_mode_t=None,
                             light_ies_enabled_t=None,
                             light_ies_profile_t=None,
                             ambient_color=None,
                             ambient_intensity=None,
                             _stats=None) -> None:
        """上传两套程序共享的 PBR uniform（proj/view/cam/lights/IBL）。

        P2 灯光重构：支持多灯光类型（dir/point/spot）+ 全局 ambient。
        新参数（light_types_t 等）为 None 时从实例属性 self._light_*_t 回退，
        这样主渲染路径打包一次后所有调用点无需显式传新参数（向后兼容）。

        u_model/u_albedo/u_metallic/u_roughness 由调用方按 mesh 设置（per-mesh 不同）。

        P8-0：新增可选 _stats 参数（Viewport3DPerfStats）。传入时，本调用设置的
        uniform 数量（约 22 个）累加到 _stats.uniform_sets，用于量化 §7.2 的
        每帧 bind 开销。不传则保持原行为（向后兼容）。

        P8-2：view 级 uniform 缓存。proj/view/lights/IBL 是 view 级常量，
        每 mesh 重复设 22 个 uniform 是明显浪费（多 mesh 场景 N×22 次）。
        缓存 key=(program_id, view_sig)；命中则跳过全量上传（u_model 等 per-mesh
        uniform 仍由调用方设置）。view_sig 由 _do_evaluate 每帧开头算一次存到
        self._shared_uniforms_view_sig，本方法只读不写（保持纯函数性）。
        """
        # P8-2：view 级缓存命中检查。view_sig 由调用方在帧开头设置；
        # 若 (program, view_sig) 已设过本视图，跳过 22 个 uniform 重发。
        _view_sig = self._shared_uniforms_view_sig
        _cache_hit = False
        if _view_sig is not None:
            _cache_key = (id(prog), _view_sig)
            _cached_sig = self._shared_uniforms_cache.get(_cache_key)
            if _cached_sig == _view_sig:
                _cache_hit = True
        if _cache_hit:
            # P8-0：命中时仍统计 0 个 uniform（区别于未命中 22 个），便于量化收益。
            # 注意：texture use() 调用仍需保证（cubemap 可能被其他 path 解绑），
            # 但 uniform value 赋值跳过。这里保守地仍做 use() 以防 binding 竞态。
            irradiance_cube.use(location=0)
            prefilter_cube.use(location=1)
            brdf_lut.use(location=2)
            return
        import math
        prog['u_proj'].value = m3d.matrix_to_tuple(proj)
        prog['u_view'].value = m3d.matrix_to_tuple(view)
        prog['u_cam_pos'].value = cam_pos
        prog['u_light_dir'].value = dirs_t
        prog['u_light_color'].value = cols_t
        prog['u_light_intensity'].value = ints_t
        prog['u_light_count'].value = light_count
        # P2 新增：多灯光类型 uniform（从实例属性回退，保持调用点简洁）
        if light_types_t is None:
            light_types_t = getattr(self, '_light_types_t', None) or (0, 0, 0, 0)
        if light_pos_t is None:
            light_pos_t = getattr(self, '_light_pos_t', None) or ((0.0, 0.0, 0.0),) * 4
        if light_range_t is None:
            light_range_t = getattr(self, '_light_range_t', None) or (0.0, 0.0, 0.0, 0.0)
        if light_inner_t is None:
            light_inner_t = getattr(self, '_light_inner_t', None) or \
                (math.cos(math.radians(30.0)),) * 4
        if light_outer_t is None:
            light_outer_t = getattr(self, '_light_outer_t', None) or \
                (math.cos(math.radians(45.0)),) * 4
        if light_decay_mode_t is None:
            light_decay_mode_t = getattr(self, '_light_decay_mode_t', None) or (2, 2, 2, 2)
        if light_ies_enabled_t is None:
            light_ies_enabled_t = getattr(self, '_light_ies_enabled_t', None) or (0, 0, 0, 0)
        if light_ies_profile_t is None:
            light_ies_profile_t = getattr(self, '_light_ies_profile_t', None) or ((1.0, 1.0, 1.0, 1.0),) * 4
        if ambient_color is None:
            ambient_color = getattr(self, '_ambient_color', (0.0, 0.0, 0.0))
        if ambient_intensity is None:
            ambient_intensity = getattr(self, '_ambient_intensity', 0.0)
        prog['u_light_type'].value = light_types_t
        prog['u_light_pos'].value = light_pos_t
        prog['u_light_range'].value = light_range_t
        prog['u_light_inner'].value = light_inner_t
        prog['u_light_outer'].value = light_outer_t
        prog['u_light_decay_mode'].value = light_decay_mode_t
        prog['u_light_ies_enabled'].value = light_ies_enabled_t
        prog['u_light_ies_profile'].value = light_ies_profile_t
        prog['u_ambient_color'].value = ambient_color
        prog['u_ambient_intensity'].value = ambient_intensity
        # P3 曝光（从实例属性读，由 cam_payload 应用时设置）
        prog['u_exposure'].value = getattr(self, '_cam_exposure', 0.0)
        irradiance_cube.use(location=0)
        prefilter_cube.use(location=1)
        brdf_lut.use(location=2)
        prog['u_irradiance_map'].value = 0
        prog['u_prefilter_map'].value = 1
        prog['u_brdf_lut'].value = 2
        prog['u_env_intensity'].value = env_int_val
        prog['u_prefilter_max_lod'].value = prefilter_max_lod
        # P8-0：统计本调用设置的 shared uniform 数量（22 个赋值 + 3 个 texture use）。
        # 用于量化 §7.2 的 per-mesh 重复上传开销，为 P8-2 的 view 级缓存提供基线。
        if _stats is not None:
            _stats.uniform_sets += 22
        # P8-2：标记 (program, view_sig) 已设过本视图，后续 mesh 命中跳过。
        if _view_sig is not None:
            _cache_key = (id(prog), _view_sig)
            self._shared_uniforms_cache[_cache_key] = _view_sig

    def _sample_animation_worlds(self, anim_enabled: bool,
                                 anim_time: float, anim_speed: float) -> dict:
        """采样动画，返回 {node_name: world_matrix}（无动画则返回空 dict）。

        约定：取第一个 animation clip；时间按 anim_time*speed 循环。
        未受动画影响的节点不在结果里（调用方 fallback 到静态 world_matrix）。
        """
        if not anim_enabled or self._model_root is None:
            return {}
        clips = self._model_root.animations
        if not clips:
            return {}
        from ..render3d.scene_graph import sample_animation, apply_animation_to_scene
        clip = clips[0]
        t = anim_time * anim_speed
        sampled = sample_animation(clip, t)
        return apply_animation_to_scene(self._model_root.root, sampled)

    def _bind_material_textures(self, ctx, prog, mat, texture_overrides: dict,
                                material_params: dict, model_dir: str) -> dict:
        """绑定材质贴图到 sampler 并返回 has_map 标志字典。

        Model3D 材质升级（计划 "Implementation route #2/#4/#5"）：把导入材质的
        TextureDescriptor 与外部端口 texture_overrides 绑到对应 sampler，置
        has_map=1。material_mode 决定 override 与 imported 的优先级：
            - override（默认策略）：external override 优先，缺失才用 imported
            - imported：只用导入材质贴图（忽略 override）
            - mix：二者都绑（override 覆盖 imported 的同名 slot）

        texture_overrides 是 {port_name: glo}（外部 TextureResult 的 texture_id，
        已是 GPU 资源，直接 glBindTexture）；导入材质 TextureDescriptor 经
        _material_tex_loader 上传为 GL 纹理（首次按 path+mtime 缓存）。

        Args:
            ctx: moderngl.Context。
            prog: 当前 mesh 的 program（含 u_has_*_map / sampler uniform）。
            mat: MaterialData（导入材质，含 *_tex TextureDescriptor），可 None。
            texture_overrides: {port_name: glo}。
            material_params: material_mode / texture_filter / texture_wrap 等。
            model_dir: 模型文件目录（解析相对贴图路径用）。

        Returns:
            {"base_color": bool, "normal": bool, "orm": bool, "ao": bool,
             "emissive": bool} —— 各 slot 是否绑定了纹理（置 has_map 用）。
        """
        from OpenGL import GL as gl
        from ..render3d.material_texture_loader import (
            MATERIAL_SAMPLER_LOCATIONS, apply_filter_wrap)

        has_map = {"base_color": False, "normal": False, "orm": False,
                   "ao": False, "emissive": False}
        mode = str(material_params.get("material_mode", "imported"))
        tex_filter = str(material_params.get("texture_filter", "mipmap"))
        tex_wrap = str(material_params.get("texture_wrap", "repeat"))

        # 延迟创建 loader（首次绑定材质贴图时）
        if self._material_tex_loader is None:
            from ..render3d.material_texture_loader import MaterialTextureLoader
            self._material_tex_loader = MaterialTextureLoader()

        # override 端口 → slot 映射（metallic/roughness 并入 orm slot）。
        # mask_in 当前不直接绑材质（用于选择性覆盖，留作后续 mix 模式用）。
        override_slot = {
            "base_color_in": "base_color",
            "normal_in": "normal",
            "metallic_in": "orm",      # metallic/roughness 都走 ORM slot 的通道
            "roughness_in": "orm",
            "ao_in": "ao",
            "emissive_in": "emissive",
        }

        # 导入材质 TextureDescriptor → slot 映射
        imported_slots = []
        if mat is not None:
            imported_slots = [
                ("base_color", getattr(mat, "base_color_tex", None)),
                ("normal", getattr(mat, "normal_tex", None)),
                ("orm", getattr(mat, "orm_tex", None)),
                ("ao", getattr(mat, "ao_tex", None)),
                ("emissive", getattr(mat, "emissive_tex", None)),
            ]

        def _bind_glo(slot: str, glo: int) -> None:
            """用 OpenGL 直接绑外部 glo 到 sampler location。"""
            loc = MATERIAL_SAMPLER_LOCATIONS.get(slot)
            if loc is None:
                return
            try:
                gl.glActiveTexture(gl.GL_TEXTURE0 + loc)
                gl.glBindTexture(gl.GL_TEXTURE_2D, int(glo))
                has_map[slot] = True
                self._perf_stats.texture_binds += 1
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    "材质 override 绑定失败 slot=%s glo=%s", slot, glo, exc_info=True)

        def _bind_descriptor(slot: str, descriptor) -> None:
            """上传/复用导入材质贴图并绑到 sampler location。"""
            if descriptor is None or not getattr(descriptor, "path", ""):
                return
            loc = MATERIAL_SAMPLER_LOCATIONS.get(slot)
            if loc is None:
                return
            tex = self._material_tex_loader.get_or_upload(ctx, descriptor, model_dir)
            if tex is None:
                return
            # P0-4：在 get_or_upload 之后读 was_last_cached（之前错放在前面，
            # 读到的是上一次调用的状态，导致 texture_uploads 计数延迟一帧）。
            _was_cached = self._material_tex_loader.was_last_cached
            try:
                # P3-3：filter/wrap 只在变化时设置（相同则跳过，省 glTexParameteri）。
                if not self._material_tex_loader.is_filter_wrap_applied(
                        tex, tex_filter, tex_wrap):
                    apply_filter_wrap(tex, tex_filter, tex_wrap)
                    self._material_tex_loader.mark_filter_wrap_applied(
                        tex, tex_filter, tex_wrap)
                gl.glActiveTexture(gl.GL_TEXTURE0 + loc)
                gl.glBindTexture(gl.GL_TEXTURE_2D, tex.glo)
                has_map[slot] = True
                self._perf_stats.texture_binds += 1
                if not _was_cached:
                    self._perf_stats.texture_uploads += 1
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    "材质贴图绑定失败 slot=%s", slot, exc_info=True)

        # 按 material_mode 决定绑定顺序（override 优先 / imported 优先 / 都绑）
        if mode == "imported":
            # 只用导入材质贴图（忽略 override）
            for slot, desc in imported_slots:
                _bind_descriptor(slot, desc)
        else:
            # override（默认）或 mix：override 优先绑，缺失则补 imported
            for port_name, glo in texture_overrides.items():
                slot = override_slot.get(port_name)
                if slot is not None and glo and glo > 0:
                    _bind_glo(slot, int(glo))
            if mode != "override":
                # mix / 回退：补齐 override 未覆盖的 slot（用导入材质）
                for slot, desc in imported_slots:
                    if not has_map[slot]:
                        _bind_descriptor(slot, desc)
            else:
                # override 模式：override 没连的 slot 也尝试 imported（更实用）
                for slot, desc in imported_slots:
                    if not has_map[slot]:
                        _bind_descriptor(slot, desc)

        return has_map

    def _unbind_material_textures(self) -> None:
        """渲染后解绑材质 sampler（避免状态泄漏到下一个 mesh / IBL）。"""
        from OpenGL import GL as gl
        from ..render3d.material_texture_loader import MATERIAL_SAMPLER_LOCATIONS
        for loc in MATERIAL_SAMPLER_LOCATIONS.values():
            try:
                gl.glActiveTexture(gl.GL_TEXTURE0 + loc)
                gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
                # P0-4：unbind 也算 texture bind（GL state change，诊断用）。
                self._perf_stats.texture_binds += 1
            except Exception:
                pass

    def _bind_null_material_samplers(self, prog) -> None:
        """把材质 sampler2D uniform 指向"空"纹理单元（不绑任何 2D 纹理）。

        关键修复：PBR fragment shader 声明了 5 个 ``sampler2D``（base_color/normal/
        orm/ao/emissive_map）。若不显式给这些 sampler uniform 赋值，OpenGL 默认
        指向 **纹理单元 0**——而单元 0 绑的是 IBL 的 ``u_irradiance_map``
        （samplerCube）。同一单元上 sampler2D 与 samplerCube 类型不一致，会让
        draw 报 ``GL_INVALID_OPERATION``（即便 shader 因 ``u_has_*_map==0`` 不采样，
        驱动仍在 program validation 阶段拒绝）→ procedural / Geo3D 几何完全不画
        （Scene3D 只显坐标轴）。

        解决：把这些 sampler uniform 指到空绑的单元 3-7（与 IBL 的 0-2 不冲突），
        并确保那些单元没绑 2D 纹理。has_map=0 时 shader 不读，类型一致不报错。

        在 procedural / Geo3D 路径（无材质贴图）渲染前调用；模型路径走
        ``_bind_material_textures``（已绑真实纹理）。

        Milestone D / P3-4：sampler uniform → 单元的赋值每个 program 只做一次
        （program 对象持久保留 sampler 绑定）。纹理单元的 glBindTexture(0) 仍每次
        调用（_bind_material_textures/_unbind 会改单元状态，需重置）。命中时省掉
        5 次 glUniform1i（procedural 多 mesh 场景累积可观）。
        """
        from OpenGL import GL as gl
        from ..render3d.material_texture_loader import MATERIAL_SAMPLER_LOCATIONS
        for loc in MATERIAL_SAMPLER_LOCATIONS.values():
            try:
                gl.glActiveTexture(gl.GL_TEXTURE0 + loc)
                gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
                # P0-4：null bind 也算 texture bind（GL state change，诊断用）。
                self._perf_stats.texture_binds += 1
            except Exception:
                pass
        # P3-4：sampler uniform → 对应单元（base_color=3 ... emissive=7），每 program 一次。
        if prog not in self._sampler_loc_set_progs:
            for slot, loc in MATERIAL_SAMPLER_LOCATIONS.items():
                uname = f"u_{slot}_map"
                try:
                    prog[uname].value = loc
                except KeyError:
                    # AUD-7310: 收窄为 KeyError——moderngl 对不存在的 uniform
                    # 抛 KeyError；(KeyError, Exception) 等价裸 except，
                    # 会吞掉非预期错误。缺失 sampler uniform 属预期探测。
                    pass
            self._sampler_loc_set_progs.add(prog)

    @staticmethod
    def _sort_meshes_by_material(flat_meshes: list) -> list:
        """Milestone D / P3-6：按材质排序 mesh（不透明），透明 mesh 保持原序。

        同材质 mesh 相邻 → 材质状态缓存命中率提升（连续同材质跳过 rebind）。
        透明 mesh（material 有 alpha<1.0 的 baseColor）不参与排序——透明物体需要
        painter's order（按深度从后向前），重排会破坏正确的 alpha 混合（§7.4 风险1）。

        策略：稳定排序，按 material 的 id() 分组。不透明在前（按材质分组），透明
        在后（保持原相对顺序）。全不透明或全透明时退化为原序（稳定排序无副作用）。
        """
        if len(flat_meshes) <= 2:
            return flat_meshes  # 太少，排序无收益
        def _is_transparent(fm) -> bool:
            mat = fm.get('material') if isinstance(fm, dict) else None
            if mat is None:
                return False
            # P8-0：MaterialData 新增 alpha 字段（默认 1.0）。<0.999 视为透明，
            # 需保持 painter's order（从后向前），不参与材质分组排序。
            alpha = getattr(mat, "alpha", 1.0)
            try:
                return float(alpha) < 0.999
            except (TypeError, ValueError):
                return False
        # 分离透明/不透明（透明保持原序，不透明按材质分组）
        opaque = []
        transparent = []
        for i, fm in enumerate(flat_meshes):
            if _is_transparent(fm):
                transparent.append((i, fm))
            else:
                opaque.append((i, fm))
        if not opaque or len(opaque) == len(flat_meshes):
            # 全透明 → 原序；或全不透明 → 直接排序
            if not opaque:
                return flat_meshes
        # 不透明按材质 id 稳定排序（同材质相邻），保持原相对顺序作 tiebreaker
        opaque.sort(key=lambda pair: id(pair[1].get('material')))
        # 重组：不透明（已按材质分组）+ 透明（原序）。两者用原始 index 保证稳定。
        return [fm for _, fm in opaque] + [fm for _, fm in transparent]

    @staticmethod
    def _material_with_override(material: Any, mesh_desc: dict,
                                override_map: dict) -> Any:
        """Return the effective material for one mesh without mutating import data."""
        if material is None or not override_map:
            return material
        from ..render3d.material_model import (
            apply_override_to_material_data, material_id_for,
        )
        material_key = str(mesh_desc.get("material_id", "") or material_id_for(
            material,
            str(mesh_desc.get("node_id", "") or mesh_desc.get("node_name", "")),
        ))
        override = override_map.get(material_key)
        if override is None:
            material_name = str(getattr(material, "name", "") or "")
            if material_name:
                override = next(
                    (item for item in override_map.values()
                     if str(getattr(item, "target_material_name", "") or "")
                     == material_name),
                    None,
                )
        if override is None or override.is_identity():
            return material
        return apply_override_to_material_data(material, override)

    @staticmethod
    def _material_override_map(payload: Any) -> dict:
        """Decode a ModelPayload's JSON-safe material override map."""
        raw = getattr(payload, "material_overrides", None) if payload is not None else None
        if not isinstance(raw, dict):
            return {}
        from ..render3d.material_model import material_override_from_dict
        return {
            str(key): material_override_from_dict(value)
            for key, value in raw.items()
            if isinstance(value, dict)
        }

    @staticmethod
    def _is_beauty_visible(render_flags: Any) -> bool:
        """Whether an object contributes colour to the beauty pass.

        Holdouts are handled by the hardware-depth prepass; matte and shadow
        catcher objects use their own auxiliary/transparent paths.
        """
        if render_flags is None:
            return True
        return bool(
            getattr(render_flags, "visible_to_camera", True)
            and not getattr(render_flags, "holdout", False)
            and not getattr(render_flags, "shadow_catcher", False)
            and not getattr(render_flags, "matte_object", False)
        )

    @staticmethod
    def _has_holdout(render_flags: Any) -> bool:
        return bool(render_flags is not None
                    and getattr(render_flags, "holdout", False))

    def _get_lod_vao(self, ctx, prog, lod_mesh) -> tuple:
        """Milestone F / P5-4：获取 LOD mesh 的 VAO（content_hash 缓存）。

        LOD mesh 由 Model3D 生成（顶点聚类简化），Scene3D 用它做导航态低面数渲染。
        VAO 通过 MeshVBOPool 共享 VBO/IBO（content_hash 复用），首次构建后缓存。
        失败返回 (None, 0)（调用方回退到原 mesh 的 vao）。

        Returns:
            (vao, triangle_count)。triangle_count = lod_mesh.indices/3。
        """
        try:
            content_hash = lod_mesh.content_hash()
        except Exception:
            return (None, 0)
        cached = self._lod_vao_cache.get(content_hash)
        if cached is not None:
            return cached
        try:
            from ..render3d.mesh_vbo_pool import MeshVBOPool
            _pool = MeshVBOPool.for_context(ctx)
            vbo, ibo, _ic = _pool.acquire(ctx, lod_mesh)
            layout = '3f 3f 2f'
            attrs = ('in_pos', 'in_normal', 'in_uv')
            contents = [(vbo, layout, *attrs)]
            vao = ctx.vertex_array(prog, contents, index_buffer=ibo)
            tri_count = int(lod_mesh.indices.shape[0]) // 3
            self._lod_vao_cache[content_hash] = (vao, tri_count)
            return (vao, tri_count)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "Scene3D LOD VAO 构建失败，回退原 mesh", exc_info=True)
            return (None, 0)

    def _release_lod_vao_cache(self) -> None:
        """释放 LOD VAO 缓存（AUD-7302 fix）。

        旧代码全生命周期从不清理 ``_lod_vao_cache``：VAO 永不释放，
        ``MeshVBOPool.acquire`` 抬高的引用计数也永不回退——用户多次调整
        LOD 档位（不同 content_hash）时缓存无上限增长，节点删除时这批
        GL 对象整体泄漏。渲染目标重建（尺寸/MSAA/AOV 变化）与 cleanup
        时统一释放；下次导航帧按需懒重建。
        """
        if not self._lod_vao_cache:
            return
        pool = None
        ctx = getattr(self, "_last_gl_ctx", None)
        if ctx is not None:
            try:
                from ..render3d.mesh_vbo_pool import MeshVBOPool
                pool = MeshVBOPool.for_context(ctx)
            except Exception:
                pool = None
        for content_hash, entry in self._lod_vao_cache.items():
            vao = entry[0] if isinstance(entry, tuple) else None
            if vao is not None:
                try:
                    vao.release()
                except Exception:
                    pass
            # LOD VAO 用 acquire(ctx, lod_mesh)（无 has_tangent）取得池化
            # VBO/IBO——对称地按 (content_hash, is_skinned=False) 减引用。
            if pool is not None:
                try:
                    pool.release(str(content_hash), False)
                except Exception:
                    pass
        self._lod_vao_cache = {}

    def _upload_bone_matrices(self, prog: Any, mesh_gl: dict,
                              anim_worlds: dict,
                              uniform_name: str = 'u_bone_matrices') -> None:
        """上传骨骼世界矩阵数组到指定的骨骼 uniform 数组。

        每骨骼矩阵 = joint_world @ inverse_bind。
        joint_world：动画时从 anim_worlds 查（按骨骼名）；否则用 inverse_bind 的逆
        （即绑定姿态，静态）。最多 64 根，超出截断。
        """
        from ..render3d.scene_graph import MAX_BONES
        from ..render3d.skinning import compute_skin_joint_matrices
        skin = mesh_gl.get('skin')
        if skin is None:
            return
        matrices = compute_skin_joint_matrices(skin, anim_worlds)
        # ModernGL accepts a tuple of flattened row-major matrices here, as in
        # the previous implementation.  The math itself is shared with the
        # Deformer3D CPU reference path above.
        prog[uniform_name].value = tuple(
            tuple(matrix.reshape(-1).tolist()) for matrix in matrices[:MAX_BONES]
        )

    @staticmethod
    def _instance_offset_range(grid_x: int, grid_z: int, spacing: float):
        """返回实例 offset 的 min/max 范围（用于实例化视锥剔除的并集 AABB）。

        offset 网格居中：(i-(gx-1)/2)*spacing, 0, (j-(gz-1)/2)*spacing。
        """
        ox_max = (grid_x - 1) / 2.0 * spacing
        oz_max = (grid_z - 1) / 2.0 * spacing
        return (-ox_max, 0.0, -oz_max), (ox_max, 0.0, oz_max)

    def _render_depth_pass(self, ctx, w, h, proj, view, model, eye,
                           import_ok, instance_enabled, instance_count,
                           anim_enabled, anim_time, anim_speed,
                           flat_meshes_src=None, anim_worlds_src=None,
                           hidden_mesh_ids=None, geo_payloads_src=None,
                           model_render_flags=None, target_fbo=None,
                           clear_target=True, only_holdout=False,
                           cam_near: float = 0.1, cam_far: float = 1000.0) -> None:
        """第二遍：把线性深度写入 R32F _depth_texture（供 deep_merge 消费）。

        用 _MESH_VERTEX（复用 v_world_pos）+ _MESH_DEPTH_WRITE_FRAGMENT。
        每 mesh 重画到 depth-only FBO；保留 DEPTH_TEST 保证遮挡正确。
        clear 到 1.0（远）。不参与视锥剔除（深度必须完整，否则边缘合成错）。
        实例化 mesh 在此 pass 也按 instances 重画。

        阶段七：flat_meshes_src / anim_worlds_src 由调用方传入（Model3D payload
        或自身 _flat_meshes_gl）。None 时退化为读 self._flat_meshes_gl + 重新采样。

        AUD-7304 fix: ``cam_near``/``cam_far`` 来自本帧实际投影（外接 Camera3D
        的可调裁剪面，payload 已携带；内置相机用同一常量来源）。旧代码硬编码
        0.1/1000，用户改裁剪面后 _MESH_DEPTH_WRITE_FRAGMENT 的线性化整体错误。
        """
        if target_fbo is None and (self._depth_texture is None or self._depth_fbo is None):
            return
        prog = self._ensure_depth_write_program(ctx)
        (target_fbo or self._depth_fbo).use()
        ctx.viewport = (0, 0, w, h)
        # clear 到 1.0（远，与 deep_merge clear 约定一致）
        if clear_target:
            ctx.clear(1.0)
        ctx.enable(moderngl.DEPTH_TEST)
        color_mask_changed = False
        if only_holdout:
            # The holdout prepass writes only the hardware depth buffer.  Its
            # colour attachment must remain untouched so already-cleared
            # background pixels stay transparent/opaque as configured.
            try:
                ctx.color_mask = (False, False, False, False)
                color_mask_changed = True
            except Exception:
                color_mask_changed = False
        try:
            prog['u_proj'].value = m3d.matrix_to_tuple(proj)
            prog['u_view'].value = m3d.matrix_to_tuple(view)
            prog['u_cam_pos'].value = (float(eye[0]), float(eye[1]), float(eye[2]))
            # AUD-7304: near/far 跟随本帧投影（外接 Camera3D 可调，内置相机
            # 为 m3d.perspective 的同一常量），不再硬编码 0.1/1000。
            prog['u_near'].value = float(cam_near)
            prog['u_far'].value = float(cam_far)
            flat_meshes = flat_meshes_src if flat_meshes_src is not None else self._flat_meshes_gl
            if import_ok and flat_meshes:
                anim_worlds = anim_worlds_src
                if anim_worlds is None:
                    anim_worlds = self._sample_animation_worlds(anim_enabled, anim_time, anim_speed)
                hidden_ids = {str(value) for value in (hidden_mesh_ids or ())}
                for m in flat_meshes:
                    if str(m.get('node_id', '') or '') in hidden_ids:
                        continue
                    if only_holdout and not getattr(model_render_flags, "holdout", False):
                        continue
                    if (not only_holdout and model_render_flags is not None
                            and (getattr(model_render_flags, "shadow_catcher", False)
                                 or getattr(model_render_flags, "matte_object", False)
                                 or not getattr(model_render_flags, "visible_to_camera", True))):
                        continue
                    mesh = m['mesh']
                    is_skinned = bool(mesh.is_skinned)
                    write_prog = (
                        self._ensure_depth_write_skinned_program(ctx)
                        if is_skinned else prog
                    )
                    vao_key = (
                        f"{m['node_name']}:skinned:{id(mesh)}"
                        if is_skinned else f"{m['node_name']}:{id(mesh)}"
                    )
                    # Keep depth geometry on the same skinning path as beauty
                    # so animated silhouettes remain occlusion-correct.
                    vao = self._get_depth_write_vao(
                        ctx, mesh, vao_key, program=write_prog)
                    if vao is None:
                        continue
                    node_name = m['node_name']
                    wm = anim_worlds.get(node_name, m['world_matrix'])
                    effective_model = (model @ wm).astype(np.float32)
                    write_prog['u_proj'].value = m3d.matrix_to_tuple(proj)
                    write_prog['u_view'].value = m3d.matrix_to_tuple(view)
                    write_prog['u_cam_pos'].value = (float(eye[0]), float(eye[1]), float(eye[2]))
                    write_prog['u_near'].value = float(cam_near)
                    write_prog['u_far'].value = float(cam_far)
                    write_prog['u_model'].value = m3d.matrix_to_tuple(effective_model)
                    if is_skinned:
                        self._upload_bone_matrices(write_prog, m, anim_worlds)
                    if instance_enabled and m.get('can_instance'):
                        vao.render(moderngl.TRIANGLES, instances=instance_count)
                    else:
                        vao.render(moderngl.TRIANGLES)
            if geo_payloads_src:
                for geo_index, geo_payload in enumerate(geo_payloads_src):
                    flags = getattr(geo_payload, "render_flags", None)
                    if only_holdout and not getattr(flags, "holdout", False):
                        continue
                    if (not only_holdout and flags is not None
                            and (getattr(flags, "shadow_catcher", False)
                                 or getattr(flags, "matte_object", False)
                                 or not getattr(flags, "visible_to_camera", True))):
                        continue
                    mesh = getattr(geo_payload, "mesh", None)
                    model_matrix = getattr(geo_payload, "model_matrix", None)
                    if mesh is None or model_matrix is None:
                        continue
                    is_instanced = bool(getattr(geo_payload, "instance_enabled", False))
                    write_prog = (self._ensure_depth_write_instanced_program(ctx)
                                  if is_instanced else self._ensure_depth_write_program(ctx))
                    vao = self._get_depth_write_vao(
                        ctx, mesh,
                        f"geo:{geo_index}:{'instanced' if is_instanced else 'plain'}:{id(mesh)}",
                        program=write_prog,
                        instance_vbo=(getattr(geo_payload, "instance_vbo", None)
                                      if is_instanced else None))
                    if vao is None:
                        continue
                    write_prog['u_proj'].value = m3d.matrix_to_tuple(proj)
                    write_prog['u_view'].value = m3d.matrix_to_tuple(view)
                    write_prog['u_cam_pos'].value = (float(eye[0]), float(eye[1]), float(eye[2]))
                    write_prog['u_near'].value = float(cam_near)
                    write_prog['u_far'].value = float(cam_far)
                    write_prog['u_model'].value = m3d.matrix_to_tuple(model_matrix)
                    draw_mode = (moderngl.LINES if getattr(geo_payload, "is_lines", False)
                                 else moderngl.TRIANGLES)
                    if is_instanced and draw_mode != moderngl.LINES:
                        vao.render(draw_mode, instances=max(1, int(
                            getattr(geo_payload, "instance_count", 1))))
                    else:
                        vao.render(draw_mode)
            elif not only_holdout and not (import_ok and flat_meshes):
                # procedural：用与主 mesh 同几何的 depth-write VAO
                vao = self._get_depth_write_vao(ctx, None, '__procedural__')
                if vao is not None:
                    prog['u_model'].value = m3d.matrix_to_tuple(model)
                    mode = moderngl.LINES if self._mesh_is_lines else moderngl.TRIANGLES
                    if instance_enabled and self._mesh_instance_enabled and not self._mesh_is_lines:
                        vao.render(mode, instances=instance_count)
                    else:
                        vao.render(mode)
        finally:
            ctx.disable(moderngl.DEPTH_TEST)
            if color_mask_changed:
                try:
                    ctx.color_mask = (True, True, True, True)
                except Exception:
                    pass

    def _get_depth_write_vao(self, ctx, mesh, key: str, program=None,
                             instance_vbo=None):
        """懒建/缓存 depth-write VAO（绑定 _MESH_VERTEX + _MESH_DEPTH_WRITE_FRAGMENT）。

        procedural（mesh=None）用主 procedural 几何；导入 mesh 用各自几何。
        蒙皮 mesh 使用与 beauty 相同的骨骼属性布局。
        """
        if key in self._depth_write_vaos:
            return self._depth_write_vaos[key]
        prog = program or self._ensure_depth_write_program(ctx)
        owned_buffers = []
        try:
            if instance_vbo is not None:
                source_mesh = mesh if mesh is not None else self._mesh_data
                if source_mesh is None:
                    return None
                vbo = ctx.buffer(np.asarray(
                    source_mesh.vertices, dtype=np.float32).tobytes())
                owned_buffers.append(vbo)
                if mesh is None:
                    if self._mesh_ibo is None:
                        return None
                    ibo = self._mesh_ibo
                else:
                    ibo = ctx.buffer(mesh.indices.tobytes())
                    owned_buffers.append(ibo)
                contents = [(vbo, '3f', 'in_pos'),
                            (instance_vbo, '3f /i', 'in_instance_offset')]
                vao = ctx.vertex_array(prog, contents, index_buffer=ibo)
            elif mesh is None:
                # procedural：用现有 _mesh_vbo/_mesh_ibo（已建）
                if self._mesh_vbo is None or self._mesh_ibo is None:
                    return None
                contents = [(
                    self._mesh_vbo, '3f 3f 2f',
                    'in_pos', 'in_normal', 'in_uv')]
                vao = ctx.vertex_array(prog, contents, index_buffer=self._mesh_ibo)
            else:
                if mesh.is_skinned:
                    data = mesh.interleaved_skinned().tobytes()
                    layout = '3f 3f 2f 4f 4f'
                    attrs = ('in_pos', 'in_normal', 'in_uv',
                             'in_bone_ids', 'in_bone_weights')
                else:
                    data = mesh.interleaved().tobytes()
                    layout = '3f 3f 2f'
                    attrs = ('in_pos', 'in_normal', 'in_uv')
                vbo = ctx.buffer(data)
                ibo = ctx.buffer(mesh.indices.tobytes())
                owned_buffers = [vbo, ibo]
                contents = [(vbo, layout, *attrs)]
                vao = ctx.vertex_array(prog, contents, index_buffer=ibo)
            self._depth_write_vaos[key] = vao
            if owned_buffers:
                self._depth_write_buffers[key] = owned_buffers
            return vao
        except Exception:
            for buffer in owned_buffers:
                try: buffer.release()
                except Exception: pass
            return None

    def _get_shadow_write_vao(self, ctx: Any, mesh: Any, key: str,
                              program: Any,
                              instance_vbo: Optional[Any] = None) -> Optional[Any]:
        """Build a VAO for the shadow-map or shadow-catcher shader variant."""
        if key in self._shadow_write_vaos:
            return self._shadow_write_vaos[key]
        owned_buffers = []
        try:
            if instance_vbo is not None:
                # The instanced auxiliary vertex shaders only consume position
                # plus the per-instance offset. Repack to a compact VBO so a
                # VAO never references inactive normal/UV attributes.
                source_mesh = mesh if mesh is not None else self._mesh_data
                if source_mesh is None or (mesh is None and self._mesh_ibo is None):
                    return None
                vbo = ctx.buffer(np.asarray(
                    source_mesh.vertices, dtype=np.float32).tobytes())
                owned_buffers = [vbo]
                ibo = (self._mesh_ibo if mesh is None
                       else ctx.buffer(mesh.indices.tobytes()))
                if mesh is not None:
                    owned_buffers.append(ibo)
                layout = "3f"
                attrs = ("in_pos",)
            elif mesh is None:
                if self._mesh_vbo is None or self._mesh_ibo is None:
                    return None
                vbo = self._mesh_vbo
                ibo = self._mesh_ibo
                layout = "3f 3f 2f"
                attrs = ("in_pos", "in_normal", "in_uv")
            elif getattr(mesh, "is_skinned", False):
                vbo = ctx.buffer(mesh.interleaved_skinned().tobytes())
                ibo = ctx.buffer(mesh.indices.tobytes())
                owned_buffers = [vbo, ibo]
                layout = "3f 3f 2f 4f 4f"
                attrs = ("in_pos", "in_normal", "in_uv",
                         "in_bone_ids", "in_bone_weights")
            else:
                vbo = ctx.buffer(mesh.interleaved().tobytes())
                ibo = ctx.buffer(mesh.indices.tobytes())
                owned_buffers = [vbo, ibo]
                layout = "3f 3f 2f"
                attrs = ("in_pos", "in_normal", "in_uv")
            contents = [(vbo, layout, *attrs)]
            if instance_vbo is not None:
                contents.append((instance_vbo, "3f /i", "in_instance_offset"))
            vao = ctx.vertex_array(program, contents, index_buffer=ibo)
            self._shadow_write_vaos[key] = vao
            if owned_buffers:
                self._shadow_write_buffers[key] = owned_buffers
            return vao
        except Exception:
            for buffer in owned_buffers:
                try: buffer.release()
                except Exception: pass
            return None

    @staticmethod
    def _can_cast_shadow(render_flags: Any) -> bool:
        """Return whether an object should be written into the shadow map."""
        if render_flags is None:
            return True
        return bool(
            getattr(render_flags, "visible_to_shadow", True)
            and not getattr(render_flags, "shadow_catcher", False)
            and not getattr(render_flags, "matte_object", False)
        )

    def _render_shadow_map(self, ctx: Any, proj: np.ndarray,
                           model: np.ndarray, cam_target: Any,
                           light_dir: tuple, import_ok: bool,
                           flat_meshes_src: Optional[list],
                           anim_worlds_src: Optional[dict],
                           model_render_flags: Any,
                           geo_payloads_src: Optional[list],
                           hidden_mesh_ids: Optional[tuple] = None,
                           anim_enabled: bool = False,
                           anim_time: float = 0.0,
                           anim_speed: float = 1.0
                           ) -> Optional[np.ndarray]:
        """Render visible casters into a directional shadow map."""
        if not self._ensure_shadow_targets(ctx) or self._shadow_fbo is None:
            return None
        light_view_proj = self._shadow_light_view_proj(light_dir, cam_target)
        self._shadow_fbo.use()
        size = max(256, int(self._shadow_map_size))
        ctx.viewport = (0, 0, size, size)
        ctx.clear(1.0, depth=1.0)
        ctx.enable(moderngl.DEPTH_TEST)
        try:
            flat_meshes = (flat_meshes_src
                           if flat_meshes_src is not None
                           else self._flat_meshes_gl)
            # AUD-7305 fix: 自身导入路径（无 Model3D payload 的 anim_worlds）
            # 旧代码回退 {} → 全部 mesh 用绑定姿态画阴影，动画播放时阴影贴在
            # 旧位置。与 _render_depth_pass 一致按本帧动画采样。
            anim_worlds = anim_worlds_src
            if anim_worlds is None:
                anim_worlds = self._sample_animation_worlds(
                    anim_enabled, anim_time, anim_speed)
            hidden_ids = {str(value) for value in (hidden_mesh_ids or ())}
            if import_ok and flat_meshes:
                if self._can_cast_shadow(model_render_flags):
                    for mesh_desc in flat_meshes:
                        if str(mesh_desc.get("node_id", "") or "") in hidden_ids:
                            continue
                        mesh = mesh_desc.get("mesh")
                        if mesh is None:
                            continue
                        is_skinned = bool(getattr(mesh, "is_skinned", False))
                        write_prog = (
                            self._ensure_shadow_depth_skinned_program(ctx)
                            if is_skinned else self._ensure_shadow_depth_program(ctx))
                        # AUD-7303 fix: node_name 不唯一（flatten_scene 保留原始
                        # 重名，如两个 "Cube"）——key 加入 id(mesh) 防止第二个
                        # 同名 mesh 命中第一个的缓存 VAO 画错几何。
                        vao = self._get_shadow_write_vao(
                            ctx, mesh,
                            f"model:{mesh_desc.get('node_name', '')}:"
                            f"{'skinned' if is_skinned else 'plain'}:{id(mesh)}",
                            write_prog)
                        if vao is None:
                            continue
                        node_name = mesh_desc.get("node_name", "")
                        world = anim_worlds.get(
                            node_name, mesh_desc.get("world_matrix"))
                        if world is None:
                            continue
                        effective_model = (model @ world).astype(np.float32)
                        write_prog["u_proj"].value = m3d.matrix_to_tuple(
                            light_view_proj)
                        write_prog["u_view"].value = m3d.matrix_to_tuple(
                            np.eye(4, dtype=np.float32))
                        write_prog["u_model"].value = m3d.matrix_to_tuple(
                            effective_model)
                        if is_skinned:
                            self._upload_bone_matrices(
                                write_prog, mesh_desc, anim_worlds)
                        vao.render(moderngl.TRIANGLES)
            if geo_payloads_src:
                for geo_index, geo_payload in enumerate(geo_payloads_src):
                    if not self._can_cast_shadow(
                            getattr(geo_payload, "render_flags", None)):
                        continue
                    mesh = getattr(geo_payload, "mesh", None)
                    model_matrix = getattr(geo_payload, "model_matrix", None)
                    if mesh is None or model_matrix is None:
                        continue
                    is_instanced = bool(getattr(
                        geo_payload, "instance_enabled", False))
                    write_prog = (
                        self._ensure_shadow_depth_instanced_program(ctx)
                        if is_instanced else self._ensure_shadow_depth_program(ctx))
                    vao = self._get_shadow_write_vao(
                        ctx, mesh,
                        f"geo:{geo_index}:{'instanced' if is_instanced else 'plain'}:{id(mesh)}",
                        write_prog,
                        instance_vbo=(getattr(geo_payload, "instance_vbo", None)
                                      if is_instanced else None))
                    if vao is None:
                        continue
                    write_prog["u_proj"].value = m3d.matrix_to_tuple(
                        light_view_proj)
                    write_prog["u_view"].value = m3d.matrix_to_tuple(
                        np.eye(4, dtype=np.float32))
                    write_prog["u_model"].value = m3d.matrix_to_tuple(
                        model_matrix)
                    draw_mode = (moderngl.LINES if getattr(
                        geo_payload, "is_lines", False) else moderngl.TRIANGLES)
                    if is_instanced and draw_mode != moderngl.LINES:
                        vao.render(draw_mode, instances=max(1, int(
                            getattr(geo_payload, "instance_count", 1))))
                    else:
                        vao.render(draw_mode)
        finally:
            ctx.disable(moderngl.DEPTH_TEST)
        return light_view_proj

    def _render_shadow_catchers(self, ctx: Any, w: int, h: int,
                                proj: np.ndarray, view: np.ndarray,
                                model: np.ndarray,
                                light_view_proj: Optional[np.ndarray],
                                import_ok: bool,
                                flat_meshes_src: Optional[list],
                                anim_worlds_src: Optional[dict],
                                model_render_flags: Any,
                                geo_payloads_src: Optional[list],
                                hidden_mesh_ids: Optional[tuple] = None,
                                anim_enabled: bool = False,
                                anim_time: float = 0.0,
                                anim_speed: float = 1.0) -> bool:
        """Composite catcher meshes over the beauty target as premultiplied shadow."""
        if light_view_proj is None or self._shadow_texture is None:
            return False
        model_is_catcher = bool(getattr(
            model_render_flags, "shadow_catcher", False))
        geo_catchers = [gp for gp in (geo_payloads_src or ())
                        if getattr(getattr(gp, "render_flags", None),
                                   "shadow_catcher", False)]
        if not model_is_catcher and not geo_catchers:
            return False
        self._render_fbo.use()
        ctx.viewport = (0, 0, w, h)
        ctx.enable(moderngl.DEPTH_TEST)
        ctx.enable(moderngl.BLEND)
        # Shader output is premultiplied shadow colour.
        # AUD-7310: save the blend function so the finally block can restore
        # it — leaving the premultiplied mode (ONE, ONE_MINUS_SRC_ALPHA)
        # armed made any later node/plugin that merely enable(BLEND) inherit
        # a wrong blend function.
        prev_blend_func: Any = None
        try:
            prev_blend_func = ctx.blend_func
        except Exception as exc:  # noqa: BLE001 — read-only probe; restore skipped
            logger.debug("Scene3D: could not read ctx.blend_func: %s", exc)
            prev_blend_func = None
        ctx.blend_func = (moderngl.ONE, moderngl.ONE_MINUS_SRC_ALPHA)
        depth_mask_disabled = False
        try:
            from OpenGL.GL import glDepthMask
            glDepthMask(False)
            depth_mask_disabled = True
        except Exception:
            pass
        try:
            self._shadow_texture.use(location=8)
            flat_meshes = (flat_meshes_src
                           if flat_meshes_src is not None
                           else self._flat_meshes_gl)
            # AUD-7305 fix: 自身导入路径按本帧动画采样（与 beauty/depth 一致），
            # 避免 catcher 在动画播放时把阴影贴到绑定姿态位置。
            anim_worlds = anim_worlds_src
            if anim_worlds is None:
                anim_worlds = self._sample_animation_worlds(
                    anim_enabled, anim_time, anim_speed)
            hidden_ids = {str(value) for value in (hidden_mesh_ids or ())}

            def _set_common(catcher_prog: Any, effective_model: np.ndarray,
                            flags: Any) -> None:
                catcher_prog["u_proj"].value = m3d.matrix_to_tuple(proj)
                catcher_prog["u_view"].value = m3d.matrix_to_tuple(view)
                catcher_prog["u_model"].value = m3d.matrix_to_tuple(
                    effective_model)
                catcher_prog["u_light_view_proj"].value = m3d.matrix_to_tuple(
                    light_view_proj)
                catcher_prog["u_shadow_map"].value = 8
                catcher_prog["u_shadow_color"].value = (0.0, 0.0, 0.0)
                catcher_prog["u_shadow_intensity"].value = max(0.0, min(
                    4.0, float(getattr(flags, "shadow_intensity", 1.0))))
                catcher_prog["u_shadow_softness"].value = max(0.0, min(
                    1.0, float(getattr(flags, "shadow_softness", 0.2))))
                catcher_prog["u_shadow_bias"].value = 0.002

            if import_ok and flat_meshes and model_is_catcher:
                for mesh_desc in flat_meshes:
                    if str(mesh_desc.get("node_id", "") or "") in hidden_ids:
                        continue
                    mesh = mesh_desc.get("mesh")
                    if mesh is None:
                        continue
                    is_skinned = bool(getattr(mesh, "is_skinned", False))
                    catcher_prog = (
                        self._ensure_shadow_catcher_skinned_program(ctx)
                        if is_skinned else self._ensure_shadow_catcher_program(ctx))
                    vao = self._get_shadow_write_vao(
                        ctx, mesh,
                        # AUD-7303: key 含 id(mesh)，重名 mesh 不复用错误几何
                        f"catcher:model:{mesh_desc.get('node_name', '')}:"
                        f"{'skinned' if is_skinned else 'plain'}:{id(mesh)}",
                        catcher_prog)
                    if vao is None:
                        continue
                    node_name = mesh_desc.get("node_name", "")
                    world = anim_worlds.get(
                        node_name, mesh_desc.get("world_matrix"))
                    if world is None:
                        continue
                    effective_model = (model @ world).astype(np.float32)
                    _set_common(catcher_prog, effective_model,
                                model_render_flags)
                    if is_skinned:
                        self._upload_bone_matrices(
                            catcher_prog, mesh_desc, anim_worlds)
                    vao.render(moderngl.TRIANGLES)
            for geo_index, geo_payload in enumerate(geo_catchers):
                mesh = getattr(geo_payload, "mesh", None)
                model_matrix = getattr(geo_payload, "model_matrix", None)
                if mesh is None or model_matrix is None:
                    continue
                is_instanced = bool(getattr(geo_payload, "instance_enabled", False))
                catcher_prog = (
                    self._ensure_shadow_catcher_instanced_program(ctx)
                    if is_instanced else self._ensure_shadow_catcher_program(ctx))
                vao = self._get_shadow_write_vao(
                    ctx, mesh,
                    f"catcher:geo:{geo_index}:{'instanced' if is_instanced else 'plain'}:{id(mesh)}",
                    catcher_prog,
                    instance_vbo=(getattr(geo_payload, "instance_vbo", None)
                                  if is_instanced else None))
                if vao is None:
                    continue
                flags = getattr(geo_payload, "render_flags", None)
                _set_common(catcher_prog, model_matrix, flags)
                draw_mode = (moderngl.LINES if getattr(
                    geo_payload, "is_lines", False) else moderngl.TRIANGLES)
                if is_instanced and draw_mode != moderngl.LINES:
                    vao.render(draw_mode, instances=max(1, int(
                        getattr(geo_payload, "instance_count", 1))))
                else:
                    vao.render(draw_mode)
            return True
        finally:
            if depth_mask_disabled:
                try:
                    from OpenGL.GL import glDepthMask
                    glDepthMask(True)
                except Exception:
                    pass
            ctx.disable(moderngl.BLEND)
            ctx.disable(moderngl.DEPTH_TEST)
            # AUD-7310: restore the blend function captured on entry so the
            # premultiplied shadow mode never leaks past this pass.
            if prev_blend_func is not None:
                try:
                    ctx.blend_func = prev_blend_func
                except Exception as exc:  # noqa: BLE001 — restore best-effort
                    logger.debug(
                        "Scene3D: blend_func restore failed: %s", exc)

    # ── P0-07：几何 AOV pass（normal/position/uv）──

    def _ensure_aov_program(self, ctx) -> moderngl.Program:
        """懒编译普通几何 AOV 程序。"""
        if self._aov_prog is None:
            from ..core.shaders import _MESH_VERTEX, _MESH_AOV_FRAGMENT
            self._aov_prog = ctx.program(
                vertex_shader=_MESH_VERTEX,
                fragment_shader=_MESH_AOV_FRAGMENT,
            )
        return self._aov_prog

    def _ensure_aov_skinned_program(self, ctx) -> moderngl.Program:
        """懒编译带骨骼输入的几何 AOV 程序。"""
        if self._aov_skinned_prog is None:
            from ..core.shaders import _MESH_VERTEX_SKINNED, _MESH_AOV_FRAGMENT
            self._aov_skinned_prog = ctx.program(
                vertex_shader=_MESH_VERTEX_SKINNED,
                fragment_shader=_MESH_AOV_FRAGMENT,
            )
        return self._aov_skinned_prog

    def _ensure_aov_instanced_program(self, ctx) -> moderngl.Program:
        """懒编译带实例偏移属性的几何 AOV 程序。"""
        if self._aov_instanced_prog is None:
            from ..core.shaders import _MESH_VERTEX_INSTANCED, _MESH_AOV_FRAGMENT
            self._aov_instanced_prog = ctx.program(
                vertex_shader=_MESH_VERTEX_INSTANCED,
                fragment_shader=_MESH_AOV_FRAGMENT,
            )
        return self._aov_instanced_prog

    def _ensure_motion_program(self, ctx) -> moderngl.Program:
        """懒编译普通几何运动矢量程序。"""
        if self._motion_prog is None:
            from ..core.shaders import _MESH_MOTION_VERTEX, _MESH_MOTION_FRAGMENT
            self._motion_prog = ctx.program(
                vertex_shader=_MESH_MOTION_VERTEX,
                fragment_shader=_MESH_MOTION_FRAGMENT,
            )
        return self._motion_prog

    def _ensure_motion_instanced_program(self, ctx) -> moderngl.Program:
        """懒编译带实例偏移属性的运动矢量程序。"""
        if self._motion_instanced_prog is None:
            from ..core.shaders import (
                _MESH_MOTION_VERTEX_INSTANCED, _MESH_MOTION_FRAGMENT)
            self._motion_instanced_prog = ctx.program(
                vertex_shader=_MESH_MOTION_VERTEX_INSTANCED,
                fragment_shader=_MESH_MOTION_FRAGMENT,
            )
        return self._motion_instanced_prog

    def _ensure_motion_skinned_program(self, ctx) -> moderngl.Program:
        """懒编译带当前/上一帧骨骼矩阵的运动矢量程序。"""
        if self._motion_skinned_prog is None:
            from ..core.shaders import (
                _MESH_MOTION_VERTEX_SKINNED, _MESH_MOTION_FRAGMENT)
            self._motion_skinned_prog = ctx.program(
                vertex_shader=_MESH_MOTION_VERTEX_SKINNED,
                fragment_shader=_MESH_MOTION_FRAGMENT,
            )
        return self._motion_skinned_prog

    def _get_aov_write_vao(self, ctx: Any, mesh: Any, key: str,
                           program: Optional[Any] = None,
                           instance_vbo: Optional[Any] = None) -> Optional[Any]:
        """懒建/缓存几何 AOV VAO，与 beauty/depth 的属性布局保持一致。"""
        if key in self._aov_write_vaos:
            return self._aov_write_vaos[key]
        prog = program or self._ensure_aov_program(ctx)
        owned_buffers = []
        try:
            if mesh is None:
                if self._mesh_vbo is None or self._mesh_ibo is None:
                    return None
                contents = [(
                    self._mesh_vbo, '3f 3f 2f',
                    'in_pos', 'in_normal', 'in_uv')]
                if instance_vbo is not None:
                    contents.append((instance_vbo, '3f /i',
                                     'in_instance_offset'))
                vao = ctx.vertex_array(
                    prog, contents,
                    index_buffer=self._mesh_ibo,
                )
            else:
                if mesh.is_skinned:
                    data = mesh.interleaved_skinned().tobytes()
                    layout = '3f 3f 2f 4f 4f'
                    attrs = ('in_pos', 'in_normal', 'in_uv',
                             'in_bone_ids', 'in_bone_weights')
                else:
                    data = mesh.interleaved().tobytes()
                    layout = '3f 3f 2f'
                    attrs = ('in_pos', 'in_normal', 'in_uv')
                vbo = ctx.buffer(data)
                ibo = ctx.buffer(mesh.indices.tobytes())
                owned_buffers = [vbo, ibo]
                contents = [(vbo, layout, *attrs)]
                if instance_vbo is not None:
                    contents.append((instance_vbo, '3f /i',
                                     'in_instance_offset'))
                vao = ctx.vertex_array(
                    prog, contents, index_buffer=ibo)
            self._aov_write_vaos[key] = vao
            if owned_buffers:
                self._aov_write_buffers[key] = owned_buffers
            return vao
        except Exception:
            for buffer in owned_buffers:
                try: buffer.release()
                except Exception: pass
            return None

    @staticmethod
    def _material_aov_key(mesh_desc: dict, fallback: str) -> str:
        """Return a stable material key for the Material ID pass."""
        explicit = str(mesh_desc.get("material_id", "") or "").strip()
        if explicit:
            return explicit
        material = mesh_desc.get("material")
        if isinstance(material, dict):
            for key in ("material_id", "name"):
                value = str(material.get(key, "") or "").strip()
                if value:
                    return value
        else:
            for attribute in ("material_id", "name"):
                value = str(getattr(material, attribute, "") or "").strip()
                if value:
                    return value
        return str(fallback or "material")

    def _material_aov_rgba(self, mesh_desc: dict, fallback: str) -> tuple:
        """Allocate and encode the stable Material ID for one draw item."""
        key = self._material_aov_key(mesh_desc, fallback)
        return encode_id_to_rgba(self._material_id_allocator.allocate(key))

    def _render_aov_pass(self, ctx: Any, aov_name: str, mode: int,
                         w: int, h: int, proj: np.ndarray,
                         view: np.ndarray, model: np.ndarray,
                         import_ok: bool, instance_enabled: bool,
                         instance_count: int, anim_enabled: bool,
                         anim_time: float, anim_speed: float,
                         flat_meshes_src: Optional[list] = None,
                         anim_worlds_src: Optional[dict] = None,
                         geo_payloads_src: Optional[list] = None,
                         hidden_mesh_ids: Optional[tuple] = None) -> None:
        """将一个几何 AOV 渲染到已分配的纹理。

        ``mode`` 对应 ``_MESH_AOV_FRAGMENT``：0=normal、1=position、2=uv、3=material_id。
        该 pass 使用同一套 mesh/skin/instance 几何路径，保证辅助输出与
        beauty 的可见轮廓一致；AOV 失败由调用方降级为不可用，不影响 beauty。
        Geo3D 通过 payload 携带的 CPU MeshData 重新绑定 AOV VAO，避免借用
        Geo3D 的 beauty VAO（后者绑定的是另一套 fragment program）。
        """
        texture = self._aov_textures.get(aov_name)
        fbo = self._aov_fbos.get(aov_name)
        if texture is None or fbo is None:
            return
        prog = self._ensure_aov_program(ctx)
        fbo.use()
        ctx.viewport = (0, 0, w, h)
        ctx.clear(0.0, 0.0, 0.0, 0.0)
        ctx.enable(moderngl.DEPTH_TEST)
        try:
            flat_meshes = (flat_meshes_src
                           if flat_meshes_src is not None
                           else self._flat_meshes_gl)
            anim_worlds = anim_worlds_src
            if anim_worlds is None:
                anim_worlds = self._sample_animation_worlds(
                    anim_enabled, anim_time, anim_speed)
            hidden_ids = {str(value) for value in (hidden_mesh_ids or ())}

            def _set_common(write_prog: Any,
                            effective_model: np.ndarray,
                            aov_id: tuple = (0.0, 0.0, 0.0, 0.0)) -> None:
                write_prog['u_proj'].value = m3d.matrix_to_tuple(proj)
                write_prog['u_view'].value = m3d.matrix_to_tuple(view)
                write_prog['u_model'].value = m3d.matrix_to_tuple(effective_model)
                write_prog['u_aov_mode'].value = int(mode)
                write_prog['u_aov_id'].value = aov_id

            if import_ok and flat_meshes:
                for mesh_desc in flat_meshes:
                    if str(mesh_desc.get('node_id', '') or '') in hidden_ids:
                        continue
                    mesh = mesh_desc['mesh']
                    is_skinned = bool(mesh.is_skinned)
                    write_prog = (self._ensure_aov_skinned_program(ctx)
                                  if is_skinned else prog)
                    vao_key = (f"{mesh_desc['node_name']}:skinned:{id(mesh)}"
                               if is_skinned
                               else f"{mesh_desc['node_name']}:{id(mesh)}")
                    vao = self._get_aov_write_vao(
                        ctx, mesh, vao_key, program=write_prog)
                    if vao is None:
                        continue
                    node_name = mesh_desc['node_name']
                    world = anim_worlds.get(
                        node_name, mesh_desc['world_matrix'])
                    effective_model = (model @ world).astype(np.float32)
                    material_id = self._material_aov_rgba(
                        mesh_desc, f"model:{mesh_desc.get('node_name', node_name)}")
                    _set_common(write_prog, effective_model, material_id)
                    if is_skinned:
                        self._upload_bone_matrices(
                            write_prog, mesh_desc, anim_worlds)
                    if instance_enabled and mesh_desc.get('can_instance'):
                        vao.render(moderngl.TRIANGLES, instances=instance_count)
                    else:
                        vao.render(moderngl.TRIANGLES)
            elif geo_payloads_src:
                for geo_index, geo_payload in enumerate(geo_payloads_src):
                    if str(getattr(geo_payload, 'node_id', '') or '') in hidden_ids:
                        continue
                    mesh = getattr(geo_payload, 'mesh', None)
                    if mesh is None:
                        continue
                    is_instanced = bool(getattr(geo_payload,
                                                'instance_enabled', False))
                    write_prog = (self._ensure_aov_instanced_program(ctx)
                                  if is_instanced
                                  else self._ensure_aov_program(ctx))
                    vao_key = (f"geo:{geo_index}:"
                               f"{'instanced' if is_instanced else 'plain'}:{id(mesh)}")
                    vao = self._get_aov_write_vao(
                        ctx, mesh, vao_key, program=write_prog,
                        instance_vbo=(getattr(geo_payload, 'instance_vbo', None)
                                      if is_instanced else None))
                    if vao is None:
                        continue
                    effective_model = getattr(geo_payload, 'model_matrix', None)
                    if effective_model is None:
                        continue
                    material_id = self._material_aov_rgba(
                        {"material": getattr(geo_payload, "material", None)},
                        f"geo:{getattr(geo_payload, 'node_id', geo_index)}")
                    _set_common(write_prog, effective_model, material_id)
                    draw_mode = (moderngl.LINES
                                 if bool(getattr(geo_payload, 'is_lines', False))
                                 else moderngl.TRIANGLES)
                    if is_instanced:
                        vao.render(draw_mode,
                                   instances=max(1, int(getattr(
                                       geo_payload, 'instance_count', 1))))
                    else:
                        vao.render(draw_mode)
            else:
                is_instanced = bool(
                    instance_enabled and self._mesh_instance_enabled
                    and self._instance_vbo is not None
                )
                write_prog = (self._ensure_aov_instanced_program(ctx)
                              if is_instanced else self._ensure_aov_program(ctx))
                vao = self._get_aov_write_vao(
                    ctx, None,
                    f"__procedural__:{'instanced' if is_instanced else 'plain'}",
                    program=write_prog,
                    instance_vbo=(self._instance_vbo if is_instanced else None))
                if vao is not None:
                    _set_common(
                        write_prog, model,
                        self._material_aov_rgba(
                            {"material": None}, "procedural"))
                    draw_mode = (moderngl.LINES if self._mesh_is_lines
                                 else moderngl.TRIANGLES)
                    if is_instanced and not self._mesh_is_lines:
                        vao.render(draw_mode, instances=instance_count)
                    else:
                        vao.render(draw_mode)
        finally:
            ctx.disable(moderngl.DEPTH_TEST)

    def _get_motion_write_vao(self, ctx: Any, mesh: Any, key: str,
                              program: Any,
                              instance_vbo: Optional[Any] = None) -> Optional[Any]:
        """Build a motion VAO using only attributes consumed by its shader.

        The beauty/AOV layouts carry normals and UVs, but the motion shader
        only needs position (plus bone data for skinned meshes). Repacking the
        compact attributes avoids ModernGL rejecting inactive vertex inputs.
        """
        if key in self._motion_write_vaos:
            return self._motion_write_vaos[key]
        buffers = []
        try:
            if mesh is None:
                motion_mesh = self._mesh_data
                if motion_mesh is None or self._mesh_ibo is None:
                    return None
                vbo = ctx.buffer(motion_mesh.vertices.tobytes())
                ibo = self._mesh_ibo
                layout = '3f'
                attrs = ('in_pos',)
            elif mesh.is_skinned:
                compact = np.concatenate((
                    mesh.vertices,
                    mesh.bone_indices.astype(np.float32),
                    mesh.bone_weights,
                ), axis=1).astype(np.float32, copy=False)
                vbo = ctx.buffer(compact.tobytes())
                ibo = ctx.buffer(mesh.indices.tobytes())
                buffers.append(ibo)
                layout = '3f 4f 4f'
                attrs = ('in_pos', 'in_bone_ids', 'in_bone_weights')
            else:
                vbo = ctx.buffer(mesh.vertices.tobytes())
                ibo = ctx.buffer(mesh.indices.tobytes())
                buffers.append(ibo)
                layout = '3f'
                attrs = ('in_pos',)
            buffers.append(vbo)
            contents = [(vbo, layout, *attrs)]
            if instance_vbo is not None:
                contents.append((instance_vbo, '3f /i',
                                 'in_instance_offset'))
            vao = ctx.vertex_array(program, contents, index_buffer=ibo)
            self._motion_write_vaos[key] = vao
            self._motion_write_buffers[key] = buffers
            return vao
        except Exception:
            for buffer in buffers:
                try: buffer.release()
                except Exception: pass
            return None

    def _render_motion_pass(
            self, ctx: Any, w: int, h: int, proj: np.ndarray,
            view: np.ndarray, model: np.ndarray, frame_number: int,
            scene_key: tuple, import_ok: bool,
            instance_enabled: bool, instance_count: int,
            flat_meshes_src: Optional[list] = None,
            anim_worlds_src: Optional[dict] = None,
            geo_payloads_src: Optional[list] = None,
            hidden_mesh_ids: Optional[tuple] = None) -> None:
        """Render signed per-frame screen-space motion into the motion AOV.

        The previous matrices are owned by this Scene3D render node and are
        advanced only after a real render. A cache hit therefore cannot move
        temporal history forward. Discontinuous timeline jumps and scene
        identity changes intentionally produce zero motion for that frame.
        """
        texture = self._aov_textures.get(AOVPass.MOTION_VECTOR.value)
        fbo = self._aov_fbos.get(AOVPass.MOTION_VECTOR.value)
        if texture is None or fbo is None:
            return

        previous_is_valid = bool(
            self._motion_prev_proj is not None
            and self._motion_prev_view is not None
            and self._motion_prev_frame is not None
            and int(frame_number) == int(self._motion_prev_frame) + 1
            and self._motion_prev_scene_key == scene_key
        )
        previous_proj = (self._motion_prev_proj
                         if previous_is_valid else proj)
        previous_view = (self._motion_prev_view
                         if previous_is_valid else view)
        previous_models = self._motion_prev_models if previous_is_valid else {}
        previous_anim_worlds = (
            self._motion_prev_anim_worlds if previous_is_valid else {})
        current_models: Dict[str, np.ndarray] = {}
        current_anim_worlds: Dict[str, np.ndarray] = {}

        fbo.use()
        ctx.viewport = (0, 0, w, h)
        ctx.clear(0.0, 0.0, 0.0, 0.0)
        ctx.enable(moderngl.DEPTH_TEST)
        try:
            flat_meshes = (flat_meshes_src
                           if flat_meshes_src is not None
                           else self._flat_meshes_gl)
            anim_worlds = anim_worlds_src or {}
            hidden_ids = {str(value) for value in (hidden_mesh_ids or ())}
            current_anim_worlds = {
                name: np.asarray(world, dtype=np.float32).copy()
                for name, world in anim_worlds.items()
            }

            def _set_common(write_prog: Any,
                            current_model: np.ndarray,
                            previous_model: np.ndarray,
                            motion_scale: float) -> None:
                write_prog['u_proj'].value = m3d.matrix_to_tuple(proj)
                write_prog['u_view'].value = m3d.matrix_to_tuple(view)
                write_prog['u_model'].value = m3d.matrix_to_tuple(current_model)
                write_prog['u_prev_proj'].value = m3d.matrix_to_tuple(
                    previous_proj)
                write_prog['u_prev_view'].value = m3d.matrix_to_tuple(
                    previous_view)
                write_prog['u_prev_model'].value = m3d.matrix_to_tuple(
                    previous_model)
                write_prog['u_motion_scale'].value = float(motion_scale)

            if import_ok and flat_meshes:
                for mesh_index, mesh_desc in enumerate(flat_meshes):
                    if str(mesh_desc.get('node_id', '') or '') in hidden_ids:
                        continue
                    mesh = mesh_desc['mesh']
                    is_skinned = bool(mesh.is_skinned)
                    write_prog = (self._ensure_motion_skinned_program(ctx)
                                  if is_skinned
                                  else self._ensure_motion_program(ctx))
                    vao_key = (f"model:{mesh_index}:{mesh_desc['node_name']}:"
                               f"{'skinned' if is_skinned else 'plain'}:{id(mesh)}")
                    vao = self._get_motion_write_vao(
                        ctx, mesh, vao_key, program=write_prog)
                    if vao is None:
                        continue
                    node_name = mesh_desc['node_name']
                    current_world = anim_worlds.get(
                        node_name, mesh_desc['world_matrix'])
                    current_model = (model @ current_world).astype(np.float32)
                    motion_key = f"model:{mesh_index}:{node_name}"
                    previous_model = previous_models.get(
                        motion_key, current_model)
                    current_models[motion_key] = current_model.copy()
                    _set_common(
                        write_prog, current_model, previous_model,
                        1.0 if previous_is_valid else 0.0)
                    if is_skinned:
                        self._upload_bone_matrices(
                            write_prog, mesh_desc, anim_worlds)
                        self._upload_bone_matrices(
                            write_prog, mesh_desc, previous_anim_worlds,
                            uniform_name='u_prev_bone_matrices')
                    if instance_enabled and mesh_desc.get('can_instance'):
                        vao.render(moderngl.TRIANGLES, instances=instance_count)
                    else:
                        vao.render(moderngl.TRIANGLES)
            elif geo_payloads_src:
                for geo_index, geo_payload in enumerate(geo_payloads_src):
                    if str(getattr(geo_payload, 'node_id', '') or '') in hidden_ids:
                        continue
                    mesh = getattr(geo_payload, 'mesh', None)
                    current_model = getattr(geo_payload, 'model_matrix', None)
                    if mesh is None or current_model is None:
                        continue
                    is_instanced = bool(getattr(geo_payload,
                                                'instance_enabled', False))
                    write_prog = (self._ensure_motion_instanced_program(ctx)
                                  if is_instanced
                                  else self._ensure_motion_program(ctx))
                    vao_key = (f"geo:{geo_index}:"
                               f"{'instanced' if is_instanced else 'plain'}:{id(mesh)}")
                    vao = self._get_motion_write_vao(
                        ctx, mesh, vao_key, program=write_prog,
                        instance_vbo=(getattr(geo_payload, 'instance_vbo', None)
                                      if is_instanced else None))
                    if vao is None:
                        continue
                    current_model = np.asarray(current_model,
                                               dtype=np.float32)
                    motion_key = f"geo:{geo_index}:{getattr(geo_payload, 'node_id', '')}"
                    previous_model = previous_models.get(
                        motion_key, current_model)
                    current_models[motion_key] = current_model.copy()
                    _set_common(
                        write_prog, current_model, previous_model,
                        1.0 if previous_is_valid else 0.0)
                    draw_mode = (moderngl.LINES
                                 if bool(getattr(geo_payload, 'is_lines', False))
                                 else moderngl.TRIANGLES)
                    if is_instanced:
                        vao.render(draw_mode, instances=max(1, int(
                            getattr(geo_payload, 'instance_count', 1))))
                    else:
                        vao.render(draw_mode)
            else:
                is_instanced = bool(
                    instance_enabled and self._mesh_instance_enabled
                    and self._instance_vbo is not None
                )
                write_prog = (self._ensure_motion_instanced_program(ctx)
                              if is_instanced
                              else self._ensure_motion_program(ctx))
                vao = self._get_motion_write_vao(
                    ctx, None,
                    f"__procedural__:{'motion_instanced' if is_instanced else 'motion'}",
                    program=write_prog,
                    instance_vbo=(self._instance_vbo if is_instanced else None))
                if vao is not None:
                    motion_key = "procedural"
                    current_model = np.asarray(model, dtype=np.float32)
                    previous_model = previous_models.get(
                        motion_key, current_model)
                    current_models[motion_key] = current_model.copy()
                    _set_common(
                        write_prog, current_model, previous_model,
                        1.0 if previous_is_valid else 0.0)
                    draw_mode = (moderngl.LINES if self._mesh_is_lines
                                 else moderngl.TRIANGLES)
                    if is_instanced and not self._mesh_is_lines:
                        vao.render(draw_mode, instances=instance_count)
                    else:
                        vao.render(draw_mode)
        finally:
            ctx.disable(moderngl.DEPTH_TEST)

        self._motion_prev_proj = np.asarray(proj, dtype=np.float32).copy()
        self._motion_prev_view = np.asarray(view, dtype=np.float32).copy()
        self._motion_prev_frame = int(frame_number)
        self._motion_prev_scene_key = scene_key
        self._motion_prev_models = current_models
        self._motion_prev_anim_worlds = current_anim_worlds

    # ── P7：GPU ID picking（object-id pass）──

    def _ensure_id_write_program(self, ctx) -> moderngl.Program:
        """懒编译并缓存 ID-write 程序（_MESH_VERTEX + _MESH_ID_WRITE_FRAGMENT）。"""
        if self._id_write_prog is None:
            from ..core.shaders import _MESH_VERTEX, _MESH_ID_WRITE_FRAGMENT
            self._id_write_prog = ctx.program(
                vertex_shader=_MESH_VERTEX,
                fragment_shader=_MESH_ID_WRITE_FRAGMENT,
            )
        return self._id_write_prog

    def _ensure_id_write_skinned_program(self, ctx) -> moderngl.Program:
        """懒编译并缓存带骨骼属性的 ID-write 程序。"""
        if self._id_write_skinned_prog is None:
            from ..core.shaders import _MESH_VERTEX_SKINNED, _MESH_ID_WRITE_FRAGMENT
            self._id_write_skinned_prog = ctx.program(
                vertex_shader=_MESH_VERTEX_SKINNED,
                fragment_shader=_MESH_ID_WRITE_FRAGMENT,
            )
        return self._id_write_skinned_prog

    def _get_id_write_vao(self, ctx, mesh, key: str, program=None):
        """懒建/缓存 ID-write VAO（绑定 _MESH_VERTEX + _MESH_ID_WRITE_FRAGMENT）。

        与 _get_depth_write_vao 同模式：procedural（mesh=None）用主几何；导入 mesh 用
        各自几何；蒙皮 mesh 使用 bone attribute layout 和同一套动画矩阵。
        AUD-7301 fix: 自建 VBO/IBO 记入 _id_write_buffers，_release_render_target
        统一释放（ModernGL VAO.release 不释放其 buffer）。
        """
        if key in self._id_write_vaos:
            return self._id_write_vaos[key]
        prog = program or self._ensure_id_write_program(ctx)
        owned_buffers = []
        try:
            if mesh is None:
                if self._mesh_vbo is None or self._mesh_ibo is None:
                    return None
                vao = ctx.vertex_array(
                    prog,
                    [(self._mesh_vbo, '3f 3f 2f', 'in_pos', 'in_normal', 'in_uv')],
                    index_buffer=self._mesh_ibo,
                )
            else:
                if mesh.is_skinned:
                    data = mesh.interleaved_skinned().tobytes()
                    layout = '3f 3f 2f 4f 4f'
                    attrs = ('in_pos', 'in_normal', 'in_uv',
                             'in_bone_ids', 'in_bone_weights')
                else:
                    data = mesh.interleaved().tobytes()
                    layout = '3f 3f 2f'
                    attrs = ('in_pos', 'in_normal', 'in_uv')
                vbo = ctx.buffer(data)
                ibo = ctx.buffer(mesh.indices.tobytes())
                owned_buffers = [vbo, ibo]
                vao = ctx.vertex_array(
                    prog,
                    [(vbo, layout, *attrs)],
                    index_buffer=ibo,
                )
            self._id_write_vaos[key] = vao
            if owned_buffers:
                self._id_write_buffers[key] = owned_buffers
            return vao
        except Exception:
            for buffer in owned_buffers:
                try:
                    buffer.release()
                except Exception:
                    pass
            return None

    @staticmethod
    def _encode_pick_id(pick_id: int) -> tuple:
        """pick_id(int) → 归一化 RGBA tuple（GLSL u_object_id 用）。

        pick_id=0 保留为背景（输出 (0,0,0,0)，viewer 读回时 alpha=0 判定未命中）。
        pick_id 拆成 R/G/B 三字节（最多 16M 个对象），A=1 表示命中。
        """
        r = (pick_id & 0xFF) / 255.0
        g = ((pick_id >> 8) & 0xFF) / 255.0
        b = ((pick_id >> 16) & 0xFF) / 255.0
        a = 1.0 if pick_id > 0 else 0.0
        return (r, g, b, a)

    def _build_pick_id_map(self, model_payloads, geo_payloads) -> tuple:
        """为本帧可见的 3D 节点分配 pick_id，返回 (pick_map, node_pick_ids)。

        pick_map: {pick_id_int: node_id_str}（viewer 读回 pick_id 后查此 map）。
        node_pick_ids: {node_id_str: pick_id_int}（渲染时每个 mesh 查此得 pick_id）。
        分配规则：每个唯一 node_id 一个 pick_id（1 起）；0=背景。同节点多 mesh 共享 pick_id。
        """
        pick_map = {}
        node_pick_ids = {}
        next_id = 1
        for payload in (model_payloads or []):
            payload_nid = getattr(payload, "node_id", "") or ""
            hidden = {
                str(value) for value in getattr(payload, "hidden_mesh_ids", ())
            }
            mesh_ids = {
                str(mesh.get("node_id", "") or payload_nid)
                for mesh in getattr(payload, "flat_meshes", ())
                if str(mesh.get("node_id", "") or payload_nid) not in hidden
            }
            for nid in sorted(mesh_ids or ({payload_nid} if payload_nid else set())):
                if nid not in node_pick_ids:
                    node_pick_ids[nid] = next_id
                    pick_map[next_id] = nid
                    next_id += 1
        for payload in (geo_payloads or []):
            nid = getattr(payload, "node_id", "") or ""
            if nid and nid not in node_pick_ids:
                node_pick_ids[nid] = next_id
                pick_map[next_id] = nid
                next_id += 1
        return pick_map, node_pick_ids

    def _build_component_pick_meshes(self, model, import_ok,
                                     flat_meshes_src, anim_worlds_src,
                                     model_payloads, geo_payloads,
                                     node_pick_ids=None) -> list:
        """Build immutable CPU geometry descriptors for component picking.

        The descriptors are attached to ``id_pick_info`` so the UI can build a
        bounded BVH lazily. Render-thread GL resources stay private; numpy
        arrays are the same read-only mesh data already owned by the payload.
        """
        meshes = []
        isolate_nid = getattr(self, "_p7_isolate_nid", None)
        if import_ok:
            flat_meshes = (flat_meshes_src
                           if flat_meshes_src is not None
                           else self._flat_meshes_gl)
            anim_worlds = anim_worlds_src or {}
            payload_nid = (getattr(model_payloads[0], "node_id", "")
                           if model_payloads else "")
            hidden = {
                str(value) for value in getattr(
                    model_payloads[0], "hidden_mesh_ids", ())
            } if model_payloads else set()
            for mesh_desc in flat_meshes or ():
                mesh = mesh_desc.get("mesh") if isinstance(mesh_desc, dict) else None
                if mesh is None:
                    continue
                node_id = str(mesh_desc.get("node_id", "") or payload_nid)
                if not node_id or node_id in hidden:
                    continue
                if isolate_nid and node_id != isolate_nid and payload_nid != isolate_nid:
                    continue
                node_name = str(mesh_desc.get("node_name", ""))
                world = anim_worlds.get(node_name, mesh_desc.get("world_matrix"))
                if world is None:
                    continue
                meshes.append({
                    "node_id": node_id,
                    "mesh_key": f"model:{node_id}:{node_name}",
                    "pick_id": (int(node_pick_ids[node_id])
                                 if node_pick_ids and node_id in node_pick_ids
                                 else 0),
                    "vertices": mesh.vertices,
                    "indices": mesh.indices,
                    "model_matrix": (np.asarray(model, dtype=np.float32)
                                     @ np.asarray(world, dtype=np.float32)),
                    "revision": (id(mesh.vertices), id(mesh.indices),
                                  bool(mesh.is_skinned)),
                    "is_skinned": bool(mesh.is_skinned),
                })
        for payload in geo_payloads or ():
            node_id = str(getattr(payload, "node_id", "") or "")
            mesh = getattr(payload, "mesh", None)
            if (not node_id or mesh is None
                    or getattr(payload, "is_lines", False)):
                continue
            if isolate_nid and node_id != isolate_nid:
                continue
            base_matrix = (np.asarray(payload.model_matrix, dtype=np.float32).copy()
                           if payload.model_matrix is not None
                           else np.eye(4, dtype=np.float32))
            offsets = getattr(payload, "instance_offsets", None)
            if getattr(payload, "instance_enabled", False) and offsets is not None:
                instance_offsets = np.asarray(offsets, dtype=np.float32).reshape(-1, 3)
            else:
                instance_offsets = np.zeros((1, 3), dtype=np.float32)
            for instance_index, offset in enumerate(instance_offsets):
                instance_matrix = base_matrix.copy()
                if getattr(payload, "instance_enabled", False):
                    translate = np.eye(4, dtype=np.float32)
                    translate[:3, 3] = offset
                    instance_matrix = base_matrix @ translate
                meshes.append({
                    "node_id": node_id,
                    "mesh_key": f"geo:{node_id}:{instance_index}",
                    "instance_index": int(instance_index),
                    "pick_id": (int(node_pick_ids[node_id])
                                 if node_pick_ids and node_id in node_pick_ids
                                 else 0),
                    "vertices": mesh.vertices,
                    "indices": mesh.indices,
                    "model_matrix": instance_matrix,
                    "revision": (int(getattr(payload, "content_revision", 0)),
                                  id(mesh.vertices), id(mesh.indices)),
                    "is_skinned": False,
                })
        return meshes

    def _render_id_pass(self, ctx, w, h, proj, view, model,
                        import_ok, instance_enabled, instance_count,
                        flat_meshes_src, anim_worlds_src,
                        model_payloads, geo_payloads,
                        anim_enabled: bool = False,
                        anim_time: float = 0.0,
                        anim_speed: float = 1.0) -> None:
        """P7：渲染 object-id 到 RGBA8 _id_texture（viewer 点击时读 1 像素拾取）。

        克隆 _render_depth_pass 结构，每 mesh 设 u_object_id = pick_id（按所属 node_id 分配）。
        隔离模式（isolate_3d_node_id）下只渲染该节点。背景 clear (0,0,0,0)。
        完成后把 {tex_glo, w, h, pick_map} 写入 eval_ctx.id_pick_info（通过实例属性回传）。

        AUD-7305 fix: 自身导入路径（anim_worlds_src=None）旧代码采样
        ``_sample_animation_worlds(False, 0.0, 1.0)`` 恒返回 {} → 拾取用绑定
        姿态而 beauty 用动画矩阵，动画播放时 GPU 拾取点不中/选错对象。现按
        本帧动画采样（与 beauty/depth 一致）。
        AUD-7306 fix: Geo3D 分支旧代码退化为 procedural id VAO（geo payload
        在场时自身 procedural 几何恒被释放 → VAO 恒 None），但 pick_map 仍为
        其分配 id——视口点击 Geo3D 永远未命中。现用 payload 携带的 CPU
        mesh（AOV pass 同模式）为每个 Geo3D 构建独立 id VAO，实例化展开用
        CPU instance_offsets（payload 注释即为辅助拾取 pass 准备）。
        """
        if self._id_texture is None or self._id_fbo is None:
            return
        # 取 eval_ctx（_do_evaluate 里解包前的原始 ctx 存在实例属性上，这里复用）
        # 实际 eval_ctx 由调用方传入 _render_depth_pass 同样的方式；此处用闭包变量。
        # （id_pick_info 的回传在 _do_evaluate 里做，此方法只负责渲染）
        prog = self._ensure_id_write_program(ctx)
        self._id_fbo.use()
        ctx.viewport = (0, 0, w, h)
        # clear 背景 = (0,0,0,0)（pick_id=0，viewer 读回 alpha=0 判定未命中）
        ctx.clear(0.0, 0.0, 0.0, 0.0)
        ctx.enable(moderngl.DEPTH_TEST)
        try:
            prog['u_proj'].value = m3d.matrix_to_tuple(proj)
            prog['u_view'].value = m3d.matrix_to_tuple(view)
            # 构建 pick_id 映射
            pick_map, node_pick_ids = self._build_pick_id_map(
                model_payloads, geo_payloads)
            # 隔离模式
            isolate_nid = getattr(self, "_p7_isolate_nid", None)
            # 渲染 model mesh（路径 A）
            flat_meshes = flat_meshes_src if flat_meshes_src is not None else self._flat_meshes_gl
            if import_ok and flat_meshes:
                anim_worlds = anim_worlds_src
                if anim_worlds is None:
                    anim_worlds = self._sample_animation_worlds(
                        anim_enabled, anim_time, anim_speed)
                payload_nid = (getattr(model_payloads[0], "node_id", "")
                               if model_payloads else "")
                hidden = {
                    str(value) for value in getattr(
                        model_payloads[0], "hidden_mesh_ids", ())
                } if model_payloads else set()
                for m in flat_meshes:
                    mesh_nid = str(m.get("node_id", "") or payload_nid)
                    if mesh_nid in hidden:
                        continue
                    if isolate_nid and mesh_nid != isolate_nid and payload_nid != isolate_nid:
                        continue
                    mesh = m['mesh']
                    pick_id = node_pick_ids.get(mesh_nid, 0)
                    is_skinned = bool(mesh.is_skinned)
                    write_prog = (
                        self._ensure_id_write_skinned_program(ctx)
                        if is_skinned else prog
                    )
                    # AUD-7303: key 含 id(mesh)（与 AOV/motion pass 对齐），
                    # 重名 mesh 不复用错误几何。
                    vao_key = (
                        f"{m['node_name']}:skinned:{id(mesh)}"
                        if is_skinned else f"{m['node_name']}:{id(mesh)}"
                    )
                    vao = self._get_id_write_vao(
                        ctx, mesh, vao_key, program=write_prog)
                    if vao is None:
                        continue
                    node_name = m['node_name']
                    wm = anim_worlds.get(node_name, m['world_matrix'])
                    effective_model = (model @ wm).astype(np.float32)
                    write_prog['u_proj'].value = m3d.matrix_to_tuple(proj)
                    write_prog['u_view'].value = m3d.matrix_to_tuple(view)
                    write_prog['u_model'].value = m3d.matrix_to_tuple(effective_model)
                    write_prog['u_object_id'].value = self._encode_pick_id(pick_id)
                    if is_skinned:
                        self._upload_bone_matrices(write_prog, m, anim_worlds)
                    if instance_enabled and m.get('can_instance'):
                        vao.render(moderngl.TRIANGLES, instances=instance_count)
                    else:
                        vao.render(moderngl.TRIANGLES)
            # 渲染 Geo3D（路径 B）——用 payload 的 CPU mesh 构建独立 id VAO
            for geo_index, gp in enumerate(geo_payloads or []):
                g_nid = getattr(gp, "node_id", "") or ""
                if isolate_nid and g_nid != isolate_nid:
                    continue
                gp_mesh = getattr(gp, "mesh", None)
                gp_model = getattr(gp, "model_matrix", None)
                if gp_mesh is None or gp_model is None:
                    continue
                pick_id = node_pick_ids.get(g_nid, 0)
                id_vao = self._get_id_write_vao(
                    ctx, gp_mesh, f"geo:{geo_index}:{id(gp_mesh)}", program=prog)
                if id_vao is None:
                    continue
                # 实例化：id-write 无 instanced 变体，用 payload 的 CPU
                # instance_offsets 逐实例合成 model 矩阵（与 CPU 组件拾取
                # _build_component_pick_meshes 同一数据源）。
                offsets = getattr(gp, "instance_offsets", None)
                if (getattr(gp, "instance_enabled", False)
                        and offsets is not None):
                    offset_list = np.asarray(
                        offsets, dtype=np.float32).reshape(-1, 3)
                else:
                    offset_list = np.zeros((1, 3), dtype=np.float32)
                _gmode = moderngl.LINES if getattr(gp, "is_lines", False) else moderngl.TRIANGLES
                for offset in offset_list:
                    if getattr(gp, "instance_enabled", False):
                        _m = np.eye(4, dtype=np.float32)
                        _m[:3, 3] = offset
                        effective = (np.asarray(gp_model, dtype=np.float32) @ _m)
                    else:
                        effective = np.asarray(gp_model, dtype=np.float32)
                    prog['u_object_id'].value = self._encode_pick_id(pick_id)
                    prog['u_model'].value = m3d.matrix_to_tuple(effective)
                    id_vao.render(_gmode)
        finally:
            ctx.disable(moderngl.DEPTH_TEST)
        # 回传 id_pick_info（调用方 _do_evaluate 读 self._p7_id_pick_info 写 eval_ctx）
            self._p7_id_pick_info = {
            "tex_glo": self._id_texture.glo,
            "w": w, "h": h,
            "pick_map": pick_map,
            "component_meshes": self._build_component_pick_meshes(
                model, import_ok, flat_meshes_src, anim_worlds_src,
                model_payloads, geo_payloads, node_pick_ids=node_pick_ids),
            "proj": np.asarray(proj, dtype=np.float32).copy(),
            "view": np.asarray(view, dtype=np.float32).copy(),
        }

    # ── P7：选中对象描边（inverted hull outline pass）──

    def _ensure_outline_program(self, ctx) -> moderngl.Program:
        """懒编译并缓存 outline 程序（_MESH_OUTLINE_VERTEX + _MESH_OUTLINE_FRAGMENT）。"""
        if self._outline_prog is None:
            from ..core.shaders import _MESH_OUTLINE_VERTEX, _MESH_OUTLINE_FRAGMENT
            self._outline_prog = ctx.program(
                vertex_shader=_MESH_OUTLINE_VERTEX,
                fragment_shader=_MESH_OUTLINE_FRAGMENT,
            )
        return self._outline_prog

    def _get_outline_vao(self, ctx, mesh, key: str):
        """懒建/缓存 outline VAO（同 id-write 模式，procedural/flat mesh 复用）。

        AUD-7301 fix: 自建 VBO/IBO 记入 _outline_buffers，_release_render_target
        统一释放。AUD-7306: Geo3D 也走 mesh 分支（不再退化为 procedural）。
        """
        if key in self._outline_vaos:
            return self._outline_vaos[key]
        prog = self._ensure_outline_program(ctx)
        owned_buffers = []
        try:
            if mesh is None:
                if self._mesh_vbo is None or self._mesh_ibo is None:
                    return None
                vao = ctx.vertex_array(
                    prog,
                    [(self._mesh_vbo, '3f 3f 2f', 'in_pos', 'in_normal', 'in_uv')],
                    index_buffer=self._mesh_ibo,
                )
            else:
                if mesh.is_skinned:
                    self._outline_vaos[key] = None
                    return None
                data = mesh.interleaved().tobytes()
                vbo = ctx.buffer(data)
                ibo = ctx.buffer(mesh.indices.tobytes())
                owned_buffers = [vbo, ibo]
                vao = ctx.vertex_array(
                    prog,
                    [(vbo, '3f 3f 2f', 'in_pos', 'in_normal', 'in_uv')],
                    index_buffer=ibo,
                )
            self._outline_vaos[key] = vao
            if owned_buffers:
                self._outline_buffers[key] = owned_buffers
            return vao
        except Exception:
            for buffer in owned_buffers:
                try:
                    buffer.release()
                except Exception:
                    pass
            return None

    def _render_outline_pass(self, ctx, w, h, proj, view, model,
                             import_ok, instance_enabled, instance_count,
                             flat_meshes_src, anim_worlds_src,
                             model_payloads, geo_payloads,
                             selected_nid: str,
                             anim_enabled: bool = False,
                             anim_time: float = 0.0,
                             anim_speed: float = 1.0) -> None:
        """P7：给选中对象的 mesh 画 inverted-hull 描边。

        选中 node_id 的 mesh 用 outline 程序渲染（背面 + 膨胀），叠加在主渲染之上。
        无选中（selected_nid 空）→ 跳过。

        AUD-7305 fix: 自身导入路径按本帧动画采样（旧代码 ``anim_worlds_src
        or {}`` 恒为绑定姿态，动画播放时描边偏移）。
        AUD-7306 fix: Geo3D 分支用 payload 的 CPU mesh 构建 outline VAO
        （旧代码退化为 procedural VAO，geo payload 在场时恒 None → 选中
        Geo3D 无描边反馈）。
        """
        if not selected_nid or self._render_fbo is None:
            return
        prog = self._ensure_outline_program(ctx)
        # 用主 render FBO（叠加在已渲染的画面上）
        self._render_fbo.use()
        ctx.viewport = (0, 0, w, h)
        try:
            prog['u_proj'].value = m3d.matrix_to_tuple(proj)
            prog['u_view'].value = m3d.matrix_to_tuple(view)
            # 描边色：亮黄（选中视觉反馈）
            prog['u_outline_color'].value = (1.0, 0.85, 0.2, 1.0)
            # 膨胀量：与场景尺度相关（用 cam_dist 估算）
            prog['u_outline_scale'].value = 0.015
            # inverted hull：cull front（只画背面 → 放大的背面形成边缘描边）
            ctx.enable(moderngl.CULL_FACE)
            ctx.cull_face = 'front'  # moderngl 无 FRONT 常量，用字符串
            ctx.disable(moderngl.DEPTH_TEST)  # 描边恒在前（不被自身遮挡）
            # model mesh
            flat_meshes = flat_meshes_src if flat_meshes_src is not None else self._flat_meshes_gl
            if import_ok and flat_meshes:
                anim_worlds = anim_worlds_src
                if anim_worlds is None:
                    anim_worlds = self._sample_animation_worlds(
                        anim_enabled, anim_time, anim_speed)
                cur_nid = (getattr(model_payloads[0], "node_id", "")
                           if model_payloads else "")
                hidden = {
                    str(value) for value in getattr(
                        model_payloads[0], "hidden_mesh_ids", ())
                } if model_payloads else set()
                for m in flat_meshes:
                    mesh_nid = str(m.get("node_id", "") or cur_nid)
                    if mesh_nid in hidden:
                        continue
                    if selected_nid not in (cur_nid, mesh_nid):
                        continue
                    # AUD-7303: key 含 id(mesh)，重名 mesh 不复用错误几何
                    vao = self._get_outline_vao(
                        ctx, m['mesh'], f"{m['node_name']}:{id(m['mesh'])}")
                    if vao is None:
                        continue
                    node_name = m['node_name']
                    wm = anim_worlds.get(node_name, m['world_matrix'])
                    effective_model = (model @ wm).astype(np.float32)
                    prog['u_model'].value = m3d.matrix_to_tuple(effective_model)
                    vao.render(moderngl.TRIANGLES)
            # Geo3D —— payload CPU mesh 构建独立 outline VAO
            for geo_index, gp in enumerate(geo_payloads or []):
                if (getattr(gp, "node_id", "") or "") != selected_nid:
                    continue
                gp_mesh = getattr(gp, "mesh", None)
                gp_model = getattr(gp, "model_matrix", None)
                if gp_mesh is None or gp_model is None:
                    continue
                id_vao = self._get_outline_vao(
                    ctx, gp_mesh, f"geo:{geo_index}:{id(gp_mesh)}")
                if id_vao is not None:
                    _gmode = (moderngl.LINES if getattr(gp, "is_lines", False)
                              else moderngl.TRIANGLES)
                    prog['u_model'].value = m3d.matrix_to_tuple(gp_model)
                    id_vao.render(_gmode)
        finally:
            ctx.enable(moderngl.DEPTH_TEST)
            ctx.disable(moderngl.CULL_FACE)
            ctx.cull_face = 'back'  # 恢复默认（避免状态泄漏到后续渲染）

    def _release_render_target(self) -> None:
        """释放渲染目标（color texture + depth renderbuffer + FBO + MSAA 资源）。"""
        if self._render_fbo is not None:
            self._render_fbo.release()
            self._render_fbo = None
        if self._depth_rb is not None:
            self._depth_rb.release()
            self._depth_rb = None
        if self._gpu_texture is not None:
            self._gpu_texture.release()
            self._gpu_texture = None
        # MSAA（msaa>0）：多重采样 color 纹理 + resolve FBO。
        # _msaa_color 是 _render_fbo 的附件，上面 release 可能已连带释放，
        # 这里显式持有引用再 release，稳妥（ModernGL 对已释放对象再 release 抛异常 → 吞）。
        if self._msaa_color is not None:
            try: self._msaa_color.release()
            except Exception: pass
            self._msaa_color = None
        if self._resolve_fbo is not None:
            try: self._resolve_fbo.release()
            except Exception: pass
            self._resolve_fbo = None
        self._msaa_samples = 0
        # P1-4：navigation 降采样渲染目标（nav 态专用）。
        if self._nav_render_color is not None:
            try: self._nav_render_color.release()
            except Exception: pass
            self._nav_render_color = None
        if self._nav_render_fbo is not None:
            try: self._nav_render_fbo.release()
            except Exception: pass
            self._nav_render_fbo = None
        self._nav_render_scale = 1.0
        # 深度合成（阶段六）：R32F 深度纹理 + depth-only FBO + depth-write VAO
        if self._depth_texture is not None:
            self._depth_texture.release()
            self._depth_texture = None
        if self._depth_fbo is not None:
            self._depth_fbo.release()
            self._depth_fbo = None
        for vao in self._depth_write_vaos.values():
            if vao is not None:
                try: vao.release()
                except Exception: pass
        self._depth_write_vaos = {}
        for buffers in self._depth_write_buffers.values():
            for buffer in buffers:
                if buffer is not None:
                    try: buffer.release()
                    except Exception: pass
        self._depth_write_buffers = {}
        for vao in self._shadow_write_vaos.values():
            if vao is not None:
                try: vao.release()
                except Exception: pass
        self._shadow_write_vaos = {}
        for buffers in self._shadow_write_buffers.values():
            for buffer in buffers:
                if buffer is not None:
                    try: buffer.release()
                    except Exception: pass
        self._shadow_write_buffers = {}
        self._release_shadow_targets()
        # P0-07：几何 AOV 纹理/FBO/VAO 与主 render target 同步释放，避免
        # 切换 AOV 设置后旧 pass 继续被 Viewer 读取或占用显存。
        for fbo in self._aov_fbos.values():
            if fbo is not None:
                try: fbo.release()
                except Exception: pass
        for texture in self._aov_textures.values():
            if texture is not None:
                try: texture.release()
                except Exception: pass
        self._aov_fbos = {}
        self._aov_textures = {}
        for depth_rb in self._aov_depth_rbs.values():
            if depth_rb is not None:
                try: depth_rb.release()
                except Exception: pass
        self._aov_depth_rbs = {}
        for vao in self._aov_write_vaos.values():
            if vao is not None:
                try: vao.release()
                except Exception: pass
        self._aov_write_vaos = {}
        for buffers in self._aov_write_buffers.values():
            for buffer in buffers:
                if buffer is not None:
                    try: buffer.release()
                    except Exception: pass
        self._aov_write_buffers = {}
        for vao in self._motion_write_vaos.values():
            if vao is not None:
                try: vao.release()
                except Exception: pass
        self._motion_write_vaos = {}
        for buffers in self._motion_write_buffers.values():
            for buffer in buffers:
                if buffer is not None:
                    try: buffer.release()
                    except Exception: pass
        self._motion_write_buffers = {}
        # P7：ID picking 纹理 + FBO + VAO（与渲染目标同生命周期）
        if self._id_texture is not None:
            self._id_texture.release()
            self._id_texture = None
        if self._id_fbo is not None:
            self._id_fbo.release()
            self._id_fbo = None
        for vao in self._id_write_vaos.values():
            if vao is not None:
                try: vao.release()
                except Exception: pass
        self._id_write_vaos = {}
        # AUD-7301: id-write VAO 自建 VBO/IBO 配对释放（VAO.release 不连带）
        for buffers in self._id_write_buffers.values():
            for buffer in buffers:
                if buffer is not None:
                    try: buffer.release()
                    except Exception: pass
        self._id_write_buffers = {}
        # P7：outline VAO（与渲染目标同生命周期）
        for vao in self._outline_vaos.values():
            if vao is not None:
                try: vao.release()
                except Exception: pass
        self._outline_vaos = {}
        # AUD-7301: outline VAO 自建 VBO/IBO 配对释放
        for buffers in self._outline_buffers.values():
            for buffer in buffers:
                if buffer is not None:
                    try: buffer.release()
                    except Exception: pass
        self._outline_buffers = {}
        # AUD-7302: LOD VAO 缓存 + MeshVBOPool 引用计数回退
        self._release_lod_vao_cache()

    # ── 求值 ──

    def _release_ortho_views(self, ctx=None) -> None:
        """释放正交视图纹理 + FBO + depth renderbuffer（quad 关闭或内容变化重建时调）。"""
        for key in list(self._ortho_textures.keys()):
            tex = self._ortho_textures.get(key)
            if tex is not None:
                try: tex.release()
                except Exception: pass
                self._ortho_textures[key] = None
            fbo = self._ortho_fbos.get(key)
            if fbo is not None:
                try: fbo.release()
                except Exception: pass
                self._ortho_fbos[key] = None
            # AUD-7300: depth attachment 随纹理/FBO 一并释放（先 FBO 后 RB，
            # 避免 GL 侧 attachment 悬挂）。
            depth_rb = self._ortho_depth_rbs.get(key)
            if depth_rb is not None:
                try: depth_rb.release()
                except Exception: pass
                self._ortho_depth_rbs[key] = None

    def _render_ortho_views(self, ctx, w, h, cam_target, cam_dist,
                            proj_persp, view_persp, eye_persp, model,
                            prog, geo_payloads, model_payloads,
                            dirs_t, cols_t, ints_t, light_count,
                            irradiance_cube, prefilter_cube, brdf_lut,
                            env_int_val, prefilter_max_lod,
                            instance_enabled, instance_count,
                            show_grid, show_axes, cam_pitch,
                            bgr, bgg, bgb, bg_opaque,
                            ocr, ocg, ocb, metallic, roughness,
                            cam_fov: float = 50.0,
                            light_types_t=None, light_pos_t=None,
                            light_range_t=None, light_inner_t=None,
                            light_outer_t=None,
                            ambient_color=(0.0, 0.0, 0.0),
                            ambient_intensity: float = 0.0,
                            grid_cell_size: float = 0.35,
                            grid_opacity: float = 0.25,
                            grid_line_width: float = 0.1,
                            grid_minor_fade_start: float = 120.0,
                            grid_minor_fade_end: float = 1200.0,
                            grid_minor_fade_slope: float = 0.55,
                            grid_major_fade_start: float = 80.0,
                            grid_major_fade_end: float = 520.0,
                            grid_major_fade_slope: float = 0.75,
                            grid_minor_color: tuple[float, float, float] = (0.10, 0.10, 0.12),
                            grid_major_color: tuple[float, float, float] = (0.20, 0.20, 0.23),
                            skip_views: Optional[set] = None,
                            solid_views: Optional[set] = None) -> None:
        """渲染 TOP/RIGHT/FRONT 三个正交视图到独立纹理（quad view）。

        复用透视视图已上传的几何（VAO）+ IBL + 灯光，每个正交视图换 ortho proj +
        对应 look_at view 重新执行 draw call。Model3D 正交视图和透视视图一样
        重新绑定导入贴图与材质参数，保证 quad view 中也能看到真实材质。

        正交视图尺寸为透视纹理的 1/2（象限只占半屏，省填充率）。ortho scale 匹配
        透视在 cam_dist 处的可见半高（cam_dist*tan(fov/2)），使四个视图里 target
        处的物体大小一致。

        Milestone C / P2：skip_views 指定本次要**跳过重渲染**的视图名集合
        （如 {"top","right"}）。被跳过的视图保留上一帧纹理（不清空、不重建），
        实现"非活动视图保留上一帧"（P2-5）——导航态时只活动视图高频更新。
        被 skip 的视图纹理若已存在则原样保留；不存在（首次/尺寸变）则仍需渲染。

        P2-3：solid_views 指定本次要**降质渲染**（solid/标量 PBR，不绑材质贴图）
        的视图名集合。与 skip_views 互补：skip=完全不渲染，solid=渲染但跳过贴图。
        quad_view_quality=active_textured 时，非活动正交视图进 solid_views
        （活动视图保留完整贴图，非活动省 texture bind/fetch 开销）。
        """
        # 正交视图尺寸（1/2 项目尺寸，向下取偶数避免子像素问题）
        ow = max(2, (w // 2))
        oh = max(2, (h // 2))
        # ortho scale 匹配透视在 cam_dist 处的可见半高（cam_dist*tan(fov/2)），
        # 使四视图里 target 处物体大小与透视一致。
        ortho_scale = m3d.ortho_scale_for_perspective_match(cam_dist, cam_fov)
        aspect = (ow / oh) if oh > 0 else 1.0
        # 正交 near/far 覆盖场景（相机在目标点一侧 dist 处，给足纵深）
        ortho_near = 0.1
        ortho_far = 1000.0

        # 三个正交视图的相机配置（eye 相对 target 偏移；dist 用足够大的值避免被近裁剪）
        _ORTHO_DIST = max(50.0, ortho_far * 0.5)
        views = {
            "top": dict(
                eye=m3d.vec3(cam_target[0], cam_target[1] + _ORTHO_DIST, cam_target[2]),
                target=cam_target,
                up=m3d.vec3(0.0, 0.0, -1.0),  # Y 向下看，屏幕上 = -Z
            ),
            "right": dict(
                eye=m3d.vec3(cam_target[0] + _ORTHO_DIST, cam_target[1], cam_target[2]),
                target=cam_target,
                up=m3d.vec3(0.0, 1.0, 0.0),   # X 向右看（从+X看向原点），屏幕上 = +Y
            ),
            "front": dict(
                eye=m3d.vec3(cam_target[0], cam_target[1], cam_target[2] + _ORTHO_DIST),
                target=cam_target,
                up=m3d.vec3(0.0, 1.0, 0.0),   # Z 向前看（从+Z看向原点），屏幕上 = +Y
            ),
        }
        ortho_proj = m3d.ortho(ortho_scale, aspect, ortho_near, ortho_far)

        # 尺寸变化则重建所有正交纹理/FBO
        if self._ortho_size != (ow, oh):
            self._release_ortho_views(ctx)
            self._ortho_size = (ow, oh)

        clear_alpha = 1.0 if bg_opaque else 0.0

        # Milestone D / P3：正交视图渲染前重置材质状态缓存，避免跨视图状态串场
        # （§7.4 风险2：多视图共享 program 时 cache 不能跨视图延续）。
        self._material_cache.reset()
        # P8-2 修复：正交视图段禁用 shared-uniforms view 级缓存。view_sig 是用透视
        # 矩阵算的（在 Path A 之前），正交视图用 ortho_proj/o_view，proj/view 不同。
        # 若不禁用，top/right/front 三个视图同 program 时 cache 误命中（都用透视
        # view_sig），导致 right/front 用 top 的矩阵渲染 → 四视图显示同一角度。
        # 正交视图 mesh 数通常少，cache 收益小，正确性优先。
        _saved_view_sig = self._shared_uniforms_view_sig
        self._shared_uniforms_view_sig = None
        self._shared_uniforms_cache.clear()

        for key, cfg in views.items():
            _v_t0 = time.perf_counter()
            # 懒建该视图的纹理 + FBO
            tex = self._ortho_textures.get(key)
            fbo = self._ortho_fbos.get(key)
            depth_rb = self._ortho_depth_rbs.get(key)
            if tex is None or fbo is None or depth_rb is None:
                # 三者同生同灭（创建/释放都在一处）；若出现部分缺失（例如
                # 旧版本遗留状态），先释放幸存者再整体重建，避免孤儿对象。
                if tex is not None:
                    try: tex.release()
                    except Exception: pass
                    self._ortho_textures[key] = None
                if fbo is not None:
                    try: fbo.release()
                    except Exception: pass
                    self._ortho_fbos[key] = None
                if depth_rb is not None:
                    try: depth_rb.release()
                    except Exception: pass
                    self._ortho_depth_rbs[key] = None
                tex = ctx.texture((ow, oh), 4, dtype='f4')
                tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
                depth_rb = ctx.depth_renderbuffer((ow, oh))
                fbo = ctx.framebuffer(color_attachments=[tex], depth_attachment=depth_rb)
                self._ortho_textures[key] = tex
                self._ortho_fbos[key] = fbo
                # AUD-7300: 记账 depth renderbuffer，_release_ortho_views 据此释放。
                self._ortho_depth_rbs[key] = depth_rb

            # Milestone C / P2：跳过非活动正交视图的重渲染（导航态保活动视图流畅）。
            # 仅当该视图纹理已存在（上一帧渲染过）才跳过——首次/尺寸变化后必须渲染，
            # 否则非活动视图会一直空白（P2-5：保留上一帧，不清空）。
            if skip_views and key in skip_views and tex is not None:
                continue

            o_view = m3d.look_at(cfg["eye"], cfg["target"], cfg["up"])
            o_eye = cfg["eye"]
            o_cam_pos = (float(o_eye[0]), float(o_eye[1]), float(o_eye[2]))

            fbo.use()
            ctx.viewport = (0, 0, ow, oh)
            ctx.clear(bgr, bgg, bgb, clear_alpha, depth=1.0)
            ctx.enable(moderngl.DEPTH_TEST)
            try:
                # 共享 PBR uniform（用正交 proj/view + 正交相机位置）
                self._set_shared_uniforms(
                    prog, ortho_proj, o_view, o_cam_pos,
                    dirs_t, cols_t, ints_t, light_count,
                    irradiance_cube, prefilter_cube, brdf_lut,
                    env_int_val, prefilter_max_lod)

                # ── 渲染 procedural 几何（内置 cube 等）──
                if self._mesh_vao is not None:
                    prog['u_model'].value = m3d.matrix_to_tuple(model)
                    prog['u_albedo'].value = (ocr, ocg, ocb)
                    prog['u_metallic'].value = metallic
                    prog['u_roughness'].value = roughness
                    prog['u_emissive'].value = (0.0, 0.0, 0.0)
                    prog['u_has_base_color_map'].value = 0
                    prog['u_has_normal_map'].value = 0
                    prog['u_has_orm_map'].value = 0
                    prog['u_has_ao_map'].value = 0
                    prog['u_has_emissive_map'].value = 0
                    prog['u_emissive_intensity'].value = 1.0
                    prog['u_normal_format'].value = 0
                    self._bind_null_material_samplers(prog)
                    mode = moderngl.LINES if self._mesh_is_lines else moderngl.TRIANGLES
                    if instance_enabled and self._mesh_instance_enabled and not self._mesh_is_lines:
                        self._mesh_vao.render(mode, instances=instance_count)
                    else:
                        self._mesh_vao.render(mode)

                # ── 渲染连接的 Geo3D payload（程序化几何）──
                for _gp in geo_payloads:
                    geo_prog = _gp.program
                    if geo_prog is None:
                        geo_prog = (self._mesh_instanced_prog
                                    if _gp.variant == "instanced"
                                    else self._mesh_prog) or prog
                    if geo_prog is None:
                        continue
                    self._set_shared_uniforms(
                        geo_prog, ortho_proj, o_view, o_cam_pos,
                        dirs_t, cols_t, ints_t, light_count,
                        irradiance_cube, prefilter_cube, brdf_lut,
                        env_int_val, prefilter_max_lod)
                    _gmat = _gp.material or {}
                    _galbedo = _gmat.get("albedo", (ocr, ocg, ocb))
                    geo_prog['u_model'].value = m3d.matrix_to_tuple(_gp.model_matrix)
                    geo_prog['u_albedo'].value = (float(_galbedo[0]), float(_galbedo[1]), float(_galbedo[2]))
                    geo_prog['u_metallic'].value = float(_gmat.get("metallic", metallic))
                    geo_prog['u_roughness'].value = max(0.01, float(_gmat.get("roughness", roughness)))
                    geo_prog['u_emissive'].value = (0.0, 0.0, 0.0)
                    geo_prog['u_has_base_color_map'].value = 0
                    geo_prog['u_has_normal_map'].value = 0
                    geo_prog['u_has_orm_map'].value = 0
                    geo_prog['u_has_ao_map'].value = 0
                    geo_prog['u_has_emissive_map'].value = 0
                    geo_prog['u_emissive_intensity'].value = 1.0
                    geo_prog['u_normal_format'].value = 0
                    self._bind_null_material_samplers(geo_prog)
                    _gmode = moderngl.LINES if _gp.is_lines else moderngl.TRIANGLES
                    if _gp.instance_enabled and not _gp.is_lines:
                        _gp.vao.render(_gmode, instances=_gp.instance_count)
                    else:
                        _gp.vao.render(_gmode)

                # ── 渲染 Model3D payload 的 flat_meshes（导入模型，用其自带 program）──
                for _payload in model_payloads:
                    _material_overrides = self._material_override_map(_payload)
                    for m in _payload.flat_meshes:
                        mesh_prog = m.get('program')
                        if mesh_prog is None:
                            mesh_prog = (self._mesh_skinned_prog if m['is_skinned']
                                         else (self._mesh_instanced_prog if m.get('can_instance')
                                               else self._mesh_prog))
                        if mesh_prog is None:
                            continue
                        node_name = m['node_name']
                        wm = _payload.anim_worlds.get(node_name, m['world_matrix'])
                        effective_model = (model @ wm).astype(np.float32)
                        self._set_shared_uniforms(
                            mesh_prog, ortho_proj, o_view, o_cam_pos,
                            dirs_t, cols_t, ints_t, light_count,
                            irradiance_cube, prefilter_cube, brdf_lut,
                            env_int_val, prefilter_max_lod)
                        mesh_prog['u_model'].value = m3d.matrix_to_tuple(effective_model)
                        mat = self._material_with_override(
                            m.get('material'), m, _material_overrides)
                        if mat is not None:
                            mesh_prog['u_albedo'].value = (float(mat.base_color[0]),
                                                            float(mat.base_color[1]),
                                                            float(mat.base_color[2]))
                            mesh_prog['u_metallic'].value = float(mat.metallic)
                            mesh_prog['u_roughness'].value = max(0.01, float(mat.roughness))
                            mesh_prog['u_emissive'].value = (float(mat.emissive[0]),
                                                              float(mat.emissive[1]),
                                                              float(mat.emissive[2]))
                        else:
                            mesh_prog['u_albedo'].value = (ocr, ocg, ocb)
                            mesh_prog['u_metallic'].value = metallic
                            mesh_prog['u_roughness'].value = roughness
                            mesh_prog['u_emissive'].value = (0.0, 0.0, 0.0)
                        material_params = (_payload.material_params
                                           if _payload.material_params else {})
                        texture_overrides = _payload.texture_overrides or {}
                        model_dir = (os.path.dirname(_payload.model_path)
                                     if _payload.model_path else "")
                        self._bind_null_material_samplers(mesh_prog)
                        # P2-3：solid_views 降质——非活动正交视图跳过材质贴图绑定 +
                        # 强制 u_has_*_map=0（solid/标量 PBR），省 texture bind/fetch。
                        # 活动视图保留完整贴图（_has_map 按实际材质设值）。
                        _is_solid_view = bool(solid_views and key in solid_views)
                        if _is_solid_view:
                            mesh_prog['u_has_base_color_map'].value = 0
                            mesh_prog['u_has_normal_map'].value = 0
                            mesh_prog['u_has_orm_map'].value = 0
                            mesh_prog['u_has_ao_map'].value = 0
                            mesh_prog['u_has_emissive_map'].value = 0
                            mesh_prog['u_emissive_intensity'].value = float(
                                material_params.get("emissive_intensity", 1.0))
                            mesh_prog['u_normal_format'].value = (
                                1 if material_params.get("normal_format") == "DirectX" else 0)
                        else:
                            _has_map = self._bind_material_textures(
                                ctx, mesh_prog, mat, texture_overrides, material_params, model_dir)
                            mesh_prog['u_has_base_color_map'].value = int(_has_map["base_color"])
                            # Imported model VAOs do not include tangent attributes yet.
                            mesh_prog['u_has_normal_map'].value = 0
                            mesh_prog['u_has_orm_map'].value = int(_has_map["orm"])
                            mesh_prog['u_has_ao_map'].value = int(_has_map["ao"])
                            mesh_prog['u_has_emissive_map'].value = int(_has_map["emissive"])
                            mesh_prog['u_emissive_intensity'].value = float(
                                material_params.get("emissive_intensity", 1.0))
                            mesh_prog['u_normal_format'].value = (
                                1 if material_params.get("normal_format") == "DirectX" else 0)
                        if instance_enabled and m.get('can_instance'):
                            m['vao'].render(moderngl.TRIANGLES, instances=instance_count)
                        else:
                            m['vao'].render(moderngl.TRIANGLES)
                        if not _is_solid_view and any(_has_map.values()):
                            self._unbind_material_textures()

                # ── 网格 + 坐标轴（正交视图适配平面网格，避免边视消失）──
                # 每个视图显示自己「地面」方向的网格：top→XZ, front→XY, right→YZ。
                if show_grid or show_axes:
                    if self._gizmo is None:
                        self._gizmo = GizmoRenderer()
                    _grid_plane = {"top": "xz", "front": "xy", "right": "yz"}.get(key, "xz")
                    # 兼容旧 draw 签名保留 cam_pitch；网格亮度不再按俯仰角压暗。
                    _ortho_pitch = 90.0 if key == "top" else 0.0
                    self._gizmo.draw(ctx, ortho_proj, o_view,
                                     cam_target=cam_target,
                                     show_grid=show_grid, show_axes=show_axes,
                                     cam_pitch_deg=_ortho_pitch,
                                     grid_plane=_grid_plane,
                                     grid_cell_size=grid_cell_size,
                                     grid_opacity=grid_opacity,
                                     grid_line_width=grid_line_width,
                                     grid_minor_fade_start=grid_minor_fade_start,
                                     grid_minor_fade_end=grid_minor_fade_end,
                                     grid_minor_fade_slope=grid_minor_fade_slope,
                                     grid_major_fade_start=grid_major_fade_start,
                                     grid_major_fade_end=grid_major_fade_end,
                                     grid_major_fade_slope=grid_major_fade_slope,
                                     grid_minor_color=grid_minor_color,
                                     grid_major_color=grid_major_color)
            finally:
                ctx.disable(moderngl.DEPTH_TEST)
            # ── 本正交视图耗时统计（P0）──
            _v_dt = (time.perf_counter() - _v_t0) * 1000.0
            if key == "top":
                self._perf_stats.top_ms += _v_dt
            elif key == "right":
                self._perf_stats.right_ms += _v_dt
            elif key == "front":
                self._perf_stats.front_ms += _v_dt
            # 正交视图的 draw call 计入 draw_calls（与透视累加）
            self._perf_stats.fbo_switches += 1
            # P2-4：记录本视图本次渲染的时间戳（供降频间隔判定用）。
            self._ortho_last_render[key] = time.monotonic()
        # P8-2 修复：恢复 view_sig（正交视图段已结束，供下一帧/其他路径用）。
        self._shared_uniforms_view_sig = _saved_view_sig

    def _single_ortho_camera(self, view_kind: int, cam_target, cam_dist: float,
                             aspect: float, cam_fov: float = 50.0) -> tuple:
        """返回单视图正交相机的 (proj, view, eye)（与 _render_ortho_views 配置一致）。

        view_kind: 1=top, 2=right, 3=front。ortho_scale 匹配透视在 cam_dist 处的
        可见半高（cam_dist*tan(fov/2)），使正交单视图与透视视图物体大小一致。
        全分辨率 aspect 由调用方传（= w/h）。
        """
        _ORTHO_DIST = 500.0  # 与 _render_ortho_views 的 max(50, far*0.5) 一致
        ortho_scale = m3d.ortho_scale_for_perspective_match(cam_dist, cam_fov)
        proj = m3d.ortho(ortho_scale, aspect, 0.1, 1000.0)
        if view_kind == 1:  # top
            eye = m3d.vec3(cam_target[0], cam_target[1] + _ORTHO_DIST, cam_target[2])
            up = m3d.vec3(0.0, 0.0, -1.0)
        elif view_kind == 2:  # right
            eye = m3d.vec3(cam_target[0] + _ORTHO_DIST, cam_target[1], cam_target[2])
            up = m3d.vec3(0.0, 1.0, 0.0)
        else:  # front (3)
            eye = m3d.vec3(cam_target[0], cam_target[1], cam_target[2] + _ORTHO_DIST)
            up = m3d.vec3(0.0, 1.0, 0.0)
        view = m3d.look_at(eye, cam_target, up)
        return proj, view, eye

    # ── Milestone B / P1：Navigation LOD 质量档位解析 ──

    @staticmethod
    def _resolve_quality_profile(eval_ctx, render_mode: str,
                                 model_payloads) -> ViewportQualityProfile:
        """根据交互态 / 渲染模式 / 模型规模决定本帧质量档位。

        决策顺序（master_plan §5.4 + §18）：
            1. render_mode=full/export → FINAL（合成输出，必须完整质量）。
            2. interaction_active=True：
               - 大模型（>= LARGE_MODEL_TRIANGLE_THRESHOLD）→ FAST_SOLID
               - 否则 → NAVIGATION
            3. 否则 → PREVIEW（静止预览）。

        这样做的好处：
            - 合成输出（导出/最终渲染）绝不被导航 LOD 污染成低质量
              （master_plan §5.5 注意点 2）。
            - 大模型拖动时自动降到 solid，保响应（§15.1 Large Model Auto LOD）。
            - 静止时恢复完整 PBR/IBL（恢复由 interaction_active 转为 False 触发，
              CompositeViewer 端用 restore timer 控制；见 P1-5）。
        """
        # 合成输出档位：full/export 必须 FINAL（不被导航降载污染）
        if str(render_mode) in ("full", "export"):
            return ViewportQualityProfile.FINAL
        # 无 eval_ctx（理论上不会发生在 Scene3D，兜底）→ PREVIEW
        if eval_ctx is None:
            return ViewportQualityProfile.PREVIEW
        # P2-04: a project-level draft request must remain visibly draft even
        # when it is rendered while the pointer is idle. Navigation is the
        # existing diffuse/no-IBL profile and is the least surprising mapping
        # for a reduced-quality still preview.
        if str(getattr(eval_ctx, "quality_profile", "")).lower() == "draft":
            return ViewportQualityProfile.NAVIGATION
        if not getattr(eval_ctx, "interaction_active", False):
            return ViewportQualityProfile.PREVIEW
        # 导航态：按模型规模决定 NAVIGATION vs FAST_SOLID
        tri_count = 0
        if model_payloads:
            for mp in model_payloads:
                if getattr(mp, "has_model", False):
                    for fm in (getattr(mp, "flat_meshes", None) or []):
                        if isinstance(fm, dict):
                            tri_count += int(fm.get("index_count", 0)) // 3
        if tri_count >= LARGE_MODEL_TRIANGLE_THRESHOLD:
            return ViewportQualityProfile.FAST_SOLID
        return ViewportQualityProfile.NAVIGATION

    def redraw_with_view(
            self,
            cam_params: Dict[str, Any],
            eval_ctx: Any,
            fb_manager: Any,
            resource_pool: Any = None,
            render_mode: str = "preview",
    ) -> Optional[TextureResult]:
        """Redraw a cached Scene3D view without evaluating upstream nodes.

        This method is render-thread-only. Scene payloads, mesh resources and
        render targets are retained by the previous full evaluation; only the
        built-in camera parameters are replaced before reusing the normal
        Scene3D draw implementation. A missing cache/context returns ``None``
        so the renderer can fall back to a complete graph evaluation.

        AUD-7312: navigation cam overrides are written into
        ``self.parameters`` ONLY for the virtual viewer scene
        (``id == "__viewer3d_scene__"``) — the established injection mode
        for that node (its ``_do_evaluate`` reads navigation values straight
        from ``self.parameters``).  Real Scene3D nodes read their camera from
        ``eval_ctx.global_3d_scene_settings`` (set fresh by the renderer for
        every redraw request), so writing nav values into a real node's
        parameters would be inert for rendering while polluting the shared
        dict: the values would leak into serialization / undo snapshots and
        race with UI-thread parameter commits.
        """
        if (
            self._last_scene_content_sig is None
            or self._gpu_texture is None
            or eval_ctx is None
            or getattr(eval_ctx, "scene_connections", None) is None
        ):
            return None
        if not isinstance(cam_params, dict):
            return None

        # AUD-7312: only the virtual viewer scene owns nav values in
        # self.parameters (see docstring).
        is_virtual_viewer = (self.id == "__viewer3d_scene__")
        for key in (
            "cam_orbit_yaw", "cam_orbit_pitch", "cam_dist",
            "cam_target_x", "cam_target_y", "cam_target_z",
        ):
            if key not in cam_params:
                continue
            try:
                value = float(cam_params[key])
            except (TypeError, ValueError):
                return None
            if is_virtual_viewer:
                self.parameters[key] = value

        outputs = self._do_evaluate(
            {}, eval_ctx, fb_manager,
            resource_pool=resource_pool,
            render_mode=render_mode,
        )
        result = outputs.get("display") if isinstance(outputs, dict) else None
        return result if isinstance(result, TextureResult) else None

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """渲染 3D 场景到 HDR 纹理。

        全程在渲染线程的 ModernGL context 内完成。读取的参数已由
        evaluator 按当前 frame 插值（base_evaluator.py:_interpolate_parameters），
        因此相机/物体动画自动生效。
        """
        # P0：整段 _do_evaluate 的 CPU 墙钟计时起点（含 cache-hit 帧）。
        _do_evaluate_t0 = time.perf_counter()
        # 解包 ModernGL context（source 节点收到 CompositeEvalContext）。
        # 阶段七：保留原始 ctx 引用（CompositeEvalContext 带 scene_payloads 旁路），
        # payload 收集必须用原始 ctx，不能用解包后的裸 moderngl.Context。
        from ..core.eval_context import CompositeEvalContext
        eval_ctx = ctx if isinstance(ctx, CompositeEvalContext) else None
        if eval_ctx is not None:
            ctx = ctx.gl
        self._last_gl_ctx = ctx

        # ── 0. 场景功能参数来源判定 ──
        # Scene3D 是纯渲染器，场景功能（相机/灯光/背景/IBL/网格/MSAA/输出控制）
        # 统一来自全局「3D Scene Settings」。但有两种节点，参数来源不同：
        #
        # 1. 虚拟 viewer 场景（id=__viewer3d_scene__）：_inject_viewer3d_scene_snapshot
        #    把全局设置 + Alt 导航实时值都写入了 self.parameters（导航覆盖全局），
        #    所以直接读 self.parameters 即可——与基线一致的路径，导航可靠工作。
        #
        # 2. 真实 Scene3D 节点：self.parameters 只有旧默认值（cam_orbit_yaw=30 等），
        #    必须读 eval_ctx.global_3d_scene_settings 才能拿到用户在 3D Scene
        #    Settings 里配置的值。
        _gs = getattr(eval_ctx, "global_3d_scene_settings", None) if eval_ctx else None
        _vmo = getattr(eval_ctx, "view_mode_overrides", None) if eval_ctx else None
        _my_overrides = _vmo.get(self.id) if (_vmo and self.id) else None
        _is_virtual = (self.id == "__viewer3d_scene__")

        def _sp(key, default=None):
            """场景参数读取。

            虚拟节点：view_mode_overrides > self.parameters（含全局+导航）> default
            真实节点：view_mode_overrides > global_3d_scene_settings > self.parameters > default
            """
            # view_mode_overrides 最高优先级（预览/输出区分的 show_grid/bg_opaque/show_axes）
            if _my_overrides is not None and key in _my_overrides:
                return _my_overrides[key]
            if _is_virtual:
                # 虚拟节点：直接读 self.parameters（_inject 已写入全局+导航值）
                return self.parameters.get(key, default)
            # 真实节点：全局设置优先
            if _gs is not None and key in _gs:
                return _gs[key]
            return self.parameters.get(key, default)

        # ── 1. 读取参数 ──
        w = max(1, int(self.parameters.get("width", 1920)))
        h = max(1, int(self.parameters.get("height", 1080)))
        # Scene3D 是纯渲染器，不再读取/渲染节点自身的内置 primitive。
        primitive = "none"
        has_builtin_primitive = False
        mesh_scale = 1.0
        # 实例化（阶段六：grid 复制器）
        instance_enabled = bool(self.parameters.get("instance_enabled", False))
        grid_x = max(1, int(self.parameters.get("grid_x", 3)))
        grid_z = max(1, int(self.parameters.get("grid_z", 3)))
        spacing = max(0.0, float(self.parameters.get("spacing", 2.0)))
        instance_count = grid_x * grid_z
        # 导入模型（阶段五）
        model_path = str(self.parameters.get("model_path", "") or "")
        use_imported_model = bool(self.parameters.get("use_imported_model", False))
        # 骨骼动画（阶段五）
        anim_enabled = bool(self.parameters.get("anim_enabled", True))
        anim_time = float(self.parameters.get("anim_time", 0.0))
        anim_speed = float(self.parameters.get("anim_speed", 1.0))

        # 旧版内置 primitive 的变换占位；纯 renderer 下固定为单位变换。
        opx = opy = opz = 0.0
        orx = ory = orz = 0.0

        # 相机（虚拟节点读 self.parameters 含导航值；真实节点读全局设置）
        cam_fov = float(_sp("cam_fov", 50.0))
        cam_dist = max(0.1, float(_sp("cam_dist", 5.0)))
        cam_yaw = float(_sp("cam_orbit_yaw", 30.0))
        cam_pitch = float(_sp("cam_orbit_pitch", 20.0))
        cam_tx = float(_sp("cam_target_x", 0.0))
        cam_ty = float(_sp("cam_target_y", 0.0))
        cam_tz = float(_sp("cam_target_z", 0.0))

        # 灯光（来自全局「3D Scene Settings」；阶段一主灯，向后兼容；阶段四 4 灯槽位）
        ldx = float(_sp("light_dir_x", -0.6))
        ldy = float(_sp("light_dir_y", 0.7))
        ldz = float(_sp("light_dir_z", 0.4))
        light_intensity = max(0.0, float(_sp("light_intensity", 2.0)))
        # 默认灯光颜色（全局设置新增；旧路径回退白色）
        lcr = float(_sp("light_color_r", 1.0))
        lcg = float(_sp("light_color_g", 1.0))
        lcb = float(_sp("light_color_b", 1.0))

        # 多灯光（4 槽位）。light0 默认 intensity=0，由旧 light_dir_*/light_intensity 接管，
        # 保持已有场景默认外观不变（向后兼容）。
        lights = []  # list[(dir_xyz, color_rgb, intensity)]
        for i in range(4):
            li = max(0.0, float(self.parameters.get(f"light{i}_intensity", 0.0)))
            if li <= 0.0 and i > 0:
                # 仅 light0 在 intensity=0 时回退到旧参数（保持兼容）
                continue
            if i == 0 and li <= 0.0:
                # light0 关闭 → 用旧主灯（颜色取自全局设置 light_color_*）
                d = m3d.normalize(m3d.vec3(ldx, ldy, ldz))
                lights.append(((float(d[0]), float(d[1]), float(d[2])),
                               (lcr, lcg, lcb), light_intensity))
            else:
                d = m3d.normalize(m3d.vec3(
                    float(self.parameters.get(f"light{i}_dir_x", 0.0)),
                    float(self.parameters.get(f"light{i}_dir_y", 1.0)),
                    float(self.parameters.get(f"light{i}_dir_z", 0.0)),
                ))
                lights.append(((float(d[0]), float(d[1]), float(d[2])),
                               (float(self.parameters.get(f"light{i}_color_r", 1.0)),
                                float(self.parameters.get(f"light{i}_color_g", 1.0)),
                                float(self.parameters.get(f"light{i}_color_b", 1.0))),
                               li))

        # 缺省 PBR 材质，仅作为 Geo3D/Model3D payload 缺省材质回退。
        metallic = 0.0
        roughness = 0.5

        # HDRI 环境（来自全局「3D Scene Settings」；无 Light3D 连接时用此内置 IBL）
        hdri_preset = str(_sp("hdri_preset", "") or "")
        use_ibl = bool(_sp("use_ibl", True))
        env_intensity = max(0.0, float(_sp("env_intensity", 1.0)))

        # 缺省 albedo，仅作为 Geo3D/Model3D payload 缺省材质回退。
        ocr = ocg = ocb = 0.8
        # 背景（来自全局「3D Scene Settings」）
        bg_opaque = bool(_sp("bg_opaque", True))
        bgr_srgb = float(_sp("bg_r", 0.10980392156862745))
        bgg_srgb = float(_sp("bg_g", 0.10980392156862745))
        bgb_srgb = float(_sp("bg_b", 0.10980392156862745))
        # Picker/swatch values are display sRGB. Scene3D renders into a
        # scene-linear HDR texture, and CompositeViewer applies the display ODT.
        # Convert before clear so the chosen UI color round-trips visually.
        bgr = _srgb_to_linear_channel(bgr_srgb)
        bgg = _srgb_to_linear_channel(bgg_srgb)
        bgb = _srgb_to_linear_channel(bgb_srgb)
        # 预乘不变量：alpha=0 时 RGB 必须为 0，否则会污染下游 Merge 的 over 计算
        if not bg_opaque:
            bgr = bgg = bgb = 0.0
        # 场景辅助元素（来自全局「3D Scene Settings」）
        show_grid = bool(_sp("show_grid", True))
        show_axes = bool(_sp("show_axes", True))
        # F6：骨骼 overlay 开关（由 viewport overlay_show_skeleton 注入）
        show_skeleton = bool(_sp("show_skeleton", False))
        # 地面网格视觉参数（来自全局「3D Scene Settings」，仅预览用）。
        grid_cell_size = max(0.1, float(_sp("grid_cell_size", 0.35)))
        grid_opacity = max(0.0, float(_sp("grid_opacity", 0.25)))
        grid_line_width = max(0.01, float(_sp("grid_line_width", 0.1)))
        # Fade distances are intentionally internal now. The visible controls
        # kept feeling ineffective because pixel LOD dominates small-grid
        # visibility, so the shader owns these tuned values.
        grid_minor_fade_start = 120.0
        grid_minor_fade_end = 1200.0
        grid_minor_fade_slope = 0.55
        grid_major_fade_start = 80.0
        grid_major_fade_end = 520.0
        grid_major_fade_slope = 0.75
        grid_minor_color = (
            max(0.0, min(1.0, float(_sp("grid_minor_color_r", 0.10)))),
            max(0.0, min(1.0, float(_sp("grid_minor_color_g", 0.10)))),
            max(0.0, min(1.0, float(_sp("grid_minor_color_b", 0.12)))),
        )
        grid_major_color = (
            max(0.0, min(1.0, float(_sp("grid_major_color_r", 0.20)))),
            max(0.0, min(1.0, float(_sp("grid_major_color_g", 0.20)))),
            max(0.0, min(1.0, float(_sp("grid_major_color_b", 0.23)))),
        )
        # 抗锯齿（MSAA，来自全局「3D Scene Settings」）：钳制到 {0,2,4,8}，非法值→0。
        _msaa_raw = int(_sp("msaa", 4))
        msaa = _msaa_raw if _msaa_raw in (2, 4, 8) else 0
        # P0-07：AOV 按全局设置按需生成。Depth/Object ID 默认开启以保持
        # 现有 DeepMerge 与 3D 选取行为；Normal/Position 默认关闭以避免
        # 未使用时额外占用显存和 fill-rate。
        aov_depth = bool(_sp("aov_depth", True))
        aov_normal = bool(_sp("aov_normal", False))
        aov_position = bool(_sp("aov_position", False))
        aov_uv = bool(_sp("aov_uv", False))
        aov_material_id = bool(_sp("aov_material_id", False))
        aov_motion_vector = bool(_sp("aov_motion_vector", False))
        aov_object_id = bool(_sp("aov_object_id", True))
        # Milestone B / P1：Navigation LOD——按 quality profile 决定降载策略。
        # profile 由 interaction_active + render_mode + 模型规模综合得出：
        #   - render_mode=full/export → FINAL（合成输出不受导航影响，保证质量）
        #   - interaction_active=True + 大模型 → FAST_SOLID（solid/顶点色，最流畅）
        #   - interaction_active=True → NAVIGATION（关 MSAA/IBL，降分辨率，简化 PBR）
        #   - 否则 → PREVIEW
        # 降载项见 QUALITY_PROFILE_PARAMS（msaa_factor/use_ibl/resolution_scale/pbr_mode）。
        # 注意：此处 _model_payloads 尚未收集（§1.5 才收集），先按"无模型"算基础
        # profile；§1.5 收集后若检测到大模型再升级到 FAST_SOLID（见下方 refine）。
        # Milestone B：interactive_lod=False（用户在 3D Scene Settings 关闭交互降载）
        # 时强制 PREVIEW（不降载），尊重用户"要画质不要流畅"的偏好。
        _interactive_lod = bool(_sp("interactive_lod", True))
        if _interactive_lod:
            _qp = self._resolve_quality_profile(eval_ctx, render_mode, None)
        else:
            _qp = (ViewportQualityProfile.FINAL
                   if str(render_mode) in ("full", "export")
                   else ViewportQualityProfile.PREVIEW)
        _qp_params = QUALITY_PROFILE_PARAMS[_qp]
        # MSAA 降级：NAVIGATION/FAST_SOLID 强制 0；其它档位保留用户设置。
        if _qp_params["msaa_factor"] <= 0.0:
            msaa = 0
        elif _qp_params["msaa_factor"] < 1.0:
            # 半量降级（向后兼容旧行为）：8×→2×、4×→0×
            if msaa >= 8:
                msaa = 2
            elif msaa >= 4:
                msaa = 0
        self._lod_active = (_qp in (ViewportQualityProfile.NAVIGATION,
                                    ViewportQualityProfile.FAST_SOLID))
        # Navigation LOD 内部分辨率缩放（0=不缩放，渲染全尺寸后 viewport 缩放；
        # 这里保持渲染目标=项目尺寸，降载体现在 MSAA/IBL/PBR，避免纹理尺寸抖动
        # 导致 _gpu_texture 反复重建 + 下游 letterbox 闪动。resolution_scale 仅
        # 用于诊断统计 + 未来 GPU 受限时的真降采样）。
        # P1-4：真降采样默认关闭（nav_downsample_enabled 设置控制）。虽然降采样能
        # 省 ~58% 片元（0.65²），但跨尺寸 resolve + viewport/aspect 协调复杂，默认
        # 保守不启用。MSAA + IBL gating + backface culling 已提供足够的导航态降压。
        _nav_downsample_on = bool(_sp("nav_downsample_enabled", False))
        _nav_res_scale = (float(_qp_params["resolution_scale"])
                          if (_nav_downsample_on and _qp in (
                              ViewportQualityProfile.NAVIGATION,
                              ViewportQualityProfile.FAST_SOLID))
                          else 1.0)
        # IBL 在 NAVIGATION/FAST_SOLID 关闭（env_int_val=0 → shader 跳过 IBL 采样）
        use_ibl = use_ibl and bool(_qp_params["use_ibl"])
        # P8-3：backface culling 由 quality profile 决定。NAVIGATION/FAST_SOLID=True
        # （闭网格背面不可见，GPU 光栅化早期剔除省 ~50% 片元）。静止态 False 保守。
        _backface_cull = bool(_qp_params.get("backface_culling", False))
        # Milestone B / P1：导航态简化地面网格（§5.3 表格：导航中降低 opacity）。
        # 降半 opacity + 保持 show_grid 开关不变（避免释放时 toggle 闪动）。
        # FAST_SOLID 更激进：几乎隐藏网格（opacity*0.2），让模型轮廓更清晰。
        if _qp == ViewportQualityProfile.FAST_SOLID:
            grid_opacity *= 0.2
        elif _qp == ViewportQualityProfile.NAVIGATION:
            grid_opacity *= 0.5

        # M2: 四视图模式开关（由 RenderRequest.quad_view 经 eval_ctx 同步）。
        # True 时本帧需额外渲染 TOP/RIGHT/FRONT 三个正交视图纹理。
        quad_view = bool(eval_ctx is not None
                         and getattr(eval_ctx, "quad_view", False))
        # M2: 单视图正交 override（0/None=perspective，1=top，2=right，3=front）。
        # 非 0 时主相机改用正交投影（SINGLE 模式下显示该正交视角）。
        _sov_raw = getattr(eval_ctx, "single_ortho_view", None) if eval_ctx is not None else None
        single_ortho_view = int(_sov_raw) if _sov_raw else 0
        # Milestone C / P2：四视图活动视图（0=perspective，1=top，2=right，3=front）。
        # Scene3D 据此在导航态对非活动正交视图降质/跳过（保活动视图流畅）。
        _qav_raw = getattr(eval_ctx, "quad_active_view", None) if eval_ctx is not None else None
        quad_active_view = int(_qav_raw) if _qav_raw else 0
        # P7：选中 / 隔离节点 id（outline / isolate / ID pass 过滤用）。
        _p7_selected_nid = (getattr(eval_ctx, "selected_3d_node_id", None)
                            if eval_ctx is not None else None)
        self._p7_isolate_nid = (getattr(eval_ctx, "isolate_3d_node_id", None)
                                if eval_ctx is not None else None)

        fmt_name = str(self.parameters.get("format_name", "Scene3D") or "Scene3D")

        # ── 1.5 P1.5 端口感知 payload 收集 + signature ──
        # 在 content_sig 构建前先收集已连接的 scene payload 并取其 signature，
        # 这样 Camera3D/Light3D/Geo3D/Model3D 参数变化（签名变化）会触发
        # content_changed → 不复用陈旧 _gpu_texture。
        # payload 对象本身（含 GL 引用）在下面 §3.5 渲染时复用本变量。
        from ..render3d.scene_payload import (
            CameraPayload, LightPayload, GeometryPayload, ModelPayload)
        _payload_ctx = eval_ctx if eval_ctx is not None else ctx
        _cam_payloads = self._collect_connected_scene_payloads(_payload_ctx, CameraPayload)
        _light_payloads = self._collect_connected_scene_payloads(_payload_ctx, LightPayload)
        _geo_payloads = self._collect_connected_scene_payloads(_payload_ctx, GeometryPayload)
        _model_payloads = self._collect_connected_scene_payloads(_payload_ctx, ModelPayload)
        # 活动摄影机选择：active_camera_id 指定时按 payload.node_id 匹配，
        # 找不到（参数过期 / 节点已删）或为空则退化为第一个连接的相机（向后兼容）。
        cam_payload = None
        _active_cam_id = str(_sp("active_camera_id", ""))
        if _active_cam_id and _cam_payloads:
            cam_payload = next(
                (p for p in _cam_payloads
                 if getattr(p, "node_id", "") == _active_cam_id),
                None)
        if cam_payload is None and _cam_payloads:
            cam_payload = _cam_payloads[0]
        # P2 灯光重构：保留所有 LightPayload（一节点一灯），由 §3.5 合并为 shader 数组。
        # 取第一个非空 IBL 三件套作为环境光（多灯场景 IBL 不重复烘焙）。
        light_payloads = _light_payloads if _light_payloads else None
        # 兼容旧变量名（_light3d_ibl 等下游引用）：保留单 payload 引用取 IBL
        light_payload = _light_payloads[0] if _light_payloads else None
        # 多几何：geo + model 都按 geometry_in 连接顺序收集（Scene3D 全部渲染）。
        # 兼容：旧逻辑只取第一个 model has_model；这里保持单 model 语义，
        # 多 geo 渲染留待后续（先保证正确性）。
        model_payload = next((p for p in _model_payloads if p.has_model), None)
        geo_payload = _geo_payloads[0] if _geo_payloads else None
        # Milestone B / P1 refine：收集到模型 payload 后，若导航态且模型够大，
        # 把 profile 从 NAVIGATION 升级到 FAST_SOLID（solid/顶点色，最流畅）。
        # 这是 §15.1 Large Model Auto LOD：超过阈值自动用 solid。仅导航态生效——
        # 静止态和合成输出不受影响（保持 PREVIEW/FINAL 的完整材质）。
        if _qp == ViewportQualityProfile.NAVIGATION and model_payload is not None:
            _tri = 0
            for _fm in (model_payload.flat_meshes or []):
                if isinstance(_fm, dict):
                    _tri += int(_fm.get("index_count", 0)) // 3
            if _tri >= LARGE_MODEL_TRIANGLE_THRESHOLD:
                _qp = ViewportQualityProfile.FAST_SOLID
                _qp_params = QUALITY_PROFILE_PARAMS[_qp]
                # FAST_SOLID 比 NAVIGATION 更激进：强制 MSAA=0（已是 0，保险起见）。
                msaa = 0
                self._lod_active = True
        # payload signature（None 的用占位，保证签名可比且稳定）
        _cam_sig = cam_payload.signature() if cam_payload is not None else ("camera", None)
        # P2：多灯光签名（每个 payload 各自签名，元组组合）
        if light_payloads:
            _light_sig = tuple(p.signature() for p in light_payloads)
        else:
            _light_sig = ("light", None)
        _geo_sig = geo_payload.signature() if geo_payload is not None else ("geo", None)
        _model_sig = model_payload.signature() if model_payload is not None else ("model", None)

        # ── 2. 内容签名（变更才重渲染，仿 ConstantNode）──
        # P6 最小闭环：拆分 scene_content_sig（决定 mesh/材质/IBL/渲染目标是否重建）
        # 与 view_sig（相机/MSAA/quality profile/quad/ortho——只影响矩阵 + draw call）。
        # 仅相机变化时 scene_content_sig 稳定 → 跳过昂贵的资源重建段（§3 渲染目标 /
        # §5 几何上传 / §5b IBL bake / _ensure_model），只重发 draw call（换 u_view/u_proj）。
        # 注意：hdri_path 变化会触发 IBL 重 bake（在签名比较后单独处理）
        scene_content_sig = (
            w, h, primitive, mesh_scale,
            instance_enabled, grid_x, grid_z, spacing,
            use_imported_model, model_path,
            anim_enabled, anim_time, anim_speed,
            opx, opy, opz, orx, ory, orz,
            tuple(lights),
            metallic, roughness,
            use_ibl, env_intensity,
            aov_depth, aov_normal, aov_position, aov_uv, aov_material_id,
            aov_motion_vector,
            aov_object_id,
            ocr, ocg, ocb, bg_opaque, bgr, bgg, bgb,
            show_grid, show_axes,
            # 网格外观参数变化触发重渲染（show_grid/show_axes 只覆盖开关，
            # cell_size/opacity/line_width 覆盖外观，需独立进签名才能实时预览）。
            grid_cell_size, grid_opacity, grid_line_width,
            grid_minor_fade_start, grid_minor_fade_end, grid_minor_fade_slope,
            grid_major_fade_start, grid_major_fade_end, grid_major_fade_slope,
            grid_minor_color, grid_major_color,
            # P1.2 上游 payload signature（Light/Geo/Model 变化触发资源重建）。
            # Camera payload 不进 scene_content（视图变化不重建资源）。
            _light_sig, _geo_sig, _model_sig,
        )
        # view_sig：相机参数 + MSAA + quality profile + quad/ortho + 活动 Camera3D。
        # 这些变化只触发 §4 矩阵重建 + §6 draw call，不重建场景资源。
        view_sig = (
            cam_fov, cam_dist, cam_yaw, cam_pitch, cam_tx, cam_ty, cam_tz,
            msaa, _qp.value,
            quad_view, single_ortho_view,
            _cam_sig, _active_cam_id,
        )
        # 合并签名（供 _last_content_sig 向后兼容 + cache-hit 判定）。
        content_sig = (scene_content_sig, view_sig)
        content_changed = content_sig != self._last_content_sig
        # P6：场景内容变化（资源需重建）。仅相机/视图变化时为 False。
        _scene_content_changed = scene_content_sig != self._last_scene_content_sig
        size_changed = (w, h) != self._last_size

        # ── 安全默认值：以下局部变量仅在 content_changed 块内重新赋值，但 §7 诊断 +
        # §6d 每帧都引用它们。cache-hit 帧（content_changed=False）不进入块内 → 这些变量
        # 必须有默认值，否则 UnboundLocalError 导致 _do_evaluate 整帧崩溃（快速拖拽时
        # 大量 cache-hit 帧崩溃 → viewport 收不到帧 → 显示卡死/空白）。
        # 默认值用「无场景内容」的占位；cache-hit 帧不会重新渲染，只是回传缓存的纹理。
        _use_model_payload = False
        _use_geo_payload = False
        light_count = 0

        # ── Milestone A / P0：本帧性能统计上下文 ──
        # 重置计数器，填充帧标识（profile/layout/active_view/nav/lod/res_scale）。
        # 计时（perspective_ms/top_ms/...）在下方对应渲染段用 ScopedTimer 累加。
        _pstats = self._perf_stats
        self._perf_frame_id += 1
        _pstats.reset(self._perf_frame_id)
        # P8-0：清空本帧 VBO 缓存命中标志（_ensure_flat_meshes_gl 会重新汇总）。
        self._last_vbo_cache_hit = None
        # Milestone D / P3：每帧重置材质状态缓存（连续 mesh 同材质跳过 rebind）。
        # 每视图开头也 reset（_render_ortho_views 内），避免跨视图状态串场。
        self._material_cache.reset()
        # Milestone B / P1：quality_profile 用解析出的 _qp（含 FINAL/NAVIGATION/FAST_SOLID）。
        _pstats.quality_profile = _qp.value
        _pstats.viewport_layout = "quad" if quad_view else "single"
        _sov_map = {1: "top", 2: "right", 3: "front"}
        _pstats.active_view = (_sov_map.get(single_ortho_view, "perspective")
                               if single_ortho_view else "perspective")
        _pstats.navigation_active = bool(
            eval_ctx is not None and getattr(eval_ctx, "interaction_active", False))
        _pstats.lod_level = 1 if self._lod_active else 0
        _pstats.internal_resolution_scale = float(_nav_res_scale)
        _pstats.resolution = (w, h)
        # 内容规模统计（P0-5）：从已收集的 payload 取 mesh/三角形/材质/贴图数。
        # 在此引用是安全的——§1.5 已收集 _model_payloads/_cam_payloads。
        if _model_payloads:
            _mp0 = _model_payloads[0]
            if getattr(_mp0, "has_model", False):
                _fm_list = getattr(_mp0, "flat_meshes", []) or []
                _pstats.mesh_count = len(_fm_list)
                _tri = 0
                _mats = 0
                for _fm in _fm_list:
                    _ic = _fm.get("index_count", 0) if isinstance(_fm, dict) else 0
                    _tri += _ic // 3
                    if isinstance(_fm, dict) and _fm.get("material") is not None:
                        _mats += 1
                _pstats.triangle_count = _tri
                _pstats.material_count = _mats

        # ── 主渲染段计时（perspective：含模型/几何/网格/坐标轴，含 MSAA resolve + 深度 pass）──
        # 注意 perspective_ms 在 cache-hit 帧也累加（仍走后续 ortho 转发等路径，但主
        # 渲染被跳过，所以实际只计 FBO 状态切换之外的开销，接近 0，符合直觉）。

        if content_changed or self._gpu_texture is None or size_changed:
            # P6：判断是否需要重建渲染目标。FBO/resolve/depth targets 只绑定尺寸
            # 和 MSAA；灯光、材质、动画、物体 transform、相机变化都只需要重画，
            # 不应释放并重建 render target 或 mesh VAO。
            _msaa_changed = (msaa != self._msaa_samples)
            # P1-4：nav_res_scale 变化也要重建渲染目标（nav↔static 切换）。
            _nav_scale_changed = (abs(_nav_res_scale - self._nav_render_scale) > 0.01)
            _requested_aov_names = {
                name for name, enabled in (
                    ("normal", aov_normal),
                    ("position", aov_position),
                    ("uv", aov_uv),
                    ("material_id", aov_material_id),
                    ("motion_vector", aov_motion_vector),
                ) if enabled
            }
            _aov_targets_changed = (
                set(self._aov_textures.keys()) != _requested_aov_names
                or bool(self._depth_texture is not None) != bool(aov_depth)
                or bool(self._id_texture is not None) != bool(aov_object_id)
            )
            _rebuild_targets = (
                size_changed or _msaa_changed or _nav_scale_changed
                or self._gpu_texture is None or _aov_targets_changed
            )
            # ── 3. 释放旧渲染目标，重建（仅 _rebuild_targets 时）──
            if _rebuild_targets:
                if self._gpu_texture is not None:
                    self._release_render_target()
                    self._cache_invalidated_needed = True

                # P1-4：计算渲染目标实际尺寸。nav 态降采样（最小 2 像素防退化），
                # 静止态全尺寸。_gpu_texture（输出）始终全尺寸，渲染后上采样。
                self._nav_render_scale = _nav_res_scale
                if _nav_res_scale < 1.0:
                    _rw = max(2, int(w * _nav_res_scale))
                    _rh = max(2, int(h * _nav_res_scale))
                else:
                    _rw, _rh = w, h
                # 存实例字段：渲染段（块外）+ cache-hit 帧需要读实际渲染尺寸。
                self._render_target_w = _rw
                self._render_target_h = _rh

                # 最终输出纹理（resolve 目标 / msaa=0 时即渲染目标）—— HDR RGBA32F。
                # 始终全尺寸（下游 letterbox/合成按此尺寸采样，nav 降采样不影响输出契约）。
                self._gpu_texture = ctx.texture((w, h), 4, dtype='f4')
                self._gpu_texture.filter = (moderngl.LINEAR, moderngl.LINEAR)
                # 深度合成（阶段六）：R32F 可采样深度纹理 + depth-only FBO（不受 MSAA 影响）。
                if aov_depth:
                    self._depth_texture = ctx.texture((w, h), 1, dtype='f4')
                    self._depth_texture.filter = (moderngl.LINEAR, moderngl.LINEAR)
                    self._depth_fbo = ctx.framebuffer(
                        color_attachments=[self._depth_texture])
                # P7：ID picking RGBA8 纹理 + id-only FBO（与渲染目标同尺寸同生命周期）。
                # ID pass 需要全尺寸精度（picking 像素准确），不做降采样。
                if aov_object_id:
                    self._id_texture = ctx.texture((w, h), 4, dtype='f1')
                    self._id_texture.filter = (moderngl.NEAREST, moderngl.NEAREST)
                    self._id_fbo = ctx.framebuffer(
                        color_attachments=[self._id_texture])
                # Geometry AOVs use RGBA16F: normal is encoded for direct Viewer
                # inspection, position retains HDR world-space values for EXR.
                for _aov_name in (
                        "normal", "position", "uv", "material_id", "motion_vector"):
                    _enabled = (aov_normal if _aov_name == "normal"
                                else (aov_position
                                      if _aov_name == "position"
                                      else (aov_uv if _aov_name == "uv"
                                            else (aov_material_id
                                                  if _aov_name == "material_id"
                                                  else aov_motion_vector))))
                    if not _enabled:
                        continue
                    _aov_tex = ctx.texture(
                        (w, h), 4,
                        dtype=('f1' if _aov_name == "material_id" else 'f2'))
                    _aov_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
                    self._aov_textures[_aov_name] = _aov_tex
                    _aov_depth_rb = ctx.depth_renderbuffer((w, h))
                    self._aov_depth_rbs[_aov_name] = _aov_depth_rb
                    self._aov_fbos[_aov_name] = ctx.framebuffer(
                        color_attachments=[_aov_tex],
                        depth_attachment=_aov_depth_rb)

                # ── MSAA：msaa>0 用多重采样 FBO 渲染，resolve 到 _gpu_texture ──
                # RGBA32F + MSAA 在某些驱动不支持 → try/except 降级 msaa=0。
                # P1-4：MSAA color/depth 用降采样尺寸 _rw/_rh（nav 态）。
                msaa_ok = False
                if msaa > 0:
                    try:
                        self._msaa_color = ctx.texture((_rw, _rh), 4, dtype='f4', samples=msaa)
                        self._depth_rb = ctx.depth_renderbuffer((_rw, _rh), samples=msaa)
                        self._render_fbo = ctx.framebuffer(
                            color_attachments=[self._msaa_color],
                            depth_attachment=self._depth_rb,
                        )
                        # resolve FBO 绑定最终 _gpu_texture（渲染完毕 blit 多重采样→普通）。
                        self._resolve_fbo = ctx.framebuffer(
                            color_attachments=[self._gpu_texture])
                        self._msaa_samples = msaa
                        msaa_ok = True
                    except Exception:
                        # 降级：释放刚建的 msaa 资源，走非多重采样路径。
                        import logging
                        logging.getLogger(__name__).warning(
                            "Scene3D MSAA x%d 不可用，降级为关闭", msaa, exc_info=True)
                        if self._msaa_color is not None:
                            try: self._msaa_color.release()
                            except Exception: pass
                            self._msaa_color = None
                        if self._depth_rb is not None:
                            try: self._depth_rb.release()
                            except Exception: pass
                            self._depth_rb = None
                        if self._render_fbo is not None:
                            try: self._render_fbo.release()
                            except Exception: pass
                            self._render_fbo = None
                        self._msaa_samples = 0
                if not msaa_ok:
                    # 非多重采样。
                    # P1-4：nav 降采样态——渲染目标用降采样 texture，渲染后上采样到 _gpu_texture。
                    # 静止态——_gpu_texture 直接是渲染目标（与升级前一致）。
                    if _nav_res_scale < 1.0:
                        self._nav_render_color = ctx.texture((_rw, _rh), 4, dtype='f4')
                        self._nav_render_color.filter = (moderngl.LINEAR, moderngl.LINEAR)
                        self._depth_rb = ctx.depth_renderbuffer((_rw, _rh))
                        self._nav_render_fbo = ctx.framebuffer(
                            color_attachments=[self._nav_render_color],
                            depth_attachment=self._depth_rb,
                        )
                        self._render_fbo = self._nav_render_fbo
                        # resolve FBO 绑定全尺寸 _gpu_texture（上采样目标）。
                        self._resolve_fbo = ctx.framebuffer(
                            color_attachments=[self._gpu_texture])
                    else:
                        self._depth_rb = ctx.depth_renderbuffer((w, h))
                        self._render_fbo = ctx.framebuffer(
                            color_attachments=[self._gpu_texture],
                            depth_attachment=self._depth_rb,
                        )
                    self._msaa_samples = 0

                # Mesh/material GL resources are independent from render targets.
                # Their own ensure/release paths handle primitive/model/instance
                # changes, so FBO rebuilds must not drop VAO/VBO cache entries.

            # ── 3.5 阶段七：优先用 scene 输入 payload（Camera3D / Light3D）──
            # P1.5：payload 已在 §1.5 端口感知收集（cam_payload/light_payload/geo_payload/
            # model_payload）。这里只做"应用"——把 payload 数据覆盖到渲染局部变量。
            if cam_payload is not None:
                proj = cam_payload.proj
                view = cam_payload.view
                eye = cam_payload.eye
                cam_target = m3d.vec3(cam_payload.target[0],
                                     cam_payload.target[1],
                                     cam_payload.target[2])
                # P1 修复：用外接相机的真实 pitch 驱动 grid 地平线渐隐。
                # payload.pitch 由 Camera3D 从 eye-target 反算（camera3d_node.py），
                # 避免 fallback cam_orbit_pitch 与外接相机不一致导致 grid fade 错位。
                cam_pitch = float(getattr(cam_payload, "pitch", cam_pitch))
                # P3 曝光：存实例属性供 _set_shared_uniforms 读取
                self._cam_exposure = float(getattr(cam_payload, "exposure", 0.0))

            if light_payloads:
                # P2 灯光重构：合并所有 LightPayload 为 shader 需要的结构。
                # - ambient 类型 → 累加到 _ambient_color（加到 shader ambient 项）
                # - 其余类型 → 直接光数组（最多 4 盏，截断 + 日志警告）
                # lights 列表保留旧 [(dir, color, intensity)] 结构供 content_sig 兼容
                from ..render3d.scene_payload import (
                    LIGHT_TYPE_AMBIENT, LIGHT_SHADER_TYPE_ID,
                    LIGHT_DECAY_SHADER_TYPE_ID)
                merged_lights = []  # list[(dir, color, intensity)] for legacy sig
                _direct_light_payloads = []  # 非 ambient 的 LightPayload 列表
                _ambient_sum_r = 0.0
                _ambient_sum_g = 0.0
                _ambient_sum_b = 0.0
                _light3d_ibl = None
                for lp in light_payloads:
                    # 取首个非空 IBL 三件套
                    if _light3d_ibl is None and lp.use_ibl and lp.irradiance_cube is not None:
                        _light3d_ibl = lp
                    if lp.light_type == LIGHT_TYPE_AMBIENT:
                        # ambient：颜色 × 强度 累加到环境项
                        _ambient_sum_r += lp.color[0] * lp.intensity
                        _ambient_sum_g += lp.color[1] * lp.intensity
                        _ambient_sum_b += lp.color[2] * lp.intensity
                    else:
                        if len(_direct_light_payloads) < 4:
                            _direct_light_payloads.append(lp)
                            merged_lights.append((lp.dir, lp.color, lp.intensity))
                        else:
                            import logging
                            logging.getLogger(__name__).warning(
                                "Scene3D 直接光超过 4 盏上限，第 %d 盏被忽略",
                                len(_direct_light_payloads) + 1)
                lights = merged_lights
                # 存合并后的灯光数据供 _set_shared_uniforms 用
                self._merged_direct_lights = _direct_light_payloads
                self._ambient_color = (_ambient_sum_r, _ambient_sum_g, _ambient_sum_b)
            else:
                _light3d_ibl = None
                self._merged_direct_lights = None
                self._ambient_color = (0.0, 0.0, 0.0)

            # 阶段七：geometry_in / model_in payload（Geo3D / Model3D）
            # 有 Model3D 且 has_model=True → 走路径 A（覆盖自身导入逻辑）
            # 否则有 Geometry3D → 走路径 B（覆盖自身 procedural 几何）
            # 都没有 → 退化为自身旧参数（过渡期兼容）
            _use_model_payload = (model_payload is not None and model_payload.has_model)
            _use_geo_payload = (geo_payload is not None and not _use_model_payload)

            # ── 4. 构建场景矩阵 ──
            if cam_payload is None:
                # 阶段七：无 Camera3D 连接 → 用内置默认相机（开箱即用 + Alt 导航回写）。
                # 内置 cam_* 参数在 PARAM_DEFS 里，Alt 导航直接写这些参数。
                aspect = w / h if h > 0 else 1.0
                cam_target = m3d.vec3(cam_tx, cam_ty, cam_tz)
                if single_ortho_view:
                    # M2: SINGLE 模式正交 override——主相机改用正交投影。
                    # 相机配置与 _render_ortho_views 一致（但全分辨率，aspect=w/h）。
                    proj, view, eye = self._single_ortho_camera(
                        single_ortho_view, cam_target, cam_dist, aspect, cam_fov)
                else:
                    proj = m3d.perspective(cam_fov, aspect, 0.1, 1000.0)
                    eye = m3d.orbit_to_eye(cam_dist, cam_yaw, cam_pitch, target=cam_target)
                    view = m3d.look_at(eye, cam_target, m3d.vec3(0.0, 1.0, 0.0))
            # 物体变换：T · R · S
            model = m3d.compose(
                m3d.translate(opx, opy, opz),
                m3d.rotate_xyz(orx, ory, orz),
                m3d.scale(mesh_scale, mesh_scale, mesh_scale),
            )
            # AUD-7304: 深度 pass 的线性化 near/far 跟随本帧投影来源——外接
            # Camera3D 用 payload 携带的可调裁剪面，内置相机与
            # m3d.perspective(..., 0.1, 1000.0) 同一常量。
            _cam_near = (float(getattr(cam_payload, "near", 0.1))
                         if cam_payload is not None else 0.1)
            _cam_far = (float(getattr(cam_payload, "far", 1000.0))
                        if cam_payload is not None else 1000.0)

            # ── 5. 上传几何（primitive / instance_enabled 变了才重建）──
            # 阶段七：Geo3D payload 在场时跳过自身 procedural 几何（用 payload 的 VAO）。
            if not _use_geo_payload and has_builtin_primitive:
                # 实例化：先建 instance buffer（grid_x×grid_z 个 offset），供 VAO 引用。
                if instance_enabled:
                    self._ensure_instance_buffer(ctx, grid_x, grid_z, spacing)
                # procedural 几何（导入模型失败/未启用时的 fallback，仍懒建）
                self._ensure_mesh_geometry(ctx, primitive, instance_enabled)
            elif not _use_geo_payload:
                self._release_mesh_geometry()
            prog = self._mesh_instanced_prog if instance_enabled else self._mesh_prog
            if prog is None:
                prog = self._ensure_mesh_program(ctx)

            # ── 5a. 导入模型加载（阶段五；按 path+mtime 缓存）──
            # 阶段七：Model3D payload 在场时跳过自身加载（用 payload 的 flat_meshes）。
            import_ok = False
            if _use_model_payload:
                import_ok = True
            elif use_imported_model and model_path:
                self._ensure_model(model_path)
                import_ok = self._ensure_flat_meshes_gl(ctx, instance_enabled)
            else:
                # 未启用导入模型：清掉旧模型缓存与错误
                if self._model_root is not None:
                    self._release_flat_meshes_gl(ctx)
                    self._model_root = None
                    self._model_path_cached = None
                self._model_error = None

            # ── 5b. IBL bake（HDRI 路径变化或首次才重 bake，内部按 mtime 缓存）──
            # use_ibl=False 或无 hdri_path 时返回黑色占位，PBR 仍可工作。
            # 阶段七：若 Light3D 连接提供 IBL payload，优先用之（覆盖自身 _ensure_ibl）。
            # P1.3 防御：Light3D bake 失败可能残留 None cube map → _set_shared_uniforms
            # 调 .use() 会崩。这里保证三件套非 None，否则降级为自身黑色占位。
            if _light3d_ibl is not None:
                irradiance_cube = _light3d_ibl.irradiance_cube
                prefilter_cube = _light3d_ibl.prefilter_cube
                brdf_lut = _light3d_ibl.brdf_lut
                env_intensity = _light3d_ibl.env_intensity
                use_ibl = _light3d_ibl.use_ibl
                # 任一为 None → bake 失败残留，降级黑色占位
                if irradiance_cube is None or prefilter_cube is None or brdf_lut is None:
                    irradiance_cube, prefilter_cube, brdf_lut = self._ensure_ibl(ctx, False, "")
            else:
                irradiance_cube, prefilter_cube, brdf_lut = self._ensure_ibl(ctx, use_ibl, hdri_preset)

            # ── 6. 渲染 ──
            self._render_fbo.use()
            # P1-4：viewport 用渲染目标实际尺寸（nav 降采样时 < 项目尺寸）。
            # 用实例字段 _render_target_w/h（_rebuild_targets 块内更新，cache-hit 复用）。
            _rtw = self._render_target_w or w
            _rth = self._render_target_h or h
            ctx.viewport = (0, 0, _rtw, _rth)
            # 清色 + 清深度。clear alpha 决定背景是否透明。
            # bg_opaque=False 时 bgr/g/b 已被强制为 0，满足预乘不变量（alpha=0 → RGB=0）。
            clear_alpha = 1.0 if bg_opaque else 0.0
            ctx.clear(bgr, bgg, bgb, clear_alpha, depth=1.0)

            ctx.enable(moderngl.DEPTH_TEST)
            try:
                # P1-21: establish holdout geometry in the hardware depth
                # buffer before any beauty colour is drawn. This is the key
                # ordering rule: a transparent/late holdout draw cannot erase
                # pixels that were already written by a farther CG object.
                self._render_depth_pass(
                    ctx, _rtw, _rth, proj, view, model, eye,
                    import_ok, instance_enabled, instance_count,
                    anim_enabled, anim_time, anim_speed,
                    flat_meshes_src=(model_payload.flat_meshes
                                     if _use_model_payload else None),
                    anim_worlds_src=(model_payload.anim_worlds
                                     if _use_model_payload else None),
                    geo_payloads_src=_geo_payloads,
                    hidden_mesh_ids=(getattr(model_payload, "hidden_mesh_ids", ())
                                     if _use_model_payload else ()),
                    model_render_flags=(getattr(model_payload, "render_flags", None)
                                        if _use_model_payload else None),
                    target_fbo=self._render_fbo,
                    clear_target=False,
                    only_holdout=True,
                    cam_near=_cam_near,
                    cam_far=_cam_far,
                )
                # _render_depth_pass owns its depth-test state; resume the
                # main beauty pass after the depth-only prepass completes.
                ctx.enable(moderngl.DEPTH_TEST)
                # ── 共享 PBR uniform（灯光 / 相机 / IBL）——两种渲染路径共用 ──
                cam_pos = (float(eye[0]), float(eye[1]), float(eye[2]))
                # P2 灯光重构：构造多类型灯光数组。
                # 优先用 _merged_direct_lights（来自 Light3D payload 合并），
                # 否则回退到内置 lights 列表（Scene3D 自身灯光参数，全方向光）。
                import math as _math
                from ..render3d.scene_payload import LIGHT_SHADER_TYPE_ID
                merged = getattr(self, '_merged_direct_lights', None)
                ambient_color = getattr(self, '_ambient_color', (0.0, 0.0, 0.0))
                if merged:
                    # P2：来自 Light3D payload 的多类型灯光
                    light_count = min(len(merged), 4)
                    dirs = [(0.0, 1.0, 0.0)] * 4
                    cols = [(0.0, 0.0, 0.0)] * 4
                    ints = [0.0] * 4
                    ltypes = [0] * 4
                    lpos = [(0.0, 0.0, 0.0)] * 4
                    lranges = [0.0] * 4
                    linners = [_math.cos(_math.radians(30.0))] * 4
                    louters = [_math.cos(_math.radians(45.0))] * 4
                    ldecays = [2] * 4
                    lies_enabled = [0] * 4
                    lies_profiles = [(1.0, 1.0, 1.0, 1.0)] * 4
                    for i in range(light_count):
                        lp = merged[i]
                        dirs[i] = tuple(lp.dir)
                        cols[i] = tuple(lp.color)
                        ints[i] = lp.intensity
                        ltypes[i] = LIGHT_SHADER_TYPE_ID.get(lp.light_type, 0)
                        lpos[i] = tuple(lp.pos)
                        lranges[i] = lp.range
                        linners[i] = _math.cos(_math.radians(min(lp.inner_cone, 89.0)))
                        louters[i] = _math.cos(_math.radians(min(lp.outer_cone, 89.0)))
                        ldecays[i] = LIGHT_DECAY_SHADER_TYPE_ID.get(
                            str(getattr(lp, "decay_mode", "inverse_square")), 2)
                        lies_enabled[i] = int(bool(getattr(lp, "ies_enabled", False)))
                        _ies = tuple(float(value) for value in getattr(
                            lp, "ies_samples", (1.0, 1.0, 1.0, 1.0)))
                        lies_profiles[i] = (_ies + (1.0, 1.0, 1.0, 1.0))[:4]
                    dirs_t = tuple(dirs)
                    cols_t = tuple(cols)
                    ints_t = tuple(ints)
                    light_types_t = tuple(ltypes)
                    light_pos_t = tuple(lpos)
                    light_range_t = tuple(lranges)
                    light_inner_t = tuple(linners)
                    light_outer_t = tuple(louters)
                    light_decay_mode_t = tuple(ldecays)
                    light_ies_enabled_t = tuple(lies_enabled)
                    light_ies_profile_t = tuple(lies_profiles)
                else:
                    # 回退：内置灯光（全方向光，无位置/衰减）
                    light_count = min(len(lights), 4)
                    dirs = [(0.0, 1.0, 0.0)] * 4
                    cols = [(0.0, 0.0, 0.0)] * 4
                    ints = [0.0] * 4
                    for i in range(light_count):
                        d, c, inten = lights[i]
                        dirs[i] = (d[0], d[1], d[2])
                        cols[i] = (c[0], c[1], c[2])
                        ints[i] = inten
                    dirs_t = tuple(dirs)
                    cols_t = tuple(cols)
                    ints_t = tuple(ints)
                    light_types_t = (0, 0, 0, 0)
                    light_pos_t = ((0.0, 0.0, 0.0),) * 4
                    light_range_t = (0.0, 0.0, 0.0, 0.0)
                    light_inner_t = (_math.cos(_math.radians(30.0)),) * 4
                    light_outer_t = (_math.cos(_math.radians(45.0)),) * 4
                    light_decay_mode_t = (2, 2, 2, 2)
                    light_ies_enabled_t = (0, 0, 0, 0)
                    light_ies_profile_t = ((1.0, 1.0, 1.0, 1.0),) * 4
                # P2：存实例属性供 _set_shared_uniforms（含 _render_ortho_views）回退读取
                self._light_types_t = light_types_t
                self._light_pos_t = light_pos_t
                self._light_range_t = light_range_t
                self._light_inner_t = light_inner_t
                self._light_outer_t = light_outer_t
                self._light_decay_mode_t = light_decay_mode_t
                self._light_ies_enabled_t = light_ies_enabled_t
                self._light_ies_profile_t = light_ies_profile_t
                # ambient：颜色已含强度累加，intensity 倍率为 1.0（避免重复乘）
                self._ambient_intensity = 1.0 if any(ambient_color) else 0.0
                env_int_val = env_intensity if use_ibl else 0.0
                # 阶段七：Light3D payload 自带 prefilter_max_lod；否则用自身 baker。
                if _light3d_ibl is not None:
                    prefilter_max_lod = _light3d_ibl.prefilter_max_lod
                else:
                    prefilter_max_lod = (
                        self._hdri_baker.get_prefilter_max_lod()
                        if self._hdri_baker is not None else 4.0)
                # P8-2：计算本帧 shared-uniforms 的 view_sig（proj/view/lights/IBL hash）。
                # 同一 view_sig 下，同 program 的 shared uniforms 只设一次（22→1/视图/program）。
                # cache 在帧开头清空；这里只设置 view_sig 供 _set_shared_uniforms 读。
                try:
                    _view_sig = (
                        proj.tobytes().__hash__() if hasattr(proj, 'tobytes') else hash(proj),
                        view.tobytes().__hash__() if hasattr(view, 'tobytes') else hash(view),
                        hash(dirs_t), hash(cols_t), hash(ints_t), int(light_count),
                        hash(light_types_t), hash(light_pos_t), hash(light_range_t),
                        hash(light_decay_mode_t),
                        hash(light_ies_enabled_t), hash(light_ies_profile_t),
                        float(env_int_val), float(prefilter_max_lod),
                        float(self._ambient_intensity),
                        float(getattr(self, '_cam_exposure', 0.0)),
                    )
                    self._shared_uniforms_view_sig = _view_sig
                    # 帧开头清空旧缓存（防止 program 对象 id 复用导致误命中）。
                    # program id 在 GL 对象释放后可能被新对象复用，所以每帧清。
                    self._shared_uniforms_cache.clear()
                except (TypeError, AttributeError):
                    self._shared_uniforms_view_sig = None

                # P1-21: build one directional shadow map when a catcher is
                # present. The map is generated before holdout/beauty draws;
                # catcher compositing itself happens after beauty below.
                _shadow_light_view_proj = None
                _model_render_flags = (
                    getattr(model_payload, "render_flags", None)
                    if _use_model_payload else None)
                _shadow_catcher_requested = bool(
                    getattr(_model_render_flags, "shadow_catcher", False)
                    or any(getattr(getattr(gp, "render_flags", None),
                                   "shadow_catcher", False)
                           for gp in (_geo_payloads or ())))
                # P1-26：shadow-catcher 仍使用单张 directional map，但其来源、
                # 开关和分辨率由 Light3D 的物理灯光设置决定。point/spot 暂不
                # 冒充方向光阴影，避免产生错误的遮挡结果。
                _shadow_light_index = -1
                _shadow_quality = "medium"
                for _li, _lp in enumerate(merged or ()):
                    if (str(getattr(_lp, "light_type", "")) == "directional"
                            and bool(getattr(_lp, "shadow_enabled", True))):
                        _shadow_light_index = _li
                        _shadow_quality = str(getattr(_lp, "shadow_quality", "medium"))
                        break
                if merged is None and light_count > 0:
                    # Legacy Scene3D fallback has one directional key light.
                    _shadow_light_index = 0
                _shadow_sizes = {"low": 512, "medium": 1024,
                                 "high": 2048, "ultra": 4096}
                self._shadow_map_size = _shadow_sizes.get(_shadow_quality, 1024)
                _has_shadow_light = bool(
                    _shadow_light_index >= 0
                    and _shadow_light_index < light_count
                    and float(ints_t[_shadow_light_index]) > 0.0)
                if _shadow_catcher_requested and _has_shadow_light:
                    _shadow_light_view_proj = self._render_shadow_map(
                        ctx, proj, model, cam_target,
                        dirs_t[_shadow_light_index],
                        import_ok,
                        (model_payload.flat_meshes
                         if _use_model_payload else None),
                        (model_payload.anim_worlds
                         if _use_model_payload else None),
                        _model_render_flags,
                        _geo_payloads,
                        getattr(model_payload, "hidden_mesh_ids", ())
                        if _use_model_payload else (),
                        anim_enabled=anim_enabled,
                        anim_time=anim_time,
                        anim_speed=anim_speed)
                    # Shadow-map rendering leaves the auxiliary FBO bound.
                    # Rebind the main target before the holdout prepass and
                    # beauty pass continue.
                    self._render_fbo.use()
                    ctx.viewport = (0, 0, _rtw, _rth)

                if import_ok and (self._flat_meshes_gl or _use_model_payload):
                    # ── 路径 A：导入模型多 mesh 渲染（含层级 + 骨骼 + 视锥剔除 + 实例化）──
                    # 阶段七：Model3D payload 在场时用其 flat_meshes + anim_worlds，
                    # 否则用自身 _flat_meshes_gl + 重新采样动画。
                    # P7：隔离模式（isolate_3d_node_id）下只渲染该节点的 mesh。
                    _cur_model_nid = (getattr(model_payload, "node_id", "")
                                      if model_payload is not None else "")
                    _hidden_model_mesh_ids = {
                        str(value) for value in getattr(
                            model_payload, "hidden_mesh_ids", ())
                    } if model_payload is not None else set()
                    _render_model_path = not (
                        self._p7_isolate_nid
                        and _cur_model_nid != self._p7_isolate_nid
                        and not any(
                            str(mesh.get("node_id", "")) == self._p7_isolate_nid
                            for mesh in (model_payload.flat_meshes
                                         if model_payload is not None else ())
                        )
                    )
                    if _render_model_path:
                        if _use_model_payload:
                            flat_meshes_src = model_payload.flat_meshes
                            anim_worlds = model_payload.anim_worlds
                        else:
                            flat_meshes_src = self._flat_meshes_gl
                            anim_worlds = self._sample_animation_worlds(anim_enabled, anim_time, anim_speed)
                        _persp_t0 = time.perf_counter()
                        # 视锥剔除用：从 proj@view 提 6 平面
                        from ..render3d.frustum import (
                            extract_frustum_planes, aabb_in_frustum,
                            transform_aabb, aabb_screen_size)
                        planes = extract_frustum_planes(proj @ view)
                        # P8 §12.2：small object culling 阈值（像素）。nav 态启用更激进，
                        # 跳过远距离极小 mesh 的 draw call。静止态阈值小（少剔除保质量）。
                        _small_obj_px = (2.0 if self._lod_active else 0.5)
                        # P8-3：navigation 态启用 backface culling（闭网格省 ~50% 片元）。
                        # 渲染结束后在 perspective_ms 统计后 disable（见下方 finally 语义）。
                        if _backface_cull:
                            ctx.enable(moderngl.CULL_FACE)
                            ctx.cull_face = 'back'
                        # 实例化时的并集 AABB 用：整组实例的 offset 范围（min/max 偏移）
                        if instance_enabled and self._instance_vbo is not None:
                            _inst_wmin, _inst_wmax = self._instance_offset_range(grid_x, grid_z, spacing)
                        # Milestone D / P3-6：按材质排序（仅不透明 mesh），让同材质 mesh 相邻
                        # → 提升材质状态缓存命中率（连续同材质跳过 rebind）。透明 mesh 保持
                        # 原顺序（透明物体需 painter's order，不能重排，§7.4 风险1）。
                        _render_meshes = self._sort_meshes_by_material(flat_meshes_src)
                        _material_overrides = self._material_override_map(model_payload)
                        # P8-0：追踪 mesh_prog 切换，统计 shader_switches（program 变化次数）。
                        _last_prog_id = None
                        for m in _render_meshes:
                            _mesh_nid = str(m.get("node_id", "") or _cur_model_nid)
                            if _mesh_nid in _hidden_model_mesh_ids:
                                _pstats.meshes_culled += 1
                                continue
                            if not self._is_beauty_visible(
                                    getattr(model_payload, "render_flags", None)
                                    if _use_model_payload else None):
                                _pstats.meshes_culled += 1
                                continue
                            if (self._p7_isolate_nid
                                    and _cur_model_nid != self._p7_isolate_nid
                                    and _mesh_nid != self._p7_isolate_nid):
                                _pstats.meshes_culled += 1
                                continue
                            # 阶段七：用 mesh 自带的 program（VAO 绑定的）设 uniform，
                            # 而非 Scene3D 自己的程序（P4 后 Scene3D 不再编译这些程序）。
                            mesh_prog = m.get('program')
                            if mesh_prog is None:
                                # 兼容旧 payload（无 program 字段）：退化为自身程序
                                mesh_prog = (self._mesh_skinned_prog if m['is_skinned']
                                             else (self._mesh_instanced_prog if m.get('can_instance')
                                                   else self._mesh_prog))
                            if mesh_prog is None:
                                continue
                            # P8-0：program 切换计数（shader_switches）。
                            _prog_id = id(mesh_prog)
                            if _prog_id != _last_prog_id:
                                _pstats.shader_switches += 1
                                _last_prog_id = _prog_id
                            # effective_model = node_model @ world_matrix（父子层级联动）
                            node_name = m['node_name']
                            wm = anim_worlds.get(node_name, m['world_matrix'])
                            effective_model = (model @ wm).astype(np.float32)
                            # ── 视锥剔除：mesh local AABB → world AABB（含实例 offset 范围）──
                            # GPU skinning can move vertices outside the bind-pose
                            # AABB. Do not cull skinned meshes from that stale box;
                            # correctness takes precedence over this optimization.
                            is_skinned = bool(m.get('is_skinned'))
                            local_min, local_max = m['mesh'].local_aabb() if m.get('mesh') else (np.array([-1,-1,-1],dtype=np.float32), np.array([1,1,1],dtype=np.float32))
                            if instance_enabled and m.get('can_instance'):
                                # 实例化：把 offset 范围并入 local AABB 后再变换
                                wmin = local_min + np.array(_inst_wmin, dtype=np.float32)
                                wmax = local_max + np.array(_inst_wmax, dtype=np.float32)
                            else:
                                wmin, wmax = local_min, local_max
                            wbmin, wbmax = transform_aabb(wmin, wmax, effective_model)
                            if not is_skinned and not aabb_in_frustum(planes, wbmin, wbmax):
                                _pstats.meshes_culled += 1
                                continue  # 视锥外，跳过 draw
                            # P8 §12.2：small object culling——远距离极小 mesh 跳过 draw call。
                            # 屏幕投影估算 < _small_obj_px 像素则剔除（nav 态更激进）。
                            # 只对非实例化 mesh（实例化一组共用 draw，单 mesh 剔除收益小）。
                            if (not is_skinned and _small_obj_px > 0
                                    and not (instance_enabled and m.get('can_instance'))):
                                _screen_px = aabb_screen_size(
                                    wbmin, wbmax, cam_pos, h, cam_fov)
                                if 0.0 < _screen_px < _small_obj_px:
                                    _pstats.meshes_culled += 1
                                    continue
                            _pstats.meshes_visible += 1
                            self._set_shared_uniforms(
                                mesh_prog, proj, view, cam_pos,
                                dirs_t, cols_t, ints_t, light_count,
                                irradiance_cube, prefilter_cube, brdf_lut,
                                env_int_val, prefilter_max_lod,
                                _stats=_pstats)
                            mesh_prog['u_model'].value = m3d.matrix_to_tuple(effective_model)
                            # P8-0：统计 per-mesh uniform（u_model + u_albedo/metallic/
                            # roughness/emissive = 5 个/每 mesh，cache hit 也要算）。
                            _pstats.uniform_sets += 5
                            # per-mesh 材质（导入模型的 base_color/metallic/roughness/emissive）
                            # Model3D 材质升级：设置 PBR 标量 + 绑定材质贴图 + has_map 标志。
                            mat = self._material_with_override(
                                m.get('material'), m, _material_overrides)
                            # 材质参数（来自 Model3D payload；None=节点默认）
                            _mp = (model_payload.material_params
                                   if _use_model_payload and model_payload.material_params else {})
                            if mat is not None:
                                mesh_prog['u_albedo'].value = (float(mat.base_color[0]),
                                                                float(mat.base_color[1]),
                                                                float(mat.base_color[2]))
                                mesh_prog['u_metallic'].value = float(mat.metallic)
                                mesh_prog['u_roughness'].value = max(0.01, float(mat.roughness))
                                # emissive 标量
                                mesh_prog['u_emissive'].value = (float(mat.emissive[0]),
                                                                  float(mat.emissive[1]),
                                                                  float(mat.emissive[2]))
                            else:
                                mesh_prog['u_albedo'].value = (ocr, ocg, ocb)
                                mesh_prog['u_metallic'].value = metallic
                                mesh_prog['u_roughness'].value = roughness
                                mesh_prog['u_emissive'].value = (0.0, 0.0, 0.0)
                            # Model3D 材质升级：绑定材质贴图（override + 导入 TextureDescriptor）
                            # 到对应 sampler，返回各 slot 的 has_map 标志。
                            # 无纹理可用时全 False → 退化为标量 PBR（与升级前行为一致）。
                            _tex_overrides = (model_payload.texture_overrides or {}
                                              if _use_model_payload else {})
                            _model_dir = (os.path.dirname(model_payload.model_path)
                                          if _use_model_payload and model_payload.model_path else "")
                            # Milestone D / P3：材质状态缓存——连续 mesh 用相同材质 + 纹理 +
                            # 采样参数时跳过 _bind_null_material_samplers +
                            # _bind_material_textures + u_has_*_map 设置（视觉等价，省
                            # Python 调用 + GL state change）。缓存范围：单视图 mesh 循环。
                            from ..render3d.material_state import MaterialStateKey
                            _mat_key = MaterialStateKey.from_material(
                                mat, _mp, _tex_overrides,
                                has_tangent=bool(m.get('has_tangent')))  # AUD-8905 接线（wd7-g39）
                            if not self._material_cache.matches(_mat_key):
                                # Model3D may have zero imported textures. Set all
                                # material sampler uniforms to the 2D texture slots
                                # first, then bind any real maps on top of them.
                                self._bind_null_material_samplers(mesh_prog)
                                _has_map = self._bind_material_textures(
                                    ctx, mesh_prog, mat, _tex_overrides, _mp, _model_dir)
                                # P8-1：FAST_SOLID profile 下强制 u_has_*_map = 0，
                                # 跳过 base_color/orm/ao/emissive 贴图采样（GPU texture
                                # fetch 是片元瓶颈之一）。用材质标量颜色做简化 PBR。
                                # 注：只在 FAST_SOLID（大模型极限交互）启用；NAVIGATION 保留
                                # 贴图（用户期望导航时仍能看到材质，§5.3 baseColor 可开）。
                                _nav_skip_maps = (_qp == ViewportQualityProfile.FAST_SOLID)
                                mesh_prog['u_has_base_color_map'].value = (
                                    0 if _nav_skip_maps else int(_has_map["base_color"]))
                                # F5：法线贴图采样。仅当 mesh VAO 含 tangent 属性
                                # （Model3D 在材质有 normal_tex 时构建 tangent VAO +
                                # _mesh_tangent_prog）且材质确实绑定了 normal map 时启用。
                                # has_tangent=False（无 tangent 的 mesh / procedural）保持 0，
                                # 行为与改动前完全一致（向后兼容）。
                                _has_normal = (not _nav_skip_maps
                                               and bool(m.get('has_tangent'))
                                               and bool(_has_map.get("normal")))
                                mesh_prog['u_has_normal_map'].value = int(_has_normal)
                                mesh_prog['u_has_orm_map'].value = (
                                    0 if _nav_skip_maps else int(_has_map["orm"]))
                                mesh_prog['u_has_ao_map'].value = (
                                    0 if _nav_skip_maps else int(_has_map["ao"]))
                                mesh_prog['u_has_emissive_map'].value = (
                                    0 if _nav_skip_maps else int(_has_map["emissive"]))
                                mesh_prog['u_emissive_intensity'].value = float(_mp.get("emissive_intensity", 1.0))
                                mesh_prog['u_normal_format'].value = (1 if _mp.get("normal_format") == "DirectX" else 0)
                                self._material_cache.update(_mat_key)
                                _pstats.material_switches += 1
                            # 蒙皮：上传骨骼世界矩阵数组（= joint_world @ inverse_bind）
                            if m['is_skinned']:
                                self._upload_bone_matrices(mesh_prog, m, anim_worlds)
                            # Milestone F / P5-4：导航态用 LOD mesh 的 VAO（低面数代理）。
                            # _lod_active=True（NAVIGATION/FAST_SOLID profile）且 mesh 带
                            # lod_mesh（Model3D 生成的大模型代理）时，构建/复用 LOD VAO 渲染。
                            # LOD VAO 通过 MeshVBOPool（content_hash 缓存），首次构建后跨帧复用。
                            _lod_vao = None
                            _lod_tris = 0
                            if (self._lod_active and m.get('lod_mesh') is not None
                                    and not m.get('is_skinned')):
                                _lod_vao, _lod_tris = self._get_lod_vao(
                                    ctx, mesh_prog, m['lod_mesh'])
                                if _lod_vao is not None:
                                    # P5：标记本帧实际使用了 LOD mesh（诊断用）
                                    _pstats.lod_level = max(_pstats.lod_level, 2)
                            # 实例化 draw（instances=N）vs 单实例 draw
                            if _lod_vao is not None:
                                _lod_vao.render(moderngl.TRIANGLES)
                                _pstats.triangles_submitted += _lod_tris
                            elif instance_enabled and m.get('can_instance'):
                                m['vao'].render(moderngl.TRIANGLES, instances=instance_count)
                                _pstats.triangles_submitted += (
                                    int(m.get('index_count', 0)) // 3) * instance_count
                            else:
                                m['vao'].render(moderngl.TRIANGLES)
                                _pstats.triangles_submitted += int(m.get('index_count', 0)) // 3
                            _pstats.draw_calls += 1
                            # Do not unbind here. The material state cache relies on
                            # the last bound sampler state staying valid for
                            # consecutive meshes with the same material. Material
                            # switches call _bind_null_material_samplers first, so
                            # maps still cannot leak into a different material.
                        # ── 路径 A 透视渲染耗时统计（导入模型多 mesh）──
                        _pstats.perspective_ms += (time.perf_counter() - _persp_t0) * 1000.0
                        # P8-3：恢复 backface culling 状态（避免污染 Geo3D/正交/outline pass）。
                        if _backface_cull:
                            ctx.disable(moderngl.CULL_FACE)
                # ── 路径 B：procedural / Geo3D 几何 ──
                # P0 缺口修复：渲染**所有**连接的 Geo3D payload（不再只取第一个）。
                # 多个 Geo3D 连到 geometry_in 时全部按连接顺序渲染（混合场景）。
                # _use_geo_payload 仍保留作签名/导入逻辑判定；这里改用列表遍历。
                # ⚠️ 修复：此块必须与上面 `if import_ok` 同级（16 sp），否则在
                # import_ok=False（无 Model3D）时整块被跳过 → Geo3D + 内置 procedural
                # 都不画 → 无子节点时 Scene3D 不显任何几何（只显坐标轴）。
                if _geo_payloads:
                    _geo_t0 = time.perf_counter()
                    for _gp in _geo_payloads:
                        # P7：隔离模式——只渲染隔离目标的 Geo3D
                        _gp_nid = getattr(_gp, "node_id", "") or ""
                        if self._p7_isolate_nid and _gp_nid != self._p7_isolate_nid:
                            continue
                        if not self._is_beauty_visible(
                                getattr(_gp, "render_flags", None)):
                            _pstats.meshes_culled += 1
                            continue
                        # 用 Geo3D payload 自带的 program（VAO 绑定的）设 uniform
                        geo_prog = _gp.program
                        if geo_prog is None:
                            # 兜底：自身程序（P4 后通常为 None，此分支基本走不到）
                            geo_prog = (self._mesh_instanced_prog
                                        if _gp.variant == "instanced"
                                        else self._mesh_prog) or prog
                        if geo_prog is None:
                            continue
                        self._set_shared_uniforms(
                            geo_prog, proj, view, cam_pos,
                            dirs_t, cols_t, ints_t, light_count,
                            irradiance_cube, prefilter_cube, brdf_lut,
                            env_int_val, prefilter_max_lod)
                        _gmat = _gp.material or {}
                        _galbedo = _gmat.get("albedo", (ocr, ocg, ocb))
                        geo_prog['u_model'].value = m3d.matrix_to_tuple(_gp.model_matrix)
                        geo_prog['u_albedo'].value = (float(_galbedo[0]), float(_galbedo[1]), float(_galbedo[2]))
                        geo_prog['u_metallic'].value = float(_gmat.get("metallic", metallic))
                        geo_prog['u_roughness'].value = max(0.01, float(_gmat.get("roughness", roughness)))
                        # Model3D 材质升级：Geo3D procedural 无纹理，has_map=0 + emissive 默认
                        geo_prog['u_emissive'].value = (0.0, 0.0, 0.0)
                        geo_prog['u_has_base_color_map'].value = 0
                        geo_prog['u_has_normal_map'].value = 0
                        geo_prog['u_has_orm_map'].value = 0
                        geo_prog['u_has_ao_map'].value = 0
                        geo_prog['u_has_emissive_map'].value = 0
                        geo_prog['u_emissive_intensity'].value = 1.0
                        geo_prog['u_normal_format'].value = 0
                        # 关键修复：同 procedural 路径，材质 sampler2D 指到空单元。
                        self._bind_null_material_samplers(geo_prog)
                        _gmode = moderngl.LINES if _gp.is_lines else moderngl.TRIANGLES
                        if _gp.instance_enabled and not _gp.is_lines:
                            _gp.vao.render(_gmode, instances=_gp.instance_count)
                        else:
                            _gp.vao.render(_gmode)
                        _pstats.draw_calls += 1
                        _pstats.meshes_visible += 1
                    _pstats.perspective_ms += (time.perf_counter() - _geo_t0) * 1000.0
                elif not import_ok and has_builtin_primitive and self._mesh_vao is not None:
                    _bp_t0 = time.perf_counter()
                    self._set_shared_uniforms(
                        prog, proj, view, cam_pos,
                        dirs_t, cols_t, ints_t, light_count,
                        irradiance_cube, prefilter_cube, brdf_lut,
                        env_int_val, prefilter_max_lod)
                    prog['u_model'].value = m3d.matrix_to_tuple(model)
                    prog['u_albedo'].value = (ocr, ocg, ocb)
                    prog['u_metallic'].value = metallic
                    prog['u_roughness'].value = roughness
                    # Model3D 材质升级：built-in procedural 无纹理，has_map=0 + emissive 默认
                    prog['u_emissive'].value = (0.0, 0.0, 0.0)
                    prog['u_has_base_color_map'].value = 0
                    prog['u_has_normal_map'].value = 0
                    prog['u_has_orm_map'].value = 0
                    prog['u_has_ao_map'].value = 0
                    prog['u_has_emissive_map'].value = 0
                    prog['u_emissive_intensity'].value = 1.0
                    prog['u_normal_format'].value = 0
                    # 关键修复：把材质 sampler2D 指到空单元，否则它们默认指向单元 0
                    # （IBL 的 samplerCube），类型冲突导致 draw 报错 → 几何不画。
                    self._bind_null_material_samplers(prog)
                    # draw call（grid 用 LINES；实例化时 instances=N）
                    mode = moderngl.LINES if self._mesh_is_lines else moderngl.TRIANGLES
                    if instance_enabled and self._mesh_instance_enabled and not self._mesh_is_lines:
                        self._mesh_vao.render(mode, instances=instance_count)
                    else:
                        self._mesh_vao.render(mode)
                    _pstats.draw_calls += 1
                    _pstats.meshes_visible += 1
                    _pstats.perspective_ms += (time.perf_counter() - _bp_t0) * 1000.0

                # 地面网格 + 世界坐标轴作为真实几何烘焙进纹理（与模型同 FBO）。
                # 此时 DEPTH_TEST 仍开启，模型几何已写入 depth buffer →
                # 网格被模型自动遮挡（标准硬件深度测试，无需手动深度采样）。
                # 坐标轴关深度测试恒在最前（gizmo_renderer 内部处理）。
                # 注意：轴线按 view-mode override 决定是否显（preview-only，默认
                # 不进合成输出 —— 与 show_grid/bg_opaque 同策略，由 page 注入）。
                if show_grid or show_axes:
                    if self._gizmo is None:
                        self._gizmo = GizmoRenderer()
                    # M2: 单视图正交 override 时，网格朝向 + cam_pitch 适配正交视角
                    if single_ortho_view:
                        _gp = {1: "xz", 2: "yz", 3: "xy"}.get(single_ortho_view, "xz")
                        _cp = 90.0 if single_ortho_view == 1 else 0.0
                        self._gizmo.draw(ctx, proj, view,
                                         cam_target=cam_target,
                                         show_grid=show_grid, show_axes=show_axes,
                                         cam_pitch_deg=_cp, grid_plane=_gp,
                                         grid_cell_size=grid_cell_size,
                                         grid_opacity=grid_opacity,
                                         grid_line_width=grid_line_width,
                                         grid_minor_fade_start=grid_minor_fade_start,
                                         grid_minor_fade_end=grid_minor_fade_end,
                                         grid_minor_fade_slope=grid_minor_fade_slope,
                                         grid_major_fade_start=grid_major_fade_start,
                                         grid_major_fade_end=grid_major_fade_end,
                                         grid_major_fade_slope=grid_major_fade_slope,
                                         grid_minor_color=grid_minor_color,
                                         grid_major_color=grid_major_color)
                    else:
                        self._gizmo.draw(ctx, proj, view,
                                         cam_target=cam_target,
                                         show_grid=show_grid, show_axes=show_axes,
                                         cam_pitch_deg=cam_pitch,
                                         grid_cell_size=grid_cell_size,
                                         grid_opacity=grid_opacity,
                                         grid_line_width=grid_line_width,
                                         grid_minor_fade_start=grid_minor_fade_start,
                                         grid_minor_fade_end=grid_minor_fade_end,
                                         grid_minor_fade_slope=grid_minor_fade_slope,
                                         grid_major_fade_start=grid_major_fade_start,
                                         grid_major_fade_end=grid_major_fade_end,
                                         grid_major_fade_slope=grid_major_fade_slope,
                                         grid_minor_color=grid_minor_color,
                                         grid_major_color=grid_major_color)
                # ── F6：骨骼 overlay（show_skeleton=True 时绘制）──
                # 在 grid/axes 之后、DEPTH_TEST 关闭之前渲染，让骨骼被模型
                # 几何正确遮挡（与 grid 同策略）。骨骼用 model 矩阵从局部→世界。
                # 失败不中断主渲染（降级为不显示骨骼）。
                if show_skeleton and model_payload is not None:
                    try:
                        _skels = getattr(model_payload, 'skeletons', None)
                        if _skels:
                            if self._skeleton_renderer is None:
                                self._skeleton_renderer = SkeletonOverlayRenderer()
                            # 骨骼在模型局部空间，用 effective model 矩阵变换到世界
                            # （model 已含 cam/scene 变换，不含 object_pos——object_pos
                            # 已烘进 flat_meshes world_matrix，但骨骼 overlay 需单独应用）。
                            _skel_model = model
                            for _skel in _skels:
                                self._skeleton_renderer.render(
                                    ctx, _skel, proj, view, _skel_model,
                                    current_worlds=(model_payload.anim_worlds or None),
                                )
                    except Exception:
                        import logging
                        logging.getLogger(__name__).warning(
                            "Scene3D 骨骼 overlay 渲染失败，跳过", exc_info=True)
                if _shadow_light_view_proj is not None:
                    try:
                        self._render_shadow_catchers(
                            ctx, _rtw, _rth, proj, view, model,
                            _shadow_light_view_proj, import_ok,
                            (model_payload.flat_meshes
                             if _use_model_payload else None),
                            (model_payload.anim_worlds
                             if _use_model_payload else None),
                            _model_render_flags, _geo_payloads,
                            getattr(model_payload, "hidden_mesh_ids", ())
                            if _use_model_payload else (),
                            anim_enabled=anim_enabled,
                            anim_time=anim_time,
                            anim_speed=anim_speed)
                    except Exception:
                        import logging
                        logging.getLogger(__name__).warning(
                            "Scene3D shadow catcher pass 失败，跳过",
                            exc_info=True)
            finally:
                ctx.disable(moderngl.DEPTH_TEST)

            # ── 6a. resolve（msaa>0 多重采样 resolve，或 nav 降采样上采样）──
            # 在深度 pass 之前完成；resolve 后 _gpu_texture 即最终输出。
            # ModernGL ctx.copy_framebuffer(dst_fbo, src_fbo) = glBlitFramebuffer，
            # src 多重采样、dst 单重采样时自动做 multisample resolve；
            # src/dst 尺寸不同时按 LINEAR filter 缩放（P1-4 nav 上采样依赖此）。
            # P1-4：nav 降采样态（msaa=0 但 _nav_render_fbo 存在）也需 blit 上采样。
            _need_resolve = (
                (self._msaa_samples > 0 or self._nav_render_fbo is not None)
                and self._resolve_fbo is not None
            )
            if _need_resolve:
                try:
                    ctx.copy_framebuffer(self._resolve_fbo, self._render_fbo)
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning(
                        "Scene3D resolve 失败，输出可能为空", exc_info=True)

            # ── 6b. 深度合成第二遍：把线性深度写入 R32F _depth_texture ──
            # 用 _MESH_VERTEX + _MESH_DEPTH_WRITE_FRAGMENT，重画每 mesh 到 depth-only FBO。
            # 保留 DEPTH_TEST 保证遮挡正确；clear depth 纹理到 1.0（远，与 deep_merge 约定一致）。
            # 阶段七：payload 在场时把 Model3D 的 flat_meshes/anim_worlds 传入。
            try:
                _depth_flat_src = None
                _depth_anim_src = None
                if _use_model_payload:
                    _depth_flat_src = model_payload.flat_meshes
                    _depth_anim_src = model_payload.anim_worlds
                self._render_depth_pass(ctx, w, h, proj, view, model, eye,
                                        import_ok, instance_enabled, instance_count,
                                        anim_enabled, anim_time, anim_speed,
                                        flat_meshes_src=_depth_flat_src,
                                        anim_worlds_src=_depth_anim_src,
                                        geo_payloads_src=_geo_payloads,
                                        hidden_mesh_ids=(
                                            getattr(model_payload, "hidden_mesh_ids", ())
                                            if _use_model_payload else ()),
                                        model_render_flags=(
                                            getattr(model_payload, "render_flags", None)
                                            if _use_model_payload else None),
                                        cam_near=_cam_near,
                                        cam_far=_cam_far)
            except Exception:
                # 深度 pass 失败不应中断渲染（color 已完成）；降级为无深度。
                import logging
                logging.getLogger(__name__).warning(
                    "Scene3D 深度 pass 失败，跳过深度输出", exc_info=True)
                if self._depth_texture is not None:
                    try: self._depth_texture.release()
                    except Exception: pass
                    self._depth_texture = None

            # ── 6b2. P7：ID picking pass（渲染 object-id 到 RGBA8，viewer 点击时读回）──
            # 与 depth pass 同级，cheap（无材质/光照，仅常量 id 输出）。隔离模式下只渲染
            # 选中节点的 mesh。失败不中断（picking 降级为不可用）。
            try:
                self._p7_id_pick_info = None
                _id_flat_src = _depth_flat_src
                _id_anim_src = _depth_anim_src
                self._render_id_pass(
                    ctx, w, h, proj, view, model,
                    import_ok, instance_enabled, instance_count,
                    _id_flat_src, _id_anim_src,
                    _model_payloads, _geo_payloads,
                    anim_enabled=anim_enabled,
                    anim_time=anim_time,
                    anim_speed=anim_speed)
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    "Scene3D ID picking pass 失败，跳过", exc_info=True)
                self._p7_id_pick_info = None

            # ── 6b3. P0-07：几何 AOV（normal/position/uv）──
            # 这些 pass 与 beauty 共用本帧的 mesh/skin/instance 数据，但使用
            # 独立 RGBA16F 目标，避免改变主输出或 DeepMerge 的 depth 语义。
            for _aov_name, _aov_mode in (
                    ("normal", 0), ("position", 1), ("uv", 2),
                    ("material_id", 3)):
                if _aov_name not in self._aov_textures:
                    continue
                try:
                    self._render_aov_pass(
                        ctx, _aov_name, _aov_mode, w, h, proj, view, model,
                        import_ok, instance_enabled, instance_count,
                        anim_enabled, anim_time, anim_speed,
                        flat_meshes_src=_depth_flat_src,
                        anim_worlds_src=_depth_anim_src,
                        geo_payloads_src=_geo_payloads,
                        hidden_mesh_ids=(
                            getattr(model_payload, "hidden_mesh_ids", ())
                            if _use_model_payload else ()))
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning(
                        "Scene3D %s AOV pass 失败，跳过", _aov_name,
                        exc_info=True)
                    texture = self._aov_textures.pop(_aov_name, None)
                    fbo = self._aov_fbos.pop(_aov_name, None)
                    depth_rb = self._aov_depth_rbs.pop(_aov_name, None)
                    if fbo is not None:
                        try: fbo.release()
                        except Exception: pass
                    if depth_rb is not None:
                        try: depth_rb.release()
                        except Exception: pass
                    if texture is not None:
                        try: texture.release()
                        except Exception: pass

            # P0-07/P1-20：真实 3D motion vector。以当前/上一帧的投影、视图、
            # 物体和骨骼矩阵计算屏幕空间 UV 位移；首次渲染或时间线跳帧输出零。
            if aov_motion_vector:
                _motion_model_src = _depth_flat_src
                _motion_model_identity = tuple(
                    (str(m.get("node_name", "")), id(m.get("mesh")))
                    for m in (_motion_model_src or []))
                _motion_geo_identity = tuple(
                    (str(getattr(gp, "node_id", "")),
                     id(getattr(gp, "mesh", None)),
                     int(getattr(gp, "index_count", 0)),
                     str(getattr(gp, "variant", "plain")))
                    for gp in (_geo_payloads or []))
                _motion_scene_key = (
                    bool(_use_model_payload), _motion_model_identity,
                    _motion_geo_identity,
                    tuple(getattr(model_payload, "hidden_mesh_ids", ())
                          if _use_model_payload else ()),
                    str(getattr(cam_payload, "node_id", "")
                        if cam_payload is not None else "builtin"),
                    int(single_ortho_view),
                )
                try:
                    self._render_motion_pass(
                        ctx, w, h, proj, view, model,
                        int(getattr(eval_ctx, "frame_number", 0)
                            if eval_ctx is not None else 0),
                        _motion_scene_key, import_ok,
                        instance_enabled, instance_count,
                        flat_meshes_src=_motion_model_src,
                        anim_worlds_src=_depth_anim_src,
                        geo_payloads_src=_geo_payloads,
                        hidden_mesh_ids=(
                            getattr(model_payload, "hidden_mesh_ids", ())
                            if _use_model_payload else ()))
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning(
                        "Scene3D motion_vector AOV pass 失败，跳过",
                        exc_info=True)
                    texture = self._aov_textures.pop(
                        AOVPass.MOTION_VECTOR.value, None)
                    fbo = self._aov_fbos.pop(
                        AOVPass.MOTION_VECTOR.value, None)
                    depth_rb = self._aov_depth_rbs.pop(
                        AOVPass.MOTION_VECTOR.value, None)
                    if fbo is not None:
                        try: fbo.release()
                        except Exception: pass
                    if depth_rb is not None:
                        try: depth_rb.release()
                        except Exception: pass
                    if texture is not None:
                        try: texture.release()
                        except Exception: pass

            # ── 6b4. P7：选中对象描边（inverted hull，叠加在主渲染上）──
            try:
                self._render_outline_pass(
                    ctx, w, h, proj, view, model,
                    import_ok, instance_enabled, instance_count,
                    _depth_flat_src, _depth_anim_src,
                    _model_payloads, _geo_payloads,
                    _p7_selected_nid or "",
                    anim_enabled=anim_enabled,
                    anim_time=anim_time,
                    anim_speed=anim_speed)
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    "Scene3D outline pass 失败，跳过", exc_info=True)

            # ── 6c. M2: 四视图正交渲染（quad_view 时额外渲染 TOP/RIGHT/FRONT）──
            # 复用本帧已上传的几何（VAO）+ IBL + 灯光，换 ortho proj + look_at 重新画。
            # quad_view=False 时释放旧正交纹理（切回单视图不残留资源）。
            # 注意：正交纹理的 glo 转发在 content_changed 块外（§6d），保证 cache-hit
            # 帧（content_changed=False）也能把缓存的正交纹理 glo 回传给 viewport。
            # Milestone C / P2-4：导航态（NAVIGATION/FAST_SOLID）时活动正交视图高频更新，
            # 非活动视图**降频**（每 inactive_view_fps 对应间隔才重渲染一次，非全跳过）。
            # quad_active_view: 0=perspective,1=top,2=right,3=front。
            # 若活动视图是透视，三个正交视图都降频；活动视图是某正交则另两个降频。
            # 受 inactive_view_skip_in_nav 设置控制；quad_view_quality=active_textured 才生效。
            # 降频间隔 = 1000/inactive_view_fps（默认 8 FPS → 125ms）。
            _quad_quality = str(_sp("quad_view_quality", "all_textured"))
            _skip_nav = bool(_sp("inactive_view_skip_in_nav", True))
            _skip_views: Optional[set] = None
            # P2-3：solid_views——非活动正交视图降质（solid/标量 PBR，不绑材质贴图）。
            # 只在导航态（NAVIGATION/FAST_SOLID）生效，静止态全部视图完整材质。
            _solid_views: Optional[set] = None
            _active_ortho = {1: "top", 2: "right", 3: "front"}.get(
                quad_active_view, None)
            if _active_ortho is None:
                _non_active_ortho = {"top", "right", "front"}
            else:
                _non_active_ortho = {"top", "right", "front"} - {_active_ortho}
            if (_quad_quality == "active_textured"
                    and _qp in (ViewportQualityProfile.NAVIGATION,
                                ViewportQualityProfile.FAST_SOLID)):
                _solid_views = set(_non_active_ortho)
            if (_skip_nav and _quad_quality == "active_textured"
                    and _qp in (ViewportQualityProfile.NAVIGATION,
                                ViewportQualityProfile.FAST_SOLID)):
                # 降频间隔（秒）：默认 8 FPS = 0.125s
                try:
                    _iv_fps = max(1, int(_sp("inactive_view_update_rate", 8)))
                except (TypeError, ValueError):
                    _iv_fps = 8
                _iv_interval = 1.0 / _iv_fps
                _now = time.monotonic()
                # 降频：距上次渲染 >= 间隔才更新（不进 skip_views）；否则 skip。
                # 首次（_ortho_last_render=0）一定更新。
                _skip_views = set()
                for _vk in _non_active_ortho:
                    _last = self._ortho_last_render.get(_vk, 0.0)
                    if _last > 0 and (_now - _last) < _iv_interval:
                        _skip_views.add(_vk)
                _pstats.lod_level = max(_pstats.lod_level, 1)
            if quad_view:
                try:
                    self._render_ortho_views(
                        ctx, w, h, cam_target, cam_dist,
                        proj, view, eye, model,
                        prog, _geo_payloads, _model_payloads,
                        dirs_t, cols_t, ints_t, light_count,
                        irradiance_cube, prefilter_cube, brdf_lut,
                        env_int_val, prefilter_max_lod,
                        instance_enabled, instance_count,
                        show_grid, show_axes, cam_pitch,
                        bgr, bgg, bgb, bg_opaque,
                        ocr, ocg, ocb, metallic, roughness,
                        cam_fov=cam_fov,
                        grid_cell_size=grid_cell_size,
                        grid_opacity=grid_opacity,
                        grid_line_width=grid_line_width,
                        grid_minor_fade_start=grid_minor_fade_start,
                        grid_minor_fade_end=grid_minor_fade_end,
                        grid_minor_fade_slope=grid_minor_fade_slope,
                        grid_major_fade_start=grid_major_fade_start,
                        grid_major_fade_end=grid_major_fade_end,
                        grid_major_fade_slope=grid_major_fade_slope,
                        grid_minor_color=grid_minor_color,
                        grid_major_color=grid_major_color,
                        skip_views=_skip_views,
                        solid_views=_solid_views)
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning(
                        "Scene3D 正交视图渲染失败，跳过", exc_info=True)
                    self._release_ortho_views(ctx)
            else:
                # quad 关闭：释放上一帧残留的正交纹理/FBO
                if any(self._ortho_textures.values()):
                    self._release_ortho_views(ctx)
            self._last_quad_view = quad_view

            self._last_content_sig = content_sig
            self._last_scene_content_sig = scene_content_sig
            self._last_size = (w, h)
            self._revision += 1

        # ── 6d. M2: 每帧转发正交纹理 glo（cache-hit 帧也生效）──
        # base_evaluator 每帧开头清空 eval_ctx.ortho_view_textures=None。这里必须在
        # content_changed 块**外**重新填充，否则 cache-hit 帧（场景未变但页面/视口
        # 仍请求渲染，如 nav preview 重绘）会丢失正交纹理 → viewport 清空三个视图。
        # 节点缓存的 _ortho_textures（含有效 glo）在 quad_view 未关时一直保留。
        if eval_ctx is not None:
            if quad_view and any(self._ortho_textures.values()):
                ow, oh = self._ortho_size
                eval_ctx.ortho_view_textures = {
                    k: (tex.glo, ow, oh)
                    for k, tex in self._ortho_textures.items()
                    if tex is not None
                }
            else:
                eval_ctx.ortho_view_textures = None

        # ── 6e. P7：每帧转发 ID picking 信息（cache-hit 帧也生效）──
        # 同 §6d 模式：base_evaluator 每帧清空 id_pick_info=None，这里在 content_changed
        # 块外回填。content_changed 帧从 _render_id_pass 拿到最新 pick_map；cache-hit 帧
        # 用上一帧的 pick_map（场景内容未变，map 仍有效）。
        if eval_ctx is not None:
            _id_info = getattr(self, "_p7_id_pick_info", None)
            if _id_info is not None and self._id_texture is not None:
                eval_ctx.id_pick_info = _id_info
            else:
                eval_ctx.id_pick_info = None

        # ── 7. 构造 TextureResult 返回（符合 HDR 线性工作空间 + 深度合成）──
        img_fmt = ImageFormat(width=w, height=h, name=fmt_name)
        has_depth = self._depth_texture is not None
        # P0 / Milestone A：finalize perf stats（total_cpu_ms 用整段 _do_evaluate 计时）。
        # _do_evaluate_start 在函数开头记录；此处结算 total 并写 render_diagnostics。
        # cache-hit 帧 perspective/top/... 接近 0（主渲染被跳过），符合直觉。
        _pstats.total_cpu_ms = (time.perf_counter() - _do_evaluate_t0) * 1000.0
        # AUD-7309: model_cache_hit 从模型导入缓存状态（diagnostics 已有该
        # 字段）换算三态标志。旧代码读取从未赋值的 self._perf_model_cache_hit，
        # 字段恒 None，失去诊断价值。
        _pstats.model_cache_hit = model_cache_hit_from_status(
            getattr(model_payload, "model_cache_status", None)
            if model_payload is not None else None)
        # P6：场景内容缓存命中——本帧进了渲染块但场景内容未变（仅相机/视图变化），
        # 资源（render target/mesh VAO/IBL）复用，只重发 draw call。None=cache-hit 帧未渲染。
        _pstats.scene_content_cache_hit = (
            bool(_scene_content_changed is False)
            if (content_changed and self._gpu_texture is not None) else None)
        # texture_cache_hit：本帧所有贴图绑定是否全命中（无 uploads 即命中）。
        if _pstats.texture_binds > 0:
            _pstats.texture_cache_hit = (_pstats.texture_uploads == 0)
        # P8-0：VBO 缓存命中（_ensure_flat_meshes_gl 汇总 MeshVBOPool 命中）。
        _pstats.vbo_cache_hit = self._last_vbo_cache_hit
        # 节流日志（debug 开关下每秒一次）
        self._perf_logger.log(_pstats)
        # P7: 渲染诊断元数据（供 UI 状态栏/日志显示当前渲染状态）。
        # 用局部变量（cam_payload/light_payload/geo_payload/model_payload 可能
        # 在 content_changed=False 分支未重新收集，但上一帧值仍可用）。
        cryptomatte_metadata = self._build_cryptomatte_metadata(
            model_payload, _geo_payloads)
        diagnostics = {
            "active_camera": "external" if cam_payload is not None else "builtin",
            # 活动相机的 node_id（无外接相机时为 ""，供视口图标高亮判断用）。
            "active_camera_id": (getattr(cam_payload, "node_id", "")
                                 if cam_payload is not None else ""),
            "light_count": int(light_count),
            "has_external_light": light_payload is not None,
            "geometry_source": ("model" if _use_model_payload
                                 else ("geo3d" if _use_geo_payload
                                       else "builtin")),
            "flat_mesh_count": (len(model_payload.flat_meshes)
                                if model_payload is not None and model_payload.has_model
                                else 0),
            "model_error": (model_payload.model_error
                            if model_payload is not None else None)
                        or self._model_error,
            "model_diagnostics": list(
                getattr(model_payload, "diagnostics", ())
                if model_payload is not None else ()
            ),
            # Milestone E / P4-6：模型导入缓存状态（"in_memory"/"disk_hit"/"miss"）。
            "model_cache_status": (model_payload.model_cache_status
                                   if model_payload is not None else None),
            # P2-03：后台导入阶段化进度。只传递不可变/JSON-safe 元数据，
            # UI 可以据此显示加载状态，而不接触 worker Future 或 GL 资源。
            "model_import_status": (getattr(
                model_payload, "model_import_status", None)
                if model_payload is not None else None),
            "model_import_phase": str(getattr(
                model_payload, "model_import_phase", "") or "")
                if model_payload is not None else "",
            "model_import_progress": float(getattr(
                model_payload, "model_import_progress", 0.0) or 0.0)
                if model_payload is not None else 0.0,
            "model_import_diagnostics": list(getattr(
                model_payload, "model_import_diagnostics", ())
                if model_payload is not None else ()),
            "model_import_resource_estimate": dict(getattr(
                model_payload, "model_import_resource_estimate", {}) or {})
                if model_payload is not None else None,
            "model_import_probe": dict(getattr(
                model_payload, "model_import_probe", {}) or {})
                if model_payload is not None else None,
            # F7/F0：UI 面板通过 metadata_ready 消费纯 Python 摘要；不要将
            # AnimationClip / ModelImportReport 实例跨 render/UI 线程传递。
            "model_animation_clips": [
                {"name": str(name), "duration": float(duration)}
                for name, duration in getattr(
                    model_payload, "animation_clips", ())
            ] if model_payload is not None else [],
            "model_import_report": getattr(
                model_payload, "import_report", None)
                if model_payload is not None else None,
            "model_materials": list(
                getattr(model_payload, "materials", ())
                if model_payload is not None else ()
            ),
            "model_source_path": str(getattr(
                model_payload, "model_path", "") or "")
                if model_payload is not None else "",
            "model_node_id": str(getattr(
                model_payload, "node_id", "") or "")
                if model_payload is not None else "",
            # P1-21：manifest 与现有 object_id pass 对齐；当前仍是
            # manifest-only 契约，真正的多层 Cryptomatte 写入留给交付后端。
            "cryptomatte": cryptomatte_metadata,
            "model_hierarchy": None,
            "model_hidden_mesh_ids": list(
                getattr(model_payload, "hidden_mesh_ids", ())
                if model_payload is not None else ()
            ),
            "model_user_hidden_mesh_ids": list(
                getattr(model_payload, "user_hidden_mesh_ids",
                        getattr(model_payload, "hidden_mesh_ids", ()))
                if model_payload is not None else ()
            ),
            "model_locked_mesh_ids": list(
                getattr(model_payload, "locked_mesh_ids", ())
                if model_payload is not None else ()
            ),
            "model_solo_mesh_id": str(
                getattr(model_payload, "solo_mesh_id", "") or "")
                if model_payload is not None else "",
            "model_render_role": (
                "holdout" if self._has_holdout(getattr(model_payload, "render_flags", None))
                else ("shadow_catcher" if getattr(
                    getattr(model_payload, "render_flags", None), "shadow_catcher", False)
                      else ("matte" if getattr(
                          getattr(model_payload, "render_flags", None), "matte_object", False)
                            else "solid"))
                if model_payload is not None else "solid"
            ),
            "geo_render_roles": [
                ("holdout" if self._has_holdout(getattr(gp, "render_flags", None))
                 else ("shadow_catcher" if getattr(
                     getattr(gp, "render_flags", None), "shadow_catcher", False)
                       else ("matte" if getattr(
                           getattr(gp, "render_flags", None), "matte_object", False)
                             else "solid")))
                for gp in (_geo_payloads or ())
            ],
            "holdout_active": bool(
                self._has_holdout(getattr(model_payload, "render_flags", None))
                or any(self._has_holdout(getattr(gp, "render_flags", None))
                       for gp in (_geo_payloads or ()))
            ),
            "shadow_catcher_active": bool(
                getattr(getattr(model_payload, "render_flags", None),
                        "shadow_catcher", False)
                or any(getattr(getattr(gp, "render_flags", None),
                               "shadow_catcher", False)
                       for gp in (_geo_payloads or ()))
            ),
            "model_anim_time": (float(model_payload.anim_time)
                                if model_payload is not None else 0.0),
            "msaa": int(self._msaa_samples),
            "has_depth": has_depth,
            # P0-07: expose the auxiliary passes with the same dimensions as
            # beauty. Depth is also kept in depth_texture_id for the existing
            # DeepMerge contract; object_id remains available to the picker.
            "aov_passes": [
                AOVPass.BEAUTY.value,
                *( [AOVPass.DEPTH.value] if has_depth else []),
                *([AOVPass.NORMAL.value]
                  if self._aov_textures.get(AOVPass.NORMAL.value) is not None
                  else []),
                *([AOVPass.POSITION.value]
                  if self._aov_textures.get(AOVPass.POSITION.value) is not None
                  else []),
                *([AOVPass.UV.value]
                  if self._aov_textures.get(AOVPass.UV.value) is not None
                  else []),
                *([AOVPass.MATERIAL_ID.value]
                  if self._aov_textures.get(AOVPass.MATERIAL_ID.value) is not None
                  else []),
                *([AOVPass.MOTION_VECTOR.value]
                  if self._aov_textures.get(AOVPass.MOTION_VECTOR.value) is not None
                  else []),
                *([AOVPass.OBJECT_ID.value]
                  if self._id_texture is not None else []),
            ],
            "resolution": (w, h),
            "lod_active": bool(self._lod_active),
            # P0：3D 视口性能统计（viewer debug overlay / perf panel / benchmark 用）。
            "perf_stats": _pstats.to_dict(),
            # Milestone D / P3：材质状态缓存命中率（连续同材质跳过 rebind 的比例）。
            "material_cache_hit_rate": round(self._material_cache.hit_rate, 3),
        }
        if model_payload is not None:
            try:
                from ..render3d.scene_hierarchy import hierarchy_to_dict
                diagnostics["model_hierarchy"] = hierarchy_to_dict(
                    getattr(model_payload, "hierarchy", None))
            except Exception:
                diagnostics["model_hierarchy"] = None
        # P2-06：把历史兼容的模型错误/代码字段归一化为统一 runtime
        # records；仍保留原字段供旧 UI 和插件读取。
        from ..core.runtime_diagnostics import records_from_scene_diagnostics
        diagnostics["diagnostic_records"] = [
            record.to_dict() for record in records_from_scene_diagnostics(
                diagnostics,
                frame=getattr(_pstats, "frame_id", None),
            )
        ]
        aov_texture_ids = {}
        aov_dimensions = {}
        if has_depth:
            aov_texture_ids[AOVPass.DEPTH.value] = self._depth_texture.glo
            aov_dimensions[AOVPass.DEPTH.value] = (w, h)
        for _aov_name in (AOVPass.NORMAL.value, AOVPass.POSITION.value,
                          AOVPass.UV.value, AOVPass.MATERIAL_ID.value,
                          AOVPass.MOTION_VECTOR.value):
            _aov_texture = self._aov_textures.get(_aov_name)
            if _aov_texture is not None:
                aov_texture_ids[_aov_name] = _aov_texture.glo
                aov_dimensions[_aov_name] = (w, h)
        if self._id_texture is not None:
            aov_texture_ids[AOVPass.OBJECT_ID.value] = self._id_texture.glo
            aov_dimensions[AOVPass.OBJECT_ID.value] = (w, h)
        diagnostics["aov_dimensions"] = dict(aov_dimensions)
        beauty_result = TextureResult(
                texture_id=self._gpu_texture.glo,
                width=w,
                height=h,
                revision=self._revision,
                format=img_fmt,
                bbox=BBox.full_frame(w, h),
                alpha_premultiplied=True,  # 3D 渲染通常预乘，背景透明区 RGB=0
                color_space=ColorSpace.LINEAR,  # 线性 HDR 工作空间
                # 深度合成（阶段六）：R32F 可采样线性深度，供 deep_merge 消费
                depth_texture_id=self._depth_texture.glo if has_depth else None,
                has_deep_data=has_depth,
                aov_texture_ids=aov_texture_ids,
                aov_dimensions=aov_dimensions,
                # P7: 渲染诊断元数据（UI 状态栏/日志用）
                render_diagnostics=diagnostics,
            )
        outputs: Dict[str, TextureResult] = {"display": beauty_result}
        for _aov_name, _aov_texture_id in aov_texture_ids.items():
            _aov_width, _aov_height = aov_dimensions.get(
                _aov_name, (w, h))
            outputs[_aov_name] = self._make_aov_output_result(
                beauty_result, _aov_name, _aov_texture_id,
                _aov_width, _aov_height)
        return outputs

    @staticmethod
    def _make_aov_output_result(
            source: TextureResult, aov_name: str, texture_id: int,
            width: int, height: int) -> TextureResult:
        """Expose one auxiliary pass as a connectable image result.

        The result keeps a one-entry AOV map so downstream unary nodes and
        FileOutput can continue to select the pass by semantic name.  The
        beauty depth contract is cleared because a direct AOV output is data,
        not a deep-compositing beauty image.
        """
        return source.with_updates(
            texture_id=int(texture_id), width=int(width), height=int(height),
            color_space="data", alpha_premultiplied=False,
            depth_texture_id=None, has_deep_data=False,
            aov_texture_ids={str(aov_name): int(texture_id)},
            aov_dimensions={str(aov_name): (int(width), int(height))},
            aov_semantic=str(aov_name),
        )

    def cleanup(self) -> None:
        """释放所有 GL 资源（节点移除时由 evaluator 调用）。"""
        self._release_render_target()
        self._release_ortho_views()
        self._release_mesh_geometry()
        # 导入模型扁平 mesh 资源（阶段五）
        self._release_flat_meshes_gl(self._last_gl_ctx)
        self._last_gl_ctx = None
        self._model_root = None
        self._model_path_cached = None
        self._model_error = None
        # Model3D 材质升级：释放材质贴图加载器缓存的 GL 纹理
        if self._material_tex_loader is not None:
            self._material_tex_loader.release_all()
            self._material_tex_loader = None
        if self._gizmo is not None:
            self._gizmo.release()
            self._gizmo = None
        # F6：骨骼 overlay 渲染器释放
        if self._skeleton_renderer is not None:
            self._skeleton_renderer.release()
            self._skeleton_renderer = None
        if self._hdri_baker is not None:
            self._hdri_baker.release()
            self._hdri_baker = None
        # 实例化（阶段六）：instance buffer + instanced program
        if self._instance_vbo is not None:
            try: self._instance_vbo.release()
            except Exception: pass
            self._instance_vbo = None
            self._instance_sig_cached = None
            self._instance_count_cached = 0
        if self._mesh_instanced_prog is not None:
            self._mesh_instanced_prog.release()
            self._mesh_instanced_prog = None
        # P3-4：清空 sampler-loc 缓存（programs 已释放，引用失效）
        self._sampler_loc_set_progs.clear()
        # 深度写入程序（阶段六）
        if self._depth_write_prog is not None:
            self._depth_write_prog.release()
            self._depth_write_prog = None
        if self._depth_write_skinned_prog is not None:
            self._depth_write_skinned_prog.release()
            self._depth_write_skinned_prog = None
        if self._depth_write_instanced_prog is not None:
            self._depth_write_instanced_prog.release()
            self._depth_write_instanced_prog = None
        # P7：ID picking 程序
        if self._id_write_prog is not None:
            self._id_write_prog.release()
            self._id_write_prog = None
        if self._id_write_skinned_prog is not None:
            self._id_write_skinned_prog.release()
            self._id_write_skinned_prog = None
        # P7：outline 程序
        if self._outline_prog is not None:
            self._outline_prog.release()
            self._outline_prog = None
        for prog_attr in (
                "_shadow_depth_prog", "_shadow_depth_skinned_prog",
                "_shadow_depth_instanced_prog", "_shadow_catcher_prog",
                "_shadow_catcher_skinned_prog",
                "_shadow_catcher_instanced_prog"):
            prog = getattr(self, prog_attr, None)
            if prog is not None:
                try: prog.release()
                except Exception: pass
                setattr(self, prog_attr, None)
        for prog_attr in (
                "_aov_prog", "_aov_instanced_prog", "_aov_skinned_prog",
                "_motion_prog", "_motion_instanced_prog",
                "_motion_skinned_prog"):
            prog = getattr(self, prog_attr, None)
            if prog is not None:
                try: prog.release()
                except Exception: pass
                setattr(self, prog_attr, None)
        self._motion_prev_proj = None
        self._motion_prev_view = None
        self._motion_prev_frame = None
        self._motion_prev_scene_key = None
        self._motion_prev_models = {}
        self._motion_prev_anim_worlds = {}
        if self._mesh_prog is not None:
            self._mesh_prog.release()
            self._mesh_prog = None
        if self._mesh_skinned_prog is not None:
            self._mesh_skinned_prog.release()
            self._mesh_skinned_prog = None
