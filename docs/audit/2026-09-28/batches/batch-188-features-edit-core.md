# Batch 188 审查报告 — src/features/edit/core/timeline_ops.py

审查日期：2026-09-28 | 编号区间：AUD-18800–AUD-18899 | 审查方式：全文通读 + 真实数据模型只读仿真验证（无文件系统副作用）

被审文件：`src/features/edit/core/timeline_ops.py`（3,346 行）。时间线纯函数操作模块（无 PyQt/OpenGL 依赖）。
关键上下文：`ClipInstance`（timeline_v2.py:208-276）除基础字段外还携带 `animated_params / label_color / waveform_show_channels / fade_in_duration / fade_out_duration / color_stack_id / color_shared / composite_id / composite_bypassed / render_cache_id / freeze_source_frame / blend_mode / audio_gain_db / raw / _source_fps_* / _source_pts_index`；`EditTrack.with_clip_added/with_clip_updated` 在中间态即执行重叠校验并抛 `ValueError`。
活调用路径已核实：`slip_clip/slide_clip/roll_trim_clips/ripple_trim_clip_left|right/insert_clip_at_playhead/overwrite_clip_at_playhead/append_clip_to_timeline/delete_clip` 由 commands.py 的 Slip/Slide/Roll/Ripple/Insert/Overwrite/Append/Delete 命令调用，且这些命令由 `timeline_view.py` / `edit_controller.py` 实际触发（timeline_view.py:8536/8760/8889，edit_controller.py:6356 等）。`trim_clip_left/trim_clip_right/move_clip/split_clip/add_transition/remove_transition/update_transition` 目前仅测试/基准引用（TrimClipCommand 走 `clip.with_trim_left/right` 的 replace 路径）。

## src/features/edit/core/timeline_ops.py (行数: 3346)

### 发现

- [AUD-18800] [Critical] 活路径纯操作（slip/slide/roll/ripple trim/overwrite）重建 ClipInstance 时丢弃十余个扩展字段，调色联动、关键帧动画、淡入淡出、混音增益、冻结帧、FPS 元数据等静默丢失
  - 位置: src/features/edit/core/timeline_ops.py:2317（slip_clip；同类：2031-2046 slide、1812-1844 roll、2451-2466 ripple left、2536-2551 ripple right、1452-1540 overwrite 半片/残片）
  - 证据:
    ```python
    # slip_clip —— timeline 位置不变，只改 source，却重建了整个实例：
    new_clip = ClipInstance(
        clip_id=clip.clip_id,
        asset_id=clip.asset_id,
        track_id=clip.track_id,
        timeline_start=clip.timeline_start,
        timeline_duration=clip.timeline_duration,
        source_start=new_source_start,
        source_duration=clip.source_duration,
        speed=clip.speed,
        linked_clip_id=clip.linked_clip_id,
        transform=clip.transform, ... is_enabled=clip.is_enabled,
    )
    ```
    未传入：`animated_params / fade_in_duration / fade_out_duration / color_stack_id / color_shared / composite_id / composite_bypassed / render_cache_id / freeze_source_frame / blend_mode / audio_gain_db / raw / label_color / waveform_show_channels / _source_fps_num/_den / _timeline_fps_num/_den / _source_pts_index`，全部回落默认值。对照组：`split_clip`（117-131）逐字段保留；commands.py `TrimClipCommand.undo` 注释明确记载"manual reconstruction previously silently reset them"并已改用 `replace()`。
  - 影响: 已用项目 venv 仿真证实：slip 后 `fade_in=0 fade_out=0 color_stack=None gain=0.0 freeze=None`；ripple 右 trim 后 `color_stack=None fade_in=0`。用户每次 slip/slide/roll/波纹 trim/overwrite，片段的 Color 页调色栈绑定（P9.1）、关键帧动画（P5.3）、音频淡入出与增益、冻结帧、RAW 参数、精确 FPS 帧映射元数据全部丢失——渲染结果错误（无调色/无动画/声像跳变）且属于用户数据丢失；SlideClipCommand.undo 仅以负向 delta 反向 slide（commands.py:10529-10531），无法找回丢失字段。这些命令均为 UI 实路径。
  - 建议: 全部改为 `dataclasses.replace(clip, <仅变更的字段>)`（或补齐 `clip.with_*` 辅助方法），与 TrimClipCommand 修复方式一致；为各操作补"字段保留"回归测试（对带 animated_params/color_stack_id/fade 的片段执行操作后断言字段不变）。
  - 置信度: 高（代码事实 + 仿真复现）

