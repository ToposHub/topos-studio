# batch-166 审查报告 — src/features/edit/core/

批次：batch-166 [P1]（编号区间 AUD-16600–AUD-16699）
文件：`src/features/edit/core/edit_gpu_preview_renderer.py`（5,297 行，已通读全文）
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解。

跨文件证据（只读核实）：
- `src/features/edit/core/timeline_preview_renderer.py:2055`：生产方以 3 元组构造 `adjustment_layers`。
- `src/shared/gpu/present_frame.py:371-401`：`OutputSlotPool.acquire_for_writing` 只回收 RELEASABLE/FREE 槽位，WRITING 状态永不复用。
- `src/shared/media/gpu_frame.py:522-533`：`safe_release_gpu_frame` 文档明确可任意线程调用（提交侧释放路径安全）。

---

## src/features/edit/core/edit_gpu_preview_renderer.py (行数: 5297)

### 发现

- [AUD-16600] [Critical] adjustment_layers 生产方为 3 元组，scoped 合成按 2 元解包 → 含调整剪辑的 GPU 帧必然 ValueError
  - 位置: src/features/edit/core/edit_gpu_preview_renderer.py:3158（生产方证据 timeline_preview_renderer.py:2055）
  - 证据:
    ```python
    # 本文件 _build_scoped_composite_work（FX-C02 时期写入，按 2 元组解包）
    for adj_layer, effects in adjustment_layers:
        adj_idx = getattr(adj_layer, 'track_index', 0)
        work.append((adj_idx, 1, 'adjustment', adj_layer, effects))
    ```
    ```python
    # timeline_preview_renderer.py:2051-2055（T-P1-22 之后按 3 元组下发）
    if asset_type == 'adjustment':
        adj_effects = self._get_clip_effects_snapshot(layer)
        preview_grade = self._get_preview_color_grade(layer)
        adjustment_layers.append((layer, adj_effects, preview_grade))
    ```
  - 影响: 时间线只要存在调整剪辑，`_process_task` → `_composite_with_adjustment_scope` → `_build_scoped_composite_work` 对 3 元组做 2 元解包抛 `ValueError: too many values to unpack`，被 `run()` 捕获后每帧 `render_error`，GPU 呈现链对该类时间线整体失效（每帧回退/丢帧）。单元测试（test_gpu_adjustment_scope_composite.py 的 `_adj` 辅助只产 2 元组）与现行代码不一致，故 CI 无法拦截。次生问题：即便修好解包，`_composite_with_adjustment_scope` 两处调用点（3241-3243、3277-3279）都只传 `[(adj_layer, effects)]`，grade 仍会被静默丢弃。
  - 建议: 解包改为 `for adj_layer, effects, *grade_rest in adjustment_layers:`（或统一数据类），把 grade 传入 work item 并透传到 `_apply_adjustment_clips`；补一条 3 元组端到端单测。
  - 置信度: 高

- [AUD-16601] [Major] present slot lease 在多条提前返回路径泄漏，WRITING 槽位永久不可回收
  - 位置: src/features/edit/core/edit_gpu_preview_renderer.py:3762-3779（acquire）；2013-2014、1994-1996、2318-2324、2326-2335、2342-2347（提前返回）；1334-1338（run() 异常路径）
  - 证据:
    ```python
    # _ensure_composite_fbo 成功后：
    lease = self._present_pool.acquire_for_writing(...)
    if lease is not None:
        self._output_pool_index = lease.slot_id
        self._current_present_lease = lease
    ```
    ```python
    # _process_task：acquire 之后的提前返回，lease 不清理
    if not task.layers:
        return
    ...
    if not track_textures:
        self.render_error.emit(task.frame, "No GPU frames rendered")
        self._release_previous_frames()
        return
    ```
    `present_frame.py` 的 `acquire_for_writing` 只选 RELEASABLE/FREE；`mark_ready`/`ack` 都不会被调用的 WRITING 槽位永久退出轮换。
  - 影响: `not task.layers`、`gpu_track_render_incomplete`、`transition_*` 错误、缓存 blit 失败、以及渲染异常——每发生一次就永久损失 24 个 present 槽位中的一个；累计 24 次后 `present_pool_full` 常驻，所有 GPU 帧报 `present_slot_unavailable`，GPU 预览链路整体死亡（仅尺寸变更触发 `release_all` 或重启可恢复）。
  - 建议: 在 `_process_task` 的每条失败/提前返回路径与 `run()` 的 except 分支统一调用一个 `_abandon_current_present_lease()`（把 lease 置 RELEASABLE/FREE），或改为 try/finally 包裹整个任务处理。
  - 置信度: 高

