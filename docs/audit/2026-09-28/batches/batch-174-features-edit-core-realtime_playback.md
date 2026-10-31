# Batch-174 审查报告：src/features/edit/core/realtime_playback//

- 任务书：docs/audit/2026-09-28/assignments/batch-174.md（编号区间 AUD-17400–AUD-17499）
- 文件数：8（2,754 行），全部 Read 通读
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失
- 交叉验证：timeline_gpu_compositor.py（GpuFrameHandle 定义）、clip_decode_stream.py / frame_ring_buffer.py（调度器调用的 API 签名）、types.py（FrameIdentity / SourceKind / PlaybackMode）、engine.py 与 timeline_preview_renderer.py（policy/resolver/budget 的真实消费路径）、effect_instance.py（to_dict 语义）、video_upload.py（纹理释放模型）
- 汇总：Critical 0 / Major 5 / Suggestion 18

---

## src/features/edit/core/realtime_playback/gpu_frame_pool.py (行数: 276)

### 发现

- [AUD-17400] [Major] 池缓存命中返回共享 handle，而模块文档教调用方 `gpu_frame.release()`，会释放仍驻留池中的纹理（use-after-release / 双重释放）
  - 位置: src/features/edit/core/realtime_playback/gpu_frame_pool.py:148-152（对照 41-46、206-220、226-241）
  - 证据:
    ```python
    with self._lock:
        cached = self._resident.get(cache_key)
        if cached is not None:
            self._cache_hits += 1
            return cached
    ```
    模块文档（41-46 行）：
    ```python
    gpu_frame = pool.ensure_gpu_frame(frame_handle, generation_id=1)
    ... 使用 gpu_frame.texture_ids ...
    gpu_frame.release()  # 或 pool.release_generation(1) 批量释放
    ```
  - 影响: 同一 cache key 的 handle 是池内单例：调用方按文档执行 `gpu_frame.release()` 后，条目仍在 `_resident` 中，下一次 `ensure_gpu_frame` 命中缓存返回已释放的 texture_ids → GL 采样无效纹理（渲染错误帧/黑帧，可能触发 GL 错误）；随后容量淘汰或 `release_generation` 会再次调用 `release()` 造成双重释放。池没有引用计数或"借用/归还"所有权模型，两条释放路径（调用方 release 与池淘汰）直接冲突。
  - 建议: 明确所有权语义：要么池独占释放（删除文档中的调用方 release 用法，命中返回不可释放的只读视图），要么改为按调用方借出引用计数，最后一个引用归还时才真正 release 并从 `_resident` 移除。
  - 置信度: 高

- [AUD-17401] [Major] 默认/external 上传路径产出的 handle `release=None`，容量淘汰、release_generation、shutdown 全部跳过释放 → GPU 纹理泄漏
  - 位置: src/features/edit/core/realtime_playback/gpu_frame_pool.py:86-98、100-117（release=None）；206-220、226-241、250-261（释放路径均要求 `handle.release is not None`）
  - 证据:
    ```python
    # external texture（macOS VT IOSurface 等）直接包装
    ...
                generation_id=0,
                release=None,
            )
    ...
    # planar / cpu payload：尝试通过 video_upload 上传
    ...
                    texture_ids=(int(tex_id),),
                    texture_type="rgb",
                    ...
                    release=None,
    ```
    释放路径（214-219 行）：
    ```python
    handle = self._resident.pop(k, None)
    if handle is not None and handle.release is not None:
        try:
            handle.release()
    ```
  - 影响: `_default_upload` 是默认上传实现（`self._upload_fn or self._default_upload`，155 行），它产出的所有 handle 都没有 release 回调；`_enforce_capacity_locked` 只会把条目 pop 掉而无法释放底层 GL 纹理，`release_generation`/`shutdown` 同样。长时间播放下池上限 64 的滚动淘汰将持续泄漏 GPU 纹理，直接违反模块自身声明的不变量"不每帧创建大量 texture 后不释放"（§12.3）以及"external texture 必须有明确 release 语义"。另注：文档用法 `gpu_frame.release()` 在 release=None 时是调用 None → TypeError。当前生产侧仅 attach 后调用 stats()，ensure_gpu_frame 无调用方，属潜伏缺陷。
  - 建议: `_default_upload` 在取得 `result.texture_id` 时同时捕获其释放回调（video_upload 已有 release 模型，见 video_upload.py:578-595）；external 纹理要求 payload 提供 release 语义或显式登记外部所有者，禁止 release=None 入池。
  - 置信度: 高

- [AUD-17402] [Suggestion] `_gl_calls_from_decode_thread` 统计从未被递增，恒为 0，规格书 §25 断言指标失效
  - 位置: src/features/edit/core/realtime_playback/gpu_frame_pool.py:59、275
  - 证据:
    ```python
    self._gl_calls_from_decode_thread = 0  # 必须 0（规格书 §25）
    ...
    "gl_calls_from_decode_thread": self._gl_calls_from_decode_thread,
    ```
  - 影响: 该计数器没有任何递增点，"必须为 0"的断言永远通过，无法侦测 decode 线程违规调用 GL 的情况，给人已监控的假象。
  - 建议: 删除该指标，或在上传路径中真实检测调用线程（如记录创建线程并在 release/ensure 时比对）。
  - 置信度: 高

