# batch-388 审查报告 [P2] src/shared/video//

- 编号区间：AUD-38800–AUD-38899
- 审查文件：1 个（decode_worker.py，1,526 行，全文通读）
- 关联模块核对（只读）：frame_queue.py、frame_request.py、frame_provider_protocol.py、media/frame.py（DecodedFrame）、media/gpu_frame.py（GpuDecodedFrame）、video/session.py（调用方）

## src/shared/video/decode_worker.py (行数: 1526)

### 发现

- [AUD-38801] [Major] 帧去重标记的检查与置位不原子，同帧可被重复解码（check-then-act 竞态）
  - 位置: src/shared/video/decode_worker.py:661-679（配合 481-491、1057-1064）
  - 证据:
    ```python
    request = self.get_next_request()          # 临界区1：_request_lock 内弹出
    if request is not None:
        ...
        frame_index = request.frame_index
        with self._request_lock:               # 临界区2：再次加锁才置在途标记
            self._decoding_frames.add(
                (request.generation_id, frame_index)
            )
    ```
    而 `request_frame` 在另一个线程用独立的一次 `_request_lock` 检查（decode_worker.py:485）：
    ```python
    if (generation_id, frame_index) in self._decoding_frames:
        ...
        return
    ```
  - 影响: 弹出请求与置位 `_decoding_frames` 之间是两次独立加锁，期间并发的 `request_frame`/`request_prefetch` 查不到在途标记，会把同一 (generation, frame) 再次入队，随后被二次解码、二次入队（frame_queue 按帧号替换可兜底，损害是浪费解码与队列抖动）。`MasterDecodeWorker.request_master_frame`（1057-1064 检查 `_request_lock`、1078-1089 才在 `_master_request_lock` 下置请求）同理存在等价窗口。
  - 建议: 将"取出请求 + 置在途标记"合并到同一次 `_request_lock` 临界区内（如在 `get_next_request` 内原子完成标记），master 专用槽位也纳入同一把锁或统一走待处理队列。
  - 置信度: 高（竞态窗口逻辑确定；触发频率取决于并发请求强度，影响受 frame_queue 去重约束）

- [AUD-38802] [Major] stop() 超时后状态为 ERROR，再次 start() 会清除停止事件并产生双工作线程
  - 位置: src/shared/video/decode_worker.py:341-382（配合 310-339）
  - 证据:
    ```python
    if self._thread.is_alive():
        logger.warning(f"[{self._name}] Worker stop timeout after {timeout}s")
        with self._state_lock:
            self._state = WorkerState.ERROR      # 旧线程仍存活，_thread 未置 None
        return False
    ```
    而 start() 仅拦截 RUNNING/STOPPING，并清除停止事件：
    ```python
    if self._state in (WorkerState.RUNNING, WorkerState.STOPPING):
        return False
    self._stop_event.clear()                     # 旧线程从此再也看不到停止标志
    self._thread = threading.Thread(target=self._run_loop, ...)
    ```
  - 影响: stop 超时（provider 解码挂起 >2s）后经公共 API 再次 start()，旧线程一旦从挂起中恢复会因 `_stop_event` 被清除而永不退出，与新线程形成两个并发 run loop：同时消费 `_pending_requests`、并发调用同一 provider 的 `get_proxy_frame`/`get_master_frame`，可能导致解码器状态错乱。当前仓库内 unload 路径会将 manager 置 None 缓解了直接触发，但 API 契约本身不安全。
  - 建议: start() 拒绝 ERROR 状态（或先 join 旧线程确认退出后再重启）；stop 超时路径也应尝试回收 `_thread` 引用并保留停止标志不被清除。
  - 置信度: 中（逻辑确定；仓库内现有调用序列下较难直接触发）

- [AUD-38803] [Suggestion] DecodeWorkerStats 跨线程读写无锁，与模块"所有状态变更加锁"的声明不符
  - 位置: src/shared/video/decode_worker.py:134-146、299-302、1497-1515
  - 证据:
    ```python
    def update_decode(self, decode_ms: float, frame_index: int) -> None:
        self.frames_decoded += 1
        self.decode_total_ms += decode_ms
        self.decode_avg_ms = self.decode_total_ms / self.frames_decoded
    ```
    （worker 线程调用 update_*；管理器线程经 `stats.to_dict()`（decode_worker.py:1507-1510）无锁读取）
  - 影响: `get_stats()` 可能读到多字段不一致的快照（如 frames_decoded 已增而 decode_total_ms 未增）；仅监控指标失真，无功能损害。
  - 建议: 为 DecodeWorkerStats 增加内部锁，或仅在快照时加锁拷贝。
  - 置信度: 高