- [AUD-16602] [Major] 输出池尺寸变更时立即释放全部纹理并 `release_all()`，在途未 ack 的 GpuPresentFrame 指向已删除纹理
  - 位置: src/features/edit/core/edit_gpu_preview_renderer.py:3727-3743
  - 证据:
    ```python
    if need_rebuild:
        # Release old pool
        for tex, fbo in self._output_pool:
            try:
                tex.release()
            ...
        self._output_pool.clear()
        # Rebuild present pool for the new size
        self._present_pool.release_all()
    ```
  - 影响: Viewer 可能仍持有旧尺寸帧的 READY/IN_VIEWER slot（fence 等待或待 blit）；纹理名被 `tex.release()` 删除后，Viewer 的 glWaitSync/blit 采样的是已释放且可能被复用的 GL 名 → 花屏、混帧甚至驱动级未定义行为。分辨率/代理尺寸切换（预览质量档变化）时即触发。
  - 建议: 尺寸重建前先排空等待（有限次 drain Viewer ack 或延迟到全部 slot RELEASABLE 再重建），或旧池进入 deferred-release 队列延后释放（与项目 GPU deferred-release 模式一致）。
  - 置信度: 中（依赖 Viewer 端在途时序，但删除仍被引用的纹理本身即不安全）

- [AUD-16603] [Major] 调整剪辑 grade 分支用槽位索引 1000+，`_ensure_graded_track_fbo` 会顺序创建 1001 张全尺寸纹理
  - 位置: src/features/edit/core/edit_gpu_preview_renderer.py:4515-4517（调用）；3972-3976（分配循环）
  - 证据:
    ```python
    # _apply_adjustment_clips（T-P1-22 注释：scratch 索引从 1000 起避开轨槽）
    graded = self._apply_preview_grade(
        source_for_grade, 1000 + adj_index, w, h, grade)
    ```
    ```python
    # _ensure_graded_track_fbo
    while len(self._graded_track_texes) <= index:
        tex = self.ctx.texture((w, h), components=4, dtype='f1')
        fbo = self.ctx.framebuffer(color_attachments=[tex])
    ```
  - 影响: 首个带 grade 的调整剪辑帧会在渲染线程分配 1001 张 RGBA f1 纹理+FBO：1080p 画布约 8.3 GB、960×540 代理约 2 GB 显存 → 分配失败抛异常（每帧 render_error）或系统级显存压力。当前被 AUD-16600 掩盖（ValueError 先抛、分支不可达），一旦 16600 修复立即触发。
  - 建议: 调整剪辑的 grade 目标应使用独立命名资源（专用 1-2 张 scratch，或 dict 按需分配），绝不能复用按索引顺序填充的轨槽数组；补显存预算断言。
  - 置信度: 高（代码路径明确；当前不可达，见 AUD-16600）

- [AUD-16604] [Major] 转场预混合后 `track_texture_indices` 未同步收缩，scoped 合成 z 序错位
  - 位置: src/features/edit/core/edit_gpu_preview_renderer.py:3571-3583（收缩 textures/ids）；3154（zip 消费 indices）
  - 证据:
    ```python
    track_textures[:] = new_textures
    track_clip_ids[:] = new_ids        # indices 列表未同步
    ```
    ```python
    for entry, idx in zip(track_textures, track_texture_indices):
    ```
  - 影响: 每个双源转场使 `track_textures` 少一项而 `track_texture_indices` 保持原长：3 轨 A(0)/B(1)/C(2) 在 A-B 转场后 zip 把 C 配到 index 1（B 的旧槽），`_build_scoped_composite_work` 的 z 排序随之错位——转场帧 + 调整剪辑同现时上层轨被插到错误层级（视觉层级翻转/调整作用域错轨）。无调整剪辑时 `_composite_tracks` 按列表顺序合成，不受影响。
  - 建议: `_preblend_transition_tracks` 重建三个平行列表时同步重建 `track_texture_indices`（按 left_index 原索引保留、right_index 移除）。
  - 置信度: 中（逻辑明确；触发需转场+调整剪辑同帧，且当前被 AUD-16600 掩盖）

