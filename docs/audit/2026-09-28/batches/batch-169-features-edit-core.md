# 批次报告：batch-169 [P1] src/features/edit/core/

编号区间：AUD-16900–AUD-16999。文件 8 个，共 2,737 行，全部 Read 通读。
统计：Critical 0 / Major 6 / Suggestion 10。

交叉验证说明：`EditTrack.with_clip_removed` 会联动清理引用该片段的转场
（timeline_v2.py:2437-2440）；`Rational`（src/shared/media/metadata.py:26）为
`@dataclass(frozen=True)`，仅定义 `__float__/__mul__/__truediv__`，无任何比较运算符；
`evaluate_nested_frame` 在 src/ 无生产调用方（预览/导出各自实现 `_nested_render_stack`
守卫，tests/edit/test_nested_sequence.py 覆盖该函数）；`MulticamSyncWorker` 无生产
`requestInterruption` 调用方。

---

## src/features/edit/core/gpu_transition_shaders.py (行数: 279)

### 发现

- [AUD-16900] [Major] iris 圆/椭圆半径上限 1.55 在宽幅画幅下永远盖不住左右边缘中点与四角，终点处全帧跳变
  - 位置: src/features/edit/core/gpu_transition_shaders.py:183
  - 证据:
    ```glsl
    vec2 size = vec2(textureSize(u_left_tex, 0));
    float aspect = size.x / max(size.y, 1.0);
    vec2 point = v_texcoord * 2.0 - 1.0;
    point.x *= aspect;
    if (u_transition_kind == 1 || u_transition_kind == 2) {
        float radius = 1.55 * progress;
    ```
    （配合 173-176 行 `if (progress >= 1.0) return 1.0;` 的端点早退）
  - 影响: `point` 的 x 已乘 aspect。16:9 时 aspect≈1.778：右边缘中点距离 1.778、
    角点距离 √(1.778²+1)≈2.04，均 > 1.55——iris 在整个过渡过程中从未覆盖左右
    边缘带与四角，这些区域一直显示 outgoing 画面，直到 progress=1.0 被端点早退
    一次性翻转为全 1（硬跳变）。覆盖完整画面需 radius ≥ √(aspect²+1)。4:3
    （角距 1.667）同样角部未覆盖；仅近方形画幅（aspect≤1.18）不受影响。CPU 路径
    transition_mask.py:136 用同一常数 1.55，预览与导出一致地呈现该缺陷。
  - 建议: 将半径改为按画幅自适应（如 `radius = progress * sqrt(aspect*aspect + 1.0)`，
    椭圆同理对 x/y 分别按 aspect 缩放），CPU transition_mask.py 同步修改；或如多边形
    注释所述"intentionally oversized"那样把 1.55 提高到 ≥2.4。
  - 置信度: 高（几何推导自代码常量；"1.55 是有意美学选择"的可能性无法排除，但
    CPU 侧 polygon 注释表明作者关注过满幅覆盖问题）

- [AUD-16901] [Suggestion] u_reverse 仅在 wipe 分支生效，circle/ellipse/polygon 静默忽略
  - 位置: src/features/edit/core/gpu_transition_shaders.py:208
  - 证据:
    ```glsl
    if (u_transition_kind == 3) return polygon_inside(point);
    ...
    if (u_reverse != 0) progress = 1.0 - progress;
    ```
  - 影响: kind 1/2 在 191/189 行提前 return、kind 3 在 193 行 return，永远到不了
    208 行的 reverse 翻转。交叉验证：transition_mask_gpu.py:174-189 构建 iris/polygon
    spec 时也不透传 reverse，故当前系统行为一致、无用户可见 bug；但 uniform 声明
    （38 行）暗示对所有 kind 生效，未来任何调用方为这些 kind 设置 reverse 会得到
    无声的反向缺失。
  - 建议: 在 circle/ellipse/polygon 分支同样应用 `progress = 1.0 - progress`，或在
    uniform 声明处注释明确 reverse 仅作用于 wipe。
  - 置信度: 高

