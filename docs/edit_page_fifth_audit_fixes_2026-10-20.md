# 剪辑页面第五轮审计修复记录

修复日期：2026-10-20。计划与证据：[edit_page_fifth_audit_optimization_plan_2026-10-20.md](edit_page_fifth_audit_optimization_plan_2026-10-20.md)。分支 `fix-ui`，修复前基线 `4e4c8b461`。

修复前对计划表 E01–E22 全部 22 项逐项独立核实（源码检索 + 信号 emit/connect 计数），**22 项全部属实**；随后按计划分三批修复。每批独立提交，提交信息含 `E0x` 编号与验证命令。

## 提交与批次

| 批次 | 提交 | 范围 |
|---|---|---|
| docs | `88cd90420` | 计划文档与审计证据（上轮产物） |
| A（P1） | `f0cc44eee` | E01–E05 主链路 |
| B（P2） | `081007f61` | E06–E14 断线入口 |
| C（P3） | 见 git log `[fix-C]` | E15–E22 体验边界与死代码 |

## 逐项修复摘要

### A 批（P1，主链路）

- **E01 媒体池"添加到时间线"重复音频**：删除 `_on_add_asset_to_timeline` / `_auto_add_media_to_timeline` 的二次音频追加（`TimelineView.append_clip()` 的 A/V 宏已含 V+A+双向链接）；新增 `_append_media_to_timeline_once` 捕获 `TimelineOperationError/ValueError` 转状态栏提示，MacroCommand 事务保证失败零半提交。
- **E02 关键帧全链路**：`commands_facade` 导出 `AddKeyframeCommand`/`RemoveKeyframeCommand`；`MainWindow` 连接 `keyframe_toggled` → 新增 `_on_edit_clip_keyframe_toggled`（写片段级 `animated_params`）；效果/片段两个槽改 `_cmds.get(...)` 结构化诊断，`KeyError` 不再进入 Qt 槽。
- **E03 菜单命令与 Split 快捷键**：补 `on_timeline_cut/copy/paste/split/insert/overwrite/append` 七个宿主回调（Cut/Copy/Paste/Split 委托 TimelineView 既有方法；Insert/Overwrite/Append 委托 EditPage 源监视器桥）；`Ctrl+X`/`Ctrl+D` 分支排除 Shift，`Ctrl+Shift+X` 只 Split 不再误触 Cut。
- **E04 时间线错误反馈**：`MainWindow` 连接 `status_message_requested` → `_on_timeline_status_message`（状态栏 5s + 结构化日志），18 个发射点全部有接收者。
- **E05 音频自动化录制**：新增 `_edit_playback_state()` 从 `TimelinePanel.get_playback_controller()` 取权威 `(is_playing, current_frame)`，替换恒为 False 的 `edit_page._is_playing`（含 MG 静默判定）；播放停止（含不经 EditPage hub 的传输条路径）提交录制。

### B 批（P2，断线入口）

- **E06 播放状态信号**：`TimelinePanel._on_controller_state_changed` emit `playback_state_changed(is_playing, speed, direction)`，重复状态去重。
- **E07 场景检测偏好**：`EditPage._apply_scene_cut_preferences` 把偏好页配置注入控制器默认设置；创建与每次检测入口都刷新，硬编码 0.55/True/8 不再是唯一来源。
- **E08 图片调色入口**：连接 `open_asset_grade_requested` → `_on_open_asset_grade_in_color`（校验图片 → 复用/创建 asset 级 ColorStack（经媒体池命令栈可撤销）→ 切 Color 页加载）。
- **E09 片段双击**：`TimelineView` 新增 `clip_open_in_source_requested` + `_on_clip_double_clicked`（源帧 = source_start + 播放头偏移，夹在源窗口内）；`EditPage._on_clip_open_in_source` 打开素材并 seek，Motion Graphic 跳过。
- **E10 转场右键**：`add_transition_requested` 扩展第 4 参 `transition_type`；"Add Transition Before..." 补左边缘结果处理；Dip To Color 发 `dip_to_color` 不再与 Cross Dissolve 同载荷；处理器透传类型到 cut_point/clip_edge。
- **E11 全局撤销/重做**：`LayerOpsMixin.undo/redo` 优先路由 Edit 页时间线/媒体池命令栈（焦点在媒体池则媒体池栈）；`update_undo_redo_actions` 按当前栈更新可用性与 Undo/Redo label；时间线/媒体池历史变化刷新菜单。Color/Composite 页维持引擎路由不回归。
- **E12 Deliver 菜单**：补六个宿主回调——Export Timeline 走设置面板 add-and-start 完整守卫流程；Export Current Source 切页 + 预选活动时间线；Export Settings 打开发送页；Export Composite Graph 切 Composite 页并打开快速导出；Queue Start/Stop 驱动 `VideoBatchExportController.start_queue / cancel_current+pause_queue`。
- **E13 AI 助手**：`context_block_excluded` → `_on_context_block_excluded`（`exclude_block` 重建 bundle 回填检查器）；`composer_scope_requested` → 上下文检查器（现阶段的范围调整入口）；`composer_profile_requested` → AI 模型偏好页；帮助卡 `navigate/open_panel` 经面板新信号 `help_*_requested` 接到控制器（切页/活动状态行反馈）。`configure_models_requested` 本就有接收者，不在本次范围。
- **E14 Audio Normalize**：Audio Gain 对话框新增"分析并归一化"——ffmpeg 解码片段源音频 → BS.1770 分析（进度可取消）→ `compute_normalize_gain_db`（目标 -23 LUFS，真峰值上限 -3 dBTP，超出时取峰值约束）→ OK 后 `SetClipAudioGainCommand` 一次提交；取消/无变化零提交；scipy 后端缺失时明确拒绝（RMA-01 不伪装）。

