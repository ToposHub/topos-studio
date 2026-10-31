# BUGS-agent-C1 — scene3d_node.py 补测过程发现的疑似 bug

模块：`src/features/composite/nodes/scene3d_node.py`（覆盖率补齐，agent-C1）
日期：2026-09-28
说明：以下问题均为阅读/补测时发现，**未修改任何 src 代码**。编号 BUG-C1-xx。

---

## BUG-C1-01 `_render_id_pass` finally 块引用可能未绑定的 `pick_map`（NameError 掩盖原始异常）

- 位置：`scene3d_node.py:2532-2641`（`_render_id_pass`）
- 现象：`pick_map, node_pick_ids = self._build_pick_id_map(...)` 在 `try:` 块内（约 :2558），
  但回传 `self._p7_id_pick_info = {...}` 的赋值位于 `finally:` 块内（:2632，且引用 `pick_map`）。
  若 try 体在赋值前抛异常（如 `prog['u_proj']` KeyError / uniform 缺失），finally 会先抛
  `NameError: pick_map`，掩盖原始异常根因；`_do_evaluate` §6b2 的 `except Exception` 只能
  记录到 NameError，排障信息丢失。
- 影响：低概率（uniform 异常时才触发），但一旦触发日志指向错误根因。
- 建议：把 `_p7_id_pick_info` 回传移出 finally（放 try 成功路径末尾），或在 finally 内
  先判断 `pick_map` 是否已定义。

## BUG-C1-02 `_render_shadow_catchers` 未判空 `self._render_fbo` 直接 `.use()`

- 位置：`scene3d_node.py:1723`（`_render_shadow_catchers`）
- 现象：早退守卫只检查 `light_view_proj is None or self._shadow_texture is None`（:1714），
  随后直接 `self._render_fbo.use()`。shadow target 与 render target 生命周期虽在
  `_release_render_target` 中一同释放，但两者分配路径独立（shadow 懒建、render target
  按 size/msaa/aov 重建）；存在 `_shadow_texture` 存活而 `_render_fbo` 为 None 的窗口
  （如 MSAA 降级分支释放后未重建时），届时 AttributeError。
- 影响：防御性缺口，正常求值顺序下不易触发。
- 建议：`_render_fbo is None` 一并纳入早退条件。

## BUG-C1-03 性能诊断字段 `model_cache_hit` 恒为 None（`_perf_model_cache_hit` 从未写入）

- 位置：`scene3d_node.py:4921`（`_do_evaluate` §7）
- 现象：`_pstats.model_cache_hit = getattr(self, "_perf_model_cache_hit", None)`，
  但 `_perf_model_cache_hit` 在 `__init__` 及整个 src/ 中都**没有任何赋值点**
  （grep 全仓仅此一处读取）。渲染诊断里的 `perf_stats.model_cache_hit` 永远是 None，
  与相邻的 `_last_vbo_cache_hit`（在 `_ensure_flat_meshes_gl` 中真实汇总）不一致。
- 影响：诊断面板/日志中模型缓存命中率指标失效（恒空），非崩溃。
- 建议：要么在 Model3D payload 导入缓存路径回写该字段，要么删除该指标避免误导。

## BUG-C1-04 `_sort_meshes_by_material` 对非 dict 的 mesh 条目会 AttributeError

- 位置：`scene3d_node.py:1195`（排序 key `pair[1].get('material')`）
- 现象：同函数内 `_is_transparent` 对非 dict 条目做了 `isinstance(fm, dict)` 防御并视为
  不透明，但排序 key 直接 `.get(...)`。若 `flat_meshes` 混入非 dict 条目（其它辅助路径
  如 `_material_aov_key`、`_build_component_pick_meshes` 都按 dict 约定防御了非 dict），
  `opaque.sort(...)` 抛 AttributeError，整帧渲染失败。
- 影响：当前内部数据均为 dict，属健壮性/一致性缺口。
- 建议：排序 key 与 `_is_transparent` 使用同一防御（`fm.get('material') if isinstance(fm, dict) else None`）。

## BUG-C1-05 Geo3D 的 GPU ID 拾取在纯渲染器架构下画不出任何几何

- 位置：`scene3d_node.py:2609-2628`（`_render_id_pass` 路径 B）与 `:4505` 附近的
  `_do_evaluate` §5（`has_builtin_primitive = False` → `_release_mesh_geometry()`）
- 现象：ID pass 渲染 Geo3D payload 时使用 `self._get_id_write_vao(ctx, None, '__procedural__')`
  （注释自述"Geo3D 的 mesh 数据不在 payload（只有 vao），退化为用 procedural id VAO"）。
  该调用要求 `self._mesh_vbo is not None`；但 Scene3D 现为纯渲染器，`primitive="none"`、
  `has_builtin_primitive=False`，procedural 几何始终被释放（`_mesh_vbo=None`）→
  `_get_id_write_vao` 返回 None → `if id_vao is not None` 恒假 → Geo3D 对象的 object-id
  从不写入 `_id_texture`，viewer GPU 点击拾取对 Geo3D 恒返回背景未命中。
  CPU 组件拾取路径（`_build_component_pick_meshes`）用 payload.mesh 是正常的，仅 GPU
  ID pass 失效。
- 影响：P7 GPU 点选拾取对 Geo3D 节点功能性失效（medium）。
- 建议：为 Geo3D payload 的 ID pass 构建独立 VAO（可复用 `_get_id_write_vao(ctx, gp.mesh, ...)`
  的 mesh 分支），或复用 `_build_component_pick_meshes` 已有的 payload mesh 数据。

---

## 汇总

| 编号 | 严重度 | 一句话 |
|------|--------|--------|
| BUG-C1-01 | Low | ID pass finally 块 NameError 风险，掩盖原始异常 |
| BUG-C1-02 | Low | shadow catcher 未判空 `_render_fbo` |
| BUG-C1-03 | Low | `model_cache_hit` 诊断恒 None（字段从未写入） |
| BUG-C1-04 | Low | 材质排序对非 dict mesh 条目崩溃（防御不一致） |
| BUG-C1-05 | Medium | Geo3D 的 GPU ID 拾取画不出几何（procedural VAO 恒 None） |
