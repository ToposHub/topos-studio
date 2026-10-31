"""Scene3DNode 纯逻辑补测（计划02 测试覆盖补齐，模块 agent-C1）。

目标：src/features/composite/nodes/scene3d_node.py 的 headless 可测单元——
参数 schema、序列化往返、拾取 id 编码/映射、组件拾取几何、矩阵数学
（阴影灯光相机 / 正交相机）、LOD/quality profile 决策、材质排序与 override、
Cryptomatte manifest 元数据、共享 uniform 上传（含 view 级缓存）、
shadow target 生命周期（mock ctx）、VAO 懒建守卫、渲染 pass 早退路径、
AOV 输出结果构造、redraw_with_view 门控与 mock 求值路径、cleanup。

不依赖 GL / QApplication：所有 GL 对象用轻量 fake 替代。
"""
import json
import os
import sys
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import numpy as np
import pytest

from src.features.composite.core.format import BBox
from src.features.composite.nodes.base_node import ParamDef, Port, TextureResult
from src.features.composite.nodes.scene3d_node import (
    Scene3DNode,
    _srgb_to_linear_channel,
)
from src.features.composite.render3d.aov import (
    AOVPass,
    AOV_EXPORTABLE_PASSES,
    IDAllocator,
    encode_id_to_rgba,
)
from src.features.composite.render3d.material_model import MaterialOverride
from src.features.composite.render3d.perf_stats import Viewport3DPerfStats
from src.features.composite.render3d.primitives import MeshData, get_primitive
from src.features.composite.render3d.scene_graph import MaterialData
from src.features.composite.ui.viewport3d_state import (
    LARGE_MODEL_TRIANGLE_THRESHOLD,
    ViewportQualityProfile,
)


# ──────────────────────────────────────────────────────────────────────────
# Fake GL 对象
# ──────────────────────────────────────────────────────────────────────────

class FakeUniform:
    """记录赋值历史的 uniform。"""

    def __init__(self) -> None:
        self.value: Any = None
        self.set_count: int = 0

    def __repr__(self) -> str:
        return f"FakeUniform(value={self.value!r}, sets={self.set_count})"


class FakeProgram:
    """支持 prog['name'].value = x 的假 program，记录全部赋值。"""

    def __init__(self) -> None:
        self.uniforms: Dict[str, FakeUniform] = {}

    def __getitem__(self, name: str) -> FakeUniform:
        if name not in self.uniforms:
            self.uniforms[name] = FakeUniform()
        return self.uniforms[name]

    def __contains__(self, name: str) -> bool:
        return name in self.uniforms

    def value_of(self, name: str) -> Any:
        uniform = self.uniforms.get(name)
        return None if uniform is None else uniform.value


class FakeGLObject:
    """带 release() 记录的假 GL 对象（texture/buffer/renderbuffer/fbo）。"""

    def __init__(self, name: str = "") -> None:
        self.name: str = name
        self.released: bool = False
        self.release_count: int = 0
        self.glo: int = id(self) & 0xFFFFFF

    def release(self) -> None:
        self.release_count += 1
        self.released = True


class FakeTexture(FakeGLObject):
    """假纹理：filter 可写 + use(location) 记录。"""

    def __init__(self, name: str = "") -> None:
        super().__init__(name)
        self.filter: Tuple[int, int] = (0, 0)
        self.used_locations: List[int] = []

    def use(self, location: int = 0) -> None:
        self.used_locations.append(int(location))


class FakeRenderbuffer(FakeGLObject):
    pass


class FakeFramebuffer(FakeGLObject):
    def __init__(self, name: str = "") -> None:
        super().__init__(name)
        self.used: int = 0

    def use(self) -> None:
        self.used += 1


class FakeGLContext:
    """按需产生 fake GL 资源的假 ctx；可注入异常模拟分配失败。"""

    def __init__(self) -> None:
        self.buffers: List[bytes] = []
        self.textures: List[FakeTexture] = []
        self.renderbuffers: List[FakeRenderbuffer] = []
        self.framebuffers: List[FakeFramebuffer] = []
        self.programs: List[FakeProgram] = []
        self.fail_texture: bool = False

    def buffer(self, data: bytes) -> FakeGLObject:
        self.buffers.append(bytes(data))
        obj = FakeGLObject("buffer")
        obj.release = self._make_release_tracker(obj)
        return obj

    def texture(self, size: Tuple[int, int], components: int,
                 dtype: str = "f4", samples: int = 0) -> FakeTexture:
        if self.fail_texture:
            raise RuntimeError("simulated texture allocation failure")
        tex = FakeTexture(f"tex{len(self.textures)}")
        self.textures.append(tex)
        return tex

    def depth_renderbuffer(self, size: Tuple[int, int]) -> FakeRenderbuffer:
        rb = FakeRenderbuffer("depth_rb")
        self.renderbuffers.append(rb)
        return rb

    def framebuffer(self, color_attachments: Any = (),
                    depth_attachment: Any = None) -> FakeFramebuffer:
        fbo = FakeFramebuffer("fbo")
        self.framebuffers.append(fbo)
        return fbo

    def program(self, vertex_shader: str = "", fragment_shader: str = "") -> FakeProgram:
        prog = FakeProgram()
        self.programs.append(prog)
        return prog

    @staticmethod
    def _make_release_tracker(obj: FakeGLObject) -> Any:
        def _release() -> None:
            obj.release_count += 1
            obj.released = True
        return _release


# ──────────────────────────────────────────────────────────────────────────
# 模块级纯函数
# ──────────────────────────────────────────────────────────────────────────

class TestSrgbToLinearChannel:
    """_srgb_to_linear_channel：sRGB→scene-linear 精确数学。"""

    def test_endpoints(self) -> None:
        assert _srgb_to_linear_channel(0.0) == 0.0
        assert _srgb_to_linear_channel(1.0) == pytest.approx(1.0)

    def test_linear_segment_below_threshold(self) -> None:
        # 0.04045 是分段边界：v/12.92
        assert _srgb_to_linear_channel(0.04045) == pytest.approx(0.04045 / 12.92)
        assert _srgb_to_linear_channel(0.02) == pytest.approx(0.02 / 12.92)

    def test_power_segment_above_threshold(self) -> None:
        expected = ((0.5 + 0.055) / 1.055) ** 2.4
        assert _srgb_to_linear_channel(0.5) == pytest.approx(expected)
        assert _srgb_to_linear_channel(0.5) < 0.5  # 线性比 sRGB 暗

    def test_clamps_out_of_range(self) -> None:
        assert _srgb_to_linear_channel(2.0) == pytest.approx(1.0)
        assert _srgb_to_linear_channel(-0.5) == 0.0

    def test_monotonic(self) -> None:
        values = [i / 20.0 for i in range(21)]
        converted = [_srgb_to_linear_channel(v) for v in values]
        assert all(a <= b for a, b in zip(converted, converted[1:]))


# ──────────────────────────────────────────────────────────────────────────
# 参数 schema / 端口 / 初始状态
# ──────────────────────────────────────────────────────────────────────────

class TestNodeSchema:
    """PARAM_DEFS schema、端口拓扑与懒建 GL 字段初始值。"""

    def test_param_defs_schema(self) -> None:
        defs: Dict[str, ParamDef] = {p.name: p for p in Scene3DNode.PARAM_DEFS}
        assert set(defs) == {"width", "height", "format_name"}

        width = defs["width"]
        assert width.ptype == "int"
        assert width.default == 1920
        assert width.min_val == 1
        assert width.max_val == 16384

        height = defs["height"]
        assert height.ptype == "int"
        assert height.default == 1080
        assert height.min_val == 1
        assert height.max_val == 16384

        fmt = defs["format_name"]
        assert fmt.ptype == "str"
        assert fmt.default == "Scene3D"
        assert fmt.hidden is True

    def test_param_defs_make_default_parameters(self) -> None:
        merged: Dict[str, Any] = {}
        for pdef in Scene3DNode.PARAM_DEFS:
            merged.update(pdef.make_default_parameters())
        node = Scene3DNode()
        assert merged == node.parameters

    def test_get_param_defs_matches_list(self) -> None:
        node = Scene3DNode()
        defs = node.get_param_defs()
        assert isinstance(defs, dict)
        assert set(defs) == {"width", "height", "format_name"}

    def test_class_metadata(self) -> None:
        assert Scene3DNode.node_type == "scene3d"
        assert Scene3DNode.node_title == "Scene3D"
        assert Scene3DNode.i18n_title_key == "nodes.scene3d.title"
        assert Scene3DNode.header_color == "#2d6a4f"
        assert "3d" in Scene3DNode.SEARCH_ALIASES
        assert Scene3DNode.DESCRIPTION
        assert "3d" in Scene3DNode.TAGS

    def test_single_scene_input_port(self) -> None:
        node = Scene3DNode("n1")
        assert len(node.inputs) == 1
        port = node.inputs[0]
        assert isinstance(port, Port)
        assert port.name == "in"
        assert port.direction == "input"
        assert port.data_type == "scene"
        assert port.max_connections == 8

    def test_output_ports_beauty_plus_exportable_aovs(self) -> None:
        node = Scene3DNode("n1")
        names = [p.name for p in node.outputs]
        assert names[0] == "display"
        expected = {"display"} | {
            p.value for p in AOV_EXPORTABLE_PASSES if p != AOVPass.BEAUTY}
        assert set(names) == expected
        assert len(names) == len(set(names))  # 无重复端口
        for port in node.outputs:
            assert port.direction == "output"
            assert port.data_type == "image"
        # beauty 不重复暴露（display 即 beauty）
        assert AOVPass.BEAUTY.value not in names[1:]

    def test_default_parameters(self) -> None:
        node = Scene3DNode()
        assert node.parameters["width"] == 1920
        assert node.parameters["height"] == 1080
        assert node.parameters["format_name"] == "Scene3D"

    def test_lazy_gl_fields_start_empty(self) -> None:
        node = Scene3DNode()
        gl_fields = (
            "_mesh_prog", "_mesh_vbo", "_mesh_ibo", "_mesh_vao",
            "_mesh_skinned_prog", "_model_root", "_model_path_cached",
            "_instance_vbo", "_depth_texture", "_depth_write_prog",
            "_depth_fbo", "_shadow_texture", "_shadow_fbo",
            "_aov_prog",
            "_id_texture", "_id_fbo", "_id_write_prog",
            "_gpu_texture", "_render_fbo",
            "_msaa_color", "_resolve_fbo", "_nav_render_color",
            "_nav_render_fbo", "_gizmo", "_hdri_baker",
            "_last_content_sig", "_last_scene_content_sig",
        )
        for field in gl_fields:
            assert getattr(node, field) is None, f"{field} 应初始为 None"
        assert node._aov_textures == {}
        assert node._aov_fbos == {}
        assert node._aov_depth_rbs == {}
        assert node._depth_write_vaos == {}
        assert node._id_write_vaos == {}
        assert node._outline_vaos == {}
        assert node._lod_vao_cache == {}
        assert node._revision == 0
        assert node._last_size == (0, 0)
        assert node._render_target_w == 0
        assert node._render_target_h == 0
        assert node._instance_count_cached == 0
        assert node._shadow_map_allocated_size == 0
        assert node._shadow_map_size == 1024

    def test_material_id_allocator_initialized(self) -> None:
        node = Scene3DNode()
        assert isinstance(node._material_id_allocator, IDAllocator)
        assert node._material_id_allocator.count() == 0

    def test_light_fallback_defaults(self) -> None:
        node = Scene3DNode()
        assert node._light_types_t == (0, 0, 0, 0)
        assert node._ambient_color == (0.0, 0.0, 0.0)
        assert node._ambient_intensity == 0.0
        assert node._cam_exposure == 0.0

    def test_motion_history_starts_empty(self) -> None:
        node = Scene3DNode()
        assert node._motion_prev_proj is None
        assert node._motion_prev_view is None
        assert node._motion_prev_frame is None
        assert node._motion_prev_scene_key is None
        assert node._motion_prev_models == {}
        assert node._motion_prev_anim_worlds == {}


