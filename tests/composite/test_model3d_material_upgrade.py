"""Model3D 材质系统升级测试。

覆盖 audit plan "Model3D UV, Texture, FBX, and Animation Audit" 的推荐项：
A. Material3DData 扩展（纹理描述符 + emissive scalar）
B. 7 个材质输入端口（base_color_in/normal_in/metallic_in/roughness_in/ao_in/emissive_in/mask_in）
C. 材质参数（material_mode/normal_format/orm_layout/texture_wrap/filter/emissive_intensity）
D. model_loader 提取导入纹理 + MeshData tangent 生成
E. shader 扩展（u_has_*_map 采样器 + tangent vertex）
F. anim_clip 动画片段选择
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import numpy as np
import pytest

from src.features.composite.render3d.scene_graph import (
    MaterialData, TextureDescriptor, TEXTURE_ROLE_COLOR, TEXTURE_ROLE_DATA)
from src.features.composite.render3d.primitives import MeshData, get_primitive


# ──────────────────────────────────────────────────────────────────────────
# Part A: Material3DData 扩展
# ──────────────────────────────────────────────────────────────────────────

class TestMaterial3DData:
    """MaterialData 扩展含纹理描述符 + emissive。"""

    def test_texture_descriptor_defaults(self):
        td = TextureDescriptor()
        assert td.path == ""
        assert td.role == TEXTURE_ROLE_DATA
        assert td.tex_coord_set == 0

    def test_material_has_texture_fields(self):
        m = MaterialData()
        for attr in ("base_color_tex", "normal_tex", "orm_tex", "ao_tex",
                     "emissive_tex", "emissive"):
            assert hasattr(m, attr)

    def test_material_has_no_texture_by_default(self):
        m = MaterialData()
        assert not m.has_any_texture()

    def test_material_with_texture_detected(self):
        m = MaterialData(base_color_tex=TextureDescriptor(path="/a.png", role=TEXTURE_ROLE_COLOR))
        assert m.has_any_texture()

    def test_emissive_default_black(self):
        m = MaterialData()
        assert m.emissive.shape == (3,)
        assert np.all(m.emissive == 0.0)


# ──────────────────────────────────────────────────────────────────────────
# Part B: Model3D 输入端口
# ──────────────────────────────────────────────────────────────────────────

class TestModel3DInputPorts:
    """Model3D 有 7 个材质纹理 override 输入端口（image 类型）。"""

    def test_has_seven_material_input_ports(self):
        from src.features.composite.nodes.model3d_node import Model3DNode
        n = Model3DNode()
        expected = {"base_color_in", "normal_in", "metallic_in",
                    "roughness_in", "ao_in", "emissive_in", "mask_in"}
        actual = {p.name for p in n.inputs}
        assert actual == expected

    def test_input_ports_are_image_type(self):
        from src.features.composite.nodes.model3d_node import Model3DNode
        n = Model3DNode()
        for p in n.inputs:
            assert p.data_type == "image", f"{p.name} 应为 image 类型"

    def test_collects_texture_overrides_in_evaluate(self):
        """_do_evaluate 收集端口输入的 texture_id 到 texture_overrides。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        src = inspect.getsource(Model3DNode._do_evaluate)
        assert "texture_overrides" in src
        assert "base_color_in" in src
        assert "texture_id" in src


# ──────────────────────────────────────────────────────────────────────────
# Part B2: 导入尺寸 / 单位转换
# ──────────────────────────────────────────────────────────────────────────

