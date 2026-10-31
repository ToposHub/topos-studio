# F2.4 普通 Transform/Opacity 动画与曲线入口 — 证据台账

- 日期：2026-10-23
- 结论：**核验通过 + 1 项真实缺陷修复（变速关键帧重映射）**。按计划
  原文"已有入口若验收通过则只记录覆盖，不重复开发"，八个验收点中七项
  已有实现与测试覆盖；两项入口类缺口（前后关键帧导航、插值/曲线编辑
  UI）登记后续片。
- 提交：`2b8036309`（feat/f-plan，已 ff-merge fix-ui 并推送）。

## 1. 核验结论（验收点 × 现状）

| 验收点 | 结论 | 证据 |
|---|---|---|
| 位置 -250/500、角度 -45/180 不被夹 0–100 | ✅ 已满足（C01 命令层权威值域 CLIP_PARAM_RANGES ±8192/±3600 + R11 旧工程加载修复；求值/GPU/CPU/导出端无二次夹取） | tests/edit/test_fix_round6_phase2.py（C01 参数化 -250/500/-45/180 全保值）、test_fix_round7_r11_legacy_ranges.py |
| opacity 值域 | ✅ 0–1（UI 0–100% 归一化写入，导出端合法钳 0–1） | 同上 + R10 测试 |
| 普通属性 ◆ 入口 | ✅ 六参数齐全（translate x/y、scale x/y、rotation、opacity；KeyframeIndicatorButton 三态） | G16/R10 + test_properties_keyframe_wiring.py |
| 锁轨零写入 | ✅ AddKeyframe/UpdateClipParam assert_track_editable（C06/R01） | test_fix_round6_phase1c_commands.py |
| add/remove/Undo/保存恢复 | ✅ 四命令 + 宏注册 + roundtrip | 上述测试集（85 项护栏全绿） |
| 跨轨移动关键帧迁移 | ✅ C02 with_temporal_shift（clip+effect），undo 对称（R14） | test_fix_round7_r14_move_undo.py |
| 预览/导出一致 | ✅ 同走 TimelineRenderPlan 逐帧求值 | test_keyframe_easing.py |
| 程序化同步控件状态锁 | ✅ `_updating` 锁模式 | test_program_refresh_does_not_write_back |

## 2. 本次修复的真实缺陷：变速关键帧错位

- 现状（修复前）：`ChangeClipSpeedCommand` 只改 speed/timeline_duration/
  fade，**不触碰 animated_params**——关键帧留在原绝对帧位，变速重锚后
  曲线与画面错位（计划原文"变速/拆分按 canonical 时间映射迁移动画"未满足）。
- 修复：`ClipInstance.with_animation_time_remapped(old_dur, new_dur)`——
  关键帧在 clip span 内比例重映射（`shifted(-start) → time_scaled(Fraction
  new/old) → shifted(start)` 组合，Fraction 精确，handle 增量同比例，
  多帧合并沿用 time_scaled "保留后值"）；`EffectInstance.
  with_animated_params_remapped` 同语义；apply（主片+链接伙伴）与 undo
  （反向映射）对称。
- 测试：tests/edit/test_timeline_clip_speed.py::TestSpeedAnimationRemap
  （2x 比例落位 / 1x 恒等 / 0.5x+undo 往返恢复原帧位）。

## 3. 登记的后续片（不阻塞本任务验收）

1. **前后关键帧导航（◀◆/◆▶）**：edit 页无入口；可移植 composite
   keyframes_panel `_goto_prev/_goto_next_keyframe` + J/K 方案。
2. **插值/曲线编辑 UI**：`SetKeyframeInterpolationCommand` 完整且经测试
   但 src 内零 UI 调用；共享 SplinePanel 只接 composite node graph，接
   edit clip animated_params 是主要开发量。
3. 次要：UI rotation ParamDef ±180 与数据域 ±3600 不一致（>180° 仅可经
   命令/API 写入）；`transform.pivot_*` 为静态值无动画入口。
4. 拆分：绝对帧域下两半段共享完整轨道各自 span 内求值正确（无丢失）；
   如需按半段局部化关键帧另行登记。

## 4. B 依赖核对

无 Plan B 依赖（纯模型/命令层 + 既有测试路径）。
