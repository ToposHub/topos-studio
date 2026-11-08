# Resolve 风格剪辑时间线：后端逻辑与宿主接线计划

日期：2026-11-07；收尾验证：2026-11-08。状态：待后续实施。本轮交付范围为时间线 UI 与交互；本文只是后续实现计划，不代表这些能力已经接通。

## 1. 范围与当前边界

本轮没有修改 core、工程 schema、渲染后端或音频后端。现有选择、拖动、修剪、撤销/重做、插入/覆盖/追加仍复用既有模型和命令。T 模式按位置选择 ripple/slip/slide，A/T 模式共享接缝中心以窄 4px 命中区域选择 roll，已经接到既有命令；它们不是本文待开发的后端功能。

必须遵循仓库 [`.clinerules`](../../.clinerules) 与 [Edit 时间线架构](../architecture/edit-timeline.md)：

- 内容修改通过不可变模型及既有 command/service 提交入口；不能只改 controller 缓存、图形 item 或保存文件中的一层。
- UI 只更新状态、发信号；音频采集/处理、解码及 GPU 工作使用既有工作线程和生命周期。
- 一次用户编辑应成为一次可撤销事务；revision 在提交边界推进，预览和取消不推进。失败不得留下部分 clip、asset 或 sequence 更新。
- `.topos` 新字段先定义版本、缺失值、未知字段保留和迁移策略。本轮没有为下述建议预先增加字段。
- 工程不保存设备绝对路径、秘密、临时缓存；UI 状态与媒体内容模型分别定义归属。

### 已实现与尚未接通

| 类别 | 当前真实状态 | 后续范围 |
|---|---|---|
| 三主工具、高级工具菜单、工具栏折叠、Full/Detail/Custom 缩放 | 已实现 UI，调用现有 view 能力 | 保留并回归；如需跨重启记忆，纳入 UI 状态保存 |
| T 模式四区域与接缝 roll | 已复用既有 slip/slide/ripple/roll 命令及取消路径 | 验证后端既有能力的边界，不重写同类命令 |
| 蓝色源 patch 徽章 | 独立于红色 target；只更新 view 内目标轨选择、发 request | 尚未修改 `SourcePatchSpec`，尚未改变真实插入/覆盖的落轨路由 |
| Position Lock | view 内 clip 鼠标拖动的临时保护；不是领域权限，也未持久化 | 确定统一语义后覆盖键盘、粘贴、删除、外部拖入、AI/service 等入口 |
| 缩略图模式、波形开关 | 每个 view 的内存偏好；重建该 view 内 item 时重投影 | 宿主保存/恢复及跨时间线归属策略 |
| 轨高、分区比例、分区滚动 | 使用既有 `TimelineLayoutState`；FileController 已收集/恢复 `timeline_layout` | 验证新滑条走通已有保存链；完善多时间线状态隔离，不能误列为从零建设 |
| 实时电平表 | 保留 `TimelinePanel.update_audio_levels()`，EditPage 已有真实音频管理器信号接线 | 验证面板重新附着、音频管理器重建和页面切换时的订阅生命周期 |
| Replace、Keyframe Tray、Voiceover、彩色 Flag | 按钮/菜单发出请求 | 下表五个新请求均没有实际 host consumer |

## 2. 真实代码入口与数据流

下列文件均已确认存在。行号随本轮 UI 修改可能变化，后续以符号检索为准。

