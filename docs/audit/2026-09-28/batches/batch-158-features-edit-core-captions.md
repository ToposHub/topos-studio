# batch-158 审查报告：src/features/edit/core/captions//

- 编号区间：AUD-15800–AUD-15899
- 审查文件：6 个，共 2,305 行
- 交叉验证：`src/features/edit/core/commands.py`（MacroCommand/CommandStack/UpdateSubtitleClipCommand/AddSubtitleClipCommand.from_clip/_ensure_subtitle_clip_editable）、`src/features/edit/core/timeline_v2.py`（SubtitleClip/EditTrack/TimelineSequence 辅助方法）、`src/features/edit/core/captions/graphics.py`（CaptionGraphicBinding/_replace/cue_snapshot_for_graphic）、`src/features/edit/ui/timeline_view.py`（edit_policy 调用方）

---

## src/features/edit/core/captions/__init__.py (行数: 17)

### 发现

（无）

### 通过项
- 通过（无逻辑）：纯 re-export——从 `.graphics` 导出 CaptionCompositeInput / CaptionGraphicBinding / GraphicClip / CaptionGraphicTemplate / CaptionGraphicTemplateRegistry / CaptionTrackGraphicStyle / DEFAULT_CAPTION_GRAPHIC_TEMPLATES，从 `.model` 导出 CaptionCueMetadata / CaptionStyle / CaptionTrackMetadata，与 `__all__` 一致
- 无隐藏副作用导入，无循环依赖问题

---

## src/features/edit/core/captions/commands.py (行数: 1049)

### 发现

- [AUD-15800] [Major] DemoteGraphicToSubtitleCommand.apply 删除空图形轨后，undo 因轨道缺失静默 no-op，撤销失败丢失图形绑定与片段
  - 位置: src/features/edit/core/captions/commands.py:834-839（成因在 829-831）
  - 证据:
    ```python
    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._binding is None or self._graphic_clip is None:
            return timeline
        graphic = ClipInstance.from_dict(self._graphic_clip)
        if timeline.get_track_by_id(graphic.track_id) is None:
            return timeline
    ```
    而 apply 侧（829-831）在图形轨清空时直接移除轨道：
    ```python
    graphics_track = timeline.get_track_by_id(graphic.track_id)
    if graphics_track is not None and graphics_track.track_role == "graphics" and not graphics_track.clips:
        timeline = timeline.with_track_removed(graphics_track.track_id)
    ```
  - 影响: Promote 自动创建的专用图形轨通常只含这一个片段，Demote 后即被删除；随后 undo 命中 `get_track_by_id(...) is None` 提前返回——绑定、图形片段、cue 的 display_mode 全部未恢复，用户模板参数/同步策略等配置永久丢失（promote→demote→Ctrl+Z 是标准操作序列）。撤销从用户视角"无声失败"。
  - 建议: undo 时若轨道缺失则先重建 graphics 角色轨道（镜像 Promote.apply 的建轨逻辑）再回加片段与绑定；或 Demote.apply 不删除仅因本次移除而变空的轨道。
  - 置信度: 高

- [AUD-15801] [Suggestion] PromoteSubtitleToGraphicCommand.apply 兜底路径覆盖 `_created_track_id`，首次新建的空图形轨在异常回退后泄漏且 undo 不清理
  - 位置: src/features/edit/core/captions/commands.py:666-675, 690-705
  - 证据:
    ```python
        try:
            timeline = timeline.with_clip_added(graphics_track.track_id, graphic_clip)
        except ValueError:
            # A video track cannot overlap regular clips. Use a second graphics
            # track for simultaneous cues rather than weakening that invariant.
            created_track_id = str(uuid.uuid4())
    ```
    而首建轨道时（667-675）`created_track_id` 已指向新建轨道 A；except 分支将其改写为轨道 B 的 id。
  - 影响: 若首建空轨道上的 `with_clip_added` 抛 ValueError（如 redo 边缘场景下 clip_id 冲突），轨道 A 留在时间线中成为空图形轨，undo 只移除轨道 B。注：空轨道通常不会触发重叠 ValueError，故触发路径窄。
  - 建议: except 分支用新变量保存兜底轨道 id，或先回滚（移除）首建轨道再建新轨。
  - 置信度: 中（代码缺陷确定，触发路径窄）

