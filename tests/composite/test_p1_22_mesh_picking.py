"""P1-22：BVH 加速与 Point/Edge/Polygon 组件选择契约测试。"""

import numpy as np


def _cube_mesh():
    vertices = np.array([
        [-1.0, -1.0, -1.0], [1.0, -1.0, -1.0],
        [1.0, 1.0, -1.0], [-1.0, 1.0, -1.0],
        [-1.0, -1.0, 1.0], [1.0, -1.0, 1.0],
        [1.0, 1.0, 1.0], [-1.0, 1.0, 1.0],
    ], dtype=np.float32)
    indices = np.array([
        0, 1, 2, 0, 2, 3,
        4, 6, 5, 4, 7, 6,
        0, 4, 5, 0, 5, 1,
        1, 5, 6, 1, 6, 2,
        2, 6, 7, 2, 7, 3,
        4, 0, 3, 4, 3, 7,
    ], dtype=np.uint32)
    return vertices, indices


def test_bvh_matches_linear_face_pick():
    from src.features.composite.render3d.mesh_picking import (
        TriangleBVH, pick_face, pick_face_bvh)

    vertices, indices = _cube_mesh()
    origin = np.array([0.25, 0.25, 5.0])
    direction = np.array([0.0, 0.0, -1.0])
    expected = pick_face(origin, direction, vertices, indices)
    actual = pick_face_bvh(
        origin, direction, vertices, indices,
        bvh=TriangleBVH(vertices, indices, leaf_size=2))
    assert actual.hit and expected.hit
    assert actual.triangle_index == expected.triangle_index
    assert abs(actual.distance - expected.distance) < 1e-8
    assert np.allclose(actual.point, expected.point)


def test_bvh_preserves_original_triangle_index_when_skipping_bad_data():
    from src.features.composite.render3d.mesh_picking import (
        TriangleBVH, pick_face_bvh)

    vertices = np.array([
        [0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0],
    ], dtype=np.float32)
    # Triangle 0 is invalid; triangle 1 is the valid hit.
    indices = np.array([99, 0, 1, 0, 1, 2], dtype=np.uint32)
    bvh = TriangleBVH(vertices, indices)
    hit = pick_face_bvh(
        np.array([0.2, 0.2, 1.0]), np.array([0.0, 0.0, -1.0]),
        vertices, indices, bvh=bvh)
    assert hit.hit
    assert hit.triangle_index == 1
    assert bvh.triangle_count == 1


def test_bvh_transform_returns_world_distance_with_non_uniform_scale():
    from src.features.composite.render3d.mesh_picking import pick_face_bvh

    vertices = np.array([
        [0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0],
    ], dtype=np.float32)
    indices = np.array([0, 1, 2], dtype=np.uint32)
    model = np.diag([2.0, 3.0, 1.0, 1.0]).astype(np.float64)
    model[0, 3] = 10.0
    hit = pick_face_bvh(
        np.array([10.5, 0.5, 4.0]), np.array([0.0, 0.0, -1.0]),
        vertices, indices, model_matrix=model)
    assert hit.hit
    assert abs(hit.distance - 4.0) < 1e-8
    assert np.allclose(hit.point, (10.5, 0.5, 0.0))


def test_mesh_pick_cache_reuses_and_rebuilds_by_revision():
    from src.features.composite.render3d.mesh_picking import MeshPickCache

    vertices, indices = _cube_mesh()
    cache = MeshPickCache(max_entries=2)
    first = cache.get("mesh", vertices, indices, revision=1)
    second = cache.get("mesh", vertices, indices, revision=1)
    assert first is second
    assert cache.hits == 1 and cache.misses == 1
    changed = cache.get("mesh", vertices, indices, revision=2)
    assert changed is not first
    assert cache.misses == 2


def test_bvh_benchmark_result_is_json_safe():
    import json
    from src.features.composite.render3d.mesh_picking import (
        benchmark_triangle_bvh)

    vertices, indices = _cube_mesh()
    origins = np.array([[0.0, 0.0, 5.0], [5.0, 5.0, 5.0]])
    directions = np.array([[0.0, 0.0, -1.0], [0.0, 0.0, -1.0]])
    result = benchmark_triangle_bvh(vertices, indices, origins, directions)
    assert result.triangle_count == 12
    assert result.ray_count == 2
    assert result.hit_count == 1
    assert result.query_p95_ms >= result.query_p50_ms >= 0.0
    assert json.loads(json.dumps(result.to_dict()))["bvh_node_count"] > 0


