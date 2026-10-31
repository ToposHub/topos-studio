# P6 拆分侦察报告：src/features/edit/core/commands.py 包化拆分聚类草案

- 日期：2026-10-28
- 对象：`src/features/edit/core/commands.py`（19,746 行）
- 方法：Python `ast` 静态解析（类/方法/行区间）+ 全库 AST 级符号引用扫描（src/ apps/ tests/，约 2,600 个 .py 文件）+ 模块内类间引用图
- 性质：只读侦察。注意：本分析期间另一会话正在对该文件做单行日志插入，行号为快照值（±2 行以内波动属正常），类/方法边界与引用计数不受影响。

---

## 1. 总览统计

| 指标 | 数值 |
|---|---|
| 文件总行数 | 19,746 |
| 顶层 class 数 | **127** |
| 顶层模块级函数数 | **13**（其中 11 个为共享 helper） |
| 方法总数 | **679** |
| 类体总行数 | 18,652（占 94.5%） |
| 模块函数总行数 | 281 |
| 单类最大行数 | MoveClipToTrackCommand 469 行；InsertLinkedAssetRippleCommand 421；RippleDeleteCommand 392；TrimClipCommand / UpdateClipParamCommand / BatchDeleteCommand 各 385；SplitClipCommand 369；AddTransitionCommand 367；InsertClipCommand 364 |
| 类行数中位数 | ~103 行（绝大多数为 50–200 行的 frozen dataclass） |
| 引用该模块符号的外部文件 | **约 160 个**（src 13 个 + tests 约 147 个；其中 137 个测试文件直接 `from src.features.edit.core.commands import ...` 深导入） |
| apps/ 引用 | 0 |

方法签名高度一致：每个命令类都是 `@dataclass(frozen=True)`，实现 `apply(timeline)` / `undo(timeline)` / `to_dict()` / `from_dict(data)`（部分含 `create(...)` 工厂）。约半数方法实现"tuple 返回契约"（返回 `(new_timeline, updated_command)`）。

## 2. 结构发现：没有继承树，是 Protocol + 容器 + 共享 helper

1. **无继承层级**。127 个类中只有 1 个有基类：`EditCommand(Protocol)`（L178-202，纯结构化协议，3 个方法签名）。其余 126 个类全部是零基类的 frozen dataclass，靠鸭子类型满足协议。**不存在 QUndoCommand 族**（项目与 PyQt 解耦，undo 栈是自研的）。
2. **"undo 栈基础设施" = 6 个容器类**（L 交错分布）：
   - `EditCommand`（Protocol）
   - `CommandStack`（L4558，170 行，timeline 域 undo/redo 栈）
   - `MacroCommand`（L12069，309 行，子命令复合，含转场复活/回滚逻辑）
   - `ProjectTransactionResult`（L7583）/ `ProjectCommandStack`（L7680，158 行，媒体池/项目域栈）/ `ProjectMacroCommand`（L7845，88 行）
3. **共享 helper 家族**（模块级函数，被 7 个以上簇交叉使用，是拆分的第一块公共底座）：

   | helper | 行区间 | 使用者（类） |
   |---|---|---|
   | `assert_track_editable` | 138-155 | **40+ 个类**，横跨 clip_basic/insert_ripple/params_keyfx/clip_props/asset_bin |
   | `_clips_from_dicts` / `_transitions_from_dicts` | 158-171 | ~12 个类的 `from_dict` 反序列化 |
   | `_move_scoped_markers_with_clip` | 48-84 | 5 个 move 类 |
   | `_split_clip_transitions_for_move` | 696-730 | 3 个 move-to-track 类 |
   | `_validate_source_curve_fps` | 2900-2916 | UpdateClipSourceCommand、SwitchMulticamAngleCommand |
   | 字幕 4 件套（`_assert_caption_graphic_track_destination` / `_is_pure_caption_unlock` / `_ensure_subtitle_clip_editable`） | 87-135 | 仅 subtitle 簇 4 类 |
   | 资产 4 件套（`_record_field` / `_detach_asset_from_composition` / `_registered_scene_id_for_asset` / `_unregister_duplicated_scene`） | 5044-6055 | 仅 asset 簇 |

