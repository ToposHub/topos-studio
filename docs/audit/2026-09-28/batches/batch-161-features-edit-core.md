# 批次报告：batch-161（src/features/edit/core）

- 审查日期：2026-09-28
- 编号区间：AUD-16100–AUD-16199（实际使用 AUD-16100–AUD-16112）
- 审查文件：channel_layout.py / chapter_export.py / clip_params.py（3 个，1,548 行，全部完整 Read）
- 汇总：Critical 0 / Major 4 / Suggestion 9

---

## src/features/edit/core/channel_layout.py (行数: 386)

### 发现

- [AUD-16100] [Major] bs1770_channel_weights 环绕声道权重 0.59/0.41 与 BS.1770-4 规定的 1.41 不符且左右不对称
  - 位置: src/features/edit/core/channel_layout.py:380-385（模块头 docstring 第 10 行同错）
  - 证据:
    ```python
    if layout == ChannelLayout.SURROUND_5_1:
        # [L, R, C, LFE, Ls, Rs]
        return np.array([1.0, 1.0, 1.0, 0.0, 0.59, 0.41], dtype=np.float64)
    if layout == ChannelLayout.SURROUND_7_1:
        # [L, R, C, LFE, Ls, Rs, Lss, Rss]
        return np.array([1.0, 1.0, 1.0, 0.0, 0.59, 0.41, 0.59, 0.41], dtype=np.float64)
    ```
  - 影响: loudness_meter.py:225-228 用该权重计算 integrated loudness。BS.1770-4 规定 5.1/7.1 各环绕声道（Ls/Rs/侧环绕）权重为 1.41（约 +3 dB 补偿），且对称；本实现 Ls=0.59（-4.6 dB）、Rs=0.41（-7.7 dB），还引入了规范中不存在的左右不对称。环绕声占比较重的素材响度被系统性低估且偏向左环绕，导致响度归一化（LUFS 目标）结果错误。
  - 建议: 改为 `np.array([1.0, 1.0, 1.0, 0.0, 1.41, 1.41])`（7.1 相应追加 1.41），同步修正模块头 docstring；如 0.59/0.41 另有出处，应在注释中给出依据并更名，不得冠以 BS.1770-4 之名。
  - 置信度: 高

- [AUD-16101] [Major] downmix() 结果直接 astype 回整型源 dtype，无削波导致 int16/int32 PCM 溢出回绕
  - 位置: src/features/edit/core/channel_layout.py:252-255
  - 证据:
    ```python
    matrix = downmix_matrix(src, dst, include_lfe=include_lfe)
    # output = M @ input → (dst_channels, n)
    output = matrix @ samples.astype(np.float64)
    return output.astype(samples.dtype if samples.dtype != np.float16 else np.float32)
    ```
  - 影响: 5.1→stereo 的 Lo = L + 0.707·C + 0.707·Ls，多个满幅声道相加可超过整型满量程（如 int16 的 32767）；numpy `astype(int16)` 对越界值做模 2^16 回绕而非饱和，导出音频出现爆音/哒声。float16 已被特判转 float32，整型却未做 clip。当前管线回退路径（timeline_export.py:4126 静音填充）为 float32，但 decoder 直供 int16 PCM 时即触发。
  - 建议: 整型 dtype 时先 `np.clip` 到该 dtype 的 [min, max] 再 cast，或统一升 float32 由下游负责量化；至少在 docstring 声明仅接受浮点输入并在整型输入时抛错。
  - 置信度: 中（回绕行为确定；是否触发取决于 decoder 输出 dtype）

- [AUD-16102] [Suggestion] channel_labels 返回裸 tuple，注解缺参数化类型
  - 位置: src/features/edit/core/channel_layout.py:72-74
  - 证据:
    ```python
    @property
    def channel_labels(self) -> tuple:
        """该布局的声道标签顺序（ITUR/ATSC 标准 WAV 顺序）。"""
        return {
    ```
  - 影响: 类型检查器无法推断元素类型，下游消费声道标签时失去静态保障；与全仓"完整类型注解"约束不符。
  - 建议: 改为 `-> tuple[str, ...]`。
  - 置信度: 高

### 通过项
- 线程安全：纯函数 + 无共享可变状态，无问题
- GPU 资源生命周期：无 GPU/ModernGL 资源
- GLSL·HDR：音频域模块，无着色器代码；float16→float32 特判正确
- 信号防递归：无 Qt 依赖
- 状态提交一致性：无状态模块，无 undo/revision 牵涉
- 异常与取消路径：未知布局显式 ValueError（拒绝而非静默截断），符合模块声明
- 输入验证：downmix 声道数不匹配显式报错；from_channel_count/from_av_layout_name 拒绝未知值
- 性能模式：矩阵一次构建、向量化 matmul，无逐样本 Python 循环
- adapt_samples_for_delivery 回退路径（截断/复制）均留 warning 日志且 broadcast_to 产物转可写数组，符合 P1-09 验收

