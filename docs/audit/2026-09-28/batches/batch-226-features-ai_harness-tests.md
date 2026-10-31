# 批次报告：batch-226 — src/features/ai_harness/tests/（P2）

- 审查范围：8 个测试文件，共 2,016 行（编号区间 AUD-22600–AUD-22699）
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失
- 总体说明：本批全部为纯逻辑单元测试（无 Qt 事件循环、无 OpenGL、无渲染线程参与），GPU/GLSL/HDR/信号防递归维度天然不适用；审查重点为测试有效性（是否真正验证其声称的红线/契约）、测试隔离（全局状态泄漏）、异常与取消路径覆盖、以及代码质量。

统计：Critical 0 / Major 1 / Suggestion 20

---

## src/features/ai_harness/tests/test_legacy_adapters.py (行数: 401)

### 发现

- [AUD-22600] [Major] 取消 token 透传验收测试空转：adapter 从未被调用，约 50 行死代码
  - 位置: src/features/ai_harness/tests/test_legacy_adapters.py:285-361
  - 证据:
    ```python
    rt = HarnessRuntime()
    adapter = LegacyHarnessAdapter(svc, rt)          # 创建后从未使用
    inv = Invocation(
        source=InvocationSource.DEVELOPER, skill_id="topos.unknown_skill", ...)
    rid = rt.start(inv)
    rt.cancel(rid)
    assert rt.is_cancelled(rid)
    # ... 注释自述"只验证 cancel 状态在 runtime 里成立"
    assert rt.is_cancelled(rid) is True              # 重复断言，仅验证 runtime 自身
    ```
    同测试中定义的 `registry`（:300-307）、`_SlowExecutor`（:310-317）、`_StubArtifact`（:318-340）以及 `import time as _time`（:309）均未被使用；已核实 `HarnessRuntime.is_cancelled`（runtime.py:485-489）只是读取 `cancel()`（:352）设置的标志位。
  - 影响: 文件头（:9）声称覆盖"取消 token 透传（plan §10.2 P0.6）：run.cancel -> run_skill 收到 cancel_requested"，红线验收实际无任何保护——adapter 若不再把 cancel token 传给 run_skill，此测试依然通过（虚假置信）。死代码还误导维护者以为多角色路径被测过。
  - 建议: 用可轮询 cancel token 的 stub SkillRuntime/executor 走 `adapter.invoke(...)` 后取消，断言 run_skill/executor 侧读到 `cancel_requested`；删除 :290-341 的未用 stub 与导入；去掉重复断言。
  - 置信度: 高

- [AUD-22601] [Suggestion] test_workflow_succeeds_with_variants 未断言任何产出/variants
  - 位置: src/features/ai_harness/tests/test_legacy_adapters.py:220-229
  - 证据:
    ```python
    rec = rt.get_run(rid)
    assert rec.status in (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_WITH_WARNINGS)
    # 有产出
    assert rec.source == InvocationSource.WORKFLOW
    assert rec.skill_id == "topos.voiceover_polish"
    ```
  - 影响: 测试名与注释声称验证"有产出（variants）"，但没有任何 `rec.artifacts` 断言；workflow 空产出也会通过。
  - 建议: 补 `assert rec.artifacts`（及 variants 相关字段）断言，或将测试改名为仅描述状态断言。
  - 置信度: 高

- [AUD-22602] [Suggestion] fixture 构造期异常时 EditSession 全局绑定泄漏到后续测试
  - 位置: src/features/ai_harness/tests/test_legacy_adapters.py:161-173, 191-204
  - 证据:
    ```python
    EditSession.unbind()
    session = EditSession.bind(_Host(state))   # bind 之后若下行构造抛错：
    bs = BriefService()
    bs.save(EditorialBrief(...))               # 此处或 AssistantService(...) 抛错时，
    svc = AssistantService(...)                # yield 之后的 teardown unbind 不会执行
    ```
  - 影响: `_build_service_with_analysis` 内 bind 后任一步抛错，模块级单例 `EditSession` 保持绑定，污染同进程后续测试（跨测试耦合，失败排查困难）。当前靠下次构造开头的 `unbind()` 自愈，属隐性依赖。
  - 建议: 把 bind 移到最后一步，或在 fixture 中用 try/except 包住构造并在异常时显式 `unbind()` 再 re-raise。
  - 置信度: 中