- [AUD-16902] [Suggestion] blur9 的 texel 参数从未被使用
  - 位置: src/features/edit/core/gpu_transition_shaders.py:58
  - 证据:
    ```glsl
    vec3 blur9(sampler2D tex, vec2 uv, vec2 texel, vec2 delta) {
        vec3 result = texture(tex, uv).rgb * 0.25;
        result += texture(tex, uv + delta).rgb * 0.125;
    ```
  - 影响: 函数体内只使用 `uv` 与 `delta`，`texel` 为死参数，误导后续维护者以为
    卷积核与 texel 尺寸相关。
  - 建议: 删除该参数（同步更新 74、240、242 行调用点），或改为用 texel 派生 delta。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：纯着色器源字符串模块，不创建/持有任何 ModernGL 对象
- GLSL 数字字面量：所有浮点字面量均带小数点/科学计数（0.5、1.0e-8 等），无隐式 int→float
- 循环上界编译期常量：radial_blur 固定 7、polygon_inside 固定 48（附有意图注释），动态 break 合规
- HDR 正确性：所有 clamp 均作用于 UV 坐标而非颜色；to_linear/from_linear 不截断 >1.0 值；无非法 clamp(0.0,1.0) 颜色截断
- blur9 权重和为 1.0，能量守恒
- 线程安全/信号防递归/状态提交：纯静态数据，无并发、无信号、无状态

---

## src/features/edit/core/interchange.py (行数: 426)

### 发现

- [AUD-16903] [Major] build_cue_sheet_from_sequence 对 Rational 帧率执行 `fps > 0` 比较抛 TypeError，首个音频 clip 即崩溃
  - 位置: src/features/edit/core/interchange.py:325
  - 证据:
    ```python
    fps = getattr(sequence, "fps", 24.0) or 24.0   # 行 304
    ...
    duration_seconds=duration / fps if fps > 0 else 0.0,   # 行 325
    ```
    时间轴真实类型：timeline_v2.py:2930 `fps: Rational = Rational(24, 1)`；
    Rational（src/shared/media/metadata.py:26）为 frozen dataclass，dunder 仅有
    `__float__/__mul__/__truediv__`，无 `__gt__` 等比较运算符；本模块自己的
    `_fps_float` docstring（53-54 行）也写明"Rational 只有 __float__，无比较运算"。
  - 影响: `TimelineSequence.fps` 默认就是 Rational（无 `__bool__`，304 行的
    `or 24.0` 不会兜底），循环里第一个音频 clip 就触发
    `TypeError: '>' not supported between instances of 'Rational' and 'int'`，
    cue sheet 导出功能整体不可用。模块标注 DORMANT（暂无生产入口），故为潜伏
    缺陷；一旦接线立即触发。
  - 建议: 该行改为 `duration_seconds=duration / _fps_float(fps) if _fps_float(fps) > 0 else 0.0`，与同文件其他路径一致。
  - 置信度: 高

### 通过项
- SMPTE drop-frame 正/逆变换抽样验算（00:01:00;02→1800、00:09:00;02→16184、00:10:00;00→17982）均正确，双射成立；非法 DF 帧号显式报错
- 输入验证：parse_timecode 校验字段数/非负/数值；timecode_to_frames 校验字段越界；负帧钳制 0 有注释说明
- 异常路径：to_csv 捕获后 log warning 并返回 False，不静默
- 状态提交/线程安全：纯计算与一次性 CSV 写出，无共享状态
- 性能：线性扫描，无热点

---

## src/features/edit/core/loudness_analyzer.py (行数: 186)

### 发现

- [AUD-16904] [Suggestion] analyze_loudness 未验证 samples 维度，1D 输入静默产出全 floor 报告
  - 位置: src/features/edit/core/loudness_analyzer.py:112
  - 证据:
    ```python
    meter = LoudnessMeter(sample_rate, channels=int(samples.shape[0]))
    ...
    for i in range(0, total, block_size):
        block = samples[:, i:i + block_size]
    ```
  - 影响: 传入 1D 单声道数组时 `shape[0]` 是样本总数（被当作声道数），切片得到
    1D block，`LoudnessMeter.process` 因 `samples.ndim != 2` 静默 return——
    integrated/LRA 全为 floor，仅 true_peak 仍正确；调用方拿到一份看似合法实则
    未测量的报告（backend_available 仍为 True，不受 RMA-01 保护）。
  - 建议: 函数入口校验 `samples.ndim == 2`，非法输入抛 ValueError 或显式 reshape。
  - 置信度: 高（机制确定；实际触发依赖调用方误传）

