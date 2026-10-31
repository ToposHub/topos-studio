# 批次报告：batch-304 — src/features/teaching_animation/render//

编号区间：AUD-30400–AUD-30499。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解。

交叉验证说明：本批结论参考了（只读）`render/effect_capabilities.py`、`render/evaluator.py`、`render/asset_resolver.py`、`render/frame_adapter.py`、`render/scene_renderer.py`、`render/scene_renderer_router.py`、`core/model.py`、`core/contracts.py`、`core/composition_schema.py`、`core/professional.py`、`scene_store.py`。

## src/features/teaching_animation/render/svg_renderer.py (行数: 629)

### 发现

- [AUD-30401] [Major] 预合成实例只展开一层，嵌套场景内部的二级实例被静默丢弃
  - 位置: src/features/teaching_animation/render/svg_renderer.py:584
  - 证据:
    ```python
    def _render_precomp_instances(self, frame: AnimationFrame, profile: RenderProfile) -> list[str]:
        ...
        for instance in sorted(read_instances(self._document.metadata), key=lambda item: item.start_frame):
            ...
            nested = evaluate(source, source_frame)
            ...
            for element in sort_for_paint(
                    nested.elements, read_hierarchy(source.metadata).parents, ...):
                ... _render_element(element, nested_resolver, source, ...)
    ```
  - 影响: 只遍历顶层文档的 instances；被引用场景自身的 instances 没有任何递归展开路径。若场景 B（被 A 引用）内部又引用场景 C，导出/预览会静默丢失 C 的全部内容；仅有的防护是直接自引用跳过（`source.scene_id == self._document.scene_id`），间接环 A→B→A 同样只靠"不递归"侥幸不崩。
  - 建议: 对实例展开做递归（带已访问 scene_id 集合防环与深度上限），或在不支持嵌套时输出显式 diagnostic/报错，禁止静默丢弃。
  - 置信度: 高

- [AUD-30402] [Major] 遮罩绑定支持以实例为目标，但渲染实例时从不引用遮罩
  - 位置: src/features/teaching_animation/render/svg_renderer.py:187
  - 证据:
    ```python
    known_targets.update(item.instance_id for item in read_instances(scene.metadata))
    ...
    bindings[mask.target] = mask_id          # line 205
    ...
    # 主元素循环 (line 559)：
    mask_bindings.get(element.source.element_id),
    ...
    # 实例组 (lines 618-621)：
    output.append(
        f'<g data-instance-id="{escape(instance.instance_id, quote=True)}" '
        f'opacity="{_number(opacity, 1.0)}" transform="translate(...)">'
        + "".join(pieces) + "</g>")
    ```
  - 影响: `_composition_defs` 明确把 instance_id 列为合法遮罩目标并生成 `<mask>` defs 与绑定，但 `mask_bindings` 只在父元素循环按 element_id 查询；实例 `<g>` 不带 `mask` 属性。遮罩指向实例时输出完全不蒙版的错误画面，且无任何 diagnostic。
  - 建议: 渲染实例组时查询 `mask_bindings.get(instance.instance_id)` 并写入 `mask="url(#...)"`，或在 `_composition_defs` 中拒绝实例目标并输出 diagnostic。
  - 置信度: 高

- [AUD-30403] [Major] final 档位下嵌套场景的 blocked 效果未 fail-closed，与父场景行为不一致
  - 位置: src/features/teaching_animation/render/svg_renderer.py:604
  - 证据:
    ```python
    nested_effect_defs, nested_chains = _effect_defs(nested.elements, profile)   # line 604
    ...
    # 对比父场景 (lines 531-538)：
    blocked = tuple(
        diagnostic
        for chain in effect_chains.values()
        if chain.blocked
        ...
    if blocked:
        raise RuntimeError("unsupported SVG effect in final profile: " + "; ".join(blocked))
    ```
  - 影响: `nested_chains` 赋值后从未检查 `blocked`。final profile（`block_on_unsupported=True`，见 effect_capabilities.py:218-219）下，被引用场景里不支持的特效被静默丢弃而不是抛错，违反 RenderPlan 的 `fail_policy="export_block"` 语义，导出出片与预览/父场景行为不一致且无提示。
  - 建议: 在 `_render_precomp_instances` 中对 `nested_chains` 执行与父场景相同的 blocked 检查并抛出同一错误。
  - 置信度: 高