---

## src/features/edit/core/chapter_export.py (行数: 128)

### 发现

- [AUD-16110] [Major] format_youtube_chapters 的 auto_first 判定用 int(fps) 截断，NTSC 分数帧率下产生两条 0:00 时间戳
  - 位置: src/features/edit/core/chapter_export.py:69-73（配合 :43 的时间码计算）
  - 证据:
    ```python
    # 首章若本就落在 0:00（帧 < fps，时间码同为 "0:00"），不能再补
    # "0:00 Chapters"——两条 0:00 时间戳会让 YouTube 拒绝整份章节。
    if auto_first and chapters[0].frame != 0 and (
            not chapters[0].frame < int(fps if fps > 0 else 24.0)):
        lines.append("0:00 Chapters")
    ```
  - 影响: 首章是否渲染为 "0:00:00" 由 `frame / fps < 1.0` 决定（:43 `total_seconds = max(0, int(frame)) / float(fps)`），而判定补行却用 `frame < int(fps)`。NTSC 分数帧率下 int(fps) < fps，两者脱节：如 fps=29.97、首章 frame=29 时时间码仍为 0.968s → "0:00:00"，但 29 < 29 为 False → 照样补 "0:00 Chapters"，输出两条 0:00 开头的行——恰是注释声称要避免、会让 YouTube 拒绝整份章节的情况。23.976/29.97/59.94 素材首章落在第一秒末尾的 1 帧窗口即触发。
  - 建议: 判定与渲染同源：用 `format_timestamp_hms(chapters[0].frame, fps) == "0:00:00"`（或 `chapters[0].frame < fps` 不取整）决定是否补行。
  - 置信度: 高

- [AUD-16111] [Suggestion] 模块 docstring 称 FFMetadata 使用 TIMEBASE=1/fps 帧号直存，实现为 TIMEBASE=1/1000 毫秒
  - 位置: src/features/edit/core/chapter_export.py:9-11（对照 :111-114 实现、:88-91 函数 docstring）
  - 证据:
    ```python
    out.append("[CHAPTER]")
    out.append("TIMEBASE=1/1000")
    out.append(f"START={int(round(start * 1000.0 / fps))}")
    out.append(f"END={max(int(round(end * 1000.0 / fps)), int(round(start * 1000.0 / fps)) + 1)}")
    ```
  - 影响: 模块头描述与实现矛盾（函数级 docstring 是对的）；按头注释理解的维护者会在时间基换算上做出错误假设。
  - 建议: 更新模块头 docstring 为 TIMEBASE=1/1000、毫秒语义。
  - 置信度: 高

- [AUD-16112] [Suggestion] docstring 声称 YouTube 要求至少 3 条章节，但 1–2 条时静默输出无效章节列表
  - 位置: src/features/edit/core/chapter_export.py:6-8（对照 :65-67）
  - 证据:
    ```python
    chapters = chapter_markers(markers)
    if not chapters:
        return ""
    ```
  - 影响: 模块自述"YouTube 要求……至少 3 条"，但只有 1–2 条章节时仍原样输出，用户贴到 YouTube 会被整份忽略，且无任何日志/提示，难以排查。
  - 建议: 章节数 < 3 时输出 logger.warning（或返回空串并在 docstring 说明），让交付侧可感知。
  - 置信度: 中

### 通过项
- 线程安全：纯函数、无 Qt/无状态
- GPU 资源生命周期：无 GPU 资源
- 异常与取消路径：fps <= 0 在两个格式化函数中一致回退 24.0；空标记返回空串
- 输入验证：负帧经 max(0, ...) 钳制；chapter_markers 筛选 kind/clip_id 属性缺失时 getattr 默认值兜底
- 性能模式：单次排序 + 线性拼接，无问题
- FFMetadata 转义完整（\ = ; # 及换行），END > START 钳制正确（含毫秒粒度 +1）
- 类型注解完整

---

## src/features/edit/core/clip_params.py (行数: 1034)

### 发现