- [AUD-17403] [Suggestion] `_default_upload` 宽泛 except 吞掉上传失败（ImportError 静默 pass、Exception 仅 debug 日志），失败不可归因
  - 位置: src/features/edit/core/realtime_playback/gpu_frame_pool.py:118-121、156-160
  - 证据:
    ```python
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"_default_upload failed: {e}")
    ```
  - 影响: 违反 .clinerules"不得宽泛 except 掩盖领域错误"：上传模块的任意异常（含纹理分配失败、数据格式错误）都被降级为 debug 日志，调用方只拿到 None，无法区分"payload 不支持"与"GPU 资源耗尽"，排障困难。
  - 建议: 至少提升为 warning 并区分 ImportError（能力缺失）与运行异常（资源/数据错误）；对后者在 stats 中计数。
  - 置信度: 高

- [AUD-17404] [Suggestion] release_generation/shutdown 可在非 GL 线程调用 `handle.release()`（GL 删除操作），与"UI 线程不得操作 OpenGL"/deferred-release 约束冲突
  - 位置: src/features/edit/core/realtime_playback/gpu_frame_pool.py:226-241、250-261
  - 证据:
    ```python
    def release_generation(self, generation_id: int) -> int:
        """释放某 generation 的所有 GPU frame（seek 后）。"""
        released = 0
        with self._lock:
            ...
                if handle is not None and handle.release is not None:
                    try:
                        handle.release()
    ```
  - 影响: 模块只在 `ensure_gpu_frame` 上标注"必须在 GL/render 线程执行"，而 `release_generation`（docstring 标注 seek 后调用，seek 通常发生在 UI/engine 线程）和 `shutdown` 直接同步执行 release 回调；一旦 release 回调做 ModernGL 删除，就从非 GL 线程操作了 OpenGL 上下文。当前无外部调用方，属设计层面的潜伏风险。
  - 建议: 释放统一走 deferred-release 队列（提交到 GL 线程执行），或在方法契约中强制标注调用线程并加断言。
  - 置信度: 低（无现存调用方佐证实际线程）

### 通过项
- 线程安全：`_lock`（RLock）覆盖 `_resident` 的全部读写路径，统计字段在锁内更新
- cache key 完整性：`_build_cache_key` 纳入 identity 全字段 + pixel_format/bit_depth/尺寸/generation（与 FrameIdentity 实际字段核实一致），proxy/master 切换不会错命中
- `GpuFrameHandle` 为非 frozen dataclass（timeline_gpu_compositor.py:94），165 行 `gpu_frame.generation_id = generation_id` 赋值合法，无 FrozenInstanceError
- 无 GLSL/HDR 数据变换，无 clamp 问题
- 无 Qt 信号，无信号递归风险

---

## src/features/edit/core/realtime_playback/playback_budget.py (行数: 244)

### 发现

- [AUD-17405] [Suggestion] before_render 成功后若渲染路径异常中断，in-flight 永久占位且无超时/abort API → 后续所有帧被 drop、播放永久冻结
  - 位置: src/features/edit/core/playback_budget 路径 src/features/edit/core/realtime_playback/playback_budget.py:145-151（登记）、153-190（唯一清理点）、192-199（reset_pressure）
  - 证据:
    ```python
    if self._in_flight_start is not None:
        # 已有渲染在途，drop 本次避免堆积
        self._total_drops += 1
        return False
    self._in_flight_start = time.time_ns()
    self._in_flight_frame = frame
    return True
    ```
    in-flight 只在 `after_render`（164-165 行）与 `reset_pressure`（197-198 行）清除，两者都依赖调用方主动调用。
  - 影响: `before_render` 返回 True 到 `after_render` 之间若抛异常且调用方未兜底调用 after_render，`_in_flight_start` 永久非 None，之后每帧都命中 in-flight 分支被 drop，等效播放冻结；类本身没有超时看门狗或 abort_render API。当前唯一生产调用方 engine.py 已将 delegate 异常包在 `_execute_delegate` 内部（engine.py:886-930 try/except 后返回 False），after_render（engine.py:787）总是执行，故主路径安全；但该类是公开 API，第三方 delegate/直接使用 budget 时即暴露。
  - 建议: 为 in-flight 增加基于时间戳的超时（如超过 N 倍预算自动清除并告警），或提供 `abort_render(frame)` 故障路径 API。
  - 置信度: 中（类级缺陷确定，生产主路径已被 engine 兜底）

