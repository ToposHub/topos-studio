# 批次报告：batch-249 [P2] src/features/color/tracking//

编号区间：AUD-24900–AUD-24999。审查日期：2026-09-28。
文件数：8（其中 4 个为兼容 shim，实现在 `src/shared/tracking/`，属其他批次）。

---

## src/features/color/tracking/shape_mask.py (行数: 14)

### 发现

（无）

### 通过项
- 通过（无逻辑）：A1 迁移兼容 shim，`from src.shared.tracking.shape_mask import *` 后再用 `dir(_impl)` 循环把私有下划线名补进 `globals()`，完整转发实现模块的全部符号（含 `rasterize_shape_mask` 等助手）；已确认 `src/shared/tracking/shape_mask.py` 存在，导入目标有效。

---

## src/features/color/tracking/track_data.py (行数: 14)

### 发现

（无）

### 通过项
- 通过（无逻辑）：同上 shim，转发 `src.shared.tracking.track_data`（`TrackingBinding` 等）全部符号；实现模块存在。

---

## src/features/color/tracking/tracker_2d.py (行数: 14)

### 发现

（无）

### 通过项
- 通过（无逻辑）：同上 shim，转发 `src.shared/tracking.tracker_2d`（`TrackRequest`/`TrackResult`/`Tracker2D`/`TrackDirection` 等）全部符号；实现模块存在。

---

## src/features/color/tracking/tracking_cache.py (行数: 14)

### 发现

（无）

### 通过项
- 通过（无逻辑）：同上 shim，转发 `src.shared/tracking.tracking_cache`（`TrackingCache`/`compute_media_fingerprint`）全部符号；实现模块存在。注意：缓存键遗漏 `shape_geometry` 的实质缺陷记在 `tracking_service.py` 一节（见 AUD-24907），因其根因与使用点跨模块。

---

## src/features/color/tracking/tracking_evaluator.py (行数: 274)

### 发现

- [AUD-24900] [Suggestion] 不支持点变换的形状类型静默原样返回，且 `evaluate_geometry` 仍返回非 None
  - 位置: src/features/color/tracking/tracking_evaluator.py:145-160, 199-204
  - 证据:
    ```python
    def apply_transform_to_geometry(base: dict, matrix_np: np.ndarray,
                                    frame_size: FrameSize) -> dict:
        ...
        """...不支持几何点变换的类型（brush/bitmap/gradient）原样返回副本。"""
        ...
        if shape_type == "curve":
            h = _normalized_homography(m, width, height)
            return _transform_curve_geometry(base, h)
        return copy.deepcopy(base)
    ```
    以及 `evaluate_geometry` 中：
    ```python
        return apply_transform_to_geometry(
            binding.base_geometry, transform.matrix_np, frame_size)
    ```
  - 影响: brush/bitmap/gradient 形状绑定跟踪后，`evaluate_geometry` 返回与基准几何相同的字典（非 None），调用方（`image_engine.get_tracked_geometry`）无从区分「跟踪已生效」与「类型根本不支持」，蒙版不跟随跟踪且无任何日志/告警，用户难以定位。
  - 建议: 对不支持类型要么让 `evaluate_geometry` 返回 None（回退原始形状语义），要么至少 `logger.warning` 一次性提示；也可在绑定创建入口（capture 阶段）即拒绝不支持类型。
  - 置信度: 高（行为确定；docstring 声明为有意设计，故仅列 Suggestion）

- [AUD-24901] [Suggestion] `_enabled` 字典只增不减，绑定删除/替换后条目永久残留
  - 位置: src/features/color/tracking/tracking_evaluator.py:175, 179-183
  - 证据:
    ```python
        self._enabled: Dict[str, bool] = {}

    def set_binding_enabled(self, binding_id: str, enabled: bool) -> None:
        self._enabled[binding_id] = bool(enabled)

    def is_binding_enabled(self, binding_id: str) -> bool:
        return self._enabled.get(binding_id, True)
    ```
  - 影响: `TrackingStore.remove_binding`/`add_binding(replace=True)` 会换新 `binding_id`，求值器侧旧 id 的开关状态永不清理；长会话反复重建绑定造成字典缓慢增长，且语义上「已删除绑定的禁用状态」不可追溯地残留。
  - 建议: 提供 `clear_binding_enabled(binding_id)` 并在绑定移除时调用；或改为按 `(clip_id, shape_id)` 键控。
  - 置信度: 高

