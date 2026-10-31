# EF-11 复查报告（GLM5.3，只读复核）

- 批次：EF-11（src/features/composite/render3d，16 条）
- 提交哈希：`7c62bbdf0`（[fix-E ef-11] composite/render3d 16条 Suggestion 全修）
- 执行报告：`fixes/reports/ef-11.md`
- 复查日期：2026-10-01 ｜ 复查基线：当前 HEAD `9984d5939`
- 白名单：`src/features/composite/render3d/` ∪ `tests/` ∪ `fixes/reports/`

## §6 八项检查逐项判定

### 1. 越界检查 — PASS

`git show --stat 7c62bbdf0`：16 个文件，全部 ∈ 白名单：
- `fixes/reports/ef-11.md`（+76）
- 14 个源文件全部在 `src/features/composite/render3d/` 下（aov / deformers/__init__ / frustum / gizmo_overlay / gizmo_renderer / hdri / material_model / math_utils / mesh_picking / model_loader / node_icon_overlay / perf_stats / scene_hierarchy / scene_payload）
- `tests/composite/test_ef11_aud.py`（新建，+903）

未触碰 REMEDIATION_PLAN.md、nodes/ 等白名单外目录。零越界。

### 2. 真实修复（逐条独立核实）— 16/16 CONFIRMED

重点条目深查（见下表"核实方式"列）：

**AUD-8915（Möller–Trumbore 向量化）**：新旧实现逐点比对——
- 平行判定：旧 `-_EPSILON < a < _EPSILON → miss` ⇔ 新 `np.abs(a) >= _EPSILON`，边界一致；
- u/v/t 谓词：旧 `u∈[0,1]、v>=0、u+v<=1、t>_EPSILON 且 t<best_t（初值 max_distance，严格 <）` ⇔ 新 `(t > _EPSILON) & (t < max_distance)`，含两端 clamp 语义一致；
- 并列取最小索引：旧循环仅在严格更小时替换（保留先到者）⇔ 新 `argmin` 取首个最小，一致；
- 旧实现经 `ray_triangle_intersect` 已强制 `t > _EPSILON`，新实现直接内联同阈值；
- 尾部不完整三角形：旧 `len(indices)//3` 截断 ⇔ 新 `idx[:n_tris*3]` 截断，一致；
- 唯一数值差异：model_matrix 路径旧实现保留 float32 累加、新实现统一 float64（精度提升，非行为回归）。
- 测试含独立可读参考实现 `_reference_pick_face`（逐三角形版）在 20 个随机网格上对拍（种子 20261001）。**语义保真确认。**

**AUD-9011（O(n²) 消除）**：旧实现每 `_place` 调用重建 `{unique_names[i] for i in range(len(unique_names))}` 全集 set + 每节点 `unique_names.index(nm)` 线性扫（累计 O(n²)）；新实现预建 `unique_set` + `raw_index_of` dict，成员测试与索引查找均 O(1)。成员集合与索引值逐一等价（同一 `unique_names` 列表推导）。

**AUD-88040（GPU 资源异常路径释放）**：`_build` 整体 try/except → `logger.warning` + `self.release()` + `raise`（释放后半成品引用全部置 None、`_built=False`、`_ctx=None`，重试不覆盖泄漏）；`__init__` 记录 `self._ctx`，`draw` 换 context 时先 `release()` 再重建；`release()` 复位 `_ctx`。调用链 draw→_build 在渲染线程，无 UI 线程 GL。

**AUD-9012（重采样向量化）**：旧逐行循环的边界（`<=times[0]` 取首值、`>=times[-1]` 取末值、`searchsorted side="right"` + `max(1e-9, span)` lerp）与新布尔掩码 + 批量 lerp 逐句等价；float32→float64 转换为精确扩展，比较结果不变。

### 3. 红线扫描 — PASS（无发现）

`git show 7c62bbdf0` 全部新增行扫描：
- 裸 `except:`：0 处。
- `except Exception:`：仅 1 处（gizmo_renderer `_build`），带 `logger.warning` 留痕 + `release()` + **重抛**，非吞错。
- `pass` 新增 1 处：hdri `while glGetError() != GL_NO_ERROR: pass` —— GL 错误位排空循环（惯用法，非异常处理）。
- UI 线程 moderngl：无（全部改动位于渲染线程调用链）。
- HDR 钳制：新增行无 `np.clip`/数值钳制；mesh_picking/model_loader 数值路径无值域约束。
- 601/709：新增行零命中。