4. **无动态分发注册表**（已验证）：全库不存在 `globals()[...]`、`COMMAND_REGISTRY`、字符串类名反射等基于 commands.py 的动态查找。所有引用都是静态 import → 拆分为包后 re-export 即可保证兼容。

## 3. 聚类草案

聚类依据：领域语义 + 类间引用图 + 簇外引用分布。13 个业务簇 + 1 个 helper 底座。**行连续性**（该簇的类在文件中占几段）：31 个 run（理想 13），多数簇基本连续。

优先级评分：`独立性 = 簇外引用文件数（越少越独立） + 入边数（被其他簇引用越少越先拆） + 连续性`。

| 簇 | 类数 | 行数 | 行区间（段） | 簇外引用文件 (src/test) | 入边（被谁依赖） | 出边（依赖谁） | 拆分优先级 |
|---|---|---|---|---|---|---|---|
| **audio_mixer**（轨道混音/总线/发送） | 14 | 1,173 | 8306-9037, 10827-11303（2 段） | 7 (2/5) | 无 | 无（不引用任何 helper 或其他簇类） | ★★★★★ |
| **automation**（音频自动化） | 4 | 364 | 10449-10821（1 段） | 3 (1/2) | 无 | 簇内互引 1 对 | ★★★★★ |
| **color**（调色栈/共享级别） | 5 | 707 | 16767-17489（1 段） | 7 (2/5) | 无 | 无 | ★★★★★ |
| **transitions**（转场） | 3 | 827 | 13629-14461（1 段） | 7 (3/3) | 无 | 无 | ★★★★☆ |
| **markers**（标记） | 4 | 485 | 17497-17990（1 段） | 7 (3/4)* | 无 | 无 | ★★★★☆ |
| **subtitle**（字幕剪辑） | 6 | 812 | 9339-10165（1 段） | 15 (4/11) | captions/commands.py 包外引用 | 字幕 helper 4 件套、`captions.model` | ★★★☆☆ |
| **track**（轨道增删/属性） | 10 | 1,007 | 7941-8302, 9041-9331, 10169-10443, 11307-11403（4 段） | 10 (3/7) | 无 | 无 | ★★★☆☆ |
| **asset_bin**（媒体池/资产/bin/项目事务） | 29 | 2,691 | 4747-7575, 7599-7677（2 段） | 26 (2/24) | 无 | 资产 helper 4 件套；`ProjectTransactionResult`（在 infra） | ★★★☆☆ |
| **params_keyfx**（clip 参数/关键帧/效果） | 10 | 2,000 | 3416-4551, 15861-16757（2 段） | 30 (4/26) | 无 | `assert_track_editable` | ★★☆☆☆ |
| **clip_props**（单 clip 属性修改） | 15 | 2,135 | 2261-2348, 2920-3405, 17998-19650（3 段） | 36 (4/31) | 无 | clip_basic（Split/UpdateClipSource）、helper | ★★☆☆☆ |
| **insert_ripple**（插入/波纹删除/三键编辑） | 8 | 2,397 | 11429-12062, 12385-12893, 13236-13620, 14469-15361（4 段） | 27 (3/24) | **infra(MacroCommand)** | clip_basic（MoveClipCommand）、helper | ★★☆☆☆ |
| **clip_basic**（add/delete/move/trim/split/slide/roll） | 13 | 3,291 | 210-2253, 2356-2892, 12897-13228, 15369-15846（4 段） | 45 (4/40) | insert_ripple、clip_props、infra | helper | ★★☆☆☆ |
| **infra**（协议/双栈/宏） | 6 | 763 | 178, 4558-4727, 7583-7932（3 段） | **73** (8/65) | 全库 | **MacroCommand → SplitClipCommand / OverwriteClipCommand / SetClipLinkedCommand（isinstance 硬耦合，L12139-12185）** | ★☆☆☆☆（最后拆） |
| **helpers**（共享底座） | 0（13 函数） | 281 | 头部 + 散布 | 1（assert_track_editable 被 edit_controller 直接 import） | 全部簇 | timeline_v2 数据类 | 前置（第一个动作） |

