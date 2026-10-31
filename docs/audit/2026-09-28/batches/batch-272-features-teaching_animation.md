# 批次报告：batch-272 — src/features/teaching_animation//

编号区间：AUD-27200–AUD-27299。审查文件 2 个（191 行），全部 Read 通读；关键疑点已
Read 交叉核实：`scene_store.py`、`composite/nodes/base_node.py`、`render/composite_effect_bridge.py`、
`render/effect_pass_runner.py`（契约注释）、`render/scene_renderer_router.py`、`render/frame_adapter.py`、
`core/professional.py`、`core/model.py`（duration/create_default）、`composite/evaluators/base_evaluator.py`
（异常兜底、帧缓存、动态源旁路、节点移除 cleanup）、`composite/render/render_scheduler.py`
（DYNAMIC_SOURCE_NODE_TYPES）、`app/controllers/file_controller.py`（保存/恢复 payload）、
`shared/media/motion_graphic_delivery.py`（preset 键）。

---

## src/features/teaching_animation/cache_quality.py (行数: 34)

### 发现

（无）

### 通过项
- 线程安全：`_QUALITY` 的读（`get_cache_quality`）写（`set_cache_quality`）均在 `threading.Lock` 内；preset 的 import 在锁外，不持锁做 IO。
- 输入验证：`set_cache_quality` 用 `str(quality or "").lower()` 归一并以 `MOTION_GRAPHIC_QUALITY_PRESETS` 白名单校验，未知值保持原选择（docstring 明示的行为）；初始值 `"high_quality"` 是 `motion_graphic_delivery.py:34` 的合法键。
- 类型注解：函数签名注解完整，`__all__` 明确。
- 无 GPU/GLSL/信号/持久化维度：纯进程级偏好持有者，不涉及渲染与撤销栈（跨会话不持久为设计意图，docstring "Process-wide"）。

---

## src/features/teaching_animation/composite_node.py (行数: 157)

### 发现

- [AUD-27201] [Major] `__init__` 注册的默认场景永不注销：节点删除/加载覆盖后成为孤儿场景，随工程保存持久化并跨保存-加载周期无界累积
  - 位置: src/features/teaching_animation/composite_node.py:42-45（注册）、144-154（cleanup 未移除）
  - 证据:
    ```python
    default_scene = TeachingSceneDocument.create_default(name="Motion Graphic Scene")
    store.register(default_scene)
    self.parameters = {"scene_id": default_scene.scene_id}
    ...
    def cleanup(self) -> None:
        if self._effect_bridge is not None:
            self._effect_bridge.cleanup()
        ...
        if self._texture is not None:
            self._texture.release()
            self._texture = None
    ```
  - 交叉证据：`scene_store.py:60-64` 提供 `remove(scene_id)`，但全库 grep 无任何调用者（仅 `file_controller.py:1278/1297` 的 `replace_all`）；节点从图中移除时求值器只调 `old_node.cleanup()`（base_evaluator.py:334-336）；保存时 `file_controller.py:1266` 以 `scenes=get_shared_scene_store().scenes()` 全量持久化 store 内容。
  - 影响: 每次实例化（画布新建、`from_dict` 加载、undo/redo 重建）都向共享 store 注册一个新场景。加载路径中 `__init__` 的默认场景随即被序列化的 `scene_id` 覆盖为孤儿；保存把孤儿写进 .topos，下次打开 `replace_all` 恢复旧孤儿、图加载又各加一个——每周期净增 N 个（N=motion_graphic 节点数）。孤儿场景污染 MG 编辑器场景列表、膨胀工程文件与内存。注意：被时间线 clip 引用的场景不随节点删除是正确的保守行为，缺陷在于"创建即弃"的默认场景与节点删除路径均无回收。
  - 建议: ① `from_dict` 恢复 `scene_id` 后注销 `__init__` 创建的默认场景；② `cleanup()` 中当场景无其他宿主引用（时间线 clip/其他节点）时调用 `store.remove()`；或③给构造路径加 factory 参数，加载时不再先建默认场景。
  - 置信度: 高

