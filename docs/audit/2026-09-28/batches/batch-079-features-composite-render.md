# batch-079 审查报告 — src/features/composite/render/

- 批次: batch-079 [P1]
- 编号区间: AUD-7900–AUD-7999（实际使用 AUD-7900–AUD-7922）
- 审查日期: 2026-09-28
- 统计: Critical 2 / Major 9 / Suggestion 12
- 审查方式: 逐文件全文 Read（长文件分段读完），跨模块核对调用方线程上下文（base_evaluator.py、module.py、composite_renderer.py、composite_export_renderer.py、gpu_resource_pool.py、cache_evaluator.py）

---

## src/features/composite/render/__init__.py (行数: 1)

### 发现

（无）

### 通过项
- 通过（无逻辑）：仅一行注释 `# Render layer components`，无任何导入或代码，不导出符号。

---

## src/features/composite/render/ai_cache_manager.py (行数: 268)

### 发现

- [AUD-7900] [Major] put() 在持锁状态下执行 EXR 写盘、全量 manifest 落盘与驱逐删文件，跨线程阻塞渲染路径
  - 位置: src/features/composite/render/ai_cache_manager.py:169
  - 证据:
    ```python
    with self._lock:
        # Save as EXR (lossless float32)
        ...
        cv2.imwrite(path, image.astype(np.float32),
                    [cv2.IMWRITE_EXR_COMPRESSION, 1])

        file_size = os.path.getsize(path) if os.path.exists(path) else 0
        ...
        self._save_manifest()
        self._evict_if_needed()
    ```
  - 影响: `self._lock` 为实例级 `threading.Lock`，与渲染线程（evaluator 缓存命中检查 `has_cache`/`get_cache_path`，见 base_evaluator.py 调用链）共享。4K float32 EXR 写入（数百 ms 级）+ 每次全量 `json.dump` + 驱逐同步 `os.remove` 全部发生在锁内，AI 结果落盘期间渲染线程的缓存查询被阻塞，造成交互卡顿（违反 60FPS 基准）。
  - 建议: 锁内只做 manifest 字典更新与条目登记；EXR 写盘、manifest 落盘、文件删除移到锁外（或写临时文件后 rename，manifest 用原子替换）。metadata 计算与 I/O 均可先在锁外完成。
  - 置信度: 中（锁竞争窗口依赖图像尺寸与调用频率，代码结构确定）

- [AUD-7901] [Major] manifest 整文件覆写 + 存在多实例并发使用，无跨实例同步导致丢更新
  - 位置: src/features/composite/render/ai_cache_manager.py:52
  - 证据:
    ```python
    def _save_manifest(self):
        """Save manifest to disk."""
        try:
            with open(self._manifest_path, 'w') as f:
                json.dump(self._manifest, f, indent=2)
        except IOError:
            pass
    ```
    且 manifest 仅在 `__init__` 时加载一次（第 41 行 `self._load_manifest()`）。消费方 module.py:71-73 每次回调新建实例：
    ```python
    def _save_to_cache(cache_key: str, result) -> None:
        cache_manager = AICacheManager()
        cache_manager.put(cache_key, result)
    ```
  - 影响: `_save_manifest` 以本实例内存中的 `_manifest` 整体覆写磁盘文件；`_lock` 只保护单实例。页面/渲染器持有的长寿命实例与新临时实例交错 put 时后写者覆盖前写者：磁盘 manifest 丢失条目（对应 EXR 成为永不回收的孤儿文件，磁盘泄漏），重启后该缓存 miss 触发重复计算。`IOError` 被静默吞掉还会掩盖写入失败。
  - 建议: 使用进程级共享锁（文件锁或模块级单例）；或 save 前重新读盘合并（read-modify-write 于临界区内）；IOError 至少记日志。
  - 置信度: 中（丢更新需要交错时序，但多实例设计确凿存在于 module.py）