- [AUD-22603] [Suggestion] 测试与辅助函数普遍缺类型注解
  - 位置: src/features/ai_harness/tests/test_legacy_adapters.py:50-401（如 :105 `def _build_service_with_analysis(segs):`、:207 `def _invocation(...)` 及各 test 方法）
  - 证据: `def _build_service_with_analysis(segs):` 无参数/返回注解；全部 test 方法无 `-> None`。
  - 影响: 与项目强类型约束不一致；IDE/静态检查无法发现 fixture 返回结构误用。
  - 建议: 至少为 helper/fixture 补注解（`-> tuple[AssistantService, ProjectSequences]` 等）。
  - 置信度: 高

### 通过项
- 线程安全：全部同步单线程调用，无共享可变状态跨线程访问。
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及（纯逻辑测试）。
- 异常路径：`test_workflow_exception_recorded_not_raised`（:365-384）真实有效——替换 `run_workflow` 抛错并断言 FAILED + `WORKFLOW_EXCEPTION` 诊断，不冒泡。
- 状态一致性：chat 与 workflow 同源 RunRecord（:231-246）、单一 runtime（:388-401）断言有效。
- 输入验证：unknown skill 映射抛 `ValueError`（:67-73）、前缀剥离（:75-82）有覆盖。
- 降级语义：无证据 → 非 FAILED（:248-258）符合 P0.6 验收方向。

---

## src/features/ai_harness/tests/test_llm_role_executor.py (行数: 273)

### 发现

- [AUD-22604] [Suggestion] 意外的海象表达式 `ContextScope := ContextSlice()` 产生无用变量
  - 位置: src/features/ai_harness/tests/test_llm_role_executor.py:132
  - 证据:
    ```python
    art = ex.execute(TaskRole.QA_REVIEWER, ContextScope := ContextSlice(), ())
    ```
  - 影响: `ContextScope` 从未被使用，属编辑残留；变量名伪装成类名，易误导读者以为存在该类型（运行时无害，因海象在实参位置合法）。
  - 建议: 改为 `ex.execute(TaskRole.QA_REVIEWER, ContextSlice(), ())`。
  - 置信度: 高

- [AUD-22605] [Suggestion] `list(degradation_counts.keys())[0]` 在空字典时抛 IndexError 而非断言失败
  - 位置: src/features/ai_harness/tests/test_llm_role_executor.py:122, 140
  - 证据:
    ```python
    assert "no_model_client" in list(ex.degradation_counts.keys())[0]
    ...
    assert "output_validation_failed" in list(ex.degradation_counts.keys())[0]
    ```
  - 影响: 降级键缺失（被测行为回归为"未降级"或键名变化）时报 `IndexError: list index out of range`，失败原因指向错误位置；同文件其他用例（:128、:134、:216）已用 `any(...)` 惯用式。
  - 建议: 统一改为 `assert any("no_model_client" in k for k in ex.degradation_counts)`。
  - 置信度: 高

- [AUD-22606] [Suggestion] secret 透传红线测试只查字段命名，不验证实际数据流
  - 位置: src/features/ai_harness/tests/test_llm_role_executor.py:249-255
  - 证据:
    ```python
    fields = {f for f in dir(ContextSlice) if not f.startswith("_")}
    for forbidden in ("secret", "api_key", "password", "absolute_path"):
        assert not any(forbidden in f.lower() for f in fields), ...
    ```
  - 影响: 只证明 ContextSlice 没有"名字里带 secret 的字段"，若敏感内容经由 `brief_summary` 等既有字段透传给 model_client，测试无法发现——与文件头"上下文最小化（不传 secret/绝对路径）"的红线声称不匹配。
  - 建议: 构造含敏感标记（如假 api_key 字符串、绝对路径）的输入，捕获传给 stub model_client 的 `system`/`user` 文本，断言不含这些标记。
  - 置信度: 中

- [AUD-22607] [Suggestion] 测试与 fake client 方法缺类型注解
  - 位置: src/features/ai_harness/tests/test_llm_role_executor.py:47-109, 118-273
  - 证据: `def generate(self, *, system, user, role, max_tokens=None, timeout_ms=None):` 等均无注解；test 方法无 `-> None`。
  - 影响: 与项目强类型约束不一致；fake 与真实 ModelClient 协议的签名漂移不能被静态发现。
  - 建议: 为 fake `generate` 补齐与 `ModelClient` 协议一致的注解。
  - 置信度: 高

