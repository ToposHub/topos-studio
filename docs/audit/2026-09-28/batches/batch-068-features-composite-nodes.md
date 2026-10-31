# 批次报告：batch-068 — src/features/composite/nodes/（fog/frame_hold/frame_interpolate/geo3d/glow/godrays/grade/group）

编号区间：AUD-6800–AUD-6899。审查基准：INSTRUCTIONS.md 九维度；支撑代码已核对
（base_node.py、base_evaluator.py 的 `_dispatch_effect`/`_resolve_output`、filter_evaluator.py、
color_evaluator.py、depth_common.py、_gpu_readback.py、gl_bind.py、core/shaders.py、
render3d/{primitives,aov,scene_payload}.py、scene3d_node.py 消费端、node_engine.py、
rife_interpolator.py、gpu_resource_pool.py）。

统计：Critical 1 · Major 4 · Suggestion 9。

---

## src/features/composite/nodes/fog_node.py (行数: 119)

### 发现
（无）

### 通过项
- 输入验证：`depth_marker_inputs` 强制 image+depth 双有效，缺深度输入时返回空输出（可见的缺输入降级，符合 depth_common 契约），不做静默 passthrough。
- 参数钳制：density≥0、start/end 钳到 [0,1] 且 `end=max(start,end)` 防倒置；`depth_params` 对非法 depth_units 回退 normalized。
- HDR 正确性：标记协议透传 image 的 format/bbox/alpha/color_space，雾混色按线性域设计，无 clamp(0,1) 暴力钳制。
- ROI/平铺：padding=0（纯逐像素），compute_input_roi 对 image/depth 同 ROI，与 `_apply_depth_tool` 双纹理绑定（unit 0/1）匹配。
- 线程安全：无 GL 操作、无可变跨线程状态；marker 由渲染线程解析。
- `cache_to_disk` 参数由 base_evaluator 磁盘缓存路径消费（base_evaluator.py:860-861），非死参数。

---

## src/features/composite/nodes/frame_hold_node.py (行数: 92)

### 发现
（无）

### 通过项
- 冻结帧语义由上游 time_remap 实现（core/time_remap.py:179 `frame_hold → TimeRemap(hold=hold_frame)`），节点 passthrough + 元数据设计正确；`_dispatch_effect` 对 "frame_hold" 返回 `{"result": effect_input}`，与协议一致。
- 输入验证：input None / `is_valid` 双检查。
- 无 GPU 资源持有、无线程安全问题；BYPASS 默认映射（first input→first output）对本节点正确。
- bbox/ROI 为纯透传（compute_output_bbox/compute_input_roi 一致）。

---

## src/features/composite/nodes/frame_interpolate_node.py (行数: 264)

### 发现
- [AUD-6800] [Major] 节点自建输出纹理不经 resource_pool/fb_manager 跟踪，任何释放路径都无法回收
  - 位置: src/features/composite/nodes/frame_interpolate_node.py:201（另 161-163 同类分支）
  - 证据:
    ```python
    data = np.ascontiguousarray(np.flipud(rgba))
    tex = gl.texture((w, h), 4, data=data.tobytes(), dtype="f4")
    return TextureResult(
        texture_id=tex.glo, width=w, height=h,
    ```
    ```python
    if out_tex is None:
        out_tex = gl.texture((w, h), 4, dtype="f4")
        own_tex = True
    ```
  - 影响: `gpu_resource_pool.release_texture_by_id` 只处理 `_active_textures`/fb_manager 跟踪的纹理（gpu_resource_pool.py:985-992），`_release_transient_textures` 同样走该入口——RIFE 模型路径 `_upload` 每次评估新建一张 w×h RGBA32F 纹理（1080p ≈ 33MB/帧），缓存逐出/取消时释放调用全部 no-op，GPU 内存持续累积，长时间预览/导出趋向 OOM。`_gpu_linear_blend` 的 own_tex=True 分支（资源池获取失败时）同理。
  - 建议: 输出纹理统一走 `resource_pool.acquire_texture`（成功路径已这么做）；确实需要自建时把纹理对象登记进 fb_manager 的活跃集合，或返回时附带可释放句柄。
  - 置信度: 高（释放链路已逐条核对；触发前提是安装了 RIFE 模型）

