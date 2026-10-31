# 计划02 覆盖率基线（P0 产出，2026-09-29）

- 运行范围：tests/ 下 18 个分片（color/core/shared/edit/composite/render/media/project/export/deliver/app/video/video_denoise/vault/scripting/architecture/feature_availability/file_explorer）
- 排除 marker：gpu_renderer/webengine/html_renderer/slow/real_media/visual/unstable/real_provider/acceptance
- 未运行分片：ai、integration、benchmark、motion、native、tests 根（机器时间预算；见 PROGRESS.md）
- 测试结果：21,573 通过 / 31 既有失败（0.14%，未修改任何既有测试）/ 356 跳过
- **总体行覆盖率：55.0%**（src/，372,370 语句，1463 文件）

## 目录聚合（按缺失行数降序 Top 25）

| 目录 | 语句数 | 缺失行 | 覆盖率估算 | 文件数 |
|------|--------|--------|-----------|--------|
| `src/features/edit` | 116,328 | 39,243 | 66.3% | 358 |
| `src/features/composite` | 79,683 | 36,966 | 53.6% | 293 |
| `src/app/controllers` | 23,766 | 16,068 | 32.4% | 19 |
| `src/features/teaching_animation` | 29,406 | 15,163 | 48.4% | 169 |
| `src/features/color` | 26,608 | 13,391 | 49.7% | 98 |
| `src/features/ai_harness` | 11,086 | 8,105 | 26.9% | 72 |
| `src/features/ai_workflow` | 7,889 | 7,112 | 9.8% | 63 |
| `src/shared/ui` | 10,241 | 6,259 | 38.9% | 53 |
| `src/shared/media` | 17,003 | 5,208 | 69.4% | 77 |
| `src/app/ui` | 4,456 | 3,963 | 11.1% | 12 |
| `src/shared/video` | 9,957 | 3,831 | 61.5% | 33 |
| `src/shared/ai_models` | 3,447 | 2,014 | 41.6% | 18 |
| `src/shared/export` | 6,493 | 1,807 | 72.2% | 20 |
| `src/features/deliver` | 5,039 | 1,538 | 69.5% | 22 |
| `src/shared/color` | 2,503 | 1,017 | 59.4% | 7 |
| `src/shared/project` | 3,021 | 997 | 67.0% | 16 |
| `src/app/main_window.py` | 1,159 | 942 | 18.7% | 1 |
| `src/features/scripting` | 1,161 | 813 | 30.0% | 5 |
| `src/shared/codec` | 3,124 | 572 | 81.7% | 9 |
| `src/shared/io` | 678 | 539 | 20.5% | 2 |
| `src/app/integrations` | 492 | 407 | 17.3% | 4 |
| `src/shared/gpu` | 2,066 | 293 | 85.8% | 24 |
| `src/shared/tracking` | 1,536 | 240 | 84.4% | 14 |
| `src/features/shared` | 1,379 | 141 | 89.8% | 10 |
| `src/main.py` | 130 | 130 | 0.0% | 1 |

## 低覆盖文件（语句≥200 且覆盖<40%，按缺失行降序 Top 60）