- [AUD-15802] [Suggestion] UpdateSubtitleTrackStyleCommand.apply 对零字段变更（no-op 样式）仍因 override=None 的锁定 cue 抛错
  - 位置: src/features/edit/core/captions/commands.py:185-194
  - 证据:
    ```python
    for cue in track.subtitle_clips:
        if not cue.is_edit_locked():
            continue
        override = cue.style_override
        affected = (
            override is None
            or any(getattr(override, field_name) is None
                   for field_name in changed_fields)
        )
        if affected:
    ```
  - 影响: 当 `changed_fields` 为空（提交与当前完全相同的全局样式，常见于样式对话框"未修改即确定"或 Patch 命令全 None 字段 merge 后）时，任何完全继承（override=None）的锁定 cue 仍被判定 affected，整个命令抛 "Locked subtitle cue would be changed by global style edit"——实际上不会有任何字段变化。
  - 建议: 在锁定检查前加 `if not changed_fields: 跳过校验`（no-op 直接放行）。
  - 置信度: 高

- [AUD-15803] [Suggestion] MergeSubtitleClipsCommand 缺少相邻性校验与同 clip 校验，与文档声明不符且同 id 合并报错误导
  - 位置: src/features/edit/core/captions/commands.py:445-448（声明）, 469-494, 514-525（实现）
  - 证据: 文档声称两条 cue "must ... be adjacent (first.end == second.start) or overlapping/abutting"，但 `_build_macro` 仅校验存在性/锁定/同轨：
    ```python
    if owner_first.track_id != owner_second.track_id:
        raise ValueError(
            f"Cannot merge clips on different tracks: "
            f"{owner_first.track_id} vs {owner_second.track_id}"
        )
    track_id = owner_first.track_id
    ```
    随后直接以 `second.timeline_end - first.timeline_start` 作为合并时长（505 行）。
  - 影响: 合并中间有间隙的两条 cue 会静默吞掉间隙（无字幕区间变为有字幕）；`first_clip_id == second_clip_id` 时宏中第二条 Delete 因片段已删而抛 "not found"，经宏回滚后用户看到的是删除失败而非"不能合并自身"。
  - 建议: 在 `_build_macro` 增加 `first.timeline_end >= second.timeline_start`（或相等）校验与 `first_clip_id != second_clip_id` 校验。
  - 置信度: 高

- [AUD-15804] [Suggestion] 两个命令违反模块自述的 `(new_timeline, updated_cmd)` 返回契约，返回裸 timeline
  - 位置: src/features/edit/core/captions/commands.py:878（ApplyCaptionTrackTemplateCommand.apply）、964（SyncCaptionGraphicCommand.apply）
  - 证据: 模块 docstring（23-26 行）声明 "All commands return the same ``(new_timeline, updated_cmd)`` contract"，但：
    ```python
    updated = timeline.without_caption_track_graphic_style(self.track_id)
    if self.style is not None:
        updated = updated.with_caption_track_graphic_style(self.style)
    return updated
    ```
    ```python
    return timeline.sync_caption_graphic_for_cue(self.clip_id)
    ```
  - 影响: 当前 CommandStack（commands.py:3707-3714）与 MacroCommand（9933-9940）对裸返回值做了兼容分支，故行为正确；但契约不一致使未来按 docstring 假定元组返回的调用方（如直接解包）会出错，也让 CommandStack 无法拿到 updated_cmd 替换实例。
  - 建议: 统一改为 `return updated, self` / `return synced, self`。
  - 置信度: 高

