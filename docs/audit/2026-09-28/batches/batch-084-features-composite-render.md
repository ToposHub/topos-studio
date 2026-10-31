# batch-084 — src/features/composite/render//

编号区间：AUD-8400–AUD-8499。审查日期：2026-09-28。
文件数：5；总行数：2,882。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解。

汇总：Critical 0 · Major 5 · Suggestion 15（共 20 项发现）。

---

## src/features/composite/render/pbo_readback.py (行数: 583)

### 发现

- [AUD-8400] [Major] 同一类内重复定义 poll_ready 与 get_latest，后一份静默覆盖前一份（死代码且两份实现已分叉）
  - 位置: src/features/composite/render/pbo_readback.py:296、341（第一份，死代码）与 :451、497（第二份，生效）
  - 证据:
    ```python
    # 第 296 行与第 451 行各出现一次：
    def poll_ready(self) -> list[ReadbackResult]:
        """Return all completed readback results and clear them from the ring.
    # 第 341 行与第 497 行各出现一次：
    def get_latest(self, wait_newest: bool = False) -> Optional[ReadbackResult]:
        """Return the most recent completed readback result.
    ```
  - 影响: Python 类体顺序执行，第 451/497 行的定义生效，第 296/341 行成为死代码。两份实现已实质性分叉：第一份 get_latest 含 T-P1-71 的超时判定修复与 `self._ring[newest_idx - self._ring_size]`（即 newest_idx 本身，冗余索引）等独有逻辑，第二份没有（见 AUD-8401）。后续维护者编辑任一副本都可能改到不生效的代码，是典型的"修复丢失"温床。
  - 建议: 删除第 296–339 与 341–400 行的死副本（或提取公共逻辑为单一实现），保留含 T-P1-71 修复的版本并补齐其超时判定（见 AUD-8401）。
  - 置信度: 高

- [AUD-8401] [Major] 生效版 get_latest(wait_newest=True) 忽略 _wait_entry_fence 的超时结果仍映射 PBO，T-P1-71 修复在生效副本中丢失
  - 位置: src/features/composite/render/pbo_readback.py:519-523（生效版）对照 :376-382（被覆盖的死代码版）
  - 证据:
    ```python
    # 生效版（497 行起）：
            if wait_newest:
                self._wait_entry_fence(newest)      # 返回值被丢弃！
                self._map_entry(newest, self._pbos[newest_idx])
                self._delete_fence(newest.fence)
                newest.fence = None
    # 被覆盖的死代码版（379 行起）才有正确判定：
                if self._wait_entry_fence(newest):
                    self._map_entry(newest, self._pbos[newest_idx])
    ```
  - 影响: `_wait_entry_fence` 的 docstring（T-P1-71）明确：超时必须按"未就绪"处理，映射在途 PBO 会读到未定稿数据；在 macOS 遗留软渲染器上对在途 buffer 的 glMapBuffer 可能阻塞（驱动同步），极端情况下复现 T-P1-71 式渲染线程卡死。生效副本丢弃了返回值，1 秒超时后无条件映射。当前生产代码未调用 wait=True 路径（仅 tests/composite/test_pbo_async_readback.py:181 使用），故未爆发。
  - 建议: 恢复死代码版中的 `if self._wait_entry_fence(newest):` 判定——超时返回 None、保留 fence 供后续非阻塞 poll。
  - 置信度: 高

- [AUD-8402] [Major] 生效版 get_latest 非阻塞路径丢弃 poll_ready 刚映射完成的数据并返回 None——异步 lane 的非阻塞收集永远无法交付已完成帧
  - 位置: src/features/composite/render/pbo_readback.py:524-532（连同 534-550 的 sweep）
  - 证据:
    ```python
            else:
                # Never block: only take it if a non-blocking poll maps it.
                if not self.poll_ready():
                    return None
                if self._ring[newest_idx] is None:
                    # poll_ready collected it — pick it up in the sweep.
                    pass
    ```
  - 影响: `poll_ready()` 会把所有已完成 entry 映射进返回列表并把 ring 槽位置 None；该返回列表在此被丢弃。之后的 sweep 只扫描 `self._ring`——必然为空——`best` 恒为 None。结果：当最新帧恰好在本次调用时完成（流水线稳态的常见情形），get_latest(wait=False) 返回 None 且把这帧数据永久丢弃；下一次调用 ring 已空仍返回 None。调用方 `composite_frame_renderer.py:522 collect_async_preview(wait=False)` 因此永远拿不到帧（测试 test_pbo_async_readback.py:205 恰好只断言 None，掩盖了该缺陷）。仅 enqueue_sync 预置 data 的路径（composite_renderer.py:1493-1496）不受影响。
  - 建议: 使用 poll_ready 的返回值（如取 `max(results, key=lambda r: r.request_id)`），或让 poll_ready 把结果留在 ring 中由 sweep 收集。
  - 置信度: 高