- [AUD-17406] [Suggestion] `should_defer_render` 是"查询"语义但内部委托 `before_render`，产生登记 in-flight / 计 drop 的副作用
  - 位置: src/features/edit/core/realtime_playback/playback_budget.py:205-207
  - 证据:
    ```python
    def should_defer_render(self, frame: int) -> bool:
        """UI tick 查询是否应推迟本次渲染（back-pressure 时 True）。"""
        return not self.before_render(frame)
    ```
  - 影响: 按文档"UI tick 查询"的用法，一次纯查询就会登记 in-flight；若调用方随后又正式调用 before_render 开始渲染，会被自己刚登记的 in-flight 拦截而 drop 一帧。当前仓库内未发现该方法的调用方，属 API 陷阱。
  - 建议: 拆分只读判断（`is_back_pressured` 已存在，可直接用）与有状态的准入（before_render），文档中注明 should_defer_render 即准入而非纯查询。
  - 置信度: 高（代码行为确定；实际影响取决于未来调用方）

- [AUD-17407] [Suggestion] after_render 不校验 frame 一致性且信任调用方 start_ns；`budget_ms`/`is_back_pressured` 之外的 `set_target_fps` 双写与无锁读存在轻微竞态
  - 位置: src/features/edit/core/realtime_playback/playback_budget.py:153-169、104-112
  - 证据:
    ```python
    def after_render(self, frame: int, start_ns: int, *, presented: bool) -> None:
        ...
        with self._lock:
            self._in_flight_start = None
            self._in_flight_frame = None
    ```
    ```python
    def set_target_fps(self, fps: float) -> None:
        with self._lock:
            self._target_fps = max(1.0, float(fps))
            self._budget_ns = int((1.0 / self._target_fps) * 1e9 * 0.8)
    ...
    @property
    def budget_ms(self) -> float:
        return self._budget_ns / 1e6
    ```
  - 影响: (1) `after_render` 忽略 `_in_flight_frame`，任何帧号的 after_render 都会清空 in-flight，误配调用会破坏并发防护，且 timing 使用调用方传入的 start_ns 而非自己登记的 `_in_flight_start`，传错则耗时统计失真；(2) `budget_ms` 无锁读 `_budget_ns`（CPython 单属性读原子，实际风险极低）。均为健壮性问题，不影响当前正确调用方。
  - 建议: after_render 校验 frame 与 _in_flight_frame（不一致记 warning），并优先使用内部登记的 start_ns；budget_ms 加锁或缓存单一派生值。
  - 置信度: 高（代码事实）/ 影响低

### 通过项
- back-pressure 状态机正确：连续超预算触发、回到预算内递减解除、探测帧间隔 `max(2, threshold)` 防止 threshold=1 时退化成忙循环（88-93 行注释与实现一致）
- probe 帧放行路径正确：`_pressure_drops` 重置后落入 in-flight 登记（140-144 行），探测帧仍会走 after_render 更新压力状态，不会自陷
- `stats()` 在 RLock 内复用 recent_avg_ms/p95_ms（可重入锁，无死锁）
- 无 GPU/Qt 资源；无信号递归问题

---

## src/features/edit/core/realtime_playback/policy.py (行数: 341)

### 发现

- [AUD-17408] [Suggestion] `get_default_policy` 惰性单例无锁，注释声称"线程安全由 GIL 保证"不准确
  - 位置: src/features/edit/core/realtime_playback/policy.py:333-341
  - 证据:
    ```python
    def get_default_policy() -> RealtimePlaybackPolicy:
        """获取默认 policy 单例（线程安全由 GIL 保证惰性初始化）。"""
        global _default_policy
        if _default_policy is None:
            _default_policy = RealtimePlaybackPolicy()
        return _default_policy
    ```
  - 影响: 两个线程可在 `if` 与赋值之间交错，各自构造一个实例（GIL 不保证 check-then-act 原子）。RealtimePlaybackPolicy 无状态，后果仅是丢失单例性，无功能错误；但注释的线程安全声明是错的，可能被后续有状态化改造继承。
  - 建议: 加模块级 Lock 双检锁，或修正注释明确"竞态下可能创建多实例，当前无害"。
  - 置信度: 高（代码事实）；影响低

### 通过项
- REALTIME 不变量成立：`_decide_realtime` 所有五个分支的 `allow_master_fallback` 均为 False（对照 140 行 `mode.allows_master` = `self is not PlaybackMode.REALTIME`，types.py:41-47 核实）
- `should_rebuffer` 的 MissReason 分类决策与文档一致：UNSUPPORTED_EFFECT 永不 rebuffer、PROXY_PENDING 立即 rebuffer、其余按连续 miss 阈值（319-326 行，`classify_miss_reason` 来自 types.py）
- 纯决策逻辑、无 Qt/解码依赖、快照 Protocol 解耦，输入全部显式参数，无隐藏状态
- 非实时分支 max_wait_ms（blocking 2000 / 否则 500）与 allows_blocking 匹配
- 注：policy 消费的 render_cache_state 全局语义缺陷见 AUD-17409（归入 render_cache_adapter.py），policy 侧对应行为位于本文件 172-182 行

---

## src/features/edit/core/realtime_playback/render_cache_adapter.py (行数: 435)

