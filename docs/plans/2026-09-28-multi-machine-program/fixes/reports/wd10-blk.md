# wd10-blk 修复报告 — 前波白名单阻塞项专项（9 条 Major）+ 登记项（2 条）

- 日期：2026-09-30
- 范围：wd8-g47（AUD-13401/13402/13406 BLOCKED 遗留）、wd9-g48（AUD-13900/13901/13902 BLOCKED 遗留）、wd9-g49（AUD-14101/14106 BLOCKED 遗留）、wd9-g50（AUD-14402/14410 BLOCKED 遗留）的 9 条 Major，加登记项 AUD-R2-013（wd5-g31 行程锁）与 AUD-18806/AUD-18813 位置确认（wd6-g8 点名）。
- 白名单文件（本批实际改动，未越界）：`src/features/edit/ai_editing/{edit_api,edit_project_service,media_generation_service,plan_compiler,plan_validator,session_models,skill_registry}.py`、`src/features/composite/core/node_engine.py`、`src/features/edit/core/timeline_ops.py`；按任务指令翻正 2 个既有测试文件、新建 `tests/edit/test_fix_wd10blk_aud.py`（40 用例）。
- 结果：**10 FIXED / 1 INVALID / 0 BLOCKED**（9 条 Major 阻塞项全部 FIXED；AUD-R2-013 FIXED（xfail 已翻正）；AUD-18806 FIXED；AUD-18813 为 INVALID——见下表）。
- 未改 REMEDIATION_PLAN.md（复选框由 orchestrator 统一处理，`git diff` 确认零改动）；未做任何 git 分支操作/commit/push。

## 逐条结论

