# batch-279 审查报告 — src/features/teaching_animation/

批次：batch-279 [P2] 主题 src/features/teaching_animation//
编号区间：AUD-27900–AUD-27999
审查文件：export_service.py (566)、facade.py (302)、host_adapters.py (568)，共 3 个文件全部通读。
跨模块佐证（只读）：render/render_worker.py、render/scene_renderer_router.py、render/svg_renderer.py、render/html_scene_renderer_v2.py、render/frame_adapter.py、scene_cache.py、render/html_realtime_recorder.py。

---

## src/features/teaching_animation/export_service.py (行数: 566)

### 发现

- [AUD-27900] [Critical] HTML 场景以 svg_sequence/html_sequence 导出无 pixel_format 分流：RGBA8 字节被写成 .svg（静默损坏）或 UTF-8 解码崩溃
  - 位置: src/features/teaching_animation/export_service.py:213-214, 497-502
  - 证据:
    ```python
    if format_name in {"svg_sequence", "html_sequence", "rgba_sequence"}:
        self._write_sequence(output_path, format_name, result, context, scene)
    ...
    if format_name == "svg_sequence":
        (stem.with_suffix(".svg")).write_bytes(frame.data)
    elif format_name == "html_sequence":
        svg = frame.data.decode("utf-8")
    ```
    而 HTML 后端产出的帧是像素字节：render/scene_renderer_router.py:188 注释 "RendererResult.frame is already a RenderedFrame (pixel_format rgba8)"（HtmlVectorSceneRendererV1Adapter 第 317-321 行亦透传 rgba8）；SVG 后端才产出 `pixel_format="svg"` 的 UTF-8 文本（render/svg_renderer.py render_frame 末尾）。`export()` 对 sequence 格式没有任何 backend/pixel_format 校验。
  - 影响: HTML Motion Graphic 场景用默认 `ExportOptions.format="svg_sequence"` 导出时，每帧原始 RGBA 字节以 .svg 扩展名写盘，导出"成功"、manifest 正常、交付文件全部损坏（数据损坏且无诊断）；选 html_sequence 则 `decode("utf-8")` 抛 UnicodeDecodeError 崩溃。违反"外部交付必须可验证"的边界要求。
  - 建议: 在 `export()` 或 `_write_sequence()` 按帧 `pixel_format` 分流：svg_sequence/html_sequence 仅接受 `pixel_format == "svg"`；HTML 后端收到这两个格式时抛 `TeachingExportError`（提示改用 rgba_sequence/视频格式），或自动降级到 rgba_sequence 并在 manifest 记录。
  - 置信度: 高

- [AUD-27901] [Major] _write_video 先写满 stdin 再读 stderr：FFmpeg stats 输出写满管道缓冲后必然死锁
  - 位置: src/features/teaching_animation/export_service.py:536-545
  - 证据:
    ```python
    command = [ffmpeg, "-y", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{context.width}x{context.height}", "-r", str(context.fps), "-i", "-", "-frames:v", str(len(result.frames)), *codec, str(output)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    ...
    for frame in result.frames:
        process.stdin.write(FrameAdapter.from_rendered_frame(frame).data)
    process.stdin.close()
    stderr = process.stderr.read() if process.stderr is not None else b""
    code = process.wait()
    ```
    命令行未加 `-nostats`/`-loglevel error`，FFmpeg 默认向 stderr 持续输出进度行；stderr 只在 stdin 全部写完之后才被读取。
  - 影响: 长序列（数千帧 × 每帧约 70 字节 stats，远超 64KB 管道缓冲）时 ffmpeg 阻塞在 stderr 写、父进程阻塞在 stdin 写，导出永久挂死且无超时；4K 长时长交付最容易触发。
  - 建议: 用 `process.communicate(input=all_bytes)` 一次完成读写（注意内存），或起一个后台线程持续排空 stderr；或命令行加 `-nostats -loglevel error` 并保留尾部 stderr 用于报错。
  - 置信度: 高

- [AUD-27902] [Suggestion] FFmpeg 子进程句柄未确定性关闭：成功路径不关 stderr，异常路径 kill 后 stdin/stderr 均不关
  - 位置: src/features/teaching_animation/export_service.py:537-549
  - 证据:
    ```python
    process.stdin.close()
    stderr = process.stderr.read() if process.stderr is not None else b""
    code = process.wait()
    except Exception:
        process.kill()
        process.wait()
        raise
    ```
  - 影响: 文件描述符依赖 GC/Popen 析构释放；批处理多次导出时 fd 回收时机不确定，异常路径下泄漏窗口更长。
  - 建议: 用 `contextlib.closing`/finally 显式 `stdin.close()`、`stderr.close()`（可与 AUD-27901 的 communicate 改造合并解决）。
  - 置信度: 高

