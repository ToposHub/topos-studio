# 批次报告：batch-358 — src/shared/media/（P2）

任务区间：AUD-35800–AUD-35899。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。
全部 5 个文件已通读（无跳读）；跨模块 API（Rational/VideoMetadata/PlaneInfo/DecodedFrame/MediaSourceError/ToposImageCodec/ToposMovieFile/topos_meta/topos_profiles/color_*）已逐一核对签名与实现。

汇总：Critical 0 · Major 6 · Suggestion 9（共 15 条）。

---

## src/shared/media/timecode.py (行数: 527)

### 发现

- [AUD-35800] [Major] `__eq__` 忽略 tc_type 而 `__hash__` 包含 tc_type，违反哈希/相等契约
  - 位置: src/shared/media/timecode.py:471-482
  - 证据:
    ```python
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Timecode):
            return NotImplemented
        return (
            self.to_frame_number() == other.to_frame_number()
            and self.fps == other.fps
        )

    def __hash__(self) -> int:
        return hash((self.to_frame_number(), self.fps, self.tc_type))
    ```
  - 影响: 同帧号同帧率的 NDF 与 DF 实例（如 `Timecode(0,0,59,29,fps,NDF)` 与 `Timecode(0,0,59,29,fps,DF)`，两者 to_frame_number() 均为 1799）`==` 为 True 但哈希不同：相等对象可同时存在于 set/dict 中，去重与查找行为不确定（如用 Timecode 做字典键或做集合比较时出现"相等却不命中"）。
  - 建议: 二选一对齐——`__eq__` 纳入 tc_type（推荐，语义更严格），或 `__hash__` 去掉 tc_type。
  - 置信度: 高

- [AUD-35801] [Suggestion] DF 类型未与帧率交叉验证，非丢帧帧率可构造 DF 时间码并输出 ';' 分隔符
  - 位置: src/shared/media/timecode.py:114-123
  - 证据:
    ```python
    # DF 模式验证：分钟开始的 0,1 帧无效（29.97）或 0-3 帧（59.94）
    if self.tc_type == TimecodeType.DROP_FRAME and self.seconds == 0:
        if self.minutes % 10 != 0:  # 非整点十分钟
            if fps_int == 30 and self.frames < 2:
    ```
    （`__post_init__` 全程未调用 `is_drop_frame_fps`；`from_string` 对任何 fps 都接受 ';' 格式）
  - 影响: 25/24fps 等非丢帧帧率的 DF 时间码被静默接受，`to_string()` 输出 `HH:MM:SS;FF`——本模块内数值自洽，但外部 SMPTE 工具会把 ';' 解析为丢帧语义（25fps DF 未定义），造成互操作错读。
  - 建议: `__post_init__` 中当 `tc_type == DROP_FRAME` 且 `not is_drop_frame_fps(self.fps)` 时显式报错（或在文档中冻结该宽容策略）。
  - 置信度: 高

- [AUD-35802] [Suggestion] NDF 帧号超过 24 小时抛 ValueError，而 DF 路径静默按天取模回绕，行为不对称
  - 位置: src/shared/media/timecode.py:219-231 与 247
  - 证据:
    ```python
    if tc_type == TimecodeType.NON_DROP_FRAME:
        # NDF：简单计算
        frames_per_hour = frames_per_minute * 60
        hours = frame // frames_per_hour          # 可 >23 → __post_init__ 抛错
        ...
    # 处理超过一天的情况
    frame = frame % (24 * fps_int * 3600 - drop_per_day)   # DF：静默回绕
    ```
  - 影响: `from_frame_number(24*3600*fps, fps, NDF)` 抛 "Invalid hours: 24"，同量级 DF 帧号则静默回绕到次日——调用方难以预期；`add_frames` 跨天边界时同样一中一绕。
  - 建议: 统一策略（都回绕或都显式报错），并在 docstring 冻结。
  - 置信度: 高

- [AUD-35803] [Suggestion] `from_frame_number` DF 路径中 `remaining` 赋值后从未使用（死代码）
  - 位置: src/shared/media/timecode.py:251
  - 证据:
    ```python
    hours = frame // (fps_int * 3600 - drop_per_hour)
    remaining = frame % (fps_int * 3600 - drop_per_hour)

    # 计算分钟
    if hours > 0:
        frame_adj = frame - hours * (fps_int * 3600 - drop_per_hour)
    ```
  - 影响: 无功能影响，但 `remaining` 与 `frame_adj` 重复计算同一值，误导读者以为后续会使用 `remaining`。
  - 建议: 删除 `remaining` 或用它替代 `frame_adj` 的重算。
  - 置信度: 高

