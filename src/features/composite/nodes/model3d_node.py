"""
Model3D Node — 3D 模型导入数据源节点（阶段七）。

把原本揉在 Scene3D 里的模型导入（glTF/FBX/OBJ/...）+ 场景层级 + 骨骼动画
独立成节点，输出 ModelPayload 到 Scene3D 聚合器的 model_in。

实现状态（CP-008 勘误）：本文件为完整实现 —— _ensure_model（model_loader）
+ flatten_scene + _ensure_flat_meshes_gl（via MeshVBOPool）+ 骨骼动画/morph
采样 + 材质覆盖 + 纹理端口 override + LOD 代理。历史"提交 1：占位骨架"
阶段注释已过时，删除以免误导调研。
"""

import logging
import os
from typing import Any, Callable, Dict, Optional

import numpy as np

from .base_node import Node, Port, TextureResult, ParamDef


class Model3DNode(Node):
    """3D 模型导入源节点：glTF/FBX/OBJ 加载 + 场景层级 + 骨骼动画，输出给 Scene3D。"""

    node_type = "model3d"
    # CP-044: 3D 场景载荷族，不参与通用 mask。
    GENERIC_MASK_MIX = False

    # Parent relationships compose the node's object_pos/object_rot/scale
    # channels before Model3D builds its local model matrix.
    supports_parent_transform = True
    node_title = "Model3D"
    i18n_title_key = "nodes.model3d.title"
    header_color = "#5b3a8a"  # 紫，模型导入类

    SEARCH_ALIASES = ["model3d", "import", "gltf", "fbx", "obj", "模型", "导入",
                      "3d模型", "load"]
    DESCRIPTION = "3D 模型导入节点（glTF/FBX/OBJ）+ 场景层级 + 骨骼动画，输出给 Scene3D"
    TAGS = ["3d", "source", "import"]

    PARAM_DEFS = [
        # ── 导入模型（阶段五）──
        ParamDef("model_path", "file", "", group="导入 / Import"),
        ParamDef("use_imported_model", "bool", False, group="导入 / Import"),
        ParamDef("size_mode", "option", "real_world",
                 labels=["fit_to_scene", "real_world"], group="导入 / Import"),
        ParamDef("source_unit", "option", "m",
                 labels=["m", "cm", "mm", "in", "ft", "custom"], group="导入 / Import"),
        ParamDef("unit_scale", "float", 1.0, min_val=0.0001, max_val=1000.0, step=0.01,
                 group="导入 / Import"),
        ParamDef("axis_mode", "option", "auto",
                 labels=["auto", "y_up", "z_up", "none"], group="导入 / Import"),
        ParamDef("uv_v_mode", "option", "auto",
                 labels=["auto", "original", "flip"], group="导入 / Import"),
        # ── 物体变换（P0 缺口：Model3D 自带变换，gizmo 可直接写它）──
        # 拆成标量以支持动画。Geo3D 也有同款参数。
        ParamDef("object_pos_x", "float", 0.0, min_val=-100.0, max_val=100.0, step=0.1,
                 group="变换 / Transform"),
        ParamDef("object_pos_y", "float", 0.0, min_val=-100.0, max_val=100.0, step=0.1,
                 group="变换 / Transform"),
        ParamDef("object_pos_z", "float", 0.0, min_val=-100.0, max_val=100.0, step=0.1,
                 group="变换 / Transform"),
        ParamDef("object_rot_x", "float", 0.0, min_val=-360.0, max_val=360.0, step=1.0,
                 group="变换 / Transform"),
        ParamDef("object_rot_y", "float", 0.0, min_val=-360.0, max_val=360.0, step=1.0,
                 group="变换 / Transform"),
        ParamDef("object_rot_z", "float", 0.0, min_val=-360.0, max_val=360.0, step=1.0,
                 group="变换 / Transform"),
        ParamDef("mesh_scale", "float", 1.0, min_val=0.01, max_val=100.0, step=0.1,
                 group="变换 / Transform"),
        # ── 骨骼动画（阶段五；仅当导入模型含 skin 时生效）──
        ParamDef("anim_enabled", "bool", True, group="动画 / Animation"),
        ParamDef("anim_time", "float", 0.0, min_val=0.0, max_val=10000.0, step=0.1,
                 group="动画 / Animation"),
        ParamDef("anim_speed", "float", 1.0, min_val=0.0, max_val=5.0, step=0.05,
                 group="动画 / Animation"),
        # Model3D-F: 动画片段选择（多动画文件选 clip）
        ParamDef("anim_clip", "int", 0, min_val=0, max_val=63, step=1,
                 group="动画 / Animation"),
        # Composite timeline animation controls are exposed through Keyframes.
        # anim_play_mode: loop（循环，默认）/ pingpong / once / clamp
        ParamDef("anim_play_mode", "option", "loop",
                 labels=["loop", "pingpong", "once", "clamp"],
                 group="动画 / Animation"),
        # anim_clip_name: 按 clip 名选片段（空=用 anim_clip 索引；名优先）
        ParamDef("anim_clip_name", "str", "", group="动画 / Animation"),
        ParamDef("anim_time_offset", "float", 0.0, min_val=-100.0, max_val=100.0, step=0.1,
                 group="动画 / Animation"),
        ParamDef("anim_start_frame", "float", 0.0, min_val=0.0, max_val=10000.0, step=1.0,
                 group="动画 / Animation"),
        # anim_end_frame: 0=不裁剪（播放到 clip 结尾）
        ParamDef("anim_end_frame", "float", 0.0, min_val=0.0, max_val=10000.0, step=1.0,
                 group="动画 / Animation"),
        # F7：合成时间轴帧率（anim_time → 秒 的转换；默认 24）
        ParamDef("anim_fps", "float", 24.0, min_val=1.0, max_val=120.0, step=1.0,
                 group="动画 / Animation"),
        # ── Model3D 材质升级：材质模式 + 纹理控制 ──
        # material_mode 决定外部输入纹理是覆盖(imported)还是替换(override)还是混合(mix)
        ParamDef("material_mode", "option", "imported",
                 labels=["imported", "override", "mix"], group="材质 / Material"),
        ParamDef("normal_format", "option", "OpenGL",
                 labels=["OpenGL", "DirectX"], group="材质 / Material"),
        ParamDef("orm_layout", "option", "glTF ORM",
                 labels=["glTF ORM", "separate"], group="材质 / Material"),
        ParamDef("texture_filter", "option", "linear",
                 labels=["nearest", "linear", "mipmap"], group="材质 / Material"),
        ParamDef("texture_wrap", "option", "clamp",
                 labels=["repeat", "clamp", "mirror"], group="材质 / Material"),
        ParamDef("emissive_intensity", "float", 1.0, min_val=0.0, max_val=10.0, step=0.05,
                 group="材质 / Material"),
        ParamDef("material_overrides", "str", "", group="材质 / Material"),
        ParamDef("morph_weights", "str", "", group="动画 / Animation"),
        # ── Milestone F / P5-6：Viewport LOD 控制（auto/force LOD0/1/2）──
        # auto=按 quality profile 自动选（导航用低 LOD，静止用 LOD0）；
        # force_lod0/1/2=强制某级（调试/特殊场景用）。
        ParamDef("viewport_lod_mode", "option", "auto",
                 labels=["auto", "force_lod0", "force_lod1", "force_lod2"],
                 group="视口 LOD / Viewport LOD"),
        # Outliner state is serialized as stable hierarchy ids. Empty means
        # all imported mesh objects are visible.
        ParamDef("hidden_mesh_ids", "str", "", group="场景 / Outliner"),
        ParamDef("locked_mesh_ids", "str", "", group="场景 / Outliner"),
        ParamDef("solo_mesh_id", "str", "", group="场景 / Outliner"),
        ParamDef("render_role", "option", "solid",
                 labels=["solid", "holdout", "shadow_catcher", "matte"],
                 group="渲染角色 / Render Role"),
        ParamDef("matte_id", "int", 0, min_val=0, max_val=16777215, step=1,
                 group="渲染角色 / Render Role"),
        ParamDef("shadow_intensity", "float", 1.0, min_val=0.0, max_val=4.0,
                 step=0.05, group="渲染角色 / Render Role"),
        ParamDef("shadow_softness", "float", 0.2, min_val=0.0, max_val=1.0,
                 step=0.01, group="渲染角色 / Render Role"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        # Model3D-B: 7 个材质纹理 override 输入端口（接受普通 image 输出，可程序化生成/校色）
        self.inputs = [
            Port("base_color_in", "input", "image"),
            Port("normal_in", "input", "image"),
            Port("metallic_in", "input", "image"),
            Port("roughness_in", "input", "image"),
            Port("ao_in", "input", "image"),
            Port("emissive_in", "input", "image"),
            Port("mask_in", "input", "image"),
        ]
        self.outputs = [
            Port("display", "output", "scene"),
        ]
        self.parameters = {
            "model_path": "",
            "use_imported_model": False,
            "size_mode": "real_world",
            "source_unit": "m",
            "unit_scale": 1.0,
            "axis_mode": "auto",
            "uv_v_mode": "auto",
            # 物体变换（P0 缺口：Model3D 自带，gizmo 写它）
            "object_pos_x": 0.0, "object_pos_y": 0.0, "object_pos_z": 0.0,
            "object_rot_x": 0.0, "object_rot_y": 0.0, "object_rot_z": 0.0,
            "mesh_scale": 1.0,
            "anim_enabled": True,
            "anim_time": 0.0,
            "anim_speed": 1.0,
            "anim_clip": 0,
            # F7：AnimationState 高级播放控制
            "anim_play_mode": "loop",
            "anim_clip_name": "",
            "anim_time_offset": 0.0,
            "anim_start_frame": 0.0,
            "anim_end_frame": 0.0,
            "anim_fps": 24.0,
            # 材质升级参数
            "material_mode": "imported",
            "normal_format": "OpenGL",
            "orm_layout": "glTF ORM",
            "texture_filter": "linear",
            "texture_wrap": "clamp",
            "emissive_intensity": 1.0,
            "material_overrides": "",
            "morph_weights": "",
            # Milestone F / P5-6：Viewport LOD 模式
            "viewport_lod_mode": "auto",
            "hidden_mesh_ids": "",
            "locked_mesh_ids": "",
            "solo_mesh_id": "",
            "render_role": "solid", "matte_id": 0,
            "shadow_intensity": 1.0, "shadow_softness": 0.2,
        }
        # GL 资源 / 模型缓存（懒建并缓存，cleanup 释放）
        self._model_root = None
        self._model_path_cached = None
        self._model_mtime_cached = 0.0
        self._model_normalize_cached = True
        self._model_flip_uv_v_cached = False
        self._model_error = None
        self._model_hierarchy = None
        # P1-25/P2-03：CPU 解析在共享 worker 中进行；render thread 只接收
        # 完成结果并负责后续 GL 上传。句柄切换时会合作式取消旧请求。
        self._model_import_handle = None
        self._model_import_request = None
        self._model_import_ready_callback: Optional[Callable[[], None]] = None
        # AUD-7008：导入失败粘滞键 (request, mtime)。文件存在但内容损坏时，
        # 后台导入每次都以失败告终；不记忆失败会让每次求值都重新提交完整
        # 解析（FBX 还可能触发 assimp CLI 转换）。同键直接短路；path/mtime/
        # 选项任一变化即失效（见 _ensure_model 的成功/提交路径）。
        self._model_import_failed_key: Optional[tuple] = None
        # AUD-7010：外部工程参数的类型容错读取（_safe_param_float/_int 的
        # 告警去重集合——同一 (key, value) 只告警一次，避免播放期刷屏）。
        self._param_coerce_warned: set = set()
        # P2-03：后台导入的阶段化状态。只保存标量/字符串，允许安全地
        # 通过 ModelPayload 交给 Scene3D 和 Qt，而不暴露 Future/SceneGraph。
        self._model_import_status: Optional[str] = None
        self._model_import_phase: str = ""
        self._model_import_progress: float = 0.0
        self._model_import_diagnostics: tuple = ()
        self._model_import_resource_estimate: Optional[Dict[str, object]] = None
        self._model_import_probe: Optional[Dict[str, object]] = None
        self._last_gl_ctx = None
        self._flat_meshes_gl = []
        # Milestone E / P4-6：模型导入缓存状态（供 UI diagnostics 显示）。
        # "in_memory"=进程内 path+mtime 命中（最快）；"disk_hit"=FBX fallback GLB
        # 持久缓存命中（跳过 assimp CLI 转换）；"miss"=首次/源变化（完整加载）。
        # None=未加载/无路径。
        self._model_cache_status: Optional[str] = None
        # Milestone F / P5：每 flat mesh 的 LOD 变体（导航用低面数代理）。
        # key=flat mesh 在 _flat_meshes_gl 中的 index，value={1: LOD1 mesh,
        # 2: LOD2 mesh}（LOD0=原 mesh 不存；_resolve_lod_level 只会返回
        # 0/1/2，LOD3 在本节点不可达，不再构建——AUD-7009）。首次需要时
        # 惰性生成，按模型 path+mtime 缓存（模型变则清空）。蒙皮 mesh 不
        # 生成 LOD（保留 LOD0）。
        self._mesh_lod_variants: dict = {}
        # Model3D 当前启用的 LOD 等级（0=LOD0，2=LOD2 导航态）。供 Scene3D 选择。
        self._current_lod_level: int = 0
        # F7/P1-24：动画采样缓存只存纯 numpy local matrices，不跨线程/GL
        # 共享。clip identity 进入缓存 key，模型切换时显式清空。
        from ..render3d.animation import AnimationSamplerCache
        self._animation_sampler_cache = AnimationSamplerCache(
            quantize_hz=120.0, max_entries=32)
        self._morph_weights_sig: tuple = ()
        # P5-5：LOD 切换去闪烁。导航结束（interaction_active=False）后不立即切回
        # LOD0，而是保持导航 LOD 一段延迟（与 nav_restore_delay_ms 同步），让
        # restore timer 触发高质量重渲染时才平滑切回。避免停止拖动瞬间 LOD2→LOD0
        # 硬切造成的视觉闪动（顶点数突变、轮廓跳变）。
        # _lod_last_nav_time：最近一次 interaction_active=True 的时间戳
        # _lod_hold_ms：保持期（默认 200ms，略大于 nav_restore_delay）
        self._lod_last_nav_time: float = 0.0
        self._lod_hold_ms: float = 200.0
        # 程序（flat mesh 可能是 plain/skinned/tangent 变体；instanced 暂不支持——导入模型实例化
        # 是 niche，留待 Model3D 加 instance 参数或 Scene3D 复制器驱动）
        self._mesh_prog = None
        self._mesh_skinned_prog = None
        # F5：带切线的 mesh 用独立 vertex shader（_MESH_VERTEX_TANGENT 定义 in_tangent），
        # 必须与 VAO 的 '3f 3f 2f 3f' layout + in_tangent attr 匹配，否则 ModernGL
        # 报 KeyError: 'in_tangent'。
        self._mesh_tangent_prog = None

    # ── 程序懒建（与 scene3d_node 同款）──

    def _ensure_mesh_program(self, ctx):
        if self._mesh_prog is None:
            from ..core.shaders import _MESH_VERTEX, _MESH_PBR_FRAGMENT
            self._mesh_prog = ctx.program(
                vertex_shader=_MESH_VERTEX, fragment_shader=_MESH_PBR_FRAGMENT)
        return self._mesh_prog

    def _ensure_mesh_tangent_program(self, ctx):
        """F5：带切线的 mesh program（in_tangent 属性 + normal mapping 前置）。

        _MESH_VERTEX_TANGENT 定义 in_tangent，必须配合 '3f 3f 2f 3f' VAO layout。
        用错 program（无 in_tangent）会导致 ModernGL vertex_array 报 KeyError。
        """
        if self._mesh_tangent_prog is None:
            from ..core.shaders import _MESH_VERTEX_TANGENT, _MESH_PBR_FRAGMENT
            self._mesh_tangent_prog = ctx.program(
                vertex_shader=_MESH_VERTEX_TANGENT, fragment_shader=_MESH_PBR_FRAGMENT)
        return self._mesh_tangent_prog

    def _ensure_skinned_program(self, ctx):
        if self._mesh_skinned_prog is None:
            from ..core.shaders import _MESH_VERTEX_SKINNED, _MESH_PBR_FRAGMENT
            self._mesh_skinned_prog = ctx.program(
                vertex_shader=_MESH_VERTEX_SKINNED, fragment_shader=_MESH_PBR_FRAGMENT)
        return self._mesh_skinned_prog

    # ── 模型加载（原样搬自 scene3d_node.py:439-475）──

    def _safe_param_float(self, key: str, default: float,
                          min_val: Optional[float] = None,
                          max_val: Optional[float] = None) -> float:
        """AUD-7010: 容错读取 float 参数（外部工程数据防御）。

        ``parameters`` 可被 from_dict 的未声明键、脚本/插件写入污染；
        裸 ``float()`` 抛 ValueError/TypeError 会让求值器把整个节点静默
        置空。坏值回落 default 并告警（同一 (key, value) 只告警一次）。
        """
        raw = self.parameters.get(key, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            warn_key = (key, repr(raw))
            if warn_key not in self._param_coerce_warned:
                self._param_coerce_warned.add(warn_key)
                logging.getLogger(__name__).warning(
                    "Model3DNode %s: parameter %r has non-numeric value "
                    "%r — using default %r", self.id, key, raw, default)
            return float(default)
        if min_val is not None:
            value = max(min_val, value)
        if max_val is not None:
            value = min(max_val, value)
        return value

    def _safe_param_int(self, key: str, default: int,
                        min_val: Optional[int] = None,
                        max_val: Optional[int] = None) -> int:
        """AUD-7010: 容错读取 int 参数（外部工程数据防御）。"""
        raw = self.parameters.get(key, default)
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            warn_key = (key, repr(raw))
            if warn_key not in self._param_coerce_warned:
                self._param_coerce_warned.add(warn_key)
                logging.getLogger(__name__).warning(
                    "Model3DNode %s: parameter %r has non-numeric value "
                    "%r — using default %r", self.id, key, raw, default)
            return int(default)
        if min_val is not None:
            value = max(min_val, value)
        if max_val is not None:
            value = min(max_val, value)
        return value

    @staticmethod
    def _source_unit_to_meters(unit: str) -> float:
        """Return source-unit to project-meter scale."""
        return {
            "m": 1.0,
            "cm": 0.01,
            "mm": 0.001,
            "in": 0.0254,
            "ft": 0.3048,
            "custom": 1.0,
        }.get(str(unit), 1.0)

    @staticmethod
    def _axis_correction_matrix(axis_mode: str, bbox_min=None, bbox_max=None) -> np.ndarray:
        """Return source-coordinate correction into the project Y-up world.

        Topos 3D uses OpenGL-style right-handed coordinates with Y as up and
        cameras looking toward -Z. Many Blender/FBX assets arrive as Z-up. In
        auto mode, a tall Z extent with a much thinner Y extent is treated as
        Z-up and rotated into Y-up.
        """
        mode = str(axis_mode or "auto").lower()
        if mode in {"none", "y_up", "y-up", "yup"}:
            return np.eye(4, dtype=np.float32)
        use_z_up = mode in {"z_up", "z-up", "zup"}
        if mode == "auto" and bbox_min is not None and bbox_max is not None:
            try:
                extents = np.asarray(bbox_max, dtype=np.float32) - np.asarray(bbox_min, dtype=np.float32)
                if extents.shape[0] >= 3:
                    y_extent = max(float(abs(extents[1])), 1.0e-6)
                    z_extent = float(abs(extents[2]))
                    use_z_up = z_extent > (y_extent * 1.25)
            except Exception:
                use_z_up = False
        if not use_z_up:
            return np.eye(4, dtype=np.float32)
        from ..render3d import math_utils as m3d
        return m3d.rotate_x(-90.0)

    @staticmethod
    def _should_flip_uv_v(uv_v_mode: str, model_path: str) -> bool:
        """Resolve Model3D UV V mode to a concrete flip flag."""
        mode = str(uv_v_mode or "auto").lower()
        if mode == "flip":
            return True
        if mode in {"original", "none", "off"}:
            return False
        ext = ""
        try:
            import os
            ext = os.path.splitext(str(model_path))[1].lower()
        except Exception:
            pass
        return ext in {".fbx", ".obj", ".3ds", ".blend"}

    def _ensure_model(self, path: str, normalize: bool = True,
                      flip_uv_v: bool = False):
        """后台懒加载 + 缓存导入模型（按 path+mtime 缓存）。

        首次调用只提交 CPU 导入任务并立即返回；后续求值轮询句柄，完成后在
        render thread 接收 SceneGraphRoot。ModernGL 资源仍由
        ``_ensure_flat_meshes_gl`` 在本线程创建，避免跨线程使用 GL 对象。
        """
        from ..render3d.model_import_service import (
            IMPORT_CANCELLED, IMPORT_PHASE_CANCELLED, IMPORT_PHASE_DONE,
            IMPORT_PHASE_FAILED, IMPORT_PHASE_HANDOFF, ModelImportRequest,
            estimate_import_resources,
            get_shared_model_import_service,
        )
        try:
            mtime = os.path.getmtime(path) if path and os.path.isfile(path) else 0.0
        except OSError:
            mtime = 0.0
        request = ModelImportRequest.from_path(
            path, normalize=normalize, flip_uv_v=flip_uv_v)

        if (self._model_root is not None
                and self._model_path_cached == path
                and self._model_normalize_cached == bool(normalize)
                and self._model_flip_uv_v_cached == bool(flip_uv_v)
                and abs(self._model_mtime_cached - mtime) < 0.1):
            # P4-6：进程内缓存命中（最快路径，无任何重解析）。
            self._model_cache_status = "in_memory"
            self._model_import_status = "ready"
            self._model_import_phase = IMPORT_PHASE_DONE
            self._model_import_progress = 1.0
            self._model_import_diagnostics = ()
            self._model_import_resource_estimate = (
                estimate_import_resources(path).to_dict())
            self._model_import_probe = None
            return self._model_root

        # A matching request is allowed to finish off-thread. poll() never
        # blocks the render thread; the completion callback wakes the renderer
        # so the next evaluation can consume the root and upload GL resources.
        pending = self._model_import_handle
        if pending is not None:
            if self._model_import_request == request:
                snapshot = pending.progress_snapshot()
                self._model_import_resource_estimate = (
                    pending.resource_estimate.to_dict())
                self._model_import_progress = float(snapshot["progress"])
                self._model_import_phase = str(snapshot["phase"] or "")
                result = pending.poll()
                if result is None:
                    self._model_cache_status = "loading"
                    self._model_import_status = (
                        "cancelling" if pending.state == "cancelling" else "loading")
                    self._model_error = None
                    return None
                self._model_import_handle = None
                self._model_import_request = None
                self._model_import_progress = float(result.progress)
                self._model_import_phase = str(result.phase or "")
                if result.resource_estimate is not None:
                    self._model_import_resource_estimate = (
                        result.resource_estimate.to_dict())
                if result.probe is not None:
                    self._model_import_probe = result.probe.to_dict()
                if not result.succeeded:
                    self._model_error = result.error or (
                        "模型导入已取消" if result.status == IMPORT_CANCELLED
                        else "模型导入失败")
                    self._model_cache_status = (
                        "cancelled" if result.status == IMPORT_CANCELLED else "error")
                    self._model_import_status = (
                        "cancelled" if result.status == IMPORT_CANCELLED else "error")
                    self._model_import_phase = (
                        IMPORT_PHASE_CANCELLED
                        if result.status == IMPORT_CANCELLED else IMPORT_PHASE_FAILED)
                    self._model_import_progress = 1.0
                    self._model_import_diagnostics = (
                        (str(self._model_error),) if self._model_error else ())
                    if result.status != IMPORT_CANCELLED:
                        # AUD-7008: 内容损坏的失败按 (request, mtime) 粘滞，
                        # 后续同键求值不再重新提交完整解析；路径/mtime/选项
                        # 变化（request 或 mtime 不同）自动解除粘滞。
                        self._model_import_failed_key = (request, mtime)
                    return None
                root = result.root
                # CPU 解析已结束，但 render thread 还要在本次求值中创建
                # VAO/纹理；此时保持 handoff，待 GL 资源完成后再报 ready。
                self._model_import_status = "handoff"
                self._model_import_phase = IMPORT_PHASE_HANDOFF
                self._model_import_progress = min(0.85, float(result.progress))
            else:
                # A changed path/option invalidates the old request. Its
                # result is discarded even if third-party parsing is already
                # in flight.
                pending.cancel()
                self._model_import_handle = None
                self._model_import_request = None
                root = None
        else:
            root = None

        # 路径变化或首次：清旧缓存
        self._release_flat_meshes_gl(self._last_gl_ctx)
        self._animation_sampler_cache.clear()
        self._model_root = None
        self._model_path_cached = None
        self._model_hierarchy = None
        # P5：模型变化 → LOD 变体失效（不同模型的 LOD 不能复用）
        self._mesh_lod_variants = {}
        if not path:
            self._model_error = None
            self._model_cache_status = None
            self._model_import_status = None
            self._model_import_phase = ""
            self._model_import_progress = 0.0
            self._model_import_diagnostics = ()
            self._model_import_resource_estimate = None
            self._model_import_probe = None
            return None
        # 文件存在性是纯本地元数据检查，不需要等待后台导入。提前把缺失
        # 素材变成结构化诊断，避免首帧静默退化为 procedural 场景；真正的
        # 格式/后端/内容解析仍继续走后台服务，保持渲染线程非阻塞。
        if not os.path.isfile(path):
            self._model_error = "asset.missing: 模型文件不存在。"
            self._model_cache_status = "error"
            self._model_import_status = "error"
            self._model_import_phase = IMPORT_PHASE_FAILED
            self._model_import_progress = 1.0
            self._model_import_diagnostics = (self._model_error,)
            self._model_import_resource_estimate = (
                estimate_import_resources(path).to_dict())
            self._model_import_probe = None
            return None
        if root is None:
            if (self._model_import_failed_key is not None
                    and self._model_import_failed_key == (request, mtime)):
                # AUD-7008: 同一 (path, mtime, normalize, flip_uv_v) 的导入
                # 已经失败过——保持粘滞错误状态直接返回，不再把损坏资产
                # 反复塞给共享导入 worker（每次完整重解析 + 日志刷屏）。
                # 上面保留的 _model_error/_model_cache_status="error" 等
                # 状态即 UI 可见的失败原因。
                return None
            try:
                self._model_import_request = request
                self._model_import_handle = get_shared_model_import_service().submit(request)
                callback = self._model_import_ready_callback
                if callback is not None:
                    self._model_import_handle.add_done_callback(
                        lambda _handle: callback())
                self._model_error = None
                self._model_cache_status = "loading"
                snapshot = self._model_import_handle.progress_snapshot()
                self._model_import_status = "loading"
                self._model_import_phase = str(snapshot["phase"] or "")
                self._model_import_progress = float(snapshot["progress"])
                self._model_import_diagnostics = ()
                self._model_import_resource_estimate = (
                    self._model_import_handle.resource_estimate.to_dict())
                self._model_import_probe = None
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning("提交 Model3D 后台导入失败: %s", exc)
                self._model_import_handle = None
                self._model_import_request = None
                self._model_error = str(exc)
                self._model_cache_status = "error"
                self._model_import_status = "error"
                self._model_import_phase = IMPORT_PHASE_FAILED
                self._model_import_progress = 1.0
                self._model_import_diagnostics = (str(exc),)
                self._model_import_resource_estimate = None
                self._model_import_probe = None
                # AUD-7008: 提交失败同样粘滞（如预算/服务不可用），避免
                # 每帧求值重复 submit 抛错刷日志。
                self._model_import_failed_key = (request, mtime)
            return None

        self._model_root = root
        self._model_path_cached = path
        self._model_mtime_cached = mtime
        self._model_normalize_cached = bool(normalize)
        self._model_flip_uv_v_cached = bool(flip_uv_v)
        self._model_error = None
        # AUD-7008: 成功加载解除失败粘滞（新内容可重试）。
        self._model_import_failed_key = None
        try:
            from ..render3d.scene_hierarchy import build_hierarchy
            self._model_hierarchy = build_hierarchy(root, source_path=path)
        except Exception:
            # Hierarchy is UI metadata; a malformed optional summary must
            # never make an otherwise renderable model fail to load.
            self._model_hierarchy = None
        # P4-6：首次加载或源变化后重新加载——标记 miss（FBX fallback GLB 命中
        # 由 model_loader 内部透明处理，此处统一标 miss/disk_hit 取决于是否
        # 走了 CLI 转换；简化为：完整 load_model 调用=miss，因为无法在此层区分）。
        self._model_cache_status = "miss"
        self._model_import_status = "handoff"
        self._model_import_phase = IMPORT_PHASE_HANDOFF
        self._model_import_progress = 0.85
        self._model_import_diagnostics = ()
        return root

    def set_model_import_ready_callback(
            self, callback: Optional[Callable[[], None]]) -> None:
        """设置后台导入完成后的 render-thread 唤醒回调。"""
        self._model_import_ready_callback = callback

    # ── 扁平 mesh GL 资源（原样搬自 scene3d_node.py:477-542，去掉 instance 分支）──

    def _ensure_flat_meshes_gl(
            self, ctx, morph_weights: Optional[Dict[str, float]] = None) -> bool:
        """为导入模型构建每扁平 mesh 的 GL 资源（VBO/IBO/VAO via MeshVBOPool）。

        返回 True 表示资源就绪可渲染；False 表示无模型/失败。
        注：导入模型实例化（can_instance）需要 instance_vbo，本节点暂不构建，
        故 flat mesh 一律单实例渲染（plain 或 skinned 变体）。
        """
        if self._model_root is None:
            return False
        morph_weights = morph_weights or {}
        morph_sig = tuple(sorted((str(key), float(value))
                                 for key, value in morph_weights.items()))
        if self._flat_meshes_gl and self._morph_weights_sig == morph_sig:
            return True  # 已建
        from ..render3d.scene_graph import flatten_scene
        from ..render3d.primitives import MeshData
        from ..render3d.mesh_vbo_pool import MeshVBOPool
        from ..render3d.morph import apply_morph_targets
        if self._flat_meshes_gl:
            self._release_flat_meshes_gl(ctx)
            self._mesh_lod_variants = {}
        self._morph_weights_sig = morph_sig
        flat = flatten_scene(
            self._model_root.root,
            source_path=str(self._model_path_cached or ""),
        )
        _pool = MeshVBOPool.for_context(ctx)
        for fm in flat:
            mesh: MeshData = fm.mesh
            try:
                if morph_weights and mesh.has_morph_targets:
                    mesh = apply_morph_targets(mesh, morph_weights)
                if mesh.is_skinned:
                    prog = self._ensure_skinned_program(ctx)
                    layout = '3f 3f 2f 4f 4f'
                    attrs = ('in_pos', 'in_normal', 'in_uv', 'in_bone_ids', 'in_bone_weights')
                    is_skinned = True
                    has_tangent = False
                else:
                    # F5：材质含 normal map 贴图时按需计算切线（lazy，避免导入阶段
                    # 全量预计算阻塞）。compute_tangents 有缓存，算过一次不会重算；
                    # 向量化实现 65K 面约 20ms，单 mesh 开销可忽略。
                    # 用含 tangent 的 VBO layout，让 Scene3D 能启用 u_has_normal_map。
                    has_tangent = False
                    mat = fm.material
                    if mat is not None and getattr(mat.normal_tex, 'path', ''):
                        if not getattr(mesh, 'has_tangents', False):
                            try:
                                mesh.compute_tangents()
                            except Exception:
                                pass
                        has_tangent = bool(getattr(mesh, 'has_tangents', False))
                    if has_tangent:
                        prog = self._ensure_mesh_tangent_program(ctx)
                        layout = '3f 3f 2f 3f'
                        attrs = ('in_pos', 'in_normal', 'in_uv', 'in_tangent')
                    else:
                        prog = self._ensure_mesh_program(ctx)
                        layout = '3f 3f 2f'
                        attrs = ('in_pos', 'in_normal', 'in_uv')
                    is_skinned = False
                vbo, ibo, _ic = _pool.acquire(ctx, mesh, has_tangent=has_tangent)
                contents = [(vbo, layout, *attrs)]
                vao = ctx.vertex_array(prog, contents, index_buffer=ibo)
                self._flat_meshes_gl.append({
                    'vbo': vbo, 'ibo': ibo, 'vao': vao,
                    'program': prog,  # VAO 绑定的 program（Scene3D 设 uniform 必须用它）
                    'vbo_hash': mesh.content_hash(),
                    'mesh': mesh,
                    'index_count': int(mesh.indices.shape[0]),
                    'is_skinned': is_skinned,
                    'has_tangent': has_tangent,  # F5：标记此 mesh 支持法线贴图
                    'can_instance': False,
                    'world_matrix': fm.world_matrix,
                    'material': fm.material,
                    'skin': fm.skin,
                    'node_name': fm.node_name,
                    'node_id': fm.node_id,
                })
            except Exception:
                import logging
                logging.getLogger(__name__).warning("Model3D 某扁平 mesh 上传失败，跳过", exc_info=True)
                continue
        return bool(self._flat_meshes_gl)

    def _release_flat_meshes_gl(self, ctx=None) -> None:
        """释放导入模型的扁平 mesh GL 资源（VAO 释放 + 池引用计数）。"""
        from ..render3d.mesh_vbo_pool import MeshVBOPool
        pool = MeshVBOPool.for_context(ctx) if ctx is not None else None
        for m in self._flat_meshes_gl:
            vao = m.get('vao')
            if vao is not None:
                try: vao.release()
                except Exception: pass
            if pool is not None and m.get('vbo_hash') is not None:
                try: pool.release(m['vbo_hash'], m['is_skinned'],
                                  m.get('has_tangent', False))
                except Exception: pass
        self._flat_meshes_gl = []

    # ── 动画采样（原样搬自 scene3d_node.py:592-608）──

    def _sample_animation_worlds(self, anim_enabled: bool,
                                 anim_time: float, anim_speed: float,
                                 anim_clip: int = 0) -> dict:
        """采样动画，返回 {node_name: world_matrix}（无动画则返回空 dict）。

        F7：支持播放模式（loop/pingpong/once/clamp）+ 时间偏移 + 起止帧裁剪。
        anim_time 视为合成时间轴帧号，经 remap_frame_to_anim_time 映射到动画秒。
        Model3D-F：anim_clip 选择第 N 个动画片段（多动画文件用）。
        anim_clip_name（F7）非空时按名选片段（优先于索引）。
        超出范围（>= len(clips)）回退到 0（默认行为）。
        """
        if not anim_enabled or self._model_root is None:
            return {}
        clips = self._model_root.animations
        if not clips:
            return {}
        from ..render3d.scene_graph import apply_animation_to_scene
        # F7：clip 选择——anim_clip_name 非空时按名，否则按索引
        clip_name = str(self.parameters.get("anim_clip_name", ""))
        clip = None
        if clip_name:
            for c in clips:
                if c.name == clip_name:
                    clip = c
                    break
        if clip is None:
            # 按索引（钳制到有效范围）
            clip_idx = max(0, min(int(anim_clip), len(clips) - 1))
            clip = clips[clip_idx]

        # F7：时间重映射（comp frame → anim time，含播放模式/偏移/裁剪）
        play_mode = str(self.parameters.get("anim_play_mode", "loop"))
        time_offset = float(self.parameters.get("anim_time_offset", 0.0))
        start_frame = float(self.parameters.get("anim_start_frame", 0.0))
        end_frame_raw = float(self.parameters.get("anim_end_frame", 0.0))
        end_frame = end_frame_raw if end_frame_raw > 0.0 else None
        fps = float(self.parameters.get("anim_fps", 24.0))

        from ..render3d.animation import remap_frame_to_anim_time
        t = remap_frame_to_anim_time(
            anim_time, fps, clip.duration,
            speed=anim_speed, time_offset=time_offset,
            start_frame=start_frame, end_frame=end_frame,
            play_mode=play_mode,
        )
        sampled = self._animation_sampler_cache.sample(clip, t)
        return apply_animation_to_scene(self._model_root.root, sampled)

    # ── Milestone F / P5：Viewport LOD Mesh ──

    def _resolve_lod_level(self, eval_ctx, render_mode: str,
                           flat_meshes: list) -> int:
        """根据交互态 / 渲染模式 / 模型规模决定本帧 LOD 等级（0=LOD0 原样）。

        决策（master_plan §9.4 + §15.1）：
            - viewport_lod_mode=force_lod0/1/2 → 强制该级（P5-6，调试用）
            - render_mode=full/export → 0（合成输出必须高质量）
            - 非 interaction_active → 0（静止用完整 mesh）
            - interaction_active=True：
                - 大模型（>=500k 三角）→ 2（LOD2，navigation）
                - 中模型（>=100k）→ 1（LOD1）
                - 小模型 → 0（不需要 LOD）
        受用户设置 interactive_lod 控制（关闭时强制 0）。

        P5-5 去闪烁：interaction_active 从 True→False 后，保持导航 LOD 一段
        延迟（_lod_hold_ms）才切回 LOD0，避免停止拖动瞬间顶点数突变的闪动。
        """
        import time as _time
        # P5-6：viewport_lod_mode 强制模式优先（调试/特殊场景）。
        # full/export 仍强制 LOD0（合成输出质量不受 force 影响）。
        lod_mode = str(self.parameters.get("viewport_lod_mode", "auto"))
        if lod_mode == "force_lod0":
            return 0
        if str(render_mode) in ("full", "export"):
            return 0
        if lod_mode == "force_lod1":
            return 1
        if lod_mode == "force_lod2":
            return 2
        # auto 模式：按交互态 + 规模决策
        _now = _time.monotonic()
        _is_nav = bool(eval_ctx is not None
                       and getattr(eval_ctx, "interaction_active", False))
        if _is_nav:
            # 导航中：记录时间戳，供停止后的保持期判定
            self._lod_last_nav_time = _now
        # interactive_lod 设置（与 Scene3D 同源 global_3d_scene_settings）
        gs = getattr(eval_ctx, "global_3d_scene_settings", None) or {}
        if not bool(gs.get("interactive_lod", True)):
            self._current_lod_level = 0
            return 0
        if eval_ctx is None or not _is_nav:
            # P5-5：停止导航后的 LOD 保持期。若距上次导航 < _lod_hold_ms，保持
            # 上一帧 LOD level（不立即回 LOD0），让 restore timer 的高质量重渲染
            # 平滑过渡。保持期过后才真正回 LOD0。
            # getattr 容错：__new__ 绕过 __init__ 的测试实例无此字段。
            _cur_lod = getattr(self, "_current_lod_level", 0)
            _last_nav = getattr(self, "_lod_last_nav_time", 0.0)
            _hold_ms = getattr(self, "_lod_hold_ms", 200.0)
            if (_cur_lod > 0
                    and (_now - _last_nav) * 1000.0 < _hold_ms):
                return _cur_lod  # 保持期内：维持导航 LOD
            self._current_lod_level = 0
            return 0
        # interactive_lod 设置（与 Scene3D 同源 global_3d_scene_settings）
        gs = getattr(eval_ctx, "global_3d_scene_settings", None) or {}
        if not bool(gs.get("interactive_lod", True)):
            return 0
        # 统计模型三角形总数
        total_tris = 0
        for fm in flat_meshes:
            if isinstance(fm, dict):
                total_tris += int(fm.get("index_count", 0)) // 3
        from ..ui.viewport3d_state import LARGE_MODEL_TRIANGLE_THRESHOLD
        if total_tris >= LARGE_MODEL_TRIANGLE_THRESHOLD:
            return 2
        if total_tris >= (LARGE_MODEL_TRIANGLE_THRESHOLD // 5):  # 100k
            return 1
        return 0

    def _apply_lod_to_flat_meshes(self, flat_meshes: list,
                                  lod_level: int) -> list:
        """为每个 flat mesh 附上选择的 LOD MeshData（'lod_mesh' 键）。

        LOD 变体按 mesh index 缓存在 _mesh_lod_variants；首次惰性生成。
        蒙皮 mesh / 带贴图材质 mesh / 生成失败 → 不附 lod_mesh（Scene3D 用原 mesh）。
        返回新的 flat_mesh dict 列表（不修改原缓存）。
        """
        if lod_level <= 0 or not flat_meshes:
            return flat_meshes
        from ..render3d.lod_builder import build_lod, lod_ratio_for_level
        result = []
        for i, fm in enumerate(flat_meshes):
            if not isinstance(fm, dict):
                result.append(fm)
                continue
            base_mesh = fm.get("mesh")
            lod_mesh = None
            material = fm.get("material")
            has_textured_material = bool(
                material is not None
                and hasattr(material, "has_any_texture")
                and material.has_any_texture()
            )
            if base_mesh is not None and not fm.get("is_skinned") and not has_textured_material:
                # 查/生成 LOD 变体（按 mesh index 缓存）
                variants = self._mesh_lod_variants.get(i)
                if variants is None:
                    try:
                        # 生成 LOD1/LOD2（LOD0=原 mesh，不存）。
                        # AUD-7009: _resolve_lod_level 只会返回 0/1/2、
                        # viewport_lod_mode 只暴露 force_lod0/1/2——LOD3 在
                        # 本节点不可达，不再同步构建（原实现每 mesh 多做
                        # 一次全网格简化却永远无人消费，导航首帧白白 +50%）。
                        variants = {
                            1: build_lod(base_mesh, target_ratio=lod_ratio_for_level(1)),
                            2: build_lod(base_mesh, target_ratio=lod_ratio_for_level(2)),
                        }
                        self._mesh_lod_variants[i] = variants
                    except Exception:
                        variants = {}
                        self._mesh_lod_variants[i] = variants
                lod_mesh = variants.get(lod_level)
            new_fm = dict(fm)
            if lod_mesh is not None:
                new_fm["lod_mesh"] = lod_mesh
                new_fm["lod_index_count"] = int(lod_mesh.indices.shape[0])
            result.append(new_fm)
        return result

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """加载模型 + 构建扁平 mesh + 采样动画，emit ModelPayload 到旁路。

        完全对应拆分前 scene3d_node.py 导入模型路径：
        - _ensure_model（model_loader.load_model，缓存 SceneGraphRoot）
        - _ensure_flat_meshes_gl（每 mesh VAO via MeshVBOPool）
        - _sample_animation_worlds（按 anim_time 采样骨骼动画）
        → ModelPayload（has_model=False 时 Scene3D 退化为 procedural）

        Milestone F / P5：导航态（interaction_active）+ 大模型时生成低面数 LOD
        代理 mesh，供 Scene3D 导航时用（保流畅）。LOD 变体按模型 path+mtime 缓存，
        首次惰性生成；蒙皮 mesh 不生成 LOD。
        """
        from ..core.eval_context import CompositeEvalContext
        from ..render3d.scene_payload import ModelPayload
        from ..render3d.aov import render_flags_from_role
        from ..render3d.material_model import (
            decode_material_overrides, material_id_for, material_override_to_dict,
            material_summary,
        )

        gl_ctx = ctx
        eval_ctx = ctx if isinstance(ctx, CompositeEvalContext) else None
        if eval_ctx is not None:
            gl_ctx = ctx.gl
        self._last_gl_ctx = gl_ctx

        model_path = str(self.parameters.get("model_path", "") or "")
        use_imported_model = bool(self.parameters.get("use_imported_model", False))
        size_mode = str(self.parameters.get("size_mode", "real_world"))
        source_unit = str(self.parameters.get("source_unit", "m"))
        # AUD-7010: 数值参数容错读取——损坏/手工编辑的工程文件写入非数值时
        # 回落默认值并告警，不再让 float()/int() 异常把节点静默置空。
        unit_scale = max(0.0001, self._safe_param_float("unit_scale", 1.0))
        axis_mode = str(self.parameters.get("axis_mode", "auto"))
        uv_v_mode = str(self.parameters.get("uv_v_mode", "auto"))
        anim_enabled = bool(self.parameters.get("anim_enabled", True))
        # CompositeEvalContext is the single transport clock for preview,
        # playback, and scrubbing. Imported models must sample that clock so
        # a Model3D layer follows the Keyframes playhead without requiring a
        # second Animation-panel timer. Keep the parameter fallback for
        # direct/legacy evaluations that do not provide an eval context.
        if eval_ctx is not None:
            try:
                anim_time = float(eval_ctx.frame_number)
            except (AttributeError, TypeError, ValueError):
                anim_time = self._safe_param_float("anim_time", 0.0)
        else:
            anim_time = self._safe_param_float("anim_time", 0.0)
        anim_speed = self._safe_param_float("anim_speed", 1.0)
        from ..render3d.morph import decode_morph_weights
        from ..render3d.skinning import validate_skinning_contract
        morph_weights, morph_weight_diagnostics = decode_morph_weights(
            self.parameters.get("morph_weights", ""))
        material_override_map, material_override_diagnostics = (
            decode_material_overrides(self.parameters.get("material_overrides", ""))
        )
        material_overrides = {
            key: material_override_to_dict(value)
            for key, value in sorted(material_override_map.items())
        }

        # Model3D 材质升级：材质控制参数
        material_params = {
            "material_mode": str(self.parameters.get("material_mode", "imported")),
            "normal_format": str(self.parameters.get("normal_format", "OpenGL")),
            "orm_layout": str(self.parameters.get("orm_layout", "glTF ORM")),
            "texture_filter": str(self.parameters.get("texture_filter", "linear")),
            "texture_wrap": str(self.parameters.get("texture_wrap", "clamp")),
            "emissive_intensity": self._safe_param_float(
                "emissive_intensity", 1.0),
        }
        hidden_mesh_ids = tuple(sorted({
            value.strip()
            for value in str(self.parameters.get("hidden_mesh_ids", "") or "").split(",")
            if value.strip()
        }))
        locked_mesh_ids = tuple(sorted({
            value.strip()
            for value in str(self.parameters.get("locked_mesh_ids", "") or "").split(",")
            if value.strip()
        }))
        solo_mesh_id = str(self.parameters.get("solo_mesh_id", "") or "").strip()
        render_flags = render_flags_from_role(
            str(self.parameters.get("render_role", "solid")),
            max(0, self._safe_param_int("matte_id", 0)),
            max(0.0, self._safe_param_float("shadow_intensity", 1.0)),
            max(0.0, min(1.0, self._safe_param_float(
                "shadow_softness", 0.2))),
        )
        # Model3D-B：收集外部纹理 override（端口输入的 texture id）。
        # inputs 是 {port_name: TextureResult}；只收有效连接的 texture_id。
        texture_overrides = {}
        for port_name in ("base_color_in", "normal_in", "metallic_in",
                          "roughness_in", "ao_in", "emissive_in", "mask_in"):
            tex = inputs.get(port_name)
            if tex is not None and getattr(tex, "is_valid", False):
                texture_overrides[port_name] = int(tex.texture_id)

        # 无路径/未启用：emit 空 payload（Scene3D 退化为 procedural）
        if not use_imported_model or not model_path:
            self._emit_scene_payload(
                ctx, "display",
                ModelPayload(
                    has_model=False,
                    model_error=self._model_error,
                    model_import_status=(self._model_import_status
                                         if use_imported_model else None),
                    model_import_phase=(self._model_import_phase
                                        if use_imported_model else ""),
                    model_import_progress=(self._model_import_progress
                                           if use_imported_model else 0.0),
                    model_import_diagnostics=(self._model_import_diagnostics
                                              if use_imported_model else ()),
                    model_import_resource_estimate=(
                        self._model_import_resource_estimate
                        if use_imported_model else None),
                    model_import_probe=(self._model_import_probe
                                        if use_imported_model else None),
                    hidden_mesh_ids=hidden_mesh_ids,
                    user_hidden_mesh_ids=hidden_mesh_ids,
                    locked_mesh_ids=locked_mesh_ids,
                    solo_mesh_id=solo_mesh_id,
                    render_flags=render_flags,
                    material_overrides=material_overrides or None,
                    morph_weights=morph_weights or None,
                    diagnostics=tuple(
                        (["model.load_error"] if self._model_error else [])
                        + list(material_override_diagnostics)
                        + list(morph_weight_diagnostics)
                    ),
                ),
            )
            return {}

        # 加载 + 构建 GL 资源
        normalize_on_load = (size_mode != "real_world")
        flip_uv_v = self._should_flip_uv_v(uv_v_mode, model_path)
        root = self._ensure_model(
            model_path, normalize=normalize_on_load, flip_uv_v=flip_uv_v)
        has_model = False
        flat_meshes = []
        if root is not None:
            has_model = self._ensure_flat_meshes_gl(gl_ctx, morph_weights)
            flat_meshes = list(self._flat_meshes_gl) if has_model else []
            if has_model:
                # CPU root 已安全交接到 render thread，且本帧 GL 资源已经
                # 创建完成；这才向 UI 报告 ready，避免“解析完成但首帧仍空白”。
                self._model_import_status = "ready"
                self._model_import_phase = "ready"
                self._model_import_progress = 1.0
                self._model_import_diagnostics = ()

        # Milestone F / P5：解析本帧 LOD 等级（导航态 + 大模型 → LOD2 低面数代理）。
        # 生成/复用 LOD 变体，把选择的 LOD MeshData 附进 flat_mesh dict（'lod_mesh' 键），
        # 供 Scene3D 导航时用。静止/小模型 → LOD0（不附 lod_mesh，Scene3D 用原 mesh）。
        self._current_lod_level = self._resolve_lod_level(eval_ctx, render_mode, flat_meshes)
        if has_model and self._current_lod_level > 0:
            flat_meshes = self._apply_lod_to_flat_meshes(flat_meshes, self._current_lod_level)

        # P1-24：GPU beauty/depth/AOV/ID/motion pass 共用同一套 skin contract。
        # 输入异常不阻断可渲染模型，但必须进入 payload diagnostics，避免
        # invalid influence 被 shader 静默忽略后难以追查。
        skinning_diagnostics = []
        for mesh_desc in flat_meshes:
            if not isinstance(mesh_desc, dict):
                continue
            mesh = mesh_desc.get("mesh")
            if mesh is not None and getattr(mesh, "is_skinned", False):
                skinning_diagnostics.extend(validate_skinning_contract(
                    mesh, mesh_desc.get("skin")))

        # P0 缺口：应用 Model3D 自身物体变换（object_pos/rot/scale）到 flat_meshes。
        # 不改 _flat_meshes_gl（缓存），构造新 dict 列表（world_matrix 预乘变换矩阵）。
        # 读取参数放在 if 外（transform_sig 也要用，且 has_model=False 时默认值）。
        opx = self._safe_param_float("object_pos_x", 0.0)
        opy = self._safe_param_float("object_pos_y", 0.0)
        opz = self._safe_param_float("object_pos_z", 0.0)
        orx = self._safe_param_float("object_rot_x", 0.0)
        ory = self._safe_param_float("object_rot_y", 0.0)
        orz = self._safe_param_float("object_rot_z", 0.0)
        mesh_scale = max(0.001, self._safe_param_float("mesh_scale", 1.0))
        real_world_mode = (size_mode == "real_world")
        unit_factor = self._source_unit_to_meters(source_unit) if real_world_mode else 1.0
        mscale = max(0.000001, mesh_scale * unit_factor * unit_scale)
        if has_model and flat_meshes:
            from ..render3d import math_utils as m3d
            axis_xform = self._axis_correction_matrix(
                axis_mode, getattr(root, "bbox_min", None), getattr(root, "bbox_max", None))
            obj_xform = m3d.compose(
                m3d.translate(opx, opy, opz),
                m3d.rotate_xyz(orx, ory, orz) @ axis_xform,
                m3d.scale(mscale, mscale, mscale),
            )
            flat_meshes = [
                {**fm, "world_matrix": (obj_xform @ fm["world_matrix"]).astype(np.float32)}
                for fm in flat_meshes
            ]

        # P1-23：为每个导入材质分配稳定 slot id，并构造 UI 可消费的摘要。
        # material_id 优先来自 loader；旧模型则退化到 mesh node id，保持向后兼容。
        material_counts: Dict[str, int] = {}
        for index, mesh_desc in enumerate(flat_meshes):
            mat = mesh_desc.get("material") if isinstance(mesh_desc, dict) else None
            if mat is None:
                continue
            material_key = material_id_for(
                mat,
                str(mesh_desc.get("node_id", "") or mesh_desc.get("node_name", "")
                    or f"material_{index}"),
            )
            mesh_desc["material_id"] = material_key
            material_counts[material_key] = material_counts.get(material_key, 0) + 1
        model_dir = os.path.dirname(model_path)
        materials = []
        seen_materials = set()
        for index, mesh_desc in enumerate(flat_meshes):
            mat = mesh_desc.get("material") if isinstance(mesh_desc, dict) else None
            if mat is None:
                continue
            material_key = str(mesh_desc.get("material_id") or material_id_for(
                mat, f"material_{index}"))
            if material_key in seen_materials:
                continue
            seen_materials.add(material_key)
            materials.append(material_summary(
                mat, material_key, model_dir,
                mesh_count=material_counts.get(material_key, 1),
            ))

        # 采样动画（返回 world matrix 字典，Scene3D 上传到 u_bone_matrices）
        # Model3D-F：传 anim_clip 选择片段
        anim_clip = max(0, self._safe_param_int("anim_clip", 0))
        anim_worlds = self._sample_animation_worlds(anim_enabled, anim_time, anim_speed, anim_clip)

        root_report = getattr(root, "import_report", None)
        import_report = (root_report.summary_dict()
                         if root_report is not None
                         and hasattr(root_report, "summary_dict") else None)
        animation_clips = tuple(
            (str(clip.name), float(clip.duration))
            for clip in getattr(root, "animations", ())
        )

        effective_hidden_mesh_ids = self._effective_hidden_mesh_ids(
            hidden_mesh_ids, solo_mesh_id, self._model_hierarchy, flat_meshes)

        payload = ModelPayload(
            flat_meshes=flat_meshes,
            anim_worlds=anim_worlds,
            has_model=has_model,
            model_error=self._model_error,
            # 资源标识（payload signature 用）：path+mtime 捕获资源变化，
            # anim_time 捕获动画帧。GL 对象不在签名（path/mtime 等价表达）。
            model_path=model_path,
            model_mtime=self._model_mtime_cached,
            anim_time=anim_time if anim_enabled else 0.0,
            # P0 缺口：物体变换签名（变换变化触发 Scene3D 重渲染）
            transform_sig=(opx, opy, opz, orx, ory, orz, mscale,
                           size_mode, source_unit, unit_scale, axis_mode,
                           uv_v_mode, flip_uv_v,
                           # 纹理 override 也进签名（连/断纹理触发重渲染）
                           tuple(sorted(texture_overrides.items())),
                           tuple(sorted(morph_weights.items()))) if has_model else (),
            # Model3D 材质升级：外部纹理 override + 材质控制参数
            texture_overrides=texture_overrides or None,
            material_params=material_params,
            material_overrides=material_overrides or None,
            morph_weights=morph_weights or None,
            materials=tuple(materials),
            # Milestone E / P4-6：导入缓存状态（Scene3D diagnostics / UI 显示）
            model_cache_status=self._model_cache_status,
            model_import_status=self._model_import_status,
            model_import_phase=self._model_import_phase,
            model_import_progress=self._model_import_progress,
            model_import_diagnostics=self._model_import_diagnostics,
            model_import_resource_estimate=self._model_import_resource_estimate,
            model_import_probe=self._model_import_probe,
            # F6：骨架列表（skeleton overlay 渲染用；None=无骨架）
            skeletons=(getattr(self._model_root, 'skeletons', None)
                       if has_model else None),
            hierarchy=(self._model_hierarchy if has_model else None),
            animation_clips=animation_clips,
            import_report=import_report,
            hidden_mesh_ids=effective_hidden_mesh_ids,
            user_hidden_mesh_ids=hidden_mesh_ids,
            locked_mesh_ids=locked_mesh_ids,
            solo_mesh_id=solo_mesh_id,
            render_flags=render_flags,
            diagnostics=tuple(
                (["model.load_error"] if self._model_error else [])
                + (["model.import_warning"]
                   if getattr(getattr(root, "import_report", None),
                              "has_warnings", lambda: False)()
                   else [])
                + list(material_override_diagnostics)
                + list(morph_weight_diagnostics)
                + list(skinning_diagnostics)
            ),
        )
        self._emit_scene_payload(ctx, "display", payload)
        return {}

    @staticmethod
    def _effective_hidden_mesh_ids(
            hidden_mesh_ids: tuple[str, ...], solo_mesh_id: str,
            hierarchy: object, flat_meshes: list[dict]) -> tuple[str, ...]:
        """Resolve the temporary Outliner solo target into render visibility."""
        base_hidden = {str(value) for value in hidden_mesh_ids if str(value)}
        solo_id = str(solo_mesh_id or "").strip()
        if not solo_id:
            return tuple(sorted(base_hidden))

        mesh_ids = {
            str(mesh.get("node_id", "") or "")
            for mesh in flat_meshes
            if isinstance(mesh, dict) and str(mesh.get("node_id", "") or "")
        }
        finder = getattr(hierarchy, "find_by_id", None)
        if solo_id not in mesh_ids and callable(finder):
            solo_item = finder(solo_id)
            collector = getattr(solo_item, "collect_mesh_ids", None)
            if callable(collector):
                mesh_ids.update(str(value) for value in collector() if str(value))
        if not mesh_ids:
            return tuple(sorted(base_hidden))

        visible_ids = {solo_id} if solo_id in mesh_ids else set()
        if callable(finder):
            solo_item = finder(solo_id)
            collector = getattr(solo_item, "collect_mesh_ids", None)
            if callable(collector):
                visible_ids = {
                    str(value) for value in collector() if str(value)
                }
        if not visible_ids:
            return tuple(sorted(base_hidden))
        base_hidden.update(mesh_ids - visible_ids)
        return tuple(sorted(base_hidden))

    def cleanup(self) -> None:
        """释放模型 GL 资源 + 程序（SceneGraphRoot 由 GC 回收）。"""
        pending = self._model_import_handle
        if pending is not None:
            pending.cancel()
        self._model_import_handle = None
        self._model_import_request = None
        self._model_import_ready_callback = None
        self._release_flat_meshes_gl(self._last_gl_ctx)
        self._last_gl_ctx = None
        self._model_root = None
        self._model_hierarchy = None
        self._model_path_cached = None
        self._morph_weights_sig = ()
        self._animation_sampler_cache.clear()
        self._model_flip_uv_v_cached = False
        self._model_cache_status = None
        self._model_import_status = None
        self._model_import_phase = ""
        self._model_import_progress = 0.0
        self._model_import_diagnostics = ()
        self._model_import_resource_estimate = None
        self._model_import_probe = None
        for prog in (self._mesh_prog, self._mesh_skinned_prog, self._mesh_tangent_prog):
            if prog is not None:
                try: prog.release()
                except Exception: pass
        self._mesh_prog = None
        self._mesh_skinned_prog = None
        self._mesh_tangent_prog = None
