# EF-04 复查报告（GLM5.3）

- 批号：**EF-04**（src/features/edit/ai_editing，37 条 Suggestion 级）
- 提交哈希：`df751224d613fda69619ab8fcb21c2d4879cd5e0`（dev 分支，单提交覆盖两个子运行）
- 白名单：`src/features/edit/ai_editing/`
- 执行报告：`fixes/reports/ef-04-part1.md`（19 条）、`fixes/reports/ef-04-part2.md`（18 条）
- 复查日期：2026-10-01 ｜ 复查方式：只读（git show / 源码逐条核对 / py_compile），未运行 pytest（测试由中心会话统一执行）

---

## §6 复查清单逐项判定

| # | 检查项 | 判定 | 说明 |
|---|--------|------|------|
| 1 | 越界 | **PASS** | `git show --stat df751224d` 共 38 文件：33 个源文件全部在 `src/features/edit/ai_editing/`（含白名单内的 `tests/test_assistant_service_cancel.py`，系 AUD-14600/14601/14603 的修复对象本身）+ 2 个新测试（`tests/edit/ai_editing/test_ef04_aud_part{1,2}.py`）+ 2 个报告。⊆ 白名单 ∪ tests/ ∪ fixes/reports/；REMEDIATION_PLAN.md 未触碰 |
| 2 | 真实修复（逐条） | **PASS** | 37/37 条在当前 HEAD 独立核实为真实修复（见逐 ID 表，全部给出 file:line 证据）。无表面打勾、无测试造假。见下表 |
| 3 | GLSL/HDR 红线 | **PASS（不适用居多）** | 本批为纯 Python AI 编辑域，无 GLSL 改动；无 HDR 数值钳制；无 601/709 矩阵触碰 |
| 4 | 并发 | **PASS** | 新增锁全部为 `threading.Lock/RLock`；锁内无重活经抽查核实：audit_store `export_all` 锁内快照、锁外 `sanitize_for_storage` 序列化（audit_store.py:167-172）；mcp_plugin snapshot 锁内取、锁外序列化；provider_pack 的 transport 网络 IO 明确留在锁外（generation_provider_pack.py:355-357 注释+实现）；runtime_capability_snapshot 监听者回调在锁外执行防死锁（:176-186）。信号无递归：mcp_vision_gateway `server_statuses`/`_session_images` 均改快照/`deque(maxlen=8)` 原子淘汰。TTS 有界等待（AUD-14117）走单工作线程 + `future.result(timeout=120s)`，调用线程不再无限阻塞，无 UI 线程 GL/模态 |
| 5 | 资源 | **PASS** | 无 ModernGL/GPU 对象触碰；deque(maxlen) 有界容器三处（agent_metrics/telemetry/tool_call_log）+ tracker/child_agents 淘汰上限，消除无界增长 |
| 6 | 异常 | **PASS** | diff 新增行零裸 `except:`；4 处新 `except Exception` 全部带 `logger.warning` 留痕 + `noqa: BLE001` 理由注释（autonomous_agent `_revoke_safely`、mcp_plugin registry 停用、snapshot 监听者——后者把既有静默 `pass` 改为留痕，是改善）；toposctl 为具体 `except ValueError`+warning；materializer 新增异常处理为具体类型（OSError/ValueError）+ 设计内回退语义（sidecar 损坏→全文重算 fail-closed） |
| 7 | 测试质量 | **PASS** | 见下节"测试质量评估" |
| 8 | 越界文件 ⊆ 白名单+测试+报告、未碰 REMEDIATION_PLAN.md | **PASS** | 同第 1 项 |

---

## 逐 ID 表（37/37，全部独立核实于当前 HEAD）

