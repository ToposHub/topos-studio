# batch-063 — src/features/composite/nodes/（AI 系节点与共享 GPU 回读助手）

编号区间：AUD-6300–AUD-6399。审查日期：2026-09-28。
上下文事实（已在源码中核实）：渲染评估运行于 `CompositeRenderer(GraphEvaluatorMixin, QThread)`（src/features/composite/render/composite_renderer.py:87）；`ai_gen_expand` 能力为 `action_type="source"`（node_capabilities.py:879-886），走持久节点 `node.evaluate → _do_evaluate` 同步执行；`ai_generate/ai_inpaint/ai_expand/ai_denoise` 为 `is_placeholder=True`，在计划层 `action=="placeholder"` 直通（base_evaluator.py:710-734）；`ai_remove`（is_placeholder=False）由 `_evaluate_ai_node` 异步分支接管（base_evaluator.py:1372，`_AI_NODE_TYPES` 见 :246）。

---

## src/features/composite/nodes/__init__.py (行数: 97)

### 发现
- [AUD-6300] [Suggestion] 包内 11 个节点模块未被 `__init__` 导出，聚合清单与目录漂移
  - 位置: src/features/composite/nodes/__init__.py:7-97
  - 证据:
    ```python
    from .ai_denoise_node import *
    from .ai_expand_node import *
    ...
    from .unknown_placeholder_node import *
    from .unpremult_node import *
    from .video_frame_node import *
    ```
  - 影响: 目录中实际存在但本文件未导入：`camera3d_node`、`deformer3d_node`、`geo3d_node`、`lens_distortion_node`、`light3d_node`、`model3d_node`、`precomp_node`、`scene3d_node`、`timeline_clip_input_node`、`topos_color_node`、`tracker_node`（与 `ls src/features/composite/nodes/` 逐一核对）。`from src.features.composite.nodes import Scene3DNode` 之类聚合导入会失败，而 node_engine.py 按模块直连注册又能工作，两套清单不一致，新增节点容易漏配。
  - 建议: 用 pkgutil 自动枚举或补齐缺失模块，保持 `__init__` 与目录一一对应；或注释说明哪些模块刻意不聚合。
  - 置信度: 高（事实确凿；是否算缺陷取决于聚合契约，故仅 Suggestion）

- [AUD-6301] [Suggestion] 约 85 个 `import *` 通配导入，同名符号会被静默覆盖
  - 位置: src/features/composite/nodes/__init__.py:15-97
  - 证据:
    ```python
    from .ai_denoise_node import *
    from .ai_expand_node import *
    from .ai_generate_node import *
    from .ai_inpaint_node import *
    from .ai_remove_node import *
    from .blend_node import *
    ...
    from .node_state import *
    ```
  - 影响: 任何一个模块在 `__all__` 之外定义的顶层辅助函数/常量都会被后导入的同名符号无提示覆盖；问题定位困难，静态检查（flake8 F403/F405）也无法追踪。
  - 建议: 各节点模块显式声明 `__all__`；或聚合层改为显式名称导入。
  - 置信度: 低（今日是否已发生实际遮蔽未逐一验证；风险机制为高置信）

### 通过项
- 纯 re-export，无逻辑：导出全部节点类型 + base_node 的 Node/Port/TextureResult/ParamDef/Connection + node_state；私有模块 `_gpu_readback` 未被导出，正确
- 无 GPU/线程/信号/持久化逻辑可审

---

## src/features/composite/nodes/_gpu_readback.py (行数: 71)

### 发现
- [AUD-6302] [Suggestion] `fb_manager is None` 回退路径每次回读新建 FBO 且从不释放
  - 位置: src/features/composite/nodes/_gpu_readback.py:60-61
  - 证据:
    ```python
    fbo = (fb_manager.get_fbo(tex)
           if fb_manager is not None else gl.framebuffer(tex))
    fbo.use()
    ```
  - 影响: 生产路径 `fb_manager` 恒非空（评估器传 `self.fb_manager`），走 `get_fbo` 缓存无泄漏；但无管理器调用方（测试/未来节点）每帧泄漏一个 ModernGL framebuffer 对象，绕过 deferred-release 体系。
  - 建议: 回退路径用局部 `try/finally fbo.release()`，或直接要求 `fb_manager` 必填。
  - 置信度: 高