- [AUD-7902] [Suggestion] put() 未检查 cv2.imwrite 返回值，写失败仍登记 manifest 并返回路径
  - 位置: src/features/composite/render/ai_cache_manager.py:175
  - 证据:
    ```python
    cv2.imwrite(path, image.astype(np.float32),
                [cv2.IMWRITE_EXR_COMPRESSION, 1])

    file_size = os.path.getsize(path) if os.path.exists(path) else 0
    ```
  - 影响: 磁盘满/权限问题时 `cv2.imwrite` 返回 False 而不抛异常；put 仍向 manifest 写入 size=0 的条目并返回路径，worker 据此 emit `finished`。虽然 `has_cache` 的 `os.path.exists` 能兜底 miss，但会造成“成功→查无→重算”的循环。
  - 建议: 检查 `cv2.imwrite` 返回值，失败时抛异常（由 worker error 路径上报）或返回 None 并跳过 manifest 登记。
  - 置信度: 高

- [AUD-7903] [Suggestion] manifest 落盘非原子，崩溃损坏后被静默清空，全部缓存文件成孤儿
  - 位置: src/features/composite/render/ai_cache_manager.py:46
  - 证据:
    ```python
    try:
        with open(self._manifest_path, 'r') as f:
            self._manifest = json.load(f)
    except (json.JSONDecodeError, IOError):
        self._manifest = {}
    ```
  - 影响: `_save_manifest` 直接以 `'w'` 截断写最终路径，进程在写入中途崩溃/断电会留下半截 JSON；下次启动 `_load_manifest` 捕获 JSONDecodeError 后静默置空——所有已缓存 EXR 不再被 manifest 覆盖，既不命中也永不驱逐，max_size_gb 上限形同虚设（磁盘泄漏）。
  - 建议: 写临时文件 + `os.replace` 原子替换；损坏时尝试扫描目录回收孤儿文件或至少告警。
  - 置信度: 高（机制确定，触发条件为崩溃时序）

- [AUD-7904] [Suggestion] _load_manifest 不校验外部 JSON 结构（外部数据未验证）
  - 位置: src/features/composite/render/ai_cache_manager.py:43
  - 证据:
    ```python
    if os.path.exists(self._manifest_path):
        try:
            with open(self._manifest_path, 'r') as f:
                self._manifest = json.load(f)
        except (json.JSONDecodeError, IOError):
            self._manifest = {}
    ```
  - 影响: manifest.json 是磁盘上的外部数据。若顶层是 list/str/int（手工编辑、其他工具写入），`json.load` 成功但后续 `self._manifest.get(...)`/`.items()` 在运行期抛 `AttributeError`/`TypeError`，且不在任何 try 内（`has_cache` 的锁内路径），异常直接冒泡到渲染线程。条目值非 dict 时 `entry.get("path")` 同样崩溃。
  - 建议: 加载后 `isinstance(self._manifest, dict)` 校验并过滤非 dict 条目。
  - 置信度: 高

- [AUD-7905] [Suggestion] 类型注解缺失/不精确
  - 位置: src/features/composite/render/ai_cache_manager.py:30
  - 证据:
    ```python
    def __init__(self, cache_dir: str = None, max_size_gb: float = 10.0):
    ```
    以及 `_load_manifest`/`_save_manifest`/`invalidate`/`invalidate_node`/`clear_all`/`_evict_if_needed` 均无参数与返回类型注解。
  - 影响: `cache_dir: str = None` 与实际语义（Optional[str]）不符，静态检查误报；项目规范要求强类型。
  - 建议: 改为 `Optional[str]`，补齐公有方法返回注解（`-> None`/`-> bool`/`-> int`）。
  - 置信度: 高

