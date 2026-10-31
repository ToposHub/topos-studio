# 剪辑页面第五轮全功能复查与优化计划表

日期：2026-10-20
基线：`4e4c8b461`（分支 `fix-ui`）
项目根：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01`
范围：剪辑页（Edit）从建工程、导入素材、Source/Program 监视器、时间线编辑、属性/效果、音频、字幕、场景检测、AI 助手、保存到导出的完整链路。

本轮是独立复查，不重复第四轮已关闭的 N/H/F 项。第四轮记录保留在 [edit_page_fourth_audit_fixes_2026-10-16.md](edit_page_fourth_audit_fixes_2026-10-16.md)，本轮新增问题以 `E` 编号记录。

本轮只新增计划文档与审计证据，不修改业务代码、既有测试或用户素材。

---

## 0. 结论摘要

本轮用完整 `MainWindow`、真实 2K 素材和真实 Qt 事件重新走了一遍剪辑主链路，并执行了两批定向回归（合计 311 次执行，去重后约 294 项，全部通过），同时完成一次真实 48 帧导出。**测试绿灯没有覆盖宿主接线**：本轮发现的 22 组问题中，多数是“控件/信号已经存在，但宿主没有连接或重复提交”，因此局部单测全部通过、用户操作仍然失效。

当前最需要优先处理的 5 项：

1. **媒体池右键“添加到时间线”对有声视频重复追加音频**：存在音频目标轨时，第二次追加必定抛 `ValueError: New clip overlaps`。操作表面上已插入素材，但槽内异常没有用户反馈。
2. **属性面板的关键帧按钮全链路断裂**：普通片段参数的 ◆ 信号没有任何接收者；效果参数的 ◆ 虽然被接收，但命令门面缺少 `AddKeyframeCommand`，点击直接抛 `KeyError`。
3. **编辑/时间线菜单的 Cut/Copy/Paste/Split/Insert/Overwrite/Append 七项命令没有回调**，被 `MenuBuilder` 静默跳过；同时 `Ctrl+Shift+X`（Split 默认键）会执行 Cut。
4. **时间线 18 处错误反馈全部丢失**：`TimelineView.status_message_requested` 没有任何接收者，锁轨、撤销事务拒绝、Slip/Slide 拒绝等错误用户看不到。
5. **音频自动化录制永远不会启动**：`_record_fader_sample()` 依赖 `EditPage._is_playing`，而该属性在 `EditPage` 中从未定义。

问题数量：**P1 5 项、P2 10 项、P3 7 项，共 22 项**。

---

## 1. 审计方法与证据等级

| 等级 | 含义 | 本轮使用 |
|---|---|---|
| G | 完整 `MainWindow` + 真实素材 + QTest 键盘/鼠标/按钮事件 | 主链路、菜单、快捷键、属性、播放、保存 |
| Q | 真实 Qt 控件/信号，但宿主或对话框为受控替身 | 信号接收者计数、局部交互 |
| M | 真实模型/命令/IO，不含完整窗口 | 时间线命令、保存/加载、导出 |
| S | 源码调用链与静态检索 | 断线信号、缺失回调、未消费偏好 |
| T | 现有 pytest | 311 项定向回归 |

环境与素材：

- macOS darwin 24.3.0 x64，Python 3.12.13，Qt 运行时 6.10.2 / 编译 6.10.0，窗口 1440×1000。
- 真实素材：`tests/test_videos/2K_h264.mp4`，1920×1080、25 fps、1572 帧、62.89 s、AAC 2 声道。
- 隔离配置与临时工程，未改写用户既有工程和测试素材。

动态证据（详见 `docs/audit/2026-10-20-edit-fifth-review/records/`）：

| 记录 | 结果 |
|---|---|
| 导入 | 1 个素材，1572 帧，25 fps，2 声道 |
| 时间线 | 自动建 1 条 V1 + 1 条 A1，素材追加为 1 V + 1 A 互链 |
| 播放 | 2.5 s 内播放头 0→66 帧 |
| 分割/撤销/重做 | `Ctrl+B` 后 1V+1A→2V+2A，Undo→1V+1A，Redo→2V+2A |
| 复制/粘贴 | 2V+2A→3V+2A（单片段选择，粘贴不复制未选中的链接音频，链接字段被清空） |
| 标记/转场 | 标记 1 个；Cross Dissolve 添加成功，Undo 后清零 |
| 属性 Transform X | 0→55→Undo 0 |
| 保存/加载 | `.topos` 4,219 字节；重载 1 素材、1 时间线、1 序列 |
| 导出 | 48 帧、640×360、H.264 VideoToolbox、AAC 48 kHz 双声道、1.94 s、50,900 字节 |

定向回归：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q \
  tests/edit/test_media_pool.py \
  tests/edit/test_media_pool_batch_delete.py \
  tests/edit/test_media_pool_analysis_ui.py \
  tests/edit/test_edit_page_playback_preview.py \
  tests/edit/test_end_to_end_playback_real_videos.py \
  tests/edit/test_export_outcome.py \
  tests/edit/test_export_manifest.py \
  tests/edit/test_export_video_effects_preflight.py \
  tests/edit/test_caption_workspace.py \
  tests/edit/test_caption_timeline_integration.py \
  tests/edit/test_scene_cut_dialog_formatting.py \
  tests/edit/test_scene_cut_session_memory.py \
  tests/edit/test_edit_effects_panel_drag_inject.py \
  tests/edit/test_edit_keyframe_effect_commands.py \
  tests/edit/test_shortcut_preset.py \
  tests/edit/test_shortcut_scope_gates.py \
  tests/edit/test_timeline_context_menu_guards.py
```