- [AUD-16605] [Major] `_cleanup` 未释放复合纹理缓存（≤256MB）、bg/snapshot FBO 与多个 program/VAO；成功路径从不销毁 shared context/surface
  - 位置: src/features/edit/core/edit_gpu_preview_renderer.py:5129-5297（清理清单）；3891-3917（缓存分配，单条目全分辨率纹理）
  - 证据:
    ```python
    # _cleanup 释放了 track/graded/transform/crop/transition/output_pool/
    # scratch/black/vt_bridge/effect_runner/ctx，但没有：
    #   _composite_cache 条目（_store_composite_cache 分配，预算 268MB）
    #   _bg_composite_tex/_bg_composite_fbo、_snapshot_tex/_snapshot_fbo
    #   _snapshot/_generator/_yuv*/_grade/_blend 等 program 与 VAO、quad VBO
    ```
  - 影响: renderer stop/重建（非进程退出）场景下，这些资源驻留在共享 share group 中直到（从未被销毁的）`shared_pyqt_context` 随进程消亡——复合缓存最高 256MB 显存泄漏，且与文件内其他资源“显式 release”的风格不一致。
  - 建议: `_cleanup` 开头调用 `invalidate_composite_cache()` + `_drain_composite_cache_invalidate()`，并补齐 bg/snapshot/program/VAO 释放；成功路径退出前 `shared_pyqt_context.deleteLater()`（需回 GUI 线程）与 `surface.destroy()`。
  - 置信度: 中（moderngl `ctx.release()` 对包装型上下文是否代删资源存在实现差异；泄漏上界确定）

- [AUD-16606] [Suggestion] grade/YUV→RGB shader 输出 clamp(0,1) 且全链 f1 中间纹理，HDR 预览被量化为 8bit，与 `_effect_hdr`(f2) 的意图相悖
  - 位置: src/features/edit/core/edit_gpu_preview_renderer.py:1401、1577（clamp）；3348-3351、3747、3910（f1 纹理）
  - 证据:
    ```glsl
    f_color = vec4(clamp(r, 0.0, 1.0), clamp(g, 0.0, 1.0), clamp(b, 0.0, 1.0), 1.0);
    ...
    f_color = vec4(clamp(apply_primary_grade(source.rgb), 0.0, 1.0), source.a);
    ```
    ```python
    self._composite_scratch_a = self.ctx.texture((w, h), components=4, dtype='f1')
    ...
    current_glo = runner.run(spec, current_glo, params, w, h,
        dtype="f2" if self._effect_hdr else "f1")
    ```
  - 影响: grade（曝光/增益可推 >1.0）在进入 f2 特效链之前就被截断到 [0,1]；铁律"HDR 内部数据不得无理由 clamp"在预览路径被打穿。预览-only，且 f1 管线本身即 8bit，影响范围限于 HDR 素材的监看精度。
  - 建议: 若 HDR 预览是目标，track/scratch/输出槽按 `_effect_hdr` 选用 f2，并去掉 grade 末端的 clamp（仅在最终 present 边界 clamp）；若非目标，在 `_effect_hdr` 注释中说明预览 8bit 的既定取舍。
  - 置信度: 高（事实明确；严重度按预览语义定为 Suggestion）