- [AUD-16103] [Major] NaN 输入绕过全部基于比较的范围校验，from_dict 未过滤非有限值
  - 位置: src/features/edit/core/clip_params.py:97-108 / 243-265 / 456-470 / 996-1009（入口 :211-219、:948-957、:1024-1034）
  - 证据:
    ```python
    if self.scale_x < 0.0 or self.scale_y < 0.0:
        raise ValueError(
            f"Scale cannot be negative: scale_x={self.scale_x}, scale_y={self.scale_y}"
        )
    if self.scale_x > 5.0 or self.scale_y > 5.0:
    ```
    ```python
    if self.speed < 0.1 or self.speed > 8.0:
        raise ValueError(f"speed must be between 0.1 and 8.0: {self.speed}")
    ```
  - 影响: 所有范围校验均为 `<`/`>` 比较，NaN 与任何数比较均为 False，四个 frozen 参数类（Transform/Crop/Speed/RawParams）遇到 NaN 一律放行。`from_dict` 直接 `float(data.get(...))` 不做 isfinite 检查，而 Python `json.loads` 默认接受 `NaN` 字面量——被污染/手改的工程文件会把 NaN 注入 scale/crop/speed，进而在渲染中产生 NaN 矩阵/NaN 时长（如 `1.0/NaN`），表现为黑帧、片段消失或布局错乱，且难以溯源。违反"外部数据须验证"铁律。
  - 建议: 在各类 `__post_init__`（或统一在 from_dict 后）用 `math.isfinite` 校验所有 float 字段，非有限值显式报错。
  - 置信度: 中（绕过机制确定；触发依赖工程文件含 NaN）

- [AUD-16104] [Suggestion] CropParams/SpeedParams.from_dict 缺 float()/bool() 强转，SpeedPoint.from_dict 直接下标取必填键
  - 位置: src/features/edit/core/clip_params.py:384-390 / 948-957 / 426-431
  - 证据:
    ```python
    return CropParams(
        left=data.get("left", 0.0),
        right=data.get("right", 0.0),
    ```
    ```python
    return SpeedParams(
        speed=data.get("speed", 1.0),
        reverse=data.get("reverse", False),
    ```
    ```python
    return SpeedPoint(
        source_offset=int(data["source_offset"]),
    ```
  - 影响: TransformParams/RawParams.from_dict 均逐字段 `float(...)` 强转，CropParams/SpeedParams 却原样透传：畸形工程文件中字符串数值（JSON 层面合法）会在 `__post_init__` 的数值比较处抛 TypeError、或 `reverse` 以字符串 "false" 传入被当作真值；SpeedPoint.from_dict 对缺键抛裸 KeyError，错误信息不带字段上下文。同类外部数据入口行为不一致。
  - 建议: 与 TransformParams.from_dict 对齐，逐字段显式强转（float/int/bool/str），SpeedPoint 改用带默认值或包装出清晰错误信息。
  - 置信度: 高

- [AUD-16105] [Suggestion] smooth 斜坡查表每次调用全量重建，且单次帧映射内重复构建两遍
  - 位置: src/features/edit/core/clip_params.py:714-719（另 :670-678、:772-779 调用；:585-589 构建）
  - 证据:
    ```python
    if self._has_smooth_ramp(source_duration):
        ns, nt, _total = self._ramp_lookup_tables(source_duration)
        tl_frames = self.timeline_duration_for_source_duration(source_duration)
        remaining = max(0, int(timeline_frame))
        pos = min(max(remaining / max(tl_frames, 1), 0.0), 1.0)
    ```
    ```python
    if samples <= 0:
        samples = max(2, source_duration * 4 + 1)
    xs = np.linspace(0.0, float(source_duration), samples)
    ```
  - 影响: `timeline_to_source_frame_pair` 每帧调用一次；斜坡路径每次都 linspace 采样 4N+1 点并 cumsum，随后 `timeline_duration_for_source_duration` 内部又把同一张表再算一遍。长素材（N=1 万帧）逐帧渲染/导出为 O(N²) 级别的冗余计算，与 4K 实时基准相悖。
  - 建议: SpeedParams 为 frozen 且可哈希，可用 `functools.lru_cache` 按 (self, source_duration) 缓存查表；或让调用方一次构建映射表复用整个片段。
  - 置信度: 高（重复计算确定；量级随素材长度增长）

- [AUD-16106] [Suggestion] timeline_to_source_frame_pair 在 timeline_frame == 总时长时返回 (0, 1, 0.0)，与 timeline_to_source_frames 的末帧钳制不一致
  - 位置: src/features/edit/core/clip_params.py:720-731
  - 证据:
    ```python
    exact = 0.0
    remaining = max(0, int(timeline_frame))
    for start, end, segment_speed in self._segments(source_duration):
        timeline_length = max(1, round((end - start) / segment_speed))
        if remaining < timeline_length:
            exact = start + (remaining * segment_speed)
            break
        remaining -= timeline_length
    else:
        if remaining > 0:
            exact = source_duration - 1
    ```
  - 影响: timeline_frame 恰等于片段总时长（越界 1 帧，转场/末帧边界常见）时，remaining 归零走 for-else 且不进 `remaining > 0` 分支，exact 保持初值 0.0 → 返回源帧 0；同函数族 `timeline_to_source_frames`（:665 初始化 `source_frame = source_duration - 1`）对该输入返回末帧。当前两处调用方（timeline_frame_mapper.py:371、timeline_export.py:7559）恰好都有 `weight <= 0` 早退保护，未实际出错，但语义矛盾是隐患。
  - 建议: for-else 分支去掉 `remaining > 0` 条件或将初值改为 `source_duration - 1`，与 timeline_to_source_frames 对齐。
  - 置信度: 高（行为确定；当前影响被调用方防护掩盖）

