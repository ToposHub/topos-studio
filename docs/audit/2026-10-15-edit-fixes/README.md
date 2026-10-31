# 第四轮剪辑复查：修复与验证证据

执行日期：2026-10-15 至 2026-10-16。代码基线 `8330e42df`。主记录：[第四轮修复记录](../../edit_page_fourth_audit_fixes_2026-10-16.md)，兼容性：[Edit v4 采样时钟](../../edit_sampling_phase_v4.md)。这是修复记录，历史三轮计划和证据未覆盖。

## 最终结果

最终 Edit 回归：**10,606 passed，20 skipped，0 failed / 0 error**（10,613 个分片用例加独立生命周期 13 项；分片 605 文件，另有生命周期 1 文件）。七个分片均退出 0 且未超时。汇总见 [final-validation-summary.json](validation/final-validation-summary.json)，原始日志/XML 在 `validation/final-edit-batches-v2/`。

各分工报告、focused 测试与最终全集存在重叠，不能累加成独立测试数。`skipped` 仍是未执行项，不能算通过；具体原因保存在 JUnit XML。未运行整个仓库所有产品模块。

## 真实完整窗口交互

`interactive_driver.py` 使用生产 `src.main` 的原生库隔离和 Qt 启动顺序、完整 `MainWindow`、macOS `cocoa`，在独立配置和新建临时工程中发真实 QTest 鼠标、键盘、拖放事件。没有打开或改写用户既有工程，没有复制/修改 `tests/test_videos/` 原素材。运行时 Qt 为 6.10.2，编译标记为 6.10.0。

最终记录位于 [gui-session3/interaction_records.json](gui-session3/interaction_records.json)，可执行输入为同目录 `command-*.py`，窗口截图保留在同目录。验证内容：

| 路径 | 实际操作与断言 |
|---|---|
| 媒体池→Source | 拖入提供的 3 个 2K 素材；双击 H264 素材；首帧绘制未失败；真实 Source 播放由 100 前进到 123；I/O、Clear、单帧范围按钮状态正确 |
| Source→时间线 | 素材链接 A/V 拖入；追加、覆盖占用区域、内部插入；最终 12 个片段全有 reciprocal link，`validate_timeline` 无问题 |
| Roll | 新建短时间线，实际 Roll 工具鼠标拖 seam；视频和音频边界同步；一步 Undo 精确恢复全部内容 |
| Slip/Slide | 实际工具按钮与片段鼠标拖动；视频、音频与邻居几何/源窗口同步；各一步 Undo 精确恢复 |
| 取消 | Slip 拖动中按 Esc，随后迟到的 mouseRelease 不产生提交 |
| 属性 | 两个片段通过显式可见数值输入器输入 X=55、Y=66；Undo 后继续编辑 Y=88，X 保持 55 |
| 设置/历史 | 实际设置对话框 25→50 fps，三段几何缩放为 0–100/100–200/200–300；随后普通属性编辑，Undo 属性→Undo 设置精确回到旧内容；切换另一时间线 Undo 不污染当前时间线 |
| Nest 项目事务 | 选区由 fixture 设置，然后调用实际右键动作槽；素材/时间线 ref/子序列各增 1；一次 Undo 全部撤销，一次 Redo 恢复相同内容和 ID |
| 工程持久化 | 通过正式 `EditProjectIO` 保存 `.topos` 并重读，3 个序列的完整 `to_dict()` 载荷完全相同，项目 revision 保持一致 |
| 关闭 | 正常关闭窗口、处理本审计工程的放弃保存对话框，记录 `window_visible=false`，走生产 orderly shutdown |

这里的真实事件是完整 Qt 应用自动交互，不冒称人工逐项操作。Nest 的选区与动作槽接线、工程保存使用生产接口，不计作右键菜单定位测试。声轨通过解码和数值验证，未宣称耳听验收。

### 真实导出

[export_smoke.py](export_smoke.py) 从上述已保存工程加载三个序列（含 Nest 父/子），通过 `TimelineExporter` 正式服务分别导出 49 帧、640×360、H264/AAC 短片，全部 `success=true`、`audio_omitted=false`。输出短片、导出 manifest 和 [export-smoke-results.json](gui-session3/export-smoke-results.json) 均保留。

