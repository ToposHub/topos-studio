# batch-067 审查报告 — src/features/composite/nodes/（第二批：difference_keyer → file_output）

- 任务书：docs/audit/2026-09-28/assignments/batch-067.md
- 编号区间：AUD-6700–AUD-6799（实际使用 AUD-6700–AUD-6713）
- 文件数：8（1,382 行）；全部 Read 通读，关键断言对照 base_node.py / base_evaluator.py / merge_evaluator.py / keyer_evaluator.py / core/shaders.py / composite_renderer.py 核实。

---

## src/features/composite/nodes/difference_keyer_node.py (行数: 151)

### 发现
- [AUD-6700] [Suggestion] option 参数值未在节点侧校验，任意字符串静默透传渲染器
  - 位置: src/features/composite/nodes/difference_keyer_node.py:130-132
  - 证据:
    ```python
    channel_mode = self.parameters.get("channel_mode", "luma")
    invert = self._get_param_bool("invert", False)
    output_mode = self.parameters.get("output_mode", "result")
    ```
  - 影响: `channel_mode`/`output_mode` 未与 `CHANNEL_MODES`/`OUTPUT_MODES`（L24-27）做成员校验。损坏的工程文件注入任意字符串时会静默回落默认模式（keyer_evaluator.py:367-376 对未知值取默认索引），用户看到的与参数面板不符且无提示。影响低（渲染端有容错）。
  - 建议: 取值后做 `value if value in CHANNEL_MODES else "luma"` 式校验，或加载时（from_dict）清洗。
  - 置信度: 高（事实确凿，影响低）

### 通过项
- 线程安全：无共享可变状态，纯参数读取（每次 evaluate 用 `_get_param_clamped` 快照）
- 信号防递归：不涉及（节点层无 UI 信号）
- 状态提交一致性：参数全部走标准 parameters/PARAM_DEFS，无 revision 副作用
- 输入验证：threshold/softness/gain 均经 `_get_param_clamped`（0-1/0-0.5/0.1-10），缺输入/无效输入分支完备（L120-124）
- marker 协议：`_effect_input_a/_effect_input_b + _effect_params` 与 base_evaluator.py:2048-2054 消费端匹配；无输入时返回真实 TextureResult（无 marker 键）符合协议
- `evaluate()` 重写仅做 alias 解析（复制 dict，不改调用方输入）；disabled 旁路走父类 evaluate，BYPASS_MAPPING {"result": "input_a"} 语义正确
- compute_output_bbox/compute_input_roi：输出随 input_a、双输入同 ROI，正确

---

## src/features/composite/nodes/dissolve_node.py (行数: 154)

### 发现
- [AUD-6701] [Suggestion] 单输入有效分支未校验 input_b.is_valid，可把无效 TextureResult 当结果下发
  - 位置: src/features/composite/nodes/dissolve_node.py:105-108
  - 证据:
    ```python
    if input_a is None or not input_a.is_valid:
        return {"result": input_b} if input_b else {}
    if input_b is None or not input_b.is_valid:
        return {"result": input_a}
    ```
  - 影响: L106 对 `input_b` 只判真值不判 `is_valid`（texture_id<=0 仍非 None），与 L107 对称分支及全库"无效即空"约定不一致；下游需各自防御。渲染端 `_apply_dissolve` 会再次过滤，实际渲染不出错，属契约不一致。
  - 建议: 改为 `if input_b is not None and input_b.is_valid: return {"result": input_b}` else `{}`。
  - 置信度: 高（不一致确凿，影响低）
- [AUD-6702] [Suggestion] marker 占位尺寸用纹理原始宽高，与渲染端画布感知尺寸逻辑不一致
  - 位置: src/features/composite/nodes/dissolve_node.py:124-133（对照 base_evaluator.py:2489-2501）
  - 证据:
    ```python
    wa, ha = max(1, input_a.width), max(1, input_a.height)
    ...
    else:  # "larger"
        out_w, out_h = max(wa, wb), max(ha, hb)
    ```
    而渲染端 `_resolve_merge_dims` 经 `_canvas_dimensions`（format 优先于纹理 w/h）取尺寸。
  - 影响: bbox-packed 输入（纹理小于声明的画布 format）时，marker 占位 TextureResult 的 w/h 与实际输出不一致。占位值在 `_resolve_output` 前若被规划/预览路径读取会产生偏差；最终结果由 dispatcher 替换，不产生错误像素。
  - 建议: 占位尺寸复用与 `_canvas_dimensions` 相同的 format 优先级。
  - 置信度: 低（影响路径依赖占位值的具体消费方）

