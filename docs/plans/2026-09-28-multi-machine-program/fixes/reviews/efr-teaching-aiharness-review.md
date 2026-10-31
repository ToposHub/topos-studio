# 复查报告：efr-teaching-aiharness（teaching/core+ai+ai_harness 197 条机械清理）

- 提交：`9240787a4f263f764f5b7f32f7c587827b277723`（[fix-E efr-teaching-aiharness]，53 files，+165/−146）
- 执行报告：`docs/plans/2026-09-28-multi-machine-program/fixes/reports/efr-teaching-aiharness.md`
- 复查日期：2026-10-02 ｜ 只读复查，未运行 pytest

## 结论：PASS-with-notes

## 1. 范围核对（含跨提交归属梳理）

提交含报告新增 + 52 个源文件 = 本报告 47 文件（44 import 删除 + 3 注解/小清理）＋ 打包的 5 个他报告文件：

| 打包文件 | 归属报告 | 内容核对 |
|---|---|---|
| teaching_animation 根 export_service/facade/host_adapters/scene_cache.py（4 个） | efr-small-s2 分节 3/7 | 逐一 diff 核对（shutil/RenderedFrame/field/SceneAssetRef/SvgSceneRenderer/RenderProgress），与 s2 声明一致 ✓ |
| ai_harness/tests/test_run_trace.py | efr-tail-t1 的 AUD-22713 | 断言解耦 diff（len==1 + startswith 位置契约）与 t1 报告一致 ✓ |

其余 47 文件与报告清单（ai_harness 16 文件非 tests + teaching/ai 18 + core 10 + runtime/devtools/content_localization 3）吻合。无越界目录、无 REMEDIATION_PLAN.md/FIXLOG.md 改动。

## 2. 未用导入删除抽查（报告称 74 处，抽样 6 名 + 收敛核验）

| 文件 | 被删名 | 文件内残留（@HEAD） | 判定 |
|---|---|---|---|
| teaching/ai/proposal_card.py | `json` | 0 | ✓ |
| teaching/core/model.py | `Keyframe`（收敛为仅 KeyframeTrack） | 0 | ✓ |
| teaching/ai/design_validator.py | `SUPPORTED_ELEMENT_TYPES` | 0（定义方 core/model 与使用方 core/validator 均直连，不受影响） | ✓ |
| ai_harness/skill_dry_run.py | `uuid` | 0 | ✓ |
| ai_harness/skill_studio_drafts.py | `compute_content_hash` | 0（真实使用在 skill_release 自文件内） | ✓ |
| teaching/ai/command_catalog.py | 顶层 `dataclass` | 0（函数内 `import dataclass as _dataclass`(:699) 惰性导入按规则保留，核实存在） | ✓ |

新增 import 行核验：全部 10 处"新增"实为既有**多行导入收敛为单行**（invocation_models/plugin_runtime/task_graph/subtitle_draft/skill_manifest/skill_studio_drafts 等逐一对照删除行），导入目标模块未新增——无导入期副作用变化 ✓。

## 3. 死代码"保留"判断抽查（本批复查重点：17 候选被 getattr 分发表否定）

- 动态分发表实证：`command_catalog.py:171` 存在 `handler = getattr(self, f"_compile_{name}", None)`（另有 :723/:834 两处同类 getattr 防御读取）——**分发表真实存在**。
- 抽 3 个被否定的候选，方法定义全部在位：`_compile_replace_element`、`_compile_set_duration`、`_compile_add_marker`（各 1 处 def）。
- 判定：**"保留"正确**。静态 token 计数=1 的候选经 f-string 拼接分发运行时可达，删除即破坏命令分发；报告"0 删除 + 证据留痕"与命名收敛整体放弃（第 4 节）的决策链自洽且正确。

## 4. 注解与小清理

- `ai_harness/runtime.py:493` `def get_metrics(self) -> RunMetricsSnapshot:` + 新增 `if TYPE_CHECKING: from ...rollout_metrics import RunMetricsSnapshot`（函数内 rollout_metrics 导入保持原状防环，零运行时影响）✓
- `core/devtools_property_mapping.py` `edits_to_update_commands -> tuple[tuple[Any,...], tuple[str,...]]`（与函数体及 docstring 一致）✓
- `core/content_localization.py` `cleaned = {...}; return cleaned` → `return {...}`（恒等内联）✓
- `plugin_broker.py:308` 带 `# type: ignore[no-untyped-def]` 的原样保留（作者声明，不算缺口）✓

## 5. 红线

- 裸 except：diff 零 except 改动（三目录既有 0 处） ✓
- 信号 connect / emit / pyqtSignal：diff 零命中 ✓
- GLSL/色彩矩阵：零触碰 ✓
- 公共 API 与 `_compile_` 动态分发前缀：零重命名 ✓；`__init__.py` 一律未动 ✓

## 6. py_compile（8 文件）

command_catalog / proposal_card / core/model / design_validator / skill_dry_run / runtime / devtools_property_mapping / facade → 全部 OK。

## 7. 综合判定

**PASS-with-notes**。Notes：① 提交代收 s2 的 4 个根文件与 t1 的 1 个测试文件（内容已逐一核实归属）；② getattr 动态分发否定 17 死代码候选的"保留"判断经验证正确（本批最有价值的保守决策）；③ 其余全部通过。