另一批 69 项覆盖 `test_edit_keyframe_effect_commands`、`test_timeline_command_regressions`、`test_shortcut_scope_gates`、`test_effect_keyframe_coordinate_contract`、`test_scene_cut_session_memory`、`test_timeline_context_menu_guards`。两批合计执行 **311 项，0 failed**；其中有 4 个文件重复执行，去重后约 294 项，不把 311 当作唯一用例数。

全量 `tests/edit` 本轮未重新跑（收集为 10,626 项）；上一轮记录的 10,606 项通过只作为历史基线，不作为本轮新证据。

---

## 2. 全链路功能检查矩阵

| 阶段 | 本轮检查 | 结果 | 对应问题 |
|---|---|---|---|
| 启动与页面装配 | 完整 `MainWindow`、Edit dock、Media Pool/Timeline/Program/Source/Properties | 通过 | — |
| 新建工程 | `_auto_create_edit_project`、空时间线的 provisional V1/A1 | 通过 | — |
| 导入素材 | 真实 2K H.264 导入、异步探测、素材元数据 | 通过 | — |
| 媒体池右键“添加到时间线” | 调实际 handler | **失败**：重复追加音频，抛重叠异常 | E01 |
| 媒体池图片“Open Asset Grade in Color” | 信号接收者 | **无接收者** | E08 |
| Source Viewer | 打开素材、播放、IN/OUT、插入/覆盖/追加按钮接线 | 源码可见；本轮未逐项做真实鼠标验收 | — |
| Program Viewer | 播放头推进、帧请求、时间码/叠层 | 播放头推进；未做 GPU 像素断言 | — |
| 时间线基础编辑 | Split、Undo/Redo、复制/粘贴、标记、转场、属性 Transform | 通过 | — |
| 时间线高级编辑 | Trim/Roll/Slip/Slide、链接 A/V、锁轨 | 本轮未重跑真实鼠标路径；结合第四轮 G 证据与静态复核，未发现新回归 | — |
| 时间线菜单/快捷键 | 菜单项存在性、`Ctrl+Shift+X` 实际分派 | **失败**：7 项菜单缺失，Split 键执行 Cut | E03 |
| 时间线错误反馈 | `status_message_requested` 接收者 | **0 接收者** | E04 |
| 普通片段双击 | `clip_double_clicked` 接收者 | **0 接收者** | E09 |
| 转场右键 | 前置转场、Dip To Color 类型 | **前置动作为空转，Dip 实为 Cross Dissolve** | E10 |
| 属性面板关键帧 | 普通参数 ◆、效果参数 ◆ | **普通 ◆ 无接收者；效果 ◆ 抛 KeyError** | E02 |
| 效果添加 | `AddEffectCommand` + 属性面板刷新 | 通过 | — |
| 音频播放 | 时间线播放、音频图 | 播放头推进；未做耳听/输出设备断言 | — |
| 音频自动化 | Write/Touch/Latch 采样、停止提交 | **永不采集** | E05 |
| 混音器 | Purge Cache、Routing 预览 | **no-op / 预览信号无人接收** | E17、E18 |
| 字幕 | 字幕工作区、时间线字幕集成、导出 | 相关测试通过（包含在 242 项批次内）；未做人工编辑 | — |
| 场景检测 | 对话流、缓存、时间线映射 | 测试通过；偏好不生效 | E07 |
| 多机位 | 创建、切角、同步 | 本轮未重跑；上一轮已有覆盖 | — |
| AI 助手 | `/scope`、上下文排除、profile、帮助卡 | **多个入口断线** | E13 |
| 交付菜单 | Deliver 菜单实际 action 列表 | **只剩“导出字幕 (SRT)...”** | E12 |
| 保存/加载 | 生产 `EditProjectIO` 往返 | 通过 | — |
| 导出 | 真实时间线 48 帧 H.264 + AAC | 通过 | — |
| 关闭 | 生产 orderly shutdown | 本轮脚本使用 `os._exit` 收尾，未重新验收关窗；上一轮记录通过 | — |