### 通过项
- mix 边界直通（0→B、1→A）只返回真实 TextureResult，符合协议注释（缓存可复用上游纹理）
- mix 经 `_get_param_clamped(0,1)` 验证
- bbox_policy 四种语义（union/intersection/a/b）与 MergeNode 一致；空 bbox（None/empty）回退另一输入处理正确（L75-78）
- `_input_a/_input_b` 与 `_effect_input_a/_effect_input_b` 双键均下发，与 merge_evaluator.py:37-38、base_evaluator.py:2001-2002 消费端匹配
- compute_input_roi 双输入同 ROI 正确；无 GPU 资源持有、无线程问题

---

## src/features/composite/nodes/dot_node.py (行数: 61)

### 发现
（无）

### 通过项
- 通过（无逻辑）：纯直通节点——`_do_evaluate` 仅判断输入有效性后原样返回（L55-61）；bbox/ROI 直通正确；无参数、无 GPU 资源、无状态副作用

---

## src/features/composite/nodes/edge_detect_node.py (行数: 156)

### 发现
（无）

### 通过项
- tile padding 正确性：`_tile_padding = kernel_size//2 + 2*preblur`（L96-100）≥ shader 实际采样半径 `kernel_size//2 + preblur`（core/shaders.py:4962-4983 tent 滤波偏移 pb 像素），方向保守、无缝隙风险
- legacy `requires_roi_expansion` 正确委托 `_tile_padding`；未覆写 `compute_input_roi` 时基类按同一 padding 扩张（base_node.py:1149-1157），两路径一致
- preview 降质（kernel>3→3、preblur 减半，L129-131）只影响 preview，export 保持全质量
- 参数读取全部带类型强转与默认值（float()/int()）
- marker 协议正确：`_effect_input + _effect_params`，placeholder 保留 alpha_premultiplied/color_space 元数据（L135-141）
- 输入验证：无效图像返回 {}（L112-114）
- 线程安全 / GPU 资源生命周期：不持有 GL 对象，无问题

---

## src/features/composite/nodes/erode_dilate_node.py (行数: 134)

### 发现
（无）

### 通过项
- tile padding 正确性：`(size//2 + 1) * iterations` ≥ 实际迭代总半径 `(size//2)*iterations`，保守正确（L91-93）
- int 参数下限保护 `max(1, ...)`（size/iterations）；preview 封顶（size>8 减半、iterations>3→3）只影响 preview
- marker 协议正确（`_effect_input + _effect_params`）
- 输入验证：无效输入返回 {}（L103-104）
- bbox/ROI：输出随输入，直通正确；无 GPU 资源、无线程问题

---

## src/features/composite/nodes/face_smooth_node.py (行数: 114)

### 发现
- [AUD-6703] [Suggestion] compute_input_roi 读 self.parameters 而非入参 params，与 _tile_padding 数据源不一致
  - 位置: src/features/composite/nodes/face_smooth_node.py:62-68（对照 70-74）
  - 证据:
    ```python
    def compute_input_roi(self, output_roi: "BBox", params: Dict[str, Any], ...):
        # Bilateral gathers within radius px.
        pad = int(self.parameters.get("radius", 4))
        expanded = output_roi.expanded(pad) if pad > 0 else output_roi
    ```
    而 `requires_roi_expansion(params)`/`_tile_padding(params)` 均读 `params.get("radius", 4)`。
  - 影响: radius（int 型，`is_animatable()` 含 int）被关键帧动画化后，调度器传入的解析值与 self.parameters 静态基础值不一致 → ROI 扩张不足 → 分块渲染时双边滤波在 tile 边缘采样缺像素，可能产生接缝。
  - 建议: 统一改为 `int(params.get("radius", 4))`。
  - 置信度: 中（需 radius 被动画化才触发，但两 API 数据源分裂是确凿的）
- [AUD-6704] [Suggestion] error_message 只置不清，mask 恢复后陈旧错误残留
  - 位置: src/features/composite/nodes/face_smooth_node.py:83-86
  - 证据:
    ```python
    mask = inputs.get("mask")
    if mask is None or not mask.is_valid:
        self.error_message = "Face Smooth: no skin mask input — " \
                             "connect build_skin_mask output"
        return {"result": image}
    ```
  - 影响: 全库无通用的"求值成功即清 error_message"路径（node_engine.py:1770 快照直读 node.error_message）；mask 重新连上后节点仍向 UI 报旧错。
  - 建议: `_do_evaluate` 成功路径开头 `self.error_message = ""`。
  - 置信度: 中