### 通过项
- 线程安全：frozen 数据类，无可变状态。
- DF/NDF 算法数值复核：29.97 与 59.94 的 from_frame_number/to_frame_number 在分钟界、十分钟界、整点界（00:01:00;02、00:10:00;00、01:00:00;00↔107892）均正确往返。
- GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交：不涉及。
- 异常路径：构造与运算错误均为显式 ValueError，无宽泛 except。
- 输入验证：hours/minutes/seconds/frames 范围、负帧号、from_string 段数与非法数字均有校验。
- 类型注解：完整（`__eq__` 返回 NotImplemented 的注解小瑕疵不构成发现）。

---

## src/shared/media/timeline.py (行数: 815)

### 发现

- [AUD-35804] [Major] `Timeline.with_track_updated` 对不存在的 track_id 静默无操作，更新被丢弃
  - 位置: src/shared/media/timeline.py:732-735
  - 证据:
    ```python
    new_tracks = tuple(
        track if t.track_id == track.track_id else t
        for t in self.tracks
    )
    ```
  - 影响: track_id 不匹配时生成的新时间线与原时间线完全相同且不报错——编辑层的更新被静默吞掉（对比同类方法 `with_clip_added`/`with_clip_removed` 均先 `get_track_by_id` 并 `raise ValueError(f"Track not found: ...")`）；在 undo/redo 重放场景下，过期 track_id 的命令会表现为"执行成功但无变化"，难以诊断。
  - 建议: 对齐其余编辑方法——track 不存在时抛 ValueError（或至少在 docstring 声明该行为并让调用方校验）。
  - 置信度: 高

- [AUD-35805] [Major] NaN 的 speed/opacity 可通过 `__post_init__` 验证，后续帧映射/渲染再崩溃
  - 位置: src/shared/media/timeline.py:89-92、377-378
  - 证据:
    ```python
    # 速度验证
    if self.speed < MIN_SPEED or self.speed > MAX_SPEED:
        raise ValueError(...)
    ...
    if self.opacity < 0.0 or self.opacity > 1.0:
        raise ValueError(f"opacity must be between 0.0 and 1.0, got {self.opacity}")
    ```
  - 影响: `float('nan') < 0.1` 与 `nan > 4.0` 均为 False，NaN 一路通过校验进入模型；此后 `int(relative_frame * nan)` 抛 "cannot convert float NaN to integer"、混色层拿到 NaN opacity 产生全 NaN 帧等，错误发生在远离污染源的渲染阶段。持久化文件损坏或外部脚本构造时可直接触发（`with_speed(float('nan'))` 同样绕过）。
  - 建议: 用 `math.isnan`/`not (MIN_SPEED <= self.speed <= MAX_SPEED)` 形式重写范围检查（opacity 同理）。
  - 置信度: 高

- [AUD-35806] [Major] 速度帧映射的浮点除法截断在 0.3 等步长上产生 off-by-one
  - 位置: src/shared/media/timeline.py:212-215（另见 173、135、244、279 的同型 `int(x / speed)`）
  - 证据:
    ```python
    # 考虑速度转换到时间线帧
    timeline_relative = int(source_relative / self.speed)
    ```
  - 影响: `speed=0.3`（合法，位于 [0.1, 4.0]）时 `3 / 0.3 == 9.999999999999998`，`int()` 截断为 9（精确值 10）——源帧 3 被映射到错误的时间线帧，`source_to_timeline_frame` 与 `timeline_to_source_frame` 的往返在边界帧上错一帧；帧精确编辑（trim、打点、tracking，见 tests/shared/tracking/test_tracking_frame_mapping.py 消费方）在特定速度下系统性偏移。
  - 建议: 改用精确有理运算（交叉相乘比较或 Fraction）或 `round()` 前先定界，避免对二进制浮点商做 floor。
  - 置信度: 中（floor 语义可能是有意设计，但 9.999…→9 的表示误差显然非预期）

- [AUD-35807] [Suggestion] `with_speed`/`with_trim` 在高速短素材上生成零长剪辑，报错信息误导
  - 位置: src/shared/media/timeline.py:243-245
  - 证据:
    ```python
    # 重新计算时间线结束帧
    new_duration = int(self.source_duration / speed)
    new_timeline_end = self.timeline_start + new_duration
    ```
  - 影响: source_duration=3、speed=4.0 时 `int(0.75)==0` → timeline_end == timeline_start → `__post_init__` 抛 "timeline_start must be less than timeline_end"，用户看到的是范围错误而非"素材太短无法 4 倍速"；`with_trim`（行 278-280）同型。
  - 建议: 在工厂方法内预检 `new_duration < 1` 并抛带速度/时长上下文的专用错误。
  - 置信度: 高

