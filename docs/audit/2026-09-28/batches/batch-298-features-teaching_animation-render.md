# 批次报告：batch-298 — src/features/teaching_animation/render（核心 8 文件）

- 任务区间：AUD-29800–AUD-29899
- 审查文件：`__init__.py`(85), `advanced.py`(128), `asset_resolver.py`(108), `benchmark.py`(621), `bitrate_probe.py`(146), `cache.py`(272), `composite_effect_bridge.py`(124), `consistency.py`(169)
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解
- 汇总：Critical 0 · Major 5 · Suggestion 10

---

## src/features/teaching_animation/render/__init__.py (行数: 85)

### 发现
- [AUD-29800] [Suggestion] `__all__` 遗漏 4 个已导入再导出的 HTML 渲染符号
  - 位置: src/features/teaching_animation/render/__init__.py:25-28
  - 证据:
    ```python
    from .html_renderer_service import HtmlRendererService
    from .html_scene_renderer_v2 import (
        HtmlRenderError, HtmlRendererSession, HtmlSceneRendererV2,
    )
    ```
    而 `__all__`（66-85 行）仅含 `"HtmlPreviewRenderer"`, `"HtmlSceneRendererV1Adapter"` 等，不含 `HtmlRendererService` / `HtmlRenderError` / `HtmlRendererSession` / `HtmlSceneRendererV2`。
  - 影响: `from ...render import *` 或依赖 `__all__` 的静态导出/公共 API 审计会漏掉 V2 HTML 渲染入口，调用方可能改用深层模块路径导入，破坏包的公共面约束。
  - 建议: 将这 4 个名字补入 `__all__`（或显式声明为私有再导出并加注释）。
  - 置信度: 高

### 通过项
- 纯 re-export 聚合模块，无逻辑、无可变状态；所有被导入符号（含 release_manifest/release_fixtures/perf_slo 等）经核实均存在
- 无线程/GPU/信号/持久化相关代码，各维度不适用

---

## src/features/teaching_animation/render/advanced.py (行数: 128)

### 发现
- [AUD-29801] [Major] `render_draw_svg` 对 `stroke_width` 直接 `float()`，非数值场景内容触发未捕获 ValueError 进入渲染线程
  - 位置: src/features/teaching_animation/render/advanced.py:96
  - 证据:
    ```python
    if len(points) < 2:
        return ""
    return f'<polyline points="{" ".join(points)}" fill="none" stroke="{escape(color, quote=True)}" stroke-width="{float(content.get("stroke_width", 3.0)):.6f}" stroke-linecap="round" stroke-linejoin="round"/>'
    ```
    调用方 svg_renderer.py:466-468 的 draw 分支无 try 保护（chart 分支 458-463 行有 `except (TypeError, ValueError, ...)`）：
    ```python
    if source.type == "draw":
        content = source.content if isinstance(source.content, Mapping) else {}
        color = _color(content.get("color", style.get("color", "#f59e0b")), "#f59e0b")
        return group_open + render_draw_svg(content, element.transform.width, element.transform.height, color=color) + "</g>"
    ```
  - 影响: 场景文档属外部序列化数据（铁律“外部数据须验证”）。`stroke_width: "abc"` 使 `float()` 抛 ValueError，沿 `render_frame` 冒泡到 `RenderWorker._render` 的 Future，单帧渲染失败；同文件对 `points` 已做逐点 try/except，此处防护不一致。
  - 建议: 在 `render_draw_svg` 内对 `stroke_width` 采用与 points 一致的容错转换（try/except 或限幅默认 3.0）。
  - 置信度: 高
