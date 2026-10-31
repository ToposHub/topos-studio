# Batch 156 — Edit core: 混合模式（含内嵌 GLSL）、波形、自动化录制

- 日期: 2026-09-28
- 审查方式: 静态只读审查（全部文件完整 Read，交叉核对 `shaders/blend.glsl`、`clip_params.py`、`commands.py`、`timeline_v2.py`、`keyframe.py`、`edit_controller.py`、`timeline_export.py`、`edit_gpu_preview_renderer.py` 的调用/被调点）
- 编号区间: AUD-15600 – AUD-15616
- 汇总: Critical 0 / Major 2 / Suggestion 15

---

## src/features/edit/core/audio_timestretch.py (行数: 153)

### 发现

- [AUD-15600] [Suggestion] `sample_rate` 缺少正值校验，0/负值静默退化为最短帧长
  - 位置: src/features/edit/core/audio_timestretch.py:86-88, 100-102
  - 证据:
    ```python
    speed = float(speed)
    if speed < MIN_SPEED or speed > MAX_SPEED:
        raise ValueError(f"speed must be between {MIN_SPEED} and {MAX_SPEED}: {speed}")
    ...
    sample_rate = float(sample_rate)
    n = x.shape[1]
    frame_len = max(64, int(round(sample_rate * frame_ms / 1000.0)))
    ```
  - 影响: speed 有显式 ValueError，但 `sample_rate <= 0` 不报错：`frame_len` 被 `max(64, ...)` 钳到 64 样本（48 kHz 下约 1.3 ms），WSOLA 分析窗/搜索容差完全失真，输出时长虽仍按样本数正确但音质崩坏，且调用方（timeline_export 的降级 try/except）无法把配置错误与 DSP 失败区分开。
  - 建议: 在函数入口对 `sample_rate <= 0`（或非有限值）抛 ValueError，与 speed 校验对称。
  - 置信度: 高（校验缺失确定；实际影响低）

### 通过项
- speed 范围校验（0.1–8.0 抛 ValueError）、speed==1 与输入过短的"原样返回副本"路径与文档一致
- `_best_offset` 的相关窗边界安全：`hi ≤ n - frame_len` 且 slice 终点 `hi + H ≤ n`，`start` 经 `max(0, min(..., n - frame_len))` 钳制，无越界
- OLA 归一化正确：`norm < 1e-6 → 1.0` 防除零，Hann 50% overlap 恒和，尾窗除以自身窗值得到正确幅度
- 多声道共用单声道参考的对齐策略（保立体声像）实现正确
- 失败路径：纯函数无宽泛 except；导出侧调用（timeline_export.py:3186-3196）有 try/except 降级回变调路径，不会崩溃
- 线程安全 n/a（纯离线 numpy，无共享状态）

---

## src/features/edit/core/audio_transition_catalog.py (行数: 81)

### 发现
（无）

### 通过项
- 纯不可变数据模块（frozen dataclass + 元组推导），无副作用、无 I/O、无线程/资源问题
- 四条 curve 键唯一，`AUDIO_CURVE_TO_TYPE` 反查无歧义；`audio:` 前缀命名空间与 `is_audio_transition` 的集合判断自洽
- 与视频侧 transition_catalog 的隔离意图（避免 video 渲染计划看到 `audio:` 类型）有明确文档

---

## src/features/edit/core/audio_waveform.py (行数: 1560)

### 发现

- [AUD-15601] [Suggestion] `WaveformCache.__new__` 单例无锁双重检查竞态
  - 位置: src/features/edit/core/audio_waveform.py:199-208
  - 证据:
    ```python
    def __new__(cls) -> WaveformCache:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._cache: OrderedDict[str, WaveformData] = OrderedDict()
            cls._instance._pending: Dict[str, bool] = {}
    ```
  - 影响: `check-then-create` 未持锁，而 `WaveformCache()` 会从 worker 线程调用（run() 内 696/704/709 行）。若 worker 与 UI 线程并发首次构造，会出现两个实例、两套 `_cache/_pending` 字典（`_mutex` 是类级共享，不防重复实例化）：pending 标记互不可见 → 同一资产重复解码、缓存命中率下降。当前 UI 几乎必然先构造，触发概率低。
  - 建议: 在 `__new__` 内用类级 `_mutex` 包住整段创建，或模块级急切实例化。
  - 置信度: 高（竞态存在确定；触发概率低）

