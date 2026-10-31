# batch-299 审查报告：src/features/teaching_animation/render/

批次：batch-299 [P2]，编号区间 AUD-29900–AUD-29999。
文件（3 个，715 行）：effect_capabilities.py (357)、evaluator.py (290)、frame_adapter.py (68)。
已完整 Read 全部文件，并 Read 了核心依赖（core/contracts.py、core/model.py、core/hierarchy.py、core/layout.py、motion/expression.py、src/shared/keyframe.py）以核实跨模块行为。

---

## src/features/teaching_animation/render/effect_capabilities.py (行数: 357)

### 发现

- [AUD-29901] [Suggestion] frozen dataclass `EffectDeclaration` 含不可哈希的 dict 字段，隐式 `__hash__` 调用即抛 TypeError，且 params 内容可被外部突变
  - 位置: src/features/teaching_animation/render/effect_capabilities.py:48-54
  - 证据:
    ```python
    @dataclass(frozen=True)
    class EffectDeclaration:
        """A scene-side reference to a catalog effect, attached to an element/layer."""

        effect_type: str                        # e.g. "blur", "glow" — a catalog key
        params: Mapping[str, Any] = field(default_factory=dict)
        version: int = 1
        enabled: bool = True
    ```
  - 影响: `frozen=True`（且默认 `eq=True`）使 dataclass 生成 `__hash__`，任何把声明放入 set / 用作 dict key 的调用方会因 `hash(dict)` 抛 TypeError；同时 frozen 不阻止 `decl.params["x"] = ...` 的内容突变，快照/缓存键语义可被悄悄破坏。当前调用方（commands.py、svg_renderer.py、v2_adapter.py、composite_effect_bridge.py）尚未 hash，属潜在陷阱。
  - 建议: 将 `params` 声明为不可变映射（如 `MappingProxyType` 包装）并设置 `eq=True, frozen=True` 之外显式 `@dataclass(frozen=True, unsafe_hash=False)`，或提供 `key()` 方法返回 `(effect_type, tuple(sorted(params.items())), version, enabled)` 供哈希使用。
  - 置信度: 高（Python 语义确定；实际触发取决于未来调用方）

- [AUD-29902] [Suggestion] `resolve_effect_chain` 的 fallback 路径不复查 `drop_high_cost` 成本策略
  - 位置: src/features/teaching_animation/render/effect_capabilities.py:267-271
  - 证据:
    ```python
        if cap.support == BackendEffectSupport.UNSUPPORTED:
            if cap.fallback_effect_type and matrix.support(backend, cap.fallback_effect_type).support != BackendEffectSupport.UNSUPPORTED:
                applied.append(ResolvedEffect(declaration, cap.support, cap.cost,
                                              cap.fallback_effect_type, "fallback"))
    ```
  - 影响: 若某效果 cost 为 medium/low 未被成本策略丢弃，但注册的 `fallback_effect_type` 成本为 high，则在 `drop_high_cost=True` 的 draft/interactive 档位下仍会执行 high-cost fallback，违背 UX-04 档位策略；且 ResolvedEffect.cost 记录的是原效果的 cost 而非 fallback 的。当前三张内置矩阵均未设置 `fallback_effect_type`（138-172 行全为 None），故为经 `EffectCapabilityMatrix.register()` 注入后才可触发的策略漏洞。
  - 建议: 在 fallback 分支复查 `profile.drop_high_cost and fallback_cap.cost == "high"` 时改为 dropped；并让 ResolvedEffect 携带 fallback 的真实 cost。
  - 置信度: 中

### 通过项
- 线程安全：纯数据/纯函数模块；`EffectCapabilityMatrix` 的实例由各调用方每次 resolve 新建（svg_renderer.py:142、v2_adapter.py:91、composite_effect_bridge.py:66 均默认构造），`register()` 的可变性不被并发路径共享。
- GPU 资源生命周期：模块自述且核实为 renderer-neutral 纯声明数据，无 GLSL/GPU 对象。
- GLSL·HDR 正确性：不涉及着色器与 HDR 像素数据（声明性目录）。
- 信号防递归：无 Qt 信号/控件。
- 状态提交一致性：`write_effect_declarations` 返回 detached style 供 `SetElementEffectsCommand.apply/undo`（core/commands.py:165-183）快照-恢复使用，undo 路径恢复 `_previous_style` 原样引用，无中间态丢失；`read_effect_declarations`/`from_dict` 往返一致。
- 异常与取消路径：`read_effect_declarations` 对无效条目捕获 `(TypeError, ValueError)` 忽略，docstring 明示为宽容读取设计（不阻塞旧场景打开）。
- 输入验证：`EffectDeclaration.__post_init__` 校验非空 effect_type 与正 version；`DirtyRange.__post_init__` 校验非负 start_frame 与递增区间；`read_effect_declarations`/`read_scene_gpu_effects` 对 style/metadata 非 Mapping、effects 非 list/tuple 均有类型防御。
- 性能模式：能力矩阵为 O(1) dict 查询；`_BACKENDS` 浅拷贝防御外部修改。
- 类型注解：公共 API 均有完整注解与返回类型。