### 通过项
- 线程安全 / GPU 资源 / GLSL·HDR / 信号防递归：不涉及。
- 异常与降级路径：无 client（:118）、异常（:124）、超时（:130）、坏 JSON（:136）、markdown fence（:194）、禁用 fallback 抛错不静默（:142-149）覆盖充分且断言有效。
- 输入验证（外部模型输出不可信）：四角色 typed artifact 校验（:158-192）真实有效，`_artifact_kind_matches` 契约有保护。
- 预算守门：超预算降级（:208-216）、未超成功且零降级（:218-226）双向覆盖。
- 红线（无 approval 字段/不写时间线，:235-247）：断言具体有效。
- 降级可见性：计数累计（:264-268）断言明确。

---

## src/features/ai_harness/tests/test_llm_role_model_client.py (行数: 54)

### 发现

- [AUD-22608] [Suggestion] fake client 内部用 `assert` 校验调用契约，失败时被 adapter 的宽异常捕获吞掉，测试失败信息失真
  - 位置: src/features/ai_harness/tests/test_llm_role_model_client.py:20-26
  - 证据:
    ```python
    def complete(self, messages, *, json_mode=False, temperature=0.7, max_tokens=None):
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert json_mode is True
        assert max_tokens == 100
        return '{"approved":true}'
    ```
    已核实被测 `LlmRoleModelClient.generate`（llm_role_model_client.py:52-53）`except Exception` 将异常转为 `ModelResponse(error=f"{type(exc).__name__}")`——上述任一 assert 失败时，最终失败点落在 `assert response.text == ...`，真实原因（如消息顺序错误）被掩盖为 "AssertionError ≠ '{"approved":true}'"。
  - 影响: 契约回归时报告的失败与根因脱节，排查成本高（测试仍会失败，无正确性风险）。
  - 建议: fake 内改用抛出带说明的 `AssertionError` 之外的显式记录（如把 messages/json_mode 存到实例属性，在测试正文断言），或接受现状但知悉此局限。
  - 置信度: 中

### 通过项
- 线程安全 / GPU 资源 / GLSL·HDR / 信号防递归：不涉及。
- 错误脱敏红线：`_FailingClient` 抛 "secret must not escape"，断言 `failed.error == "RuntimeError"` 且不含 "secret"（:45-50）真实有效。
- 输入验证：`timeout_ms=0` → `invalid_role_timeout`（:51-54）覆盖边界。
- 类型注解：两个测试函数均有 `-> None`。
- usage 映射（tokens/cost 来自 `last_usage`）断言具体。

---

## src/features/ai_harness/tests/test_mcp_outbound.py (行数: 345)

### 发现

- [AUD-22609] [Suggestion] `StdioTransport` 导入未使用
  - 位置: src/features/ai_harness/tests/test_mcp_outbound.py:31
  - 证据:
    ```python
    from src.features.ai_harness.mcp_outbound import (
        McpAuthorizationLog, McpConnectionProfile, McpOutboundClient,
        McpServerManifest, McpServerRegistry, StdioTransport,
    )
    ```
    全文仅使用前 6 个名称，`StdioTransport` 无任何引用。
  - 影响: 死导入；若意图覆盖 StdioTransport 行为则属遗漏。
  - 建议: 删除该导入，或补真实 StdioTransport 的最小行为测试。
  - 置信度: 高

- [AUD-22610] [Suggestion] test_discover_caches 未验证"第二次不再调 transport"
  - 位置: src/features/ai_harness/tests/test_mcp_outbound.py:255-260
  - 证据:
    ```python
    def test_discover_caches(self, client_with_server):
        _, client = client_with_server
        client.discover("srv")
        # 第二次从缓存（不再调 transport）
        m2 = client.discover("srv")
        assert m2 is not None
    ```
  - 影响: 断言与注释声称的缓存行为无关——即使 discover 每次都打 transport（缓存失效），测试仍通过（同文件 :266-281 已示范了计数 transport 的写法）。
  - 建议: 用带调用计数的 fake transport，断言第二次 discover 后 `send_request` 调用数不变。
  - 置信度: 高