- [AUD-15602] [Suggestion] 播放抢占丢弃已完成的波形，恢复后从头全量重解码
  - 位置: src/features/edit/core/audio_waveform.py:699-701, 1064-1067
  - 证据:
    ```python
    waveform = self._extract_waveform()
    if self.isInterruptionRequested():
        raise _WaveformExtractionDeferred()
    ...
    def _on_extraction_deferred(self, asset_id: str, file_path: str) -> None:
        if WaveformCache().is_pending(asset_id):
            self._queued_requests[asset_id] = file_path
    ```
  - 影响: 若抢占发生在 `_extract_waveform()` 返回之后（解码已完成），完整结果被直接丢弃，重新排队后要整段重解码一遍——长素材（分钟级）白白重复全部 PyAV 解码 + 聚合 CPU/IO。这是"全量重算 vs 增量"维度上最直接的浪费点。
  - 建议: 解码完成后不再检查中断（直接 cache + emit），或把半成品 envelope 缓存供续算。
  - 置信度: 高

- [AUD-15603] [Suggestion] `generate_placeholder_waveform` 污染全局 `np.random` 且用进程随机 `hash()`
  - 位置: src/features/edit/core/audio_waveform.py:1530-1532
  - 证据:
    ```python
    seed = hash(asset_id) % 10000

    np.random.seed(seed)
    ```
  - 影响: (1) `np.random.seed` 重置进程级全局 RNG，影响其它 `np.random.*` 使用者且非线程安全；(2) Python 字符串 `hash()` 每次进程启动随机化（PYTHONHASHSEED），docstring 宣称的 "deterministic generation" 跨会话不成立，占位波形形状每次启动都变。
  - 建议: 改用 `np.random.default_rng(...)`（局部生成器）+ 与 hash 无关的稳定种子（如 sha1(asset_id)）。
  - 置信度: 高

- [AUD-15604] [Suggestion] 解码中途异常被吞后仍标记 `is_complete=True` 并写入磁盘缓存
  - 位置: src/features/edit/core/audio_waveform.py:842-843, 856-864
  - 证据:
    ```python
    except Exception as e:
        logger.warning(f"Audio decode error: {e}")
    ...
    waveform = WaveformData(
        ...
        is_complete=True
    )
    ```
  - 影响: 容器后半段损坏/截断的文件，前半段峰值被当作"完整"结果进内存缓存并 `save_waveform_to_disk` 持久化（缓存键是 path+mtime+size，文件不变则永不重算）；UI 无法区分截断波形与完整波形，长素材显示缺尾且跨会话复现。
  - 建议: 捕获到解码异常时置 `is_complete=False` 或跳过磁盘持久化（并在数据结构上允许 UI 提示不完整）。
  - 置信度: 高

- [AUD-15605] [Suggestion] `cancel_all` 在 GUI 线程无超时 `wait()`
  - 位置: src/features/edit/core/audio_waveform.py:1101-1107
  - 证据:
    ```python
    workers = tuple(self._workers.values())
    for worker in workers:
        if worker.isRunning():
            worker.requestInterruption()
    for worker in workers:
        if worker.isRunning():
            worker.wait()
    ```
  - 影响: 协作式中断只在解码帧间检查；若 worker 正阻塞在 `av.open()`/底层 IO（慢速网络源、SMB 挂起），`wait()` 无限期挂起调用线程（通常是被关闭项目/退出时的 GUI 线程）→ 界面冻结直至 FFmpeg 自身 IO 超时。
  - 建议: `wait(ms)` 带超时 + 超时告警，或改为后台 detached 清理。
  - 置信度: 中（取决于媒体源类型）