- [AUD-16607] [Suggestion] `_render_yuv_to_track_fbo` 失败路径静默返回，轨道按“成功”进入合成（空轨无 CPU 回退信号）
  - 位置: src/features/edit/core/edit_gpu_preview_renderer.py:2523-2536
  - 证据:
    ```python
    if uv_plane is None:
        logger.warning("No UV plane in wrapped external textures")
        return
    ...
    program = self._yuv_rect_program if use_rectangle else self._yuv_program
    vao = self._yuv_rect_vao if use_rectangle else self._yuv2_vao
    if program is None or vao is None:
        logger.warning("YUV shader program is not initialized")
        return
    ```
    调用方（2149-2150）不检查任何返回值，随后照常 crop/grade/effects 并 append 到 `track_textures`（该 FBO 已被 clear 为全透明）。
  - 影响: 当前两个早退条件实际不可达（`_wrap_external_gpu_planes` 保证 ≥2 平面；shader 失败会让 `_init_shaders` 返回 False），属防御性缺口：未来任何新增早退都会产出黑/透明轨道而非 `failed_tracks += 1` + render_error，观众看到残缺合成而不错误回退。
  - 建议: 方法改为返回 bool，调用方失败时计入 `failed_tracks` 走既有回退路径。
  - 置信度: 中

- [AUD-16608] [Suggestion] `_ExternalTextureProxy.use()` 每次绑定新建并初始化 `QOpenGLFunctions_4_1_Core`；初始化失败时静默不绑定
  - 位置: src/features/edit/core/edit_gpu_preview_renderer.py:557-569
  - 证据:
    ```python
    def use(self, location: int = 0) -> None:
        from PyQt6.QtOpenGL import QOpenGLFunctions_4_1_Core
        gl = QOpenGLFunctions_4_1_Core()
        if gl.initializeOpenGLFunctions():
            gl.glActiveTexture(0x84C0 + location)
            gl.glBindTexture(0x0DE1, self._glo)
    ```
  - 影响: 每轨每帧（特效链输出被采样时）都做一次函数指针表初始化（额外开销）；`initializeOpenGLFunctions()` 返回 False 时不绑定也不报错——采样落到上一帧残留绑定，得到错误纹理且无诊断。
  - 建议: 在渲染线程初始化一次模块级/实例级 functions 对象复用；初始化失败时记录 warning 并显式 glBindTexture(0) 或抛出可观测错误。
  - 置信度: 高

- [AUD-16609] [Suggestion] `_ensure_transition_resources` 总是分配生产路径不用的全尺寸 f4 mask 纹理
  - 位置: src/features/edit/core/edit_gpu_preview_renderer.py:3479-3487（注释亦承认“生产转场路径不写入它”）
  - 证据:
    ```python
    self._transition_mask_tex = self.ctx.texture(
        (w, h), components=1, dtype='f4',
    )
    ```
  - 影响: 每次重建多占 w×h×4 字节显存（1080p ≈ 8.3MB）且参与重建条件判断（`_transition_mask_tex is not None`），纯属遗留兼容负担。
  - 建议: 删除该纹理及其条件（或仅在确实存在旧直接调用方时按需创建）。
  - 置信度: 高

- [AUD-16610] [Suggestion] 图层 opacity 直接进 shader 未钳制（对比：调整剪辑路径有钳制）
  - 位置: src/features/edit/core/edit_gpu_preview_renderer.py:2063/2166/2218/2280（取值）；3017-3020、3043-3049、3063-3073（使用）；对照 3280-3282
  - 证据:
    ```python
    opacity = track_info.get('opacity', 1.0)      # 未钳制
    ...
    self._blend_program['u_opacity'] = opacity     # 直接写 uniform
    ...
    adjustment_opacity = max(
        0.0, min(1.0, float(getattr(adj_layer, 'opacity', 1.0)))
    )                                             # 调整路径有钳制
    ```
  - 影响: 异常 track_info（NaN/负值/>1）会令 shader `mix` 外推，出现过曝或反相残影；与同文件调整路径的防御标准不一致。
  - 建议: 在 `_process_task` 收集 opacity 处统一 `max(0.0, min(1.0, float(...)))`（NaN→1.0）。
  - 置信度: 中（正常生产方在 0..1 内，属防御不一致）