### 通过项
- strength clamp（0-1）与 radius/luminance_only 类型转换正确；strength<0.01 精确直通（无 marker 键）符合协议
- ROI 设计正确：image 按 radius 扩张、mask 不扩张（逐像素门控无需扩张）
- 复用已验证 denoise shader，mask 经 `_effect_mask` 挂载，由 base_evaluator.py:1980/2070-2071 通用注入
- placeholder 完整保留 format/bbox/alpha_premultiplied/color_space 元数据（L96-104）
- 输入验证：image/mask 缺失或无效均有分支；无 GPU 资源持有

---

## src/features/composite/nodes/face_track_node.py (行数: 130)

### 发现
- [AUD-6705] [Major] rerun_on_evaluate 在求值中途 set_parameter，自我置脏并触发状态回调（渲染线程）
  - 位置: src/features/composite/nodes/face_track_node.py:127-129、113-116、86-88（对照 base_node.py:643-651、912-919）
  - 证据:
    ```python
    # _do_evaluate 内（渲染线程）：
    if (self.parameters.get("rerun_on_evaluate") and _DETECTOR
            is not None and ctx is not None):
        self.detect_at(int(getattr(ctx, "frame_number", 0) or 0))
    # detect_at 末尾：
    tracks[int(frame)] = [(float(x), float(y)) for x, y in points]
    self.set_landmarks(tracks)
    # set_parameter（base_node）：
    self.parameters[key] = value
    self._dirty = True
    self.param_revision += 1
    self.bump_output_revision()
    if self._state != NodeState.PLACEHOLDER:
        self.state = NodeState.DIRTY   # 触发 _on_state_changed 回调
    ```
  - 影响: 求值中途修改 parameters 并 bump param_revision/输出 revision → 本次求值所产结果的缓存键即刻失效，预览下持续置脏可能造成反复重求值/缓存抖动；`_on_state_changed` 回调从渲染线程触发，若回调链触及 Qt UI 需自行保证跨线程安全；landmarks 为整段 JSON 字符串，逐帧重写放大 revision 抖动；且该参数变更绕过 undo 系统（无 undo command 记录）。`rerun_on_evaluate` 默认 False，属 opt-in 路径。
  - 建议: 检测结果改走求值期旁路容器（类似 `_emit_scene_payload` 的 scene_payloads 旁路），或求值结束后由 engine 主线程统一提交 set_parameter；至少为 landmarks 提供跳过 dirty/revision 副作用的内部写入通道。
  - 置信度: 中（副作用链路确凿；是否形成可见的重渲染循环取决于调度器对求值中置脏的处理，未逐行验证）
- [AUD-6706] [Suggestion] 进程级全局 _DETECTOR 为无锁可变共享状态
  - 位置: src/features/composite/nodes/face_track_node.py:26-31
  - 证据:
    ```python
    FaceDetector = Any
    _DETECTOR: Optional[FaceDetector] = None

    def set_face_detector(detector: Optional[FaceDetector]) -> None:
        global _DETECTOR
        _DETECTOR = detector
    ```
  - 影响: 任意线程可替换、渲染线程读取。CPython 引用赋值原子，实际竞态风险低，但作为跨线程可变全局缺少约定（替换时机与正在进行的 detect_at 之间无同步）。
  - 建议: 注释明确"仅初始化期设置"，或以不可变引用快照读取。
  - 置信度: 高（模式确凿，实际影响低）
- [AUD-6707] [Suggestion] 检测失败仅写日志与 detection_state 字符串，对 UI 不可见
  - 位置: src/features/composite/nodes/face_track_node.py:104-112
  - 证据:
    ```python
    except Exception as exc:
        logger.warning("face detector failed: %s", exc)
        self.detection_state = "FAILED"
        return False
    ```
  - 影响: `detection_state` 不进 NodeSnapshot（node_engine.py:1770 仅暴露 state/progress/error_message），检测失败对用户不可见（只有日志）；不符合"外部数据须验证/错误可见"精神（except 有日志非静默，尚可）。
  - 建议: 失败时同步写 `self.error_message`（会进快照供 UI 呈现）。
  - 置信度: 中