### 发现

- [AUD-17409] [Major] `has_render_cache_ready`/`render_cache_path` 是"任意 clip 就绪"的全局语义，而 policy 逐层查询，多 clip 时间线下会把 clip A 的缓存路径判给 clip B → 错误解码源/渲染错帧
  - 位置: src/features/edit/core/realtime_playback/render_cache_adapter.py:346-359（根因）；policy.py:172-182 与 source_resolver.py:259-275（误用链）
  - 证据:
    ```python
    @property
    def has_render_cache_ready(self) -> bool:
        """是否有任意 ready 的 render cache（policy 查询）。"""
        with self._lock:
            return any(e.is_ready for e in self._entries.values())

    @property
    def render_cache_path(self) -> Optional[str]:
        """返回第一个 ready 的 cache path（policy 查询）。"""
        with self._lock:
            for e in self._entries.values():
                if e.is_ready:
                    return e.cache_path
            return None
    ```
    SourceResolver 对每个 layer 传入同一 adapter 实例（source_resolver.py:261-262 `cache_state = self._render_cache`）；policy 命中后（policy.py:172-182）：
    ```python
    if render_cache_state is not None and render_cache_state.has_render_cache_ready:
        return SourceDecision(
            source_kind=SourceKind.RENDER_CACHE,
            source_path=render_cache_state.render_cache_path,
            ...
    ```
  - 影响: 只要时间线里任一 clip 有 ready 缓存，所有无缓存 layer 的 decision 都是 `RENDER_CACHE` 且 `source_path` 指向"第一个 ready"的其他 clip 缓存文件。该 decision 经 engine.py:757 → `_build_realtime_scheduler_config`（timeline_preview_renderer.py:7574-7580 取 `decision.source_path` 构造 `LayerSpec.source_path`）进入 `TimelineDecodeScheduler.configure`，驱动实际预取解码 → 从错误的 clip 缓存解帧，渲染错误内容；`SourceKind.RENDER_CACHE.is_realtime_ready` 为 True（types.py:71-76 核实），不会被 engine 拦截。缓解因素：当前生产构造 `SourceResolver(proxy_manager=DiskProxyServiceAdapter())` 未传 render_cache（timeline_preview_renderer.py:5889-5892），adapter 始终为空，且 `register_manifest`/`upsert_entry` 无生产调用方，缺陷处于潜伏状态——一旦按规格书 §8 接入 render cache 即触发。
  - 建议: 快照协议必须按 clip 查询：`RenderCacheStateSnapshot` 增加 `get_state(clip_id)`（或 policy 改为传入 per-clip entry），`render_cache_path` 按 clip_id 取对应 entry 的 cache_path 并校验 hash（现成的 `get_cache_entry(key)` 已具备正确语义，policy 应改用它）。
  - 置信度: 高（代码逻辑确定；生产触发条件当前未接线，已注明）

- [AUD-17410] [Major] `register_manifest` 硬编码 NTSC 帧率与 1080p 默认值，且 `get_cache_entry` 失效校验不比对 fps/cache_version/source_path 等关键字段
  - 位置: src/features/edit/core/realtime_playback/render_cache_adapter.py:310-320、361-372
  - 证据:
    ```python
    key = RenderCacheKey(
        ...
        frame_rate_num=24000,
        frame_rate_den=1001,
        width=getattr(manifest, "width", 1920),
        height=getattr(manifest, "height", 1080),
        render_hash=render_hash,
    )
    ```
    ```python
    def get_cache_entry(self, key: RenderCacheKey) -> Optional[RenderCacheEntry]:
        with self._lock:
            entry = self._entries.get(key.clip_id)
            if entry is None:
                return None
            # key 不匹配（参数变化）→ 失效
            if entry.key.render_hash != key.render_hash:
                return None
            if entry.key.width != key.width or entry.key.height != key.height:
                return None
            return entry
    ```
  - 影响: (1) 除非 manifest 恰好带这些属性，注册条目的 fps 恒为 23.976、分辨率恒为 1920x1080，key 中的 fps 字段形同虚设；(2) 查询校验只比对 render_hash/width/height，`frame_rate_num/den`、`cache_version`、`source_path`、`timeline_id` 变化不会判定失效，违反模块自身不变量"没有 hash/version 不复用缓存"（§8），跨工程/换源复用同一 clip_id 时可能命中陈旧缓存。
  - 建议: register_manifest 接受并透传真实 fps/尺寸（缺失时报错而非默认）；get_cache_entry 比对完整 `key.to_hash()` 或至少补上 fps、cache_version、source_path、timeline_id。
  - 置信度: 中（缺陷模式确定；生产调用方缺失，无法确认实际数据形态）

