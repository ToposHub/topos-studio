# EFR 批修复报告 —— E-rollup `src/features/composite/core/` 机械清理（67 条 process 级改进落线）

日期：2026-10-01 ｜ 分支：dev（**未做任何 git add/commit，改动全部留在工作树**）｜ 白名单：仅 `src/features/composite/core/`（30 个 py / 25471 行）
红线遵守：零行为变化；锁与线程契约相关文件（image_loader / graph_snapshot / node_engine / layer_stack_controller 等）仅做声明级改动（`-> None` 注解）；无裸 `except:`（既有即为 0）；无函数重排；REMEDIATION_PLAN.md 未触碰；未运行 tests/edit。

## 分类统计

| 类别 | 数量 | 说明 |
|------|------|------|
| 1. 未使用 import 删除 | **17 个绑定 / 11 个文件** | AST 扫描（34 候选绑定）→ 全仓 grep 反向导入 + 属性访问逐一核实 → 17 处实删；16 个 `from __future__ import annotations` 与 1 个向后兼容 re-export **有意保留**（见下） |
| 2. 死代码删除 | **2 个函数 / 1 个文件** | `node_engine.get_all_node_types` / `get_all_categories`（全仓零引用，证据见下）；`PortRevision` 评估后**有意保留** |
| 3. 类型注解补充 | **41 个签名追加 `-> None`**（10 个文件） | 全部经 AST 验证"函数体无带值 return、无 yield"；7 个有返回值但类型非显然的签名**未加**（拿不准就不加） |
| 4. 命名收敛 | **0**（评估后不做） | 与 efr-composite-nodes 同判：私有标识符重命名需逐个全仓核实、收益低、与"最小 diff/绝不改公共 API"红线冲突风险高 |
| 5. 小清理 | **0 处** | "单一赋值后立即 return"模式全模块 AST 扫描 **0 命中**；重复字面量提取评估后放弃（见下） |
| 合计改动 | **16 个文件，+53/−75 行** | 全部位于 `src/features/composite/core/` |

## 未使用 import 删除明细（17 绑定）

- 整行删除：`image_loader.py` `import traceback`、`node_plugin.py` `import os`
- typing 收敛：`node_engine`（`TYPE_CHECKING`）、`media_drop_payload`（`Tuple`）、`node_capabilities`（`FrozenSet`）、`time_remap`（`List`）、`tile_scheduler`（`Dict`、`Optional`）
- dataclasses 收敛：`field` ×6（asset_dependency / layer_provenance / node_capabilities / node_plugin / tile_scheduler / video_context）
- 内部符号：`tile_padding_computer` 的 `is_effect_node_type`（from node_capabilities，本文件零使用）、`tile_scheduler` 顶部 `from .graph_snapshot import GraphSnapshot`（旧实现的残留，见下）
- 守卫内删除：`video_context.py` `if TYPE_CHECKING:` 块内 `import numpy as np`（全文件无任何 `np` 注解/使用引用；块内其余导入均使用，保留）

**核实方法**（每条过三关）：
1. AST Name 收集 + 全文件原始文本 `\b<name>\b` 扫描（排除 import 行自身，防 getattr / 字符串注解漏判）→ 文件内零使用；
2. 全仓 `grep -rEn "from ...<mod> import ... <name>"` + `<mod>.<name>` 属性访问双模式（src/ tests/ plugins/ scripts/ docs/）→ 零反向引用；
3. **模块导入冒烟**：29 个模块逐一 `importlib.import_module` → NameError 级错误零容忍（见"过程记录"）。

**有意保留**：
- 16 个文件的 `from __future__ import annotations` —— 语义性导入（注解惰性求值），删除有前向引用求值风险；
- `tile_scheduler.py` 底部 `TileSizeRecommendation` re-export —— 所在代码块有显式注释 `# Backward Compatibility / Alias for backward compatibility`，是有意的兼容 shim，删除属公共面变化（尽管全仓当前零引用，仍保留）。

## 死代码证据（2 条，均已删）