### 通过项
- compute_key 确定性与覆盖面良好：prompt/negative_prompt/seed/guidance/strength/尺寸/mode/model_path 全部入键，B6 补充 inpaint 族参数（model/use_tiling/tile_size/overlap/blend 仅在存在时追加），F6-03 的 frame/provider 后缀正确拼接；`json.dumps(..., sort_keys=True, default=str)` 保证可序列化。
- has_cache/get_cache_path 对 manifest 条目做了存在性复核（path 不存在视为 miss）。
- _evict_if_needed 仅在持锁路径调用（put 内），按时间戳 LRU 驱逐至 80% 容量，删除失败容错。
- EXR 写入显式指定 RLE 压缩规避 libdeflate SIGILL（T-P1-71 注释与实现一致）。
- invalidate/invalidate_node/clear_all 删除文件均有 OSError 容错，删除后统一 `_save_manifest`。
- get_cache_info 返回条目副本（dict(entry)），避免外部直接改内部状态。

---

## src/features/composite/render/ai_compute_manager.py (行数: 1275)

### 发现

- [AUD-7906] [Critical] _generate_preview 使用未导入的 cv2，默认参数下所有标准 AI 节点必然 NameError 失败
  - 位置: src/features/composite/render/ai_compute_manager.py:563
  - 证据:
    ```python
    image_small = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)
    mask_small = None
    if mask is not None:
        mask_small = cv2.resize(mask, (new_w, new_h), interpolation=cv2.INTER_NEAREST)
    ```
    模块级导入（23-29 行）只有 logging/os/traceback/numpy/threading/typing/PyQt6，无 cv2；函数内局部 `import cv2` 仅存在于 `_process_tiled`(604)/`_run_inference`(705)/`_postprocess`(740)/`_run_generate_inference`(779)/`_generate_placeholder`(904)/`_run_custom_generate_model`(959)，函数级导入不外溢。调用链（run() 163-164 行）：
    ```python
    if enable_preview and preview_scale < 1.0:
        self._generate_preview(image, mask, session, preview_scale)
    ```
  - 影响: `enable_preview` 默认 True、`preview_scale` 默认 0.5（run() 149-153 行取默认值），因此 ai_denoise/ai_inpaint/ai_remove 在默认参数下 100% 触发 `NameError: name 'cv2' is not defined`，被 run() 的 except 捕获后走 error 信号——AI 节点计算完全不可用（渲染错误级缺陷）。
  - 建议: 在模块顶部 `import cv2`（与 ai_cache_manager 一致），并移除各方法内的重复局部导入。
  - 置信度: 高

- [AUD-7907] [Critical] _prepare_outpaint_input 使用未导入的 cv2，ai_expand 节点必然 NameError 失败
  - 位置: src/features/composite/render/ai_compute_manager.py:241
  - 证据:
    ```python
    canvas = cv2.copyMakeBorder(
        image,
        expand_top,
        expand_bottom,
        expand_left,
        expand_right,
        cv2.BORDER_REFLECT_101
    )
    ```
    （252 行还有 `cv2.resize`。）本方法内无 `import cv2`，模块级亦无。调用链（run() 158-160 行）：
    ```python
    if self.node_type == "ai_expand":
        original_image = image.copy()
        image, mask, expand_params = self._prepare_outpaint_input(image, self.params)
    ```
  - 影响: ai_expand（outpaint）节点在任何参数下都在展开画布阶段抛 NameError → error 信号，功能完全不可用。
  - 建议: 同 AUD-7906，模块级导入 cv2。
  - 置信度: 高