- [AUD-30404] [Major] 每帧重复磁盘 IO 与 base64：图片 data_uri 逐帧重读，嵌套实例逐帧重建 AssetResolver 并 preflight
  - 位置: src/features/teaching_animation/render/svg_renderer.py:447
  - 证据:
    ```python
    asset = resolver.asset_by_id(scene, source.asset_ref or "")
    uri = resolver.data_uri(asset) if asset is not None else None      # 每帧调用
    ...
    nested_resolver = AssetResolver(self._context)                     # line 602
    nested_resolver.preflight(source)                                  # 每实例每帧
    ```
    且 asset_resolver.py:104 `encoded = base64.b64encode(path.read_bytes()).decode("ascii")` 无缓存。
  - 影响: `render_frame` 逐帧调用，图片元素每帧重新 read_bytes + base64（多 MB 素材在 24fps×数百帧导出中是纯重复 IO）；每个实例每帧重新构造 resolver 并执行含 `is_file()` 的 preflight。与项目 4K 实时性能基准相悖。
  - 建议: 在 `prepare`/`_render_precomp_instances` 首次解析时缓存 asset_id→data_uri 与 AssetResolver 实例，帧循环只查表。
  - 置信度: 高

- [AUD-30405] [Major] 父场景与嵌套场景元素 element_id 相同时，filter/gradient 定义 id 在同一 SVG 文档内重复
  - 位置: src/features/teaching_animation/render/svg_renderer.py:149
  - 证据:
    ```python
    return (f"effect_{element.source.element_id}" if fragments else None), chain   # line 149
    ...
    gradient_id = _safe_id(element.source.element_id, "gradient")                  # line 264
    ...
    pieces: list[str] = [nested_effect_defs] if nested_effect_defs else []         # line 605
    # pieces 与父级 effect_defs 拼进同一个 <svg>（line 546 + 561）
    ```
  - 影响: element_id 是场景局部命名，跨场景极易撞名（如 "shape_1"）。父级 defs 先出现，SVG 对重复 id 取首个定义：嵌套元素引用 `url(#effect_shape_1)`/`url(#gradient_shape_1)` 时会套用父场景的滤镜或渐变，产出错误效果。`_gradient_fill` 的 docstring 声称 "generated ids never collide across layers"，对预合成内联不成立。
  - 建议: 为实例子树引入 id 命名空间（如 `{instance_id}__{element_id}`）贯穿 `_effect_filter`/`_gradient_fill`/`_safe_id`。
  - 置信度: 中（机制确定；实际发生依赖跨场景撞名）

- [AUD-30406] [Suggestion] mask 的 composition_diagnostics 计算后被整体丢弃
  - 位置: src/features/teaching_animation/render/svg_renderer.py:530
  - 证据:
    ```python
    composition_defs, mask_bindings, composition_diagnostics = _composition_defs(self._document)
    ```
    此后文件内无任何对 `composition_diagnostics` 的引用（expression/effect diagnostics 均进入了 metadata，见 line 580）。
  - 影响: "mask target not found / path_ref unsafe" 等校验结果无人消费，场景数据问题在导出产物中不可观测。
  - 建议: 并入 RenderedFrame.metadata（如 `composition_diagnostics`）或按档位决定是否 fail。
  - 置信度: 高

- [AUD-30407] [Suggestion] `_filter_fragment` 对每个元素每帧最多重复构建三次
  - 位置: src/features/teaching_animation/render/svg_renderer.py:556
  - 证据:
    ```python
    effect_defs, effect_chains = _effect_defs(evaluated.elements, profile)   # 内部先 _effect_filter 后再重建 fragments (143-168)
    ...
    for element in sort_for_paint(...):
        ...
        filter_id, _ = _effect_filter(element, profile)                      # line 556，第二次
    ```
  - 影响: `_effect_defs` 里 `_effect_filter` 构建并丢弃 fragments、随后 163-168 重建一次，绘制循环又调一次 `_effect_filter`；`_render_precomp_instances`（604/611）同样成对重复。纯 CPU 字符串构建，量级小但属无谓重复。
  - 建议: `_effect_defs` 返回 element_id→filter_id 映射，绘制循环直接查表。
  - 置信度: 高

- [AUD-30408] [Suggestion] 未使用导入：read_mattes / SceneAssetRef / SceneElement；travel matte 在 SVG 路径完全未实现
  - 位置: src/features/teaching_animation/render/svg_renderer.py:13
  - 证据:
    ```python
    from ..core.model import SceneAssetRef, Scene_element, TeachingSceneDocument
    from ..core.composition_schema import MaskKind, read_instances, read_masks, read_mattes
    ```
    （SceneAssetRef/SceneElement/read_mattes 在本文件中除导入行外零引用。）
  - 影响: 死导入掩盖了功能缺口：SVG 渲染器既不解析也不应用 mattes，携带 travel matte 的场景静默按无 matte 输出（与 AUD-30413 的 plan 虚报叠加）。
  - 建议: 删除死导入；matte 不支持时应输出 diagnostic 或在 plan 中如实声明。
  - 置信度: 高