| ID | 结论 | 修复摘要 |
|----|------|---------|
| AUD-13401 | FIXED | `edit_api.py::_host_approval_service` 不再用 `except Exception: return None` 吞掉宿主访问器异常：协议方法缺失仍返回 None；`fn()` 抛出的异常先 `logger.exception`（完整堆栈留痕）再**原样上抛**——宿主缺陷不再被伪装成"无授权服务 → apply 返回误导性授权失败"，fail loud 且可定位（.clinerules"不得宽泛 except 掩盖领域错误"）。 |
| AUD-13402 | FIXED | TAEP §8"输出不允许静默覆盖"在 `EditSession.request_export` 的 job 创建边界强制执行：解析后的输出路径已存在且（无 grant 或 `PathGrant.allow_overwrite=False`）→ 拒绝创建 job，返回 `{"job": None, "preflight": PreflightReport(ok=False, blocking, diagnostics=(TAEP_PERMISSION_DENIED 诊断,)), "config": None}`；仅 `allow_overwrite=True` 的显式用户授权放行覆盖。`PathGrant` docstring 同步标注该位已是强制执行点（此前全库无消费者）。export_service.py（建议的备选落点）不在白名单，检查收敛在本文件调用边界，错误形状与既有 preflight blocking 契约一致。 |
| AUD-13406 | FIXED | `edit_project_service.py::commit_command` 的 revision 冲突不再 `raise RevisionConflict`，折叠为 `CommitResult(applied=False, error=str(conflict), errors=(str(conflict),))`——与 domain 校验失败同走 `applied=False` 错误通道，公开"统一写入入口"的 docstring 承诺（失败 → applied=False 且 error 描述原因，含 base/current revision）成立。`RevisionConflict` 类保留（含 base_revision/current_revision/timeline_id 结构化字段）供深层调用方；`TransactionService.commit` 既有 `except RevisionConflict` 分支成为防御性兜底（不再触发，行为不变）。锁定旧行为的 `test_edit_project_service.py::test_commit_command_revision_conflict_raises`（原断言 `pytest.raises(RevisionConflict)`）按协议翻正为锁定新通道，并补 `RevisionConflict` 公共 API 结构化字段断言。 |
| AUD-13900 | FIXED | `media_generation_service.py`：`_check_cost` 超阈值时产出 `WARNING` 级 `QUOTA_EXCEEDED` 诊断（携 `check_alert` 消息，替换原 `pass` 丢弃）；`_prepare_generation` 早退判定从"有诊断即阻断"收紧为"有 **ERROR** 级才阻断"，warning 随第二个返回值流动；三个入口（`request_generation` 合并 execute 诊断、`request_generation_async`、`resume_generation_async`）把 WARNING 送达调用方——§10.2 月度成本告警链路恢复（提醒非阻断语义保持：job 照常创建并成功）。硬 gate（`max_cost_units<=0`）仍 ERROR 阻断。 |
| AUD-13901 | FIXED | `CostLedger` 增加内部 `threading.RLock`（dataclass `field(init=False, repr=False, compare=False)`，不影响构造/repr/相等语义、实例间不共享）；`add`（读-改-写）/`total_for`/`grand_total`/`reset` 全部锁内执行，`check_alert` 经 RLock 安全复用 `total_for`。默认 2 worker 的 `JobWorkerPool` 并发完成不再丢失成本累计（成本台账线程安全，.clinerules 硬约束）。 |
| AUD-13902 | FIXED | 取消 TOCTOU 关闭（.clinerules 硬约束）：新增 `GenerationService._finalize_lock`（RLock）；worker 侧"终态判定 → CANCELLING 收尾或候选入箱 + `job.complete`"整体置于锁内临界区，`cancel()` 与 `close()` 的判死循环同持该锁。取消要么发生在入箱前（候选丢弃、Job 收尾 CANCELLED），要么发生在 complete 之后（`request_cancel` 对终态 no-op，已交付结果保留）——"已取消却多了候选"的窗口消除（§11.1 红线）。早退 CANCELLING 分支的 `complete({})` 同样移入锁内并复查；`close()` 的判死循环持锁后与 worker 临界区互斥（顺带收窄 wd9-g49 记录的 "complete 后被 fail 改写" 残余窗口：锁内 complete 的 Job 不再出现在 `list_active()`）。 |
| AUD-14101 | FIXED | `plan_compiler.py::_compile_operation` 的异常收敛扩展为 `except KeyError`（missing param，原文案保留）+ `except (ValueError, TypeError)`（新：`invalid param value/type (<ExcType>): ...`，携 op_id/kind 的 `CompileError`）——`int("abc")`、`int(None)`、`state["mute"]` 字符串索引等来自 LLM 外部数据的强转错误不再以未分类异常穿透 `preview()/commit` 只捕 `CompileError` 的错误协议（与 wd9-g49 给出的处方一致；`CompileError` 自身继承 Exception，不会被误捕获重包）。 |
| AUD-14106 | FIXED | `plan_validator.py::_validate_operation_domain`：SET_CLIP_FADE 新增 fade 约束（fade_in/fade_out 至少其一存在；提供的值须为非负 int，bool 排除——复用 ADD_SUBTITLE 的 `not isinstance(x, int) or isinstance(x, bool)` 模式）；ADD_TRANSITION 新增 duration 约束（显式提供时须为正 int 帧；缺省仍走 compiler 默认 12）。`duration:"abc"/0/-3/12.0/True`、`fade=-5/"8"`、双 fade 缺失在 validate 阶段即产出 SCHEMA_INVALID/TRANSITION_INVALID 可修复诊断，不再以 VALID 放行后在 compile 期炸未分类异常（与 AUD-14100/14101 的故障链闭环）。 |
| AUD-14402 | FIXED | `session_models.py` 新增 `UnsupportedCatalogVersionError(ValueError)`（含 found_version/supported_version 结构化字段，入 `__all__`）；`SessionCatalog.from_dict` 对 `version > SESSION_CATALOG_VERSION` 显式拒绝上抛——未来版本目录不再被"解析成功 + 只取已知字段"静默接受（下一次 `to_dict()` 落盘即字段蒸发）。`AssistantSessionStore.load` 的畸形 payload 边界（`_PAYLOAD_ERRORS` 含 ValueError）会捕获并 warning + 回退，工程打开不崩溃；v1 迁移与 v2 round-trip 行为不变。 |
| AUD-14410 | FIXED | `skill_registry.py::register` 升级冲突分支传入 REVISION_CONFLICT 模板所需变量 `base=existing.version, current=manifest.version`，完整说明（skill_id + 双版本号 + "use upgrade with higher version"）放入 `details={skill_id, registered_version, new_version, reason}`——消息不再渲染为带 `{base}`/`{current}` 花括号的原始占位符，真实原因不再丢失（模板文本属 diagnostics.py，不在白名单，按审计第一方案闭合）。 |
| AUD-R2-013 | FIXED | 图引擎环依赖检测真实工作（.clinerules 硬约束）：`ExpressionManager.detect_circular_dependency`（utils/expression_parser.py，**不在本批白名单**）的 DFS 在首轮即把起点标记 visited、起点永不被重新入栈，任何环（含自引用）都检不出。该函数全库唯一消费者是 `node_engine.py:864`（引擎 set_node_expression 的环检查点），故在白名单内的 `NodeGraph` 上新增 `_expression_would_cycle`（正确 DFS：沿依赖边遍历，经 ≥1 条边回到起点即环；持图锁，经 manager 的 `get_dependencies` 读取，无反向锁序），引擎调用点改用它——双节点环/自引用/三节点长环均被拒绝且回滚干净（node 与 manager 表达式双双移除），无环链照常接受；wd5-g31 的 `xfail(strict=True)` 行程锁 `test_cyclic_expressions_in_one_plan_are_rejected` 按任务指令翻正为常规通过用例。**残留**：`ExpressionManager.detect_circular_dependency` 本体仍是死 DFS（现零消费者），建议持有 expression_parser.py 白名单的批次同步修复或删除该方法。 |
| AUD-18806 | FIXED | `timeline_ops.py` 遗留纯函数全部改用 w6-g8 的 `copy_clip_with`（replace 语义 + 动态属性回拷 + 淡入出钳制）：`split_clip` 左/右半片（右半片沿用 `fade_in_duration=0`、新 id、`linked_clip_id=None` 语义）、`trim_clip_left`、`trim_clip_right`、`move_clip`——label_color/waveform_show_channels/freeze_source_frame/blend_mode/audio_gain_db/raw/FPS 元数据等全部扩展字段不再回落默认值（w6-g8 点名的 split 六字段漏传一并闭合）；`add_transition` 重加分支保留原 transition 的 `params`（此前重建为空 dict）。剩余三处 `ClipInstance(...)` 构造为"从素材新建片段"（insert/overwrite/append），无源片段字段可保留，不属于本条范围。 |
| AUD-18813 | INVALID | **该 ID 不存在于 REMEDIATION_PLAN.md**（`grep -c "AUD-18813" REMEDIATION_PLAN.md` → `0`；仅出现于 docs/audit/2026-09-28/batches/batch-188-features-edit-core.md:238 的原始审计发现与 w6-g8 报告的"已单列、本批不处理"注记，从未登记入计划）。且其建议方向（split 左右半片不共享 `render_cache_id`）与已登记并修复的 AUD-18800 契约**直接冲突**——w6-g8 的 `copy_clip_with` 语义与 `tests/edit/test_fix_w6g8_aud6.py` 锁定"重建保留全部扩展字段（含 render_cache_id）"，本批 AUD-18806 修复正是对齐该契约。无计划条目即无修复授权，本批不改动；如需采纳应先经 orchestrator 正式登记并裁决两项契约的优先级。 |