- [AUD-15805] [Suggestion] 类型注解问题：`_inner` 默认 None 却标注非 Optional；`_ALLOWED_FIELDS` 未标 ClassVar 被当作 dataclass 字段；apply/undo 普遍缺返回类型注解
  - 位置: src/features/edit/core/captions/commands.py:77, 124, 279
  - 证据:
    ```python
    _inner: UpdateSubtitleClipCommand = field(default=None, repr=False, compare=False)
    ```
    ```python
    _ALLOWED_FIELDS = frozenset({"language", "role", "safe_anchor", "label"})
    ```
    对照 core/commands.py:8174 的正确写法 `FORBIDDEN_UPDATE_FIELDS: ClassVar[tuple[str, ...]] = (...)`。
  - 影响: `_inner` 注解与默认值矛盾，类型检查报错；`_ALLOWED_FIELDS` 成为 dataclass 字段后进入 `__init__` 签名、`repr` 与 `eq`，属意外公开接口；`def apply(self, timeline: TimelineSequence):` 等全部缺 `-> ...` 返回注解（Suggestion 级类型注解维度）。
  - 建议: 改为 `Optional[UpdateSubtitleClipCommand]`；`_ALLOWED_FIELDS: ClassVar[frozenset]`；补返回注解。
  - 置信度: 高

### 通过项
- 线程安全：全部为不可变 TimelineSequence 的纯函数式重建，无 Qt/OpenGL 触碰，命令经 CommandStack 串行执行
- GPU 资源生命周期 / GLSL·HDR：不涉及（无渲染资源、无浮点字面量管线）
- 信号防递归：不涉及（无 Qt 信号）
- 状态提交一致性：所有变更走 `with_*` 不可变路径并在 add 时重验 SUB-P0-03 不变量；Split/Merge 宏的子命令异常由 MacroCommand 反向回滚（core/commands.py:9941-9947）保证原子性；`from_clip` 正确携带 style_override/caption_metadata；锁校验（track.is_locked / cue.is_edit_locked）在 Split/Merge/Promote/TrackStyle/TrackMetadata/TrackTemplate 均覆盖
- 异常与取消路径：有界异常（ValueError/TypeError），无宽泛 except；交叉验证 UpdateSubtitleClipCommand/AddSubtitleClipCommand.from_clip/CaptionGraphicBinding._replace 等被调用接口签名均存在且匹配
- 输入验证：split_frame 严格区间校验、primary 白名单、binding 类型校验、updates 字段白名单

---

## src/features/edit/core/captions/delivery.py (行数: 281)

### 发现

- [AUD-15806] [Suggestion] 交付文件名去重 `used_names` 区分大小写，macOS 大小写不敏感文件系统上会静默互相覆盖
  - 位置: src/features/edit/core/captions/delivery.py:233-240（list_subtitle_tracks 的 language/label 未做大小写归一在 124-129）
  - 证据:
    ```python
    base_name = delivery_file_name(summary, sequence_name, normalized)
    name = base_name
    counter = 2
    while name in used_names:
        stem, ext = os.path.splitext(base_name)
        name = f"{stem}_{counter}{ext}"
        counter += 1
    used_names.add(name)
    ```
  - 影响: 两条轨的 language/label 仅大小写不同（如 "zh-Hans" 与 "ZH-Hans"）时生成两个不同 key 的文件名，在默认 APFS（大小写不敏感）上后写的覆盖先写的，而 manifest 记录两个文件且状态均为 written——下游按 manifest 校验会得到错误的语言轨对应关系。
  - 建议: `used_names` 以 `name.lower()`（或 `casefold()`）比较；或在 `list_subtitle_tracks` 对 language 做 BCP-47 大小写归一。
  - 置信度: 中

