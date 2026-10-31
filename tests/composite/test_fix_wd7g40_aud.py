"""wd7-g40 批次（计划表批 D）修复回归测试。

覆盖条目（Major 级 16 条，render3d 资源 + composite 页面域）：
- AUD-8919  MeshVBOPool.acquire IBO 创建失败回滚 VBO（无 GL buffer 泄漏）
- AUD-9005  assimp CLI 可取消等待 + 服务取消/退出终止在途转换（非 daemon
            worker 不再阻塞应用退出）
- AUD-9006  _convert_trimesh_scene 用世界矩阵（含祖先链）而非 transforms.local
- AUD-9007  glTF 重建树匹配审计：名字不匹配时保留 trimesh 几何树
- AUD-9008  _flip_scene_uv_v 收窄捕获 + 失败计数进导入报告
- AUD-9106  外部点云 NaN/Inf 过滤（加载侧 + rasterize 双保险）
- AUD-9109  content_hash 纳入 normals/uvs 字节（VBO 池不再误共享）
- AUD-9117  flatten_scene 同名兄弟 node_id 消歧
- AUD-9300  runtime 可选面板构建失败以 warning+堆栈记录（不再 debug 吞掉）
- AUD-9301  runtime.cleanup 停用自建面板（dispose + 停 QTimer）
- AUD-9400  取色对话框引用保持（非模态不被 GC 销毁）
- AUD-9401  取色实时预览不再逐 tick 提交 undo（一次事务一次 undo）
- AUD-9402  from_dict/apply_state 对持久化数据逐字段校验回退
- AUD-9501  _add_ai_generate_node 惰性导入 AIGenerateNode（不再 NameError）
- AUD-9502  参数防抖累加器归属校验：异参/异节点先 flush 旧条目
- AUD-9503  _undo_add_node/_redo_remove_node 提交 CLEANUP_NODE

GL 对象用 fake 替代；Qt 测试类标 visual（headless offscreen）。
"""

import json
import logging
import os
import struct
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

import numpy as np

import src.features.composite.render3d.model_loader as model_loader
import src.features.composite.render3d.model_import_service as model_import_service
from src.features.composite.render3d import point_cloud, scene_graph as sg
from src.features.composite.render3d.mesh_vbo_pool import MeshVBOPool
from src.features.composite.render3d.model_import_service import (
    IMPORT_CANCELLED,
    ModelImportRequest,
    ModelImportService,
)
from src.features.composite.render3d.model_import_report import (
    LOADER_BACKEND_TRIMESH,
    ModelImportReport,
)
from src.features.composite.render3d.primitives import MeshData, cube


# ════════════════════════════════════════════════════════════════════════
# AUD-8919 — MeshVBOPool IBO 失败回滚
# ════════════════════════════════════════════════════════════════════════

class _FakeBuffer:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


class _FlakyCtx:
    """第一个 buffer 成功（VBO），第二个抛异常（IBO 失败）。"""

    def __init__(self, fail_on_second: bool = True) -> None:
        self.fail_on_second = fail_on_second
        self.buffers: list = []

    def buffer(self, data):
        if self.fail_on_second and self.buffers:
            raise RuntimeError("out of video memory")
        buf = _FakeBuffer()
        self.buffers.append(buf)
        return buf


def test_aud_8919_acquire_rolls_back_vbo_when_ibo_fails():
    ctx = _FlakyCtx(fail_on_second=True)
    pool = MeshVBOPool()
    mesh = cube(0.5)
    with pytest.raises(RuntimeError):
        pool.acquire(ctx, mesh)
    # VBO 已回滚释放，未进池、无泄漏。
    assert ctx.buffers[0].released is True
    assert pool.stats() == (0, 0)


def test_aud_8919_acquire_happy_path_returns_buffers():
    ctx = _FlakyCtx(fail_on_second=False)
    pool = MeshVBOPool()
    mesh = cube(0.5)
    vbo, ibo, index_count = pool.acquire(ctx, mesh)
    assert vbo is ctx.buffers[0]
    assert ibo is ctx.buffers[1]
    assert index_count == int(mesh.indices.shape[0])
    assert pool.stats() == (1, 1)


# ════════════════════════════════════════════════════════════════════════
# AUD-9005 — 可取消的 assimp CLI 等待 / 服务退出终止在途转换
# ════════════════════════════════════════════════════════════════════════

class _FakePopen:
    """模拟挂起的 assimp CLI 子进程：活着时 communicate 超时。"""

    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        return "", ""

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


