# EF-04 批修复报告 — 子运行 2/2（part2）

- 分支：`dev`（未做任何 git 写操作）
- 范围：`src/features/edit/ai_editing/` 下 18 条 Suggestion 级审计发现（AUD-14117 … AUD-14918）
- 测试：`tests/edit/ai_editing/test_ef04_aud_part2.py`（35 个用例，全绿）
- 状态：**18/18 FIXED，0 INVALID / 0 DUPLICATE / 0 BLOCKED**

## 逐条结论

| ID | 结论 | 修复位置（文件:行） | 说明 |
|----|------|--------------------|------|
| AUD-14117 | FIXED | `src/features/edit/ai_editing/profile_tts_provider.py:41-44, 149-171, 190-195` | 逐 cue 的同步 `provider.generate` 改经 `_generate_bounded()`：单工作线程执行 + `future.result(timeout=REMOTE_CUE_TIMEOUT_SECONDS=120s)`，超时抛带 request_id 的 `RuntimeError`，`shutdown(wait=False)` 保证调用线程立即返回不二次等待。`_decode_audio` 的 ffmpeg 子进程原有 `timeout=60`，至此每 cue 阻塞有界。测试以 monkeypatch(raising=True) 钉住循环必须走有界路径（回退即 AttributeError 失败，不会进入旧的无限阻塞）。 |
| AUD-14201 | FIXED | `src/features/edit/ai_editing/provider_plugin.py:120-137, 195-197, 213-215, 233-239, 243-246` | `AnalysisPluginHost` 新增 `_bindings_lock`（threading.Lock），register 的成员检查、写入与 activate/descriptor/list_descriptors 的读取全部持锁，与线程安全的 `PluginRegistry` 对齐。并发 register×8 + list 压测无异常。 |
| AUD-14206 | FIXED | `src/features/edit/ai_editing/release_evidence.py:71-127` | `TelemetryCollector._events` 由 `list` + `pop(0)` 改为 `deque(maxlen=...)`——饱和后 O(1) 淘汰最旧。FIFO 语义不变（旧首条被淘汰、最新保留）。 |
| AUD-14207 | FIXED | `src/features/edit/ai_editing/release_evidence.py:71-127` | `TelemetryCollector` 新增 `threading.Lock`：`opted_in`/`set_opt_in`/`emit`/`events`/`export`/`clear` 全部持锁，消除 clear×append×export 迭代竞态。3 emit 线程 + 2 读线程 + 1 opt-in 线程压测：600/600 条入账、无异常。 |
| AUD-14314 | FIXED | `src/features/edit/ai_editing/runtime_capability_snapshot.py:133-185` | `CapabilitySnapshotStore` 新增 `_lock`：`get`/`set`/`subscribe`/退订持锁；监听者回调在锁外执行（防回调内 get/subscribe 死锁）；监听者异常改为 `logger.warning` 留痕（不再是静默 `pass`）；退订函数幂等（旧 lambda 重复 remove 抛 ValueError）。 |
| AUD-14318 | FIXED | `src/features/edit/ai_editing/schema.py:109-133` | `validate_plan_structure` 两个连续 docstring 字面量合并为一个（Args + Returns + 不校验 domain 的说明）；测试用 AST 断言函数体只含 1 个字符串字面量表达式（回退即失败）。 |
| AUD-14504 | FIXED | `src/features/edit/ai_editing/subtitle_draft.py:1-7` | 模块 docstring 更正：`ADD_SUBTITLE` 已接入编译器（`plan_compiler.COMPILABLE_OPERATIONS` 含之，测试同时验证该前提），删除"强提交必 CompileError"的过时声明。 |
| AUD-14600 | FIXED | `src/features/edit/ai_editing/tests/test_assistant_service_cancel.py:90-113` | cancelled 短路测试不再只 `assert result is not None`：显式断言 `result.degradation == "cancelled"`、状态机翻到 `AssistantState.CANCELLED`、留下取消消息。part2 另做源码 introspection 反例（弱断言回归即失败）+ 真实服务功能契约验证。 |
| AUD-14601 | FIXED | `src/features/edit/ai_editing/tests/test_assistant_service_cancel.py:127-181` | 死脚手架替换：新增 `_StageEmittingRunner`（真实执行 `emit_stage` 回调的 fake runner），emit 回调签名改为真实契约 `(kind, node_id, payload=None)`，并断言 `emitted == [("progress", "", {"stage": "analyze"})]`；补充"不传回调亦正常"用例。 |
| AUD-14603 | FIXED | `src/features/edit/ai_editing/tests/test_assistant_service_cancel.py:28-31` | 两处函数内 `WorkflowResult` 局部导入删除，并入模块顶部 `from ...workflow import (WorkflowInput, WorkflowResult, WorkflowId)`。part2 测试用 AST 断言函数/类体内无 workflow 局部导入且顶部含 WorkflowResult（回退即失败）。 |
| AUD-14703 | FIXED | `src/features/edit/ai_editing/timebase.py:292-316` | `from_audio_sample` 改为全程整数运算：`frame = (sample*fps.num + den//2) // den`（half away from zero，与 `_pts_to_frame_via_timebase` 同一策略族），不再经 `to_fps()` 浮点中转。既有用例（48000→24 等）不变；精确 0.5 帧用例钉死策略（回退到浮点 round 即失败）。 |
| AUD-14711 | FIXED | `src/features/edit/ai_editing/tool_call_log.py:199-213` | `ToolCallLog._records` 改 `deque(maxlen=max(1, max_entries))`——饱和后每条 O(1) 淘汰；AUD-14710 的锁语义与 `records()/count/clear()` 快照语义不变；`_PersistentToolCallLog`（AUD-14807）继承路径不受影响。 |
| AUD-14803 | FIXED | `src/features/edit/ai_editing/tool_result.py:124-138` | `ToolResult.to_dict()` 的 `artifact_refs` 逐项过 `sanitize_for_storage`（绝对路径 → `<absolute-path>`、secret 形状 → `api_key=<redacted-secret>`），与模块"审计事件绝不泄漏绝对路径"红线一致；普通 ref 原样保留。 |
| AUD-14809 | FIXED | `src/features/edit/ai_editing/toposctl.py:528-543` | `--log-level` 非法值不再使 `main()` 崩溃：捕获 `setLevel` 的 `ValueError`，降级为 WARNING 并 `logger.warning("invalid --log-level %r; ...")` 留痕；合法级别名行为保持（debug → DEBUG）。stdout 单一 JSON / 退出码契约不变。 |
| AUD-14817 | FIXED | `src/features/edit/ai_editing/transaction_service.py:470-504, 526-541` | 授权移到可预先失败的检查之后：commit 在 `_authorize`（consume 一次性 grant）**之前**先做 `get_sequence` + 乐观锁预检（与 commit_command 在同一未变更 state 上等价），revision 冲突/缺 timeline 直接返回冲突结果，grant 不再被烧掉；残余 TOCTOU 窗口的 `except RevisionConflict` 分支错误文案明确告知"grant 已消耗需重新批准"。测试模拟 preview-通过→state 被推进的 TOCTOU：冲突返回且 grant 仍可正常 consume（旧实现下该断言因 GrantNotFound 失败）。 |
| AUD-14904 | FIXED | `src/features/edit/ai_editing/transcript_provider.py:397-449` | `analyze()` 拆为薄包装 + `_analyze_impl`：`thread_local.request_options` 以 sentinel 保存旧值，`finally` 中删除或恢复（与 `analyze_with_preprocessor`/`validate_options` 同模式），正常/异常路径均不残留；线程池线程复用不再读到上一次请求的选项。 |
| AUD-14912 | FIXED | `src/features/edit/ai_editing/transcript_router.py:251-256, 271-289` | `ProfileAwareTranscriptProvider._remote_by_profile` 双检锁（`_remote_lock`）：无锁命中为热路径，miss 进锁二次确认后构造——并发 analyze 下每个 profile 恰好实例化一次（8 线程 Barrier 压测，构造计数 == 1）。 |
| AUD-14918 | FIXED | `src/features/edit/ai_editing/tts_provider.py:58-68, 346-369` | `Qwen3TTSProvider._engines` 双检锁（`_engines_lock`）：并发 synthesize 不再重复加载 3.8GB 权重（8 线程 Barrier 压测，load 计数 == 1 且引擎同实例）；不同 model_kind 仍各自加载一次（分立缓存语义保持）。 |