| ID | 计划指控（摘要） | 报告结论 | 复查判定 | 当前 HEAD 证据（file:line） |
|----|----------------|---------|---------|---------------------------|
| AUD-12704 | `_trackers` 无淘汰无界增长 | FIXED | **CONFIRMED** | agent_metrics.py:97-103（`max_terminal_trackers` 默认上限+校验）、:129（`prune(keep_last_n)`）、:138 |
| AUD-12907 | search_assets 每段新建 SemanticIndex O(n) | FIXED | **CONFIRMED** | analysis_service.py:1012-1013（整轮共享单一 SemanticIndex，注释明示 AUD-12907 动机） |
| AUD-13100 | AuditStore 全程无锁 | FIXED | **CONFIRMED** | audit_store.py:105（RLock）、:109/139/143/161/167（record/list/get/clear/export/import 全持锁）；export 锁内快照锁外序列化 |
| AUD-13104 | 紧急停用分支 revoke 抛异常逃逸+grant 泄漏 | FIXED | **CONFIRMED** | autonomous_agent.py:258/266（两分支调用）、:388（`_revoke_safely`，异常降级 `logger.warning`） |
| AUD-13112 | remove() 空 owner 可删任何条目 | FIXED | **CONFIRMED** | capability_catalog.py:180-199（core 条目 `entry.owner_plugin_id` 为空即拒删；owner 必须非空且匹配） |
| AUD-13221 | `_child_agents` 只增不减 | FIXED | **CONFIRMED** | composition_plugin.py:232-240（`max_child_agents` 默认 256）、:287（超限按插入序淘汰） |
| AUD-13405 | display_name 非字符串过校验后在 to_dict 崩溃 | FIXED | **CONFIRMED** | edit_capability_plugin.py:45-60（`str(...).strip()` 归一化 + `object.__setattr__` 回写） |
| AUD-13504 | 缓存命中仍全文重算 sha256 | FIXED | **CONFIRMED** | generated_media_materializer.py:146-155（sidecar 命中直返）、:224-227（下载后写 sidecar）、:248/293（`_write_sidecar`/`_cached_result_from_sidecar`，size 不符/expected 不符/损坏→None→调用方回退全文重算，fail-closed 保持） |
| AUD-13505 | GenerationBin 无锁无线程契约 | FIXED | **CONFIRMED** | generation_bin.py:104（docstring 声明线程安全）、:124（RLock）、:130-148（全读写持锁） |
| AUD-13508 | fingerprint 未纳入 selection | FIXED | **CONFIRMED** | evidence_kinds.py:180（docstring）、:189-190（payload 纳入 `dict(self.selection)`，注释明示） |
| AUD-13607 | provider_pack `_jobs`/`_requests` 无锁 | FIXED | **CONFIRMED** | generation_provider_pack.py:357（RLock，注释明示 transport IO 留锁外）、:369-396（create/poll/cancel/resume 持锁） |
| AUD-13608 | ProviderRegistry 无锁 | FIXED | **CONFIRMED** | generation_provider.py:99（RLock）、:113-159（register/get/require/preferred/list/count 全持锁） |
| AUD-13612 | `_feature_enabled` 每条重复调 list_features() | FIXED | **CONFIRMED** | help_index.py:201-202（一次性预构建 feature_map）、:298/:377（传入映射）、:408（`_feature_enabled(feature_map, ...)`） |
| AUD-13707 | can_spend+record 两次加锁非原子 | FIXED | **CONFIRMED** | llm_cost.py:63（`try_reserve(record, limit) -> bool` 单锁内原子检查+记账） |
| AUD-13830 | McpPluginHost `_servers` 无锁无清理路径 | FIXED | **CONFIRMED** | mcp_plugin.py:119（Lock）、:160（新增 `remove(plugin_id)` 清理路径）、:156/167/191/235/241（register/server/descriptor/snapshot 持锁） |
| AUD-13851 | `_session_images`/`_server_disabled` 无锁 | FIXED | **CONFIRMED** | mcp_vision_gateway.py:264（`deque(maxlen=8)` 原子淘汰）、:269（`_state_lock` 与慢速锁分离）、:305/:336（读写持锁） |
| AUD-13905 | cancel() docstring 与实际行为不符 | FIXED | **CONFIRMED** | media_generation_service.py:607-620（docstring 如实写明"True=请求已下发，不代表生效"，判断生效需读 job 状态） |
| AUD-14014 | OCR `_engine` 惰性初始化无锁 | FIXED | **CONFIRMED** | ocr_provider.py:124（`_engine_lock`）、:288-305（双检锁 + `_build_engine` 抽取） |
| AUD-14105 | find_by_hash O(n) 且无淘汰 | FIXED | **CONFIRMED** | plan_repository.py:142（docstring：_by_hash 使 O(1)）、:152（反向索引）、:148-156（可选 `max_plans` LRU，默认 None 不改行为） |
| AUD-14117 | 逐 cue 同步 generate 无超时，长时间阻塞 | FIXED | **CONFIRMED** | profile_tts_provider.py:43（`REMOTE_CUE_TIMEOUT_SECONDS=120.0`）、:145（每 cue 走 `_generate_bounded`）、:159-174（单工作线程 + `future.result(timeout=...)`，超时抛带 request_id 的 RuntimeError） |
| AUD-14201 | AnalysisPluginHost 状态无锁 | FIXED | **CONFIRMED** | provider_plugin.py:139（`_bindings_lock`）、:164/201/207/222/228（register/activate/descriptor/list 持锁） |
| AUD-14206 | TelemetryCollector `pop(0)` O(n) | FIXED | **CONFIRMED** | release_evidence.py:86（`deque(maxlen=...)`）、:113（注释：deque 自动淘汰） |
| AUD-14207 | TelemetryCollector 无锁 | FIXED | **CONFIRMED** | release_evidence.py:87（Lock）、:91-125（opted_in/set_opt_in/emit/events/export/clear 全持锁） |
| AUD-14314 | CapabilitySnapshotStore 无锁、退订不幂等、监听者异常静默 | FIXED | **CONFIRMED** | runtime_capability_snapshot.py:148（Lock）、:176-186（回调锁外执行；异常 `logger.warning` 留痕；`unsubscribe` 幂等） |
| AUD-14318 | 函数体两个连续 docstring | FIXED | **CONFIRMED** | schema.py:109-133（合并为单一 docstring，`grep -c '"""'` = 1 对） |
| AUD-14504 | docstring 声称 ADD_SUBTITLE 未接编译器 | FIXED | **CONFIRMED** | subtitle_draft.py:4-7（更正为"已接入 plan_compiler.COMPILABLE_OPERATIONS"） |
| AUD-14600 | 已取消短路测试断言过弱 | FIXED | **CONFIRMED** | tests/test_assistant_service_cancel.py:98（显式断言 `degradation == "cancelled"`、状态翻 CANCELLED、留取消消息）；part2 测试 :515（AST/introspection 反例锚定弱断言不回潮） |
| AUD-14601 | emit 回调签名不匹配，emitted 恒空（死脚手架） | FIXED | **CONFIRMED** | tests/test_assistant_service_cancel.py:128（`_StageEmittingRunner` 真实执行回调）、:165（`assert emitted == [("progress", "", {"stage": "analyze"})]`） |
| AUD-14603 | WorkflowResult 函数内重复局部导入 | FIXED | **CONFIRMED** | tests/test_assistant_service_cancel.py:29（模块顶部统一导入；函数体内零局部 workflow 导入，AST 断言锚定） |
| AUD-14703 | from_audio_sample 浮点运算违背文档承诺 | FIXED | **CONFIRMED** | timebase.py:292-316（全程整数：`(num + den//2)//den` half-away-from-zero，负数分支对称处理；不再经 to_fps() 浮点中转） |
| AUD-14711 | ToolCallLog `pop(0)` O(n) | FIXED | **CONFIRMED** | tool_call_log.py:204（`deque(maxlen=max(1, max_entries))`） |
| AUD-14803 | artifact_refs 未过 sanitize_for_storage | FIXED | **CONFIRMED** | tool_result.py:130-133（`[sanitize_for_storage(str(ref)) for ref in self.artifact_refs]`） |
| AUD-14809 | --log-level 非法值崩溃 main() | FIXED | **CONFIRMED** | toposctl.py:528-543（捕获 ValueError→降级 WARNING+`logger.warning` 留痕，stdout 单 JSON 契约保持） |
| AUD-14817 | grant 在乐观锁检查前被 consume | FIXED | **CONFIRMED** | transaction_service.py:474-503（`get_sequence`+revision 预检在 `_authorize`（:506）之前，冲突直接返回 CommitResult，grant 不烧）、:529-530（残余 TOCTOU 窗口文案明示"grant 已消耗需重新批准"） |
| AUD-14904 | thread_local.request_options 不清理 | FIXED | **CONFIRMED** | transcript_provider.py:349-357/370-387/419+（三处 sentinel 保存+finally 恢复/删除，异常路径同样恢复） |
| AUD-14912 | `_remote_by_profile` 无锁可重复创建 | FIXED | **CONFIRMED** | transcript_router.py:254（`_remote_lock`）、:272-286（双检锁：无锁命中热路径，miss 进锁二次确认后构造） |
| AUD-14918 | `_engines` 无锁可重复加载 3.8GB | FIXED | **CONFIRMED** | tts_provider.py:66（`_engines_lock`）、:359-368（双检锁，分立 model_kind 缓存语义保持） |