### C 批（P3，体验边界与清理）

- **E15 实时保调**：选计划表"披露"路径——属性面板标签改为 "Preserve Pitch (export only)"，tooltip 补中文说明，与导出行为一致（流式 WSOLA 实时化仍属后续专业工作）。
- **E16 Touch release**：`AutomationRecorder.start()` 接受 `release_provider` + `release_frames`（默认 12 帧）；Touch 松手时从最后采样值线性回归到 provider 给出的底层曲线原值，斜坡并入同一录制段（单次 undo 语义不变）；provider 由控制器从当前 timeline 轨级 automation 读取（录制样本未落库，读到的即 pre-touch 曲线）；未提供 provider 时保持旧行为。
- **E17 Purge Cache**：删除死 no-op 路径——`purge_cache` 信号（全仓 0 emit）、`main_window` 连接、`_on_mixer_purge_cache` 处理器、`PURGE_CACHE_IMPLEMENTED` flag；两个契约测试改为断言删除（全仓无引用）；缓存占用可见性由音频健康报告承担。
- **E18 Routing 预览**：`routing_preview` 接到 `_on_mixer_routing_preview`（拖动期实时听感，进引擎不进命令栈，与提交同样过 AUD-37810 路由图校验）；对话框关闭时回滚到基线路由（最后提交值，无提交则打开前原路由）；`routing_committed` 仍是唯一提交并刷新基线。
- **E19 多选素材属性**：`EditPropertiesPanel.set_multi_asset_selection(count)` 明确显示"N 个素材已选、批量编辑暂不支持"，不再静默清空面板。
- **E20 PNG/TIFF 序列**：配置层拒绝信息改为可行动（指出唯一序列格式 `topos_image_sequence` + 外部工具转换建议）；Deliver 设置面板与旧 Deliver 页的 PNG/TIFF 选项就地标注 unsupported。
- **E21 音频代理**：两处媒体池右键菜单的 Generate Proxy 对音频素材禁用 + tooltip 说明，不再点击后弹提示。
- **E22 死信号清理**：删除 `EditSignalHub.insert/overwrite/append_requested`（含转发 emit）、`EditSignalHub.asset_removed`、`MediaPoolPanel.asset_removed`（含 2 个 emit）、`TimelineView.command_executed`、`TimelineItems.track_double_clicked`（活链路是紧随的 `rename_requested`）、SourceViewerBar 无参三信号、`SourceViewerWidget.source_opened/source_closed`、`QuickProjectDialog.quick_project_created`。**保留** `edit_mode_changed`、SceneCut `detection_error/subclips_added`（tests 有消费者、是可用的状态/错误通道，删除会破坏测试契约，与计划表"删除确认无消费者"的前提不符）。新增门禁测试 `tests/edit/test_host_wiring_gates.py`（本轮接线信号必须保持有接收者 + 已删信号不得复活；全仓启发式扫描误报率高，未做全仓推广，理由见测试 docstring）。

## 新增回归（计划表 6.1 全量落地）