- [AUD-27903] [Suggestion] _realtime_progress 的 fps 参数从未使用
  - 位置: src/features/teaching_animation/export_service.py:57-61
  - 证据:
    ```python
    def _realtime_progress(completed: int, total: int, fps: float) -> Any:
        """Adapt recorder progress to the export worker's progress contract."""
        from .render.render_worker import RenderProgress
        frame = max(0, min(max(0, int(total) - 1), int(completed) - 1))
        return RenderProgress("realtime-record", int(completed), int(total), frame)
    ```
  - 影响: 死参数（调用点第 306 行还专门传入 `scene.fps`），误导读者以为 fps 参与换算。
  - 建议: 删除 fps 参数，或如未来需要按 fps 换算时间戳再引入。
  - 置信度: 高

- [AUD-27904] [Suggestion] provenance 两处宽泛 `except Exception` 静默吞错，交付 manifest 可能缺失 content_hashes 而无任何记录
  - 位置: src/features/teaching_animation/export_service.py:82-89, 460-468
  - 证据:
    ```python
    try:
        from .render.scene_renderer_router import get_shared_html_service
        return get_shared_html_service().backend_build
    except Exception:
        return ""
    ...
    try:
        from .render.cache import build_cache_manifest
        manifest = build_cache_manifest(scene, 0, context, renderer_build=build)
        ...
    except Exception:  # noqa: BLE001 - provenance is best-effort
        pass
    ```
  - 影响: manifest 的定位是"可审计、可复现到字节"（release gate §7.2），但哈希构建失败时静默产出缺少 `content_hashes` 的 manifest，审计方无法区分"没有资产"与"哈希失败"；违反"不得宽泛 except 掩盖领域错误"的精神（虽有注释声明 best-effort）。
  - 建议: 吞错时在 manifest 写入 `content_hashes_error: str(exc)`（截断）而非完全无痕，至少保留诊断线索。
  - 置信度: 高（模式确凿；是否算缺陷取决于 best-effort 约定，故仅 Suggestion）

- [AUD-27905] [Suggestion] _effective_html_renderer_build 读取共享单例的 backend_build，并发导出时可能把别的导出的 Chromium build 写进 manifest
  - 位置: src/features/teaching_animation/export_service.py:76-89
  - 证据:
    ```python
    if getattr(context, "backend", "") != "html":
        return ""
    try:
        from .render.scene_renderer_router import get_shared_html_service
        return get_shared_html_service().backend_build
    except Exception:
        return ""
    ```
  - 影响: 层上未 stamp renderer_build 时（注释自己承认存在该情况，第 473-476 行），build 取自全局共享 HTML service；两个导出交错进行时 A 的 manifest 可能记录 B 的捕获所确认的 build，provenance 失真。
  - 建议: 优先要求层在渲染后回写 renderer_build；或在渲染完成后立刻从本次会话读取 build 并传入 `_provenance`，而不是导出尾声再查全局单例。
  - 置信度: 中（并发导出是否为受支持场景未证实）

- [AUD-27906] [Suggestion] width/height 选项未验证为正整数：0 被静默替换为场景尺寸，负值一路透传
  - 位置: src/features/teaching_animation/export_service.py:396-397
  - 证据:
    ```python
    width=options.width or scene.width,
    height=options.height or scene.height,
    ```
    fps 有显式一致性校验（第 171 行），尺寸没有等价校验。
  - 影响: `width=0` 静默回退（调用者无从得知实际交付尺寸），`width=-100` 直达渲染器与 ffmpeg `-s -100x...`，错误在很远的下游以难懂的形式报出。
  - 建议: preflight 中校验 `options.width/height` 为 None 或正整数，非法即 `TeachingExportError`。
  - 置信度: 高（缺校验确凿；实际触发依赖调用方）

- [AUD-27907] [Suggestion] 实时录制路径硬编码 `transparent=False`，静默丢弃用户的 transparent 选项
  - 位置: src/features/teaching_animation/export_service.py:29, 302
  - 证据:
    ```python
    transparent: bool = False          # ExportOptions（第 29 行）
    ...
    result = record_html_video(
        ...
        transparent=False,             # 第 302 行，无条件
    ```
  - 影响: 调用方设 `ExportOptions(transparent=True)` 并走 realtime 捕获时，交付文件不透明且无任何提示；与 frame_accurate 路径（`_context` 第 401 行尊重 transparent）行为不一致。
  - 建议: 透传 `transparent=options.transparent`（或在不支持的 delivery 组合上显式报错/写 manifest 标记）。
  - 置信度: 中（可能是有意为之——需对照 recorder 的 alpha 支持范围）