- [AUD-30409] [Suggestion] `_sanitize_svg` 用 xml.etree 解析不可信模型内容，缺少尺寸/实体加固
  - 位置: src/features/teaching_animation/render/svg_renderer.py:212
  - 证据:
    ```python
    try:
        root = ElementTree.fromstring(value)
    except ElementTree.ParseError:
        return ""
    ```
  - 影响: 输入是模型/LLM 生成的场景 JSON 内嵌 SVG（外部数据）。Python 文档明确 xml.etree 对实体膨胀类攻击不设防；当前运行时（expat 2.6.x）默认缓解 billion-laughs，但深层嵌套/超大输入仍可造成渲染线程内存与 CPU 放大，且无输入长度上限。
  - 建议: 解析前限制输入长度（如 256KB），或换用 defusedxml；保留现有白名单+转义逻辑不变。
  - 置信度: 中（运行时 expat 已默认缓解主要向量，属加固项）

### 通过项
- 线程安全：渲染器实例每次任务独立创建，无可变共享状态；渲染期访问的共享 scene store 内部 RLock 保护（scene_store.py:35-37）
- GPU 资源生命周期：本文件不创建任何 ModernGL/纹理/缓冲资源，纯 CPU 字符串生成，无泄漏面
- GLSL·HDR 正确性：无 GLSL；HDR 场景经 SVG 8-bit 输出时在 metadata 中显式声明 `hdr_fallback=svg_8bit_tonemap`（line 579），working_space/bit_depth 取值域与 RenderContext 校验集合对齐（professional.py:20-24 vs contracts.py:75-78），无不当 clamp 的浮点像素数据
- 信号防递归：无 Qt 信号/控件
- 状态提交一致性：对文档与 scene store 全程只读，不涉及 undo/redo/revision/持久化写入
- 异常与取消路径：每帧开始检查 CancellationToken（line 514），与 router 记载的 per-frame 轮询约定一致；错误帧向上抛出而非静默黑帧
- 输入验证：颜色走 `_HEX_COLOR` 白名单、字体名走字符白名单、数值经 `_safe_float`、路径经 `sanitize_path_data`/`_SAFE_PATH`；`_sanitize_svg` 白名单标签+属性、剥离全部 href 类引用、文本/属性均 html escape；`prepare` 校验 scene 类型（TypeError）；demo 渲染的 title/color 均 escape
- 类型注解：完整，无缺失

## src/features/teaching_animation/render/v2_adapter.py (行数: 254)

### 发现

- [AUD-30410] [Major] 协议参数 `cancel_token` 被完全忽略，调用方无法按 V2 约定取消渲染
  - 位置: src/features/teaching_animation/render/v2_adapter.py:195
  - 证据:
    ```python
    def render(
        self,
        session: RendererSession,
        context: FrameContext,
        cancel_token: Any | None = None,
    ) -> RendererResult:
        if not isinstance(session, SvgRendererSession):
            raise TypeError("SvgSceneRendererV2.render expects an SvgRendererSession")
        if self._cancellation is not None and self._cancellation.is_cancelled:
            raise RuntimeError("render cancelled before start")
    ```
    `cancel_token` 在整个函数体内（及整个文件中）除签名外零引用。
  - 影响: SceneRendererV2 协议（contracts.py:517-523）以 `cancel_token` 为取消通道；HTML 侧同型实现（html_scene_renderer_v2.py:290）会消费它。走本适配器的调用方传入 token 得不到响应，已取消的作业仍会完成整帧光栅化（含 AUD-30404 的重复 IO），取消只能靠构造期 token 在下一帧生效。
  - 建议: 将 `cancel_token`（duck-typed `is_cancelled`）与 `self._cancellation` 一并检查，或透传给 inner renderer。
  - 置信度: 高

- [AUD-30411] [Suggestion] 取消时抛裸 `RuntimeError` 而非 `RenderCancelled`，异常语义与 V1 路径不一致
  - 位置: src/features/teaching_animation/render/v2_adapter.py:200
  - 证据:
    ```python
    raise RuntimeError("render cancelled before start")
    ```
    对比 scene_renderer.py:8 `class RenderCancelled(RuntimeError)` 及 router/inner 渲染器统一抛 `RenderCancelled`（scene_renderer_router.py:182,301；svg_renderer.py:515）。
  - 影响: `RenderCancelled` 是 RuntimeError 子类，捕获 `RenderCancelled` 的调用方无法识别此处取消（反向不兼容）；当前 src 内尚无专门捕获点，属一致性/未来兼容缺陷。
  - 建议: 改为 `raise RenderCancelled("render cancelled before start")`。
  - 置信度: 高