\* markers 的簇外引用剔除了假阳性：`src/features/teaching_animation/core/commands.py:745` 定义了**自己的同名 `AddMarkerCommand`**（由 `from ..core.commands import` 相对导入，见 `teaching_animation/ai/command_catalog.py:36`、`teaching_animation/ui/editor_controller.py:11`）。edit 侧 AddMarkerCommand 真实消费者仅 7 个文件。

### 建议的包结构（目标态）

```
src/features/edit/core/commands/        # 由 commands.py 包化
├── __init__.py      # 全量 re-export（127 类 + 13 函数），保住 137 个测试文件的深导入
├── _support.py      # helpers 底座（assert_track_editable、_clips_from_dicts、
│                    #   _transitions_from_dicts、_move_scoped_markers_with_clip、
│                    #   _split_clip_transitions_for_move 等，先拆）
├── audio_commands.py     # audio_mixer + automation（第一刀）
├── color_commands.py     # color
├── transition_commands.py
├── marker_commands.py
├── subtitle_commands.py  # 字幕 helper 4 件套随迁
├── track_commands.py
├── asset_commands.py     # asset_bin + 资产 helper 4 件套（注意：勿名 timeline_settings.py，
│                         #   core/ 下已有 timeline_settings_command.py 存在）
├── clip_commands.py      # clip_basic + clip_props（或分两个文件）
├── keyframe_effect_commands.py  # params_keyfx
├── insert_commands.py    # insert_ripple
└── stack.py              # infra：EditCommand/CommandStack/MacroCommand/ProjectCommandStack/
                          #   ProjectMacroCommand/ProjectTransactionResult
```

### 各簇 re-export 面（簇外被引用的符号 → 消费者）

- **audio_mixer**：SetTrackVolumeCommand、SetTrackPanCommand、SetTrackStripEQCommand、SetTrackEffectChainCommand、SetTrackOutputBusCommand、SetMasterVolumeCommand、SetMasterMuteCommand、AddBusCommand、RemoveBusCommand、AddSendCommand、RemoveSendCommand、UpdateSendCommand（src 侧仅 `edit_controller.py` + `timeline_view.py` 两个文件）。ApplyChannelStripPresetCommand / ApplyMixSnapshotCommand 簇外零引用。
- **automation**：Add/Remove/Clear/SetInterpolation 全部 4 类（src 侧仅 `edit_controller.py`）。
- **color**：UpdateColorStackCommand、UpdateClipColorStackCommand、CreateColorStackCommand（src 侧 `edit_controller.py` + `commands_facade.py`）；UseSharedGradeCommand / MakeLocalGradeCopyCommand 仅测试引用。
- **transitions**：Add/Remove/Update 3 类（edit_controller、commands_facade、plan_compiler、timeline_view）。
- **markers**：Add/Edit/Delete/Move 4 类（timeline_view、plan_compiler、scene_cut/apply_ops + 3 测试）。
- **subtitle**：全部 6 符号（edit_controller、plan_compiler、timeline_view、**core/captions/commands.py**——这是唯一的 core 包内消费者，且它同时引用 infra 的 MacroCommand）。
- **track**：10 类全部（edit_controller、plan_compiler、timeline_view 为主）。
- **asset_bin**：27/29 有簇外引用，集中在 `media_pool_panel.py`（17 类）+ `edit_controller.py`（5 类）；BatchRelinkAssetsCommand、UpdateAssetProxyCommand 零引用。
- **params_keyfx**：9/10（edit_controller、commands_facade 为主）；UpdateKeyframeCommand 零引用。
- **clip_basic**：13 类全部有引用（timeline_view 53 符号、edit_controller 49 符号是最大消费方）。
- **clip_props**：14/15（仅 UpdateClipSampleFieldsCommand 只有测试引用）。
- **insert_ripple**：8 类全部（timeline_view、multicam、timeline_ops、plan_compiler）。
- **infra**：CommandStack(39 文件)、MacroCommand(30)、ProjectCommandStack(16)、ProjectMacroCommand(3)；EditCommand、ProjectTransactionResult 零外部引用（纯内部类型）。