### 通过项
- 异常与取消路径：单轨写失败捕获 `(OSError, ValueError)` 记录 STATUS_FAILED 并继续其余轨道；manifest 写失败仅告警不使交付中断（261-269）
- 输入验证：格式白名单（SUPPORTED_FORMATS）、无字幕轨抛 ValueError、`_fps_fraction` 对 None/非法 fps 回退 24；文件名经 `_UNSAFE_FILENAME` 清洗并限长 100
- 状态提交一致性：报告 dataclass 与 manifest 结构一致（to_dict 全覆盖），skipped_empty/skipped_role 状态显式记录
- 性能：单次遍历、每轨一次渲染一次落盘，无重复 IO
- 线程安全 / GPU 资源 / GLSL·HDR / 信号防递归：不涉及
- `zip(tracks, summaries)` 两列表来自同一 `subtitle_tracks` 源且过滤条件一致，顺序对齐无误

---

## src/features/edit/core/captions/edit_policy.py (行数: 232)

### 发现

- [AUD-15807] [Major] compute_trim_range 将"整体移动"约束混入左缘裁剪（left-trim）上限，cue 与右邻相接时左缘完全无法向内拉
  - 位置: src/features/edit/core/captions/edit_policy.py:192-195（调用方 src/features/edit/ui/timeline_view.py:6308-6317）
  - 证据:
    ```python
    if right_neighbour_start is not None:
        max_start = min(end - 1, right_neighbour_start - current_duration)
    else:
        max_start = end - 1
    ```
    调用方对左/右缘裁剪共用该范围并直接 `clamp_start`：
    ```python
    start = rng.clamp_start(int(proposed_start))
    dur = rng.clamp_duration(int(proposed_dur))
    ```
  - 影响: 左缘裁剪保持 end 不变、仅缩短时长，本应允许 start 到 `end-1`；但 `right_neighbour_start - current_duration` 是"整体平移"约束。当右邻恰好与本 cue 相接（`right_nb == end`，密集字幕轨的常态）时 `max_start == current_start`，左缘向右拖动被完全钳回原位——左缘裁剪功能失效；即便有间隙也被过度限制（只能拉到 `right_nb - duration` 而非 `end-1`）。自身 docstring（"start is bounded ... above by end-1"）与实现矛盾。
  - 建议: 为左缘裁剪单独给出上限 `end - 1`（仅受左邻约束），`min/max_start` 表达纯移动语义，或返回结构中拆分 `max_start_trim_left`；调用方按 mode 选择对应边界。
  - 置信度: 高（公式与调用方路径均已核实）

### 通过项
- 线程安全：纯静态/无状态计算，无 Qt 依赖，headless 可测
- GPU 资源 / GLSL·HDR / 信号防递归：不涉及
- 输入验证：`duration = max(1, int(duration))`、`current_duration = max(1, ...)`；对象/dict 双形态访问带 hasattr 回退
- 锁定语义：锁定轨道返回 `editable=False`，调用方（timeline_view.py:6313-6314）先检查 `rng.editable` 再用 clamp，`clamp_*` 在不可编辑时原样返回不误伤
- restore_on_failure 用 getattr+callable 探测旧版 item，无崩溃路径

---

## src/features/edit/core/captions/export.py (行数: 300)

### 发现

- [AUD-15808] [Major] _render_scc 的 CEA-608 控制码时序颠倒：cue 开始发 9420+文本、cue 结束才发 942f，且缺少 942c 清屏
  - 位置: src/features/edit/core/captions/export.py:183-187
  - 证据:
    ```python
    # 9420 = resume caption loading; 942f = end of caption display.
    payload = _cea608_hex(_cue_text(cue.text).replace("\n", " "))
    lines.append(f"{start}\t9420 {payload}".rstrip())
    lines.append(f"{end}\t942f")
    ```
  - 影响: 0x9420 是 Resume Caption Loading（开始写非显示缓冲），0x942F 是 End Of Caption（触发上屏显示），并非"结束显示"。当前输出使字幕直到 cue 结束时刻才上屏，且从不发送 Erase Displayed Memory（942c）——后续 cue 逐条延迟显示并相互叠压，SCC 交付物在参考解码器上时间轴完全错位。
  - 建议: cue 开始处发 `9420 ...payload... 942f`（写完即上屏），cue 结束处发 `942c`（清屏）。
  - 置信度: 高

