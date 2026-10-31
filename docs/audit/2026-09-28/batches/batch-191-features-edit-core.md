# 批次报告：batch-191 [P1] src/features/edit/core/

- 审查文件：`src/features/edit/core/timeline_v2.py`（3,549 行，已全文通读）
- 编号区间：AUD-19100–AUD-19199
- 维度覆盖：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失
- 说明：本文件为纯 Python 不可变数据模型（无 Qt、无 ModernGL/GLSL、无渲染线程交互），故 GPU 生命周期、GLSL·HDR、信号防递归三个维度为 N/A 或通过（详见通过项）。发现按严重度降序排列。

## src/features/edit/core/timeline_v2.py (行数: 3549)

### 发现

- [AUD-19100] [Major] 五个手工枚举字段的 EditTrack with_* 方法静默丢失 is_sync_locked 与 track_role
  - 位置: src/features/edit/core/timeline_v2.py:2491（with_strip_eq）、2511（with_effect_chain）、2544（with_name）、2563（with_color）、2605（with_output_bus）
  - 证据:
    ```python
    def with_strip_eq(self, new_strip_eq: Optional["StripEQParams"]) -> EditTrack:
        # 直接构造以绕过 _rebuild 的 None="不传" 歧义，同时保留所有字段（含 effect_chain）
        return EditTrack(
            track_id=self.track_id, name=self.name, track_type=self.track_type,
            index=self.index, clips=self.clips, subtitle_clips=self.subtitle_clips,
            transitions=self.transitions, is_enabled=self.is_enabled,
            ...
            stem_group=self.stem_group,
            caption_metadata=self.caption_metadata,
        )
    ```
    而 EditTrack 字段默认值为 `is_sync_locked: bool = True`（2188 行）与 `track_role: str = "standard"`（2224 行）。五个方法（with_strip_eq / with_effect_chain / with_name / with_color / with_output_bus）的手工构造均未传递这两个字段。
  - 影响: 与文件内 897–905 行 FX-C01 注释描述的正是同一类缺陷（"dataclasses.replace 是唯一安全方式"）。用户关闭同步锁（P1-05 is_sync_locked=False，音乐/参考轨常见）后，任何重命名轨道（commands.py:8735 处 `track.with_name(...)` 的注释甚至声称"用 with_name 保留所有字段"）、改色、改 EQ/FX 链/输出 Bus 都会把同步锁重置回 True；对 caption graphics 轨道（track_role="graphics"）执行重命名/改色会把 track_role 重置为 "standard"，使 `TimelineSequence.get_graphics_track()`（3146–3151 行）返回 None，字幕图形渲染链路静默失效。undo 快照同样携带被重置的字段。
  - 建议: 这五个方法改用 `dataclasses.replace(self, strip_eq=new_strip_eq)` 等方式（None 可正常经 replace 传递，本无需手工枚举）；或统一改走 `_rebuild` 并为可空字段引入独立哨兵。回归断言：新旧实例除目标字段外逐字段相等。
  - 置信度: 高

- [AUD-19101] [Major] __post_init__ 显式支持 int/float fps，但 to_dict 无条件访问 fps.num/.den
  - 位置: src/features/edit/core/timeline_v2.py:3427（to_dict）；契约来源 2973–2979 行
  - 证据:
    ```python
    # fps can be Rational or int for convenience
    if hasattr(self.fps, 'num'):
        if self.fps.num <= 0 or self.fps.den <= 0:
            raise ValueError(f"Invalid fps: {self.fps}")
    elif isinstance(self.fps, (int, float)):
        if self.fps <= 0:
            raise ValueError(f"Invalid fps: {self.fps}")
    ```
    对比 to_dict：
    ```python
    return {"timeline_id": self.timeline_id, "name": self.name,
        "fps": f"{self.fps.num}/{self.fps.den}", "width": self.width, ...}
    ```
  - 影响: `TimelineSequence(fps=30)` 可通过构造校验（模型注释明确声明"for convenience"），且 `effective_timeline_duration_frames`（2891–2895 行）也用 `hasattr(fps_value, "to_float")` 做了双类型防御；但保存工程时 `to_dict` 的 `self.fps.num` 会抛 `AttributeError: 'int' object has no attribute 'num'`，导致保存崩溃。当前仓库内生产调用点（timeline_view.py:14653、otio/fcpxml 适配器等）均传 Rational，故为潜伏契约破裂而非现行崩溃路径。
  - 建议: 在 `__post_init__` 中把 int/float fps 归一化为 `Rational(int(fps), 1)`（用 object.__setattr__），使模型内部单一类型；或 to_dict 做类型分支。两者取一，消除双类型契约。
  - 置信度: 高（代码级不一致确凿；现行调用方均传 Rational，故实际触发依赖未来调用方）

