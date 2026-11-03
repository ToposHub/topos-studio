# R00 基线留档（2026-11-03）

- `write_entry_audit.md`：写入口全量审计（旧兼容 A1–A9 / 正式 B1–B3 / 废弃 D1–D2，待审计数 0）。
- `media_preview_report.json`：验证脚本完整报告（status=passed，19.92s）。
  `baseline` 段记录 Compound 权威节点 id、两个独立色彩栈 stack_id、2 clip/2 Corrector；
  `baseline_samples` 段记录遗留插件（Fake_Infrared）实例顺序、未知参数
  `__unknown_future_param`、identity LUT 应用结果。
- `baseline_graph.json`：Compound 封装后的权威图 JSON（后续 R 轮迁移/保存对照基线）。
- `baseline_project.topos`：可重开基线工程（含图、遗留插件、未知参数、LUT 引用）。
- `regression_ui.log` / `regression_graph.log`：§7 两组回归（59 + 111 passed）。
- 截图为 `--resources` 阶段产物（面板/拖放/浮动证据），与 2026-11-03 目录同源。

复现命令：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests/ui/test_effects_browser_panel.py tests/ui/test_lut_browser_panel.py \
  tests/app/test_color_resource_preview.py tests/ui/test_color_resource_dock_layout.py \
  tests/ui/test_color_node_editor_view.py tests/ui/test_color_node_editor_panel.py \
  tests/ui/test_factory_default_layout.py -q

TOPOS_COLOR_GRAPH_RENDER=1 .venv/bin/python scripts/validate_color_node_media.py \
  tests/test_videos/测试素材01.mov --resources --workflow \
  --output docs/validation/color_resources/r00_baseline
```