| 层 | 文件与关键符号 | 当前职责及约束 |
|---|---|---|
| 时间线面板 | `src/features/edit/ui/timeline_panel.py`：五个 request signals、`_setup_timeline_menus()`、`_setup_annotation_menus()` | 声明 UI 请求及菜单；仅 `source_patch_changed` 从 view 转发。不可把发出 signal 当作内容操作成功 |
| 时间线视图 | `src/features/edit/ui/timeline_view.py`：`source_patch_track_id()`、`_on_source_patch_requested()`、`set_position_lock_enabled()`、`set_thumbnail_display_mode()`、`set_waveforms_visible()` | 保存 view 内状态。`_execute_command()` 继续负责现有 UI 编辑的历史、revision、增量刷新和工程同步通知 |
| Edit 组合页面 | `src/features/edit/ui/edit_page.py`：`set_timeline_panel()`（原约 396 行）、`_on_timeline_source_insert/overwrite/append()`、`_get_source_edit_params()` | 现已接 Insert/Overwrite/Append，读取 Source Viewer 素材及 In/Out；新五项未接。应在此做 view/page 层桥接，避免面板反查整个主窗口 |
| 应用控制器 | `src/app/controllers/edit_controller.py`：`_execute_timeline_edit_command()`、`_on_timeline_switched()`、录音及关键帧 handlers | 负责应用级编排、活动工程和序列缓存同步。新功能优先复用此层已有事务入口，不另造平行缓存 |
| 项目保存宿主 | `src/app/controllers/file_controller.py`：`_get_edit_ui_state()`（约 1424）、`_restore_edit_ui_state()`（约 4632） | 当前保存 zoom、选择、playhead 和 `timeline_layout`；当前 `scroll_position` 收集为 0，恢复链未恢复本次新增偏好 |
| UI 序列化 | `src/features/edit/core/serialization.py`：`UIState` | 现有字段含 `timeline_layout`，不含 Position Lock、缩略图/波形偏好、zoom preset、源目标轨 UI 路由 |
| 素材级源 patch | `src/features/edit/core/source_patch.py`：`SourcePatchSpec`；`commands/asset_commands.py`：`UpdateSourcePatchCommand` | 定义素材 V/A 流开关及源声道到目标声道映射；**没有目标轨 ID**。不能将蓝徽章选择直接写成已有 spec 的字段 |
| 现有 patch 编辑 | `src/features/edit/ui/source_patch_dialog.py`、`media_pool_panel.py: apply_source_patch()`；`core/source_patch_matrix.py: channel_matrix()` | 现有素材级覆盖及项目命令入口；矩阵映射的是源/目标声道，不是目标轨ID。与新增时间线目标轨路由互补，不是同一状态 |
| 现有替换命令 | `src/features/edit/core/commands/clip_props_commands.py: ReplaceClipAssetCommand` | 用于占位片段替换素材，主要换 `asset_id`，可截短；不等于 Resolve Source Viewer/Record playhead 对齐的 Replace Edit |
| 录音 | `src/app/controllers/edit_controller.py: _start_recording()`、`_stop_recording_and_import()`、`_on_take_recorded()`；`core/recording_manager.py`；`ui/adr_take_sheet_panel.py` | 已有设备/监听/pre-post-roll/录音导入链；Voiceover 应桥接这一套生命周期 |
| 关键帧 | `edit_controller.py: _on_edit_clip_keyframe_toggled()`、`_on_edit_effect_keyframe_toggled()`；`commands/effect_commands.py` | 已有 clip/effect 关键帧命令。Tray 可消费这些能力；不能把 Composite 或 Color 的 keyframe UI 直接绑定到 Edit 数据模型 |
| AI/脚本提交 | `src/features/edit/ai_editing/edit_project_service.py: EditProjectService.commit_command()`；`transaction_service.py` | 使用一致快照、base_revision、领域校验、原子提交和 ref↔sequence 同步；统一锁策略需覆盖此路径 |

建议整体链路：`TimelinePanel request → EditPage 明确上下文/转发 → EditController 编排 → 现有命令或后续新增领域命令 → 既有提交边界 → 统一模型发布 → UI 回写`。显示/展开类请求无需创建内容命令。

## 3. 后续任务总表

估时为熟悉项目的一名开发者的净开发人日，含 focused tests，不含人工评审排期及设备兼容排障。表中“新增”均是建议，不是当前已有符号。

