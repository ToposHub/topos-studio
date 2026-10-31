# EFR 批修复报告 —— E-rollup 13 个小模块行机械清理（efr-small-s1）

日期：2026-10-02 ｜ 分支：dev（**未做任何 git add/commit，改动全部留在工作树**）｜ REMEDIATION_PLAN.md 未触碰
范围：仅下列 13 个模块行；其他代理负责的目录（composite/ui、composite/core、composite/nodes、composite/utils、composite/adapters、composite/ai、composite/render3d、edit/core 子目录、edit/ui、color/ui、color/core、teaching_animation/core|ai、ai_harness 非 tests、shared/media、shared/video）**零触碰**（工作树中这些目录的既有改动系其他代理所为）。

红线遵守：零行为变化（全部改动为 import 删除、`-> None` 注解、死代码删除三类，均经 AST + 全仓核实）；不修改任何 except 语义；无函数重排；最小 diff（净 -182 行，绝大多数为整行 import 删除）。

## 总分类统计

| 类别 | 数量 | 说明 |
|------|------|------|
| 1. 未使用 import 删除 | **293 个绑定 / 133 个文件** | AST 扫描（首轮 373 语句级候选，两轮迭代至不动点）→ 全仓核实 → 293 绑定实删；含 6 处整块 `if TYPE_CHECKING:`（moderngl 空引用块）删除及其级联 typing 导入收敛 |
| 2. 死代码删除 | **4 处 / 4 个文件** | 全仓（src/tests/scripts/tools/plugins/apps × py/md/tglsl/json）裸词零引用的模块级私有函数，逐处人工目检（证据见下） |
| 3. 类型注解补充 | **79 处 `-> None`（仅 `__init__`）** | 语言层面保证返回 None，无需新导入、注解期求值绝对安全；拿不准的其余签名一律未加 |
| 4. 命名收敛 | **0**（评估后不做） | 私有标识符重命名与"绝不改公共 API/最小 diff"红线冲突风险高（测试辅助函数可能被 monkeypatch 按名引用）；本轮放弃 |
| 5. 冗余中间变量 | **0**（评估后不做） | 收益低、目检成本高；为守住零行为变化红线本轮不做 |
| 合计改动 | **151 个文件，+196/−378 行** | 全部位于 13 个模块行内 |

## 有意保留（不算遗漏）

- **30 个 `__init__.py` 再导出绑定**（shared/ui、shared/ui/widgets、teaching_animation/render）：全仓两轮核实显示消费方直接从子模块导入、无人经包导入，但 `__init__` 再导出属包公共 API 面，按"绝不改公共 API"红线整体保留。
- **5 处 try 块内惰性导入**（`ai_compute_manager.py:201`、`video_export_worker.py:90`、`base_evaluator.py:1286` 等）：条件/降级导入语义，删除属行为变化。
- **4 处带 `# noqa: F401` 的显式再导出**（`test_comfyui_converter.py:118`、`test_comfyui_models.py:373` 的 `import inspect as _inspect` 预热 + `library_adapter.py` 全文件——该文件 docstring 自述为 backward-compatible re-export 门面）。
- **`render_scheduler.py:121` `_check_capability_consistency`**：同文件 142 行有注释掉的调用 `# _check_capability_consistency()`（CI 校验工具，刻意保留），不算死代码。

## 按模块行分节

| # | 模块行 | 改动文件 | 删 import 行 | `-> None` 注解 | +/- | 备注 |
|---|--------|---------|------------|----------------|-----|------|
| 1 | src/features/composite/render/ | 17 | 22 | 17 | +27/−49 | 含 composite_renderer 8 个未用 shader 常量整块删除 |
| 2 | src/shared/export/ | 13 | 16 | 6 | +19/−25 | |
| 3 | src/features/ai_harness/tests/ | 22 | 11 | 4 | +10/−50 | 含死代码 1（test_runtime `_wf_invocation`） |
| 4 | src/features/composite/evaluators/ | 14 | 29 | 4 | +14/−41 | 含 6 处 `if TYPE_CHECKING: import moderngl` 整块删除 |
| 5 | src/app/ui/ | 9 | 11 | 15 | +33/−37 | library_adapter 门面整体保留 |
| 6 | src/shared/ui/widgets/ | 16 | 18 | 21 | +39/−43 | |
| 7 | src/features/teaching_animation/render/ | 15 | 18 | 0 | +10/−26 | 含死代码 1（html_validation `_page_probe_frames`） |
| 8 | src/features/teaching_animation/ui/ | 2 | 2 | 0 | +1/−2 | |
| 9 | src/shared/project/ | 6 | 11 | 1 | +10/−17 | |
| 10 | src/shared/ui/audio_mixer/ | 11 | 7 | 5 | +17/−27 | 含死代码 1（dynamics_dialog `_knob_value`） |
| 11 | src/features/ai_workflow/tests/ | 16 | 16 | 1 | +3/−40 | 2 处 noqa 预热导入保留 |
| 12 | src/features/composite/ 根文件 | 4 | 3 | 4 | +6/−7 | edit_service / module / preferences / __init__ 未动再导出 |
| 13 | src/shared/ui/ 根文件 | 6 | 9 | 1 | +7/−14 | __init__ 再导出整体保留 |
| 合计 | | **151** | **173 行** | **79** | **+196/−378** | 293 绑定（含多名字行内删除） |

> 注：绑定数（293）> 删行数（173），因多名字 import 行采用行内名字删除（保留行上仍在使用的名字），另含 6 处级联 `TYPE_CHECKING` 导入收敛。

## 抽样证据

### 未使用 import（AST + 全仓三关核实）

