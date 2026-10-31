# Batch 015 — shared/gpu/metal 第二批（IDCT 内核 / 视频内核 / 提交调度器）

- 日期: 2026-09-28
- 范围: src/shared/gpu/metal/ 三个文件（IDCT 大内核、视频 YUV→RGBA 内核、提交调度器）
- 问题编号区间: [AUD-1500–AUD-1599]
- 计数: Critical 0 / Major 3 / Suggestion 8

---

## src/shared/gpu/metal/kernels_idct.py (行数: 1,195)

### 发现

- [AUD-1500] [Suggestion] 两个 MSL 源常量约 570 行逐字重复，存在版本漂移风险
  - 位置: src/shared/gpu/metal/kernels_idct.py:54-623 与 625-1195
  - 证据:
    ```python
    METAL_INVERSE_DCT_MSL = """
    ...
    kernel void inverse_dct_px(...)
    ...
    kernel void inverse_dct_ws(...)
    ...

    METAL_INVERSE_DCT_WSD_V8_MSL = """
    ...
    kernel void inverse_dct_px(...)      # 与上一常量逐字相同
    ```
    两常量中的 `inverse_dct_px`/`inverse_dct_m8`/`inverse_dct_sp`/`inverse_dct_w1`/`inverse_dct_ws`（含 `idct_round_shift32`/`idct_dequant` 辅助函数）逐字相同，仅最后一个 kernel 不同（`inverse_dct_wsd` vs `inverse_dct_wsd_v8`）。
  - 影响: 本模块的核心契约是"所有变体与 CPU 参考逐位一致"。今后若修复任一公共 kernel（如 px 基准版）而漏改另一常量，非 V8 与 V8 管线将静默产生数值分歧，差分测试若只覆盖一条路径即漏检。
  - 建议: 将公共 kernel 源抽为 Python 侧共享片段（f-string / 列表 join 拼装两个常量），或至少在两个常量头部互相注释"修改须同步"锚点。
  - 置信度: 高

- [AUD-1501] [Suggestion] params[7] 与 buffer(8) 槽位语义在不同 kernel 间漂移，装配侧易错
  - 位置: src/shared/gpu/metal/kernels_idct.py:440-446（ws: params[7]=off_stride, buffer(8)=row_slice）、545-546（wsd: params[7]=dc0）、1103-1104（wsd_v8: buffer(8)=blkseg）
  - 证据:
    ```c
    // inverse_dct_ws
    const uint off_stride = (uint)params[7];
    ...
    device const uint* row_slice [[buffer(8)]],
    // inverse_dct_wsd
    const uint dc0 = (uint)params[7];
    // inverse_dct_wsd_v8
    device const uint* blkseg   [[buffer(8)]],   /* V8：per-block 段索引 */
    ```
  - 影响: 同一槽位在 ws/wsd 两代 kernel 中语义不同（off_stride vs dc0），buffer(8) 在 V8 源中由 row_slice 变为 blkseg。装配代码若在切换 kernel 变体时漏更新任一绑定，将得到越界读或错误 Q 表——表现为静默渲染错位而非崩溃，排查成本高。
  - 建议: 在 Python 装配层（本批外的调用方）为每个 kernel 变体定义显式的 buffer/params 绑定表，杜绝按位置手工传参；或将 ws 的 off_stride 移到 params[8] 使 params[7] 恒为 dc0。
  - 置信度: 高（槽位漂移是代码事实；实际装配错误是否发生取决于本批外代码）

- [AUD-1502] [Suggestion] CSR 偏移与 sblk0 无下溢/越界守卫，完全依赖上游装配契约
  - 位置: src/shared/gpu/metal/kernels_idct.py:1035-1038、1043（ws）；569-572（wsd）；249-250、260（sp）
  - 证据:
    ```c
    const uint bi = (uint)gidx - sblk0[s];
    const uint ob = s * off_stride;
    const uint p0 = off[ob + bi];
    const uint p1 = off[ob + bi + 1u];
    ...
    for (uint k = tid; k < p1 - p0; k += 64u) {
    ```
  - 影响: 若 C 侧/Python 装配发出非单调 CSR（p1 < p0）或 sblk0[s] > gidx，uint 下溢产生巨大索引/循环次数，GPU 侧越界读（Metal 不保证 device 越界读的安全行为），错误以画面花屏或设备断言形式出现且无诊断信息。GPU 侧难做廉价校验，故为契约问题而非实现 bug。
  - 建议: 在装配侧（发射前 CPU 端）加一次低成本断言（每平面一次的 offset 单调性检查 + sblk0[s] <= gidx 抽查）， kernel 侧可不动。
  - 置信度: 低（契约在文件头 docstring 中有明确定义；无法在本批文件中确认装配侧是否已有校验）