---

## 3. 问题总表

P1＝正常功能失败、错误内容或用户可见阻断；P2＝功能不可达/明显不一致；P3＝体验、清理或已披露的能力边界。

| ID | 优先级 | 模块 | 当前问题 | 证据 |
|---|---|---|---|---|
| E01 | P1 | 媒体池 → 时间线 | 右键“添加到时间线”对有声视频先追加一次链接 A/V，再额外追加一次音频；存在音频目标轨时第二次必定抛 `New clip overlaps`。同一重复逻辑也存在于快速工程自动添加路径。 | G：`ValueError: New clip overlaps ... at [1572, 3144)`；`edit_controller.py:6529-6551`、`:6585-6604`；`timeline_view.py:16722-16860` |
| E02 | P1 | 属性/效果 | 属性面板关键帧按钮全链路断裂：普通片段参数的 `keyframe_toggled` 无接收者；效果参数的 `_on_edit_effect_keyframe_toggled` 从门面取 `AddKeyframeCommand` 时抛 `KeyError`。 | G：`keyframe_toggled` 接收者 0、`effect_keyframe_toggled` 接收者 1；`KeyError: 'AddKeyframeCommand'`；`edit_properties_panel.py:825,1969`；`edit_controller.py:4649`；`commands_facade.py:38-69` |
| E03 | P1 | 菜单/快捷键 | Edit/Timeline 的 Cut、Copy、Paste、Split、Insert、Overwrite、Append 七项 action 因宿主回调不存在被 `MenuBuilder` 静默跳过；对应快捷键成为孤儿。`Ctrl+Shift+X` 被 `Ctrl+X` 的 Cut 分支吞掉。 | G：菜单只剩 Ripple Delete/Delete/Add Marker/Scene Cut/overlay/placeholder；`module.py:208-290,467-529`；`menu_builder.py:219-240`；`timeline_view.py:4669-4675` |
| E04 | P1 | 时间线反馈 | `TimelineView.status_message_requested` 有 18 个发射点，但全仓 0 个接收者。锁轨、撤销/重做事务拒绝、Slip/Slide 预检拒绝等错误只进日志。 | G/Q：`receivers=0`；`timeline_view.py:581` 及 18 处 emit |
| E05 | P1 | 音频自动化 | `_record_fader_sample()` 要求 `EditPage._is_playing` 为 True，但该属性在 `EditPage` 中从未定义/赋值；Write/Touch/Latch 推子样本永远不采集，停止播放也没有内容可提交。 | G：`hasattr(edit_page, '_is_playing') == False`；采样前后 automation 不变；`edit_controller.py:2687-2700`；`edit_page.py:2639-2651` |
| E06 | P2 | 播放协调 | `TimelinePanel.playback_state_changed` 有接收者，但控制器状态变化时从不 emit。播放开始时不会触发 Motion Graphic 缓存互斥，外部监听者收不到状态。 | G：2.5 s 播放期间 0 次发射；`timeline_panel.py:108,1341-1371`；`main_window.py:1927-1930` |
| E07 | P2 | 场景检测 | 偏好页会保存 sensitivity/use_proxy/min_scene_frames，但生产代码只读取硬编码 0.55/True/8；`set_default_settings()` 从未被调用。 | G：配置 0.91/False/2，实际默认 0.55/True/8；`preferences.py:269-323,442-451,520-535`；`scene_cut_controller.py:587-589,626-636` |
| E08 | P2 | 媒体池 → 调色 | 图片素材右键“Open Asset Grade in Color”会 emit `open_asset_grade_requested`，但全仓无 connect；点击无任何反应。 | G/Q：`receivers=0`；`media_pool_panel.py:4748,8360,8723` |
| E09 | P2 | 时间线 | 普通视频/音频片段双击只 emit `clip_double_clicked`，无人接收；标准“在 Source 中打开”工作流不可用。 | G：场景内 5 个 `ClipItem` 的接收者均为 0；`timeline_items.py:588,5684` |
| E10 | P2 | 转场 | 右键靠近左边缘时创建“Add Transition Before...”，但结果处理只覆盖 `is_near_right_edge`；“Add Transition (Dip To Color)”与 Cross Dissolve 发出同一个 `("right")` 请求，实际都走默认 Cross Dissolve。 | S：`timeline_items.py:4671,5266-5270`；`timeline_view.py:10868` |
| E11 | P2 | 撤销/重做 | 全局 Edit 菜单的 Undo/Redo 只路由图像引擎/Composite，不驱动时间线或媒体池命令栈；时间线编辑后菜单仍为 disabled。 | G：分割/属性编辑后 `undo_action_menu.isEnabled()==False`；`menu_toolbar_controller.py:185-193`；`layer_controller.py:2148-2176` |
| E12 | P2 | 交付菜单 | Deliver 模块注册的 Export Current Source/Settings/Timeline/Composite、Queue Start/Stop 六项 action 没有宿主回调，被 `MenuBuilder` 跳过；对应快捷键同样成为孤儿。 | G：Deliver 菜单只剩“导出字幕 (SRT)...”；S：`deliver/module.py:156-220`；`menu_builder.py:219-240` |
| E13 | P2 | AI 助手 | `/scope`、上下文检查器“排除”、无 profile 时的“配置模型”、帮助卡“定位/打开页面”分别 emit 到无人接收的信号；UI 有入口但无行为。 | S：`ai_edit_assistant_panel.py:457-463,609,2507,2886,2937,2945`；`ai_help_answer.py:44-45`；`ai_edit_assistant_panel.py:3226-3240` |
| E14 | P2 | 音频增益 | Audio Gain 对话框只有手动 dB；注释明确 Normalize 目标（-23 LUFS/峰值 -3 dB）的分析按钮待接响度管线，`compute_normalize_gain_db` 已存在但无入口。 | S：`timeline_view.py:11517-11530` |
| E15 | P2 | 变速保调 | Preserve Pitch 只在离线导出走 WSOLA；实时预览仍是变调 resample。tooltip 已披露，但预览/导出一致性未闭环。 | S：`audio_playback.py:920,1403`；`clip_params.py:487`；`timeline_export.py:4209-4235` |
| E16 | P3 | 自动化 | Touch 模式松手后不回归原曲线；`automation_recorder.py` 顶部注释与实现明确说明未做 release 曲线，UI 仍提供 Touch/Latch。 | S：`automation_recorder.py:20-23,128,397` |
| E17 | P3 | 混音器 | `Purge Cache` 连接存在，但全仓无 emit；处理器在 `PURGE_CACHE_IMPLEMENTED=False` 时只记日志，即使改 True 仍只打 debug。 | S：`audio_mixer_panel.py:53`；`edit_controller.py:3900-3912`；`mixer_feature_flags.py:31-33` |
| E18 | P3 | 混音器 | Routing 对话框拖动 send level 时 `routing_preview` 无人接收，控制器只接 `routing_committed`；拖动无实时听感预览。 | S：`routing_dialog.py:177,399`；`edit_controller.py:3727` |
| E19 | P3 | 媒体池属性 | 多选素材时 Properties 直接清空，注释写明“currently not supported”；用户看不到批量素材属性。 | S：`edit_controller.py:5421-5429` |
| E20 | P3 | 导出 | PNG/TIFF 序列导出被显式拒绝，只支持 `topos_image_sequence`；这是已声明边界，但 Deliver/帮助没有就地说明。 | S：`timeline_export.py:574-588` |
| E21 | P3 | 代理 | 音频代理生成明确提示 Not Supported；没有队列/替代方案。 | S：`media_pool_panel.py:10578-10588` |
| E22 | P3 | 清理 | 大量发射后无人接收的信号、休眠面板与旧入口：`EditSignalHub` 多项、Source Viewer 旧 Insert/Overwrite/Append、`clip_double_clicked` 之外的 `track_double_clicked`、`source_opened/closed`、`command_executed/edit_mode_changed`、Scene Cut `detection_error/subclips_added`、AI Job/Decision/Variant/Diff 面板、混音器 Preset/RoutingMatrix 对话框、`quick_project_created`、`asset_removed` 等。 | S：死信号扫描结果与各文件声明 |