- [AUD-18801] [Critical] slide_clip 中间态触发重叠校验：紧邻轨道上任意方向的 slide 都抛 ValueError，Slide 功能不可用
  - 位置: src/features/edit/core/timeline_ops.py:2139-2145
  - 证据:
    ```python
    # 应用更新
    new_track = track.with_clip_updated(slid_clip)

    if updated_prev_clip is not None:
        new_track = new_track.with_clip_updated(updated_prev_clip)

    if updated_next_clip is not None:
        new_track = new_track.with_clip_updated(updated_next_clip)
    ```
    第一步先把被 slide 的片段移到新位置，此时前/后邻居尚未调整；`EditTrack.with_clip_updated`（timeline_v2.py:2442-2446）对中间态即抛 `ValueError("Updated clip overlaps ...")`。
  - 影响: 仿真证实（A[0,100] B[100,200] C[200,300]）：`slide +30 → ValueError: Updated clip overlaps with C`；`slide -30（后邻有余量时）→ ValueError: Updated clip overlaps with A`。紧邻（无间隙）正是 slide 的典型场景，此时任何非零 delta 都失败——SlideClipCommand.apply（timeline_view.py:8889 触发）不捕获该异常，抛出的是模型层裸 ValueError 而非文档承诺的 TimelineOperationError。对照：`roll_trim_clips`（1846-1854）与 `ripple_trim_*`（2469-2477/2553-2563）均专门按方向排序或原子 `_rebuild` 规避此问题，slide 未同步修复。
  - 建议: 仿照 roll_trim：delta>0 时先更新 next、再 slid、再 prev；delta<0 时先 prev、再 slid、再 next；或对三片段一次 `track._rebuild(clips=...)` 原子替换。
  - 置信度: 高（仿真复现，两方向均已触发）

- [AUD-18802] [Major] roll_trim_clips 调整 source 范围时未按 effective_speed 缩放，变速片段 roll 后内容错位
  - 位置: src/features/edit/core/timeline_ops.py:1800-1809
  - 证据:
    ```python
    # 计算新的剪辑属性
    # 左剪辑：延长或缩短
    new_left_timeline_duration = left_clip.timeline_duration + delta_frames
    new_left_source_duration = left_clip.source_duration + delta_frames
    ...
    new_right_timeline_start = right_clip.timeline_start + delta_frames
    new_right_timeline_duration = right_clip.timeline_duration - delta_frames
    new_right_source_start = right_clip.source_start + delta_frames
    new_right_source_duration = right_clip.source_duration - delta_frames
    ```
    对照同文件 split_clip（100-102）、trim_clip_left（210）均按 `int(delta * clip.effective_speed())` 换算。
  - 影响: 仿真证实：左片段 speed=2.0 时 roll +10，source_duration 由 200 → 210（正确应为 220）。变速片段 roll trim 后播放内容与预期偏移，音频/画面错位，属渲染内容错误；RollTrimCommand（timeline_view.py:8760 触发）为实路径。
  - 建议: source 侧增减统一乘 `effective_speed()` 并取整（与 split/trim 一致）；补变速片段的 roll 单测。
  - 置信度: 高（仿真复现）