## 回退敏感性验证（随机抽 2 条）

| ID | 操作 | 结果 |
|----|------|------|
| AUD-14703 | 临时还原 `from_audio_sample` 为浮点实现 → 跑对应用例 | `test_aud_14703_exact_half_rounds_half_up_not_bankers` **FAILED**（0.5 帧走 banker's rounding 得 0）→ 还原修复 → **PASSED** |
| AUD-14207（连带 14206） | 临时还原 `TelemetryCollector` 为无锁 list 实现 → 跑对应用例 | `test_aud_14207_collector_has_lock`、`test_aud_14206_events_fifo_eviction_correct_and_deque_backed` **均 FAILED** → 还原修复 → **PASSED** |

## 验证关卡输出摘要

1. **py_compile**：15 个改动源文件 + 2 个测试文件全部编译通过（`GATE1_COMPILE_OK`）。
2. **新测试文件**：`.venv/bin/python -m pytest tests/edit/ai_editing/test_ef04_aud_part2.py -q -p no:cacheprovider` → **35 passed in 1.6s**。
3. **定向既有测试**（无新增失败）：
   - `test_timebase_service.py + test_tool_call_log.py + test_release_evidence.py + test_runtime_capability_snapshot.py` → **74 passed**
   - `test_transaction_service.py + test_provider_plugin_host.py + test_toposctl.py + test_profile_speech_routing.py` → **55 passed**
   - `src/.../tests/test_assistant_service_cancel.py + test_taep_contracts.py + test_tool_facade.py` → **89 passed**