- [AUD-7908] [Major] 自定义生成模型失败路径无终态信号，_workers 残留条目使该 cache_key 永久卡死
  - 位置: src/features/composite/render/ai_compute_manager.py:814
  - 证据:
    ```python
    result = self._run_custom_generate_model(model_path, prompt, width, height, seed)
    if result is None or self._cancelled:
        return
    ```
    而 `_run_custom_generate_model` 失败时静默返回 None（1047-1050 行）：
    ```python
    except Exception as e:
        logger.error(f"Custom model inference error: {e}")
        logger.debug("Stack trace:", exc_info=True)
        return None
    ```
  - 影响: result 为 None 时 run() 直接 return，不 emit error/finished/stale 中任何一个；`_on_worker_finished/_on_worker_error/_on_worker_stale` 均不触发，`self._workers[cache_key]` 永不清理（且 request_id 相同不会被 set_latest_request_id 清扫）。此后 `is_processing()` 恒为 True、`request_compute()` 恒返回 False，该节点永久停留在“计算中”直到重启或显式 cancel。
  - 建议: `_run_generate_inference` 中对 result None 走 `self.error.emit(...)`；或将 `_run_custom_generate_model` 的异常上抛交由 run() 统一 error 路径。
  - 置信度: 高（代码路径确定；触发依赖自定义模型失败）

- [AUD-7909] [Major] 并发上限驱逐的 worker 被静默丢弃，无任何终态信号反馈
  - 位置: src/features/composite/render/ai_compute_manager.py:1155
  - 证据:
    ```python
    while len(self._workers) >= self.MAX_CONCURRENT_COMPUTES:
        oldest_key = next(iter(self._workers))
        oldest = self._workers.pop(oldest_key)
        self._node_ids.pop(oldest_key, None)
        oldest.cancel()
    ```
  - 影响: 被驱逐 worker 的 run() 在各 `_cancelled` 检查处直接 return（143/166/176/815/829 行），不 emit 任何信号；`compute_failed/compute_stale` 均不发出，`_node_ids` 条目也已删除。UI（composite_page 的 `_on_ai_compute_finished/_on_ai_compute_failed`）对被驱逐的 cache_key 永远收不到回音，节点停留在计算/进度状态。行为语义与 `set_latest_request_id` 的“清扫后 cancel”不一致（后者同样不发信号但至少语义是过期）。
  - 建议: 驱逐时对 oldest_key emit `compute_stale`（或专用 `compute_dropped`），让消费方可重试或复位状态。
  - 置信度: 高（机制确定，UI 卡滞程度为中等评估）

- [AUD-7910] [Major] 取消以 RuntimeError 形式进入通用异常分支，被当作失败误报给 UI
  - 位置: src/features/composite/render/ai_compute_manager.py:506
  - 证据:
    ```python
    for provider in providers:
        if self._cancelled:
            raise RuntimeError("Cancelled")
    ```
    以及 `_process_tiled` 645-646 行 `raise RuntimeError("Cancelled")`；二者均被 run() 末尾的通用 except 捕获（196-199 行）：
    ```python
    except Exception as e:
        error_msg = f"{self.node_type} inference error: {str(e)}"
        traceback.print_exc()
        self.error.emit(self.cache_key, error_msg, self.request_id)
    ```
  - 影响: 用户主动取消（manager.cancel / cancel_all）会以 `compute_failed("... inference error: Cancelled")` 形式上报，UI 把取消显示为失败错误；且 traceback.print_exc() 向 stderr 刷取消堆栈。取消路径与错误路径语义混淆。
  - 建议: run() 的 except 中先判断 `self._cancelled`，是则走静默/stale 返回；或取消用专用异常类型并在 except 前置分支处理。
  - 置信度: 高

- [AUD-7911] [Major] _on_worker_stale 弹出 worker 后未 deleteLater，QThread 可能在仍在运行时被 Python GC 销毁（竞态崩溃）
  - 位置: src/features/composite/render/ai_compute_manager.py:1271
  - 证据:
    ```python
    def _on_worker_stale(self, cache_key: str, request_id: int):
        """Handle stale request."""
        with self._lock:
            self._workers.pop(cache_key, None)
        self.compute_stale.emit(cache_key)
    ```
    对比 `_on_worker_finished`（1236-1238 行）在 pop 后调用 `worker.deleteLater()`，`_on_worker_error`（1258-1260 行）同样调用。
  - 影响: `stale_detected` 从 worker 线程 emit（110 行），槽在管理器线程排队执行时 run() 可能尚未返回；pop 丢掉最后一个 Python 引用后 QThread 包装器（无 parent，Python 拥有所有权）被立即析构，若此刻线程仍在运行则触发 Qt 的 "QThread: Destroyed while thread is still running" 崩溃。窗口窄但属崩溃级风险，且与其他终态处理器的清理方式不一致。
  - 建议: 与其他处理器对齐，pop 后 `worker.deleteLater()`（局部变量持有引用直至槽返回）。
  - 置信度: 中（竞态窗口小，析构语义确定）