- [AUD-8403] [Suggestion] enqueue 覆写未收集的 pending entry 时不删除其 fence，GLsync 对象泄漏
  - 位置: src/features/composite/render/pbo_readback.py:182-185、212-219
  - 证据:
    ```python
            prev = self._ring[idx]
            if prev is not None and prev.data is None:
                self._wait_entry_fence(prev)
            ...
            self._ring[idx] = _PBOEntry(
                request_id=request_id,
                width=width,
                height=height,
                data=None,
                fence=fence,
            )
    ```
  - 影响: prev 只被用来等待，随后被新 entry 覆盖，其 `prev.fence` 从未 `glDeleteSync`。当消费方停止收集（如预览不可见）而生产方持续 enqueue 超过一圈 ring 时，每次覆写泄漏一个 GLsync 驱动对象；长会话下无上界。
  - 建议: 覆写前 `self._delete_fence(prev.fence)`。
  - 置信度: 高

- [AUD-8404] [Suggestion] enqueue_sync 直接覆写 ring 槽位：在途 entry 的 fence 泄漏且其请求被静默丢弃
  - 位置: src/features/composite/render/pbo_readback.py:424-444
  - 证据:
    ```python
            idx = self._write_idx
            raw = fbo.read(components=4, dtype='f4')
            ...
            self._ring[idx] = _PBOEntry(
                request_id=request_id,
                width=width,
                height=height,
                data=bgr,  # CPU top-left BGR float32
            )
    ```
  - 影响: enqueue_sync 不检查槽位现有 entry；若同一实例上混用了异步 enqueue（entry.data=None、fence 在途），该 entry 连同其 fence 被直接丢弃。当前两个使用方（composite_renderer.py 仅 enqueue_sync、composite_frame_renderer.py 仅 enqueue）未混用，属条件性风险。
  - 建议: 覆写前若 `prev is not None and prev.fence is not None` 则 `_delete_fence`；或在 docstring 明确禁止混用。
  - 置信度: 中

- [AUD-8405] [Suggestion] fence 不可用（glFenceSync 创建失败 / glClientWaitSync 查询异常）时无等待兜底直接映射在途 PBO
  - 位置: src/features/composite/render/pbo_readback.py:205-210、476-484
  - 证据:
    ```python
                fence = None
                try:
                    from OpenGL.GL import GL_SYNC_GPU_COMMANDS_COMPLETE, glFenceSync
                    fence = glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0)
                except Exception:
                    fence = None
                ...
                except Exception:
                    pass  # fence unusable — fall through and try the map
    ```
  - 影响: fence=None 的异步 entry 在 poll_ready 中跳过等待分支直接 `_map_entry`，可能映射到尚未完成 DMA 的 PBO（读取未定稿数据或驱动阻塞）。代码注释自认此为 fallback，但未做任何二次确认（如 glFinish 或时间戳）。
  - 建议: fence 创建失败时降级为同步语义（enqueue 内立即映射），或至少记录 warning 并跳过该帧。
  - 置信度: 中

