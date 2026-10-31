# batch-016 — src/shared/gpu/present_frame.py 静态审查

审查日期：2026-09-28
文件：`src/shared/gpu/present_frame.py`（464 行）
范围：GPU 合成帧上屏呈现协议（渲染线程 → UI Viewer 交接）：`SlotState` / `TextureLease` / `GpuPresentFrame` / `OutputSlotPool`。模块自身不依赖 Qt/GL（fence 以 opaque int 表示）。

---

## src/shared/gpu/present_frame.py (行数: 464)

### 发现

- [AUD-1601] [Major] TextureLease 状态转移未统一纳入 pool 锁，渲染线程锁外 `mark_ready` 与锁内 `release_all`/`acquire_for_writing` 存在 check-then-act 竞态
  - 位置: src/shared/gpu/present_frame.py:162-170（及 29-44 协议图、424-433、439-441）
  - 证据:
    ```python
    # 渲染线程按模块协议直接在锁外调用（docstring 第 30-36 行）：
    #   slot.mark_ready()                        # WRITING -> READY
    def mark_ready(self, fence_ns: int, fence_cleanup: Optional[Callable[[int], None]] = None) -> None:
        """WRITING -> READY。renderer 写完，附带 fence。"""
        if self.state is not SlotState.WRITING:
            raise RuntimeError(
                f"TextureLease {self.slot_id}: cannot mark_ready in state {self.state.name}"
            )
        self.state = SlotState.READY
        self.fence_ns = fence_ns
        self.fence_cleanup = fence_cleanup

    # 而 shutdown 线程在 pool 锁内读写同一批字段：
    def release_all(self) -> None:
        """强制释放所有 slot（shutdown 用）。"""
        with self._lock:
            for slot in self._slots.values():
                if slot.state is not SlotState.FREE:
    ```
  - 影响: `OutputSlotPool._lock` 只保护 pool 方法，但 `mark_ready`/`make_present_frame` 由渲染线程持 lease 直接调用（协议图 29-44 行明确如此），`lease.state/fence_ns/fence_cleanup` 的读-改-写在锁外进行。具体竞态窗口：`release_all()`（shutdown）读到 WRITING → 强制置 FREE 并清 fence；随后渲染线程 `mark_ready()` 把状态"复活"为 READY 并写入新 fence——池认为已全部释放，纹理可能被上层销毁，而 slot 仍标 READY，Viewer 若再消费即访问已删除纹理；新写入的 fence 也不会再有人清理（GLsync 泄漏）。另外 `release_all` 对 WRITING 强制 FREE 仅告警（200-206 行），随后另一路 `acquire_for_writing` 可把同一 slot 交给新写入者，出现双写者。`get_slot()`（439-441 行）把可变 lease 直接交给锁外调用方，扩大同一问题。
  - 建议: 把所有状态转移收进 pool（提供 `pool.mark_ready(slot_id, ...)` 等锁内 API），或让 `TextureLease` 持有对 pool 锁的引用在转移方法内自加锁；`release_all` 前置要求渲染线程已 join/停摆，并对 WRITING 强制释放后拒绝后续 `mark_ready`（如引入 CLOSED 终态）。
  - 置信度: 高（协议图与实现共同证明转移发生在锁外；实际触发依赖 shutdown 与渲染并发，属边界路径）

- [AUD-1602] [Major] `GpuPresentFrame.extra` 为可变 dict，随 frozen descriptor 跨线程按引用共享
  - 位置: src/shared/gpu/present_frame.py:256、296
  - 证据:
    ```python
            source_kinds=source_kinds,
            extra=extra or {},          # 第 256 行：直接引用调用方传入的 dict
        )

        # 第 296 行：
        extra: dict = field(default_factory=dict)
    ```
  - 影响: `make_present_frame(extra=d)` 不做拷贝，渲染线程 emit 后仍可继续修改 `d`；PyQt 队列连接跨线程传对象引用而非副本，UI 线程读取 `present.extra` 时可能看到半更新状态（正是本协议要消除的"共享可变状态跨线程"问题在元数据维度的复现）。`frozen=True` 只阻止属性重绑定，给人不可变假象。
  - 建议: 构造时 `extra=dict(extra) if extra else {}` 浅拷贝；或改 `MappingProxyType`/`types.MappingProxyType` 包装；文档标注 extra 一经 emit 不得再写。
  - 置信度: 中（共享机制确定；是否实际触发取决于调用方是否复用/改写 dict，本文件内不可见）