### 通过项
- 异常与降级路径：scipy 后端缺失时 backend_available=False + 警告 + 拒绝计算 normalize 增益（RMA-01 语义正确）
- true peak 近似有诚实注释（无 4x 过采样）；-inf 序列化处理正确（to_dict/to_text）
- 性能：分块处理避免内存峰值；块内 numpy 向量化
- 报告数据为 frozen dataclass，不可变，无状态提交问题
- 剪波预测警告（predicted_peak > 0 dBTP）逻辑正确

---

## src/features/edit/core/loudness_meter.py (行数: 324)

### 发现

- [AUD-16905] [Major] _update_integrated 每个 hop 全量重扫 _gated_blocks，实时音频路径上成本随会话时长无界线性增长
  - 位置: src/features/edit/core/loudness_meter.py:281
  - 证据:
    ```python
    mean_z_first = self._gated_sum / len(self._gated_blocks)
    relative_gate_z = self._lufs_to_z(self._z_to_lufs(mean_z_first) - 10.0)
    # 第二阶段：筛出高于相对门限的块
    second_stage = [z for z in self._gated_blocks if z > relative_gate_z]
    if not second_stage:
        self._integrated_lufs = -70.0
        return
    final_mean_z = sum(second_stage) / len(second_stage)
    ```
  - 影响: 每 100ms hop 执行一次 O(N) 列表推导；调用方是 audio_playback.py:2322
    实时混音路径（每轨一个 meter、每 mix 周期 process）。块数随播放时长线性累积：
    1 小时会话末尾每 hop 每轨约 3.6 万次比较（毫秒级尖峰），8 轨即每 100ms
    十余毫秒的额外音频线程开销，且无上界——长时间播放/混音会出现可闻的规律性
    负载尖峰甚至 dropout。本文件自身注释（124-128 行）记录过同类逐样本瓶颈
    （每 chunk 37ms），此路径是同性质回归。
  - 建议: 把第二阶段筛选从每 hop 移到 `integrated_lufs` property 读取时惰性计算
    （UI 只 ~10Hz 读取）；或维护按 z 排序的结构/增量统计近似门限，避免每 hop 全扫。
  - 置信度: 中（增长机制与调用路径确定；实际可闻影响取决于会话长度与音频缓冲）

### 通过项
- 线程安全：文档明确实例不共享（每 strip/总线一个）；无可变共享状态
- K-weighting 系数与 BS.1770-4 一致（48k/44.1k 双套，其他采样率最近邻近似有注释）；声道权重（Ls/Rs 减权、LFE 排除）符合标准，非标准声道数回退等权
- 环形缓冲 `_push_sq/_window_sum` 边界（超长块、回绕、空窗）验算正确；numpy 向量化替代逐样本 deque 的优化保持数值语义
- 后端不可用降级（AM-01）：process 静默 return + is_backend_available 属性供 UI 区分
- 类型注解完整；无 OpenGL、无信号、无持久化状态

---

## src/features/edit/core/mixer_preset.py (行数: 261)

### 发现

- [AUD-16906] [Major] load_presets 宽泛 except 返回空列表 + add/remove 无条件整文件覆写，单条损坏可永久清空整个用户预设库
  - 位置: src/features/edit/core/mixer_preset.py:171
  - 证据:
    ```python
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [ChannelStripPreset.from_dict(item) for item in data.get("presets", [])]
    except Exception:
        return []
    ...
    def add_preset(preset: ChannelStripPreset) -> list[ChannelStripPreset]:
        presets = load_presets()
        presets = [p for p in presets if p.preset_id != preset.preset_id]
        presets.append(preset)
        save_presets(presets)
    ```
    （save_presets 175-185 行直接 `open(path, "w")` 覆写，非临时文件+rename 原子写）
  - 影响: `presets` 数组里只要有一个元素不是 dict（如手工编辑、旧版本格式、
    json.load 之外的部分损坏——from_dict 内部异常已被吞，外层剩余触发点即
    AttributeError），load_presets 返回 []；用户随后新增/删除任一预设即把文件
    覆写为仅含 1 条，其余 N-1 条好数据永久丢失。save 的非原子写（崩溃/断电时
    截断文件）进一步提高进入该路径的概率。属用户数据丢失风险。
  - 建议: load 逐条解析、跳过坏条目并 log（而不是整文件放弃）；save 改为
    临时文件 + os.replace 原子替换；可选地覆写前对"从 N 条变为 1 条"做保护性备份。
  - 置信度: 高（机制确定；触发需要文件先出现局部损坏）

