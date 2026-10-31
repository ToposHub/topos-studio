# 剪辑页第五轮全功能复查证据

日期：2026-10-20
基线：`4e4c8b461`（分支 `fix-ui`）
主计划：[edit_page_fifth_audit_optimization_plan_2026-10-20.md](../../edit_page_fifth_audit_optimization_plan_2026-10-20.md)

## 1. 本轮做了什么

用生产启动路径创建完整 `MainWindow`（macOS cocoa，1440×1000），在隔离配置和临时工程中：

1. 新建 Edit 工程；
2. 通过媒体池导入真实素材 `tests/test_videos/2K_h264.mp4`（1920×1080、25 fps、1572 帧、62.89 s、AAC 2 声道）；
3. 走媒体池右键“添加到时间线”的实际 handler；
4. 走时间线 Split/Undo/Redo、复制/粘贴、标记、转场、属性 Transform；
5. 通过属性面板命令添加 saturation 效果并触发关键帧 ◆；
6. 发真实 `Ctrl+Shift+X` 键盘事件；
7. 检查播放 2.5 s 的播放头与播放状态信号；
8. 读取 Scene Cut 偏好并调用控制器默认设置；
9. 通过生产 `EditProjectIO` 保存并重读 `.topos`；
10. 通过 `TimelineExporter` 导出 48 帧 640×360 H.264/AAC；
11. 用 `ffprobe -count_frames` 复核输出帧数与音轨。

本轮没有修改产品代码、既有测试或用户素材；新增内容只有本目录与主计划文档。

## 2. 运行环境

| 项 | 值 |
|---|---|
| OS | macOS darwin 24.3.0 x64 |
| Python | 3.12.13（`.venv/bin/python`） |
| Qt | 运行时 6.10.2 / 编译 6.10.0 |
| 平台插件 | cocoa |
| 窗口 | 1440×1000 |
| 素材 | `tests/test_videos/2K_h264.mp4` |
| 配置 | 隔离的临时 `TOPOS_CONFIG_PATH` |
| 工程 | 临时自动创建，未覆盖用户工程 |

`QT_QPA_PLATFORM=offscreen` 在完整 `MainWindow` 下会在 `FilesPanel` 的 QWebEngineView 构造处 abort，因此完整窗口改用 cocoa；纯导出脚本使用 offscreen，与上一轮证据一致。

## 3. 结果摘要

| 检查 | 结果 |
|---|---|
| 导入 | 1 素材，1572 帧，25 fps，2 声道 |
| 时间线 | 自动 V1+A1，追加后 1 V + 1 A 互链 |
| 媒体池右键添加到时间线 | **失败**：第二次音频追加抛 `ValueError: New clip overlaps ... at [1572, 3144)`；第一对 A/V 已落库 |
| 播放 | 2.5 s 内 0→66 帧；`playback_state_changed` 发射 0 次 |
| Split `Ctrl+B` | 1V+1A→2V+2A，link 完整 |
| Undo/Redo | 2V+2A→1V+1A→2V+2A |
| 复制/粘贴 | 2V+2A→3V+2A（单片段选择；未复制链接音频，link 清空） |
| 标记 | 1 个 |
| 转场 | Cross Dissolve 1 个；Undo 后 0 |
| Transform X | 0→55→Undo 0 |
| 效果添加 | saturation 添加成功，param=`drive` |
| 效果关键帧 ◆ | **失败**：`KeyError: 'AddKeyframeCommand'` |
| 普通片段关键帧 ◆ | **无接收者**：`keyframe_toggled` 接收者 0，`effect_keyframe_toggled` 接收者 1 |
| Deliver 菜单 | 只剩“导出字幕 (SRT)...”；Export Current Source/Settings/Timeline/Composite、Queue Start/Stop 均不存在 |
| `Ctrl+Shift+X` | `cut_calls=1, split_calls=0`（Split 未执行） |
| 菜单 | Edit/Timeline 只剩 Ripple Delete、Delete、Add Marker、Scene Cut Detection、场景检测覆盖层、新建占位片段 |
| 时间线错误反馈 | `status_message_requested` 接收者 0 |
| 媒体池图片调色入口 | `open_asset_grade_requested` 接收者 0 |
| 片段双击 | 5 个 ClipItem 的 `clip_double_clicked` 接收者均为 0 |
| 自动化 | `EditPage._is_playing` 不存在；采样前后 automation 不变；recorder 0 |
| 场景检测偏好 | 配置 0.91/False/2，实际默认 0.55/True/8 |
| 保存/加载 | 4,219 字节；重载 1 素材、1 时间线、1 序列 |
| 导出 | 成功；48 帧、640×360、H.264 VideoToolbox、AAC 48 kHz 双声道、1.94 s、50,900 字节 |

