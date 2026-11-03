# R00 — 固定基线与写入口清单

更新：2026-11-03。本文件是 [资源后端实施计划](../../../color_resources_backend_implementation_plan.md) R00 的留档证据。

## 1. 基线环境

| 项 | 值 |
|---|---|
| git SHA | `350a037e50e3ca552308da96cb3061c095a98d83`（feat(color): redesign node resources and complete integration） |
| 工作区 | 干净（本轮 R00 改动前 `git status --short` 为空） |
| 平台 | macOS 15.3.1 (24D70)，Intel x86_64 |
| GPU | AMD Radeon RX 6800，16 GB，Metal 3 |
| 图执行 flag | `TOPOS_COLOR_GRAPH_RENDER` 默认关闭（`engine.color_graph_render_enabled=False`，见 media_preview_report.json） |
| 素材 | `tests/test_videos/测试素材01.mov`（12.4 MB） |

## 2. 基线回归（R00.4）

- UI 组（§7 第一组）：**59 passed**（effects/lut browser、resource preview、dock layout、node editor view/panel、factory layout）。
- 图组（§7 第二组）：**111 passed**（graph ui contract、commands、service、persistence、app host、parameter feedback）。
- 原始输出见 `regression_ui.log`、`regression_graph.log`。

## 3. 写入口审计（R00.2）

审计命令：

```bash
rg -n 'add_plugin|remove_plugin|plugins|apply_lut_from_path|active_lut_path|lut_intensity' \
  src/app src/features/color src/features/edit
```

命中逐项分类如下。**待审计数 = 0**。

### 3.1 旧兼容（图模式下仍可触发；R10 逐个迁移到正式入口或禁用）

| # | 入口 | 触发链 | 写目标 | 入历史 | 保存/导出 |
|---|---|---|---|---|---|
| A1 | 旧 Effects 面板添加 | `main_window.py:1375` `plugin_panel.add_effect_requested` → `color_controller.add_plugin_to_current_layer`（`color_controller.py:2616`）→ `layer.add_plugin` | legacy Layer.plugins | 否（无 command；仅 `_trigger_autosave`） | autosave/工程 IO 保存 plugins 字段 |
| A2 | 顶栏 Effects 菜单 | `menu_toolbar_controller.py:344,354` → 同 A1 | 同 A1 | 否 | 同 A1 |
| A3 | 旧 LUT 面板应用 | `main_window.py:1761` `lut_panel.lut_selected` → `color_controller.apply_lut_from_path`（`:1982`）→ `engine.apply_lut` | legacy Layer.active_lut_path | 否 | autosave |
| A4 | 旧 LUT 强度滑条 | `main_window.py:1763` → `color_controller.on_lut_intensity_changed`（`:1991`）→ `engine.set_lut_intensity` | legacy Layer.lut_intensity | 否（连 autosave 都没有） | 随层保存 |
| A5 | LUT 重置/移除 | `color_controller.on_lut_reset` / `on_lut_removed`（`:2004-2016`）→ `engine.reset_lut` | legacy Layer | 否 | autosave |
| A6 | LUT 反编译 | `color_controller.on_decompile_lut_requested`（`:2018`）→ `engine.decompile_lut`；成功后 `image_engine.py:1630` 清 `active_lut_path` | legacy Layer + engine | 否 | autosave |
| A7 | 旧 PluginPanel 删除实例 | `plugin_panel.py:675-698` `_delete_plugin` 直接 `engine.remove_plugin` / `layer.remove_plugin`（UI 控件直写 engine） | legacy Layer.plugins | interaction_started/ended 边界（与 grading/mixer 面板同一 `main_window.on_interaction_*` 快照机制，`main_window.py:1721-1739`） | autosave |
| A8 | 脚本 API | `src/features/scripting/api.py` `apply_lut` / `set_lut_intensity` / `remove_lut` 直写 engine | legacy Layer | 否 | 随层保存 |
| A9 | 素材派生净化 | `layer_controller.py:455,816` `layer.plugins = [dctl 过滤]`（图→派生图层流程内部） | legacy Layer.plugins | 随宿主命令 | 随层保存 |

A1–A8 的目标全部来自 `engine.active_layer_index` 猜测（无 typed target），是 R03 要替换的核心。