- [AUD-15606] [Suggestion] `peaks_max` 实为 |peak| 幅值包络，与 `WaveformData` 文档语义不符
  - 位置: src/features/edit/core/audio_waveform.py:74-75（文档）vs 616-619（实现）
  - 证据:
    ```python
    peaks_min: np.ndarray  # Negative peaks (downward)
    peaks_max: np.ndarray  # Positive peaks (upward)
    ...
    # Peak magnitude is max(|samples|): combining both envelope
    # halves keeps all-negative blocks (e.g. deep troughs) correct.
    self.peaks_max[:, seg] = np.maximum(
        self.peaks_max[:, seg], np.maximum(np.abs(bmin), np.abs(bmax)))
    ```
  - 影响: 全负样本块（深谷）的上半包络会画出与下半对称的正幅值，而非真正的正峰；文档声称 "Positive peaks (upward)"，依赖该语义的任何消费者（如未来做非对称显示/响度分析）会得到偏大值。显示层当前用 min/max 带渲染，视觉上是"幅值包络"，可接受但语义错位。
  - 建议: 统一文档与实现——要么文档改为 "max |peak| magnitude"，要么聚合时存真正的逐侧 min/max（与 finish() 尾部 643 行 `np.abs(tail).max` 一并改）。
  - 置信度: 高（不一致确定；显示影响低）

- [AUD-15607] [Suggestion] `_on_extraction_failed` 可能清掉新请求刚注册的回调（丢通知竞态）
  - 位置: src/features/edit/core/audio_waveform.py:1079（结合 969-981、719）
  - 证据:
    ```python
    # worker run()（跨线程，先执行）:
    WaveformCache().clear_pending(self._asset_id)
    self.extraction_failed.emit(self._asset_id, str(e))   # 排队投递到 GUI
    ...
    # _on_extraction_failed（GUI，后执行）:
    self._callbacks.pop(asset_id, None)
    ```
  - 影响: worker 在 run() 里先清 pending 再 emit（信号排队）；在信号送达前，同一 asset 的新 `request_waveform` 走"非 pending"分支注册新回调（甚至排进 `_queued_requests`）。随后旧的 failed 处理器执行，无条件 pop 掉新回调并对该 asset 发出失败信号——新一次（排队中的）提取随后成功时回调已丢，UI 停在加载态直到下次 paint 重新请求。
  - 建议: 失败处理器按"代际"/worker 归属校验后再清回调（如回调表记录 request 序号）。
  - 置信度: 中

### 通过项
- 后台生成与 UI 读取的共享状态全部经 `WaveformCache._mutex` 保护；缓存后 `WaveformData` 视为不可变，`finish()` 输出经非连续 transpose+reshape 拷贝，无跨线程写
- 取消/延迟路径：全程协作式 `requestInterruption()`（帧间检查、关闭容器后再抛 `_WaveformExtractionDeferred`），明确避免 terminate；deferred 与 failed 两类结果分离，不把抢占误报为媒体失败
- worker 生命周期：结果信号在 run() 内发出，但清理统一延迟到 `finished` 后 `deleteLater()`，规避 "QThread destroyed while running"；`set_playback_active` 还防御了 sip.isdeleted 的僵尸包装
- 磁盘缓存：版本号 + path+mtime+size 身份键防串档；tmp + `os.replace` 原子写；256 文件容量修剪；读侧 `allow_pickle=False` 且任何异常返回 None 走重新提取
- `get_samples_for_range` 对负时间、num_pixels<=0、空范围、通道号越界均有防御；reduceat 像素重采样的 starts 单调且界内，zoom 分支索引界内
- 播放优先级策略自洽：串行提取（MAX_CONCURRENT=1）、低优先级线程、批间 1ms 让出、播放期排队 + 播放结束发布 `_deferred_ready`
- ClipEffectWaveformWorker 从 clip in-point 起播以喂满有状态 EffectChain（与实时播放语义一致），seek 失败静默从头解码并用时间片过滤（正确，代价可接受）
- 增量渲染：ranged 二级缓存按 (asset, range, width) 键命中，避免每次 paint 重采样

---

## src/features/edit/core/automation_recorder.py (行数: 259)

### 发现

