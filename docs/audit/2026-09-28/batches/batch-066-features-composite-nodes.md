# Batch batch-066 — src/features/composite/nodes/（cs_transform / deep_merge / deflicker / defocus / deformer3d / denoise / depth_common / depth_matte）

编号区间：AUD-6600–AUD-6699。审查日期：2026-09-28。
共 8 个文件、1,930 行，全部通读；关键跨文件契约（base_node 标记协议、base_evaluator 派发/缓存所有权、
filter/blur/merge/transform evaluator、effect_pass_runner、effect_tiers、MeshVBOPool、FramebufferManager、
moderngl 5.12 资源回收行为）均已 Read 核实。

发现计数：Critical 1 / Major 4 / Suggestion 8。

---

## src/features/composite/nodes/cs_transform_node.py (行数: 91)

### 发现
- [AUD-6600] [Suggestion] `direction` 参数未做合法性校验，非法持久化值被静默当作 linear_to_srgb
  - 位置: src/features/composite/nodes/cs_transform_node.py:68-77
  - 证据:
    ```python
    direction = self.parameters.get("direction", "srgb_to_linear")

    # No-op if already in target space
    if direction == "srgb_to_linear" and image_input.color_space == ColorSpace.LINEAR:
        return {"image": image_input}
    if direction == "linear_to_srgb" and image_input.color_space == ColorSpace.SRGB:
        return {"image": image_input}

    # Determine the target color_space for metadata
    target_cs = ColorSpace.LINEAR if direction == "srgb_to_linear" else ColorSpace.SRGB
    ```
  - 影响: 参数经 `Node.from_dict` 从工程文件原样合并（base_node.py:1243-1245 不做清洗）。手工编辑/旧版工程中
    `direction` 若为任意字符串（非两个合法值之一），no-op 判定全部落空，`u_direction=1`、`target_cs=SRGB`，
    对本已 linear 的输入再编码一次 sRGB——色彩错误且无任何提示。
  - 建议: 与 depth_common.depth_params 同款处理：值不在 `CS_TRANSFORM_MODES` 时回落默认值并记 warning。
  - 置信度: 高

### 通过项
- 线程安全：纯标记节点，无共享可变状态，求值在渲染线程单线程执行
- GPU 资源生命周期：不直接创建 GL 对象，占位 TextureResult(texture_id=-1) 由 `_apply_cs_transform`
  （transform_evaluator.py:397-440）用资源池纹理替换，生命周期正确
- GLSL/HDR 正确性：转换由 `_CS_TRANSFORM_FRAGMENT` 完成，节点侧仅传 direction/target_color_space，
  无 clamp(0,1)，符合 HDR 内部约定
- 信号防递归：无 Qt 控件，不适用
- 状态提交一致性：参数经 set_parameter 修改会提升 param_revision，缓存键随之失效
- 异常与取消路径：无效输入早退返回 {}，符合"缺输入可见空白"约定
- 标记协议：`_effect_type/_effect_input/_effect_params` 与 base_node.py:9-35 文档及派发端
  （base_evaluator.py:2097-2098）一致；`direction` 双模式 int 映射与 shader u_direction 匹配
- 性能模式：no-op 早退（色彩空间已在目标空间时直接透传）正确

---

## src/features/composite/nodes/deep_merge_node.py (行数: 223)

### 发现
- [AUD-6601] [Suggestion] 单输入旁路分支可能把无效 TextureResult 当作 result 下发
  - 位置: src/features/composite/nodes/deep_merge_node.py:140-147
  - 证据:
    ```python
    if input_a is None and input_b is None:
        return {}

    # Pass through if only one input
    if input_a is None or not input_a.is_valid:
        return {"result": input_b} if input_b else {}
    if input_b is None or not input_b.is_valid:
        return {"result": input_a}
    ```
  - 影响: `input_a` 无效且 `input_b` 存在但 `is_valid=False` 时，返回 `{"result": <无效纹理>}` 而非 `{}`。
    下游拿到"已连接但无效"的结果；多数消费者按缺失处理，但与"缺输入返回空"的节点约定不一致，
    依赖 `if not image or not image.is_valid` 之外写法（如仅判 None）的下游会出错。
  - 建议: 该分支改为 `if input_b and input_b.is_valid: return {"result": input_b}` 否则 `return {}`。
  - 置信度: 高