# ──────────────────────────────────────────────────────────────────────────
# 序列化往返 / 畸形输入
# ──────────────────────────────────────────────────────────────────────────

class TestSerialization:
    """to_dict/from_dict 往返 + 默认值合并 + 畸形输入容忍。"""

    def test_roundtrip_preserves_parameters_and_type(self) -> None:
        node = Scene3DNode("scene-1")
        node.parameters["width"] = 640
        node.parameters["height"] = 360
        data = node.to_dict()
        assert data["type"] == "scene3d"
        assert data["id"] == "scene-1"

        restored = Scene3DNode.from_dict(data)
        assert restored.node_type == "scene3d"
        assert restored.parameters["width"] == 640
        assert restored.parameters["height"] == 360
        assert restored.parameters["format_name"] == "Scene3D"

    def test_roundtrip_preserves_position(self) -> None:
        node = Scene3DNode()
        node.position = (120.5, -40.0)
        restored = Scene3DNode.from_dict(node.to_dict())
        assert restored.position == (120.5, -40.0)

    def test_from_dict_merges_defaults_for_missing_params(self) -> None:
        restored = Scene3DNode.from_dict({
            "id": "n2",
            "parameters": {"width": 128},
        })
        assert restored.parameters["width"] == 128
        # 缺失参数回落到默认值（新参数兼容旧工程）
        assert restored.parameters["height"] == 1080
        assert restored.parameters["format_name"] == "Scene3D"

    def test_from_dict_tolerates_empty_and_minimal_payload(self) -> None:
        node = Scene3DNode.from_dict({})
        assert node.parameters["width"] == 1920
        assert node.disabled is False
        assert node.position == (0, 0)

    def test_from_dict_restores_disabled_flag(self) -> None:
        node = Scene3DNode.from_dict({"disabled": True})
        assert node.disabled is True
        assert node.to_dict()["disabled"] is True

    def test_from_dict_preserves_unknown_fields(self) -> None:
        node = Scene3DNode.from_dict({
            "id": "n3",
            "future_field": {"inner": 7},
        })
        assert node._unknown_fields.get("future_field") == {"inner": 7}
        assert node.to_dict()["future_field"] == {"inner": 7}

    def test_to_dict_returns_deep_copy(self) -> None:
        node = Scene3DNode()
        data = node.to_dict()
        data["parameters"]["width"] = 1
        data["parameters"]["new_key"] = "x"
        assert node.parameters["width"] == 1920
        assert "new_key" not in node.parameters

    def test_id_autogenerated_when_missing(self) -> None:
        node = Scene3DNode()
        assert node.id
        assert Scene3DNode.from_dict({}).id


# ──────────────────────────────────────────────────────────────────────────
# ROI 契约（继承默认行为在 scene3d 端口拓扑上的表现）
# ──────────────────────────────────────────────────────────────────────────

class TestBboxRoiContract:
    """compute_output_bbox / compute_input_roi 在 Scene3D 端口上的默认契约。"""

    def test_compute_output_bbox_passes_through_first_input(self) -> None:
        node = Scene3DNode()
        bbox = BBox(x=4, y=8, width=16, height=12)
        result = node.compute_output_bbox({"in": bbox}, node.parameters)
        assert result == bbox

    def test_compute_output_bbox_empty_inputs_returns_none(self) -> None:
        node = Scene3DNode()
        assert node.compute_output_bbox({}, node.parameters) is None

    def test_compute_input_roi_covers_single_scene_port(self) -> None:
        node = Scene3DNode()
        roi = BBox(x=0, y=0, width=32, height=32)
        result = node.compute_input_roi(roi, node.parameters)
        assert set(result) == {"in"}
        assert result["in"] == roi


# ──────────────────────────────────────────────────────────────────────────
# 拾取 id 编码与映射
# ──────────────────────────────────────────────────────────────────────────

class TestEncodePickId:
    """_encode_pick_id：pick_id → 归一化 RGBA。"""

    def test_background_zero(self) -> None:
        assert Scene3DNode._encode_pick_id(0) == (0.0, 0.0, 0.0, 0.0)

    def test_byte_decomposition(self) -> None:
        # 0x010203 → R=3, G=2, B=1, A=1
        r, g, b, a = Scene3DNode._encode_pick_id(0x010203)
        assert r == pytest.approx(3 / 255.0)
        assert g == pytest.approx(2 / 255.0)
        assert b == pytest.approx(1 / 255.0)
        assert a == 1.0

    def test_roundtrip_decode(self) -> None:
        for pick_id in (1, 2, 255, 256, 65535, 65536, 0xABCDEF):
            r, g, b, a = Scene3DNode._encode_pick_id(pick_id)
            decoded = (round(r * 255)
                       | (round(g * 255) << 8)
                       | (round(b * 255) << 16))
            assert decoded == pick_id
            assert a == 1.0


class TestBuildPickIdMap:
    """_build_pick_id_map：按可见 node_id 分配 pick_id（1 起，0=背景）。"""

    def test_empty_payloads(self) -> None:
        pick_map, node_pick_ids = Scene3DNode()._build_pick_id_map([], [])
        assert pick_map == {}
        assert node_pick_ids == {}

    def test_model_mesh_ids_assigned_sorted(self) -> None:
        payload = SimpleNamespace(
            node_id="model-1",
            hidden_mesh_ids=(),
            flat_meshes=[
                {"node_id": "mesh-b", "node_name": "B"},
                {"node_id": "mesh-a", "node_name": "A"},
            ],
        )
        pick_map, node_pick_ids = Scene3DNode()._build_pick_id_map([payload], [])
        # sorted：mesh-a 先分配 → id 1
        assert node_pick_ids == {"mesh-a": 1, "mesh-b": 2}
        assert pick_map == {1: "mesh-a", 2: "mesh-b"}

    def test_hidden_mesh_ids_excluded(self) -> None:
        payload = SimpleNamespace(
            node_id="model-1",
            hidden_mesh_ids=("mesh-a",),
            flat_meshes=[
                {"node_id": "mesh-a", "node_name": "A"},
                {"node_id": "mesh-b", "node_name": "B"},
            ],
        )
        _, node_pick_ids = Scene3DNode()._build_pick_id_map([payload], [])
        assert "mesh-a" not in node_pick_ids
        assert node_pick_ids["mesh-b"] == 1

    def test_model_without_meshes_falls_back_to_payload_node_id(self) -> None:
        payload = SimpleNamespace(
            node_id="model-1", hidden_mesh_ids=(), flat_meshes=())
        _, node_pick_ids = Scene3DNode()._build_pick_id_map([payload], [])
        assert node_pick_ids == {"model-1": 1}

    def test_model_mesh_without_node_id_uses_payload_id(self) -> None:
        payload = SimpleNamespace(
            node_id="model-9", hidden_mesh_ids=(),
            flat_meshes=[{"node_id": "", "node_name": "anon"}])
        _, node_pick_ids = Scene3DNode()._build_pick_id_map([payload], [])
        assert node_pick_ids == {"model-9": 1}

    def test_geo_ids_assigned_after_models_without_duplicates(self) -> None:
        model = SimpleNamespace(
            node_id="model-1", hidden_mesh_ids=(),
            flat_meshes=[{"node_id": "mesh-a", "node_name": "A"}])
        geo_a = SimpleNamespace(node_id="geo-a")
        geo_dup = SimpleNamespace(node_id="mesh-a")  # 与 model mesh 同 id → 不重复分配
        pick_map, node_pick_ids = Scene3DNode()._build_pick_id_map(
            [model], [geo_a, geo_dup])
        assert node_pick_ids == {"mesh-a": 1, "geo-a": 2}
        assert pick_map[2] == "geo-a"

    def test_geo_without_node_id_skipped(self) -> None:
        geo = SimpleNamespace(node_id="")
        _, node_pick_ids = Scene3DNode()._build_pick_id_map([], [geo])
        assert node_pick_ids == {}

    def test_maps_are_inverse(self) -> None:
        model = SimpleNamespace(
            node_id="m", hidden_mesh_ids=(),
            flat_meshes=[{"node_id": "a"}, {"node_id": "b"}])
        geo = SimpleNamespace(node_id="c")
        pick_map, node_pick_ids = Scene3DNode()._build_pick_id_map([model], [geo])
        for nid, pid in node_pick_ids.items():
            assert pick_map[pid] == nid
        assert set(node_pick_ids.values()) == {1, 2, 3}
        assert 0 not in pick_map  # 0 保留给背景


