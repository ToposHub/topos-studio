# EF-15 复查报告（GLM5.3，只读复核）

- 批次：EF-15（src/shared/ai_models 12 条 + src/features/ai_harness 11 条，共 23 条）
- 提交哈希：`377e19318`（[fix-E ef-15] ai_models+ai_harness 23条）
- 执行报告：`fixes/reports/ef-15.md`
- 复查日期：2026-10-01 ｜ 复查基线：当前 HEAD `9984d5939`
- 白名单：`src/shared/ai_models/` ∪ `src/features/ai_harness/`（不含其 tests/，EF-16 范围）∪ `tests/` ∪ `fixes/reports/`

## §6 八项检查逐项判定

### 1. 越界检查 — PASS

`git show --stat 377e19318`：17 个文件：
- `fixes/reports/ef-15.md`（+87）✓
- `src/features/ai_harness/` 8 个（__init__ / mcp_outbound / plugin_rollout / runtime / runtime_invariants / scheduler_adapter / session_fork / skill_studio_drafts）——**未触碰 `src/features/ai_harness/tests/`**（与报告声明一致，EF-16 范围避让）✓
- `src/shared/ai_models/` 7 个（ai_plugin_engine / generation/credential_store / generation/model_pack_manager / generation/volcengine_generation_provider / inference_engine / model_manager / rife_interpolator）✓
- `tests/shared/test_ef15_aud.py`（新建，+1486）✓

零越界，未碰 REMEDIATION_PLAN.md。

### 2. 真实修复（逐条独立核实）— 22 FIXED + 1 INVALID，全部属实

**AUD-31525（INVALID 结论）验证**：
- 指控：error 路径不清理 worker 对象，每次失败泄漏一个 AIDenoiseWorker。
- 现状核实：denoise_worker.py:611-613 `worker.finished/error/cancelled` **三条路径均已连接 `worker.deleteLater`**，:582 注释显式引用 AUD-31525 语义；`_on_thread_finished` 清空 `_worker`/`_thread`。
- 历史核实：`git log -S "AUD-31525"` 显示该修复早由 `218bb19d4`（[fix-D w15]）引入；EF-15 父提交（377e19318~1）中三连接已存在；**EF-15 的 diff 未改动 denoise_worker.py（`git show --stat 377e19318 -- <file>` 为空）**。
- 护栏测试：test_ef15_aud.py:785-810 `TestAud31525DenoiseWorkerCleanup::test_error_path_cleans_worker_and_thread` 用真实 controller + 不存在的模型路径驱动 error 路径，断言引用清空 + `worker.objectName()` 抛 RuntimeError（C++ 对象已析构，deleteLater 已生效）——反例断言真实反映修复语义。
- **INVALID 判定成立**（审计证据针对重构前代码）。

其余 22 条逐条核实见下表。重点抽查：
- **AUD-31807**：`_load_onnx_session` 三重防护（inference_engine.py:528-604）——缓存命中（LRU 触-touch）→ per-path 锁内二次检查 → 插入时 LRU 淘汰（`_SESSION_CACHE_MAX=4`，OrderedDict）；`_path_lock` 表清理只移除不在缓存中的路径且持锁线程仍持引用（:641-650 注释说明，安全）；`_schedule_worker_release` 鸭子类型 `getattr(worker, "deleteLater", None)`（:1133-1145），对纯 Python stub 兼容（报告披露的 WD16G81 初轮失败及修复属实）。
- **AUD-22318**：`_check_core_indexed`（runtime_invariants.py:210-253）在**同一锁内先检查后索引**（TOOL_FINISHED 检查时自身尚未入索引，顺序正确）；RETRY_AFTER_TERMINAL 用终态集合；自定义检查仅在存在时取一次全量快照——O(n²) 消除且语义保持。
- **AUD-31617/31810**：原子写均 tempfile/mkstemp（或 .tmp）+ fsync + `os.replace` 同目录落位；失败清理临时文件并留痕。
- **AUD-31811**：`cancel_download`（model_manager.py:432-449）只投递协作取消，`wait(3000)` 已移除；运行中 worker 留在表内由 `_on_download_finished` pop + deleteLater（:549-551 核实）。

### 3. 红线扫描 — PASS（无 Critical）

`git show 377e19318` 全部新增行扫描：
- 裸 `except:`：0；`except Exception:`（新增行）：0。
- `pass` 新增 4 处：`except FileNotFoundError: pass`（model_pack_manager 临时文件清理，os.replace 成功后 tmp 必不存在——窄类型正确）、`except OSError: pass`（model_manager tmp 清理）、2 处测试 stub 类（FakeSession/close）——均非吞错路径。
- 该批是清一色 Python 服务层（无 GL/无渲染），UI 线程 GL、HDR 钳制、601/709 三条红线天然不适用且零命中。
- Qt 对象释放全部走 `deleteLater`（线程安全队列路径），无跨线程直接析构。

