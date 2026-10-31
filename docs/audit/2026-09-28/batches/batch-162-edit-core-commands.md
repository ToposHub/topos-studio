# Batch-162 静态审查报告：src/features/edit/core/commands.py

- 日期：2026-09-28
- 审查对象：`src/features/edit/core/commands.py`（16,251 行，Edit feature 命令系统 / undo-redo 核心）
- 审查方式：只读。全文按 offset/limit 顺序通读 9 段（1–16251 行无跳读），配合 grep 复核 `object.__setattr__`、`except Exception`、手工构造器、`Tuple` 导入等模式。

## 审查覆盖说明

- **深读并逐条推演 do/undo 互逆性的类**（破坏性 / 组合 / 反序列化高风险）：
  AddClip / DeleteClip / MoveClip / MoveClipToTrack（新建+既有轨）/ CopyClipsToNewTrack / MoveClipToExistingTrack / MoveClipsOverwrite / TrimClip / RetimeMotionGraphic / SlipClip / SplitClip / UpdateClipSource / SwitchMulticamAngle / UpdateClipParam(含 effect 分支) / Keyframe 四类 / CommandStack / CreateBin / DeleteBin / DeleteAsset / DeleteTimeline / Rename 系列 / Move 系列 / DuplicateAsset / 生产信息与 Log 系列 / SmartBin / ReplaceMedia / Relink / BatchRelink / UpdateAssetProxy / ImportMediaAsset / MarkBestTake / ProjectCommandStack / ProjectMacroCommand / 轨道 Mute/Solo/Lock/SyncLock/Volume/Pan/StripEQ/EffectChain/MasterVolume/MasterMute / ApplyChannelStripPreset / ApplyMixSnapshot / SetTrackVisibility / AddTrack / 字幕五类（Add/Update/Move/Copy/Delete）/ DeleteTrack / RenameTrack / SetTrackColor / Automation 三类 / Bus 三类 / Send 三类 / MoveTrack / InsertLinkedAsset / MacroCommand / RippleDelete / SlideClip / BatchDelete / Transition 三类 / InsertClip / OverwriteClip / AppendClip / RollTrim / RippleTrim / Effect 五类 / ColorStack 三类 / Grade 三类 / Marker 四类 / ChangeClipFade / SetClipLabelColor / SetClipWaveformDisplay / SetTimelineRange / ToggleClipEnabled / SetClipLinked / ChangeClipSpeed / FreezeFrameAtPlayhead / SetClipBlendMode / ReplaceClipAsset / SetClipAudioGain。
- **按模式核对的类**：简单对称 setter（mute/solo/lock/volume/pan/visibility/color/rename/master 等）——均确认 apply 返回 `(new_timeline, updated_cmd)` 携带旧值、undo 对称恢复，未见问题。
- 复核 grep：`object.__setattr__` 共 96 处；`except Exception` 吞错 12 处；手工 `EditTrack(...)` 构造 8 处（其中 5 处为丢字段重建）；手工 `TimelineSequence(...)` 构造 1 处；`Tuple[...]` 注解 16+ 处但 `typing.Tuple` 未导入。

## src/features/edit/core/commands.py (行数: 16251)

### 发现

- [AUD-16200] [Critical] InsertLinkedAssetCommand.undo 手工重建 TimelineSequence 丢弃 subtitle/master 等全部新字段
  - 位置: src/features/edit/core/commands.py:9839-9849
  - 证据:
    ```python
    timeline = TimelineSequence(
        timeline_id=timeline.timeline_id,
        name=timeline.name,
        fps=timeline.fps,
        width=timeline.width,
        height=timeline.height,
        sample_rate=timeline.sample_rate,
        video_tracks=timeline.video_tracks,
        audio_tracks=new_audio,
        markers=timeline.markers,
    )
    ```
  - 影响: 撤销"插入 A/V 素材且自动创建了音频轨"的操作时，整个时间线被替换为仅含 9 个字段的实例。本文件他处已大量使用 `timeline.subtitle_tracks`、`master_volume`、`master_muted`、caption graphic bindings 等字段（如 7592、7402、8541）：若这些字段是必填则 undo 直接 TypeError 崩溃；若有默认值则字幕轨/混音状态/绑定等被静默清空——undo 即数据丢失。
  - 建议: 改用 `timeline._rebuild(audio_tracks=new_audio)`（本文件 AddTrackCommand:7657 等已是该写法）。
  - 置信度: 高