- [AUD-6303] [Suggestion] `fbo.use()` + 改写 `gl.viewport` 后不恢复，GL 全局状态外泄
  - 位置: src/features/composite/nodes/_gpu_readback.py:62-63
  - 证据:
    ```python
    fbo.use()
    gl.viewport = (0, 0, int(tex_result.width), int(tex_result.height))
    raw = fbo.read(components=4, dtype="f4")
    ```
  - 影响: 调用发生在其它节点评估中途时，帧缓冲绑定与视口被改写；当前调用方（ai_gen_expand 在 `_do_evaluate` 开头调用）之后的各 GPU pass 均自行 `fbo.use()+viewport`（如 frame_interpolate_node.py:168-169），暂无实际错绘，但契约脆弱，依赖"后续 pass 必定重设状态"的隐式假设。
  - 建议: 记录并恢复原绑定/视口，或在 docstring 明确"调用后 GL 状态被污染"的契约。
  - 置信度: 中

### 通过项
- 线程安全：仅在渲染线程（评估器）内调用，无共享可变状态
- GPU 资源生命周期：`get_fbo` 缓存路径符合 fb_manager 设计（见 AUD-6302 的回退路径例外）
- HDR 正确性：`dtype="f4"` 全精度回读、`np.flipud` 翻转正确，无 clamp
- 异常路径：宽泛 `except Exception`（:69-71）有 docstring 声明的 best-effort 契约且 `logger.warning` 记录，属合理防御而非掩盖领域错误（建议后续在日志中带上 texture_id/尺寸便于定位）
- 容器形状兼容：set / list / dict-of-lists 三种 holder 均处理（:38-48），与 `_get_texture_by_id`（base_evaluator.py:3017-3041）扫描的容器集合一致
- 类型注解完整

---

## src/features/composite/nodes/ai_denoise_node.py (行数: 24)

### 发现
- [AUD-6304] [Suggestion] `__init__` 缺少类型注解，与同族已注解节点不一致
  - 位置: src/features/composite/nodes/ai_denoise_node.py:19
  - 证据:
    ```python
    def __init__(self, node_id=None):
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
    ```
  - 影响: 与 `AIGenExpandNode.__init__(self, node_id: Optional[str] = None) -> None` 风格不一致，弱化类型检查覆盖。
  - 建议: 补 `node_id: Optional[str] = None) -> None`。
  - 置信度: 高

### 通过项
- 占位节点无逻辑：PARAM_DEFS 合法（`ParamDef` 支持 min_val/max_val/labels/step，base_node.py:208-259），端口定义 + 二次 `_init_port_revisions()`（子类在 super().__init__ 后才设 outputs，需重调）正确
- 计划层 `action=="placeholder"` 直通，`_do_evaluate` 不会真正执行
- 无 GPU/线程/信号/持久化代码

---

## src/features/composite/nodes/ai_expand_node.py (行数: 26)

### 发现
- [AUD-6305] [Suggestion] `__init__` 缺少类型注解
  - 位置: src/features/composite/nodes/ai_expand_node.py:21
  - 证据:
    ```python
    def __init__(self, node_id=None):
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
    ```
  - 影响: 同 AUD-6304。
  - 建议: 补 `Optional[str]` 注解。
  - 置信度: 高

### 通过项
- `ParamDef("direction", "option", "all", labels=[...])` 用法合法且默认值 "all" 在 labels 内
- `ParamDef("amount", "int", 256, min_val=1, max_val=4096, step=1)` 边界合理
- 占位直通与端口/revision 初始化正确，无 GPU/线程/持久化代码

---

## src/features/composite/nodes/ai_gen_expand_node.py (行数: 248)