### 4. 测试质量 — PASS

`tests/shared/test_ef15_aud.py`（62 用例，与报告一致，py_compile 通过）：
- 23 条 ID 全覆盖（含 INVALID 条目的护栏用例）。
- 锁语义验证采用"持锁期间操作必须阻塞"模式（如 TestAud22312RuntimeEmitLock::test_emit_blocks_while_lock_held_elsewhere 断言 emitter 在他人持锁时 0.4s 内不可完成）——真正体现"锁保护"修复语义，回退（去锁）即失败。
- 并发用例（session seq 唯一性、bridge 绑定不丢）用多线程 + Event + 超时断言，无悬挂风险。
- AUD-31710 回退抽验（移除 prune 调用 → 测试失败）与 AUD-22321 回退抽验在执行报告中留有记录，断言反例真实。

### 5. 回退验证建议（抽查 2 条）

| 条目 | 文件:行（当前 HEAD） | 回退操作 | 应失败的 pytest 节点 |
|------|---------------------|---------|---------------------|
| AUD-22312 | `src/features/ai_harness/runtime.py:769-770`（`_emit` 内 `with self._lock:` + `state.events.append(event)`） | 将两行改为无锁直写 `state.events.append(event)`（去掉 with 块缩进） | `tests/shared/test_ef15_aud.py::TestAud22312RuntimeEmitLock::test_emit_blocks_while_lock_held_elsewhere` |
| AUD-31710 | `src/shared/ai_models/generation/volcengine_generation_provider.py:553-554`（`# AUD-31710：每次写入后做容量收敛...` 注释行 + `prune_generated_media_cache(root)`） | 删除调用行（:753 的 `_cache_audio` 内同名调用为第二处，可一并验证） | `tests/shared/test_ef15_aud.py::TestAud31710GeneratedMediaPrune::test_cache_writer_triggers_prune` |

### 6. 综合结论 — **PASS**

## 逐 ID 核实表（23/23）

### src/features/ai_harness（11 条）

| ID | 计划指控 | 报告结论 | HEAD 独立核实（file:line 证据） | 判定 |
|----|---------|---------|-------------------------------|------|
| AUD-21900 | __init__ docstring 停留 P0 与导出脱节 | FIXED | __init__.py:1-20 起 docstring 与 `__all__` 逐项对齐（invocation_models/runtime/scheduler/generation/legacy/run_trace/plugin 系列），声明新增 API 须同步 | CONFIRMED |
| AUD-21911 | worker 回调无锁 sync 依赖 GIL | FIXED | scheduler_adapter.py:129-139 JobRunBridge `threading.RLock` + 线程契约 docstring；:147-160 各方法持锁 | CONFIRMED |
| AUD-22022 | registry/auth log/transport 缓存/请求 id 无锁 | FIXED | mcp_outbound.py:133（auth log Lock）、:199（registry RLock）、:317（transport 缓存 RLock）、:323-324 `_next_request_id` 锁内递增 | CONFIRMED |
| AUD-22200 | PluginRolloutRouter 完全无锁 | FIXED | plugin_rollout.py:242 `threading.Lock`；register/remove/to_dict/target_for_new_run 全部 `with self._lock`（:248-274 等）；纯计算 admits_new_run 锁外（合理） | CONFIRMED |
| AUD-22312 | _emit/_emit_node 锁外 append | FIXED | runtime.py:766-770 / :794-797 append 入 `self._lock`（RLock 可重入），_dispatch 保持锁外（慢订阅者不阻塞 runtime） | CONFIRMED |
| AUD-22313 | retry() 空锁块死代码 | FIXED | runtime.py:440-443 死块已删并留注释（事件由 start/_emit_by_id 各自锁内发布）；grep 源码无 new_state+pass 模式（测试锁定） | CONFIRMED |
| AUD-22317 | _seen/_violations 无锁可重复记录 | FIXED | runtime_invariants.py:66-78 `threading.Lock`；:100-107 violations/clear/raise_if_violated 快照；:171-180 _seen 去重在锁内 | CONFIRMED |
| AUD-22318 | 每事件回放全量 any() O(n²) | FIXED | :184-208 `_on_event` 走 `_check_core_indexed`（O(1) 增量索引 _tool_starts/_terminal_runs）；自定义检查仅存在时取一次快照；check_run 诊断 API 不变 | CONFIRMED |
| AUD-22319 | _bindings/_job_to_runs 无并发保护 | FIXED | scheduler_adapter.py:139 RLock 全方法；:180-186 propagate_cancel 锁内快照、锁外逐个同步 | CONFIRMED |
| AUD-22321 | SessionEventLog seq check-then-act 竞态 | FIXED | session_fork.py:78-165 append/events/fork/fork_info 全部 `with self._lock`（:25/:40/:54/:80 相对行号） | CONFIRMED |
| AUD-22413 | SkillStudioStore 无锁无上限、_registry 死字段 | FIXED | skill_studio_drafts.py:581-588 `max_drafts`（默认 500，<=0 拒绝）+ RLock；:595-601 超限按 updated_at 淘汰、刚保存者豁免；`_registry` 全文件零命中（既有测试 :307 `not hasattr ... or is None` 兼容） | CONFIRMED |