- [AUD-17411] [Suggestion] `_key_index`/`_last_keys` 只写不读（死状态），invalidate_by_clip 不清理索引
  - 位置: src/features/edit/core/realtime_playback/render_cache_adapter.py:294-297、330-340、392-398、400-405
  - 证据:
    ```python
    # key_hash -> clip_id（快速反查）
    self._key_index: Dict[str, str] = {}
    # clip_id -> 最后查询的 RenderCacheKey（用于 invalidate 判断）
    self._last_keys: Dict[str, RenderCacheKey] = {}
    ```
    所有查询路径（346-386 行）都遍历 `_entries` 或按 clip_id 取，从不读 `_key_index`/`_last_keys`；`invalidate_by_clip`（392-398 行）只改 status。
  - 影响: 死代码造成"有快速索引"的假象；注释声称 `_last_keys` 用于 invalidate 判断但 invalidate 实际未使用它，误导维护者。
  - 建议: 删除两个字段，或让 get_cache_entry 真正走 `_key_index`、invalidate 用 `_last_keys` 判断 hash 漂移。
  - 置信度: 高

- [AUD-17412] [Suggestion] `speed_params_signature` 对既无 `.speed` 又不可 `float()` 的输入抛 TypeError 未捕获
  - 位置: src/features/edit/core/realtime_playback/render_cache_adapter.py:86-89
  - 证据:
    ```python
    base_speed = getattr(speed_params, "speed", None)
    if base_speed is None:
        # 标量速度（旧模型）
        return f"scalar={float(speed_params):.6f}"
    ```
  - 影响: 若 speed_params 是不含 speed 属性且未实现 __float__ 的对象（外部数据未验证），float() 抛 TypeError 直接向上传播打断调用方。注释已声明该函数"尚未在生产路径调用"，故当前为低风险。
  - 建议: 包裹 try/except TypeError 并返回稳定降级签名（如 "invalid"），或在文档标注接受的输入类型。
  - 置信度: 中

### 通过项
- `build_effects_signature` 无模型变异风险：已核实 EffectInstance.to_dict 每次构造全新 dict（effect_instance.py:558-576），215 行 `data.pop("effect_id", None)` 只作用于该副本；排序 key 带 effect_id 仅用于稳定性，签名本体剔除 id（注释声明一致）
- `build_render_hash` 覆盖 §8.1 要求的全部参数段（source range/speed/transform/crop/opacity/effects/comp/color/transition/scale/retime）
- 线程安全：全部状态访问持 `self._lock`
- `open_cache_source` 失败降级为 warning + None，不抛出；`is_ready` 要求 status=valid 且 path 非空

---

## src/features/edit/core/realtime_playback/source_resolver.py (行数: 349)

### 发现

- [AUD-17413] [Suggestion] `_ensured_proxies` 集合无锁 check-then-add，跨线程调用可重复触发后台代理生成
  - 位置: src/features/edit/core/realtime_playback/source_resolver.py:221、299-315
  - 证据:
    ```python
    # 已 ensure 过的 source_path（避免重复触发后台生成）
    self._ensured_proxies: set[str] = set()
    ...
    if query.source_path in self._ensured_proxies:
        return
    # 检查是否已有 ready proxy
    entry = self._proxy_manager.get_proxy_entry(query.source_path)
    ...
    self._proxy_manager.ensure_proxy_async(...)
    self._ensured_proxies.add(query.source_path)
    ```
  - 影响: resolve_layers/ensure_proxy 可能在 tick 线程与 UI 线程并发；单个 add/discard 在 GIL 下原子，但"检查→触发→登记"整段不是，两线程可同时通过检查并重复调用 ensure_proxy_async，造成重复代理任务（DiskProxyService 侧是否去重未知）。影响小但违背去重意图。
  - 建议: 加一把小锁包住 check-then-add，或依赖 service 层幂等并在注释声明。
  - 置信度: 中（竞态窗口确定，实际并发调用路径未完全确认）

- [AUD-17414] [Suggestion] `DiskProxyServiceAdapter._resolve_service` 失败后把 `self._service` 固化为 None，服务恢复后不再重试；赋值无锁
  - 位置: src/features/edit/core/realtime_playback/source_resolver.py:155-165
  - 证据:
    ```python
        try:
            from src.shared.media.disk_proxy_service import get_disk_proxy_service
            self._service = get_disk_proxy_service()
        except Exception as e:
            logger.debug(f"DiskProxyServiceAdapter: get_disk_proxy_service failed: {e}")
            self._service = None
        return self._service
    ```
  - 影响: 首次解析失败（如循环 import 时序）后永久缓存 None，之后每帧查询/触发都直接放弃，代理状态查询静默失效为"无代理"（policy 走 PLACEHOLDER 分支），无恢复路径；`self._service` 的懒赋值也无锁（GIL 下引用赋值原子，风险低）。
  - 建议: 失败时不缓存 None（保留每次重试）或缓存"失败次数/退避"，并加锁保护懒初始化。
  - 置信度: 中