- [AUD-15608] [Major] `simplify_samples` 递归 Douglas-Peucker 无深度保护，噪声之字形采样触发 RecursionError，整段录制被上层静默丢弃
  - 位置: src/features/edit/core/automation_recorder.py:71-97（结合 src/app/controllers/edit_controller.py:2434-2437）
  - 证据:
    ```python
    def dp(start: int, end: int) -> List[_FaderSample]:
        ...
        if max_dist > threshold and max_idx > 0:
            left = dp(start, max_idx)
            right = dp(max_idx, end)
    ...
    result = dp(0, len(pts) - 1)
    ```
    调用方：
    ```python
    try:
        cmd = rec.stop()
        ...
    except Exception as e:
        logger.warning("Failed to commit automation recording %s/%s: %s", ...)
    ```
  - 影响: 采样在两值间交替（帧间偏离持续 > 0.005 阈值，如抖动的硬件推子/MIDI 控制器）时，`np.argmax` 总取第一个最大偏离点 → 每层递归只剥掉一个点，深度 O(n)、总扫描 O(n²)。按 60 fps 采集，持续约 30 s+（≥2000 样本、深度≈n/2 超过默认 1000 递归上限）`stop()` 抛 RecursionError，controller 的 `except Exception` 把它降级为一条 warning——该次自动化录制数据全部丢失、无 undo、UI 无提示。即使不越递归上限，O(n²) 的 Python 循环也会拖慢停止提交。
  - 建议: 改为显式栈的迭代 DP，或对超长采样先分段/限深简化；controller 侧对该路径单独上报失败而非静默。
  - 置信度: 高（递归无界与吞异常路径均已核实；触发需特定噪声形态）

- [AUD-15609] [Major] Write 模式"覆盖整段已录曲线"未实现：默认命令只追加关键帧，旧关键帧残留导致新旧曲线混杂
  - 位置: src/features/edit/core/automation_recorder.py:8-9（文档宣称）vs 219-253（实现）（结合 src/shared/keyframe.py:142-155、src/app/controllers/edit_controller.py:2443 附近）
  - 证据:
    ```python
    - Write：播放开始即持续写入，覆盖整段已录曲线。
    ...
    # 默认：构建 MacroCommand（每个关键帧一个 AddKeyframeCommand）
    return self._build_macro_command(self._track_id, self._param, keyframes)
    ```
    而 `KeyframeTrack.add_keyframe` 仅同帧替换：`if kf.frame == keyframe.frame: ... return`（keyframe.py:150-153），controller 提交前也不清理旧关键帧。
  - 影响: 对已有自动化曲线的轨道二次 Write 录制后，录制窗口内旧关键帧（帧号不同于新采样）原样保留；简化后的新关键帧（可能只有起止两点）与旧点并集插值，曲线在新旧值之间跳变——"覆盖"语义落空，播放结果错误。状态提交一致性维度：宏命令只含 Add，不含对录制区间的 Clear。
  - 建议: 默认命令改为"删除录制帧区间内旧关键帧 + 添加新关键帧"的复合宏（保持单次 undo），或在 stop() 前由调用方显式清理并文档化。
  - 置信度: 高（模块、命令、keyframe 语义、controller 三方核实）

- [AUD-15610] [Suggestion] `record`/`stop` 无并发保护（当前仅 UI 线程使用，约束未文档化）
  - 位置: src/features/edit/core/automation_recorder.py:169-184, 201-205
  - 证据:
    ```python
    def record(self, frame: int, value: float, fader_down: bool = True) -> None:
        if not self._active:
            return
        ...
    def stop(self, ...) -> Optional[Any]:
        ...
        self._active = False
        samples = self._samples
        self._samples = []
    ```
  - 影响: 类 docstring 写"播放循环中，每帧调 record"，若未来从播放线程采集、UI 线程 stop，`self._samples = []` 重绑后并发 `record` 的 append 落入新表，尾部样本静默丢失（GIL 下不崩溃）。当前 controller 的两个入口都在 UI 线程，现实风险低。
  - 建议: 加 `threading.Lock`，或类文档明确"仅限单一（UI）线程调用"。
  - 置信度: 高（无锁确定；当前风险低）

