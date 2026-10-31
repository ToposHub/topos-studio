# 批次报告：batch-303 — src/features/teaching_animation/render//

- 审查日期：2026-09-28
- 编号区间：AUD-30300–AUD-30399
- 文件数：8（1,672 行）
- 汇总：Critical 1 · Major 4 · Suggestion 8

| 编号 | 严重度 | 文件 | 标题 |
|------|--------|------|------|
| AUD-30309 | Critical | render_worker.py | `_handles` 无界增长，完成 job 的全部帧数据永不释放 |
| AUD-30300 | Major | html_scene_renderer_v2.py | 宽泛 `except Exception: pass` 吞掉领域错误与 IO 错误 |
| AUD-30301 | Major | html_scene_renderer_v2.py | `document.lower()` 索引切片原文档，Unicode 大小写展开导致插入偏移 |
| AUD-30304 | Major | html_validation.py | 以异常消息子串分类错误（"uniform frame"/"near-uniform"） |
| AUD-30311 | Major | scene_renderer_router.py | `_html_with_native_overlay` 同样的 lower() 索引偏移（主渲染路径） |
| AUD-30302 | Suggestion | html_scene_renderer_v2.py | 每帧 `inspect.signature` 反射探测服务签名 |
| AUD-30303 | Suggestion | html_scene_renderer_v2.py | 验证背景色未校验即内插 `<style>` |
| AUD-30305 | Suggestion | html_validation.py | `scene.fps`/`scene.scene_id` 直接访问与同文件防御式 getattr 不一致 |
| AUD-30306 | Suggestion | html_validation.py | `RenderFrame` 类型别名与实际签名不符且未使用 |
| AUD-30307 | Suggestion | perf_slo.py | 冻结 dataclass 持有可变 Mapping |
| AUD-30308 | Suggestion | release_fixtures.py | 硬编码 renderer_build 版本漂移风险 |
| AUD-30310 | Suggestion | render_worker.py | `_handles` 无锁共享；`render_sync` 回调注解与 `submit` 不一致 |
| AUD-30312 | Suggestion | scene_renderer_router.py | Vector 适配器复制基类 render_frame 检查逻辑 |

---

## src/features/teaching_animation/render/html_scene_renderer_v2.py (行数: 378)

### 发现

- [AUD-30300] [Major] `resolve_bound_image_bytes` 用宽泛 `except Exception: pass` 吞掉所有错误
  - 位置: src/features/teaching_animation/render/html_scene_renderer_v2.py:150
  - 证据:
    ```python
        try:
            resolver = AssetResolver(context)
            ...
            resolver.preflight(scene)
            for asset_id in bound:
                ref = resolver.asset_by_id(scene, asset_id)
                ...
                out[asset_id] = path.read_bytes()
        except Exception:
            # Best-effort: a strict preflight failure, a dict-shaped scene, or a
            # read error leaves `out` partial/empty; the backend then serves its
            # deterministic placeholder for whatever is missing.
            pass
    ```
  - 影响: 违反铁律"不得宽泛 except 掩盖领域错误"。除预期的 `AssetPreflightError` 外，磁盘 IO 错误（PermissionError/坏扇区）、API 变更导致的 TypeError、`from_dict` 解析错误等全部被静默吞掉，图片一律降级为 1×1 占位符且无任何日志/诊断，问题几乎不可定位（渲染内容错误但无报错）。
  - 建议: 只捕获已知可降级异常（`AssetPreflightError`, `OSError`, `ValueError`）并至少记录 warning 级诊断；其余异常向上传播。
  - 置信度: 高