- [AUD-1603] [Major] 消费端消失/迟到帧被丢弃时无 slot 回收路径：READY 态无超时、无 discard API，仅全局 `release_all` 兜底
  - 位置: src/shared/gpu/present_frame.py:377-401、424-433
  - 证据:
    ```python
        未 ack 的 READY/IN_VIEWER slot **永不复用**（缺陷计划核心不变量）。
        所有 slot 都被占用时返回 None（调用方应 hold/drop）。
        """
        with self._lock:
            # 优先 RELEASABLE（刚被 Viewer ack 的，热路径友好）
    ```
    全文检索仅有 `release_all()`（"shutdown 用"）能从 READY/IN_VIEWER 强制回收，无针对单个 slot 的 cancel/discard。
  - 影响: 核心不变量"未 ack 永不复用"的另一面是：帧 emit 后若 Viewer 队列溢出丢帧、或 Viewer 窗口在 take 之前关闭而未补 ack，该 slot 永久滞留 READY。池容量典型为 3，三次此类事件后 `acquire_for_writing` 永远返回 None，渲染线程持续 hold/drop，画面静默冻结且无告警（仅 debug 日志）。虽然 `ack()` 容忍从 READY 直接转移（182-183 行）提供了补救手段，但模块未提供显式 `discard(slot_id)` API，也未在协议文档中强制"丢弃帧必须补 ack"。
  - 建议: 增加 `pool.discard(slot_id)`（READY/IN_VIEWER → RELEASABLE，含 fence 清理）供丢帧路径调用；考虑对 READY 滞留时间做 watchdog 统计并在 stats 中暴露；协议文档明确"任何被丢弃的 present 都必须 ack 或 discard"。
  - 置信度: 中（模块内自洽，泄漏由调用方缺纪律触发；本文件无法验证 Viewer 侧是否总有补 ack）

- [AUD-1604] [Major] `register_slot` 重复 slot_id 静默覆盖旧 lease，可致 fence（GLsync）泄漏；且无尺寸/句柄校验
  - 位置: src/shared/gpu/present_frame.py:346-356
  - 证据:
    ```python
    def register_slot(self, slot_id: int, *, texture_glo: int, width: int, height: int) -> TextureLease:
        """注册一个 output slot（纹理对象由调用方创建/管理）。"""
        with self._lock:
            lease = TextureLease(
                slot_id=slot_id,
                texture_glo=texture_glo,
                width=width,
                height=height,
            )
            self._slots[slot_id] = lease
            return lease
    ```
  - 影响: 若旧 slot 处于 READY/IN_VIEWER 且持有未清理的 fence（`fence_ns != 0`），被覆盖后其 `fence_cleanup` 永不可达，GLsync 对象泄漏；池内状态统计也与真实纹理脱节。同时 width/height ≤ 0、texture_glo ≤ 0 未校验，坏参数会沿协议传到 Viewer 端才暴露。
  - 建议: 覆盖前检查旧 lease：非 FREE 则先走 fence 清理（或抛错拒绝重复注册）；对 width/height/texture_glo 做 `> 0` 校验。`from_resource` 路径同理校验 `resource.native_handle`。
  - 置信度: 中（覆盖行为由代码确定；是否实际发生重复注册取决于调用方，本批次未见）