- [AUD-18803] [Major] insert_clip_at_playhead 不分割跨越播放头的片段，直接抛 ValueError("Clip overlaps")，插入操作失败
  - 位置: src/features/edit/core/timeline_ops.py:1319-1339
  - 证据:
    ```python
    # Ripple：将 playhead 之后的所有剪辑向后移动
    clips_after = tuple(
        c for c in track.clips
        if c.timeline_start >= playhead
    )
    ...
    # 后添加新剪辑
    new_track = new_track.with_clip_added(new_clip)
    ```
    起始帧在 playhead 之前、结束帧在 playhead 之后的片段既不后移也不被分割，`with_clip_added` 校验重叠即抛错。
  - 影响: 仿真证实（X[0,300]、playhead=100）：`ValueError: Clip overlaps with X`。专业 NLE 的 Insert 语义应先分割跨越片段；当前插入点落在片段中间时 InsertClipCommand（edit_controller.py:6356、timeline_view.py:15423 触发）整体失败，且异常类型与 docstring 承诺的 TimelineOperationError 不符。
  - 建议: 对跨越 playhead 的片段先执行 split（复用 build_range_delete_commands 中 boundary 分割模式），再后移、再插入；或至少将模型层 ValueError 转译为 TimelineOperationError。
  - 置信度: 高（仿真复现）

- [AUD-18804] [Major] ripple_trim_clip_right / roll_trim_clips 左延长无素材上界校验，source 末尾可超出素材实际长度
  - 位置: src/features/edit/core/timeline_ops.py:2516-2533（ripple right，仅校验 new_source_duration > 0）；1752-1764（roll delta>0 仅校验右片段最短 1 帧）
  - 证据:
    ```python
    delta = new_end_frame - clip.timeline_end
    ...
    source_delta = int(delta * clip.effective_speed())
    new_source_duration = clip.source_duration + source_delta
    ...
    if new_source_duration <= 0:
        raise TimelineOperationError(
            f"Ripple trim would result in zero or negative duration"
        )
    ```
    对照：左入点有 `new_source_start < 0` 下界校验（2437-2440），roll 的数值入口 `build_numeric_trim_command` 也用 `clip_handle_headroom`（tail，未知时保守为 0）钳制上界——纯函数本身无上界。
  - 影响: 仿真证实：素材仅 100 帧、clip source_duration=100 时 ripple 右延至 150 帧被直接允许（source_duration=150）。下游按越界 source 帧取数会读到素材外内容（末帧冻结/解码失败/导出越界），属渲染错误风险；RippleTrimCommand 为实路径（鼠标/命令调用不经 numeric 钳制时即暴露）。
  - 建议: 为 ripple_trim_clip_right / roll_trim_clips 增加 `max_source_frames: int | None` 参数并校验 `source_start + source_duration <= max_source_frames`（None 时按 build_numeric_trim_command 的保守策略禁止延长），与左界校验对称。
  - 置信度: 高（代码事实 + 仿真复现；实际越界后果取决于下游取帧策略，标中高）

- [AUD-18805] [Major] remove_transition / update_transition 手工重建 EditTrack，丢弃 volume/pan/is_sync_locked/is_visible/color/stem_group/automation/effect_chain 等轨道设置
  - 位置: src/features/edit/core/timeline_ops.py:1022-1033（remove_transition）、1101-1112（update_transition）
  - 证据:
    ```python
    # 创建新轨道
    new_track = EditTrack(
        track_id=track.track_id,
        name=track.name,
        track_type=track.track_type,
        index=track.index,
        clips=track.clips,
        transitions=new_transitions,
        is_enabled=track.is_enabled,
        is_locked=track.is_locked,
        is_muted=track.is_muted,
        is_solo=track.is_solo,
    )
    ```
    对照同文件 add_transition（795-800）正确使用 `track._rebuild(transitions=...)`，_rebuild 的 docstring 明言其存在就是为避免"forgetting new fields"。
  - 影响: 仿真证实（audio 轨 volume=1.8、pan=-0.5、is_sync_locked=False、is_visible=False、color/stem_group 非默认）：remove_transition 后全部回落默认（volume=1.0、pan=0.0、sync_locked=True、visible=True、color=None、stem=''）。音频轨可承载转场（TransitionInstance docstring 明示 audio:* 命名空间），一旦经由本 API 删除/更新转场即静默丢失用户混音与轨道设置。当前无生产调用方（仅 ai_editing 协议字符串/测试引用），故未列 Critical，但属公开 API 的明确数据丢失缺陷。
  - 建议: 两处均改为 `track._rebuild(transitions=new_transitions)`，删除手工 EditTrack 构造。
  - 置信度: 高（仿真复现）