- [AUD-6801] [Major] 未实现 cleanup()：blend program/VBO 与 ONNX session 随节点移除/上下文切换泄漏
  - 位置: src/features/composite/nodes/frame_interpolate_node.py:111-127（_ensure_blend_resources），全文件无 cleanup 定义
  - 证据:
    ```python
    if getattr(self, "_blend_prog", None) is None \
            or self._blend_ctx is not gl:
        ...
        self._blend_prog = gl.program(
            vertex_shader=_PASSTHROUGH_VERTEX,
            fragment_shader=self._BLEND_FALLBACK_FRAGMENT,
        )
        self._blend_ctx = gl
    ```
  - 影响: base_evaluator._sync_render_nodes 移除节点时仅在 `hasattr(old_node, 'cleanup')` 时清理（base_evaluator.py:335-336）——本节点不满足，`_blend_prog`/`_blend_vbo`（GL program/VBO）与 `_interpolator`（缓存 ONNX session）永不释放；且 `_ensure_blend_resources` 在 GL ctx 变化时直接覆盖引用，旧 ctx 的 program/VBO 也随之泄漏。反复增删节点/切换上下文累积 GL 资源。
  - 建议: 实现 `cleanup()` 释放 `_blend_prog`/`_blend_vbo`（并置空 `_blend_ctx`/`_blend_vbo_ctx`）；ctx 切换时先释放旧资源再重建。
  - 置信度: 高

- [AUD-6802] [Suggestion] RIFE 模型路径保留每帧双全帧 GPU→CPU 回读 + CPU→GPU 上传
  - 位置: src/features/composite/nodes/frame_interpolate_node.py:249-261
  - 证据:
    ```python
    a = self._read_input(frame_a, ctx, fb_manager)
    b = self._read_input(frame_b, ctx, fb_manager)
    ...
    rgb = interpolator.interpolate(a[..., :3], b[..., :3], t, ...)
    ...
    result = self._upload(rgb, frame_a, ctx)
    ```
  - 影响: 4K 下每帧两次 `fbo.read`（RGBA32F 全帧）+ 一次上传，属 P2-9 已知遗留（注释自认），实时预览帧率受损。
  - 建议: 长期把 ONNX 输入/输出迁移到 GPUresident 缓冲或至少 ROI/降采样回读；中期可缓存相邻帧回读结果。
  - 置信度: 高（现状确认；严重度视使用频率而定）

### 通过项
- 端点短路正确：t≤0/t≥1 或单输入无效时直接返回端点帧（真 TextureResult，无 marker 泄漏）。
- GLSL 合规：`_BLEND_FALLBACK_FRAGMENT` 无数字字面量缺 `.0`、无循环；`_PASSTHROUGH_VERTEX` 的 `in_vert/in_uv` 与 VAO layout `'2f 2f'` 匹配（core/shaders.py:12-21）。
- GPU blend 失败路径防御完善：`bind_texture_unit` 对无上下文环境 no-op（gl_bind.py:42-57），渲染后解绑双 unit（P1-6 注释），异常统一 warning + 端点降级。
- `_ensure_blend_resources` 短路求值避免了未初始化属性读取（`_blend_ctx` 在首次读取前必被赋值）。
- 输入验证：`is_valid` 检查 + t 钳制 [0,1]；`params_require_frame_cache` 覆盖 t 的逐帧缓存维度。

---

## src/features/composite/nodes/geo3d_node.py (行数: 326)

### 发现
- [AUD-6803] [Critical] 实例参数变化时缓存 VAO 仍绑定已释放的旧 instance VBO，按新实例数渲染产生越界属性取数
  - 位置: src/features/composite/nodes/geo3d_node.py:149-167（释放重建）、169-182（VAO 复用早退）、274-279（调用顺序）
  - 证据:
    ```python
    if self._instance_vbo is not None:
        try: self._instance_vbo.release()
        except Exception: pass
        self._instance_vbo = None
    ```
    ```python
    if (primitive == self._last_primitive
            and self._mesh_vbo is not None
            and self._mesh_instance_enabled == instance_enabled):
        return  # 复用缓存
    ```
    ```python
    if instance_enabled:
        instance_count = self._ensure_instance_buffer(gl_ctx, grid_x, grid_z, spacing)
    self._ensure_mesh_geometry(gl_ctx, primitive, instance_enabled)
    ```
  - 影响: 复用条件只看 primitive/instance_enabled，不看实例签名。用户拖动 grid_x/grid_z/spacing（primitive 与实例开关不变）时：旧 `_instance_vbo` 被 `release()` 删除，新 VBO 建立，但 `_ensure_mesh_geometry` 早退、`_mesh_vao` 仍引用旧 buffer 名；payload 携带旧 VAO + 新 instance_count（如 3×3→5×3 时 15 个实例读 9 个 offset 的 buffer）。Scene3D beauty pass 按新实例数渲染该 VAO（scene3d_node.py:3078 `_gp.vao.render(_gmode, instances=_gp.instance_count)`），实例属性越界读取——GL 未定义行为：驱动相关表现为实例位置垃圾/闪烁/部分驱动崩溃；即使驱动按"删除即保活"处理，也渲染陈旧 offset，与 CPU 侧 `instance_offsets`（ID picking 用新值）不一致。
  - 建议: 把实例签名纳入 VAO 复用条件（primitive/instance_enabled/instance sig 任一变化即重建 VAO），或在 `_ensure_instance_buffer` 重建后强制失效 `_mesh_vao`（置 None 并清 `_last_primitive`）。scene3d_node.py:563-589/653-656 存在同款模式，建议一并修复。
  - 置信度: 高（调用链已逐行核对）

