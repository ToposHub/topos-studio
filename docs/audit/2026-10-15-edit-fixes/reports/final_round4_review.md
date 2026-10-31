# 第四轮最终审查与曲线编辑时钟补修

日期：2026-10-16。已读取 `.clinerules`、`AGENTS.md` 及 nested_audio_gate_final、slide_domain_final、slip_slide_ui_final 报告。重点检查 immutable speed curve / negative handles、严格嵌套音频导出门禁、结构项目事务与 controller/history 原子发布。未提交 Git，未回退工作区成果。

## 确认并修复的缺陷

裁窗后的 `SpeedParams` 使用 `curve_time_offset` 保存完整曲线中的连续时钟。原 `with_speed` 在修改基速时照搬旧时钟，使其在新积分曲线上落到另一源位置。可证复现：100 源帧，基速 1.0，第 80 源帧开始 2.0x；左裁 20 帧后将基速改为 2.0。原裁窗首帧为源相对 0，修改后却变为 20；80 源帧窗口被算成 30 时间线帧，应为 40。Inspector 的单选/批量速度编辑都能调用这一工厂。

在主代理明确追加授权后，仅对 `clip_params.py` 的曲线修改工厂实施补修：旧曲线求当前窗口起点的连续源坐标，再用修改后曲线的逆映射建立新 `curve_time_offset`。保留分数采样 phase、curve_offset、curve_length 和原对象；不将时钟重置为 None。相同修复覆盖经矩阵验证存在同类风险的速度点添加、点速度、点 easing 工厂。reverse、interpolation、preserve_pitch 不改变曲线积分，保持既有行为。

新增 `test_round4_curve_edit_clock.py` 15 项：真实 Trim 后正反向基速修改、首帧固定和准确时长；四类工厂在负 handle、分数时钟、坡内窗口的连续源坐标与 blend phase；原对象不变；JSON 往返；真实 Inspector → controller → TimelineView 单命令提交及 Undo/Redo。

## 其它审查结论

没有确认其它具有可证触发条件的严重回归。严格 nested audio 在创建 encoder 前检查当前导出范围；保留 preview 降级和 video-only 绕过音频的既有语义。项目事务在修改 history 前验证活动 sequence 和 touched project/child keys，冲突 Undo 保留历史，发布后同步项目、父/子序列及面板。

## 验证

补修前独立运行 project_transactions、settings_history、core_time_windows、slide_domain、nested_audio_gate 五文件：**190 passed，21.35 秒**。

补修后运行 curve_edit_clock、core_time_windows、slide_domain、timeline_clip_speed、smooth_speed_ramp、frame_blend、interaction_commit 七文件：**200 passed，19.51 秒**。新增 Inspector 用例已实际通过。

所涉文件 `compileall`、`git diff --check` 通过。审查范围不涉及 GPU 实机验证；本报告不将既有能力限制当作新增缺陷。