- [AUD-16907] [Suggestion] strip_eq/effect_chain/sends 反序列化静默吞掉所有异常且无日志，预设静默缺胳膊少腿
  - 位置: src/features/edit/core/mixer_preset.py:94
  - 证据:
    ```python
        if se_data:
            try:
                from src.features.edit.core.timeline_v2 import StripEQParams
                strip_eq = StripEQParams.from_dict(se_data)
            except Exception:
                strip_eq = None
    ```
    （effect_chain 98-103、sends 107-115 同型）
  - 影响: 版本演化导致 from_dict 抛错时，用户保存的含 EQ/FX/发送的预设被加载成
    不含这些参数的"成功"预设，应用后效果悄然丢失，无从排查（无任何日志）。
    违反"不得宽泛 except 掩盖领域错误"。
  - 建议: except 收窄到 (KeyError, TypeError, ValueError) 并 logger.warning 记录
    被丢弃的字段与原因。
  - 置信度: 高

### 通过项
- 状态提交一致性：预设文件为用户级独立 JSON，与 timeline/undo 体系无耦合；MixSnapshot 走 timeline 的 mixer_snapshots 字段持久化，to_dict/from_dict 对称
- 类型注解基本完整；frozen dataclass 不可变
- from_track/from_timeline 用 getattr 宽容提取，缺字段有默认值
- 无信号、无 OpenGL、无并发（文档明示纯数据类）

---

## src/features/edit/core/multicam.py (行数: 300)

### 发现

- [AUD-16908] [Suggestion] build_multicam_group_from_assets 不拒绝重复 asset_id，asset_id 键控的查表产生歧义
  - 位置: src/features/edit/core/multicam.py:198
  - 证据:
    ```python
    if len(asset_ids) < 2:
        raise ValueError("A multicam group needs at least 2 angle assets")
    ...
    angles = tuple(
        MulticamAngle(
            angle_id=str(uuid.uuid4()),
            asset_id=aid,
    ```
  - 影响: `__post_init__` 只校验 angle_id 唯一，同一素材被选两次（UI 多选去重
    失效时）会生成两个合法角度；而 `angle_by_asset_id`（253-258 行）与
    `group_with_sync_offsets`（261-272 行）都按 asset_id 查表/写回——切换高亮
    与同步偏移只会命中第一个角度，第二个角度永远拿不到自己的偏移。
  - 建议: 构建入口对 asset_ids 去重或对重复项抛 ValueError（与 <2 校验并列）。
  - 置信度: 高（代码事实确定；实际影响依赖 UI 是否可能传入重复）

### 通过项
- 输入验证：MulticamAngle/MulticamAngleGroup 的 __post_init__ 校验空 id、angle_id 唯一、default_angle_id 存在；multicam_group_from_asset 捕获 (TypeError, ValueError) 返回 None，坏 metadata 不炸
- 状态提交一致性：组定义经 stamp_multicam_group_on_assets 写进素材 metadata 随项目持久化；group_with_sync_offsets 为纯函数返回新组
- resolve_angle_source_frame 公式与 docstring 一致，结果钳制 [0, total_frames-1]，total<1 返回 0
- frozen dataclass 全不可变；to_dict/from_dict 对称；无线程/信号/GLSL 维度

---

## src/features/edit/core/multicam_sync.py (行数: 304)

### 发现

- [AUD-16909] [Suggestion] compute_sync_alignments 用全体信号的最小长度限制所有配对的相关窗口，一个短角度拖累全部精度
  - 位置: src/features/edit/core/multicam_sync.py:156
  - 证据:
    ```python
    max_lag = int(sample_rate * max_shift_seconds)
    n = min(s.size for s in signals.values())
    for asset_id, wave in signals.items():
        ...
        lag, confidence = best_lag_samples(ref[:n], wave[:n], max_lag)
    ```
  - 影响: 某角度只有 0.6s 可解码音频时，所有角度（含互相都有 60s 的）都被截到
    0.6s 做互相关，信噪比与置信度集体下降；而该短角度随后还可能被单独标记
    segment_too_short（>=0.5s 时不会），即"没被拒的短信号降低了别人的精度"。
  - 建议: `n = min(ref.size, wave.size)` 放到循环内按对计算。
  - 置信度: 高