- [AUD-15809] [Major] _format_scc_time 的 29.97 drop-frame 换算双重补偿：分钟边界取 1798 帧后又对帧内偏移 +2，时间码恒偏约 2 帧且 10 分钟块末尾误跳下一分钟
  - 位置: src/features/edit/core/captions/export.py:196-208
  - 证据:
    ```python
    ten_minutes = nominal * 60 * 10 - 18
    minute_frames = nominal * 60 - 2
    ten_count, remainder = divmod(absolute, ten_minutes)
    minute_count, frame_in_minute = divmod(remainder, minute_frames)
    minutes_total = ten_count * 10 + minute_count
    ...
    seconds, frames = divmod(
        frame_in_minute + (2 if minute_count and frame_in_minute >= 0 else 0),
        nominal,
    )
    ```
  - 影响: SMPTE ST 12-1 下分钟 0 含 1800 实际帧（00:00:59:29 后直接跳到 00:01:00:02），跳帧已体现在显示编号中；本实现把分钟边界放在 m*1798 又额外 +2 帧。复核：F=1800 实际应显示 00:01:00:02，本函数输出 00:01:00:04（恒 +2 帧，约 67ms）；F=17980/17981 实际为 00:09:59:26/27，本函数因 `17980//1798 == 10` 输出 00:10:00:02/03——块末 2 帧被标入下一分钟。广播交付的 SCC 时间码系统性偏差。另 `frame_in_minute >= 0` 恒真，属死条件。
  - 建议: 分钟边界用 `1800 + (m-1)*1798`（或等价的标准 D↔F 换算 `D = F + 2*floor((F-2)/17982 的逐块累加)`），帧内偏移不再 +2；删除恒真条件。
  - 置信度: 高（算术两遍复核）

- [AUD-15810] [Suggestion] _render_vtt 输出未做 HTML 转义，含 `<`/`&` 的文本会被 WebVTT 解析器当作标记/实体
  - 位置: src/features/edit/core/captions/export.py:97-103
  - 证据:
    ```python
    def _render_vtt(cues: Iterable[SubtitleClip], fps: Any) -> str:
        blocks: list[str] = ["WEBVTT", ""]
        for index, cue in enumerate(cues, start=1):
            ...
            blocks.extend((str(index), f"{start} --> {end}", _cue_text(cue.text), ""))
    ```
  - 影响: WebVTT cue 文本是 HTML 子集；未转义的 `<` 可能开出一个非法标签导致该行被吞或解析告警。同文件 TTML 路径已用 `html.escape`（224-227 行），VTT 路径不一致。
  - 建议: 对 VTT 文本做 `html.escape(text, quote=False)`（保留预期的 `\n`→换行语义）。
  - 置信度: 中

### 通过项
- 输入验证：`render_subtitle_track` 校验 track_type 与格式白名单；`_fps_fraction` 对 Rational/float/非法值统一归一并对 ≤0 回退 24；负帧 `max(0, ...)` 防护
- 确定性：cue 按 `(timeline_start, clip_id)` 排序，导出顺序稳定
- 转义：TTML 文本+语言属性均转义；ASS 路径转义 `{}`、换行转 `\N`、speaker 逗号清洗；SCC 非 ASCII 替换 `?`（文档声明的保守策略）
- 帧→毫秒/厘秒换算为单调半上取整，SRT/VTT 起止不会倒序
- 线程安全 / GPU 资源 / GLSL·HDR / 信号防递归 / 状态提交：不涉及（纯导出边界，无模型变更）
- write_subtitle_track 显式 UTF-8 + `\n` 换行，跨平台一致

---