### 簇间依赖图（模块内 AST 自引用统计）

```
helpers(_support)  ←── 所有业务簇（audio/color/transitions/markers/track 除外，它们零 helper 依赖）
clip_basic ──────←── insert_ripple (RippleDeleteCommand、BatchDeleteCommand → MoveClipCommand)
                  ←── clip_props   (SwitchMulticamAngleCommand → SplitClipCommand、UpdateClipSourceCommand)
                  ←── infra        (MacroCommand → SplitClipCommand)
insert_ripple ───←── infra        (MacroCommand → OverwriteClipCommand, L12171)
clip_props ──────←── infra        (MacroCommand → SetClipLinkedCommand, L12185)
asset_bin ───────←── (内部: BatchUpdateAssetLogCommand → UpdateAssetLogCommand)
                     (出边: UpdateTimelineSettingsCommand/ProjectCommandStack → ProjectTransactionResult ∈ infra)
subtitle ────────←── (内部: Move → Add; 全部 → CaptionEditMode/字幕 helper)
automation ──────←── (内部: Add → SetInterpolation)
```

依赖方向是单向无环的：`_support → clip_basic → {insert_ripple, clip_props, infra}`。唯一需要警惕的潜在环：infra 的 MacroCommand import 三个 clip 类——只要 clip 簇永远不 import MacroCommand/CommandStack（现状如此），方向就是安全的。

## 4. 建议拆分顺序（第一刀最安全的选择）

### 第一刀：`commands/_support.py`（helpers 底座）+ `commands/audio_commands.py`（audio_mixer + automation）

**为什么 audio 是最安全的第一刀：**

1. **零入边**：没有任何其他簇的类引用这 18 个类。
2. **零共享依赖**：audio/automation 类是全部 127 个类中唯一**不引用任何模块级 helper**（不用 `assert_track_editable`、不用 `_transitions_from_dicts`）的群体——完全自包含，只依赖 `timeline_v2` 数据类。
3. **簇外引用面最小的大簇**：1,537 行（文件 7.8%）、18 个类，src 侧只被 `edit_controller.py` 和 `timeline_view.py`（仅 SetTrackVolumeCommand）两个文件引用，测试 7 个文件。
4. **行区间干净**：三段（8306-9037、10449-10821、10827-11303），机械搬移无交叉。
5. 验证手段充分：tests/edit/test_audio_bus_routing.py、test_audio_automation.py、test_strip_eq.py、test_routing_ui.py、T:app/test_fix_wd17w2_aud.py 直接覆盖。

第一刀同时把 `assert_track_editable` 等 helpers 拆到 `_support.py`（audio 用不到它们，但这一步为后续所有刀铺路，且 `assert_track_editable` 被 edit_controller.py 直接 import 过——re-export 面注意）。

### 后续顺序（独立性从高到低）

