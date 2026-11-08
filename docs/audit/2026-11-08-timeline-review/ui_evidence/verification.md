# 2026-11-08 UI 与交互最终验证

最终组合 **286 passed / 19.36s**，0 failed、0 skipped。测试在Qt offscreen平台执行，覆盖真实QTest按下/移动/释放及既有命令。测试中的缺失素材路径是刻意构造的UI夹具，产生OpenCV读失败日志，不代表真实素材验证使用了缺失文件。

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q \
  tests/edit/test_timeline_resolve_ui_review.py \
  tests/edit/test_resolve_timeline_interactions.py \
  tests/edit/test_timeline_interaction_stopgaps.py \
  tests/edit/test_timeline_linked_av_regressions.py \
  tests/edit/test_round4_linked_slip_slide.py \
  tests/edit/test_timeline_panel_zoom_coalescing.py \
  tests/edit/test_presentation_state_and_levels.py \
  tests/edit/test_timeline_shortcuts_migration.py \
  tests/edit/test_timeline_context_menu_guards.py \
  tests/edit/test_change_clip_speed_dialog.py \
  tests/edit/test_timeline_clip_fade.py \
  tests/edit/test_edit_keyframe_tray.py \
  tests/edit/test_timeline_resolve_toolbar.py \
  tests/edit/test_timeline_clip_speed.py \
  tests/edit/test_round4_linked_roll.py \
  tests/edit/test_recheck_a_lock_trim_fade.py \
  tests/edit/test_replace_edit_and_voiceover.py \
  tests/edit/test_clip_flags.py \
  tests/edit/test_position_lock_policy.py \
  tests/edit/test_source_destination_wiring.py \
  tests/edit/test_properties_keyframe_wiring.py \
  tests/edit/test_selection_diagnostics.py
```

组合通过后，真实原生窗口发现变速窗口的checkbox/radio指示器在暗色主题下不清晰。补显式QSS、复用已有`src/shared/assets/icons/checkmark.svg`；随后13项速度窗口用例再次通过（2.55s）。没有重复累加测试数量。

本机原生QA使用隔离的`Topos — Resolve Timeline UI Review`窗口，载入用户提供的`tests/test_videos/2K_h264.mp4`和`测试素材01.mov`；未重启运行中的用户编辑器，未更改其未保存工程。观察到真实缩略图/音频波形、工具布局和电平表保留。电平无伪造活动，独立预览未连接完整音频播放后端。

实际在新速度窗口输入200%，AX确认Speed=200、FPS=50、Duration=00:00:01:00（原100%、25、00:00:02:00），Cancel关闭并返回原时间线；截图确认复选框/单选框与各选项清晰可见。Retime与linked选中轮廓也完成原生截图观察。跨轨/trim/fade/密集关键帧/取消等主要由真实Qt事件与模型验证，未宣称全部Resolve原生鼠标手势都已自动完成。

复看速度窗口：

```bash
.venv/bin/python tools/preview_resolve_timeline.py --speed-dialog
```

后端反例脚本和UI回归区分：B17/B18等探针故意确认缺陷仍存在，返回0是复现脚本完成，不是产品通过。详见新计划表。

组合之后又补充两项快捷键回归与超大整数恢复边界，最后一组专项覆盖UI review 25项、速度窗口13项及zoom coalescing 7项，共45 passed。展示ppf先钳制再转float，scroll先钳制至Qt int范围，避免畸形工程偏好触发OverflowError。此45项与286项存在重叠，不相加。完整上方命令若在当前工作树执行会包含后来新增的3项。

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/edit/test_timeline_resolve_ui_review.py tests/edit/test_change_clip_speed_dialog.py tests/edit/test_timeline_panel_zoom_coalescing.py
```