- [AUD-38804] [Suggestion] `_last_clock_frame`、`_prefetch_lookahead`、`_current_decode_frame` 跨线程读写无同步
  - 位置: src/shared/video/decode_worker.py:943-944、951-959、914、1183
  - 证据:
    ```python
    if current_frame != self._last_clock_frame:      # 时钟/会话线程
        self._last_clock_frame = current_frame
    ...
    self._prefetch_lookahead = max(1, value)          # UI/管理器线程
    ...
    self._current_decode_frame = frame_index          # worker 线程
    ```
  - 影响: GIL 下单个 int 读写原子，实际风险极低；但模块文档声明"所有状态变更通过 threading.Lock 保护"，此处为文档与实现不一致，后续演进易引入真竞态。
  - 建议: 补充锁保护或修正文档声明；`_current_decode_frame` 目前无读者，可删除或改为 stats 字段。
  - 置信度: 高

- [AUD-38805] [Suggestion] 用 `object.__setattr__` 动态注入 `is_proxy` 属性，对 GpuDecodedFrame 是未声明字段
  - 位置: src/shared/video/decode_worker.py:896、1165
  - 证据:
    ```python
    object.__setattr__(frame, 'is_proxy', True)
    ...
    object.__setattr__(frame, 'is_proxy', False)
    ```
  - 影响: `DecodedFrame`（media/frame.py:338）声明了 `is_proxy` 字段，但 `_enqueue_frame` 明确支持 GpuDecodedFrame（media/gpu_frame.py:293-343），该 dataclass 没有 `is_proxy` 字段，当前靠动态 `__dict__` 注入生效；一旦其引入 `__slots__` 或实现变更，将抛 AttributeError 并被 `_process_request` 的宽泛 except 吞掉，帧静默丢失。
  - 建议: 在构造侧显式设置该标记（或通过 QueueEntry.source 判定 proxy/master，下游已可用 FrameSource 区分），避免运行时打补丁式属性注入。
  - 置信度: 高（字段缺失为事实；崩溃为潜在情景）

- [AUD-38806] [Suggestion] DecodeWorkerManager.update_generation 不同步帧队列的 generation，与 increment_generation 不对称
  - 位置: src/shared/video/decode_worker.py:1391-1431
  - 证据:
    ```python
    def increment_generation(self) -> int:
        ...
        self._frame_queue.increment_generation()      # 同步队列
        ...
    def update_generation(self, generation_id: int) -> None:
        ...
        self._proxy_worker.update_generation(generation_id)
        self._master_worker.update_generation(generation_id)
        # （无任何队列同步；ReadyFrameQueue 也未提供 set-generation API）
    ```
  - 影响: 当前唯一调用方 session.py:1251-1259 先自行 `self._frame_queue.increment_generation()` 再调 `update_generation`，恰好保持一致；契约完全依赖调用方自觉，未来新调用方漏掉队列同步会使 frame_queue 的过期帧过滤（frame_queue.py:335）用旧 generation 判定，seek 后旧帧可能被接收。
  - 建议: 统一 generation 单一入口（manager 内同时推进队列），或给 ReadyFrameQueue 增加 set/update 方法并在此调用。
  - 置信度: 高（不对称为事实；损害为潜在）

- [AUD-38807] [Suggestion] 多处宽泛 `except Exception` 且 error 日志不带堆栈，掩盖领域错误根因
  - 位置: src/shared/video/decode_worker.py:698-700、916-918、1185-1187、1300-1302、1379-1388
  - 证据:
    ```python
    except Exception as e:
        logger.error(f"[{self._name}] Worker loop error: {e}")
        self._stats.update_error()
    ```
  - 影响: 工作线程保持存活继续运行是合理设计，但仅记录 `{e}` 不带 `exc_info`/堆栈，解码器抛出的领域异常（类型错误、越界、状态错）根因不可追溯；`pause_workers` 中以 debug 级别吞异常，生产日志不可见。
  - 建议: 统一加 `exc_info=True`（至少 error 级路径），并尽量收窄捕获范围。
  - 置信度: 高

- [AUD-38808] [Suggestion] request_frame / request_master_frame 不校验 frame_index 上界
  - 位置: src/shared/video/decode_worker.py:450-479、1034-1075（对照 548）
  - 证据:
    ```python
    def request_frame(self, frame_index: int, generation_id: int, ...) -> None:
        ...
        request = VideoFrameRequest(frame_index=frame_index, ...)
    ```
    VideoFrameRequest 仅校验非负（frame_request.py:137-138）；`request_prefetch` 有 clamp（decode_worker.py:548 `end_frame = min(current_frame + lookahead, total_frames - 1)`），单帧请求路径没有。
  - 影响: 越界帧号直接传给 provider，依赖 provider 返回 None 兜底，表现为 warning + error_count，浪费一次解码调度。
  - 建议: 在 worker 入口用 `self._frame_provider.total_frames` 对单帧请求做同样的边界钳制/拒绝。
  - 置信度: 高