- [AUD-24902] [Suggestion] 每帧求值对基准几何整树 `copy.deepcopy`，播放热路径持续分配
  - 位置: src/features/color/tracking/tracking_evaluator.py:123, 132, 150-151, 202
  - 证据:
    ```python
    result = copy.deepcopy(base)
    ...
    def _transform_curve_geometry(base: dict, h: np.ndarray) -> dict:
        result = copy.deepcopy(base)
    ...
        if _is_identity(np.asarray(matrix_np, dtype=np.float64).reshape(3, 3)):
            return copy.deepcopy(base)
    ...
        if transform is None or transform.status == TrackStatus.FAILED:
            return copy.deepcopy(binding.base_geometry)
    ```
  - 影响: 渲染链每形状每帧调用 `evaluate_geometry`（image_engine `_tracked_shape_view`），曲线形状的 points 列表逐点深拷贝；60fps 播放多形状时产生可观的小对象分配/GC 压力（4K 实时基准下的非必要开销）。
  - 建议: 基准几何改为不可变结构（嵌套 tuple）后浅拷贝；或对同 (binding, frame) 的求值结果做单槽缓存。
  - 置信度: 高（代码事实；量级影响属估算）

- [AUD-24903] [Suggestion] `capture_base_geometry(shape)` 等参数缺少类型注解
  - 位置: src/features/color/tracking/tracking_evaluator.py:45
  - 证据:
    ```python
    def capture_base_geometry(shape) -> dict:
        """从蒙版形状捕获几何快照（求值基准，参考帧时刻）。
    ```
  - 影响: `shape` 接受 MaskShape 或 dict 的鸭子接口未注明（`Union[MaskShape, dict]` 或 Protocol），调用方与审查者只能读实现才能知道约定；批次其余文件注解完整，仅此处缺失。
  - 建议: 补 `shape: "MaskShape | dict" ` 或定义轻量 Protocol。
  - 置信度: 高

### 通过项
- GLSL·HDR 正确性：本文件不触 GPU；归一化/像素坐标共轭换算 `H_norm = N @ H @ N⁻¹`（`_normalized_homography`）数学正确；rect/circle 在像素空间拟合旋转角避免宽高比畸变的设计正确。
- 参考帧恒等：`_is_identity` 检查避免参考帧双重变换，求值结果与基准几何一致。
- 失败帧策略：FAILED 帧回退基准几何而非输出假有效结果，与文档声明一致。
- 手动修正优先：直接复用 `TrackingBinding.transform_at`（manual 优先于自动）。
- 输入验证：`apply_transform_to_geometry` 校验帧尺寸为正（ValueError）；`bake_binding_keyframes` 校验 `interval >= 1`；`frame_range_with_results` 返回 `(min, max)` 保证 `frames[-1]` 不越界。
- 线程安全：求值经由 image_engine 的 UI 线程渲染路径调用（`render_pipeline`/`_update_mask_textures` 均由控制器在 UI 线程触发），符合 store「只在 UI 线程读写」的约定；未发现 GPU 线程访问。
- 异常路径：`evaluate_geometry_at_timeline` 捕获 mapper 的 ValueError 回退 None。

---

## src/features/color/tracking/tracking_service.py (行数: 373)

### 发现

