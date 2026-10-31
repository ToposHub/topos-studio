# batch-073 审查报告 — src/features/composite/nodes//

编号区间：AUD-7300–AUD-7399。审查日期：2026-09-28。
文件已全文通读（5,253 行，分块 Read，无跳读）。交叉验证：
`base_node.py`（`_collect_connected_scene_payloads`/`TextureResult.with_updates`/`aov_semantic`）、
`scene_graph.py`（`flatten_scene` 的 node_name 生成）、`camera3d_node.py`（可调 near/far）、
`model3d_node.py`（`can_instance` 恒 False）、`composite_renderer.py`（`redraw_with_view` 调用方）、
`perf_stats.py`/`material_state.py`/`mesh_vbo_pool.py`/`material_texture_loader.py`（API 存在性）、
以及 moderngl 5.12 源码（`Context.gc_mode` 默认 `None`，`__del__` 在该模式下不调用 `release()`，
且项目未设置 gc_mode —— 未显式 release 的 GL 对象不会因 GC 而释放）。

---

## src/features/composite/nodes/scene3d_node.py (行数: 5253)

### 发现

- [AUD-7300] [Major] 正交视图 depth renderbuffer 无任何持有/释放记账，每次 quad 视图重建泄漏 3 个 GL renderbuffer
  - 位置: src/features/composite/nodes/scene3d_node.py:2999
  - 证据:
    ```python
    if tex is None or fbo is None:
        tex = ctx.texture((ow, oh), 4, dtype='f4')
        tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        depth_rb = ctx.depth_renderbuffer((ow, oh))
        fbo = ctx.framebuffer(color_attachments=[tex], depth_attachment=depth_rb)
        self._ortho_textures[key] = tex
        self._ortho_fbos[key] = fbo
    ```
    而 `_release_ortho_views`（2877–2889）只释放 texture 与 fbo：
    ```python
    for key in list(self._ortho_textures.keys()):
        tex = self._ortho_textures.get(key)
        if tex is not None:
            try: tex.release()
            ...
        fbo = self._ortho_fbos.get(key)
    ```
  - 影响: `depth_rb` 是局部变量，不存任何实例字段。对比同文件 `_aov_depth_rbs` dict（2811–2833 有记账并释放），此处遗漏。moderngl 5.12 默认 `gc_mode=None`，`__del__` 不调用 `release()`，且 ModernGL 释放 Framebuffer 不连带释放附件 —— 每次 `_ortho_size` 变化（2973–2975 重建路径，交互式改窗口尺寸时高频触发）泄漏 3 个 renderbuffer，节点 cleanup 时再泄漏最后一组，显存单调增长。
  - 建议: 仿照 `_aov_depth_rbs` 增加 `self._ortho_depth_rbs: Dict[str, Optional[moderngl.Renderbuffer]]` 记账，在 `_release_ortho_views` 与 `_release_render_target` 中统一释放。
  - 置信度: 高

- [AUD-7301] [Major] ID-write 与 outline VAO 自建的 VBO/IBO 无释放记账，目标重建/清理时泄漏
  - 位置: src/features/composite/nodes/scene3d_node.py:2386
  - 证据:
    ```python
    vbo = ctx.buffer(data)
    ibo = ctx.buffer(mesh.indices.tobytes())
    vao = ctx.vertex_array(
        prog,
        [(vbo, layout, *attrs)],
        index_buffer=ibo,
    )
    self._id_write_vaos[key] = vao
    return vao
    ```
    outline 同模式（2673–2682）；而释放侧只删 VAO：
    ```python
    for vao in self._id_write_vaos.values():
        if vao is not None:
            try: vao.release()
            except Exception: pass
    self._id_write_vaos = {}
    ```
  - 影响: 同文件 depth/aov/motion/shadow 四套 write-VAO 都有配对的 `_depth_write_buffers`/`_aov_write_buffers`/`_motion_write_buffers`/`_shadow_write_buffers` 记账并在 `_release_render_target` 释放（2799–2855），唯独 `_get_id_write_vao`/`_get_outline_vao` 没有（grep 全文件无 `_id_write_buffers`/`_outline_buffers`）。`_release_render_target` 在每次尺寸/MSAA/AOV 开关变化时执行（3759–3762），每次泄漏 (mesh 数 × 2) 个 buffer；cleanup 同样泄漏。VAO release 不释放其 buffer，moderngl 默认 gc_mode=None 下 GC 不兜底。
  - 建议: 为 `_id_write_vaos`/`_outline_vaos` 增加配对 buffers dict（或复用 MeshVBOPool），在 `_release_render_target` 统一释放。
  - 置信度: 高

