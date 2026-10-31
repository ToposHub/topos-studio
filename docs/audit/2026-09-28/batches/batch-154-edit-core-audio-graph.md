# Batch 154 — Edit Core 音频图渲染/快照/健康检查 静态审查

- 日期: 2026-09-28
- 审查文件:
  - `src/features/edit/core/audio_graph_renderer.py` (621 行)
  - `src/features/edit/core/audio_graph_snapshot.py` (359 行)
  - `src/features/edit/core/audio_health.py` (322 行)
- 结论: Critical 0 / Major 6 / Suggestion 9
- 跨模块核实（只读）: `audio_playback.topological_sort_buses`（环返回契约）、`audio_capabilities.get_audio_capabilities`（缓存语义）、`shared/keyframe.py get_value_ramp`（keyframes 并发访问面）、renderer 生产调用点（`stem_export.py:263-264`、`timeline_export.py:390-391, 3928-3929`，均为新建实例）。

---

## src/features/edit/core/audio_graph_renderer.py (行数: 621)

### 发现

- [AUD-15400] [Major] 节点持有模型 KeyframeTrack 活引用，导出线程与 UI 编辑并发读写竞态
  - 位置: src/features/edit/core/audio_graph_renderer.py:77, 170, 310-339
  - 证据:
    ```python
    self.automation = dict(getattr(track, "automation", {}) or {})   # :77 浅拷贝，值仍是模型对象
    ...
    self._master_automation = dict(getattr(sequence, "master_automation", {}) or {})  # :170 同样
    ...
    mute_ramp = np.array(
        mute_track.get_value_ramp(                                     # :311-315 导出线程迭代 live keyframes
            float(current_frame), fps_effective, samples.shape[-1],
            self._sample_rate, 0.0,
        ),
    ```
  - 影响: dict 浅拷贝只隔离字典结构，KeyframeTrack 对象与 UI 模型共享。导出线程经 `get_value_ramp → get_value_float` 访问 `self.keyframes[0]`/遍历列表（src/shared/keyframe.py:294-328），UI 线程同时增删关键帧时可能 IndexError 中断导出或读到撕裂值。与 `audio_graph_snapshot.py` 自述原则"禁止播放线程读取 UI 对象"（:8、:203）直接冲突——导出路径未遵守同一约束。
  - 建议: 投影时深拷贝关键帧数据（如 snapshot 的 `_automation_signature` 做法，拷成 `(frame, value)` 元组），或导出期间冻结/锁保护 sequence 的 automation 访问。
  - 置信度: 高

- [AUD-15401] [Major] render_bus_stage 丢弃 cycle_buses，环中 Bus 音频静默丢失且无日志
  - 位置: src/features/edit/core/audio_graph_renderer.py:458
  - 证据:
    ```python
    sorted_buses, cycle_buses = topological_sort_buses(bus_ids, routing)

    # 确保每个 Bus 都有 buffer
    for bid in bus_ids:
        if bid not in bus_inputs:
            bus_inputs[bid] = np.zeros_like(output)
    ```
    `cycle_buses` 之后从未被使用（全文件无第二处引用）。
  - 影响: 被调用方契约明确"环中的 Bus 全部排除并返回，调用方应记录日志"（audio_playback.py:303-309），且本批 snapshot 模块自述"错误送到 UI/日志，不允许无声跳过"（audio_graph_snapshot.py:16-17）。导出路径检测到环后既不处理也不记录，环内 Bus 及其上游贡献的音频无声消失，用户无从得知原因。
  - 建议: 与 snapshot 校验结果联动：`cycle_buses` 非空时记日志（含 node IDs）并至少上报 warning；或复用 `validation_errors` 在导出前拒绝/提示。
  - 置信度: 高