- [AUD-17415] [Suggestion] `bind_engine` 从外部写 engine 私有属性 `_source_resolver`（带 type: ignore），破坏封装且无类型契约
  - 位置: src/features/edit/core/realtime_playback/source_resolver.py:227-230
  - 证据:
    ```python
    def bind_engine(self, engine: Any) -> None:
        """把 resolver 注入 engine（engine 调用 resolve_layers）。"""
        # engine 持有 resolver 引用即可，这里做 type-erased 绑定
        engine._source_resolver = self  # type: ignore[attr-defined]
    ```
  - 影响: duck-typed 私有注入：engine 属性改名不会有任何静态检查报错，运行时静默失效（engine 拿不到 resolver 也不报错）；生产实际走 `configure(source_resolver=...)`（timeline_preview_renderer.py:5897-5901），bind_engine 是冗余的危险路径。
  - 建议: 删除该方法或要求 engine 实现显式 `set_source_resolver()` 公有接口。
  - 置信度: 高（设计问题确定）

### 通过项
- resolve_layers 异常路径：`_query_proxy_state` 捕获查询异常降级 None（不向热路径抛出），policy 对 None 快照有明确分支
- ensure_proxy 失败时不登记 `_ensured_proxies`（316-317 行 except 在 add 之前），保留重试可能，语义正确
- `_DiskProxyManifestEntry`/`_ProxyStateFromEntry` 适配层全部 getattr 容错 + 默认值，外部数据（DiskProxyInfo/ProxyManifestEntry）字段缺失不崩溃
- `configure()` 仅覆盖非 None 参数，不会意外清空已注入依赖
- 注：261-262 行对每层传同一 `self._render_cache` 是 AUD-17409 的共因，已在该条目记录

---

## src/features/edit/core/realtime_playback/task_priority.py (行数: 124)

### 发现

- [AUD-17416] [Suggestion] `EvictionVerdict.action` 用裸 str 而非 Literal/枚举，调用方拼写错误无法静态发现
  - 位置: src/features/edit/core/realtime_playback/task_priority.py:80-92
  - 证据:
    ```python
    @dataclass(frozen=True)
    class EvictionVerdict:
        """满队列丢弃裁决。"""

        action: str            # "accept" | "evict_pending" | "drop_incoming"
    ```
  - 影响: 协议只有注释约束词表，比较方写 `"evict_pendng"` 之类的笔误无任何检查；且注释列出 "accept" 但函数从不返回它（只返回 evict_pending/drop_incoming），文档与行为不一致。
  - 建议: 改为 `Literal["accept", "evict_pending", "drop_incoming"]` 或枚举；修正注释中 "accept" 的来源说明（或补充队列有空位时返回 accept 的语义）。
  - 置信度: 高

### 通过项
- 协议实现与文档一致：incoming < pending 与 == 均 evict_pending（latest wins）、> 则 drop_incoming（89-116 行），满足"cache fill 永远挤不掉 playhead"
- `resolve_request_priority` 未知 kind 保守映射 PLAYHEAD、`background=True` 强制 CACHE_FILL，与注释一致；`_REQUEST_KIND_PRIORITY` 键集合与词表声明匹配
- `priority_name` 对越界数值返回 "unknown"（ValueError 捕获），无崩溃路径
- 纯函数、无状态、无线程/GPU/信号维度问题

---

## src/features/edit/core/realtime_playback/telemetry.py (行数: 289)

### 发现

- [AUD-17417] [Suggestion] summary() 空窗口分支与非空分支的键名不一致：`averaged_total_ms` vs `total_ms_mean`
  - 位置: src/features/edit/core/realtime_playback/telemetry.py:207-222（空分支，211 行）与 248-274（非空分支，251 行）
  - 证据:
    ```python
    if not records:
        return {
            "frames": 0,
            "window": self._window,
            "averaged_total_ms": 0.0,
            ...
    ```
    ```python
        return {
            "frames": window_frames,
            "window": self._window,
            "total_ms_mean": round(avg_total, 3) if totals_ms else 0.0,
            ...
    ```
  - 影响: 同一指标两个键名，消费方（benchmark/回归断言/调试面板）在会话首帧（窗口为空）取 `total_ms_mean` 会得到 KeyError 或默认值，随播放开始又出现——行为随时间漂移的隐性 bug。
  - 建议: 统一键名（建议 `total_ms_mean`），空分支补齐 `decode_ms_*`/`composite_ms_*` 等缺失键，保证两个分支键集合一致。
  - 置信度: 高

- [AUD-17418] [Suggestion] summary() 在释放锁后读取 `_first_record_ts`/`_last_record_ts`（与 reset()/record() 竞态）；`field` 导入未使用
  - 位置: src/features/edit/core/realtime_playback/telemetry.py:200-235（233-234 行读 ts）、20 行（import field）
  - 证据:
    ```python
        with self._lock:
            records = list(self._records)
            totals = dict(self._session_totals)
            source_counts = dict(self._source_kind_counter)

        if not records:
            ...
        first_ts = self._first_record_ts or 0.0
        last_ts = self._last_record_ts or 0.0
    ```
    20 行：`from dataclasses import dataclass, field`
  - 影响: ts 读取在锁外，与并发 reset()（置 None）或 record()（更新）交错时 `wall_elapsed_s` 可能用混合时戳计算（`or 0.0` 兜底避免崩溃，仅统计失真）；`field` 导入死代码。
  - 建议: 把两个 ts 一并拷进锁内快照；删除未用导入。
  - 置信度: 高（代码事实）；影响低