---

## 4. 详细实施计划

### 4.1 P1：先恢复主链路

#### E01 媒体池“添加到时间线”重复音频

根因：`TimelineView.append_clip()` 在素材为有声视频且存在音频目标轨时，已经自行构造 `AddClipCommand(V) + AddClipCommand(A) + Link` 的 A/V 宏；`_on_add_asset_to_timeline()` 和 `_auto_add_media_to_timeline()` 在调用它之后又手动追加一次音频。

实施：

1. 删除 `edit_controller.py` 两处 `audio_track_id` 二次追加逻辑，统一依赖 `TimelineView.append_clip()` 的 A/V 分支。
2. 如果未来确实需要“只加视频”入口，给 `append_clip` 增加显式 `include_linked_audio: bool = True` 参数，禁止调用方在外部二次拼装。
3. 控制器入口增加结构化失败处理：捕获 `TimelineOperationError/ValueError`，发 `status_message_requested` 或状态栏提示，不留下半提交状态。
4. 回归：新增 `tests/edit/test_media_pool_add_to_timeline_av.py`，覆盖普通视频、无声视频、纯音频、已有 A/V 轨；断言无异常、恰好 1 V + 1 A、双向 link、一步 Undo 同时移除两片；快速工程自动添加路径同样覆盖。

关闭条件：真实 2K H.264 右键添加到时间线无异常；时间线只有一对互链 V/A；日志无 overlap；一步 Undo 清空；无声素材只加视频。