- [AUD-24904] [Critical] `_cancel_workers` 超时后丢弃仍在运行的 worker 的最后引用，GC 将析构运行中的 QThread 导致进程 Abort
  - 位置: src/features/color/tracking/tracking_service.py:274-287（配合 tracking_worker.py:54-60 无 parent）
  - 证据:
    ```python
    def _cancel_workers(self, key: str, wait_ms: int = 2000) -> None:
        workers = self._workers.pop(key, [])
        for worker in workers:
            worker.request_cancel()
        for worker in workers:
            if not worker.wait(wait_ms):
                # 注意：这里刻意不强杀。QThread.terminate 可能在任意点
                # 终止持锁线程导致堆损坏/进程 Abort（P0-COLOR-01 要消灭
                # 的正是这类崩溃）；运行中路径宁可让卡死 worker 自然泄漏
                # 到进程退出，强杀兜底只用于关闭路径。
                logger.warning(
                    "TrackingWorker %s did not stop within %dms after "
                    "cancel; leaving it to exit on its own",
                    worker, wait_ms)
    ```
    `TrackingWorker` 由默认工厂以 `parent=None` 创建（tracking_worker.py:60 `super().__init__(parent)`）；`_spawn_worker` 中的 lambda 连接只捕获 `self`（服务）与键，不持有 worker。
  - 影响: wait 超时恰恰意味着 worker 仍卡在运行（如 av 解码/IO 挂起）。`self._workers.pop` 之后进程内不存在任何强引用——注释声称「自然泄漏到进程退出」，但实际是函数返回、局部列表释放后 CPython 立即回收 Python 包装并析构 C++ QThread 对象；Qt 对「销毁运行中的 QThread」的行为是未定义/致命（典型表现为进程 Abort）。本仓库已在 image_engine.py:27-29 用 `_TERMINATED_THREADS` 保活列表防御同类崩溃（P1-THREAD-01），此文件漏配同等防护。
  - 建议: 仿照 image_engine 的 `_TERMINATED_THREADS`，把 `wait()` 超时的 worker 放入服务级保活列表（如 `self._zombie_workers`），并可在收到其 `finished_with_result` 后安全移除；或给 worker 设置 parent 保引用。
  - 置信度: 高（机制与项目内同类崩溃先例均可查证）

- [AUD-24905] [Major] 重跟踪失败段时，经合并过滤的「部分结果」被写入全量缓存键，污染缓存
  - 位置: src/features/color/tracking/tracking_service.py:310-315
  - 证据:
    ```python
        if merge_filter is not None:
            result = self._apply_merge_filter(result, merge_filter)

        # 未取消且代次有效：写入缓存（分方向键由请求决定，这里用结果携带的键）
        self.cache.put(cache_key, result)
        self.task_finished.emit(key, gen, result)
    ```
  - 影响: `retrack_failed` 重新跑的是**全范围**双向跟踪，worker 返回完整 transforms；`_apply_merge_filter` 只保留失败帧+衔接帧后，这个残缺结果被 `cache.put` 到与全量任务完全相同的键下（缓存键不含「是否重跟踪」信息，见 `TrackingCache.key_for_request`）。此后任何同参数的完整跟踪命中缓存只拿到失败帧子集——原先有效的帧全部缺失（蒙版跟踪结果大面积消失）。触发路径现实存在：进程重启后缓存为空、工程加载出含 FAILED 帧的绑定 → 用户点「重跟踪失败段」→ worker 全量计算被过滤后入缓存 → 之后用户再点完整跟踪（同参数）即命中残缺缓存。
  - 建议: 缓存写入使用**过滤前**的原始 result；merge_filter 只作用于回发给调用方的结果。
  - 置信度: 高（机制确定，触发依赖缓存未命中的重跟踪场景）