- [AUD-18806] [Suggestion] 遗留纯函数 trim_clip_left/trim_clip_right/move_clip/split_clip/add_transition 同样丢弃扩展字段（仅测试/基准调用）
  - 位置: src/features/edit/core/timeline_ops.py:223-238（trim_clip_left）、290-305（trim_clip_right）、343-358（move_clip）、1452-1540（overwrite 各分支）等
  - 证据:
    ```python
    new_clip = ClipInstance(
        clip_id=clip.clip_id,
        ...
        opacity=clip.opacity,
        effects=clip.effects,
        is_enabled=clip.is_enabled,
    )
    ```
    与 AUD-18800 同型。实路径 TrimClipCommand 已改用 `clip.with_trim_left/right`（replace 保留字段），但本模块同名纯函数仍保留旧构造并被 tests/edit/test_timeline_ops.py、benchmark 使用，任何新调用方接入即复现字段丢失。
  - 影响: 潜在数据丢失（同 AUD-18800），现仅影响测试语义与后续复用。
  - 建议: 统一改为 `replace()` 或委托 `clip.with_*`；或标注 deprecated 引导至命令层。
  - 置信度: 高

- [AUD-18807] [Suggestion] overwrite_clip_at_playhead 不可达 else 分支中 clip_duration 先用后赋（潜在 UnboundLocalError）
  - 位置: src/features/edit/core/timeline_ops.py:1544-1549
  - 证据:
    ```python
    else:
        # 其他重叠情况，需要切
        # 新剪辑部分覆盖现有剪辑
        overlap_start_in_clip = max(0, playhead - clip_start)
        overlap_end_in_clip = min(clip_duration, new_clip_end - clip_start)
        clip_duration = clip.timeline_duration
    ```
    `clip_duration` 在 1548 行使用、1549 行才赋值。该分支要求 `playhead > clip_start 且 new_clip_end < clip_end`，与 1436 行的 elif 条件完全重合，逻辑上不可达（仿真：中间覆盖情形走 1436 分支）。
  - 影响: 当前为死代码；一旦 elif 链被重构即触发 UnboundLocalError 崩溃。
  - 建议: 删除该死分支，或将赋值上移并明确各分支条件互斥。
  - 置信度: 高（代码顺序 + 分支条件分析 + 仿真佐证）

- [AUD-18808] [Suggestion] insert/overwrite/append 未校验 speed>0，speed=0 时除法先于模型校验抛 ZeroDivisionError
  - 位置: src/features/edit/core/timeline_ops.py:1301、1396、1661
  - 证据:
    ```python
    # 计算时间线时长（考虑速度）
    timeline_duration = int(source_duration / speed)
    ```
    函数校验了 in_frame/out_frame（1291-1294 等）但不校验 speed；ClipInstance 的 MIN_SPEED 校验发生在构造时，晚于该除法。
  - 影响: speed=0（或由外部数据注入的非法值）直接 ZeroDivisionError 崩溃，违背"外部数据须验证"铁律与函数自身 Raises 契约。
  - 建议: 入口增加 `if speed <= 0: raise TimelineOperationError(...)`。
  - 置信度: 高（路径确定；触发依赖调用方传 0）

