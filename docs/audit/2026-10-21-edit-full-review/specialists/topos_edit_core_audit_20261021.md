# Edit 核心只读审计（2026-10-21）

基线：`439a8cc15f40cdaa19edd86410399c0210d97089`。根目录 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01`；遵守 `.clinerules`，未改产品代码、现有测试或素材。已读取第五轮计划/修复记录；E01–E22 的历史问题不按未修复重复报告。

本子审计未启动 GUI。证据分级：M=真实不可变模型/生产命令动态复现；S=源码/UI 调用链确认；T=当前现有测试。所有下列六项都有 M 证据，其 UI 可达性由 S 追踪；完整窗口的鼠标路径由主审计补充。

## 已有能力，不能误报为整体缺失

- 不可变 TimelineSequence/EditTrack/ClipInstance + CommandStack；ProjectCommandStack + TimelineProjectTransactionCommand 将工程/序列/素材事务合并；失败撤销和 redo 的栈恢复已有保护。
- Insert/Overwrite/Append、批量拖拽、自动建轨、框选/多选/链接 A/V、Ripple Delete/Trim、Roll、Slip、Slide、同步锁轨、In/Out Lift/Extract。
- 混合 FPS 的有理帧映射、源窗口 phase、变速/倒放/速度点/冻结帧、关键帧及逐效果参数动画。
- Multicam 已生产接线：多选创建、波形互相关同步、1–9 切角、切点 split+换源、单步撤销、保存重开。
- Nested/Compound 已生产接线：选片建子序列、进入/退出、循环阻断、递归预览/导出、父混音展开、子轨 EQ/FX/自动化/Bus保留、创建事务撤销。
- 源替换/重连、代理策略、归档/Consolidate、自动保存/备份、未知 schema 字段保留、FCPXML/OTIO 受控子集进出。
- 字幕独立轨、批量元数据、音轨/Bus/Send、音量/声像/EQ/效果机架/自动化、LUFS/true peak/Stem、BWF 能力探测已有实现。

## 新问题与复现

### C01 P2（命令层已证实、普通UI入口不足）：变换关键帧按错误范围求值

证据 M+S。`src/features/edit/core/commands.py:3697–3705` 判断 `transform.position*`（实际名称是 `transform.translate_*`），平移/旋转落默认 `[0,100]`；scale 用 `[0.1,10]`。RemoveKeyframeCommand.undo 也复制了同一错误范围（3985 附近）。`src/features/edit/ui/edit_properties_panel.py:1950–1969` 与 `src/app/controllers/edit_controller.py:4829–4833` 虽有普通关键帧槽，但主审计提醒后复核：`_build_transform_section:3256–3301` 的 XY 行不建 ◆、numeric row 的 enable_keyframe 默认False且Rotation调用未开启；普通clip参数目前没有已确认可点击的◆入口。因此本项只算M命令层错误，不能称完整GUI点击复现。效果参数有独立可用◆，不经过此错误clip值域分支。

M 实测：translate_x=-250 → evaluated 0；translate_y=500 →100；rotation=-45→0、180→100。关键帧原值仍储存，求值时 KeyframeTrack.get_value clamp，所以不是输入 UI 拒绝，而是动画显示/导出错误。

建议：复用 transform 参数 schema 的实际 min/max 与单位，clip/effect/keyframe 的值域单一来源；修正恢复最后一个关键帧时的范围。另把普通 Transform/Opacity 动画编辑入口作为独立功能缺口，核实适当UI方案后再启用。验收：负平移、大于100px平移、负角度/180°、零缩放（若产品允许）在 static→add→sample→remove→undo→save/load 均保持值，预览/导出一致。

### C02 P1：拖到新建轨区域时片段动画没有随时间位置平移

证据 M+S。`commands.py:798–799`（新建轨）、`:827`（另一 existing-track 支路）只有 with_timeline_start，缺少同轨 MoveClipCommand 已有的 `.with_temporal_shift(delta)`。旧 MoveClipToExistingTrackCommand `:1339–1341` 也一样。生产 UI 新建轨调用 `src/features/edit/ui/timeline_view.py:8086–8093`、`:8181–8188`。

M 实测：clip [100,200)，opacity关键帧[100:0,199:1]，移到新轨 start300 后关键帧仍[100,199]；片头透明度由0变1，动画已在片段出现前结束。普通新轨拖动即可发生，effects动画也受影响。

建议：统一跨轨移动/同轨移动的时域平移 helper，apply/undo 对称。验收：带 clip+effect关键帧的单片/链接组/多选跨到现有轨和新建轨，local曲线保持；Undo/Redo/保存重开逐字段一致。

### C03 P1：跨到新建轨的片段转场被移除，Undo 不能恢复

证据 M+S。同一生产新建轨调用链。`commands.py:839–843` 用源轨 with_clip_removed，顺带删除引用片段的转场；`:939–975` Undo 只回 clip，未存/恢复源转场；已有轨旧命令同型。与 DeleteClipCommand/SplitClipCommand 的 C11 快照修复不一致。

M 实测：clip头部dip_to_color duration12，执行新建轨移动，转场数量1→0，Undo仍0。即使产品决定跨轨时移除双边转场，Undo也必须恢复原状态。

建议：保存源/目标轨转场快照；单边转场随clip迁移并重锚，双边分离策略明确；撤销完整恢复。验收：head/tail/双边转场×新建/已有轨×Undo/Redo，序列to_dict回到原始数据，未选邻片不受影响。

### C04 P1：链接 A/V 变速的音频转场快照取晚，Undo 丢原始时长

证据 M+S。`commands.py:18114–18118` `_before_linked_transitions` 从已经被 linked_track.with_clip_updated 钳制过的新时间线取；主视频轨则取原 track 正确。`ChangeClipSpeedCommand.undo` 因而恢复了变更后的音频转场。

M 实测：1V+1A互链、duration100，A1头部 audio:constant_power duration80。V片设2x后A转场80→50；Undo回100帧片段，但转场仍50。

建议：apply开始、修改任何一层前捕获两个原始轨道的快照，统一原子恢复。验收：主选V/主选A都测、2x缩短/0.5x放大、单边/双边音频转场，Undo的全部clip/fade/transition一致。

### C05 P2（画面/数据错误）：Freeze 路径遗漏 Split 的 marker/转场迁移

证据 M+S。`commands.py:18296–18319` 手动把原clip换成左片+frozen片，不复用 SplitClipCommand 的 marker/transition 归属迁移。生产 `timeline_view.py:11456–11468` 上下文 Freeze frame at playhead 调该命令。

M 实测：clip[0,100)，clip-scoped marker75；Freeze50后 marker仍挂原c，但原c只到50，`validate_timeline` 报 `Marker m frame 75 outside owning clip c range [0, 50)`。尾部dip_to_color center100被重锚到左片end50，冻结右片[50,100)反而没有尾部淡出，画面会在切点淡出后突然出现冻结段。

建议：以通用split事务产生左右片/markers/transitions，然后仅给右片加freeze字段，保存必要快照；保留左段源映射。验收：分割点/左/右markers，头尾/双边transitions，reverse/ramp/mixedFPS，validate_timeline为空，Undo/Redo准确。

### C06 P2：轨锁仍可被关键帧/效果编辑绕过

证据 M+S。AddKeyframeCommand.apply `:3659–3683`、RemoveKeyframeCommand.apply `:3890–3915`、UpdateKeyframe/SetInterpolation无assert_track_editable；Add/Remove/Update/Enable/ReorderEffect 的 apply（15167、15385、15514、15693、15858起）同样没有守卫。controller的普通/效果◆槽没有补轨锁检查，Properties也未按轨锁统一禁用；普通◆当前无已确认可点击UI入口，本项用户可达重点为效果参数◆/效果启用等控件。静态Transform/Opacity已有`UpdateClipParamCommand:3286`守卫，形成同一锁状态不同操作不同策略。

M 实测：is_locked=True，AddKeyframeCommand成功新增opacity关键帧，Remove成功移除；命令栈会把这些视为合法内容变更。效果分支仅S审计，本probe尚未逐命令测试，不应把全部效果操作称为真实UI已复现。

建议：所有写内容命令apply统一assert_track_editable，Undo保留还原能力；界面同时显示拒绝原因。验收：锁轨下普通/效果 add/remove/update/interpolation/reorder/enable一律零提交，sequence revision和历史不变；Undo已有合法历史仍可运行。

## 现存专业边界（不是六个新bug）

- Multicam：网格仍是静帧/250ms去抖，非多路实时监看；无timecode/marker同步，音频不跟随切角，重同步不追溯已插入切点（`capability_registry.py:342–368`）。创建/切角/波形同步本体已有。
- Nested：父嵌套变速/倒放/曲线/混合父子FPS需要音频重采样，目前预览诊断并跳过，strict交付拒绝。子轨转场`nested_sequence.py:847`仍清空且未诊断；10/14总审计FE-F07已记录，应列“未关闭”。没有可检索的Unnest/breadcrumb专用入口；下拉切换已有。
- Preserve Pitch：离线WSOLA已有；实时仍变调resample。第五轮已把UI改为export only，属于明确边界，不再报“未披露”。
- Interchange：FCPXML/OTIO受控子集已生产接线；AAF/OMF注册为unavailable，精细audio routing/第三方FX/multicam/audition等不保证互换（`capability_registry.py:258–310`）。CSV cue sheet/Stem/BWF不能代替AAF post-production交接。
- 搜索没有发现 Match Frame/Reverse Match Frame/Replace Edit 按播放头对齐的命令/UI入口；已有 ReplaceMedia/ReplaceClipAsset 是换源/占位素材替换，不等同三/四点Replace Edit。该条是S层未找到证据，不能写成绝对全仓不存在；建议主审计核实快捷键/菜单。

## 本轮现有测试

指定`.venv/bin/python`，26个文件614项，通过，27.30s。无跳过、无失败。本批只读审计未用真实媒体/GUI，因此不能取代主审计的播放音画/导出/交互结论。

```
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q \
 tests/edit/test_timeline_command_regressions.py tests/edit/test_edit_commands.py \
 tests/edit/test_edit_transaction_undo_integrity.py tests/edit/test_trim_professional.py \
 tests/edit/test_round4_linked_slip_slide.py tests/edit/test_round4_linked_roll.py \
 tests/edit/test_round4_slide_domain.py tests/edit/test_sync_lock_ripple.py \
 tests/edit/test_timeline_ripple_delete.py tests/edit/test_timeline_clip_speed.py \
 tests/edit/test_multicam.py tests/edit/test_multicam_production.py \
 tests/edit/test_multicam_sync.py tests/edit/test_round4_multicam_preflight.py \
 tests/edit/test_nested_sequence.py tests/edit/test_nested_sequence_production.py \
 tests/edit/test_nested_audio_flatten.py tests/edit/test_round4_nested_audio_gate.py \
 tests/edit/test_round4_nested_sampling.py tests/edit/test_round4_media_replacement.py \
 tests/edit/test_round4_project_transactions.py tests/edit/test_content_state_transactions.py \
 tests/edit/test_project_migration.py tests/edit/test_project_archive.py \
 tests/edit/test_round4_core_time_windows.py tests/edit/test_clip_field_preservation.py
```

可重放探针：`/tmp/topos_edit_core_probes_20261021.py`；原始输出`/tmp/topos_edit_core_probes_20261021.txt`；前3组结构化输出`/tmp/topos_edit_core_probes_20261021.json`。探针直接使用真实生产模型与命令，没有写产品/素材文件。
