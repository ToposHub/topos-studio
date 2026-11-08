# 后端收尾复审验证

审查基准：`7dee505c`；当前修改留在工作树。日期：2026-11-09。

用户完成的全量结果、基线归因与本轮复审分开记录；本轮不重复跑12k全量。所有观察探针exit0只表示采集完成。新计划：[时间线后端收尾复审](../../plans/timeline_backend_recheck_2026-11-09.md)。

| 验证 | 结果 | 原始记录 |
|---|---|---|
| 后端+UI组合 | 645 passed / 25.41s，34文件 | [组合输出](focused_645.txt) |
| 最后追加倒放/冻结签名 | 29 passed / 6.97s，2文件，与上组重叠 | [追加输出](render_signature_29.txt) |
| 新命令反序列化反例（修前） | 4 failed / 1 passed | [修前](command_roundtrip_before.txt) |
| 命令/曲线/速度回归（修后） | 35 passed / 5.79s | [修后](command_roundtrip_after.txt) |
| 音频签名与Retime UI | 47 passed / 6.56s | [小修回归](small_fixes_after.txt) |
| 用户f1acf2b全量基线 | 12073 passed / 20 failed /117 skipped | [缓冲日志末尾备份](user_baseline_final_tail.txt) |

最初音频curve signature与smooth菜单勾选的两个新断言修前失败；之后通过。没有放宽像素/hover断言，也没有用加sleep处理用户归因的负载抖动。

## 组合命令

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/edit/test_backend_review_command_roundtrip.py tests/edit/test_audio_signature_completeness.py tests/edit/test_fade_preview_signature_regression.py tests/edit/test_timeline_retime_controls.py tests/edit/test_fade_curve.py tests/edit/test_move_speed_point.py tests/edit/test_speed_settings_reception.py tests/edit/test_alt_copy_group_plan.py tests/edit/test_linked_numeric_trim.py tests/edit/test_voiceover_completion.py tests/edit/test_replace_edit_and_voiceover.py tests/edit/test_rate_stretch.py tests/edit/test_timeline_clip_speed.py tests/edit/test_presentation_state_and_levels.py tests/edit/test_audio_playback.py tests/edit/test_position_lock_policy.py tests/edit/test_clip_flags.py tests/edit/test_embedded_trim_monitor.py tests/edit/test_timeline_playhead_style.py tests/edit/test_timeline_clip_affordance_gestures.py tests/edit/test_timeline_view_layout.py tests/edit/test_resolve_timeline_interactions.py tests/edit/test_timeline_host_drag_regression.py tests/edit/test_timeline_pointer_and_chrome.py tests/edit/test_change_clip_speed_dialog.py tests/edit/test_timeline_resolve_ui_review.py tests/edit/test_trim_professional.py tests/edit/test_round4_linked_roll.py tests/edit/test_round3_a_roll_focus.py tests/edit/test_subtitle_layout_persistence.py tests/edit/test_trim_dual_frame.py tests/edit/test_round4_interaction_commit.py tests/edit/test_program_viewer_toolbar.py tests/edit/test_edit_page_playback_preview.py
```

追加命令：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/edit/test_fade_preview_signature_regression.py tests/edit/test_edit_page_playback_preview.py
```

## 原生UI

- 本机Resolve Studio20：再次开关retime条，观察标题/箭头/底部百分比与24px展开；未保存或修改速度。底部菜单的CUA点击未捕获展开状态，不能声称逐项菜单原生验证完成。
- 隔离Topos预览：`tools/preview_resolve_timeline.py --viewer`加载用户tests/test_videos素材，实际ProgramViewer内显示两侧边界画面。L显示Δ+1，J回Δ0，时间线播放头保持00:00:04:00；Esc收起覆盖层，电平表保留。未关闭或重启用户原应用。
- 覆盖层下普通节目画布的真实HDR、调色与输出一致性仍属R07，独立预览工具不是完整主程序渲染会话。

## 独立反例

- [速度probe](speed_review_probe.py) / [当前观察](speed_review_results.json)：伙伴曲线、转场undo、短源stretch仍有问题；UI小修后坐标反例已关闭。
- [淡变probe](fade_review_probe.py) / [当前观察](fade_review_results.json)：签名和宏registry反例修后关闭，future_curve保存丢失仍待修。
- [事务probe](transaction_review_probe.py) / [观察](transaction_review_results.json)：录音时码、双自动轨组复制仍待修，B19下游联动观察正常。

compileall及git diff --check通过。只读二次Review确认本轮小修未发现新问题；大问题依新计划后续处理。