| ID | 优先级 | UI 入口 | 现有状态 | 建议接收层与命令入口 | 数据契约 | 实现步骤 | 验收 | 依赖与估时 |
|---|---|---|---|---|---|---|---|---|
| W01 | P1 | `source_replace_requested()` | 只发请求，主窗口无消费者；按钮 enabled 目前沿用源 In/Out 可编辑状态 | EditPage 读取源与目标，EditController 执行；新增明确 Replace Edit 事务，复用 `_execute_timeline_edit_command()`；AI 经 service | 源 asset/源定位帧/源 fps；目标 timeline/clip/track/record 帧；linked policy；base revision；范围策略 | 定义 Replace 语义→解析目标→源区间预检→成对 A/V 命令→单次提交→刷新/失败反馈 | 成功单步 undo；目标时长、邻片位置及保留效果满足约定；源不足无半替换 | 依赖 W00、W06 策略；2–4 日 |
| W02 | P1 | `source_patch_changed(track_type, track_id, enabled)` | 只改 view 内蓝徽章；不改红 target，不改真实路由 | EditPage/Controller 的源编辑上下文；插入/覆盖/追加/拖放及 source edit plan 共用解析器；素材流/声道继续用 `UpdateSourcePatchCommand` | 类型仅 video/audio；目标轨存在且类型匹配；`enabled=False` 表示禁用该源流路由；携带 timeline/source 上下文；区分“无显式覆盖”和“显式关闭” | 确定状态归属→加入路由上下文→编辑计划统一消费→host 回写徽章→保存/恢复→锁轨/轨删除处理 | 蓝徽章选 v2 真正落 v2；关闭 V 留 A；红 target 可独立；undo/source 切换后状态一致 | 依赖 W00、W06、W07；2–4 日；若扩多源声道徽章另估 |
| W03 | P2 | `keyframe_tray_requested(visible)` | 仅按钮 check 和请求；无 Tray 宿主 | EditPage 管理 Edit Tray，Controller 复用 clip/effect keyframe handlers；已有 Add/Remove/Update/Interpolation 命令 | 可见 bool；选中 clip/track/param/effect；时间坐标明确是 timeline frame 还是 clip/source local | 建立 tray host→selection/playhead 投影→参数轨→命令桥接→undo/reload刷新→关闭回写按钮 | 不切换到别域；移动/修剪后点位正确；拖点单步 undo；隐藏不改内容 | W00、W07；基础参数轨 2–4 日，曲线编辑另估 |
| W04 | P1 | `voiceover_requested()` | 只发请求，未启动录音 | EditPage/Controller 打开并配置既有 ADR/Voiceover 流程；复用 `_start_recording()`、stop/import/take transaction | timeline/project/目标音轨 ID、设备及通道选择、record anchor、fps、sample_rate、pre/post roll；会话 ID | 选目标音轨→展示设备/录制状态→用户启动→复用采集→停止/取消→原子导入落轨→清理 | 正确时间码落轨；停止一次只导入一次；取消无孤儿；切工程不投旧工程；单步撤销 | W00、W06；1.5–3 日；真实设备回归另留 0.5–1 日 |
| W05 | P2 | `flag_requested(hex_color)` | 选择颜色或按钮主操作发字符串；清除发 `""`；没有 clip flag 模型/消费者 | Controller 解析目标；建议新增 clip 颜色旗标模型/命令后走 timeline 提交入口；若采用 asset 级方案需单独设计项目事务 | `#RRGGBB` 规范色或 `""`；目标 clip 集、timeline/revision；单色或多旗标策略先定 | 明确作用域→schema/兼容设计→新增命令/序列化→图形投影→过滤/列表一致→测试 | 标记/清除均可撤销；重开保留；无选中/多选/linked规则明确；旧包可读 | W00、schema决策；1.5–3 日；多色列表另估 |
| W06 | P1 | Position Lock 按钮/view setter | 仅部分 clip 鼠标位置操作 guard；不是持久化或 service 约束 | 共享编辑策略解析/提交预检；Controller/UI 命令入口及 AI/service 同一策略；不只加按钮判断 | 按 timeline/session 的 policy + revision/version；区分 move、trim、roll、slip、ripple、add/remove、metadata | 锁语义矩阵→内容变更 footprint→统一预检→各入口接入→失败状态反馈→设置保存→混合事务原子拒绝 | 鼠标/键盘/粘贴/删除/外部拖放/AI一致；允许操作可执行；被拒绝不改revision/history | W00；3–5 日，涉及 service 策略扩展需独立评审 |
| W07 | P2 | View Options、轨高滑条、缩放及蓝徽章 UI状态 | 轨高已有 layout 保存链；新显示偏好只有 view 内存；水平 scroll 收集目前为0 | FileController + UIState/配置层，必要时增加有版本 UI payload；Controller 切timeline时保存恢复 | project/timeline key；thumbnail enum、waveforms bool、zoom preset/custom zoom+scroll、position lock、routing UI；非法值回退 | 明确全局/项目/时间线归属→扩UI payload→保存→加载后恢复→控件同步→旧包/多timeline测试 | 重开/切timeline恢复正确；不把显示偏好写入clip；恢复零额外命令；未知字段可重存 | W00、W02/W06契约；1–2.5 日 |
| W08 | P2 | 电平表、Mixer、Edit/Deliver共享面板 | 真实 `audio_level_changed → update_audio_levels` 已有 | EditPage 音频管理器/面板附着生命周期；复用 `_reconnect_audio_mixer_signals()` 模式 | 通道dB数组及主线程交付；manager/session generation；停止清零策略 | 核实订阅顺序→去重/断开旧manager→页面及项目切换→真实信号回归 | 无重复连接/旧manager回调；meter随真实音频，单声道/多声道正确；静音及暂停一致 | 无需重写meter；0.5–1 日 |
| W09 | P1 | 所有新增请求 | 均未接host；本次可review UI状态已成立 | 建立集中bind/unbind，复用EditPage/SignalHub/Controller当前边界 | bind generation、当前panel/source/project上下文、明确结果/错误回写 | 一次连接→替换面板时断旧连新→提供成功/取消/拒绝→按钮状态由实际host回写 | 重开页面/多次附着不重复执行；无上下文不操作；失败后按钮无假成功 | W00；0.5–1 日，可与各功能合并 |

W00 为实施前的契约确认，建议 0.5–1 日：确认 Replace 对齐规则、蓝徽章路由归属、Position Lock 的允许/拒绝矩阵及彩色 Flag 的作用域。建议顺序：W00/W09 → W02 → W01/W04 → W06 → W03/W05 → W07/W08 → 集成回归。可先将 W06 最小语义定稿，再让其他任务实施，避免事后改变写入边界。