- [AUD-7302] [Major] `_lod_vao_cache` 全生命周期从不释放（含 MeshVBOPool 引用计数不减）
  - 位置: src/features/composite/nodes/scene3d_node.py:1288
  - 证据:
    ```python
    vao = ctx.vertex_array(prog, contents, index_buffer=ibo)
    tri_count = int(lod_mesh.indices.shape[0]) // 3
    self._lod_vao_cache[content_hash] = (vao, tri_count)
    return (vao, tri_count)
    ```
    全文件仅 3 处引用该字段（369 定义、1275 读、1288 写）；`_release_render_target`（2754–2873）与 `cleanup`（5165–5253）均未清空或释放它。
  - 影响: LOD VAO（及其引用的池化 VBO/IBO）永不释放；`_pool.acquire` 抬高的 MeshVBOPool 引用计数也永不 `pool.release` 回退。节点长期存活且用户多次调整 LOD 档位（不同 content_hash）时缓存无上限增长；节点删除时 cleanup 也不释放这批 GL 对象。
  - 建议: 在 `_release_render_target` 或 cleanup 中遍历 `_lod_vao_cache` 释放 VAO 并对池做 `release(content_hash, is_skinned=False)`，随后清空 dict；可另加简单的容量上限/逐出策略。
  - 置信度: 高

- [AUD-7303] [Major] 深度/ID/阴影 pass 的 VAO 缓存仅按 node_name 键控，重名 mesh 复用到错误几何
  - 位置: src/features/composite/nodes/scene3d_node.py:1393
  - 证据:
    ```python
    vao_key = (
        f"{m['node_name']}:skinned"
        if is_skinned else m['node_name']
    )
    vao = self._get_depth_write_vao(
        ctx, mesh, vao_key, program=write_prog)
    ```
    同样键控还有 ID pass（2587–2590）、shadow map（1643–1644 `f"model:{node_name}:..."`）与 catcher（1773–1774）；而 AOV pass 特意加入了 `id(mesh)`：
    ```python
    vao_key = (f"{mesh_desc['node_name']}:skinned:{id(mesh)}"
               if is_skinned
               else f"{mesh_desc['node_name']}:{id(mesh)}")
    ```
    `flatten_scene`（scene_graph.py:388–394）生成 `node_name=node.name` 原始名、不去重（只有 node_id 用全路径哈希）。
  - 影响: 导入场景中两个同名 mesh（常见："Cube"、"mesh_0" 等重复部件）在 depth/object-id/shadow pass 中第二个 mesh 命中第一个的缓存 VAO → 以错误几何 + 自身矩阵绘制 → 深度 AOV、拾取、阴影轮廓错误；`anim_worlds.get(node_name, ...)`（1404 等）对重名节点同样取到第一个匹配的动画矩阵。AOV/motion pass 已包含 id(mesh)/mesh_index 证明作者知晓 node_name 不唯一，仅这三处遗漏。
  - 建议: depth/id/shadow/catcher 的 vao_key 统一加入 `id(mesh)` 或扁平列表下标（与 AOV/motion pass 对齐）。
  - 置信度: 高