### 4. 测试质量 — PASS

`tests/composite/test_ef11_aud.py`（56 用例，与报告一致，py_compile 通过）：
- 每条 ID 一组正例 + 回退敏感反例；doc 类修复用"docstring 不再含错误承诺 + 行为契约用例"双重锁定。
- 8915 的对拍测试（随机网格 vs 逐三角形参考实现）直接体现"语义等价"修复语义；507 行 `test_no_per_triangle_python_loop` 用源码检查锁定向量化不被回退。
- 88040 的 `test_build_failure_releases_partial_objects` 断言半成品被释放；`test_ctx_change_triggers_release_and_rebuild` 断言换 ctx 重建。

### 5. 回退验证建议（抽查 2 条）

| 条目 | 文件:行（当前 HEAD） | 回退操作（old → new） | 应失败的 pytest 节点 |
|------|---------------------|----------------------|---------------------|
| AUD-88030 | `src/features/composite/render3d/gizmo_overlay.py:336-337` | 删除 `if not self._projection_ready():` 与 `return []` 两行（恢复旧的无守卫路径） | `tests/composite/test_ef11_aud.py::TestAud88030GizmoDefensive::test_build_draw_list_before_update_empty` |
| AUD-8915 | `src/features/composite/render3d/mesh_picking.py:485-528`（`# Möller–Trumbore 批量化` 起至函数尾） | 将向量化块整体替换回 7c62bbdf0~1 的逐三角形循环（`for i in range(n_tris): t,u,v = ray_triangle_intersect(...); if 0.0 < t < best_t: ...`） | `tests/composite/test_ef11_aud.py::TestAud8915PickFaceVectorized::test_hit_nearest_triangle` 与 `test_no_per_triangle_python_loop`（后者因源码检查必然失败，前者随机对拍仍可能通过——建议以 `test_no_per_triangle_python_loop` 为准） |

### 6. 综合结论 — **PASS**

## 逐 ID 核实表（16/16）

