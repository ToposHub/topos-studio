# batch-142 审查报告 — src/features/edit/ai_editing（provider/release 系列，8 文件，1,463 行）

编号区间：AUD-14200–AUD-14299。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。
本批次全部为纯 Python 逻辑/合同层（无 OpenGL、无 GLSL、无 Qt 控件），GPU 资源生命周期、GLSL·HDR 正确性、信号防递归三个维度整体不适用（各文件通过项中注明 N/A）。

汇总：Critical 0 / Major 4 / Suggestion 8。

---

## src/features/edit/ai_editing/provider_plugin.py (行数: 241)

### 发现

- [AUD-14201] [Suggestion] `AnalysisPluginHost` 自身状态与其委托的 `AnalysisService` 无锁保护，与线程安全的 `PluginRegistry` 保护级别不一致
  - 位置: src/features/edit/ai_editing/provider_plugin.py:133
  - 证据:
    ```python
    self.analysis_service = analysis_service
    self.plugin_registry = plugin_registry or PluginRegistry()
    self._bindings: dict[str, tuple[ProviderDescriptor, AnalysisProvider]] = {}
    ```
    （对照 src/features/ai_harness/plugin_runtime.py:401 `self._lock = threading.RLock()`；src/features/edit/ai_editing/analysis_service.py:190 `self._providers: dict[str, AnalysisProvider] = {}` 亦无锁）
  - 影响: `register/activate/deactivate` 对 `_bindings` 的读改写与 `analysis_service.register/unregister` 的字典写均无同步。当前调用方（composition root / `__init__.py` 导出）未见跨线程调用，属潜在风险而非现实缺陷；若未来从 worker 线程注册 provider，会出现绑定表与 registry/服务状态漂移（activate 成功但 service 未注册等半完成状态）。
  - 建议: 与 `PluginRegistry` 对齐——在 host 层加 `threading.RLock` 包裹 `_bindings` 变更与 service 注册/注销的组合操作，或在文档中明确限定单线程（UI/composition 线程）使用。
  - 置信度: 中（无当前跨线程调用方，为防御性/一致性建议）

### 通过项
- GPU 资源生命周期：N/A（无 GL 资源）
- GLSL·HDR 正确性：N/A
- 信号防递归：N/A（无 Qt 信号）
- 状态提交一致性：N/A（无 undo/revision/持久化）；activate→service.register / deactivate→service.unregister 的次序与失败短路（registry 失败即返回 False，不再动 service）一致
- 异常与取消路径：插件 `activate()` 异常由 `PluginRegistry.activate` 内部 try/except 隔离（plugin_runtime.py:542-544）并标记 FAILED，host 不会被击穿；内置 provider 防覆盖守卫（159-165 行）fail-closed 返回 False 而非抛异常
- 输入验证：`ProviderDescriptor.__post_init__` 对 provider_id 正则、kind、data_policy 白名单校验完整；`descriptor.provider_id != provider_id` 拒绝不一致（156-157）；构造的 `PluginManifest`/`PluginCapability` 复用 plugin_runtime 的校验（含 `declared_hash` 为空即通过 `verify_declared_hash`，语义已核实）
- 性能模式：无热点；`list_descriptors` 排序开销可忽略
- 类型注解：完整

---

## src/features/edit/ai_editing/provider_profiles.py (行数: 12)

### 发现

（无——通过，无逻辑）

### 通过项
- 通过（无逻辑）：兼容 shim，将本模块条目替换为共享实现 `src.shared.ai_models.generation.provider_profiles` 的 `sys.modules` 项，等价导出其全部公开与私有名称（`AiProviderProfile`、`ProfileRegistry`、`CredentialStore`、`DataPolicy`、`ProviderLimits` 等）
- 已核实共享实现只向上导入 `src.shared.ai_models.generation.user_ai_config`，不回导 edit 包，无循环导入；10+ 历史调用方（`model_router.py`、`llm_variant_planner.py`、`http_generation_provider.py`、`ai_preferences_page.py` 等）经此路径导入均能解析
- 备注（不计发现）：模块身份交换后类的 `__module__` 指向 shared 路径，若未来对该包对象做 pickle-by-QN 或 `importlib.reload` 需注意；当前持久化走 JSON（`save_user_ai_config`），不受影响