### 通过项
- 浮点字面量规范: 全部 kernel 为整数域运算（int64），无浮点路径，不存在缺 `.0` 问题。
- IDCT 舍入一致性: `idct_round_shift32`（符号拆分四舍五入）与 `idct_dequant`（先钳 ±2^25 再乘 W）在两个常量的全部 12 个 kernel 变体间逐字一致，未见 10bit/12bit 路径舍入分歧。
- 整数域位等价: px 直和与 m8/sp/w1/ws/wsd 两遍结构在 int64 精确算术下可任意重排；行掩码跳过仅省略"加 0 项"，位不变性论证成立。
- barrier 竞态: rmu 掩码清零（第一 barrier 前）、置位（B 相）、读取（第二 barrier 后）分居 `mem_threadgroup` barrier 两侧，relaxed atomic + barrier 模式无数据竞态；`rmu`/`loff`/`G` 的写读均被 barrier 分隔。
- barrier 分歧: `bx0 >= cols || by >= rows` 早退条件为组级常量（bx0、by 由 grp 决定），整组一致返回，无 barrier 分歧 UB。
- 边缘像素处理: 像素写守卫 `bx < cols && px < vis_w && py < vis_h`（m8/sp）/ `px < vis_w && py < vis_h`（w1/ws/wsd）正确处理非 8 整除的可见区边缘；G 暂存读以 `bx < cols` 守卫。
- sp 组横跨行尾: `gidx = by * cols + (bj < cols ? bj : cols)` 将越列块钳到行尾偏移（空区间），配合 `k >= loff[j] - p0` 上界搜索正确定位组内块号，未见越列散射。
- threadgroup 内存: G[512]+B[512] (long) = 16KB，处于常见 Apple GPU 32KB 上限内。
- 输出 clamp 语义: `clamp(x̂+mid, 0, max)` 属码流重建域规范（docstring 声明的 CPU 契约同款），非 HDR 无理由 clamp。
- 信号防递归: 文件无 Qt 信号/UI 逻辑，不适用。
- 异常路径: 文件仅含字符串常量与 import，无 Python 运行时逻辑，无异常处理需求。

---

## src/shared/gpu/metal/kernels_video.py (行数: 174)

### 发现

- [AUD-1510] [Major] 位深仅校验下限（bd < 8），11–16 位深静默通过并生成错误码值域参数
  - 位置: src/shared/gpu/metal/kernels_video.py:109-111
  - 证据:
    ```python
    bd = int(bit_depth)
    if bd < 8:
        raise ValueError(f"yuv convert: 不支持的位深 {bit_depth}（Phase 5 覆盖 8/10-bit）")
    ```
  - 影响: 错误消息与 docstring 均声称"覆盖 8/10-bit"，但代码只拦截下限。`bd=12`（HEVC 4:2:0 12-bit 源）会静默通过：max_code=4095、c_neutral=2048 配 R16Unorm 纹理时，12-bit 左对齐码值的解释公式整体错位，画面色彩错误但看起来"正常"——正是该函数 docstring 自己声明要防止的故障模式（违反 .clinerules:11 的"不得静默取默认"精神）。
  - 建议: 增加 `if bd not in (8, 10): raise ValueError(...)`（或显式支持集 `{8, 10}`），与错误消息保持一致。
  - 置信度: 高（代码与自身错误消息/文档的不一致是事实；是否有 >10bit 源会流经此处取决于上游）

- [AUD-1511] [Suggestion] 非整数位深被 `int()` 静默截断
  - 位置: src/shared/gpu/metal/kernels_video.py:109
  - 证据:
    ```python
    bd = int(bit_depth)
    if bd < 8:
    ```
  - 影响: PyObjC/CVPixelBuffer 元数据传来的位深若为 float（如 10.0 无害，但 8.5→8、10.9→10），`int()` 向零截断后静默通过，掩盖上游数据异常。
  - 建议: `if bd != int(bd) 或 not isinstance(bit_depth, int)` 时报错，或直接要求 int 类型并用精确匹配集校验（可与 AUD-1510 合并修复）。
  - 置信度: 中（类型注解已声明 `bit_depth: int`，运行时不强制）