---

## src/features/teaching_animation/render/evaluator.py (行数: 290)

### 发现

- [AUD-29903] [Major] element style 中非数值 `opacity` 经裸 `float()` 无防护，脏场景数据使整帧求值抛异常
  - 位置: src/features/teaching_animation/render/evaluator.py:203
  - 证据:
    ```python
            opacity = max(0.0, min(1.0, resolved_value(("opacity",), float(effective.style.get("opacity", 1.0)))))
    ```
    而同文件 212 行对其他样式属性使用了防护函数：
    ```python
            static_default = _float_or(effective.style.get(style_prop), _STYLE_DEFAULTS[style_prop])
    ```
  - 影响: `SceneElement.from_dict` 对 style 仅 deepcopy 不做类型校验（core/model.py:189），core/validator.py 无 opacity 类型检查；持久化 JSON 中 `"opacity": null`（TypeError）或 `"opacity": "50%"`（ValueError）会让 `evaluate()` 直接抛异常，该元素所在帧的预览/导出渲染失败（host_adapters.render_frame 无捕获，异常向播放路径传播；timeline_preview_renderer 仅静默丢帧）。
  - 建议: 改用 `_float_or(effective.style.get("opacity", 1.0), 1.0)`，与 212 行保持一致；或在场景加载层对 style 标量键做 schema 校验。
  - 置信度: 中（需脏数据触发，但验证链路确实缺失）

- [AUD-29904] [Major] `base_style` 为 None 时防御分支 `dict(base_style)` 自身抛 TypeError
  - 位置: src/features/teaching_animation/render/evaluator.py:274-277
  - 证据:
    ```python
            base_style = effective_elements[element.element_id].style
            merged_style = dict(
                resolve_style(base_style, tokens) if isinstance(base_style, Mapping) else dict(base_style)
            )
    ```
  - 影响: `SceneElement.style` 注解为 Mapping 且默认 dict，但 `from_dict` 用 `_copy_json(data.get("style", {}))`（deepcopy）——JSON 中 `"style": null` 会原样存为 None（dataclass 无类型校验），else 分支 `dict(None)` 抛 `TypeError: 'NoneType' object is not iterable`，防御写法对最常见脏值（null）恰好崩溃，整帧求值失败。
  - 建议: else 分支改为 `dict(base_style) if isinstance(base_style, (Mapping, dict)) else {}`，或统一前置 `base_style = base_style if isinstance(base_style, Mapping) else {}`。
  - 置信度: 中

- [AUD-29905] [Suggestion] Auto Layout `resolve` 失败被宽泛 except 静默吞掉且不记录任何 diagnostics
  - 位置: src/features/teaching_animation/render/evaluator.py:227-229
  - 证据:
    ```python
        try:
            layout_rects = scene_layout.resolve(scene.width, scene.height, preferred)
        except Exception:  # noqa: BLE001 - a layout error must never break rendering
            layout_rects = {}
    ```
  - 影响: 降级不崩渲染是合理的，但与同函数 185-187 行表达式失败记录 diagnostics 的做法不一致：布局坏档时元素静默回退到未布局坐标，预览与导出一致错位且无任何可观测信号（EvaluatedScene.diagnostics 为空），违背"不得宽泛 except 掩盖领域错误"的精神（尽管有 noqa 注释声明意图）。
  - 建议: 在 except 分支向 diagnostics 追加一条（如 `layout resolution failed; elements placed at authored coordinates`），供 RenderEvidence/导出预检消费。
  - 置信度: 高（行为确定，评级为改进项）

- [AUD-29906] [Suggestion] 表达式求值仅捕获 `ExpressionError`，深嵌套源码触发的 `RecursionError`/`MemoryError` 会逃逸并打断整帧渲染
  - 位置: src/features/teaching_animation/render/evaluator.py:170-187
  - 证据:
    ```python
                try:
                    value = evaluate_expression(
                        expression.source,
                        ExpressionContext(...),
                    )
                    ...
                except ExpressionError as exc:
                    expression_values[expression.property] = float(expression.default)
    ```
  - 影响: `evaluate_expression`（motion/expression.py:142-174）只包装 SyntaxError/ZeroDivisionError/ArithmeticError/TypeError/ValueError 为 ExpressionError；模型限制表达式最长 4096 字符（core/model.py:318），足以构造上千层嵌套括号使 `ast.parse`/`eval` 抛 RecursionError（RuntimeError 子类，未包装），逃过本处 except 与 expression.default 回退，直接打断 `evaluate()` 整帧渲染。
  - 建议: 此处扩大捕获面至 `except (ExpressionError, RecursionError, MemoryError)`，或在 evaluate_expression 内统一包装。
  - 置信度: 中（触发需病态嵌套源码，路径真实存在）

