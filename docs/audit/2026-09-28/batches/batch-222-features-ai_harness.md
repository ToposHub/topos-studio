# batch-222 审查报告 — src/features/ai_harness//

批次：batch-222 [P2]；编号区间 AUD-22200–AUD-22299；审查日期 2026-09-28。
8 个文件全部 Read 通读。本批次均为 Qt/GPU 无关的纯 Python 模块（插件治理、
沙箱、状态投影、结果卡状态模型），不涉及 OpenGL/GLSL/HDR 与 Qt 信号路径。

统计：Critical 0；Major 4；Suggestion 8。

---

## src/features/ai_harness/plugin_rollout.py (行数: 286)

### 发现

- [AUD-22200] [Suggestion] PluginRolloutRouter 完全无锁，register/remove 与 target_for_new_run/to_dict 存在 check-then-act 与迭代竞争
  - 位置: src/features/ai_harness/plugin_rollout.py:239-243（另见 245-246、260-272）
  - 证据:
    ```python
    def register(self, flag: PluginRolloutFlag) -> bool:
        if flag.plugin_id in self._flags:
            return False
        self._flags[flag.plugin_id] = flag
        return True
    ```
    以及 `to_dict()` 中 `for flag in sorted(self._flags.values(), ...)`（line 270）在无锁状态下迭代同一 dict。
  - 影响: 同文件中 `PluginRolloutLedger` 明确自述 "Bounded, thread-safe" 并用 RLock，而 Router 无任何同步。若注册线程与查询/序列化线程并发：两个线程可同时通过 `in` 检查导致重复注册被静默接受；`to_dict()` 迭代期间 dict 变更会抛 `RuntimeError: dictionary changed size during iteration`。当前 src/ 内未见跨线程使用点（仅 `__init__.py` 再导出），故为加固项而非现行缺陷。
  - 建议: 与 Ledger 一致引入 `threading.RLock` 包裹 register/remove/target_for_new_run/to_dict；`register` 的存在性检查与写入放入同一临界区。
  - 置信度: 中

### 通过项
- 线程安全：PluginRolloutLedger 的 record/samples/metrics 在 RLock 临界区内完成，边界裁剪 `del self._samples[...]` 正确
- 输入验证：PluginRunSample/PluginRolloutPolicy/PluginRolloutFlag 的 `__post_init__` 对区间（percentage 0–100、rate 0–1、正数上限）校验完整
- 性能模式：p95 用排序+索引（ceil(n*0.95)-1，空列表回退 0），ledger 有界（默认 10,000）避免无界增长
- GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径：不适用（纯数据模块，无 GPU、UI、持久化写路径）
- 类型注解：完整（含返回类型）

---

## src/features/ai_harness/plugin_runtime.py (行数: 1078)

### 发现

- [AUD-22201] [Major] PluginManifest/PluginCapability 持有的 Mapping 字段未做防御性拷贝，frozen 契约与 content_hash 完整性可被注册后突变破坏
  - 位置: src/features/ai_harness/plugin_runtime.py:241（metadata）、193-195（input_schema/output_schema/metadata）、265-271 与 277-284
  - 证据:
    ```python
    metadata: Mapping[str, Any] = field(default_factory=dict)
    declared_hash: str = ""
    ...
    @property
    def content_hash(self) -> str:
        payload = self.to_dict(include_hash=False)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    ```
    `__post_init__`（246-271）规范化了 plugin_id/source/capabilities/dependencies，但 `metadata`、每个 capability 的 `input_schema/output_schema/metadata` 均保留调用方传入的原对象引用。
  - 影响: manifest 自述 "Immutable, versioned plugin declaration"，且 content_hash/declared_hash/签名（plugin_trust）均覆盖这些字段。调用方在注册后原地修改传入 dict，会使同一 manifest 对象的 `content_hash` 漂移、`verify_declared_hash` 结果随时间变化，破坏哈希/签名完整性边界（注册时校验一次，此后不再检测）。
  - 建议: 在 `__post_init__` 中对 Mapping 字段做 `types.MappingProxyType(dict(...))` 或深拷贝为不可变映射；capability 同样处理。
  - 置信度: 中