- [AUD-15611] [Suggestion] Touch 模式松手后的 release 时间回归未实现（文档描述与行为不符）
  - 位置: src/features/edit/core/automation_recorder.py:9（文档）vs 172-179（实现）
  - 证据:
    ```python
    - Touch：仅当用户**按住**推子时写入；松手后经 release 时间回到原曲线（停止写入）。
    ...
    elif mode == "touch":
        should_record = fader_down
    ```
  - 影响: 松手即停止写入，不生成 release 渐变；松手点之后曲线直接朝（可能很远的）下一个既有/新关键帧线性回归，听感跳变，与 §3.1 描述的 DAW 惯例不符。
  - 建议: stop/松手时补一小段 release 渐变关键帧，或在模块文档明确该行为交由调用方/后续任务实现。
  - 置信度: 中（可能属规划中的后续工作，但本模块文档未声明缺失）

- [AUD-15612] [Suggestion] `param_bounds` 与 KeyframeTrack 值域重复定义，存在漂移风险
  - 位置: src/features/edit/core/automation_recorder.py:229-234
  - 证据:
    ```python
    param_bounds = {
        "volume": (0.0, 2.0),
        "pan": (-1.0, 1.0),
        "mute": (0.0, 1.0),
    }
    ```
  - 影响: 注释称"与 KeyframeTrack 一致"，但属手抄副本；任一侧调整值域（如 volume 上限）后录制的关键帧 clamp 域与播放侧不一致。
  - 建议: 从 KeyframeTrack/共享常量单一来源导入。
  - 置信度: 高（重复确定）

### 通过项
- 信号防递归 n/a（模块不持有任何 Qt 信号，纯采集+命令生成，符合其"模块边界"声明）
- 单次 undo 目标达成：MacroCommand 聚合多个 AddAutomationKeyframeCommand，一次录制一个命令
- Touch/Latch 写入窗口语义本身正确（Latch 首触锁定、Touch 按住才写、未激活时 record 为 no-op）
- `stop()` 的样本清空/复位顺序正确，`cancel()` 完整重置状态；同帧去重保留最后值正确

---

## src/features/edit/core/blend_modes.py (行数: 256)

### 发现

- [AUD-15613] [Suggestion] `blend_rgb` 无条件 clip(0,1)（含 normal 模式），GPU normal 分支不 clip——HDR 浮点域复用时 CPU/GPU 将分叉
  - 位置: src/features/edit/core/blend_modes.py:213-222（对照 shaders/blend.glsl:88 `return src;`）
  - 证据:
    ```python
    if code == ClipBlendMode.NORMAL.code:
        return np.clip(overlay, 0.0, 1.0)
    func = _MODE_FUNCS.get(code)
    if func is not None:
        return np.clip(func(overlay, base), 0.0, 1.0)
    ...
    return np.clip(overlay, 0.0, 1.0)
    ```
  - 影响: 当前三个 CPU 调用点（timeline_export/timeline_preview_renderer/blend_frames_u8）输入均为 u8/u16 归一化到 [0,1]，CPU/GPU 数值一致、无实际错误。但作为共享 API：normal 模式 CPU 会把 >1.0 的输入砍到 1.0 而 GPU（`blend_channel` mode 0 直接 `return src`）保留原值；且 dodge/burn/`_clip_color_simple` 等公式整体隐含 [0,1] 假设。若日后接入 HDR float16/32 内部域（项目原则：HDR 不得无理由 clamp），预览（GPU）与导出（CPU）会在同一混合模式下给出不同结果。已核实 GPU 侧合成 scratch 纹理为 `dtype='f1'` 8-bit（edit_gpu_preview_renderer.py:3348-3350），当前管线域自洽，故仅记前瞻风险。
  - 建议: docstring 已写 [0,1] 域，建议再加断言/显式拒绝超域输入，或为 normal 分支去掉 clip 并按数据域分派，避免 HDR 复用时静默分叉。
  - 置信度: 高（不对称确定；影响为前瞻性）