- [AUD-29802] [Suggestion] 折线图负值数据点绘制在画布外（y 超出 height），与 bar 分支的零高钳制不一致
  - 位置: src/features/teaching_animation/render/advanced.py:69-79
  - 证据:
    ```python
    maximum = max(max(data.values), 0.0) or 1.0
    ...
    if chart_type == "line":
        points = " ".join(f"{((index + 0.5) * width / count):.6f},{(height - value / maximum * height):.6f}" ...)
    else:
        ...
            bar_height = max(0.0, value / maximum * height)
    ```
  - 影响: 全负/含负值数据时 `maximum=0.0→1.0`，负值点 y = height − value·height > height，折线整体画在视口外不可见；bar 分支则被 `max(0.0, …)` 钳为零高。同一数据两种图型行为不一致，产生“数据丢失”观感。
  - 建议: line 分支对 y 做与 bar 一致的钳制，或对含负值数据采用对称区间（min..max 归一化）。
  - 置信度: 高（行为推导自代码；`ChartData.__post_init__` 只校验 finite，不拒绝负值）

### 通过项
- 输入验证：ChartData 校验长度一致、非空、finite；from_json/from_csv 路径异常向上抛出由调用方（svg_renderer chart 分支）捕获
- SVG 输出注入防护：color/文本经 `escape(quote=True)`，labels 不进入输出
- 纯 CPU 无状态函数，线程安全不适用；无 GPU/GLSL/信号/持久化代码

---

## src/features/teaching_animation/render/asset_resolver.py (行数: 108)

### 发现
- [AUD-29803] [Major] `data_uri` 无大小/类型上限，任意大资产整文件读入并 base64 内联进 SVG
  - 位置: src/features/teaching_animation/render/asset_resolver.py:99-105
  - 证据:
    ```python
    def data_uri(self, asset: SceneAssetRef) -> str | None:
        path = self.resolve(asset.asset_id)
        if path is None:
            return None
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{encoded}"
    ```
    调用方 svg_renderer.py:447-448：`uri = resolver.data_uri(asset) if asset is not None else None`（仅按 `source.type == "image"` 选取 asset_ref，不校验 kind/大小）。
  - 影响: image 元素引用大文件（如误绑的视频资产）时，`read_bytes()` 峰值 = 文件大小，base64 再放大 ~1.33×，生成的 SVG 同量级膨胀，Qt 光栅化阶段进一步放大内存，可致渲染卡死/OOM。HTML 侧同类内联已设 `max_data_uri_bytes = 32_000`（html_renderer_spike/security.py:335），SVG 路径完全无限制，防护不对称。
  - 建议: 在 `data_uri`（或调用方）加入 kind 白名单 + 字节上限，超限回退占位符并与 HTML 预算对齐。
  - 置信度: 中（代码事实确定；实际触发取决于上层是否限制资产体积，本批次内未见任何限制）
- [AUD-29804] [Suggestion] `data_uri` 的 `read_bytes()` 无 OSError 处理，preflight 通过后的删除/权限竞态使异常冒泡
  - 位置: src/features/teaching_animation/render/asset_resolver.py:104
  - 证据:
    ```python
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"
    ```
  - 影响: preflight（is_file 检查，65 行）与 data_uri 读取之间存在 TOCTOU 窗口；文件被删或不可读时 OSError 沿渲染路径抛出，与 preflight 已建立“缺资产→占位符”的降级语义相悖（svg_renderer 447-453 行对 None 有占位符分支）。
  - 建议: 捕获 OSError 返回 None，走既有占位符路径。
  - 置信度: 中

### 通过项
- 路径逃逸防护完备：`resolve()` + `candidate != root and root not in candidate.parents` 双重判定，绝对路径、symlink（resolve 展开）、`..` 均被拦截且该 issue 恒为 blocking（63、88 行）
- 输入验证：空路径、asset_root 缺失、音频计划引用未知资产、字体路径逃逸/缺失均产生结构化 issue；strict_assets 语义（blocking 与否）正确，strict 下抛 AssetPreflightError 且错误信息聚合 blocking 项
- 音频计划一致性检查（71-77 行）防止“视频成功但音频静默丢失”
- 无状态锁需求低（`_result` 为最后一次 preflight 结果，实例按场景使用）