- [AUD-7304] [Major] 深度 pass 硬编码 near=0.1/far=1000，与外接 Camera3D 可调裁剪面不一致 → 线性深度输出错误
  - 位置: src/features/composite/nodes/scene3d_node.py:1369
  - 证据:
    ```python
    # near/far 与主透视一致（perspective 用 0.1..1000）
    prog['u_near'].value = 0.1
    prog['u_far'].value = 1000.0
    ```
    （1409–1410、1446–1447 重复同样硬编码。）但 §3.5 允许外接相机直接提供投影：
    ```python
    if cam_payload is not None:
        proj = cam_payload.proj
        view = cam_payload.view
    ```
    camera3d_node.py 中 near/far 是用户可调参数（`ParamDef("cam_near", ..., min_val=0.001, max_val=100.0)`、`ParamDef("cam_far", ..., max_val=100000.0)`，263–264 行计算后随 payload 携带 near/far 字段）。
  - 影响: 用户在 Camera3D 上设置非默认 near/far（或 ortho 相机）时，`_MESH_DEPTH_WRITE_FRAGMENT` 的线性化用错误的 near/far → `_depth_texture`（R32F）数值整体错误 → 下游 DeepMerge 深度合成/遮挡结果错误。内置相机路径（3958 行 `m3d.perspective(cam_fov, aspect, 0.1, 1000.0)`）不受影响。
  - 建议: 从 `cam_payload.near/cam_far` 读取（payload 已携带），内置相机路径用同一常量来源；把 near/far 作为参数传入 `_render_depth_pass`。
  - 置信度: 高

- [AUD-7305] [Major] ID/outline/shadow pass 在自身导入路径使用 bind-pose 世界矩阵，动画播放时拾取/描边/阴影错位
  - 位置: src/features/composite/nodes/scene3d_node.py:2567
  - 证据:
    ```python
    anim_worlds = anim_worlds_src
    if anim_worlds is None:
        anim_worlds = self._sample_animation_worlds(False, 0.0, 1.0)
    ```
    `_sample_animation_worlds(False, ...)` 直接返回 `{}` → 全部回退静态 `world_matrix`。同类问题：outline 2716 `anim_worlds = anim_worlds_src or {}`、shadow map 1627、catcher 1741 同样 `or {}`。而 beauty/depth 路径正确采样动画（4244）：
    ```python
    anim_worlds = self._sample_animation_worlds(anim_enabled, anim_time, anim_speed)
    ```
  - 影响: 走 Scene3D 自身 `use_imported_model` 路径（无 Model3D payload 连接）且开启动画时，beauty 用动画矩阵、ID/outline/shadow 用绑定姿态 → GPU 拾取点不中或选错对象、选中描边偏移、shadow-catcher 阴影贴在旧位置。蒙皮骨骼矩阵（`_upload_bone_matrices(prog, m, {})`）同样退化为绑定姿态。Model3D payload 路径（anim_worlds_src 非空）不受影响。
  - 建议: 这些 pass 的回退分支改为 `self._sample_animation_worlds(anim_enabled, anim_time, anim_speed)`（与 `_render_depth_pass` 1375 行一致），或在调用点统一传入本帧已采样的 anim_worlds。
  - 置信度: 中

- [AUD-7306] [Major] Geo3D 对象从不进入 GPU ID pass 与 outline pass（procedural 回退 VAO 恒为 None），但 pick_map 仍为其分配 id
  - 位置: src/features/composite/nodes/scene3d_node.py:2621
  - 证据:
    ```python
    # Geo3D 的 mesh 数据不在 payload（只有 vao），退化为用 procedural id VAO
    # （Geo3D primitive 与主 procedural 同路径，简化处理）
    pick_id = node_pick_ids.get(g_nid, 0)
    prog['u_object_id'].value = self._encode_pick_id(pick_id)
    id_vao = self._get_id_write_vao(ctx, None, '__procedural__')
    if id_vao is not None and gp.model_matrix is not None:
    ```
    但 procedural 几何在 payload 时代恒被释放（3370–3371 `primitive = "none"`、`has_builtin_primitive = False`；3976–3977 `elif not _use_geo_payload: self._release_mesh_geometry()`），`_get_id_write_vao(ctx, None, ...)` 因 `self._mesh_vbo is None` 返回 None。注释的前提也不成立——`_render_depth_pass`/`_render_aov_pass` 都能读到 `geo_payload.mesh`（1428、2045）。outline 对 Geo3D 同样回退（2741–2748）。`_build_pick_id_map`（2436–2441）仍为每个 Geo3D 分配 pick_id。
  - 影响: 视口点击 Geo3D 物体时 ID 纹理该处为背景 (0,0,0,0) → GPU 对象拾取对 Geo3D 全部未命中；选中 Geo3D 无描边反馈。CPU 组件拾取数据（`component_meshes`）虽包含 Geo3D，对象级 GPU 拾取契约被破坏。
  - 建议: ID/outline pass 的 Geo3D 分支改为用 `gp.mesh`/`gp.model_matrix`/`gp.instance_vbo` 构建各自 VAO（复用 `_get_depth_write_vao` 的 geo 分支模式，key 含 `id(mesh)`）。
  - 置信度: 中