class TestBuildComponentPickMeshes:
    """_build_component_pick_meshes：CPU 几何快照（BVH/组件拾取用）。"""

    @staticmethod
    def _make_mesh() -> MeshData:
        return get_primitive("cube")

    @staticmethod
    def _make_model_payload(hidden: Tuple[str, ...] = ()) -> SimpleNamespace:
        return SimpleNamespace(node_id="model-1", hidden_mesh_ids=hidden)

    @staticmethod
    def _make_flat_mesh(node_id: str, world: Optional[np.ndarray]) -> dict:
        return {
            "node_id": node_id,
            "node_name": f"node-{node_id}",
            "mesh": TestBuildComponentPickMeshes._make_mesh(),
            "world_matrix": world,
        }

    def test_model_meshes_built_with_model_world_composition(self) -> None:
        node = Scene3DNode()
        world = np.eye(4, dtype=np.float32)
        world[2, 3] = 3.0  # z 平移
        model = np.eye(4, dtype=np.float32)
        model[0, 3] = 10.0
        flat = [self._make_flat_mesh("mesh-a", world)]
        meshes = node._build_component_pick_meshes(
            model, True, flat, {}, [self._make_model_payload()], [])
        assert len(meshes) == 1
        entry = meshes[0]
        assert entry["node_id"] == "mesh-a"
        assert entry["mesh_key"] == "model:mesh-a:node-mesh-a"
        assert entry["pick_id"] == 0  # 未传 node_pick_ids → 0
        assert entry["is_skinned"] is False
        expected = model @ world
        np.testing.assert_allclose(entry["model_matrix"], expected, atol=1e-6)
        # 顶点/索引直接引用 payload 的 MeshData（零拷贝）
        assert entry["vertices"] is flat[0]["mesh"].vertices
        assert entry["indices"] is flat[0]["mesh"].indices

    def test_hidden_and_missing_world_entries_skipped(self) -> None:
        node = Scene3DNode()
        flat = [
            self._make_flat_mesh("mesh-hidden", np.eye(4, dtype=np.float32)),
            self._make_flat_mesh("mesh-noworld", None),
            self._make_flat_mesh("mesh-ok", np.eye(4, dtype=np.float32)),
        ]
        meshes = node._build_component_pick_meshes(
            np.eye(4, dtype=np.float32), True, flat, {},
            [self._make_model_payload(hidden=("mesh-hidden",))], [])
        ids = [m["node_id"] for m in meshes]
        assert "mesh-hidden" not in ids
        assert "mesh-noworld" not in ids
        assert ids == ["mesh-ok"]

    def test_isolate_mode_filters_model_meshes(self) -> None:
        node = Scene3DNode()
        node._p7_isolate_nid = "mesh-b"
        flat = [
            self._make_flat_mesh("mesh-a", np.eye(4, dtype=np.float32)),
            self._make_flat_mesh("mesh-b", np.eye(4, dtype=np.float32)),
        ]
        meshes = node._build_component_pick_meshes(
            np.eye(4, dtype=np.float32), True, flat, {},
            [self._make_model_payload()], [])
        assert [m["node_id"] for m in meshes] == ["mesh-b"]

    def test_isolate_mode_matching_payload_keeps_all_meshes(self) -> None:
        node = Scene3DNode()
        node._p7_isolate_nid = "model-1"  # 隔离整个模型节点
        flat = [
            self._make_flat_mesh("mesh-a", np.eye(4, dtype=np.float32)),
            self._make_flat_mesh("mesh-b", np.eye(4, dtype=np.float32)),
        ]
        meshes = node._build_component_pick_meshes(
            np.eye(4, dtype=np.float32), True, flat, {},
            [self._make_model_payload()], [])
        assert len(meshes) == 2

    def test_pick_id_map_applied_to_model_entries(self) -> None:
        node = Scene3DNode()
        flat = [self._make_flat_mesh("mesh-a", np.eye(4, dtype=np.float32))]
        meshes = node._build_component_pick_meshes(
            np.eye(4, dtype=np.float32), True, flat, {},
            [self._make_model_payload()], [],
            node_pick_ids={"mesh-a": 7})
        assert meshes[0]["pick_id"] == 7

    def test_geo_payload_single_instance(self) -> None:
        node = Scene3DNode()
        geo = SimpleNamespace(
            node_id="geo-1",
            mesh=self._make_mesh(),
            is_lines=False,
            model_matrix=np.eye(4, dtype=np.float32),
            instance_enabled=False,
            instance_offsets=None,
            content_revision=3,
        )
        meshes = node._build_component_pick_meshes(
            np.eye(4, dtype=np.float32), False, [], {}, [], [geo])
        assert len(meshes) == 1
        entry = meshes[0]
        assert entry["node_id"] == "geo-1"
        assert entry["mesh_key"] == "geo:geo-1:0"
        assert entry["instance_index"] == 0
        assert entry["revision"][0] == 3
        np.testing.assert_allclose(entry["model_matrix"], np.eye(4), atol=1e-6)

    def test_geo_payload_instanced_expands_per_instance(self) -> None:
        node = Scene3DNode()
        base = np.eye(4, dtype=np.float32)
        geo = SimpleNamespace(
            node_id="geo-2",
            mesh=self._make_mesh(),
            is_lines=False,
            model_matrix=base,
            instance_enabled=True,
            instance_offsets=[(0.0, 0.0, 0.0), (2.0, 0.0, 0.0)],
            content_revision=1,
        )
        meshes = node._build_component_pick_meshes(
            np.eye(4, dtype=np.float32), False, [], {}, [], [geo],
            node_pick_ids={"geo-2": 4})
        assert len(meshes) == 2
        assert [m["instance_index"] for m in meshes] == [0, 1]
        assert all(m["pick_id"] == 4 for m in meshes)
        # 第二个实例矩阵带 x=2 平移
        np.testing.assert_allclose(meshes[1]["model_matrix"][0, 3], 2.0, atol=1e-6)
        np.testing.assert_allclose(meshes[0]["model_matrix"][0, 3], 0.0, atol=1e-6)

    def test_geo_lines_and_missing_node_id_skipped(self) -> None:
        node = Scene3DNode()
        lines_geo = SimpleNamespace(
            node_id="geo-lines", mesh=self._make_mesh(), is_lines=True,
            model_matrix=np.eye(4, dtype=np.float32),
            instance_enabled=False, instance_offsets=None, content_revision=0)
        no_id_geo = SimpleNamespace(
            node_id="", mesh=self._make_mesh(), is_lines=False,
            model_matrix=np.eye(4, dtype=np.float32),
            instance_enabled=False, instance_offsets=None, content_revision=0)
        meshes = node._build_component_pick_meshes(
            np.eye(4, dtype=np.float32), False, [], {}, [],
            [lines_geo, no_id_geo])
        assert meshes == []

    def test_import_not_ok_skips_model_path(self) -> None:
        node = Scene3DNode()
        flat = [self._make_flat_mesh("mesh-a", np.eye(4, dtype=np.float32))]
        meshes = node._build_component_pick_meshes(
            np.eye(4, dtype=np.float32), False, flat, {},
            [self._make_model_payload()], [])
        assert meshes == []


# ──────────────────────────────────────────────────────────────────────────
# 实例化偏移范围
# ──────────────────────────────────────────────────────────────────────────

class TestInstanceOffsetRange:
    """_instance_offset_range：网格居中实例偏移 min/max。"""

    def test_symmetric_grid(self) -> None:
        lo, hi = Scene3DNode._instance_offset_range(3, 3, 2.0)
        assert lo == (-2.0, 0.0, -2.0)
        assert hi == (2.0, 0.0, 2.0)

    def test_single_instance_zero_range(self) -> None:
        lo, hi = Scene3DNode._instance_offset_range(1, 1, 5.0)
        assert lo == (0.0, 0.0, 0.0)
        assert hi == (0.0, 0.0, 0.0)

    def test_rectangular_grid(self) -> None:
        lo, hi = Scene3DNode._instance_offset_range(2, 5, 1.5)
        assert lo == (-0.75, 0.0, -3.0)
        assert hi == (0.75, 0.0, 3.0)

    def test_y_always_zero(self) -> None:
        for gx in (1, 2, 7):
            for gz in (1, 3, 9):
                lo, hi = Scene3DNode._instance_offset_range(gx, gz, 2.0)
                assert lo[1] == 0.0 and hi[1] == 0.0


# ──────────────────────────────────────────────────────────────────────────
# 矩阵数学：阴影灯光相机 / 单视图正交相机
# ──────────────────────────────────────────────────────────────────────────

class TestShadowLightViewProj:
    """_shadow_light_view_proj：方向光正交 shadow 相机矩阵。"""

    def test_shape_and_dtype(self) -> None:
        mvp = Scene3DNode._shadow_light_view_proj((0.0, 1.0, 0.0), (0, 0, 0))
        assert isinstance(mvp, np.ndarray)
        assert mvp.shape == (4, 4)
        assert mvp.dtype == np.float32
        assert np.all(np.isfinite(mvp))

    def test_target_projects_to_center(self) -> None:
        target = (1.0, 2.0, 3.0)
        mvp = Scene3DNode._shadow_light_view_proj((0.0, 1.0, 0.0), target)
        clip = mvp @ np.array([target[0], target[1], target[2], 1.0],
                              dtype=np.float32)
        # 目标点应投影到光相机视口中心（x/y ≈ 0），z 在裁剪范围内
        assert abs(float(clip[0])) < 1e-4
        assert abs(float(clip[1])) < 1e-4
        assert -1.0 - 1e-4 <= float(clip[2]) <= 1.0 + 1e-4

    def test_zero_direction_falls_back_to_up(self) -> None:
        zero = Scene3DNode._shadow_light_view_proj((0.0, 0.0, 0.0), (4, 5, 6))
        up = Scene3DNode._shadow_light_view_proj((0.0, 1.0, 0.0), (4, 5, 6))
        np.testing.assert_allclose(zero, up, atol=1e-6)

    def test_tiny_direction_falls_back_to_up(self) -> None:
        tiny = Scene3DNode._shadow_light_view_proj((1e-9, 0.0, 0.0), (0, 0, 0))
        up = Scene3DNode._shadow_light_view_proj((0.0, 1.0, 0.0), (0, 0, 0))
        np.testing.assert_allclose(tiny, up, atol=1e-6)

    def test_deterministic(self) -> None:
        a = Scene3DNode._shadow_light_view_proj((-0.6, 0.7, 0.4), (0, 0, 0))
        b = Scene3DNode._shadow_light_view_proj((-0.6, 0.7, 0.4), (0, 0, 0))
        np.testing.assert_array_equal(a, b)

    def test_unnormalized_direction_accepted(self) -> None:
        # 未归一化方向应内部归一化：同方向不同模长 → 相同矩阵
        short = Scene3DNode._shadow_light_view_proj((0.0, 1.0, 0.0), (0, 0, 0))
        long = Scene3DNode._shadow_light_view_proj((0.0, 100.0, 0.0), (0, 0, 0))
        np.testing.assert_allclose(short, long, atol=1e-5)