### 3.2 正式命令路径（保留）

| # | 入口 | 说明 |
|---|---|---|
| B1 | `ApplyColorGraphCommand`（`color_graph_controller.py` 提交，工程 command 栈） | 图模式权威命令：节点/边/grade 手势，undo/redo 完整。**资源 action 尚缺**（R03 新增，不复用 `set_grade` 任意字典） |
| B2 | `SetGradeCommand`（`src/features/edit/core/commands/color_commands.py`，含 lut_path/lut_intensity 字段） | 旧面板栈级参数命令，带 undo/redo 与序列化。属旧模式的正式命令；R03 后资源语义迁移到新命令，B2 保留给 legacy 参数域 |
| B3 | `color_graph_migration.py:294` | legacy stack → graph 一次性迁移（读 stack 写 graph），正式迁移路径 |

### 3.3 投影/读取路径（非用户写入口，仅登记）

- 权威重投影：`color_stack_hydrator.py:363-364,508-509,543-544`、`edit_controller.py:8717-8718`（graph/stack → engine Layer 水合，方向为权威→镜像）。
- 渲染读取：`composition_uniforms.py:423`、`shader_compiler.py:503`、`msl_composition.py:150`、`corrector_render_service.py:81`、`color_graph_cpu.py:520`、`image_engine.py:1663-1665,3851-3855`（LUT 纹理准备）。
- 导出/归档：`lut_exporter.py`、`timeline_export.py`、`delivery_matrix.py:404-405`、`project_archive.py:421,570-587`（重定位）、`project_dependencies.py:186`（依赖收集）。
- UI 只读：`node_tool_indicators.py:118`、`color_resource_preview.py:129-131`、`timeline_clip_strip.py:374`、`main_window.py:1653`（读 lut_intensity 字段）、`layer_controller.py:186-187`（读）。
- `image_engine.py:5402-5403`、`layer.py:573-574,661-672,776,827,838,951,1077,1093,1351,1369`：Layer 模型字段与序列化。

### 3.4 废弃待删（R10 处理）

| # | 项 | 说明 |
|---|---|---|
| D1 | `tools_panel.py:169,235-236` | 已注释的 lut_panel 接线残留 |
| D2 | 旧 `plugin_panel` / `lut_panel` 实例 | `main_window.py` 中已隐藏保留（供旧控制器读写兼容）；R10 迁移读路径后删除 |

### 3.5 分类结论

- **正式新命令**：资源类（effect add/remove/reorder/enable/param、LUT apply/strength/remove）当前 **0 个**，全部经旧兼容路径 → R03 建。
- **旧兼容**：A1–A9 → R10 全表路由。
- **废弃待删**：D1–D2 → R10 清零。
- 纯 UI 会话断言：验证脚本 `resources` 阶段内建 `assert graph unchanged` + `assert legacy plugins unchanged`，本轮重跑通过（见 `media_preview_report.json`）。

## 4. 基线工程（R00.3）

`scripts/validate_color_node_media.py --resources --workflow` 新增 `baseline` 阶段构建：

- 2 Corrector（serial 添加）+ 1 parallel 分支 + 1 Compound（`make_compound` 封装两个 Corrector）；
- 2 clip（同一素材先后两段），各自独立色彩栈（stack_id 不同，重绑回第一 clip 校验）；
- 留样：遗留插件实例（catalog 首个 effect）、未知参数 `__unknown_future_param`、identity LUT（`Baseline Identity.cube`）；
- 产物：`baseline_graph.json`（图 JSON）、`baseline_project.topos`（可重开工程）、`media_preview_report.json`（含实例顺序与 stack_ids）。

后续 R01+ 的迁移/保存测试以本工程为对照基线，不更换已有 golden。

## 5. 出口检查

- [x] 每个写点归为“正式新命令/旧兼容/废弃待删”，待审计数为 0
- [x] 纯 UI 会话不改项目/engine 的断言通过（resources 阶段）
- [x] 基线回归结果已保存，未覆盖前轮改动（R00 改动只新增 baseline 阶段与本文档）
- [x] 回退点：删除本文档与脚本 baseline 阶段即回到 `350a037` 状态