- [AUD-24906] [Major] 缓存命中路径 `task_finished` 先于 `task_started` 发射，全部方向命中缓存时面板永久卡在 RUNNING
  - 位置: src/features/color/tracking/tracking_service.py:157-173
  - 证据:
    ```python
            cached = self.cache.get(cache_key)
            if cached is not None:
                # 命中缓存：以当前代次回发，不重新计算；
                ...
                self.task_finished.emit(key, gen, result)
                emitted_cached = True
                continue
            self._spawn_worker(key, gen, request, cache_key, merge_filter)

        if not emitted_cached and not self._workers.get(key):
            # 所有方向都命中缓存时不算运行中；这里处理空方向列表等异常
            logger.debug("任务 %s 没有可启动的方向", key)
        self.task_started.emit(key, gen)
    ```
  - 影响: 已核实消费链：`task_finished` → 控制器 `_on_tracker_task_finished` 应用结果并 `set_tracking_binding`（面板置 SUCCESS/PARTIAL_FAILED）；随后才发射的 `task_started` → `_on_tracker_task_started` 无条件 `set_state(PanelState.RUNNING)`。当所有方向都命中缓存（用户对同一形状同参数第二次点跟踪，或 retrack 的两个方向都命中）时没有任何后续信号纠正状态：面板停留在 RUNNING，`_apply_state` 中全部跟踪按钮禁用、自动按钮变「取消」，而实际无任务运行；点取消也无效（`cancel` 只清空已空的 worker 列表）。
  - 建议: `task_started` 移到方法开头（取消旧任务、登记新代次之后、任何回发之前）发射；或缓存命中路径不发射 `task_started`。
  - 置信度: 高（服务/控制器/面板三方代码均已查证）

- [AUD-24907] [Major] 缓存键未包含 `shape_geometry`，形状几何变化不会使缓存失效（根因在 shared 缓存键构造，经本文件使用生效）
  - 位置: src/features/color/tracking/tracking_service.py:143-158（实质缺陷位于 src/shared/tracking/tracking_cache.py:63-75 `key_for_request` 未收 shape_geometry）
  - 证据:
    ```python
            request = TrackRequest(
                media_path=media_path,
                ...
                generation=gen,
                shape_geometry=shape_geometry,
            )
            cache_key = TrackingCache.key_for_request(request, fingerprint)
            cached = self.cache.get(cache_key)
    ```
    而 `shape_geometry` 实际影响结果——tracker_2d.py:252-258 用它光栅化掩码限制初始特征点：
    ```python
    shape_mask = None
    if request.shape_geometry:
        from src.shared.tracking.shape_mask import rasterize_shape_mask
        ...
        shape_mask = rasterize_shape_mask(request.shape_geometry, ana_w, ana_h)
    ```
  - 影响: 用户编辑蒙版几何（如移动曲线控制点而 pattern 包围盒不变）后以相同区域/参数重跟踪，会直接命中按旧几何计算的缓存结果，特征点集合与用户新遮罩不符，静默给出过期跟踪；与「任一要素变化都构成不同键」的缓存设计声明矛盾。
  - 建议: `TrackingCache.make_key` 增加由 `shape_geometry` 派生的摘要（如稳定序列化后的短哈希）；服务侧在 `geometry_changed` 时调用 `cache.remove` 兜底。
  - 置信度: 中（机制确定；实际触发需 pattern/search 区域与参数完全一致而仅几何变化的操作序列）

- [AUD-24908] [Suggestion] `_fingerprint_cache` 无上限增长
  - 位置: src/features/color/tracking/tracking_service.py:78, 249-254
  - 证据:
    ```python
        self._fingerprint_cache: Dict[str, str] = {}  # media_path -> fingerprint
    ...
    def _fingerprint(self, media_path: str) -> str:
        fp = self._fingerprint_cache.get(media_path)
        if fp is None:
            fp = compute_media_fingerprint(media_path)
            self._fingerprint_cache[media_path] = fp
        return fp
    ```
  - 影响: 长会话中每个访问过的媒体文件永久占一个条目（键为完整路径），无 LRU 淘汰；同时缓存的指纹基于 mtime/size，外部替换文件后指纹过期问题另由缓存键设计吸收，但条目本身永不清理。
  - 建议: 换成有界 LRU（如 `OrderedDict`，容量 ~64）或定期清理。
  - 置信度: 高