class TestSingleOrthoCamera:
    """_single_ortho_camera：top/right/front 单视图正交相机。"""

    @staticmethod
    def _camera(node: Scene3DNode, kind: int,
                target: Tuple[float, float, float]) -> Tuple[Any, Any, Any]:
        return node._single_ortho_camera(kind, target, 5.0, 1.0)

    def test_top_camera_eye_and_up(self) -> None:
        node = Scene3DNode()
        proj, view, eye = self._camera(node, 1, (1.0, 2.0, 3.0))
        np.testing.assert_allclose(eye, (1.0, 502.0, 3.0), atol=1e-4)
        # view 把 eye 变换到原点
        eye_h = np.array([eye[0], eye[1], eye[2], 1.0], dtype=np.float32)
        transformed = view @ eye_h
        np.testing.assert_allclose(transformed[:3], (0, 0, 0), atol=1e-3)

    def test_right_camera_eye(self) -> None:
        node = Scene3DNode()
        _, _, eye = self._camera(node, 2, (0.0, 0.0, 0.0))
        np.testing.assert_allclose(eye, (500.0, 0.0, 0.0), atol=1e-4)

    def test_front_camera_eye(self) -> None:
        node = Scene3DNode()
        _, _, eye = self._camera(node, 3, (2.0, 0.0, -1.0))
        np.testing.assert_allclose(eye, (2.0, 0.0, 499.0), atol=1e-4)

    def test_projection_is_orthographic(self) -> None:
        node = Scene3DNode()
        proj, _, _ = node._single_ortho_camera(1, (0, 0, 0), 5.0, 1.5)
        # 正交投影：w 分量恒为 1（无透视除法缩放）
        for z in (0.0, 10.0, 100.0):
            point = np.array([1.0, 1.0, z, 1.0], dtype=np.float32)
            clip = proj @ point
            assert clip[3] == pytest.approx(1.0)

    def test_all_view_kinds_return_valid_matrices(self) -> None:
        node = Scene3DNode()
        for kind in (1, 2, 3):
            proj, view, eye = self._camera(node, kind, (0, 0, 0))
            assert proj.shape == (4, 4)
            assert view.shape == (4, 4)
            assert len(eye) == 3
            assert np.all(np.isfinite(proj))
            assert np.all(np.isfinite(view))


# ──────────────────────────────────────────────────────────────────────────
# LOD / quality profile 决策
# ──────────────────────────────────────────────────────────────────────────

class TestResolveQualityProfile:
    """_resolve_quality_profile：FINAL/PREVIEW/NAVIGATION/FAST_SOLID 决策树。"""

    def test_full_render_mode_forces_final(self) -> None:
        ctx = SimpleNamespace(interaction_active=True, quality_profile="")
        assert (Scene3DNode._resolve_quality_profile(ctx, "full", None)
                is ViewportQualityProfile.FINAL)
        assert (Scene3DNode._resolve_quality_profile(ctx, "export", None)
                is ViewportQualityProfile.FINAL)

    def test_none_ctx_falls_back_to_preview(self) -> None:
        assert (Scene3DNode._resolve_quality_profile(None, "preview", None)
                is ViewportQualityProfile.PREVIEW)

    def test_idle_interaction_returns_preview(self) -> None:
        ctx = SimpleNamespace(interaction_active=False, quality_profile="")
        assert (Scene3DNode._resolve_quality_profile(ctx, "preview", None)
                is ViewportQualityProfile.PREVIEW)

    def test_navigation_when_interacting_without_large_model(self) -> None:
        ctx = SimpleNamespace(interaction_active=True, quality_profile="")
        assert (Scene3DNode._resolve_quality_profile(ctx, "preview", None)
                is ViewportQualityProfile.NAVIGATION)

    def test_fast_solid_when_model_exceeds_threshold(self) -> None:
        ctx = SimpleNamespace(interaction_active=True, quality_profile="")
        big = SimpleNamespace(
            has_model=True,
            flat_meshes=[{"index_count": LARGE_MODEL_TRIANGLE_THRESHOLD * 3}],
        )
        assert (Scene3DNode._resolve_quality_profile(ctx, "preview", [big])
                is ViewportQualityProfile.FAST_SOLID)

    def test_navigation_when_model_below_threshold(self) -> None:
        ctx = SimpleNamespace(interaction_active=True, quality_profile="")
        small = SimpleNamespace(
            has_model=True,
            flat_meshes=[{"index_count": (LARGE_MODEL_TRIANGLE_THRESHOLD - 1) * 3}],
        )
        assert (Scene3DNode._resolve_quality_profile(ctx, "preview", [small])
                is ViewportQualityProfile.NAVIGATION)

    def test_multiple_meshes_triangles_summed(self) -> None:
        ctx = SimpleNamespace(interaction_active=True, quality_profile="")
        half = LARGE_MODEL_TRIANGLE_THRESHOLD // 2
        payloads = [SimpleNamespace(
            has_model=True,
            flat_meshes=[{"index_count": half * 3}, {"index_count": half * 3}],
        )]
        assert (Scene3DNode._resolve_quality_profile(ctx, "preview", payloads)
                is ViewportQualityProfile.FAST_SOLID)

    def test_model_without_has_model_ignored(self) -> None:
        ctx = SimpleNamespace(interaction_active=True, quality_profile="")
        payloads = [SimpleNamespace(
            has_model=False,
            flat_meshes=[{"index_count": LARGE_MODEL_TRIANGLE_THRESHOLD * 10}],
        )]
        assert (Scene3DNode._resolve_quality_profile(ctx, "preview", payloads)
                is ViewportQualityProfile.NAVIGATION)

    def test_draft_profile_maps_to_navigation(self) -> None:
        ctx = SimpleNamespace(interaction_active=False, quality_profile="draft")
        assert (Scene3DNode._resolve_quality_profile(ctx, "preview", None)
                is ViewportQualityProfile.NAVIGATION)


# ──────────────────────────────────────────────────────────────────────────
# 材质排序与渲染角色谓词
# ──────────────────────────────────────────────────────────────────────────

class TestSortMeshesByMaterial:
    """_sort_meshes_by_material：不透明按材质分组，透明保持原序殿后。"""

    @staticmethod
    def _mesh(tag: str, material: Any) -> dict:
        return {"tag": tag, "material": material}

    def test_small_lists_returned_unchanged(self) -> None:
        meshes = [self._mesh("a", None), self._mesh("b", None)]
        assert Scene3DNode._sort_meshes_by_material(meshes) is meshes

    def test_opaque_grouped_by_material_stably(self) -> None:
        mat_a = SimpleNamespace(alpha=1.0)
        mat_b = SimpleNamespace(alpha=1.0)
        meshes = [
            self._mesh("a1", mat_a),
            self._mesh("b1", mat_b),
            self._mesh("a2", mat_a),
            self._mesh("b2", mat_b),
        ]
        result = Scene3DNode._sort_meshes_by_material(meshes)
        tags = [m["tag"] for m in result]
        assert sorted(tags) == ["a1", "a2", "b1", "b2"]
        # 同材质相邻（分组），组内保持原始相对顺序（稳定排序）
        assert abs(tags.index("a1") - tags.index("a2")) == 1
        assert abs(tags.index("b1") - tags.index("b2")) == 1
        assert tags.index("a1") < tags.index("a2")
        assert tags.index("b1") < tags.index("b2")

    def test_transparent_kept_at_end_in_original_order(self) -> None:
        mat_a = SimpleNamespace(alpha=1.0)
        mat_b = SimpleNamespace(alpha=1.0)
        mat_t1 = SimpleNamespace(alpha=0.5)
        mat_t2 = SimpleNamespace(alpha=0.3)
        meshes = [
            self._mesh("t1", mat_t1),
            self._mesh("a1", mat_a),
            self._mesh("t2", mat_t2),
            self._mesh("b1", mat_b),
        ]
        result = Scene3DNode._sort_meshes_by_material(meshes)
        tags = [m["tag"] for m in result]
        # 不透明在前（分组），透明殿后且保持原相对顺序 t1 在 t2 前
        assert set(tags[:2]) == {"a1", "b1"}
        assert set(tags[2:]) == {"t1", "t2"}
        assert tags.index("t1") < tags.index("t2")

    def test_all_transparent_returns_original_order(self) -> None:
        meshes = [
            self._mesh("t1", SimpleNamespace(alpha=0.2)),
            self._mesh("t2", SimpleNamespace(alpha=0.4)),
            self._mesh("t3", SimpleNamespace(alpha=0.6)),
        ]
        result = Scene3DNode._sort_meshes_by_material(meshes)
        assert [m["tag"] for m in result] == ["t1", "t2", "t3"]

    def test_missing_material_grouped_together(self) -> None:
        mat = SimpleNamespace(alpha=1.0)
        meshes = [
            self._mesh("none1", None),
            self._mesh("mat1", mat),
            self._mesh("none2", None),
            self._mesh("mat2", mat),
        ]
        result = Scene3DNode._sort_meshes_by_material(meshes)
        tags = [m["tag"] for m in result]
        # none（id(None) 相同）也聚为一组；两组相邻
        assert abs(tags.index("none1") - tags.index("none2")) == 1
        assert abs(tags.index("mat1") - tags.index("mat2")) == 1

    def test_invalid_alpha_treated_opaque(self) -> None:
        bad = SimpleNamespace(alpha="not-a-number")
        meshes = [
            self._mesh("bad", bad),
            self._mesh("x1", None),
            self._mesh("x2", None),
        ]
        result = Scene3DNode._sort_meshes_by_material(meshes)
        assert sorted(m["tag"] for m in result) == ["bad", "x1", "x2"]


