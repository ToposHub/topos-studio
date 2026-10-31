# EFR-Small-S5 清理报告（21 个小模块行 E-rollup）

- 执行者：机械清理代理（E-rollup small-s5）
- 日期：2026-10-01
- 分支：dev（未做任何 git 写操作；工作树留改动，未 add/commit）
- 范围红线遵守：未触碰 edit/core、edit/ui、teaching_animation/core|ai|render、ai_harness、
  composite/render、composite/render3d（deformers 除外）、composite/ui、shared/video；
  未修改 REMEDIATION_PLAN.md；presets/library 下仅动 Python（build.py），预设数据零触碰；
  未删除任何 `from __future__ import annotations`；未改任何 except 语义。

## 总览

| 分类 | 数量 | 说明 |
|------|------|------|
| 删除未用 import | 13 个名字（9 个文件） | AST 扫描（50 个范围内文件）+ 全仓 grep 核实（含 re-export / `__all__` / 外部 `from <模块> import` 逐一面核查） |
| 删除死代码 | 3 处（1 处因跨机冲突撤回） | 全仓零引用 + 审计条目佐证（AUD-1107 / AUD-24704 / AUD-37200），均留证据 |
| 补类型注解 | 4 处 | 4 个 preset `build() -> dict`（`.to_dict()` 返回 `dict[str, Any]`，可证明；不引入新 import） |
| 私有命名收敛 | 0 | 未发现安全收敛点；公共/门面 API 与信号名一律不动 |
| 冗余中间变量 | 0 | 无低风险确证点（如 workflow_context.py 的 `else "" or ""` 冗余，属表达式语义触碰，按最小 diff 保留） |
| 行为变化 | 0 | 全部为 import/死代码删除与注解补充 |

改动量：13 个文件，+13 / −63 行（另 1 个文件 render_diagnostics.py 因跨机并发冲突撤回，见下）。

有意保留的"零引用"项（非遗漏）：
- `src/features/ai_workflow/module.py:41-42` `WorkflowPanel` / `WorkflowCanvasAdapter`：
  可用性探测 import，自带 `# noqa: F401`（PyQt6/核心 UI 可导入性检查），意图明确。
- `src/shared/i18n/i18n.py` `I18N.get_locales_dir` / `I18N.is_locale_supported`：
  全仓零引用，但属核心 i18n 门面类的公共访问器，且 338 批次审计对该模块复核时未标记为死代码——
  按"公共 API 不动"原则保留，仅记录。

## 按模块行分节

### 1. src/features/composite/ai/（5 条）
- 0 改动。composite_plan.py 为契约模块：全部常量在 `SUPPORTED_*`/`_TARGET_NODE_OPS` 集合或
  `__all__` 中消费；`_freeze_mapping`/`validate_composite_plan` 均在用。无未用 import。
- 文件：无

### 2. src/shared/effects/（4 条）
- 未用 import：1（preview_quality.py `Set`；外部 8 处 `from ...preview_quality import` 均未导入 `Set`）
- 死代码：`set_custom_threshold` + `check_threshold_degradation` 两方法 + 其独占状态
  `self._custom_thresholds`（AUD-1107：阈值 API 与降级决策完全断链、全库无调用，
  审计明示"或删除该 API 避免误导"；+1/−37）
- 文件：`src/shared/effects/preview_quality.py`

### 3. src/products/（4 条）
- 未用 import：1（`__init__.py` 顶层 `FrozenSet`——模块实际用内建 `frozenset()`；
  `from src.products import` 全部消费方仅取 ProductPreset/PRODUCT_PRESETS/函数）
- 文件：`src/products/__init__.py`

### 4. src/features/color/shaders/（4 条）
- 死代码：`LUT3D_VERTEX`（12 行 GLSL 字符串，全仓零引用；AUD-24704 已判定死代码、
  与 composite 侧活副本双份维护。同批的 `LUT3D_FRAGMENT` 因被
  tests/color/test_fix_wd3g21_aud.py 断言引用而保留）
- 文件：`src/features/color/shaders/shaders.py`（0/−12；.glsl 数据文件零触碰）