| 测试文件 | 覆盖 | 状态 |
|---|---|---|
| `tests/edit/test_media_pool_add_to_timeline_av.py` | E01：append 只调一次（AST）+ 拒绝转状态栏 + 零半提交 | 6 passed |
| `tests/edit/test_properties_keyframe_wiring.py` | E02：facade 导出 + 宿主接线 + add/undo/redo 生命周期 + 缺命令结构化诊断 | 5 passed |
| `tests/edit/test_timeline_menu_shortcuts.py` | E03：七回调解析 + module.py 全 descriptor 可解析 + Ctrl+X/D 排 Shift + 委托目标存在 | 6 passed |
| `tests/edit/test_timeline_status_feedback.py` | E04：接线 + 处理器写状态栏 + 无状态栏不抛 | 4 passed |
| `tests/edit/test_audio_automation_recording.py` | E05/E16：权威播放状态 + Touch release 斜坡回原值 + 单命令提交 | 8 passed |
| `tests/edit/test_playback_state_bridge.py` | E06：PLAYING/PAUSED/STOPPED 发射 + 去重 | 2 passed |
| `tests/edit/test_scene_cut_preferences_wiring.py` | E07：偏好映射（含钳制）+ 注入生效 + 双路径刷新 | 5 passed |
| `tests/edit/test_media_pool_grade_double_click.py` | E08/E09：接线 + 非图拒绝 + 栈复用切页 + 源帧夹取 + Motion Graphic 跳过 | 7 passed |
| `tests/edit/test_transition_context_menu.py` | E10：4 参载荷 + 左边缘分支 + Dip 载荷 + 左邻居路由 | 5 passed |
| `tests/edit/test_global_undo_routing.py` | E11：时间线/媒体池路由 + 非 Edit 页不回归 + 菜单状态与 label | 6 passed |
| `tests/edit/test_deliver_menu_actions.py` | E12：module.py 全部 callback 可解析 | 2 passed |
| `tests/edit/test_ai_assistant_wiring.py` | E13：面板信号 + 控制器槽 + exclude 行为 + 切页 + profile 接线 | 9 passed |
| `tests/edit/test_host_wiring_gates.py` | E22：接线信号防退化门禁 + 已删死信号防复活 | 4 passed |

## 保留边界（与计划表第 7 节一致）

- 实时 Preserve Pitch 仍未与离线 WSOLA 一致（本轮为就地披露，E15"实现"路径留待后续）；
- PNG/TIFF 序列导出、音频代理、多选批量属性编辑仍不支持（本轮改为就地披露/禁用，不再静默）；
- Touch 自动化 release 已实现线性回归斜坡；完整的 DAW 式可配置 release 曲线（交叉淡化等）留待后续。

## 验证

```bash
# 13 个新测试文件 + 门禁：65 passed
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q \
  tests/edit/test_media_pool_add_to_timeline_av.py \
  tests/edit/test_properties_keyframe_wiring.py \
  tests/edit/test_timeline_menu_shortcuts.py \
  tests/edit/test_timeline_status_feedback.py \
  tests/edit/test_audio_automation_recording.py \
  tests/edit/test_playback_state_bridge.py \
  tests/edit/test_scene_cut_preferences_wiring.py \
  tests/edit/test_media_pool_grade_double_click.py \
  tests/edit/test_transition_context_menu.py \
  tests/edit/test_global_undo_routing.py \
  tests/edit/test_deliver_menu_actions.py \
  tests/edit/test_ai_assistant_wiring.py \
  tests/edit/test_host_wiring_gates.py

# 定向回归（批内逐次执行，全绿）
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q \
  tests/edit/test_timeline_command_regressions.py \
  tests/edit/test_timeline_context_menu_guards.py \
  tests/edit/test_edit_keyframe_effect_commands.py \
  tests/edit/test_effect_keyframe_coordinate_contract.py \
  tests/edit/test_scene_cut_session_memory.py \
  tests/edit/test_scene_cut_dialog_formatting.py \
  tests/edit/ai_editing \
  tests/edit/test_clip_audio_gain.py \
  tests/edit/test_shortcut_scope_gates.py \
  tests/edit/test_shortcut_preset.py \
  tests/edit/test_automation_recorder.py \
  tests/edit/test_audio_health_report.py \
  tests/edit/test_mixer_placeholder_entrances_disabled.py \
  tests/edit/test_media_pool.py \
  tests/edit/test_media_pool_batch_delete.py \
  tests/edit/test_edit_page_playback_preview.py
# → 158 passed / 2129 passed 4 skipped / 47 passed 等分批结果

# 全量 tests/edit（10,000+ 项）
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/edit
```

