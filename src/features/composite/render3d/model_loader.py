"""
3D 模型加载器（软依赖）—— glTF/GLB/OBJ/STL/PLY/DAE（trimesh）+ FBX/3DS/blend（pyassimp）。

设计要点：
- 软依赖：trimesh / pyassimp 在函数内 import；缺失时 raise ModelLoaderUnavailable，
  节点层 catch 后 fallback 到 procedural 几何，绝不崩。
- 统一输出：返回 scene_graph.SceneGraphRoot（纯 numpy，无 GL 依赖）。
- 蒙皮/动画：trimesh 的 glTF 解析含 skin/joints 字段时尽力提取；不保证所有模型完整。

依赖说明（用户需自行安装）：
- glTF/OBJ/STL/PLY：pip install trimesh（纯 Python，无系统依赖）。
- FBX/3DS：pip install pyassimp + 系统级 libassimp（macOS: brew install assimp）。

参考：trimesh.load(force='scene') 返回 Scene；scene.graph.transforms.node_to_matrix
给层级 world matrix；scene.geometry 给 mesh 字典。
"""

import base64
import hashlib
import io
import json
import logging
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Callable, Iterable, List, Optional

import numpy as np

from .primitives import MeshData
from . import scene_graph as sg
from .model_import_report import (
    ModelImportReport,
    LOADER_BACKEND_TRIMESH,
    LOADER_BACKEND_PYASSIMP,
    LOADER_BACKEND_ASSIMP_CLI_GLB,
)

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from .skeleton import SkeletonData

logger = logging.getLogger(__name__)

# P4-3：嵌入贴图提取跳过标志（线程局部）。cache 命中且 textures_extracted=True
# 时设置，_persist_embedded_texture 检查此标志跳过 PNG 重编码（用已缓存的路径）。
# 用 threading.local 而非全局，保证多线程导入隔离。
_tls = threading.local()


def _set_skip_embedded_persist(skip: bool) -> None:
    """设置当前线程是否跳过嵌入贴图提取（cache 命中复用）。"""
    _tls.skip_embedded_persist = bool(skip)


def _should_skip_embedded_persist() -> bool:
    """当前线程是否应跳过嵌入贴图提取。"""
    return bool(getattr(_tls, "skip_embedded_persist", False))


def _set_cancel_check(hook: Optional[Callable[[], bool]]) -> None:
    """设置当前线程的协作取消钩子（传 ``None`` 清除）。

    AUD-9005：ModelImportService 的 worker 在解析前把 ``token.is_cancelled``
    设为钩子，使取消/服务 shutdown 能立刻终止在途的 assimp CLI 转换子进程，
    而不是等满 120s 超时（非 daemon worker 会在解释器退出时被逐个 join，
    在途转换会把应用退出阻塞最长约两分钟）。
    """
    _tls.cancel_check = hook


def _cancel_requested() -> bool:
    """当前线程的取消钩子是否已触发（未设置钩子时恒为 False）。"""
    hook: Optional[Callable[[], bool]] = getattr(_tls, "cancel_check", None)
    return bool(hook is not None and hook())


# AUD-9005：登记在途的 assimp CLI 导出进程（key=id(Popen)），供退出/取消
# 路径终止。服务 worker 均在自身线程内完成注册/移除；跨线程访问由锁保护。
_active_cli_exports: "dict[int, subprocess.Popen]" = {}
_active_cli_exports_lock = threading.Lock()


def _stop_cli_process(proc: subprocess.Popen) -> None:
    """SIGTERM（2s 宽限）→ SIGKILL，确保子进程退出并回收管道。"""
    for sig_name in ("terminate", "kill"):
        try:
            getattr(proc, sig_name)()
        except OSError:
            logger.debug("assimp CLI %s 失败", sig_name, exc_info=True)
        try:
            proc.communicate(timeout=2.0)
            return
        except (subprocess.TimeoutExpired, OSError, ValueError):
            continue
    logger.debug("assimp CLI 进程终止后仍无法回收输出", exc_info=True)


def _terminate_active_cli_exports() -> int:
    """终止所有在途的 assimp CLI 导出进程，返回发出终止信号的数量。

    供 ``ModelImportService.shutdown`` 调用：外部 terminate 后，等待循环中的
    ``communicate`` 会立刻返回（进程退出），worker 随之结束，应用退出不再被
    在途转换阻塞。
    """
    with _active_cli_exports_lock:
        procs = list(_active_cli_exports.values())
    terminated = 0
    for proc in procs:
        if proc.poll() is None:
            try:
                proc.terminate()
                terminated += 1
            except OSError:
                logger.debug("终止 assimp CLI 进程失败", exc_info=True)
    return terminated


