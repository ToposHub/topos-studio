# 复查报告：efr-edit-ai-editing（E-rollup 机械清理批）

- 复查对象：提交 `9448ce8b89c21ef5e2b59ad8f4272c8284cc6dd2`（模块行 `src/features/edit/ai_editing/`，248 条 process 级）
- 执行报告：`fixes/reports/efr-edit-ai-editing.md`
- 复查日期：2026-10-01（GLM5.3 复查会话，只读复核，未运行 pytest，未做任何 git 写操作）
- 复查基线：当前 HEAD（复查时 5 提交及其后续提交均已入库）

## 七项判定

| # | 检查项 | 判定 | 依据 |
|---|--------|------|------|
| 1 | 越界 | **PASS** | `git show --name-only`：57 个 .py 全部在 `src/features/edit/ai_editing/` + 1 个报告文件；tests/ 零触碰；REMEDIATION_PLAN.md 零触碰 |
| 2 | 未用导入删除 | **PASS** | 76 个声称删除名逐一验证零代码引用（明细见下）；14 个"有意保留"项抽查 6 项全部属实 |
| 3 | 死代码删除 | **PASS** | 唯一删除项 `_join_url`：全仓 .py 零命中（含 tests/scripts/tools/benchmarks），唯一非代码命中为 `docs/testcoverage/2026-09-28/coverage.json` 历史元数据，与报告一致 |
| 4 | 注解零行为 | **PASS** | 18 处注解全部声明级；plan_compiler 新增 TYPE_CHECKING 块（运行时恒 False）；改动文件均有 `from __future__ import annotations` |
| 5 | 红线 | **PASS** | 新增行零裸 except/零 connect/零 pyqtSignal/零 GLSL 触碰；净删除 def/class 仅 `_join_url` 1 个，公共 API 零改名 |
| 6 | py_compile | **PASS** | 该提交全部 57 个改动源文件在当前 HEAD 下 `py_compile` 全部通过 |
| 7 | 综合结论 | **PASS** | — |

## 抽查明细

### 未用导入（76 名单行 48 + 多行 28，双路覆盖）

单行删除 48 名（脚本比对 diff 删名 vs 当前 HEAD 文件内词边界残留）：

| 结果 | 数量 | 说明 |
|------|------|------|
| 零残留 | 43 | 含 build_manifest、LockedSourceRange、SourceRef、KIND_SNAPSHOT/HISTORY、derive_shot_spans、AssetFingerprint、GenerationBudget、McpTransportError、PluginLifecycle、SkillSource、KeyframeManifest、AnalysisProvenance、GenerationProvenance、AgentMessageStatus、ALL_DOMAINS、DomainArtifactRef、OperationKind、hashlib/json、Any 等 |
| 残留但为散文 | 5 | analysis_service `field`（docstring "field-level provenance"×2）、eval_trace `time`（**L200 `timestamp=time.time()` 位于 EvalTraceStore docstring 用法示例内，非代码**）、ocr_provider `Protocol`（docstring）、plan_compiler `RemoveTransitionCommand`（L204 注释，与报告声称一致）、tts_provider `torch`（脚本误报：实为"删除后按承诺重写并加勿删注释"） |

多行删除 28 名手工核验（文件内词边界零命中）：artifact_store（DeliveryOutlineArtifact/ScriptArtifact/ShotListArtifact/StoryboardArtifact）、assistant_events（FileAttachment/ImageAttachment）、context_service（KIND_SNAPSHOT 顶层 + KIND_HISTORY 函数内惰性句，同句 ContextBlockPriority 正确保留）、generation_provider（GenerationBudget）、ocr_provider（derive_shot_spans/dataclass）、editorial_advanced（hashlib/json）、tool_facade（field/Protocol/AssetFingerprint/Diagnostic）。

全仓其他文件对这些名字的命中均为各自家模块直接导入（如 tests 从 brief_service 导 LockedSourceRange、app/controllers 从 capability_manifest 导 build_manifest），与本模块删除无关。

**专项复核**：plan_compiler REMOVE_TRANSITION 分支内的惰性 `from ...commands import RemoveTransitionCommand` 被删——逐行比对父提交与 HEAD，该分支体仅 `raise CompileError(...)`，导入确为死导入，删除零行为。

### 有意保留项（声称 14，抽 6 验 6）

| 项 | 现状 |
|----|------|
| tts_provider:425 `import torch` 探测 | 在，且已按要求加注释"依赖可用性探测：缺失时须在此抛安装提示（勿删）" |
| analysis_service:309 `"AnalysisJobScheduler \| None"` | 引号注解在，导入在 |
| ocr_provider:102 `"Callable[[str], tuple[ShotSpan, ...]] \| None"` | 引号注解在，ShotSpan 导入在 |
| generation_provider:66/204 `"Callable[...]"` 引号注解 | Callable 保留于 typing 导入行 |
| message_record:74 `:class:`ContextUsage`` | Sphinx 引用在，导入在 |
| motion_graphic_run:191 `:func:`can_write_artifact`` | 同上 |

### 死代码

`_join_url`（llm_client.py，6 行）：`grep -rn -w` 于 src/tests/scripts/tools/apps/benchmarks/plugins 零 .py 命中；非 .py 命中仅 coverage.json。其余 258 个私有定义未删（与报告"仅 1 项达标"一致）。

### 注解（18 处）

plan_compiler 8 处返回注解 + 新增 TYPE_CHECKING 块导入 8 个 Command 类型（运行时零求值）；mcp_vision_gateway `_http_factory -> HttpMcpTransport` 同步删 `# type: ignore[no-untyped-def]`（声明级）；export_service `build_export_config -> tuple[...]` 与实返回值核对一致。文件均有 future-annotations，全部惰性字符串。

### 小清理（3 处内联 return）

assistant_service `merged`、keyframe_sampler `arr`（`val` 保留下）、llm_variant_planner `filtered`——赋值后立即 return 的直译内联，AST 等价，diff 已逐行核对。

## 发现

- **Critical：0；Suggestion：0；Note：1**
- Note：eval_trace.py docstring 示例仍含 `time.time()` 用法示例，示例代码与现行导入不再对应——纯文档瑕疵，零行为影响，不要求整改。

## 结论

**PASS**。76+1 项删除全部零代码引用，14 保留项抽查属实，注解/内联全部零行为，红线全绿，57 文件编译通过。执行报告证据链与 diff 实况完全吻合。