- [AUD-1605] [Suggestion] `mark_in_viewer` 静默 no-op，Viewer 无法区分"成功进入 IN_VIEWER"与"slot 已过期/不存在"
  - 位置: src/shared/gpu/present_frame.py:403-410
  - 证据:
    ```python
    def mark_in_viewer(self, slot_id: int) -> None:
        """Viewer 取走 slot（READY -> IN_VIEWER）。"""
        with self._lock:
            slot = self._slots.get(slot_id)
            if slot is None:
                return
            if slot.state is SlotState.READY:
                slot.begin_viewer_use()
    ```
  - 影响: 状态非 READY 或 slot 缺失时不抛错、不返回布尔，调用方默认成功并继续 blit；若 slot 已被回收/重写，存在读到半写入纹理的隐患（该协议要防的场景）。正常单消费流不会触发，属协议违例时的静默降级。
  - 建议: 返回 `bool`（或过期时抛异常/日志 warning），Viewer 在 False 时放弃本帧。
  - 置信度: 中（代码行为确定；触发需协议违例路径）

- [AUD-1606] [Suggestion] `_reuse_violations` 计数器无任何递增路径，`assert_no_reuse_violations` 恒通过，验收机制空转
  - 位置: src/shared/gpu/present_frame.py:340、456、459-464
  - 证据:
    ```python
        self._reuse_violations = 0  # 试图复用未 ack 的 slot（必须为 0）
    ...
        def assert_no_reuse_violations(self) -> None:
            """断言没有复用未 ack 的 slot（缺陷计划 §7 验收）。"""
            if self._reuse_violations != 0:
                raise AssertionError(
    ```
    全文除初始化（340 行）与读取（456、461 行）外无写入点。
  - 影响: "缺陷计划 §7 验收"形同虚设——断言不可能失败，给人不变量已被监控的假信心；若未来 acquire 逻辑退化，也无法被该指标捕获。
  - 建议: 在 `begin_writing` 允许态检查或 acquire 选择逻辑中，对"试图获取非可写 slot"的分支递增该计数（需经 pool 回调/包装实现），或删除该指标避免误导。
  - 置信度: 高

- [AUD-1607] [Suggestion] 宽泛 `except Exception` 静默吞异常且无日志（`release_all` 与 `TextureLease.release` 的 fence 清理路径）
  - 位置: src/shared/gpu/present_frame.py:429-432、209-212
  - 证据:
    ```python
                    try:
                        slot.ack()
                    except Exception:
                        pass
                    slot.release()
    ...
                try:
                    self.fence_cleanup(self.fence_ns)
                except Exception:
                    pass
    ```
  - 影响: shutdown/回收阶段 GL 资源清理失败完全不可见（对比 `TextureLease.ack` 中同类路径至少有 `logger.debug`），GLsync/驱动错误被吞，泄漏无从排查。
  - 建议: 与 193-194 行保持一致，至少 `logger.debug/warning` 记录异常再继续。
  - 置信度: 高

- [AUD-1608] [Suggestion] frozen dataclass 含 dict 字段导致 `hash()` 必然抛 TypeError
  - 位置: src/shared/gpu/present_frame.py:265、296
  - 证据:
    ```python
    @dataclass(frozen=True)
    class GpuPresentFrame:
        ...
        extra: dict = field(default_factory=dict)
    ```
  - 影响: `frozen=True`（且默认 eq=True）会生成基于全部字段的 `__hash__`，`extra` 为 dict 不可哈希，任何把 present 帧放入 set / 作 dict key / 去重的用法都会在运行时 TypeError。机制确定，是否被如此使用本文件不可见。
  - 建议: 设 `eq=True` 同时 `field(compare=False, hash=False)` 不够（hash 仍含字段时可 `__hash__ = None` 语义化），最简单是文档注明不可哈希，或以 `(slot_id, generation_id, frame)` 提供显式 key 方法。
  - 置信度: 中（hash 抛错确定；是否存在哈希用法未知）

