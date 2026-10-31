# EF-04 子运行 1/2 修复报告 — src/features/edit/ai_editing（19 条 Suggestion）

- 日期：2026-10-01
- 分支：dev（未做任何 git add/commit/branch/push）
- 白名单目录：`src/features/edit/ai_editing/`（未触碰白名单外任何文件；工作树中
  `src/features/edit/core`、`src/features/edit/ui` 等改动属于其他批次代理）
- 结论：**19/19 FIXED**，0 INVALID / 0 DUPLICATE / 0 BLOCKED

## 逐条结论

| ID | 结论 | 文件:位置 | 说明 |
|----|------|-----------|------|
| AUD-12704 | FIXED | agent_metrics.py:91-153, 217-232 | 新增 `prune(keep_last_n)` + `max_terminal_trackers`（默认 512）；新建 tracker 与进入终态时自动淘汰最旧终态 tracker，进行中 run 永不淘汰 |
| AUD-12907 | FIXED | analysis_service.py:996-1027 | `search_assets` 整轮只构建一个共享探针 `SemanticIndex`（search_evidence 逐段独立打分，结果等价），消除每段 O(n) 重对象构建 |
| AUD-13100 | FIXED | audit_store.py:100-181 | 仿同包 AutonomousMissionStore/CheckpointStore 加 `threading.RLock` 覆盖 record/list_entries/get/clear/export_all/import_all；export_all 锁内快照锁外序列化，落盘 IO 留在锁外 |
| AUD-13104 | FIXED | autonomous_agent.py:388-405, 415-457 | 新增 `_revoke_safely()`：紧急停用两处分支（live_checker 抛异常 / 返回 False）的 revoke 均受保护，失败降级为 `logger.warning`，apply() 的"总返回结构化结果"契约不被破坏，grant 尽力撤销 |
| AUD-13112 | FIXED | capability_catalog.py:180-199 | `remove()` 强制：core 条目（owner 为空）永不删除；调用方必须传非空且匹配的 `owner_plugin_id`（tool_facade.py:515 的唯一调用方不受影响） |
| AUD-13221 | FIXED | composition_plugin.py:219-241, 277-290 | `SubagentPluginHost` 增加 `max_child_agents`（默认 256），`run()` 超限按插入序淘汰最旧 child_run_id 映射（对照 ContextSpillStore 模式） |
| AUD-13405 | FIXED | edit_capability_plugin.py:45-60 | `display_name` 与 plugin_id 一致做 `str(...).strip()` 归一化并 `object.__setattr__` 回写，非字符串值不再在 `to_dict` 的 `[:200]` 处崩溃 |
| AUD-13504 | FIXED | generated_media_materializer.py:126-135, 152-166, 209-216, 267-352 | 下载成功后写 `<size, sha256, mime>` JSON sidecar；缓存复用按 size+记录直接返回结果（跳过 O(文件大小) 重哈希）；sidecar 缺失/损坏/size 不符/expected 不一致时回退全文重算，保持 fail-closed 校验语义 |
| AUD-13505 | FIXED | generation_bin.py:101-131（全文方法加锁） | 加 `threading.RLock` 覆盖全部读写（含 import_to_media_pool 整个临界区），类 docstring 声明线程安全（对照 FeedbackStore 模式） |
| AUD-13508 | FIXED | evidence_kinds.py:183-202 | `fingerprint()` payload 纳入 `"selection": dict(self.selection)`，与 docstring 声明一致；选择状态变化现在会被 stale 检测捕获（tests/ai/test_agent_w0_kernel.py 既有断言不受影响，已验证通过） |
| AUD-13607 | FIXED | generation_provider_pack.py:331-401 | SeedanceVideoProviderPack 加 `threading.RLock`：create/poll/cancel/resume 的 `_requests`/`_jobs` 读写持锁，transport 网络 IO 留在锁外 |
| AUD-13608 | FIXED | generation_provider.py:97-171 | ProviderRegistry 加 `threading.RLock` 保护 register/get/require/preferred_for/set_preferred/list_providers/count；preferred_for 退化遍历改锁内快照 |
| AUD-13612 | FIXED | help_index.py:191-210, 261-271, 379-411 | `build()` 只调一次 `list_features()` 预构建 `{feature_id: enabled}` 映射传给 `_add_shortcuts`/`_add_panels`；未列入映射的 id 保持"假定可用"旧语义 |
| AUD-13707 | FIXED | llm_cost.py:35-99 | 新增 `try_reserve(record, limit) -> bool` 单锁内原子完成检查+记账（硬预算）；类 docstring 声明 can_spend+record 组合为 best-effort |
| AUD-13830 | FIXED | mcp_plugin.py:112-152, 158-172, 218-235 | McpPluginHost 加 `threading.Lock` 覆盖 register/server/descriptor/snapshot（快照锁内取、锁外序列化）；新增 `remove(plugin_id)` 清理路径，registry 停用失败记 warning |
| AUD-13851 | FIXED | mcp_vision_gateway.py:25-31, 260-270, 445-451, 470-476, 489-503 | `_session_images` 改 `deque(maxlen=8)`（append+淘汰原子），读取取局部快照；`_server_disabled` 读写经新增 `_state_lock`（与慢速 `_lock` 分离），server_statuses 用快照 |
| AUD-13905 | FIXED | media_generation_service.py:607-629 | cancel() docstring 改为与 `JobRegistry.cancel` 实际行为一致：True=job 存在且取消请求已下发，不代表取消一定生效；判断生效需读 job 状态 |
| AUD-14014 | FIXED | ocr_provider.py:33, 123-125, 297-337 | `_engine` 惰性初始化改双检锁（`_engine_lock`），构建逻辑抽到 `_build_engine()`（持锁调用），并发 analyze 只构建一次 RapidOCR |
| AUD-14105 | FIXED | plan_repository.py:135-197, 213-216, 222-235, 268-285 | 新增 `_by_hash` 反向索引（store/import_all/remove/clear 同步维护，覆盖同 plan_id 换内容的场景），`find_by_hash` O(1)；可选 `max_plans` LRU 淘汰（默认 None 不改变行为，文档声明无淘汰策略） |