class TestRenderFlagPredicates:
    """_is_beauty_visible / _has_holdout / _can_cast_shadow 谓词组合。"""

    def test_none_flags_default_visible(self) -> None:
        assert Scene3DNode._is_beauty_visible(None) is True
        assert Scene3DNode._has_holdout(None) is False
        assert Scene3DNode._can_cast_shadow(None) is True

    def test_plain_object(self) -> None:
        flags = SimpleNamespace(
            visible_to_camera=True, holdout=False,
            shadow_catcher=False, matte_object=False,
            visible_to_shadow=True)
        assert Scene3DNode._is_beauty_visible(flags) is True
        assert Scene3DNode._has_holdout(flags) is False
        assert Scene3DNode._can_cast_shadow(flags) is True

    def test_holdout(self) -> None:
        flags = SimpleNamespace(
            visible_to_camera=True, holdout=True,
            shadow_catcher=False, matte_object=False,
            visible_to_shadow=True)
        assert Scene3DNode._is_beauty_visible(flags) is False
        assert Scene3DNode._has_holdout(flags) is True
        # holdout 仍投影
        assert Scene3DNode._can_cast_shadow(flags) is True

    def test_shadow_catcher(self) -> None:
        flags = SimpleNamespace(
            visible_to_camera=True, holdout=False,
            shadow_catcher=True, matte_object=False,
            visible_to_shadow=True)
        assert Scene3DNode._is_beauty_visible(flags) is False
        assert Scene3DNode._can_cast_shadow(flags) is False

    def test_matte_object(self) -> None:
        flags = SimpleNamespace(
            visible_to_camera=True, holdout=False,
            shadow_catcher=False, matte_object=True,
            visible_to_shadow=True)
        assert Scene3DNode._is_beauty_visible(flags) is False
        assert Scene3DNode._can_cast_shadow(flags) is False

    def test_hidden_from_camera_still_casts_shadow(self) -> None:
        flags = SimpleNamespace(
            visible_to_camera=False, holdout=False,
            shadow_catcher=False, matte_object=False,
            visible_to_shadow=True)
        assert Scene3DNode._is_beauty_visible(flags) is False
        assert Scene3DNode._can_cast_shadow(flags) is True

    def test_hidden_from_shadow(self) -> None:
        flags = SimpleNamespace(
            visible_to_camera=True, holdout=False,
            shadow_catcher=False, matte_object=False,
            visible_to_shadow=False)
        assert Scene3DNode._is_beauty_visible(flags) is True
        assert Scene3DNode._can_cast_shadow(flags) is False


# ──────────────────────────────────────────────────────────────────────────
# Material ID AOV
# ──────────────────────────────────────────────────────────────────────────

class TestMaterialAovKey:
    """_material_aov_key / _material_aov_rgba：稳定材质键 + id 编码。"""

    def test_explicit_material_id_wins(self) -> None:
        key = Scene3DNode._material_aov_key(
            {"material_id": "explicit",
             "material": {"name": "ignored"}}, "fallback")
        assert key == "explicit"

    def test_dict_material_name_fallback(self) -> None:
        assert Scene3DNode._material_aov_key(
            {"material": {"name": "Wood"}}, "fb") == "Wood"
        assert Scene3DNode._material_aov_key(
            {"material": {"material_id": "wood-1"}}, "fb") == "wood-1"

    def test_object_material_name_fallback(self) -> None:
        mat = SimpleNamespace(name="Paint")
        assert Scene3DNode._material_aov_key({"material": mat}, "fb") == "Paint"
        mat2 = SimpleNamespace(material_id="m-9")
        assert Scene3DNode._material_aov_key({"material": mat2}, "fb") == "m-9"

    def test_fallback_used_when_nothing_else(self) -> None:
        assert Scene3DNode._material_aov_key({}, "") == "material"
        assert Scene3DNode._material_aov_key({}, "geo:3") == "geo:3"
        assert Scene3DNode._material_aov_key({"material": None}, "fb") == "fb"

    def test_rgba_stable_per_key(self) -> None:
        node = Scene3DNode()
        first = node._material_aov_rgba({"material_id": "steel"}, "f")
        again = node._material_aov_rgba({"material_id": "steel"}, "f")
        other = node._material_aov_rgba({"material_id": "wood"}, "f")
        assert first == again
        assert first != other

    def test_rgba_matches_allocator_ids(self) -> None:
        node = Scene3DNode()
        rgba_1 = node._material_aov_rgba({"material_id": "first"}, "f")
        rgba_2 = node._material_aov_rgba({"material_id": "second"}, "f")
        assert rgba_1 == encode_id_to_rgba(1)
        assert rgba_2 == encode_id_to_rgba(2)
        assert node._material_id_allocator.count() == 2


# ──────────────────────────────────────────────────────────────────────────
# 材质 override
# ──────────────────────────────────────────────────────────────────────────

class TestMaterialOverride:
    """_material_override_map / _material_with_override：JSON-safe 覆盖解码与应用。"""

    def test_override_map_none_payload(self) -> None:
        assert Scene3DNode._material_override_map(None) == {}

    def test_override_map_non_dict_raw(self) -> None:
        payload = SimpleNamespace(material_overrides=["not", "a", "dict"])
        assert Scene3DNode._material_override_map(payload) == {}

    def test_override_map_missing_attr(self) -> None:
        assert Scene3DNode._material_override_map(SimpleNamespace()) == {}

    def test_override_map_decodes_and_filters(self) -> None:
        payload = SimpleNamespace(material_overrides={
            "steel": {
                "target_material_id": "steel",
                "overrides": {"metallic": 0.9, "roughness": 0.2},
            },
            "bad": "not-a-dict",  # 非 dict 值被过滤
        })
        result = Scene3DNode._material_override_map(payload)
        assert set(result) == {"steel"}
        override = result["steel"]
        assert isinstance(override, MaterialOverride)
        assert override.overrides["metallic"] == 0.9

    def test_with_override_none_material(self) -> None:
        assert Scene3DNode._material_with_override(
            None, {"material_id": "x"}, {"x": MaterialOverride()}) is None

    def test_with_override_empty_map_returns_identity(self) -> None:
        mat = MaterialData(metallic=0.1)
        result = Scene3DNode._material_with_override(mat, {"node_id": "n"}, {})
        assert result is mat

    def test_with_override_identity_override_returns_same_object(self) -> None:
        mat = MaterialData(metallic=0.1)
        override = MaterialOverride(target_material_id="steel")
        assert override.is_identity() is True
        result = Scene3DNode._material_with_override(
            mat, {"material_id": "steel"}, {"steel": override})
        assert result is mat

    def test_with_override_applies_without_mutating_source(self) -> None:
        mat = MaterialData(
            base_color=(0.2, 0.4, 0.6), metallic=0.1, roughness=0.5,
            material_id="steel")
        override_map = Scene3DNode._material_override_map(SimpleNamespace(
            material_overrides={"steel": {
                "target_material_id": "steel",
                "overrides": {"metallic": 0.9, "roughness": 0.05},
            }}))
        result = Scene3DNode._material_with_override(
            mat, {"material_id": "steel"}, override_map)
        assert result is not mat
        assert result.metallic == pytest.approx(0.9)
        assert result.roughness == pytest.approx(0.05)
        # 未覆盖字段保留；原对象不变
        np.testing.assert_allclose(result.base_color, (0.2, 0.4, 0.6))
        assert mat.metallic == pytest.approx(0.1)
        assert mat.roughness == pytest.approx(0.5)

    def test_with_override_matches_by_material_name(self) -> None:
        mat = MaterialData(name="Wood", roughness=0.8)
        override = MaterialOverride(
            target_material_name="Wood",
            overrides={"roughness": 0.15})
        # map 的 key 与 material_id_for 不同 → 走 target_material_name 回退
        result = Scene3DNode._material_with_override(
            mat, {"node_id": "mesh-1"}, {"slot-x": override})
        assert result is not mat
        assert result.roughness == pytest.approx(0.15)

    def test_with_override_unmatched_returns_original(self) -> None:
        mat = MaterialData(name="Wood")
        override = MaterialOverride(
            target_material_name="Other",
            overrides={"roughness": 0.1})
        result = Scene3DNode._material_with_override(
            mat, {"node_id": "n"}, {"k": override})
        assert result is mat


# ──────────────────────────────────────────────────────────────────────────
# Cryptomatte manifest 元数据
# ──────────────────────────────────────────────────────────────────────────

class TestBuildCryptomatteMetadata:
    """_build_cryptomatte_metadata：manifest-only 对象 matte 描述。"""

    def test_no_objects_returns_none(self) -> None:
        assert Scene3DNode._build_cryptomatte_metadata(None, ()) is None
        empty = SimpleNamespace(flat_meshes=[])
        assert Scene3DNode._build_cryptomatte_metadata(empty, []) is None

    def test_model_mesh_names_and_ids(self) -> None:
        payload = SimpleNamespace(flat_meshes=[
            {"node_name": "body", "node_id": "m-body"},
            {"node_name": "", "node_id": ""},   # 双空 → 跳过
            "junk",                              # 非 dict → 跳过
        ])
        meta = Scene3DNode._build_cryptomatte_metadata(payload, ())
        assert meta is not None
        assert meta["status"] == "manifest_only"
        assert meta["aov_source"] == AOVPass.OBJECT_ID.value
        assert meta["layer"] == "crypto_object"
        assert meta["hash_algorithm"] == "MurmurHash3_32"
        assert meta["object_count"] == 1
        manifest = json.loads(meta["manifest"])
        assert "body" in manifest.values()

    def test_geo_payload_names(self) -> None:
        geos = [
            SimpleNamespace(node_id="geo-a"),
            SimpleNamespace(node_id=""),  # 无 id → "geo:1"
            None,                          # None → 跳过
        ]
        meta = Scene3DNode._build_cryptomatte_metadata(None, geos)
        assert meta is not None
        manifest = json.loads(meta["manifest"])
        names = set(manifest.values())
        assert "geo:geo-a" in names
        assert "geo:1" in names
        assert meta["object_count"] == 2

    def test_collision_reports_error_status(self) -> None:
        payload = SimpleNamespace(flat_meshes=[{"node_name": "a", "node_id": "x"}])
        with patch(
            "src.features.composite.nodes.scene3d_node"
            ".build_cryptomatte_manifest",
            side_effect=ValueError("Cryptomatte hash collision: 'a' and 'b'"),
        ):
            meta = Scene3DNode._build_cryptomatte_metadata(payload, ())
        assert meta is not None
        assert meta["status"] == "collision"
        assert "collision" in meta["error"]
        assert meta["layer"] == "crypto_object"
        assert meta["object_count"] == 1

    def test_model_and_geo_combined(self) -> None:
        payload = SimpleNamespace(flat_meshes=[{"node_name": "mesh", "node_id": "m"}])
        meta = Scene3DNode._build_cryptomatte_metadata(
            payload, [SimpleNamespace(node_id="g")])
        assert meta is not None
        assert meta["object_count"] == 2