- [AUD-30301] [Major] `_append_validation_background` 用 `document.lower()` 的索引切片原文档，Unicode 大小写展开字符使插入位置偏移
  - 位置: src/features/teaching_animation/render/html_scene_renderer_v2.py:56
  - 证据:
    ```python
        lower = document.lower()
        closing_head = lower.rfind("</head>")
        if closing_head >= 0:
            return document[:closing_head] + style + document[closing_head:]
        return style + document
    ```
  - 影响: `str.lower()` 对部分字符会改变长度（如土耳其语 `İ` U+0130 → 2 个字符，已实测 `len('İ')==1, len('İ'.lower())==2`）。文档在 `</head>` 之前含此类字符时，`closing_head` 相对原文档偏移，`<style>` 被插进错误位置（可能插进标签/文本中间），验证探针页面损坏 → 合法场景被误判"HTML 验证捕获为空"而拒绝。
  - 建议: 用不改变长度的定位方式，如 `re.search(r"</head\s*>", document, re.IGNORECASE)` 直接在原文档上取 `match.start()`；下文 AUD-30311 同根因。
  - 置信度: 高（机制确证；触发依赖文档含大小写展开字符，实际频率中等）

- [AUD-30302] [Suggestion] `render()` 每帧调用 `inspect.signature` 反射探测服务签名
  - 位置: src/features/teaching_animation/render/html_scene_renderer_v2.py:306
  - 证据:
    ```python
        try:
            params = inspect.signature(self._service.render_frame).parameters
        except (TypeError, ValueError):
            params = {}
        if "allow_uniform" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        ):
            service_kwargs["allow_uniform"] = bool(getattr(context, "allow_uniform_frames", False))
    ```
  - 影响: 逐帧反射开销（每帧一次 `inspect.signature`），且 `HtmlRendererService.render_frame` 已固定含 `allow_uniform` 形参（html_renderer_service.py:137），探测实为恒真的兼容层，徒增热路径成本与分支复杂度。
  - 建议: 在 `__init__`/模块级缓存一次签名判定，或直接传参并删除探测。
  - 置信度: 高

- [AUD-30303] [Suggestion] 验证背景色未校验即内插进 `<style>`
  - 位置: src/features/teaching_animation/render/html_scene_renderer_v2.py:52
  - 证据:
    ```python
        style = (
            "<style id=\"topos-html-validation-background\">"
            f"html,body{{background:{background} !important;}}"
            "</style>"
        )
    ```
  - 影响: `background` 来自 `context.html_validation_background`，可经 `RenderContext.from_dict` 从持久化数据构造（contracts.py:385-388 仅转 str 不校验）。含 `</style><script>…` 的值可逃逸 style 上下文向捕获文档注入标记（虽为受控渲染进程，仍违反"外部数据须验证"）。当前生产调用仅传 "#101827"（html_validation.py:245），风险低。
  - 建议: 用正则限定为颜色字面量（`#[0-9a-fA-F]{3,8}` 或命名色集合），不匹配即忽略。
  - 置信度: 中

### 通过项
- 线程安全：无共享可变状态；session 为冻结 dataclass；浏览器池生命周期归 `HtmlRendererService` 所有
- GPU/浏览器资源生命周期：`HtmlRendererSession.close()` 幂等（`object.__setattr__`），fail-closed——`CaptureFailure` 一律抛 `HtmlRenderError`，绝不把空白/陈旧帧当成功返回
- 异常与取消路径：`_failure_to_code` 覆盖全部 `CaptureFailureCode` 枚举（含 default）；`CANCELLED`/`TIMEOUT` 正确标记 `recoverable`；非 `CancelToken` 入参安全回退 None
- GLSL/HDR：本文件为 rgba8 浏览器捕获路径，无 GLSL、无 HDR clamp 问题
- 输入验证：`detect_page_count` 恒 `max(1,…)`（html_page_deck.py:379/385），第 218 行除以 page_count 无除零；`FrameContext.fps` 属性存在（contracts.py:353-355）
- 状态提交一致性：不涉及 undo/redo/持久化
- 信号防递归：无 Qt 信号交互

---

## src/features/teaching_animation/render/html_validation.py (行数: 259)

### 发现