- [AUD-16611] [Suggestion] 类型注解缺失/不准确若干处；过期注释与数据形状不符
  - 位置: src/features/edit/core/edit_gpu_preview_renderer.py:295（`adjustment_layers: list = None`）、282（`layers: list` 无元素类型）；1793/1811（`_ensure_vt_import_bridge`、`_import_vt_pixel_buffer_frame` 无注解）；2505-2506、2657-2659（`decoded_frame`/`wrapped_planes` 无类型、返回未注解）；2994-2995（`_composite_tracks` 返回未注解）；4197-4205（`_apply_clip_effects` 的 `source_tex` 无类型、返回未注解）；2146-2147（注释称 "TransformParams (7-tuple)"，实际 8 元组，见 clip_params/timeline_render_plan.ClipLayerInfo.transform）
  - 证据:
    ```python
    adjustment_layers: list = None  # type: ignore[assignment]
    ...
    # clip TransformParams (7-tuple) from track_info → deforms the
    # YUV→track-FBO quad in NDC space.
    ```
  - 影响: 可读性/静态检查弱化；7 元组过期注释误导后续维护（真实形状为 8 元组，`_transform_to_ndc_mat3_bytes` 按 8 元解包）。
  - 建议: `Optional[List[...]]` 显式化、为帧/纹理参数建立 Protocol 或用现有类型（GpuDecodedFrame/moderngl.Texture|_ExternalTextureProxy 联合），修正 7-tuple 注释。
  - 置信度: 高

### 通过项
- 线程安全（队列协同）：submit/set_generation_id/_coalesce 等提交侧操作全部持 `_submit_lock`；渲染线程消费与提交侧互不破坏队列不变式；`_generation_id`、`_snapshot_armed` 等 bool/int 标志为 GIL 原子读取，无锁可接受。
- 线程安全（GL 边界）：全部 GL 调用（含 `QOpenGLFunctions`、PyOpenGL fence/waitSync）限定在渲染线程且 context current 时执行；提交侧释放经 `safe_release_gpu_frame`（文档明确任意线程安全，IOSurface 槽位回收不做 GL 调用）；GL 生产/消费经 `GLContextManager.mutex` 共享命令车道串行化。
- 信号防递归：本文件只发射信号（frame_ready/present_frame_ready/render_error/snapshot_ready/slot_acked），无程序化控件更新，不存在递归回路；跨线程发射走 Qt 排队连接。
- 状态提交一致性：渲染器不持有 undo/revision/持久化状态，仅只读消费任务快照（frozen ClipLayerInfo、effects tuple），无一致性风险。
- 异常与取消路径：generation fence 双侧校验（submit + run 出队后）、deadline 过期丢弃、scrub 任务清空、背景任务永不驱逐交互任务、`stop()` 经 cancelled/complete Event 取消延迟握手、run() 30 秒有界等待防止死等——覆盖完整（槽位泄漏见 AUD-16601）。
- GPU 资源生命周期（常规路径）：IOSurface/comp handle 释放经 fence + 延迟 drain（上限 8 条、超限阻塞排空 1 条）、阻塞回退 glFinish、`_cleanup` 释放任务队列中的未处理任务源——外部源生命周期处理成熟（例外见 AUD-16602/16605）。
- GLSL 字面量与循环边界：内联 shader 数字全部显式 `.0`；循环边界为编译期常量（`i < 3`）；`#version 330` 与 sampler/attribute 用法一致；fullscreen-triangle 快照 shader 的 gl_VertexID 用法正确。
- 输入验证：`_classify_planar_format` 前置集合 + 子串回退、CFA 位深三链解析、comp handle 十余项契约校验、`_apply_crop` 的 CropParams 构造 try 保护、环境变量缓存上限解析容错——总体扎实（缺口仅 AUD-16610）。
- 性能模式：identity transform 预计算 mat3 字节、PBO 仅在 ≥2× 输出像素的大平面帧启用、快照仅在布防时回读且字节计入审计、复合缓存命中跳过解码+合成、LRU 受条目数与字节预算双重约束、present pool 满时背压不静默复用未 ack 槽。
