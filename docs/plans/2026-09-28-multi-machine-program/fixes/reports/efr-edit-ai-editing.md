# EFR 报告：`src/features/edit/ai_editing/` 机械清理

- 日期：2026-10-01
- 分支：dev（仅工作树改动，未 git add/commit；REMEDIATION_PLAN.md 未触碰）
- 范围：`src/features/edit/ai_editing/` 全模块（约 150 个 .py，含模块内 tests/ 子目录扫描但未改动测试）
- 原则：纯机械清理、零行为变化、最小 diff
- 改动统计：57 个文件，+94 / -114 行（净 -20）

---

## 一、分类统计

| 类别 | 数量 | 说明 |
|------|------|------|
| 1a. 删除未使用 import | 76 个名字（51 个文件） | AST 扫描 90 候选 → 全仓精确核实 → 删 76 |
| 1b. 有意保留的"未使用" import | 14 个名字 | 引号注解前向引用 11 + 依赖可用性探测 1 + docstring Sphinx 引用 2（见下） |
| 2. 删除死代码 | 1 处（6 行） | 259 个私有顶层 def/class 全量扫描，仅 `_join_url` 零引用 |
| 3. 补类型注解 | 18 个签名（7 个文件） | 全模块缺注解函数恰好 18 个，全部补齐；均为可确证类型，无一处用 Any 兜底 |
| 4. 命名收敛 | 0 | 拼写错误 sweep（recieve/seperate/occured/verison/paramter 等 30+ 模式）零命中 |
| 5. 小清理 | 3 处 | 赋值后立即 return 的冗余中间变量内联（AST 扫描全模块仅 3 处） |

> 248 条覆盖情况：五个分类全部扫描完毕。未用导入 + 死代码 + 注解主体已做完；
> 命名收敛与小清理扫描后无可安全应用项（分别 0 / 3 处），无剩余积压。

## 二、抽样证据

### 1. 未使用 import（AST + 全仓三层核实）

核实链条（每个候选都过三关，任一不过即保留）：
1. `ast.parse` 全文件收集：import 绑定名在 Name 节点 + `__all__` 字符串中零使用；
2. 精确再导出检测：全仓所有 `ImportFrom` 按 AST 解析（含相对导入 level 解析），
   确认无任何文件从本模块 import 该名字（唯一命中是 assistant_service 从
   assistant_events 的 import——本身就是待删项，链式核实后双删）；
3. 同文件引号注解 / getattr 字符串 / 文档提及复核。

删除样例（完整清单 76 项，按文件）：

| 文件 | 删除的名字 |
|------|-----------|
| artifact_store.py | DeliveryOutlineArtifact, ScriptArtifact, ShotListArtifact, StoryboardArtifact（多行 import 块内 4 行整行删除） |
| assistant_events.py | FileAttachment, ImageAttachment |
| context_service.py | KIND_SNAPSHOT（顶层块）+ KIND_HISTORY（函数内惰性 import，仅删名保留同句 ContextBlockPriority） |
| editorial_advanced.py | hashlib, json（两行整行删） |
| generation_provider.py | Any, GenerationBudget（同句 Callable 因引号注解保留） |
| ocr_provider.py | dataclass, Protocol, derive_shot_spans（该文件用 `importlib.util.find_spec` 做依赖探测，不用 @dataclass/Protocol） |
| tool_facade.py | field, Protocol, AssetFingerprint, Diagnostic |
| plan_compiler.py | RemoveTransitionCommand（仅 L193 注释提及"RemoveTransitionCommand.create 需要 timeline"） |
| 其余 43 个文件 | `field`×21、`time`×4、`Callable`/`Protocol`/`Iterable`/`Any` 等 typing 名 ×9、`build_manifest`、`LockedSourceRange`、`SourceRef`、`McpTransportError`、`PluginLifecycle`、`SkillSource`、`KeyframeManifest`、`AnalysisProvenance`、`GenerationProvenance`、`GenerationProvider`、`Path`、`OperationKind`、`ALL_DOMAINS`、`DomainArtifactRef`、`AgentMessageStatus`、`can_write_artifact` 除外（见保留项）等 |