- [AUD-30304] [Major] 以异常消息子串（"uniform frame"/"near-uniform"）分类空白捕获，脆弱的字符串契约
  - 位置: src/features/teaching_animation/render/html_validation.py:190
  - 证据:
    ```python
        try:
            rendered = render_frame(scene.scene_id, frame, context=probe_context)
        except Exception as exc:  # noqa: BLE001 - classify only blank captures
            if "uniform frame" not in str(exc).lower() and "near-uniform" not in str(exc).lower():
                raise
            last_uniform = exc
            return None
    ```
  - 影响: 空白帧重试的判定完全依赖后端异常的英文措辞。消息一旦改写/本地化（或异常被包装），合法的 uniform-frame 诊断会直接 raise，验证门把可重试的空白误判为硬错误，场景被错误拒绝；反向地，任何无关异常若恰含该子串会被当作空白静默重试，掩盖真实故障。
  - 建议: 定义结构化异常类型（如 `UniformFrameError` 带 code 字段）或检查 `getattr(exc, "render_error", None)` 的 `RenderErrorCode`，替代子串匹配。
  - 置信度: 高

- [AUD-30305] [Suggestion] `scene.fps`/`scene.scene_id` 直接属性访问与同文件防御式 getattr 混用
  - 位置: src/features/teaching_animation/render/html_validation.py:38
  - 证据:
    ```python
                fallback_default_seconds=(
                    max(0.001, duration / max(0.001, float(scene.fps)))
                    / max(1, page_count)
                ),
    ...
        fps = max(1, round(float(getattr(scene, "fps", 24.0) or 24.0)))
    ```
  - 影响: 第 62 行用 `getattr(scene, "fps", 24.0)` 容错，第 38/43/154/169/189 行却直接 `scene.fps`、`scene.scene_id`。同管线的 router/渲染器显式支持 Mapping 形态场景（`scene_has_html_layer`、`_native_overlay_for_frame` 均处理 dict），dict 场景进入本函数会在 `float(scene.fps)` 处 AttributeError。当前生产调用（export_service.py:187、module.py:3602）均传 TeachingSceneDocument，故未触发。
  - 建议: 统一为 `getattr` 防御式访问或在签名处声明仅接受 TeachingSceneDocument 并加 isinstance 断言。
  - 置信度: 高（不一致确证；触发依赖 dict 场景入参）

- [AUD-30306] [Suggestion] `RenderFrame` 类型别名与实际回调签名不符且未被使用
  - 位置: src/features/teaching_animation/render/html_validation.py:19
  - 证据:
    ```python
    RenderFrame = Callable[[str, int], Any]
    ```
  - 影响: 实际回调是 `(scene_id, frame, *, context)`（第 169-171 行的 lambda 含 keyword-only 参数），别名 `Callable[[str, int], Any]` 描述错误且从未被 `validate_html_scene` 的注解引用（用的是 `Callable[..., Any]`），误导读者。
  - 建议: 修正为 `Callable[..., Any]` 并补 keyword-only 说明，或删除该别名。
  - 置信度: 高

### 通过项
- 性能模式：`captured[(frame, background)]` 缓存避免 `_attempt` 与 `_validate_page_probes` 重复捕获同一帧
- 宽泛 except 复核：`_candidate_frames`/`_page_probe_candidates` 的 `except Exception`（:59、:132）均有 noqa 注明"可修复的旧元数据"并回退确定性候选，不掩盖领域错误
- 输入验证：候选帧统一 `max(0, min(duration - 1, …))` 钳制，无越界帧号
- `replace(render_context, html_validation_background=…)` 合法（字段存在于 RenderContext，contracts.py:60 已验证）
- 线程安全 / GPU 生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性：无相关交互，通过

---

## src/features/teaching_animation/render/perf_slo.py (行数: 164)

### 发现