- [AUD-6602] [Suggestion] `_bypass_evaluate` 中 BYPASS_MAPPING 分支之后的代码不可达（死代码）
  - 位置: src/features/composite/nodes/deep_merge_node.py:208-223
  - 证据:
    ```python
    # Use BYPASS_MAPPING if defined
    if self.BYPASS_MAPPING:
        result: Dict[str, TextureResult] = {}
        for out_port, in_port in self.BYPASS_MAPPING.items():
            tex = inputs.get(in_port)
            if tex and tex.is_valid:
                result[out_port] = tex
        return result

    # Default: pass through first available input
    if input_a and input_a.is_valid:
        return {"result": input_a}
    ```
  - 影响: 类属性 `BYPASS_MAPPING` 恒为真值（第 60 行），217-223 行永远不会执行；且这段逻辑与
    base_node.py:966-984 的基类实现完全重复。维护时易误以为禁用行为会走"first available"路径。
  - 建议: 删除整个 `_bypass_evaluate` 覆写，直接复用基类（BYPASS_MAPPING 已声明）；或将不可达分支删除。
  - 置信度: 高

### 通过项
- 线程安全：无常驻可变状态
- GPU 资源生命周期：占位结果 texture_id=-1，真实纹理由 `_apply_deep_merge`/`_dispatch_merge_effect`
  从资源池获取并纳入 CACHE_OWNED_TYPES 释放路径
- GLSL/HDR 正确性：参数钳制（`_get_param_clamped`）到位，未引入无理由 clamp
- 标记协议双键名核实：无深度回落用 `_effect_input_a/_effect_input_b/_effect_input_mask`
  （匹配 `_dispatch_merge_effect`，base_evaluator.py:2214-2216）；深度路径用 `_effect_mask`
  （匹配 `_apply_deep_merge`，merge_evaluator.py:97-99）——两套键名虽易混淆但各自正确
- bbox/ROI 契约：`compute_output_bbox` 四种 policy 齐全，与 MergeNode 同构；`compute_input_roi`
  对 A/B/mask 一并下发输出 ROI 正确
- 输入验证：opacity/depth_tolerance 均经 `_get_param_clamped(0,1)`
- 性能模式：单输入直接透传，无双读

---

## src/features/composite/nodes/deflicker_node.py (行数: 601)

### 发现
- [AUD-6603] [Critical] 每帧输出纹理没有任何所有者且从不释放——GPU 显存随评估帧数无界增长
  - 位置: src/features/composite/nodes/deflicker_node.py:308-324, 418-426, 453-459
  - 证据:
    ```python
    # _upload_result（legacy CPU 路径，每帧执行）
    data = np.ascontiguousarray(np.flipud(rgba))
    tex = gl.texture((w, h), 4, data=data.tobytes(), dtype="f4")
    return TextureResult(texture_id=tex.glo, ...)
    ```
    ```python
    # _composite_gpu（GPU 路径）
    if resource_pool is not None:
        try:
            out_tex = resource_pool.acquire_texture(w, h, components=4, dtype="f4")
        except Exception:
            out_tex = None
    if out_tex is None:
        out_tex = gl.texture((w, h), 4, dtype="f4")
    ...
    return TextureResult(texture_id=out_tex.glo, ...)
    ```
  - 影响: 三条子路径全部泄漏——(1) legacy 路径每帧新建 `gl.texture`，Python 侧仅保留整数 glo；
    (2) GPU 池路径 acquire 后无人调用 release_texture；(3) 池失败回退路径每帧新建纹理且经
    `fb_manager.get_fbo(out_tex)` 永久登记进 `_fbo_cache`（gl_backend.py:98-105，仅 cleanup 清空）。
    所有权链核实：deflicker 注册为 `owns_color_texture=False`（node_capabilities.py:870-877）→ 不在
    CACHE_OWNED_TYPES → `_collect_owned_texture_ids` 返回空集（base_evaluator.py:1279-1280）→
    缓存逐出/瞬态释放都拿不到该 glo；moderngl 5.12 默认 `gc_mode=None`（本机实测：连续新建纹理 glo
    单调递增不回收），且全库无任何 `ctx.gc()` 调用。播放/逐帧导出时每评估一个新帧泄漏一张全分辨率
    RGBA f4 纹理（1080p ≈33MB，4K ≈133MB），数百帧即耗尽显存导致分配失败/崩溃。
  - 建议: 与 Constant/Text 同款"自管"策略——节点持有最近输出纹理对象并在下一帧/参数变更/cleanup 时
    `release()`；池路径在结果被替换时调用 `resource_pool.release_texture(out_tex)`；或改为输出
    `_effect_type` 标记让求值器按 CACHE_OWNED_TYPES 统一接管。
  - 置信度: 高