- [AUD-15402] [Major] 普通/Bus 轨路由到未知 bus 目标时行为不一致，普通轨音频写入孤儿缓冲后静默丢弃
  - 位置: src/features/edit/core/audio_graph_renderer.py:405-418, 461-463, 547
  - 证据:
    ```python
    if target is not None:                       # :405 route_track_output
        if target not in bus_inputs:
            bus_inputs[target] = np.zeros_like(output)   # 未知目标也创建缓冲
        bus_inputs[target][:, :n] += samples
    ...
    for bid in bus_ids:                          # :461-463 仅注册 bus 有消费者
        if bid not in bus_inputs:
            bus_inputs[bid] = np.zeros_like(output)
    ...
    if target is not None and target in bus_inputs:   # :547 Bus→未知目标兜底进 master
        bus_inputs[target][:, :n] += bus_samples
    else:
        output[:, :n] += bus_samples
    ```
  - 影响: `output_bus_id`/send 指向不存在的 bus（如 bus 已删除但轨的路由未清理）时，普通轨样本累积进一个永不被消费的孤儿缓冲——该轨音频在导出中完全丢失且无任何提示；而 Bus 轨同样场景却兜底进 master。两类路由行为不一致，且违反"不允许无声跳过"。snapshot 层虽能校验出未知目标，但 renderer 不消费校验结果。
  - 建议: 路由前校验目标存在性：未知目标按 Bus 阶段语义统一兜底到 master（或整体拒绝并报错），并记日志。
  - 置信度: 高

- [AUD-15403] [Suggestion] configure_from_sequence 不清空旧节点/限幅器状态，非幂等
  - 位置: src/features/edit/core/audio_graph_renderer.py:161-173
  - 证据:
    ```python
    def configure_from_sequence(self, sequence: Any) -> None:
        audio_tracks = getattr(sequence, "audio_tracks", ()) or ()
        for track in audio_tracks:
            node = _TrackNode(track, self._sample_rate)
            self._nodes[node.track_id] = node        # 旧 track_id 从不移除
    ```
  - 影响: 同一实例二次配置时，已删除 track/bus 的旧节点（含有状态 EQ/FX）残留于 `self._nodes`，会混入 solo 闭包（:193）与 bus 拓扑（:447-457）；`_master_limiter` 跨配置保留连续状态。当前生产调用点均新建实例（stem_export.py:263-264、timeline_export.py:390-391/3928-3929），属潜在风险而非现行缺陷。
  - 建议: 方法开头 `self._nodes.clear()` 并复位 `_master_limiter`，使重复配置幂等。
  - 置信度: 高（代码事实）；影响为潜在

- [AUD-15404] [Suggestion] Bus pan automation 逐样本 Python 循环，导出热路径未向量化
  - 位置: src/features/edit/core/audio_graph_renderer.py:526-531
  - 证据:
    ```python
    if pan_ramp is not None and bus_samples.shape[0] >= 2:
        bus_samples = bus_samples.copy()
        for i in range(min(len(pan_ramp), bus_samples.shape[-1])):
            lg, rg = balance_gains(float(pan_ramp[i]))
            bus_samples[0, i] *= lg
            bus_samples[1, i] *= rg
    ```
  - 影响: 每样本两次 Python 函数调用；10 分钟 48kHz 导出约 2880 万次迭代。track 路径同类逻辑已向量化（:356-367，纯 numpy cos/sin/clip），Bus 路径显著拖慢导出。
  - 建议: 参照 :356-367 用 `np.clip` + 向量化算式替代逐样本循环。
  - 置信度: 高

- [AUD-15405] [Suggestion] mono 源 pan ramp 未 clip 到 [-1,1]，与 stereo 路径及静态路径不一致
  - 位置: src/features/edit/core/audio_graph_renderer.py:356-364
  - 证据:
    ```python
    if source_channels <= 1:
        theta = (pan_ramp + 1.0) * 0.5 * (np.pi / 2.0)   # pan_ramp 未 clip
        ...
    else:
        pr = np.clip(pan_ramp, -1.0, 1.0)                # stereo 路径有 clip
    ```
  - 影响: automation pan 曲线值超出 [-1,1] 时（模型未强制范围的情况下），mono equal-power 增益最大可到 √2，与 clip 后的 stereo 路径、静态 `pan_gains_for_channels` 行为不一致，可能引入意外过电平（靠 master limiter 兜底）。
  - 建议: mono 分支同样先 `np.clip(pan_ramp, -1.0, 1.0)`。
  - 置信度: 中