### 通过项
- 与 blend.glsl 公式逐项对齐核验通过：multiply/screen/darken/lighten/difference/exclusion 完全一致；overlay/hard_light 的 0.5 分支边界（CPU `d <= 0.5` 取 low vs GPU `step(0.5, dst)` 取 high）在 d==0.5 处两支公式数值相等（2·s·d = s = 1-2(1-s)(1-d)），无可见差异；color_dodge/burn 的 1e-6 防护与饱和分支两侧一致
- soft_light W3C 分段一致（含 `sqrt` 的 max(0) 防护；d==0.25 处 g_low(0.25)=0.5=sqrt(0.25) 连续）
- `_set_sat` 平局（两通道并列 max/min、纯灰）与 GPU `blend_set_sat` 的 bvec 分派数值等价：mid==mx 时 `sat_mid` 化简为 s、mid==mn 时为 0，argsort 的非稳定性不改变结果；纯灰路径 CPU 显式置 0 后经 set_lum 与 GPU 的 (s,s,s) 路径同收敛
- `_lum` 系数 0.3/0.59/0.11 与 GLSL `dot(c, vec3(0.3, 0.59, 0.11))` 一致；`_clip_color_simple` 的两段顺序缩放与 GLSL 顺序 if 等价，分母均有 1e-6 防护
- BGR↔RGB 翻转约定（`[..., ::-1]`）在 CPU 三个调用点统一，HSL 系模式按 RGB 加权符合 W3C
- `blend_frames_u8` 的 opacity clamp、`+0.5` 四舍五入、255 上限处理正确
- 未知 mode 编号/字符串回退 normal，不会崩溃
- 无循环；无资源/线程问题（纯函数）

---

## src/features/edit/core/blend_shader.py (行数: 65)

### 发现

- [AUD-15614] [Suggestion] `_read_blend_glsl` 吞 OSError 且零日志：blend.glsl 缺失时整个 GPU 预览初始化失败且根因不可见
  - 位置: src/features/edit/core/blend_shader.py:33-38（结合 edit_gpu_preview_renderer.py:1625-1628 的无独立 try 的 `ctx.program(...)` 调用）
  - 证据:
    ```python
    def _read_blend_glsl() -> str:
        try:
            with open(_BLEND_PROGRAM_PATH, encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return ""
    ```
  - 影响: 文件缺失/权限/编码问题时返回空串，fragment shader 随即缺少 `blend_channel` 定义；`_blend_program` 的创建不在独立 try 内（不像 transition/dip 等 pass 有各自的降级），外层 `_init` 大 try 捕获后让**整个 GPU 预览渲染器**初始化失败——比"仅禁用混合"严重得多，且日志只有泛化的 shader 编译错误，看不到是 blend.glsl 读失败。
  - 建议: OSError 时 `logger.error` 带路径并向上抛（或返回 None 让调用方只禁用 blend pass），使失败范围与根因都可见。
  - 置信度: 高

### 通过项
- GLSL 浮点字面量规范：blend.glsl 全部数值常量带 `.0`；`1e-6` 为合法浮点字面量（非 int），无隐式类型问题
- 无循环 → 不存在循环边界非常量问题
- 与 color/合成页 blend 语义无同名冲突：`clip_params.ClipBlendMode` 文档明确区分图层混合与 `timeline_render_plan.BlendMode` 转场混合两个命名空间；本 shader 的 mode 编号与 `_CLIP_BLEND_CODES`（枚举序数 0-15）一致，blend.glsl 分支编号 1-15 逐一核对无错位
- 混合域自洽：GPU 合成 scratch 纹理为 f1 8-bit UNORM，[0,1] 假设在当前编辑页管线内成立（见 AUD-15613 的前瞻说明）
- source-over 合成公式 `mix(base.rgb, mixed, u_opacity * overlay.a)` 与 CPU 导出 `blended * src_a + composite * (1 - src_a)` 语义一致（非预乘、不透明底）

---

## src/features/edit/core/bwf.py (行数: 210)

### 发现

- [AUD-15615] [Suggestion] RIFF 尺寸超过 4 GiB 时被静默 clamp，产出非法 WAV
  - 位置: src/features/edit/core/bwf.py:147-152
  - 证据:
    ```python
    riff_size = min(len(new_data) - 8, 0xFFFFFFFF)
    new_data = (
        new_data[:4]
        + struct.pack("<I", riff_size)
        + new_data[8:]
    )
    ```
  - 影响: 48 kHz/24 bit/立体声约 4 小时的录音超过 4 GiB；此时 size 字段被钳到 0xFFFFFFFF，标准 RIFF 解析器按错误长度读 chunk 边界，文件实质损坏且过程无任何告警（注入仍返回 True）。>4 GiB 的 WAV 应走 RF64。概率受限于 PyAV WAV muxer 自身的 4 GiB 限制，但一旦发生即为静默数据损坏。
  - 建议: 超限时拒绝注入并显式报错（或留 RF64 迁移 TODO），不要静默 clamp。
  - 置信度: 高（行为确定；触发概率低）