### 通过项
- 信号防递归：无 Qt 控件程序化更新，不适用
- 状态提交一致性：导出为只读交付路径，不触碰 undo/revision；manifest 与 sidecar 写入时机一致
- 取消路径：非实时路径 cancelled 时在写盘前提前返回（第 211-212 行），不产生半成品交付文件；realtime 路径同样先判 cancelled（第 319-322 行）
- HDR 处理：无 GLSL；HDR 场景显式记录 `explicit_8bit_fallback`/`svg_8bit_tonemap` 于 provenance（第 443-449 行），无隐藏 clamp
- 线程安全：facade 用法下每次导出新建独立 service/worker 并在 finally 中 close；service 自身无可变共享状态
- 异常转译：validate/preflight/asset 失败统一转 `TeachingExportError`，FFmpeg 非零退出带尾部 stderr 诊断（第 550-553 行）
- 输入验证（部分）：fps 覆盖一致性校验（第 171 行）、HTML 导出前 fail-closed 质量门 + 消毒器定点复扫（第 122-166 行）、realtime 格式白名单（第 200-208 行）——尺寸项见 AUD-27906
- 性能模式：realtime 路径复用持久页面录制避免逐帧冷启动；缓存键经 build_cache_manifest 与渲染端一致
- 类型注解：公开函数/方法注解完整

---

## src/features/teaching_animation/facade.py (行数: 302)

### 发现

- [AUD-27910] [Major] export_scene_document 只捕获 (TypeError, ValueError, OSError)，HTML 渲染失败的 HtmlRenderError(RuntimeError) 会以原始异常逃出 facade
  - 位置: src/features/teaching_animation/facade.py:272-283
  - 证据:
    ```python
    try:
        from .export_service import ExportOptions, TeachingAnimationExportService
        service = TeachingAnimationExportService()
        try:
            result = service.export(scene, output, options=ExportOptions(**dict(options or {})))
        finally:
            service.close()
        return FacadeResult(ok=True, value=result)
    except (TypeError, ValueError, OSError) as exc:
        return FacadeResult(ok=False, error=RenderError(code=RenderErrorCode.RENDER_FAILED, message=str(exc), recoverable=True))
    ```
    `export()` → `worker.render_sync()` → `future.result()` 会原样重抛渲染线程异常；HTML 捕获失败抛 `HtmlRenderError`（render/html_scene_renderer_v2.py:63 `class HtmlRenderError(RuntimeError)`，第 186/258/316 行多处 raise）。同文件的 `preview_frame`（第 200-206 行）专门把 HtmlRenderError 转成结构化 RenderError，导出边界却没有对应处理。
  - 影响: HTML 场景导出中浏览器/捕获故障以未结构化 RuntimeError 逃出 facade，违反模块 docstring 承诺的"callers receive explicit unavailable/fallback results instead of ... raising an incidental"边界契约；宿主（AI/Edit）拿到无法按 RenderError 处理的裸异常。
  - 建议: 在 except 链中先判 `isinstance(exc, HtmlRenderError): return FacadeResult(ok=False, error=exc.render_error)`（与 preview_frame 对齐），再评估是否统一兜底 RuntimeError。
  - 置信度: 高

- [AUD-27911] [Major] preview_frame 的 renderer.prepare 位于 try/finally 之外，prepare 中途抛错时浏览器会话泄漏
  - 位置: src/features/teaching_animation/facade.py:184-189
  - 证据:
    ```python
    renderer = select_scene_renderer(scene)
    renderer.prepare(scene, context)
    try:
        return FacadeResult(ok=True, value=renderer.render_frame(AnimationFrame(frame, context.fps)))
    finally:
        renderer.cleanup()
    ```
    渲染线程自己的实现（render/render_worker.py:89-93）明确要求 "Keep cleanup in the same finally block as prepare: asset preflight or browser session setup can fail before the first frame, and a renderer must still release any partial resources"。且 `HtmlVectorSceneRendererV1Adapter.prepare` 在 `super().prepare()`（已建立浏览器 session）之后仍有可抛语句（scene_renderer_router.py:245-260 的 `scene_asset_uri_lookup` 之外的 `read_hierarchy`/`read_scene_tokens`）。
  - 影响: prepare 分配浏览器会话后抛错 → cleanup 永不执行 → Playwright 会话/浏览器进程泄漏；此路径每次预览帧都走一次，泄漏会累积。
  - 建议: 把 `renderer.prepare(...)` 移入 try 块（与 render_worker._render 的结构一致）。
  - 置信度: 高