- [AUD-30412] [Suggestion] `SvgRendererSession` 未实现 RendererSession 协议要求的 `close()`，靠 `type: ignore` 掩盖
  - 位置: src/features/teaching_animation/render/v2_adapter.py:140
  - 证据:
    ```python
    @dataclass(frozen=True)
    class SvgRendererSession:
        """Holds the prepared inner renderer + resolved contexts (owned by V2)."""
        inner: SvgSceneRenderer
        render_context: RenderContext
        plan: RenderPlan
    ...
    return SvgRendererSession(inner=inner, render_context=render_context, plan=plan)  # type: ignore[return-value]
    ```
    协议要求（contracts.py:495-499）：`def close(self) -> None`，"closed exactly once by the owning renderer"。
  - 影响: 任何按协议持有 `RendererSession` 并调用 `session.close()` 的通用代码在此实现上抛 AttributeError；"frozen contract" 的符合性被 type: ignore 静默绕过（当前 src 无人直接调 session.close()，故为潜在缺陷）。
  - 建议: 给 `SvgRendererSession` 增加幂等 `close()`（委托 `inner.cleanup()`）并移除 type: ignore。
  - 置信度: 高

- [AUD-30413] [Suggestion] plan 为 travel matte 声明 SVG_RASTER 能力，但 SVG 渲染器根本不实现 matte
  - 位置: src/features/teaching_animation/render/v2_adapter.py:113
  - 证据:
    ```python
    for matte in read_mattes(metadata):
        passes.append(RenderPass(stage="effect", target=matte.target,
                                 capability=Capability.SVG_RASTER,
                                 params={"matte_id": matte.matte_id, "source": matte.source, "mode": matte.mode.value}))
    ```
    而 svg_renderer.py 导入 `read_mattes` 后零使用（见 AUD-30408）。
  - 影响: plan/evidence 宣称该 pass 由 SVG 后端承担，实际 matte 从未应用到像素；下游若按 plan 判定能力完备性会被误导（当前 plan 仅描述性元数据，故定为 Suggestion）。
  - 建议: matte pass 的 capability 如实标注（或 capability_fallback 中记录 `matte:unsupported_svg`），与像素行为一致。
  - 置信度: 高

- [AUD-30414] [Suggestion] `render()` 忽略后续 FrameContext 的 surface 变化，但 evidence 记录的是新 context 的值
  - 位置: src/features/teaching_animation/render/v2_adapter.py:191
  - 证据:
    ```python
    animation_frame = AnimationFrame(context.frame, context.fps)
    rendered = session.inner.render_frame(animation_frame)   # 使用 prepare 期缓存的 RenderContext（尺寸/透明/档位）
    ...
    "colorspace": context.surface.colorspace.value,
    "dpr": context.surface.dpr,
    ```
  - 影响: 帧号与 fps 从新 context 取，但宽高、alpha_mode、proxy_tier 全部沿用 `session.render_context`（prepare 时冻结）。若调用方在 render 时传入与 prepare 不同的 surface（如 DPR 变化），产物是旧尺寸像素、evidence 却登记新值，诊断/缓存键会失真。
  - 建议: render 中断言 context 与 session.render_context 的 surface 关键项一致，不一致即抛错；或以新 context 重建。
  - 置信度: 中（正常调用序 prepare/render 同 context 时不触发）

### 通过项
- 线程安全：session 为 frozen dataclass，无可变共享状态；无 UI 线程交互
- GPU 资源生命周期：无 GPU 资源；光栅化走 CPU QSvgRenderer（QImage 工作线程绘制为 Qt 支持模式）。跨文件备注：frame_adapter.py:55 在无实例时会创建 QApplication，若本 render 在工作线程首次触发存在归属风险，归属 batch 另行审查的 frame_adapter.py
- GLSL·HDR 正确性：无 GLSL；HDR 管线的 working_space/bit_depth 透传进 RenderContext（值域与校验集合一致），`fail_policy` 按 profile.block_on_unsupported 正确给出 export_block/preview_placeholder（line 136）
- 信号防递归：无 Qt 信号
- 状态提交一致性：全程只读（scene store 仅读），close→cleanup 释放路径幂等
- 输入验证：`transparent` 采用 for_scene 的 PREMULT=带 alpha 冻结约定（contracts.py:413，与 html_scene_renderer_v2.py:294 一致）；profile 映射的 proxy_tier 值均在 RenderContext 校验集合内；session 类型不匹配抛 TypeError
- 性能模式：frame_hash/evidence 仅对最终 RGBA 计算一次；plan 构建在 prepare 期一次性完成（嵌套场景不进 plan 的元素级 pass，属 AUD-30401 范畴）
- 类型注解：完整（协议符合性缺口由 AUD-30412 记录）