- [AUD-8406] [Suggestion] 类型注解缺失与未使用 import
  - 位置: src/features/composite/render/pbo_readback.py:89（gl_context 无类型）、:552（cleanup 无 -> None）、:304-305（第一份 poll_ready 内 import ctypes/_gl 未使用；该份为死代码，合并修复时一并处理）
  - 证据:
    ```python
    def __init__(self, gl_context, ring_size: int = 3, dtype: str = "f4",
                 reorder_bgr: bool = True):
    ...
    def cleanup(self):
        """Release PBO buffer objects and pending fences."""
    ```
  - 影响: 与项目强类型规范不符；gl_context 实际接受 ModernGL context，IDE/静态检查无法校验。
  - 建议: `gl_context: Any`（或 "mg.Context" 字符串注解）、`cleanup(self) -> None`。
  - 置信度: 高

### 通过项
- GL 状态保存/恢复正确：enqueue 保存并恢复 GL_READ_FRAMEBUFFER_BINDING 与 GL_PACK_ALIGNMENT（191-203）
- GPU 资源释放：cleanup 删除全部 PBO 与残留 fence，并复位 _initialized/_ring（552-573）
- HDR 正确性：f4/f2 通道按原始 dtype 读回，无非法 clamp(0.0,1.0)；BGR 重排与 flip 约定与 docstring 一致
- 线程边界：GL 调用均发生在渲染线程（initialize/enqueue/enqueue_sync 由渲染器在同线程调用），无 UI 线程操作 OpenGL
- fence 有界等待常量 _DEFAULT_FENCE_TIMEOUT_NS=1s（T-P1-71），_wait_entry_fence 默认不传 GL_TIMEOUT_IGNORED

---

## src/features/composite/render/parallel_frame_render.py (行数: 262)

### 发现

- [AUD-8407] [Suggestion] on_worker_error 对同一错误被调用两次，违反 "called once" 契约
  - 位置: src/features/composite/render/parallel_frame_render.py:199-205（worker 侧）与 :246-252 / :240-244（consumer 侧）
  - 证据:
    ```python
                except Exception as exc:
                    collector.put(frame_no, ("error", exc))
                    with errors_lock:
                        if not errors:
                            errors.append(str(exc))
                            stop_event.set()
                            collector.finish()
                            if on_worker_error is not None:
                                on_worker_error(str(exc))     # 第一次
    ...
            status, payload = item
            if status != "ok":
                stop_event.set()
                collector.finish()
                if on_worker_error is not None:
                    on_worker_error(str(payload))             # 第二次
    ```
  - 影响: docstring 声明 "first renderer exception message (called once)"，实际 worker 与 consumer 各调用一次。现调用方 composite_render_output.py:937/1067 用 `state["error"] = m` 幂等写无碍；但任何非幂等回调（弹错误框、计费式计数）会重复执行。
  - 建议: worker 侧不再直接调用 on_worker_error（只记录 errors），统一由 consumer 的 error/drained 路径上报一次。
  - 置信度: 高

### 通过项
- 线程安全：errors_lock/active_lock/Condition/Semaphore 使用一致；slot 先取后领任务的设计避免超时丢帧（179-189 注释并实现）；_OrderedFrameCollector 的 put/next/drained 无竞态
- 异常与取消路径：factory 失败降级（163-173）、渲染异常首错止损、cancel 逐帧检查、finally 中 stop_event+finish+join(timeout=5.0) 覆盖完整；worker 提前退出时 active_workers 归零触发 finish 解除 consumer 阻塞
- GPU 资源生命周期：teardown_fn 在各 worker 自有线程执行，符合 GL 资源线程亲和要求（207-215）
- 状态提交一致性：按序消费（sink_fn 收到 frame_numbers[consumed]），N=1 与 N>1 决定性由独立求值保证
- 输入验证：resolve_parallel_workers 对 preferred/cap/cpu_count 做了 int 转换与下限钳制（38-47）
- 类型注解完整（全部公开函数与内部类均有注解）

---

## src/features/composite/render/prefetch_scheduler.py (行数: 531)

### 发现