- [AUD-22611] [Suggestion] test_max_events_cap 未验证保留的是"最近"事件
  - 位置: src/features/ai_harness/tests/test_mcp_outbound.py:337-345
  - 证据:
    ```python
    for i in range(10):
        log.record(McpAuthorizationEvent(timestamp=float(i), ...))
    assert len(log.events()) == 3  # 截断到最近 3
    ```
  - 影响: 只验证数量截断；若实现保留最旧 3 条（审计上更糟），测试同样通过，与注释"最近 3"不符。
  - 建议: 补断言 `[e.timestamp for e in log.events()] == [7.0, 8.0, 9.0]`（或等价顺序断言）。
  - 置信度: 高

- [AUD-22612] [Suggestion] 测试与 `_profile` 辅助缺类型注解
  - 位置: src/features/ai_harness/tests/test_mcp_outbound.py:40-47, 50-69 等
  - 证据: `def _profile(profile_id="srv", **kw):`、`def send_request(self, req):` 均无注解。
  - 影响: 与项目强类型约束不一致。
  - 建议: 为 `_profile` 补 `-> McpConnectionProfile`，fake transport 方法补参数/返回注解。
  - 置信度: 高

### 通过项
- 线程安全 / GPU 资源 / GLSL·HDR / 信号防递归：不涉及。
- 授权链覆盖全面且断言有效：allowlist（:164）、BLOCKED（:170）、会话未启用（:176）、写未审批 denied（:182）、写审批通过（:188）、工具 allowlist（:193）、transport 异常归 error（:201）、无 factory 降级（:210）。
- 红线"外部输出只作数据"（:224-231）：断言 result 为 dict，有效。
- 红线"非只读 scope 一律需审批"（:233-240）：有效覆盖 scope 门控语义。
- 输入验证：discovered inputSchema 强制（:283-297，required query 缺失 → denied_invalid_request）、禁用 server 不触 transport（:266-281）覆盖良好。
- 授权审计：全决策记录（:306）、denial_count（:316）、按 profile 过滤（:322）、write 标记（:330）有效；fixture（:72-78）teardown 正确 `client.close()`。
- profile 不存 secret（:93-99）：字段级断言与 credential_ref 语义有效。

---

## src/features/ai_harness/tests/test_p1_editorial_provider.py (行数: 199)

### 发现

- [AUD-22613] [Suggestion] 返回类型注解为字符串前向引用，指向函数内部导入的名称（模块级不可解析）
  - 位置: src/features/ai_harness/tests/test_p1_editorial_provider.py:26-28
  - 证据:
    ```python
    def _make_analysis_service_with_transcript() -> "AnalysisService":
        """构造一个含 transcript + shot segments 的 AnalysisService。"""
        from src.features.edit.ai_editing.analysis_service import AnalysisService
    ```
  - 影响: 因 `from __future__ import annotations` 运行时不求值故不报错，但模块命名空间无 `AnalysisService`，mypy/IDE 无法解析，注解形同虚设。
  - 建议: 将 `AnalysisService` 提升为模块级导入，注解去引号。
  - 置信度: 高

- [AUD-22614] [Suggestion] provider fake 与测试函数缺类型注解
  - 位置: src/features/ai_harness/tests/test_p1_editorial_provider.py:70-199
  - 证据: `def test_make_editorial_provider_filters_by_kind():` 等 test 方法与局部 provider 无注解。
  - 影响: 与项目强类型约束不一致。
  - 建议: 补 `-> None` 及 provider 的 `Callable[[tuple[str, ...], str], list[SemanticSegment]]` 注解。
  - 置信度: 高

### 通过项
- 线程安全 / GPU 资源 / GLSL·HDR / 信号防递归：不涉及。
- 输入验证（provider 契约）：按 kind 过滤（:70-79，不含 SHOT）、多 asset 聚合（:82-112）、未知 asset 空返回（:115）、未知 kind 空返回（:126）、空 asset_ids（:136）覆盖完整且断言具体。
- 异常路径：未知输入返回空不崩，符合契约。
- 装配一致性：editorial_provider 注入后 `adapter._editorial_provider` 非 None（:151-166）、默认 None 向后兼容（:169-178）、provider 产物可被 `_fetch_evidence` 消费（:186-199）断言有效。

---