- [AUD-16910] [Suggestion] worker 中断路径不发射任何信号，若被中断则 UI 永久停留"同步中"且按钮禁用
  - 位置: src/features/edit/core/multicam_sync.py:269
  - 证据:
    ```python
    for job in self._jobs:
        if self.isInterruptionRequested():
            return
    ```
  - 影响: 正常结束有 sync_finished/sync_failed 两条信号，控制器
    `_on_multicam_sync_done`（edit_controller.py:6580-6587）负责
    `set_sync_running(False)` 解锁面板；中断 return 两条都不发，面板按钮将一直
    显示"同步中…"并禁用。当前 src/ 无任何调用方对该 worker 调
    requestInterruption（已 grep 验证），故为潜伏陷阱而非现行 bug。
  - 建议: 中断 return 前发射一个轻量信号（如 sync_failed("interrupted")）或让
    控制器同时连接 worker.finished 做状态清理。
  - 置信度: 中（代码路径确定；"是否会有中断调用方"属未来推断）

### 通过项
- 线程安全：QThread worker 只在 run 内解码（AudioDecoder 类内零 Qt 调用有架构注释支撑），信号携带纯 dict 负载跨线程安全
- 异常路径：_decode_mono_segment 后台兜底 except 有 noqa 说明、log warning 并把错误字符串回传结果；finally 关闭 decoder 且释放失败不掩盖结果
- DSP 正确性：correlate/correlation_lags 的滞后符号约定与 docstring（k*>0=other 更晚、需正偏移）及模块头部的 sync_offset 语义一致；normalize_signal 对近静音（std<1e-9）回退原信号；decimate_mean 边界（usable==0）处理正确
- 输入验证：参考缺失/过短、各角度过短均产出带 error 的显式结果而非异常
- 性能：48k→8k 降采样（factor 6）+ FFT 相关，量级合理；搜索窗 |lag|≤max_lag 掩码正确（-np.inf 填充）

---

## src/features/edit/core/nested_sequence.py (行数: 657)

### 发现

- [AUD-16911] [Major] build_nested_from_selection 在宿主轨道 span 内存在未选片段时因 with_clip_added 重叠校验抛 ValueError，合法多轨/非连续选择失败且报错为内部 UUID
  - 位置: src/features/edit/core/nested_sequence.py:444
  - 证据:
    ```python
    nested_clip = ClipInstance(
        ...
        track_id=host_track_id,
        timeline_start=min_start,
        timeline_duration=span,      # span = max_end - min_start，覆盖整个选择
    ...
        if track.track_id == host_track_id:
            t = t.with_clip_added(nested_clip)
    ```
    EditTrack.with_clip_added（timeline_v2.py:2430-2434）对任何时间重叠直接
    `raise ValueError(f"Clip overlaps with {existing.clip_id}")`。
  - 影响: 嵌套片段占据 [min_start, max_end) 的整个跨度；宿主轨道（最早选中
    视频片段的轨道）上任何未选片段落在该跨度内（典型：同轨非连续选择 A、C
    而中间的 B 未选；或多轨选择时宿主轨的背景片段未选）即触发异常。控制器
    （edit_controller.py:6774-6777）捕获后仅把 "Clip overlaps with <uuid>" 显示
    到状态栏——用户视角是功能随机失败加一条不可读报错。docstring 未声明
    "选择必须连续/宿主轨道必须干净"的前置条件。
  - 建议: 构建前显式检测该前置条件并抛带用户语义的错误（或允许嵌套片段与
    未选片段共存的分层放置策略）；至少在 docstring 与 UI 层明确该限制。
  - 置信度: 中（失败机制与触发场景确定；"仅支持连续选择"若属有意设计则降级为
    文档缺失）