- [AUD-24909] [Suggestion] `invalidate` 用 `key.startswith(f"{clip_id}:")` 前缀匹配，clip_id 含冒号时可误伤
  - 位置: src/features/color/tracking/tracking_service.py:233-238
  - 证据:
    ```python
        keys = list(self._workers.keys())
        for key in keys:
            if clip_id is not None and not key.startswith(f"{clip_id}:"):
                continue
            self._cancel_workers(key)
            self._generation[key] = self._generation.get(key, 0) + 1
    ```
  - 影响: task_key 为 `f"{clip_id}:{shape_id}"`；若 clip_id 自身含 `:`（或一个 clip_id 恰为另一个 clip_id 的前缀），`startswith` 会把其它片段的任务一并取消并推进代次（其结果被当陈旧丢弃）。当前 clip_id 多为 UUID 时风险低，但接口未约束。
  - 建议: 解析 key 时用 `key.split(":", 1)` 与 clip_id 精确比较；或在文档中约束 id 字符集。
  - 置信度: 高（机制）/触发概率低

- [AUD-24910] [Suggestion] `_drop_worker` 的 `gen` 参数未使用，`isRunning()` 过滤存在竞态可致 worker 残留
  - 位置: src/features/color/tracking/tracking_service.py:324-335
  - 证据:
    ```python
    def _drop_worker(self, key: str, gen: int) -> None:
        """任务结束后从运行列表移除；全部结束时清理空列表。"""
        workers = self._workers.get(key)
        if workers is None:
            return
        # QThread 自身没有直接比对依据，按代次无法区分同 key 多 worker；
        # 保留列表直到代次变化/取消时整体清理即可。
        remaining = [w for w in workers if w.isRunning()]
    ```
  - 影响: 信号送达 UI 线程时，刚发完 `finished_with_result` 的 worker 线程可能尚未完全退出（`isRunning()` 仍真），该 worker 被留在 `self._workers[key]` 且此后无人再移除 → `is_running()` 对该形状持续返回 True，控制器 `_on_tracker_task_finished` 末尾据此把面板置回 RUNNING（与 AUD-24906 同样的卡态症状，低概率）。`gen` 参数从未使用，签名有误导性。
  - 建议: 连接时把 worker 引用绑定进 lambda 闭包，回调中按对象身份移除；删除未用的 `gen` 参数。
  - 置信度: 中（竞态窗口小，但代码路径存在）

### 通过项
- 线程模型：服务仅在 UI 线程使用（docstring 声明 + 控制器调用点核实）；worker 结果经队列信号回 UI 线程，代次（generation）陈旧保护 `_current` 检查正确，`stale_result_discarded` 明确回执。
- 同形状互斥：`start` 先 `_cancel_workers` 再登记新代次，旧结果必然被判陈旧。
- 取消路径：不使用 `QThread.terminate` 的决策正确（除 AUD-24904 的引用保留缺陷外）。
- 不越界：服务不直接写 `TrackingStore`，结果经信号交调用方，边界清晰。
- 缓存回发代次：`replace(cached, generation=gen)`（TrackResult 为 frozen dataclass，字段核实一致，无丢字段）。
- `retrack_failed` 的代次推导一致（assert 防漂移）、`_gap_frames` 外扩限制在帧范围内且排除参考帧。
- 异常路径：worker 异常经 `failed_with_error` 信号回 UI 线程并带 generation；`_apply_merge_filter`/`_gap_frames` 为纯函数。

---

## src/features/color/tracking/tracking_store.py (行数: 118)

### 发现

- [AUD-24911] [Suggestion] `get_binding` O(n) 线性扫描，位于渲染每帧每形状的求值热路径
  - 位置: src/features/color/tracking/tracking_store.py:50-54
  - 证据:
    ```python
    def get_binding(self, clip_id: str, shape_id: str) -> Optional[TrackingBinding]:
        for binding in self._bindings.values():
            if binding.clip_id == clip_id and binding.shape_id == shape_id:
                return binding
        return None
    ```
  - 影响: `TrackingEvaluator.evaluate_geometry` 每次求值都调用 `get_binding`，渲染链每形状每帧触发一次（image_engine `_tracked_shape_view`）；绑定数增多后是播放热路径上的线性开销（4K 实时基准下不必要）。
  - 建议: 维护 `(clip_id, shape_id) -> binding_id` 二级字典，增删改查同步维护。
  - 置信度: 高