### 通过项
- 线程安全：三层模型均 frozen + tuple 容器，无共享可变状态。
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及（纯数据模型）。
- 状态提交一致性：immutable with_* 变换链适配 undo/redo 快照模式（无内部状态提交）。
- 输入验证（除上述 NaN/空窗项）：负帧、起止倒置、source_end 越界 total_frames、track_type 白名单、轨道内与跨剪辑重叠检测均正确。
- 性能模式：重叠校验 O(n log n)，查询线性可接受。
- 类型注解：完整。

---

## src/shared/media/topos_frame_extract.py (行数: 219)

### 发现

- [AUD-35808] [Suggestion] `_image_profile_for_movie` / `_detect_interframe` 的 `summary` 参数无类型注解
  - 位置: src/shared/media/topos_frame_extract.py:41、87
  - 证据:
    ```python
    def _image_profile_for_movie(summary) -> int:
    ...
    def _detect_interframe(self, summary) -> bool:
    ```
  - 影响: summary 实际为 `ToposMovieSummary`（topos_binding.py:2021 返回类型），缺注解降低可读性并与本批其余文件的完整注解风格不一致。
  - 建议: 补 `summary: ToposMovieSummary`（已在该模块导入路径可达）。
  - 置信度: 高

### 通过项
- 异常与取消路径：构造失败 `except Exception: self.close(); raise` 清理后重抛；close() 幂等；帧间源 P 包预检显式拒绝（TC_ERR_STATE 映射为可诊断 ToposCodecError），拒绝时不落文件——无静默数据丢失。
- 输入验证：frame_index 经 `operator.index` + 范围检查；`.toos` 后缀强制；`overwrite=False` 时 FileExistsError。
- 资源生命周期：ToposMovieFile 句柄由 close()/上下文管理器统一释放；批量路径复用句柄与 packet 缓冲。
- 状态提交一致性：输出走 binding 的原子 temp+rename + 可选 fsync（durable）。
- 性能模式：免转码单包封装；帧间源仅 I 帧预检一次 decode。
- 线程安全：实例无共享可变状态（单线程使用契约，docstring 明确）。
- GPU/GLSL/HDR/信号防递归：不涉及。

---

## src/shared/media/topos_image_sequence.py (行数: 640)

### 发现

- [AUD-35809] [Major] hold 模式"文件消失保留前驱帧"分支不可达——`_nearest_before(slot.frame)` 恒返回 slot.frame 自身
  - 位置: src/shared/media/topos_image_sequence.py:498-503（`_nearest_before` 定义在 553-555）
  - 证据:
    ```python
    key = self._current_fingerprint(slot, self._decode_token)
    if key is None:
        # 文件消失：hold 保留上一已解码帧（无则按缺帧报错）
        if self._options.missing.mode == "hold":
            nearest = self._nearest_before(slot.frame)
            if nearest is not None and nearest != slot.frame:
                return self._read_slot(nearest, gen)
        raise FileNotFoundError(f"文件不可读（可能正被替换/删除）: {slot.path}")
    ```
    而 `_nearest_before` 用 `bisect_right(self._frames_sorted, frame)` 取 `frames_sorted[i-1]`——slot.frame 本身在有序表中，返回值必等于 slot.frame。
  - 影响: hold 策略对"文件被替换/删除"场景永远不会保留前驱帧，而是直接抛 FileNotFoundError——与缺帧号场景（行 482-488，frame 不在表中，`nearest < frame` 成立）行为不一致；`max_consecutive_missing` 容忍窗口对该路径完全失效，播放中文件被瞬时替换（指纹为 None 的瞬间）时 hold 序列会中断报错而非续播。
  - 建议: 改为 `self._nearest_before(slot.frame - 1)`（或用 bisect_left 语义取严格前驱）。
  - 置信度: 高

- [AUD-35810] [Suggestion] `_read_slot` 在无锁状态下回写共享 `self._by_frame`（预取线程与前台线程并发）
  - 位置: src/shared/media/topos_image_sequence.py:505-508
  - 证据:
    ```python
    # 实时指纹与扫描态不一致 → 刷新槽位（后续调用用新 stat）
    if key[:3] != self._fingerprint(slot)[:3]:
        slot = FrameSlot(frame=slot.frame, path=slot.path,
                         size=key[1], mtime_ns=key[2])
        self._by_frame[slot.frame] = slot
    ```
  - 影响: 预取 worker 与前台 `read_slot` 可同时执行该回写。CPython GIL 下同 key dict 赋值原子、两线程写入的值语义等价（仅 stat 新旧之差），实际风险低；但这是本文件里唯一一处绕过 `_prefetch_lock` 的共享可变状态写，与文件其余部分的加锁纪律不一致，PyPy/无 GIL 构建下成为竞态。
  - 建议: 将槽位刷新移入 `_prefetch_lock` 保护（与代际检查同一段临界区）。
  - 置信度: 高（事实）/ 风险低