---

## src/features/edit/ai_editing/provider_resume.py (行数: 271)

### 发现

- [AUD-14202] [Major] `make_fingerprint` 用 `default=str` 序列化输入，非 JSON 原生类型按 `repr` 入哈希，重启后同一逻辑请求指纹不稳定，破坏 OS-09 幂等去重
  - 位置: src/features/edit/ai_editing/provider_resume.py:89-95
  - 证据:
    ```python
    input_hash = hashlib.sha256(
        json.dumps(input_payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    params_hash = hashlib.sha256(
        json.dumps(params, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    ```
  - 影响: 无自定义 `__repr__` 的对象会哈希到含内存地址的 `<obj at 0x...>`；set/dict 视图类容器的 `repr` 顺序跨进程不稳定（PYTHONHASHSEED）。重启后同一次逻辑请求算出不同 fingerprint → `reconcile_inflight` 的 SKIP 分支（209 行）判定失败 → 对 provider 重复发起调用，正是本模块 docstring 声称要防止的 double-charge/double-insert。当前仅测试调用（tests/ai/test_w6_w9_contracts.py，输入全为 JSON 原生类型），故为合同级风险。
  - 建议: 在哈希前显式规范化 payload（拒绝不可 JSON 化类型并抛 `ValueError`，或提供注册式 canonicalizer：set→sorted list、路径/时间→规范字符串），移除 `default=str` 这一静默兜底。
  - 置信度: 中（缺陷客观存在；是否触发取决于调用方传入的 payload 类型，当前无生产调用方）

- [AUD-14203] [Suggestion] `EXT_PENDING`（默认状态）落入"未知状态"else 分支，reason 误标已定义状态；`RECONCILE_FAIL` 已定义导出但无任何产生路径
  - 位置: src/features/edit/ai_editing/provider_resume.py:243-248（对照 113 行 `EXT_PENDING = "pending"`、34 行 `RECONCILE_FAIL = "fail"`）
  - 证据:
    ```python
    else:
        decisions.append(ReconcileDecision(
            job_id=record.job_id,
            decision=RECONCILE_ASK_USER,
            reason=f"未知状态 {record.state!r}，需用户确认。",
        ))
    ```
  - 影响: `ExternalJobRecord.state` 有默认值 `EXT_PENDING`，且 `__post_init__` 校验后 else 分支实际只可能捕获 `pending`。崩溃恢复时"已发出但无结果"恰是最常见的在途状态，却被报告为"未知状态"，误导排查；docstring 决策表也未覆盖 pending。行为本身 fail-closed（ask_user）尚可接受。`RECONCILE_FAIL` 在 `ALL_RECONCILE_DECISIONS` 中对外承诺，但 `reconcile_inflight` 永不产出，集成方据此分支会写成死代码。
  - 建议: 为 `EXT_PENDING` 增加显式分支（语义上应与 UNKNOWN 同走 ask_user 或按 `provider_job_ref` 是否存在细分），修正 reason 文案；删除 `RECONCILE_FAIL` 或补充产出条件并在 docstring 决策表中写明。
  - 置信度: 高

### 通过项
- 线程安全：纯 frozen dataclass + 无状态函数，无可变共享状态
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：N/A
- 状态提交一致性：`to_dict` 往返字段完整；fingerprint 属性基于同一 canonical JSON 稳定
- 异常与取消路径：`ExternalJobRecord`/`ReconcileDecision` 构造期校验状态/决策白名单，未知值立即 `ValueError`，无宽泛 except
- 输入验证：`job_id` 非空校验（140-141）；fingerprint 字段本身未校验空 provider_id/model_id（轻微，随 AUD-14202 一并加固即可）
- 性能模式：无
- 类型注解：完整

---

## src/features/edit/ai_editing/quality_gate.py (行数: 247)