- [AUD-22202] [Major] activate() 与 register(replace_existing=True) 竞态：激活窗口内条目可被替换，新条目被标记 ACTIVE 但从未执行其 activate，且 host_io/policy 绑定来自旧条目
  - 位置: src/features/ai_harness/plugin_runtime.py:511-519、529-540、545-552
  - 证据:
    ```python
        with self._lock:
            entry = self._entries.get(plugin_id)
            if entry is None or entry.state not in {
                PluginState.ENABLED, PluginState.DEGRADED,
            }:
                return False
            plugin = entry.plugin
        ...
        try:
            bound_broker = PolicyBoundResourceBroker(
                self._resource_broker, plugin_id, entry.policy,
            )
        ...
        with self._lock:
            entry = self._entries.get(plugin_id)
            if entry is None:
                return False
            entry.state = PluginState.ACTIVE if health.status != "failed" else PluginState.FAILED
            entry.health = health
            entry.host_io = host_io
    ```
  - 影响: `plugin.activate()` 在锁外执行（必要的），但期间旧条目状态仍为 ENABLED，另一线程 `register(replace_existing=True)`（434-448 仅拒绝 ACTIVE）可整体替换条目。随后 activate 收尾把**新**条目标为 ACTIVE：新版本插件对象从未 activate 却可被 invoke_tool 调用；写入的 `host_io`/`entry.error` 基于旧 `entry.policy` 构建的 broker；旧插件实例被孤儿化且不 deactivate。
  - 建议: 收尾临界区内比对 `entry.plugin is plugin`（或记录注册代次/entry id），不一致时不落 ACTIVE 并对旧实例执行 deactivate 清理；或替换期间给条目加"transitioning"互斥。
  - 置信度: 中

- [AUD-22203] [Suggestion] invoke_tool 成功路径原样返回插件 Mapping、未统一注入 ok=True，与错误路径恒含 ok=False 的契约不对称
  - 位置: src/features/ai_harness/plugin_runtime.py:955（对照 745-749、961-965）
  - 证据:
    ```python
            self._emit(plugin_id, "plugin.tool_completed", self._invocation_payload(
                context,
                code="PLUGIN_OK",
                ...
            ))
            return output
    ```
    错误路径统一 `{"ok": False, "error": ..., "diagnostics": [...]}`；调用方 `src/features/edit/ai_editing/edit_capability_plugin.py:171` 以 `if result.get("ok") is not True or ...` 判定成败。
  - 影响: 插件 handler 若忘记在返回 Mapping 中带 `"ok": True`，宿主已发 `plugin.tool_completed` 事件（trace 记为成功），而调用方按失败处理——遥测与语义不一致，且成败判定完全依赖插件自觉。
  - 建议: 成功路径统一 `output.setdefault("ok", True)`（或包装 `{"ok": True, "result": output}`），并在 PluginToolHandler 协议文档中写明。
  - 置信度: 高

- [AUD-22204] [Suggestion] in_process 旧式（不接受 context 的）handler 无超时看门狗，卡死调用永久占用 BoundedSemaphore，插件持续 PLUGIN_BUSY 直至重启
  - 位置: src/features/ai_harness/plugin_runtime.py:840-849、966-967（对照 716-725）
  - 证据:
    ```python
        if not semaphore.acquire(blocking=False):
            return {
                "ok": False,
                "error": "plugin tool concurrency limit reached",
                ...
    ...
        finally:
            semaphore.release()
    ```
    docstring（719-725）已声明"never claims that Python code was forcibly terminated"，deadline 对旧式 handler 只做事后报告（900-918）。
  - 影响: 旧式 handler 死循环/死锁时 `semaphore.release()` 永不执行（max_concurrent_calls 默认 1），该插件此后所有调用立即返回 PLUGIN_BUSY，无自愈路径；与 process 隔离模式不同，宿主无任何检测或熔断。
  - 建议: 至少为 in_process 调用增加可选的看门狗（监视线程 + 超时后 degrade 并重建 semaphore/条目），或在文档与注册策略中强制不可信插件使用 process 隔离。
  - 置信度: 高（设计已声明，列为风险而非违规）