- [AUD-30307] [Suggestion] 冻结 dataclass `PerfBudget.limits` 持有可变 Mapping
  - 位置: src/features/teaching_animation/render/perf_slo.py:31
  - 证据:
    ```python
    @dataclass(frozen=True)
    class PerfBudget:
        """Maximum allowed latency per metric for one render profile."""

        profile: str                                 # draft | interactive | preview | final
        limits: Mapping[PerfMetric, float] = field(default_factory=dict)
    ```
  - 影响: `frozen=True` 只冻结字段绑定，调用方仍可突变传入的 dict，预算可被无意改动；`PerfGate.__init__` 虽 `dict(budgets)` 浅拷贝外层，内层 `limits` 仍共享引用。
  - 建议: 构造时包一层 `MappingProxyType` 或在 `__post_init__` 中转 `types.MappingProxyType(dict(self.limits))`。
  - 置信度: 高

### 通过项
- 纯数据/纯逻辑脚手架：无线程、无 GPU、无 IO、无信号交互
- `PerfViolation.exceeded_by` 对 EXPORT_FPS 方向处理正确（越低越差 → budget - measured）
- 预算缺失/无上限指标以 notes 或 skip 表达，不误报
- 类型注解完整

---

## src/features/teaching_animation/render/release_fixtures.py (行数: 166)

### 发现

- [AUD-30308] [Suggestion] 硬编码 `renderer_build="teaching-animation-svg/0.2"`，与上下文默认版本漂移
  - 位置: src/features/teaching_animation/render/release_fixtures.py:144
  - 证据:
    ```python
        evidence = tuple(
            RenderEvidence(
                renderer_id="teaching-svg",
                renderer_build="teaching-animation-svg/0.2",
                frame_hash=hashlib.sha256(frame.data).hexdigest(),
    ```
  - 影响: `RenderContext.renderer_version` 默认为 `"teaching-animation-svg/0.1"`（contracts.py:38），`SvgSceneRenderer` 自身无 renderer_build 属性（svg_renderer.py 无该属性，已验证），证据中的 0.2 为孤立硬编码。渲染器升级后此处不会跟随，清单（manifest）的 provenance 与真实构建脱钩，跨层一致性比对可能误判。
  - 建议: 从单一常量（如同处维护的 `RENDERER_BUILD`）派生，或让 `SvgSceneRenderer` 暴露 build 属性后引用之。
  - 置信度: 中

### 通过项
- 资源生命周期：`_render_once` 的 `renderer.cleanup()` 在 finally 中，prepare 失败也释放
- 状态一致性：`strict_consistent` 逐帧比较 frame/宽高/格式/字节，语义严格
- 确定性：fixtures 无机器本地媒体/浏览器缓存依赖；帧采样 (0, 3, 5) 固定
- S3 缺失资产 + 效果回退组合由 `strict_assets=False` 显式声明，与 preflight 语义一致

---

## src/features/teaching_animation/render/release_manifest.py (行数: 129)

### 发现

（无）

### 通过项
- 通过（无重大问题）：`manifest_hash` 对 `to_dict(include_hash=False)` 全字段排序序列化后 SHA-256，自洽且稳定
- `build_release_manifest`：evidence 为空时回退 `context.renderer_version`；renderer_ids/fallbacks 去重排序，确定性输出
- `compare_rendered_frame_sequences`：长度不等即 ValueError（fail-fast，符合"不允许静默错位比较"）
- asset/font hash 与 cache manifest 同源（cache_key_inputs 一致性）
- 类型注解完整；无线程/GPU/信号/持久化交互

---

## src/features/teaching_animation/render/render_worker.py (行数: 123)

### 发现