- [AUD-35811] [Suggestion] `PlaneCache.put` 的字节记账未防护 None 平面，而同函数冻结循环防护了——防御不一致
  - 位置: src/shared/media/topos_image_sequence.py:177-183
  - 证据:
    ```python
    def put(self, key: tuple, planes: list[np.ndarray]) -> None:
        nbytes = sum(p.nbytes for p in planes)
        if nbytes > self._max:
            return  # 单帧超预算：不缓存（不无界增长）
        for p in planes:
            if p is not None:
                p.flags.writeable = False
    ```
  - 影响: 若 planes 真可能含 None（冻结循环的防护暗示这一假设），`p.nbytes` 在第 178 行先行崩溃；若不可能含 None，`if p is not None` 是死防护。两处假设必须统一，否则未来引入 None 平面时在记账处而非契约处失败。
  - 建议: 统一契约——要么平面列表保证无 None（删除死防护），要么记账也做 None 过滤。
  - 置信度: 高（不一致事实确凿；当前调用路径 `_read_slot` 不产生 None 平面，暂不触发）

- [AUD-35812] [Suggestion] `ToposImageSequenceSource.__init__` 的 `codec` 参数无类型注解
  - 位置: src/shared/media/topos_image_sequence.py:212-213
  - 证据:
    ```python
    def __init__(self, slots: list[FrameSlot], options: Optional[SequenceOptions] = None,
                 codec=None) -> None:
    ```
  - 影响: codec 实际为 `ToposImageCodec`（get_image_codec() 返回类型），缺注解使注入测试替身与真实 API 的契约模糊；同文件 `read_slot` 返回值也未注解。
  - 建议: 补 `codec: Optional[ToposImageCodec] = None`（该类型已可从 ..codec.topos_image_binding 导入）。
  - 置信度: 高

### 通过项
- 线程安全（主体）：单预取线程 + generation cancel 设计复核成立——pending 携带发起代号、worker 全部退出路径在锁内置 `_worker_done`、`prefetch_around` 在锁内观察 `not _worker_done` 即保证 pending 必被自取；`_read_slot` 缓存写入前的代际+关闭双门复核无竞态；close() 先加锁失效再无锁 join(timeout=5)，无死锁路径（PlaneCache 锁与 `_prefetch_lock` 无嵌套持有）。
- GPU 资源生命周期 / GLSL / 信号防递归：不涉及。
- 资源有界性：预取深度、LRU 字节上限、重复 put 先扣旧值再入账（防 `_bytes` 虚高）、单帧超预算不入缓存——复核正确。
- 输入验证：step≥1、重复帧号显式拒绝、缺帧策略白名单、error 模式开时验洞、帧号越界检查；`_pixel` 指纹（size+mtime_ns+decode_token）使被替换文件强制重解码，不返回旧数据。
- 平面只读契约：N07 修复复核——alpha 右移后、入缓存决策前统一冻结 `writeable=False`，容量 0/超预算/代际失效路径同样只读。
- 一致性校验（C07）：逐帧 probe 与首帧锚比对几何/像素域/样本域/位深/alpha/色彩，混合格式显式拒绝。
- `_output_dimensions` 与 binding `_preview_dimensions`（topos_image_binding.py:663-674）公式与舍入（`min(1.0,…)`、`int(x+0.5)`）逐项一致——`_meta` 维度与缩放解码平面几何不会错位。
- 性能模式：probe 仅在未命中路径执行（≈17µs，有注释实测依据）；F05 去除整平面冗余拷贝。
- 类型注解（除 codec 参数外）完整。

---

## src/shared/media/topos_image_source.py (行数: 319)

### 发现