- [AUD-24912] [Suggestion] `from_dict` 对非数值 `schema_version` 抛出未捕获 ValueError，与单条损坏容忍策略不一致
  - 位置: src/features/color/tracking/tracking_store.py:91-92
  - 证据:
    ```python
        version = int(data.get("schema_version", 0))
        migrated = TrackingStore._migrate(data, version)
    ```
  - 影响: 外部工程数据（外部数据须验证）中 `schema_version` 若为非数值字符串（如 `"one"`、`null` 外的垃圾值），`int()` 抛 ValueError 且未被本层捕获，可能使整个调色栈/工程加载失败；而同函数对单条绑定损坏采取「跳过并告警」的容忍策略，口径不一致。
  - 建议: `try: version = int(...) except (TypeError, ValueError): version = 0` 并记 warning 走迁移链。
  - 置信度: 高（机制确定，触发依赖损坏文件，频率低故列 Suggestion）

### 通过项
- 增删改查：`add_binding` 的重复检测/replace 语义正确（先移除旧 id 再写入）；`remove_bindings_for_shape`/`bindings_for_clip`/`all_bindings` 行为一致。
- 持久化一致性：`to_dict`/`from_dict` 键位对应（schema_version + bindings），空 store 序列化为 `{}` 可无损回读；绑定级损坏被捕获并 `logger.warning(exc_info=True)` 跳过，不静默吞错。
- schema 迁移：v0→v1 显式补齐，未知未来版本告警后尽力加载，不静默猜测。
- 类型注解：完整。

---

## src/features/color/tracking/tracking_worker.py (行数: 96)

### 发现

- [AUD-24913] [Suggestion] `_CancelToken` 用裸布尔属性实现跨线程取消，建议改用 `threading.Event`
  - 位置: src/features/color/tracking/tracking_worker.py:26-35
  - 证据:
    ```python
    class _CancelToken:
        """简单布尔 token（worker 内部使用，跨线程只做原子读写）。"""

        def __init__(self) -> None:
            self._set = False

        def set(self) -> None:
            self._set = True

        def is_set(self) -> bool:
            return self._set
    ```
  - 影响: 在当前 GIL CPython 下可用（属性赋值为原子），但跨线程可见性依赖实现细节，未来 free-threaded 构建或移植时无保证；标准库 `threading.Event` 提供明确的同步语义且接口同名（set/is_set），替换零成本。
  - 建议: 用 `threading.Event` 替换（tracker 侧只依赖 `is_set()`，接口兼容）。
  - 置信度: 高

### 通过项
- 线程安全：worker 在工作线程内只运行无 Qt 依赖的 `Tracker2D.track`（已核实 Tracker2D 纯 numpy/cv2，无 OpenGL/Qt 资源）；结果与异常全部经信号（队列连接）送回 UI 线程，不跨线程直接调用 UI。
- 异常路径：`run()` 捕获 Exception 后 `logger.exception` 并经 `failed_with_error` 回发，不吞错、不让异常裸穿线程边界。
- 生命周期：对象由服务持有（引用问题见 tracking_service.py AUD-24904，属调用方缺陷）；`request_cancel` 幂等，取消在帧边界生效由 tracker 主循环检查。
- 类型注解：完整（信号与构造参数均注解）。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-24904 |
| Major | 3 | AUD-24905, AUD-24906, AUD-24907 |
| Suggestion | 10 | AUD-24900–24903, AUD-24908–24913 |

Critical 概要：`TrackingService._cancel_workers` 在 wait 超时后丢弃仍在运行的 `TrackingWorker` 的最后引用，GC 将析构运行中的 QThread，可致进程 Abort——与仓库已在 image_engine 中用 `_TERMINATED_THREADS` 防护的 P1-THREAD-01 同类崩溃。