- [AUD-30309] [Critical] `_handles` 无界增长：完成 job 的句柄与全部帧数据永不释放，应用级 worker 内存泄漏可致 OOM
  - 位置: src/features/teaching_animation/render/render_worker.py:69
  - 证据:
    ```python
            job_id = f"render-{uuid.uuid4().hex}"
            token = CancellationToken()
            future = self._executor.submit(self._render, job_id, token, scene, frame_numbers, context, on_progress)
            handle = RenderHandle(job_id, future, token)
            self._handles[job_id] = handle
            return handle
    ...
        def shutdown(self, wait: bool = True) -> None:
            for handle in tuple(self._handles.values()):
                if not handle.done:
                    handle.cancel()
            self._executor.shutdown(wait=wait, cancel_futures=True)
            self._handles.clear()
    ```
  - 影响: 每次-submit 都向 `self._handles[job_id]` 追加且仅在 `shutdown()` 清除。完成的 `Future` 持有 `RenderResult.frames`——整个 job 的全部 RGBA 帧。而本类被应用级长生命周期对象使用：`TeachingSceneCacheCoordinator`（scene_cache.py:16，经 `get_shared_scene_cache_coordinator()` 进程级单例）每次 `schedule()` 都 submit 全场景帧（`range(scene.duration_frames)`）。一个 1280×720、10 秒的场景即约 240 帧 × 3.7MB ≈ 885MB 被 `_handles` 永久钉住（bounded 的 `SceneRenderCache` 上限 512 帧并不约束 handles）。多次调度后内存无界增长直至 OOM 崩溃。
  - 建议: 在 job 终态（future `done` 后）从 `_handles` 移除条目（如 future.add_done_callback 里删除，或由 `RenderHandle.result()` 消费后通知清理），并提供 `max_handles`/LRU 上限；`scene_cache.py` 侧替换旧句柄时同样应从 worker 侧摘除。
  - 置信度: 高

- [AUD-30310] [Suggestion] `_handles` 无锁共享可变；`render_sync` 回调注解与 `submit` 取消语义不一致
  - 位置: src/features/teaching_animation/render/render_worker.py:72
  - 证据:
    ```python
        def render_sync(self, scene: TeachingSceneDocument, frames: Iterable[int], context: RenderContext | None = None,
                        on_progress: Callable[[RenderProgress], None] | None = None) -> RenderResult:
            return self.submit(scene, frames, context, on_progress).result()
    ```
  - 影响: `submit()` 的注解为 `Callable[[RenderProgress], bool | None]`（truthy 返回即取消，:116-117），`render_sync` 声明为返回 `None`，同一回调两种契约易误用。另外 `self._handles` 可被多线程 submit/shutdown 并发读写，当前靠 GIL 的 C 级原子性兜底（`tuple(values())` 原子），属脆弱依赖。
  - 建议: 统一回调注解为 `Callable[[RenderProgress], bool | None]`；为 `_handles` 加 `threading.Lock`（与 30309 的清理一并处理）。
  - 置信度: 高（事实）；低（实际竞态触发概率）

### 通过项
- 异常与取消路径：每帧循环先查 `token.is_cancelled`；`on_progress` truthy 即 `token.cancel()`；`prepare`/渲染全程包在 try/finally，`renderer.cleanup()` 必然执行（含 prepare 阶段失败的资源释放，注释明确）
- 缓存一致性：`renderer_build` 参与 cache key，首帧捕获后确认 build 并重键（防渲染器升级后服务陈旧帧）；`SceneRenderCache` 自身 RLock 线程安全（cache.py:153 已验证）
- 线程模型：渲染全部在 executor 线程，无 UI 线程操作；`RenderHandle.cancel()` 先 token 后 future 的次序正确
- GLSL/HDR/信号防递归/持久化：无相关交互

---

## src/features/teaching_animation/render/scene_renderer.py (行数: 25)

### 发现

（无）

### 通过项
- 通过（无逻辑）：仅定义 `RenderCancelled` 与 `CancellationToken`（bool 标志，CPython 下原子读写，跨线程可见性由 GIL 保证），并 re-export `AnimationFrame/RenderContext/RenderedFrame/SceneRenderer` 五个契约类型

---

## src/features/teaching_animation/render/scene_renderer_router.py (行数: 428)

### 发现