- [AUD-6604] [Major] 节点持有 GL 资源（_luma_tex、两个 program、VBO）但未实现 cleanup()；ctx 切换时旧资源直接覆盖不释放
  - 位置: src/features/composite/nodes/deflicker_node.py:327-343, 366-375
  - 证据:
    ```python
    def _ensure_gpu_resources(self, gl: Any) -> tuple:
        """Lazy-compile the luma/composite programs and quad VBO (per ctx)."""
        if getattr(self, "_gpu_ctx", None) is not gl:
            ...
            self._luma_prog = gl.program(...)
            self._composite_prog = gl.program(...)
            self._gpu_vbo = gl.buffer(self._QUAD_VERTICES)
            self._gpu_ctx = gl
        return self._luma_prog, self._composite_prog, self._gpu_vbo
    ```
    （全文件无 `def cleanup`；基类 Node 也没有——已核实 base_node.py 全文）
  - 影响: `_sync_render_nodes` 删除节点时只在 `hasattr(old_node, 'cleanup')` 才清理
    （base_evaluator.py:332-336），故从画布删除 Deflicker 节点即泄漏 `_luma_tex`（R32F 全分辨率
    8-33MB）、两个 shader program、VBO，以及 `_luma_tex` 在 `_fbo_cache` 里的陈旧 FBO。ctx 更换时
    旧 program/VBO 被静默覆盖（同 ctx 的 `_luma_tex` 有条件释放，program/VBO 没有）。对照：
    particle_node.py:537-571 明确实现了同场景的资源释放（`_release_gpu_objects(release=(self._gpu_ctx is gl))`）。
  - 建议: 实现 `cleanup()`（释放 `_luma_tex`/program/VBO，仅在 ctx 仍存活时），并在 `_ensure_gpu_resources`
    的 ctx 切换分支里对旧 ctx 资源走 particle 同款 `release=False` 丢弃逻辑。
  - 置信度: 高
- [AUD-6605] [Major] 环形缓冲上限 96 帧全分辨率 float32 平面（RGB 模式 ×3 通道），峰值内存过高
  - 位置: src/features/composite/nodes/deflicker_node.py:49, 254-265
  - 证据:
    ```python
    _BUFFER_MAX_ENTRIES = 96
    ...
    def _store_luma(self, frame, luma, rgb=None):
        self._luma_buffer[int(frame)] = luma
        if rgb is not None:
            self._rgb_buffer[int(frame)] = rgb
        # Prune far-away frames to bound memory.
        if len(self._luma_buffer) > _BUFFER_MAX_ENTRIES:
    ```
  - 影响: 剪枝只在超过 96 条后触发，且触发后仅保留当前尾随窗（5-9 帧），随后又重新累积到 96 条，
    峰值驻留 = 96 张平面。luma (H,W) float32：1080p ≈8.3MB/张 → 峰值 ≈796MB；4K ≈33MB/张 → 峰值
    ≈3.2GB。`per_pixel + luma_only=False` 模式还叠加 `_rgb_buffer`（每帧 RGB 三通道，1080p 24.9MB、
    4K 99.5MB/张 → 峰值 2.4GB / 9.6GB）。远超 4K 实时基准可接受的内存预算，易触发系统级交换或 OOM。
  - 建议: 上限按分辨率自适应（如按字节数封顶，例如 ≤256MB），或在剪枝时保留
    `min(96, 字节预算换算条数)`；RGB 缓冲可降采样或仅缓存窗口所需帧数。
  - 置信度: 高（内存数值由代码直接推得；96 条上限可能是有意支持回看，但未做字节维度约束）