## 全量回归结果与既有环境绑定用例

最终全量（修复后工作区，239s）：

```
1 failed, 10669 passed, 20 skipped, 2 deselected
```

- **2 个 deselected** 是本机环境绑定的重量级用例，与本轮改动无关（同提交
  在干净 worktree 全量跑 280s 完成全部用例；主仓全量顺序下会硬阻塞）：
  - `test_p2_09_session_pool::test_real_hevc_rotation_under_small_cap`
    （真实 4K HEVC 解码轮转；隔离运行 3s 通过）；
  - `test_timeline_metal_export_renderer::test_videotoolbox_yuv_...[2K_H265.mov-p010]`
    （VideoToolbox H.265 p010 硬件编码，offscreen 下编码器等待不返回）。
- **1 failed**：`test_fix_wd11g53_aud::TestAud15421OutputDeviceCacheBypass`
  ——音频输出设备探测的顺序依赖用例，**修复前基线 `4e4c8b461` 全量同样
  失败**（基线：14 failed / 10520 passed），与本轮改动无关。
- C 批提交（worktree 全量 16 failed）相对基线（14 failed）多出的 2 个
  失败是本轮有意修改的两份契约（AUD-15611 文档声明、MG idle 的播放态
  来源），已随本轮更新并通过；其余失败集合与基线一致（均为 consolidate
  remux / LUT 工程根 / 设备探测等真实媒体顺序依赖用例，在多次运行间
  波动）。

## 真实窗口复现（修复后）

`docs/audit/2026-10-20-edit-fifth-review/records/fix-round/`：

- 媒体池右键添加：`ok=true`，恰好 1V+1A 互链，无 overlap 异常（E01）；
- Edit/Timeline 菜单 7 项全部存在且 enabled（E03）；`Ctrl+Shift+X` →
  `cut_calls=0, split_calls=1`（E03）；
- 关键帧 toggle `ok=true`，无 KeyError（E02）；
- `status_message_requested=1` / `playback_state_changed=1` /
  `open_asset_grade_requested=1` 接收者（E04/E06/E08）；
- 播放 2.5s 内 `playback_state_emissions=2`（E06）；
- 播放中自动化采样 `recorders=1`，停止后经命令栈提交（E05）；
- 菜单 Undo 显示 "Undo Split 2 clips at frame 100" 且驱动时间线栈（E11）；
- 5 个 ClipItem 双击接收者 5/5（E09）；
- 场景检测偏好：配置 0.91/False/2 → 实际 0.75(范围内钳制)/False/2，
  不再硬编码 0.55/True/8（E07）；
- 保存/加载与导出 smoke（H.264 263KB）通过。

## 二次复核（2026-10-20 收口后）

按计划表对 E01–E22 逐项复核（代码标记 + §6.1 测试清单 + §6.2 四个
driver 重跑），发现并修复两处遗漏：

1. **E22 第 3 条未完全落地**：PresetDialog/RoutingMatrixDialog 已有
   DORMANT 标记，但 4 个同样休眠的 AI 面板
   （`ai_job_panel.py` / `ai_decision_inspector.py` / `ai_variant_list.py` /
   `ai_timeline_diff.py`，均"已实现有测试、生产零 import"）漏标。
   已按 `preset_dialog.py` 同款补 `DORMANT(2026-10-20 第五轮审计 E22)`
   头注释。
2. **audit_driver 的 `ctrl_shift_x` 检查依赖窗口激活态**：Split 的
   Ctrl+Shift+X 走菜单 QAction 的 WindowShortcut 分发，要求 mw 是
   QApplication active window；driver 只 `show()` 不激活，后台 shell
   启动时 `split_calls` 假阴性为 0（`cut_calls=0` 的不误剪保证不受影响，
   走 TimelineView 本地按键路径）。已加 `QApplication.setActiveWindow(mw)`
   使分发可复现，`cut_calls=0, split_calls=1` 确定性通过。

二次复核结果：

- 13 个新回归 + 门禁：65 passed；休眠面板消费方
  （test_ai_subpanels / test_ai_job_decision_prefs / test_ai_timeline_diff /
  wd12g65 / wd11g64）：111 passed；
- audit/ops/undo/export 四个 driver 重跑全绿（关键值同上：
  1V+1A 互链、菜单 9 项 enabled、双击接收者 5/5、`recorders=1`、
  `playback_state_emissions=2`、偏好钳制生效、菜单 Undo label、
  导出 H.264 263KB）。