## src/features/ai_harness/tests/test_p1_host_wiring.py (行数: 211)

### 发现

- [AUD-22615] [Suggestion] skill_resolver 绑定契约测试实际未验证绑定（默认 router 也能通过）
  - 位置: src/features/ai_harness/tests/test_p1_host_wiring.py:73-84
  - 证据:
    ```python
    def test_build_p1_dependencies_router_skill_resolver_bound_to_registry(self):
        """router 的 skill_resolver 绑到共享 registry（is_enabled 一致）。"""
        ...
        # router 内部 skill_resolver 应与 reg.is_enabled 一致
        # 测一个真实注册的 skill + 一个不存在的 skill
        assert router.route("自动字幕").skill_id == "topos.auto_caption"
        # 未注册的 skill 不会因 resolver 失败（router 的 skill_resolver 只校验已知 skill_id）
    ```
  - 影响: 唯一断言不涉及 resolver——若 `build_p1_dependencies` 漏传 `skill_resolver`（契约失守，未注册/禁用 skill 不会被拦截），测试仍通过；注释承诺的"不存在的 skill"用例未写。
  - 建议: 补负向用例（构造禁用/未注册 skill 的路由，断言 resolver 拦截），或直接检查 router 是否以 registry 为 resolver 构造。
  - 置信度: 高

- [AUD-22616] [Suggestion] "only accepts imported" 命名与覆盖不符：只测正向路径
  - 位置: src/features/ai_harness/tests/test_p1_host_wiring.py:95-114
  - 证据:
    ```python
    def test_localization_media_provider_only_accepts_imported_generated_asset(self):
        """A GenerationBin id cannot become a timeline asset before import."""
        class Bridge:
            def get_ai_generated_media_asset_id(self, generated_asset_id: str) -> str:
                return "media-tts-1" if generated_asset_id == "generated-tts-1" else ""
        ...
        artifact = resolver(invocation, (), "en")
        assert artifact is not None
    ```
  - 影响: 名字与 docstring 声称"仅接受已导入资产"，但 Bridge 返回 `""` 的拒绝分支（红线：GenerationBin id 不得未导入即上时间线）从未被断言。
  - 建议: 补 `tts_generated_asset_id="unknown"` → resolver 返回 None 的断言。
  - 置信度: 高

- [AUD-22617] [Suggestion] 测试方法与 stub 类方法缺类型注解
  - 位置: src/features/ai_harness/tests/test_p1_host_wiring.py:32-126, 134-172
  - 证据: 各 test 方法无 `-> None`；`_StubHelpSources.list_diagnostics` 等无返回注解。
  - 影响: 与项目强类型约束不一致。
  - 建议: 补注解（help stub 可标注返回 `list[dict] | None`）。
  - 置信度: 高

### 通过项
- 线程安全 / GPU 资源 / GLSL·HDR / 信号防递归：不涉及（MagicMock 隔离真实 service）。
- 装配契约主体有效：全组件类型断言（:32-50）、单一 runtime 红线 `deps["adapter"]._runtime is runtime`（:53-60）、P2 skill 注册 enabled（:63-70）、editorial provider 可选（:86-92）。
- 异常路径：help_sources 缺失/坏源三态降级（:175-211）断言有效，且验证单点失败不阻断其他装配。
- 重复调用返回独立实例、不共享可变 registry（:117-126）有效。

---

## src/features/ai_harness/tests/test_p2_acceptance.py (行数: 210)

### 发现

- [AUD-22618] [Suggestion] `from dataclasses import replace` 导入未使用
  - 位置: src/features/ai_harness/tests/test_p2_acceptance.py:23
  - 证据:
    ```python
    from dataclasses import replace
    ```
    全文无 `replace(` 调用（`_make_analysis` 均直接构造）。
  - 影响: 死导入，暗示本应用 replace 构造变体的测试未写或被删。
  - 建议: 删除导入，或补回用 `replace` 派生分析的用例。
  - 置信度: 高

- [AUD-22619] [Suggestion] 测试与 fake 构造函数缺类型注解
  - 位置: src/features/ai_harness/tests/test_p2_acceptance.py:50-117 等
  - 证据: `def _make_analysis(asset_id, router_selection, confidence=0.5, words=False):`、`def _run_auto_caption_with_analysis(analysis_by_asset):` 及各 test 方法均无注解。
  - 影响: 与项目强类型约束不一致。
  - 建议: 补参数/返回注解（如 `-> AssetAnalysis`、`-> tuple[RunRecord, CompiledSkill]`）。
  - 置信度: 高