### 发现
- [AUD-6306] [Major] 输入像素从不进入生成请求，节点却用 provider 全帧输出整体替换输入帧
  - 位置: src/features/composite/nodes/ai_gen_expand_node.py:231-248（并见 src/shared/ai_models/generation/image_gen_job.py:202-209）
  - 证据:
    ```python
    # The provider output replaces the frame (it already composited
    # the request context per the mask semantics above).  Upload
    # defensively — fall back to passthrough.
    ...
    tex = gl.texture((w, h), 4, data=data.tobytes(), dtype="f4")
    return {"image": TextureResult(
        texture_id=tex.glo, width=w, height=h, ...
    ```
    而 CloudImageGenJob.run 发出的请求体（image_gen_job.py:202-209）为：
    ```python
    request = {
        "prompt": self.prompt, "negative_prompt": self.negative,
        "model": self.model, "seed": self.seed,
        "strength": self.strength, "mask": self.mask,
    }
    ```
    ——不含 `image`。`input_pixels` 只参与 cache key（image_gen_job.py:135-142）。
  - 影响: 注释声称 provider "已经合成请求上下文"不成立：transport 拿不到输入帧，无从合成。一旦接线使用，遮罩外的原始像素会被生成内容整体覆盖——文档语义（"masked fill / outpainting"，`_build_job` docstring "both mark pixels to generate"）下这就是渲染结果错误；当前因无生产 transport（见 AUD-6311）路径不可达，故记 Major 而非 Critical。
  - 建议: 把 `image`（或其编码）纳入 request；或在节点侧用 mask 做 `input*(1-m)+generated*m` 合成后再上传（evaluator `_read_ai_cache_result` 已有同款合成可复用）。
  - 置信度: 高

- [AUD-6307] [Major] 云端生成在渲染线程上同步执行，网络往返期间整个渲染队列停摆
  - 位置: src/features/composite/nodes/ai_gen_expand_node.py:220-222
  - 证据:
    ```python
    job = self._build_job(image, mask_pixels, input_pixels,
                          f"rev{int(image.revision)}")
    state, rgba = job.run(count=1)
    self.last_state = state
    ```
    本节点能力为 `action_type="source"`（node_capabilities.py:879-886）→ 评估器在 `CompositeRenderer(GraphEvaluatorMixin, QThread)`（composite_renderer.py:87）线程内同步调用 `node.evaluate`；`job.run` 内部 `rgba = self.transport(request, api_key, self.output_size)`（image_gen_job.py:214）为一次云 API 往返，且无超时控制。
  - 影响: 一次生成数秒到数十秒内预览/导出/播放全部阻塞；模块 docstring 自认 "MVP … Background job queueing via the AI compute manager is the follow-up wiring"。
  - 建议: 按既定计划接入 AIComputeManager 异步分支（cache miss 时先直通、结果落地后触发 `AI_RESULT_READY` 重评估，同 `_evaluate_ai_node` 模式）；transport 层加超时。
  - 置信度: 高

- [AUD-6308] [Major] `cost_confirm`（费用确认回调）在渲染线程被调用，任何交互式实现都会从非 UI 线程操作 Qt
  - 位置: src/features/composite/nodes/ai_gen_expand_node.py:40-41, 189-193
  - 证据:
    ```python
    _COST_CONFIRM: Optional[Callable[[int, str], bool]] = None
    ...
        cost_confirm=_COST_CONFIRM,
        output_size=(width, height),
    ```
    `job.run` 在网络调用前执行 `if self.cost_confirm is not None and not self.cost_confirm(int(count), self.prompt):`（image_gen_job.py:192-195），运行于渲染 QThread（见 AUD-6307）。
  - 影响: 回调语义是"向用户确认费用"（image_gen_job.py:49-51 注释 "prompt the user"）——按设计实现的弹窗/对话框会在渲染线程创建 Qt 控件，违反 UI 线程铁律，可能崩溃或未定义行为；当前生产未接线（AUD-6311），属潜伏缺陷。
  - 建议: 确认回调通过信号投递到 UI 线程执行并同步等待结果，或改为预授权额度检查（纯数据、无 UI）。
  - 置信度: 高（线程归属确凿）/ 中（后果取决于未来实现形态）