### 发现

- [AUD-14204] [Suggestion] 硬门槛/软 KPI 输入未做范围校验，负计数与越界比率可令硬门槛 fail-open
  - 位置: src/features/edit/ai_editing/quality_gate.py:198-213（对照 106-111 行 `GateMetric.met`）
  - 证据:
    ```python
    for name, value, target, unit, higher_is_better in hard_specs:
        m = GateMetric(
            name=name, value=float(value), target=float(target),
            hard_gate=True, higher_is_better=higher_is_better, unit=unit,
        )
    ```
    ```python
    @property
    def met(self) -> bool:
        if self.higher_is_better:
            return self.value >= self.target
        return self.value <= self.target
    ```
  - 影响: `unconfirmed_writes=-3` 满足 `-3 <= 0` 判为达标，安全硬门槛（必须=0）对畸形输入放行；`evidence_locatable_rate=2.5` 或 NaN 也会被当作合法达标值记录。当前生产入口 `eval_runner.py:107` 传默认 `QualityGateInput()` 不触发，但该门是 §12.2 硬门槛判定层，应自证输入可信。
  - 建议: 在 `QualityGateInput.__post_init__`（或 evaluate 入口）校验计数 >= 0、rate ∈ [0.0, 1.0]、成本/时长 >= 0，非法即抛 `ValueError`。
  - 置信度: 高（逻辑客观；现无触发路径）

### 通过项
- 线程安全：frozen dataclass + 纯函数
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：N/A
- 状态提交一致性：`passed` 仅由硬门槛诊断推导（233 行 `len(diags) == 0`），与 `hard_gate_failures` 属性一致；软 KPI 不影响 passed，符合 §12.2
- 异常与取消路径：无 except，构造期校验 fail-fast
- 输入验证：除上述范围校验外，`make_diagnostic(..., detail=...)` 模板变量与 `MESSAGE_TEMPLATES[QUALITY_GATE_FAILED]` 的 `{detail}` 已核实匹配（diagnostics.py:239-242）
- 性能模式：无
- 类型注解：完整

---

## src/features/edit/ai_editing/release_evidence.py (行数: 240)

### 发现

- [AUD-14205] [Major] `_safe_metadata` 只做 key 黑名单 + 值前缀路径过滤，字符串值中段包含 secret/绝对路径不被清除，且未复用同包已有的 `redact_text`
  - 位置: src/features/edit/ai_editing/release_evidence.py:27-35
  - 证据:
    ```python
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and _ABSOLUTE_PATH.match(value):
            continue
        safe[key[:64]] = value if not isinstance(value, str) else value[:200]
    ```
    （`_ABSOLUTE_PATH` 锚定行首：18 行 `re.compile(r"^(?:/|[A-Za-z]:[\\/]|~[\\/])")`；对照同包 release_test_matrix.py:47 `redact_text(self.error_message)`）
  - 影响: `metadata={"error": "render failed at /Users/testuser/proj/x.mov (bearer sk-xxxxxxxx)"}` —— key "error" 不在黑名单、值不以 `/` 开头，路径与 secret 原样进入 `TelemetryEvent` 并随 `export()` 导出。这与本文件 docstring"不含用户标识、prompt、素材或路径"的承诺及 quality_gate 的 `path_leaks`/`secret_leaks` 硬门槛目标冲突；同包 `MatrixCase.to_dict` 已示范用 `redact_text` 做值级脱敏，此处遗漏。当前 `TelemetryCollector` 仅测试引用、无生产调用方，属明确风险而非现实泄露。
  - 建议: 对通过类型检查的 str 值先过 `privacy_guard.redact_text`（或至少用非锚定的 `_ABSOLUTE_PATH` 与 secret 模式匹配值内容）再截断；key 黑名单保留为第一道。
  - 置信度: 高（过滤缺口客观存在；无生产触发方）