def test_component_descriptors_keep_gpu_id_and_instance_identity():
    from types import SimpleNamespace

    from src.features.composite.nodes.scene3d_node import Scene3DNode

    node = Scene3DNode.__new__(Scene3DNode)
    mesh = SimpleNamespace(
        vertices=np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0],
                           [0.0, 1.0, 0.0]], dtype=np.float32),
        indices=np.array([0, 1, 2], dtype=np.uint32),
        is_skinned=False,
    )
    model = SimpleNamespace(
        node_id="model",
        hidden_mesh_ids=(),
    )
    geo = SimpleNamespace(
        node_id="geo",
        mesh=mesh,
        is_lines=False,
        model_matrix=np.eye(4, dtype=np.float32),
        instance_enabled=True,
        instance_offsets=np.array([[0.0, 0.0, 0.0], [2.0, 0.0, 0.0]],
                                  dtype=np.float32),
        content_revision=1,
    )
    descriptors = node._build_component_pick_meshes(
        np.eye(4, dtype=np.float32), True,
        [{"mesh": mesh, "node_id": "model-part", "node_name": "part",
          "world_matrix": np.eye(4, dtype=np.float32)}],
        {}, [model], [geo], node_pick_ids={"model-part": 4, "geo": 8})
    assert descriptors[0]["pick_id"] == 4
    assert [item["pick_id"] for item in descriptors[1:]] == [8, 8]
    assert len({item["mesh_key"] for item in descriptors[1:]}) == 2


def test_screen_ray_uses_projection_after_view_composition():
    from src.features.composite.render3d.mesh_picking import screen_to_world_ray

    # A translated view matrix makes inverse(proj @ view) observably different
    # from the old inverse(view @ proj) order.
    from src.features.composite.render3d.math_utils import perspective

    proj = perspective(90.0, 1.0, 0.1, 100.0).astype(np.float64)
    view = np.eye(4, dtype=np.float64)
    view[0, 3] = -3.0
    origin, direction = screen_to_world_ray((50.0, 50.0), 100, 100, proj, view)
    assert np.allclose(origin[:2], (3.0, 0.0), atol=1e-8)
    assert abs(direction[0]) < 1e-8
    assert direction[2] < 0.0


def test_component_modes_share_face_hit_and_return_stable_ids():
    from src.features.composite.render3d.mesh_picking import (
        TriangleBVH, pick_component)

    vertices, indices = _cube_mesh()
    origin = np.array([0.0, 0.0, 5.0])
    direction = np.array([0.0, 0.0, -1.0])
    bvh = TriangleBVH(vertices, indices)
    polygon = pick_component(origin, direction, vertices, indices,
                             "polygon", bvh=bvh)
    point = pick_component(origin, direction, vertices, indices,
                           "point", bvh=bvh)
    edge = pick_component(origin, direction, vertices, indices,
                          "edge", bvh=bvh)
    assert polygon.hit and point.hit and edge.hit
    assert point.triangle_index == polygon.triangle_index
    assert edge.triangle_index == polygon.triangle_index
    assert point.vertex_index in set(indices[polygon.triangle_index * 3:
                                                polygon.triangle_index * 3 + 3])
    assert edge.edge == tuple(sorted(edge.edge))
    assert edge.edge[0] != edge.edge[1]


def test_selection_adapter_maps_all_component_modes():
    from src.features.composite.render3d.mesh_picking import ComponentHit
    from src.features.composite.render3d.selection import (
        SelectionState, apply_component_hit)

    state = SelectionState()
    assert apply_component_hit(
        state, "mesh", "point", ComponentHit(
            component_mode="point", triangle_index=0, vertex_index=4))
    assert state.vertex_ids == {"mesh": {4}}
    assert state.active_object == "mesh"
    assert apply_component_hit(
        state, "mesh", "edge", ComponentHit(
            component_mode="edge", triangle_index=0, edge=(8, 2)), "add")
    assert state.edge_ids == {"mesh": {(2, 8)}}
    assert apply_component_hit(
        state, "mesh", "polygon", ComponentHit(
            component_mode="polygon", triangle_index=7), "add")
    assert state.face_ids == {"mesh": {7}}
    assert state.selected_edge_count("mesh") == 1


def test_mesh_pick_cache_rebuilds_after_original_arrays_garbage_collected():
    """AUD-8916：原数组 GC 后不得凭 id 地址复用误命中旧 BVH。

    修复前签名只含 id/shape/dtype——原数组释放后同形状新数组复用地址
    即命中失效 BVH（拾取结果错乱）。修复后条目持弱引用，活性校验不过
    必然 miss 重建，与地址是否复用无关（确定性锁定契约）。
    """
    import gc

    from src.features.composite.render3d.mesh_picking import MeshPickCache

    cache = MeshPickCache(max_entries=2)
    vertices, indices = _cube_mesh()
    first = cache.get("mesh", vertices, indices, revision=1)

    # 同一数组仍存活 → 命中
    assert cache.get("mesh", vertices, indices, revision=1) is first
    assert cache.hits == 1

    # 丢弃原数组并强制回收；再以同内容新数组查询（无论地址是否复用）
    del vertices, indices
    gc.collect()
    fresh_vertices, fresh_indices = _cube_mesh()
    rebuilt = cache.get("mesh", fresh_vertices, fresh_indices, revision=1)

    assert rebuilt is not first
    assert cache.misses == 2
    # 新条目对存活的新数组恢复命中
    assert cache.get("mesh", fresh_vertices, fresh_indices, revision=1) \
        is rebuilt
    assert cache.hits == 2
