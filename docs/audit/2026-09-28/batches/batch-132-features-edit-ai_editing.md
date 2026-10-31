# batch-132 审查报告 — src/features/edit/ai_editing/

批次：batch-132 [P1]　编号区间：AUD-13200–AUD-13299　文件数：8（2,712 行）
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。

本批次为纯 Python 服务层（LLM 驱动、上下文预算、商用验收门禁、插件宿主），不涉及 OpenGL/GLSL/HDR 与 Qt 信号，相关维度按"不适用/无问题"记录。跨模块结论均已 Read 验证：`agent_inbox`（RLock 保护，线程安全）、`plugin_runtime.PluginRegistry`（RLock + 重复 ID 拒绝）、`agent_tool_loop.run`（`local_messages = list(messages)` 不改调用方列表）、`request_recovery`（cancel_check 抛 `LLM_CANCELLED`）、`token_estimator`/`image_attachment`（None/dict/list 均有兜底）、`CompressionCheckpoint.to_dict`（持久化时 sanitize）、`postcondition_checker.create_default_checker`（CHAT 注册 `reply_non_empty`）。

---

## src/features/edit/ai_editing/chat_run_driver.py (行数: 739)

### 发现

- [AUD-13201] [Major] 工具循环失败被无日志的宽泛 `except Exception` 静默降级，掩盖领域错误
  - 位置: src/features/edit/ai_editing/chat_run_driver.py:317-318
  - 证据:
    ```python
                    return ChatRunResult(
                        run_id=run_id,
                        error=str(exc),
                        error_code=exc.code,
                        terminal_state=AgentRunState.FAILED,
                    )
            except Exception:  # noqa: BLE001 - tool loop failure degrades to plain
                use_tool_loop = False
    ```
  - 影响: `_run_with_tool_loop` 内含 middleware、ledger、pruner、多次模型调用等整条子系统；任何非 `LLMError` 异常（中间件 TypeError、账本 bug、空 `tool_defs` 的 `tool.name` 访问等）都被吞掉并降级为普通 completion，无日志、无 run 事件、无 trace 记录，违反 .clinerules「不得宽泛 except 掩盖领域错误」。此前已消耗的工具循环 token 也全部脱离审计，且降级后 RUN_REQUEST_HEADER 仍标记 `request_mode="tool_loop"`，与实际请求不符。
  - 建议: 仅对明确的可降级错误类型（如 HTTP 400/无效响应）降级；其余异常记录 logger.exception 并 `orchestrator.emit` 一个降级事件（含异常摘要）后再降级，或直接按失败上报。
  - 置信度: 高

- [AUD-13202] [Major] inbox 追问循环不断追加消息但不重估窗口预算，多轮请求用量也未累计
  - 位置: src/features/edit/ai_editing/chat_run_driver.py:328-357（循环）、262（估算点）、395-399（用量落账）
  - 证据:
    ```python
                    if followup_turns >= 4 or not self._inbox.has_pending(run_id):
                        break
                    messages.append({"role": "assistant", "content": reply or ""})
                    if not self._claim_inbox_pre_step(run_id, messages):
                        break
                    followup_turns += 1
    ```
  - 影响: `estimate_window_usage` 只在第 262 行执行一次；后续最多 4 轮追问持续向 `messages` 追加助手回复与新 pre-step 消息，既不重估 `window_prompt`，也不调用 `would_exceed_limit`/`should_auto_compress`，可把窗口推过 profile 上限而无任何压缩或告警。用量侧 `final_usage` 在 provider 无 usage 时回落到首次发送的估算值（未含追问增量），有 usage 时 `client.last_usage` 只反映最后一次请求——本模块自带的 `accumulate_session_total` 从未被调用，run 的 `record_usage` 系统性低报多轮请求的真实消耗。
  - 建议: 每轮追问后重跑 `estimate_window_usage` 并在超限时终止循环；用 `accumulate_session_total` 逐轮累计 session total 后再 `record_usage`。
  - 置信度: 高