### 通过项
- record()/enrich_last()/reset() 均在 `self._lock` 内操作共享状态，deque(maxlen) 滑动窗口边界正确
- enrich_last：trace_id 精确匹配优先、帧号回退、`timeline_frame < target-64` 提前 break 的方向假设与注释一致（倒放时退化为全窗扫描，窗口 ≤ 上限，性能可接受）
- `_percentile` 线性插值实现正确，单元素/空输入边界处理完备
- gpu_composite_frames 用 composite_ms>0 的近似已注释声明，且不参与 §25 断言指标
- assert_realtime_invariants 检查的三个断言指标（realtime_master_requests/wrong_source_frame/stale_generation）与 §25 声明对应

---

## src/features/edit/core/realtime_playback/timeline_decode_scheduler.py (行数: 696)

### 发现

- [AUD-17419] [Major] configure() 持有 `self._lock` 期间调用用户回调 `media_source_factory()` 与 `stream.close()`，可长时间阻塞所有调度热路径
  - 位置: src/features/edit/core/realtime_playback/timeline_decode_scheduler.py:168-207（工厂调用在 196-201 行、close 在 182-184/193 行，均在 `with self._lock:` 块内）
  - 证据:
    ```python
        with self._lock:
            ...
            for cid in list(existing_keys - new_clip_ids):
                stream = self._streams.pop(cid, None)
                if stream is not None:
                    stream.close()
            ...
                if layer.clip_id not in self._streams:
                    media_source = None
                    if media_source_factory is not None:
                        try:
                            media_source = media_source_factory(desired_key)
                        except Exception as e:
                            logger.debug(f"media_source_factory failed: {e}")
    ```
  - 影响: `self._lock` 同时保护 request_bundle/prepare_window/generation/stats 等播放 tick 路径。工厂回调（生产环境为 `_build_realtime_scheduler_config` 提供的 `source_factory`，内部构造 DecodeManagerMediaSource）与 stream.close()（关闭解码器，可能含 I/O）都在锁内执行：多 layer 逐个同步开源/关源期间，渲染线程的 request_bundle 会被卡住，表现为播放卡顿/丢帧——正是该模块要解决的"多轨 CPU 雪崩卡 UI"问题的反面。
  - 建议: 锁内只做计划（收集要建/要关的 key），把工厂调用与 close 移到锁外执行后再持锁提交结果；或对 stream 表采用更细粒度的分段锁。
  - 置信度: 中（模式缺陷确定；实际阻塞时长取决于工厂/close 实现）

- [AUD-17420] [Suggestion] `request_bundle` 的 `_bundle_hits/_bundle_misses` 无锁 `+=`；stats()/reset_stats() 与 prefetch 计数器的锁不对称
  - 位置: src/features/edit/core/realtime_playback/timeline_decode_scheduler.py:510-511、560-564（无锁 +=）；666-688（stats 在 _lock 下读 _prefetch_*）；690-696（reset_stats 不清 prefetch 计数）
  - 证据:
    ```python
        if not layers:
            self._bundle_misses += 1
        ...
        complete = len(ready_handles) == len(layers)
        if complete:
            self._bundle_hits += 1
        else:
            self._bundle_misses += 1
    ```
    ```python
    def reset_stats(self) -> None:
        with self._lock:
            self._bundle_hits = 0
            self._bundle_misses = 0
            self._seek_flushes = 0
    ```
  - 影响: request_bundle 释放 `self._lock` 后才累加计数（504-509 行锁块只做快照），`+=` 是读-改-写，多线程并发（engine tick 与测试/其他调用方）会丢更新，hit_rate 统计失真；`_prefetch_submitted/completed/dropped` 由 `_prefetch_lock` 保护却在 `self._lock` 下读取（锁序不一致，CPython 下仅瞬时不一致）；reset_stats 不重置 prefetch 三项，stats 输出与重置语义不对称。
  - 建议: 计数器统一收敛到一把锁（或改用 itertools/atomics 风格单锁封装）；reset_stats 补齐 prefetch 计数清零。
  - 置信度: 高（代码事实）；影响为统计正确性