- [AUD-16201] [Critical] 转场三命令用手工 EditTrack 构造，apply/undo 均重置轨道的 strip_eq/automation/sends/subtitle_clips 等 14+ 字段
  - 位置: src/features/edit/core/commands.py:11106-11120（AddTransition.apply）、11159-11173（AddTransition.undo）、11432-11446（RemoveTransition.apply）、11641-11655 与 11685-11699（UpdateTransition）
  - 证据（AddTransition.apply，RemoveTransition/UpdateTransition 同构）:
    ```python
    new_track = EditTrack(
        track_id=track.track_id,
        name=track.name,
        track_type=track.track_type,
        index=track.index,
        clips=track.clips,
        transitions=new_transitions,
        is_enabled=track.is_enabled, is_locked=track.is_locked,
        is_visible=track.is_visible, is_muted=track.is_muted,
        is_solo=track.is_solo, volume=track.volume, pan=track.pan,
    )
    ```
  - 影响: 在任何轨道上添加/移除/修改转场（以及撤销这些操作），该轨道的 `strip_eq`、`effect_chain`、`automation`、`sends`、`output_bus_id`、`is_sync_locked`、`track_role`、`color`、`subtitle_clips`、`caption_metadata` 等字段全部回落默认值。本文件 1247-1251 的修复注释明确指出该模式"丢 14 个字段"，音频轨的 EQ/自动化/路由在加转场后即被清空。同为手工构造的 677/1048/7636 行是"新建空轨道"，无此问题。
  - 建议: 统一改为 `track._rebuild(transitions=new_transitions)` 或 `replace(track, transitions=...)`。
  - 置信度: 高

- [AUD-16202] [Critical] BatchDeleteCommand(ripple=True) 的 undo 误调 move_cmd.undo，片段回移全部退化为 no-op，随后恢复被删片段造成重叠损坏
  - 位置: src/features/edit/core/commands.py:10809-10830（对照正确写法 RippleDeleteCommand:10249-10255 用 `move_cmd.apply`）
  - 证据:
    ```python
    move_cmd = MoveClipCommand.create(
        track_id, clip_id, old_start, current_clip.timeline_start,
    )
    current_timeline = move_cmd.undo(current_timeline)
    ...
    for clip in reversed(self._deleted_clips):
        ...
        new_track = track.with_clip_added(clip)
    ```
  - 影响: `MoveClipCommand.undo` 语义是"恢复到 old_timeline_start"（515-536 行）；此处构造命令时把 `old_start=当前位置`、`new_start=原始位置`，再调 undo 即把片段移到其当前位置——no-op。波纹左移的跟随片段没有移回，紧接着按原位恢复被删片段，与未移回的片段区间重叠：轻则 `with_clip_added` 抛异常使 undo 中途失败（部分恢复的半执行状态），重则时间线出现重叠片段。这是常用破坏性操作（多选波纹删除）的撤销路径。
  - 建议: 与 RippleDeleteCommand 一致改为 `move_cmd.apply(current_timeline)`；并补一条 ripple 批删→undo 的回归测试。
  - 置信度: 高

- [AUD-16203] [Critical] OverwriteClipCommand.undo 的"清除分裂残留"启发式会删除同轨所有未受影响且 asset 不同的片段
  - 位置: src/features/edit/core/commands.py:12275-12286
  - 证据:
    ```python
    original_ids = set(c.clip_id for c in self._original_clips) if self._original_clips else set()
    modified_ids = set(self._modified_clips)
    clips_to_remove = []
    for clip in new_track.clips:
        if clip.clip_id not in original_ids and clip.clip_id not in modified_ids and clip.asset_id != self.asset_id:
            # This clip was created by a split, remove it
            clips_to_remove.append(clip.clip_id)
    ```
  - 影响: `_modified_clips` 只含与覆盖区间重叠的片段（12193-12199）。同轨上位于 playhead 之前或覆盖终点之后的所有其他素材片段（asset 与新插入片段不同）都不在 original/modified 集合中，会被当作"分裂残留"整体删除。撤销一次覆盖操作即可清空整条轨道的无关内容——明确的数据丢失。
  - 建议: apply 时把 `overwrite_clip_at_playhead` 实际新建的片段 ID（分裂产生的右半段等）记录为白名单，undo 只删白名单内的 ID。
  - 置信度: 高