- [AUD-13203] [Suggestion] `max_tool_calls` 被当作 `max_steps` 传入执行器，预算语义混用且默认值不一致
  - 位置: src/features/edit/ai_editing/chat_run_driver.py:545（对照 626-627）
  - 证据:
    ```python
                capability_catalog=self._capability_catalog,
                tool_allowlist=preset.tool_allowlist if preset is not None else (),
                max_steps=preset.max_tool_calls if preset is not None else 10,
                system_instruction=preset.system_instruction if preset is not None else "",
    ```
  - 影响: Agent 模式把「工具调用次数预算」灌进「规划步数预算」，且 `AgentTaskExecutor.__init__` 会 `min(_MAX_STEPS, int(max_steps))` 截断（上限 8），preset 的 `max_tool_calls=16` 实际失效；无 preset 时默认 10（截为 8），而 `_run_with_tool_loop` 同场景默认 16——两条 agent 路径预算口径不一致，用户可感知的行为随路径漂移。
  - 建议: 为 executor 单独取 `preset.max_iterations` 或新增显式 `max_steps` 字段；统一两条路径的无 preset 默认值。
  - 置信度: 高

- [AUD-13204] [Suggestion] agent 模式在无 ToolFacade 时静默退化为普通聊天，无任何标记
  - 位置: src/features/edit/ai_editing/chat_run_driver.py:220-224
  - 证据:
    ```python
        # ----- agent mode: delegate to AgentTaskExecutor -----
        if mode == "agent" and self._tool_facade is not None:
            return self._run_agent_task(
                run_id=run_id,
                resolution=resolution,
    ```
  - 影响: 路由已判定 agent 模式可用，但 facade 缺失时请求落入普通 chat 路径：无工具、无多步规划，run 仍可能以 SUCCEEDED 结束，用户以为执行了自主任务，实际只是一次问答；降级既不写 run 事件也不改 reason。
  - 建议: 降级时 `orchestrator.emit` 一个降级事件（或在 ChatRunResult 上加 `degraded` 标记），必要时直接 fail closed。
  - 置信度: 中

### 通过项
- 线程安全：驱动自身无可变共享状态，依赖的 AgentInbox/PluginRegistry 均为 RLock 保护（已验证）。
- 取消路径：模型调用前（recovery cancel_check）、调用后（386 行）、agent 委托前均有 `is_run_active` 复查；取消后不落 success/usage。
- 失败闭合：blocking resolution、demo_only 媒体模式、未知 preset、client 构造失败均 fail closed 且带 reason。
- 状态提交一致性：终态转换经 orchestrator.fail/finish_verification，空回复由 `reply_non_empty` 后置条件判失败（已验证 checker 注册）。
- 输入验证：messages 复制后使用；profile limit 经 int()/异常兜底。
- 类型注解：完整（含返回类型与 Callable 别名）。

---

## src/features/edit/ai_editing/commercial_acceptance.py (行数: 359)

### 发现

（无发现）

### 通过项
- 输入验证：`CommercialAcceptanceSpec.__post_init__` 校验必填字段与 stages 非空；probe 返回类型与 spec 一致性在 `run_commercial_acceptance` 中校验，异常一律转为失败 observation（fail closed，不伪造通过）。
- 隐私：`to_dict` 对 provider_id/evidence refs/error 文本统一 `redact_text` + 截断；`report_id` 由内容哈希派生，可复现。
- 状态一致性：全部为 frozen dataclass，无隐藏可变状态；`passed` 判定链（真实凭据证据 + panel-only + 商用/运营检查 + rollout 安全）与文档一致。
- 异常路径：probe 异常被捕获为数据而非崩溃，符合模块声明的集成边界语义。
- 性能：纯内存聚合，无热点。
- 类型注解：完整。

---

## src/features/edit/ai_editing/commercial_gate.py (行数: 77)

### 发现

- [AUD-13210] [Major] `entries` 为显式 null 时导出门禁抛 TypeError 而非 fail closed
  - 位置: src/features/edit/ai_editing/commercial_gate.py:62-66
  - 证据:
    ```python
        for entry in report.get("entries", ()):
            if not isinstance(entry, dict):
                continue
            if not entry.get("commercial_ready", False):
                blocking.append(f"生成素材 {entry.get('asset_id', '')!r} 未通过许可/安全商用门禁")
    ```
  - 影响: `report.get("entries", ())` 在键存在且值为 `None`（如持久化 JSON 中的显式 null）时返回 None，`for entry in None` 抛 TypeError 且未被捕获——商用导出路径直接崩溃。本函数的设计承诺是「任何证据缺失都 fail closed 返回 blocking 理由」，崩溃使调用方拿不到门禁结论；provenance report 属外部/持久化数据，违反「外部数据须验证」。
  - 建议: `entries = report.get("entries") or ()`，并加 `isinstance(entries, (list, tuple))` 校验，不合法时返回 `CommercialExportCheck(False, ("provenance entries 格式无效",))`。
  - 置信度: 高（崩溃机制为确定性 Python 语义；触发依赖报告中出现 `entries: null`）