- [AUD-14206] [Suggestion] `TelemetryCollector.emit` 超限用 `list.pop(0)` 淘汰，O(n) 每事件
  - 位置: src/features/edit/ai_editing/release_evidence.py:95-96
  - 证据:
    ```python
    if len(self._events) > self._max_events:
        self._events.pop(0)
    ```
  - 影响: `max_events` 默认 1000，环满后每次 emit 搬移整个列表；遥测本应零开销，持续高频 emit 时形成无谓 CPU 开销（性能模式维度）。
  - 建议: 改用 `collections.deque(maxlen=...)`，`events()`/`export()` 用 `tuple(self._events)` 快照。
  - 置信度: 高

- [AUD-14207] [Suggestion] `TelemetryCollector` 无锁：`set_opt_in` 的 `clear()`、`emit` 的 append/pop 与 `export()` 迭代并发时无保护
  - 位置: src/features/edit/ai_editing/release_evidence.py:91-97、103
  - 证据:
    ```python
    self._events.append(TelemetryEvent(...))
    if len(self._events) > self._max_events:
        self._events.pop(0)
    ```
    ```python
    def export(self) -> dict[str, Any]:
        return {"opted_in": self._opt_in, "events": [event.to_dict() for event in self._events]}
    ```
  - 影响: 若 emit 来自 worker/渲染侧线程而 UI 线程同时 `export()`，迭代中 `pop(0)` 会抛 `RuntimeError: list changed size during iteration`；`set_opt_in` 关闭时 `clear()` 与并发 emit 交错可能残留 opt-out 后的事件。当前无多线程调用方（仅测试），为防御性发现。
  - 建议: 加 `threading.Lock` 保护 append/淘汰/clear/快照（快照式导出，避免持锁调用 `to_dict` 亦可）。
  - 置信度: 中（无当前跨线程调用方）

### 通过项
- 线程安全：见 AUD-14207（其余类为 frozen dataclass）
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：N/A
- 状态提交一致性：`set_opt_in(False)` 即 `clear()`，符合"默认关闭、可删除"承诺；`ReleaseEvidenceBundle.blockers` 全量 fail-closed 条件与 `releasable` 一致
- 异常与取消路径：`GrayRolloutConfig.__post_init__` 校验 percentage∈[0,100] 与 channel 白名单
- 输入验证：`GrayRolloutConfig.enabled_for` 空 cohort_key → False、percentage=100 短路、0 → False（128-132 行分桶确定性 sha256+salt，已核实）；`TelemetryEvent` 字段截断（80/40/100/200）合理
- 性能模式：除 AUD-14206 外无问题
- 类型注解：完整

---

## src/features/edit/ai_editing/release_gate.py (行数: 168)

### 发现

- [AUD-14208] [Major] 首 token 硬门槛在指标为 None 时被静默跳过，"runs>0 但无一次首 token"可空过发布门，与自述"No data is a failure"矛盾
  - 位置: src/features/edit/ai_editing/release_gate.py:103-107
  - 证据:
    ```python
    token = summary.get("avg_first_token_latency_ms")
    if token is not None:
        ok = float(token) <= max_first_token_ms
    ```
    （对照数据源 src/features/edit/ai_editing/agent_metrics.py:229-232：`def _avg_ms(values): if not values: return None`——无任何 run 记录到首 token 时该键为 None）
  - 影响: `total_runs=10` 且全部 run 在流式开始前失败（或控制器从未接线 `record_first_token`）时，`avg_first_token_latency_ms=None` → 硬门槛检查整体缺席，`passed=True`。模块 docstring 明言"Latency gates are hard / No data is a failure"，却只对 `total_runs` 落实 fail-closed；首 token 延迟恰是 §9 表中不可豁免的硬指标。cancel 的 None 跳过合理（无取消发生），但首 token 的 None 在有 run 的前提下不是"从未观测到的可选指标"。
  - 建议: 对 first-token 单独处理：`total_runs>0` 而 `avg_first_token_latency_ms is None` 时追加失败项（如 "首 token 延迟无数据，无法证明门槛"），或将该项并入 has_data 检查；cancel 保持现状。
  - 置信度: 高