class TestModel3DImportUnits:
    """Model3D 支持 fit-to-scene 与真实单位导入模式。"""

    def test_has_import_unit_params(self):
        from src.features.composite.nodes.model3d_node import Model3DNode

        n = Model3DNode()
        assert n.parameters["size_mode"] == "real_world"
        assert n.parameters["source_unit"] == "m"
        assert n.parameters["unit_scale"] == 1.0
        assert n.parameters["axis_mode"] == "auto"
        assert n.parameters["uv_v_mode"] == "auto"

    def test_size_mode_options(self):
        from src.features.composite.nodes.model3d_node import Model3DNode

        pdef = next(p for p in Model3DNode.PARAM_DEFS if p.name == "size_mode")
        assert pdef.ptype == "option"
        assert "fit_to_scene" in pdef.labels
        assert "real_world" in pdef.labels

    def test_source_unit_to_meters(self):
        from src.features.composite.nodes.model3d_node import Model3DNode

        assert Model3DNode._source_unit_to_meters("m") == 1.0
        assert Model3DNode._source_unit_to_meters("cm") == 0.01
        assert Model3DNode._source_unit_to_meters("mm") == 0.001

    def test_do_evaluate_uses_size_mode_for_loader_and_signature(self):
        from src.features.composite.nodes.model3d_node import Model3DNode

        src = inspect.getsource(Model3DNode._do_evaluate)
        assert 'normalize_on_load = (size_mode != "real_world")' in src
        assert "normalize=normalize_on_load" in src
        assert "size_mode, source_unit, unit_scale" in src
        assert "axis_mode" in src
        assert "flip_uv_v" in src

    def test_uv_v_mode_auto_flips_fbx_and_obj(self):
        from src.features.composite.nodes.model3d_node import Model3DNode

        assert Model3DNode._should_flip_uv_v("auto", "/tmp/a.fbx")
        assert Model3DNode._should_flip_uv_v("auto", "/tmp/a.obj")
        assert not Model3DNode._should_flip_uv_v("auto", "/tmp/a.glb")
        assert Model3DNode._should_flip_uv_v("flip", "/tmp/a.glb")
        assert not Model3DNode._should_flip_uv_v("original", "/tmp/a.fbx")

    def test_axis_correction_auto_detects_z_up(self):
        from src.features.composite.nodes.model3d_node import Model3DNode

        mat = Model3DNode._axis_correction_matrix(
            "auto",
            np.asarray([-0.5, -0.2, -1.0], dtype=np.float32),
            np.asarray([0.5, 0.2, 1.0], dtype=np.float32),
        )
        p = mat @ np.asarray([0.0, 0.0, 1.0, 1.0], dtype=np.float32)
        assert p[1] == pytest.approx(1.0)

    def test_axis_correction_keeps_y_up(self):
        from src.features.composite.nodes.model3d_node import Model3DNode

        mat = Model3DNode._axis_correction_matrix(
            "auto",
            np.asarray([-0.5, -1.0, -0.2], dtype=np.float32),
            np.asarray([0.5, 1.0, 0.2], dtype=np.float32),
        )
        assert np.allclose(mat, np.eye(4, dtype=np.float32))

    def test_loader_defaults_to_real_world_size(self):
        from src.features.composite.render3d import model_loader

        sig = inspect.signature(model_loader.load_model)
        assert sig.parameters["normalize"].default is False
        assert sig.parameters["flip_uv_v"].default is False


# ──────────────────────────────────────────────────────────────────────────
# Part C: 材质参数
# ──────────────────────────────────────────────────────────────────────────