## 3.1 W00 契约决策（2026-11-08 定稿，后续实施按此执行）

### D1 Replace Edit 对齐规则（W01 依据）

1. **目标解析顺序**：恰好一个选中 clip → 用它；无选中 → 播放头帧下、位于"有效视频目的轨"（W02 蓝路由，无显式路由时退红 target）上的 clip；两者都无 → 拒绝并在状态栏报告。多选（>1）不隐式批量替换，先拒绝并提示。
2. **源区间**：Source Viewer 的 In/Out 有效时，源对齐帧 = In（inclusive）；In/Out 缺失时 Replace 按钮不可用（与 Insert/Overwrite/Append 同一 enabled 判定）。Out 不决定目标时长。
3. **保留项**（Replace 不改）：`clip_id`、`track_id`、`timeline_start`、`timeline_duration`、`speed`、效果/grade/动画/淡变/`label_color`/链接关系/增益/混合模式等其余字段。**变更项**：`asset_id`、`source_start`（= 源对齐帧经 fps 映射）、`source_duration`（按目标 timeline_duration × 有效速度换算，混合帧率沿用统一时长 helper）。
4. **源不足预检**：素材可用源帧（源时长 − 源对齐帧）< 所需 source_duration → 整次拒绝并报告，不截短目标。
5. **Linked A/V 配对**：目标 clip 存在 `linked_clip_id` 伙伴、伙伴仍存在、且新源为有声视频时，两个 clip 在一个宏事务中一起替换（同一源对齐帧，单步 undo）；否则只替换目标 clip（伙伴保持），状态栏说明。任一伙伴锁轨、源不足或素材缺失 → 整次拒绝。
6. **提交**：新增领域命令 `ReplaceClipWithSourceCommand`（成对时包 `MacroCommand`），经 `TimelineView._execute_command` 提交（revision 在提交边界推进）；既有 `ReplaceClipAssetCommand`（占位替换）保持原职责不变。

### D2 蓝徽章路由归属（W02 依据）

1. 蓝徽章是**时间线范围的源目的轨 UI 上下文**：按 `(timeline_id, track_type)` 记录 `track_id`（显式选中）、`None`（显式关闭该流）或"无条目"（从未设置，旧工程）。不写入 `SourcePatchSpec`（那是素材级流开关/声道映射），两者互补。
2. **有效目的轨解析**（`resolve_source_destination_track_id(type)`）：显式选中且轨仍存在 → 该轨；显式关闭 → `None`（该流不落）；无条目或轨已删（陈旧条目即清理）→ 回退红 target 语义（现有 `get_target_track_id()/get_target_audio_track_id()`）。红 target 逻辑不动。
3. **消费入口**：源 Insert/Overwrite/Append、媒体池拖放（`_add_asset_as_clip` 与 insert-mode drop）统一经上述解析器取落轨；素材级 SourcePatch 流开关判定保持不变，任一层关闭该流即不落。
4. **持久化**：经 W07 的 UIState presentation payload（`source_destination_ui`）保存/恢复；因不是领域状态，undo/redo 不影响蓝路由。
5. 轨删除/undo 后：解析器按轨存在性回退，`load_timeline` 重建时徽章重投影（既有行为），不产生悬空 ID。

### D3 Position Lock 允许/拒绝矩阵（W06 依据，最小语义定稿）

锁 = 冻结 clip 的时间线足迹 `(track_id, timeline_start, timeline_duration)`；开启后对所有 clip 生效（会话级防误操作，非持久权限、非协作权限）。

| 操作族 | 锁开启语义 |
|---|---|
| Move/Slide/跨轨移动/Alt-copy 放置 | 拒绝（足迹变化） |
| 常规 trim（左/右）、roll | 拒绝（trim 改 start/duration，roll 改两边界） |
| Slip、效果/音量/gain/颜色/label/flag/enabled/重命名 | 允许（足迹不变） |
| Delete/Cut、ripple delete/insert、paste/overwrite/外部拖放到锁 clip 范围 | 拒绝（移除锁 clip、推动其足迹、或拆分缩短锁 clip） |
| 纯新增 clip（不触碰既有 clip 足迹） | 允许 |
| Undo/Redo | 允许（恢复用户历史优先，跳过预检） |

**判定机制**：提交预检基于内容变更 footprint——命令先对当前时间线 dry-run `apply`（命令纯数据、apply 返回新实例不原地改），diff 前后所有既有 clip 的足迹；任一 clip 足迹变化或被移除 → 拒绝；新增 clip 不算违规。命令套件的 apply 快照在输入不变时幂等，dry-run 后的正式 apply 不改变结果。
**接入点**：`TimelineView._execute_command`（覆盖鼠标/键盘/菜单/拖放全部 UI 编辑）与 `EditProjectService.commit_command`（AI/service，显式传入锁定策略）。拒绝时零 revision、零 history、状态栏反馈。