- [AUD-14209] [Suggestion] 与同包 `eval_runner` 存在同名异构的 `ReleaseGateResult` / `evaluate_release_gate`，易误导入
  - 位置: src/features/edit/ai_editing/release_gate.py:58-59、76-82
  - 证据:
    ```python
    @dataclass(frozen=True)
    class ReleaseGateResult:
        passed: bool
        checks: tuple[GateCheck, ...] = ()
    ```
    （对照 src/features/edit/ai_editing/eval_runner.py:49 `class ReleaseGateResult:`（字段 `allowed/reasons/...`）与 eval_runner.py:130 `def evaluate_release_gate(trace: EvalTrace, ...)`）
  - 影响: 同一包内两个同名类、两个同名函数语义不同（性能门槛 vs eval-trace 门槛），`from src.features.edit.ai_editing import ...` 时极易拿错，且二者字段不同（passed/ checks vs allowed/reasons），错配在序列化/展示层才暴露。
  - 建议: 重命名其一（如本模块改 `AgentPerfGateResult`/`evaluate_agent_perf_gate`，或 eval_runner 侧改名），或在两模块 docstring 交叉指明分工。
  - 置信度: 高

### 通过项
- 线程安全：纯函数 + frozen dataclass
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：N/A
- 状态提交一致性：`passed = not failures` 与 checks/failures/warnings 分列，`to_dict` 可解释性完整
- 异常与取消路径：无宽泛 except；`float()` 对畸形值会显式抛错而非误判通过
- 输入验证：`total_runs` 经 `int(... or 0)` 防 None/缺键；duration 为软告警不误伤
- 性能模式：无
- 类型注解：完整

---

## src/features/edit/ai_editing/release_manifest.py (行数: 137)

### 发现

- [AUD-14210] [Suggestion] `default_flags` 传入时不校验键集/值类型；自动填充路径隐式依赖两常量集不漂移
  - 位置: src/features/edit/ai_editing/release_manifest.py:27-37
  - 证据:
    ```python
    if self.default_flags is None:
        object.__setattr__(
            self,
            "default_flags",
            {name: DEFAULT_FEATURE_FLAGS[name] for name in COMMERCIAL_AUTOMATION_FLAGS},
        )
    ```
  - 影响: 调用方传入 `default_flags={"agent_auto_mode": "yes"}` 或任意陌生键都能构造成功，`to_dict()` 原样外发，manifest 的 flag 集合不再收敛于 `COMMERCIAL_AUTOMATION_FLAGS`；自动填充分支在 `COMMERCIAL_AUTOMATION_FLAGS` 新增而 `DEFAULT_FEATURE_FLAGS` 漏配时会 `KeyError`（当前已核实两集合一致，feature_flags.py:8-61）。
  - 建议: `__post_init__` 校验键集 ⊆ COMMERCIAL_AUTOMATION_FLAGS、值为 bool，并对自动填充用 `DEFAULT_FEATURE_FLAGS.get(name, False)` 或在启动诊断中检测两常量漂移。
  - 置信度: 高（现状一致、属预防性加固）

- [AUD-14211] [Suggestion] `feature_flags` 参数缺 `is_enabled` 时静默回退默认值，无任何诊断
  - 位置: src/features/edit/ai_editing/release_manifest.py:112-119
  - 证据:
    ```python
    flags = {
        name: bool(
            feature_flags.is_enabled(name) if feature_flags is not None
            and callable(getattr(feature_flags, "is_enabled", None))
            else DEFAULT_FEATURE_FLAGS[name]
        )
        for name in COMMERCIAL_AUTOMATION_FLAGS
    }
    ```
  - 影响: 调用方传入了形态不符的 flags 对象（如数据类快照而非 FeatureFlags 服务）时不报错，manifest 记录的是默认值而非真实运行时 flag 状态——发布清单与实际发布物配置悄悄背离。
  - 建议: `feature_flags is not None` 但缺 `is_enabled` 时抛 `TypeError`（或至少在 manifest metadata 里记录 fallback 事实）。
  - 置信度: 高

