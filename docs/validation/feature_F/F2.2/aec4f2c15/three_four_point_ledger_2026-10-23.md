# F2.2 高级三/四点、backtime 与 fit-to-fill — 证据台账

- 日期：2026-10-23
- 结论：**已完成（普通 backtime/定长编辑全量；fit-to-fill 变速填充为变调填充）**。
  实时保调填充依赖 F3.2（计划条件依赖原文："不要求普通 backtime/定长编辑等待
  音频保调"）；当前 fit 的音频按既有 retime 语义变调（preserve_pitch 走
  clip 级设置，不在编辑判定层强制）。
- 提交：`aec4f2c15`（feat/f-plan，已 ff-merge fix-ui 并推送）。

## 1. 语义与验收对照（计划 §F2.2）

| 要求 | 落地 | 证据 |
|---|---|---|
| 复用现有 In/Out 与 Insert/Overwrite | 判定层只读 `TimelineSequence.range_start/range_end` 与源 In/Out，命令层复用 InsertClipCommand/OverwriteClipCommand/InsertLinkedAssetRippleCommand 的既有 speed 参数与 fps 四元时基 | timeline_view.py `_apply_source_edit_plan` |
| 目的 In/Out | 时间线 In=落点（三点）；In+Out=四点 | test_three_four_point_editing.py |
| 由目的 Out 倒推（backtime） | 仅 Out：落点 = Out − 1x 时长 + 1，片段恰好结束在 Out | ::test_out_only_backtime_ends_at_range_end / ::test_backtime_ends_at_timeline_out |
| 四点冲突规则 | 源时长 ≠ range 时长 → 冲突描述符（含建议速度、0.1–8.0 边界判定、裁剪可用性=目标更短）；QMessageBox 三选：变速填充/裁剪源/取消；两策略均不可行时状态栏明确反馈并拒绝 | ::test_mismatch_returns_conflict_descriptor / ::test_conflict_fit_speed_applies_speed / ::test_conflict_cancel_writes_nothing |
| 变速方案调用已有时间映射，不直接改 clip 长度掩盖源映射 | fit 只计算 speed 传入命令 create；时长由 timeline_duration_for_source 权威推出；speed 闭式初值+比例校正保证取整后**精确命中**目标帧数 | timeline_ops._speed_for_target_duration + ::test_fit_speed_hits_target_exactly / ::test_fit_speed_mixed_framerate |
| 裁剪源（修源范围） | trim_source 按目标帧数 Fraction 换算裁源窗（入点不动、出点内收），无法反向延长时拒绝 | ::test_trim_source_cuts_source_window / ::test_trim_mixed_framerate_converts_frames / ::test_trim_with_longer_target_rejected |
| 指定片段恰好结束在目标切点 | backtime 与四点 fit/match 均断言 end == range_end | 接线测试 |
| 不同帧率 | 判定层全程 Fraction 四元 fps（48fps 源→24fps 时间线 fit 2.0 / trim 24 帧均有测试） | 同上 |
| 锁轨 | 命令层 assert_track_editable 保持单点拒绝；新增 insert/overwrite 四条执行路径 ValueError → status_message_requested（锁轨/重叠不再向 Qt 槽外抛裸异常） | ::test_insert_on_locked_track_feedback_not_exception |
| Undo | 单命令/宏单步（未新增命令种类）；既有 undo 完整性护栏未回归 | test_edit_transaction_undo_integrity 等 162 项护栏 |
| 导出一致 | 落轨 clip 携带 speed + fps 四元（复用 N18/RF02 链），导出/预览按既有 retime 语义消费 | test_clip_fps_metadata_propagation 护栏 |

## 2. 关键设计决策

1. **判定层为纯函数**（timeline_ops.resolve_source_edit_plan）：无 UI 依赖，
   ask/fit_speed/trim_source 三策略参数化；UI 层（timeline_view）只负责读
   range、弹冲突三选、状态栏反馈。显式 speed≠1.0 的调用（API/自动化）跳过
   判定，保持既有落点语义。
2. **speed 精确命中**：闭式初值（Fraction 源帧/目标帧，混合 fps 换算）+
   按实际取整时长最多 8 次比例校正；无法精确命中返回 None → fit 不可行
   （诚实拒绝而非悄悄差 1 帧）。发现路径含端点：range 闭区间语义与
   resolve_timeline_in_out 一致（end−start+1）。
3. **冲突三选而非静默默认**：计划要求"冲突明确选修源范围/修目的范围/
   变速填充/取消"——修目的范围 = 取消后手动调整时间线 In/Out（弹窗文本
   说明源/目标帧数，状态栏在两策略均不可行时给出完整指引）。
4. **fit 音频策略声明**：当前变调填充（既有 retime 语义）；实时保调填充
   登记为 F3.2 条件依赖（计划原文允许）。

## 3. 测试账本

- tests/edit/test_three_four_point_editing.py：判定层 13 + 接线层 8 + 锁轨
  反馈 1 = 22 项（子类继承再跑父类 8 项共 30 collect）。
- 护栏：insert/overwrite/fps/speed/lift-extract/range-bar/F2.1 门等
  96–162 项全绿（`QT_QPA_PLATFORM=offscreen TOPOS_HEADLESS_GL=1`）。

## 4. B 依赖核对

无 Plan B 依赖：判定层纯 Python + Fraction；接线层与命令层均为跨平台路径。

## 5. 登记的后续项

1. fit-to-fill 实时保调（条件依赖 F3.2 WSOLA 实时化）。
2. Alt+X 清除时间线 range 的键盘入口（调研发现注释声称存在但未实现——
   目前清除走标尺右键菜单；属快捷键补全，登记至 F1.4 后续或顺手片）。