### 通过项
- 失败闭合：`generation_bin` 缺失、report_fn 不可调用、report_fn 抛异常、report 非 dict 均返回 blocking，不放过商用导出。
- 输入验证：entry/provenance 的嵌套 dict 均有 isinstance 防护。
- 类型注解：完整；无状态纯函数，线程安全。
- 无 GPU/信号/撤销维度涉及。

---

## src/features/edit/ai_editing/composition_plugin.py (行数: 290)

### 发现

- [AUD-13220] [Suggestion] 三个宿主的 register() 激活失败路径遗留孤儿插件记录，同 ID 无法再注册
  - 位置: src/features/edit/ai_editing/composition_plugin.py:62-69（同型逻辑另见 150-163、248-259）
  - 证据:
    ```python
        accepted = self.plugin_registry.register(manifest, enabled=enabled)
        if not accepted:
            return False
        self._presets[preset.preset_id] = preset
        if enabled and not self.plugin_registry.activate(plugin_id):
            self._presets.pop(preset.preset_id, None)
            return False
        return True
    ```
  - 影响: 已验证 `PluginRegistry.register` 对重复 plugin_id 且 `replace_existing=False` 一律返回 False。激活失败后宿主只清理本地 dict，不 `unregister`，注册表残留 ENABLED 孤儿记录；此后对同一 preset/skill/subagent 的任何 `register()` 调用永远静默返回 False（`register_existing` 亦受影响），宿主与注册表状态永久漂移。SkillPluginHost 还会留下已被 disable 的 skill 记录。
  - 建议: 激活失败回滚时调用 `plugin_registry.unregister(plugin_id)`（非 ACTIVE 状态可注销，已验证语义支持）；或后续 register 对已存在同 ID 记录改走 activate 恢复路径。
  - 置信度: 高（registry 语义已核实；是否构成实际故障取决于宿主是否重试注册）

- [AUD-13221] [Suggestion] `SubagentPluginHost._child_agents` 只增不减，长会话内存无界增长
  - 位置: src/features/edit/ai_editing/composition_plugin.py:275-277
  - 证据:
    ```python
            raise SubagentError(f"subagent plugin is not active: {agent_id}")
        result = self.supervisor.run(**kwargs)
        self._child_agents[result.child_run_id] = agent_id
        return result
    ```
  - 影响: 每次 `run()` 追加一条 child_run_id→agent_id 映射，无任何淘汰/上限机制（对照同目录 `ContextSpillStore` 的 max_items+trim 设计）；编辑器长会话高频派生子代理时字典无界增长，且已结束的 child 永久占据 `continue_child` 的查找表。
  - 建议: 仿照 ContextSpillStore 加上限 + 按时间淘汰，或在 supervisor 终态回调中清理。
  - 置信度: 高

### 通过项
- 线程安全：底层 PluginRegistry/AgentInbox 均 RLock 保护（已验证）；宿主方法为短临界区组合操作。
- 生命周期门禁：未激活插件 `get()/tools_for()/validate_tool_call()/run()` 一律拒绝；`restore_project_state` 恢复后强制 disable，不隐式重新激活，防权限意外回滚。
- 输入验证：skill_id/agent_id 经 str() 规范化；SubagentPluginHost.run 前检查 active。
- 无 GPU/GLSL/HDR/Qt 信号维度涉及。
- 类型注解：完整。

---

## src/features/edit/ai_editing/context_budget.py (行数: 284)

### 发现

- [AUD-13230] [Suggestion] 压缩阈值与超限判定的口径不一致：比率不含 reserved_output，存在「已超限但既不告警也不压缩」区间
  - 位置: src/features/edit/ai_editing/context_budget.py:194-205、208-222
  - 证据:
    ```python
        if usage.window_limit is None or usage.window_prompt is None:
            return False
        reserved = usage.reserved_output or 0
        return usage.window_prompt + reserved + extra_tokens > usage.window_limit
    ```
  - 影响: 已验证 `ContextUsage.window_ratio = window_prompt / window_limit`（agent_models.py:301-309），不含 reserved_output；而 `would_exceed_limit` 按 prompt+reserved 比较。默认 reserved 为 limit 的 25% 时，prompt 比率在约 75%–80% 区间已触发 would_exceed_limit（预留被吃穿），但 `should_warn_compression`（≥0.80）与 `should_auto_compress`（≥0.90）均为 False——窗口实际超预算却无告警、无自动压缩，两套判定在同一模块内给出矛盾答案。
  - 建议: 统一口径——`window_ratio` 改用 `window_total/limit`，或阈值改为对「扣除预留后的可用额度」取比率，并在文档中写明。
  - 置信度: 中（代码事实确定；属设计口径问题而非崩溃缺陷）