### 通过项
- 线程安全：frozen dataclass + 纯构造函数
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：N/A
- 状态提交一致性：`release_blockers` 覆盖 quality gate / 测试报告 / 矩阵 / 商用审计 / 面板验收 / stable 回滚版本，fail-closed 且与 `releasable` 一致；`build_release_manifest` 对 provider_id 去重排序、known_issues 截断 300、report_id 截断 200
- 异常与取消路径：channel/version 非法即 `ValueError`，无宽泛 except
- 输入验证：除上述两条外无问题（providers 缺 provider_id 属性安全跳过，110 行 getattr+真值过滤已核实）
- 性能模式：无
- 类型注解：完整

---

## src/features/edit/ai_editing/release_test_matrix.py (行数: 147)

### 发现

- [AUD-14212] [Major] `min_pass_rate` 参数被 `not failed` 条件完全蕴含，<1.0 的取值无效（死参数），与 docstring"pass rate 阈值"承诺矛盾
  - 位置: src/features/edit/ai_editing/release_test_matrix.py:124
  - 证据:
    ```python
    matrix_passed = bool(normalized) and rate >= min_pass_rate and not missing and not failed
    ```
    （116-120 行：`passed = sum(1 for case in normalized if case.passed)`、`failed = tuple(case.name for case in normalized if not case.passed)`）
  - 影响: 只要存在任一 failed case，`not failed` 即为 False，矩阵必失败；反之 `not failed` 成立时 rate 恒为 1.0，`rate >= min_pass_rate` 对任何 `min_pass_rate<=1.0` 恒真。即参数文档暗示的"允许 80% 通过率"语义不存在——调用方设 `min_pass_rate=0.8` 期望容忍个别失败时仍会被拒（fail-closed 方向，但违背参数契约且无法区分"按阈值拒绝"与"按零容忍拒绝"）。当前唯一生产调用方 `media_probe.py:174` 与 `eval_runner.py:111` 均传 1.0，未触发。
  - 建议: 二选一——去掉 `not failed` 让 `rate >= min_pass_rate` 真正生效（失败明细仍列 `failed_cases`）；或删除 `min_pass_rate` 参数并在 docstring 明示零容忍。改动需同步测试与调用方。
  - 置信度: 高

### 通过项
- 线程安全：frozen dataclass + 纯函数
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：N/A
- 状态提交一致性：`to_dict` 全量导出含 blockers 类字段；synthetic 案例的 real-evidence 缺口总是上报（121-122），`require_real_evidence` 只收紧不放松，fail-closed
- 异常与取消路径：`min_pass_rate` 越界抛 `ValueError`（113-114）；空案例集 `rate=0.0` 且 `bool(normalized)=False` 必失败，不空过
- 输入验证：`find_missing_dimensions` 对未知字段名按缺失处理（getattr 缺省 ""），拼写错误只会收紧门槛不会放松
- 性能模式：`MatrixCase.to_dict` 用 `redact_text` 做值级脱敏（隐私正确，见 AUD-14205 对照），截断合理
- 类型注解：完整

---

## 附注（跨文件核实记录）

- `AnalysisService.register/unregister/available_providers`（analysis_service.py:207-216）为无锁字典写，AUD-14201 的风险评估以此为准。
- `PluginRegistry.register/activate`（plugin_runtime.py:403-458/511-558）拒绝重复注册、要求 ENABLED/DEGRADED 才能 activate、插件异常被隔离为 FAILED——`AnalysisPluginHost` 的返回值语义与之吻合。
- `agent_metrics._avg_ms`（agent_metrics.py:229-232）空列表返回 None，是 AUD-14208 的直接数据源。
- `privacy_guard.redact_text`（privacy_guard.py:29-34）具备 secret/路径值级脱敏能力，是 AUD-14205 建议的现成工具。
- `feature_flags.COMMERCIAL_AUTOMATION_FLAGS` 三键均存在于 `DEFAULT_FEATURE_FLAGS`（feature_flags.py:8-61），AUD-14210 目前不触发。