### 5. src/shared/config/（4 条）
- 0 改动。config_manager.py 无未用 import、无零引用顶层定义（DEFAULT_CONFIG 键由
  Preferences/各 feature 按字符串键消费，不属可删对象）。
- 文件：无

### 6. src/shared/i18n/（4 条）
- 0 改动。i18n.py 全部方法在用；两个零引用公共访问器按公共 API 保留（见总览）。
- 文件：无

### 7. src/features/composite/render3d/deformers/（4 条）
- 0 改动。base.py/cpu.py 全部私有 helper 经 `_DEFORMERS` 分发表/`evaluate_deformer` 消费；
  `DEFORMER_SHEAR`/`DEFORMER_SPHERIFY` 占位常量按 AUD-8703 决策保留在 base.py（不导出）。
- 文件：无

### 8. src/shared/render/（3 条）
- 0 改动。source.py 为纯 Protocol 定义（RenderSource，AUD-1702 契约）。
- 文件：无

### 9. src/features/ai_workflow/runtime/（3 条）
- 未用 import：1（fake_workflow_service.py `field`——仅 `dataclass` 在用）
- workflow_context/workflow_action_service/workflow_run_bridge 逐文件核查无未用 import、无死代码
- 文件：`src/features/ai_workflow/runtime/fake_workflow_service.py`

### 10. .../presets/library/charts/line_curve_chart/（3 条）
- 未用 import：1（build.py `linear_gradient`）
- 注解：1（`def build() -> dict`）
- 文件：`build.py`

### 11. src/shared/（根文件，3 条）
- 未用 import：1（keyframe.py `Union`）
- 冲突记录：`src/shared/render_diagnostics.py` 的 `_VALID_SOURCES`（AUD-37200 死常量）我侧按
  审计"删除"选项执行；另一清理机同窗口以"告警级校验"方案重写了该文件并恢复 `_VALID_SOURCES`
  供 `record()` 使用。**我方撤回对该文件的全部主张**，不再触碰（其当前合并态含我的一条注释行，
  编译与相关测试已按合并态复验通过）。该文件不计入本报告改动清单。
- 文件：`src/shared/keyframe.py`

### 12. src/shared/logging/（3 条）
- 0 改动。log_manager.py（AUD-34203 修复后）全部成员在用。
- 文件：无

### 13. src/shared/node_canvas/（3 条）
- 未用 import：2（graph_ui_protocol.py `Dict`、`List`；`Any/Optional/Protocol/Tuple/
  runtime_checkable/field` 均在用）；undo_stack.py 干净
- 文件：`src/shared/node_canvas/graph_ui_protocol.py`

### 14. src/shared/node_help/（3 条）
- 0 改动。`__init__.py`（AUD-36401/36403 修复后）与 dialog.py 全部在用。
- 文件：无

### 15. src/shared/panels/（3 条）
- 未用 import：2（registry.py `field`、`FrozenSet`；`Dict/List/Optional/Set` 在用）
- 文件：`src/shared/panels/registry.py`

### 16. src/features/ai_workflow/（根文件，2 条）
- 0 改动。module.py 的 2 个命中为 `# noqa: F401` 可用性探测 import（有意保留，见总览）。
- 文件：无

### 17. src/features/color/integrations/（2 条）
- 0 改动。tangent_bridge.py（1092 行）AST 扫描零命中。
- 文件：无

### 18. .../backgrounds/blueprint_grid_bg/（2 条）
- 注解：1（`def build() -> dict`）；import 全部在用（linear_gradient 使用中）
- 文件：`build.py`

### 19. .../particles/confetti_burst/（2 条）
- 注解：1（`def build() -> dict`）
- 文件：`build.py`

### 20. .../shapes3d/isometric_cube/（2 条）
- 注解：1（`def build() -> dict`）
- 文件：`build.py`

### 21. src/shared/actions/ + src/shared/paths/ + src/shared/preferences/（各 2 条）
- 未用 import：4 + 0 + 1（actions `__init__.py` `field/Any/Callable/List`——类字段全用
  str/Optional，泛型用内建 `dict[]/list[]/set[]`；preferences `__init__.py` `field`，
  该模块有显式 `__all__` 且不含 `field`）