## src/features/edit/core/captions/font_resolver.py (行数: 426)

### 发现

- [AUD-15811] [Major] 无 Qt 环境下的字体扫描硬编码 4 个族名恒为"可用"，且 glob 漏掉 .otf——preflight 可能漏报缺失字体，违背模块自述目标
  - 位置: src/features/edit/core/captions/font_resolver.py:193-208
  - 证据:
    ```python
    families: set[str] = set()
    # Common font names to always claim as available (PIL can load them
    # from the filesystem scan above, or from its bundled defaults).
    families.update({"Arial", "Helvetica", "Helvetica Neue", "DejaVu Sans"})
    for d in font_dirs:
        ...
        for path in glob.glob(os.path.join(d, "**/*.tt[fc]"), recursive=True):
    ```
  - 影响: 模块目标是"deliver gate 使项目绝不静默用替代字体渲染"（SUB-P2-11）。headless 导出/AI 自动化走 `_scan_filesystem_families` 时：(1) 在未安装 Arial 的 Linux 上 preflight 判定 available，实际渲染被静默替换——恰好放过了该门禁要拦的情形；(2) `*.tt[fc]` 不匹配 `.otf`，macOS 上大量 OTF-only 族（如 Source Han Sans）被误报缺失，产生噪音告警使 preflight 结果不可信。
  - 建议: 移除硬编码集合（或仅当目录扫描确实命中对应文件时才加入）；glob 扩展为 `*.tt[fc]` + `*.otf`。
  - 置信度: 中（行为依据代码确凿；对下游 PIL 能力假设未在本批次内验证）

- [AUD-15812] [Suggestion] 多处宽泛 `except Exception`（其一为裸 `pass`）吞掉 Qt 层错误，违反 .clinerules"不得宽泛 except 掩盖领域错误"铁律
  - 位置: src/features/edit/core/captions/font_resolver.py:167-174, 336-337
  - 证据:
    ```python
        try:
            from PyQt6.QtGui import QFontDatabase  # noqa: WPS433 lazy import
            fams = QFontDatabase.families()
            if fams:
                return frozenset(fams)
        except Exception:
            pass
    ```
    ```python
            try:
                font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, float(pct))
            except Exception:
                pass
    ```
  - 影响: Qt 查询的真实失败（API 变更、权限、运行时错误）被静默降级为文件名扫描，调用方无感知；`setLetterSpacing` 的失败导致字距设置无声丢失且无日志，排障困难。
  - 建议: 收窄为 `(ImportError, AttributeError, RuntimeError)` 等预期异常并 `logger.warning` 记录降级原因。
  - 置信度: 高（规则违反明确；现有降级行为本身有注释说明，故仅 Suggestion）

### 通过项
- 线程安全（Qt 边界）：`_query_qt_families` 先检查 `QGuiApplication.instance()` 避免 `QFontDatabase.families()` 在无应用实例时 qFatal 崩溃（模块内已注释该风险）
- 输入验证：空族名回退 "Arial"；未知 weight 回退 400 不抛错；`resolve` 大小写不敏感匹配
- 性能：`available_families` 惰性缓存、`resolve` 按族名缓存，避免重复查询 QFontDatabase
- QFont 构造：pixel_size `max(1, ...)` 防护；`setFamilies` 旧版 Qt 经 AttributeError 有界回退 `setFamily`；字距百分比换算公式正确（N px → 100 + N/size*100）
- `preflight_fonts`/`collect_families` 对缺 `font_family` 属性的 clip 归入空串并跳过，容错良好
- GPU 资源 / GLSL·HDR / 信号防递归 / 状态提交：不涉及

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 5 | AUD-15800, AUD-15807, AUD-15808, AUD-15809, AUD-15811 |
| Suggestion | 8 | AUD-15801, AUD-15802, AUD-15803, AUD-15804, AUD-15805, AUD-15806, AUD-15810, AUD-15812 |