- [AUD-6804] [Major] _geo_revision 每次求值无条件自增，Scene3D 内容签名缓存恒 miss
  - 位置: src/features/composite/nodes/geo3d_node.py:290
  - 证据:
    ```python
    # content_revision：捕获所有影响渲染的参数变化（primitive/instance/transform/材质）。
    # Scene3D 把它进 content_sig，参数变了不复用陈旧纹理。
    self._geo_revision = getattr(self, "_geo_revision", 0) + 1
    ```
  - 影响: `GeometryPayload.signature()` 将 `content_revision` 计入（scene_payload.py:279），Scene3D 把 geo 签名并入 `scene_content_sig`（scene3d_node.py:3663）并据此走昂贵的资源重建路径（渲染目标/几何上传/IBL，仅签名不同即 `content_changed=True`）。revision 每次评估 +1（而非注释声称的"参数变化时"递增）意味着只要 Geo3D 参与 evaluate，Scene3D 的 sig 缓存永远 miss——相机交互、参数微调、动画播放期间每帧全量重建，违背 4K 实时基准；signature 中 model_matrix/材质/instance_offsets 本已覆盖参数变化，该字段的自增方式反噬缓存。
  - 建议: revision 只在实际影响渲染的参数（primitive/instance/transform/材质/render_role）变化时递增（对比上次 payload 参数快照），或直接依赖 signature 其余字段并去掉每次自增。
  - 置信度: 高（签名链路已核对；稳态下求值器 dirty-skip 会兜底，交互期损失成立）

- [AUD-6805] [Suggestion] primitive 参数未经白名单校验直传 get_primitive，损坏工程文件导致节点空输出
  - 位置: src/features/composite/nodes/geo3d_node.py:184、249
  - 证据:
    ```python
    primitive = str(self.parameters.get("primitive", "cube"))
    ...
    mesh = get_primitive(primitive)
    ```
    （primitives.py:642-644 `if gen is None: raise ValueError(...)`）
  - 影响: `Node.from_dict` 原样合并文件参数（base_node.py:1243-1245），拼错/手改的 primitive 值使 `_do_evaluate` 抛 ValueError；base_evaluator 捕获后 `cache[node_id] = {}`（base_evaluator.py:1034-1039）——该几何静默消失、仅一条 warning 日志，违背"外部数据须验证"铁律的处置预期。
  - 建议: 以 `_LINE_PRIMITIVES`/ParamDef labels 建白名单，未知值回退 "cube" 并记录诊断。
  - 置信度: 高（机制确定，触发需畸形输入）

### 通过项
- GL 资源生命周期：懒建 + cleanup() 完整释放 VAO/VBO/IBO/双 program/实例 VBO；`_ensure_mesh_geometry` 复用条件含 instance_enabled（P1.4 修复保留）。
- 线程安全：GL 资源构建全部在 `_do_evaluate`（渲染线程）内，ctx 经 `CompositeEvalContext.gl` 正确解包；payload 经 `_emit_scene_payload` 旁路传递。
- 输入验证：grid_x/grid_z ≥1、spacing ≥0、metallic/roughness/shadow_* 钳制；`render_flags_from_role` 自带未知 role 回退 solid（aov.py:283-298）。
- 类型注解完整（数值转换显式 int/float/bool）。

---

## src/features/composite/nodes/glow_node.py (行数: 148)