- [AUD-29907] [Suggestion] `_decompose_affine(m: Any)` 与 `layout_rects: dict[str, Any]` 丢失精确类型注解
  - 位置: src/features/teaching_animation/render/evaluator.py:78、223
  - 证据:
    ```python
    def _decompose_affine(m: Any) -> tuple[float, float, float, float, float]:
    ...
        layout_rects: dict[str, Any] = {}
    ```
  - 影响: `m` 实际恒为 `core.hierarchy.Affine2d`（evaluator 已在函数内从 `..core.hierarchy` 导入符号，无循环导入障碍）；`layout_rects` 值实际为 `core.layout.Rect`（frozen dataclass，x/y/width/height 均已核实存在且验证非负）。Any 掩盖了这两个契约，静态检查无法发现属性拼写错误。
  - 建议: 延迟导入并以 `"Affine2d"`、`dict[str, Rect]` 标注（配合 `if TYPE_CHECKING` 导入）。
  - 置信度: 高

### 通过项
- 线程安全：`evaluate` 为纯函数，无模块级可变状态；被 render_worker.py 的 ThreadPoolExecutor 与 UI 命令层并发调用时只读共享（ANIMATABLE_STYLE_PROPERTIES 等模块常量只读），每次求值新建 tokens/hierarchy/layout 对象，无跨帧可变共享。
- GPU 资源生命周期：CPU 求值路径，不涉及 ModernGL/GLSL 对象。
- GLSL·HDR 正确性：opacity/fill_opacity/blur 等的 clamp 均为带语义边界的 UI 量（注释明示镜像 DOM overlay 静态范围），非 HDR 像素数据，不违反"无理由 clamp"铁律；`max(0.0001, scale)` 为防零缩放下限。
- 信号防递归：无 Qt 信号/控件。
- 状态提交一致性：只读求值，不产生命令或修订状态；与 commands.py:808、scene_renderer_router.py:272 调用方契约一致（同帧同轨同问法）。
- 异常与取消路径（部分）：帧边界检查（118-119 行 `frame >= duration_frames` 抛错）+ `AnimationFrame.__post_init__` 负帧/fps 非正验证（contracts.py:90-94）共同闭合边界；fps 匹配校验（116-117 行）；表达式正常失败路径正确回退 `expression.default` 并记录 diagnostics。
- 输入验证（部分）：`_float_or`/`_tracks_for` 对非数值与非本元素动画防御正确；场景非 TeachingSceneDocument 抛 TypeError；`page_index_for_frame` 对负帧有 max(0,·) 保护（core/model.py:490）；SceneAnimation.track 强制为标量 KeyframeTrack（from_dict 单态反序列化），`float(track.get_value(...))` 无元组返回风险。
- 性能模式：每帧 O(元素数×轨道数)；闭包/临时 dict 开销相对 SVG 光栅化可忽略；无每像素热点。
- 数学正确性核实：`_decompose_affine` 对 `Affine2d.from_transform` 的 T·R·S 列向量约定（hierarchy.py:41-53）精确（scale 列模长、atan2 旋转），scale 非负前提由 206 行 `max(0.0001, scale)` 保证；组合链多级非均匀缩放+旋转的剪切残余已在 docstring 声明折入 scale 估计。

---

## src/features/teaching_animation/render/frame_adapter.py (行数: 68)

### 发现

- [AUD-29908] [Major] `QApplication` 无锁 check-then-act 惰性初始化在渲染 worker 线程执行：可双实例创建或在工作线程创建
  - 位置: src/features/teaching_animation/render/frame_adapter.py:44-55
  - 证据:
    ```python
        global _QT_APPLICATION
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        ...
        _QT_APPLICATION = QApplication.instance() or QApplication([])
    ```
  - 影响: 本方法被 `render_worker.py` 的 ThreadPoolExecutor worker、Edit 时间线预览 worker（timeline_preview_renderer.py:4113-4115，docstring 明示 "this worker"）、export_service.py:505/542、composite_node.py:84 等非主线程路径调用。`instance() or QApplication([])` 是无锁的检查-创建：(a) 两个尚无全局实例的线程并发首调会各自构造 QApplication，PyQt6 下创建第二个实例会导致 Qt fatal/未定义行为；(b) 若 worker 先于 GUI 主线程到达，QApplication 在非主线程创建，违反 Qt "QApplication 须在主线程" 约束，宿主随后打开编辑器时主线程复用该实例创建 QWidget 可能告警或出错。`_QT_APPLICATION` 写入也无内存屏障。
  - 建议: 用 `threading.Lock` 包住检查-创建；GUI 宿主内优先依赖主线程已存在的实例，headless 路径在进程启动时（单线程阶段）显式初始化一次。
  - 置信度: 高（竞争窗口与线程上下文为代码事实；崩溃的实际触发依赖时序）