- [AUD-17421] [Suggestion] configure 用 `is` 身份比较 callable（bound method/闭包），同配置重复 configure 可能误判 changed 而取消活跃预取
  - 位置: src/features/edit/core/realtime_playback/timeline_decode_scheduler.py:170-176、209-214
  - 证据:
    ```python
            config_changed = (
                previous_layers != tuple(layer.stream_key() for layer in self._layers)
                or previous_provider is not source_frame_provider
            )
        ...
        if config_changed:
            self._cancel_prefetch_requests(include_active=True)
    ```
  - 影响: `source_frame_provider` 生产来源是 `self._realtime_scheduler_source_frames`（bound method）或每次新建的闭包：每次访问 bound method 都生成新对象，`is` 比较恒为"变了"→ 即使 layers 完全相同也会触发 `_cancel_prefetch_requests(include_active=True)`，丢掉正在执行的预取窗口。当前 engine 侧有 signature 去重（engine.py:565-567），configure 实际只在配置变化时被调用，掩盖了该问题；直接调用 configure 的其他使用方会踩中。
  - 建议: 改为相等性比较（bound method 的 `==` 比较 __self__/__func__，语义正确），或对 provider 传入稳定标识符参与比较。
  - 置信度: 中（bound method 每次 `is` 不同是 Python 确定行为；实际触发依赖调用方模式）

- [AUD-17422] [Suggestion] prepare_window 中 source_frame_provider 异常时静默按 1:1 回退，变速/倒放 clip 会预取错误的源帧
  - 位置: src/features/edit/core/realtime_playback/timeline_decode_scheduler.py:276-294
  - 证据:
    ```python
                try:
                    source_frames = dict(
                        source_frame_provider(int(timeline_frame)) or {}
                    )
                except Exception:
                    logger.debug(
                        "TimelineDecodeScheduler source frame mapping failed",
                        exc_info=True,
                    )
                    source_frames = {}
                ...
                    source_frame = int(
                        source_frames.get(layer.clip_id, timeline_frame)
                    )
    ```
  - 影响: provider 失败时回退 `source_frame = timeline_frame`，变速/倒放/冻结 clip 的预取会按时间线帧号当源帧号解码，浪费解码预算并填错 ring 内容（呈现路径用权威映射不受影响，但 ring 内是错帧，`ring.contains` 命中后 request_bundle 若与权威帧号不一致则 miss，窗口命中率下降）；仅 debug 级日志，问题难以察觉。
  - 建议: provider 异常时跳过该帧的预取（宁可不预取也不预取错帧），并在 stats 中计数 provider 失败次数。
  - 置信度: 中

- [AUD-17423] [Suggestion] shutdown() 同步等待 prefetch executor，活跃预取内的同步解码无 deadline，可能长时间阻塞关闭流程
  - 位置: src/features/edit/core/realtime_playback/timeline_decode_scheduler.py:652-657
  - 证据:
    ```python
    def shutdown(self) -> None:
        """关闭所有 stream。"""
        with self._prefetch_lock:
            self._cancel_prefetch_requests_locked(include_active=True)
            self._prefetch_active = False
        self._prefetch_executor.shutdown(wait=True, cancel_futures=True)
    ```
  - 影响: cancel_event 只在帧间检查（prepare_window 循环与 request_frame 内部配合），若当前正卡在一次慢解码/慢 I/O 上，`shutdown(wait=True)` 会一直等它返回；关闭应用/重建调度器时表现为卡死数秒。
  - 建议: 给 shutdown 增加 join 超时 + 强制丢弃，或让底层解码支持 cancel_event 中断。
  - 置信度: 低（取决于 clip_decode_stream 解码是否可被及时取消，未深入验证）

### 通过项
- latest-only 异步预取背压实现正确：同参数活跃请求去重返回同一 future（369-374 行）、被取代的 pending future 必定 set_result(False)（379-385、443-444、629-633 行），无悬空 future；submit 失败的 RuntimeError 路径正确复位状态（397-408 行）
- 锁序安全：`_lock` 与 `_prefetch_lock` 无嵌套持有（configure/reset_generation/flush_seek 均先释放 `_lock` 再取 `_prefetch_lock`；_run_prefetch 中两把锁顺序获取不重叠），无死锁环
- request_bundle 的 all-or-nothing consume 契约正确：`can_consume` 先整体验证所有 layer 的 stream/ring 就绪才 take_exact（532-537 行），避免部分帧所有权转移
- stale generation 防护：request_bundle 对 `generation_id != self._generation` 返回 None（505-506 行）；prepare_window/_run_prefetch 帧间检查 generation 与 cancel_event（321-332、421-440 行）；reset_generation 变更时 flush 全部 ring（602-613 行）
- stream 生命周期：configure 对 key 变化（代理/缓存路径切换）重建 stream 并关闭旧的（189-194 行），移除的 layer stream 正确 close（181-184 行）
- 调用的 stream/ring API（request_frame/prefetch_range/get_ready_frame/take_exact/contains/flush/evict_outside_window/close）签名已与 clip_decode_stream.py、frame_ring_buffer.py 核实一致
- 无 Qt 信号、无 GL 调用（解码在 worker/stream 层），线程铁律未被本模块违反

---

## 总结

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 5 | AUD-17400, AUD-17401, AUD-17409, AUD-17410, AUD-17419 |
| Suggestion | 18 | AUD-17402–17408, AUD-17411–17418, AUD-17420–17423 |

无编号越界（全部位于 AUD-17400–17499 区间），编号按报告文件顺序连续分配。