- [AUD-15406] [Suggestion] `audible: set = None` 类型注解应为 Optional[set]
  - 位置: src/features/edit/core/audio_graph_renderer.py:200, 427
  - 证据:
    ```python
    def is_track_audible(self, track_id: str, has_solo: bool, audible: set = None) -> bool:
    ...
        audible_solo: set = None,
    ```
  - 影响: 类型注解与默认值矛盾，静态检查（mypy）会报错；项目要求强类型注解。
  - 建议: 改为 `Optional[set]`（或 `Optional[frozenset]`）并加 `from typing import Optional`（已导入）。
  - 置信度: 高

### 通过项
- 线程安全（设计声明）: 类 docstring 明确单线程使用（:22-30），实时路径不经本类；与调用点（导出线程单线程）一致——除 AUD-15400 的活引用问题外无共享可变状态暴露。
- 资源生命周期: 模块不持有文件/设备/GPU 资源，节点仅含 numpy/DSP 状态，随实例 GC 释放，无泄漏路径。
- 数值正确性（其余）: `_apply_declick_edges` 的 `ramp_len = max(1, int(...))` 无除零；mute gate 语义（`<=0.5` 为有声）与 de-click 顺序正确；`get_value_ramp` 内部对 `fps<=0 / sample_rate<=0 / num_samples<=0` 有防护（keyframe.py:296-297）；fps 回退链（显式参数 > clip_fade > 24.0）有注释依据（:299-306）。
- 信号防递归: 本文件无 Qt 信号。
- 状态提交一致性: 节点图由 `configure_from_sequence` 从 sequence 一次性投影，与导出循环消费同一 sequence，无中间态错位。

---

## src/features/edit/core/audio_graph_snapshot.py (行数: 359)

### 发现

- [AUD-15410] [Major] SnapshotHistory 无锁，却按文档契约供播放线程跨线程消费并遍历 live sequence
  - 位置: src/features/edit/core/audio_graph_snapshot.py:322-327, 329-331, 351-359
  - 证据:
    ```python
    class SnapshotHistory:
        """快照历史（§1.4 原子换图基础）。
        维护 revision 单调递增的快照序列，供播放线程在 block 边界原子换图。"""
        def __init__(self) -> None:
            self._current: Optional[AudioGraphSnapshot] = None
            self._revision: int = 0
    ...
        def needs_rebuild(self, sequence: "TimelineSequence") -> bool:
            """...播放线程在 block 边界调用：若 hash 变化则原子换图。"""
            candidate = build_audio_graph_snapshot(sequence, self._revision)
    ```
  - 影响: 类被文档定位为跨线程换图原语，但 `update()`（`self._revision += 1` 非原子读改写）与 `needs_rebuild()`/`.current` 读取间无任何锁；且 `needs_rebuild` 在调用方线程**遍历 live TimelineSequence 的列表/dict**（builder :215-258 迭代 `audio_tracks`/`track.clips`/`track.sends`），若 UI 线程同时增删 clip/track，则发生共享列表并发读写——漏项、错项或 "dictionary changed size during iteration"。这恰是模块自身要解决的核心问题（"禁止播放线程读取 UI 对象"，:8、:203），`needs_rebuild` 本身违反了该原则。
  - 建议: `update()` 与读取加锁；`needs_rebuild` 改为接收不可变快照/由 UI 线程预构建的 candidate，而非在播放线程重新遍历 live 模型。
  - 置信度: 高（代码与 docstring 均为实据；模块头 :19-21 自述当前实时路径未接线，属按契约接通时必然触发的缺陷）

- [AUD-15411] [Suggestion] needs_rebuild 每次全量重建快照 + sha256，作音频 block 边界调用成本过高
  - 位置: src/features/edit/core/audio_graph_snapshot.py:104-131, 351-359
  - 证据:
    ```python
    candidate = build_audio_graph_snapshot(sequence, self._revision)
    if self._current is None:
        return True
    return candidate.stable_hash != self._current.stable_hash
    ```
    而 `stable_hash` 为 property，每次访问重算（repr 所有 EQ/FX params 与全部关键帧 → json → sha256，:114-131）。
  - 影响: 按 docstring 的用法（每个音频 block 边界调用一次，约每 21ms@1024 样本），一次调用要遍历全部 clip/track、repr 序列化所有 DSP 参数与 automation 关键帧——音频线程上的重负载，是 underrun 的典型来源。`self._current.stable_hash` 同样每次重算，无缓存。
  - 建议: 快照构建时缓存 `stable_hash`（dataclass 字段或 `functools.cached_property` 不可用于 frozen，可在 `__post_init__` 用 object.__setattr__ 缓存）；block 边界只比较预构建快照的缓存 hash。
  - 置信度: 中（当前实时路径未接线，属前瞻性性能风险）