统计：**37 CONFIRMED / 0 ISSUE / 0 INVALID / 0 BLOCKED**（与执行报告 37 FIXED 一致）。

---

## 红线发现

**零 Critical、零红线违规。** 具体核查记录：

1. 裸 `except:`：diff 新增行零命中（扫描唯一命中为报告 markdown 自述文字）。
2. 吞错 `except Exception: pass`：零新增。4 处新 `except Exception` 全部 `logger.warning` 留痕；runtime_capability_snapshot 把既有静默 pass **改善**为留痕。
3. UI 线程 GL/moderngl：本批无任何 GL 相关改动（grep 源文件新增行零命中）。
4. HDR/601/709：不涉及（AI 编辑域，无色彩矩阵与值域代码路径）。
5. 锁内重活：抽查 audit_store（序列化锁外）、mcp_plugin（序列化锁外）、generation_provider_pack（transport IO 锁外，注释+实现双确认）、runtime_capability_snapshot（回调锁外）——均合规。
6. 信号递归：mcp_vision_gateway 读端全部改快照/deque 原子操作，无递归风险。

---

## 测试质量评估

- 规模与断言密度：part1 61 用例 / part2 35 用例（grep 复核与报告数字一致），合计 96；总计 3260 行（含两文件）。不调 GPU/Qt 模态/真实 AI 服务（报告声明+抽读证实：fake provider/runner 注入、有界并发循环）。
- 正向断言体现修复语义（抽读样本）：
  - AUD-14117：真实验证超时语义（挂起 provider + 实例级调小超时 + `elapsed < 3.0` 上界断言）；`monkeypatch(raising=True)` 钉住 `_generate_bounded` 必须存在，回退即 AttributeError，不会滑回无限阻塞——设计正确。
  - AUD-14817：真实 TOCTOU 行为断言（真实 preview→签发 grant→对过期 state 提交→断言冲突返回**且 grant 仍可 consume**）——旧实现下该断言必失败。
  - AUD-39402 式守卫（本批对应 AUD-13112）：`remove()` 各分支布尔断言完整（core 拒删×2、非 owner 拒删、owner 删成）。
