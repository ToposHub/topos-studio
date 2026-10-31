# Batch 004 — color/core 会话与降噪静态审查

- 日期: 2026-09-28
- 范围: src/features/color/core/{color_session,color_stack_render_service,composition_uniforms,delivery_matrix,denoise_engine,export_denoise_bridge}.py（共 6 文件，2884 行）
- 编号区间: AUD-0400 – AUD-0499
- 方法: 逐文件全文 Read；跨文件事实（Layer.BLEND_MODES、PluginBinding.index 赋值、消费方 `_apply_topos_color`、双引擎实例、denoise 输入范围、`project.color_presets` 填充方）经只读 grep/sed 到真实代码行核实。
- 结论: Critical 0 / Major 3 / Suggestion 16。

---

## src/features/color/core/color_session.py (行数: 735)

### 发现

- [AUD-0401] [Suggestion] ColorSessionManager._session_history 只增不减，长期会话内存缓慢泄漏
  - 位置: src/features/color/core/color_session.py:513,532
  - 证据:
    ```python
    def __init__(self) -> None:
        self._current_session: ColorSession | None = None
        self._session_history: dict[str, ColorSession] = {}
    ...
    def set_current_session(self, session: ColorSession) -> None:
        self._current_session = session
        # 记录历史
        self._session_history[session.session_id] = session
    ```
  - 影响: 每次进入 Color 页都会创建新 uuid4 session（page_controller.py:2241 已核实调用链），历史 dict 无淘汰机制，`clear_current_session()` 也不清理对应条目；数小时编辑会话可累积数千条。
  - 建议: 加容量上限（如 OrderedDict + move_to_end）或 TTL 淘汰；`get_session_by_id` 若无实际调用方可直接删除历史功能。
  - 置信度: 高

- [AUD-0402] [Suggestion] from_dict/load_from_path 对损坏工程数据抛裸 ValueError，未转领域错误
  - 位置: src/features/color/core/color_session.py:424-438
  - 证据:
    ```python
    return ColorSession(
        session_id=data.get("session_id", str(uuid.uuid4())),
        source_kind=SourceKind(data.get("source_kind", "single_asset")),
        project_id=data.get("project_id", ""),
        ...
        save_policy=SavePolicy(data.get("save_policy", "topos_project")),
    )
    ```
  - 影响: 工程/会话文件缺 `project_id`、`color_stack_id` 或含非法枚举值时，`__post_init__`/`SourceKind()` 直接抛 ValueError（如 "project_id cannot be empty"），加载路径把内部校验异常当解析错误上抛；`session_id` 键存在但值为 null 时同样崩溃（`data.get` 默认值不生效）。fail-fast 可接受，但缺少统一的反序列化错误包装与文件级上下文。
  - 建议: `from_dict` 内显式校验并抛带文件上下文的领域异常（如 SessionDataError），或对 null 值做 `or` 归一。
  - 置信度: 中

- [AUD-0403] [Suggestion] get_save_target_region 三个分支返回完全相同字符串，与文档表格承诺不符
  - 位置: src/features/color/core/color_session.py:371-378
  - 证据:
    ```python
    if self.source_kind == SourceKind.SINGLE_ASSET:
        return f"color_stacks[{self.color_stack_id}]"
    elif self.source_kind == SourceKind.PROJECT_ASSET:
        return f"color_stacks[{self.color_stack_id}]"
    elif self.source_kind == SourceKind.TIMELINE_CLIP:
        return f"color_stacks[{self.color_stack_id}]"
    ```
  - 影响: 行为正确（session 内已解析出最终 stack id），但与模块头部文档表格（primary_color_stack_id / asset_default_stack_id / clip.color_stack_id 三种区域）形成误导，读者会以为分支有语义差异；无 else 前置分支也使未来新增 SourceKind 时静默返回 "unknown"。
  - 建议: 收敛为单行 return，并同步修正文档表格或注明"ID 已在会话创建时解析"。
  - 置信度: 高