- [AUD-16204] [Major] CommandStack/ProjectCommandStack 在 undo 抛异常时丢失命令（先 pop 后执行，失败不回填）
  - 位置: src/features/edit/core/commands.py:3744-3746（CommandStack.undo）、6207-6209（ProjectCommandStack.undo）
  - 证据:
    ```python
    command = self._undo_stack.pop()
    new_timeline = command.undo(timeline)
    self._redo_stack.append(command)
    ```
  - 影响: 多个命令的 undo 在快照缺失时抛 ValueError（如 UpdateKeyframeCommand:3468、RemoveEffectCommand:13224、RemoveTransitionCommand:11466、InsertClipCommand:12022）。异常一旦抛出：命令已从 undo 栈弹出、又未进 redo 栈——该操作从此既不可撤销也不可重做，且调用方拿到异常后时间线引用未更新，栈内容与时间线状态错位。
  - 建议: pop 后 try/except，失败时把命令 push 回 `_undo_stack` 再 re-raise；或先执行成功再弹出。
  - 置信度: 高

- [AUD-16205] [Major] 系统性缺陷：apply 计算了旧值但不持久化（无 object.__setattr__、不返回 updated 命令），7 类命令 undo 为 no-op/抛错/错位
  - 位置与证据:
    - MoveClipCommand — 470-471 行 `old_start = clip.timeline_start` 仅局部变量；undo 在 `old_timeline_start is None` 时取"当前位置"（515-523 行）→ undo 退化为 no-op（除非工厂显式传旧值；docstring 声称 "Set during apply" 属实不符）。
    - MoveClipToTrackCommand（新建轨路径）— undo 回退 `restored_start = ... else 0`（822-826 行）→ 未传旧值时片段被移到第 0 帧。
    - TrimClipCommand — apply 1626-1629 行旧值仅局部；undo 1688-1690 / 1703-1705 行抛 "Cannot undo: old values not stored"。
    - UpdateKeyframeCommand — apply 3427-3435 行 `old_value` 仅局部；undo 3467-3468 行抛错。
    - RelinkAssetCommand — apply 5701-5702 行 `old_path = asset.path` 仅局部；undo 5721-5723 行 warning 后原样返回（换链媒体无法撤销）。
    - BatchRelinkAssetsCommand — apply 5790-5793 行 `old_paths` 仅局部、从未写入 `_old_paths`；undo 5809-5811 行 no-op。
    - UpdateAssetProxyCommand — apply 5886-5887 行旧代理信息仅局部；undo 5908-5910 行 no-op。
    - RemoveTransitionCommand.apply — 11417-11424 行找到 transition 仅局部变量，不存 `_deleted_transition`；undo 11466 行抛错（仅当工厂 `create(timeline,...)` 预存时才可用）。
  - 影响: 这些命令经 `CommandStack.execute` 走 else 分支压栈的是原实例（3713-3715 行），旧值永远为空。用户视角分别是：撤销无效（剪辑移动/重链/代理）、撤销报错中断（裁剪/关键帧/转场删除）、素材被移到时间线开头（跨轨移动）。与 DeleteClipCommand:342-350、SlipClipCommand:1964-1974 等正确实现并存，属遗漏而非设计。
  - 建议: 统一采用本文件已有的两种正确机制之一：返回 `(new_timeline, updated_cmd)`，或 `object.__setattr__` 回写；为每个命令补 undo 往返测试。
  - 置信度: 高

- [AUD-16206] [Major] RemoveBusCommand.apply 删除指向该 Bus 的 sends，undo 不恢复（sends 永久丢失）
  - 位置: src/features/edit/core/commands.py:9204-9208（删除）、9217-9227（undo 仅恢复 bus 与 output_bus 路由）
  - 证据:
    ```python
    if any(s.target_bus_id == self.bus_id for s in track.sends):
        new_sends = tuple(s for s in track.sends if s.target_bus_id != self.bus_id)
        ...
        new_timeline = new_timeline.with_track_updated(updated.with_sends(new_sends))
    ```
    undo 侧仅：
    ```python
    for track_id, old_output_bus in (self._rerouted_tracks or []):
        ...
        new_timeline = new_timeline.with_track_updated(track.with_output_bus(old_output_bus))
    ```
  - 影响: 删除 Bus 时各轨发往该 Bus 的 send 被清除，`_rerouted_tracks` 只记录 output_bus 改路由；撤销后 Bus 和路由回来了，但所有 send（含 level/enabled）永久丢失。
  - 建议: apply 时对每条被改轨道快照完整 `(track_id, old_output_bus_id, old_sends)`，undo 一并还原。
  - 置信度: 高