### 通过项
- 「不伪造真实用量」红线：`usage_from_llm` 无真实 prompt 时保留估算与 estimated 标记，actual_* 保持 None；`accumulate_session_total` 对缺失 usage 不清零。
- 输入验证：`_sum_message_tokens` 对 None/str/list/dict content 均安全（extract_text_from_content/estimate_image_tokens 已验证有兜底）；`_resolve_reserved`/`_profile_context_limit` 对非法 limit 返回 None。
- 纯函数无共享状态，线程安全；无 GPU/信号/持久化维度涉及。
- 类型注解：完整。

---

## src/features/edit/ai_editing/context_bundle.py (行数: 299)

### 发现

（无发现）

### 通过项
- 不可变性：ContextBlock/TokenBudget/ContextBundle 均 frozen，`with_block/exclude_block/with_budget` 返回新对象，无隐藏别名共享（blocks 为 tuple）。
- 预算算法：`with_budget` 按 priority 降序装箱、HARD_CONSTRAINT 永不丢弃、恢复原始顺序、omitted ids 记录正确；同优先级排序确定性（index / kind）。
- prompt 装配：`to_messages` 对非 str content 的 JSON 序列化有 TypeError/ValueError 回退；空文本块仅保留标记。
- 输入验证：`input_limit` 对 reserved>limit 用 max(0,...) 兜底。
- 纯数据结构，线程安全；无 GPU/信号/异常路径维度涉及。
- 类型注解：完整。

---

## src/features/edit/ai_editing/context_service.py (行数: 500)

### 发现

- [AUD-13240] [Major] 工具 manifest 块假设每个条目都是 dict，畸形第三方工具定义会使整个上下文构建崩溃
  - 位置: src/features/edit/ai_editing/context_service.py:330-338
  - 证据:
    ```python
        compact = [
            {"name": t.get("name"), "permission": t.get("permission"), "desc": t.get("description")}
            for t in tools
        ]
        content = {"tools": compact, "capability": sources.get_capability_manifest() or {}}
    ```
  - 影响: `t.get(...)` 对非 dict 条目（字符串、None）抛 AttributeError，直接炸掉 `build_bundle` → 本次 LLM 请求整体失败。tool manifest 来自宿主序列化的插件工具定义（本架构存在 VERIFIED_THIRD_PARTY/LOCAL_DEV 插件源），属外部数据边界，违反「外部数据须验证」；一个坏插件会拖垮所有后续 assistant 请求而非被跳过。
  - 建议: 列表推导内先 `isinstance(t, dict)` 过滤（或跳过并记录），`tools` 本身也校验为 list/tuple。
  - 置信度: 中（崩溃路径确定；实际触发依赖宿主 manifest 是否可能含非 dict 条目）

- [AUD-13241] [Suggestion] 压缩摘要块 token 估算 `len//4` 与项目估算器的 CJK 系数矛盾，系统性低估
  - 位置: src/features/edit/ai_editing/context_service.py:289-295
  - 证据:
    ```python
        summary_block = ContextBlock(
            kind="recent_summary",
            content=summary_text,
            priority=ContextBlockPriority.RECENT_SUMMARY,
            # 摘要 token 估算：每 4 字符约 1 token（粗略）
            token_estimate=max(1, len(summary_text) // 4),
    ```
  - 影响: 本项目 `token_estimator` 明确按 CJK ~1.5 字符/token 加权；摘要由中文历史内容拼接而成，`//4` 低估约 2.7 倍。压缩后 bundle 的 `total_tokens`/`needs_compression`/`with_budget` 决策全部基于被低估的值，可能导致压缩后仍超预算却判定安全。
  - 建议: 改用 `estimate_tokens(summary_text)`（本文件已导入该模块的依赖链）。
  - 置信度: 高