- [AUD-19102] [Major] EditTrack.from_dict 用 `except Exception: pass` 静默丢弃损坏的轨级 automation 数据
  - 位置: src/features/edit/core/timeline_v2.py:2748–2752
  - 证据:
    ```python
    auto_data = data.get("automation") or {}
    automation: dict = {}
    for param, track_data in auto_data.items():
        try:
            automation[param] = KeyframeTrack.from_dict(track_data)
        except Exception:
            pass
    ```
  - 影响: 违反 .clinerules"不得宽泛 except 掩盖领域错误"。单条关键帧数据损坏时，整条参数的自动化曲线（音量/声像/静音关键帧）被无日志、无诊断地静默丢弃，用户保存的混音自动化在下次加载后消失且无从排查；且 `except Exception` 还会吞掉编程错误（如 from_dict 内部 AttributeError），掩盖真实缺陷。
  - 建议: 至少 `except (ValueError, TypeError, KeyError) as exc: logger.warning("丢弃损坏的 automation 轨 %s: %s", param, exc)`（本文件已有 logger），或像 caption binding 那样把坏数据移入 orphaned 诊断字段（3491–3493 行已有先例）。
  - 置信度: 高

- [AUD-19103] [Major] TimelineSequence.from_dict 对 master_automation 同样宽泛静默吞异常
  - 位置: src/features/edit/core/timeline_v2.py:3451–3456
  - 证据:
    ```python
        master_auto = {}
        for param, track_data in (data.get("master_automation") or {}).items():
            try:
                master_auto[param] = KeyframeTrack.from_dict(track_data)
            except Exception:
                pass
    ```
  - 影响: 与 AUD-19102 同类：Master 总线自动化（W12/§3.1）在数据轻微损坏时被静默丢弃，无任何日志或用户提示；`except Exception` 范围过宽，违背铁律。
  - 建议: 收窄异常类型并 logger.warning 记录 param 与原因；两处（19102/19103）应采用同一修复模式。
  - 置信度: 高

- [AUD-19104] [Major] frozen 模型内嵌可变共享子对象，快照/undo 历史间引用别名
  - 位置: src/features/edit/core/timeline_v2.py:2088（TrackEffectInstance.params: dict）、2203（EditTrack.automation: dict，值为可变 KeyframeTrack）、3278（TimelineSequence._rebuild 原样传递 master_automation 引用）、3531–3533（with_master_automation 仅浅拷贝 dict）
  - 证据:
    ```python
    @dataclass(frozen=True)
    class TrackEffectInstance:
        effect_type: str
        params: dict          # 可变 dict 直接内嵌于 frozen 模型
    ...
    def with_master_automation(self, automation: dict) -> TimelineSequence:
        """§3.1: 创建新实例，设置 Master automation（{param: KeyframeTrack}）。"""
        return self._rebuild(master_automation=dict(automation))   # 仅浅拷贝
    ```
    `KeyframeTrack` 是非 frozen 的 `@dataclass`（src/shared/keyframe.py:119–120，`add_keyframe` 原位 append/sort）。轨级 `_clone_automation`（2660–2666 行）做了深重建，但 `_rebuild` 对 automation/master_automation 原样传引用（2392、3278 行），EditTrack 的所有派生快照共享同一 dict 与同一批 KeyframeTrack 实例；`with_master_automation` 的 `dict(automation)` 也只复制外层。
  - 影响: 与模块 docstring"不可变数据类（frozen=True）确保线程安全"（16 行）直接矛盾。命令层持有 effect_chain / automation 引用（如 commands.py:7035 `track.with_effect_chain(self.effect_chain)`），undo 前后快照共享内部可变对象：任何未来对 KeyframeTrack 的原位修改（AM-07 注释明确 Write/Touch/Latch 录制"留待完整实现"，正是原位写入场景）或对 params dict 的原位编辑，都会同时改写历史快照且不触发 revision bump，破坏 TAEP 乐观锁与 undo 一致性。当前 src/features/edit 内未检索到既有原位修改调用点，故为明确潜伏风险而非现行缺陷。
  - 建议: 统一克隆深度——with_master_automation 复用轨级 `_clone_automation` 式深拷贝；TrackEffectInstance.params/TransitionInstance.params 在构造时 `dict(params)` 复制；或引入"构造即冻结副本"的辅助函数。长期看用 frozen 容器（MappingProxy 或 frozen dataclass）替代裸 dict。
  - 置信度: 中（别名结构确凿；实际损坏依赖尚未实现的写入路径）

