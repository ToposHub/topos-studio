# 批次报告：batch-165 [P1] src/features/edit/core

- 审查日期：2026-09-28
- 编号区间：AUD-16500–AUD-16599（实际使用 AUD-16500–AUD-16509）
- 审查文件：`src/features/edit/core/edit_gpu_decode_manager.py`（3,037 行，已全文通读）
- 交叉验证（只读）：`src/features/edit/core/timeline_preview_renderer.py`（close_all / invalidate_all_sources / set_preview_quality 调用时序、解码执行线程）、`src/shared/media/disk_proxy_service.py`（get_disk_proxy_info 缓存）、`src/features/edit/facade.py`（管理器生命周期）

## src/features/edit/core/edit_gpu_decode_manager.py (行数: 3037)

### 发现

- [AUD-16500] [Critical] close/invalidate 三条关闭路径无视 P2-09 租约，解码中途关闭 VT/FFmpeg 会话 → C 层 use-after-free（SIGSEGV）风险
  - 位置: src/features/edit/core/edit_gpu_decode_manager.py:2984-3016（invalidate_all_sources；close_source:2878-2911、close_all:2926-2969 同构）
  - 证据:
    ```python
    # 2984-2987 / 2998（invalidate_all_sources：先清空 _sources/_source_refs，不看租约）
    self._source_cache.clear()
    sources = dict(self._sources)
    self._sources.clear()
    self._source_types.clear()
    ...
    self._source_refs.clear()
    ...
    # 3013-3016（随即在锁外逐个 close）
    try:
        source.close()
    ```
    而租约仅在 LRU 逐出路径被检查（1348: `if self._source_refs.get(key, 0) > 0: continue`）。
  - 影响: 租约契约（文件头 186-190 行注释："逐出只回收租约为 0 的会话，杜绝‘解码中途会话被关闭’的 C 层 use-after-free"）只对 `_evict_sources_if_over_cap` 生效；`invalidate_all_sources` / `close_all` / `close_source` 直接清空并 close。调用方验证：`timeline_preview_renderer.py:6443` 在 `set_preview_quality`（用户切画质，运行中）里直接调 `invalidate_all_sources()`，此刻 PreviewRenderWorker（2286-2296 行的 `get_frame`/`get_frame_with_policy`）与 `_decode_executor` 池线程（2463 行）可能正持租约在 C 层解码；`stop()` 虽先 `wait(5000)` 再 close_all，但对解码池用的是 `shutdown(wait=False, cancel_futures=True)`（1295 行）——运行中的 future 不会被取消也不被等待，随后 1301 行立即 `close_all()`。仓库自身注释（timeline_preview_renderer.py:1283-1287）已确认该并发会 "销毁正在使用的会话 → SIGSEGV（真实复现）"。属可致崩溃的渲染错误级缺陷。
  - 建议: 关闭路径在 close 前等待/校验租约——例如把 `_source_refs[key]>0` 的 key 推迟 close（等待事件或带超时重试），或为 invalidate 增加"排水"阶段（等在飞租约归零、join 解码 executor 的运行中 future），使租约契约覆盖全部三条关闭路径。
  - 置信度: 高（本文件代码事实与调用时序均已核读；崩溃模式为仓库注释自认）

- [AUD-16501] [Major] 交互预置位线程未纳入关闭等待注册表，仅 0.12–0.3s 协作等待，与播放预取线程的 2s join 保护不一致
  - 位置: src/features/edit/core/edit_gpu_decode_manager.py:751-756（线程创建，未注册）；2875-2876 / 2923-2924 / 2979-2980（close 时对 preposition 事件仅 0.12/0.3s 等待）
  - 证据:
    ```python
    # 751-756：_do_preposition 直接 start，未登记到任何 join 注册表
    t = threading.Thread(
        target=_do_preposition,
        daemon=True,
        name="DecodeInteractivePreposition",
    )
    t.start()
    # 对比播放预取（368-372）：注册后才 start
    #   with self._playback_prefetch_lock:
    #       self._playback_prefetch_threads.append(t)
    # 2876（close_source）：仅协作等待
    self._wait_for_events(preposition_events, timeout=0.3)   # _wait_for_events 总预算 0.3s、每事件 0.03s 粒度
    ```
  - 影响: close/invalidate 会先 bump generation 使 `_is_stale` 返回 True，但该中止是协作式的：`_decode_frame_interactive` 若正阻塞在单次 C 层解码内（如 8K/长 GOP 单帧）则无法及时返回；超 0.3s 后 `close_source`/`close_all`/`invalidate_all_sources` 即关闭该池克隆源，而 `_do_preposition` 仍在使用它 → 与 AUD-16500 同类的 C 层 use-after-free。播放预取线程对同一风险有 `_join_playback_prefetches(timeout=2.0)` 的强保护，交互预置位没有，保护标准不一致。
  - 建议: 把预置位线程纳入与 `_playback_prefetch_threads` 相同的注册表 + join 机制（或复用 `_join_playback_prefetches`），close 前等待其退出。
  - 置信度: 中（竞态窗口较窄，需解码耗时超过 0.3s；但机制差异与代码事实明确）