### 发现
- [AUD-6806] [Suggestion] compute_output_bbox 声明按 radius 扩张数据窗，实际派发输出不扩张，规划与结果不一致
  - 位置: src/features/composite/nodes/glow_node.py:60-69、93-95（对照 filter_evaluator.py:229-237）
  - 证据:
    ```python
    radius = float(params.get("radius", 15.0))
    return input_bbox.expanded(int(radius))
    ```
    而实际 GPU 结果为：
    ```python
    return {
        "result": TextureResult(
            texture_id=out_tex.glo, width=w, height=h,
            ...
            bbox=input_tex.bbox,
    ```
  - 影响: 调度器按扩张 bbox 规划 ROI（下游会请求超出输入的数据窗），但 `_apply_glow` 输出尺寸/ bbox 均等于输入——声明数据窗大于实际内容，规划侧过度请求、语义未实现；好在实际 TextureResult 携带正确 bbox，属规划层冗余而非像素错误。
  - 建议: 要么实现真正的外扩渲染（输出画布含 glow 溢出），要么把 compute_output_bbox 改回透传以与实现一致。
  - 置信度: 中（影响限于 ROI 规划层；未发现像素级错误路径）

### 通过项
- 参数钳制与早退：radius<0.1 或 intensity<0.01 直接返回输入（真实 TextureResult，无 marker）；preview 半径降采样与 quality→sample_count 映射明确。
- marker 协议合规：`_effect_type/_effect_input/_effect_params` 齐全，placeholder 字段被调度器丢弃并由 `_apply_glow` 重建元数据（format/bbox 取自输入），无不一致。
- HDR：threshold/tint 上限 2.0、无 clamp(0,1)；`_tile_padding`/`requires_roi_expansion` 一致返回 radius。
- 线程安全：无 GL 操作、无跨线程可变状态。

---

## src/features/composite/nodes/godrays_node.py (行数: 151)

### 发现
（无）

### 通过项
- 输入验证：light_x/y 钳 [-0.5,1.5]、density/weight ≥0、decay 钳 [0.5,1.0]、samples 钳 [16,64]（preview 再折半至 ≥8，与 `_apply_godrays` 的 max(8,min(64,...)) 双向一致）；mask 仅在有效时附加 `_effect_mask`（调度器按 is_valid 注入 `_mask_tex`，base_evaluator.py:2073-2074）。
- full-frame-only ROI 策略自洽：`compute_input_roi` 取输入 bbox 与 output_roi 的并集（径向 march 全局性），`requires_roi_expansion`=0，与文档声明一致。
- marker 协议：placeholder result 透传输入 format/bbox/alpha/color_space；参数名与 `_apply_godrays` 消费端逐项匹配（light_pos/density/weight/decay/sample_count/light_color）。
- `cache_to_disk` 由求值器磁盘缓存消费；无线程/GPU 资源持有。

---

## src/features/composite/nodes/grade_node.py (行数: 155)

### 发现
- [AUD-6807] [Suggestion] mask_channels 使用非规范 ptype "string"（全库规范为 "str"）
  - 位置: src/features/composite/nodes/grade_node.py:72
  - 证据:
    ```python
    # Channel mask
    ParamDef("mask_channels", "string", "rgb"),
    ```
    （base_node.py:224 类型注释 `"float", "int", "str", ...`；effect_spec.py:209 亦按 `"str"` 分派）
  - 影响: 属性面板/序列化按 `ptype == "str"` 分派的路径把该参数当未知类型处理（回落默认编辑器），类型语义丢失；与全库约定不一致，未来按 ptype 做校验/迁移的代码会漏掉它。
  - 建议: 改为 `"str"`；或改为 `"option"`（labels=["rgb","rgba","r","g","b",...]）更符合其离散语义。
  - 置信度: 高（不一致确定；当前无崩溃路径）