有引用而保留（14 项，含证据）：
- **引号注解前向引用**（删了会让 `get_type_hints` 类运行时求值/类型工具失真，且按惯例属"有引用"）：
  `analysis_service.py` AnalysisJobScheduler（L309 `"AnalysisJobScheduler | None"`）、
  `edit_api.py` ApprovalScope/ApprovalService（L111）、`edit_project_service.py` EditCommand（L167）、
  `generation_provider.py` Callable（L67/205）、`llm_variant_planner.py` Callable（L127/160）、
  `media_generation_service.py` JobEvent/JobMetrics（L99/103）、`ocr_provider.py` ShotSpan（L104）、
  `semantic_index.py` AnalysisService（L279）、`variant_planner.py` Callable（L112）
- **依赖可用性探测**（惰性 import，删除会改变异常行为——红线）：
  `tts_provider.py:425` `import torch`（`try/except ImportError → RuntimeError("请安装 qwen-tts、torch、soundfile")`）。
  已按任务要求加一行注释：`import torch  # 依赖可用性探测：缺失时须在此抛安装提示（勿删）`
- **docstring Sphinx 角色引用**（保守保留）：`message_record.py` ContextUsage（L75 `:class:`ContextUsage``）、
  `motion_graphic_run.py` can_write_artifact（L191 `:func:`can_write_artifact``）

### 2. 死代码（删 1 处）

扫描方法：AST 收集模块内 259 个下划线开头的顶层 def/class → 全仓 .py 单遍 token 化
（标识符级、含字符串内容），定义行之外零命中才列为候选。结果仅 1 项：

| 位置 | 标识符 | 证据 |
|------|--------|------|
| `llm_client.py`（原 L611，6 行） | `_join_url` | 全仓 .py token 零命中（含 tests/字符串）；唯一非代码命中是历史产物 `docs/testcoverage/2026-09-28/coverage.json` |

未删（有引用）：其余 258 个私有顶层定义全部有活跃引用（含 `assistant_service` 调用的
`BriefService._merge`、`transcript_provider` 的 `_resolve_torch_device`/`_cpu_fallback_kwargs`
跨模块私有引用等——本报告未做任何跨模块私有引用改动）。

### 3. 类型注解（18 个签名，全部声明级）

全部文件已有 `from __future__ import annotations`，注解为惰性字符串，运行时零影响；
核心模块类型名经查证后经 `TYPE_CHECKING` 块引入（5 个文件新增 3-12 行 TYPE_CHECKING 块）：

| 文件 | 函数 | 补充注解 |
|------|------|---------|
| plan_compiler.py（8） | `_compile_add_clip` | `-> AddClipCommand` |
| | `_compile_trim` | `-> TrimClipCommand` |
| | `_compile_replace_clip` | `-> UpdateClipSourceCommand` |
| | `_compile_set_fade` | `-> ChangeClipFadeCommand` |
| | `_compile_set_param` / `_compile_set_effect` / `_compile_remove_effect` | `-> UpdateClipParamCommand` |
| | `_compile_set_track_state` | `-> SetTrackMuteCommand \| SetTrackLockCommand \| SetTrackSoloCommand`（三分支 return 全部核实） |
| plan_validator.py（4） | `_advance_projection` | `(projection: TimelineSequence, …) -> TimelineSequence`（caller：`projection = sequence`（`state.sequences.get`）→ `compiled.macro.apply` 返回值） |
| | `_validate_operation_domain` | `sequence: TimelineSequence`（call site 在 `if sequence is not None:` 块内） |
| | `_asset_duration_frames` | `asset: MediaAsset`（caller `project.assets.get(asset_id)`） |
| | `_check_transition_adjacency` | `track: EditTrack`（caller `sequence.get_track_by_id(...)`；`all_tracks -> tuple[EditTrack, ...]`） |
| export_service.py（2） | `run_export_preflight` | `config: TimelineExportConfig, sequence: TimelineSequence`（caller 传 `build_export_config` 产物 + `state.sequences.get(...)`） |
| | `ExportService.build_export_config` | `-> tuple[TimelineExportConfig \| None, Diagnostic \| None]`（返回 `(config, None)` / `(None, make_diagnostic(...))` 均核实） |
| snapshot_service.py（1） | `_build_track_summary` | `sequence: TimelineSequence, track: EditTrack` |
| transaction_service.py（1） | `compute_diff` | `base: TimelineSequence, proposed: TimelineSequence`（docstring 明示） |
| mcp_vision_gateway.py（1） | `_http_factory` | `-> HttpMcpTransport`（同文件姊妹工厂 `_factory -> SubprocessStdioTransport` 已有同风格注解；同步删除已失效的 `# type: ignore[no-untyped-def]`） |
| operation_protocol.py（1） | `_op` | `**kw: Any` |