### src/shared/ai_models（12 条）

| ID | 计划指控 | 报告结论 | HEAD 独立核实（file:line 证据） | 判定 |
|----|---------|---------|-------------------------------|------|
| AUD-31503 | 超时泄漏 daemon loader + 吞真实异常 | FIXED | ai_plugin_engine.py:69-74 `_stray_loaders` + QMutex；:335-344 加载异常 logger.error；:358-375 超时收容 + 存活剪枝 + logger.warning | CONFIRMED |
| AUD-31525 | error 路径不清理 worker | INVALID（已修） | denoise_worker.py:611-613 三路径 deleteLater（由 218bb19d4 引入，早于本批）；EF-15 未改该文件；护栏测试真实驱动 error 路径验证析构 | INVALID 属实 |
| AUD-31603 | security 子进程无 timeout | FIXED | credential_store.py:34 `_SECURITY_TIMEOUT_SECONDS=10.0`；get/set/delete 三入口均带 timeout（:70/:110/:131），get 超时回退文件后端、set 超时 RuntimeError 不假成功 | CONFIRMED |
| AUD-31617 | manifest 非原子写 + load 静默吞错 | FIXED | model_pack_manager.py:897-917 原子写（mkstemp+fsync+os.replace+finally unlink）；:899-908 load 失败提级 logger.warning | CONFIRMED |
| AUD-31618 | default_cache_dir 依赖 parents[4] 硬编码 | FIXED | model_pack_manager.py:57-77 向上定位 `src` 锚点取父目录；找不到回退旧深度 + warning | CONFIRMED |
| AUD-31619 | _installed 与 manifest 保存无锁 | FIXED | _save_manifest 锁内快照 records（:918-920）；mark_installed/remove/verify 持 `self._lock`（grep :326/:529 等） | CONFIRMED |
| AUD-31710 | generated_media 无容量上限 | FIXED | volcengine_generation_provider.py:47-110 `prune_generated_media_cache`（512MB+500 文件双上限、最旧优先、跳过 `.` 临时文件、删除失败留痕）；:554/:753 两处写入后调用 | CONFIRMED |
| AUD-31807 | 会话缓存竞态/无淘汰/worker 未 deleteLater | FIXED | inference_engine.py:528-604 per-path 锁双检 + OrderedDict LRU（上限 4）+ 淘汰日志；:1121-1169 `_schedule_worker_release` 鸭子类型 deleteLater，终结路径统一调度 | CONFIRMED |
| AUD-31808 | 死代码与文档不符 | FIXED | `import time` 删除（:32 仅 traceback 在用，grep 证实）；tile_size/overlap 死形参删除（:678/:855）；:998-1008 `_latest_previews`（上限 8）+ `latest_preview()` 访问器 | CONFIRMED |
| AUD-31810 | model_manager manifest 非原子写 | FIXED | model_manager.py:299-318 tmp + fsync + os.replace；失败 logger.error + 清理临时文件 | CONFIRMED |
| AUD-31811 | MODEL_REGISTRY 无锁/单例无锁/cancel_download 同步 wait | FIXED | :40-43 `_REGISTRY_LOCK`（MODEL_REGISTRY 保持模块级，写路径 :326/:417/:499/:529/:545/:558 持锁）；:586-593 单例双检锁；:432-449 cancel_download 只投递协作取消，无 wait | CONFIRMED |
| AUD-31815 | RIFE 硬编码 CPU EP + 会话缓存无锁 | FIXED | rife_interpolator.py:28-30 `_SESSION_CACHE_LOCK`；:78-97 `get_configured_providers()`（尊重用户 GPU 偏好，CPU 保底）+ `get_session_options()`，创建在锁内；异常 logger.warning 留痕 | CONFIRMED |

## 红线发现

无 Critical。备注 2 条（非阻塞）：
1. rife_interpolator.py:97 `except Exception` 覆盖整段（含 import 与 session 创建）统一 warning 后返回 None——留痕符合铁律，但范围偏宽；后续可按需细分。
2. inference_engine `_loading_locks` 锁表清理阈值 32 略带魔数色彩，注释已说明安全性（持锁线程持引用），可接受。

## 最终结论

**PASS**。22 FIXED + 1 INVALID 全部属实（INVALID 有提交考古证据 + 真实护栏测试）；无越界；无红线违规；锁类测试以"持锁阻塞"断言真实验证语义。允许 `check_fixes.py --mark`（commit 填 `377e19318`；AUD-31525 按计划规程以 `--note "invalid:..."` 标记）。