- [AUD-35813] [Major] 预留/未知 pixel_format（≥4）叠加 Alpha 时绕过 open 校验并被误标为 YUV 422 命名
  - 位置: src/shared/media/topos_image_source.py:134-152（校验）、64-73（命名）
  - 证据:
    ```python
    unsupported = (
        alpha_bd <= 0
        and (self._info.pixel_format, self._info.bit_depth)
        not in _PIXEL_FORMAT_NO_ALPHA
    ) or (alpha_bd > 0 and (
        self._info.bit_depth not in (10, 12)
        or self._info.pixel_format == 3
    ))
    ...
    def _pixel_format_name(pixel_format: int, bit_depth: int,
                           alpha_bit_depth: int) -> str:
        if alpha_bit_depth <= 0:
            return _PIXEL_FORMAT_NO_ALPHA[(pixel_format, bit_depth)]
        if pixel_format == 2:
            return f'topos_gbrap{bit_depth}a{alpha_bit_depth}'
        cf = '444' if pixel_format == 1 else '422'
        return f'topos_yuva{cf}p{bit_depth}a{alpha_bit_depth}'
    ```
  - 影响: alpha 分支只黑名单了 `pixel_format == 3` 与位深，pf=4/5…（IDSC 预留域，手拼/损坏文件可达）+ bit_depth∈{10,12} + alpha 会通过 open，并被 `_pixel_format_name` 兜底拼成 'topos_yuva422p10aN'——组件名走 `_COMPONENTS.get` 落到 ('P0','P1','P2','A')，产出格式名与平面语义自相矛盾的帧。这与本模块 M1-F1 自述原则（"错标比 open 失败更糟"）直接冲突；无 alpha 的未知组合则被正确拒绝（MediaOpenError），两分支宽严不一。
  - 建议: alpha 分支改为白名单 `pixel_format in (0, 1, 2)`（对齐 no-alpha 分支的表驱动严格性）。
  - 置信度: 中（Python 层路径确凿；实际可达性取决于 native probe 是否已在 tc_image_probe 内限制 pf 域——binding 层未做范围校验，无法从 Python 侧排除）

- [AUD-35814] [Suggestion] `close()` 后 `read_frame`/`probe` 会静默自动重开，与 `is_open` 属性语义矛盾
  - 位置: src/shared/media/topos_image_source.py:201-205、212-213
  - 证据:
    ```python
    def probe(self) -> VideoMetadata:
        if self._metadata is None:
            self.open()
    ...
    def read_frame(self, frame_index: int) -> DecodedFrame:
        if frame_index != 0:
            raise MediaSourceError(...)
        if not self._open:
            self.open()
    ```
  - 影响: 消费方按 C15 契约检查 `is_open == False` 后调用 read_frame 仍会成功（并翻转内部状态为已打开）；兄弟类 `ToposImageSequenceSource.read_frame`（topos_image_sequence.py:351-354）则显式抛 "图片序列未打开"——同协议两个实现生命周期语义不一致，掩盖调用顺序错误。
  - 建议: read_frame 在关闭态抛 MediaSourceError（重开职责留给显式 open()），或冻结现行为并写进类 docstring。
  - 置信度: 中

### 通过项
- 线程安全：无可变共享状态（游标 `_current_frame` 为诊断值）；本类按单解码线程契约使用。
- GPU 资源生命周期 / GLSL / 信号防递归：不涉及（仅解码与元数据搬运，符合模块自述边界）。
- HDR 正确性：HALF 浮点档走 decode_float（float16 线性平面、无满刻度契约、不做 alpha 缩放）；UINT 档 alpha 按 spec §8.6 满刻度右移（新数组，非原地）——契约与代码一致，无未理由 clamp。
- 异常路径：FileNotFoundError/PermissionError/OSError/ToposImageError → MediaOpenError 分类映射；色彩元数据冲突（ToposColorMetadataError/UnicodeDecodeError）显式拒绝；open 失败不留半初始化可复用状态。
- 输入验证（除 AUD-35813 外）：frame_index 限定 0；(2,16) 非 HALF 显式拒绝（M1-F1）；CFA 与 alpha 互斥；pixel_aspect 分母 ≤0 防护；TMET 缺失/损坏降级已由 binding 内部吞掉（topos_image_binding.py:919-937 复核）。
- 状态提交：无持久化职责；TMET 档位展示名优先级链（label → tier+位深 → profile 类名）复核正确。
- 性能模式：open 走 probe 而非 validate（避免整文件 CRC 重复 I/O，有阶段 7 实测依据）。
- 类型注解：完整。

---

## 编号使用

已用：AUD-35800 – AUD-35814（共 15 条：Major 6 = 35800/35804/35805/35806/35809/35813，Suggestion 9 = 其余）。区间 AUD-35815–AUD-35899 未使用。

复核说明：AUD-35800 的哈希冲突例、AUD-35805 的 NaN 通过例、AUD-35806 的 3/0.3→9 例、AUD-35809 的 bisect 自返例均为代码路径推演可复现的事实；AUD-35806、AUD-35813 已按"依赖外部输入/原生库行为"降为中等置信度。