- [AUD-6309] [Major] 每次评估都做全帧 GPU 回读 + 全量像素 SHA-256，cache 命中路径也不例外
  - 位置: src/features/composite/nodes/ai_gen_expand_node.py:216-221（并见 image_gen_job.py:135-142, 86-93）
  - 证据:
    ```python
    input_pixels = read_rgba(image, ctx, fb_manager)
    mask_pixels = (read_rgba(mask, ctx, fb_manager)
                   if mask is not None else None)

    job = self._build_job(image, mask_pixels, input_pixels,
                          f"rev{int(image.revision)}")
    state, rgba = job.run(count=1)
    ```
    `CloudImageGenJob.__init__` 恒定执行 `image_bytes = np.ascontiguousarray(image.astype(np.float32)).tobytes()` 后 `cache_key(...)` 对其做 `digest.update(part)`（sha256，image_gen_job.py:86-93）。
  - 影响: 4K RGBA f4 一帧约 132 MB：每次评估付出全帧回读 + 多次 numpy 拷贝 + 132 MB 哈希，即使磁盘缓存命中（静帧重复导出/重放）也要全付；与项目基准（4K 实时）和 evaluator AI 分支的 P2-9 优化（"key derived BEFORE any full-frame readback … an in-flight request costs zero readback per frame"，base_evaluator.py:1382-1385）方向相悖。上游 `texture_id:revision` 指纹（本节点自己的 identity 回退 :175-180 已示范）即可作为等价 key 盐。
  - 建议: cache key 用 `texture_id/width/height/revision` 指纹；仅在真正发起网络生成前才做像素回读。
  - 置信度: 高

- [AUD-6310] [Major] 输出纹理用裸 `gl.texture` 创建，脱离 resource_pool/fb_manager 追踪体系，无法被释放也无法被下游按 id 解析
  - 位置: src/features/composite/nodes/ai_gen_expand_node.py:234-248
  - 证据:
    ```python
    gl = getattr(ctx, "gl", ctx)
    h, w = rgba.shape[:2]
    data = np.ascontiguousarray(np.flipud(rgba))
    tex = gl.texture((w, h), 4, data=data.tobytes(), dtype="f4")
    return {"image": TextureResult(
        texture_id=tex.glo, width=w, height=h,
    ```
    Python 侧 `tex` 包装随即丢弃（TextureResult 只存 int glo）。而所有按 id 的解析/释放路径只扫描三处容器：`fb_manager._active_textures`(set)/`_texture_pool`/`resource_pool._pooled_textures`（`_gpu_readback.find_texture` :30-36；`_get_texture_by_id` base_evaluator.py:3017-3041；`release_texture_by_id` gpu_resource_pool.py:985-992）。
  - 影响: (a) 该纹理不在任何容器 → 缓存驱逐时 `release_texture_by_id` 找不到，GL 释放完全依赖 Python GC（且可能在非 GL 线程析构，违背 deferred-release 模型）；每次评估新分配一张全分辨率 f4 纹理（即便 cache 命中）造成显存churn。(b) 下游任何 CPU 评估节点（本节点串联、`_evaluate_ai_node` 的 `_readback_texture_fullres`）按 glo 解析失败 → 静默降级。repo 已有正确范式：`_upload_ai_result_texture` 用 `self._resource_pool.acquire_texture(w, h)` + `tex.write(...)`（base_evaluator.py:1587-1590）；`_do_evaluate` 签名里本就传入 `resource_pool` 却未使用。注：frame_interpolate/deflicker 等旧节点存在同款模式，属跨文件共性问题，本条仅就本文件记录。
  - 建议: 改用 `resource_pool.acquire_texture(w, h, components=4, dtype="f4")` + `tex.write(data.tobytes())`，纳入预算与 deferred-release 体系。
  - 置信度: 高