- [AUD-16207] [Major] UpdateSubtitleClipCommand：apply 在时序字段变更时同步 caption graphic，undo 不同步（副作用不对称）
  - 位置: src/features/edit/core/commands.py:8208-8209（apply 同步）、8216-8227（undo 无同步）
  - 证据:
    ```python
    if any(key in self.updates for key in ("timeline_start", "timeline_duration")):
        new_timeline = new_timeline.sync_caption_graphic_for_cue(self.clip_id)
    ```
    而 undo 仅 `new_clip = clip.with_params(**self._previous)` 后 `with_subtitle_clip_updated`。
  - 影响: 修改字幕 cue 时间后撤销，cue 本身还原，但绑定的图形剪辑时间不回退（apply 时已被同步改写），图文错位在 undo 后残留。
  - 建议: undo 分支在恢复 timing 字段后同样调用 `sync_caption_graphic_for_cue`（或在 `_previous` 含时序字段时同步）。
  - 置信度: 高

- [AUD-16208] [Major] SwitchMulticamAngleCommand.undo 捕获 ValueError/AttributeError 后原样返回 timeline——栈状态推进但数据未回滚
  - 位置: src/features/edit/core/commands.py:2549-2553
  - 证据:
    ```python
    try:
        return split_cmd.undo(timeline)
    except (ValueError, AttributeError):
        logger.warning("SwitchMulticamAngleCommand undo: split undo failed")
        return timeline
    ```
  - 影响: 该 undo 走的是"重新构造 SplitClipCommand（`_original_clip=None`）+ 通用合并"路径（2542-2548 行），依赖 `get_clip_at(split_frame-1/+1)` 找到两段；若后续编辑改变了分割点邻接关系则合并失败，此处吞掉异常返回未回滚的时间线。CommandStack 认为撤销成功（命令进入 redo 栈），实际多机位切换仍然生效——状态与数据不一致，且 redo 会再次叠加。
  - 建议: 失败时应向上抛出（配合 AUD-16204 的回填修复），或 apply 时把 split 的 `_original_clip`/段落 ID 存入本命令使 undo 精确还原。
  - 置信度: 高

- [AUD-16209] [Major] MoveClipsOverwriteCommand 覆盖裁剪用 int() 截断量化速度映射，分数速度下保留段源窗口系统性漂移
  - 位置: src/features/edit/core/commands.py:1499-1518
  - 证据:
    ```python
    speed = clip.effective_speed()
    for index, (start, end) in enumerate(retained_intervals):
        duration = end - start
        source_offset = int((start - clip_start) * speed)
        source_duration = int(duration * speed)
    ```
  - 影响: 时间线帧→源帧换算用 `int()` 向零截断：speed=0.5 时每段丢最多 1 帧、且误差不补偿（第二保留段的 `source_start = clip.source_start + source_offset` 以截断值累加）。反复覆盖-修剪同一变速片段会让保留段的源窗口单调漂移，渲染出的画面内容与用户所见不一致（内容错误级别的 HDR/帧映射问题）。同文件 SplitClipCommand:2122 已改用 `timeline_to_source_frame()` 精确映射，此处未跟进。
  - 建议: 复用 `clip.timeline_to_source_frame()` 做边界映射，段内 duration 用两边界源帧差计算；必要时 round 而非截断。
  - 置信度: 高（漂移确定性存在；用户可见程度取决于变速使用率）

- [AUD-16210] [Major] MacroCommand.from_dict 仅识别 3 种子命令，其余静默丢弃——畸形/部分历史反序列化后宏残缺
  - 位置: src/features/edit/core/commands.py:10031-10040
  - 证据:
    ```python
    if cmd_type == "DeleteClipCommand":
        sub_commands.append(DeleteClipCommand.from_dict(sub_data))
    elif cmd_type == "MoveClipCommand":
        sub_commands.append(MoveClipCommand.from_dict(sub_data))
    elif cmd_type == "AddClipCommand":
        sub_commands.append(AddClipCommand.from_dict(sub_data))
    # Add other command types as needed
    ```
  - 影响: 宏（Ripple Delete、批量操作、Shift-split 等大量走 MacroCommand）序列化后再加载，包含 Trim/Split/UpdateParam 等子命令的宏只剩子集：redo 只执行部分子命令、undo 也只回滚部分——静默的数据错乱。这是反序列化输入校验（维度 7）的直接缺口：未知 command_type 应显式报错或至少警告计数，而非丢弃。
  - 建议: 建立全量 command_type→from_dict 注册表（本文件各命令均有 command_type 字符串可注册），未知类型抛 ValueError 或记录失败并禁止该宏执行。
  - 置信度: 高