- [AUD-15616] [Suggestion] `inject_bext_chunk` 不校验 chunk size 越过文件末尾，畸形输入仍"成功"
  - 位置: src/features/edit/core/bwf.py:105-112, 137-139
  - 证据:
    ```python
    def _iter_chunks(data: bytes):
        pos = 12
        while pos + 8 <= len(data):
            cid = data[pos:pos + 4]
            size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
            yield cid, pos + 8, size
            pos += 8 + size + (size & 1)
    ...
    if cid == b"fmt ":
        insert_at = offset + size + (size & 1)
        break
    ```
  - 影响: 完全信任 size 字段。下载不完整/已损坏的 WAV 中 fmt 的 size 超出文件实际长度时，`insert_at > len(data)`，`data[insert_at:]` 为空 → bext 追加在被截断的文件末尾，返回 True"成功"；上层以为元数据已写入，实际产出仍是畸形文件（读侧 `read_bext_chunk` 有 `len(body) < 602` 防护，写侧没有对应校验）。
  - 建议: `insert_at > len(data)` 或任一 chunk 声称的 data 区越过 EOF 时返回 False 并告警。
  - 置信度: 高

### 通过项
- 读侧输入验证充分：非 RIFF/WAVE、文件不可读、bext body < 602 字节均安全返回 None；`allow` 无 pickle 类风险（纯 struct 解析）
- bext v0 602 字节布局偏移逐项核对正确（Description 0-255 / Originator 256-287 / Reference 288-319 / Date 320-329 / Time 330-337 / TimeReference 338-345 / Version 346-347 / Reserved 348-601）
- `_iter_chunks` 对 size=0 与超大 size 均能推进或终止（pos 恒增），无死循环
- 写入原子性：tmp + `os.replace`，失败路径清理 tmp、原文件不动；odd size 的 word 对齐填充与 coding history 的 NUL 剥离正确
- `time_reference_samples`/`version` 打包前做 64/16 位掩码，溢出不抛异常
- ASCII 编码统一 `errors="replace"`，无 UnicodeEncodeError 路径

---

## 审查维度总览

| 维度 | 结论 |
|---|---|
| 1. 线程安全 | 波形缓存互斥保护到位（AUD-15601 单例竞态、AUD-15610 recorder 无锁为残余风险）；recorder 现仅 UI 线程使用 |
| 2. 资源生命周期 | worker finished 后 deleteLater、容器关闭后再抛、磁盘缓存修剪齐备；无泄漏路径 |
| 3. GLSL/HDR 正确性 | 公式 CPU/GPU 逐项等价（含边界与平局）；字面量/循环规范；域为 [0,1] 8-bit，当前自洽，HDR 复用风险见 AUD-15613；AUD-15614 为健壮性 |
| 4. 信号防递归 | 涉审模块无 Qt 信号，n/a；波形回调异常有 try/except 隔离 |
| 5. 状态提交一致性 | AUD-15609（Write 覆盖语义缺口）为本批最重要发现之一 |
| 6. 异常与取消路径 | 协作式中断设计良好；AUD-15602/15604/15605/15608 为残余问题；变速失败由调用方降级兜底 |
| 7. 输入验证 | bwf 读侧充分、写侧缺口（AUD-15615/15616）；波形对畸形音频容错（无流/无时长/解码异常均有路径） |
| 8. 性能模式 | ranged 缓存 + reduceat 聚合已是增量/向量化；AUD-15602（抢占后全量重算）与 AUD-15608（O(n²) 简化）为热点 |
| 9. 类型注解 | 七文件注解覆盖良好，仅个别 `Callable`/`make_command` 参数缺泛型参数，未达单列发现门槛 |