# ──────────────────────────────────────────────────────────────────────────
# 共享 uniform 上传（fake program/texture）
# ──────────────────────────────────────────────────────────────────────────

class TestSetSharedUniforms:
    """_set_shared_uniforms：矩阵/灯光/IBL 上传 + P8-2 view 级缓存。"""

    @staticmethod
    def _call(node: Scene3DNode, prog: FakeProgram,
              stats: Optional[Viewport3DPerfStats] = None,
              **kwargs: Any) -> Tuple[FakeTexture, FakeTexture, FakeTexture]:
        irr = FakeTexture("irr")
        pre = FakeTexture("pre")
        lut = FakeTexture("lut")
        node._set_shared_uniforms(
            prog,
            np.eye(4, dtype=np.float32), np.eye(4, dtype=np.float32),
            (1.0, 2.0, 3.0),
            ((0.0, 1.0, 0.0),) * 4, ((1.0, 1.0, 1.0),) * 4, (2.0, 0.0, 0.0, 0.0),
            1,
            irr, pre, lut,
            1.0, 4.0,
            _stats=stats,
            **kwargs,
        )
        return irr, pre, lut

    def test_core_uniforms_uploaded(self) -> None:
        node = Scene3DNode()
        prog = FakeProgram()
        irr, pre, lut = self._call(node, prog)
        assert prog.value_of("u_cam_pos") == (1.0, 2.0, 3.0)
        assert prog.value_of("u_light_count") == 1
        assert prog.value_of("u_env_intensity") == 1.0
        assert prog.value_of("u_prefilter_max_lod") == 4.0
        # IBL sampler 指向单元 0/1/2
        assert prog.value_of("u_irradiance_map") == 0
        assert prog.value_of("u_prefilter_map") == 1
        assert prog.value_of("u_brdf_lut") == 2
        for tex, location in ((irr, 0), (pre, 1), (lut, 2)):
            assert tex.used_locations == [location]

    def test_light_tuple_fallbacks_from_instance(self) -> None:
        node = Scene3DNode()
        node._light_types_t = (1, 0, 0, 0)
        node._light_pos_t = ((5.0, 0.0, 0.0),) * 4
        node._light_range_t = (9.0, 0.0, 0.0, 0.0)
        node._ambient_color = (0.1, 0.2, 0.3)
        node._ambient_intensity = 1.0
        node._cam_exposure = 1.5
        prog = FakeProgram()
        self._call(node, prog)
        assert prog.value_of("u_light_type") == (1, 0, 0, 0)
        assert prog.value_of("u_light_pos") == ((5.0, 0.0, 0.0),) * 4
        assert prog.value_of("u_light_range") == (9.0, 0.0, 0.0, 0.0)
        assert prog.value_of("u_ambient_color") == (0.1, 0.2, 0.3)
        assert prog.value_of("u_ambient_intensity") == 1.0
        assert prog.value_of("u_exposure") == 1.5

    def test_explicit_light_args_override_instance_defaults(self) -> None:
        node = Scene3DNode()
        prog = FakeProgram()
        self._call(node, prog, light_types_t=(2, 0, 0, 0),
                   light_pos_t=((7.0, 0.0, 0.0),) * 4)
        assert prog.value_of("u_light_type") == (2, 0, 0, 0)
        assert prog.value_of("u_light_pos") == ((7.0, 0.0, 0.0),) * 4

    def test_stats_counts_22_uniforms_per_miss(self) -> None:
        node = Scene3DNode()
        prog = FakeProgram()
        stats = Viewport3DPerfStats()
        before = stats.uniform_sets
        self._call(node, prog, stats=stats)
        assert stats.uniform_sets == before + 22

    def test_view_cache_hit_skips_uniform_writes(self) -> None:
        node = Scene3DNode()
        node._shared_uniforms_view_sig = ("view-sig",)
        prog = FakeProgram()
        stats = Viewport3DPerfStats()

        irr, pre, lut = self._call(node, prog, stats=stats)
        assert stats.uniform_sets == 22
        first_values = {k: u.value for k, u in prog.uniforms.items()}
        for tex in (irr, pre, lut):
            tex.used_locations.clear()

        # 第二次同 (program, view_sig) → 缓存命中：无 uniform 写入，只重绑纹理
        irr, pre, lut = self._call(node, prog, stats=stats)
        assert stats.uniform_sets == 22  # 未增加
        after = {k: u.value for k, u in prog.uniforms.items()}
        assert after == first_values
        for tex, location in ((irr, 0), (pre, 1), (lut, 2)):
            assert tex.used_locations == [location]

    def test_view_cache_miss_on_different_program(self) -> None:
        node = Scene3DNode()
        node._shared_uniforms_view_sig = ("view-sig",)
        prog_a = FakeProgram()
        prog_b = FakeProgram()
        stats = Viewport3DPerfStats()
        self._call(node, prog_a, stats=stats)
        self._call(node, prog_b, stats=stats)
        assert stats.uniform_sets == 44  # 两个 program 各 miss 一次

    def test_no_view_sig_always_full_upload(self) -> None:
        node = Scene3DNode()
        assert node._shared_uniforms_view_sig is None
        prog = FakeProgram()
        stats = Viewport3DPerfStats()
        self._call(node, prog, stats=stats)
        self._call(node, prog, stats=stats)
        assert stats.uniform_sets == 44


# ──────────────────────────────────────────────────────────────────────────
# Shadow target 生命周期（mock ctx）
# ──────────────────────────────────────────────────────────────────────────

class TestShadowTargets:
    """_ensure_shadow_targets / _release_shadow_targets：分配/复用/重建/失败。"""

    def test_first_allocation_succeeds(self) -> None:
        node = Scene3DNode()
        ctx = FakeGLContext()
        assert node._ensure_shadow_targets(ctx) is True
        assert node._shadow_texture is not None
        assert node._shadow_depth_rb is not None
        assert node._shadow_fbo is not None
        assert node._shadow_map_allocated_size == 1024
        assert len(ctx.textures) == 1

    def test_min_size_clamped_to_256(self) -> None:
        node = Scene3DNode()
        node._shadow_map_size = 64
        ctx = FakeGLContext()
        assert node._ensure_shadow_targets(ctx) is True
        assert node._shadow_map_allocated_size == 256

    def test_same_size_reuses_targets(self) -> None:
        node = Scene3DNode()
        ctx = FakeGLContext()
        assert node._ensure_shadow_targets(ctx) is True
        texture_before = node._shadow_texture
        assert node._ensure_shadow_targets(ctx) is True  # 复用
        assert node._shadow_texture is texture_before
        assert len(ctx.textures) == 1
        assert len(ctx.framebuffers) == 1

    def test_size_change_rebuilds_and_releases_old(self) -> None:
        node = Scene3DNode()
        ctx = FakeGLContext()
        assert node._ensure_shadow_targets(ctx) is True
        old_texture = node._shadow_texture
        old_fbo = node._shadow_fbo
        old_rb = node._shadow_depth_rb

        node._shadow_map_size = 2048
        assert node._ensure_shadow_targets(ctx) is True
        assert node._shadow_map_allocated_size == 2048
        assert node._shadow_texture is not old_texture
        assert node._shadow_fbo is not old_fbo
        assert node._shadow_depth_rb is not old_rb
        assert old_texture.released is True
        assert old_fbo.released is True
        assert old_rb.released is True

    def test_allocation_failure_returns_false_and_resets(self) -> None:
        node = Scene3DNode()
        ctx = FakeGLContext()
        ctx.fail_texture = True
        assert node._ensure_shadow_targets(ctx) is False
        assert node._shadow_texture is None
        assert node._shadow_fbo is None
        assert node._shadow_depth_rb is None
        assert node._shadow_map_allocated_size == 0

    def test_release_resets_all_fields(self) -> None:
        node = Scene3DNode()
        ctx = FakeGLContext()
        assert node._ensure_shadow_targets(ctx) is True
        texture, fbo, rb = (node._shadow_texture, node._shadow_fbo,
                            node._shadow_depth_rb)
        node._release_shadow_targets()
        assert texture.released and fbo.released and rb.released
        assert node._shadow_texture is None
        assert node._shadow_fbo is None
        assert node._shadow_depth_rb is None
        assert node._shadow_map_allocated_size == 0

    def test_release_without_resources_is_noop(self) -> None:
        node = Scene3DNode()
        node._release_shadow_targets()  # 不抛异常
        assert node._shadow_texture is None

    def test_release_swallows_exceptions(self) -> None:
        node = Scene3DNode()
        bad = MagicMock()
        bad.release.side_effect = RuntimeError("already released")
        node._shadow_fbo = bad
        node._release_shadow_targets()  # 异常被吞
        assert node._shadow_fbo is None


# ──────────────────────────────────────────────────────────────────────────
# VAO 懒建守卫（无几何 → None，不触碰 ctx）
# ──────────────────────────────────────────────────────────────────────────

class TestVaoLazyGuards:
    """无 procedural 几何时各辅助 VAO 构建路径返回 None。"""

    def test_depth_write_vao_requires_mesh_buffers(self) -> None:
        node = Scene3DNode()
        ctx = FakeGLContext()  # program 懒编译需要 ctx
        assert node._get_depth_write_vao(ctx, None, "k") is None
        assert node._depth_write_vaos == {}
        # 失败结果不缓存：program 仍被编译一次（懒建副作用）
        assert node._depth_write_prog is not None

    def test_shadow_write_vao_requires_mesh_buffers(self) -> None:
        node = Scene3DNode()
        assert node._get_shadow_write_vao(None, None, "k", object()) is None
        assert node._shadow_write_vaos == {}

    def test_id_write_vao_requires_mesh_buffers(self) -> None:
        node = Scene3DNode()
        ctx = FakeGLContext()
        assert node._get_id_write_vao(ctx, None, "k") is None
        assert node._id_write_vaos == {}

    def test_motion_write_vao_requires_mesh_data(self) -> None:
        node = Scene3DNode()
        assert node._get_motion_write_vao(None, None, "k", object()) is None
        assert node._motion_write_vaos == {}

    def test_outline_vao_requires_mesh_buffers(self) -> None:
        node = Scene3DNode()
        ctx = FakeGLContext()  # program 编译用 mock
        assert node._get_outline_vao(ctx, None, "k") is None
        assert node._outline_vaos == {}

    def test_outline_skinned_mesh_rejected_and_cached_none(self) -> None:
        node = Scene3DNode()
        ctx = FakeGLContext()
        skinned = SimpleNamespace(is_skinned=True)
        assert node._get_outline_vao(ctx, skinned, "sk") is None
        # 蒙皮 mesh 的 None 结果被缓存（后续不再重试）
        assert "sk" in node._outline_vaos
        assert node._outline_vaos["sk"] is None

    def test_aov_write_vao_requires_mesh_buffers(self) -> None:
        node = Scene3DNode()
        ctx = FakeGLContext()
        assert node._get_aov_write_vao(ctx, None, "k") is None
        assert node._aov_write_vaos == {}