- [AUD-7912] [Major] cancel/cancel_all/cancel_stale_requests 持管理器锁同步 wait(1000-3000ms)×N，阻塞渲染线程的并发调用
  - 位置: src/features/composite/render/ai_compute_manager.py:1193
  - 证据:
    ```python
    with self._lock:
        worker = self._workers.get(cache_key)
        if worker:
            worker.cancel()
            worker.wait(3000)
            self._workers.pop(cache_key, None)
    ```
    `cancel_all`（1201-1209 行）在锁内对每个 worker `wait(3000)`；`cancel_stale_requests`（1213-1223 行）锁内 `wait(1000)`。
  - 影响: ONNX 单次推理不可中断（长任务数秒），wait 会实际等待。持锁等待期间（cancel_all 最坏 ~6s），渲染线程（base_evaluator 的 `request_compute`/`is_processing`/`is_request_stale`）全部阻塞，造成渲染停顿。
  - 建议: 锁内仅取 worker 引用并 pop，`cancel()` 与 `wait()` 在锁外执行（`set_latest_request_id` 已采用此模式，1103-1111 行，保持一致）。
  - 置信度: 高（代码结构确定；实际停顿时长取决于推理时长）

- [AUD-7913] [Major] ONNX session 缓存无上限、无失效，GPU 显存随模型切换累积
  - 位置: src/features/composite/render/ai_compute_manager.py:545
  - 证据:
    ```python
    # Cache
    AIComputeWorker._cache_mutex.lock()
    AIComputeWorker._session_cache[model_path] = session
    AIComputeWorker._cache_mutex.unlock()
    ```
    类属性 `_session_cache` 只增不减，唯一读取路径是命中返回（488-493 行），全文件无任何逐出/清理调用。
  - 影响: 每个 InferenceSession（CUDA/CoreML EP）常驻数百 MB 显存/内存；`model_path` 来自节点 params（467-469 行允许任意自定义路径），用户切换多个自定义模型时显存无界增长直至 OOM，且无释放途径（GPU 资源生命周期缺失）。
  - 建议: 为 session 缓存加容量上限与 LRU 逐出（逐出时调用 session 上可用的释放手段），或随 manager 生命周期统一清理。
  - 置信度: 中（无上限确定；OOM 取决于用户模型切换行为）

- [AUD-7914] [Major] tile/overlap 边界无输入验证：overlap>=tile_size 抛 ValueError；overlap==1 产生 NaN 权重黑缝
  - 位置: src/features/composite/render/ai_compute_manager.py:624
  - 证据:
    ```python
    stride = tile_size - overlap
    ...
    for y in range(0, h, stride):
    ```
    以及 613-615 行：
    ```python
    if overlap > 0:
        t = np.arange(overlap) / (overlap - 1)
    ```
  - 影响: overlap==tile_size 时 stride=0，`range(0, h, 0)` 抛 `ValueError: range() arg 3 must not be zero`（经 error 信号报错，非崩溃但功能失败）；overlap==1 时 `0/(1-1)` 得 NaN，Hanning 权重全 NaN → `output/np.maximum(weight,1e-5)` 为 NaN → `_postprocess` 的 nan_to_num 归零——大面积黑缝静默出现（渲染错误）。另 `_prepare_outpaint_input` 226-229 行 `int(params.get("expand_left", 0))` 接受负值，负 border 会使 copyMakeBorder 报错。
  - 建议: 在 run() 取参处 clamp/校验：`0 <= overlap < tile_size`（且 overlap>=2）、expand_* 非负；非法值记日志并回退默认。
  - 置信度: 高（数值机制确定；是否可从 UI 设到边界值未验证）