- [AUD-7307] [Suggestion] `_render_id_pass` 的 `_p7_id_pick_info` 赋值意外落在 finally 块内；异常路径会以 UnboundLocalError 掩盖根因
  - 位置: src/features/composite/nodes/scene3d_node.py:2632
  - 证据:
    ```python
        finally:
            ctx.disable(moderngl.DEPTH_TEST)
        # 回传 id_pick_info（调用方 _do_evaluate 读 self._p7_id_pick_info 写 eval_ctx）
            self._p7_id_pick_info = {
            "tex_glo": self._id_texture.glo,
            "w": w, "h": h,
            "pick_map": pick_map,
    ```
  - 影响: 注释缩进（8 空格）表明赋值本应在 finally 之外，实际语句在 12 空格、落进 finally。`pick_map`/`node_pick_ids` 在 try 内 2558 行才定义：若 try 体在定义前抛异常（如 uniform 赋值失败），finally 里的 `pick_map` 触发 UnboundLocalError，替换并掩盖原始异常（外层 4687–4691 会捕获，但日志只见到 UnboundLocalError，丢失根因）。
  - 建议: 把该赋值移出 finally（与注释缩进对齐），并仅在 try 成功完成后回传。
  - 置信度: 高

- [AUD-7308] [Suggestion] `_inst_wmin/_inst_wmax` 的赋值守卫与使用守卫不一致（潜在 UnboundLocalError，当前路径不可达）
  - 位置: src/features/composite/nodes/scene3d_node.py:4309
  - 证据:
    ```python
    if instance_enabled and self._instance_vbo is not None:
        _inst_wmin, _inst_wmax = self._instance_offset_range(grid_x, grid_z, spacing)
    ```
    使用处守卫更宽：
    ```python
    if instance_enabled and m.get('can_instance'):
        # 实例化：把 offset 范围并入 local AABB 后再变换
        wmin = local_min + np.array(_inst_wmin, dtype=np.float32)
    ```
  - 影响: 若某 mesh `can_instance=True` 而 `self._instance_vbo is None`（两个条件判定来源不同），`_inst_wmin` 未定义 → UnboundLocalError 使整帧渲染失败。当前不可达的原因是双重巧合：`has_builtin_primitive` 恒 False 使 `_ensure_instance_buffer` 不再被调用（3970–3973），且 Model3D 硬编码 `'can_instance': False`（model3d_node.py:597）——任一前提变化即引爆。
  - 建议: 统一守卫：在进入 Path A 前计算 `instance_active = instance_enabled and self._instance_vbo is not None`，赋值与使用同用该标志；或给 `_inst_wmin` 预置默认 (0,0,0)。
  - 置信度: 高（结构性问题确凿；可触发性当前为零）