- [AUD-6808] [Suggestion] HELP_TEXT 与实现不符：声称 rgba 通道支持与负 gamma 处理，派发着色器均未实现
  - 位置: src/features/composite/nodes/grade_node.py:36-38、56-58（对照 core/shaders.py:910-933）
  - 证据:
    ```python
      - mask_channels: 指定应用校正的通道（rgb/rgba 等）
    ...
    - Negative gamma value handling
    ```
    ```python
    ParamDef("gamma_r", "float", 1.0, min_val=-4.0, max_val=4.0, step=0.01),
    ```
    而 `_GRADE_FRAGMENT` 中：
    ```python
    uniform vec3 u_channel_mask;
    ...
    g = pow(max(g, 0.0001), 1.0 / max(u_gamma[i], 0.001));
    ...
    fragColor = vec4(result, color.a);
    ```
  - 影响: channel_mask 是 RGB 三元 vec（alpha 恒直通），用户填 "rgba"/"a" 时 'a' 无任何效果；gamma 允许负值/0，但派发着色器把负 gamma 钳到 0.001（指数 1000 的幂）而非 _TOPOS_COLOR_FRAGMENT 那样的 sign 镜像处理（shaders.py:1004）——负 gamma 产生极端不可预期值，与文档承诺相悖。
  - 建议: 文档改为仅 rgb；或在节点端把 gamma 钳到正值区间；如需保留负 gamma，派发端采用 sign 镜像公式。
  - 置信度: 高（节点与着色器两侧均已核对）

### 通过项
- `has_changes` 早退正确：默认参数组合是恒等变换，早退返回输入（真实 TextureResult）不改变像素；mix=0 等边界经 `mix()` 语义安全。
- 数学安全：gamma=0/负由着色器 `max(u_gamma[i],0.001)` 防除零；whitepoint-blackpoint 差值有 0.0001 下限（shaders.py:920）。
- HDR：全程无 clamp(0,1)，支持 >1 值（lift/gain/gain 上限 4.0、whitepoint/multiply 至 4.0）。
- mask 可选输入：`_effect_mask` 为 None 时调度器跳过注入并绑定白色 mask（color_evaluator.py:114-121）。
- marker 协议、线程安全（无 GL/无跨线程状态）、bbox/ROI 默认逐像素透传。

---

## src/features/composite/nodes/group_node.py (行数: 405)

### 发现
- [AUD-6809] [Major] from_dict 反序列化 input_mappings/output_mappings/member_node_ids 无结构验证，畸形工程文件在加载期崩溃
  - 位置: src/features/composite/nodes/group_node.py:192-202
  - 证据:
    ```python
    node.member_node_ids = data.get("member_node_ids", [])

    input_mappings = data.get("input_mappings", {})
    node.input_mappings = {
        k: (v[0], v[1]) for k, v in input_mappings.items()
    }

    output_mappings = data.get("output_mappings", {})
    node.output_mappings = {
        k: (v[0], v[1]) for k, v in output_mappings.items()
    }
    ```
  - 影响: 违背"外部数据须验证"铁律。值非序列（int/None）→ TypeError 在工程加载时抛出（整个文件打不开）；值是长度≠2 序列 → ValueError/IndexError；值恰为 2 字符字符串（"ab"）→ 被静默拆成 ('a','b') 的错误映射。member_node_ids 同样未校验元素类型。promoted_params 已做了 isinstance 过滤，三个映射字段没有。
  - 建议: 逐项校验 `isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(x, str) for x in v)`，非法项丢弃并记日志；member_node_ids 过滤为 str 列表。
  - 置信度: 高（机制确定；触发需文件损坏/手改）

- [AUD-6810] [Suggestion] promote/unpromote 直接改 parameters/promoted_params，绕过 set_parameter 的脏状态/修订号提交
  - 位置: src/features/composite/nodes/group_node.py:278-284、295-297
  - 证据:
    ```python
    self.parameters[proxy_name] = self.promoted_params[proxy_name]["default"]
    ok, err = graph.set_parameter_binding(
        member_id, member_param, self.id, proxy_name)
    ```
    ```python
    self.promoted_params.pop(proxy_name, None)
    self.parameters.pop(proxy_name, None)
    ```
  - 影响: 不经过 `set_parameter`（不 bump param_revision/_dirty/输出 revision，base_node.py:643-651），组节点自身的缓存失效依赖成员 binding 表达式的 dirty 传播兜底；undo/redo 快照若按 param_revision 判断也会漏掉 promote 引入的状态突变（add_member 系列倒是有 `_dirty = True`，此处没有）。
  - 建议: 参数种子经 `set_parameter(proxy_name, default)` 写入；promote/unpromote 后置 `self._dirty = True` 并 bump param_revision。
  - 置信度: 中（正确性风险依赖引擎侧兜底范围，状态提交不一致本身确定）