- [AUD-27912] [Suggestion] insert_into_timeline 的 mode 参数无白名单校验，未知值静默降级为普通插入
  - 位置: src/features/teaching_animation/facade.py:241
  - 证据:
    ```python
    mode=str(values.pop("mode", "append")),
    ```
    而 MotionGraphicClipCommand.apply 只识别 `"append"`（第 189 行）与 `"overwrite"`（第 204 行），其余值（如 `"Overwrite"`、`"replace"`）落入隐式插入语义。
  - 影响: 拼写大小写错误不报错，clip 意外叠在已有素材上，用户难以察觉。
  - 建议: 在 facade 或 command 构造处校验 `mode in {"append", "overwrite"}`，非法返回 INVALID_SCENE/RENDER_FAILED。
  - 置信度: 高

- [AUD-27913] [Suggestion] insert_into_timeline 把 AttributeError/KeyError 一律归为"可恢复 RENDER_FAILED"，可能掩盖宿主接线错误
  - 位置: src/features/teaching_animation/facade.py:244-246
  - 证据:
    ```python
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        return FacadeResult(ok=False, error=RenderError(code=RenderErrorCode.RENDER_FAILED,
                                                        message=str(exc), recoverable=True))
    ```
  - 影响: 宿主传入的 timeline 对象缺少预期方法/属性（集成期典型 bug）被降级为"渲染失败、可恢复"，诊断信息只剩 `str(AttributeError)`，违反"不得宽泛 except 掩盖领域错误"的精神。
  - 建议: 至少将 AttributeError/KeyError 归类为单独错误码或打日志保留堆栈，便于区分"场景数据问题"与"宿主 API 误用"。
  - 置信度: 中

### 通过项
- 线程安全：模块无可变共享状态，全部为无状态函数
- 信号防递归：无 Qt 信号，不适用
- 状态提交一致性：时间线插入走 MotionGraphicClipCommand 可 undo 命令；Phase 0 dict 兼容路径不污染 store（第 147 行直接透传）
- 异常路径（部分）：create_scene 的 from_dict/validate_scene 失败均转结构化 FacadeResult；preview_frame 对 HtmlRenderError 有显式映射（导出侧见 AUD-27910）
- 输入验证（部分）：scene 类型分支、scene_id/output 空值守卫（第 215-223、251-268 行）
- GPU/浏览器资源：preview_frame 正常路径 try/finally cleanup（异常窗口见 AUD-27911）
- 类型注解：全部公开函数注解完整，Protocol 契约清晰

---

## src/features/teaching_animation/host_adapters.py (行数: 568)

### 发现

- [AUD-27920] [Major] TimelineSceneFrameProvider.render_frame 的 prepare 同样位于 try/finally 之外，prepare 中途抛错时浏览器会话泄漏
  - 位置: src/features/teaching_animation/host_adapters.py:333-349
  - 证据:
    ```python
    renderer.prepare(scene, context)
    try:
        if rendered is None:
            rendered = renderer.render_frame(AnimationFrame(local, context.fps))
            confirmed_build = getattr(renderer, "renderer_build", "") or ""
            ...
    finally:
        renderer.cleanup()
    ```
    与 facade.py AUD-27911 同构；render/render_worker.py:89-93 的注释明确了"prepare 可能部分分配资源后失败"的契约，HtmlVectorSceneRendererV1Adapter.prepare 在会话建立后仍可抛（scene_renderer_router.py:245-260）。
  - 影响: 时间线播放/缩略图路径（每次 render_frame 调用）在 prepare 失败时泄漏浏览器会话；该 provider 是 Edit/Composite 的常规播放路径，泄漏频率最高。
  - 建议: prepare 移入 try 块，保证任何退出路径都执行 cleanup。
  - 置信度: 高

- [AUD-27921] [Suggestion] undo→redo 循环中自建 track_id 不稳定：clip_id 有意复用而 track_id 每次 redo 重新生成
  - 位置: src/features/teaching_animation/host_adapters.py:181-186, 226-229, 191
  - 证据:
    ```python
    track_id = self.track_id or f"teaching_graphics_{uuid.uuid4().hex[:10]}"
    ...
    clip_id = self._clip_id or f"teaching_clip_{uuid.uuid4().hex}"     # redo 复用旧 id
    ...
    if self._created_track_id and sequence.get_track_by_id(self._created_track_id) is not None:
        ...
        sequence = sequence.with_track_removed(self._created_track_id)  # undo 删除自建轨道
    ```
  - 影响: `self.track_id is None` 时首次 apply 创建轨道并返回 track_id；undo 删除该轨道后 redo 会以全新 uuid 再建轨道——宿主若缓存了第一次返回的 track_id（GraphicsClipInsertResult.track_id），redo 后引用失效。与 clip_id 的稳定复用策略不一致。
  - 建议: 像 `_clip_id` 一样在命令内缓存 `_created_track_id` 对应的首次 id 并在 redo 时复用（轨道被外部删除时再回退到新建）。
  - 置信度: 中（取决于宿主是否跨 redo 缓存 track_id）

