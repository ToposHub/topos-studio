# 批次报告：batch-321 — src/shared/codec/

任务区间：AUD-32100–AUD-32199。审查日期：2026-09-28。
文件：4 个，559 行。九维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解。

本批次为 ctypes 绑定之上的纯 Python 配置/策略层，不涉及 OpenGL 资源、GLSL 与 Qt 信号，相关维度标"不适用"。

---

## src/shared/codec/__init__.py (行数: 24)

### 发现

（无）

### 通过项
- 通过（无逻辑）：纯 re-export。导出 9 个符号（TOPOS_CODEC_ABI_VERSION、ToposCodec、ToposCodecCapabilities、ToposCodecError、ToposCpuFeatures、ToposDecodeRequest、ToposDecodeStageStats、ToposVersion、load_library），逐一与 `topos_binding.py` 核对均存在（第 21、2783、548、183、525、401、663、514、1253 行），无 ImportError 风险。
- GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交 / 异常取消：不适用（无逻辑）。

---

## src/shared/codec/color_metadata_config.py (行数: 151)

### 发现

- [AUD-32101] [Major] `__post_init__` 警告宣称回退到 'bt709'/'limited' 但从不实际改写字段，无效值继续下传编码器
  - 位置: src/shared/codec/color_metadata_config.py:69-76
  - 证据:
    ```python
    # 仅对真正无效的值发出警告，"unknown" 是合法的缺失标记
    if self.primaries not in valid_primaries:
        logger.warning(f"Unknown primaries: {self.primaries}, using 'bt709'")
    if self.transfer not in valid_transfer:
        logger.warning(f"Unknown transfer: {self.transfer}, using 'bt709'")
    if self.matrix not in valid_matrix:
        logger.warning(f"Unknown matrix: {self.matrix}, using 'bt709'")
    if self.range not in valid_range:
        logger.warning(f"Unknown range: {self.range}, using 'limited'")
    ```
  - 影响: 日志声称"using 'bt709'"但四个字段从未被重新赋值（dataclass 无 coerce 逻辑），无效字符串原样保留。`from_metadata` 把源素材原始字符串直接送入 `cls(...)`（第 93-105 行），违反"外部数据须验证"铁律：一旦 ffmpeg 侧出现名单外的值（拼写变体、新标准名），下游 ToposVideoEncoder/PyAV 编码器会拿到非法 primaries/transfer/matrix/range，可能写出非法 VUI/SEI 元数据或触发编码器报错，且运维被日志误导以为已回退。
  - 建议: 二选一并保持言行一致——要么真正 coerce（`self.primaries = "bt709"` 等），要么把日志改为"保留原值，输出色彩可能异常"；`from_metadata` 路径对名单外值做显式映射或拒绝。
  - 置信度: 高

- [AUD-32102] [Suggestion] HDR10 静态元数据（mastering_display/max_cll/max_fall）零验证，`Optional[tuple]` 未参数化
  - 位置: src/shared/codec/color_metadata_config.py:43-45
  - 证据:
    ```python
    # Stage-2 HDR static metadata (ST 2086 + CTA-861.3). None = omit SEI.
    mastering_display: Optional[tuple] = None  # (gx,gy,bx,by,rx,ry,wpx,wpy)
    max_cll: Optional[int] = None               # nits
    max_fall: Optional[int] = None               # nits
    ```
  - 影响: `__post_init__` 只校验四个枚举字符串；mastering_display 的长度（须为 8）、各坐标取值域、max_cll/max_fall 非负均不校验。调用方传入长度错误的元组时，ST 2086 SEI 会带错误负载写入码流（HDR 元数据错误属渲染/显示错误类风险），且 `Optional[tuple]` 缺少元素类型注解。
  - 建议: 在 `__post_init__` 增加 mastering_display 长度=8 与数值有限性、max_cll/max_fall>0 的校验（无效则置 None 并告警）；注解改为 `Optional[tuple[float, ...]]`。
  - 置信度: 高