### 通过项
- passthrough 语义正确：仅返回真实 TextureResult，无 marker 键
- landmarks 持久化走 parameters → to_dict/from_dict 标准通道；serialize/deserialize_landmarks 在 face_workflow.py:98/111 存在且配对
- `params_require_frame_cache` 返回 True 正确（landmarks 按帧取值，缓存键须含帧号）
- detect_at 对注入检测器的返回结果做结构解包验证并 float() 强转坐标（L113-115）
- 无 GPU 资源持有；bbox/ROI 直通正确

---

## src/features/composite/nodes/file_output_node.py (行数: 482)

### 发现
- [AUD-6708] [Critical] _readback_texture 在函数内 import moderngl（L252）之前引用 moderngl.LINEAR（L227）→ 每次导出必然 UnboundLocalError，写盘功能全灭
  - 位置: src/features/composite/nodes/file_output_node.py:227（import 在 252；模块顶部无 moderngl 导入）
  - 证据:
    ```python
    readback_tex = ctx.texture((w, h), 4, dtype='f4')
    readback_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)   # L227
    ...
    import moderngl                                             # L252
    vao = ctx.vertex_array(prog, [(vbo, '2f 2f', 'in_vert', 'in_uv')])
    vao.render(moderngl.TRIANGLE_STRIP)
    ```
  - 影响: 函数内 `import moderngl` 使 `moderngl` 成为整个函数作用域的局部名，L227 先于 L252 执行 → UnboundLocalError → 被 L267 宽 except 捕获记 error 日志后 return None → `_do_evaluate` 置 "GPU readback failed"（L174-177）→ 任何格式都写不出文件。全文件仅此 3 处 moderngl 引用，无模块级导入。composite_export_renderer.py:142/179 注释确认 FileOutputNode 自带 readback/色彩转换是导出路径，故此 bug 直接使导出功能整体不可用，且日志只有一行易被误诊为 GPU 问题。
  - 建议: 将 `import moderngl` 移至模块顶部（与 cv2/numpy 并列）；补一条不 mock `_readback_texture` 的导出端到端回归测试。
  - 置信度: 高
- [AUD-6709] [Major] VBO/VAO 不在 finally 释放，异常路径泄漏 GL 缓冲区
  - 位置: src/features/composite/nodes/file_output_node.py:246-258、272-282
  - 证据:
    ```python
    vbo = ctx.buffer(np.array([...], dtype='f4'))
    ...
    vao = ctx.vertex_array(prog, [(vbo, '2f 2f', 'in_vert', 'in_uv')])
    vao.render(moderngl.TRIANGLE_STRIP)
    bind_texture_unit(0, 0)
    vao.release()
    vbo.release()
    ...
    finally:
        if readback_fbo is not None: ...readback_fbo.release()
        if readback_tex is not None: ...readback_tex.release()
    ```
  - 影响: L242-254 之间（uniform 赋值、render）抛任何异常时 vao/vbo 永不释放；finally 只覆盖 readback_fbo/readback_tex。当前因 AUD-6708 执行流到不了 L246，一旦修复即暴露此泄漏面；多次导出失败会累积泄漏 GL 对象。
  - 建议: vao/vbo 预置 None 一并纳入 try/finally 释放。
  - 置信度: 高
- [AUD-6710] [Major] 非 EXR 一律 clip(0,1)：TIFF+32bit 浮点导出丢失 HDR 高光（与自身注释矛盾）
  - 位置: src/features/composite/nodes/file_output_node.py:344-346、369-373
  - 证据:
    ```python
    # Only clamp for non-float formats; EXR must preserve HDR values
    if fmt != "EXR":
        rgba = np.clip(rgba, 0.0, 1.0)
    ...
    if fmt == "EXR":
        out = out.astype(np.float32)
    elif bit_depth == "32":
        out = out.astype(np.float32)
    ```
  - 影响: 注释写"仅对非浮点格式 clamp"，代码却对全部非 EXR clamp。UI 允许 TIFF+32bit（float32 TIFF 完全可存 >1.0），线性 HDR 值在写盘前被静默截断到 [0,1] —— 允许参数组合下的 HDR 数据丢失，违反"HDR 数据不得无理由 clamp"铁律（对 8/16bit 整数有理由，对 float32 无理由）。
  - 建议: clamp 条件改为 `if fmt != "EXR" and bit_depth != "32"`（或按目标 dtype 是否浮点判断）。
  - 置信度: 高