def test_aud_9005_run_assimp_export_cancels_and_terminates():
    made: list = []

    def factory(cmd, **kwargs):
        proc = _FakePopen(cmd, **kwargs)
        made.append(proc)
        return proc

    with patch.object(model_loader.subprocess, "Popen", factory):
        model_loader._set_cancel_check(lambda: True)
        try:
            with pytest.raises(model_loader.ModelImportCancelled):
                model_loader._run_assimp_export(
                    "assimp", "a.fbx", "b.glb", timeout_s=5.0)
        finally:
            model_loader._set_cancel_check(None)
    # 取消 → 子进程被 SIGTERM（宽限内退出则无需 SIGKILL）。
    assert made and made[0].terminated is True
    assert made[0].killed is False


def test_aud_9005_run_assimp_export_timeout_still_raises():
    made: list = []

    def factory(cmd, **kwargs):
        proc = _FakePopen(cmd, **kwargs)
        made.append(proc)
        return proc

    with patch.object(model_loader.subprocess, "Popen", factory):
        model_loader._set_cancel_check(None)
        with pytest.raises(subprocess.TimeoutExpired):
            model_loader._run_assimp_export(
                "assimp", "a.fbx", "b.glb", timeout_s=0.2)
    assert made and made[0].terminated is True


def test_aud_9005_terminate_active_cli_exports():
    fake = _FakePopen(["assimp"])
    with model_loader._active_cli_exports_lock:
        model_loader._active_cli_exports[id(fake)] = fake
    try:
        terminated = model_loader._terminate_active_cli_exports()
        assert terminated == 1
        assert fake.terminated is True
    finally:
        with model_loader._active_cli_exports_lock:
            model_loader._active_cli_exports.pop(id(fake), None)


def test_aud_9005_service_cancels_inflight_parse_as_cancelled():
    """在途解析收到取消令牌后应立刻以 IMPORT_CANCELLED 终止，而非 FAILED。"""
    started = threading.Event()

    def fake_load_model(path, normalize=False, flip_uv_v=False):
        started.set()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if model_loader._cancel_requested():
                raise model_loader.ModelImportCancelled("in-flight cancel")
            time.sleep(0.005)
        raise AssertionError("worker was never cancelled")

    probe = SimpleNamespace(can_import=True, reason_code="", reason="")
    service = ModelImportService(max_workers=1)
    try:
        with patch.object(model_import_service, "probe_import",
                          return_value=probe), \
             patch.object(model_loader, "load_model", fake_load_model):
            handle = service.submit(ModelImportRequest.from_path("/tmp/m.fbx"))
            assert started.wait(5.0), "worker never entered load_model"
            assert handle.cancel() is False  # 运行中 → 合作式取消
            result = handle.result(timeout=5.0)
        assert result.cancelled is True
        assert result.status == IMPORT_CANCELLED
        assert result.root is None
    finally:
        service.shutdown()


def test_aud_9005_service_shutdown_terminates_registered_cli_processes():
    fake = _FakePopen(["assimp", "export"])
    with model_loader._active_cli_exports_lock:
        model_loader._active_cli_exports[id(fake)] = fake
    service = ModelImportService(max_workers=1)
    try:
        service.shutdown(wait=False)
        assert fake.terminated is True
    finally:
        with model_loader._active_cli_exports_lock:
            model_loader._active_cli_exports.pop(id(fake), None)


# ════════════════════════════════════════════════════════════════════════
# AUD-9006 — trimesh 层级场景使用世界矩阵
# ════════════════════════════════════════════════════════════════════════

def _build_hierarchical_scene(trimesh):
    scene = trimesh.Scene()
    box = trimesh.creation.box(extents=(1.0, 1.0, 1.0))
    t1 = trimesh.transformations.translation_matrix((2.0, 0.0, 0.0))
    t2 = trimesh.transformations.translation_matrix((0.0, 3.0, 0.0))
    scene.add_geometry(box, geom_name="geometry_0", node_name="meshnode_0",
                       transform=t1)
    scene.add_geometry(box, geom_name="geometry_1", node_name="meshnode_1",
                       parent_node_name="meshnode_0", transform=t2)
    return scene


def test_aud_9006_world_matrix_helper_composes_ancestors(tmp_path):
    trimesh = pytest.importorskip("trimesh")
    scene = _build_hierarchical_scene(trimesh)
    # geometry_1 挂在带 T1 的父节点下，自身 T2 → 世界平移 (2, 3, 0)。
    matrix = model_loader._trimesh_geometry_world_matrix(scene, "geometry_1")
    assert matrix is not None
    assert np.allclose(matrix[:3, 3], (2.0, 3.0, 0.0), atol=1e-5)


def test_aud_9006_convert_trimesh_scene_keeps_hierarchy_transforms(tmp_path):
    trimesh = pytest.importorskip("trimesh")
    scene = _build_hierarchical_scene(trimesh)
    path = tmp_path / "hier.glb"
    scene.export(str(path))

    root = model_loader._load_trimesh(str(path), source_path=str(path))
    by_name = {n.name: n for n in root.root.children}
    assert set(by_name) == {"geometry_0", "geometry_1"}
    # 平铺挂 root 的 local_transform 必须是各几何的世界矩阵（含祖先链）。
    assert np.allclose(by_name["geometry_0"].local_transform[:3, 3],
                       (2.0, 0.0, 0.0), atol=1e-4)
    assert np.allclose(by_name["geometry_1"].local_transform[:3, 3],
                       (2.0, 3.0, 0.0), atol=1e-4)