- [AUD-0404] [Suggestion] from_page_context 入参 context: Any 弱类型，帧字段 getattr 与 clip/asset 字段直接属性访问混用
  - 位置: src/features/color/core/color_session.py:276-307
  - 证据:
    ```python
    @staticmethod
    def from_page_context(
        context: Any,
        ...
    clip_id = context.clip_id
    asset_id = context.asset_id
    timeline_frame = getattr(context, 'timeline_frame', None)
    ```
  - 影响: PageContext 未以 Protocol/类型注解约束，`timeline_frame` 用 getattr 兜底而 `clip_id` 直接访问，暗示两套 API 版本并存；PageContext 缺 `clip_id` 属性时直接 AttributeError 而非受控回退。
  - 建议: 定义 PageContextLike Protocol（含可选帧字段），统一访问方式。
  - 置信度: 高（注解事实）/ 中（AttributeError 可达性）

### 通过项
- 线程安全: frozen dataclass 全不可变字段，manager 仅做原子引用赋值，无共享可变状态。
- GPU 资源生命周期: 本文件无 GPU 资源。
- GLSL/HDR: 无内嵌 GLSL。
- 信号防递归: 无信号定义。
- 状态提交一致性: 不触碰 model/undo 数据，仅做保存路由；dirty 恒 True 有文档说明（会话瞬态）。
- 工厂方法验证链（视频必须走 TIMELINE_CLIP、PROJECT_ASSET 拒绝视频）与 `__post_init__` 约束一致。

---

## src/features/color/core/color_stack_render_service.py (行数: 150)

### 发现

- [AUD-0405] [Major] embedded 模式丢弃 lut_path/lut_intensity，嵌入栈的 3D LUT 在 Composite 渲染中静默丢失
  - 位置: src/features/color/core/color_stack_render_service.py:125-131（对照 :94-95）
  - 证据:
    ```python
    return {
        "grading_params": dict(embedded_stack.get("grading_params", {})),
        "curves_data": dict(embedded_stack.get("curves_data", {})),
        "hsl_curve_data": dict(embedded_stack.get("hsl_curve_data", {})),
        "mixer_params": dict(embedded_stack.get("mixer_params", {})),
        "soft_clip_params": dict(embedded_stack.get("soft_clip_params", {})),
    }
    ```
    对照 clip_stack 分支显式透传: `"lut_path": stack.lut_path ...`、`"lut_intensity": stack.lut_intensity ...`（:94-95）；且 ToposColorNode 注明 embedded_stack 为 "full stack snapshot"（src/features/composite/nodes/topos_color_node.py:55）。
  - 影响: 用户把带 3D LUT 的调色栈嵌入 Composite 节点后，时间线/合成渲染不含 LUT，与 Color 页预览不一致——渲染结果错误且无任何提示。消费方 `_apply_topos_color`（color_evaluator.py:415 起）只读返回 dict 的键，无法弥补缺键。
  - 建议: `_resolve_embedded` 与 `_resolve_clip_stack` 的键集合对齐（补 `lut_path`、`lut_intensity`）。
  - 置信度: 中（嵌入快照是否携带 lut 键取决于上游序列化；delivery_matrix.py:221-222 表明 layer 快照含 `active_lut_path`/`lut_path`，快照可携带 LUT）

- [AUD-0406] [Suggestion] preset 模式数据源 project.color_presets 全库无填充方，且只提取 grading_params
  - 位置: src/features/color/core/color_stack_render_service.py:113-115
  - 证据:
    ```python
    return {
        "grading_params": dict(preset.get("stack", {}).get("grading_params", {})),
    }
    ```
  - 影响: grep 全 src/ 无任何代码写入 `project.color_presets`——preset 模式当前永远落到"ColorStack not found"中性默认（静默无调色）；若未来填充，presets 实际含 curves/warper/plugins（presets_panel.py:595 证实数据形态），此实现也会只应用 primary 分量。
  - 建议: 要么补全 preset 提取键集合，要么在 preset 源未实现前让 resolve 显式报错/警告，避免静默 no-op。
  - 置信度: 中