- [AUD-16502] [Major] `_playback_prefetch_pending` 检查-添加非原子，破坏"每条流同时最多一个预取线程"的 dedup 不变量
  - 位置: src/features/edit/core/edit_gpu_decode_manager.py:331-335（check-then-add，无锁）；355（解码线程 finally 中 discard）
  - 证据:
    ```python
    if source_key in self._playback_prefetch_pending:
        return
    if getattr(self, "_closing", False):
        return
    self._playback_prefetch_pending.add(source_key)
    ...
    # _run_prefetch 的 finally（另一线程）：
    self._playback_prefetch_pending.discard(source_key)
    ```
  - 影响: 多线程预览（文件 249-251 行注释自述"multi-track preview can decode layer streams in parallel"）下，两个线程可在对方 add 前都通过 `in` 检查，为同一 source_key 各起一个预取线程：同一 VT 源上重复预取/解码，浪费解码预算并扰动 reorder 缓冲；dedup 注释承诺（"每条流同时最多一个预取线程"）被打破。另有 add/discard 与检查间的 TOCTOU 重启窗口。
  - 建议: 在 `self._lock` 内完成 检查+add（与 `_lease_source` 同一临界区），或将 pending 改为 `set` 的原子占用模式；discard 保持现状即可。
  - 置信度: 中（竞态成立取决于并发同流请求；后果为性能损耗而非崩溃）

- [AUD-16503] [Major] `_closing` 标志不阻止创建路径，close_all 之后迟到解码会重建并永久泄漏 VT/FFmpeg 会话
  - 位置: src/features/edit/core/edit_gpu_decode_manager.py:2915（close_all 置 True 后永不复位）；1344（仅逐出路径检查 `_closing`）；2352-2356（`_get_or_create_source` 无 `_closing` 检查仍写入 `_sources`）
  - 证据:
    ```python
    # 1344：_closing 只影响逐出
    if self._closing or len(self._sources) <= _MAX_LIVE_SOURCES:
        return victims
    # 2351-2357：插入路径不看 _closing
    else:
        self._sources[source_key] = source
        self._source_types[source_key] = source_type
        self._source_last_used[source_key] = time.perf_counter()
        self._lease_source(source_key)
        victims = self._evict_sources_if_over_cap()   # _closing=True → 永不逐出
    ```
  - 影响: `stop()` 对渲染线程 `wait(5000)` 超时（仅 warning，1290 行）或解码池运行中 future 在 close_all 后仍调用 get_frame 时，`_get_or_create_source` 会重新创建会话并写入 `_sources`；此后管理器已"关闭"（无人再调 close_all），`_closing=True` 反而禁用逐出 → VT 解码会话与文件句柄泄漏直到进程退出。`warmup_source`/`get_frame` 慢路径同样无 `_closing` 检查。
  - 建议: `_get_or_create_source` 与 `warmup_source` 入口在 `_closing` 时直接返回 None/跳过；或提供 `_reset_closing` 语义使 close_all 后实例明确不可再用。
  - 置信度: 中（需 stop 超时或线程越界存活才触发，但泄漏路径真实存在）

- [AUD-16504] [Suggestion] `get_frame_with_policy` 的段注释与 docstring 仍描述已删除的 'placeholder' 拒绝行为，与实现矛盾
  - 位置: src/features/edit/core/edit_gpu_decode_manager.py:396-398（段注释）；421-423（docstring）；441-448（实际实现仅 telemetry）
  - 证据:
    ```python
    # - source_kind == PLACEHOLDER/NONE 时拒绝 master 解码（realtime 严格禁止）
    #   并返回 (None, 'placeholder')，让 engine 走 hold/rebuffer。
    ...
    Returns:
        (frame, frame_type). When denied, frame_type == 'placeholder'
    ...
    # 实际实现：
    if sk_val in ("placeholder", "none"):
        playback_trace("decode_manager.realtime_no_proxy_fallback_decode", ...)
    return self.get_frame(...)
    ```
  - 影响: Final-2 修正（425-449 行注释）已把拒绝降级为遥测，但上方段注释与 Returns 文档未同步；依赖文档的维护者/调用方（timeline_preview_renderer.py:895、5910、7642 的注释同样引用旧行为）会误判 realtime 阻塞语义。
  - 建议: 删除/改写 396-400 行段注释与 docstring 中 'placeholder' 返回值描述，明确"仅遥测、不阻塞"。
  - 置信度: 高