- [AUD-8408] [Major] velocity 单位不一致（时间戳为秒、阈值按毫秒标定），趋势预测实质失效
  - 位置: src/features/composite/render/prefetch_scheduler.py:189、50、104、236-241
  - 证据:
    ```python
            timestamp: Monotonic timestamp (time.monotonic()).   # ← 秒
    ...
        velocity: float = 0.0  # units per millisecond            # ← 声明为 ms
    ...
        MAX_VELOCITY_THRESHOLD = 0.01  # units per ms
    ...
            if len(history.timestamps) >= 2:
                time_diff = history.timestamps[-1] - history.timestamps[-2]
                if time_diff > 0:
                    history.velocity = abs(diff) / time_diff      # 实为 units/秒
    ```
  - 影响: time.monotonic() 返回秒，velocity 实际单位是 units/s，却与 0.01 units/ms（=10 units/s）比较，阈值等效收紧 1000 倍。任何快于 0.01 units/s 的拖拽（正常拖拽速度）都会走 `_fallback_prediction`，线性回归/趋势外推路径基本不可达，预取命中率依赖的低置信 fallback（见 AUD-8409）——整个预取特性静默退化为"几乎不触发"。
  - 建议: 统一单位——velocity 乘 1000 换算为 units/ms，或将阈值改标 units/s 并同步注释。
  - 置信度: 高

- [AUD-8409] [Suggestion] fallback 预测 confidence=0.2 恒低于触发阈值 0.4，该分支永远不会触发 prefetch，且缺 bounds clamp 与统计计数
  - 位置: src/features/composite/render/prefetch_scheduler.py:399-405、101、412-424
  - 证据:
    ```python
                    return PrefetchPrediction(
                        node_id=node_id,
                        param_name=param_name,
                        predicted_value=predicted,
                        confidence=0.2,  # Low confidence
                        based_on_trend=False,
                    )
    ...
        MIN_CONFIDENCE_THRESHOLD = 0.4
    ...
        return prediction.confidence >= self.MIN_CONFIDENCE_THRESHOLD
    ```
  - 影响: `should_prefetch` 对 fallback 恒 False，_fallback_prediction 成为纯计算死路；叠加 AUD-8408 后预取几乎全灭。fallback 也不做 param_bounds clamp（可能预测出界外值）、不递增 predictions_made（统计失真）。
  - 建议: 要么调低阈值/调高 fallback confidence 使其可用，要么删除 fallback 分支；补 clamp 与统计。
  - 置信度: 高

- [AUD-8410] [Suggestion] 2 值历史的 simple extrapolation 经 history_factor 缩放后置信度上限 0.32，该分支实际不可触发
  - 位置: src/features/composite/render/prefetch_scheduler.py:281-283、319-322
  - 证据:
    ```python
        # Confidence adjustment based on history size
        history_factor = len(history.values) / self.MAX_HISTORY_SIZE
        confidence *= history_factor
    ...
            confidence = 0.5 * diff_similarity + 0.3   # 上限 0.8
    ```
  - 影响: len=2 时 factor=0.4，0.8×0.4=0.32 < 0.4 阈值——恰好只有两个采样点时的简单外推永远低于触发线，与"最少 2 值即可预测"的设计意图相悖。
  - 建议: history_factor 对 len>=2 用 `len / max(len, 3)` 之类更平缓的曲线，或对 2 值情形单独定标。
  - 置信度: 高

- [AUD-8411] [Suggestion] _param_histories 无淘汰机制 + 类型注解缺失 + 死变量
  - 位置: src/features/composite/render/prefetch_scheduler.py:191-206、109、115、338-339
  - 证据:
    ```python
        history = self._param_histories.get(key)
        if history is None:
            history = ParamHistory(...)
            self._param_histories[key] = history   # 只增不减（除非显式 clear）
    ...
        values = list(history.values)
        timestamps = list(history.timestamps)      # 从未使用
    ...
    def __init__(self):                            # 缺 -> None
    def set_prefetch_callback(self, callback: Callable):   # 缺返回注解/参数泛型
    ```
  - 影响: 每个曾拖动过的 (node_id, param_name) 组合永久驻留（每条约 5 个浮点的 deque），长会话/多图切换下缓慢增长（composite_page 仅在特定时机 clear）。ParamHistory.values/timestamps 的 deque 未注解元素类型。
  - 建议: 加 LRU/上限淘汰或节点删除时联动清理；补齐注解；删除 timestamps 死变量。
  - 置信度: 中