### 通过项
- GLSL 正确性：`_LUMA_EXTRACTION_FRAGMENT`/`_GAIN_COMPOSITE_FRAGMENT` 字面量均带 .0，
  Rec.709 系数与 CPU `luma_of` 一致；`max(...,0.0)` 无上限 clamp，HDR 安全
- GPU/CPU 一致性：global_gain 标量在两条路径共用 `compute_global_gain`，GPU 混合着色器与
  `apply_deflicker` 语义一致（RGB 乘增益、alpha 原样、下限 0）
- 线程安全：单例 live 实例（action_type="source"，node_capabilities.py:870-877）只在渲染线程求值；
  `warming/error_message` 为渲染线程写、UI 读的简单标志
- 窗口语义：负帧防御（注释引用 P2-9）、冷启动/跳帧可见降级（warming + error_message）、
  分辨率不匹配平面按缺失处理（`_collect_window` expected_shape 过滤）均正确
- 缓存契约：`params_require_frame_cache` 返回 True，帧号进缓存键（base_evaluator.py:824-827 已核实消费）
- 异常路径：GPU 各步骤失败均回落 legacy 链而非崩溃；`_ENV_FORCE_CPU` 提供逃生门
- 输入验证：percentile/mode/window 均有钳制或白名单（`_window_size` 的 except (TypeError, ValueError) 窄且合理）
- 信号防递归：无 Qt 控件，不适用

---

## src/features/composite/nodes/defocus_node.py (行数: 170)

### 发现
- [AUD-6606] [Suggestion] 预览半径下限 `max(5.0, max_radius * 0.4)` 恒不生效（死逻辑）
  - 位置: src/features/composite/nodes/defocus_node.py:143-145
  - 证据:
    ```python
    # Preview mode: reduce max radius for faster interactive response
    if render_mode == "preview" and max_radius > 15.0:
        max_radius = max(5.0, max_radius * 0.4)
    ```
  - 影响: 该分支仅在 `max_radius > 15.0` 时进入，此时 `max_radius * 0.4 > 6.0 > 5.0`，`max(5.0, ...)`
    永远取乘积项——下限是死代码。若原意是"预览半径至少 5.0"（对 12.5-15 区间也生效），则当前实现
    与意图不符；若无意，则是误导性冗余。
  - 建议: 明确意图后删除下限，或把条件改为 `max_radius > 12.5` 使下限可达。
  - 置信度: 高

### 通过项
- 线程安全：无状态标记节点
- GPU 资源生命周期：占位结果由 `_apply_defocus`（blur_evaluator.py:328-369）用资源池纹理替换并纳入
  缓存所有权释放
- GLSL/HDR 正确性：节点仅转发参数，shader 端 `max(focal_size, 0.01)` 自防（shaders.py:1281），
  无 clamp(0,1)
- 参数派发契约：focal_x/y/size/max_radius/mode/edge_policy/process_alpha 与
  `_defocus_params_to_uniforms`（blur_evaluator.py:291-326）逐一对齐（旧 iris_size 缺陷已修复）
- bbox/ROI 契约：`compute_output_bbox`/`compute_input_roi`/`_tile_padding` 三处一致按 max_radius 扩展，
  预览半径缩小只影响着色采样不影响几何扩张（安全方向）
- 状态提交一致性：参数经 set_parameter 修订，缓存键含参数
- 异常路径：`max_radius < 0.1`（含负值）早退透传
- 性能模式：preview 与 mipmap 双重降档为有意设计

---

## src/features/composite/nodes/deformer3d_node.py (行数: 490)