## 设计决策与残留说明

- **AUD-13406 的通道统一方向**：审计给出"折叠为 CommitResult"与"仅修 docstring"两案，取第一案（审计首选）：统一写入入口的失败语义真正一致，`TransactionService.commit` 无需依赖异常兜底（其 `except RevisionConflict` 保留为防御）。代价是 1 个锁定旧行为的既有断言按协议翻正（见上表），`errors` 元组携带含 base/current 的完整冲突文本，`tests/edit/ai_editing/test_transaction_service.py` 的既有断言（`any("revision" in e.lower())`）无需修改即通过。
- **AUD-13402 的执行边界**：检查在 `request_export` 创建 job 前（审计建议位置），属 TOCTOU-best-effort（实际写文件由导出 worker 异步执行，本批白名单内无 worker 侧落点）；无 grant 但目标不存在的请求保持既有放行语义（不收紧为"导出必须 grant"，避免破坏现有调用方）。`ExportRequest` 增加 allow_overwrite 字段的建议落点在 export_service.py（不在白名单），授权语义由 grant 携带并在本边界强制。
- **AUD-13900 的非阻断语义**：`_prepare_generation` 的早退从"诊断非空"改为"ERROR 级非空"是必要配套——否则 WARNING 会把 `request_generation` 的 gate 判成阻断，与 docstring"alert 是提醒非阻断"矛盾。consent/resolve 失败仍整体阻断（全部为 ERROR 级诊断）。
- **AUD-13902 的锁选择**：`_finalize_lock` 只包住"判定 + 入箱 + complete"短临界区（provider 调用、校验、ledger 均在锁外），锁序恒为 finalize → registry/bin，无反向获取路径，无死锁面。jobs.py 的 `JobHandle.complete/fail` 不校验终态的窄窗口（wd9-g49 残余、属 AUD-13701 域）仍待 jobs.py 白名单批次内置修复；本批以 close() 持锁收窄了主窗口。
- **AUD-14402 的拒绝方式**：抛 ValueError 族异常（而非静默回退空目录）——复用 `AssistantSessionStore.load` 既有 AUD-13018 边界（捕获 + warning + 回退 v1/空目录），零新增耦合即获得"不崩溃 + 不静默覆盖落盘"。若 store 落盘覆盖的彻底防护要做在 store 层（白名单外），当前已保证 from_dict 契约本身不再"解析成功但字段蒸发"。
- **AUD-R2-013 的落点说明**：根因函数在 `utils/expression_parser.py`（不在白名单），本批在唯一消费者（引擎）侧以正确实现替换调用，环检测在引擎与 plan 路径（CompositeEditService.apply → set_node_expression）真实生效；死 DFS 方法本体留待持有该文件的批次（残留已写入上表）。wd5-g31 报告中"引擎修复后自动获得拒绝能力"预期兑现。
- **AUD-18806 与 AUD-18813 的关系**：本批修复严格对齐已登记条目 AUD-18806（及 w6-g8 残留注记的 split 六字段）；AUD-18813 无计划条目且与 AUD-18800 锁定契约冲突，判 INVALID（证据见上表），未做任何行为改动。
- **锁定旧行为协议处置（2 个测试文件）**：(1) `tests/edit/ai_editing/test_edit_project_service.py::test_commit_command_revision_conflict_raises`（原断言 raise RevisionConflict → 现断言 applied=False + 错误文本含 base/current + 两层不变，AUD-13406 修复所锁定旧行为的翻正）；(2) `tests/composite/test_fix_wd5g31_aud.py::test_cyclic_expressions_in_one_plan_are_rejected`（xfail(strict=True) 行程锁翻正为常规用例——任务指令明确要求，REMEDIATION_PLAN §"修复一条→勾选并同时翻转对应 xfail 断言"协议）。此外无任何既有断言被修改。