| 序 | 动作 | 行数 | 理由 |
|---|---|---|---|
| 0 | `_support.py`（helpers） | 281 | 全部后续拆分的前置；无入边风险 |
| 1 | `audio_commands.py`（audio_mixer+automation） | 1,537 | 见上 |
| 2 | `color_commands.py` | 707 | 1 段连续、零入边、7 文件；facade 迁移点单一 |
| 3 | `transition_commands.py` | 827 | 1 段连续、零入边、7 文件 |
| 4 | `marker_commands.py` | 485 | 1 段连续、零入边；注意 teaching 同名类（见风险 3） |
| 5 | `subtitle_commands.py` | 812 | 1 段连续；字幕 helper 随迁；唯一包内消费者 captions/commands.py 经 `__init__` 中转可不改 |
| 6 | `track_commands.py` | 1,007 | 零入边但文件中散布 4 段，搬移需小心 |
| 7 | `asset_commands.py` | 2,691 | 资产 helper 随迁；ProjectCommandStack/ProjectMacroCommand/ProjectTransactionResult 此时可留 stack.py 先行或随本刀 |
| 8 | `keyframe_effect_commands.py`（params_keyfx） | 2,000 | 引用面开始变大（commands_facade 全量迁移点） |
| 9 | `clip_commands.py`（clip_basic） | 3,291 | 最大簇；先于 insert/clip_props |
| 10 | `clip_props` 并入 clip_commands.py 或 `clip_property_commands.py` | 2,135 | 依赖 clip_basic，必须在其后 |
| 11 | `insert_commands.py`（insert_ripple） | 2,397 | 依赖 clip_basic（MoveClipCommand） |
| 12 | `stack.py`（infra） | 763 | 最后：MacroCommand 对 Split/Overwrite/SetClipLinked 的 isinstance 耦合在此收敛；`__init__.py` 收口全量 re-export |

每一刀均保持 `commands/__init__.py` 全量 re-export，外部 160 个引用文件零改动。

## 5. 风险清单

1. **【高】137 个测试文件 + 13 个 src 文件深导入 `src.features.edit.core.commands`**。包化后 `commands/__init__.py` 必须 re-export 全部 127 类 + 13 函数（含下划线私有 helper？不需要，但 `assert_track_editable` 是公开名，edit_controller.py 直接 import 它）。建议显式 `__all__` + 显式 import 列表；ruff F401 需 `as` 别名或 noqa。
2. **【高】循环 import 唯一风险点：MacroCommand**。`MacroCommand._resurrect_adjacent_transitions` 等逻辑对 `SplitClipCommand`（L12143）、`OverwriteClipCommand`（L12171）、`SetClipLinkedCommand`（L12185）做 isinstance 判断。infra 若先于 clip 簇拆出，则 stack.py → clip_commands.py 单向依赖可行；但任何人在 clip_commands.py 里 import CommandStack/MacroCommand 立即成环。约定并写入模块 docstring：**clip/insert 子模块禁止 import infra**。
3. **【中】同名类阴影**：`src/features/teaching_animation/core/commands.py:745` 自带 `AddMarkerCommand`（同文件还有 RemoveMarkerCommand/RenameMarkerCommand）。拆分期间 grep 引用计数必须排除 teaching_animation 路径，否则 markers 簇引用面会虚高 2 个文件。同理存在两个无关的 "commands" 模块命名空间（edit/core 与 teaching/core、edit/core/captions），IDE 自动 import 时容易串。
4. **【中】命名冲突**：core/ 已存在 `timeline_settings_command.py`（TimelineSettingsCommand 类）与 commands.py 内的 `UpdateTimelineSettingsCommand`（asset_bin 簇）职责相近但类不同。新子模块不要命名为 `timeline_settings.py`；asset 簇模块名用 `asset_commands.py`。
5. **【中】序列化契约随类走**：每个类的 `to_dict`/`from_dict` 与 `command_id` 字符串是 undo 历史/快照持久化契约（含 `_before_transitions` 等私有恢复字段的 `_clips_from_dicts`/`_transitions_from_dicts` 反序列化）。搬移不得改动任何 dict 键名。已确认无全局注册表/字符串反射分发，静态 import 全覆盖。
6. **【低】captions/commands.py 是包内先行先例**：它 import 了 `AddSubtitleClipCommand`、`CaptionEditMode`、`DeleteSubtitleClipCommand`、`MacroCommand`（跨 infra+subtitle 两簇）。subtitle 拆出后让它继续从 `src.features.edit.core.commands`（包 `__init__`）导入，零改动；不要顺手改成深导入新子模块，否则加剧对未来拆分的阻力。
7. **【低】`core/__init__.py` 与 `commands_facade.py` 双层 re-export**：`core/__init__.py` 只导出 12 个命令符号（Add/Delete/Move/Trim/RetimeMG/Split/3 transitions/CommandStack/EditCommand）；`commands_facade.py` 的 `get_command_classes()` 深导入 15 个符号。两者都是显式清单，拆分后 import 路径不变（仍指向 commands 包），无需改动，但需加入回归验证。
8. **【低】行区间非连续簇的机械搬移**：track（4 段）、insert_ripple（4 段）、clip_basic（4 段）、infra（3 段）需要按类搬移而非按行切块；本报告第 3 节行区间表 + 附录可作为搬移清单。
9. **【低】另一会话并发编辑**：本文件正被插入单行日志，搬移前 rebase/重读最新行号；建议拆分 PR 以"整类搬移、git mv 语义"提交以便审查。