- [AUD-16211] [Major] AddEffectCommand.apply 不持久化生成的 effect 实例，undo 用"最后一个同类型效果"回退查找
  - 位置: src/features/edit/core/commands.py:12992-13021（apply 未存 `_effect_id`/`_added_effect`）、13043-13059（undo 回退查找）
  - 证据:
    ```python
    else:
        # Find the last effect of this type
        effects = clip.effects if isinstance(clip.effects, tuple) else tuple(clip.effects) if clip.effects else ()
        effect_id = None
        for effect in reversed(effects):
            if isinstance(effect, EffectInstance) and effect.effect_type == self.effect_type:
                effect_id = effect.effect_id
                break
    ```
  - 影响: `_added_effect` 快照字段与 from_dict 路径形同虚设；当同片段存在多个同类型效果、或用户在 add 与 undo 之间 reorder/删除过效果时，undo 移除的是错误的目标（例如移除后来添加的那个），效果栈状态错乱。
  - 建议: apply 时返回 updated 命令（携带 effect_id 与 effect 快照，本文件已有大量先例），undo 按 id 精确移除。
  - 置信度: 高

- [AUD-16212] [Major] AppendClipCommand.apply 用启发式识别"新追加的片段"，同素材多片段时 undo 可能删错片段
  - 位置: src/features/edit/core/commands.py:12414-12423
  - 证据:
    ```python
    for clip in new_track.clips:
        if clip.asset_id == self.asset_id:
            # Check if this is the new clip (at the end)
            if clip.timeline_start >= new_track.duration_frames - clip.timeline_duration:
                inserted_clip = clip
                break
    ```
  - 影响: 追加操作本身在轨道末尾创建片段，但识别依据是"asset 相同且 start 接近轨道总长"。若轨道上已有同 asset 的旧片段恰在末尾区域（或 append 前轨道末尾即有该素材），undo 会移除旧片段而留下新片段——撤销后内容错误。
  - 建议: `append_clip_to_timeline` 返回新建片段的 clip_id（或 apply 前后 diff 出新增 ID）。
  - 置信度: 中（依赖轨道布局才触发，但逻辑缺陷确定）

- [AUD-16213] [Major] DeleteBinCommand.undo 只恢复 bin 记录本身，未快照/恢复 remove_bin 对子 bin 与资产归属的副作用
  - 位置: src/features/edit/core/commands.py:3974-3982（快照仅 5 个字段）、3988-4025（undo 仅重建该 bin）
  - 证据:
    ```python
    deleted_bin_data = {
        "bin_id": bin_obj.bin_id, "name": bin_obj.name,
        "parent_id": bin_obj.parent_id,
        "asset_ids": bin_obj.asset_ids, "timeline_ids": bin_obj.timeline_ids,
    }
    ```
  - 影响: `project.remove_bin` 对子 bin 的处理（reparent 到根或级联删除，实现在 project.py，本批未审）副作用不在快照内；若为级联/移动语义，撤销删除 bin 后其子 bin 的父子关系无法还原。作为破坏性命令其 undo 覆盖范围不完整。
  - 建议: 在 apply 时遍历快照 `parent_id == bin_id` 的全部后代 bin 的原 parent 值，undo 一并还原（或确认 remove_bin 无级联语义并在注释中固化契约）。
  - 置信度: 中

- [AUD-16214] [Suggestion] `typing.Tuple` 未导入却在 16+ 处注解中使用
  - 位置: src/features/edit/core/commands.py:28（`from typing import Protocol, Any, Optional, ClassVar`）；使用处 6944、7028、7115、7180、7258、7378、8817、8910、8990、9071、9135、9188 等
  - 证据:
    ```python
    def apply(self, timeline: TimelineSequence) -> Tuple[TimelineSequence, "SetTrackStripEQCommand"]:
    ```
  - 影响: 因文件头 `from __future__ import annotations`（25 行）注解延迟求值，运行时不报错；但任何 `typing.get_type_hints()`（序列化/文档/校验框架常用）会 NameError。与全文件其余 `tuple[...]` 风格不一致。
  - 建议: 统一为小写 `tuple[...]`。
  - 置信度: 高