## 验证记录

- `py_compile`（`.venv/bin/python -m py_compile`，全部退出 0，"PY_COMPILE ALL OK"）：9 个改动源文件 + `tests/edit/test_fix_wd10blk_aud.py` + 2 个按协议更新的既有测试文件，共 12 个。
- 新建回归：`tests/edit/test_fix_wd10blk_aud.py` → **40 passed**（覆盖全部 10 条 FIXED：宿主异常上抛×3、覆盖门×4、冲突通道、告警链路×4、并发累计×2、TOCTOU×2（含"取消恰在入箱前到达→素材箱为空、Job CANCELLED"的确定性复现）、编译错误协议×4、validator 约束×6、未来版本拒绝×2、模板渲染×2、环检测×5（双节点/自引用/三节点环/无环链+回滚/plan 路径）、扩展字段保留×5（split/trim 左/右/move/转场 params，含动态属性与全部 22 个扩展字段断言））。
- 邻接既有测试（均按原断言、零修改）：
  - `tests/edit/ai_editing/`（全目录，含 edit_api/edit_project_service/media_generation_service/plan 管线/skill/session 全部邻接）→ **2009 passed, 4 skipped**。
  - `tests/composite`（全目录，含 node_engine/环检测/expression/edit_service/undo）→ **7044 passed, 13 skipped, 1 xfailed**（xfailed 为无关的 BUG-B2-02 UI 面板回归锁；wd5-g31 的环检测行程锁已翻正为通过）。
  - timeline_ops 邻接：`test_timeline_ops.py` + `test_fix_w6g8_aud6.py` → **57 passed**；命令层 11 文件（edit_commands/timeline_command_regressions/trim_professional/native_transitions/clip_blend_modes/subtitle_snap/sync_lock_ripple/lift_extract_range/track_sync_lock/clip_fps_metadata_propagation/fix_wd8g46）→ **267 passed**；plan 管线消费者 4 文件（caption_graphics/caption_delivery_extensions/adjustment_clip_delivery/clip_field_preservation）→ **46 passed**。
  - `tests/ai/`（全目录）→ **625 passed, 10 skipped**。
  - 宿主/控制器消费者：`test_ai_controller_agent_runtime_v2` + `test_fix_wd1g2_aud` + `test_toposctl_offline` + `test_timeline_capabilities` → **96 passed**；`test_ai_e2e_deterministic` + `test_ai_command_stack_commit` + `test_ai_edit_controller` + `test_ai_persistence_wiring` + `test_ai_payload_io` + `test_p0_wiring_jobs` → **57 passed**。
  - 前波回归（共享文件域）：wd8-g47/45/46 + wd9-g48/49/50/55/56 共 8 个测试文件 → **302 passed, 2 skipped**（本批对 media_generation_service 的 cancel/close 改动与 wd9-g49 锁定行为兼容）。
  - 同波兄弟批次回归：wd10g51/57/60/63 → **174 passed**。