### 通过项
- HDR 正确性: kernel 输出 `rgba.write(float4(r, g, b, 1.0), gid)` 无任何 clamp，符合"HDR 超界值留给上游调色链路"约定。
- 浮点字面量: kernel 与 Python 侧 `16.0`/`1.0/(219.0*unit)`/`2.0`/`1.0` 均带 `.0`，无缺 `.0` 项。
- 线程组/纹理边界: `gid` 守卫以输出纹理宽高为准；`uv_gid = uint2(gid.x >> 1, gid.y >> 1)` 对奇数宽高也落在 ceil 半幅 UV 平面界内（(W-1)>>1 <= ceil(W/2)-1）。
- 参考实现一致性: `yuv_to_rgba_reference` 的最近邻上采样（repeat×2 后 `[:h, :w]` 裁剪）与 kernel `gid>>1` 寻址逐点对应；两侧由同一 `build_yuv_convert_params` 驱动。
- 参数布局: `YUV_CONVERT_PARAMS_DTYPE` 9×float32 与 MSL `YuvConvertParams` 逐字段对齐，全 float 无 padding 分歧。
- 矩阵输入校验: 未知色彩矩阵显式 `ValueError` 并列出支持集，未静默取默认。
- 信号防递归: 纯计算模块无 Qt 信号，不适用。
- 类型注解: 函数签名注解完整。
- 异常路径: 仅显式 ValueError，无宽泛 except。

---

## src/shared/gpu/metal/submission_scheduler.py (行数: 139)

### 发现

- [AUD-1520] [Major] drain() 可快照到 submit() 预留的 None 槽并解引用 `.command_buffer`，关机路径崩溃
  - 位置: src/shared/gpu/metal/submission_scheduler.py:68、96-97、133-139
  - 证据:
    ```python
    self._pending[sequence] = None  # type: ignore[assignment]
    ...
    command_buffer = self._context.submit(completion=completed, wait=False)   # 锁外
    ...
    with self._lock:
        pending = tuple(self._pending.values())
    for submission in pending:
        if wait_fn is not None:
            wait_fn(submission.command_buffer)
        else:
            submission.command_buffer.waitUntilCompleted()
    ```
  - 影响: submit 在锁内放入 `None` 预留槽后、在锁外调用 `self._context.submit(...)`；此窗口内另一线程（exporter 关机路径）调用 `drain()` 会对 `None` 取 `.command_buffer`，抛 `AttributeError`。该异常发生在关闭/导出生命周期边界，可能中断导出收尾或使异常逃逸到不可恢复的关闭序列。
  - 建议: drain 循环内跳过 None 条目（`if submission is None: continue`，等待其完成由已注册的 completion handler 收尾），或 drain 先对 None 槽执行一次有限等待/提交探测。
  - 置信度: 高（竞态窗口是代码结构事实；触发需 drain 与 submit 并发，属文档声明的 shutdown 场景）

- [AUD-1521] [Major] on_complete 回调异常未捕获，在 Metal completion 线程上逃逸
  - 位置: src/shared/gpu/metal/submission_scheduler.py:86-94
  - 证据:
    ```python
    for callback in callbacks:
        try:
            callback()
        except Exception:
            pass
    if on_complete is not None:
        on_complete(submission, error)
    ```
  - 影响: release 回调被明确防护（注释说明"broken callback must not prevent the ring from draining"），但紧随其后的 `on_complete` 无同等防护。若 on_complete（调用方传入的帧推进/信号逻辑）抛异常，异常在 PyObjC block 分发的驱动回调线程上逃逸——轻则 traceback 打印后被吞、错误状态丢失且无人知晓，重则触发进程级异常处理终止。
  - 建议: 对 `on_complete` 同样 try/except，将异常记录到 `self._errors[sequence]`（连同已有 error）供 poll 侧发现，保持语义一致。
  - 置信度: 中（未捕获是代码事实；实际后果取决于 PyObjC 异常配置与 on_complete 实现，未在本批文件中）

- [AUD-1522] [Suggestion] submit() 返回 None 的语义二义：容量满 vs context 提交失败
  - 位置: src/shared/gpu/metal/submission_scheduler.py:61-62、103-107
  - 证据:
    ```python
    if len(self._pending) >= self._max:
        return None
    ...
    if command_buffer is None:
        with self._lock:
            self._pending.pop(sequence, None)
            self._release_callbacks.pop(sequence, None)
        return None
    ```
  - 影响: 调用方无法区分"ring 满，应施加背压并稍后重试"与"context.submit 返回 None（异常状态）"，两者的正确响应不同——前者重试即可，后者重试可能永远失败，背压逻辑会退化为活锁。
  - 建议: 区分返回值（如容量满抛专用异常或返回哨兵枚举），或文档化调用方须在收到 None 时检查 `error()`/context 状态。
  - 置信度: 高（语义二义为代码事实；是否造成实际缺陷取决于调用方，本批外）