```diff
# composite_renderer.py —— 8 个 shader 常量块：全仓无人从 composite_renderer 反向导入
# （frame_interpolate_node / deflicker_node 均直接 from ..core.shaders import），
# core.shaders 仍经 image_loader/eval_context 等同文件其他导入链加载，无加载时序变化
-from src.features.composite.core.shaders import (
-    _PASSTHROUGH_VERTEX, _PASSTHROUGH_FRAGMENT,
-    _BLEND_FRAGMENT, _BLUR_H_FRAGMENT, _BLUR_V_FRAGMENT,
-    _GRADE_FRAGMENT, _TRANSFORM_FRAGMENT, _INVERT_FRAGMENT,
-)

# evaluators/blur_evaluator.py —— moderngl 全文仅出现在该 TYPE_CHECKING 导入
-if TYPE_CHECKING:
-    import moderngl
-from typing import Dict, TYPE_CHECKING      # → 级联收敛为 from typing import TYPE_CHECKING 后整行删除

# export_panel.py —— 行内名字删除，保留同行在用名字
-from PyQt6.QtCore import Qt, pyqtSignal, QSize, QMimeData, QUrl, QObject, QEvent
+from PyQt6.QtCore import Qt, pyqtSignal, QSize, QObject, QEvent
```

**核实方法**（与 efr-composite-nodes 同款三关）：① AST Name + 字符串常量收集（防 getattr/字符串注解漏判）文件内零使用；② 多名字行做别名级行定位、行内精确删名；③ 每文件写盘前/后 `ast.parse` 自校验，任何解析失败即放弃该文件（首轮注解器曾因此拦截 85 个文件，全部未写盘；真删集另用"备份 vs 当前 AST 导入差集"复核得 293 绑定/133 文件）。扫描迭代两轮至不动点：最终残留 39 项全部落在上述四类有意保留清单。

### 死代码（4 处，全仓裸词零引用 + 人工目检）

- `topos_cache_codec.py:302` `_sizeof`（3 行 helper；删除后 `Any`/`ctypes` 无孤儿——`Any` 仍被 `_planes_to_rgba` 使用，ctypes 为函数内导入随删）
- `test_runtime.py:55` `_wf_invocation`（连同其专属注释行；`Invocation` 仍被 `_make_invocation` 使用）
- `html_validation.py:95` `_page_probe_frames`（`__all__ = ["validate_html_scene"]` 未含它；`_page_probe_candidates` 仍被其他在用函数引用）
- `dynamics_dialog.py:137` `_knob_value`（`QWidget` 仍被 `_DynamicsTab(QWidget)` 使用）

对照组：`render_scheduler._check_capability_consistency` 因同文件注释调用而**保留**（见上）。

### 类型注解（79 处，仅 `__init__` 补 `-> None`）

```diff
 # preferences.py / edit_service.py / pref_drag_value.py 等 53 个文件同款
-    def __init__(self, graph: Any):
+    def __init__(self, graph: Any) -> None:
```
仅当 AST 确认 `returns is None` 才加；多行签名用括号深度扫描（处理字符串/注释）定位 `):`；拿不准的一律不加。首轮脚本曾产生 7 处 def 行与函数体合并的格式损伤，已逐处还原并修复脚本后重跑（最终 diff 无合并行，`git diff | grep "None:\s\S"` 为 0）；首轮误伤的 1 处既有 `# type: ignore[override]` 单行写法亦已原样还原。

## 验证输出（全过）

1. **py_compile**：151 个改动文件 `.venv/bin/python -m py_compile` 全部通过，0 失败。
   （既有 `viewport_toolbar_base.py:197` SyntaxWarning 为改动前即存在，与本次无关。）
2. **定向测试**（未触碰 tests/edit；QT_QPA_PLATFORM=offscreen）：
   - composite/render：`tests/composite/test_composite_cache_key.py` + `test_cache_strategy.py` → **96 passed**
   - shared/export：`tests/export/test_export_stage_telemetry.py` + `test_gpu_trail.py` → 并入上组（同批 96 内）
   - ai_harness：`src/features/ai_harness/tests/` 的 test_runtime / test_skill_dry_run / test_legacy_adapters / test_skill_compiler → **81 passed**
   - app/ui + shared/ui：`tests/app/test_script_panel_project_root_aud40014.py` + `tests/shared/test_audio_mixer_units.py` + `test_fix_audio_mixer_aud4.py` + `tests/teaching_animation/test_fix_wd16g77_aud.py`（html_validation 死代码删除覆盖）→ **122 passed**
   - ai_workflow + project：`test_p5_acceptance.py` + `test_comfyui_converter.py` + `tests/project/test_topos_project_controller.py` → **73 passed**
   - 补充：`tests/composite/test_pass_fusion_execution.py`（evaluators/base_evaluator）+ `tests/shared/test_fix_ui_utils_aud4.py` → **180 passed**
   - 合计 **552 passed / 0 failed**，无新增失败。
3. **裸 `except:` 统计**：13 个模块行内 `grep -rn "except:"` = **0 处**（无可记录项）。
4. **不动点复核**：清理后重扫，13 个模块行内"非 init / 非 try / 非 noqa"未使用 import = **0**。

## 改动文件清单

151 个文件的完整清单见工作树 `git status`（下述路径前缀内全部 Modified 文件即本报告范围；render3d/utils 等 report 区间的既有改动属其他代理）：
`src/features/composite/render`(17) · `src/shared/export`(13) · `src/features/ai_harness/tests`(22) · `src/features/composite/evaluators`(14) · `src/app/ui`(9) · `src/shared/ui/widgets`(16) · `src/features/teaching_animation/render`(15) · `src/features/teaching_animation/ui`(2) · `src/shared/project`(6) · `src/shared/ui/audio_mixer`(11) · `src/features/ai_workflow/tests`(16) · `src/features/composite/`根(4) · `src/shared/ui/`根(6)。