- [AUD-38809] [Suggestion] 空闲期 100Hz 轮询唤醒，且 DECODE_INTERVAL_SECONDS 常量从未使用
  - 位置: src/shared/video/decode_worker.py:95-98、690-696
  - 证据:
    ```python
    IDLE_WAIT_SECONDS: float = 0.01
    DECODE_INTERVAL_SECONDS: float = 0.005      # 定义后全文件无引用
    ...
    self._request_event.wait(timeout=IDLE_WAIT_SECONDS)
    self._request_event.clear()
    ```
  - 影响: 事件 set/clear 配对本身无丢失唤醒问题（可证明安全），10ms 超时纯属轮询：两个 worker 空闲时合计约 200 次/秒无意义唤醒并反复加锁查队列；死常量误导维护者以为存在解码限速。
  - 建议: 空闲等待改为无超时阻塞（事件语义已足够），删除未使用的 DECODE_INTERVAL_SECONDS；`get_next_request` 的 `pop(0)` 为 O(n)，当前 n≈预读窗口大小可接受，无需改 deque。
  - 置信度: 高

- [AUD-38810] [Suggestion] start_workers 部分失败不回滚；stop_workers 串行 join 最坏耗时 2×timeout，超出 2 秒退出设计指标
  - 位置: src/shared/video/decode_worker.py:1304-1327、1340-1369
  - 证据:
    ```python
    if self._proxy_worker is not None:
        proxy_stopped = self._proxy_worker.stop(timeout=timeout)
    if self._master_worker is not None:
        master_stopped = self._master_worker.stop(timeout=timeout)
    ```
  - 影响: 默认 timeout=2.0 时两个 worker 串行停止最坏 4s，与模块文档"stop/unload 后线程能在 2 秒内退出"（decode_worker.py:20、351-352）不符；start_workers 半成功（proxy 起、master 败）返回 False 但不停掉已启动的 proxy，把清理责任留给调用方。
  - 建议: 并行 join（两个 stop_event 先全部 set 再依次 join），或按剩余时间预算分配 timeout；start 半失败时回滚已启动的 worker。
  - 置信度: 中（最坏路径需两 worker 同时挂起，但契约违背确定）

- [AUD-38811] [Suggestion] MasterDecodeWorker API 一致性：优先级硬编码 SEEK 忽略 reason；has_pending_requests 不考虑 master 专用槽位
  - 位置: src/shared/video/decode_worker.py:1066-1067、632-640（对照 1097-1114）
  - 证据:
    ```python
    # master 请求有最高优先级
    priority = RequestPriority.SEEK  # SEEK = 50, 高优先级
    ```
    而 reason 可为 COMPOSITE(70)/EXPORT(100)；`has_pending_requests`（基类）只检查 `_pending_requests`，不含 `_pending_master_request`。
  - 影响: 当前 master 请求走专用槽位优先处理，错误优先级尚无实际调度影响，但请求对象携带失真的 priority 元数据；`has_pending_requests` 在仅剩 master 请求时返回 False，未来调用方据此判断空闲会出错（当前仓库内无调用方）。
  - 建议: priority 用 `RequestPriority.get_default(reason)`；`has_pending_requests` 重写以纳入 master 槽位。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：`_enqueue_frame` 返回 False（过期 generation）路径由 frame_queue.enqueue 内部 `safe_release_gpu_frame` 释放 GPU 帧（frame_queue.py:335-342），worker 侧无泄漏；模块不直接创建/持有 ModernGL 对象，符合"Worker 禁止接触 UI 控件和 ModernGL"约束
- GLSL·HDR 正确性：不适用——模块不涉及着色器与像素数据运算，仅透传解码帧，无 clamp(0.0,1.0) 风险点
- 信号防递归：不适用——无 Qt 控件/信号，UI 解耦通过队列与事件完成
- 状态提交一致性（undo/redo/revision/持久化）：不适用——无撤销栈与持久化职责
- 异常路径（部分）：`_run_loop` 中 `_decoding_frames` 置位后用 try/finally 保证移除（decode_worker.py:681-689），异常不会遗留卡死帧标记；generation 提升时的批量清除（V-P0-05）与 (generation, frame) 复合键去重（V-P0-04）修复逻辑正确
- 取消/过期链路：请求三重过期防护（loop 检查 665、_process_request 复查 871/1131、frame_queue.enqueue 兜底 335）完整，seek 后旧帧丢弃可靠
- 死锁审查：锁序一致——update_generation 刻意不嵌套 `_generation_lock` 与 `_request_lock`（decode_worker.py:423 有注释），`_run_loop` 内各锁无嵌套获取
- 输入验证（部分）：frame_provider/frame_queue 缺失时抛 ValueError（839-841、1017-1019、1247-1250），废弃参数走 warning 降级路径明确
- 类型注解：公共方法/属性均有完整注解（含返回类型 `dict[str, float | int]` 等），符合规范要求
