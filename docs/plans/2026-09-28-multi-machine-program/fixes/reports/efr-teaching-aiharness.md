# EFR 报告：teaching_animation core / ai / ai_harness 三模块行机械清理

- 日期：2026-10-01
- 分支：dev（仅工作树改动，无任何 git 写操作）
- 范围：
  - `src/features/teaching_animation/core/`（36 个 .py）
  - `src/features/teaching_animation/ai/`（42 个 .py）
  - `src/features/ai_harness/`（不含 `tests/`，40 个 .py）
- 原则：纯机械、零行为变化；每项删除均经「AST 全文件核实 + 全仓 grep/导入解析核实」双重把关；拿不准一律不动。

## 一、分类统计

| 类别 | 处理数 | 说明 |
|------|--------|------|
| 1. 未使用 import 删除 | **74 处（44 个文件）** | AST 扫描 186 候选 → 112 个为 `__future__ import annotations`（语义需要，保留）→ 剩余 74 处经全仓导入解析核实后全部删除 |
| 2. 死代码删除 | **0 处** | 17 个候选全部被动态分发表否定（见第二节），证据在案，一律保留 |
| 3. 类型注解补齐 | **2 处** | 全范围仅 3 处未注解函数，2 处补齐，1 处按原样保留（有作者 type: ignore 声明） |
| 4. 命名收敛 | **0 处** | 主动放弃，理由见第四节 |
| 5. 小清理 | **1 处**（另有 1 处因红线跳过） | 相邻 `x = …; return x` 内联 |

删除 import 分布（处数 / 文件数）：ai 行 33 / 18，ai_harness 行 29 / 16，core 行 12 / 10。
全部为运行时 import（无 TYPE_CHECKING 块内删除）；`__init__.py` 一律不动（包门面/再导出）；函数体内惰性导入一律不动并保留原状。

### 安全护栏（每个删除项逐条执行）

1. **AST 全文件使用核实**：Name/Attribute 节点 + 字符串字面量 token（`getattr(x, "Name")` 类字符串用法视为已用，受保护）。
2. **全仓导入解析核实**：解析全仓 .py 的 `from <mod> import <name>` / `import <mod>` / 相对导入 / `from pkg import <submodule>` / `import *`；任何其他文件从本模块导入该名字，或对目标模块存在 star-import → 保留。结果：74 处候选无一被跨模块再导出。
3. **唯一导入方副作用护栏**：若本文件是目标模块全仓唯一导入方，删除该 import 会移除目标模块的 import 时执行（潜在副作用）→ 保留。74 处全部通过（目标模块均有其他导入方，或为无副作用的标准库/typing 名字）。
4. **注释保护**：import 行含 `#`（noqa 等）→ 不动。

### 抽样证据（删除项）

- `src/features/teaching_animation/ai/proposal_card.py`：`import json` 全文件零引用；`from .fallback import …` 中 `FALLBACK_INVALID_SCENE_PLAN / FALLBACK_NONE / FALLBACK_OPTIONAL_TEMPLATE / FALLBACK_PROVIDER_UNAVAILABLE` 四名本文件未使用且全仓无人从 proposal_card 再导入（`ACTION_*` 与 `MotionGraphicFallbackDecision` 保留，导入语句收敛）。
- `src/features/teaching_animation/core/model.py:17`：`from src.shared.keyframe import Keyframe, KeyframeTrack` → `Keyframe` 本文件零使用，收敛为仅 `KeyframeTrack`（名字定义方 `src/shared/keyframe.py` 不受影响）。
- `src/features/teaching_animation/ai/design_validator.py:39`：`SUPPORTED_ELEMENT_TYPES` 从 `..core.model` 导入但本文件未使用（定义方 `core/model.py:23`，使用方 `core/validator.py:18` 均直连导入，不受影响）；同文件 `TeachingSceneDocument` 仅出现在 import 行与 docstring（`:class:` 文档引用不需要运行时导入）。
- `src/features/ai_harness/skill_dry_run.py`：`import uuid`、typing `Iterable/Sequence`、`ModelRequirement` 本文件零使用；`skill_studio_drafts` 多行导入收敛为 `ExecutorKind, SkillDraft` 两名。
- `src/features/teaching_animation/ai/command_catalog.py`：顶层 `from dataclasses import dataclass` 删除；该文件 699 行存在函数内惰性 `from dataclasses import dataclass as _dataclass`（惰性导入防护，按规则未动），顶层名被遮蔽且零使用。
- `src/features/ai_harness/skill_release.py` / `skill_studio_drafts.py`：`compute_content_hash` 导入后未使用（定义与真实使用均在 `skill_release.py` 文件内；studio_drafts 侧的导入是死导入）。
- 残留复核：74 个名字逐一在改动后文件中 `grep -c -w` 复查：仅 9 处命中注释/docstring（如 `ai_harness/llm_role_executor.py` 中 `` :class:`RoleExecutor` `` 文档引用、`ai/slo_benchmark.py:5` 文案 "time-to-first-"），可执行代码零残留。

## 二、死代码排查（0 删除，证据在案）

保守候选规则：模块级私有函数/方法（`_` 前缀、非 dunder），全仓文本（46,805 个文本文件，标识符词边界 token 计数）出现次数恰为 1（即仅 def 行自身）。

