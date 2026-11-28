# Topos AI Assistant 隐私与安全验收清单（M0 基线）

> 本文是 AI Assistant / Agent Harness 的**隐私与安全硬门槛清单**，对应产品计划
> `docs/ai_assistant_agent_harness_product_plan_2026-08-01.md` 的 §0 红线、§1.2 不变量、
> §10 数据/审计/隐私与 §15.5 风险登记表。它定义"什么才算安全"，并标注每一条当前
> 的验收方式与所属里程碑。
>
> 状态图例：✅ 已达成并有用例覆盖 · 🟡 部分达成 / 契约存在待接线 · ⬜ 待实施（属于
> 后续里程碑）· 🔒 硬门槛（违反即阻断发布，对应 §12.2 的"未经确认的写入 = 0、
> 泄露 secret/路径 = 0"）。

---

## 1. 写操作必须显式确认（🔒 硬门槛）

| # | 要求 | 状态 | 验收方式 | 里程碑 |
| --- | --- | --- | --- | --- |
| 1.1 | 未经用户确认不得**改时间线** | ✅ | `tests/edit/ai_editing/test_approval_service.py`：伪造/重复/换工程/换 timeline/换 revision grant 全部拒绝；`tests/app/test_ai_command_stack_commit.py` 证明 commit 走真实 CommandStack 且 Undo 恢复 | M1 |
| 1.2 | 未经用户确认不得**分析整批素材** | ⬜ | `media_analysis` flag 默认关闭；M3 实施 `MediaAnalysisController` + `AnalysisRequest.user_confirmed_at` 后补 e2e | M3 |
| 1.3 | 未经用户确认不得**上传数据到云** | ⬜ | `cloud_analysis` flag 默认关闭；M3/M4 数据上传提示 + 范围/费用确认卡 | M3/M4 |
| 1.4 | 未经用户确认不得**生成付费素材** | ⬜ | `generation` flag 默认关闭；M11 `GenerationRequest` 同意 + 成本上限 | M11 |
| 1.5 | 未经用户确认不得**导出/发布文件** | ⬜ | `export_service.py` 已有 PathGrant 概念（🟡）；M11 补最终确认 + 覆盖二次确认 | M11 |

**红线来源**：§0「未经用户确认不得分析整批素材、上传数据、改时间线、生成付费素材或导出文件」。

---

## 2. 编辑闭环必须是单事实源、可撤销（🔒 硬门槛）

| # | 要求 | 状态 | 验收方式 | 里程碑 |
| --- | --- | --- | --- | --- |
| 2.1 | 所有 AI 编辑走 `EditPlan → Preview/Diff → Approval → CommandStack → Undo/Redo` | ✅ | `transaction_service.py` `preview`/`commit`；`plan_compiler.py` 编译为 `MacroCommand` | M1 |
| 2.2 | Preview 绑定 `project_id/timeline_id/base_revision/plan_hash/diff_hash/manifest_hash`，任一变化即 stale | 🟡 | `plan_repository.state_fingerprint` 绑 5 维，缺 `diff_hash`（M1 收尾补） | M1 |
| 2.3 | AI 提交编译为单个 `MacroCommand`，一次 Undo 恢复全部 | ✅ | `tests/app/test_ai_command_stack_commit.py::test_bridge_commit_then_undo_restores_timeline` | M1 |
| 2.4 | 任一中间步骤失败不产生半套状态 | ✅ | `tests/edit/ai_editing/test_transaction_service.py::test_commit_partial_failure_no_half_state` | M1 |
| 2.5 | revision 冲突不自动 last-write-wins | ✅ | `test_commit_revision_conflict_returns_not_applied` | M1 |

**红线来源**：§1.2 不变量 1/3/5、§1.3 不做。

---

## 3. 工程文件不保存敏感数据（🔒 硬门槛）

| # | 要求 | 状态 | 验收方式 | 里程碑 |
| --- | --- | --- | --- | --- |
| 3.1 | API key / secret 仅存系统 keychain，不进工程文件 | ✅ | `credential_store.py`；`assistant_session_store.py` 注释明说不存 secret | M0/M1 |
| 3.2 | 不保存绝对机器路径 | 🟡 | `safe_path_policy.py` 已有 PathGrant；待 M2 工程序列化扫描 | M2 |
| 3.3 | 不保存不可重建的大型缓存（embedding/waveform/代理） | ⬜ | M2/M3 分析存储分层（§17.6）：工程 payload vs 可重建 cache vs 索引 | M2/M3 |
| 3.4 | 结构变更有 schema version + migration + 未知字段保留 | 🟡 | `schema.py` 已有 EditPlan JSON Schema；分析数据 schema migration 待 M2 | M2 |

**红线来源**：§0「项目只保存版本化、可解释的结果；API key、绝对路径和可重建的大型缓存不进入工程文件」、`.clinerules` 项目兼容。

---

## 4. 能力默认关闭、最小权限（🔒 硬门槛）