- [AUD-19105] [Suggestion] TimelineSequence.from_dict 对 subtitle_tracks 做了两次完整反序列化
  - 位置: src/features/edit/core/timeline_v2.py:3462–3466（第一次）与 3501（第二次）
  - 证据:
    ```python
        subtitle_ids = {
            clip.clip_id
            for track in tuple(EditTrack.from_dict(t) for t in data.get("subtitle_tracks", []))
            for clip in track.subtitle_clips
        }
        ...
            subtitle_tracks=tuple(EditTrack.from_dict(t) for t in data.get("subtitle_tracks", [])),
    ```
  - 影响: 为构建 caption binding 校验用的 cue id 集合，同一批字幕轨（含全部 SubtitleClip/CaptionStyle/CaptionCueMetadata 解析）被完整解析两遍；长字幕工程（数千 cue）加载耗时近乎翻倍，纯浪费（两次解析结果中 cue id 一致，集合本身正确）。
  - 建议: 先 `parsed_sub_tracks = tuple(EditTrack.from_dict(t) for t in data.get("subtitle_tracks", []))` 一次，集合与构造共同复用。
  - 置信度: 高

- [AUD-19106] [Suggestion] 所有实体模型 __eq__/__hash__ 仅按 ID 判等，内容不同的两个修订判相等
  - 位置: src/features/edit/core/timeline_v2.py:1150–1156（ClipInstance）、2780–2786（EditTrack）、3543–3549（TimelineSequence）等
  - 证据:
    ```python
    def __hash__(self) -> int:
        return hash(self.clip_id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ClipInstance):
            return NotImplemented
        return self.clip_id == other.clip_id
    ```
  - 影响: ID 相同但内容不同（如 undo 前后、命令执行前后）的两个实例判等且哈希相同。在 set/dict 去重或用 `clip_old == clip_new` 做"是否变化"判断的调用方会得到假阴性，漏掉变更检测。作为实体身份语义是常见设计，但与 frozen 值对象直觉相悖，宜显式声明约束。
  - 建议: 保持 ID 身份语义但在 docstring 标注"仅身份相等，不可用于内容 diff"；或提供 `content_equals()` 辅助供 undo/diff 使用。
  - 置信度: 中

- [AUD-19107] [Suggestion] with_transition_added 不校验同一切点的重复/重叠转场
  - 位置: src/features/edit/core/timeline_v2.py:2450–2478
  - 证据:
    ```python
        if left is not None and right is not None:
            _, next_ = self.get_adjacent_clips(transition.left_clip_id)
            if next_ is None or next_.clip_id != transition.right_clip_id:
                raise ValueError(f"Clips ... are not adjacent")
            if transition.start_frame < left.timeline_start or transition.end_frame > right.timeline_end:
                raise ValueError("Transition range exceeds the adjacent clips")
        ...
        new_transitions = self.transitions + (transition,)
        return self._rebuild(transitions=new_transitions)
    ```
  - 影响: 模型对 clips/subtitle_clips 严格强制不重叠（2245–2256 行），但对同一对相邻 clip 允许添加多个转场、且同轨转场时间范围允许互相重叠；timeline_render_plan 的 TransitionBlendInfo 列表按帧收集全部命中转场（`transitions: tuple[TransitionBlendInfo, ...]`），重复添加会导致同一帧双重混合、导出与预览不一致的渲染结果。当前 UI 可能阻止了重复添加，但模型边界未设防。
  - 建议: 在 with_transition_added 中拒绝同 (left_clip_id, right_clip_id) 对的重复转场，或拒绝时间范围与既有转场重叠的新转场。
  - 置信度: 中