### D4 彩色 Flag 作用域（W05 依据）

1. **clip 级单色旗标**：`ClipInstance.flag_color: str | None`（`"#RRGGBB"` 白名单 7 色或 None=无旗标）。独立于 `MediaAsset.flag`（none/reject 素材级）与 `ClipInstance.label_color`（片段配色）。
2. 多选 → 全部选中 clip 设同一色（宏事务单步 undo）；无选中 → 播放头下 clip；都无 → 状态栏提示、零写入。清除（`""`）→ 置 None。
3. 不随 linked 伙伴传播；复制/split/Replace 经字段保留契约自动继承。
4. **schema**：带默认 `None` 的新字段，旧工程缺字段 → None（零迁移）；`to_dict` 仅非默认时写出；未知颜色值读取保留、UI 绘制 fallback 默认色。UI 旗标徽章与筛选从提交后的模型读取。
5. 写入采用新命令 `SetClipFlagCommand`（clip_ids 元组 + 颜色，快照 undo）。

### D5 Keyframe Tray 宿主归属（W03 依据）

EditPage 是隐形协调者（实际面板都挂在 dock 上），Tray 由 **TimelinePanel 持有并托管在时间线视图下方**（惰性创建、默认隐藏），EditPage 绑定 `keyframe_tray_requested` → `panel.set_keyframe_tray_visible(bool)`，实际可见性/当前 clip 由 host 回写工具栏按钮。参数轨首版覆盖 clip 级 `animated_params`（效果参数轨留待后续）；命令复用既有 Add/Remove/Update/Interpolation keyframe 命令，拖动释放合并为一次 Update，Esc/切换选区取消。

## 4. 五个请求的详细契约

### W01：Replace Edit

1. 明确无参信号只表达“当前上下文执行 Replace”，接收时读取最新 Source Viewer 和时间线快照；不能在工具栏创建时缓存 clip/source。
2. Source Viewer 当前 `_get_source_edit_params()` 返回源 `asset_id` 及 inclusive In/Out，适用于已有 Insert/Overwrite/Append。Resolve Replace 通常涉及源/时间线定位帧对齐，不能假设只把它接到现有 overwrite 就等价。
3. 推荐先实现单个目标 clip：明确选中优先或 record playhead 下目标优先、target track优先级及无目标时拒绝。多选 Replace 单独规定；不要隐式把所有选中 clip 换同一素材。
4. 以目标有效时长和源对齐帧计算 source start/end；Rational fps 换算、变速/反放、源不足及 linked A/V 的配对约束先预检。源不足建议拒绝并报告，不默默截短目标。
5. 分别定义保留项：clip ID、timeline start/duration、轨道、效果/grade/动画/淡变/label/链接；定义源音频布局及媒体类型改变时的兼容规则。
6. 现有 `ReplaceClipAssetCommand` 用于占位替换，可能截短时长，仅替换素材引用；它没有完整 record/source 对齐语义。可复用其不可变替换/undo结构思路，不能直接宣称它就是 Replace Edit。
7. 先构造包含所有 A/V 修改的事务，再提交一次；任一伙伴锁轨、源范围不足、素材不存在或 revision 冲突时整次拒绝。成功统一更新序列缓存、ref、预览及音频图。

### W02：蓝色源目标轨与现有 SourcePatchSpec

当前 signal 的数据不足以单独恢复完整路由上下文。接收端必须补齐当前 project/timeline/source asset 标识，并检测事件触发后活动上下文是否已变化。

| 状态 | 当前含义 | 建议消费与保存策略 |
|---|---|---|
| 红色 target track | 既有编辑目标 | 保留原逻辑，不能因蓝徽章切换而自动修改 |
| 蓝色 V1/A1 徽章目标轨 | view 内一个视频/一个音频源目的轨 | 建议定义独立 `SourceDestination` 上下文；名称是建议，当前尚无此类。单独记录 `video_track_id/audio_track_id` 及 enabled 状态 |
| `SourcePatchSpec.video_enabled/audio_enabled` | 素材级源流开关 | 既有 `UpdateSourcePatchCommand` 管理。蓝徽章关闭能否修改素材级开关，须明确产品语义；推荐目的轨UI状态先保持独立 |
| `audio_channel_map` | 源声道索引 → 目标轨内声道索引 | 不是目标轨映射。多目标音轨路由若需要，另加领域契约和迁移，不能向此 tuple 塞 track ID |
| 缺少路由字段 | 默认行为 | 旧工程默认回退既有 target 语义，兼容当前编辑；明确关闭必须使用独立 sentinel/flag，不能与“无覆盖”混淆 |