#### E02 关键帧按钮全链路

根因一：`EditPropertiesPanel.keyframe_toggled` 只在面板内部 emit，`MainWindow` 只连接了 `effect_keyframe_toggled`。
根因二：`_on_edit_effect_keyframe_toggled()` 用 `_cmds["AddKeyframeCommand"]` / `["RemoveKeyframeCommand"]` 直接索引，而 `commands_facade.get_command_classes()` 没有导出这两个类。

实施：

1. 在 `commands_facade.get_command_classes()` 增加 `AddKeyframeCommand`、`RemoveKeyframeCommand`，并加契约测试断言键存在。
2. 在 `MainWindow` 连接 `edit_properties_panel.keyframe_toggled` 到 `EditController._on_edit_clip_keyframe_toggled`；该槽复用与效果关键帧相同的命令栈、缓存失效和面板刷新流程。
3. 两个槽都改为 `_cmds.get(...)`，缺失时返回结构化诊断，不把 `KeyError` 抛进 Qt 槽。
4. 回归：对 Transform/Opacity/Crop 参数和效果参数分别点击 ◆；断言关键帧在播放头帧生成、面板按钮变为已激活、Undo 删除、Redo 恢复；锁轨/无效帧给状态提示且零提交。

关闭条件：普通参数与效果参数的关键帧均可添加/删除；Undo/Redo 精确；属性面板与 canonical model 同步；不再出现 `KeyError`。

#### E03 菜单命令与 Split 快捷键

根因：`module.py` 注册了 7 个 `MenuActionDescriptor`，但 `MainWindow` 没有 `on_timeline_cut/copy/paste/split/insert/overwrite/append`；`MenuBuilder` 按设计跳过缺少回调的 action。`TimelineView` 的硬编码 `Ctrl+X` 分支只检查 Control，不排除 Shift，因此 Split 默认键 `Ctrl+Shift+X` 先命中 Cut。

实施：

1. 在 `MainWindow`/`EditController` 增加 7 个宿主回调，委托 `TimelineView` 的公共方法或命令构建；Insert/Overwrite/Append 若实际是“编辑模式/落点动作”，明确其语义并接到 `timeline_panel` 的现有方法，不保留空壳。
2. 修正 `Ctrl+X` 条件为 `Control and not Shift`；同时检查 `Ctrl+C/V/A/Z` 是否都需要排除冲突修饰键。
3. 让 `ShortcutManager` 的 `edit.timeline.cut/copy/paste/split` 真正绑定到 QAction；如果继续使用 `TimelineView` 硬编码分支，则从快捷键注册表移除这些孤儿项，避免用户在设置里改键无效。
4. 回归：菜单中 7 项可见且可用；Split 菜单项分割播放头；`Ctrl+Shift+X` 只 Split 不 Cut；Cut/Copy/Paste 的菜单与键盘结果一致；快捷键设置改键后生效。

关闭条件：菜单、快捷键、右键/工具栏三条入口结果一致；无孤儿快捷键；`Ctrl+Shift+X` 不再误剪。

#### E04 时间线错误反馈

根因：`TimelineView` 没有状态栏引用，错误统一 emit `status_message_requested`；但 `MainWindow`/`EditPage` 未连接该信号。

实施：

1. 在 Edit 接线处连接 `timeline_view.status_message_requested` 到 MainWindow 状态栏（例如 `status_bar.showMessage(text, 5000)`），并写入结构化日志。
2. 如果 Deliver 页复用同一个 TimelineView，连接一次即可；用 active-page 判断避免重复消息。
3. 对事务拒绝类错误，除状态栏外保留对话框/通知入口，避免长文本被 5 秒截断。
4. 回归：锁轨 trim、Undo 事务拒绝、Slip/Slide 预检拒绝三条路径均能在状态栏看到原因；无异常逃逸。

关闭条件：现有 18 个 emit 点至少有统一接收者；用户可见错误与日志一致；不重复弹窗。

#### E05 音频自动化录制

根因：`_record_fader_sample()` 读取 `EditPage._is_playing`，但该属性不存在；`EditPage.start_playback()` 也只发 hub 信号，没有维护状态。

实施：

1. 不新增第二套播放状态。优先从 `edit_page._timeline_panel.get_playback_controller().is_playing` 与 `current_frame` 取权威状态；或在 `EditPage` 订阅 controller `state_changed` 后维护只读缓存。
2. `_record_fader_sample()` 使用权威状态和当前帧；播放停止/暂停、模式切回 Off、工程切换时提交或取消 recorder。
3. 回归：`tests/edit/test_audio_automation_recording.py` 覆盖 Write/Touch/Latch；播放中推子产生关键帧，停止后生成一个 command，Undo 恢复；暂停/切轨/切工程不提交半截数据。