- [AUD-15412] [Suggestion] diff() 完全不比较 clips 字段
  - 位置: src/features/edit/core/audio_graph_snapshot.py:133-152
  - 证据:
    ```python
    if self.master != other.master:
        diffs.append(f"master: {self.master} → {other.master}")
    # track diff
    self_tracks = {t.track_id: t for t in self.tracks}
    ...
    elif self_tracks[tid] != other_tracks[tid]:
        diffs.append(f"track changed: {tid}")
    return diffs
    ```
  - 影响: docstring 称"字段级，供调试/日志"，但 clip 移动/换素材/改 speed 等变化不产生任何 diff 行——排查换图/同步问题时输出误导性"无差异"结论，而 `stable_hash` 实际已包含 clips。
  - 建议: 补充 clips 的按 `clip_id` 增删改比较，与 `stable_hash` 字段覆盖对齐。
  - 置信度: 高

### 通过项
- 快照不可变性（重点核查通过）: 四个 dataclass 均 `frozen=True`；builder 产物全部值拷贝/tuple 化——`clips`/`tracks`/`validation_errors` 传 `tuple(...)`（:275-283），`sends` 为 `tuple(tuple)`（:237-241），clip FX/EQ/FX 链摘要用 `repr` 字符串（:159-184），automation 摘要拷贝为 `(frame, value)` 值元组（:187-194）而非持对象引用。快照对象本身是真 immutable。
- 状态提交一致性: `stable_hash` 明确排除 revision（:104-113 注释），同 sequence 不同 revision 不误判重建；字段覆盖为 edit signature 的严格超集（含 asset_id/source_start/is_enabled，:110-112）；undo 后 hash 回到旧值，`needs_rebuild` 判定正确。
- 数值正确性: fps 的 Fraction 处理有 `den` 除零防护（:271-273）。
- 异常处理: `_validate_snapshot` 的 `except Exception` 将异常文本转为 `validation_errors` 条目上报（:307-312），非静默吞掉，符合"错误送到 UI/日志"原则，可接受。
- 资源生命周期: 纯数据模块，无外部资源。

---

## src/features/edit/core/audio_health.py (行数: 322)

### 发现

- [AUD-15420] [Major] has_blocking 的 DSP 分支是死代码（pass），DSP 后端缺失既不阻断也不告警
  - 位置: src/features/edit/core/audio_health.py:158-168, 171-183, 231-236
  - 证据:
    ```python
    @property
    def has_blocking(self) -> bool:
        if not self.dsp_backend_available:
            # numpy/pyav 缺失是阻断性的（音频链不可用）；scipy 缺失只降级 EQ/LUFS
            pass
        if self.disk.blocking:
            return True
    ```
  - 影响: 注释断言 numpy/pyav 缺失"是阻断性的"，代码却是 `pass`；`has_warnings` 同样不含 `dsp_backend_available`。结果：numpy/pyav 缺失时 `has_blocking=False`、`has_warnings=False`，`to_text` 末行输出 "Status: OK"，与同报告第 206 行的 "DSP backend : DEGRADED" 自相矛盾——健康检查在最严重的降级场景下给出 OK 结论，preflight 不会阻止注定失败的导出/录音。
  - 建议: 补上 `if not self.dsp_backend_available: return True`（或至少纳入 has_warnings），并为该分支补单测（现有 test_audio_health_report.py 未覆盖）。
  - 置信度: 高