- [AUD-6311] [Major] `set_generation_transport` 在生产代码中零调用，节点永远停留在 NEEDS_CONFIG 直通
  - 位置: src/features/composite/nodes/ai_gen_expand_node.py:43-56, 206-210
  - 证据:
    ```python
    def set_generation_transport(transport: Optional[GenerationTransport], ...):
        """Install the process transport (production: Volcengine adapter)."""
    ...
    if _TRANSPORT is None:
        self.last_state = "NEEDS_CONFIG"
        self.state_detail = "no generation transport configured"
        self.error_message = "AI Gen: NEEDS_CONFIG — no transport/key"
        return {"image": image}
    ```
    全库 grep：调用方仅 tests/composite/test_ai_gen_expand.py 与 tests/composite/test_advanced_plan_audit.py；docstring 声称 "wired by the page from preferences/env"（:6-8），无任何 page/适配器实现。
  - 影响: 节点已注册进 NODE_REGISTRY 与 AI 分类（node_engine.py:227,351）可被用户创建，但在生产构建中功能完全不可用（恒直通）——已发布功能与实现脱节。
  - 建议: 接入 Volcengine 适配器与偏好设置密钥时完成 :43 的接线，或在接线前将节点标记 placeholder/隐藏。
  - 置信度: 高

- [AUD-6312] [Suggestion] 进程级三个全局无锁、非原子地成组替换，热切换窗口可能错配
  - 位置: src/features/composite/nodes/ai_gen_expand_node.py:38-52
  - 证据:
    ```python
    _TRANSPORT: Optional[GenerationTransport] = None
    _KEY_PROVIDER: Optional[Callable[[], str]] = None
    _COST_CONFIRM: Optional[Callable[[int, str], bool]] = None

    def set_generation_transport(transport, key_provider=None, cost_confirm=None) -> None:
        global _TRANSPORT, _KEY_PROVIDER, _COST_CONFIRM
        _TRANSPORT = transport
        _KEY_PROVIDER = key_provider
    ```
  - 影响: 渲染线程读取三者时若 UI 线程正在重新注入，可能拿到"新 transport + 旧 key_provider"的错配组合；当前只在测试中调用、且 `transport is None` 有前置检查，实际风险低。
  - 建议: 合并为单个不可变的 dataclass/tuple 全局（一次赋值原子），或加锁。
  - 置信度: 中

- [AUD-6313] [Suggestion] provider 输出未经形状/通道/取值域校验即写缓存并上传，坏结果会被"投毒缓存"固化为 DONE+直通
  - 位置: src/features/composite/nodes/ai_gen_expand_node.py:222-229, 236-248
  - 证据:
    ```python
    state, rgba = job.run(count=1)      # run 内 store_cache(rgba) 在上传前执行
    ...
    self.error_message = ""
    try:
        gl = getattr(ctx, "gl", ctx)
        h, w = rgba.shape[:2]
        data = np.ascontiguousarray(np.flipud(rgba))
    ```
  - 影响: 若 transport 返回 HxWx3 或 [0,255] 域数组，`job.run` 先把它缓存并返回 DONE；随后 `gl.texture(...,4,...)` 因字节数不符抛异常 → 走 except 永久直通，但 `last_state=="DONE"` 且缓存命中使每次评估都重复该结局——状态与像素不一致且无法自愈（需手删磁盘缓存）。取值域未校验时若上传成功则显示过曝。
  - 建议: `job.run` 返回后校验 `rgba.ndim==3 and rgba.shape[2]==4` 及数值域，不合法则 FAILED 且不落缓存。
  - 置信度: 中（需要 transport 违约才触发）

- [AUD-6314] [Suggestion] 连接的 mask 尺寸不符时被静默丢弃，退化为 border/full-frame 生成请求
  - 位置: src/features/composite/nodes/ai_gen_expand_node.py:156-174
  - 证据:
    ```python
    if mask_pixels is not None and mask_pixels.ndim >= 2 \
            and mask_pixels.shape[0] == height \
            and mask_pixels.shape[1] == width:
        mask_array = np.ascontiguousarray(...)
    if mask_array is None and (float(self.parameters.get("expand_left", 0.0)) ...
    ```
  - 影响: mask 与输出尺寸不一致（proxy/ROI 端口错配等）时无任何日志，用户请求"局部填充"却可能触发计费的整帧生成，难排查。
  - 建议: 丢弃 mask 时 `logger.warning` 附带两侧尺寸。
  - 置信度: 高（行为确凿；后果取决于 provider 计费）