关闭条件：自动化模式不再是“UI 可切、实际不录”；播放状态与录制生命周期一致；一步 Undo。

### 4.2 P2：恢复不可达入口与一致性

#### E06 播放状态信号

在 `TimelinePanel._on_controller_state_changed()` 中根据 controller 状态 emit `playback_state_changed(is_playing, speed, direction)`，并做重复状态去重。回归断言播放/暂停/停止各触发一次，Motion Graphic 缓存停止/恢复被调用。关闭条件：2.5 s 播放至少收到 PLAYING/PAUSED 两次状态，外部监听者与 transport 一致。

#### E07 场景检测偏好

在 `EditPage._ensure_scene_cut_controller()` 创建控制器后，读取 `get_scene_cut_settings_from_config(config)` 并调用 `set_default_settings()`；偏好页保存后刷新下一次检测的默认值。回归：配置 0.91/False/2 时对话框默认值一致，修改后下一次检测生效。关闭条件：偏好页与检测实际参数一致，不再使用硬编码。

#### E08 图片素材调色入口

在 `dock_layout_controller` 或 `EditController` 连接 `media_pool_panel.open_asset_grade_requested`：
1. 校验素材为 image 且存在；
2. 通过现有 `ColorStackData.create_standalone_image()`/工程命令创建或复用 stack；
3. 切换到 Color 页并选中该 stack。回归：右键图片素材后页面切换、stack id 正确、视频素材不出现该入口。

#### E09 片段双击

连接场景内 `ClipItem.clip_double_clicked` 到 `TimelineView` 新槽：按 `playhead - timeline_start` 计算当前源帧，调用 `SourceViewerWidget.open_asset(asset_id, frame=...)` 或等价公共入口。回归：双击视频/音频片段打开正确素材与源帧；锁定/离线素材给明确提示。

#### E10 转场右键

1. 为 `is_near_left_edge` 增加结果分支：找左邻居，emit `add_transition_requested` 的 `"left"` 侧与显式 `transition_type`。
2. 扩展 `add_transition_requested` 载荷（clip_id, track_id, side, transition_type）或新增 `add_transition_before_requested`；`TimelineView` 按 side 选择 cut point。
3. Dip To Color 传 `transition_type="dip_to_color"`，不再复用 Cross Dissolve 默认值。
4. 回归：左边缘菜单前置转场成功，右边缘 Cross Dissolve/Dip 类型正确，Undo 精确。

#### E11 全局撤销/重做

时间线聚焦时局部快捷键仍可 Undo，但菜单始终 disabled，焦点移到 Properties/媒体池后也没有全局撤销。增加页面感知的 undo/redo 路由：
1. 当前页为 Edit 且焦点在 Timeline/Media Pool 时，调用对应命令栈；
2. 当前页为 Composite/Color 时保持现有引擎路由；
3. 根据当前栈的 `can_undo/can_redo` 更新菜单 enabled 与 label；
4. 确保 QAction 快捷键不抢占 TimelineView 的局部按键。
回归：时间线 Split 后菜单 Undo 可用并撤销；媒体池删除后菜单 Undo 可用；Color/Composite 原有 Undo 不回归；菜单/键盘结果一致。

#### E12 Deliver 菜单

实际运行中 Deliver 菜单只剩“导出字幕 (SRT)...”，说明另外六项已被 `MenuBuilder` 跳过。为 6 个缺失 action 实现宿主回调，或删除 descriptor 与快捷键注册。推荐实现：
- Export Current Source/Settings/Timeline/Composite → 切到 Deliver 页并调用对应页方法/预填配置；
- Queue Start/Stop → 调用导出队列服务。
回归：Deliver 菜单六项均存在且可用；快捷键有效；无回调缺失日志。

#### E13 AI 助手断线入口

1. `/scope` → 打开范围确认卡或 composer scope 选择；
2. `context_block_excluded` → `ContextBundle.exclude_block(block_id)` 并刷新上下文检查器；
3. `composer_profile_requested` → 打开 AI 偏好页/模型选择；
4. 帮助卡 `navigate_requested` / `open_panel_requested` → 宿主定位条目/切页；
5. 若某项暂不实现，删除菜单/按钮，避免“有入口无行为”。
回归：每个入口都有可观察结果；取消/失败有反馈；不执行写操作。

#### E14 Audio Normalize

在 Audio Gain 对话框增加“分析并归一化”：
1. 后台分析 clip/时间线音频的 LUFS 与真峰值；
2. 用 `compute_normalize_gain_db()` 给出目标 gain；
3. 用户确认后通过 `SetClipAudioGainCommand` 一次提交；
4. 取消/失败零提交。
回归：标准 PCM 目标 -23 LUFS / -3 dBTP 得到预期 gain；长片段分析可取消；Undo 恢复原 gain。

#### E15 实时 Preserve Pitch