- [AUD-32103] [Suggestion] `from_metadata` 注解引用 `"VideoMetadata"` 但该名称未导入（无 TYPE_CHECKING 兜底）
  - 位置: src/shared/codec/color_metadata_config.py:79
  - 证据:
    ```python
    @classmethod
    def from_metadata(cls, metadata: "VideoMetadata") -> "ColorMetadataConfig":
    ```
  - 影响: 模块头有 `from __future__ import annotations`，运行时不求值故不崩溃；但对 mypy/IDE 是未定义名称，`VideoMetadata` 的字段访问（color_primaries 等）失去静态检查，类型注解维度缺口。
  - 建议: 增加 `from typing import TYPE_CHECKING` 与 `if TYPE_CHECKING: from ... import VideoMetadata`（按实际来源模块），保持运行时零依赖。
  - 置信度: 高

### 通过项
- 线程安全：纯不可变配置 dataclass（构造后无共享可变状态），多线程共享安全。
- 预设正确性（GLSL·HDR 维度的元数据侧）：hdr10() 的 BT.2020/D65 mastering 坐标、PQ/smpte2084、bt2020nc+limited 组合正确；hlg()、dci_p3()（smpte431+bt709 matrix 为 P3 常见投放组合）未见错误。
- 异常与取消路径：无吞异常路径（本文件无 try/except）。
- 性能模式：仅在构造期做成员校验，成本低，无热路径问题。

---

## src/shared/codec/decode_worker_budget.py (行数: 222)

### 发现

- [AUD-32104] [Major] `_apply_updates` 在锁外执行，两个并发 acquire/release 的旧快照可后到先应用，最终下发过期的 worker 数
  - 位置: src/shared/codec/decode_worker_budget.py:123-127（同型代码 134-137）
  - 证据:
    ```python
            updates = self._rebalance_locked()
            snapshot = self._snapshot_locked()
        self._apply_updates(updates)
        logger.debug("decode worker lease acquired: %s", snapshot)
        return DecodeWorkerLease(self, key)
    ```
  - 影响: 锁内计算 `updates` 后释放锁再调用回调（意图是避免持锁执行用户回调，方向正确），但没有序列化"计算→应用"。交错序列：T1(acquire A) 算出 {A:16} → T2(acquire B) 拿锁算出 {A:8,B:8} 并先应用 → T1 恢复后应用过期快照 {A:16}。终态 native/上下文 worker 数为 16，而登记表真实公平份额是 8——本模块的核心目标（公平预算、防超订）在并发开合播放/导出上下文时可被静默打破（超订或饥饿），直到下一次任意 lease 变更才自愈。docstring 只要求回调"线程安全"，线程安全的 setter 仍挡不住旧值后写覆盖。
  - 建议: 为回调应用引入序号/世代（每把锁内递增 generation，apply 前比对全局已应用世代，过期快照丢弃或重算重放），或用独立的 apply 锁 + 最新快照重读（锁外仅回调用户代码、但按世代单调递增地应用）。
  - 置信度: 高

- [AUD-32105] [Suggestion] `bind()` 替换回调后不回放当前 assigned 值，绑定瞬间错过 rebalance 的上下文将长期运行在过期值
  - 位置: src/shared/codec/decode_worker_budget.py:144-150
  - 证据:
    ```python
    def bind(self, key: str, apply: Callable[[int], None]) -> None:
        """Attach the live context setter to an already acquired lease."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                raise KeyError(key)
            entry.apply = apply
    ```
  - 影响: 实际消费方（topos_source.py:760-776）先 acquire（此时回调是 no-op lambda）再构造解码上下文再 bind。acquire→bind 窗口内若其他 lease 变化触发 rebalance，no-op 回调不会真正下发；bind 后也不补一次 `apply(entry.assigned)`，该上下文的 worker 数会停留在构造时的 initial，直到下一次外来 lease 变更。影响仅为预算不准（非崩溃），故列 Suggestion。
  - 建议: `bind()` 末尾在锁外立即调用一次 `apply(entry.assigned)`（复用 `_apply_updates` 的异常隔离），消除绑定窗口。
  - 置信度: 中