### 发现
- [AUD-6607] [Major] 变形缓存签名与 mesh program 不含 GL context 身份，上下文更换后复用已死 VAO/program
  - 位置: src/features/composite/nodes/deformer3d_node.py:133-152, 418-435
  - 证据:
    ```python
    def _ensure_mesh_program(self, ctx):
        if self._mesh_prog is None:
            from ..core.shaders import _MESH_VERTEX, _MESH_PBR_FRAGMENT
            self._mesh_prog = ctx.program(
                vertex_shader=_MESH_VERTEX, fragment_shader=_MESH_PBR_FRAGMENT)
        return self._mesh_prog
    ```
    ```python
    current_sig = (upstream_mesh_sig, self._deformer_param_sig(),
                   upstream.signature())
    if current_sig == self._deform_sig and self._deformed_meshes:
        # 缓存命中：动画矩阵已经包含在 upstream.signature() 中。
        deformed_meshes = self._deformed_meshes
    ```
  - 影响: `_deform_sig` 与 `upstream.signature()` 都不含 GL context；三个 `_ensure_*_program` 只判
    `is None`，不像 deflicker `_ensure_gpu_resources` / particle `_release_gpu_objects` 那样按 ctx 身份
    重建。若渲染上下文重建而 live 节点实例存续（代码库明确承认该场景：particle_node.py:540-541 注释
    "context recreation"；deflicker 以 `_gpu_ctx is gl` 防御同一问题），缓存命中路径会把旧 ctx 的
    VAO 直接塞进 payload 给 Scene3D 渲染——绑定已销毁 GL 对象，轻则 mesh 消失/报 GL 错误，重则驱动级
    崩溃；缓存未命中路径则用旧 program 在新 ctx 上建 VAO，被 364 行宽异常吞掉后每帧输出
    "deformer.vao_upload_failed" 降级。
  - 建议: 在 `current_sig` 中加入 `id(gl_ctx)`（或缓存 `_last_gl_ctx is gl` 判定，不等则强制
    `_release_deformed_meshes` + 置空三个 program），对齐 particle/deflicker 的 ctx 键控模式。
  - 置信度: 中（ctx 重建时 live 节点是否存续取决于求值器生命周期，未能在本批次内完全溯源；
    但同库两个兄弟节点均为此场景做了防御，风险模式成立）

### 通过项
- 线程安全：live 实例仅渲染线程求值；`_last_diagnostics` 等实例状态无跨线程写
- GPU 资源生命周期（常规路径）：`_release_deformed_meshes` 对 VAO release + MeshVBOPool 引用计数
  release 的键（content_hash, is_skinned, has_tangent）与 acquire 键（mesh_vbo_pool.py:90 与
  117）核实一致；`cleanup()` 释放 mesh 资源与三个 program，并正确缓存 `_last_gl_ctx` 供无参 cleanup 使用
- 蒙皮/变形正确性：skin→deformer 顺序正确；烘出的普通 mesh 去掉 bone 属性、`anim_worlds` 仅在仍有
  is_skinned 透传 mesh 时保留（避免 Scene3D 二次蒙皮），失败时带诊断透传
- 异常路径：变形失败捕获窄异常（ValueError/TypeError/LinAlgError）并记 warning+诊断；无上游/无模型
  时 emit `has_model=False` 降级 payload 而非崩溃
- 输入验证：`_build_deformer_spec` 全参数 float()/str()/bool() 强转；非 dict 的 flat_mesh 跳过
- 状态提交一致性：`transform_sig` 级联上游签名+变形参数，Scene3D 据此重渲染；参数动画经快照烘焙后由
  `_sync_render_nodes` 同步进 live 实例（node_engine.py:1764 已核实 evaluated_params 进快照）
- 缓存诊断复现核实：缓存命中复用的 `_last_diagnostics` 描述的正是产出该缓存 mesh 的那次变形，无累积错报

---

## src/features/composite/nodes/denoise_node.py (行数: 161)

