# 复核报告：batch-162 Critical 发现（src/features/edit/core/commands.py）

- 日期：2026-09-28
- 复核人：独立复核子智能体（不采信原结论，全部证据重新采集）
- 复核对象：batch-162-edit-core-commands.md 中 4 条 Critical（AUD-16200 ~ AUD-16203）
- 权威模型来源：`src/features/edit/core/timeline_v2.py`
  - `TimelineSequence` 字段定义：2926-2964 行（21 个字段）
  - `EditTrack` 字段定义：2169-2224 行（26 个字段）
  - `TimelineSequence._rebuild`：3241-3281 行；`EditTrack._rebuild`：2340-2401 行
  - `EditTrack.with_clip_added`（含重叠校验）：2430-2435 行
- 其他佐证：`src/features/edit/core/timeline_ops.py` `delete_clip`（397-453 行，ripple 分支左移后续片段）

## [AUD-16200] InsertLinkedAssetCommand.undo 手工重建 TimelineSequence 丢字段

- 结论：**成立（维持 Critical）**
- 独立证据：
  - commands.py:9839-9849（undo 中手工构造，仅传 9 个字段：timeline_id/name/fps/width/height/sample_rate/video_tracks/audio_tracks/markers）。
  - 权威字段集 timeline_v2.py:2928-2964 共 21 个字段，逐项对比后**遗漏 12 个**：`format_settings`(2935)、`subtitle_tracks`(2939)、`caption_graphic_bindings`(2942)、`caption_track_graphic_styles`(2943)、`caption_graphics_extra`(2944)、`range_start`(2947)、`range_end`(2948)、`master_volume`(2952)、`master_muted`(2953)、`master_automation`(2957)、`master_automation_mode`(2958)、`revision`(2964)。
  - 全部遗漏字段均有默认值（`()`/`{}`/`None`/`1.0`/`False`/`"off"`/`0`），故**不会 TypeError，而是静默清空**——字幕轨全部 cue、caption 图形绑定/样式、Master 音量/静音/自动化、In/Out 区间、格式设置全部丢失，revision 归 0（破坏 TAEP 乐观锁基线）。
  - 触发路径真实且为主路径：apply 在 9772-9785 行自动创建音频轨并记录 `_created_audio_track_id`；undo 先删两片段（9824-9828）后该轨为空，9831-9833 条件成立即进入重建分支。
  - 修复路径有效：`TimelineSequence._rebuild(audio_tracks=...)` 存在（timeline_v2.py:3241-3281，保留全部字段含 revision）；AddTrackCommand 已用该写法（commands.py:7656-7661）。
- 与原报告的出入：无实质出入。原报告"若必填则 TypeError"的不确定性可以收敛：全部字段有默认值，实际走"静默清空"分支。原报告引用的字段使用处（7402/7592/8541 附近）经抽查属实。
- 修复建议确认：将 9839-9849 改为 `timeline._rebuild(audio_tracks=new_audio)`，一行修复即可保留全部 21 个字段。

## [AUD-16201] 转场三命令手工 EditTrack 重建丢轨道字段

- 结论：**成立（维持 Critical）**。丢失字段数为 13 而非原报告的"14+"，属轻微夸大，不影响定级。
- 独立证据：
  - grep 全文件手工 `EditTrack(` 构造共 11 处，其中**丢字段重建 5 处、涉及 3 个命令**，与原报告一致：
    - AddTransition.apply：11106-11120；AddTransition.undo：11159-11173
    - RemoveTransition.apply：11432-11446（其 undo 11474 用 `with_transition_added`→`_rebuild`，安全）
    - UpdateTransition.apply：11641-11655；UpdateTransition.undo：11685-11699
    - 其余 6 处（677/1048/7636/9139/9743/9772）为新建空轨道，无源轨道数据可丢，不涉问题。
  - 5 处均只传 13 个字段（track_id/name/track_type/index/clips/transitions/is_enabled/is_locked/is_visible/is_muted/is_solo/volume/pan）。
  - 权威字段集 timeline_v2.py:2175-2224 共 26 个字段，逐项对比后**遗漏 13 个**：`subtitle_clips`(2181)、`is_sync_locked`(2188)、`strip_eq`(2195)、`effect_chain`(2197)、`color`(2199)、`automation`(2203)、`automation_mode`(2207)、`is_bus`(2209)、`output_bus_id`(2210)、`sends`(2211)、`stem_group`(2215)、`caption_metadata`(2219)、`track_role`(2224)。
  - 后果确认：在任一轨道添加/移除/修改转场（含撤销、重做），该轨 EQ/效果链/自动化曲线/Bus 路由/发送/颜色/同步锁等全部回落默认值。原报告引用的 commands.py:1247-1250 修复注释属实（该注释语境下计 14 是把 `is_visible` 一并计入——本批 5 处 `is_visible` 已传入，故实际丢 13）。
  - 修复路径有效：`EditTrack._rebuild(transitions=...)` 存在且集中传全字段（timeline_v2.py:2340-2401）。