- paths/paths.py 干净（AUD-36601/36602 修复后）
- 文件：`src/shared/actions/__init__.py`、`src/shared/preferences/__init__.py`

## 死代码证据（删前全仓 `grep -rn -w` src+tests+scripts+plugins+docs，零引用）

- `set_custom_threshold` / `check_threshold_degradation` / `_custom_thresholds`：
  唯一命中为定义/赋值行（preview_quality.py:350/560/573）；AUD-1107 审计明示断链并给出删除选项
- `LUT3D_VERTEX`：唯一命中为定义行（shaders.py:115）+ 审计文档；AUD-24704 判定死代码
- `_VALID_SOURCES`：删前零引用（AUD-37200）；因并发冲突撤回，现由他机方案在 `record()` 中使用

## 裸 except 普查（只记录，不修）

`grep -rn "except:"` 全部 21 个模块行目录 + 6 个根文件（最终态复跑）：
- **0 处**。无既有裸 except 需要记录。

## 验证输出

1. `py_compile`：14 个触碰文件全部通过（ALL_COMPILE_OK，含 render_diagnostics 合并态）；
   另做运行时 `importlib` 导入验证 15 个模块 + 4 个 preset `build()` 实跑（均返回 dict）——ALL_IMPORT_OK
2. 定向测试（未跑 tests/edit）：
   - i18n：`tests/architecture/test_i18n_lifecycle.py + tests/shared/test_i18n_local_import_guard.py` → 通过
   - node_help：`tests/composite/test_node_help.py` → 通过
   - config/logging：`tests/architecture/test_config_manager.py + tests/shared/test_fix_wd16g88_aud.py` → 通过
   - preferences：`tests/architecture/test_module_preferences.py + tests/shared/test_fix_wd17g94_aud.py` → 该文件 145 passed 中 2 failed（见下）
   - composite/ai + shaders + effects + products：`tests/ai/test_cg_cp_ix.py + tests/color/test_fix_wd3g21_aud.py + tests/color/test_fix_shaders_tracking_aud.py + tests/color/test_effect_preview_quality.py + tests/shared/test_fix_wd17g99_aud.py + tests/architecture/test_product_presets.py + tests/shared/test_ef21_aud.py` → **237 passed**
   - keyframe/actions/panels/deformers/runtime/node_canvas/诊断：`tests/composite/test_typed_keyframe_values.py + tests/architecture/test_menu_contributions.py + tests/composite/test_3d_deformers.py + src/features/ai_workflow/tests/test_fake_workflow_service.py + tests/ui/test_workflow_run_controller.py + src/features/ai_workflow/tests/test_node_canvas_protocol.py + tests/composite/test_fix_wd6g32_aud.py + tests/video/test_dynamic_preview_quality.py` → **187 passed**
   - render_diagnostics 合并态复验：`tests/composite/test_fix_wd6g32_aud.py + tests/color/test_builtin_plugin_compile.py` → **111 passed**
   - 汇总：**680 passed，2 failed（非本次范围、非我方文件）**
   - 2 个失败均在 `tests/shared/test_fix_wd17g94_aud.py::TestAud36201`（媒体容器测试）：
     报错 `'WindowsHwPlanarSource' object has no attribute '_gop_index_thread'`，属性定义于
     `src/shared/media/ffmpeg_source.py:532` —— 属 shared/media（他机正在清理的目录，工作树有
     大量未提交改动）的在途状态；该测试类与本次 13 个改动文件零调用路径交集。
3. AST 复扫：范围内仅剩 module.py 两个 `noqa` 探测 import（有意保留）

## 改动文件清单（13，+13/−63）

src/features/ai_workflow/runtime/fake_workflow_service.py
src/features/color/shaders/shaders.py
src/features/teaching_animation/presets/library/{backgrounds/blueprint_grid_bg,charts/line_curve_chart,particles/confetti_burst,shapes3d/isometric_cube}/build.py
src/products/__init__.py
src/shared/actions/__init__.py
src/shared/effects/preview_quality.py
src/shared/keyframe.py
src/shared/node_canvas/graph_ui_protocol.py
src/shared/panels/registry.py
src/shared/preferences/__init__.py

（撤回：src/shared/render_diagnostics.py —— 跨机并发冲突，归还在途清理机）
