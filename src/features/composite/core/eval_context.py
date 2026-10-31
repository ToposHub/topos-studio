"""Composite evaluation context - wraps GL + business context.

Provides a single object that bundles the ModernGL context together with
optional business-level context (runtime_context, timeline_clip_gpu_provider,
resource_pool, framebuffer_manager) so that source nodes like
TimelineClipInputNode can access timeline clip data during graph evaluation.

Usage:
    eval_ctx = CompositeEvalContext(
        gl=moderngl_ctx,
        runtime_context=runtime_ctx,
        timeline_clip_gpu_provider=provider,
        resource_pool=pool,
        framebuffer_manager=fb_mgr,
    )

    # Non-source nodes only need GL context:
    node.evaluate(inputs, eval_ctx.gl, eval_ctx.framebuffer_manager,
                  eval_ctx.resource_pool, render_mode=render_mode)

    # Source nodes that need business context receive the full eval_ctx:
    if isinstance(ctx, CompositeEvalContext):
        runtime = ctx.runtime_context
        provider = ctx.timeline_clip_gpu_provider
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class CompositeEvalContext:
    """Evaluation context passed to composite nodes during graph evaluation.

    Wraps the ModernGL context and optional business-level context so that
    source nodes like TimelineClipInputNode can access timeline clip data.

    Attributes:
        gl: Core ModernGL context (moderngl.Context).
        runtime_context: Optional runtime context for timeline clip input nodes
                         (CompositeRuntimeContext).
        timeline_clip_gpu_provider: Optional GPU texture provider for timeline
                                    clip frames (TimelineClipFrameProvider).
        resource_pool: Optional GPU resource pool (GPUResourcePool).
        framebuffer_manager: Optional framebuffer manager (FramebufferManager).
    """

    # Core GL context (moderngl.Context)
    gl: Any = None

    # Optional runtime context for timeline clip input nodes
    runtime_context: Any = None  # CompositeRuntimeContext

    # Convenience mirror for direct callers; the runtime context remains the
    # authoritative source for controlled caption data.
    caption_input: Any = None

    # Optional GPU texture provider for timeline clip frames
    timeline_clip_gpu_provider: Any = None  # TimelineClipFrameProvider

    # Optional resource pool
    resource_pool: Any = None

    # Optional framebuffer manager
    framebuffer_manager: Any = None

    # Optional ConfigManager for colour-engine selection (builtin vs OCIO).
    # Carried here so nodes that do their own colour conversion (e.g.
    # FileOutputNode) honour the user's composite.color_engine preference
    # instead of always defaulting to OCIO. None → OCIO default (legacy).
    config_manager: Any = None

    # 阶段七双轨制：scene 数据旁路。
    # 现有求值管线全程硬编码 TextureResult，scene payload（含 GL 对象引用）
    # 无法走标准缓存轨道。Camera3D/Light3D/Geo3D/Model3D 源节点在 _do_evaluate
    # 内把真正的 ScenePayload 写入此字典（key=node_id, value={port: payload}），
    # Scene3D 聚合器据此读取上游场景数据。
    # 求值器在每次 frame 开始时清空；None 表示未启用旁路（向后兼容）。
    scene_payloads: Any = None  # Optional[Dict[str, Dict[str, "ScenePayload"]]]

    # P1.5 端口感知连接映射：{node_id: {input_port: upstream_node_id}}。
    # 由求值器每帧从 snapshot 连接构建（仅 scene 类型源节点的 scene 输入端口）。
    # Scene3D 用它做端口感知 payload 收集——只读真正连到 camera_in/lights_in/
    # geometry_in 的上游 payload，断开的场景节点不再影响渲染。
    # None 表示未启用（Scene3D 退化为全局扫描，保持向后兼容）。
    scene_connections: Any = None  # Optional[Dict[str, Dict[str, str]]]

    # P7: 交互态标记（导航拖拽/handle 拖拽进行中）。由 renderer 从 RenderRequest
    # 同步；Scene3D 据此降质（LOD）：交互中临时降 MSAA/简化，保证大场景流畅；
    # 交互结束后下一帧恢复完整质量。None/False = 非交互态（完整质量）。
    interaction_active: Any = False

    # P2-04: semantic project quality profile. ``proxy_scale``/LOD remains
    # the concrete resolution control; this label lets nodes such as Scene3D
    # choose a predictable draft/preview/final shading policy and keeps the
    # same profile visible in cache keys and diagnostics.
    quality_profile: str = "preview"

    # M2: 四视图模式开关。由 renderer 从 RenderRequest.quad_view 同步；Scene3D
    # 据此决定是否额外渲染 TOP/RIGHT/FRONT 三个正交视图纹理。False = 仅透视。
    quad_view: Any = False

    # M2: 正交视图纹理旁路。Scene3D 渲染正交视图后把 {view_kind: (glo, w, h)} 写入此
    # 字典；composite_renderer 读取后经 metadata_ready 传给 viewport。每帧清空（None
    # 表示本帧无正交纹理，如 quad_view=False 或渲染失败）。
    ortho_view_textures: Any = None  # Optional[Dict[str, Tuple[int, int, int]]]

    # M2: 单视图正交 override（0/None=perspective，1=top，2=right，3=front）。
    # SINGLE 模式下，若非 0，Scene3D 用对应正交投影渲染**主纹理**（全分辨率），
    # 让中键从 quad 的正交象限进入单视图时能继续显示该正交视角。
    single_ortho_view: Any = None
    frame_number: int = 0

    # CP-021: 合成级运动模糊快门（0..1）。由渲染器从 Output 节点的
    # motion_blur_shutter 参数解析注入；VectorBlur 等 motion-blur 消费节点
    # 在 use_project_shutter=True 时读取。None = 未设置（节点回落自身 shutter）。
    motion_blur_shutter: Any = None

    # P0-04: 下游 time 节点（time_offset/frame_hold/retime）的帧重映射：
    # {node_id: TimeRemap}。由求值器每帧从 snapshot.frame_overrides 同步；
    # 读帧源节点（video_frame）在解析解码帧前把播放头经重映射换算。
    # None/空 = 无重映射（向后兼容，播放头直通）。
    node_frame_overrides: Any = None

    # Milestone C / P2：四视图活动视图代码（0/None=perspective，1=top，2=right，3=front）。
    # quad 模式下鼠标所在象限=活动视图。Scene3D 据此对非活动视图降质/降频：
    # 导航态时非活动正交视图可跳过或用 solid，保活动视图流畅。None=无活动信息
    # （向后兼容，退化为全部正交视图同质量）。
    quad_active_view: Any = None

    # 全局 3D 场景默认设置（来自 viewer_state["default_3d_scene"]，已与
    # DEFAULT_VIEWER3D_SCENE_SETTINGS 合并）。Scene3D 作为纯渲染器时，所有
    # 场景功能（默认相机/灯光/背景/IBL/网格/输出控制/活动相机）统一从此处读取，
    # 不再依赖节点自身参数。payload（连接的 Camera3D/Light3D）仍优先覆盖。
    # None = 未注入（向后兼容旧路径，Scene3D 退化为读节点自身参数）。
    global_3d_scene_settings: Any = None  # Optional[dict]

    # 预览上下文覆盖：{node_id: {show_grid/bg_opaque/show_axes: bool}}。
    # 由 CompositePage._apply_view_mode_overrides 构建——3D 预览的 Scene3D 用
    # 预览参考开关（show_grid/bg_opaque/show_axes），合成输出的 Scene3D 用
    # output_* 开关。因为场景参数已不在节点上（移至全局设置），快照层无法再
    # 直接覆盖节点参数，改为经此字典按 node_id 精确覆盖。Scene3D 的 _sp()
    # 读取顺序：view_mode_overrides > global_3d_scene_settings > 节点参数。
    # None = 无覆盖（所有 Scene3D 用全局设置默认值）。
    view_mode_overrides: Any = None  # Optional[Dict[str, Dict[str, Any]]]

    # ── P7：DCC 对象交互 ──
    # id_pick_info：Scene3D 渲染 ID pass 后写回——{tex_glo, w, h, pick_map}。
    # pick_map = {pick_id_int: node_id_str}；viewer 点击时读 1 像素 RGBA → 解码
    # pick_id → 查 map → node_id。None = 本帧无 ID pass（非 3D / 渲染失败）。
    id_pick_info: Any = None
    # selected_3d_node_id：当前选中的 3D 节点 id（Camera3D/Light3D/Geo3D/Model3D）。
    # Scene3D outline pass 据此给选中对象的 mesh 画描边。None = 无选中。
    selected_3d_node_id: Any = None
    # isolate_3d_node_id：隔离模式下的节点 id——只渲染该节点的 mesh（其余跳过）。
    # None = 不隔离（渲染全部）。Shift+I toggle。
    isolate_3d_node_id: Any = None
    # P7-5：component selection（面级）。选中的面 = {node_id → [triangle_indices]}。
    # Scene3D 据此高亮渲染选中的三角形（emissive 叠加或独立 overlay pass）。
    # None = 无面选中（OBJECT 模式或未点击）。mesh_picking.pick_face 产出。
    selected_faces_3d: Any = None

def resolve_project_motion_blur_shutter(snapshot) -> Optional[float]:
    """CP-021: composition-level shutter from the (first) Output node.

    Scans the snapshot's node parameters for an ``output`` node carrying
    ``motion_blur_shutter``; returns the clamped value or None when the
    graph has no authoritative setting (consumers fall back to their own
    node-level shutter).
    """
    nodes = getattr(snapshot, "nodes", None) or {}
    for ns in nodes.values():
        if getattr(ns, "node_type", "") != "output":
            continue
        params = getattr(ns, "parameters", None) or {}
        try:
            val = float(params.get("motion_blur_shutter", 0.5))
        except (TypeError, ValueError):
            return None
        return min(1.0, max(0.0, val))
    return None