## 4. 定向回归

两批合计执行 311 项，全部通过；其中 4 个文件重复执行，去重后约 294 项：

- 批 1（69 项）：`test_edit_keyframe_effect_commands`、`test_timeline_command_regressions`、`test_shortcut_scope_gates`、`test_effect_keyframe_coordinate_contract`、`test_scene_cut_session_memory`、`test_timeline_context_menu_guards`。
- 批 2（242 项）：`test_media_pool*`、`test_edit_page_playback_preview`、`test_end_to_end_playback_real_videos`、`test_export*`、`test_caption*`、`test_scene_cut*`、`test_edit_effects_panel_drag_inject`、`test_edit_keyframe_effect_commands`、`test_shortcut*`、`test_timeline_context_menu_guards`。

注意：这些测试验证的是局部信号发射/模型结果；它们没有覆盖 `MainWindow`/`EditController` 的宿主连接，因此本轮多个“有信号、无接收者”问题在全绿测试下仍然存在。新增回归必须从真实宿主或至少从实际接线后的对象开始。

全量 `tests/edit` 本轮未重跑（收集 10,626 项）；上一轮的 10,606 项通过仅作为历史基线。

## 5. 文件说明

| 路径 | 内容 |
|---|---|
| `records/main.json` | 主链路动态记录（导入、添加、菜单、接收者、关键帧、快捷键、自动化、播放、场景检测、保存/加载） |
| `records/ops.json` | 编辑操作（Split/Undo/Redo、复制粘贴、标记、转场、双击接收者、Transform） |
| `records/undo.json` | 菜单 Undo 状态与键盘分派 |
| `records/export.json` | 导出结果 |
| `logs/main.log` | 主链路日志（含媒体池 overlap 异常） |
| `logs/ops.log` | 编辑操作日志 |
| `logs/undo.log` | Undo 路由日志 |
| `logs/export.log` | 编码器/导出日志 |
| `logs/receiver.log` | 属性关键帧接收者与 Deliver 菜单实际列表 |
| `scripts/audit_driver.py` | 主链路完整窗口脚本（当轮副本，ROOT 为本机绝对路径） |
| `scripts/ops_driver.py` | 编辑操作脚本（当轮副本） |
| `scripts/undo_driver.py` | Undo 路由脚本（当轮副本） |
| `scripts/export_check.py` | 导出 smoke 脚本（当轮副本） |
| `scripts/receiver_check.py` | 属性关键帧接收者与 Deliver 菜单检查（当轮副本） |

脚本中的 `/tmp/topos-edit-audit-1020` 与项目绝对路径是当轮运行位置；复现时请先把 `ROOT` 改为你的项目根，并为 `SESSION/OUT` 换一个新的临时目录，避免覆盖本目录证据。

## 6. 已确认未覆盖

- 4K60、长时 VFR/HDR、耳听、多设备音频漂移；
- 所有硬件编码器与外部软件往返；
- 多机位长片、Nest 多层音频 retime、跨 FPS 速度曲线换源；
- 关闭流程的完整人工验收（本轮脚本用 `os._exit` 收尾）；
- GPU 纹理逐像素断言（本轮只确认播放头推进与导出文件帧数）。

这些不因本轮通过而视为已验收。
