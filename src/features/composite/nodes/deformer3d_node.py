"""Deformer3D Node — F13 非破坏变形器节点（scene→scene 中间节点）。

master_plan §16.9：支持显式节点链路 ``Model3D -> Deformer3D -> Scene3D``。
本节点接收上游 ModelPayload（含导入模型的 flat_meshes），对其中的 MeshData
应用非破坏变形（Bend/Twist/Bulge/Taper/Stretch/Noise），重建 VAO/VBO 后
emit 新的 ModelPayload 给下游（Scene3D 或下一个 Deformer3D）。

数据流：
    1. _collect_connected_scene_payloads(ctx, ModelPayload) 拿上游 payload
    2. 对每个 flat_mesh: 读 mesh(MeshData) → DeformerStack.evaluate(mesh)
    3. 构造新 MeshData（变形后 vertices/normals）→ acquire 新 VBO/VAO
    4. emit 新 ModelPayload（transform_sig 塞变形参数 → Scene3D 参数变时重渲染）

设计原则（master_plan §16.2）：
- 非破坏：上游 Model3D 的 mesh/VAO 不被修改，变形结果来自运行时 evaluate。
- 可叠加：多个 Deformer3D 串联，按节点顺序依次变形。
- 可缓存：变形参数签名不变时复用缓存的变形 VAO（避免每帧重算）。
- 蒙皮 mesh 在变形前先按当前动画姿态烘出 skinning 结果，再走同一套
  DeformerStack；输出转为普通 mesh，避免 Scene3D 再次蒙皮。

参考：render3d/deformers/（变形算法）、geo3d_node.py（源节点骨架）、
model3d_node.py（flat_meshes GL 资源管理）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import numpy as np

from .base_node import Node, Port, TextureResult, ParamDef

logger = logging.getLogger(__name__)


# 变形器类型 → 中文标签（UI 下拉用）
_DEFORMER_TYPE_LABELS = [
    "bend", "twist", "bulge", "taper", "stretch", "noise",
]


class Deformer3DNode(Node):
    """通用 3D 变形器节点（scene→scene）。

    通过 ``deformer_type`` 参数选择变形类型（Bend/Twist/Bulge/Taper/Stretch/Noise）。
    串联多个 Deformer3D 可叠加变形（Bend → Twist → ...），顺序由节点图连接决定。
    """

    node_type = "deformer3d"
    # CP-044: 3D 场景载荷族，不参与通用 mask。
    GENERIC_MASK_MIX = False

    node_title = "Deformer3D"
    i18n_title_key = "nodes.deformer3d.title"
    header_color = "#6b4226"  # 棕色，变形器类（区别于几何绿/场景深绿）

    SEARCH_ALIASES = ["deformer3d", "deformer", "bend", "twist", "bulge",
                      "taper", "stretch", "noise", "变形", "弯曲", "扭转",
                      "膨胀", "锥化", "拉伸", "噪波"]
    DESCRIPTION = "3D 非破坏变形器节点（Bend/Twist/Bulge/Taper/Stretch/Noise），串联在 Model3D 与 Scene3D 之间"
    TAGS = ["3d", "deformer", "scene"]

    PARAM_DEFS = [
        # ── 变形器类型 ──
        ParamDef("deformer_type", "option", "bend",
                 labels=_DEFORMER_TYPE_LABELS, group="变形 / Deformer"),
        ParamDef("enabled", "bool", True, group="变形 / Deformer"),
        ParamDef("strength", "float", 1.0, min_val=0.0, max_val=5.0, step=0.05,
                 group="变形 / Deformer"),
        ParamDef("axis", "option", "y", labels=["x", "y", "z"],
                 group="变形 / Deformer"),
        # ── 变形参数（因类型而异）──
        ParamDef("angle", "float", 45.0, min_val=-360.0, max_val=360.0, step=1.0,
                 group="变形 / Deformer"),
        ParamDef("amount", "float", 1.5, min_val=-5.0, max_val=5.0, step=0.05,
                 group="变形 / Deformer"),
        ParamDef("mode", "option", "normal",
                 labels=["normal", "radial"], group="变形 / Deformer"),
        # ── 变形中心 ──
        ParamDef("center_x", "float", 0.0, min_val=-100.0, max_val=100.0, step=0.1,
                 group="中心 / Center"),
        ParamDef("center_y", "float", 0.0, min_val=-100.0, max_val=100.0, step=0.1,
                 group="中心 / Center"),
        ParamDef("center_z", "float", 0.0, min_val=-100.0, max_val=100.0, step=0.1,
                 group="中心 / Center"),
        # ── Noise 参数（noise 类型专用）──
        ParamDef("frequency", "float", 2.0, min_val=0.01, max_val=20.0, step=0.1,
                 group="噪波 / Noise"),
        ParamDef("amplitude", "float", 0.1, min_val=0.0, max_val=5.0, step=0.01,
                 group="噪波 / Noise"),
        ParamDef("seed", "float", 0.0, min_val=0.0, max_val=100.0, step=1.0,
                 group="噪波 / Noise"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        # 端口：输入 scene（接收 Model3D/上游 Deformer3D）+ 输出 scene（给 Scene3D/下游）。
        # 输入端口名必须叫 "in"：base_evaluator 的 _scene_ports=("in",) 据此登记连接。
        self.inputs = [
            Port("in", "input", "scene"),
        ]
        self.outputs = [
            Port("display", "output", "scene"),
        ]
        self.parameters = {
            "deformer_type": "bend",
            "enabled": True,
            "strength": 1.0,
            "axis": "y",
            "angle": 45.0,
            "amount": 1.5,
            "mode": "normal",
            "center_x": 0.0, "center_y": 0.0, "center_z": 0.0,
            "frequency": 2.0,
            "amplitude": 0.1,
            "seed": 0.0,
        }
        # GL 资源：变形后的 flat_meshes（每帧可能重建，cleanup 释放）。
        self._deformed_meshes: list = []
        # 缓存签名：避免每帧重算变形（参数没变就复用）。
        self._deform_sig: tuple = ()
        # program 懒建（与 Model3D 同款：变形后 mesh 用这些 program 建 VAO）。
        self._mesh_prog = None
        self._mesh_tangent_prog = None
        self._mesh_skinned_prog = None
        # 缓存最近一次 evaluate 的 GL context，供 cleanup() 释放 VBO 池引用用
        #（cleanup() 无 ctx 参数，必须靠 evaluate 时缓存）。
        self._last_gl_ctx = None
        # P0-10: stable diagnostics for capabilities that are intentionally
        # degraded instead of silently disappearing in a scene chain.
        self._last_diagnostics: list[str] = []

    # ── 程序懒建（与 model3d_node 同款）──

    def _ensure_mesh_program(self, ctx):
        if self._mesh_prog is None:
            from ..core.shaders import _MESH_VERTEX, _MESH_PBR_FRAGMENT
            self._mesh_prog = ctx.program(
                vertex_shader=_MESH_VERTEX, fragment_shader=_MESH_PBR_FRAGMENT)
        return self._mesh_prog

    def _ensure_mesh_tangent_program(self, ctx):
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

    # ── GL 资源释放 ──

    def _release_mesh_programs(self, release: bool = True) -> None:
        """Drop the lazily-built mesh programs; release when still live.

        ``release=False`` is for GL context recreation (AUD-6607): the
        programs belong to the dead context — the same drop-without-release
        discipline as particle_node._release_gpu_objects.  The per-context
        VBO pool dies with its context, so its refcounts need no decrement
        either.
        """
        for prog in (self._mesh_prog, self._mesh_tangent_prog,
                     self._mesh_skinned_prog):
            if prog is not None and release:
                try:
                    prog.release()
                except Exception:
                    pass
        self._mesh_prog = None
        self._mesh_tangent_prog = None
        self._mesh_skinned_prog = None

    def _release_deformed_meshes(self, ctx=None) -> None:
        """释放变形后的 flat_meshes GL 资源（VAO + VBO 池引用计数）。"""
        from ..render3d.mesh_vbo_pool import MeshVBOPool
        pool = MeshVBOPool.for_context(ctx) if ctx is not None else None
        for m in self._deformed_meshes:
            vao = m.get('vao')
            if vao is not None:
                try:
                    vao.release()
                except Exception:
                    pass
            if pool is not None and m.get('vbo_hash') is not None:
                try:
                    pool.release(m['vbo_hash'], m.get('is_skinned', False),
                                 m.get('has_tangent', False))
                except Exception:
                    pass
        self._deformed_meshes = []
        self._deform_sig = ()

    def cleanup(self) -> None:
        """释放变形 GL 资源 + 程序。

        用 _last_gl_ctx（evaluate 时缓存）释放 VBO 池引用计数。
        cleanup() 无 ctx 参数（调用方 _sync_render_nodes 不传 ctx），
        所以必须靠 evaluate 时缓存 gl_ctx。
        """
        self._release_deformed_meshes(self._last_gl_ctx)
        self._last_gl_ctx = None
        self._release_mesh_programs(release=True)

    # ── 变形参数 → DeformerSpec ──

    def _build_deformer_spec(self):
        """从节点参数构造 DeformerSpec（render3d.deformers.base）。"""
        from ..render3d.deformers.base import DeformerSpec
        dtype = str(self.parameters.get("deformer_type", "bend"))
        params: dict = {}
        # 因类型而异的参数映射到 params dict
        if dtype in ("bend", "twist"):
            params["angle"] = float(self.parameters.get("angle", 45.0))
        elif dtype in ("taper", "stretch"):
            params["amount"] = float(self.parameters.get("amount", 1.5))
        elif dtype == "bulge":
            params["mode"] = str(self.parameters.get("mode", "normal"))
        elif dtype == "noise":
            params["frequency"] = float(self.parameters.get("frequency", 2.0))
            params["amplitude"] = float(self.parameters.get("amplitude", 0.1))
            params["seed"] = float(self.parameters.get("seed", 0.0))
        return DeformerSpec(
            deformer_type=dtype,
            enabled=bool(self.parameters.get("enabled", True)),
            strength=float(self.parameters.get("strength", 1.0)),
            space="object",
            axis=str(self.parameters.get("axis", "y")),
            center=np.array([
                float(self.parameters.get("center_x", 0.0)),
                float(self.parameters.get("center_y", 0.0)),
                float(self.parameters.get("center_z", 0.0)),
            ], dtype=np.float32),
            params=params,
        )

    def _deformer_param_sig(self) -> tuple:
        """变形参数签名（缓存 key 用，参数变 → 重建变形 VAO）。"""
        return (
            str(self.parameters.get("deformer_type", "bend")),
            bool(self.parameters.get("enabled", True)),
            float(self.parameters.get("strength", 1.0)),
            str(self.parameters.get("axis", "y")),
            float(self.parameters.get("angle", 45.0)),
            float(self.parameters.get("amount", 1.5)),
            str(self.parameters.get("mode", "normal")),
            float(self.parameters.get("center_x", 0.0)),
            float(self.parameters.get("center_y", 0.0)),
            float(self.parameters.get("center_z", 0.0)),
            float(self.parameters.get("frequency", 2.0)),
            float(self.parameters.get("amplitude", 0.1)),
            float(self.parameters.get("seed", 0.0)),
        )

    # ── 变形执行 ──

    @staticmethod
    def _skin_mesh(mesh, skin, anim_worlds: Dict[str, np.ndarray]):
        """将一个蒙皮 mesh 烘成当前姿态的普通 MeshData。

        Model3D 的 GPU shader 执行顺序是 ``skin -> model``，而 Deformer3D
        需要在 skin 之后变形。这里复用相同的 ``joint_world @ inverse_bind``
        约定在渲染线程中做向量化烘焙；后续输出不再携带 bone attributes，
        因此 Scene3D 不会把同一套骨骼矩阵应用两次。
        """
        from ..render3d.skinning import skin_mesh_cpu

        return skin_mesh_cpu(mesh, skin, anim_worlds)

    def _deform_flat_meshes(self, gl_ctx, upstream_meshes: list,
                            anim_worlds: Optional[Dict[str, np.ndarray]] = None
                            ) -> list:
        """对上游 flat_meshes 应用变形，返回新的 flat_meshes 列表。

        Args:
            gl_ctx: ModernGL context。
            upstream_meshes: 上游 ModelPayload.flat_meshes（list of dict）。

        Returns:
            变形后的 flat_meshes 列表（新 dict，含新 mesh/vao/vbo）。
        """
        from ..render3d.primitives import MeshData
        from ..render3d.mesh_vbo_pool import MeshVBOPool
        from ..render3d.deformers.base import DeformerStack

        spec = self._build_deformer_spec()
        # enabled=False 或空栈 → 直接透传上游 mesh（但仍需建自己的 VAO 引用）
        # 实际上 enabled=False 时变形为 no-op，但仍 emit payload（保持数据流）。
        stack = DeformerStack(deformers=[spec])
        pool = MeshVBOPool.for_context(gl_ctx)
        result = []
        self._last_diagnostics = []
        anim_worlds = anim_worlds or {}

        for fm in upstream_meshes:
            if not isinstance(fm, dict):
                continue
            base_mesh: Optional[MeshData] = fm.get("mesh")
            if base_mesh is None:
                # 无原始 mesh 数据（不应该发生），跳过
                continue
            is_skinned = bool(fm.get("is_skinned", base_mesh.is_skinned))
            deformed_mesh = base_mesh
            if spec.enabled and is_skinned:
                try:
                    # Correct order: current animation pose → deformer stack.
                    posed_mesh = self._skin_mesh(
                        base_mesh, fm.get("skin"), anim_worlds)
                    deform_result = stack.evaluate(posed_mesh)
                    deformed_mesh = MeshData(
                        vertices=deform_result.vertices,
                        normals=deform_result.normals,
                        uvs=posed_mesh.uvs.copy(),
                        indices=posed_mesh.indices.copy(),
                    )
                except (ValueError, TypeError, np.linalg.LinAlgError):
                    logger.warning("Deformer3D 蒙皮/变形失败，透传原 mesh",
                                   exc_info=True)
                    if "deformer.skinning_failed" not in self._last_diagnostics:
                        self._last_diagnostics.append("deformer.skinning_failed")
            elif spec.enabled and not stack.is_empty():
                try:
                    deform_result = stack.evaluate(base_mesh)
                    deformed_mesh = MeshData(
                        vertices=deform_result.vertices,
                        normals=deform_result.normals,
                        uvs=base_mesh.uvs.copy(),
                        indices=base_mesh.indices.copy(),
                    )
                except (ValueError, TypeError, np.linalg.LinAlgError):
                    logger.warning("Deformer3D 变形失败，透传原 mesh", exc_info=True)
                    if "deformer.evaluation_failed" not in self._last_diagnostics:
                        self._last_diagnostics.append("deformer.evaluation_failed")

            # 为变形后 mesh 建新 VBO/VAO（走 VBO 池，hash 不同自然新建）
            result_is_skinned = bool(deformed_mesh.is_skinned)
            has_tangent = bool(fm.get("has_tangent", False)) and not result_is_skinned
            try:
                if result_is_skinned:
                    prog = self._ensure_skinned_program(gl_ctx)
                    layout = '3f 3f 2f 4f 4f'
                    attrs = ('in_pos', 'in_normal', 'in_uv',
                             'in_bone_ids', 'in_bone_weights')
                elif has_tangent:
                    prog = self._ensure_mesh_tangent_program(gl_ctx)
                    layout = '3f 3f 2f 3f'
                    attrs = ('in_pos', 'in_normal', 'in_uv', 'in_tangent')
                else:
                    prog = self._ensure_mesh_program(gl_ctx)
                    layout = '3f 3f 2f'
                    attrs = ('in_pos', 'in_normal', 'in_uv')
                # pool.acquire 内部会按 layout 重新计算 interleaved bytes，
                # 这里不需要预计算 data（之前版本有死代码 data = ...tobytes()）
                vbo, ibo, _ic = pool.acquire(gl_ctx, deformed_mesh,
                                             has_tangent=has_tangent)
                contents = [(vbo, layout, *attrs)]
                vao = gl_ctx.vertex_array(prog, contents, index_buffer=ibo)
                result.append({
                    'vbo': vbo, 'ibo': ibo, 'vao': vao,
                    'program': prog,
                    'vbo_hash': deformed_mesh.content_hash(),
                    'mesh': deformed_mesh,
                    'index_count': int(deformed_mesh.indices.shape[0]),
                    'is_skinned': result_is_skinned,
                    'has_tangent': has_tangent,
                    'can_instance': False,  # 变形后强制不复用实例化
                    'world_matrix': fm.get('world_matrix', np.eye(4, dtype=np.float32)),
                    'material': fm.get('material'),
                    'material_id': fm.get('material_id', ''),
                    'skin': fm.get('skin') if result_is_skinned else None,
                    'node_name': fm.get('node_name', ''),
                    'node_id': fm.get('node_id', ''),
                })
            except Exception:
                logger.warning("Deformer3D 变形 mesh VAO 上传失败，跳过", exc_info=True)
                if "deformer.vao_upload_failed" not in self._last_diagnostics:
                    self._last_diagnostics.append("deformer.vao_upload_failed")
                continue
        return result

    # ── 求值 ──

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """接收上游 ModelPayload → 变形 → emit 新 ModelPayload。

        数据流（master_plan §16.9 显式节点链路）：
            上游 Model3D/Deformer3D emit ModelPayload
              → 本节点 _collect_connected_scene_payloads 取回
              → 对 flat_meshes 应用 DeformerStack 变形
              → 重建 VAO/VBO
              → emit 新 ModelPayload（transform_sig 含变形参数）
              → 下游 Scene3D/Deformer3D 取回渲染
        """
        from ..core.eval_context import CompositeEvalContext
        from ..render3d.scene_payload import ModelPayload

        # 无 GL context 或非合成求值上下文：emit 空降级 payload（不崩）
        eval_ctx = ctx if isinstance(ctx, CompositeEvalContext) else None
        if eval_ctx is None:
            self._emit_scene_payload(ctx, "display", ModelPayload(has_model=False))
            return {}

        gl_ctx = eval_ctx.gl
        # AUD-6607: GL context identity guard.  The deformation cache
        # signature and the lazy ``_ensure_*_program`` builders key on
        # content only — after a context recreation the cached VAOs and
        # programs belong to a destroyed context, and the cache-hit path
        # would hand Scene3D dead GL objects (mesh vanishing / driver-level
        # errors).  Same discipline as deflicker._ensure_gpu_resources and
        # particle._release_gpu_objects: on ctx change drop the cached
        # deformation and rebuild the programs for the new context
        # (drop-without-release — the old ctx's pool died with it).
        if self._last_gl_ctx is not None and gl_ctx is not self._last_gl_ctx:
            self._deformed_meshes = []
            self._deform_sig = ()
            self._release_mesh_programs(release=False)
        # 缓存 GL context 供 cleanup() 释放 VBO 池引用（cleanup 无 ctx 参数）
        self._last_gl_ctx = gl_ctx

        # 收集上游 ModelPayload（按连接拓扑，只取真正连到本节点 in 端口的）
        upstream_payloads = self._collect_connected_scene_payloads(eval_ctx, ModelPayload)

        # 无上游 / 上游无模型：emit 空降级 payload（Scene3D 退化为 procedural）
        if not upstream_payloads:
            self._emit_scene_payload(eval_ctx, "display", ModelPayload(has_model=False))
            return {}

        # 取第一个有效上游 payload（多上游连接时取首个；后续可扩展为合并）
        upstream: Optional[ModelPayload] = None
        for p in upstream_payloads:
            if p is not None and p.has_model and p.flat_meshes:
                upstream = p
                break

        if upstream is None or not upstream.flat_meshes:
            # 上游有连接但无有效模型 → 透传 has_model=False
            self._emit_scene_payload(eval_ctx, "display", ModelPayload(has_model=False))
            return {}

        # 变形缓存判定：参数签名 + 上游 mesh 签名没变 → 复用上次结果
        upstream_mesh_sig = tuple(
            fm.get('vbo_hash', '') for fm in upstream.flat_meshes
            if isinstance(fm, dict)
        )
        current_sig = (upstream_mesh_sig, self._deformer_param_sig(),
                       upstream.signature())
        if current_sig == self._deform_sig and self._deformed_meshes:
            # 缓存命中：动画矩阵已经包含在 upstream.signature() 中。
            deformed_meshes = self._deformed_meshes
        else:
            # 参数变化或首次：释放旧资源 + 重新变形
            self._release_deformed_meshes(gl_ctx)
            deformed_meshes = self._deform_flat_meshes(
                gl_ctx, upstream.flat_meshes,
                anim_worlds=dict(upstream.anim_worlds))
            self._deformed_meshes = deformed_meshes
            self._deform_sig = current_sig

        if not deformed_meshes:
            self._emit_scene_payload(eval_ctx, "display", ModelPayload(has_model=False))
            return {}

        # 构造变形后 ModelPayload：保留上游路径/mtime/动画，替换 flat_meshes，
        # transform_sig 塞入变形参数（Scene3D 据此在变形参数变化时重渲染）。
        deform_param_tuple = self._deformer_param_sig()
        # 上游 transform_sig + 变形参数（串联时上游 transform 已含上游变形）
        combined_transform_sig = tuple(upstream.transform_sig) + deform_param_tuple
        # 成功 CPU 烘焙后输出的是普通 mesh，不要把旧动画矩阵继续传给
        # Scene3D；否则未来某个消费者只看 anim_worlds 时可能误认为还需要二次蒙皮。
        # 蒙皮失败或节点禁用时，flat_meshes 仍保留 is_skinned=True，必须保留
        # anim_worlds 让 Scene3D 的 GPU fallback 继续工作。
        output_anim_worlds = (
            dict(upstream.anim_worlds)
            if any(bool(item.get("is_skinned"))
                   for item in deformed_meshes if isinstance(item, dict))
            else {}
        )
        payload = ModelPayload(
            flat_meshes=deformed_meshes,
            anim_worlds=output_anim_worlds,
            has_model=True,
            model_path=upstream.model_path,
            model_mtime=upstream.model_mtime,
            anim_time=upstream.anim_time,
            transform_sig=combined_transform_sig,
            texture_overrides=upstream.texture_overrides,
            material_params=upstream.material_params,
            material_overrides=upstream.material_overrides,
            materials=upstream.materials,
            morph_weights=upstream.morph_weights,
            model_cache_status=upstream.model_cache_status,
            model_import_status=upstream.model_import_status,
            model_import_phase=upstream.model_import_phase,
            model_import_progress=upstream.model_import_progress,
            model_import_diagnostics=upstream.model_import_diagnostics,
            model_import_resource_estimate=upstream.model_import_resource_estimate,
            model_import_probe=upstream.model_import_probe,
            node_id=upstream.node_id,
            hierarchy=upstream.hierarchy,
            skeletons=upstream.skeletons,
            model_error=upstream.model_error,
            hidden_mesh_ids=upstream.hidden_mesh_ids,
            user_hidden_mesh_ids=upstream.user_hidden_mesh_ids,
            locked_mesh_ids=upstream.locked_mesh_ids,
            solo_mesh_id=upstream.solo_mesh_id,
            render_flags=upstream.render_flags,
            animation_clips=upstream.animation_clips,
            import_report=upstream.import_report,
            diagnostics=tuple(upstream.diagnostics) + tuple(self._last_diagnostics),
        )
        self._emit_scene_payload(eval_ctx, "display", payload)
        return {}