- [AUD-27202] [Major] 宽泛 `except Exception` 静默吞掉全部渲染错误且无任何日志，违反"不得宽泛 except 掩盖领域错误"铁律
  - 位置: src/features/teaching_animation/composite_node.py:82-91
  - 证据:
    ```python
    try:
        frame = renderer.render_frame(AnimationFrame(frame_number, scene.fps))
        rgba = FrameAdapter.from_rendered_frame(frame).data
    except Exception:
        # Fail-soft, matching the SVG path's ``scene is None`` return above:
        # an unavailable HTML backend (Playwright not installed) or a render
        # error must not crash the composite GL evaluate loop. ...
        return {}
    ```
  - 影响: 域错误（FrameAdapter 的 `ValueError("SVG frame is invalid")`/维度不匹配、渲染器自身 bug）与预期的"Playwright 缺失"一同被无声吞掉——无 logging、无 render_diagnostics，节点当帧空输出且完全不可排查。对照之下 `renderer.prepare()`（76 行）抛错会传到求值器并被 `base_evaluator.py:1034-1038` 记 warning，同类失败可观测性不一致。
  - 建议: 收窄为具体异常（`HtmlRenderError`/`ImportError`/`RuntimeError`/`ValueError`），并至少 `logger.debug(..., exc_info=True)` 记录后保持 fail-soft 返回 `{}`。
  - 置信度: 高

- [AUD-27203] [Major] 每帧重建渲染器 prepare/cleanup 且无帧级缓存：HTML 场景每显示帧一次浏览器会话建立-截图-关闭，SVG 每帧 CPU 重栅格化
  - 位置: src/features/teaching_animation/composite_node.py:70-93
  - 证据:
    ```python
    backend = backend_for_scene(scene)
    ...
    renderer = select_scene_renderer(scene)
    renderer.prepare(scene, render_context)
    ...
    frame = renderer.render_frame(AnimationFrame(frame_number, scene.fps))
    ...
    finally:
        renderer.cleanup()
    ```
  - 交叉证据：`motion_graphic` 属 `DYNAMIC_SOURCE_NODE_TYPES`（render_scheduler.py:106），求值器对其绕过持久缓存（base_evaluator.py:1160-1181），即每个显示帧都完整执行一次 `_do_evaluate`；HTML 适配器的 `prepare` 每次新建 v2 会话、`cleanup` 关闭（scene_renderer_router.py:172-200）；SVG 路径经 `FrameAdapter._svg_to_rgba` 重建 `QSvgRenderer` 栅格化（frame_adapter.py:57-68）。
  - 影响: 播放/导出教学动画（默认 240 帧）时逐帧全量重建渲染资源，HTML 场景浏览器往返无法接近 4K 实时基准；宿主主链已有 `scene_cache.py` 帧缓存机制，本节点未复用。
  - 建议: 按 (scene_id, scene revision, frame_number, backend) 缓存已渲染帧与渲染器/会话实例，场景变更或尺寸变化时失效（可复用 scene_cache 与 HtmlRendererService 池化思路）。
  - 置信度: 中（逐帧重建的事实为高置信；对实际帧率的量化影响未实测）

- [AUD-27204] [Major] HDR 场景实际输出 8bit 数据，TextureResult 却按声明的 color_pipeline 上报 bit_depth/color_space
  - 位置: src/features/teaching_animation/composite_node.py:97、126-133
  - 证据:
    ```python
    self._texture = gl.texture((scene.width, scene.height), 4, dtype="u1")
    ...
    return {"image": TextureResult(
        ...
        color_space=color_pipeline.working_space,
        bit_depth=color_pipeline.bit_depth,
    ```
  - 交叉证据：`RgbaFrame` 仅接受 `rgba8`（frame_adapter.py:22-24），`FrameAdapter.from_rendered_frame` 只产出 rgba8/svg；`SceneColorPipeline.is_hdr` 在 bit_depth>8 或 rec2020/acescg 时为真（professional.py:34-35）；`hdr_fallback: "svg_8bit_tonemap"` 仅在 render_diagnostics 中披露（composite_node.py:138）。
  - 影响: 场景声明 acescg/32bit 管线时，实际像素是 8bit（tonemap 后）显示域数据，TextureResult 却声明 32bit acescg；`TextureResult.bit_depth` 文档注明"Used for HDR processing and display conversion"（base_node.py:93-94），下游按其做 HDR/显示变换会对已 tonemap 数据二次变换 → 合成色彩错误。仅 gpu_effects 路径经 bridge 的 f2/f4 scratch 才接近声明精度。
  - 建议: 无 GPU effects、输出 u1 纹理时如实上报 `bit_depth=8` 与实际交付的 color_space（或引入"声明管线 vs 实际交付"分离字段），hdr_fallback 语义并入 TextureResult 元数据而非仅 diagnostics。
  - 置信度: 中（元数据与实际数据不符为确定事实；下游是否据此做显示转换未逐点核实）

