"""
Geo3D Node — 3D 程序化几何数据源节点（阶段七）。

把原本揉在 Scene3D 里的程序化几何（cube/plane/grid/sphere）+ 实例化 grid 复制器
+ 物体变换 + 默认 PBR 材质独立成节点，输出 GeometryPayload 到 Scene3D 聚合器
的 geometry_in。

P1 基本体扩展：新增 cylinder / cone / torus / null，补齐常见 DCC 基本体。
null 为轴参考对象（LINES 渲染，与 grid 同）。
"""

from typing import Any, Dict, Optional

from .base_node import Node, Port, TextureResult, ParamDef


class Geo3DNode(Node):
    """3D 程序化几何源节点：cube/plane/grid/sphere/cylinder/cone/torus/null + 实例化。"""

    node_type = "geo3d"
    # AE layer parenting can be applied to the object transform channels in
    # NodeGraph._resolve_parameters().
    supports_parent_transform = True
    node_title = "Geo3D"
    i18n_title_key = "nodes.geo3d.title"
    header_color = "#2d6a4f"  # 深绿，几何类（与 Scene3D 同色系）

    SEARCH_ALIASES = ["geo3d", "geometry", "primitive", "cube", "sphere", "plane",
                      "grid", "cylinder", "cone", "torus", "null",
                      "几何", "立方体", "球体", "网格", "圆柱", "圆锥", "圆环"]
    DESCRIPTION = "3D 程序化几何节点（cube/plane/grid/sphere/cylinder/cone/torus/null）+ 实例化，输出给 Scene3D"
    TAGS = ["3d", "source", "geometry"]

    # 用 LINES 渲染的基本体（grid 网格线 + null 轴指示器）。
    _LINE_PRIMITIVES = frozenset(["grid", "null"])

    PARAM_DEFS = [
        # ── 几何 ──（P1 扩展：补 cylinder/cone/torus/null）
        ParamDef("primitive", "option", "cube",
                 labels=["cube", "plane", "grid", "sphere",
                         "cylinder", "cone", "torus", "null"], group="几何 / Geometry"),
        ParamDef("mesh_scale", "float", 1.0, min_val=0.01, max_val=100.0, step=0.1,
                 group="几何 / Geometry"),
        # ── 实例化（阶段六：grid 复制器，对齐 Element 3D）──
        ParamDef("instance_enabled", "bool", False, group="实例化 / Instancing"),
        ParamDef("grid_x", "int", 3, min_val=1, max_val=100, step=1, group="实例化 / Instancing"),
        ParamDef("grid_z", "int", 3, min_val=1, max_val=100, step=1, group="实例化 / Instancing"),
        ParamDef("spacing", "float", 2.0, min_val=0.0, max_val=100.0, step=0.1,
                 group="实例化 / Instancing"),
        # ── 物体变换（拆成标量以支持动画）──
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
        # ── 默认 PBR 材质（导入模型 mesh 会覆盖）──
        ParamDef("metallic", "float", 0.0, min_val=0.0, max_val=1.0, step=0.01,
                 group="材质 / Material"),
        ParamDef("roughness", "float", 0.5, min_val=0.01, max_val=1.0, step=0.01,
                 group="材质 / Material"),
        ParamDef("object_color_r", "float", 0.8, min_val=0.0, max_val=1.0, step=0.01,
                 group="材质 / Material"),
        ParamDef("object_color_g", "float", 0.8, min_val=0.0, max_val=1.0, step=0.01,
                 group="材质 / Material"),
        ParamDef("object_color_b", "float", 0.8, min_val=0.0, max_val=1.0, step=0.01,
                 group="材质 / Material"),
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
        self.outputs = [
            Port("display", "output", "scene"),
        ]
        self.parameters = {
            "primitive": "cube",
            "mesh_scale": 1.0,
            "instance_enabled": False,
            "grid_x": 3,
            "grid_z": 3,
            "spacing": 2.0,
            "object_pos_x": 0.0, "object_pos_y": 0.0, "object_pos_z": 0.0,
            "object_rot_x": 0.0, "object_rot_y": 0.0, "object_rot_z": 0.0,
            "metallic": 0.0,
            "roughness": 0.5,
            "object_color_r": 0.8, "object_color_g": 0.8, "object_color_b": 0.8,
            "render_role": "solid", "matte_id": 0,
            "shadow_intensity": 1.0, "shadow_softness": 0.2,
        }
        # GL 资源（懒建并缓存，cleanup 释放）
        self._mesh_prog = None
        self._mesh_instanced_prog = None
        self._mesh_vbo = None
        self._mesh_ibo = None
        self._mesh_vao = None
        self._mesh_index_count = 0
        self._mesh_is_lines = False
        self._last_primitive = None
        self._mesh_prog_used = None  # VAO 绑定的 program（payload 携带）
        self._mesh_data = None  # CPU MeshData，供 Scene3D AOV pass 绑定独立 shader
        self._instance_vbo = None
        self._instance_count_cached = 0
        self._instance_sig_cached = None
        self._instance_offsets_cached = None
        self._mesh_instance_enabled = False  # procedural VAO 是否含 instance buffer
        self._geo_revision = 0  # 几何内容修订号（payload signature 用）
        # AUD-6804: 上次求值的内容签名——_geo_revision 只在签名变化时递增。
        self._geo_content_sig: Optional[tuple] = None

    # ── 程序懒建（与 scene3d_node 同款）──

    def _ensure_mesh_program(self, ctx):
        if self._mesh_prog is None:
            from ..core.shaders import _MESH_VERTEX, _MESH_PBR_FRAGMENT
            self._mesh_prog = ctx.program(
                vertex_shader=_MESH_VERTEX, fragment_shader=_MESH_PBR_FRAGMENT)
        return self._mesh_prog

    def _ensure_instanced_program(self, ctx):
        if self._mesh_instanced_prog is None:
            from ..core.shaders import _MESH_VERTEX_INSTANCED, _MESH_PBR_FRAGMENT
            self._mesh_instanced_prog = ctx.program(
                vertex_shader=_MESH_VERTEX_INSTANCED, fragment_shader=_MESH_PBR_FRAGMENT)
        return self._mesh_instanced_prog

    # ── 几何 / 实例 buffer 懒建（原样搬自 scene3d_node.py:332-419）──

    def _ensure_instance_buffer(self, ctx, grid_x: int, grid_z: int, spacing: float) -> int:
        """按 (grid_x, grid_z, spacing) 构建/缓存实例偏移 buffer，返回实例数。

        offset 网格居中：(i - (gx-1)/2) * spacing, 0, (j - (gz-1)/2) * spacing。
        实例数 = grid_x * grid_z。
        """
        import numpy as np
        sig = (int(grid_x), int(grid_z), float(spacing))
        if self._instance_sig_cached == sig and self._instance_vbo is not None:
            return self._instance_count_cached
        if self._instance_vbo is not None:
            try: self._instance_vbo.release()
            except Exception: pass
            self._instance_vbo = None
            # AUD-6803 修复：_mesh_vao 仍绑定刚 release 的旧 instance VBO。
            # VBO 重建后必须让 VAO 缓存失效（VAO/VBO 生命周期绑定），
            # 否则 _ensure_mesh_geometry 复用早退（复用条件不含实例签名），
            # Scene3D 按新实例数渲染旧 VAO，对已删除 buffer 越界取实例
            # 属性（GL 未定义行为：位置垃圾/闪烁/部分驱动崩溃）。
            if self._mesh_vao is not None:
                try: self._mesh_vao.release()
                except Exception: pass
                self._mesh_vao = None
                self._last_primitive = None  # 强制 _ensure_mesh_geometry 重建
        self._instance_offsets_cached = None
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
        self._instance_offsets_cached = offsets_np.copy()
        return self._instance_count_cached

    def _ensure_mesh_geometry(self, ctx, primitive: str, instance_enabled: bool) -> None:
        """按 primitive 上传/重建几何（VBO/IBO/VAO）。

        primitive 或 instance_enabled 变了才重建；否则复用缓存。
        instance_enabled=True 时 VAO 带 instance buffer + instanced program。
        记录所用 program 到 _mesh_prog_used，供 payload 携带给 Scene3D 设 uniform。
        P1.4 修复：复用条件必须含 instance_enabled，否则切换实例化会复用
        错误属性的 VAO（绑到错误的 shader/attribute）。
        """
        from ..render3d.primitives import get_primitive
        if (primitive == self._last_primitive
                and self._mesh_vbo is not None
                and self._mesh_instance_enabled == instance_enabled):
            return  # 复用缓存
        self._release_mesh_geometry()
        mesh = get_primitive(primitive)
        self._mesh_data = mesh
        self._mesh_vbo = ctx.buffer(mesh.interleaved().tobytes())
        self._mesh_ibo = ctx.buffer(mesh.indices.tobytes())
        self._mesh_index_count = int(mesh.indices.shape[0])
        # grid / null 用 LINES 渲染（线条类），其余用 TRIANGLES。
        self._mesh_is_lines = (primitive in self._LINE_PRIMITIVES)
        self._mesh_instance_enabled = instance_enabled
        if instance_enabled and self._instance_vbo is not None:
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
        self._mesh_prog_used = prog  # 记录 VAO 绑定的 program
        self._last_primitive = primitive

    def _release_mesh_geometry(self) -> None:
        """释放几何资源（保留 program）。"""
        if self._mesh_vao is not None:
            try: self._mesh_vao.release()
            except Exception: pass
            self._mesh_vao = None
        if self._mesh_ibo is not None:
            try: self._mesh_ibo.release()
            except Exception: pass
            self._mesh_ibo = None
        if self._mesh_vbo is not None:
            try: self._mesh_vbo.release()
            except Exception: pass
            self._mesh_vbo = None
        self._mesh_index_count = 0
        self._mesh_is_lines = False
        self._last_primitive = None
        self._mesh_instance_enabled = False
        self._mesh_data = None

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """构建 procedural 几何 + 实例 buffer + 物体变换，emit GeometryPayload 到旁路。

        完全对应拆分前 scene3d_node.py procedural 路径：
        - _ensure_instance_buffer（grid 偏移 VBO）
        - _ensure_mesh_geometry（procedural VAO，按 instance_enabled 选 program）
        - object_pos/rot + mesh_scale 合成 model_matrix（= scene3d_node model 矩阵）
        """
        from ..core.eval_context import CompositeEvalContext
        from ..render3d import math_utils as m3d
        from ..render3d.scene_payload import GeometryPayload
        from ..render3d.aov import render_flags_from_role

        gl_ctx = ctx
        if isinstance(ctx, CompositeEvalContext):
            gl_ctx = ctx.gl

        # 读参数
        primitive = str(self.parameters.get("primitive", "cube"))
        mesh_scale = float(self.parameters.get("mesh_scale", 1.0))
        instance_enabled = bool(self.parameters.get("instance_enabled", False))
        grid_x = max(1, int(self.parameters.get("grid_x", 3)))
        grid_z = max(1, int(self.parameters.get("grid_z", 3)))
        spacing = max(0.0, float(self.parameters.get("spacing", 2.0)))
        opx = float(self.parameters.get("object_pos_x", 0.0))
        opy = float(self.parameters.get("object_pos_y", 0.0))
        opz = float(self.parameters.get("object_pos_z", 0.0))
        orx = float(self.parameters.get("object_rot_x", 0.0))
        ory = float(self.parameters.get("object_rot_y", 0.0))
        orz = float(self.parameters.get("object_rot_z", 0.0))
        metallic = max(0.0, min(1.0, float(self.parameters.get("metallic", 0.0))))
        roughness = max(0.01, min(1.0, float(self.parameters.get("roughness", 0.5))))
        ocr = float(self.parameters.get("object_color_r", 0.8))
        ocg = float(self.parameters.get("object_color_g", 0.8))
        ocb = float(self.parameters.get("object_color_b", 0.8))
        render_role = str(self.parameters.get("render_role", "solid"))
        matte_id = max(0, int(self.parameters.get("matte_id", 0)))
        shadow_intensity = max(0.0, float(
            self.parameters.get("shadow_intensity", 1.0)))
        shadow_softness = max(0.0, min(1.0, float(
            self.parameters.get("shadow_softness", 0.2))))

        # 构建 GL 资源（与 scene3d_node procedural 路径一致）
        instance_count = 1
        if instance_enabled:
            instance_count = self._ensure_instance_buffer(gl_ctx, grid_x, grid_z, spacing)
        # instance_enabled 变化需强制重建 VAO（VAO content 不同）。
        # _ensure_mesh_geometry 的复用条件已含 instance_enabled 比较，切换时自动重建。
        self._ensure_mesh_geometry(gl_ctx, primitive, instance_enabled)

        # 物体变换：T · R · S（与 scene3d_node.py:958-962 model 矩阵一致）
        model = m3d.compose(
            m3d.translate(opx, opy, opz),
            m3d.rotate_xyz(orx, ory, orz),
            m3d.scale(mesh_scale, mesh_scale, mesh_scale),
        )

        # content_revision：捕获所有影响渲染的参数变化（primitive/instance/transform/材质）。
        # Scene3D 把它进 content_sig，参数变了不复用陈旧纹理。
        # AUD-6804：修订号只在内容签名实际变化时递增。原实现每次求值无条件
        # +1，使 Scene3D 的 payload signature（含 content_revision）逐帧变化，
        # 即使场景完全静止也恒 miss 内容缓存、每帧全量重渲染。
        content_sig = (
            primitive, mesh_scale, instance_enabled, grid_x, grid_z, spacing,
            opx, opy, opz, orx, ory, orz,
            metallic, roughness, ocr, ocg, ocb,
            render_role, matte_id, shadow_intensity, shadow_softness,
        )
        if content_sig != self._geo_content_sig:
            self._geo_content_sig = content_sig
            self._geo_revision += 1

        payload = GeometryPayload(
            vao=self._mesh_vao,
            program=self._mesh_prog_used,
            index_count=self._mesh_index_count,
            is_lines=self._mesh_is_lines,
            instance_vbo=self._instance_vbo if instance_enabled else None,
            instance_count=instance_count if instance_enabled else 1,
            instance_enabled=instance_enabled,
            instance_offsets=(self._instance_offsets_cached.copy()
                              if instance_enabled and self._instance_offsets_cached is not None
                              else None),
            model_matrix=model,
            mesh=self._mesh_data,
            material={"albedo": (ocr, ocg, ocb), "metallic": metallic, "roughness": roughness},
            variant="instanced" if instance_enabled else "plain",
            content_revision=self._geo_revision,
            render_flags=render_flags_from_role(
                render_role, matte_id, shadow_intensity, shadow_softness),
        )
        self._emit_scene_payload(ctx, "display", payload)
        return {}

    def cleanup(self) -> None:
        """释放 procedural 几何 + 实例 buffer + 程序。"""
        self._release_mesh_geometry()
        # AUD-6804：清空内容签名——cleanup 后若再次求值（重建 VAO），
        # 修订号应递增一次以反映"新的几何实例"。
        self._geo_content_sig = None
        if self._instance_vbo is not None:
            try: self._instance_vbo.release()
            except Exception: pass
            self._instance_vbo = None
        for prog in (self._mesh_prog, self._mesh_instanced_prog):
            if prog is not None:
                try: prog.release()
                except Exception: pass
        self._mesh_prog = None
        self._mesh_instanced_prog = None