- [AUD-7309] [Suggestion] 性能诊断统计失真：`vbo_cache_hit` 永不为 False、`model_cache_hit` 恒 None
  - 位置: src/features/composite/nodes/scene3d_node.py:4921
  - 证据:
    ```python
    if _pool.last_was_cached:
        self._last_vbo_cache_hit = True
    ```
    （793–794 只置 True；3696 每帧重置为 None，从不置 False——与 792 行注释“全 miss 才 False”矛盾。）另一处：
    ```python
    _pstats.model_cache_hit = getattr(self, "_perf_model_cache_hit", None)
    ```
    `_perf_model_cache_hit` 全文件从未赋值（grep 仅此一处引用）。
  - 影响: `perf_stats.vbo_cache_hit` 只会是 None/True，全 miss 帧无法与“未经过该路径”区分；`model_cache_hit` 恒为 None，两个诊断字段都失去量化价值，误导调优。
  - 建议: `_ensure_flat_meshes_gl` 循环结束时按命中与否显式置 True/False；`model_cache_hit` 从 `_model_payloads` 的 `model_cache_status`（diagnostics 已有该字段）转换，或删除死引用。
  - 置信度: 高

- [AUD-7310] [Suggestion] GL 状态泄漏与冗余宽泛 except
  - 位置: src/features/composite/nodes/scene3d_node.py:1728
  - 证据:
    ```python
    ctx.enable(moderngl.BLEND)
    # Shader output is premultiplied shadow colour.
    ctx.blend_func = (moderngl.ONE, moderngl.ONE_MINUS_SRC_ALPHA)
    ```
    finally 只关开关不恢复混合函数：
    ```python
    ctx.disable(moderngl.BLEND)
    ctx.disable(moderngl.DEPTH_TEST)
    ```
    另有冗余异常元组（1154）：
    ```python
    try:
        prog[uname].value = loc
    except (KeyError, Exception):
        pass
    ```
  - 影响: shadow-catcher 之后 blend_func 残留为预乘模式，后续任何直接 `enable(BLEND)` 的节点/插件会继承错误混合函数（需各自显式重设才安全）；`except (KeyError, Exception)` 等价于裸 `except Exception`，不符合“不得宽泛 except”规范且吞掉非预期错误。
  - 建议: finally 中保存并恢复原 blend_func（或在进入时记录）；异常收窄为 `except KeyError`。
  - 置信度: 中

- [AUD-7311] [Suggestion] 参数输入验证不完整：尺寸无上限钳制、浮点参数无 NaN 防护
  - 位置: src/features/composite/nodes/scene3d_node.py:3367
  - 证据:
    ```python
    w = max(1, int(self.parameters.get("width", 1920)))
    h = max(1, int(self.parameters.get("height", 1080)))
    ```
    PARAM_DEFS 声明 `max_val=16384`（121–122）但运行时不钳制；相机/动画浮点直接 `float()` 无有限性检查（如 3392–3398 `cam_fov = float(_sp("cam_fov", 50.0))`）。
  - 影响: 异常保存的工程文件/外部注入携带超大尺寸（如 50000×50000）时直接尝试分配约 40GB RGBA32F 纹理导致分配失败或上下文异常；NaN 相机参数会生成 NaN 矩阵输出黑帧且难排查。
  - 建议: `min(16384, max(1, int(...)))` 与 PARAM_DEFS 对齐；对 cam_*/anim_* 做 `math.isfinite` 校验，非法值回退默认并记日志。
  - 置信度: 中

- [AUD-7312] [Suggestion] `redraw_with_view` 在渲染线程直写 `self.parameters`，绕过参数提交/undo 路径
  - 位置: src/features/composite/nodes/scene3d_node.py:3300
  - 证据:
    ```python
    for key in (
        "cam_orbit_yaw", "cam_orbit_pitch", "cam_dist",
        "cam_target_x", "cam_target_y", "cam_target_z",
    ):
        if key not in cam_params:
            continue
        try:
            self.parameters[key] = float(cam_params[key])
    ```
  - 影响: 相机导航快速路径（composite_renderer.py:634 调用）把导航值直接写入节点参数字典：不经过 set_parameter/params_changed/undo 栈。对虚拟 `__viewer3d_scene__` 节点是既定注入模式；但该快速路径未强制排除真实 Scene3D 节点（仅要求 viewer_node_id 一致），真实节点的参数可能被导航值污染并随序列化/undo 快照持久化，造成撤销后参数与渲染态不一致。渲染线程与 UI 线程并发读写同一 dict 也存在逻辑竞态（单键赋值原子，但与批量参数提交交错）。
  - 建议: `redraw_with_view` 只在 `self.id == "__viewer3d_scene__"` 时写 parameters；真实节点改为把 cam 覆盖值作为局部变量传给 `_do_evaluate`。
  - 置信度: 低（对真实节点的可达性未完全验证，renderer 侧通常以虚拟场景为 viewer 目标）