| # | 要求 | 状态 | 验收方式 | 里程碑 |
| --- | --- | --- | --- | --- |
| 4.1 | 5 个 AI 能力 flag 默认关闭 | ✅ | `src/shared/feature_flags.py` `AI_CAPABILITY_FLAGS` 全 False；`tests/architecture/test_feature_flags_registry.py::TestAiCapabilityFlags` | M0 |
| 4.2 | 任何 product mode 下 AI 能力都关闭 | ✅ | `PRODUCT_MODE_PRESETS` 各 mode 注入 `_ai_capability_defaults()` | M0 |
| 4.3 | 能力开启只能经 `TOPOS_FEATURE_*` env 或 config 显式 true | ✅ | `load_feature_flags` precedence 链；测试覆盖 | M0 |
| 4.4 | 首发范围写入产品配置 | ✅ | `config.json` `ai_assistant.release_scope` = 口播/访谈/产品短视频 | M0 |
| 4.5 | Agent 无项目/无选择/无授权/无 capability 时明确说明限制 | ⬜ | M5 Agent Dock V2 + `agent_policy.py` | M5 |

**红线来源**：§1.2 不变量 4「最小权限且可见」、§11.2 M0 退出条件。

---

## 5. 界面不阻塞、任务可取消

| # | 要求 | 状态 | 验收方式 | 里程碑 |
| --- | --- | --- | --- | --- |
| 5.1 | 推理/转录/检索/渲染/生成/导出都是可取消 Job | 🟡 | `jobs.py` `JobHandle` 有 cancel；分析执行仍同步（M3 接 scheduler） | M3 |
| 5.2 | Qt UI 线程与 renderer 线程不跑模型推理/全片解码/网络 | ⬜ | M3 worker process；`.clinerules` 线程与 GPU 约束 | M3 |
| 5.3 | 实时播放/导出优先级高于后台分析 | ⬜ | M3 资源池分级 | M3 |
| 5.4 | 取消父任务取消未开始的子任务，进行中 Job 安全取消 | ⬜ | M9 `task_graph.py` 取消传播 | M9 |

**红线来源**：§0「所有长任务都进入可取消 Job」、§1.2 不变量 7。

---

## 6. 脱敏、网络边界、注入隔离

| # | 要求 | 状态 | 验收方式 | 里程碑 |
| --- | --- | --- | --- | --- |
| 6.1 | 日志不写 API key、请求头、绝对路径 | 🟡 | 现有模块不显式打印；待补日志扫描测试 | M0/M5 |
| 6.2 | 错误页/诊断不显示 secret/路径 | ✅ | `diagnostics.py` 脱敏；`safe_path_policy.py` | M1 |
| 6.3 | 网络请求默认只到 profile/MCP allowlist 域名 | ⬜ | M5/M10 network allowlist | M5/M10 |
| 6.4 | prompt / MCP 输出 / OCR / 字幕 / 网页是不可信数据，不影响系统指令与授权 | ⬜ | M10 prompt-injection 隔离 + trust label；M8 OCR untrusted | M8/M10 |
| 6.5 | 遥测默认 opt-in，不上传素材/转录/prompt/路径/密钥 | ⬜ | M12 telemetry；§12.3 发布策略 | M12 |

**红线来源**：§1.2 不变量 2「模型与外部内容不可信」、§10.2 滥用保护、§15.5。

---

## 7. 来源、成本、版权可追溯

| # | 要求 | 状态 | 验收方式 | 里程碑 |
| --- | --- | --- | --- | --- |
| 7.1 | 外部资料/生成素材/模型/提示版本有 provenance | ⬜ | M2 field-level provenance；M11 生成 provenance report | M2/M11 |
| 7.2 | 未知权利状态不能表述为"可商用" | ⬜ | M11 license/safety 字段阻断"商用就绪"声明 | M11 |
| 7.3 | 每个任务记录 task/skill/profile/model/tool + cost/time + artifact refs | 🟡 | `audit_store.py` 存在；待 M5/M9 完整 wiring | M5/M9 |

**红线来源**：§1.2 不变量 6「来源、成本、版权可追溯」。

---

## M0 退出条件核对（本批达成项）

来自 §11.2 M0「完成检查」：

- [x] **功能开关**：5 个 AI 能力 flag 定义、默认关闭、任何 product mode 关闭（§4.1–4.3）。
- [x] **测试命令**：`scripts/run_ai_edit_tests.sh` 聚焦运行 AI/edit 测试并输出通过数/耗时/失败。
- [x] **首发范围写入产品配置**：`config.json` `ai_assistant.release_scope` = 口播/访谈/产品短视频（§4.4）。
- [x] **隐私验收清单**：本文档。
- [ ] **性能基线**：`tests/benchmark/` 已有播放/渲染基线；AI/edit focused 性能基线待 M3/M5（推理/分析任务接入后才有意义）。

> **新人自检**（§11.2 M0 完成检查）：在不读全部源码的情况下，应能运行
> `scripts/run_ai_edit_tests.sh`、解释首发闭环（口播/访谈/产品短视频）、并说出
> 禁止能力（无公网 MCP、无全自动发布、无生成式媒体默认混入、无任意 shell/Python）。