---

## src/features/teaching_animation/render/benchmark.py (行数: 621)

### 发现
- [AUD-29805] [Major] `_measure_gpu_effect` 中 ModernGL 上下文与纹理创建位于 try/finally 之外，早期失败泄漏 GL 资源
  - 位置: src/features/teaching_animation/render/benchmark.py:498-528
  - 证据:
    ```python
    try:
        import moderngl
        gl_context = moderngl.create_standalone_context()
    except (ImportError, ModuleNotFoundError, OSError, RuntimeError) as exc:
        return BenchmarkSegment("gpu_effect", "skipped", reason=f"ModernGL context unavailable: {exc}")
    source = gl_context.texture((32, 32), 4, dtype="f1")
    source.write(bytes([128, 64, 32, 255]) * (32 * 32))
    bridge = CompositeEffectBridge(gl_context)
    values: list[float] = []
    try:
        ...
    finally:
        bridge.cleanup()
        source.release()
        gl_context.release()
    ```
  - 影响: 504-506 行（texture 创建/write/bridge 构造）在第二个 try 之前；任何一步抛异常（如纹理创建失败、write 失败）时 `source` 与 `gl_context` 均不释放。GL 资源泄漏是本仓库审查红线（deferred-release 模式约束）；基准反复跑失败用例会累积泄漏。
  - 建议: 将 504-506 行移入带 finally 的 try 块（bridge/source/context 可空引用判断后释放）。
  - 置信度: 高
- [AUD-29806] [Suggestion] `_measure_export_encode` 的 av container 无 finally 关闭，encode 中途异常时未释放
  - 位置: src/features/teaching_animation/render/benchmark.py:560-576
  - 证据:
    ```python
    try:
        container = av.open(output, mode="w", format="mp4")
        stream = container.add_stream(...)
        ...
        for packet in stream.encode():
            container.mux(packet)
        container.close()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return BenchmarkSegment("export_encode", "skipped", reason=f"PyAV encode unavailable: {exc}")
    ```
  - 影响: `container.close()` 只在成功路径执行；encode/mux 中途抛异常时 container 未关闭（缓冲/封装器资源延迟回收）。写 BytesIO 无 fd 泄漏，但多次迭代失败场景下资源语义不完整，且与同文件其它 segment 的 finally-cleanup 模式不一致。
  - 建议: `finally: container.close()`（或 contextlib.closing）。
  - 置信度: 高
- [AUD-29807] [Suggestion] `run_benchmark` 未校验场景元素非空，`scene.elements[0]` 可 IndexError
  - 位置: src/features/teaching_animation/render/benchmark.py:400-403
  - 证据:
    ```python
    def _scene_with_single_element_change(scene: TeachingSceneDocument) -> TeachingSceneDocument:
        element = scene.elements[0]
        changed = replace(element, content=f"{element.content} / updated")
        return scene.with_element_updated(changed)
    ```
    `run_benchmark` 校验了 frames/iterations/尺寸（250-266 行）但不校验 `scene.elements`。
  - 影响: 空元素场景（文档模型未禁止）进入 `run_benchmark` 后在 294 行调用 `_scene_with_single_element_change` 抛 IndexError，报告无法生成且报错信息不指向真实原因。
  - 建议: `run_benchmark` 入口对空元素场景显式 `raise ValueError("benchmark scene requires at least one element")`。
  - 置信度: 高

### 通过项
- 异常与跳过语义：所有可选 segment（qt_rasterize/html_capture/gpu_effect/export_encode）以结构化 skipped+reason 报告环境缺失，不静默造假数据
- 确定性保障：`_record_hash` 跨迭代输出哈希不一致即抛 RuntimeError，防止基准报告掩盖非确定性渲染
- 资源清理：run_benchmark 的 worker.shutdown() 在 finally；html_capture 的 service.close() 在 finally；qt_rasterize 的 renderer.cleanup() 在 finally
- 输入验证：frames 区间、iterations、上下文尺寸匹配、segment 名称白名单均有显式校验
- 纯测量代码无 UI 信号，防递归维度不适用；不操作 HDR 数据本身