补充后重扫：缺注解函数 = 0（原 18）。拿不准的类型（如 `_timeline_clips(sequence: Any)`
内部参数）一律不动。

### 4. 命名收敛

未执行任何重命名。30+ 常见拼写错误模式全模块 grep 零命中；无模块内私有标识符拼写
不一致需收敛；公共 API 名称零改动。

### 5. 小清理（3 处，AST 扫描"赋值后紧接 return 同名变量且无其他使用"模式）

- `assistant_service.py:594`：`merged = self._brief._merge(...); return merged` → 直接 return
- `keyframe_sampler.py:187`：`arr = np.full(...); return arr` → 直接 return（`val` 仍被使用，保留）
- `llm_variant_planner.py:239`：`filtered = [...]; return filtered` → 直接 return 列表推导

重复字面量提取：未发现零风险目标（模块内字面量多为中文提示/参数键名，提取反而增加 diff）。

## 三、跳过项（含原因）

- `tts_provider.py` torch、引号注解 11 项、Sphinx 引用 2 项：见 1b，均有引用，不满足删除标准。
- `except:` 语义：本报告未改任何 except 子句。
- 测试文件（模块内 `tests/` 子目录）：零改动。
- `git` 写操作：零（分支保持 dev，工作树留改动）。

## 四、验证输出（全部通过）

1. `py_compile`：57 个改动文件逐一编译 → 0 失败
2. 模块导入冒烟：`importlib.import_module` 遍历 `src.features.edit.ai_editing` 全部子模块 → 全部 OK
3. 本模块测试：
   `.venv/bin/python -m pytest tests/edit/ai_editing -q -p no:cacheprovider`
   → **2105 passed, 4 skipped, 1 warning（24.67s）**
4. 消费方定向测试（改动模块的直接外部消费方）：

| 测试文件 | 覆盖模块 | 结果 |
|----------|---------|------|
| tests/edit/test_fix_wd12g52_aud.py | transaction_service.compute_diff | 117 passed（三文件合计，17.46s） |
| tests/app/test_fix_wd1g2_aud.py | export_service.run_export_preflight（monkeypatch 目标核实仍在） | 同上 |
| tests/app/test_ef08_aud.py | export_service | 同上 |

5. `grep -rn "except:" src/features/edit/ai_editing/` → 2 命中，均为 docstring/注释散文
   （`assistant_service.py:248`、`assistant_service.py:1037`，文字提及"取代 broad except: pass"），
   **真实裸 except 子句 = 0**；清理前后一致，未修改。
6. `git diff` 仅涉及 `src/features/edit/ai_editing/` 下 57 个文件；未 add/commit。

## 五、改动文件清单（57 个）

action_contract, agent_metrics, analysis_scheduler, analysis_service, approval_service,
artifact_models, artifact_store, assistant_events, assistant_service, audit_store,
capability_manifest, context_service, cross_domain_cost, edit_api, editorial_advanced,
eval_runner, eval_trace, evidence_kinds, export_service, generation_bin, generation_provider,
generation_provider_pack, help_index, keyframe_sampler, kill_switch, llm_client,
llm_variant_planner, mcp_gateway, mcp_vision_gateway, media_generation_service,
message_record, music_policy, ocr_provider, operation_protocol, plan_compiler,
plan_repository, plan_validator, professional_skills, provider_capabilities, provider_plugin,
quality_gate, release_gate, safe_path_policy, skill_registry, skill_runtime, snapshot_service,
task_artifacts, tool_call_log, tool_facade, tool_result, transaction_service,
transcript_router, tts_provider, tts_settings, variant_planner, visual_semantic_provider,
workflow（均位于 `src/features/edit/ai_editing/` 下）