| ID | 计划指控 | 报告结论 | HEAD 独立核实（file:line 证据） | 判定 |
|----|---------|---------|-------------------------------|------|
| AUD-8607 | IDAllocator 文档承诺跨会话一致，实现按遍历顺序递增 | FIXED（doc 收窄） | aov.py:203-217 docstring 明示"仅同一分配器+相同遍历顺序内稳定，跨会话无保证，需跨会话用 cryptomatte" | CONFIRMED |
| AUD-8710 | docstring 与导出不一致；导出无实现的 SHEAR/SPHERIFY | FIXED（收敛导出） | deformers/__init__.py:1-45 `__all__`/import 去除两常量；grep 全仓库确认包级导入方为零（仅 tests 从 `.base` 直接导入，不受影响） | CONFIRMED |
| AUD-88020 | aabb_screen_size 文档承诺"<0 相机后"，无负值路径 | FIXED（doc 修正） | frustum.py:107-113 docstring 声明恒 >=0、剔除归 aabb_in_frustum；死注释删除 | CONFIRMED |
| AUD-88030 | update() 前调用 build_draw_list/begin_drag 因 None 崩溃 | FIXED（守卫） | gizmo_overlay.py:290 `_projection_ready()`；:305（_world_to_tex_ndc 返回 (0,0)）、:336（build_draw_list 返回 []）、:630（begin_drag 跳过 raycast） | CONFIRMED |
| AUD-88040 | _build 无异常安全；换 ctx 不复位 _built | FIXED（释放+重建） | gizmo_renderer.py:186-260 try/except→warning+release+raise；:174-178 `self._ctx` 记录与换 ctx 重建；:374-391 release() 复位 `_built`/`_ctx` | CONFIRMED |
| AUD-88055 | bake 不恢复 viewport/FBO；缓存键文档不符 | FIXED（finally 恢复） | hdri.py:444-445 进入时保存（先于 release()，注释说明防删绑定 FBO 丢状态）；:478-483 finally 恢复；:378-386 缓存键 docstring 改"bake 配置常量" | CONFIRMED |
| AUD-8904 | 渲染线程安全路径每槽 os.path.isfile | FIXED（TTL 缓存） | material_model.py:502-535 `_path_exists_cached`（2s TTL）+ `clear_texture_stat_cache()`；:569 missing 标记走缓存 | CONFIRMED（注：缓存 dict 无锁，GIL 下诊断路径可接受，见"备注"） |
| AUD-8911 | spot_intensity docstring 称 smoothstep，实现线性 | FIXED（doc 修正） | math_utils.py:344-352 docstring 改线性；对照 shaders.py:6470-6473 GLSL 实现同为线性 clamp——Python 与 shader 数值一致属实，shader 侧注释偏差不在白名单未动（处置正确） | CONFIRMED |
| AUD-8915 | pick_face 兼容路径 O(三角形数) 纯 Python 循环 | FIXED（向量化） | mesh_picking.py:435-528 全三角形 einsum/cross/argmin；语义逐点比对一致（见上）；测试对拍参考实现 | CONFIRMED |
| AUD-9011 | 拓扑排序 O(n²) | FIXED（set+dict） | model_loader.py:1015-1017 `unique_set`/`raw_index_of` 预建；:1031 O(1) 索引；旧代码确认为每调用重建全集 set + `list.index` | CONFIRMED |
| AUD-9012 | 逐行 Python lerp | FIXED（向量化） | model_loader.py:1330-1362 布尔掩码 clamp + searchsorted + 批量 lerp；与旧逐行实现逐句等价 | CONFIRMED |
| AUD-9015 | 宽 except 静默 return None 丢材质 | FIXED（留痕+fallback） | model_loader.py:696-703（trimesh）与 :2012-2017（assimp）except → logger.warning → `_default_material_data()`；:706-719 默认 0.8 灰标量材质；None 输入路径保留 | CONFIRMED |
| AUD-9100 | 图标 near/far/range 存而不用，doc 不符 | FIXED（doc 修正） | node_icon_overlay.py:137-143 与 :376-381 docstring 明示"存档不参与图标几何，深度由 size×FRUSTUM_LENGTH 推出"；参数保留 API 兼容（调用方在 nodes/ 白名单外，处置合理） | CONFIRMED |
| AUD-9105 | reset() 不复位三个上下文字段 | FIXED | perf_stats.py:99-110 reset() 复位 quality_profile/viewport_layout/active_view；scene3d_node.py:3953-3956 每帧 reset 后重新赋值（兼容核实属实） | CONFIRMED |
| AUD-9120 | hierarchy 形参完全未使用 | FIXED（删参） | scene_hierarchy.py:230-245 形参已删；grep 全仓库无调用方传参（test_scene_hierarchy.py 调用均无该参） | CONFIRMED |
| AUD-9204 | signature 每帧 astype+tobytes MB 级拷贝 | FIXED（O(1) shape） | scene_payload.py:260-290 只取 shape；依赖核实：geo3d_node.py:300-312 content_sig 含 (primitive, grid_x, grid_z, spacing, …) 且变化递增 revision；instance_offsets 全仓库唯一生产者是 geo3d_node（grep 证实）——契约闭环成立 | CONFIRMED |

## 红线发现

无 Critical。备注 2 条（非阻塞）：
1. material_model `_texture_stat_cache` 为无锁模块级 dict（仅诊断路径消费，GIL 原子操作下最坏重复 stat；键为纹理路径，实际数量有界）。
2. AUD-8911 的 GLSL 侧注释（自称 smoothstep）仍在 composite/core/shaders.py:6470（白名单外），执行报告已如实披露为"未动"——建议记入后续清理候选。

## 最终结论

**PASS**。16/16 真实修复（含 4 条 doc-gap 类按审计首选方案做文档侧收窄，理由充分且已披露）；无越界、无红线违规；测试回归有效性良好（执行侧已抽 88030/9015 做过回退敏感验证，本复查另附 2 条可操作回退方案）。允许 `check_fixes.py --mark`（commit 填 `7c62bbdf0`）。