## 测试

### 新建测试
`tests/edit/ai_editing/test_ef04_aud_part1.py`（61 个用例，每条 ID 正例 + 反例；
不调 GPU/Qt 模态/真实 AI 服务，线程测试全部有界循环 + 注入 fake）：

```
$ .venv/bin/python -m pytest tests/edit/ai_editing/test_ef04_aud_part1.py -q -p no:cacheprovider
61 passed in 1.58s
```

### 回退敏感性验证（随机抽 2 条）
- AUD-13112：临时还原 `remove()` 旧实现 →
  `test_core_entry_not_removable` / `test_extension_entry_removed_only_by_owner` **FAILED** →
  还原修复后 6/6 PASSED。
- AUD-13508：临时从 fingerprint payload 移除 selection →
  `test_selection_change_changes_fingerprint` **FAILED** → 还原修复后 PASSED。

### 验证关卡
1. `py_compile` 全部 19 个源文件 + 测试文件 → **OK (20 files)**
2. 新测试全绿 → **61 passed**
3. 定向既有测试（分 5 批，未整目录跑）：
   - test_agent_metrics / test_analysis / test_autonomous_agent / test_composition_plugin → **76 passed**
   - test_generated_media_materializer / test_generation_bin / test_generation_provider / test_generation_provider_pack → **59 passed**
   - test_mcp_plugin_host / test_mcp_vision_gateway / test_plan_repository / test_help_index / test_edit_capability_plugin / test_ocr_provider → **83 passed, 1 skipped（预存 skip）**
   - test_media_generation_service / test_capability_catalog_runtime / tests/ai/test_agent_w0_kernel → **55 passed**
   - test_tool_facade / test_assistant_plan_lifecycle / test_autonomous_recovery / test_ai_persistence_wiring / test_fix_wd10blk_aud / test_commercial_p0_p1 / test_safe_path_policy → **164 passed**
   - **无新增失败**；已知预存失败类别（GPU 位精确 / x265 10-bit / VT 探针）未出现
4. 分支保持 `dev`；未执行任何 git 写操作

## 改动文件清单

源文件（19，全部在白名单 `src/features/edit/ai_editing/` 内）：
- agent_metrics.py / analysis_service.py / audit_store.py / autonomous_agent.py
- capability_catalog.py / composition_plugin.py / edit_capability_plugin.py
- evidence_kinds.py / generated_media_materializer.py / generation_bin.py
- generation_provider.py / generation_provider_pack.py / help_index.py / llm_cost.py
- mcp_plugin.py / mcp_vision_gateway.py / media_generation_service.py
- ocr_provider.py / plan_repository.py

测试文件（新建 1）：
- tests/edit/ai_editing/test_ef04_aud_part1.py

备注：AUD-13504 语义说明——复用路径信任 sidecar（size+sha256 记录），同 size 的
本地文件篡改不再逐字节重哈希校验（这正是审计建议的消除项）；expected_sha256 与
记录不一致、size 不符、sidecar 缺失/损坏时仍回退全文重算 fail-closed，并保留
checksum 失败删缓存的既有行为。