- [AUD-7915] [Suggestion] run() 中无使用的惰性导入 AIInferenceWorker
  - 位置: src/features/composite/render/ai_compute_manager.py:104
  - 证据:
    ```python
    # Import inference engine (lazy to avoid circular imports)
    from src.shared.ai_models.inference_engine import AIInferenceWorker
    ```
  - 影响: 导入后从未使用（推理实际由本类方法完成）；每次标准节点计算都付出该模块导入成本，且若该模块携带重依赖或加载失败会连累本不需要它的路径。
  - 建议: 删除该导入。
  - 置信度: 高

- [AUD-7916] [Suggestion] get_preview_result 读取 _workers 未持锁，与其余访问方式不一致
  - 位置: src/features/composite/render/ai_compute_manager.py:1118
  - 证据:
    ```python
    def get_preview_result(self, cache_key: str) -> Optional[np.ndarray]:
        """Get preview result for a cache key."""
        worker = self._workers.get(cache_key)
    ```
  - 影响: `_workers` 的所有其他读写都在 `self._lock` 内；此处裸读依赖 GIL 下 dict.get 的原子性才不出错，线程安全约定被破坏，后续改动易引入竞态。
  - 建议: 与 is_processing 一致加锁读取。
  - 置信度: 高

- [AUD-7917] [Suggestion] _on_worker_preview 写入 _preview_results 绕过 MAX_PREVIEW_RESULTS FIFO 上限
  - 位置: src/features/composite/render/ai_compute_manager.py:1251
  - 证据:
    ```python
    def _on_worker_preview(self, cache_key: str, preview: np.ndarray, request_id: int):
        with self._lock:
            if request_id < self._latest_request_id:
                return
            self._preview_results[cache_key] = preview
    ```
    上限裁剪仅存在于 `_on_worker_finished`（1235-1237 行）。
  - 影响: 两次 finished 之间多个不同 cache_key 的 preview 累积可超过 MAX_PREVIEW_RESULTS=8，B6 的内存上限注释约束被该路径绕过（长时间 scrub 场景短时超限，半分辨率 float32 每帧数 MB）。
  - 建议: 该方法写入时同样执行 FIFO 裁剪，或抽公共 `_store_preview()`。
  - 置信度: 高

- [AUD-7918] [Suggestion] 工作线程中 np.random.seed 污染进程级全局 RNG
  - 位置: src/features/composite/render/ai_compute_manager.py:926
  - 证据:
    ```python
    if seed >= 0:
        np.random.seed(seed)
    noise = np.random.rand(height, width, 3) * 0.3
    ```
    （`_run_custom_generate_model` 1014-1016 行同样用法。）
  - 影响: `np.random` 全局状态被后台线程重设，影响进程内其他依赖全局 RNG 的代码的可复现性；且 numpy 全局 RNG 非线程安全，并发 worker 下行为不确定。
  - 建议: 使用 `np.random.default_rng(seed)` 局部 Generator。
  - 置信度: 高

- [AUD-7919] [Suggestion] 类型注解缺失/含糊
  - 位置: src/features/composite/render/ai_compute_manager.py:88
  - 证据:
    ```python
    def set_stale_callback(self, callback):
        """Set callback to check if request is stale."""
        self._is_stale_callback = callback
    ```
    另：`_prepare_outpaint_input(...) -> tuple`（201-205 行）返回三元组未参数化；473/555 行字符串注解 `"ort.InferenceSession"` 引用的 `ort` 仅在函数体内导入，静态工具无法解析。
  - 影响: 违反项目强类型规范；callback 无签名约定，调用方易传错。
  - 建议: `callback: Callable[[int], bool]`；返回注解 `Tuple[np.ndarray, np.ndarray, dict]`；`ort` 用 `TYPE_CHECKING` 导入。
  - 置信度: 高