4. 已知预存失败（GPU 位精确 / x265 10-bit / VT 探针）未涉及、未触碰。

## 约束自查（.clinerules）

- 未触碰 UI 线程 / OpenGL / ModernGL；新增锁均为 `threading.Lock`，监听者回调锁外执行避免死锁。
- 无裸 `except:`；唯一的 `except Exception`（snapshot 监听者）改为带 `logger.warning` 留痕；toposctl 的 `except ValueError` 为具体类型 + warning 留痕。
- 新增/修改函数均有类型注解（`_generate_bounded`、`unsubscribe`、`_analyze_impl`、`_fake_preview` 等）。
- AI 域：未引入任何 UI 线程同步等待；远程 TTS 增加的是**有界**等待而非无界阻塞；ASR/TTS/LLM 全部 mock，未真实调用服务；无模态对话框。
- 白名单外文件零改动；`REMEDIATION_PLAN.md` 未修改。

## 改动文件清单（工作树未提交，未 git add/commit）

源码（14）：
- `src/features/edit/ai_editing/profile_tts_provider.py`
- `src/features/edit/ai_editing/release_evidence.py`
- `src/features/edit/ai_editing/provider_plugin.py`
- `src/features/edit/ai_editing/runtime_capability_snapshot.py`
- `src/features/edit/ai_editing/schema.py`
- `src/features/edit/ai_editing/subtitle_draft.py`
- `src/features/edit/ai_editing/timebase.py`
- `src/features/edit/ai_editing/tool_call_log.py`
- `src/features/edit/ai_editing/tool_result.py`
- `src/features/edit/ai_editing/toposctl.py`
- `src/features/edit/ai_editing/transaction_service.py`
- `src/features/edit/ai_editing/transcript_provider.py`
- `src/features/edit/ai_editing/transcript_router.py`
- `src/features/edit/ai_editing/tts_provider.py`

测试（2）：
- `src/features/edit/ai_editing/tests/test_assistant_service_cancel.py`（AUD-14600/14601/14603 的修复对象本身）
- `tests/edit/ai_editing/test_ef04_aud_part2.py`（新建，35 用例）

报告（1）：
- `docs/plans/2026-09-28-multi-machine-program/fixes/reports/ef-04-part2.md`（本文件）