class TestGetLodVao:
    """_get_lod_vao：content_hash 失败与缓存命中。"""

    def test_hash_failure_returns_none_zero(self) -> None:
        node = Scene3DNode()
        bad_mesh = SimpleNamespace(
            content_hash=MagicMock(side_effect=ValueError("hash boom")))
        assert node._get_lod_vao(None, object(), bad_mesh) == (None, 0)
        assert node._lod_vao_cache == {}

    def test_cache_hit_returns_cached_tuple(self) -> None:
        node = Scene3DNode()
        mesh = SimpleNamespace(content_hash=lambda: "lod-hash-1")
        sentinel_vao = object()
        node._lod_vao_cache["lod-hash-1"] = (sentinel_vao, 42)
        assert node._get_lod_vao(None, object(), mesh) == (sentinel_vao, 42)

    def test_cache_keyed_by_content_hash(self) -> None:
        node = Scene3DNode()
        mesh_a = SimpleNamespace(content_hash=lambda: "hash-a")
        mesh_b = SimpleNamespace(content_hash=lambda: "hash-b")
        node._lod_vao_cache["hash-a"] = ("vao-a", 1)
        node._lod_vao_cache["hash-b"] = ("vao-b", 2)
        assert node._get_lod_vao(None, object(), mesh_a)[1] == 1
        assert node._get_lod_vao(None, object(), mesh_b)[1] == 2


# ──────────────────────────────────────────────────────────────────────────
# 渲染 pass 早退路径（无 GL 状态时安全返回）
# ──────────────────────────────────────────────────────────────────────────

class TestRenderPassEarlyReturns:
    """未分配目标时各渲染 pass 立即返回，不触碰 ctx。"""

    @staticmethod
    def _matrix() -> np.ndarray:
        return np.eye(4, dtype=np.float32)

    def test_depth_pass_without_targets_returns(self) -> None:
        node = Scene3DNode()
        node._render_depth_pass(
            None, 8, 8, self._matrix(), self._matrix(), self._matrix(),
            (0.0, 0.0, 5.0), False, False, 1, False, 0.0, 1.0)

    def test_aov_pass_without_texture_returns(self) -> None:
        node = Scene3DNode()
        node._render_aov_pass(
            None, "normal", 0, 8, 8, self._matrix(), self._matrix(),
            self._matrix(), False, False, 1, False, 0.0, 1.0)

    def test_motion_pass_without_texture_returns(self) -> None:
        node = Scene3DNode()
        node._render_motion_pass(
            None, 8, 8, self._matrix(), self._matrix(), self._matrix(),
            12, ("scene",), False, False, 1)

    def test_shadow_catchers_without_light_matrix(self) -> None:
        node = Scene3DNode()
        assert node._render_shadow_catchers(
            None, 8, 8, self._matrix(), self._matrix(), self._matrix(),
            None, False, None, None, None, None) is False

    def test_shadow_catchers_without_shadow_texture(self) -> None:
        node = Scene3DNode()
        assert node._render_shadow_catchers(
            None, 8, 8, self._matrix(), self._matrix(), self._matrix(),
            self._matrix(), False, None, None, None, None) is False

    def test_shadow_map_without_targets_returns_none(self) -> None:
        node = Scene3DNode()
        ctx = FakeGLContext()
        ctx.fail_texture = True  # _ensure_shadow_targets 失败
        result = node._render_shadow_map(
            ctx, self._matrix(), self._matrix(), (0, 0, 0),
            (0.0, 1.0, 0.0), False, None, None, None, None)
        assert result is None

    def test_outline_pass_without_selection_returns(self) -> None:
        node = Scene3DNode()
        node._render_outline_pass(
            None, 8, 8, self._matrix(), self._matrix(), self._matrix(),
            False, False, 1, None, None, [], [], "")

    def test_id_pass_without_targets_returns(self) -> None:
        node = Scene3DNode()
        node._render_id_pass(
            None, 8, 8, self._matrix(), self._matrix(), self._matrix(),
            False, False, 1, None, None, [], [])


# ──────────────────────────────────────────────────────────────────────────
# AOV 输出结果构造
# ──────────────────────────────────────────────────────────────────────────

class TestMakeAovOutputResult:
    """_make_aov_output_result：辅助 pass 暴露为可连接的图像输出。"""

    @staticmethod
    def _source() -> TextureResult:
        return TextureResult(
            texture_id=11, width=64, height=32, revision=3,
            alpha_premultiplied=True, color_space="linear",
            depth_texture_id=55, has_deep_data=True)

    def test_aov_result_contract(self) -> None:
        source = self._source()
        result = Scene3DNode._make_aov_output_result(
            source, "normal", 99, 64, 32)
        assert result.texture_id == 99
        assert (result.width, result.height) == (64, 32)
        assert result.color_space == "data"
        assert result.alpha_premultiplied is False
        assert result.depth_texture_id is None
        assert result.has_deep_data is False
        assert result.aov_texture_ids == {"normal": 99}
        assert result.aov_dimensions == {"normal": (64, 32)}
        assert result.aov_semantic == "normal"

    def test_source_not_mutated(self) -> None:
        source = self._source()
        Scene3DNode._make_aov_output_result(source, "uv", 7, 8, 4)
        assert source.texture_id == 11
        assert source.alpha_premultiplied is True
        assert source.depth_texture_id == 55
        assert source.aov_semantic == "beauty"
        assert source.aov_texture_ids == {}


# ──────────────────────────────────────────────────────────────────────────
# redraw_with_view 门控 + mock 求值路径
# ──────────────────────────────────────────────────────────────────────────

class TestRedrawWithView:
    """redraw_with_view：缓存视图重绘的守卫条件与参数注入。"""

    @staticmethod
    def _armed_node() -> Scene3DNode:
        node = Scene3DNode("view-node")
        node._last_scene_content_sig = ("scene",)
        node._gpu_texture = MagicMock()  # 占位：非 None 即可
        return node

    @staticmethod
    def _eval_ctx() -> SimpleNamespace:
        return SimpleNamespace(scene_connections={}, interaction_active=False)

    def test_fresh_node_returns_none(self) -> None:
        node = Scene3DNode()
        assert node.redraw_with_view(
            {"cam_dist": 5.0}, self._eval_ctx(), None) is None

    def test_missing_eval_ctx_returns_none(self) -> None:
        node = self._armed_node()
        assert node.redraw_with_view({"cam_dist": 5.0}, None, None) is None

    def test_missing_scene_connections_returns_none(self) -> None:
        node = self._armed_node()
        ctx = SimpleNamespace(scene_connections=None, interaction_active=False)
        assert node.redraw_with_view({"cam_dist": 5.0}, ctx, None) is None

    def test_non_dict_cam_params_returns_none(self) -> None:
        node = self._armed_node()
        assert node.redraw_with_view(
            "not-a-dict", self._eval_ctx(), None) is None

    def test_invalid_cam_value_returns_none_without_evaluate(self) -> None:
        node = self._armed_node()
        with patch.object(Scene3DNode, "_do_evaluate") as evaluate:
            result = node.redraw_with_view(
                {"cam_dist": "not-a-number"}, self._eval_ctx(), None)
        assert result is None
        evaluate.assert_not_called()

    def test_valid_params_coerced_and_result_returned(self) -> None:
        # AUD-7312: 导航值只写入虚拟 viewer 场景的 parameters；真实节点的
        # 参数不得被导航值污染（见 EF-12）。写入断言改用虚拟节点。
        node = Scene3DNode("__viewer3d_scene__")
        node._last_scene_content_sig = ("scene",)
        node._gpu_texture = MagicMock()  # 占位：非 None 即可
        tex = TextureResult(texture_id=3, width=2, height=2)
        with patch.object(Scene3DNode, "_do_evaluate",
                          return_value={"display": tex}) as evaluate:
            result = node.redraw_with_view(
                {"cam_dist": 7, "cam_orbit_yaw": 15, "cam_target_x": 1.5},
                self._eval_ctx(), None, render_mode="preview")
        assert result is tex
        evaluate.assert_called_once()
        assert node.parameters["cam_dist"] == 7.0
        assert isinstance(node.parameters["cam_dist"], float)
        assert node.parameters["cam_orbit_yaw"] == 15.0
        assert node.parameters["cam_target_x"] == 1.5
        # 未提供的相机键不被写入
        assert "cam_orbit_pitch" not in node.parameters

    def test_real_node_params_not_polluted_by_nav_values(self) -> None:
        # AUD-7312: 真实 Scene3D 节点的 parameters 不接收导航值（相机器
        # 来源是 eval_ctx.global_3d_scene_settings）。
        node = self._armed_node()
        tex = TextureResult(texture_id=3, width=2, height=2)
        before = dict(node.parameters)
        with patch.object(Scene3DNode, "_do_evaluate",
                          return_value={"display": tex}) as evaluate:
            result = node.redraw_with_view(
                {"cam_dist": 7, "cam_orbit_yaw": 15},
                self._eval_ctx(), None, render_mode="preview")
        assert result is tex
        evaluate.assert_called_once()
        assert node.parameters == before
        assert "cam_dist" not in node.parameters

    def test_non_texture_result_output_returns_none(self) -> None:
        node = self._armed_node()
        with patch.object(Scene3DNode, "_do_evaluate",
                          return_value={"display": "junk"}):
            assert node.redraw_with_view(
                {}, self._eval_ctx(), None) is None

    def test_empty_dict_result_returns_none(self) -> None:
        node = self._armed_node()
        with patch.object(Scene3DNode, "_do_evaluate", return_value={}):
            assert node.redraw_with_view(
                {"cam_dist": 2.0}, self._eval_ctx(), None) is None


# ──────────────────────────────────────────────────────────────────────────
# 动画采样守卫
# ──────────────────────────────────────────────────────────────────────────