二选一：
- 实现 streaming WSOLA 的实时预览路径，复用离线参数与 time-map；
- 或把实时开关明确标为“仅导出保调”，在属性面板禁用/提示，避免用户误以为预览已保调。
回归：0.5/1.25/2x 语音与 tone 对比；若禁用，UI 文案与导出行为一致；预览/导出差异有测量记录。

### 4.3 P3：体验与清理

#### E16 Touch 自动化 release

在 `AutomationRecorder` 保存 pre-touch 曲线，松手后按可配置时间常数回到原曲线或做交叉淡化；增加“Touch 松手不改变未触碰段”的回归。关闭条件：Touch 录制结束曲线连续、无台阶；Write/Latch 语义不变。

#### E17 Purge Cache

决定产品策略：
- 实现真实缓存清理并暴露入口；或
- 删除信号、菜单项、处理器和 feature flag，避免保留 no-op。
回归：若保留，调用后缓存计数下降、播放/导出不受影响；若删除，全仓无引用。

#### E18 Routing 预览

把 `routing_preview` 接到控制器，使用临时监听增益/路由在音频图中预览，不写命令；`routing_committed` 仍作为唯一提交。回归：拖动 send level 有实时听感，取消恢复原路由，提交一步 Undo。

#### E19 多选素材属性

实现批量素材属性面板（至少显示类型/分辨率/FPS/时长/色彩空间，允许批量重命名/代理策略），或把多选状态明确显示为“批量编辑暂不支持”，不要清空面板造成“坏掉”的印象。

#### E20 PNG/TIFF 序列

实现图像序列导出适配器，或在导出配置与 Deliver UI 中就地说明“仅支持 .topos 序列”，并提供转换建议。回归：错误信息可行动、不产生半文件。

#### E21 音频代理

实现音频代理（波形/解码缓存）或保留明确 Not Supported；如果保留，代理菜单对音频素材应禁用而非点击后弹提示。

#### E22 死信号与休眠组件

按以下顺序清理：
1. 先修 E04/E06/E08/E09/E10/E13 涉及的信号；
2. 删除确认无消费者的 `EditSignalHub` 项、Source Viewer 旧编辑信号、`quick_project_created`、`asset_removed`、`command_executed/edit_mode_changed`、Scene Cut 控制器 `detection_error/subclips_added`；
3. 休眠 UI（AI Job/Decision/Variant/Diff、PresetDialog、RoutingMatrixDialog）标记 experimental 或移出生产 import；
4. 增加静态检查：对 `pyqtSignal` 做“有 emit 无 connect”白名单，防止再次出现“UI 有入口、宿主没接线”。

---

## 5. 实施批次与顺序

| 批次 | 范围 | 目标 | 依赖 |
|---|---|---|---|
| A（立即） | E01–E05 | 恢复媒体池添加、关键帧、菜单/快捷键、错误反馈、自动化录制 | 无 |
| B（随后） | E06–E14 | 恢复播放状态、场景检测偏好、调色/双击/转场入口、撤销路由、交付菜单、AI 入口、Normalize | A 的 E04 状态栏先落地；B 内先做 E11 撤销路由 |
| C（收尾） | E15–E22 | 实时保调/自动化 release、混音器清理、序列/代理边界、死代码清理 | B 的信号接线完成后再删代码 |
| D（门禁） | 回归加固 | 新增宿主接线测试、真实素材 smoke、导出探测 | A–C |

建议每批独立提交，提交信息包含 `E0x` 与验证命令；不要在同一提交里同时改领域命令和 UI 路由，便于回滚和定位。

---

## 6. 验收与回归要求

### 6.1 新增测试

| 测试文件 | 覆盖 |
|---|---|
| `tests/edit/test_media_pool_add_to_timeline_av.py` | E01：有声/无声/纯音频，V/A 数量、互链、一步 Undo |
| `tests/edit/test_properties_keyframe_wiring.py` | E02：普通/效果 ◆ 的宿主连接、KeyError 消失、Undo/Redo |
| `tests/edit/test_timeline_menu_shortcuts.py` | E03：7 项菜单、Split 快捷键、Cut/Copy/Paste 一致性 |
| `tests/edit/test_timeline_status_feedback.py` | E04：锁轨/事务拒绝/Slip 拒绝状态栏可见 |
| `tests/edit/test_audio_automation_recording.py` | E05：Write/Touch/Latch 录制与停止提交 |
| `tests/edit/test_playback_state_bridge.py` | E06：PLAYING/PAUSED/STOPPED 发射与去重 |
| `tests/edit/test_scene_cut_preferences_wiring.py` | E07：偏好→控制器默认值 |
| `tests/edit/test_media_pool_grade_double_click.py` | E08/E09：图片调色入口、片段双击 Source |
| `tests/edit/test_transition_context_menu.py` | E10：前置转场、Dip 类型 |
| `tests/edit/test_global_undo_routing.py` | E11：Edit/Media Pool/Color/Composite 路由 |
| `tests/edit/test_deliver_menu_actions.py` | E12：6 项 action 存在且可调用 |
| `tests/edit/test_ai_assistant_wiring.py` | E13：scope/exclude/profile/help 入口 |