- 锁定旧行为协议：仅上述 2 处计划内翻正；无既有测试因本批改动而失败。
- REMEDIATION_PLAN.md 零改动（`git diff` 确认）；无 git 分支/commit/push 操作。

## 残留与后续建议

1. **AUD-R2-013**：`ExpressionManager.detect_circular_dependency`（utils/expression_parser.py:1072-1098）本体仍是死 DFS，现已被引擎旁路（零消费者）。建议持有该文件白名单的批次直接修复 DFS（把"起点经 ≥1 条边回到自身"作为判据）或删除该方法，消除"看起来有检测、实际恒 False"的陷阱。
2. **AUD-13402**：导出 worker 实际写文件前的二次存在性检查（write-time enforcement）与 `ExportRequest.allow_overwrite` 字段落点均在 export_service.py / 导出 worker（白名单外），job 创建边界的强制执行已闭合审计指出的授权合同失效。
3. **AUD-13902**：`JobHandle.complete/fail` 不校验终态的窄窗口（jobs.py，AUD-13701 域）仍待内置状态机校验；本批已将 close() 判死与 worker 临界区互斥。
4. **AUD-18813**：如 orchestrator 决定采纳（split 半片不共享 render_cache_id），需先裁决与 AUD-18800"重建保留全部扩展字段"锁定契约的优先级，并同步更新 w6-g8 与本批的保留断言。