- [AUD-19108] [Suggestion] _rebuild 的 caption_metadata 哨兵类型注解非法（裸字符串参与 `|` 联合）且以字符串字面量作哨兵
  - 位置: src/features/edit/core/timeline_v2.py:2363 与 2399–2400
  - 证据:
    ```python
                 stem_group: str | None = None,
                 track_role: str | None = None,
                 caption_metadata: Optional[CaptionTrackMetadata] | "ellipsis" = "ellipsis",
                 ) -> EditTrack:
    ...
            caption_metadata=(self.caption_metadata
                              if caption_metadata == "ellipsis" else caption_metadata),
    ```
  - 影响: `Optional[CaptionTrackMetadata] | "ellipsis"` 不是合法类型表达式（字符串字面量未包在 typing.Literal 中）；仅因模块启用 `from __future__ import annotations` 而未在运行期求值，静态检查器会报错。同时用魔法字符串做哨兵，若 CaptionTrackMetadata 未来定义了与字符串比较的 __eq__ 会有歧义风险。同类文件（TimelineSequence._rebuild）对 range_start/range_end 用 `...` 哨兵，风格也不一致。
  - 建议: 模块级 `_ELLIPSIS = object()` 哨兵 + `Literal["ellipsis"] | CaptionTrackMetadata | None`（或直接用 Ellipsis 哨兵）统一两种 _rebuild 的"不传"语义。
  - 置信度: 高

- [AUD-19109] [Suggestion] 多处容器字段/参数类型注解未参数化（Suggestion 级维度）
  - 位置: src/features/edit/core/timeline_v2.py:1646（`bands: tuple = ()`，仅靠注释说明 tuple[float, ...]）、2057（`_EFFECT_PARAM_REGISTRY: dict = {}`）、2088（`params: dict`）、2160（`to_dsp_entries(self) -> list`）、2203（`automation: dict = field(default_factory=dict)`）、2621（`with_sends(self, sends: tuple)`）、2957（`master_automation: dict = ...`）、894（`with_effects(self, effects: tuple)`）
  - 证据:
    ```python
    mode: str = "10-band"
    bands: tuple = ()  # tuple[float, ...]
    ...
    # 效果器类型 → 参数 dataclass 的注册（供反序列化 / UI 工厂使用）
    _EFFECT_PARAM_REGISTRY: dict = {}
    ...
    def with_sends(self, sends: tuple) -> EditTrack:
    ```
  - 影响: 模块自述"完整类型注解"（18 行），但上述容器类型丢失元素类型信息，静态检查无法发现传错元素类型的调用，IDE 补全失效。
  - 建议: 改为 `tuple[float, ...]`、`dict[str, Any]`、`dict[str, type]`、`tuple[Send, ...]`、`list[tuple[str, dict, bool]]` 等具体类型（KeyframeTrack 的 automation 可标 `dict[str, KeyframeTrack]`）。
  - 置信度: 高

### 通过项

- 线程安全（模块级纯数据模型，无共享可变全局状态写入；唯一模块级惰性注册表 `_ensure_effect_param_registry` 为幂等写入，风险可忽略——主体风险已计入 AUD-19104）
- GPU 资源生命周期（文件不创建/持有任何 ModernGL 纹理、缓冲或上下文，N/A）
- GLSL·HDR 正确性（无着色器代码；SubtitleClip 的 fill_*/outline_*/shadow_* HDR float 色值全链路未做 clamp(0.0,1.0)，符合 HDR 铁律；opacity/gain 等有界参数的 [0,1]/[-48,+12] 限制属业务定义而非 HDR clamp）
- 信号防递归（无 PyQt 依赖、无信号，N/A）
- 状态提交一致性（revision 在 `_rebuild` 中保持、仅经 `with_revision_bumped` 在提交边界递增，语义与 TAEP 注释一致；`with_clip_removed` 正确清理 reciprocal link 并刷新 track 引用；`with_asset_clips_removed` 走常规不可变路径；`repair_linked_clips` 在 from_dict 出口统一修复单向链接；from_dict 剔除 legacy ghost FX 有告警日志）
- 异常与取消路径（无异步任务/取消逻辑，N/A；from_dict 对坏数据的 fail-fast 与容错清洗策略总体清晰，唯 automation 两处已列发现）
- 输入验证（__post_init__ 覆盖全面：非空 ID、非负帧、正时长、speed/opacity/volume/pan/gain/EQ/limiter/compressor/reverb/delay/saturation/imager/noise-gate 各参数域、clip/字幕不重叠、subtitle 轨不变量、binding 唯一性；EQ bands 越界索引由 audio_dsp.py:858/985 的 `if i < len(...)` 防御，无崩溃路径）
- 性能模式（`timeline_to_source_frame` 热路径避免导入与分配的注释性优化到位；O(n log n) 的 __post_init__ 全量校验属不可变模型的有意不变量代价；唯一实际浪费已列 AUD-19105）