- [AUD-0407] [Suggestion] 三种 stack_source 返回 dict 形状不一致（8 键 / 1 键 / 5 键），契约脆弱
  - 位置: src/features/color/core/color_stack_render_service.py:88-96,113-115,125-131
  - 证据: clip_stack 返回 grading/curves/hsl/mixer/soft_clip/lut_path/lut_intensity 共 8 键；preset 仅 grading_params 1 键；embedded 5 键（见上）。
  - 影响: 当前消费方全用 `.get(key, default)` 尚能容忍，但任何新消费方按 clip_stack 形状取键（如 `resolved["lut_intensity"]`）在 preset/embedded 模式下 KeyError；三模式能力差异（AUD-0405/0406）也因此被掩盖。
  - 建议: 统一由一个 `_stack_payload(...)` 构造完整形状，模式间只差数据来源。
  - 置信度: 高

### 通过项
- 线程安全/GPU 生命周期/信号防递归: 纯解析服务，无 GPU、无线程、无信号（类文档明确 "does NOT do GPU rendering directly"）。
- 输入验证: stack 缺失回退中性默认 + debug 日志；未知 stack_source 有 warning。
- 浅拷贝（dict(...)）隔离调用方与模型数据；grading_params 值为标量/元组，浅拷即足够。
- 类型注解完整。

---

## src/features/color/core/composition_uniforms.py (行数: 495)

### 发现

- [AUD-0408] [Suggestion] upload_composition_uniforms 逐 uniform 宽泛 except Exception 且只记 debug 日志
  - 位置: src/features/color/core/composition_uniforms.py:488-495
  - 证据:
    ```python
    for name, value in values.items():
        if name not in prog:
            continue
        try:
            prog[name].value = value
        except Exception:
            logger.debug(f"Failed to set composition uniform '{name}'",
                         exc_info=True)
    ```
  - 影响: "uniform 被编译器剔除属预期" 的跳过由 `name not in prog` 承担，本 except 捕的只剩真实错误（类型不匹配、tuple 长度错、上传侧 bug），却以 debug 级静默——生产日志完全不可见，参数错配会表现为难以定位的画面异常。
  - 建议: 至少升级为 warning 级（可加一次性去重），或按异常类型区分（KeyError/AttributeError 预期路径 vs TypeError 真错误）。
  - 置信度: 高

- [AUD-0409] [Suggestion] u_opacity/u_lut_intensity 用直接 float() 转换，显式 None 时抛 TypeError，与全文件 `or` 惯用法不一致
  - 位置: src/features/color/core/composition_uniforms.py:362,398（对照 :376 `float(p.get("exposure") or 0.0)`）
  - 证据:
    ```python
    values["u_opacity"] = float(_layer_get(layer, "opacity", 1.0))
    ...
    values["u_lut_intensity"] = float(_layer_get(layer, "lut_intensity", 1.0))
    ```
  - 影响: 序列化数据中这两键为 null 时（`dict.get` 默认值不生效），float(None) 抛 TypeError 中断整帧 uniform 提取；模块文档明确 grading 标量用 `or 0.0` 容忍 None 的裁决，这两处未遵循同一契约。
  - 建议: 统一为 `_layer_get(layer, "opacity", 1.0) or 1.0` 形态或提取 helper。
  - 置信度: 中