- [AUD-18809] [Suggestion] 异常契约不一致：docstring 承诺 TimelineOperationError，模型层 ValueError 直接穿透
  - 位置: src/features/edit/core/timeline_ops.py:1339（insert 撞重叠）、1613（overwrite）、375-376（move_clip 跨轨 with_clip_added）、815（add_transition 时长越界经 with_transition_added）
  - 证据:
    ```python
    Raises:
        TimelineOperationError: 如果轨道不存在或参数无效
    ```
    而实际 `with_clip_added/with_clip_updated/with_transition_added` 抛 `ValueError("Clip overlaps with ...")` 等并直接向上传播（见 AUD-18801/18803 仿真输出）。
  - 影响: 按 docstring 捕获 TimelineOperationError 的调用方漏接裸 ValueError，异常处理路径分叉。
  - 建议: 在各操作内捕获模型 ValueError 并转译为 TimelineOperationError（保留原信息），或修正 docstring。
  - 置信度: 高

- [AUD-18810] [Suggestion] get_roll_trim_constraints 的 right_max_source_frames 参数从未被使用（死参数）
  - 位置: src/features/edit/core/timeline_ops.py:1896-1951（签名 1900-1901，函数体仅使用 left_max_source_frames，1940-1949）
  - 证据:
    ```python
    def get_roll_trim_constraints(
        sequence: TimelineSequence,
        left_clip_id: str,
        right_clip_id: str,
        left_max_source_frames: int = 0,  # 左剪辑素材最大帧数
        right_max_source_frames: int = 0  # 右剪辑素材最大帧数
    ) -> tuple[int, int]:
    ```
    函数体内无任何 `right_max_source_frames` 引用；调用方 build_numeric_trim_command（3292-3297）特意计算并传入该参数。
  - 影响: 调用方以为右移上限受右片段素材约束，实际被静默忽略——右片段延长越界依赖运行时异常兜底；误导维护者。
  - 建议: 实现该约束（右片段延长时 source_end 不超上限）或删除参数。
  - 置信度: 高

- [AUD-18811] [Suggestion] numeric_trim_bounds left 分支 ripple 与非 ripple 计算完全相同（冗余 if/else）
  - 位置: src/features/edit/core/timeline_ops.py:3221-3229
  - 证据:
    ```python
    if trim_side == "left":
        max_delta = duration - 1  # 至少留 1 帧
        if ripple:
            # 审计修复：入点左延长受前邻物理限制（ripple_trim_clip_left
            # 的前邻重叠守卫），否则 apply 抛未捕获 TimelineOperationError
            min_delta = -min(head, clip.timeline_start - prev_end)
        else:
            min_delta = -min(head, clip.timeline_start - prev_end)
    ```
  - 影响: 两分支逐字相同，if/else 为死区分；ripple 语义差异（右侧才有）被掩盖，易在后续修改中引入误判。
  - 建议: 合并为单一 min_delta 计算，用注释说明左侧 ripple 与普通 trim 约束一致的原因。
  - 置信度: 高

- [AUD-18812] [Suggestion] overwrite_clip_at_playhead 删除被覆盖片段时不清理序列级 marker.clip_id，留下悬挂引用
  - 位置: src/features/edit/core/timeline_ops.py:1598-1601
  - 证据:
    ```python
    # 删除被覆盖的剪辑
    new_track = track
    for clip_id in clips_to_remove:
        new_track = new_track.with_clip_removed(clip_id)
    ```
    对照 delete_clip（418-424）删除后执行 `sequence._rebuild(markers=...)` 清理 `marker.clip_id == clip_id` 的标记；轨道级 `with_clip_removed` 只清转场（timeline_v2.py:2437-2440），不清序列标记。
  - 影响: 被 overwrite 删除/替换的片段所绑定的标记残留 `clip_id` 悬挂引用，后续标记随片段迁移逻辑失效，序列进入 validate_timeline 无法覆盖的不一致状态。
  - 建议: 在返回前对 clips_to_remove 做与 delete_clip 相同的标记清理。
  - 置信度: 高（代码对照；实际影响取决于标记绑定使用频率，标中高）