- [AUD-30311] [Major] `_html_with_native_overlay` 以 `document.lower()` 的索引切片原文档，主渲染路径的插入偏移 bug
  - 位置: src/features/teaching_animation/render/scene_renderer_router.py:335
  - 证据:
    ```python
        closing_body = document.lower().rfind("</body>")
        if closing_body >= 0:
            return document[:closing_body] + overlay + document[closing_body:]
        return document + overlay
    ```
  - 影响: 与 AUD-30301 同根因：`str.lower()` 可使字符变长（`'İ'` → 2 字符，已实测）。`HtmlVectorSceneRendererV1Adapter.render_frame` 对每个含原生元素的 HTML 场景**每帧**调用本函数；文档 `</body>` 前存在大小写展开字符时，overlay `<div>` 被插到错误偏移（可落进标签内部破坏标记），产出错误帧或触发 fail-closed 报错——正常导出/预览路径上的条件性渲染错误。
  - 建议: 改用 `re.search(r"</body\s*>", document, re.IGNORECASE)` 在原文档上定位（长度不变），或 `document.casefold()` 同样不可靠——必须用正则 IGNORECASE 直接索引原文档。
  - 置信度: 高（机制确证；触发依赖文档含此类字符）

- [AUD-30312] [Suggestion] `HtmlVectorSceneRendererV1Adapter.render_frame` 复制基类的会话/取消检查与 RenderedFrame 重建逻辑
  - 位置: src/features/teaching_animation/render/scene_renderer_router.py:297
  - 证据:
    ```python
        def render_frame(self, frame: Any) -> Any:
            if self._session is None:
                raise RuntimeError("HtmlVectorSceneRendererV1Adapter.render_frame before prepare")
            if self._cancellation is not None and self._cancellation.is_cancelled:
                raise RenderCancelled("HTML/vector render cancelled before frame")
            ...
            result = self._v2.render(
                session,
                self._frame_context(int(frame.frame)),
                cancel_token=self._html_cancel_token,
            )
    ```
  - 影响: 与 `HtmlSceneRendererV1Adapter.render_frame`（:176-189）重复的错误消息、取消检查、`_v2.render` 调用形状；两处将来易失同步（错误字符串/检查次序漂移）。
  - 建议: 提取 `_render_session_html(session, frame)` 基类辅助，子类只负责注入 overlay 后的 html。
  - 置信度: 高

### 通过项
- 线程安全：进程级 `_shared_service` 由 `_shared_lock` 双检保护；`reset_shared_html_service` 在锁内置 None、锁外 close，避免持锁做 IO
- fail-closed 路由：backend 不可用抛结构化 `HtmlRenderError(UNSUPPORTED_BACKEND)` 而非静默降级 SVG（不丢 HTML 内容）；`backend_for_scene` 保证 HTML/SVG 帧缓存键分离
- 取消路径：`_LinkedCancelToken` 把 V1 CancellationToken 桥接到浏览器 token，捕获进行中亦可中断；V1 语义逐帧前置检查
- overlay 无叠加污染：每帧用 `dataclasses.replace(session, html=…)` 从原始 session 派生，不修改原 session（frozen），重复 render_frame 不会叠加多层 overlay
- GPU/浏览器资源：prepare/cleanup 对称（`cleanup` 置空 session 后 close）；`scene_asset_uri_lookup` 的宽泛 except 有 noqa 与降级说明
- 状态提交一致性 / GLSL·HDR / 信号防递归：无相关交互

---

## 审查方法备注

- 8 个文件全部 Read 通读；关键跨文件契约已实读验证：`RenderContext`/`FrameContext`/`RenderEvidence`/`RenderError`/`RendererResult`（core/contracts.py）、`PageDeckTiming.durations` 恒非空（html_page_deck.py:341-345）、`detect_page_count` 恒 ≥1、`CancelToken`/`CapturedFrame`/`CaptureEvidence`/`is_effectively_blank`（html_renderer_spike）、`SvgSceneRenderer.__init__(cancellation)`、`SceneRenderCache` 线程安全（cache.py）、`AssetResolver.preflight` 先置 `_result` 后 raise（asset_resolver.py:82-84）、`render_teaching_scene_frame` 签名（host_adapters.py:355）。
- lower() 长度变化以 `python3 -c "print(len('İ'), len('İ'.lower()))"` 实测（输出 1 2）。
- `RenderWorker` 的生产使用方（scene_cache.py 进程级协调器、export_service.py）已实读确认，为 AUD-30309 的严重度依据。