### 通过项
- 线程安全（除 AUD-22202 竞态外）：registry 全部状态变更在 RLock 内；invoke_tool 在锁外调用插件代码、事件 sink 在锁外触发；`_emit` 失败隔离不成为执行后门；invoke_tool 的信号量 acquire/try-finally-release 配对正确（acquire 与 try 之间无提前 return）
- 异常与取消路径：PluginInvocationCancelled/TimedOut/ProcessCrashed 分类清晰；cancel_check 抛错按 fail-closed 视为已取消（124-129）；非合作式取消/超时以 *_NONCOOPERATIVE 代码如实上报；TypeError("must return a mapping") 等插件异常被包含且带类型名
- 输入验证：args 必须 Mapping 且 JSON 可序列化（allow_nan=False）；输入/输出字符上限前置检查并发出 tool_rejected；timeout 正数校验；approval 缺失拒绝；process 隔离必须配适配器、required 沙箱必须 process 隔离的注册期检查完备
- 状态提交一致性：restore_project_state 恢复持久状态绝不隐式调用 plugin.activate()，ACTIVE 恢复为 ENABLED 并标记 reactivation_required；版本单调性（同版本/降版替换拒绝）；unregister/replace 拒绝 ACTIVE 条目
- GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归：不适用（无 GPU、无 Qt）
- 性能模式：capability 查找在锁内 next() 短路；active_tool_capabilities 排序保证确定性；快照/list 拷贝后锁外构建
- 类型注解：完整

---

## src/features/ai_harness/plugin_sandbox.py (行数: 419)

### 发现

- [AUD-22205] [Major] 沙箱 worker 继承宿主完整 os.environ，凭据对第三方插件代码可读，违背"插件不得隐式获得凭据"边界
  - 位置: src/features/ai_harness/plugin_sandbox.py:216-223
  - 证据:
    ```python
        env = os.environ.copy()
        prior_pythonpath = env.get("PYTHONPATH", "")
        python_paths: list[str] = [self._project_root]
        for candidate in (*sys.path, *prior_pythonpath.split(os.pathsep)):
            ...
        env["PYTHONPATH"] = os.pathsep.join(python_paths)
    ```
  - 影响: 子进程运行 unpickle 出的第三方插件代码（plugin_sandbox_worker 经 bootstrap 接收 factory）。sandbox-exec/bwrap 可限制文件系统与网络，但环境变量在进程内存中天然可读——宿主 env 中的 API key、token 等全部泄漏给被沙箱的插件，与 plugin_runtime 模块自述 "never receives implicit ... credentials"（1-6 行）矛盾。
  - 建议: 构建最小环境（白名单 PATH/SystemRoot 等必需项 + PYTHONPATH），不从 os.environ 整体复制。
  - 置信度: 中（取决于部署形态，但代码路径确定）

- [AUD-22206] [Suggestion] 沙箱子进程 stdout/stderr 均 DEVNULL，worker 启动失败（退出码 2/3/4 或异常 traceback）只表现为连接 EOF，无法诊断
  - 位置: src/features/ai_harness/plugin_sandbox.py:117-126
  - 证据:
    ```python
            self._process = subprocess.Popen(
                list(self._command),
                cwd=self._cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=(ipc_fd, bootstrap_fd),
            )
    ```
  - 影响: profile 编译失败、PYTHONPATH 错误、bootstrap 校验失败（worker 返回 2/3/4）均无任何日志留痕，现场只能看到 "process exited without a result"（PluginInvocationProcessCrashed），排障成本高。
  - 建议: 将 stderr 重定向到宿主管理的临时文件或环形缓冲（内容仅含 worker 自身 traceback，不涉及用户数据），失败时附加到报告/诊断。
  - 置信度: 高