### 通过项
- 线程安全 / GPU 资源 / GLSL·HDR / 信号防递归：不涉及。
- plan §10.4 验收断言具体有效：Demucs 路径（used_demucs/final_selection/selected step/evidence_version，:123-137）、Qwen 质量可见（:139-148）、WhisperX 回退（:151-159）、高质不进 Demucs（:162-171）、多 asset 独立 trace（:197-210）。
- 红线"不直接写时间线"：断言 RunRecord 无 apply/commit 事件（:185-194），有效。
- 状态比较：`rec.status in ("succeeded", ...)` 与 `RunStatus`（纯字符串常量类，invocation_models.py:100-117）兼容，无类型问题。
- provider 双返回值 `(segments, analysis_by_asset)` 契约被端到端消费验证。

---

## src/features/ai_harness/tests/test_p2_wiring.py (行数: 323)

### 发现

- [AUD-22620] [Suggestion] 模块 docstring 与实际断言矛盾（target=workflow vs target=editorial）
  - 位置: src/features/ai_harness/tests/test_p2_wiring.py:10（对照 :147-162）
  - 证据:
    ```python
    # 模块头（:8-10）：
    - SkillCompiler 能编译 P2 skill 的 invocation（target=workflow，因为 roles=()）。
    # 实际测试（:160-162）：
    compiled = compiler.compile(definition, inv)
    assert compiled.target == "editorial"
    ```
  - 影响: 文档声称 workflow target，测试断言 editorial target——误导后续维护者对 P2 编译目标的理解（行为已变、头注释未更新）。
  - 建议: 更新模块 docstring 为 target=editorial（editorial_skills 构造器路径）。
  - 置信度: 高

- [AUD-22621] [Suggestion] 测试方法与局部 provider 缺类型注解；方法内重复导入
  - 位置: src/features/ai_harness/tests/test_p2_wiring.py:173-323
  - 证据: 各 test 方法无 `-> None`；`def provider(asset_ids, kind):` 无注音；:176、:226、:251、:288 在方法内重复导入顶部已导入的 `SkillCompiler` 等。
  - 影响: 与项目强类型约束不一致；局部重复导入掩盖顶部导入的存在。
  - 建议: 补注解；将 `HarnessRuntime`/`LegacyHarnessAdapter` 提升到模块级导入后删除方法内重复导入。
  - 置信度: 高

### 通过项
- 线程安全 / GPU 资源 / GLSL·HDR / 信号防递归：不涉及。
- 注册契约：5 个官方 skill + P2 共 6 个（:47-53）、重复注册幂等不翻倍（:55-58）、enabled（:60-62）有效。
- catalog：P2 卡可见（:71-81）、P2_SKILL_META 分类/触发示例（:83-94）、prereq 就绪双向（有 transcript+模型 ready :96-105 / 无证据 not ready :107-112）有效。
- 输入验证（路由）：中文意图→auto_caption（:121-124）、配音+语言抽取（:126-131）、resolver 校验（:133-138）有效。
- editorial invoke 路径端到端：成功产 subtitle_draft（:173-220）、无证据降级 NO_TRANSCRIPT + with_warnings（:222-246）、localization 产 text_document + NO_TTS_MEDIA 诊断（:248-284）、导入 TTS 生成 media_generation_ref 且无降级诊断（:286-323）覆盖完整、断言具体。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 1 | AUD-22600 |
| Suggestion | 20 | AUD-22601–AUD-22621（除 22600 外全部） |

主题分布：测试声称与实际断言脱节（空转/名不符实/未验证缓存与截断语义）6 项（AUD-22600/22601/22610/22611/22615/22616）；死代码/死导入 4 项（22604/22609/22618 及 22600 内含）；测试隔离 1 项（22602）；弱防线 2 项（22606/22608）；注解/文档质量 8 项（22603/22605/22607/22612/22613/22614/22617/22619/22620/22621 中注解类）。

无线程安全、GPU 资源、GLSL/HDR、信号防递归类问题（本批均为纯逻辑测试，不涉及渲染管线）。
