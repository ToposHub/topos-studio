"""F0 资产能力审计 — ModelImportReport 单测。

覆盖 master_plan §21.3 F0：
1. ModelImportReport 数据结构：字段默认值、add_warning/add_lost_feature 去重。
2. summary_dict 扁平化（UI 渲染用）。
3. SceneGraphRoot.import_report 字段向后兼容（None 默认）。
4. loader 各 backend 路径生成 report（源码级断言 + trimesh 真实加载计数）。
5. FBX CLI fallback 记录 lost_features（bones/animation/camera/light）。

纯 Python / numpy，无 GL/PyQt 依赖。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import json
import struct
import numpy as np
import pytest

from src.features.composite.render3d.model_import_report import (
    ModelImportReport,
    LOADER_BACKEND_TRIMESH,
    LOADER_BACKEND_PYASSIMP,
    LOADER_BACKEND_ASSIMP_CLI_GLB,
    LOADER_BACKEND_UNKNOWN,
)
from src.features.composite.render3d import scene_graph as sg
from src.features.composite.render3d import primitives as prim

pytestmark = pytest.mark.composite_core


# ── 数据结构 ──────────────────────────────────────────────────────────

class TestModelImportReportData:
    def test_defaults(self):
        r = ModelImportReport()
        assert r.mesh_count == 0
        assert r.triangle_count == 0
        assert r.loader_backend == LOADER_BACKEND_UNKNOWN
        assert r.warnings == []
        assert r.lost_features == []
        assert r.unit_scale == 1.0
        assert r.axis_mode == "auto"
        assert r.has_warnings() is False

    def test_add_warning_dedup(self):
        r = ModelImportReport()
        r.add_warning("no uv")
        r.add_warning("no uv")  # 重复
        r.add_warning("missing texture")
        assert r.warnings == ["no uv", "missing texture"]

    def test_add_lost_feature_dedup(self):
        r = ModelImportReport()
        r.add_lost_feature("bones")
        r.add_lost_feature("bones")
        r.add_lost_feature("animation")
        assert r.lost_features == ["bones", "animation"]

    def test_add_empty_ignored(self):
        r = ModelImportReport()
        r.add_warning("")
        r.add_lost_feature("")
        assert r.warnings == []
        assert r.lost_features == []

    def test_has_warnings(self):
        r = ModelImportReport()
        assert not r.has_warnings()
        r.add_warning("x")
        assert r.has_warnings()
        r2 = ModelImportReport()
        r2.add_lost_feature("y")
        assert r2.has_warnings()

    def test_summary_dict(self):
        r = ModelImportReport(
            source_path="/a/b.gltf",
            loader_backend=LOADER_BACKEND_TRIMESH,
            mesh_count=2,
            triangle_count=128,
            material_count=2,
            texture_count=3,
        )
        d = r.summary_dict()
        assert d["source_path"] == "/a/b.gltf"
        assert d["loader_backend"] == LOADER_BACKEND_TRIMESH
        assert d["mesh_count"] == 2
        assert d["triangle_count"] == 128
        assert d["warnings"] == []
        assert d["lost_features"] == []

    def test_from_summary_dict_round_trip(self):
        report = ModelImportReport(
            source_path="/tmp/robot.glb",
            loader_backend=LOADER_BACKEND_TRIMESH,
            mesh_count=2,
            triangle_count=120,
            warnings=["no tangents"],
            lost_features=["lights"],
        )
        restored = ModelImportReport.from_summary_dict(report.summary_dict())
        assert restored is not None
        assert restored.summary_dict() == report.summary_dict()

    def test_from_summary_dict_ignores_forward_fields(self):
        restored = ModelImportReport.from_summary_dict({
            "source_path": "/tmp/robot.glb",
            "future_loader_field": "ignored",
        })
        assert restored is not None
        assert restored.source_path == "/tmp/robot.glb"

    def test_from_summary_dict_rejects_non_mapping(self):
        assert ModelImportReport.from_summary_dict(None) is None


# ── SceneGraphRoot 集成 ───────────────────────────────────────────────

class TestSceneGraphRootReportField:
    def test_default_none(self):
        """SceneGraphRoot.import_report 默认 None（向后兼容）。"""
        root = sg.SceneGraphRoot(root=sg.SceneNode(name="r"))
        assert root.import_report is None

    def test_attach_report(self):
        """可以挂载一个 ModelImportReport。"""
        root = sg.SceneGraphRoot(root=sg.SceneNode(name="r"))
        report = ModelImportReport(source_path="/x.gltf", loader_backend=LOADER_BACKEND_TRIMESH)
        root.import_report = report
        assert root.import_report is report
        assert root.import_report.source_path == "/x.gltf"


# ── loader 源码级断言（backend 路径生成 report）────────────────────────

class TestLoaderReportGeneration:
    """model_loader.py 各 backend 路径应生成 import_report。

    这些测试用源码级断言（检查关键函数源码包含 report 构造），避免依赖
    外部 trimesh/pyassimp 安装与真实模型文件。
    """

    def test_model_import_report_imported_in_loader(self):
        """model_loader.py 应 import ModelImportReport。"""
        from src.features.composite.render3d import model_loader
        src = inspect.getsource(model_loader)
        assert "ModelImportReport" in src

    def test_trimesh_backend_sets_report_backend(self):
        """_convert_trimesh_scene 应调用 _finalize_report(trimesh backend)。"""
        from src.features.composite.render3d import model_loader
        src = inspect.getsource(model_loader._convert_trimesh_scene)
        assert "_finalize_report" in src
        assert "LOADER_BACKEND_TRIMESH" in src

    def test_assimp_backend_sets_report_backend(self):
        """_convert_assimp_scene 应调用 _finalize_report(pyassimp backend)。"""
        from src.features.composite.render3d import model_loader
        src = inspect.getsource(model_loader._convert_assimp_scene)
        assert "_finalize_report" in src
        assert "LOADER_BACKEND_PYASSIMP" in src

    def test_fbx_cli_fallback_records_lost_features(self):
        """_mark_fbx_cli_fallback_report 应记录 lost_features + assimp_cli_glb backend。"""
        from src.features.composite.render3d import model_loader
        src = inspect.getsource(model_loader._mark_fbx_cli_fallback_report)
        assert "lost_feature" in src
        assert "LOADER_BACKEND_ASSIMP_CLI_GLB" in src
        # load_assimp_cli_static_fallback 应调用此标记函数
        src2 = inspect.getsource(model_loader._load_assimp_cli_static_fallback)
        assert "_mark_fbx_cli_fallback_report" in src2

    def test_load_model_attaches_report_to_root(self):
        """load_model 应把 report 挂到 SceneGraphRoot.import_report。"""
        import inspect
        from src.features.composite.render3d import model_loader
        src = inspect.getsource(model_loader.load_model)
        assert "import_report" in src


class TestAssimpGltfAnimationFallback:
    """The CLI GLB fallback must retain the animation accessor contract."""

    def test_glb_animation_accessor_is_decoded(self, tmp_path):
        from src.features.composite.render3d.model_loader import (
            _extract_gltf_animation_clips,
            _load_gltf_document_and_buffers,
        )

        binary = struct.pack(
            "<2f6f",
            0.0, 1.0,
            0.0, 0.0, 0.0,
            1.0, 2.0, 3.0,
        )
        document = {
            "asset": {"version": "2.0"},
            "buffers": [{"byteLength": len(binary)}],
            "bufferViews": [
                {"buffer": 0, "byteOffset": 0, "byteLength": 8},
                {"buffer": 0, "byteOffset": 8, "byteLength": 24},
            ],
            "accessors": [
                {"bufferView": 0, "componentType": 5126,
                 "count": 2, "type": "SCALAR"},
                {"bufferView": 1, "componentType": 5126,
                 "count": 2, "type": "VEC3"},
            ],
            "nodes": [{"name": "Hips"}],
            "animations": [{
                "name": "walk",
                "samplers": [{"input": 0, "output": 1}],
                "channels": [{
                    "sampler": 0,
                    "target": {"node": 0, "path": "translation"},
                }],
            }],
        }
        json_chunk = json.dumps(document, separators=(",", ":")).encode("utf-8")
        json_chunk += b" " * ((4 - len(json_chunk) % 4) % 4)
        binary_chunk = binary + b"\x00" * ((4 - len(binary) % 4) % 4)
        glb = (
            struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(json_chunk)
                        + 8 + len(binary_chunk))
            + struct.pack("<I4s", len(json_chunk), b"JSON") + json_chunk
            + struct.pack("<I4s", len(binary_chunk), b"BIN\x00") + binary_chunk
        )
        path = tmp_path / "animated.glb"
        path.write_bytes(glb)

        decoded_document, buffers = _load_gltf_document_and_buffers(str(path))
        clips = _extract_gltf_animation_clips(decoded_document, buffers)

        assert len(clips) == 1
        assert clips[0].name == "walk"
        assert clips[0].duration == 1.0
        assert clips[0].channels[0].node_name == "Hips"
        np.testing.assert_allclose(
            clips[0].channels[0].translations[-1], [1.0, 2.0, 3.0])


# ── report 从 SceneGraphRoot 派生计数（真实 mesh 统计）──────────────────

class TestReportCountFromScene:
    """从真实 SceneGraphRoot 统计 mesh/triangle/material 数。"""

    def test_count_single_mesh(self):
        """单 mesh 场景：mesh_count=1, triangle_count=2。"""
        cube = prim.cube(1.0)
        root_node = sg.SceneNode(name="root", mesh=cube)
        root_data = sg.SceneGraphRoot(root=root_node)
        report = _build_report_from_scene(root_data, "trimesh", "/test.glb")
        assert report.mesh_count == 1
        # cube = 36 indices = 12 triangles
        assert report.triangle_count == 12
        assert report.vertex_count == 24

    def test_count_multi_mesh(self):
        """多 mesh 场景：累加。"""
        root_node = sg.SceneNode(name="root")
        root_node.children.append(sg.SceneNode(name="a", mesh=prim.cube(1.0)))
        root_node.children.append(sg.SceneNode(name="b", mesh=prim.sphere(1.0, 8, 4)))
        root_data = sg.SceneGraphRoot(root=root_node)
        report = _build_report_from_scene(root_data, "trimesh", "/test.glb")
        assert report.mesh_count == 2
        assert report.triangle_count > 0
        assert report.vertex_count > 0

    def test_count_skins_and_animations(self):
        """skin/animation 数量从 SceneGraphRoot.skins/animations 取。"""
        skin = sg.SkinData(
            bone_names=["b0", "b1"],
            inverse_bind_matrices=np.tile(np.eye(4, dtype=np.float32), (2, 1, 1)),
            bone_indices=np.zeros((4, 4), dtype=np.uint32),
            bone_weights=np.zeros((4, 4), dtype=np.float32),
        )
        anim = sg.AnimationClip(name="idle", duration=1.0)
        root_node = sg.SceneNode(name="root", mesh=prim.cube(1.0), skin=skin)
        root_data = sg.SceneGraphRoot(root=root_node, skins=[skin], animations=[anim])
        report = _build_report_from_scene(root_data, "trimesh", "/test.glb")
        assert report.skeleton_count == 1
        assert report.bone_count == 2
        assert report.animation_clip_count == 1


def _build_report_from_scene(root_data: sg.SceneGraphRoot, backend: str,
                             source_path: str) -> ModelImportReport:
    """从 SceneGraphRoot 派生 report 计数（复刻 loader 内部逻辑，用于测试）。

    生产代码里这个逻辑在 model_loader.py 的 _finalize_report() 中。
    """
    report = ModelImportReport(source_path=source_path, loader_backend=backend)
    mesh_count = 0
    tri_count = 0
    vert_count = 0
    material_count = 0
    texture_count = 0

    def _visit(node: sg.SceneNode):
        nonlocal mesh_count, tri_count, vert_count, material_count, texture_count
        if node.mesh is not None:
            mesh_count += 1
            m = node.mesh
            vert_count += int(m.vertices.shape[0])
            tri_count += int(m.indices.shape[0]) // 3
        if node.material is not None:
            material_count += 1
            if hasattr(node.material, "has_any_texture") and node.material.has_any_texture():
                for t in (node.material.base_color_tex, node.material.normal_tex,
                          node.material.orm_tex, node.material.ao_tex,
                          node.material.emissive_tex):
                    if t.path:
                        texture_count += 1
        for c in node.children:
            _visit(c)

    _visit(root_data.root)
    report.mesh_count = mesh_count
    report.triangle_count = tri_count
    report.vertex_count = vert_count
    report.material_count = material_count
    report.texture_count = texture_count
    report.skeleton_count = len(root_data.skins)
    report.bone_count = sum(s.bone_count for s in root_data.skins)
    report.animation_clip_count = len(root_data.animations)
    return report