- [AUD-6315] [Suggestion] `_build_job` 的 `revision_signature` 参数从未被使用（死参数）
  - 位置: src/features/composite/nodes/ai_gen_expand_node.py:134-137, 220-221
  - 证据:
    ```python
    def _build_job(self, image: TextureResult,
                   mask_pixels: Optional["np.ndarray"],
                   input_pixels: Optional["np.ndarray"],
                   revision_signature: str) -> Any:
    ```
    方法体内无任何 `revision_signature` 引用；调用方传入 `f"rev{int(image.revision)}"`（:220-221）。
  - 影响: 误导维护者以为 revision 参与 cache key（实际 key 由 `image.texture_id/width/height/revision` 组成的 identity 数组——仅当 `input_pixels is None` 时——或全量像素决定）。
  - 建议: 删除该参数或真正纳入 key。
  - 置信度: 高

- [AUD-6316] [Suggestion] 输出 TextureResult 沿用输入 `bbox/format`，当实际输出尺寸与输入不一致时元数据失配
  - 位置: src/features/composite/nodes/ai_gen_expand_node.py:152-155, 239-245
  - 证据:
    ```python
    width = int(self.parameters.get("output_width", 0)) \
        or int(image.width)
    height = int(self.parameters.get("output_height", 0)) \
        or int(image.height)
    ...
    return {"image": TextureResult(
        texture_id=tex.glo, width=w, height=h,
        format=image.format,
        bbox=image.bbox,
    ```
  - 影响: `output_width/height` 是可序列化参数（测试即设 128），provider 亦可能返回非请求尺寸：此时 `w/h` 来自 `rgba` 实际形状，而 `bbox/format` 仍是输入的，"纹理实际尺寸 ≠ 声明数据窗口"会让下游按 bbox 对位取样时错位。
  - 建议: 输出尺寸 ≠ 输入时按新尺寸重建 bbox（或直接拒绝非请求尺寸的返回并 FAILED）。
  - 置信度: 中

### 通过项
- 线程安全（除 AUD-6307/6308/6312 外）：无共享可变状态；`last_state/state_detail/error_message` 由渲染线程写、UI 读，GIL 下信息性读一致
- 异常与取消路径：transport 异常在 `job.run` 内被捕获转 FAILED（image_gen_job.py:210-217）；上传失败 try/except 回退直通（:246-248）；缺输入/无效输入返回 `{}`（:200-202）；无 transport 时 NEEDS_CONFIG 可见降级（:206-210）——降级可见性好
- 状态提交一致性：参数经 `set_parameter`（bump param_revision/output revision）；cache key 含 prompt/seed/strength/model/尺寸/mask，序列化往返有测试覆盖（test_ai_gen_expand.py:124-131）
- HDR 正确性：f4 上传无 clamp；`np.flipud` 上传/回读方向约定一致（与 `_readback_texture_fullres`、`_upload_ai_result_texture` 相同）
- GLSL：本文件无着色器
- 输入验证：`image.is_valid` 前置（:201）；mask 有效性检查（:204）
- 信号防递归：无 Qt 信号代码
- 时域一致性（plan C1）已在 docstring 明示静帧适用范围

---

## src/features/composite/nodes/ai_generate_node.py (行数: 31)

### 发现
- [AUD-6317] [Suggestion] `__init__` 缺少类型注解
  - 位置: src/features/composite/nodes/ai_generate_node.py:24
  - 证据:
    ```python
    def __init__(self, node_id=None):
        super().__init__(node_id)
        # 输入端口不可省：快照按端口名校验连接，缺 inputs 会让上游连接
    ```
  - 影响: 同 AUD-6304。
  - 建议: 补 `Optional[str]` 注解。
  - 置信度: 高

### 通过项
- 端口完备性注释（P0-3：缺 inputs 端口导致快照丢连接→图尾 None）与实现一致，占位直通安全
- PARAM_DEFS（output_width/height 1024 默认，min 1）合法
- 计划层 placeholder 直通；无 GPU/线程/信号/持久化代码