### 6.2 必跑命令

```bash
# 定向回归
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/edit

# 真实窗口复现
.venv/bin/python docs/audit/2026-10-20-edit-fifth-review/scripts/audit_driver.py
.venv/bin/python docs/audit/2026-10-20-edit-fifth-review/scripts/ops_driver.py
.venv/bin/python docs/audit/2026-10-20-edit-fifth-review/scripts/undo_driver.py

# 导出 smoke（先由 audit_driver 生成临时工程）
.venv/bin/python docs/audit/2026-10-20-edit-fifth-review/scripts/export_check.py
```

### 6.3 关闭标准

每项必须同时满足：

1. 当前失败输入消失，且新增回归在未修版本上会失败、修复后通过；
2. 真实 `MainWindow` + 真实素材路径通过，不以单个 handler 或 fake 字段代替；
3. 保存/重载后语义一致；
4. preview/export 一致（涉及渲染/音频/变速的项）；
5. Undo/Redo、revision、dirty 状态一致；
6. 拒绝/取消/no-op 不产生内容、历史或 revision 副作用。

不得通过放宽误差容差、删除断言或把真实素材换成空文件来让测试变绿。

---

## 7. 能力边界与未覆盖项

本轮没有声称以下内容已验收：

- 4K60、长时 VFR、HDR 全链路、耳听音频、多设备音频漂移；
- 所有硬件编码器、外部软件 FCPXML/OTIO 往返；
- 多机位长片、Nest 多层音频 retime、跨 FPS 速度曲线换源；
- 关闭流程的完整人工验收（本轮脚本用 `os._exit` 收尾）。

第三轮 F01–F16、H01–H08 中尚未完成的专业工作流仍按原计划跟踪，不因本轮修复而删除；本轮的 E01–E22 是当前代码的接线/行为问题，与那些新建设施分开管理。

明确的产品边界保留：

- PNG/TIFF 序列导出不支持，仅 `.topos` 图像序列；
- 音频代理不支持；
- 多选素材属性暂不支持；
- 实时 Preserve Pitch 尚未与离线一致；
- Touch 自动化 release 曲线尚未实现。

---

## 8. 证据索引

目录：`docs/audit/2026-10-20-edit-fifth-review/`

| 文件 | 内容 |
|---|---|
| `README.md` | 本轮执行摘要与复现说明 |
| `records/main.json` | 导入、媒体池添加、菜单、接收者、关键帧、快捷键、自动化、播放、场景检测、保存/加载 |
| `records/ops.json` | Split/Undo/Redo、复制粘贴、标记、转场、双击接收者、Transform |
| `records/undo.json` | 菜单 Undo 状态、`Cmd+Z`/`Ctrl+Z` 分派 |
| `records/export.json` | 真实 48 帧导出结果 |
| `logs/*.log` | 运行日志、异常与导出编码器记录 |
| `scripts/*.py` | 当轮使用的完整窗口/导出脚本副本（含本机绝对路径） |

关键动态值：

- 媒体池添加：`ValueError: New clip overlaps with ... at [1572, 3144)`。
- 菜单：Edit/Timeline 仅 Ripple Delete、Delete、Add Marker、Scene Cut Detection、场景检测覆盖层、新建占位片段。
- 接收者：`status_message_requested=0`、`open_asset_grade_requested=0`、`asset_removed=0`、`keyframe_toggled=0`、`effect_keyframe_toggled=1`、`playback_state_changed=1`（但 0 次发射）、5 个 `ClipItem.clip_double_clicked=0`。
- Deliver 菜单：只剩“导出字幕 (SRT)...”，Export Current Source/Settings/Timeline/Composite、Queue Start/Stop 均不存在。
- 关键帧：`KeyError: 'AddKeyframeCommand'`。
- 快捷键：`Ctrl+Shift+X` → `cut_calls=1, split_calls=0`。
- 自动化：`edit_page_has_is_playing=false`；采样前后 automation 不变；recorder 数量 0。
- 场景检测：配置 0.91/False/2 → 实际 0.55/True/8。
- 保存/加载：4,219 字节，1 素材、1 时间线、1 序列。
- 导出：48 帧、640×360、H.264、AAC 48 kHz 双声道、1.94 s、50,900 字节。

---

## 附：一句话优先级

**先修 E01–E05，让“能点的按钮真的工作、出错能看见”；再修 E06–E14 的断线入口；最后清理 E15–E22 的已知边界与死代码。** 每批都以真实素材、完整窗口和 preview/export 一致性作为关闭门禁。