class TestSampleAnimationWorlds:
    """_sample_animation_worlds：无模型/无动画/未启用的空结果。"""

    def test_disabled_animation(self) -> None:
        node = Scene3DNode()
        node._model_root = SimpleNamespace(animations=["clip"])
        assert node._sample_animation_worlds(False, 0.0, 1.0) == {}

    def test_missing_model_root(self) -> None:
        node = Scene3DNode()
        assert node._sample_animation_worlds(True, 1.0, 1.0) == {}

    def test_model_without_clips(self) -> None:
        node = Scene3DNode()
        node._model_root = SimpleNamespace(animations=[])
        assert node._sample_animation_worlds(True, 1.0, 1.0) == {}


# ──────────────────────────────────────────────────────────────────────────
# 实例 buffer 构建（mock ctx，字节级校验）
# ──────────────────────────────────────────────────────────────────────────

class TestEnsureInstanceBuffer:
    """_ensure_instance_buffer：网格居中偏移上传 + (gx,gz,spacing) 缓存。"""

    def test_count_and_byte_layout(self) -> None:
        node = Scene3DNode()
        ctx = FakeGLContext()
        count = node._ensure_instance_buffer(ctx, 3, 2, 1.0)
        assert count == 6
        assert node._instance_count_cached == 6
        assert node._instance_sig_cached == (3, 2, 1.0)
        assert len(ctx.buffers) == 1
        offsets = np.frombuffer(ctx.buffers[0], dtype=np.float32).reshape(-1, 3)
        # 首个偏移：i=0, j=0 → ((0-1)*1.0, 0, (0-0.5)*1.0)
        np.testing.assert_allclose(offsets[0], (-1.0, 0.0, -0.5), atol=1e-6)
        # 最后一个偏移：i=2, j=1 → (+1.0, 0, +0.5)
        np.testing.assert_allclose(offsets[-1], (1.0, 0.0, 0.5), atol=1e-6)
        # y 全 0
        assert np.all(offsets[:, 1] == 0.0)

    def test_same_signature_uses_cache(self) -> None:
        node = Scene3DNode()
        ctx = FakeGLContext()
        first = node._ensure_instance_buffer(ctx, 2, 2, 1.0)
        vbo = node._instance_vbo
        second = node._ensure_instance_buffer(ctx, 2, 2, 1.0)
        assert first == second == 4
        assert len(ctx.buffers) == 1
        assert node._instance_vbo is vbo

    def test_signature_change_rebuilds(self) -> None:
        node = Scene3DNode()
        ctx = FakeGLContext()
        node._ensure_instance_buffer(ctx, 2, 2, 1.0)
        old_vbo = node._instance_vbo
        node._ensure_instance_buffer(ctx, 2, 2, 2.0)
        assert node._instance_vbo is not old_vbo
        assert old_vbo.released is True
        assert len(ctx.buffers) == 2
        assert node._instance_count_cached == 4

    def test_grid_offsets_are_centered(self) -> None:
        node = Scene3DNode()
        ctx = FakeGLContext()
        node._ensure_instance_buffer(ctx, 4, 4, 0.5)
        offsets = np.frombuffer(ctx.buffers[0], dtype=np.float32).reshape(-1, 3)
        assert offsets[:, 0].min() == pytest.approx(-0.75)
        assert offsets[:, 0].max() == pytest.approx(0.75)
        assert offsets[:, 2].min() == pytest.approx(-0.75)
        assert offsets[:, 2].max() == pytest.approx(0.75)


# ──────────────────────────────────────────────────────────────────────────
# 模型加载缓存（mock loader）
# ──────────────────────────────────────────────────────────────────────────

class TestEnsureModel:
    """_ensure_model：空路径 / 加载失败降级 / path+mtime 缓存命中。"""

    def test_empty_path_returns_none_and_clears_error(self) -> None:
        node = Scene3DNode()
        node._model_error = "stale"
        assert node._ensure_model("") is None
        assert node._model_root is None
        assert node._model_error is None

    def test_loader_unavailable_sets_error_and_returns_none(self) -> None:
        from src.features.composite.render3d.model_loader import (
            ModelLoaderUnavailable)
        node = Scene3DNode()
        with patch(
            "src.features.composite.render3d.model_loader.load_model",
            side_effect=ModelLoaderUnavailable("no trimesh"),
        ):
            assert node._ensure_model("/tmp/nonexistent-model.glb") is None
        assert node._model_error == "no trimesh"
        assert node._model_root is None

    def test_cache_hit_returns_cached_root(self) -> None:
        node = Scene3DNode()
        sentinel = SimpleNamespace(animations=[])
        node._model_root = sentinel
        node._model_path_cached = "/tmp/never-reloaded.glb"
        node._model_mtime_cached = 0.0  # 不存在的路径 mtime=0.0
        with patch(
            "src.features.composite.render3d.model_loader.load_model",
        ) as load_model:
            assert node._ensure_model("/tmp/never-reloaded.glb") is sentinel
            load_model.assert_not_called()

    def test_successful_load_caches_root(self) -> None:
        node = Scene3DNode()
        root = SimpleNamespace(animations=[])
        with patch(
            "src.features.composite.render3d.model_loader.load_model",
            return_value=SimpleNamespace(root=root),
        ):
            result = node._ensure_model("/tmp/definitely-missing.xyz")
        assert result is not None
        assert node._model_path_cached == "/tmp/definitely-missing.xyz"
        assert node._model_error is None


# ──────────────────────────────────────────────────────────────────────────
# cleanup（headless）
# ──────────────────────────────────────────────────────────────────────────

class TestCleanup:
    """cleanup：GL 资源释放 + 状态复位，可重复调用。"""

    def test_cleanup_on_fresh_node_is_safe(self) -> None:
        node = Scene3DNode()
        node.cleanup()
        assert node._gpu_texture is None
        assert node._mesh_prog is None
        assert node._model_root is None

    def test_cleanup_releases_auxiliary_renderers_and_programs(self) -> None:
        node = Scene3DNode()
        mocks: Dict[str, MagicMock] = {
            "_gizmo": MagicMock(),
            "_skeleton_renderer": MagicMock(),
            "_hdri_baker": MagicMock(),
            "_material_tex_loader": MagicMock(),
            "_instance_vbo": MagicMock(),
            "_mesh_prog": MagicMock(),
            "_mesh_skinned_prog": MagicMock(),
            "_mesh_instanced_prog": MagicMock(),
            "_depth_write_prog": MagicMock(),
            "_id_write_prog": MagicMock(),
            "_outline_prog": MagicMock(),
        }
        for attr, mock in mocks.items():
            setattr(node, attr, mock)

        node.cleanup()

        mocks["_gizmo"].release.assert_called_once()
        mocks["_skeleton_renderer"].release.assert_called_once()
        mocks["_hdri_baker"].release.assert_called_once()
        mocks["_material_tex_loader"].release_all.assert_called_once()
        mocks["_instance_vbo"].release.assert_called_once()
        for attr in ("_mesh_prog", "_mesh_skinned_prog",
                     "_mesh_instanced_prog", "_depth_write_prog",
                     "_id_write_prog", "_outline_prog"):
            assert getattr(node, attr) is None, f"{attr} 应被清空"

    def test_cleanup_resets_model_and_motion_state(self) -> None:
        node = Scene3DNode()
        node._model_root = SimpleNamespace()
        node._model_path_cached = "/tmp/x.glb"
        node._model_error = "err"
        node._motion_prev_proj = np.eye(4)
        node._motion_prev_view = np.eye(4)
        node._motion_prev_frame = 9
        node._motion_prev_scene_key = ("k",)
        node._motion_prev_models = {"a": np.eye(4)}
        node._motion_prev_anim_worlds = {"a": np.eye(4)}

        node.cleanup()

        assert node._model_root is None
        assert node._model_path_cached is None
        assert node._model_error is None
        assert node._motion_prev_proj is None
        assert node._motion_prev_view is None
        assert node._motion_prev_frame is None
        assert node._motion_prev_scene_key is None
        assert node._motion_prev_models == {}
        assert node._motion_prev_anim_worlds == {}

    def test_cleanup_releases_flat_meshes_and_instance_state(self) -> None:
        node = Scene3DNode()
        vao = MagicMock()
        node._flat_meshes_gl = [{
            "vao": vao, "vbo": None, "ibo": None, "vbo_hash": None,
            "is_skinned": False,
        }]
        node._instance_vbo = MagicMock()
        node._instance_count_cached = 9
        node._instance_sig_cached = (2, 2, 1.0)

        node.cleanup()

        vao.release.assert_called_once()
        assert node._flat_meshes_gl == []
        assert node._instance_vbo is None
        assert node._instance_count_cached == 0
        assert node._instance_sig_cached is None

    def test_cleanup_idempotent(self) -> None:
        node = Scene3DNode()
        node._gizmo = MagicMock()
        node.cleanup()
        node.cleanup()  # 第二次不抛异常
        assert node._gizmo is None


# ──────────────────────────────────────────────────────────────────────────
# 渲染目标释放（空态安全）
# ──────────────────────────────────────────────────────────────────────────

class TestReleaseTargets:
    """_release_render_target / _release_ortho_views：空态与 mock 释放。"""

    def test_release_render_target_empty_state(self) -> None:
        node = Scene3DNode()
        node._release_render_target()
        assert node._gpu_texture is None
        assert node._aov_textures == {}
        assert node._id_texture is None

    def test_release_render_target_releases_everything(self) -> None:
        node = Scene3DNode()
        tex = MagicMock()
        fbo = MagicMock()
        depth_rb = MagicMock()
        node._gpu_texture = tex
        node._render_fbo = fbo
        node._depth_rb = depth_rb
        node._msaa_samples = 4
        node._nav_render_scale = 0.5

        node._release_render_target()

        tex.release.assert_called_once()
        fbo.release.assert_called_once()
        depth_rb.release.assert_called_once()
        assert node._msaa_samples == 0
        assert node._nav_render_scale == 1.0

    def test_release_ortho_views_resets_entries(self) -> None:
        node = Scene3DNode()
        tex = MagicMock()
        fbo = MagicMock()
        node._ortho_textures = {"top": tex, "right": None, "front": None}
        node._ortho_fbos = {"top": fbo, "right": None, "front": None}

        node._release_ortho_views()

        tex.release.assert_called_once()
        fbo.release.assert_called_once()
        assert node._ortho_textures == {"top": None, "right": None, "front": None}
        assert node._ortho_fbos == {"top": None, "right": None, "front": None}