---

## src/features/composite/nodes/ai_inpaint_node.py (行数: 29)

### 发现
- [AUD-6318] [Suggestion] `__init__` 缺少类型注解
  - 位置: src/features/composite/nodes/ai_inpaint_node.py:21
  - 证据:
    ```python
    def __init__(self, node_id=None):
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
    ```
  - 影响: 同 AUD-6304。
  - 建议: 补 `Optional[str]` 注解。
  - 置信度: 高

### 通过项
- image+mask 双输入端口与 ai_gen_expand/ai_remove 同族一致；占位直通安全
- PARAM_DEFS 合法（strength 0.75 默认）
- 无 GPU/线程/信号/持久化代码

---

## src/features/composite/nodes/ai_remove_node.py (行数: 88)

### 发现
- [AUD-6319] [Suggestion] 异步分支不可用时的直通回退完全静默，无任何诊断状态
  - 位置: src/features/composite/nodes/ai_remove_node.py:76-88
  - 证据:
    ```python
    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx, fb_manager,
                     resource_pool=None, render_mode="preview") -> Dict[str, TextureResult]:
        """Pass-through fallback.
        ...
        image = inputs.get("image")
        if image is None or not image.is_valid:
            return {}
        return {"image": image}
    ```
  - 影响: 管理器缺失（`_ai_compute_manager/_ai_cache_manager` 为 None，evaluator `_evaluate_ai_node` 同样静默直通，base_evaluator.py:1389-1391）或测试直评时，"AI 擦除"实际什么都没做却无 `error_message`/last_state 提示——与 ai_gen_expand 的 NEEDS_CONFIG 可见降级模式不一致，用户误以为擦除已完成。
  - 建议: 回退路径设置 `self.error_message = "AI Remove: compute unavailable — passthrough"` 之类的可见状态。
  - 置信度: 高（行为事实确凿；严重度定 Suggestion 因为主路径接线正常）

- [AUD-6320] [Suggestion] `_do_evaluate` 参数部分缺类型注解（ctx/fb_manager/resource_pool/render_mode）
  - 位置: src/features/composite/nodes/ai_remove_node.py:76-77
  - 证据:
    ```python
    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx, fb_manager,
                     resource_pool=None, render_mode="preview") -> Dict[str, TextureResult]:
    ```
  - 影响: 与同文件其余注解风格不齐（基类同参亦未注解，属全局一致性欠账）。
  - 建议: 补 `ctx: Any, fb_manager: Any, resource_pool: Any = None, render_mode: str = "preview"`。
  - 置信度: 高

### 通过项
- 线程安全：真实计算走 AIComputeManager 异步 worker（含并发上限与取消，ai_compute_manager.py:1147-1165），节点自身无共享状态
- GPU 资源生命周期：节点不持有 GPU 资源；遮罩外像素保持由 evaluator 的 `_read_ai_cache_result` 合成保证（base_evaluator.py:1517-1553）
- bbox/ROI：`compute_output_bbox` 恒等、`compute_input_roi` 全 ROI，与 `roi_policy="identity"` 能力声明一致
- 异常路径：缺/无效输入返回 `{}`；直通回退保证图不断流（防黑帧，注释明确）
- 输入验证：mask 白=擦除语义在 docstring 明示；`_init_port_revisions` 正确
- 持久化：无自管参数外的状态；`self.parameters` 初始化值与 PARAM_DEFS 一致
- `if __name__ != "__main__"` 的 BBox 导入守卫无害（BBox 仅出现在字符串注解中）

---

## 汇总

| 严重度 | 数量 | 编号 |
|---|---|---|
| Critical | 0 | — |
| Major | 6 | AUD-6306, 6307, 6308, 6309, 6310, 6311 |
| Suggestion | 15 | AUD-6300–6305, 6312–6320 |

Major 全部集中于 `ai_gen_expand_node.py`（云端生成路径）；其余 7 个文件以占位直通/助手为主，仅存质量级问题。`_gpu_readback.py` 的两个 Suggestion 为状态卫生问题。