- [AUD-16912] [Suggestion] evaluate_nested_frame 的环检测依赖调用方穿透私有 _render_stack，与注释/签名矛盾；生产路径未使用该函数
  - 位置: src/features/edit/core/nested_sequence.py:181
  - 证据:
    ```python
    # The composite_evaluator may itself encounter nested-sequence layers; it
    # is responsible for threading the recursion guard through.  For the common
    # case we pass a closure that re-enters evaluate_nested_frame.
    return composite_evaluator(plan, source_frame)
    ```
  - 影响: `composite_evaluator: Callable[[Any, int], ...]` 签名无法携带 stack，
    167 行新建的 `stack = stack | {child_tid}` 从未传给 evaluator——注释声称
    "we pass a closure" 但实际什么都没传。按 docstring 正常调用的使用者（evaluator
    闭包不自备 _render_stack 透传）在循环嵌套（A 含 B、B 含 A）下会无限递归
    RecursionError。已验证：src/ 无生产调用方（预览/导出各自维护
    `_nested_render_stack`，timeline_preview_renderer.py:3425、
    timeline_export.py:9408），仅测试通过手动透传 _render_stack 覆盖。
  - 建议: 要么把 stack 作为 composite_evaluator 的第三个参数传递，要么在
    docstring 中以显眼的警告说明"环安全完全依赖 evaluator 闭包自行透传
    _render_stack"；长期看可让生产渲染器复用此函数消除三套守卫。
  - 置信度: 高（代码事实与调用方分布已核实；因无生产调用方，现行为无用户影响）

- [AUD-16913] [Suggestion] 选择边界的转场（单端选中）在父时间线被静默删除，效果无提示丢失
  - 位置: src/features/edit/core/nested_sequence.py:437
  - 证据:
    ```python
    for cid in removed_here:
        t = t.with_clip_removed(cid)
    ```
    （子序列侧规则为"两端都在选择内才搬运"，374-380 行；EditTrack.with_clip_removed
    会删除引用该片段的转场，timeline_v2.py:2437-2440）
  - 影响: 连接"被选片段—未选片段"的转场既不搬进子序列（单端），也在父时间线
    随片段移除而被清理——用户嵌套选择后边界处的溶解/擦除无声消失。
  - 建议: 至少在事务 docstring 与 UI 确认对话框中说明边界转场会被丢弃；或把
    单端转场改写为嵌套片段与相邻未选片段之间的新转场。
  - 置信度: 中（行为由模型不变量合理推导而来，但"丢弃"是否为设计决策未注明）

- [AUD-16914] [Suggestion] 直接调用 TimelineSequence 私有方法 _rebuild
  - 位置: src/features/edit/core/nested_sequence.py:452
  - 证据:
    ```python
    if moved_marker_ids:
        new_timeline = new_timeline._rebuild(markers=tuple(
            m for m in new_timeline.markers if m.marker_id not in moved_marker_ids
        ))
    ```
  - 影响: 跨层依赖私有 API，TimelineSequence 重构时此处会静默破裂（AttributeError）。
  - 建议: 在 TimelineSequence 上提供公开的 `with_markers_updated` 之类的替换方法。
  - 置信度: 高

### 通过项
- 环安全（创建路径）：would_create_cycle/collect_contained_timeline_ids 的 visited 集合递归正确；控制器创建前已调用检查（含直接自引用防御）
- flatten_nested_audio_tracks：path_ids 环检测、_NESTED_AUDIO_MAX_DEPTH=8 深度限制、root_id 入初始路径、速度≠1x 实例跳过（effective_speed 为方法已核实 timeline_v2.py:343）、虚拟轨命名空间 `nest:<clip_id>:<track_id>` 隔离实例、bus/sends 重映射逻辑自洽；孙辈坐标经 _map_clip_into_window 级联平移验算正确
- _map_clip_into_window 的窗口钳制（start_trim/end_trim、new_duration<1 返回 None）边界正确
- 纯函数纪律：build_nested_from_selection 不触碰 project/controller 状态，产物为可整体提交的事务对象
- 类型注解完整；自动化关键帧平移（_shift_automation）不改原轨、负帧钳 0 有注释
- 无 OpenGL/无 UI 信号；子序列内容平移归零（timeline_start - min_start）与标记平移对称

---

## 审计覆盖说明

- 九个维度（线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性 / 异常与取消 / 输入验证 / 性能 / 类型注解）对 8 个文件逐项过检；类型注解缺失仅一处值得记录的宽泛 `tuple`/`Optional[Any]`（mixer_preset，已有避免循环导入的注释说明，未单列）。
- 未发现 Critical 级问题：无崩溃于生产路径的缺陷（AUD-16903 位于 DORMANT 模块）、无渲染管线数据损坏、无 OpenGL 线程违规。