def test_aud_9006_world_matrix_helper_falls_back_to_old_trimesh_dict():
    """旧版 trimesh（3.x）的 node_to_matrix dict 路径仍可用。"""

    class _Transforms:
        node_to_matrix = {"geometry_0": np.array(
            [[1, 0, 0, 9], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
            dtype=np.float32)}

    class _Graph:
        transforms = _Transforms()
        geometry_nodes = {}

    matrix = model_loader._trimesh_geometry_world_matrix(
        SimpleNamespace(graph=_Graph), "geometry_0")
    assert matrix is not None
    assert matrix[0, 3] == 9.0


def test_aud_9006_world_matrix_helper_missing_geometry_returns_none():
    matrix = model_loader._trimesh_geometry_world_matrix(
        SimpleNamespace(graph=None), "geometry_0")
    assert matrix is None


# ════════════════════════════════════════════════════════════════════════
# AUD-9007 — glTF 重建树的匹配审计
# ════════════════════════════════════════════════════════════════════════

def _write_minimal_glb(path, document: dict) -> None:
    payload = json.dumps(document).encode("utf-8")
    while len(payload) % 4:
        payload += b" "
    total = 12 + 8 + len(payload)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<4sII", b"glTF", 2, total))
        fh.write(struct.pack("<I4s", len(payload), b"JSON"))
        fh.write(payload)


def _make_source_root() -> sg.SceneGraphRoot:
    node = sg.SceneNode(name="geometry_0", mesh=cube(0.5))
    root_node = sg.SceneNode(name="root", children=[node])
    root = sg.SceneGraphRoot(root=root_node)
    root.import_report = ModelImportReport(
        source_path="src", loader_backend=LOADER_BACKEND_TRIMESH)
    return root


def test_aud_9007_name_mismatch_preserves_trimesh_tree(tmp_path):
    glb = tmp_path / "mismatch.glb"
    _write_minimal_glb(glb, {
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"name": "Armature", "mesh": 0}],
        "meshes": [{"name": "SomeOtherMesh", "primitives": []}],
    })
    root = _make_source_root()
    original_child = root.root.children[0]

    animation, skinning = model_loader._restore_gltf_animation_and_skin(
        root, str(glb))

    # 几何未被丢弃：仍是原 trimesh 树的同一节点。
    assert root.root.children == [original_child]
    assert root.root.children[0].mesh is not None
    assert any("保留 trimesh 场景树" in w
               for w in root.import_report.warnings)
    assert animation is False and skinning is False


def test_aud_9007_mesh_name_fallback_rebuilds_tree(tmp_path):
    glb = tmp_path / "fallback.glb"
    _write_minimal_glb(glb, {
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"name": "Armature", "mesh": 0}],
        "meshes": [{"name": "geometry_0", "primitives": []}],
    })
    root = _make_source_root()

    model_loader._restore_gltf_animation_and_skin(root, str(glb))

    # 节点名不匹配但 glTF mesh 名命中 → 重建树且几何保留。
    assert root.root.children[0].name == "Armature"
    assert root.root.children[0].mesh is not None


def test_aud_9007_full_match_by_node_name_rebuilds_tree(tmp_path):
    glb = tmp_path / "match.glb"
    _write_minimal_glb(glb, {
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"name": "geometry_0", "mesh": 0}],
        "meshes": [{"name": "whatever", "primitives": []}],
    })
    root = _make_source_root()

    model_loader._restore_gltf_animation_and_skin(root, str(glb))

    assert root.root.children[0].name == "geometry_0"
    assert root.root.children[0].mesh is not None


def test_aud_9007_scene_without_mesh_nodes_preserves_tree(tmp_path):
    """文档无 mesh 节点但 trimesh 已加载几何：重建会产生空树 → 保留。"""
    glb = tmp_path / "nomesh.glb"
    _write_minimal_glb(glb, {
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"name": "group"}],
    })
    root = _make_source_root()
    original_child = root.root.children[0]

    model_loader._restore_gltf_animation_and_skin(root, str(glb))

    assert root.root.children == [original_child]
    assert root.root.children[0].mesh is not None


# ════════════════════════════════════════════════════════════════════════
# AUD-9008 — UV 翻转失败可诊断
# ════════════════════════════════════════════════════════════════════════