- [AUD-27205] [Suggestion] 渲染器选择与 `prepare()` 在 try 保护之外，与注释宣称的 fail-soft 范围不符
  - 位置: src/features/teaching_animation/composite_node.py:67-82
  - 证据:
    ```python
        renderer = select_scene_renderer(scene)
        renderer.prepare(scene, render_context)
        ...
        try:
            frame = renderer.render_frame(AnimationFrame(frame_number, scene.fps))
    ```
  - 影响: `prepare()` 抛出的 `HtmlRenderError`（如资源解析失败）不会被本节点的 fail-soft 捕获，而是传到求值器兜底（记 warning、空输出）——与 85-91 行注释声称的"must not crash the composite GL evaluate loop"由不同层实现，两条路径行为与日志不对称。
  - 建议: 将 select/prepare 纳入同一受控异常块，统一本节点内的失败边界。
  - 置信度: 高

- [AUD-27206] [Suggestion] `_scene_changed` 的 `scene` 参数与 `_do_evaluate` 的 `ctx`/`fb_manager` 缺类型注解
  - 位置: src/features/teaching_animation/composite_node.py:53-58
  - 证据:
    ```python
    def _scene_changed(self, scene) -> None:
    ...
    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx, fb_manager,
                     resource_pool=None, render_mode="preview") -> Dict[str, TextureResult]:
    ```
  - 影响: 与 .clinerules 强类型要求不符；`scene` 应为 `TeachingSceneDocument`，`ctx` 实际是 `CompositeEvalContext | moderngl.Context` 二选一（60 行有 isinstance 分支），`resource_pool` 应为 `Optional[...]`。
  - 建议: 补全注解；`ctx` 的双形态可用 Union 或在文档注明。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：尺寸变化时先 `release()` 旧纹理再重建（94-98 行，求值线程持有 GL 上下文，直接释放合法）；effect bridge 在 GL 上下文切换时 cleanup 旧实例并重建（108-112 行）；`cleanup()` 释放 bridge/退订/纹理，且节点移除与图拆除确有调用方（base_evaluator.py:334-336、composite_renderer.py:1275、composite_export_renderer.py:1500）。
- GPU effects 输出的 scratch 纹理生命周期：`EffectPassRunner` 契约要求"同帧同线程立即消费、不得跨帧持有"（effect_pass_runner.py 头注）；motion_graphic 属动态源节点绕过持久缓存（base_evaluator.py:1004-1012、1160-1181），scratch glo 仅在当帧求值内被下游消费，未违反契约。
- 信号防递归：本文件无 Qt 信号；store 订阅回调仅置 `NodeState.DIRTY`（53-55 行），无递归路径。
- alpha 语义正确性：HTML premultiplied / SVG straight 如实写入 `alpha_premultiplied`（77-81 行），与渲染器实现（scene_renderer_router.py `_frame_context` 的 AlphaMode.PREMULT）一致，避免合成器双重转换。
- 输入验证：`scene is None` 返回 `{}`；`frame_number` clamp 到 `[0, duration_frames-1]`，且模型层强制 `duration_frames >= 1`（core/model.py:423-424），不会出现 -1 帧；`RgbaFrame` 校验数据长度=宽×高×4，纹理创建与写入尺寸一致。
- 状态提交一致性（帧缓存方向）：`motion_graphic` 在能力表中 `requires_frame_number=True`（node_capabilities.py:148-156）且属 `DYNAMIC_SOURCE_NODE_TYPES`，帧号进缓存键、逐帧重评，不会在播放中复用陈旧帧；场景变更经 `_scene_changed` 置 DIRTY 触发重评；`_revision` 每次 evaluate 递增供下游失效。
- 异常与取消（部分）：GPU effects 可选路径的 `except (ImportError, RuntimeError, TypeError, ValueError)` 有注释说明且降级保留源纹理（121-124 行），可接受。
- 线程安全（部分）：场景 store 以 RLock 保护且文档化线程安全；`_scene_changed` 仅做 GIL 原子的状态赋值。store 在锁外同步回调 listener 的线程归属由注册方保证，未发现本文件侧问题。