插入/覆盖/追加、媒体池拖放、外部文件导入后落轨应读取同一份有效源编辑上下文；决定导入工具是否使用蓝路由或显式鼠标落点时，也需在计划层统一优先级。轨删除、undo、换源、源无视频/音频时回写所有 header。UI显示接收结果后才成为“真实路由”，否则只作为当前请求投影。

### W03：Keyframe Tray

- bool 只控制期望可见性。宿主应返回实际 visible、当前clip及可编辑参数；用户关闭Tray或当前选择无效时反向同步按钮，使用 signal blocker 避免递归。
- 建议 EditPage 持有独立 Edit Tray；Composite 的 `keyframes_panel.py`/`keyframe_editor.py`、Color 的 `color_keyframes_panel.py` 是参考，不直接混接其数据域。
- 已有 clip/effect keyframe commands 和 Controller handlers 可复用。先写明 timeline frame、clip local frame、source frame 的转换，特别覆盖 clip 左修剪、非零source_start、变速/反放与效果局部时间。
- 关键帧拖动为预览，释放后合并为一次 Update 命令；Esc/选区切换/关窗口取消，不能每像素入历史。选择、参数、playhead变更仅重投影，不产生写入。
- 任何曲线缓动/插值新增能力需声明对应预览/导出消费者，避免只在Tray显示插值而渲染仍用旧值。

### W04：Voiceover

优先打开已存在 ADR/录音设置入口，不要把无参 toolbar click 直接等同于“立即采集默认麦克风”。目标音轨取当前有效音频目标或用户明确选择；无轨时通过既有新增轨命令。

复用 `_start_recording()` 的 RecordingConfig、设备、输入监听、pre/post-roll、时间码和已有 take completion callback。`_on_take_recorded()`/既有录音导入事务负责 asset 和 clip 的一致提交；停止、post-roll完成、窗口关闭不得产生重复 take。

录音会话需要绑定 project ID、timeline ID、track ID 和起始时间上下文。异步完成时重新验证目标仍有效，尤其是录制中切工程/切时间线/删轨；拒绝旧上下文回调时说明文件保留位置并清理会话，不把 take 投到新工程。采集失败、设备拔出、取消和应用退出路径都停止监听、释放设备和定时器。

### W05：彩色 Flag

当前 `MediaAsset.flag` 合法值只有 `none/reject`（见 `core/project.py: ASSET_FLAG_VALUES`）；这不是颜色旗标。现有 `ClipInstance.label_color` 负责片段配色，也不应被旗标按钮改写。Signal 为七种菜单 hex 或空字符串，并未包含 clip 身份。

推荐定义 clip 级独立旗标数据；是否允许多个颜色、是否跟随 linked伙伴、复制/嵌套/多机位/Replace时如何保留先确定。若选择 Resolve式素材级旗标，则必须明确同一asset所有实例一起变化，走项目命令并刷新全部时间线；不可悄悄从单clip按钮升级为全素材修改。

后端新增前先做 schema 决策：颜色白名单/规范化、空值语义、去重/排序、未知未来值保留与运行时显示fallback。写入采用 Set/Clear 命令，批量选中用宏事务；未选中时按明确规则选择playhead下clip或提示。UI flag badge、列表、筛选均从提交后的模型读取。

## 5. Position Lock 一致约束方案

当前实现是方便的鼠标交互保护，没有对所有编辑命令施加领域锁。后续不能以 toolbar checked 状态充当 AI、脚本或服务权限来源。

建议定义按时间线生效的编辑策略，先确定以下矩阵。下表是待确认的建议，不是当前保证：

| 操作族 | 建议锁开启语义 | 必查入口/边界 |
|---|---|---|
| Move/Slide、跨轨移动、Alt-copy放置 | 拒绝时间/轨道位置改变 | 鼠标、键盘nudge、菜单、command replay、AI/service |
| Ripple trim/delete/insert | 先按受影响clip集合检查，避免间接推动被保护内容 | ripple followers、sync-lock轨、linked A/V、宏事务 |
| 常规trim、roll | 明确“锁位置”是否允许调整边界；UI与service必须采用同一决定 | 左trim改变timeline_start，roll改变伙伴边界，不能仅按command名称判断 |
| Slip、效果、音量、颜色/Flag | 通常允许不改变时间/轨道位置的编辑 | 仍遵守轨道锁、有效源范围及其他领域约束 |
| Paste/Overwrite/Replace/外部拖放 | 明确允许新增还是拒绝，检查覆盖/拆分对已有clip的影响 | 素材池、Finder拖入、批量导入、三/四点编辑 |
| Delete、Cut | 明确是否禁止移除；Ripple与普通删除分别规定 | 菜单/快捷键/脚本共用策略，不只禁用按钮 |
| Undo/Redo | 推荐可恢复用户历史，并明确是否跳过临时锁或按历史意图受控执行 | 不能让锁造成“undo栈已pop但模型未恢复”；先验证再改变历史 |