### 通过项
- 线程安全：launcher/report 为无共享可变状态的构造后只读对象；`_ExternalSandboxProcess` 的 start/terminate/kill/join 与 plugin_isolation 使用面（is_alive/terminate/kill/join）匹配
- 异常与取消路径：start() 失败时杀死半启动进程、关闭 bootstrap 两端后重抛；join 超时静默返回符合 Process.join 语义
- 输入验证：macOS profile 必须以 (version 开头、含 (deny default)、不含 (allow default)；bubblewrap 根不得为 "/" 或空、去重；DarwinSandboxLauncher 可用性同时校验平台与可执行文件存在
- 状态诚实性：detect_plugin_sandbox 对仅"检测到后端"报告 enforced=False，不冒充已加固；validate_sandbox_launcher 强制返回 PluginSandboxReport 类型
- GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性：不适用
- 性能模式：bwrap 使用 --die-with-parent/--new-session/--unshare-*，命令为 argv 列表不经 shell
- 类型注解：完整

---

## src/features/ai_harness/plugin_sandbox_worker.py (行数: 36)

### 通过（无逻辑问题）
- 职责单一的引导壳：从环境变量取 IPC/bootstrap fd（缺失返回 2）、接收 bootstrap dict（类型不符返回 3）、调用 `plugin_isolation._plugin_worker`（返回 0）。
- 异常与取消路径：捕获 KeyError/TypeError/ValueError/OSError/EOFError/PickleError（返回 4）；BrokenPipeError/ConnectionResetError 属 OSError 已覆盖；fd 解析失败的 ValueError 已覆盖。bootstrap 端 recv 后即关闭，无 fd 泄漏；非捕获异常导致非零退出属预期（宿主按 EOF/崩溃处理）。
- 输入验证：isinstance(bootstrap, dict) 检查后才取键。
- 其余维度（GPU/GLSL/HDR/信号/持久化）不适用；类型注解完整。

---

## src/features/ai_harness/plugin_status.py (行数: 371)

### 发现

- [AUD-22207] [Suggestion] can_enable 仅对 DISABLED 为真，而 registry.enable() 允许 FAILED/DEGRADED 状态回 ENABLED，UI 将不提供失败插件的启用入口
  - 位置: src/features/ai_harness/plugin_status.py:354（对照 src/features/ai_harness/plugin_runtime.py:984-993）
  - 证据:
    ```python
            can_enable=record.state == PluginState.DISABLED,
            can_activate=(
                api_compatible
                and record.state in {PluginState.ENABLED, PluginState.DEGRADED}
            ),
    ```
    而 `_set_state`（plugin_runtime.py:984-993）仅在 ACTIVE 或同态时拒绝，FAILED→ENABLED 合法。
  - 影响: FAILED 插件 can_enable=False 且 can_activate=False，UI 上既无"启用"也无"激活"路径，但 registry 层明明支持 FAILED→ENABLED→ACTIVE 恢复；失败插件在界面上成为死端，只能靠代码路径恢复。
  - 建议: can_enable 扩展为 `record.state in {DISABLED, FAILED, DEGRADED}`（DEGRADED 已可 activate，可保持仅 DISABLED/FAILED），与 `_set_state` 实际允许的迁移集合对齐。
  - 置信度: 高

### 通过项
- 线程安全：journal 的 record 在锁内构建事件/推进序号/快照订阅者元组，锁外分发且逐个 try/except 隔离；deque(maxlen) 天然有界
- 输入验证：max_events 正数；recent 的 limit<=0 返回空；latest_failure 先按 plugin 过滤再取前 20（语义为"该插件最近 20 条"而非"全局最近 20 条"，实现正确）
- 异常与取消路径：订阅者异常不影响 record；sanitize_for_storage 在入队前脱敏
- 性能模式：投影为纯函数、无共享可变状态；content_hash 每次投影重算开销可接受（插件数量级小）
- GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性：不适用（只读视图层，registry 为唯一事实源）
- 类型注解：完整

---

## src/features/ai_harness/plugin_trace.py (行数: 87)

### 发现

- [AUD-22208] [Suggestion] 会话解析器抛错被宽泛 except 静默降级为 "default" 会话，trace 事件可能被错误归属且无任何诊断
  - 位置: src/features/ai_harness/plugin_trace.py:56-59
  - 证据:
    ```python
        try:
            session_id = str(self._session_id_for_run(run_id) or "default")
        except Exception:
            session_id = "default"
    ```
  - 影响: 解析器（查 run→session 映射）出现领域错误（如持久层 IO/键错误）时，插件工具事件被静默写入 "default" 会话的 run trace——用户在自己的 Run Drawer 里看不到这次工具调用，且无法从日志发现归属错误。与 .clinerules "不得宽泛 except 掩盖领域错误" 相悖（此处无 noqa 注释说明）。
  - 建议: 捕获面收窄到预期异常类型，或至少在降级时向 journal/trace 记录一条低代价的诊断事件；维持 "default" 兜底可以保留。
  - 置信度: 中

### 通过项
- 线程安全：adapter 仅持有一个闭包引用，attach/close 幂等（close 先取走 unsubscribe 再调用），无共享可变状态
- 状态提交一致性：无 run_id 的注册/激活事件留在 journal 不投影；事件 id `plugin-event:{plugin_id}:{sequence}` 依赖 journal 全局序号，无碰撞；kind 映射（tool_started/完成族 → run.tool_*）保留 plugin_event_kind 原始语义
- 异常与取消路径：记录失败由 RunTraceStore.append 的契约承担；本模块自身无吞错路径（除上述发现）
- 输入验证：run_id 取值 strip 后判空；kind/outcome 默认值经 setdefault 不覆盖既有字段
- GPU/GLSL/HDR/信号防递归/性能：不适用；类型注解完整

---

## src/features/ai_harness/plugin_trust.py (行数: 82)

### 发现

- [AUD-22209] [Suggestion] HmacPluginSignatureVerifier 构造对非 str/bytes 键执行 bytes(value)，整数会静默生成全零密钥
  - 位置: src/features/ai_harness/plugin_trust.py:59-62
  - 证据:
    ```python
        self._keys = {
            str(key_id): value.encode("utf-8") if isinstance(value, str) else bytes(value)
            for key_id, value in keys.items()
        }
    ```
  - 影响: `keys={"k": 5}` 时 `bytes(5)` 产生 `b"\x00"*5`——一个确定已知、强度为零的"密钥"被静默接受，后续签名校验形同虚设（攻击者若能猜到此配置错误即可伪造 manifest 签名）。keys 为宿主注入的可信配置，故降为 Suggestion；但这是安全边界上的输入验证缺口。
  - 建议: 显式 `if not isinstance(value, (bytes, bytearray, str)): raise TypeError(...)`；bytearray 可 `bytes(value)` 归一。
  - 置信度: 高

### 通过项
- 输入验证/安全：verify 对缺失签名、缺失 key_id、无 verifier 分别给出可区分的失败码且 fail-closed；verify 抛错视为无效签名（noqa 注释说明理由充分）
- 异常与取消路径：compare_digest 恒时比较；B64 解码仅作用于自产签名串（verify 路径不解码外部输入，只做字符串比较）
- 线程安全：构造后只读 dict
- GPU/GLSL/HDR/信号/持久化：不适用；类型注解完整（Protocol 定义清晰，密钥不落入 manifest/project 文件的设计在注释中明确）

---

## src/features/ai_harness/result_card.py (行数: 320)

### 发现

- [AUD-22210] [Major] generated_media 未审批即启用 import_to_pool/place_on_timeline，违反模块自述红线"审批前所有应用/导出类按钮禁用"
  - 位置: src/features/ai_harness/result_card.py:190-200（对照 8-9、139 行红线声明）
  - 证据:
    ```python
    - **不把未审批 proposal 显示成已应用**：``ResultCardState.applied`` 严格区分；
      审批前所有"应用/导出"类按钮禁用。（模块 docstring，8-9 行）
    ...
    if kind == "generated_media":
        actions.append(ResultAction(
            kind="locate_in_pool", label="查看生成素材箱", enabled=True,
        ))
        if not applied:
            actions.append(ResultAction(
                kind="import_to_pool", label="导入媒体池", enabled=True,
            ))
            actions.append(ResultAction(
                kind="place_on_timeline", label="放入时间线（需审批）", enabled=True,
            ))
    ```
    实际调用方 result_card_builder.py:130-137 以默认 `approved=False` 构造 generated_media 卡。
  - 影响: "导入媒体池" 是写媒体池的应用类操作，`approved` 参数完全未被消费即 enabled=True（连 disabled_reason 都未给）；未审批的 untrusted/unverified 生成素材可被一键入池，绕过模块声明的审批红线。编辑类（variant/script/subtitle_draft）的 apply 则严格 `enabled=approved and not applied and trust != "untrusted"`（177-185），同类行为不一致。
  - 建议: `import_to_pool` 至少 `enabled=approved and not applied`（或拆成"提案+审批流"两步）；`place_on_timeline` 若语义是"提出待审批方案"应在注释/label 与 enabled 逻辑中显式区分提案与执行。
  - 置信度: 中（place_on_timeline 的"需审批"措辞暗示提案语义，但 import_to_pool 无任何审批门是明确的）

- [AUD-22211] [Suggestion] preview_diff 分支仅在 not applied 时追加，其 enabled=not applied 与 disabled_reason 的 applied 分支均为死代码
  - 位置: src/features/ai_harness/result_card.py:164-169
  - 证据:
    ```python
        if not applied:
            actions.append(ResultAction(
                kind="preview_diff", label="预览 diff",
                enabled=not applied, disabled_reason="已应用，无需预览" if applied else "",
            ))
    ```
  - 影响: 追加时 applied 恒为 False，`enabled` 恒 True、`disabled_reason` 恒 ""；本应表达的"已应用后禁用预览"语义丢失（applied 时动作被整体省略而非禁用展示），且误导后续维护者以为存在禁用态渲染。
  - 建议: 二选一：applied 时也追加并 `enabled=False, disabled_reason="已应用，无需预览"`（与其他动作的禁用展示一致），或删掉恒假的分支只保留 `enabled=True`。
  - 置信度: 高

### 通过项
- 状态提交一致性（审批红线）：applied/approved 严格分层（status_label 四态：失败/已应用/已审批（待应用）/提议（未审批））；编辑类 apply 的 enabled 覆盖 approved、applied、trust 三条件且 disabled_reason 与之一一对应；applied 后提供"撤销应用"（discard）
- 输入验证/信任标注：trust 三级（trusted/unverified/untrusted）贯穿 apply 门禁与 trust_label；subtitle_draft/generated_media 工厂默认 unverified 待核符合"OCR/网页不可信文本不当事实"的约束
- 异常与取消路径：error 卡恒提供 retry（+可选诊断）；不可信结果保留"查看依据/调整参数"人工核对通道
- 线程安全：纯 frozen dataclass 工厂函数，无可变共享状态
- GPU/GLSL/HDR/信号防递归：不适用（Qt-free 状态模型，UI 渲染在 ai_result_card.py）
- 性能模式：动作集合为小元组，无热点
- 类型注解：完整（Literal 类型别名约束 kind/trust/action 枚举面）

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 4 | AUD-22201, AUD-22202, AUD-22205, AUD-22210 |
| Suggestion | 8 | AUD-22200, AUD-22203, AUD-22204, AUD-22206, AUD-22207, AUD-22208, AUD-22209, AUD-22211 |