### 通过项
- 线程安全：仅 UI 线程使用（docstring 声明且 composite_page.py 单线程调用），无需锁
- 信号防递归：不涉及 Qt 信号，回调为单向 trigger_prefetch，无回路
- 状态提交一致性：check_prediction_hit 命中/未命中后清空 _active_prediction，无双计
- 输入验证：趋势路径的预测值做 bounds clamp（285-288）
- 算法正确性：线性回归/R² 计算本身正确（ss_tot==0、denominator==0 均有退化分支）

---

## src/features/composite/render/render_scheduler.py (行数: 1372)

### 发现

- [AUD-8412] [Major] ROI 启用时，非 Output 上游的 viewer_node 预览分支 output_roi 恒为 None → 被判 lod_level=2（四分之一分辨率评估）
  - 位置: src/features/composite/render/render_scheduler.py:924-927、1045-1053（消费方 src/features/composite/evaluators/base_evaluator.py:769-772）
  - 证据:
    ```python
        output_node_ids = [
            nid for nid, np in node_plans.items()
            if is_output_node_type(np.node_type)
        ]
        if not output_node_ids:
            return
    ...
        for node_id, np in node_plans.items():
            ...
            if np.output_roi is None or np.output_roi.is_empty:
                # No ROI assigned (not needed for current viewport) → low-res
                np.lod_level = 2
                continue
    ```
  - 影响: ROI 反向传播只从 Output 节点出发；RenderRequest.viewer_node_id 预览一个不位于 Output 上游的节点时（"preview any node"特性，见 166-174 行 docstring），该分支的所有节点不会被指派 output_roi，从而落入 lod_level=2。base_evaluator 按 `lod_level = np.lod_level` 对输入降采样——用户开启了 ROI 后，节点预览画面降为 1/4 分辨率且与视口相交与否无关。仅在 ROI 激活（viewer_roi 非 None 且 preview 模式、force_lod==0）时触发，故标中置信度。
  - 建议: _propagate_roi 的起点加入 viewer_node_id 的预览目标（或对该分支禁用 LOD 赋值，保持 lod 0）。
  - 置信度: 中

- [AUD-8413] [Suggestion] update_tracking=False 的 analysis-only plan 仍可能改脏跟踪状态（ROI 键重置未受该开关保护）
  - 位置: src/features/composite/render/render_scheduler.py:624-632（对照 540-548 的契约说明）
  - 证据:
    ```python
        if effective_roi_key != self._prev_roi_key:
            self._prev_param_revisions = {}
        self._prev_roi_key = effective_roi_key
        dirty = self._compute_dirty_nodes(snapshot, node_plans)
    ...
            update_tracking: Advance the dirty-comparison tracking state.
                Must stay True for the plan call that DRIVES evaluation.
                Analysis-only callers (e.g. the readback renderer's tiling
                probe) must pass False: ...
    ```
  - 影响: update_tracking=False 只豁免 `_update_tracking`，但 ROI 键比较仍会清空 `_prev_param_revisions` 并改写 `_prev_roi_key`——违反"analysis-only 不消费脏状态"的契约：若探测调用与驱动调用的 effective_roi_key 不同，下一次驱动 plan 将被强制全量 dirty（多余的全图重渲染）；若两调用跨线程共用实例则存在并发写。当前实际调用（composite_export_renderer.py:260-261，独立实例、EXPORT 模式、无 viewer_roi，键恒 None==None）未触发，属条件性风险。
  - 建议: 把 629-631 行的 ROI 重置也置于 `if update_tracking:` 之下。
  - 置信度: 中

- [AUD-8414] [Suggestion] _prev_port_revisions/_prev_upstream_revisions 只写不读，死状态
  - 位置: src/features/composite/render/render_scheduler.py:523-525、1215-1216、1223-1224、1242-1243、1267-1268
  - 证据:
    ```python
        # Maps node_id -> {port_name -> revision}
        self._prev_port_revisions: Dict[str, Dict[str, int]] = {}
        # Maps node_id -> {input_port_name -> upstream_revision}
        self._prev_upstream_revisions: Dict[str, Dict[str, int]] = {}
    ...
            self._prev_port_revisions[node_id] = dict(ns.port_revisions)
            self._prev_upstream_revisions[node_id] = dict(ns.upstream_port_revisions)
    ```
  - 影响: `_compute_dirty_nodes` 的端口级比较（1177-1184）用的是快照内的 `upstream_ns.port_revisions` vs `ns.upstream_port_revisions`，从不读这两个字典（全仓 grep 仅本文件出现）。每次 plan 白白深拷贝两份 dict，reset/prepare_for_export 也随行维护——纯开销与误导。
  - 建议: 删除这两个字段及其维护代码，或改写到真正参与比较。
  - 置信度: 高