- [AUD-13242] [Suggestion] 注释声称 summary 已脱敏，实际 compress() 存入的是原始拼接文本
  - 位置: src/features/edit/ai_editing/context_service.py:303-307
  - 证据:
    ```python
        # CompressionCheckpoint（summary 经 sanitize_for_storage 脱敏）
        cp = CompressionCheckpoint(
            checkpoint_id=_uuid.uuid4().hex,
            created_at=_time.time(),
            summary=summary_text,
    ```
  - 影响: 已验证脱敏只发生在 `CompressionCheckpoint.to_dict()`（持久化边界）；compress() 构造的内存对象 `cp.summary` 是历史/摘要块的原始前 80 字符拼接。注释与实现不符，容易让后续消费方（日志、UI 直接读 cp.summary、绕过 to_dict 的存储）误以为内存值已安全。
  - 建议: 或在 compress() 内对 summary_text 先 `sanitize_for_storage`，或改注释明确「脱敏发生在 to_dict 持久化边界，内存对象未脱敏」。
  - 置信度: 高

- [AUD-13243] [Suggestion] 模块中部/函数内重复导入，`"history"` 字面量未用已导入常量
  - 位置: src/features/edit/ai_editing/context_service.py:239-244（另见 115、452）
  - 证据:
    ```python
        import time as _time
        import uuid as _uuid
        from dataclasses import replace as _replace
        from src.features.edit.ai_editing.context_bundle import (
            ContextBlockPriority, KIND_HISTORY,
        )
    ```
  - 影响: `ContextBlockPriority`/`replace` 顶部已可用（同模块 import），`KIND_HISTORY` 在 compress() 内局部重导入而 `_build_history_block`(452) 却用字面量 `"history"`、`expiry="session"` 也用字面量——同一 kind 字符串两处写法，漂移风险；115 行的 mid-file `from dataclasses import dataclass` 同类。
  - 建议: 统一提到模块顶部导入并使用 KIND_HISTORY/EXPIRY_SESSION 常量。
  - 置信度: 高

### 通过项
- 线程安全：ContextService 无实例状态，声明并符合「无状态、线程安全」。
- 红线遵守：压缩只合并 HISTORY/RECENT_SUMMARY，EVIDENCE/APPROVED_DECISIONS/DELIVERY_TARGET 原样保留，evidence/artifact refs 进 checkpoint（可追溯）；build_bundle 的 with_budget 不丢 HARD_CONSTRAINT。
- 证据截断：transcript/text 截 240 字符，防长转录塞爆（§7.2 可逆索引）。
- 无 GPU/GLSL/HDR/Qt 信号维度涉及；本服务不持久化 bundle（已核实持久化在 session 层）。
- 类型注解：完整（Protocol + dataclass）。

---

## src/features/edit/ai_editing/context_spill.py (行数: 164)

### 发现

- [AUD-13250] [Suggestion] `from_dict` 截断 spill_id 至 64 字符，与 `get()` 的全 ID 匹配不一致
  - 位置: src/features/edit/ai_editing/context_spill.py:63-67（对照 115-117）
  - 证据:
    ```python
        return cls(
            spill_id=spill_id[:64],
            scope=str(data.get("scope", ""))[:128],
            kind=str(data.get("kind", "context"))[:64],
            content_hash=content_hash[:128],
    ```
  - 影响: 自产 spill_id 为 24 位 hex 不受影响；但导入的外部记录若 spill_id 超过 64 字符会被截断存储，之后 `get(原始ID)` 与 `_trim_locked` 重建的 key（scope/kind/content_hash 均取截断值）都可能对不上，审计引用悬空。另外 `from_dict` 的 preview 导入时未再 sanitize（导出时 to_dict 会补），内存对象与自产记录的脱敏时序不一致。
  - 建议: 导入时拒绝超长 spill_id（或按完整 ID 建索引）；import 分支对 preview 复用 `sanitize_for_storage`。
  - 置信度: 低-中（机制确定，触发依赖异常外部数据）

### 通过项
- 线程安全：RLock 全路径覆盖 record/get/records/import_all/clear/_trim_locked；哈希计算在锁外（纯函数）。
- 输入验证：`from_dict` 对非 dict/缺 ID/缺 hash 抛 ValueError，import_all 捕获并跳过；`_max_items` 下限 1；`_canonical` 递归截断（列表 128、字符串 4000）防超大 payload。
- 有界性：max_items + 按 created_at 淘汰，无内存无界增长。
- 隐私：record/to_dict 均过 sanitize_for_storage 且 preview 截 400；仅存哈希/计数/短预览。
- 类型注解：完整。

---

## 汇总

| 严重度 | 数量 | 编号 |
|---|---|---|
| Critical | 0 | — |
| Major | 4 | AUD-13201, AUD-13202, AUD-13210, AUD-13240 |
| Suggestion | 9 | AUD-13203, AUD-13204, AUD-13220, AUD-13221, AUD-13230, AUD-13241, AUD-13242, AUD-13243, AUD-13250 |