- [AUD-16215] [Suggestion] 96 处 `object.__setattr__` 改写 frozen dataclass，模块级不可变契约名存实亡
  - 位置: 全文件 96 处（如 655、1034、2136-2138、3873、3981-3982、4197-4198、7925-7926、8201、8359-8361、14568 等）
  - 证据:
    ```python
    object.__setattr__(self, "_original_clip", clip)
    object.__setattr__(self, "_left_clip_id", left_clip_id)
    object.__setattr__(self, "_right_clip_id", right_clip_id)
    ```
  - 影响: 模块头声明 "All commands are immutable"。同一命令实例在 execute/redo 间被就地改写：单线程下语义尚可，但（1）若 undo/execute 被非 UI 线程并发调用（异步渲染回调直接走命令路径）存在数据竞争；（2）依赖实例共享才生效的 undo（如 UpdateSubtitleClipCommand）与依赖返回新实例的命令（如 SlipClipCommand）两套机制混用，正是 AUD-16205 类遗漏的温床。
  - 建议: 统一为"apply 返回 (timeline, updated_cmd)"一种机制，逐步移除 `object.__setattr__`；CommandStack 加 `threading.Lock` 保护三栈操作。
  - 置信度: 高

- [AUD-16216] [Suggestion] 9 个命令类缺少 from_dict（其一连 to_dict 都没有），历史一旦持久化将丢失/变形
  - 位置: MoveClipToExistingTrackCommand:1156（无 to_dict/from_dict）、RetimeMotionGraphicClipCommand:1815、SwitchMulticamAngleCommand:2443、UpdateProxyPolicyCommand:5204（无 to_dict/from_dict）、SetClipLabelColorCommand:15258、SetClipWaveformDisplayCommand:15301、SetTimelineRangeCommand:15364、ToggleClipEnabledCommand:15400、SetClipLinkedCommand:15443（后五者为普通 class，均无序列化）
  - 证据: grep 复核上述 9 类均无 `def from_dict`；MoveClipToExistingTrackCommand 仅有 apply/undo/create（1317-1334 行）。
  - 影响: 当前栈为内存态尚无直接故障；但这些命令进入 undo 历史后与可序列化命令不可同等对待，未来启用历史持久化（to_dict 的存在表明有此意图）时这些操作会静默消失。
  - 建议: 补齐 to_dict/from_dict，或明确注册"不可序列化"清单并在序列化层拒绝。
  - 置信度: 高

- [AUD-16217] [Suggestion] 反序列化默认值与工厂默认值不一致；多类 to_dict 丢弃旧值快照
  - 位置: src/features/edit/core/commands.py:8086 vs 8100；5160-5170、5316-5327、5434-5446
  - 证据:
    ```python
    # from_dict: mode 默认 INSERT
    mode=data.get("mode", CaptionEditMode.INSERT),
    # create: mode 默认 OVERWRITE
    mode: str = CaptionEditMode.OVERWRITE,
    ```
    UpdateAssetProductionInfoCommand.to_dict 不含 old_reel/old_scene/old_shot/old_take 与 `_applied`。
  - 影响: 同一命令经序列化往返后语义变化（OVERWRITE 变 INSERT）；UpdateAssetProductionInfo / UpdateAssetLog / BatchUpdateAssetLog 反序列化后 `old_*` 为空串默认值，undo 会把空值写回资产字段而非 no-op（若历史被加载）。属维度 7（畸形/往返数据校验）范畴。
  - 建议: from_dict 与 create 默认值对齐；to_dict 完整携带快照或在 from_dict 中将缺失快照标记为"不可 undo"。
  - 置信度: 高

- [AUD-16218] [Suggestion] 多处 `except Exception: pass` 静默吞掉反序列化/恢复失败
  - 位置: src/features/edit/core/commands.py:7393-7397（ApplyMixSnapshot strip_eq）、8902-8906、8917-8921、9027-9031、9092-9096（automation 快照恢复）、9243-9246、9423-9427（from_dict）
  - 证据:
    ```python
    for param, td in snapshot.items():
        try:
            new_auto[param] = KeyframeTrack.from_dict(td)
        except Exception:
            pass
    ```
  - 影响: undo 恢复 automation 时单个参数反序列化失败即被静默丢弃（该参数的自动化曲线丢失且无日志）；ApplyMixSnapshot 同理。违反"宽泛 except 吞错导致坏状态入栈"的审查关注点。
  - 建议: 至少 logger.warning 记录失败的 param 与异常；恢复后校验键集合完整性。
  - 置信度: 高