- [AUD-32106] [Suggestion] `acquire(capacity=...)` 永久改写进程级单例容量，且与 slice_threads_policy 的进程级线程值存在跨模块踩踏窗口
  - 位置: src/shared/codec/decode_worker_budget.py:112-114
  - 证据:
    ```python
        with self._lock:
            if capacity is not None:
                self._capacity = self._sanitize_capacity(capacity)
    ```
  - 影响: `DECODE_WORKER_BUDGET` 是模块级单例（第 222 行）；topos_source.py:763 以 `codec.slice_threads()` 当前值作为 capacity 传入——该进程级值可被 SLICE_THREADS_POLICY 的 export 登记临时调低。若播放源恰在 export 接管窗口内打开，单例容量被钉在低值，此后所有公平分配基于缩水容量，直到下一次带 capacity 的 acquire 覆盖。lease 释放也不恢复容量。属预算偏差而非错误崩溃。
  - 建议: capacity 作为 acquire 的临时覆盖（随 lease 释放恢复），或仅在容量增大方向采纳，或从 policy 层统一取值避免读瞬时值。
  - 置信度: 中

- [AUD-32107] [Suggestion] 类型注解缺口：`__exit__(self, *_exc)`、`describe() -> dict` 等未参数化
  - 位置: src/shared/codec/decode_worker_budget.py:70、195-215
  - 证据:
    ```python
    def __exit__(self, *_exc) -> None:
        self.release()
    ```
  - 影响: `*_exc` 无类型、`_snapshot_locked`/`describe` 返回裸 `dict`，静态检查对诊断结构（capacity/active/leases…）无约束；与项目"所有函数有类型注解"约定不符。
  - 建议: `*exc_info: tuple[type[BaseException] | None, ...]`（或 `object`），describe 返回 TypedDict。
  - 置信度: 高

### 通过项
- 线程安全（除 AUD-32104/32105/32106 外）：登记表全程 RLock 保护；`_apply_updates` 锁外执行避免持锁调用用户回调导致的重入死锁；RLock 兼容 lease.assigned 在回调内查询。
- 异常与取消路径：`_apply_updates` 的 `except Exception` 有 noqa 注明理由（坏回调不得污染登记表）并带 exc_info 告警，属回调边界的合理隔离，非掩盖领域错误；lease 提供 release/上下文管理器双路径，`release()` 幂等（`_released` 标志），重复 release 安全。
- 输入验证：`_sanitize_capacity`/`_sanitize_requested` 将容量/请求钳制到 [1,16]/[0,16]；空 key 显式 ValueError；重复 key 显式 ValueError。
- 算法正确性：`_rebalance_locked` 每上下文保底 1、按登记序 round-robin 到 requested 上限，`progressed` 保证有限终止；contexts>capacity 的有界超订有 diagnostics（oversubscribed）暴露。
- 性能模式：rebalance O(capacity×entries) 且仅 lease 变化时触发，热路径无锁竞争（assigned() 单次锁查询）。

---

## src/shared/codec/slice_threads_policy.py (行数: 162)

### 发现

- [AUD-32108] [Major] `_native_set`/`_native_get` 用裸 `except Exception` 吞掉包括 ToposCodecError 在内的领域错误且零日志，native 下发失败完全不可见
  - 位置: src/shared/codec/slice_threads_policy.py:48-70
  - 证据:
    ```python
        try:
            from src.shared.codec.topos_binding import ToposCodec

            codec = ToposCodec()
            got = codec.set_slice_threads(n)
            self._applied = int(got)
        except Exception:  # noqa: BLE001 — native 不可用时登记仍一致
            self._applied = None
    ```
  - 影响: 注释声明意图是"native 不可用时登记仍一致"，但 except 面同时覆盖 `set_slice_threads` 自身抛出的 ToposCodecError（如库不兼容、参数被拒）与所有其它异常，且无任何 logging。后果：register() 静默"成功"而进程级线程数从未改写——导出以为接管了线程预算，实际以旧值运行（QoS/性能缺陷不可观测）；同理 `_native_get` 失败返回 0 导致 baseline=None，全部注销后 `_apply_locked`（第 112-120 行）提前 return，native 值永久停留在最后登记值，"表清空时恢复原始值"的承诺失守。违反"不得宽泛 except 掩盖领域错误"铁律。describe() 虽暴露 applied=None，但无主动信号。
  - 建议: 收窄为 `except (ImportError, OSError, ToposCodecError)` 并至少 `logger.warning(..., exc_info=True)`；native_set 失败时在 register 返回值或诊断中显式上报"未下发"。
  - 置信度: 中（静默吞异常为客观事实；实际触发依赖 native 故障场景）