- [AUD-29909] [Suggestion] `os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")` 为进程级副作用，worker 先行时会劫持宿主 GUI 平台
  - 位置: src/features/teaching_animation/render/frame_adapter.py:45
  - 证据:
    ```python
        # Lazy Qt imports keep the render contract usable in headless workers
        # and avoid importing any QWidget/WebEngine UI classes.
        global _QT_APPLICATION
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    ```
  - 影响: 环境变量在 QApplication 构造时读取且进程全局。若任何后台渲染/缓存预热任务先于主窗口运行（此代码在每次 `_svg_to_rgba` 调用时执行），宿主后续创建的 QApplication 将落在 offscreen 平台上，主窗口不可见；多线程下写 `os.environ` 与主线程读环境（如派生 ffmpeg 子进程）也存在非线程安全竞争。正常启动顺序（GUI 先建）下为 no-op。
  - 建议: 将平台选择移到显式的 headless 入口（CLI/测试启动参数 `--platform offscreen`），模块内不再触碰 `os.environ`。
  - 置信度: 中

- [AUD-29910] [Suggestion] QImage 分配失败时 `bits()` 返回 None 导致 AttributeError，且异常路径 QPainter 未 end
  - 位置: src/features/teaching_animation/render/frame_adapter.py:57-67
  - 证据:
    ```python
        image = QImage(rendered.width, rendered.height, QImage.Format.Format_RGBA8888)
        image.fill(QColor(0, 0, 0, 0))
        renderer = QSvgRenderer(QByteArray(rendered.data))
        if not renderer.isValid():
            raise ValueError("SVG frame is invalid")
        painter = QPainter(image)
        ...
        data = bytes(image.bits().asstring(image.sizeInBytes()))
    ```
  - 影响: `RenderedFrame` 已验证尺寸为正（contracts.py:136-137），但极大画布（宽×高溢出或内存不足）时 QImage 构造失败返回 null image：`fill` 变 no-op、`bits()` 返回 None，`None.asstring` 抛 AttributeError 而非结构化错误；`renderer.render` 若抛异常，painter 未 `end()` 即被 GC（Qt 告警）。4K 常规尺寸不触发。
  - 建议: 构造后检查 `image.isNull()` 抛出带尺寸信息的 ValueError；用 try/finally 或 context 保证 `painter.end()`。
  - 置信度: 低

### 通过项
- 线程安全（除 QApplication 初始化外）：每次调用新建局部 QSvgRenderer/QImage/QPainter，不共享 painter；QImage 离屏绘制在非 GUI 线程为 Qt 允许的用法。
- GPU 资源生命周期：纯 CPU 光栅化适配器，无 ModernGL/纹理/缓冲对象；QSvgRenderer 为局部对象由引用计数回收，无泄漏路径。
- GLSL·HDR 正确性：rgba8 CPU 适配边界（上游 RenderedFrame 已定格 rgba8/svg），`renderer.render` 的 QRectF 参数显式浮点字面量（0.0 与 float() 转换），无隐式截断。
- 信号防递归：无 Qt 信号/控件逻辑。
- 状态提交一致性：无状态持久化职责。
- 异常与取消路径（部分）：无效 SVG 以 ValueError 显式失败而非黑帧；`from_rendered_frame` 对未知 pixel_format 拒绝；调用方（timeline_preview_renderer.py:4114-4117）以 (KeyError, TypeError, ValueError) 兜底降级。
- 输入验证（部分）：`RgbaFrame.__post_init__` 强校验 pixel_format 白名单与 data 长度 = w×h×4；rgba8 直通路径复用该验证。
- 性能模式：QImage RGBA8888 行对齐恒为 4 字节（width×4），`bits().asstring(sizeInBytes)` 无行填充错位；返回前 `bytes()` 拷贝避免悬挂缓冲，符合注释声明。
- 类型注解：公共 API（from_rendered_frame/RgbaFrame）注解完整。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 3 | AUD-29903, AUD-29904, AUD-29908 |
| Suggestion | 7 | AUD-29901, AUD-29902, AUD-29905, AUD-29906, AUD-29907, AUD-29909, AUD-29910 |