- [AUD-27922] [Suggestion] SceneReferenceManifest.from_dict 对损坏数据不设防，None/非数值直接抛原始 TypeError/ValueError
  - 位置: src/features/teaching_animation/host_adapters.py:76-87
  - 证据:
    ```python
    scene_schema_version=int(data.get("scene_schema_version", 1)),
    ...
    payload_revision=int(data.get("payload_revision", 0)),
    ...
    cache_id=data.get("cache_id"),
    ```
  - 影响: 该结构存于工程文件 `caption_graphics_extra`（第 119-129 行）并经持久化往返；手改/半迁移工程中键存在但值为 None 时，`int(None)` 抛 TypeError 从 `get_scene_reference_manifest` 一路冒出，违反"外部数据须验证"。
  - 建议: from_dict 做类型容错（None 视为缺省）或抛带上下文的 ValueError，由调用方决定降级为 None（视为无 manifest）。
  - 置信度: 中

- [AUD-27923] [Suggestion] render_frame 中 `if rendered is None` 恒为真（缓存命中已提前 return），死条件误导维护者
  - 位置: src/features/teaching_animation/host_adapters.py:328-339
  - 证据:
    ```python
    rendered = coordinator.cache.get(manifest)
    if rendered is not None:
        renderer.cleanup()
        return FrameAdapter.from_rendered_frame(rendered)

    renderer.prepare(scene, context)
    try:
        if rendered is None:          # 恒真：非 None 已在上分支返回
            rendered = renderer.render_frame(AnimationFrame(local, context.fps))
    ```
  - 影响: 无行为危害，但让读者误以为此处 rendered 可能非 None、缓存回填存在第三条路径，增加改动出错概率。
  - 建议: 删除该条件，直接渲染。
  - 置信度: 高

### 通过项
- 线程安全：模块级调度状态（_SCHEDULED_SCENE_FRAMES/_SCENE_FRAME_ATTEMPTS）全部由 _SCHEDULED_SCENE_FRAME_LOCK 保护，reset 与 schedule 均持锁；`pending.done` 属性真实存在（render/render_worker.py:42-44 RenderHandle.done）；锁内不获取其他锁，无死锁环
- GPU/浏览器资源（部分）：缓存命中路径对未 prepare 的 renderer 调用 cleanup 是安全的（SvgSceneRenderer.cleanup 仅置 None；HTML adapter cleanup 判 `session is not None`，scene_renderer_router.py:191-195）；provider.close() 正确退订 store，render_teaching_scene_frame 在 finally 中 close（prepare 异常窗口见 AUD-27920）
- 状态提交一致性（部分）：overwrite 移除的 clip 连同其 manifest 在 undo 中可逆恢复（第 221-225 行），`_removed_clips` 每次 apply 前清空保证 redo 语义正确；clip 的 render_cache_id 带场景 revision（第 201 行）
- 信号防递归：无 Qt 信号，SceneChangeBus.publish 以 tuple 快照遍历，迭代中订阅/退订安全
- 输入验证（部分）：motion_graphic_scene_id 对 None/空串/两种前缀容错；local_frame 的重定时映射公式边界（首末帧、timeline_span≥1、source_span≥0）推演正确
- 异常路径：render_teaching_scene_frame 未注册场景先 close 再抛 KeyError，无订阅泄漏；schedule_teaching_scene_frame 未注册场景直接 False 不入锁竞争
- 性能模式：render_frame 先建缓存键查命中，命中路径完全跳过 prepare/浏览器会话；首播帧写入与后台任务共享同一确定性缓存键
- 类型注解：公开类/函数注解完整

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-27900 |
| Major | 4 | AUD-27901, AUD-27910, AUD-27911, AUD-27920 |
| Suggestion | 11 | AUD-27902–27907, AUD-27912–27913, AUD-27921–27923 |

无行为争议的共性主题：prepare-outside-try 的资源泄漏模式在 facade 与 host_adapters 各一处（与 render_worker 自身契约相悖）；导出边界对 HTML 后端帧格式的假设未校验。