- 结构断言（AST/introspection）仅作反例锚定（AUD-14318/14603），主断言均为行为断言——合理。
- 执行报告自述的回退敏感性抽查（13112、13508、14703、14207）与测试断言设计一致可信。
- 可疑弱断言：**未发现**。AUD-13905（docstring 修正类）无法有行为断言，属 ID 性质决定，不算弱。

py_compile：33 个改动源文件 + 2 个测试文件在当前 HEAD 全部编译通过（复查者独立执行）。

---

## 回退验证建议（供中心会话执行；回退后对应测试必须 FAILED）

### 候选 1：AUD-13112（capability_catalog.py）

文件：`src/features/edit/ai_editing/capability_catalog.py`（当前 :180-199）
回退方式：将 `remove()` 的归属守卫删去，恢复旧语义——

当前（old，回退目标位置）：
```python
        entry = self._entries.get(str(name))
        if entry is None:
            return False
        owner = str(owner_plugin_id).strip()
        if not owner or not entry.owner_plugin_id:
            return False
        if entry.owner_plugin_id != owner:
            return False
        del self._entries[str(name)]
        return True
```
回退为（new）：
```python
        entry = self._entries.get(str(name))
        if entry is None:
            return False
        del self._entries[str(name)]
        return True
```
应失败的 pytest 节点：
- `tests/edit/ai_editing/test_ef04_aud_part1.py::TestAud13112RemoveCoreGuard::test_core_entry_not_removable`
- `tests/edit/ai_editing/test_ef04_aud_part1.py::TestAud13112RemoveCoreGuard::test_extension_entry_removed_only_by_owner`

### 候选 2：AUD-14817（transaction_service.py）

文件：`src/features/edit/ai_editing/transaction_service.py`（当前 :474-503 预检块 + :506 `_authorize`）
回退方式：删除 :474-503 的 `get_sequence` + revision 预检块（含 try/except ValueError 折叠与 RevisionConflict 提前返回），让 `grant = self._authorize(...)`（:506）直接跟在 preview 之后执行。
应失败的 pytest 节点：
- `tests/edit/ai_editing/test_ef04_aud_part2.py::test_aud_14817_revision_conflict_does_not_burn_grant`
（该测试断言"冲突返回且 grant 仍可 consume"——回退后 grant 先被 consume，`consume()` 必抛 GrantNotFound / 断言失败。）

---

## 最终结论

**PASS。**

- 37/37 条真实修复、独立核实全 CONFIRMED；零红线违规；零越界；测试断言有效。
- 备注（不阻塞）：AUD-13504 的语义变化（复用路径信任 sidecar 记录，同 size 本地篡改不再逐字节重哈希）已在报告正文披露且与审计建议方向一致，属设计内取舍；AUD-13905/14318/14504 为 doc-gap 类 ID，docstring 修复即修复语义本身。
- 建议：本批可 `check_fixes.py --mark` 勾选全部 37 条（commit=df751224d）。