- [AUD-1609] [Suggestion] 模块头部协议图与实现不符：`make_present_frame(fence=...)` 参数不存在，且图中调用顺序会触发状态检查异常
  - 位置: src/shared/gpu/present_frame.py:29-44（对 230-244）
  - 证据:
    ```python
      present = slot.make_present_frame(fence=fence, ...)
      slot.mark_ready()                        # WRITING -> READY
    ...
      glWaitSync(present.fence)  # 等 renderer 写完
    ```
    而实现（241-244 行）要求 `make_present_frame` 在 READY 态调用且签名无 `fence` 形参；属性名为 `fence_ns` 非 `fence`；正确顺序见 327-329 行用法示例（先 `mark_ready` 再 `make_present_frame`）。
  - 影响: 按模块 docstring 实现的新调用方会先撞 `TypeError`（意外的 fence 关键字），改正后按图中顺序又撞 `RuntimeError`（WRITING 态禁止 make_present_frame）。
  - 建议: 更新 29-44 行协议图为与 327-329 行一致的真实顺序与签名。
  - 置信度: 高

- [AUD-1610] [Suggestion] acquire 失败热路径日志 f-string 与 `_state_summary()` 急切求值，debug 未启用也每帧分配
  - 位置: src/shared/gpu/present_frame.py:397-400
  - 证据:
    ```python
            self._acquire_failures += 1
            logger.debug(
                f"OutputSlotPool: no writable slot (gen={generation_id}, "
                f"frame={frame_index}); states={self._state_summary()}"
            )
    ```
  - 影响: 槽位耗尽通常发生在播放反压持续期间（正是每帧都失败的时段），此时每次失败都构造字符串并遍历全部 slot 生成 dict，即便日志级别过滤掉输出；4K 实时下属可避免的每帧分配。量级小，仅记录。
  - 建议: `if logger.isEnabledFor(logging.DEBUG):` 守卫，或 `%` 惰性格式化。
  - 置信度: 高（行为确定，量级小）

- [AUD-1611] [Suggestion] 类型注解使用裸 `tuple`/`dict` 泛型；`_size` 从不参与注册数量约束
  - 位置: src/shared/gpu/present_frame.py:295、302、334-335、443、449
  - 证据:
    ```python
        source_kinds: tuple = ()
        ...
        def as_legacy_tuple(self) -> tuple:
    ...
        def __init__(self, size: int = 3) -> None:
            self._size = max(1, int(size))
    ...
        def _state_summary(self) -> dict:
        def stats(self) -> dict:
    ```
  - 影响: `tuple`/`dict` 裸注解丢失元素类型信息（应为 `tuple[str, ...]`、`dict[str, int]`）；`_size` 仅在 `size` 属性与 stats 中出现，`register_slot` 可注册任意多于/少于 `size` 的 slot，池容量约束实为文档性而非强制的。
  - 建议: 补全泛型参数；要么在 `register_slot` 达到 `_size` 时告警/抛错，要么注释明确 `_size` 仅为标称值。
  - 置信度: 高

### 通过项

- GLSL/HDR：本文件无内嵌 GLSL、无色彩转换/量化代码，`color_space` 仅作元数据透传，不存在 clamp(0.0,1.0) 或把 HDR float16/32 当 8bit 处理的风险（该职责在下游 blit/viewer）。
- 信号防递归：模块不含 Qt 信号/回调重入路径，`ack`/`mark_in_viewer` 无发射动作，无成环风险。
- 丢帧基础语义：`acquire_for_writing` 池满返回 None（不阻塞渲染线程）符合实时设计；`ack` 容忍 READY 直接转移，同步 blit 的 Viewer 可事后补 ack。
- 非法状态转移 fail-fast：`begin_writing`/`mark_ready`/`begin_viewer_use`/`ack` 均校验前置状态并抛带明细的 RuntimeError；`pool.ack` 对双重 ack 降级为 debug 日志不崩溃。
- fence 生命周期双兜底：`ack()`（190-196 行）与 `release()`（207-214 行）都会清理未消费的 fence，正常周期内无 GLsync 泄漏。
- 热路径无显著每帧分配：present descriptor 为轻量 dataclass，`extra or {}` 仅按需创建；状态摘要仅在失败路径执行（见 AUD-1610 的小额外开销）。
- 池操作（acquire/mark_in_viewer/ack/release_all/stats）均在 `self._lock`（RLock）内执行，pool 方法彼此之间互斥正确。