- [AUD-32109] [Suggestion] 每次 native 调用都新构 `ToposCodec()`，连带 load_library 全路径搜索与全量签名重配，register/unregister 反复付费
  - 位置: src/shared/codec/slice_threads_policy.py:56-57、66-68（对照 topos_binding.py:1253-1279、2794-2797）
  - 证据:
    ```python
            codec = ToposCodec()
            got = codec.set_slice_threads(n)
    ```
    （ToposCodec.__init__ 每次执行 `load_library()` + `_configure_signatures()` + `_verify_abi()` + `_warn_degraded_features()`，而 load_library 无缓存，逐候选路径 is_file + CDLL。）
  - 影响: 性能模式维度——每次 register/unregister（值变化时）及首次 baseline get 都完整走一遍库解析、签名配置、ABI 校验；`_warn_degraded_features` 的"一次性 warning"按实例计，重复构造会在日志中反复告警。频率低（编码器 open/close 一次）故仅 Suggestion，但与 4K 实时基准下"避免重复昂贵初始化"的模式不符。
  - 建议: 模块内惰性缓存一个共享 ToposCodec 实例（v1 ABI 纯函数线程安全，类文档已确认可共享），或经 bind() 由宿主注入现成实例。
  - 置信度: 高

- [AUD-32110] [Suggestion] 类型注解缺口：`bind(setter=None, getter=None)` 无参数/返回注解，`_order: list`、`_setter/_getter` 未参数化
  - 位置: src/shared/codec/slice_threads_policy.py:37-43
  - 证据:
    ```python
        self._order: list = []                # 登记顺序（最近在后）
        self._baseline: Optional[int] = None  # 首个登记者之前的进程值
        self._applied: Optional[int] = None   # 当前已下发 native 的值
        self._setter = None                   # 注入（测试/无 native 时）
        self._getter = None
    ```
  - 影响: setter/getter 回调类型（Callable[[int], None] / Callable[[], int]）无静态约束，`_order: list` 应为 `list[str]`；测试注入点类型不受检查。
  - 建议: 补 `Optional[Callable[[int], None]]` / `Optional[Callable[[], int]]` 与 `list[str]` 注解。
  - 置信度: 高

### 通过项
- 线程安全（核心场景）：登记/注销/诊断全程 `threading.Lock` 保护；native set/get 持锁串行化（docstring 明示 set 为廉价原子写，锁内 FFI 可接受）；`register` 对重复 key 的 order 移除/追加逻辑在锁内原子完成。
- 状态提交一致性：后设先得语义（`_order[-1]`）与 R-32 文档一致；表清空恢复 baseline 的主干路径正确（baseline 在首个登记者前捕获，unregister 重算一次性下发，`want != self._applied` 去重避免多余 native 写）。
- 异常与取消路径（除 AUD-32108 外）：unregister 对不存在 key 幂等返回；`reset_for_test` 明确不触碰 native 当前值并把责任交给调用方（注释清楚）。
- QoS 可观测性：异角色接管时记 warning（register 第 85-100 行），describe() 输出 effective/applied/baseline/registrations，满足 P1-05 归因需求。
- 输入验证：`register` 将 n 强转 int；`_apply_locked` 仅在值变化时下发。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 3 | AUD-32101, AUD-32104, AUD-32108 |
| Suggestion | 7 | AUD-32102, AUD-32103, AUD-32105, AUD-32106, AUD-32107, AUD-32109, AUD-32110 |

（合计 10 项发现；`__init__.py` 通过（无逻辑）。）