- 与原报告的出入：丢失字段实际为 13 个（原报告写"14+"，数字轻微夸大）；"3 个命令还是 5 处"的疑问已核实为**3 个命令、5 处**（Add/Update 各 2 处 apply+undo，Remove 仅 apply 1 处）。
- 修复建议确认：5 处统一改为 `track._rebuild(transitions=new_transitions)`，与库内既有 `_rebuild` 契约一致。

## [AUD-16202] BatchDeleteCommand(ripple=True) undo 误调 move_cmd.undo，回移全部 no-op

- 结论：**成立（维持 Critical）**
- 独立证据：
  - commands.py:10815-10821（undo）：`MoveClipCommand.create(track_id, clip_id, old_start, current_clip.timeline_start)` 后调 `move_cmd.undo(...)`。
  - `MoveClipCommand.create` 签名（575-588 行）为 `(track_id, clip_id, new_start, old_start=None)`——即 new_timeline_start=原始位置、old_timeline_start=**当前位置**。
  - `MoveClipCommand.undo`（515-536 行）把片段恢复到 `old_timeline_start`（当前位置）→ 每次移动都是**确定的 no-op**。
  - do 侧确实发生左移：`delete_clip(ripple=True)`（timeline_ops.py:423-452）把 `timeline_start >= clip_end` 的后续片段左移 `clip_duration`，故 undo 时跟随片段停在左移后的位置。
  - 对照组属实：RippleDeleteCommand.undo（10249-10256 行）同样构造但调 `move_cmd.apply`，且带注释 "Move back using MoveClipCommand.apply (not undo)"——同文件已有正确写法，BatchDelete 属明确笔误。
  - 后果推演（轨道 A[0,100) B[100,200) C[200,300)，波纹删 A 后 undo）：B/C 回移 no-op，恢复 A[0,100) 时撞上仍在 [0,100) 的 B，`with_clip_added`（timeline_v2.py:2430-2435）抛 `ValueError("Clip overlaps ...")`，undo 中途失败（markers/链接伙伴未恢复，叠加栈先 pop 后执行的缺陷命令丢失）。若删除点后有足够空隙使恢复片段与未回移片段恰好不相交，则 undo"成功"但跟随片段永久停在左移位置——静默错位。
- 与原报告的出入：一处措辞需修正——"重则时间线出现重叠片段"不可能经此路径落地：`with_clip_added` 必做重叠校验并抛异常，实际结局是"undo 抛异常中断（半执行）"或"静默错位"二选一，而非重叠片段入栈。核心论断（回移 no-op、常用破坏性操作的 undo 损坏）完全成立。
- 修复建议确认：改为 `move_cmd.apply(current_timeline)` 与 RippleDeleteCommand 对齐，并补 ripple 批删→undo 回归测试。

## [AUD-16203] OverwriteClipCommand.undo 启发式误删同轨未受影响片段

- 结论：**成立（维持 Critical）**
- 独立证据：
  - commands.py:12275-12286（undo 末段）：`if clip.clip_id not in original_ids and clip.clip_id not in modified_ids and clip.asset_id != self.asset_id: clips_to_remove.append(clip.clip_id)`。
  - `original_ids` 与 `modified_ids` 实为**同一集合**：apply 中 `_modified_clips=affected_clip_ids`、`_original_clips=tuple(affected_clips)`（12237-12238 行），而 affected 集合只含与覆盖区间 [playhead, new_clip_end) 相交的片段（12192-12199 行）。判定条件因此退化为"不在受影响集且 asset 与插入片段不同 → 删"。
  - 构造性反例（确认存在"未受 do 影响但被 undo 删除"）：轨道上 ClipA[0,100)（asset X）与 ClipB[200,300)（asset X），在 playhead=50 覆盖插入 asset Y 时长 40 帧——affected={ClipA}，ClipB 与 [50,90) 不相交、apply 不触碰；undo 时 ClipB 不在 original/modified 集且 `asset X != Y` → 被当"分裂残留"删除。同轨任何位于覆盖区间之外的异 asset 片段同样被整体清除。
  - 触发门槛：仅当 `_modified_clips` 非空（覆盖区间至少碰到一个既有片段）才进入该启发式；与插入片段同 asset 的片段被 `asset_id != self.asset_id` 条件豁免（反向也意味着同 asset 的真分裂残留会漏删，但那是较轻的泄漏问题）。
- 与原报告的出入：无实质出入。原报告对 `_modified_clips` 范围（12193-12199）与后果的描述与代码一致；可补充的细化是触发需满足"覆盖区间至少影响一个片段"且"被删片段 asset 须异于插入片段"，这缩小了触发面但不改变数据丢失本质。
- 修复建议确认：apply 时把 `overwrite_clip_at_playhead` 实际新建的片段 ID（分裂右半段等）记为白名单，undo 仅删白名单 ID——方向正确且必要。

## 总体判定

| 编号 | 判定 | 定级 |
|------|------|------|
| AUD-16200 | 成立（12 个字段静默丢失） | 维持 Critical |
| AUD-16201 | 成立（13 个字段丢失；3 命令 5 处） | 维持 Critical |
| AUD-16202 | 成立（回移 no-op；结局为 undo 中断或静默错位，非"重叠入栈"） | 维持 Critical |
| AUD-16203 | 成立（未受影响异 asset 片段被误删） | 维持 Critical |