---

## src/features/teaching_animation/render/bitrate_probe.py (行数: 146)

### 发现
- [AUD-29808] [Major] 两处 ffprobe `subprocess.run` 均无 timeout，损坏文件可无限挂起，违反“绝不抛错/退化为 unknown”契约
  - 位置: src/features/teaching_animation/render/bitrate_probe.py:70-84, 122-131
  - 证据:
    ```python
    result = subprocess.run(
        [
            binary, "-v", "error",
            ...
            "-of", "json", str(video),
        ],
        check=False, capture_output=True, text=True,
    )
    payload = json.loads(result.stdout or "{}")
    ```
    123-128 行的 `-count_packets` 二次探测同样无 timeout。
  - 影响: 模块契约是“Never raises: acceptance reporting must degrade to 'unknown'”（60-65 行 docstring），被 cache/报告管线调用。病态或截断的视频文件存在让 ffprobe 长时间不退出的已知场景——挂起比异常更糟：整条验收/cache 管线停摆且无任何 skipped 信号。
  - 建议: 两个 `subprocess.run` 加 `timeout=`（如 30s）并捕获 `subprocess.TimeoutExpired` 一并归入 `return None`。
  - 置信度: 中（结构缺陷确定；实际挂起需病态输入）
- [AUD-29809] [Suggestion] `video.stat().st_size` 存在 TOCTOU 竞态，文件消失时 OSError 突破 Never-raises 契约
  - 位置: src/features/teaching_animation/render/bitrate_probe.py:140
  - 证据:
    ```python
    size_bytes=_int(fmt.get("size")) or (video.stat().st_size if video.is_file() else 0),
    ```
  - 影响: 68 行的 `is_file()` 与 140 行 `stat()` 之间文件被删除时抛 FileNotFoundError（OSError 子类），该行已位于函数 try 块（70-84 行）之外，异常直接冒泡，违反 60-65 行声明的契约。
  - 建议: 用 try/except OSError 包裹 stat，失败回退 0。
  - 置信度: 中（竞态窗口小，但契约明确禁止抛出）

### 通过项
- 解析健壮性：JSON 解析失败、streams 缺失、`r_frame_rate` 分母为 0（`max(1e-9, …)`）、非数值字段（_int/_float/_opt_int）均安全退化
- WebM/Matroska 无 nb_frames 时以 `-count_packets` 只解封装计数，避免全解码开销——性能意识良好
- 输入验证：ffprobe 缺失/文件不存在返回 None；bitrate_ratio 除零防护
- 类型注解完整；不触碰 GL/UI 线程

---

## src/features/teaching_animation/render/cache.py (行数: 272)

### 发现
- [AUD-29810] [Major] `build_cache_manifest` 每次调用整读并哈希全部资产文件，而渲染路径每帧调用（build 确认后最多两次）
  - 位置: src/features/teaching_animation/render/cache.py:17-18, 97-110
  - 证据:
    ```python
    def _file_hash(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    ...
    def build_cache_manifest(...):
        resolver = AssetResolver(context)
        result = resolver.preflight(scene)
        scene_payload = json.dumps(scene.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        asset_hashes = tuple(sorted((asset_id, _file_hash(path)) for asset_id, path in result.resolved.items()))
    ```
    调用方 render_worker.py:104-113（本批次外，已核实）每帧执行，且 build 确认变化时同帧再调一次：
    ```python
    manifest = build_cache_manifest(scene, frame_number, context, renderer_build=renderer_build)
    frame = self.cache.get(manifest)
    if frame is None:
        frame = renderer.render_frame(...)
        confirmed = getattr(renderer, "renderer_build", "") or ""
        if confirmed != manifest.renderer_build:
            manifest = build_cache_manifest(scene, frame_number, context, renderer_build=confirmed)
    ```
  - 影响: 每帧成本 = 全场景 preflight 文件系统探测 + 每个已解析资产**整文件** read_bytes+sha256 + 场景 JSON 序列化。N 帧导出对单个 100MB 资产即 ~N×100MB（最坏 2N×）纯冗余 I/O；每帧缓存查询（cache.get）前也要付全款，直接违背 4K 实时性能基准目标。这是缓存层的系统性性能模式缺陷。
  - 建议: 文件哈希按 (path, size, mtime_ns) 做 memoize（模块级或 context 级），或将 manifest 构建按 (scene_id, scene_hash) 每渲染任务构建一次、仅 frame/renderer_build 字段逐帧替换。
  - 置信度: 高