- [AUD-1523] [Suggestion] _completed/_errors 无界增长，依赖调用方自觉 poll
  - 位置: src/shared/gpu/metal/submission_scheduler.py:84、116-121、123-125
  - 证据:
    ```python
    self._completed.append(sequence)
    ...
    def poll(self) -> tuple[int, ...]:
        with self._lock:
            completed = tuple(self._completed)
            self._completed.clear()
            return completed
    ...
    self._errors[sequence] = str(error)
    ```
  - 影响: 若帧循环调用方忘记 poll（或在异常路径停止 poll），`_completed` 每提交一条追加一个 int，长时间 4K 播放下无界累积；`_errors` 同理且无任何淘汰机制（正常流不增长，但持续出错的流每帧积累一条字符串）。调度器声称"bounded"却对自身记账结构无界。
  - 建议: `_completed` 达阈值（如 4×max_in_flight）时记录一次警告；`_errors` 保留最近 N 条环形缓冲。
  - 置信度: 中（增长路径是代码事实；实际泄漏量级小且依赖调用方行为）

- [AUD-1524] [Suggestion] drain() 不清理完成后的记账残留，wait_fn 异常中断剩余等待
  - 位置: src/shared/gpu/metal/submission_scheduler.py:135-139
  - 证据:
    ```python
    for submission in pending:
        if wait_fn is not None:
            wait_fn(submission.command_buffer)
        else:
            submission.command_buffer.waitUntilCompleted()
    ```
  - 影响: (1) `wait_fn` 抛异常时循环中断，其后所有 in-flight buffer 不再等待即从 drain 返回，关机代码可能在 GPU 仍持资源时释放上下文；(2) drain 后 `_completed`/`_errors` 未清空，若对象被复用（而非丢弃），陈旧序列号混入下一轮 poll 结果。
  - 建议: wait_fn 逐个 try/except 后继续等待其余；drain 结束时清空 `_completed`（并可顺带归档 `_errors`）。
  - 置信度: 中

- [AUD-1525] [Suggestion] command_buffer 以裸 Any 贯穿全模块，弱化类型契约
  - 位置: src/shared/gpu/metal/submission_scheduler.py:21、32、71、97
  - 证据:
    ```python
    sequence: int
    command_buffer: Any
    ...
    def __init__(self, context: Any, max_in_flight: int = 4) -> None:
    ...
    error = command_buffer.error()
    ```
  - 影响: `error()`/`waitUntilCompleted()` 等 duck-typed 调用全靠约定，IDE/静态检查无法发现拼写或协议漂移；测试替身与真实 MTLCommandBuffer 的协议差异只能在运行时暴露。
  - 建议: 定义 `Protocol`（如 `SupportsErrorAndWait`）替换 `Any`，测试替身与 context 同受结构化约束。
  - 置信度: 高

### 通过项
- 多来源提交串行化: 容量检查、序列分配、槽预留、回调表写入全部在 RLock 临界区内，并发 submit 不会超发或序列错乱。
- 同步完成路径: "预留先于 commit"设计正确处理了驱动/test double 同步回调——completed 同步执行后 submit 侧以 `if sequence in self._pending` 防复活，回调不重复执行。
- commit 异常回滚: `except Exception` 路径在锁内 pop 预留槽与回调表后 re-raise，无槽泄漏。
- 锁外回调执行: release 回调与 on_complete 均在锁外调用，避免持锁重入死锁；RLock 允许 completed 内再取锁。
- release 回调 exactly-once: 回调表 pop 于锁内、执行于锁外，异常被吞以保护 ring 排空（注释声明设计意图），单条回调失败不影响其余。
- 无隐藏等待: `submit(wait=False)`、ring 满显式返回 None 施加背压，符合模块"不隐藏 wait、不静默增长队列"的声明。
- 输入校验: `max_in_flight < 1` 显式 ValueError。
- 取消路径: 模块未提供取消 API（取消语义归 context/调用方），不存在取消后调度器侧积压残留问题；积压由 bounded ring + 完成回调自然排空。
- 性能模式: 每次 submit 仅创建一个闭包与一个小 dataclass，无可观的每帧重建开销；poll 锁内仅 tuple 拷贝，无明显吞吐瓶颈。
- 类型注解: 公共 API 注解完整（除 Any 弱类型，已记 AUD-1525）。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 3 | AUD-1510, AUD-1520, AUD-1521 |
| Suggestion | 8 | AUD-1500, AUD-1501, AUD-1502, AUD-1511, AUD-1522, AUD-1523, AUD-1524, AUD-1525 |

Critical 为空的说明: IDCT 内核的数值契约（int64 域逐位等价、行掩码跳过加零项不变、barrier 分隔的原子操作）经逐条核对均成立；视频内核 HDR 无 clamp 符合约定；调度器核心提交/完成路径的锁协议正确。三个 Major 均为边界条件（输入校验缺口、关机竞态、回调线程异常逃逸），不构成常规播放路径的确定性错误。