### 通过项
- 动态注入命名隔离（本文件重点）: 插件 uniform 前缀 `u_P{index}_` 已核实唯一——`binding.index` 由 `plugin_bindings_from_instances` 按原始 enumerate 位置赋值（shader_compiler.py:105 起），前缀与上传侧（composition_uniform_values:460）一致，无碰撞、无与固定 uniform 撞名。
- `_MSL_SCALAR` 覆盖 `GLSL_UNIFORM_TYPES` 可产出的全部类型（float/int/vec2/vec3，effect_spec.py:228-236），`_layout` 无 KeyError 路径。
- snippet-owned（u_tex_size/u_visible_rect）只上传不重复发射，GL 声明不重复。
- u_visible_rect 像素单位回退 `(0, 0, width, height)` 与 GLSL 契约一致（shader_nodes.py:739 注明绝对物理像素坐标）。
- params_to_rgb 无 HDR clamp，亮度平方映射对负值处理正确（bright<0 取负 stops），除零有 1e-6 下限。
- fx/blur 组总是写中性默认，修复了共享缓存 program 的跨 layer 残留（模块文档台账 0004a）。
- u_blend_mode 未知值回退 Normal 有显式注释与 except 收窄（ValueError/AttributeError/TypeError）。
- MSL struct / numpy dtype / offset 三方对齐规则（4 字节紧排、bool→uint）内部一致。

---

## src/features/color/core/delivery_matrix.py (行数: 665)

### 发现

- [AUD-0410] [Suggestion] _changed 对 None/不可转值按"已修改"处理，交付报告可能误报操作启用
  - 位置: src/features/color/core/delivery_matrix.py:171-180
  - 证据:
    ```python
    def _changed(value: Any, default: Any) -> bool:
        try:
            if isinstance(default, tuple):
                values = tuple(float(item) for item in value)
                ...
            return abs(float(value) - float(default)) > 1e-6
        except (TypeError, ValueError):
            return value != default
    ```
  - 影响: `grading_params` 中显式 null（损坏/手改工程文件）时 `gp.get(name, default)` 返回 None → 转换抛 TypeError → 回退 `None != default` = True → 报告列出实际中性的 primary/texture 为"启用"，在 video/LUT 目标下显示为"丢失"，制造虚假的有损确认门槛（方向保守、不丢数据，仅报告噪音）。
  - 建议: None 视为默认值（`if value is None: return False`）。
  - 置信度: 高（逻辑确定）/ 低（null 出现频率）

### 通过项
- 输入验证（重点维度）: 全文件大量 isinstance/getattr/默认值防御，legacy tracking 两种序列化格式（bindings dict / 旧 map）均有兼容分支；空快照不可证伪时不误杀绑定（注释说明权威性边界）。
- ndarray 真值陷阱已有针对性规避（ai_matte 用 `is not None`，注释明示避免 truth-testing ndarrays）；warper_params 以 None 为默认值（layer.py:394 已核实），`is not None` 判定无误报。
- 线程安全/GPU 生命周期/信号防递归: 纯只读报告生成，无 GPU、无线程、无信号。
- 状态提交一致性: 只读消费 model 数据，不回写。
- 无操作集合越界: `_layer_operations` 产出的 op 全部在 `_OPERATIONS` 注册（tracking/video_denoise 由 report_from_color_stacks 单独补行），无 KeyError 路径。
- i18n 兜底（_tr 失败回退中文 fallback）与 preflight code 稳定命名完整。

---

## src/features/color/core/denoise_engine.py (行数: 662)

### 发现