def test_aud_9008_flip_scene_uv_v_counts_failures():
    good = cube(0.5)
    good_original_v = good.uvs[:, 1].copy()
    bad = cube(0.5)
    bad.uvs = object()  # np.array(...) 抛 TypeError
    root = sg.SceneNode(name="root", children=[
        sg.SceneNode(name="good", mesh=good),
        sg.SceneNode(name="bad", mesh=bad),
    ])

    failed = model_loader._flip_scene_uv_v(root)

    assert failed == 1
    assert np.allclose(good.uvs[:, 1], 1.0 - good_original_v)
    # bad mesh 不再被静默吞掉异常，也不应崩溃。


def test_aud_9008_flip_scene_uv_v_all_good_returns_zero():
    mesh = cube(0.5)
    root = sg.SceneNode(name="root",
                        children=[sg.SceneNode(name="m", mesh=mesh)])
    assert model_loader._flip_scene_uv_v(root) == 0


# ════════════════════════════════════════════════════════════════════════
# AUD-9106 — 点云非有限坐标过滤
# ════════════════════════════════════════════════════════════════════════

def test_aud_9106_load_xyz_filters_nan_inf(tmp_path):
    path = tmp_path / "cloud.xyz"
    path.write_text("# comment\n0 0 0\nnan 1 0\n1 inf 0\n1 1 0\n", encoding="utf-8")
    cloud = point_cloud.load_point_cloud(str(path))
    assert cloud.count == 2
    assert bool(np.isfinite(cloud.positions).all())