- [AUD-7313] [Suggestion] 类型注解缺失（多处）
  - 位置: src/features/composite/nodes/scene3d_node.py:1318
  - 证据:
    ```python
    @staticmethod
    def _instance_offset_range(grid_x: int, grid_z: int, spacing: float):
        """返回实例 offset 的 min/max 范围（用于实例化视锥剔除的并集 AABB）。"""
    ```
    同类：`_do_evaluate(self, inputs: Dict[str, TextureResult], ctx, fb_manager, resource_pool=None, render_mode="preview")`（3314，ctx/fb_manager/resource_pool/render_mode 无注解、无返回注解）、`cleanup(self)`（5165）、`_collect_connected_scene_payloads` 调用链、`_ensure_mesh_program(self, ctx)` 等大量 `ctx`/`prog` 裸参数。
  - 影响: 与 `.clinerules`“所有函数有类型注解”约束不符；moderngl 类型（`moderngl.Context`/`moderngl.Program`）在文件头已导入却未用于这些签名，IDE/静态检查无法发现传参错误。
  - 建议: 按文件既有风格（如 `_ensure_shadow_targets(self, ctx) -> bool`、`_get_shadow_write_vao(...) -> Optional[Any]`）补齐公共路径签名注解，优先 `_do_evaluate`/`cleanup`/`_instance_offset_range`。
  - 置信度: 高

### 通过项

- 线程安全：文件无 Qt 控件/信号；全部 GL 调用限定渲染线程（`_do_evaluate`/`redraw_with_view` docstring 明示 render-thread-only，调用方 composite_renderer 亦在渲染命令处理中）；跨线程只传 glo 整数与 JSON-safe 诊断快照（`to_dict()`/`component_meshes` 纯 numpy），无 GL 对象泄漏到 UI 线程。
- 信号防递归：不适用——本文件不创建/连接任何 Qt 信号，无程序化更新控件的递归风险点。
- GLSL·HDR 正确性（本文件侧）：输出 RGBA32F 线性工作空间 + 预乘声明；`bg_opaque=False` 时强制 RGB=0 满足预乘不变量（3457–3459）；`_srgb_to_linear_channel` 为标准 sRGB EOTF；AOV 纹理格式选择合理（normal/position/motion 用 f2 保 HDR、material_id 用 f1、depth 用 R32F）；着色器源码在 core/shaders，本文件无内联 GLSL。
- 状态提交一致性：`_revision` 仅在真实渲染块内递增并与 TextureResult 绑定（4885、5123）；motion 历史仅在真实渲染后推进（2329–2334），cache-hit 帧不推进（设计正确）；内容签名拆分 scene_content/view 保证相机变化不重建资源。
- 异常与取消路径：所有辅助 pass（depth/ID/AOV/motion/outline/ortho/shadow catcher/skeleton）均有 try/except + logging 降级且不中断 beauty；失败 AOV 弹出字典避免持有半成品纹理（4717–4728）；beauty 主路径 finally 恢复 DEPTH_TEST；MSAA 分配失败自动降级 msaa=0（3834–3851）。
- 输入验证（部分通过）：width/height/grid/spacing/msaa 有下限与枚举钳制（3367–3377、3489–3490），外部 glo 绑定有 try/except 防护（1038–1046）；遗留缺口见 AUD-7311。
- 性能模式（实现与注释一致性）：MSAA 档位降级、导航 LOD/FAST_SOLID、材质排序跳过透明重排、shared-uniform 视图级缓存在正交段正确禁用并恢复（2982–2989、3196–3197）、视锥/small-object 剔除对 skinned mesh 保守跳过、材质状态缓存每视图 reset；统计口径问题见 AUD-7309。