class TestMaterialParams:
    """Model3D 有材质控制参数。"""

    def test_has_material_params(self):
        from src.features.composite.nodes.model3d_node import Model3DNode
        n = Model3DNode()
        for k in ("material_mode", "normal_format", "orm_layout",
                  "texture_filter", "texture_wrap", "emissive_intensity"):
            assert k in n.parameters, f"应含 {k}"

    def test_imported_atlas_defaults_to_linear_clamp(self):
        from src.features.composite.nodes.model3d_node import Model3DNode

        n = Model3DNode()
        assert n.parameters["texture_filter"] == "linear"
        assert n.parameters["texture_wrap"] == "clamp"

    def test_material_mode_options(self):
        from src.features.composite.nodes.model3d_node import Model3DNode
        md = next(p for p in Model3DNode.PARAM_DEFS if p.name == "material_mode")
        assert md.ptype == "option"
        assert "imported" in md.labels
        assert "override" in md.labels

    def test_material_params_passed_to_payload(self):
        """材质参数填入 ModelPayload.material_params。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        from src.features.composite.render3d.scene_payload import ModelPayload
        p = ModelPayload(has_model=True)
        assert hasattr(p, "material_params")


# ──────────────────────────────────────────────────────────────────────────
# Part D: model_loader 纹理提取 + tangent 生成
# ──────────────────────────────────────────────────────────────────────────

class TestTextureExtraction:
    """model_loader 提取导入纹理路径到 TextureDescriptor。"""

    def test_convert_trimesh_material_extracts_textures(self):
        """_convert_trimesh_material 提取纹理描述符（源码级断言）。"""
        from src.features.composite.render3d import model_loader
        src = inspect.getsource(model_loader._convert_trimesh_material)
        assert "base_color_tex" in src
        assert "normal_tex" in src
        assert "orm_tex" in src
        assert "emissive_tex" in src

    def test_extract_texture_path_helper_exists(self):
        """有 _extract_texture_path 辅助函数。"""
        from src.features.composite.render3d import model_loader
        assert hasattr(model_loader, "_extract_texture_path")

    def test_material_with_texture_paths(self):
        """构造带纹理的 MaterialData，has_any_texture 正确。"""
        m = MaterialData(
            base_color_tex=TextureDescriptor(path="/base.png", role=TEXTURE_ROLE_COLOR),
            normal_tex=TextureDescriptor(path="/normal.png"),
            orm_tex=TextureDescriptor(path="/orm.png"),
            emissive_tex=TextureDescriptor(path="/emi.png", role=TEXTURE_ROLE_COLOR),
        )
        assert m.has_any_texture()
        assert m.base_color_tex.role == "color"  # baseColor 需 sRGB→linear
        assert m.normal_tex.role == "data"       # normal 保持线性

    def test_embedded_trimesh_texture_is_cached(self, tmp_path, monkeypatch):
        """GLB/FBX fallback 的内嵌 PIL 贴图会落盘成 TextureDescriptor.path。"""
        Image = pytest.importorskip("PIL.Image")
        from src.features.composite.render3d import model_loader

        monkeypatch.setattr(model_loader, "_embedded_texture_cache_dir",
                            lambda source_path="": str(tmp_path))

        class FakeMaterial:
            baseColorTexture = Image.new("RGB", (2, 2), (128, 64, 32))

        path = model_loader._extract_texture_path(
            FakeMaterial(), "baseColorTexture", source_path="/tmp/model.glb", material_key="mat")
        assert path.endswith("_baseColorTexture.png")
        assert os.path.isfile(path)

    def test_single_channel_mr_texture_is_packed_as_orm(self, tmp_path, monkeypatch):
        """单通道 roughness 图不能被当成 metallic；缓存时打包成 AO/R/M。"""
        Image = pytest.importorskip("PIL.Image")
        from src.features.composite.render3d import model_loader

        monkeypatch.setattr(model_loader, "_embedded_texture_cache_dir",
                            lambda source_path="": str(tmp_path))

        class FakeMaterial:
            metallicRoughnessTexture = Image.new("L", (1, 1), 77)
            metallicFactor = 0.0

        path = model_loader._extract_texture_path(
            FakeMaterial(), "metallicRoughnessTexture",
            source_path="/tmp/model.glb", material_key="mat")
        px = Image.open(path).convert("RGB").getpixel((0, 0))
        assert px == (255, 77, 0)

    def test_color_factor_255_arrays_are_normalized(self):
        from src.features.composite.render3d import model_loader

        c = model_loader._normalize_color_factor(np.asarray([204, 102, 0, 255], dtype=np.uint8))
        assert c.tolist() == pytest.approx([0.8, 0.4, 0.0])

    def test_flip_scene_uv_v(self):
        from src.features.composite.render3d import model_loader
        from src.features.composite.render3d import scene_graph as sg

        mesh = MeshData(
            vertices=np.asarray([[0.0, 0.0, 0.0]], dtype=np.float32),
            normals=np.asarray([[0.0, 1.0, 0.0]], dtype=np.float32),
            uvs=np.asarray([[0.25, 0.2]], dtype=np.float32),
            indices=np.asarray([0], dtype=np.uint32),
        )
        root = sg.SceneNode(mesh=mesh)
        model_loader._flip_scene_uv_v(root)
        assert mesh.uvs[0, 0] == pytest.approx(0.25)
        assert mesh.uvs[0, 1] == pytest.approx(0.8)

    def test_embedded_texture_dir_prefers_model_sibling_fbm(self, tmp_path):
        from src.features.composite.render3d import model_loader

        model_path = tmp_path / "asset.fbx"
        model_path.write_bytes(b"fbx")
        out_dir = model_loader._embedded_texture_cache_dir(str(model_path))
        assert out_dir == str(tmp_path / "asset.fbm")
        assert os.path.isdir(out_dir)


class TestAssimpFaceConversion:
    """FBX/assimp face containers convert to visible triangle indices."""

    def test_numpy_faces_are_flattened(self):
        from src.features.composite.render3d.model_loader import _assimp_faces_to_indices

        faces = np.asarray([[0, 1, 2], [2, 3, 0]], dtype=np.uint32)
        assert _assimp_faces_to_indices(faces) == [0, 1, 2, 2, 3, 0]

    def test_quad_faces_are_triangulated(self):
        from src.features.composite.render3d.model_loader import _assimp_faces_to_indices

        faces = [np.asarray([0, 1, 2, 3], dtype=np.uint32)]
        assert _assimp_faces_to_indices(faces) == [0, 1, 2, 0, 2, 3]

    def test_assimp_mesh_accepts_numpy_faces_and_texcoords(self):
        from src.features.composite.render3d.model_loader import _convert_assimp_mesh

        class FakeMesh:
            vertices = np.asarray(
                [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
                dtype=np.float32,
            )
            normals = np.asarray(
                [[0.0, 0.0, 1.0], [0.0, 0.0, 1.0], [0.0, 0.0, 1.0]],
                dtype=np.float32,
            )
            texcoords = [np.asarray(
                [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
                dtype=np.float32,
            )]
            faces = np.asarray([[0, 1, 2]], dtype=np.uint32)

        mesh = _convert_assimp_mesh(FakeMesh())
        assert mesh is not None
        assert mesh.indices.tolist() == [0, 1, 2]
        assert mesh.uvs.shape == (3, 2)

    def test_assimp_node_accepts_mesh_objects(self):
        from src.features.composite.render3d.model_loader import _convert_assimp_node

        class FakeMesh:
            name = "body"
            materialindex = 0
            vertices = np.asarray(
                [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
                dtype=np.float32,
            )
            normals = np.asarray(
                [[0.0, 0.0, 1.0], [0.0, 0.0, 1.0], [0.0, 0.0, 1.0]],
                dtype=np.float32,
            )
            texcoords = None
            faces = np.asarray([[0, 1, 2]], dtype=np.uint32)

        class FakeNode:
            name = "root"
            transformation = None
            meshes = [FakeMesh()]
            children = []

        node = _convert_assimp_node(FakeNode(), [], [])
        assert len(node.children) == 1
        assert node.children[0].mesh is not None

    def test_assimp_node_transform_keeps_translation_column(self):
        from src.features.composite.render3d.model_loader import _convert_assimp_node

        class FakeNode:
            name = "root"
            transformation = np.asarray(
                [[1.0, 0.0, 0.0, 4.0],
                 [0.0, 1.0, 0.0, 5.0],
                 [0.0, 0.0, 1.0, 6.0],
                 [0.0, 0.0, 0.0, 1.0]],
                dtype=np.float32,
            )
            meshes = []
            children = []

        node = _convert_assimp_node(FakeNode(), [], [])
        assert node.local_transform[:3, 3].tolist() == [4.0, 5.0, 6.0]
        assert node.local_transform[3, :3].tolist() == [0.0, 0.0, 0.0]


class TestTangentGeneration:
    """MeshData 切线生成（normal mapping 用）。"""

    def test_cube_generates_tangents(self):
        cube = get_primitive("cube")
        t = cube.compute_tangents()
        assert t.shape == (cube.vertex_count, 4)
        assert cube.has_tangents

    def test_tangent_is_cached(self):
        cube = get_primitive("cube")
        t1 = cube.compute_tangents()
        t2 = cube.compute_tangents()
        assert t1 is t2  # 缓存命中

    def test_interleaved_with_tangent_layout(self):
        """interleaved_with_tangent 返回 (N,11) 布局。"""
        cube = get_primitive("cube")
        buf = cube.interleaved_with_tangent()
        assert buf.shape[1] == 11  # pos(3)+normal(3)+uv(2)+tangent(3)

    def test_degenerate_mesh_returns_zero_tangent(self):
        """顶点 <3 或无 UV 时返回零切线（不崩）。"""
        m = MeshData(
            vertices=np.array([[0, 0, 0]], dtype=np.float32),
            normals=np.array([[0, 1, 0]], dtype=np.float32),
            uvs=np.array([[0, 0]], dtype=np.float32),
            indices=np.array([0], dtype=np.uint32),
        )
        t = m.compute_tangents()
        assert t.shape == (1, 4)
        assert np.all(t == 0.0)


# ──────────────────────────────────────────────────────────────────────────
# Part E: shader 扩展
# ──────────────────────────────────────────────────────────────────────────

class TestShaderExtension:
    """_MESH_PBR_FRAGMENT 含材质纹理采样器 + has_map 标志 + tangent vertex。"""

    def test_fragment_has_material_samplers(self):
        from src.features.composite.core.shaders import _MESH_PBR_FRAGMENT
        for u in ("u_base_color_map", "u_normal_map", "u_orm_map",
                  "u_ao_map", "u_emissive_map"):
            assert u in _MESH_PBR_FRAGMENT, f"缺 {u}"

    def test_fragment_has_has_map_flags(self):
        from src.features.composite.core.shaders import _MESH_PBR_FRAGMENT
        for u in ("u_has_base_color_map", "u_has_normal_map", "u_has_orm_map",
                  "u_has_ao_map", "u_has_emissive_map"):
            assert u in _MESH_PBR_FRAGMENT

    def test_fragment_has_emissive_uniforms(self):
        from src.features.composite.core.shaders import _MESH_PBR_FRAGMENT
        assert "u_emissive_intensity" in _MESH_PBR_FRAGMENT
        assert "u_emissive" in _MESH_PBR_FRAGMENT
        assert "u_normal_format" in _MESH_PBR_FRAGMENT

    def test_fragment_samples_base_color_map(self):
        """main() 在 has_map 时采样 base_color_map 并转线性。"""
        from src.features.composite.core.shaders import _MESH_PBR_FRAGMENT
        assert "texture(u_base_color_map, v_uv)" in _MESH_PBR_FRAGMENT
        assert "pow(tex_col, vec3(2.2))" in _MESH_PBR_FRAGMENT  # sRGB→linear

    def test_fragment_applies_ao_to_ambient(self):
        """AO 压暗间接光（ambient *= ao）。"""
        from src.features.composite.core.shaders import _MESH_PBR_FRAGMENT
        assert "ambient *= ao" in _MESH_PBR_FRAGMENT

    def test_tangent_vertex_shader_exists(self):
        """_MESH_VERTEX_TANGENT 存在且输出 v_tangent。"""
        from src.features.composite.core.shaders import _MESH_VERTEX_TANGENT
        assert "in_tangent" in _MESH_VERTEX_TANGENT
        assert "v_tangent = mat3(u_model) * in_tangent" in _MESH_VERTEX_TANGENT

    def test_original_vertex_outputs_placeholder_tangent(self):
        """_MESH_VERTEX 输出占位 v_tangent=0（避免链接错误）。"""
        from src.features.composite.core.shaders import _MESH_VERTEX
        assert "v_tangent = vec3(0.0)" in _MESH_VERTEX

    def test_scene3d_sets_material_uniform_defaults(self):
        """Scene3D 路径 A/B/built-in 设 has_map=0 默认值（shader 链接安全）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "u_has_base_color_map" in src
        assert "u_emissive" in src

    def test_model3d_path_binds_null_material_samplers(self):
        """Model3D 无贴图时 sampler2D 不应默认指向 IBL 的 cube map 单元。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode

        src = inspect.getsource(Scene3DNode._do_evaluate)
        null_idx = src.index("self._bind_null_material_samplers(mesh_prog)")
        bind_idx = src.index("self._bind_material_textures(")
        assert null_idx < bind_idx

    def test_model3d_normal_map_enabled_when_tangent_present(self):
        """F5：mesh VAO 含 tangent 属性时启用 normal map 采样。

        Model3D 在材质含 normal_tex 时构建 tangent VAO（has_tangent=True）；
        Scene3D 据此 + 材质确实绑定 normal map 时设 u_has_normal_map=1。
        has_tangent=False 时保持 0（向后兼容）。
        """
        from src.features.composite.nodes.scene3d_node import Scene3DNode

        src = inspect.getsource(Scene3DNode._do_evaluate)
        # 应检查 m.get('has_tangent') 决定是否启用
        assert "has_tangent" in src
        assert "_has_map.get(\"normal\")" in src
        assert "u_has_normal_map'].value = int(_has_normal)" in src

    def test_quad_ortho_views_bind_model3d_material_textures(self):
        """四视图正交渲染也应绑定 Model3D 导入贴图，而不是灰模。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode

        src = inspect.getsource(Scene3DNode._render_ortho_views)
        assert "_bind_material_textures(" in src
        assert 'u_has_base_color_map\'].value = int(_has_map["base_color"])' in src
        assert 'u_has_orm_map\'].value = int(_has_map["orm"])' in src
        assert "texture_overrides = _payload.texture_overrides or {}" in src