def test_aud_9106_load_xyz_all_nonfinite_raises_value_error(tmp_path):
    path = tmp_path / "bad.xyz"
    path.write_text("nan nan nan\ninf 1 2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        point_cloud.load_point_cloud(str(path))


def test_aud_9106_rasterize_filters_nonfinite_without_cast_warning():
    import warnings
    pts = np.array([
        [0.0, 0.0, 0.0],
        [np.nan, 1.0, 0.0],
        [np.inf, 2.0, 0.0],
        [1.0, 1.0, 0.0],
    ], dtype=np.float32)
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        out = point_cloud.rasterize(pts, None, 32, 32, point_size=1)
    assert out.shape == (32, 32, 4)
    assert float(out[..., 3].max()) == 1.0  # 有限点正常画出


def test_aud_9106_rasterize_all_nonfinite_returns_background():
    pts = np.array([[np.nan, np.nan, 0.0]], dtype=np.float32)
    out = point_cloud.rasterize(pts, None, 8, 8)
    assert not bool(out[..., 3].any())


def test_aud_9106_rasterize_filters_aligned_vertex_colors():
    pts = np.array([[0.0, 0.0, 0.0], [np.nan, 0.0, 0.0]], dtype=np.float32)
    colors = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
    out = point_cloud.rasterize(pts, colors, 16, 16, point_size=1,
                                color_mode="vertex_color")
    painted = out[..., 3] > 0
    assert bool(painted.any())
    # 只剩 1 个有限点，其颜色为红（颜色行随过滤对齐）。
    assert np.allclose(out[painted][0][:3], [1.0, 0.0, 0.0], atol=1e-6)


# ════════════════════════════════════════════════════════════════════════
# AUD-9109 — content_hash 纳入 normals/uvs
# ════════════════════════════════════════════════════════════════════════

def test_aud_9109_content_hash_distinguishes_uvs():
    a = cube(0.5)
    b = cube(0.5)
    assert a.content_hash() == b.content_hash()
    b.uvs = (b.uvs * 0.5 + 0.25).astype(np.float32)
    b._content_hash = None
    assert a.content_hash() != b.content_hash()


def test_aud_9109_content_hash_distinguishes_normals():
    a = cube(0.5)
    c = cube(0.5)
    c.normals = (c.normals * -1.0).astype(np.float32)
    c._content_hash = None
    assert a.content_hash() != c.content_hash()


def test_aud_9109_content_hash_identical_meshes_share():
    a = cube(0.5)
    b = cube(0.5)
    assert a.content_hash() == b.content_hash()


# ════════════════════════════════════════════════════════════════════════
# AUD-9117 — 同名兄弟 node_id 消歧
# ════════════════════════════════════════════════════════════════════════

def test_aud_9117_flatten_scene_same_name_siblings_get_distinct_ids():
    mesh = cube(0.5)
    root = sg.SceneNode(name="root", children=[
        sg.SceneNode(name="wheel", mesh=mesh),
        sg.SceneNode(name="axle"),
        sg.SceneNode(name="wheel", mesh=mesh),
    ])
    flat = sg.flatten_scene(root, source_path="/tmp/model.glb")
    ids = [fm.node_id for fm in flat]
    assert len(ids) == 2
    assert ids[0] != ids[1]


def test_aud_9117_flatten_scene_unique_names_keep_stable_ids():
    mesh = cube(0.5)
    root = sg.SceneNode(name="root",
                        children=[sg.SceneNode(name="wheel", mesh=mesh)])
    flat = sg.flatten_scene(root, source_path="/tmp/model.glb")
    expected = sg.stable_scene_node_id("/tmp/model.glb", "root/wheel")
    assert flat[0].node_id == expected


def test_aud_9117_flatten_scene_first_duplicate_keeps_unsuffixed_id():
    mesh = cube(0.5)
    root = sg.SceneNode(name="root", children=[
        sg.SceneNode(name="wheel", mesh=mesh),
        sg.SceneNode(name="wheel", mesh=mesh),
    ])
    flat = sg.flatten_scene(root, source_path="/tmp/model.glb")
    assert flat[0].node_id == sg.stable_scene_node_id(
        "/tmp/model.glb", "root/wheel")
    assert flat[1].node_id == sg.stable_scene_node_id(
        "/tmp/model.glb", "root/wheel#1")


# ════════════════════════════════════════════════════════════════════════
# AUD-9501/9502/9503 — composite_page（unbound-method + SimpleNamespace 模式）
# ════════════════════════════════════════════════════════════════════════

def test_aud_9501_add_ai_generate_node_resolves_class():
    from src.features.composite.ui.composite_page import CompositePage
    from src.features.composite.nodes.ai_generate_node import AIGenerateNode
    recorded = {}

    def fake_add_by_type(node_cls, position=(0, 0)):
        recorded["cls"] = node_cls
        recorded["pos"] = position

    page = SimpleNamespace(_add_node_by_type=fake_add_by_type)
    CompositePage._add_ai_generate_node(page)  # 不再 NameError
    assert recorded["cls"] is AIGenerateNode
    assert recorded["pos"] == (100, 400)


class _FakeUndoStack:
    def __init__(self):
        self.events: list = []
        self.actions: list = []

    def begin_merge_coalescing(self):
        self.events.append("begin")

    def end_merge_coalescing(self):
        self.events.append("end")

    def push(self, action):
        self.actions.append(action)


def _make_param_page(graph):
    from src.features.composite.ui.composite_page import CompositePage
    stack = _FakeUndoStack()
    page = SimpleNamespace(
        _node_graph=graph,
        _node_editor=None,
        _pending_param_change=None,
        _param_debounce_timer=MagicMock(),
        _prefetch_timer=MagicMock(),
        _prefetch_enabled=False,
        _prefetch_scheduler=MagicMock(),
        _active_prefetch_prediction=None,
        _undo_stack=stack,
        _renderer=None,
        _emit_user_edit_once=lambda: None,
        _sync_active_viewer_camera_if_needed=lambda nid, n: None,
        _refresh_keyframes_and_spline_for_node=lambda nid: None,
        _is_realtime_3d_transform_preview=lambda n, params: False,
        _evaluate_graph=lambda: None,
        _evaluate_graph_now=lambda: None,
        update_viewer_handles=lambda: None,
        _refresh_properties_panel=lambda nid: None,
        _sync_video_trim_layer=lambda n: None,
    )
    page._flush_param_changes = lambda: CompositePage._flush_param_changes(page)
    page._on_parameter_changed = (
        lambda nid, pname, value:
        CompositePage._on_parameter_changed(page, nid, pname, value))
    # trim 路径的 classmethod 委托（简单命名空间上按函数访问）。
    page._video_trim_snapshot = (
        lambda node: CompositePage._video_trim_snapshot(node))
    page._apply_video_trim_range = (
        lambda node, start, end:
        CompositePage._apply_video_trim_range(node, start, end))
    return page


def _two_params(node):
    names = [name for name in node.parameters]
    assert len(names) >= 2, "test node needs >= 2 parameters"
    return names[0], names[1]


def test_aud_9502_param_accumulator_flushes_on_param_mismatch():
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.nodes.transform_node import TransformNode
    from src.features.composite.ui import composite_page as cp_mod
    from src.features.composite.ui.composite_page import CompositePage

    graph = NodeGraph()
    node = TransformNode()
    graph.add_node(node)
    param_a, param_b = _two_params(node)
    original_a = node.parameters[param_a]
    original_b = node.parameters[param_b]
    page = _make_param_page(graph)

    recorded: list = []

    def fake_factory(**kwargs):
        recorded.append(kwargs)
        return SimpleNamespace()

    with patch.object(cp_mod, "create_param_change_action", fake_factory):
        # tick 1：参数 A 的首个修改（记录原始 old_value）。
        page._on_parameter_changed(node.id, param_a, 10.0)
        assert page._pending_param_change == (node.id, param_a, original_a, 10.0)
        assert page._undo_stack.events.count("begin") == 1
        assert not page._undo_stack.actions

        # tick 2：同一参数的后续 tick——合并，保留原始 old。
        page._on_parameter_changed(node.id, param_a, 20.0)
        assert page._pending_param_change == (node.id, param_a, original_a, 20.0)
        assert not page._undo_stack.actions

        # tick 3：80ms 窗口内修改第二个参数 → 先 flush A，再开 B 条目。
        page._on_parameter_changed(node.id, param_b, 99.0)

    # A 的修改已作为独立 undo 记录入库（不再丢失），old/new 正确。
    assert len(page._undo_stack.actions) == 1
    assert len(recorded) == 1
    assert recorded[0]["param_name"] == param_a
    assert recorded[0]["old_value"] == original_a
    assert recorded[0]["new_value"] == 20.0
    # 新累加条目属于 B，old_value 是 B 自己的原始值（不沿用 A 的）。
    assert page._pending_param_change == (node.id, param_b, original_b, 99.0)
    assert page._undo_stack.events.count("begin") == 2


def test_aud_9502_param_accumulator_flushes_on_node_mismatch():
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.nodes.transform_node import TransformNode
    from src.features.composite.ui import composite_page as cp_mod
    from src.features.composite.ui.composite_page import CompositePage

    graph = NodeGraph()
    node1 = TransformNode()
    node2 = TransformNode()
    graph.add_node(node1)
    graph.add_node(node2)
    param1, _ = _two_params(node1)
    param2, _ = _two_params(node2)
    original1 = node1.parameters[param1]
    original2 = node2.parameters[param2]
    page = _make_param_page(graph)

    recorded: list = []

    with patch.object(cp_mod, "create_param_change_action",
                      lambda **kwargs: (recorded.append(kwargs),
                                        SimpleNamespace())[1]):
        page._on_parameter_changed(node1.id, param1, 5.0)
        page._on_parameter_changed(node2.id, param2, 7.0)

    assert len(recorded) == 1
    assert recorded[0]["node_id"] == node1.id
    assert recorded[0]["old_value"] == original1
    assert page._pending_param_change == (node2.id, param2, original2, 7.0)


def test_aud_9502_trim_path_flushes_foreign_pending_entry():
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.nodes.transform_node import TransformNode
    from src.features.composite.nodes.video_frame_node import VideoFrameNode
    from src.features.composite.ui import composite_page as cp_mod
    from src.features.composite.ui.composite_page import CompositePage

    graph = NodeGraph()
    transform = TransformNode()
    video = VideoFrameNode(node_id="video")
    video.set_parameter("total_frames", 500)
    graph.add_node(transform)
    graph.add_node(video)
    param, _ = _two_params(transform)
    original = transform.parameters[param]
    page = _make_param_page(graph)

    recorded: list = []

    with patch.object(cp_mod, "create_param_change_action",
                      lambda **kwargs: (recorded.append(kwargs),
                                        SimpleNamespace())[1]):
        # 先积累一个普通参数修改，再在防抖窗口内修改 trim_range。
        page._on_parameter_changed(transform.id, param, 3.0)
        CompositePage._on_video_trim_parameter_changed(page, video.id, (10, 90))

    # 普通参数修改被 flush（不丢失、old 正确），trim 新条目归位。
    assert len(recorded) == 1
    assert recorded[0]["param_name"] == param
    assert recorded[0]["old_value"] == original
    pending = page._pending_param_change
    assert pending is not None
    assert pending[0] == video.id and pending[1] == "trim_range"


def test_aud_9503_undo_add_node_submits_cleanup_node():
    from src.features.composite.render.composite_renderer import CompositeCommand
    from src.features.composite.ui.composite_page import CompositePage
    renderer = MagicMock()
    graph = MagicMock()
    editor = MagicMock()
    page = SimpleNamespace(
        _renderer=renderer,
        _node_graph=graph,
        _node_editor=editor,
        _clear_deleted_node_viewer_state=lambda nid: None,
        _evaluate_graph=lambda: None,
    )
    CompositePage._undo_add_node(page, {"node_id": "n1"})
    renderer.submit.assert_called_once_with(
        CompositeCommand.CLEANUP_NODE, {"node_id": "n1"})
    graph.remove_node.assert_called_once_with("n1")
    editor.remove_node_visual.assert_called_once_with("n1")


def test_aud_9503_redo_remove_node_submits_cleanup_node():
    from src.features.composite.render.composite_renderer import CompositeCommand
    from src.features.composite.ui.composite_page import CompositePage
    renderer = MagicMock()
    graph = MagicMock()
    editor = MagicMock()
    page = SimpleNamespace(
        _renderer=renderer,
        _node_graph=graph,
        _node_editor=editor,
        _clear_deleted_node_viewer_state=lambda nid: None,
        _evaluate_graph=lambda: None,
    )
    CompositePage._redo_remove_node(page, {"node_id": "n2"})
    renderer.submit.assert_called_once_with(
        CompositeCommand.CLEANUP_NODE, {"node_id": "n2"})


def test_aud_9503_undo_add_node_tolerates_missing_renderer():
    from src.features.composite.ui.composite_page import CompositePage
    page = SimpleNamespace(
        _renderer=None,
        _node_graph=MagicMock(),
        _node_editor=MagicMock(),
        _clear_deleted_node_viewer_state=lambda nid: None,
        _evaluate_graph=lambda: None,
    )
    CompositePage._undo_add_node(page, {"node_id": "n3"})  # 不抛


# ════════════════════════════════════════════════════════════════════════
# AUD-9300/9301 — CompositeRuntime（Qt，visual）
# ════════════════════════════════════════════════════════════════════════

class TestRuntimePanels:
    pytestmark = pytest.mark.visual

    @staticmethod
    def _install_fake_ui(monkeypatch):
        import src.features.composite.ui.composite_page as cp_mod
        import src.features.composite.ui.composite_viewport as cv_mod
        import src.features.composite.ui.node_properties_panel as npp_mod
        import src.features.composite.ui.composite_scope_widgets as csw_mod
        fake_page = MagicMock()
        fake_page.node_editor = MagicMock()
        monkeypatch.setattr(cp_mod, "CompositePage",
                            lambda parent=None, **kw: fake_page)
        monkeypatch.setattr(cv_mod, "CompositeViewer",
                            lambda parent=None, **kw: MagicMock())
        monkeypatch.setattr(npp_mod, "NodePropertiesPanel",
                            lambda parent=None, **kw: MagicMock())
        monkeypatch.setattr(csw_mod, "CompositeScopePanel",
                            lambda parent=None, **kw: MagicMock())
        monkeypatch.setattr(csw_mod, "configure_composite_scope_panel",
                            lambda panel: panel)
        return fake_page

    def test_aud_9300_panel_construction_failure_logs_warning(
            self, monkeypatch, qapp, caplog):
        self._install_fake_ui(monkeypatch)
        import src.features.composite.ui.spline_panel as sp_mod
        from src.features.composite.runtime import CompositeRuntime

        def boom(parent=None, **kw):
            raise TypeError("bad panel state")

        monkeypatch.setattr(sp_mod, "SplinePanel", boom)
        runtime = CompositeRuntime()
        with caplog.at_level(logging.WARNING,
                             logger="src.features.composite.runtime"):
            ok = runtime.create(parent=None)
        assert ok is True  # 面板失败不拖垮整个 feature
        assert runtime.spline_panel is None
        warnings_seen = [r for r in caplog.records
                         if r.levelno == logging.WARNING
                         and "SplinePanel creation failed" in r.getMessage()]
        assert warnings_seen, "construction failure must log at WARNING"

    def test_aud_9300_missing_module_stays_debug_and_feature_on(
            self, monkeypatch, qapp):
        self._install_fake_ui(monkeypatch)
        import sys
        from src.features.composite.runtime import CompositeRuntime
        # sys.modules 里置 None → import 语句抛 ImportError（特性探测分支）。
        monkeypatch.setitem(
            sys.modules, "src.features.composite.ui.spline_panel", None)
        runtime = CompositeRuntime()
        assert runtime.create(parent=None) is True
        assert runtime.spline_panel is None

    def test_aud_9301_cleanup_disposes_panels_and_stops_timers(self, qapp):
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QWidget
        from src.features.composite.runtime import CompositeRuntime

        runtime = CompositeRuntime()
        page = MagicMock()
        runtime.page = page
        keyframes = MagicMock()
        keyframes.findChildren = lambda cls: []
        runtime.keyframes_panel = keyframes
        spline_host = QWidget()
        runtime.spline_panel = spline_host
        play_timer = QTimer(spline_host)  # SplinePanel._play_timer 同型
        play_timer.start(20)
        assert play_timer.isActive()

        runtime.cleanup()

        page.cleanup.assert_called_once()
        keyframes.dispose.assert_called_once()
        assert not play_timer.isActive()

    def test_aud_9301_cleanup_without_page_is_noop(self):
        from src.features.composite.runtime import CompositeRuntime
        runtime = CompositeRuntime()
        runtime.cleanup()  # page=None，不抛


# ════════════════════════════════════════════════════════════════════════
# AUD-9400/9401/9402 — BackdropItem（Qt，visual）
# ════════════════════════════════════════════════════════════════════════

class TestBackdropFixes:
    pytestmark = pytest.mark.visual

    @pytest.fixture
    def backdrop(self, qapp):
        from PyQt6.QtWidgets import QGraphicsScene
        from src.features.composite.ui.backdrop_item import BackdropItem
        scene = QGraphicsScene()
        item = BackdropItem(backdrop_id="test-backdrop",
                            color="#2d4a3e")
        scene.addItem(item)
        yield item
        if item.scene() is not None:
            scene.removeItem(item)
        dialog = getattr(item, "_color_dialog", None)
        if dialog is not None:
            dialog.deleteLater()

    def test_aud_9400_dialog_reference_kept_while_open(self, backdrop):
        backdrop._choose_color()
        dlg = backdrop._color_dialog
        assert dlg is not None  # 引用被保持，非模态窗口不被 GC 销毁
        assert dlg.isVisible()
        dlg.close()

    def test_aud_9401_live_preview_no_undo_flood_single_commit(self, backdrop):
        from PyQt6.QtGui import QColor
        changed_calls: list = []
        modified_calls: list = []
        backdrop.set_changed_handler(lambda item: changed_calls.append(1))
        backdrop.set_modified_handler(
            lambda item, old, new: modified_calls.append((old, new)))
        original_color = backdrop.color()

        backdrop._choose_color()
        dlg = backdrop._color_dialog
        # 模拟拖动色轮：每个 tick 颜色都不同。
        for value in ("#ff0000", "#00ff00", "#0000ff"):
            dlg.currentColorChanged.emit(QColor(value))
        assert backdrop.color() == "#0000ff"  # 实时预览生效
        assert changed_calls == []            # 无 graph 同步洪泛
        assert modified_calls == []           # 无 undo 洪泛

        dlg.accept()
        # accepted → 恰好一次事务提交，old=预览前颜色。
        assert len(modified_calls) == 1
        old_data, new_data = modified_calls[0]
        assert old_data["color"] == original_color
        assert new_data["color"] == "#0000ff"
        assert backdrop._color_dialog is None  # 引用已释放

    def test_aud_9401_reject_rolls_back_without_undo(self, backdrop):
        from PyQt6.QtGui import QColor
        modified_calls: list = []
        backdrop.set_modified_handler(
            lambda item, old, new: modified_calls.append(1))
        original_color = backdrop.color()

        backdrop._choose_color()
        dlg = backdrop._color_dialog
        dlg.currentColorChanged.emit(QColor("#ff00ff"))
        assert backdrop.color() == "#ff00ff"
        dlg.reject()
        # 取消 → 回滚到预览前颜色，无 undo 记录。
        assert backdrop.color() == original_color
        assert modified_calls == []
        assert backdrop._change_old_data is None
        assert backdrop._color_dialog is None

    def test_aud_9401_second_open_while_active_is_ignored(self, backdrop):
        backdrop._choose_color()
        first = backdrop._color_dialog
        backdrop._choose_color()  # 已打开 → 不再开第二个
        assert backdrop._color_dialog is first
        first.close()

    def test_aud_9402_from_dict_missing_keys_fall_back(self, qapp):
        from src.features.composite.ui.backdrop_item import BackdropItem
        item = BackdropItem.from_dict({"id": "b1"})  # 缺 position/size/...
        assert item.backdrop_id == "b1"
        assert (item.x(), item.y()) == (0.0, 0.0)
        assert item.rect().width() == 300
        assert item.rect().height() == 200
        assert abs(item.backgroundOpacity() - 0.65) < 1e-6
        assert item.zOrder() == -10

    def test_aud_9402_from_dict_malformed_types_fall_back(self, qapp):
        from src.features.composite.ui.backdrop_item import BackdropItem
        item = BackdropItem.from_dict({
            "id": "b2",
            "position": "bad",
            "size": [None, "x"],
            "opacity": "high",
            "z_order": [],
            "title": 123,
            "note": None,
            "color": 3.14,
        })
        assert (item.x(), item.y()) == (0.0, 0.0)
        assert item.rect().width() == 300
        assert abs(item.backgroundOpacity() - 0.65) < 1e-6
        assert item.zOrder() == -10
        assert item.title() == "Backdrop"
        assert item.note() == ""

    def test_aud_9402_apply_state_malformed_entries_keep_current(
            self, backdrop):
        before = backdrop.to_dict()
        malformed = {
            "position": {"x": 1},
            "size": 5,
            "opacity": "abc",
            "z_order": "fast",
            "title": 42,
            "color": 3.14,
            "title_font_size": "big",
        }
        backdrop.apply_state(malformed)  # 不抛 KeyError/TypeError
        after = backdrop.to_dict()
        assert after == before

    def test_aud_9402_apply_state_valid_data_still_applies(self, backdrop):
        backdrop.apply_state({
            "position": [11.0, 22.0],
            "size": [222.0, 333.0],
            "opacity": 0.25,
            "z_order": -5,
            "title": "renamed",
        })
        assert (backdrop.x(), backdrop.y()) == (11.0, 22.0)
        assert backdrop.rect().width() == 222
        assert backdrop.rect().height() == 333
        assert abs(backdrop.backgroundOpacity() - 0.25) < 1e-6
        assert backdrop.title() == "renamed"