- [AUD-18813] [Suggestion] split_clip 左右两半共享同一 render_cache_id
  - 位置: src/features/edit/core/timeline_ops.py:126、156
  - 证据:
    ```python
    left_clip = ClipInstance(
        ...
        render_cache_id=clip.render_cache_id,
    ...
    right_clip = ClipInstance(
        ...
        render_cache_id=clip.render_cache_id,
    ```
  - 影响: 两个不同范围/不同 ID 的片段引用同一缓存 manifest，缓存归属与失效语义模糊（file_controller.py:1234 会按 clip 读取该 ID）；当前消费方较少，暂列 Suggestion。
  - 建议: 右半片置 None（强制重算）或在 manifest 层按子范围派生新 ID。
  - 置信度: 中（共享事实确定；缓存错用后果未在消费端复现）

- [AUD-18814] [Suggestion] 类型注解缺失/不准确：P1-04 辅助函数参数无注解、moves 快照标注 Dict[str, int] 实含 str 值
  - 位置: src/features/edit/core/timeline_ops.py:3139-3141（clip_handle_headroom 的 clip）、3159（nearest_marker_frame 的 markers）、3179（_neighbor_bounds 的 track/clip）、3198-3204（numeric_trim_bounds 的 track/clip）、3240-3249（build_numeric_trim_command 无返回注解）、596/622（moves: "List[Dict[str, int]]"）
  - 证据:
    ```python
    def clip_handle_headroom(
        clip,
        max_source_frames: int | None = None,
    ) -> tuple[int, int]:
    ...
    def shift_sync_lock_followers(
        ...
    ) -> "Tuple[TimelineSequence, Tuple[Dict[str, int], ...]]":
    ```
    而 moves 实际写入 `{"track_id": <str>, "clip_id": <str>, "old_start": <int>, "new_start": <int>}`（655-663）。
  - 影响: 违反项目强类型约束；moves 注解与实际结构不符会误导类型检查与调用方。
  - 建议: 补 `clip: ClipInstance`、`markers: Iterable[TimelineMarker]`、`track: EditTrack` 注解；moves 改为 `Dict[str, int | str]` 或 TypedDict；build_numeric_trim_command 标注返回 `tuple[EditCommand | None, int]`。
  - 置信度: 高

- [AUD-18815] [Suggestion] SnapManager.add_point 重复添加同一点时帧索引列表出现重复项；find_nearest 对 pixels_per_frame=0 除零
  - 位置: src/features/edit/core/timeline_ops.py:2766-2783、2975
  - 证据:
    ```python
    self._all_points.add(point)
    # 添加到帧索引
    if point.frame not in self._points_by_frame:
        self._points_by_frame[point.frame] = []
    self._points_by_frame[point.frame].append(point)
    ...
    threshold_frames = int(self._config.threshold_pixels / pixels_per_frame)
    ```
    集合去重但列表无条件 append；`add_guide` 重复调用即可触发列表重复。
  - 影响: `get_point_count()`（基于集合）与 `get_points_at_frame()`（基于列表）计数不一致；pixels_per_frame=0（极端缩放/初始化时序）抛 ZeroDivisionError。
  - 建议: append 前判断 `point not in self._points_by_frame[point.frame]`；find_nearest 入口对 `pixels_per_frame <= 0` 早退返回 None。
  - 置信度: 高（重复项逻辑确定；除零触发依赖调用方，标中）