### 发现
- [AUD-6609] [Major] `quality` 档位未写入 `_effect_params`，合成页 GPU 派发完全忽略质量设置
  - 位置: src/features/composite/nodes/denoise_node.py:46-49, 155-160
  - 证据:
    ```python
    # T-P1-21 batch 2: 质量档位。fast 封顶 kernel 半径（预览级）、
    # high 加倍（封顶 schema 上限 10）；effective 半径换算在
    # effect_tiers.effective_denoise_radius，GPU/CPU 同契约。
    ParamDef("quality", "option", "normal", labels=QUALITY_LEVELS),
    ```
    ```python
    "_effect_params": {
        "strength": strength,
        "radius": radius,
        "luminance_only": luminance_only,
        "chroma_only": chroma_only,
    },
    ```
  - 影响: 合成页路径为 `_dispatch_effect("denoise") → _apply_denoise`
    （base_evaluator.py:2141-2142 → filter_evaluator.py:1218-1247），后者只读 strength/radius/
    luminance_only/chroma_one，`quality` 根本不在 marker 里——fast/normal/high 在合成页是死参数，
    UI 上调档无任何效果。`effective_denoise_radius` 只被 Edit 页的
    effect_pass_runner.py:253-255 与导出 CPU 路径（timeline_export.py:1523-1524）使用，导致同一节点
    在合成页与 Edit/导出路径行为不一致（与注释宣称的"GPU/CPU 同契约"相悖）。
  - 建议: 在 `_effect_params` 中带上 `"quality": self.parameters.get("quality", "normal")`，并让
    `_apply_denoise` 的 u_radius 经 `effective_denoise_radius(radius, quality)` 换算（注意与 preview
    半径折减的先后关系）。
  - 置信度: 高
- [AUD-6610] [Suggestion] `compute_input_roi` 的 radius 未做 int 化，与 `_tile_padding` 类型处理不一致
  - 位置: src/features/composite/nodes/denoise_node.py:84-87, 100-102
  - 证据:
    ```python
    def compute_input_roi(self, output_roi, params, input_bboxes=None):
        radius = params.get("radius", 3)
        expanded_roi = output_roi.expanded(radius)
    ```
    ```python
    def _tile_padding(self, params: Dict[str, Any]) -> int:
        radius = params.get("radius", 3)
        return int(radius)
    ```
  - 影响: 快照参数来自工程文件原样合并；若 radius 为字符串（手工编辑/异常持久化），
    `BBox.expanded`（format.py:83-90）执行 `self.x - margin` 直接 TypeError，打断 ROI 传播而非降级。
    `_tile_padding` 已示范了正确写法。
  - 建议: `radius = int(params.get("radius", 3))`，并钳到 [1,10]。
  - 置信度: 高
- [AUD-6611] [Suggestion] `luminance_only` 与 `chroma_only` 可同时为 True，无互斥约束
  - 位置: src/features/composite/nodes/denoise_node.py:50-51, 132-133
  - 证据:
    ```python
    ParamDef("luminance_only", "bool", False),
    ParamDef("chroma_only", "bool", False),
    ```
    ```python
    luminance_only = self.parameters.get("luminance_only", False)
    chroma_only = self.parameters.get("chroma_only", False)
    ```
  - 影响: 两个布尔直接透传给 shader 的 u_luminance_only/u_chroma_only；同时开启时行为取决于
    shader 内部分支叠加顺序，语义未定义且 UI 未阻止，用户会得到不可预期的降噪结果。
  - 建议: `_do_evaluate` 中互斥处理（如 chroma_only 优先并清 luminance_only），或 UI 侧联动。
  - 置信度: 中（shader 端具体叠加行为未在本次范围内取证，但无约束本身确定）

### 通过项
- 线程安全：无状态标记节点
- GPU 资源生命周期：输出纹理由 `_apply_denoise` 从资源池获取，节点在 CACHE_OWNED_TYPES 内
  （owns_color_texture=True 族），逐出时统一释放
- GLSL/HDR 正确性：参数钳制（strength 0-1、radius 1-10）到位；无 clamp(0,1)
- bbox/ROI 契约：`compute_output_bbox` 原样透传、`compute_input_roi`/`_tile_padding`/`requires_roi_expansion`
  均按 kernel 半径扩展且三者一致
- 异常路径：strength < 0.01 早退透传
- 性能模式：preview 半径折减（radius>4 → ×0.5，下限 2）逻辑正确，padding 按全半径保守取值
- 状态提交一致性：参数经 set_parameter 修订，动画经快照烘焙（denoise 属 effect 类，走
  `_create_ephemeral_node` 的插值路径）

---

## src/features/composite/nodes/depth_common.py (行数: 79)

### 发现
（无）