- [AUD-6711] [Major] 格式/位深/通道组合无前置验证：JPEG+RGBA、JPEG+16/32bit、PNG+32bit 等必然失败组合到 imwrite 才报错且原因被吞
  - 位置: src/features/composite/nodes/file_output_node.py:349-377、422-424、439-442
  - 证据:
    ```python
    elif channels == "RGBA":
        keep_alpha = True          # JPEG 也会走到 4 通道输出
    ...
    if fmt == "JPEG":
        params = [cv2.IMWRITE_JPEG_QUALITY, quality]
        return cv2.imwrite(path, image, params)
    ...
    except Exception as e:
        logger.error(f"Save error: {e}")
        return False               # 原因不进 last_write_message
    ```
  - 影响: PARAM_DEFS 的 labels 允许任意组合：JPEG 不支持 alpha 通道与 16/32 位深、PNG 不支持 float32 —— 这些组合一路走到 cv2.imwrite 才失败，用户只得到 "Failed to save: <path>"，无原因（cv2 异常细节被吞）；另外 `quality`/`compression` 对不适用格式也不提示。属明确功能缺陷（可预防的导出失败 + 诊断信息缺失）。
  - 建议: `_do_evaluate` 出口前按 format 校验合法 bit_depth/channels（非法组合自动降级并写入 last_write_message 说明）；`_save_to_disk` 捕获时把 `str(e)` 并入 last_write_message。
  - 置信度: 中（OpenCV 对个别组合的容错随版本变化，但 JPEG 无 alpha、PNG 无 float 为确定约束）
- [AUD-6712] [Suggestion] readback 改绑 FBO/viewport 后不恢复调用方 GL 状态
  - 位置: src/features/composite/nodes/file_output_node.py:228-231、262
  - 证据:
    ```python
    readback_fbo.use()
    ctx.viewport = (0, 0, w, h)
    ctx.clear(0.0, 0.0, 0.0, 0.0)
    ...
    raw = readback_fbo.read(components=4, dtype='f4')
    ```
  - 影响: 求值流中途切换绑定的 FBO 与 viewport 且不恢复。当前 evaluator 每个 dispatch 前都自设 FBO/viewport（如 keyer_evaluator.py:352-354），暂无实际故障；属对调度器行为的脆弱依赖（同一求值中 FileOutput 之后若有依赖既有状态的路径会被破坏）。
  - 建议: 保存并恢复 viewport 与先前绑定的 framebuffer。
  - 置信度: 低
- [AUD-6713] [Suggestion] `import traceback` 未使用（L23）
  - 位置: src/features/composite/nodes/file_output_node.py:23
  - 证据: `import traceback`（全文件仅 logger.debug(..., exc_info=True)，无 traceback 引用）
  - 影响: 死导入；顺带建议 moderngl 置顶（AUD-6708 修复）后统一导入布局。
  - 建议: 删除。
  - 置信度: 高

### 通过项
- preview 模式零拷贝直通 "display"，磁盘副作用严格限制在 export 模式（L151-152），符合终端节点协议（passthrough 无 marker 键）
- `_select_render_pass`：AOV 数据通道正确绕过显示 OETF（is_data_pass 判定 L316 + L336 跳过 export_encode）；`aov_texture_ids` 基类默认 dict，`.get` 安全（base_node.py:122）；pass 缺失时给出明确 message 并回退 beauty
- premult/straight 处理次序正确：unpremultiply（带 1e-4 防除零）→ 色彩转换（provider，经 CompositeEvalContext 尊重 composite.color_engine）→ 按目标 re-premultiply
- EXR 压缩映射经 T-P1-71 修正（避开 ZIP/ZIPS/PIZ 的 libdeflate SIGILL 风险），映射键完备（L406-409）
- `os.makedirs(out_dir, exist_ok=True)` 处理目录缺失；`_normalise_extension` 扩展名规范化符合文档化行为
- `cleanup()` 生命周期正确：composite_renderer.py:2046-2049 在关停时于渲染线程统一调用 `_render_nodes` 的 cleanup，`_passthrough_prog` 随渲染器存亡
- 线程安全：evaluate/readback 全程在渲染线程执行 GL 调用；`last_write_*` 为简单属性写（GIL 下原子）
- `_save_to_disk` 的 except 有日志且返回 False 走 last_write_message 状态通道，非静默吞错（但见 AUD-6711 的信息缺失）

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-6708 |
| Major | 4 | AUD-6705, AUD-6709, AUD-6710, AUD-6711 |
| Suggestion | 9 | AUD-6700, AUD-6701, AUD-6702, AUD-6703, AUD-6704, AUD-6706, AUD-6707, AUD-6712, AUD-6713 |

dot_node / edge_detect_node / erode_dilate_node 三文件无发现（纯直通或经核实的保守 padding/preview 降质实现）。
