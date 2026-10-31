# EFR-Small-S2 清理报告（10 个小模块行）

- 执行者：机械清理代理（E-rollup small-s2）
- 日期：2026-10-01
- 分支：dev（未做任何 git 写操作；工作树留改动）
- 范围红线遵守：未触碰 edit/core、shared/media、composite/ui、edit/ui、color/ui、teaching_animation/core|ai、ai_harness、color/core、composite/render3d、shared/video；
  未修改 REMEDIATION_PLAN.md；未删除任何 `from __future__ import annotations`（语义风险，明确排除）。

## 总览

| 分类 | 数量 | 说明 |
|------|------|------|
| 删除未用 import | 118 个名字（31 个文件） | AST 扫描 + 全仓 grep 核实（含 re-export / __all__ / 测试 patch 目标核查） |
| 删除死代码 | 2 处 | VideoQueueDelegate 类（~130 行，全仓零引用）、_apply_extrapolation（全仓零引用），均留证据 |
| 补类型注解 | 0 | 无"可证明"且必要的缺口，按"拿不准就不加"执行 |
| 私有命名收敛 | 0 | 未发现安全收敛点；公共/门面 API 一律不动 |
| 冗余中间变量 | 0 | 无低风险确证点，坚持最小 diff |
| 行为变化 | 0 | 全部为 import/死代码删除；GL 调用序列与线程契约零触碰 |

改动量：31 个文件，+46 / −227 行。

排除项（有意保留的"未用 import"）：
- `src/features/deliver/core/delivery_capability.py:91` `ExportPreflightResult`（自带 `# noqa: F401 — 类型可读性`，意图明确）
- `src/app/feature_flags.py:7` `FeatureFlags`（backward-compat shim 的再导出面）
- `src/app/main_window.py` `QMediaPlayer`（自带 `# noqa: F401`，QtMultimedia 可用性探测预导入）
- 各文件 `from __future__ import annotations`（移除属语义变化，超机械范围）

## 按模块行分节

### 1. src/shared/gpu/（含 metal/，25 条）
- 未用 import：2（gl_backend.py：`QOpenGLWidget`、`QThread`；全仓无 `from ...gl_backend import` 再导出使用）
- metal/ 19 个文件的命中均为 `__future__ import annotations`，全部保留
- 死代码：backend_selector/capabilities 等候选经核实均为同文件使用（误报），未删
- 文件：`src/shared/gpu/gl_backend.py`

### 2. src/features/ai_workflow/ui/（24 条）
- 未用 import：2（workflow_canvas_adapter.py：`DEFAULT_VISUAL_TOKENS`；workflow_canvas_view.py：`QGraphicsItem`）
- 文件：`workflow_canvas_adapter.py`、`workflow_canvas_view.py`

### 3. src/features/teaching_animation/（根文件，24 条）
- 未用 import：6（export_service.py：`shutil`；facade.py：`RenderedFrame`；host_adapters.py：`field`、`SceneAssetRef`、`SvgSceneRenderer`；scene_cache.py：`RenderProgress`）
- 死代码候选（facade 的 Host/Provider Protocol、create_default_scene、missing_module_fallback 等）全部为门面公共 API —— 保留
- 文件：`export_service.py`、`facade.py`、`host_adapters.py`、`scene_cache.py`

### 4. src/features/deliver/ui/（22 条）
- 未用 import：19（deliver_page.py：`Callable/Dict/List/Qt/QTimer/QColor/QFrame/QSizePolicy/DARK_THEME`；video_batch_export_panel.py：`QAbstractItemView/QComboBox/QHeaderView/QTreeView/ExportJob/JobStatus`；video_export_settings_panel.py：`SourceKind` + 死的函数内 `QColor` 局部导入）
- 死代码：`VideoQueueDelegate` 类（panel 内 ~130 行）全仓零引用（src+tests+plugins+scripts，含 patch 目标核查）→ 删除；连带清理其独占 import（QSize/QRect/QFont/QStyle/QStyleOptionViewItem/QStyledItemDelegate）
- 保留：`_QueueThumbnail`/`_RenderQueueCard`（在用）
- 文件：`deliver_page.py`、`video_batch_export_panel.py`、`video_export_settings_panel.py`

### 5. src/features/ai_workflow/adapters/（22 条）
- 未用 import：13（executor：`ComfyPreflightResult/ComfyConnectionKind`；export：`field/Mapping`；importer：`field/Mapping/ComfyNodeSummary`；node_specs：`Dict`；composite_adapter：`field/List`；generation_node_specs：`field/Mapping`）
- `_match_text_to_image` 经核实 in-file 使用（误报），未删
- 文件：`comfyui_executor.py`、`comfyui_export.py`、`comfyui_importer.py`、`comfyui_node_specs.py`、`composite_adapter.py`、`generation_node_specs.py`

### 6. src/app/（根文件，20 条）
- 未用 import：36（main_window.py：`json/subprocess/uuid/Dict/List/Union` + 15 个 QtWidgets + `QAction/QIcon/QKeySequence` + `QSize/QByteArray` + `cv2/np` + `QuickExportDialog/TAB_STYLE/apply_combo_dropdown_style/icon_reset/icon_settings`；app_context.py：`field`；feature_registry.py：`Any`）
- 核查：外部仅 `from src.app.main_window import MainWindow/PageContext*/PLAYER_AVAILABLE/_config_bool`；全仓无 `main_window.<名>` patch 目标；QDockWidget/QMenu 的第 2 次出现为注释；mixin 全局名解析在各自定义模块，不受影响
- 文件：`main_window.py`、`app_context.py`、`feature_registry.py`（`src/app/ui/*` 他机改动未触碰）