- [AUD-16505] [Suggestion] 播放预取线程 `except Exception: pass` 完全静默，违反"不得宽泛 except 掩盖领域错误"约束
  - 位置: src/features/edit/core/edit_gpu_decode_manager.py:352-353
  - 证据:
    ```python
                try:
                    source.prefetch_sequential_frame(frame_index + 1)
                finally:
                    self._stream_lock(source_key).release()
            except Exception:
                pass
    ```
  - 影响: 预取失败（含源被关闭后的对象错误、VT 会话错误）无任何日志/trace，问题只表现为"下一帧偶发变慢"，无法在 playback_trace 中定位；与同文件其它异常路径（均有 playback_trace/debug 日志）标准不一。
  - 建议: 至少 `logger.debug(...)` 或 `playback_trace("decode_manager.playback_prefetch_error", ...)` 记录异常类型。
  - 置信度: 高

- [AUD-16506] [Suggestion] `preposition_interactive_frame` 在 `t.start()` 之前登记 busy 预约与 done_event，start 失败则两者永久泄漏
  - 位置: src/features/edit/core/edit_gpu_decode_manager.py:663-667（锁内登记）；751-756（锁外 start，无 try）
  - 证据:
    ```python
    if best is not None:
        _, _index, selected_source, selected_type = best
        self._interactive_busy_sources[id(selected_source)] = selected_source
        done_event = threading.Event()
        self._interactive_prefetch_events.add(done_event)
    ...
    t = threading.Thread(target=_do_preposition, daemon=True, ...)
    t.start()
    ```
  - 影响: 线程启动失败（资源耗尽时 `RuntimeError: can't start new thread`）会：a) 该克隆源的 busy 预约永不释放（`_select_interactive_source`/逐出永远跳过它；若 id() 后被新对象复用还可能误判新源 busy）；b) done_event 永不 set 且残留于 `_interactive_prefetch_events`，之后每次 close 的 `_wait_for_events` 都白等 0.12s。异常还会直接抛给调用方。
  - 建议: 将 `t.start()` 包入 try，失败时在 finally 语义里释放 busy 预约并 discard/set done_event。
  - 置信度: 中（触发概率低，但登记-启动次序缺陷明确）

- [AUD-16507] [Suggestion] 探测缓存清理不一致：`invalidate_all_sources` 清 codec/topos 却保留 pix_fmt/resolution/route 缓存；`close_source` 完全不清 per-asset 探测缓存
  - 位置: src/features/edit/core/edit_gpu_decode_manager.py:2994-2995（清 codec/topos）；2802-2815（`_pixfmt_cache`）与 2786-2800（`_resolution_cache`）、1594（`_playback_route_logged`）不在清理之列；close_source（2861-2911）无任何探测缓存清理
  - 证据:
    ```python
    self._codec_cache.clear()
    self._topos_movie_cache.clear()
    # P2-09：画质切换后文件环境可能变化，租约/LRU/负缓存一并清空，
    # 让所有素材重新探测（负缓存不清会永久钉死修复过的素材）。
    self._source_refs.clear()
    self._source_last_used.clear()
    self._failed_source_keys.clear()
    # _pixfmt_cache / _resolution_cache / _playback_route_logged 未清
    ```
  - 影响: 同一路径文件被替换（重链/重导出）时，`_playback_vt_policy`（1552-1577 用 pix_fmt 定路由）与 `_should_preserve_native_planar_size`（1636 用 resolution）会继续用旧文件的探测结果做路由决策，与管理器"让所有素材重新探测"的既定意图不一致；`close_source` 作为 per-asset 重置入口同样残留旧探测。
  - 建议: invalidate_all_sources 一并清 `_pixfmt_cache`/`_resolution_cache`/`_playback_route_logged`；close_source 按 `key[1]==asset_path` 顺带清该资产的四类探测缓存。
  - 置信度: 高（代码事实确定；实际影响依赖文件替换场景）