| 文件 | 覆盖率 | 语句 | 缺失 |
|------|--------|------|------|
| `src/features/composite/ui/composite_viewport.py` | 31% | 6,308 | 4,332 |
| `src/features/composite/ui/composite_page.py` | 35% | 5,534 | 3,601 |
| `src/app/controllers/edit_controller.py` | 32% | 5,030 | 3,398 |
| `src/app/controllers/file_controller.py` | 22% | 3,935 | 3,070 |
| `src/features/color/ui/canvas.py` | 29% | 3,706 | 2,617 |
| `src/features/composite/ui/node_properties_panel.py` | 38% | 4,168 | 2,587 |
| `src/features/composite/nodes/scene3d_node.py` | 13% | 2,788 | 2,419 |
| `src/features/teaching_animation/module.py` | 10% | 2,686 | 2,405 |
| `src/features/color/core/async_renderer.py` | 32% | 3,550 | 2,399 |
| `src/app/controllers/ai_edit_controller.py` | 39% | 3,594 | 2,199 |
| `src/features/composite/ui/node_editor_widget.py` | 26% | 2,876 | 2,125 |
| `src/features/edit/core/edit_gpu_preview_renderer.py` | 23% | 2,411 | 1,858 |
| `src/features/teaching_animation/ui/motion_graphic_editor_window.py` | 10% | 1,809 | 1,620 |
| `src/features/edit/ui/ai_edit_assistant_panel.py` | 40% | 2,176 | 1,316 |
| `src/app/controllers/dock_layout_controller.py` | 21% | 1,616 | 1,282 |
| `src/app/ui/preferences_dialog.py` | 7% | 1,333 | 1,240 |
| `src/features/teaching_animation/ui/mg_keyframes_panel.py` | 12% | 1,403 | 1,233 |
| `src/features/composite/ui/graph_editor_view.py` | 15% | 1,153 | 981 |
| `src/app/ui/script_panel.py` | 10% | 1,089 | 980 |
| `src/app/main_window.py` | 19% | 1,159 | 942 |
| `src/app/controllers/menu_toolbar_controller.py` | 10% | 1,021 | 920 |
| `src/features/composite/ui/spline_view.py` | 17% | 1,044 | 865 |
| `src/app/controllers/deliver_controller.py` | 19% | 1,004 | 809 |
| `src/features/composite/render/composite_renderer.py` | 32% | 1,173 | 799 |
| `src/app/controllers/color_controller.py` | 32% | 1,135 | 777 |
| `src/features/edit/ui/ai_preferences_page.py` | 11% | 808 | 721 |
| `src/features/scripting/api.py` | 25% | 839 | 627 |
| `src/features/teaching_animation/ui/scene_inspector.py` | 12% | 696 | 615 |
| `src/app/controllers/ai_edit_host_bridge_impl.py` | 36% | 954 | 608 |
| `src/features/deliver/ui/deliver_page.py` | 0% | 598 | 598 |
| `src/app/ui/project_manager_dialog.py` | 12% | 675 | 597 |
| `src/features/composite/ui/node_editor_actions.py` | 14% | 645 | 552 |
| `src/shared/io/file_io.py` | 20% | 676 | 539 |
| `src/features/composite/render/ai_compute_manager.py` | 18% | 635 | 519 |
| `src/features/color/ui/layer_panel.py` | 0% | 512 | 512 |
| `src/features/ai_workflow/runtime/workflow_run_controller.py` | 0% | 494 | 494 |
| `src/features/composite/ui/keyframe_editor.py` | 15% | 543 | 461 |
| `src/shared/ui/widgets/hsl_curves_widget.py` | 12% | 519 | 456 |
| `src/features/composite/ui/backdrop_item.py` | 11% | 481 | 427 |
| `src/features/color/core/lut_exporter.py` | 25% | 568 | 426 |
| `src/shared/ui/widgets/curves_widget.py` | 10% | 471 | 425 |
| `src/features/color/ui/color_wheel.py` | 35% | 648 | 419 |
| `src/app/controllers/composite_controller.py` | 39% | 675 | 411 |
| `src/shared/ai_models/inference_engine.py` | 14% | 475 | 410 |
| `src/features/teaching_animation/ai/command_catalog.py` | 24% | 536 | 405 |
| `src/features/teaching_animation/ui/asset_library_panel.py` | 15% | 458 | 390 |
| `src/shared/ui/playback_hud.py` | 0% | 389 | 389 |
| `src/app/ui/library_panel.py` | 12% | 438 | 385 |
| `src/features/composite/ui/connection_item.py` | 32% | 526 | 359 |
| `src/features/teaching_animation/ui/scene_view.py` | 14% | 411 | 353 |
| `src/features/composite/render/composite_render_output.py` | 28% | 477 | 343 |
| `src/features/color/ui/blur_sharpen_panel.py` | 0% | 339 | 339 |
| `src/features/ai_harness/plugin_isolation.py` | 13% | 390 | 338 |
| `src/features/color/ui/denoise_panel.py` | 0% | 337 | 337 |
| `src/features/composite/ui/sticky_note_item.py` | 13% | 382 | 333 |
| `src/features/teaching_animation/ui/layer_tree_panel.py` | 16% | 395 | 330 |
| `src/features/color/core/denoise_engine.py` | 0% | 328 | 328 |
| `src/shared/media/macos_vt/vt_zero_copy_probe.py` | 19% | 402 | 325 |
| `src/features/ai_harness/tests/test_runtime.py` | 0% | 324 | 324 |
| `src/features/ai_workflow/ui/workflow_panel.py` | 14% | 376 | 323 |

（满足条件的低覆盖文件共 134 个；完整数据见 coverage.json）