- [AUD-8415] [Suggestion] plan() 节点循环内重复局部 import，且 get_node_capability 未使用
  - 位置: src/features/composite/render/render_scheduler.py:582-583（对照模块级 26-30 已导入 is_output_node_type）
  - 证据:
    ```python
            # Classify node action using node_capabilities system
            ...
            from ..core.node_capabilities import is_output_node_type, get_node_capability
    ```
  - 影响: import 语句位于 per-node 循环内，每个节点每次规划都执行一次 sys.modules 查找并遮蔽模块级同名导入；get_node_capability 导入后从未使用。
  - 建议: 移到模块级（已无循环依赖顾虑——模块级已 import 同一包）；删除 get_node_capability。
  - 置信度: 高

- [AUD-8416] [Suggestion] is_stale_request/mark_current 对 _latest_* 字段无锁读写，与同类方法加锁风格不一致
  - 位置: src/features/composite/render/render_scheduler.py:1329-1337、1301-1302（对照 is_stale:1310-1313、should_cancel_evaluation:1354-1355 均持 _id_lock）
  - 证据:
    ```python
        if request.request_id < self._latest_request_id:      # 无锁
            return True
        if request.graph_generation < self._latest_graph_generation:   # 无锁
            return True
        if request.color_revision < self._latest_color_revision:       # 无锁
            return True
    ```
  - 影响: `_latest_request_id` 由 UI 线程 advance_request_id 持锁写入，此处裸读；mark_current 又在锁外写 `_latest_graph_generation/_latest_color_revision`。CPython GIL 下单 int 读不撕裂，但三字段可见性无顺序保证，可能读到"request_id 已新、generation 尚旧"的中间组合。当前无外部调用方（grep 全仓仅定义），风险潜伏。
  - 建议: is_stale_request 内改持 _id_lock 读取；mark_current 把两个字段写入移入锁内。
  - 置信度: 中

- [AUD-8417] [Suggestion] _snapshot_node_needs_frame 两处宽泛 except Exception 吞错，帧依赖误判会导致 dirty-skip 回放旧帧
  - 位置: src/features/composite/render/render_scheduler.py:66-71、79-89
  - 证据:
    ```python
        try:
            from ..core.node_capabilities import requires_frame_number
            if requires_frame_number(ns.node_type):
                return True
        except Exception:
            pass
        ...
        except Exception:
            # A missing/optional node implementation must not prevent the
            # scheduler from producing a plan. ...
            pass
    ```
  - 影响: 若 import 或 `params_require_frame_cache` 因真实缺陷抛错，节点会被静默当作"帧无关"——播放头移动不标 dirty，evaluator 的 dirty-skip 便复用上一帧缓存（docstring 自述此为"particularly dangerous"）。违反 .clinerules"不得宽泛 except 掩盖领域错误"。
  - 建议: except 收窄到 ImportError/AttributeError 并 logger.warning，或失败时按 True（帧相关）处理——安全方向应偏向多渲染而非旧帧。
  - 置信度: 中

- [AUD-8418] [Suggestion] _check_capability_consistency 定义后调用被注释，_GPU_STATEFUL_TYPES 与能力表一致性校验实际未运行
  - 位置: src/features/composite/render/render_scheduler.py:139-141
  - 证据:
    ```python
    # Run consistency check at module load (only in debug/development)
    # Commented out for production; enable for CI validation
    # _check_capability_consistency()
    ```
  - 影响: BUG-13 修复依赖的两组来源类型集合（_GPU_STATEFUL_TYPES vs SOURCE_NODE_TYPES）漂移时无任何报警，调度器可能把新增 source 节点误当 effect 处理（注释 99-100 行所述的故障模式会复发）。
  - 建议: 挪入 CI 测试（tests 中调用断言），而非注释在模块内。
  - 置信度: 高