### 通过项
- 线程模型总体清晰：worker 为 QThread，manager 方法用 `self._lock` 保护共享字典；信号通过 Qt 队列跨线程回传；`request_compute` 的查重与登记在锁内原子完成。
- stale 检测链完整：`set_latest_request_id` 锁内清扫 + 锁外 cancel（注释明确说明原因），`is_request_stale` 供 worker 起点/manager 终点双侧复核。
- `_run_inference` 对 inpaint 族 4 通道输入的准备正确（BGR→RGB、mask 降维、clip、NCHW 连续内存），防御性处理 mask 为 3 通路的情形。
- `_postprocess`/生成路径对模型输出的 clip(0,1) 属于模型输出域约束（有理由，非违反 HDR 内部不 clamp 铁律——输入图像本就是 [0,1] BGR float32 约定）。
- `_process_tiled` 的 Hanning 窗 blending、边缘 tile 的 REFLECT_101 padding、除以 weight 累积归一化实现正确。
- `_blend_outpaint_result` 的 feather 梯度方向与 `_create_outpaint_feather_mask` 互为逆，混合公式 original*m + result*(1-m) 正确。
- `_generate_placeholder`/生成路径的空 prompt 校验（799-806 行）与 img2img 的尺寸匹配+strength 混合逻辑正确。
- GPU provider 探测失败逐级回退 CPU（506-542 行），异常有日志。
- run() 顶层 try/except 保证 worker 异常总能以 error 信号收尾（除 AUD-7908 所述 None 路径外）。

---

## src/features/composite/render/cache_manager.py (行数: 871)

### 发现

- [AUD-7920] [Suggestion] 逐条驱逐全量重扫 + 每轮 O(n) 内存汇总，驱逐复杂度 O(n²)
  - 位置: src/features/composite/render/cache_manager.py:671
  - 证据:
    ```python
    while self.estimated_memory_mb() > target_mb and iterations < max_iterations:
        iterations += 1
        evicted_this_round = False
        for category in eviction_order:
            # Find oldest non-pinned entry in this category
            for key in list(self._cache.keys()):
                ...
                    break  # Restart scan after eviction
    ```
    `estimated_memory_mb()`（814-819 行）每轮遍历全部条目；`list(self._cache.keys())` 每轮全量拷贝。`_evict_to_entry_count`（713-742 行）同构。`end_interaction`（596-597 行）恢复 512→128 预算时一次性触发大规模驱逐。
  - 影响: 交互模式结束时的同步驱逐在渲染线程上执行，最坏 ~O(n²)（n=512）量级的扫描与列表拷贝，造成拖拽结束瞬间数十毫秒级卡顿；常态 put 触发的驱逐也按此模式付出重复扫描成本。
  - 建议: 按类别维护待驱逐游标或一次性收集各类别条目排序后批量驱逐；`estimated_memory_mb` 用 `_category_memory` 汇总替代全量遍历（二者已同步维护）。
  - 置信度: 高（复杂度确定；卡顿幅度为估算）

- [AUD-7921] [Suggestion] _current_node_ids 为未在 __init__ 声明的动态属性，未设置时陈旧清理静默失效
  - 位置: src/features/composite/render/cache_manager.py:759
  - 证据:
    ```python
    current_nodes = getattr(self, '_current_node_ids', None)
    if current_nodes is not None:
        return node_id in current_nodes
    return True
    ```
    属性仅由 `set_current_nodes`（764-766 行）注入，`__init__`（150-170 行）未声明。
  - 影响: 忘记调用 `set_current_nodes` 时 Step-1 陈旧条目清理整体退化为 no-op（保守 True），已删除节点的缓存条目只能靠预算驱逐回收；依赖 getattr 默认值的隐式协议易在重构中断裂。
  - 建议: `__init__` 中显式 `self._current_node_ids: Optional[Set[str]] = None`，并在文档标注调用契约。
  - 置信度: 高