- [AUD-0411] [Major] 类级共享 _session_cache/_current_model_path：denoise 与 deblur 双引擎交替使用时互清缓存，模型每次从磁盘重载，且跨 QThread 无锁
  - 位置: src/features/color/core/denoise_engine.py:84-85,165-168
  - 证据:
    ```python
    # Shared model cache to avoid reloading from disk
    _session_cache = {}
    _current_model_path = None
    ...
    # Release old session memory when switching models
    if DenoiseWorker._current_model_path != model_path:
        if DenoiseWorker._current_model_path is not None:
            DenoiseWorker._session_cache.clear()
        DenoiseWorker._current_model_path = model_path
    ```
  - 影响: 已核实两个引擎实例并存——denoise_panel.py:39 `DenoiseEngine()` 与 deblur_panel.py:29 `RestorationEngine(model_subdir="Deblur")`（RestorationEngine 即本类别名，:662）。单条 `_current_model_path` 记录"最近模型"：用户在降噪/去模糊两个面板间交替操作时，每次切换都 clear 对方缓存并整模型重载（SwinIR 类模型加载可达数秒，GPU 编译路径甚至设有 30s 超时，:270）；两 worker 并发运行时（各引擎只拒绝自身并发，:595-597，不拦截跨引擎）对类属性/dict 的读写无任何锁。
  - 建议: 缓存键即 model_path、按 LRU 容量淘汰（如最多 2 个 session），删除 `_current_model_path` 互斥清除逻辑；并发场景加 threading.Lock 保护缓存读写。
  - 置信度: 高

- [AUD-0412] [Major] AI 降噪路径对 HDR 输入强制 clip [0,1]，>1.0 高光在结果中永久压平（Basic 路径明确支持 HDR，行为不一致）
  - 位置: src/features/color/core/denoise_engine.py:180-181,205-213
  - 证据:
    ```python
    self.image = np.nan_to_num(self.image, nan=0.0, posinf=1.0, neginf=0.0)
    self.image = np.clip(self.image, 0, 1)
    ...
    output = np.nan_to_num(output, nan=0.0, posinf=1.0, neginf=0.0)
    output = np.clip(output, 0, 1)
    ...
    if blend < 1.0:
        output = cv2.addWeighted(output, blend, self.image, 1.0 - blend, 0)
    output = np.clip(output, 0, 1)
    ```
  - 影响: 调用方只对 uint8/uint16 归一化（color_controller.py:1494-1500），float32 源原样透传；Basic 路径注释明确"支持负数和 >1.0 的 HDR"（:116-117），说明 HDR 输入是受支持场景。AI 路径不仅模型输入被裁剪（模型需要 [0,1] 可理解），blend（:211）用的也是裁剪后的 self.image 副本、输出再裁剪（:213）——即使 blend=0.5，>1.0 的原值也无法回到结果，HDR 高光信息不可逆丢失，且这是静默发生。
  - 建议: 模型输入归一化改用 max-scaling（按帧最大值缩放，输出反缩放）或仅对送入模型的副本裁剪、blend/输出保留未裁剪原值。
  - 置信度: 中（取决于 img_full_original 是否实际出现 >1.0 的 float32；on_denoise_finished 处 "Preserve it to keep HDR/RAW precision" 注释与 Basic 路径 HDR 声明支持该假设）

- [AUD-0413] [Suggestion] cancel() 兜底 worker.terminate() 有进程级未定义行为风险，且 Basic 路径完全无协作取消检查点
  - 位置: src/features/color/core/denoise_engine.py:645-658（对照 _run_classic_split_denoise:106-146 无任何 isInterruptionRequested）
  - 证据:
    ```python
    self.worker.requestInterruption()
    if self.worker.wait(2000):
        return True

    logger.warning("Denoise worker did not respond to interruption, forcing terminate")
    self.worker.terminate()
    ```
  - 影响: Basic 双边滤波路径无取消检查点，大图长耗时下协作取消必然超时落入 terminate()——在 ONNX/原生内存操作中被强杀属未定义行为（Qt 文档明确不推荐），可能崩溃或留下损坏的共享 session；缓解因素：grep 未发现生产代码调用 `DenoiseEngine.cancel()`（当前为不可达路径）。
  - 建议: Basic 路径按通道/分块插入 isInterruptionRequested 检查点；terminate 分支移除或改为标记放弃并等待线程自然退出。
  - 置信度: 高（代码事实）/ 影响当前不可达