`src/features/composite/core/node_engine.py` 原 :2889 `get_all_node_types()`、原 :2898 `get_all_categories()`：
- 全仓 grep（src/ tests/ scripts/ plugins/ docs/，*.py/*.md/*.json/*.tglsl）**各仅 1 处命中 = 定义行本身**，零调用、零导入；
- `core/__init__.py` 仅一行注释（无 re-export），全仓无 `from ...node_engine import *`；
- 二者为 NODE_REGISTRY / NODE_CATEGORIES 的薄包装 getter，注册表本体仍被大量使用（未动）。

**评估后保留**：`graph_snapshot.py:18` `PortRevision` dataclass（代码零引用，但 `docs/composite_optimization_plan_2026-09-07.md:67` 将其作为缓存正确性关键概念点名，且 `tests/composite/test_fix_wd5g33_aud.py::TestPortRevisionRegistration` 以其命名测试端口 revision 机制）——删除信号冲突，按"拿不准就不删"保留。

## 类型注解明细（41 处 `-> None`）

undo_stack ×17、image_loader ×12、node_engine ×3、asset_dependency ×2、layer_stack_controller ×2、layer_model / layer_provenance / layer_stack_compiler / layer_stack_validator / node_plugin 各 ×1。
- 覆盖：`__init__` ×14、undo_stack 的 undo/redo/push/clear/compress/group 系方法、image_loader 的 QThread worker 生命周期方法（`run`/`cancel`/`cancel_all`/`request_decode`/`_cancel_worker_unlocked`/`_on_decode_completed` 等）、`NodeGraph.disconnect_by_dst`/`_mark_dirty_downstream` 等；
- 编辑方式：tokenize 定位签名括号深度 0 的终止冒号后插入 ` -> None`，每个文件写盘前重新 `ast.parse` 验证；生成器（yield）与带值 return 的函数被排除；
- **未加**的 7 个有返回值签名（`spawn_3d_scene_children`、`_create_node`、`world_transform`、`request_gpu_frame_for_node`、`__iter__`、`_val`、controller 的 pyqtSignal 工厂）：返回类型非显然或需新增 typing 导入，按"拿不准就不加"原则不动；
- 锁/线程契约文件（image_loader、undo_stack、node_engine、layer_stack_controller）**只动了签名行**，方法体零改动。

## 小清理（0 处的理由）

- 冗余中间变量：全模块 AST 扫描"`x = expr` 下一语句立即 `return x`"模式，**0 命中**（对比：nodes 模块同模式有 8 处）；
- 重复字面量提取：高频字面量均为序列化 dict key / 状态字符串（如 node_capabilities 的 `'effect'` ×69、`'identity'` ×35），批量提取为常量属结构性大 diff，违反"最小 diff/不重排"红线，整类放弃。

## 过程记录（自纠错，供审计）

初轮编辑曾依据扫描表误删 `media_drop_payload.py` / `tile_padding_computer.py` 的 `field`（两文件的 `field` 实为 dataclass default_factory 使用，本就不在未使用清单——属编辑环节看错行）。**模块导入冒烟（py_compile 无法捕获的 NameError）当场拦截**，已立即原样恢复，净 diff 中这两个文件的 dataclasses 导入行零变化。此教训已固化为验证步骤 2。

## 验证输出（全过）

1. **py_compile**：`git diff --name-only -- src/features/composite/core/` 的 16 个文件逐一 `python -m py_compile` → `PY_COMPILE_ALL_OK`
2. **模块导入冒烟**：`importlib` 逐个导入 core 包全部 29 个模块 → `failures: NONE`（曾据此拦截 2 处误删）
3. **定向既有测试**（未跑大目录、未跑 tests/edit）：
   - `pytest tests/composite/test_layer_stack_validator.py tests/composite/test_runtime_diagnostics.py tests/composite/test_time_remap_and_layer_gates.py -q` → **82 passed**
   - `pytest tests/composite/test_layer_stack_controller.py tests/composite/test_composite_undo.py tests/composite/test_image_loader_callbacks.py tests/composite/test_composite_tile.py -q` → **165 passed**
   - `pytest tests/composite/test_asset_dependency.py tests/composite/test_graph_preflight.py tests/composite/test_layer_stack_compiler.py -q` → **74 passed**
   - `pytest tests/composite/test_fix_wd5g33_aud.py -q`（覆盖 node_engine 死代码所在模块与端口 revision 机制）→ **45 passed**
   - `pytest tests/composite/test_node_capabilities.py -q` → **51 passed**
   - 合计 **417 passed / 0 failed**（既有 1 warning，与本批无关）
4. **裸 except 检查**：`grep -rn "except:" src/features/composite/core/ --include="*.py"` → **0 命中**（既有即为 0，无记录项）

## 改动文件清单（16 个，全部 `src/features/composite/core/`，+53/−75）

| 文件 | 改动 |
|------|------|
| asset_dependency.py | −field import；2×`-> None` |
| image_loader.py | −`import traceback`；12×`-> None` |
| layer_model.py | 1×`-> None` |
| layer_provenance.py | −field；1×`-> None` |
| layer_stack_compiler.py | 1×`-> None` |
| layer_stack_controller.py | 2×`-> None`（含 Qt 降级桩 `__init__`） |
| layer_stack_validator.py | 1×`-> None` |
| media_drop_payload.py | −Tuple import（field 误删已复原） |
| node_capabilities.py | −field、−FrozenSet |
| node_engine.py | −TYPE_CHECKING；3×`-> None`；死代码 −22 行 |
| node_plugin.py | −`import os`、−field；1×`-> None` |
| tile_padding_computer.py | −is_effect_node_type（field 误删已复原） |
| tile_scheduler.py | −field、−Dict/Optional、−顶部 GraphSnapshot 残留（底部兼容 shim 保留） |
| time_remap.py | −List |
| undo_stack.py | 17×`-> None` |
| video_context.py | −field、−TYPE_CHECKING 块内 `import numpy as np` |