# ──────────────────────────────────────────────────────────────────────────
# Part F: anim_clip 动画片段选择
# ──────────────────────────────────────────────────────────────────────────

class TestAnimClipSelection:
    """Model3D anim_clip 选择第 N 个动画片段。"""

    def test_model3d_has_anim_clip_param(self):
        from src.features.composite.nodes.model3d_node import Model3DNode
        n = Model3DNode()
        assert "anim_clip" in n.parameters
        assert n.parameters["anim_clip"] == 0  # 默认第一个

    def test_sample_animation_accepts_clip_index(self):
        """_sample_animation_worlds 接受 anim_clip 参数选片段。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        sig = inspect.signature(Model3DNode._sample_animation_worlds)
        assert "anim_clip" in sig.parameters

    def test_clip_index_clamped(self):
        """anim_clip 超出范围回退到 0（钳制逻辑）。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        src = inspect.getsource(Model3DNode._sample_animation_worlds)
        assert "clip_idx" in src
        assert "min(int(anim_clip)" in src  # 钳制到 [0, len-1]

    def test_evaluate_passes_anim_clip(self):
        """_do_evaluate 把 anim_clip 传给 _sample_animation_worlds。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        src = inspect.getsource(Model3DNode._do_evaluate)
        assert "anim_clip" in src
        assert "_sample_animation_worlds(anim_enabled, anim_time, anim_speed, anim_clip)" in src


# ──────────────────────────────────────────────────────────────────────────
# Part: Payload 携带纹理/材质数据
# ──────────────────────────────────────────────────────────────────────────

class TestModelPayloadMaterialData:
    """ModelPayload 携带 texture_overrides + material_params。"""

    def test_payload_has_texture_overrides_field(self):
        from src.features.composite.render3d.scene_payload import ModelPayload
        p = ModelPayload(has_model=True)
        assert hasattr(p, "texture_overrides")
        assert p.texture_overrides is None  # 默认 None

    def test_payload_has_material_params_field(self):
        from src.features.composite.render3d.scene_payload import ModelPayload
        p = ModelPayload(has_model=True)
        assert hasattr(p, "material_params")
        assert p.material_params is None

    def test_transform_sig_includes_texture_overrides(self):
        """transform_sig 含纹理 override（连纹理触发重渲染）。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        src = inspect.getsource(Model3DNode._do_evaluate)
        # 签名元组里有 sorted(texture_overrides.items())
        assert "sorted(texture_overrides.items())" in src