- 扫描范围内私有定义共 367 个名字 → 候选 17 个，全部集中在 `src/features/teaching_animation/ai/command_catalog.py` 的 `_compile_*` 方法簇（`_compile_replace_element`、`_compile_set_duration`、`_compile_add_marker`、`_compile_apply_template` 等）。
- **全部否定**：`command_catalog.py:171` 存在动态分发：

  ```python
  handler = getattr(self, f"_compile_{name}", None)
  ```

  方法名由命令名经 f-string 运行时拼出，静态扫描天然不可见；删除其中任何一个都会破坏命令分发。
- 结论：**未删除任何死代码**。该 17 个方法为活代码；候选清单与否定证据一并记录在案。

## 三、类型注解补齐（2 处；全范围未注解函数仅 3 处）

1. `src/features/ai_harness/runtime.py:490` `get_metrics` → `def get_metrics(self) -> RunMetricsSnapshot:`
   返回值来自 `aggregate_run_metrics(self.list_runs())`（`rollout_metrics.py:91` 已注解 `-> RunMetricsSnapshot`）。原实现把 `rollout_metrics` 导入保持在函数内（防循环依赖），故注解所需类型经新增 `if TYPE_CHECKING:` 块导入补齐——零运行时影响，事件序列不变。
2. `src/features/teaching_animation/core/devtools_property_mapping.py:231` `edits_to_update_commands` → `-> tuple[tuple[Any, ...], tuple[str, ...]]:`
   与函数体 `return commands, tuple(unmappable)` 及 docstring「Returns (commands, unmappable)」一致；命令类为函数内惰性导入，故用 `Any` 表示命令元组，不新增运行时导入。
3. `src/features/ai_harness/plugin_broker.py:308` `redirect_request`：**按原样保留**。作者已声明 `# type: ignore[no-untyped-def]`（urllib 标准库覆写、函数体纯 `raise`），属有意声明，不算缺口。

## 四、命名收敛（0 处，主动放弃）

`command_catalog.py:171` 的 `getattr(self, f"_compile_{name}")` 证明本代码库存在 f-string 拼接属性名的动态分发模式——私有命名前缀本身可能被运行时依赖。在无法静态证明某私有标识符不被字符串拼接引用的前提下，任何重命名都有破坏行为的风险，故本轮不做任何重命名（含公共 API/信号名，红线本来也禁止）。

## 五、小清理（1 处应用，1 处红线跳过）

- 应用：`src/features/teaching_animation/core/content_localization.py` `sanitized_i18n_dict` 内相邻 `cleaned = {…}` + `return cleaned` 内联为 `return {…}`（纯绑定消除，行为恒等）。
- 跳过：`src/features/teaching_animation/ai/service.py:63` 同模式（`result = insert_motion_graphic_clip(…); return result`）——位于 AI 调用链，红线规定只允许声明级改动，不动。
- 重复字面量提取：未执行（提取会新增模块级名字，与命名收敛同样的动态引用风险，收益不抵风险）。

## 六、验证输出（全部通过）

1. **py_compile**：本任务全部 48 个改动文件逐一 `.venv/bin/python -m py_compile`，0 失败。
2. **定向测试**（全部编辑完成后运行）：
   - `pytest tests/teaching_animation/test_fix_core_aud4.py tests/teaching_animation/test_fix_w6g7_aud30017.py -q`
     → `26 passed in 1.92s`
   - `pytest src/features/ai_harness/tests -q`
     → `641 passed in 4.87s`
   - 无新增失败（未运行 tests/edit 下任何测试）。
3. **裸 except 检查**：`grep -rn "except:" <三目录> --include=*.py | grep -v "/tests/"` → **0 命中**（清理前即为 0，无既有裸 except 需要记录）。
4. **导入冒烟**：`src.features.ai_harness` / `src.features.teaching_animation.core` / `src.features.teaching_animation.ai` 三个包导入全部成功。
5. **删除残留复核**：见第一节抽样证据末条，可执行代码零残留。

## 七、改动文件清单（47 个，均为本任务改动）

import 删除（44 个文件 / 74 处）：

- `src/features/ai_harness/`（16 文件 / 29 处）：analysis_skills、editorial_skills、generation_adapter、help_assistant、invocation_models、legacy_adapters、llm_role_executor、mcp_outbound、plugin_persistence、plugin_trace、result_card、scheduler_adapter、skill_catalog、skill_dry_run、skill_release、skill_studio_drafts
- `src/features/teaching_animation/ai/`（18 文件 / 33 处）：advanced_capabilities、approval_scope、asset_provenance、audio_plan、command_catalog、commands、creative_rubric、creative_templates、design_validator、html_generation_protocol、proposal_card、provider_registry、quality_gate、revision、rollout、service、session、slo_benchmark
- `src/features/teaching_animation/core/`（10 文件 / 12 处）：audio_plan、hierarchy、html_layer_commands、html_template、layout、model、professional、template_commands、text_layout、validator

注解 / 小清理（3 个文件，与上无重叠）：

- `src/features/ai_harness/runtime.py`（get_metrics 返回注解 + TYPE_CHECKING 导入）
- `src/features/teaching_animation/core/devtools_property_mapping.py`（edits_to_update_commands 返回注解）
- `src/features/teaching_animation/core/content_localization.py`（冗余中间变量内联）

工作树同时存在其他并行任务的改动（如 `src/features/ai_harness/tests/test_run_trace.py` 及 `src/features/color/ui/` 多文件），非本任务所为，未触碰。本任务未执行任何 `git add / commit / push`，改动全部留在工作树。