建议提交预检基于内容变更 footprint（哪些clip的轨道/start/end被改变或移除），而非仅维护容易遗漏的命令黑名单。UI guard负责及时反馈，共享提交策略负责最终一致性；AI/service接收显式context/policy及base_revision，禁止由客户端随意绕过。锁是协作权限还是个人防误操作要分清：若只是个人偏好，应明确服务从哪个会话获得策略，避免保存工程后意外把个人锁当全局权限。

## 6. UI 状态保存与真实音频订阅

### 保存/恢复

现有 FileController 已保存/恢复 `timeline_layout`，因此轨高滑条应先用现有 `.topos` 往返验证。不要为了新滑条新增第二套轨高字段。

新增偏好建议集中为有版本的时间线展示payload。字段草案：`thumbnail_mode`、`waveforms_visible`、`zoom_preset`、`custom_pixels_per_frame`、`custom_scroll`、`position_lock`、`source_destination_ui`。这些仅为建议名称；若路由已升级为真正领域状态，其真值不能继续只保存在UI payload。

宿主需明确全局默认、项目覆盖与时间线覆盖顺序。恢复顺序建议：加载project/sequence→layout配置→读取并校验presentation→应用zoom/scroll→选区/playhead→同步菜单/按钮。恢复使用状态锁，不触发内容命令，不因为恢复偏好推进timeline revision。保存仅标识符和可重建数值，不保存Qt对象或设备路径。

旧payload缺字段使用当前默认；未知字段保留以支持未来版本重存；损坏enum/bool/数字、已删除轨ID、无效clip选择安全回退并诊断。多timeline切换前保存旧状态，载入后恢复新状态，避免一个view的内存偏好误充所有timeline的已持久化状态。

### 电平表

`EditPage` 惰性创建 AudioPlaybackManager 时，已经把 `audio_level_changed` 接到 `TimelinePanel.update_audio_levels()`。本次保留这一链路，后续不需要重写meter、音频mixer或生成模拟动画。

补测重点是时序：先创建manager再附着panel、先附着panel再创建manager、重新附着panel、重建manager、Edit/Deliver共享面板、项目关闭与再次打开。旧对象断开、新对象连接应幂等；验证通道dB数组在UI线程消费，暂停/静音/结束的清零或峰值保留行为与既有mixer一致。只保留一个真实信号来源，不在UI定时器伪造电平。

## 7. 接线示意（伪代码，不是现有可运行接口）

```text
attach(panel, page, controller):
    detach_previous_panel_connections()
    bind panel.source_replace_requested -> page.resolve_current_replace_context
    bind panel.source_patch_changed     -> page.request_source_destination
    bind panel.keyframe_tray_requested  -> page.set_edit_tray_visible
    bind panel.voiceover_requested      -> controller.open_voiceover_setup
    bind panel.flag_requested           -> controller.request_clip_flag

request_replace:
    capture latest project/timeline/source/selection/playheads/revision
    validate context + edit policy + linked source ranges
    build one immutable edit transaction
    submit via existing UI command boundary (AI: service + base_revision)
    publish committed model and result; on cancellation/failure publish no content

source_destination_request:
    validate timeline/type/track/source
    update the agreed host context or domain transaction
    repaint all source badges from accepted state
    keep the red target independent
```

以上接收函数名均为说明用建议，不表示代码中已存在。每项需提供明确成功/取消/错误及状态回写；布尔按钮不能长期显示“已打开”而宿主实际上未创建面板。工具栏的enabled状态也需由host根据源和目标能力决定，不能仅因有In/Out就启用所有新操作。

## 8. 真实素材与复验入口

现成fixture：[`artifacts/timeline_resolve_ui/resolve_ui_review.topos`](../../artifacts/timeline_resolve_ui/resolve_ui_review.topos)。生成/预览工具：[`tools/preview_resolve_timeline.py`](../../tools/preview_resolve_timeline.py)。默认用仓库中的真实视频构建独立review工程；它不是运行中编辑器的后端接线宿主，不能用该预览窗口证明五个新请求已实现。

```bash
cd /Users/heng/Documents/vscode/topos-studio
.venv/bin/python tools/preview_resolve_timeline.py
.venv/bin/python tools/preview_resolve_timeline.py --save-project artifacts/timeline_resolve_ui/resolve_ui_review.topos --project-only
```

生成命令会重写同名review fixture；需要保留原review结果时改用另一个输出文件名。自带fixture在工程索引与媒体池中均使用 `../../tests/test_videos/` 相对素材路径，保持仓库目录结构即可随仓库移动；使用其他外部素材时按既有素材定位/重链接流程处理，不用修改业务schema。