- [AUD-16107] [Suggestion] is_normal_speed / effective_duration_factor 忽略 speed_points 且全仓无调用方
  - 位置: src/features/edit/core/clip_params.py:474-482 / 504-515
  - 证据:
    ```python
    @property
    def is_normal_speed(self) -> bool:
        ...
        return self.speed == 1.0 and not self.reverse
    ```
    ```python
    @property
    def effective_duration_factor(self) -> float:
        ...
        return 1.0 / self.speed
    ```
  - 影响: 二者只看基础 speed，带速度点的变速片段会被 is_normal_speed 判为"正常速度"、effective_duration_factor 给出与 `timeline_duration_for_source_duration` 不一致的时长。src/ 与 tests/ 均无调用（grep 核实），属于 A3 速度点功能之前的遗留语义，一旦被新代码复用将直接造成视音频时长失配。
  - 建议: 删除这两个属性，或让它们兼容速度点（is_normal_speed 加 `not self.speed_points`；duration 走 timeline_duration_for_source_duration 语义并在 docstring 标注需要 source_duration）。
  - 置信度: 高

- [AUD-16108] [Suggestion] RawParams.black_level 完全无范围与有限性校验
  - 位置: src/features/edit/core/clip_params.py:984（校验体 :996-1009 未覆盖）
  - 证据:
    ```python
    def __post_init__(self) -> None:
        for name in ("wb_gain_r", "wb_gain_g", "wb_gain_b"):
            v = getattr(self, name)
            if not (self.MIN_GAIN <= v <= self.MAX_GAIN):
    ```
  - 影响: wb_gain/exposure_stops/gamma/color_space 均有校验，唯独 black_level（码值域，10-bit 通常 0–1023）任意 float 均可通过，负值或超量程值会经 debayer 前减除直接污染 RAW 解码结果，且从工程文件加载时无任何拦截。
  - 建议: 增加 [0.0, 1023.0]（或按位深推导）范围 + isfinite 校验。
  - 置信度: 高

- [AUD-16109] [Suggestion] 注解引用 "np.ndarray" 但 numpy 仅在函数体内局部导入，get_type_hints 会 NameError
  - 位置: src/features/edit/core/clip_params.py:571-573 / 620-622（`import numpy as np` 在 :585、:630 等函数体内）
  - 证据:
    ```python
    def _ramp_source_curve(
        self, source_duration: int, samples: int = 0,
    ) -> tuple["np.ndarray", "np.ndarray"]:
    ```
  - 影响: `from __future__ import annotations` 下注解为惰性字符串，模块级无 `np` 名字；任何 `typing.get_type_hints()`（部分序列化/文档工具会调用）在这些方法上抛 `NameError: name 'np' is not defined`。
  - 建议: 模块顶部 `import numpy as np` 并加 `if TYPE_CHECKING` 保护（或改用 `Any`/自定义别名），函数内局部导入可保留为运行时优化。
  - 置信度: 高

### 通过项
- 线程安全：全模块 frozen dataclass + tuple，不可变共享安全（设计原则落实）
- GPU 资源生命周期：无 GPU 资源
- GLSL·HDR 正确性：ClipBlendMode.code 编号与定义顺序一致（0–15，注释核对无误），文档明确与转场 BlendMode 语义区分；RawParams 明确线性域增益与 OOTF 仅查看语义
- 信号防递归：无 Qt 依赖
- 状态提交一致性：to_dict/from_dict 键集合对称（含 preserve_pitch/speed_points/pivot 等新字段），legacy 归一化数据迁移路径明确
- 异常与取消路径：_segments 对越界遗留速度点容错（保工程可打开）并文档化；with_speed_point 幂等（已存在即返回 self）
- 输入验证：scale/crop/速度范围、pivot_mode、easing、interpolation、gamma、color_space、速度点严格排序唯一均有校验（NaN 见 AUD-16103）
- 性能模式（除 AUD-16105 外）：非斜坡路径为解析式 O(段数) 计算，无重分配