- [AUD-0414] [Suggestion] apply_denoise 原地改写调用方 params dict（model_path 相对路径→绝对路径）
  - 位置: src/features/color/core/denoise_engine.py:599-601
  - 证据:
    ```python
    if mode == "AI" and "model_path" in params:
        if not os.path.isabs(params["model_path"]):
            params["model_path"] = os.path.join(self.model_dir, os.path.basename(params["model_path"]))
    ```
  - 影响: 输入 dict 被副作用修改。当前调用方每次新建 dict（denoise_panel.py:442-451）无实害，但任何复用 params（预设、撤销栈、批量队列）的 future 调用方会把机器相关绝对路径写回共享数据。
  - 建议: `params = dict(params)` 后再改，或仅在传给 worker 前解析。
  - 置信度: 高

- [AUD-0415] [Suggestion] _load_with_timeout 超时后悬挂的加载线程永不回收
  - 位置: src/features/color/core/denoise_engine.py:359-367
  - 证据:
    ```python
    loader_thread = __import__('threading').Thread(target=_load, daemon=True)
    loader_thread.start()
    loader_thread.join(timeout=timeout)

    if loader_thread.is_alive():
        # Thread is still running = compilation hung
        return None
    ```
  - 影响: GPU 编译 hang 时（正是该函数设计的场景），每个 provider 尝试泄漏一个常驻 daemon 线程并持有半初始化的 ONNX session；多次重试后累积。daemon 属性避免了退出阻塞，但长会话内是纯浪费，且若其最终完成，`result[0]` 的 session 被静默丢弃。
  - 建议: 记录泄漏线程并在后续调用检查回收；或改用带取消能力的子进程加载。
  - 置信度: 高

- [AUD-0416] [Suggestion] 用户取消经 error 信号通道上报，UI 会弹"严重错误"对话框
  - 位置: src/features/color/core/denoise_engine.py:450-451（对照 color_controller.py:1541-1546）
  - 证据:
    ```python
    if self.isInterruptionRequested():
        logger.info("Denoise worker interrupted at tile row %d", y)
        raise RuntimeError("Cancelled by user")
    ```
    error 信号直连 `on_denoise_error` → `QMessageBox.critical(self, "Denoise Error", err_msg)`（color_controller.py:1544-1546，已核实）。
  - 影响: 正常的用户取消将以 "Denoise Error: Cancelled by user" 严重错误弹窗呈现；取消语义应走独立 cancelled 信号或被 error 处理方按消息内容过滤。当前 cancel 无生产调用方，问题随 AUD-0413 的 UI 接入而显性化。
  - 建议: DenoiseWorker 增加 cancelled 信号，或 run() 中拦截 "Cancelled by user" 单独 emit。
  - 置信度: 高

- [AUD-0417] [Suggestion] 多处公开方法与 Worker 方法缺类型注解
  - 位置: src/features/color/core/denoise_engine.py:87-92,581,610
  - 证据:
    ```python
    def __init__(self, image, mode, params):   # DenoiseWorker
    ...
    def apply_denoise(self, image, mode, params):
    ...
    def get_available_models(self):
    ```
  - 影响: 与代码库强类型约定不符（.clinerules）；入参形态（float32 BGR [0,1]、params 键集）只能靠 docstring 传达。
  - 建议: 补 `image: np.ndarray, mode: str, params: Dict[str, Any] -> bool` 等注解。
  - 置信度: 高

### 通过项
- 协作取消检查点已进入 tile 双层循环（:448-462），Basic 模式除外（见 AUD-0413）。
- GPU 资源生命周期: 本文件为 CPU ONNX 路径，不持有 ModernGL 资源，无 deferred-release 义务。
- 跨线程信号只传 ndarray 结果（fresh 数组，emit 后 worker 不再持有/修改），无共享可变对象经信号传递（缓存问题见 AUD-0411）。
- Basic 路径 HDR 语义正确（纯数学 YUV 提取/重建，无损往返）。
- tile 混合用互补余弦渐变（fade_in + fade_out = 1.0）修复了旧 Hanning 窗重叠区双零权重黑线问题，注释有据。
- ONNX provider 降级链（GPU 超时→下一 provider→纯 CPU）失败路径均收窄为日志 + RuntimeError 透传，无静默吞错。
- apply_denoise 拒绝并发任务（busy 信号）防止同引擎 worker 叠加。