- [AUD-7922] [Suggestion] cache key 内嵌完整 upstream 元组字符串，键体量大且每次驱逐全串 split
  - 位置: src/features/composite/render/cache_manager.py:314
  - 证据:
    ```python
    return (f"{ns.node_type}:{ns.node_id}:{params_hash}:{upstream}:"
            f"{disabled_flag}:rm={render_mode}{color_suffix}{roi_suffix}"
            ...
    ```
    `_evict` Step-1 对每个 key 调用 `_is_for_current_graph` → `key.split(":")`（754 行）解析整条长键，而实际只需 parts[1]。
  - 影响: 多输入节点的键可达数百字符，512 条目 × 每次 put 的全量 split 带来可观的字符串分配成本与 dict 内存开销；解析只为取第二段却付出全文代价。
  - 建议: key 只需对 upstream 部分取 hash（如 md5 前 12 位，同 params_hash 做法）；或 CacheEntry 记录 node_id 字段供驱逐直接读取。
  - 置信度: 中（收益幅度依赖键长度分布，结构问题确定）

### 通过项
- 线程模型自洽：类文档明确 render-thread-only；核对到的调用方（composite_frame_renderer/composite_renderer/cache_evaluator/base_evaluator 均在渲染/导出线程侧，module.py 不触碰本类；导出渲染器自建独立 CacheManager+GPUResourcePool，见 composite_export_renderer.py:18 注释）符合该约定，无 UI 线程 GL 释放路径。
- compute_key 覆盖完整：参数、上游纹理元组（id/revision/format/bbox/premult/color_space/aov 签名）、disabled、render_mode、color 状态、ROI、输出格式、alpha 模式、proxy_scale、frame+remap 签名、LOD、scene/runtime 签名、quality_profile——防止跨模式/跨分辨率误复用（含 P0-2/P0-04 修复注释）。
- put 覆盖同 key 时先减旧类别内存并 `_release_entry` 释放旧纹理，无双重计数。
- 驱逐类别优先级（PREFETCH>PREVIEW>EXPORT>AI>EFFECT，SOURCE 保护）在内存与条数两条预算下一致实现；pinned 集合在两处循环与 retain_frame 中都被尊重。
- retain_frame 的帧级清扫正确豁免 SOURCE/pinned/当前帧条目（TL-C03 注释与实现一致）。
- `_release_entry` 以 `owned_texture_ids` 为权威所有权契约（含 depth/IDT 等辅助纹理），set 去重避免共享纹理双重释放，非法 id（非 int/<=0）容错跳过；与 Scene3D 自管理 AOV 的边界有注释说明。
- 交互模式 begin/end 幂等、备份恢复逻辑正确，end 后立即 `_evict` 回收拖拽残留。
- get() 的 LRU move_to_end、PREFETCH→EFFECT 提升的类别内存记账双向平衡。
- clear/clear_export_cache/clear_preview_cache/clear_prefetch_cache 均通过 pop+release 路径释放 GPU 纹理（cache_evaluator.py:190 注释强调不可裸 clear，实现遵守）。
- invalidate_node 对规范键与简单测试键两种格式兼容。

---

## 附注

- 两个 Critical（AUD-7906/7907）同根因（cv2 仅在各方法内局部导入），一处修复（模块级 `import cv2`）即可同时消除；其余各方法的局部导入可顺带清理。
- AUD-7901 的多实例证据来自本批次文件之外的 module.py:71-73，已实读核实，仅作为该发现的调用方佐证，未对 module.py 本身立案（不在本批次范围）。