- [AUD-15421] [Major] output_device_available 可能取自"跳过探测"的缓存默认值，健康报告误报设备 OK
  - 位置: src/features/edit/core/audio_health.py:268
  - 证据:
    ```python
    caps = get_audio_capabilities(check_output_device=True)
    ...
    output_device_available=caps.output_device.available,
    ```
    而 `get_audio_capabilities` 的缓存语义是"check_output_device 仅在首次探测时生效；默认 False 以避免 DSP 路径触发 Qt 依赖"（audio_capabilities.py:190-192），且跳过时构造 `CapabilityResult(True, "跳过探测")`（audio_capabilities.py:167）。
  - 影响: 进程内任何 DSP 热路径先以默认 `check_output_device=False` 填充缓存后，健康报告这次 `check_output_device=True` 的调用拿到的仍是"跳过探测→available=True"的占位结果——无输出设备的机器上报告显示 "Output device : OK"，恰好误导最需要该信息的场景。
  - 建议: 健康报告路径调用 `reset_audio_capabilities_cache()` 后再以 `check_output_device=True` 探测，或 capabilities 层区分"未探测"与"可用"（unknown ≠ available）。
  - 置信度: 高（两处代码交叉证实）

- [AUD-15422] [Suggestion] 磁盘/目录探测使用无日志的宽泛 except，失败时降级为"未知"且不阻断
  - 位置: src/features/edit/core/audio_health.py:54-63, 73-84, 285-286
  - 证据:
    ```python
    try:
        ...
        usage = shutil.disk_usage(check_path)
        return usage.free / (1024.0 ** 3)
    except Exception:
        return None
    ...
    except Exception:
        return None
    ...
    disk_warning = free_gb is not None and free_gb < DISK_WARNING_THRESHOLD_GB
    disk_blocking = free_gb is not None and free_gb < DISK_BLOCKING_THRESHOLD_GB
    ```
  - 影响: `.clinerules` 禁止宽泛 except。探测异常（权限/IO 错误）被静默吞掉无日志；`free_gb=None` 时 warning/blocking 均为 False——磁盘真实耗尽但探测失败时报告不产生任何阻断标记。
  - 建议: 至少 `logging.debug/warning` 记录异常原因；probe 失败可考虑归入 warning 级而非完全静默。
  - 置信度: 高

- [AUD-15423] [Suggestion] probe_dir_size_mb 同步 os.walk 整个缓存目录，大目录会卡住调用线程
  - 位置: src/features/edit/core/audio_health.py:74-82
  - 证据:
    ```python
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    ```
  - 影响: 对多 GB、数十万文件的 render_cache 目录逐文件 `getsize`，若健康面板在 UI 线程构建报告会造成可感知的卡顿/冻结。
  - 建议: 移到后台线程，或限制遍历深度/文件数上限，或复用已有的缓存大小记账。
  - 置信度: 中

- [AUD-15424] [Suggestion] build_audio_health_report 的 sequence 参数缺类型注解
  - 位置: src/features/edit/core/audio_health.py:245
  - 证据:
    ```python
    def build_audio_health_report(
        sequence=None,
    ```
  - 影响: 项目要求强类型注解；`sequence` 应为 `Optional["TimelineSequence"]`。
  - 建议: 补 `Optional["TimelineSequence"]`（TYPE_CHECKING 前置导入，与 snapshot 模块一致）。
  - 置信度: 高

### 通过项
- 快照不可变性: DiskStatus/CacheStatus/AudioHealthReport 均 `frozen=True`，tuple 字段，`to_dict/to_text` 只读——报告确为不可变快照（:23-24 声明成立）。
- graph 校验异常路径: `build_audio_graph_snapshot` 包裹 try/except 且把异常转为 `graph_valid=False` + 错误条目（:273-281），错误可见、不崩溃，处理得当。
- 数值正确性: disk 阈值比较对 `None` 有防护（:285-286）；GB/MB 换算幂次正确。
- capabilities 调用崩溃风险: `get_audio_capabilities` 内部各探测均有 try/except 防护（audio_capabilities.py:139-155），健康报告构建不会因设备探测异常而抛出。
- 信号防递归: 本文件无 Qt 信号。
- 输入验证（媒体元数据）: 本批三文件均不解析媒体文件元数据（属 decoder 层职责），不适用。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 6 | AUD-15400, 15401, 15402, 15410, 15420, 15421 |
| Suggestion | 9 | AUD-15403, 15404, 15405, 15406, 15411, 15412, 15422, 15423, 15424 |