另用 [probe_exports.py](probe_exports.py) 的独立 PyAV 进程重新解码三个文件，确认每个 49 帧视频、AAC 95,232 个解码样本、非零音频 RMS，见 [export-probe-results.json](gui-session3/export-probe-results.json)。解码样本含 AAC 编码填充，不当作时长误差证明。GPU primary 数值路径另见调色回归：实际 OpenGL 预览/导出与 Metal worker、uint8/uint16/float、alpha、HDR 超白/负值；随机 150 个合法轮/暗部/饱和度组合中 68 个被快路接受，最大 uint8 code 偏差为 0，见 `validation/color-random-domain.json`。

## 自动回归与复现

最终分片入口（独立子进程、每片最多 90 个文件、360 秒上限、90 秒 faulthandler、隔离配置）：

```sh
.venv/bin/python docs/audit/2026-10-15-edit-fixes/run_edit_validation.py final-edit-batches-v2
```

命令会创建指定证据子目录；复现时请换新 label，避免覆盖历史日志。单进程 `tests/edit` 首次聚合曾在 FFmpeg decoder `__del__ → container.close` 原生生命周期处停止推进，见 `validation/edit-full.log`；只终止当次审计进程后改为分片。生命周期文件 `test_p2_09_session_pool.py` 单独进程执行，13 passed，见 `validation/decode-lifecycle.log/xml`，最终分片不重复收集此文件。**不宣称单进程全量已通过，也没有把原生聚合挂起解释为已修复的产品根因。**

早期与中间分片保留原失败证据（`validation/edit-batch-*`、`validation/final-edit-batches/`）；其中涉及真实产品缺陷、已更新的旧契约、未初始化替身或配置隔离问题，均通过具体修复/更准确的契约回归处理。最后一轮的两个替身问题是导出性能测试绕过构造器未提供 sequence resolver、Slide 取消测试绕过 press 快照入口；改用真实模型/真实 press 入口，未放宽产品校验。

额外 UI 回归 `tests/ui/test_ef03_aud_part1.py`：51 passed，见 `validation/ui-extra-final.log/xml`。跨功能 controller、调色关键帧/剪贴板与共享 DragValue：159 passed，见 `validation/crossfeature-final.log/xml`；项目包 schema、模块保留、project document 与 Color state roundtrip：93 passed，见 `validation/package-final.log/xml`。这些文件位于 Edit 测试目录外，单独列出。最后补修的 focused 证据为 `validation/audio-perf-final.log/xml`（43 passed）、`validation/gesture-clock-final.log/xml`（61 passed），同样与全集重叠。

## 审查报告索引

- [核心最终审查](reports/core_review.md)
- [最终曲线编辑时钟审查](reports/final_round4_review.md)
- [Slide 源域、smooth 扩展、混合采样](reports/slide_domain_final.md)
- [链接 Slip/Slide 真实 Qt 交互](reports/slip_slide_ui_final.md)
- [严格嵌套音频与 File/Deliver Stem 接线](reports/nested_audio_gate_final.md)
- [预览/交付调色审查](reports/render_review.md)

其它 `reports/*.md` 保存阶段分析与 focused 结果。最终结果以前述最终全集、完整窗口 JSON 和导出探测为准。

## 脚本错误与能力边界

- session1 的第一次属性输入没有命中活动 editor，是审计 fixture 错误；同轮显式输入器复测通过。session2 的 `create_` NameError 来自审计脚本错误替换 timer 代码，未修改产品；其后成功执行并暴露真实 Roll 只改视频的问题，现由链接 Roll 实现及 session3 复测关闭。
- session3 初次设置用 `setCurrentText('50')`，真实选项文本为 `50 fps`，因此没有改帧率；改为按 50.0 的 item data 查找后，实际设置及连续 Undo 测试通过。建序列的自动接受发生过 Cocoa 模态初始化竞态，审计重新显示并延迟结束模态，后续窗口事件正常；不将其称为产品缺陷修复。脚本断言及恢复记录保留。
- 父 Nest 音频变速/反放/曲线及跨 FPS 音频映射仍有明确能力限制，带音频交付会在编码前拒绝并返回结构化原因；video-only 不受此门禁影响。跨 FPS 速度曲线换源/切角同样先拒绝。它们不再静默产生错误或无声“成功”，但不能声明完整音频 retime/全部多机位组合已实现。
- 本轮不是长时 VFR/4K60 压力、所有硬件编码器、外部软件往返、多设备音频漂移或全专业功能清单的完成证明。缺失设施仍依照第三轮功能计划推进。

保存可复现源脚本、日志、截图、测试工程和短导出；删除的仅为本轮生成且可重建的 `.run-configs`、`.topos_cache` 与锁文件，未删除用户素材。