## 附录 A：127 类清单（名称/行区间/行数/方法数，快照于 2026-10-28）

| 类 | 行区间 | 行数 | 方法数 |
|---|---|---|---|
| EditCommand | 178-202 | 25 | 3 |
| AddClipCommand | 210-306 | 97 | 5 |
| DeleteClipCommand | 314-484 | 171 | 5 |
| MoveClipCommand | 492-689 | 198 | 5 |
| MoveClipToTrackCommand | 734-1202 | 469 | 9 |
| CopyClipsToNewTrackCommand | 1210-1358 | 149 | 5 |
| MoveClipToExistingTrackCommand | 1366-1619 | 254 | 5 |
| MoveClipsOverwriteCommand | 1627-1861 | 235 | 7 |
| TrimClipCommand | 1869-2253 | 385 | 6 |
| RetimeMotionGraphicClipCommand | 2261-2348 | 88 | 5 |
| SlipClipCommand | 2356-2516 | 161 | 5 |
| SplitClipCommand | 2524-2892 | 369 | 5 |
| UpdateClipSourceCommand | 2920-3111 | 192 | 5 |
| SwitchMulticamAngleCommand | 3120-3405 | 286 | 5 |
| UpdateClipParamCommand | 3416-3800 | 385 | 8 |
| AddKeyframeCommand | 3808-4041 | 234 | 8 |
| RemoveKeyframeCommand | 4045-4262 | 218 | 7 |
| UpdateKeyframeCommand | 4266-4415 | 150 | 5 |
| SetKeyframeInterpolationCommand | 4424-4551 | 128 | 6 |
| CommandStack | 4558-4727 | 170 | 10 |
| CreateBinCommand | 4747-4846 | 100 | 5 |
| DeleteBinCommand | 4850-5041 | 192 | 5 |
| DeleteAssetCommand | 5131-5332 | 202 | 5 |
| DeleteTimelineCommand | 5336-5445 | 110 | 5 |
| RenameBinCommand | 5449-5542 | 94 | 5 |
| RenameAssetCommand | 5546-5622 | 77 | 5 |
| MoveBinCommand | 5626-5699 | 74 | 5 |
| MoveAssetToBinCommand | 5703-5820 | 118 | 5 |
| MoveTimelineToBinCommand | 5824-5902 | 79 | 5 |
| RemoveAssetFromBinCommand | 5906-5961 | 56 | 5 |
| RemoveTimelineFromBinCommand | 5965-6016 | 52 | 5 |
| DuplicateAssetCommand | 6059-6140 | 82 | 5 |
| UpdateAssetProductionInfoCommand | 6144-6227 | 84 | 5 |
| UpdateProxyPolicyCommand | 6231-6306 | 76 | 6 |
| UpdateAssetLogCommand | 6310-6413 | 104 | 5 |
| UpdateSourcePatchCommand | 6417-6489 | 73 | 5 |
| UpdateAssetInterpretationCommand | 6493-6582 | 90 | 5 |
| UpdateAudioSyncBindingCommand | 6586-6654 | 69 | 5 |
| UpdateClipFpsInterpretationCommand | 6658-6777 | 120 | 5 |
| BatchUpdateAssetLogCommand | 6781-6903 | 123 | 5 |
| CreateSmartBinCommand | 6907-6956 | 50 | 5 |
| DeleteSmartBinCommand | 6960-7004 | 45 | 5 |
| ReplaceMediaCommand | 7012-7097 | 86 | 5 |
| RelinkAssetCommand | 7100-7189 | 90 | 5 |
| BatchRelinkAssetsCommand | 7193-7276 | 84 | 5 |
| UpdateAssetProxyCommand | 7280-7387 | 108 | 5 |
| ImportMediaAssetCommand | 7395-7481 | 87 | 5 |
| MarkBestTakeCommand | 7489-7575 | 87 | 5 |
| ProjectTransactionResult | 7583-7595 | 13 | 0 |
| UpdateTimelineSettingsCommand | 7599-7677 | 79 | 5 |
| ProjectCommandStack | 7680-7837 | 158 | 10 |
| ProjectMacroCommand | 7845-7932 | 88 | 4 |
| SetTrackMuteCommand | 7941-8025 | 85 | 5 |
| SetTrackSoloCommand | 8029-8113 | 85 | 5 |
| SetTrackLockCommand | 8117-8203 | 87 | 5 |
| SetTrackSyncLockCommand | 8207-8302 | 96 | 6 |
| SetTrackVolumeCommand | 8306-8396 | 91 | 5 |
| SetTrackPanCommand | 8400-8489 | 90 | 5 |
| SetTrackStripEQCommand | 8493-8577 | 85 | 5 |
| SetTrackEffectChainCommand | 8581-8659 | 79 | 5 |
| SetMasterVolumeCommand | 8663-8730 | 68 | 5 |
| SetMasterMuteCommand | 8734-8794 | 61 | 5 |
| ApplyChannelStripPresetCommand | 8798-8905 | 108 | 5 |
| ApplyMixSnapshotCommand | 8909-9037 | 129 | 6 |
| SetTrackVisibilityCommand | 9041-9126 | 86 | 5 |
| AddTrackCommand | 9130-9331 | 202 | 5 |
| CaptionEditMode | 9339-9348 | 10 | 0 |
| AddSubtitleClipCommand | 9352-9710 | 359 | 10 |
| UpdateSubtitleClipCommand | 9714-9858 | 145 | 7 |
| MoveSubtitleClipCommand | 9862-10015 | 154 | 5 |
| CopySubtitleClipCommand | 10019-10093 | 75 | 5 |
| DeleteSubtitleClipCommand | 10097-10165 | 69 | 5 |
| DeleteTrackCommand | 10169-10285 | 117 | 5 |
| RenameTrackCommand | 10289-10373 | 85 | 5 |
| SetTrackColorCommand | 10377-10443 | 67 | 5 |
| AddAutomationKeyframeCommand | 10449-10551 | 103 | 7 |
| RemoveAutomationKeyframeCommand | 10555-10640 | 86 | 5 |
| ClearAutomationCommand | 10644-10706 | 63 | 5 |
| SetAutomationKeyframeInterpolationCommand | 10710-10821 | 112 | 6 |
| AddBusCommand | 10827-10875 | 49 | 5 |
| RemoveBusCommand | 10879-11007 | 129 | 5 |
| SetTrackOutputBusCommand | 11011-11061 | 51 | 5 |
| AddSendCommand | 11065-11122 | 58 | 5 |
| RemoveSendCommand | 11126-11189 | 64 | 5 |
| UpdateSendCommand | 11193-11303 | 111 | 5 |
| MoveTrackCommand | 11307-11403 | 97 | 5 |
| InsertLinkedAssetCommand | 11429-11638 | 210 | 5 |
| InsertLinkedAssetRippleCommand | 11642-12062 | 421 | 5 |
| MacroCommand | 12069-12377 | 309 | 7 |
| RippleDeleteCommand | 12385-12776 | 392 | 5 |
| RangeRippleDeleteCommand | 12784-12893 | 110 | 5 |
| SlideClipCommand | 12897-13228 | 332 | 6 |
| BatchDeleteCommand | 13236-13620 | 385 | 5 |
| AddTransitionCommand | 13629-13995 | 367 | 7 |
| RemoveTransitionCommand | 13999-14153 | 155 | 5 |
| UpdateTransitionCommand | 14157-14461 | 305 | 8 |
| InsertClipCommand | 14469-14832 | 364 | 6 |
| OverwriteClipCommand | 14840-15158 | 319 | 5 |
| AppendClipCommand | 15166-15361 | 196 | 5 |
| RollTrimCommand | 15369-15560 | 192 | 5 |
| RippleTrimCommand | 15568-15846 | 279 | 6 |
| AddEffectCommand | 15861-16079 | 219 | 5 |
| RemoveEffectCommand | 16083-16212 | 130 | 5 |
| UpdateEffectParamCommand | 16216-16395 | 180 | 5 |
| SetEffectEnabledCommand | 16399-16564 | 166 | 5 |
| ReorderEffectCommand | 16568-16757 | 190 | 5 |
| CreateColorStackCommand | 16767-16854 | 88 | 5 |
| UpdateColorStackCommand | 16858-17083 | 226 | 5 |
| UpdateClipColorStackCommand | 17087-17214 | 128 | 5 |
| UseSharedGradeCommand | 17222-17348 | 127 | 5 |
| MakeLocalGradeCopyCommand | 17352-17489 | 138 | 5 |
| AddMarkerCommand | 17497-17590 | 94 | 5 |
| DeleteMarkerCommand | 17594-17691 | 98 | 5 |
| MoveMarkerCommand | 17695-17823 | 129 | 5 |
| EditMarkerCommand | 17827-17990 | 164 | 5 |
| ChangeClipFadeCommand | 17998-18207 | 210 | 7 |
| SetClipLabelColorCommand | 18214-18278 | 65 | 6 |
| SetClipWaveformDisplayCommand | 18281-18361 | 81 | 6 |
| SetTimelineRangeCommand | 18368-18421 | 54 | 6 |
| ToggleClipEnabledCommand | 18428-18490 | 63 | 6 |
| SetClipLinkedCommand | 18497-18658 | 162 | 8 |
| ChangeClipSpeedCommand | 18667-18949 | 283 | 3 |
| FreezeFrameAtPlayheadCommand | 18957-19188 | 232 | 6 |
| SetClipBlendModeCommand | 19196-19307 | 112 | 5 |
| ReplaceClipAssetCommand | 19315-19436 | 122 | 5 |
| SetClipAudioGainCommand | 19444-19535 | 92 | 5 |
| UpdateClipSampleFieldsCommand | 19558-19650 | 93 | 5 |

## 附录 B：模块级函数清单

| 函数 | 行区间 | 行数 | 归属建议 |
|---|---|---|---|
| `_move_scoped_markers_with_clip` | 48-84 | 37 | _support |
| `_assert_caption_graphic_track_destination` | 87-99 | 13 | subtitle |
| `_is_pure_caption_unlock` | 102-120 | 19 | subtitle |
| `_ensure_subtitle_clip_editable` | 123-135 | 13 | subtitle |
| `assert_track_editable` | 138-155 | 18 | _support（唯一公开名，edit_controller 直接 import） |
| `_clips_from_dicts` | 158-162 | 5 | _support |
| `_transitions_from_dicts` | 165-171 | 7 | _support |
| `_split_clip_transitions_for_move` | 696-730 | 35 | _support |
| `_validate_source_curve_fps` | 2900-2916 | 17 | _support（clip_props 用） |
| `_record_field` | 5044-5048 | 5 | asset |
| `_detach_asset_from_composition` | 5051-5127 | 77 | asset |
| `_registered_scene_id_for_asset` | 6019-6042 | 24 | asset |
| `_unregister_duplicated_scene` | 6045-6055 | 11 | asset |