---

## src/features/color/core/export_denoise_bridge.py (行数: 177)

### 发现

- [AUD-0418] [Suggestion] process_frame 每帧创建并释放 FBO，长序列 4K 导出存在分配churn
  - 位置: src/features/color/core/export_denoise_bridge.py:136-140
  - 证据:
    ```python
    fbo = self._ctx.framebuffer(out_tex)
    try:
        data = fbo.read(components=3, dtype="f4")
    finally:
        fbo.release()
    ```
  - 影响: 每帧一次 FBO 对象创建/销毁（4K f4 readback 本身才是大头，但对象churn叠加其上）；out_tex 在无时域累积时通常稳定，按输出纹理缓存 FBO 可省去逐帧分配。属小性能项。
  - 建议: 按 out_tex 身份缓存 framebuffer（尺寸/纹理变化时重建），release() 时统一释放。
  - 置信度: 高

- [AUD-0419] [Suggestion] VideoDenoiseParams.from_dict 解析失败完全静默直通，导出成片无降噪且日志无痕
  - 位置: src/features/color/core/export_denoise_bridge.py:88-93
  - 证据:
    ```python
    try:
        from src.shared.video.denoise.params import VideoDenoiseParams
        params = VideoDenoiseParams.from_dict(params_dict)
    except Exception:
        return frame_bgr
    ```
  - 影响: 参数 dict 损坏/版本不匹配时每帧静默直通——用户以为导出含降噪，成片实际没有，事后无法从日志定位；与 `_ensure_ready` 失败时的一次性 warning 策略（:69-72）不一致。宽泛 except 吞掉 ImportError（依赖缺失）等非数据错误。
  - 建议: 首次失败记 warning（沿用 _warned_process_error 一次性模式），并区分 ImportError 与数据错误。
  - 置信度: 高

### 通过项
- GPU 资源生命周期（重点维度）: 每导出 worker 独立实例 + standalone context（类文档明确非线程安全、不跨线程共享）；输入纹理尺寸变化时先 release 旧纹理再建新（:119-123），同线程直 release 不适用 deferred-release（无跨线程共享 context）；release() 幂等且涵盖 pipeline/tex/ctx 三层；`_failed` 置位后不再重试，避免逐帧重复撞墙。
- 异常与取消路径: process_frame 整体兜底保证"导出永不因降噪中断"（一次性 exception 日志后直通），符合模块契约。
- 色彩/量化边界: BGR↔RGB 双向反转对称、16-bit 源按 iinfo.max 归一化、f2 上传精度覆盖 10/12-bit、rint 后回写保持原 dtype、alpha 通道经 copy 保留。
- 线程安全边界: 文档与实现一致（惰性初始化在导出线程内完成）。
- 输出 clip(0,1) 发生在整数目标帧量化域，非 HDR 无理由裁剪（输入域 BGR 整数帧，无 HDR 范围）。

---

## 汇总

| 严重度 | 数量 | 编号 |
|---|---|---|
| Critical | 0 | — |
| Major | 3 | AUD-0405, AUD-0411, AUD-0412 |
| Suggestion | 16 | AUD-0401–0404, AUD-0406–0410, AUD-0413–0419 |

Major 摘要:
1. AUD-0405 color_stack_render_service: embedded 模式丢 lut_path/lut_intensity → 合成渲染静默丢 3D LUT。
2. AUD-0411 denoise_engine: 类级模型缓存被 denoise/deblur 双引擎互清，交替使用即整模型重载 + 跨线程无锁。
3. AUD-0412 denoise_engine: AI 降噪对 HDR 输入强制 clip [0,1]（含 blend 路径），高光不可逆压平，与 Basic 路径 HDR 支持不一致。