- [AUD-29811] [Suggestion] `SceneRenderCache` 淘汰为插入序 FIFO 而非 LRU：命中不提升、原地更新不续期
  - 位置: src/features/teaching_animation/render/cache.py:167-174
  - 证据:
    ```python
    def put(self, manifest: RenderCacheManifest, frame: RenderedFrame) -> None:
        with self._lock:
            if len(self._frames) >= self._max_entries and manifest.cache_key not in self._frames:
                oldest = next(iter(self._frames))
                self._frames.pop(oldest, None)
                self._manifests.pop(oldest, None)
            self._frames[manifest.cache_key] = frame
            self._manifests[manifest.cache_key] = manifest
    ```
  - 影响: `get` 命中不改变 dict 插入序，`put` 更新已有 key 也不移动位置；长 scrub 会话中频繁命中的热帧（如首帧/最近帧）会按插入序被淘汰，造成可避免的重复渲染抖动。
  - 建议: 命中/更新时 `move_to_end`（dict 序即可实现 LRU），或注明 FIFO 为有意设计。
  - 置信度: 高
- [AUD-29812] [Suggestion] `cache_key` 是无缓存的 property，单次 `put` 重复执行 json.dumps+sha256 最多 3 次
  - 位置: src/features/teaching_animation/render/cache.py:52-69, 158-174
  - 证据:
    ```python
    @property
    def cache_key(self) -> str:
        data = { ... "asset_hashes": self.asset_hashes, ... }
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    ...
    if len(self._frames) >= self._max_entries and manifest.cache_key not in self._frames:
        ...
    self._frames[manifest.cache_key] = frame
    self._manifests[manifest.cache_key] = manifest
    ```
  - 影响: 每次 get/put 对含全部资产哈希的字典重复序列化+哈希；在逐帧渲染热路径上叠加 CPU 开销（与 AUD-29810 同向放大）。
  - 建议: frozen dataclass 上用 `functools.cached_property` 不可用时，可在构造时预计算 `cache_key` 字段，或在 put/get 内 `key = manifest.cache_key` 计算一次复用。
  - 置信度: 高

### 通过项
- 线程安全：所有可变状态（frames/manifests/计数器）均在 `threading.RLock` 内读写，`cached_frames_for_scene`/`nearest_frame_for_scene` 快照读取也在锁内
- 缓存键完整性：scene_hash/renderer_version/renderer_build/backend/frame/尺寸/dpr/asset_hashes/font_hashes/font_set_hash/declared_asset_ids 全部参与键，覆盖 renderer 升级、字体集变更、DPR 差异与“未声明资产后补”投毒场景（W2 D-4），注释与实现一致
- `invalidate_frame_range` 半开区间与负数校验；`max_entries` 正数校验；`is_range_cached` 空区间返回 False
- `nearest_frame_for_scene` 用存储键而非重建键，规避 renderer_build 不可重建导致的必然 miss——设计正确

---

## src/features/teaching_animation/render/composite_effect_bridge.py (行数: 124)

