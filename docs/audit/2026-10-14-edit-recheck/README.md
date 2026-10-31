# 2026-10-14 剪辑实施后复查证据

审计 HEAD：`833dcd382d40f3ff75cb31bc3e3374814efa2b1b`。主计划：[新优化计划表](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/edit_page_followup_audit_optimization_plan_2026-10-14.md>)。

这是本轮新增目录，旧计划、旧审计、生产代码及用户测试素材均保留。`audit-scratch.topos` 是本次专用工程，仅引用原素材，未写用户现有工程。

## 可见窗口交互

[驱动脚本](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/interactive_driver.py>) 显示完整 MainWindow，隔离配置和项目；QTest 驱动真实 Qt 按钮、键盘、拖动事件。人工阅读窗口截图进行像素/界面核对，但没有把它描述为人工鼠标操作。`evidence/command-01.py` 到 `command-08.py` 为按顺序执行的动作；`command-99.py` 为退出动作。

[交互记录](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/interaction_records.json>)、[完整应用日志](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/full-app.log>) 和 [未捕获槽异常](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/uncaught-qt-exceptions.log>) 保留了模型前后数据、输入、截图名称及 Source Overwrite 调用链。异常记录钩子只属于审计驱动，不是生产修复；不能以进程继续运行来判断槽异常已被产品处理。

关键记录：

| 文件/记录 | 用途 |
|---|---|
| 07-roll-esc-release.png、roll_escape_release | 真正Roll seam取消后仍提交 |
| 09-source-visible.png、10-source-play.png | Source真正激活、有效视频画面和播放，CPU纹理上传路径 |
| 15-two-selected-one-edit.png、multi_edit_real_control | 两ID上下文保持，属性实际只改一片 |
| 16-locked-property.png、locked_property_real_control | 锁定轨上通过可见属性输入仍修改 |
| 17-source-appended-av.png | 空轨/等尾Append正常，A/V成对 |
| 18-overwrite-failure.png、source_overwrite_occupied | 占用A/V覆盖抛ValueError，未发布Macro |
| 19-insert-audio-fallback.png、source_insert_internal | 内部插入没有新音频、旧linked关系不互逆 |
| 20-program-playback.png、program_playback | 实际Program像素、播放头和有效frame_ready |
| save-reload-diff.json | ZIP重载清除一处由内部Insert产生的非互逆link；不宣称全payload原样通过 |

早期03截图当时Program仍激活，不能证明Source画面；command-03的一次属性输入已变回单选，不能证明多选失败。普通右trim可见尝试未进入drag mode，不当作成功或确定生产bug，主计划G02保留待核查。Qt运行时以command-04的qVersion结果6.10.2为准；驱动startup的编译版本不能冒充运行时。

退出时一次性弹窗处理器没有完成全部确认，进程采样显示`MainWindow.closeEvent → QMessageBox.question → QDialog.exec`等待选择。随后核对PID/命令，仅对审计专用进程发送SIGTERM；见`audit-cleanup.json`、`close-modal-sample.txt`。没有据此确认产品退出死锁，也没有把正常退出写成通过；没有终止用户进程。

重新运行驱动会打开一个新的专用可见窗口。它使用固定临时会话目录 `/tmp/topos-edit-recheck-20261014`，并在本证据目录写结果；重跑时先复制整个审计目录并改SESSION/OUT，避免覆盖原记录。投递动作文件必须先写`.tmp`再rename为`.py`；poll在执行时暂停，防止qWait重入导致动作交错。退出处理器应使用重复定时响应所有确认，而非只处理一次。

## 模型、offscreen交互和分工报告

详细报告在`reports/`；复现脚本和采集输出副本在`evidence/probes/`。K01–K12、RI-01–08、F-R01–09及RENDER-01–03与主计划R01–R31有明确映射；RI-03与K01合并，GPU三个数值失败同一shader根因只计一组。

模型脚本使用真实领域实现；部分controller handler使用轻量宿主/spy，不是完整窗口。offscreen脚本创建真实Qt控件并发送事件；Source GPU有效像素不能用offscreen证明。Alt取消组仅在mouseMove期间提供受控modifier，不是OS持续Alt验证。

脚本副本通常可在项目根用`.venv/bin/python`执行。它们保留原始临时fixture/output路径以保全本轮代码；重跑输出不自动成为新的认证结果。`topos_recheck_features_probe.py`创建的`.mp4/.jpg`是1–3字节元数据路径占位物，仅用于handler/生命周期验证，无视频解码意义，未作为媒体素材复制交付。真实媒体验证来自用户的三个测试视频。

## 测试与导出

[修复批次日志](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/pytest-fixes.log>) 与同名XML：107 passed、1 skipped；skip原因是fixture中ShortcutManager unavailable。实际Qt清空快捷键probe另通过。

[既有失败范围日志](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/evidence/pytest-existing-failures.log>) 与同名XML：168 passed、9 failed；4个数值失败为LUT及GPU primary grade真实问题，5个为no_deblock fixture、孤立Sync Lock状态断言、Metal私有缓存键三类测试维护项。

[导出脚本](</Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-10-14-edit-recheck/export_smoke.py>)、`export-smoke.log`、`export-smoke-results.json`：两条专用时间线各49帧、640×360 H.264，均success、audio_omitted=false。`smoke-*.mp4`保留实际输出；仅为短片服务烟测，未宣称硬件编码、耳听或完整颜色保真通过。

`manifest.json`记录交付文件大小和SHA-256，便于将来核对本次证据。不同分工测试存在重叠，主计划没有将其直接相加。