- [AUD-6811] [Suggestion] 显式 proxy_name 与既有 promotion 重名时静默覆盖，旧成员 binding 变孤儿
  - 位置: src/features/composite/nodes/group_node.py:256-267、279-284
  - 证据:
    ```python
    if not proxy_name:
        proxy_name = f"{member_param}"
        if proxy_name in self.promoted_params:
            return False, f"already promoted as {proxy_name}"
    ...
    self.promoted_params[proxy_name] = {
        "member_id": str(member_id), ...
    ```
  - 影响: 重名检查只在自动命名分支；显式传入已存在的 proxy_name 时直接覆盖 `promoted_params[proxy_name]`，旧成员的表达式 binding 仍残留（`set_parameter_binding` 按 (member,param) 维度覆盖），unpromote 只解除新 meta 指向的 binding——旧成员永远跟随一个 UI 上已不存在的代理参数，行为不可追溯。
  - 建议: 显式路径同样检查 `proxy_name in self.promoted_params` 并拒绝（或返回已存在的错误信息）。
  - 置信度: 高（逻辑确定；UI 现路径大概率只用自动命名，故降为 Suggestion）

- [AUD-6812] [Suggestion] 死代码与遗留路径：_compute_cache_key 无调用方、_internal_eval_cache 从未使用、_last_result 回退永不命中
  - 位置: src/features/composite/nodes/group_node.py:87、348-352、361-405
  - 证据:
    ```python
    # Internal evaluation cache for member nodes
    self._internal_eval_cache: Dict[str, Dict[str, TextureResult]] = {}
    ```
    ```python
    if hasattr(member_node, '_last_result') and member_node._last_result:
    ```
  - 影响: `_compute_cache_key` 全库无调用（缓存键权威在 CacheManager.compute_key，base_node.py:38-40 注释明确）；`_internal_eval_cache` 声明后从未读写；base Node 无 `_last_result` 属性（base_node.py 全文核对），该回退分支恒 False——维护者会误以为存在双通道结果提取。另注：`_do_evaluate` 读取模型侧 `self._node_graph._evaluation_cache` 私有属性，与快照层重接线（IS_PLACEHOLDER 注释）并存，属遗留兼容路径。
  - 建议: 删除 `_compute_cache_key`/`_internal_eval_cache`；`_last_result` 回退一并删除或在 Node 基类明确契约。
  - 置信度: 高（调用方已全库检索）

- [AUD-6813] [Suggestion] group_name/is_expanded/group_color 参数与实例属性双轨状态，面板编辑不生效、持久化以属性为准
  - 位置: src/features/composite/nodes/group_node.py:55-59、72-73、165-184（对照 to_dict/from_dict）
  - 证据:
    ```python
    PARAM_DEFS: List[ParamDef] = [
        ParamDef(name="group_name", ptype="str", default="Group"),
        ParamDef(name="is_expanded", ptype="bool", default=False),
        ParamDef(name="group_color", ptype="str", default="#4A90D9"),
    ]
    ```
    而序列化/恢复用独立属性：
    ```python
    "is_expanded": self.is_expanded,
    "group_color": self.group_color,
    ```
  - 影响: 属性面板编辑这三个参数只写 `self.parameters`，而 to_dict 的顶层键从 `self.is_expanded`/`self.group_color` 属性取值（from_dict 亦恢复到属性）——面板修改不会反映到组的外观/展开态，重载后丢失；`is_expanded` 尤其同时存在于 parameters 与属性两处，状态可分叉。
  - 建议: 二选一：去掉这三个 ParamDef（组属性由专用 UI 命令维护属性），或让属性 getter/setter 委托 parameters 单一事实源。
  - 置信度: 高（读写两侧路径均已核对）

### 通过项
- promote_param 核心校验完备：成员存在性/成员关系/pdef 存在/`is_expression_compatible`（仅 float/int）、自动命名去重、binding 失败回滚（pop promoted_params + parameters）。
- `set_parameter_binding`/`remove_parameter_binding` 与 NodeGraph 实现签名匹配（node_engine.py:649-706），含环检测与表达式别名。
- to_dict/from_dict 往返：mappings 元组↔列表转换正确；promoted_params 序列化带 isinstance 防御，旧图缺键兼容。
- promoted 参数经 get_param_defs 动态生成 float ParamDef（roto 动态通道模式），min/max/step/default 从成员 pdef 派生，animated=True 支持关键帧。
- `_do_evaluate` 的 passthrough 回退正确（无 graph/无映射时 first input→第一个输出端口，真实 TextureResult 无 marker 泄漏）；对 `_evaluation_cache` 的访问有 hasattr 防御，成员缺失时 continue 不崩溃。
- 无 GPU 资源持有、无线程安全问题（全部模型侧状态）。
