# 计划02 覆盖率缺口报告（P1 产出）

排序依据：缺失行数 ×（1 + 0.15×该文件 Critical/Major 审查发现数，来自 plan-01 台账，经 `git show plan/01-audit:...` 只读引用）——低覆盖与已知缺陷风险交叉加权；仅纳入语句≥150 且覆盖<60% 的文件。

| # | 文件 | 覆盖率 | 语句 | 缺失 | 审查C+M |
|---|------|--------|------|------|---------|
| 1 | `src/features/composite/ui/composite_viewport.py` | 31% | 6,308 | 4,332 | 6 |
| 2 | `src/app/controllers/edit_controller.py` | 32% | 5,030 | 3,398 | 9 |
| 3 | `src/features/composite/ui/composite_page.py` | 35% | 5,534 | 3,601 | 7 |
| 4 | `src/features/edit/ui/timeline_view.py` | 47% | 7,750 | 4,077 | 5 |
| 5 | `src/features/color/ui/canvas.py` | 29% | 3,706 | 2,617 | 9 |
| 6 | `src/features/edit/ui/media_pool_panel.py` | 47% | 5,621 | 2,973 | 7 |
| 7 | `src/app/controllers/file_controller.py` | 22% | 3,935 | 3,070 | 6 |
| 8 | `src/features/composite/ui/node_properties_panel.py` | 38% | 4,168 | 2,587 | 8 |
| 9 | `src/features/edit/ui/timeline_items.py` | 49% | 5,364 | 2,739 | 6 |
| 10 | `src/app/controllers/ai_edit_controller.py` | 39% | 3,594 | 2,199 | 9 |
| 11 | `src/features/composite/nodes/scene3d_node.py` | 13% | 2,788 | 2,419 | 7 |
| 12 | `src/features/color/core/async_renderer.py` | 32% | 3,550 | 2,399 | 6 |
| 13 | `src/features/composite/ui/node_editor_widget.py` | 26% | 2,876 | 2,125 | 7 |
| 14 | `src/features/teaching_animation/module.py` | 10% | 2,686 | 2,405 | 5 |
| 15 | `src/features/color/core/image_engine.py` | 45% | 2,569 | 1,406 | 12 |
| 16 | `src/features/edit/core/edit_gpu_preview_renderer.py` | 23% | 2,411 | 1,858 | 6 |
| 17 | `src/features/teaching_animation/ui/motion_graphic_editor_window.py` | 10% | 1,809 | 1,620 | 5 |
| 18 | `src/app/ui/preferences_dialog.py` | 7% | 1,333 | 1,240 | 8 |
| 19 | `src/features/edit/ui/edit_properties_panel.py` | 60% | 2,538 | 1,025 | 9 |
| 20 | `src/shared/video/video_playback.py` | 45% | 2,091 | 1,158 | 7 |
| 21 | `src/features/edit/ui/ai_edit_assistant_panel.py` | 40% | 2,176 | 1,316 | 5 |
| 22 | `src/app/controllers/dock_layout_controller.py` | 21% | 1,616 | 1,282 | 4 |
| 23 | `src/app/controllers/deliver_controller.py` | 19% | 1,004 | 809 | 10 |
| 24 | `src/app/controllers/page_controller.py` | 43% | 1,928 | 1,103 | 5 |
| 25 | `src/shared/color/color_picker.py` | 46% | 1,714 | 922 | 7 |
| 26 | `src/features/edit/ui/program_viewer.py` | 41% | 1,684 | 989 | 5 |
| 27 | `src/app/main_window.py` | 19% | 1,159 | 942 | 5 |
| 28 | `src/app/controllers/color_controller.py` | 32% | 1,135 | 777 | 7 |
| 29 | `src/features/composite/ui/spline_view.py` | 17% | 1,044 | 865 | 5 |
| 30 | `src/features/composite/ui/node_editor_actions.py` | 14% | 645 | 552 | 11 |

候选总数（语句≥150 且 <60%）：259；上表为 Top 30（P2 首批目标）。

## 补测策略

1. 每批 = 1 个模块：读实现 + 既有测试惯例 → 设计用例（正常/边界/异常/取消，按模块性质选）→ 新增 `tests/<对应目录>/test_<模块>_coverage.py` → 实际跑到全绿。
2. 硬约束：不改 src、不改既有测试；发现疑似 bug 记入本目录 BUGS.md；mock 外部 IO/GPU；需要 GPU 的用例标 `@pytest.mark.gpu_renderer`。
3. 既有失败 31 个（0.14%）为环境性既有问题，与本计划无关。