- [AUD-16508] [Suggestion] 私有符号跨模块导入 `_infer_bit_depth`；多处容器类型注解未参数化
  - 位置: src/features/edit/core/edit_gpu_decode_manager.py:1555；151、207、210、924、2337
  - 证据:
    ```python
    from src.shared.media.ffmpeg_source import _infer_bit_depth
    ...
    self._failed_source_keys: set = set()          # 可为 set[_SourceKey]
    self._playback_prefetch_pending: set = set()   # 可为 set[_SourceKey]
    self._playback_prefetch_threads: list = []     # 可为 list[threading.Thread]
    ```
  - 影响: 依赖 ffmpeg_source 的私有函数形成隐式耦合（上游重命名即断）；未参数化容器注解降低可读性与类型检查覆盖（项目规范要求强类型）。
  - 建议: 在 ffmpeg_source 提供公开包装（如 `infer_bit_depth`）后改导入；补齐容器项类型注解。
  - 置信度: 高

- [AUD-16509] [Suggestion] `warmup_source` 中对同一条件的连续重复 if 块冗余
  - 位置: src/features/edit/core/edit_gpu_decode_manager.py:880-898
  - 证据:
    ```python
    with self._lock:
        generation = self._warmup_generation
        if source_key in self._source_cache or source_key in self._sources:
            existing_source_available = True
            existing_source_type = self._source_types.get(source_key)
            if existing_source_type is None and source_key in self._source_cache:
                existing_source_type = self._source_cache[source_key][1]
        if source_key in self._source_cache or source_key in self._sources:
            should_start_pool = (
                warm_interactive_pool
                and existing_source_type in ('vt_pixel_buffer', 'vt_planar')
            )
            warmup_event = None
    ```
  - 影响: 同一条件两次求值（锁内多余计算），且 `existing_source_available` 变量与第二个 if 职责重叠，阅读时易误以为两块语义不同；无行为错误，纯代码质量。
  - 建议: 合并为单个 if 块，删除 `existing_source_available` 或由 `should_start_pool`/`warmup_event` 表达全部分支。
  - 置信度: 高

### 通过项

- GLSL/HDR 正确性：本文件不含 GLSL/色彩处理；解码路由保留原生分辨率与 planar YUV（`_should_preserve_native_planar_size`），无不当 clamp 或精度降级
- 信号防递归：文件为纯 Python 解码管理器，无 Qt 控件/信号，不适用（无问题）
- 状态提交一致性（undo/redo/revision/持久化）：文件不持有可提交编辑状态，不适用（无问题）
- 输入验证：`_normalize_target_size` 对非法/非正尺寸回退 None；`topos://` 虚拟素材在 `_create_source`/`_probe_codec`/`_is_topos_movie_path` 提前短路；`frame_index` 非法由下游解码失败回退 None 处理
- 性能模式：probe 缓存（codec/pixfmt/resolution/topos）+ 负缓存上限 + LRU 会话池上限 + 交互优先级让位（`_acquire_stream_lock_background`）设计自洽；`_get_disk_proxy_info` 每帧调用但服务侧 `_info_cache` 有缓存（已核读 disk_proxy_service.py），热路径无逐帧磁盘探测
- 租约/预约配对：逐一核对 get_frame、get_frame_interactive、preposition_stream_frame、warmup、池预置位的 `_lease_source`/`_release_source` 与 busy 预约路径，取出-归还均单次配对，`max(0, ...)` 钳制防负计数
- warmup 去重与 generation 失效：`_do_warmup` 直接调 `_create_source` 避免自等死锁的注释与实现一致；generation 失效与竞速输家源关闭（loser/victims 锁外 close）正确
- 线程安全（其余面）：`_stream_locks` 创建、`_interactive_busy_sources` 身份比较（id 复用防护）、`_playback_prefetch_threads` 注册表 + join、`_interactive_prefetch_events` 均在 `self._lock` 内操作；无锁字段（`_playback_active`、`_last_interactive_read_at`）为 GIL 原子的 bool/float 单写多读，无撕裂风险
- 异常路径（其余面）：`_decode_frame`/`_decode_frame_interactive` 对 StopIteration 与一般异常均有 trace + 降级返回；`preposition_stream_frame` 全程 try/finally 保证 inflight 事件与租约释放
- 类型注解：公开方法与内部函数签名注解完整（缺失项仅容器参数化，见 AUD-16508）