### 通过项
- 通过（无逻辑缺陷）。该文件是 A3 深度工具的共享契约模块，导出：`DEPTH_UNITS` 常量、
  `DEPTH_INPUT_GLSL` 着色器前缀、`depth_to_normalized`（CPU 镜像）、`depth_params`（参数提取+白名单
  校验）、`depth_marker_inputs`（输入校验）、`image_output_bbox`（bbox 辅助）
- GLSL 正确性：`DEPTH_INPUT_GLSL` 字面量均带 .0（`0.00001`、`0.0, 1.0`），`max(u_far - u_near, 0.00001)`
  防除零；`u_depth_units == 1` 整型比较合法；该前缀被 zblur/fog/depth_matte 三个 shader 复用
  （shaders.py:6920 拼接核实）
- CPU/GPU 一致性：`depth_to_normalized` 与 GLSL depth01() 逐式对应（含 1e-5 下限）
- 输入验证：`depth_params` 对 depth_units 做白名单回落；`depth_marker_inputs` 要求 image+depth
  双有效否则返回空——符合"缺 depth 输入输出可见空白"的模块契约
- 线程安全：纯函数无状态

---

## src/features/composite/nodes/depth_matte_node.py (行数: 115)

### 发现
- [AUD-6612] [Suggestion] `range_start > range_end` 未做归一化（swap），反转区间输出几乎为空的 matte
  - 位置: src/features/composite/nodes/depth_matte_node.py:89-94
  - 证据:
    ```python
    z0 = min(1.0, max(0.0,
             float(self.parameters.get("range_start", 0.3))))
    z1 = min(1.0, max(0.0,
             float(self.parameters.get("range_end", 0.7))))
    ```
  - 影响: 两个端点各自钳到 [0,1] 但不排序。shader 端
    `smoothstep(u_range_start - f, u_range_start + f, z) * (1.0 - smoothstep(u_range_end - f, u_range_end + f, z))`
    （shaders.py:6929-6930）在 start > end 时乘积几乎处处为 0，仅边界羽化区可能残留窄条——用户把
    起止拖反后得到"看似失效"的节点，无提示。
  - 建议: 求值前 `z0, z1 = min(z0, z1), max(z0, z1)`（或 UI 拖拽联动约束）。
  - 置信度: 高

### 通过项
- 线程安全：无状态标记节点
- GPU 资源生命周期：占位结果由 `_apply_depth_tool`（filter_evaluator.py:1393-1483）用池纹理替换并
  纳入缓存释放；matte 输出 alpha_premultiplied=True 与白色×覆盖 alpha 语义匹配
- GLSL 正确性：feather 钳到 [0,0.5] 与 shader `max(u_feather, 0.0001)` 双保险
- 标记协议：`_effect_depth` 键与 base_evaluator.py:2027-2029 的 `outputs.get("_effect_depth")` 匹配；
  `_effect_params` 的 depth_units/near/far/range_*/feather 与 `_apply_depth_tool` 读取的 uniform 一一对应
- bbox/ROI 契约：输出 bbox 透传 image、双输入同 ROI、padding 0（纯逐像素）一致
- 输入验证：`depth_marker_inputs` 保证缺 depth 时输出空（可见空白而非静默透传）
- 状态提交一致性：range/feather/near/far 均可动画，经快照烘焙生效；`cache_to_disk` 属 effect 类
  节点磁盘缓存开关（base_evaluator.py:856-861 已核实消费面——本节点 action 为 effect 时生效）
- 性能模式：无逐像素以外开销，cache_to_disk 默认关闭合理

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-6603 |
| Major | 4 | AUD-6604, AUD-6605, AUD-6607, AUD-6609 |
| Suggestion | 8 | AUD-6600, AUD-6601, AUD-6602, AUD-6606, AUD-6610, AUD-6611, AUD-6612（及保留观察项无） |

复核说明：所有发现均基于对 8 个目标文件全文及所引用的 base_node.py、base_evaluator.py、
filter_evaluator.py、blur_evaluator.py、merge_evaluator.py、transform_evaluator.py、effect_pass_runner.py、
node_capabilities.py、node_engine.py、mesh_vbo_pool.py、gpu_resource_pool.py、gl_backend.py、
shaders.py 的实际 Read；moderngl 5.12 `gc_mode=None` 行为经本机独立实验证实（连续新建纹理 glo 单调
递增、无自动回收）。