def _run_assimp_export(assimp_bin: str, src_path: str, dst_path: str,
                       timeout_s: float = 120.0) -> subprocess.CompletedProcess:
    """运行 ``assimp export`` 并以轮询方式等待，支持取消与外部终止。

    相比 ``subprocess.run`` 的固定超时等待，这里每 0.25s 检查一次：

    - 进程结束 → 返回 ``CompletedProcess``（语义与 ``subprocess.run`` 一致）；
    - 协作取消（线程 cancel hook 触发）→ 终止子进程并抛
      ``ModelImportCancelled``；
    - 超过 ``timeout_s`` → kill 子进程并抛 ``subprocess.TimeoutExpired``
      （与原 ``subprocess.run(timeout=120)`` 的超时语义一致）。

    外部 ``terminate``（``_terminate_active_cli_exports``）无需显式检查：
    进程退出后下一次 ``communicate`` 立刻返回。
    """
    cmd = [assimp_bin, "export", src_path, dst_path, "-f", "glb2"]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    with _active_cli_exports_lock:
        _active_cli_exports[id(proc)] = proc
    deadline = time.monotonic() + float(timeout_s)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                _stop_cli_process(proc)
                raise subprocess.TimeoutExpired(cmd, int(timeout_s))
            try:
                # 文档化模式：超时后重试 communicate 不会丢失已产生的输出。
                stdout, stderr = proc.communicate(timeout=min(0.25, remaining))
                return subprocess.CompletedProcess(
                    cmd, proc.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                if _cancel_requested():
                    _stop_cli_process(proc)
                    raise ModelImportCancelled(
                        "assimp CLI 导出已随导入取消而终止") from None
    finally:
        with _active_cli_exports_lock:
            _active_cli_exports.pop(id(proc), None)


class ModelLoaderUnavailable(Exception):
    """模型加载失败：库未装、文件损坏、格式不支持、libassimp 缺失等。

    节点层应 catch 此异常并降级（fallback 到 procedural 几何），不中断渲染。
    """


class ModelImportCancelled(ModelLoaderUnavailable):
    """在途导入被取消（assimp CLI 子进程已随取消终止，结果不会交付）。

    AUD-9005：service worker 的取消令牌经 ``_set_cancel_check`` 接到 CLI
    等待循环；取消发生时子进程被终止并抛出本异常。service 侧按
    IMPORT_CANCELLED 归类，节点层按 ModelLoaderUnavailable 降级。
    """


# 由 trimesh 处理的扩展（glTF 系 + 通用 mesh）
_TRIMESH_EXTS = {".gltf", ".glb", ".obj", ".stl", ".ply", ".dae"}
# 由 pyassimp 处理的扩展（需要 native assimp）
_ASSIMP_EXTS = {".fbx", ".3ds", ".blend"}


def load_model(path: str, normalize: bool = False,
               flip_uv_v: bool = False) -> sg.SceneGraphRoot:
    """加载 3D 模型文件，返回场景图根。

    按 扩展名路由：
      - .gltf/.glb/.obj/.stl/.ply/.dae → trimesh
      - .fbx/.3ds/.blend → pyassimp（需系统 libassimp）

    Args:
        path: 模型文件绝对路径。
        normalize: True 时居中并缩放到预览友好的标准尺寸；False 时保留
            导入文件的原始单位尺寸，由调用方做单位换算。
        flip_uv_v: True 时把所有 mesh 的 UV.v 改为 ``1.0 - v``。用于修正
            FBX/OBJ 等 DCC 坐标和 OpenGL 贴图采样方向不一致的模型。

    Returns:
        SceneGraphRoot：含 root 节点树、skins、animations、bbox。

    Raises:
        ModelLoaderUnavailable: 库未装 / 文件不存在 / 解析失败。
    """
    if not path or not os.path.isfile(path):
        raise ModelLoaderUnavailable(f"模型文件不存在: {path!r}")
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in _TRIMESH_EXTS:
            root = _load_trimesh(path)
        elif ext in _ASSIMP_EXTS:
            root = _load_assimp(path)
        else:
            raise ModelLoaderUnavailable(f"不支持的模型格式: {ext!r}（{os.path.basename(path)}）")
    except ModelLoaderUnavailable:
        raise  # 透传
    except Exception as exc:
        # 把任何解析异常包成统一类型，节点层只需 catch 一种
        raise ModelLoaderUnavailable(
            f"解析模型失败 [{os.path.basename(path)}]: {exc}") from exc
    # 归一化：居中 + 缩放到合理大小（target_size=2，匹配默认相机距离）
    if normalize:
        sg.normalize_scene(root, target_size=2.0)
    uv_flip_failures = 0
    if flip_uv_v:
        uv_flip_failures = _flip_scene_uv_v(root.root)
    # F5：切线采用 lazy 策略——不在导入阶段预计算（避免大模型导入卡顿），
    # 而是在 Model3D._ensure_flat_meshes_gl 构建 VAO 时按需 compute_tangents
    # （仅材质含 normal map 的 mesh 才算，compute_tangents 有缓存不重复）。
    # F0：若 backend 未单独生成 report（老路径），在这里补一份默认 report。
    # backend 已在 _load_trimesh/_load_assimp 里通过 _finalize_report 设置；
    # 此处兜底确保任何路径返回的 root 都有 report（loader_backend 从 ext 推断）。
    if getattr(root, "import_report", None) is None:
        fallback_backend = (LOADER_BACKEND_TRIMESH if ext in _TRIMESH_EXTS
                            else LOADER_BACKEND_PYASSIMP)
        _finalize_report(root, fallback_backend, path)
    # AUD-9008：UV 翻转失败数写进导入报告（report 兜底之后再写，确保存在）。
    if uv_flip_failures and root.import_report is not None:
        root.import_report.add_warning(
            f"flip_uv_v: {uv_flip_failures} 个 mesh 的 UV 翻转失败，"
            "贴图方向可能不正确")
    logger.info("模型加载完成: %s（mesh=%d, skins=%d, anims=%d, backend=%s）",
                os.path.basename(path),
                root.import_report.mesh_count if root.import_report else 0,
                len(root.skins), len(root.animations),
                root.import_report.loader_backend if root.import_report else "?")
    return root


def _iter_meshes(node: sg.SceneNode):
    """递归 yield 场景树里所有非 None 的 mesh 节点。"""
    if node.mesh is not None:
        yield node
    for c in node.children:
        yield from _iter_meshes(c)


def _finalize_report(root_data: sg.SceneGraphRoot, backend: str,
                     source_path: str) -> ModelImportReport:
    """F0：从已构建的 SceneGraphRoot 派生导入报告计数并挂到 root_data.import_report。

    遍历场景树统计 mesh/triangle/vertex/material/texture/skin 数量；skin/animation
    从 SceneGraphRoot 顶层字段取。backend 标识哪个 loader 路径生效。返回的 report
    同时写回 root_data.import_report（供 Model3DNode/UI 读取）。

    Args:
        root_data: 已完成解析的 SceneGraphRoot（含 root 树、skins、animations）。
        backend: LOADER_BACKEND_* 常量（trimesh / pyassimp / assimp_cli_glb）。
        source_path: 源文件路径（写进 report.source_path）。

    Returns:
        填充了计数的 ModelImportReport（也已挂到 root_data.import_report）。
    """
    report = ModelImportReport(source_path=source_path, loader_backend=backend)
    mesh_count = tri_count = vert_count = 0
    material_count = texture_count = morph_target_count = 0

    def _visit(node: sg.SceneNode) -> None:
        nonlocal mesh_count, tri_count, vert_count, material_count
        nonlocal texture_count, morph_target_count
        if node.mesh is not None:
            mesh_count += 1
            m = node.mesh
            vert_count += int(m.vertices.shape[0])
            tri_count += int(m.indices.shape[0]) // 3
            morph_target_count += len(getattr(m, "morph_targets", {}) or {})
            # F0：无 UV 的 mesh 记 warning（影响纹理采样）
            if m.uvs.shape[0] != m.vertices.shape[0] or not np.any(m.uvs):
                report.add_warning(f"mesh '{node.name}' 无 UV 坐标")
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
    report.morph_target_count = morph_target_count
    report.skeleton_count = len(root_data.skins)
    report.bone_count = sum(s.bone_count for s in root_data.skins)
    report.animation_clip_count = len(root_data.animations)
    # skin_weight_count：有 bone_weights 的 mesh 顶点总和
    sw = 0
    for node in _iter_meshes(root_data.root):
        m = node.mesh
        if m.is_skinned and m.bone_weights is not None:
            sw += int(m.bone_weights.shape[0])
    report.skin_weight_count = sw
    root_data.import_report = report
    return report


def _flip_scene_uv_v(node: sg.SceneNode) -> int:
    """Flip UV V for every mesh in a scene tree in-place.

    Returns:
        翻转失败的 mesh 数（0=全部成功）。

    AUD-9008：``flip_uv_v`` 是用户显式传入的导入参数（修正贴图方向），失败
    必须可诊断——收窄捕获类型、记录 warning，并由调用方把失败数写进导入
    报告，不再静默吞错后产出上下颠倒的贴图。
    """
    failed = 0
    if node.mesh is not None and getattr(node.mesh, "uvs", None) is not None:
        try:
            uvs = np.array(node.mesh.uvs, dtype=np.float32, copy=True)
            if uvs.ndim == 2 and uvs.shape[1] >= 2:
                uvs[:, 1] = 1.0 - uvs[:, 1]
                node.mesh.uvs = np.ascontiguousarray(uvs, dtype=np.float32)
        except (TypeError, ValueError, MemoryError) as exc:
            failed += 1
            logger.warning("flip UV.v 失败（mesh '%s'）: %s", node.name, exc)
    for c in node.children:
        failed += _flip_scene_uv_v(c)
    return failed


def _precompute_tangents_for_normal_maps(root_data: sg.SceneGraphRoot) -> int:
    """F5：为带 normal map 的 mesh 预计算切线（normal mapping 前置条件）。

    导入模型的 VAO 升级为 tangent layout 前，必须先有切线数据。本函数遍历场景树，
    对材质含 normal_tex.path 的 mesh 调用 mesh.compute_tangents()。

    master_plan §21.8 F5-1/F5-2：MeshData tangent 持久化 + Model3D 上传 tangent VAO。
    本步实现 F5-1（持久化）；F5-2（VAO 上传）在 model3d_node._ensure_flat_meshes_gl。

    Args:
        root_data: 已解析的 SceneGraphRoot。

    Returns:
        预计算了切线的 mesh 数量（诊断用）。
    """
    count = 0

    def _visit(node: sg.SceneNode) -> None:
        nonlocal count
        if node.mesh is not None and node.material is not None:
            mat = node.material
            # 有 normal map 贴图 → 需要切线
            if mat.normal_tex.path:
                try:
                    if not node.mesh.has_tangents:
                        node.mesh.compute_tangents()
                        if node.mesh.has_tangents:
                            count += 1
                except Exception:
                    pass
        for c in node.children:
            _visit(c)

    _visit(root_data.root)
    return count


# ──────────────────────────────────────────────────────────────────────
# trimesh 后端（glTF / OBJ / STL / PLY / DAE）
# ──────────────────────────────────────────────────────────────────────

def _load_trimesh(path: str, source_path: str = "") -> sg.SceneGraphRoot:
    """用 trimesh 加载（glTF/OBJ/STL/PLY/DAE）。"""
    try:
        import trimesh
    except ImportError as exc:
        raise ModelLoaderUnavailable(
            "trimesh 未安装。请运行: pip install trimesh") from exc

    ext = os.path.splitext(path)[1].lower()
    # force='scene' 让多 mesh 文件返回 Scene；单 mesh 文件可能返回 Trimesh，
    # 这种情况包成单节点 Scene。
    try:
        loaded = trimesh.load(path, force="scene" if ext in {".gltf", ".glb", ".dae"} else None)
    except Exception as exc:
        raise ModelLoaderUnavailable(f"trimesh.load 失败: {exc}") from exc

    # 单 mesh（Trimesh 对象）→ 包成 Scene
    if isinstance(loaded, trimesh.Trimesh):
        loaded = trimesh.Scene(loaded)

    if not isinstance(loaded, trimesh.Scene):
        raise ModelLoaderUnavailable(
            f"trimesh 返回了非 Scene/Trimesh 类型: {type(loaded).__name__}")

    return _convert_trimesh_scene(loaded, source_path=source_path or path)


def _trimesh_geometry_world_matrix(scene, geom_name: str) -> Optional[np.ndarray]:
    """取几何体的世界矩阵（含祖先链变换，相对 scene base frame）。

    AUD-9006：trimesh 各版本取世界矩阵的 API 不一致——

    - 4.x：``scene.graph.get(node)`` 返回沿父链组合好的世界矩阵；几何名 →
      节点名映射在 ``scene.graph.geometry_nodes``。旧代码读
      ``transforms.local``（4.x 已不存在该属性，异常被静默吞掉后回退
      identity）。
    - 旧版（3.x）：``scene.graph.transforms.node_to_matrix`` 是
      ``{node: world matrix}`` dict。

    所有路径失败时返回 ``None``（调用方回退 identity，保持可加载）。
    """
    graph = getattr(scene, "graph", None)
    if graph is None:
        return None
    # 候选节点名：geometry_nodes 的映射 + 几何名本身（部分加载路径直接用
    # 几何名做 frame 名）。
    candidates: List[str] = []
    try:
        mapped = graph.geometry_nodes.get(geom_name)
        if mapped:
            candidates.extend(str(name) for name in mapped)
    except (AttributeError, TypeError, ValueError):
        pass
    candidates.append(geom_name)
    for node_name in candidates:
        try:
            matrix, _attached = graph.get(node_name)
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        arr = np.asarray(matrix, dtype=np.float32)
        if arr.shape == (4, 4):
            return arr
    # 旧版 trimesh 兜底：node_to_matrix（世界矩阵 dict）。
    ntm = getattr(getattr(graph, "transforms", None), "node_to_matrix", None)
    if isinstance(ntm, dict):
        for node_name in candidates:
            if node_name in ntm:
                arr = np.asarray(ntm[node_name], dtype=np.float32)
                if arr.shape == (4, 4):
                    return arr
    return None


def _convert_trimesh_scene(scene, source_path: str = "") -> sg.SceneGraphRoot:
    """把 trimesh.Scene 转成内部 SceneGraphRoot。

    trimesh Scene 结构：
      - scene.geometry: {name: Trimesh|Scene} 几何体字典
      - scene.graph.transforms.node_to_matrix: node_name → world matrix (4x4)
      - scene.graph.transforms.local: node_name → local matrix
      - scene.graph: SceneGraph，含父子边
    """
    root = sg.SceneNode(name="root")
    geometries = scene.geometry

    # 收集每个 geometry 节点的世界矩阵
    mesh_nodes: List[sg.SceneNode] = []
    for geom_name, geom in geometries.items():
        mesh = _convert_trimesh_geometry(geom)
        if mesh is None:
            continue
        # AUD-9006：所有 mesh 平铺挂 root，因此 local_transform 必须取该几何
        # 节点的世界矩阵（含祖先链变换）。trimesh 的 transforms.local 是相对
        # 父节点的矩阵（4.x 中该属性已不存在），当 local 用会丢失层级 glTF
        # 的祖先变换（部件错位/模型散架）。
        world = _trimesh_geometry_world_matrix(scene, geom_name)
        local = (world if world is not None
                 else np.eye(4, dtype=np.float32)).astype(np.float32).reshape(4, 4)
        # 提取材质
        material = _convert_trimesh_material(
            getattr(geom, "visual", None),
            source_path=source_path,
            material_key=geom_name,
        )
        # 提取蒙皮（glTF skin）
        skin = _extract_trimesh_skin(geom, geom_name, scene)
        node = sg.SceneNode(
            name=geom_name, mesh=mesh, material=material, skin=skin,
            local_transform=local,
        )
        mesh_nodes.append(node)
        root.children.append(node)

    # trimesh 的层级信息在 scene.graph 里（edges）。这里用 flat 结构（所有
    # mesh 直接挂 root）：每个 mesh 的 local_transform 已取「相对 scene 根的
    # 世界矩阵」（见上方 AUD-9006 注释），祖先链变换被吸收进 local，挂 root
    # 下即正确；父子联动的 world 由 flatten_scene 从 local 累乘。

    # 提取动画（glTF animations）
    animations = _extract_trimesh_animations(scene)

    # 计算 bbox（用 world matrix，但 local 已是相对 root，flatten_scene 会乘）
    bmin, bmax = sg.compute_bbox(root)
    root_root = sg.SceneGraphRoot(root=root, animations=animations)
    root_root.bbox_min = bmin
    root_root.bbox_max = bmax
    # 收集所有 skin 到 root.skins（供节点层查找骨骼）
    for n in mesh_nodes:
        if n.skin is not None:
            root_root.skins.append(n.skin)
    # F6：提取骨架层级（骨骼树 + rest pose；skeleton overlay 渲染用）
    try:
        root_root.skeletons = _extract_trimesh_skeletons(scene)
    except Exception:
        root_root.skeletons = []
    # F0：生成导入报告（统计 mesh/tri/mat/tex/skin/anim 数量 + 无 UV 警告）。
    _finalize_report(root_root, LOADER_BACKEND_TRIMESH, source_path)
    return root_root


def _convert_trimesh_geometry(geom) -> Optional[MeshData]:
    """trimesh.Trimesh/PointCloud → 内部 MeshData（只要顶点+索引）。"""
    # 仅处理三角形 mesh；PointCloud/Grids 无面，转成退化 mesh 会渲染异常，跳过
    if not hasattr(geom, "vertices") or not hasattr(geom, "faces"):
        return None
    verts = np.asarray(geom.vertices, dtype=np.float32)
    if verts.shape[0] == 0:
        return None
    faces = np.asarray(geom.faces, dtype=np.uint32)
    if faces.ndim != 2 or faces.shape[0] == 0:
        return None
    # 法线
    try:
        normals = np.asarray(geom.vertex_normals, dtype=np.float32)
        if normals.shape != verts.shape:
            normals = np.zeros_like(verts)
    except Exception:
        normals = np.zeros_like(verts)
    # UV
    uvs = np.zeros((verts.shape[0], 2), dtype=np.float32)
    visual = getattr(geom, "visual", None)
    if visual is not None:
        try:
            uv_attr = getattr(visual, "uv", None)
            if uv_attr is not None:
                uv_arr = np.asarray(uv_attr, dtype=np.float32)
                if uv_arr.shape[0] == verts.shape[0] and uv_arr.shape[1] >= 2:
                    uvs = uv_arr[:, :2]
        except Exception:
            pass
    # 索引展平（faces 可能是 (M,3)）
    indices = faces.reshape(-1).astype(np.uint32)
    morph_targets, morph_normals = _extract_trimesh_morph_targets(
        geom, int(verts.shape[0]))
    return MeshData(
        vertices=verts, normals=normals, uvs=uvs, indices=indices,
        morph_targets=morph_targets, morph_normals=morph_normals,
    )


def _extract_trimesh_morph_targets(
    geom: Any,
    vertex_count: int,
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """Extract position/normal morph deltas from tolerant geometry metadata.

    Trimesh does not expose one stable public morph API across its supported
    importers.  Some project-specific glTF adapters nevertheless preserve
    targets in ``metadata['morph_targets']``.  Accept the common glTF names
    and ignore malformed entries instead of making the entire model fail.
    """
    meta = getattr(geom, "metadata", None)
    raw = meta.get("morph_targets") if isinstance(meta, Mapping) else None
    if raw is None and isinstance(meta, Mapping):
        raw = meta.get("morph")
    if raw is None:
        raw = getattr(geom, "morph_targets", None)
    if raw is None:
        return {}, {}

    if isinstance(raw, Mapping):
        entries = raw.items()
    elif isinstance(raw, (list, tuple)):
        entries = ((item.get("name", f"morph_{index}"), item)
                   for index, item in enumerate(raw)
                   if isinstance(item, Mapping))
    else:
        return {}, {}

    positions: dict[str, np.ndarray] = {}
    normals: dict[str, np.ndarray] = {}
    for raw_name, target in entries:
        name = str(raw_name).strip()
        if not name:
            continue
        if isinstance(target, Mapping):
            position = next((target[key] for key in (
                "position_delta", "POSITION", "positions", "vertices")
                if key in target), None)
            normal = next((target[key] for key in (
                "normal_delta", "NORMAL", "normals")
                if key in target), None)
        else:
            position = target
            normal = None
        try:
            position_array = np.asarray(position, dtype=np.float32)
            if position_array.shape != (vertex_count, 3):
                continue
            positions[name] = np.ascontiguousarray(position_array)
            if normal is not None:
                normal_array = np.asarray(normal, dtype=np.float32)
                if normal_array.shape == (vertex_count, 3):
                    normals[name] = np.ascontiguousarray(normal_array)
        except (TypeError, ValueError):
            continue
    return positions, normals


def _convert_trimesh_material(visual, source_path: str = "",
                              material_key: str = "") -> Optional[sg.MaterialData]:
    """trimesh visual.material → 内部 MaterialData（PBR 标量 + 纹理描述符）。

    Model3D 材质升级：提取 glTF 材质纹理（baseColor/normal/metallicRoughness/
    occlusion/emissive）的路径到 TextureDescriptor。trimesh 把这些存在
    material 属性或 visual 的 image/texture 上——尽力提取，失败返回标量 fallback。
    """
    if visual is None:
        return None
    material = getattr(visual, "material", None)
    if material is None:
        return None
    try:
        # glTF PBRMaterial：baseColorFactor / metallicFactor / roughnessFactor
        base_color = getattr(material, "baseColorFactor", None)
        metallic = getattr(material, "metallicFactor", None)
        roughness = getattr(material, "roughnessFactor", None)
        # fallback：单色 diffuse / color
        if base_color is None:
            diffuse = getattr(material, "diffuse", None) or getattr(material, "color", None)
            if diffuse is not None:
                base_color = _normalize_color_factor(diffuse)
        if base_color is None:
            base_color = np.array([0.8, 0.8, 0.8], dtype=np.float32)
        else:
            base_color = _normalize_color_factor(base_color)

        # ── Model3D 材质升级：纹理描述符提取 ──
        # trimesh 把 glTF 纹理信息存在 material 的 *_texture / image 属性，或
        # visual 的 uv/image 上。这里尽力提取路径（glTF URI 或 image 文件名）。
        # 失败（属性不存在/格式不同）→ 空描述符，fallback 到标量。
        base_color_tex = sg.TextureDescriptor(role=sg.TEXTURE_ROLE_COLOR)
        normal_tex = sg.TextureDescriptor(role=sg.TEXTURE_ROLE_DATA)
        orm_tex = sg.TextureDescriptor(role=sg.TEXTURE_ROLE_DATA)
        ao_tex = sg.TextureDescriptor(role=sg.TEXTURE_ROLE_DATA)
        emissive_tex = sg.TextureDescriptor(role=sg.TEXTURE_ROLE_COLOR)
        try:
            base_color_tex.path = _extract_texture_path(
                material, "baseColorTexture", visual, source_path, material_key)
            normal_tex.path = _extract_texture_path(
                material, "normalTexture", visual, source_path, material_key)
            orm_tex.path = _extract_texture_path(
                material, "metallicRoughnessTexture", visual, source_path, material_key)
            ao_tex.path = _extract_texture_path(
                material, "occlusionTexture", visual, source_path, material_key)
            emissive_tex.path = _extract_texture_path(
                material, "emissiveTexture", visual, source_path, material_key)
        except Exception:
            pass
        # emissive 标量
        emissive = getattr(material, "emissiveFactor", None)
        emissive = (np.asarray(emissive, dtype=np.float32)[:3]
                    if emissive is not None else np.array([0.0, 0.0, 0.0], dtype=np.float32))

        return sg.MaterialData(
            base_color=base_color,
            metallic=float(metallic) if metallic is not None else 0.0,
            roughness=_roughness_factor(roughness, has_roughness_map=bool(orm_tex.path)),
            base_color_tex=base_color_tex,
            normal_tex=normal_tex,
            orm_tex=orm_tex,
            ao_tex=ao_tex,
            emissive_tex=emissive_tex,
            emissive=emissive,
            material_id=str(material_key or getattr(material, "name", "") or ""),
            name=str(getattr(material, "name", "") or material_key or ""),
        )
    except Exception as exc:
        # AUD-9015：不再整体静默丢材质（原 except → return None 连已成功
        # 提取的纹理路径一起丢、report 无记录，且与 docstring「失败返回标量
        # fallback」不符）。记 warning 后返回默认标量材质。
        logger.warning(
            "材质转换失败（trimesh, key=%s）：%s —— 返回默认标量材质",
            material_key, exc)
        return _default_material_data(material_key)


def _default_material_data(material_key: str = "") -> sg.MaterialData:
    """默认标量材质（0.8 灰 base color / metallic 0 / roughness 0.5）。

    AUD-9015：材质转换抛错时的 fallback（对齐 _convert_*_material 的
    docstring 承诺），无任何纹理描述符。
    """
    return sg.MaterialData(
        base_color=np.array([0.8, 0.8, 0.8], dtype=np.float32),
        metallic=0.0,
        roughness=0.5,
        material_id=str(material_key or ""),
        name=str(material_key or ""),
    )


def _normalize_color_factor(value) -> np.ndarray:
    """Normalize color-like arrays from either 0..1 or 0..255 into RGB 0..1."""
    arr = np.asarray(value, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return np.array([0.8, 0.8, 0.8], dtype=np.float32)
    arr = arr[:3] if arr.size >= 3 else np.pad(arr, (0, 3 - arr.size), constant_values=1.0)
    if float(np.nanmax(arr)) > 1.0:
        arr = arr / 255.0
    return np.clip(arr, 0.0, 1.0).astype(np.float32)


def _roughness_factor(value, has_roughness_map: bool = False) -> float:
    """Return a useful roughness factor for imported PBR materials."""
    if value is None:
        return 1.0 if has_roughness_map else 0.5
    try:
        roughness = float(value)
    except Exception:
        return 1.0 if has_roughness_map else 0.5
    if roughness <= 0.0 and has_roughness_map:
        return 1.0
    return max(0.01, min(1.0, roughness))


def _extract_texture_path(material, attr_name: str, visual=None,
                          source_path: str = "", material_key: str = "") -> str:
    """从 trimesh material/visual 提取纹理路径（尽力，失败返回 ""）。

    trimesh glTF 纹理可能存为：material.<attr>（Texture 或 image 对象）、
    visual.image（PIL Image）、或 material 的 image 属性。这里返回可定位的
    路径或标识；render 线程的材质纹理加载器据此解析（模型文件旁相对路径）。
    """
    tex = getattr(material, attr_name, None)
    if tex is None:
        return ""
    # 常见形态：tex 是 trimesh.visual.texture.Texture，含 .image 或 .path。
    # GLB/assimp fallback 常见形态：tex 直接是 PIL Image，没有路径；这种
    # 需要落到缓存文件，否则 render 线程无法按 TextureDescriptor 上传。
    for path_attr in ("path", "uri", "filename", "name"):
        p = getattr(tex, path_attr, None)
        if isinstance(p, str) and p and (os.path.isfile(p) or path_attr != "name"):
            return p
    # tex 本身可能是 image（PIL）或 dict
    if isinstance(tex, str):
        return tex
    embedded = _extract_embedded_image(tex)
    if embedded is None:
        embedded = _extract_embedded_image(getattr(tex, "image", None))
    if embedded is None and visual is not None:
        embedded = _extract_embedded_image(getattr(visual, "image", None))
    if embedded is not None:
        return _persist_embedded_texture(
            embedded, source_path=source_path, attr_name=attr_name,
            material_key=material_key or str(getattr(material, "name", "")),
            material=material,
        )
    return ""


def _extract_embedded_image(value):
    """Return a PIL-like image object if value is an embedded texture image."""
    if value is None:
        return None
    if hasattr(value, "save") and hasattr(value, "size"):
        return value
    return None


def _persist_embedded_texture(image, source_path: str, attr_name: str,
                              material_key: str = "", material=None) -> str:
    """Persist an embedded material image to the user cache and return its path.

    P4-3：统一用 _texture_digest（图像原始字节 + source_id + material_key + attr）
    命名文件。cache 命中（textures_extracted=True）时先检查文件是否存在，存在则
    直接返回跳过 PNG 编码；不存在才做 PNG 编码写入。第二次加载同模型省 encoding。
    """
    try:
        source_id = os.path.abspath(source_path or "")
        try:
            st = os.stat(source_id)
            source_id = f"{source_id}:{st.st_mtime_ns}:{st.st_size}"
        except OSError:
            pass
        cache_dir = _embedded_texture_cache_dir(source_path)
        os.makedirs(cache_dir, exist_ok=True)
        safe_attr = "".join(ch if ch.isalnum() else "_" for ch in attr_name).strip("_")

        # P4-3：统一用图像原始字节 digest 命名（不用 PNG data，可预计算）。
        digest_hex = _texture_digest(image, source_id, material_key, attr_name)
        out_path = os.path.join(cache_dir, f"{digest_hex}_{safe_attr}.png")

        # P4-3：cache 命中标志下，文件已存在则跳过 PNG 编码直接返回。
        if _should_skip_embedded_persist() and os.path.isfile(out_path):
            return out_path

        # 文件不存在（首次提取）或 cache 未命中：做 PNG 编码写入。
        img = _prepare_embedded_texture_image(image, attr_name, material)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        if not os.path.isfile(out_path):
            with open(out_path, "wb") as fh:
                fh.write(data)
        return out_path
    except Exception as exc:
        logger.warning("嵌入材质贴图缓存失败 attr=%s: %s", attr_name, exc)
        return ""


def _texture_digest(image, source_id: str, material_key: str,
                    attr_name: str) -> str:
    """P4-3：用图像原始字节（非 PNG 编码）算 sha1 digest，命名缓存文件。

    比 PNG data digest 快（tobytes 无压缩），且可在编码前预计算，支持命中跳过。
    """
    d = hashlib.sha1()
    d.update(source_id.encode("utf-8", "ignore"))
    d.update(str(material_key).encode("utf-8", "ignore"))
    d.update(str(attr_name).encode("utf-8", "ignore"))
    try:
        if hasattr(image, "tobytes"):
            d.update(bytes(image.tobytes()))
        elif isinstance(image, (bytes, bytearray)):
            d.update(bytes(image))
        if hasattr(image, "size"):
            d.update(str(image.size).encode("utf-8", "ignore"))
    except Exception:
        pass
    return d.hexdigest()[:20]


def _prepare_embedded_texture_image(image, attr_name: str, material=None):
    """Prepare embedded images for the shader's expected PBR channel layout."""
    img = image.copy() if hasattr(image, "copy") else image
    if attr_name == "metallicRoughnessTexture":
        try:
            from PIL import Image
            if getattr(img, "mode", "") in {"1", "L", "LA", "I", "I;16", "F"}:
                roughness = img.convert("L")
                ao = Image.new("L", roughness.size, 255)
                metallic_factor = float(getattr(material, "metallicFactor", 0.0) or 0.0)
                metallic = (roughness if metallic_factor > 0.0
                            else Image.new("L", roughness.size, 0))
                return Image.merge("RGB", (ao, roughness, metallic))
        except Exception:
            pass
    if getattr(img, "mode", "") not in {"RGB", "RGBA", "L"}:
        try:
            img = img.convert("RGBA")
        except Exception:
            pass
    return img


def _embedded_texture_cache_dir(source_path: str = "") -> str:
    """Return the extraction directory for embedded model textures.

    Prefer a Cinema4D/FBX-style sibling ``<model>.fbm`` directory so users can
    inspect and relink textures next to the imported model. Fall back to the
    user cache only if the source path is unavailable or not writable.
    """
    if source_path:
        try:
            abs_source = os.path.abspath(source_path)
            model_dir = os.path.dirname(abs_source)
            model_name = os.path.splitext(os.path.basename(abs_source))[0]
            if model_dir and model_name:
                fbm_dir = os.path.join(model_dir, f"{model_name}.fbm")
                os.makedirs(fbm_dir, exist_ok=True)
                return fbm_dir
        except OSError:
            pass

    home = os.path.expanduser("~")
    if home and os.path.isdir(home):
        return os.path.join(home, "Library", "Caches", "ToposColor", "model_textures")
    return os.path.join(tempfile.gettempdir(), "ToposColor", "model_textures")


def _extract_trimesh_skin(geom, geom_name: str, scene) -> Optional[sg.SkinData]:
    """从 trimesh geometry 提取 glTF 蒙皮数据（若存在）。

    trimesh 把 glTF skin 存在 geometry.metadata 或几何体属性里；具体字段名因版本
    而异。这里尽力提取；失败返回 None（节点层按非蒙皮渲染）。
    """
    try:
        # 常见位置：geom.metadata['skin'] 或 scene 里的 extras
        meta = getattr(geom, "metadata", None) or {}
        skin_data = meta.get("skin") if isinstance(meta, dict) else None
        if skin_data is None:
            return None
        bone_names = skin_data.get("joints", [])
        ibm = np.asarray(skin_data.get("inverseBindMatrices", []), dtype=np.float32)
        if ibm.ndim != 3 or ibm.shape[0] == 0:
            return None
        bi = np.asarray(skin_data.get("bone_indices",
                                      np.zeros((geom.vertices.shape[0], 4))), dtype=np.uint32)
        bw = np.asarray(skin_data.get("bone_weights",
                                      np.zeros((geom.vertices.shape[0], 4))), dtype=np.float32)
        if bi.shape[0] != geom.vertices.shape[0]:
            return None
        return sg.SkinData(
            bone_names=list(bone_names),
            inverse_bind_matrices=ibm.reshape(-1, 4, 4),
            bone_indices=bi.reshape(-1, 4),
            bone_weights=bw.reshape(-1, 4),
        )
    except Exception:
        return None


def _extract_trimesh_animations(scene) -> List[sg.AnimationClip]:
    """从 trimesh Scene 提取 glTF 动画片段（若存在）。

    trimesh 把 glTF animations 存在 scene.metadata['animations'] 或
    scene.graph 里。字段名因版本而异，尽力提取。
    """
    clips: List[sg.AnimationClip] = []
    try:
        meta = getattr(scene, "metadata", None) or {}
        anims = meta.get("animations") if isinstance(meta, dict) else None
        if not anims:
            return clips
        for anim in anims:
            name = anim.get("name", "anim")
            channels = []
            duration = 0.0
            for ch in anim.get("channels", []):
                node_name = ch.get("node", "")
                times = np.asarray(ch.get("times", []), dtype=np.float32)
                if times.size == 0:
                    continue
                duration = max(duration, float(times[-1]))
                channels.append(sg.AnimationChannel(
                    node_name=node_name,
                    times=times,
                    translations=np.asarray(ch.get("translation",
                                                    np.zeros((times.size, 3))), dtype=np.float32),
                    rotations=np.asarray(ch.get("rotation",
                                                np.tile([0, 0, 0, 1], (times.size, 1))),
                                         dtype=np.float32),
                    scales=np.asarray(ch.get("scale",
                                             np.ones((times.size, 3))), dtype=np.float32),
                ))
            clips.append(sg.AnimationClip(name=name, duration=duration, channels=channels))
    except Exception as e:
        logger.warning("Animation extraction failed (trimesh): %s", e)
    return clips


def _extract_trimesh_skeletons(scene) -> List["SkeletonData"]:
    """从 trimesh Scene 提取 glTF 骨架层级（SkeletonData，F6 集成）。

    遍历 scene.metadata['skins']，每个 skin 构建一个 SkeletonData：
    - joints 节点树 → build_skeleton_from_joints（计算 rest_world）
    - bone_names 去重（unique_bone_names，防 glTF 重名）

    失败返回空列表（不影响主加载流程）。
    """
    out: List["SkeletonData"] = []
    try:
        from .skeleton import _JointNode, build_skeleton_from_joints, unique_bone_names
    except Exception:
        return out
    try:
        meta = getattr(scene, "metadata", None) or {}
        skins = meta.get("skins") if isinstance(meta, dict) else None
        if not skins:
            return out

        # scene.graph 提供节点变换（glTF nodes 的 TRS/matrix）
        # 注意：node_to_matrix 给的是 WORLD 矩阵；骨骼累乘需要 LOCAL。
        # trimesh transforms.local 才是相对父节点的 local matrix。
        graph = getattr(scene, "graph", None)
        local_transforms = {}
        world_transforms = {}
        if graph is not None:
            try:
                # 优先用 local（骨骼层级累乘用）
                loc = getattr(graph.transforms, "local", None)
                if loc:
                    local_transforms = dict(loc)
                # world 作为 fallback（无 hierarchy 时直接用 world 当 rest_world）
                ntm = getattr(graph.transforms, "node_to_matrix", None)
                if ntm:
                    world_transforms = dict(ntm)
            except Exception:
                pass

        for skin in skins:
            name = skin.get("name", "skin")
            joint_names_raw = skin.get("joints", [])
            if not joint_names_raw:
                continue
            # 去重骨骼名
            unique_names = unique_bone_names([str(j) for j in joint_names_raw])

            # 先解析 hierarchy（需在构建 _JointNode 前确定 parent，
            # 且 parent_joint_index 必须指向更早出现的 joint——build_skeleton_from_joints
            # 的约束）。这里把 hierarchy 拓扑排序保证 parent 先于 child。
            hierarchy_raw = skin.get("hierarchy", {})
            # 构建 parent 映射（用去重后的名）
            raw_to_unique = {joint_names_raw[i]: unique_names[i]
                             for i in range(len(joint_names_raw))}
            parent_map: dict = {}  # unique_name → unique_parent_name
            if isinstance(hierarchy_raw, dict):
                for jname_raw, parent_raw in hierarchy_raw.items():
                    jn = raw_to_unique.get(str(jname_raw))
                    pn = raw_to_unique.get(str(parent_raw))
                    if jn is not None and pn is not None:
                        parent_map[jn] = pn

            # 拓扑排序：保证 parent 的 index < child 的 index
            ordered_names: List[str] = []
            placed: set = set()
            # AUD-9011：成员测试/索引查找预先建 set + dict（原实现每节点重建
            # 全集 set + 每次线性 index，累计 O(n²)，大骨架导入被拖慢）。
            unique_set = set(unique_names)
            raw_index_of = {nm: i for i, nm in enumerate(unique_names)}

            def _place(nm: str) -> None:
                if nm in placed:
                    return
                pn = parent_map.get(nm)
                if pn is not None and pn not in placed and pn in unique_set:
                    _place(pn)  # 先放 parent
                placed.add(nm)
                ordered_names.append(nm)
            for nm in unique_names:
                _place(nm)
            # name → index（拓扑序）
            name_to_idx = {nm: i for i, nm in enumerate(ordered_names)}

            # 构建 _JointNode（按拓扑序，保证 parent 已处理）
            joints: List[_JointNode] = []
            hit_count = 0
            for nm in ordered_names:
                raw_idx = raw_index_of[nm]  # 在原始 joints 数组的位置（O(1)）
                raw_name = joint_names_raw[raw_idx]
                # 优先用 local transform；找不到则 fallback world（rest_world 直接给）
                local = local_transforms.get(raw_name)
                if local is None:
                    # fallback：world 矩阵当 rest_world 用（parent_index=-1，跳过累乘）
                    local = world_transforms.get(raw_name, np.eye(4, dtype=np.float32))
                else:
                    hit_count += 1
                pn = parent_map.get(nm)
                pidx = name_to_idx[pn] if (pn is not None and pn in name_to_idx) else -1
                joints.append(_JointNode(
                    name=nm,
                    local_matrix=np.asarray(local, dtype=np.float32).reshape(4, 4),
                    parent_joint_index=pidx,
                ))
            if hit_count == 0 and local_transforms:
                import logging
                logging.getLogger(__name__).debug(
                    "_extract_trimesh_skeletons: joint 名与 scene 节点名不匹配，"
                    "全部 fallback 到 world/identity（骨骼位置可能不准）")

            skel = build_skeleton_from_joints(name, joints)
            if not skel.is_empty:
                out.append(skel)
    except Exception as e:
        logger.warning("Skeleton extraction failed (trimesh): %s", e)
    return out

# ──────────────────────────────────────────────────────────────────────
# pyassimp 后端（FBX / 3DS / blend）—— 需系统 libassimp
# ──────────────────────────────────────────────────────────────────────

def _load_assimp(path: str) -> sg.SceneGraphRoot:
    """用 pyassimp 加载（FBX/3DS/blend）。需系统装 libassimp。"""
    try:
        import pyassimp
    except ImportError as exc:
        try:
            return _load_assimp_cli_static_fallback(path, exc)
        except ModelLoaderUnavailable:
            pass
        raise ModelLoaderUnavailable(
            "pyassimp 未安装。FBX/3DS 导入需要: pip install pyassimp") from exc

    # libassimp 是 native 依赖；pyassimp.load 在缺失时会抛异常
    try:
        with pyassimp.load(path) as scene:
            root = _convert_assimp_scene(scene)
    except ModelLoaderUnavailable:
        raise
    except Exception as exc:
        try:
            return _load_assimp_cli_static_fallback(path, exc)
        except ModelLoaderUnavailable as fallback_exc:
            logger.warning("assimp CLI fallback failed for %s: %s", path, fallback_exc)
        msg = str(exc).lower()
        if "assimp" in msg or "library" in msg or "load" in msg:
            raise ModelLoaderUnavailable(
                "libassimp 未安装或损坏。FBX 需要: brew install assimp（macOS）") from exc
        raise ModelLoaderUnavailable(f"pyassimp 加载失败: {exc}") from exc
    return root


def _load_assimp_cli_static_fallback(path: str, original_exc: Exception) -> sg.SceneGraphRoot:
    """Fallback for FBX files that pyassimp cannot initialize.

    Some valid FBX files crash pyassimp's Python object initialization around
    sparse/null bone arrays, while the system ``assimp`` CLI can still import
    them. Export to a temporary GLB and load it through the existing trimesh
    path so the model remains visible. Geometry/materials come from trimesh;
    animation and skin accessors are restored from the exported GLB when
    available. Cameras and lights remain unsupported on this fallback path.

    Milestone E / P4-2：assimp CLI 转换的 GLB 持久化缓存（跨进程）。二次加载同一
    FBX 直接复用缓存的 GLB，跳过 120s 超时的 CLI 转换。缓存 key = 源 path+mtime+
    size+版本（normalize/flip 是 SceneGraph 层后处理，不影响 GLB 转换产物，故不进
    key）。源文件变化自动失效（mtime_ns/size 不匹配）。
    """
    assimp_bin = shutil.which("assimp")
    if not assimp_bin:
        raise ModelLoaderUnavailable(
            f"pyassimp 加载失败，且未找到 assimp CLI fallback: {original_exc}")

    # P4-2：先查持久缓存——命中则直接加载缓存的 GLB，跳过 CLI 转换。
    cache = None
    cached_glb = None
    _cached_textures_extracted = False
    try:
        from .model_import_cache import build_cache_key, get_default_cache
        cache = get_default_cache()
        if cache.enabled:
            key = build_cache_key(
                path, normalize=False, flip_uv_v=False,
                axis_mode="auto", uv_v_mode="auto")
            entry = cache.get(key)
            if entry is not None and entry.fallback_glb:
                cached_glb = cache.file_path(entry.fallback_glb)
                # P4-3：读取 textures_extracted 标记，命中则跳过嵌入贴图 PNG 编码。
                _cached_textures_extracted = bool(entry.textures_extracted)
    except Exception:
        # 缓存查询失败不应阻断加载；回退到正常转换路径。
        cache = None

    if cached_glb and os.path.isfile(cached_glb):
        logger.info("FBX fallback GLB 命中缓存: %s", os.path.basename(path))
        # P4-3：cache 命中 + textures_extracted=True → 设跳过标志，
        # _persist_embedded_texture 检查文件存在直接返回，跳过 PNG 重编码。
        if _cached_textures_extracted:
            _set_skip_embedded_persist(True)
        try:
            root = _load_trimesh(cached_glb, source_path=path)
        finally:
            _set_skip_embedded_persist(False)
        animation_preserved, skinning_preserved = (
            _restore_gltf_animation_and_skin(root, cached_glb))
        _mark_fbx_cli_fallback_report(
            root, original_exc,
            animation_preserved=animation_preserved,
            skinning_preserved=skinning_preserved,
        )
        return root

    try:
        with tempfile.TemporaryDirectory(prefix="topos_fbx_fallback_") as tmpdir:
            glb_path = os.path.join(tmpdir, "fallback.glb")
            # AUD-9005：可取消的等待（原 subprocess.run(timeout=120) 无法被
            # 取消/shutdown 中断，非 daemon worker 会被它阻塞应用退出）。
            proc = _run_assimp_export(assimp_bin, path, glb_path, timeout_s=120.0)
            if proc.returncode != 0 or not os.path.isfile(glb_path):
                detail = (proc.stderr or proc.stdout or "").strip()
                raise ModelLoaderUnavailable(
                    f"assimp CLI fallback 导出失败: {detail[:300]}")
            logger.warning(
                "pyassimp failed for %s; loaded GLB via assimp CLI fallback: %s",
                os.path.basename(path), original_exc)
            # P4-3：先 _load_trimesh 提取贴图，再 cache.put 标记 textures_extracted。
            # 这样首次加载完成后，下次同 key 命中可跳过 PNG 编码。
            root = _load_trimesh(glb_path, source_path=path)
            # P4-2：转换成功 → 写入持久缓存（下次直接命中，省 CLI 转换）。
            # P4-3：textures_extracted=True 标记贴图已提取（闭环）。
            if cache is not None and cache.enabled:
                try:
                    cache.put(key, fallback_glb=glb_path,
                              textures_extracted=True)
                except Exception:
                    logger.debug("FBX fallback GLB 缓存写入失败", exc_info=True)
            animation_preserved, skinning_preserved = (
                _restore_gltf_animation_and_skin(root, glb_path))
            _mark_fbx_cli_fallback_report(
                root, original_exc,
                animation_preserved=animation_preserved,
                skinning_preserved=skinning_preserved,
            )
            return root
    except subprocess.TimeoutExpired as exc:
        raise ModelLoaderUnavailable("assimp CLI fallback 导出超时") from exc


def _load_gltf_document_and_buffers(path: str) -> tuple[dict, list[bytes]]:
    """Read a GLB/GLTF document and its binary buffers.

    The normal FBX fallback uses an animated GLB produced by assimp.  Trimesh
    is intentionally kept as the geometry/material reader, but it does not
    expose the GLB animation and skin accessors.  This small accessor reader
    restores those two data classes without introducing another heavyweight
    importer dependency.
    """
    with open(path, "rb") as handle:
        raw = handle.read()

    binary_chunk = b""
    if raw[:4] == b"glTF":
        if len(raw) < 12:
            raise ValueError("GLB header is truncated")
        _magic, version, total_length = struct.unpack_from("<4sII", raw, 0)
        if version != 2 or total_length > len(raw):
            raise ValueError("unsupported GLB version or length")
        document = None
        offset = 12
        while offset + 8 <= total_length:
            chunk_length, chunk_type = struct.unpack_from("<I4s", raw, offset)
            start = offset + 8
            end = start + int(chunk_length)
            if end > len(raw):
                raise ValueError("GLB chunk is truncated")
            chunk = raw[start:end]
            if chunk_type == b"JSON":
                document = json.loads(chunk.rstrip(b"\x00 \t\r\n").decode("utf-8"))
            elif chunk_type == b"BIN\x00":
                binary_chunk = chunk
            offset = end
        if not isinstance(document, dict):
            raise ValueError("GLB has no JSON chunk")
    else:
        document = json.loads(raw.decode("utf-8"))

    buffers: list[bytes] = []
    base_dir = os.path.dirname(os.path.abspath(path))
    for index, buffer_info in enumerate(document.get("buffers", [])):
        uri = buffer_info.get("uri")
        if not uri:
            if index != 0:
                raise ValueError(f"buffer {index} has no URI in a non-first slot")
            buffers.append(binary_chunk)
            continue
        if uri.startswith("data:"):
            _header, encoded = uri.split(",", 1)
            buffers.append(base64.b64decode(encoded))
        else:
            with open(os.path.join(base_dir, uri), "rb") as handle:
                buffers.append(handle.read())
    return document, buffers


_GLTF_COMPONENT_DTYPES = {
    5120: np.dtype("<i1"),
    5121: np.dtype("<u1"),
    5122: np.dtype("<i2"),
    5123: np.dtype("<u2"),
    5125: np.dtype("<u4"),
    5126: np.dtype("<f4"),
}
_GLTF_TYPE_COMPONENTS = {
    "SCALAR": 1,
    "VEC2": 2,
    "VEC3": 3,
    "VEC4": 4,
    "MAT2": 4,
    "MAT3": 9,
    "MAT4": 16,
}


def _read_gltf_accessor(document: dict, buffers: list[bytes], index: int) -> np.ndarray:
    """Decode one glTF accessor, including interleaved buffer views."""
    accessor = document["accessors"][int(index)]
    count = int(accessor.get("count", 0))
    component_type = int(accessor["componentType"])
    dtype = _GLTF_COMPONENT_DTYPES[component_type]
    component_count = _GLTF_TYPE_COMPONENTS[accessor["type"]]
    if count <= 0:
        return np.empty((0, component_count), dtype=dtype)
    view_index = accessor.get("bufferView")
    if view_index is None:
        return np.zeros((count, component_count), dtype=dtype)
    view = document["bufferViews"][int(view_index)]
    buffer_data = buffers[int(view.get("buffer", 0))]
    item_size = dtype.itemsize * component_count
    offset = int(view.get("byteOffset", 0)) + int(accessor.get("byteOffset", 0))
    stride = int(view.get("byteStride", item_size))
    if stride == item_size:
        values = np.frombuffer(
            buffer_data, dtype=dtype, count=count * component_count,
            offset=offset).reshape(count, component_count)
    else:
        values = np.ndarray(
            shape=(count, component_count), dtype=dtype, buffer=buffer_data,
            offset=offset, strides=(stride, dtype.itemsize))
    values = np.array(values, copy=True)
    if accessor.get("normalized") and np.issubdtype(values.dtype, np.integer):
        if np.issubdtype(values.dtype, np.unsignedinteger):
            values = values.astype(np.float32) / float(np.iinfo(values.dtype).max)
        else:
            info = np.iinfo(values.dtype)
            values = np.maximum(values.astype(np.float32) / max(1.0, float(info.max)), -1.0)
    if accessor["type"].startswith("MAT"):
        side = int(accessor["type"][3:])
        values = values.reshape(count, side, side).transpose(0, 2, 1)
    return values


def _gltf_node_local_transform(node: dict) -> np.ndarray:
    """Convert a glTF node TRS/matrix to the project's row-major matrix form."""
    matrix = node.get("matrix")
    if matrix is not None:
        # glTF stores matrices column-major; the engine uses M @ v arrays.
        return np.asarray(matrix, dtype=np.float32).reshape(4, 4).T
    translation = np.asarray(node.get("translation", (0.0, 0.0, 0.0)), dtype=np.float32)
    rotation = np.asarray(node.get("rotation", (0.0, 0.0, 0.0, 1.0)), dtype=np.float32)
    scale = np.asarray(node.get("scale", (1.0, 1.0, 1.0)), dtype=np.float32)
    return sg.trs_to_mat4(translation, rotation, scale)


def _resample_gltf_track(times: np.ndarray, values: np.ndarray,
                         sample_times: np.ndarray, default: np.ndarray) -> np.ndarray:
    """Linearly resample a glTF TRS track onto one channel time domain.

    AUD-9012：以 ``np.searchsorted`` 一次求出全部采样点的插值区间，批量
    向量化线性插值，替代逐采样点的 Python 循环（数千关键帧 × 数十节点的
    动画导入不再卡在逐行 lerp 上）。语义与逐行版本一致：两端 clamp
    （sample<=首时间取首值、>=末时间取末值），区间内按时间线性插值。
    """
    default_array = np.asarray(default, dtype=np.float32)
    if times.size == 0 or values.shape[0] == 0:
        return np.tile(default_array, (sample_times.size, 1)).astype(np.float32)
    values = np.asarray(values, dtype=np.float32).reshape(times.size, -1)
    st = np.asarray(sample_times).reshape(-1).astype(np.float64)
    times_f = np.asarray(times, dtype=np.float64)
    output = np.empty((st.size, values.shape[1]), dtype=np.float32)
    if st.size == 0:
        return output
    head = st <= times_f[0]
    tail = st >= times_f[-1]
    mid = ~(head | tail)
    output[head] = values[0]
    output[tail] = values[-1]
    if mid.any():
        st_mid = st[mid]
        right = np.searchsorted(times_f, st_mid, side="right")
        left = right - 1
        span = np.maximum(1.0e-9, times_f[right] - times_f[left])
        alpha = ((st_mid - times_f[left]) / span)[:, None]
        output[mid] = (values[left] * (1.0 - alpha)
                       + values[right] * alpha)
    return output


def _extract_gltf_animation_clips(document: dict, buffers: list[bytes]) -> list[sg.AnimationClip]:
    """Decode glTF animation samplers into the engine's TRS channel contract."""
    nodes = document.get("nodes", [])
    clips: list[sg.AnimationClip] = []
    for animation_index, animation in enumerate(document.get("animations", [])):
        tracks: dict[str, dict[str, tuple[np.ndarray, np.ndarray]]] = {}
        duration = 0.0
        for channel in animation.get("channels", []):
            target = channel.get("target", {})
            node_index = target.get("node")
            path = str(target.get("path", ""))
            if node_index is None or path not in {"translation", "rotation", "scale"}:
                continue
            sampler_index = channel.get("sampler")
            if sampler_index is None:
                continue
            sampler = animation.get("samplers", [])[int(sampler_index)]
            try:
                times = _read_gltf_accessor(document, buffers, sampler["input"]).reshape(-1)
                values = _read_gltf_accessor(document, buffers, sampler["output"])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            if times.size == 0 or values.shape[0] == 0:
                continue
            # CUBICSPLINE stores in-tangent/value/out-tangent triplets.  The
            # engine currently uses linear interpolation, so retain values.
            if values.shape[0] == times.size * 3:
                values = values[1::3]
            if values.shape[0] != times.size:
                continue
            node_name = str(nodes[int(node_index)].get("name", f"node_{node_index}"))
            tracks.setdefault(node_name, {})[path] = (times.astype(np.float32), values)
            duration = max(duration, float(times[-1]))

        channels: list[sg.AnimationChannel] = []
        for node_name, node_tracks in tracks.items():
            all_times = [track[0] for track in node_tracks.values() if track[0].size]
            if not all_times:
                continue
            sample_times = np.unique(np.concatenate(all_times)).astype(np.float32)
            translation = _resample_gltf_track(
                *node_tracks.get("translation", (np.empty(0), np.empty((0, 3)))),
                sample_times, np.zeros(3, dtype=np.float32))
            rotation = _resample_gltf_track(
                *node_tracks.get("rotation", (np.empty(0), np.empty((0, 4)))),
                sample_times, np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float32))
            scale = _resample_gltf_track(
                *node_tracks.get("scale", (np.empty(0), np.empty((0, 3)))),
                sample_times, np.ones(3, dtype=np.float32))
            lengths = np.linalg.norm(rotation, axis=1, keepdims=True)
            rotation = rotation / np.maximum(lengths, 1.0e-8)
            channels.append(sg.AnimationChannel(
                node_name=node_name,
                times=sample_times,
                translations=translation,
                rotations=rotation,
                scales=scale,
            ))
        if channels:
            name = str(animation.get("name", f"animation_{animation_index}"))
            clips.append(sg.AnimationClip(name=name, duration=duration, channels=channels))
    return clips


def _gltf_skin_template(document: dict, buffers: list[bytes], skin_index: int) -> tuple[list[str], np.ndarray]:
    """Read joint names and inverse bind matrices for one glTF skin."""
    skin = document["skins"][int(skin_index)]
    joints = [int(index) for index in skin.get("joints", [])]
    names = [str(document["nodes"][index].get("name", f"joint_{index}"))
             for index in joints]
    accessor_index = skin.get("inverseBindMatrices")
    if accessor_index is None:
        inverse_bind = np.tile(np.eye(4, dtype=np.float32), (len(names), 1, 1))
    else:
        inverse_bind = _read_gltf_accessor(document, buffers, accessor_index)
        inverse_bind = np.asarray(inverse_bind, dtype=np.float32).reshape(-1, 4, 4)
    if inverse_bind.shape[0] != len(names):
        raise ValueError("glTF skin inverse bind count does not match joints")
    return names, inverse_bind


def _gltf_mesh_skin(document: dict, buffers: list[bytes], mesh_index: int,
                    template: tuple[list[str], np.ndarray], vertex_count: int) -> sg.SkinData | None:
    """Decode JOINTS_0/WEIGHTS_0 for a mesh and attach it to SkinData."""
    primitives = document.get("meshes", [])[int(mesh_index)].get("primitives", [])
    primitive = next((item for item in primitives
                      if "JOINTS_0" in item.get("attributes", {})
                      and "WEIGHTS_0" in item.get("attributes", {})), None)
    if primitive is None:
        return None
    try:
        attributes = primitive["attributes"]
        bone_indices = _read_gltf_accessor(document, buffers, attributes["JOINTS_0"])
        bone_weights = _read_gltf_accessor(document, buffers, attributes["WEIGHTS_0"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if (bone_indices.shape != (vertex_count, 4)
            or bone_weights.shape != (vertex_count, 4)):
        logger.warning("glTF skin vertex count mismatch: expected=%d joints=%s weights=%s",
                       vertex_count, bone_indices.shape, bone_weights.shape)
        return None
    return sg.SkinData(
        bone_names=list(template[0]),
        inverse_bind_matrices=template[1],
        bone_indices=bone_indices,
        bone_weights=bone_weights,
    )


def _attach_gltf_skin(document: dict, buffers: list,
                      templates: dict, source: sg.SceneNode,
                      mesh_index: int, skin_index: int) -> sg.SkinData | None:
    """Decode a glTF skin onto an already-loaded trimesh MeshData.

    Trimesh supplied the positions/normals/UVs, while the GLB accessor carries
    the skin vertex streams. Keep both on the same MeshData instance so the
    renderer selects the skinned VAO path. Returns None (and leaves the mesh
    untouched) when the skin template or vertex streams are unusable.
    """
    template = templates.get(int(skin_index))
    if template is None:
        return None
    node_skin = _gltf_mesh_skin(
        document, buffers, int(mesh_index), template,
        int(source.mesh.vertex_count))
    if node_skin is not None:
        source.mesh.bone_indices = node_skin.bone_indices
        source.mesh.bone_weights = node_skin.bone_weights
    return node_skin


def _restore_gltf_animation_and_skin(root: sg.SceneGraphRoot,
                                     gltf_path: str) -> tuple[bool, bool]:
    """Restore animation/skin metadata lost by trimesh's GLB conversion.

    AUD-9007：重建树按「glTF 节点名 == trimesh 几何名」匹配。两套命名体系
    不一致时（assimp 导出的 GLB 常见），未匹配的 mesh 会在重建中被静默丢弃
    （模型消失/bbox 归零）。因此先审计匹配率：只要有带 mesh 的 glTF 节点
    无法匹配到已加载几何（或 trimesh 已加载几何但零匹配），就保留原
    trimesh 树，只在其 mesh 上补充蒙皮数据并记录告警；全部匹配时才按
    glTF 层级重建。
    """
    try:
        document, buffers = _load_gltf_document_and_buffers(gltf_path)
        source_nodes: dict[str, sg.SceneNode] = {}

        def _collect(node: sg.SceneNode) -> None:
            if node.mesh is not None and node.name:
                source_nodes.setdefault(node.name, node)
            for child in node.children:
                _collect(child)

        _collect(root.root)
        skin_templates = {
            index: _gltf_skin_template(document, buffers, index)
            for index in range(len(document.get("skins", [])))
        }
        skin_data: list[sg.SkinData] = []

        def _resolve_source(node_name: str,
                            mesh_index: Any) -> Optional[sg.SceneNode]:
            """两级匹配：glTF 节点名 → glTF mesh 名（trimesh 几何名体系）。"""
            source = source_nodes.get(node_name)
            if source is None and mesh_index is not None:
                meshes = document.get("meshes") or []
                index = int(mesh_index)
                if 0 <= index < len(meshes):
                    mesh_name = str(meshes[index].get("name") or "")
                    if mesh_name:
                        source = source_nodes.get(mesh_name)
            return source

        scenes = document.get("scenes", [])
        scene_index = int(document.get("scene", 0)) if scenes else -1
        scene_roots = (scenes[scene_index].get("nodes", [])
                       if 0 <= scene_index < len(scenes) else [])

        # AUD-9007 匹配审计：带 mesh 的 glTF 节点逐个尝试匹配 trimesh 几何。
        matched_mesh_nodes = 0
        unmatched_mesh_nodes = 0
        matched_skin_specs: list[tuple[sg.SceneNode, int, int]] = []

        def _audit(index: int) -> None:
            nonlocal matched_mesh_nodes, unmatched_mesh_nodes
            node_data = document["nodes"][int(index)]
            mesh_index = node_data.get("mesh")
            if mesh_index is not None:
                node_name = str(node_data.get("name", f"node_{index}"))
                source = _resolve_source(node_name, mesh_index)
                if source is None:
                    unmatched_mesh_nodes += 1
                else:
                    matched_mesh_nodes += 1
                    skin_index = node_data.get("skin")
                    if skin_index is not None:
                        matched_skin_specs.append(
                            (source, int(mesh_index), int(skin_index)))
            for child in node_data.get("children", []):
                _audit(child)

        for index in scene_roots:
            _audit(index)

        preserve_tree = (unmatched_mesh_nodes > 0
                         or (bool(source_nodes) and matched_mesh_nodes == 0))
        if preserve_tree:
            # 保留 trimesh 已加载的几何树——重建会把这些 mesh 静默丢弃。
            # 已匹配到的蒙皮仍然补充（_attach_skin 直接改写 source.mesh，
            # 对原树同样生效）；动画按名采样保持 best-effort。
            logger.warning(
                "glTF 节点名与 trimesh 几何名不匹配"
                "（matched=%d, unmatched=%d, loaded=%d），保留 trimesh 场景树，"
                "动画/蒙皮层级可能不完整: %s",
                matched_mesh_nodes, unmatched_mesh_nodes, len(source_nodes),
                gltf_path)
            skin_data = []
            for source, mesh_index, skin_index in matched_skin_specs:
                node_skin = _attach_gltf_skin(
                    document, buffers, skin_templates,
                    source, mesh_index, skin_index)
                if node_skin is not None:
                    skin_data.append(node_skin)
            root.skins = skin_data
            root.animations = _extract_gltf_animation_clips(document, buffers)
            report = getattr(root, "import_report", None)
            if report is not None:
                report.add_warning(
                    f"glTF 节点名与几何名不匹配（{unmatched_mesh_nodes} 个 mesh"
                    " 无法按名绑定），已保留 trimesh 场景树，动画层级可能不完整")
            return bool(root.animations), bool(root.skins)

        def _build_node(node_index: int) -> sg.SceneNode:
            node_data = document["nodes"][int(node_index)]
            node_name = str(node_data.get("name", f"node_{node_index}"))
            mesh_index = node_data.get("mesh")
            skin_index = node_data.get("skin")
            source = _resolve_source(node_name, mesh_index)
            node_skin = None
            if source is not None and mesh_index is not None and skin_index is not None:
                node_skin = _attach_gltf_skin(
                    document, buffers, skin_templates,
                    source, int(mesh_index), int(skin_index))
                if node_skin is not None:
                    skin_data.append(node_skin)
            return sg.SceneNode(
                name=node_name,
                mesh=source.mesh if source is not None else None,
                material=source.material if source is not None else None,
                skin=node_skin,
                local_transform=_gltf_node_local_transform(node_data),
                children=[_build_node(child) for child in node_data.get("children", [])],
            )

        if scene_roots:
            root.root = sg.SceneNode(
                name="root",
                children=[_build_node(index) for index in scene_roots],
            )
            root.skins = skin_data
            root.animations = _extract_gltf_animation_clips(document, buffers)
            root.bbox_min, root.bbox_max = sg.compute_bbox(root.root)

            # Build the optional skeleton overlay from the same node tree. The
            # SkinData order remains glTF joint order; animation sampling uses
            # names, so a topologically ordered overlay is safe here.
            try:
                from .skeleton import _JointNode, build_skeleton_from_joints
                skeletons = []
                for skin_index, skin in enumerate(document.get("skins", [])):
                    joint_indices = [int(value) for value in skin.get("joints", [])]
                    joint_set = set(joint_indices)
                    parent_map = {
                        child: parent_index
                        for parent_index in joint_indices
                        for child in document["nodes"][parent_index].get("children", [])
                        if int(child) in joint_set
                    }
                    ordered: list[int] = []
                    placed: set[int] = set()

                    def _place(index: int) -> None:
                        if index in placed:
                            return
                        parent = parent_map.get(index)
                        if parent is not None:
                            _place(int(parent))
                        placed.add(index)
                        ordered.append(index)

                    for index in joint_indices:
                        _place(index)
                    order_map = {index: pos for pos, index in enumerate(ordered)}
                    joints = [
                        _JointNode(
                            name=str(document["nodes"][index].get("name", f"joint_{index}")),
                            local_matrix=_gltf_node_local_transform(document["nodes"][index]),
                            parent_joint_index=order_map.get(parent_map.get(index), -1),
                        )
                        for index in ordered
                    ]
                    skeleton = build_skeleton_from_joints(
                        str(skin.get("name", f"skin_{skin_index}")), joints)
                    if not skeleton.is_empty:
                        skeletons.append(skeleton)
                root.skeletons = skeletons
            except Exception:
                root.skeletons = []
            _finalize_report(root, LOADER_BACKEND_ASSIMP_CLI_GLB,
                             getattr(getattr(root, "import_report", None),
                                     "source_path", ""))
        return bool(root.animations), bool(root.skins)
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning("无法从 assimp GLB 恢复动画/蒙皮: %s", exc)
        return False, False


def _mark_fbx_cli_fallback_report(root: sg.SceneGraphRoot,
                                  original_exc: Exception,
                                  animation_preserved: bool = False,
                                  skinning_preserved: bool = False) -> None:
    """F0：覆盖 trimesh report，标记 assimp CLI fallback 的能力边界。

    FBX 经 assimp CLI 转 GLB 后走 trimesh 路径，trimesh 已生成 backend=trimesh
    的 report。这里把它改为 assimp_cli_glb，并记录仍未支持的能力（相机/灯光，
    以及恢复器失败时的骨骼/动画），让用户在 import report 面板看到降级原因。
    """
    report = getattr(root, "import_report", None)
    if report is None:
        report = _finalize_report(root, LOADER_BACKEND_ASSIMP_CLI_GLB, "")
    else:
        report.loader_backend = LOADER_BACKEND_ASSIMP_CLI_GLB
    # 只有恢复器失败时才记录骨骼/动画丢失；现代 assimp CLI 导出的 GLB
    # 仍包含这些 accessor，不能因为 pyassimp wrapper 崩溃就把它们静态化。
    if not skinning_preserved:
        report.add_lost_feature("skeleton/bones（FBX CLI fallback 静态化）")
    if not animation_preserved:
        report.add_lost_feature("animation（FBX CLI fallback 静态化）")
    report.add_lost_feature("camera（FBX CLI fallback 未提取）")
    report.add_lost_feature("light（FBX CLI fallback 未提取）")
    preserved = []
    if skinning_preserved:
        preserved.append("蒙皮")
    if animation_preserved:
        preserved.append("动画")
    suffix = f"，已恢复{'/'.join(preserved)}" if preserved else ""
    report.add_warning(f"pyassimp 失败，已用 assimp CLI GLB fallback{suffix}: {original_exc}")
    root.import_report = report


def _convert_assimp_scene(scene) -> sg.SceneGraphRoot:
    """pyassimp scene（rootnode 递归）→ 内部 SceneGraphRoot。"""
    root_node = sg.SceneNode(name="root")
    if scene.rootnode is not None:
        root_node = _convert_assimp_node(scene.rootnode, scene.meshes, scene.materials)
    bmin, bmax = sg.compute_bbox(root_node)
    root_data = sg.SceneGraphRoot(root=root_node, bbox_min=bmin, bbox_max=bmax)
    # F8：提取相机/灯光数据到 SceneGraphRoot.cameras/lights。
    root_data.cameras = _extract_assimp_cameras(scene)
    root_data.lights = _extract_assimp_lights(scene)
    # F0：生成导入报告。pyassimp 路径支持骨骼/动画（scene.skins/animations）。
    report = _finalize_report(root_data, LOADER_BACKEND_PYASSIMP, "")
    # F8：camera/light 计数从已提取的列表取（与 root_data.cameras/lights 一致）。
    report.camera_count = len(root_data.cameras)
    report.light_count = len(root_data.lights)
    return root_data


def _extract_assimp_cameras(scene) -> List[sg.CameraData]:
    """F8：从 pyassimp scene 提取相机数据（尽力）。"""
    cameras: List[sg.CameraData] = []
    try:
        cams = getattr(scene, "cameras", None)
        if not cams:
            return cameras
        for cam in cams:
            name = str(getattr(cam, "mName", "") or getattr(cam, "name", "") or "")
            cam_type = sg.CAMERA_TYPE_PERSPECTIVE
            # assimp camera 的 mHorizontalFOV 是水平角；内部契约统一保存
            # 垂直 FOV，避免导入相机和 Camera3D 在 16:9 画幅下错配。
            fov = float(getattr(cam, "mHorizontalFOV", 0.0) or 0.0)
            aspect = float(getattr(cam, "mAspect", 0.0) or 0.0)
            if aspect <= 0.0:
                aspect = 16.0 / 9.0
            sensor_width = 36.0
            sensor_height = sensor_width / aspect
            fov_y = (2.0 * np.arctan(np.tan(fov * 0.5) / aspect)
                     if fov > 0.0 else 0.0)
            focal_length = (sensor_height / (2.0 * np.tan(fov_y * 0.5))
                            if fov_y > 1e-6 else 50.0)
            near = float(getattr(cam, "mClipPlaneNear", 0.1) or 0.1)
            far = float(getattr(cam, "mClipPlaneFar", 1000.0) or 1000.0)
            # 变换矩阵（assimp camera 需要查 scene graph 找节点；这里用单位矩阵占位）
            transform = np.eye(4, dtype=np.float32)
            cameras.append(sg.CameraData(
                name=name, camera_type=cam_type, transform=transform,
                focal_length=float(focal_length),
                sensor_width=sensor_width, sensor_height=sensor_height,
                fov_y=float(np.degrees(fov_y)) if fov_y > 0 else 0.0,
                near=near, far=far,
            ))
    except Exception as e:
        logger.warning("Camera extraction failed (pyassimp): %s", e)
    return cameras


def _extract_assimp_lights(scene) -> List[sg.LightData]:
    """F8：从 pyassimp scene 提取灯光数据（尽力）。"""
    lights: List[sg.LightData] = []
    try:
        lts = getattr(scene, "lights", None)
        if not lts:
            return lights
        # assimp light type 映射
        _type_map = {
            0: sg.LIGHT_TYPE_DIRECTIONAL,  # aiLightSource_DIRECTIONAL
            1: sg.LIGHT_TYPE_POINT,        # aiLightSource_POINT
            2: sg.LIGHT_TYPE_SPOT,         # aiLightSource_SPOT
            3: sg.LIGHT_TYPE_AMBIENT,      # aiLightSource_AMBIENT
            4: sg.LIGHT_TYPE_AREA,         # aiLightSource_AREA
        }
        for lt in lts:
            name = str(getattr(lt, "mName", "") or getattr(lt, "name", "") or "")
            raw_type = int(getattr(lt, "mType", 0) or 0)
            light_type = _type_map.get(raw_type, sg.LIGHT_TYPE_DIRECTIONAL)
            color = np.array([
                float(getattr(lt, "mColorDiffuse", (1, 1, 1))[0]),
                float(getattr(lt, "mColorDiffuse", (1, 1, 1))[1]),
                float(getattr(lt, "mColorDiffuse", (1, 1, 1))[2]),
            ], dtype=np.float32)
            intensity = float(np.linalg.norm(color))
            transform = np.eye(4, dtype=np.float32)
            inner = float(np.degrees(getattr(lt, "mAngleInnerCone", 0.5236) or 0.5236))
            outer = float(np.degrees(getattr(lt, "mAngleOuterCone", 0.7854) or 0.7854))
            light_range = float(getattr(lt, "mAttenuationDistance", 0.0) or 0.0)
            lights.append(sg.LightData(
                name=name, light_type=light_type, transform=transform,
                color=color, intensity=intensity,
                range_=max(0.0, light_range), inner_cone=inner, outer_cone=outer,
                decay_mode="inverse_square", shadow_enabled=True,
                shadow_quality="medium", unit_mode="m",
            ))
    except Exception as e:
        logger.warning("Light extraction failed (pyassimp): %s", e)
    return lights


def _convert_assimp_node(assimp_node, meshes, materials) -> sg.SceneNode:
    """递归把 pyassimp node 转成内部 SceneNode。"""
    name = getattr(assimp_node, "name", "") or ""
    # local transform：pyassimp exposes a regular 4x4 transform with translation
    # in column 3, matching the project's column-vector matrix convention.
    local = np.eye(4, dtype=np.float32)
    trans = getattr(assimp_node, "transformation", None)
    if trans is not None:
        try:
            local = np.array(trans, dtype=np.float32).reshape(4, 4)
        except Exception:
            local = np.eye(4, dtype=np.float32)

    node = sg.SceneNode(name=str(name), local_transform=local)

    # 该节点引用的 mesh
    mesh_refs = getattr(assimp_node, "meshes", []) or []
    for mesh_ref in mesh_refs:
        try:
            m = _resolve_assimp_mesh(mesh_ref, meshes)
            if m is None:
                continue
            mesh = _convert_assimp_mesh(m)
            material_index = int(getattr(m, "materialindex", 0))
            material = _convert_assimp_material(
                materials[material_index] if materials else None,
                material_key=f"material_{material_index}",
            )
            if mesh is not None:
                child = sg.SceneNode(
                    name=f"{name}_{getattr(m, 'name', 'mesh')}",
                    mesh=mesh, material=material,
                    local_transform=np.eye(4, dtype=np.float32),
                )
                node.children.append(child)
        except Exception:
            continue

    # 递归子节点
    children = getattr(assimp_node, "children", []) or []
    for c in children:
        node.children.append(_convert_assimp_node(c, meshes, materials))
    return node


def _resolve_assimp_mesh(mesh_ref: Any, meshes: Iterable[Any]) -> Optional[Any]:
    """Resolve a pyassimp node mesh reference.

    Depending on pyassimp/libassimp versions, ``node.meshes`` can contain
    integer mesh indices or actual ``LP_Mesh`` objects. Supporting both keeps
    FBX imports from silently dropping every mesh.
    """
    if hasattr(mesh_ref, "vertices") and hasattr(mesh_ref, "faces"):
        return mesh_ref
    try:
        return list(meshes)[int(mesh_ref)]
    except Exception:
        return None


def _convert_assimp_mesh(m) -> Optional[MeshData]:
    """pyassimp mesh → 内部 MeshData。"""
    verts = np.asarray(getattr(m, "vertices", []), dtype=np.float32)
    if verts.shape[0] == 0:
        return None
    norms = np.asarray(getattr(m, "normals", []), dtype=np.float32)
    if norms.shape != verts.shape:
        norms = np.zeros_like(verts)
    # assimp 给 uvchannels[0]
    uvs = np.zeros((verts.shape[0], 2), dtype=np.float32)
    texcoords = getattr(m, "texcoords", None)
    if texcoords is not None and len(texcoords) > 0:
        try:
            tc0 = np.asarray(texcoords[0], dtype=np.float32)
            if tc0.shape[0] == verts.shape[0] and tc0.shape[1] >= 2:
                uvs = tc0[:, :2]
        except Exception:
            pass
    faces = getattr(m, "faces", None)
    if faces is None or len(faces) == 0:
        return None
    indices = _assimp_faces_to_indices(faces)
    if not indices:
        return None
    morph_targets, morph_normals = _extract_assimp_morph_targets(m, verts, norms)
    return MeshData(
        vertices=verts, normals=norms, uvs=uvs,
        indices=np.asarray(indices, dtype=np.uint32),
        morph_targets=morph_targets, morph_normals=morph_normals,
    )


def _extract_assimp_morph_targets(
    mesh: Any,
    vertices: np.ndarray,
    normals: np.ndarray,
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """Convert Assimp anim meshes to position/normal deltas."""
    raw = getattr(mesh, "animMeshes", None)
    if raw is None:
        raw = getattr(mesh, "anim_meshes", None)
    if raw is None:
        raw = getattr(mesh, "animmeshes", None)
    if raw is None:
        return {}, {}
    if isinstance(raw, Mapping):
        entries = raw.items()
    else:
        entries = ((getattr(item, "name", "") or f"morph_{index}", item)
                   for index, item in enumerate(raw))

    positions: dict[str, np.ndarray] = {}
    normals_out: dict[str, np.ndarray] = {}
    for raw_name, target in entries:
        name = str(raw_name).strip()
        target_vertices = getattr(target, "vertices", None)
        if target_vertices is None and isinstance(target, Mapping):
            target_vertices = target.get("vertices")
        try:
            target_array = np.asarray(target_vertices, dtype=np.float32)
            if target_array.shape != vertices.shape:
                continue
            positions[name] = np.ascontiguousarray(target_array - vertices)
            target_normals = getattr(target, "normals", None)
            if target_normals is None and isinstance(target, Mapping):
                target_normals = target.get("normals")
            if target_normals is not None and normals.shape == vertices.shape:
                normal_array = np.asarray(target_normals, dtype=np.float32)
                if normal_array.shape == normals.shape:
                    normals_out[name] = np.ascontiguousarray(normal_array - normals)
        except (TypeError, ValueError):
            continue
    return positions, normals_out


def _assimp_faces_to_indices(faces: Iterable[Any]) -> List[int]:
    """Normalize pyassimp face containers to a flat triangle index list.

    pyassimp may expose faces as numpy rows (``array([0, 1, 2])``), as
    ``Face`` objects with an ``indices`` attribute, or as simple Python
    sequences. Keep this parser deliberately tolerant so a valid FBX mesh does
    not silently become an empty Model3D payload.
    """
    out: List[int] = []
    for face in faces:
        raw_idx = getattr(face, "indices", None)
        if raw_idx is None:
            raw_idx = face
        try:
            idx = [int(i) for i in np.asarray(raw_idx).reshape(-1).tolist()]
        except Exception:
            try:
                idx = [int(i) for i in raw_idx]
            except Exception:
                continue
        if len(idx) == 3:
            out.extend(idx)
        elif len(idx) == 4:
            out.extend((idx[0], idx[1], idx[2], idx[0], idx[2], idx[3]))
        elif len(idx) > 4:
            # Fan triangulation for n-gons. Assimp usually triangulates before
            # this point, but this keeps imported meshes visible if it does not.
            for i in range(1, len(idx) - 1):
                out.extend((idx[0], idx[i], idx[i + 1]))
    return out


def _convert_assimp_material(mat, material_key: str = "") -> Optional[sg.MaterialData]:
    """pyassimp material → 内部 MaterialData。"""
    if mat is None:
        return None
    try:
        # assimp 材质属性在 properties dict
        props = getattr(mat, "properties", {})
        if not isinstance(props, dict):
            return None
        base = np.array([0.8, 0.8, 0.8], dtype=np.float32)
        if "baseColor" in props:
            base = np.asarray(props["baseColor"][:3], dtype=np.float32)
        elif "diffuse" in props:
            base = np.asarray(props["diffuse"][:3], dtype=np.float32)
        metallic = float(props.get("metallicFactor", 0.0))
        roughness = float(props.get("roughnessFactor", 0.5))
        if roughness <= 0:
            roughness = 0.5
        name = str(props.get("name", "") or getattr(mat, "name", "") or "")
        return sg.MaterialData(
            base_color=base, metallic=metallic, roughness=roughness,
            material_id=str(material_key or name), name=name,
        )
    except Exception as exc:
        # AUD-9015：同 _convert_trimesh_material——记 warning 并返回默认标量
        # 材质，不再整体静默丢材质（如 props["diffuse"] 为标量导致切片失败时，
        # 原实现连可恢复的字段一起丢且无诊断）。
        logger.warning(
            "材质转换失败（assimp, key=%s）：%s —— 返回默认标量材质",
            material_key, exc)
        return _default_material_data(material_key)


# ──────────────────────────────────────────────────────────────────────
# 依赖可用性探测（供 UI / 节点层友好提示）
# ──────────────────────────────────────────────────────────────────────

def trimesh_available() -> bool:
    """trimesh 是否可用（glTF/OBJ/STL/PLY）。"""
    try:
        import trimesh  # noqa: F401
        return True
    except ImportError:
        return False


def assimp_available() -> bool:
    """pyassimp 是否可用（FBX/3DS）。注：native libassimp 另需运行时验证。"""
    try:
        import pyassimp  # noqa: F401
        return True
    except ImportError:
        return False