- [AUD-18816] [Suggestion] insert_clip_at_playhead 逐个 with_clip_updated 平移后续片段，O(n²) 且每步全量重排序
  - 位置: src/features/edit/core/timeline_ops.py:1328-1336
  - 证据:
    ```python
    new_track = track
    for clip in clips_after:
        # 需要重新获取，因为 with_clip_updated 可能改变了顺序
        existing = new_track.get_clip_by_id(clip.clip_id)
        if existing is not None:
            moved_clip = existing.with_timeline_start(
                existing.timeline_start + timeline_duration
            )
            new_track = new_track.with_clip_updated(moved_clip)
    ```
    对照 ripple_trim_clip_left/right 已改单次原子 `track._rebuild(clips=new_clips)`（2469-2477、2553-2563）。
  - 影响: 长轨道（数百片段）插入时 n 次 O(n log n) 重排 + n 次全片段重叠校验，UI 插入操作可感卡顿，违背 4K 实时性能基线的性能模式要求。
  - 建议: 仿照 ripple_trim 用一次 `track._rebuild(clips=tuple(...))` 原子完成平移 + 插入。
  - 置信度: 高（复杂度事实确定；卡顿程度未实测，标中高）

- [AUD-18817] [Suggestion] move_clip ripple 边界用 > 而非 >=，紧邻后续片段不跟随；跨轨移动时 ripple 被静默忽略
  - 位置: src/features/edit/core/timeline_ops.py:381-392
  - 证据:
    ```python
    if ripple and new_track_id == clip.track_id:
        ...
        clips_after = tuple(
            c for c in track.clips
            if c.timeline_start > moved_clip.timeline_end
        )
    ```
    起始帧恰等于移动后片段末端的后续片段不参与波纹；`new_track_id != clip.track_id` 时 ripple 参数无任何提示地失效（docstring 未说明）。
  - 影响: 波纹移动后与紧邻片段之间留下等于 |offset| 的间隙，或行为与用户预期（波纹收拢）不符；当前函数仅测试引用，影响有限。
  - 建议: 过滤条件改为基于原始位置的 `c.timeline_start >= clip.timeline_end`；跨轨 + ripple 时抛出或文档化忽略。
  - 置信度: 中（边界事实确定；语义意图未见文档）

### 通过项

- 线程安全：模块为纯函数 + 无共享可变模块级状态（仅常量与类定义），无 PyQt/线程交互；SnapManager 为 UI 侧单线程使用，本文件未跨线程共享状态
- GPU 资源生命周期：全文件不涉及 OpenGL/ModernGL/纹理/缓冲区，无 GPU 资源可泄漏
- GLSL/HDR 正确性：不涉及着色器与 HDR 像素数据，无 clamp(0,1) 类问题
- 信号防递归：无 PyQt 依赖（模块头声明与 import 列表核实），无信号递归风险
- undo/redo/revision 一致性（部分通过）：delete_clip 的标记随片段删除/迁移（418-449）、build_range_delete_commands 组合既有可撤销命令保证原子回滚（508-583）、shift/restore_sync_lock_followers 成对快照可逆（591-701）、build_numeric_trim_command 强制传 old_* 修复 undo 丢历史（3324-3331）均正确；slide 的 undo 依赖负向 slide 受 AUD-18800/18801 牵连
- 异常与取消路径（部分通过）：领域错误均抛类型化 TimelineOperationError，无宽泛 except；缺口为异常类型穿透（AUD-18809）
- 输入验证（部分通过）：clip/track 存在性、帧范围、in/out frame、source 下界等入口校验覆盖良好；缺口见 AUD-18804/18808
- delete_clip 标记清理与转场清理：轨道级 with_clip_removed 自动清理悬挂转场（timeline_v2.py:2439），序列级标记由 _rebuild 显式清理
- merged_ripple_spans / nearest_marker_frame / clip_handle_headroom / _neighbor_bounds / numeric_trim_bounds（右侧）：边界计算逻辑正确，wrap-around 与保守策略符合注释语义