### 通过项
- GPU 资源生命周期：scratch 纹理与程序所有权显式委托给 `EffectPassRunner`（其 `release()`/`get_surface_diagnostics()` 已核实存在），`cleanup()` 在持有线程释放并置 None；懒初始化 `_ensure_runner` 限定单 GL 线程调用（docstring 声明 MotionGraphicNode 渲染线程独占），无跨线程 GL 对象外泄
- HDR/精度正确性：`dtype_for_color_pipeline` 映射（≥32bit→f4，HDR 或 >8bit→f2，SDR8→f1）与 EffectPassRunner 的 f1/f2 语义一致，避免 FX-M01 类 8bit 量化截断；效果链中间面按 dtype 分配
- 异常路径：`current <= 0 or not declarations` 提前返回；目录缺失条目 `continue` 不中断链；与 runner “着色器失败回传输入纹理”的失败策略一致，不在渲染线程抛错
- motion_blur→blur 的参数适配（blur_type=motion）正确
- 类型注解基本完整（gl_context/runner 为 Any 属跨模块边界的有意宽松）

---

## src/features/teaching_animation/render/consistency.py (行数: 169)

### 发现
- [AUD-29813] [Suggestion] 像素比较为纯 Python 逐字节双重遍历，4K 单帧对比约 3300 万次解释器级循环
  - 位置: src/features/teaching_animation/render/consistency.py:83-92, 99-103
  - 证据:
    ```python
    for pixel in range(a.width * a.height):
        base = pixel * 4
        for channel in range(3):
            diff = abs(data_a[base + channel] - data_b[base + channel])
            total += diff
            if diff > max_diff:
                max_diff = diff
        count += 3
    ```
    `_max_alpha_diff` 再做一次全像素遍历。
  - 影响: 3840×2160 帧：RGB 遍历 ~24.9M 次内层迭代 + alpha 8.3M 次，纯 Python 下单帧对比以十秒计；两遍可合一遍。模块 docstring 声明“无 PIL/numpy 依赖”是有意取舍，但在本仓库 4K 性能语境下，验收管线批量化对比（AQ-04/05 多帧 golden）会成为明显瓶颈。
  - 建议: 单遍循环同时累计 RGB 与 alpha；并在 numpy 可用时走向量化快路径（可选加速，保留纯 Python 回退）。
  - 置信度: 高（量级估算确定；属文档化取舍，故定为 Suggestion）
- [AUD-29814] [Suggestion] `golden_evidence` 返回未参数化的 `dict`；`field` 导入未使用
  - 位置: src/features/teaching_animation/render/consistency.py:21, 152-153
  - 证据:
    ```python
    from dataclasses import dataclass, field
    ...
    def golden_evidence(snapshot: FrameSnapshot, *, frame: int, label: str) -> dict:
    ```
  - 影响: 类型注解缺失（Suggestion 级维度）：调用方/类型检查器无法得知证据记录结构；`field` 为死导入。
  - 建议: 返回类型改为 `dict[str, Any]`；删除未使用的 `field` 导入。
  - 置信度: 高

### 通过项
- 输入验证：`FrameSnapshot.__post_init__` 校验尺寸为正且 data 长度恰为 w*h*4，杜绝越界读
- 比较逻辑：维度不匹配短路并报告；strict 模式字节级比较 + 双 sha256 证据；perceptual 模式容差判断清晰，RGB 与 alpha 差异分离报告（alpha_mismatch 独立 code）
- 冻结 dataclass 全程不可变，无共享状态，线程安全
- 不操作 GL/UI/持久化，各维度不适用

---

## 汇总统计

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 5 | AUD-29801, AUD-29803, AUD-29805, AUD-29808, AUD-29810 |
| Suggestion | 10 | AUD-29800, AUD-29802, AUD-29804, AUD-29806, AUD-29807, AUD-29809, AUD-29811, AUD-29812, AUD-29813, AUD-29814 |