- [AUD-8419] [Suggestion] _propagate_roi 每次规划为每个 effect 节点两遍实例化临时 Node，叠加每节点 json.dumps+md5——交互路径性能模式
  - 位置: src/features/composite/render/render_scheduler.py:981-986、1004-1007、576-577
  - 证据:
    ```python
                else:
                    # Effect/output nodes: use compute_output_bbox method
                    node = self._create_temp_node(ns)
                    if node:
                        bbox = node.compute_output_bbox(input_bboxes, ns.parameters)
    ...
            params_str = json.dumps(ns.parameters, sort_keys=True, default=str)
            params_hash = hashlib.md5(params_str.encode()).hexdigest()[:12]
    ```
  - 影响: 开启 ROI 时每次 plan() 对每个 effect 节点 create_node_by_type + 参数拷贝两次（前向 bbox + 反向 ROI）；json.dumps+md5 对全部节点每帧执行。拖拽期间每次参数变更都触发新 plan，大图（数百节点）下是可测的 CPU 开销。未做实测，规模影响为估算。
  - 建议: 按 (node_type, params_hash) 缓存临时 Node / compute_input_roi 结果；params_hash 可用更廉价的 hash()（进程内一致即可，无需跨进程稳定）。
  - 置信度: 中

- [AUD-8420] [Suggestion] 类型注解缺失；RenderRequest 声称 Immutable 但非 frozen 且 __post_init__ 就地改字段
  - 位置: src/features/composite/render/render_scheduler.py:9（"Immutable request"）、152-157 与 271-282；:1274 advance_request_id、:1285 mark_current(request_or_id)、:1233 reset_dirty_tracking、:1247 prepare_for_export、:1357 record_plan_stats 均缺返回注解
  - 证据:
    ```python
    - RenderRequest: Immutable request with snapshot, request_id, and timestamp.
    ...
    @dataclass
    class RenderRequest:
        """Immutable render request submitted to the render thread.
    ...
        def __post_init__(self) -> None:
            ...
            self.quality_profile = profile      # 就地变更
    ...
        def mark_current(self, request_or_id):   # 无注解
    ```
  - 影响: 文档与实现不符（dataclass 非 frozen）；playback_drive: Optional[dict]、runtime_context/edit_project/view_mode_overrides: Any 等业务字段缺精确类型，request_or_id 无 Union[RenderRequest, int] 注解。
  - 建议: 或加 frozen=True（归一化移到工厂函数），或修正 docstring；补齐注解。
  - 置信度: 高

### 通过项
- 线程安全（ID 生成）：RequestIdGenerator.next_id 持锁自增，无重复 ID 风险
- 脏传播完备性：新节点 / param_revision / 参数内容 hash（P1-8）/ disabled / 拓扑 / color_revision / 动态源节点 / 帧号（P0-2）/ 端口级 revision + 下游 BFS 传播，各维度齐全且方向安全（偏向多渲染）
- prepare_for_export：单向 dict 拷贝，无共享可变状态（1260-1270）
- 异常路径：plan() 对缺节点（567-568）、缺 temp node（1006-1007）均有跳过兜底，不致崩溃
- ROI 传播正确性：多消费端 union 合并（1029-1037）、compute_input_roi 内处理扩展避免二次扩张（1026-1027 注释）
- 分类计数：counts 五类键齐全，无 KeyError

---

## src/features/composite/render/thumbnail_policy.py (行数: 134)

### 发现

（无）

### 通过项
- 通过（无逻辑问题）：纯函数无副作用，state 只读契约成立（调用方 composite_page.py:6111 负责回写 last_request_at/last_cache_key）；播放挂起 / 节流间隔 / 主视图优先（render_pending）/ 交互期仅选中节点 / cache-key 跳过 / max_nodes_per_tick 截断，逻辑顺序正确；min_interval 除零有 max(...,1) 保护（97-99）；类型注解完整；无线程/GPU/信号维度涉及