- [AUD-16219] [Suggestion] 栈无命令合并（coalesce）与 revision 计数；AddTrackCommand/MoveTrackCommand 的 undo 回退按位置猜测
  - 位置: src/features/edit/core/commands.py:3684-3728（CommandStack 无 merge/revision）、7692-7697（AddTrackCommand.undo 位置回退）、9598-9605（MoveTrackCommand 越界时静默 no-op）
  - 证据:
    ```python
    position = self.position if self.position is not None else len(tracks) - 1
    if position < len(tracks):
        track_id = tracks[position].track_id
    ```
  - 影响: 拖拽类操作（每次 mouse-up 一条命令）快速消耗 max_size=100 的历史，最旧可撤销操作被静默挤掉；命令层无 revision/脏标记，持久化时机完全依赖上层。AddTrackCommand 在 `_created_track_id` 缺失时按 position 取轨道，若期间轨道增删会删错轨道；MoveTrackCommand undo 在边界时 apply 仅 warning 返回原 timeline（undo 假成功）。
  - 建议: 为可合并参数类命令（transform/effect param/音量拖拽）增加 coalesce 接口（同 command 目标 + 时间窗内合并）；位置回退路径改为按 track_id/name 双重校验或直接拒绝。
  - 置信度: 高

### 通过项

- 线程边界：命令为纯数据操作、不依赖 PyQt、不触碰 OpenGL/ModernGL（模块头声明且全文未见 GUI/GL 调用）；命令执行不改共享可变容器（timeline 侧全部经 `_rebuild`/`with_*` 产生新实例）。
- 资源生命周期：命令只持数据快照，无纹理/帧缓存等 GPU 资源引用；`MoveClipsOverwriteCommand._before_timeline` 保留整时间线引用但不可变结构共享、开销可控；两个栈均有 max_size=100 截断。
- 信号防递归：本模块不发射任何信号（信号合并在上层 controller），无递归风险。
- 简单 setter 命令族（Mute/Solo/Lock/SyncLock/Volume/Pan/StripEQ/EffectChain/MasterVolume/MasterMute/Visibility/TrackColor/RenameTrack 等）：apply 一律返回 `(new_timeline, updated_cmd)` 携带旧值，undo 对称恢复，抽查未见问题。
- 快照式 undo 的正确实现（供对照）：DeleteClipCommand（剪辑+markers+解除链接伙伴三重恢复）、SlipClipCommand、UpdateClipSourceCommand（保时间线窗口的换源）、RippleTrimCommand（P1-04 原子快照还原）、ChangeClipSpeedCommand（含链接伙伴）、FreezeFrameAtPlayheadCommand、ReplaceClipAssetCommand（还原被截断时长）、ChangeClipFadeCommand、SetClipAudioGainCommand、SetClipBlendModeCommand——do/undo 互逆且经 `replace()` 保留全部字段。
- MacroCommand.apply 失败时逆序回滚已应用子命令后再抛出（9931-9947），异常路径不留半执行状态（不可变模型使中间时间线天然可弃）。
- 字幕命令的锁定校验（`_ensure_subtitle_clip_editable`/`_is_pure_caption_unlock`）在 add/update/move/copy/delete 各路径一致执行；AddSubtitleClip 的 overwrite/ripple 位移快照在 undo 中先删改后还原，避免重叠冲突。
- 大多数 `from_dict` 对缺省字段提供安全默认；SetClipBlendModeCommand.from_dict 对未知模式回退 NORMAL 并有注释说明（15997-16002）。
- 转场创建工厂（create_from_clip_gap:11300-11305）已按两片段并集钳制时长，防越界（附审计修复注释）。
- 类型注解总体状况：公开方法普遍有类型注解与 docstring；主要缺口为 AUD-16214 的 `Tuple` 拼写、部分 `Optional[Any]` 弱类型（strip_eq/effect_chain）与少数无注解分支（SetClipLabelColorCommand 等 P2/P4 小命令），Suggestion 级。