### 7. src/features/teaching_animation/motion/（19 条）
- 未用 import：4（beat.py：`field/Mapping`；motion_binding.py：`field`；motion_commands.py：`SceneCommand`）
- 死代码：`_apply_extrapolation`（interpolate.py，逻辑已内联进 `interpolate()`）全仓零引用 → 删除
- 误报排除：presets.py 的 `_fade_preset` 等工厂函数经 registry `build=` 引用；easing/expression/motion_commands 各 helper 均同文件在用
- 文件：`beat.py`、`motion_binding.py`、`motion_commands.py`、`interpolate.py`

### 8. src/features/composite/utils/（18 条）
- 未用 import：2（expression_parser.py：`Union`；preset_manager.py：`shutil`）
- 误报排除：`_linear/_ease_in/_ease_in_out`（SAFE_MATH/EASE 映射表在用）；pass_fusion `is_node_fusible` 等、serialization_formats `FormatVersion/parse_extension` 为公共 API → 保留
- 文件：`expression_parser.py`、`preset_manager.py`

### 9. src/features/deliver/core/（17 条）
- 未用 import：10（video_batch_export_controller.py：`Callable/VideoJobStatus/PreflightContext/run_video_export_preflight`；video_export_preflight.py：函数内 probe import 里 4 个未用名 `TimelineExportConfig/ExportSource/ExportRangeType/ExportCodec`（已核实 edit 侧均存在，try/except 分支取向不变）；video_export_presets.py：`Any/Dict/List`；video_job_runner.py：`VideoJobStatus`）
- 保留：delivery_capability.py 的 `ExportPreflightResult`（noqa 意图）
- 文件：`video_batch_export_controller.py`、`video_export_preflight.py`、`video_export_presets.py`、`video_job_runner.py`

### 10. src/shared/color/（17 条）
- 未用 import：14（color_picker.py：`QSpacerItem/QInputDialog/QCheckBox/QScreen/QPainterPath/QBrush/QBitmap/QIcon/QSize`；color_space_descriptor.py：`field`；color_transform_provider.py：2 处函数内未用 `LINEAR_SCENE` 局部导入）
- 公共 API 保留：`ensure_ocio_available`（公共名，零引用也不删）
- 文件：`color_picker.py`、`color_space_descriptor.py`、`color_transform_provider.py`

## 裸 except 普查（只记录，不修）

`grep -rn "except:"` 改动目录结果：
- `src/shared/gpu/gl_backend.py`：3 处（240/246/251，cleanup 释放失败的 logger.debug 短路）—— 既有，未触碰
- 其余 9 个模块行：0 处

## 验证输出

1. `py_compile`：31 个改动文件全部通过（ALL_COMPILE_OK，无输出即成功）
2. 定向测试（未跑 tests/edit）：
   - `tests/shared/gpu/test_backend_selector_config.py + test_backend_types.py + test_gl_resource_registry.py + test_graphics_backend_contract.py` → **52 passed**
   - `src/features/ai_workflow/tests/test_comfyui_executor.py + test_comfyui_export.py + test_comfyui_converter.py` → **60 passed**
   - `tests/deliver/test_export_queue.py + test_delivery_capability.py + test_custom_export_presets.py` → **36 passed**
   - `tests/shared/color/test_color_transform_provider.py + tests/color/test_color_transform_provider_parity.py + tests/shared/test_color_picker_coverage.py + tests/color/test_color_space_descriptor.py` → **173 passed, 3 skipped**
   - `tests/motion/test_interpolate.py + test_presets.py + test_easing.py + tests/composite/test_graph_expression.py + test_path_versioning.py` → **90 passed**
   - 附加：`tests/teaching_animation/ + tests/ai/test_teaching_animation_phase5.py` → **622 passed**；`tests/app/test_gap027_main_window.py` → **35 passed**
   - 无新增失败
3. 死代码证据（全仓 `grep -rn -w` src+tests+plugins+scripts，均为零引用）：
   - `VideoQueueDelegate`：唯一命中为定义行 `video_batch_export_panel.py:257`（删前）
   - `_apply_extrapolation`：唯一命中为定义行 `interpolate.py:44`（删前）

## 改动文件清单（31）

src/app/{app_context,feature_registry,main_window}.py
src/features/ai_workflow/adapters/{comfyui_executor,comfyui_export,comfyui_importer,comfyui_node_specs,composite_adapter,generation_node_specs}.py
src/features/ai_workflow/ui/{workflow_canvas_adapter,workflow_canvas_view}.py
src/features/composite/utils/{expression_parser,preset_manager}.py
src/features/deliver/core/{video_batch_export_controller,video_export_preflight,video_export_presets,video_job_runner}.py
src/features/deliver/ui/{deliver_page,video_batch_export_panel,video_export_settings_panel}.py
src/features/teaching_animation/{export_service,facade,host_adapters,scene_cache}.py
src/features/teaching_animation/motion/{beat,motion_binding,motion_commands,interpolate}.py
src/shared/color/{color_picker,color_space_descriptor,color_transform_provider}.py
src/shared/gpu/gl_backend.py