本轮UI交互回归入口：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/edit/test_resolve_timeline_interactions.py tests/edit/test_timeline_resolve_toolbar.py
```

后续接线应补独立host/领域集成测试，不能只保留按钮发signal测试。

2026-11-08 收尾记录：相关现有与新增回归共 334 项通过、1 项跳过；图标及文案更新后，两个新增测试文件共 31 项再次全部通过。已通过真实素材预览检查、Python 编译与 `git diff --check`，测试工程保存/加载往返验证为 2 个素材、1 条时间线、20 个片段，素材路径全部可解析。这些结果不代表五个 request-only 能力已经完成宿主接线。

参考：[Blackmagic Design 官方 Edit 页说明](https://www.blackmagicdesign.com/products/davinciresolve/edit/)，以及本机打开的 DaVinci Resolve Studio 20 剪辑页。颜色继续使用 Topos 既有设计令牌。

## 9. 手动验收与自动化矩阵

| 场景 | 手动操作 | 自动化建议 | 通过条件 |
|---|---|---|---|
| 新请求消费与生命周期 | 多次切Edit/Deliver、关闭重开工程后各点一次按钮 | 假host计数、bind/unbind替换面板测试 | 每次只执行一次；旧panel/manager不响应；失败有反馈 |
| Replace正常/失败 | 选中真实A/V clip，切新源定位后Replace；再选源不足/锁轨目标 | source/record对齐、fps换算、linked宏事务、revision冲突测试 | 无邻片漂移；一次undo恢复；失败无半替换 |
| Source目的轨 | 蓝V1选v2、红target保留v1；执行insert/overwrite；关闭视频再插 | 对四个编辑入口参数化，验证实际新clip.track_id及流 | 真正按蓝路由落轨；目标独立；关闭流无相应clip |
| Patch删除/undo/切源 | 选空v2后删轨/undo，换只有音频/视频源 | 路由解析fallback及所有header状态测试 | 徽章与真实有效路由一致，无悬空ID |
| Keyframe Tray | 打开/关闭、换选clip、拖点/取消、左修剪/变速后复查 | 坐标契约、命令合并、host关闭回写及selection生命周期测试 | 模型、Tray、属性面板、预览与导出一致 |
| Voiceover | 选设备/音轨，录制停止；取消；录制中换timeline/工程 | 复用record manager替身，注入重复完成/迟到回调；真实设备smoke | 只一take，正确起点/声道；资源释放；旧会话不写新工程 |
| Flag | 单选/多选设色清除、undo/redo、重开包 | 模型命令、schema往返、旧包缺字段、未知颜色兼容 | 与clip配色/asset reject独立；所选作用域一致 |
| Position Lock | 锁后鼠标/快捷键nudge/cut/delete/paste、Finder拖入、AI edit | 按操作族及入口双参数化，宏事务中混合允许/拒绝 | 一致拒绝或允许；拒绝零revision/零history；undo语义明确 |
| 展示状态 | 修改filmstrip/波形/轨高/zoom，保存重开，切两条timeline | UIState往返、非法字段、旧包、多timeline隔离测试 | 状态按归属恢复，无额外内容命令；轨高复用现有layout |
| 电平表 | 播放真实音频，静音/暂停/循环，切页面/项目 | manager attach顺序、重复连接、旧manager回调、单/多声道测试 | 来源真实；没有重复更新/销毁对象调用；沿用原meter表现 |
| 既有编辑不回退 | A/T/B、linked、snap、四区域trim、Esc、undo、窄窗口往返 | 继续运行现有UI/trim/linked/transport用例 | 本次UI行为保留；没有另造后端分支 |

现有可复用测试文件包括：`test_source_patch_model.py`、`test_source_patch_ui.py`、`test_source_patch_consumption.py`、`test_properties_keyframe_wiring.py`、`test_effect_keyframe_coordinate_contract.py`、`test_record_arm_wiring.py`、`test_recording_import_transaction.py`、`test_adr_take_chain.py`、`test_round4_media_replacement.py`、`test_subtitle_layout_persistence.py`，均位于 `tests/edit/`。它们覆盖的是既有能力，仍需为新增toolbar→host链路补测试。

## 10. 后续完成标准

每个request从真实toolbar入口触发并由host消费；内容变更经过可撤销、revision一致的提交路径；成功后显示模型真值，失败/取消不留下局部状态；重开工程和切timeline恢复约定状态；音视频预览/导出语义一致。单纯连接signal或让按钮变色不能视为完成。

后续实施前应把 W00 决策记录在本文并更新任务状态。涉及schema、领域策略和新增命令的任务单独审查和分批合并；本轮UI成果不依赖这些任务先落地。
